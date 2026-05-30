#!/usr/bin/env python3
"""Tests for local_pr_review_dispatcher (Phase 0).

Runs without any external service. The closed PR (ittae/ittae#425) fixture
captures the live `gh pr view --json` shape so we replay it as a fixture
in `proceed / not-high-risk` mode; synthetic fixtures cover the other two
gate-classify branches.

Run from .github repo root:

    python3 -m pytest tools/tests/test_local_pr_review_dispatcher.py -q

or, without pytest:

    python3 tools/tests/test_local_pr_review_dispatcher.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

# Make the dispatcher importable when running this file directly.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
FIXTURE_DIR = os.path.join(TOOLS_DIR, "fixtures")
sys.path.insert(0, TOOLS_DIR)

import local_pr_review_dispatcher as dispatcher  # noqa: E402


ROSTER_PATH = os.path.join(FIXTURE_DIR, "roster_phase0.json")
FIXTURE_PR_425 = os.path.join(FIXTURE_DIR, "ittae_ittae_pr_425.json")
FIXTURE_HIGH_RISK = os.path.join(FIXTURE_DIR, "synthetic_high_risk.json")
FIXTURE_TOO_LARGE = os.path.join(FIXTURE_DIR, "synthetic_too_large.json")


class TestParsePrUrl(unittest.TestCase):
    def test_accepts_canonical_pr_url(self) -> None:
        ref = dispatcher.parse_pr_url("https://github.com/ittae/ittae/pull/425")
        self.assertEqual(ref.repo, "ittae/ittae")
        self.assertEqual(ref.number, 425)
        self.assertEqual(ref.url, "https://github.com/ittae/ittae/pull/425")

    def test_accepts_trailing_slash(self) -> None:
        ref = dispatcher.parse_pr_url("https://github.com/ittae/ittae/pull/425/")
        self.assertEqual(ref.number, 425)

    def test_accepts_www_host(self) -> None:
        ref = dispatcher.parse_pr_url("https://www.github.com/ittae/ittae/pull/7")
        self.assertEqual(ref.repo, "ittae/ittae")
        self.assertEqual(ref.number, 7)

    def test_rejects_non_github_host(self) -> None:
        with self.assertRaises(dispatcher.DispatcherError):
            dispatcher.parse_pr_url("https://gitlab.com/ittae/ittae/pull/1")

    def test_rejects_issue_url(self) -> None:
        with self.assertRaises(dispatcher.DispatcherError):
            dispatcher.parse_pr_url("https://github.com/ittae/ittae/issues/425")

    def test_rejects_relative_path(self) -> None:
        with self.assertRaises(dispatcher.DispatcherError):
            dispatcher.parse_pr_url("ittae/ittae/pull/425")


class TestFixtureLoading(unittest.TestCase):
    def test_loads_pr_425_fixture(self) -> None:
        ref = dispatcher.parse_pr_url("https://github.com/ittae/ittae/pull/425")
        metadata = dispatcher.load_pr_metadata_from_fixture(ref, FIXTURE_PR_425)
        self.assertEqual(metadata.ref.repo, "ittae/ittae")
        self.assertEqual(metadata.additions, 4)
        self.assertEqual(metadata.deletions, 96)
        self.assertEqual(metadata.total_changes, 100)
        self.assertEqual(metadata.head_ref, "chore/ITT-180-use-org-pr-template-ittae")
        self.assertEqual(metadata.base_ref, "main")
        self.assertIn(".github/pull_request_template.md", metadata.files)
        self.assertEqual(metadata.author, "get6")

    def test_missing_fixture_raises(self) -> None:
        ref = dispatcher.parse_pr_url("https://github.com/ittae/ittae/pull/1")
        with self.assertRaises(dispatcher.DispatcherError):
            dispatcher.load_pr_metadata_from_fixture(
                ref, os.path.join(FIXTURE_DIR, "does_not_exist.json")
            )

    def test_non_object_fixture_rejected(self) -> None:
        ref = dispatcher.parse_pr_url("https://github.com/ittae/ittae/pull/1")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(["not", "an", "object"], handle)
            path = handle.name
        try:
            with self.assertRaises(dispatcher.DispatcherError):
                dispatcher.load_pr_metadata_from_fixture(ref, path)
        finally:
            os.unlink(path)

    def test_nullable_fields_become_empty_string(self) -> None:
        ref = dispatcher.PRRef(repo="ittae/ittae", number=1, url="u")
        payload = {
            "title": None,
            "headRefOid": None,
            "headRefName": None,
            "baseRefName": None,
            "state": None,
            "author": None,
            "additions": None,
            "deletions": None,
        }
        meta = dispatcher._pr_metadata_from_payload(ref, payload)
        self.assertEqual(meta.title, "")
        self.assertEqual(meta.head_sha, "")
        self.assertEqual(meta.head_ref, "")
        self.assertEqual(meta.base_ref, "")
        self.assertEqual(meta.state, "")
        self.assertEqual(meta.author, "")
        self.assertEqual(meta.additions, 0)
        self.assertEqual(meta.deletions, 0)
        # Critically: never the literal string "None".
        self.assertNotIn("None", [meta.title, meta.head_sha, meta.state])


class TestClassifyPr(unittest.TestCase):
    def _meta(
        self,
        *,
        additions: int = 0,
        deletions: int = 0,
        files: list[str] | None = None,
    ) -> dispatcher.PRMetadata:
        ref = dispatcher.PRRef(repo="ittae/ittae", number=1, url="https://example.invalid")
        return dispatcher.PRMetadata(
            ref=ref,
            title="t",
            head_sha="sha",
            head_ref="head",
            base_ref="main",
            additions=additions,
            deletions=deletions,
            files=files or [],
            author="x",
            state="OPEN",
        )

    def test_proceed_when_under_size_limit_and_no_risky_paths(self) -> None:
        meta = self._meta(additions=4, deletions=96, files=["docs/x.md"])
        result = dispatcher.classify_pr(meta)
        self.assertEqual(result.verdict, "proceed")
        self.assertFalse(result.is_high_risk)
        self.assertEqual(result.matched_high_risk_files, [])
        self.assertEqual(result.would_apply_labels, [])
        self.assertEqual(result.would_post_comments, [])

    def test_too_large_emits_label_and_comment_and_skips_risk_scan(self) -> None:
        meta = self._meta(
            additions=1800,
            deletions=200,
            # Even though this path matches high-risk, too-large short-circuits.
            files=["lib/features/auth/x.dart"],
        )
        result = dispatcher.classify_pr(meta)
        self.assertEqual(result.verdict, "too-large")
        self.assertFalse(result.is_high_risk)
        self.assertEqual(result.matched_high_risk_files, [])
        self.assertEqual(result.would_apply_labels, ["too-large"])
        self.assertEqual(len(result.would_post_comments), 1)
        body = result.would_post_comments[0]["body"]
        self.assertIn("PR 크기", body)
        self.assertIn("2000줄", body)
        self.assertIn("1500줄", body)

    def test_high_risk_path_matches(self) -> None:
        meta = self._meta(
            additions=10,
            deletions=5,
            files=[
                "lib/features/auth/data/repositories/auth_repository_impl.dart",
                "pubspec.yaml",
                "docs/x.md",
            ],
        )
        result = dispatcher.classify_pr(meta)
        self.assertEqual(result.verdict, "proceed")
        self.assertTrue(result.is_high_risk)
        self.assertIn(
            "lib/features/auth/data/repositories/auth_repository_impl.dart",
            result.matched_high_risk_files,
        )
        self.assertIn("pubspec.yaml", result.matched_high_risk_files)
        self.assertNotIn("docs/x.md", result.matched_high_risk_files)
        self.assertEqual(
            result.would_apply_labels,
            ["high-risk", "needs-human-review"],
        )
        self.assertEqual(len(result.would_post_comments), 1)
        self.assertIn(
            "민감 영역 변경 감지",
            result.would_post_comments[0]["body"],
        )

    def test_invalid_regex_raises(self) -> None:
        meta = self._meta()
        with self.assertRaises(dispatcher.DispatcherError):
            dispatcher.classify_pr(meta, high_risk_paths_regex="(unbalanced")

    def test_size_limit_boundary_is_inclusive(self) -> None:
        # Workflow uses `-gt`, so total == limit is allowed.
        meta = self._meta(additions=500, deletions=500, files=["docs/x.md"])
        result = dispatcher.classify_pr(meta, pr_size_limit=1000)
        self.assertEqual(result.verdict, "proceed")


class TestBuildDispatchPayload(unittest.TestCase):
    def _meta(self, **overrides) -> dispatcher.PRMetadata:
        ref = dispatcher.PRRef(repo="ittae/ittae", number=425, url="u")
        base = {
            "ref": ref,
            "title": "t",
            "head_sha": "abc123",
            "head_ref": "feat/x",
            "base_ref": "main",
            "additions": 4,
            "deletions": 96,
            "files": [],
            "author": "get6",
            "state": "OPEN",
        }
        base.update(overrides)
        return dispatcher.PRMetadata(**base)

    def test_proceed_dispatch_includes_all_active_agents(self) -> None:
        meta = self._meta()
        classify = dispatcher.classify_pr(meta)
        roster = dispatcher.load_reviewer_roster(ROSTER_PATH)
        payload = dispatcher.build_dispatch_payload(meta, classify, roster)
        self.assertEqual(payload["decision"], "dispatch")
        self.assertEqual(payload["reason"], "standard")
        names = [agent["name"] for agent in payload["agents"]]
        self.assertEqual(set(names), {"claude-code", "codex", "gemini", "copilot"})
        claude_entry = next(a for a in payload["agents"] if a["name"] == "claude-code")
        self.assertEqual(claude_entry["model"], dispatcher.DEFAULT_MODEL)
        self.assertEqual(len(claude_entry["perspectives"]), 5)
        self.assertEqual(payload["agent_input"]["pr_number"], 425)
        self.assertEqual(payload["agent_input"]["head_sha"], "abc123")
        self.assertEqual(payload["ai_review_meta_template"]["head_sha"], "abc123")

    def test_too_large_payload_skips_dispatch(self) -> None:
        meta = self._meta(additions=1800, deletions=200)
        classify = dispatcher.classify_pr(meta)
        roster = dispatcher.load_reviewer_roster(ROSTER_PATH)
        payload = dispatcher.build_dispatch_payload(meta, classify, roster)
        self.assertEqual(payload["decision"], "skip")
        self.assertEqual(payload["reason"], "pr-too-large")

    def test_high_risk_payload_carries_reason(self) -> None:
        meta = self._meta(files=["pubspec.yaml"], additions=4, deletions=2)
        classify = dispatcher.classify_pr(meta)
        roster = dispatcher.load_reviewer_roster(ROSTER_PATH)
        payload = dispatcher.build_dispatch_payload(meta, classify, roster)
        self.assertEqual(payload["decision"], "dispatch")
        self.assertEqual(payload["reason"], "high-risk-needs-human-merge")
        self.assertTrue(payload["agent_input"]["is_high_risk"])
        self.assertIn("pubspec.yaml", payload["agent_input"]["high_risk_files"])

    def test_inactive_reviewer_is_excluded(self) -> None:
        meta = self._meta()
        classify = dispatcher.classify_pr(meta)
        roster = {
            "reviewers": {
                "claude-code": {
                    "display_name": "Claude",
                    "role": "required",
                    "availability": "active",
                    "agent_ids": ["x"],
                },
                "retired-bot": {
                    "display_name": "Retired",
                    "role": "supplementary",
                    "availability": "retired",
                    "agent_ids": ["y"],
                },
            }
        }
        payload = dispatcher.build_dispatch_payload(meta, classify, roster)
        names = [agent["name"] for agent in payload["agents"]]
        self.assertIn("claude-code", names)
        self.assertNotIn("retired-bot", names)


class TestReviewerRosterLoading(unittest.TestCase):
    def test_non_object_roster_rejected(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(["reviewers"], handle)
            path = handle.name
        try:
            with self.assertRaises(dispatcher.DispatcherError):
                dispatcher.load_reviewer_roster(path)
        finally:
            os.unlink(path)


class TestLiveMetadataAugmentation(unittest.TestCase):
    """Live mode must source the changed-file list from `gh pr diff
    --name-only`, not the 100-file-capped `gh pr view --json files`."""

    def test_files_sourced_from_diff_name_only(self) -> None:
        ref = dispatcher.parse_pr_url("https://github.com/ittae/ittae/pull/9")
        # `gh pr view --json` returns a TRUNCATED file list (missing the
        # high-risk pubspec.yaml); `gh pr diff --name-only` returns the
        # complete list including it.
        view_payload = {
            "title": "big PR",
            "headRefOid": "deadbeef",
            "headRefName": "feat/big",
            "baseRefName": "main",
            "additions": 50,
            "deletions": 10,
            "state": "OPEN",
            "author": {"login": "someone"},
            "files": [{"path": "lib/a.dart"}],  # truncated
        }
        diff_output = "lib/a.dart\nlib/b.dart\npubspec.yaml\n"

        def fake_run(args, **kwargs):  # noqa: ANN001
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            if "view" in args:
                stdout = json.dumps(view_payload)
            elif "diff" in args:
                self.assertIn("--name-only", args)
                stdout = diff_output
            else:
                raise AssertionError(f"unexpected gh args: {args}")
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
            meta = dispatcher.load_pr_metadata_via_gh(ref)

        self.assertEqual(meta.files, ["lib/a.dart", "lib/b.dart", "pubspec.yaml"])
        # The high-risk file only present in the diff list must be detected.
        classify = dispatcher.classify_pr(meta)
        self.assertTrue(classify.is_high_risk)
        self.assertIn("pubspec.yaml", classify.matched_high_risk_files)


class TestRunDispatchEndToEnd(unittest.TestCase):
    def test_replay_pr_425_returns_proceed(self) -> None:
        result = dispatcher.run_dispatch(
            pr_url="https://github.com/ittae/ittae/pull/425",
            pr_size_limit=dispatcher.DEFAULT_PR_SIZE_LIMIT,
            high_risk_paths_regex=dispatcher.DEFAULT_HIGH_RISK_PATHS,
            model=dispatcher.DEFAULT_MODEL,
            roster_path=ROSTER_PATH,
            fixture_path=FIXTURE_PR_425,
        )
        self.assertEqual(result["version"], dispatcher.VERSION)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["pr"]["repo"], "ittae/ittae")
        self.assertEqual(result["pr"]["number"], 425)
        self.assertEqual(result["pr"]["total"], 100)
        self.assertEqual(result["classify"]["verdict"], "proceed")
        self.assertFalse(result["classify"]["is_high_risk"])
        self.assertEqual(result["classify"]["would_apply_labels"], [])
        self.assertEqual(result["dispatch"]["decision"], "dispatch")
        self.assertEqual(result["dispatch"]["reason"], "standard")

    def test_replay_high_risk_fixture(self) -> None:
        result = dispatcher.run_dispatch(
            pr_url="https://github.com/ittae/ittae/pull/1",
            pr_size_limit=dispatcher.DEFAULT_PR_SIZE_LIMIT,
            high_risk_paths_regex=dispatcher.DEFAULT_HIGH_RISK_PATHS,
            model=dispatcher.DEFAULT_MODEL,
            roster_path=ROSTER_PATH,
            fixture_path=FIXTURE_HIGH_RISK,
        )
        self.assertEqual(result["classify"]["verdict"], "proceed")
        self.assertTrue(result["classify"]["is_high_risk"])
        self.assertEqual(
            result["classify"]["would_apply_labels"],
            ["high-risk", "needs-human-review"],
        )
        self.assertEqual(result["dispatch"]["reason"], "high-risk-needs-human-merge")

    def test_replay_too_large_fixture(self) -> None:
        result = dispatcher.run_dispatch(
            pr_url="https://github.com/ittae/ittae/pull/2",
            pr_size_limit=dispatcher.DEFAULT_PR_SIZE_LIMIT,
            high_risk_paths_regex=dispatcher.DEFAULT_HIGH_RISK_PATHS,
            model=dispatcher.DEFAULT_MODEL,
            roster_path=ROSTER_PATH,
            fixture_path=FIXTURE_TOO_LARGE,
        )
        self.assertEqual(result["classify"]["verdict"], "too-large")
        self.assertEqual(result["classify"]["would_apply_labels"], ["too-large"])
        self.assertEqual(result["dispatch"]["decision"], "skip")


class TestCli(unittest.TestCase):
    def _invoke(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatcher.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_dry_run_with_fixture(self) -> None:
        code, stdout, _ = self._invoke(
            [
                "--pr-url",
                "https://github.com/ittae/ittae/pull/425",
                "--dry-run",
                "--fixture",
                FIXTURE_PR_425,
                "--roster",
                ROSTER_PATH,
            ]
        )
        self.assertEqual(code, 0)
        parsed = json.loads(stdout)
        self.assertEqual(parsed["mode"], "dry-run")
        self.assertEqual(parsed["pr"]["number"], 425)

    def test_cli_missing_dry_run_exits_nonzero(self) -> None:
        # argparse `required=True` exits with code 2 to stderr before main()
        # gets a chance to run. SystemExit is expected.
        with self.assertRaises(SystemExit) as cm:
            self._invoke(
                [
                    "--pr-url",
                    "https://github.com/ittae/ittae/pull/425",
                    "--fixture",
                    FIXTURE_PR_425,
                    "--roster",
                    ROSTER_PATH,
                ]
            )
        self.assertNotEqual(cm.exception.code, 0)

    def test_cli_invalid_pr_url_exits_one(self) -> None:
        code, _, stderr = self._invoke(
            [
                "--pr-url",
                "https://example.com/foo",
                "--dry-run",
                "--fixture",
                FIXTURE_PR_425,
                "--roster",
                ROSTER_PATH,
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("ERROR", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
