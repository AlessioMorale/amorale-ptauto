"""ptauto — declarative, idempotent networks for Cisco Packet Tracer.

    from ptauto import load_spec, PTClient, Planner, Applier

    spec = load_spec("network.yaml")
    client = PTClient()
    plan = Planner(client, spec).build()
    Applier(client).run(plan)

The connectivity to Packet Tracer (the HTTP command bridge, its shared-token
authentication and the file mailbox fallback) is reused from the
`packet-tracer-mcp` project rather than reimplemented; see `ptauto.transport`.
"""

from __future__ import annotations

from .apply import Applier, ApplyReport, wait_for_dhcp
from .client import HostState, ObservedTopology, PingResult, PTClient
from .errors import BridgeError, PtAutoError, PTError, PTTimeout, SpecError
from .loader import load_spec, parse_spec, validate_spec
from .model import NetworkSpec
from .plan import Action, Plan, Planner
from .testing import Network
from .transport import BridgeTransport

__version__ = "0.1.0"

__all__ = [
    "Action",
    "Applier",
    "ApplyReport",
    "BridgeError",
    "BridgeTransport",
    "HostState",
    "Network",
    "NetworkSpec",
    "ObservedTopology",
    "PTClient",
    "PTError",
    "PTTimeout",
    "PingResult",
    "Plan",
    "Planner",
    "PtAutoError",
    "SpecError",
    "load_spec",
    "parse_spec",
    "validate_spec",
    "wait_for_dhcp",
    "__version__",
]
