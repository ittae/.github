#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


TEXT_TEMPLATES = ("review_followup_runbook.md", "review_followup_webhook_prompt.txt")
DIRECT_COPIES = ("review_followup.py", "reviewer_roster.example.json", "reviewer_roster.schema.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the public review_followup source-of-truth into a Hermes runtime tools directory."
    )
    parser.add_argument(
        "--runtime-dir",
        default=str(Path.home() / ".hermes" / "workspace" / "tools"),
        help="Destination directory for the generated runtime copy.",
    )
    parser.add_argument(
        "--fallback-project-title",
        default="ittae",
        help="Value injected into the webhook prompt template.",
    )
    parser.add_argument(
        "--overwrite-roster",
        action="store_true",
        help="Replace runtime reviewer_roster.json with reviewer_roster.example.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned writes without touching the runtime directory.",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser.parse_args()


def render_template(text: str, replacements: dict[str, str]) -> str:
    rendered = text
    for key, value in replacements.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def record_action(actions: list[dict[str, Any]], source: Path, target: Path, *, mode: str, wrote: bool) -> None:
    actions.append(
        {
            "mode": mode,
            "source": str(source),
            "target": str(target),
            "wrote": wrote,
        }
    )


def write_text(
    source: Path,
    target: Path,
    text: str,
    *,
    dry_run: bool,
    actions: list[dict[str, Any]],
    mode: str,
) -> None:
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    record_action(actions, source, target, mode=mode, wrote=not dry_run)


def copy_file(
    source: Path,
    target: Path,
    *,
    dry_run: bool,
    actions: list[dict[str, Any]],
    mode: str,
) -> None:
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    record_action(actions, source, target, mode=mode, wrote=not dry_run)


def emit_result(actions: list[dict[str, Any]], output: str) -> None:
    if output == "json":
        print(json.dumps({"actions": actions}, ensure_ascii=False, indent=2))
        return
    for action in actions:
        status = "planned" if not action["wrote"] else "wrote"
        print(f"{status}: {action['mode']} {action['source']} -> {action['target']}")


def main() -> int:
    args = parse_args()
    source_dir = Path(__file__).resolve().parent
    runtime_dir = Path(args.runtime_dir).expanduser()
    replacements = {
        "RUNTIME_TOOLS_DIR": str(runtime_dir),
        "REVIEW_FOLLOWUP_PATH": str(runtime_dir / "review_followup.py"),
        "REVIEWER_ROSTER_PATH": str(runtime_dir / "reviewer_roster.json"),
        "MULTICA_GUARD_PATH": str(runtime_dir / "multica_agent_guard.py"),
        "FALLBACK_PROJECT_TITLE": args.fallback_project_title,
    }
    actions: list[dict[str, Any]] = []

    for filename in DIRECT_COPIES:
        source = source_dir / filename
        target = runtime_dir / filename
        copy_file(source, target, dry_run=args.dry_run, actions=actions, mode="copy")

    for filename in TEXT_TEMPLATES:
        source = source_dir / filename
        target = runtime_dir / filename
        rendered = render_template(source.read_text(encoding="utf-8"), replacements)
        write_text(source, target, rendered, dry_run=args.dry_run, actions=actions, mode="render")

    source_example = source_dir / "reviewer_roster.example.json"
    runtime_roster = runtime_dir / "reviewer_roster.json"
    if args.overwrite_roster or not runtime_roster.exists():
        mode = "initialize-roster" if not args.overwrite_roster else "overwrite-roster"
        copy_file(source_example, runtime_roster, dry_run=args.dry_run, actions=actions, mode=mode)

    emit_result(actions, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
