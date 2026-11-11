"""Low-cardinality Prometheus exposition for current Gridzilla observations."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import time
from pathlib import Path
from typing import Any


_SAFE_LABEL = re.compile(r"^[a-zA-Z0-9_.-]{1,40}$")
_PHASES = {"baseline", "pressure", "recovery", "direct-simulator-reference", "direct-reference"}
_STATUSES = {"LIVE", "STALE", "UNAVAILABLE"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def live_observation_is_fresh(live: dict[str, Any], *, now: float | None = None,
                              max_age_seconds: float = 15.0) -> bool:
    """Return true only for a complete LIVE snapshot inside its age budget."""
    if live.get("status") != "LIVE":
        return False
    observed_at = _timestamp(live.get("observedAtUtc"))
    current = time.time() if now is None else now
    return observed_at is not None and -5 <= current - observed_at <= max_age_seconds


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(lines: list[str], name: str, value: float, labels: dict[str, str] | None = None) -> None:
    if not math.isfinite(value):
        return
    pairs = []
    for key, raw in sorted((labels or {}).items()):
        if not _SAFE_LABEL.fullmatch(key) or not _SAFE_LABEL.fullmatch(raw):
            return
        pairs.append(f'{key}="{_escape(raw)}"')
    suffix = "{" + ",".join(pairs) + "}" if pairs else ""
    lines.append(f"{name}{suffix} {value:.12g}")


def _family(lines: list[str], name: str, kind: str, help_text: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tail_jsonl(path: Path, limit: int = 2_000_000) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            data = stream.read(limit).decode("utf-8", errors="replace")
        return [json.loads(line) for line in data.splitlines()[-2000:] if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def _active_run(evidence_root: Path, setup_run_id: str, now: float) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    try:
        directories = list(evidence_root.iterdir())
    except OSError:
        return None
    for directory in directories:
        if not directory.is_dir() or directory.name != directory.name.replace("/", ""):
            continue
        state = _read_json(directory / "live-state.json")
        if not isinstance(state, dict) or state.get("status") != "RUNNING":
            continue
        if setup_run_id and state.get("setupRunId") != setup_run_id:
            continue
        if not setup_run_id and state.get("scenario") != "generic-endpoint":
            continue
        updated = _timestamp(state.get("updatedAtUtc"))
        if updated is None or now - updated < -5 or now - updated > 20:
            continue
        candidates.append((updated, directory, state))
    if not candidates:
        return None
    _updated, directory, state = max(candidates, key=lambda item: item[0])
    return directory, state


def _active_benchmark_metrics(lines: list[str], evidence_root: Path, setup_run_id: str,
                              allowed_sites: set[str], pool_by_site: dict[str, str], now: float) -> None:
    active = _active_run(evidence_root, setup_run_id, now)
    if active is None:
        return
    directory, state = active
    phase = state.get("phase")
    labels = {"phase": phase} if phase in {
        "preflight", "baseline", "pressure-applying", "pressure-convergence", "pressure-measurement",
        "restoring", "recovery-measurement",
    } else None
    _family(lines, "gridzilla_benchmark_run_active", "gauge", "One while a benchmark has a fresh running heartbeat.")
    _sample(lines, "gridzilla_benchmark_run_active", 1, labels)

    windows = [row for row in _tail_jsonl(directory / "windows.jsonl")
               if row.get("type") == "measurement" and row.get("phase") in _PHASES
               and isinstance(row.get("accounting"), dict)]
    latest_by_phase: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in windows:
        phase_name = str(row["phase"])
        path_name = str(row.get("trafficPath") or ("direct-reference" if row.get("reference") else
                                                     "generic-endpoint" if row.get("scenario") == "generic-endpoint" else "gateway"))
        body_bytes = row.get("verifiedBodyBytes") or row.get("requestedBodyBytes")
        size_class = {16384: "16KiB", 65536: "64KiB", 262144: "256KiB"}.get(body_bytes, "other" if body_bytes else "unavailable")
        latest_by_phase[(phase_name, path_name, size_class)] = row
    profile = _read_json(directory / "profile.json")
    minimum = profile.get("percentileMinimumSamples") if isinstance(profile, dict) else None
    minimum_samples = int(minimum) if isinstance(minimum, (int, float)) and minimum > 0 else None

    metric_names = {
        "offered": "gridzilla_benchmark_offered_requests_per_second",
        "achieved": "gridzilla_benchmark_achieved_requests_per_second",
        "requests": "gridzilla_benchmark_http_requests",
        "errors": "gridzilla_benchmark_http_errors",
        "dropped": "gridzilla_benchmark_dropped_iterations",
        "latency": "gridzilla_benchmark_latency_milliseconds",
        "samples": "gridzilla_benchmark_latency_sample_count",
        "minimum": "gridzilla_benchmark_percentile_minimum_samples",
        "route": "gridzilla_benchmark_route_attributed_requests",
        "route_dimension": "gridzilla_benchmark_route_attributed_requests_by_dimension",
        "timestamp": "gridzilla_benchmark_window_completed_timestamp_seconds",
        "body_bytes": "gridzilla_benchmark_request_body_bytes",
    }
    descriptions = {
        "offered": "Configured constant-arrival rate for the latest completed window of the active run.",
        "achieved": "Completed HTTP requests per second in the latest completed window of the active run.",
        "requests": "Completed HTTP requests in the latest completed window of the active run.",
        "errors": "Non-success HTTP responses in the latest completed window of the active run.",
        "dropped": "Dropped k6 iterations in the latest completed window of the active run.",
        "latency": "Sample-size-qualified latency percentile in milliseconds from the active run's latest completed window.",
        "samples": "Latency sample count in the active run's latest completed window.",
        "minimum": "Minimum latency samples required before exporting percentiles for the active run.",
        "route": "Observed attributed Gateway requests by site in the active run's latest completed window; absent site samples remain unavailable.",
        "route_dimension": "Observed attributed Gateway requests by bounded routing dimension and value; absent observations are unavailable.",
        "timestamp": "Unix timestamp of the latest completed measurement window in the active run.",
        "body_bytes": "Requested and k6-verified serialized UTF-8 JSON request-body bytes for the latest active-run window; body-size labels are fixed classes.",
    }
    for key, name in metric_names.items():
        _family(lines, name, "gauge", descriptions[key])

    scenario = "generic-endpoint" if state.get("scenario") == "generic-endpoint" else "grid-llmd-signals"
    for (phase_name, traffic_path, size_class), row in latest_by_phase.items():
        account = row["accounting"]
        phase_labels = {"scenario": scenario, "phase": phase_name,
                        "traffic_path": traffic_path, "body_size": size_class}
        rate = _number(row.get("ratePerSecond"))
        elapsed = _number(row.get("elapsedSeconds"))
        requests = _number(account.get("httpRequests"))
        dropped = _number(account.get("droppedIterations"))
        if rate is not None and 0 < rate <= 100:
            _sample(lines, metric_names["offered"], rate, phase_labels)
        if requests is not None:
            _sample(lines, metric_names["requests"], requests, phase_labels)
        if requests is not None and elapsed is not None and elapsed > 0:
            _sample(lines, metric_names["achieved"], requests / elapsed, phase_labels)
        if dropped is not None:
            _sample(lines, metric_names["dropped"], dropped, phase_labels)
        verified_bytes = _number(row.get("verifiedBodyBytes") or account.get("verifiedBodyBytes"))
        if verified_bytes is not None:
            _sample(lines, metric_names["body_bytes"], verified_bytes, phase_labels)
        statuses = account.get("statusCounts")
        if isinstance(statuses, dict):
            errors = sum(_number(count) or 0 for status, count in statuses.items()
                         if not str(status).startswith(("2", "3")))
            _sample(lines, metric_names["errors"], errors, phase_labels)
        latency = account.get("latencyMs", {})
        sample_count = _number(latency.get("sampleCount"))
        if sample_count is not None:
            _sample(lines, metric_names["samples"], sample_count, phase_labels)
        if minimum_samples is not None:
            _sample(lines, metric_names["minimum"], minimum_samples, phase_labels)
            if sample_count is not None and sample_count >= minimum_samples:
                for quantile in ("p50", "p95", "p99"):
                    value = _number((latency.get(quantile) or {}).get("value"))
                    if value is not None:
                        _sample(lines, metric_names["latency"], value,
                                {**phase_labels, "quantile": quantile})
        if requests is not None and requests > 0 and phase_name != "direct-simulator-reference":
            raw_dimensions = account.get("routeCountsByDimension")
            dimensions = raw_dimensions if isinstance(raw_dimensions, dict) else {
                "site": account.get("routeCountsBySite", {})}
            endpoint_config = _read_json(directory / "endpoint.redacted.json") if scenario == "generic-endpoint" else {}
            generic_dimensions = endpoint_config.get("responseHeaderDimensions", {}) if isinstance(endpoint_config, dict) else {}
            # Grid uses the handoff-owned site/pool vocabulary. Generic
            # dimensions are limited to values in server-loaded configuration.
            emitted = 0
            for dimension, values in sorted(dimensions.items()):
                if not isinstance(values, dict):
                    continue
                if scenario == "generic-endpoint":
                    if dimension not in generic_dimensions:
                        continue
                    allowed = set(generic_dimensions[dimension].get("allowedValues", [])) | {"unknown"}
                elif dimension not in {"site", "pool"}:
                    continue
                for value_name, raw_count in sorted(values.items()):
                    if scenario == "generic-endpoint" and value_name not in allowed:
                        continue
                    if scenario == "grid-llmd-signals" and dimension == "site" and value_name not in allowed_sites:
                        continue
                    if scenario == "grid-llmd-signals" and dimension == "pool" and value_name not in set(pool_by_site.values()):
                        continue
                    count = _number(raw_count)
                    if count is None or count < 0:
                        continue
                    _sample(lines, metric_names["route_dimension"], count,
                            {**phase_labels, "dimension": str(dimension), "value": str(value_name)})
                    if scenario == "grid-llmd-signals" and dimension == "site":
                        _sample(lines, metric_names["route"], count, {**phase_labels, "site": str(value_name)})
                        pool = pool_by_site.get(str(value_name))
                        if pool and "pool" not in dimensions:
                            _sample(lines, metric_names["route_dimension"], count,
                                    {**phase_labels, "dimension": "pool", "value": pool})
                    emitted += 1
                    if emitted >= 64:
                        break
                if emitted >= 64:
                    break
        stamp = _timestamp(row.get("finishedAtUtc"))
        if stamp is not None:
            _sample(lines, metric_names["timestamp"], stamp, phase_labels)

    resource_rows = _tail_jsonl(directory / "resource-samples.jsonl", limit=1_000_000)
    if resource_rows:
        latest = resource_rows[-1]
        sampled_at = _timestamp(latest.get("observedAtUtc"))
        # Resource sampling is normally every 10 seconds. A still-running
        # benchmark heartbeat must not make an old snapshot look live.
        if sampled_at is None or now - sampled_at > 20 or now - sampled_at < -5:
            return
        host = latest.get("host", {})
        _family(lines, "gridzilla_host_available_memory_bytes", "gauge", "Available memory on the local benchmark host during the active run.")
        _family(lines, "gridzilla_host_cpu_saturated", "gauge", "One when the local benchmark host load exceeded its CPU count in the latest active-run sample.")
        mem = _number(host.get("memoryAvailableBytes"))
        if mem is not None:
            _sample(lines, "gridzilla_host_available_memory_bytes", mem)
        saturated = latest.get("loadGeneratorSaturated")
        if isinstance(saturated, bool):
            _sample(lines, "gridzilla_host_cpu_saturated", 1 if saturated else 0)
        _family(lines, "gridzilla_cluster_cpu_cores", "gauge", "CPU cores used by observed Grid runtime components during the active run.")
        _family(lines, "gridzilla_cluster_memory_bytes", "gauge", "Bytes of memory used by observed Grid runtime components during the active run.")
        for cluster in latest.get("clusters", []):
            site = cluster.get("site")
            if site not in allowed_sites:
                continue
            cpu = 0.0
            memory = 0
            cpu_found = False
            memory_found = False
            for component in cluster.get("componentUsage", {}).values():
                for pod in component.get("pods", []):
                    value = _number(pod.get("cpuCores"))
                    if value is not None:
                        cpu += value
                        cpu_found = True
                    value = _number(pod.get("memoryBytes"))
                    if value is not None:
                        memory += value
                        memory_found = True
            if cpu_found:
                _sample(lines, "gridzilla_cluster_cpu_cores", cpu, {"site": site})
            if memory_found:
                _sample(lines, "gridzilla_cluster_memory_bytes", memory, {"site": site})


def render_metrics(live: dict[str, Any], handoff: dict[str, Any] | None,
                   evidence_root: Path, *, now: float | None = None,
                   max_live_age_seconds: float = 15.0) -> str:
    """Render only current observations and fresh active-run windows, never saved reports."""
    timestamp = time.time() if now is None else now
    lines = ["# HELP gridzilla_metrics_endpoint Gridzilla local metrics exporter is responding.",
             "# TYPE gridzilla_metrics_endpoint gauge", "gridzilla_metrics_endpoint 1"]
    if not isinstance(handoff, dict):
        _active_benchmark_metrics(lines, evidence_root, "", set(), {}, timestamp)
        return "\n".join(lines) + "\n"
    site_specs = [site for site in handoff.get("sites", []) if isinstance(site, dict)]
    known_sites = {str(site.get("name")) for site in site_specs
                   if isinstance(site.get("name"), str) and _SAFE_LABEL.fullmatch(site["name"])}
    pool_by_site = {str(site.get("name")): str(site.get("provider", {}).get("pool")) for site in site_specs
                    if str(site.get("name")) in known_sites
                    and isinstance(site.get("provider"), dict)
                    and isinstance(site.get("provider", {}).get("pool"), str)
                    and _SAFE_LABEL.fullmatch(str(site["provider"]["pool"]))}

    _family(lines, "gridzilla_live_observation_status", "gauge", "One-hot state of the latest complete live Gridzilla observation; no series means no complete observation.")
    state_status = live.get("status")
    observed_timestamp = _timestamp(live.get("observedAtUtc"))
    fresh = live_observation_is_fresh(live, now=timestamp, max_age_seconds=max_live_age_seconds)
    export_status = state_status if state_status in _STATUSES and (state_status != "LIVE" or fresh) else (
        "STALE" if state_status == "LIVE" and observed_timestamp is not None else "UNAVAILABLE")
    _sample(lines, "gridzilla_live_observation_status", 1, {"status": export_status})
    observed_at = _timestamp(live.get("observedAtUtc")) if len(live.get("sites", [])) == len(known_sites) else None
    if observed_at is not None:
        _family(lines, "gridzilla_live_observation_timestamp_seconds", "gauge", "Unix timestamp of the latest complete Gridzilla live observation.")
        _sample(lines, "gridzilla_live_observation_timestamp_seconds", observed_at)
    if not fresh:
        return "\n".join(lines) + "\n"

    families = {
        "queue": ("gridzilla_epp_queue_size", "EPP average waiting-request queue signal per endpoint, sampled live; this is measured EPP state, not synthetic simulator pressure."),
        "capacity": ("gridzilla_queue_capacity_scoring_scale", "Configured queue-depth score normalization scale from the live run handoff."),
        "ready": ("gridzilla_epp_ready_endpoints", "Ready endpoints reported by the authenticated EPP scrape."),
        "epp_age": ("gridzilla_epp_sample_age_seconds", "Age of the EPP metric sample when the authenticated scrape supplied a source timestamp; absent means age is unavailable."),
        "epp_scrape": ("gridzilla_epp_scrape_available", "One when Gridzilla has a current successful authenticated EPP metrics observation."),
        "synthetic": ("gridzilla_simulator_synthetic_waiting_requests", "Synthetic waiting-request setting configured in llm-d-sim fake metrics; this is not measured request traffic."),
        "available": ("gridzilla_provider_available", "One or zero for the observed InferenceProvider availability condition."),
        "score": ("gridzilla_candidate_score", "Current Grid score for a candidate in the live overlay."),
        "rank": ("gridzilla_candidate_rank", "Current Grid rank for a candidate in the live overlay; lower ranks are preferred."),
        "accepted": ("gridzilla_gateway_overlay_accepted_revision_match", "One when Gateway accepted revision equals the current overlay; zero when both are observed and differ."),
        "serving": ("gridzilla_gateway_overlay_serving_revision_match", "One when Gateway serving revision equals the current overlay; zero when both are observed and differ."),
        "cpu": ("gridzilla_live_cluster_cpu_cores", "CPU cores used by observed Grid runtime components from the latest live resource sample."),
        "memory": ("gridzilla_live_cluster_memory_bytes", "Memory bytes used by observed Grid runtime components from the latest live resource sample."),
        "restarts": ("gridzilla_live_component_restarts", "Observed restart count for Grid runtime components in the live resource snapshot."),
        "pods": ("gridzilla_live_component_ready_pods", "Observed ready Pod count for Grid runtime components in the live resource snapshot."),
    }
    for name, help_text in families.values():
        _family(lines, name, "gauge", help_text)

    observed = {site.get("name"): site for site in live.get("sites", []) if site.get("name") in known_sites}
    for site_name, spec in ((site["name"], site) for site in site_specs if site.get("name") in known_sites):
        row = observed.get(site_name)
        if not isinstance(row, dict):
            continue
        labels = {"site": site_name}
        pool = pool_by_site.get(site_name)
        pool_labels = {**labels, **({"pool": pool} if pool else {})}
        metric = row.get("metrics", {})
        if metric.get("status") == "LIVE":
            _sample(lines, families["epp_scrape"][0], 1, labels)
            queue = [_number(sample.get("value")) for sample in metric.get("queue", []) if isinstance(sample, dict)]
            queue_values = [value for value in queue if value is not None]
            if queue_values:
                _sample(lines, families["queue"][0], sum(queue_values) / len(queue_values), pool_labels)
            ready = [_number(sample.get("value")) for sample in metric.get("readyEndpoints", []) if isinstance(sample, dict)]
            ready_values = [value for value in ready if value is not None]
            if ready_values:
                _sample(lines, families["ready"][0], sum(ready_values), pool_labels)
            age = _number(metric.get("sampleAgeSeconds"))
            if age is not None and age >= 0:
                _sample(lines, families["epp_age"][0], age, labels)
        provider = row.get("provider", {})
        if isinstance(provider.get("available"), bool):
            _sample(lines, families["available"][0], 1 if provider["available"] else 0, pool_labels)
        capacity = _number(provider.get("queueCapacity"))
        if capacity is not None and capacity > 0:
            _sample(lines, families["capacity"][0], capacity, pool_labels)
        simulator = row.get("simulators", [])
        values = [_number(item.get("fakeMetricValue")) for item in simulator
                  if isinstance(item, dict) and item.get("ready") and item.get("adminApiReachable")]
        synthetic = [value for value in values if value is not None]
        if synthetic and pool:
            _sample(lines, families["synthetic"][0], sum(synthetic) / len(synthetic), pool_labels)
        overlay = row.get("overlay", {})
        for candidate in overlay.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            candidate_site = candidate.get("site") or candidate.get("key")
            if candidate_site not in known_sites:
                continue
            candidate_pool = pool_by_site.get(candidate_site)
            candidate_labels = {"site": candidate_site}
            if candidate_pool:
                candidate_labels["pool"] = candidate_pool
            score = _number(candidate.get("score"))
            rank = _number(candidate.get("rank"))
            if score is not None:
                _sample(lines, families["score"][0], score, candidate_labels)
            if rank is not None:
                _sample(lines, families["rank"][0], rank, candidate_labels)
        overlay_revision = overlay.get("revision")
        gateway = row.get("gateway", {})
        if isinstance(overlay_revision, str) and overlay_revision:
            accepted = gateway.get("acceptedRevision")
            serving = gateway.get("servingRevision")
            if isinstance(accepted, str) and accepted:
                _sample(lines, families["accepted"][0], 1 if accepted == overlay_revision else 0, labels)
            if isinstance(serving, str) and serving:
                _sample(lines, families["serving"][0], 1 if serving == overlay_revision else 0, labels)
        resources = row.get("resources", {})
        sampled_at = _timestamp(resources.get("sampledAtUtc"))
        if sampled_at is not None and timestamp - sampled_at <= 90:
            cpu_text = resources.get("cpu")
            match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s+cores\s*", str(cpu_text or ""))
            if match:
                _sample(lines, families["cpu"][0], float(match.group(1)), labels)
            memory_text = resources.get("memory")
            match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s+MiB\s*", str(memory_text or ""))
            if match:
                _sample(lines, families["memory"][0], float(match.group(1)) * 1024 * 1024, labels)
            for metric_key, resource_key in (("restarts", "restarts"), ("pods", "readyPods")):
                value = _number(resources.get(resource_key))
                if value is not None:
                    _sample(lines, families[metric_key][0], value, labels)
    _active_benchmark_metrics(lines, evidence_root, str(handoff.get("runId", "")) if handoff else "",
                              known_sites, pool_by_site, timestamp)
    return "\n".join(lines) + "\n"
