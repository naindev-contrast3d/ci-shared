"""Tests for report/weekly_report.py: the report written to a file, and a toast only for a deviation."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "report"))

import ci_report
import weekly_report as weekly

NOW = dt.datetime(2026, 10, 19, 8, 0, 0, tzinfo=dt.UTC)  # a Monday
ORG = "acme"


def stats(repo="acme/a", conclusive=20, failed=2, infra=0, p90=None):
    s = ci_report.RepoStats(
        repo=repo, conclusive=conclusive, failed=failed, infra_failures=infra
    )
    s.job_wait_p90_s = p90
    s.jobs_sampled = 1 if p90 is not None else 0
    return s


class Deviations(unittest.TestCase):
    def test_a_healthy_repository_raises_nothing(self):
        self.assertEqual(weekly.find_deviations([stats(failed=2, p90=60)]), [])

    def test_a_high_failure_rate_is_a_deviation(self):
        found = weekly.find_deviations([stats(conclusive=20, failed=6)])
        self.assertEqual(len(found), 1)
        self.assertIn("acme/a", found[0])
        self.assertIn("30%", found[0])

    def test_a_few_runs_prove_nothing(self):
        # Two failures out of three is noise: below the minimum number of runs nothing is judged.
        self.assertEqual(weekly.find_deviations([stats(conclusive=3, failed=2)]), [])

    def test_a_long_queue_is_a_deviation(self):
        found = weekly.find_deviations([stats(p90=601)])
        self.assertEqual(len(found), 1)
        self.assertIn("10m01s", found[0])

    def test_infrastructure_failures_are_a_deviation_of_their_own(self):
        found = weekly.find_deviations([stats(conclusive=20, failed=4, infra=3)])
        self.assertTrue(any("infrastructure" in f for f in found))

    def test_the_thresholds_are_parameters(self):
        self.assertEqual(
            len(weekly.find_deviations([stats(failed=2)], fail_rate_max=0.05)), 1
        )
        self.assertEqual(weekly.find_deviations([stats(p90=601)], wait_p90_max=900), [])

    def test_a_repository_without_data_is_not_judged(self):
        self.assertEqual(weekly.find_deviations([stats(conclusive=0, failed=0)]), [])


class FakeGithub:
    """Just enough of the reads `ci_report.collect` makes."""

    def __init__(self, failing=False):
        self.failing = failing

    def org_repos(self, org):
        return [
            {"name": "a", "visibility": "PRIVATE", "isArchived": False},
            {"name": "old", "visibility": "PRIVATE", "isArchived": True},
            {"name": "open", "visibility": "PUBLIC", "isArchived": False},
        ]

    def runs(self, repo, limit):
        out = []
        for i in range(10):
            conclusion = "failure" if (self.failing and i < 6) else "success"
            moment = (NOW - dt.timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append(
                {
                    "databaseId": i + 1,
                    "conclusion": conclusion,
                    "status": "completed",
                    "workflowName": "CI",
                    "createdAt": moment,
                    "startedAt": moment,
                    "updatedAt": moment,
                    "event": "push",
                    "headBranch": "main",
                }
            )
        return out

    def jobs(self, repo, run_id):
        return [
            {
                "name": "Verify",
                "conclusion": "failure",
                "startedAt": None,
                "steps": [{"name": "Tests", "conclusion": "failure"}],
            }
        ]

    def job_timings(self, repo, run_id):
        return []

    def runners(self, repo):
        return []

    def queued_runs(self, repo):
        return []

    def open_prs(self, repo):
        return []


class Running(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.toasts = []

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, gh, notify_path=r"C:\x\notify.ps1"):
        return weekly.run(
            gh,
            ORG,
            self.out,
            NOW,
            days=7,
            notify_path=notify_path,
            runner=self.toasts.append,
        )

    def test_the_report_is_written_with_the_date_in_its_name_and_as_latest(self):
        result = self.go(FakeGithub())
        dated = self.out / "ci-report-2026-10-19.md"
        self.assertTrue(dated.exists())
        self.assertEqual(
            (self.out / "latest.md").read_text(encoding="utf-8"),
            dated.read_text(encoding="utf-8"),
        )
        text = dated.read_text(encoding="utf-8")
        self.assertIn("# CI health report", text)
        self.assertIn("acme/a", text)
        self.assertEqual(result.path, dated)

    def test_only_private_repositories_that_are_not_archived_are_reported(self):
        self.go(FakeGithub())
        text = (self.out / "latest.md").read_text(encoding="utf-8")
        self.assertNotIn("acme/old", text)
        self.assertNotIn("acme/open", text)

    def test_a_healthy_week_writes_the_report_and_raises_no_toast(self):
        result = self.go(FakeGithub())
        self.assertEqual(result.deviations, [])
        self.assertEqual(self.toasts, [])

    def test_a_bad_week_raises_one_toast_naming_the_repositories(self):
        result = self.go(FakeGithub(failing=True))
        self.assertGreaterEqual(len(result.deviations), 1)
        self.assertEqual(len(self.toasts), 1)
        command = self.toasts[0]
        self.assertIn("-File", command)
        self.assertIn("acme/a", command[command.index("-Body") + 1])

    def test_without_a_notify_path_nothing_is_toasted(self):
        self.go(FakeGithub(failing=True), notify_path=None)
        self.assertEqual(self.toasts, [])

    def test_a_failing_toast_never_stops_the_report(self):
        def boom(command):
            raise OSError("no powershell")

        result = weekly.run(
            FakeGithub(failing=True),
            ORG,
            self.out,
            NOW,
            days=7,
            notify_path="n.ps1",
            runner=boom,
        )
        self.assertTrue(result.path.exists())


if __name__ == "__main__":
    unittest.main()
