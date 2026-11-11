# Implementation plan: two Gridzilla scenarios

## Existing assumptions

The current flow is Grid-specific end to end: a three-site setup handoff, west/Pool A pressure, an authenticated EPP and overlay gate, fixed attribution headers, and a direct simulator reference. Profile duration/evidence arithmetic is repeated between profile loading and dashboard overrides. The k6 script already supplies the common open-loop request engine, while report normalization and saved-run browsing already have a generic layer.

## Design

Keep two explicit orchestration paths and one traffic contract:

- **Grid / llm-d signals:** preserve the current handoff validation, ownership and image checks, readiness and attribution probes, simulator-pressure phases, restoration, and signal report. Adapt it to a common `TrafficSpec` and shared profile planner without changing its gates.
- **Generic endpoint:** load a server-owned endpoint definition from a configured directory; validate its URL, JSON template, string padding path, credentials/CA paths, accepted statuses, and bounded response-header dimensions. Its preflight verifies local configuration and reachability, then it runs baseline measurement windows only. No cluster, Grid, simulator, attribution receipt, or pressure behavior is inferred.
- **Shared traffic:** one k6 script and runner accept a validated endpoint, serialized JSON template, optional byte target, auth/CA, success statuses, and bounded dimensions. Padding is deterministic, created once in k6 initialization, and checked against the exact UTF-8 bytes sent. The runner records requested and verified body bytes per window without recording request content.
- **Shared planning/reporting:** one planning function computes windows, duration, offered volume, evidence and memory bounds for profile loading, API/UI overrides, and progress. The normalized report gains scenario/endpoint metadata and body-size fields while old Grid reports remain readable.
- **Controls/API:** configuration discovery, preflight, start, operation-specific status/stop, report/evidence retrieval, and Grid-only pressure/restore use the same validated controls as the UI. The local API keeps browser same-origin checks and adds a separate configured local-agent token path; endpoint URLs, credentials, commands, and profile paths are never accepted from requests.

The existing Grid dashboard remains specialized, with generic run/report views shared across both scenarios. The shipped size profile targets 16, 64, and 256 KiB serialized requests. A mock endpoint proves exact received sizes and preserved JSON fields before any performance claim.
