"""Offline k6 sample parsing, denominators, and sample-size-aware percentiles."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable


def percentile(values: Iterable[float], percentile_value: float, minimum_samples: int = 100) -> dict[str, Any]:
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile_value must be in [0, 100]")
    samples = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            samples.append(numeric)
    samples.sort()
    result: dict[str, Any] = {"sampleCount": len(samples), "value": None}
    if len(samples) < minimum_samples:
        result["reason"] = f"requires at least {minimum_samples} samples"
        return result
    position = (len(samples) - 1) * percentile_value / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        value = samples[lower]
    else:
        value = samples[lower] + (samples[upper] - samples[lower]) * (position - lower)
    result["value"] = value
    return result


def _metric_values(summary: dict[str, Any], metric: str) -> dict[str, Any]:
    entry = summary.get("metrics", {}).get(metric, {})
    if not isinstance(entry, dict):
        return {}
    # k6 summary exports used to place metric values under `values`; current
    # exports flatten the same fields directly into each metric object.
    values = entry.get("values")
    return values if isinstance(values, dict) else entry


def summarize_raw(raw_path: Path, summary: dict[str, Any], *, minimum_samples: int,
                  reference: bool = False) -> dict[str, Any]:
    route_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    durations: list[float] = []
    bytes_out = 0
    attribution_failures = 0
    output_limit_failures = 0
    timeout_errors = 0
    http_request_points = 0
    if raw_path.is_file():
        with raw_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    point = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if point.get("type") != "Point":
                    continue
                metric = point.get("metric")
                data = point.get("data", {})
                value = data.get("value")
                tags = data.get("tags", {}) or {}
                if metric == "http_reqs":
                    http_request_points += int(float(value or 0))
                    status = str(tags.get("status", "unknown"))
                    status_counts[status] = status_counts.get(status, 0) + int(float(value or 0))
                    if tags.get("error_code") in {"1050", "1051", "1052", "1053"} or "timeout" in str(tags.get("error", "")).lower():
                        timeout_errors += int(float(value or 0))
                elif metric == "gridzilla_gateway_latency_ms" and value is not None:
                    durations.append(float(value))
                elif metric == "gridzilla_response_bytes" and value is not None:
                    bytes_out += int(float(value))
                elif metric == "gridzilla_attribution_failures" and value is not None:
                    attribution_failures += int(float(value))
                elif metric == "gridzilla_output_limit_failures" and value is not None:
                    output_limit_failures += int(float(value))
                elif metric == "gridzilla_attributed_routes" and value is not None:
                    site = str(tags.get("site", "unknown"))
                    route_counts[site] = route_counts.get(site, 0) + int(float(value))

    metrics = summary.get("metrics", {})
    iterations_values = _metric_values(summary, "iterations")
    dropped_values = _metric_values(summary, "dropped_iterations")
    failed_values = _metric_values(summary, "http_req_failed")
    requested = float(iterations_values.get("count", 0))
    dropped = float(dropped_values.get("count", 0))
    http_count = int(_metric_values(summary, "http_reqs").get("count", http_request_points))
    http_failed_rate = failed_values.get("rate", failed_values.get("value"))
    duration_ms = _metric_values(summary, "http_req_duration")
    sample_values = durations or []
    if not sample_values and duration_ms.get("count", 0) >= minimum_samples:
        # k6's summary already computed percentiles from the exact same samples.
        p50 = {"sampleCount": int(duration_ms.get("count", 0)), "value": duration_ms.get("med"), "source": "k6-summary"}
        p95 = {"sampleCount": int(duration_ms.get("count", 0)), "value": duration_ms.get("p(95)"), "source": "k6-summary"}
        p99 = {"sampleCount": int(duration_ms.get("count", 0)), "value": duration_ms.get("p(99)"), "source": "k6-summary"}
    else:
        p50 = percentile(sample_values, 50, minimum_samples)
        p95 = percentile(sample_values, 95, minimum_samples)
        p99 = percentile(sample_values, 99, minimum_samples)
    attributed_count = sum(route_counts.values())
    return {
        "offeredIterations": requested + dropped,
        "completedIterations": requested,
        "droppedIterations": int(dropped),
        "httpRequests": http_count,
        "statusCounts": status_counts,
        "httpFailureRate": http_failed_rate,
        "timeouts": timeout_errors,
        "retries": 0,
        "responseBytes": bytes_out,
        "routeCountsBySite": route_counts,
        "attributedRequests": 0 if reference else attributed_count,
        "attributionDenominator": 0 if reference else http_count,
        "unattributedRequests": 0 if reference else max(0, http_count - attributed_count),
        "attributionCompleteness": (None if reference or http_count == 0 else attributed_count / http_count),
        "referencePath": reference,
        "attributionFailures": attribution_failures,
        "outputLimitFailures": output_limit_failures,
        "latencyMs": {"p50": p50, "p95": p95, "p99": p99, "sampleCount": int(duration_ms.get("count", len(sample_values)))},
    }
