"""Tests for standards/audit.py: each rule, the exceptions, the severities and the rendering."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "standards"))

import audit

ORG = "naindev-contrast3d"
NOW = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=dt.UTC)
GOOD_PIN = (
    "    uses: naindev-contrast3d/ci-shared/.github/workflows/gate.yml"
    "@150de5008682330747ba6880f1649b388858c206 # v0.1.0\n"
)


def repo(
    name, *, private=True, delete_branch=True, spdx="MIT", scanning=True, archived=False
):
    status = "enabled" if scanning else "disabled"
    return {
        "name": name,
        "full_name": f"{ORG}/{name}",
        "private": private,
        "visibility": "private" if private else "public",
        "archived": archived,
        "default_branch": "main",
        "delete_branch_on_merge": delete_branch,
        "allow_merge_commit": True,
        "allow_squash_merge": True,
        "allow_rebase_merge": True,
        "license": {"spdx_id": spdx} if spdx else None,
        "security_and_analysis": {
            "secret_scanning": {"status": status},
            "secret_scanning_push_protection": {"status": status},
        },
    }


class FakeGithub:
    def __init__(
        self,
        repos,
        files=None,
        dirs=None,
        branches=None,
        two_fa=True,
        insecure=(),
        alerts=None,
        protected=True,
        keys=None,
        hooks=None,
        actions=None,
    ):
        self._repos = repos
        self._files = files or {}
        self._dirs = dirs or {}
        self._branches = branches or {}
        self._two_fa = two_fa
        self._insecure = list(insecure)
        self._alerts = alerts or {}
        self._protected = protected
        self._keys = keys or {}
        self._hooks = hooks or {}
        self._actions = (
            {"allowed_actions": "selected", "sha_pinning_required": True}
            if actions is None
            else actions
        )

    def org(self, org):
        return {"two_factor_requirement_enabled": self._two_fa}

    def org_actions_permissions(self, org):
        return self._actions

    def dependabot_alerts(self, full_name):
        return self._alerts.get(full_name, [])

    def branch_protected(self, full_name, branch):
        return self._protected

    def deploy_keys(self, full_name):
        return self._keys.get(full_name, [])

    def hooks(self, full_name):
        return self._hooks.get(full_name, [])

    def org_repos(self, org):
        return self._repos

    def file_text(self, full_name, path):
        return self._files.get((full_name, path))

    def dir_names(self, full_name, path):
        return self._dirs.get((full_name, path), [])

    def branches(self, full_name):
        return self._branches.get(full_name, ["main"])

    def insecure_two_factor_members(self, org):
        return self._insecure


def good_files(name):
    full = f"{ORG}/{name}"
    return (
        {
            (full, ".github/dependabot.yml"): "version: 2\n",
            (full, ".github/CODEOWNERS"): "* @Nain9Dev\n",
            (full, ".github/workflows/ci.yml"): "jobs:\n  gate:\n" + GOOD_PIN,
        },
        {(full, ".github/workflows"): ["ci.yml"]},
    )


def standard(**overrides):
    base = {
        "organisation": ORG,
        "max_topic_branches": 8,
        "severity": {},
        "exceptions": [],
    }
    base.update(overrides)
    return base


def clean_world(**kw):
    files, dirs = good_files("app")
    return FakeGithub([repo("app")], files, dirs, **kw)


def rules(findings, name="app"):
    return {f.rule for f in findings if f.repo == f"{ORG}/{name}"}


class Rules(unittest.TestCase):
    def test_a_repository_that_follows_the_standard_has_no_findings(self):
        self.assertEqual(audit.audit(clean_world(), standard(), NOW), [])

    def test_branch_deletion_after_merge_must_be_on(self):
        files, dirs = good_files("app")
        gh = FakeGithub([repo("app", delete_branch=False)], files, dirs)
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual(rules(findings), {"delete-branch-on-merge"})
        self.assertEqual(findings[0].severity, "error")

    def test_dependabot_is_an_error_when_the_repository_has_workflows(self):
        files, dirs = good_files("app")
        del files[(f"{ORG}/app", ".github/dependabot.yml")]
        findings = audit.audit(FakeGithub([repo("app")], files, dirs), standard(), NOW)
        self.assertEqual(rules(findings), {"dependabot-config"})
        self.assertEqual(findings[0].severity, "error")

    def test_codeowners_is_a_warning_and_any_of_the_three_places_counts(self):
        files, dirs = good_files("app")
        del files[(f"{ORG}/app", ".github/CODEOWNERS")]
        findings = audit.audit(FakeGithub([repo("app")], files, dirs), standard(), NOW)
        self.assertEqual(
            (rules(findings), findings[0].severity), ({"codeowners"}, "warning")
        )
        files[(f"{ORG}/app", "docs/CODEOWNERS")] = "* @x\n"
        self.assertEqual(
            audit.audit(FakeGithub([repo("app")], files, dirs), standard(), NOW), []
        )

    def test_a_public_repository_must_have_secret_scanning_and_push_protection(self):
        files, dirs = good_files("pub")
        gh = FakeGithub([repo("pub", private=False, scanning=False)], files, dirs)
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual(
            (rules(findings, "pub"), findings[0].severity),
            ({"secret-scanning"}, "warning"),
        )

    def test_a_private_repository_is_not_asked_for_paid_secret_scanning(self):
        files, dirs = good_files("app")
        gh = FakeGithub([repo("app", private=True, scanning=False)], files, dirs)
        self.assertEqual(audit.audit(gh, standard(), NOW), [])

    def test_a_public_repository_needs_a_license_github_recognises(self):
        files, dirs = good_files("pub")
        for spdx in ("NOASSERTION", None):
            with self.subTest(spdx=spdx):
                gh = FakeGithub([repo("pub", private=False, spdx=spdx)], files, dirs)
                findings = audit.audit(gh, standard(), NOW)
                self.assertEqual(
                    (rules(findings, "pub"), findings[0].severity),
                    ({"license"}, "error"),
                )

    def test_a_private_repository_may_have_no_license(self):
        files, dirs = good_files("app")
        gh = FakeGithub([repo("app", spdx=None)], files, dirs)
        self.assertEqual(audit.audit(gh, standard(), NOW), [])

    def test_a_repository_without_workflows_is_flagged(self):
        files, _ = good_files("app")
        files.pop((f"{ORG}/app", ".github/workflows/ci.yml"))
        gh = FakeGithub([repo("app")], files, {})
        self.assertIn("has-ci", rules(audit.audit(gh, standard(), NOW)))

    def test_the_shared_gate_pin_must_be_a_full_sha_with_a_version_comment(self):
        bad_pins = {
            "branch name": "@5b96639ee3e06da9f4ab7603c59fd818643ade6c # main\n",
            "no comment": "@150de5008682330747ba6880f1649b388858c206\n",
            "short sha": "@150de50 # v0.1.0\n",
            "moving tag": "@v0 # v0\n",
        }
        for label, pin in bad_pins.items():
            with self.subTest(label):
                files, dirs = good_files("app")
                files[(f"{ORG}/app", ".github/workflows/ci.yml")] = (
                    "    uses: naindev-contrast3d/ci-shared/.github/workflows/gate.yml"
                    + pin
                )
                findings = audit.audit(
                    FakeGithub([repo("app")], files, dirs), standard(), NOW
                )
                self.assertEqual(
                    (rules(findings), findings[0].severity), ({"gate-pin"}, "error")
                )

    def test_a_commented_reference_is_documentation_not_a_call(self):
        files, dirs = good_files("app")
        files[(f"{ORG}/app", ".github/workflows/ci.yml")] = (
            "# example:\n"
            "#   uses: naindev-contrast3d/ci-shared/.github/workflows/gate.yml@<full commit sha>\n"
            "jobs:\n  test:\n    runs-on: x\n"
        )
        findings = audit.audit(FakeGithub([repo("app")], files, dirs), standard(), NOW)
        self.assertEqual(rules(findings), {"shared-gate"})

    def test_a_ci_that_does_not_call_the_shared_gate_is_a_warning(self):
        files, dirs = good_files("app")
        files[(f"{ORG}/app", ".github/workflows/ci.yml")] = (
            "jobs:\n  test:\n    runs-on: x\n"
        )
        findings = audit.audit(FakeGithub([repo("app")], files, dirs), standard(), NOW)
        self.assertEqual(
            (rules(findings), findings[0].severity), ({"shared-gate"}, "warning")
        )

    def test_too_many_topic_branches_is_a_warning_with_the_count(self):
        files, dirs = good_files("app")
        names = ["main"] + [f"feat/{i}" for i in range(12)]
        gh = FakeGithub([repo("app")], files, dirs, branches={f"{ORG}/app": names})
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual(rules(findings), {"topic-branches"})
        self.assertIn("12", findings[0].message)

    def test_archived_repositories_are_skipped(self):
        gh = FakeGithub([repo("old", delete_branch=False, archived=True)])
        self.assertEqual(audit.audit(gh, standard(), NOW), [])

    def test_the_organisation_must_require_two_factor_authentication(self):
        findings = audit.audit(clean_world(two_fa=False), standard(), NOW)
        org_findings = [f for f in findings if f.repo == ORG]
        self.assertEqual(
            ([f.rule for f in org_findings], org_findings[0].severity),
            (["org-2fa"], "error"),
        )

    def test_members_with_an_insecure_second_factor_are_a_warning(self):
        findings = audit.audit(clean_world(insecure=["david"]), standard(), NOW)
        org_findings = [f for f in findings if f.repo == ORG]
        self.assertEqual(
            ([f.rule for f in org_findings], org_findings[0].severity),
            (["org-2fa-method"], "warning"),
        )
        self.assertIn("david", org_findings[0].message)


def alert(severity, days_old, patched="1.2.3", package="left-pad"):
    created = NOW - dt.timedelta(days=days_old)
    return {
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "dependency": {"package": {"name": package}},
        "security_advisory": {"severity": severity},
        "security_vulnerability": {
            "first_patched_version": {"identifier": patched} if patched else None
        },
    }


class SecurityRules(unittest.TestCase):
    def world(self, **kw):
        files, dirs = good_files("app")
        return FakeGithub([repo("app")], files, dirs, **kw)

    def test_disabled_dependabot_alerts_are_a_warning(self):
        gh = self.world(alerts={f"{ORG}/app": None})
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual(rules(findings), {"dependabot-alerts"})
        self.assertEqual(findings[0].severity, "warning")

    def test_an_alert_inside_its_deadline_is_not_a_finding(self):
        gh = self.world(alerts={f"{ORG}/app": [alert("critical", 6), alert("high", 29)]})
        self.assertEqual(audit.audit(gh, standard(), NOW), [])

    def test_an_alert_past_its_deadline_names_the_package_and_the_age(self):
        gh = self.world(alerts={f"{ORG}/app": [alert("critical", 9, package="happy-dom")]})
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual(rules(findings), {"dependabot-overdue"})
        self.assertIn("happy-dom", findings[0].message)
        self.assertIn("9 days", findings[0].message)

    def test_an_alert_without_a_patched_version_is_never_overdue(self):
        gh = self.world(alerts={f"{ORG}/app": [alert("high", 400, patched=None)]})
        self.assertEqual(audit.audit(gh, standard(), NOW), [])

    def test_the_deadlines_come_from_the_standard(self):
        gh = self.world(alerts={f"{ORG}/app": [alert("medium", 20)]})
        std = standard(dependency_deadline_days={"medium": 10})
        self.assertEqual(rules(audit.audit(gh, std, NOW)), {"dependabot-overdue"})

    def test_a_public_repository_must_protect_its_default_branch(self):
        files, dirs = good_files("lib")
        gh = FakeGithub([repo("lib", private=False)], files, dirs, protected=False)
        self.assertEqual(rules(audit.audit(gh, standard(), NOW), "lib"), {"branch-protection"})

    def test_a_private_repository_is_not_asked_for_paid_branch_protection(self):
        self.assertEqual(audit.audit(self.world(protected=False), standard(), NOW), [])

    def test_a_writable_deploy_key_is_listed_by_title_without_key_material(self):
        key = {"title": "release-tags", "read_only": False, "key": "ssh-ed25519 AAAA"}
        gh = self.world(keys={f"{ORG}/app": [key, {"title": "ro", "read_only": True}]})
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual(rules(findings), {"deploy-key-write"})
        self.assertIn("release-tags", findings[0].message)
        self.assertNotIn("AAAA", findings[0].message)
        self.assertEqual(len(findings), 1)

    def test_a_webhook_without_a_secret_is_an_observation(self):
        hook = {"active": True, "config": {"url": "https://discord.com/api/webhooks/x"}}
        gh = self.world(hooks={f"{ORG}/app": [hook]})
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual(rules(findings), {"webhook-secret"})
        self.assertEqual(findings[0].severity, "info")
        self.assertNotIn("discord.com/api", findings[0].message)

    def test_a_webhook_with_a_secret_is_fine(self):
        hook = {"active": True, "config": {"url": "https://x.example/h", "secret": "********"}}
        self.assertEqual(
            audit.audit(self.world(hooks={f"{ORG}/app": [hook]}), standard(), NOW), []
        )

    def test_actions_must_be_restricted_and_pinned_by_sha(self):
        gh = self.world(actions={"allowed_actions": "all", "sha_pinning_required": False})
        findings = audit.audit(gh, standard(), NOW)
        self.assertEqual({f.rule for f in findings}, {"actions-allowlist"})
        self.assertEqual(len(findings), 1)

    def test_unreadable_actions_permissions_are_skipped(self):
        self.assertEqual(audit.audit(self.world(actions={}), standard(), NOW), [])


class Exceptions(unittest.TestCase):
    def finding_world(self):
        files, dirs = good_files("app")
        return FakeGithub([repo("app", delete_branch=False)], files, dirs)

    def exception(self, expires):
        return {
            "repo": f"{ORG}/app",
            "rule": "delete-branch-on-merge",
            "reason": "kept on purpose",
            "expires": expires,
        }

    def test_a_valid_exception_silences_the_rule_for_that_repository(self):
        std = standard(exceptions=[self.exception("2027-01-01")])
        self.assertEqual(audit.audit(self.finding_world(), std, NOW), [])

    def test_an_expired_exception_stops_applying_and_is_itself_a_finding(self):
        std = standard(exceptions=[self.exception("2026-10-01")])
        found = {f.rule: f for f in audit.audit(self.finding_world(), std, NOW)}
        self.assertEqual(set(found), {"delete-branch-on-merge", "exception-expired"})
        self.assertEqual(found["exception-expired"].severity, "error")

    def test_an_exception_for_another_repository_or_rule_changes_nothing(self):
        other = dict(self.exception("2027-01-01"), repo=f"{ORG}/elsewhere")
        std = standard(
            exceptions=[other, dict(self.exception("2027-01-01"), rule="codeowners")]
        )
        self.assertEqual(
            rules(audit.audit(self.finding_world(), std, NOW)),
            {"delete-branch-on-merge"},
        )

    def test_an_exception_without_reason_or_date_is_rejected(self):
        for bad in (
            {"repo": "a/b", "rule": "x"},
            {"repo": "a/b", "rule": "x", "reason": "r"},
        ):
            with self.subTest(bad=bad), self.assertRaises(audit.StandardError):
                audit.validate_standard(standard(exceptions=[bad]))


class Severity(unittest.TestCase):
    def test_the_standard_can_change_the_severity_of_a_rule(self):
        files, dirs = good_files("app")
        gh = FakeGithub([repo("app", delete_branch=False)], files, dirs)
        findings = audit.audit(
            gh, standard(severity={"delete-branch-on-merge": "warning"}), NOW
        )
        self.assertEqual(findings[0].severity, "warning")

    def test_an_unknown_severity_is_rejected(self):
        with self.assertRaises(audit.StandardError):
            audit.validate_standard(standard(severity={"codeowners": "fatal"}))

    def test_the_exit_code_follows_fail_on(self):
        warning = [audit.Finding("o/a", "codeowners", "warning", "m")]
        error = [audit.Finding("o/a", "license", "error", "m")]
        self.assertEqual(audit.exit_code(warning, "error"), 0)
        self.assertEqual(audit.exit_code(error, "error"), 1)
        self.assertEqual(audit.exit_code(warning, "warning"), 1)
        self.assertEqual(audit.exit_code([], "warning"), 0)
        self.assertEqual(audit.exit_code(error, None), 0)


class Rendering(unittest.TestCase):
    def test_findings_are_grouped_by_repository_and_ordered_by_severity(self):
        findings = [
            audit.Finding("o/b", "codeowners", "warning", "no CODEOWNERS"),
            audit.Finding("o/a", "license", "error", "license not recognised"),
            audit.Finding("o/a", "codeowners", "warning", "no CODEOWNERS"),
        ]
        text = audit.render(findings, NOW)
        self.assertLess(text.index("### o/a"), text.index("### o/b"))
        self.assertLess(
            text.index("license not recognised"), text.index("no CODEOWNERS")
        )
        self.assertIn("- error `license`: license not recognised", text)

    def test_no_findings_says_so(self):
        self.assertIn("No deviations", audit.render([], NOW))

    def test_the_summary_counts_by_severity(self):
        findings = [
            audit.Finding("o/a", "license", "error", "m"),
            audit.Finding("o/a", "codeowners", "warning", "m"),
            audit.Finding("o/b", "codeowners", "warning", "m"),
        ]
        self.assertIn("1 error, 2 warnings", audit.render(findings, NOW))


class RealCommands(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.outputs = {}
        self.errors = {}
        self.gh = audit.Github(self.fake_run)

    def fake_run(self, command):
        self.commands.append(command)
        key = " ".join(command)
        for needle, error in self.errors.items():
            if needle in key:
                raise subprocess.CalledProcessError(1, command, stderr=error)
        for needle, output in self.outputs.items():
            if needle in key:
                return output
        return "[]"

    def test_a_missing_file_reads_as_none(self):
        self.errors["contents/.github/CODEOWNERS"] = "HTTP 404"
        self.assertIsNone(self.gh.file_text(f"{ORG}/app", ".github/CODEOWNERS"))

    def test_a_file_is_read_through_the_raw_contents_media_type(self):
        self.outputs["contents/.github/dependabot.yml"] = "version: 2\n"
        self.assertEqual(
            self.gh.file_text(f"{ORG}/app", ".github/dependabot.yml"), "version: 2\n"
        )
        self.assertIn("application/vnd.github.raw+json", " ".join(self.commands[0]))

    def test_a_missing_directory_is_empty(self):
        self.errors["contents/.github/workflows"] = "HTTP 404"
        self.assertEqual(self.gh.dir_names(f"{ORG}/app", ".github/workflows"), [])

    def test_the_repositories_are_listed_with_the_fields_the_rules_read(self):
        self.outputs["orgs/" + ORG + "/repos"] = json.dumps([repo("app")])
        self.assertEqual(self.gh.org_repos(ORG)[0]["name"], "app")

    def test_insecure_two_factor_members_are_listed_by_login(self):
        self.outputs["2fa_insecure"] = json.dumps([{"login": "david"}])
        self.assertEqual(self.gh.insecure_two_factor_members(ORG), ["david"])

    def test_disabled_dependabot_alerts_read_as_none(self):
        self.errors["dependabot/alerts"] = "Dependabot alerts are disabled"
        self.assertIsNone(self.gh.dependabot_alerts(f"{ORG}/app"))

    def test_open_alerts_are_flattened_across_pages(self):
        self.outputs["dependabot/alerts"] = json.dumps([[{"a": 1}], [{"a": 2}]])
        self.assertEqual(self.gh.dependabot_alerts(f"{ORG}/app"), [{"a": 1}, {"a": 2}])
        self.assertIn("state=open", " ".join(self.commands[0]))

    def test_an_unprotected_branch_reads_as_false(self):
        self.errors["branches/main/protection"] = "Branch not protected"
        self.assertFalse(self.gh.branch_protected(f"{ORG}/app", "main"))

    def test_a_protected_branch_reads_as_true(self):
        self.outputs["branches/main/protection"] = "{}"
        self.assertTrue(self.gh.branch_protected(f"{ORG}/app", "main"))

    def test_keys_and_hooks_that_cannot_be_read_are_empty(self):
        self.errors["/keys"] = "HTTP 403"
        self.errors["/hooks"] = "HTTP 403"
        self.assertEqual(self.gh.deploy_keys(f"{ORG}/app"), [])
        self.assertEqual(self.gh.hooks(f"{ORG}/app"), [])

    def test_actions_permissions_that_cannot_be_read_are_empty(self):
        self.errors["actions/permissions"] = "HTTP 403"
        self.assertEqual(self.gh.org_actions_permissions(ORG), {})


if __name__ == "__main__":
    unittest.main()
