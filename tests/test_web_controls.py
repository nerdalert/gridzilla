"""Local dashboard control gates and worker lifecycle tests."""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gridzilla.controls import BenchmarkControls, ControlFailure
from gridzilla.controller import SignalRestorer
from gridzilla.models import load_profile
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
            with self.assertRaisesRegex(ControlFailure, "listed profiles"):
                controls.start("../smoke")

    def test_start_preflights_and_stop_runs_through_the_shared_benchmark(self) -> None:
        started = threading.Event()
        seen_stop = threading.Event()
        handoff = {"runId": "test-run"}

        def fake_run(_handoff, _profile, _path, *, root, stop_event, transport, operation_id, on_progress):
            self.assertEqual(transport, marker_transport)
            self.assertTrue(operation_id, "the shared run should receive the API operation ID")
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
            stopped = controls.stop(accepted["operationId"])
            self.assertTrue(stopped["accepted"], "stop should request graceful restoration")
            controls.close()
            self.assertTrue(seen_stop.is_set(), "the runner must receive the stop signal")
            self.assertEqual(benchmark.call_count, 1)
            self.assertFalse(controls.status()["running"])
            self.assertEqual(controls.status()["lastOutcome"], "FAIL")
            self.assertEqual(controls.status()["lastRunId"], "test-run-created")

    def test_dashboard_benchmark_overrides_are_parameterized_and_safety_checked(self) -> None:
        stepped = load_profile(ROOT / "profiles" / "stepped.json")
        configured = BenchmarkControls._profile_with_overrides(stepped, "10,25,50,75,100", 15)
        self.assertEqual(configured.rates, (10, 25, 50, 75, 100))
        self.assertEqual(configured.window, "15s")
        self.assertEqual(configured.raw["rates"], [10, 25, 50, 75, 100])
        self.assertEqual(configured.raw["window"], "15s")
        with self.assertRaisesRegex(ControlFailure, "increase strictly"):
            BenchmarkControls._profile_with_overrides(stepped, "25,10", 30)
        with self.assertRaisesRegex(ControlFailure, "25,000-request"):
            BenchmarkControls._profile_with_overrides(stepped, "25,50,75,100", 30)
        with self.assertRaisesRegex(ControlFailure, "15 and 600"):
            BenchmarkControls._profile_with_overrides(stepped, "25,50", 10)

    def test_start_passes_overrides_into_the_recorded_profile(self) -> None:
        started = threading.Event()
        observed = {}

        def fake_run(_handoff, profile, _path, *, root, stop_event, transport, operation_id, on_progress):
            observed["rates"] = profile.raw["rates"]
            observed["window"] = profile.raw["window"]
            observed["operationId"] = operation_id
            started.set()
            return {"status": "PASS", "evidenceDirectory": str(root / "test-run-parameters")}

        with TemporaryDirectory() as temporary, \
             patch("gridzilla.controls.doctor", return_value={"ok": True}), \
             patch("gridzilla.controls.run_benchmark", side_effect=fake_run):
            controls = BenchmarkControls(enabled=True, handoff={"runId": "test-run"},
                                         evidence_root=Path(temporary), profiles_dir=ROOT / "profiles",
                                         transport=object())  # type: ignore[arg-type]
            accepted = controls.start("stepped", "10,25,50,75,100", 15)
            self.assertEqual(accepted["rates"], [10, 25, 50, 75, 100])
            self.assertEqual(accepted["soakSeconds"], 15)
            self.assertTrue(started.wait(1), "the worker should receive the customized profile")
            controls.close()
        self.assertTrue(observed.pop("operationId"))
        self.assertEqual(observed, {"rates": [10, 25, 50, 75, 100], "window": "15s"})

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

    def test_queue_pressure_options_are_derived_from_the_marked_pool_capacity(self) -> None:
        handoff = {"sites": [
            {"name": "west", "provider": {"pressureTarget": True, "pool": "pool-a", "queueCapacity": 4},
             "simulator": {"adminUrl": "http://sim", "targets": [{}], "pressureJsonPointer": "/pressure"}},
            {"name": "central", "provider": {"pressureTarget": False, "pool": "pool-b", "queueCapacity": 8},
             "simulator": {"adminUrl": "http://sim", "targets": [{}], "pressureJsonPointer": "/pressure"}},
        ]}
        self.assertEqual(BenchmarkControls._pressure_options(handoff), [
            {"value": 2, "capacity": 4, "label": "2 / 4", "site": "west", "pool": "pool-a"},
            {"value": 4, "capacity": 4, "label": "4 / 4", "site": "west", "pool": "pool-a"},
            {"value": 4, "capacity": 8, "label": "4 / 8", "site": "central", "pool": "pool-b"},
            {"value": 8, "capacity": 8, "label": "8 / 8", "site": "central", "pool": "pool-b"},
        ])
        self.assertEqual(BenchmarkControls._pressure_options({"sites": []}), [])
        odd = {"sites": [{"name": "west", "provider": {"pressureTarget": True,
                                                               "pool": "pool-a", "queueCapacity": 5}}]}
        self.assertEqual(BenchmarkControls._pressure_options(odd), [],
                         "the UI should not label a rounded value as half of an odd capacity")

    def test_manual_pressure_restores_captured_simulator_configuration(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            west = {"name": "west", "provider": {"pressureTarget": True, "pool": "pool-a", "queueCapacity": 4},
                    "simulator": {"adminUrl": "http://sim", "targets": [{}], "pressureJsonPointer": "/pressure"}}
            site = {"name": "central", "provider": {"pressureTarget": False, "pool": "pool-b", "queueCapacity": 4},
                    "simulator": {"adminUrl": "http://sim", "targets": [{}], "pressureJsonPointer": "/pressure"}}
            handoff = {"runId": "pressure-test", "_resolvedClientCredential": "/unused",
                       "source": {}, "runtimeImages": [],
                       "sites": [{**west, "kubeconfig": "/unused", "context": "kind-west"},
                                 {**site, "kubeconfig": "/unused", "context": "kind-central"}],
                       "client": {"site": "west", "attribution": {}}}
            profile = load_profile(ROOT / "profiles" / "smoke.json")
            gate = {"ok": True, "sites": []}
            candidate = {"key": "west"}
            lock = type("Lock", (), {"close": lambda _self: None})()
            with patch("gridzilla.controls.doctor", return_value={"ok": True}), \
                 patch("gridzilla.controls._ready_gate", return_value=gate), \
                 patch("gridzilla.controls._candidate_on_client_site", return_value=candidate), \
                 patch("gridzilla.controls._expected_site", return_value="west"), \
                 patch("gridzilla.controls._acquire_run_lock", return_value=lock), \
                 patch("gridzilla.controls.snapshot_simulators", return_value={"pressureSite": "central"}) as snapshot, \
                 patch("gridzilla.controls.mutate_simulators", return_value=[{"observedValue": 2}]) as mutate, \
                 patch("gridzilla.controls.wait_for_signal_transition", return_value=(gate, {"selectionChanged": False})) as transition, \
                 patch("gridzilla.controls.send_probe", return_value={"status": 200}) as probe, \
                 patch("gridzilla.controls.restore_simulators", return_value={"verified": True}) as restore, \
                 patch("gridzilla.controls.wait_for_recovery", return_value=(gate, {"selectionRestored": True})), \
                 patch("gridzilla.controls.gate_snapshot", return_value=gate):
                controls = BenchmarkControls(enabled=True, handoff=handoff, evidence_root=root,
                                             profiles_dir=ROOT / "profiles", transport=object())  # type: ignore[arg-type]
                controls.PRESSURE_HOLD_SECONDS = 0
                accepted = controls.start_pressure(2, "central")
                self.assertTrue(accepted["accepted"], "a listed pressure value should start the temporary operation")
                deadline = time.monotonic() + 3
                while controls.status()["running"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(controls.status()["running"], "pressure control must finish and restore without manual teardown")
                evidence = list(root.glob("pressure-test-*/pressure-result.json"))
                self.assertEqual(controls.status()["lastOutcome"], "PASS",
                                 evidence[0].read_text(encoding="utf-8") if evidence else "pressure evidence was not written")
                self.assertTrue(controls.status()["pressure"]["restorationVerified"])
                snapshot.assert_called_once()
                self.assertEqual(snapshot.call_args.kwargs["pressure_site"], "central",
                                 "manual pressure must use the region selected in its card")
                self.assertEqual(transition.call_args.args[4], "central")
                self.assertFalse(transition.call_args.kwargs["require_selection_change"],
                                 "pressure on a non-selected region need not change the current top provider")
                self.assertEqual(mutate.call_args.args[3], 2)
                restore.assert_called_once()
                self.assertEqual(probe.call_count, 3, "baseline, pressure, and recovery each need an attributed probe")

    def test_manual_pressure_restores_after_routing_transition_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            events: list[str] = []
            site = {"name": "west", "provider": {"pressureTarget": True, "pool": "pool-a", "queueCapacity": 4},
                    "simulator": {"adminUrl": "http://sim", "targets": [{}], "pressureJsonPointer": "/pressure"}}
            handoff = {"runId": "pressure-failure", "_resolvedClientCredential": "/unused",
                       "source": {}, "runtimeImages": [],
                       "sites": [{**site, "kubeconfig": "/unused", "context": "kind-west"}],
                       "client": {"site": "west", "attribution": {}}}
            gate = {"ok": True, "sites": []}

            def restore(*_args, **_kwargs):
                events.append("restore")
                return {"verified": True}

            def diagnostics(*_args, **_kwargs):
                events.append("diagnostics")

            def fail_transition(*_args, **_kwargs):
                events.append("transition")
                raise RuntimeError("routing did not converge")

            with patch("gridzilla.controls.doctor", return_value={"ok": True}), \
                 patch("gridzilla.controls._ready_gate", return_value=gate), \
                 patch("gridzilla.controls._candidate_on_client_site", return_value={"key": "west"}), \
                 patch("gridzilla.controls._expected_site", return_value="west"), \
                 patch("gridzilla.controls.snapshot_simulators", return_value={"pressureSite": "west"}), \
                 patch("gridzilla.controls.mutate_simulators", side_effect=lambda *_args: events.append("mutate")), \
                 patch("gridzilla.controls.wait_for_signal_transition", side_effect=fail_transition), \
                 patch("gridzilla.controls.send_probe", return_value={"status": 200}), \
                 patch("gridzilla.controls.restore_simulators", side_effect=restore), \
                 patch("gridzilla.controls.wait_for_recovery", return_value=(gate, {"selectionRestored": True})), \
                 patch("gridzilla.controls.gate_snapshot", return_value=gate), \
                 patch("gridzilla.controls._capture_diagnostics", side_effect=diagnostics):
                controls = BenchmarkControls(enabled=True, handoff=handoff, evidence_root=root,
                                             profiles_dir=ROOT / "profiles", transport=object())  # type: ignore[arg-type]
                controls.start_pressure(2, "west")
                deadline = time.monotonic() + 3
                while controls.status()["running"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(controls.status()["running"], "failed signal convergence must reach finalization")
                result_path = next(root.glob("pressure-failure-*/pressure-result.json"))
                result = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "FAIL", "a failed routing transition must not be reported as a pass")
                self.assertTrue(result["restoration"]["verified"], "the original simulator state must still be restored")
                self.assertLess(events.index("restore"), events.index("diagnostics"),
                                "diagnostics must not delay restoring synthetic pressure")

    def test_manual_pressure_rejects_values_outside_the_two_safe_steps(self) -> None:
        with TemporaryDirectory() as temporary:
            handoff = {"runId": "pressure-test", "sites": [
                {"name": "west", "provider": {"pressureTarget": True, "pool": "pool-a", "queueCapacity": 4},
                 "simulator": {"adminUrl": "http://sim", "targets": [{}], "pressureJsonPointer": "/pressure"}},
            ]}
            controls = BenchmarkControls(enabled=True, handoff=handoff, evidence_root=Path(temporary),
                                         profiles_dir=ROOT / "profiles", transport=object())  # type: ignore[arg-type]
            with patch("gridzilla.controls.doctor") as doctor:
                with self.assertRaisesRegex(ControlFailure, "listed queue-pressure levels"):
                    controls.start_pressure(3, "west")
                doctor.assert_not_called()

    def test_restore_pressure_requests_the_worker_finalizer(self) -> None:
        with TemporaryDirectory() as temporary:
            controls = BenchmarkControls(enabled=False, handoff=None, evidence_root=Path(temporary),
                                         profiles_dir=ROOT / "profiles", transport=None)
            event = threading.Event()
            operation_id = "a" * 32
            controls.thread = type("ActiveThread", (), {"is_alive": lambda _self: True})()  # type: ignore[assignment]
            controls.active_operation = "pressure"
            controls.active_operation_id = operation_id
            controls.stop_event = event
            controls.pressure_status = {"phase": "pressure-active"}
            controls.enabled = True
            response = controls.restore_pressure(operation_id)
            self.assertTrue(response["accepted"], "manual restore should be accepted for an active pressure operation")
            self.assertTrue(event.is_set(), "restore must wake the worker so its finalizer restores the baseline")
            self.assertEqual(controls.pressure_status["phase"], "restore-requested")

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
