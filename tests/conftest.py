"""A Packet Tracer stand-in, so the library can be tested without the simulator.

The fake answers the same four questions the planner asks: what is in the
workspace, what a router's configuration says, what a host's IP settings are and
what a server is running, and records what was done to it. That is enough to
test every decision ptauto makes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ptauto.client import HostState, ObservedDevice, ObservedLink, ObservedPort, ObservedTopology, PingResult


@dataclass
class FakePT:
    devices: dict[str, ObservedDevice] = field(default_factory=dict)
    links: list[ObservedLink] = field(default_factory=list)
    configs: dict[str, str] = field(default_factory=dict)
    hosts: dict[str, HostState] = field(default_factory=dict)
    services: dict[str, dict] = field(default_factory=dict)
    vlans: dict[str, dict[int, str]] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)

    # -- reads ---------------------------------------------------------

    def topology(self) -> ObservedTopology:
        return ObservedTopology(devices=dict(self.devices), links=list(self.links))

    def read_ios_config(self, device: str, refresh: bool = True) -> str | None:
        self.calls.append(("read_ios_config", device))
        return self.configs.get(device)

    def read_host(self, device: str) -> HostState:
        self.calls.append(("read_host", device))
        return self.hosts.get(device, HostState(found=False))

    def read_services(self, device: str) -> dict:
        self.calls.append(("read_services", device))
        return self.services.get(device, {"found": True, "dns": None, "http": None})

    def read_vlans(self, switch: str) -> dict[int, str] | None:
        return self.vlans.get(switch)

    # -- writes ---------------------------------------------------------

    def add_device(self, name, info, x, y):
        self.calls.append(("add_device", name))
        self.devices[name] = ObservedDevice(name=name, model=info.pt_type, x=x, y=y)
        for port in info.ports:
            self.devices[name].ports[port] = ObservedPort(name=port)

    def delete_device(self, name):
        self.calls.append(("delete_device", name))
        self.devices.pop(name, None)

    def move_device(self, name, x, y):
        self.calls.append(("move_device", name))

    def add_link(self, device_a, port_a, device_b, port_b, cable):
        self.calls.append(("add_link", device_a, port_a, device_b, port_b, cable))
        self.links.append(ObservedLink(device_a, port_a, device_b, port_b))

    def delete_link(self, device, port):
        self.calls.append(("delete_link", device, port))

    def configure_ios(self, device, cli, timeout=60.0):
        self.calls.append(("configure_ios", device, cli))

    def configure_host(self, device, dhcp, ip="", mask="", gateway="", dns=""):
        self.calls.append(("configure_host", device, dhcp, ip))

    def configure_dns(self, device, enabled, records):
        self.calls.append(("configure_dns", device, enabled, tuple(records)))

    def configure_http(self, device, enabled, pages=None, port=None):
        self.calls.append(("configure_http", device, enabled))

    def ping(self, device, target, timeout=25.0):
        return PingResult(device, target, "ok", "Packets: Sent = 4, Received = 4, Lost = 0")

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


def device(name: str, model: str, ports: tuple[str, ...] = (), **kwargs) -> ObservedDevice:
    observed = ObservedDevice(name=name, model=model, **kwargs)
    for port in ports:
        observed.ports[port] = ObservedPort(name=port)
    return observed


@pytest.fixture
def fake_pt() -> FakePT:
    return FakePT()
