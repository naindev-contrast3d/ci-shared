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

    def runners(self, repo):
        return self._runners

    def queued_runs(self, repo):
        return self._queued

    def open_prs(self, repo):
        return self._prs


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
