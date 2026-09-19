"""Rendering IOS configuration, and reading it back to decide convergence."""

from __future__ import annotations

from ptauto.ios import build_cli, parse_config, render_blocks
from ptauto.model import DeviceSettings

ROUTER_CONFIG = """
!
hostname Router-HQ
no ip domain-lookup
!
ip dhcp excluded-address 192.168.10.1
!
ip dhcp pool ADMIN
 network 192.168.10.0 255.255.255.224
 default-router 192.168.10.1
 dns-server 192.168.30.2
!
interface GigabitEthernet0/0
 description Administration LAN
 ip address 192.168.10.1 255.255.255.224
 duplex auto
!
interface GigabitEthernet0/1
 no ip address
 shutdown
!
end
"""

SETTINGS = DeviceSettings(
    hostname="Router-HQ",
    domain_lookup=False,
    interfaces={
        "GigabitEthernet0/0": {
            "address": "192.168.10.1/27",
            "description": "Administration LAN",
        }
    },
    dhcp={
        "excluded": ["192.168.10.1"],
        "pools": {
            "ADMIN": {
                "network": "192.168.10.0/27",
                "default_router": "192.168.10.1",
                "dns_server": "192.168.30.2",
            }
        },
    },
)


def test_config_text_parses_into_sections():
    config = parse_config(ROUTER_CONFIG)
    assert config.hostname == "Router-HQ"
    assert config.has_global("no ip domain-lookup")
    assert config.has_in("interface GigabitEthernet0/0", "ip address 192.168.10.1 255.255.255.224")
    assert config.has_in("interface GigabitEthernet0/1", "shutdown")
    assert not config.has_in("interface GigabitEthernet0/0", "shutdown")


def test_packet_tracer_comma_separated_config_parses_too():
    """PT returns the saved configuration with commas where newlines should be."""
    config = parse_config(ROUTER_CONFIG.replace("\n", ","))
    assert config.hostname == "Router-HQ"
    assert config.has_in("ip dhcp pool ADMIN", "network 192.168.10.0 255.255.255.224")


def test_a_matching_device_needs_no_changes():
    config = parse_config(ROUTER_CONFIG)
    assert all(block.is_converged(config) for block in render_blocks("Router-HQ", SETTINGS, "router"))


def test_a_changed_address_is_detected():
    config = parse_config(ROUTER_CONFIG.replace("192.168.10.1 255.255.255.224", "10.9.9.9 255.255.255.0"))
    pending = [b for b in render_blocks("Router-HQ", SETTINGS, "router") if not b.is_converged(config)]
    assert [b.label for b in pending] == ["interface GigabitEthernet0/0"]
    assert "missing `ip address 192.168.10.1 255.255.255.224`" in pending[0].missing(config)


def test_a_shut_interface_is_detected():
    """`no shutdown` never appears in a config; the absence of `shutdown` is the proof."""
    config = parse_config(
        ROUTER_CONFIG.replace(" duplex auto", " shutdown")
    )
    pending = [b for b in render_blocks("Router-HQ", SETTINGS, "router") if not b.is_converged(config)]
    assert pending and "still has `shutdown`" in pending[0].missing(config)


def test_a_missing_dhcp_pool_is_detected():
    config = parse_config(ROUTER_CONFIG.replace("ip dhcp pool ADMIN", "ip dhcp pool OTHER"))
    pending = [b.label for b in render_blocks("Router-HQ", SETTINGS, "router") if not b.is_converged(config)]
    assert "dhcp pool ADMIN" in pending


def test_a_device_with_no_readable_config_is_undecided_not_converged():
    blocks = render_blocks("Router-HQ", SETTINGS, "router")
    assert all(block.is_converged(None) is None for block in blocks)


def test_switchport_configuration_renders():
    settings = DeviceSettings(
        vlans={10: "ADMIN"},
        interfaces={"FastEthernet0/1": {"mode": "access", "vlan": 10}},
    )
    cli = build_cli(render_blocks("SW1", settings, "switch"))
    assert "vlan 10" in cli and " name ADMIN" in cli
    assert " switchport mode access" in cli and " switchport access vlan 10" in cli


def test_generated_script_enters_and_leaves_config_mode_and_saves():
    cli = build_cli(render_blocks("Router-HQ", SETTINGS, "router")).splitlines()
    assert cli[0] == "enable"
    assert cli[1] == "configure terminal"
    assert cli[-2] == "end"
    # The save is what makes the next run able to read this configuration back.
    assert cli[-1] == "write memory"


def test_hostname_defaults_to_the_device_name():
    cli = build_cli(render_blocks("SW-Admin", DeviceSettings(), "switch"))
    assert "hostname SW-Admin" in cli


# --- bare CLI: indentation must survive validation ------------------------------


def test_extra_cli_preserves_indentation_through_validation():
    """Regression: Strict.model_config strips whitespace from every string field,
    which used to erase the leading space that says a line is a submode child."""
    settings = DeviceSettings(extra_cli=["line vty 0 4", " login", " password secret"])
    assert settings.extra_cli == ["line vty 0 4", " login", " password secret"]


def test_extra_cli_accepts_a_pasted_block():
    """`extra_cli` as one YAML `|` block, exactly like a pasted `show run` excerpt."""
    settings = DeviceSettings(
        extra_cli="""
        line vty 0 4
         login
         password secret
        !
        service timestamps log datetime msec
        """.replace("        ", "")
    )
    assert settings.extra_cli == [
        "line vty 0 4",
        " login",
        " password secret",
        "service timestamps log datetime msec",
    ]


def test_a_submode_child_is_checked_against_its_section_not_the_top_level():
    """Regression: the old renderer checked every extra_cli line with `has_global`,
    so a line one level deep (like `login` under `line vty 0 4`) could never be
    found in a real device's configuration and was reported as pending forever."""
    settings = DeviceSettings(extra_cli=["line vty 0 4", " login", " password secret"])
    blocks = render_blocks("R1", settings, "router")
    block = next(b for b in blocks if b.label == "line vty 0 4")
    assert block.section == "line vty 0 4"

    config = parse_config("line vty 0 4\n login\n password secret\n!\n")
    assert block.is_converged(config) is True


def test_a_missing_submode_child_is_reported_precisely():
    settings = DeviceSettings(extra_cli=["line vty 0 4", " login", " password secret"])
    block = next(b for b in render_blocks("R1", settings, "router") if b.label == "line vty 0 4")
    config = parse_config("line vty 0 4\n login\n!\n")
    assert block.is_converged(config) is False
    assert block.missing(config) == ["missing `password secret`"]


def test_a_bare_global_line_and_a_submode_block_are_reported_separately():
    settings = DeviceSettings(
        extra_cli=["line vty 0 4", " login", "service timestamps log datetime msec"]
    )
    blocks = {b.label: b for b in render_blocks("R1", settings, "router")}
    assert set(["line vty 0 4", "service timestamps log datetime msec"]) <= set(blocks)
    # Only the vty block is a section; the bare line is checked at global scope.
    assert blocks["line vty 0 4"].section == "line vty 0 4"
    assert blocks["service timestamps log datetime msec"].section is None


def test_extra_cli_auto_exits_a_submode_it_entered():
    settings = DeviceSettings(extra_cli=["line vty 0 4", " login"])
    cli = build_cli(render_blocks("R1", settings, "router"))
    assert " exit" in cli.splitlines()


def test_extra_cli_does_not_double_exit_a_submode_that_already_does():
    settings = DeviceSettings(extra_cli=["line vty 0 4", " login", " exit"])
    cli = build_cli(render_blocks("R1", settings, "router")).splitlines()
    assert cli.count(" exit") == 1


def test_a_leading_indented_line_with_nothing_to_attach_to_is_kept_not_dropped():
    """A malformed spec (first extra_cli line indented) still produces a command
    rather than silently disappearing."""
    settings = DeviceSettings(extra_cli=[" login"])
    blocks = [b for b in render_blocks("R1", settings, "router") if b.label != "hostname R1"]
    assert [b.label for b in blocks] == ["login"]


def test_no_shutdown_inside_extra_cli_converges_like_the_structured_renderer_does():
    """Regression: IOS never writes `no shutdown` back into a saved
    configuration, so checking for it as literal required text meant an
    interface stanza written as bare CLI could never converge: every apply
    would re-push it forever, even though the interface really was up."""
    settings = DeviceSettings(
        extra_cli=[
            "interface GigabitEthernet0/0",
            " ip address 192.168.1.1 255.255.255.0",
            " no shutdown",
        ]
    )
    block = next(
        b for b in render_blocks("R1", settings, "router") if b.label == "interface GigabitEthernet0/0"
    )
    assert block.forbidden == ["shutdown"]
    assert "no shutdown" not in block.required

    up_config = parse_config(
        "interface GigabitEthernet0/0\n ip address 192.168.1.1 255.255.255.0\n!\n"
    )
    assert block.is_converged(up_config) is True

    shut_config = parse_config(
        "interface GigabitEthernet0/0\n ip address 192.168.1.1 255.255.255.0\n shutdown\n!\n"
    )
    assert block.is_converged(shut_config) is False
    assert block.missing(shut_config) == ["still has `shutdown`"]
