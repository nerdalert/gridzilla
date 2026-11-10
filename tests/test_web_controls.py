"""Local dashboard control gates and worker lifecycle tests."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gridzilla.controls import BenchmarkControls, ControlFailure
from gridzilla.controller import SignalRestorer
from gridzilla.web import _local_control_origin


ROOT = Path(__file__).resolve().parents[1]


class WebControlTests(unittest.TestCase):
    def test_controls_require_explicit_enablement_and_known_profile(self) -> None:
        disabled = BenchmarkControls(enabled=False, handoff=None, evidence_root=Path("/tmp/runs"),
                                     profiles_dir=ROOT / "profiles", transport=None)
        with self.assertRaisesRegex(ControlFailure, "--allow-controls"):
            disabled.start("smoke")

        with TemporaryDirectory() as temporary:
            controls = BenchmarkControls(enabled=True, handoff={"runId": "test"},
                                         evidence_root=Path(temporary), profiles_dir=ROOT / "profiles",
                                         transport=object())  # type: ignore[arg-type]
            with self.assertRaisesRegex(ControlFailure, "listed bounded profiles"):
                controls.start("../smoke")

    def test_start_preflights_and_stop_runs_through_the_shared_benchmark(self) -> None:
        started = threading.Event()
        seen_stop = threading.Event()
        handoff = {"runId": "test-run"}

        def fake_run(_handoff, _profile, _path, *, root, stop_event, transport):
            self.assertEqual(transport, marker_transport)
            started.set()
            stop_event.wait(2)
            seen_stop.set()
            return {"status": "FAIL", "evidenceDirectory": str(root / "test-run-created")}

        marker_transport = object()
        with TemporaryDirectory() as temporary, \
             patch("gridzilla.controls.doctor", return_value={"ok": True}) as doctor, \
             patch("gridzilla.controls.run_benchmark", side_effect=fake_run) as benchmark:
            controls = BenchmarkControls(enabled=True, handoff=handoff,
                                         evidence_root=Path(temporary), profiles_dir=ROOT / "profiles",
                                         transport=marker_transport)  # type: ignore[arg-type]
            accepted = controls.start("smoke")
            self.assertTrue(accepted["accepted"], "a ready benchmark should be accepted")
            self.assertTrue(started.wait(1), "the shared benchmark runner should start asynchronously")
            doctor.assert_called_once()
            with self.assertRaisesRegex(ControlFailure, "already running"):
                controls.start("smoke")
            stopped = controls.stop()
            self.assertTrue(stopped["accepted"], "stop should request graceful restoration")
            controls.close()
            self.assertTrue(seen_stop.is_set(), "the runner must receive the stop signal")
            self.assertEqual(benchmark.call_count, 1)
            self.assertFalse(controls.status()["running"])
            self.assertEqual(controls.status()["lastOutcome"], "FAIL")
            self.assertEqual(controls.status()["lastRunId"], "test-run-created")

    def test_failed_readiness_preflight_does_not_start_a_benchmark(self) -> None:
        with TemporaryDirectory() as temporary, \
             patch("gridzilla.controls.doctor", return_value={"ok": False, "problems": ["stale gateway"]}), \
             patch("gridzilla.controls.run_benchmark") as benchmark:
            controls = BenchmarkControls(enabled=True, handoff={"runId": "test-run"},
                                         evidence_root=Path(temporary), profiles_dir=ROOT / "profiles",
                                         transport=object())  # type: ignore[arg-type]
            with self.assertRaisesRegex(ControlFailure, "readiness"):
                controls.start("smoke")
            benchmark.assert_not_called()

    def test_pre_result_failure_does_not_misreport_a_previous_run(self) -> None:
        with TemporaryDirectory() as temporary, \
             patch("gridzilla.controls.doctor", return_value={"ok": True}), \
             patch("gridzilla.controls.run_benchmark", side_effect=RuntimeError("failed before run output")):
            root = Path(temporary)
            previous = root / "test-run-old"
            previous.mkdir()
            (previous / "run-result.json").write_text("{}", encoding="utf-8")
            controls = BenchmarkControls(enabled=True, handoff={"runId": "test-run"},
                                         evidence_root=root, profiles_dir=ROOT / "profiles",
                                         transport=object())  # type: ignore[arg-type]
            controls.start("smoke")
            controls.close()
            self.assertEqual(controls.status()["lastOutcome"], "FAIL")
            self.assertIsNone(controls.status()["lastRunId"],
                              "a failed launch must not point the UI at an older passing run")

    def test_controls_accept_only_same_origin_loopback_requests(self) -> None:
        self.assertTrue(_local_control_origin("127.0.0.1:8765", "http://127.0.0.1:8765", 8765))
        self.assertTrue(_local_control_origin("localhost:8765", "http://localhost:8765", 8765))
        self.assertFalse(_local_control_origin("127.0.0.1:8765", None, 8765))
        self.assertFalse(_local_control_origin("evil.example:8765", "http://evil.example:8765", 8765))
        self.assertFalse(_local_control_origin("127.0.0.1:8765", "http://evil.example", 8765))
        self.assertFalse(_local_control_origin("127.0.0.1:8765", "http://127.0.0.1:8000", 8765))
        self.assertFalse(_local_control_origin("127.0.0.1:8765", "http://[invalid", 8765))

    def test_signal_handler_installation_is_skipped_for_dashboard_workers(self) -> None:
        called: list[int] = []

        def worker() -> None:
            with SignalRestorer():
                pass

        with patch("gridzilla.controller.signal.signal", side_effect=lambda *args: called.append(args[0])):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "signal handling must not block the worker")
        self.assertEqual(called, [], "worker threads must use the dashboard stop event, not process signals")


if __name__ == "__main__":
    unittest.main()
