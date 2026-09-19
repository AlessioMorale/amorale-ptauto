"""The declarative network specification: the shape of the YAML file.

Three sections, as the requirements describe them:

    components      what devices exist, and what they are
    connections     how those devices are cabled together
    configurations  what settings each device carries

`components` may carry a `config:` block of its own, in which case the
`configurations` section is optional: the two are merged by the loader, with
the standalone section winning on conflict.

Every model is strict (`extra="forbid"`): a mistyped key is a loud error at load
time, not a setting that silently does nothing.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

# `Strict.model_config` strips whitespace from every string field, which is right
# for a hostname or a description but wrong for a raw CLI line: the leading
# space on " login" is not incidental formatting, it is what says the line is a
# child of the "line vty 0 4" above it. This type opts a field back out of that
# stripping so indentation typed in the YAML survives into the model.
RawCliLine = Annotated[str, StringConstraints(strip_whitespace=False)]

from .catalog import normalise_port


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# addressing helpers
# --------------------------------------------------------------------------


def split_address(value: str) -> tuple[str, str]:
    """'192.168.10.1/27' or '192.168.10.1 255.255.255.224' -> (ip, dotted mask)."""
    text = value.strip()
    if "/" in text:
        iface = ipaddress.ip_interface(text)
        return str(iface.ip), str(iface.netmask)
    parts = text.split()
    if len(parts) == 2:
        ipaddress.ip_address(parts[0])
        ipaddress.ip_address(parts[1])
        return parts[0], parts[1]
    raise ValueError(
        f"{value!r} is not an address: use '10.0.0.1/24' or '10.0.0.1 255.255.255.0'"
    )


def network_and_mask(value: str) -> tuple[str, str]:
    """'192.168.10.0/27' -> ('192.168.10.0', '255.255.255.224')."""
    net = ipaddress.ip_network(value.strip(), strict=False)
    return str(net.network_address), str(net.netmask)


# --------------------------------------------------------------------------
# configuration fragments
# --------------------------------------------------------------------------


class InterfaceSpec(Strict):
    """One interface on an IOS device (routed port, SVI or access port)."""

    address: str | None = Field(
        default=None,
        description="IPv4 address as 'a.b.c.d/prefix' or 'a.b.c.d 255.255.255.0'",
    )
    description: str | None = None
    shutdown: bool = False
    # Layer-2 switch port settings.
    mode: Literal["access", "trunk"] | None = None
    vlan: int | None = Field(default=None, ge=1, le=4094)
    trunk_vlans: list[int] | None = None
    # Anything the schema does not model, applied verbatim inside the interface.
    extra: list[str] = Field(default_factory=list)

    @field_validator("address")
    @classmethod
    def _check_address(cls, v: str | None) -> str | None:
        if v is not None:
            split_address(v)
        return v

    @model_validator(mode="after")
    def _check_switchport(self) -> InterfaceSpec:
        if self.vlan is not None and self.mode is None:
            self.mode = "access"
        if self.mode == "access" and self.trunk_vlans:
            raise ValueError("trunk_vlans is meaningless on an access port")
        return self

    @property
    def ip_and_mask(self) -> tuple[str, str] | None:
        return split_address(self.address) if self.address else None


class DhcpPoolSpec(Strict):
    """A router DHCP pool. `network` sizes the pool; the rest are the options."""

    network: str
    default_router: str | None = None
    dns_server: str | None = None
    domain: str | None = None

    @field_validator("network")
    @classmethod
    def _check_network(cls, v: str) -> str:
        network_and_mask(v)
        return v

    @property
    def network_and_mask(self) -> tuple[str, str]:
        return network_and_mask(self.network)


class DhcpSpec(Strict):
    """`ip dhcp` configuration on a router."""

    excluded: list[str] = Field(
        default_factory=list,
        description="Addresses or 'first last' ranges kept out of every pool",
    )
    pools: dict[str, DhcpPoolSpec] = Field(default_factory=dict)


class StaticRouteSpec(Strict):
    destination: str
    next_hop: str

    @field_validator("destination")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        network_and_mask(v)
        return v

    @field_validator("next_hop")
    @classmethod
    def _check_hop(cls, v: str) -> str:
        ipaddress.ip_address(v)
        return v


class DnsRecordSpec(Strict):
    name: str
    address: str
    type: Literal["A", "CNAME"] = "A"

    @model_validator(mode="after")
    def _check_target(self) -> DnsRecordSpec:
        if self.type == "A":
            ipaddress.ip_address(self.address)
        return self


class DnsServiceSpec(Strict):
    enabled: bool = True
    records: list[DnsRecordSpec] = Field(default_factory=list)


class HttpServiceSpec(Strict):
    enabled: bool = True
    # Page contents by filename, e.g. {"index.html": "<html>…</html>"}.
    pages: dict[str, str] = Field(default_factory=dict)
    port: int | None = Field(default=None, ge=1, le=65535)


class ServicesSpec(Strict):
    """Services hosted by a Server-PT (its Services tab, driven over the bridge)."""

    dns: DnsServiceSpec | None = None
    http: HttpServiceSpec | None = None


class DeviceSettings(Strict):
    """Everything that can be configured on one device.

    Host-only and IOS-only fields live in the same model so the YAML reads
    uniformly; `validate_for()` rejects the combinations that make no sense for a
    given device category.
    """

    # --- IOS devices (router / switch) ---
    hostname: str | None = None
    interfaces: dict[str, InterfaceSpec] = Field(default_factory=dict)
    vlans: dict[int, str] = Field(default_factory=dict)
    dhcp: DhcpSpec | None = None
    static_routes: list[StaticRouteSpec] = Field(default_factory=list)
    default_gateway: str | None = Field(
        default=None, description="`ip default-gateway` on a layer-2 switch"
    )
    domain_lookup: bool | None = None
    banner: str | None = None
    enable_secret: str | None = None
    extra_cli: list[RawCliLine] = Field(
        default_factory=list,
        description=(
            "Bare IOS commands, applied at global-config scope after everything "
            "else. Indentation matters: a line indented under a preceding "
            "line (e.g. ' login' under 'line vty 0 4') is treated as that "
            "line's submode child, exactly as `show running-config` prints it."
        ),
    )

    @field_validator("extra_cli", mode="before")
    @classmethod
    def _accept_raw_cli_block(cls, v: Any) -> Any:
        """Let `extra_cli` be written as one pasted block, not just a YAML list.

        YAML's `|` block scalar is the natural way to hand over real IOS
        output, indentation and all::

            extra_cli: |
              line vty 0 4
               login
               password cisco123
              logging synchronous

        A blank line or a bare `!` (IOS's own section separator) carries no
        command, so both are dropped rather than turned into a line ptauto
        then has to explain why it can never find in a real configuration.
        """
        if isinstance(v, str):
            return [
                line
                for line in v.splitlines()
                if line.strip() and line.strip() != "!"
            ]
        return v

    # --- hosts (PC / Server / Printer / Laptop) ---
    dhcp_client: bool | None = Field(
        default=None, alias="dhcp_client", description="True = obtain settings via DHCP"
    )
    address: str | None = None
    gateway: str | None = None
    dns: str | None = None
    services: ServicesSpec | None = None

    @field_validator("interfaces", mode="before")
    @classmethod
    def _normalise_interface_names(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {normalise_port(str(k)): val for k, val in v.items()}
        return v

    @field_validator("address")
    @classmethod
    def _check_address(cls, v: str | None) -> str | None:
        if v is not None:
            split_address(v)
        return v

    @field_validator("gateway", "dns", "default_gateway")
    @classmethod
    def _check_ip(cls, v: str | None) -> str | None:
        if v is not None:
            ipaddress.ip_address(v)
        return v

    @property
    def ip_and_mask(self) -> tuple[str, str] | None:
        return split_address(self.address) if self.address else None

    # Field groups, used for the "this setting does not apply here" check.
    IOS_FIELDS: ClassVar[tuple[str, ...]] = (
        "hostname",
        "interfaces",
        "vlans",
        "dhcp",
        "static_routes",
        "default_gateway",
        "domain_lookup",
        "banner",
        "enable_secret",
        "extra_cli",
    )
    HOST_FIELDS: ClassVar[tuple[str, ...]] = ("dhcp_client", "address", "gateway", "dns", "services")

    def used_fields(self, names: tuple[str, ...]) -> list[str]:
        used = []
        for name in names:
            value = getattr(self, name)
            if value not in (None, {}, [], ""):
                used.append(name)
        return used


# --------------------------------------------------------------------------
# topology
# --------------------------------------------------------------------------


class ComponentSpec(Strict):
    """One device in the `components` section."""

    model: str
    position: tuple[int, int] | None = None
    config: DeviceSettings | None = None

    @field_validator("model", mode="before")
    @classmethod
    def _as_text(cls, v: Any) -> Any:
        # YAML reads `model: 2911` as an integer; the catalog is keyed by text.
        return str(v) if isinstance(v, (int, float)) else v

    @field_validator("position", mode="before")
    @classmethod
    def _accept_mapping(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return (v.get("x", 0), v.get("y", 0))
        return v


class ConnectionSpec(Strict):
    """One cable. Ports are stored canonicalised so `g0/0` == `GigabitEthernet0/0`."""

    device_a: str
    port_a: str
    device_b: str
    port_b: str
    cable: str | None = None

    @property
    def key(self) -> frozenset[tuple[str, str]]:
        """Identity of the cable, independent of which end is written first."""
        return frozenset({(self.device_a, self.port_a), (self.device_b, self.port_b)})

    def __str__(self) -> str:
        cable = f" [{self.cable}]" if self.cable else ""
        return (
            f"{self.device_a}:{self.port_a} <-->{cable} {self.device_b}:{self.port_b}"
        )


class ProjectSpec(Strict):
    name: str = "ptauto"
    # Where `ptauto apply --save` writes the .pkt.
    save_as: str | None = None


class NetworkSpec(Strict):
    """A whole network description: the parsed YAML file."""

    version: Annotated[int, Field(ge=1, le=1)] = 1
    project: ProjectSpec = Field(default_factory=ProjectSpec)
    components: dict[str, ComponentSpec]
    connections: list[ConnectionSpec] = Field(default_factory=list)
    configurations: dict[str, DeviceSettings] = Field(default_factory=dict)

    def settings_for(self, device: str) -> DeviceSettings:
        return self.configurations.get(device, DeviceSettings())
