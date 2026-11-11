"""Explicitly enabled, localhost-only dashboard controls."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .controller import (
    BenchmarkFailure,
    _acquire_run_lock,
    _candidate_on_client_site,
    _capture_diagnostics,
    _expected_site,
    _ready_gate,
    _source_identity,
    _token_resolved_handoff,
    append_jsonl,
    doctor,
    gate_snapshot,
    mutate_simulators,
    new_run_dir,
    restore_simulators,
    run_benchmark,
    send_probe,
    snapshot_simulators,
    wait_for_recovery,
    wait_for_signal_transition,
    write_json,
)
from .models import Profile, ValidationError, duration_seconds, load_profile, plan_profile, redacted_handoff
from .endpoints import GenericEndpoint, endpoint_preflight, load_endpoint_registry
from .generic import generic_preflight, run_generic_benchmark
from .runtime import EndpointTransport, monotonic_utc_pair, utc_now


class ControlFailure(RuntimeError):
    """A safe dashboard control request was rejected."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class BenchmarkControls:
    """Run profiles or a temporary signal probe using the dashboard transport."""

    PROFILE_NAMES = ("smoke", "baseline", "stepped", "body-sizes")
    PRESSURE_HOLD_SECONDS = 30

    def __init__(self, *, enabled: bool, handoff: dict[str, Any] | None,
                 evidence_root: Path, profiles_dir: Path,
                 transport: EndpointTransport | None, endpoints_dir: Path | None = None):
        self.enabled = enabled
        self.handoff = handoff
        self.evidence_root = evidence_root.resolve()
        self.profiles: dict[str, tuple[Profile, Path]] = {}
        self.endpoints: dict[str, GenericEndpoint] = load_endpoint_registry(endpoints_dir)
        self.transport = transport
        self.lock = threading.RLock()
        self.operation_dir = self.evidence_root / ".operations"
        self.operations: dict[str, dict[str, Any]] = {}
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.active_profile: str | None = None
        self.active_operation: str | None = None
        self.active_operation_id: str | None = None
        self.last_operation_id: str | None = None
        self.active_scenario: str | None = None
        self.active_endpoint_id: str | None = None
        self.stop_requested = False
        self.last_outcome: str | None = None
        self.last_run_id: str | None = None
        self.pressure_status: dict[str, Any] = {"phase": "idle", "restorationVerified": None}
        self.prior_run_ids: set[str] = set()
        self.pressure_options: list[dict[str, Any]] = self._pressure_options(handoff)

        root = profiles_dir.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValidationError(f"profile directory is not a directory: {root}")
        for name in self.PROFILE_NAMES:
            path = (root / f"{name}.json").resolve(strict=True)
            if path.parent != root or not path.is_file():
                raise ValidationError(f"dashboard profile {name!r} must be a regular file inside {root}")
            profile = load_profile(path)
            if profile.name != name:
                raise ValidationError(f"dashboard profile file {name}.json must declare name {name!r}")
            self.profiles[name] = (profile, path)
        if not enabled:
            return
        if handoff is None and not self.endpoints:
            raise ValidationError("--allow-controls requires --handoff or at least one server-loaded endpoint configuration")
        if handoff is not None and transport is None:
            raise ValidationError("Grid scenario controls require a ready local cluster transport")

    @staticmethod
    def _pressure_options(handoff: dict[str, Any] | None) -> list[dict[str, Any]]:
        if handoff is None:
            return []
        options = []
        for site in handoff.get("sites", []):
            provider = site.get("provider", {})
            simulator = site.get("simulator", {})
            try:
                capacity = int(provider["queueCapacity"])
                if (capacity < 2 or capacity % 2 != 0
                        or float(provider["queueCapacity"]) != capacity):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            if (not simulator.get("adminUrl") or not simulator.get("targets")
                    or not simulator.get("pressureJsonPointer")):
                continue
            for value in (capacity // 2, capacity):
                options.append({"value": value, "capacity": capacity, "label": f"{value} / {capacity}",
                                "site": site.get("name"), "pool": provider.get("pool")})
        return options

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = bool(self.thread and self.thread.is_alive())
            return {
                "enabled": self.enabled,
                "profiles": list(self.profiles),
                "endpointConfigurations": [endpoint.public() for endpoint in self.endpoints.values()],
                "scenarios": (["grid-llmd-signals"] if self.handoff is not None else []) +
                             (["generic-endpoint"] if self.endpoints else []),
                "profileSettings": {name: {
                    "rates": list(profile.rates),
                    "soakSeconds": duration_seconds(profile.window, "profile.window"),
                    "warmupSeconds": duration_seconds(profile.warmup, "profile.warmup"),
                    "repetitions": profile.repetitions,
                    "maxArrivalRatePerSecond": profile.max_rate,
                    "percentileMinimumSamples": profile.percentile_minimum_samples,
                    "requestBodySizesBytes": list(profile.request_body_sizes_bytes),
                    "genericPlan": plan_profile(profile, scenario="generic", has_reference=False),
                    "genericPlanByConfiguration": {
                        endpoint.id: plan_profile(profile, scenario="generic",
                                                  has_reference=endpoint.direct_reference is not None)
                        for endpoint in self.endpoints.values()},
                    "signalRate": profile.signal_rate,
                    "directReferenceRate": profile.direct_reference_rate,
                } for name, (profile, _path) in self.profiles.items()},
                "running": running,
                "operation": self.active_operation if running else None,
                "operationId": self.active_operation_id if running else None,
                "lastOperationId": self.last_operation_id,
                "runId": self.last_run_id if running else None,
                "scenario": self.active_scenario if running else None,
                "endpointId": self.active_endpoint_id if running else None,
                "profile": self.active_profile if running else None,
                "stopRequested": self.stop_requested if running else False,
                "lastOutcome": self.last_outcome,
                "lastRunId": self.last_run_id,
                "pressureOptions": self.pressure_options,
                "pressure": dict(self.pressure_status),
            }

    _OPERATION_FIELDS = frozenset({
        "runId", "scenario", "endpointId", "profile", "phase", "currentWindow",
        "offeredRatePerSecond", "requestedBodyBytes", "verifiedBodyBytes",
        "completedWindows", "expectedWindows", "trafficProgress", "stopRequested",
        "restoration", "restorationVerified", "site", "pool", "value", "capacity",
        "updatedAtUtc", "createdAtUtc", "finishedAtUtc", "operation", "status",
    })

    def _save_operation(self, operation_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Persist a bounded, credential-free operation record for local API polling."""
        with self.lock:
            current = dict(self.operations.get(operation_id, {}))
            for key, value in changes.items():
                if key in self._OPERATION_FIELDS:
                    current[key] = value
            current["operationId"] = operation_id
            current["updatedAtUtc"] = utc_now()
            if changes.get("failureAvailable"):
                current["failureAvailable"] = True
                current["failureSummary"] = "Failure details are in the saved local evidence."
            self.operations[operation_id] = current
            self.operation_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json(self.operation_dir / f"{operation_id}.json", current)
            if len(self.operations) > 128:
                oldest = sorted(self.operations, key=lambda key: self.operations[key].get("createdAtUtc", ""))
                for expired in oldest[:-128]:
                    self.operations.pop(expired, None)
                    try:
                        (self.operation_dir / f"{expired}.json").unlink()
                    except OSError:
                        pass
            return dict(current)

    def _progress_callback(self, operation_id: str):
        safe_fields = {"action", "runId", "scenario", "phase", "index", "ratePerSecond",
                       "duration", "requestedBodyBytes", "verifiedBodyBytes", "repeat",
                       "completedWindows", "expectedWindows"}

        def record(update: dict[str, Any]) -> None:
            progress = {key: value for key, value in update.items() if key in safe_fields}
            self._save_operation(operation_id, {
                "status": "RUNNING", "runId": progress.get("runId"),
                "scenario": progress.get("scenario"), "phase": progress.get("phase"),
                "currentWindow": progress.get("index"), "offeredRatePerSecond": progress.get("ratePerSecond"),
                "requestedBodyBytes": progress.get("requestedBodyBytes"),
                "verifiedBodyBytes": progress.get("verifiedBodyBytes"),
                "completedWindows": progress.get("completedWindows"),
                "expectedWindows": progress.get("expectedWindows"), "trafficProgress": progress,
            })
        return record

    def _ensure_available(self) -> None:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        if self.handoff is None and not self.endpoints:
            raise ControlFailure("dashboard has no configured benchmark scenario", 503)
        if self.thread and self.thread.is_alive():
            raise ControlFailure("another Gridzilla operation is already running", 409)

    @staticmethod
    def _profile_with_overrides(profile: Profile, rates_input: Any, soak_input: Any,
                                *, scenario: str = "grid", has_reference: bool = True,
                                sizes_input: Any = None) -> Profile:
        """Apply and safety-check dashboard arrival-rate and soak overrides."""
        if not isinstance(rates_input, str):
            raise ControlFailure("enter comma-separated arrival rates, such as 25,50", 400)
        try:
            parts = [part.strip() for part in rates_input.split(",")]
            if not parts or len(parts) > 6 or any(not part for part in parts):
                raise ValueError
            rates = tuple(float(part) for part in parts)
        except (TypeError, ValueError):
            raise ControlFailure("arrival rates must be 1–6 comma-separated numbers, such as 25,50", 400) from None
        if (any(rate <= 0 or rate > profile.max_rate for rate in rates)
                or any(left >= right for left, right in zip(rates, rates[1:]))):
            raise ControlFailure(
                f"arrival rates must increase strictly and stay at or below {profile.max_rate:g} requests/second",
                400,
            )
        if isinstance(soak_input, bool) or not isinstance(soak_input, (int, float)):
            raise ControlFailure("soak duration must be whole seconds", 400)
        if int(soak_input) != soak_input or not 15 <= int(soak_input) <= 600:
            raise ControlFailure("soak duration must be between 15 and 600 seconds", 400)
        soak_seconds = int(soak_input)
        raw = json.loads(json.dumps(profile.raw))
        raw["rates"] = list(rates)
        raw["window"] = f"{soak_seconds}s"
        signal_rate = float(raw.get("signalRate", rates[0]))
        reference_rate = float(raw.get("directReferenceRate", rates[0]))
        raw["signalRate"] = signal_rate
        raw["directReferenceRate"] = reference_rate
        reference_rate = min(reference_rate, profile.max_rate)
        signal_rate = min(signal_rate, profile.max_rate)
        sizes = profile.request_body_sizes_bytes
        if sizes_input is not None:
            if (not isinstance(sizes_input, list) or not sizes_input or len(sizes_input) > 6
                    or any(size is not None and (isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 1048576)
                           for size in sizes_input)):
                raise ControlFailure("requestBodySizesBytes must contain 1–6 sizes (null for template size, integers up to 1 MiB)", 400)
            sizes = tuple(sizes_input)
            raw["requestBodySizesBytes"] = list(sizes)
        updated = replace(profile, rates=rates, window=f"{soak_seconds}s", signal_rate=signal_rate,
                          direct_reference_rate=reference_rate, request_body_sizes_bytes=sizes, raw=raw)
        try:
            plan_profile(updated, scenario=scenario, has_reference=has_reference)
        except ValidationError as exc:
            raise ControlFailure(str(exc), 400) from None
        return updated

    def start(self, profile_name: str, rates_input: Any = None, soak_input: Any = None,
              sizes_input: Any = None) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        if profile_name not in self.profiles:
            raise ControlFailure("choose one of the listed profiles", 400)
        if self.handoff is None or self.transport is None:
            raise ControlFailure("dashboard has no active run-owned environment", 503)

        with self.lock:
            self._ensure_available()
            profile, profile_path = self.profiles[profile_name]
            if rates_input is not None or soak_input is not None or sizes_input is not None:
                settings = {
                    "rates": ",".join(str(rate) for rate in profile.rates),
                    "soakSeconds": duration_seconds(profile.window, "profile.window"),
                }
                profile = self._profile_with_overrides(
                    profile,
                    settings["rates"] if rates_input is None else rates_input,
                    settings["soakSeconds"] if soak_input is None else soak_input,
                    scenario="grid", has_reference=bool(self.handoff.get("backendReference", {}).get("url")),
                    sizes_input=sizes_input,
                )
            gate = doctor(self.handoff, profile=profile, transport=self.transport)
            if not gate.get("ok"):
                raise ControlFailure("live readiness and image-provenance checks are not all passing", 409)
            stop_event = threading.Event()
            self.prior_run_ids = {path.name for path in self.evidence_root.glob(f"{self.handoff['runId']}-*")
                                  if path.is_dir()}
            self.stop_event = stop_event
            self.stop_requested = False
            self.active_profile = profile_name
            self.active_operation = "benchmark"
            operation_id = uuid.uuid4().hex
            self.active_operation_id = operation_id
            self.last_operation_id = operation_id
            self.active_scenario = "grid-llmd-signals"
            self.active_endpoint_id = None
            self.last_outcome = None
            self.last_run_id = None
            self._save_operation(operation_id, {"operation": "benchmark", "status": "RUNNING",
                                                "scenario": "grid-llmd-signals", "profile": profile_name,
                                                "createdAtUtc": utc_now(), "stopRequested": False})
            self.thread = threading.Thread(
                target=self._run,
                args=(profile, profile_path, stop_event, operation_id),
                name="gridzilla-dashboard-benchmark",
                daemon=False,
            )
            self.thread.start()
            return {"accepted": True, "profile": profile_name, "rates": list(profile.rates),
                    "soakSeconds": duration_seconds(profile.window, "profile.window"),
                    "operationId": operation_id, "scenario": "grid-llmd-signals"}

    def generic_configurations(self) -> list[dict[str, Any]]:
        return [endpoint.public() for endpoint in self.endpoints.values()]

    def preflight(self, scenario: str, profile_name: str, endpoint_id: str | None = None,
                  rates_input: Any = None, soak_input: Any = None,
                  sizes_input: Any = None) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("controls are disabled", 403)
        if profile_name not in self.profiles:
            raise ControlFailure("choose one of the listed profiles", 400)
        profile = self.profiles[profile_name][0]
        if scenario == "generic-endpoint":
            endpoint = self.endpoints.get(str(endpoint_id))
            if endpoint is None:
                raise ControlFailure("choose one of the server-listed endpoint configurations", 400)
            if rates_input is not None or soak_input is not None or sizes_input is not None:
                profile = self._profile_with_overrides(
                    profile,
                    ",".join(str(rate) for rate in profile.rates) if rates_input is None else rates_input,
                    duration_seconds(profile.window, "profile.window") if soak_input is None else soak_input,
                    scenario="generic", has_reference=endpoint.direct_reference is not None,
                    sizes_input=sizes_input)
            return generic_preflight(endpoint, profile)
        if scenario == "grid-llmd-signals" and self.handoff is not None and self.transport is not None:
            if rates_input is not None or soak_input is not None or sizes_input is not None:
                profile = self._profile_with_overrides(
                    profile,
                    ",".join(str(rate) for rate in profile.rates) if rates_input is None else rates_input,
                    duration_seconds(profile.window, "profile.window") if soak_input is None else soak_input,
                    scenario="grid", has_reference=bool(self.handoff.get("backendReference", {}).get("url")),
                    sizes_input=sizes_input)
            result = doctor(self.handoff, profile=profile, transport=self.transport)
            if not result.get("ok"):
                raise ControlFailure("Grid readiness and provenance preflight failed", 409)
            return result
        raise ControlFailure("unsupported scenario selection", 400)

    def start_generic(self, endpoint_id: str, profile_name: str, rates_input: Any = None,
                      soak_input: Any = None, sizes_input: Any = None) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled", 403)
        endpoint = self.endpoints.get(endpoint_id)
        if endpoint is None:
            raise ControlFailure("choose one of the server-listed endpoint configurations", 400)
        if profile_name not in self.profiles:
            raise ControlFailure("choose one of the listed profiles", 400)
        with self.lock:
            self._ensure_available()
            profile, profile_path = self.profiles[profile_name]
            if rates_input is not None or soak_input is not None or sizes_input is not None:
                profile = self._profile_with_overrides(
                    profile,
                    ",".join(str(rate) for rate in profile.rates) if rates_input is None else rates_input,
                    duration_seconds(profile.window, "profile.window") if soak_input is None else soak_input,
                    scenario="generic", has_reference=endpoint.direct_reference is not None,
                    sizes_input=sizes_input)
            try:
                for size in profile.request_body_sizes_bytes:
                    endpoint.body_template(size)
                preflight = generic_preflight(endpoint, profile)
            except ValidationError as exc:
                raise ControlFailure(str(exc), 400) from None
            if not preflight.get("ok"):
                raise ControlFailure("generic endpoint preflight returned an unexpected status", 409)
            stop_event = threading.Event()
            operation_id = uuid.uuid4().hex
            self.stop_event = stop_event
            self.stop_requested = False
            self.active_profile = profile_name
            self.active_operation = "benchmark"
            self.active_operation_id = operation_id
            self.last_operation_id = operation_id
            self.active_scenario = "generic-endpoint"
            self.active_endpoint_id = endpoint.id
            self.last_outcome = None
            self.last_run_id = None
            self._save_operation(operation_id, {"operation": "benchmark", "status": "RUNNING",
                                                "scenario": "generic-endpoint", "endpointId": endpoint.id,
                                                "profile": profile_name, "createdAtUtc": utc_now(),
                                                "stopRequested": False})
            self.thread = threading.Thread(target=self._run_generic,
                                           args=(endpoint, profile, profile_path, stop_event, operation_id),
                                           name="gridzilla-generic-benchmark", daemon=False)
            self.thread.start()
            return {"accepted": True, "scenario": "generic-endpoint", "endpointId": endpoint.id,
                    "profile": profile_name, "operationId": operation_id,
                    "rates": list(profile.rates),
                    "requestBodySizesBytes": list(profile.request_body_sizes_bytes),
                    "soakSeconds": duration_seconds(profile.window, "profile.window"),
                    "plan": preflight.get("plan")}

    def _run_generic(self, endpoint: GenericEndpoint, profile: Profile, profile_path: Path,
                     stop_event: threading.Event, operation_id: str) -> None:
        outcome = "FAIL"
        run_id = None
        try:
            result = run_generic_benchmark(endpoint, profile, profile_path, root=self.evidence_root,
                                           stop_event=stop_event, operation_id=operation_id,
                                           on_progress=self._progress_callback(operation_id))
            outcome = str(result.get("status", "FAIL"))
            run_id = result.get("runId")
        except BaseException:
            outcome = "FAIL"
        finally:
            self._save_operation(operation_id, {"status": outcome, "runId": run_id,
                                                "finishedAtUtc": utc_now(), "stopRequested": stop_event.is_set(),
                                                "failureAvailable": outcome == "FAIL"})
            with self.lock:
                self.last_outcome = outcome
                self.last_run_id = run_id
                self.active_profile = None
                self.active_operation = None
                self.active_operation_id = None
                self.active_scenario = None
                self.active_endpoint_id = None
                self.stop_requested = False
                self.stop_event = None

    def start_pressure(self, value: Any, site_name: Any) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        try:
            requested = int(value)
        except (TypeError, ValueError):
            raise ControlFailure("choose one of the listed queue-pressure levels", 400) from None
        if not isinstance(site_name, str) or not site_name:
            raise ControlFailure("select a site for queue pressure", 400)
        option = next((item for item in self.pressure_options
                       if item["value"] == requested and item["site"] == site_name), None)
        if option is None or str(value).strip() not in {str(requested), f"{requested}.0"}:
            raise ControlFailure("choose one of the listed queue-pressure levels", 400)
        if self.handoff is None or self.transport is None:
            raise ControlFailure("dashboard has no active run-owned environment", 503)

        with self.lock:
            self._ensure_available()
            profile, _ = self.profiles["smoke"]
            gate = doctor(self.handoff, transport=self.transport)
            if not gate.get("ok"):
                raise ControlFailure("live readiness and image-provenance checks are not all passing", 409)
            target = next((site for site in self.handoff["sites"] if site["name"] == site_name), None)
            if target is None or target["provider"].get("pool") != option["pool"]:
                raise ControlFailure("selected site no longer matches the handoff", 409)
            raw_profile = json.loads(json.dumps(profile.raw))
            raw_profile["pressureTargetPool"] = target["provider"]["pool"]
            raw_profile["signalRate"] = float(requested)
            profile = replace(profile, pressure_target_pool=target["provider"]["pool"],
                              signal_rate=float(requested), raw=raw_profile)
            self.stop_event = threading.Event()
            self.stop_requested = False
            self.active_profile = None
            self.active_operation = "pressure"
            operation_id = uuid.uuid4().hex
            self.active_operation_id = operation_id
            self.last_operation_id = operation_id
            self.active_scenario = "grid-llmd-signals"
            self.active_endpoint_id = None
            self.last_outcome = None
            self.last_run_id = None
            self.pressure_status = {"phase": "accepted", "value": requested,
                                    "capacity": option["capacity"], "site": site_name,
                                    "pool": target["provider"]["pool"], "restorationVerified": None}
            self._save_operation(operation_id, {"operation": "pressure", "status": "RUNNING",
                                                "scenario": "grid-llmd-signals", "profile": "smoke",
                                                "site": site_name, "pool": target["provider"]["pool"],
                                                "value": requested, "capacity": option["capacity"],
                                                "createdAtUtc": utc_now(), "stopRequested": False})
            self.thread = threading.Thread(
                target=self._run_pressure,
                args=(profile, requested, option["capacity"], site_name, self.stop_event, operation_id),
                name="gridzilla-dashboard-pressure",
                daemon=False,
            )
            self.thread.start()
            return {"accepted": True, "value": requested, "capacity": option["capacity"],
                    "site": site_name, "pool": target["provider"]["pool"],
                    "operationId": operation_id}

    def restore_pressure(self, operation_id: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        with self.lock:
            if (not self.thread or not self.thread.is_alive() or self.active_operation != "pressure"
                    or self.stop_event is None):
                raise ControlFailure("no temporary pressure operation is currently running", 409)
            if not operation_id or operation_id != self.active_operation_id:
                raise ControlFailure("operationId does not identify the active pressure operation", 409)
            self.stop_requested = True
            self.pressure_status["phase"] = "restore-requested"
            self._save_operation(operation_id, {"stopRequested": True, "phase": "restore-requested"})
            self.stop_event.set()
            return {"accepted": True, "operationId": operation_id,
                    "restoration": "Baseline restoration and routing recovery verification started"}

    def _set_pressure_status(self, **values: Any) -> None:
        with self.lock:
            self.pressure_status.update(values)
            operation_id = self.active_operation_id
            pressure = dict(self.pressure_status)
            stop_requested = self.stop_requested
        if operation_id:
            terminal = pressure.get("outcome")
            status = str(terminal) if terminal in {"PASS", "FAIL", "STOPPED"} else "RUNNING"
            changes = {"operation": "pressure", "status": status,
                       "phase": pressure.get("phase"), "site": pressure.get("site"),
                       "pool": pressure.get("pool"), "value": pressure.get("value"),
                       "capacity": pressure.get("capacity"), "runId": pressure.get("runId"),
                       "restorationVerified": pressure.get("restorationVerified"),
                       "stopRequested": stop_requested}
            if terminal:
                changes["finishedAtUtc"] = utc_now()
                changes["failureAvailable"] = terminal == "FAIL"
            self._save_operation(operation_id, changes)

    def _run(self, profile: Profile, path: Path, stop_event: threading.Event, operation_id: str) -> None:
        outcome = "FAIL"
        run_id = None
        try:
            result = run_benchmark(self.handoff, profile, path, root=self.evidence_root,
                                   stop_event=stop_event, transport=self.transport,
                                   operation_id=operation_id,
                                   on_progress=self._progress_callback(operation_id))
            outcome = str(result.get("status", "FAIL"))
            if result.get("evidenceDirectory"):
                run_id = Path(str(result["evidenceDirectory"])).name
        except BaseException:
            # Detailed errors belong in credential-safe local evidence. Do not
            # send exception text or credential paths to the browser.
            outcome = "FAIL"
            run_id = self._latest_completed_run_id(self.prior_run_ids)
        finally:
            self._save_operation(operation_id, {"status": outcome, "runId": run_id,
                                                "finishedAtUtc": utc_now(), "stopRequested": stop_event.is_set(),
                                                "failureAvailable": outcome == "FAIL"})
            with self.lock:
                self.last_outcome = outcome
                self.last_run_id = run_id
                self.active_profile = None
                self.active_operation = None
                self.active_operation_id = None
                self.active_scenario = None
                self.active_endpoint_id = None
                self.stop_requested = False
                self.stop_event = None

    def _run_pressure(self, profile: Profile, value: int, capacity: int, site_name: str,
                      stop_event: threading.Event, operation_id: str) -> None:
        assert self.handoff is not None and self.transport is not None
        handoff = self.handoff
        directory: Path | None = None
        run_lock = None
        snapshot = None
        baseline_gate = None
        pressure_gate = None
        mutation_attempted = False
        failure: str | None = None
        restoration: dict[str, Any] = {"verified": False, "notNeeded": True}
        result: dict[str, Any] = {
            "schemaVersion": 1,
            "type": "dashboard-signal-pressure",
            "setupRunId": handoff["runId"],
            "operationId": operation_id,
            "requestedWaitingRequests": value,
            "queueCapacity": capacity,
            "pressureSite": site_name,
            "createdAtUtc": utc_now(),
            "status": "RUNNING",
            "probes": [],
        }
        try:
            handoff = _token_resolved_handoff(handoff)
            directory = new_run_dir(handoff, self.evidence_root)
            result["runId"] = directory.name
            run_lock = _acquire_run_lock(handoff["runId"], directory)
            write_json(directory / "handoff.redacted.json", redacted_handoff(handoff))
            write_json(directory / "source-lock.json", {"setupSource": handoff["source"],
                                                           "gridzillaSource": _source_identity(),
                                                           "runtimeImages": handoff["runtimeImages"]})
            write_json(directory / "operation.json", result)
            doctor_result = doctor(handoff, transport=self.transport)
            write_json(directory / "doctor.json", doctor_result)
            if not doctor_result.get("ok"):
                raise BenchmarkFailure("doctor failed: " + "; ".join(doctor_result.get("problems", [])))
            baseline_gate = _ready_gate(handoff, self.transport, profile)
            write_json(directory / "baseline-state.json", baseline_gate)
            target = next((site for site in handoff["sites"] if site["name"] == site_name), None)
            if (target is None or target["provider"].get("pool") != profile.pressure_target_pool
                    or int(target["provider"]["queueCapacity"]) != capacity):
                raise BenchmarkFailure("selected site changed after the pressure request was accepted")
            baseline_candidate = _candidate_on_client_site(handoff, baseline_gate)
            baseline_site = _expected_site(baseline_candidate, handoff["client"]["attribution"])
            snapshot = snapshot_simulators(handoff, self.transport, directory, pressure_site=site_name)
            baseline_probe = send_probe(handoff, self.transport, evidence_dir=directory, sequence=1,
                                        expected_site=baseline_site, label="manual-pressure-baseline")
            result["probes"].append(baseline_probe)
            write_json(directory / "baseline-probe.json", baseline_probe)
            if stop_event.is_set():
                raise BenchmarkFailure("pressure was cancelled before the simulator signal changed")

            self._set_pressure_status(phase="applying", value=value, capacity=capacity,
                                      site=site_name, pool=target["provider"]["pool"],
                                      restorationVerified=None, runId=directory.name)
            mutation_attempted = True
            mutation_started = monotonic_utc_pair()
            mutation = mutate_simulators(handoff, self.transport, snapshot, value)
            append_jsonl(directory / "signal-boundaries.jsonl", {
                "boundary": "manual-simulator-pressure-applied", "time": mutation_started,
                "completedAt": monotonic_utc_pair(), "requestedWaitingRequests": value,
                "capacity": capacity, "site": site_name, "pods": mutation,
            })
            result["mutation"] = mutation
            self._set_pressure_status(phase="waiting-for-routing", value=value, capacity=capacity,
                                      site=site_name, pool=target["provider"]["pool"],
                                      restorationVerified=None, runId=directory.name)
            pressure_gate, transition = wait_for_signal_transition(
                handoff, self.transport, profile, baseline_gate, site_name,
                evidence_dir=directory, stop_event=stop_event,
                require_selection_change=(baseline_site == site_name))
            result["transition"] = transition
            candidate = _candidate_on_client_site(handoff, pressure_gate)
            selected_site = _expected_site(candidate, handoff["client"]["attribution"])
            pressure_probe = send_probe(handoff, self.transport, evidence_dir=directory, sequence=2,
                                        expected_site=selected_site, label="manual-pressure-selected-site")
            result["probes"].append(pressure_probe)
            write_json(directory / "pressure-state.json", pressure_gate)
            write_json(directory / "pressure-probe.json", pressure_probe)
            self._set_pressure_status(phase="pressure-active", value=value, capacity=capacity,
                                      site=site_name, pool=target["provider"]["pool"],
                                      selectedSite=selected_site, restorationVerified=None,
                                      autoRestoreSeconds=self.PRESSURE_HOLD_SECONDS, runId=directory.name)
            stop_event.wait(self.PRESSURE_HOLD_SECONDS)
            result["restorationRequestedByUser"] = stop_event.is_set()
        except BaseException as exc:
            failure = str(exc)
            result["failure"] = failure
            if directory is not None:
                try:
                    write_json(directory / "failure.json", {"failure": failure, "observedAtUtc": utc_now()})
                except Exception:
                    pass
        finally:
            if snapshot is not None and mutation_attempted and directory is not None:
                self._set_pressure_status(phase="restoring", value=value, capacity=capacity,
                                          site=site_name, restorationVerified=None,
                                          runId=str(directory.name))
                try:
                    restored = restore_simulators(handoff, self.transport, snapshot, directory)
                    recovery = None
                    if baseline_gate is not None:
                        _recovered_gate, recovery = wait_for_recovery(
                            handoff, self.transport, profile, baseline_gate, site_name,
                            evidence_dir=directory, pressured=pressure_gate)
                        recovery_candidate = _candidate_on_client_site(handoff, _recovered_gate)
                        recovery_site = _expected_site(recovery_candidate, handoff["client"]["attribution"])
                        recovery_probe = send_probe(handoff, self.transport, evidence_dir=directory,
                                                    sequence=3, expected_site=recovery_site,
                                                    label="manual-pressure-recovery")
                        result["probes"].append(recovery_probe)
                        write_json(directory / "recovery-probe.json", recovery_probe)
                    restoration = {**restored, "recovery": recovery,
                                   "verified": bool(restored.get("verified") and recovery is not None)}
                except Exception as exc:
                    restoration = {"verified": False, "error": str(exc)}
                    if failure is None:
                        failure = f"simulator restoration or routing recovery failed: {exc}"
                    try:
                        _capture_diagnostics(handoff, directory)
                    except Exception:
                        pass
                try:
                    write_json(directory / "restoration-proof.json", restoration)
                    final_gate = gate_snapshot(handoff, self.transport, max_age_seconds=30)
                    write_json(directory / "end-state.json", final_gate)
                    if not final_gate.get("ok") and failure is None:
                        failure = "final readiness gate failed after simulator restoration"
                except Exception as exc:
                    final_gate = {"ok": False, "error": str(exc)}
                    if failure is None:
                        failure = "could not verify the final live readiness gate"
                result.update({"status": "PASS" if failure is None and restoration.get("verified") else "FAIL",
                               "failure": failure,
                               "restoration": restoration,
                               "endState": final_gate,
                               "finishedAtUtc": utc_now()})
                try:
                    write_json(directory / "pressure-result.json", result)
                    write_json(directory / "operation.json", result)
                    if result["status"] != "PASS":
                        _capture_diagnostics(handoff, directory)
                except Exception:
                    result["status"] = "FAIL"
                self._set_pressure_status(phase="restored" if restoration.get("verified") else "restore-failed",
                                          value=value, capacity=capacity, site=site_name,
                                          restorationVerified=bool(restoration.get("verified")),
                                          runId=directory.name, outcome=result["status"])
            elif directory is not None:
                result.update({"status": "FAIL", "failure": failure or "pressure operation stopped before mutation",
                               "restoration": {"verified": True, "notNeeded": True},
                               "finishedAtUtc": utc_now()})
                try:
                    write_json(directory / "restoration-proof.json", result["restoration"])
                    write_json(directory / "pressure-result.json", result)
                    write_json(directory / "operation.json", result)
                except Exception:
                    pass
                self._set_pressure_status(phase="not-applied", value=value, capacity=capacity,
                                          site=site_name, restorationVerified=True,
                                          runId=directory.name, outcome="FAIL")
            else:
                self._set_pressure_status(phase="failed-before-evidence", value=value,
                                          capacity=capacity, site=site_name,
                                          restorationVerified=None, outcome="FAIL")
            with self.lock:
                self.last_outcome = result.get("status", "FAIL")
                self.last_run_id = directory.name if directory else None
                self.active_profile = None
                self.active_operation = None
                self.active_operation_id = None
                self.active_scenario = None
                self.active_endpoint_id = None
                self.stop_requested = False
                self.stop_event = None
            if run_lock is not None:
                run_lock.close()

    def _latest_completed_run_id(self, excluded: set[str]) -> str | None:
        if self.handoff is None:
            return None
        matches = sorted((path for path in self.evidence_root.glob(f"{self.handoff['runId']}-*/run-result.json")
                          if path.parent.name not in excluded),
                         key=lambda path: path.stat().st_mtime, reverse=True)
        return matches[0].parent.name if matches else None

    def stop(self, operation_id: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        with self.lock:
            if (not self.thread or not self.thread.is_alive() or self.stop_event is None
                    or self.active_operation != "benchmark"):
                raise ControlFailure("no performance profile is currently running", 409)
            if not operation_id or operation_id != self.active_operation_id:
                raise ControlFailure("operationId does not identify the active benchmark", 409)
            self.stop_requested = True
            self._save_operation(operation_id, {"stopRequested": True})
            self.stop_event.set()
            restoration = ("Grid scenario will restore and verify simulator state before stopping"
                           if self.active_scenario == "grid-llmd-signals" else
                           "Generic endpoint traffic will stop at the next bounded window check")
            return {"accepted": True, "operationId": operation_id, "restoration": restoration}

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.lock:
            if (not operation_id or not operation_id.isascii() or not operation_id.isalnum()
                    or len(operation_id) > 64):
                return None
            value = self.operations.get(operation_id)
            if value is None:
                try:
                    saved_operation = json.loads((self.operation_dir / f"{operation_id}.json").read_text(encoding="utf-8"))
                    value = saved_operation if isinstance(saved_operation, dict) else None
                except (OSError, json.JSONDecodeError):
                    value = None
            if value is None:
                return None
            value = dict(value)
            run_id = value.get("runId")
            if isinstance(run_id, str) and Path(run_id).name == run_id:
                path = self.evidence_root / run_id / "live-state.json"
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    saved = None
                if isinstance(saved, dict) and saved.get("operationId") == operation_id:
                    value.update({key: saved[key] for key in self._OPERATION_FIELDS if key in saved})
                    progress = saved.get("trafficProgress")
                    if isinstance(progress, dict):
                        safe_fields = {"action", "runId", "scenario", "phase", "index", "ratePerSecond",
                                       "duration", "requestedBodyBytes", "verifiedBodyBytes", "repeat",
                                       "completedWindows", "expectedWindows"}
                        value["trafficProgress"] = {key: item for key, item in progress.items() if key in safe_fields}
                    if saved.get("failure"):
                        value["failureAvailable"] = True
                        value["failureSummary"] = "Failure details are in the saved local evidence."
            return value

    def close(self) -> None:
        with self.lock:
            thread = self.thread
            if thread and thread.is_alive() and self.stop_event is not None:
                self.stop_requested = True
                self.stop_event.set()
        if thread and thread.is_alive():
            # Keep the shared port-forwards alive until any pressure restoration finishes.
            thread.join()
