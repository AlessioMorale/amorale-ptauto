"""The bridge client, exercised against a real PTCommandBridge.

The bridge is the upstream MCP project's; what is tested here is ptauto's side
of the conversation: that it signs its requests, correlates a result with the
request that asked for it, and refuses to pretend PT is there when it is not.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest
from packet_tracer_mcp.infrastructure.execution.live_bridge import PTCommandBridge

from ptauto.errors import BridgeError
from ptauto.transport import BridgeTransport

TOKEN = "t" * 40


@pytest.fixture
def bridge():
    instance = PTCommandBridge(port=0, token=TOKEN)
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def transport(bridge):
    return BridgeTransport(port=bridge.port, token=TOKEN, autostart=False)


def fake_packet_tracer(transport: BridgeTransport, answer: str) -> threading.Thread:
    """Poll /next like the PT extension does, and post back the answer."""

    def run():
        status, body = transport._get(f"{transport.base_url}/next", timeout=5.0)
        if status != 200 or not body:
            return
        rid = body.split("rid=")[1].split("\\")[0].split("&")[0]
        urllib.request.urlopen(
            urllib.request.Request(
                f"{transport.base_url}/result?t={TOKEN}&rid={rid}",
                data=answer.encode(),
                method="POST",
            ),
            timeout=5.0,
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_bridge_with_our_token_is_recognised_as_ours(transport):
    assert transport.identity() == "ours"


def test_a_bridge_with_a_different_token_is_foreign(bridge):
    stranger = BridgeTransport(port=bridge.port, token="x" * 40, autostart=False)
    assert stranger.identity() == "foreign"


def test_nothing_listening_is_reported_as_none():
    assert BridgeTransport(port=1, token=TOKEN, autostart=False).identity() == "none"


def test_no_packet_tracer_means_no_channel_and_a_useful_error(transport):
    # The bridge is up, but nothing inside PT is polling it.
    assert transport.channel() == ""
    with pytest.raises(BridgeError, match="MCP Control Center"):
        transport.send("reportResult('hi')")


def test_a_result_comes_back_to_the_request_that_asked_for_it(transport):
    fake_packet_tracer(transport, json.dumps({"ok": True}))
    # /next marks PT as connected, so the send picks the HTTP channel.
    answer = transport.send_and_wait("reportResult('x')", timeout=5.0)
    assert json.loads(answer) == {"ok": True}


def test_every_request_carries_the_token(transport):
    assert f"t={TOKEN}" in transport._signed(f"{transport.base_url}/next")
    assert transport._get(f"{transport.base_url}/status")[0] == 200
    unsigned = urllib.request.Request(f"{transport.base_url}/status")
    with pytest.raises(Exception):
        urllib.request.urlopen(unsigned, timeout=2.0)


def test_a_freshly_started_bridge_waits_for_packet_tracers_first_poll():
    """The regression that made ptauto look broken against a healthy PT.

    When no MCP server holds the port, ptauto starts its own bridge and then
    asked it, microseconds later, whether PT had polled. It never had, so every
    short-lived CLI run reported "PT polling no" and exited, tearing the
    listener down before the webview's 500 ms tick could reach it.
    """
    transport = BridgeTransport(port=0, token=TOKEN)

    def late_packet_tracer():
        # PT cannot poll before the port is bound, and does not poll the instant
        # it is: this stands in for the tick ptauto used to run away from.
        deadline = time.monotonic() + 5.0
        while transport._own_bridge is None and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        transport._get(f"{transport.base_url}/next", timeout=5.0)

    thread = threading.Thread(target=late_packet_tracer, daemon=True)
    thread.start()
    try:
        assert transport.status().channel == "http"
    finally:
        thread.join(timeout=5.0)
        if transport._own_bridge is not None:
            transport._own_bridge.stop()


def test_the_first_poll_wait_happens_once_and_does_not_delay_later_calls():
    transport = BridgeTransport(port=0, token=TOKEN)
    try:
        # No PT at all: the first call pays the wait and reports honestly.
        assert transport.status().channel == ""
        assert transport._awaited_first_poll
        started = time.monotonic()
        assert transport.status().channel == ""
        assert time.monotonic() - started < 1.0
    finally:
        if transport._own_bridge is not None:
            transport._own_bridge.stop()
