# Normalized report format design

The current dashboard and evidence adapter assume the Grid signals combined-site
scenario: three named sites, west/Pool A as the pressure target, the current
Grid attribution headers, an authenticated EPP queue-depth signal, and
llm-d-sim pressure. Those assumptions remain in the combined-site runner and
the dedicated live llm-d view. They do not belong in the saved report model.

`gridzilla.dev/normalized-report/v1alpha1` is the saved-run contract. A small
adapter maps today's `run-result.json`, `report-data.json`, `source-lock.json`,
`profile.json`, redacted handoff, and JSONL evidence into generic run metadata,
measurement windows, bounded routing dimensions, resources, and optional signal
observations. It does not change the benchmark runner or create a plugin
system. `gridzilla report <evidence-directory>` writes the normalized report
deterministically from saved evidence; the UI/API performs the same read-only
adaptation when older runs lack that file. Existing evidence files remain
untouched.

Routing is represented as named bounded dimensions with observed values and
explicit attribution quality; the report never assumes three sites or equal
traffic. Direct simulator-reference windows remain separate from Gateway
windows. Missing measurements stay unavailable, and percentile eligibility is
carried with each window. Comparisons match scenario, profile name/version,
phase, and offered rate, then show repeat-level differences without claiming
statistical significance. Topology, source/image, host-capacity, and sample
eligibility differences remain visible as caveats.

The optional `signals` section adapts the existing llm-d-sim → EPP → Grid →
overlay → Gateway boundaries and low-rate correlated probes. UTC observation
times and process-local monotonic durations remain separate. Probe identifiers
are evidence fields, never Prometheus labels. The generic overview works when
that section is absent.
