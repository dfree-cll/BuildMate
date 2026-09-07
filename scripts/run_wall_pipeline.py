"""Run or approve the config-driven PDF/DWG wall pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.engines.wall_pipeline import (  # noqa: E402
    approve_revit_write,
    approve_wall_model,
    dry_run_wall_model,
    run_wall_pipeline,
)
from backend.engines.wall_pipeline.audit import REQUIRED_SIMILARITY  # noqa: E402
from backend.engines.wall_pipeline.pipeline import finalize_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build stable wall evidence/model artifacts from PDF or DWG"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("config", type=Path)
    approval_parser = subparsers.add_parser("approve")
    approval_parser.add_argument("wall_model", type=Path)
    approval_parser.add_argument("--actor", required=True)
    approval_parser.add_argument("--reason", required=True)
    approval_parser.add_argument("--reject", action="store_true")
    dry_run_parser = subparsers.add_parser("dry-run")
    dry_run_parser.add_argument("wall_model", type=Path)
    write_parser = subparsers.add_parser("approve-revit")
    write_parser.add_argument("wall_model", type=Path)
    write_parser.add_argument("--actor", required=True)
    write_parser.add_argument("--reason", required=True)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("wall_model", type=Path)
    audit_parser.add_argument("revit_result", type=Path)
    audit_parser.add_argument("--source-render", type=Path)
    audit_parser.add_argument("--overlay", type=Path)
    audit_parser.add_argument(
        "--minimum-edge-iou", type=float, default=REQUIRED_SIMILARITY,
        help="independent edge-IoU floor (cannot be lower than 0.95)",
    )
    args = parser.parse_args()
    if args.command == "run":
        artifacts = run_wall_pipeline(args.config)
        print(json.dumps({key: str(value.resolve()) for key, value in artifacts.items()},
                         ensure_ascii=False, indent=2))
    elif args.command == "approve":
        model = approve_wall_model(
            args.wall_model, actor_id=args.actor, reason=args.reason,
            approved=not args.reject,
        )
        print(json.dumps({
            "wall_model": str(args.wall_model.resolve()),
            "review_status": model.review_status,
            "gate_status": model.gate.status,
        }, ensure_ascii=False, indent=2))
    elif args.command == "dry-run":
        result = dry_run_wall_model(args.wall_model)
        print(result.model_dump_json(indent=2))
    elif args.command == "approve-revit":
        result = approve_revit_write(
            args.wall_model, actor_id=args.actor, reason=args.reason
        )
        print(result.model_dump_json(indent=2))
    else:
        report = finalize_audit(
            args.wall_model, args.revit_result,
            source_render_path=args.source_render,
            overlay_path=args.overlay,
            minimum_edge_iou=args.minimum_edge_iou,
        )
        print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
