"""Audit the repositories of an organisation against a written standard.

    py -3 audit.py                          # print the deviations
    py -3 audit.py --fail-on error          # exit 1 when there is an error (for a CI step)

The standard is `standards.json` beside this script: thresholds, the severity of each rule and the
exceptions. An exception names a repository and a rule, says why, and expires; an expired exception
stops silencing the rule and is itself an error, so "on purpose" cannot turn into "forgotten".
Severities: `error` breaks a guarantee, `warning` is hygiene, `info` is an observation.

Rules (per repository unless noted): delete-branch-on-merge, dependabot-config, codeowners,
secret-scanning (public repositories, where it is free), license (public repositories),
has-ci, shared-gate, gate-pin (the shared gate is pinned by a full commit SHA with a vX.Y.Z comment,
which is what online zizmor verifies), topic-branches, dependabot-alerts (off) and
dependabot-overdue (an open alert with a patched version past its deadline, 7/30/90 days for
critical/high/medium, `dependency_deadline_days` overrides), deploy-key-write, webhook-secret,
branch-protection (public repositories, where it is free), and for the organisation org-2fa,
org-2fa-method and actions-allowlist (actions restricted to a list and pinned by SHA). The audit
only reports: deploy keys, webhooks and two-factor are the owner's to change. Archived
repositories are skipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SEVERITIES = ("error", "warning", "info")
GATE_REFERENCE = "naindev-contrast3d/ci-shared/.github/workflows/"
GOOD_PIN = re.compile(r"@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+\b")

DEFAULT_SEVERITY = {
    "delete-branch-on-merge": "error",
    "dependabot-config": "error",
    "license": "error",
    "gate-pin": "error",
    "org-2fa": "error",
    "exception-expired": "error",
    "codeowners": "warning",
    "secret-scanning": "warning",
    "has-ci": "warning",
    "shared-gate": "warning",
    "topic-branches": "warning",
    "org-2fa-method": "warning",
    "dependabot-alerts": "warning",
    "dependabot-overdue": "warning",
    "branch-protection": "warning",
    "deploy-key-write": "warning",
    "webhook-secret": "info",
    "actions-allowlist": "warning",
}

# Days an open alert with a patched version may stay open, by severity (spec org-security-baseline
# FR1). `dependency_deadline_days` in the standard overrides it; a missing severity has no deadline.
DEFAULT_DEADLINE_DAYS = {"critical": 7, "high": 30, "medium": 90}


class StandardError(ValueError):
    """The standard file cannot be used."""


@dataclass(frozen=True)
class Finding:
    repo: str
    rule: str
    severity: str
    message: str


def run_gh(command: list[str]) -> str:
    # Every command is built in this file from fixed words and names GitHub returned.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        creationflags=no_window,
    ).stdout


class Github:
    """The reads the audit needs, over the `gh` command line."""

    def __init__(self, run: Callable[[list[str]], str] = run_gh):
        self._run = run

    def org(self, org: str) -> dict:
        return json.loads(self._run(["gh", "api", f"orgs/{org}"]) or "{}")

    def org_repos(self, org: str) -> list[dict]:
        out = self._run(
            [
                "gh",
                "api",
                f"orgs/{org}/repos?per_page=100&type=all",
                "--paginate",
                "--slurp",
            ]
        )
        listed = json.loads(out or "[]")
        if listed and isinstance(listed[0], list):  # --slurp returns one array per page
            listed = [item for page in listed for item in page]
        repos = []
        for entry in listed:
            # The list endpoint omits settings such as delete_branch_on_merge: read each one.
            detail = json.loads(
                self._run(["gh", "api", f"repos/{org}/{entry['name']}"]) or "{}"
            )
            repos.append(detail if isinstance(detail, dict) and detail else entry)
        return repos

    def file_text(self, full_name: str, path: str) -> str | None:
        try:
            return self._run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github.raw+json",
                    f"repos/{full_name}/contents/{path}",
                ]
            )
        except subprocess.CalledProcessError:
            return None

    def dir_names(self, full_name: str, path: str) -> list[str]:
        try:
            out = self._run(["gh", "api", f"repos/{full_name}/contents/{path}"])
        except subprocess.CalledProcessError:
            return []
        listing = json.loads(out or "[]")
        return [item["name"] for item in listing] if isinstance(listing, list) else []

    def branches(self, full_name: str) -> list[str]:
        out = self._run(
            [
                "gh",
                "api",
                f"repos/{full_name}/branches?per_page=100",
                "--paginate",
                "--jq",
                ".[].name",
            ]
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    def insecure_two_factor_members(self, org: str) -> list[str]:
        try:
            out = self._run(["gh", "api", f"orgs/{org}/members?filter=2fa_insecure"])
        except subprocess.CalledProcessError:
            return []  # needs organisation owner rights
        return [member["login"] for member in json.loads(out or "[]")]

    def org_actions_permissions(self, org: str) -> dict:
        try:
            return json.loads(self._run(["gh", "api", f"orgs/{org}/actions/permissions"]) or "{}")
        except subprocess.CalledProcessError:
            return {}  # needs organisation owner rights

    def dependabot_alerts(self, full_name: str) -> list[dict] | None:
        """Open alerts, or None when Dependabot alerts are disabled or cannot be read."""
        try:
            out = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{full_name}/dependabot/alerts?state=open&per_page=100",
                    "--paginate",
                    "--slurp",
                ]
            )
        except subprocess.CalledProcessError:
            return None
        pages = json.loads(out or "[]")
        return [item for page in pages for item in page] if pages and isinstance(pages[0], list) else pages

    def branch_protected(self, full_name: str, branch: str) -> bool:
        try:
            self._run(["gh", "api", f"repos/{full_name}/branches/{branch}/protection"])
        except subprocess.CalledProcessError:
            return False
        return True

    def deploy_keys(self, full_name: str) -> list[dict]:
        try:
            return json.loads(self._run(["gh", "api", f"repos/{full_name}/keys"]) or "[]")
        except subprocess.CalledProcessError:
            return []

    def hooks(self, full_name: str) -> list[dict]:
        try:
            return json.loads(self._run(["gh", "api", f"repos/{full_name}/hooks"]) or "[]")
        except subprocess.CalledProcessError:
            return []


def validate_standard(standard: dict) -> None:
    for rule, severity in standard.get("severity", {}).items():
        if severity not in SEVERITIES:
            raise StandardError(
                f"severity of {rule} must be one of {', '.join(SEVERITIES)}"
            )
    for item in standard.get("exceptions", []):
        for key in ("repo", "rule", "reason", "expires"):
            if not item.get(key):
                raise StandardError(f"every exception needs `{key}`: {item}")
        try:
            dt.date.fromisoformat(item["expires"])
        except ValueError as exc:
            raise StandardError(
                f"`expires` must be a date (YYYY-MM-DD): {item}"
            ) from exc


def audit(gh, standard: dict, now: dt.datetime) -> list[Finding]:
    validate_standard(standard)
    org = standard["organisation"]
    today = now.date()
    severity = {**DEFAULT_SEVERITY, **standard.get("severity", {})}
    exceptions = standard.get("exceptions", [])
    active = {
        (item["repo"], item["rule"])
        for item in exceptions
        if dt.date.fromisoformat(item["expires"]) >= today
    }
    findings: list[Finding] = []

    def add(repo: str, rule: str, message: str, level: str | None = None) -> None:
        if (repo, rule) in active:
            return
        findings.append(Finding(repo, rule, level or severity[rule], message))

    for item in exceptions:
        if dt.date.fromisoformat(item["expires"]) < today:
            findings.append(
                Finding(
                    item["repo"],
                    "exception-expired",
                    severity["exception-expired"],
                    f"the exception for `{item['rule']}` expired on {item['expires']} "
                    f"({item['reason']}); decide again",
                )
            )

    info = gh.org(org)
    if not info.get("two_factor_requirement_enabled"):
        add(
            org,
            "org-2fa",
            "the organisation does not require two-factor authentication",
        )
    insecure = gh.insecure_two_factor_members(org)
    if insecure:
        add(
            org,
            "org-2fa-method",
            "members with an insecure second factor: " + ", ".join(insecure),
        )
    permissions = gh.org_actions_permissions(org)
    if permissions and (
        permissions.get("allowed_actions") != "selected"
        or not permissions.get("sha_pinning_required")
    ):
        add(
            org,
            "actions-allowlist",
            "actions are not restricted to a list or not pinned by SHA "
            f"(allowed_actions={permissions.get('allowed_actions')}, "
            f"sha_pinning_required={bool(permissions.get('sha_pinning_required'))})",
        )

    for repo in gh.org_repos(org):
        if repo.get("archived"):
            continue
        full = repo["full_name"]
        public = not repo.get("private", True)
        _audit_repo(gh, standard, repo, full, public, now, add)
    return findings


def _audit_security(gh, standard: dict, full: str, now: dt.datetime, add) -> None:
    deadlines = {**DEFAULT_DEADLINE_DAYS, **standard.get("dependency_deadline_days", {})}
    alerts = gh.dependabot_alerts(full)
    if alerts is None:
        add(full, "dependabot-alerts", "Dependabot alerts are disabled or cannot be read")
    for item in alerts or []:
        patched = (item.get("security_vulnerability") or {}).get("first_patched_version")
        level = (item.get("security_advisory") or {}).get("severity")
        if not patched or level not in deadlines:
            continue  # nothing to upgrade to yet, or a severity without a deadline
        opened = dt.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        age = (now - opened).days
        if age > deadlines[level]:
            package = ((item.get("dependency") or {}).get("package") or {}).get("name", "?")
            add(
                full,
                "dependabot-overdue",
                f"{level} alert on `{package}` open {age} days, a fix exists "
                f"(deadline {deadlines[level]} days)",
            )
    for key in gh.deploy_keys(full):
        if not key.get("read_only", True):
            add(full, "deploy-key-write", f"deploy key `{key.get('title', '?')}` can write")
    for hook in gh.hooks(full):
        if hook.get("active") and not (hook.get("config") or {}).get("secret"):
            add(full, "webhook-secret", "an active webhook has no secret (the URL is the credential)")


def _audit_repo(
    gh, standard: dict, repo: dict, full: str, public: bool, now: dt.datetime, add
) -> None:
    if not repo.get("delete_branch_on_merge"):
        add(
            full,
            "delete-branch-on-merge",
            "merged branches are not deleted automatically",
        )

    workflow_names = [
        name
        for name in gh.dir_names(full, ".github/workflows")
        if name.endswith((".yml", ".yaml"))
    ]
    if not workflow_names:
        add(full, "has-ci", "no GitHub Actions workflow")

    has_dependabot = any(
        gh.file_text(full, f".github/dependabot.{ext}") is not None
        for ext in ("yml", "yaml")
    )
    if not has_dependabot:
        add(
            full,
            "dependabot-config",
            "no .github/dependabot.yml",
            "error" if workflow_names else "warning",
        )

    if not any(
        gh.file_text(full, path) is not None
        for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
    ):
        add(full, "codeowners", "no CODEOWNERS file")

    if public:
        security = repo.get("security_and_analysis") or {}
        scanning = (security.get("secret_scanning") or {}).get("status") == "enabled"
        protection = (security.get("secret_scanning_push_protection") or {}).get(
            "status"
        ) == "enabled"
        if not (scanning and protection):
            add(
                full,
                "secret-scanning",
                "secret scanning or push protection is off (free on public repositories)",
            )
        spdx = (repo.get("license") or {}).get("spdx_id")
        if spdx in (None, "NOASSERTION"):
            add(full, "license", "GitHub does not recognise the license")
        branch = repo.get("default_branch", "main")
        if not gh.branch_protected(full, branch):
            add(
                full,
                "branch-protection",
                f"`{branch}` is not protected (free on public repositories)",
            )

    _audit_security(gh, standard, full, now, add)
    _audit_gate(gh, workflow_names, full, add)

    limit = standard.get("max_topic_branches", 8)
    topics = [b for b in gh.branches(full) if b != repo.get("default_branch", "main")]
    if len(topics) > limit:
        add(
            full,
            "topic-branches",
            f"{len(topics)} branches besides the default one (limit {limit})",
        )


def _audit_gate(gh, workflow_names: list[str], full: str, add) -> None:
    if not workflow_names:
        return
    references = []
    for name in workflow_names:
        text = gh.file_text(full, f".github/workflows/{name}") or ""
        references += [
            line
            for line in text.splitlines()
            if GATE_REFERENCE in line
            and "uses:" in line
            and not line.lstrip().startswith("#")
        ]
    if not references:
        add(full, "shared-gate", "no workflow calls the shared gate of ci-shared")
        return
    if not all(GOOD_PIN.search(line) for line in references):
        add(
            full,
            "gate-pin",
            "the shared workflow must be pinned by a full commit SHA with a `# vX.Y.Z` comment",
        )


def exit_code(findings: list[Finding], fail_on: str | None) -> int:
    if fail_on is None:
        return 0
    levels = {"error": ("error",), "warning": ("error", "warning")}[fail_on]
    return 1 if any(f.severity in levels for f in findings) else 0


def render(findings: list[Finding], now: dt.datetime) -> str:
    counts = {
        level: sum(1 for f in findings if f.severity == level) for level in SEVERITIES
    }
    parts = [f"{counts['error']} error{'s' if counts['error'] != 1 else ''}"]
    parts.append(f"{counts['warning']} warning{'s' if counts['warning'] != 1 else ''}")
    if counts["info"]:
        parts.append(f"{counts['info']} info")
    lines = [
        "# Standards audit",
        "",
        f"{now.strftime('%Y-%m-%d')}: {', '.join(parts)}.",
        "",
    ]
    if not findings:
        lines.append("No deviations from the standard.")
        return "\n".join(lines)
    order = {level: index for index, level in enumerate(SEVERITIES)}
    for repo in sorted({f.repo for f in findings}):
        lines.append(f"### {repo}")
        for f in sorted(
            (x for x in findings if x.repo == repo),
            key=lambda x: (order[x.severity], x.rule),
        ):
            lines.append(f"- {f.severity} `{f.rule}`: {f.message}")
        lines.append("")
    return "\n".join(lines)


def run(
    standard_path: Path | str, gh=None, now: dt.datetime | None = None
) -> list[Finding]:
    standard = json.loads(Path(standard_path).read_text(encoding="utf-8"))
    return audit(gh or Github(), standard, now or dt.datetime.now(dt.UTC))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--standard", default=str(Path(__file__).with_name("standards.json"))
    )
    parser.add_argument(
        "--fail-on", choices=("error", "warning"), help="exit 1 from this severity"
    )
    parser.add_argument("--output", help="write the report here instead of printing it")
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    try:
        findings = run(args.standard, now=now)
    except StandardError as exc:
        print(f"standard error: {exc}", file=sys.stderr)
        return 2
    text = render(findings, now)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return exit_code(findings, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
