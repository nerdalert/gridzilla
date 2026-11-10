"""Rebuild reports and reproducible SVG charts only from saved run evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .stats import summarize_raw


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


def _svg_line(title: str, labels: list[str], values: list[float | None], unit: str) -> str:
    width, height, pad = 900, 300, 54
    valid = [(index, value) for index, value in enumerate(values) if value is not None]
    if not valid:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"><text x="20" y="40">{title}: no eligible samples</text></svg>\n'
    maximum = max(value for _, value in valid) or 1.0
    minimum = min(value for _, value in valid)
    span = max(maximum - minimum, 1e-9)
    plot_w, plot_h = width - 2 * pad, height - 2 * pad
    coords = []
    for index, value in valid:
        x = pad + (index / max(len(values) - 1, 1)) * plot_w
        y = height - pad - ((value - minimum) / span) * plot_h
        coords.append((x, y, value, index))
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y, _v, _i in coords)
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{labels[i]}: {value:.3f} {unit}</title></circle>' for x, y, value, i in coords)
    ticks = "".join(f'<text x="{pad + i * plot_w / max(len(labels)-1,1):.1f}" y="{height - 18}" text-anchor="middle">{label}</text>' for i, label in enumerate(labels) if i % max(1, len(labels)//8) == 0)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">
<style>text{{font:12px system-ui;fill:#4d5b6a}} polyline{{fill:none;stroke:#2671b8;stroke-width:3}} circle{{fill:#2671b8}} .title{{font-size:17px;font-weight:700;fill:#16283a}}</style>
<text class="title" x="{pad}" y="28">{title}</text><line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" stroke="#ccd5df"/><line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#ccd5df"/>
<polyline points="{points}"/>{circles}{ticks}<text x="{pad}" y="{height-2}">{unit}</text></svg>\n'''


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
        if row.get("type") != "measurement":
            continue
        raw_name = row.get("rawPath")
        summary_name = row.get("summaryPath")
        raw_path = directory / raw_name if isinstance(raw_name, str) else None
        summary_path = directory / summary_name if isinstance(summary_name, str) else None
        if raw_path and summary_path and raw_path.is_file() and summary_path.is_file():
            summary = _read_json(summary_path, {})
            row["accounting"] = summarize_raw(
                raw_path, summary, minimum_samples=minimum_samples, reference=bool(row.get("reference")),
            )

    measured = [row for row in windows if row.get("type") == "measurement"]
    chart_labels = [f"{row.get('phase','?')} · {row.get('ratePerSecond','?')} rps · {row.get('repeat','?')}" for row in measured]
    throughput = []
    p95 = []
    routes: dict[str, int] = {}
    route_windows: list[dict[str, Any]] = []
    for row in measured:
        account = row.get("accounting", {})
        elapsed = float(row.get("elapsedSeconds", 0))
        throughput.append(account.get("httpRequests", 0) / elapsed if elapsed > 0 else None)
        p95.append(account.get("latencyMs", {}).get("p95", {}).get("value"))
        route_counts = {str(site): int(count) for site, count in account.get("routeCountsBySite", {}).items()}
        for site, count in account.get("routeCountsBySite", {}).items():
            routes[site] = routes.get(site, 0) + int(count)
        route_windows.append({"phase": row.get("phase"), "ratePerSecond": row.get("ratePerSecond"),
                              "repeat": row.get("repeat"), "routes": route_counts,
                              "attributedRequests": account.get("attributedRequests", sum(route_counts.values())),
                              "requestDenominator": account.get("httpRequests", 0),
                              "unattributedRequests": account.get("unattributedRequests", 0)})

    host_start = _read_json(directory / "host-start.json", {})
    host_end = _read_json(directory / "host-end.json", {})
    saturated_samples = [row for row in samples if row.get("loadGeneratorSaturated") is True]
    saturation_observed = bool(saturated_samples or host_start.get("loadGeneratorSaturated") or host_end.get("loadGeneratorSaturated"))
    throughput_eligible = bool(samples) and not saturation_observed
    throughput_validity = {"eligible": throughput_eligible,
                           "reason": "host load samples show no saturation" if throughput_eligible else
                                     "host saturation was observed" if saturation_observed else "no in-window host saturation samples were saved",
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
    result["probes"] = probes
    result["signalBoundaries"] = signal_boundaries
    result["resourceSampleCount"] = len(samples)
    result["clusterResourceSampleCount"] = cluster_resource_sample_count
    result["generatedFromEvidenceOnly"] = True
    result["sourceLock"] = source
    result["profile"] = profile
    result["routeCountsBySite"] = routes
    result["routeDistributionByWindow"] = route_windows
    result["chartInputs"] = {"labels": chart_labels, "achievedRps": throughput, "p95LatencyMs": p95}
    result["throughputClaim"] = throughput_validity
    result["resourceSeries"] = resource_series
    result["evidenceBytesBeforeFinalReport"] = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
    (directory / "report-data.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (directory / "charts").mkdir(exist_ok=True)
    (directory / "charts" / "throughput.svg").write_text(_svg_line("Achieved throughput per measurement window", chart_labels, throughput, "requests/second"), encoding="utf-8")
    (directory / "charts" / "latency-p95.svg").write_text(_svg_line("p95 response latency per measurement window", chart_labels, p95, "milliseconds"), encoding="utf-8")
    (directory / "charts" / "routes.svg").write_text(_svg_bars("Attributed route counts (bounded site values)", routes), encoding="utf-8")

    lines = [
        f"# Gridzilla benchmark report: {directory.name}", "",
        f"- Result: **{result.get('status', 'INCOMPLETE')}**",
        f"- Setup run: `{result.get('setupRunId', 'unknown')}`",
        f"- Grid source: `{source.get('setupSource', {}).get('revision', source.get('source', {}).get('revision', 'unknown'))}`",
        f"- Gridzilla source fingerprint: `{source.get('gridzillaSource', {}).get('sourceFingerprintSha256', 'not recorded')}`",
        f"- Runtime image identities: {len(source.get('runtimeImages', []))} locked image(s); exact identities are in `source-lock.json`.",
        f"- k6: `{source.get('k6Version', 'unknown')}`",
        f"- Profile: `{profile.get('name', 'unknown')}`",
        f"- Measurement windows: {len(measured)}; correlated probes: {len(probes)}; resource samples: {len(samples)}",
        f"- Throughput claim eligible: **{'yes' if throughput_eligible else 'no'}** — {throughput_validity['reason']}",
        f"- Evidence bytes before final report generation: {result['evidenceBytesBeforeFinalReport']} bytes",
        f"- Failure: {result.get('failure') or 'none recorded'}", "",
        "## Offered and achieved traffic", "",
        "| Phase | Offered rate (req/s) | Window | Offered iterations | Completed requests | Dropped iterations | HTTP failure rate | p50 ms | p95 ms | p99 ms | Samples |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in measured:
        account = row.get("accounting", {})
        lat = account.get("latencyMs", {})
        lines.append("| {phase} | {rate} | {duration} | {offered} | {done} | {dropped} | {failure} | {p50} | {p95} | {p99} | {samples} |".format(
            phase=row.get("phase", "?"), rate=row.get("ratePerSecond", "?"), duration=row.get("duration", "?"),
            offered=account.get("offeredIterations", "?"), done=account.get("httpRequests", "?"), dropped=account.get("droppedIterations", "?"),
            failure=_fmt(account.get("httpFailureRate")), p50=_fmt(lat.get("p50", {}).get("value")),
            p95=_fmt(lat.get("p95", {}).get("value")), p99=_fmt(lat.get("p99", {}).get("value")), samples=lat.get("sampleCount", "?")))
    lines += ["", "## Attributed routes", "", "Route counts below include valid Grid attribution only. Missing/inconsistent attribution is recorded as a failure and remains in the request denominator.", ""]
    if routes:
        lines.extend(f"- `{site}`: {count}" for site, count in sorted(routes.items()))
    else:
        lines.append("No attributed measurement samples were recorded.")
    lines += ["", "### Per-window attribution", "", "| Phase | Offered rate | Repeat | Attributed requests | Request denominator | Unattributed | Route counts |", "|---|---:|---:|---:|---:|---:|---|"]
    for row in route_windows:
        lines.append(f"| {row.get('phase')} | {row.get('ratePerSecond')} | {row.get('repeat')} | {row.get('attributedRequests')} | {row.get('requestDenominator')} | {row.get('unattributedRequests')} | `{json.dumps(row.get('routes', {}), sort_keys=True)}` |")
    lines += ["", "## Signal-to-route boundaries", "", "Each entry is a separate saved boundary; elapsed times are not collapsed into one convergence number.", ""]
    for item in signal_boundaries:
        lines.append(f"- `{item.get('boundary', 'unknown')}` at `{item.get('observedAtUtc', item.get('time', {}).get('utc', 'time unavailable'))}`: {json.dumps({k:v for k,v in item.items() if k not in {'sites'}}, sort_keys=True)}")
    lines += ["", "## Resource samples", "", f"Saved raw host samples: {len(samples)}; saved cluster samples: {cluster_resource_sample_count}. Host CPU/memory/network counters are sampled at the configured poll interval. More expensive cluster snapshots (node pressure, Pod restarts, `kubectl top` when Metrics Server is available, and best-effort per-Pod network counters) are sampled every 10 seconds and immediately on a detected gate failure. Unavailable component samples are retained with their error.", ""]
    lines += ["## Limitations", "", "The benchmark uses synthetic llm-d-sim fake metrics and deterministic simulator responses. It is not a GPU saturation, model quality, real-token throughput, or production capacity test. The simulator has no separate receipt API, so correlated probes validate its echoed X-Request-Id and inference Pod identity from response headers alongside the selected provider Gateway and route headers. No equal route distribution is assumed; compare observed routing to the Grid policy and overlay ranks.", "", "## Reproduction", "", "This report was regenerated from files in this evidence directory only. See `windows.jsonl`, per-window `*-raw.jsonl`/`*-summary.json`, `probes.jsonl`, `signal-boundaries.jsonl`, `resource-samples.jsonl`, host samples, and `source-lock.json`.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return result
