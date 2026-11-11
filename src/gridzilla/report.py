"""Rebuild reports and reproducible SVG charts only from saved run evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .stats import summarize_raw
from .report_model import normalize_report, window_execution


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except json.JSONDecodeError:
                continue
    return rows


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "not reported (sample count below the configured percentile minimum)"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _body_size_label(requested: Any, verified: Any) -> str:
    """Format current and legacy window body-size evidence without inventing data."""
    requested_label = (str(requested) if requested is not None else
                       "template" if verified is not None else "unavailable")
    verified_label = str(verified) if verified is not None else "unavailable"
    return f"{requested_label} / {verified_label}"


def _svg_line(title: str, labels: list[str], values: list[float | None], unit: str, *,
              y_axis_label: str | None = None, empty_message: str | None = None) -> str:
    width, height, left, right, top, bottom = 900, 330, 70, 25, 45, 75
    valid = [(index, value) for index, value in enumerate(values) if value is not None]
    if not valid:
        message = empty_message or f"{title}: no eligible samples"
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"><text x="20" y="40">{message}</text></svg>\n'
    maximum = max(value for _, value in valid) or 1.0
    minimum = min(value for _, value in valid)
    span = max(maximum - minimum, 1e-9)
    plot_w, plot_h = width - left - right, height - top - bottom
    coords = []
    for index, value in valid:
        x = left + (index / max(len(values) - 1, 1)) * plot_w
        y = height - bottom - ((value - minimum) / span) * plot_h
        coords.append((x, y, value, index))
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y, _v, _i in coords)
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{labels[i]}: {value:.3f} {unit}</title></circle>' for x, y, value, i in coords)
    tick_indexes = sorted(set(index for index in (0, (len(labels) - 1) // 2, len(labels) - 1) if 0 <= index < len(labels)))
    def clock_label(value: str) -> str:
        match = re.search(r"T(\d{2}:\d{2}:\d{2})", value)
        return match.group(1) if match else value[:18]
    ticks = "".join(f'<text class="tick" x="{left + i * plot_w / max(len(labels)-1,1):.1f}" y="{height - bottom + 18}" text-anchor="middle"><title>{labels[i]}</title>{clock_label(labels[i])}</text>' for i in tick_indexes)
    y_ticks = "".join(f'<g><line class="grid" x1="{left}" x2="{width-right}" y1="{top + j * plot_h/4:.1f}" y2="{top + j * plot_h/4:.1f}"/><text class="tick" x="{left-8}" y="{top + j * plot_h/4 + 4:.1f}" text-anchor="end">{minimum + span * (4-j)/4:.3g}</text></g>' for j in range(5))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">
<style>text{{font:12px system-ui;fill:#4d5b6a}} polyline{{fill:none;stroke:#2671b8;stroke-width:3}} circle{{fill:#2671b8}} .title{{font-size:17px;font-weight:700;fill:#16283a}} .axis{{font-weight:600;fill:#354a5e}} .tick{{font-size:10px}} .grid{{stroke:#e2e8ed;stroke-dasharray:3 3}}</style>
<text class="title" x="{left}" y="28">{title}</text>{y_ticks}<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#ccd5df"/><line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#ccd5df"/>
<polyline points="{points}"/>{circles}{ticks}<text class="axis" x="{(left + width-right)/2}" y="{height-8}" text-anchor="middle">Measurement window (UTC)</text><text class="axis" transform="translate(16 {(top+height-bottom)/2}) rotate(-90)" text-anchor="middle">{y_axis_label or unit}</text></svg>\n'''


def _svg_bars(title: str, counts: dict[str, int]) -> str:
    width, height, pad = 900, 300, 48
    if not counts:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"><text x="20" y="40">{title}: no attributed samples</text></svg>\n'
    max_value = max(counts.values()) or 1
    slot = (width - 2 * pad) / len(counts)
    bars = []
    labels = []
    for index, (name, value) in enumerate(sorted(counts.items())):
        bar_h = (height - 2 * pad) * value / max_value
        x = pad + index * slot + slot * 0.18
        y = height - pad - bar_h
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{slot*0.64:.1f}" height="{bar_h:.1f}" rx="4"><title>{name}: {value}</title></rect>')
        labels.append(f'<text x="{pad + index*slot + slot/2:.1f}" y="{height-18}" text-anchor="middle">{name}</text>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">
<style>text{{font:12px system-ui;fill:#4d5b6a}} rect{{fill:#2671b8}} .title{{font-size:17px;font-weight:700;fill:#16283a}}</style>
<text class="title" x="{pad}" y="28">{title}</text>{''.join(bars)}{''.join(labels)}</svg>\n'''


def regenerate_report(directory: Path) -> dict[str, Any]:
    directory = directory.resolve()
    result = _read_json(directory / "run-result.json", {})
    source = _read_json(directory / "source-lock.json", {})
    profile = _read_json(directory / "profile.json", {})
    windows = _read_jsonl(directory / "windows.jsonl")
    probes = _read_jsonl(directory / "probes.jsonl")
    signal_boundaries = _read_jsonl(directory / "signal-boundaries.jsonl")
    samples = _read_jsonl(directory / "resource-samples.jsonl")

    # Recompute measurements from the retained k6 summary and raw samples so
    # report regeneration applies the current parser to the original evidence
    # rather than preserving stale derived accounting in windows.jsonl.
    minimum_samples = profile.get("percentileMinimumSamples", 100)
    try:
        minimum_samples = max(1, int(minimum_samples))
    except (TypeError, ValueError):
        minimum_samples = 100
    for row in windows:
        if row.get("type") not in {"measurement", "failed-window"}:
            continue
        raw_name = row.get("rawPath")
        summary_name = row.get("summaryPath")
        raw_path = directory / raw_name if isinstance(raw_name, str) else None
        summary_path = directory / summary_name if isinstance(summary_name, str) else None
        if raw_path and summary_path and raw_path.is_file() and summary_path.is_file():
            summary = _read_json(summary_path, {})
            endpoint = _read_json(directory / "endpoint.redacted.json", {})
            dimensions = endpoint.get("dimensions", {}) if isinstance(endpoint, dict) else {}
            row["accounting"] = summarize_raw(
                raw_path, summary, minimum_samples=minimum_samples, reference=bool(row.get("reference")),
                required_dimensions=tuple(dimensions) if not row.get("reference") and isinstance(dimensions, dict) else (),
            )

    scenario_value = result.get("scenario")
    scenario = scenario_value.get("id") if isinstance(scenario_value, dict) else scenario_value
    generic = scenario == "generic-endpoint"
    measured = [row for row in windows if row.get("type") in {"measurement", "failed-window"}]
    chart_labels = [f"{row.get('phase','?')} · {row.get('ratePerSecond','?')} rps · {row.get('verifiedBodyBytes') or row.get('requestedBodyBytes') or 'body size unavailable'} bytes · repeat {row.get('repeat','?')} · {row.get('started', {}).get('utc', 'time unavailable')}" for row in measured]
    throughput = []
    p95 = []
    p95_sample_counts = []
    routes_by_dimension: dict[str, dict[str, int]] = {}
    routes_by_site: dict[str, int] = {}
    route_windows: list[dict[str, Any]] = []
    for row in measured:
        account = row.get("accounting", {})
        elapsed = float(row.get("elapsedSeconds", 0))
        throughput.append(account.get("httpRequests", 0) / elapsed if elapsed > 0 else None)
        p95.append(account.get("latencyMs", {}).get("p95", {}).get("value"))
        p95_sample_counts.append(account.get("latencyMs", {}).get("sampleCount", 0))
        raw_dimensions = account.get("routeCountsByDimension")
        if not isinstance(raw_dimensions, dict):
            legacy_sites = account.get("routeCountsBySite")
            raw_dimensions = {"site": legacy_sites} if isinstance(legacy_sites, dict) else {}
        route_counts = {
            str(dimension): {str(value): int(count) for value, count in counts.items()
                             if isinstance(count, (int, float)) and not isinstance(count, bool)}
            for dimension, counts in raw_dimensions.items() if isinstance(counts, dict)
        }
        for dimension, values in route_counts.items():
            aggregate = routes_by_dimension.setdefault(dimension, {})
            for value, count in values.items():
                aggregate[value] = aggregate.get(value, 0) + count
        for site, count in route_counts.get("site", {}).items():
            routes_by_site[site] = routes_by_site.get(site, 0) + count
        route_windows.append({"phase": row.get("phase"), "ratePerSecond": row.get("ratePerSecond"),
                              "repeat": row.get("repeat"),
                              "trafficPath": row.get("trafficPath") or ("direct-reference" if account.get("referencePath") else "endpoint" if generic else "gateway"),
                              "requestedBodyBytes": row.get("requestedBodyBytes"),
                              "verifiedBodyBytes": row.get("verifiedBodyBytes"),
                              "referencePath": bool(account.get("referencePath")),
                              "startedAtUtc": row.get("started", {}).get("utc"),
                              "routes": route_counts.get("site", {}),
                              "routesByDimension": route_counts,
                              "attributedRequests": account.get("attributedRequests", sum(route_counts.get("site", {}).values())),
                              "requestDenominator": account.get("httpRequests", 0),
                              "unattributedRequests": account.get("unattributedRequests", 0)})

    host_start = _read_json(directory / "host-start.json", {})
    host_end = _read_json(directory / "host-end.json", {})
    saturated_samples = [row for row in samples if row.get("loadGeneratorSaturated") is True]
    saturation_observed = bool(saturated_samples or host_start.get("loadGeneratorSaturated") or host_end.get("loadGeneratorSaturated"))
    dropped_iterations = sum(
        int(row.get("accounting", {}).get("droppedIterations", 0) or 0)
        for row in windows if isinstance(row.get("accounting"), dict)
    )
    execution = window_execution(result.get("plan"), windows)
    complete_plan = (execution["plannedTotal"] is not None and execution["plannedMeasurement"] is not None
                     and execution["plannedMeasurement"] > 0 and execution["notAttemptedTotal"] == 0
                     and not any(row.get("type") == "failed-window" for row in windows))
    throughput_eligible = (result.get("status") == "PASS" and complete_plan and bool(samples)
                           and not saturation_observed and dropped_iterations == 0)
    if dropped_iterations:
        throughput_reason = (f"k6 dropped {dropped_iterations} iterations; the configured load generator did not issue all offered traffic"
                             + (" and host saturation was also observed" if saturation_observed else ""))
    elif saturation_observed:
        throughput_reason = "host saturation was observed"
    elif result.get("status") != "PASS":
        throughput_reason = "the benchmark did not pass"
    elif not complete_plan:
        throughput_reason = "the planned measurement and warmup windows were not all completed successfully"
    elif samples:
        throughput_reason = "host load samples show no saturation and k6 dropped no iterations"
    else:
        throughput_reason = "no in-window host saturation samples were saved"
    throughput_validity = {"eligible": throughput_eligible,
                           "reason": throughput_reason,
                           "droppedIterationCount": dropped_iterations,
                           "saturatedSampleCount": len(saturated_samples),
                           "loadGeneratorLocation": "local Gridzilla host"}

    resource_series = []
    cluster_resource_sample_count = 0
    for sample in samples:
        host = sample.get("host", {})
        load = host.get("loadAverage1m5m15m") or []
        cluster_cpu = 0.0
        cluster_memory = 0
        cluster_values_found = False
        clusters = sample.get("clusters", [])
        if clusters:
            cluster_resource_sample_count += 1
        for cluster in clusters:
            for component in cluster.get("componentUsage", {}).values():
                for pod in component.get("pods", []):
                    if pod.get("cpuCores") is not None:
                        cluster_cpu += float(pod["cpuCores"])
                        cluster_values_found = True
                    if pod.get("memoryBytes") is not None:
                        cluster_memory += int(pod["memoryBytes"])
                        cluster_values_found = True
        resource_series.append({"observedAtUtc": sample.get("observedAtUtc"), "phase": sample.get("phase"),
                                "hostLoad1m": load[0] if load else None, "hostCpuCount": host.get("cpuCount"),
                                "hostMemoryAvailableBytes": host.get("memoryAvailableBytes"),
                                "clusterCpuCores": cluster_cpu if cluster_values_found else None,
                                "clusterMemoryBytes": cluster_memory if cluster_values_found else None,
                                "loadGeneratorSaturated": sample.get("loadGeneratorSaturated")})

    result["windows"] = windows
    result["windowExecution"] = execution
    result["probes"] = probes
    result["signalBoundaries"] = signal_boundaries
    result["resourceSampleCount"] = len(samples)
    result["clusterResourceSampleCount"] = cluster_resource_sample_count
    result["generatedFromEvidenceOnly"] = True
    result["sourceLock"] = source
    result["profile"] = profile
    result["routeCountsBySite"] = {} if generic else routes_by_site
    result["routeCountsByDimension"] = routes_by_dimension
    result["routeDistributionByWindow"] = route_windows
    result["chartInputs"] = {"labels": chart_labels, "achievedRps": throughput, "p95LatencyMs": p95,
                             "p95SampleCounts": p95_sample_counts,
                             "percentileMinimumSamples": minimum_samples}
    result["throughputClaim"] = throughput_validity
    result["resourceSeries"] = resource_series
    generated_report_names = {"report-data.json", "report.md", "normalized-report.json"}
    result["evidenceBytesBeforeFinalReport"] = sum(
        path.stat().st_size for path in directory.rglob("*")
        if path.is_file() and path.name not in generated_report_names
        and not ("charts" in path.relative_to(directory).parts)
        and "route-dimensions" not in path.relative_to(directory).parts
    )
    (directory / "report-data.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (directory / "charts").mkdir(exist_ok=True)
    p95_empty = (f"p95 hidden: each window needs {minimum_samples} latency samples; "
                 f"{sum(int(count or 0) for count in p95_sample_counts)} were saved across {len(p95_sample_counts)} windows")
    (directory / "charts" / "throughput.svg").write_text(
        _svg_line("Achieved throughput per measurement window", chart_labels, throughput, "requests/second",
                  y_axis_label="Achieved requests / second"), encoding="utf-8")
    (directory / "charts" / "latency-p95.svg").write_text(
        _svg_line("p95 response latency per measurement window", chart_labels, p95, "milliseconds",
                  y_axis_label="Response time (ms)", empty_message=p95_empty), encoding="utf-8")
    charts_dir = directory / "charts"
    (charts_dir / "routes.svg").write_text(
        _svg_bars("Configured response dimensions" if generic else "Attributed route counts by site",
                  {} if generic else routes_by_site), encoding="utf-8")
    dimension_dir = charts_dir / "route-dimensions"
    dimension_dir.mkdir(exist_ok=True)
    for dimension, counts in sorted(routes_by_dimension.items()):
        safe_dimension = re.sub(r"[^a-zA-Z0-9_.-]", "-", dimension)[:40] or "dimension"
        (dimension_dir / f"{safe_dimension}.svg").write_text(
            _svg_bars(f"Attributed route counts by {dimension}", counts), encoding="utf-8")

    endpoint = result.get("endpoint") if isinstance(result.get("endpoint"), dict) else {}
    scenario_label = "Generic HTTP endpoint" if generic else "Grid / llm-d signals"
    endpoint_label = endpoint.get("endpoint", "unavailable") if generic else "not applicable"
    setup_run_label = result.get("setupRunId", "not applicable" if generic else "unknown")
    setup_source_revision = source.get("setupSource", {}).get("revision") or source.get("source", {}).get("revision") or ("unavailable" if generic else "unknown")
    runtime_identity_label = ("not applicable to a generic endpoint" if generic else
                              f"{len(source.get('runtimeImages', []))} locked image(s); exact identities are in `source-lock.json`.")
    execution = result["windowExecution"]
    measurement_execution = (f"{execution['attemptedMeasurement']}/{execution['plannedMeasurement']} measurement windows attempted"
                             if execution["plannedMeasurement"] is not None else
                             f"{execution['attemptedMeasurement']} measurement windows attempted")
    total_execution = (f"{execution['attemptedTotal']}/{execution['plannedTotal']} total windows attempted"
                       if execution["plannedTotal"] is not None else
                       f"{execution['attemptedTotal']} total windows attempted")
    not_run = execution["notAttemptedTotal"]
    execution_label = f"{measurement_execution}; {total_execution}"
    if not_run:
        details = []
        if execution["notAttemptedMeasurement"]:
            details.append(f"{execution['notAttemptedMeasurement']} measurement")
        if execution["notAttemptedWarmup"]:
            details.append(f"{execution['notAttemptedWarmup']} warmup")
        execution_label += f"; {not_run} not started ({' + '.join(details)})"
    lines = [
        f"# Gridzilla benchmark report: {directory.name}", "",
        f"- Result: **{result.get('status', 'INCOMPLETE')}**",
        f"- Scenario: **{scenario_label}**",
        f"- Endpoint: `{endpoint_label}`",
        f"- Setup run: `{setup_run_label}`",
        f"- Setup source: `{setup_source_revision}`",
        f"- Gridzilla source fingerprint: `{source.get('gridzillaSource', {}).get('sourceFingerprintSha256', 'not recorded')}`",
        f"- Runtime image identities: {runtime_identity_label}",
        f"- k6: `{source.get('k6Version', 'unknown')}`",
        f"- Profile: `{profile.get('name', 'unknown')}`",
        f"- Window execution: {execution_label}",
        f"- Correlated probes: {len(probes)}; resource samples: {len(samples)}",
        f"- Throughput claim eligible: **{'yes' if throughput_eligible else 'no'}** — {throughput_validity['reason']}",
        f"- Evidence bytes before final report generation: {result['evidenceBytesBeforeFinalReport']} bytes",
        f"- Failure: {result.get('failure') or 'none recorded'}", "",
        "## Offered and achieved traffic", "",
        "| Phase / path | Offered rate (req/s) | Body bytes requested / verified | Window | Offered iterations | Completed requests | Dropped iterations | HTTP failure rate | p50 ms | p95 ms | p99 ms | Samples |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in measured:
        account = row.get("accounting", {})
        lat = account.get("latencyMs", {})
        requested_body = row.get("requestedBodyBytes")
        verified_body = row.get("verifiedBodyBytes")
        body_text = _body_size_label(requested_body, verified_body)
        lines.append("| {phase} | {rate} | {body} | {duration} | {offered} | {done} | {dropped} | {failure} | {p50} | {p95} | {p99} | {samples} |".format(
            phase=f"{row.get('phase', '?')} · {'direct reference' if row.get('reference') else result.get('scenario', 'Grid')}", rate=row.get("ratePerSecond", "?"), body=body_text, duration=row.get("duration", "?"),
            offered=account.get("offeredIterations", "?"), done=account.get("httpRequests", "?"), dropped=account.get("droppedIterations", "?"),
            failure=_fmt(account.get("httpFailureRate")), p50=_fmt(lat.get("p50", {}).get("value")),
            p95=_fmt(lat.get("p95", {}).get("value")), p99=_fmt(lat.get("p99", {}).get("value")), samples=lat.get("sampleCount", "?")))
    if generic:
        lines += ["", "## Configured response-header dimensions", "", "These bounded response-header values are observations only; they do not prove Grid route selection.", ""]
        if routes_by_dimension:
            for dimension, counts in sorted(routes_by_dimension.items()):
                lines.append(f"### {dimension}")
                lines.extend(f"- `{value}`: {count}" for value, count in sorted(counts.items()))
                lines.append("")
        else:
            lines.append("No response-header dimension samples were recorded.")
        lines += ["", "### Per-window observations", "", "| Phase | Traffic path | Rate | Body bytes requested / verified | Repeat | Requests | Values by dimension |", "|---|---|---:|---:|---:|---:|---|"]
        for row in route_windows:
            body_text = _body_size_label(row.get("requestedBodyBytes"), row.get("verifiedBodyBytes"))
            lines.append(f"| {row.get('phase')} | {row.get('trafficPath')} | {row.get('ratePerSecond')} | {body_text} | {row.get('repeat')} | {row.get('requestDenominator')} | `{json.dumps(row.get('routesByDimension', {}), sort_keys=True)}` |")
    else:
        lines += ["", "## Grid route attribution", "", "Counts are grouped by each observed routing dimension. Missing or inconsistent attribution remains in the request denominator and is not filled with zero.", ""]
        if routes_by_dimension:
            for dimension, counts in sorted(routes_by_dimension.items()):
                lines.append(f"### {dimension}")
                lines.extend(f"- `{value}`: {count}" for value, count in sorted(counts.items()))
                lines.append("")
        else:
            lines.append("No attributed measurement samples were recorded.")
        lines += ["", "### Per-window attribution", "", "| Phase | Traffic path | Rate | Body bytes requested / verified | Repeat | Attributed requests | Request denominator | Unattributed | Counts by dimension |", "|---|---|---:|---:|---:|---:|---:|---:|---|"]
        for row in route_windows:
            body_text = _body_size_label(row.get("requestedBodyBytes"), row.get("verifiedBodyBytes"))
            lines.append(f"| {row.get('phase')} | {row.get('trafficPath')} | {row.get('ratePerSecond')} | {body_text} | {row.get('repeat')} | {row.get('attributedRequests')} | {row.get('requestDenominator')} | {row.get('unattributedRequests')} | `{json.dumps(row.get('routesByDimension', {}), sort_keys=True)}` |")
        lines += ["", "## Signal-to-route boundaries", "", "Each entry is a separate saved boundary; elapsed times are not collapsed into one convergence number.", ""]
        for item in signal_boundaries:
            lines.append(f"- `{item.get('boundary', 'unknown')}` at `{item.get('observedAtUtc', item.get('time', {}).get('utc', 'time unavailable'))}`: {json.dumps({k:v for k,v in item.items() if k not in {'sites'}}, sort_keys=True)}")
    lines += ["", "## Resource samples", "", f"Saved raw host samples: {len(samples)}; saved cluster samples: {cluster_resource_sample_count}. Host CPU/memory/network counters are sampled at the configured poll interval. More expensive cluster snapshots (node pressure, Pod restarts, `kubectl top` when Metrics Server is available, and best-effort per-Pod network counters) are sampled every 10 seconds and immediately on a detected gate failure. Unavailable component samples are retained with their error.", ""]
    limitation = ("This measures observed HTTP behavior for the configured endpoint and workload. It does not establish GPU capacity, model quality, or real token throughput. Direct-reference requests, when configured, are reported separately from endpoint traffic." if generic else "The benchmark uses synthetic llm-d-sim queue signals and deterministic simulator responses. It is not a GPU capacity, model-quality, real-token-throughput, or production-capacity test. No equal route distribution is assumed; compare observed routing with the Grid policy and overlay ranks.")
    lines += ["## Limitations", "", limitation, "", "## Reproduction", "", "This report was regenerated from files in this evidence directory only. See `windows.jsonl`, per-window `*-raw.jsonl`/`*-summary.json`, `probes.jsonl`, `signal-boundaries.jsonl`, `resource-samples.jsonl`, host samples, and `source-lock.json`.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    # Keep the original evidence and legacy summary for existing consumers;
    # publish a versioned normalized view for new UI/API clients.
    normalized = normalize_report(directory, report_data=result)
    (directory / "normalized-report.json").write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
