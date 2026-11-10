"""
POX SDN Controller - Bandwidth Meter and Traffic Analyzer (Project 26)
5-Switch Redundant Topology

Copy to: ~/pox/ext/bandwidth_controller.py

Run with:
    cd ~/pox
    python3 pox.py bandwidth_controller openflow.of_01 --port=6633

REST API (port 8080):
    GET  /stats       - port bandwidth per switch
    GET  /flows       - active flows per switch
    GET  /port_stats  - detailed port counters
    GET  /topology    - connected switch count
    POST /alert       - app reports high loss, controller installs DROP rule
"""

from pox.core import core
from pox.lib.util import dpidToStr
from pox.lib.addresses import IPAddr
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import *
from pox.lib.recoco import Timer

import json, time, threading, ipaddress
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

log = core.getLogger()

UDP_TEST_PORT    = 5005
MONITOR_INTERVAL = 2
BW_ALERT_MBPS    = 8.0
REST_PORT        = 8080


# ------------------------------------------------------------------ #
#  Shared state                                                        #
# ------------------------------------------------------------------ #

class StatsStore:
    def __init__(self):
        self.port_stats  = {}   # dpid -> {port_no -> {tx_bytes,...}}
        self.port_bw     = {}   # dpid -> {port_no -> mbps}
        self.flow_stats  = {}   # dpid -> [flows]
        self.blocked_ips = set()
        self.connections = {}   # dpid -> connection
        self.lock        = threading.Lock()

store = StatsStore()


# ------------------------------------------------------------------ #
#  REST API                                                            #
# ------------------------------------------------------------------ #

class RestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress HTTP access logs

    def _json(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        with store.lock:
            if path == "/stats":
                self._json(200, {
                    "port_bandwidth_mbps": {
                        str(k): {str(p): v for p, v in bw.items()}
                        for k, bw in store.port_bw.items()
                    },
                    "blocked_ips": list(store.blocked_ips),
                    "connected_switches": len(store.connections),
                })
            elif path == "/flows":
                self._json(200, {
                    str(k): v for k, v in store.flow_stats.items()
                })
            elif path == "/port_stats":
                self._json(200, {
                    str(k): {str(p): s for p, s in ps.items()}
                    for k, ps in store.port_stats.items()
                })
            elif path == "/topology":
                self._json(200, {
                    "connected_switches": len(store.connections),
                    "switch_ids": [dpidToStr(d) for d in store.connections],
                })
            else:
                self._json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path == "/alert":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data   = json.loads(self.rfile.read(length))
                src_ip = data.get("src_ip")
                loss   = float(data.get("loss_pct", 0))
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            if not src_ip:
                self._json(400, {"error": "missing src_ip"})
                return
            try:
                src_ip = str(ipaddress.IPv4Address(src_ip))
            except (ipaddress.AddressValueError, TypeError):
                self._json(400, {"error": "src_ip must be a valid IPv4 address"})
                return

            log.warning("APP ALERT: src=%s loss=%.1f%% — blocking on all switches", src_ip, loss)
            installed = _block_ip(src_ip)
            if installed == 0:
                self._json(503, {"error": "no connected switches; drop rule was not installed"})
                return
            with store.lock:
                store.blocked_ips.add(src_ip)
            self._json(200, {
                "action": "blocked", "src_ip": src_ip, "loss_pct": loss,
                "switches_blocked": installed,
            })
        else:
            self._json(404, {"error": "not found"})


def _install_drop(conn, src_ip):
    """Install a temporary IPv4 source-address DROP rule on one switch."""
    msg               = of.ofp_flow_mod()
    msg.priority      = 100
    msg.hard_timeout  = 60
    msg.match.dl_type = 0x0800
    msg.match.nw_src  = IPAddr(src_ip)
    # No actions means DROP in OpenFlow 1.0.
    conn.send(msg)


def _block_ip(src_ip):
    """Install DROP rules on every currently connected switch."""
    with store.lock:
        conns = dict(store.connections)
    installed = 0
    for dpid, conn in conns.items():
        try:
            _install_drop(conn, src_ip)
            installed += 1
            log.warning("DROP installed: dpid=%s src=%s", dpidToStr(dpid), src_ip)
        except Exception as exc:
            log.error("Could not install DROP on dpid=%s for %s: %s",
                      dpidToStr(dpid), src_ip, exc)
    return installed


def _start_rest():
    try:
        server = HTTPServer(("0.0.0.0", REST_PORT), RestHandler)
        log.info("REST API on http://0.0.0.0:%d", REST_PORT)
        server.serve_forever()
    except OSError as e:
        log.error("REST API failed to start: %s", e)


# ------------------------------------------------------------------ #
#  POX Controller                                                      #
# ------------------------------------------------------------------ #

class BandwidthController(EventMixin):

    def __init__(self):
        self.mac_to_port = {}  # dpid -> {mac -> port}
        self.listenTo(core.openflow)
        threading.Thread(target=_start_rest, daemon=True).start()
        Timer(MONITOR_INTERVAL, self._monitor, recurring=True)
        log.info("BandwidthController started — 5-switch redundant topology")

    # ── Switch connects ──────────────────────────────────────────────

    def _handle_ConnectionUp(self, event):
        dpid = event.dpid
        log.info("Switch connected: %s (total=%d)", dpidToStr(dpid), len(store.connections) + 1)
        self.mac_to_port.setdefault(dpid, {})
        with store.lock:
            store.connections[dpid] = event.connection
            blocked_now = list(store.blocked_ips)
        # Reapply active blocks if a switch reconnects during an alert window.
        for src_ip in blocked_now:
            try:
                _install_drop(event.connection, src_ip)
            except Exception as exc:
                log.error("Could not restore DROP on dpid=%s for %s: %s",
                          dpidToStr(dpid), src_ip, exc)
        # Install table-miss: send unknown packets to controller
        msg          = of.ofp_flow_mod()
        msg.priority = 0
        msg.match    = of.ofp_match()
        msg.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
        event.connection.send(msg)

    def _handle_ConnectionDown(self, event):
        dpid = event.dpid
        log.warning("Switch DISCONNECTED: %s — rerouting activating", dpidToStr(dpid))
        with store.lock:
            store.connections.pop(dpid, None)
            # Stale counters from a dead switch must not be reported as live data.
            store.port_stats.pop(dpid, None)
            store.port_bw.pop(dpid, None)
            store.flow_stats.pop(dpid, None)
            if not store.connections:
                # Experiment over: the DROP rules lived on the switches that just
                # went away, so do not keep blocking those IPs in the next run.
                store.blocked_ips.clear()
        self.mac_to_port.pop(dpid, None)
        self._clear_and_relearn()

    def _handle_PortStatus(self, event):
        """Detect link failures and trigger rerouting."""
        desc   = event.ofp.desc
        reason = event.ofp.reason
        if reason == of.OFPPR_MODIFY:
            if desc.state & of.OFPPS_LINK_DOWN:
                log.warning("LINK DOWN: dpid=%s port=%d — rerouting via backup path",
                            dpidToStr(event.dpid), desc.port_no)
                self._clear_and_relearn()
            else:
                log.info("LINK UP: dpid=%s port=%d", dpidToStr(event.dpid), desc.port_no)

    def _clear_and_relearn(self):
        """
        Delete all learned flows on all switches.
        Forces flooding → switches re-learn MAC locations
        → traffic automatically takes alternate path.
        """
        with store.lock:
            conns = dict(store.connections)
        # Empty each switch's MAC table but keep the per-switch dicts: removing
        # them made every later PacketIn raise KeyError, so the controller
        # stopped forwarding after the first link failure and never recovered.
        for table in self.mac_to_port.values():
            table.clear()
        for dpid, conn in conns.items():
            # Delete all installed flows (priority > 0)
            del_msg          = of.ofp_flow_mod()
            del_msg.command  = of.OFPFC_DELETE
            del_msg.match    = of.ofp_match()
            del_msg.priority = 1
            conn.send(del_msg)
            # Re-install table-miss
            miss          = of.ofp_flow_mod()
            miss.priority = 0
            miss.match    = of.ofp_match()
            miss.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
            conn.send(miss)
            # A topology change must not silently remove active security blocks.
            with store.lock:
                blocked_now = list(store.blocked_ips)
            for src_ip in blocked_now:
                try:
                    _install_drop(conn, src_ip)
                except Exception as exc:
                    log.error("Could not restore DROP on dpid=%s for %s: %s",
                              dpidToStr(dpid), src_ip, exc)
            log.info("Flows cleared on %s — re-learning via alternate path", dpidToStr(dpid))

    # ── Packet in ────────────────────────────────────────────────────

    def _handle_PacketIn(self, event):
        dpid   = event.dpid
        inport = event.port
        pkt    = event.parsed

        if not pkt.parsed:
            return

        # Drop blocked IPs immediately
        ip_pkt = pkt.find('ipv4')
        if ip_pkt:
            with store.lock:
                blocked = set(store.blocked_ips)
            if str(ip_pkt.srcip) in blocked:
                return

        src = pkt.src
        dst = pkt.dst
        self.mac_to_port.setdefault(dpid, {})[src] = inport

        # Determine output port
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = of.OFPP_FLOOD

        # Install flow rule when destination is known
        if out_port != of.OFPP_FLOOD:
            udp_pkt = pkt.find('udp')
            if udp_pkt and ip_pkt and udp_pkt.dstport == UDP_TEST_PORT:
                # Priority 20 for UDP measurement traffic (QoS)
                msg              = of.ofp_flow_mod()
                msg.priority     = 20
                msg.idle_timeout = 30
                msg.hard_timeout = 60
                # Build the match from the parsed packet so OpenFlow 1.0
                # receives the complete, correctly-wildcarded UDP/IP tuple.
                msg.match = of.ofp_match.from_packet(pkt, inport)
                msg.actions.append(of.ofp_action_output(port=out_port))
                event.connection.send(msg)
                log.debug("QoS prio=20 flow: %s->%s on dpid=%s",
                          ip_pkt.srcip, ip_pkt.dstip, dpidToStr(dpid))
            else:
                # Priority 10 for all other traffic
                msg              = of.ofp_flow_mod()
                msg.priority     = 10
                msg.idle_timeout = 20
                msg.hard_timeout = 40
                # Use the same packet-derived match for non-measurement traffic.
                msg.match = of.ofp_match.from_packet(pkt, inport)
                msg.actions.append(of.ofp_action_output(port=out_port))
                event.connection.send(msg)

        # Forward current packet
        out         = of.ofp_packet_out()
        out.data    = event.ofp
        out.in_port = inport
        out.actions.append(of.ofp_action_output(port=out_port))
        event.connection.send(out)

    # ── Stats replies ─────────────────────────────────────────────────

    def _handle_PortStatsReceived(self, event):
        dpid = event.connection.dpid
        now  = time.time()
        with store.lock:
            prev = store.port_stats.get(dpid, {})
            store.port_bw.setdefault(dpid, {})
            for stat in event.stats:
                pno = stat.port_no
                if pno > 0xFF00:
                    continue
                if pno in prev:
                    d_bytes = stat.tx_bytes - prev[pno].get("tx_bytes", 0)
                    d_time  = now - prev[pno].get("time", now)
                    if d_time > 0:
                        mbps = (d_bytes * 8) / d_time / 1e6
                        store.port_bw[dpid][pno] = round(mbps, 3)
                        if mbps > BW_ALERT_MBPS:
                            log.warning("BW ALERT dpid=%s port=%d %.2f Mbps",
                                        dpidToStr(dpid), pno, mbps)
                store.port_stats.setdefault(dpid, {})[pno] = {
                    "tx_bytes":   stat.tx_bytes,
                    "rx_bytes":   stat.rx_bytes,
                    "tx_packets": stat.tx_packets,
                    "rx_packets": stat.rx_packets,
                    "tx_dropped": stat.tx_dropped,
                    "rx_dropped": stat.rx_dropped,
                    "rx_errors":  stat.rx_errors,
                    "time":       now,
                }

    def _handle_FlowStatsReceived(self, event):
        dpid = event.connection.dpid
        with store.lock:
            store.flow_stats[dpid] = [
                {
                    "priority":     f.priority,
                    "packet_count": f.packet_count,
                    "byte_count":   f.byte_count,
                    "duration_sec": f.duration_sec,
                }
                for f in event.stats
            ]

    # ── Periodic monitor ─────────────────────────────────────────────

    def _monitor(self):
        with store.lock:
            conns = dict(store.connections)
        for dpid, conn in conns.items():
            conn.send(of.ofp_stats_request(body=of.ofp_port_stats_request()))
            conn.send(of.ofp_stats_request(body=of.ofp_flow_stats_request()))


def launch():
    core.registerNew(BandwidthController)
