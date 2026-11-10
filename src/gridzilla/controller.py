"""Gridzilla orchestration. This code observes clusters and only mutates llm-d-sim admin config."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .models import Profile, ValidationError, duration_seconds, stable_digest
from .runner import K6Failure, execute_window, verify_k6_version
from .runtime import (
    EndpointTransport, PortForward, RuntimeErrorChecked, conditions_ready, gate_snapshot,
    kubectl, metrics_snapshot, monotonic_utc_pair, normalize_image_digest,
    overlay_snapshot, parse_overlay_candidate, post_simulator_config,
    provider_snapshot, runtime_image_snapshot, set_json_pointer, simulator_config,
    utc_now, verify_exact_kind_ownership, _replace_url_port,
)
from .stats import summarize_raw


class BenchmarkFailure(RuntimeError):
    """A safety gate or benchmark phase failed."""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def evidence_root(handoff: dict[str, Any], configured: Path | None = None) -> Path:
    if configured:
        return configured.resolve()
    return Path("evidence").resolve()


def new_run_dir(handoff: dict[str, Any], root: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{handoff['runId']}-{stamp}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def _token_resolved_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    if not handoff.get("_resolvedClientCredential"):
        base = Path(handoff["_handoffPath"]).parent
        credential = Path(handoff["client"]["credentialFile"]).expanduser()
        if not credential.is_absolute():
            credential = base / credential
        handoff["_resolvedClientCredential"] = str(credential.resolve(strict=True))
    for site in handoff["sites"]:
        site.setdefault("_cluster", {"kubeconfig": site["kubeconfig"], "context": site["context"]})
    return handoff


def _startup_transports(handoff: dict[str, Any], evidence: Path) -> EndpointTransport:
    transport = EndpointTransport(handoff, evidence)
    try:
        for site in handoff["sites"]:
            transport.start_site_forwards(site)
            targets = site["simulator"].get("targets", [])
            # A per-Pod local forward is needed to snapshot and mutate every
            # simulator replica independently. Avoid also starting an aggregate
            # simulator forward when those per-Pod forwards are present.
            if not any(target.get("portForward") for target in targets):
                cfg = site["simulator"].get("portForward")
                if cfg:
                    instance = PortForward(site, cfg, evidence / "port-forwards" / f"{site['name']}-simulator.log")
                    instance.start()
                    transport.forwards.append(instance)
            for index, target in enumerate(targets):
                cfg = target.get("portForward")
                if cfg:
                    target_site = dict(site)
                    target_site["simulator"] = target
                    instance = PortForward(target_site, cfg, evidence / "port-forwards" / f"{site['name']}-sim-pod-{index}.log")
                    instance.start()
                    transport.forwards.append(instance)
                    if target.get("adminUrl"):
                        target["adminUrl"] = _replace_url_port(str(target["adminUrl"]), int(cfg["localPort"]))
        client = handoff["client"]
        if client.get("portForward"):
            site = next(item for item in handoff["sites"] if item["name"] == client["site"])
            instance = PortForward(site, client["portForward"], evidence / "port-forwards" / "client-gateway.log")
            instance.start()
            transport.forwards.append(instance)
            client["url"] = _replace_url_port(str(client["url"]), int(client["portForward"]["localPort"]))
            if client.get("gatewayUrl"):
                client["gatewayUrl"] = _replace_url_port(str(client["gatewayUrl"]), int(client["portForward"]["localPort"]))
        reference = handoff.get("backendReference", {})
        if reference.get("portForward"):
            site = next(item for item in handoff["sites"] if item["name"] == reference.get("site"))
            cfg = reference["portForward"]
            cfg.setdefault("namespace", site["namespace"])
            instance = PortForward(site, cfg, evidence / "port-forwards" / "direct-provider-reference.log")
            instance.start()
            transport.forwards.append(instance)
            reference["url"] = _replace_url_port(str(reference["url"]), int(cfg["localPort"]))
    except Exception:
        transport.close()
        raise
    return transport


def _required_tools() -> dict[str, Any]:
    names = ("kubectl", "kind", "docker")
    tools = {name: shutil.which(name) for name in names}
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise BenchmarkFailure(f"required tools are missing: {', '.join(missing)}")
    expected = (Path(__file__).resolve().parents[2] / "tools" / "k6-version").read_text(encoding="utf-8").strip()
    binary, actual = verify_k6_version(expected)
    tools["k6"] = binary
    tools["k6Version"] = actual
    return tools


def _host_capacity() -> dict[str, Any]:
    mem_total = mem_available = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw, *_ = line.split()
            if key == "MemTotal:":
                mem_total = int(raw) * 1024
            elif key == "MemAvailable:":
                mem_available = int(raw) * 1024
    except OSError:
        pass
    disk = shutil.disk_usage(Path.cwd())
    load = None
    try:
        load = [float(value) for value in Path("/proc/loadavg").read_text(encoding="ascii").split()[:3]]
    except (OSError, ValueError):
        pass
    return {
        "clientLocation": "local Gridzilla host (not a Kubernetes Pod)",
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpuCount": os.cpu_count(),
        "loadAverage1m5m15m": load,
        "memoryTotalBytes": mem_total,
        "memoryAvailableBytes": mem_available,
        "diskFreeBytes": disk.free,
    }


def _profile_host_check(host: dict[str, Any], profile: Profile) -> dict[str, Any]:
    free = host.get("diskFreeBytes")
    enough_disk = free is None or free >= profile.max_evidence_bytes
    load = host.get("loadAverage1m5m15m") or []
    cpus = host.get("cpuCount")
    saturated = bool(load and cpus and load[0] > cpus)
    minimum_mem = int(profile.max_vus * 128 * 1024 * 1024)
    available = host.get("memoryAvailableBytes")
    enough_mem = available is None or available >= minimum_mem
    return {"diskFreeBytes": free, "maxEvidenceBytes": profile.max_evidence_bytes,
            "evidenceDiskSpaceAvailable": enough_disk, "loadGeneratorAlreadySaturated": saturated,
            "estimatedGeneratorMemoryBytes": minimum_mem, "memoryAvailableBytes": available,
            "sufficientMemory": enough_mem,
            "suitableForThroughputClaim": enough_disk and enough_mem and not saturated,
            "cpuCount": cpus, "loadAverage1m5m15m": load,
            "interpretation": "Existing host saturation is recorded and invalidates throughput claims; Gridzilla does not apply a latency threshold by default."}


def _host_resource_sample() -> dict[str, Any]:
    data: dict[str, Any] = {"observedAtUtc": utc_now(), "monotonicNs": time.monotonic_ns(), "host": _host_capacity()}
    try:
        lines = Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]
        network = {}
        for line in lines:
            name, values = line.split(":", 1)
            numbers = [int(value) for value in values.split()]
            network[name.strip()] = {"rxBytes": numbers[0], "rxPackets": numbers[1], "txBytes": numbers[8], "txPackets": numbers[9]}
        data["hostNetworkCounters"] = network
    except (OSError, ValueError, IndexError):
        data["hostNetworkCounters"] = None
    try:
        cpu_line = next(line for line in Path("/proc/stat").read_text(encoding="ascii").splitlines() if line.startswith("cpu "))
        data["hostCpuCounters"] = [int(value) for value in cpu_line.split()[1:]]
    except (OSError, ValueError, StopIteration):
        data["hostCpuCounters"] = None
    load = (data.get("host") or {}).get("loadAverage1m5m15m")
    cpus = (data.get("host") or {}).get("cpuCount")
    data["loadGeneratorSaturated"] = bool(load and cpus and load[0] > cpus)
    return data


def _parse_kubectl_top(text: str) -> list[dict[str, Any]]:
    def cpu(value: str) -> float | None:
        try:
            return float(value[:-1]) / 1000 if value.endswith("m") else float(value)
        except ValueError:
            return None

    def memory(value: str) -> int | None:
        units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
                 "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
        for suffix, multiplier in units.items():
            if value.endswith(suffix):
                try:
                    return int(float(value[:-len(suffix)]) * multiplier)
                except ValueError:
                    return None
        try:
            return int(value)
        except ValueError:
            return None

    result = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        result.append({"pod": fields[0], "container": fields[1],
                       "cpuCores": cpu(fields[2]), "memoryBytes": memory(fields[3])})
    return result


def _cluster_resources(site: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"site": site["name"], "clusterName": site["clusterName"]}
    try:
        nodes = json.loads(kubectl(site, ["get", "nodes", "-o", "json"]))
        result["nodePressure"] = []
        for node in nodes.get("items", []):
            conditions = node.get("status", {}).get("conditions", [])
            result["nodePressure"].append({
                "node": node.get("metadata", {}).get("name"),
                "conditions": [{"type": item.get("type"), "status": item.get("status"), "reason": item.get("reason")}
                               for item in conditions if item.get("type") in {"MemoryPressure", "DiskPressure", "PIDPressure", "Ready"}],
            })
    except Exception as exc:
        result["nodePressureError"] = str(exc)
    try:
        top = __import__("gridzilla.runtime", fromlist=["run_command"]).run_command(
            ["kubectl", "--kubeconfig", site["_cluster"]["kubeconfig"], "--context", site["_cluster"]["context"],
             "top", "pods", "-A", "--containers"], timeout=15)
        result["podUsageRaw"] = top.stdout.decode(errors="replace") if top.returncode == 0 else None
        result["podUsageError"] = top.stderr.decode(errors="replace")[:500] if top.returncode else None
    except Exception as exc:
        result["podUsageError"] = str(exc)
    result["podRestarts"] = {}
    result["componentUsage"] = {}
    result["runtimeImageIdentity"] = {}
    for lock in site.get("runtimeImages", []):
        try:
            snapshot = runtime_image_snapshot(site, lock)
            result["podRestarts"][lock["component"]] = [{"pod": p["pod"], "restarts": p["restarts"], "ready": p["ready"]} for p in snapshot["pods"]]
            result["runtimeImageIdentity"][lock["component"]] = snapshot
            top = __import__("gridzilla.runtime", fromlist=["run_command"]).run_command(
                ["kubectl", "--kubeconfig", site["_cluster"]["kubeconfig"], "--context", site["_cluster"]["context"],
                 "-n", lock["namespace"], "top", "pods", "-l", lock["selector"], "--containers"], timeout=15)
            top_text = top.stdout.decode(errors="replace") if top.returncode == 0 else None
            usage = _parse_kubectl_top(top_text) if top_text else []
            pod_network = {}
            for pod in snapshot["pods"]:
                if not pod.get("pod"):
                    continue
                counters = {}
                for direction in ("rx_bytes", "tx_bytes"):
                    try:
                        output = kubectl(site, ["-n", lock["namespace"], "exec", pod["pod"], "-c", lock["container"],
                                                "--", "cat", f"/sys/class/net/eth0/statistics/{direction}"], timeout=4)
                        counters[direction] = int(output.decode().strip())
                    except Exception as exc:
                        counters[direction] = {"unavailable": str(exc)[:180]}
                pod_network[pod["pod"]] = counters
            result["componentUsage"][lock["component"]] = {
                "raw": top_text, "pods": usage, "networkCounters": pod_network,
                "error": top.stderr.decode(errors="replace")[:300] if top.returncode else None,
            }
        except Exception as exc:
            result["podRestarts"][lock.get("component", "unknown")] = {"error": str(exc)}
    return result


def doctor(handoff: dict[str, Any], *, profile: Profile | None = None,
           evidence: Path | None = None, transport: EndpointTransport | None = None) -> dict[str, Any]:
    handoff = _token_resolved_handoff(handoff)
    result: dict[str, Any] = {
        "schemaVersion": 1, "kind": "gridzilla-doctor", "observedAtUtc": utc_now(),
        "runId": handoff["runId"], "source": handoff["source"], "ok": True, "problems": [],
        "tools": {}, "host": _host_capacity(), "sites": [],
    }
    try:
        result["tools"] = _required_tools()
    except Exception as exc:
        result["ok"] = False
        result["problems"].append(str(exc))
    missing = verify_exact_kind_ownership(handoff)
    if missing:
        result["ok"] = False
        result["problems"].append(f"run-owned Kind clusters missing: {', '.join(missing)}")
    own_transport = transport is None
    transport = transport or _startup_transports(handoff, evidence or Path("/tmp/gridzilla-doctor"))
    try:
        image_lock = json.loads(Path(handoff["_resolvedImageLock"]).read_text(encoding="utf-8"))
        host_images = {item.get("reference"): item for item in image_lock.get("images", []) if isinstance(item, dict)}
        image_provenance = []
        for image in handoff["runtimeImages"]:
            site = next((x for x in handoff["sites"] if x["name"] == image["site"]), None)
            if site is None:
                result["ok"] = False
                result["problems"].append(f"runtime image {image['component']} refers to unknown site {image['site']}")
                continue
            observed = runtime_image_snapshot(site, image)
            if not observed["ready"]:
                result["ok"] = False
                result["problems"].append(f"{image['site']}/{image['component']} imageID does not match expected digest or Pod is not ready")
            result.setdefault("runtimeImages", []).append(observed)
            host = host_images.get(image["reference"], {})
            host_source = host.get("sourceRevision")
            runtime_source = image.get("sourceRevision")
            if host_source and runtime_source and host_source != runtime_source:
                result["ok"] = False
                result["problems"].append(f"{image['site']}/{image['component']} runtime source revision disagrees with the preloaded host image lock")
            image_provenance.append({"site": image["site"], "component": image["component"],
                                     "reference": image["reference"], "hostImageId": host.get("imageId"),
                                     "hostSourceRevision": host_source, "runtimeSourceRevision": runtime_source,
                                     "sourceRevisionKnown": bool(host_source or runtime_source)})
        result["imageProvenance"] = image_provenance
        result["unknownImageSourceProvenance"] = [item for item in image_provenance if not item["sourceRevisionKnown"]]
        gate = gate_snapshot(handoff, transport, max_age_seconds=30)
        result["readiness"] = gate
        if not gate["ok"]:
            result["ok"] = False
            result["problems"].extend(gate["problems"])
        result["endpointReachability"] = _endpoint_reachability(handoff)
        if not result["endpointReachability"]["ok"]:
            result["ok"] = False
            result["problems"].extend(result["endpointReachability"]["problems"])
    finally:
        if own_transport:
            transport.close()
    if profile:
        result["profile"] = profile.raw
        capacity = _profile_host_check(result["host"], profile)
        result["hostCapacityCheck"] = capacity
        if not capacity["sufficientMemory"]:
            result["ok"] = False
            result["problems"].append("local load-generator host lacks the profile's estimated memory headroom")
        if not capacity["evidenceDiskSpaceAvailable"]:
            result["ok"] = False
            result["problems"].append("local filesystem has less free space than the profile evidence bound")
        if capacity["loadGeneratorAlreadySaturated"]:
            result["ok"] = False
            result["problems"].append("local load-generator host is already CPU-saturated; refusing a throughput run")
        targets = [site for site in handoff["sites"] if site["provider"].get("pressureTarget") is True]
        if len(targets) != 1 or targets[0]["provider"].get("pool") != profile.pressure_target_pool:
            result["ok"] = False
            result["problems"].append(f"handoff must mark exactly one {profile.pressure_target_pool} as pressureTarget")
        else:
            client_site = next((item for item in (result.get("readiness") or {}).get("sites", [])
                                if item.get("name") == handoff["client"]["site"]), None)
            target_name = targets[0]["name"]
            top = (client_site or {}).get("overlay", {}).get("topCandidate") or {}
            expected_top = _expected_site(top, handoff["client"]["attribution"])
            result["pressureTargetCheck"] = {"site": target_name, "pool": targets[0]["provider"].get("pool"),
                                              "westGatewayRank0Site": expected_top,
                                              "isCurrentRank0": expected_top == target_name}
            if expected_top != target_name:
                result["ok"] = False
                result["problems"].append("Pool A pressure target is not the current west Gateway rank-0 provider")
    return result


def _source_identity() -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[2]
    def git_value(*args: str) -> str | None:
        try:
            completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                       text=True, timeout=5, check=False)
            return completed.stdout.strip() if completed.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    excluded = {".git", "node_modules", "web", "evidence", ".venv", "__pycache__", "test-results"}
    digest = hashlib.sha256()
    files = 0
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or any(part in excluded for part in path.relative_to(repo).parts):
            continue
        try:
            relative = path.relative_to(repo).as_posix()
            data = path.read_bytes()
        except OSError:
            continue
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(data)
        digest.update(b"\0")
        files += 1
    return {"repository": "nerdalert/gridzilla", "branch": git_value("branch", "--show-current"),
            "head": git_value("rev-parse", "HEAD"), "worktreeStatus": git_value("status", "--porcelain"),
            "sourceFingerprintSha256": digest.hexdigest(), "sourceFileCount": files}


def _pressure_target(handoff: dict[str, Any], profile: Profile) -> dict[str, Any]:
    marked = [site for site in handoff["sites"] if site["provider"].get("pressureTarget") is True]
    if len(marked) != 1:
        raise BenchmarkFailure("handoff must mark exactly one pressureTarget provider")
    site = marked[0]
    if site["provider"].get("pool") != profile.pressure_target_pool:
        raise BenchmarkFailure(f"pressure target must identify {profile.pressure_target_pool}; got {site['provider'].get('pool')!r}")
    return site


def _endpoint_reachability(handoff: dict[str, Any]) -> dict[str, Any]:
    endpoints: list[tuple[str, str]] = [("gateway", handoff["client"]["url"])]
    endpoint_configs: dict[str, dict[str, Any]] = {handoff["client"]["url"]: handoff["client"]}
    endpoints.extend((f"epp:{site['name']}", site["epp"]["metricsUrl"]) for site in handoff["sites"])
    endpoints.extend((f"simulator-admin:{site['name']}", site["simulator"]["adminUrl"]) for site in handoff["sites"])
    for site in handoff["sites"]:
        endpoint_configs[site["epp"]["metricsUrl"]] = site["epp"]
        endpoint_configs[site["simulator"]["adminUrl"]] = site["simulator"]
        for target in site["simulator"].get("targets", []):
            if target.get("adminUrl"):
                endpoint_configs[target["adminUrl"]] = target
    reference = handoff.get("backendReference", {})
    if reference.get("url"):
        endpoints.append(("direct-provider-reference", reference["url"]))
        endpoint_configs[reference["url"]] = reference
    checks = []
    problems = []
    for name, url in endpoints:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            checks.append({"name": name, "host": host, "port": port, "dns": "resolved"})
        except OSError as exc:
            endpoint = endpoint_configs.get(url, {})
            mapping = endpoint.get("resolve", {}).get(host)
            pf = endpoint.get("portForward")
            if mapping or pf:
                checks.append({"name": name, "host": host, "port": port, "dns": "mapped/port-forward"})
            else:
                checks.append({"name": name, "host": host, "port": port, "error": str(exc)})
                problems.append(f"{name} endpoint is not resolvable and has no run-owned host mapping")
    return {"ok": not problems, "checks": checks, "problems": problems}


def send_probe(handoff: dict[str, Any], transport: EndpointTransport, *, evidence_dir: Path,
               sequence: int, expected_site: str | None = None, label: str = "probe",
               response_limit_bytes: int = 256 * 1024) -> dict[str, Any]:
    client = handoff["client"]
    client_site = next(site for site in handoff["sites"] if site["name"] == client["site"])
    request_id = f"gz-{handoff['runId']}-{label}-{sequence}-{uuid.uuid4().hex[:12]}"
    body = json.loads(json.dumps(client["requestBody"]))
    token_field = client.get("outputTokenField", "max_tokens")
    body[token_field] = min(int(body.get(token_field, 32)), int(client.get("smokeMaxOutputTokens", 32)))
    headers = {
        "Content-Type": "application/json",
        client["authHeader"]: "Bearer " + Path(handoff["_resolvedClientCredential"]).read_text(encoding="utf-8").strip(),
    }
    for header in client.get("requestIdHeaders", [client["requestIdHeader"]]):
        headers[str(header)] = request_id
    url = __import__("urllib.parse", fromlist=["urljoin"]).urljoin(client["url"].rstrip("/") + "/", client["path"].lstrip("/"))
    start = monotonic_utc_pair()
    response = transport.request(client_site, client, "POST", url, body=json.dumps(body).encode(),
                                 headers=headers, timeout=20, ca_file=client.get("caFile"))
    end = monotonic_utc_pair()
    attribution = client["attribution"]
    response_headers = response["headers"]
    provider_gateway_header = attribution.get("providerGatewayHeader")
    facts = {
        "provider": response_headers.get(attribution["providerHeader"].lower()),
        "site": response_headers.get(attribution["siteHeader"].lower()),
        "pool": response_headers.get(attribution["poolHeader"].lower()) if attribution.get("poolHeader") else None,
        "backend": response_headers.get(attribution["backendHeader"].lower()),
        "providerGateway": response_headers.get(provider_gateway_header.lower()) if provider_gateway_header else None,
        "backendPod": response_headers.get(attribution["backendPodHeader"].lower()),
        "backendRequestId": response_headers.get(attribution["requestIdEchoHeader"].lower()),
    }
    problems = []
    allowed_providers = attribution.get("allowedProviders", attribution.get("allowedSites", []))
    if response["status"] < 200 or response["status"] >= 300:
        problems.append(f"Gateway returned HTTP {response['status']}")
    if facts["provider"] not in allowed_providers:
        problems.append("provider attribution missing or outside allowed set")
    if facts["site"] not in attribution["allowedSites"]:
        problems.append("site attribution missing or outside allowed set")
    if facts["pool"] is None:
        facts["pool"] = attribution.get("providerToPool", {}).get(facts["site"])
        facts["poolSource"] = "site-specific provider identity in handoff; response has no pool header"
    if facts["pool"] not in attribution["allowedPools"]:
        problems.append("pool attribution missing or outside allowed set")
    if facts["backend"] not in attribution["allowedBackends"]:
        problems.append("backend attribution missing or outside allowed set")
    expected_backend = attribution.get("providerToBackend", {}).get(facts["provider"])
    if expected_backend and facts["backend"] != expected_backend:
        problems.append("backend header does not match the selected provider's handoff identity")
    expected_pod_prefix = attribution.get("backendPodPrefixBySite", {}).get(facts["site"])
    if not facts["backendPod"] or not expected_pod_prefix or not facts["backendPod"].startswith(expected_pod_prefix):
        problems.append("backend Pod receipt does not match the selected site")
    if facts["backendRequestId"] != request_id:
        problems.append("simulator request-ID receipt does not match this request")
    if provider_gateway_header and facts["providerGateway"] != facts["site"]:
        problems.append("provider-gateway response attribution does not match Grid-selected site")
    for header in attribution.get("requiredResponseHeaders", []):
        if not response_headers.get(str(header).lower()):
            problems.append(f"required request-attribution header {header} is missing")
    if expected_site and facts["site"] != expected_site:
        problems.append(f"request selected {facts['site']}, expected current rank-0 site {expected_site}")
    if len(response["body"]) > response_limit_bytes:
        problems.append(f"correlated probe response exceeded configured {response_limit_bytes}-byte limit")
    receipt = _check_receipt(handoff, transport, request_id, response_headers, facts["site"])
    if receipt.get("available", True) and not receipt.get("found"):
        problems.append("independent backend receipt not found")
    record = {
        "schemaVersion": 1, "type": "correlated-probe", "label": label, "requestId": request_id,
        "started": start, "finished": end,
        "durationMs": (end["monotonicNs"] - start["monotonicNs"]) / 1e6,
        "status": response["status"], "responseBytes": len(response["body"]),
        "responseSha256": hashlib.sha256(response["body"]).hexdigest(),
        "attribution": facts, "receipt": receipt, "expectedSite": expected_site,
        "ok": not problems, "problems": problems,
    }
    append_jsonl(evidence_dir / "probes.jsonl", record)
    if problems:
        raise BenchmarkFailure("correlated request failed: " + "; ".join(problems))
    return record


def _check_receipt(handoff: dict[str, Any], transport: EndpointTransport, request_id: str,
                   response_headers: dict[str, str], provider_site: str) -> dict[str, Any]:
    receipt = handoff["client"]["receipt"]
    if not receipt.get("available", True):
        return {"available": False, "independent": False,
                "reason": receipt.get("reason", "no backend receipt endpoint is exposed by this environment")}
    if receipt.get("type") == "response_headers":
        echoed_id = response_headers.get(str(receipt["requestIdHeader"]).lower())
        backend_pod = response_headers.get(str(receipt["backendPodHeader"]).lower())
        pod_prefix = receipt["backendPodPrefixBySite"].get(provider_site)
        matched = echoed_id == request_id and bool(pod_prefix) and bool(backend_pod) and backend_pod.startswith(pod_prefix)
        return {
            "available": True, "independent": True, "type": "simulator-response-headers",
            "found": matched, "requestIdMatched": echoed_id == request_id,
            "backendPod": backend_pod, "providerSite": provider_site,
        }
    url = receipt["urlTemplate"].replace("{request_id}", quote(request_id, safe=""))
    site = next(s for s in handoff["sites"] if s["name"] == handoff["client"]["site"])
    token_file = receipt.get("_resolvedCredentialFile", receipt.get("credentialFile"))
    try:
        # Receipt propagation may lag the Gateway response. Retry only this
        # read-only lookup; the inference request itself is never retried.
        last_status = None
        for attempt in range(5):
            response = transport.request(site, receipt, "GET", url, timeout=3, token_file=token_file,
                                         ca_file=receipt.get("caFile"))
            last_status = response["status"]
            if response["status"] == 200:
                value = json.loads(response["body"])
                expected_id = value.get(receipt.get("requestIdField", "requestId"), value.get("request_id"))
                backend = value.get(receipt.get("backendField", "backend"), value.get("provider"))
                if expected_id == request_id:
                    return {"found": True, "requestIdMatched": True, "backend": backend,
                            "httpStatus": 200, "receiptAttempt": attempt + 1}
            if attempt < 4:
                time.sleep(0.2)
        return {"found": False, "httpStatus": last_status}
    except Exception as exc:
        return {"found": False, "error": str(exc)[:300]}


def smoke(handoff: dict[str, Any], *, evidence_dir: Path | None = None,
          root: Path | None = None, count: int = 3,
          response_limit_bytes: int = 256 * 1024,
          transport: EndpointTransport | None = None) -> dict[str, Any]:
    handoff = _token_resolved_handoff(handoff)
    if not (1 <= count <= 10):
        raise ValidationError("smoke request count must be 1-10")
    directory = evidence_dir or new_run_dir(handoff, evidence_root(handoff, root))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Preserve file paths, but never serialize token values or body contents.
    from .models import redacted_handoff
    write_json(directory / "handoff.redacted.json", redacted_handoff(handoff))
    owns_transport = transport is None
    try:
        if transport is None:
            transport = _startup_transports(handoff, directory)
            write_json(directory / "handoff.redacted.json", redacted_handoff(handoff))
        gate = gate_snapshot(handoff, transport, max_age_seconds=30)
        write_json(directory / "smoke-gates-before.json", gate)
        if not gate["ok"]:
            raise BenchmarkFailure("readiness gates failed before smoke: " + "; ".join(gate["problems"]))
        probes = [send_probe(handoff, transport, evidence_dir=directory, sequence=i, label="smoke",
                             response_limit_bytes=response_limit_bytes) for i in range(1, count + 1)]
        gate_after = gate_snapshot(handoff, transport, max_age_seconds=30)
        write_json(directory / "smoke-gates-after.json", gate_after)
        if not gate_after["ok"]:
            raise BenchmarkFailure("readiness gates failed after smoke: " + "; ".join(gate_after["problems"]))
        result = {"schemaVersion": 1, "type": "smoke", "runId": handoff["runId"], "ok": True,
                  "probeCount": len(probes), "probes": probes, "before": gate, "after": gate_after}
        write_json(directory / "smoke-result.json", result)
        return result
    except Exception as exc:
        write_json(directory / "failure.json", {"observedAtUtc": utc_now(), "type": "smoke", "error": str(exc)})
        raise
    finally:
        if owns_transport and transport is not None:
            transport.close()


class StopRequested(Exception):
    """SIGINT/SIGTERM converted into a guaranteed restoration path."""


class SignalRestorer:
    def __init__(self):
        self.received: int | None = None
        self.previous: dict[int, Any] = {}

    def __enter__(self) -> "SignalRestorer":
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        return self

    def __exit__(self, *_: Any) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)

    def _handle(self, signum: int, _frame: Any) -> None:
        self.received = signum
        raise StopRequested(f"received {signal.Signals(signum).name}")


def _sim_targets(site: dict[str, Any]) -> list[dict[str, Any]]:
    simulator = site["simulator"]
    targets = simulator.get("targets")
    if not isinstance(targets, list) or not targets:
        raise BenchmarkFailure(f"{site['name']}: handoff must list every simulator Pod target")
    return targets


def _sim_target_config(site: dict[str, Any], target: dict[str, Any], transport: EndpointTransport) -> dict[str, Any]:
    endpoint = dict(target)
    endpoint.setdefault("adminUrl", site["simulator"]["adminUrl"])
    endpoint.setdefault("credentialFile", site["simulator"].get("credentialFile"))
    return simulator_config({**site, "simulator": endpoint}, transport)


def _fake_metric_snapshot(config: dict[str, Any], pointer: str) -> tuple[list[str], Any]:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.strip("/").split("/")]
    if len(parts) != 2 or parts[0] != "fake-metrics":
        raise BenchmarkFailure("pressure pointer must identify one scalar fake-metrics field")
    metric = parts[1]
    fake = config.get("fake-metrics", config.get("fake_metrics"))
    if not isinstance(fake, dict):
        raise BenchmarkFailure("simulator is not in fake-metrics mode; refusing to switch modes")
    if metric not in fake:
        raise BenchmarkFailure(f"simulator GET omitted fake-metrics.{metric}; cannot prove restore value")
    return ["fake-metrics", metric], fake[metric]


def _fake_metric_patch(pointer: str, value: Any) -> dict[str, Any]:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.strip("/").split("/")]
    if len(parts) != 2 or parts[0] != "fake-metrics":
        raise BenchmarkFailure("only fake-metrics scalar fields may be changed by Gridzilla")
    return {"fake-metrics": {parts[1]: value}}


def _verify_sim_pod_identity(site: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    pod = target["podName"]
    namespace = target.get("namespace", site["namespace"])
    obj = json.loads(kubectl(site, ["-n", namespace, "get", "pod", pod, "-o", "json"]))
    uid = obj.get("metadata", {}).get("uid")
    expected_uid = target["podUID"]
    phase = obj.get("status", {}).get("phase")
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in obj.get("status", {}).get("conditions", []))
    if uid != expected_uid or phase != "Running" or not ready:
        raise BenchmarkFailure(f"{site['name']}: simulator Pod {pod} identity/readiness changed (uid {uid}, expected {expected_uid})")
    deployment_name = str(target.get("deploymentName", f"vcr-inference-{site['name']}"))
    deployment = json.loads(kubectl(site, ["-n", namespace, "get", "deployment", deployment_name, "-o", "json"]))
    generation = deployment.get("metadata", {}).get("generation")
    observed_generation = deployment.get("status", {}).get("observedGeneration")
    expected_generation = target.get("configGeneration")
    if generation != expected_generation or observed_generation != generation:
        raise BenchmarkFailure(f"{site['name']}: simulator Deployment generation changed or is unobserved ({generation}/{observed_generation}, expected {expected_generation})")
    return {"podName": pod, "podUID": uid, "resourceVersion": obj.get("metadata", {}).get("resourceVersion"),
            "phase": phase, "ready": ready, "deployment": deployment_name,
            "configGeneration": generation, "observedGeneration": observed_generation}


def snapshot_simulators(handoff: dict[str, Any], transport: EndpointTransport, evidence_dir: Path,
                        *, pressure_site: str) -> dict[str, Any]:
    snapshots_by_site: dict[str, list[dict[str, Any]]] = {}
    for site in handoff["sites"]:
        snapshots = []
        for target in _sim_targets(site):
            identity = _verify_sim_pod_identity(site, target)
            config = _sim_target_config(site, target, transport)
            pointer = site["simulator"]["pressureJsonPointer"]
            metric_path, metric_value = _fake_metric_snapshot(config, pointer)
            snapshots.append({"site": site["name"], "pod": identity, "config": config,
                              "metricPath": metric_path, "originalMetricValue": metric_value,
                              "target": {k: v for k, v in target.items() if not k.startswith("_") and k not in {"credentialFile", "token"}}})
        snapshots_by_site[site["name"]] = snapshots
    if pressure_site not in snapshots_by_site:
        raise BenchmarkFailure(f"unknown pressure site {pressure_site}")
    snapshot = {"schemaVersion": 1, "runId": handoff["runId"], "capturedAtUtc": utc_now(),
                "pressureSite": pressure_site, "sites": snapshots_by_site}
    write_json(evidence_dir / "simulator-original.json", snapshot)
    return snapshot


def mutate_simulators(handoff: dict[str, Any], transport: EndpointTransport, snapshot: dict[str, Any], value: Any) -> list[dict[str, Any]]:
    site_name = snapshot["pressureSite"]
    site = next(item for item in handoff["sites"] if item["name"] == site_name)
    pointer = site["simulator"]["pressureJsonPointer"]
    if value is None:
        raise BenchmarkFailure("scalar fake metric cannot be reset to null; restore the captured value")
    results = []
    for saved in snapshot["sites"][site_name]:
        identity_before = _verify_sim_pod_identity(site, saved["target"])
        target = dict(saved["target"])
        target["adminUrl"] = saved["target"].get("adminUrl", site["simulator"]["adminUrl"])
        site_target = {**site, "simulator": target}
        response = post_simulator_config(site_target, transport, _fake_metric_patch(pointer, value))
        config_after = _sim_target_config(site, target, transport)
        metric_path, observed = _fake_metric_snapshot(config_after, pointer)
        identity_after = _verify_sim_pod_identity(site, saved["target"])
        if observed != value:
            raise BenchmarkFailure(f"{site['name']}/{identity_after['podName']}: simulator GET did not confirm requested fake metric")
        if identity_before["podUID"] != identity_after["podUID"]:
            raise BenchmarkFailure("simulator Pod UID changed during atomic pressure update")
        expected_config = set_json_pointer(saved["config"], "/".join(["", *metric_path]), value)
        if config_after != expected_config:
            raise BenchmarkFailure(f"{site['name']}/{identity_after['podName']}: simulator changed fields outside the requested fake-metric scalar")
        results.append({"pod": identity_after, "post": response, "metricPath": metric_path, "observedValue": observed})
    verify_untouched_simulators(handoff, transport, snapshot, except_site=site_name)
    return results


def verify_untouched_simulators(handoff: dict[str, Any], transport: EndpointTransport, snapshot: dict[str, Any],
                                *, except_site: str) -> None:
    for site in handoff["sites"]:
        if site["name"] == except_site:
            continue
        for saved in snapshot["sites"][site["name"]]:
            identity = _verify_sim_pod_identity(site, saved["target"])
            current = _sim_target_config(site, saved["target"], transport)
            if current != saved["config"]:
                raise BenchmarkFailure(f"{site['name']}/{identity['podName']}: simulator config changed outside Gridzilla's pressure target")


def _ready_gate(handoff: dict[str, Any], transport: EndpointTransport, profile: Profile) -> dict[str, Any]:
    gate = gate_snapshot(handoff, transport, max_age_seconds=duration_seconds(profile.freshness_limit, "freshness limit"))
    if not gate["ok"]:
        raise BenchmarkFailure("readiness gate failed: " + "; ".join(gate["problems"]))
    return gate


def _candidate_on_client_site(handoff: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    client_site = handoff["client"]["site"]
    observation = next(item for item in gate["sites"] if item["name"] == client_site)
    candidate = observation["overlay"].get("topCandidate")
    if not candidate:
        raise BenchmarkFailure(f"{client_site}: current overlay has no rank-0 candidate")
    return candidate


def _expected_site(candidate: dict[str, Any], attribution: dict[str, Any]) -> str | None:
    if candidate.get("site"):
        return candidate["site"]
    provider_map = attribution.get("providerToSite", {})
    return provider_map.get(candidate.get("key"))


def _candidate_for_provider(handoff: dict[str, Any], gate: dict[str, Any], site_name: str) -> dict[str, Any] | None:
    site_spec = next(item for item in handoff["sites"] if item["name"] == site_name)
    observation = next(item for item in gate["sites"] if item["name"] == site_name)
    return parse_overlay_candidate(observation["overlay"], "key", site_spec["provider"]["candidateValue"])


def _check_signal_gate(handoff: dict[str, Any], baseline: dict[str, Any], current: dict[str, Any],
                       pressured_site: str) -> tuple[bool, dict[str, Any]]:
    baseline_sites = {x["name"]: x for x in baseline["sites"]}
    current_sites = {x["name"]: x for x in current["sites"]}
    report: dict[str, Any] = {"sites": {}, "ok": True}
    for name in current_sites:
        old = baseline_sites[name]
        new = current_sites[name]
        old_q = [x["value"] for x in old["metrics"]["queue"]]
        new_q = [x["value"] for x in new["metrics"]["queue"]]
        old_candidate = parse_overlay_candidate(old["overlay"], "key", next(s["provider"]["candidateValue"] for s in handoff["sites"] if s["name"] == name))
        new_candidate = parse_overlay_candidate(new["overlay"], "key", next(s["provider"]["candidateValue"] for s in handoff["sites"] if s["name"] == name))
        changed_revision = old["overlay"]["revision"] != new["overlay"]["revision"]
        score_decreased = bool(old_candidate and new_candidate and new_candidate["score"] < old_candidate["score"])
        rank_worsened = bool(old_candidate and new_candidate and new_candidate["rank"] > old_candidate["rank"])
        queue_changed = bool(old_q and new_q and max(new_q) > max(old_q))
        site_ok = changed_revision and new["gateway"]["servingRevision"] == new["overlay"]["revision"]
        if name == pressured_site:
            site_ok = site_ok and queue_changed and score_decreased
        report["sites"][name] = {"queueChanged": queue_changed, "scoreDecreased": score_decreased,
                                  "rankWorsened": rank_worsened, "revisionChanged": changed_revision,
                                  "oldRevision": old["overlay"]["revision"], "newRevision": new["overlay"]["revision"],
                                  "oldQueue": old_q, "newQueue": new_q, "ok": site_ok}
        if not site_ok:
            report["ok"] = False
    old_top = _candidate_on_client_site(handoff, baseline)
    new_top = _candidate_on_client_site(handoff, current)
    report["selectionChanged"] = old_top["key"] != new_top["key"]
    report["baselineTop"] = old_top
    report["pressureTop"] = new_top
    report["ok"] = report["ok"] and report["selectionChanged"]
    return bool(report["ok"]), report


def wait_for_signal_transition(handoff: dict[str, Any], transport: EndpointTransport, profile: Profile,
                               baseline: dict[str, Any], pressured_site: str, *, timeout_seconds: int = 180,
                               evidence_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    started = monotonic_utc_pair()
    deadline = time.monotonic() + timeout_seconds
    polls = 0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        polls += 1
        try:
            current = gate_snapshot(handoff, transport, max_age_seconds=duration_seconds(profile.freshness_limit, "freshness limit"))
            last = current
            append_jsonl(evidence_dir / "readiness-timeline.jsonl", {"stage": "pressure-transition", "poll": polls, "snapshot": current})
            if current["ok"]:
                ok, facts = _check_signal_gate(handoff, baseline, current, pressured_site)
                facts.update({"poll": polls, "observedAtUtc": utc_now(), "monotonicNs": time.monotonic_ns()})
                append_jsonl(evidence_dir / "signal-boundaries.jsonl", {"boundary": "grid-overlay-and-gateway", **facts})
                if ok:
                    facts["started"] = started
                    facts["durationMs"] = (facts["monotonicNs"] - started["monotonicNs"]) / 1e6
                    return current, facts
        except Exception as exc:
            last = {"ok": False, "error": str(exc)}
        time.sleep(min(2.0, duration_seconds(profile.poll_interval, "poll interval")))
    raise BenchmarkFailure(f"signal/routing transition did not converge within {timeout_seconds}s: {json.dumps(last, default=str)[:1800]}")


def wait_for_recovery(handoff: dict[str, Any], transport: EndpointTransport, profile: Profile,
                      baseline: dict[str, Any], pressured_site: str, *, timeout_seconds: int = 180,
                      evidence_dir: Path, pressured: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    started = monotonic_utc_pair()
    deadline = time.monotonic() + timeout_seconds
    polls = 0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        polls += 1
        try:
            current = gate_snapshot(handoff, transport, max_age_seconds=duration_seconds(profile.freshness_limit, "freshness limit"))
            last = current
            append_jsonl(evidence_dir / "readiness-timeline.jsonl", {"stage": "recovery-transition", "poll": polls, "snapshot": current})
            if current["ok"]:
                sites = {x["name"]: x for x in current["sites"]}
                old_sites = {x["name"]: x for x in baseline["sites"]}
                recovered = True
                facts = {"sites": {}}
                for name, item in sites.items():
                    pressure_target = next(s for s in handoff["sites"] if s["name"] == name)
                    metric_vals = [x["value"] for x in item["metrics"]["queue"]]
                    old_vals = [x["value"] for x in old_sites[name]["metrics"]["queue"]]
                    queue_restored = bool(metric_vals and old_vals and abs(max(metric_vals) - max(old_vals)) < 0.01)
                    site_facts = {"queueRestored": queue_restored, "revision": item["overlay"]["revision"],
                                  "servingRevision": item["gateway"]["servingRevision"]}
                    facts["sites"][name] = site_facts
                    if name == pressured_site and not queue_restored:
                        recovered = False
                    if name == pressured_site:
                        baseline_candidate = _candidate_for_provider(handoff, baseline, name)
                        recovered_candidate = _candidate_for_provider(handoff, current, name)
                        score_restored = bool(baseline_candidate and recovered_candidate and
                                              abs(baseline_candidate["score"] - recovered_candidate["score"]) <= 1e-6)
                        rank_restored = bool(baseline_candidate and recovered_candidate and
                                             baseline_candidate["rank"] == recovered_candidate["rank"])
                        site_facts["scoreRestored"] = score_restored
                        site_facts["rankRestored"] = rank_restored
                        if not (score_restored and rank_restored):
                            recovered = False
                        if pressured is not None:
                            pressure_revision = next(item for item in pressured["sites"] if item["name"] == name)["overlay"]["revision"]
                            site_facts["pressureRevision"] = pressure_revision
                            site_facts["revisionLeftPressureState"] = item["overlay"]["revision"] != pressure_revision
                            if not site_facts["revisionLeftPressureState"]:
                                recovered = False
                current_top = _candidate_on_client_site(handoff, current)
                baseline_top = _candidate_on_client_site(handoff, baseline)
                facts["baselineTop"] = baseline_top
                facts["recoveryTop"] = current_top
                facts["selectionRestored"] = current_top["key"] == baseline_top["key"]
                facts["poll"] = polls
                facts["observedAtUtc"] = utc_now()
                facts["monotonicNs"] = time.monotonic_ns()
                facts["durationMs"] = (facts["monotonicNs"] - started["monotonicNs"]) / 1e6
                if recovered and facts["selectionRestored"]:
                    facts["started"] = started
                    append_jsonl(evidence_dir / "signal-boundaries.jsonl", {"boundary": "recovery-complete", **facts})
                    return current, facts
        except Exception as exc:
            last = {"ok": False, "error": str(exc)}
        time.sleep(min(2.0, duration_seconds(profile.poll_interval, "poll interval")))
    raise BenchmarkFailure(f"simulator/EPP/Grid/Gateway recovery did not converge: {json.dumps(last, default=str)[:1800]}")


def run_traffic_phase(handoff: dict[str, Any], profile: Profile, transport: EndpointTransport,
                      evidence_dir: Path, *, phase: str, rates: list[float],
                      repeats: int, duration: str, warmup: str,
                      stop_event: threading.Event, reference: bool = False,
                      on_progress: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    sequence = 0
    completed = 0

    def progress(action: str, *, index: str, rate: float, window_phase: str,
                 repeat: int | None = None) -> None:
        if on_progress:
            on_progress({"action": action, "index": index, "phase": window_phase,
                         "ratePerSecond": rate, "duration": warmup if action == "warmup-start" else duration,
                         "repeat": repeat, "reference": reference, "completedInPhase": completed})

    for rate in rates:
        if warmup:
            sequence += 1
            _ready_gate(handoff, transport, profile)
            warmup_index = f"{sequence:03d}-{phase}-warmup"
            progress("warmup-start", index=warmup_index, rate=rate, window_phase=f"{phase}-warmup")
            warmup_result = _run_window_recording_failure(handoff, profile, transport, evidence_dir,
                                                          phase=f"{phase}-warmup", rate=rate, duration=warmup,
                                                          index=warmup_index, stop_event=stop_event,
                                                          reference=reference)
            warmup_result["type"] = "warmup"
            append_jsonl(evidence_dir / "windows.jsonl", warmup_result)
            completed += 1
            progress("window-complete", index=warmup_index, rate=rate, window_phase=f"{phase}-warmup")
        for repeat in range(1, repeats + 1):
            sequence += 1
            _ready_gate(handoff, transport, profile)
            index = f"{sequence:03d}-{phase}-r{repeat}"
            progress("measurement-start", index=index, rate=rate, window_phase=phase, repeat=repeat)
            result = _run_window_recording_failure(handoff, profile, transport, evidence_dir,
                                                   phase=phase, rate=rate, duration=duration,
                                                   index=index, stop_event=stop_event,
                                                   reference=reference)
            result["type"] = "measurement"
            result["repeat"] = repeat
            result["ratePerSecond"] = rate
            raw_path = evidence_dir / result["rawPath"] if result.get("rawPath") else Path()
            result["accounting"] = summarize_raw(raw_path, result.get("summary", {}),
                                                  minimum_samples=profile.percentile_minimum_samples,
                                                  reference=bool(result.get("reference")))
            windows.append(result)
            append_jsonl(evidence_dir / "windows.jsonl", result)
            completed += 1
            progress("window-complete", index=index, rate=rate, window_phase=phase, repeat=repeat)
    return windows


def _run_window_recording_failure(handoff: dict[str, Any], profile: Profile, transport: EndpointTransport,
                                 evidence_dir: Path, **kwargs: Any) -> dict[str, Any]:
    try:
        return _run_one_window(handoff, profile, transport, evidence_dir, **kwargs)
    except Exception as exc:
        index = str(kwargs.get("index", "unknown"))
        result_path = evidence_dir / f"{index}-window-result.json"
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result = {"index": index, "phase": kwargs.get("phase"), "failure": str(exc)}
        else:
            result = {"index": index, "phase": kwargs.get("phase"), "failure": str(exc), "exitCode": None}
        result["type"] = "failed-window"
        result["failure"] = str(exc)
        result["accounting"] = summarize_raw(evidence_dir / f"{index}-raw.jsonl",
                                              result.get("summary") or {},
                                              minimum_samples=profile.percentile_minimum_samples,
                                              reference=bool(result.get("reference")))
        append_jsonl(evidence_dir / "windows.jsonl", result)
        raise


def _run_one_window(handoff: dict[str, Any], profile: Profile, transport: EndpointTransport,
                    evidence_dir: Path, *, phase: str, rate: float, duration: str,
                    index: str, stop_event: threading.Event, reference: bool = False) -> dict[str, Any]:
    last_poll = 0.0
    last_cluster_resource_sample = 0.0
    cluster_resource_sample_interval = 10.0
    last_probe = time.monotonic()
    probe_sequence = 0
    poll_seconds = max(1.0, float(duration_seconds(profile.poll_interval, "poll interval")))

    def monitor() -> str | None:
        nonlocal last_poll, last_probe, last_cluster_resource_sample, probe_sequence
        now = time.monotonic()
        if now - last_poll < poll_seconds:
            return None
        last_poll = now
        if _directory_size(evidence_dir) > profile.max_evidence_bytes:
            return "evidence directory reached the profile's maxEvidenceBytes bound"
        sample = _host_resource_sample()
        sample["phase"] = phase
        sample["clusterResourceSampleIntervalSeconds"] = cluster_resource_sample_interval
        if sample["loadGeneratorSaturated"]:
            sample["gateFailure"] = "local load-generator host is CPU-saturated"
            append_jsonl(evidence_dir / "resource-samples.jsonl", sample)
            return "local load-generator saturation invalidates the throughput window"
        site_samples = []
        try:
            if now - last_cluster_resource_sample >= cluster_resource_sample_interval:
                for site in handoff["sites"]:
                    site_samples.append(_cluster_resources(site))
                sample["clusters"] = site_samples
                sample["clusterResourceSampleAgeSeconds"] = 0
                last_cluster_resource_sample = now
            else:
                sample["clusterResourceSampleAgeSeconds"] = now - last_cluster_resource_sample
            gate = gate_snapshot(handoff, transport, max_age_seconds=duration_seconds(profile.freshness_limit, "freshness limit"))
            append_jsonl(evidence_dir / "readiness-timeline.jsonl", {"stage": phase, "snapshot": gate})
            if not gate["ok"]:
                sample["gateFailure"] = gate["problems"]
                if "clusters" not in sample:
                    sample["clusters"] = [_cluster_resources(site) for site in handoff["sites"]]
                    sample["clusterResourceSampleAgeSeconds"] = 0
                append_jsonl(evidence_dir / "resource-samples.jsonl", sample)
                return "readiness, freshness, or revision gate failed during traffic"
            if not reference and now - last_probe >= 15.0:
                last_probe = now
                probe_sequence += 1
                try:
                    candidate = _candidate_on_client_site(handoff, gate)
                    expected = _expected_site(candidate, handoff["client"]["attribution"])
                    send_probe(handoff, transport, evidence_dir=evidence_dir, sequence=probe_sequence,
                               expected_site=expected, label=f"{phase}-window",
                               response_limit_bytes=profile.max_response_bytes)
                except Exception as exc:
                    sample["correlatedProbeFailure"] = str(exc)[:500]
                    append_jsonl(evidence_dir / "resource-samples.jsonl", sample)
                    return "low-rate correlated route probe or backend attribution failed"
        except Exception as exc:
            sample["gateFailure"] = str(exc)
            append_jsonl(evidence_dir / "resource-samples.jsonl", sample)
            return "live gate sampling failed during traffic"
        append_jsonl(evidence_dir / "resource-samples.jsonl", sample)
        return None

    result = execute_window(handoff, profile, evidence_dir=evidence_dir, phase=phase,
                            rate=rate, duration=duration, index=index, reference=reference,
                            monitor=monitor, stop_event=stop_event)
    return result


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def run_benchmark(handoff: dict[str, Any], profile: Profile, profile_path: Path, *, root: Path | None = None,
                  stop_event: threading.Event | None = None) -> dict[str, Any]:
    handoff = _token_resolved_handoff(handoff)
    stop_event = stop_event or threading.Event()
    if profile.slo and not isinstance(profile.slo, dict):
        raise ValidationError("profile.slo must be an object or null")
    directory = new_run_dir(handoff, evidence_root(handoff, root))
    run_lock = _acquire_run_lock(handoff["runId"], directory)
    from .models import redacted_handoff
    write_json(directory / "handoff.redacted.json", redacted_handoff(handoff))
    write_json(directory / "profile.json", profile.raw)
    write_json(directory / "source-lock.json", {
        "setupSource": handoff["source"], "gridzillaSource": _source_identity(),
        "runtimeImages": handoff["runtimeImages"],
        "setupImageLockPath": handoff.get("_resolvedImageLock"),
        "setupImageLockSha256": hashlib.sha256(Path(handoff["_resolvedImageLock"]).read_bytes()).hexdigest(),
        "setupImageLock": json.loads(Path(handoff["_resolvedImageLock"]).read_text(encoding="utf-8")),
        "maasBenchmarkReference": {"repository": "https://github.com/nerdalert/maas-benchmark",
                                   "branch": "main", "commit": "e3467b377d29a882e93ef1dff437d6935a0ea11d"},
        "k6Version": (Path(__file__).resolve().parents[2] / "tools" / "k6-version").read_text().strip(),
    })
    write_json(directory / "host-start.json", _host_resource_sample())
    status_path = directory / "live-state.json"
    state = {"runId": directory.name, "setupRunId": handoff["runId"], "phase": "preflight", "status": "RUNNING",
             "createdAtUtc": utc_now(), "updatedAtUtc": utc_now()}
    write_json(status_path, state)
    completed_traffic_windows = 0
    phase_count = len(profile.rates) + (1 if handoff.get("backendReference", {}).get("url") else 0) + 2
    expected_traffic_windows = phase_count * (profile.repetitions + (1 if duration_seconds(profile.warmup, "profile.warmup") else 0))

    def record_traffic_progress(update: dict[str, Any]) -> None:
        nonlocal completed_traffic_windows
        if update["action"] == "window-complete":
            completed_traffic_windows += 1
        state.update({"trafficProgress": {**update, "completedWindows": completed_traffic_windows,
                                           "expectedWindows": expected_traffic_windows},
                      "updatedAtUtc": utc_now()})
        write_json(status_path, state)

    try:
        transport = _startup_transports(handoff, directory)
    except Exception as exc:
        run_lock.close()
        state.update({"phase": "preflight-failed", "status": "FAIL", "failure": str(exc), "updatedAtUtc": utc_now()})
        write_json(status_path, state)
        write_json(directory / "run-result.json", {"schemaVersion": 1, "type": "benchmark", "runId": directory.name,
                                                    "setupRunId": handoff["runId"], "status": "FAIL", "failure": str(exc),
                                                    "createdAtUtc": state["createdAtUtc"], "finishedAtUtc": utc_now(), "windows": [],
                                                    "claimLimit": "Synthetic llm-d-sim signal pressure; no GPU saturation, model quality, or token-throughput claim."})
        _capture_diagnostics(handoff, directory)
        with contextlib.suppress(Exception):
            from .report import regenerate_report
            regenerate_report(directory)
        raise
    write_json(directory / "handoff.redacted.json", redacted_handoff(handoff))
    original_snapshot = None
    pressure_applied = False
    restoration: dict[str, Any] | None = None
    windows: list[dict[str, Any]] = []
    signal_events = []
    failure: str | None = None
    with SignalRestorer():
        try:
            preflight = doctor(handoff, profile=profile, evidence=directory, transport=transport)
            write_json(directory / "doctor.json", preflight)
            if not preflight["ok"]:
                raise BenchmarkFailure("doctor failed: " + "; ".join(preflight["problems"]))
            _ready_gate(handoff, transport, profile)
            state.update({"phase": "baseline", "status": "RUNNING", "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            # Reuse the benchmark's already-owned forwards. Starting another
            # set here rewrites the handoff's local ports and then closes those
            # ports, leaving the benchmark transport pointed at dead listeners.
            smoke(handoff, evidence_dir=directory, count=2,
                  response_limit_bytes=profile.max_response_bytes, transport=transport)
            windows.extend(run_traffic_phase(handoff, profile, transport, directory, phase="baseline",
                                             rates=list(profile.rates), repeats=profile.repetitions,
                                             duration=profile.window, warmup=profile.warmup,
                                             stop_event=stop_event, on_progress=record_traffic_progress))
            if handoff.get("backendReference", {}).get("url"):
                reference_windows = run_traffic_phase(handoff, profile, transport, directory,
                                                      phase="direct-simulator-reference",
                                                      rates=[profile.direct_reference_rate], repeats=profile.repetitions,
                                                      duration=profile.window, warmup=profile.warmup,
                                                      stop_event=stop_event, reference=True,
                                                      on_progress=record_traffic_progress)
                windows.extend(reference_windows)
            _ready_gate(handoff, transport, profile)
            baseline_gate = gate_snapshot(handoff, transport, max_age_seconds=duration_seconds(profile.freshness_limit, "freshness limit"))
            write_json(directory / "baseline-state.json", baseline_gate)
            baseline_top = _candidate_on_client_site(handoff, baseline_gate)
            pressure_target = _pressure_target(handoff, profile)
            pressured_site = pressure_target["name"]
            baseline_top_site = _expected_site(baseline_top, handoff["client"]["attribution"])
            if baseline_top_site != pressured_site:
                raise BenchmarkFailure(f"{profile.pressure_target_pool} must be rank-0 on the west/client Gateway before pressure; "
                                       f"rank-0={baseline_top_site!r}, target={pressured_site!r}")
            if pressured_site not in {site["name"] for site in handoff["sites"]}:
                raise BenchmarkFailure("baseline rank-0 candidate did not map to a handoff site")
            pressure_site = next(site for site in handoff["sites"] if site["name"] == pressured_site)
            capacity = float(pressure_site["provider"].get("queueCapacity", 0))
            if capacity <= 0:
                raise BenchmarkFailure("handoff must carry the active Grid queueCapacity to choose safe synthetic pressure")
            pressure_value = pressure_site["simulator"].get("pressureValue")
            if pressure_value is None or float(pressure_value) <= capacity:
                raise BenchmarkFailure("configured waiting-requests pressure must be greater than the live queue capacity")
            original_snapshot = snapshot_simulators(handoff, transport, directory, pressure_site=pressured_site)
            state.update({"phase": "pressure-applying", "status": "RUNNING", "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            pressure_applied = True
            pressure_mutation_start = monotonic_utc_pair()
            results = mutate_simulators(handoff, transport, original_snapshot, pressure_value)
            pressure_posted = monotonic_utc_pair()
            signal_events.append({"boundary": "simulator-config-post-complete", "time": pressure_posted,
                                  "started": pressure_mutation_start,
                                  "postResults": results,
                                  "durationMs": (pressure_posted["monotonicNs"] - pressure_mutation_start["monotonicNs"]) / 1e6})
            write_json(directory / "simulator-pressure-applied.json", {"requestedWaitingRequests": pressure_value, "pods": results,
                                                                         "appliedAt": monotonic_utc_pair()})
            state.update({"phase": "pressure-convergence", "status": "RUNNING", "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            pressured_gate, pressure_facts = wait_for_signal_transition(handoff, transport, profile, baseline_gate,
                                                                        pressured_site, evidence_dir=directory)
            signal_events.append({"boundary": "grid-selection-shift", **pressure_facts})
            expected_pressure_site = _expected_site(_candidate_on_client_site(handoff, pressured_gate), handoff["client"]["attribution"])
            send_probe(handoff, transport, evidence_dir=directory, sequence=1,
                       expected_site=expected_pressure_site, label="pressure-route",
                       response_limit_bytes=profile.max_response_bytes)
            state.update({"phase": "pressure-measurement", "status": "RUNNING", "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            windows.extend(run_traffic_phase(handoff, profile, transport, directory, phase="pressure",
                                             rates=[profile.signal_rate], repeats=profile.repetitions,
                                             duration=profile.window, warmup=profile.warmup,
                                             stop_event=stop_event, on_progress=record_traffic_progress))
            state.update({"phase": "restoring", "status": "RUNNING", "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            restore_result = restore_simulators(handoff, transport, original_snapshot, directory)
            pressure_applied = False
            restoration = {**restore_result, "verified": False, "recovery": None}
            write_json(directory / "simulator-restore-post.json", restore_result)
            try:
                restored_gate, recovery = wait_for_recovery(handoff, transport, profile, baseline_gate,
                                                            pressured_site, evidence_dir=directory, pressured=pressured_gate)
            except Exception as recovery_exc:
                restoration["recoveryError"] = str(recovery_exc)
                raise
            restoration.update({"recovery": recovery, "verified": True})
            signal_events.append({"boundary": "recovery-complete", **recovery})
            expected_recovery_site = _expected_site(_candidate_on_client_site(handoff, restored_gate), handoff["client"]["attribution"])
            send_probe(handoff, transport, evidence_dir=directory, sequence=2,
                       expected_site=expected_recovery_site, label="recovery-route",
                       response_limit_bytes=profile.max_response_bytes)
            state.update({"phase": "recovery-measurement", "status": "RUNNING", "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            windows.extend(run_traffic_phase(handoff, profile, transport, directory, phase="recovery",
                                             rates=[profile.signal_rate], repeats=profile.repetitions,
                                             duration=profile.window, warmup=profile.warmup,
                                             stop_event=stop_event, on_progress=record_traffic_progress))
            if profile.slo:
                _evaluate_slo(windows, profile.slo)
        except BaseException as exc:
            failure = str(exc)
            state.update({"status": "FAIL", "failure": failure, "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            _capture_diagnostics(handoff, directory)
            raise
        finally:
            if pressure_applied and original_snapshot is not None:
                try:
                    restoration = restore_simulators(handoff, transport, original_snapshot, directory)
                    pressure_applied = False
                    if "baseline_gate" in locals():
                        _restored_gate, recovery = wait_for_recovery(handoff, transport, profile,
                                                                     baseline_gate, original_snapshot["pressureSite"], evidence_dir=directory,
                                                                     pressured=locals().get("pressured_gate"))
                        restoration["recovery"] = recovery
                        restoration["verified"] = True
                    else:
                        restoration["verified"] = False
                        restoration["error"] = "No baseline overlay/revision snapshot exists to verify restoration"
                except Exception as restore_exc:
                    restoration = {**(restoration or {}), "verified": False, "error": str(restore_exc)}
                    _capture_diagnostics(handoff, directory)
            if restoration is None:
                restoration = {"verified": True, "notNeeded": True}
            write_json(directory / "restoration-proof.json", restoration)
            final_gate = None
            try:
                final_gate = gate_snapshot(handoff, transport, max_age_seconds=duration_seconds(profile.freshness_limit, "freshness limit"))
            except Exception as exc:
                final_gate = {"ok": False, "error": str(exc)}
            if not final_gate.get("ok") and failure is None:
                failure = "final readiness gate failed after benchmark"
            if not restoration.get("verified") and failure is None:
                failure = "simulator restoration was not verified"
            write_json(directory / "end-state.json", final_gate)
            write_json(directory / "host-end.json", _host_resource_sample())
            state.update({"phase": "complete" if not failure else "failed", "status": "PASS" if not failure and restoration.get("verified") else "FAIL", "updatedAtUtc": utc_now()})
            write_json(status_path, state)
            write_json(directory / "run-result.json", {
                "schemaVersion": 1, "type": "benchmark", "runId": directory.name,
                "setupRunId": handoff["runId"], "status": state["status"], "failure": failure,
                "createdAtUtc": state.get("createdAtUtc", utc_now()), "finishedAtUtc": utc_now(),
                "windows": windows, "signalEvents": signal_events,
                "restoration": restoration, "endState": final_gate,
                "loadGeneratorLocation": "local Gridzilla host",
                "claimLimit": "Synthetic llm-d-sim signal pressure; no GPU saturation, model quality, or token-throughput claim.",
            })
            try:
                from .report import regenerate_report
                regenerate_report(directory)
            except Exception as report_exc:
                write_json(directory / "report-error.json", {"error": str(report_exc), "observedAtUtc": utc_now()})
            transport.close()
            run_lock.close()
    return {"evidenceDirectory": str(directory), "status": state["status"], "windows": len(windows), "failure": failure}


def restore_simulators(handoff: dict[str, Any], transport: EndpointTransport, snapshot: dict[str, Any],
                       evidence_dir: Path) -> dict[str, Any]:
    site = next(item for item in handoff["sites"] if item["name"] == snapshot["pressureSite"])
    pointer = site["simulator"]["pressureJsonPointer"]
    results = []
    for saved in snapshot["sites"][snapshot["pressureSite"]]:
        target = dict(saved["target"])
        target.setdefault("adminUrl", site["simulator"]["adminUrl"])
        _verify_sim_pod_identity(site, target)
        site_target = {**site, "simulator": target}
        post = post_simulator_config(site_target, transport, _fake_metric_patch(pointer, saved["originalMetricValue"]))
        actual_config = _sim_target_config(site, target, transport)
        _path, actual_value = _fake_metric_snapshot(actual_config, pointer)
        if actual_value != saved["originalMetricValue"]:
            raise BenchmarkFailure(f"{site['name']}/{target['podName']}: original simulator value did not restore")
        if actual_config != saved["config"]:
            raise BenchmarkFailure(f"{site['name']}/{target['podName']}: full simulator configuration differs from its captured original")
        identity = _verify_sim_pod_identity(site, target)
        results.append({"pod": identity, "post": post, "restoredValue": actual_value,
                        "matchesOriginal": True})
    verify_untouched_simulators(handoff, transport, snapshot, except_site=snapshot["pressureSite"])
    proof = {"restoredAt": monotonic_utc_pair(), "pressureSite": snapshot["pressureSite"], "pods": results, "verified": True}
    append_jsonl(evidence_dir / "signal-boundaries.jsonl", {"boundary": "simulator-metrics-restored", **proof})
    return proof


def _capture_diagnostics(handoff: dict[str, Any], directory: Path) -> None:
    diagnostics = []
    for site in handoff["sites"]:
        for kind, args in (
            ("pods", ["get", "pods", "-A", "-o", "wide"]),
            ("endpointslices", ["get", "endpointslices", "-A", "-o", "wide"]),
            ("events", ["get", "events", "-A", "--sort-by=.lastTimestamp"]),
        ):
            try:
                text = kubectl(site, args, timeout=20).decode(errors="replace")
                path = directory / "diagnostics" / f"{site['name']}-{kind}.txt"
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_text(text, encoding="utf-8")
                diagnostics.append({"site": site["name"], "kind": kind, "path": str(path.relative_to(directory))})
            except Exception as exc:
                diagnostics.append({"site": site["name"], "kind": kind, "error": str(exc)[:300]})
    write_json(directory / "diagnostics" / "index.json", diagnostics)


def _evaluate_slo(windows: list[dict[str, Any]], slo: dict[str, Any]) -> None:
    for key, limit in slo.items():
        if key not in {"maxHttpFailureRate", "maxP95LatencyMs", "maxDroppedIterations"}:
            raise ValidationError(f"unsupported explicit SLO {key}")
        for window in windows:
            if window.get("reference"):
                continue
            account = window.get("accounting", {})
            if key == "maxHttpFailureRate" and account.get("httpFailureRate") is not None and account["httpFailureRate"] > float(limit):
                raise BenchmarkFailure(f"explicit SLO exceeded: {key}={account['httpFailureRate']} > {limit}")
            if key == "maxP95LatencyMs":
                p95 = account.get("latencyMs", {}).get("p95", {}).get("value")
                if p95 is not None and p95 > float(limit):
                    raise BenchmarkFailure(f"explicit SLO exceeded: {key}={p95} > {limit}")
            if key == "maxDroppedIterations" and account.get("droppedIterations", 0) > int(limit):
                raise BenchmarkFailure(f"explicit SLO exceeded: {key}={account['droppedIterations']} > {limit}")


def _acquire_run_lock(run_id: str, run_directory: Path) -> Any:
    lock_root = run_directory.parent / ".gridzilla-locks"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    lock_path = lock_root / f"{run_id}.lock"
    stream = lock_path.open("a+")
    try:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, BlockingIOError) as exc:
        stream.close()
        raise BenchmarkFailure(f"Gridzilla run {run_id} already has an active controller lock") from exc
    stream.write(json.dumps({"runId": run_id, "evidenceDirectory": str(run_directory), "pid": os.getpid(), "startedAtUtc": utc_now()}) + "\n")
    stream.flush()
    return stream


def load_original_snapshot(handoff: dict[str, Any], root: Path) -> tuple[Path, dict[str, Any]]:
    matches = sorted(root.glob(f"{handoff['runId']}-*/simulator-original.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise BenchmarkFailure(f"no captured simulator snapshot for setup run {handoff['runId']} under {root}")
    path = matches[0]
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("runId") != handoff["runId"]:
        raise BenchmarkFailure("simulator snapshot belongs to a different setup run")
    return path.parent, snapshot


def restore_command(handoff: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    handoff = _token_resolved_handoff(handoff)
    directory, snapshot = load_original_snapshot(handoff, evidence_root(handoff, root))
    lock = _acquire_run_lock(handoff["runId"], directory)
    transport: EndpointTransport | None = None
    try:
        transport = _startup_transports(handoff, directory)
        result = restore_simulators(handoff, transport, snapshot, directory)
        freshness = min(float(site["epp"]["maxSampleAgeSeconds"]) for site in handoff["sites"])
        current = gate_snapshot(handoff, transport, max_age_seconds=freshness)
        proof = {"simulator": result, "currentGate": current, "verified": result["verified"] and current["ok"]}
        write_json(directory / "manual-restore-proof.json", proof)
        if not proof["verified"]:
            raise BenchmarkFailure("simulator restored, but EPP/Grid/overlay/Gateway recovery is not proven")
        return proof
    finally:
        if transport is not None:
            transport.close()
        lock.close()
