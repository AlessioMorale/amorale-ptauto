"""Typed operations against a live Packet Tracer.

Everything in ptauto that touches PT goes through this class. Each method sends
one piece of JavaScript through the bridge and parses one JSON answer, so the
rest of the library never sees a string of JS or a bare `reportResult`.

The JS itself is built with `json.dumps` for every interpolated value. That is
not decoration: a device name with a quote or a newline in it produces a JS
SyntaxError inside PT, and a SyntaxError in the Script Engine surfaces as a
modal dialog that freezes the webview and takes the bridge down with it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from packet_tracer_mcp.adapters.mcp.tool_registry import (
    console_ping_arm_js,
    console_ping_poll_js,
)
from packet_tracer_mcp.shared.constants import PT_CONNECT_TYPE, PT_DEVICE_TYPE
from packet_tracer_mcp.shared.utils import classify_ping

from .catalog import ModelInfo
from .errors import PTError, PTTimeout
from .transport import BridgeTransport

# PT's own device-type enum, keyed by the catalog's category. A few models are
# their own type even though they behave like a PC, so they are matched by name.
_TYPE_BY_NAME_HINT = (
    ("printer", PT_DEVICE_TYPE["printer"]),
    ("laptop", PT_DEVICE_TYPE["laptop"]),
    ("tablet", PT_DEVICE_TYPE["tablet"]),
    ("tv", PT_DEVICE_TYPE["tv"]),
)


def pt_device_type(info: ModelInfo) -> int:
    lowered = info.pt_type.lower()
    for hint, value in _TYPE_BY_NAME_HINT:
        if hint in lowered:
            return value
    return PT_DEVICE_TYPE.get(info.category, PT_DEVICE_TYPE["pc"])


def pt_cable_type(cable: str) -> int:
    return PT_CONNECT_TYPE.get(cable, PT_CONNECT_TYPE["auto"])


# --------------------------------------------------------------------------
# what PT reports back
# --------------------------------------------------------------------------


@dataclass
class ObservedPort:
    name: str
    ip: str = "0.0.0.0"
    mask: str = "0.0.0.0"
    linked: bool = False
    up: bool = False
    dhcp_client: bool = False

    @property
    def has_address(self) -> bool:
        return bool(self.ip) and self.ip != "0.0.0.0"


@dataclass
class ObservedDevice:
    name: str
    model: str
    x: int = 0
    y: int = 0
    ports: dict[str, ObservedPort] = field(default_factory=dict)


@dataclass
class ObservedLink:
    device_a: str
    port_a: str
    device_b: str
    port_b: str

    @property
    def key(self) -> frozenset[tuple[str, str]]:
        return frozenset({(self.device_a, self.port_a), (self.device_b, self.port_b)})


@dataclass
class ObservedTopology:
    devices: dict[str, ObservedDevice] = field(default_factory=dict)
    links: list[ObservedLink] = field(default_factory=list)

    def link_keys(self) -> dict[frozenset, ObservedLink]:
        return {link.key: link for link in self.links}

    def port_is_cabled(self, device: str, port: str) -> bool:
        observed = self.devices.get(device)
        if observed is None:
            return False
        entry = observed.ports.get(port)
        return bool(entry and entry.linked)


@dataclass
class HostState:
    """A PC/Server/Printer's IP settings, as PT has them."""

    found: bool
    dhcp: bool = False
    ip: str = "0.0.0.0"
    mask: str = "0.0.0.0"
    gateway: str = ""
    dns: str = ""
    port: str = ""


@dataclass
class PingResult:
    source: str
    target: str
    verdict: str  # "ok" | "partial" | "none" | "unknown"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"

    def __bool__(self) -> bool:
        return self.ok


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------


class PTClient:
    """High-level, typed access to the Packet Tracer workspace."""

    def __init__(self, transport: BridgeTransport | None = None) -> None:
        self.transport = transport or BridgeTransport()

    # -- plumbing -------------------------------------------------------

    def _call(self, js: str, timeout: float = 15.0, what: str = "operation") -> dict:
        """Run JS that reports a JSON object, and return it."""
        raw = self.transport.send_and_wait(js, timeout=timeout)
        if raw is None:
            raise PTTimeout(
                f"Packet Tracer did not answer within {timeout:g}s ({what}). "
                f"Check `ptauto status`; a modal dialog open in PT blocks the bridge."
            )
        if raw.startswith("PT_ERROR:"):
            raise PTError(f"Packet Tracer raised an error during {what}: {raw[9:].strip()}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise PTError(f"Unreadable answer from Packet Tracer during {what}: {raw[:200]!r}") from None

    @staticmethod
    def _wrap(body: str) -> str:
        """An IIFE, so early `return` works inside the Script Engine."""
        return "(function(){" + body + "})()"

    def is_connected(self) -> bool:
        return self.transport.channel() != ""

    # -- reading the workspace -------------------------------------------

    def topology(self) -> ObservedTopology:
        """Everything PT currently has: devices, ports and links."""
        body = """
var net=ipc.network();
var out={devices:[],links:[]};
var n=net.getDeviceCount();
for(var i=0;i<n;i++){
  var d=net.getDeviceAt(i);
  var dev={name:String(d.getName()),model:String(d.getModel()),x:0,y:0,ports:[]};
  // Placement is by centre (that is what lwAddDevice takes), while
  // getXCoordinate returns the icon's top-left corner. Comparing the two makes
  // every device look displaced by half an icon on every run.
  try{dev.x=Math.round(d.getCenterXCoordinate());dev.y=Math.round(d.getCenterYCoordinate());}
  catch(e){try{dev.x=Math.round(d.getXCoordinate());dev.y=Math.round(d.getYCoordinate());}catch(e2){}}
  var pc=d.getPortCount();
  for(var j=0;j<pc;j++){
    var p=d.getPortAt(j);
    var info={name:String(p.getName()),ip:"0.0.0.0",mask:"0.0.0.0",linked:false,up:false,dhcp:false};
    try{info.ip=String(p.getIpAddress());}catch(e){}
    try{info.mask=String(p.getSubnetMask());}catch(e){}
    try{info.linked=(p.getLink()!=null);}catch(e){}
    try{info.up=!!p.isPortUp();}catch(e){}
    try{info.dhcp=(typeof p.isDhcpClientOn==="function")?!!p.isDhcpClientOn():false;}catch(e){}
    dev.ports.push(info);
  }
  out.devices.push(dev);
}
var lc=net.getLinkCount();
for(var k=0;k<lc;k++){
  var l=net.getLinkAt(k);
  try{
    if(String(l.getClassName())==="Antenna"){continue;}
    var p1=l.getPort1(),p2=l.getPort2();
    out.links.push({a:String(p1.getOwnerDevice().getName()),ap:String(p1.getName()),
                    b:String(p2.getOwnerDevice().getName()),bp:String(p2.getName())});
  }catch(e){}
}
reportResult(JSON.stringify(out));
"""
        data = self._call(self._wrap(body), timeout=25.0, what="reading the topology")
        topology = ObservedTopology()
        for entry in data.get("devices", []):
            device = ObservedDevice(
                name=entry["name"], model=entry["model"], x=entry.get("x", 0), y=entry.get("y", 0)
            )
            for port in entry.get("ports", []):
                device.ports[port["name"]] = ObservedPort(
                    name=port["name"],
                    ip=port.get("ip", "0.0.0.0"),
                    mask=port.get("mask", "0.0.0.0"),
                    linked=bool(port.get("linked")),
                    up=bool(port.get("up")),
                    dhcp_client=bool(port.get("dhcp")),
                )
            topology.devices[device.name] = device
        for link in data.get("links", []):
            topology.links.append(
                ObservedLink(link["a"], link["ap"], link["b"], link["bp"])
            )
        return topology

    # -- building --------------------------------------------------------

    def add_device(self, name: str, info: ModelInfo, x: int, y: int) -> None:
        body = (
            f"var name={json.dumps(name)};"
            "if(ipc.network().getDevice(name)){reportResult(JSON.stringify({ok:false,error:'already exists'}));return;}"
            f"lwAddDevice(name,{pt_device_type(info)},{json.dumps(info.pt_type)},{int(x)},{int(y)});"
            "var made=ipc.network().getDevice(name);"
            "reportResult(JSON.stringify(made?{ok:true,model:String(made.getModel())}"
            ":{ok:false,error:'PT did not create the device'}));"
        )
        result = self._call(self._wrap(body), timeout=20.0, what=f"creating {name}")
        if not result.get("ok"):
            raise PTError(f"Could not create {name} ({info.pt_type}): {result.get('error')}")

    def delete_device(self, name: str) -> None:
        body = (
            f"var name={json.dumps(name)};"
            "var d=ipc.network().getDevice(name);"
            "if(!d){reportResult(JSON.stringify({ok:true,note:'already absent'}));return;}"
            "var lw=ipc.appWindow().getActiveWorkspace().getLogicalWorkspace();"
            "if(typeof lw.removeDevice!=='function'){reportResult(JSON.stringify({ok:false,error:'removeDevice missing in this PT build'}));return;}"
            "lw.removeDevice(d.getName());"
            "reportResult(JSON.stringify(ipc.network().getDevice(name)?{ok:false,error:'still present'}:{ok:true}));"
        )
        result = self._call(self._wrap(body), timeout=15.0, what=f"deleting {name}")
        if not result.get("ok"):
            raise PTError(f"Could not delete {name}: {result.get('error')}")

    def add_link(
        self, device_a: str, port_a: str, device_b: str, port_b: str, cable: str
    ) -> None:
        body = (
            f"var a={json.dumps(device_a)},ap={json.dumps(port_a)};"
            f"var b={json.dumps(device_b)},bp={json.dumps(port_b)};"
            "var da=ipc.network().getDevice(a),db=ipc.network().getDevice(b);"
            "if(!da||!db){reportResult(JSON.stringify({ok:false,error:'device missing'}));return;}"
            "var pa=da.getPort(ap),pb=db.getPort(bp);"
            "if(!pa){reportResult(JSON.stringify({ok:false,error:'no port '+ap+' on '+a}));return;}"
            "if(!pb){reportResult(JSON.stringify({ok:false,error:'no port '+bp+' on '+b}));return;}"
            "if(pa.getLink()!=null){reportResult(JSON.stringify({ok:false,error:ap+' on '+a+' is already cabled'}));return;}"
            "if(pb.getLink()!=null){reportResult(JSON.stringify({ok:false,error:bp+' on '+b+' is already cabled'}));return;}"
            f"lwAddLink(a,ap,b,bp,{pt_cable_type(cable)});"
            "var check=ipc.network().getDevice(a).getPort(ap);"
            "reportResult(JSON.stringify((check&&check.getLink()!=null)?{ok:true}:{ok:false,error:'link not created'}));"
        )
        result = self._call(
            self._wrap(body), timeout=20.0, what=f"cabling {device_a}:{port_a} to {device_b}:{port_b}"
        )
        if not result.get("ok"):
            raise PTError(
                f"Could not cable {device_a}:{port_a} <-> {device_b}:{port_b}: {result.get('error')}"
            )

    def delete_link(self, device: str, port: str) -> None:
        body = (
            f"var d=ipc.network().getDevice({json.dumps(device)});"
            "if(!d){reportResult(JSON.stringify({ok:true,note:'device absent'}));return;}"
            f"var p=d.getPort({json.dumps(port)});"
            "if(!p||p.getLink()==null){reportResult(JSON.stringify({ok:true,note:'no link'}));return;}"
            "p.deleteLink();"
            "reportResult(JSON.stringify({ok:true}));"
        )
        self._call(self._wrap(body), timeout=15.0, what=f"removing the cable on {device}:{port}")

    def move_device(self, name: str, x: int, y: int) -> None:
        body = (
            f"var d=ipc.network().getDevice({json.dumps(name)});"
            "if(!d){reportResult(JSON.stringify({ok:false,error:'not found'}));return;}"
            f"d.moveToLocation({int(x)},{int(y)});"
            "reportResult(JSON.stringify({ok:true}));"
        )
        self._call(self._wrap(body), timeout=10.0, what=f"moving {name}")

    # -- IOS devices ------------------------------------------------------

    def configure_ios(self, device: str, cli: str, timeout: float = 60.0) -> None:
        """Push a CLI script through PT's own `configureIosDevice` helper."""
        body = (
            f"var ok=configureIosDevice({json.dumps(device)},{json.dumps(cli)});"
            "reportResult(JSON.stringify({ok:(ok===true||ok===undefined)}));"
        )
        result = self._call(self._wrap(body), timeout=timeout, what=f"configuring {device}")
        if not result.get("ok"):
            raise PTError(f"Packet Tracer refused the configuration for {device}")

    def read_ios_config(self, device: str, refresh: bool = True) -> str | None:
        """The device's own configuration text, or None if it has none.

        PT exposes the saved configuration, not the running one, so `refresh`
        first issues a bare `write memory`. That makes the two identical at the
        moment of reading — including any change made by hand in the GUI, which
        is exactly the drift an idempotent tool has to notice.
        """
        if refresh:
            try:
                self.configure_ios(device, "enable\nwrite memory", timeout=45.0)
            except (PTError, PTTimeout):
                # A device that cannot save is still worth reading.
                pass
        body = (
            f"var d=ipc.network().getDevice({json.dumps(device)});"
            "if(!d){reportResult(JSON.stringify({found:false}));return;}"
            "if(typeof d.getStartupFile!=='function'){reportResult(JSON.stringify({found:true,supported:false}));return;}"
            "reportResult(JSON.stringify({found:true,supported:true,config:String(d.getStartupFile()||'')}));"
        )
        result = self._call(self._wrap(body), timeout=25.0, what=f"reading {device}'s configuration")
        if not result.get("found") or not result.get("supported"):
            return None
        return result.get("config") or ""

    def read_vlans(self, switch: str) -> dict[int, str] | None:
        """VLAN database of a switch, or None if the model has no VlanManager."""
        body = (
            f"var d=ipc.network().getDevice({json.dumps(switch)});"
            "if(!d){reportResult(JSON.stringify({found:false}));return;}"
            "var vm=(typeof d.getProcess==='function')?d.getProcess('VlanManager'):null;"
            "if(!vm){reportResult(JSON.stringify({found:true,supported:false}));return;}"
            "var out=[];var n=vm.getVlanCount();"
            "for(var i=0;i<n;i++){try{var v=vm.getVlanAt(i);if(v){out.push({n:v.getVlanNumber(),name:String(v.getName())});}}catch(e){}}"
            "reportResult(JSON.stringify({found:true,supported:true,vlans:out}));"
        )
        result = self._call(self._wrap(body), timeout=15.0, what=f"reading VLANs on {switch}")
        if not result.get("found") or not result.get("supported"):
            return None
        return {int(v["n"]): v["name"] for v in result.get("vlans", [])}

    # -- hosts -------------------------------------------------------------

    def read_host(self, device: str) -> HostState:
        """A host's IP settings.

        The gateway and the DNS server have setters in PT's API but no getters,
        so they are recovered from the device's own XML — which is the same data
        the GUI shows, read without a round trip per field.
        """
        body = (
            f"var d=ipc.network().getDevice({json.dumps(device)});"
            "if(!d){reportResult(JSON.stringify({found:false}));return;}"
            "var port=null,pname='';"
            "if(typeof d.getPorts==='function'){var ps=d.getPorts();"
            "for(var i=0;i<ps.length;i++){var pn=String(ps[i]);"
            "if(pn.indexOf('Ethernet')>=0||pn==='Wireless0'){var cand=d.getPort(pn);if(cand){port=cand;pname=pn;break;}}}}"
            "if(!port){port=d.getPort('FastEthernet0');pname='FastEthernet0';}"
            "if(!port){reportResult(JSON.stringify({found:true,port:''}));return;}"
            "var out={found:true,port:pname,ip:String(port.getIpAddress()),mask:String(port.getSubnetMask()),"
            "dhcp:(typeof d.getDhcpFlag==='function')?!!d.getDhcpFlag():false,gateway:'',dns:''};"
            "try{var xml=String(d.serializeToXml()||'');"
            # A statically addressed host keeps its gateway in PORT_GATEWAY; a
            # DHCP client leaves that empty and records the leased one in
            # GATEWAY. Reading only the first made every DHCP client look as if
            # it had no default route.
            "var g=xml.match(/<PORT_GATEWAY>([^<]*)<\\/PORT_GATEWAY>/);if(g&&g[1]){out.gateway=g[1];}"
            "if(!out.gateway){var g2=xml.match(/<GATEWAY>([^<]*)<\\/GATEWAY>/);if(g2){out.gateway=g2[1];}}"
            "var s=xml.match(/<PORT_DNS>([^<]*)<\\/PORT_DNS>/);if(s){out.dns=s[1];}"
            "var dh=xml.match(/<PORT_DHCP_ENABLE>([^<]*)<\\/PORT_DHCP_ENABLE>/);"
            "if(dh){out.dhcp=(dh[1]==='true');}"
            "}catch(e){}"
            "reportResult(JSON.stringify(out));"
        )
        result = self._call(self._wrap(body), timeout=25.0, what=f"reading {device}'s IP settings")
        if not result.get("found"):
            return HostState(found=False)
        return HostState(
            found=True,
            dhcp=bool(result.get("dhcp")),
            ip=result.get("ip", "0.0.0.0"),
            mask=result.get("mask", "0.0.0.0"),
            gateway=result.get("gateway", ""),
            dns=result.get("dns", ""),
            port=result.get("port", ""),
        )

    def configure_host(
        self,
        device: str,
        dhcp: bool,
        ip: str = "",
        mask: str = "",
        gateway: str = "",
        dns: str = "",
    ) -> None:
        body = (
            f"var ok=configurePcIp({json.dumps(device)},{json.dumps(bool(dhcp))},"
            f"{json.dumps(ip)},{json.dumps(mask)},{json.dumps(gateway)},{json.dumps(dns)});"
            "reportResult(JSON.stringify({ok:(ok!==false)}));"
        )
        result = self._call(self._wrap(body), timeout=20.0, what=f"addressing {device}")
        if not result.get("ok"):
            raise PTError(f"Could not apply IP settings to {device}")

    def renew_dhcp(self, device: str) -> None:
        """Nudge a host into re-running DHCP (setting the flag again re-arms it)."""
        body = (
            f"var d=ipc.network().getDevice({json.dumps(device)});"
            "if(!d||typeof d.setDhcpFlag!=='function'){reportResult(JSON.stringify({ok:false}));return;}"
            "d.setDhcpFlag(false);d.setDhcpFlag(true);"
            "reportResult(JSON.stringify({ok:true}));"
        )
        self._call(self._wrap(body), timeout=15.0, what=f"renewing DHCP on {device}")

    # -- server services ---------------------------------------------------

    def read_services(self, device: str) -> dict:
        """DNS and HTTP state on a Server-PT, straight from its service processes."""
        body = (
            f"var d=ipc.network().getDevice({json.dumps(device)});"
            "if(!d){reportResult(JSON.stringify({found:false}));return;}"
            "var out={found:true,dns:null,http:null};"
            "try{var dns=d.getProcess('DnsServer');"
            "if(dns){var recs=[];var n=dns.getSizeOfNameServerDb();"
            "for(var i=0;i<n;i++){try{var rr=dns.getRrFromNameServerDbAt(i);"
            "if(rr){recs.push({name:String(rr.name),address:String(rr.ipAddress),type:String(rr.pduType)});}}catch(e){}}"
            "out.dns={enabled:!!dns.isEnabled(),records:recs};}}catch(e){}"
            "try{var http=d.getProcess('HttpServer');"
            "if(http){out.http={enabled:!!http.isEnabled(),port:http.getPortNumber(),"
            "index:String(http.getPage('index.html')||'')};}}catch(e){}"
            "reportResult(JSON.stringify(out));"
        )
        return self._call(self._wrap(body), timeout=20.0, what=f"reading services on {device}")

    def configure_dns(
        self, device: str, enabled: bool, records: list[tuple[str, str, str]]
    ) -> None:
        """Enable the DNS service and make its database match `records` exactly."""
        payload = json.dumps(
            [{"name": n, "address": a, "type": t} for n, a, t in records]
        )
        body = (
            f"var d=ipc.network().getDevice({json.dumps(device)});"
            "if(!d){reportResult(JSON.stringify({ok:false,error:'device not found'}));return;}"
            "var dns=d.getProcess('DnsServer');"
            "if(!dns){reportResult(JSON.stringify({ok:false,error:'no DnsServer process on this model'}));return;}"
            f"dns.setEnable({json.dumps(bool(enabled))});"
            f"var want={payload};"
            # Remove what should not be there, then add what is missing: the DB
            # ends up matching the spec whatever state it started in.
            "var have=[];var n=dns.getSizeOfNameServerDb();"
            "for(var i=0;i<n;i++){try{var rr=dns.getRrFromNameServerDbAt(i);"
            "if(rr){have.push({name:String(rr.name),address:String(rr.ipAddress),type:String(rr.pduType)});}}catch(e){}}"
            "var wanted=function(h){for(var i=0;i<want.length;i++){"
            "if(want[i].name===h.name&&want[i].address===h.address){return true;}}return false;};"
            "var removed=0;"
            "for(var i=have.length-1;i>=0;i--){if(!wanted(have[i])){"
            # Both removals take (name, value): with the name alone PT answers
            # "Invalid arguments" and the record silently survives.
            "try{if(have[i].type==='DnsRrCNAME'){dns.removeCNAMEFromNameServerDb(have[i].name,have[i].address);}"
            "else{dns.removeARecordFromNameServerDb(have[i].name,have[i].address);}removed++;}catch(e){}}}"
            "var added=0;"
            "for(var i=0;i<want.length;i++){var w=want[i];var exists=false;"
            "for(var j=0;j<have.length;j++){if(have[j].name===w.name&&have[j].address===w.address){exists=true;}}"
            "if(exists){continue;}"
            "try{if(w.type==='CNAME'){dns.addCNAMEToNameServerDb(w.name,w.address);}"
            "else{dns.addARecordToNameServerDb(w.name,w.address);}added++;}catch(e){}}"
            "reportResult(JSON.stringify({ok:true,added:added,removed:removed,enabled:!!dns.isEnabled()}));"
        )
        result = self._call(self._wrap(body), timeout=25.0, what=f"configuring DNS on {device}")
        if not result.get("ok"):
            raise PTError(f"Could not configure DNS on {device}: {result.get('error')}")

    def configure_http(
        self,
        device: str,
        enabled: bool,
        pages: dict[str, str] | None = None,
        port: int | None = None,
    ) -> None:
        body = (
            f"var d=ipc.network().getDevice({json.dumps(device)});"
            "if(!d){reportResult(JSON.stringify({ok:false,error:'device not found'}));return;}"
            "var http=d.getProcess('HttpServer');"
            "if(!http){reportResult(JSON.stringify({ok:false,error:'no HttpServer process on this model'}));return;}"
            f"http.setEnable({json.dumps(bool(enabled))});"
            + (f"http.setPortNumber({int(port)});" if port else "")
            + f"var pages={json.dumps(pages or {})};"
            "var written=[];"
            "for(var name in pages){try{http.setPageContents(name,pages[name]);written.push(name);}catch(e){}}"
            "reportResult(JSON.stringify({ok:true,pages:written,enabled:!!http.isEnabled()}));"
        )
        result = self._call(self._wrap(body), timeout=25.0, what=f"configuring HTTP on {device}")
        if not result.get("ok"):
            raise PTError(f"Could not configure HTTP on {device}: {result.get('error')}")

    # -- verification ------------------------------------------------------

    def ping(self, device: str, target: str, timeout: float = 25.0) -> PingResult:
        """A real ping, run on the device's own console.

        The arm/poll JavaScript is imported from the MCP project rather than
        rewritten: it encodes two things that are easy to get wrong — routers
        expose `getCommandLine()` but not `getCommandPrompt()`, and a console
        that has never been touched is still sitting on the initial
        configuration dialog, where the first command is eaten by the prompt.
        """
        armed = self.transport.send_and_wait(console_ping_arm_js(device, target), timeout=12.0)
        if armed is None:
            raise PTTimeout(f"{device} did not answer when starting the ping to {target}")
        if not armed.startswith("BASE:"):
            raise PTError(f"Could not ping from {device}: {armed}")
        base = int(armed[5:])

        poll = console_ping_poll_js(device, base)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            answer = self.transport.send_and_wait(poll, timeout=8.0)
            if answer is None or answer == "WAIT":
                continue
            if answer.startswith("DONE:"):
                statistics = answer[5:]
                return PingResult(device, target, classify_ping(statistics), statistics)
            if answer.startswith("ERR:"):
                raise PTError(f"Could not ping from {device}: {answer[4:]}")
        return PingResult(device, target, "unknown", f"no result after {timeout:g}s")

    def resolve(self, device: str, hostname: str, timeout: float = 25.0) -> PingResult:
        """Ping by name — proves DNS resolution and reachability in one step."""
        return self.ping(device, hostname, timeout=timeout)

    # -- project -----------------------------------------------------------

    def save_project(self, path: str) -> str:
        """Save the workspace to a .pkt file and confirm it landed on disk.

        The directory is created here, before PT is asked for anything: asked to
        write into a directory that does not exist, PT opens a modal error
        dialog, and a modal dialog freezes the webview the bridge runs in — the
        failure then looks like "Packet Tracer stopped answering".
        """
        target = path.replace("\\", "/")
        if not target.lower().endswith(".pkt"):
            target += ".pkt"
        parent = Path(target).expanduser().parent
        if str(parent) not in ("", "."):
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PTError(f"Cannot save to {target}: {exc}") from None
        body = (
            f"var full={json.dumps(target)};"
            "if(full.indexOf('/')<0){full=String(ipc.appWindow().getDefaultFileSaveLocation()).replace(/\\\\/g,'/').replace(/\\/+$/,'')+'/'+full;}"
            "ipc.appWindow().fileSaveAsNoPrompt(full,false);"
            "var fm=ipc.systemFileManager();"
            "reportResult(JSON.stringify(fm.fileExists(full)?{ok:true,path:full,size:fm.getFileSize(full)}:{ok:false,path:full}));"
        )
        result = self._call(self._wrap(body), timeout=45.0, what="saving the project")
        if not result.get("ok"):
            raise PTError(f"Packet Tracer did not write {result.get('path')}")
        return result["path"]

    def open_project(self, path: str) -> int:
        body = (
            f"var p={json.dumps(path.replace(chr(92), '/'))};"
            "var fm=ipc.systemFileManager();"
            "if(!fm.fileExists(p)){reportResult(JSON.stringify({ok:false,error:'no such file'}));return;}"
            "ipc.appWindow().fileOpen(p);"
            "reportResult(JSON.stringify({ok:true,devices:ipc.network().getDeviceCount()}));"
        )
        result = self._call(self._wrap(body), timeout=60.0, what="opening the project")
        if not result.get("ok"):
            raise PTError(f"Could not open {path}: {result.get('error')}")
        return int(result.get("devices", 0))
