"""Executing a plan against Packet Tracer.

Ordering is the whole job here. Devices have to exist before they can be cabled,
a router's DHCP pool has to exist before a PC asks for a lease, and a server
needs its own address before its services are worth enabling. The planner emits
actions in the order it discovers them; this module sorts them into the order
the network needs, which is what lets a spec be applied to an empty workspace in
one pass.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .client import PTClient
from .errors import PTError, PTTimeout
from .plan import Action, Plan

# Lower runs first. Two things are worth pointing out: strays are removed before
# anything is built (they may be holding a port the spec wants), and hosts that
# use DHCP are configured after every router, so the pool answering them already
# exists.
_PRIORITY = {
    "delete_device": 5,
    "create_device": 10,
    "replace_device": 10,
    "move_device": 15,
    "delete_link": 20,
    "create_link": 30,
    "configure_ios": 40,
    "configure_host_static": 50,
    "configure_dns": 55,
    "configure_http": 55,
    "configure_host_dhcp": 60,
}


def _priority(action: Action) -> int:
    kind = action.kind
    if kind == "configure_host":
        kind = "configure_host_dhcp" if action.payload.get("dhcp") else "configure_host_static"
    return _PRIORITY.get(kind, 90)


def ordered_actions(plan: Plan) -> list[Action]:
    """The plan's actions in execution order (stable within a priority)."""
    return sorted(plan.actions, key=_priority)


@dataclass
class ActionResult:
    action: Action
    ok: bool
    detail: str = ""


@dataclass
class ApplyReport:
    results: list[ActionResult] = field(default_factory=list)
    dhcp_leases: dict[str, str] = field(default_factory=dict)
    saved_to: str | None = None

    @property
    def applied(self) -> list[ActionResult]:
        return [r for r in self.results if r.ok]

    @property
    def failures(self) -> list[ActionResult]:
        return [r for r in self.results if not r.ok]

    @property
    def ok(self) -> bool:
        return not self.failures


class Applier:
    """Runs a plan. One action at a time, stopping on the first failure unless
    told otherwise: a half-applied topology is easier to reason about when the
    failure is where it happened."""

    def __init__(
        self,
        client: PTClient,
        on_action: Callable[[Action], None] | None = None,
        keep_going: bool = False,
    ) -> None:
        self.client = client
        self.on_action = on_action or (lambda action: None)
        self.keep_going = keep_going

    def run(self, plan: Plan) -> ApplyReport:
        report = ApplyReport()
        for action in ordered_actions(plan):
            self.on_action(action)
            try:
                detail = self._execute(action)
                report.results.append(ActionResult(action, True, detail))
            except (PTError, PTTimeout) as exc:
                report.results.append(ActionResult(action, False, str(exc)))
                if not self.keep_going:
                    break
        return report

    def _execute(self, action: Action) -> str:
        handler = getattr(self, f"_do_{action.kind}", None)
        if handler is None:
            raise PTError(f"ptauto does not know how to perform {action.kind!r}")
        return handler(action) or ""

    # -- handlers ---------------------------------------------------------

    def _do_create_device(self, action: Action) -> str:
        payload = action.payload
        self.client.add_device(action.target, payload["info"], payload["x"], payload["y"])
        return f"{payload['info'].pt_type} created"

    def _do_replace_device(self, action: Action) -> str:
        self.client.delete_device(action.target)
        return self._do_create_device(action)

    def _do_delete_device(self, action: Action) -> str:
        self.client.delete_device(action.target)
        return "deleted"

    def _do_move_device(self, action: Action) -> str:
        self.client.move_device(action.target, action.payload["x"], action.payload["y"])
        return "moved"

    def _do_create_link(self, action: Action) -> str:
        payload = action.payload
        self.client.add_link(
            payload["device_a"],
            payload["port_a"],
            payload["device_b"],
            payload["port_b"],
            payload["cable"],
        )
        return f"{payload['cable']} cable"

    def _do_delete_link(self, action: Action) -> str:
        self.client.delete_link(action.payload["device"], action.payload["port"])
        return "cable removed"

    def _do_configure_ios(self, action: Action) -> str:
        self.client.configure_ios(action.target, action.payload["cli"])
        return f"{len(action.payload.get('blocks', []))} block(s) applied and saved"

    def _do_configure_host(self, action: Action) -> str:
        payload = action.payload
        self.client.configure_host(
            action.target,
            dhcp=payload["dhcp"],
            ip=payload["ip"],
            mask=payload["mask"],
            gateway=payload["gateway"],
            dns=payload["dns"],
        )
        return "DHCP client enabled" if payload["dhcp"] else f"{payload['ip']} configured"

    def _do_configure_dns(self, action: Action) -> str:
        self.client.configure_dns(
            action.target, action.payload["enabled"], action.payload["records"]
        )
        return f"{len(action.payload['records'])} record(s)"

    def _do_configure_http(self, action: Action) -> str:
        self.client.configure_http(
            action.target,
            action.payload["enabled"],
            action.payload.get("pages"),
            action.payload.get("port"),
        )
        return "web service configured"


def wait_for_dhcp(
    client: PTClient,
    devices: list[str],
    timeout: float = 45.0,
    on_lease: Callable[[str, str], None] | None = None,
) -> dict[str, str]:
    """Wait until every named host holds a lease; returns device -> address.

    A PC that has just been switched to DHCP does not get an address the instant
    the flag is set, and the next thing a run does is usually a connectivity
    test. Waiting here turns a race into a wait.
    """
    pending = list(devices)
    leases: dict[str, str] = {}
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for device in list(pending):
            try:
                state = client.read_host(device)
            except (PTError, PTTimeout):
                continue
            if state.found and state.ip and state.ip != "0.0.0.0":
                leases[device] = f"{state.ip}/{state.mask}"
                pending.remove(device)
                if on_lease:
                    on_lease(device, leases[device])
        if pending:
            time.sleep(1.5)
    for device in pending:
        leases[device] = ""
    return leases
