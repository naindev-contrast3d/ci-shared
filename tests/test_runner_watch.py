"""Tests for report/runner_watch.py: when a runner or a queue deserves a toast, and only once."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "report"))

import runner_watch as watch

NOW = dt.datetime(2026, 10, 15, 12, 0, 0, tzinfo=dt.UTC)
ORG = "acme"


def ago(minutes: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot(runners=(), queued=()):
    return {"runners": list(runners), "queued": list(queued)}


class Offline(unittest.TestCase):
    def test_an_online_runner_raises_nothing(self):
        alerts, state = watch.evaluate(
            snapshot([("acme/a", "a-01", "online")]), {}, NOW
        )
        self.assertEqual(alerts, [])
        self.assertEqual(state["offline_since"], {})

    def test_a_runner_just_seen_offline_is_remembered_not_reported(self):
        alerts, state = watch.evaluate(
            snapshot([("acme/a", "a-01", "offline")]), {}, NOW
        )
        self.assertEqual(alerts, [])
        self.assertEqual(state["offline_since"], {"acme/a/a-01": ago(0)})

    def test_it_is_reported_once_it_has_been_offline_long_enough(self):
        state = {"offline_since": {"acme/a/a-01": ago(11)}}
        alerts, state = watch.evaluate(
            snapshot([("acme/a", "a-01", "offline")]), state, NOW
        )
        self.assertEqual([a.key for a in alerts], ["offline:acme/a/a-01"])
        self.assertIn("a-01", alerts[0].body)
        self.assertIn("11 min", alerts[0].body)

    def test_a_runner_offline_for_less_than_the_threshold_is_not_reported(self):
        state = {"offline_since": {"acme/a/a-01": ago(9)}}
        alerts, _ = watch.evaluate(
            snapshot([("acme/a", "a-01", "offline")]), state, NOW
        )
        self.assertEqual(alerts, [])

    def test_the_same_runner_is_not_reported_twice(self):
        state = {"offline_since": {"acme/a/a-01": ago(30)}}
        alerts, state = watch.evaluate(
            snapshot([("acme/a", "a-01", "offline")]), state, NOW
        )
        self.assertEqual(len(alerts), 1)
        again, _ = watch.evaluate(snapshot([("acme/a", "a-01", "offline")]), state, NOW)
        self.assertEqual(again, [])

    def test_a_runner_that_comes_back_is_forgotten_and_can_be_reported_again(self):
        state = {
            "offline_since": {"acme/a/a-01": ago(30)},
            "alerted": ["offline:acme/a/a-01"],
        }
        alerts, state = watch.evaluate(
            snapshot([("acme/a", "a-01", "online")]), state, NOW
        )
        self.assertEqual(alerts, [])
        self.assertEqual(state["offline_since"], {})
        self.assertEqual(state["alerted"], [])

    def test_an_ignored_runner_is_never_reported(self):
        state = {"offline_since": {"acme/a/a-01": ago(300)}}
        alerts, state = watch.evaluate(
            snapshot([("acme/a", "a-01", "offline")]), state, NOW, ignore={"a-01"}
        )
        self.assertEqual(alerts, [])
        self.assertEqual(state["offline_since"], {})

    def test_a_runner_that_vanished_from_the_list_is_forgotten(self):
        state = {
            "offline_since": {"acme/a/gone": ago(300)},
            "alerted": ["offline:acme/a/gone"],
        }
        _, state = watch.evaluate(snapshot([]), state, NOW)
        self.assertEqual(state["offline_since"], {})
        self.assertEqual(state["alerted"], [])


class Queue(unittest.TestCase):
    def test_a_young_queued_run_is_not_reported(self):
        alerts, _ = watch.evaluate(snapshot(queued=[("acme/a", 7, ago(14))]), {}, NOW)
        self.assertEqual(alerts, [])

    def test_an_old_queued_run_is_reported_once(self):
        snap = snapshot(queued=[("acme/a", 7, ago(16))])
        alerts, state = watch.evaluate(snap, {}, NOW)
        self.assertEqual([a.key for a in alerts], ["queue:acme/a/7"])
        self.assertIn("16 min", alerts[0].body)
        again, _ = watch.evaluate(snap, state, NOW)
        self.assertEqual(again, [])

    def test_a_run_that_started_is_forgotten(self):
        state = {"alerted": ["queue:acme/a/7"]}
        _, state = watch.evaluate(snapshot(queued=[]), state, NOW)
        self.assertEqual(state["alerted"], [])

    def test_the_thresholds_are_parameters(self):
        snap = snapshot(queued=[("acme/a", 7, ago(5))])
        alerts, _ = watch.evaluate(snap, {}, NOW, queue_min=3)
        self.assertEqual(len(alerts), 1)


class Collecting(unittest.TestCase):
    def test_it_reads_the_organisation_and_the_private_repositories(self):
        class Gh:
            def org_repos(self, org):
                return [
                    {"name": "a", "visibility": "PRIVATE", "isArchived": False},
                    {"name": "old", "visibility": "PRIVATE", "isArchived": True},
                    {"name": "open", "visibility": "PUBLIC", "isArchived": False},
                ]

            def org_runners(self, org):
                return [{"name": "pool-01", "status": "online"}]

            def runners(self, repo):
                return [{"name": f"{repo}-01", "status": "offline"}]

            def queued_runs(self, repo):
                return [{"databaseId": 5, "createdAt": ago(40)}]

        snap = watch.collect(Gh(), ORG)
        self.assertIn(("acme", "pool-01", "online"), snap["runners"])
        self.assertIn(("acme/a", "acme/a-01", "offline"), snap["runners"])
        self.assertEqual(snap["queued"], [("acme/a", 5, ago(40))])
        self.assertEqual(
            len(snap["runners"]), 2, "archived and public repositories are skipped"
        )

    def test_an_unreadable_part_does_not_stop_the_rest(self):
        class Gh:
            def org_repos(self, org):
                return [{"name": "a", "visibility": "PRIVATE", "isArchived": False}]

            def org_runners(self, org):
                raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403")

            def runners(self, repo):
                return [{"name": "a-01", "status": "online"}]

            def queued_runs(self, repo):
                raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")

        snap = watch.collect(Gh(), ORG)
        self.assertEqual(snap["runners"], [("acme/a", "a-01", "online")])
        self.assertEqual(snap["queued"], [])


class RealCommands(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake(self, command):
        self.calls.append(command)
        return json.dumps({"runners": [{"name": "pool-01", "status": "online"}]})

    def test_org_runners_asks_the_organisation_endpoint(self):
        runners = watch.Github(self.fake).org_runners(ORG)
        self.assertEqual(runners[0]["name"], "pool-01")
        self.assertIn("orgs/acme/actions/runners", " ".join(self.calls[0]))

    def test_org_repos_asks_for_visibility_and_archived(self):
        watch.Github(lambda c: self.calls.append(c) or "[]").org_repos(ORG)
        joined = " ".join(self.calls[0])
        self.assertIn("repo list acme", joined)
        self.assertIn("name,visibility,isArchived", joined)


class Notifying(unittest.TestCase):
    def test_each_alert_becomes_one_toast_command(self):
        sent = []
        alerts = [watch.Alert("k1", "T1", "B1"), watch.Alert("k2", "T2", "B2")]
        watch.notify(alerts, r"C:\x\notify.ps1", lambda c: sent.append(c))
        self.assertEqual(len(sent), 2)
        self.assertIn("-File", sent[0])
        self.assertEqual(sent[0][sent[0].index("-Title") + 1], "T1")
        self.assertEqual(sent[1][sent[1].index("-Body") + 1], "B2")

    def test_a_failing_toast_never_raises(self):
        def boom(command):
            raise OSError("no powershell")

        watch.notify([watch.Alert("k", "T", "B")], "notify.ps1", boom)


class Main(unittest.TestCase):
    def test_state_is_saved_and_a_second_run_stays_quiet(self):
        import tempfile

        class Gh:
            def org_repos(self, org):
                return []

            def org_runners(self, org):
                return [{"name": "pool-01", "status": "offline"}]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = {"offline_since": {"acme/pool-01": ago(40)}}
            path.write_text(json.dumps(state), encoding="utf-8")
            first = watch.run(Gh(), ORG, path, NOW, notify_path=None)
            second = watch.run(Gh(), ORG, path, NOW, notify_path=None)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertIn(
                "offline:acme/pool-01",
                json.loads(path.read_text(encoding="utf-8"))["alerted"],
            )

    def test_a_corrupt_state_file_is_a_clean_slate(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(watch.load_state(path), {})


if __name__ == "__main__":
    unittest.main()
