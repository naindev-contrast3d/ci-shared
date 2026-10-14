"""Tests for train/automerge.py: the decision rules, the cycle and the real `gh` commands."""

from __future__ import annotations

import datetime as dt
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
        queue=None,
        queue_error=False,
        comment_error=False,
    ):
        self.comment_error = comment_error
        self.comments = []
        self.prs = prs or []
        self.queue = queue or {}
        self.queue_error = queue_error
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

    def queued_runs(self, repo):
        if self.queue_error:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="queue boom")
        return self.queue.get(repo, [])

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

    def comment(self, repo, number, body):
        if self.comment_error:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="comment boom")
        self.comments.append((repo, number, body))

    def update_branch(self, repo, number, sha):
        self.calls.append(("update", repo, number, sha))
        return self.update_result

    def flag_needs_human(self, repo, number, label, needs_label):
        self.calls.append(("flag", repo, number, label, needs_label))

    def mutations(self):
        return [c for c in self.calls if c[0] != "open_prs"]


def run(gh, repos=None, dry_run=False, alerts=None, now=None, heartbeat=None):
    logs = []
    settings = train.Settings()
    configs = repos or [train.RepoConfig(repo=REPO)]
    code = train.run_cycle(
        gh,
        configs,
        settings,
        dry_run=dry_run,
        log=logs.append,
        alerts=alerts,
        now=now,
        heartbeat=heartbeat,
    )
    return code, logs


NOW = dt.datetime(2026, 10, 14, 13, 0, 0, tzinfo=dt.UTC)


class MergeMark(unittest.TestCase):
    """The train marks what it merges, so a merge made around it can be counted (FR13)."""

    def test_a_merged_pull_request_gets_the_train_mark_with_its_head(self):
        gh = FakeGithub(prs=[pr(7)])
        run(gh)
        self.assertEqual(len(gh.comments), 1)
        repo, number, body = gh.comments[0]
        self.assertEqual((repo, number), (REPO, 7))
        self.assertIn(train.MERGE_MARK, body)
        self.assertIn(SHA[:12], body)

    def test_nothing_is_marked_in_a_dry_run_or_when_the_merge_is_refused(self):
        dry = FakeGithub(prs=[pr(7)])
        run(dry, dry_run=True)
        refused = FakeGithub(prs=[pr(7)], merge_error=True)
        run(refused)
        self.assertEqual((dry.comments, refused.comments), ([], []))

    def test_a_failing_comment_is_logged_and_does_not_undo_or_fail_the_merge(self):
        gh = FakeGithub(prs=[pr(7)], comment_error=True)
        code, logs = run(gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.mutations(), [("merge", REPO, 7, SHA, "merge")])
        self.assertTrue(any("mark failed" in line for line in logs))


class HeartbeatLine(unittest.TestCase):
    """A line at least once an hour proves the train is alive and says which script it runs (FR14)."""

    def state(self):
        return Path(tempfile.mkdtemp()) / "heartbeat.json"

    def test_the_first_cycle_logs_a_heartbeat_with_counts_and_the_script_version(self):
        gh = FakeGithub(prs=[pr(1), pr(2, "b" * 40)], behind=2)
        _, logs = run(gh, heartbeat=train.Heartbeat(self.state()), now=NOW)
        line = next(item for item in logs if "heartbeat" in item)
        self.assertIn("1 repositories", line)
        self.assertIn("2 labelled pull requests", line)
        self.assertIn(train.script_version(), line)

    def test_no_second_heartbeat_within_the_hour_but_one_after_it(self):
        path = self.state()
        gh = FakeGithub(prs=[])
        _, first = run(gh, heartbeat=train.Heartbeat(path), now=NOW)
        _, soon = run(
            gh, heartbeat=train.Heartbeat(path), now=NOW + dt.timedelta(minutes=59)
        )
        _, later = run(
            gh, heartbeat=train.Heartbeat(path), now=NOW + dt.timedelta(minutes=61)
        )
        counts = [
            sum("heartbeat" in item for item in logs) for logs in (first, soon, later)
        ]
        self.assertEqual(counts, [1, 0, 1])

    def test_a_dry_run_does_not_write_the_heartbeat_state(self):
        path = self.state()
        run(FakeGithub(prs=[]), dry_run=True, heartbeat=train.Heartbeat(path), now=NOW)
        self.assertFalse(path.exists())

    def test_the_script_version_is_the_same_for_lf_and_crlf_copies(self):
        text = "a\nb\n"
        self.assertEqual(
            train.text_version(text), train.text_version(text.replace("\n", "\r\n"))
        )
        self.assertNotEqual(train.text_version(text), train.text_version(text + "c\n"))


def queued(run_id, minutes_ago, branch="feat/x", workflow="CI"):
    created = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "databaseId": run_id,
        "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "headBranch": branch,
        "workflowName": workflow,
    }


class QueueAlert(unittest.TestCase):
    def state_path(self):
        return Path(tempfile.mkdtemp()) / "queue-alerts.json"

    def test_a_run_queued_longer_than_the_threshold_is_logged_with_its_details(self):
        gh = FakeGithub(queue={REPO: [queued(77, 45, branch="feat/y", workflow="CI")]})
        _, logs = run(gh, alerts=train.QueueAlerts(self.state_path()), now=NOW)
        line = next(item for item in logs if "queued" in item)
        for part in (REPO, "77", "feat/y", "CI", "45"):
            self.assertIn(part, line)

    def test_a_younger_run_is_not_logged(self):
        gh = FakeGithub(queue={REPO: [queued(78, 5)]})
        _, logs = run(gh, alerts=train.QueueAlerts(self.state_path()), now=NOW)
        self.assertFalse(any("queued" in item for item in logs))

    def test_the_same_run_is_logged_only_once_even_across_restarts(self):
        path = self.state_path()
        gh = FakeGithub(queue={REPO: [queued(79, 60)]})
        _, first = run(gh, alerts=train.QueueAlerts(path), now=NOW)
        _, again = run(gh, alerts=train.QueueAlerts(path), now=NOW)
        self.assertEqual(sum("queued" in item for item in first), 1)
        self.assertEqual(sum("queued" in item for item in again), 0)

    def test_runs_that_left_the_queue_are_forgotten(self):
        path = self.state_path()
        run(
            FakeGithub(queue={REPO: [queued(80, 60)]}),
            alerts=train.QueueAlerts(path),
            now=NOW,
        )
        run(FakeGithub(queue={REPO: []}), alerts=train.QueueAlerts(path), now=NOW)
        self.assertNotIn("80", path.read_text(encoding="utf-8"))

    def test_a_dry_run_logs_but_does_not_remember(self):
        path = self.state_path()
        gh = FakeGithub(queue={REPO: [queued(81, 60)]})
        _, logs = run(gh, dry_run=True, alerts=train.QueueAlerts(path), now=NOW)
        self.assertTrue(any("queued" in item for item in logs))
        self.assertFalse(path.exists())

    def test_the_alert_works_even_when_the_repository_has_no_open_pull_requests(self):
        gh = FakeGithub(prs=[], queue={REPO: [queued(82, 90)]})
        _, logs = run(gh, alerts=train.QueueAlerts(self.state_path()), now=NOW)
        self.assertTrue(any("82" in item for item in logs))

    def test_a_failing_queue_query_is_logged_and_does_not_stop_the_merge(self):
        gh = FakeGithub(prs=[pr(3)], queue_error=True)
        code, logs = run(gh, alerts=train.QueueAlerts(self.state_path()), now=NOW)
        self.assertEqual(code, 0)
        self.assertTrue(any("queue check failed" in item for item in logs))
        self.assertEqual(gh.mutations(), [("merge", REPO, 3, SHA, "merge")])

    def test_without_an_alerts_object_nothing_is_checked(self):
        gh = FakeGithub(queue={REPO: [queued(83, 90)]})
        _, logs = run(gh)
        self.assertEqual(logs, [])

    def test_the_threshold_comes_from_the_settings(self):
        gh = FakeGithub(queue={REPO: [queued(84, 10)]})
        logs = []
        train.run_cycle(
            gh,
            [train.RepoConfig(repo=REPO)],
            train.Settings(queue_alert_minutes=5),
            dry_run=False,
            log=logs.append,
            alerts=train.QueueAlerts(self.state_path()),
            now=NOW,
        )
        self.assertTrue(any("84" in item for item in logs))


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
        self.assertEqual(settings.queue_alert_minutes, 30)

    def test_the_queue_alert_threshold_can_be_set_and_must_be_a_positive_integer(self):
        settings, _ = train.load_config(
            self.write({"queue_alert_minutes": 10, "repos": [{"repo": "o/r"}]})
        )
        self.assertEqual(settings.queue_alert_minutes, 10)
        for bad in (0, -5, "10", 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(train.ConfigError):
                train.load_config(
                    self.write({"queue_alert_minutes": bad, "repos": [{"repo": "o/r"}]})
                )

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

    def test_comment_posts_the_body_on_the_pull_request(self):
        self.gh.comment(REPO, 9, f"{train.MERGE_MARK}\nMerged")
        self.assertEqual(
            self.commands[0][:7], ["gh", "pr", "comment", "9", "--repo", REPO, "--body"]
        )
        self.assertIn(train.MERGE_MARK, self.commands[0][7])

    def test_queued_runs_asks_for_queued_status_and_parses_json(self):
        self.outputs["run list"] = json.dumps([queued(5, 40)])
        runs = self.gh.queued_runs(REPO)
        self.assertEqual(runs[0]["databaseId"], 5)
        command = self.commands[0]
        self.assertEqual(command[:4], ["gh", "run", "list", "--repo"])
        self.assertIn("queued", command)
        self.assertIn("databaseId,createdAt,headBranch,workflowName", command)

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
