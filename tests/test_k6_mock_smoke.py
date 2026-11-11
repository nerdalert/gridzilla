from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gridzilla.runner import arrival_parameters, verify_k6_version
from gridzilla.endpoints import load_endpoint
from gridzilla.models import load_profile
from gridzilla.runner import build_generic_k6_command


class MockGateway(BaseHTTPRequestHandler):
    requests_seen = 0

    def do_POST(self):  # noqa: N802 - HTTP server API
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        type(self).requests_seen += 1
        payload = b'{"choices":[{"message":{"content":"ok"}}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("x-request-id", self.headers.get("X-Request-Id", ""))
        self.send_header("x-inference-pod", "vcr-inference-west-testpod")
        self.send_header("x-ai-inference-provider", "vcr-backend")
        self.send_header("x-grid-combined-provider-gateway", "west")
        self.send_header("x-ai-demo-provider-gateway", "west")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


class MockGenericEndpoint(BaseHTTPRequestHandler):
    received: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802 - HTTP server API
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400)
            return
        with type(self).lock:
            type(self).received.append((len(raw), body))
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Region", "mock-east")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


class K6MockSmokeTests(unittest.TestCase):
    def test_fractional_arrival_rate_uses_integer_k6_rate_and_time_unit(self):
        self.assertEqual(arrival_parameters(0.2), (1, "5s"))
        self.assertEqual(arrival_parameters(1.5), (3, "2s"))
        self.assertEqual(arrival_parameters(100), (100, "1s"))

    def test_pinned_k6_gridzilla_script_against_local_mock_gateway(self):
        if not os.environ.get("K6_BIN"):
            self.skipTest("set K6_BIN to the checksum-verified pinned k6 binary to run this mock smoke")
        try:
            binary, actual = verify_k6_version("v1.8.2")
        except Exception as exc:
            self.skipTest(f"pinned k6 binary is unavailable: {exc}")
        self.assertEqual(actual, "v1.8.2")
        repo = Path(__file__).resolve().parents[1]
        MockGateway.requests_seen = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockGateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                summary = root / "summary.json"
                raw = root / "raw.jsonl"
                attribution = {
                    "providerHeader": "x-grid-combined-provider-gateway",
                    "providerGatewayHeader": "x-ai-demo-provider-gateway",
                    "siteHeader": "x-grid-combined-provider-gateway",
                    "backendHeader": "x-ai-inference-provider",
                    "backendPodHeader": "x-inference-pod",
                    "requestIdEchoHeader": "x-request-id",
                    "allowedSites": ["west", "central", "east"],
                    "allowedProviders": ["west", "central", "east"],
                    "allowedPools": ["pool-a", "pool-b", "pool-c"],
                    "allowedBackends": ["vcr-backend"],
                    "providerToSite": {"west": "west"}, "providerToPool": {"west": "pool-a"},
                    "providerToBackend": {"west": "vcr-backend"},
                    "backendPodPrefixBySite": {"west": "vcr-inference-west-"},
                    "requiredResponseHeaders": ["x-request-id", "x-inference-pod", "x-ai-inference-provider",
                                                "x-grid-combined-provider-gateway", "x-ai-demo-provider-gateway"],
                }
                env = os.environ.copy()
                env.update({
                    "GZ_URL": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                    "GZ_BODY_JSON": json.dumps({"model": "mock-model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 4}),
                    "GZ_CREDENTIAL_FILE": "", "GZ_AUTH_HEADER": "Authorization",
                    "GZ_REQUEST_ID_HEADER": "X-Request-Id", "GZ_REQUEST_ID_HEADERS": '["X-Request-Id"]',
                    "GZ_ATTRIBUTION_JSON": json.dumps(attribution),
                    "GZ_ALLOWED_SITES": '["west","central","east"]',
                    "GZ_ALLOWED_POOLS": '["pool-a","pool-b","pool-c"]',
                    "GZ_ALLOWED_BACKENDS": '["vcr-backend"]',
                    "GZ_ALLOWED_PROVIDERS": '["west","central","east"]',
                    "GZ_PROVIDER_TO_SITE": json.dumps(attribution["providerToSite"]),
                    "GZ_PROVIDER_TO_POOL": json.dumps(attribution["providerToPool"]),
                    "GZ_PROVIDER_TO_BACKEND": json.dumps(attribution["providerToBackend"]),
                    "GZ_REQUIRED_RESPONSE_HEADERS": json.dumps(attribution["requiredResponseHeaders"]),
                    "GZ_MAX_RESPONSE_BYTES": "65536", "GZ_RATE": "2", "GZ_TIME_UNIT": "1s", "GZ_DURATION": "2s",
                    "GZ_PREALLOCATED_VUS": "1", "GZ_MAX_VUS": "2", "GZ_REQUEST_TIMEOUT": "3s",
                    "GZ_PHASE": "offline-mock", "GZ_CLIENT_SITE": "west", "GZ_INGRESS_NAME": "local-mock",
                    "GZ_REFERENCE_MODE": "false", "GZ_SUMMARY_PATH": str(summary), "GZ_HOSTS_JSON": "{}",
                })
                script = repo / "k6" / "gridzilla.js"
                result = subprocess.run([binary, "run", "--no-color", "--summary-export", str(summary),
                                         "--out", f"json={raw}", str(script)],
                                        env=env, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertGreaterEqual(MockGateway.requests_seen, 1)
                exported = json.loads(summary.read_text())
                http_metric = next((value for key, value in exported["metrics"].items() if key.startswith("http_reqs")), {})
                route_metric = next((value for key, value in exported["metrics"].items() if key.startswith("gridzilla_attributed_routes")), {})
                self.assertGreaterEqual(http_metric.get("count", http_metric.get("values", {}).get("count", 0)), 1,
                                        result.stdout + result.stderr + json.dumps(exported["metrics"], sort_keys=True))
                self.assertGreaterEqual(route_metric.get("count", route_metric.get("values", {}).get("count", 0)), 1,
                                        result.stdout + result.stderr + json.dumps(exported["metrics"], sort_keys=True))
                points = [json.loads(line) for line in raw.read_text().splitlines() if line]
                self.assertTrue(any(point.get("metric") == "gridzilla_attributed_routes" for point in points))
                self.assertFalse(any("GRIDZILLA_ATTRIBUTION_FAILURE" in line for line in result.stderr.splitlines()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_pinned_k6_sends_exact_generic_json_body_sizes(self):
        if not os.environ.get("K6_BIN"):
            self.skipTest("set K6_BIN to the checksum-verified pinned k6 binary to run this mock smoke")
        try:
            binary, actual = verify_k6_version("v1.8.2")
        except Exception as exc:
            self.skipTest(f"pinned k6 binary is unavailable: {exc}")
        self.assertEqual(actual, "v1.8.2")
        repo = Path(__file__).resolve().parents[1]
        MockGenericEndpoint.received = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockGenericEndpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config_path = root / "endpoint.json"
                config_path.write_text(json.dumps({
                    "id": "mock-generic", "name": "Local JSON endpoint",
                    "url": f"http://127.0.0.1:{server.server_port}/complete",
                    "requestTemplate": {"model": "model-preserved", "messages": [
                        {"role": "user", "content": "small prompt"}], "temperature": 0},
                    "padJsonPointer": "/messages/0/content", "expectedStatuses": [200],
                    "responseHeaderDimensions": {"region": {"header": "X-Region", "allowedValues": ["mock-east"]}},
                }), encoding="utf-8")
                endpoint = load_endpoint(config_path)
                profile = load_profile(repo / "profiles" / "body-sizes.json")
                for size in (16 * 1024, 64 * 1024, 256 * 1024):
                    summary = root / f"summary-{size}.json"
                    argv, env, expected_bytes = build_generic_k6_command(
                        endpoint, profile, evidence_dir=root, phase="mock-generic", rate=1,
                        duration="1s", index=str(size), request_body_size_bytes=size)
                    self.assertEqual(expected_bytes, size)
                    env["GZ_SUMMARY_PATH"] = str(summary)
                    env["GZ_URL"] = endpoint.url
                    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30, check=False)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(summary.is_file(), result.stdout + result.stderr)
                    raw = root / f"{size}-raw.jsonl"
                    points = [json.loads(line) for line in raw.read_text().splitlines() if line]
                    point_sizes = [point.get("data", {}).get("value") for point in points
                                   if point.get("type") == "Point"
                                   and point.get("metric") == "gridzilla_request_body_bytes"]
                    point_sizes = [value for value in point_sizes if value is not None]
                    self.assertTrue(point_sizes, f"k6 must record serialized request-body bytes; points={points!r}")
                    self.assertEqual(set(point_sizes), {size})
                self.assertEqual({size for size, _body in MockGenericEndpoint.received},
                                 {16 * 1024, 64 * 1024, 256 * 1024})
                for size, body in MockGenericEndpoint.received:
                    self.assertEqual(body["model"], "model-preserved")
                    self.assertEqual(body["temperature"], 0)
                    self.assertEqual(body["messages"][0]["role"], "user")
                    self.assertTrue(body["messages"][0]["content"].startswith("small prompt"))
                    self.assertEqual(size, len(json.dumps(body, separators=(",", ":")).encode("utf-8")))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
