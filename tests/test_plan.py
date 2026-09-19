"""The diff engine: what changes, what does not, and in which order."""

from __future__ import annotations

import pytest
from conftest import device

from ptauto.apply import Applier, ordered_actions
from ptauto.client import HostState, ObservedLink
from ptauto.loader import parse_spec
from ptauto.plan import Planner

SPEC = {
    "components": {
        "R1": {"model": "2911", "position": [400, 100]},
        "SW1": {"model": "2960-24TT", "position": [400, 250]},
        "PC1": {"model": "PC-PT", "position": [300, 400]},
        "SRV": {"model": "Server-PT", "position": [500, 400]},
    },
    "connections": [
        ["R1:g0/0", "SW1:g0/1"],
        ["SW1:fa0/1", "PC1:fa0"],
        ["SW1:fa0/2", "SRV:fa0"],
    ],
    "configurations": {
        "R1": {
            "hostname": "R1",
            "interfaces": {"g0/0": {"address": "10.0.0.1/24"}},
            "dhcp": {
                "excluded": ["10.0.0.1"],
                "pools": {"LAN": {"network": "10.0.0.0/24", "default_router": "10.0.0.1"}},
            },
        },
        "SW1": {"hostname": "SW1"},
        "PC1": {"dhcp_client": True},
        "SRV": {
            "address": "10.0.0.10/24",
            "gateway": "10.0.0.1",
            "services": {"http": {"enabled": True}, "dns": {"records": [{"name": "www.local", "address": "10.0.0.10"}]}},
        },
    },
}

CONVERGED_R1 = """
hostname R1
ip dhcp excluded-address 10.0.0.1
!
ip dhcp pool LAN
 network 10.0.0.0 255.255.255.0
 default-router 10.0.0.1
!
interface GigabitEthernet0/0
 ip address 10.0.0.1 255.255.255.0
!
"""


@pytest.fixture
def spec():
    return parse_spec(SPEC)


def build(fake_pt, spec, **kwargs):
    return Planner(fake_pt, spec, **kwargs).build()


def converge(fake_pt):
    """Put the fake into the state the spec describes."""
    fake_pt.devices = {
        "R1": device("R1", "2911", ("GigabitEthernet0/0",), x=400, y=100),
        "SW1": device("SW1", "2960-24TT", ("GigabitEthernet0/1", "FastEthernet0/1", "FastEthernet0/2"), x=400, y=250),
        "PC1": device("PC1", "PC-PT", ("FastEthernet0",), x=300, y=400),
        "SRV": device("SRV", "Server-PT", ("FastEthernet0",), x=500, y=400),
    }
    for name, port in (("R1", "GigabitEthernet0/0"), ("SW1", "GigabitEthernet0/1"),
                       ("SW1", "FastEthernet0/1"), ("SW1", "FastEthernet0/2"),
                       ("PC1", "FastEthernet0"), ("SRV", "FastEthernet0")):
        fake_pt.devices[name].ports[port].linked = True
    fake_pt.links = [
        ObservedLink("R1", "GigabitEthernet0/0", "SW1", "GigabitEthernet0/1"),
        ObservedLink("SW1", "FastEthernet0/1", "PC1", "FastEthernet0"),
        ObservedLink("SW1", "FastEthernet0/2", "SRV", "FastEthernet0"),
    ]
    fake_pt.configs = {"R1": CONVERGED_R1, "SW1": "hostname SW1\n"}
    fake_pt.hosts = {
        "PC1": HostState(found=True, dhcp=True, ip="10.0.0.5", mask="255.255.255.0", gateway="10.0.0.1"),
        "SRV": HostState(found=True, dhcp=False, ip="10.0.0.10", mask="255.255.255.0", gateway="10.0.0.1"),
    }
    fake_pt.services = {
        "SRV": {
            "found": True,
            "dns": {"enabled": True, "records": [{"name": "www.local", "address": "10.0.0.10"}]},
            "http": {"enabled": True, "port": 80, "index": ""},
        }
    }
    return fake_pt


# --- building from nothing ----------------------------------------------------


def test_an_empty_workspace_plans_the_whole_network(fake_pt, spec):
    plan = build(fake_pt, spec)
    kinds = [action.kind for action in plan.actions]
    assert kinds.count("create_device") == 4
    assert kinds.count("create_link") == 3
    assert "configure_ios" in kinds and "configure_host" in kinds
    assert plan.unchanged == []


def test_nothing_is_read_from_devices_that_do_not_exist_yet(fake_pt, spec):
    build(fake_pt, spec)
    assert "read_ios_config" not in fake_pt.kinds()


def test_cable_type_is_inferred_from_the_device_categories(fake_pt, spec):
    plan = build(fake_pt, spec)
    cables = {action.payload["cable"] for action in plan.by_kind("create_link")}
    assert cables == {"straight"}


# --- the idempotency claim -------------------------------------------------------


def test_a_converged_network_plans_nothing(fake_pt, spec):
    plan = build(converge(fake_pt), spec)
    assert plan.is_empty, [a.summary for a in plan.actions]
    assert len(plan.unchanged) > 8


def test_applying_a_converged_network_sends_nothing(fake_pt, spec):
    plan = build(converge(fake_pt), spec)
    fake_pt.calls.clear()
    Applier(fake_pt).run(plan)
    assert fake_pt.calls == []


# --- drift ------------------------------------------------------------------------


def test_a_missing_device_is_the_only_thing_planned(fake_pt, spec):
    converge(fake_pt)
    del fake_pt.devices["PC1"]
    fake_pt.links = [l for l in fake_pt.links if "PC1" not in (l.device_a, l.device_b)]
    fake_pt.devices["SW1"].ports["FastEthernet0/1"].linked = False
    plan = build(fake_pt, spec)
    assert [a.kind for a in plan.actions] == ["create_device", "create_link", "configure_host"]


def test_a_changed_router_address_is_planned_with_its_reason(fake_pt, spec):
    converge(fake_pt)
    fake_pt.configs["R1"] = CONVERGED_R1.replace("10.0.0.1 255.255.255.0", "10.9.9.9 255.255.255.0")
    plan = build(fake_pt, spec)
    assert [a.kind for a in plan.actions] == ["configure_ios"]
    assert "ip address 10.0.0.1 255.255.255.0" in plan.actions[0].reason


def test_a_host_switched_to_a_static_address_is_planned_back_to_dhcp(fake_pt, spec):
    converge(fake_pt)
    fake_pt.hosts["PC1"] = HostState(found=True, dhcp=False, ip="10.0.0.99", mask="255.255.255.0")
    plan = build(fake_pt, spec)
    assert [a.kind for a in plan.actions] == ["configure_host"]
    assert "DHCP is off" in plan.actions[0].reason


def test_a_dhcp_client_without_a_lease_is_reconfigured(fake_pt, spec):
    converge(fake_pt)
    fake_pt.hosts["PC1"] = HostState(found=True, dhcp=True, ip="0.0.0.0", mask="0.0.0.0")
    plan = build(fake_pt, spec)
    assert "no lease" in plan.actions[0].reason


def test_a_deleted_dns_record_is_planned_back(fake_pt, spec):
    converge(fake_pt)
    fake_pt.services["SRV"]["dns"]["records"] = []
    plan = build(fake_pt, spec)
    assert [a.kind for a in plan.actions] == ["configure_dns"]
    assert "missing record www.local" in plan.actions[0].reason


def test_an_extra_dns_record_is_planned_away(fake_pt, spec):
    converge(fake_pt)
    fake_pt.services["SRV"]["dns"]["records"].append({"name": "rogue.local", "address": "10.0.0.66"})
    plan = build(fake_pt, spec)
    assert "extra record rogue.local" in plan.actions[0].reason


def test_a_moved_device_is_planned_back_but_small_offsets_are_ignored(fake_pt, spec):
    converge(fake_pt)
    fake_pt.devices["R1"].x = 404  # within tolerance: PT rounds coordinates
    assert build(fake_pt, spec).is_empty
    fake_pt.devices["R1"].x = 700
    assert [a.kind for a in build(fake_pt, spec).actions] == ["move_device"]


def test_an_unreadable_configuration_is_reapplied_but_flagged(fake_pt, spec):
    converge(fake_pt)
    fake_pt.configs.pop("R1")
    plan = build(fake_pt, spec)
    action = plan.by_kind("configure_ios")[0]
    assert action.verified is False
    assert "could not be read" in action.reason


# --- cabling ---------------------------------------------------------------------


def test_a_port_cabled_somewhere_else_is_freed_first(fake_pt, spec):
    converge(fake_pt)
    fake_pt.links[1] = ObservedLink("SW1", "FastEthernet0/1", "SRV", "FastEthernet0")
    plan = build(fake_pt, spec)
    kinds = [a.kind for a in ordered_actions(plan)]
    assert kinds.index("delete_link") < kinds.index("create_link")


def test_strays_are_left_alone_unless_pruning(fake_pt, spec):
    converge(fake_pt)
    fake_pt.devices["Rogue"] = device("Rogue", "PC-PT", ("FastEthernet0",))
    assert build(fake_pt, spec).is_empty
    pruned = build(fake_pt, spec, prune=True)
    assert [a.kind for a in pruned.actions] == ["delete_device"]
    assert pruned.actions[0].destructive


def test_packet_tracers_own_infrastructure_is_never_pruned(fake_pt, spec):
    converge(fake_pt)
    fake_pt.devices["Power Distribution Device0"] = device(
        "Power Distribution Device0", "Power Distribution Device"
    )
    assert build(fake_pt, spec, prune=True).is_empty


def test_a_wrong_model_warns_but_does_not_replace_by_default(fake_pt, spec):
    converge(fake_pt)
    fake_pt.devices["R1"].model = "1941"
    plan = build(fake_pt, spec)
    assert plan.warnings and "--prune" in plan.warnings[0]
    assert not plan.by_kind("replace_device")
    assert build(fake_pt, spec, prune=True).by_kind("replace_device")


def test_a_replaced_devices_cable_is_replanned_not_marked_unchanged(fake_pt, spec):
    # R1's cable to SW1 is destroyed along with R1 when it is replaced; a
    # plan that calls it "unchanged" would leave R1 uncabled after apply.
    converge(fake_pt)
    fake_pt.devices["R1"].model = "1941"
    plan = build(fake_pt, spec, prune=True)
    links = [a for a in plan.by_kind("create_link") if "R1" in a.target]
    assert links, [a.summary for a in plan.actions]
    assert not any("R1:GigabitEthernet0/0" in u for u in plan.unchanged)


# --- ordering --------------------------------------------------------------------


def test_actions_run_in_an_order_the_network_can_survive(fake_pt, spec):
    plan = build(fake_pt, spec)
    kinds = [a.kind for a in ordered_actions(plan)]
    assert kinds.index("create_device") < kinds.index("create_link")
    assert kinds.index("create_link") < kinds.index("configure_ios")
    # A DHCP client is configured only once the router that answers it is up.
    dhcp_host = next(
        i for i, a in enumerate(ordered_actions(plan))
        if a.kind == "configure_host" and a.payload["dhcp"]
    )
    assert kinds.index("configure_ios") < dhcp_host


def test_a_server_is_addressed_before_its_services_are_enabled(fake_pt, spec):
    plan = build(fake_pt, spec)
    actions = ordered_actions(plan)
    address = next(i for i, a in enumerate(actions) if a.kind == "configure_host" and a.target == "SRV")
    services = next(i for i, a in enumerate(actions) if a.kind == "configure_dns")
    assert address < services


def test_apply_reports_each_action_and_stops_at_the_first_failure(fake_pt, spec):
    plan = build(fake_pt, spec)

    def explode(device_name, cli, timeout=60.0):
        raise __import__("ptauto").PTError("boom")

    fake_pt.configure_ios = explode
    report = Applier(fake_pt).run(plan)
    assert not report.ok
    assert report.failures[0].detail == "boom"
    # Nothing after the failing action ran.
    assert not any(r.action.kind == "configure_host" for r in report.results)


def test_an_ios_device_with_no_configuration_at_all_is_left_alone(fake_pt, spec):
    """Regression: render_blocks always pushes a default hostname, which used
    to generate a configure_ios action for every IOS-category device even when
    the spec asked for nothing, fatal for a model (a plain AP) that Packet
    Tracer will not accept configureIosDevice on at all."""
    converge(fake_pt)
    fake_pt.devices["AP1"] = device("AP1", "AccessPoint-PT", ("Port 0",), x=400, y=250)
    spec.components["AP1"] = spec.components["SW1"].model_copy(update={"model": "AccessPoint-PT", "position": [400, 250]})
    plan = build(fake_pt, spec)
    assert not any(a.kind in ("configure_ios", "create_device") and a.target == "AP1" for a in plan.actions)
    assert ("read_ios_config", "AP1") not in fake_pt.calls
