from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gridzilla.report_model import REPORT_FORMAT, normalize_report
from gridzilla.report import regenerate_report


class NormalizedReportTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        run = root / "synthetic-five-region-run"
        run.mkdir()
        sites = [f"region-{index}" for index in range(1, 6)]
        (run / "handoff.redacted.json").write_text(json.dumps({
            "schemaVersion": "gridzilla.test/v1",
            "runId": "synthetic-five-region-setup",
            "topology": "five-region-fixture",
            "sites": [{"name": site, "provider": {"pool": f"pool-{index}"}}
                      for index, site in enumerate(sites, 1)],
        }), encoding="utf-8")
        (run / "source-lock.json").write_text(json.dumps({
            "setupSource": {"repository": "example/setup", "revision": "a" * 40},
            "gridzillaSource": {"repository": "example/gridzilla", "sourceFingerprintSha256": "b" * 64},
            "runtimeImages": [{"site": site, "component": "gateway", "reference": f"example/gateway:{site}",
                               "imageId": f"sha256:{index:064x}", "sourceRevision": "c" * 40}
                              for index, site in enumerate(sites, 1)],
            "k6Version": "v1.8.2",
        }), encoding="utf-8")
        (run / "profile.json").write_text(json.dumps({"name": "steady", "version": 2,
            "rates": [25], "window": "30s", "warmup": "5s", "percentileMinimumSamples": 5,
            "maxOutputTokens": 16, "requestTimeout": "10s"}), encoding="utf-8")
        windows = [
            {"type": "measurement", "phase": "baseline", "ratePerSecond": 25, "repeat": 1,
             "elapsedSeconds": 10, "started": {"utc": "2026-11-10T10:00:00Z"},
             "finished": {"utc": "2026-11-10T10:00:10Z"}, "accounting": {
                 "offeredIterations": 252, "httpRequests": 250, "droppedIterations": 2,
                 "statusCounts": {"200": 248, "503": 2}, "timeouts": 1, "responseBytes": 50000,
                 "latencyMs": {"sampleCount": 4, "p50": {"value": 20}, "p95": {"value": 40}, "p99": {"value": 50}},
                 "routeCountsByDimension": {"site": {"region-1": 150, "region-4": 98},
                                             "pool": {"pool-1": 150, "pool-4": 98}},
                 "attributedRequests": 248, "attributionDenominator": 250,
                 "unattributedRequests": 2, "attributionCompleteness": 0.992}},
            {"type": "measurement", "phase": "direct-simulator-reference", "reference": True,
             "ratePerSecond": 25, "repeat": 1, "elapsedSeconds": 10,
             "started": {"utc": "2026-11-10T10:01:00Z"}, "finished": {"utc": "2026-11-10T10:01:10Z"},
             "accounting": {"httpRequests": 250, "statusCounts": {"200": 250},
                            "latencyMs": {"sampleCount": 250, "p50": {"value": 5}, "p95": {"value": 8},
                                          "p99": {"value": 9}, "referencePath": True}}},
            {"type": "measurement", "phase": "pressure", "ratePerSecond": 25, "repeat": 1,
             "elapsedSeconds": 10, "started": {"utc": "2026-11-10T10:02:00Z"},
             "finished": {"utc": "2026-11-10T10:02:10Z"}, "accounting": {
                 "httpRequests": 250, "statusCounts": {"200": 250}, "droppedIterations": 0,
                 "latencyMs": {"sampleCount": 250, "p50": {"value": 21}, "p95": {"value": 42}, "p99": {"value": 51}}}},
        ]
        result = {"runId": run.name, "setupRunId": "synthetic-five-region-setup",
            "type": "benchmark", "scenario": {"id": "mock-routing", "name": "Mock routing"}, "status": "PASS",
            "createdAtUtc": "2026-11-10T09:59:00Z", "finishedAtUtc": "2026-11-10T10:03:00Z",
            "restoration": {"verified": True, "pressureSite": "region-1", "restoredAt": {"utc": "2026-11-10T10:03:00Z"}},
            "windows": windows, "signalEvents": [{"boundary": "simulator-config-post-complete",
                "started": {"utc": "2026-11-10T10:01:20Z", "monotonicNs": 1},
                "time": {"utc": "2026-11-10T10:01:21Z", "monotonicNs": 2}, "durationMs": 1000,
                "postResults": [{"observedValue": 4}]}]}
        (run / "run-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        (run / "windows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in windows), encoding="utf-8")
        (run / "signal-boundaries.jsonl").write_text(json.dumps({"boundary": "grid-overlay-and-gateway",
            "observedAtUtc": "2026-11-10T10:01:30Z", "monotonicNs": 3, "selectionChanged": True,
            "baselineTop": {"site": "region-1", "rank": 0, "score": 1},
            "pressureTop": {"site": "region-4", "rank": 0, "score": 1},
            "sites": {"region-1": {"oldQueue": [0], "newQueue": [4], "oldRevision": "old",
                "newRevision": "new", "oldServingRevision": "old", "newServingRevision": "new",
                "oldScore": 0.2, "newScore": 0.1, "oldRank": 0, "newRank": 1,
                "newEppSampleTimestampUtc": "2026-11-10T10:01:29Z", "eppFreshnessBasis": "prometheus-sample-timestamp"}}}) + "\n", encoding="utf-8")
        (run / "probes.jsonl").write_text(json.dumps({"type": "correlated-probe", "label": "pressure-route",
            "requestId": "probe-safe-id", "status": 200, "ok": True, "durationMs": 3.2,
            "started": {"utc": "2026-11-10T10:01:31Z", "monotonicNs": 4},
            "finished": {"utc": "2026-11-10T10:01:31.003Z", "monotonicNs": 5},
            "attribution": {"site": "region-4", "pool": "pool-4", "backend": "backend-4"},
            "receipt": {"available": True, "found": True, "independent": True, "requestIdMatched": True}}) + "\n", encoding="utf-8")
        (run / "resource-samples.jsonl").write_text(json.dumps({"observedAtUtc": "2026-11-10T10:00:00Z",
            "phase": "baseline", "loadGeneratorSaturated": False,
            "host": {"cpuCount": 8, "memoryAvailableBytes": 1000000, "memoryTotalBytes": 2000000,
                     "diskFreeBytes": 4000000, "loadAverage1m5m15m": [1, 1, 1]},
            "clusters": [{"site": "region-1", "componentUsage": {"gateway": {"pods": [
                {"cpuCores": 0.4, "memoryBytes": 400000}]}}}]}) + "\n", encoding="utf-8")
        return run

    def test_five_site_report_is_normalized_without_fixed_site_assumptions(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = self._fixture(Path(temporary))
            normalized = normalize_report(run)
            self.assertEqual(normalized["formatVersion"], REPORT_FORMAT)
            self.assertEqual(normalized["run"]["scenario"]["id"], "mock-routing")
            self.assertEqual(normalized["run"]["topology"]["siteCount"], 5)
            self.assertEqual([site["name"] for site in normalized["run"]["topology"]["sites"]],
                             [f"region-{index}" for index in range(1, 6)])
            self.assertEqual([dimension["name"] for dimension in normalized["routing"]["dimensions"]], ["pool", "site"])
            phases = normalized["measurements"]["phases"]
            baseline = next(phase["windows"][0] for phase in phases if phase["name"] == "baseline")
            self.assertEqual(baseline["offeredRps"], 25)
            self.assertEqual(baseline["achievedRps"], 25)
            self.assertEqual(baseline["httpErrors"], 2)
            self.assertEqual(baseline["timeouts"], 1)
            self.assertEqual(baseline["droppedIterations"], 2)
            self.assertEqual(baseline["responseBytes"], 50000)
            self.assertFalse(baseline["latencyMs"]["eligible"])
            self.assertIsNone(baseline["latencyMs"]["p95"])
            reference = next(phase["windows"][0] for phase in phases if phase["name"] == "direct-simulator-reference")
            self.assertEqual(reference["trafficPath"], "direct-reference")
            self.assertEqual(reference["attribution"]["status"], "not-applicable")
            self.assertEqual(reference["httpErrors"], 0)
            self.assertIsNone(reference["offeredRequests"])
            self.assertFalse(normalized["resources"]["throughputEligibility"]["eligible"])
            self.assertEqual(normalized["resources"]["throughputEligibility"]["droppedIterationCount"], 2)
            self.assertIn("dropped 2 iterations", normalized["resources"]["throughputEligibility"]["reason"])
            self.assertEqual(normalized["resources"]["workloads"][0]["site"], "region-1")
            self.assertEqual(normalized["run"]["restorationStatus"], "VERIFIED")
            self.assertEqual(normalized["signals"]["kind"], "signal-observations")
            self.assertIn("synthetic-pressure", [event["kind"] for event in normalized["signals"]["events"]])
            probe = next(event for event in normalized["signals"]["events"] if event["kind"] == "correlated-probe")
            self.assertEqual(probe["attribution"]["site"], "region-4")
            (run / "resource-samples.jsonl").write_text(json.dumps({
                "observedAtUtc": "2026-11-10T10:00:00Z", "loadGeneratorSaturated": False,
                "host": {"cpuCount": 8, "memoryAvailableBytes": 1000000},
            }) + "\n", encoding="utf-8")
            missing_host = normalize_report(run, report_data={"windows": [],
                "throughputClaim": {"eligible": False, "reason": "required host data missing"}})
            self.assertIn("loadAverage", missing_host["resources"]["host"][0]["missing"])
            self.assertIn("memoryTotalBytes", missing_host["resources"]["host"][0]["missing"])
            self.assertFalse(missing_host["resources"]["throughputEligibility"]["eligible"])

    def test_legacy_report_migration_is_deterministic_and_writes_versioned_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = self._fixture(Path(temporary))
            first = regenerate_report(run)
            stored = json.loads((run / "normalized-report.json").read_text())
            second = normalize_report(run)
            self.assertEqual(stored["formatVersion"], REPORT_FORMAT)
            self.assertEqual(stored, second)
            self.assertTrue(first["generatedFromEvidenceOnly"])
            baseline = next(phase["windows"][0] for phase in stored["measurements"]["phases"]
                            if phase["name"] == "baseline")
            self.assertIsNone(baseline["requestedBodyBytes"])
            self.assertIsNone(baseline["verifiedBodyBytes"])
            markdown = (run / "report.md").read_text(encoding="utf-8")
            self.assertIn("unavailable / unavailable", markdown)

    def test_generic_template_window_identity_uses_verified_wire_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = self._fixture(Path(temporary))
            result_path = run / "run-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            row = result["windows"][0]
            row.update({"scenario": "generic-endpoint", "trafficPath": "endpoint",
                        "requestedBodyBytes": None, "verifiedBodyBytes": 16384,
                        "bodySizeSamples": 3})
            result.update({"scenario": "generic-endpoint", "endpointId": "mock-json",
                           "endpoint": {"id": "mock-json", "name": "Mock JSON endpoint",
                                        "endpoint": "http://127.0.0.1:8081/respond"},
                           "windows": [row]})
            result_path.write_text(json.dumps(result), encoding="utf-8")
            normalized = normalize_report(run)
            window = normalized["measurements"]["phases"][0]["windows"][0]
            self.assertIsNone(window["requestedBodyBytes"])
            self.assertEqual(window["verifiedBodyBytes"], 16384)
            self.assertIn(":16384:", window["id"])

    def test_generic_report_preserves_measured_attribution_quality(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = self._fixture(Path(temporary))
            result_path = run / "run-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            row = result["windows"][0]
            row.update({"scenario": "generic-endpoint", "trafficPath": "endpoint"})
            row["accounting"].update({"attributionStatus": "partial", "attributedRequests": 248,
                                      "attributionDenominator": 250, "unattributedRequests": 2,
                                      "attributionCompleteness": 0.992,
                                      "requiredAttributionDimensions": ["site", "backend"]})
            result.update({"scenario": "generic-endpoint", "endpointId": "mock-json", "windows": [row]})
            result_path.write_text(json.dumps(result), encoding="utf-8")
            normalized = normalize_report(run)
            window = normalized["measurements"]["phases"][0]["windows"][0]
            self.assertEqual(window["attribution"]["status"], "partial")
            self.assertEqual(window["attribution"]["attributedRequests"], 248)
            self.assertEqual(window["attribution"]["requestDenominator"], 250)

    def test_legacy_dimension_counts_restore_partial_generic_attribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = self._fixture(Path(temporary))
            result_path = run / "run-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            row = result["windows"][0]
            row.update({"scenario": "generic-endpoint", "trafficPath": "endpoint"})
            row["accounting"].update({
                "attributionStatus": "unavailable", "attributedRequests": None,
                "attributionDenominator": 422, "unattributedRequests": None,
                "attributionCompleteness": None, "requiredAttributionDimensions": ["site"],
                "httpRequests": 422, "routeCountsByDimension": {
                    "site": {"central": 198, "east": 201, "unknown": 23}}})
            result.update({"scenario": "generic-endpoint", "windows": [row]})
            result_path.write_text(json.dumps(result), encoding="utf-8")
            normalized = normalize_report(run)
            window = normalized["measurements"]["phases"][0]["windows"][0]
            self.assertEqual(window["attribution"]["status"], "partial")
            self.assertEqual(window["attribution"]["attributedRequests"], 399)
            self.assertEqual(window["attribution"]["requestDenominator"], 422)
            self.assertEqual(window["attribution"]["unattributedRequests"], 23)

    def test_window_execution_distinguishes_attempted_from_not_started(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = self._fixture(Path(temporary))
            result_path = run / "run-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["plan"] = {"windowCount": 8, "measurementWindowCount": 6, "warmupWindowCount": 2}
            result["windows"] = [
                {"type": "warmup", "phase": "baseline-warmup"},
                {"type": "failed-window", "phase": "baseline", "failure": "status 404"},
            ]
            result_path.write_text(json.dumps(result), encoding="utf-8")
            normalized = normalize_report(run)
            self.assertEqual(normalized["run"]["windowExecution"], {
                "plannedMeasurement": 6, "attemptedMeasurement": 1, "notAttemptedMeasurement": 5,
                "plannedWarmup": 2, "attemptedWarmup": 1, "notAttemptedWarmup": 1,
                "plannedTotal": 8, "attemptedTotal": 2, "notAttemptedTotal": 6,
            })
            attempted = [{"type": "warmup", "phase": "baseline-warmup"},
                         {"type": "failed-window", "phase": "baseline", "failure": "status 404"}]
            (run / "windows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in attempted),
                                                encoding="utf-8")
            regenerated = regenerate_report(run)
            markdown = (run / "report.md").read_text(encoding="utf-8")
            self.assertEqual(regenerated["windowExecution"]["attemptedMeasurement"], 1)
            self.assertIn("1/6 measurement windows attempted; 2/8 total windows attempted", markdown)
            self.assertIn("6 not started (5 measurement + 1 warmup)", markdown)


if __name__ == "__main__":
    unittest.main()
