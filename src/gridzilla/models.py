"""Versioned handoff and bounded profile validation (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HANDOFF_VERSION = "gridzilla.dev/v1alpha1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
MAX_REQUEST_BODY_BYTES = 1024 * 1024


class ValidationError(ValueError):
    """Input is malformed or violates a safety bound."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


def _required(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj or obj[key] is None or obj[key] == "":
        raise ValidationError(f"{where}.{key} is required")
    return obj[key]


def _safe_url(value: Any, where: str) -> str:
    from urllib.parse import urlsplit

    if not isinstance(value, str):
        raise ValidationError(f"{where} must be a URL string")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValidationError(f"{where} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValidationError(f"{where} must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ValidationError(f"{where} must not contain a query or fragment")
    return value


def _credential_file(value: Any, base: Path, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} must be a credential file path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{where} does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise ValidationError(f"{where} is not a file: {resolved}")
    return resolved


def validate_setup_manifest(handoff: dict[str, Any], handoff_path: Path) -> Path:
    """Require the adjacent setup owner's ready manifest to match this handoff."""
    path = handoff_path.parent / "run-manifest.json"
    manifest = load_json(path)
    run_id = handoff.get("runId")
    sites = handoff.get("sites")
    expected_clusters = [site.get("clusterName") for site in sites if isinstance(site, dict)] if isinstance(sites, list) else []
    expected_prefix = f"grid-signals-{run_id}"
    actual_evidence = manifest.get("evidenceDir")
    try:
        evidence_matches = Path(str(actual_evidence)).expanduser().resolve(strict=True) == handoff_path.parent.resolve(strict=True)
    except OSError:
        evidence_matches = False
    if manifest.get("schemaVersion") != "1":
        raise ValidationError("adjacent run-manifest.json has an unsupported schemaVersion")
    if manifest.get("runId") != run_id:
        raise ValidationError("adjacent run-manifest.json does not belong to handoff.runId")
    if manifest.get("clusterPrefix") != expected_prefix:
        raise ValidationError("adjacent run-manifest.json does not declare this setup's run-owned cluster prefix")
    if manifest.get("clusterNames") != expected_clusters:
        raise ValidationError("adjacent run-manifest.json clusterNames do not exactly match the handoff")
    if manifest.get("lifecycle") != "ready":
        raise ValidationError("setup run-manifest.json lifecycle must be 'ready' before Gridzilla can attach")
    if not evidence_matches:
        raise ValidationError("adjacent run-manifest.json evidenceDir does not match the handoff directory")
    return path.resolve(strict=True)


def _normalize_setup_handoff(raw_value: dict[str, Any], handoff_path: Path) -> dict[str, Any]:
    """Adapt the combined-site xtask's v1alpha1 handoff into Gridzilla's internal form."""
    raw = json.loads(json.dumps(raw_value))
    if raw.get("schemaVersion") != HANDOFF_VERSION:
        return raw
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    source = {
        **source,
        **{
            key: value.strip() if isinstance(value, str) else value
            for key, value in source.items()
            if key in {"branch", "commit", "revision"}
        },
    }
    worktree_status = source.get("worktreeStatus")
    dirty_value = source.get("dirty")
    if not isinstance(dirty_value, bool):
        if isinstance(worktree_status, str):
            dirty_value = bool(worktree_status.strip()) and worktree_status.strip().lower() not in {"clean", "working tree clean"}
        else:
            dirty_value = bool(worktree_status)
    raw["createdAtUtc"] = raw.get("createdAtUtc", raw.get("createdAt"))
    raw["source"] = {
        **source,
        "revision": source.get("revision", source.get("commit")),
        "dirty": dirty_value,
        "diffFingerprint": source.get("diffFingerprint", source.get("sourceFingerprintSha256")),
    }
    raw["source"]["repository"] = source.get("repository", "praxis-proxy/grid")

    client = raw.get("client") if isinstance(raw.get("client"), dict) else {}
    if not client.get("site") and isinstance(client.get("context"), str):
        context_sites = [
            site.get("name")
            for site in raw.get("sites", [])
            if isinstance(site, dict) and site.get("context") == client["context"] and site.get("name")
        ]
        if len(context_sites) == 1:
            client["site"] = context_sites[0]
    gateway_url = client.get("url", client.get("gatewayUrl"))
    path = client.get("path", client.get("requestPath"))
    if isinstance(gateway_url, str):
        _safe_url(gateway_url, "client.gatewayUrl")
        from urllib.parse import urlsplit, urlunsplit
        parsed = urlsplit(gateway_url)
        # The setup hands off a full URL, while Gridzilla carries a separate
        # path so k6 can also reuse it for the direct provider reference.
        client["url"] = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    client["path"] = path
    request = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    client["model"] = client.get("model", request.get("model"))
    client["requestBody"] = client.get("requestBody", request.get("body"))
    client["authHeader"] = client.get("authHeader", "Authorization")
    client["requestIdHeader"] = client.get("requestIdHeader", "X-Gridzilla-Request-Id")
    client["requestIdHeaders"] = client.get("requestIdHeaders", ["X-Request-Id", "X-Gridzilla-Request-Id"])
    header_names = {str(item).lower() for item in client.get("attributionHeaders", [])}
    attribution = client.get("attribution")
    if not isinstance(attribution, dict):
        site_names = [site.get("name") for site in raw.get("sites", []) if isinstance(site, dict)]
        provider_to_pool = {site.get("name"): site.get("provider", {}).get("pool") for site in raw.get("sites", [])}
        provider_to_site = {site.get("name"): site.get("name") for site in raw.get("sites", [])}
        provider_to_backend = {site.get("name"): "vcr-backend" for site in raw.get("sites", [])}
        backend_pod_prefix_by_site = {
            site.get("name"): f"vcr-inference-{site.get('name')}-"
            for site in raw.get("sites", [])
        }
        attribution = {
            "siteHeader": "x-grid-combined-provider-gateway", "backendHeader": "x-ai-inference-provider",
            "providerHeader": "x-grid-combined-provider-gateway",
            "providerGatewayHeader": "x-ai-demo-provider-gateway",
            "backendPodHeader": "x-inference-pod", "requestIdEchoHeader": "x-request-id",
            "poolHeader": None,
            "allowedSites": site_names, "allowedPools": [value for value in provider_to_pool.values() if value],
            "allowedBackends": ["vcr-backend"],
            "allowedProviders": site_names, "providerToSite": provider_to_site,
            "providerToPool": provider_to_pool, "providerToBackend": provider_to_backend,
            "backendPodPrefixBySite": backend_pod_prefix_by_site,
            "requiredResponseHeaders": sorted(header_names | {
                "x-grid-combined-provider-gateway", "x-ai-inference-provider",
                "x-ai-demo-provider-gateway", "x-request-id", "x-inference-pod",
            }),
        }
    client["attribution"] = attribution
    client["receipt"] = client.get("receipt", {
        "available": True, "type": "response_headers", "requestIdHeader": "x-request-id",
        "backendPodHeader": "x-inference-pod", "backendPodPrefixBySite": backend_pod_prefix_by_site,
    })
    raw["client"] = client

    reference = raw.get("backendReference") if isinstance(raw.get("backendReference"), dict) else {}
    if "url" not in reference and all(key in reference for key in ("site", "service", "port")):
        from urllib.parse import urlsplit, urlunsplit
        local_port = int(reference.get("localPort", 35791))
        service_port = int(reference["port"])
        path_ref = str(reference.get("path", client["path"]))
        reference["url"] = f"http://127.0.0.1:{local_port}"
        reference["path"] = path_ref
        site_ref = next(site for site in raw["sites"] if site["name"] == reference["site"])
        reference["portForward"] = {"namespace": reference.get("namespace", site_ref.get("namespace", "grid-system")),
                                    "resource": f"svc/{reference['service']}", "localPort": local_port,
                                    "remotePort": service_port}
        reference["model"] = reference.get("model", request.get("model"))
    raw["backendReference"] = reference

    for site in raw.get("sites", []):
        if not isinstance(site, dict):
            continue
        site.setdefault("clusterName", site.get("cluster_name"))
        site.setdefault("ownerRunId", site.get("owner_run_id"))
        site.setdefault("context", site.get("clusterName"))
        site.setdefault("namespace", site.get("provider", {}).get("namespace", "grid-system"))
        site["_cluster"] = {"kubeconfig": str(Path(site["kubeconfig"]).expanduser().resolve()),
                            "context": str(site["context"])}
        site.setdefault("readinessTargets", [])
        for target in site["readinessTargets"]:
            if "resource" not in target and target.get("kind") and target.get("name"):
                target["resource"] = f"{target['kind']}/{target['name']}"
        provider = site.get("provider", {})
        if provider:
            provider.setdefault("resource", f"{provider.get('kind', 'InferenceProvider')}/{provider.get('resourceName')}")
            provider.setdefault("candidateKey", "site")
            provider.setdefault("candidateValue", site.get("name"))
            provider.setdefault("pool", site.get("name"))
            if site.get("pressureTarget") is True or site.get("simulator", {}).get("pressureTarget") is True:
                provider["pressureTarget"] = True
        runtime_images = raw.get("runtimeImages", [])
        for image in runtime_images:
            if isinstance(image, dict):
                image.setdefault("imageID", image.get("imageId"))
                image.setdefault("sourceRevision", image.get("source_revision"))
        site["runtimeImages"] = [image for image in runtime_images if image.get("site") == site.get("name")]

        epp = site.get("epp", {})
        auth = epp.get("auth", {})
        epp.setdefault("credentialFile", auth.get("tokenFile"))
        if epp.get("portForward"):
            pf = epp["portForward"]
            epp.setdefault("metricsUrl", f"http://127.0.0.1:{pf['localPort']}{epp.get('path', '/metrics')}")
        epp.setdefault("queueMetricNames", [epp.get("metric", "llm_d_epp_average_queue_size")])
        epp.setdefault("readyMetricNames", ["llm_d_epp_ready_endpoints"])
        epp.setdefault("maxSampleAgeSeconds", 30)
        if epp.get("portForward"):
            epp["portForward"].setdefault("namespace", epp.get("namespace", site.get("namespace", "grid-system")))
        epp.setdefault("requiredLabelAlternatives", [
            {"name": epp.get("pool", site.get("name"))},
            {"pool": epp.get("pool", site.get("name"))},
            {"inferencepool": epp.get("pool", site.get("name"))},
        ])

        simulator = site.get("simulator", {})
        targets = simulator.get("targets", [])
        for target in targets:
            target.setdefault("configGeneration", target.get("config_generation"))
            target.setdefault("adminUrl", target.get("admin_url"))
            target.setdefault("podName", target.get("pod_name"))
            target.setdefault("podUID", target.get("podUid", target.get("pod_uid")))
            if target.get("portForward"):
                target["portForward"].setdefault("namespace", target.get("namespace", site.get("namespace", "grid-system")))
        if targets:
            simulator.setdefault("adminUrl", targets[0].get("adminUrl"))
        capacity = float(provider.get("queueCapacity", 0))
        simulator.setdefault("pressureJsonPointer", "/fake-metrics/waiting-requests")
        simulator.setdefault("pressureValue", int(max(capacity + 5, 9)))
        simulator.setdefault("baselineValue", 0)

        overlay = site.get("overlay", {})
        overlay.setdefault("resource", f"ConfigMap/{overlay.get('configMap')}")
        overlay.setdefault("dataKey", "routing-config.json")
        overlay.setdefault("revisionAnnotation", "grid.praxis.fast/overlay-revision")
        overlay.setdefault("candidateKey", "site")
        gateway = site.get("gateway", {})
        if gateway.get("service"):
            gateway.setdefault("resource", "deployment/consumer-gateway")
            gateway.setdefault("container", "praxis")
            gateway.setdefault("acceptedField", "accepted_revision")
            gateway.setdefault("servingField", "serving_revision")
            gateway.setdefault("url", gateway.get("url"))
        gateway.setdefault("logsSince", "10m")
        site["gateway"] = gateway
    raw["_handoffPath"] = str(handoff_path.resolve())
    return raw


def validate_handoff(raw: dict[str, Any], handoff_path: Path) -> dict[str, Any]:
    """Validate required structure and secret-free identity fields.

    Unknown fields are intentionally preserved and ignored so setup can add
    diagnostics without breaking older Gridzilla releases.
    """
    raw = _normalize_setup_handoff(raw, handoff_path)
    if raw.get("schemaVersion") != HANDOFF_VERSION:
        raise ValidationError(
            f"schemaVersion must be {HANDOFF_VERSION!r}, got {raw.get('schemaVersion')!r}"
        )
    run_id = _required(raw, "runId", "handoff")
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise ValidationError("handoff.runId must be a DNS-safe 3-63 character identifier")
    _required(raw, "createdAtUtc", "handoff")
    source = _required(raw, "source", "handoff")
    if not isinstance(source, dict):
        raise ValidationError("handoff.source must be an object")
    revision = _required(source, "revision", "source")
    if not isinstance(revision, str) or not SHA_RE.fullmatch(revision):
        raise ValidationError("source.revision must be a full 40-character Git SHA")
    if source.get("dirty") is True and not source.get("diffFingerprint"):
        raise ValidationError("dirty source must include source.diffFingerprint")

    images = _required(raw, "runtimeImages", "handoff")
    if not isinstance(images, list) or not images:
        raise ValidationError("handoff.runtimeImages must be a non-empty list")
    for index, image in enumerate(images):
        where = f"runtimeImages[{index}]"
        if not isinstance(image, dict):
            raise ValidationError(f"{where} must be an object")
        for key in ("site", "component", "namespace", "selector", "container", "reference", "imageID"):
            _required(image, key, where)
        if not DIGEST_RE.search(str(image["imageID"])):
            raise ValidationError(f"{where}.imageID must contain an immutable sha256 digest")
        if image.get("sourceRevision") is not None and not SHA_RE.fullmatch(str(image["sourceRevision"])):
            raise ValidationError(f"{where}.sourceRevision must be a full Git SHA")
    image_lock_path = Path(str(_required(raw, "imageLock", "handoff"))).expanduser()
    if not image_lock_path.is_absolute():
        image_lock_path = handoff_path.parent / image_lock_path
    try:
        image_lock_path = image_lock_path.resolve(strict=True)
        image_lock = load_json(image_lock_path)
    except (OSError, ValidationError) as exc:
        raise ValidationError(f"handoff.imageLock must point to a readable image-lock JSON: {exc}") from exc
    locked_images = image_lock.get("images")
    if not isinstance(locked_images, list) or not locked_images:
        raise ValidationError("handoff.imageLock must contain a non-empty images array")
    references = {item.get("reference") for item in locked_images if isinstance(item, dict)}
    for index, item in enumerate(locked_images):
        if not isinstance(item, dict):
            raise ValidationError(f"imageLock.images[{index}] must be an object")
        for key in ("role", "reference", "imageId"):
            _required(item, key, f"imageLock.images[{index}]")
        if not DIGEST_RE.search(str(item["imageId"])):
            raise ValidationError(f"imageLock.images[{index}].imageId must be immutable sha256 identity")
        if item.get("sourceRevision") is not None and not SHA_RE.fullmatch(str(item["sourceRevision"])):
            raise ValidationError(f"imageLock.images[{index}].sourceRevision must be a full Git SHA")
    unmatched = sorted({str(item.get("reference")) for item in images if item.get("reference") not in references})
    if unmatched:
        raise ValidationError("runtime image references are absent from imageLock: " + ", ".join(unmatched))

    client = _required(raw, "client", "handoff")
    if not isinstance(client, dict):
        raise ValidationError("handoff.client must be an object")
    for key in ("site", "path", "model", "requestBody", "credentialFile", "requestIdHeader", "authHeader", "attribution"):
        _required(client, key, "client")
    _safe_url(_required(client, "url", "client"), "client.url")
    if not isinstance(client["requestBody"], dict):
        raise ValidationError("client.requestBody must be a JSON object")
    if str(client["requestBody"].get("model", "")) != str(client["model"]):
        raise ValidationError("client.requestBody.model must match client.model")
    _credential_file(client["credentialFile"], handoff_path.parent, "client.credentialFile")
    attribution = client["attribution"]
    if not isinstance(attribution, dict):
        raise ValidationError("client.attribution must be an object")
    for key in ("providerHeader", "siteHeader", "backendHeader", "providerGatewayHeader",
                "backendPodHeader", "requestIdEchoHeader",
                "allowedSites", "allowedPools", "allowedBackends"):
        _required(attribution, key, "client.attribution")
    for key in ("allowedSites", "allowedPools", "allowedBackends", "allowedProviders"):
        if not isinstance(attribution.get(key), list) or not attribution[key]:
            raise ValidationError(f"client.attribution.{key} must be a non-empty list")
    required_headers = {str(value).lower() for value in attribution.get("requiredResponseHeaders", [])}
    required_headers.update({str(attribution[key]).lower() for key in
                             ("providerHeader", "siteHeader", "backendHeader", "providerGatewayHeader",
                              "backendPodHeader", "requestIdEchoHeader")})
    if len(required_headers) < 5:
        raise ValidationError("client.attribution must require route identity, backend route, request receipt, and Pod identity headers")
    attribution["requiredResponseHeaders"] = sorted(required_headers)
    pod_prefixes = _required(attribution, "backendPodPrefixBySite", "client.attribution")
    if not isinstance(pod_prefixes, dict) or set(pod_prefixes) != set(attribution["allowedSites"]):
        raise ValidationError("client.attribution.backendPodPrefixBySite must cover every allowed site")
    receipt = client.get("receipt", {"available": False})
    if not isinstance(receipt, dict):
        raise ValidationError("client.receipt must be an object")
    if receipt.get("available", True):
        if receipt.get("type") == "response_headers":
            _required(receipt, "requestIdHeader", "client.receipt")
            _required(receipt, "backendPodHeader", "client.receipt")
            prefixes = _required(receipt, "backendPodPrefixBySite", "client.receipt")
            if not isinstance(prefixes, dict) or set(prefixes) != set(attribution["allowedSites"]):
                raise ValidationError("client.receipt.backendPodPrefixBySite must cover every allowed site")
            if receipt["requestIdHeader"].lower() != attribution["requestIdEchoHeader"].lower():
                raise ValidationError("client.receipt.requestIdHeader must match the attribution requestIdEchoHeader")
            if receipt["backendPodHeader"].lower() != attribution["backendPodHeader"].lower():
                raise ValidationError("client.receipt.backendPodHeader must match the attribution backendPodHeader")
        else:
            template = _required(receipt, "urlTemplate", "client.receipt")
            if not isinstance(template, str) or "{request_id}" not in template:
                raise ValidationError("client.receipt.urlTemplate must contain {request_id}")
            _safe_url(template.replace("{request_id}", "gridzilla-probe"), "client.receipt.urlTemplate")
            if receipt.get("credentialFile"):
                _credential_file(receipt["credentialFile"], handoff_path.parent, "client.receipt.credentialFile")

    reference = _required(raw, "backendReference", "handoff")
    if not isinstance(reference, dict):
        raise ValidationError("handoff.backendReference must be an object")
    _safe_url(_required(reference, "url", "backendReference"), "backendReference.url")

    sites = _required(raw, "sites", "handoff")
    if not isinstance(sites, list) or len(sites) != 3:
        raise ValidationError("handoff.sites must contain exactly three sites")
    names: set[str] = set()
    clusters: set[str] = set()
    local_forward_ports: set[int] = set()
    for index, site in enumerate(sites):
        where = f"sites[{index}]"
        if not isinstance(site, dict):
            raise ValidationError(f"{where} must be an object")
        for key in ("name", "clusterName", "ownerRunId", "kubeconfig", "context", "namespace", "readinessTargets", "provider", "epp", "simulator", "overlay", "gateway"):
            _required(site, key, where)
        if site["ownerRunId"] != run_id:
            raise ValidationError(f"{where}.ownerRunId must match handoff.runId")
        if site["name"] in names or site["clusterName"] in clusters:
            raise ValidationError("site names and cluster names must be unique")
        names.add(site["name"])
        clusters.add(site["clusterName"])
        if not (str(site["clusterName"]).startswith(run_id) or str(site["clusterName"]).startswith(f"grid-signals-{run_id}-")):
            raise ValidationError(f"{where}.clusterName must begin with the run-owned prefix to prove ownership")
        kubeconfig = Path(str(site["kubeconfig"])).expanduser()
        if not kubeconfig.is_absolute():
            kubeconfig = handoff_path.parent / kubeconfig
        if not kubeconfig.is_file():
            raise ValidationError(f"{where}.kubeconfig does not exist: {kubeconfig}")
        site["_cluster"] = {"kubeconfig": str(kubeconfig.resolve()), "context": str(site["context"])}
        if not isinstance(site["readinessTargets"], list) or not site["readinessTargets"]:
            raise ValidationError(f"{where}.readinessTargets must be non-empty")
        provider = site["provider"]
        for key in ("resource", "pool", "candidateKey", "candidateValue"):
            _required(provider, key, f"{where}.provider")
        epp = site["epp"]
        for key in ("metricsUrl", "credentialFile", "queueMetricNames", "readyMetricNames", "maxSampleAgeSeconds"):
            _required(epp, key, f"{where}.epp")
        _safe_url(epp["metricsUrl"], f"{where}.epp.metricsUrl")
        _credential_file(epp["credentialFile"], handoff_path.parent, f"{where}.epp.credentialFile")
        epp["_resolvedCredentialFile"] = str(_credential_file(epp["credentialFile"], handoff_path.parent, f"{where}.epp.credentialFile"))
        if epp.get("caFile"):
            _credential_file(epp["caFile"], handoff_path.parent, f"{where}.epp.caFile")
            epp["_resolvedCaFile"] = str(_credential_file(epp["caFile"], handoff_path.parent, f"{where}.epp.caFile"))
        simulator = site["simulator"]
        for key in ("adminUrl", "pressureJsonPointer", "pressureValue", "baselineValue"):
            _required(simulator, key, f"{where}.simulator")
        _safe_url(simulator["adminUrl"], f"{where}.simulator.adminUrl")
        if not str(simulator["pressureJsonPointer"]).startswith("/"):
            raise ValidationError(f"{where}.simulator.pressureJsonPointer must be a JSON Pointer")
        capacity = float(provider.get("queueCapacity", 0))
        if capacity <= 0:
            raise ValidationError(f"{where}.provider.queueCapacity must be positive and come from active Grid policy")
        if float(simulator["pressureValue"]) <= capacity:
            raise ValidationError(f"{where}.simulator.pressureValue must exceed queue capacity")
        if not isinstance(simulator.get("targets"), list) or not simulator["targets"]:
            raise ValidationError(f"{where}.simulator.targets must list every simulator Pod")
        for target_index, target in enumerate(simulator["targets"]):
            target_where = f"{where}.simulator.targets[{target_index}]"
            if not isinstance(target, dict):
                raise ValidationError(f"{target_where} must be an object")
            for key in ("namespace", "podName", "podUID", "configGeneration", "adminUrl", "portForward"):
                _required(target, key, target_where)
            if not isinstance(target["configGeneration"], int) or isinstance(target["configGeneration"], bool):
                raise ValidationError(f"{target_where}.configGeneration must be an integer generation")
            _safe_url(target["adminUrl"], f"{target_where}.adminUrl")
            port_forward = target["portForward"]
            for key in ("resource", "localPort", "remotePort"):
                _required(port_forward, key, f"{target_where}.portForward")
            local_port = int(port_forward["localPort"])
            if local_port in local_forward_ports:
                raise ValidationError(f"{where}.simulator targets reuse local port {local_port}")
            local_forward_ports.add(local_port)
        overlay = site["overlay"]
        for key in ("namespace", "resource", "dataKey", "revisionAnnotation"):
            _required(overlay, key, f"{where}.overlay")
        gateway = site["gateway"]
        for key in ("namespace", "resource", "container", "acceptedField", "servingField"):
            _required(gateway, key, f"{where}.gateway")

    if client["site"] not in names:
        raise ValidationError("client.site must name one of the three handoff sites")
    allowed_sites = set(attribution["allowedSites"])
    if not allowed_sites.issubset(names):
        raise ValidationError("client.attribution.allowedSites must name handoff sites")
    pressure_targets = [site for site in sites if site.get("provider", {}).get("pressureTarget") is True]
    if len(pressure_targets) != 1:
        raise ValidationError("handoff must mark exactly one provider as pressureTarget")
    declared_pressure_pool = raw.get("pressureTargetPool")
    if declared_pressure_pool and pressure_targets[0]["provider"].get("pool") != declared_pressure_pool:
        raise ValidationError("handoff.pressureTargetPool does not match the marked pressureTarget provider")
    forward_specs = [client.get("portForward"), reference.get("portForward")]
    for site in sites:
        forward_specs.append(site.get("epp", {}).get("portForward"))
        simulator = site.get("simulator", {})
        if not simulator.get("targets"):
            forward_specs.append(simulator.get("portForward"))
    for item in forward_specs:
        if not item:
            continue
        local_port = int(_required(item, "localPort", "portForward"))
        if local_port in local_forward_ports:
            raise ValidationError(f"port-forward local port {local_port} is reused")
        local_forward_ports.add(local_port)
    raw["_handoffPath"] = str(handoff_path.resolve())
    raw["_resolvedRunManifest"] = str(validate_setup_manifest(raw, handoff_path))
    raw["_resolvedImageLock"] = str(image_lock_path)
    raw["_resolvedClientCredential"] = str(_credential_file(client["credentialFile"], handoff_path.parent, "client.credentialFile"))
    return raw


@dataclass(frozen=True)
class Profile:
    name: str
    max_rate: float
    preallocated_vus: int
    max_vus: int
    max_concurrency: int
    request_timeout: str
    max_output_tokens: int
    max_response_bytes: int
    max_evidence_bytes: int
    warmup: str
    window: str
    repetitions: int
    rates: tuple[float, ...]
    request_body_sizes_bytes: tuple[int | None, ...]
    poll_interval: str
    freshness_limit: str
    percentile_minimum_samples: int
    signal_rate: float
    direct_reference_rate: float
    pressure_target_pool: str
    slo: dict[str, Any] | None
    raw: dict[str, Any]


def duration_seconds(value: Any, where: str, *, maximum: int = 3600) -> int:
    if not isinstance(value, str):
        raise ValidationError(f"{where} must be a duration string like 10s or 2m")
    match = re.fullmatch(r"([1-9][0-9]*)(s|m)", value)
    if not match:
        raise ValidationError(f"{where} must use positive integer seconds or minutes")
    amount = int(match.group(1)) * (60 if match.group(2) == "m" else 1)
    if amount > maximum:
        raise ValidationError(f"{where} exceeds the {maximum}s safety cap")
    return amount


def plan_profile(profile: Profile, *, scenario: str = "grid", has_reference: bool | None = None,
                 rates: tuple[float, ...] | None = None, window: str | None = None,
                 request_body_sizes_bytes: tuple[int | None, ...] | None = None) -> dict[str, Any]:
    """Return the shared bounded traffic plan used by validation, controls and progress."""
    if scenario not in {"grid", "generic"}:
        raise ValidationError("scenario must be 'grid' or 'generic'")
    selected_rates = rates or profile.rates
    selected_window = window or profile.window
    selected_sizes = request_body_sizes_bytes or profile.request_body_sizes_bytes
    window_seconds = duration_seconds(selected_window, "profile.window", maximum=600)
    warmup_seconds = duration_seconds(profile.warmup, "profile.warmup", maximum=300)
    if has_reference is None:
        has_reference = scenario == "grid"
    if scenario == "grid":
        rate_sets = [(selected_rates, "baseline"),
                     ( (profile.direct_reference_rate,), "direct-reference") if has_reference else ((), "direct-reference"),
                     ((profile.signal_rate,), "pressure"), ((profile.signal_rate,), "recovery")]
    else:
        rate_sets = [(selected_rates, "baseline"),
                     ((profile.direct_reference_rate,), "direct-reference") if has_reference else ((), "direct-reference")]
    traffic_window_specs = [(phase, rate, size) for rate_values, phase in rate_sets
                            for rate in rate_values for size in selected_sizes]
    measurement_window_count = len(traffic_window_specs) * profile.repetitions
    warmup_window_count = len(traffic_window_specs) * (1 if warmup_seconds else 0)
    window_count = measurement_window_count + warmup_window_count
    total_seconds = measurement_window_count * window_seconds + warmup_window_count * warmup_seconds
    offered = sum(rate for _phase, rate, _size in traffic_window_specs) * (
        profile.repetitions * window_seconds + warmup_seconds
    )
    estimated_evidence = int(offered * 12_000 + window_count * 2_000_000)
    max_body_bytes = max((size or 0 for size in selected_sizes), default=0)
    # A VU can retain the serialized string plus JS/K6 copies while the HTTP
    # request is active. Four copies is a conservative planning estimate; it
    # is not an allocation guarantee or a substitute for host monitoring.
    estimated_body_memory = max_body_bytes * profile.max_vus * 4
    if total_seconds > 7200:
        raise ValidationError("total run duration exceeds Gridzilla's 2-hour safety limit")
    if offered > 25_000:
        raise ValidationError("profile exceeds Gridzilla's 25,000-request safety limit")
    if estimated_evidence > profile.max_evidence_bytes:
        raise ValidationError(f"profile may produce ~{estimated_evidence} bytes of raw evidence, above maxEvidenceBytes")
    if estimated_body_memory > 128 * 1024 * 1024:
        raise ValidationError("estimated request body copies multiplied by maxVUs exceed the 128 MiB memory safety estimate")
    return {"scenario": scenario, "windowCount": window_count, "measurementWindowCount": measurement_window_count,
            "warmupWindowCount": warmup_window_count,
            "durationSeconds": total_seconds, "offeredRequests": int(offered),
            "estimatedEvidenceBytes": estimated_evidence, "estimatedRequestMemoryBytes": estimated_body_memory,
            "requestBodySizesBytes": list(selected_sizes), "rates": list(selected_rates),
            "windowSeconds": window_seconds, "warmupSeconds": warmup_seconds}


def load_profile(path: Path) -> Profile:
    raw = load_json(path)
    if raw.get("version") != 1:
        raise ValidationError("profile.version must be 1")
    name = str(_required(raw, "name", "profile"))
    max_rate = float(_required(raw, "maxArrivalRatePerSecond", "profile"))
    preallocated = int(_required(raw, "preallocatedVUs", "profile"))
    max_vus = int(_required(raw, "maxVUs", "profile"))
    concurrency = int(_required(raw, "maxConcurrency", "profile"))
    if not (0 < max_rate <= 100):
        raise ValidationError("maxArrivalRatePerSecond must be in (0, 100]")
    if not (1 <= preallocated <= max_vus <= concurrency <= 100):
        raise ValidationError("VUs must satisfy 1 <= preallocatedVUs <= maxVUs <= maxConcurrency <= 100")
    timeout = str(_required(raw, "requestTimeout", "profile"))
    duration_seconds(timeout, "profile.requestTimeout", maximum=300)
    output_tokens = int(_required(raw, "maxOutputTokens", "profile"))
    response_bytes = int(_required(raw, "maxResponseBytes", "profile"))
    evidence_bytes = int(_required(raw, "maxEvidenceBytes", "profile"))
    if not (1 <= output_tokens <= 4096 and 1 <= response_bytes <= 8 * 1024 * 1024):
        raise ValidationError("output limits exceed hard safety caps")
    if not (1 * 1024 * 1024 <= evidence_bytes <= 512 * 1024 * 1024):
        raise ValidationError("maxEvidenceBytes must be between 1 MiB and 512 MiB")
    warmup = str(_required(raw, "warmup", "profile"))
    window = str(_required(raw, "window", "profile"))
    duration_seconds(warmup, "profile.warmup", maximum=300)
    duration_seconds(window, "profile.window", maximum=600)
    repetitions = int(_required(raw, "repetitions", "profile"))
    if not (3 <= repetitions <= 5):
        raise ValidationError("repetitions must be between 3 and 5 independent windows")
    rates_raw = _required(raw, "rates", "profile")
    if not isinstance(rates_raw, list) or not rates_raw or len(rates_raw) > 6:
        raise ValidationError("profile.rates must contain 1-6 bounded arrival rates")
    rates = tuple(float(rate) for rate in rates_raw)
    if any(rate <= 0 or rate > max_rate for rate in rates):
        raise ValidationError("every rate must be positive and <= maxArrivalRatePerSecond")
    sizes_raw = raw.get("requestBodySizesBytes", [None])
    if (not isinstance(sizes_raw, list) or not sizes_raw or len(sizes_raw) > 6
            or any(size is not None and (isinstance(size, bool) or not isinstance(size, int)
                                         or not 1 <= size <= MAX_REQUEST_BODY_BYTES)
                   for size in sizes_raw)):
        raise ValidationError("requestBodySizesBytes must be up to 6 body sizes between 1 byte and 1 MiB")
    sizes = tuple(sizes_raw)
    poll = str(_required(raw, "pollInterval", "profile"))
    freshness = str(_required(raw, "freshnessLimit", "profile"))
    duration_seconds(poll, "profile.pollInterval", maximum=60)
    duration_seconds(freshness, "profile.freshnessLimit", maximum=300)
    min_samples = int(_required(raw, "percentileMinimumSamples", "profile"))
    if not (20 <= min_samples <= 100000):
        raise ValidationError("percentileMinimumSamples must be between 20 and 100000")
    signal_rate = float(raw.get("signalRate", min(rates[0], max_rate)))
    reference_rate = float(raw.get("directReferenceRate", min(rates[0], max_rate)))
    pressure_target_pool = str(raw.get("pressureTargetPool", "pool-a"))
    if not pressure_target_pool.strip():
        raise ValidationError("pressureTargetPool must identify the Pool A signal target")
    if not (0 < signal_rate <= max_rate and 0 < reference_rate <= max_rate):
        raise ValidationError("signalRate and directReferenceRate must be positive and within max rate")
    profile = Profile(name, max_rate, preallocated, max_vus, concurrency, timeout, output_tokens,
                   response_bytes, evidence_bytes, warmup, window, repetitions, rates, sizes, poll, freshness,
                   min_samples, signal_rate, reference_rate, pressure_target_pool, raw.get("slo"), raw)
    # Parsing a shared profile must not require a Grid handoff or include
    # Grid-only pressure/reference phases. The selected scenario's preflight
    # applies the same planner with its actual phase set before traffic.
    plan_profile(profile, scenario="generic", has_reference=False)
    return profile


def redacted_handoff(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy handoff, preserving credential paths but never resolving values."""
    result = json.loads(json.dumps(raw))
    def prune(value: Any) -> None:
        if isinstance(value, dict):
            for key in list(value):
                if key.startswith("_") or key.lower() in {"token", "secret", "password", "authorization", "apikey"}:
                    del value[key]
                else:
                    prune(value[key])
        elif isinstance(value, list):
            for item in value:
                prune(item)
    prune(result)
    return result


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
