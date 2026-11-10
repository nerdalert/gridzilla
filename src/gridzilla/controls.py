"""Explicitly enabled, localhost-only dashboard controls."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .controller import (
    BenchmarkFailure,
    _acquire_run_lock,
    _candidate_on_client_site,
    _capture_diagnostics,
    _expected_site,
    _pressure_target,
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
from .models import Profile, ValidationError, load_profile, redacted_handoff
from .runtime import EndpointTransport, monotonic_utc_pair, utc_now


class ControlFailure(RuntimeError):
    """A safe dashboard control request was rejected."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class BenchmarkControls:
    """Run profiles or a temporary signal probe using the dashboard transport."""

    PROFILE_NAMES = ("smoke", "baseline", "stepped")
    PRESSURE_HOLD_SECONDS = 30

    def __init__(self, *, enabled: bool, handoff: dict[str, Any] | None,
                 evidence_root: Path, profiles_dir: Path,
                 transport: EndpointTransport | None):
        self.enabled = enabled
        self.handoff = handoff
        self.evidence_root = evidence_root.resolve()
        self.profiles: dict[str, tuple[Profile, Path]] = {}
        self.transport = transport
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.active_profile: str | None = None
        self.active_operation: str | None = None
        self.stop_requested = False
        self.last_outcome: str | None = None
        self.last_run_id: str | None = None
        self.pressure_status: dict[str, Any] = {"phase": "idle", "restorationVerified": None}
        self.prior_run_ids: set[str] = set()
        self.pressure_options: list[dict[str, Any]] = self._pressure_options(handoff)

        if not enabled:
            return
        if handoff is None:
            raise ValidationError("--allow-controls requires --handoff")
        if transport is None:
            raise ValidationError("dashboard controls require a ready local cluster transport")
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

    @staticmethod
    def _pressure_options(handoff: dict[str, Any] | None) -> list[dict[str, Any]]:
        if handoff is None:
            return []
        marked = [site for site in handoff.get("sites", [])
                  if site.get("provider", {}).get("pressureTarget") is True]
        if len(marked) != 1:
            return []
        try:
            capacity = int(marked[0]["provider"]["queueCapacity"])
            if (capacity < 2 or capacity % 2 != 0
                    or float(marked[0]["provider"]["queueCapacity"]) != capacity):
                return []
        except (KeyError, TypeError, ValueError):
            return []
        midpoint = capacity // 2
        if midpoint <= 0 or midpoint >= capacity:
            return []
        return [
            {"value": midpoint, "capacity": capacity, "label": f"{midpoint} / {capacity}",
             "site": marked[0]["name"], "pool": marked[0]["provider"].get("pool")},
            {"value": capacity, "capacity": capacity, "label": f"{capacity} / {capacity}",
             "site": marked[0]["name"], "pool": marked[0]["provider"].get("pool")},
        ]

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = bool(self.thread and self.thread.is_alive())
            return {
                "enabled": self.enabled,
                "profiles": list(self.profiles),
                "running": running,
                "operation": self.active_operation if running else None,
                "profile": self.active_profile if running else None,
                "stopRequested": self.stop_requested if running else False,
                "lastOutcome": self.last_outcome,
                "lastRunId": self.last_run_id,
                "pressureOptions": self.pressure_options,
                "pressure": dict(self.pressure_status),
            }

    def _ensure_available(self) -> None:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        if self.handoff is None or self.transport is None:
            raise ControlFailure("dashboard has no active run-owned environment", 503)
        if self.thread and self.thread.is_alive():
            raise ControlFailure("another Gridzilla operation is already running", 409)

    def start(self, profile_name: str) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        if profile_name not in self.profiles:
            raise ControlFailure("choose one of the listed profiles", 400)
        if self.handoff is None or self.transport is None:
            raise ControlFailure("dashboard has no active run-owned environment", 503)

        with self.lock:
            self._ensure_available()
            profile, profile_path = self.profiles[profile_name]
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
            self.last_outcome = None
            self.last_run_id = None
            self.thread = threading.Thread(
                target=self._run,
                args=(profile, profile_path, stop_event),
                name="gridzilla-dashboard-benchmark",
                daemon=False,
            )
            self.thread.start()
            return {"accepted": True, "profile": profile_name}

    def start_pressure(self, value: Any) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        try:
            requested = int(value)
        except (TypeError, ValueError):
            raise ControlFailure("choose one of the listed queue-pressure levels", 400) from None
        option = next((item for item in self.pressure_options if item["value"] == requested), None)
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
            try:
                target = _pressure_target(self.handoff, profile)
            except BenchmarkFailure as exc:
                raise ControlFailure("handoff pressure target does not match the smoke profile", 409) from exc
            self.stop_event = threading.Event()
            self.stop_requested = False
            self.active_profile = None
            self.active_operation = "pressure"
            self.last_outcome = None
            self.last_run_id = None
            self.pressure_status = {"phase": "accepted", "value": requested,
                                    "capacity": option["capacity"], "site": target["name"],
                                    "pool": target["provider"]["pool"], "restorationVerified": None}
            self.thread = threading.Thread(
                target=self._run_pressure,
                args=(profile, requested, option["capacity"], target["name"], self.stop_event),
                name="gridzilla-dashboard-pressure",
                daemon=False,
            )
            self.thread.start()
            return {"accepted": True, "value": requested, "capacity": option["capacity"],
                    "site": target["name"], "pool": target["provider"]["pool"]}

    def restore_pressure(self) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        with self.lock:
            if (not self.thread or not self.thread.is_alive() or self.active_operation != "pressure"
                    or self.stop_event is None):
                raise ControlFailure("no temporary pressure operation is currently running", 409)
            self.stop_requested = True
            self.pressure_status["phase"] = "restore-requested"
            self.stop_event.set()
            return {"accepted": True, "restoration": "Baseline restoration and routing recovery verification started"}

    def _set_pressure_status(self, **values: Any) -> None:
        with self.lock:
            self.pressure_status.update(values)

    def _run(self, profile: Profile, path: Path, stop_event: threading.Event) -> None:
        outcome = "FAIL"
        run_id = None
        try:
            result = run_benchmark(self.handoff, profile, path, root=self.evidence_root,
                                   stop_event=stop_event, transport=self.transport)
            outcome = str(result.get("status", "FAIL"))
            if result.get("evidenceDirectory"):
                run_id = Path(str(result["evidenceDirectory"])).name
        except BaseException:
            # Detailed errors belong in credential-safe local evidence. Do not
            # send exception text or credential paths to the browser.
            outcome = "FAIL"
            run_id = self._latest_completed_run_id(self.prior_run_ids)
        finally:
            with self.lock:
                self.last_outcome = outcome
                self.last_run_id = run_id
                self.active_profile = None
                self.active_operation = None
                self.stop_requested = False
                self.stop_event = None

    def _run_pressure(self, profile: Profile, value: int, capacity: int, site_name: str,
                      stop_event: threading.Event) -> None:
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
            target = _pressure_target(handoff, profile)
            if target["name"] != site_name or int(target["provider"]["queueCapacity"]) != capacity:
                raise BenchmarkFailure("marked pressure target changed after the request was accepted")
            baseline_candidate = _candidate_on_client_site(handoff, baseline_gate)
            baseline_site = _expected_site(baseline_candidate, handoff["client"]["attribution"])
            if baseline_site != site_name:
                raise BenchmarkFailure(f"pressure target {site_name} is not currently selected at the client Gateway ({baseline_site})")
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
                evidence_dir=directory, stop_event=stop_event)
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

    def stop(self) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        with self.lock:
            if (not self.thread or not self.thread.is_alive() or self.stop_event is None
                    or self.active_operation != "benchmark"):
                raise ControlFailure("no performance profile is currently running", 409)
            self.stop_requested = True
            self.stop_event.set()
            return {"accepted": True, "restoration": "Gridzilla will restore and verify simulator state before stopping"}

    def close(self) -> None:
        with self.lock:
            thread = self.thread
            if thread and thread.is_alive() and self.stop_event is not None:
                self.stop_requested = True
                self.stop_event.set()
        if thread and thread.is_alive():
            # Keep the shared port-forwards alive until any pressure restoration finishes.
            thread.join()
