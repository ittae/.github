#!/usr/bin/env python3
"""Tests for pr_review_gate_actions (Phase 1 mutation adapter).

Runs without any external service. Verifies:
    - classify → action plan translation is correct and ordered
    - DryRunExecutor records the exact would-run command but never mutates
    - GhCliExecutor is disabled-by-default (raises) and only mutates when
      explicitly enabled, building the correct gh argv
    - gh command building mirrors claude-code-review.yml's gate-classify

Run from .github repo root:

    python3 -m pytest tools/tests/test_pr_review_gate_actions.py -q

or, without pytest:

    python3 tools/tests/test_pr_review_gate_actions.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
sys.path.insert(0, TOOLS_DIR)

import pr_review_gate_actions as actions  # noqa: E402


def _pr() -> actions.PRTarget:
    return actions.PRTarget(repo="ittae/ittae", number=425)


class TestPRTarget(unittest.TestCase):
    def test_accepts_valid_two_part_slug(self) -> None:
        target = actions.PRTarget(repo="ittae/ittae", number=1)
        self.assertEqual(target.repo, "ittae/ittae")

    def test_rejects_bad_repo(self) -> None:
        with self.assertRaises(actions.GateActionError):
            actions.PRTarget(repo="not-a-repo", number=1)

    def test_rejects_partial_two_part_slugs(self) -> None:
        # owner/, /name, owner/name/extra, and empty must all be rejected.
        for bad in ("owner/", "/name", "owner/name/extra", "", "/", "a/b/c"):
            with self.subTest(repo=bad):
                with self.assertRaises(actions.GateActionError):
                    actions.PRTarget(repo=bad, number=1)

    def test_rejects_nonpositive_number(self) -> None:
        with self.assertRaises(actions.GateActionError):
            actions.PRTarget(repo="ittae/ittae", number=0)


class TestBuildGhCommand(unittest.TestCase):
    def test_add_labels(self) -> None:
        action = actions.PlannedAction(
            kind=actions.ADD_LABELS, target=_pr(), labels=("high-risk", "needs-human-review")
        )
        self.assertEqual(
            actions.build_gh_command(action),
            [
                "gh", "pr", "edit", "425", "-R", "ittae/ittae",
                "--add-label", "high-risk",
                "--add-label", "needs-human-review",
            ],
        )

    def test_remove_labels(self) -> None:
        action = actions.PlannedAction(
            kind=actions.REMOVE_LABELS, target=_pr(), labels=("ai-iterating",)
        )
        self.assertEqual(
            actions.build_gh_command(action),
            ["gh", "pr", "edit", "425", "-R", "ittae/ittae", "--remove-label", "ai-iterating"],
        )

    def test_post_comment(self) -> None:
        action = actions.PlannedAction(
            kind=actions.POST_COMMENT, target=_pr(), body="민감 영역 변경 감지"
        )
        self.assertEqual(
            actions.build_gh_command(action),
            ["gh", "pr", "comment", "425", "-R", "ittae/ittae", "--body", "민감 영역 변경 감지"],
        )

    def test_add_labels_requires_labels(self) -> None:
        action = actions.PlannedAction(kind=actions.ADD_LABELS, target=_pr())
        with self.assertRaises(actions.GateActionError):
            actions.build_gh_command(action)

    def test_comment_requires_body(self) -> None:
        action = actions.PlannedAction(kind=actions.POST_COMMENT, target=_pr())
        with self.assertRaises(actions.GateActionError):
            actions.build_gh_command(action)

    def test_unknown_kind_raises(self) -> None:
        action = actions.PlannedAction(kind="frobnicate", target=_pr())
        with self.assertRaises(actions.GateActionError):
            actions.build_gh_command(action)

    def test_set_check_status_uses_checks_api(self) -> None:
        action = actions.PlannedAction(
            kind=actions.SET_CHECK_STATUS,
            target=_pr(),
            check_name="hermes/pr-review-gate",
            head_sha="deadbeef",
            check_conclusion="success",
            check_summary="all reviewers passed",
        )
        self.assertEqual(
            actions.build_gh_command(action),
            [
                "gh", "api", "-X", "POST", "repos/ittae/ittae/check-runs",
                "-f", "name=hermes/pr-review-gate",
                "-f", "head_sha=deadbeef",
                "-f", "status=completed",
                "-f", "conclusion=success",
                "-f", "output[title]=hermes/pr-review-gate",
                "-f", "output[summary]=all reviewers passed",
            ],
        )

    def test_set_check_status_includes_details_url_when_present(self) -> None:
        action = actions.PlannedAction(
            kind=actions.SET_CHECK_STATUS,
            target=_pr(),
            head_sha="abc123",
            check_conclusion="failure",
            details_url="https://example.invalid/run/1",
        )
        cmd = actions.build_gh_command(action)
        self.assertIn("details_url=https://example.invalid/run/1", cmd)
        # No PR comment fallback — must be the Checks API path.
        self.assertEqual(cmd[:5], ["gh", "api", "-X", "POST", "repos/ittae/ittae/check-runs"])

    def test_set_check_status_requires_head_sha(self) -> None:
        action = actions.PlannedAction(
            kind=actions.SET_CHECK_STATUS,
            target=_pr(),
            check_conclusion="success",
        )
        with self.assertRaises(actions.GateActionError):
            actions.build_gh_command(action)

    def test_set_check_status_requires_conclusion(self) -> None:
        action = actions.PlannedAction(
            kind=actions.SET_CHECK_STATUS,
            target=_pr(),
            head_sha="abc123",
        )
        with self.assertRaises(actions.GateActionError):
            actions.build_gh_command(action)


class TestPlanActionsFromClassify(unittest.TestCase):
    def test_proceed_no_actions(self) -> None:
        classify = {"would_apply_labels": [], "would_post_comments": []}
        self.assertEqual(actions.plan_actions_from_classify(classify, _pr()), [])

    def test_high_risk_labels_then_comment_order(self) -> None:
        classify = {
            "would_apply_labels": ["high-risk", "needs-human-review"],
            "would_post_comments": [{"target": "pr", "body": "민감 영역 변경 감지"}],
        }
        plan = actions.plan_actions_from_classify(classify, _pr())
        self.assertEqual(len(plan), 2)
        # Labels first, then comment — matches the live workflow ordering.
        self.assertEqual(plan[0].kind, actions.ADD_LABELS)
        self.assertEqual(plan[0].labels, ("high-risk", "needs-human-review"))
        self.assertEqual(plan[1].kind, actions.POST_COMMENT)
        self.assertIn("민감 영역", plan[1].body)

    def test_too_large_single_label_and_comment(self) -> None:
        classify = {
            "would_apply_labels": ["too-large"],
            "would_post_comments": [{"target": "pr", "body": "PR 크기 2000줄"}],
        }
        plan = actions.plan_actions_from_classify(classify, _pr())
        self.assertEqual([a.kind for a in plan], [actions.ADD_LABELS, actions.POST_COMMENT])

    def test_skips_empty_comment_body(self) -> None:
        classify = {
            "would_apply_labels": [],
            "would_post_comments": [{"target": "pr", "body": ""}],
        }
        self.assertEqual(actions.plan_actions_from_classify(classify, _pr()), [])

    def test_skips_whitespace_only_comment_body(self) -> None:
        classify = {
            "would_apply_labels": [],
            "would_post_comments": [{"target": "pr", "body": "   \n\t "}],
        }
        self.assertEqual(actions.plan_actions_from_classify(classify, _pr()), [])

    def test_filters_none_and_blank_labels(self) -> None:
        classify = {
            "would_apply_labels": [None, "", "  ", "high-risk", "  needs-human-review  "],
            "would_post_comments": [],
        }
        plan = actions.plan_actions_from_classify(classify, _pr())
        self.assertEqual(len(plan), 1)
        # None/blank dropped; surviving labels stripped. No "None" label.
        self.assertEqual(plan[0].labels, ("high-risk", "needs-human-review"))
        self.assertNotIn("None", plan[0].labels)

    def test_all_blank_labels_produce_no_label_action(self) -> None:
        classify = {
            "would_apply_labels": [None, "", "   "],
            "would_post_comments": [],
        }
        self.assertEqual(actions.plan_actions_from_classify(classify, _pr()), [])

    def test_rejects_non_dict_classify(self) -> None:
        with self.assertRaises(actions.GateActionError):
            actions.plan_actions_from_classify(["nope"], _pr())  # type: ignore[arg-type]

    def test_rejects_bad_labels_type(self) -> None:
        with self.assertRaises(actions.GateActionError):
            actions.plan_actions_from_classify(
                {"would_apply_labels": "high-risk"}, _pr()
            )


class TestDryRunExecutor(unittest.TestCase):
    def test_records_without_mutating(self) -> None:
        executor = actions.DryRunExecutor()
        self.assertFalse(executor.mutates)
        action = actions.PlannedAction(
            kind=actions.ADD_LABELS, target=_pr(), labels=("too-large",)
        )
        result = executor.execute(action)
        self.assertFalse(result.executed)
        self.assertEqual(
            result.command,
            ["gh", "pr", "edit", "425", "-R", "ittae/ittae", "--add-label", "too-large"],
        )
        self.assertEqual(len(executor.recorded), 1)

    def test_apply_actions_defaults_to_dry_run(self) -> None:
        classify = {
            "would_apply_labels": ["high-risk", "needs-human-review"],
            "would_post_comments": [{"target": "pr", "body": "민감 영역 변경 감지"}],
        }
        plan = actions.plan_actions_from_classify(classify, _pr())
        results = actions.apply_actions(plan)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r.executed for r in results))


class TestGhCliExecutorDisabledByDefault(unittest.TestCase):
    def test_disabled_executor_raises_and_does_not_run(self) -> None:
        ran: list[list[str]] = []

        def spy_runner(cmd, **kwargs):  # noqa: ANN001
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        executor = actions.GhCliExecutor(enable_mutations=False, runner=spy_runner)
        action = actions.PlannedAction(
            kind=actions.ADD_LABELS, target=_pr(), labels=("too-large",)
        )
        with self.assertRaises(actions.GateActionError) as cm:
            executor.execute(action)
        # The would-run command is surfaced for debugging...
        self.assertIn("gh pr edit 425", str(cm.exception))
        # ...but the runner was never invoked.
        self.assertEqual(ran, [])

    def test_enabled_executor_runs_correct_command(self) -> None:
        captured: list[list[str]] = []

        def fake_runner(cmd, **kwargs):  # noqa: ANN001
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        executor = actions.GhCliExecutor(enable_mutations=True, runner=fake_runner)
        action = actions.PlannedAction(
            kind=actions.POST_COMMENT, target=_pr(), body="hello"
        )
        result = executor.execute(action)
        self.assertTrue(result.executed)
        self.assertEqual(result.detail, "ok")
        self.assertEqual(
            captured,
            [["gh", "pr", "comment", "425", "-R", "ittae/ittae", "--body", "hello"]],
        )

    def test_enabled_executor_surfaces_gh_failure(self) -> None:
        def failing_runner(cmd, **kwargs):  # noqa: ANN001
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")

        executor = actions.GhCliExecutor(enable_mutations=True, runner=failing_runner)
        action = actions.PlannedAction(
            kind=actions.POST_COMMENT, target=_pr(), body="hi"
        )
        with self.assertRaises(actions.GateActionError) as cm:
            executor.execute(action)
        self.assertIn("boom", str(cm.exception))

    def test_default_construction_is_disabled(self) -> None:
        executor = actions.GhCliExecutor()
        self.assertFalse(executor.enable_mutations)


class TestSerialization(unittest.TestCase):
    def test_planned_action_to_dict_round_trips_through_json(self) -> None:
        import json

        plan = actions.plan_actions_from_classify(
            {
                "would_apply_labels": ["high-risk"],
                "would_post_comments": [{"target": "pr", "body": "x"}],
            },
            _pr(),
        )
        dumped = json.dumps([a.to_dict() for a in plan])
        loaded = json.loads(dumped)
        self.assertEqual(loaded[0]["kind"], actions.ADD_LABELS)
        self.assertEqual(loaded[0]["labels"], ["high-risk"])
        self.assertEqual(loaded[1]["kind"], actions.POST_COMMENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
