"""Tests for train/automerge.py: the decision rules, the cycle and the real `gh` commands."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "train"))

import automerge as train

REPO = "naindev-contrast3d/example"
SHA = "a" * 40


def pr(
    number=1,
    sha=SHA,
    labels=("automerge",),
    draft=False,
    base="main",
    mergeable="MERGEABLE",
):
    return {
        "number": number,
        "headRefOid": sha,
        "baseRefName": base,
        "isDraft": draft,
        "mergeable": mergeable,
        "labels": [{"name": name} for name in labels],
    }


class FakeGithub:
    """Records every call; answers from plain dictionaries."""

    def __init__(
        self,
        prs=None,
        gate_ok=True,
        sealed=True,
        behind=0,
        update_result="updated",
        fail_repos=(),
        merge_error=False,
    ):
        self.prs = prs or []
        self.gate_ok = gate_ok
        self.sealed_shas = sealed
        self.behind = behind
        self.update_result = update_result
        self.fail_repos = set(fail_repos)
        self.merge_error = merge_error
        self.calls = []

    def _answer(self, value, sha):
        return value(sha) if callable(value) else value

    def open_prs(self, repo, label):
        self.calls.append(("open_prs", repo))
        if repo in self.fail_repos:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")
        return self.prs.get(repo, []) if isinstance(self.prs, dict) else self.prs

    def gate_green(self, repo, sha, check_name):
        return self._answer(self.gate_ok, sha)

    def sealed(self, repo, sha):
        return self._answer(self.sealed_shas, sha)

    def behind_by(self, repo, base, sha):
        return self._answer(self.behind, sha)

    def merge(self, repo, number, sha, method):
        self.calls.append(("merge", repo, number, sha, method))
        if self.merge_error:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="not allowed")

    def update_branch(self, repo, number, sha):
        self.calls.append(("update", repo, number, sha))
        return self.update_result

    def flag_needs_human(self, repo, number, label, needs_label):
        self.calls.append(("flag", repo, number, label, needs_label))

    def mutations(self):
        return [c for c in self.calls if c[0] != "open_prs"]


def run(gh, repos=None, dry_run=False):
    logs = []
    settings = train.Settings()
    configs = repos or [train.RepoConfig(repo=REPO)]
    code = train.run_cycle(gh, configs, settings, dry_run=dry_run, log=logs.append)
    return code, logs


class DecisionRules(unittest.TestCase):
    def cfg(self, **kw):
        return train.RepoConfig(repo=REPO, **kw)

    def classify(self, pull, gh=None, **kw):
        return train.classify(
            pull, self.cfg(**kw), train.Settings(), gh or FakeGithub()
        )

    def test_a_sealed_green_up_to_date_pull_request_is_ready(self):
        self.assertEqual(self.classify(pr())[0], "ready")

    def test_missing_label_draft_and_other_base_are_skipped(self):
        self.assertEqual(self.classify(pr(labels=()))[0], "skip")
        self.assertEqual(self.classify(pr(draft=True))[0], "skip")
        self.assertEqual(self.classify(pr(base="develop"))[0], "skip")

    def test_unknown_mergeability_is_skipped_and_a_conflict_is_flagged(self):
        self.assertEqual(self.classify(pr(mergeable="UNKNOWN"))[0], "skip")
        self.assertEqual(self.classify(pr(mergeable="CONFLICTING"))[0], "conflict")

    def test_a_red_or_missing_gate_is_skipped(self):
        state, reason = self.classify(pr(), FakeGithub(gate_ok=False))
        self.assertEqual(state, "skip")
        self.assertIn("gate", reason)

    def test_the_seal_is_required_only_when_configured(self):
        no_seal = FakeGithub(sealed=False)
        self.assertEqual(self.classify(pr(), no_seal)[0], "ready")
        state, reason = self.classify(pr(), no_seal, require_seal=True)
        self.assertEqual(state, "skip")
        self.assertIn("seal", reason)

    def test_a_head_that_misses_main_is_stale(self):
        state, reason = self.classify(pr(), FakeGithub(behind=3))
        self.assertEqual(state, "stale")
        self.assertIn("3", reason)

    def test_a_red_stale_pull_request_is_not_stale_but_skipped(self):
        self.assertEqual(
            self.classify(pr(), FakeGithub(behind=3, gate_ok=False))[0], "skip"
        )


class Cycle(unittest.TestCase):
    def test_a_ready_pull_request_is_merged_at_its_exact_head(self):
        gh = FakeGithub(prs=[pr(7)])
        code, _ = run(gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.mutations(), [("merge", REPO, 7, SHA, "merge")])

    def test_the_merge_method_of_the_repository_is_used(self):
        gh = FakeGithub(prs=[pr(7)])
        configs = [train.RepoConfig(repo=REPO, merge_method="squash")]
        run(gh, repos=configs)
        self.assertEqual(gh.mutations(), [("merge", REPO, 7, SHA, "squash")])

    def test_only_the_oldest_ready_pull_request_is_merged_per_cycle(self):
        gh = FakeGithub(prs=[pr(9, "b" * 40), pr(4, "c" * 40)])
        _, logs = run(gh)
        self.assertEqual(gh.mutations(), [("merge", REPO, 4, "c" * 40, "merge")])
        self.assertTrue(any("#9" in line and "waits" in line for line in logs))

    def test_a_stale_pull_request_gets_main_merged_in_and_is_not_merged(self):
        gh = FakeGithub(prs=[pr(3)], behind=2)
        run(gh)
        self.assertEqual(gh.mutations(), [("update", REPO, 3, SHA)])

    def test_a_ready_pull_request_is_merged_before_any_stale_one_is_updated(self):
        def behind(sha):
            return 0 if sha == "c" * 40 else 2

        gh = FakeGithub(prs=[pr(2, "b" * 40), pr(5, "c" * 40)], behind=behind)
        run(gh)
        self.assertEqual(gh.mutations(), [("merge", REPO, 5, "c" * 40, "merge")])

    def test_only_one_stale_pull_request_is_updated_per_cycle(self):
        gh = FakeGithub(prs=[pr(2, "b" * 40), pr(5, "c" * 40)], behind=2)
        run(gh)
        self.assertEqual(gh.mutations(), [("update", REPO, 2, "b" * 40)])

    def test_a_conflicting_pull_request_is_flagged_and_never_merged(self):
        gh = FakeGithub(prs=[pr(6, mergeable="CONFLICTING")])
        run(gh)
        self.assertEqual(
            gh.mutations(), [("flag", REPO, 6, "automerge", "needs-human")]
        )

    def test_a_conflict_found_while_updating_the_branch_is_flagged(self):
        gh = FakeGithub(prs=[pr(8)], behind=1, update_result="conflict")
        run(gh)
        self.assertEqual(
            gh.mutations(),
            [("update", REPO, 8, SHA), ("flag", REPO, 8, "automerge", "needs-human")],
        )

    def test_a_dry_run_changes_nothing(self):
        gh = FakeGithub(prs=[pr(1), pr(2, "b" * 40, mergeable="CONFLICTING")], behind=0)
        _, logs = run(gh, dry_run=True)
        self.assertEqual(gh.mutations(), [])
        self.assertTrue(any("dry run" in line for line in logs))

    def test_a_failing_repository_does_not_stop_the_others(self):
        gh = FakeGithub(prs={"o/working": [pr(1)]}, fail_repos={"o/broken"})
        configs = [
            train.RepoConfig(repo="o/broken"),
            train.RepoConfig(repo="o/working"),
        ]
        code, logs = run(gh, repos=configs)
        self.assertEqual(code, 1)
        self.assertEqual(gh.mutations(), [("merge", "o/working", 1, SHA, "merge")])
        self.assertTrue(any("o/broken" in line and "failed" in line for line in logs))

    def test_a_refused_merge_is_logged_and_reported(self):
        gh = FakeGithub(prs=[pr(1)], merge_error=True)
        code, logs = run(gh)
        self.assertEqual(code, 1)
        self.assertTrue(any("merge refused" in line for line in logs))

    def test_no_open_pull_requests_is_a_quiet_success(self):
        code, logs = run(FakeGithub(prs=[]))
        self.assertEqual(code, 0)
        self.assertEqual(logs, [])


class Config(unittest.TestCase):
    def write(self, data):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "repos.json"
        path.write_text(
            data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
        )
        return path

    def test_defaults_apply_to_a_minimal_entry(self):
        settings, repos = train.load_config(self.write({"repos": [{"repo": "o/r"}]}))
        self.assertEqual(settings.label, "automerge")
        self.assertEqual(settings.needs_human_label, "needs-human")
        self.assertEqual(repos[0].gate_check, "Gate / Gate")
        self.assertFalse(repos[0].require_seal)
        self.assertEqual(repos[0].base, "main")
        self.assertEqual(repos[0].merge_method, "merge")

    def test_overrides_are_read(self):
        _, repos = train.load_config(
            self.write(
                {
                    "repos": [
                        {
                            "repo": "o/r",
                            "gate_check": "CI gate",
                            "require_seal": True,
                            "base": "trunk",
                        }
                    ],
                }
            )
        )
        self.assertEqual(
            (repos[0].gate_check, repos[0].require_seal, repos[0].base),
            ("CI gate", True, "trunk"),
        )

    def test_invalid_configuration_is_rejected(self):
        for bad in (
            "not json",
            {"repos": []},
            {"repos": [{"gate_check": "x"}]},
            {"repos": [{"repo": "no-slash"}]},
            {"repos": [{"repo": "o/r", "require_seal": "yes"}]},
            {"repos": [{"repo": "o/r", "merge_method": "fast-forward"}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(train.ConfigError):
                train.load_config(self.write(bad))


class RealGithubCommands(unittest.TestCase):
    """The commands are built from fixed words plus GitHub's own numbers and shas."""

    def setUp(self):
        self.commands = []
        self.outputs = {}
        self.errors = {}
        self.gh = train.Github(self.fake_run)

    def fake_run(self, command):
        self.commands.append(command)
        key = " ".join(command)
        for needle, error in self.errors.items():
            if needle in key:
                raise subprocess.CalledProcessError(1, command, stderr=error)
        for needle, output in self.outputs.items():
            if needle in key:
                return output
        return ""

    def test_the_merge_method_is_read_from_the_configuration(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "repos.json"
        path.write_text(
            json.dumps({"repos": [{"repo": "o/r", "merge_method": "squash"}]}),
            encoding="utf-8",
        )
        _, repos = train.load_config(path)
        self.assertEqual(repos[0].merge_method, "squash")

    def test_each_merge_method_maps_to_its_gh_flag(self):
        for method in ("merge", "squash", "rebase"):
            with self.subTest(method=method):
                self.commands.clear()
                self.gh.merge(REPO, 5, SHA, method)
                self.assertIn(f"--{method}", self.commands[0])
                self.assertIn("--match-head-commit", self.commands[0])

    def test_merge_pins_the_head_commit_and_makes_a_merge_commit(self):
        self.gh.merge(REPO, 5, SHA, "merge")
        self.assertEqual(
            self.commands[0],
            [
                "gh",
                "pr",
                "merge",
                "5",
                "--repo",
                REPO,
                "--merge",
                "--match-head-commit",
                SHA,
            ],
        )

    def test_open_prs_filters_by_label_and_parses_json(self):
        self.outputs["pr list"] = json.dumps([pr(2)])
        self.assertEqual(self.gh.open_prs(REPO, "automerge")[0]["number"], 2)
        self.assertIn("--label", self.commands[0])
        self.assertIn("automerge", self.commands[0])

    def test_gate_is_green_only_when_every_run_of_that_name_succeeded(self):
        runs = {
            "check_runs": [
                {"name": "Gate / Gate", "status": "completed", "conclusion": "success"},
                {"name": "Other", "status": "completed", "conclusion": "failure"},
            ]
        }
        self.outputs["check-runs"] = json.dumps(runs)
        self.assertTrue(self.gh.gate_green(REPO, SHA, "Gate / Gate"))
        runs["check_runs"].append(
            {"name": "Gate / Gate", "status": "completed", "conclusion": "failure"}
        )
        self.outputs["check-runs"] = json.dumps(runs)
        self.assertFalse(self.gh.gate_green(REPO, SHA, "Gate / Gate"))

    def test_gate_is_not_green_when_missing_or_still_running(self):
        self.outputs["check-runs"] = json.dumps({"check_runs": []})
        self.assertFalse(self.gh.gate_green(REPO, SHA, "Gate / Gate"))
        self.outputs["check-runs"] = json.dumps(
            {
                "check_runs": [
                    {"name": "Gate / Gate", "status": "in_progress", "conclusion": None}
                ]
            }
        )
        self.assertFalse(self.gh.gate_green(REPO, SHA, "Gate / Gate"))

    def test_the_seal_is_a_tag_that_either_exists_or_not(self):
        self.assertTrue(self.gh.sealed(REPO, SHA))
        self.errors["git/ref/tags"] = "Not Found"
        self.assertFalse(self.gh.sealed(REPO, SHA))

    def test_behind_by_reads_the_compare_endpoint(self):
        self.outputs["compare"] = json.dumps({"behind_by": 4})
        self.assertEqual(self.gh.behind_by(REPO, "main", SHA), 4)
        self.assertIn(f"repos/{REPO}/compare/main...{SHA}", self.commands[0])

    def test_update_branch_pins_the_expected_head(self):
        self.assertEqual(self.gh.update_branch(REPO, 3, SHA), "updated")
        self.assertIn(f"expected_head_sha={SHA}", self.commands[0])
        self.assertIn("PUT", self.commands[0])

    def test_update_branch_reports_a_merge_conflict_and_raises_on_other_errors(self):
        self.errors["update-branch"] = (
            "gh: merge conflict between base and head (HTTP 422)"
        )
        self.assertEqual(self.gh.update_branch(REPO, 3, SHA), "conflict")
        self.errors["update-branch"] = "gh: Resource not accessible (HTTP 403)"
        with self.assertRaises(subprocess.CalledProcessError):
            self.gh.update_branch(REPO, 3, SHA)

    def test_flagging_creates_the_label_if_needed_then_swaps_labels(self):
        self.errors["label create"] = "already exists"
        self.gh.flag_needs_human(REPO, 6, "automerge", "needs-human")
        edit = self.commands[-1]
        self.assertEqual(edit[:5], ["gh", "pr", "edit", "6", "--repo"])
        self.assertIn("--add-label", edit)
        self.assertIn("needs-human", edit)
        self.assertIn("--remove-label", edit)
        self.assertIn("automerge", edit)


if __name__ == "__main__":
    unittest.main()
