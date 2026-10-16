#!/usr/bin/env python3
"""Measure Online Boutique before and during bounded purchases.

Requires kubectl and an installed, healthy Metrics Server. CPU is in
millicores; memory is the Metrics API working set in MiB, not RSS or a
guaranteed memory-limit requirement. All evidence is written outside the
repository to a new output directory supplied by the operator.
"""

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def quantity(value):
    """Convert a Kubernetes decimal/binary resource quantity to base units."""
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)", value)
    if not match:
        raise ValueError("Unsupported Kubernetes quantity: " + value)
    scales = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"),
              "m": Decimal("1e-3"), "k": 1000, "K": 1000,
              "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
              "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
              "Pi": 2**50, "Ei": 2**60}
    return float(Decimal(match.group(1)) * Decimal(scales[match.group(2)]))


def owner(obj, kind):
    return next((ref["name"] for ref in obj["metadata"].get("ownerReferences", [])
                 if ref["kind"] == kind and ref.get("controller", False)), None)


def percentile95(values):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * .95) - 1]


def round_up(value, step):
    return math.ceil(value / step) * step


def summarize(rows):
    # Aggregate all containers/pods belonging to a Deployment for each sample.
    # Resource suggestions are emitted only for the measured one-pod,
    # one-container baseline used by this project.
    grouped = {}
    for row in rows:
        key = row["phase"], row["sample"], row["deployment"]
        item = grouped.setdefault(key, {"cpu": 0, "memory": 0, "containers": 0})
        item["cpu"] += row["cpu_millicores"]
        item["memory"] += row["memory_mib"]
        item["containers"] += 1
    result = {}
    for deployment in sorted({row["deployment"] for row in rows}):
        phases = {}
        combined = []
        for phase in ("idle", "load"):
            measurements = [value for key, value in grouped.items()
                            if key[0] == phase and key[2] == deployment]
            if not measurements:
                continue
            combined.extend(measurements)
            phases[phase] = {
                "samples": len(measurements),
                "cpu_p95_millicores": round(percentile95([m["cpu"] for m in measurements]), 3),
                "cpu_peak_millicores": round(max(m["cpu"] for m in measurements), 3),
                "memory_p95_mib": round(percentile95([m["memory"] for m in measurements]), 3),
                "memory_peak_mib": round(max(m["memory"] for m in measurements), 3),
            }
        entry = {"phases": phases}
        if combined and all(m["containers"] == 1 for m in combined):
            cpu_p95 = percentile95([m["cpu"] for m in combined])
            cpu_peak = max(m["cpu"] for m in combined)
            memory_peak = max(m["memory"] for m in combined)
            entry["provisional_resources"] = {
                "requests": {"cpu": "{}m".format(round_up(max(25, cpu_p95 * 1.5), 25)),
                             "memory": "{}Mi".format(round_up(max(32, memory_peak * 1.25), 16))},
                "limits": {"cpu": "{}m".format(round_up(max(200, cpu_peak * 3), 100)),
                           "memory": "{}Mi".format(round_up(max(128, memory_peak * 2), 32))},
            }
        else:
            entry["provisional_resources"] = None
        result[deployment] = entry
    return result


def main():
    spec = importlib.util.spec_from_file_location("boutique_load", Path(__file__).with_name("boutique-load.py"))
    load = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(load)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", type=load.validate_url)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--namespace", default="microservice")
    parser.add_argument("--idle-seconds", type=float, default=120)
    parser.add_argument("--load-seconds", type=float, default=300)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--cycle-interval", type=float, default=5)
    parser.add_argument("--expected-deployments", type=int, default=11)
    parser.add_argument("--warmup-timeout", type=float, default=120,
                        help="Wait for Ready pod metrics before measuring (seconds)")
    args = parser.parse_args()
    if not 5 <= args.interval <= 60 or not args.interval <= args.idle_seconds <= 1800 or not args.interval <= args.load_seconds <= 1800:
        parser.error("Use interval 5–60 s and each phase between interval and 1800 s")
    if args.cycle_interval < 2 or args.expected_deployments < 1:
        parser.error("cycle interval must be >=2 s; expected deployments must be positive")
    if not 1 <= args.warmup_timeout <= 600:
        parser.error("warmup timeout must be between 1 and 600 seconds")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw").mkdir()
    kubectl = [args.kubectl, "--kubeconfig", args.kubeconfig, "--request-timeout=20s"]
    rows = []
    errors = []
    sample_counts = {}
    load_process = None
    load_log = None
    started_at = utc_now()

    def command(arguments, timeout=30):
        proc = subprocess.run(kubectl + arguments, text=True, capture_output=True, timeout=timeout)
        if proc.returncode:
            raise RuntimeError("kubectl {}: {}".format(" ".join(arguments), proc.stderr.strip()))
        return proc.stdout

    def api(arguments, timeout=30):
        return json.loads(command(arguments, timeout=timeout))

    def save_json(name, data):
        (output / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    try:
        deployments = api(["get", "deployments", "-n", args.namespace, "-o", "json"])
        replicasets = api(["get", "replicasets", "-n", args.namespace, "-o", "json"])
        save_json("deployments-before.json", deployments)
        save_json("replicasets-before.json", replicasets)
        save_json("nodes.json", api(["get", "nodes", "-o", "json"]))
        names = {item["metadata"]["name"] for item in deployments["items"]}
        if len(names) != args.expected_deployments:
            raise RuntimeError("Expected {} Deployments, observed {}".format(args.expected_deployments, len(names)))
        rs_deployments = {item["metadata"]["name"]: owner(item, "Deployment")
                          for item in replicasets["items"]}
        save_json("configuration.json", {
            "started_at_utc": started_at, "namespace": args.namespace,
            "base_url": args.base_url, "idle_seconds": args.idle_seconds,
            "load_seconds": args.load_seconds, "interval_seconds": args.interval,
            "workers": args.workers, "cycle_interval_seconds": args.cycle_interval,
            "warmup_timeout_seconds": args.warmup_timeout,
            "deployments": sorted(names),
            "cpu_unit": "millicores", "memory_unit": "MiB (working set)",
            "metrics_reference": "https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/",
        })
        # Metrics Server may be Ready before it has a sample for every pod.
        # Keep this bounded warmup separate from the measured phases so a
        # newly installed server cannot bias the first idle sample.
        warmup = {"status": "WAITING", "started_at_utc": utc_now(),
                  "timeout_seconds": args.warmup_timeout, "checks": []}
        warmup_deadline = time.monotonic() + args.warmup_timeout
        desired = {item["metadata"]["name"]: max(1, item["spec"].get("replicas", 1))
                   for item in deployments["items"]}
        print("{} waiting for metrics coverage".format(utc_now()), flush=True)
        while time.monotonic() < warmup_deadline:
            check = {"at_utc": utc_now()}
            try:
                pods = api(["get", "pods", "-n", args.namespace, "-o", "json"],
                           timeout=min(30, max(.001, warmup_deadline - time.monotonic())))
                metrics = api(["get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/{}/pods".format(args.namespace)],
                              timeout=min(30, max(.001, warmup_deadline - time.monotonic())))
                ready_counts = {name: 0 for name in names}
                required_containers = set()
                for pod in pods["items"]:
                    deployment = rs_deployments.get(owner(pod, "ReplicaSet"))
                    ready = any(condition["type"] == "Ready" and condition["status"] == "True"
                                for condition in pod.get("status", {}).get("conditions", []))
                    if deployment in names and ready and not pod["metadata"].get("deletionTimestamp"):
                        ready_counts[deployment] += 1
                        required_containers.update((pod["metadata"]["name"], container["name"])
                                                   for container in pod["spec"]["containers"])
                measured_containers = {(metric["metadata"]["name"], container["name"])
                                       for metric in metrics["items"]
                                       for container in metric["containers"]
                                       if "cpu" in container.get("usage", {}) and "memory" in container["usage"]}
                check["ready_pods_by_deployment"] = ready_counts
                check["deployments_waiting_for_ready_pods"] = sorted(
                    name for name in names if ready_counts[name] < desired[name])
                check["containers_waiting_for_metrics"] = sorted(
                    "{}/{}".format(*item) for item in required_containers - measured_containers)
                check["metrics_timestamps"] = {metric["metadata"]["name"]: metric["timestamp"]
                                                for metric in metrics["items"]}
                if not check["deployments_waiting_for_ready_pods"] and not check["containers_waiting_for_metrics"]:
                    warmup["status"] = "PASS"
            except Exception as error:
                check["error"] = str(error)
            warmup["checks"].append(check)
            save_json("warmup.json", warmup)
            if warmup["status"] == "PASS":
                break
            time.sleep(min(5, max(0, warmup_deadline - time.monotonic())))
        warmup["finished_at_utc"] = utc_now()
        if warmup["status"] != "PASS":
            warmup["status"] = "FAIL"
        save_json("warmup.json", warmup)
        if warmup["status"] != "PASS":
            raise RuntimeError("Metrics coverage did not become ready within {} seconds; see warmup.json".format(args.warmup_timeout))
        for phase, duration in (("idle", args.idle_seconds), ("load", args.load_seconds)):
            print("{} phase={} duration={}s".format(utc_now(), phase, duration), flush=True)
            if phase == "load":
                load_log = (output / "load-process.log").open("w", encoding="utf-8")
                load_process = subprocess.Popen([
                    sys.executable, str(Path(__file__).with_name("boutique-load.py")),
                    args.base_url, "--output-dir", str(output / "load"),
                    "--duration", str(duration), "--workers", str(args.workers),
                    "--cycle-interval", str(args.cycle_interval)], stdout=load_log, stderr=subprocess.STDOUT)
            phase_start = time.monotonic()
            count = 0
            while time.monotonic() - phase_start < duration:
                count += 1
                stem = "raw/{}-{:03d}".format(phase, count)
                collected_at = utc_now()
                try:
                    pods = api(["get", "pods", "-n", args.namespace, "-o", "json"])
                    metrics = api(["get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/{}/pods".format(args.namespace)])
                    save_json(stem + "-pods.json", pods)
                    save_json(stem + "-metrics.json", metrics)
                    top = command(["top", "pods", "-n", args.namespace, "--containers"])
                    (output / (stem + "-top.txt")).write_text(collected_at + "\n" + top, encoding="utf-8")
                    pod_map = {item["metadata"]["name"]: item for item in pods["items"]}
                    observed = set()
                    for metric in metrics["items"]:
                        pod_name = metric["metadata"]["name"]
                        pod = pod_map.get(pod_name)
                        deployment = rs_deployments.get(owner(pod, "ReplicaSet")) if pod else None
                        if deployment not in names:
                            continue
                        observed.add(deployment)
                        for container in metric["containers"]:
                            rows.append({
                                "phase": phase, "sample": count, "collected_at_utc": collected_at,
                                "metrics_timestamp": metric["timestamp"], "metrics_window": metric["window"],
                                "deployment": deployment, "pod": pod_name, "pod_uid": pod["metadata"]["uid"],
                                "node": pod["spec"]["nodeName"], "container": container["name"],
                                "cpu_millicores": quantity(container["usage"]["cpu"]) * 1000,
                                "memory_mib": quantity(container["usage"]["memory"]) / 2**20,
                                "restart_count": sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", [])),
                            })
                    if observed != names:
                        raise RuntimeError("Missing metrics for: " + ", ".join(sorted(names - observed)))
                except Exception as error:
                    errors.append({"phase": phase, "sample": count, "at_utc": utc_now(), "error": str(error)})
                    print("sample error: {}".format(error), flush=True)
                pause = min(phase_start + count * args.interval, phase_start + duration) - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
            sample_counts[phase] = count
        if load_process.wait(timeout=30) != 0:
            errors.append({"phase": "load", "error": "Load generator failed; see load/summary.json and load-process.log"})
        save_json("deployments-after.json", api(["get", "deployments", "-n", args.namespace, "-o", "json"]))
        save_json("pods-after.json", api(["get", "pods", "-n", args.namespace, "-o", "json"]))
    except Exception as error:
        errors.append({"at_utc": utc_now(), "error": str(error)})
    finally:
        if load_process is not None and load_process.poll() is None:
            load_process.terminate()
            try:
                load_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                load_process.kill()
                load_process.wait(timeout=5)
        if load_log:
            load_log.close()
    if rows:
        with (output / "samples.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    summary = {
        "status": "PASS" if rows and not errors else "FAIL", "started_at_utc": started_at,
        "finished_at_utc": utc_now(), "samples_by_phase": sample_counts, "errors": errors,
        "deployments": summarize(rows),
        "sizing_method": {
            "cpu_request": "round up max(25m, 1.5 * combined p95) to 25m",
            "cpu_limit": "round up max(200m, 3 * combined peak) to 100m",
            "memory_request": "round up max(32Mi, 1.25 * combined peak working set) to 16Mi",
            "memory_limit": "round up max(128Mi, 2 * combined peak working set) to 32Mi",
            "limitations": [
                "Provisional lab sizing: validate startup/restarts and node allocatable capacity before accepting.",
                "Metrics API CPU is an average over its recorded window; brief peaks may be missed.",
                "Memory is working set, not RSS or all memory charged against the cgroup limit.",
                "Two phases and bounded demo traffic do not establish production capacity or a maximum.",
                "Suggestions are emitted only for one container per Deployment sample; compare raw data if pods changed.",
            ],
        },
    }
    save_json("summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
