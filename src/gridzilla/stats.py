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
                  reference: bool = False, required_dimensions: tuple[str, ...] = ()) -> dict[str, Any]:
    route_counts: dict[str, int] = {}
    route_counts_by_dimension: dict[str, dict[str, int]] = {}
    status_counts: dict[str, int] = {}
    durations: list[float] = []
    bytes_out = 0
    attribution_failures = 0
    generic_attribution: dict[str, int] = {"complete": 0, "incomplete": 0}
    generic_attribution_samples = 0
    output_limit_failures = 0
    timeout_errors = 0
    http_request_points = 0
    request_body_sizes: list[int] = []
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
                elif metric == "gridzilla_generic_attribution" and value is not None:
                    quality = str(tags.get("quality", "unknown"))
                    if quality in generic_attribution:
                        count = int(float(value))
                        generic_attribution[quality] += count
                        generic_attribution_samples += count
                elif metric == "gridzilla_output_limit_failures" and value is not None:
                    output_limit_failures += int(float(value))
                elif metric == "gridzilla_attributed_routes" and value is not None:
                    site = str(tags.get("site", "unknown"))
                    route_counts[site] = route_counts.get(site, 0) + int(float(value))
                elif metric == "gridzilla_gateway_requests" and value is not None:
                    for tag, dimension_value in tags.items():
                        if tag.startswith("dimension_"):
                            dimension = tag.removeprefix("dimension_")
                            counts = route_counts_by_dimension.setdefault(dimension, {})
                            label = str(dimension_value)
                            counts[label] = counts.get(label, 0) + int(float(value))
                elif metric == "gridzilla_request_body_bytes" and value is not None:
                    request_body_sizes.append(int(float(value)))

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
    if reference:
        attribution_status = "not-applicable"
        attribution_numerator = 0
        attribution_denominator = 0
    elif required_dimensions:
        attribution_denominator = http_count
        if generic_attribution_samples:
            attribution_numerator = generic_attribution["complete"]
            attribution_status = "complete" if attribution_numerator == attribution_denominator else "partial"
        else:
            # Older k6 output retained per-dimension counts but not the
            # per-response quality metric. A single required dimension can
            # still be counted exactly; multiple dimensions cannot be joined
            # without per-request observations, so never invent an intersection.
            missing_values = {"", "unknown", "unavailable", "missing", "none", "null"}
            observed = [route_counts_by_dimension.get(dimension, {}) for dimension in required_dimensions]
            valid_counts = [sum(count for label, count in values.items()
                                 if label.strip().lower() not in missing_values)
                            for values in observed]
            if len(required_dimensions) == 1 and observed[0]:
                attribution_numerator = valid_counts[0]
                observed_total = sum(observed[0].values())
                attribution_status = ("complete" if observed_total == attribution_denominator
                                      and attribution_numerator == attribution_denominator else
                                      "partial" if attribution_numerator > 0 else "unavailable")
            elif any(valid_counts):
                attribution_numerator = None
                attribution_status = "partial"
            else:
                attribution_numerator = 0
                attribution_status = "unavailable"
    else:
        attribution_numerator = attributed_count
        attribution_denominator = http_count
        attribution_status = ("not-applicable" if not route_counts else
                              "complete" if http_count > 0 and attributed_count == http_count else "partial")
    attribution_unattributed = (None if attribution_status in {"not-applicable", "unavailable"}
                                or attribution_numerator is None
                                else max(0, attribution_denominator - attribution_numerator))
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
        "routeCountsByDimension": route_counts_by_dimension or ({"site": route_counts} if route_counts else {}),
        "requestedBodyBytes": (max(request_body_sizes) if request_body_sizes else None),
        "verifiedBodyBytes": (min(request_body_sizes) if request_body_sizes and
                               len(set(request_body_sizes)) == 1 else None),
        "bodySizeSamples": len(request_body_sizes),
        "attributionStatus": attribution_status,
        "attributedRequests": None if attribution_status in {"not-applicable", "unavailable"}
        or attribution_numerator is None else attribution_numerator,
        "attributionDenominator": (None if attribution_status in {"not-applicable", "unavailable"}
                                   else attribution_denominator),
        "unattributedRequests": attribution_unattributed,
        "attributionCompleteness": (None if attribution_unattributed is None or attribution_denominator == 0
                                    else attribution_numerator / attribution_denominator),
        "genericAttributionCounts": generic_attribution if required_dimensions else {},
        "requiredAttributionDimensions": list(required_dimensions),
        "referencePath": reference,
        "attributionFailures": attribution_failures,
        "outputLimitFailures": output_limit_failures,
        "latencyMs": {"p50": p50, "p95": p95, "p99": p99, "sampleCount": int(duration_ms.get("count", len(sample_values)))},
    }
