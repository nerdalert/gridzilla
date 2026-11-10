"""Safe k6 invocation and result-window accounting."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import signal
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from .models import Profile, duration_seconds
from .runtime import RuntimeErrorChecked, utc_now


class K6Failure(RuntimeError):
    """k6 did not complete a valid bounded measurement window."""


def resolve_k6_binary(expected: str, *, repo_root: Path | None = None) -> str | None:
    """Resolve explicit, repository-pinned, then system k6 in that order."""
    configured = os.environ.get("K6_BIN")
    if configured:
        return configured
    root = repo_root or Path(__file__).resolve().parents[2]
    local = root / ".tools" / f"k6-{expected}" / "k6"
    if local.is_file():
        return str(local)
    return shutil.which("k6")


def verify_k6_version(expected: str) -> tuple[str, str]:
    binary = resolve_k6_binary(expected)
    if not binary:
        raise K6Failure("k6 is not installed; see docs/usage.md for the pinned release")
    result = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=10, check=False)
    match = re.search(r"k6\s+(v[0-9]+\.[0-9]+\.[0-9]+)", result.stdout + result.stderr)
    actual = match.group(1) if match else "unknown"
    if actual != expected:
        raise K6Failure(f"k6 version mismatch: expected {expected}, found {actual} at {binary}")
    if platform.system() == "Linux" and platform.machine().lower() in {"x86_64", "amd64"}:
        pinned_path = Path(__file__).resolve().parents[2] / "tools" / "k6-binary-linux-amd64.sha256"
        expected_binary_sha = pinned_path.read_text(encoding="ascii").split()[0]
        digest = hashlib.sha256()
        with Path(binary).open("rb") as executable:
            for chunk in iter(lambda: executable.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_binary_sha:
            raise K6Failure(f"k6 binary checksum mismatch at {binary}; reinstall the pinned official release")
    else:
        raise K6Failure(f"pinned k6 binary identity is only defined for Linux amd64, got {platform.system()} {platform.machine()}")
    return binary, actual


def _output_token_body(handoff: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    body = json.loads(json.dumps(handoff["client"]["requestBody"]))
    client = handoff["client"]
    token_field = client.get("outputTokenField")
    if not token_field:
        token_field = "max_tokens" if "max_tokens" in body else "max_completion_tokens" if "max_completion_tokens" in body else None
    if token_field:
        original = body.get(token_field)
        body[token_field] = min(int(original), max_tokens) if isinstance(original, (int, float)) else max_tokens
    elif not client.get("fixedResponseBoundBytes"):
        raise K6Failure("handoff request body must expose outputTokenField or fixedResponseBoundBytes")
    if str(body.get("model", "")) != str(client["model"]):
        raise K6Failure("effective request model differs from the handoff model")
    return body


def _resolve_hosts(client: dict[str, Any], url: str) -> dict[str, str]:
    host_map: dict[str, str] = {}
    resolve = client.get("resolve", {})
    if isinstance(resolve, dict):
        for hostname, mapping in resolve.items():
            if isinstance(mapping, str):
                host_map[hostname] = mapping
            elif isinstance(mapping, dict) and mapping.get("address"):
                host_map[hostname] = str(mapping["address"])
    pf = client.get("portForward")
    if pf:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        url_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if int(pf["localPort"]) != url_port:
            raise K6Failure("k6 host mapping requires client.portForward.localPort to equal the Gateway URL port")
        host_map[str(parsed.hostname)] = "127.0.0.1"
    return host_map


def arrival_parameters(requests_per_second: float) -> tuple[int, str]:
    """Encode a fractional open-loop rate using k6's integer rate/timeUnit pair."""
    if not 0 < requests_per_second <= 50:
        raise K6Failure("arrival rate must be in (0, 50] requests per second")
    rate = Fraction(str(requests_per_second)).limit_denominator(1000)
    if abs(float(rate) - requests_per_second) > 1e-9 or rate.numerator > 50_000:
        raise K6Failure(f"arrival rate {requests_per_second} cannot be represented safely by k6")
    return rate.numerator, f"{rate.denominator}s"


def build_k6_command(handoff: dict[str, Any], profile: Profile, *, evidence_dir: Path,
                     phase: str, rate: float, duration: str, index: str,
                     reference: bool = False) -> tuple[list[str], dict[str, str]]:
    repo_root = Path(__file__).resolve().parents[2]
    expected_k6 = repo_root / "tools" / "k6-version"
    # Installed editable/source layout: repo/src/gridzilla/runner.py -> repo/tools.
    expected = expected_k6.read_text(encoding="utf-8").strip()
    binary, _ = verify_k6_version(expected)
    client = handoff["client"]
    endpoint_url = handoff["backendReference"]["url"] if reference else client["url"]
    target_url = urljoin(endpoint_url.rstrip("/") + "/", client["path"].lstrip("/"))
    body = _output_token_body(handoff, profile.max_output_tokens)
    summary_path = evidence_dir / f"{index}-summary.json"
    raw_path = evidence_dir / f"{index}-raw.jsonl"
    script_path = repo_root / "k6" / "gridzilla.js"
    argv = [binary, "run", "--no-color", "--summary-export", str(summary_path), "--out", f"json={raw_path}", str(script_path)]
    attribution = client["attribution"]
    k6_rate, k6_time_unit = arrival_parameters(rate)
    allowed_providers = attribution.get("allowedProviders", attribution.get("allowedSites", []))
    env = os.environ.copy()
    env.update({
        "GZ_URL": target_url,
        "GZ_BODY_JSON": json.dumps(body, separators=(",", ":")),
        "GZ_CREDENTIAL_FILE": "" if reference else str(handoff.get("_resolvedClientCredential") or Path(client["credentialFile"]).expanduser().resolve()),
        "GZ_AUTH_HEADER": str(client.get("authHeader", "Authorization")),
        "GZ_REQUEST_ID_HEADER": str(client.get("requestIdHeader", "X-Request-ID")),
        "GZ_REQUEST_ID_HEADERS": json.dumps(client.get("requestIdHeaders", [client.get("requestIdHeader", "X-Request-ID")])),
        "GZ_ATTRIBUTION_JSON": json.dumps(attribution),
        "GZ_ALLOWED_SITES": json.dumps(attribution["allowedSites"]),
        "GZ_ALLOWED_POOLS": json.dumps(attribution["allowedPools"]),
        "GZ_ALLOWED_BACKENDS": json.dumps(attribution["allowedBackends"]),
        "GZ_ALLOWED_PROVIDERS": json.dumps(allowed_providers),
        "GZ_PROVIDER_TO_SITE": json.dumps(attribution.get("providerToSite", {})),
        "GZ_PROVIDER_TO_POOL": json.dumps(attribution.get("providerToPool", {})),
        "GZ_PROVIDER_TO_BACKEND": json.dumps(attribution.get("providerToBackend", {})),
        "GZ_REQUIRED_RESPONSE_HEADERS": json.dumps(attribution.get("requiredResponseHeaders", [])),
        "GZ_MAX_RESPONSE_BYTES": str(profile.max_response_bytes),
        "GZ_RATE": str(k6_rate),
        "GZ_TIME_UNIT": k6_time_unit,
        "GZ_DURATION": duration,
        "GZ_PREALLOCATED_VUS": str(profile.preallocated_vus),
        "GZ_MAX_VUS": str(profile.max_vus),
        "GZ_REQUEST_TIMEOUT": profile.request_timeout,
        "GZ_PHASE": phase,
        "GZ_CLIENT_SITE": str(client["site"]),
        "GZ_INGRESS_NAME": "direct-simulator-reference" if reference else "combined-site-gateway",
        "GZ_REFERENCE_MODE": "true" if reference else "false",
        "GZ_SUMMARY_PATH": str(summary_path),
        "GZ_HOSTS_JSON": json.dumps({} if reference else _resolve_hosts(client, target_url)),
    })
    ca_file = client.get("caFile")
    if ca_file and not reference:
        env["SSL_CERT_FILE"] = str(Path(ca_file).expanduser().resolve())
    return argv, env


def execute_window(handoff: dict[str, Any], profile: Profile, *, evidence_dir: Path,
                   phase: str, rate: float, duration: str, index: str,
                   reference: bool = False,
                   monitor: Callable[[], str | None] | None = None,
                   stop_event: threading.Event | None = None) -> dict[str, Any]:
    argv, env = build_k6_command(handoff, profile, evidence_dir=evidence_dir,
                                 phase=phase, rate=rate, duration=duration,
                                 index=index, reference=reference)
    timeout = duration_seconds(duration, "traffic window", maximum=600) + duration_seconds(profile.request_timeout, "request timeout", maximum=300) + 45
    log_path = evidence_dir / f"{index}-k6.log"
    started = {"utc": utc_now(), "monotonicNs": time.monotonic_ns()}
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, start_new_session=(os.name == "posix"), bufsize=0)
    markers: list[str] = []
    reader_done = threading.Event()

    def capture() -> None:
        with log_path.open("wb") as output:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, b""):
                output.write(line)
                output.flush()
                decoded = line.decode(errors="replace")
                if "GRIDZILLA_ATTRIBUTION_FAILURE" in decoded or "GRIDZILLA_OUTPUT_LIMIT_FAILURE" in decoded:
                    markers.append(decoded.strip()[:1000])
        reader_done.set()

    thread = threading.Thread(target=capture, name=f"gridzilla-k6-{index}", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    monitor_failure: str | None = None
    try:
        while process.poll() is None:
            if stop_event is not None and stop_event.is_set():
                monitor_failure = "benchmark stop requested"
                _terminate_group(process)
                break
            if markers:
                monitor_failure = markers[0]
                _terminate_group(process)
                break
            if monitor:
                monitor_failure = monitor()
                if monitor_failure:
                    _terminate_group(process)
                    break
            if time.monotonic() >= deadline:
                monitor_failure = f"k6 exceeded {timeout}s safety deadline"
                _terminate_group(process)
                break
            time.sleep(0.5)
        return_code = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            _terminate_group(process)
        if process.stdout:
            process.stdout.close()
        thread.join(timeout=5)
    finished = {"utc": utc_now(), "monotonicNs": time.monotonic_ns()}
    summary_path = evidence_dir / f"{index}-summary.json"
    raw_path = evidence_dir / f"{index}-raw.jsonl"
    summary = None
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = None
    result = {
        "index": index, "phase": phase, "ratePerSecond": rate, "duration": duration,
        "reference": reference, "started": started, "finished": finished,
        "elapsedSeconds": (finished["monotonicNs"] - started["monotonicNs"]) / 1e9,
        "command": argv, "exitCode": return_code, "monitorFailure": monitor_failure,
        "failureMarkers": markers, "summaryPath": summary_path.name if summary else None,
        "rawPath": raw_path.name if raw_path.exists() else None,
        "summary": summary,
    }
    window_result = evidence_dir / f"{index}-window-result.json"
    window_result.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence_bytes = sum(path.stat().st_size for path in evidence_dir.rglob("*") if path.is_file())
    result["evidenceBytesAfterWindow"] = evidence_bytes
    if evidence_bytes > profile.max_evidence_bytes and not result["monitorFailure"]:
        result["monitorFailure"] = (f"evidence directory reached {evidence_bytes} bytes, above the profile's "
                                     f"maxEvidenceBytes={profile.max_evidence_bytes}")
    window_result.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if result["monitorFailure"] or return_code != 0 or summary is None:
        reason = result["monitorFailure"] or (f"k6 exited {return_code}" if summary is not None else "k6 summary missing")
        raise K6Failure(f"window {index} failed: {reason}; see {log_path}")
    return result


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
