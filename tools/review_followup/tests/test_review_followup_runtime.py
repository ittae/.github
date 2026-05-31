from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import unittest
from unittest.mock import patch


TESTS_DIR = pathlib.Path(__file__).resolve().parent
TOOL_DIR = TESTS_DIR.parent
MODULE_PATH = TOOL_DIR / "review_followup.py"
PROMPT_PATH = TOOL_DIR / "review_followup_webhook_prompt.txt"
RUNBOOK_PATH = TOOL_DIR / "review_followup_runbook.md"
EXAMPLE_ROSTER_PATH = TOOL_DIR / "reviewer_roster.example.json"
SPEC = importlib.util.spec_from_file_location("review_followup", MODULE_PATH)
review_followup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = review_followup
SPEC.loader.exec_module(review_followup)


def sample_issue(issue_id: str, identifier: str, title: str, labels: list[dict] | None = None) -> dict:
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "status": "blocked",
        "priority": "medium",
        "assignee_type": None,
        "assignee_id": None,
        "labels": labels or [],
    }


def linked_issue(
    issue_id: str,
    identifier: str,
    status: str,
    pr_url: str | None,
    parent_issue_id: str | None = None,
) -> dict:
    description = f"GitHub PR: {pr_url}" if pr_url else "No explicit PR URL on the issue."
    return {
        **sample_issue(issue_id, identifier, identifier),
        "status": status,
        "description": description,
        "parent_issue_id": parent_issue_id,
    }


class ReviewFollowupTests(unittest.TestCase):
    def run_main_json(self, argv: list[str]) -> tuple[int, dict]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                exit_code = review_followup.main()
        return exit_code, json.loads(stdout.getvalue())

    def test_source_of_truth_docs_include_pull_request_merge_route(self) -> None:
        runbook = RUNBOOK_PATH.read_text()
        prompt = PROMPT_PATH.read_text()

        self.assertIn(
            '--events "pull_request,pull_request_review,pull_request_review_comment,issue_comment"',
            runbook,
        )
        self.assertIn("--merged-aftercare-pr-url <PR_URL>", prompt)
        self.assertIn(
            "`closed` and `{pull_request.merged}` resolves to `true`",
            prompt,
        )

    def test_extract_issue_references_collects_identifiers_and_mentions(self) -> None:
        text = "Related Multica: ITT-156\nDeep link: mention://issue/12345678-1234-1234-1234-1234567890ab"
        refs = review_followup.extract_issue_references(text)
        self.assertEqual(refs, ["ITT-156", "12345678-1234-1234-1234-1234567890ab"])

    @patch.object(review_followup, "get_comments", return_value=([], None))
    @patch.object(review_followup, "get_issue")
    def test_resolve_issue_refs_from_pr_uses_safe_refs_from_title_body(
        self,
        mock_get_issue,
        _mock_get_comments,
    ) -> None:
        mock_get_issue.return_value = (sample_issue("issue-1", "ITT-156", "linked issue"), None)
        pr = {
            "title": "docs: align webhook handling for ITT-156",
            "body": "No extra refs here.",
        }

        matches, warnings, refs = review_followup.resolve_issue_refs_from_pr(
            pr,
            ["Claude", "Codex"],
            ["Gemini", "Copilot"],
        )

        self.assertEqual(refs, ["ITT-156"])
        self.assertEqual(warnings, [])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["identifier"], "ITT-156")
        self.assertEqual(matches[0]["match_source"], "pr-text-ref")

    def test_prefer_non_tracking_matches_ignores_placeholder_issue(self) -> None:
        tracking = {
            "id": "triage-1",
            "identifier": "ITT-200",
            "title": "needs-triage: .github PR #13 review 이슈 연결 누락",
            "is_tracking_issue": True,
        }
        real = {
            "id": "work-1",
            "identifier": "ITT-156",
            "title": "unlinked PR review webhook 누락 처리",
            "is_tracking_issue": False,
        }

        matches, warnings = review_followup.prefer_non_tracking_matches([tracking, real])

        self.assertEqual([match["identifier"] for match in matches], ["ITT-156"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("ignored tracking placeholder issues", warnings[0])

    @patch.object(review_followup, "infer_project_for_repo")
    def test_build_unlinked_triage_preview_contains_required_metadata(self, mock_infer_project_for_repo) -> None:
        mock_infer_project_for_repo.return_value = ({"id": "project-1", "title": "ittae"}, [])
        pr = {
            "title": "docs: enforce Korean AI PR review language",
            "headRefOid": "ce330e4896b8631a2661b034f31e23d543936130",
        }

        preview, warnings = review_followup.build_unlinked_triage_preview(
            "https://github.com/ittae/.github/pull/13",
            pr,
            [],
            None,
            None,
            "submitted",
            "get6",
            "gemini-code-assist",
            "COMMENTED",
            None,
            "ce330e4896b8631a2661b034f31e23d543936130",
        )

        self.assertEqual(warnings, [])
        self.assertEqual(preview["status"], "blocked")
        self.assertEqual(preview["priority"], "medium")
        self.assertEqual(preview["project_title"], "ittae")
        self.assertEqual(preview["label_name"], "needs-triage")
        self.assertIn("https://github.com/ittae/.github/pull/13", preview["description"])
        self.assertIn("gemini-code-assist", preview["description"])
        self.assertIn("COMMENTED", preview["description"])
        self.assertIn("Dedupe key", preview["description"])

    @patch.object(review_followup, "infer_project_for_repo")
    @patch.object(review_followup, "create_tracking_issue")
    @patch.object(review_followup, "resolve_issue_refs_from_pr", return_value=([], [], []))
    @patch.object(review_followup, "gh_pr_view")
    @patch.object(review_followup, "gh_auth_error", return_value=None)
    @patch.object(review_followup, "resolve_pr_matches", return_value=([], []))
    def test_main_creates_tracking_issue_for_unlinked_ittae_pr(
        self,
        _mock_resolve_pr_matches,
        _mock_gh_auth_error,
        mock_gh_pr_view,
        _mock_resolve_issue_refs_from_pr,
        mock_create_tracking_issue,
        mock_infer_project_for_repo,
    ) -> None:
        mock_infer_project_for_repo.return_value = ({"id": "project-1", "title": "ittae"}, [])
        mock_gh_pr_view.return_value = (
            {
                "title": "docs: enforce Korean AI PR review language",
                "body": "",
                "headRefOid": "ce330e4896b8631a2661b034f31e23d543936130",
            },
            None,
        )
        mock_create_tracking_issue.return_value = (
            sample_issue(
                "triage-1",
                "ITT-200",
                "needs-triage: ittae/.github PR #13 review 이슈 연결 누락",
                labels=[{"id": "label-needs-triage", "name": review_followup.TRACKING_LABEL_NAME}],
            ),
            None,
        )

        exit_code, payload = self.run_main_json(
            [
                "review_followup.py",
                "--resolve-pr-url",
                "https://github.com/ittae/.github/pull/13",
                "--create-triage-issue-on-miss",
                "--fallback-project-title",
                "ittae",
                "--event-action",
                "submitted",
                "--review-author",
                "gemini-code-assist",
                "--review-state",
                "COMMENTED",
                "--head-sha",
                "ce330e4896b8631a2661b034f31e23d543936130",
                "--output",
                "json",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["resolution"]["state"], "needs-triage")
        self.assertEqual(payload["resolution"]["next_action"], "human triage required")
        self.assertEqual(payload["created_issue"]["identifier"], "ITT-200")
        mock_create_tracking_issue.assert_called_once()

    @patch.object(review_followup, "create_tracking_issue")
    @patch.object(review_followup, "resolve_issue_refs_from_pr", return_value=([], [], []))
    @patch.object(review_followup, "gh_pr_view")
    @patch.object(review_followup, "gh_auth_error", return_value=None)
    @patch.object(review_followup, "resolve_pr_matches", return_value=([], []))
    def test_main_skips_unlinked_external_repo(
        self,
        _mock_resolve_pr_matches,
        _mock_gh_auth_error,
        mock_gh_pr_view,
        _mock_resolve_issue_refs_from_pr,
        mock_create_tracking_issue,
    ) -> None:
        mock_gh_pr_view.return_value = (
            {
                "title": "docs: unrelated external repo review",
                "body": "",
                "headRefOid": "8d6f74cb1dca9afb14754c7be7f6c79933a6ec41",
            },
            None,
        )

        exit_code, payload = self.run_main_json(
            [
                "review_followup.py",
                "--resolve-pr-url",
                "https://github.com/external/repo/pull/13",
                "--create-triage-issue-on-miss",
                "--event-action",
                "submitted",
                "--review-author",
                "copilot-pull-request-reviewer",
                "--review-state",
                "COMMENTED",
                "--head-sha",
                "8d6f74cb1dca9afb14754c7be7f6c79933a6ec41",
                "--output",
                "json",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["resolution"]["state"], review_followup.EXTERNAL_REPO_RESOLUTION_STATE)
        self.assertEqual(payload["resolution"]["next_action"], "skip external repo")
        self.assertIsNone(payload["created_issue"])
        self.assertIn("unlinked external repo skipped", payload["warnings"][0])
        mock_create_tracking_issue.assert_not_called()

    @patch.object(review_followup, "create_tracking_issue")
    @patch.object(review_followup, "resolve_pr_matches")
    def test_main_keeps_existing_linked_issue_behavior(
        self,
        mock_resolve_pr_matches,
        mock_create_tracking_issue,
    ) -> None:
        mock_resolve_pr_matches.return_value = (
            [
                {
                    "id": "work-1",
                    "identifier": "ITT-156",
                    "title": "unlinked PR review webhook 누락 처리",
                    "status": "in_review",
                    "priority": "high",
                    "assignee": "agent/codex",
                    "pr_url": "https://github.com/ittae/.github/pull/13",
                    "review_sources": [],
                    "review_gate": {
                        "required": ["Claude", "Codex"],
                        "present": [],
                        "missing": ["Claude", "Codex"],
                        "ready": False,
                        "supplementary": {
                            "configured": ["Gemini", "Copilot"],
                            "present": [],
                            "missing": ["Gemini", "Copilot"],
                        },
                        "all_present": [],
                    },
                    "comment_count": 0,
                    "labels": [],
                    "is_tracking_issue": False,
                    "match_source": "pr-url",
                }
            ],
            [],
        )

        exit_code, payload = self.run_main_json(
            [
                "review_followup.py",
                "--resolve-pr-url",
                "https://github.com/ittae/.github/pull/13",
                "--create-triage-issue-on-miss",
                "--output",
                "json",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["resolution"]["state"], "linked")
        self.assertEqual(payload["resolution"]["next_action"], "follow up on the linked issue")
        self.assertEqual(payload["matches"][0]["identifier"], "ITT-156")
        self.assertIsNone(payload["created_issue"])
        mock_create_tracking_issue.assert_not_called()

    @patch.object(review_followup, "gh_review_threads", return_value=([], None))
    @patch.object(review_followup, "gh_pr_checks", return_value=([{"name": "CI", "state": "SUCCESS"}], None))
    @patch.object(review_followup, "gh_repo_default_branch", return_value=("main", None))
    @patch.object(
        review_followup,
        "gh_pr_view",
        return_value=(
            {
                "title": "test: restore missing business-logic coverage tests for ITT-53",
                "state": "OPEN",
                "isDraft": False,
                "reviewDecision": None,
                "mergeStateStatus": "CLEAN",
                "headRefName": "test/pr-423",
                "baseRefName": "main",
            },
            None,
        ),
    )
    @patch.object(review_followup, "gh_auth_error", return_value=None)
    @patch.object(review_followup, "get_comments")
    @patch.object(review_followup, "get_issue")
    def test_main_accepts_pr_423_claude_github_signal(
        self,
        mock_get_issue,
        mock_get_comments,
        _mock_gh_auth_error,
        _mock_gh_repo_default_branch,
        _mock_gh_pr_view,
        _mock_gh_pr_checks,
        _mock_gh_review_threads,
    ) -> None:
        mock_get_issue.return_value = (
            {
                "id": "issue-141",
                "identifier": "ITT-141",
                "title": "PR #423 CI/review follow-up fix",
                "status": "in_review",
                "priority": "high",
                "assignee_type": "agent",
                "assignee_id": "cbe053f4-b53e-4786-81de-6554ddb86fad",
                "description": "GitHub PR: https://github.com/ittae/ittae/pull/423",
            },
            None,
        )
        mock_get_comments.return_value = (
            [
                {
                    "author_id": "cbe053f4-b53e-4786-81de-6554ddb86fad",
                    "author_type": "agent",
                    "created_at": "2026-05-20T08:57:00Z",
                    "content": "## non-actionable\n- tests look good\n\n## Final State\n- ready to merge",
                }
            ],
            None,
        )

        exit_code, payload = self.run_main_json(
            [
                "review_followup.py",
                "ITT-141",
                "--pr-url",
                "https://github.com/ittae/ittae/pull/423",
                "--reviewer-roster-file",
                str(EXAMPLE_ROSTER_PATH),
                "--event-name",
                "issue_comment",
                "--event-action",
                "created",
                "--comment-author",
                "claude",
                "--comment-id",
                "4496448621",
                "--comment-url",
                "https://github.com/ittae/ittae/pull/423#issuecomment-4496448621",
                "--comment-body",
                "## 🎯 코드 리뷰 결과 (iteration 1)\n\n**총점: 10.0 / 10.0** ✅ PASS\n\n<!-- ai-review-meta\n{\"score\": 10.0, \"iteration\": 1, \"high\": 0, \"medium\": 0, \"low\": 0, \"verdict\": \"PASS\"}\n-->",
                "--head-sha",
                "3d69830e1f3b30347b585d2d59c7bc26c3914b34",
                "--output",
                "json",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["follow_up"]["gate"]["present"], ["Claude", "Codex"])
        self.assertEqual(payload["follow_up"]["reviewer_signals"]["Claude"]["signal_source"], "github-review")
        self.assertEqual(payload["follow_up"]["reviewer_signals"]["Claude"]["verdict"], "pass")
        self.assertEqual(payload["follow_up"]["reviewer_verdicts"]["Claude"], "ready")

    def test_build_merged_aftercare_plan_closes_direct_and_child_explicit_links(self) -> None:
        pr_url = "https://github.com/ittae/ittae/pull/423"
        parent = linked_issue("issue-1", "ITT-53", "in_review", pr_url)
        child = linked_issue("issue-2", "ITT-141", "in_review", pr_url, parent_issue_id="issue-1")
        blocked_child = linked_issue("issue-3", "ITT-142", "blocked", pr_url, parent_issue_id="issue-1")
        issue_by_id = {issue["id"]: issue for issue in [parent, child, blocked_child]}
        issue_records = {
            issue_id: review_followup.build_issue_record(issue, [])
            for issue_id, issue in issue_by_id.items()
        }

        plan = review_followup.build_merged_aftercare_plan(
            pr_url,
            [{"id": "issue-1", "identifier": "ITT-53", "status": "in_review", "match_source": "pr-url"}],
            issue_by_id,
            issue_records,
        )

        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["top_level_match_ids"], ["issue-1"])
        self.assertEqual(
            [(entry["identifier"], entry["reason"]) for entry in plan["updates"]],
            [("ITT-53", "direct-explicit-pr-link"), ("ITT-141", "child-explicit-pr-link")],
        )
        self.assertEqual(plan["skipped"][0]["identifier"], "ITT-142")
        self.assertEqual(plan["skipped"][0]["reason"], "child-status-left-unchanged:blocked")

    def test_build_merged_aftercare_plan_closes_leaf_safe_ref_issue(self) -> None:
        issue = linked_issue("issue-1", "ITT-160", "in_review", None)
        issue_by_id = {"issue-1": issue}
        issue_records = {"issue-1": review_followup.build_issue_record(issue, [])}

        plan = review_followup.build_merged_aftercare_plan(
            "https://github.com/ittae/ittae/pull/423",
            [{"id": "issue-1", "identifier": "ITT-160", "status": "in_review", "match_source": "pr-text-ref"}],
            issue_by_id,
            issue_records,
        )

        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["updates"][0]["reason"], "direct-leaf-pr-ref")
        self.assertEqual(plan["updates"][0]["identifier"], "ITT-160")

    def test_build_merged_aftercare_plan_keeps_non_leaf_safe_ref_issue_open(self) -> None:
        parent = linked_issue("issue-1", "ITT-131", "in_review", None)
        child = linked_issue("issue-2", "ITT-160", "todo", None, parent_issue_id="issue-1")
        issue_by_id = {issue["id"]: issue for issue in [parent, child]}
        issue_records = {
            issue_id: review_followup.build_issue_record(issue, [])
            for issue_id, issue in issue_by_id.items()
        }

        plan = review_followup.build_merged_aftercare_plan(
            "https://github.com/ittae/ittae/pull/423",
            [{"id": "issue-1", "identifier": "ITT-131", "status": "in_review", "match_source": "pr-text-ref"}],
            issue_by_id,
            issue_records,
        )

        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["updates"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "direct-safe-ref-non-leaf")

    def test_build_merged_aftercare_plan_blocks_multiple_issue_families(self) -> None:
        first = linked_issue("issue-1", "ITT-53", "in_review", "https://github.com/ittae/ittae/pull/423")
        second = linked_issue("issue-2", "ITT-160", "in_review", "https://github.com/ittae/ittae/pull/423")
        issue_by_id = {issue["id"]: issue for issue in [first, second]}
        issue_records = {
            issue_id: review_followup.build_issue_record(issue, [])
            for issue_id, issue in issue_by_id.items()
        }

        plan = review_followup.build_merged_aftercare_plan(
            "https://github.com/ittae/ittae/pull/423",
            [
                {"id": "issue-1", "identifier": "ITT-53", "status": "in_review", "match_source": "pr-url"},
                {"id": "issue-2", "identifier": "ITT-160", "status": "in_review", "match_source": "pr-url"},
            ],
            issue_by_id,
            issue_records,
        )

        self.assertEqual(plan["state"], "ambiguous")
        self.assertEqual(len(plan["family_root_ids"]), 2)

    @patch.object(review_followup, "run_merged_aftercare")
    @patch.object(review_followup, "resolve_pr_context")
    def test_main_merged_aftercare_json_dry_run(
        self,
        mock_resolve_pr_context,
        mock_run_merged_aftercare,
    ) -> None:
        mock_resolve_pr_context.return_value = {
            "matches": [
                {"id": "issue-1", "identifier": "ITT-53", "status": "done", "match_source": "pr-url"},
            ],
            "warnings": [],
            "safe_references": [],
            "triage_preview": None,
            "created_issue": None,
            "resolution_state": "linked",
            "pr_context": None,
        }
        mock_run_merged_aftercare.return_value = {
            "state": "dry-run",
            "plan": {
                "state": "ready",
                "matched_issue_ids": ["issue-1"],
                "top_level_match_ids": ["issue-1"],
                "updates": [],
                "skipped": [],
                "family_root_ids": ["issue-1"],
            },
            "updated_issues": [],
            "posted_comments": [],
            "comment_previews": [],
            "errors": [],
            "warnings": [],
        }

        exit_code, payload = self.run_main_json(
            [
                "review_followup.py",
                "--merged-aftercare-pr-url",
                "https://github.com/ittae/ittae/pull/423",
                "--pr-merged",
                "true",
                "--output",
                "json",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["resolution"]["state"], "linked")
        self.assertEqual(payload["resolution"]["next_action"], "merged aftercare completed")
        self.assertEqual(payload["aftercare"]["state"], "dry-run")

    @patch.object(review_followup, "resolve_pr_context")
    @patch.object(review_followup, "load_issue_records")
    @patch.object(review_followup, "list_issues_for_statuses")
    def test_main_pr_423_merged_aftercare_smoke_dry_run(
        self,
        mock_list_issues_for_statuses,
        mock_load_issue_records,
        mock_resolve_pr_context,
    ) -> None:
        pr_url = "https://github.com/ittae/ittae/pull/423"
        parent = linked_issue("issue-1", "ITT-53", "in_review", pr_url)
        child = linked_issue("issue-2", "ITT-141", "in_review", pr_url, parent_issue_id="issue-1")
        blocked_child = linked_issue("issue-3", "ITT-142", "blocked", pr_url, parent_issue_id="issue-1")
        issues = [parent, child, blocked_child]
        issue_records = {
            issue["id"]: review_followup.build_issue_record(issue, [])
            for issue in issues
        }

        mock_resolve_pr_context.return_value = {
            "matches": [
                {"id": "issue-1", "identifier": "ITT-53", "status": "in_review", "match_source": "pr-url"},
            ],
            "warnings": [],
            "safe_references": [],
            "triage_preview": None,
            "created_issue": None,
            "resolution_state": "linked",
            "pr_context": None,
        }
        mock_list_issues_for_statuses.return_value = (issues, [])
        mock_load_issue_records.return_value = (issue_records, [])

        exit_code, payload = self.run_main_json(
            [
                "review_followup.py",
                "--merged-aftercare-pr-url",
                pr_url,
                "--statuses",
                "done,in_review,in_progress,blocked,todo,backlog",
                "--pr-merged",
                "true",
                "--head-sha",
                "3d69830e1f3b30347b585d2d59c7bc26c3914b34",
                "--head-ref",
                "fix/ITT-141-pr-423-follow-up",
                "--base-ref",
                "main",
                "--merge-commit-sha",
                "99c92fb9c0ffee00000000000000000000000000",
                "--merged-at",
                "2026-05-21T00:00:00Z",
                "--merged-by",
                "ittae",
                "--output",
                "json",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["resolution"]["state"], "linked")
        self.assertEqual(payload["aftercare"]["state"], "dry-run")
        self.assertEqual(
            [(entry["identifier"], entry["reason"]) for entry in payload["aftercare"]["plan"]["updates"]],
            [("ITT-53", "direct-explicit-pr-link"), ("ITT-141", "child-explicit-pr-link")],
        )
        self.assertEqual(
            payload["aftercare"]["plan"]["skipped"][0]["reason"],
            "child-status-left-unchanged:blocked",
        )
        preview = payload["aftercare"]["comment_previews"][0]
        self.assertEqual(preview["identifier"], "ITT-53")
        self.assertIn("[hermes:pr-merged]", preview["content"])
        self.assertIn("ITT-53", preview["content"])
        self.assertIn("ITT-141", preview["content"])


if __name__ == "__main__":
    unittest.main()
