"""Generic HTTP JSON endpoint scenario using Gridzilla's shared k6 traffic engine."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .controller import _host_resource_sample, _profile_host_check, _source_identity, append_jsonl, write_json
from .endpoints import GenericEndpoint, endpoint_preflight
from .models import Profile, ValidationError, duration_seconds, plan_profile
from .report import regenerate_report
from .runner import K6Failure, K6Stopped, execute_generic_window, verify_k6_version
from .stats import summarize_raw
from .runtime import utc_now


def generic_preflight(endpoint: GenericEndpoint, profile: Profile | None = None) -> dict[str, Any]:
    """Validate body-size templates and send one low-impact JSON preflight request."""
    if profile:
        sizes = profile.request_body_sizes_bytes
        for size in sizes:
            endpoint.body_template(size)
    result = endpoint_preflight(endpoint)
    result["scenario"] = "generic-endpoint"
    result["endpoint"] = endpoint.public()
    if profile:
        result["plan"] = plan_profile(profile, scenario="generic",
                                      has_reference=endpoint.direct_reference is not None)
    return result


def run_generic_benchmark(endpoint: GenericEndpoint, profile: Profile, profile_path: Path, *,
                          root: Path, stop_event: threading.Event | None = None,
                          on_progress: Callable[[dict[str, Any]], None] | None = None,
                          operation_id: str | None = None) -> dict[str, Any]:
    """Run a bounded generic endpoint profile and save normalized, secret-free evidence."""
    stop_event = stop_event or threading.Event()
    sizes = profile.request_body_sizes_bytes
    for size in sizes:
        endpoint.body_template(size)
    plan = plan_profile(profile, scenario="generic", has_reference=endpoint.direct_reference is not None)
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"generic-{stamp}-{uuid.uuid4().hex[:8]}"
    directory = root / run_id
    directory.mkdir(mode=0o700, exist_ok=False)
    started_at = utc_now()
    result: dict[str, Any] = {"schemaVersion": 1, "type": "benchmark", "scenario": "generic-endpoint",
                              "runId": run_id, "operationId": operation_id, "status": "RUNNING",
                              "createdAtUtc": started_at, "endpointId": endpoint.id,
                              "endpoint": endpoint.public(), "profileName": profile.name,
                              "plan": plan, "windows": [], "restoration": {"verified": None, "notApplicable": True},
                              "claimLimit": "Observed HTTP behavior under this endpoint and workload; no GPU capacity, model quality, or token-throughput claim."}
    write_json(directory / "endpoint.redacted.json", endpoint.public())
    write_json(directory / "profile.json", profile.raw)
    write_json(directory / "source-lock.json", {"gridzillaSource": _source_identity(), "runtimeImages": [],
                                                  "k6Version": (Path(__file__).resolve().parents[2] / "tools" / "k6-version").read_text().strip(),
                                                  "endpointRuntimeIdentity": "unavailable"})
    write_json(directory / "host-start.json", _host_resource_sample())
    status = {"runId": run_id, "operationId": operation_id, "scenario": "generic-endpoint",
              "status": "RUNNING", "phase": "preflight", "createdAtUtc": started_at,
              "updatedAtUtc": started_at, "completedWindows": 0, "expectedWindows": plan["windowCount"]}
    write_json(directory / "live-state.json", status)
    try:
        expected_k6 = (Path(__file__).resolve().parents[2] / "tools" / "k6-version").read_text().strip()
        binary, version = verify_k6_version(expected_k6)
        host_check = _profile_host_check(_host_resource_sample()["host"], profile)
        write_json(directory / "host-capacity-check.json", host_check)
        if not host_check["evidenceDiskSpaceAvailable"] or not host_check["sufficientMemory"]:
            raise K6Failure("local host lacks the memory or disk headroom required by this profile")
        if host_check["loadGeneratorAlreadySaturated"]:
            raise K6Failure("local load-generator host is already saturated; refusing a throughput run")
        preflight = generic_preflight(endpoint, profile)
        preflight["k6"] = {"binary": binary, "version": version}
        write_json(directory / "preflight.json", preflight)
        if not preflight.get("ok"):
            raise K6Failure("generic endpoint preflight did not return a configured expected status")

        phases: list[tuple[str, list[float], bool]] = [("baseline", list(profile.rates), False)]
        if endpoint.direct_reference:
            phases.append(("direct-reference", [profile.direct_reference_rate], True))
        windows_per_rate = profile.repetitions + (1 if duration_seconds(profile.warmup, "profile.warmup") else 0)
        sequence = 0
        completed = 0

        def run_one(phase: str, rate: float, repeat: int | None, size: int | None,
                    reference: bool, warmup: bool) -> dict[str, Any]:
            nonlocal sequence, completed
            if stop_event.is_set():
                raise K6Stopped("benchmark stopped by operator")
            sequence += 1
            suffix = "warmup" if warmup else f"r{repeat}"
            size_tag = "template" if size is None else f"{size}b"
            index = f"{sequence:03d}-{phase}-{size_tag}-{suffix}"
            duration = profile.warmup if warmup else profile.window
            effective_phase = f"{phase}-warmup" if warmup else phase
            status.update({"phase": effective_phase, "currentWindow": index,
                           "offeredRatePerSecond": rate, "requestedBodyBytes": size,
                           "updatedAtUtc": utc_now()})
            write_json(directory / "live-state.json", status)
            if on_progress:
                on_progress({"action": "measurement-start" if not warmup else "warmup-start",
                             "runId": run_id, "operationId": operation_id, "index": index,
                             "scenario": "generic-endpoint", "phase": effective_phase,
                             "ratePerSecond": rate, "duration": duration,
                             "requestedBodyBytes": size, "repeat": repeat,
                             "completedWindows": completed, "expectedWindows": plan["windowCount"]})
            try:
                sample_now = 0.0

                def monitor() -> str | None:
                    nonlocal sample_now
                    import time
                    now = time.monotonic()
                    if now - sample_now < 5:
                        return None
                    sample_now = now
                    sample = _host_resource_sample()
                    sample.update({"scenario": "generic-endpoint", "phase": effective_phase,
                                   "requestedBodyBytes": size})
                    append_jsonl(directory / "resource-samples.jsonl", sample)
                    return ("local load-generator host became CPU-saturated" if
                            sample.get("loadGeneratorSaturated") else None)

                sample = _host_resource_sample()
                sample.update({"scenario": "generic-endpoint", "phase": effective_phase,
                               "requestedBodyBytes": size})
                append_jsonl(directory / "resource-samples.jsonl", sample)
                row = execute_generic_window(endpoint, profile, evidence_dir=directory,
                                             phase=effective_phase, rate=rate, duration=duration,
                                             index=index, request_body_size_bytes=size,
                                             reference=reference, stop_event=stop_event,
                                             monitor=monitor)
            except Exception as exc:
                fail_path = directory / f"{index}-window-result.json"
                failure = {"index": index, "scenario": "generic-endpoint", "phase": effective_phase,
                           "type": "failed-window", "ratePerSecond": rate,
                           "requestedBodyBytes": size, "verifiedBodyBytes": None,
                           "reference": reference, "failure": str(exc), "finishedAtUtc": utc_now()}
                if fail_path.is_file():
                    try:
                        prior = json.loads(fail_path.read_text(encoding="utf-8"))
                        failure.update({key: prior.get(key) for key in ("started", "finished", "elapsedSeconds", "verifiedBodyBytes", "bodySizeSamples", "summaryPath", "rawPath", "exitCode")})
                    except (OSError, json.JSONDecodeError):
                        pass
                raw = directory / f"{index}-raw.jsonl"
                summary_path = directory / f"{index}-summary.json"
                summary = {}
                if summary_path.is_file():
                    try:
                        summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        pass
                failure["accounting"] = summarize_raw(raw, summary,
                                                       minimum_samples=profile.percentile_minimum_samples,
                                                       reference=reference,
                                                       required_dimensions=tuple(endpoint.dimensions) if not reference else ())
                failure["verifiedBodyBytes"] = failure["accounting"].get("verifiedBodyBytes")
                failure["bodySizeSamples"] = failure["accounting"].get("bodySizeSamples", 0)
                append_jsonl(directory / "windows.jsonl", failure)
                result["windows"].append(failure)
                raise
            row.update({"type": "warmup" if warmup else "measurement", "scenario": "generic-endpoint",
                        "repeat": repeat, "requestedBodyBytes": size,
                        "trafficPath": "direct-reference" if reference else "endpoint"})
            if not warmup:
                raw_path = directory / str(row.get("rawPath") or "")
                row["accounting"] = summarize_raw(raw_path, row.get("summary", {}),
                                                   minimum_samples=profile.percentile_minimum_samples,
                                                   reference=reference,
                                                   required_dimensions=tuple(endpoint.dimensions) if not reference else ())
                # The Trend's observed samples are the wire body length proof.
                accounting = row["accounting"]
                row["verifiedBodyBytes"] = accounting.get("verifiedBodyBytes")
                row["bodySizeSamples"] = accounting.get("bodySizeSamples", 0)
                expected_bytes = size if size is not None else len(endpoint.body_template()[0].encode("utf-8"))
                if row["verifiedBodyBytes"] != expected_bytes:
                    failure = {**row, "type": "failed-window",
                               "failure": "window did not verify one consistent serialized request-body size",
                               "finishedAtUtc": utc_now()}
                    append_jsonl(directory / "windows.jsonl", failure)
                    result["windows"].append(failure)
                    raise K6Failure(f"window {index} did not verify one consistent serialized request-body size")
            append_jsonl(directory / "windows.jsonl", row)
            result["windows"].append(row)
            completed += 1
            status.update({"completedWindows": completed, "updatedAtUtc": utc_now(),
                           "lastWindow": {"index": index, "phase": effective_phase,
                                          "requestedBodyBytes": size, "verifiedBodyBytes": row.get("verifiedBodyBytes")}})
            write_json(directory / "live-state.json", status)
            if on_progress:
                on_progress({"action": "window-complete", "runId": run_id,
                             "operationId": operation_id, "index": index,
                             "scenario": "generic-endpoint", "phase": effective_phase,
                             "ratePerSecond": rate, "requestedBodyBytes": size,
                             "verifiedBodyBytes": row.get("verifiedBodyBytes"),
                             "completedWindows": completed, "expectedWindows": plan["windowCount"]})
            return row

        for phase, rates, reference in phases:
            for body_size in sizes:
                for rate in rates:
                    if profile.warmup:
                        run_one(phase, rate, None, body_size, reference, True)
                    for repeat in range(1, profile.repetitions + 1):
                        run_one(phase, rate, repeat, body_size, reference, False)
        status.update({"phase": "complete", "status": "PASS", "updatedAtUtc": utc_now()})
        result["status"] = "PASS"
    except BaseException as exc:
        stopped = isinstance(exc, K6Stopped) and stop_event.is_set()
        result["status"] = "STOPPED" if stopped else "FAIL"
        if not stopped:
            result["failure"] = str(exc)
        status.update({"phase": "stopped" if stopped else "failed",
                       "status": result["status"], "stopRequested": stopped,
                       "failureAvailable": not stopped, "updatedAtUtc": utc_now()})
    finally:
        try:
            write_json(directory / "host-end.json", _host_resource_sample())
            result.update({"finishedAtUtc": utc_now(), "restoration": {"verified": None, "notApplicable": True},
                           "windows": result.get("windows", [])})
            write_json(directory / "run-result.json", result)
            status.update({"status": result.get("status", "FAIL"), "finishedAtUtc": result["finishedAtUtc"],
                           "updatedAtUtc": utc_now()})
            write_json(directory / "live-state.json", status)
            regenerate_report(directory)
        except Exception as report_error:
            # Keep the original run outcome, while retaining enough evidence to diagnose report failure.
            write_json(directory / "report-generation-failure.json", {"errorType": type(report_error).__name__,
                                                                        "observedAtUtc": utc_now()})
    return {"status": result.get("status", "FAIL"), "runId": run_id,
            "operationId": operation_id, "evidenceDirectory": str(directory),
            "plan": plan, "failure": result.get("failure")}
