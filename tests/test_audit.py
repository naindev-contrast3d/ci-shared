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
        self, repos, files=None, dirs=None, branches=None, two_fa=True, insecure=()
    ):
        self._repos = repos
        self._files = files or {}
        self._dirs = dirs or {}
        self._branches = branches or {}
        self._two_fa = two_fa
        self._insecure = list(insecure)

    def org(self, org):
        return {"two_factor_requirement_enabled": self._two_fa}

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


if __name__ == "__main__":
    unittest.main()
