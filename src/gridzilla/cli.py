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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gridzilla", description="Bounded llm-d/EPP signals routing benchmark")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="validate handoff, tools, ownership, identities, and readiness")
    doctor_parser.add_argument("--handoff", required=True, type=Path)
    doctor_parser.add_argument("--profile", type=Path)
    doctor_parser.add_argument("--evidence-root", type=Path)

    smoke_parser = sub.add_parser("smoke", help="send a few attributed requests through the current Gateway")
    smoke_parser.add_argument("--handoff", required=True, type=Path)
    smoke_parser.add_argument("--count", type=int, default=3)
    smoke_parser.add_argument("--evidence-root", type=Path)

    run_parser = sub.add_parser("run", help="run a bounded baseline/pressure/recovery benchmark")
    run_parser.add_argument("--handoff", required=True, type=Path)
    run_parser.add_argument("--profile", required=True, type=Path)
    run_parser.add_argument("--evidence-root", type=Path)

    restore_parser = sub.add_parser("restore", help="restore captured simulator metrics and verify routing recovery")
    restore_parser.add_argument("--handoff", required=True, type=Path)
    restore_parser.add_argument("--evidence-root", type=Path)

    report_parser = sub.add_parser("report", help="regenerate report and plots from saved evidence only")
    report_parser.add_argument("evidence_directory", type=Path)

    serve_parser = sub.add_parser("serve", help="serve a localhost read-only live and saved-run dashboard")
    serve_parser.add_argument("--handoff", type=Path)
    serve_parser.add_argument("--runs-dir", type=Path, default=Path("evidence"))
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--refresh-seconds", type=float, default=3.0)
    serve_parser.add_argument("--dist", type=Path)
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
            result = run_benchmark(_handoff(args.handoff), load_profile(args.profile), args.profile,
                                   root=args.evidence_root)
            _emit(result)
            return 0 if result["status"] == "PASS" else 2
        if args.command == "restore":
            result = restore_command(_handoff(args.handoff), root=args.evidence_root)
            _emit(result)
            return 0 if result["verified"] else 2
        if args.command == "report":
            result = regenerate_report(args.evidence_directory)
            _emit({"status": result.get("status"), "report": str(args.evidence_directory / "report.md"),
                   "reportData": str(args.evidence_directory / "report-data.json")})
            return 0
        if args.command == "serve":
            if args.host != "127.0.0.1":
                raise ValidationError("Gridzilla UI binds only to 127.0.0.1 to keep cluster credentials local")
            if not 1 <= args.port <= 65535:
                raise ValidationError("port must be between 1 and 65535")
            handoff = _handoff(args.handoff) if args.handoff else None
            from .web import serve
            serve(handoff=handoff, runs_dir=args.runs_dir.resolve(), host=args.host, port=args.port,
                  refresh_seconds=args.refresh_seconds, dist=args.dist)
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
