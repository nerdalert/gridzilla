# Gridzilla usage

Gridzilla attaches to a persistent three-site environment created by Grid's
`start-grid-combined-site-signals` xtask. It never creates or deletes clusters.
The only supported environment mutation is a temporary `llm-d-sim` fake queue
metric update through the simulator's local `/admin/config` endpoint; Gridzilla
captures and restores the exact original settings. The benchmark measures
Grid's routing/control path with deterministic simulator responses. It does
not measure real GPU saturation, model quality, or tokens per second.

## Prerequisites

- Linux amd64 host with Python 3.11+, Docker, Kind, kubectl, Node.js/npm, and
  enough CPU, memory, and disk for the selected profile.
- A Grid checkout at the setup revision and a persistent setup run that passed
  its readiness and pressure/recovery qualification.
- A real handoff file emitted by that run and readable credential files at the
  paths it names. Handoff files contain paths and runtime identities, never
  credential values.
- The pinned k6 v1.8.2 binary. `tools/install-k6.sh` verifies the release
  archive and unpacked binary SHA-256 before installing under `.tools/`.

Install Gridzilla and its UI dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
tools/install-k6.sh
npm ci
npm run build
```

The Grid setup repository is separate. From its exact tested checkout, start a
fresh persistent setup run:

```sh
cargo xtask env start-grid-combined-site-signals \
  --run-id grid-signals-<unique-suffix>
```

The setup prints and writes its run-owned handoff under
`evidence/grid-signals-combined-site/<run-id>/handoff.json`. Use that exact
path. For teardown, review the run manifest and execute the setup owner's
explicit cleanup command separately:

```sh
cargo xtask env clean-grid-combined-site-signals \
  --manifest evidence/grid-signals-combined-site/<run-id>/run-manifest.json
```

Gridzilla commands do not invoke either setup command and never delete the
environment.

## Handoff and preflight

The setup producer contract is `gridzilla.dev/v1alpha1`. See
[handoff-schema.md](handoff-schema.md) and
[`examples/handoff.redacted.json`](../examples/handoff.redacted.json). The
template is illustrative: replace every placeholder with the exact setup
paths, cluster names, image IDs, credentials-file locations, and runtime
resources. Then validate the real handoff before traffic:

```sh
gridzilla doctor --handoff /absolute/path/to/handoff.json
```

`doctor` checks the three run-owned Kind clusters and explicit kubeconfig
contexts; Docker/Kind/kubectl/k6 availability; host image-lock and running Pod
image identities; source provenance when available; provider generation and
conditions and live queue capacity against the handoff; ready simulator Pods,
their exact UID and Deployment generation, and a reachable fake-metrics admin
API; EPP/Gateway targets; authenticated EPP metrics and sample age; distributed
overlay candidates and revisions; Gateway
accepted/serving revisions; host capacity; and endpoint reachability. A failed
gate exits nonzero. It does not change simulator settings.

For the baseline request path, use:

```sh
gridzilla smoke --handoff /absolute/path/to/handoff.json --count 3
```

Smoke sends a bounded number (1–10) of uniquely identified authenticated
requests, checks status and route attribution, verifies the selected
site/pool/backend mappings, matches the simulator's echoed request ID and Pod
identity to the selected site, and rechecks readiness gates. The simulator
does not expose a separate receipt API; the response echo and backend Pod
identity provide a request-correlated receipt without enabling request-body
logging. No request body or credential is written to evidence.

## Benchmark profiles

Profiles in `profiles/` are versioned JSON. They bound:

- Open-loop constant-arrival rate, warmup, measurement window, and three
  independent repeats per rate.
- Preallocated VUs, maximum VUs/concurrency, request timeout, maximum output
  tokens, response bytes, total duration, and evidence-directory bytes.
- Signal/probe rate, freshness age, sample minimum for percentiles, and the
  expected pressure pool (`pool-a`).

k6 requires an integer arrival count per time unit. Gridzilla represents a
fractional request rate as an equivalent integer pair; for example, 0.2
requests/second is one scheduled request per five seconds. It does not round a
slow smoke rate up to one request per second.

The shipped profiles are intentionally conservative. `smoke.json` is a short
profile; `baseline.json` is the default bounded comparison; `stepped.json`
offers several low rates. `doctor --profile ...` refuses a run if the local
host lacks its estimated memory/disk headroom, is already CPU-saturated, or
the marked Pool A target is not west and rank 0 on the west/client Gateway.
Profile SLOs are optional and explicitly user-specified; no latency threshold
is inferred from MaaS benchmarks.

Run the complete baseline, pressure, and recovery procedure:

```sh
gridzilla run \
  --handoff /absolute/path/to/handoff.json \
  --profile profiles/baseline.json
```

Each run creates a unique `evidence/<setup-run-id>-<UTC-time>/` directory and
retains the redacted handoff, profile, source/image lock, k6 summary and raw
metrics, per-request low-rate probes, EPP samples, Grid scores and overlay
revisions, resource samples, command logs, original simulator configurations,
and restoration proof. The process uses a run lock to prevent two Gridzilla
instances from overwriting the same restoration snapshot.

The benchmark sequence is: healthy smoke; baseline windows at each configured
rate; direct backend reference windows; pressure only on the marked Pool A
simulator using its atomic `POST /admin/config`; wait for a fresh EPP sample,
Grid score/rank, distributed overlay, and Gateway revision; then pressure
traffic; restore the exact saved simulator configuration; wait for EPP/Grid/
overlay/Gateway recovery; and run recovery traffic. The direct simulator path
is clearly labeled as a reference, not an equivalent authorization or routing
path. No Dynamo is used.

Signal propagation remains recorded at each separate boundary:

1. simulator configuration update;
2. authenticated EPP sample;
3. Grid provider score/rank;
4. distributed overlay revision;
5. Gateway-consumed/serving revision;
6. first correlated request using the resulting route.

UTC timestamps, monotonic durations, sample age, and polling intervals are
retained separately. The report does not collapse them into one convergence
duration. The Gateway request's unique ID is a header, never a k6 metric tag.
Metric tags are bounded to phase, ingress, site, pool, and status class.
Requests with missing or inconsistent attribution remain in the full HTTP
denominator and fail the run; they are not retried away.

The run stops traffic and captures diagnostics at readiness loss, stale
metrics, endpoint loss, revision divergence, attribution failure, request
timeout, evidence limit, or load-generator saturation. It attempts restoration
on success, failure, SIGINT, and SIGTERM and verifies simulator values plus
EPP/Grid/overlay/Gateway recovery. If restoration is not proven, the report
marks the run failed and `gridzilla restore` can retry from the saved snapshot:

```sh
gridzilla restore --handoff /absolute/path/to/handoff.json
```

This restores simulator values only; it does not clean up cluster resources.
If a handoff/provider/Pod identity changed, Gridzilla fails closed instead of
mutating the replacement object.

## Reports and UI

Regenerate a Markdown report, machine-readable JSON, and SVG charts only from
saved evidence:

```sh
gridzilla report evidence/<run-directory>
```

Open the local read-only dashboard:

```sh
gridzilla serve --handoff /absolute/path/to/handoff.json \
  --runs-dir evidence --host 127.0.0.1 --port 8765
```

Then visit `http://127.0.0.1:8765/`. Without `--handoff`, saved-run browsing
still works:

```sh
gridzilla serve --runs-dir evidence
```

The live view distinguishes `LIVE`, `STALE`, `UNAVAILABLE`, and historical
data; separates the local overlay from the cross-site snapshot; and shows
signal, score/rank, revision, route, and resource state. It is read-only: the
UI has no pressure, start, stop, or restore controls and never sends
credentials to the browser. Saved-run mode reads evidence without cluster
access. The UI is bound to localhost only.

## Interpreting results and limits

- Offered arrival rate and completed HTTP rate are separate. Dropped
  iterations, status counts, timeout errors, response bytes, p50/p95/p99, and
  sample counts are retained per window.
- p50/p95/p99 are suppressed unless the profile's minimum sample count was
  reached. Small smoke runs are correctness checks, not latency measurements.
- A throughput claim is marked ineligible if host load samples are absent or
  show the local load generator saturated. Compare host and cluster resource
  samples before interpreting results.
- Route distribution is evaluated against the Grid scoring/selection policy;
  deterministic selection is not expected to distribute requests evenly.
- Pressure is a synthetic queue-depth signal in llm-d-sim. This is not GPU
  saturation, real model queueing, token throughput, or a capacity SLO.
- The simulator returns the echoed request ID and inference Pod name. Gridzilla
  validates those on Gateway responses to correlate selected site with the
  backend Pod; request IDs remain out of metric tags.
- Network latency and host/cluster clock skew can affect cross-process
  timestamps; UTC and process-local monotonic durations are both retained.
- `doctor`, `smoke`, and `report` do not start a benchmark. Only `run` applies
  temporary simulator pressure.

## Offline validation

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
npm ci
npm run build
npm run test:ui
```

Set `K6_BIN` to a checksum-verified pinned executable to include the local
mock-backed k6 smoke test. No live provider key or cluster is needed for these
offline checks.
