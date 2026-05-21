#!/usr/bin/env python3
"""Dry-run report for Multica issues that track GitHub PR review follow-up."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
ISSUE_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
ISSUE_MENTION_RE = re.compile(r"mention://issue/([0-9a-fA-F-]{36})")
PLACEHOLDER_VALUE_RE = re.compile(r"^\{[^{}]+\}$")
HERMES_GATE_META_RE = re.compile(r"<!--\s*hermes:pr-review-gate-meta\s*(\{.*?\})\s*-->", re.DOTALL)
HERMES_IGNORED_APPROVAL_META_RE = re.compile(r"<!--\s*hermes:agent-approval-ignored-meta\s*(\{.*?\})\s*-->", re.DOTALL)
AI_REVIEW_META_RE = re.compile(r"<!--\s*ai-review-meta\s*(\{.*?\})\s*-->", re.DOTALL)
VISIBLE_GATE_VALUE_PATTERNS = {
    "pr_url": re.compile(r"^- PR:\s*(https://github\.com/\S+)$", re.MULTILINE),
    "head_sha": re.compile(r"^- head SHA:\s*`?([0-9a-fA-F]{7,40})`?$", re.MULTILINE),
    "ci_state": re.compile(r"^- CI state:\s*`?([a-z_]+)`?", re.MULTILINE),
    "state": re.compile(r"^- state:\s*`?([a-z_]+)`?$", re.MULTILINE),
    "verdict": re.compile(r"^- verdict:\s*`?([a-z_]+)`?$", re.MULTILINE),
    "dedupe_key": re.compile(r"^- dedupe:\s*`?([^`\n]+)`?$", re.MULTILINE),
}
HERMES_GATE_MARKER = "[hermes:pr-review-gate]"
HERMES_APPROVAL_MARKER = "[hermes:approval-needed]"
HERMES_APPROVAL_MIRRORED_MARKER = "[hermes:approval-mirrored]"
HERMES_AGENT_APPROVAL_IGNORED_MARKER = "[hermes:agent-approval-ignored]"
MERGE_STATE_GREEN = {"CLEAN", "HAS_HOOKS", "MERGEABLE"}
MERGE_STATE_APPROVAL_READY = {"CLEAN", "HAS_HOOKS"}
MERGE_STATE_PENDING = {"BEHIND", "DRAFT", "UNKNOWN", "UNSTABLE"}
MERGE_STATE_FAILING = {"BLOCKED", "DIRTY"}
NOTIFIABLE_STATES = {"collecting_reviews", "approval_needed", "needs_agent_fix", "ready_for_approved_merge", "blocked"}

DEFAULT_REVIEWER_PROFILES = [
    {
        "key": "claude-code",
        "name": "Claude",
        "role": "required",
        "availability": "active",
        "legacy_names": ["Claude", "claude", "claude[bot]"],
        "signal_source": ["multica", "github-review"],
        "agent_ids": ["ac215516-af99-4832-b5f8-d8cb99e51260"],
        "github_logins": ["claude", "claude[bot]"],
        "excluded_when_worker": False,
    },
    {
        "key": "codex",
        "name": "Codex",
        "role": "required",
        "availability": "active",
        "legacy_names": ["Codex"],
        "signal_source": "multica",
        "agent_ids": ["cbe053f4-b53e-4786-81de-6554ddb86fad"],
        "github_logins": [],
        "excluded_when_worker": True,
    },
    {
        "key": "gemini",
        "name": "Gemini",
        "role": "supplementary",
        "availability": "active",
        "legacy_names": ["Gemini", "gemini-code-assist"],
        "signal_source": ["multica", "github-review"],
        "agent_ids": ["cc7dd930-ea0f-485f-b74b-134e1da1c2f1"],
        "github_logins": ["gemini-code-assist", "gemini-code-assist[bot]"],
        "excluded_when_worker": False,
    },
    {
        "key": "copilot",
        "name": "Copilot",
        "role": "supplementary",
        "availability": "active",
        "legacy_names": ["Copilot", "copilot-pull-request-reviewer"],
        "signal_source": ["github-review"],
        "agent_ids": ["3d75b4bf-146f-4d4f-91df-81d28577004d"],
        "github_logins": ["copilot-pull-request-reviewer", "copilot-pull-request-reviewer[bot]"],
        "excluded_when_worker": False,
    },
]

DEFAULT_REQUIRED_REVIEWERS = ["Claude", "Codex"]
DEFAULT_SUPPLEMENTARY_REVIEWERS = ["Gemini", "Copilot"]
DEFAULT_REVIEWERS = DEFAULT_REQUIRED_REVIEWERS + DEFAULT_SUPPLEMENTARY_REVIEWERS
DEFAULT_RESOLVE_STATUSES = ["in_review", "in_progress", "blocked", "todo"]
DEFAULT_MERGED_AFTERCARE_STATUSES = ["done", "in_review", "in_progress", "blocked", "todo", "backlog"]
RECOGNIZED_REVIEWERS = {"Claude", "Codex", "Gemini", "Copilot"}
TRIAGE_BUCKETS = ["must-fix", "should-fix", "question", "non-actionable"]
SUPPORTED_REVIEW_SIGNAL_SOURCES = {"multica", "github-review"}
FAILING_CHECK_CONCLUSIONS = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "FAILURE",
    "SKIPPED_FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
FAILING_CHECK_STATES = {"ACTION_REQUIRED", "CANCELLED", "ERROR", "FAILURE", "TIMED_OUT"}
PASSING_CHECK_STATES = {"NEUTRAL", "SKIPPED", "SUCCESS"}
PENDING_CHECK_STATES = {"EXPECTED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}

FOLLOW_UP_HINTS = [
    "architecture",
    "cross-repo",
    "data migration",
    "migration",
    "refactor",
    "schema",
    "separate issue",
    "large",
    "큰",
    "대규모",
    "리팩터",
    "마이그레이션",
    "아키텍처",
    "전반",
    "별도 이슈",
]
PRODUCT_POLICY_HINTS = [
    "product",
    "policy",
    "pricing",
    "plan",
    "rollout",
    "release",
    "security",
    "privacy",
    "legal",
    "compliance",
    "approval",
    "제품",
    "정책",
    "가격",
    "출시",
    "보안",
    "개인정보",
    "법무",
    "승인",
]
RISKY_CHANGE_HINTS = [
    "migration",
    "schema",
    "auth",
    "permission",
    "billing",
    "payment",
    "iap",
    "infra",
    "database",
    "security",
    "delete",
    "rollback",
    "마이그레이션",
    "스키마",
    "권한",
    "결제",
    "인프라",
    "데이터베이스",
    "삭제",
    "롤백",
]
SCOPE_EXPANSION_HINTS = FOLLOW_UP_HINTS + [
    "out of scope",
    "scope creep",
    "bigger than",
    "separate pr",
    "follow-up",
    "후속",
    "범위 확대",
    "스코프",
]
APPROVAL_REASON_LABELS = {
    "ci-failing": "CI failure가 있어 사용자가 수정 방향을 승인해야 함",
    "ci-pending": "CI pending 상태라 자동 병합 판단 전에 사용자가 대기/진행을 결정해야 함",
    "unresolved-must-fix": "high-signal 또는 종합 triage에 unresolved must-fix가 남아 있음",
    "unresolved-review-threads": "GitHub review thread가 아직 unresolved 상태임",
    "claude-codex-conflict": "Claude와 Codex의 결론이 충돌함",
    "product-or-policy-decision": "제품/정책 성격의 결정이 남아 있음",
    "risky-change": "위험도가 높은 변경 범위가 포함됨",
    "scope-expansion": "현재 PR 범위를 넘는 후속 작업이 감지됨",
    "pr-draft": "Draft PR은 `approve merge` 대상으로 인정하지 않음",
    "merge-state-not-clean": "PR `mergeStateStatus`가 `CLEAN` 또는 `HAS_HOOKS`가 아님",
    "non-default-base": "PR base branch가 repo default branch와 다름",
    "stale-head-approval": "승인 시점의 head SHA가 현재 PR head와 달라 재승인이 필요함",
    "fix-approval-missing": "사용자 fix 승인 없이 코드 수정으로 진행할 수 없음",
    "merge-approval-missing": "사용자 merge 승인 없이 자동 병합 후보로 넘길 수 없음",
    "awaiting-user-approval": "다음 자동 액션 전에 사용자 승인 필요",
    "user-held": "사용자가 명시적으로 보류함",
}
APPROVAL_COMMAND_PATTERNS = {
    "fix": re.compile(r"(?im)(?:^|\s)(?:hermes\s+approve\s+fix|\[hermes:approve-fix\])(?:\s|$)"),
    "merge": re.compile(r"(?im)(?:^|\s)(?:hermes\s+approve\s+merge|\[hermes:approve-merge\])(?:\s|$)"),
    "split": re.compile(r"(?im)(?:^|\s)(?:hermes\s+approve\s+split|\[hermes:approve-split\])(?:\s|$)"),
    "hold": re.compile(r"(?im)(?:^|\s)(?:hermes\s+approve\s+hold|\[hermes:approve-hold\]|hermes\s+hold|\[hermes:hold\]|hermes\s+reject)(?:\s|$)"),
}
MERGE_BLOCKING_APPROVAL_REASONS = {
    "ci-failing",
    "ci-pending",
    "unresolved-must-fix",
    "unresolved-review-threads",
    "claude-codex-conflict",
    "product-or-policy-decision",
    "risky-change",
    "pr-draft",
    "merge-state-not-clean",
    "non-default-base",
    "stale-head-approval",
}
APPROVAL_POLICY_NOTE = (
    "승인은 `author_type=member`인 Multica issue comment만 인정합니다. "
    "chat/GitHub 경로는 member-authored mirror comment가 확인되기 전까지 무효입니다."
)
REVIEWER_VERDICT_PATTERNS = (
    ("ready", re.compile(r"ready(?:\s+to)?\s+merge|ready_for_approved_merge", re.IGNORECASE)),
    ("needs_review", re.compile(r"needs another review|needs another pass|needs_agent_fix|approval_needed", re.IGNORECASE)),
    ("blocked", re.compile(r"\bblocked\b", re.IGNORECASE)),
)
GITHUB_REVIEW_STATE_VERDICTS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "needs-fix",
    "REQUEST_CHANGES": "needs-fix",
    "COMMENTED": "commented",
}
GITHUB_REVIEW_VERDICT_PATTERNS = (
    ("blocked", re.compile(r"\bblocked\b|do not merge|must not merge|hold\b", re.IGNORECASE)),
    ("needs-fix", re.compile(r"changes requested|request changes|must[- ]fix|should[- ]fix|critical issue", re.IGNORECASE)),
    ("actionable", re.compile(r"\bactionable\b|follow-up|needs fix|needs another review", re.IGNORECASE)),
    ("unclear", re.compile(r"\bunclear\b|needs clarification|open question", re.IGNORECASE)),
    ("pass", re.compile(r"\bpass\b|ready(?:\s+to)?\s+merge|looks good to me|\blgtm\b", re.IGNORECASE)),
    ("approved", re.compile(r"\bapproved?\b", re.IGNORECASE)),
)
TRACKING_LABEL_NAME = "needs-triage"
TRACKING_LABEL_COLOR = "#6b7280"
TRACKING_LABEL_ID = os.environ.get("REVIEW_FOLLOWUP_TRACKING_LABEL_ID", "").strip() or None
TRACKING_TITLE_PREFIX = "needs-triage:"
TRACKED_UNLINKED_PR_OWNERS = {"ittae"}
EXTERNAL_REPO_RESOLUTION_STATE = "ignored-external-repo"
MERGED_AFTERCARE_MARKER = "[hermes:pr-merged]"
MERGED_AFTERCARE_CLOSEABLE_STATUSES = {"todo", "in_progress", "in_review"}


REVIEW_LOOP_STEPS = [
    "1. PR URL을 구현 이슈 설명 또는 PR/Pull Request/GitHub PR 라벨이 붙은 댓글에 남긴다.",
    "2. Claude/Codex를 high-signal reviewer로, Gemini/Copilot을 supplementary reviewer로 요청한다.",
    "3. Hermes가 Multica 댓글과 GitHub reviewThreads를 수집한다.",
    "4. Hermes가 가중치 기반으로 must-fix, should-fix, question, non-actionable를 종합한다.",
    "5. `autoMergeRequest: null`은 GitHub auto-merge 미설정이라는 정보일 뿐 리뷰 blocker가 아니다.",
    "6. Hermes 자동 병합 후보 판단은 GitHub auto-merge 설정이 아니라 risk/CI/review gate/사용자 정책을 기준으로 한다.",
    "7. 범위가 큰 항목은 별도 Multica 이슈로 쪼개고, 작은 항목만 현재 PR에 반영한다.",
    "8. 수정 후 targeted test/analyze, gh checks, unresolved reviewThreads를 다시 확인한다.",
    "9. CI success + high-signal blocker 없음 + unresolved must-fix 없음 + 사용자 정책 충족일 때만 자동 병합 후보로 본다.",
]


REVIEW_FOCUS_BY_REVIEWER = {
    "Claude": [
        "기능 요구사항 충족 여부",
        "multi-file 구조와 도메인 경계",
        "사용자 흐름/UX 회귀 가능성",
    ],
    "Gemini": [
        "넓은 repo context에서의 의존성 영향",
        "숨은 edge case와 누락된 경로",
        "아키텍처/데이터 흐름 일관성",
    ],
    "Codex": [
        "테스트 가능성, 재현 가능한 버그, regression risk",
        "CI/analyze/test 실패 가능성",
        "작고 바로 반영 가능한 수정 제안",
    ],
    "Copilot": [
        "GitHub PR workflow와 reviewer ergonomics",
        "CI/checks 신호와 GitHub-native thread 관찰",
        "보조적 구현/정리 의견",
    ],
}


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ReviewerProfile:
    key: str
    name: str
    role: str
    availability: str
    legacy_names: tuple[str, ...]
    signal_sources: tuple[str, ...]
    agent_ids: tuple[str, ...]
    github_logins: tuple[str, ...]
    excluded_when_worker: bool


@dataclass(frozen=True)
class ReviewerRoster:
    profiles_by_key: dict[str, ReviewerProfile]
    order: tuple[str, ...]
    alias_to_key: dict[str, str]
    agent_id_to_key: dict[str, str]
    github_login_to_key: dict[str, str]
    source: str


def run(command: list[str]) -> CommandResult:
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return CommandResult(
        ok=completed.returncode == 0,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def run_json(command: list[str]) -> tuple[Any | None, str | None]:
    result = run(command)
    if not result.ok:
        return None, result.stderr or result.stdout
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"invalid-json: {exc}"


def normalize_lookup_token(value: str | None) -> str:
    if value is None:
        return ""
    lowered = value.strip().lower()
    lowered = re.sub(r"[\s_]+", "-", lowered)
    lowered = re.sub(r"[^a-z0-9-]+", "", lowered)
    return lowered.strip("-")


def normalize_role(value: str | None) -> str:
    normalized = normalize_lookup_token(value or "optional")
    aliases = {
        "required": "required",
        "high-signal": "required",
        "highsignal": "required",
        "primary": "required",
        "supplementary": "supplementary",
        "supplemental": "supplementary",
        "secondary": "supplementary",
        "optional": "optional",
        "observer": "optional",
    }
    return aliases.get(normalized, "optional")


def normalize_signal_source(value: str | None) -> str:
    normalized = normalize_lookup_token(value or "multica")
    if normalized in {"multica", "multicacomment", "multica-comments", "multica-comment", "issue-comment"}:
        return "multica"
    if normalized in {
        "github",
        "github-review",
        "githubreview",
        "gh-review",
        "ghreview",
        "pull-request-review",
        "pullrequestreview",
        "pull-request-review-comment",
        "pullrequestreviewcomment",
        "issue-comment-bot",
        "github-comment",
        "githubcomment",
    }:
        return "github-review"
    return normalized or "multica"


def normalize_github_login(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def build_reviewer_roster(entries: list[dict[str, Any]], source: str) -> ReviewerRoster:
    profiles_by_key: dict[str, ReviewerProfile] = {}
    order: list[str] = []
    alias_to_key: dict[str, str] = {}
    agent_id_to_key: dict[str, str] = {}
    github_login_to_key: dict[str, str] = {}

    for raw_entry in entries:
        key = normalize_lookup_token(
            str(raw_entry.get("key") or raw_entry.get("id") or raw_entry.get("name") or "")
        )
        if not key:
            raise ValueError("reviewer roster entry missing key/name")
        if key in profiles_by_key:
            raise ValueError(f"duplicate reviewer key: {key}")

        name = clean_template_value(
            str(raw_entry.get("display_name") or raw_entry.get("name") or raw_entry.get("label") or key)
        ) or key
        role = normalize_role(str(raw_entry.get("role") or "optional"))
        availability = normalize_lookup_token(str(raw_entry.get("availability") or "active")) or "active"
        legacy_names = tuple(dedupe_preserve(normalize_string_list(raw_entry.get("legacy_names"))))
        signal_sources = tuple(
            dedupe_preserve(
                [normalize_signal_source(item) for item in normalize_string_list(raw_entry.get("signal_source"))]
                or [normalize_signal_source(item) for item in normalize_string_list(raw_entry.get("signal_sources"))]
                or ["multica"]
            )
        )
        agent_ids = tuple(
            dedupe_preserve(
                normalize_string_list(raw_entry.get("agent_ids")) or normalize_string_list(raw_entry.get("agent_id"))
            )
        )
        github_logins = tuple(
            dedupe_preserve(
                [
                    login
                    for login in (
                        normalize_github_login(item)
                        for item in (
                            normalize_string_list(raw_entry.get("github_logins"))
                            or normalize_string_list(raw_entry.get("github_login"))
                        )
                    )
                    if login
                ]
            )
        )
        excluded_when_worker = parse_bool_flag(str(raw_entry.get("excluded_when_worker"))) is True
        profile = ReviewerProfile(
            key=key,
            name=name,
            role=role,
            availability=availability,
            legacy_names=legacy_names,
            signal_sources=signal_sources,
            agent_ids=agent_ids,
            github_logins=github_logins,
            excluded_when_worker=excluded_when_worker,
        )
        profiles_by_key[key] = profile
        order.append(key)

        for alias in [key, name, *legacy_names]:
            normalized_alias = normalize_lookup_token(alias)
            if normalized_alias and normalized_alias not in alias_to_key:
                alias_to_key[normalized_alias] = key
        for agent_id in agent_ids:
            if agent_id not in agent_id_to_key:
                agent_id_to_key[agent_id] = key
        for github_login in github_logins:
            if github_login not in github_login_to_key:
                github_login_to_key[github_login] = key

    return ReviewerRoster(
        profiles_by_key=profiles_by_key,
        order=tuple(order),
        alias_to_key=alias_to_key,
        agent_id_to_key=agent_id_to_key,
        github_login_to_key=github_login_to_key,
        source=source,
    )


def load_reviewer_roster_file(path: str) -> ReviewerRoster:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise ValueError(f"reviewer roster read failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"reviewer roster is not valid JSON: {exc}") from exc

    if isinstance(raw, dict):
        raw_reviewers = raw.get("reviewers", raw)
        if isinstance(raw_reviewers, dict):
            entries = [{**value, "key": key} for key, value in raw_reviewers.items() if isinstance(value, dict)]
        elif isinstance(raw_reviewers, list):
            entries = [entry for entry in raw_reviewers if isinstance(entry, dict)]
        else:
            raise ValueError("reviewer roster JSON must contain a reviewers object or list")
    elif isinstance(raw, list):
        entries = [entry for entry in raw if isinstance(entry, dict)]
    else:
        raise ValueError("reviewer roster JSON must be an object or list")

    return build_reviewer_roster(entries, path)


def reviewer_profiles(reviewer_roster: ReviewerRoster | None = None) -> list[ReviewerProfile]:
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    return [roster.profiles_by_key[key] for key in roster.order if key in roster.profiles_by_key]


def resolve_reviewer_key(alias: str | None, reviewer_roster: ReviewerRoster | None = None) -> str | None:
    if not alias:
        return None
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    return roster.alias_to_key.get(normalize_lookup_token(alias))


def reviewer_display_name(alias: str | None, reviewer_roster: ReviewerRoster | None = None) -> str | None:
    key = resolve_reviewer_key(alias, reviewer_roster)
    if not key:
        return None
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    profile = roster.profiles_by_key.get(key)
    return profile.name if profile else None


def reviewer_profiles_for_role(role: str, reviewer_roster: ReviewerRoster | None = None) -> list[ReviewerProfile]:
    return [profile for profile in reviewer_profiles(reviewer_roster) if profile.role == role]


def reviewer_names_for_role(
    role: str,
    reviewer_roster: ReviewerRoster | None = None,
    active_only: bool = False,
) -> list[str]:
    profiles = reviewer_profiles_for_role(role, reviewer_roster)
    if active_only:
        profiles = [profile for profile in profiles if profile.availability == "active"]
    return [profile.name for profile in profiles]


def reviewer_supports_source(profile: ReviewerProfile, signal_source: str | None) -> bool:
    normalized = normalize_signal_source(signal_source)
    return normalized in SUPPORTED_REVIEW_SIGNAL_SOURCES and normalized in profile.signal_sources


def reviewer_supports_any_source(profile: ReviewerProfile) -> bool:
    return any(source in SUPPORTED_REVIEW_SIGNAL_SOURCES for source in profile.signal_sources)


def reviewer_supports_multica(profile: ReviewerProfile) -> bool:
    return reviewer_supports_source(profile, "multica")


def reviewer_supports_gate(profile: ReviewerProfile) -> bool:
    return reviewer_supports_any_source(profile)


def github_auto_merge_state(pr: dict[str, Any] | None) -> tuple[str, str]:
    if not pr or "autoMergeRequest" not in pr:
        return "unknown", "not queried"
    request = pr.get("autoMergeRequest")
    if request is None:
        return "not_enabled", "GitHub auto-merge field is informational; `null` is not a review blocker."
    return "enabled", "GitHub auto-merge is enabled on the PR."


def comment_signal_source(comment: dict[str, Any]) -> str:
    explicit = clean_template_value(str(comment.get("signal_source") or ""))
    if explicit:
        return normalize_signal_source(explicit)
    author_type = normalize_lookup_token(str(comment.get("author_type") or ""))
    if author_type == "github-review":
        return "github-review"
    return "multica"


def build_placeholder_profile(alias: str, role: str) -> ReviewerProfile:
    name = alias.strip() or "unknown-reviewer"
    key = normalize_lookup_token(name) or "unknown-reviewer"
    return ReviewerProfile(
        key=key,
        name=name,
        role=role,
        availability="active",
        legacy_names=(),
        signal_sources=("multica",),
        agent_ids=(),
        github_logins=(),
        excluded_when_worker=False,
    )


def apply_role_overrides(
    reviewer_roster: ReviewerRoster,
    required_reviewers: list[str] | None,
    supplementary_reviewers: list[str] | None,
    source: str,
) -> ReviewerRoster:
    if required_reviewers is None and supplementary_reviewers is None:
        return reviewer_roster

    required_aliases = dedupe_preserve(required_reviewers or [])
    supplementary_aliases = dedupe_preserve(supplementary_reviewers or [])
    profiles_by_key = dict(reviewer_roster.profiles_by_key)
    order = list(reviewer_roster.order)

    def ensure_profile(alias: str, role: str) -> str:
        key = resolve_reviewer_key(alias, reviewer_roster)
        if key:
            return key
        profile = build_placeholder_profile(alias, role)
        if profile.key not in profiles_by_key:
            profiles_by_key[profile.key] = profile
            order.append(profile.key)
        return profile.key

    if required_reviewers is None:
        required_keys = [profile.key for profile in reviewer_profiles_for_role("required", reviewer_roster)]
    else:
        required_keys = [ensure_profile(alias, "required") for alias in required_aliases]
    if supplementary_reviewers is None:
        supplementary_keys = [
            profile.key
            for profile in reviewer_profiles_for_role("supplementary", reviewer_roster)
            if profile.key not in required_keys
        ]
    else:
        supplementary_keys = [ensure_profile(alias, "supplementary") for alias in supplementary_aliases]

    entries: list[dict[str, Any]] = []
    for key in order:
        profile = profiles_by_key[key]
        role = (
            "required"
            if key in required_keys
            else "supplementary"
            if key in supplementary_keys and key not in required_keys
            else "optional"
        )
        updated = replace(profile, role=role)
        entries.append(
            {
                "key": updated.key,
                "display_name": updated.name,
                "role": updated.role,
                "availability": updated.availability,
                "legacy_names": list(updated.legacy_names),
                "signal_sources": list(updated.signal_sources),
                "agent_ids": list(updated.agent_ids),
                "github_logins": list(updated.github_logins),
                "excluded_when_worker": updated.excluded_when_worker,
            }
        )

    return build_reviewer_roster(entries, source)


def load_cli_reviewer_roster(
    reviewer_roster_file: str | None,
    required_reviewers_arg: str | None,
    supplementary_reviewers_arg: str | None,
) -> ReviewerRoster:
    base_roster = load_reviewer_roster_file(reviewer_roster_file) if reviewer_roster_file else DEFAULT_REVIEWER_ROSTER
    required_override = parse_csv(required_reviewers_arg) if required_reviewers_arg else None
    supplementary_override = parse_csv(supplementary_reviewers_arg) if supplementary_reviewers_arg else None
    if not reviewer_roster_file:
        required_override = required_override or DEFAULT_REQUIRED_REVIEWERS
        supplementary_override = supplementary_override or DEFAULT_SUPPLEMENTARY_REVIEWERS
    return apply_role_overrides(base_roster, required_override, supplementary_override, reviewer_roster_file or "legacy-cli")


def reviewer_roster_payload(reviewer_roster: ReviewerRoster) -> dict[str, Any]:
    return {
        "source": reviewer_roster.source,
        "profiles": [
            {
                "key": profile.key,
                "name": profile.name,
                "role": profile.role,
                "availability": profile.availability,
                "legacy_names": list(profile.legacy_names),
                "signal_sources": list(profile.signal_sources),
                "agent_ids": list(profile.agent_ids),
                "github_logins": list(profile.github_logins),
                "excluded_when_worker": profile.excluded_when_worker,
            }
            for profile in reviewer_profiles(reviewer_roster)
        ],
    }


def list_issue_page(status: str, limit: int, offset: int = 0) -> tuple[list[dict[str, Any]], bool, str | None]:
    payload, error = run_json(
        [
            "multica",
            "issue",
            "list",
            "--status",
            status,
            "--limit",
            str(limit),
            "--offset",
            str(offset),
            "--output",
            "json",
        ]
    )
    if error:
        return [], False, error
    if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
        return payload["issues"], bool(payload.get("has_more")), None
    if isinstance(payload, list):
        return payload, False, None
    return [], False, "unexpected-issues-shape"


def list_issues(status: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    issues, _has_more, error = list_issue_page(status, limit, 0)
    return issues, error


def list_issues_for_statuses(statuses: list[str], page_limit: int = 200) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    seen_issue_ids: set[str] = set()
    collected: list[dict[str, Any]] = []
    for status in dedupe_preserve(statuses):
        offset = 0
        while True:
            issues, has_more, error = list_issue_page(status, page_limit, offset)
            if error:
                warnings.append(f"{status}: issue list failed: {error}")
                break
            for issue in issues:
                issue_id = issue.get("id")
                if not isinstance(issue_id, str) or issue_id in seen_issue_ids:
                    continue
                seen_issue_ids.add(issue_id)
                collected.append(issue)
            if not has_more or not issues:
                break
            offset += page_limit
    return collected, warnings


def add_multica_comment(issue_id: str, content: str, parent_id: str | None = None) -> str | None:
    """Add a Multica comment, preferring the local agent-attribution guard.

    Agent/webhook contexts can have both agent-scoped env and persisted member
    auth available. Until the raw Multica CLI attribution fallback is deployed
    everywhere, route agent-context comments through multica_agent_guard.py so a
    member-authored comment fails instead of silently posting with the wrong
    attribution.
    """
    guard = runtime_tools_dir() / "multica_agent_guard.py"
    has_agent_env = all(
        os.environ.get(key, "").strip()
        for key in ("MULTICA_AGENT_ID", "MULTICA_TASK_ID", "MULTICA_TOKEN")
    )
    if has_agent_env and guard.exists():
        command = [sys.executable, str(guard), "comment-add", issue_id, "--content-stdin"]
    else:
        command = ["multica", "issue", "comment", "add", issue_id, "--content-stdin"]
    if parent_id:
        command.extend(["--parent", parent_id])

    result = subprocess.run(
        command,
        check=False,
        text=True,
        input=content,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip() or result.stdout.strip() or "multica comment add failed"


def post_comment_notification(issue_id: str, notification: dict[str, Any]) -> str | None:
    if not notification.get("should_post"):
        return None
    comment_body = clean_template_value(str(notification.get("comment_body") or ""))
    if not comment_body:
        return "notification requested comment post without a body"
    parent_id = clean_template_value(str(notification.get("parent_comment_id") or ""))
    return add_multica_comment(issue_id, comment_body, parent_id)


def post_notification_comment(issue_id: str, notification: dict[str, Any]) -> str | None:
    return post_comment_notification(issue_id, notification)


def update_multica_issue_status(issue_id: str, status: str) -> tuple[dict[str, Any] | None, str | None]:
    payload, error = run_json(
        [
            "multica",
            "issue",
            "update",
            issue_id,
            "--status",
            status,
            "--output",
            "json",
        ]
    )
    if error:
        return None, error
    if isinstance(payload, dict):
        return payload, None
    return None, "unexpected-issue-update-shape"


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def ordered_reviewers(reviewers: list[str], reviewer_roster: ReviewerRoster | None = None) -> list[str]:
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    requested = dedupe_preserve(
        [reviewer_display_name(reviewer, roster) or reviewer for reviewer in reviewers]
    )
    ordered = [profile.name for profile in reviewer_profiles(roster) if profile.name in requested]
    ordered.extend(reviewer for reviewer in requested if reviewer not in ordered)
    return ordered


def clean_template_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"none", "null"}:
        return None
    if PLACEHOLDER_VALUE_RE.match(cleaned):
        return None
    return cleaned


def parse_bool_flag(value: str | None) -> bool | None:
    cleaned = clean_template_value(value)
    if cleaned is None:
        return None
    normalized = cleaned.lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    return None


def issue_lookup_key(issue: dict[str, Any]) -> str:
    return str(issue.get("identifier") or issue.get("id") or "unknown")


def issue_parent_id(issue: dict[str, Any]) -> str | None:
    parent_id = issue.get("parent_issue_id")
    return parent_id if isinstance(parent_id, str) and parent_id else None


def format_issue_mention(issue: dict[str, Any]) -> str:
    issue_id = issue.get("id")
    identifier = issue_lookup_key(issue)
    if isinstance(issue_id, str) and identifier and identifier != issue_id:
        return f"[{identifier}](mention://issue/{issue_id})"
    return identifier


def first_present(*values: str | None) -> str | None:
    for value in values:
        cleaned = clean_template_value(value)
        if cleaned:
            return cleaned
    return None


DEFAULT_REVIEWER_ROSTER = build_reviewer_roster(DEFAULT_REVIEWER_PROFILES, "legacy-default")


def is_human_comment(comment: dict[str, Any]) -> bool:
    return str(comment.get("author_type") or "").lower() not in {"agent", "system"}


def is_member_comment(comment: dict[str, Any]) -> bool:
    return normalize_lookup_token(str(comment.get("author_type") or "")) == "member"


def extract_approval_command(content: str | None) -> str | None:
    for command, pattern in APPROVAL_COMMAND_PATTERNS.items():
        if pattern.search(content or ""):
            return command
    return None


def approval_signal_snippet(content: str | None) -> str | None:
    snippet = clean_template_value(content)
    if not snippet:
        return None
    snippet = snippet.replace("\n", " ")
    if len(snippet) > 140:
        return snippet[:137] + "..."
    return snippet


def canonical_pr_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = parse_pr_url(url)
    if not parsed:
        return None
    repo, number = parsed
    return f"https://github.com/{repo}/pull/{number}"


def repo_owner(repo_full_name: str | None) -> str | None:
    cleaned = clean_template_value(repo_full_name)
    if not cleaned or "/" not in cleaned:
        return None
    owner, _repo = cleaned.split("/", 1)
    normalized = owner.strip().lower()
    return normalized or None


def should_track_unlinked_pr(pr_url: str) -> bool:
    parsed = parse_pr_url(pr_url)
    if not parsed:
        return False
    repo_full_name, _number = parsed
    return repo_owner(repo_full_name) in TRACKED_UNLINKED_PR_OWNERS


def runtime_tools_dir() -> Path:
    return Path(__file__).resolve().parent


def extract_issue_references(text: str | None) -> list[str]:
    if not text:
        return []
    refs = ISSUE_IDENTIFIER_RE.findall(text)
    refs.extend(ISSUE_MENTION_RE.findall(text))
    return dedupe_preserve(refs)


def issue_labels(issue: dict[str, Any]) -> list[dict[str, Any]]:
    labels = issue.get("labels")
    return labels if isinstance(labels, list) else []


def is_tracking_issue(issue: dict[str, Any]) -> bool:
    title = str(issue.get("title") or "").strip().lower()
    if title.startswith(TRACKING_TITLE_PREFIX):
        return True
    for label in issue_labels(issue):
        if (TRACKING_LABEL_ID and label.get("id") == TRACKING_LABEL_ID) or str(label.get("name") or "").strip().lower() == TRACKING_LABEL_NAME:
            return True
    return False


def dedupe_issue_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_issue_ids: set[str] = set()
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        issue_id = candidate.get("id")
        if not isinstance(issue_id, str) or issue_id in seen_issue_ids:
            continue
        seen_issue_ids.add(issue_id)
        output.append(candidate)
    return output


def prefer_non_tracking_matches(matches: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    filtered = dedupe_issue_candidates(matches)
    non_tracking = [match for match in filtered if not match.get("is_tracking_issue")]
    if len(non_tracking) == 1 and len(filtered) > 1:
        ignored = [match.get("identifier") or match.get("id") for match in filtered if match.get("is_tracking_issue")]
        return non_tracking, [f"ignored tracking placeholder issues: {', '.join(ignored)}"]
    return filtered, []


def detect_pr_url(issue: dict[str, Any], comments: list[dict[str, Any]], explicit: str | None) -> str | None:
    if explicit:
        return explicit

    haystacks = [issue.get("description") or ""]

    for text in haystacks:
        match = PR_URL_RE.search(text)
        if match:
            return match.group(0)

    # Comments often contain example commands. Only treat comment URLs as PR links
    # when the line itself labels the URL as real PR context.
    for comment in comments:
        for line in (comment.get("content") or "").splitlines():
            if "review_followup.py" in line or "--pr-url" in line:
                continue
            if not re.search(r"\b(PR|Pull Request|GitHub PR)\b", line, re.IGNORECASE):
                continue
            match = PR_URL_RE.search(line)
            if match:
                return match.group(0)
    return None


def review_sources_from_comments(
    comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> list[str]:
    sources = {profile.name for comment in comments if (profile := reviewer_profile_for_comment(comment, reviewer_roster))}
    return ordered_reviewers(list(sources), reviewer_roster)


def reviewer_profile_for_github_login(
    github_login: str | None,
    reviewer_roster: ReviewerRoster | None = None,
) -> ReviewerProfile | None:
    normalized_login = normalize_github_login(github_login)
    if not normalized_login:
        return None
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    key = roster.github_login_to_key.get(normalized_login)
    if not key:
        return None
    profile = roster.profiles_by_key.get(key)
    if not profile or not reviewer_supports_source(profile, "github-review"):
        return None
    return profile


def add_identity_tokens(tokens: set[str], value: Any) -> None:
    cleaned = clean_template_value(str(value) if value is not None else None)
    if not cleaned:
        return
    lowered = cleaned.lower()
    normalized = normalize_lookup_token(cleaned)
    tokens.add(lowered)
    if normalized:
        tokens.add(normalized)


def reviewer_identity_tokens(profile: ReviewerProfile) -> set[str]:
    tokens: set[str] = set()
    for value in [profile.key, profile.name, *profile.legacy_names, *profile.github_logins, *profile.agent_ids]:
        add_identity_tokens(tokens, value)
    return tokens


def head_commit_identity_tokens(head_commit: dict[str, Any] | None) -> set[str]:
    if not head_commit:
        return set()
    tokens: set[str] = set()
    for value in [
        head_commit.get("author", {}).get("login"),
        head_commit.get("committer", {}).get("login"),
        head_commit.get("commit", {}).get("author", {}).get("name"),
        head_commit.get("commit", {}).get("author", {}).get("email"),
        head_commit.get("commit", {}).get("committer", {}).get("name"),
        head_commit.get("commit", {}).get("committer", {}).get("email"),
    ]:
        add_identity_tokens(tokens, value)
    return tokens


def reviewer_is_self_review_excluded(profile: ReviewerProfile, head_commit: dict[str, Any] | None) -> bool:
    if not profile.excluded_when_worker:
        return False
    reviewer_tokens = reviewer_identity_tokens(profile)
    head_tokens = head_commit_identity_tokens(head_commit)
    return bool(reviewer_tokens and head_tokens and reviewer_tokens.intersection(head_tokens))


def filter_review_comments(
    comments: list[dict[str, Any]],
    excluded_reviewer_keys: set[str],
    reviewer_roster: ReviewerRoster | None = None,
) -> list[dict[str, Any]]:
    if not excluded_reviewer_keys:
        return comments
    filtered: list[dict[str, Any]] = []
    for comment in comments:
        profile = reviewer_profile_for_comment(comment, reviewer_roster)
        if profile and profile.key in excluded_reviewer_keys:
            continue
        filtered.append(comment)
    return filtered


def build_review_gate(
    comments: list[dict[str, Any]],
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    reviewer_roster: ReviewerRoster | None = None,
    head_commit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    roster = reviewer_roster or apply_role_overrides(
        DEFAULT_REVIEWER_ROSTER,
        required_reviewers,
        supplementary_reviewers,
        "legacy-cli",
    )
    required_profiles = reviewer_profiles_for_role("required", roster)
    supplementary_profiles = reviewer_profiles_for_role("supplementary", roster)
    optional_profiles = reviewer_profiles_for_role("optional", roster)

    def split_profiles(
        profiles: list[ReviewerProfile],
        apply_exclusion: bool = False,
    ) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
        active: list[str] = []
        skipped: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for profile in profiles:
            if profile.availability != "active":
                skipped.append(
                    {
                        "key": profile.key,
                        "name": profile.name,
                        "availability": profile.availability,
                        "signal_sources": list(profile.signal_sources),
                        "reason": "unavailable",
                    }
                )
                continue
            if not reviewer_supports_gate(profile):
                skipped.append(
                    {
                        "key": profile.key,
                        "name": profile.name,
                        "availability": profile.availability,
                        "signal_sources": list(profile.signal_sources),
                        "reason": "unsupported-signal-source",
                    }
                )
                continue
            if apply_exclusion and reviewer_is_self_review_excluded(profile, head_commit):
                excluded.append(
                    {
                        "key": profile.key,
                        "name": profile.name,
                        "availability": profile.availability,
                        "signal_sources": list(profile.signal_sources),
                        "reason": "self-review-excluded",
                    }
                )
                continue
            active.append(profile.name)
        return active, skipped, excluded

    required_reviewers, skipped_required, excluded_required = split_profiles(required_profiles, apply_exclusion=True)
    supplementary_reviewers, skipped_supplementary, excluded_supplementary = split_profiles(supplementary_profiles)
    optional_reviewers, skipped_optional, excluded_optional = split_profiles(optional_profiles)
    supplementary_reviewers = [reviewer for reviewer in supplementary_reviewers if reviewer not in required_reviewers]
    excluded_reviewer_keys = {
        entry.get("key")
        for entry in [*excluded_required, *excluded_supplementary, *excluded_optional]
        if isinstance(entry.get("key"), str)
    }
    effective_comments = filter_review_comments(comments, excluded_reviewer_keys, roster)
    present_lookup = set(review_sources_from_comments(effective_comments, roster))
    present = [reviewer for reviewer in required_reviewers if reviewer in present_lookup]
    missing = [reviewer for reviewer in required_reviewers if reviewer not in present_lookup]
    supplementary_present = [reviewer for reviewer in supplementary_reviewers if reviewer in present_lookup]
    supplementary_missing = [reviewer for reviewer in supplementary_reviewers if reviewer not in present_lookup]
    return {
        "required": required_reviewers,
        "present": present,
        "missing": missing,
        "ready": not missing,
        "required_status": "configured" if required_reviewers else "not_configured",
        "required_skipped": skipped_required,
        "required_excluded": excluded_required,
        "supplementary": {
            "configured": supplementary_reviewers,
            "present": supplementary_present,
            "missing": supplementary_missing,
            "status": "configured" if supplementary_reviewers else "not_configured",
            "skipped": skipped_supplementary,
            "excluded": excluded_supplementary,
        },
        "optional": {
            "configured": optional_reviewers,
            "present": [reviewer for reviewer in optional_reviewers if reviewer in present_lookup],
            "missing": [reviewer for reviewer in optional_reviewers if reviewer not in present_lookup],
            "status": "configured" if optional_reviewers else "not_configured",
            "skipped": skipped_optional,
            "excluded": excluded_optional,
        },
        "all_present": ordered_reviewers(list(present_lookup), roster),
        "excluded_reviewer_keys": sorted(excluded_reviewer_keys),
        "roster_source": roster.source,
    }


def classify_check_state(check: dict[str, Any]) -> str:
    state = str(check.get("state") or "").strip().upper()
    conclusion = str(check.get("conclusion") or "").strip().upper()

    if state in FAILING_CHECK_STATES or conclusion in FAILING_CHECK_CONCLUSIONS:
        return "failing"
    if state in PASSING_CHECK_STATES or conclusion in {"NEUTRAL", "SKIPPED", "SUCCESS"}:
        return "passing"
    if state in PENDING_CHECK_STATES or (state and not conclusion):
        return "pending"
    return "passing"


def summarize_checks_by_state(checks: list[dict[str, Any]]) -> dict[str, list[str]]:
    summary = {"failing": [], "pending": []}
    for check in checks:
        bucket = classify_check_state(check)
        if bucket not in summary:
            continue
        name = check.get("name") or "unnamed-check"
        state = check.get("state") or ""
        conclusion = check.get("conclusion") or ""
        summary[bucket].append(f"{name}: {state}/{conclusion}")
    return summary


def actionable_review_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [thread for thread in threads if not thread.get("isResolved") and not thread.get("isOutdated")]


def build_follow_up_summary(
    comments: list[dict[str, Any]],
    blockers: list[str],
    checks: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    reviewer_roster: ReviewerRoster | None = None,
    extra_review_comments: list[dict[str, Any]] | None = None,
    head_commit: dict[str, Any] | None = None,
    pr: dict[str, Any] | None = None,
    current_head_sha: str | None = None,
    default_base_ref: str | None = None,
) -> dict[str, Any]:
    merged_comments = sorted(
        [*comments, *(extra_review_comments or [])],
        key=lambda item: str(item.get("created_at") or ""),
    )
    gate = build_review_gate(
        merged_comments,
        required_reviewers,
        supplementary_reviewers,
        reviewer_roster,
        head_commit=head_commit,
    )
    effective_comments = filter_review_comments(
        merged_comments,
        set(gate.get("excluded_reviewer_keys") or []),
        reviewer_roster,
    )
    triage = extract_review_triage(effective_comments, reviewer_roster)
    check_summary = summarize_checks_by_state(checks)
    current_threads = actionable_review_threads(threads)
    reviewer_triage = extract_reviewer_triage(effective_comments, reviewer_roster)
    reviewer_verdicts = extract_reviewer_verdicts(effective_comments, reviewer_roster)
    reviewer_signals = extract_reviewer_signal_details(merged_comments, reviewer_roster)
    apply_plan = build_apply_plan(triage)
    effective_current_head = first_present(
        clean_template_value(current_head_sha),
        clean_template_value(str((pr or {}).get("headRefOid") or "")),
    )
    approval_state = extract_approval_signal(comments, effective_current_head)
    approval_signal = approval_state["signal"]
    effective_required_reviewers = gate.get("required") or required_reviewers
    review_approval_reasons = build_approval_reasons(
        triage,
        reviewer_triage,
        reviewer_verdicts,
        check_summary,
        current_threads,
        apply_plan,
        effective_required_reviewers,
    )
    merge_candidate_blockers = build_merge_candidate_blockers(
        review_approval_reasons,
        gate["ready"],
        pr,
        default_base_ref,
        approval_signal,
    )
    approval_reasons = dedupe_preserve(
        [*review_approval_reasons, *[reason for reason in merge_candidate_blockers if reason != "missing-required-reviewers"]]
    )
    merge_candidate_ready = is_ready_for_approved_merge(
        merge_candidate_blockers,
    )
    actionable_actions = {"apply-now", "apply-if-low-risk", "create-follow-up-issue"}
    has_actionable_fix = any(entry["action"] in actionable_actions for entry in apply_plan)
    recommendation = recommend_approval_action(
        approval_reasons,
        merge_candidate_ready,
        has_actionable_fix,
        apply_plan,
    )
    approval_request = build_approval_request(
        recommendation,
        approval_reasons,
        merge_candidate_ready,
        apply_plan,
    )
    reasons: list[str] = list(blockers)
    effective_command = approval_signal.get("effective_command")

    if blockers:
        state = "blocked"
    elif not gate["ready"]:
        state = "collecting_reviews"
        reasons.append(f"missing-high-signal-reviewers: {', '.join(gate['missing'])}")
    elif effective_command == "hold":
        state = "approval_needed"
        reasons.extend(approval_reasons or ["user-held"])
    elif merge_candidate_ready and effective_command == "merge":
        state = "ready_for_approved_merge"
    elif has_actionable_fix and effective_command in {"fix", "split"}:
        state = "needs_agent_fix"
    else:
        state = "approval_needed"
        reasons.extend(approval_reasons)
        if merge_candidate_ready and effective_command != "merge":
            reasons.append("merge-approval-missing")
        elif has_actionable_fix and effective_command not in {"fix", "split"}:
            reasons.append("fix-approval-missing")
        elif not reasons:
            reasons.append("awaiting-user-approval")

    reasons = dedupe_preserve(reasons)

    return {
        "state": state,
        "reasons": reasons,
        "gate": gate,
        "failing_checks": check_summary["failing"],
        "pending_checks": check_summary["pending"],
        "actionable_thread_count": len(current_threads),
        "triage_counts": {bucket: len(items) for bucket, items in triage.items()},
        "reviewer_verdicts": reviewer_verdicts,
        "reviewer_signals": reviewer_signals,
        "approval": {
            "required": gate["ready"],
            "reasons": approval_reasons,
            "review_reasons": review_approval_reasons,
            "merge_blockers": merge_candidate_blockers,
            "signal": approval_signal,
            "ignored_signals": approval_state["ignored_signals"],
            "recommendation": recommendation,
            "request": approval_request,
        },
        "merge_candidate_ready": merge_candidate_ready,
    }


def summarize_ci_state(
    checks: list[dict[str, Any]],
    follow_up: dict[str, Any],
    pr: dict[str, Any] | None,
) -> str:
    if follow_up.get("failing_checks"):
        return "failing"
    if follow_up.get("pending_checks"):
        return "pending"
    merge_state = str((pr or {}).get("mergeStateStatus") or "").strip().upper()
    if checks:
        return "green"
    if merge_state in MERGE_STATE_GREEN:
        return "green"
    if merge_state in MERGE_STATE_PENDING:
        return "pending"
    if merge_state in MERGE_STATE_FAILING:
        return "failing"
    return "unknown"


def reviewer_signal_statuses(
    follow_up: dict[str, Any],
    reviewer_roster: ReviewerRoster | None = None,
) -> list[dict[str, Any]]:
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    gate = follow_up.get("gate", {})
    reviewer_signals = follow_up.get("reviewer_signals", {})

    def entries_for_role(
        role: str,
        present: list[str],
        missing: list[str],
        skipped: list[dict[str, Any]],
        excluded: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        present_lookup = set(present)
        missing_lookup = set(missing)
        skipped_lookup = {
            entry.get("key") or entry.get("name"): entry
            for entry in skipped
            if entry.get("key") or entry.get("name")
        }
        excluded_lookup = {
            entry.get("key") or entry.get("name"): entry
            for entry in (excluded or [])
            if entry.get("key") or entry.get("name")
        }
        output: list[dict[str, Any]] = []
        for profile in reviewer_profiles_for_role(role, roster):
            key = profile.key
            skipped_entry = skipped_lookup.get(key) or skipped_lookup.get(profile.name)
            excluded_entry = excluded_lookup.get(key) or excluded_lookup.get(profile.name)
            signal_entry = reviewer_signals.get(profile.name) or {}
            if profile.name in present_lookup:
                status = "responded"
                reason = None
            elif profile.name in missing_lookup:
                status = "awaiting"
                reason = None
            elif excluded_entry:
                status = "excluded"
                reason = excluded_entry.get("reason")
            elif skipped_entry:
                status = "skipped"
                reason = skipped_entry.get("reason")
            else:
                status = "not_configured"
                reason = None
            output.append(
                {
                    "key": profile.key,
                    "name": profile.name,
                    "role": role,
                    "status": status,
                    "reason": reason,
                    "availability": profile.availability,
                    "signal_sources": list(profile.signal_sources),
                    "last_signal_source": signal_entry.get("signal_source"),
                    "verdict": signal_entry.get("verdict"),
                    "normalized_verdict": signal_entry.get("normalized_verdict"),
                }
            )
        return output

    supplementary = gate.get("supplementary", {})
    optional = gate.get("optional", {})
    statuses = entries_for_role(
        "required",
        gate.get("present") or [],
        gate.get("missing") or [],
        gate.get("required_skipped") or [],
        gate.get("required_excluded") or [],
    )
    statuses.extend(
        entries_for_role(
            "supplementary",
            supplementary.get("present") or [],
            supplementary.get("missing") or [],
            supplementary.get("skipped") or [],
            supplementary.get("excluded") or [],
        )
    )
    statuses.extend(
        entries_for_role(
            "optional",
            optional.get("present") or [],
            optional.get("missing") or [],
            optional.get("skipped") or [],
            optional.get("excluded") or [],
        )
    )
    return statuses


def notification_verdict(follow_up: dict[str, Any]) -> str:
    state = follow_up.get("state")
    recommendation = str(follow_up.get("approval", {}).get("recommendation") or "").strip()
    if state == "ready_for_approved_merge":
        return "merge"
    if state == "needs_agent_fix":
        return recommendation or "fix"
    if state == "approval_needed":
        return recommendation or "hold"
    if state == "blocked":
        return "blocked"
    return "hold"


def build_event_key(
    pr_url: str | None,
    head_sha: str | None,
    event_name: str | None,
    event_action: str | None,
    review_id: str | None,
    comment_id: str | None,
) -> str | None:
    canonical = canonical_pr_url(pr_url)
    normalized_head = clean_template_value(head_sha)
    payload = {
        "pr_url": canonical,
        "head_sha": normalized_head,
        "event_name": clean_template_value(event_name),
        "event_action": clean_template_value(event_action),
        "review_id": clean_template_value(review_id),
        "comment_id": clean_template_value(comment_id),
    }
    if not any(payload.values()):
        return None
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"event:{digest}"


def build_gate_dedupe_key(
    pr_url: str | None,
    head_sha: str | None,
    follow_up: dict[str, Any],
    ci_state: str,
    reviewer_statuses: list[dict[str, Any]],
) -> str:
    canonical = canonical_pr_url(pr_url) or "unknown-pr"
    parsed = parse_pr_url(canonical)
    repo = parsed[0] if parsed else canonical
    number = str(parsed[1]) if parsed else "unknown"
    normalized_head = clean_template_value(head_sha) or "unknown-head"
    verdict = notification_verdict(follow_up)
    reviewer_snapshot = [
        {
            "key": entry.get("key"),
            "role": entry.get("role"),
            "status": entry.get("status"),
            "reason": entry.get("reason"),
            "availability": entry.get("availability"),
            "last_signal_source": entry.get("last_signal_source"),
            "verdict": entry.get("verdict"),
            "normalized_verdict": entry.get("normalized_verdict"),
        }
        for entry in reviewer_statuses
    ]
    payload = {
        "pr_url": canonical,
        "head_sha": normalized_head,
        "state": follow_up.get("state"),
        "ci_state": ci_state,
        "verdict": verdict,
        "reviewers": reviewer_snapshot,
        "approval_reasons": sorted(follow_up.get("approval", {}).get("reasons") or []),
    }
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return (
        f"pr:{repo}#{number}:sha:{normalized_head}:state:{follow_up.get('state')}"
        f":ci:{ci_state}:verdict:{verdict}:reviewers:{digest}"
    )


def issue_mention(issue: dict[str, Any]) -> str:
    identifier = issue.get("identifier") or issue.get("id") or "unknown"
    issue_id = issue.get("id")
    if issue_id:
        return f"[{identifier}](mention://issue/{issue_id})"
    return str(identifier)


def event_summary_line(
    event_name: str | None,
    event_action: str | None,
    review_author: str | None,
    comment_author: str | None,
    review_state: str | None,
) -> str:
    parts = [clean_template_value(event_name) or "github-webhook", clean_template_value(event_action) or "activity"]
    author = first_present(review_author, comment_author)
    state = clean_template_value(review_state)
    if author:
        parts.append(f"by {author}")
    if state:
        parts.append(state)
    return " / ".join(parts)


def format_reviewer_status(entry: dict[str, Any]) -> str:
    role = entry.get("role") or "unknown"
    key = entry.get("key") or entry.get("name") or "unknown"
    name = entry.get("name") or key
    status = entry.get("status") or "unknown"
    reason = entry.get("reason")
    signal_source = entry.get("last_signal_source")
    verdict = entry.get("normalized_verdict") or entry.get("verdict")
    if status == "responded":
        detail_parts = ["responded"]
        if signal_source:
            detail_parts.append(f"via {signal_source}")
        if verdict:
            detail_parts.append(f"verdict={verdict}")
        detail = " / ".join(detail_parts)
    elif status == "excluded" and reason:
        if signal_source and verdict:
            detail = f"excluded ({reason}; via {signal_source}; verdict={verdict})"
        elif signal_source:
            detail = f"excluded ({reason}; via {signal_source})"
        else:
            detail = f"excluded ({reason})"
    elif status == "skipped" and reason == "unavailable":
        detail = f"skipped ({entry.get('availability') or 'unknown'})"
    elif status == "skipped" and reason:
        sources = ", ".join(entry.get("signal_sources") or []) or "unknown"
        detail = f"skipped ({reason}: {sources})"
    else:
        detail = status
    return f"- `{key}` / {name} / {role}: `{detail}`"


def next_action_text(follow_up: dict[str, Any]) -> str:
    state = follow_up.get("state")
    missing = ", ".join(follow_up.get("gate", {}).get("missing") or [])
    if state == "collecting_reviews":
        return f"{missing or 'required high-signal reviewer'} 신호를 기다립니다."
    if state == "approval_needed":
        return "사람이 `hermes approve merge|fix|split|hold` 중 하나로 결정합니다."
    if state == "needs_agent_fix":
        return "승인된 범위 안에서 worker 후속 수정 또는 분리 작업을 진행합니다."
    if state == "ready_for_approved_merge":
        return "현재 상태는 승인된 merge 후보입니다. GitHub write는 사용자 정책에 따라 별도 진행합니다."
    if state == "blocked":
        reasons = ", ".join(follow_up.get("reasons") or [])
        return reasons or "운영 blocker를 먼저 해소해야 합니다."
    return "현재 상태를 유지합니다."


def render_gate_metadata(payload: dict[str, Any]) -> str:
    return (
        "<!-- hermes:pr-review-gate-meta "
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + " -->"
    )


def render_notification_comment(
    issue: dict[str, Any],
    pr_url: str | None,
    head_sha: str | None,
    follow_up: dict[str, Any],
    ci_state: str,
    reviewer_statuses: list[dict[str, Any]],
    dedupe_key: str,
    event_name: str | None,
    event_action: str | None,
    review_author: str | None,
    comment_author: str | None,
    review_state: str | None,
    event_key: str | None,
    parent_comment_id: str | None,
    pr: dict[str, Any] | None = None,
) -> str:
    state = follow_up.get("state") or "unknown"
    verdict = notification_verdict(follow_up)
    normalized_head = clean_template_value(head_sha) or "unknown"
    reviewer_lines = [format_reviewer_status(entry) for entry in reviewer_statuses if entry.get("role") != "optional"]
    auto_merge_state, auto_merge_note = github_auto_merge_state(pr)
    if not reviewer_lines:
        reviewer_lines = ["- reviewer roster unavailable"]
    intro = "PR 리뷰 게이트 상태를 정정합니다. 아래 상태가 최신입니다." if parent_comment_id else "PR 리뷰 게이트 상태를 갱신합니다."
    output = [
        HERMES_GATE_MARKER,
        intro,
        "",
        f"- event: {event_summary_line(event_name, event_action, review_author, comment_author, review_state)}",
        f"- PR: {canonical_pr_url(pr_url) or 'missing'}",
        f"- Linked issue: {issue_mention(issue)}",
        f"- head SHA: `{normalized_head}`",
        f"- CI state: `{ci_state}`",
        f"- GitHub auto-merge: `{auto_merge_state}`",
        f"- state: `{state}`",
        f"- verdict: `{verdict}`",
        f"- dedupe: `{dedupe_key}`",
        "",
        "Reviewer roster status:",
        *reviewer_lines,
        "",
        "Next action:",
        f"- {next_action_text(follow_up)}",
        "",
        "Notes:",
        f"- {auto_merge_note}",
    ]
    approval = follow_up.get("approval", {})
    if state == "approval_needed":
        request = approval.get("request", {})
        output.extend(["", HERMES_APPROVAL_MARKER])
        for reason in approval.get("reasons") or []:
            output.append(f"- reason: {APPROVAL_REASON_LABELS.get(reason, reason)}")
        for option in request.get("options") or []:
            output.append(f"- option {option['label']}: `{option['command']}` - {option['summary']}")
        if request.get("recommended_command"):
            output.append(f"- recommendation: `{request['recommended_command']}`")
        if request.get("policy_note"):
            output.append(f"- policy: {request['policy_note']}")

    metadata_payload = {
        "version": 1,
        "kind": "pr-review-gate",
        "issue_id": issue.get("id"),
        "pr_url": canonical_pr_url(pr_url),
        "head_sha": normalized_head,
        "state": state,
        "ci_state": ci_state,
        "verdict": verdict,
        "dedupe_key": dedupe_key,
        "event_key": event_key,
        "approval_reasons": sorted(approval.get("reasons") or []),
        "parent_comment_id": parent_comment_id,
    }
    output.extend(["", render_gate_metadata(metadata_payload)])
    return "\n".join(output)


def extract_gate_metadata(comment: dict[str, Any]) -> dict[str, Any] | None:
    content = comment.get("content") or ""
    if HERMES_GATE_MARKER not in content and HERMES_APPROVAL_MARKER not in content:
        return None
    match = HERMES_GATE_META_RE.search(content)
    if match:
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}
    for key, pattern in VISIBLE_GATE_VALUE_PATTERNS.items():
        if payload.get(key):
            continue
        visible = pattern.search(content)
        if visible:
            payload[key] = visible.group(1).strip()
    reasons = re.findall(r"^- reason:\s*(.+)$", content, re.MULTILINE)
    if reasons and not payload.get("approval_reasons"):
        payload["approval_reasons"] = dedupe_preserve(reasons)
    payload["comment_id"] = comment.get("id")
    payload["parent_id"] = comment.get("parent_id")
    payload["created_at"] = comment.get("created_at")
    payload["author_id"] = comment.get("author_id")
    return payload


def gate_metadata_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for comment in comments:
        metadata = extract_gate_metadata(comment)
        if metadata:
            output.append(metadata)
    output.sort(key=lambda item: str(item.get("created_at") or ""))
    return output


def approval_head_sha_for_comment(
    comment: dict[str, Any],
    comments_by_id: dict[str, dict[str, Any]],
    gate_history: list[dict[str, Any]],
    current_head_sha: str | None,
) -> str | None:
    explicit_head = clean_template_value(str(comment.get("head_sha") or ""))
    if explicit_head:
        return explicit_head

    parent_id = clean_template_value(str(comment.get("parent_id") or ""))
    while parent_id:
        parent = comments_by_id.get(parent_id)
        if not parent:
            break
        parent_meta = extract_gate_metadata(parent)
        if parent_meta:
            parent_head = clean_template_value(str(parent_meta.get("head_sha") or ""))
            if parent_head:
                return parent_head
        parent_id = clean_template_value(str(parent.get("parent_id") or ""))

    created_at = str(comment.get("created_at") or "")
    prior_gates = [entry for entry in gate_history if str(entry.get("created_at") or "") <= created_at]
    if prior_gates:
        prior_head = clean_template_value(str(prior_gates[-1].get("head_sha") or ""))
        if prior_head:
            return prior_head

    return clean_template_value(current_head_sha)


def build_agent_approval_ignored_dedupe_key(
    issue_id: str | None,
    head_sha: str | None,
    author_id: str | None,
    command: str | None,
) -> str:
    payload = {
        "issue_id": clean_template_value(issue_id) or "unknown-issue",
        "head_sha": clean_template_value(head_sha) or "unknown-head",
        "author_id": clean_template_value(author_id) or "unknown-author",
        "command": clean_template_value(command) or "unknown-command",
    }
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return (
        f"issue:{payload['issue_id']}:head:{payload['head_sha']}:"
        f"agent:{payload['author_id']}:command:{payload['command']}:dedupe:{digest}"
    )


def render_agent_approval_ignored_metadata(payload: dict[str, Any]) -> str:
    return (
        "<!-- hermes:agent-approval-ignored-meta "
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + " -->"
    )


def extract_agent_approval_ignored_metadata(comment: dict[str, Any]) -> dict[str, Any] | None:
    content = comment.get("content") or ""
    if HERMES_AGENT_APPROVAL_IGNORED_MARKER not in content:
        return None
    match = HERMES_IGNORED_APPROVAL_META_RE.search(content)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        payload = {}
    payload["comment_id"] = comment.get("id")
    payload["created_at"] = comment.get("created_at")
    payload["parent_id"] = comment.get("parent_id")
    return payload


def agent_approval_ignored_metadata_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for comment in comments:
        metadata = extract_agent_approval_ignored_metadata(comment)
        if metadata:
            output.append(metadata)
    output.sort(key=lambda item: str(item.get("created_at") or ""))
    return output


def same_gate_semantics(existing: dict[str, Any], state: str, ci_state: str, verdict: str, dedupe_key: str, approval_reasons: list[str]) -> bool:
    existing_reasons = sorted(existing.get("approval_reasons") or [])
    return (
        str(existing.get("state") or "") == state
        and str(existing.get("ci_state") or "") == ci_state
        and str(existing.get("verdict") or "") == verdict
        and (
            str(existing.get("dedupe_key") or "") == dedupe_key
            or existing_reasons == sorted(approval_reasons)
        )
    )


def build_notification_policy(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    pr_url: str | None,
    head_sha: str | None,
    follow_up: dict[str, Any],
    pr: dict[str, Any] | None,
    checks: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
    event_name: str | None = None,
    event_action: str | None = None,
    review_author: str | None = None,
    comment_author: str | None = None,
    review_state: str | None = None,
    review_id: str | None = None,
    comment_id: str | None = None,
) -> dict[str, Any]:
    state = str(follow_up.get("state") or "")
    ci_state = summarize_ci_state(checks, follow_up, pr)
    reviewer_statuses = reviewer_signal_statuses(follow_up, reviewer_roster)
    verdict = notification_verdict(follow_up)
    dedupe_key = build_gate_dedupe_key(pr_url, head_sha, follow_up, ci_state, reviewer_statuses)
    event_key = build_event_key(pr_url, head_sha, event_name, event_action, review_id, comment_id)
    normalized_pr = canonical_pr_url(pr_url)
    normalized_head = clean_template_value(head_sha)
    approval_reasons = sorted(follow_up.get("approval", {}).get("reasons") or [])
    existing_comments = gate_metadata_comments(comments)
    relevant = [
        item
        for item in existing_comments
        if (not normalized_pr or item.get("pr_url") in {None, normalized_pr})
        and (not normalized_head or item.get("head_sha") in {None, normalized_head})
    ]
    latest_same_head = relevant[-1] if relevant else None
    should_post = state in NOTIFIABLE_STATES
    mode = "top_level"
    parent_comment_id: str | None = None
    suppression_reason: str | None = None
    decision_reason: str | None = None

    if not should_post:
        suppression_reason = f"state-not-notifiable:{state}"
    elif event_key and any(item.get("event_key") == event_key for item in relevant):
        should_post = False
        suppression_reason = "duplicate-event-key"
    elif state == "collecting_reviews":
        if latest_same_head and latest_same_head.get("state") not in {"collecting_reviews"}:
            mode = "reply"
            parent_comment_id = latest_same_head.get("comment_id")
            decision_reason = "reply-correction-for-misclassified-same-head"
        elif relevant:
            should_post = False
            suppression_reason = "same-head-collecting_reviews-already-notified"
        else:
            decision_reason = "first-collecting_reviews-notification-for-head"
    elif latest_same_head and same_gate_semantics(latest_same_head, state, ci_state, verdict, dedupe_key, approval_reasons):
        should_post = False
        suppression_reason = "duplicate-semantic-state"
    else:
        decision_reason = f"state-transition:{state}"

    comment_body = None
    if should_post:
        comment_body = render_notification_comment(
            issue,
            pr_url,
            head_sha,
            follow_up,
            ci_state,
            reviewer_statuses,
            dedupe_key,
            event_name,
            event_action,
            review_author,
            comment_author,
            review_state,
            event_key,
            parent_comment_id,
            pr,
        )

    return {
        "should_post": should_post,
        "mode": mode if should_post else "skip",
        "parent_comment_id": parent_comment_id,
        "reason": decision_reason,
        "suppression_reason": suppression_reason,
        "ci_state": ci_state,
        "verdict": verdict,
        "dedupe_key": dedupe_key,
        "event_key": event_key,
        "reviewer_statuses": reviewer_statuses,
        "latest_same_head_comment_id": latest_same_head.get("comment_id") if latest_same_head else None,
        "comment_body": comment_body,
    }


def render_agent_approval_ignored_comment(
    issue: dict[str, Any],
    approval_signal: dict[str, Any],
    head_sha: str | None,
    dedupe_key: str,
) -> str:
    observed_command = approval_option(str(approval_signal.get("command") or "hold")).get("command")
    normalized_head = clean_template_value(head_sha) or clean_template_value(str(approval_signal.get("head_sha") or "")) or "unknown"
    payload = {
        "version": 1,
        "kind": "agent-approval-ignored",
        "issue_id": issue.get("id"),
        "head_sha": normalized_head,
        "author_id": approval_signal.get("author_id"),
        "author_type": approval_signal.get("author_type"),
        "command": approval_signal.get("command"),
        "dedupe_key": dedupe_key,
        "source_comment_id": approval_signal.get("comment_id"),
    }
    lines = [
        HERMES_AGENT_APPROVAL_IGNORED_MARKER,
        "이 approval 명령은 정책상 무시했습니다.",
        "",
        f"- linked issue: {issue_mention(issue)}",
        f"- observed author: `{approval_signal.get('author_type') or 'unknown'}` / `{approval_signal.get('author_id') or 'unknown'}`",
        f"- observed command: `{observed_command}`",
        f"- head SHA: `{normalized_head}`",
        f"- reason: {APPROVAL_POLICY_NOTE}",
        "- next action: 사람이 linked issue에 `hermes approve merge|fix|split|hold` 중 하나로 다시 남겨 주세요.",
        "",
        render_agent_approval_ignored_metadata(payload),
    ]
    return "\n".join(lines)


def build_agent_approval_ignored_notification(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    head_sha: str | None,
    follow_up: dict[str, Any],
) -> dict[str, Any]:
    ignored_signals = follow_up.get("approval", {}).get("ignored_signals") or []
    if not ignored_signals:
        return {
            "should_post": False,
            "mode": "skip",
            "parent_comment_id": None,
            "suppression_reason": "no-agent-approval-attempt",
            "dedupe_key": None,
            "comment_body": None,
        }

    existing = agent_approval_ignored_metadata_comments(comments)
    issue_id = clean_template_value(str(issue.get("id") or issue.get("identifier") or ""))
    normalized_head = clean_template_value(head_sha) or clean_template_value(
        str((follow_up.get("approval", {}).get("signal") or {}).get("current_head_sha") or "")
    )

    for approval_signal in reversed(ignored_signals):
        dedupe_key = build_agent_approval_ignored_dedupe_key(
            issue_id,
            clean_template_value(str(approval_signal.get("head_sha") or "")) or normalized_head,
            clean_template_value(str(approval_signal.get("author_id") or "")),
            clean_template_value(str(approval_signal.get("command") or "")),
        )
        if any(item.get("dedupe_key") == dedupe_key for item in existing):
            continue
        return {
            "should_post": True,
            "mode": "reply" if approval_signal.get("comment_id") else "top_level",
            "parent_comment_id": approval_signal.get("comment_id"),
            "suppression_reason": None,
            "dedupe_key": dedupe_key,
            "comment_body": render_agent_approval_ignored_comment(
                issue,
                approval_signal,
                normalized_head,
                dedupe_key,
            ),
        }

    return {
        "should_post": False,
        "mode": "skip",
        "parent_comment_id": None,
        "suppression_reason": "duplicate-agent-approval-ignored",
        "dedupe_key": None,
        "comment_body": None,
    }


def build_issue_candidate(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    match_source: str = "pr-url",
    reviewer_roster: ReviewerRoster | None = None,
) -> dict[str, Any]:
    gate = build_review_gate(comments, required_reviewers, supplementary_reviewers, reviewer_roster)
    return {
        "id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "assignee": f"{issue.get('assignee_type')}/{issue.get('assignee_id')}",
        "pr_url": canonical_pr_url(detect_pr_url(issue, comments, None)),
        "review_sources": review_sources_from_comments(comments, reviewer_roster),
        "review_gate": gate,
        "comment_count": len(comments),
        "labels": [label.get("name") for label in issue_labels(issue) if label.get("name")],
        "is_tracking_issue": is_tracking_issue(issue),
        "match_source": match_source,
    }


def scan_pr_candidates(
    status: str,
    limit: int,
    reviewer_roster: ReviewerRoster | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    issues, error = list_issues(status, limit)
    warnings: list[str] = []
    if error:
        return [], [f"issue list failed: {error}"]

    candidates: list[dict[str, Any]] = []
    for issue in issues:
        issue_key = issue.get("identifier") or issue.get("id")
        if not issue_key:
            warnings.append("skipped issue without identifier/id")
            continue

        comments, comments_error = get_comments(str(issue_key))
        if comments_error:
            warnings.append(f"{issue_key}: comment collection failed: {comments_error}")
            comments = []

        candidates.append(
            build_issue_candidate(
                issue,
                comments,
                DEFAULT_REQUIRED_REVIEWERS,
                DEFAULT_SUPPLEMENTARY_REVIEWERS,
                reviewer_roster=reviewer_roster,
            )
        )

    candidates.sort(key=lambda item: (item["pr_url"] is None, item.get("identifier") or ""))
    return candidates, warnings


def render_candidate_scan(candidates: list[dict[str, Any]], warnings: list[str]) -> str:
    output = [
        "# PR Review Follow-up Candidate Scan",
        "",
        "## Candidates",
    ]
    if not candidates:
        output.append("- none")
    else:
        for candidate in candidates:
            identifier = candidate.get("identifier") or candidate.get("id")
            pr_url = candidate.get("pr_url") or "missing"
            sources = ", ".join(candidate.get("review_sources") or []) or "none"
            missing = ", ".join(candidate.get("review_gate", {}).get("missing") or []) or "none"
            supplementary = (
                ", ".join(candidate.get("review_gate", {}).get("supplementary", {}).get("present") or []) or "none"
            )
            output.append(
                f"- {identifier}: PR={pr_url} / reviewers={sources} / "
                f"high-signal missing={missing} / supplementary={supplementary} / "
                f"comments={candidate.get('comment_count')} / title={candidate.get('title')}"
            )

    if warnings:
        output.extend(["", "## Warnings"])
        output.extend(f"- {warning}" for warning in warnings)
    return "\n".join(output)


def resolve_pr_matches(
    pr_url: str,
    statuses: list[str],
    limit: int,
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    reviewer_roster: ReviewerRoster | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    canonical = canonical_pr_url(pr_url)
    if not canonical:
        return [], ["invalid-pr-url"]

    matches: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_issue_ids: set[str] = set()

    for status in statuses:
        issues, error = list_issues(status, limit)
        if error:
            warnings.append(f"{status}: issue list failed: {error}")
            continue

        for issue in issues:
            issue_id = issue.get("id")
            if not isinstance(issue_id, str) or issue_id in seen_issue_ids:
                continue

            issue_key = issue.get("identifier") or issue_id
            comments, comments_error = get_comments(str(issue_key))
            if comments_error:
                warnings.append(f"{issue_key}: comment collection failed: {comments_error}")
                comments = []

            detected = canonical_pr_url(detect_pr_url(issue, comments, None))
            if detected != canonical:
                continue

            seen_issue_ids.add(issue_id)
            matches.append(
                build_issue_candidate(
                    issue,
                    comments,
                    required_reviewers,
                    supplementary_reviewers,
                    match_source="pr-url",
                    reviewer_roster=reviewer_roster,
                )
            )

    matches.sort(key=lambda item: (item.get("status") != "in_review", item.get("identifier") or ""))
    return matches, warnings


def resolve_issue_refs_from_pr(
    pr: dict[str, Any],
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    reviewer_roster: ReviewerRoster | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    references = extract_issue_references(
        "\n".join(
            filter(
                None,
                [
                    str(pr.get("title") or "").strip(),
                    str(pr.get("body") or "").strip(),
                ],
            )
        )
    )
    warnings: list[str] = []
    matches: list[dict[str, Any]] = []
    seen_issue_ids: set[str] = set()

    for reference in references:
        issue, issue_error = get_issue(reference)
        if issue_error or not issue:
            warnings.append(f"{reference}: issue lookup failed: {issue_error or 'not found'}")
            continue

        issue_id = issue.get("id")
        if not isinstance(issue_id, str) or issue_id in seen_issue_ids:
            continue

        issue_key = issue.get("identifier") or issue_id
        comments, comments_error = get_comments(str(issue_key))
        if comments_error:
            warnings.append(f"{issue_key}: comment collection failed: {comments_error}")
            comments = []

        seen_issue_ids.add(issue_id)
        matches.append(
            build_issue_candidate(
                issue,
                comments,
                required_reviewers,
                supplementary_reviewers,
                match_source="pr-text-ref",
                reviewer_roster=reviewer_roster,
            )
        )

    return matches, warnings, references


def triage_priority(event_state: str | None) -> str:
    normalized = str(event_state or "").strip().upper()
    if normalized in {"CHANGES_REQUESTED", "REQUEST_CHANGES"}:
        return "high"
    return "medium"


def build_unlinked_dedupe_key(
    pr_url: str,
    pr: dict[str, Any] | None,
    event_name: str | None,
    event_action: str | None,
    event_author: str | None,
    event_state: str | None,
    head_sha: str | None,
) -> str:
    parsed = parse_pr_url(pr_url)
    repo, number = parsed if parsed else ("unknown/unknown", 0)
    normalized_head = clean_template_value(head_sha) or clean_template_value(str((pr or {}).get("headRefOid") or "")) or "unknown-head"
    normalized_event = clean_template_value(event_name) or "webhook"
    normalized_action = clean_template_value(event_action) or "unknown-action"
    normalized_author = clean_template_value(event_author) or "unknown-actor"
    normalized_state = clean_template_value(event_state) or "unknown-state"
    return (
        f"unlinked-pr:{repo}#{number}:event:{normalized_event}:action:{normalized_action}:"
        f"actor:{normalized_author}:state:{normalized_state}:head:{normalized_head}"
    )


def build_unlinked_triage_preview(
    pr_url: str,
    pr: dict[str, Any] | None,
    safe_references: list[str],
    fallback_project_title: str | None,
    event_name: str | None,
    event_action: str | None,
    sender: str | None,
    review_author: str | None,
    review_state: str | None,
    comment_author: str | None,
    head_sha: str | None,
) -> tuple[dict[str, Any], list[str]]:
    parsed = parse_pr_url(pr_url)
    repo_full_name, pr_number = parsed if parsed else ("unknown/unknown", 0)
    project, warnings = infer_project_for_repo(repo_full_name, fallback_project_title)
    project_title = project.get("title") if isinstance(project, dict) else None
    event_author = first_present(review_author, comment_author, sender)
    event_state = first_present(review_state, event_action)
    pr_title = clean_template_value(str((pr or {}).get("title") or "")) or f"{repo_full_name} PR #{pr_number}"
    dedupe_key = build_unlinked_dedupe_key(
        pr_url,
        pr,
        event_name,
        event_action,
        event_author,
        event_state,
        head_sha,
    )
    missing_reason = (
        "no exact PR URL match was found in open Multica issues, and PR title/body did not contain a safe "
        "Multica issue reference."
    )
    next_action_lines = [
        "- 기존 관련 Multica issue가 있으면 그 issue description 또는 댓글에 `GitHub PR: <url>`을 남긴다.",
        "- 관련 issue가 없으면 repo/project에 맞는 구현 또는 운영 issue를 새로 만들고 이 PR URL을 연결한다.",
        "- 연결 후 webhook을 다시 보내거나 `review_followup.py --resolve-pr-url <url>`로 재확인한다.",
    ]
    description_lines = [
        "[hermes:unlinked-pr]",
        "",
        "## Context",
        "GitHub PR review/comment webhook saw activity on a PR that is not safely linked to any Multica issue.",
        f"- Repository: {repo_full_name}",
        f"- PR: {pr_url}",
        f"- PR title: {pr_title}",
        f"- Event action: {clean_template_value(event_action) or 'unknown'}",
        f"- Event author: {event_author or 'unknown'}",
        f"- Review state: {event_state or 'unknown'}",
        f"- Sender: {clean_template_value(sender) or 'unknown'}",
        f"- Head SHA: {clean_template_value(head_sha) or clean_template_value(str((pr or {}).get('headRefOid') or '')) or 'unknown'}",
        f"- Missing link reason: {missing_reason}",
        f"- Safe PR references found: {', '.join(safe_references) if safe_references else 'none'}",
        f"- Dedupe key: `{dedupe_key}`",
        "",
        "## Goal",
        "이 PR review activity를 올바른 Multica issue에 연결하거나, 별도 후속 추적 없이 무시해도 되는 운영 PR인지 결정한다.",
        "",
        "## Done Condition",
        "- [ ] 이 PR을 기존 Multica issue에 연결하거나 새 구현/운영 issue를 만든다.",
        "- [ ] PR URL, reviewer/review state, 누락 원인, 다음 액션이 기록된다.",
        "- [ ] 중복 webhook delivery는 새 triage issue를 만들지 않고 기존 기록을 재사용한다.",
        "",
        "## Recommended Worker",
        "none — linked issue가 없어서 사람 triage가 먼저 필요하다.",
        "",
        "## Next Action",
        *next_action_lines,
    ]
    title = f"{TRACKING_TITLE_PREFIX} {repo_full_name} PR #{pr_number} review 이슈 연결 누락"
    preview = {
        "title": title,
        "description": "\n".join(description_lines),
        "status": "blocked",
        "priority": triage_priority(event_state),
        "label_id": TRACKING_LABEL_ID,
        "label_name": TRACKING_LABEL_NAME,
        "label_color": TRACKING_LABEL_COLOR,
        "project_id": project.get("id") if isinstance(project, dict) else None,
        "project_title": project_title,
        "dedupe_key": dedupe_key,
        "missing_reason": missing_reason,
        "next_action": next_action_lines,
    }
    return preview, warnings


def create_tracking_issue(preview: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    issue, issue_error = create_multica_issue(
        preview["title"],
        preview["description"],
        preview["status"],
        preview["priority"],
        preview.get("project_id"),
    )
    if issue_error or not issue:
        return None, issue_error or "unknown issue create failure"

    issue_id = issue.get("id")
    if not isinstance(issue_id, str):
        return None, "created issue missing id"

    label_id = clean_template_value(str(preview.get("label_id") or ""))
    if not label_id:
        label, label_error = ensure_multica_label(
            str(preview.get("label_name") or TRACKING_LABEL_NAME),
            str(preview.get("label_color") or TRACKING_LABEL_COLOR),
        )
        if label_error or not label:
            return None, label_error or "tracking label missing id"
        label_id = clean_template_value(str(label.get("id") or ""))
        if not label_id:
            return None, "tracking label missing id"

    label_error = add_multica_label(issue_id, label_id)
    if label_error:
        return None, label_error
    return issue, None


def render_pr_match_report(
    pr_url: str,
    statuses: list[str],
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    matches: list[dict[str, Any]],
    safe_references: list[str],
    triage_preview: dict[str, Any] | None,
    created_issue: dict[str, Any] | None,
    resolution_state: str,
    warnings: list[str],
    reviewer_roster: ReviewerRoster | None = None,
) -> str:
    output = [
        "# PR Link Resolver",
        "",
        f"- PR: {canonical_pr_url(pr_url) or pr_url}",
        f"- scanned statuses: {', '.join(statuses)}",
        f"- high-signal reviewers: {', '.join(required_reviewers)}",
        f"- supplementary reviewers: {', '.join(supplementary_reviewers)}",
        f"- reviewer roster: {(reviewer_roster.source if reviewer_roster else DEFAULT_REVIEWER_ROSTER.source)}",
        f"- resolution: {resolution_state}",
        "",
        "## Matches",
    ]
    if not matches:
        output.append("- none")
    else:
        for match in matches:
            gate = match.get("review_gate", {})
            output.append(
                f"- {match.get('identifier')}: status={match.get('status')} / "
                f"source={match.get('match_source')} / "
                f"high-signal present={', '.join(gate.get('present') or []) or 'none'} / "
                f"high-signal missing={', '.join(gate.get('missing') or []) or 'none'} / "
                f"high-signal skipped={format_skipped_reviewers(gate.get('required_skipped') or [])} / "
                f"high-signal excluded={format_skipped_reviewers(gate.get('required_excluded') or [])} / "
                f"supplementary present={', '.join(gate.get('supplementary', {}).get('present') or []) or 'none'} / "
                f"supplementary skipped={format_skipped_reviewers(gate.get('supplementary', {}).get('skipped') or [])} / "
                f"title={match.get('title')}"
            )

    output.extend(["", "## Safe PR References"])
    if safe_references:
        output.extend(f"- {reference}" for reference in safe_references)
    else:
        output.append("- none")

    if triage_preview:
        output.extend(
            [
                "",
                "## Triage Preview",
                f"- title: {triage_preview.get('title')}",
                f"- status: {triage_preview.get('status')}",
                f"- priority: {triage_preview.get('priority')}",
                f"- project: {triage_preview.get('project_title') or 'unassigned'}",
                f"- label: {triage_preview.get('label_name')}",
                f"- dedupe: {triage_preview.get('dedupe_key')}",
                f"- missing reason: {triage_preview.get('missing_reason')}",
            ]
        )

    if created_issue:
        output.extend(
            [
                "",
                "## Tracking Issue",
                f"- created: {created_issue.get('identifier') or created_issue.get('id')}",
                f"- title: {created_issue.get('title')}",
                f"- status: {created_issue.get('status')}",
            ]
        )

    if warnings:
        output.extend(["", "## Warnings"])
        output.extend(f"- {warning}" for warning in warnings)
    return "\n".join(output)


def resolve_pr_context(
    pr_url: str,
    statuses: list[str],
    scan_limit: int,
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    reviewer_roster: ReviewerRoster | None,
    create_triage_issue_on_miss: bool,
    fallback_project_title: str | None,
    event_name: str | None,
    event_action: str | None,
    sender: str | None,
    review_author: str | None,
    review_state: str | None,
    comment_author: str | None,
    head_sha: str | None,
) -> dict[str, Any]:
    matches, warnings = resolve_pr_matches(
        pr_url,
        statuses,
        scan_limit,
        required_reviewers,
        supplementary_reviewers,
        reviewer_roster,
    )
    matches, preferred_warnings = prefer_non_tracking_matches(matches)
    warnings.extend(preferred_warnings)
    safe_references: list[str] = []
    triage_preview: dict[str, Any] | None = None
    created_issue: dict[str, Any] | None = None
    resolution_state = "ambiguous"
    pr_context: dict[str, Any] | None = None

    should_inspect_pr = len(matches) != 1 or all(match.get("is_tracking_issue") for match in matches)
    if should_inspect_pr:
        auth_error = gh_auth_error()
        if auth_error:
            warnings.append(f"gh auth status failed: {auth_error}")
        else:
            pr_context, pr_error = gh_pr_view(pr_url)
            if pr_error:
                warnings.append(f"gh pr view failed: {pr_error}")
            elif pr_context:
                ref_matches, ref_warnings, safe_references = resolve_issue_refs_from_pr(
                    pr_context,
                    required_reviewers,
                    supplementary_reviewers,
                    reviewer_roster,
                )
                warnings.extend(ref_warnings)
                matches = dedupe_issue_candidates(matches + ref_matches)
                matches, preferred_warnings = prefer_non_tracking_matches(matches)
                warnings.extend(preferred_warnings)

    if len(matches) == 1:
        resolution_state = "linked"
    elif len(matches) > 1:
        resolution_state = "ambiguous"
    elif not canonical_pr_url(pr_url):
        resolution_state = "blocked"
        warnings.append("invalid-pr-url")
    elif not should_track_unlinked_pr(pr_url):
        resolution_state = EXTERNAL_REPO_RESOLUTION_STATE
        warnings.append("unlinked external repo skipped: owner not in tracked orgs")
    else:
        resolution_state = "needs-triage"
        triage_preview, triage_warnings = build_unlinked_triage_preview(
            pr_url,
            pr_context,
            safe_references,
            fallback_project_title,
            event_name,
            event_action,
            sender,
            review_author,
            review_state,
            comment_author,
            head_sha,
        )
        warnings.extend(triage_warnings)
        if create_triage_issue_on_miss:
            created_issue, create_error = create_tracking_issue(triage_preview)
            if create_error:
                warnings.append(f"tracking issue create failed: {create_error}")
                resolution_state = "blocked"

    return {
        "matches": dedupe_issue_candidates(matches),
        "warnings": dedupe_preserve(warnings),
        "safe_references": safe_references,
        "triage_preview": triage_preview,
        "created_issue": created_issue,
        "resolution_state": resolution_state,
        "pr_context": pr_context,
    }


def build_issue_record(issue: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "issue": issue,
        "comments": comments,
        "pr_url": canonical_pr_url(detect_pr_url(issue, comments, None)),
    }


def load_issue_records(
    issue_ids: list[str],
    issue_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    records: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for issue_id in dedupe_preserve(issue_ids):
        issue = issue_by_id.get(issue_id)
        if not issue:
            issue, issue_error = get_issue(issue_id)
            if issue_error or not issue:
                warnings.append(f"{issue_id}: issue lookup failed: {issue_error or 'not found'}")
                continue
        comments, comments_error = get_comments(issue_lookup_key(issue))
        if comments_error:
            warnings.append(f"{issue_lookup_key(issue)}: comment collection failed: {comments_error}")
            comments = []
        records[issue_id] = build_issue_record(issue, comments)
    return records, warnings


def issue_ancestor_ids(issue_id: str, issue_by_id: dict[str, dict[str, Any]]) -> list[str]:
    ancestors: list[str] = []
    seen: set[str] = set()
    current = issue_by_id.get(issue_id)
    while current:
        parent_id = issue_parent_id(current)
        if not parent_id or parent_id in seen:
            break
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = issue_by_id.get(parent_id)
    return ancestors


def issue_root_id(issue_id: str, issue_by_id: dict[str, dict[str, Any]]) -> str:
    ancestors = issue_ancestor_ids(issue_id, issue_by_id)
    return ancestors[-1] if ancestors else issue_id


def build_children_index(issues: list[dict[str, Any]]) -> dict[str, list[str]]:
    children_by_parent: dict[str, list[str]] = {}
    for issue in issues:
        issue_id = issue.get("id")
        parent_id = issue_parent_id(issue)
        if not isinstance(issue_id, str) or not parent_id:
            continue
        children_by_parent.setdefault(parent_id, []).append(issue_id)
    return children_by_parent


def collect_descendant_ids(root_issue_id: str, children_by_parent: dict[str, list[str]]) -> list[str]:
    descendants: list[str] = []
    stack = list(children_by_parent.get(root_issue_id, []))
    seen: set[str] = set()
    while stack:
        current_id = stack.pop()
        if current_id in seen:
            continue
        seen.add(current_id)
        descendants.append(current_id)
        stack.extend(children_by_parent.get(current_id, []))
    return descendants


def issue_is_self_or_descendant(
    candidate_issue_id: str,
    ancestor_issue_id: str,
    issue_by_id: dict[str, dict[str, Any]],
) -> bool:
    return candidate_issue_id == ancestor_issue_id or ancestor_issue_id in issue_ancestor_ids(candidate_issue_id, issue_by_id)


def describe_merged_aftercare_reason(reason: str) -> str:
    if reason == "direct-explicit-pr-link":
        return "직접 linked issue에 merged PR URL이 명시돼 있어 `done`으로 전환"
    if reason == "direct-leaf-pr-ref":
        return "leaf issue가 PR title/body safe reference로 직접 연결돼 있어 `done`으로 전환"
    if reason == "child-explicit-pr-link":
        return "하위 이슈가 동일 PR URL로 명시 연결돼 있어 `done`으로 전환"
    if reason == "direct-safe-ref-non-leaf":
        return "safe reference만 있고 non-leaf issue라 자동 종료하지 않음"
    if reason == "direct-no-explicit-pr-link":
        return "직접 linked issue지만 명시 PR URL이 없어 상태를 유지함"
    if reason.startswith("direct-status-left-unchanged:"):
        status = reason.rsplit(":", 1)[-1]
        return f"직접 linked issue status=`{status}` 는 자동 종료 대상이 아님"
    if reason.startswith("child-status-left-unchanged:"):
        status = reason.rsplit(":", 1)[-1]
        return f"하위 이슈 status=`{status}` 는 자동 종료 대상이 아님"
    return reason


def has_existing_merged_aftercare_comment(
    comments: list[dict[str, Any]],
    pr_url: str,
    merge_commit_sha: str | None,
    head_sha: str | None,
) -> bool:
    canonical = canonical_pr_url(pr_url)
    for comment in comments:
        content = comment.get("content") or ""
        if MERGED_AFTERCARE_MARKER not in content:
            continue
        if canonical and canonical not in content:
            continue
        if merge_commit_sha and merge_commit_sha in content:
            return True
        if head_sha and f"- head SHA: `{head_sha}`" in content:
            return True
        if canonical and not merge_commit_sha and not head_sha:
            return True
    return False


def build_merged_aftercare_plan(
    pr_url: str,
    matched_candidates: list[dict[str, Any]],
    issue_by_id: dict[str, dict[str, Any]],
    issue_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not matched_candidates:
        return {
            "state": "unlinked",
            "matched_issue_ids": [],
            "top_level_match_ids": [],
            "updates": [],
            "skipped": [],
            "family_root_ids": [],
        }

    matched_issue_ids = [
        issue_id
        for issue_id in [candidate.get("id") for candidate in matched_candidates]
        if isinstance(issue_id, str)
    ]
    matched_issue_ids = dedupe_preserve(matched_issue_ids)
    family_root_ids = dedupe_preserve([issue_root_id(issue_id, issue_by_id) for issue_id in matched_issue_ids])
    if len(family_root_ids) > 1:
        return {
            "state": "ambiguous",
            "matched_issue_ids": matched_issue_ids,
            "top_level_match_ids": [],
            "updates": [],
            "skipped": [],
            "family_root_ids": family_root_ids,
        }

    matched_issue_id_set = set(matched_issue_ids)
    top_level_match_ids = [
        issue_id
        for issue_id in matched_issue_ids
        if not any(ancestor in matched_issue_id_set for ancestor in issue_ancestor_ids(issue_id, issue_by_id))
    ]
    children_by_parent = build_children_index(list(issue_by_id.values()))
    updates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_update_issue_ids: set[str] = set()

    for candidate in matched_candidates:
        issue_id = candidate.get("id")
        if not isinstance(issue_id, str):
            continue
        record = issue_records.get(issue_id)
        if not record:
            skipped.append(
                {
                    "issue_id": issue_id,
                    "identifier": candidate.get("identifier") or issue_id,
                    "kind": "direct",
                    "status": candidate.get("status") or "unknown",
                    "reason": "missing-issue-record",
                }
            )
            continue
        issue = record["issue"]
        status = str(issue.get("status") or "unknown")
        descendants = collect_descendant_ids(issue_id, children_by_parent)
        explicit_pr_link = record.get("pr_url") == pr_url
        if status not in MERGED_AFTERCARE_CLOSEABLE_STATUSES:
            skipped.append(
                {
                    "issue_id": issue_id,
                    "identifier": issue_lookup_key(issue),
                    "kind": "direct",
                    "status": status,
                    "reason": f"direct-status-left-unchanged:{status}",
                }
            )
        elif explicit_pr_link:
            updates.append(
                {
                    "issue_id": issue_id,
                    "identifier": issue_lookup_key(issue),
                    "kind": "direct",
                    "from_status": status,
                    "to_status": "done",
                    "reason": "direct-explicit-pr-link",
                }
            )
            seen_update_issue_ids.add(issue_id)
        elif candidate.get("match_source") == "pr-text-ref" and not descendants:
            updates.append(
                {
                    "issue_id": issue_id,
                    "identifier": issue_lookup_key(issue),
                    "kind": "direct",
                    "from_status": status,
                    "to_status": "done",
                    "reason": "direct-leaf-pr-ref",
                }
            )
            seen_update_issue_ids.add(issue_id)
        elif candidate.get("match_source") == "pr-text-ref":
            skipped.append(
                {
                    "issue_id": issue_id,
                    "identifier": issue_lookup_key(issue),
                    "kind": "direct",
                    "status": status,
                    "reason": "direct-safe-ref-non-leaf",
                }
            )
        else:
            skipped.append(
                {
                    "issue_id": issue_id,
                    "identifier": issue_lookup_key(issue),
                    "kind": "direct",
                    "status": status,
                    "reason": "direct-no-explicit-pr-link",
                }
            )

        for descendant_id in descendants:
            if descendant_id in matched_issue_id_set or descendant_id in seen_update_issue_ids:
                continue
            descendant_record = issue_records.get(descendant_id)
            if not descendant_record:
                continue
            descendant_issue = descendant_record["issue"]
            descendant_status = str(descendant_issue.get("status") or "unknown")
            if descendant_record.get("pr_url") != pr_url:
                continue
            if descendant_status in MERGED_AFTERCARE_CLOSEABLE_STATUSES:
                updates.append(
                    {
                        "issue_id": descendant_id,
                        "identifier": issue_lookup_key(descendant_issue),
                        "kind": "child",
                        "from_status": descendant_status,
                        "to_status": "done",
                        "reason": "child-explicit-pr-link",
                    }
                )
                seen_update_issue_ids.add(descendant_id)
            else:
                skipped.append(
                    {
                        "issue_id": descendant_id,
                        "identifier": issue_lookup_key(descendant_issue),
                        "kind": "child",
                        "status": descendant_status,
                        "reason": f"child-status-left-unchanged:{descendant_status}",
                    }
                )

    return {
        "state": "ready",
        "matched_issue_ids": matched_issue_ids,
        "top_level_match_ids": top_level_match_ids,
        "updates": updates,
        "skipped": skipped,
        "family_root_ids": family_root_ids,
    }


def render_merged_aftercare_comment(
    target_issue: dict[str, Any],
    target_updates: list[dict[str, Any]],
    target_skipped: list[dict[str, Any]],
    merge_context: dict[str, Any],
    issue_by_id: dict[str, dict[str, Any]],
) -> str:
    output = [
        MERGED_AFTERCARE_MARKER,
        "GitHub PR 병합 후처리 기록입니다.",
        "",
        f"- PR: {merge_context['pr_url']}",
        f"- Linked issue: {issue_mention(target_issue)}",
        f"- repository: {merge_context.get('repo_full_name') or 'unknown'}",
        f"- PR number: {merge_context.get('pr_number') or 'unknown'}",
        f"- base branch: `{merge_context.get('base_ref') or 'unknown'}`",
        f"- head branch: `{merge_context.get('head_ref') or 'unknown'}`",
        f"- head SHA: `{merge_context.get('head_sha') or 'unknown'}`",
        f"- merge commit: `{merge_context.get('merge_commit_sha') or 'unknown'}`",
        f"- merged at: {merge_context.get('merged_at') or 'unknown'}",
        f"- merged by: {merge_context.get('merged_by') or 'unknown'}",
        "",
        "Status changes:",
    ]
    if target_updates:
        for entry in target_updates:
            issue = issue_by_id.get(entry["issue_id"], {"id": entry["issue_id"], "identifier": entry["identifier"]})
            output.append(
                f"- {issue_mention(issue)}: `{entry['from_status']}` -> `{entry['to_status']}`"
                f" ({describe_merged_aftercare_reason(entry['reason'])})"
            )
    else:
        output.append("- none")

    output.extend(["", "Left unchanged:"])
    if target_skipped:
        for entry in target_skipped:
            issue = issue_by_id.get(entry["issue_id"], {"id": entry["issue_id"], "identifier": entry["identifier"]})
            output.append(
                f"- {issue_mention(issue)}: status=`{entry.get('status') or 'unknown'}`"
                f" ({describe_merged_aftercare_reason(entry['reason'])})"
            )
    else:
        output.append("- none")
    return "\n".join(output)


def apply_merged_aftercare_plan(
    plan: dict[str, Any],
    issue_by_id: dict[str, dict[str, Any]],
    issue_records: dict[str, dict[str, Any]],
    merge_context: dict[str, Any],
    apply_aftercare: bool,
) -> dict[str, Any]:
    if plan.get("state") != "ready":
        return {
            "state": "blocked" if plan.get("state") == "ambiguous" else str(plan.get("state")),
            "updated_issues": [],
            "posted_comments": [],
            "comment_previews": [],
            "errors": [],
        }

    updated_issues: list[dict[str, Any]] = []
    posted_comments: list[dict[str, Any]] = []
    comment_previews: list[dict[str, Any]] = []
    errors: list[str] = []

    for entry in plan.get("updates", []):
        if apply_aftercare:
            _updated_issue, update_error = update_multica_issue_status(entry["issue_id"], entry["to_status"])
            if update_error:
                errors.append(f"{entry['identifier']}: status update failed: {update_error}")
                continue
        updated_issues.append(entry)

    for target_issue_id in plan.get("top_level_match_ids", []):
        target_record = issue_records.get(target_issue_id)
        if not target_record:
            continue
        target_issue = target_record["issue"]
        target_updates = [
            entry
            for entry in plan.get("updates", [])
            if issue_is_self_or_descendant(entry["issue_id"], target_issue_id, issue_by_id)
        ]
        target_skipped = [
            entry
            for entry in plan.get("skipped", [])
            if issue_is_self_or_descendant(entry["issue_id"], target_issue_id, issue_by_id)
        ]
        comment_body = render_merged_aftercare_comment(
            target_issue,
            target_updates,
            target_skipped,
            merge_context,
            issue_by_id,
        )
        if has_existing_merged_aftercare_comment(
            target_record["comments"],
            merge_context["pr_url"],
            merge_context.get("merge_commit_sha"),
            merge_context.get("head_sha"),
        ):
            continue
        if apply_aftercare:
            comment_error = add_multica_comment(target_issue_id, comment_body)
            if comment_error:
                errors.append(f"{issue_lookup_key(target_issue)}: merged aftercare comment failed: {comment_error}")
                continue
            posted_comments.append({"issue_id": target_issue_id, "identifier": issue_lookup_key(target_issue)})
        else:
            comment_previews.append(
                {
                    "issue_id": target_issue_id,
                    "identifier": issue_lookup_key(target_issue),
                    "content": comment_body,
                }
            )

    if errors:
        state = "blocked"
    elif not apply_aftercare:
        state = "dry-run"
    elif not updated_issues and not posted_comments:
        state = "noop"
    else:
        state = "completed"

    return {
        "state": state,
        "updated_issues": updated_issues,
        "posted_comments": posted_comments,
        "comment_previews": comment_previews,
        "errors": errors,
    }


def run_merged_aftercare(
    pr_url: str,
    matches: list[dict[str, Any]],
    statuses: list[str],
    merge_context: dict[str, Any],
    apply_aftercare: bool,
) -> dict[str, Any]:
    issues, warnings = list_issues_for_statuses(statuses)
    issue_by_id = {
        issue_id: issue
        for issue in issues
        if isinstance((issue_id := issue.get("id")), str)
    }
    children_by_parent = build_children_index(issues)
    family_issue_ids: list[str] = []
    for match in matches:
        issue_id = match.get("id")
        if not isinstance(issue_id, str):
            continue
        family_issue_ids.append(issue_id)
        family_issue_ids.extend(collect_descendant_ids(issue_id, children_by_parent))
    issue_records, record_warnings = load_issue_records(family_issue_ids, issue_by_id)
    warnings.extend(record_warnings)
    plan = build_merged_aftercare_plan(pr_url, matches, issue_by_id, issue_records)
    result = apply_merged_aftercare_plan(plan, issue_by_id, issue_records, merge_context, apply_aftercare)
    return {
        "state": result["state"],
        "plan": plan,
        "updated_issues": result["updated_issues"],
        "posted_comments": result["posted_comments"],
        "comment_previews": result["comment_previews"],
        "errors": result["errors"],
        "warnings": dedupe_preserve(warnings),
    }


def render_merged_aftercare_report(
    merge_context: dict[str, Any],
    statuses: list[str],
    resolution: dict[str, Any],
    aftercare: dict[str, Any] | None,
    warnings: list[str],
) -> str:
    output = [
        "# PR Merge Aftercare",
        "",
        f"- PR: {merge_context['pr_url']}",
        f"- repository: {merge_context.get('repo_full_name') or 'unknown'}",
        f"- PR number: {merge_context.get('pr_number') or 'unknown'}",
        f"- merged: {merge_context.get('pr_merged')}",
        f"- base branch: {merge_context.get('base_ref') or 'unknown'}",
        f"- head branch: {merge_context.get('head_ref') or 'unknown'}",
        f"- head SHA: {merge_context.get('head_sha') or 'unknown'}",
        f"- merge commit: {merge_context.get('merge_commit_sha') or 'unknown'}",
        f"- merged at: {merge_context.get('merged_at') or 'unknown'}",
        f"- merged by: {merge_context.get('merged_by') or 'unknown'}",
        f"- scanned statuses: {', '.join(statuses)}",
        f"- resolution: {resolution.get('resolution_state')}",
    ]
    if aftercare:
        output.append(f"- aftercare state: {aftercare.get('state')}")
    output.extend(["", "## Matches"])
    matches = resolution.get("matches") or []
    if matches:
        for match in matches:
            output.append(
                f"- {match.get('identifier')}: status={match.get('status')} / source={match.get('match_source')}"
            )
    else:
        output.append("- none")

    if aftercare:
        output.extend(["", "## Planned Status Changes"])
        updates = aftercare.get("plan", {}).get("updates") or []
        if updates:
            for entry in updates:
                output.append(
                    f"- {entry['identifier']}: {entry['from_status']} -> {entry['to_status']} / {entry['reason']}"
                )
        else:
            output.append("- none")

        output.extend(["", "## Left Unchanged"])
        skipped = aftercare.get("plan", {}).get("skipped") or []
        if skipped:
            for entry in skipped:
                output.append(
                    f"- {entry['identifier']}: status={entry.get('status') or 'unknown'} / {entry['reason']}"
                )
        else:
            output.append("- none")

    resolution_warnings = resolution.get("warnings") if isinstance(resolution, dict) else []
    aftercare_warnings = aftercare.get("warnings") if isinstance(aftercare, dict) else []
    combined_warnings = dedupe_preserve((resolution_warnings or []) + (aftercare_warnings or []) + (warnings or []))
    if combined_warnings:
        output.extend(["", "## Warnings"])
        output.extend(f"- {warning}" for warning in combined_warnings)
    return "\n".join(output)


def render_review_request_pack(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    pr_url: str | None,
    reviewers: list[str],
) -> str:
    identifier = issue.get("identifier") or issue.get("id")
    title = issue.get("title") or "(untitled)"
    recent_context = summarize_comments(comments)[-5:]

    output = [
        f"# PR Multi-AI Review Pack: {identifier}",
        "",
        "## Target",
        f"- issue: {identifier}",
        f"- title: {title}",
        f"- PR: {pr_url or 'missing'}",
        "",
        "## Operating Loop",
    ]
    output.extend(f"- {step}" for step in REVIEW_LOOP_STEPS)

    output.extend(
        [
            "",
            "## Shared Reviewer Prompt",
            "아래 PR을 코드 리뷰해 주세요. 결과는 반드시 `must-fix`, `should-fix`, `question`, `non-actionable`로 나누고, 각 항목에는 파일/라인 또는 근거를 붙여 주세요. "
            "단순 취향은 `non-actionable`로 분리하고, 실제 merge 전에 고쳐야 하는 correctness/security/regression 문제는 `must-fix`로 표시해 주세요. "
            "Hermes는 Claude/Codex를 high-signal로, Gemini/Copilot을 supplementary로 가중 반영합니다.",
            "",
            f"- Multica issue: {identifier}",
            f"- GitHub PR: {pr_url or '<GitHub PR URL>'}",
            f"- 구현 제목: {title}",
            "",
            "## Reviewer Focus",
        ]
    )

    for reviewer in reviewers:
        focus_items = REVIEW_FOCUS_BY_REVIEWER.get(reviewer, ["일반 코드 리뷰"])
        output.append(f"### {reviewer}")
        output.extend(f"- {item}" for item in focus_items)
        output.append("")

    output.extend(["## Intake Template", "```md"])
    output.extend(
        [
            "## Review Intake",
            f"PR: {pr_url or '<GitHub PR URL>'}",
            f"Source reviewers: {' / '.join(reviewers)}",
            "",
            "## Triage",
            "- must-fix:",
            "- should-fix:",
            "- question:",
            "- non-actionable:",
            "",
            "## Apply",
            "- changed:",
            "- deferred:",
            "- rejected with reason:",
            "",
            "## Verify",
            "- gh checks:",
            "- reviewThreads:",
            "- tests/analyze:",
            "- remaining risk:",
            "",
            "## Final State",
            "- ready to merge / needs another review / blocked",
        ]
    )
    output.extend(["```", "", "## Recent Multica Context"])
    output.extend(recent_context or ["- none"])
    return "\n".join(output).strip()


def parse_pr_url(url: str) -> tuple[str, int] | None:
    match = PR_URL_RE.search(url)
    if not match:
        return None
    owner, repo, number = match.groups()
    return f"{owner}/{repo}", int(number)


def get_issue(issue_id: str) -> tuple[dict[str, Any] | None, str | None]:
    return run_json(["multica", "issue", "get", issue_id, "--output", "json"])


def get_comments(issue_id: str) -> tuple[list[dict[str, Any]], str | None]:
    comments, error = run_json(["multica", "issue", "comment", "list", issue_id, "--output", "json"])
    if error:
        return [], error
    if isinstance(comments, list):
        return comments, None
    return [], "unexpected-comments-shape"


def list_projects() -> tuple[list[dict[str, Any]], str | None]:
    projects, error = run_json(["multica", "project", "list", "--output", "json"])
    if error:
        return [], error
    if isinstance(projects, list):
        return projects, None
    return [], "unexpected-projects-shape"


def infer_project_for_repo(
    repo_full_name: str,
    fallback_project_title: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    projects, error = list_projects()
    if error:
        return None, [f"project lookup failed: {error}"]

    owner, repo_name = repo_full_name.split("/", 1)
    candidates = [repo_name]
    if repo_name == ".github":
        candidates = [owner]
    elif owner not in candidates:
        candidates.append(owner)

    fallback = clean_template_value(fallback_project_title)
    if fallback and fallback not in candidates:
        candidates.append(fallback)

    lowered = [candidate.lower() for candidate in candidates]
    for project in projects:
        title = str(project.get("title") or "").strip()
        if title.lower() in lowered:
            return project, warnings

    warnings.append(f"no project title matched repo candidates: {', '.join(candidates)}")
    return None, warnings


def create_multica_issue(
    title: str,
    description: str,
    status: str,
    priority: str,
    project_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    command = [
        "multica",
        "issue",
        "create",
        "--title",
        title,
        "--description-stdin",
        "--priority",
        priority,
        "--status",
        status,
        "--output",
        "json",
    ]
    if project_id:
        command.extend(["--project", project_id])
    result = subprocess.run(
        command,
        check=False,
        text=True,
        input=description,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip() or "multica issue create failed"
    try:
        issue = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"invalid-json: {exc}"
    if isinstance(issue, dict):
        return issue, None
    return None, "unexpected-issue-create-shape"


def list_multica_labels() -> tuple[list[dict[str, Any]], str | None]:
    payload, error = run_json(["multica", "label", "list", "--output", "json"])
    if error:
        return [], error
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)], None
    return [], "unexpected-label-list-shape"


def find_multica_label_by_name(name: str) -> tuple[dict[str, Any] | None, str | None]:
    labels, error = list_multica_labels()
    if error:
        return None, error
    normalized = name.strip().lower()
    for label in labels:
        label_name = str(label.get("name") or "").strip().lower()
        if label_name == normalized:
            return label, None
    return None, None


def create_multica_label(name: str, color: str) -> tuple[dict[str, Any] | None, str | None]:
    payload, error = run_json(
        ["multica", "label", "create", "--name", name, "--color", color, "--output", "json"]
    )
    if error:
        return None, error
    if isinstance(payload, dict):
        return payload, None
    return None, "unexpected-label-create-shape"


def ensure_multica_label(name: str, color: str) -> tuple[dict[str, Any] | None, str | None]:
    label, error = find_multica_label_by_name(name)
    if error:
        return None, error
    if label:
        return label, None
    return create_multica_label(name, color)


def add_multica_label(issue_id: str, label_id: str) -> str | None:
    result = run(["multica", "issue", "label", "add", issue_id, label_id])
    if result.ok:
        return None
    return result.stderr or result.stdout or "multica issue label add failed"


def github_signal_author(
    event_name: str | None,
    sender: str | None,
    review_author: str | None,
    comment_author: str | None,
) -> str | None:
    normalized_event = clean_template_value(event_name)
    if normalized_event == "pull_request_review":
        return first_present(review_author, sender)
    return first_present(comment_author, review_author, sender)


def github_signal_body(
    event_name: str | None,
    review_body: str | None,
    comment_body: str | None,
) -> str:
    normalized_event = clean_template_value(event_name)
    if normalized_event == "pull_request_review":
        return clean_template_value(review_body) or clean_template_value(comment_body) or ""
    return clean_template_value(comment_body) or clean_template_value(review_body) or ""


def github_signal_url(
    event_name: str | None,
    review_url: str | None,
    comment_url: str | None,
) -> str | None:
    normalized_event = clean_template_value(event_name)
    if normalized_event == "pull_request_review":
        return first_present(review_url, comment_url)
    return first_present(comment_url, review_url)


def synthesize_github_reviewer_signal(
    reviewer_roster: ReviewerRoster,
    pr_url: str | None,
    head_sha: str | None,
    event_name: str | None = None,
    event_action: str | None = None,
    sender: str | None = None,
    review_author: str | None = None,
    review_id: str | None = None,
    review_state: str | None = None,
    review_body: str | None = None,
    review_url: str | None = None,
    comment_author: str | None = None,
    comment_id: str | None = None,
    comment_body: str | None = None,
    comment_url: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    normalized_event = clean_template_value(event_name)
    canonical = canonical_pr_url(pr_url)
    if normalized_event == "issue_comment" and not canonical:
        return None, "blocked/non-pr-issue-comment"

    author = github_signal_author(normalized_event, sender, review_author, comment_author)
    if not author:
        return None, None

    profile = reviewer_profile_for_github_login(author, reviewer_roster)
    if not profile:
        reviewer_key = resolve_reviewer_key(author, reviewer_roster)
        profile = reviewer_roster.profiles_by_key.get(reviewer_key) if reviewer_key else None
    if not profile or not reviewer_supports_source(profile, "github-review"):
        return None, None

    body = github_signal_body(normalized_event, review_body, comment_body)
    raw_verdict, normalized_verdict = extract_github_review_verdict(body, review_state)
    source_url = github_signal_url(normalized_event, review_url, comment_url)
    signal_id = first_present(review_id, comment_id, source_url, head_sha, author, normalized_event) or "github-signal"
    return {
        "id": f"github-signal:{normalized_event or 'event'}:{signal_id}",
        "author_id": author,
        "author_name": author,
        "author_type": "github-review",
        "created_at": "",
        "content": body,
        "signal_source": "github-review",
        "reviewer_key": profile.key,
        "reviewer_name": profile.name,
        "source_event": normalized_event,
        "source_action": clean_template_value(event_action),
        "source_url": source_url,
        "head_sha": clean_template_value(head_sha),
        "review_state": clean_template_value(review_state),
        "verdict": raw_verdict,
        "normalized_verdict": normalized_verdict,
    }, None


def gh_auth_error() -> str | None:
    result = run(["gh", "auth", "status"])
    if result.ok:
        return None
    return result.stderr or result.stdout or "gh auth status failed"


def gh_pr_view(pr_url: str) -> tuple[dict[str, Any] | None, str | None]:
    return run_json(
        [
            "gh",
            "pr",
            "view",
            pr_url,
            "--json",
            "number,url,title,body,state,isDraft,reviewDecision,mergeStateStatus,autoMergeRequest,headRefName,headRefOid,baseRefName,author,closingIssuesReferences",
        ]
    )


def gh_repo_default_branch(repo_full_name: str) -> tuple[str | None, str | None]:
    payload, error = run_json(["gh", "repo", "view", repo_full_name, "--json", "defaultBranchRef"])
    if error:
        return None, error
    if not isinstance(payload, dict):
        return None, "unexpected-repo-view-shape"
    branch = clean_template_value(str((payload.get("defaultBranchRef") or {}).get("name") or ""))
    if branch:
        return branch, None
    return None, "missing-default-branch"


def gh_pr_checks(pr_url: str) -> tuple[list[dict[str, Any]], str | None]:
    checks, error = run_json(["gh", "pr", "checks", pr_url, "--json", "name,state,link,workflow"])
    if error:
        return [], error
    if isinstance(checks, list):
        return checks, None
    return [], "unexpected-checks-shape"


def gh_review_threads(repo: str, number: int) -> tuple[list[dict[str, Any]], str | None]:
    query = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 20) {
            nodes {
              author { login }
              body
              createdAt
              url
            }
          }
        }
      }
    }
  }
}
"""
    owner, name = repo.split("/", 1)
    data, error = run_json(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
            "-f",
            f"query={query}",
        ]
    )
    if error:
        return [], error
    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    if isinstance(nodes, list):
        return nodes, None
    return [], "unexpected-reviewThreads-shape"


def gh_pr_reviews(repo: str, number: int) -> tuple[list[dict[str, Any]], str | None]:
    query = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: 100) {
        nodes {
          author { login }
          body
          state
          submittedAt
          url
          commit { oid }
        }
      }
    }
  }
}
"""
    owner, name = repo.split("/", 1)
    data, error = run_json(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
            "-f",
            f"query={query}",
        ]
    )
    if error:
        return [], error
    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviews", {})
        .get("nodes", [])
    )
    if isinstance(nodes, list):
        return nodes, None
    return [], "unexpected-reviews-shape"


def gh_issue_comments(repo: str, number: int) -> tuple[list[dict[str, Any]], str | None]:
    comments, error = run_json(["gh", "api", f"repos/{repo}/issues/{number}/comments"])
    if error:
        return [], error
    if isinstance(comments, list):
        return comments, None
    return [], "unexpected-issue-comments-shape"


def gh_commit_details(repo: str, sha: str | None) -> tuple[dict[str, Any] | None, str | None]:
    normalized_sha = clean_template_value(sha)
    if not normalized_sha:
        return None, None
    commit, error = run_json(["gh", "api", f"repos/{repo}/commits/{normalized_sha}"])
    if error:
        return None, error
    if isinstance(commit, dict):
        return commit, None
    return None, "unexpected-commit-shape"


def build_github_review_comment(
    reviewer_roster: ReviewerRoster | None,
    author_login: str | None,
    body: str | None,
    created_at: str | None,
    review_state: str | None,
    source_url: str | None,
    source_event: str,
    source_action: str,
    head_sha: str | None = None,
) -> dict[str, Any] | None:
    profile = reviewer_profile_for_github_login(author_login, reviewer_roster)
    if not profile:
        return None
    verdict, normalized_verdict = extract_github_review_verdict(body, review_state)
    return {
        "author_id": normalize_github_login(author_login),
        "author_name": clean_template_value(author_login),
        "author_type": "github-review",
        "created_at": clean_template_value(created_at),
        "content": body or "",
        "signal_source": "github-review",
        "reviewer_key": profile.key,
        "reviewer_name": profile.name,
        "verdict": verdict,
        "normalized_verdict": normalized_verdict,
        "source_event": source_event,
        "source_action": source_action,
        "source_url": clean_template_value(source_url),
        "review_state": clean_template_value(review_state),
        "head_sha": clean_template_value(head_sha),
    }


def synthesize_github_review_comments(
    repo: str,
    pr: dict[str, Any] | None,
    reviews: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> list[dict[str, Any]]:
    head_sha = clean_template_value(str((pr or {}).get("headRefOid") or ""))
    synthesized: list[dict[str, Any]] = []
    for review in reviews:
        synthesized_comment = build_github_review_comment(
            reviewer_roster,
            (review.get("author") or {}).get("login"),
            review.get("body"),
            review.get("submittedAt"),
            review.get("state"),
            review.get("url"),
            "pull_request_review",
            "submitted",
            head_sha=clean_template_value(str((review.get("commit") or {}).get("oid") or head_sha or "")),
        )
        if synthesized_comment:
            synthesized.append(synthesized_comment)
    for issue_comment in issue_comments:
        synthesized_comment = build_github_review_comment(
            reviewer_roster,
            (issue_comment.get("user") or {}).get("login"),
            issue_comment.get("body"),
            issue_comment.get("created_at"),
            "COMMENTED",
            issue_comment.get("html_url"),
            "issue_comment",
            "created",
            head_sha=head_sha,
        )
        if synthesized_comment:
            synthesized.append(synthesized_comment)
    return dedupe_review_comments(sorted(synthesized, key=lambda item: str(item.get("created_at") or "")))


def dedupe_review_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for comment in comments:
        source_url = clean_template_value(str(comment.get("source_url") or ""))
        key = source_url or "|".join(
            [
                clean_template_value(str(comment.get("reviewer_key") or "")) or "unknown-reviewer",
                clean_template_value(str(comment.get("source_event") or "")) or "unknown-event",
                clean_template_value(str(comment.get("source_action") or "")) or "unknown-action",
                clean_template_value(str(comment.get("created_at") or "")) or "unknown-time",
                clean_template_value(str(comment.get("head_sha") or "")) or "unknown-head",
                clean_template_value(str(comment.get("normalized_verdict") or comment.get("verdict") or "")) or "unknown-verdict",
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(comment)
    return output


def summarize_comments(comments: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for comment in comments[-10:]:
        content = (comment.get("content") or "").strip().replace("\n", " ")
        if len(content) > 160:
            content = content[:157] + "..."
        author = comment.get("author_type", "unknown")
        if comment_signal_source(comment) == "github-review":
            author = f"github-review:{comment.get('author_name') or comment.get('author_id') or 'unknown'}"
        created = comment.get("created_at", "")
        lines.append(f"- {created} {author}: {content}")
    return lines


def commenter_name(
    comment: dict[str, Any],
    reviewer_roster: ReviewerRoster | None = None,
) -> str:
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    reviewer_key = normalize_lookup_token(str(comment.get("reviewer_key") or ""))
    if reviewer_key:
        profile = roster.profiles_by_key.get(reviewer_key)
        if profile:
            return profile.name
    reviewer_name = clean_template_value(str(comment.get("reviewer_name") or ""))
    if reviewer_name:
        resolved = reviewer_display_name(reviewer_name, roster)
        if resolved:
            return resolved
    author_id = comment.get("author_id")
    if isinstance(author_id, str) and author_id in roster.agent_id_to_key:
        profile = roster.profiles_by_key.get(roster.agent_id_to_key[author_id])
        if profile:
            return profile.name
    for candidate in [comment.get("author_name"), author_id, comment.get("author_type")]:
        resolved = reviewer_display_name(str(candidate), roster) if candidate else None
        if resolved:
            return resolved
    return comment.get("author_type", "unknown")


def reviewer_profile_for_comment(
    comment: dict[str, Any],
    reviewer_roster: ReviewerRoster | None = None,
) -> ReviewerProfile | None:
    roster = reviewer_roster or DEFAULT_REVIEWER_ROSTER
    reviewer_key = normalize_lookup_token(str(comment.get("reviewer_key") or ""))
    profile = roster.profiles_by_key.get(reviewer_key) if reviewer_key else None
    if profile and reviewer_supports_source(profile, comment_signal_source(comment)) and comment_has_review_signal(comment):
        return profile

    reviewer = commenter_name(comment, roster)
    key = resolve_reviewer_key(reviewer, roster)
    if not key:
        return None
    profile = roster.profiles_by_key.get(key)
    if not profile or not reviewer_supports_source(profile, comment_signal_source(comment)):
        return None
    if not comment_has_review_signal(comment):
        return None
    return profile


def comment_has_review_signal(comment: dict[str, Any]) -> bool:
    if clean_template_value(str(comment.get("normalized_verdict") or comment.get("verdict") or "")):
        return True
    content = comment.get("content") or ""
    if AI_REVIEW_META_RE.search(content):
        return True
    has_bucket_heading = False
    has_final_state_heading = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if normalize_bucket_heading(line):
            has_bucket_heading = True
        if re.match(r"^#+\s*final state\b", line, re.IGNORECASE):
            has_final_state_heading = True
    if comment_signal_source(comment) != "github-review":
        return has_bucket_heading or has_final_state_heading
    if has_bucket_heading or has_final_state_heading:
        return True
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for _candidate, pattern in REVIEWER_VERDICT_PATTERNS:
            if pattern.search(line):
                return True
    return False


def summarize_review_sources(
    comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> list[str]:
    by_reviewer: dict[str, list[str]] = {}
    for comment in comments:
        profile = reviewer_profile_for_comment(comment, reviewer_roster)
        if not profile:
            continue
        name = profile.name
        content = (comment.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        if len(content) > 220:
            content = content[:217] + "..."
        created = comment.get("created_at", "")
        prefix = f"[{comment_signal_source(comment)}]"
        timestamp = f"{created} " if created else ""
        by_reviewer.setdefault(name, []).append(f"{timestamp}{prefix} {content}")

    if not by_reviewer:
        return ["- no recognized reviewer signals found in Multica or GitHub review events"]

    lines: list[str] = []
    for reviewer in ordered_reviewers(sorted(by_reviewer), reviewer_roster):
        lines.append(f"- {reviewer}:")
        for item in by_reviewer[reviewer][-5:]:
            lines.append(f"  - {item}")
    return lines


def normalize_bucket_heading(line: str) -> str | None:
    normalized = line.strip().lower()
    normalized = normalized.lstrip("#*-0123456789. )\t").strip()
    normalized = normalized.rstrip(":").strip()
    normalized = normalized.replace("_", "-")
    for bucket in TRIAGE_BUCKETS:
        if normalized == bucket or normalized.startswith(f"{bucket} "):
            return bucket
    return None


def collect_review_triage(
    comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]]:
    triage = {bucket: [] for bucket in TRIAGE_BUCKETS}
    by_reviewer: dict[str, dict[str, list[str]]] = {}

    for comment in comments:
        profile = reviewer_profile_for_comment(comment, reviewer_roster)
        if not profile:
            continue
        reviewer = profile.name
        by_reviewer.setdefault(reviewer, {bucket: [] for bucket in TRIAGE_BUCKETS})
        current_bucket: str | None = None

        for raw_line in (comment.get("content") or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue

            bucket = normalize_bucket_heading(line)
            if bucket:
                current_bucket = bucket
                continue

            if line.startswith("#"):
                current_bucket = None
                continue

            if not current_bucket:
                continue

            if not line.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.")):
                continue

            item = re.sub(r"^[-*]\s*", "", line)
            item = re.sub(r"^\d+\.\s*", "", item).strip()
            if not item or item.lower() in {"none", "없음", "n/a"}:
                continue
            if len(item) > 220:
                item = item[:217] + "..."
            entry = f"{reviewer}: {item}"
            triage[current_bucket].append(entry)
            by_reviewer[reviewer][current_bucket].append(item)

    return triage, by_reviewer


def extract_review_triage(
    comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> dict[str, list[str]]:
    triage, _ = collect_review_triage(comments, reviewer_roster)
    return triage


def extract_reviewer_triage(
    comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> dict[str, dict[str, list[str]]]:
    _, by_reviewer = collect_review_triage(comments, reviewer_roster)
    return by_reviewer


def extract_reviewer_verdicts(
    comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> dict[str, str]:
    verdicts: dict[str, str] = {}
    for comment in comments:
        profile = reviewer_profile_for_comment(comment, reviewer_roster)
        if not profile:
            continue
        reviewer = profile.name

        verdict = clean_template_value(str(comment.get("normalized_verdict") or ""))
        if not verdict:
            for raw_line in (comment.get("content") or "").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                for candidate, pattern in REVIEWER_VERDICT_PATTERNS:
                    if pattern.search(line):
                        verdict = candidate
        if verdict:
            verdicts[reviewer] = verdict
    return verdicts


def extract_reviewer_signal_details(
    comments: list[dict[str, Any]],
    reviewer_roster: ReviewerRoster | None = None,
) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for comment in comments:
        profile = reviewer_profile_for_comment(comment, reviewer_roster)
        if not profile:
            continue
        reviewer = profile.name
        normalized_verdict = clean_template_value(str(comment.get("normalized_verdict") or ""))
        if not normalized_verdict:
            for raw_line in (comment.get("content") or "").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                for candidate, pattern in REVIEWER_VERDICT_PATTERNS:
                    if pattern.search(line):
                        normalized_verdict = candidate
        details[reviewer] = {
            "reviewer": reviewer,
            "reviewer_key": profile.key,
            "signal_source": comment_signal_source(comment),
            "verdict": clean_template_value(str(comment.get("verdict") or "")) or normalized_verdict,
            "normalized_verdict": normalized_verdict,
            "source_event": clean_template_value(str(comment.get("source_event") or "")),
            "source_action": clean_template_value(str(comment.get("source_action") or "")),
            "source_url": clean_template_value(str(comment.get("source_url") or "")),
            "review_state": clean_template_value(str(comment.get("review_state") or "")),
            "author": clean_template_value(str(comment.get("author_name") or comment.get("author_id") or "")),
        }
    return details


def extract_ai_review_meta(body: str | None) -> dict[str, Any]:
    if not body:
        return {}
    match = AI_REVIEW_META_RE.search(body)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_github_review_verdict(value: str | None) -> str | None:
    normalized = normalize_lookup_token(value)
    aliases = {
        "pass": "pass",
        "approved": "approved",
        "approve": "approved",
        "changes-requested": "needs-fix",
        "changesrequested": "needs-fix",
        "request-changes": "needs-fix",
        "requestchanges": "needs-fix",
        "needs-fix": "needs-fix",
        "needsfix": "needs-fix",
        "must-fix": "needs-fix",
        "mustfix": "needs-fix",
        "should-fix": "actionable",
        "shouldfix": "actionable",
        "actionable": "actionable",
        "unclear": "unclear",
        "blocked": "blocked",
        "commented": "commented",
        "ready": "pass",
    }
    return aliases.get(normalized)


def normalized_review_verdict(raw_verdict: str | None) -> str | None:
    normalized = normalize_github_review_verdict(raw_verdict)
    if normalized in {"pass", "approved"}:
        return "ready"
    if normalized in {"needs-fix", "actionable", "unclear", "commented"}:
        return "needs_review"
    if normalized == "blocked":
        return "blocked"
    return None


def extract_github_review_verdict(body: str | None, review_state: str | None) -> tuple[str | None, str | None]:
    meta = extract_ai_review_meta(body)
    meta_verdict = normalize_github_review_verdict(str(meta.get("verdict") or ""))
    if meta_verdict:
        return meta_verdict, normalized_review_verdict(meta_verdict)

    state_verdict = normalize_github_review_verdict(str(GITHUB_REVIEW_STATE_VERDICTS.get(str(review_state or "").strip().upper()) or ""))
    if state_verdict == "needs-fix":
        return state_verdict, normalized_review_verdict(state_verdict)

    for candidate, pattern in GITHUB_REVIEW_VERDICT_PATTERNS:
        if pattern.search(body or ""):
            return candidate, normalized_review_verdict(candidate)

    if state_verdict:
        return state_verdict, normalized_review_verdict(state_verdict)
    return None, None


def extract_approval_signal(
    comments: list[dict[str, Any]],
    current_head_sha: str | None = None,
) -> dict[str, Any]:
    ordered_comments = sorted(comments, key=lambda item: str(item.get("created_at") or ""))
    gate_history = gate_metadata_comments(ordered_comments)
    comments_by_id = {
        comment.get("id"): comment
        for comment in ordered_comments
        if isinstance(comment.get("id"), str) and comment.get("id")
    }
    normalized_current_head = clean_template_value(current_head_sha)
    latest: dict[str, Any] = {
        "command": None,
        "effective_command": None,
        "created_at": None,
        "author_type": None,
        "snippet": None,
        "head_sha": normalized_current_head,
        "current_head_sha": normalized_current_head,
        "stale": False,
        "valid": False,
        "comment_id": None,
        "parent_id": None,
    }
    ignored_signals: list[dict[str, Any]] = []

    for comment in ordered_comments:
        content = comment.get("content") or ""
        command = extract_approval_command(content)
        if not command:
            continue

        signal_head_sha = approval_head_sha_for_comment(
            comment,
            comments_by_id,
            gate_history,
            normalized_current_head,
        )
        signal = {
            "command": command,
            "created_at": comment.get("created_at"),
            "author_id": clean_template_value(str(comment.get("author_id") or "")),
            "author_type": comment.get("author_type"),
            "snippet": approval_signal_snippet(content),
            "head_sha": signal_head_sha,
            "current_head_sha": normalized_current_head,
            "stale": bool(
                normalized_current_head
                and signal_head_sha
                and clean_template_value(signal_head_sha) != normalized_current_head
            ),
            "comment_id": comment.get("id"),
            "parent_id": comment.get("parent_id"),
        }
        signal["valid"] = is_member_comment(comment) and not signal["stale"]
        signal["effective_command"] = command if signal["valid"] else None

        if is_member_comment(comment):
            latest = signal
            continue

        author_type = normalize_lookup_token(str(comment.get("author_type") or ""))
        if author_type in {"agent", "system"} and HERMES_APPROVAL_MIRRORED_MARKER not in content:
            ignored_signals.append(signal)

    return {
        "signal": latest,
        "ignored_signals": ignored_signals,
    }


def triage_signal_texts(
    triage: dict[str, list[str]],
    reviewer_triage: dict[str, dict[str, list[str]]],
    threads: list[dict[str, Any]],
) -> list[str]:
    texts = [item for items in triage.values() for item in items]
    for reviewer_items in reviewer_triage.values():
        for items in reviewer_items.values():
            texts.extend(items)
    for thread in threads:
        for comment in thread.get("comments", {}).get("nodes", []):
            body = (comment.get("body") or "").strip()
            if body:
                texts.append(body)
    return texts


def contains_any_hint(texts: list[str], hints: list[str]) -> bool:
    lowered = [text.lower() for text in texts]
    return any(hint in text for text in lowered for hint in hints)


def has_high_signal_conflict(
    reviewer_triage: dict[str, dict[str, list[str]]],
    reviewer_verdicts: dict[str, str],
    required_reviewers: list[str],
) -> bool:
    present_high_signal = dedupe_preserve(required_reviewers)
    if len(present_high_signal) < 2:
        return False
    if not all(reviewer in reviewer_triage for reviewer in present_high_signal):
        return False

    verdicts = {
        reviewer: reviewer_verdicts.get(reviewer)
        for reviewer in present_high_signal
        if reviewer_verdicts.get(reviewer)
    }
    if len(set(verdicts.values())) > 1:
        return True

    must_fix_presence = {
        reviewer: bool(reviewer_triage.get(reviewer, {}).get("must-fix"))
        for reviewer in present_high_signal
    }
    return len(set(must_fix_presence.values())) > 1 and any(must_fix_presence.values())


def build_approval_reasons(
    triage: dict[str, list[str]],
    reviewer_triage: dict[str, dict[str, list[str]]],
    reviewer_verdicts: dict[str, str],
    check_summary: dict[str, list[str]],
    current_threads: list[dict[str, Any]],
    apply_plan: list[dict[str, str]],
    required_reviewers: list[str],
) -> list[str]:
    reasons: list[str] = []
    texts = triage_signal_texts(triage, reviewer_triage, current_threads)
    if check_summary["failing"]:
        reasons.append("ci-failing")
    if check_summary["pending"]:
        reasons.append("ci-pending")
    if triage["must-fix"]:
        reasons.append("unresolved-must-fix")
    if current_threads:
        reasons.append("unresolved-review-threads")
    if has_high_signal_conflict(reviewer_triage, reviewer_verdicts, required_reviewers):
        reasons.append("claude-codex-conflict")
    if triage["question"] or contains_any_hint(texts, PRODUCT_POLICY_HINTS):
        reasons.append("product-or-policy-decision")
    if contains_any_hint(texts, RISKY_CHANGE_HINTS):
        reasons.append("risky-change")
    if any(entry["action"] == "create-follow-up-issue" for entry in apply_plan) or contains_any_hint(
        texts,
        SCOPE_EXPANSION_HINTS,
    ):
        reasons.append("scope-expansion")
    return dedupe_preserve(reasons)


def build_merge_candidate_blockers(
    approval_reasons: list[str],
    gate_ready: bool,
    pr: dict[str, Any] | None,
    default_base_ref: str | None,
    approval_signal: dict[str, Any],
) -> list[str]:
    blockers = [reason for reason in approval_reasons if reason in MERGE_BLOCKING_APPROVAL_REASONS]
    if not gate_ready:
        blockers.append("missing-required-reviewers")

    if pr:
        if bool(pr.get("isDraft")):
            blockers.append("pr-draft")

        merge_state = clean_template_value(str(pr.get("mergeStateStatus") or ""))
        if not merge_state or merge_state.upper() not in MERGE_STATE_APPROVAL_READY:
            blockers.append("merge-state-not-clean")

        normalized_default_base = clean_template_value(default_base_ref)
        current_base = clean_template_value(str(pr.get("baseRefName") or ""))
        if normalized_default_base and current_base and current_base != normalized_default_base:
            blockers.append("non-default-base")

    if approval_signal.get("stale"):
        blockers.append("stale-head-approval")

    return dedupe_preserve(blockers)


def is_ready_for_approved_merge(
    merge_candidate_blockers: list[str],
) -> bool:
    return not merge_candidate_blockers


def recommend_approval_action(
    approval_reasons: list[str],
    merge_candidate_ready: bool,
    has_actionable_fix: bool,
    apply_plan: list[dict[str, str]],
) -> str:
    if "claude-codex-conflict" in approval_reasons or "product-or-policy-decision" in approval_reasons:
        return "hold"
    if merge_candidate_ready:
        return "merge"
    if any(entry["action"] == "create-follow-up-issue" for entry in apply_plan) and "unresolved-must-fix" not in approval_reasons:
        return "split"
    if has_actionable_fix:
        return "fix"
    return "hold"


def approval_option(command: str) -> dict[str, str]:
    options = {
        "merge": {
            "command": "hermes approve merge",
            "summary": "현재 상태를 승인하고 자동 병합 후보 판단으로 진행",
        },
        "fix": {
            "command": "hermes approve fix",
            "summary": "현재 PR 범위 안에서 리뷰 반영 작업을 진행",
        },
        "split": {
            "command": "hermes approve split",
            "summary": "범위가 큰 항목은 child issue로 분리하고 현재 PR은 최소 수정만 진행",
        },
        "hold": {
            "command": "hermes approve hold",
            "summary": "추가 판단 전까지 자동 조치를 보류",
        },
    }
    return options[command]


def build_approval_request(
    recommendation: str,
    approval_reasons: list[str],
    merge_candidate_ready: bool,
    apply_plan: list[dict[str, str]],
) -> dict[str, Any]:
    if merge_candidate_ready:
        commands = ["merge", "fix", "hold"]
    elif any(entry["action"] == "create-follow-up-issue" for entry in apply_plan):
        commands = ["split", "fix", "hold"]
    else:
        commands = ["fix", "split", "hold"]

    options = []
    for index, command in enumerate(commands, start=1):
        label = chr(ord("A") + index - 1)
        option = approval_option(command)
        options.append({"label": label, **option})

    return {
        "marker": "[hermes:approval-needed]",
        "recommended": recommendation,
        "recommended_command": approval_option(recommendation)["command"],
        "reason_labels": [APPROVAL_REASON_LABELS.get(reason, reason) for reason in approval_reasons],
        "options": options,
        "policy_note": APPROVAL_POLICY_NOTE,
    }


def render_triage(triage: dict[str, list[str]]) -> list[str]:
    lines: list[str] = []
    for bucket in TRIAGE_BUCKETS:
        lines.append(f"### {bucket}")
        items = triage.get(bucket) or []
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- none collected")
        lines.append("")
    return lines[:-1]


def needs_follow_up(item: str) -> bool:
    lowered = item.lower()
    return len(item) > 180 or any(hint in lowered for hint in FOLLOW_UP_HINTS)


def build_apply_plan(triage: dict[str, list[str]]) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    for bucket in TRIAGE_BUCKETS:
        for item in triage.get(bucket) or []:
            if bucket == "must-fix":
                if needs_follow_up(item):
                    action = "create-follow-up-issue"
                    reason = "merge 전에 필요하지만 범위가 커서 별도 추적이 안전함"
                else:
                    action = "apply-now"
                    reason = "merge 전 correctness/regression 리스크로 취급"
            elif bucket == "should-fix":
                if needs_follow_up(item):
                    action = "create-follow-up-issue"
                    reason = "품질 개선이지만 현재 PR 범위를 넘을 가능성이 큼"
                else:
                    action = "apply-if-low-risk"
                    reason = "작고 명확하면 현재 PR에 반영, 아니면 deferred"
            elif bucket == "question":
                action = "needs-decision"
                reason = "리뷰어/사용자 결정 없이는 구현 방향을 단정하지 않음"
            else:
                action = "no-code-change"
                reason = "approval, FYI, duplicate, stale 성격으로 처리"

            plan.append(
                {
                    "bucket": bucket,
                    "action": action,
                    "item": item,
                    "reason": reason,
                }
            )
    return plan


def render_apply_plan(plan: list[dict[str, str]]) -> list[str]:
    if not plan:
        return ["- no actionable review items collected"]

    lines: list[str] = []
    for entry in plan:
        lines.append(
            f"- [{entry['bucket']}] {entry['action']}: {entry['item']} "
            f"({entry['reason']})"
        )
    return lines


def format_skipped_reviewers(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "none"
    output: list[str] = []
    for entry in entries:
        name = entry.get("name") or entry.get("key") or "unknown"
        availability = entry.get("availability") or "unknown"
        reason = entry.get("reason") or "skipped"
        signal_sources = ", ".join(entry.get("signal_sources") or []) or "unknown"
        if reason == "unavailable":
            output.append(f"{name} ({availability})")
        else:
            output.append(f"{name} ({reason}: {signal_sources})")
    return ", ".join(output)


def thread_summary(thread: dict[str, Any]) -> str:
    comments = thread.get("comments", {}).get("nodes", [])
    first = comments[0] if comments else {}
    body = (first.get("body") or "").strip().replace("\n", " ")
    if len(body) > 140:
        body = body[:137] + "..."
    state = "resolved" if thread.get("isResolved") else "unresolved"
    if thread.get("isOutdated"):
        state += ", outdated"
    location = thread.get("path") or "unknown-path"
    line = thread.get("line")
    if line:
        location += f":{line}"
    author = first.get("author", {}).get("login", "unknown")
    return f"- [{state}] {location} ({author}): {body}"


def render_report(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    pr_url: str | None,
    pr: dict[str, Any] | None,
    checks: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    blockers: list[str],
    warnings: list[str],
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    reviewer_roster: ReviewerRoster | None = None,
    extra_review_comments: list[dict[str, Any]] | None = None,
    head_commit: dict[str, Any] | None = None,
    current_head_sha: str | None = None,
    default_base_ref: str | None = None,
) -> str:
    identifier = issue.get("identifier") or issue.get("id")
    title = issue.get("title") or "(untitled)"
    follow_up = build_follow_up_summary(
        comments,
        blockers,
        checks,
        threads,
        required_reviewers,
        supplementary_reviewers,
        reviewer_roster,
        extra_review_comments=extra_review_comments,
        head_commit=head_commit,
        pr=pr,
        current_head_sha=current_head_sha,
        default_base_ref=default_base_ref,
    )

    output: list[str] = [
        f"# PR Review Follow-up Dry Run: {identifier}",
        "",
        "## Issue",
        f"- title: {title}",
        f"- status: {issue.get('status')}",
        f"- priority: {issue.get('priority')}",
        f"- assignee: {issue.get('assignee_type')}/{issue.get('assignee_id')}",
        "",
        "## Follow-up State",
        f"- state: {follow_up['state']}",
        f"- reasons: {', '.join(follow_up['reasons']) or 'none'}",
        f"- merge candidate ready: {follow_up['merge_candidate_ready']}",
        "",
        "## Review Gate",
        f"- reviewer roster: {follow_up['gate'].get('roster_source') or (reviewer_roster.source if reviewer_roster else DEFAULT_REVIEWER_ROSTER.source)}",
        f"- high-signal required: {', '.join(follow_up['gate']['required']) or 'none'}",
        f"- high-signal present: {', '.join(follow_up['gate']['present']) or 'none'}",
        f"- high-signal missing: {', '.join(follow_up['gate']['missing']) or 'none'}",
        f"- high-signal status: {follow_up['gate'].get('required_status') or 'configured'}",
        f"- high-signal skipped: {format_skipped_reviewers(follow_up['gate'].get('required_skipped') or [])}",
        f"- high-signal excluded: {format_skipped_reviewers(follow_up['gate'].get('required_excluded') or [])}",
        f"- supplementary configured: {', '.join(follow_up['gate']['supplementary']['configured']) or 'none'}",
        f"- supplementary present: {', '.join(follow_up['gate']['supplementary']['present']) or 'none'}",
        f"- supplementary missing: {', '.join(follow_up['gate']['supplementary']['missing']) or 'none'}",
        f"- supplementary status: {follow_up['gate']['supplementary'].get('status') or 'configured'}",
        f"- supplementary skipped: {format_skipped_reviewers(follow_up['gate']['supplementary'].get('skipped') or [])}",
        f"- optional configured: {', '.join(follow_up['gate']['optional']['configured']) or 'none'}",
        f"- optional present: {', '.join(follow_up['gate']['optional']['present']) or 'none'}",
        f"- optional skipped: {format_skipped_reviewers(follow_up['gate']['optional'].get('skipped') or [])}",
        "",
        "## Operating Plan",
        *[f"- {step}" for step in REVIEW_LOOP_STEPS],
        "",
        "## Approval Gate",
        f"- required: {follow_up['approval']['required']}",
        f"- detected signal: {follow_up['approval']['signal']['command'] or 'none'}",
        f"- effective signal: {follow_up['approval']['signal']['effective_command'] or 'none'}",
        f"- detected at: {follow_up['approval']['signal']['created_at'] or 'n/a'}",
        f"- recommendation: {follow_up['approval']['recommendation']}",
        f"- recommended command: {follow_up['approval']['request']['recommended_command']}",
        f"- policy: {follow_up['approval']['request']['policy_note']}",
        "",
        f"{follow_up['approval']['request']['marker']}",
    ]

    if follow_up["approval"]["reasons"]:
        output.extend(
            f"- reason: {APPROVAL_REASON_LABELS.get(reason, reason)}"
            for reason in follow_up["approval"]["reasons"]
        )
    else:
        output.append("- reason: none")

    for option in follow_up["approval"]["request"]["options"]:
        output.append(f"- option {option['label']}: `{option['command']}` - {option['summary']}")

    output.extend(
        [
            "",
            "## Blockers",
        ]
    )

    if blockers:
        output.extend(f"- {blocker}" for blocker in blockers)
    else:
        output.append("- none")

    if warnings:
        output.extend(["", "## Warnings"])
        output.extend(f"- {warning}" for warning in warnings)

    output.extend(["", "## Pull Request"])
    if pr_url:
        output.append(f"- url: {pr_url}")
    else:
        output.append("- url: missing")

    if pr:
        output.extend(
            [
                f"- title: {pr.get('title')}",
                f"- state: {pr.get('state')}",
                f"- draft: {pr.get('isDraft')}",
                f"- reviewDecision: {pr.get('reviewDecision')}",
                f"- mergeStateStatus: {pr.get('mergeStateStatus')}",
                f"- branch: {pr.get('headRefName')} -> {pr.get('baseRefName')}",
            ]
        )

    output.extend(["", "## Checks"])
    if checks:
        for check in checks:
            name = check.get("name")
            state = check.get("state")
            workflow = check.get("workflow")
            suffix = f" ({workflow})" if workflow else ""
            output.append(f"- {name}: {state}{suffix}")
        if follow_up["failing_checks"]:
            output.append(f"- failing summary: {' | '.join(follow_up['failing_checks'])}")
        if follow_up["pending_checks"]:
            output.append(f"- pending summary: {' | '.join(follow_up['pending_checks'])}")
    else:
        output.append("- not collected")

    output.extend(["", "## Review Threads"])
    actionable_threads = actionable_review_threads(threads)
    if actionable_threads:
        output.extend(thread_summary(thread) for thread in actionable_threads)
    else:
        output.append("- no unresolved current threads collected")

    output.extend(["", "## Reviewer Verdicts"])
    if follow_up["reviewer_verdicts"]:
        for reviewer in ordered_reviewers(list(follow_up["reviewer_verdicts"].keys()), reviewer_roster):
            output.append(f"- {reviewer}: {follow_up['reviewer_verdicts'][reviewer]}")
    else:
        output.append("- none parsed")

    output.extend(["", "## Review Sources"])
    output.extend(summarize_review_sources([*comments, *(extra_review_comments or [])], reviewer_roster))

    triage = extract_review_triage(
        filter_review_comments(
            [*comments, *(extra_review_comments or [])],
            set(follow_up["gate"].get("excluded_reviewer_keys") or []),
            reviewer_roster,
        ),
        reviewer_roster,
    )
    apply_plan = build_apply_plan(triage)
    output.extend(
        [
            "",
            "## Review Triage",
            *render_triage(triage),
            "",
            "## Apply Plan",
            *render_apply_plan(apply_plan),
            "",
            "## Recent Comments and Signals",
        ]
    )
    recent = summarize_comments(comments)
    output.extend(recent or ["- none"])

    output.extend(
        [
            "",
            "## Default Write Policy",
            "- GitHub comments, review submissions, thread resolution, merge, push, branch protection changes are disabled in this dry run.",
            "- Approval signal이 없으면 코드 수정/자동 병합으로 진행하지 않는다.",
            "- Multica comment posting is enabled only with --post-comment.",
        ]
    )
    return "\n".join(output)


def collect_issue_follow_up_context(
    issue_lookup_id: str,
    issue: dict[str, Any],
    explicit_pr_url: str | None,
    required_reviewers: list[str],
    supplementary_reviewers: list[str],
    reviewer_roster: ReviewerRoster | None = None,
    event_name: str | None = None,
    event_action: str | None = None,
    sender: str | None = None,
    review_author: str | None = None,
    review_id: str | None = None,
    review_state: str | None = None,
    review_body: str | None = None,
    review_url: str | None = None,
    comment_author: str | None = None,
    comment_id: str | None = None,
    comment_body: str | None = None,
    comment_url: str | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    base_comments, comments_error = get_comments(issue_lookup_id)
    warnings: list[str] = []
    if comments_error:
        warnings.append(f"comment collection failed: {comments_error}")

    pr_url = detect_pr_url(issue, base_comments, explicit_pr_url)
    blockers: list[str] = []
    pr: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = []
    threads: list[dict[str, Any]] = []
    github_review_comments: list[dict[str, Any]] = []
    head_commit: dict[str, Any] | None = None
    default_base_ref: str | None = None
    github_signal, github_signal_error = synthesize_github_reviewer_signal(
        reviewer_roster or DEFAULT_REVIEWER_ROSTER,
        pr_url,
        head_sha,
        event_name=event_name,
        event_action=event_action,
        sender=sender,
        review_author=review_author,
        review_id=review_id,
        review_state=review_state,
        review_body=review_body,
        review_url=review_url,
        comment_author=comment_author,
        comment_id=comment_id,
        comment_body=comment_body,
        comment_url=comment_url,
    )

    if not pr_url:
        blockers.append(github_signal_error or "blocked/missing-pr-url")
    else:
        parsed = parse_pr_url(pr_url)
        if not parsed:
            blockers.append("blocked/invalid-pr-url")
        else:
            auth_error = gh_auth_error()
            if auth_error:
                blockers.append(f"blocked/gh-auth: {auth_error}")
            else:
                repo, number = parsed
                pr, pr_error = gh_pr_view(pr_url)
                if pr_error:
                    warnings.append(f"gh pr view failed: {pr_error}")
                default_base_ref, default_base_error = gh_repo_default_branch(repo)
                if default_base_error:
                    warnings.append(f"gh repo view failed: {default_base_error}")
                checks, checks_error = gh_pr_checks(pr_url)
                if checks_error:
                    warnings.append(f"gh pr checks failed: {checks_error}")
                threads, threads_error = gh_review_threads(repo, number)
                if threads_error:
                    warnings.append(f"gh reviewThreads failed: {threads_error}")
                reviews, reviews_error = gh_pr_reviews(repo, number)
                if reviews_error:
                    warnings.append(f"gh pr reviews failed: {reviews_error}")
                issue_comments, issue_comments_error = gh_issue_comments(repo, number)
                if issue_comments_error:
                    warnings.append(f"gh issue comments failed: {issue_comments_error}")
                head_commit, head_commit_error = gh_commit_details(
                    repo,
                    first_present(clean_template_value(head_sha), clean_template_value(str((pr or {}).get("headRefOid") or ""))),
                )
                if head_commit_error:
                    warnings.append(f"gh head commit failed: {head_commit_error}")
                github_review_comments = synthesize_github_review_comments(
                    repo,
                    pr,
                    reviews,
                    issue_comments,
                    reviewer_roster,
                )
    extra_review_comments = list(github_review_comments)
    if github_signal:
        extra_review_comments.append(github_signal)
    extra_review_comments = dedupe_review_comments(
        sorted(extra_review_comments, key=lambda item: str(item.get("created_at") or ""))
    )
    effective_head_sha = first_present(
        clean_template_value(head_sha),
        clean_template_value(str((pr or {}).get("headRefOid") or "")),
    )

    follow_up = build_follow_up_summary(
        base_comments,
        blockers,
        checks,
        threads,
        required_reviewers,
        supplementary_reviewers,
        reviewer_roster,
        extra_review_comments=extra_review_comments,
        head_commit=head_commit,
        pr=pr,
        current_head_sha=effective_head_sha,
        default_base_ref=default_base_ref,
    )
    comments = sorted([*base_comments, *extra_review_comments], key=lambda item: str(item.get("created_at") or ""))
    triage = extract_review_triage(
        filter_review_comments(
            comments,
            set(follow_up["gate"].get("excluded_reviewer_keys") or []),
            reviewer_roster,
        ),
        reviewer_roster,
    )
    apply_plan = build_apply_plan(triage)
    report = render_report(
        issue,
        base_comments,
        pr_url,
        pr,
        checks,
        threads,
        blockers,
        warnings,
        required_reviewers,
        supplementary_reviewers,
        reviewer_roster,
        extra_review_comments=extra_review_comments,
        head_commit=head_commit,
        current_head_sha=effective_head_sha,
        default_base_ref=default_base_ref,
    )
    return {
        "comments": comments,
        "pr_url": pr_url,
        "warnings": warnings,
        "blockers": blockers,
        "pr": pr,
        "default_base_ref": default_base_ref,
        "current_head_sha": effective_head_sha,
        "checks": checks,
        "threads": threads,
        "follow_up": follow_up,
        "triage": triage,
        "apply_plan": apply_plan,
        "report": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only PR review follow-up report from a Multica issue."
    )
    parser.add_argument("issue_id", nargs="?", help="Multica issue id or identifier, for example ITT-102")
    parser.add_argument("--pr-url", help="GitHub PR URL. If omitted, the script scans issue text/comments.")
    parser.add_argument("--output", choices=["markdown", "json"], default="markdown")
    parser.add_argument(
        "--review-pack",
        action="store_true",
        help="Render the reviewer dispatch pack and intake template instead of the follow-up report.",
    )
    parser.add_argument(
        "--reviewers",
        default=",".join(DEFAULT_REVIEWERS),
        help="Comma-separated reviewer names for --review-pack. Default: Claude,Codex,Gemini,Copilot.",
    )
    parser.add_argument(
        "--scan-status",
        help="Scan Multica issues with this status for PR review follow-up candidates, for example in_review.",
    )
    parser.add_argument("--scan-limit", type=int, default=50, help="Maximum issues to scan with --scan-status.")
    parser.add_argument("--resolve-pr-url", help="Resolve Multica issues linked to this GitHub PR URL.")
    parser.add_argument(
        "--merged-aftercare-pr-url",
        help="Run merged-PR aftercare for this GitHub PR URL. Default is dry-run unless --apply-aftercare is set.",
    )
    parser.add_argument(
        "--statuses",
        default="",
        help=(
            "Comma-separated issue statuses used with --resolve-pr-url or --merged-aftercare-pr-url. "
            "Defaults depend on mode."
        ),
    )
    parser.add_argument(
        "--reviewer-roster-file",
        help=(
            "Path to a reviewer roster JSON file keyed by reviewer id/name. "
            "Each entry may set role, availability, legacy_names, signal_source, and agent_ids."
        ),
    )
    parser.add_argument(
        "--required-reviewers",
        help=(
            "Comma-separated high-signal reviewer names or keys required before a final follow-up decision. "
            "Defaults to Claude,Codex, or the active required reviewers from --reviewer-roster-file."
        ),
    )
    parser.add_argument(
        "--supplementary-reviewers",
        help=(
            "Comma-separated supplementary reviewer names or keys included in synthesis but not hard-blocking. "
            "Defaults to Gemini,Copilot, or the active supplementary reviewers from --reviewer-roster-file."
        ),
    )
    parser.add_argument(
        "--create-triage-issue-on-miss",
        action="store_true",
        help="When --resolve-pr-url finds no safe Multica issue match, create a blocked needs-triage tracking issue.",
    )
    parser.add_argument(
        "--fallback-project-title",
        help="Optional Multica project title to use when repo->project inference has no exact match.",
    )
    parser.add_argument("--event-name", help="Webhook event name, for example pull_request_review.")
    parser.add_argument("--event-action", help="Webhook event action, for example submitted.")
    parser.add_argument("--sender", help="Webhook sender login.")
    parser.add_argument("--review-author", help="GitHub review author login.")
    parser.add_argument("--review-id", help="GitHub review id for semantic webhook idempotency.")
    parser.add_argument("--review-state", help="GitHub review state, for example COMMENTED.")
    parser.add_argument("--review-body", help="GitHub review body text from webhook payload.")
    parser.add_argument("--review-url", help="GitHub review URL from webhook payload.")
    parser.add_argument("--comment-author", help="GitHub comment author login.")
    parser.add_argument("--comment-id", help="GitHub comment id for semantic webhook idempotency.")
    parser.add_argument("--comment-body", help="GitHub comment body text from webhook payload.")
    parser.add_argument("--comment-url", help="GitHub comment URL from webhook payload.")
    parser.add_argument("--head-sha", help="Pull request head SHA for dedupe and tracking.")
    parser.add_argument("--head-ref", help="Pull request head branch name for merged PR aftercare.")
    parser.add_argument("--base-ref", help="Pull request base branch name for merged PR aftercare.")
    parser.add_argument("--pr-merged", help="Webhook pull_request.merged flag for merged PR aftercare.")
    parser.add_argument("--merge-commit-sha", help="Merge commit SHA for merged PR aftercare.")
    parser.add_argument("--merged-at", help="Merge timestamp for merged PR aftercare.")
    parser.add_argument("--merged-by", help="GitHub login that merged the PR.")
    parser.add_argument(
        "--stabilize-seconds",
        type=float,
        default=0.0,
        help="Optional delay before a second data collection pass for webhook stability checks.",
    )
    parser.add_argument(
        "--stabilize-attempts",
        type=int,
        default=1,
        help="Number of total collection attempts when --stabilize-seconds is set.",
    )
    parser.add_argument(
        "--post-comment",
        action="store_true",
        help="Post the generated report back to the Multica issue. GitHub writes remain disabled.",
    )
    parser.add_argument(
        "--apply-aftercare",
        action="store_true",
        help="Apply issue status/comment updates when using --merged-aftercare-pr-url.",
    )
    args = parser.parse_args()
    try:
        reviewer_roster = load_cli_reviewer_roster(
            args.reviewer_roster_file,
            args.required_reviewers,
            args.supplementary_reviewers,
        )
    except ValueError as exc:
        print(f"blocked/reviewer-roster: {exc}", file=sys.stderr)
        return 2
    required_reviewers = ordered_reviewers(
        reviewer_names_for_role("required", reviewer_roster, active_only=True),
        reviewer_roster,
    )
    supplementary_reviewers = ordered_reviewers(
        reviewer_names_for_role("supplementary", reviewer_roster, active_only=True),
        reviewer_roster,
    )
    status_overrides = parse_csv(args.statuses)
    resolve_statuses = status_overrides or DEFAULT_RESOLVE_STATUSES
    merged_aftercare_statuses = status_overrides or DEFAULT_MERGED_AFTERCARE_STATUSES

    if args.scan_status:
        candidates, warnings = scan_pr_candidates(args.scan_status, args.scan_limit, reviewer_roster)
        report = render_candidate_scan(candidates, warnings)
        if args.output == "json":
            print(
                json.dumps(
                    {
                        "status": args.scan_status,
                        "limit": args.scan_limit,
                        "candidates": candidates,
                        "warnings": warnings,
                        "reviewer_roster": reviewer_roster_payload(reviewer_roster),
                        "report": report,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(report)
        return 0 if any(candidate.get("pr_url") for candidate in candidates) else 1

    if args.merged_aftercare_pr_url:
        canonical = canonical_pr_url(args.merged_aftercare_pr_url)
        parsed = parse_pr_url(args.merged_aftercare_pr_url) if canonical else None
        merge_context = {
            "pr_url": canonical or args.merged_aftercare_pr_url,
            "repo_full_name": parsed[0] if parsed else None,
            "pr_number": parsed[1] if parsed else None,
            "pr_merged": parse_bool_flag(args.pr_merged),
            "head_sha": clean_template_value(args.head_sha),
            "head_ref": clean_template_value(args.head_ref),
            "base_ref": clean_template_value(args.base_ref),
            "merge_commit_sha": clean_template_value(args.merge_commit_sha),
            "merged_at": clean_template_value(args.merged_at),
            "merged_by": clean_template_value(args.merged_by),
        }
        resolution = resolve_pr_context(
            args.merged_aftercare_pr_url,
            merged_aftercare_statuses,
            args.scan_limit,
            required_reviewers,
            supplementary_reviewers,
            reviewer_roster,
            args.create_triage_issue_on_miss,
            args.fallback_project_title,
            args.event_name,
            args.event_action,
            args.sender,
            args.review_author,
            args.review_state,
            args.comment_author,
            args.head_sha,
        )
        aftercare = None
        combined_warnings = list(resolution["warnings"])
        if merge_context["pr_merged"] is False:
            resolution["resolution_state"] = "not-merged"
        elif resolution["matches"]:
            aftercare = run_merged_aftercare(
                merge_context["pr_url"],
                resolution["matches"],
                merged_aftercare_statuses,
                merge_context,
                args.apply_aftercare,
            )
            combined_warnings.extend(aftercare["warnings"])
            combined_warnings.extend(aftercare["errors"])
        report = render_merged_aftercare_report(
            merge_context,
            merged_aftercare_statuses,
            resolution,
            aftercare,
            dedupe_preserve(combined_warnings),
        )
        if args.output == "json":
            print(
                json.dumps(
                    {
                        "pr_url": merge_context["pr_url"],
                        "statuses": merged_aftercare_statuses,
                        "merge": merge_context,
                        "required_reviewers": required_reviewers,
                        "supplementary_reviewers": supplementary_reviewers,
                        "reviewer_roster": reviewer_roster_payload(reviewer_roster),
                        "matches": resolution["matches"],
                        "safe_references": resolution["safe_references"],
                        "resolution": {
                            "state": resolution["resolution_state"],
                            "next_action": (
                                "merged aftercare completed"
                                if aftercare and aftercare["state"] in {"completed", "dry-run", "noop"}
                                else "skip external repo"
                                if resolution["resolution_state"] == EXTERNAL_REPO_RESOLUTION_STATE
                                else "not a merged PR"
                                if resolution["resolution_state"] == "not-merged"
                                else "human triage required"
                            ),
                        },
                        "triage_preview": resolution["triage_preview"],
                        "created_issue": resolution["created_issue"],
                        "aftercare": aftercare,
                        "warnings": dedupe_preserve(combined_warnings),
                        "report": report,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(report)
        if aftercare and aftercare["state"] in {"completed", "dry-run", "noop"}:
            return 0
        if resolution["created_issue"]:
            return 0
        if resolution["resolution_state"] in {EXTERNAL_REPO_RESOLUTION_STATE, "not-merged"}:
            return 0
        return 1

    if args.resolve_pr_url:
        resolution = resolve_pr_context(
            args.resolve_pr_url,
            resolve_statuses,
            args.scan_limit,
            required_reviewers,
            supplementary_reviewers,
            reviewer_roster,
            args.create_triage_issue_on_miss,
            args.fallback_project_title,
            args.event_name,
            args.event_action,
            args.sender,
            args.review_author,
            args.review_state,
            args.comment_author,
            args.head_sha,
        )
        report = render_pr_match_report(
            args.resolve_pr_url,
            resolve_statuses,
            required_reviewers,
            supplementary_reviewers,
            resolution["matches"],
            resolution["safe_references"],
            resolution["triage_preview"],
            resolution["created_issue"],
            resolution["resolution_state"],
            resolution["warnings"],
            reviewer_roster,
        )
        if args.output == "json":
            print(
                json.dumps(
                    {
                        "pr_url": canonical_pr_url(args.resolve_pr_url),
                        "statuses": resolve_statuses,
                        "required_reviewers": required_reviewers,
                        "supplementary_reviewers": supplementary_reviewers,
                        "reviewer_roster": reviewer_roster_payload(reviewer_roster),
                        "matches": resolution["matches"],
                        "safe_references": resolution["safe_references"],
                        "resolution": {
                            "state": resolution["resolution_state"],
                            "next_action": (
                                "follow up on the linked issue"
                                if len(resolution["matches"]) == 1
                                else "skip external repo"
                                if resolution["resolution_state"] == EXTERNAL_REPO_RESOLUTION_STATE
                                else "human triage required"
                            ),
                        },
                        "triage_preview": resolution["triage_preview"],
                        "created_issue": resolution["created_issue"],
                        "warnings": resolution["warnings"],
                        "report": report,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(report)
        if len(resolution["matches"]) == 1:
            return 0
        if resolution["created_issue"]:
            return 0
        if resolution["resolution_state"] == EXTERNAL_REPO_RESOLUTION_STATE:
            return 0
        return 1

    if not args.issue_id:
        parser.error(
            "issue_id is required unless --scan-status, --resolve-pr-url, or --merged-aftercare-pr-url is used"
        )

    issue, issue_error = get_issue(args.issue_id)
    if issue_error or not issue:
        print(f"blocked/multica-issue: {issue_error}", file=sys.stderr)
        return 2

    reviewers = [name.strip() for name in args.reviewers.split(",") if name.strip()]
    issue_lookup_id = issue.get("id") or args.issue_id

    if args.review_pack:
        comments, comments_error = get_comments(issue_lookup_id)
        warnings: list[str] = []
        if comments_error:
            warnings.append(f"comment collection failed: {comments_error}")
        pr_url = detect_pr_url(issue, comments, args.pr_url)
        report = render_review_request_pack(issue, comments, pr_url, reviewers)
        if args.output == "json":
            post_error = add_multica_comment(issue_lookup_id, report) if args.post_comment else None
            print(
                json.dumps(
                    {
                        "issue": {
                            "id": issue.get("id"),
                            "identifier": issue.get("identifier"),
                            "title": issue.get("title"),
                            "status": issue.get("status"),
                        },
                        "pr_url": pr_url,
                        "reviewers": reviewers,
                        "posted_comment": args.post_comment and post_error is None,
                        "post_error": post_error,
                        "report": report,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(report)
            if args.post_comment:
                post_error = add_multica_comment(issue_lookup_id, report)
                if post_error:
                    print(f"blocked/multica-comment: {post_error}", file=sys.stderr)
                    return 2
        return 1 if not pr_url else 0

    context = collect_issue_follow_up_context(
        str(issue_lookup_id),
        issue,
        args.pr_url,
        required_reviewers,
        supplementary_reviewers,
        reviewer_roster,
        event_name=args.event_name,
        event_action=args.event_action,
        sender=args.sender,
        review_author=args.review_author,
        review_id=args.review_id,
        review_state=args.review_state,
        review_body=args.review_body,
        review_url=args.review_url,
        comment_author=args.comment_author,
        comment_id=args.comment_id,
        comment_body=args.comment_body,
        comment_url=args.comment_url,
        head_sha=args.head_sha,
    )
    stabilization_runs = 1
    if (
        args.stabilize_seconds > 0
        and args.stabilize_attempts > 1
        and context["pr_url"]
        and not context["blockers"]
    ):
        for _ in range(args.stabilize_attempts - 1):
            time.sleep(args.stabilize_seconds)
            context = collect_issue_follow_up_context(
                str(issue_lookup_id),
                issue,
                context["pr_url"],
                required_reviewers,
                supplementary_reviewers,
                reviewer_roster,
                event_name=args.event_name,
                event_action=args.event_action,
                sender=args.sender,
                review_author=args.review_author,
                review_id=args.review_id,
                review_state=args.review_state,
                review_body=args.review_body,
                review_url=args.review_url,
                comment_author=args.comment_author,
                comment_id=args.comment_id,
                comment_body=args.comment_body,
                comment_url=args.comment_url,
                head_sha=args.head_sha,
            )
            stabilization_runs += 1

    notification = build_notification_policy(
        issue=issue,
        comments=context["comments"],
        pr_url=context["pr_url"],
        head_sha=context["current_head_sha"] or args.head_sha,
        follow_up=context["follow_up"],
        pr=context["pr"],
        checks=context["checks"],
        reviewer_roster=reviewer_roster,
        event_name=args.event_name,
        event_action=args.event_action,
        review_author=args.review_author,
        comment_author=args.comment_author,
        review_state=args.review_state,
        review_id=args.review_id,
        comment_id=args.comment_id,
    )
    notification["stabilization_runs"] = stabilization_runs
    ignored_approval_notification = build_agent_approval_ignored_notification(
        issue=issue,
        comments=context["comments"],
        head_sha=context["current_head_sha"] or args.head_sha,
        follow_up=context["follow_up"],
    )

    if args.output == "json":
        post_errors: list[str] = []
        posted_gate_comment = False
        posted_ignored_approval_comment = False
        if args.post_comment:
            gate_post_error = post_comment_notification(str(issue_lookup_id), notification)
            if gate_post_error:
                post_errors.append(gate_post_error)
            else:
                posted_gate_comment = bool(notification.get("should_post"))

            ignored_post_error = post_comment_notification(str(issue_lookup_id), ignored_approval_notification)
            if ignored_post_error:
                post_errors.append(ignored_post_error)
            else:
                posted_ignored_approval_comment = bool(ignored_approval_notification.get("should_post"))
        post_error = "; ".join(post_errors) if post_errors else None
        print(
            json.dumps(
                {
                    "issue": {
                        "id": issue.get("id"),
                        "identifier": issue.get("identifier"),
                        "title": issue.get("title"),
                        "status": issue.get("status"),
                    },
                    "pr_url": context["pr_url"],
                    "blockers": context["blockers"],
                    "warnings": context["warnings"],
                    "follow_up": context["follow_up"],
                    "triage": context["triage"],
                    "apply_plan": context["apply_plan"],
                    "notification": notification,
                    "ignored_approval_notification": ignored_approval_notification,
                    "required_reviewers": required_reviewers,
                    "supplementary_reviewers": supplementary_reviewers,
                    "reviewer_roster": reviewer_roster_payload(reviewer_roster),
                    "posted_comment": args.post_comment and (posted_gate_comment or posted_ignored_approval_comment),
                    "posted_gate_comment": posted_gate_comment,
                    "posted_ignored_approval_comment": posted_ignored_approval_comment,
                    "post_error": post_error,
                    "report": context["report"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(context["report"])
        if args.post_comment:
            gate_post_error = post_comment_notification(str(issue_lookup_id), notification)
            ignored_post_error = post_comment_notification(str(issue_lookup_id), ignored_approval_notification)
            post_errors = [error for error in [gate_post_error, ignored_post_error] if error]
            if post_errors:
                print(f"blocked/multica-comment: {'; '.join(post_errors)}", file=sys.stderr)
                return 2

    return 1 if context["follow_up"]["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
