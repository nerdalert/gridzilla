"""Localhost dashboard API and static UI server."""

from __future__ import annotations

import json
import mimetypes
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .controls import BenchmarkControls, ControlFailure
from .controller import _cluster_resources, _startup_transports
from .models import ValidationError, redacted_handoff
from .runtime import EndpointTransport, gate_snapshot, utc_now


class LiveState:
    def __init__(self, handoff: dict[str, Any] | None, refresh_seconds: float, evidence_root: Path):
        self.handoff = handoff
        self.evidence_root = evidence_root.resolve()
        self.refresh_seconds = max(1.0, min(float(refresh_seconds), 30.0))
        self.lock = threading.Lock()
        self.value: dict[str, Any] = self._empty("No live handoff was supplied")
        self.closed = threading.Event()
        self.transport: EndpointTransport | None = None
        self.thread: threading.Thread | None = None
        self._resource_sample_interval = max(15.0, self.refresh_seconds * 5)
        self._resource_samples: dict[str, tuple[float, dict[str, Any]]] = {}
        if handoff:
            try:
                self.transport = _startup_transports(handoff, Path("/tmp/gridzilla-ui"))
                self.thread = threading.Thread(target=self._poll_loop, name="gridzilla-live-state", daemon=True)
                self.thread.start()
            except Exception as exc:
                self.value = self._empty(f"Live attachments could not start: {exc}")

    @staticmethod
    def _empty(reason: str) -> dict[str, Any]:
        return {"status": "UNAVAILABLE", "phase": "READ ONLY", "observedAtUtc": utc_now(),
                "source": {}, "sites": [], "localOverlay": {}, "crossSite": {"status": "UNAVAILABLE", "sites": []},
                "gates": {}, "timeline": [], "reason": reason}

    def close(self) -> None:
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=4)
        if self.transport:
            self.transport.close()

    def read(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.value))

    def _poll_loop(self) -> None:
        assert self.handoff is not None and self.transport is not None
        while not self.closed.is_set():
            try:
                value = self._collect()
            except Exception as exc:
                value = self._empty(f"Live observation failed: {exc}")
            with self.lock:
                # _collect runs outside the lock so a slow cluster API cannot block the UI.
                self.value = value
            self.closed.wait(self.refresh_seconds)

    def _collect(self) -> dict[str, Any]:
        assert self.handoff is not None and self.transport is not None
        max_age = min(float(site["epp"]["maxSampleAgeSeconds"]) for site in self.handoff["sites"])
        gate = gate_snapshot(self.handoff, self.transport, max_age_seconds=max_age)
        observed_sites = []
        cross_sites = []
        for site in gate["sites"]:
            provider_conditions = site.get("provider", {}).get("conditions", [])
            available = next((item for item in provider_conditions
                              if item.get("type") in {"Available", "Ready"}), None)
            metric = site.get("metrics", {})
            age = metric.get("sampleAgeSeconds")
            status = "LIVE" if site.get("ok") else "STALE" if any("stale" in str(problem).lower() or "age" in str(problem).lower() or "revision" in str(problem).lower()
                                                                      for problem in site.get("problems", [])) else "UNAVAILABLE"
            resource = next((item for item in self.handoff["sites"] if item["name"] == site["name"]), {})
            observed_sites.append({
                "name": site["name"], "status": status,
                "provider": {"resource": site.get("provider", {}).get("resource"),
                             "available": bool(available and available.get("status") == "True"),
                             "status": "READY" if available and available.get("status") == "True" else "UNAVAILABLE",
                             "queueCapacity": site.get("provider", {}).get("queueCapacity"),
                             "queueCapacityMatches": site.get("provider", {}).get("queueCapacityMatches")},
                "metrics": {"queue": site.get("metrics", {}).get("queue", []),
                            "readyEndpoints": site.get("metrics", {}).get("readyEndpoints", []),
                            "sampleAgeSeconds": age,
                            "freshnessBasis": site.get("metrics", {}).get("freshnessBasis"),
                            "sampleTimestampAvailable": site.get("metrics", {}).get("sampleTimestampAvailable"),
                            "status": "LIVE" if site.get("ok") else "STALE" if status == "STALE" else "UNAVAILABLE"},
                "overlay": site.get("overlay", {}), "gateway": site.get("gateway", {}),
                "simulators": site.get("simulators", []),
                "resources": self._resource_summary_cached(resource),
                "problems": site.get("problems", []),
            })
            cross_sites.append({"name": site["name"], "overlayRevision": site.get("overlay", {}).get("revision"),
                                "servingRevision": site.get("gateway", {}).get("servingRevision"), "status": status})
        client_site = self.handoff["client"]["site"]
        local = next((x for x in observed_sites if x["name"] == client_site), None)
        timeline = _latest_signal_timeline(self.evidence_root, self.handoff["runId"])
        all_fresh = all(item["status"] == "LIVE" for item in observed_sites)
        return {"schemaVersion": 1, "status": "LIVE" if gate["ok"] else "STALE" if any(
                    item["status"] == "STALE" for item in observed_sites) else "UNAVAILABLE",
                "phase": _phase_from_saved_timeline(timeline), "observedAtUtc": gate["observedAtUtc"],
                "source": self.handoff["source"], "sites": observed_sites,
                "localOverlay": {"site": client_site, "revision": (local or {}).get("overlay", {}).get("revision"),
                                 "candidates": (local or {}).get("overlay", {}).get("candidates", [])},
                "crossSite": {"status": "LIVE" if all_fresh else "STALE", "sites": cross_sites},
                "gates": {"simulators": "READY" if all(item.get("simulators") and all(target.get("ready") and target.get("adminApiReachable") for target in item["simulators"]) for item in observed_sites) else "UNAVAILABLE",
                          "eppMetrics": "LIVE" if all(item.get("metrics", {}).get("status") == "LIVE" for item in observed_sites) else "STALE",
                          "providers": "READY" if all(item.get("provider", {}).get("available") for item in observed_sites) else "UNAVAILABLE",
                          "overlays": "LIVE" if all(item.get("overlay", {}).get("revision") for item in observed_sites) else "UNAVAILABLE",
                          "gateways": "READY" if all(item.get("gateway", {}).get("servingRevision") == item.get("overlay", {}).get("revision")
                                                       for item in gate["sites"]) else "STALE"},
                "timeline": timeline, "benchmark": _active_benchmark(self.evidence_root, self.handoff["runId"]),
                "problems": gate.get("problems", [])}

    def _resource_summary_cached(self, site: dict[str, Any]) -> dict[str, Any]:
        name = str(site.get("name", "unknown"))
        now = time.monotonic()
        cached = self._resource_samples.get(name)
        if cached and now - cached[0] < self._resource_sample_interval:
            return cached[1]
        value = _resource_summary(site)
        value["sampledAtUtc"] = utc_now()
        self._resource_samples[name] = (now, value)
        return value


def _resource_summary(site: dict[str, Any]) -> dict[str, Any]:
    try:
        result = _cluster_resources(site)
        restart_count = 0
        ready_count = 0
        for items in result.get("podRestarts", {}).values():
            if isinstance(items, list):
                restart_count += sum(int(item.get("restarts", 0)) for item in items)
                ready_count += sum(bool(item.get("ready")) for item in items)
        top = str(result.get("podUsageRaw") or "").splitlines()
        usage_rows = [row for component in result.get("componentUsage", {}).values() if isinstance(component, dict)
                      for row in component.get("pods", [])]
        cpu = sum(float(row["cpuCores"]) for row in usage_rows if row.get("cpuCores") is not None)
        memory = sum(int(row["memoryBytes"]) for row in usage_rows if row.get("memoryBytes") is not None)
        return {"restarts": restart_count, "readyPods": ready_count,
                "cpu": f"{cpu:.3f} cores" if usage_rows and any(row.get("cpuCores") is not None for row in usage_rows) else None,
                "memory": f"{memory / 1024**2:.1f} MiB" if usage_rows and any(row.get("memoryBytes") is not None for row in usage_rows) else None,
                "imageIdentities": {component: {"expectedImageID": identity.get("expectedImageID"),
                                                  "pods": [{"pod": pod.get("pod"), "imageID": pod.get("imageID"),
                                                            "digestMatches": pod.get("digestMatches"), "ready": pod.get("ready")}
                                                           for pod in identity.get("pods", [])]}
                                     for component, identity in result.get("runtimeImageIdentity", {}).items()},
                "nodePressure": result.get("nodePressure")}
    except Exception:
        return {"restarts": None, "readyPods": None, "cpu": None, "memory": None}


def _latest_signal_timeline(root: Path, setup_run_id: str) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    candidates = sorted(root.glob(f"{setup_run_id}-*/signal-boundaries.jsonl"),
                        key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        return []
    rows = []
    try:
        for line in candidates[0].read_text(encoding="utf-8").splitlines()[-50:]:
            item = json.loads(line)
            rows.append({"boundary": item.get("boundary", "unknown"),
                         "observedAtUtc": item.get("observedAtUtc") or item.get("time", {}).get("utc"),
                         "durationMs": item.get("durationMs")})
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def _phase_from_saved_timeline(timeline: list[dict[str, Any]]) -> str:
    return timeline[-1]["boundary"] if timeline else "IDLE · NO SIGNAL TRANSITION"


def _saved_runs(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    items = []
    for directory in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name, reverse=True):
        result_path = directory / "run-result.json"
        report_path = directory / "report-data.json"
        live_path = directory / "live-state.json"
        if not result_path.exists() and not report_path.exists() and not live_path.exists():
            continue
        result = _read_json(report_path if report_path.exists() else result_path if result_path.exists() else live_path, {})
        items.append({"id": directory.name, "startedAtUtc": result.get("createdAtUtc"),
                      "status": result.get("status", "HISTORICAL")})
    return items


def _saved_run(root: Path, run_id: str) -> dict[str, Any] | None:
    if not run_id or Path(run_id).name != run_id:
        return None
    directory = (root / run_id).resolve()
    if root.resolve() not in directory.parents:
        return None
    report = _read_json(directory / "report-data.json", None)
    if report is not None:
        report["evidenceFiles"] = _safe_evidence_files(directory)
        return report
    result = _read_json(directory / "run-result.json", None)
    if result is not None:
        return {**result, "evidenceFiles": _safe_evidence_files(directory)}
    live = _read_json(directory / "live-state.json", None)
    if live is None:
        return None
    windows = []
    try:
        with (directory / "windows.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    windows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        windows = []
    safe_live = _safe_live_state(live)
    return {**safe_live, "windows": windows, "evidenceFiles": _safe_evidence_files(directory)}


def _safe_run_file(root: Path, run_id: str, filename: str) -> Path | None:
    if not run_id or Path(run_id).name != run_id or not filename or Path(filename).name != filename:
        return None
    if not filename.isascii() or not all(character.isalnum() or character in "._-" for character in filename):
        return None
    if Path(filename).suffix not in {".json", ".jsonl", ".csv", ".md", ".svg", ".log"}:
        return None
    if "credential" in filename.lower() or "token" in filename.lower():
        return None
    root = root.resolve()
    directory = (root / run_id).resolve()
    if root not in directory.parents:
        return None
    target = (directory / filename).resolve()
    if target.parent != directory or not target.is_file():
        return None
    return target


def _active_benchmark(root: Path, setup_run_id: str) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    candidates = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        state = _read_json(directory / "live-state.json", None)
        if isinstance(state, dict) and state.get("setupRunId") == setup_run_id:
            candidates.append((str(state.get("updatedAtUtc", "")), directory.name, state))
    if not candidates:
        return None
    _updated, _name, state = max(candidates)
    safe = _safe_live_state(state)
    return {"runId": state.get("runId"), "status": state.get("status"),
            "phase": state.get("phase"), "updatedAtUtc": state.get("updatedAtUtc"),
            "trafficProgress": safe.get("trafficProgress")}


def _safe_live_state(state: dict[str, Any]) -> dict[str, Any]:
    safe = {key: state.get(key) for key in
            ("runId", "setupRunId", "phase", "status", "createdAtUtc", "updatedAtUtc")}
    traffic = state.get("trafficProgress")
    safe_traffic = None
    if isinstance(traffic, dict):
        safe_traffic = {key: traffic.get(key) for key in
                        ("action", "index", "phase", "ratePerSecond", "duration", "repeat", "reference",
                         "completedWindows", "expectedWindows")}
    safe["trafficProgress"] = safe_traffic
    return safe


def _safe_evidence_files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.is_file() and
                  path.suffix in {".json", ".jsonl", ".csv", ".md", ".svg", ".log"} and
                  "credential" not in path.name.lower() and "token" not in path.name.lower())


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _local_control_origin(host_header: str, origin_header: str | None, port: int) -> bool:
    """Reject cross-origin browser requests to the local control API."""
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host_header.lower() not in allowed_hosts:
        return False
    if not origin_header:
        return False
    try:
        parsed = urlsplit(origin_header)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.netloc.lower() in allowed_hosts and not parsed.path and not parsed.query and not parsed.fragment


def serve(*, handoff: dict[str, Any] | None, runs_dir: Path, host: str, port: int,
          refresh_seconds: float, dist: Path | None = None,
          allow_controls: bool = False, profiles_dir: Path | None = None) -> None:
    if host != "127.0.0.1":
        raise ValueError("Gridzilla UI must bind only to 127.0.0.1")
    assets = (dist or Path(__file__).resolve().parents[2] / "web" / "dist").resolve()
    if not assets.is_dir():
        raise FileNotFoundError(f"built Gridzilla UI not found at {assets}; run npm run build")
    evidence_root = runs_dir.resolve()
    live = LiveState(handoff, refresh_seconds, evidence_root)
    try:
        controls = BenchmarkControls(
            enabled=allow_controls, handoff=handoff, evidence_root=evidence_root,
            profiles_dir=profiles_dir or Path(__file__).resolve().parents[2] / "profiles",
            transport=live.transport,
        )
    except Exception:
        live.close()
        raise

    class Handler(BaseHTTPRequestHandler):
        server_version = "Gridzilla/0.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            # Avoid logging query parameters, request bodies, or credentials.
            return

        def _json(self, value: Any, status: int = 200) -> None:
            data = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            route = urlsplit(self.path).path
            if not controls.enabled:
                self._json({"error": "Gridzilla UI is read-only; restart with --allow-controls to enable benchmark actions"}, 405)
                return
            if not _local_control_origin(self.headers.get("Host", ""), self.headers.get("Origin"), port):
                self._json({"error": "dashboard controls accept same-origin localhost requests only"}, 403)
                return
            if self.headers.get("Transfer-Encoding"):
                self._json({"error": "chunked control requests are not accepted"}, 411)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json({"error": "invalid Content-Length"}, 400)
                return
            if content_length < 0 or content_length > 1024:
                self._json({"error": "control request body exceeds 1 KiB"}, 413)
                return
            try:
                body = json.loads(self.rfile.read(content_length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json({"error": "control request must be JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._json({"error": "control request must be a JSON object"}, 400)
                return
            try:
                if route == "/api/run":
                    result = controls.start(str(body.get("profile", "")))
                elif route == "/api/stop":
                    result = controls.stop()
                elif route == "/api/pressure":
                    result = controls.start_pressure(body.get("value"))
                elif route == "/api/pressure/restore":
                    result = controls.restore_pressure()
                else:
                    self._json({"error": "control endpoint not found"}, 404)
                    return
            except ControlFailure as exc:
                self._json({"error": str(exc)}, exc.status)
                return
            self._json(result, 202)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            route = urlsplit(self.path).path
            if route == "/api/state":
                value = live.read()
                value["controls"] = controls.status()
                self._json(value)
                return
            if route == "/api/runs":
                self._json(_saved_runs(evidence_root))
                return
            if route.startswith("/api/runs/") and "/files/" in route:
                run_id, filename = route.removeprefix("/api/runs/").split("/files/", 1)
                target = _safe_run_file(evidence_root, unquote(run_id), unquote(filename))
                if target is None:
                    self._json({"error": "saved evidence file not found"}, 404)
                    return
                size = target.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with target.open("rb") as source:
                    while chunk := source.read(64 * 1024):
                        self.wfile.write(chunk)
                return
            if route.startswith("/api/runs/"):
                value = _saved_run(evidence_root, unquote(route.removeprefix("/api/runs/")))
                self._json(value if value is not None else {"error": "saved run not found"}, 200 if value is not None else 404)
                return
            relative = "index.html" if route == "/" else route.lstrip("/")
            target = (assets / relative).resolve()
            if assets not in target.parents and target != assets:
                self._json({"error": "invalid asset path"}, 400)
                return
            if not target.is_file():
                self._json({"error": "asset not found"}, 404)
                return
            payload = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except Exception:
        controls.close()
        live.close()
        raise
    server.daemon_threads = True
    print(f"Gridzilla dashboard: http://{host}:{port}/ ({'controls enabled' if allow_controls else 'read-only'})", flush=True)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_on_sigterm(_signum: int, _frame: Any) -> None:
        # Route SIGTERM through the same finalization path as Ctrl-C so an
        # active benchmark restores simulator state before its forwards close.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        server.shutdown()
        server.server_close()
        controls.close()
        live.close()
