#!/usr/bin/env python3
"""Collect POX port/flow counters and compare them with UDP application stats."""
import argparse
import json
import os
import time
import urllib.request

POX_BASE = "http://127.0.0.1:8080"
# Mininet port numbers follow the link creation order in topology/topo.py.
# Host-facing ingress ports: h1 -> s1:1 and h3 -> s3:3.
INGRESS_PORTS = {"1": ["1"], "3": ["3"]}


def get_json(path, timeout=3):
    with urllib.request.urlopen(f"{POX_BASE}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def collect_sdn_stats(duration, interval, output_file):
    snapshots = []
    start = time.time()
    print(f"[SDN] Collecting port and flow stats for {duration}s...")
    while time.time() - start < duration:
        stamp = time.time()
        try:
            ports = get_json("/port_stats")
            flows = get_json("/flows")
            snapshots.append({"time": stamp, "port_stats": ports, "flow_stats": flows})
            print(f"[SDN] Snapshot {len(snapshots)} at t={stamp-start:.1f}s")
        except Exception as exc:
            print(f"[SDN] Poll error: {exc}")
        remaining = interval - (time.time() - stamp)
        if remaining > 0:
            time.sleep(remaining)

    summary = {
        "collection_duration_s": round(time.time() - start, 3),
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
        "last_port_stats": snapshots[-1]["port_stats"] if snapshots else {},
        "last_flow_stats": snapshots[-1]["flow_stats"] if snapshots else {},
        "ingress_port_map": INGRESS_PORTS,
    }
    with open(output_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[SDN] Stats saved -> {output_file}")
    return summary


def _counter_delta(first, last, key):
    return max(0, int(last.get(key, 0)) - int(first.get(key, 0)))


def _load(path):
    with open(path) as f:
        return json.load(f)


def _ingress_totals(snapshots, ingress_map):
    """RX counter deltas on host-facing ports over the whole collection window
    and over the *active* window (snapshots in which the counters were rising)."""
    def rx(snap, key):
        total = 0
        for dpid, ports in ingress_map.items():
            for port in ports:
                total += int(snap.get("port_stats", {}).get(dpid, {}).get(str(port), {}).get(key, 0))
        return total

    out = {"bytes": 0, "packets": 0, "window_s": 0.0,
           "active_bytes": 0, "active_window_s": 0.0}
    if len(snapshots) < 2:
        return out
    first, last = snapshots[0], snapshots[-1]
    out["window_s"] = max(0.0, float(last["time"]) - float(first["time"]))
    out["bytes"] = max(0, rx(last, "rx_bytes") - rx(first, "rx_bytes"))
    out["packets"] = max(0, rx(last, "rx_packets") - rx(first, "rx_packets"))

    rising = [i for i in range(len(snapshots) - 1)
              if rx(snapshots[i + 1], "rx_bytes") > rx(snapshots[i], "rx_bytes")]
    if rising:
        a, b = snapshots[rising[0]], snapshots[rising[-1] + 1]
        out["active_window_s"] = max(0.0, float(b["time"]) - float(a["time"]))
        out["active_bytes"] = max(0, rx(b, "rx_bytes") - rx(a, "rx_bytes"))
    return out


def _measurement_flows(flow_stats):
    flows = []
    for dpid, entries in flow_stats.items():
        for flow in entries:
            if int(flow.get("priority", 0)) >= 20:
                flows.append({"dpid": dpid, **flow})
    return flows


def compare_and_report(sender_files, receiver_file, sdn_file, report_file, events_file=None):
    if isinstance(sender_files, str):
        sender_files = [sender_files]
    senders  = [_load(p) for p in sender_files]
    receiver = _load(receiver_file)
    sdn      = _load(sdn_file)
    events   = _load(events_file) if events_file and os.path.exists(events_file) else {}

    snapshots = sdn.get("snapshots", [])
    ing = _ingress_totals(snapshots, sdn.get("ingress_port_map", INGRESS_PORTS))
    sdn_mbps = round((ing["bytes"] * 8) / ing["window_s"] / 1e6, 4) if ing["window_s"] > 0 else None
    sdn_active_mbps = (round((ing["active_bytes"] * 8) / ing["active_window_s"] / 1e6, 4)
                       if ing["active_window_s"] > 0 else None)

    # Flow entries: the last snapshot alone can be empty (e.g. flows were just
    # flushed by a link failure), so also report the peak and a timeline.
    measurement_flows = _measurement_flows(sdn.get("last_flow_stats", {}))
    timeline, peak = [], 0
    t0 = float(snapshots[0]["time"]) if snapshots else 0.0
    for snap in snapshots:
        n = len(_measurement_flows(snap.get("flow_stats", {})))
        peak = max(peak, n)
        timeline.append({"t_s": round(float(snap["time"]) - t0, 1), "entries": n})

    # ---- per-flow (per sender IP) statistics -------------------------------
    rx_senders = receiver.get("per_sender", {})
    per_flow, matched = {}, set()
    tot_sent = tot_unique = tot_sender_bytes = 0
    for s in senders:
        ip   = s.get("src_ip") or s.get("sender_id") or "unknown"
        r    = rx_senders.get(ip, {})
        matched.add(ip)
        sent   = int(s.get("packets_sent", 0))
        unique = int(r.get("unique_packets_received", 0))
        lost   = max(0, sent - unique)
        tot_sent   += sent
        tot_unique += unique
        per_flow[ip] = {
            "sender_id":                s.get("sender_id"),
            "packets_sent":             sent,
            "unique_packets_received":  unique,
            "total_packets_received":   int(r.get("packets_received", 0)),
            "duplicate_packets":        int(r.get("duplicate_packets", 0)),
            "packets_lost":             lost,
            "packet_loss_pct":          round(lost / sent * 100, 2) if sent else 0.0,
            "sender_throughput_mbps":   s.get("throughput_mbps"),
            "receiver_throughput_mbps": r.get("throughput_mbps", 0.0),
            "avg_one_way_latency_ms":   r.get("avg_latency_ms", 0.0),
            "longest_gap_s":            r.get("longest_gap_s", 0.0),
            "receiver_fin_received":    r.get("fin_received", False),
            "receiver_reported_sent":   r.get("packets_sent"),
            "sender_receiver_count_match": (r.get("packets_sent") == sent) if r.get("fin_received") else None,
        }
    unmatched = sorted(set(rx_senders) - matched)

    # ---- failover evidence (failure scenario) ------------------------------
    failover = None
    t_down = events.get("link_down_time")
    if t_down:
        failover = {"link_down_time": t_down, "link_down_ok": events.get("link_down_ok")}
        for s in senders:
            ip = s.get("src_ip") or s.get("sender_id")
            r  = rx_senders.get(ip)
            if not r or not r.get("last_packet_time"):
                continue
            last_rx = float(r["last_packet_time"])
            gs, ge = r.get("longest_gap_start"), r.get("longest_gap_end")
            if last_rx <= t_down + 3:
                failover[ip] = {"traffic_restored": False,
                                "note": "no packets received after the link failure"}
            elif gs and ge and float(ge) > t_down and float(gs) <= t_down + 3:
                failover[ip] = {"traffic_restored": True,
                                "outage_s": round(float(ge) - float(gs), 2),
                                "restored_after_link_down_s": round(float(ge) - t_down, 2)}
            else:
                failover[ip] = {"traffic_restored": True, "outage_s": 0.0,
                                "note": "no significant gap around the failure"}

    overall_loss = round((tot_sent - tot_unique) / tot_sent * 100, 2) if tot_sent else 0.0
    report = {
        "comparison": {
            "app_sender_throughput_mbps": round(sum(float(s.get("throughput_mbps") or 0) for s in senders), 4),
            "app_receiver_throughput_mbps": receiver.get("throughput_mbps"),
            "sdn_ingress_throughput_mbps": sdn_active_mbps if sdn_active_mbps is not None else sdn_mbps,
            "sdn_ingress_throughput_whole_window_mbps": sdn_mbps,
            "sdn_ingress_bytes_delta": ing["bytes"],
            "sdn_ingress_packets_delta": ing["packets"],
            "sdn_measurement_flow_entries": len(measurement_flows),
            "sdn_measurement_flow_entries_peak": peak,
            "app_packets_sent": tot_sent,
            "app_packets_received": receiver.get("packets_received"),
            "app_unique_packets_received": tot_unique,
            "app_packets_lost": max(0, tot_sent - tot_unique),
            "app_packet_loss_pct": overall_loss,
            "avg_one_way_latency_ms": receiver.get("avg_one_way_latency_ms"),
            "sdn_measurement_interval_s": round(ing["window_s"], 3),
            "sdn_active_interval_s": round(ing["active_window_s"], 3),
            "receiver_stop_reason": receiver.get("stop_reason"),
        },
        "per_flow": per_flow,
        "unmatched_receiver_senders": unmatched,
        "failover": failover,
        "sdn_measurement_flows": measurement_flows,
        "sdn_measurement_flow_timeline": timeline,
        "sender_raw": senders[0] if len(senders) == 1 else senders,
        "receiver_raw": receiver,
        "sdn_raw": sdn,
        "measurement_note": (
            "Packet loss per flow = (packets sent by that sender - unique packets the receiver "
            "got from that sender IP) / packets sent. SDN throughput uses RX-byte counter deltas on "
            "host-facing ingress ports (s1:1 for h1 and s3:3 for h3) over the window in which those "
            "counters were rising; the whole-collection-window figure is also given. Flow byte "
            "counters are listed separately because summing them across switches would count the "
            "same packets more than once. The last SDN snapshot can contain zero measurement flows "
            "after a link failure flushes them; see the peak value and the timeline."
        ),
    }
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    print("\n========== COMPARISON REPORT ==========")
    for key, value in report["comparison"].items():
        print(f"  {key}: {value}")
    for ip, fl in per_flow.items():
        print(f"  [flow {ip} / {fl['sender_id']}] sent={fl['packets_sent']} "
              f"unique_rx={fl['unique_packets_received']} lost={fl['packets_lost']} "
              f"loss={fl['packet_loss_pct']}% tput={fl['receiver_throughput_mbps']}Mbps "
              f"latency={fl['avg_one_way_latency_ms']}ms")
    if failover:
        print(f"  failover: {json.dumps({k: v for k, v in failover.items() if k not in ('link_down_time',)})}")
    print("========================================")
    print(f"Report saved -> {report_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    collect = sub.add_parser("collect")
    collect.add_argument("--duration", type=float, default=20.0)
    collect.add_argument("--interval", type=float, default=2.0)
    collect.add_argument("--out", default="/tmp/sdn_stats.json")
    compare = sub.add_parser("compare")
    compare.add_argument("--sender", action="append",
                         help="sender stats JSON; repeat for each sender (h1, h3, ...)")
    compare.add_argument("--receiver", default="/tmp/receiver_stats.json")
    compare.add_argument("--sdn", default="/tmp/sdn_stats.json")
    compare.add_argument("--report", default="/tmp/comparison_report.json")
    compare.add_argument("--events", default=None,
                         help="optional events JSON (link_down_time) written by topo.py")
    args = parser.parse_args()
    if args.cmd == "collect":
        collect_sdn_stats(args.duration, args.interval, args.out)
    elif args.cmd == "compare":
        compare_and_report(args.sender or ["/tmp/sender_stats.json"], args.receiver,
                           args.sdn, args.report, args.events)
    else:
        parser.print_help()
