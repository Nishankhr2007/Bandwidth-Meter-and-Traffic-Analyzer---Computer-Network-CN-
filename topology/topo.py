#!/usr/bin/env python3
r"""
Mininet Topology - Bandwidth Meter and Traffic Analyzer (Project 26)

Topology:
         h1 (10.0.0.1)
               |
              s1
             /  \
            s2   s3 --- h3 (10.0.0.3)
             \  /
              s4
               |
              s5
               |
         h2 (10.0.0.2)

Paths:
  Primary : h1 - s1 - s2 - s4 - s5 - h2
  Backup  : h1 - s1 - s3 - s4 - s5 - h2

STP handles the loop between s2 and s3.
On failure, STP unblocks the backup path automatically.

Scenarios:
  normal    - clean UDP traffic h1 -> h2
  baseline  - two competing UDP senders; measurements only, no SDN alert/action
  congested - same load as baseline; receiver alerts POX and POX blocks high-loss source
  failure   - s1-s2 link goes down at t=10s of a 60s run, traffic reroutes via s3
"""

from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.link import Link
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import time, os, sys, json, threading, subprocess, urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR      = os.path.join(PROJECT_ROOT, "app")

BW         = 10
DELAY      = "5ms"
CTRL_IP    = "127.0.0.1"
CTRL_PORT  = 6633
TEST_PORT  = 5005
RESULT_DIR = "/tmp/bw_results"

# Failure scenario timing. With default STP timers (15 s forward delay) the
# backup path needs roughly 30-50 s to start forwarding after the link goes
# down, so the failure run is longer than the others to leave time to observe it.
FAILURE_DURATION = 60
FAILURE_AT       = 10
RECEIVER_GRACE   = 25   # bounded wait after senders finish; receiver has its own runtime limit
REST_API_URL     = "http://10.0.0.254:8080"
EXPECTED_SWITCHES = 5


def apply_link_shaping(net):
    """Apply and verify symmetric 10-Mbit/s, 5-ms netem shaping on every link end.

    Use plain Mininet links and one explicit netem qdisc per interface. This avoids
    TCLink's nested HTB/netem setup, which produced qdisc-not-found warnings on
    some of the target Ubuntu/WSL kernels.
    """
    subprocess.run(["modprobe", "sch_netem"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
    configured = []
    for link in net.links:
        for intf in (link.intf1, link.intf2):
            cmd = (f"tc qdisc replace dev {intf.name} root netem "
                   f"rate {BW}mbit delay {DELAY} limit 10000")
            result = intf.node.cmd(cmd + " 2>&1; echo __TC_RC:$?")
            if "__TC_RC:0" not in result:
                raise RuntimeError(
                    f"Could not configure bandwidth shaping on {intf.name}: {result.strip()}"
                )
            verify = intf.node.cmd(f"tc qdisc show dev {intf.name}")
            if "netem" not in verify or "rate" not in verify:
                raise RuntimeError(
                    f"Traffic shaping verification failed on {intf.name}: {verify.strip()}"
                )
            configured.append(intf.name)
    info(f"*** Verified {BW} Mbit/s and {DELAY} netem shaping on "
         f"{len(configured)} interfaces\n")


def run(scenario="normal"):
    os.makedirs(RESULT_DIR, exist_ok=True)
    setLogLevel("info")
    duration = FAILURE_DURATION if scenario == "failure" else 15

    net = Mininet(
        controller=RemoteController,
        switch=OVSKernelSwitch,
        link=Link,
        autoSetMacs=True,
    )

    info("*** Adding remote controller\n")
    c0 = net.addController("c0", controller=RemoteController,
                           ip=CTRL_IP, port=CTRL_PORT)

    info("*** Adding hosts\n")
    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")

    info("*** Adding 5 switches\n")
    s1 = net.addSwitch("s1", cls=OVSKernelSwitch, protocols="OpenFlow10")
    s2 = net.addSwitch("s2", cls=OVSKernelSwitch, protocols="OpenFlow10")
    s3 = net.addSwitch("s3", cls=OVSKernelSwitch, protocols="OpenFlow10")
    s4 = net.addSwitch("s4", cls=OVSKernelSwitch, protocols="OpenFlow10")
    s5 = net.addSwitch("s5", cls=OVSKernelSwitch, protocols="OpenFlow10")

    info("*** Adding links (with redundant loop paths)\n")
    net.addLink(h1, s1)
    net.addLink(s1, s2)   # primary path
    net.addLink(s1, s3)   # backup path
    net.addLink(s2, s4)
    net.addLink(s3, s4)
    net.addLink(s4, s5)
    net.addLink(s5, h2)
    net.addLink(h3, s3)

    info("*** Starting network\n")
    net.build()
    apply_link_shaping(net)
    c0.start()
    for sw in [s1, s2, s3, s4, s5]:
        sw.start([c0])

    # Give the root-namespace s1 bridge a reachable in-band address for the
    # receiver inside h2 to reach POX. The bridge's local interface is DOWN by
    # default on some OVS/Mininet versions, so explicitly bring it UP.
    s1.cmd("ip addr replace 10.0.0.254/24 dev s1")
    s1.cmd("ip link set dev s1 up")

    # Enable STP on all switches to handle loops
    info("*** Enabling STP on all switches to handle redundant paths...\n")
    for sw in [s1, s2, s3, s4, s5]:
        sw.cmd(f"ovs-vsctl set bridge {sw.name} stp-enable=true")
    # Make s1 the deterministic STP root so s1-s2 is the primary branch.
    s1.cmd("ovs-vsctl set bridge s1 other_config:stp-priority=4096")

    info("*** Waiting 35s for STP to converge (this is normal)...\n")
    time.sleep(35)

    info("*** Testing connectivity\n")
    result = net.ping([h1, h2], timeout=3)
    if result > 0.0:
        info("*** Waiting 10 more seconds for STP / ARP learning...\n")
        time.sleep(10)
        result = net.ping([h1, h2], timeout=3)
    if result > 0.0:
        net.stop()
        raise RuntimeError("Host-to-host connectivity failed after STP convergence")

    # Verify the exact REST route used by the receiver, from h2's namespace.
    # Also require all five switches to be connected before running traffic.
    info("*** Verifying h2 -> POX REST API connectivity...\n")
    probe = (
        "python3 -c 'import urllib.request,json; "
        f"d=json.load(urllib.request.urlopen(\"{REST_API_URL}/topology\", timeout=2)); "
        "print(d.get(\"connected_switches\", 0))' 2>/dev/null"
    )
    connected = 0
    deadline = time.time() + 15
    while time.time() < deadline:
        output = h2.cmd(probe).strip()
        try:
            connected = int(output.splitlines()[-1])
        except (ValueError, IndexError):
            connected = 0
        if connected >= EXPECTED_SWITCHES:
            break
        time.sleep(1)
    if connected < EXPECTED_SWITCHES:
        info("*** ERROR: h2 cannot reach POX REST API or not all switches are connected.\n")
        info("*** Check POX terminal, port 8080, s1 bridge IP, and OVS connections.\n")
        net.stop()
        raise RuntimeError(f"POX REST health check failed (connected switches: {connected}/{EXPECTED_SWITCHES})")
    info(f"*** POX REST API reachable from h2; {connected}/{EXPECTED_SWITCHES} switches connected.\n")

    sender_out   = f"{RESULT_DIR}/sender_stats_{scenario}.json"
    receiver_out = f"{RESULT_DIR}/receiver_stats_{scenario}.json"
    receiver_log = f"{RESULT_DIR}/receiver_{scenario}.log"
    sdn_out      = f"{RESULT_DIR}/sdn_stats_{scenario}.json"
    report_out   = f"{RESULT_DIR}/comparison_{scenario}.json"
    h3_out       = f"{RESULT_DIR}/h3_sender_{scenario}.json"
    events_out   = f"{RESULT_DIR}/events_{scenario}.json"

    # Remove results of an earlier run so the report can never read stale files.
    for f in (sender_out, receiver_out, receiver_log, sdn_out, report_out, h3_out, events_out):
        if os.path.exists(f):
            os.remove(f)

    info(f"\n{'='*50}\n  SCENARIO: {scenario.upper()}\n{'='*50}\n")

    # 1. Start receiver on h2 FIRST. It keeps listening through outages and
    #    stops when every expected sender has sent its FIN message.
    expect_senders = 2 if scenario in ("baseline", "congested") else 1
    info("*** Starting UDP receiver on h2\n")
    receiver_log_handle = open(receiver_log, "w", buffering=1)
    h2_recv = h2.popen(
        f"python3 {APP_DIR}/udp_receiver.py --port {TEST_PORT} --out {receiver_out} "
        f"--expect-senders {expect_senders} --max-runtime {duration + 8}"
        + (" --disable-sdn-alert" if scenario == "baseline" else ""),
        stdout=receiver_log_handle, stderr=subprocess.STDOUT,
    )
    time.sleep(1)

    # 2. Start SDN stats collector (waited for before the report is built)
    info("*** Starting SDN stats collector\n")
    collector = subprocess.Popen([
        sys.executable, f"{APP_DIR}/sdn_stats_collector.py", "collect",
        "--duration", str(duration + 4), "--interval", "1", "--out", sdn_out,
    ])

    # Congested: h3 sends background traffic
    h3_proc = None
    sender_files = [sender_out]
    if scenario in ("baseline", "congested"):
        info("*** h3 sending concurrent UDP traffic at 900 pps\n")
        h3_proc = h3.popen(
            f"python3 {APP_DIR}/udp_sender.py "
            f"--dest 10.0.0.2 --port {TEST_PORT} --duration {duration} "
            f"--rate 900 --sender-id h3 --out {h3_out}"
        )
        sender_files.append(h3_out)
        time.sleep(0.5)

    # Failure: bring down s1-s2 link at t=FAILURE_AT
    events = {"scenario": scenario}
    if scenario == "failure":
        info(f"*** Link failure will occur at t={FAILURE_AT}s\n")
        def bring_down():
            time.sleep(FAILURE_AT)
            info("\n*** BRINGING DOWN s1-s2 LINK\n")
            events["link_down_time"] = time.time()
            ok = True
            for iface in ("s1-eth2", "s2-eth1"):
                if subprocess.call(["ip", "link", "set", iface, "down"]) != 0:
                    ok = False
            events["link_down_ok"] = ok
            if not ok:
                info("*** WARNING: could not bring the link down (run topo.py with sudo)\n")
            with open(events_out, "w") as ef:
                json.dump(events, ef, indent=2)
            info("*** STP will now activate backup path via s3\n")
            # The one-way UDP stream never makes h2 talk, so switches cannot
            # re-learn h2 on the new path. A light periodic ping from h2 (like
            # ARP refresh in a real network) lets them learn it again.
            while not events.get("stop"):
                h2.cmd("ping -c 1 -W 1 10.0.0.1 > /dev/null 2>&1")
                time.sleep(1)
        threading.Thread(target=bring_down, daemon=True).start()

    # 3. Start sender on h1 (blocks until its FIN messages are sent)
    h1_rate = 900 if scenario in ("baseline", "congested") else 600
    info(f"*** h1 sending UDP to h2 ({h1_rate} pps, {duration}s)\n")
    h1.cmd(
        f"python3 {APP_DIR}/udp_sender.py "
        f"--dest 10.0.0.2 --port {TEST_PORT} "
        f"--duration {duration} --rate {h1_rate} --sender-id h1 --out {sender_out}"
    )
    if h3_proc is not None:
        h3_proc.wait()
    events["stop"] = True

    # 4. Give the receiver time to process the FIN messages, else stop it
    #    explicitly (SIGTERM makes it write its statistics before exiting).
    info("*** Waiting for receiver to finish...\n")
    try:
        h2_recv.wait(timeout=RECEIVER_GRACE)
    except subprocess.TimeoutExpired:
        info("*** Receiver did not finish in time - terminating it explicitly\\n")
        h2_recv.terminate()
        try:
            h2_recv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            h2_recv.kill()
    finally:
        receiver_log_handle.close()

    info("*** Receiver alert log (if any)\\n")
    if os.path.exists(receiver_log):
        with open(receiver_log, "r", errors="replace") as rf:
            for line in rf:
                if "alert" in line.lower() or "controller" in line.lower():
                    info(f"  {line.rstrip()}\\n")
    info(f"*** Full receiver log: {receiver_log}\\n")

    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/stats", timeout=3) as response:
            controller_state = json.loads(response.read().decode())
        info(f"*** Controller blocked_ips: {controller_state.get('blocked_ips', [])}\\n")
    except Exception as exc:
        info(f"*** Could not read controller state: {exc}\\n")

    # 5. Wait for the SDN collector so its JSON is complete before reporting
    info("*** Waiting for SDN collector to finish...\n")
    try:
        collector.wait(timeout=15)
    except subprocess.TimeoutExpired:
        collector.terminate()

    # Generate comparison report
    info("*** Generating comparison report\n")
    cmd = [sys.executable, f"{APP_DIR}/sdn_stats_collector.py", "compare",
           "--receiver", receiver_out, "--sdn", sdn_out, "--report", report_out,
           "--events", events_out]
    for sf in sender_files:
        cmd += ["--sender", sf]
    if all(os.path.exists(f) for f in sender_files + [receiver_out, sdn_out]):
        subprocess.call(cmd)
    else:
        info("*** Report skipped: a results file is missing (see messages above)\n")

    try:
        with open(report_out) as f:
            rpt = json.load(f)
        info("\n===== COMPARISON REPORT =====\n")
        for k, v in rpt.get("comparison", {}).items():
            info(f"  {k}: {v}\n")
        for ip, fl in rpt.get("per_flow", {}).items():
            info(f"  flow {ip} ({fl['sender_id']}): sent={fl['packets_sent']} "
                 f"unique_rx={fl['unique_packets_received']} lost={fl['packets_lost']} "
                 f"loss={fl['packet_loss_pct']}% tput={fl['receiver_throughput_mbps']}Mbps\n")
        if rpt.get("failover"):
            info(f"  failover: {json.dumps(rpt['failover'])}\n")
        info("=============================\n")
    except Exception:
        pass

    info("\n*** CLI — type 'exit' when done\n")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else "normal"
    if scenario not in ("normal", "baseline", "congested", "failure"):
        print("Usage: sudo python3 topo.py [normal|baseline|congested|failure]")
        sys.exit(1)
    run(scenario)
