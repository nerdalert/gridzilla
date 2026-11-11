"""Server-owned generic JSON endpoint definitions and safe preflight."""

from __future__ import annotations

import copy
import json
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import MAX_REQUEST_BODY_BYTES, ValidationError

_CONFIG_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SENSITIVE_NAME = re.compile(r"(?:^|[-_])(?:auth|token|secret|password|credential|cookie|request[-_]?id|trace[-_]?id|url|body|key|digest)(?:$|[-_])", re.I)


def _url(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{where} must be a URL string")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError(f"{where} must be an absolute http(s) URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValidationError(f"{where} must not include a query or fragment")
    return value


def _file(value: Any, base: Path, where: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} must be a file path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        result = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{where} does not exist") from exc
    if not result.is_file():
        raise ValidationError(f"{where} is not a regular file")
    return result


def _pointer_parent(value: Any, pointer: str, *, require_string: bool) -> tuple[Any, str | int]:
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 256:
        raise ValidationError("padJsonPointer must be a bounded JSON pointer such as /messages/0/content")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    current: Any = value
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValidationError("padJsonPointer does not resolve to an existing JSON field")
    key = parts[-1]
    if isinstance(current, dict) and key in current:
        resolved_key: str | int = key
    elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
        resolved_key = int(key)
    else:
        raise ValidationError("padJsonPointer does not resolve to an existing JSON field")
    if require_string and not isinstance(current[resolved_key], str):
        raise ValidationError("padJsonPointer must resolve to an existing string field")
    return current, resolved_key


def validate_padded_size(template: dict[str, Any], pointer: str, requested_bytes: int) -> int:
    """Validate a serialized JSON template can be padded to an exact UTF-8 size."""
    if not 1 <= requested_bytes <= MAX_REQUEST_BODY_BYTES:
        raise ValidationError(f"request body size must be between 1 and {MAX_REQUEST_BODY_BYTES} bytes")
    body = copy.deepcopy(template)
    parent, key = _pointer_parent(body, pointer, require_string=True)
    baseline = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if requested_bytes < baseline:
        raise ValidationError(f"requested body size {requested_bytes} is too small for the serialized template ({baseline} bytes)")
    parent[key] += "x" * (requested_bytes - baseline)
    actual = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if actual != requested_bytes:
        raise ValidationError(f"template cannot produce exactly {requested_bytes} serialized UTF-8 bytes")
    return actual


@dataclass(frozen=True)
class GenericEndpoint:
    id: str
    name: str
    url: str
    template: dict[str, Any]
    pad_json_pointer: str | None
    credential_file: Path | None
    credential_header: str | None
    credential_prefix: str
    ca_file: Path | None
    expected_statuses: tuple[int, ...]
    dimensions: dict[str, dict[str, Any]]
    request_id_header: str
    direct_reference: dict[str, Any] | None
    source_path: Path

    def public(self) -> dict[str, Any]:
        parsed = urlsplit(self.url)
        return {"id": self.id, "name": self.name, "scenario": "generic-endpoint",
                "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                "credentialConfigured": self.credential_file is not None,
                "caConfigured": self.ca_file is not None,
                "padJsonPointer": self.pad_json_pointer,
                "expectedStatuses": list(self.expected_statuses),
                "dimensions": {name: {"header": value["header"], "allowedValues": value["allowedValues"]}
                               for name, value in self.dimensions.items()},
                "directReferenceConfigured": self.direct_reference is not None}

    def body_template(self, requested_bytes: int | None = None) -> tuple[str, int]:
        body = copy.deepcopy(self.template)
        if requested_bytes is not None:
            if not self.pad_json_pointer:
                raise ValidationError("this endpoint template has no configured string padJsonPointer")
            validate_padded_size(body, self.pad_json_pointer, requested_bytes)
            parent, key = _pointer_parent(body, self.pad_json_pointer, require_string=True)
            baseline = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            parent[key] += "x" * (requested_bytes - baseline)
        serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        actual = len(serialized.encode("utf-8"))
        if requested_bytes is not None and actual != requested_bytes:
            raise ValidationError(f"template cannot produce exactly {requested_bytes} serialized UTF-8 bytes")
        return serialized, actual


def load_endpoint(path: Path) -> GenericEndpoint:
    source = path.expanduser().resolve(strict=True)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read endpoint configuration {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError("endpoint configuration must be a JSON object")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not _CONFIG_ID.fullmatch(identifier):
        raise ValidationError("endpoint id must be a 2–63 character DNS-safe name")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValidationError("endpoint name must be a nonempty string of at most 100 characters")
    url = _url(raw.get("url"), "endpoint.url")
    template = raw.get("requestTemplate")
    if not isinstance(template, dict):
        raise ValidationError("requestTemplate must be a JSON object")
    # Reject non-JSON values and non-finite numbers before any traffic.
    try:
        json.dumps(template, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("requestTemplate must contain only finite JSON values") from exc
    pad = raw.get("padJsonPointer")
    if pad is not None:
        _pointer_parent(template, pad, require_string=True)
    credential_file = _file(raw.get("credentialFile"), source.parent, "credentialFile")
    credential_header = raw.get("credentialHeader", "Authorization" if credential_file else None)
    if credential_header is not None and (not isinstance(credential_header, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", credential_header)):
        raise ValidationError("credentialHeader must be an HTTP header token")
    prefix = raw.get("credentialPrefix", "Bearer ")
    if not isinstance(prefix, str) or len(prefix) > 64 or "\r" in prefix or "\n" in prefix:
        raise ValidationError("credentialPrefix must be a short single-line string")
    ca_file = _file(raw.get("caFile"), source.parent, "caFile")
    statuses = raw.get("expectedStatuses", [200])
    if (not isinstance(statuses, list) or not statuses or len(statuses) > 10
            or any(isinstance(code, bool) or not isinstance(code, int) or not 200 <= code <= 399 for code in statuses)):
        raise ValidationError("expectedStatuses must list 1–10 HTTP statuses from 200 through 399")
    dims_raw = raw.get("responseHeaderDimensions", {})
    if not isinstance(dims_raw, dict) or len(dims_raw) > 8:
        raise ValidationError("responseHeaderDimensions must be an object with at most 8 dimensions")
    dimensions: dict[str, dict[str, Any]] = {}
    for dimension, config in dims_raw.items():
        if not isinstance(dimension, str) or not _DIMENSION_NAME.fullmatch(dimension) or not isinstance(config, dict):
            raise ValidationError("dimension names must be short lowercase identifiers with a header/allowedValues object")
        header = config.get("header")
        values = config.get("allowedValues")
        if _SENSITIVE_NAME.search(dimension):
            raise ValidationError(f"responseHeaderDimensions.{dimension} cannot be used as an observability dimension")
        if (not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header)
                or not isinstance(values, list) or not values or len(values) > 64
                or any(not isinstance(value, str) or not _DIMENSION_NAME.fullmatch(value) for value in values)):
            raise ValidationError(f"responseHeaderDimensions.{dimension} must define a header and 1–64 bounded allowedValues")
        if _SENSITIVE_NAME.search(header) or any(_SENSITIVE_NAME.search(value) for value in values):
            raise ValidationError(f"responseHeaderDimensions.{dimension} cannot capture credential, request, body, URL, or image identity data")
        dimensions[dimension] = {"header": header, "allowedValues": sorted(set(values))}
    request_id_header = raw.get("requestIdHeader", "X-Request-ID")
    if not isinstance(request_id_header, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", request_id_header):
        raise ValidationError("requestIdHeader must be an HTTP header token")
    direct = raw.get("directReference")
    if direct is not None:
        if not isinstance(direct, dict):
            raise ValidationError("directReference must be an object")
        direct = {"url": _url(direct.get("url"), "directReference.url"),
                  "name": str(direct.get("name", "direct reference"))[:100]}
        direct["credentialFile"] = str(_file(raw.get("directReference", {}).get("credentialFile"), source.parent,
                                             "directReference.credentialFile") or "") or None
        direct["caFile"] = str(_file(raw.get("directReference", {}).get("caFile"), source.parent,
                                     "directReference.caFile") or "") or None
    endpoint = GenericEndpoint(identifier, name, url, copy.deepcopy(template), pad, credential_file,
                               credential_header, prefix, ca_file, tuple(statuses), dimensions,
                               request_id_header, direct, source)
    endpoint.body_template()
    if len(json.dumps(template, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 32 * 1024:
        raise ValidationError("requestTemplate must be at most 32 KiB; padding is generated by k6")
    return endpoint


def load_endpoint_registry(directory: Path | None) -> dict[str, GenericEndpoint]:
    if directory is None:
        return {}
    root = directory.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("endpoint configuration path must be a directory")
    result: dict[str, GenericEndpoint] = {}
    for path in sorted(root.glob("*.json")):
        endpoint = load_endpoint(path)
        if endpoint.id in result:
            raise ValidationError(f"duplicate generic endpoint id: {endpoint.id}")
        result[endpoint.id] = endpoint
    return result


def _read_credential(path: Path | None) -> str:
    if path is None:
        return ""
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\r" in value or "\n" in value:
        raise ValidationError("credential file must contain one nonempty line")
    return value


def endpoint_preflight(endpoint: GenericEndpoint, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    body, body_bytes = endpoint.body_template()
    credential = _read_credential(endpoint.credential_file)
    headers = {"Content-Type": "application/json", endpoint.request_id_header: f"gridzilla-preflight-{uuid.uuid4().hex}"}
    if credential and endpoint.credential_header:
        headers[endpoint.credential_header] = f"{endpoint.credential_prefix}{credential}"
    context = ssl.create_default_context(cafile=str(endpoint.ca_file) if endpoint.ca_file else None)
    request = urllib.request.Request(endpoint.url, data=body.encode("utf-8"), headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds, context=context) as response:
            status = response.status
            headers_seen = response.headers
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers_seen = exc.headers
    except Exception as exc:
        # Do not include request headers, credentials, or body in diagnostics.
        return {"ok": False, "endpointId": endpoint.id, "status": "UNAVAILABLE",
                "error": type(exc).__name__, "durationMs": (time.monotonic() - started) * 1000,
                "requestBodyBytes": body_bytes}
    observed_dimensions = {}
    for name, config in endpoint.dimensions.items():
        value = headers_seen.get(config["header"])
        observed_dimensions[name] = value if value in config["allowedValues"] else None
    return {"ok": status in endpoint.expected_statuses, "endpointId": endpoint.id,
            "status": status, "durationMs": (time.monotonic() - started) * 1000,
            "requestBodyBytes": body_bytes, "dimensions": observed_dimensions,
            "error": None if status in endpoint.expected_statuses else "unexpected_status"}
