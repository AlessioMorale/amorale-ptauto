"""Connectivity to Packet Tracer: the same channels the MCP server uses.

ptauto deliberately does not invent its own way into PT. It reuses
`packet_tracer_mcp`'s primitives:

* the HTTP command bridge (`PTCommandBridge`) that the MCP Control Center
  webview polls, including its shared-token authentication, and
* the file mailbox (`FileBridge`) the PT Script Engine drains when the
  extension window is closed.

What lives here is only the *client* side of those channels. Upstream that code
sits inside the `register_tools()` closure and cannot be imported, so it is
re-expressed here against the same endpoints and the same wire format: POST
/queue to enqueue, GET /result?rid=… to collect, one channel per command.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from packet_tracer_mcp.infrastructure.execution.bridge_token import (
    get_bridge_token,
    token_fingerprint,
)
from packet_tracer_mcp.infrastructure.execution.file_bridge import FileBridge
from packet_tracer_mcp.infrastructure.execution.live_bridge import (
    DEFAULT_PORT,
    PTCommandBridge,
    next_rid,
    report_result_js,
)

from .errors import BridgeError

# The PT webview polls on a 500 ms tick against a 2 s long-poll on /next, so a
# bridge we just bound has no poll recorded yet. Reading "connected" straight
# after start() therefore always says no, and a short-lived CLI process exits
# before PT ever gets to answer. Only a bridge we started needs this wait: one
# already listening has been polled for a while.
FIRST_POLL_WAIT_S = 3.0

NO_PT_MESSAGE = (
    "Packet Tracer is not reachable on any channel.\n"
    "  * Open Packet Tracer and load the MCP Control Center extension "
    "(Extensions > MCP BUILDER).\n"
    "  * With the extension window open ptauto uses the HTTP bridge; close it "
    "and the file bridge takes over while PT stays open.\n"
    "  * `ptauto status` shows what ptauto can see."
)


@dataclass
class ChannelStatus:
    """What ptauto can see of the bridge right now."""

    channel: str  # "http" | "file" | ""
    bridge_identity: str  # "ours" | "foreign" | "none"
    pt_connected: bool
    file_bridge_alive: bool
    port: int
    token_id: str
    detail: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.channel in ("http", "file")


class BridgeTransport:
    """Sends JavaScript to Packet Tracer and (optionally) waits for a result.

    One command goes over exactly one channel, never both, so nothing can be
    executed twice. HTTP wins when the extension window is open because that is
    the tested path; the file mailbox covers the window-closed case.
    """

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        token: str | None = None,
        autostart: bool = True,
    ) -> None:
        self.port = port
        self._token = token
        self._autostart = autostart
        self._own_bridge: PTCommandBridge | None = None
        self._awaited_first_poll = False
        self._file_bridge = FileBridge()

    # -- plumbing -------------------------------------------------------

    @property
    def token(self) -> str:
        return self._token or get_bridge_token()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _signed(self, url: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}t={urllib.parse.quote(self.token)}"

    def _get(self, url: str, timeout: float = 2.0) -> tuple[int | None, str | None]:
        try:
            with urllib.request.urlopen(self._signed(url), timeout=timeout) as r:
                return r.status, r.read().decode("utf-8")
        except Exception:
            return None, None

    def _post(
        self, url: str, body: str, timeout: float = 5.0
    ) -> tuple[int | None, str | None]:
        try:
            req = urllib.request.Request(
                self._signed(url), data=body.encode("utf-8"), method="POST"
            )
            req.add_header("Content-Type", "text/plain")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8")
        except Exception:
            return None, None

    # -- channel discovery ----------------------------------------------

    def identity(self) -> str:
        """Who owns the bridge port: 'ours' | 'foreign' | 'none'.

        /ping is unauthenticated and returns only a fingerprint of the token, so
        this tells us whether the listener is a bridge that shares our secret
        without ever handing the secret out.
        """
        status, body = self._get(f"{self.base_url}/ping", timeout=1.0)
        if status != 200 or not body:
            return "none"
        try:
            doc = json.loads(body)
        except Exception:
            return "foreign"
        if doc.get("service") != "pt-mcp-bridge":
            return "foreign"
        if doc.get("id") != token_fingerprint(self.token):
            return "foreign"
        return "ours"

    def pt_connected(self) -> bool:
        status, body = self._get(f"{self.base_url}/status", timeout=1.0)
        if status == 200 and body:
            try:
                return bool(json.loads(body).get("connected"))
            except Exception:
                return False
        return False

    def ensure_bridge(self) -> bool:
        """Make sure a bridge is listening, starting an in-process one if not."""
        if self.identity() == "ours":
            return True
        if not self._autostart:
            return False
        if self._own_bridge is None:
            try:
                bridge = PTCommandBridge(port=self.port, token=self._token)
                bridge.start()
                self.port = bridge.port
                self._own_bridge = bridge
            except OSError:
                return False
        if self.identity() != "ours":
            return False
        self._await_first_poll()
        return True

    def _await_first_poll(self, timeout: float = FIRST_POLL_WAIT_S) -> bool:
        """Give a bridge we just started time for PT's first poll to land.

        Runs at most once per process, and only for our own bridge: without it
        every `ptauto` invocation made while no MCP server holds the port reports
        "PT polling no" against a perfectly healthy Packet Tracer, because the
        answer is read milliseconds after bind.
        """
        if self._own_bridge is None or self._awaited_first_poll:
            return self.pt_connected()
        self._awaited_first_poll = True
        deadline = time.monotonic() + timeout
        while True:
            if self.pt_connected():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def channel(self) -> str:
        """'http' | 'file' | '': the channel a command would take right now."""
        if self.identity() == "ours" and self.pt_connected():
            return "http"
        if self._file_bridge.pt_alive():
            return "file"
        return ""

    def status(self) -> ChannelStatus:
        self.ensure_bridge()
        identity = self.identity()
        connected = self.pt_connected() if identity == "ours" else False
        alive = self._file_bridge.pt_alive()
        channel = "http" if (identity == "ours" and connected) else ("file" if alive else "")
        detail: dict = {}
        if identity == "ours":
            _, body = self._get(f"{self.base_url}/status", timeout=1.0)
            if body:
                try:
                    detail = json.loads(body)
                except Exception:
                    detail = {}
        return ChannelStatus(
            channel=channel,
            bridge_identity=identity,
            pt_connected=connected,
            file_bridge_alive=alive,
            port=self.port,
            token_id=token_fingerprint(self.token),
            detail=detail,
        )

    def require_channel(self) -> str:
        channel = self.channel()
        if channel:
            return channel
        # Nothing answered: start the HTTP bridge anyway, in case PT is about to
        # come up, then report honestly.
        self.ensure_bridge()
        channel = self.channel()
        if channel:
            return channel
        raise BridgeError(NO_PT_MESSAGE)

    # -- sending ---------------------------------------------------------

    def send(self, js: str) -> bool:
        """Fire-and-forget. The guard keeps a PT-side error from opening a modal
        that would freeze the webview and kill the bridge."""
        channel = self.require_channel()
        guarded = "try{" + js + "}catch(__pterr){}"
        if channel == "http":
            status, _ = self._post(f"{self.base_url}/queue", guarded)
            return status == 200
        return self._file_bridge.send(guarded)

    def send_and_wait(self, js: str, timeout: float = 10.0) -> str | None:
        """Run `js` in PT and return whatever it passes to `reportResult(...)`.

        The two channels differ only in how reportResult reaches us: over HTTP it
        is defined inline and XHRs back with a per-operation `rid`; over the file
        mailbox the Script Engine supplies its own and correlates by filename.
        """
        channel = self.require_channel()
        guarded = "try{" + js + "}catch(__pterr){reportResult('PT_ERROR: '+__pterr);}"
        if channel == "http":
            rid = next_rid()
            wrapped = report_result_js(self.port, self.token, rid) + ";" + guarded
            status, _ = self._post(f"{self.base_url}/queue", wrapped)
            if status != 200:
                return None
            # The server owns the wait; the socket timeout sits above it so a 204
            # means "PT did not answer", never "the socket gave up first".
            status, body = self._get(
                f"{self.base_url}/result?rid={rid}&wait={timeout}",
                timeout=timeout + 5.0,
            )
            return body if status == 200 else None
        return self._file_bridge.send_and_wait(guarded, timeout=timeout)
