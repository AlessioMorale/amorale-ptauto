"""Rendering IOS configuration, and deciding whether it is already there.

Every piece of IOS configuration ptauto can apply is expressed as a
`ConfigBlock`: the commands that put it in place, plus the evidence that proves
it is already in place. Convergence is then a text question against the device's
own configuration, which is what makes `ptauto apply` idempotent rather than
"just run it again and hope the commands are harmless".

The config text ptauto compares against comes from the device itself
(`getStartupFile()` after a `write memory`), so the comparison is against what
IOS *normalised*, not against what someone typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import DeviceSettings, InterfaceSpec

# IOS abbreviates nothing in its own output, but it does collapse whitespace and
# drop defaults. Comparisons are therefore done on squeezed whitespace.
_WS = re.compile(r"\s+")


def _norm(line: str) -> str:
    return _WS.sub(" ", line.strip())


@dataclass
class IosConfig:
    """A device configuration parsed into global lines and indented sections."""

    globals: list[str] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    raw: str = ""

    def has_global(self, line: str) -> bool:
        return _norm(line) in self.globals

    def has_in(self, section: str, line: str) -> bool:
        return _norm(line) in self.sections.get(_norm(section), [])

    def section_exists(self, section: str) -> bool:
        return _norm(section) in self.sections

    @property
    def hostname(self) -> str | None:
        for line in self.globals:
            if line.startswith("hostname "):
                return line.split(" ", 1)[1]
        return None


def parse_config(text: str) -> IosConfig:
    """Parse IOS configuration text into globals and one level of sections.

    PT returns the startup file with the lines joined by commas; real newlines
    are handled the same way, so either shape parses.
    """
    if "\n" not in text and "," in text:
        text = text.replace(",", "\n")
    config = IosConfig(raw=text)
    current: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.strip() == "!":
            current = None
            continue
        if raw_line.startswith((" ", "\t")):
            if current is not None:
                config.sections.setdefault(current, []).append(_norm(raw_line))
            continue
        line = _norm(raw_line)
        config.globals.append(line)
        # A line that can own indented children opens a section.
        config.sections.setdefault(line, [])
        current = line
    return config


@dataclass
class ConfigBlock:
    """One unit of configuration: how to apply it, and how to know it is applied."""

    label: str
    commands: list[str]
    section: str | None = None
    required: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)

    def is_converged(self, config: IosConfig | None) -> bool | None:
        """True / False, or None when the device's config could not be read."""
        if config is None:
            return None
        if self.section is not None:
            if not config.section_exists(self.section):
                return False
            for line in self.required:
                if not config.has_in(self.section, line):
                    return False
            for line in self.forbidden:
                if config.has_in(self.section, line):
                    return False
            return True
        for line in self.required:
            if not config.has_global(line):
                return False
        for line in self.forbidden:
            if config.has_global(line):
                return False
        return True

    def missing(self, config: IosConfig | None) -> list[str]:
        """The specific evidence that is absent, for a readable plan."""
        if config is None:
            return ["device configuration could not be read"]
        out = []
        if self.section is not None and not config.section_exists(self.section):
            return [f"no `{self.section}`"]
        for line in self.required:
            present = (
                config.has_in(self.section, line)
                if self.section
                else config.has_global(line)
            )
            if not present:
                out.append(f"missing `{line}`")
        for line in self.forbidden:
            present = (
                config.has_in(self.section, line)
                if self.section
                else config.has_global(line)
            )
            if present:
                out.append(f"still has `{line}`")
        return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render_blocks(device: str, settings: DeviceSettings, category: str) -> list[ConfigBlock]:
    """Every configuration block implied by `settings`, in application order."""
    blocks: list[ConfigBlock] = []
    hostname = settings.hostname or device

    blocks.append(
        ConfigBlock(
            label=f"hostname {hostname}",
            commands=[f"hostname {hostname}"],
            required=[f"hostname {hostname}"],
        )
    )

    if settings.domain_lookup is False:
        blocks.append(
            ConfigBlock(
                label="disable DNS lookup",
                commands=["no ip domain-lookup"],
                required=["no ip domain-lookup"],
            )
        )

    if settings.enable_secret:
        # The secret is stored hashed, so its presence (not its value) is what
        # can be verified; the command itself is idempotent.
        blocks.append(
            ConfigBlock(
                label="enable secret",
                commands=[f"enable secret {settings.enable_secret}"],
                required=["enable secret"],
            )
        )

    if settings.banner:
        blocks.append(
            ConfigBlock(
                label="banner motd",
                commands=[f"banner motd #{settings.banner}#"],
                required=["banner motd"],
            )
        )

    for number, name in sorted(settings.vlans.items()):
        blocks.append(
            ConfigBlock(
                label=f"vlan {number} ({name})",
                commands=[f"vlan {number}", f" name {name}", " exit"],
                section=f"vlan {number}",
                required=[f"name {name}"],
            )
        )

    for port, iface in settings.interfaces.items():
        blocks.append(_interface_block(port, iface))

    if settings.default_gateway:
        blocks.append(
            ConfigBlock(
                label=f"default gateway {settings.default_gateway}",
                commands=[f"ip default-gateway {settings.default_gateway}"],
                required=[f"ip default-gateway {settings.default_gateway}"],
            )
        )

    if settings.dhcp:
        blocks += _dhcp_blocks(settings)

    for route in settings.static_routes:
        network, mask = _network_mask(route.destination)
        line = f"ip route {network} {mask} {route.next_hop}"
        blocks.append(ConfigBlock(label=line, commands=[line], required=[line]))

    if settings.extra_cli:
        blocks += _extra_cli_blocks(settings.extra_cli)

    return blocks


# IOS never writes `no shutdown` back into a saved configuration: an
# interface that is up is one with no `shutdown` line at all, which is exactly
# how `_interface_block` above already checks it (`forbidden=["shutdown"]`,
# not `required=["no shutdown"]`). The generic line-by-line matcher below has
# no way to know that on its own, so without this one line it would treat a
# bare `no shutdown` as literal text to find (text IOS will never write)
# and report the interface as pending forever. It is the same class of
# "IOS rewrites this" case the module already documents for `enable_secret`,
# just narrow and common enough to special-case rather than leave as a trap.
_NO_SHUTDOWN = "no shutdown"


def _extra_cli_blocks(lines: list[str]) -> list[ConfigBlock]:
    """Group bare CLI lines the way IOS itself groups them: by indentation.

    A line with no leading whitespace opens global config (or a submode, if
    it's something like `line vty 0 4`); every line indented under it is that
    line's child, checked against the *submode's* text, not the top level,
    which is exactly how the earlier version of this function got it wrong.
    Checking every line against `has_global` meant a login banner or an ACL
    entry sitting one line deep could never be found, so ptauto reported it as
    still missing and re-sent it on every single run.
    """
    blocks: list[ConfigBlock] = []
    top: str | None = None
    commands: list[str] = []
    required: list[str] = []
    forbidden: list[str] = []

    def flush() -> None:
        if top is None:
            return
        if required or forbidden:
            # Mirrors every other multi-line block in this module: enter,
            # configure, leave, unless the caller's own lines already do.
            if _norm(commands[-1]) not in ("exit", "end"):
                commands.append(" exit")
            blocks.append(
                ConfigBlock(
                    label=top,
                    commands=list(commands),
                    section=top,
                    required=list(required),
                    forbidden=list(forbidden),
                )
            )
        else:
            blocks.append(ConfigBlock(label=top, commands=list(commands), required=[top]))

    for raw in lines:
        if raw.startswith((" ", "\t")):
            if top is None:
                # A child with nothing to attach to (a stray leading space, or
                # the spec's first extra_cli line is indented by mistake) is
                # still a real command; treat it as its own global line rather
                # than silently dropping it.
                top, commands, required, forbidden = _norm(raw), [raw], [], []
            else:
                commands.append(raw)
                if _norm(raw) == _NO_SHUTDOWN:
                    forbidden.append("shutdown")
                else:
                    required.append(_norm(raw))
        else:
            flush()
            top, commands, required, forbidden = _norm(raw), [raw], [], []
    flush()
    return blocks


def _interface_block(port: str, iface: InterfaceSpec) -> ConfigBlock:
    commands = [f"interface {port}"]
    required: list[str] = []
    forbidden: list[str] = []

    if iface.description:
        commands.append(f" description {iface.description}")
        required.append(f"description {iface.description}")

    if iface.address:
        from .model import split_address

        ip, mask = split_address(iface.address)
        commands.append(f" ip address {ip} {mask}")
        required.append(f"ip address {ip} {mask}")

    if iface.mode == "access":
        commands.append(" switchport mode access")
        required.append("switchport mode access")
        if iface.vlan:
            commands.append(f" switchport access vlan {iface.vlan}")
            required.append(f"switchport access vlan {iface.vlan}")
    elif iface.mode == "trunk":
        commands.append(" switchport mode trunk")
        required.append("switchport mode trunk")
        if iface.trunk_vlans:
            allowed = ",".join(str(v) for v in iface.trunk_vlans)
            commands.append(f" switchport trunk allowed vlan {allowed}")
            required.append(f"switchport trunk allowed vlan {allowed}")

    for line in iface.extra:
        commands.append(f" {line.strip()}")
        required.append(line.strip())

    if iface.shutdown:
        commands.append(" shutdown")
        required.append("shutdown")
    else:
        # IOS omits `no shutdown` from its config: an enabled interface is one
        # with no `shutdown` line, which is exactly what `forbidden` expresses.
        commands.append(" no shutdown")
        forbidden.append("shutdown")

    commands.append(" exit")
    return ConfigBlock(
        label=f"interface {port}",
        commands=commands,
        section=f"interface {port}",
        required=required,
        forbidden=forbidden,
    )


def _dhcp_blocks(settings: DeviceSettings) -> list[ConfigBlock]:
    blocks: list[ConfigBlock] = []
    assert settings.dhcp is not None

    for excluded in settings.dhcp.excluded:
        line = f"ip dhcp excluded-address {_norm(excluded)}"
        blocks.append(ConfigBlock(label=line, commands=[line], required=[line]))

    for name, pool in settings.dhcp.pools.items():
        network, mask = pool.network_and_mask
        commands = [f"ip dhcp pool {name}", f" network {network} {mask}"]
        required = [f"network {network} {mask}"]
        if pool.default_router:
            commands.append(f" default-router {pool.default_router}")
            required.append(f"default-router {pool.default_router}")
        if pool.dns_server:
            commands.append(f" dns-server {pool.dns_server}")
            required.append(f"dns-server {pool.dns_server}")
        if pool.domain:
            commands.append(f" domain-name {pool.domain}")
            required.append(f"domain-name {pool.domain}")
        commands.append(" exit")
        blocks.append(
            ConfigBlock(
                label=f"dhcp pool {name}",
                commands=commands,
                section=f"ip dhcp pool {name}",
                required=required,
            )
        )
    return blocks


def _network_mask(cidr: str) -> tuple[str, str]:
    from .model import network_and_mask

    return network_and_mask(cidr)


def build_cli(blocks: list[ConfigBlock], save: bool = True) -> str:
    """Wrap configuration blocks into the script `configureIosDevice` expects.

    The trailing `write memory` is not a nicety: the saved configuration is what
    ptauto reads back to decide, next run, that there is nothing to do.
    """
    lines = ["enable", "configure terminal"]
    for block in blocks:
        lines.extend(block.commands)
    lines.append("end")
    if save:
        lines.append("write memory")
    return "\n".join(lines)
