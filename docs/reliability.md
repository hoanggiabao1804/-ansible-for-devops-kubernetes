# Probes and measured resource settings

The `0.3.0` microservice chart (resources/probes introduced in `0.2.0`) accepts Kubernetes container `resources`,
`startupProbe`, `readinessProbe`, and `livenessProbe` mappings. Empty chart defaults
omit these fields. `vars/advanced.yml` enables the measured project settings for
the ten microservice-chart releases. Redis still uses the pinned upstream chart.
The checkout naming override remains. The integrated frontend is ClusterIP;
external tests use the controller NodePort with `--host-header boutique.local`.

## Measure before choosing limits

`site.yml` installs Metrics Server; it can also be installed separately:

```bash
ansible-playbook playbooks/metrics.yml
sudo python3 scripts/measure-resources.py http://<worker-private-ip>:30080 \
  --host-header boutique.local \
  --kubeconfig /etc/kubernetes/admin.conf \
  --output-dir /var/tmp/project01-resources-<unique-run> \
  --idle-seconds 120 --load-seconds 300 --interval 15 \
  --workers 2 --cycle-interval 5
```

Run the script on a controller that has access to the kubeconfig and worker URL.
It waits for complete Metrics API coverage, samples all eleven Deployments,
saves raw metrics and `kubectl top` output, and runs bounded demo purchases.
Each worker owns its cookie session and starts at most one purchase per five
seconds. `summary.json`, `samples.csv`, and `load/summary.json` contain the
measurements and traffic results. Use a new evidence directory for every run.

CPU is an average over the recorded Metrics API window; memory is working set,
not total cgroup memory. Sampling can miss brief peaks. These short lab runs do
not establish production capacity. See the [Kubernetes metrics
documentation](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/).

Metrics Server is pinned to `v0.8.1` with its release-manifest SHA-256. This version
supports Kubernetes 1.31 and newer. The private kubeadm lab uses self-signed
kubelet serving certificates, so `vars/metrics.yml` explicitly enables
`--kubelet-insecure-tls` for scraping. Set it to false after provisioning trusted
serving certificates. The upstream APIService also uses `insecureSkipTLSVerify`.
These are lab TLS settings; see [Metrics Server requirements](https://github.com/kubernetes-sigs/metrics-server#requirements).

## Health checks

| Deployment | Probe target | Startup | Readiness | Liveness |
| --- | --- | --- | --- | --- |
| frontend | HTTP `/_healthz`, container port 8080 | Every 5 s, 30 failures | Every 5 s, 2 failures | Every 10 s, 3 failures |
| checkoutservice | gRPC health, port 5050 | Every 5 s, 30 failures | Every 5 s, 2 failures | Every 10 s, 3 failures |
| productcatalogservice | gRPC health, port 3550 | Every 5 s, 30 failures | Every 5 s, 2 failures | Every 10 s, 3 failures |
| shippingservice | gRPC health, port 50051 | Every 5 s, 30 failures | Every 5 s, 2 failures | Every 10 s, 3 failures |

All probe timeouts are 2 s. Startup allows approximately 150 s before startup
failure and keeps readiness/liveness from running prematurely. Readiness controls
traffic; liveness restarts an unresponsive container. These endpoints check the
service's own responsiveness, not every dependency. Full checkout smoke tests
therefore remain part of validation. Endpoints come from the application's
[pinned v0.2.3 release](https://github.com/GoogleCloudPlatform/microservices-demo/blob/v0.2.3/release/kubernetes-manifests.yaml);
native gRPC probes are supported by this project's Kubernetes 1.32.

## Apply and verify

```bash
ansible-playbook playbooks/deploy.yml
ansible-playbook playbooks/verify.yml
```

Deployment combines common values, per-release reliability settings, and existing
release overrides. Verification checks actual current Ready Pods as well as
Deployment specifications, including resource quantities, probe fields and
rollout ownership. Run deploy again to check idempotency. Set
`microservice_reliability_enabled=false` as an extra variable to deploy without
the advanced settings and skip their verification.

## Demonstrate a liveness restart

This explicit experiment is excluded from `site.yml`. Use the dedicated lab
after the deployment has stabilized. The key and known-hosts paths must exist
on the master, where the experiment executes; supply a verified worker host key.

```bash
ansible-playbook playbooks/diagnose-probes.yml \
  -e probe_worker_key=/absolute/master/path/to/worker-ssh-key \
  -e probe_worker_known_hosts=/absolute/master/path/to/known_hosts \
  -e probe_diagnosis_root=/var/tmp/project01-probes
```

The script identifies the running checkout container through Kubernetes and
containerd, checks its namespace/Pod UID/process identity, then sends SIGSTOP
to that process. A detached worker watchdog is armed before the fault and resumes
the original process if recovery does not happen by the deadline. Cleanup also
attempts a guarded SIGCONT. No Pod is deleted and no Helm values are modified.

Evidence must show the original running container becoming unready and leaving
ready Service endpoints before restart, fresh readiness/liveness events, the
same Pod UID with a new container ID and restart count incremented once, and
successful checkout after recovery. Exit code 137 after SIGKILL alone is not
evidence of OOM. The script saves the container termination reason and kubelet
events to distinguish the mechanisms. See [Kubernetes probe
semantics](https://v1-32.docs.kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/).

The historical validation below predates operations integration. The integrated
source still requires live acceptance and a clean Vagrant
rebuild; passing on an existing Azure lab does not replace that requirement.

## Measured sizing: 20261015T140139Z

The baseline measurement ran from `2026-10-15T14:01:39.374+00:00` to
`2026-10-15T14:08:39.979+00:00` (UTC), before applying these resource limits.
It passed with 28 complete samples per Deployment: 8 during 120 s idle and 20
during 300 s load, sampled every 15 s. Two workers each started at most one
demo purchase every 5 s. All 120 purchases and 720 HTTP steps succeeded; the
recorded successful purchase-cycle latency p95 was 0.1073 s. HTTP steps count
opener calls; automatic redirect hops are additional HTTP requests.
The external baseline evidence directory
is `evidence/measurement-20261015T140139Z`, relative to the reliability report.
The raw Metrics API responses, `kubectl top` captures, CSV, configuration and
load summary remain the source of record.

Peak values below are the maximum across both phases. CPU is in millicores;
RAM is the Metrics API working set in MiB. Requests and limits show CPU/RAM.
The cgroup column is a separate pre-deployment peak-memory cross-check, not a
Metrics API working-set sample or a measurement confined to the load phase.

| Deployment | CPU peak (m) | RAM peak (MiB) | Cgroup peak (MiB) | Chosen requests | Chosen limits |
| --- | ---: | ---: | ---: | --- | --- |
| adservice | 12.640 | 77.727 | 96.59 | 25m / 112Mi | 200m / 160Mi |
| cartservice | 8.480 | 41.934 | 44.87 | 25m / 64Mi | 200m / 128Mi |
| checkoutservice | 6.114 | 4.906 | 8.41 | 25m / 32Mi | 200m / 128Mi |
| currencyservice | 7.155 | 24.863 | 34.34 | 25m / 32Mi | 200m / 128Mi |
| emailservice | 1.126 | 38.832 | 43.71 | 25m / 64Mi | 200m / 128Mi |
| frontend | 12.466 | 6.992 | 9.39 | 25m / 32Mi | 200m / 128Mi |
| paymentservice | 1.018 | 23.121 | 36.52 | 25m / 32Mi | 200m / 128Mi |
| productcatalogservice | 5.031 | 4.395 | 8.19 | 25m / 32Mi | 200m / 128Mi |
| recommendationservice | 3.524 | 36.023 | 37.79 | 25m / 48Mi | 200m / 128Mi |
| shippingservice | 1.924 | 4.715 | 7.89 | 25m / 32Mi | 200m / 128Mi |
| redis-cart | 1.862 | 5.641 | 6.68 | Excluded: upstream chart | Excluded: upstream chart |

`scripts/measure-resources.py` calculates suggestions from the combined idle
and load samples, using nearest-rank p95 and upward rounding. Here `ceil_step`
means round upward to a multiple of the indicated step:

```text
CPU request (m) = ceil_25(max(25, 1.5 * combined CPU p95))
CPU limit   (m) = ceil_100(max(200, 3 * combined CPU peak))
RAM request (Mi) = ceil_16(max(32, 1.25 * combined working-set peak))
RAM limit   (Mi) = ceil_32(max(128, 2 * combined working-set peak))
```

The ten selected mappings in `vars/advanced.yml` match these suggestions.
Every chosen memory limit also exceeds 1.5 times the independently observed
cgroup peak; the smallest ratio is approximately 1.66 for adservice
(160 / 96.59). All eleven pre-deployment cgroup OOM counters were zero.
This cross-check adds headroom for memory charged beyond the working set; it
does not establish a worst-case bound for future startup or traffic.

For one replica of each of the ten microservices, requests total **250m CPU
and 480Mi RAM**, and limits total **2000m CPU and 1312Mi RAM**. Redis is measured
for context but excluded from these settings and totals. The observed worker
allocatable capacity was 2 CPU and approximately 3811.4Mi RAM. Comparing these
application totals alone with allocatable is insufficient: Redis, system Pods,
node reservations and temporary rollout replicas also need capacity. Acceptance
requires the live node-budget, deployment, repeated-deploy, post-change load,
startup/restart and rollback evidence described above; baseline PASS alone
does not certify those checks.

## Live validation: 20261016T014729Z

The existing Azure lab completed the application validation at
`2026-10-16T01:54:37Z`. Evidence is external to Git under
`evidence/validation-20261016T014729Z/`, relative to the reliability report;
that report links directly to each artifact.

| Check | Observed result |
| --- | --- |
| Chart rendering | Ten disabled renders match upstream; ten enabled resource mappings and four complete probe sets pass. |
| Deploy / verify | Initial deploy: `ok=22 changed=2 failed=0`; initial verify: `ok=21 changed=2 failed=0` (installs verifier). |
| Application idempotency | Repeated deploy: `ok=22 changed=0 failed=0`; all eleven release revisions, Pod UIDs/restarts and Deployment states remain unchanged. |
| Post-change load | 30 s idle + 120 s load, 4 workers/2 s, 15 s sampling: 2 idle + 8 load samples; 240/240 purchases, 1440 HTTP steps; successful-cycle p95 0.1573 s. |
| Memory | All ten microservices remain below their memory limits; all ten cgroup `oom` and `oom_kill` counters are zero. |
| Worker budget | All nonterminated worker Pods request 650m CPU / 870Mi RAM and have limits totaling 2 CPU / 1652Mi RAM; allocatable is 2 CPU / approximately 3811.4Mi RAM. |
| Probe recovery | All seven required observations pass; same checkout Pod UID, new container ID, restart count 0 to 1; Ready 59.22 s after fault acknowledgement; checkout succeeds. |
| Checkout rollback | Original revision 8's manifest and values restored, including probes/resources; checkout succeeds; `ok=83 changed=20 failed=0`. |
| Final verification | Eleven Deployments Ready; `ok=21 changed=0 failed=0`. |

The faulted checkout container exited 137 with reason `Error`; fresh kubelet
`Killing` events identify liveness as the restart trigger. It was not reported
as `OOMKilled`. The achieved purchase rate was approximately 2 cycles/s, five
times the baseline offered rate; this bounded run does not establish maximum
capacity. Declared resource headroom is not a measurement of free resources.

Metrics Server initially reported its Deployment as `configured` on repeated
apply, as recorded in `metrics-audit-first.log` and `metrics-audit-second.log`.
No underlying cause is assumed. The playbook now checks diff before apply: two unchanged
runs each returned `ok=10 changed=0 failed=0`. A deliberately changed
ServiceAccount `k8s-app` label was restored by Ansible with
`ok=12 changed=1 failed=0`; its UID was preserved. The supporting
`metrics-idempotency.json`, `metrics-fixed-first.log`,
`metrics-fixed-second.log` and `metrics-drift-repaired.log` accompany the
validation evidence. Integration with the group's other changes and a clean
Vagrant rebuild remain unperformed acceptance steps.
