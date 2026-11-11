"""Gridzilla command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .controller import BenchmarkFailure, StopRequested, doctor, run_benchmark, restore_command, smoke
from .models import ValidationError, load_json, load_profile, validate_handoff
from .report import regenerate_report
from .runtime import RuntimeErrorChecked
from .endpoints import endpoint_preflight, load_endpoint, load_endpoint_registry
from .generic import generic_preflight, run_generic_benchmark


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gridzilla", description="Bounded generic HTTP and Grid signals benchmark")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="validate handoff, tools, ownership, identities, and readiness")
    doctor_parser.add_argument("--handoff", required=True, type=Path)
    doctor_parser.add_argument("--profile", type=Path)
    doctor_parser.add_argument("--evidence-root", type=Path)

    smoke_parser = sub.add_parser("smoke", help="send a few attributed requests through the current Gateway")
    smoke_parser.add_argument("--handoff", required=True, type=Path)
    smoke_parser.add_argument("--count", type=int, default=3)
    smoke_parser.add_argument("--evidence-root", type=Path)

    endpoint_parser = sub.add_parser("endpoint", help="discover or preflight server-owned generic endpoint configurations")
    endpoint_sub = endpoint_parser.add_subparsers(dest="endpoint_command", required=True)
    endpoint_list = endpoint_sub.add_parser("list", help="list validated generic endpoint configurations")
    endpoint_list.add_argument("--config-dir", required=True, type=Path)
    endpoint_preflight_parser = endpoint_sub.add_parser("preflight", help="send one bounded JSON preflight request")
    endpoint_preflight_parser.add_argument("--config-dir", required=True, type=Path)
    endpoint_preflight_parser.add_argument("--id", required=True)
    endpoint_preflight_parser.add_argument("--profile", type=Path)

    run_parser = sub.add_parser("run", help="run a bounded generic endpoint or Grid/llm-d signals scenario")
    run_target = run_parser.add_mutually_exclusive_group(required=True)
    run_target.add_argument("--handoff", type=Path, help="Grid combined-site setup handoff")
    run_target.add_argument("--endpoint-config", type=Path, help="server-owned generic HTTP endpoint JSON")
    run_parser.add_argument("--profile", required=True, type=Path)
    run_parser.add_argument("--evidence-root", type=Path)

    restore_parser = sub.add_parser("restore", help="restore captured simulator metrics and verify routing recovery")
    restore_parser.add_argument("--handoff", required=True, type=Path)
    restore_parser.add_argument("--evidence-root", type=Path)

    report_parser = sub.add_parser("report", help="regenerate report and plots from saved evidence only")
    report_parser.add_argument("evidence_directory", type=Path)

    serve_parser = sub.add_parser("serve", help="serve the localhost live and saved-run dashboard")
    serve_parser.add_argument("--handoff", type=Path)
    serve_parser.add_argument("--runs-dir", type=Path, default=Path("evidence"))
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--refresh-seconds", type=float, default=3.0)
    serve_parser.add_argument("--dist", type=Path)
    serve_parser.add_argument("--allow-controls", action="store_true",
                              help="enable local Start/Stop controls for a loaded Grid handoff or generic endpoint configuration")
    serve_parser.add_argument("--profiles-dir", type=Path,
                              help="directory containing smoke.json, baseline.json, and stepped.json")
    serve_parser.add_argument("--endpoints-dir", type=Path,
                              help="directory of validated generic endpoint JSON configurations")
    serve_parser.add_argument("--agent-token-file", type=Path,
                              help="optional local-only bearer token for JSON agent controls")
    return parser


def _handoff(path: Path) -> dict[str, Any]:
    absolute = path.expanduser().resolve(strict=True)
    return validate_handoff(load_json(absolute), absolute)


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            handoff = _handoff(args.handoff)
            profile = load_profile(args.profile) if args.profile else None
            result = doctor(handoff, profile=profile)
            if args.evidence_root:
                args.evidence_root.mkdir(parents=True, exist_ok=True)
                (args.evidence_root / f"{handoff['runId']}-doctor.json").write_text(json.dumps(result, indent=2) + "\n")
            _emit(result)
            return 0 if result["ok"] else 2
        if args.command == "smoke":
            result = smoke(_handoff(args.handoff), root=args.evidence_root, count=args.count)
            _emit(result)
            return 0 if result["ok"] else 2
        if args.command == "run":
            profile = load_profile(args.profile)
            if args.endpoint_config:
                endpoint = load_endpoint(args.endpoint_config)
                result = run_generic_benchmark(endpoint, profile, args.profile,
                                               root=(args.evidence_root or Path("evidence")))
            else:
                handoff = _handoff(args.handoff)
                result = run_benchmark(handoff, profile, args.profile, root=args.evidence_root)
            _emit(result)
            return 0 if result["status"] == "PASS" else 2
        if args.command == "endpoint":
            registry = load_endpoint_registry(args.config_dir)
            if args.endpoint_command == "list":
                _emit([endpoint.public() for endpoint in registry.values()])
                return 0
            endpoint = registry.get(args.id)
            if endpoint is None:
                raise ValidationError("unknown endpoint id; use `gridzilla endpoint list` first")
            profile = load_profile(args.profile) if args.profile else None
            result = generic_preflight(endpoint, profile)
            _emit(result)
            return 0 if result.get("ok") else 2
        if args.command == "restore":
            result = restore_command(_handoff(args.handoff), root=args.evidence_root)
            _emit(result)
            return 0 if result["verified"] else 2
        if args.command == "report":
            result = regenerate_report(args.evidence_directory)
            _emit({"status": result.get("status"), "report": str(args.evidence_directory / "report.md"),
                   "reportData": str(args.evidence_directory / "report-data.json"),
                   "normalizedReport": str(args.evidence_directory / "normalized-report.json")})
            return 0
        if args.command == "serve":
            if args.host != "127.0.0.1":
                raise ValidationError("Gridzilla UI binds only to 127.0.0.1 to keep cluster credentials local")
            if not 1 <= args.port <= 65535:
                raise ValidationError("port must be between 1 and 65535")
            if args.agent_token_file and not args.allow_controls:
                raise ValidationError("--agent-token-file requires --allow-controls")
            handoff = _handoff(args.handoff) if args.handoff else None
            from .web import serve
            serve(handoff=handoff, runs_dir=args.runs_dir.resolve(), host=args.host, port=args.port,
                  refresh_seconds=args.refresh_seconds, dist=args.dist, allow_controls=args.allow_controls,
                  profiles_dir=args.profiles_dir, endpoints_dir=args.endpoints_dir,
                  agent_token_file=args.agent_token_file)
            return 0
        raise ValidationError(f"unsupported command {args.command!r}")
    except (ValidationError, BenchmarkFailure, RuntimeErrorChecked, OSError, ValueError) as exc:
        print(f"gridzilla: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, StopRequested):
        # run_benchmark installs its own SIGINT handler so simulator recovery runs first.
        print("gridzilla: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
