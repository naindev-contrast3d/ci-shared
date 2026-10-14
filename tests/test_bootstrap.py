"""Tests for standards/bootstrap.py: the plan of settings for a new repository and its application."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "standards"))

import bootstrap

REPO = "naindev-contrast3d/new-app"


def settings(**overrides):
    base = {
        "private": True,
        "archived": False,
        "delete_branch_on_merge": False,
        "security_and_analysis": {
            "secret_scanning": {"status": "disabled"},
            "secret_scanning_push_protection": {"status": "disabled"},
        },
    }
    base.update(overrides)
    return base


class FakeGithub:
    def __init__(self, repo=None, labels=(), alerts=False, fixes=False):
        self._repo = repo or settings()
        self._labels = list(labels)
        self._alerts = alerts
        self._fixes = fixes
        self.calls = []

    def repo(self, full_name):
        return self._repo

    def labels(self, full_name):
        return self._labels

    def alerts_enabled(self, full_name):
        return self._alerts

    def security_fixes_enabled(self, full_name):
        return self._fixes

    def patch_repo(self, full_name, fields):
        self.calls.append(("patch", fields))

    def create_label(self, full_name, name, color, description):
        self.calls.append(("label", name))

    def enable_alerts(self, full_name):
        self.calls.append(("alerts",))

    def enable_security_fixes(self, full_name):
        self.calls.append(("fixes",))


def names(changes):
    return {c.setting for c in changes}


class Plan(unittest.TestCase):
    def test_a_fresh_private_repository_needs_branch_deletion_labels_and_security_updates(
        self,
    ):
        changes = bootstrap.plan(FakeGithub(), REPO)
        self.assertEqual(
            names(changes),
            {
                "delete_branch_on_merge",
                "label:automerge",
                "label:needs-human",
                "vulnerability_alerts",
                "automated_security_fixes",
            },
        )

    def test_secret_scanning_is_asked_for_only_on_public_repositories(self):
        public = FakeGithub(settings(private=False))
        self.assertIn("secret_scanning", names(bootstrap.plan(public, REPO)))
        self.assertIn(
            "secret_scanning_push_protection", names(bootstrap.plan(public, REPO))
        )
        self.assertNotIn("secret_scanning", names(bootstrap.plan(FakeGithub(), REPO)))

    def test_settings_already_in_place_are_not_planned(self):
        done = settings(delete_branch_on_merge=True)
        gh = FakeGithub(
            done, labels=["automerge", "needs-human"], alerts=True, fixes=True
        )
        self.assertEqual(bootstrap.plan(gh, REPO), [])

    def test_an_archived_repository_is_refused(self):
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.plan(FakeGithub(settings(archived=True)), REPO)

    def test_every_change_says_what_it_is_now_and_what_it_will_be(self):
        change = next(
            c
            for c in bootstrap.plan(FakeGithub(), REPO)
            if c.setting == "delete_branch_on_merge"
        )
        self.assertEqual((change.current, change.wanted), ("off", "on"))


class Apply(unittest.TestCase):
    def test_nothing_changes_without_apply(self):
        gh = FakeGithub()
        bootstrap.apply(gh, REPO, bootstrap.plan(gh, REPO), dry_run=True)
        self.assertEqual(gh.calls, [])

    def test_apply_makes_each_planned_change_once(self):
        gh = FakeGithub()
        bootstrap.apply(gh, REPO, bootstrap.plan(gh, REPO), dry_run=False)
        kinds = [call[0] for call in gh.calls]
        self.assertEqual(sorted(kinds), ["alerts", "fixes", "label", "label", "patch"])
        patch = next(call for call in gh.calls if call[0] == "patch")
        self.assertEqual(patch[1], {"delete_branch_on_merge": True})

    def test_public_security_settings_go_in_one_patch(self):
        gh = FakeGithub(settings(private=False, delete_branch_on_merge=True))
        bootstrap.apply(gh, REPO, bootstrap.plan(gh, REPO), dry_run=False)
        patch = next(call for call in gh.calls if call[0] == "patch")
        self.assertEqual(
            patch[1],
            {
                "security_and_analysis": {
                    "secret_scanning": {"status": "enabled"},
                    "secret_scanning_push_protection": {"status": "enabled"},
                }
            },
        )


class Rendering(unittest.TestCase):
    def test_the_plan_is_printed_as_a_list_and_says_when_nothing_is_needed(self):
        text = bootstrap.render(bootstrap.plan(FakeGithub(), REPO), REPO, dry_run=True)
        self.assertIn("dry run", text.lower())
        self.assertIn("delete_branch_on_merge: off -> on", text)
        done = FakeGithub(
            settings(delete_branch_on_merge=True),
            labels=["automerge", "needs-human"],
            alerts=True,
            fixes=True,
        )
        self.assertIn(
            "already follows",
            bootstrap.render(bootstrap.plan(done, REPO), REPO, dry_run=True),
        )


class RealCommands(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.outputs = {}
        self.errors = {}
        self.gh = bootstrap.Github(self.fake_run)

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

    def test_alerts_are_enabled_when_the_endpoint_answers_and_not_when_it_is_404(self):
        self.assertTrue(self.gh.alerts_enabled(REPO))
        self.errors["vulnerability-alerts"] = "HTTP 404"
        self.assertFalse(self.gh.alerts_enabled(REPO))

    def test_labels_are_read_by_name(self):
        self.outputs["labels"] = json.dumps([{"name": "automerge"}, {"name": "bug"}])
        self.assertEqual(self.gh.labels(REPO), ["automerge", "bug"])

    def test_the_repository_is_patched_with_a_json_body(self):
        self.gh.patch_repo(REPO, {"delete_branch_on_merge": True})
        command = self.commands[0]
        self.assertIn("PATCH", command)
        self.assertIn("--input", command)

    def test_security_updates_and_alerts_use_their_endpoints(self):
        self.gh.enable_alerts(REPO)
        self.gh.enable_security_fixes(REPO)
        joined = [" ".join(c) for c in self.commands]
        self.assertTrue(any("PUT" in c and "vulnerability-alerts" in c for c in joined))
        self.assertTrue(
            any("PUT" in c and "automated-security-fixes" in c for c in joined)
        )


if __name__ == "__main__":
    unittest.main()
