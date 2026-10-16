#!/usr/bin/env python3
"""Observe kubelet recovery from a stopped checkout process in an explicit lab run.

Requires Linux/Python 3.10+ on the master and worker, containerd/crictl on the
worker, and verified SSH access with passwordless sudo. No Pods are deleted.
Only the identified checkout container process receives SIGSTOP/SIGCONT.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time


# Executed on the worker through sudo python3 with this source on stdin. A pidfd
# pins the process identity even if the kernel later reuses its numeric PID.
WORKER_HELPER = r'''
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

action, endpoint, container_id, namespace, pod, uid = sys.argv[1:7]

def inspect():
    result = subprocess.run(
        ["crictl", "--runtime-endpoint", endpoint, "inspect", container_id],
        check=True, capture_output=True, text=True, timeout=10)
    data = json.loads(result.stdout)
    status = data["status"]
    labels = status["labels"]
    assert status["id"] == container_id, "Runtime container ID changed"
    assert status["metadata"]["name"] == "checkoutservice", "Unexpected container"
    assert labels["io.kubernetes.pod.namespace"] == namespace, "Namespace mismatch"
    assert labels["io.kubernetes.pod.name"] == pod, "Pod name mismatch"
    assert labels["io.kubernetes.pod.uid"] == uid, "Pod UID mismatch"
    return data

def start_time(pid):
    # /proc stat field 22; the comm field may contain spaces and parentheses.
    return Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()[19]

data = inspect()
status = data["status"]
if action == "resume" and status["state"] != "CONTAINER_RUNNING":
    print(json.dumps({"action": "original-container-exited"}))
    sys.exit(0)
assert status["state"] == "CONTAINER_RUNNING", "Container is not running"
pid = int(data["info"]["pid"])
assert pid > 1, "Refusing to signal host init"
started = start_time(pid)
if action in ("inspect", "stop"):
    process_state = Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
    assert process_state not in ("T", "t", "Z", "X"), "Process is already stopped or exited"
if action == "resume":
    assert pid == int(sys.argv[7]) and started == sys.argv[8], "Process identity changed"
fd = os.pidfd_open(pid)
assert start_time(pid) == started, "PID was reused while opening pidfd"
identity = {"pid": pid, "start_time_ticks": started, "container_id": container_id,
            "namespace": namespace, "pod": pod, "pod_uid": uid}
if action == "inspect":
    print(json.dumps(identity))
elif action == "resume":
    signal.pidfd_send_signal(fd, signal.SIGCONT)
    print(json.dumps(dict(identity, action="SIGCONT")))
elif action == "stop":
    assert pid == int(sys.argv[7]) and started == sys.argv[8], "Process identity changed"
    seconds = int(sys.argv[9])
    assert 30 <= seconds <= 300, "Invalid watchdog deadline"
    watchdog_file = "/var/tmp/project01-probe-watchdog-" + container_id + ".json"
    # Arm a detached watchdog BEFORE stopping the process. It inherits the
    # original pidfd and can resume that process if the SSH/coordinator dies.
    child = os.fork()
    if child == 0:
        os.setsid()
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with open(os.devnull, "r+") as null:
            for stream in (0, 1, 2):
                os.dup2(null.fileno(), stream)
        record = dict(identity, watchdog_seconds=seconds)
        try:
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            if poller.poll(seconds * 1000):
                record["action"] = "original-process-exited"
            else:
                assert start_time(pid) == started, "Process identity changed"
                signal.pidfd_send_signal(fd, signal.SIGCONT)
                record["action"] = "failsafe-SIGCONT"
        except Exception as error:
            record["error"] = str(error)
        record["finished_epoch"] = time.time()
        Path(watchdog_file).write_text(json.dumps(record, indent=2) + "\n")
        os._exit(0)
    signal.pidfd_send_signal(fd, signal.SIGSTOP)
    print(json.dumps(dict(identity, action="SIGSTOP", stopped_at_epoch=time.time(), watchdog_pid=child,
                          watchdog_seconds=seconds, watchdog_file=watchdog_file)))
else:
    raise ValueError("Unknown action")
'''


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_json(command, source=None):
    result = subprocess.run(command, input=source, text=True, capture_output=True, timeout=25)
    if result.returncode:
        raise RuntimeError("Command failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def write_json(directory, name, value):
    (directory / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def container_state(pod):
    matches = [item for item in pod.get("status", {}).get("containerStatuses", [])
               if item["name"] == "checkoutservice"]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one checkout container status")
    container = matches[0]
    return {"pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
            "ready": container["ready"], "restart_count": container["restartCount"],
            "container_id": container.get("containerID", ""),
            "state": container.get("state", {}),
            "last_state": container.get("lastState", {})}


def endpoint_ready(slices, uid):
    matching = [endpoint for item in slices["items"] for endpoint in item.get("endpoints", [])
                if endpoint.get("targetRef", {}).get("uid") == uid]
    return any(endpoint.get("conditions", {}).get("ready") is True for endpoint in matching)


def event_count(event):
    return int(event.get("series", {}).get("count", event.get("count", 1)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", default="microservice")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--worker-node", required=True)
    parser.add_argument("--worker-host", required=True)
    parser.add_argument("--worker-user", required=True)
    parser.add_argument("--worker-key", required=True)
    parser.add_argument("--worker-known-hosts", required=True)
    parser.add_argument("--runtime-endpoint", default="unix:///run/containerd/containerd.sock")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    if not 30 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be 30-300")
    if not args.evidence_dir.is_absolute():
        parser.error("--evidence-dir must be absolute")
    for path in (args.kubeconfig, args.worker_key, args.worker_known_hosts):
        if not Path(path).is_file():
            parser.error("Required local file does not exist: " + path)
    args.evidence_dir.mkdir(parents=True, exist_ok=False)
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "-n", args.namespace,
               "--request-timeout=15s"]
    result = {"status": "FAIL", "started_at_utc": utc_now(),
              "method": "SIGSTOP checkout process; kubelet liveness recovery",
              "timeout_seconds": args.timeout_seconds}
    original = None
    identity = None
    stop_attempted = False
    observed = {"unready": False, "endpoint_removed": False,
                "readiness_failure_event": False,
                "liveness_failure_event": False, "liveness_restart_event": False,
                "same_pod_restarted_once": False, "ready_again": False}

    def get(*items):
        return run_json(kubectl + ["get", *items, "-o", "json"])

    def worker(action):
        command = ["sudo", "-n", "python3", "-", action, args.runtime_endpoint,
                   original["container_id"].removeprefix("containerd://"),
                   args.namespace, original["pod"], original["uid"]]
        if action in ("stop", "resume"):
            command += [str(identity["pid"]), identity["start_time_ticks"]]
        if action == "stop":
            command += [str(args.timeout_seconds)]
        ssh = ["ssh", "-i", args.worker_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + args.worker_known_hosts,
               args.worker_user + "@" + args.worker_host, shlex.join(command)]
        return run_json(ssh, WORKER_HELPER)

    def interrupted(signum, frame):
        raise RuntimeError("Experiment interrupted by signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    started = time.monotonic()
    try:
        pods = get("pods", "-l", "app=checkoutservice")["items"]
        if len(pods) != 1 or pods[0]["metadata"].get("deletionTimestamp"):
            raise RuntimeError("Expected one stable checkout Pod; finish rollouts first")
        pod = pods[0]
        original = container_state(pod)
        if not original["ready"] or pod["spec"]["nodeName"] != args.worker_node:
            raise RuntimeError("Checkout must be Ready on the explicitly selected worker")
        if not re.fullmatch(r"containerd://[0-9a-f]{64}", original["container_id"]):
            raise RuntimeError("Expected a full containerd container ID")
        containers = [item for item in pod["spec"]["containers"] if item["name"] == "checkoutservice"]
        for name in ("readinessProbe", "livenessProbe"):
            if not containers[0].get(name, {}).get("grpc", {}).get("port"):
                raise RuntimeError("Deploy the native gRPC " + name + " before this experiment")
        slices = get("endpointslices", "-l", "kubernetes.io/service-name=checkoutservice")
        if not endpoint_ready(slices, original["uid"]):
            raise RuntimeError("Checkout has no ready Service endpoint")
        events = get("events", "--field-selector", "involvedObject.uid=" + original["uid"])
        baseline_counts = {event["metadata"]["uid"]: event_count(event) for event in events["items"]}
        write_json(args.evidence_dir, "baseline-pod.json", pod)
        write_json(args.evidence_dir, "baseline-endpoints.json", slices)
        write_json(args.evidence_dir, "baseline-events.json", events)
        identity = worker("inspect")
        write_json(args.evidence_dir, "process-identity.json", identity)
        stop_attempted = True
        fault = worker("stop")
        fault_acknowledged = time.monotonic()
        fault["observed_at_utc"] = utc_now()
        write_json(args.evidence_dir, "fault.json", fault)
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline:
            pod = get("pod", original["pod"])
            current = container_state(pod)
            if current["uid"] != original["uid"]:
                raise RuntimeError("Pod was replaced; this does not demonstrate container recovery")
            slices = get("endpointslices", "-l", "kubernetes.io/service-name=checkoutservice")
            events = get("events", "--field-selector", "involvedObject.uid=" + original["uid"])
            ready_endpoint = endpoint_ready(slices, original["uid"])
            for event in events["items"]:
                if event_count(event) <= baseline_counts.get(event["metadata"]["uid"], 0):
                    continue
                message = event.get("message", "").lower()
                if event.get("reason") == "Unhealthy" and "readiness probe failed" in message:
                    observed["readiness_failure_event"] = True
                if event.get("reason") == "Unhealthy" and "liveness probe failed" in message:
                    observed["liveness_failure_event"] = True
                if event.get("reason") == "Killing" and "liveness probe" in message:
                    observed["liveness_restart_event"] = True
            restarts = current["restart_count"] - original["restart_count"]
            # Capture readiness removing the original, still-running container
            # before the liveness Killing event, rather than only a restart gap.
            before_kill = (restarts == 0 and current["container_id"] == original["container_id"]
                           and "running" in current["state"]
                           and not observed["liveness_restart_event"])
            observed["unready"] |= before_kill and not current["ready"]
            observed["endpoint_removed"] |= before_kill and not current["ready"] and not ready_endpoint
            if restarts > 1:
                raise RuntimeError("Container restarted more than once during the experiment")
            recovered = (restarts == 1 and bool(current["container_id"])
                         and current["container_id"] != original["container_id"])
            observed["same_pod_restarted_once"] |= recovered
            healthy_now = recovered and current["ready"] and ready_endpoint and "running" in current["state"]
            observed["ready_again"] |= healthy_now
            sample = dict(current, at_utc=utc_now(), endpoint_ready=ready_endpoint,
                          elapsed_seconds=round(time.monotonic() - started, 2),
                          seconds_since_fault_acknowledged=round(time.monotonic() - fault_acknowledged, 2))
            with (args.evidence_dir / "timeline.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample) + "\n")
            write_json(args.evidence_dir, "latest-pod.json", pod)
            write_json(args.evidence_dir, "latest-endpoints.json", slices)
            write_json(args.evidence_dir, "latest-events.json", events)
            if all(observed.values()) and healthy_now:
                result["status"] = "PASS"
                result["original"] = original
                result["recovered"] = current
                result["recovery_seconds_since_fault_acknowledged"] = round(time.monotonic() - fault_acknowledged, 2)
                break
            time.sleep(2)
        if result["status"] != "PASS":
            raise RuntimeError("Recovery evidence incomplete before deadline")
    except (Exception, KeyboardInterrupt) as error:
        result["error"] = str(error)
    finally:
        if stop_attempted and identity:
            try:
                result["cleanup"] = worker("resume")
            except Exception as error:
                result["cleanup_error"] = str(error)
                # The independent worker watchdog still holds the original pidfd.
                result["status"] = "FAIL"
        result["observations"] = observed
        result["finished_at_utc"] = utc_now()
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        write_json(args.evidence_dir, "recovery-result.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
