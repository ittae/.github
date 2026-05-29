#!/usr/bin/env python3
"""Local Hermes PR Review Gate — dispatcher skeleton (Phase 0, dry-run only).

This is the Phase 0 deliverable of ITT-276 (AI 리뷰 GitHub Action을 로컬
Hermes PR Review Gate로 대체). It ports the `gate-classify` job from
.github/.github/workflows/claude-code-review.yml into a standalone Python
tool that the local Hermes runtime can call.

Phase 0 contract:
    1. Read PR metadata via `gh pr view` / `gh pr diff` (or a JSON fixture
       when `--fixture` is supplied — used by the test suite).
    2. Run the gate-classify logic: size limit + high-risk path matching.
    3. Build the agent dispatch payload (which agents would be triggered
       with what input) using the existing reviewer_roster.json.
    4. Emit the result as JSON on stdout. **No GitHub mutations.** Labels,
       comments, and agent runs are described in the payload, not executed.

Live wiring (gate-classify mutations + agent dispatch) is Phase 1+ and
explicitly out of scope here — calling without `--dry-run` is a hard
error so accidental live use cannot happen.

Approval references:
- Plan comment:    https://github.com/ittae/.github (ITT-276 plan)
- Decision comment: ITT-276 comment 8817fcaa-f8d1-4c1d-9367-b6d471d0cd8b
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

VERSION = "0.1.0"

# Mirrors claude-code-review.yml inputs (defaults section).
DEFAULT_PR_SIZE_LIMIT = 1500
DEFAULT_HIGH_RISK_PATHS = (
    r"lib/features/(auth|secure_storage|iap)/"
    r"|lib/core/(router|constants/storage_keys)"
    r"|^pubspec\.(yaml|lock)$"
    r"|^app_config\.yaml$"
    r"|^android/app/build\.gradle"
    r"|^ios/Runner/Info\.plist$"
    r"|^\.github/workflows/"
    r"|^\.env"
)
DEFAULT_MODEL = "claude-opus-4-7"


class DispatcherError(Exception):
    """Raised for any user-facing failure in the dispatcher."""


@dataclass
class PRRef:
    repo: str
    number: int
    url: str


@dataclass
class PRMetadata:
    ref: PRRef
    title: str
    head_sha: str
    head_ref: str
    base_ref: str
    additions: int
    deletions: int
    files: list[str]
    author: str
    state: str

    @property
    def total_changes(self) -> int:
        return self.additions + self.deletions


@dataclass
class ClassifyResult:
    size_limit: int
    pr_size: int
    high_risk_paths_regex: str
    matched_high_risk_files: list[str]
    verdict: str  # "proceed" | "too-large"
    is_high_risk: bool
    would_apply_labels: list[str] = field(default_factory=list)
    would_post_comments: list[dict[str, str]] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# PR URL parsing
# ─────────────────────────────────────────────────────────────────────────────

_PR_PATH_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<num>\d+)/?$")


def parse_pr_url(pr_url: str) -> PRRef:
    """Parse a GitHub PR URL into (repo, number)."""
    parsed = urlparse(pr_url)
    if parsed.scheme not in ("http", "https") or parsed.netloc != "github.com":
        raise DispatcherError(
            f"PR URL must be a https://github.com/<owner>/<repo>/pull/<n> URL, got: {pr_url!r}"
        )
    match = _PR_PATH_RE.match(parsed.path)
    if not match:
        raise DispatcherError(
            f"PR URL path does not look like /<owner>/<repo>/pull/<n>: {pr_url!r}"
        )
    return PRRef(
        repo=f"{match.group('owner')}/{match.group('repo')}",
        number=int(match.group("num")),
        url=pr_url,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Metadata loading
# ─────────────────────────────────────────────────────────────────────────────

_PR_VIEW_FIELDS = "number,title,additions,deletions,files,headRefOid,headRefName,baseRefName,state,url,author"


def _run_gh_json(args: list[str]) -> Any:
    """Invoke `gh` and parse stdout as JSON. Surfaces stderr on failure."""
    try:
        completed = subprocess.run(
            args, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise DispatcherError(
            "`gh` CLI not found on PATH. Install GitHub CLI or use --fixture."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise DispatcherError(
            f"gh command failed ({' '.join(args)}): {stderr}"
        ) from exc
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DispatcherError(
            f"gh returned non-JSON output for {' '.join(args)}: {completed.stdout[:200]!r}"
        ) from exc


def load_pr_metadata_via_gh(pr_ref: PRRef) -> PRMetadata:
    """Fetch PR metadata via the `gh` CLI."""
    payload = _run_gh_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_ref.number),
            "--repo",
            pr_ref.repo,
            "--json",
            _PR_VIEW_FIELDS,
        ]
    )
    return _pr_metadata_from_payload(pr_ref, payload)


def load_pr_metadata_from_fixture(pr_ref: PRRef, fixture_path: str) -> PRMetadata:
    """Load PR metadata from a JSON fixture (test/replay mode)."""
    try:
        with open(fixture_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise DispatcherError(f"Cannot read fixture {fixture_path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DispatcherError(f"Fixture {fixture_path!r} is not valid JSON: {exc}") from exc
    return _pr_metadata_from_payload(pr_ref, payload)


def _pr_metadata_from_payload(pr_ref: PRRef, payload: dict[str, Any]) -> PRMetadata:
    """Normalize a `gh pr view --json` payload (or fixture) into PRMetadata."""
    files_raw = payload.get("files") or []
    file_paths: list[str] = []
    for entry in files_raw:
        if isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str):
                file_paths.append(path)
        elif isinstance(entry, str):
            file_paths.append(entry)
    author_payload = payload.get("author") or {}
    author_login = ""
    if isinstance(author_payload, dict):
        author_login = author_payload.get("login") or author_payload.get("name") or ""
    elif isinstance(author_payload, str):
        author_login = author_payload
    return PRMetadata(
        ref=pr_ref,
        title=str(payload.get("title", "")),
        head_sha=str(payload.get("headRefOid", "")),
        head_ref=str(payload.get("headRefName", "")),
        base_ref=str(payload.get("baseRefName", "")),
        additions=int(payload.get("additions", 0) or 0),
        deletions=int(payload.get("deletions", 0) or 0),
        files=file_paths,
        author=str(author_login),
        state=str(payload.get("state", "")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# gate-classify port
# ─────────────────────────────────────────────────────────────────────────────

# Comment templates kept verbatim from claude-code-review.yml so dual-run diffs
# stay readable. Both `%s` placeholders are replaced with positional args.
_TOO_LARGE_COMMENT_TEMPLATE = (
    "🚫 **PR 크기 {total}줄 — {limit}줄 초과**\n\n"
    "500줄 넘는 PR은 AI/사람 모두 정확도가 급락합니다. "
    "단일 concern 단위로 분할 후 다시 올려주세요.\n\n"
    "@claude @codex @gemini @copilot 이 PR을 의미 단위로 쪼개서 별도 PR로 다시 만들어주세요."
)
_HIGH_RISK_COMMENT_TEMPLATE = (
    "⚠️ **민감 영역 변경 감지** — AI 리뷰는 진행되지만 자동 머지 라벨"
    "(ai-approved) 부여 안 됨. 사람 머지 필수.\n\n"
    "매치된 파일:\n```\n{matched_list}\n```\n\n"
    "High-risk 영역 (auth, secure_storage, iap, router, storage_keys, pubspec, "
    "native configs, .github/workflows, .env) PR은 점수 통과 시에도 "
    "needs-human-review 라벨 유지."
)


def classify_pr(
    metadata: PRMetadata,
    *,
    pr_size_limit: int = DEFAULT_PR_SIZE_LIMIT,
    high_risk_paths_regex: str = DEFAULT_HIGH_RISK_PATHS,
) -> ClassifyResult:
    """Port of claude-code-review.yml::gate-classify.

    Pure function: no GitHub mutations. The would_apply_labels and
    would_post_comments fields describe what the live phase would do.
    """
    try:
        pattern = re.compile(high_risk_paths_regex)
    except re.error as exc:
        raise DispatcherError(
            f"high_risk_paths_regex is invalid: {exc}"
        ) from exc

    total = metadata.total_changes

    if total > pr_size_limit:
        body = _TOO_LARGE_COMMENT_TEMPLATE.format(total=total, limit=pr_size_limit)
        return ClassifyResult(
            size_limit=pr_size_limit,
            pr_size=total,
            high_risk_paths_regex=high_risk_paths_regex,
            matched_high_risk_files=[],
            verdict="too-large",
            is_high_risk=False,
            would_apply_labels=["too-large"],
            would_post_comments=[{"target": "pr", "body": body}],
        )

    matched = [path for path in metadata.files if pattern.search(path)]
    labels: list[str] = []
    comments: list[dict[str, str]] = []
    if matched:
        labels.extend(["high-risk", "needs-human-review"])
        matched_block = "\n".join(f"- {path}" for path in matched)
        comments.append(
            {
                "target": "pr",
                "body": _HIGH_RISK_COMMENT_TEMPLATE.format(matched_list=matched_block),
            }
        )

    return ClassifyResult(
        size_limit=pr_size_limit,
        pr_size=total,
        high_risk_paths_regex=high_risk_paths_regex,
        matched_high_risk_files=matched,
        verdict="proceed",
        is_high_risk=bool(matched),
        would_apply_labels=labels,
        would_post_comments=comments,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent dispatch payload
# ─────────────────────────────────────────────────────────────────────────────

# Mirrors the 5 perspectives Claude does today in claude-code-review.yml so the
# Hermes-driven dispatch keeps the same review surface during dual-run.
_CLAUDE_PERSPECTIVES = [
    "Security",
    "Architecture",
    "Bug & null-safety",
    "Performance",
    "Test",
]


def load_reviewer_roster(path: str) -> dict[str, Any]:
    """Load the reviewer roster (path-agnostic). Shape mirrors
    ~/.hermes/workspace/tools/reviewer_roster.json."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise DispatcherError(f"Cannot read reviewer roster {path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DispatcherError(f"Reviewer roster {path!r} is not valid JSON: {exc}") from exc


def build_dispatch_payload(
    metadata: PRMetadata,
    classify: ClassifyResult,
    roster: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Build the per-agent dispatch payload. Phase 0: describe only — do not
    actually trigger any agents. The payload is what Phase 1 will hand to
    each agent runtime."""
    reviewers_block = roster.get("reviewers") or {}
    agents: list[dict[str, Any]] = []
    for key, spec in reviewers_block.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("availability") not in (None, "active"):
            continue
        entry: dict[str, Any] = {
            "name": key,
            "display_name": spec.get("display_name", key),
            "role": spec.get("role", "supplementary"),
            "agent_ids": list(spec.get("agent_ids") or []),
            "github_logins": list(spec.get("github_logins") or []),
            "signal_source": spec.get("signal_source"),
        }
        if key == "claude-code":
            entry["model"] = model
            entry["perspectives"] = list(_CLAUDE_PERSPECTIVES)
        agents.append(entry)

    if classify.verdict == "too-large":
        # When too-large, the live phase would skip agent dispatch entirely.
        dispatch_decision = "skip"
        dispatch_reason = "pr-too-large"
    else:
        dispatch_decision = "dispatch"
        dispatch_reason = "high-risk-needs-human-merge" if classify.is_high_risk else "standard"

    return {
        "decision": dispatch_decision,
        "reason": dispatch_reason,
        "agents": agents,
        "agent_input": {
            "repo": metadata.ref.repo,
            "pr_number": metadata.ref.number,
            "pr_url": metadata.ref.url,
            "head_sha": metadata.head_sha,
            "head_ref": metadata.head_ref,
            "base_ref": metadata.base_ref,
            "title": metadata.title,
            "pr_size": metadata.total_changes,
            "is_high_risk": classify.is_high_risk,
            "high_risk_files": classify.matched_high_risk_files,
        },
        "ai_review_meta_template": {
            "agent": "<filled-by-agent>",
            "iteration": 0,
            "score": None,
            "verdict": "PASS",
            "high": 0,
            "medium": 0,
            "low": 0,
            "categories": [],
            "head_sha": metadata.head_sha,
            "head_ref": metadata.head_ref,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Top-level dispatch
# ─────────────────────────────────────────────────────────────────────────────


def run_dispatch(
    *,
    pr_url: str,
    pr_size_limit: int,
    high_risk_paths_regex: str,
    model: str,
    roster_path: str,
    fixture_path: str | None,
) -> dict[str, Any]:
    """Top-level Phase-0 dispatch. Returns the result dict (caller serializes)."""
    pr_ref = parse_pr_url(pr_url)
    if fixture_path:
        metadata = load_pr_metadata_from_fixture(pr_ref, fixture_path)
    else:
        metadata = load_pr_metadata_via_gh(pr_ref)
    classify = classify_pr(
        metadata,
        pr_size_limit=pr_size_limit,
        high_risk_paths_regex=high_risk_paths_regex,
    )
    roster = load_reviewer_roster(roster_path)
    dispatch = build_dispatch_payload(metadata, classify, roster, model=model)
    return {
        "version": VERSION,
        "mode": "dry-run",
        "pr": {
            "repo": metadata.ref.repo,
            "number": metadata.ref.number,
            "url": metadata.ref.url,
            "title": metadata.title,
            "head_sha": metadata.head_sha,
            "head_ref": metadata.head_ref,
            "base_ref": metadata.base_ref,
            "additions": metadata.additions,
            "deletions": metadata.deletions,
            "total": metadata.total_changes,
            "files": metadata.files,
            "author": metadata.author,
            "state": metadata.state,
        },
        "classify": {
            "size_limit": classify.size_limit,
            "pr_size": classify.pr_size,
            "high_risk_paths_regex": classify.high_risk_paths_regex,
            "matched_high_risk_files": classify.matched_high_risk_files,
            "verdict": classify.verdict,
            "is_high_risk": classify.is_high_risk,
            "would_apply_labels": classify.would_apply_labels,
            "would_post_comments": classify.would_post_comments,
        },
        "dispatch": dispatch,
    }


def _default_roster_path() -> str:
    import os

    return os.path.expanduser("~/.hermes/workspace/tools/reviewer_roster.json")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local_pr_review_dispatcher",
        description=(
            "Local Hermes PR Review Gate dispatcher (Phase 0 — dry-run only). "
            "Ports gate-classify from claude-code-review.yml and emits the "
            "agent dispatch payload to stdout. No GitHub mutations."
        ),
    )
    parser.add_argument(
        "--pr-url",
        required=True,
        help="GitHub PR URL, e.g. https://github.com/ittae/ittae/pull/425",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Mandatory in Phase 0. Live wiring lands in Phase 1+.",
    )
    parser.add_argument(
        "--pr-size-limit",
        type=int,
        default=DEFAULT_PR_SIZE_LIMIT,
        help=f"Total (additions+deletions) threshold for too-large verdict (default {DEFAULT_PR_SIZE_LIMIT}).",
    )
    parser.add_argument(
        "--high-risk-paths",
        default=DEFAULT_HIGH_RISK_PATHS,
        help="Regex matched against changed file paths to flag high-risk PRs.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Claude model id for the claude-code agent (default {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--roster",
        default=_default_roster_path(),
        help="Path to reviewer_roster.json (default: ~/.hermes/workspace/tools/reviewer_roster.json).",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="Optional path to a JSON fixture in `gh pr view --json` shape. Bypasses gh CLI.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if not args.dry_run:
        # argparse `required=True` already forces this, but keep the explicit
        # guard so future refactors can't accidentally drop it.
        print("ERROR: --dry-run is required in Phase 0.", file=sys.stderr)
        return 2
    try:
        result = run_dispatch(
            pr_url=args.pr_url,
            pr_size_limit=args.pr_size_limit,
            high_risk_paths_regex=args.high_risk_paths,
            model=args.model,
            roster_path=args.roster,
            fixture_path=args.fixture,
        )
    except DispatcherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
