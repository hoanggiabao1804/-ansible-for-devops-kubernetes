#!/usr/bin/env python3
"""Check configured resources/probes and the Pods serving the current rollout.

Read {"services": {release: Helm values}, "deployments": {release: name}}
from stdin. Print a JSON report and return nonzero when any check fails.
Only read Kubernetes resources; never change the cluster.
"""

import argparse
from decimal import Decimal, InvalidOperation
import json
import re
import subprocess
import sys


PROBES = ("startupProbe", "readinessProbe", "livenessProbe")
HANDLERS = {"exec", "httpGet", "tcpSocket", "grpc"}
QUANTITY_SUFFIXES = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    **{unit + "i": Decimal(1024) ** power
       for power, unit in enumerate("KMGTPE", start=1)},
}


def quantity(value):
    """Compare API-normalized quantities without requiring identical spelling."""
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[a-zA-Z]*)",
        str(value),
    )
    if not match:
        raise ValueError(f"Invalid Kubernetes quantity: {value!r}")
    number, suffix = match.groups()
    try:
        if re.fullmatch(r"[eE][+-]?\d+", suffix):
            return Decimal(number + suffix)
        return Decimal(number) * QUANTITY_SUFFIXES[suffix]
    except (KeyError, InvalidOperation) as exc:
        raise ValueError(f"Invalid Kubernetes quantity: {value!r}") from exc


def require(condition, message):
    if not condition:
        raise ValueError(message)


def contains(actual, expected, path):
    """Allow fields defaulted by the API while checking every configured field."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{path}: expected an object")
        for key, value in expected.items():
            require(key in actual, f"{path}.{key}: missing")
            contains(actual[key], value, f"{path}.{key}")
    else:
        require(actual == expected, f"{path}: expected {expected!r}, got {actual!r}")


def check_container(container, expected, path):
    resources = expected.get("resources", {})
    actual_resources = container.get("resources", {})
    for resource in ("cpu", "memory"):
        amounts = {}
        for kind in ("requests", "limits"):
            configured = resources.get(kind, {}).get(resource)
            actual = actual_resources.get(kind, {}).get(resource)
            require(configured is not None, f"{path}: configure {kind}.{resource}")
            require(actual is not None, f"{path}: missing {kind}.{resource}")
            amounts[kind] = quantity(configured)
            require(amounts[kind] > 0, f"{path}: {kind}.{resource} must be positive")
            require(
                quantity(actual) == amounts[kind],
                f"{path}: {kind}.{resource} expected {configured}, got {actual}",
            )
        require(
            amounts["requests"] <= amounts["limits"],
            f"{path}: {resource} request exceeds limit",
        )
    for probe in PROBES:
        if not expected.get(probe):
            continue
        require(isinstance(expected[probe], dict), f"{path}.{probe}: expected an object")
        actual_probe = container.get(probe, {})
        expected_handlers = HANDLERS.intersection(expected[probe])
        require(len(expected_handlers) == 1, f"{path}.{probe}: select one handler")
        require(
            HANDLERS.intersection(actual_probe) == expected_handlers,
            f"{path}.{probe}: handler differs from configuration",
        )
        contains(actual_probe, expected[probe], f"{path}.{probe}")


def app_container(containers, deployment):
    matches = [item for item in containers if item.get("name") == deployment]
    require(len(matches) == 1, f"{deployment}: expected exactly one app container")
    return matches[0]


def controlled_by(item, kind, uid):
    return any(
        owner.get("kind") == kind
        and owner.get("uid") == uid
        and owner.get("controller") is True
        for owner in item.get("metadata", {}).get("ownerReferences", [])
    )


def check_release(release, name, expected, objects):
    deployments = [item for item in objects["deployments"]
                   if item["metadata"]["name"] == name]
    require(len(deployments) == 1, f"{release}: Deployment {name} not found")
    deployment = deployments[0]
    metadata, spec, status = (deployment.get(key, {})
                              for key in ("metadata", "spec", "status"))
    require(not metadata.get("deletionTimestamp"), f"{name}: Deployment terminating")
    desired = spec.get("replicas", 1)
    require(desired > 0, f"{name}: no replicas requested")
    require(
        status.get("observedGeneration", 0) >= metadata.get("generation", 1),
        f"{name}: controller has not observed current generation",
    )
    for field in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"):
        require(status.get(field, 0) == desired, f"{name}: {field} != {desired}")
    require(status.get("unavailableReplicas", 0) == 0, f"{name}: unavailable replicas")
    check_container(app_container(spec["template"]["spec"]["containers"], name),
                    expected, f"Deployment/{name}")

    revision = metadata.get("annotations", {}).get("deployment.kubernetes.io/revision")
    require(revision is not None, f"{name}: no current Deployment revision")
    owned_sets = [item for item in objects["replicasets"]
                  if controlled_by(item, "Deployment", metadata["uid"])]
    current_sets = [item for item in owned_sets
                    if item["metadata"].get("annotations", {}).get(
                        "deployment.kubernetes.io/revision") == revision]
    require(len(current_sets) == 1, f"{name}: current ReplicaSet is ambiguous or absent")
    current_set = current_sets[0]
    set_uids = {item["metadata"]["uid"] for item in owned_sets}
    pods = [item for item in objects["pods"]
            if not item["metadata"].get("deletionTimestamp")
            and any(controlled_by(item, "ReplicaSet", uid) for uid in set_uids)]
    require(len(pods) == desired, f"{name}: expected {desired} active Pods, got {len(pods)}")
    pod_results = []
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        require(controlled_by(pod, "ReplicaSet", current_set["metadata"]["uid"]),
                f"{pod_name}: still belongs to an older ReplicaSet")
        require(pod.get("status", {}).get("phase") == "Running", f"{pod_name}: not Running")
        require(any(item.get("type") == "Ready" and item.get("status") == "True"
                    for item in pod.get("status", {}).get("conditions", [])),
                f"{pod_name}: Pod is not Ready")
        check_container(app_container(pod["spec"]["containers"], name), expected,
                        f"Pod/{pod_name}")
        container_status = app_container(pod.get("status", {}).get("containerStatuses", []), name)
        require(container_status.get("ready") is True, f"{pod_name}: app container not ready")
        require("running" in container_status.get("state", {}), f"{pod_name}: app not running")
        require(container_status.get("started", True) is True, f"{pod_name}: startup incomplete")
        pod_results.append({
            "name": pod_name,
            "uid": pod["metadata"]["uid"],
            "node": pod["spec"].get("nodeName"),
            "ready": True,
            "restart_count": container_status.get("restartCount", 0),
            "container_id": container_status.get("containerID"),
        })
    return {
        "release": release,
        "deployment": name,
        "revision": revision,
        "replicas": desired,
        "resources": expected["resources"],
        "probes": {key: expected[key] for key in PROBES if expected.get(key)},
        "pods": pod_results,
        "status": "PASS",
    }


def validate(configuration, objects, minimum_probe_services=4):
    require(isinstance(configuration, dict), "Configuration must be an object")
    services = configuration.get("services", {})
    names = configuration.get("deployments", {})
    require(isinstance(services, dict) and services, "services must be a nonempty mapping")
    require(all(isinstance(values, dict) for values in services.values()),
            "Each service must contain a mapping of Helm values")
    require(isinstance(names, dict), "deployments must be a mapping")
    require(all(release in names for release in services), "Deployment mapping is incomplete")
    require(len({names[key] for key in services}) == len(services),
            "Each release must map to a distinct Deployment")
    probe_count = sum(all(values.get(key) for key in PROBES) for values in services.values())
    require(probe_count >= minimum_probe_services,
            f"Need startup/readiness/liveness on >= {minimum_probe_services} services; got {probe_count}")
    results, errors = [], []
    for release, expected in services.items():
        try:
            results.append(check_release(release, names[release], expected, objects))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"{release}: {exc}")
    return {
        "status": "FAIL" if errors else "PASS",
        "configured_services": len(services),
        "services_with_three_probes": probe_count,
        "services": results,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--minimum-probe-services", type=int, default=4)
    args = parser.parse_args()
    try:
        configuration = json.load(sys.stdin)
        objects = {}
        for kind in ("deployments", "replicasets", "pods"):
            result = subprocess.run(
                ["kubectl", "--kubeconfig", args.kubeconfig, "--namespace", args.namespace,
                 "get", kind, "--output=json", "--request-timeout=20s"],
                check=False, capture_output=True, text=True, timeout=30,
            )
            require(result.returncode == 0, f"kubectl get {kind} failed (exit {result.returncode})")
            objects[kind] = json.loads(result.stdout)["items"]
        report = validate(configuration, objects, args.minimum_probe_services)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        report = {"status": "FAIL", "errors": [str(exc)]}
    report["namespace"] = args.namespace
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
