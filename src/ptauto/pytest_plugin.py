"""pytest fixtures for testing a Packet Tracer network.

Installing ptauto is enough — the plugin registers itself through the
`pytest11` entry point, so a suite needs no conftest.py.

    $ pytest --pt-spec examples/two-site-guest-wifi.yaml

    def test_the_two_sites_can_talk(pt_network):
        assert pt_network.ping("PC-A1", "PC-B1").ok

Tests that need PT are skipped, not failed, when Packet Tracer is not running:
a suite is a description of the network, and it should stay readable when the
simulator happens to be closed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .apply import Applier, wait_for_dhcp
from .client import PTClient
from .errors import PtAutoError
from .loader import load_spec
from .plan import Planner
from .testing import Network, describe_ping
from .transport import BridgeTransport

DEFAULT_SPEC_NAMES = ("network.yaml", "network.yml", "ptauto.yaml")


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ptauto", "Packet Tracer network testing")
    group.addoption(
        "--pt-spec",
        action="store",
        default=None,
        metavar="PATH",
        help="network specification to test against (default: ./network.yaml)",
    )
    group.addoption(
        "--pt-apply",
        action="store_true",
        default=False,
        help="apply the specification to Packet Tracer before running the suite",
    )
    group.addoption(
        "--pt-require",
        action="store_true",
        default=False,
        help="fail instead of skipping when Packet Tracer is not reachable",
    )
    parser.addini("pt_spec", "default network specification for ptauto", default="")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "network: test that talks to a live Packet Tracer instance"
    )


@pytest.fixture(scope="session")
def pt_spec_path(pytestconfig: pytest.Config) -> Path:
    """Where the specification lives: --pt-spec, then PTAUTO_SPEC, then ini, then
    a conventional filename next to the tests."""
    explicit = pytestconfig.getoption("--pt-spec") or os.environ.get("PTAUTO_SPEC")
    if explicit:
        return Path(explicit)
    from_ini = pytestconfig.getini("pt_spec")
    if from_ini:
        return Path(str(pytestconfig.rootpath / from_ini))
    for name in DEFAULT_SPEC_NAMES:
        candidate = pytestconfig.rootpath / name
        if candidate.exists():
            return candidate
    pytest.skip(
        "no network specification found — pass --pt-spec PATH or add network.yaml "
        "to the project root"
    )


@pytest.fixture(scope="session")
def pt_spec(pt_spec_path: Path):
    """The parsed, validated specification."""
    try:
        return load_spec(pt_spec_path)
    except PtAutoError as exc:
        pytest.fail(f"{pt_spec_path}: {exc}", pytrace=False)


@pytest.fixture(scope="session")
def pt_transport() -> BridgeTransport:
    return BridgeTransport()


@pytest.fixture(scope="session")
def pt_client(pt_transport: BridgeTransport, pytestconfig: pytest.Config) -> PTClient:
    """A client, or a skipped suite when PT is not there."""
    client = PTClient(pt_transport)
    if not client.is_connected():
        message = (
            "Packet Tracer is not reachable — open it with the MCP Control Center "
            "extension (Extensions > MCP BUILDER)"
        )
        if pytestconfig.getoption("--pt-require"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    return client


@pytest.fixture(scope="session")
def pt_network(pt_client: PTClient, pt_spec, pytestconfig: pytest.Config) -> Network:
    """The network under test, applied first if --pt-apply was given."""
    network = Network(spec=pt_spec, client=pt_client)
    if pytestconfig.getoption("--pt-apply"):
        plan = Planner(pt_client, pt_spec).build()
        report = Applier(pt_client).run(plan)
        if not report.ok:
            failures = "\n".join(
                f"  {r.action.summary}: {r.detail}" for r in report.failures
            )
            pytest.fail(f"--pt-apply could not build the network:\n{failures}", pytrace=False)
        clients = network.dhcp_clients()
        if clients:
            wait_for_dhcp(pt_client, clients)
    return network


@pytest.fixture
def pt_ping(pt_network: Network):
    """`pt_ping("PC-A", "PC-B")` — asserts, with a readable failure."""

    def _ping(source: str, target: str, timeout: float = 25.0):
        result = pt_network.ping(source, target, timeout=timeout)
        assert result.ok, describe_ping(result)
        return result

    return _ping
