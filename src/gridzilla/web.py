"""Localhost dashboard API and static UI server."""

from __future__ import annotations

import json
import hmac
import mimetypes
import os
import signal
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .controls import BenchmarkControls, ControlFailure
from .controller import _cluster_resources, _startup_transports
from .metrics import live_observation_is_fresh, render_metrics
from .models import ValidationError, redacted_handoff
from .report_model import REPORT_FORMAT, normalize_report
from .runtime import EndpointTransport, gate_snapshot, utc_now


def _gateway_revision_is_current(site: dict[str, Any]) -> bool:
    overlay_revision = site.get("overlay", {}).get("revision")
    serving_revision = site.get("gateway", {}).get("servingRevision")
    return (isinstance(overlay_revision, str) and bool(overlay_revision)
            and isinstance(serving_revision, str) and bool(serving_revision)
            and overlay_revision == serving_revision)


def _live_snapshot_max_age(refresh_seconds: float, handoff: dict[str, Any] | None) -> float:
    """Allow one bounded collection cycle while keeping the snapshot expiry finite."""
    refresh = max(1.0, min(float(refresh_seconds), 30.0))
    # A full collection includes serial Kubernetes and authenticated EPP reads.
    # Give that work a little room, but never let a snapshot outlive the handoff's
    # configured EPP freshness window. A genuinely stalled observer still expires.
    observer_budget = max(10.0, refresh * 4 + 4)
    limits = [float(site["epp"]["maxSampleAgeSeconds"]) for site in (handoff or {}).get("sites", [])
              if isinstance(site.get("epp", {}).get("maxSampleAgeSeconds"), (int, float))]
    return min(observer_budget, min(limits)) if limits else observer_budget


class LiveState:
    def __init__(self, handoff: dict[str, Any] | None, refresh_seconds: float, evidence_root: Path):
        self.handoff = handoff
        self.evidence_root = evidence_root.resolve()
        self.refresh_seconds = max(1.0, min(float(refresh_seconds), 30.0))
        self.max_live_age_seconds = _live_snapshot_max_age(self.refresh_seconds, handoff)
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
            value = json.loads(json.dumps(self.value))
        if value.get("status") == "LIVE" and not live_observation_is_fresh(
                value, max_age_seconds=self.max_live_age_seconds):
            value["status"] = "STALE" if value.get("observedAtUtc") else "UNAVAILABLE"
            value["reason"] = "live observer has not produced a fresh complete snapshot"
            for site in value.get("sites", []):
                if site.get("status") == "LIVE":
                    site["status"] = "STALE"
                if site.get("metrics", {}).get("status") == "LIVE":
                    site["metrics"]["status"] = "STALE"
        return value

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
        # Stamp the snapshot when the complete observation is available. The
        # gate timestamp is taken before its sequential cluster/API reads and
        # would make a healthy, slower collection appear stale immediately.
        observed_at = utc_now()
        return {"schemaVersion": 1, "status": "LIVE" if gate["ok"] else "STALE" if any(
                    item["status"] == "STALE" for item in observed_sites) else "UNAVAILABLE",
                "phase": _phase_from_saved_timeline(timeline), "observedAtUtc": observed_at,
                "source": self.handoff["source"], "sites": observed_sites,
                "localOverlay": {"site": client_site, "revision": (local or {}).get("overlay", {}).get("revision"),
                                 "candidates": (local or {}).get("overlay", {}).get("candidates", [])},
                "crossSite": {"status": "LIVE" if all_fresh else "STALE", "sites": cross_sites},
                "gates": {"simulators": "READY" if all(item.get("simulators") and all(target.get("ready") and target.get("adminApiReachable") for target in item["simulators"]) for item in observed_sites) else "UNAVAILABLE",
                          "eppMetrics": "LIVE" if all(item.get("metrics", {}).get("status") == "LIVE" for item in observed_sites) else "STALE",
                          "providers": "READY" if all(item.get("provider", {}).get("available") for item in observed_sites) else "UNAVAILABLE",
                          "overlays": "LIVE" if all(item.get("overlay", {}).get("revision") for item in observed_sites) else "UNAVAILABLE",
                          "gateways": "READY" if all(_gateway_revision_is_current(item)
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
        normalized_path = directory / "normalized-report.json"
        report_path = directory / "report-data.json"
        live_path = directory / "live-state.json"
        if not result_path.exists() and not report_path.exists() and not normalized_path.exists() and not live_path.exists():
            continue
        normalized = _read_json(normalized_path, {})
        if normalized.get("formatVersion") != REPORT_FORMAT:
            legacy = _read_json(report_path, {})
            normalized = normalize_report(directory, report_data=legacy if legacy else None)
        run = normalized.get("run", {})
        topology = run.get("topology", {})
        source = run.get("source", {})
        setup = source.get("setup") or source.get("gridzilla") or {}
        items.append({"id": run.get("id") or directory.name,
                      "startedAtUtc": run.get("startedAtUtc") or run.get("createdAtUtc"),
                      "status": run.get("status", "HISTORICAL"),
                      "scenario": run.get("scenario", {}), "type": run.get("type"),
                      "topology": topology, "profile": run.get("profile", {}),
                      "sourceIdentity": setup.get("revision") or setup.get("commit"),
                      "throughputEligible": (run.get("result") or {}).get("throughputEligibility"),
                      "formatVersion": normalized.get("formatVersion")})
    return items


def _saved_run(root: Path, run_id: str) -> dict[str, Any] | None:
    if not run_id or Path(run_id).name != run_id:
        return None
    directory = (root / run_id).resolve()
    if root.resolve() not in directory.parents:
        return None
    normalized = _read_json(directory / "normalized-report.json", {})
    if normalized.get("formatVersion") == REPORT_FORMAT:
        normalized["evidenceFiles"] = _safe_evidence_files(directory)
        return normalized
    report = _read_json(directory / "report-data.json", {})
    result = _read_json(directory / "run-result.json", {})
    live = _read_json(directory / "live-state.json", {})
    if not report and not result and not live:
        return None
    normalized = normalize_report(directory, report_data=report if report else None)
    normalized["evidenceFiles"] = _safe_evidence_files(directory)
    return normalized


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
          allow_controls: bool = False, profiles_dir: Path | None = None,
          endpoints_dir: Path | None = None, agent_token_file: Path | None = None,
          _shutdown_event: threading.Event | None = None,
          _on_ready: Callable[[int], None] | None = None) -> None:
    if host != "127.0.0.1":
        raise ValueError("Gridzilla UI must bind only to 127.0.0.1")
    assets = (dist or Path(__file__).resolve().parents[2] / "web" / "dist").resolve()
    if not assets.is_dir():
        raise FileNotFoundError(f"built Gridzilla UI not found at {assets}; run npm run build")
    evidence_root = runs_dir.resolve()
    agent_token: bytes | None = None
    if agent_token_file is not None:
        token_path = agent_token_file.expanduser().resolve(strict=True)
        if not token_path.is_file():
            raise ValueError("local agent token path must be a regular file")
        if os.name == "posix" and stat.S_IMODE(token_path.stat().st_mode) & 0o077:
            raise ValueError("local agent token file must not be accessible by group or other users; use chmod 600")
        agent_token = token_path.read_bytes().strip()
        if len(agent_token) < 32:
            raise ValueError("local agent token file must contain at least 32 bytes")
    live = LiveState(handoff, refresh_seconds, evidence_root)
    try:
        controls = BenchmarkControls(
            enabled=allow_controls, handoff=handoff, evidence_root=evidence_root,
            profiles_dir=profiles_dir or Path(__file__).resolve().parents[2] / "profiles",
            transport=live.transport, endpoints_dir=endpoints_dir,
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

        def _error(self, code: str, message: str, status: int) -> None:
            self._json({"status": "REJECTED", "error": {"code": code, "message": message}}, status)

        def _authorized_control(self) -> bool:
            host = self.headers.get("Host", "").lower()
            if host not in {f"127.0.0.1:{server_port}", f"localhost:{server_port}"}:
                return False
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                return False
            origin = self.headers.get("Origin")
            if origin is not None:
                # A bearer token is for non-browser local agents. It must not
                # override a hostile browser Origin and bypass same-origin
                # protection.
                return _local_control_origin(host, origin, server_port)
            authorization = self.headers.get("Authorization", "")
            if agent_token is None or not authorization.startswith("Bearer "):
                return False
            return hmac.compare_digest(authorization[7:].encode("utf-8"), agent_token)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            route = urlsplit(self.path).path
            if not controls.enabled:
                self._error("CONTROLS_DISABLED", "restart with --allow-controls to enable benchmark actions", 405)
                return
            if not self._authorized_control():
                self._error("LOCAL_AUTH_REQUIRED", "use the same-origin UI or configured local-agent bearer token", 403)
                return
            if self.headers.get("Transfer-Encoding"):
                self._error("CHUNKED_NOT_ACCEPTED", "chunked control requests are not accepted", 411)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._error("INVALID_CONTENT_LENGTH", "invalid Content-Length", 400)
                return
            if content_length < 0 or content_length > 4096:
                self._error("BODY_TOO_LARGE", "control request body exceeds 4 KiB", 413)
                return
            try:
                body = json.loads(self.rfile.read(content_length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error("INVALID_JSON", "control request must be JSON", 400)
                return
            if not isinstance(body, dict):
                self._error("INVALID_BODY", "control request must be a JSON object", 400)
                return
            try:
                if route == "/api/preflight":
                    if set(body) - {"scenario", "configurationId", "profile", "rates", "soakSeconds", "requestBodySizesBytes"}:
                        raise ControlFailure("preflight accepts only scenario, configurationId, profile, rates, soak, and body sizes", 400)
                    result = controls.preflight(str(body.get("scenario", "")), str(body.get("profile", "")),
                                                body.get("configurationId"), body.get("rates"),
                                                body.get("soakSeconds"), body.get("requestBodySizesBytes"))
                    response = dict(result)
                    endpoint_status = response.pop("status", None)
                    response["status"] = "READY" if response.get("ok", True) else "BLOCKED"
                    if isinstance(endpoint_status, int) and not isinstance(endpoint_status, bool):
                        response["endpointStatus"] = endpoint_status
                    self._json(response)
                    return
                if route == "/api/run":
                    scenario = body.get("scenario")
                    if scenario == "generic-endpoint":
                        if set(body) - {"scenario", "configurationId", "profile", "rates", "soakSeconds", "requestBodySizesBytes"}:
                            raise ControlFailure("generic run accepts only a listed configuration, profile, rates, soak, and body sizes", 400)
                        result = controls.start_generic(str(body.get("configurationId", "")),
                                                        str(body.get("profile", "")), body.get("rates"),
                                                        body.get("soakSeconds"), body.get("requestBodySizesBytes"))
                    elif scenario in {None, "grid-llmd-signals"}:
                        if set(body) - {"scenario", "profile", "rates", "soakSeconds", "requestBodySizesBytes"}:
                            raise ControlFailure("Grid run accepts only scenario, profile, rates, soak, and body sizes", 400)
                        result = controls.start(str(body.get("profile", "")), body.get("rates"),
                                                body.get("soakSeconds"), body.get("requestBodySizesBytes"))
                    else:
                        raise ControlFailure("choose a server-listed scenario", 400)
                elif route == "/api/stop":
                    if set(body) != {"operationId"}:
                        raise ControlFailure("stop requires the operationId returned by start", 400)
                    result = controls.stop(str(body.get("operationId", "")))
                elif route == "/api/pressure":
                    if set(body) != {"value", "site"}:
                        raise ControlFailure("pressure accepts only value and site from the Grid handoff", 400)
                    result = controls.start_pressure(body.get("value"), body.get("site"))
                elif route == "/api/pressure/restore":
                    if set(body) != {"operationId"}:
                        raise ControlFailure("restore requires the pressure operationId", 400)
                    result = controls.restore_pressure(str(body.get("operationId", "")))
                else:
                    self._error("NOT_FOUND", "control endpoint not found", 404)
                    return
            except ControlFailure as exc:
                self._error("CONTROL_REJECTED", str(exc), exc.status)
                return
            except (ValueError, ValidationError) as exc:
                self._error("INVALID_CONFIGURATION", str(exc), 400)
                return
            except OSError:
                self._error("LOCAL_STORAGE_UNAVAILABLE", "Gridzilla could not persist operation state or evidence", 503)
                return
            self._json(result, 202)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            route = urlsplit(self.path).path
            if route == "/metrics":
                payload = render_metrics(live.read(), handoff, evidence_root,
                                         max_live_age_seconds=live.max_live_age_seconds).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if route == "/api/state":
                value = live.read()
                control_state = controls.status()
                value["controls"] = control_state
                operation_id = control_state.get("operationId") or control_state.get("lastOperationId")
                if operation_id:
                    value["operationState"] = controls.operation(str(operation_id))
                self._json(value)
                return
            if route == "/api/configurations":
                state = controls.status()
                self._json({"scenarios": state["scenarios"], "profiles": state["profileSettings"],
                            "endpointConfigurations": controls.generic_configurations(),
                            "gridAvailable": handoff is not None})
                return
            if route.startswith("/api/operations/"):
                operation_id = unquote(route.removeprefix("/api/operations/"))
                if "/" in operation_id or not operation_id or len(operation_id) > 64:
                    self._error("INVALID_OPERATION_ID", "operationId is malformed", 400)
                    return
                value = controls.operation(operation_id)
                if value is None:
                    self._error("OPERATION_NOT_FOUND", "no saved operation has this ID", 404)
                else:
                    self._json(value)
                return
            if route == "/api/runs":
                self._json(_saved_runs(evidence_root))
                return
            if route.startswith("/api/runs/") and "/files/" in route:
                run_id, filename = route.removeprefix("/api/runs/").split("/files/", 1)
                target = _safe_run_file(evidence_root, unquote(run_id), unquote(filename))
                if target is None:
                    self._error("EVIDENCE_NOT_FOUND", "saved evidence file not found", 404)
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
                if value is None:
                    self._error("RUN_NOT_FOUND", "saved run not found", 404)
                else:
                    self._json(value)
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
    server_port = int(server.server_address[1])
    print(f"Gridzilla dashboard: http://{host}:{server_port}/ ({'controls enabled' if allow_controls else 'read-only'})", flush=True)
    previous_sigterm = signal.getsignal(signal.SIGTERM) if _shutdown_event is None else None

    def stop_on_sigterm(_signum: int, _frame: Any) -> None:
        # Route SIGTERM through the same finalization path as Ctrl-C so an
        # active benchmark restores simulator state before its forwards close.
        raise KeyboardInterrupt

    try:
        if _shutdown_event is None:
            signal.signal(signal.SIGTERM, stop_on_sigterm)
            if _on_ready:
                _on_ready(server_port)
            server.serve_forever(poll_interval=0.25)
        else:
            server.timeout = 0.1
            if _on_ready:
                _on_ready(server_port)
            while not _shutdown_event.is_set():
                server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
            server.shutdown()
        server.server_close()
        controls.close()
        live.close()
