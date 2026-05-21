from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


TESTS_DIR = pathlib.Path(__file__).resolve().parent
MODULE_PATH = TESTS_DIR.parent / "review_followup.py"
SPEC = importlib.util.spec_from_file_location("review_followup", MODULE_PATH)
review_followup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = review_followup
SPEC.loader.exec_module(review_followup)


def reviewer_comment(author_id: str, content: str) -> dict[str, str]:
    return {
        "author_id": author_id,
        "author_type": "agent",
        "created_at": "2026-05-19T10:00:00Z",
        "content": content,
    }


def member_comment(
    content: str,
    *,
    created_at: str = "2026-05-19T10:05:00Z",
    parent_id: str | None = None,
) -> dict[str, str | None]:
    return {
        "author_id": "member-1",
        "author_type": "member",
        "created_at": created_at,
        "content": content,
        "parent_id": parent_id,
    }


def issue_comment(
    comment_id: str,
    content: str,
    created_at: str,
    parent_id: str | None = None,
) -> dict[str, str | None]:
    return {
        "id": comment_id,
        "author_id": "member-1",
        "author_type": "member",
        "created_at": created_at,
        "content": content,
        "parent_id": parent_id,
    }


def success_check() -> dict[str, str]:
    return {"name": "CI", "state": "SUCCESS"}


def pending_check() -> dict[str, str]:
    return {"name": "CI", "state": "IN_PROGRESS"}


def gate_comment(
    comment_id: str,
    head_sha: str,
    created_at: str,
) -> dict[str, str | None]:
    return issue_comment(
        comment_id,
        (
            "[hermes:pr-review-gate]\n\n"
            f"- head SHA: `{head_sha}`\n\n"
            "<!-- hermes:pr-review-gate-meta "
            f'{{"head_sha":"{head_sha}","state":"approval_needed"}}'
            " -->"
        ),
        created_at,
    )


def approval_ignored_comment(dedupe_key: str, source_comment_id: str) -> dict[str, str | None]:
    return issue_comment(
        "ignored-approval",
        (
            "[hermes:agent-approval-ignored]\n\n"
            "<!-- hermes:agent-approval-ignored-meta "
            f'{{"dedupe_key":"{dedupe_key}","source_comment_id":"{source_comment_id}"}}'
            " -->"
        ),
        "2026-05-19T10:07:00Z",
        parent_id=source_comment_id,
    )


CLAUDE_ID = "ac215516-af99-4832-b5f8-d8cb99e51260"
CODEX_ID = "cbe053f4-b53e-4786-81de-6554ddb86fad"
GEMINI_ID = "cc7dd930-ea0f-485f-b74b-134e1da1c2f1"


def write_roster(payload: dict) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    with handle:
        json.dump(payload, handle)
    return handle.name


def head_commit(author_login: str, author_name: str | None = None) -> dict[str, object]:
    return {
        "author": {"login": author_login},
        "committer": {"login": author_login},
        "commit": {
            "author": {"name": author_name or author_login},
            "committer": {"name": author_name or author_login},
        },
    }


def ready_pr(
    *,
    head_sha: str = "3d69830e1f3b30347b585d2d59c7bc26c3914b34",
    base_ref: str = "main",
    merge_state: str = "CLEAN",
    is_draft: bool = False,
) -> dict[str, object]:
    return {
        "headRefOid": head_sha,
        "baseRefName": base_ref,
        "mergeStateStatus": merge_state,
        "isDraft": is_draft,
    }


def github_signal(
    *,
    pr_url: str = "https://github.com/ittae/ittae/pull/423",
    head_sha: str = "3d69830e1f3b30347b585d2d59c7bc26c3914b34",
    event_name: str = "issue_comment",
    event_action: str = "created",
    sender: str | None = None,
    review_author: str | None = None,
    review_id: str | None = None,
    review_state: str | None = None,
    review_body: str | None = None,
    review_url: str | None = None,
    comment_author: str | None = "claude",
    comment_id: str | None = "4496448621",
    comment_body: str | None = None,
    comment_url: str | None = "https://github.com/ittae/ittae/pull/423#issuecomment-4496448621",
) -> dict[str, str]:
    signal, error = review_followup.synthesize_github_reviewer_signal(
        review_followup.DEFAULT_REVIEWER_ROSTER,
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
    assert error is None
    assert signal is not None
    return signal


class ReviewFollowupPolicyTest(unittest.TestCase):
    def build_follow_up(
        self,
        comments,
        checks=None,
        threads=None,
        blockers=None,
        reviewer_roster=None,
        required_reviewers=None,
        supplementary_reviewers=None,
        extra_review_comments=None,
        head_commit_payload=None,
        pr_payload=None,
        current_head_sha=None,
        default_base_ref="main",
    ):
        return review_followup.build_follow_up_summary(
            comments=comments,
            blockers=blockers or [],
            checks=checks or [],
            threads=threads or [],
            required_reviewers=required_reviewers or review_followup.DEFAULT_REQUIRED_REVIEWERS,
            supplementary_reviewers=supplementary_reviewers or review_followup.DEFAULT_SUPPLEMENTARY_REVIEWERS,
            reviewer_roster=reviewer_roster,
            extra_review_comments=extra_review_comments,
            head_commit=head_commit_payload,
            pr=pr_payload,
            current_head_sha=current_head_sha,
            default_base_ref=default_base_ref,
        )

    def test_collecting_reviews_when_codex_missing(self):
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## Triage\n- non-actionable:\n  - looks good\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                GEMINI_ID,
                "## Triage\n- should-fix:\n  - maybe add one small assertion\n\n## Final State\n- needs another review",
            ),
        ]

        follow_up = self.build_follow_up(comments, checks=[success_check()])

        self.assertEqual(follow_up["state"], "collecting_reviews")
        self.assertIn("missing-high-signal-reviewers: Codex", follow_up["reasons"])
        self.assertFalse(follow_up["gate"]["supplementary"]["missing"] == review_followup.DEFAULT_SUPPLEMENTARY_REVIEWERS)

    def test_approval_needed_before_fix_without_human_command(self):
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## must-fix\n- add regression coverage for login redirect\n\n## Final State\n- needs another review",
            ),
            reviewer_comment(
                CODEX_ID,
                "## must-fix\n- handle failing null branch in auth service\n\n## Final State\n- blocked",
            ),
        ]

        follow_up = self.build_follow_up(comments, checks=[success_check()])

        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertIn("unresolved-must-fix", follow_up["approval"]["reasons"])
        self.assertIn("fix-approval-missing", follow_up["reasons"])
        self.assertEqual(follow_up["approval"]["request"]["marker"], "[hermes:approval-needed]")

    def test_needs_agent_fix_after_fix_approval(self):
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## must-fix\n- add regression coverage for login redirect\n\n## Final State\n- needs another review",
            ),
            reviewer_comment(
                CODEX_ID,
                "## should-fix\n- tighten assertion around retry count\n\n## Final State\n- needs another review",
            ),
            member_comment("hermes approve fix"),
        ]

        follow_up = self.build_follow_up(comments, checks=[success_check()])

        self.assertEqual(follow_up["state"], "needs_agent_fix")
        self.assertEqual(follow_up["approval"]["signal"]["command"], "fix")

    def test_ready_for_approved_merge_requires_success_and_merge_approval(self):
        pr = ready_pr()
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests and structure look good\n\n## Final State\n- ready to merge",
            ),
            member_comment("hermes approve merge"),
        ]

        follow_up = self.build_follow_up(
            comments,
            checks=[success_check()],
            pr_payload=pr,
            current_head_sha=str(pr["headRefOid"]),
            default_base_ref="main",
        )

        self.assertTrue(follow_up["merge_candidate_ready"])
        self.assertEqual(follow_up["state"], "ready_for_approved_merge")
        self.assertEqual(follow_up["approval"]["signal"]["command"], "merge")
        self.assertEqual(follow_up["approval"]["signal"]["effective_command"], "merge")

    def test_ready_for_approved_merge_rejects_draft_unclean_or_non_default_base(self):
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests and structure look good\n\n## Final State\n- ready to merge",
            ),
            member_comment("hermes approve merge"),
        ]
        cases = [
            ("pr-draft", ready_pr(is_draft=True), "pr-draft"),
            ("merge-state-not-clean", ready_pr(merge_state="BLOCKED"), "merge-state-not-clean"),
            ("non-default-base", ready_pr(base_ref="release/1.0"), "non-default-base"),
        ]

        for _label, pr, reason in cases:
            with self.subTest(reason=reason):
                follow_up = self.build_follow_up(
                    comments,
                    checks=[success_check()],
                    pr_payload=pr,
                    current_head_sha=str(pr["headRefOid"]),
                    default_base_ref="main",
                )

                self.assertFalse(follow_up["merge_candidate_ready"])
                self.assertEqual(follow_up["state"], "approval_needed")
                self.assertIn(reason, follow_up["approval"]["reasons"])

    def test_high_signal_conflict_stays_in_approval_needed(self):
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## must-fix\n- change rollout policy before merge\n\n## Final State\n- blocked",
            ),
        ]

        follow_up = self.build_follow_up(comments, checks=[success_check()])

        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertIn("claude-codex-conflict", follow_up["approval"]["reasons"])
        self.assertEqual(follow_up["approval"]["recommendation"], "hold")

    def test_stale_merge_approval_after_head_change_requires_reapproval(self):
        old_head = "99c92fb94f74fbb66c5c78635d6dd69eb389dc4d"
        new_head = "3d69830e1f3b30347b585d2d59c7bc26c3914b34"
        pr = ready_pr(head_sha=new_head)
        comments = [
            gate_comment("gate-1", old_head, "2026-05-19T10:04:00Z"),
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests and structure look good\n\n## Final State\n- ready to merge",
            ),
            member_comment(
                "hermes approve merge",
                created_at="2026-05-19T10:05:00Z",
                parent_id="gate-1",
            ),
        ]

        follow_up = self.build_follow_up(
            comments,
            checks=[success_check()],
            pr_payload=pr,
            current_head_sha=new_head,
            default_base_ref="main",
        )

        self.assertFalse(follow_up["merge_candidate_ready"])
        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertTrue(follow_up["approval"]["signal"]["stale"])
        self.assertIsNone(follow_up["approval"]["signal"]["effective_command"])
        self.assertIn("stale-head-approval", follow_up["approval"]["reasons"])
        self.assertIn("stale-head-approval", follow_up["reasons"])

    def test_agent_approval_is_ignored_for_signal_extraction(self):
        pr = ready_pr()
        agent_command = {
            "id": "agent-approval",
            "author_id": "agent-1",
            "author_type": "agent",
            "created_at": "2026-05-19T10:05:00Z",
            "content": "hermes approve fix",
            "parent_id": None,
        }
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## must-fix\n- add regression coverage for login redirect\n\n## Final State\n- needs another review",
            ),
            reviewer_comment(
                CODEX_ID,
                "## should-fix\n- tighten assertion around retry count\n\n## Final State\n- needs another review",
            ),
            agent_command,
        ]

        follow_up = self.build_follow_up(
            comments,
            checks=[success_check()],
            pr_payload=pr,
            current_head_sha=str(pr["headRefOid"]),
            default_base_ref="main",
        )

        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertIsNone(follow_up["approval"]["signal"]["command"])
        self.assertEqual(len(follow_up["approval"]["ignored_signals"]), 1)
        self.assertEqual(follow_up["approval"]["ignored_signals"][0]["command"], "fix")
        self.assertIn("fix-approval-missing", follow_up["reasons"])

    def test_non_member_mirrored_approval_does_not_take_effect(self):
        pr = ready_pr()
        mirrored_comment = {
            "id": "mirror-approval",
            "author_id": "hermes",
            "author_type": "agent",
            "created_at": "2026-05-19T10:05:00Z",
            "content": "[hermes:approval-mirrored]\n\nhermes approve merge",
            "parent_id": None,
        }
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests and structure look good\n\n## Final State\n- ready to merge",
            ),
            mirrored_comment,
        ]

        follow_up = self.build_follow_up(
            comments,
            checks=[success_check()],
            pr_payload=pr,
            current_head_sha=str(pr["headRefOid"]),
            default_base_ref="main",
        )

        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertIn("merge-approval-missing", follow_up["reasons"])
        self.assertEqual(follow_up["approval"]["ignored_signals"], [])

    def test_roster_file_skips_unavailable_and_unsupported_reviewers(self):
        roster_path = write_roster(
            {
                "reviewers": {
                    "claude-code": {
                        "display_name": "Claude",
                        "role": "required",
                        "availability": "paused",
                        "legacy_names": ["Claude"],
                        "signal_source": "multica",
                        "agent_ids": [CLAUDE_ID],
                    },
                    "codex": {
                        "display_name": "Codex",
                        "role": "required",
                        "availability": "active",
                        "legacy_names": ["Codex"],
                        "signal_source": "multica",
                        "agent_ids": [CODEX_ID],
                    },
                    "gemini": {
                        "display_name": "Gemini",
                        "role": "supplementary",
                        "availability": "active",
                        "legacy_names": ["Gemini"],
                        "signal_source": "manual",
                        "agent_ids": [GEMINI_ID],
                    },
                }
            }
        )
        reviewer_roster = review_followup.load_cli_reviewer_roster(roster_path, None, None)
        comments = [
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
            )
        ]

        follow_up = review_followup.build_follow_up_summary(
            comments=comments,
            blockers=[],
            checks=[success_check()],
            threads=[],
            required_reviewers=review_followup.reviewer_names_for_role("required", reviewer_roster, active_only=True),
            supplementary_reviewers=review_followup.reviewer_names_for_role(
                "supplementary",
                reviewer_roster,
                active_only=True,
            ),
            reviewer_roster=reviewer_roster,
        )

        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertEqual(follow_up["gate"]["required"], ["Codex"])
        self.assertEqual(follow_up["gate"]["missing"], [])
        self.assertEqual(follow_up["gate"]["required_status"], "configured")
        self.assertEqual(follow_up["gate"]["required_skipped"][0]["name"], "Claude")
        self.assertEqual(follow_up["gate"]["required_skipped"][0]["availability"], "paused")
        self.assertEqual(follow_up["gate"]["supplementary"]["configured"], [])
        self.assertEqual(follow_up["gate"]["supplementary"]["status"], "not_configured")
        self.assertEqual(follow_up["gate"]["supplementary"]["skipped"][0]["name"], "Gemini")
        self.assertEqual(
            follow_up["gate"]["supplementary"]["skipped"][0]["reason"],
            "unsupported-signal-source",
        )

    def test_legacy_display_name_overrides_still_map_to_roster_keys(self):
        roster_path = write_roster(
            {
                "reviewers": {
                    "claude-code": {
                        "display_name": "claude-code",
                        "role": "optional",
                        "availability": "active",
                        "legacy_names": ["Claude"],
                        "signal_source": "multica",
                        "agent_ids": [CLAUDE_ID],
                    },
                    "codex": {
                        "display_name": "codex",
                        "role": "optional",
                        "availability": "active",
                        "legacy_names": ["Codex"],
                        "signal_source": "multica",
                        "agent_ids": [CODEX_ID],
                    },
                    "gemini": {
                        "display_name": "gemini",
                        "role": "optional",
                        "availability": "active",
                        "legacy_names": ["Gemini"],
                        "signal_source": "multica",
                        "agent_ids": [GEMINI_ID],
                    },
                }
            }
        )

        reviewer_roster = review_followup.load_cli_reviewer_roster(
            roster_path,
            "Claude,Codex",
            "Gemini",
        )

        self.assertEqual(
            [profile.key for profile in review_followup.reviewer_profiles_for_role("required", reviewer_roster)],
            ["claude-code", "codex"],
        )
        self.assertEqual(
            [profile.key for profile in review_followup.reviewer_profiles_for_role("supplementary", reviewer_roster)],
            ["gemini"],
        )

    def test_github_claude_issue_comment_satisfies_required_signal(self):
        comments = [
            github_signal(
                comment_body=(
                    "## 🎯 코드 리뷰 결과 (iteration 1)\n\n"
                    "**총점: 10.0 / 10.0** ✅ PASS\n\n"
                    "<!-- ai-review-meta\n"
                    '{"score": 10.0, "iteration": 1, "high": 0, "medium": 0, "low": 0, "verdict": "PASS"}\n'
                    "-->"
                )
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
            ),
        ]

        follow_up = self.build_follow_up(comments, checks=[success_check()])

        self.assertEqual(follow_up["gate"]["present"], ["Claude", "Codex"])
        self.assertEqual(follow_up["reviewer_verdicts"]["Claude"], "ready")
        self.assertEqual(follow_up["reviewer_signals"]["Claude"]["signal_source"], "github-review")
        self.assertEqual(follow_up["reviewer_signals"]["Claude"]["verdict"], "pass")

    def test_codex_self_review_is_excluded_from_required_gate(self):
        roster_path = write_roster(
            {
                "reviewers": {
                    "claude-code": {
                        "display_name": "Claude",
                        "role": "required",
                        "availability": "active",
                        "legacy_names": ["Claude", "claude", "claude[bot]"],
                        "signal_source": ["multica", "github-review"],
                        "agent_ids": [CLAUDE_ID],
                        "github_logins": ["claude", "claude[bot]"],
                        "excluded_when_worker": False,
                    },
                    "codex": {
                        "display_name": "Codex",
                        "role": "required",
                        "availability": "active",
                        "legacy_names": ["Codex"],
                        "signal_source": "multica",
                        "agent_ids": [CODEX_ID],
                        "github_logins": ["codex-bot"],
                        "excluded_when_worker": True,
                    },
                }
            }
        )
        reviewer_roster = review_followup.load_cli_reviewer_roster(roster_path, None, None)
        comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- worker validated the patch\n\n## Final State\n- ready to merge",
            ),
        ]

        follow_up = self.build_follow_up(
            comments,
            checks=[success_check()],
            reviewer_roster=reviewer_roster,
            required_reviewers=review_followup.reviewer_names_for_role("required", reviewer_roster, active_only=True),
            supplementary_reviewers=review_followup.reviewer_names_for_role(
                "supplementary",
                reviewer_roster,
                active_only=True,
            ),
            head_commit_payload=head_commit("codex-bot", "Codex"),
        )

        self.assertEqual(follow_up["gate"]["required"], ["Claude"])
        self.assertEqual(follow_up["gate"]["required_excluded"][0]["name"], "Codex")
        self.assertTrue(follow_up["gate"]["ready"])
        self.assertEqual(follow_up["reviewer_signals"]["Codex"]["signal_source"], "multica")
        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertIn("merge-approval-missing", follow_up["reasons"])

    def test_pr_423_policy_smoke_reaches_merge_approval_missing_with_claude_github_signal(self):
        roster_path = write_roster(
            {
                "reviewers": {
                    "claude-code": {
                        "display_name": "Claude",
                        "role": "required",
                        "availability": "active",
                        "legacy_names": ["Claude", "claude", "claude[bot]"],
                        "signal_source": ["multica", "github-review"],
                        "agent_ids": [CLAUDE_ID],
                        "github_logins": ["claude", "claude[bot]"],
                        "excluded_when_worker": False,
                    },
                    "codex": {
                        "display_name": "Codex",
                        "role": "required",
                        "availability": "active",
                        "legacy_names": ["Codex"],
                        "signal_source": "multica",
                        "agent_ids": [CODEX_ID],
                        "github_logins": ["codex-bot"],
                        "excluded_when_worker": True,
                    },
                    "gemini": {
                        "display_name": "Gemini",
                        "role": "supplementary",
                        "availability": "active",
                        "legacy_names": ["Gemini"],
                        "signal_source": ["github-review"],
                        "agent_ids": [GEMINI_ID],
                    },
                }
            }
        )
        reviewer_roster = review_followup.load_cli_reviewer_roster(roster_path, None, None)
        comments = [
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- worker fix is green locally\n\n## Final State\n- ready to merge",
            )
        ]
        extra_review_comments = [
            github_signal(
                comment_author="claude[bot]",
                comment_body=(
                    "## 🎯 코드 리뷰 결과 (iteration 1)\n\n"
                    "**총점: 10.0 / 10.0** ✅ PASS\n\n"
                    "<!-- ai-review-meta\n"
                    '{"score": 10.0, "iteration": 1, "high": 0, "medium": 0, "low": 0, "verdict": "PASS"}\n'
                    "-->"
                ),
            )
        ]

        follow_up = self.build_follow_up(
            comments,
            checks=[success_check()],
            reviewer_roster=reviewer_roster,
            required_reviewers=review_followup.reviewer_names_for_role("required", reviewer_roster, active_only=True),
            supplementary_reviewers=review_followup.reviewer_names_for_role(
                "supplementary",
                reviewer_roster,
                active_only=True,
            ),
            extra_review_comments=extra_review_comments,
            head_commit_payload=head_commit("codex-bot", "Codex"),
            pr_payload=ready_pr(),
            current_head_sha="3d69830e1f3b30347b585d2d59c7bc26c3914b34",
            default_base_ref="main",
        )

        self.assertTrue(follow_up["gate"]["ready"])
        self.assertEqual(follow_up["gate"]["present"], ["Claude"])
        self.assertEqual(follow_up["gate"]["required_excluded"][0]["name"], "Codex")
        self.assertTrue(follow_up["merge_candidate_ready"])
        self.assertEqual(follow_up["state"], "approval_needed")
        self.assertIn("merge-approval-missing", follow_up["reasons"])
        self.assertEqual(follow_up["reviewer_signals"]["Claude"]["signal_source"], "github-review")
        self.assertEqual(follow_up["reviewer_signals"]["Claude"]["normalized_verdict"], "ready")

    def test_non_pr_issue_comment_is_blocked_safely(self):
        signal, error = review_followup.synthesize_github_reviewer_signal(
            review_followup.DEFAULT_REVIEWER_ROSTER,
            None,
            "3d69830e1f3b30347b585d2d59c7bc26c3914b34",
            event_name="issue_comment",
            comment_author="claude",
            comment_body="✅ PASS",
        )

        self.assertIsNone(signal)
        self.assertEqual(error, "blocked/non-pr-issue-comment")


class ReviewFollowupNotificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = {
            "id": "issue-141",
            "identifier": "ITT-141",
            "title": "PR #423 CI/review follow-up fix",
            "status": "in_review",
            "priority": "high",
            "assignee_type": "agent",
            "assignee_id": CODEX_ID,
        }
        self.pr_url = "https://github.com/ittae/ittae/pull/423"
        self.head_sha = "3d69830e1f3b30347b585d2d59c7bc26c3914b34"

    def build_follow_up(self, comments, checks=None):
        return review_followup.build_follow_up_summary(
            comments=comments,
            blockers=[],
            checks=checks or [],
            threads=[],
            required_reviewers=review_followup.DEFAULT_REQUIRED_REVIEWERS,
            supplementary_reviewers=review_followup.DEFAULT_SUPPLEMENTARY_REVIEWERS,
            pr=ready_pr(head_sha=self.head_sha),
            current_head_sha=self.head_sha,
            default_base_ref="main",
        )

    def build_notification(self, comments, issue_comments=None, checks=None, review_id="review-1"):
        follow_up = self.build_follow_up(comments, checks=checks)
        return review_followup.build_notification_policy(
            issue=self.issue,
            comments=issue_comments or comments,
            pr_url=self.pr_url,
            head_sha=self.head_sha,
            follow_up=follow_up,
            pr=ready_pr(head_sha=self.head_sha),
            checks=checks or [],
            reviewer_roster=review_followup.DEFAULT_REVIEWER_ROSTER,
            event_name="pull_request_review",
            event_action="submitted",
            review_author="copilot-pull-request-reviewer",
            comment_author=None,
            review_state="COMMENTED",
            review_id=review_id,
            comment_id=None,
        )

    def test_collecting_reviews_only_posts_first_same_head_notification(self):
        reviewer_comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- looks good\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                GEMINI_ID,
                "## non-actionable\n- FYI only\n\n## Final State\n- needs another review",
            ),
        ]
        first_notification = self.build_notification(reviewer_comments, checks=[pending_check()], review_id="review-1")
        existing_comments = [
            issue_comment(
                "gate-1",
                first_notification["comment_body"],
                "2026-05-20T08:42:09Z",
            )
        ]

        repeated_notification = self.build_notification(
            reviewer_comments,
            issue_comments=existing_comments + reviewer_comments,
            checks=[success_check()],
            review_id="review-2",
        )

        self.assertFalse(repeated_notification["should_post"])
        self.assertEqual(
            repeated_notification["suppression_reason"],
            "same-head-collecting_reviews-already-notified",
        )

    def test_same_head_misfire_is_replied_with_correction(self):
        approval_comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
            ),
        ]
        misfired_notification = self.build_notification(
            approval_comments,
            checks=[success_check()],
            review_id="review-approval",
        )
        existing_comments = [
            issue_comment(
                "gate-misfire",
                misfired_notification["comment_body"],
                "2026-05-20T08:58:37Z",
            )
        ]
        collecting_comments = [
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- codex already responded\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                GEMINI_ID,
                "## non-actionable\n- FYI only\n\n## Final State\n- needs another review",
            ),
        ]

        correction = self.build_notification(
            collecting_comments,
            issue_comments=existing_comments + collecting_comments,
            checks=[success_check()],
            review_id="review-correction",
        )

        self.assertTrue(correction["should_post"])
        self.assertEqual(correction["mode"], "reply")
        self.assertEqual(correction["parent_comment_id"], "gate-misfire")
        self.assertIn("정정합니다", correction["comment_body"])

    def test_duplicate_approval_needed_semantics_are_suppressed(self):
        reviewer_comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
            ),
        ]
        first_notification = self.build_notification(reviewer_comments, checks=[success_check()], review_id="review-3")
        existing_comments = [
            issue_comment(
                "gate-approval",
                first_notification["comment_body"],
                "2026-05-20T08:58:37Z",
            )
        ]

        repeated = self.build_notification(
            reviewer_comments,
            issue_comments=existing_comments + reviewer_comments,
            checks=[success_check()],
            review_id="review-4",
        )

        self.assertFalse(repeated["should_post"])
        self.assertEqual(repeated["suppression_reason"], "duplicate-semantic-state")

    @patch.object(review_followup, "add_multica_comment")
    def test_post_notification_comment_skips_suppressed_notifications(self, mock_add_multica_comment) -> None:
        post_error = review_followup.post_notification_comment(
            "issue-141",
            {
                "should_post": False,
                "comment_body": "ignored",
                "parent_comment_id": None,
            },
        )

        self.assertIsNone(post_error)
        mock_add_multica_comment.assert_not_called()

    @patch.object(review_followup, "add_multica_comment", return_value=None)
    def test_post_notification_comment_uses_parent_reply_when_present(self, mock_add_multica_comment) -> None:
        post_error = review_followup.post_notification_comment(
            "issue-141",
            {
                "should_post": True,
                "comment_body": "reply body",
                "parent_comment_id": "gate-misfire",
            },
        )

        self.assertIsNone(post_error)
        mock_add_multica_comment.assert_called_once_with("issue-141", "reply body", "gate-misfire")

    def test_agent_approval_ignored_notification_replies_once_per_dedupe_key(self) -> None:
        agent_signal = {
            "comment_id": "agent-approval",
            "author_id": "agent-1",
            "author_type": "agent",
            "command": "fix",
            "head_sha": self.head_sha,
        }
        follow_up = {
            "approval": {
                "signal": {"current_head_sha": self.head_sha},
                "ignored_signals": [agent_signal],
            }
        }

        notification = review_followup.build_agent_approval_ignored_notification(
            self.issue,
            [],
            self.head_sha,
            follow_up,
        )

        self.assertTrue(notification["should_post"])
        self.assertEqual(notification["mode"], "reply")
        self.assertEqual(notification["parent_comment_id"], "agent-approval")
        self.assertIn("[hermes:agent-approval-ignored]", notification["comment_body"])

        duplicate = review_followup.build_agent_approval_ignored_notification(
            self.issue,
            [approval_ignored_comment(notification["dedupe_key"], "agent-approval")],
            self.head_sha,
            follow_up,
        )

        self.assertFalse(duplicate["should_post"])
        self.assertEqual(duplicate["suppression_reason"], "duplicate-agent-approval-ignored")

    def test_dedupe_key_changes_when_signal_source_or_verdict_changes(self):
        multica_comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
            ),
        ]
        github_comments = [
            github_signal(
                comment_body=(
                    "## 🎯 코드 리뷰 결과\n\n"
                    "**총점: 10.0 / 10.0** ✅ PASS\n\n"
                    "<!-- ai-review-meta\n"
                    '{"score": 10.0, "iteration": 1, "high": 0, "medium": 0, "low": 0, "verdict": "PASS"}\n'
                    "-->"
                )
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
            ),
        ]

        multica_notification = self.build_notification(multica_comments, checks=[success_check()], review_id="review-multica")
        github_notification = self.build_notification(github_comments, checks=[success_check()], review_id="review-github")

        self.assertNotEqual(multica_notification["dedupe_key"], github_notification["dedupe_key"])
        claude_status = next(
            entry for entry in github_notification["reviewer_statuses"] if entry["name"] == "Claude"
        )
        self.assertEqual(claude_status["last_signal_source"], "github-review")
        self.assertEqual(claude_status["verdict"], "pass")

    def test_auto_merge_request_null_is_reported_as_informational_not_a_blocker(self):
        reviewer_comments = [
            reviewer_comment(
                CLAUDE_ID,
                "## non-actionable\n- no blocking concerns\n\n## Final State\n- ready to merge",
            ),
            reviewer_comment(
                CODEX_ID,
                "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
            ),
        ]
        follow_up = self.build_follow_up(reviewer_comments, checks=[success_check()])

        policy = review_followup.build_notification_policy(
            issue=self.issue,
            comments=reviewer_comments,
            pr_url=self.pr_url,
            head_sha=self.head_sha,
            follow_up=follow_up,
            pr={"mergeStateStatus": "CLEAN", "autoMergeRequest": None},
            checks=[success_check()],
            reviewer_roster=review_followup.DEFAULT_REVIEWER_ROSTER,
            event_name="pull_request_review",
            event_action="submitted",
            review_author="copilot-pull-request-reviewer",
            comment_author=None,
            review_state="COMMENTED",
            review_id="review-auto-merge-null",
            comment_id=None,
        )

        self.assertTrue(policy["should_post"])
        self.assertIn("- GitHub auto-merge: `not_enabled`", policy["comment_body"])
        self.assertIn("GitHub auto-merge field is informational; `null` is not a review blocker.", policy["comment_body"])
        self.assertNotIn("autoMergeRequest", follow_up["reasons"])
        self.assertNotIn("autoMergeRequest", follow_up["approval"]["reasons"])


if __name__ == "__main__":
    unittest.main()
