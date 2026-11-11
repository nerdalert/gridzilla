from __future__ import annotations

import json
import tempfile
import time
import unittest
import datetime as dt
from pathlib import Path

from gridzilla.metrics import render_metrics


class PrometheusMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handoff = {
            "runId": "setup-run",
            "sites": [
                {"name": name, "provider": {"pool": pool}}
                for name, pool in (("west", "pool-a"), ("central", "pool-b"), ("east", "pool-c"))
            ],
        }
        self.live = {
            "status": "LIVE",
            "observedAtUtc": "2026-11-10T10:00:00Z",
            "sites": [
                {
                    "name": "west",
                    "status": "LIVE",
                    "provider": {"available": True, "queueCapacity": 4},
                    "metrics": {
                        "status": "LIVE",
                        "queue": [{"value": 1.5, "labels": {"request_id": "must-not-leak"}}],
                        "readyEndpoints": [{"value": 2}],
                        "sampleAgeSeconds": 2.0,
                    },
                    "simulators": [{"ready": True, "adminApiReachable": True, "fakeMetricValue": 4}],
                    "overlay": {
                        "revision": "sha256:private-revision-marker",
                        "candidates": [{"site": "west", "score": 0.75, "rank": 0}],
                    },
                    "gateway": {
                        "acceptedRevision": "sha256:private-revision-marker",
                        "servingRevision": "sha256:private-revision-marker",
                    },
                    "resources": {"sampledAtUtc": "2026-11-10T10:00:00Z", "cpu": "0.250 cores",
                                  "memory": "128.0 MiB", "restarts": 0, "readyPods": 3},
                }
            ],
        }

    def test_live_metrics_are_bounded_and_do_not_export_revision_or_request_ids(self) -> None:
        output = render_metrics(self.live, self.handoff, Path("/no-evidence"), now=1794304805)
        self.assertIn('gridzilla_epp_queue_size{pool="pool-a",site="west"} 1.5', output)
        self.assertIn('gridzilla_queue_capacity_scoring_scale{pool="pool-a",site="west"} 4', output)
        self.assertIn('gridzilla_simulator_synthetic_waiting_requests{pool="pool-a",site="west"} 4', output)
        self.assertIn('gridzilla_gateway_overlay_serving_revision_match{site="west"} 1', output)
        self.assertIn('gridzilla_live_cluster_memory_bytes{site="west"} 134217728', output)
        self.assertNotIn("must-not-leak", output)
        self.assertNotIn("private-revision-marker", output)
        self.assertNotIn("sha256", output)
        self.assertNotIn("request_id", output)

    def test_missing_samples_are_absent_not_zero(self) -> None:
        live = {"status": "UNAVAILABLE", "sites": [], "observedAtUtc": "2026-11-10T10:00:00Z"}
        output = render_metrics(live, self.handoff, Path("/no-evidence"), now=1794304805)
        self.assertNotIn("gridzilla_epp_queue_size{", output)
        self.assertNotIn("gridzilla_epp_sample_age_seconds{", output)
        self.assertIn('gridzilla_live_observation_status{status="UNAVAILABLE"} 1', output)

    def test_stalled_live_observer_does_not_export_old_snapshot_as_live(self) -> None:
        now = 1794304805
        stale = json.loads(json.dumps(self.live))
        stale["observedAtUtc"] = "2026-11-10T09:55:00Z"
        output = render_metrics(stale, self.handoff, Path("/no-evidence"), now=now,
                                max_live_age_seconds=15)
        self.assertIn('gridzilla_live_observation_status{status="STALE"} 1', output)
        self.assertNotIn('gridzilla_live_observation_status{status="LIVE"}', output)
        self.assertNotIn("gridzilla_epp_queue_size{", output)
        self.assertNotIn("gridzilla_candidate_rank{", output)

    def test_percentiles_require_profile_sample_count_and_only_active_evidence_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "setup-run-20261110T100000Z"
            directory.mkdir()
            current_utc = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            self.live["observedAtUtc"] = current_utc
            state = {"setupRunId": "setup-run", "status": "RUNNING", "phase": "baseline",
                     "updatedAtUtc": current_utc}
            (directory / "live-state.json").write_text(json.dumps(state), encoding="utf-8")
            (directory / "profile.json").write_text(json.dumps({"percentileMinimumSamples": 10}), encoding="utf-8")
            account = {"httpRequests": 12, "droppedIterations": 2,
                       "statusCounts": {"200": 11, "503": 1},
                       "routeCountsBySite": {"west": 7, "central": 4},
                       "latencyMs": {"sampleCount": 9, "p50": {"value": 20},
                                     "p95": {"value": 45}, "p99": {"value": 50}}}
            row = {"type": "measurement", "phase": "baseline", "ratePerSecond": 25,
                   "elapsedSeconds": 10, "accounting": account, "finishedAtUtc": "2026-11-10T10:00:00Z"}
            (directory / "windows.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            active = render_metrics(self.live, self.handoff, root, now=time.time())
            labels = 'body_size="unavailable",phase="baseline",scenario="grid-llmd-signals",traffic_path="gateway"'
            self.assertIn(f'gridzilla_benchmark_offered_requests_per_second{{{labels}}} 25', active)
            self.assertIn(f'gridzilla_benchmark_achieved_requests_per_second{{{labels}}} 1.2', active)
            self.assertIn(f'gridzilla_benchmark_http_errors{{{labels}}} 1', active)
            self.assertNotIn(f'gridzilla_benchmark_route_attributed_requests{{{labels},site="east"}}', active)
            self.assertIn('gridzilla_benchmark_route_attributed_requests_by_dimension{body_size="unavailable",dimension="site",phase="baseline",scenario="grid-llmd-signals",traffic_path="gateway",value="west"} 7', active)
            self.assertIn('gridzilla_benchmark_route_attributed_requests_by_dimension{body_size="unavailable",dimension="pool",phase="baseline",scenario="grid-llmd-signals",traffic_path="gateway",value="pool-a"} 7', active)
            self.assertNotIn('gridzilla_benchmark_route_attributed_requests_by_dimension{body_size="unavailable",dimension="site",phase="baseline",scenario="grid-llmd-signals",traffic_path="gateway",value="east"}', active)
            self.assertNotIn(f'gridzilla_benchmark_latency_milliseconds{{{labels},quantile="p95"}}', active)
            self.assertNotIn("setup-run-20261110T100000Z", active)

            state["status"] = "PASS"
            (directory / "live-state.json").write_text(json.dumps(state), encoding="utf-8")
            saved = render_metrics(self.live, self.handoff, root, now=time.time())
            self.assertNotIn("gridzilla_benchmark_offered_requests_per_second", saved)
            self.assertNotIn("gridzilla_benchmark_route_attributed_requests", saved)

    def test_latency_percentiles_are_exported_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "active-run"
            directory.mkdir()
            now = time.time()
            updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            self.live["observedAtUtc"] = updated
            (directory / "live-state.json").write_text(json.dumps(
                {"setupRunId": "setup-run", "status": "RUNNING", "phase": "baseline", "updatedAtUtc": updated}),
                encoding="utf-8")
            (directory / "profile.json").write_text(json.dumps({"percentileMinimumSamples": 10}), encoding="utf-8")
            row = {"type": "measurement", "phase": "baseline", "ratePerSecond": 25,
                   "elapsedSeconds": 10, "accounting": {"httpRequests": 10, "statusCounts": {"200": 10},
                   "latencyMs": {"sampleCount": 10, "p50": {"value": 20}, "p95": {"value": 45},
                                 "p99": {"value": 50}}}}
            (directory / "windows.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            output = render_metrics(self.live, self.handoff, root, now=now)
            self.assertIn('gridzilla_benchmark_latency_milliseconds{body_size="unavailable",phase="baseline",quantile="p95",scenario="grid-llmd-signals",traffic_path="gateway"} 45', output)
            self.assertNotIn("gridzilla_benchmark_route_attributed_requests{", output)

    def test_stale_resource_samples_are_not_exported_as_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "active-run"
            directory.mkdir()
            now = time.time()
            updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            self.live["observedAtUtc"] = updated
            (directory / "live-state.json").write_text(json.dumps(
                {"setupRunId": "setup-run", "status": "RUNNING", "phase": "baseline", "updatedAtUtc": updated}),
                encoding="utf-8")
            (directory / "profile.json").write_text(json.dumps({"percentileMinimumSamples": 10}), encoding="utf-8")
            old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
            sample = {"observedAtUtc": old, "host": {"memoryAvailableBytes": 1024},
                      "clusters": [{"site": "west", "componentUsage": {"gateway": {"pods": [
                          {"cpuCores": 0.5, "memoryBytes": 4096}]}}}]}
            (directory / "resource-samples.jsonl").write_text(json.dumps(sample) + "\n", encoding="utf-8")

            output = render_metrics(self.live, self.handoff, root, now=now)
            self.assertNotIn("gridzilla_host_available_memory_bytes", output)
            self.assertNotIn("gridzilla_cluster_cpu_cores", output)
            self.assertNotIn("gridzilla_cluster_memory_bytes", output)


if __name__ == "__main__":
    unittest.main()
