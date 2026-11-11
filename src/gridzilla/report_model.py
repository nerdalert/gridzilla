"""Versioned, topology-independent representation of saved Gridzilla runs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_FORMAT = "gridzilla.dev/normalized-report/v1alpha1"


def window_execution(plan: Any, windows: Any) -> dict[str, int | None]:
    """Summarize planned versus actually started measurement and warmup windows."""
    plan = plan if isinstance(plan, dict) else {}
    rows = windows if isinstance(windows, list) else []
    attempted_measurement = 0
    attempted_warmup = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type", ""))
        phase = str(row.get("phase", "")).lower()
        if kind == "warmup" or "warmup" in phase:
            attempted_warmup += 1
        elif kind in {"measurement", "failed-window"}:
            attempted_measurement += 1
    planned_measurement = _number(plan.get("measurementWindowCount"))
    planned_warmup = _number(plan.get("warmupWindowCount"))
    planned_total = _number(plan.get("windowCount"))
    attempted_total = attempted_measurement + attempted_warmup
    return {
        "plannedMeasurement": int(planned_measurement) if planned_measurement is not None else None,
        "attemptedMeasurement": attempted_measurement,
        "notAttemptedMeasurement": max(0, int(planned_measurement) - attempted_measurement)
            if planned_measurement is not None else None,
        "plannedWarmup": int(planned_warmup) if planned_warmup is not None else None,
        "attemptedWarmup": attempted_warmup,
        "notAttemptedWarmup": max(0, int(planned_warmup) - attempted_warmup)
            if planned_warmup is not None else None,
        "plannedTotal": int(planned_total) if planned_total is not None else None,
        "attemptedTotal": attempted_total,
        "notAttemptedTotal": max(0, int(planned_total) - attempted_total)
            if planned_total is not None else None,
    }


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _utc(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(end.replace("Z", "+00:00")) -
                         datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _source_identity(source_lock: dict[str, Any]) -> dict[str, Any]:
    setup = source_lock.get("setupSource") or source_lock.get("source") or {}
    gridzilla = source_lock.get("gridzillaSource") or {}
    def identity(source: Any) -> dict[str, Any] | None:
        if not isinstance(source, dict) or not any(source.get(key) for key in ("repository", "revision", "commit")):
            return None
        return {key: source.get(key) for key in
                ("repository", "branch", "revision", "commit", "dirty", "sourceFingerprintSha256")
                if source.get(key) is not None}
    return {"setup": identity(setup), "gridzilla": identity(gridzilla),
            "k6Version": source_lock.get("k6Version")}


def _images(source_lock: dict[str, Any]) -> list[dict[str, Any]]:
    images = source_lock.get("runtimeImages", [])
    normalized = []
    if not isinstance(images, list):
        return normalized
    for image in images:
        if not isinstance(image, dict):
            continue
        normalized.append({key: image.get(source_key) for key, source_key in (
            ("site", "site"), ("component", "component"), ("reference", "reference"),
            ("imageId", "imageId"), ("sourceRevision", "sourceRevision")) if image.get(source_key) is not None})
    return sorted(normalized, key=lambda item: (str(item.get("site", "")), str(item.get("component", ""))))


def _topology(handoff: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    if data.get("scenario") == "generic-endpoint":
        endpoint = data.get("endpoint") if isinstance(data.get("endpoint"), dict) else {}
        return {"id": "generic-endpoint", "description": endpoint.get("name"),
                "siteCount": None, "sites": []}
    sites = handoff.get("sites", [])
    normalized_sites = []
    if isinstance(sites, list):
        for site in sites:
            if not isinstance(site, dict) or not isinstance(site.get("name"), str):
                continue
            provider = site.get("provider") if isinstance(site.get("provider"), dict) else {}
            normalized_sites.append({"name": site["name"], **({"pool": provider["pool"]} if provider.get("pool") else {})})
    if not normalized_sites:
        source_lock = data.get("sourceLock") if isinstance(data.get("sourceLock"), dict) else {}
        image_sites = sorted({str(image.get("site")) for image in source_lock.get("runtimeImages", [])
                              if isinstance(image, dict) and isinstance(image.get("site"), str)})
        normalized_sites = [{"name": name} for name in image_sites]
    explicit = data.get("topology") if isinstance(data.get("topology"), dict) else {}
    identifier = explicit.get("id") or explicit.get("name") or handoff.get("topology")
    if not identifier and normalized_sites:
        identifier = "multi-site" if len(normalized_sites) > 1 else "single-site"
    return {"id": identifier or "unspecified", "description": explicit.get("description"),
            "siteCount": len(normalized_sites) if normalized_sites else None,
            "sites": normalized_sites}


def _scenario(data: dict[str, Any], profile: dict[str, Any], handoff: dict[str, Any]) -> dict[str, str]:
    value = data.get("scenario") or profile.get("scenario")
    if isinstance(value, dict):
        scenario_id = value.get("id") or "unspecified"
        label = value.get("name") or scenario_id
    elif isinstance(value, str):
        scenario_id = value
        label = value.replace("-", " ").title()
    elif handoff.get("schemaVersion") == "gridzilla.dev/v1alpha1" and handoff.get("pressureTargetPool"):
        scenario_id = "grid-llmd-signals"
        label = "Grid llm-d signals routing"
    else:
        scenario_id = "unspecified"
        label = "Unspecified scenario"
    return {"id": str(scenario_id), "name": str(label)}


def _phase(name: Any, reference: bool) -> str:
    value = str(name or "measurement").lower()
    if reference or "reference" in value:
        return "reference"
    if "pressure" in value:
        return "pressure"
    if "recovery" in value or "restore" in value:
        return "recovery"
    if "warmup" in value:
        return "warmup"
    return "baseline"


def _route_counts(account: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = account.get("routeCountsByDimension")
    if isinstance(value, dict):
        return {str(dimension): {str(key): count for key, count in counts.items() if _number(count) is not None}
                for dimension, counts in value.items() if isinstance(counts, dict)}
    legacy = account.get("routeCountsBySite")
    if isinstance(legacy, dict):
        return {"site": {str(key): count for key, count in legacy.items() if _number(count) is not None}}
    return {}


def _measurement(row: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    account = row.get("accounting") if isinstance(row.get("accounting"), dict) else {}
    reference = bool(row.get("reference") or account.get("referencePath"))
    generic = row.get("scenario") == "generic-endpoint" or row.get("trafficPath") == "endpoint"
    elapsed = _number(row.get("elapsedSeconds"))
    requests = _number(account.get("httpRequests"))
    status_counts = account.get("statusCounts") if isinstance(account.get("statusCounts"), dict) else {}
    errors = 0
    status_samples = False
    for status, count in status_counts.items():
        if _number(count) is None:
            continue
        status_samples = True
        status_text = str(status).upper()
        if not status_text.startswith(("2", "3")):
            errors += int(count)
    latency = account.get("latencyMs") if isinstance(account.get("latencyMs"), dict) else {}
    sample_count = _number(latency.get("sampleCount"))
    minimum = _number(profile.get("percentileMinimumSamples", 100))
    percentile_values = {}
    eligible = sample_count is not None and (minimum is None or sample_count >= minimum)
    for quantile in ("p50", "p95", "p99"):
        sample = latency.get(quantile) if isinstance(latency.get(quantile), dict) else {}
        value = _number(sample.get("value"))
        percentile_values[quantile] = value if eligible else None
    achieved = requests / elapsed if requests is not None and elapsed and elapsed > 0 else None
    route_counts = _route_counts(account)
    attribution_denominator = _number(account.get("attributionDenominator", account.get("httpRequests")))
    attributed = _number(account.get("attributedRequests"))
    unattributed = _number(account.get("unattributedRequests"))
    completeness = _number(account.get("attributionCompleteness"))
    if generic:
        required = account.get("requiredAttributionDimensions")
        attribution_status = str(account.get("attributionStatus") or
                                 ("unavailable" if required else "not-applicable"))
        # Legacy raw k6 output can contain bounded dimension counts without
        # the newer per-response attribution-quality metric. Recover exact
        # quality for one dimension and report partial (without inventing a
        # cross-dimension intersection) for multiple dimensions.
        if attribution_status in {"unavailable", "not-applicable"} and isinstance(required, list) and required:
            missing = {"", "unknown", "unavailable", "missing", "none", "null"}
            observed = [route_counts.get(str(name), {}) for name in required]
            valid_counts = [sum(int(count) for label, count in values.items()
                                if str(label).strip().lower() not in missing)
                            for values in observed]
            if any(valid_counts):
                attribution_status = "partial"
                attribution_denominator = attribution_denominator or requests
                if len(required) == 1 and observed[0]:
                    attributed = valid_counts[0]
                    observed_total = sum(int(count) for count in observed[0].values())
                    attribution_status = ("complete" if attribution_denominator is not None
                                          and observed_total == attribution_denominator
                                          and attributed == attribution_denominator else "partial")
                    if attribution_denominator is not None:
                        unattributed = max(0, int(attribution_denominator) - attributed)
                        completeness = attributed / attribution_denominator if attribution_denominator else None
                else:
                    attributed = None
                    unattributed = None
                    completeness = None
    elif reference:
        attribution_status = "not-applicable"
    elif not route_counts:
        attribution_status = "unavailable"
    elif completeness == 1 or (attributed is not None and attribution_denominator == attributed):
        attribution_status = "complete"
    else:
        attribution_status = "partial"
    started = row.get("started") if isinstance(row.get("started"), dict) else {}
    finished = row.get("finished") if isinstance(row.get("finished"), dict) else {}
    requested_body = _number(row.get("requestedBodyBytes") if row.get("requestedBodyBytes") is not None
                             else account.get("requestedBodyBytes"))
    verified_body = _number(row.get("verifiedBodyBytes") if row.get("verifiedBodyBytes") is not None
                            else account.get("verifiedBodyBytes"))
    body_identity = requested_body if requested_body is not None else verified_body
    return {
        "id": f"{row.get('phase', 'measurement')}:{row.get('trafficPath', 'direct-reference' if reference else 'gateway')}:{row.get('ratePerSecond', 'unknown')}:{body_identity if body_identity is not None else 'unavailable'}:{row.get('repeat', row.get('index', 'unknown'))}:{row.get('index', 'unknown')}",
        "phase": str(row.get("phase") or "measurement"),
        "phaseClass": _phase(row.get("phase"), reference),
        "trafficPath": str(row.get("trafficPath") or ("direct-reference" if reference else "gateway")),
        "repeat": _number(row.get("repeat")),
        "offeredRps": _number(row.get("ratePerSecond")),
        "requestedBodyBytes": requested_body,
        "verifiedBodyBytes": verified_body,
        "bodySizeSamples": _number(row.get("bodySizeSamples") or account.get("bodySizeSamples")),
        "offeredRequests": _number(account.get("offeredIterations")),
        "achievedRps": achieved,
        "completedRequests": requests,
        "httpErrors": errors if status_samples else None,
        "statusCounts": status_counts,
        "timeouts": _number(account.get("timeouts")),
        "droppedIterations": _number(account.get("droppedIterations")),
        "responseBytes": _number(account.get("responseBytes")),
        "windowStatus": "FAIL" if row.get("type") == "failed-window" or row.get("failure") else "MEASURED",
        "responseDimensions": ({"requestDenominator": requests,
                                "dimensions": [{"name": name, "counts": values}
                                               for name, values in sorted(route_counts.items())]}
                               if generic else None),
        "latencyMs": {**percentile_values, "sampleCount": sample_count,
                      "minimumSamples": minimum, "eligible": bool(eligible)},
        "attribution": {"status": attribution_status,
                        "attributedRequests": attributed,
                        "requestDenominator": attribution_denominator,
                        "unattributedRequests": unattributed,
                        "completeness": completeness,
                        "dimensions": [{"name": name, "counts": values} for name, values in sorted(route_counts.items())]},
        "timing": {"startedAtUtc": _utc(started.get("utc")), "finishedAtUtc": _utc(finished.get("utc")),
                   "elapsedSeconds": elapsed},
        "failure": row.get("monitorFailure") or (row.get("failureMarkers") or [None])[0],
    }


def _resources(samples: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    host_samples = []
    workloads = []
    for row in samples:
        host = row.get("host") if isinstance(row.get("host"), dict) else {}
        if not host and any(key in row for key in ("hostMemoryAvailableBytes", "hostCpuCount", "hostLoad1m")):
            host = {"memoryAvailableBytes": row.get("hostMemoryAvailableBytes"),
                    "cpuCount": row.get("hostCpuCount"),
                    "loadAverage1m5m15m": [row.get("hostLoad1m")] if row.get("hostLoad1m") is not None else []}
        host_sample = {"observedAtUtc": _utc(row.get("observedAtUtc")), "phase": row.get("phase"),
                       "clientLocation": host.get("clientLocation"),
                       "cpuCount": _number(host.get("cpuCount")),
                       "loadAverage": host.get("loadAverage1m5m15m"),
                       "memoryAvailableBytes": _number(host.get("memoryAvailableBytes")),
                       "memoryTotalBytes": _number(host.get("memoryTotalBytes")),
                       "diskFreeBytes": _number(host.get("diskFreeBytes")),
                       "loadGeneratorSaturated": row.get("loadGeneratorSaturated"),
                       "missing": []}
        for field in ("cpuCount", "loadAverage", "memoryAvailableBytes", "memoryTotalBytes", "diskFreeBytes",
                      "loadGeneratorSaturated"):
            value = host_sample[field]
            if value is None or (field == "loadAverage" and not value):
                host_sample["missing"].append(field)
        host_samples.append(host_sample)
        clusters = row.get("clusters") if isinstance(row.get("clusters"), list) else []
        if not clusters and (row.get("clusterCpuCores") is not None or row.get("clusterMemoryBytes") is not None):
            workloads.append({"observedAtUtc": _utc(row.get("observedAtUtc")), "phase": row.get("phase"),
                              "site": None, "component": "cluster aggregate", "podCount": None,
                              "cpuCores": _number(row.get("clusterCpuCores")),
                              "memoryBytes": _number(row.get("clusterMemoryBytes")),
                              "missing": [field for field in ("cpuCores", "memoryBytes") if row.get(
                                  "clusterCpuCores" if field == "cpuCores" else "clusterMemoryBytes") is None]})
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            components = cluster.get("componentUsage") if isinstance(cluster.get("componentUsage"), dict) else {}
            for component, usage in components.items():
                pods = usage.get("pods") if isinstance(usage, dict) and isinstance(usage.get("pods"), list) else []
                cpu = [value for pod in pods if isinstance(pod, dict)
                       if (value := _number(pod.get("cpuCores"))) is not None]
                memory = [value for pod in pods if isinstance(pod, dict)
                          if (value := _number(pod.get("memoryBytes"))) is not None]
                workloads.append({"observedAtUtc": _utc(row.get("observedAtUtc")), "phase": row.get("phase"),
                                  "site": cluster.get("site"), "component": str(component),
                                  "podCount": len(pods), "cpuCores": sum(cpu) if cpu else None,
                                  "memoryBytes": sum(memory) if memory else None,
                                  "missing": [field for field, values in (("cpuCores", cpu), ("memoryBytes", memory)) if not values]})
    claim = result.get("throughputClaim") if isinstance(result.get("throughputClaim"), dict) else {}
    window_rows = result.get("windows") if isinstance(result.get("windows"), list) else []
    dropped_iterations = sum(
        max(0, int(_number(row.get("accounting", {}).get("droppedIterations")) or 0))
        for row in window_rows if isinstance(row, dict) and isinstance(row.get("accounting"), dict)
    )
    eligible = claim.get("eligible")
    reason = claim.get("reason")
    if dropped_iterations:
        eligible = False
        reason = (f"k6 dropped {dropped_iterations} iterations; offered traffic was not fully issued"
                  + (" and host saturation was observed" if any(sample.get("loadGeneratorSaturated") is True for sample in host_samples) else ""))
    elif eligible is None:
        eligible = bool(host_samples) and all(
            sample.get("loadGeneratorSaturated") is False
            and sample.get("cpuCount") is not None
            and sample.get("memoryAvailableBytes") is not None
            for sample in host_samples)
        if not eligible and not reason:
            reason = "Host capacity or saturation evidence is missing."
    return {"host": host_samples, "workloads": workloads,
            "throughputEligibility": {"eligible": bool(eligible), "reason": reason,
                                      "loadGeneratorLocation": claim.get("loadGeneratorLocation") or result.get("loadGeneratorLocation"),
                                      "saturatedSampleCount": claim.get("saturatedSampleCount"),
                                      "droppedIterationCount": claim.get("droppedIterationCount", dropped_iterations)},
            "missingSamples": not bool(host_samples)}


def _signal_trace(boundaries: list[dict[str, Any]], probes: list[dict[str, Any]],
                  handoff: dict[str, Any], signal_events: list[dict[str, Any]],
                  pressure_site: str | None) -> dict[str, Any] | None:
    if not boundaries and not probes and not signal_events:
        return None
    events = []
    def queue_values(value: Any) -> list[float | int]:
        if not isinstance(value, list):
            return []
        return [number for item in value if (number := _number(item)) is not None]

    for row in signal_events:
        if not isinstance(row, dict):
            continue
        if row.get("boundary") != "simulator-config-post-complete":
            continue
        observed = row.get("time") if isinstance(row.get("time"), dict) else {}
        started = row.get("started") if isinstance(row.get("started"), dict) else {}
        posted = row.get("postResults") if isinstance(row.get("postResults"), list) else []
        values = [_number(item.get("observedValue")) for item in posted if isinstance(item, dict)]
        value = next((item for item in values if item is not None), None)
        events.append({"kind": "synthetic-pressure", "name": "Simulator pressure applied",
                       "source": "llm-d-sim configuration observation",
                       "observedAtUtc": _utc(observed.get("utc")),
                       "startedAtUtc": _utc(started.get("utc")),
                       "monotonicNs": _number(observed.get("monotonicNs")),
                       "durationMs": _number(row.get("durationMs")),
                       "site": pressure_site, "syntheticWaitingRequests": value,
                       "observedPodCount": len(posted) if posted else None})
    for row in boundaries:
        sites = []
        for site, values in (row.get("sites", {}) if isinstance(row.get("sites"), dict) else {}).items():
            if not isinstance(values, dict):
                continue
            sites.append({"site": site, "previousEppQueue": queue_values(values.get("oldQueue")),
                          "eppQueue": queue_values(values.get("newQueue")),
                          "previousOverlayRevision": values.get("oldRevision"),
                          "overlayRevision": values.get("newRevision"),
                          "previousGatewayAcceptedRevision": values.get("oldAcceptedRevision"),
                          "gatewayAcceptedRevision": values.get("newAcceptedRevision"),
                          "previousGatewayServingRevision": values.get("oldServingRevision"),
                          "gatewayServingRevision": values.get("newServingRevision"),
                          "previousScore": _number(values.get("oldScore")),
                          "score": _number(values.get("newScore")),
                          "previousRank": _number(values.get("oldRank")),
                          "rank": _number(values.get("newRank")),
                          "eppObservedAtUtc": _utc(values.get("newEppObservedAtUtc")),
                          "eppSampleTimestampUtc": _utc(values.get("newEppSampleTimestampUtc")),
                          "eppFreshnessBasis": values.get("eppFreshnessBasis"),
                          "queueChanged": values.get("queueChanged"),
                          "rankWorsened": values.get("rankWorsened"),
                          "scoreDecreased": values.get("scoreDecreased")})
        top = row.get("pressureTop") if isinstance(row.get("pressureTop"), dict) else row.get("baselineTop")
        time_row = row.get("time") if isinstance(row.get("time"), dict) else {}
        completed = row.get("completedAt") if isinstance(row.get("completedAt"), dict) else {}
        events.append({"kind": "signal-boundary", "name": row.get("boundary"),
                       "source": "llm-d-sim configuration observation" if "simulator" in str(row.get("boundary")) else "Gridzilla observer",
                       "observedAtUtc": _utc(row.get("observedAtUtc") or time_row.get("utc") or completed.get("utc")),
                       "monotonicNs": _number(row.get("monotonicNs")), "poll": _number(row.get("poll")),
                       "durationMs": _number(row.get("durationMs")), "ok": row.get("ok"),
                       "selectionChanged": row.get("selectionChanged"),
                       "site": row.get("site") or row.get("pressureSite"),
                       "syntheticWaitingRequests": _number(row.get("requestedWaitingRequests")),
                       "selected": {key: top.get(key) for key in ("site", "key", "cluster", "name", "rank", "score")
                                    if isinstance(top, dict) and top.get(key) is not None},
                       "sites": sites})
    for row in probes:
        if row.get("type") != "correlated-probe":
            continue
        attribution = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
        started = row.get("started") if isinstance(row.get("started"), dict) else {}
        finished = row.get("finished") if isinstance(row.get("finished"), dict) else {}
        events.append({"kind": "correlated-probe", "name": row.get("label") or "sampled probe",
                       "source": "Gridzilla probe", "observedAtUtc": _utc(finished.get("utc") or started.get("utc")),
                       "startedAtUtc": _utc(started.get("utc")), "finishedAtUtc": _utc(finished.get("utc")),
                       "monotonicNs": _number(finished.get("monotonicNs")),
                       "durationMs": _number(row.get("durationMs")), "requestId": row.get("requestId"),
                       "status": _number(row.get("status")), "ok": row.get("ok"),
                       "expectedSite": row.get("expectedSite"),
                       "attribution": {key: attribution.get(key) for key in ("site", "pool", "provider", "providerGateway", "backend")
                                       if attribution.get(key) is not None},
                       "receipt": {key: (row.get("receipt") or {}).get(key) for key in
                                   ("available", "found", "independent", "requestIdMatched", "providerSite")
                                   if isinstance(row.get("receipt"), dict) and (row.get("receipt") or {}).get(key) is not None}})
    events.sort(key=lambda item: item.get("observedAtUtc") or "")
    if not events:
        return None
    llmd = bool(handoff.get("pressureTargetPool") or any("llmd" in str(key).lower() for key in handoff))
    return {"kind": "llm-d-signals" if llmd else "signal-observations",
            "path": ["simulator synthetic setting", "authenticated EPP sample", "Grid score/rank",
                     "distributed overlay", "Gateway serving revision", "correlated route probe"] if llmd else [],
            "events": events,
            "timestampNote": "UTC wall-clock observations and process-local monotonic durations are separate; cross-cluster clock skew is not corrected."}


def normalize_report(directory: Path, *, report_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Adapt current or legacy saved evidence into normalized report v1alpha1."""
    directory = directory.resolve()
    run_result = _json(directory / "run-result.json")
    data = dict(run_result)
    legacy = report_data if report_data is not None else _json(directory / "report-data.json")
    data.update(legacy)
    source_lock = data.get("sourceLock") if isinstance(data.get("sourceLock"), dict) else _json(directory / "source-lock.json")
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else _json(directory / "profile.json")
    handoff = _json(directory / "handoff.redacted.json") or _json(directory / "handoff.json")
    live_state = _json(directory / "live-state.json")
    if not data and live_state:
        data = live_state
    raw_windows = data.get("windows") if isinstance(data.get("windows"), list) else _jsonl(directory / "windows.jsonl")
    if isinstance(raw_windows, list):
        windows = [_measurement(row, profile) for row in raw_windows
                   if isinstance(row, dict) and row.get("type") in {"measurement", "failed-window"}]
    else:
        windows = []
    boundaries = data.get("signalBoundaries") if isinstance(data.get("signalBoundaries"), list) else _jsonl(directory / "signal-boundaries.jsonl")
    if not boundaries:
        boundaries = _jsonl(directory / "signal-events.jsonl")
    probes = data.get("probes") if isinstance(data.get("probes"), list) else _jsonl(directory / "probes.jsonl")
    samples = _jsonl(directory / "resource-samples.jsonl")
    if not samples and isinstance(data.get("resourceSeries"), list):
        samples = data["resourceSeries"]
    topology = _topology(handoff, data)
    dimensions: dict[str, set[str]] = {}
    for site in topology["sites"]:
        dimensions.setdefault("site", set()).add(site["name"])
    for window in windows:
        for dimension in window["attribution"]["dimensions"]:
            dimensions.setdefault(dimension["name"], set()).update(dimension["counts"].keys())
    routing_dimensions = [{"name": name, "values": sorted(values)} for name, values in sorted(dimensions.items())]
    generic = data.get("scenario") == "generic-endpoint"
    sources = _source_identity(source_lock)
    image_identities = _images(source_lock)
    restoration = data.get("restoration") if isinstance(data.get("restoration"), dict) else {}
    failure_recorded = bool(data.get("failure"))
    restoration_verified = restoration.get("verified")
    if restoration.get("notApplicable") is True or data.get("scenario") == "generic-endpoint":
        restoration_status = "NOT_APPLICABLE"
    elif restoration_verified is True:
        restoration_status = "VERIFIED"
    elif restoration_verified is False:
        restoration_status = "NOT_VERIFIED"
    elif data.get("type") in {"smoke", "doctor"}:
        restoration_status = "NOT_APPLICABLE"
    else:
        restoration_status = "UNKNOWN"
    timing_rows = [row.get("timing", {}) for row in windows]
    starts = [row.get("startedAtUtc") for row in timing_rows if row.get("startedAtUtc")]
    ends = [row.get("finishedAtUtc") for row in timing_rows if row.get("finishedAtUtc")]
    created_at = _utc(data.get("createdAtUtc") or live_state.get("createdAtUtc"))
    finished_at = _utc(data.get("finishedAtUtc"))
    run_started_at = min(starts) if starts else created_at
    run_finished_at = finished_at or (max(ends) if ends else None)
    gateway_windows = [window for window in windows if window["trafficPath"] == "gateway"]
    def total_if_observed(field: str) -> float | None:
        values = [_number(window["attribution"].get(field)) for window in gateway_windows]
        known = [value for value in values if value is not None]
        return sum(known) if known else None
    normalized = {
        "formatVersion": REPORT_FORMAT,
        "run": {"id": data.get("runId") or directory.name,
                "setupRunId": data.get("setupRunId") or handoff.get("runId"),
                "type": data.get("type") or "benchmark",
                "scenario": _scenario(data, profile, handoff),
                "topology": topology,
                "endpoint": data.get("endpoint") if isinstance(data.get("endpoint"), dict) else None,
                "status": data.get("status") or live_state.get("status") or "UNKNOWN",
                "createdAtUtc": created_at,
                "startedAtUtc": run_started_at,
                "finishedAtUtc": run_finished_at,
                "durationSeconds": _seconds_between(run_started_at, run_finished_at),
                "source": sources, "images": image_identities,
                "profile": profile,
                "progress": live_state.get("benchmark") if isinstance(live_state.get("benchmark"), dict) else None,
                "windowExecution": (data.get("windowExecution") if isinstance(data.get("windowExecution"), dict)
                                    else window_execution(data.get("plan"), raw_windows)),
                "result": {"status": data.get("status") or live_state.get("status") or "UNKNOWN",
                           "failureRecorded": failure_recorded,
                           "failureSummary": "Failure recorded; inspect saved evidence." if failure_recorded else None,
                           "throughputEligibility": (data.get("throughputClaim") or {}).get("eligible")},
                "restorationStatus": restoration_status,
                "restoration": {"verified": restoration_verified,
                                "restoredAtUtc": ((restoration.get("restoredAt") or {}).get("utc")
                                                  if isinstance(restoration.get("restoredAt"), dict) else None),
                                "pressureSite": restoration.get("pressureSite"),
                                "selectionRestored": (restoration.get("recovery") or {}).get("selectionRestored")
                                    if isinstance(restoration.get("recovery"), dict) else None}},
        "measurements": {"phases": []},
        "routing": None if generic else {"dimensions": routing_dimensions,
                    "attributionQuality": {"attributedRequests": total_if_observed("attributedRequests"),
                                           "requestDenominator": total_if_observed("requestDenominator"),
                                           "unattributedRequests": total_if_observed("unattributedRequests")},
                    "windows": [{"phase": window["phase"], "phaseClass": window["phaseClass"],
                                 "offeredRps": window["offeredRps"], "repeat": window["repeat"],
                                 "trafficPath": window["trafficPath"],
                                 "requestedBodyBytes": window["requestedBodyBytes"],
                                 "verifiedBodyBytes": window["verifiedBodyBytes"],
                                 "attribution": window["attribution"]}
                                for window in windows]},
        "responseDimensions": ({"dimensions": routing_dimensions,
                                "windows": [{"phase": window["phase"], "phaseClass": window["phaseClass"],
                                             "trafficPath": window["trafficPath"], "offeredRps": window["offeredRps"],
                                             "requestedBodyBytes": window["requestedBodyBytes"], "repeat": window["repeat"],
                                             "requestDenominator": window["responseDimensions"]["requestDenominator"],
                                             "dimensions": window["responseDimensions"]["dimensions"]}
                                            for window in windows if window.get("responseDimensions") is not None]}
                               if generic else None),
        "resources": _resources(samples, data),
        "limitations": ["Failure recorded; inspect saved evidence."] if failure_recorded else [],
        "evidenceFiles": sorted(path.name for path in directory.iterdir() if path.is_file()
                                 and path.suffix in {".json", ".jsonl", ".csv", ".md", ".svg", ".log"}
                                 and path.name not in {"report-data.json", "report.md", "normalized-report.json"}
                                 and "credential" not in path.name.lower() and "token" not in path.name.lower()),
    }
    phases: dict[str, list[dict[str, Any]]] = {}
    for window in windows:
        phases.setdefault(window["phase"], []).append(window)
    normalized["measurements"]["phases"] = [{"name": name, "classification": _phase(name, False), "windows": rows}
                                              for name, rows in phases.items()]
    run_restoration = data.get("restoration") if isinstance(data.get("restoration"), dict) else {}
    trace = _signal_trace(boundaries if isinstance(boundaries, list) else [],
                          probes if isinstance(probes, list) else [], handoff,
                          data.get("signalEvents", []) if isinstance(data.get("signalEvents"), list) else [],
                          run_restoration.get("pressureSite"))
    if trace:
        normalized["signals"] = trace
    # Pre-computed legacy values are not the new contract, but retain the
    # report's derivation and source links for reproducibility.
    normalized["provenance"] = {"generatedFromSavedEvidence": True,
                                "setupImageLockSha256": source_lock.get("setupImageLockSha256"),
                                "profileName": profile.get("name"),
                                "profileVersion": profile.get("version")}
    return normalized
