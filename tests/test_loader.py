"""Parsing and cross-checking specifications."""

from __future__ import annotations

import pytest

from ptauto.errors import SpecError
from ptauto.loader import parse_spec, validate_spec

MINIMAL = {
    "components": {
        "R1": {"model": "2911"},
        "SW1": {"model": "2960-24TT"},
        "PC1": {"model": "PC-PT"},
    },
    "connections": [
        ["R1:g0/0", "SW1:g0/1"],
        {"from": "SW1:fa0/1", "to": "PC1:fa0"},
    ],
    "configurations": {
        "R1": {"interfaces": {"g0/0": {"address": "10.0.0.1/24"}}},
        "PC1": {"dhcp_client": True},
    },
}


def spec(**overrides):
    data = {k: (overrides[k] if k in overrides else v) for k, v in MINIMAL.items()}
    data.update({k: v for k, v in overrides.items() if k not in MINIMAL})
    return data


def test_port_names_are_canonicalised():
    parsed = parse_spec(spec())
    assert parsed.connections[0].port_a == "GigabitEthernet0/0"
    assert parsed.connections[1].port_b == "FastEthernet0"
    assert "GigabitEthernet0/0" in parsed.configurations["R1"].interfaces


def test_every_connection_spelling_parses_the_same():
    forms = [
        [["R1:g0/0", "SW1:g0/1"]],
        ["R1:g0/0 <-> SW1:g0/1"],
        [{"from": "R1:g0/0", "to": "SW1:g0/1"}],
        [{"endpoints": ["R1:g0/0", "SW1:g0/1"]}],
    ]
    keys = {parse_spec(spec(connections=form)).connections[0].key for form in forms}
    assert len(keys) == 1


def test_inline_config_is_merged_into_configurations():
    parsed = parse_spec(
        {
            "components": {"PC1": {"model": "PC-PT", "config": {"dhcp_client": True}}},
        }
    )
    assert parsed.configurations["PC1"].dhcp_client is True


def test_standalone_configuration_wins_over_inline():
    parsed = parse_spec(
        {
            "components": {
                "PC1": {"model": "PC-PT", "config": {"address": "10.0.0.5/24", "gateway": "10.0.0.1"}}
            },
            "configurations": {"PC1": {"address": "10.0.0.9/24"}},
        }
    )
    assert parsed.configurations["PC1"].address == "10.0.0.9/24"
    # merged, not replaced
    assert parsed.configurations["PC1"].gateway == "10.0.0.1"


def test_unknown_key_is_rejected():
    with pytest.raises(SpecError, match="extra_input|Extra inputs"):
        parse_spec({"components": {"PC1": {"model": "PC-PT", "colour": "blue"}}})


def test_unknown_model_is_reported_with_suggestions():
    parsed = parse_spec({"components": {"R1": {"model": "2912"}}})
    with pytest.raises(SpecError) as exc:
        validate_spec(parsed)
    assert "2911" in str(exc.value)


def test_port_that_the_model_does_not_have_is_rejected():
    parsed = parse_spec(
        spec(connections=[["R1:GigabitEthernet0/9", "SW1:GigabitEthernet0/1"]])
    )
    with pytest.raises(SpecError, match="no port"):
        validate_spec(parsed)


def test_a_port_cannot_take_two_cables():
    parsed = parse_spec(
        spec(connections=[["R1:g0/0", "SW1:g0/1"], ["R1:g0/0", "SW1:g0/2"]])
    )
    with pytest.raises(SpecError, match="cabled twice"):
        validate_spec(parsed)


def test_connection_to_an_unknown_device_is_rejected():
    parsed = parse_spec(spec(connections=[["R1:g0/0", "Ghost:g0/1"]]))
    with pytest.raises(SpecError, match="no component named 'Ghost'"):
        validate_spec(parsed)


def test_host_settings_on_a_router_are_rejected():
    parsed = parse_spec(spec(configurations={"R1": {"dhcp_client": True}}))
    with pytest.raises(SpecError, match="host-only"):
        validate_spec(parsed)


def test_ios_settings_on_a_pc_are_rejected():
    parsed = parse_spec(spec(configurations={"PC1": {"hostname": "PC1"}}))
    with pytest.raises(SpecError, match="IOS-only"):
        validate_spec(parsed)


def test_dhcp_and_a_static_address_cannot_both_be_set():
    parsed = parse_spec(
        spec(configurations={"PC1": {"dhcp_client": True, "address": "10.0.0.5/24"}})
    )
    with pytest.raises(SpecError, match="contradict"):
        validate_spec(parsed)


def test_duplicate_addresses_are_caught():
    parsed = parse_spec(
        spec(
            configurations={
                "R1": {"interfaces": {"g0/0": {"address": "10.0.0.1/24"}}},
                "PC1": {"address": "10.0.0.1/24", "gateway": "10.0.0.1"},
            }
        )
    )
    with pytest.raises(SpecError, match="duplicate address"):
        validate_spec(parsed)


def test_network_address_cannot_be_assigned_to_an_interface():
    parsed = parse_spec(
        spec(configurations={"R1": {"interfaces": {"g0/0": {"address": "10.0.0.0/24"}}}})
    )
    with pytest.raises(SpecError, match="network address"):
        validate_spec(parsed)


def test_gateway_outside_the_hosts_subnet_is_caught():
    parsed = parse_spec(
        spec(
            configurations={
                "R1": {"interfaces": {"g0/0": {"address": "10.0.0.1/24"}}},
                "PC1": {"address": "10.0.0.5/24", "gateway": "192.168.1.1"},
            }
        )
    )
    with pytest.raises(SpecError, match="outside the host's own subnet"):
        validate_spec(parsed)


def test_dhcp_pool_pointing_at_a_router_nobody_owns_is_caught():
    parsed = parse_spec(
        spec(
            configurations={
                "R1": {
                    "interfaces": {"g0/0": {"address": "10.0.0.1/24"}},
                    "dhcp": {"pools": {"LAN": {"network": "10.0.0.0/24", "default_router": "10.0.0.254"}}},
                },
                "PC1": {"dhcp_client": True},
            }
        )
    )
    with pytest.raises(SpecError, match="which no device in this spec owns"):
        validate_spec(parsed)


def test_a_valid_specification_passes():
    validate_spec(parse_spec(spec()))
