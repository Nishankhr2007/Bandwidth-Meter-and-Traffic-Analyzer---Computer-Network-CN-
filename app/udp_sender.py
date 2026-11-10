#!/usr/bin/env python3
"""
UDP Sender - Bandwidth Meter and Traffic Analyzer (Project 26)
Generates UDP measurement traffic with sequence numbers and timestamps.

When the transmission ends, the sender sends a small end-of-transmission (FIN)
datagram, repeated a few times because UDP is unreliable. It carries:
    sender_id, final_sequence_number, total_packets_sent
so the receiver can compute exact packet loss and know the sender has finished.
"""

import socket, time, argparse, json, struct

PACKET_SIZE  = 1024
HEADER_SIZE  = 12    # seq(4 bytes) + timestamp(8 bytes)
PAYLOAD_SIZE = PACKET_SIZE - HEADER_SIZE

FIN_MAGIC    = b"BWFIN1"   # a data packet starts with a 4-byte seq, never this
FIN_REPEATS  = 5           # FIN is repeated in case some copies are dropped
FIN_SPACING  = 0.2         # seconds between FIN copies


def build_packet(seq):
    header  = struct.pack("!Id", seq, time.time())
    payload = b"X" * PAYLOAD_SIZE
    return header + payload


def build_fin(sender_id, final_seq, total_sent):
    body = json.dumps({
        "sender_id":            sender_id,
        "final_sequence_number": final_seq,
        "total_packets_sent":   total_sent,
    }).encode()
    return FIN_MAGIC + body          # always much smaller than PACKET_SIZE


def local_ip_for(dest_ip, dest_port):
    """IP address this host uses to reach dest (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((dest_ip, dest_port))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def send_traffic(dest_ip, dest_port, duration, rate_pps, output_file, sender_id=None):
    sock     = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    src_ip   = local_ip_for(dest_ip, dest_port)
    sender_id = sender_id or src_ip or "unknown"
    interval = 1.0 / rate_pps
    sent     = 0
    bytes_sent = 0
    seq      = 0                 # sequence number advances only for successful sends
    attempts = 0                 # pacing advances even if a local send fails
    errors   = 0
    start    = time.time()

    print(f"[SENDER] {sender_id} ({src_ip}) -> {dest_ip}:{dest_port} | rate={rate_pps} pps | duration={duration}s")

    while time.time() - start < duration:
        pkt = build_packet(seq)
        try:
            sock.sendto(pkt, (dest_ip, dest_port))
            sent += 1
            bytes_sent += len(pkt)
            seq += 1
        except OSError:
            errors += 1
        attempts += 1
        next_send = start + attempts * interval
        now = time.time()
        if next_send > now:
            time.sleep(next_send - now)

    elapsed = time.time() - start          # measured before the FIN, so FIN does not skew rates
    final_seq = seq - 1                    # last sequence number used (-1 if nothing was sent)

    # End-of-transmission notification (not counted in packets_sent / bytes_sent)
    fin = build_fin(sender_id, final_seq, sent)
    for i in range(FIN_REPEATS):
        try:
            sock.sendto(fin, (dest_ip, dest_port))
        except OSError:
            pass
        if i < FIN_REPEATS - 1:
            time.sleep(FIN_SPACING)
    sock.close()

    stats = {
        "role":             "sender",
        "sender_id":        sender_id,
        "src_ip":           src_ip,
        "dest":             f"{dest_ip}:{dest_port}",
        "duration_s":       round(elapsed, 3),
        "packets_sent":     sent,
        "final_sequence_number": final_seq,
        "bytes_sent":       bytes_sent,
        "target_rate_pps":  rate_pps,
        "actual_rate_pps":  round(sent / elapsed, 2),
        "throughput_mbps":  round((bytes_sent * 8) / elapsed / 1e6, 4),
        "send_errors":      errors,
        "start_time":       start,
        "end_time":         start + elapsed,
    }

    print("\n[SENDER] ---- Results ----")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    with open(output_file, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"[SENDER] Stats saved -> {output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest",     default="10.0.0.2")
    parser.add_argument("--port",     type=int, default=5005)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--rate",     type=int, default=500)
    parser.add_argument("--out",      default="/tmp/sender_stats.json")
    parser.add_argument("--sender-id", default=None,
                        help="label sent in the end-of-transmission message (default: source IP)")
    args = parser.parse_args()
    send_traffic(args.dest, args.port, args.duration, args.rate, args.out, args.sender_id)
