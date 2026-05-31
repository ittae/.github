#!/usr/bin/env python3
"""Phase 2 dual-run reporter for the local Hermes PR Review Gate.

This tool keeps the Phase 2 contract deliberately narrow:
    1. Run the Phase 0 dispatcher in dry-run mode.
    2. Translate the classify result through the Phase 1 adapter using the
       default DryRunExecutor, so the would-run gh argv is recorded.
    3. Read the existing GitHub Action footprint from PR labels/comments.
    4. Emit a comparison report. No comments, labels, check-runs, or settings
       are created or changed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import local_pr_review_dispatcher as dispatcher
import pr_review_gate_actions as actions

TARGET_GATE_LABELS = ("too-large", "high-risk", "needs-human-review")
VERSION = "0.1.0"


class DualRunError(Exception):
    """Raised for user-facing dual-run failures."""


@dataclass(frozen=True)
class ActionSnapshot:
    labels: tuple[str, ...]
    comments: tuple[dict[str, Any], ...]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "labels": list(self.labels),
            "comments": list(self.comments),
        }


def _run_gh_json(args: list[str]) -> Any:
    try:
        completed = subprocess.run(
            args, check=True, capture_output=True, text=True, encoding="utf-8"
        )
    except FileNotFoundError as exc:
        raise DualRunError("`gh` CLI not found on PATH. Use --action-snapshot-fixture.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise DualRunError(f"gh command failed ({' '.join(args)}): {stderr}") from exc
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DualRunError(
            f"gh returned non-JSON output for {' '.join(args)}: {completed.stdout[:200]!r}"
        ) from exc


def _load_json_file(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise DualRunError(f"Cannot read fixture {path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DualRunError(f"Fixture {path!r} is not valid JSON: {exc}") from exc


def _normalize_label_names(raw_labels: Any) -> tuple[str, ...]:
    if not isinstance(raw_labels, list):
        return ()
    labels: list[str] = []
    for item in raw_labels:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            labels.append(name)
    return tuple(sorted(set(labels)))


def _normalize_comments(raw_comments: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_comments, list):
        return ()
    comments: list[dict[str, Any]] = []
    for item in raw_comments:
        if not isinstance(item, dict):
            continue
        author = item.get("author") or {}
        author_login = ""
        if isinstance(author, dict):
            author_login = str(author.get("login") or author.get("name") or "")
        elif author:
            author_login = str(author)
        comments.append(
            {
                "body": str(item.get("body") or ""),
                "author": author_login,
                "created_at": str(item.get("createdAt") or item.get("created_at") or ""),
                "url": str(item.get("url") or ""),
            }
        )
    return tuple(comments)


def action_snapshot_from_payload(payload: dict[str, Any], *, source: str) -> ActionSnapshot:
    if not isinstance(payload, dict):
        raise DualRunError(
            f"Action snapshot must be a JSON object, got {type(payload).__name__}."
        )
    return ActionSnapshot(
        labels=_normalize_label_names(payload.get("labels")),
        comments=_normalize_comments(payload.get("comments")),
        source=source,
    )


def load_action_snapshot(pr_ref: dispatcher.PRRef, fixture_path: str | None) -> ActionSnapshot:
    if fixture_path:
        return action_snapshot_from_payload(
            _load_json_file(fixture_path),
            source=f"fixture:{fixture_path}",
        )
    payload = _run_gh_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_ref.number),
            "--repo",
            pr_ref.repo,
            "--json",
            "labels,comments",
        ]
    )
    return action_snapshot_from_payload(payload, source="gh:pr-view")


def _normalize_comment_body(body: str) -> str:
    return body.replace("\r\n", "\n")


def _comment_matched(expected_body: str, comments: tuple[dict[str, Any], ...]) -> bool:
    expected = _normalize_comment_body(expected_body).strip()
    if not expected:
        return True
    return any(
        _normalize_comment_body(str(comment.get("body") or "")).strip() == expected
        for comment in comments
    )


def _expected_comment_bodies(classify: dict[str, Any]) -> list[str]:
    comments = classify.get("would_post_comments") or []
    bodies: list[str] = []
    if isinstance(comments, list):
        for item in comments:
            if isinstance(item, dict) and str(item.get("body") or "").strip():
                bodies.append(str(item.get("body")))
    return bodies


def compare_gate_to_action(
    classify: dict[str, Any],
    snapshot: ActionSnapshot,
) -> dict[str, Any]:
    expected_labels = tuple(
        label
        for label in classify.get("would_apply_labels") or []
        if isinstance(label, str) and label.strip()
    )
    expected_label_set = set(expected_labels)
    actual_gate_label_set = set(snapshot.labels).intersection(TARGET_GATE_LABELS)

    expected_bodies = _expected_comment_bodies(classify)
    comment_results = [
        {
            "expected_index": index,
            "matched": _comment_matched(body, snapshot.comments),
            "body_preview": body.strip().splitlines()[0] if body.strip() else "",
        }
        for index, body in enumerate(expected_bodies)
    ]

    missing_labels = sorted(expected_label_set - actual_gate_label_set)
    unexpected_gate_labels = sorted(actual_gate_label_set - expected_label_set)
    missing_comments = [
        result["expected_index"] for result in comment_results if not result["matched"]
    ]
    status = "match"
    if missing_labels or unexpected_gate_labels or missing_comments:
        status = "mismatch"
    elif not expected_label_set and not expected_bodies:
        status = "no_expected_gate_action"

    return {
        "status": status,
        "target_gate_labels": list(TARGET_GATE_LABELS),
        "expected_labels": list(expected_labels),
        "actual_gate_labels": sorted(actual_gate_label_set),
        "missing_labels": missing_labels,
        "unexpected_gate_labels": unexpected_gate_labels,
        "expected_comments": comment_results,
        "missing_comment_indexes": missing_comments,
    }


def _dry_run_actions(dispatch_result: dict[str, Any]) -> list[dict[str, Any]]:
    pr = dispatch_result["pr"]
    target = actions.PRTarget(repo=str(pr["repo"]), number=int(pr["number"]))
    plan = actions.plan_actions_from_classify(dispatch_result["classify"], target)
    return [result.to_dict() for result in actions.apply_actions(plan)]


def run_dual_run(
    *,
    pr_url: str,
    pr_size_limit: int,
    high_risk_paths_regex: str,
    model: str,
    roster_path: str,
    fixture_path: str | None,
    action_snapshot_fixture: str | None,
) -> dict[str, Any]:
    try:
        dispatch_result = dispatcher.run_dispatch(
            pr_url=pr_url,
            pr_size_limit=pr_size_limit,
            high_risk_paths_regex=high_risk_paths_regex,
            model=model,
            roster_path=roster_path,
            fixture_path=fixture_path,
        )
    except dispatcher.DispatcherError as exc:
        raise DualRunError(str(exc)) from exc
    pr_ref = dispatcher.parse_pr_url(pr_url)
    snapshot = load_action_snapshot(pr_ref, action_snapshot_fixture)
    dry_run_results = _dry_run_actions(dispatch_result)
    comparison = compare_gate_to_action(dispatch_result["classify"], snapshot)
    return {
        "version": VERSION,
        "mode": "dry-run",
        "pr": dispatch_result["pr"],
        "local_gate": {
            "classify": dispatch_result["classify"],
            "dispatch": dispatch_result["dispatch"],
            "dry_run_actions": dry_run_results,
        },
        "existing_action_snapshot": snapshot.to_dict(),
        "comparison": comparison,
        "safety": {
            "mutations_enabled": False,
            "live_github_mutations": "disabled",
            "executor": "DryRunExecutor",
        },
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    pr = report["pr"]
    comparison = report["comparison"]
    classify = report["local_gate"]["classify"]
    lines = [
        f"# PR Review Gate Dual-Run Report — {pr['repo']}#{pr['number']}",
        "",
        f"- PR: {pr['url']}",
        f"- head_sha: `{pr.get('head_sha') or ''}`",
        f"- local verdict: `{classify['verdict']}`",
        f"- high-risk: `{classify['is_high_risk']}`",
        f"- comparison: `{comparison['status']}`",
        f"- mutations: `disabled`",
        "",
        "## Labels",
        "",
        f"- expected: {', '.join(comparison['expected_labels']) or 'none'}",
        f"- actual gate labels: {', '.join(comparison['actual_gate_labels']) or 'none'}",
        f"- missing: {', '.join(comparison['missing_labels']) or 'none'}",
        f"- unexpected: {', '.join(comparison['unexpected_gate_labels']) or 'none'}",
        "",
        "## Comments",
        "",
    ]
    if comparison["expected_comments"]:
        for item in comparison["expected_comments"]:
            marker = "matched" if item["matched"] else "missing"
            lines.append(f"- [{marker}] #{item['expected_index']}: {item['body_preview']}")
    else:
        lines.append("- none expected")
    lines.extend(["", "## Dry-Run Commands", ""])
    dry_run_actions = report["local_gate"]["dry_run_actions"]
    if dry_run_actions:
        for item in dry_run_actions:
            command = item.get("command") or []
            lines.append(f"- `{' '.join(str(part) for part in command)}`")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _default_roster_path() -> str:
    return dispatcher._default_roster_path()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local_pr_review_dual_run",
        description=(
            "Compare the local Hermes PR Review Gate dry-run with the existing "
            "GitHub Action labels/comments. Read-only; requires --dry-run."
        ),
    )
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--pr-size-limit", type=int, default=dispatcher.DEFAULT_PR_SIZE_LIMIT)
    parser.add_argument("--high-risk-paths", default=dispatcher.DEFAULT_HIGH_RISK_PATHS)
    parser.add_argument("--model", default=dispatcher.DEFAULT_MODEL)
    parser.add_argument("--roster", default=_default_roster_path())
    parser.add_argument("--fixture", default=None, help="PR metadata fixture for dispatcher replay.")
    parser.add_argument(
        "--action-snapshot-fixture",
        default=None,
        help="Fixture in `gh pr view --json labels,comments` shape.",
    )
    parser.add_argument("--output", choices=("json", "markdown"), default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("ERROR: --dry-run is required for Phase 2 dual-run.", file=sys.stderr)
        return 2
    try:
        report = run_dual_run(
            pr_url=args.pr_url,
            pr_size_limit=args.pr_size_limit,
            high_risk_paths_regex=args.high_risk_paths,
            model=args.model,
            roster_path=args.roster,
            fixture_path=args.fixture,
            action_snapshot_fixture=args.action_snapshot_fixture,
        )
    except DualRunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.output == "markdown":
        sys.stdout.write(render_markdown_report(report))
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
