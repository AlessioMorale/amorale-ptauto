"""Deciding what has to change: the part that makes `apply` idempotent.

The planner reads what Packet Tracer actually has, compares it with the
specification, and emits one action per real difference. A converged network
produces an empty plan, and an empty plan means `apply` sends nothing to PT.

Everything the planner cannot verify is reported as such instead of being
silently re-applied: a block marked `verified=False` is work ptauto is doing
without proof that it was needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import ModelInfo, infer_cable_for, model_info
from .client import HostState, ObservedTopology, PTClient
from .errors import PTError, PTTimeout
from .ios import ConfigBlock, IosConfig, build_cli, parse_config, render_blocks
from .model import DeviceSettings, NetworkSpec, split_address

# PT inserts infrastructure objects of its own (the power distribution device
# that appears alongside powered end devices). They are not part of any spec and
# must never be treated as strays to prune.
PT_MANAGED_MODELS = frozenset({"Power Distribution Device"})

# Devices are only nudged when they are meaningfully off-position; PT rounds
# coordinates, so an exact comparison would move everything on every run.
POSITION_TOLERANCE = 6


@dataclass
class Action:
    """One change to make in Packet Tracer."""

    kind: str
    target: str
    summary: str
    reason: str = ""
    payload: dict = field(default_factory=dict)
    verified: bool = True  # False = ptauto could not confirm it was needed
    destructive: bool = False

    def __str__(self) -> str:
        return self.summary


@dataclass
class Plan:
    """The full set of changes, plus what was already in place."""

    actions: list[Action] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.actions

    @property
    def destructive_actions(self) -> list[Action]:
        return [a for a in self.actions if a.destructive]

    def by_kind(self, kind: str) -> list[Action]:
        return [a for a in self.actions if a.kind == kind]


class Planner:
    """Builds a `Plan` by comparing a spec against a live Packet Tracer."""

    def __init__(
        self,
        client: PTClient,
        spec: NetworkSpec,
        prune: bool = False,
        refresh_configs: bool = True,
    ) -> None:
        self.client = client
        self.spec = spec
        self.prune = prune
        self.refresh_configs = refresh_configs
        self.plan = Plan()
        self.topology: ObservedTopology = ObservedTopology()
        self._models: dict[str, ModelInfo] = {}

    # -- entry point ------------------------------------------------------

    def build(self) -> Plan:
        self.topology = self.client.topology()
        self._models = {
            name: model_info(component.model)
            for name, component in self.spec.components.items()
        }
        self._plan_devices()
        self._plan_links()
        self._plan_configuration()
        return self.plan

    # -- devices -----------------------------------------------------------

    def _plan_devices(self) -> None:
        for name, component in self.spec.components.items():
            info = self._models[name]
            observed = self.topology.devices.get(name)
            x, y = component.position or self._auto_position(name)

            if observed is None:
                self.plan.actions.append(
                    Action(
                        kind="create_device",
                        target=name,
                        summary=f"create {name} ({info.pt_type}) at ({x}, {y})",
                        reason="not present in Packet Tracer",
                        payload={"info": info, "x": x, "y": y},
                    )
                )
                continue

            if not _same_model(observed.model, info.pt_type):
                if self.prune:
                    self.plan.actions.append(
                        Action(
                            kind="replace_device",
                            target=name,
                            summary=f"replace {name}: {observed.model} -> {info.pt_type}",
                            reason=f"model differs from the spec ({info.pt_type})",
                            payload={"info": info, "x": x, "y": y},
                            destructive=True,
                        )
                    )
                else:
                    self.plan.warnings.append(
                        f"{name} is a {observed.model} in PT but the spec says "
                        f"{info.pt_type}. Re-run with --prune to replace it."
                    )
                continue

            self.plan.unchanged.append(f"{name} ({info.pt_type}) exists")
            if component.position and (
                abs(observed.x - x) > POSITION_TOLERANCE
                or abs(observed.y - y) > POSITION_TOLERANCE
            ):
                self.plan.actions.append(
                    Action(
                        kind="move_device",
                        target=name,
                        summary=f"move {name} to ({x}, {y})",
                        reason=f"currently at ({observed.x}, {observed.y})",
                        payload={"x": x, "y": y},
                    )
                )

        if self.prune:
            for name, observed in self.topology.devices.items():
                if name in self.spec.components:
                    continue
                if observed.model in PT_MANAGED_MODELS:
                    continue
                self.plan.actions.append(
                    Action(
                        kind="delete_device",
                        target=name,
                        summary=f"delete {name} ({observed.model})",
                        reason="present in Packet Tracer but not in the spec",
                        destructive=True,
                    )
                )

    def _auto_position(self, name: str) -> tuple[int, int]:
        """A tidy default spot for a device with no `position:` in the spec."""
        index = list(self.spec.components).index(name)
        return 120 + (index % 6) * 140, 120 + (index // 6) * 160

    # -- links --------------------------------------------------------------

    def _plan_links(self) -> None:
        observed_links = self.topology.link_keys()
        desired_keys = set()

        for connection in self.spec.connections:
            key = connection.key
            desired_keys.add(key)
            if key in observed_links:
                self.plan.unchanged.append(f"cable {connection} exists")
                continue

            cable = connection.cable or self._infer_cable(connection)
            # A port that is already cabled elsewhere has to be freed first,
            # otherwise PT simply refuses the new link.
            for device, port in (
                (connection.device_a, connection.port_a),
                (connection.device_b, connection.port_b),
            ):
                if self.topology.port_is_cabled(device, port):
                    self.plan.actions.append(
                        Action(
                            kind="delete_link",
                            target=f"{device}:{port}",
                            summary=f"remove the existing cable on {device}:{port}",
                            reason=f"the spec cables it as {connection}",
                            payload={"device": device, "port": port},
                            destructive=True,
                        )
                    )

            self.plan.actions.append(
                Action(
                    kind="create_link",
                    target=str(connection),
                    summary=f"cable {connection.device_a}:{connection.port_a} <-> "
                    f"{connection.device_b}:{connection.port_b} ({cable})",
                    reason="cable missing",
                    payload={
                        "device_a": connection.device_a,
                        "port_a": connection.port_a,
                        "device_b": connection.device_b,
                        "port_b": connection.port_b,
                        "cable": cable,
                    },
                )
            )

        if self.prune:
            for key, link in observed_links.items():
                if key in desired_keys:
                    continue
                if not (
                    link.device_a in self.spec.components
                    or link.device_b in self.spec.components
                ):
                    continue  # nothing to do with this spec
                self.plan.actions.append(
                    Action(
                        kind="delete_link",
                        target=f"{link.device_a}:{link.port_a}",
                        summary=f"remove cable {link.device_a}:{link.port_a} <-> "
                        f"{link.device_b}:{link.port_b}",
                        reason="cable present in PT but not in the spec",
                        payload={"device": link.device_a, "port": link.port_a},
                        destructive=True,
                    )
                )

    def _infer_cable(self, connection) -> str:
        info_a = self._models.get(connection.device_a)
        info_b = self._models.get(connection.device_b)
        if info_a is None or info_b is None:
            return "auto"
        return infer_cable_for(info_a.category, info_b.category)

    # -- configuration --------------------------------------------------------

    def _plan_configuration(self) -> None:
        for name in self.spec.components:
            settings = self.spec.settings_for(name)
            info = self._models[name]
            exists = name in self.topology.devices
            if info.is_ios:
                self._plan_ios(name, settings, info, exists)
            elif info.is_host:
                self._plan_host(name, settings, exists)
                if settings.services:
                    self._plan_services(name, settings, exists)

    def _plan_ios(
        self, name: str, settings: DeviceSettings, info: ModelInfo, exists: bool
    ) -> None:
        if not settings.used_fields(DeviceSettings.IOS_FIELDS):
            # Nothing was asked of this device, so leave it exactly as it is.
            # `render_blocks` always includes a default hostname push otherwise,
            # harmless for a router or switch, but some IOS-categorised
            # models (a plain AccessPoint-PT, unlike an enterprise 3702i) accept
            # no CLI at all in Packet Tracer. Pushing that implicit default
            # would not converge; it would fail outright, every single run.
            return

        blocks = render_blocks(name, settings, info.category)
        if not blocks:
            return

        config: IosConfig | None = None
        if exists:
            config = self._read_config(name)

        pending: list[ConfigBlock] = []
        # A device this run is about to create needs everything, and that is not
        # guesswork: "unverified" is reserved for a device that exists but whose
        # configuration ptauto could not read.
        unverified = False
        for block in blocks:
            state = block.is_converged(config)
            if state is True:
                continue
            if state is None and exists:
                unverified = True
            pending.append(block)

        if not pending:
            self.plan.unchanged.append(f"{name}: IOS configuration matches the spec")
            return

        if exists and config is not None:
            reason = "; ".join(
                f"{block.label}: {', '.join(block.missing(config))}"
                for block in pending[:4]
            )
            if len(pending) > 4:
                reason += f"; and {len(pending) - 4} more"
        elif exists:
            reason = "the device's configuration could not be read"
        else:
            reason = "the device is being created by this run"

        self.plan.actions.append(
            Action(
                kind="configure_ios",
                target=name,
                summary=f"configure {name}: {len(pending)} block(s) "
                f"({', '.join(b.label for b in pending[:3])}"
                f"{', …' if len(pending) > 3 else ''})",
                reason=reason,
                payload={"cli": build_cli(pending), "blocks": [b.label for b in pending]},
                verified=not unverified,
            )
        )

        # VLANs have their own database, which the text config does not always
        # show; check it directly when the switch already exists.
        if exists and settings.vlans:
            self._check_vlans(name, settings)

    def _check_vlans(self, name: str, settings: DeviceSettings) -> None:
        try:
            observed = self.client.read_vlans(name)
        except (PTError, PTTimeout) as exc:
            self.plan.warnings.append(f"{name}: could not read the VLAN database ({exc})")
            return
        if observed is None:
            return
        missing = {
            number: vlan_name
            for number, vlan_name in settings.vlans.items()
            if observed.get(number) != vlan_name
        }
        if missing:
            self.plan.warnings.append(
                f"{name}: VLAN(s) {', '.join(str(v) for v in missing)} are not in the "
                f"switch's VLAN database yet; this run creates them."
            )

    def _read_config(self, name: str) -> IosConfig | None:
        try:
            text = self.client.read_ios_config(name, refresh=self.refresh_configs)
        except (PTError, PTTimeout) as exc:
            self.plan.warnings.append(
                f"{name}: configuration could not be read ({exc}); ptauto will "
                f"re-apply its configuration without being able to check it first."
            )
            return None
        if text is None:
            return None
        return parse_config(text)

    def _plan_host(self, name: str, settings: DeviceSettings, exists: bool) -> None:
        if settings.dhcp_client is None and settings.address is None:
            return

        desired_dhcp = bool(settings.dhcp_client)
        desired_ip, desired_mask = ("", "")
        if settings.address:
            desired_ip, desired_mask = split_address(settings.address)

        observed: HostState | None = None
        if exists:
            try:
                observed = self.client.read_host(name)
            except (PTError, PTTimeout) as exc:
                self.plan.warnings.append(f"{name}: IP settings could not be read ({exc})")

        differences = self._host_differences(
            observed, desired_dhcp, desired_ip, desired_mask, settings
        )
        if observed is not None and not differences:
            self.plan.unchanged.append(f"{name}: IP settings match the spec")
            return

        summary = (
            f"set {name} to DHCP"
            if desired_dhcp
            else f"address {name} as {desired_ip}/{desired_mask}"
        )
        self.plan.actions.append(
            Action(
                kind="configure_host",
                target=name,
                summary=summary,
                reason="; ".join(differences)
                or ("the device is being created by this run" if not exists else "unknown state"),
                payload={
                    "dhcp": desired_dhcp,
                    "ip": desired_ip,
                    "mask": desired_mask,
                    "gateway": settings.gateway or "",
                    "dns": settings.dns or "",
                },
                verified=(observed is not None) or not exists,
            )
        )

    @staticmethod
    def _host_differences(
        observed: HostState | None,
        desired_dhcp: bool,
        desired_ip: str,
        desired_mask: str,
        settings: DeviceSettings,
    ) -> list[str]:
        if observed is None or not observed.found:
            return []
        differences: list[str] = []
        if observed.dhcp != desired_dhcp:
            differences.append(
                f"DHCP is {'on' if observed.dhcp else 'off'}, spec wants "
                f"{'on' if desired_dhcp else 'off'}"
            )
        if not desired_dhcp:
            if observed.ip != desired_ip:
                differences.append(f"address is {observed.ip}, spec says {desired_ip}")
            if observed.mask != desired_mask:
                differences.append(f"mask is {observed.mask}, spec says {desired_mask}")
            if settings.gateway and observed.gateway != settings.gateway:
                differences.append(
                    f"gateway is {observed.gateway or 'unset'}, spec says {settings.gateway}"
                )
            if settings.dns and observed.dns != settings.dns:
                differences.append(
                    f"DNS server is {observed.dns or 'unset'}, spec says {settings.dns}"
                )
        elif observed.ip == "0.0.0.0":
            differences.append("DHCP is on but no lease has been obtained")
        return differences

    def _plan_services(self, name: str, settings: DeviceSettings, exists: bool) -> None:
        assert settings.services is not None
        observed: dict | None = None
        if exists:
            try:
                observed = self.client.read_services(name)
            except (PTError, PTTimeout) as exc:
                self.plan.warnings.append(f"{name}: services could not be read ({exc})")

        dns_spec = settings.services.dns
        if dns_spec is not None:
            wanted = [(r.name, r.address, r.type) for r in dns_spec.records]
            current = (observed or {}).get("dns") if observed else None
            differences = []
            if current is None and observed is not None:
                differences.append("the server exposes no DNS service")
            elif current is not None:
                if bool(current.get("enabled")) != dns_spec.enabled:
                    differences.append(
                        f"service is {'on' if current.get('enabled') else 'off'}, "
                        f"spec wants {'on' if dns_spec.enabled else 'off'}"
                    )
                have = {(r["name"], r["address"]) for r in current.get("records", [])}
                want = {(n, a) for n, a, _ in wanted}
                for record in sorted(want - have):
                    differences.append(f"missing record {record[0]} -> {record[1]}")
                for record in sorted(have - want):
                    differences.append(f"extra record {record[0]} -> {record[1]}")
            if differences or observed is None:
                self.plan.actions.append(
                    Action(
                        kind="configure_dns",
                        target=name,
                        summary=f"configure DNS on {name} "
                        f"({len(wanted)} record(s), service "
                        f"{'on' if dns_spec.enabled else 'off'})",
                        reason="; ".join(differences) or "the server is being created by this run",
                        payload={"enabled": dns_spec.enabled, "records": wanted},
                        verified=(observed is not None) or not exists,
                    )
                )
            else:
                self.plan.unchanged.append(f"{name}: DNS service matches the spec")

        http_spec = settings.services.http
        if http_spec is not None:
            current = (observed or {}).get("http") if observed else None
            differences = []
            if current is None and observed is not None:
                differences.append("the server exposes no HTTP service")
            elif current is not None:
                if bool(current.get("enabled")) != http_spec.enabled:
                    differences.append(
                        f"service is {'on' if current.get('enabled') else 'off'}, "
                        f"spec wants {'on' if http_spec.enabled else 'off'}"
                    )
                if http_spec.port and current.get("port") != http_spec.port:
                    differences.append(
                        f"listening on port {current.get('port')}, spec says {http_spec.port}"
                    )
                index = http_spec.pages.get("index.html")
                if index is not None and current.get("index") != index:
                    differences.append("index.html differs from the spec")
            if differences or observed is None:
                self.plan.actions.append(
                    Action(
                        kind="configure_http",
                        target=name,
                        summary=f"configure the web service on {name}"
                        + (f" ({len(http_spec.pages)} page(s))" if http_spec.pages else ""),
                        reason="; ".join(differences) or "the server is being created by this run",
                        payload={
                            "enabled": http_spec.enabled,
                            "pages": dict(http_spec.pages),
                            "port": http_spec.port,
                        },
                        verified=(observed is not None) or not exists,
                    )
                )
            else:
                self.plan.unchanged.append(f"{name}: web service matches the spec")


def _same_model(observed: str, expected: str) -> bool:
    """PT reports models with slight spelling differences between builds."""
    return observed.strip().lower().replace("-", "") == expected.strip().lower().replace("-", "")
