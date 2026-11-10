"""Bounded Kubernetes, HTTP, metrics, and revision observations."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .models import validate_setup_manifest


class RuntimeErrorChecked(RuntimeError):
    """A live readiness or transport operation failed."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def run_command(argv: list[str], *, timeout: float = 15, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Execute argv without a shell, kill the process group on deadline."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
        raise RuntimeErrorChecked(f"command exceeded {timeout:g}s: {argv[0]}")
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def kubectl(site: dict[str, Any], args: list[str], *, timeout: float = 15, stdin: bytes | None = None) -> bytes:
    cluster = site.get("_cluster", site.get("cluster", site))
    argv = [
        "kubectl", "--kubeconfig", str(Path(cluster["kubeconfig"]).expanduser()),
        "--context", str(cluster["context"]), *args,
    ]
    result = run_command(argv, timeout=timeout, stdin=stdin)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeErrorChecked(f"kubectl failed ({args[:3]}): {detail[:500]}")
    return result.stdout


def normalize_image_digest(image_id: str) -> str | None:
    match = re.search(r"sha256:[0-9a-f]{64}", image_id)
    return match.group(0) if match else None


def metric_samples(text: str, names: list[str]) -> list[dict[str, Any]]:
    """Parse matching Prometheus samples, preserving labels and sample time."""
    wanted = set(names)
    found: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([^\s]+)(?:\s+([0-9]+))?", line)
        if not match or match.group(1) not in wanted:
            continue
        labels: dict[str, str] = {}
        if match.group(2):
            for item in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', match.group(2)):
                labels[item.group(1)] = bytes(item.group(2), "utf-8").decode("unicode_escape")
        try:
            value = float(match.group(3))
            timestamp_ms = int(match.group(4)) if match.group(4) else None
        except ValueError:
            continue
        found.append({"name": match.group(1), "labels": labels, "value": value, "timestampMs": timestamp_ms})
    return found


def overlay_snapshot(site: dict[str, Any]) -> dict[str, Any]:
    overlay = site["overlay"]
    namespace = overlay["namespace"]
    resource = overlay["resource"]
    kind, name = resource.split("/", 1)
    obj = json.loads(kubectl(site, ["-n", namespace, "get", kind, name, "-o", "json"]))
    annotations = obj.get("metadata", {}).get("annotations", {})
    revision = annotations.get(overlay["revisionAnnotation"], "")
    data_key = overlay["dataKey"]
    payload = obj.get("data", {}).get(data_key)
    if not isinstance(payload, str):
        raise RuntimeErrorChecked(f"{site['name']}: overlay {resource} missing data[{data_key!r}]")
    config = json.loads(payload)
    candidates = config.get("candidates")
    if not isinstance(candidates, list):
        raise RuntimeErrorChecked(f"{site['name']}: overlay routing data has no candidates array")
    parsed = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        rank = item.get("rank")
        score = item.get("score")
        if not isinstance(rank, int) or not isinstance(score, (int, float)):
            continue
        parsed.append({
            "key": str(item.get(overlay.get("candidateKey", "cluster"), "")),
            "name": str(item.get("name", "")),
            "site": str(item.get("site", "")),
            "cluster": str(item.get("cluster", "")),
            "rank": rank,
            "score": float(score),
            "fresh": item.get("fresh") if isinstance(item.get("fresh"), bool) else None,
            "admissionState": item.get("admission_state"),
        })
    return {
        "revision": revision,
        "resourceVersion": obj.get("metadata", {}).get("resourceVersion"),
        "candidates": parsed,
        "topCandidate": min(parsed, key=lambda c: c["rank"]) if parsed else None,
    }


def provider_snapshot(site: dict[str, Any]) -> dict[str, Any]:
    provider = site["provider"]
    namespace = provider.get("namespace", site["namespace"])
    kind, name = provider["resource"].split("/", 1)
    obj = json.loads(kubectl(site, ["-n", namespace, "get", kind, name, "-o", "json"]))
    status = obj.get("status", {})
    generation = obj.get("metadata", {}).get("generation")
    conditions = status.get("conditions", [])
    normalized = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        observed = condition.get("observedGeneration")
        normalized.append({
            "type": condition.get("type"), "status": condition.get("status"),
            "reason": condition.get("reason"), "message": condition.get("message"),
            "observedGeneration": observed,
            "currentGeneration": observed is not None and generation is not None and observed == generation,
        })
    status_observed = status.get("observedGeneration")
    metrics_config = obj.get("spec", {}).get("metricsConfig", {})
    live_capacity = metrics_config.get("queueCapacity") if isinstance(metrics_config, dict) else None
    expected_capacity = provider.get("queueCapacity")
    try:
        capacity_matches = live_capacity is not None and expected_capacity is not None and float(live_capacity) == float(expected_capacity)
    except (TypeError, ValueError):
        capacity_matches = False
    return {"generation": generation, "observedGeneration": status_observed,
            "currentGeneration": generation is not None and status_observed == generation,
            "conditions": normalized, "resource": f"{kind}/{name}",
            "queueCapacity": live_capacity, "expectedQueueCapacity": expected_capacity,
            "queueCapacityMatches": capacity_matches}


def _resource_ready(site: dict[str, Any], target: dict[str, str]) -> dict[str, Any]:
    namespace = target.get("namespace", site["namespace"])
    resource = target["resource"]
    kind, name = resource.split("/", 1)
    obj = json.loads(kubectl(site, ["-n", namespace, "get", kind, name, "-o", "json"]))
    spec = obj.get("spec", {})
    status = obj.get("status", {})
    if kind.lower() in {"deployment", "deployments"}:
        wanted = int(spec.get("replicas", 1))
        ready = int(status.get("readyReplicas", 0))
        generation = obj.get("metadata", {}).get("generation")
        available = any(c.get("type") == "Available" and c.get("status") == "True" for c in status.get("conditions", []))
        current = status.get("observedGeneration") == generation
        return {"resource": resource, "namespace": namespace, "ready": ready >= wanted and current and available,
                "desired": wanted, "readyReplicas": ready, "observedGeneration": status.get("observedGeneration"),
                "generation": generation, "available": available}
    if kind.lower() in {"statefulset", "statefulsets"}:
        wanted = int(spec.get("replicas", 1))
        ready = int(status.get("readyReplicas", 0))
        generation = obj.get("metadata", {}).get("generation")
        current = status.get("observedGeneration") == generation
        return {"resource": resource, "namespace": namespace, "ready": ready >= wanted and current,
                "desired": wanted, "readyReplicas": ready, "observedGeneration": status.get("observedGeneration"),
                "generation": generation}
    if kind.lower() in {"daemonset", "daemonsets"}:
        wanted = int(status.get("desiredNumberScheduled", 0))
        ready = int(status.get("numberReady", 0))
        generation = obj.get("metadata", {}).get("generation")
        current = status.get("observedGeneration") == generation
        return {"resource": resource, "namespace": namespace, "ready": wanted > 0 and ready >= wanted and current,
                "desired": wanted, "readyReplicas": ready, "observedGeneration": status.get("observedGeneration"),
                "generation": generation, "currentGeneration": current}
    if kind.lower() in {"inferenceprovider", "inferenceproviders"}:
        generation = obj.get("metadata", {}).get("generation")
        status = obj.get("status", {})
        conditions = status.get("conditions", [])
        available = next((item for item in conditions if item.get("type") in {"Available", "Ready"}), None)
        metrics = next((item for item in conditions if item.get("type") == "MetricsAvailable"), None)
        ready = bool(available and available.get("status") == "True" and
                     available.get("observedGeneration") == generation and
                     metrics and metrics.get("status") == "True" and
                     metrics.get("observedGeneration") == generation)
        return {"resource": resource, "namespace": namespace, "ready": ready,
                "availableCondition": available, "metricsCondition": metrics,
                "generation": generation}
    raise RuntimeErrorChecked(f"unsupported readiness target type {kind!r}")


def runtime_image_snapshot(site: dict[str, Any], image_lock: dict[str, Any]) -> dict[str, Any]:
    selector = image_lock["selector"]
    # The combined-site producer currently serializes an exact Pod identity as
    # ``pod=<name>`` in its selector field. Treat that form as a name lookup;
    # passing it to kubectl -l would silently match no Pods because Pod names
    # are not labels. Ordinary selectors retain kubectl's label semantics.
    if selector.startswith("pod="):
        pod_name = selector.removeprefix("pod=")
        if not pod_name or any(character.isspace() for character in pod_name):
            raise RuntimeErrorChecked("runtime image pod selector contains an invalid Pod name")
        args = ["-n", image_lock["namespace"], "get", "pod", pod_name, "-o", "json"]
    else:
        args = ["-n", image_lock["namespace"], "get", "pods", "-l", selector, "-o", "json"]
    pods_value = json.loads(kubectl(site, args))
    if pods_value.get("kind") == "Pod":
        pods = {"items": [pods_value]}
    else:
        pods = pods_value
    expected_digest = normalize_image_digest(str(image_lock["imageID"]))
    observations = []
    for pod in pods.get("items", []):
        pod_status = pod.get("status", {})
        container_statuses = pod_status.get("containerStatuses", [])
        status = next((x for x in container_statuses if x.get("name") == image_lock["container"]), None)
        if not status:
            continue
        actual_id = str(status.get("imageID", ""))
        actual_digest = normalize_image_digest(actual_id)
        observations.append({
            "pod": pod.get("metadata", {}).get("name"),
            "ready": bool(status.get("ready")),
            "image": status.get("image"),
            "imageID": actual_id,
            "digestMatches": bool(expected_digest and actual_digest == expected_digest),
            "restarts": status.get("restartCount", 0),
        })
    return {"component": image_lock["component"], "expectedImageID": image_lock["imageID"], "pods": observations,
            "ready": bool(observations) and all(x["ready"] and x["digestMatches"] for x in observations)}


def metrics_snapshot(site: dict[str, Any], transport: "EndpointTransport", max_age: float) -> dict[str, Any]:
    epp = site["epp"]
    response = transport.request(site, epp, "GET", epp["metricsUrl"], timeout=10,
                                 token_file=epp.get("_resolvedCredentialFile", epp.get("credentialFile")),
                                 ca_file=epp.get("_resolvedCaFile", epp.get("caFile")))
    if response["status"] != 200:
        raise RuntimeErrorChecked(f"{site['name']}: authenticated EPP metrics returned HTTP {response['status']}")
    text = response["body"].decode(errors="replace")
    queue = metric_samples(text, epp["queueMetricNames"])
    ready = metric_samples(text, epp["readyMetricNames"])
    labels = epp.get("requiredLabels", {})
    alternatives = epp.get("requiredLabelAlternatives", [])
    if labels:
        queue = [sample for sample in queue if all(sample["labels"].get(key) == value for key, value in labels.items())]
        ready = [sample for sample in ready if all(sample["labels"].get(key) == value for key, value in labels.items())]
    elif alternatives:
        matches = lambda sample: any(all(sample["labels"].get(key) == value for key, value in option.items()) for option in alternatives)
        queue = [sample for sample in queue if matches(sample)]
        ready = [sample for sample in ready if matches(sample)]
    if not queue:
        raise RuntimeErrorChecked(f"{site['name']}: no queue-depth EPP metric found")
    if not ready:
        raise RuntimeErrorChecked(f"{site['name']}: no ready-endpoint metric found")
    if not any(sample["value"] > 0 for sample in ready):
        raise RuntimeErrorChecked(f"{site['name']}: EPP reports no ready simulator endpoints")
    now_ms = int(time.time() * 1000)
    ages = [((now_ms - item["timestampMs"]) / 1000) for item in queue if item["timestampMs"] is not None]
    newest_age = min(ages) if ages else 0.0
    timestamp_available = bool(ages)
    if timestamp_available and (newest_age < -5 or newest_age > max_age):
        raise RuntimeErrorChecked(f"{site['name']}: EPP queue sample age {newest_age:.1f}s exceeds {max_age:.1f}s")
    freshness_mode = "prometheus-sample-timestamp" if timestamp_available else "authenticated-live-scrape-only"
    return {
        "observedAtUtc": utc_now(),
        "monotonicNs": time.monotonic_ns(),
        "httpStatus": response["status"],
        "queue": queue,
        "readyEndpoints": ready,
        "sampleTimestampAvailable": timestamp_available,
        "sampleAgeSeconds": newest_age if timestamp_available else None,
        "freshnessBasis": freshness_mode,
        "dateHeader": response["headers"].get("date"),
        "bodySha256": __import__("hashlib").sha256(response["body"]).hexdigest(),
    }


def gateway_revisions(site: dict[str, Any]) -> dict[str, Any]:
    gateway = site["gateway"]
    kind, name = gateway["resource"].split("/", 1)
    # This environment is intentionally persistent and can sit idle for hours.
    # A --since window loses the revision accepted at startup after the window
    # passes, even though the current Gateway is still serving it. Read a
    # bounded tail of the current workload's logs so the latest accepted and
    # serving revisions remain observable after idle periods.
    logs = kubectl(site, ["-n", gateway["namespace"], "logs", f"{kind}/{name}", "-c", gateway["container"], "--tail=1000"], timeout=20).decode(errors="replace")
    accepted = _last_field(logs, gateway["acceptedField"])
    serving = _last_field(logs, gateway["servingField"])
    return {"acceptedRevision": accepted, "servingRevision": serving}


def _last_field(text: str, field: str) -> str | None:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    pattern = re.compile(rf"(?:^|\s){re.escape(field)}=(?:\"([^\"]+)\"|([^\s,}}]+))")
    for line in reversed(text.splitlines()):
        matches = list(pattern.finditer(line))
        if matches:
            match = matches[-1]
            value = match.group(1) or match.group(2)
            if value:
                return value
    return None


def conditions_ready(snapshot: dict[str, Any]) -> tuple[bool, list[str]]:
    problems = []
    for condition in snapshot.get("conditions", []):
        if condition.get("type") in {"Available", "Ready", "MetricsAvailable"}:
            if condition.get("status") != "True" or not condition.get("currentGeneration", False):
                problems.append(f"{condition.get('type')}={condition.get('status')} reason={condition.get('reason')}")
    if not any(x.get("type") in {"Available", "Ready"} and x.get("status") == "True" and x.get("currentGeneration", False) for x in snapshot.get("conditions", [])):
        problems.append("no current-generation Available/Ready=True condition")
    metrics = next((x for x in snapshot.get("conditions", []) if x.get("type") == "MetricsAvailable"), None)
    if not metrics or metrics.get("status") != "True" or not metrics.get("currentGeneration", False):
        problems.append("no current-generation MetricsAvailable=True condition")
    if not snapshot.get("currentGeneration", False):
        problems.append("InferenceProvider status.observedGeneration is absent or stale")
    return not problems, problems


def gate_snapshot(handoff: dict[str, Any], transport: "EndpointTransport", *, max_age_seconds: float = 30) -> dict[str, Any]:
    result: dict[str, Any] = {"observedAtUtc": utc_now(), "monotonicNs": time.monotonic_ns(), "sites": [], "ok": True, "problems": []}
    run_id = handoff["runId"]
    known = set(run_command(["kind", "get", "clusters"], timeout=10).stdout.decode().split())
    for site in handoff["sites"]:
        observation: dict[str, Any] = {"name": site["name"], "clusterName": site["clusterName"], "ok": True, "problems": []}
        if site["ownerRunId"] != run_id or site["clusterName"] not in known:
            observation["ok"] = False
            observation["problems"].append("exact run-owned Kind cluster not found")
        readiness = []
        try:
            for target in site["readinessTargets"]:
                readiness.append(_resource_ready(site, target))
            observation["readinessTargets"] = readiness
            for check in readiness:
                if not check["ready"]:
                    observation["ok"] = False
                    observation["problems"].append(f"{check['resource']} not ready")
            simulator_targets = site["simulator"].get("targets", [])
            simulator_snapshots = []
            for target in simulator_targets:
                try:
                    simulator_snapshots.append(simulator_target_snapshot(site, target, transport))
                except Exception as exc:
                    observation["ok"] = False
                    observation["problems"].append(f"simulator {target.get('podName', 'unknown')} not ready: {exc}")
                    simulator_snapshots.append({"podName": target.get("podName"), "ready": False,
                                                "adminApiReachable": False, "error": str(exc)[:400]})
            observation["simulators"] = simulator_snapshots
            provider = provider_snapshot(site)
            observation["provider"] = provider
            provider_ok, provider_problems = conditions_ready(provider)
            if not provider_ok:
                observation["ok"] = False
                observation["problems"].extend(provider_problems)
            if not provider.get("queueCapacityMatches"):
                observation["ok"] = False
                observation["problems"].append("live InferenceProvider metricsConfig.queueCapacity differs from the run handoff")
            metric = metrics_snapshot(site, transport, max_age_seconds)
            observation["metrics"] = metric
            overlay = overlay_snapshot(site)
            observation["overlay"] = overlay
            if not overlay["revision"] or not overlay["candidates"]:
                observation["ok"] = False
                observation["problems"].append("overlay revision/candidates are absent")
            if any(candidate["fresh"] is False for candidate in overlay["candidates"]):
                observation["ok"] = False
                observation["problems"].append("overlay has a stale candidate")
            gateway = gateway_revisions(site)
            observation["gateway"] = gateway
            if gateway["acceptedRevision"] != overlay["revision"] or gateway["servingRevision"] != overlay["revision"]:
                observation["ok"] = False
                observation["problems"].append("Gateway has not accepted and served the current overlay revision")
        except Exception as exc:  # retain a per-site diagnostic rather than hiding partial observations
            observation["ok"] = False
            observation["problems"].append(str(exc))
        result["sites"].append(observation)
        if not observation["ok"]:
            result["ok"] = False
            result["problems"].extend(f"{site['name']}: {x}" for x in observation["problems"])
    if len(result["sites"]) == 3 and all(item.get("overlay", {}).get("candidates") for item in result["sites"]):
        expected_sites = {site["name"] for site in handoff["sites"]}
        distribution = {}
        for item in result["sites"]:
            observed_sites = {candidate.get("site") or candidate.get("key") for candidate in item["overlay"]["candidates"]}
            distribution[item["name"]] = sorted(str(name) for name in observed_sites)
            if observed_sites != expected_sites:
                item["ok"] = False
                message = f"distributed overlay candidates {sorted(str(name) for name in observed_sites)} do not cover all sites"
                item["problems"].append(message)
                result["problems"].append(f"{item['name']}: {message}")
                result["ok"] = False
        result["overlayDistribution"] = {"expectedSites": sorted(expected_sites), "observedSitesByGateway": distribution,
                                         "ok": all(set(values) == expected_sites for values in distribution.values())}
    return result


class PortForward:
    """Owned, bounded kubectl port-forward process for one endpoint."""
    def __init__(self, site: dict[str, Any], config: dict[str, Any], log_path: Path):
        self.site = site
        self.config = config
        self.log_path = log_path
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        cfg = self.config
        cluster = self.site.get("_cluster", self.site.get("cluster", self.site))
        port_hint = int(cfg["localPort"])
        remote = int(cfg["remotePort"])
        if not (1024 <= port_hint <= 65535 and 1 <= remote <= 65535):
            raise RuntimeErrorChecked("port-forward ports are invalid")
        argv = ["kubectl", "--kubeconfig", str(Path(cluster["kubeconfig"]).expanduser()), "--context", cluster["context"],
                "-n", cfg["namespace"], "port-forward", "--address", "127.0.0.1", cfg["resource"], f":{remote}"]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("wb") as stream:
            self.process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT, start_new_session=(os.name == "posix"))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeErrorChecked(f"kubectl port-forward exited; see {self.log_path}")
            local = port_forward_log_port(self.log_path, remote)
            if local is not None:
                try:
                    with socket.create_connection(("127.0.0.1", local), timeout=0.2):
                        cfg["localPort"] = local
                        return
                except OSError:
                    pass
            time.sleep(0.1)
        self.stop()
        raise RuntimeErrorChecked(f"kubectl port-forward did not bind a loopback port for remote {remote}; see {self.log_path}")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()
            self.process.wait(timeout=2)


def port_forward_log_confirms(path: Path, local: int, remote: int) -> bool:
    """Ensure the listener is this owned kubectl forward, not a port collision."""
    return port_forward_log_port(path, remote) == local


def port_forward_log_port(path: Path, remote: int) -> int | None:
    """Read kubectl's dynamically assigned loopback port for a remote endpoint."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(rf"Forwarding from 127\.0\.0\.1:(\d+) -> {remote}(?:\s|$)", content)
    return int(match.group(1)) if match else None


def _replace_url_port(url: str, port: int) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise RuntimeErrorChecked("cannot update a port-forward URL without a hostname")
    host = f"[{hostname}]" if ":" in hostname else hostname
    return parsed._replace(netloc=f"{host}:{port}").geturl()


class EndpointTransport:
    """HTTP client honoring handoff host maps, CA files, and bearer files."""
    def __init__(self, handoff: dict[str, Any], evidence: Path | None = None):
        self.handoff = handoff
        self.evidence = evidence or Path(".")
        self.forwards: list[PortForward] = []

    def start_site_forwards(self, site: dict[str, Any]) -> None:
        for key in ("epp", "simulator"):
            endpoint = site.get(key, {})
            cfg = endpoint.get("portForward")
            if not cfg:
                continue
            cfg.setdefault("namespace", site["namespace"])
            instance = PortForward(site, cfg, self.evidence / "port-forwards" / f"{site['name']}-{key}.log")
            instance.start()
            self.forwards.append(instance)
            url_key = "metricsUrl" if key == "epp" else "adminUrl"
            if endpoint.get(url_key):
                endpoint[url_key] = _replace_url_port(str(endpoint[url_key]), int(cfg["localPort"]))

    def close(self) -> None:
        for item in reversed(self.forwards):
            item.stop()
        self.forwards.clear()

    def _destination(self, endpoint: dict[str, Any], parsed: Any) -> tuple[str, int]:
        cfg = endpoint.get("portForward")
        if cfg:
            return "127.0.0.1", int(cfg["localPort"])
        mapping = endpoint.get("resolve", {}).get(parsed.hostname, {}) if isinstance(endpoint.get("resolve"), dict) else {}
        if isinstance(mapping, str):
            return mapping, parsed.port or (443 if parsed.scheme == "https" else 80)
        if mapping:
            return str(mapping.get("address", parsed.hostname)), int(mapping.get("port", parsed.port or (443 if parsed.scheme == "https" else 80)))
        return str(parsed.hostname), int(parsed.port or (443 if parsed.scheme == "https" else 80))

    def request(self, site: dict[str, Any], endpoint: dict[str, Any], method: str, url: str,
                *, body: bytes | None = None, headers: dict[str, str] | None = None,
                timeout: float = 10, token_file: str | None = None, ca_file: str | None = None) -> dict[str, Any]:
        parsed = urlsplit(url)
        host, port = self._destination(endpoint, parsed)
        original_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request_headers = dict(headers or {})
        request_headers.setdefault("Host", f"{parsed.hostname}:{original_port}" if parsed.port else str(parsed.hostname))
        if token_file:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
            if not token or "\n" in token:
                raise RuntimeErrorChecked(f"credential file {token_file} did not contain one token")
            request_headers["Authorization"] = f"Bearer {token}"
        if parsed.scheme == "https":
            context = ssl.create_default_context(cafile=ca_file)
            raw = socket.create_connection((host, port), timeout=timeout)
            try:
                wrapped = context.wrap_socket(raw, server_hostname=str(parsed.hostname))
            except Exception:
                raw.close()
                raise
            conn: http.client.HTTPConnection = _PreconnectedHTTPSConnection(str(parsed.hostname), original_port, wrapped, timeout)
        else:
            raw = socket.create_connection((host, port), timeout=timeout)
            conn = _PreconnectedHTTPConnection(str(parsed.hostname), original_port, raw, timeout)
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            response_body = response.read(8 * 1024 * 1024 + 1)
            if len(response_body) > 8 * 1024 * 1024:
                raise RuntimeErrorChecked("response exceeded Gridzilla's hard 8 MiB capture bound")
            return {"status": response.status, "headers": {k.lower(): v for k, v in response.getheaders()}, "body": response_body}
        finally:
            conn.close()


class _PreconnectedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, sock: socket.socket, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.sock = sock

    def connect(self) -> None:
        if self.sock is None:
            raise RuntimeErrorChecked("preconnected HTTP socket was lost")


class _PreconnectedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, sock: ssl.SSLSocket, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.sock = sock

    def connect(self) -> None:
        if self.sock is None:
            raise RuntimeErrorChecked("preconnected HTTPS socket was lost")


def simulator_config(site: dict[str, Any], transport: EndpointTransport, *, timeout: float = 10) -> dict[str, Any]:
    simulator = site["simulator"]
    response = transport.request(site, simulator, "GET", simulator["adminUrl"], timeout=timeout)
    if response["status"] != 200:
        raise RuntimeErrorChecked(f"{site['name']}: simulator GET /admin/config returned HTTP {response['status']}")
    try:
        config = json.loads(response["body"])
    except json.JSONDecodeError as exc:
        raise RuntimeErrorChecked(f"{site['name']}: simulator admin config was not JSON") from exc
    if not isinstance(config, dict):
        raise RuntimeErrorChecked(f"{site['name']}: simulator config root must be an object")
    return config


def simulator_target_snapshot(site: dict[str, Any], target: dict[str, Any],
                              transport: EndpointTransport) -> dict[str, Any]:
    """Verify a handoff-owned simulator Pod, Deployment generation, and admin API."""
    namespace = target.get("namespace", site["namespace"])
    pod_name = str(target["podName"])
    pod = json.loads(kubectl(site, ["-n", namespace, "get", "pod", pod_name, "-o", "json"]))
    metadata = pod.get("metadata", {})
    pod_status = pod.get("status", {})
    ready = any(item.get("type") == "Ready" and item.get("status") == "True"
                for item in pod_status.get("conditions", []))
    if metadata.get("uid") != target["podUID"] or pod_status.get("phase") != "Running" or not ready:
        raise RuntimeErrorChecked(f"{site['name']}: simulator Pod {pod_name} identity/readiness changed")

    deployment_name = str(target.get("deploymentName", f"vcr-inference-{site['name']}"))
    deployment = json.loads(kubectl(site, ["-n", namespace, "get", "deployment", deployment_name, "-o", "json"]))
    deployment_metadata = deployment.get("metadata", {})
    deployment_status = deployment.get("status", {})
    generation = deployment_metadata.get("generation")
    observed_generation = deployment_status.get("observedGeneration")
    desired = int(deployment.get("spec", {}).get("replicas", 1))
    ready_replicas = int(deployment_status.get("readyReplicas", 0))
    expected_generation = target.get("configGeneration")
    if (generation != expected_generation or observed_generation != generation or
            ready_replicas < desired):
        raise RuntimeErrorChecked(f"{site['name']}: simulator Deployment {deployment_name} generation/readiness changed")

    simulator_endpoint = {**target, "adminUrl": target.get("adminUrl", site["simulator"]["adminUrl"])}
    config = simulator_config({**site, "simulator": simulator_endpoint}, transport)
    fake_metrics = config.get("fake-metrics", config.get("fake_metrics"))
    if not isinstance(fake_metrics, dict):
        raise RuntimeErrorChecked(f"{site['name']}/{pod_name}: simulator is not running fake-metrics mode")
    pointer = site["simulator"]["pressureJsonPointer"]
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.strip("/").split("/")]
    if len(parts) != 2 or parts[0] != "fake-metrics" or parts[1] not in fake_metrics:
        raise RuntimeErrorChecked(f"{site['name']}/{pod_name}: configured simulator pressure metric is missing")
    value = fake_metrics[parts[1]]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeErrorChecked(f"{site['name']}/{pod_name}: configured simulator pressure metric is not numeric")
    return {"podName": pod_name, "podUID": metadata.get("uid"), "namespace": namespace,
            "podResourceVersion": metadata.get("resourceVersion"), "phase": pod_status.get("phase"),
            "ready": True, "deployment": deployment_name, "configGeneration": generation,
            "observedGeneration": observed_generation, "desiredReplicas": desired,
            "readyReplicas": ready_replicas, "fakeMetric": parts[1], "fakeMetricValue": value,
            "adminApiReachable": True}


def set_json_pointer(document: dict[str, Any], pointer: str, value: Any) -> dict[str, Any]:
    """Return a deep copy with one existing JSON Pointer leaf changed."""
    clone = json.loads(json.dumps(document))
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.lstrip("/").split("/")]
    if not parts or not parts[0]:
        raise RuntimeErrorChecked("pressure JSON Pointer must identify a nested field")
    cursor: Any = clone
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise RuntimeErrorChecked(f"pressure JSON Pointer parent {part!r} is missing")
        cursor = cursor[part]
    leaf = parts[-1]
    if not isinstance(cursor, dict) or leaf not in cursor:
        raise RuntimeErrorChecked(f"pressure JSON Pointer leaf {leaf!r} is missing")
    cursor[leaf] = value
    return clone


def post_simulator_config(site: dict[str, Any], transport: EndpointTransport, config: dict[str, Any], *, timeout: float = 10) -> dict[str, Any]:
    sim = site["simulator"]
    body = json.dumps(config, separators=(",", ":")).encode()
    response = transport.request(site, sim, "POST", sim["adminUrl"], body=body,
                                headers={"Content-Type": "application/json"}, timeout=timeout)
    if response["status"] not in {200, 202, 204}:
        raise RuntimeErrorChecked(f"{site['name']}: simulator atomic config POST returned HTTP {response['status']}")
    return {"status": response["status"], "responseBytes": len(response["body"])}


def verify_exact_kind_ownership(handoff: dict[str, Any]) -> list[str]:
    # Re-read the setup-owned manifest immediately before live gates. This
    # confirms the handoff still belongs to a successful setup run and that
    # its exact cluster inventory has not been edited since parsing.
    validate_setup_manifest(handoff, Path(handoff["_handoffPath"]))
    result = run_command(["kind", "get", "clusters"], timeout=10)
    if result.returncode:
        raise RuntimeErrorChecked("kind get clusters failed")
    existing = set(result.stdout.decode().split())
    missing = [site["clusterName"] for site in handoff["sites"] if site["clusterName"] not in existing]
    return missing


def parse_overlay_candidate(snapshot: dict[str, Any], key: str, value: str) -> dict[str, Any] | None:
    return next((candidate for candidate in snapshot.get("candidates", []) if candidate.get(key) == value), None)


def monotonic_utc_pair() -> dict[str, Any]:
    return {"utc": utc_now(), "monotonicNs": time.monotonic_ns()}
