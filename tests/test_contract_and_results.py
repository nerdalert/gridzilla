from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gridzilla import controller
from gridzilla.models import ValidationError, load_profile, redacted_handoff, validate_handoff
from gridzilla.report import regenerate_report
from gridzilla.runtime import (PortForward, RuntimeErrorChecked, gateway_revisions, port_forward_log_confirms,
                               port_forward_log_port, provider_snapshot, runtime_image_snapshot,
                               simulator_target_snapshot)
from gridzilla.runner import resolve_k6_binary
from gridzilla.stats import percentile, summarize_raw
from gridzilla.web import LiveState, _active_benchmark, _latest_signal_timeline, _safe_run_file, _saved_run, _saved_runs


def fixture_handoff(root: Path) -> dict:
    credentials = root / "client.token"
    credentials.write_text("test-only-secret\n")
    sites = []
    for index, name in enumerate(("west", "central", "east")):
        kubeconfig = root / f"{name}.kubeconfig"
        kubeconfig.write_text("apiVersion: v1\nclusters: []\n")
        token = root / f"{name}.metrics-token"
        token.write_text("test-metrics-token\n")
        site = {
            "name": name,
            "clusterName": f"grid-signals-gridzilla-test-{name}",
            "ownerRunId": "gridzilla-test",
            "kubeconfig": str(kubeconfig),
            "context": name,
            "namespace": "grid-system",
            "readinessTargets": [{"resource": "deployment/epp", "namespace": "llm-d"}],
            "provider": {"resource": "InferenceProvider/provider", "pool": f"pool-{('a','b','c')[index]}",
                         "candidateKey": "key", "candidateValue": name,
                         "routingCluster": f"cluster-{name}", "queueCapacity": 4, "pressureTarget": index == 0},
            "epp": {"metricsUrl": f"http://127.0.0.1:{19000 + index}/metrics",
                    "credentialFile": str(token), "queueMetricNames": ["llm_d_epp_average_queue_size"],
                    "readyMetricNames": ["llm_d_epp_ready_endpoints"], "maxSampleAgeSeconds": 30,
                    "portForward": {"resource": "svc/epp", "namespace": "llm-d",
                                    "localPort": 19000 + index, "remotePort": 9090}},
            "simulator": {"adminUrl": f"http://127.0.0.1:{19100 + index}",
                          "pressureJsonPointer": "/fake-metrics/waiting-requests",
                          "pressureValue": 9, "baselineValue": 0,
                          "targets": [{"namespace": "sim", "podName": f"sim-{name}",
                                       "podUID": f"uid-{name}", "configGeneration": 1,
                                       "adminUrl": f"http://127.0.0.1:{19100 + index}",
                                       "portForward": {"resource": f"pod/sim-{name}", "namespace": "sim",
                                                       "localPort": 19100 + index, "remotePort": 8080}}]},
            "overlay": {"namespace": "grid-system", "resource": "ConfigMap/routing-overlay",
                        "dataKey": "routing-config.json", "revisionAnnotation": "grid/overlay-revision"},
            "gateway": {"namespace": "grid-system", "resource": "deployment/consumer",
                        "container": "praxis", "acceptedField": "accepted_revision", "servingField": "serving_revision"},
        }
        sites.append(site)
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}
    image_lock = root / "image-lock.json"
    image_lock.write_text(json.dumps({"schemaVersion": "1", "images": [
        {"role": "grid-controller", "reference": f"quay.io/example/grid@sha256:{'c' * 64}",
         "imageId": f"sha256:{'c' * 64}", "sourceRevision": "d" * 40}
        for _site in sites
    ]}))
    (root / "run-manifest.json").write_text(json.dumps({
        "schemaVersion": "1", "runId": "gridzilla-test",
        "clusterPrefix": "grid-signals-gridzilla-test",
        "clusterNames": [site["clusterName"] for site in sites],
        "evidenceDir": str(root.resolve()), "lifecycle": "ready",
    }))
    return {
        "schemaVersion": "gridzilla.dev/v1alpha1", "runId": "gridzilla-test", "createdAt": "2026-11-09T00:00:00Z",
        "pressureTargetPool": "pool-a",
        "source": {"repository": "praxis-proxy/grid", "branch": "main", "commit": "a" * 40,
                   "worktreeStatus": "", "sourceFingerprintSha256": "b" * 64},
        "runtimeImages": [{"site": site["name"], "component": "grid-controller", "namespace": "grid-system",
                           "selector": "app=grid-controller", "container": "manager",
                           "reference": f"quay.io/example/grid@sha256:{'c' * 64}",
                           "imageId": f"containerd://sha256:{'c' * 64}", "sourceRevision": "d" * 40}
                          for site in sites],
        "imageLock": str(image_lock),
        "client": {"site": "west", "gatewayUrl": "http://127.0.0.1:18080/v1/chat/completions",
                   "requestPath": "/v1/chat/completions", "credentialFile": str(credentials),
                   "authorizationScheme": "Bearer", "model": "test-model",
                   "requestIdHeader": "X-Gridzilla-Request-Id",
                   "attributionHeaders": ["x-grid-combined-provider-gateway", "x-ai-demo-provider-gateway",
                                          "x-ai-inference-provider", "x-request-id", "x-inference-pod"],
                   "requestBody": body},
        "request": {"model": "test-model", "body": body},
        "backendReference": {"site": "west", "namespace": "grid-system", "service": "simulator",
                             "port": 8080, "localPort": 19200, "path": "/v1/chat/completions"},
        "sites": sites,
    }


def minimal_gate(*, revision: str, top: str, target_score: float = 0.8) -> dict:
    sites = []
    for name, score, rank, queue in (("west", target_score, 0 if top == "west" else 1, 0),
                                     ("central", 0.7, 0 if top == "central" else 1, 0),
                                     ("east", 0.6, 0 if top == "east" else 2, 0)):
        sites.append({"name": name,
                      "metrics": {"queue": [{"value": queue}]},
                      "overlay": {"revision": revision,
                                  "candidates": [{"key": name, "score": score, "rank": rank}],
                                  "topCandidate": {"key": top, "site": top, "score": score, "rank": 0}},
                      "gateway": {"servingRevision": revision}})
    return {"ok": True, "sites": sites}


class HandoffTests(unittest.TestCase):
    def test_current_setup_handoff_normalizes_and_requires_runtime_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = fixture_handoff(root)
            raw["source"]["branch"] = "feat/signals-combined-site-kind\n"
            raw["source"]["commit"] = "a" * 40 + "\n"
            raw["client"].pop("site")
            raw["client"]["context"] = raw["sites"][0]["context"]
            target = raw["sites"][0]["simulator"]["targets"][0]
            target["podUid"] = target.pop("podUID")
            target["portForward"].pop("namespace")
            raw["sites"][0]["epp"]["portForward"].pop("namespace")
            handoff_path = root / "handoff.json"
            normalized = validate_handoff(raw, handoff_path)
            self.assertEqual(normalized["source"]["revision"], "a" * 40)
            self.assertEqual(normalized["source"]["branch"], "feat/signals-combined-site-kind")
            self.assertEqual(normalized["client"]["site"], "west")
            self.assertEqual(normalized["sites"][0]["simulator"]["targets"][0]["podUID"], "uid-west")
            self.assertEqual(normalized["sites"][0]["simulator"]["targets"][0]["portForward"]["namespace"], "sim")
            self.assertEqual(normalized["client"]["url"], "http://127.0.0.1:18080")
            self.assertEqual(normalized["client"]["path"], "/v1/chat/completions")
            self.assertIn("x-ai-demo-provider-gateway", normalized["client"]["attribution"]["requiredResponseHeaders"])
            self.assertIn("x-request-id", normalized["client"]["attribution"]["requiredResponseHeaders"])
            self.assertEqual(normalized["sites"][0]["provider"]["pool"], "pool-a")
            self.assertTrue(normalized["sites"][0]["provider"]["pressureTarget"])
            self.assertEqual(normalized["sites"][0]["epp"]["portForward"]["namespace"], "grid-system")
            self.assertEqual(normalized["runtimeImages"][0]["sourceRevision"], "d" * 40)
            self.assertIsNone(normalized["client"]["attribution"]["poolHeader"])
            self.assertEqual(normalized["client"]["attribution"]["providerToPool"]["west"], "pool-a")
            self.assertEqual(normalized["client"]["attribution"]["backendHeader"], "x-ai-inference-provider")
            self.assertEqual(normalized["client"]["receipt"]["type"], "response_headers")
            self.assertTrue(normalized["client"]["receipt"]["available"])

    def test_setup_exact_pod_runtime_identity_uses_a_name_lookup(self):
        digest = "c" * 64
        pod = {
            "kind": "Pod",
            "metadata": {"name": "consumer-gateway-abc123"},
            "status": {"containerStatuses": [{
                "name": "praxis", "ready": True, "image": "ghcr.io/praxis-proxy/ai:0.7.0",
                "imageID": f"containerd://sha256:{digest}", "restartCount": 0,
            }]},
        }
        with patch("gridzilla.runtime.kubectl", return_value=json.dumps(pod)) as mocked:
            observation = runtime_image_snapshot(
                {"_cluster": {"kubeconfig": "/tmp/kubeconfig", "context": "kind-test"}},
                {"component": "consumer-gateway", "namespace": "grid-system", "selector": "pod=consumer-gateway-abc123",
                 "container": "praxis", "imageID": f"sha256:{digest}"},
            )
        self.assertEqual(mocked.call_args.args[1], [
            "-n", "grid-system", "get", "pod", "consumer-gateway-abc123", "-o", "json",
        ])
        self.assertTrue(observation["ready"])
        self.assertTrue(observation["pods"][0]["digestMatches"])

    def test_gateway_revision_parser_handles_ansi_colored_structured_logs(self):
        accepted = "a" * 64
        serving = "b" * 64
        previous = "c" * 64
        logs = (f"\x1b[2m INFO\x1b[0m \x1b[3maccepted_revision\x1b[0m=\x1b[0m\"{accepted}\" "
                f"\x1b[3mserving_revision\x1b[0m=\x1b[0m\"{serving}\" "
                f"\x1b[3mprevious_serving_revision\x1b[0m=\x1b[0m\"{previous}\"\n").encode()
        site = {"name": "west", "gateway": {"namespace": "grid-system", "resource": "deployment/consumer",
                                              "container": "praxis", "acceptedField": "accepted_revision",
                                              "servingField": "serving_revision"}}
        with patch("gridzilla.runtime.kubectl", return_value=logs):
            result = gateway_revisions(site)
        self.assertEqual(result["acceptedRevision"], accepted)
        self.assertEqual(result["servingRevision"], serving)

    def test_gateway_revision_snapshot_reads_bounded_log_tail_for_persistent_runs(self):
        revision = "d" * 64
        logs = f"accepted_revision=\"{revision}\" serving_revision=\"{revision}\"\n".encode()
        site = {"name": "west", "gateway": {"namespace": "grid-system", "resource": "deployment/consumer",
                                              "container": "praxis", "acceptedField": "accepted_revision",
                                              "servingField": "serving_revision", "logsSince": "10m"}}
        with patch("gridzilla.runtime.kubectl", return_value=logs) as mocked:
            result = gateway_revisions(site)
        args = mocked.call_args.args[1]
        self.assertIn("--tail=1000", args, "persistent readiness must inspect startup revision after idle")
        self.assertNotIn("--since", args, "a short time window hides startup revision on idle runs")
        self.assertEqual(result["acceptedRevision"], revision)
        self.assertEqual(result["servingRevision"], revision)

    def test_smoke_reuses_benchmark_transport_without_closing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = fixture_handoff(root)
            handoff_path = root / "handoff.json"
            handoff = validate_handoff(raw, handoff_path)
            class ExistingTransport:
                closed = False

                def close(self):
                    self.closed = True

            transport = ExistingTransport()
            with patch("gridzilla.controller._startup_transports", side_effect=AssertionError("must reuse existing forwards")), \
                    patch("gridzilla.controller.gate_snapshot", return_value={"ok": True, "sites": []}), \
                    patch("gridzilla.controller.send_probe", return_value={"ok": True, "status": 200}):
                result = controller.smoke(handoff, evidence_dir=root / "smoke", count=1, transport=transport)
            self.assertTrue(result["ok"])
            self.assertFalse(transport.closed, "the benchmark still owns this transport")

    def test_k6_resolution_prefers_repo_pinned_binary_to_unpinned_system_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / ".tools" / "k6-v1.8.2" / "k6"
            local.parent.mkdir(parents=True)
            local.write_text("test stub")
            with patch.dict("os.environ", {"K6_BIN": ""}), patch("gridzilla.runner.shutil.which", return_value="/usr/bin/k6"):
                self.assertEqual(resolve_k6_binary("v1.8.2", repo_root=root), str(local))

    def test_echoed_simulator_receipt_requires_request_id_and_site_pod_match(self):
        handoff = {"client": {"receipt": {
            "available": True, "type": "response_headers",
            "requestIdHeader": "x-request-id", "backendPodHeader": "x-inference-pod",
            "backendPodPrefixBySite": {"west": "vcr-inference-west-", "central": "vcr-inference-central-",
                                       "east": "vcr-inference-east-"},
        }}}
        matched = controller._check_receipt(
            handoff, None, "request-123",
            {"x-request-id": "request-123", "x-inference-pod": "vcr-inference-west-abc"}, "west",
        )
        self.assertTrue(matched["found"], matched)
        self.assertTrue(matched["requestIdMatched"], matched)
        mismatch = controller._check_receipt(
            handoff, None, "request-123",
            {"x-request-id": "other-id", "x-inference-pod": "vcr-inference-east-abc"}, "west",
        )
        self.assertFalse(mismatch["found"], mismatch)
        self.assertFalse(mismatch["requestIdMatched"], mismatch)

    def test_handoff_rejects_missing_pressure_target_and_reused_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = fixture_handoff(root)
            raw["sites"][0]["provider"]["pressureTarget"] = False
            with self.assertRaisesRegex(ValidationError, "exactly one provider"):
                validate_handoff(raw, root / "handoff.json")
            raw = fixture_handoff(root)
            raw["sites"][1]["simulator"]["targets"][0]["portForward"]["localPort"] = 19100
            with self.assertRaisesRegex(ValidationError, "reuse local port 19100"):
                validate_handoff(raw, root / "handoff.json")

    def test_handoff_rejects_setup_manifest_for_another_or_unready_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = fixture_handoff(root)
            manifest_path = root / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["lifecycle"] = "failed-retained"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValidationError, "lifecycle must be 'ready'"):
                validate_handoff(raw, root / "handoff.json")

    def test_handoff_rejects_query_credentials_in_endpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = fixture_handoff(root)
            raw["client"]["gatewayUrl"] = "https://gateway.example/v1/chat/completions?key=caller-secret"
            with self.assertRaisesRegex(ValidationError, "must not contain a query"):
                validate_handoff(raw, root / "handoff.json")

    def test_port_forward_ownership_requires_expected_kubectl_log_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "forward.log"
            log.write_text("error: unable to listen on port 19090\n")
            self.assertFalse(port_forward_log_confirms(log, 19090, 9090))
            log.write_text("Forwarding from 127.0.0.1:19090 -> 9090\n")
            self.assertTrue(port_forward_log_confirms(log, 19090, 9090))
            self.assertEqual(port_forward_log_port(log, 9090), 19090)

    def test_port_forward_uses_kubectl_assigned_loopback_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = {"_cluster": {"kubeconfig": str(root / "config"), "context": "kind-test"}}
            config = {"namespace": "grid-system", "resource": "svc/epp", "localPort": 19090, "remotePort": 9090}
            with patch("gridzilla.runtime.subprocess.Popen") as process, \
                    patch("gridzilla.runtime.port_forward_log_port", return_value=49152), \
                    patch("gridzilla.runtime.socket.create_connection") as connection:
                process.return_value.poll.return_value = None
                connection.return_value.__enter__.return_value = None
                forward = PortForward(site, config, root / "forward.log")
                forward.start()
            argv = process.call_args.args[0]
            self.assertEqual(argv[-1], ":9090")
            self.assertEqual(config["localPort"], 49152)

    def test_simulator_readiness_requires_owned_pod_deployment_and_fake_metric_api(self):
        site = {"name": "west", "namespace": "grid-system",
                "simulator": {"pressureJsonPointer": "/fake-metrics/waiting-requests",
                              "adminUrl": "http://127.0.0.1:19180/admin/config"}}
        target = {"namespace": "grid-system", "podName": "sim-west", "podUID": "uid-west",
                  "configGeneration": 7, "adminUrl": "http://127.0.0.1:19180/admin/config"}
        pod = {"metadata": {"uid": "uid-west", "resourceVersion": "12"},
               "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
        deployment = {"metadata": {"generation": 7}, "spec": {"replicas": 1},
                      "status": {"observedGeneration": 7, "readyReplicas": 1}}
        calls = iter([json.dumps(pod).encode(), json.dumps(deployment).encode()])

        class Transport:
            def request(self, *_args, **_kwargs):
                return {"status": 200, "body": b'{"fake-metrics":{"waiting-requests":0}}'}

        with patch("gridzilla.runtime.kubectl", side_effect=lambda *_args, **_kwargs: next(calls)):
            snapshot = simulator_target_snapshot(site, target, Transport())
        self.assertTrue(snapshot["ready"])
        self.assertTrue(snapshot["adminApiReachable"])
        self.assertEqual(snapshot["fakeMetricValue"], 0)

        bad_target = {**target, "podUID": "replacement-uid"}
        calls = iter([json.dumps(pod).encode()])
        with patch("gridzilla.runtime.kubectl", side_effect=lambda *_args, **_kwargs: next(calls)):
            with self.assertRaisesRegex(RuntimeErrorChecked, "identity/readiness changed"):
                simulator_target_snapshot(site, bad_target, Transport())

    def test_provider_capacity_must_match_live_metrics_configuration(self):
        provider = {"namespace": "grid-system", "resource": "InferenceProvider/provider", "queueCapacity": 4}
        site = {"provider": provider, "namespace": "grid-system"}
        obj = {"metadata": {"generation": 3},
               "spec": {"metricsConfig": {"queueCapacity": 4}},
               "status": {"observedGeneration": 3, "conditions": []}}
        with patch("gridzilla.runtime.kubectl", return_value=json.dumps(obj).encode()):
            self.assertTrue(provider_snapshot(site)["queueCapacityMatches"])
        obj["spec"]["metricsConfig"]["queueCapacity"] = 8
        with patch("gridzilla.runtime.kubectl", return_value=json.dumps(obj).encode()):
            self.assertFalse(provider_snapshot(site)["queueCapacityMatches"])

    def test_redaction_removes_secret_fields_but_retains_paths(self):
        value = {"client": {"credentialFile": "/tmp/test-token", "token": "never-copy", "requestBody": {"model": "mock"}}}
        result = redacted_handoff(value)
        self.assertNotIn("never-copy", json.dumps(result))
        self.assertEqual(result["client"]["credentialFile"], "/tmp/test-token")


class ResultsTests(unittest.TestCase):
    def test_percentiles_require_sample_minimum_and_ignore_nonfinite_data(self):
        result = percentile([1, 2, float("nan"), "bad", 3], 50, minimum_samples=4)
        self.assertIsNone(result["value"])
        self.assertEqual(result["sampleCount"], 3)
        enough = percentile([1, 2, 3, 4], 50, minimum_samples=4)
        self.assertEqual(enough["value"], 2.5)

    def test_attribution_failures_remain_in_request_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "raw.jsonl"
            rows = [
                {"type": "Point", "metric": "http_reqs", "data": {"value": 1, "tags": {"status": "200"}}},
                {"type": "Point", "metric": "http_reqs", "data": {"value": 1, "tags": {"status": "502"}}},
                {"type": "Point", "metric": "gridzilla_attributed_routes", "data": {"value": 1, "tags": {"site": "west"}}},
                {"type": "Point", "metric": "gridzilla_attribution_failures", "data": {"value": 1, "tags": {"reason": "missing_header"}}},
            ]
            raw.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = summarize_raw(raw, {"metrics": {"http_reqs": {"values": {"count": 2}},
                                                        "iterations": {"values": {"count": 2}},
                                                        "dropped_iterations": {"values": {"count": 0}}}},
                                   minimum_samples=20)
            self.assertEqual(result["attributionDenominator"], 2)
            self.assertEqual(result["attributedRequests"], 1)
            self.assertEqual(result["unattributedRequests"], 1)
            self.assertEqual(result["attributionFailures"], 1)
            self.assertEqual(result["statusCounts"], {"200": 1, "502": 1})

    def test_direct_reference_is_not_counted_as_missing_gateway_attribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = summarize_raw(Path(temporary) / "missing.jsonl", {"metrics": {"http_reqs": {"values": {"count": 7}}}},
                                  minimum_samples=20, reference=True)
            self.assertEqual(result["attributionDenominator"], 0)
            self.assertEqual(result["unattributedRequests"], 0)

    def test_current_k6_summary_shape_reports_offered_count_and_http_failure_rate(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "raw.jsonl"
            points = [
                {"type": "Point", "metric": "http_reqs", "data": {"value": 1, "tags": {"status": "200"}}},
                {"type": "Point", "metric": "gridzilla_gateway_latency_ms", "data": {"value": 12.0, "tags": {}}},
            ]
            raw.write_text("".join(json.dumps(point) + "\n" for point in points))
            summary = {"metrics": {
                "iterations": {"count": 3, "rate": 0.2},
                "dropped_iterations": {"count": 1, "rate": 0.07},
                "http_reqs": {"count": 1, "rate": 0.07},
                "http_req_failed": {"passes": 0, "fails": 1, "value": 0.0},
            }}
            result = summarize_raw(raw, summary, minimum_samples=20)
            self.assertEqual(result["offeredIterations"], 4)
            self.assertEqual(result["completedIterations"], 3)
            self.assertEqual(result["droppedIterations"], 1)
            self.assertEqual(result["httpRequests"], 1)
            self.assertEqual(result["httpFailureRate"], 0.0)
            self.assertEqual(result["latencyMs"]["sampleCount"], 1)

    def test_profile_bounds_and_estimated_evidence(self):
        repo = Path(__file__).resolve().parents[1]
        for name in ("smoke", "baseline", "stepped"):
            profile = load_profile(repo / "profiles" / f"{name}.json")
            self.assertEqual(profile.pressure_target_pool, "pool-a")
            self.assertLessEqual(profile.max_rate, 50)

    def test_profile_wall_time_cap_includes_each_phase_warmup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "long.json"
            raw = json.loads((Path(__file__).resolve().parents[1] / "profiles" / "smoke.json").read_text())
            raw["warmup"] = "100s"
            raw["window"] = "500s"
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValidationError, "total run duration"):
                load_profile(path)


class RevisionAndRecoveryTests(unittest.TestCase):
    def test_pressure_requires_distributed_revision_and_selection_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            handoff = validate_handoff(fixture_handoff(Path(temporary)), Path(temporary) / "handoff.json")
            profile = load_profile(Path(__file__).resolve().parents[1] / "profiles" / "baseline.json")
            baseline = minimal_gate(revision="baseline", top="west")
            pressured = minimal_gate(revision="pressured", top="central", target_score=0.3)
            for item in pressured["sites"]:
                item["metrics"]["queue"][0]["value"] = 9 if item["name"] == "west" else 0
            ok, facts = controller._check_signal_gate(handoff, baseline, pressured, "west")
            self.assertTrue(ok, facts)
            still_baseline = minimal_gate(revision="pressured", top="west", target_score=0.3)
            ok, facts = controller._check_signal_gate(handoff, baseline, still_baseline, "west")
            self.assertFalse(ok)

    def test_benchmark_restores_after_failure_during_pressure_convergence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = validate_handoff(fixture_handoff(root), root / "handoff.json")
            profile_path = Path(__file__).resolve().parents[1] / "profiles" / "smoke.json"
            profile = load_profile(profile_path)
            baseline = minimal_gate(revision="baseline", top="west")
            snapshot = {"pressureSite": "west", "runId": handoff["runId"], "sites": {}}
            calls = {"restored": 0}

            class DummyTransport:
                def close(self):
                    return None

            def restore(*_args, **_kwargs):
                calls["restored"] += 1
                return {"verified": True, "pressureSite": "west"}

            with patch.object(controller, "_startup_transports", return_value=DummyTransport()), \
                 patch.object(controller, "doctor", return_value={"ok": True, "problems": []}), \
                 patch.object(controller, "_ready_gate", return_value={"ok": True}), \
                 patch.object(controller, "smoke", return_value={"ok": True}), \
                 patch.object(controller, "run_traffic_phase", return_value=[]), \
                 patch.object(controller, "gate_snapshot", return_value=baseline), \
                 patch.object(controller, "snapshot_simulators", return_value=snapshot), \
                 patch.object(controller, "mutate_simulators", return_value=[{"post": {"status": 200}}]), \
                 patch.object(controller, "wait_for_signal_transition", side_effect=controller.BenchmarkFailure("injected convergence failure")), \
                 patch.object(controller, "restore_simulators", side_effect=restore), \
                 patch.object(controller, "wait_for_recovery", return_value=(baseline, {"restored": True})), \
                 patch.object(controller, "send_probe", return_value={"ok": True}), \
                 patch.object(controller, "_capture_diagnostics", return_value=None):
                with self.assertRaisesRegex(controller.BenchmarkFailure, "injected convergence failure"):
                    controller.run_benchmark(handoff, profile, profile_path, root=root / "evidence")
            self.assertEqual(calls["restored"], 1)
            run_dirs = list((root / "evidence").glob("gridzilla-test-*"))
            self.assertEqual(len(run_dirs), 1)
            result = json.loads((run_dirs[0] / "run-result.json").read_text())
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(result["restoration"]["verified"])
            report = json.loads((run_dirs[0] / "report-data.json").read_text())
            self.assertTrue(report["generatedFromEvidenceOnly"])

    def test_normal_path_persists_restore_and_recovery_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = validate_handoff(fixture_handoff(root), root / "handoff.json")
            profile_path = Path(__file__).resolve().parents[1] / "profiles" / "smoke.json"
            profile = load_profile(profile_path)
            baseline = minimal_gate(revision="baseline", top="west")
            baseline.update({"ok": True, "problems": [], "observedAtUtc": "2026-11-09T12:00:00Z"})
            pressured = minimal_gate(revision="pressured", top="central")
            snapshot = {"pressureSite": "west", "runId": handoff["runId"], "sites": {}}
            restore_result = {"pressureSite": "west", "pods": [{"matchesOriginal": True}], "configRestored": True}
            recovery = {"selectionRestored": True, "servingRevision": "baseline"}

            class DummyTransport:
                def close(self):
                    return None

            with patch.object(controller, "_startup_transports", return_value=DummyTransport()), \
                 patch.object(controller, "doctor", return_value={"ok": True, "problems": []}), \
                 patch.object(controller, "_ready_gate", return_value={"ok": True}), \
                 patch.object(controller, "smoke", return_value={"ok": True}), \
                 patch.object(controller, "run_traffic_phase", return_value=[]), \
                 patch.object(controller, "gate_snapshot", return_value=baseline), \
                 patch.object(controller, "snapshot_simulators", return_value=snapshot), \
                 patch.object(controller, "mutate_simulators", return_value=[{"status": 200}]), \
                 patch.object(controller, "wait_for_signal_transition", return_value=(pressured, {"selectionChanged": True})), \
                 patch.object(controller, "restore_simulators", return_value=restore_result), \
                 patch.object(controller, "wait_for_recovery", return_value=(baseline, recovery)), \
                 patch.object(controller, "send_probe", return_value={"ok": True}), \
                 patch.object(controller, "_capture_diagnostics", return_value=None):
                result = controller.run_benchmark(handoff, profile, profile_path, root=root / "evidence")

            run_dir = Path(result["evidenceDirectory"])
            run_result = json.loads((run_dir / "run-result.json").read_text())
            restore_proof = json.loads((run_dir / "restoration-proof.json").read_text())
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(run_result["restoration"]["configRestored"])
            self.assertTrue(run_result["restoration"]["verified"])
            self.assertEqual(run_result["restoration"]["recovery"], recovery)
            self.assertEqual(restore_proof, run_result["restoration"])

    def test_recovery_nonconvergence_is_recorded_and_fails_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = validate_handoff(fixture_handoff(root), root / "handoff.json")
            profile_path = Path(__file__).resolve().parents[1] / "profiles" / "smoke.json"
            profile = load_profile(profile_path)
            baseline = minimal_gate(revision="baseline", top="west")
            baseline.update({"ok": True, "problems": [], "observedAtUtc": "2026-11-09T12:00:00Z"})
            pressured = minimal_gate(revision="pressured", top="central")
            snapshot = {"pressureSite": "west", "runId": handoff["runId"], "sites": {}}
            restore_result = {"pressureSite": "west", "pods": [{"matchesOriginal": True}], "configRestored": True}

            class DummyTransport:
                def close(self):
                    return None

            with patch.object(controller, "_startup_transports", return_value=DummyTransport()), \
                 patch.object(controller, "doctor", return_value={"ok": True, "problems": []}), \
                 patch.object(controller, "_ready_gate", return_value={"ok": True}), \
                 patch.object(controller, "smoke", return_value={"ok": True}), \
                 patch.object(controller, "run_traffic_phase", return_value=[]), \
                 patch.object(controller, "gate_snapshot", return_value=baseline), \
                 patch.object(controller, "snapshot_simulators", return_value=snapshot), \
                 patch.object(controller, "mutate_simulators", return_value=[{"status": 200}]), \
                 patch.object(controller, "wait_for_signal_transition", return_value=(pressured, {"selectionChanged": True})), \
                 patch.object(controller, "restore_simulators", return_value=restore_result), \
                 patch.object(controller, "wait_for_recovery", side_effect=controller.BenchmarkFailure("route recovery timed out")), \
                 patch.object(controller, "send_probe", return_value={"ok": True}), \
                 patch.object(controller, "_capture_diagnostics", return_value=None):
                with self.assertRaisesRegex(controller.BenchmarkFailure, "route recovery timed out"):
                    controller.run_benchmark(handoff, profile, profile_path, root=root / "evidence")

            run_dirs = list((root / "evidence").glob("gridzilla-test-*"))
            self.assertEqual(len(run_dirs), 1)
            run_result = json.loads((run_dirs[0] / "run-result.json").read_text())
            restore_proof = json.loads((run_dirs[0] / "restoration-proof.json").read_text())
            self.assertEqual(run_result["status"], "FAIL")
            self.assertIn("route recovery timed out", run_result["failure"])
            self.assertFalse(run_result["restoration"]["verified"])
            self.assertIn("route recovery timed out", run_result["restoration"]["recoveryError"])
            self.assertEqual(restore_proof, run_result["restoration"])


class ReportTests(unittest.TestCase):
    def test_signal_timeline_reads_the_configured_evidence_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "custom-evidence"
            run = root / "grid-signals-setup-20261109T120000Z-abc12345"
            run.mkdir(parents=True)
            (run / "signal-boundaries.jsonl").write_text(json.dumps({
                "boundary": "selection-shift", "observedAtUtc": "2026-11-09T12:01:00Z", "durationMs": 1500,
            }) + "\n")
            timeline = _latest_signal_timeline(root, "grid-signals-setup")
            self.assertEqual(timeline[0]["boundary"], "selection-shift")
            self.assertEqual(timeline[0]["durationMs"], 1500)

    def test_live_state_uses_configured_runs_directory_for_signal_timeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "custom-evidence"
            run = root / "gridzilla-test-20261109T120000Z-abc12345"
            run.mkdir(parents=True)
            (run / "signal-boundaries.jsonl").write_text(json.dumps({
                "boundary": "custom-evidence-pressure", "observedAtUtc": "2026-11-09T12:01:00Z", "durationMs": 1500,
            }) + "\n")
            handoff = validate_handoff(fixture_handoff(root), root / "handoff.json")
            gate_sites = []
            for site in handoff["sites"]:
                revision = f"rev-{site['name']}"
                gate_sites.append({"name": site["name"], "ok": True, "problems": [],
                                   "provider": {"resource": site["provider"]["resource"], "queueCapacity": 4,
                                                "conditions": [{"type": "Available", "status": "True"}]},
                                   "metrics": {"queue": [{"value": 0}], "readyEndpoints": [{"value": 1}],
                                               "sampleAgeSeconds": 1, "status": "LIVE"},
                                   "overlay": {"revision": revision, "candidates": [],
                                               "topCandidate": {"key": site["provider"]["candidateValue"], "site": site["name"]}},
                                   "gateway": {"acceptedRevision": revision, "servingRevision": revision},
                                   "simulators": [{"ready": True, "adminApiReachable": True}]})
            gate = {"ok": True, "observedAtUtc": "2026-11-09T12:01:00Z", "sites": gate_sites, "problems": []}

            class DummyTransport:
                pass

            live = LiveState.__new__(LiveState)
            live.handoff = handoff
            live.transport = DummyTransport()
            live.evidence_root = root
            with patch("gridzilla.web.gate_snapshot", return_value=gate), \
                 patch.object(LiveState, "_resource_summary_cached", return_value={}):
                state = live._collect()
            self.assertEqual(state["phase"], "custom-evidence-pressure")
            self.assertEqual(state["timeline"][0]["durationMs"], 1500)

    def test_saved_evidence_download_is_confined_and_name_filtered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run-1"
            run.mkdir()
            (run / "report.md").write_text("safe")
            (run / "provider-token.json").write_text("secret")
            outside = root / "outside.json"
            outside.write_text("private")
            (run / "external.json").symlink_to(outside)
            self.assertEqual(_safe_run_file(root, "run-1", "report.md"), run / "report.md")
            self.assertIsNone(_safe_run_file(root, "run-1", "provider-token.json"))
            self.assertIsNone(_safe_run_file(root, "run-1", "external.json"))
            self.assertIsNone(_safe_run_file(root, "../outside.json", "outside.json"))
            self.assertIsNone(_safe_run_file(root, "run-1", "bad\r\nContent-Type: text/html.md"))

    def test_report_regenerates_only_from_saved_evidence_and_lists_per_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run-result.json").write_text(json.dumps({"status": "PASS", "setupRunId": "run-1", "windows": []}))
            (root / "source-lock.json").write_text(json.dumps({"setupSource": {"revision": "a" * 40}, "runtimeImages": []}))
            (root / "profile.json").write_text(json.dumps({"name": "baseline"}))
            (root / "windows.jsonl").write_text(json.dumps({"type": "measurement", "phase": "baseline", "ratePerSecond": 1,
                                                             "repeat": 1, "elapsedSeconds": 1,
                                                             "accounting": {"httpRequests": 2, "routeCountsBySite": {"west": 1},
                                                                            "attributedRequests": 1, "unattributedRequests": 1,
                                                                            "latencyMs": {"p95": {"value": 4}}}}) + "\n")
            (root / "resource-samples.jsonl").write_text(json.dumps({"loadGeneratorSaturated": False}) + "\n")
            value = regenerate_report(root)
            self.assertTrue(value["generatedFromEvidenceOnly"])
            self.assertEqual(value["routeDistributionByWindow"][0]["unattributedRequests"], 1)
            self.assertTrue((root / "charts" / "throughput.svg").is_file())
            self.assertIn("unattributed", (root / "report.md").read_text().lower())

    def test_report_recomputes_accounting_from_current_k6_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run-result.json").write_text(json.dumps({"status": "PASS", "setupRunId": "run-1"}))
            (root / "source-lock.json").write_text(json.dumps({"runtimeImages": []}))
            (root / "profile.json").write_text(json.dumps({"name": "smoke", "percentileMinimumSamples": 100}))
            raw_name, summary_name = "window-raw.jsonl", "window-summary.json"
            points = [
                {"type": "Point", "metric": "http_reqs", "data": {"value": 1, "tags": {"status": "200"}}},
                {"type": "Point", "metric": "gridzilla_attributed_routes", "data": {"value": 1, "tags": {"site": "west"}}},
                {"type": "Point", "metric": "gridzilla_gateway_latency_ms", "data": {"value": 8.0, "tags": {}}},
            ]
            (root / raw_name).write_text("".join(json.dumps(point) + "\n" for point in points))
            (root / summary_name).write_text(json.dumps({"metrics": {
                "iterations": {"count": 3, "rate": 0.2},
                "dropped_iterations": {"count": 1, "rate": 0.07},
                "http_reqs": {"count": 1, "rate": 0.07},
                "http_req_failed": {"passes": 0, "fails": 1, "value": 0.0},
            }}))
            row = {"type": "measurement", "phase": "baseline", "ratePerSecond": 0.2,
                   "repeat": 1, "duration": "15s", "elapsedSeconds": 15,
                   "rawPath": raw_name, "summaryPath": summary_name,
                   "accounting": {"offeredIterations": 0, "httpRequests": 0}}
            (root / "windows.jsonl").write_text(json.dumps(row) + "\n")
            value = regenerate_report(root)
            account = value["windows"][0]["accounting"]
            self.assertEqual(account["offeredIterations"], 4)
            self.assertEqual(account["completedIterations"], 3)
            self.assertEqual(account["droppedIterations"], 1)
            self.assertEqual(account["httpFailureRate"], 0.0)
            report = (root / "report.md").read_text()
            self.assertIn("| baseline | 0.2 | 15s | 4.0 | 1 | 1 | 0.00", report)

    def test_active_benchmark_progress_is_visible_without_exposing_failure_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "gridzilla-run-1"
            run.mkdir()
            state = {"runId": run.name, "setupRunId": "grid-signals-setup", "status": "RUNNING",
                     "phase": "pressure-measurement", "createdAtUtc": "2026-11-09T12:00:00Z",
                     "updatedAtUtc": "2026-11-09T12:01:00Z", "failure": "private-token-must-not-escape",
                     "trafficProgress": {"action": "measurement-start", "index": "008-pressure-r1",
                                         "phase": "pressure", "ratePerSecond": 0.5, "duration": "15s",
                                         "completedWindows": 7, "expectedWindows": 16,
                                         "unexpected": "private-token-must-not-escape"}}
            (run / "live-state.json").write_text(json.dumps(state))
            (run / "windows.jsonl").write_text(json.dumps({"type": "measurement", "phase": "baseline"}) + "\n")
            active = _active_benchmark(root, "grid-signals-setup")
            self.assertEqual(active["phase"], "pressure-measurement")
            self.assertEqual(active["trafficProgress"]["completedWindows"], 7)
            self.assertNotIn("private-token-must-not-escape", json.dumps(active))
            self.assertEqual(_saved_runs(root)[0]["status"], "RUNNING")
            saved = _saved_run(root, run.name)
            self.assertEqual(len(saved["windows"]), 1)
            self.assertNotIn("private-token-must-not-escape", json.dumps(saved))


if __name__ == "__main__":
    unittest.main()
