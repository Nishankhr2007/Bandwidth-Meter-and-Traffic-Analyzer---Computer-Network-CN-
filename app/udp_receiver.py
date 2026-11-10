#!/usr/bin/env python3
"""
UDP Receiver - Bandwidth Meter and Traffic Analyzer (Project 26)
Receives UDP packets, measures throughput, loss, and latency.
If packet loss from any sender exceeds 30%, alerts the SDN controller.

The receiver keeps listening through temporary interruptions (e.g. a link
failure). It stops only when
  * every expected sender has sent its end-of-transmission (FIN) message, or
  * it is terminated (SIGINT / SIGTERM), or
  * the optional --idle-timeout elapses with no traffic at all.

Packet loss is computed per sender as
    (packets_sent - unique_packets_received) / packets_sent * 100
where packets_sent comes from the sender's FIN message.
"""

import socket, time, argparse, json, struct, signal, threading
import urllib.request

PACKET_SIZE     = 1024
HEADER_SIZE     = 12
FIN_MAGIC       = b"BWFIN1"          # must match udp_sender.py
LOSS_ALERT_PCT  = 30.0
CONTROLLER_URL  = "http://10.0.0.254:8080/alert"

# Live congestion detection (the sender's final count is not known yet while
# traffic is still flowing, so loss is estimated per time window from gaps in
# the sequence numbers).
ALERT_WINDOW_S   = 2.0    # evaluate loss over windows of this length
ALERT_MIN_PKTS   = 100    # ignore windows that expected fewer packets than this
OUTAGE_GAP_S     = 1.0    # silence longer than this = outage, not congestion
POLL_S           = 0.25   # socket timeout used to poll the stop conditions
ALERT_RETRIES     = 5
ALERT_TIMEOUT_S   = 3
ALERT_RETRY_WAIT  = 1.0


def parse_header(data):
    if len(data) < HEADER_SIZE:
        return None, None
    seq, ts = struct.unpack_from("!Id", data, 0)
    return seq, ts


def parse_fin(data):
    try:
        return json.loads(data[len(FIN_MAGIC):].decode())
    except Exception:
        return {}


def post_alert(src_ip, loss_pct, controller_url=CONTROLLER_URL):
    """Notify POX controller, retrying transient failures."""
    payload = json.dumps({"src_ip": src_ip, "loss_pct": round(loss_pct, 2)}).encode()
    for attempt in range(1, ALERT_RETRIES + 1):
        req = urllib.request.Request(
            controller_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=ALERT_TIMEOUT_S) as response:
                body = response.read().decode()
                print(f"[RECEIVER] SDN alert sent for {src_ip} "
                      f"(attempt {attempt}/{ALERT_RETRIES}): {body}", flush=True)
                return True
        except Exception as exc:
            print(f"[RECEIVER] SDN alert attempt {attempt}/{ALERT_RETRIES} "
                  f"failed for {src_ip}: {exc}", flush=True)
            if attempt < ALERT_RETRIES:
                time.sleep(ALERT_RETRY_WAIT)
    print(f"[RECEIVER] SDN alert FAILED for {src_ip} after "
          f"{ALERT_RETRIES} attempts", flush=True)
    return False


def new_sender():
    return {
        "seq_nums": set(), "latencies": [], "bytes": 0, "count": 0,
        "first_time": None, "last_time": None,
        "max_gap": 0.0, "gap_start": None, "gap_end": None,
        "cp_time": None, "cp_max_seq": -1, "cp_unique": 0,   # alert-window checkpoint
        "max_seq": -1, "fin": None,
    }


def receive_traffic(port, output_file, expect_senders=1, drain=1.0,
                    idle_timeout=0.0, max_runtime=0.0,
                    controller_url=CONTROLLER_URL, enable_sdn_alert=True):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("", port))
    sock.settimeout(POLL_S)

    senders          = {}
    total_received   = 0
    total_bytes      = 0
    start_time       = None
    receiver_start   = time.time()
    last_packet_time = None      # last DATA packet (FIN messages do not count)
    alerted          = set()
    alerting         = set()
    alert_threads    = []
    stop_reason      = None
    stop_flag        = {"set": False}
    drain_deadline   = None

    print(f"[RECEIVER] Listening on UDP port {port} "
          f"(expect_senders={expect_senders}, drain={drain}s, idle_timeout={idle_timeout}s)")

    def handle_signal(sig, frame):
        stop_flag["set"] = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def fins_received():
        return sum(1 for d in senders.values() if d["fin"] is not None)

    def run_alert(src_ip, loss_pct):
        try:
            if post_alert(src_ip, loss_pct, controller_url):
                alerted.add(src_ip)
        finally:
            alerting.discard(src_ip)

    def check_alert(src_ip, s, now):
        """Windowed loss estimate for live congestion alerts."""
        if not enable_sdn_alert:
            return
        if s["cp_time"] is None:
            s["cp_time"] = now
            return
        if now - s["cp_time"] < ALERT_WINDOW_S:
            return
        expected = s["max_seq"] - s["cp_max_seq"]
        got      = len(s["seq_nums"]) - s["cp_unique"]
        if expected >= ALERT_MIN_PKTS:
            loss = max(0, expected - got) / expected * 100
            if loss > LOSS_ALERT_PCT and src_ip not in alerted and src_ip not in alerting:
                print(f"[RECEIVER] High loss ({loss:.1f}%) from {src_ip} — alerting SDN controller")
                alerting.add(src_ip)
                alert_thread = threading.Thread(target=run_alert, args=(src_ip, loss), daemon=True)
                alert_threads.append(alert_thread)
                alert_thread.start()
        s["cp_time"], s["cp_max_seq"], s["cp_unique"] = now, s["max_seq"], len(s["seq_nums"])

    try:
        while True:
            now = time.time()
            if stop_flag["set"]:
                stop_reason = "terminated"
                break
            if max_runtime > 0 and now - receiver_start >= max_runtime:
                stop_reason = "max_runtime"
                print(f"[RECEIVER] Maximum runtime ({max_runtime}s) reached", flush=True)
                break
            if drain_deadline is not None and now >= drain_deadline:
                stop_reason = "all_senders_finished"
                break
            if (idle_timeout > 0 and total_received > 0 and last_packet_time
                    and now - last_packet_time > idle_timeout):
                stop_reason = "idle_timeout"
                break

            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if stop_flag["set"]:
                    continue
                raise

            now    = time.time()
            src_ip = addr[0]

            # ---- end-of-transmission message ----
            if data.startswith(FIN_MAGIC) and len(data) < PACKET_SIZE:
                s = senders.setdefault(src_ip, new_sender())
                if s["fin"] is None:
                    s["fin"] = parse_fin(data)
                    print(f"[RECEIVER] FIN from {src_ip}: {s['fin']}")
                if (drain_deadline is None and expect_senders > 0
                        and fins_received() >= expect_senders):
                    drain_deadline = now + drain
                    print(f"[RECEIVER] All {expect_senders} sender(s) finished — draining {drain}s")
                continue

            seq, ts = parse_header(data)
            if seq is None:
                continue
            s = senders.setdefault(src_ip, new_sender())

            last_packet_time = now
            if start_time is None:
                start_time = now
                print(f"[RECEIVER] First packet from {src_ip}")
            if s["count"] == 0:
                print(f"[RECEIVER] New sender: {src_ip}")
                s["first_time"] = now

            # outage / gap tracking (per sender)
            outage = False
            if s["last_time"] is not None:
                gap = now - s["last_time"]
                if gap > s["max_gap"]:
                    s["max_gap"], s["gap_start"], s["gap_end"] = gap, s["last_time"], now
                outage = gap > OUTAGE_GAP_S
            s["last_time"] = now

            latency = now - ts
            s["seq_nums"].add(seq)
            s["max_seq"] = max(s["max_seq"], seq)
            if 0 < latency < 10:
                s["latencies"].append(latency)
            s["bytes"] += len(data)
            s["count"] += 1
            total_received += 1
            total_bytes    += len(data)

            if outage:
                # A link failure is not congestion: restart the alert window
                # instead of reporting the silent period as packet loss.
                s["cp_time"], s["cp_max_seq"], s["cp_unique"] = now, seq, len(s["seq_nums"])
            else:
                check_alert(src_ip, s, now)

    except Exception as e:
        print(f"[RECEIVER] Error: {e}")
        stop_reason = f"error: {e}"

    try:
        sock.close()
    except Exception:
        pass

    # Wait for alert retries to finish before writing final statistics.
    for alert_thread in alert_threads:
        alert_thread.join()

    # ---------------------------------------------------------------- #
    #  Final statistics                                                 #
    # ---------------------------------------------------------------- #
    measured_end = last_packet_time or start_time or time.time()
    elapsed = measured_end - (start_time or measured_end)
    if elapsed <= 0:
        elapsed = 1e-6

    sender_stats    = {}
    sum_sent        = 0
    sum_unique      = 0
    for ip, d in senders.items():
        unique = len(d["seq_nums"])
        fin    = d["fin"]
        if fin and "total_packets_sent" in fin:
            sent  = int(fin["total_packets_sent"])
            basis = "sender_fin"
        else:
            # No FIN (lost or blocked): best estimate from the highest sequence seen.
            sent  = (max(d["seq_nums"]) + 1) if d["seq_nums"] else 0
            basis = "estimated_from_max_seq (no FIN received - tail loss not counted)"
        lost     = max(0, sent - unique)
        loss_pct = (lost / sent * 100) if sent > 0 else 0.0
        flow_elapsed = ((d["last_time"] - d["first_time"])
                        if d["first_time"] is not None and d["last_time"] is not None else elapsed)
        flow_elapsed = max(flow_elapsed, 1e-6)
        bw_mbps  = (d["bytes"] * 8) / flow_elapsed / 1e6
        avg_lat  = (sum(d["latencies"]) / len(d["latencies"]) * 1000) if d["latencies"] else 0
        sum_sent   += sent
        sum_unique += unique
        sender_stats[ip] = {
            "sender_id":               (fin or {}).get("sender_id"),
            "fin_received":            fin is not None,
            "loss_basis":              basis,
            "packets_sent":            sent,
            "final_sequence_number":   (fin or {}).get("final_sequence_number"),
            "packets_received":        d["count"],
            "packets_expected":        sent,
            "packets_lost":            lost,
            "unique_packets_received": unique,
            "duplicate_packets":       max(0, d["count"] - unique),
            "packet_loss_pct":         round(loss_pct, 2),
            "throughput_mbps":         round(bw_mbps, 4),
            "avg_latency_ms":          round(avg_lat, 3),
            "longest_gap_s":           round(d["max_gap"], 3),
            "longest_gap_start":       d["gap_start"],
            "longest_gap_end":         d["gap_end"],
            "first_packet_time":       d["first_time"],
            "last_packet_time":        d["last_time"],
        }

    total_mbps  = round((total_bytes * 8) / elapsed / 1e6, 4)
    total_loss  = round(((sum_sent - sum_unique) / sum_sent * 100) if sum_sent > 0 else 0.0, 2)
    lat_pkts    = sum(len(d["latencies"]) for d in senders.values())
    avg_latency = round((sum(sum(d["latencies"]) for d in senders.values()) / lat_pkts * 1000)
                        if lat_pkts else 0.0, 3)

    stats = {
        "role":                    "receiver",
        "port":                    port,
        "stop_reason":             stop_reason,
        "duration_s":              round(elapsed, 3),
        "measurement_start_time":  start_time,
        "measurement_end_time":    measured_end,
        "packets_received":        total_received,
        "unique_packets_received": sum_unique,
        "packets_sent":            sum_sent,
        "bytes_received":          total_bytes,
        "throughput_mbps":         total_mbps,
        "packet_loss_pct":         total_loss,
        "avg_one_way_latency_ms":  avg_latency,
        "per_sender":              sender_stats,
    }

    print("\n[RECEIVER] ---- Results ----")
    for k, v in stats.items():
        if k != "per_sender":
            print(f"  {k}: {v}")
    for ip, s in sender_stats.items():
        print(f"  [{ip}] sent={s['packets_sent']} unique_rx={s['unique_packets_received']} "
              f"loss={s['packet_loss_pct']}% bw={s['throughput_mbps']} Mbps "
              f"latency={s['avg_latency_ms']}ms longest_gap={s['longest_gap_s']}s "
              f"({s['loss_basis']})")

    with open(output_file, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"[RECEIVER] Stats saved -> {output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--out",  default="/tmp/receiver_stats.json")
    parser.add_argument("--expect-senders", type=int, default=1,
                        help="stop after this many senders sent their FIN (0 = run until terminated)")
    parser.add_argument("--drain", type=float, default=1.0,
                        help="seconds to keep receiving after the last FIN (late packets)")
    parser.add_argument("--idle-timeout", type=float, default=0.0,
                        help="optional: stop after this many seconds without data (0 = never)")
    parser.add_argument("--max-runtime", type=float, default=0.0,
                        help="stop after this many seconds even if a blocked sender cannot deliver FIN (0 = unlimited)")
    parser.add_argument("--controller-url", default=CONTROLLER_URL,
                        help="POX REST alert endpoint reachable from this network namespace")
    parser.add_argument("--disable-sdn-alert", action="store_true",
                        help="measure traffic without sending dynamic alerts (static baseline mode)")
    args = parser.parse_args()
    receive_traffic(args.port, args.out, args.expect_senders, args.drain,
                    args.idle_timeout, args.max_runtime, args.controller_url,
                    not args.disable_sdn_alert)
