# Selective reuse matrix

Reviewed repository: `https://github.com/nerdalert/maas-benchmark`, official
`main` at `e3467b377d29a882e93ef1dff437d6935a0ea11d` (read-only clone under
`reference/maas-benchmark`). This is an implementation-pattern review, not a
claim that MaaS's routes, authentication, or thresholds apply to Grid.

| Pattern | Reuse | Gridzilla adaptation |
| --- | --- | --- |
| k6 `constant-arrival-rate` executor | Yes | Use per-window fixed arrival rates, warmup, and three independent windows; report offered and achieved rates separately. |
| Bounded burst/concurrency scenarios | Selectively | Bound VUs and request duration in versioned profiles; do not copy the unbounded/high-VU examples. |
| Configurable workload profiles | Yes | Store profiles as validated JSON with hard maximums for rate, VUs, duration, timeout, and output tokens/bytes. |
| `--summary-export` / summary JSON | Yes | Save a summary for every independent window and regenerate aggregate results from those files. |
| Raw k6 metric output | Yes, with independent receipt probes | Retain k6 JSON output for offered/achieved iteration and HTTP samples; correlated low-rate requests carry IDs and are reconciled against backend receipts separately. |
| MaaS host/model/token setup | No | Grid Gateway URL, request model/body, and credential-file path come from the versioned setup handoff. |
| Service-account token creation and OpenShift policy patching | No | Handoff owns credentials and cluster setup; Gridzilla only reads the referenced token file and makes no cluster mutations besides simulator admin API pressure/reset. |
| Existing `scripts/run-test.sh` | No | It assembles a command and executes the string with `eval`; Gridzilla passes a fixed argv array to `k6` and never shells through user profile values. |
| Fixed latency thresholds | No | No implicit SLO; profiles can declare an explicit SLO, and reports label it as user-selected. |

The reference `k6/maas-performance-test.js` uses `constant-arrival-rate` for its
soak scenarios and emits summary data, but its payload and MaaS-specific route
are not reused. The separate Grid controller owns readiness, signal transition,
resource collection, and evidence orchestration; k6 only generates bounded HTTP
traffic.
