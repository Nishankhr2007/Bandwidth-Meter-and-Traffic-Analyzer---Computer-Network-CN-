# Bandwidth Meter and Traffic Analyzer (Project 26)

UDP socket-based traffic measurement with a POX/OpenFlow 1.0 controller and a Mininet topology.

## Project contents

```text
bw_loop/
├── app/
│   ├── udp_sender.py
│   ├── udp_receiver.py
│   └── sdn_stats_collector.py
├── controller/
│   └── bandwidth_controller.py
├── topology/
│   └── topo.py
├── miniedit/
│   └── bandwidth_meter_topology.mn
├── tools/
│   ├── fix_miniedit.py
│   └── launch_miniedit.sh
├── requirements.txt
└── README.md
```

## Requirements

- Ubuntu with Mininet and Open vSwitch installed
- POX controller (Python 3 compatible) with OpenFlow 1.0 support
- Python 3
- MiniEdit (included in the Mininet `examples` directory) for opening the `.mn` visual topology

Install the Python dependency:

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-tk
```

## 1. Start the POX controller

Copy the controller module into the POX `ext` directory:

```bash
cp controller/bandwidth_controller.py ~/pox/ext/bandwidth_controller.py
cd ~/pox
sudo python3 pox.py bandwidth_controller openflow.of_01 --port=6633
```

Keep this terminal open. The controller listens for OpenFlow on port `6633` and exposes its REST API on port `8080`. The topology assigns `10.0.0.254/24` to the s1 bridge interface and explicitly brings that interface UP, so the receiver inside h2 can reach the REST API at `http://10.0.0.254:8080/alert`. Before starting traffic, the topology verifies from h2 that the REST endpoint responds and all five switches are connected. `127.0.0.1` inside a Mininet host would refer to that host itself, not the controller.

## 2. Run the Mininet experiment

Open a second terminal, go to the extracted project directory, and run one scenario at a time:

```bash
cd bw_loop
sudo python3 topology/topo.py normal
```

Other scenarios:

```bash
sudo python3 topology/topo.py baseline
sudo python3 topology/topo.py congested
sudo python3 topology/topo.py failure
```

`baseline` and `congested` use the same two UDP senders (h1 and h3), packet size,
sending rates, topology, link rate and test duration. The `baseline` receiver has
SDN alerts disabled, so the controller only observes/forwards traffic and does not
install congestion DROP rules. The `congested` run enables alerts; when the receiver
detects high loss, it reports the source to POX, which installs a temporary DROP
rule. This gives a controlled static-vs-dynamic comparison. Run `baseline` first,
then `congested`, with the same POX controller running. Check the POX terminal for
`APP ALERT` and `DROP installed` messages during the dynamic run.

The script creates the five-switch, three-host topology, enables STP for the redundant loop, applies and verifies a 10-Mbit/s, 5-ms Linux `netem` qdisc on both ends of every link, starts the receiver and SDN statistics collector, runs the selected UDP traffic scenario, writes JSON results under `/tmp/bw_results`, and then opens the Mininet CLI. Type `exit` in the Mininet CLI to stop the network. The s1 bridge's in-band management address is brought UP automatically on every run.

Receiver SDN alerts retry up to 5 times (3 s timeout per attempt) if the controller REST request fails. Receiver output is logged to `/tmp/bw_results/receiver_<scenario>.log`, and the topology prints alert results plus the controller's `blocked_ips` state before the report. The congested test sends concurrent 900 packet/s streams from h1 and h3 across the shared 10 Mbps bottleneck. The failure test runs for 60 s and brings down the s1-s2 link at t=10 s while traffic is running. With default STP timers the redundant path needs roughly 30-50 s to start forwarding, so the longer run leaves time to observe whether traffic comes back. A light periodic ping from h2 runs after the failure so the switches can re-learn h2 on the new path (the UDP stream is one-way and would otherwise never teach them).

Run order inside `topo.py`: the receiver starts first and keeps listening through outages; the sender(s) then run; each sender ends with a small end-of-transmission (FIN) message carrying `sender_id`, `final_sequence_number` and `total_packets_sent`; the receiver stops once every expected sender has sent its FIN or its scenario-length maximum runtime is reached. A maximum runtime is important when the controller blocks a sender before its FIN can reach h2; the sender JSON still provides the exact transmitted count for the comparison report. Restart POX between scenarios for clean results.

After both congestion experiments finish, generate the static-vs-dynamic comparison:

```bash
python3 app/compare_scenarios.py
```

It reads `comparison_baseline.json` and `comparison_congested.json` and writes
`/tmp/bw_results/static_dynamic_comparison.json`, with application throughput,
SDN ingress throughput, packet loss, latency and received-packet comparisons.
Use the POX terminal log as evidence that the dynamic run actually installed DROP
rules; the JSON comparison does not fabricate controller-action evidence.

## 3. Display the topology in MiniEdit

The ZIP includes a MiniEdit version-2 `.mn` JSON topology file using MiniEdit's native controller, switch, host, and link option fields. It is for opening/displaying the topology canvas; use `topology/topo.py` to run the measured experiments because that script also starts the receiver, collector, scenario traffic and failure/congestion actions.

On Ubuntu 24.04 with the Mininet package, MiniEdit is commonly installed at
`/usr/lib/python3/dist-packages/mininet/examples/miniedit.py`. Its packaged
source may contain a Python 2-era `dict_items` expression that fails on Python 3.12.
The project includes a small compatibility patch that makes a backup before changing
only that expression.

From the project root, launch MiniEdit with:

```bash
sudo bash tools/launch_miniedit.sh
```

If MiniEdit is installed elsewhere, locate it and pass its path:

```bash
find /usr -name miniedit.py 2>/dev/null
sudo bash tools/launch_miniedit.sh /full/path/to/miniedit.py
```

The launcher applies the compatibility fix, then opens MiniEdit. In MiniEdit choose
**File -> Open**, then select:

```text
bw_loop/miniedit/bandwidth_meter_topology.mn
```

The canvas contains h1, h2, h3; switches s1-s5; the remote controller c0; and the same eight links as `topology/topo.py`. Link bandwidth and delay are set to 10 Mbps and 5 ms. The controller properties use `127.0.0.1:6633`.

For the experiment itself, start POX first and run `topology/topo.py` from a terminal. Do not run the MiniEdit canvas and the scripted topology simultaneously, because they would create two separate Mininet networks with duplicate switch names.

## 4. Results and comparison

Results are written to:

```text
/tmp/bw_results/
```

Files include sender, receiver, SDN snapshots and the final comparison JSON for each scenario, plus `static_dynamic_comparison.json` after running the comparison script. The SDN collector samples both `/port_stats` and `/flows`. Its throughput calculation uses RX-byte counter deltas at host-facing ingress ports (s1 port 1 for h1 and s3 port 3 for h3), divided by the actual SDN sampling interval. Flow counters are reported separately because summing the same flow across multiple switches would count packets more than once.

Packet loss is computed **per sender** as `(packets sent - unique packets received) / packets sent x 100`, using the sender's own packet count (also carried in its FIN message). Duplicate datagrams do not reduce the loss figure, and packets lost at the start or end of a run are counted. If a FIN never reaches the receiver (for example, the source was blocked by the controller), the receiver falls back to an estimate from the highest sequence number and labels it `estimated_from_max_seq`; the comparison report always uses the sender's own count instead.

The receiver measures the data interval from the first to the last data packet. Per-flow results in `comparison_<scenario>.json` (`per_flow`) are keyed by sender IP, so h1 and h3 are never mixed. The report also lists packets sent, unique received, lost, loss %, throughput, latency, the longest receive gap per flow, the SDN flow-entry count (last snapshot, peak, and a timeline - the last snapshot can legitimately be empty right after a link failure flushes the flows) and, for the failure scenario, whether traffic was restored and after how long. Live congestion alerts use loss measured over 2-second windows; a silence longer than 1 s is treated as an outage, not congestion, so a link failure does not trigger a block.

## 5. Useful controller REST endpoints

```bash
curl http://127.0.0.1:8080/topology
curl http://127.0.0.1:8080/stats
curl http://127.0.0.1:8080/port_stats
curl http://127.0.0.1:8080/flows
```

The receiver can POST a high-loss alert to `/alert`; the controller then installs a temporary high-priority drop rule for the reporting source IP.

## 6. Notes for reproducing the demo

- Start the controller before starting Mininet.
- Run one scenario at a time and type `exit` in the Mininet CLI before starting another. For the static-vs-dynamic evaluation, run `baseline` first and `congested` second, then run `python3 app/compare_scenarios.py`.
- Run Mininet and MiniEdit with the privileges required by your Linux installation.
- The complete Mininet + POX execution must be verified on the target Ubuntu/Mininet/POX installation; syntax and local UDP tests do not substitute for a live network test.
