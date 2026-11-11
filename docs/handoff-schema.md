# Combined-site handoff v1alpha1

Gridzilla consumes the run-owned file written by Grid's
`start-grid-combined-site-signals` xtask. The current schema identifier is
`gridzilla.dev/v1alpha1`. The producer remains authoritative for cluster,
resource, source, and image identities; Gridzilla's normalizer adapts the
producer's serialized camelCase fields into its internal representation.

The file contains no token or credential values. It records paths to the client
credential, authenticated EPP metric tokens, optional CA files, per-run
kubeconfigs, and the image-lock JSON. Keep the file and those paths private to
the host that owns the run. A redacted shape is provided in
[`../examples/handoff.redacted.json`](../examples/handoff.redacted.json).
The setup's adjacent `run-manifest.json` is also required: Gridzilla verifies
its schema, ready lifecycle, run ID, evidence directory, exact cluster names,
and run-owned prefix before attaching.

## Required fields

| Field | Contract and validation |
|---|---|
| `schemaVersion`, `runId`, `createdAt` | Exact schema ID; DNS-safe unique setup run ID; UTC creation time. |
| `source` | Grid repository/revision and dirty-state fingerprint when source is dirty. Revision is a full commit SHA. |
| `runtimeImages[]` | Site, component, namespace, selector, container, image reference, immutable `imageId`, optional full source revision. |
| `imageLock` | Path to setup's pre-provision image lock. Runtime image refs must occur in its `images[]`; image IDs must include immutable SHA-256 identities. |
| `client` | West/client Gateway URL, `requestPath`, request credential file, model/request contract, request ID and auth headers, route attribution headers, and the simulator's echoed request ID and Pod identity receipt. |
| `backendReference` | Direct deterministic simulator reference endpoint and request path; explicitly not the same security/routing path as Gateway traffic. |
| `pressureTargetPool` | `pool-a` for the default queue-pressure test. |
| `sites[]` | Exactly three unique run-owned Kind clusters with kubeconfig/context, workload readiness targets, provider/pool/capacity, EPP access, simulator targets, overlay, and Gateway identity. |

Each site must have `ownerRunId == runId`; cluster names must carry the run-owned prefix. Each provider's queue capacity must be positive and sourced from active
Grid policy. Exactly one provider must set `pressureTarget: true`, and its pool must equal the profile's explicit `pressureTargetPool`. The setup currently
requires the marked provider to be west/Pool A and rank 0 on the west Gateway before pressure; Gridzilla fails closed if the actual current selection differs.
This marked target is used by the complete CLI/UI benchmark. The dashboard's
separate manual pressure buttons are derived per region from each site's
simulator targets and queue-capacity scale; they do not change the benchmark's
configured pressure target.
Setup-provided `localPort` values are hints: the environment may remain idle while other host processes claim those ports. Gridzilla asks kubectl to bind an
ephemeral loopback port and records the assigned value in the attached redacted handoff and per-forward logs.

Every simulator Pod is recorded separately with namespace, name, UID,
configuration generation, admin URL, and a local-only port-forward. Gridzilla
rechecks the Pod UID/readiness before mutation and restoration. EPP metrics
must be fetched with the named bearer-file path. The selected queue sample
must have an allowed pool label and be fresh; a live authenticated scrape with
no Prometheus sample timestamp is labeled `authenticated-live-scrape-only`,
not falsely described as a timestamped sample.

The combined-site response exposes these routing and backend fields:

- `x-grid-combined-provider-gateway`
- `x-ai-demo-provider-gateway`
- `x-ai-inference-provider`
- `x-inference-pod`
- `x-request-id`

Gridzilla requires both provider Gateway fields to agree on the selected site,
`x-ai-inference-provider` to identify the provider route, and the simulator Pod
name to match the selected site. The simulator is configured to echo the
incoming `X-Request-Id`; Gridzilla requires that echo to equal the unique
request ID sent by the client. This is a request-correlated backend response
receipt, not a separate receipt API. The request ID is never a k6 metric tag.

## Validation behavior

`doctor`, `smoke`, and `run` all load and validate the handoff before acting.
Validation checks actual path existence for kubeconfigs, token files, CA files,
the image lock, and the ready ownership manifest; it rejects credential-bearing
endpoint URLs and query strings. A redacted
template's placeholder paths are intentionally nonfunctional: replace them
with the real setup output before running `gridzilla doctor`.

The contract normalization is covered by `tests/test_contract_and_results.py`
using synthetic temporary files and test-only credentials. It asserts the
producer field aliases, trusted response-header set, image provenance lock,
Pool A marking, and per-pod port-forward uniqueness. The pinned-k6 mock smoke
test exercises a valid request/response attribution set end to end without any
Kind cluster.
