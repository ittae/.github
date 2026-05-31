#!/usr/bin/env python3
"""Tests for local_pr_review_dual_run (Phase 2 dry-run comparison)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
FIXTURE_DIR = os.path.join(TOOLS_DIR, "fixtures")
sys.path.insert(0, TOOLS_DIR)

import local_pr_review_dual_run as dual_run  # noqa: E402


ROSTER_PATH = os.path.join(FIXTURE_DIR, "roster_phase0.json")
FIXTURE_HIGH_RISK = os.path.join(FIXTURE_DIR, "synthetic_high_risk.json")
SNAPSHOT_HIGH_RISK = os.path.join(FIXTURE_DIR, "action_snapshot_high_risk.json")


class TestSnapshotNormalization(unittest.TestCase):
    def test_accepts_gh_pr_view_shape(self) -> None:
        snapshot = dual_run.action_snapshot_from_payload(
            {
                "labels": [{"name": "high-risk"}, {"name": "high-risk"}, "custom"],
                "comments": [
                    {
                        "body": "body",
                        "author": {"login": "github-actions[bot]"},
                        "createdAt": "2026-05-31T00:00:00Z",
                        "url": "u",
                    }
                ],
            },
            source="fixture",
        )
        self.assertEqual(snapshot.labels, ("custom", "high-risk"))
        self.assertEqual(snapshot.comments[0]["author"], "github-actions[bot]")

    def test_rejects_non_object_snapshot(self) -> None:
        with self.assertRaises(dual_run.DualRunError):
            dual_run.action_snapshot_from_payload([], source="fixture")  # type: ignore[arg-type]


class TestCompareGateToAction(unittest.TestCase):
    def test_match_for_high_risk_labels_and_comment(self) -> None:
        report = dual_run.run_dual_run(
            pr_url="https://github.com/ittae/ittae/pull/1",
            pr_size_limit=1500,
            high_risk_paths_regex=dual_run.dispatcher.DEFAULT_HIGH_RISK_PATHS,
            model=dual_run.dispatcher.DEFAULT_MODEL,
            roster_path=ROSTER_PATH,
            fixture_path=FIXTURE_HIGH_RISK,
            action_snapshot_fixture=SNAPSHOT_HIGH_RISK,
        )
        self.assertEqual(report["comparison"]["status"], "match")
        self.assertFalse(any(item["executed"] for item in report["local_gate"]["dry_run_actions"]))
        self.assertEqual(report["safety"]["mutations_enabled"], False)

    def test_no_expected_gate_action_when_clean_pr_has_no_gate_labels(self) -> None:
        snapshot = dual_run.ActionSnapshot(
            labels=(),
            comments=(),
            source="test",
        )
        classify = {"would_apply_labels": [], "would_post_comments": []}
        comparison = dual_run.compare_gate_to_action(classify, snapshot)
        self.assertEqual(comparison["status"], "no_expected_gate_action")

    def test_needs_human_review_alone_is_not_gate_footprint(self) -> None:
        snapshot = dual_run.ActionSnapshot(
            labels=("needs-human-review",),
            comments=(),
            source="test",
        )
        classify = {"would_apply_labels": [], "would_post_comments": []}
        comparison = dual_run.compare_gate_to_action(classify, snapshot)
        self.assertEqual(comparison["status"], "no_expected_gate_action")
        self.assertEqual(comparison["actual_gate_labels"], [])
        self.assertEqual(comparison["unexpected_gate_labels"], [])

    def test_needs_human_review_counts_with_high_risk_provenance(self) -> None:
        snapshot = dual_run.ActionSnapshot(
            labels=("high-risk", "needs-human-review"),
            comments=(),
            source="test",
        )
        classify = {"would_apply_labels": [], "would_post_comments": []}
        comparison = dual_run.compare_gate_to_action(classify, snapshot)
        self.assertEqual(comparison["status"], "mismatch")
        self.assertEqual(
            comparison["unexpected_gate_labels"],
            ["high-risk", "needs-human-review"],
        )

    def test_detects_missing_expected_label(self) -> None:
        snapshot = dual_run.ActionSnapshot(labels=("high-risk",), comments=(), source="test")
        classify = {
            "would_apply_labels": ["high-risk", "needs-human-review"],
            "would_post_comments": [],
        }
        comparison = dual_run.compare_gate_to_action(classify, snapshot)
        self.assertEqual(comparison["status"], "mismatch")
        self.assertEqual(comparison["missing_labels"], ["needs-human-review"])

    def test_detects_unexpected_gate_label(self) -> None:
        snapshot = dual_run.ActionSnapshot(labels=("too-large",), comments=(), source="test")
        classify = {"would_apply_labels": [], "would_post_comments": []}
        comparison = dual_run.compare_gate_to_action(classify, snapshot)
        self.assertEqual(comparison["status"], "mismatch")
        self.assertEqual(comparison["unexpected_gate_labels"], ["too-large"])

    def test_comment_body_match_normalizes_crlf_to_lf(self) -> None:
        snapshot = dual_run.ActionSnapshot(
            labels=(),
            comments=({"body": "line 1\r\nline 2\r\nline 3"},),
            source="test",
        )
        classify = {
            "would_apply_labels": [],
            "would_post_comments": [{"target": "pr", "body": "line 1\nline 2\nline 3"}],
        }
        comparison = dual_run.compare_gate_to_action(classify, snapshot)
        self.assertEqual(comparison["status"], "match")
        self.assertEqual(comparison["missing_comment_indexes"], [])


class TestMultiReviewerDebateGate(unittest.TestCase):
    def test_report_contains_multi_reviewer_debate_schema(self) -> None:
        report = dual_run.run_dual_run(
            pr_url="https://github.com/ittae/ittae/pull/1",
            pr_size_limit=1500,
            high_risk_paths_regex=dual_run.dispatcher.DEFAULT_HIGH_RISK_PATHS,
            model=dual_run.dispatcher.DEFAULT_MODEL,
            roster_path=ROSTER_PATH,
            fixture_path=FIXTURE_HIGH_RISK,
            action_snapshot_fixture=SNAPSHOT_HIGH_RISK,
        )
        gate = report["multi_reviewer_debate_gate"]
        reviewers = [item["reviewer"] for item in gate["roster"]["reviewers"]]
        self.assertEqual(
            reviewers,
            ["Claude", "Codex", "Gemini", "Copilot", "local-hermes"],
        )
        self.assertEqual(gate["mode"], "dry-run-schema-only")
        self.assertEqual(
            gate["round_1_independent_findings"][0]["finding_required_fields"],
            ["reviewer", "file", "line", "evidence", "severity", "message"],
        )
        self.assertEqual(
            gate["round_2_counter_review"][0]["counter_review_required_fields"],
            [
                "reviewer",
                "target_reviewer",
                "target_finding_id",
                "position",
                "file",
                "line",
                "evidence",
            ],
        )
        self.assertEqual(
            sorted(gate["hermes_final_arbitration"]["outcome_buckets"]),
            [
                "false-positive",
                "hold",
                "must-fix",
                "nice-to-have",
                "safe-to-merge",
            ],
        )

    def test_quality_metrics_are_present_for_each_reviewer(self) -> None:
        report = dual_run.run_dual_run(
            pr_url="https://github.com/ittae/ittae/pull/1",
            pr_size_limit=1500,
            high_risk_paths_regex=dual_run.dispatcher.DEFAULT_HIGH_RISK_PATHS,
            model=dual_run.dispatcher.DEFAULT_MODEL,
            roster_path=ROSTER_PATH,
            fixture_path=FIXTURE_HIGH_RISK,
            action_snapshot_fixture=SNAPSHOT_HIGH_RISK,
        )
        metrics = report["multi_reviewer_debate_gate"]["quality_metrics"]
        self.assertEqual(len(metrics), 5)
        for item in metrics:
            self.assertIn("reviewer", item)
            self.assertEqual(item["finding_count"], 0)
            self.assertEqual(item["accepted_count"], 0)
            self.assertIsNone(item["false_positive_count"])
            self.assertEqual(item["status"], "unknown")
            self.assertIn("elapsed_ms", item["extensions"])
            self.assertIn("cost", item["extensions"])
            self.assertIn("local_resource", item["extensions"])


class TestCli(unittest.TestCase):
    def test_cli_requires_dry_run(self) -> None:
        with self.assertRaises(SystemExit):
            dual_run.main(["--pr-url", "https://github.com/ittae/ittae/pull/1"])

    def test_markdown_output_smoke(self) -> None:
        report = dual_run.run_dual_run(
            pr_url="https://github.com/ittae/ittae/pull/1",
            pr_size_limit=1500,
            high_risk_paths_regex=dual_run.dispatcher.DEFAULT_HIGH_RISK_PATHS,
            model=dual_run.dispatcher.DEFAULT_MODEL,
            roster_path=ROSTER_PATH,
            fixture_path=FIXTURE_HIGH_RISK,
            action_snapshot_fixture=SNAPSHOT_HIGH_RISK,
        )
        text = dual_run.render_markdown_report(report)
        self.assertIn("Dual-Run Report", text)
        self.assertIn("mutations: `disabled`", text)
        self.assertIn("Multi-Reviewer Debate Gate", text)
        self.assertIn("Claude, Codex, Gemini, Copilot, local-hermes", text)

    def test_live_snapshot_uses_read_only_gh_pr_view(self) -> None:
        payload = {"labels": [{"name": "high-risk"}], "comments": []}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            self.assertEqual(cmd[:4], ["gh", "pr", "view", "7"])
            self.assertIn("labels,comments", cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        with mock.patch.object(dual_run.subprocess, "run", fake_run):
            ref = dual_run.dispatcher.parse_pr_url("https://github.com/ittae/ittae/pull/7")
            snapshot = dual_run.load_action_snapshot(ref, None)
        self.assertEqual(snapshot.labels, ("high-risk",))


if __name__ == "__main__":
    unittest.main()
