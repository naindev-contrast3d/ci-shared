"""Tests for report/ci_report.py: failure classification, the metrics and the rendered report."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "report"))

import ci_report as report

REPO = "naindev-contrast3d/example"
NOW = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=dt.UTC)


def iso(minutes_ago: float, seconds: int = 0) -> str:
    moment = NOW - dt.timedelta(minutes=minutes_ago) + dt.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(run_id, conclusion, created_min_ago, duration_s, name="CI", status="completed"):
    return {
        "databaseId": run_id,
        "conclusion": conclusion,
        "status": status,
        "workflowName": name,
        "createdAt": iso(created_min_ago),
        "startedAt": iso(created_min_ago),
        "updatedAt": iso(created_min_ago, duration_s),
        "event": "pull_request",
        "headBranch": "feat/x",
    }


def job(name, conclusion, started_after_s, failed_step=None):
    steps = [{"name": failed_step, "conclusion": "failure"}] if failed_step else []
    return {
        "name": name,
        "conclusion": conclusion,
        "startedAt": started_after_s,
        "steps": steps,
    }


class FakeGithub:
    def __init__(self, runs, jobs, runners=None, queued=None, prs=None):
        self._runs = runs
        self._jobs = jobs  # run id -> list of (name, conclusion, delay_s, failed_step)
        self._runners = runners if runners is not None else []
        self._queued = queued or []
        self._prs = prs or []

    def runs(self, repo, limit):
        return self._runs

    def jobs(self, repo, run_id):
        created = next(r for r in self._runs if r["databaseId"] == run_id)["createdAt"]
        base = dt.datetime.fromisoformat(created)
        out = []
        for name, conclusion, delay, failed_step in self._jobs.get(run_id, []):
            started = (base + dt.timedelta(seconds=delay)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            out.append(job(name, conclusion, started, failed_step))
        return out

    def job_timings(self, repo, run_id):
        return getattr(self, "timings", {}).get(run_id, [])

    def runners(self, repo):
        return self._runners

    def queued_runs(self, repo):
        return self._queued

    def open_prs(self, repo):
        return self._prs

    def merged_prs(self, repo, limit):
        return getattr(self, "merged", [])

    def file_text(self, repo, path):
        return getattr(self, "files", {}).get((repo, path))


def sample_github():
    runs = [
        run(1, "success", 60, 300),
        run(2, "success", 120, 600),
        run(3, "failure", 180, 120),  # a real test failure
        run(4, "failure", 240, 60),  # the runner could not start the job
        run(5, "cancelled", 300, 30),
        run(6, "skipped", 360, 0),
        run(7, "success", 60 * 24 * 10, 300),  # older than the window
        run(8, "success", 70, 100, name="Other"),  # another workflow
    ]
    jobs = {
        1: [("Verify", "success", 30, None)],
        2: [("Verify", "success", 10, None)],
        3: [("Verify", "success", 90, None), ("Tests", "failure", 95, "Pruebas")],
        4: [("Verify", "failure", 200, "Set up job")],
    }
    runners = [
        {"name": "vm-01", "status": "online", "busy": True},
        {"name": "vm-02", "status": "offline", "busy": False},
    ]
    queued = [
        {"databaseId": 90, "createdAt": iso(45)},
        {"databaseId": 91, "createdAt": iso(5)},
    ]
    prs = [
        {"number": 1, "labels": [{"name": "automerge"}], "createdAt": iso(60 * 5)},
        {"number": 2, "labels": [], "createdAt": iso(60)},
    ]
    return FakeGithub(runs, jobs, runners, queued, prs)


class Classification(unittest.TestCase):
    def test_a_job_that_never_started_its_steps_is_infrastructure(self):
        jobs = [job("Verify", "failure", iso(1), "Set up job")]
        self.assertEqual(report.classify_failure(jobs), "infra")

    def test_a_failure_with_no_steps_at_all_is_infrastructure(self):
        self.assertEqual(
            report.classify_failure([job("Verify", "failure", iso(1))]), "infra"
        )

    def test_a_failed_test_step_is_code(self):
        jobs = [job("Tests", "failure", iso(1), "Pruebas")]
        self.assertEqual(report.classify_failure(jobs), "code")

    def test_the_shared_preflight_failing_is_infrastructure(self):
        jobs = [
            job("Runner preflight", "failure", iso(1), "Check tools, memory and disk")
        ]
        self.assertEqual(report.classify_failure(jobs), "infra")

    def test_a_run_is_infrastructure_only_when_every_failed_job_is(self):
        jobs = [
            job("Verify", "failure", iso(1), "Set up job"),
            job("Tests", "failure", iso(1), "Pruebas"),
        ]
        self.assertEqual(report.classify_failure(jobs), "code")

    def test_no_failed_job_is_reported_as_code(self):
        self.assertEqual(
            report.classify_failure([job("Verify", "success", iso(1))]), "code"
        )


class Metrics(unittest.TestCase):
    def setUp(self):
        self.stats = report.collect(sample_github(), REPO, "CI", days=7, now=NOW)

    def test_only_conclusive_runs_of_the_workflow_inside_the_window_are_counted(self):
        self.assertEqual(self.stats.conclusive, 4)
        self.assertEqual(self.stats.failed, 2)
        self.assertEqual(self.stats.cancelled, 1)

    def test_failures_are_split_by_cause(self):
        self.assertEqual(self.stats.infra_failures, 1)

    def test_the_median_duration_is_over_conclusive_runs(self):
        self.assertEqual(self.stats.median_duration_s, 210)  # 60, 120, 300, 600

    def test_the_median_queue_time_is_the_first_job_start_after_the_run_was_created(
        self,
    ):
        self.assertEqual(self.stats.median_queue_s, 60)  # 10, 30, 90, 200

    def test_the_snapshot_reports_runners_the_queue_and_open_pull_requests(self):
        self.assertEqual((self.stats.runners_online, self.stats.runners_busy), (1, 1))
        self.assertEqual(self.stats.runners_offline, 1)
        self.assertEqual(self.stats.queued_now, 2)
        self.assertEqual(self.stats.oldest_queued_min, 45)
        self.assertEqual((self.stats.open_prs, self.stats.automerge_prs), (2, 1))

    def test_a_repository_without_runs_has_no_rates_instead_of_dividing_by_zero(self):
        stats = report.collect(FakeGithub([], {}), REPO, "CI", days=7, now=NOW)
        self.assertEqual(stats.conclusive, 0)
        self.assertIsNone(stats.fail_rate)
        self.assertIsNone(stats.median_duration_s)
        self.assertIsNone(stats.median_queue_s)


MARK = "<!-- merge-train:merged -->"


def merged_pr(number, minutes_ago, marked):
    comments = (
        [{"body": f"{MARK}\nMerged by the merge train"}]
        if marked
        else [{"body": "lgtm"}]
    )
    return {"number": number, "mergedAt": iso(minutes_ago), "comments": comments}


class MergedOutsideTheTrain(unittest.TestCase):
    """A merged pull request without the train's mark was merged around the train (FR13)."""

    def stats(self, merged, train_repos=(REPO,), train_since=None):
        gh = sample_github()
        gh.merged = merged
        return report.collect(
            gh,
            REPO,
            "CI",
            days=7,
            now=NOW,
            train_repos=set(train_repos),
            train_since=train_since,
        )

    def test_unmarked_merges_inside_the_window_are_counted(self):
        stats = self.stats(
            [merged_pr(1, 60, True), merged_pr(2, 120, False), merged_pr(3, 180, False)]
        )
        self.assertEqual((stats.merged_total, stats.merged_by_hand), (3, 2))

    def test_merges_older_than_the_window_or_before_the_train_are_not_counted(self):
        old = merged_pr(
            4, 60 * 24 * 10, False
        )  # ten days ago, outside the 7-day window
        before_train = merged_pr(5, 60 * 24 * 3, False)
        since = NOW - dt.timedelta(days=1)
        stats = self.stats(
            [old, before_train, merged_pr(6, 30, True)], train_since=since
        )
        self.assertEqual((stats.merged_total, stats.merged_by_hand), (1, 0))

    def test_a_repository_without_the_shared_train_is_not_counted_at_all(self):
        stats = self.stats([merged_pr(1, 60, False)], train_repos=())
        self.assertIsNone(stats.merged_by_hand)
        self.assertIsNone(stats.merged_total)

    def test_the_snapshot_says_how_many_were_merged_around_the_train(self):
        stats = self.stats([merged_pr(1, 60, True), merged_pr(2, 120, False)])
        text = report.render([stats], days=7, now=NOW)
        self.assertIn("Merged outside the train: 1 of 2", text)

    def test_the_real_query_asks_for_merged_pull_requests_with_their_comments(self):
        commands = []
        gh = report.Github(lambda command: commands.append(command) or "[]")
        gh.merged_prs(REPO, 100)
        self.assertIn("merged", commands[0])
        self.assertTrue(any("comments" in part for part in commands[0]))

    def test_the_mark_is_the_same_text_the_train_writes(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "train"))
        import automerge as train

        self.assertEqual(report.TRAIN_MARK, train.MERGE_MARK)


class TrainHealth(unittest.TestCase):
    """The report shows whether the train is alive and runs the current script (FR14)."""

    def setUp(self):
        import os
        import tempfile

        self.os = os
        self.dir = Path(tempfile.mkdtemp())
        self.gh = FakeGithub([], {})
        self.gh.files = {
            ("naindev-contrast3d/ci-shared", "train/automerge.py"): "script v1\n"
        }

    def write(self, name, text, minutes_ago=None):
        path = self.dir / name
        path.write_text(text, encoding="utf-8", newline="")
        if minutes_ago is not None:
            stamp = (NOW - dt.timedelta(minutes=minutes_ago)).timestamp()
            self.os.utime(path, (stamp, stamp))

    def health(self):
        return report.train_health(self.dir, self.gh, NOW)

    def test_a_recent_log_and_the_current_script_are_healthy(self):
        self.write("train.log", "[x] heartbeat\n", minutes_ago=12)
        self.write("automerge.py", "script v1\n")
        health = self.health()
        self.assertEqual(health.log_age_min, 12)
        self.assertFalse(health.silent)
        self.assertEqual(health.copy, "current")

    def test_a_log_older_than_ninety_minutes_means_the_train_is_silent(self):
        self.write("train.log", "[x] heartbeat\n", minutes_ago=130)
        self.write("automerge.py", "script v1\n")
        self.assertTrue(self.health().silent)

    def test_a_copy_that_differs_from_ci_shared_main_is_stale(self):
        self.write("train.log", "x\n", minutes_ago=5)
        self.write("automerge.py", "script v0\n")
        health = self.health()
        self.assertEqual(health.copy, "stale")
        self.assertNotEqual(health.installed_version, health.repository_version)

    def test_line_endings_alone_do_not_make_a_copy_stale(self):
        self.write("train.log", "x\n", minutes_ago=5)
        self.write("automerge.py", "script v1\r\n")
        self.assertEqual(self.health().copy, "current")

    def test_a_missing_folder_is_reported_as_not_installed(self):
        health = report.train_health(self.dir / "nowhere", self.gh, NOW)
        self.assertEqual(health.copy, "missing")
        self.assertIsNone(health.log_age_min)

    def test_the_section_states_each_finding_in_words(self):
        self.write("train.log", "x\n", minutes_ago=130)
        self.write("automerge.py", "script v0\n")
        text = report.render_train(self.health())
        self.assertIn("## Merge train", text)
        self.assertIn("no log line for 130 min", text)
        self.assertIn("differs from ci-shared main", text)

    def test_the_version_function_matches_the_trains(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "train"))
        import automerge as train

        for sample in ("a\nb\n", "a\r\nb\r\n", ""):
            self.assertEqual(report.text_version(sample), train.text_version(sample))


class AuditSection(unittest.TestCase):
    """`--audit` appends the standards audit to the report."""

    def test_the_audit_module_is_found_beside_the_report(self):
        module = report.load_audit()
        self.assertTrue(callable(module.run))
        self.assertTrue(callable(module.render))

    def test_the_section_is_the_audit_report_without_its_own_title(self):
        module = report.load_audit()
        findings = [module.Finding("o/a", "license", "error", "license not recognised")]
        text = report.audit_section(module.render(findings, NOW))
        self.assertTrue(text.startswith("## Standards audit"))
        self.assertIn("- error `license`: license not recognised", text)
        self.assertNotIn(
            "# Standards audit\n\n2026", text.replace("## Standards audit", "")
        )


class Until(unittest.TestCase):
    """A window that ends in the past makes a baseline reproducible after later changes."""

    def test_runs_created_after_the_end_of_the_window_are_ignored(self):
        gh = sample_github()
        gh._runs.append(run(20, "failure", 10, 60))  # 10 minutes ago
        gh._jobs[20] = [("Verify", "failure", 5, "Pruebas")]
        until = NOW - dt.timedelta(minutes=30)
        stats = report.collect(gh, REPO, "CI", days=7, now=NOW, until=until)
        self.assertEqual(stats.conclusive, 4)
        self.assertEqual(stats.failed, 2)

    def test_without_until_the_window_ends_now(self):
        gh = sample_github()
        gh._runs.append(run(20, "failure", 10, 60))
        gh._jobs[20] = [("Verify", "failure", 5, "Pruebas")]
        stats = report.collect(gh, REPO, "CI", days=7, now=NOW)
        self.assertEqual(stats.conclusive, 5)

    def test_the_window_start_moves_with_until(self):
        until = NOW - dt.timedelta(days=3)
        text = report.render([], days=7, now=NOW, until=until)
        self.assertIn("2026-10-04 to 2026-10-11", text)
        self.assertIn("snapshot", text.lower())

    def test_a_moment_may_be_a_date_or_a_date_and_time_in_utc(self):
        self.assertEqual(
            report.parse_moment("2026-10-14"),
            dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC),
        )
        self.assertEqual(
            report.parse_moment("2026-10-14T11:00"),
            dt.datetime(2026, 10, 14, 11, 0, tzinfo=dt.UTC),
        )
        self.assertEqual(
            report.parse_moment("2026-10-14T11:00:00Z"),
            dt.datetime(2026, 10, 14, 11, 0, tzinfo=dt.UTC),
        )


class Rendering(unittest.TestCase):
    def test_the_table_row_is_pinned(self):
        stats = report.collect(sample_github(), REPO, "CI", days=7, now=NOW)
        text = report.render([stats], days=7, now=NOW)
        self.assertIn(
            f"| {REPO} | 4 | 2 | 50% | 50% | 3m30s | 1m00s |",
            text,
        )

    def test_the_snapshot_lines_are_present(self):
        stats = report.collect(sample_github(), REPO, "CI", days=7, now=NOW)
        text = report.render([stats], days=7, now=NOW)
        self.assertIn("Runners: 1 online (1 busy), 1 offline", text)
        self.assertIn("Queued now: 2 (oldest 45 min)", text)
        self.assertIn("Open pull requests: 2 (1 labelled automerge)", text)

    def test_a_repository_without_data_renders_dashes(self):
        stats = report.collect(FakeGithub([], {}), REPO, "CI", days=7, now=NOW)
        text = report.render([stats], days=7, now=NOW)
        self.assertIn(f"| {REPO} | 0 | 0 | - | - | - | - |", text)

    def test_the_header_states_the_period(self):
        text = report.render([], days=7, now=NOW)
        self.assertIn("2026-10-07", text)
        self.assertIn("2026-10-14", text)

    def test_durations_are_formatted_for_people(self):
        self.assertEqual(report.fmt_duration(45), "45s")
        self.assertEqual(report.fmt_duration(210), "3m30s")
        self.assertEqual(report.fmt_duration(3720), "1h02m")


def timing(name, queued_s, conclusion="success", started=True):
    """A job from the REST API that waited `queued_s` seconds for a runner."""
    created = NOW - dt.timedelta(hours=1)
    began = created + dt.timedelta(seconds=queued_s)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "name": name,
        "conclusion": conclusion,
        "created_at": created.strftime(stamp),
        "started_at": began.strftime(stamp) if started else None,
    }


class JobQueueTime(unittest.TestCase):
    def test_a_job_waits_from_its_creation_to_its_start(self):
        waits = report.job_waits([timing("Verify", 45), timing("Seal", 780)])
        self.assertEqual(waits, [("Verify", 45), ("Seal", 780)])

    def test_skipped_and_never_started_jobs_do_not_count(self):
        jobs = [timing("A", 0, "skipped"), timing("B", 0, None, started=False)]
        self.assertEqual(report.job_waits(jobs), [])

    def test_a_clock_skew_never_gives_a_negative_wait(self):
        self.assertEqual(report.job_waits([timing("A", -3)]), [("A", 0)])

    def test_the_percentile_is_the_nearest_rank(self):
        values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        self.assertEqual(report.percentile(values, 90), 90)
        self.assertEqual(report.percentile(values, 50), 50)
        self.assertEqual(report.percentile([7], 90), 7)
        self.assertIsNone(report.percentile([], 90))

    def test_the_report_gives_median_p90_max_and_the_slowest_jobs(self):
        gh = sample_github()
        gh.timings = {
            1: [timing("Verify", 10), timing("Seal", 780)],
            2: [timing("Verify", 20), timing("Seal", 600)],
            3: [timing("Verify", 30), timing("Seal", 900)],
        }
        stats = report.collect(gh, REPO, "CI", days=7, now=NOW)
        self.assertEqual(stats.jobs_sampled, 6)
        self.assertEqual(stats.job_wait_max_s, 900)
        self.assertEqual(stats.job_wait_p90_s, 900)
        self.assertEqual(stats.job_wait_median_s, 315)  # 10, 20, 30, 600, 780, 900
        self.assertEqual(stats.slowest_jobs[0], ("Seal", 780, 3))

    def test_a_repository_without_timings_has_no_queue_figures(self):
        stats = report.collect(sample_github(), REPO, "CI", days=7, now=NOW)
        self.assertEqual(stats.jobs_sampled, 0)
        self.assertIsNone(stats.job_wait_median_s)
        self.assertEqual(stats.slowest_jobs, [])

    def test_the_section_names_the_slowest_job_and_the_runner_count(self):
        gh = sample_github()
        gh.timings = {1: [timing("Verify", 10), timing("Seal", 780)]}
        stats = report.collect(gh, REPO, "CI", days=7, now=NOW)
        text = report.render([stats], days=7, now=NOW)
        self.assertIn("## Queue time per job", text)
        self.assertIn(
            f"| {REPO} | 2 | 6m35s | 13m00s | 13m00s | Seal (13m00s) | 1 |", text
        )

    def test_the_section_is_left_out_when_no_repository_has_timings(self):
        stats = report.collect(sample_github(), REPO, "CI", days=7, now=NOW)
        self.assertNotIn(
            "## Queue time per job", report.render([stats], days=7, now=NOW)
        )

    def test_the_real_command_reads_the_jobs_of_the_run_with_their_creation_time(self):
        calls = []

        def fake(command):
            calls.append(command)
            return json.dumps({"jobs": [timing("Seal", 5)]})

        jobs = report.Github(fake).job_timings(REPO, 42)
        self.assertEqual(jobs[0]["name"], "Seal")
        self.assertEqual(calls[0][:2], ["gh", "api"])
        self.assertIn(f"repos/{REPO}/actions/runs/42/jobs", " ".join(calls[0]))

    def test_a_run_whose_jobs_cannot_be_read_gives_no_timings(self):
        def refuse(command):
            raise subprocess.CalledProcessError(1, command, stderr="HTTP 404")

        self.assertEqual(report.Github(refuse).job_timings(REPO, 42), [])


class RealCommands(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.outputs = {}
        self.gh = report.Github(self.fake_run)

    def fake_run(self, command):
        self.commands.append(command)
        key = " ".join(command)
        for needle, output in self.outputs.items():
            if needle in key:
                return output
        return "[]"

    def test_runs_asks_for_the_fields_the_metrics_need(self):
        self.gh.runs(REPO, 200)
        command = self.commands[0]
        self.assertEqual(command[:4], ["gh", "run", "list", "--repo"])
        fields = (
            "databaseId,conclusion,status,workflowName,createdAt,startedAt,updatedAt"
        )
        self.assertTrue(any(fields in part for part in command))

    def test_jobs_reads_the_jobs_of_one_run(self):
        self.outputs["run view"] = json.dumps(
            {"jobs": [job("Verify", "success", iso(1))]}
        )
        self.assertEqual(self.gh.jobs(REPO, 7)[0]["name"], "Verify")
        self.assertIn("7", self.commands[0])

    def test_runners_reads_the_repository_runners(self):
        self.outputs["actions/runners"] = json.dumps({"runners": [{"name": "vm-01"}]})
        self.assertEqual(self.gh.runners(REPO)[0]["name"], "vm-01")

    def test_a_repository_without_runner_access_reports_none_instead_of_failing(self):
        def refuse(command):
            raise subprocess.CalledProcessError(1, command, stderr="HTTP 403")

        self.assertEqual(report.Github(refuse).runners(REPO), [])


if __name__ == "__main__":
    unittest.main()
