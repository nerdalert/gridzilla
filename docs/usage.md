# Gridzilla usage

Gridzilla has two scenarios that use the same profile validation, k6 traffic
engine, and normalized report model:

- **Generic endpoint** measures bounded HTTP JSON POST behavior at a
  server-configured URL. It has no Kubernetes or Grid prerequisite and does
  not infer synthetic pressure, route attribution, or restoration.
- **Grid / llm-d signals** attaches to the combined-site setup handoff. It
  retains ownership and image checks, authenticated EPP freshness, provider
  readiness, distributed overlay and Gateway revision agreement, attributed
  probes, and simulator pressure/recovery.

Both scenarios record offered and achieved requests/second, status/error
counts, timeouts, dropped iterations, response bytes, qualified latency
percentiles, requested and verified request-body bytes, and host observations.
Neither scenario is a GPU capacity, model-quality, or real token-throughput
benchmark. Generic results are observed HTTP behavior for the chosen workload;
Grid results are control-path behavior under synthetic llm-d-sim pressure.
k6 numbers are not directly comparable with historical Vegeta numbers just
because request body sizes match.

## Generic HTTP JSON endpoint

### Configuration

Endpoint definitions are trusted files loaded from a server-owned directory.
The API accepts their IDs only; it never accepts an endpoint URL, shell
command, profile path, credential, or CA path from the browser or agent.
Configuration supports:

- An HTTP(S) JSON POST URL and JSON request template.
- An optional JSON Pointer to an existing string field that can be padded.
- Optional credential and CA file paths. Their contents remain server-side.
- Expected success statuses and up to eight response-header dimensions, each
  with a short allowlist of values.
- An optional direct reference endpoint. Its measurements remain separate
  from the configured endpoint and are never merged into its throughput.

For example, `examples/endpoints/openai-chat.json` uses an OpenAI
chat-completions request and pads `/messages/0/content`. OpenAI fields are not
required: the generic JSON example is any JSON object with an explicit string
padding field when a request-size sweep is selected. Unsupported pointers and
templates that cannot fit a requested serialized size fail before traffic.
Never put prompts, request bodies, or secret values in a configuration intended
for source control.

List configurations, validate one configuration/profile pair, and run it:

```sh
gridzilla endpoint list --config-dir examples/endpoints
gridzilla endpoint preflight --config-dir examples/endpoints \
  --id example-openai-chat --profile profiles/body-sizes.json
gridzilla run --endpoint-config examples/endpoints/openai-chat.json \
  --profile profiles/body-sizes.json --evidence-root evidence
```

The `body-sizes` profile runs 16, 64, and 256 KiB serialized JSON bodies,
three measurement repeats for each size, and the profile warmup. The byte size
is measured on the exact compact UTF-8 JSON string passed to k6's HTTP client,
including punctuation and JSON syntax. k6 constructs deterministic padding
once during VU initialization and reuses the serialized body for that VU's
requests. The padded body is not put in an environment variable or evidence.
Each measurement window stores requested bytes, verified bytes, and sample
count, including a partial or failed window when evidence was produced.

### Bounded planning

One `plan_profile` calculation is used when validating a profile, applying UI
or API overrides, returning preflight output, and setting expected progress
windows. It accounts for body sizes × rates × repeats × phases, warmups,
optional reference traffic, offered requests, elapsed time, estimated evidence,
and a conservative request-body memory estimate. Profiles and overrides are
rejected before traffic when they exceed configured caps. k6 and the runner
also enforce request timeouts, response-size limits, evidence bounds, and
stop/failure handling. The estimate is not an allocation guarantee; the
runner samples the load-generator host during the run.

## Grid / llm-d signals

### Attach to a persistent setup

Gridzilla does not provision or delete clusters. A Grid checkout creates the
environment and writes a run-owned handoff:

```sh
cargo xtask env start-grid-combined-site-signals \
  --run-id grid-signals-<unique-suffix>
gridzilla doctor --handoff /absolute/path/to/handoff.json
gridzilla smoke --handoff /absolute/path/to/handoff.json --count 3
gridzilla run --handoff /absolute/path/to/handoff.json \
  --profile profiles/baseline.json
```

The producer contract is `gridzilla.dev/v1alpha1`; see
[`handoff-schema.md`](handoff-schema.md) and
[`examples/handoff.redacted.json`](../examples/handoff.redacted.json). The
real handoff and adjacent `run-manifest.json` must agree on the run ID,
cluster names, ownership prefix, and evidence location. The handoff records
explicit kubeconfig/context pairs, endpoints, resources, credential paths,
source revisions, and immutable image identities. Secret contents are not
included. `doctor` also verifies provider availability and queue capacity,
ready simulator Pods, authenticated fresh EPP metrics, overlay candidates,
Gateway accepted/serving revisions, endpoint reachability, host resources, and
tool/image identities. A failed gate exits nonzero. It does not change
simulator settings.

The baseline smoke sends a bounded number of authenticated, uniquely
identified requests. It checks status and route attribution, provider/pool/site
mappings, a request-correlated simulator response receipt, and readiness
again. It writes no request body or credential.

### Grid signal benchmark behavior

Profiles are versioned JSON in `profiles/`. They bound rate, warmup, window,
repeat count, VUs/concurrency, timeout, response bytes, total offered traffic,
runtime, evidence volume, body sizes, and percentile sample minimum. The
`body-sizes` profile can also be used with Grid, but it runs the full Grid
baseline, optional direct simulator reference, pressure, and recovery phases
for each size. Its planned windows and duration are shown by preflight; reduce
rates/window or use the usual `baseline` profile when that total is too long.

The full Grid sequence is:

1. Run healthy readiness gates and an attributed baseline smoke.
2. Measure baseline traffic for every configured rate and body size.
3. Measure an optional direct simulator/provider reference separately from
   Gateway traffic.
4. Snapshot the marked simulator's original configuration, then change its
   fake waiting-request metric through its supported atomic admin API.
5. Wait for a fresh authenticated EPP sample, new Grid score/rank, distributed
   overlay, Gateway consumption, and an attributed request using the new
   selection. Measure pressure traffic.
6. Restore the captured simulator configuration and verify fresh EPP/Grid/
   overlay/Gateway recovery plus a correlated request. Measure recovery
   traffic.

The direct simulator reference is a diagnostic comparison path, not an
equivalent client-authentication or Gateway-routing path. Synthetic queue
pressure does not enqueue actual requests. No Dynamo path is used. Signal
boundaries keep UTC timestamps and process-local monotonic durations separate;
Gridzilla does not collapse cross-process timestamps into a single latency.

The environment remains running after a benchmark. If a setup owner intends
to delete it, use that setup's explicit cleanup command after checking its
manifest, for example:

```sh
cargo xtask env clean-grid-combined-site-signals \
  --manifest evidence/grid-signals-combined-site/<run-id>/run-manifest.json
```

That command belongs to the Grid setup repository, not Gridzilla.

## Reports, comparison, and saved evidence

Every run writes a unique directory under its configured evidence root.
`gridzilla report <directory>` regenerates the Markdown report, normalized
JSON, and charts from saved files only; it does not contact the endpoint or
cluster. The normalized format records scenario, topology, profile, endpoint
identity, phases/windows, body sizes, response dimensions or Grid attribution,
resource samples, missing-data markers, and claim eligibility. Older reports
without body-size fields display “unavailable.”

The UI compares windows only when scenario, exact phase, traffic path, offered
rate, requested body size, and repeat match. It shows repeats independently,
includes sample counts, and warns about topology, image/source, host capacity,
or eligibility differences. It does not claim statistical significance from
a few repeats. Generic response-header dimensions are bounded observations,
not route proofs. Grid route attribution remains in the Grid-specific view.

Saved evidence includes k6 raw samples and summaries, window accounting,
bounded probes, source/image locks, host/workload resource observations, and
Grid signal/restoration evidence where applicable. Request bodies, response
bodies, credential contents, request IDs as metric labels, arbitrary URLs as
metric labels, and image digests as metric labels are not saved/exported.

## Local dashboard and JSON agent API

Build the UI, then serve the read-only dashboard. Generic endpoint
configurations can be loaded without a Grid handoff:

```sh
npm ci
npm run build
gridzilla serve --runs-dir evidence --host 127.0.0.1 --port 8765 \
  --endpoints-dir examples/endpoints
```

Add a Grid handoff to enable its live signal view. Controls remain disabled
unless `--allow-controls` is explicitly supplied. When enabled, the UI and API
still call the same server-side validators and scenario runners.

For an agent, create a private bearer token file. The token must have at least
32 bytes and on POSIX systems the file must be mode `0600`:

```sh
umask 077
agent_token_file="$HOME/.local/state/gridzilla/agent-token"
mkdir -p "$(dirname "$agent_token_file")"
openssl rand -hex 32 > "$agent_token_file"
chmod 600 "$agent_token_file"
gridzilla serve --runs-dir evidence --host 127.0.0.1 --port 8765 \
  --allow-controls --endpoints-dir examples/endpoints \
  --agent-token-file "$agent_token_file"
```

The server remains bound to loopback. Browser POSTs must be same-origin. Agent
POSTs use the bearer token and omit `Origin`; a token does not override a
cross-origin browser `Origin`. Do not put token values in logs or evidence.

### Agent workflow

The API is local JSON over the same server, configuration registry, profile
planner, preflight, and runner used by the dashboard. Read-only discovery and
saved-report routes work without a control token on loopback. Control POSTs
require same-origin browser access or the configured local-agent token.

```sh
base=http://127.0.0.1:8765
token="$(cat "$agent_token_file")"
auth_header="Authorization: Bearer $token"

# 1. Discover validated scenarios, endpoint IDs, and server-loaded profiles.
curl --fail --silent "$base/api/configurations"

# 2. Preflight the selected generic endpoint and size plan.
curl --fail --silent -X POST "$base/api/preflight" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"scenario":"generic-endpoint","configurationId":"example-openai-chat","profile":"body-sizes","rates":"10","soakSeconds":15,"requestBodySizesBytes":[16384,65536,262144]}'

# 3. Start only a server-listed endpoint/profile combination and retain its operation ID.
started=$(curl --fail --silent -X POST "$base/api/run" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"scenario":"generic-endpoint","configurationId":"example-openai-chat","profile":"body-sizes","rates":"10","soakSeconds":15,"requestBodySizesBytes":[16384,65536,262144]}')
operation_id=$(printf '%s' "$started" | python3 -c 'import json,sys; print(json.load(sys.stdin)["operationId"])')

# 4. Poll this operation ID; status is RUNNING, PASS, FAIL, or STOPPED.
curl --fail --silent "$base/api/operations/$operation_id"

# 5. On interruption, stop only this run. Grid runs restore simulator state first.
curl --fail --silent -X POST "$base/api/stop" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$operation_id\"}"

# 6. List saved runs, fetch the selected normalized report, and download files.
curl --fail --silent "$base/api/runs"
curl --fail --silent "$base/api/runs/<run-id>"
curl --fail --silent "$base/api/runs/<run-id>/files/report.md"
```

For Grid-only temporary pressure, select a site/value from the server-provided
`pressureOptions` in `/api/state`, then call `POST /api/pressure` with only
`{"site":"west","value":2}`. Save the returned `operationId`. It restores
the captured simulator settings automatically after 30 seconds; to request
earlier restoration call `POST /api/pressure/restore` with that exact operation
ID. A benchmark cannot overlap a pressure operation. The API exposes no
simulator URL, shell command, credential, or arbitrary pressure path.

Operations are persisted as small status records below
`<runs-dir>/.operations/` so an agent can poll final status after the worker
finishes. Detailed failures stay in run evidence; the status API returns a
safe summary and report/run ID. Old operation records are capped in memory at
128 while saved evidence remains available through the runs API.

## Prometheus and Perses

The optional local stack reads only Gridzilla's loopback `/metrics`. It does
not receive kubeconfig or cluster credentials. Start it from this repository:

```sh
docker compose -f compose.yaml up -d
```

Prometheus and Perses bind to localhost and persist data in local Docker
volumes. Dashboards distinguish synthetic simulator settings from measured
EPP queue and HTTP traffic. Missing/stale observations are unavailable, not
zero. Latency percentiles are omitted until the configured sample minimum is
reached. Metric labels use bounded scenario, phase, traffic path, body-size
class, and configured routing/response dimensions; never request IDs,
credentials, prompt text, arbitrary URLs, or image digests.

Stop only this local observability stack with:

```sh
docker compose -f compose.yaml down
```

This does not stop a Gridzilla server or alter the persistent Kind environment.

## Claim and compatibility limits

- A generic run reports observed HTTP behavior for its endpoint, request
  template, body sizes, rate, and duration. It makes no Grid or backend
  attribution claim unless that is separately configured and observed.
- A Grid run proves the configured routing/control path and signal response
  against the recorded synthetic simulator environment. Synthetic queue depth
  is not actual queued traffic.
- Neither scenario establishes GPU saturation capacity, model quality, or real
  token throughput. A small number of repeats does not establish statistical
  significance.
- Direct-reference traffic remains a separate path and result.
- Missing measurements, old reports without sizes, or incomplete provenance
  remain unavailable; they are not converted into zeros or fabricated passes.
