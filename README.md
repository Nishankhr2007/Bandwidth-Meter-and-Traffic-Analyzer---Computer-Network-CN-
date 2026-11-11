# Bandwidth Meter and Traffic Analyzer
### CN Mini Project — PES University | UE25CS243A

Project Contributors:
PES2UG25CS339 [Nishank H R]
PES2UG25CS366 []
PES2UG25CS816 []


> UDP-based network traffic measurement with SDN-controlled topology, real-time flow statistics, and automatic failover using POX and Mininet.

---

## What This Project Does

This project measures network bandwidth at two levels simultaneously:

- **Application level** — UDP sockets count packets, measure throughput, detect packet loss, and calculate latency
- **Network level** — The POX SDN controller collects OpenFlow flow statistics from each switch via REST API

The two measurements are compared side by side to show how app-level and network-level observations differ, and why.

---

## Network Topology

```
        h1 (10.0.0.1)
              |
             s1
            /  \
           s2   s3 ──── h3 (10.0.0.3)
            \  /
             s4
              |
             s5
              |
        h2 (10.0.0.2)

        POX SDN Controller
        REST API → port 8080
```

| Node | Role |
|------|------|
| h1 | UDP Sender — generates 600 pps test traffic |
| h2 | UDP Receiver — measures throughput, loss, latency |
| h3 | Congestion source — active in congested scenario |
| s1 | Root switch — splits traffic to primary (s2) or backup (s3) path |
| s2 | Primary path switch |
| s3 | Backup path switch — activated by STP when s2 fails |
| s4 | Convergence switch — both paths meet here |
| s5 | Final switch before h2 |

**Two redundant paths:**
- Primary: `h1 → s1 → s2 → s4 → s5 → h2`
- Backup: `h1 → s1 → s3 → s4 → s5 → h2`

STP (Spanning Tree Protocol) handles the loop and activates the backup path automatically on failure.

---

## Tech Stack

| Technology | Purpose |
|------------|---------|
| Python 3 | UDP socket programming — sender, receiver, stats collector |
| Mininet | Network emulator — creates virtual hosts and switches |
| Open vSwitch | Software switch with OpenFlow support |
| POX 0.7.0 | SDN controller — manages all switches, exposes REST API |
| OpenFlow 1.0 | Protocol between POX and switches |
| STP | Handles redundant loop paths, enables automatic failover |

---

## Project Structure

```
bw_loop/
├── app/
│   ├── udp_sender.py          # Generates UDP test traffic
│   ├── udp_receiver.py        # Receives packets, measures metrics
│   └── sdn_stats_collector.py # Polls POX REST API, generates report
├── controller/
│   └── bandwidth_controller.py # POX SDN controller
├── topology/
│   └── topo.py                # Mininet 5-switch topology
└── README.md
```

---

## Test Scenarios

| Scenario | What Happens | Expected Result |
|----------|-------------|-----------------|
| `normal` | h1 sends 600 pps to h2 | ~4-5 Mbps, 0% loss |
| `congested` | h3 also sends 500 pps to h2 | Higher load, increased packets at h2 |
| `failure` | s1-s2 link goes down at t=7s | Brief loss spike, traffic reroutes via s3 |

---

## SDN Features

- **Reactive L2 forwarding** — switches learn MAC addresses on first packet
- **QoS** — UDP test traffic (port 5005) gets priority 20 vs priority 10 for all other traffic
- **Bandwidth monitoring** — port stats polled every 2 seconds, alert if > 8 Mbps
- **Automatic rerouting** — on link failure, all flows cleared and re-learned via alternate path
- **Dynamic blocking** — if receiver detects > 30% packet loss from any sender, it POSTs to `/alert` and POX installs a DROP rule on all 5 switches

### REST API (port 8080)

```
GET  /stats       → port bandwidth per switch
GET  /flows       → active flow rules
GET  /port_stats  → TX/RX counters per port
GET  /topology    → connected switch count
POST /alert       → app triggers DROP rule for a source IP
```

---

## Setup

### Requirements
- Ubuntu 20.04 / 22.04
- Python 3.x
- Mininet + Open vSwitch
- POX Controller

### Install
```bash
sudo apt install mininet -y
sudo apt install python3-requests -y
cd ~ && git clone https://github.com/noxrepo/pox
cp controller/bandwidth_controller.py ~/pox/ext/
```

---

## How to Run

**Terminal 1 — start POX:**
```bash
sudo fuser -k 6633/tcp 2>/dev/null; sudo fuser -k 8080/tcp 2>/dev/null; sudo mn -c
cd ~/pox && python3 pox.py bandwidth_controller openflow.of_01 --port=6633 2>&1 | grep -v "packet:(dns)"
```

**Terminal 2 — run scenario:**
```bash
# Normal
sudo python3 topology/topo.py normal

# Congested
sudo mn -c && sudo python3 topology/topo.py congested

# Failure (link goes down at t=7s, auto-reroutes)
sudo mn -c && sudo python3 topology/topo.py failure
```

> The topology prints **"Waiting 35s for STP to converge"** — this is normal. STP needs time to handle the redundant loop.

---

## Mininet CLI Commands

```
mininet> h1 ping h2 -c 4   # test connectivity
mininet> net                # view connections
mininet> dump               # node details
mininet> exit               # quit
```

## Check REST API (while test is running)
```bash
curl http://127.0.0.1:8080/stats
curl http://127.0.0.1:8080/flows
curl http://127.0.0.1:8080/topology
```

## View Results
```bash
cat /tmp/bw_results/comparison_normal.json
cat /tmp/bw_results/comparison_congested.json
cat /tmp/bw_results/comparison_failure.json
```

---

## Metrics Measured

| Metric | Level | How |
--------------------------------
| Throughput (Mbps) | App | bytes × 8 ÷ time |
| Packets sent | App | Counter in sender |
| Packets received | App | Counter in receiver |
| Packet loss % | App | Sequence number gaps |
| One-way latency | App | Timestamp in each packet |
| SDN inferred throughput | Network | Port byte counters ÷ time |
| Active flow count | Network | Flow stats from POX |

---

## References
- [Mininet](http://mininet.org)
- [POX Controller](https://github.com/noxrepo/pox)
- [OpenFlow 1.0](https://opennetworking.org)
- [Open vSwitch](https://www.openvswitch.org)
