"""The facade a pytest suite talks to.

A test should read like the thing it is checking — `net.ping("PC-A1",
"PC-B1")`, not four bridge calls and a regex. `Network` resolves device
names to the addresses they actually hold in Packet Tracer (which is the only
sensible way to test a DHCP client), and exposes the planner so a suite can
assert that the live network still matches its specification.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .client import HostState, PTClient, PingResult
from .errors import PtAutoError
from .model import NetworkSpec, split_address
from .plan import Plan, Planner


class NetworkError(PtAutoError):
    """A test asked about something the network does not have."""


@dataclass
class Network:
    """Everything a test needs: the spec, the live client, and the bridge."""

    spec: NetworkSpec
    client: PTClient

    # -- addressing ---------------------------------------------------------

    def address_of(self, device: str) -> str:
        """The address `device` actually holds right now.

        Live state first, spec second: a DHCP client has no address in the spec,
        and a device whose address was changed by hand should make a test fail,
        not quietly pass against the spec's value.
        """
        if device in self.spec.components:
            live = self._live_address(device)
            if live:
                return live
            settings = self.spec.settings_for(device)
            if settings.address:
                return split_address(settings.address)[0]
            for iface in settings.interfaces.values():
                if iface.address:
                    return split_address(iface.address)[0]
            raise NetworkError(
                f"{device} has no address: it is not configured in the spec and "
                f"holds nothing in Packet Tracer."
            )
        return device  # already an address or a hostname

    def _live_address(self, device: str) -> str | None:
        topology = self.client.topology()
        observed = topology.devices.get(device)
        if observed is None:
            return None
        for port in observed.ports.values():
            if port.has_address:
                return port.ip
        return None

    def host(self, device: str) -> HostState:
        state = self.client.read_host(device)
        if not state.found:
            raise NetworkError(f"{device} is not in the Packet Tracer workspace")
        return state

    def services(self, device: str) -> dict:
        return self.client.read_services(device)

    def interface(self, device: str, port: str):
        """The live state of one interface."""
        from .catalog import normalise_port

        topology = self.client.topology()
        observed = topology.devices.get(device)
        if observed is None:
            raise NetworkError(f"{device} is not in the Packet Tracer workspace")
        name = normalise_port(port)
        if name not in observed.ports:
            raise NetworkError(
                f"{device} has no interface {name}. It has: {', '.join(observed.ports)}"
            )
        return observed.ports[name]

    # -- reachability ---------------------------------------------------------

    def ping(
        self, source: str, target: str, timeout: float = 25.0, retry_partial: bool = True
    ) -> PingResult:
        """Ping from one device to another device, address or hostname.

        A first ping across a cold ARP cache loses its first packet — every
        Packet Tracer session shows `Sent = 4, Received = 3` the first time two
        hosts talk. That is a warm-up artefact, not a fault, so a partial result
        is repeated once and the second attempt is the answer. Pass
        `retry_partial=False` to see the raw first result.
        """
        result = self.client.ping(source, self.address_of(target), timeout=timeout)
        if retry_partial and result.verdict == "partial":
            result = self.client.ping(source, self.address_of(target), timeout=timeout)
        return result

    def ping_hostname(
        self, source: str, hostname: str, timeout: float = 30.0, retry_partial: bool = True
    ) -> PingResult:
        """Ping a *name*, so the result also proves DNS resolution works."""
        result = self.client.ping(source, hostname, timeout=timeout)
        if retry_partial and result.verdict == "partial":
            result = self.client.ping(source, hostname, timeout=timeout)
        return result

    def reachable(self, source: str, target: str, timeout: float = 25.0) -> bool:
        return self.ping(source, target, timeout=timeout).ok

    # -- convergence -----------------------------------------------------------

    def plan(self, prune: bool = False) -> Plan:
        """What would `ptauto apply` still have to change?"""
        return Planner(self.client, self.spec, prune=prune).build()

    def is_converged(self) -> bool:
        return self.plan().is_empty

    # -- helpers used by assertions --------------------------------------------

    def same_subnet(self, device_a: str, device_b: str) -> bool:
        state_a = self.host(device_a)
        state_b = self.host(device_b)
        net_a = ipaddress.ip_interface(f"{state_a.ip}/{state_a.mask}").network
        net_b = ipaddress.ip_interface(f"{state_b.ip}/{state_b.mask}").network
        return net_a == net_b

    def dhcp_clients(self) -> list[str]:
        return [
            name
            for name in self.spec.components
            if self.spec.settings_for(name).dhcp_client
        ]

    def devices(self) -> list[str]:
        return list(self.spec.components)


def describe_ping(result: PingResult) -> str:
    """A failure message a human can act on."""
    return (
        f"{result.source} -> {result.target}: {result.verdict.upper()}"
        + (f"\n  {result.detail}" if result.detail else "")
    )
