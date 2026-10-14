"""Apply the organisation's repository settings to a new repository.

    py -3 bootstrap.py owner/name            # show what would change (dry run, the default)
    py -3 bootstrap.py owner/name --apply    # make the changes

The settings come from the standards audit (audit.py): branch deletion after merge, secret
scanning and push protection where they are free (public repositories), Dependabot alerts and
security updates, and the two labels the merge train uses. Nothing is changed without --apply, and a
setting that already follows the standard is left alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

LABELS = {
    "automerge": (
        "0E8A16",
        "The merge train merges this pull request when its checks pass",
    ),
    "needs-human": (
        "D93F0B",
        "A person must resolve this before the merge train retries",
    ),
}
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class BootstrapError(ValueError):
    """The repository cannot be bootstrapped."""


@dataclass(frozen=True)
class Change:
    setting: str
    current: str
    wanted: str


def run_gh(command: list[str]) -> str:
    # Every command is built in this file from fixed words and the repository name given.
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
    def __init__(self, run: Callable[[list[str]], str] = run_gh):
        self._run = run

    def repo(self, full_name: str) -> dict:
        return json.loads(self._run(["gh", "api", f"repos/{full_name}"]) or "{}")

    def labels(self, full_name: str) -> list[str]:
        out = self._run(
            [
                "gh",
                "api",
                f"repos/{full_name}/labels?per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        listed = json.loads(out or "[]")
        if listed and isinstance(listed[0], list):
            listed = [item for page in listed for item in page]
        return [item["name"] for item in listed]

    def alerts_enabled(self, full_name: str) -> bool:
        try:
            self._run(["gh", "api", f"repos/{full_name}/vulnerability-alerts"])
        except subprocess.CalledProcessError:
            return False
        return True

    def security_fixes_enabled(self, full_name: str) -> bool:
        try:
            out = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{full_name}/automated-security-fixes",
                    "--jq",
                    ".enabled",
                ]
            )
        except subprocess.CalledProcessError:
            return False
        return out.strip() == "true"

    def patch_repo(self, full_name: str, fields: dict) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(fields, handle)
            path = handle.name
        try:
            self._run(
                ["gh", "api", "-X", "PATCH", f"repos/{full_name}", "--input", path]
            )
        finally:
            os.unlink(path)

    def create_label(
        self, full_name: str, name: str, color: str, description: str
    ) -> None:
        self._run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{full_name}/labels",
                "-f",
                f"name={name}",
                "-f",
                f"color={color}",
                "-f",
                f"description={description}",
            ]
        )

    def enable_alerts(self, full_name: str) -> None:
        self._run(["gh", "api", "-X", "PUT", f"repos/{full_name}/vulnerability-alerts"])

    def enable_security_fixes(self, full_name: str) -> None:
        self._run(
            ["gh", "api", "-X", "PUT", f"repos/{full_name}/automated-security-fixes"]
        )


def plan(gh, full_name: str) -> list[Change]:
    repo = gh.repo(full_name)
    if repo.get("archived"):
        raise BootstrapError(f"{full_name} is archived")
    changes: list[Change] = []
    if not repo.get("delete_branch_on_merge"):
        changes.append(Change("delete_branch_on_merge", "off", "on"))
    if not repo.get("private", True):
        security = repo.get("security_and_analysis") or {}
        for key in ("secret_scanning", "secret_scanning_push_protection"):
            if (security.get(key) or {}).get("status") != "enabled":
                changes.append(Change(key, "off", "on"))
    existing = set(gh.labels(full_name))
    for name in LABELS:
        if name not in existing:
            changes.append(Change(f"label:{name}", "missing", "created"))
    if not gh.alerts_enabled(full_name):
        changes.append(Change("vulnerability_alerts", "off", "on"))
    if not gh.security_fixes_enabled(full_name):
        changes.append(Change("automated_security_fixes", "off", "on"))
    return changes


def apply(gh, full_name: str, changes: list[Change], dry_run: bool) -> None:
    if dry_run:
        return
    fields: dict = {}
    security: dict = {}
    for change in changes:
        if change.setting == "delete_branch_on_merge":
            fields["delete_branch_on_merge"] = True
        elif change.setting in ("secret_scanning", "secret_scanning_push_protection"):
            security[change.setting] = {"status": "enabled"}
    if security:
        fields["security_and_analysis"] = security
    if fields:
        gh.patch_repo(full_name, fields)
    for change in changes:
        if change.setting.startswith("label:"):
            name = change.setting.removeprefix("label:")
            color, description = LABELS[name]
            gh.create_label(full_name, name, color, description)
        elif change.setting == "vulnerability_alerts":
            gh.enable_alerts(full_name)
        elif change.setting == "automated_security_fixes":
            gh.enable_security_fixes(full_name)


def render(changes: list[Change], full_name: str, dry_run: bool) -> str:
    mode = "dry run: nothing changed" if dry_run else "applied"
    lines = [f"# Bootstrap of {full_name} ({mode})", ""]
    if not changes:
        lines.append(f"{full_name} already follows the standard.")
    for change in changes:
        lines.append(f"- {change.setting}: {change.current} -> {change.wanted}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument(
        "--apply", action="store_true", help="make the changes (default: dry run)"
    )
    args = parser.parse_args(argv)
    if not REPO_PATTERN.match(args.repo):
        print("repo must look like owner/name", file=sys.stderr)
        return 2
    gh = Github()
    try:
        changes = plan(gh, args.repo)
    except BootstrapError as exc:
        print(f"bootstrap error: {exc}", file=sys.stderr)
        return 2
    apply(gh, args.repo, changes, dry_run=not args.apply)
    print(render(changes, args.repo, dry_run=not args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
