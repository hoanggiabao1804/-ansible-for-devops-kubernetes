# Vendored microservice chart

`microservice/` was initially imported without changes from `chart/microservice` in
[iankisali/microservice-deployment](https://github.com/iankisali/microservice-deployment/tree/8ed96c8d98625c73b67c7172b6703383ea9e46e9/chart/microservice),
pinned at commit `8ed96c8d98625c73b67c7172b6703383ea9e46e9` and imported on
2026-10-15 with `git -c core.autocrlf=false -c core.eol=lf archive`. All five files
in the initial `0.1.0` import matched the upstream Git blobs.
[UPSTREAM.json](UPSTREAM.json) records the source and per-file SHA-256 checksums;
the root `.gitattributes` preserves LF line endings on Windows.

The chart retains the generic defaults (including `appName: servicename`).
Deployment values and project overrides still
supply each service's name, image and configuration. In particular, checkout needs
the existing `appName: checkoutservice` override. The upstream Redis chart and
service-specific values are outside this import.

Version `0.2.0` adds optional `resources`, `startupProbe`, `readinessProbe`, and
`livenessProbe` mappings to the Deployment container. They accept the corresponding
Kubernetes container fields and default to `{}`, which omits those fields and
preserves the baseline Deployment behavior. Per-service settings belong in
`vars/advanced.yml`; the chart does not guess resource sizes or probe endpoints.
The chart's `appVersion` metadata now reflects the existing `v0.2.3` image default;
the image tag itself has not changed.

For further local behavior changes, increment `microservice/Chart.yaml`'s chart
version and document the changes. Keep the import checksums as the original
snapshot reference; update the provenance when deliberately importing a new
upstream revision. Metadata stays outside `microservice/` so it does not enter the
packaged chart.

For the pinned application version, the frontend serves HTTP `/_healthz` on port
8080. Checkout (5050), product catalog (3550), shipping (50051), payment (50051),
and currency (7000) expose the standard gRPC health check. These endpoints are
documented in the application's
[v0.2.3 release manifest](https://github.com/GoogleCloudPlatform/microservices-demo/blob/v0.2.3/release/kubernetes-manifests.yaml).
Native `grpc` probes require Kubernetes 1.27 or newer; their numeric port must
refer to the container listener. Startup probes delay readiness and liveness
checks until the application starts. Readiness failure removes the Pod from
service traffic, while liveness failure restarts its container. See the
[Kubernetes 1.32 probe documentation](https://v1-32.docs.kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/).
These health handlers confirm that the application responds; checkout smoke
tests remain necessary to verify the full dependency chain.

The pinned upstream snapshot contains no `LICENSE` file. This import adds no
license and makes no assumption about one.

## Operations integration (0.3.0)

Adds optional Pod affinity and a chart-owned `policy/v1` PodDisruptionBudget.
Defaults remain empty, so upstream rendering behavior is preserved when these
features and reliability overrides are disabled. The frontend receives two
replicas by default, preferred node separation, and minAvailable=1 from
`vars/operations.yml`. Other services retain their existing replica counts.
The 0.1.0 import checksums in UPSTREAM.json remain provenance, not hashes of
the extended 0.3.0 chart.
