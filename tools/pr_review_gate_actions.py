#!/usr/bin/env python3
"""Local Hermes PR Review Gate — mutation adapter (Phase 1 wiring, disabled-by-default).

This is the Phase 1 deliverable of ITT-294 / ITT-276 (AI 리뷰 GitHub Action을
로컬 Hermes PR Review Gate로 대체). Phase 0 produced
`local_pr_review_dispatcher.py`, a *dry-run-only* classifier that describes the
labels/comments a live gate would apply via `would_apply_labels` /
`would_post_comments`. This module turns those descriptions into an
executable — but **disabled-by-default** — mutation layer.

Design contract:
    - All GitHub mutations (label add/remove, PR comment, check status) go
      through a single `GateActionExecutor` interface.
    - The default executor is `DryRunExecutor`: it records the exact gh command
      it *would* run and performs **no** mutation. This is what dual-run uses.
    - `GhCliExecutor` performs real mutations, but only after the caller passes
      `enable_mutations=True`. Constructing it disabled (the default) makes
      every mutating call raise — two independent safety gates (pick the live
      executor AND flip the flag) before any side effect can happen.
    - `plan_actions_from_classify` is a pure translation from the Phase 0
      classify result into an ordered list of `PlannedAction`s, so the mapping
      is unit-testable without touching GitHub.

Nothing in this module is auto-invoked by the Phase 0 dispatcher; it is an
independent building block that Phase 1 live wiring will call once dual-run is
validated and live mutation is approved.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Action kinds. Kept as plain string constants so PlannedAction stays trivially
# JSON-serializable for dual-run logging / comparison against the live Action.
ADD_LABELS = "add_labels"
REMOVE_LABELS = "remove_labels"
POST_COMMENT = "post_comment"
SET_CHECK_STATUS = "set_check_status"

# The GitHub Check name the review_followup gate posts in Phase 1+. Kept here so
# the live executor and the dual-run logger agree on a single source of truth.
DEFAULT_CHECK_NAME = "hermes/pr-review-gate"


class GateActionError(Exception):
    """Raised for any user-facing failure in the mutation adapter."""


# ─────────────────────────────────────────────────────────────────────────────
# PR identity (decoupled from the dispatcher's PRRef so this module can be used
# standalone, e.g. from review_followup, without importing the whole dispatcher)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PRTarget:
    repo: str  # "owner/name"
    number: int

    def __post_init__(self) -> None:
        # Require an exact two-part owner/name slug. Reject "owner/", "/name",
        # and "owner/name/extra" so a malformed slug can't slip into a gh path.
        parts = self.repo.split("/") if self.repo else []
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise GateActionError(
                f"PRTarget.repo must be exactly 'owner/name', got: {self.repo!r}"
            )
        if self.number <= 0:
            raise GateActionError(f"PRTarget.number must be positive, got: {self.number!r}")


@dataclass(frozen=True)
class PlannedAction:
    """A single intended mutation. Pure data — no GitHub calls.

    `labels` is used by ADD_LABELS / REMOVE_LABELS.
    `body` is used by POST_COMMENT.
    `check_name` / `head_sha` / `check_conclusion` / `check_summary` /
    `details_url` are used by SET_CHECK_STATUS. `head_sha` is mandatory for a
    real check-run (the Checks API keys the run to a commit), so it has no
    default — the caller must supply the head SHA being reviewed.
    """

    kind: str
    target: PRTarget
    labels: tuple[str, ...] = ()
    body: str = ""
    check_name: str = DEFAULT_CHECK_NAME
    head_sha: str = ""
    check_conclusion: str = ""
    check_summary: str = ""
    details_url: str = ""

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "kind": self.kind,
            "repo": self.target.repo,
            "pr_number": self.target.number,
        }
        if self.kind in (ADD_LABELS, REMOVE_LABELS):
            out["labels"] = list(self.labels)
        elif self.kind == POST_COMMENT:
            out["body"] = self.body
        elif self.kind == SET_CHECK_STATUS:
            out["check_name"] = self.check_name
            out["head_sha"] = self.head_sha
            out["check_conclusion"] = self.check_conclusion
            out["check_summary"] = self.check_summary
            out["details_url"] = self.details_url
        return out


@dataclass
class ActionResult:
    """Outcome of attempting one PlannedAction."""

    action: PlannedAction
    executed: bool  # True only when a real mutation happened (live executor)
    command: list[str] = field(default_factory=list)  # the gh argv (would-run or did-run)
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.to_dict(),
            "executed": self.executed,
            "command": list(self.command),
            "detail": self.detail,
        }


# ─────────────────────────────────────────────────────────────────────────────
# gh command building (shared by dry-run and live so the "would-run" string is
# byte-for-byte what the live path runs — this is what makes dual-run trustworthy)
# ─────────────────────────────────────────────────────────────────────────────


def build_gh_command(action: PlannedAction) -> list[str]:
    """Build the `gh` argv for an action. Pure; no execution.

    Mirrors the mutation calls in claude-code-review.yml's gate-classify job:
        gh pr edit <n> -R <repo> --add-label <l> ...
        gh pr comment <n> -R <repo> --body <body>
    SET_CHECK_STATUS creates/updates a real GitHub check-run via the Checks API
        gh api -X POST repos/<owner>/<repo>/check-runs -f head_sha=<sha> ...
    so it can serve as a required check in Phase 2. This argv documents intent
    and is only ever run by the live executor once that path is wired + approved.
    """
    repo = action.target.repo
    num = str(action.target.number)
    if action.kind == ADD_LABELS:
        if not action.labels:
            raise GateActionError("ADD_LABELS requires at least one label")
        cmd = ["gh", "pr", "edit", num, "-R", repo]
        for label in action.labels:
            cmd += ["--add-label", label]
        return cmd
    if action.kind == REMOVE_LABELS:
        if not action.labels:
            raise GateActionError("REMOVE_LABELS requires at least one label")
        cmd = ["gh", "pr", "edit", num, "-R", repo]
        for label in action.labels:
            cmd += ["--remove-label", label]
        return cmd
    if action.kind == POST_COMMENT:
        if not action.body:
            raise GateActionError("POST_COMMENT requires a non-empty body")
        return ["gh", "pr", "comment", num, "-R", repo, "--body", action.body]
    if action.kind == SET_CHECK_STATUS:
        if not action.head_sha:
            raise GateActionError(
                "SET_CHECK_STATUS requires head_sha (the Checks API keys a "
                "check-run to a commit)."
            )
        if not action.check_conclusion:
            raise GateActionError("SET_CHECK_STATUS requires a conclusion")
        # Real check-run creation via the Checks API. A conclusion implies a
        # completed run, so status=completed is set alongside it.
        cmd = [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/check-runs",
            "-f",
            f"name={action.check_name}",
            "-f",
            f"head_sha={action.head_sha}",
            "-f",
            "status=completed",
            "-f",
            f"conclusion={action.check_conclusion}",
        ]
        if action.details_url:
            cmd += ["-f", f"details_url={action.details_url}"]
        cmd += [
            "-f",
            f"output[title]={action.check_name}",
            "-f",
            f"output[summary]={action.check_summary}",
        ]
        return cmd
    raise GateActionError(f"Unknown action kind: {action.kind!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Executors
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class GateActionExecutor(Protocol):
    mutates: bool

    def execute(self, action: PlannedAction) -> ActionResult: ...


class DryRunExecutor:
    """Default executor. Records the would-run command; never mutates GitHub."""

    mutates = False

    def __init__(self) -> None:
        self.recorded: list[ActionResult] = []

    def execute(self, action: PlannedAction) -> ActionResult:
        command = build_gh_command(action)
        result = ActionResult(
            action=action,
            executed=False,
            command=command,
            detail="dry-run: command recorded, not executed",
        )
        self.recorded.append(result)
        return result


class GhCliExecutor:
    """Live executor. Performs real GitHub mutations via the `gh` CLI.

    Disabled by default: unless `enable_mutations=True` is passed, every call
    raises GateActionError. This is the second safety gate on top of choosing
    the live executor at all.
    """

    mutates = True

    def __init__(
        self,
        *,
        enable_mutations: bool = False,
        runner=subprocess.run,
    ) -> None:
        self.enable_mutations = enable_mutations
        self._runner = runner

    def execute(self, action: PlannedAction) -> ActionResult:
        command = build_gh_command(action)
        if not self.enable_mutations:
            raise GateActionError(
                "GhCliExecutor is disabled (enable_mutations=False). "
                "Live GitHub mutation requires explicit opt-in after dual-run "
                f"validation + approval. Would have run: {' '.join(command)}"
            )
        try:
            completed = self._runner(
                command, check=True, capture_output=True, text=True, encoding="utf-8"
            )
        except FileNotFoundError as exc:
            raise GateActionError("`gh` CLI not found on PATH.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (getattr(exc, "stderr", "") or "").strip()
            raise GateActionError(
                f"gh command failed ({' '.join(command)}): {stderr}"
            ) from exc
        return ActionResult(
            action=action,
            executed=True,
            command=command,
            detail=(getattr(completed, "stdout", "") or "").strip(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# classify → action plan (pure translation)
# ─────────────────────────────────────────────────────────────────────────────


def plan_actions_from_classify(
    classify: dict[str, object],
    target: PRTarget,
) -> list[PlannedAction]:
    """Translate a Phase 0 classify result into an ordered action plan.

    `classify` is the `classify` block emitted by local_pr_review_dispatcher
    (a dict with `would_apply_labels` and `would_post_comments`). The ordering
    matches the live workflow: labels first, then comments.
    """
    if not isinstance(classify, dict):
        raise GateActionError(
            f"classify must be a dict, got {type(classify).__name__}"
        )
    actions: list[PlannedAction] = []

    labels = classify.get("would_apply_labels") or []
    if not isinstance(labels, list):
        raise GateActionError("would_apply_labels must be a list")
    # Drop None and whitespace-only entries; never let `str(None)` => "None"
    # become a real label.
    label_strs = tuple(
        str(label).strip()
        for label in labels
        if label is not None and str(label).strip()
    )
    if label_strs:
        actions.append(
            PlannedAction(kind=ADD_LABELS, target=target, labels=label_strs)
        )

    comments = classify.get("would_post_comments") or []
    if not isinstance(comments, list):
        raise GateActionError("would_post_comments must be a list")
    for comment in comments:
        if not isinstance(comment, dict):
            raise GateActionError("each would_post_comments entry must be a dict")
        body = str(comment.get("body") or "")
        # Skip whitespace-only bodies; keep meaningful body verbatim.
        if not body.strip():
            continue
        actions.append(PlannedAction(kind=POST_COMMENT, target=target, body=body))

    return actions


def apply_actions(
    actions: list[PlannedAction],
    executor: GateActionExecutor | None = None,
) -> list[ActionResult]:
    """Run each planned action through the executor. Defaults to dry-run."""
    if executor is None:
        executor = DryRunExecutor()
    return [executor.execute(action) for action in actions]
