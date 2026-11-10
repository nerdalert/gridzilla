"""Explicitly enabled, localhost-only dashboard benchmark controls."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .controller import doctor, run_benchmark
from .models import Profile, ValidationError, load_profile
from .runtime import EndpointTransport


class ControlFailure(RuntimeError):
    """A safe dashboard control request was rejected."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class BenchmarkControls:
    """Run the CLI's bounded benchmark using the dashboard's live transport."""

    PROFILE_NAMES = ("smoke", "baseline", "stepped")

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
        self.stop_requested = False
        self.last_outcome: str | None = None
        self.last_run_id: str | None = None
        self.prior_run_ids: set[str] = set()

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

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = bool(self.thread and self.thread.is_alive())
            return {
                "enabled": self.enabled,
                "profiles": list(self.profiles),
                "running": running,
                "profile": self.active_profile if running else None,
                "stopRequested": self.stop_requested if running else False,
                "lastOutcome": self.last_outcome,
                "lastRunId": self.last_run_id,
            }

    def start(self, profile_name: str) -> dict[str, Any]:
        if not self.enabled:
            raise ControlFailure("dashboard controls are disabled; restart with --allow-controls", 403)
        if profile_name not in self.profiles:
            raise ControlFailure("choose one of the listed bounded profiles", 400)
        if self.handoff is None or self.transport is None:
            raise ControlFailure("dashboard has no active run-owned environment", 503)

        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ControlFailure("a benchmark is already running", 409)
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
                self.stop_requested = False
                self.stop_event = None

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
            if not self.thread or not self.thread.is_alive() or self.stop_event is None:
                raise ControlFailure("no benchmark is currently running", 409)
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
            # Do not close the shared port-forwards while a run is restoring.
            thread.join()
