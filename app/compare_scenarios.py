#!/usr/bin/env python3
"""Compare the static congestion baseline with the dynamic SDN mitigation run."""
import argparse
import json
from pathlib import Path


def load_report(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("comparison", {}), data.get("per_flow", {})


def pct_change(before, after):
    if before in (None, 0) or after is None:
        return None
    return round((after - before) / before * 100.0, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="/tmp/bw_results/comparison_baseline.json")
    parser.add_argument("--dynamic", default="/tmp/bw_results/comparison_congested.json")
    parser.add_argument("--out", default="/tmp/bw_results/static_dynamic_comparison.json")
    args = parser.parse_args()

    base, base_flows = load_report(args.baseline)
    dyn, dyn_flows = load_report(args.dynamic)
    metrics = [
        ("Application receiver throughput (Mbps)", "app_receiver_throughput_mbps"),
        ("SDN ingress throughput (Mbps)", "sdn_ingress_throughput_mbps"),
        ("Packet loss (%)", "app_packet_loss_pct"),
        ("Average one-way latency (ms)", "avg_one_way_latency_ms"),
        ("Packets received", "app_unique_packets_received"),
    ]
    rows = []
    for label, key in metrics:
        before, after = base.get(key), dyn.get(key)
        rows.append({"metric": label, "static_baseline": before,
                     "dynamic_sdn": after, "change_pct": pct_change(before, after)})

    result = {
        "experiment": "Static congestion baseline vs dynamic SDN mitigation",
        "static_baseline": args.baseline,
        "dynamic_sdn": args.dynamic,
        "baseline_per_flow": base_flows,
        "dynamic_per_flow": dyn_flows,
        "metrics": rows,
        "dynamic_controller_action_evidence": {
            "baseline_alerts_disabled": True,
            "dynamic_receiver_alerts_enabled": True,
            "dynamic_packet_loss_pct": dyn.get("app_packet_loss_pct"),
            "dynamic_blocked_source_expected": "10.0.0.1 when controller log confirms DROP installation",
        },
        "interpretation_note": (
            "Run both experiments with the same topology, link shaping, sender rates, "
            "packet size and duration. The baseline disables receiver-to-controller "
            "alerts; the dynamic run enables the alert and controller DROP response. "
            "Use controller logs to verify the actual DROP installation."
        ),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print("\n===== STATIC vs DYNAMIC SDN COMPARISON =====")
    print(f"{'Metric':42} {'Static':>12} {'Dynamic':>12} {'Change':>12}")
    for row in rows:
        change = "n/a" if row["change_pct"] is None else f"{row['change_pct']:+.2f}%"
        print(f"{row['metric']:42} {str(row['static_baseline']):>12} "
              f"{str(row['dynamic_sdn']):>12} {change:>12}")
    print(f"Saved -> {args.out}")


if __name__ == "__main__":
    main()
