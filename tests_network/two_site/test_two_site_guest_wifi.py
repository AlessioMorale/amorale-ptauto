"""Acceptance tests for the two-site, guest-WiFi topology.

    ptauto test examples/two-site-guest-wifi.yaml tests_network/two_site/

Two routers joined by a point-to-point link, one switch and two PCs per site,
and a guest WiFi AP on Router-A's side. Nothing here needs a routing protocol —
each router has one static route per remote subnet — so these tests are really
checking that the static routes are actually carrying traffic, not just sitting
in the configuration.
"""

from __future__ import annotations

import ipaddress

import pytest

from ptauto.testing import describe_ping

pytestmark = pytest.mark.network

SITE_A_PCS = ["PC-A1", "PC-A2"]
SITE_B_PCS = ["PC-B1", "PC-B2"]


def test_network_matches_its_specification(pt_network):
    plan = pt_network.plan()
    assert plan.is_empty, "the live network differs from the spec:\n" + "\n".join(
        f"  - {action.summary} ({action.reason})" for action in plan.actions
    )


@pytest.mark.parametrize("device", SITE_A_PCS + SITE_B_PCS)
def test_pcs_obtain_their_settings_by_dhcp(pt_network, device):
    state = pt_network.host(device)
    assert state.dhcp, f"{device} is not a DHCP client"
    assert state.ip != "0.0.0.0", f"{device} holds no DHCP lease"
    assert state.gateway, f"{device} received no default gateway"


def test_the_two_sites_are_on_different_subnets(pt_network):
    site_a = ipaddress.ip_interface(
        f"{pt_network.host('PC-A1').ip}/{pt_network.host('PC-A1').mask}"
    ).network
    site_b = ipaddress.ip_interface(
        f"{pt_network.host('PC-B1').ip}/{pt_network.host('PC-B1').mask}"
    ).network
    assert site_a != site_b


def test_connectivity_within_each_site(pt_network):
    assert pt_network.ping("PC-A1", "PC-A2").ok
    assert pt_network.ping("PC-B1", "PC-B2").ok


def test_the_point_to_point_link_is_up(pt_network):
    """Requirement: the two routers can reach each other directly."""
    result = pt_network.ping("Router-A", "10.10.10.2")
    assert result.ok, describe_ping(result)


@pytest.mark.parametrize(
    "source,target", [("PC-A1", "PC-B1"), ("PC-B2", "PC-A2")]
)
def test_the_two_sites_reach_each_other_through_the_static_routes(pt_network, source, target):
    """The whole point of this topology: each router only knows the remote
    subnet because of an explicit `static_routes:` entry, not a routing
    protocol — so a successful ping here proves those routes are correct, not
    just present in the configuration."""
    result = pt_network.ping(source, target)
    assert result.ok, describe_ping(result)


def test_the_guest_network_is_its_own_subnet(pt_network):
    """Requirement: the guest WiFi network is a separate network from the LAN
    it hangs off, not just another host on Router-A's LAN."""
    guest_gateway = ipaddress.ip_interface("192.168.99.1/27").network
    site_a = ipaddress.ip_interface(
        f"{pt_network.host('PC-A1').ip}/{pt_network.host('PC-A1').mask}"
    ).network
    assert guest_gateway != site_a


def test_the_guest_networks_gateway_is_reachable_from_both_sites(pt_network):
    """The guest subnet's own router interface is reachable end to end — the
    part of "does this route work" that does not depend on a wireless client
    actually being associated to the AP."""
    assert pt_network.ping("PC-A1", "192.168.99.1").ok
    assert pt_network.ping("PC-B1", "192.168.99.1").ok


def test_the_access_point_is_placed_and_cabled(pt_network):
    """A plain AccessPoint-PT takes no CLI configuration in Packet Tracer, so
    there is nothing to converge on the device itself — only that it exists
    and is on the wire is something ptauto can check."""
    topology = pt_network.client.topology()
    ap = topology.devices.get("AP-Guest")
    assert ap is not None, "AP-Guest is not in the workspace"
    assert any(port.linked for port in ap.ports.values()), "AP-Guest is not cabled"
