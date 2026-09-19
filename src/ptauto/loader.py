"""Reading a network specification from YAML.

Parsing is the cheap half; the valuable half is the cross-checking that happens
afterwards, against the same device catalog Packet Tracer is driven with. A
typo'd port name, a cable between two ports that cannot take one, two devices
claiming the same interface: all of that is caught here, before anything is
sent to PT, because a half-built topology is much more annoying than a rejected
file.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .catalog import model_info, normalise_port, resolve_cable
from .errors import SpecError
from .model import (
    ComponentSpec,
    ConnectionSpec,
    DeviceSettings,
    NetworkSpec,
)


def load_spec(path: str | Path) -> NetworkSpec:
    """Load, merge and validate a network specification file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"No such specification file: {path}") from None
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: invalid YAML: {exc}") from None
    if raw is None:
        raise SpecError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: the top level must be a mapping, got {type(raw).__name__}")
    spec = parse_spec(raw, source=str(path))
    validate_spec(spec)
    return spec


def parse_spec(raw: dict[str, Any], source: str = "<spec>") -> NetworkSpec:
    """Turn a raw mapping into a NetworkSpec, without touching Packet Tracer."""
    data = dict(raw)
    data.setdefault("components", {})
    data["connections"] = [
        _parse_connection(item, index, source)
        for index, item in enumerate(data.get("connections") or [])
    ]

    try:
        spec = NetworkSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(f"{source}: {_format_validation_error(exc)}") from None

    # Coalesced form: `components.<name>.config` is folded into `configurations`,
    # which stays authoritative so a standalone section can override an inline one.
    merged: dict[str, DeviceSettings] = {}
    for name, component in spec.components.items():
        if component.config is not None:
            merged[name] = component.config
    for name, settings in spec.configurations.items():
        if name in merged:
            merged[name] = _merge_settings(merged[name], settings)
        else:
            merged[name] = settings
    spec.configurations = merged
    return spec


def _merge_settings(base: DeviceSettings, override: DeviceSettings) -> DeviceSettings:
    """Shallow-merge two settings blocks; anything set in `override` wins."""
    data = base.model_dump(exclude_unset=True, by_alias=True)
    data.update(override.model_dump(exclude_unset=True, by_alias=True))
    return DeviceSettings.model_validate(data)


def _parse_connection(item: Any, index: int, source: str) -> dict[str, Any]:
    """Accept every connection spelling and return the canonical mapping.

    Supported forms::

        - [Router:g0/0, SW-A:g0/1]
        - "Router:g0/0 <-> SW-A:g0/1"
        - {from: Router:g0/0, to: SW-A:g0/1, cable: straight}
        - {endpoints: [Router:g0/0, SW-A:g0/1]}
    """
    where = f"{source}: connections[{index}]"
    cable: str | None = None

    if isinstance(item, str):
        endpoints = _split_inline(item, where)
    elif isinstance(item, list):
        endpoints = item
    elif isinstance(item, dict):
        cable = item.get("cable")
        if "endpoints" in item:
            endpoints = item["endpoints"]
        elif "from" in item and "to" in item:
            endpoints = [item["from"], item["to"]]
        else:
            raise SpecError(
                f"{where}: needs either `endpoints: [a, b]` or `from:`/`to:`"
            )
        unknown = set(item) - {"endpoints", "from", "to", "cable"}
        if unknown:
            raise SpecError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    else:
        raise SpecError(f"{where}: expected a list, string or mapping")

    if not isinstance(endpoints, list) or len(endpoints) != 2:
        raise SpecError(f"{where}: a connection joins exactly two endpoints")

    device_a, port_a = _split_endpoint(endpoints[0], where)
    device_b, port_b = _split_endpoint(endpoints[1], where)
    return {
        "device_a": device_a,
        "port_a": port_a,
        "device_b": device_b,
        "port_b": port_b,
        "cable": resolve_cable(cable),
    }


def _split_inline(text: str, where: str) -> list[str]:
    for separator in ("<->", "<-->", "--", "<>"):
        if separator in text:
            return [part.strip() for part in text.split(separator, 1)]
    raise SpecError(
        f"{where}: {text!r} is not a connection, write 'A:port <-> B:port' "
        f"or use the list form"
    )


def _split_endpoint(text: Any, where: str) -> tuple[str, str]:
    if isinstance(text, dict):
        try:
            return str(text["device"]).strip(), normalise_port(str(text["port"]))
        except KeyError:
            raise SpecError(f"{where}: an endpoint mapping needs `device` and `port`") from None
    if not isinstance(text, str) or ":" not in text:
        raise SpecError(
            f"{where}: {text!r} is not an endpoint, write it as 'DeviceName:PortName'"
        )
    device, _, port = text.partition(":")
    device, port = device.strip(), port.strip()
    if not device or not port:
        raise SpecError(f"{where}: {text!r} is missing the device or the port")
    return device, normalise_port(port)


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = " -> ".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"{location}: {error['msg']}")
    return "invalid specification\n  " + "\n  ".join(lines)


# --------------------------------------------------------------------------
# cross-validation
# --------------------------------------------------------------------------


def validate_spec(spec: NetworkSpec) -> None:
    """Every check that needs the whole document, or the device catalog."""
    problems: list[str] = []
    problems += _check_components(spec)
    problems += _check_connections(spec)
    problems += _check_configurations(spec)
    problems += _check_addressing(spec)
    if problems:
        raise SpecError(
            "the specification has "
            + (f"{len(problems)} problems:" if len(problems) > 1 else "a problem:")
            + "\n  - "
            + "\n  - ".join(problems)
        )


def _check_components(spec: NetworkSpec) -> list[str]:
    problems = []
    if not spec.components:
        problems.append("`components` is empty: there is nothing to build")
    for name, component in spec.components.items():
        if not name.strip():
            problems.append("a component has an empty name")
        try:
            model_info(component.model)
        except SpecError as exc:
            problems.append(f"{name}: {exc}")
    return problems


def _check_connections(spec: NetworkSpec) -> list[str]:
    problems: list[str] = []
    claimed: dict[tuple[str, str], str] = {}
    seen: set[frozenset] = set()

    for connection in spec.connections:
        for device, port in (
            (connection.device_a, connection.port_a),
            (connection.device_b, connection.port_b),
        ):
            component = spec.components.get(device)
            if component is None:
                problems.append(
                    f"connection {connection}: no component named {device!r}"
                )
                continue
            try:
                info = model_info(component.model)
            except SpecError:
                continue  # already reported by _check_components
            if port not in info.ports:
                problems.append(
                    f"connection {connection}: {device} ({component.model}) has no "
                    f"port {port!r}. Available: {', '.join(info.ports)}"
                )
                continue
            previous = claimed.get((device, port))
            if previous is not None:
                problems.append(
                    f"{device}:{port} is cabled twice, by '{previous}' and by "
                    f"'{connection}'. A PT port takes one cable."
                )
            else:
                claimed[(device, port)] = str(connection)

        if connection.device_a == connection.device_b and connection.port_a == connection.port_b:
            problems.append(f"connection {connection}: both ends are the same port")
        if connection.key in seen:
            problems.append(f"connection {connection} is listed twice")
        seen.add(connection.key)

    return problems


def _check_configurations(spec: NetworkSpec) -> list[str]:
    problems: list[str] = []
    for name, settings in spec.configurations.items():
        component = spec.components.get(name)
        if component is None:
            problems.append(
                f"configurations has an entry for {name!r}, which is not a component"
            )
            continue
        try:
            info = model_info(component.model)
        except SpecError:
            continue

        if info.is_ios:
            misplaced = settings.used_fields(DeviceSettings.HOST_FIELDS)
            if misplaced:
                problems.append(
                    f"{name} is a {info.category} configured with host-only "
                    f"setting(s): {', '.join(misplaced)}. Configure it under "
                    f"`interfaces:` instead."
                )
            problems += _check_ios_interfaces(name, info, settings)
        elif info.is_host:
            misplaced = settings.used_fields(DeviceSettings.IOS_FIELDS)
            if misplaced:
                problems.append(
                    f"{name} is a {info.category} configured with IOS-only "
                    f"setting(s): {', '.join(misplaced)}."
                )
            if settings.dhcp_client and settings.address:
                problems.append(
                    f"{name}: `dhcp_client: true` and a static `address:` "
                    f"contradict each other: pick one."
                )
            if settings.address is None and not settings.dhcp_client:
                problems.append(
                    f"{name}: needs either `address:` or `dhcp_client: true`."
                )
            if settings.services and info.category != "server":
                problems.append(
                    f"{name} is a {info.category}: only a server hosts `services:`."
                )
    return problems


def _check_ios_interfaces(name: str, info, settings: DeviceSettings) -> list[str]:
    problems = []
    for port, iface in settings.interfaces.items():
        # SVIs and subinterfaces are not physical ports, so they are not in the
        # catalog; everything else has to be a port the model really has.
        is_virtual = port.startswith("Vlan") or "." in port
        if not is_virtual and port not in info.ports:
            problems.append(
                f"{name}: no interface {port!r} on a {info.pt_type}. "
                f"Available: {', '.join(info.ports)}"
            )
        if iface.mode and info.category != "switch":
            problems.append(
                f"{name}: `mode: {iface.mode}` on {port}: switchport settings "
                f"only apply to a switch."
            )
    if settings.dhcp and info.category != "router":
        problems.append(f"{name}: `dhcp:` pools are configured on a router.")
    return problems


def _check_addressing(spec: NetworkSpec) -> list[str]:
    """Catch the addressing mistakes that survive per-field validation."""
    problems: list[str] = []
    addresses: dict[str, str] = {}
    subnets: list[tuple[str, ipaddress.IPv4Network]] = []

    for name, settings in spec.configurations.items():
        entries: list[tuple[str, str]] = []
        if settings.address:
            entries.append((name, settings.address))
        for port, iface in settings.interfaces.items():
            if iface.address:
                entries.append((f"{name}:{port}", iface.address))

        for label, value in entries:
            try:
                iface_obj = ipaddress.ip_interface(
                    value if "/" in value else f"{value.split()[0]}/{_mask_prefix(value)}"
                )
            except ValueError:
                continue  # field validation already rejected it
            key = str(iface_obj.ip)
            if key in addresses:
                problems.append(
                    f"{label} and {addresses[key]} are both {key}: duplicate address"
                )
            else:
                addresses[key] = label
            if iface_obj.ip == iface_obj.network.network_address and iface_obj.network.prefixlen < 31:
                problems.append(f"{label}: {key} is the network address of {iface_obj.network}")
            if iface_obj.ip == iface_obj.network.broadcast_address and iface_obj.network.prefixlen < 31:
                problems.append(f"{label}: {key} is the broadcast address of {iface_obj.network}")
            subnets.append((label, iface_obj.network))

        if settings.dhcp:
            for pool_name, pool in settings.dhcp.pools.items():
                pool_net = ipaddress.ip_network(pool.network, strict=False)
                router = pool.default_router
                if router and ipaddress.ip_address(router) not in pool_net:
                    problems.append(
                        f"{name}: DHCP pool {pool_name} hands out {pool_net} but its "
                        f"default-router {router} is outside it"
                    )
                if router and router not in addresses:
                    problems.append(
                        f"{name}: DHCP pool {pool_name} points at default-router "
                        f"{router}, which no device in this spec owns"
                    )

        # Hosts must be able to reach their own gateway.
        if settings.gateway and settings.address:
            host = ipaddress.ip_interface(settings.address)
            if ipaddress.ip_address(settings.gateway) not in host.network:
                problems.append(
                    f"{name}: gateway {settings.gateway} is outside the host's own "
                    f"subnet {host.network}"
                )
    return problems


def _mask_prefix(value: str) -> int:
    parts = value.split()
    if len(parts) == 2:
        return ipaddress.IPv4Network(f"0.0.0.0/{parts[1]}").prefixlen
    return 32
