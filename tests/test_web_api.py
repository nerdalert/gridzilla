from __future__ import annotations

import json
import os
import threading
import time
import unittest
from contextlib import nullcontext
from http.client import HTTPResponse
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from gridzilla.controls import BenchmarkControls
from gridzilla.web import LiveState, _gateway_revision_is_current, _live_snapshot_max_age, serve

ROOT = Path(__file__).resolve().parents[1]


class FakeGridLiveState:
    def __init__(self, handoff, refresh_seconds, evidence_root):
        self.handoff = handoff
        self.transport = object()
        self.max_live_age_seconds = 15

    def read(self):
        return {"status": "UNAVAILABLE", "sites": [], "timeline": []}

    def close(self):
        pass


class LocalApiTests(unittest.TestCase):
    def test_live_snapshot_expiry_allows_collection_time_but_remains_bounded(self):
        handoff = {"sites": [{"epp": {"maxSampleAgeSeconds": 30}} for _ in range(3)]}
        self.assertEqual(_live_snapshot_max_age(3, handoff), 16)
        handoff["sites"][1]["epp"]["maxSampleAgeSeconds"] = 9
        self.assertEqual(_live_snapshot_max_age(3, handoff), 9)

    def test_stalled_live_observer_expires_after_collection_budget(self):
        state = object.__new__(LiveState)
        state.lock = threading.Lock()
        state.max_live_age_seconds = 16
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        state.value = {"status": "LIVE", "observedAtUtc": old,
                       "sites": [{"status": "LIVE", "metrics": {"status": "LIVE"}}]}
        value = state.read()
        self.assertEqual(value["status"], "STALE")
        self.assertEqual(value["reason"], "live observer has not produced a fresh complete snapshot")
        self.assertEqual(value["sites"][0]["status"], "STALE")
        self.assertEqual(value["sites"][0]["metrics"]["status"], "STALE")

    def test_gateway_revision_gate_requires_both_revision_ids(self):
        self.assertTrue(_gateway_revision_is_current({
            "overlay": {"revision": "rev-a"}, "gateway": {"servingRevision": "rev-a"}}))
        self.assertFalse(_gateway_revision_is_current({
            "overlay": {"revision": None}, "gateway": {"servingRevision": None}}))
        self.assertFalse(_gateway_revision_is_current({
            "overlay": {"revision": "rev-a"}, "gateway": {"servingRevision": None}}))
        self.assertFalse(_gateway_revision_is_current({
            "overlay": {"revision": "rev-a"}, "gateway": {"servingRevision": "rev-b"}}))

    def _request(self, base: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                 token: str | None = None, origin: str | None = None):
        headers = {}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        request = Request(base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                value = response.read()
                return response.status, json.loads(value) if "application/json" in response.headers.get("Content-Type", "") else value
        except HTTPError as exc:
            value = exc.read()
            return exc.code, json.loads(value) if "application/json" in exc.headers.get("Content-Type", "") else value

    def _serve(self, root: Path, *, endpoints_dir: Path | None, token_file: Path,
               handoff: dict[str, Any] | None = None, patched_live_state: bool = False):
        dist = root / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>test</html>", encoding="utf-8")
        stopped = threading.Event()
        ready = threading.Event()
        bound: list[int] = []
        kwargs = {"handoff": handoff, "runs_dir": root / "runs", "host": "127.0.0.1", "port": 0,
                  "refresh_seconds": 1, "dist": dist, "allow_controls": True,
                  "profiles_dir": ROOT / "profiles", "endpoints_dir": endpoints_dir,
                  "agent_token_file": token_file, "_shutdown_event": stopped,
                  "_on_ready": lambda port: (bound.append(port), ready.set())}
        server_thread = threading.Thread(target=serve, kwargs=kwargs, daemon=True)
        if patched_live_state:
            live_patch = patch("gridzilla.web.LiveState", FakeGridLiveState)
        else:
            live_patch = nullcontext()
        live_patch.__enter__()
        server_thread.start()
        self.assertTrue(ready.wait(3), "local control API should bind to an ephemeral loopback port")
        return f"http://127.0.0.1:{bound[0]}", stopped, server_thread, live_patch

    def test_generic_api_discovery_preflight_start_poll_report_and_safe_stop(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            endpoint_dir = root / "endpoints"
            endpoint_dir.mkdir()
            (endpoint_dir / "local.json").write_text(json.dumps({
                "id": "local-api", "name": "Local JSON API", "url": "http://127.0.0.1:9/submit",
                "requestTemplate": {"prompt": {"text": "small"}, "limit": 3},
                "padJsonPointer": "/prompt/text", "expectedStatuses": [200],
                "responseHeaderDimensions": {"region": {"header": "X-Region", "allowedValues": ["north", "south"]}},
            }), encoding="utf-8")
            token_file = root / "agent-token"
            token_file.write_text("a" * 64, encoding="ascii")
            os.chmod(token_file, 0o600)
            entered_runner = threading.Event()

            def fake_runner(endpoint, profile, profile_path, *, root, stop_event, on_progress, operation_id):
                self.assertEqual(endpoint.id, "local-api")
                self.assertEqual(profile.request_body_sizes_bytes, (16384, 65536, 262144))
                run_dir = root / "api-run"
                run_dir.mkdir()
                (run_dir / "report.md").write_text("saved generic report", encoding="utf-8")
                (run_dir / "run-result.json").write_text(json.dumps({
                    "runId": "api-run", "scenario": "generic-endpoint", "status": "STOPPED",
                    "endpoint": endpoint.public(), "profile": profile.raw, "windows": [],
                    "restoration": {"verified": None, "notApplicable": True},
                }), encoding="utf-8")
                on_progress({"action": "measurement-start", "scenario": "generic-endpoint",
                             "phase": "baseline", "index": "001", "ratePerSecond": 10,
                             "requestedBodyBytes": 16384, "body": "must-not-be-returned"})
                entered_runner.set()
                stop_event.wait(3)
                return {"status": "STOPPED", "runId": "api-run", "evidenceDirectory": str(run_dir)}

            api, stopped, thread, live_patch = self._serve(root, endpoints_dir=endpoint_dir,
                                                           token_file=token_file)
            try:
                token = token_file.read_text(encoding="ascii")
                status, discovery = self._request(api, "/api/configurations")
                self.assertEqual(status, 200)
                self.assertEqual(discovery["scenarios"], ["generic-endpoint"])
                self.assertEqual([item["id"] for item in discovery["endpointConfigurations"]], ["local-api"])
                self.assertNotIn("requestTemplate", json.dumps(discovery))
                status, unauthenticated = self._request(api, "/api/preflight", method="POST", body={})
                self.assertEqual(status, 403)
                self.assertEqual(unauthenticated["error"]["code"], "LOCAL_AUTH_REQUIRED")

                with patch("gridzilla.controls.generic_preflight", return_value={"ok": True, "status": 200,
                                                                               "plan": {"windowCount": 36}}):
                    status, browser_preflight = self._request(api, "/api/preflight", method="POST",
                                                              body={"scenario": "generic-endpoint", "configurationId": "local-api",
                                                                    "profile": "body-sizes"},
                                                              origin=api)
                    self.assertEqual(status, 200, "same-origin browser controls should not need the agent token")
                    self.assertEqual(browser_preflight["status"], "READY")
                    self.assertEqual(browser_preflight["endpointStatus"], 200)

                status, rejected = self._request(api, "/api/preflight", method="POST",
                                                 body={"scenario": "generic-endpoint", "configurationId": "local-api",
                                                       "profile": "body-sizes", "url": "http://attacker/"},
                                                 token=token)
                self.assertEqual(status, 400)
                self.assertEqual(rejected["status"], "REJECTED")
                status, no_grid_pressure = self._request(api, "/api/pressure", method="POST",
                                                         body={"site": "west", "value": 2}, token=token)
                self.assertEqual(status, 400, "generic endpoint mode rejects Grid-only simulator pressure")
                self.assertEqual(no_grid_pressure["error"]["code"], "CONTROL_REJECTED")

                with patch("gridzilla.controls.generic_preflight", return_value={"ok": True, "status": 200,
                                                                               "plan": {"windowCount": 36}}), \
                     patch("gridzilla.controls.run_generic_benchmark", side_effect=fake_runner):
                    status, preflight = self._request(api, "/api/preflight", method="POST",
                                                      body={"scenario": "generic-endpoint", "configurationId": "local-api",
                                                            "profile": "body-sizes", "rates": "10", "soakSeconds": 15,
                                                            "requestBodySizesBytes": [16384, 65536, 262144]},
                                                      token=token)
                    self.assertEqual(status, 200)
                    self.assertEqual(preflight["status"], "READY")
                    self.assertEqual(preflight["endpointStatus"], 200)
                    self.assertEqual(preflight["plan"]["windowCount"], 36)
                    status, started = self._request(api, "/api/run", method="POST",
                                                    body={"scenario": "generic-endpoint", "configurationId": "local-api",
                                                          "profile": "body-sizes", "rates": "10", "soakSeconds": 15,
                                                          "requestBodySizesBytes": [16384, 65536, 262144]},
                                                    token=token)
                    self.assertEqual(status, 202)
                    operation_id = started["operationId"]
                    self.assertEqual(len(operation_id), 32)
                    self.assertTrue(entered_runner.wait(2), "API start must use the shared generic runner")
                    status, progress = self._request(api, f"/api/operations/{operation_id}")
                    self.assertEqual(status, 200)
                    self.assertEqual(progress["status"], "RUNNING")
                    self.assertNotIn("must-not-be-returned", json.dumps(progress))
                    status, bad_stop = self._request(api, "/api/stop", method="POST",
                                                     body={"operationId": "b" * 32}, token=token)
                    self.assertEqual(status, 409)
                    self.assertEqual(bad_stop["error"]["code"], "CONTROL_REJECTED")
                    status, cross_origin = self._request(api, "/api/stop", method="POST",
                                                         body={"operationId": operation_id}, token=token,
                                                         origin="https://evil.example")
                    self.assertEqual(status, 403, "a bearer token must not override a browser Origin")
                    status, stopped_value = self._request(api, "/api/stop", method="POST",
                                                          body={"operationId": operation_id}, token=token)
                    self.assertEqual(status, 202)
                    self.assertEqual(stopped_value["operationId"], operation_id)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        status, final = self._request(api, f"/api/operations/{operation_id}")
                        if status == 200 and final["status"] != "RUNNING":
                            break
                        time.sleep(0.02)
                    self.assertEqual(final["status"], "STOPPED")
                    self.assertEqual(final["scenario"], "generic-endpoint")
                    self.assertEqual(final["runId"], "api-run")
                    status, runs = self._request(api, "/api/runs")
                    self.assertEqual(status, 200)
                    self.assertIn("api-run", [item["id"] for item in runs])
                    status, report = self._request(api, "/api/runs/api-run")
                    self.assertEqual(status, 200)
                    self.assertEqual(report["run"]["scenario"]["id"], "generic-endpoint")
                    status, file_value = self._request(api, "/api/runs/api-run/files/report.md")
                    self.assertEqual(status, 200)
                    self.assertIn(b"saved generic report", file_value)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        final_operation = self._request(api, f"/api/operations/{operation_id}")[1]
                        if final_operation["status"] != "RUNNING":
                            break
                        time.sleep(0.02)
                    self.assertEqual(final_operation["status"], "STOPPED")
            finally:
                stopped.set()
                thread.join(timeout=3)
                live_patch.__exit__(None, None, None)
                self.assertFalse(thread.is_alive(), "test server should close without touching the running Gridzilla server")

    def test_grid_api_uses_shared_runner_and_grid_only_pressure_routes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / "agent-token"
            token_file.write_text("b" * 64, encoding="ascii")
            os.chmod(token_file, 0o600)
            handoff = {"runId": "api-grid", "sites": [], "client": {}}
            started_runner = threading.Event()

            def fake_run(_handoff, profile, _path, *, root, stop_event, transport, operation_id, on_progress):
                self.assertEqual(profile.name, "smoke")
                self.assertIsNotNone(transport)
                started_runner.set()
                stop_event.wait(3)
                run_dir = root / "grid-api-run"
                run_dir.mkdir()
                (run_dir / "run-result.json").write_text(json.dumps({"runId": run_dir.name,
                    "scenario": "grid-llmd-signals", "status": "STOPPED", "windows": []}), encoding="utf-8")
                return {"status": "STOPPED", "evidenceDirectory": str(run_dir)}

            api, stopped, thread, live_patch = self._serve(root, endpoints_dir=None, token_file=token_file,
                                                           handoff=handoff, patched_live_state=True)
            try:
                token = token_file.read_text(encoding="ascii")
                with patch("gridzilla.controls.doctor", return_value={"ok": True}), \
                     patch("gridzilla.controls.run_benchmark", side_effect=fake_run), \
                     patch.object(BenchmarkControls, "start_pressure", return_value={"accepted": True,
                                                                                     "operationId": "c" * 32}) as pressure, \
                     patch.object(BenchmarkControls, "restore_pressure", return_value={"accepted": True,
                                                                                        "operationId": "c" * 32}) as restore:
                    status, discovery = self._request(api, "/api/configurations")
                    self.assertEqual(status, 200)
                    self.assertIn("grid-llmd-signals", discovery["scenarios"])
                    self.assertTrue(discovery["gridAvailable"])
                    status, grid_preflight = self._request(api, "/api/preflight", method="POST",
                                                           body={"scenario": "grid-llmd-signals", "profile": "smoke"},
                                                           token=token)
                    self.assertEqual(status, 200)
                    self.assertEqual(grid_preflight["status"], "READY")
                    status, started = self._request(api, "/api/run", method="POST",
                                                    body={"scenario": "grid-llmd-signals", "profile": "smoke"},
                                                    token=token)
                    self.assertEqual(status, 202)
                    operation_id = started["operationId"]
                    self.assertTrue(started_runner.wait(2), "Grid API run must use the shared controller runner")
                    status, progress = self._request(api, f"/api/operations/{operation_id}")
                    self.assertEqual(status, 200)
                    self.assertEqual(progress["scenario"], "grid-llmd-signals")
                    status, stopped_run = self._request(api, "/api/stop", method="POST",
                                                        body={"operationId": operation_id}, token=token)
                    self.assertEqual(status, 202)
                    self.assertEqual(stopped_run["operationId"], operation_id)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        final_operation = self._request(api, f"/api/operations/{operation_id}")[1]
                        if final_operation["status"] != "RUNNING":
                            break
                        time.sleep(0.02)
                    self.assertEqual(final_operation["status"], "STOPPED")
                    status, pressure_started = self._request(api, "/api/pressure", method="POST",
                                                             body={"site": "west", "value": 2}, token=token)
                    self.assertEqual(status, 202)
                    self.assertEqual(pressure_started["operationId"], "c" * 32)
                    pressure.assert_called_once_with(2, "west")
                    status, pressure_restored = self._request(api, "/api/pressure/restore", method="POST",
                                                              body={"operationId": "c" * 32}, token=token)
                    self.assertEqual(status, 202)
                    restore.assert_called_once_with("c" * 32)
            finally:
                stopped.set()
                thread.join(timeout=3)
                live_patch.__exit__(None, None, None)
                self.assertFalse(thread.is_alive(), "test server should close")


if __name__ == "__main__":
    unittest.main()
