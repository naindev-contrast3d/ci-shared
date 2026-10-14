"""Merge sealed pull requests into `main` for every repository of a list (the merge train).

    py -3 automerge.py --config repos.json            # one cycle over every repository
    py -3 automerge.py --config repos.json --dry-run  # say what would happen, change nothing

GitHub's auto-merge needs branch protection, which free private repositories do not have, so the
desktop does it: a scheduled task runs this every two minutes with the owner's `gh` login. Per
repository and cycle, at most one pull request is merged. A pull request is merged only when all of
this holds, and anything unclear is skipped, never merged:

- open, into the base branch, not a draft, labelled `automerge`;
- GitHub says it is mergeable (no conflicts);
- the repository's gate check is green on its head commit, and, if `require_seal` is set, its head
  carries the `ci-ok/<sha>` seal;
- its head contains the base branch. A sealed head that misses commits of the base was never tested
  together with them, so the train merges the base into the branch through the API (never a force
  push), stops for that cycle, and merges the new head once CI is green again. One stale pull
  request is updated per repository and cycle, and only when no other one is ready to merge.

A conflict (found by GitHub or while updating the branch) is flagged: the pull request gets the
`needs-human` label and loses `automerge`, so the train does not try again until the owner resolves
it and labels it again. The merge pins the exact head (`--match-head-commit`), so a push after the
checks cannot slip in. The configuration is a JSON file, see `repos.example.json`.
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

PR_FIELDS = "number,headRefOid,baseRefName,isDraft,mergeable,labels"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """The configuration file cannot be used."""


@dataclass(frozen=True)
class Settings:
    label: str = "automerge"
    needs_human_label: str = "needs-human"


@dataclass(frozen=True)
class RepoConfig:
    repo: str
    gate_check: str = "Gate / Gate"
    require_seal: bool = False
    base: str = "main"


def load_config(path: Path | str) -> tuple[Settings, list[RepoConfig]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a JSON object")

    defaults = Settings()
    settings = Settings(
        label=_text(data, "label", defaults.label),
        needs_human_label=_text(data, "needs_human_label", defaults.needs_human_label),
    )
    entries = data.get("repos")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("`repos` must be a non-empty list")

    repos = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("every entry of `repos` must be an object")
        repo = entry.get("repo")
        if not isinstance(repo, str) or not REPO_PATTERN.match(repo):
            raise ConfigError(f"`repo` must look like owner/name, got {repo!r}")
        require_seal = entry.get("require_seal", False)
        if not isinstance(require_seal, bool):
            raise ConfigError(f"{repo}: `require_seal` must be true or false")
        repos.append(
            RepoConfig(
                repo=repo,
                gate_check=_text(entry, "gate_check", RepoConfig.gate_check),
                require_seal=require_seal,
                base=_text(entry, "base", RepoConfig.base),
            )
        )
    return settings, repos


def _text(source: dict, key: str, default: str) -> str:
    value = source.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"`{key}` must be a non-empty string")
    return value


def run_gh(command: list[str]) -> str:
    # Every command is built in this file from fixed words and GitHub's own numbers and shas.
    # No console window for `gh`: the task runs every two minutes on the owner's desktop.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command, check=True, capture_output=True, text=True, creationflags=no_window
    ).stdout


class Github:
    """The few GitHub operations the train needs, over the `gh` command line."""

    def __init__(self, run: Callable[[list[str]], str] = run_gh):
        self._run = run

    def open_prs(self, repo: str, label: str) -> list[dict]:
        out = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--label",
                label,
                "--json",
                PR_FIELDS,
                "--limit",
                "50",
            ]
        )
        return json.loads(out or "[]")

    def gate_green(self, repo: str, sha: str, check_name: str) -> bool:
        out = self._run(
            ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"]
        )
        runs = [
            r
            for r in json.loads(out or "{}").get("check_runs", [])
            if r.get("name") == check_name
        ]
        return bool(runs) and all(
            r.get("status") == "completed" and r.get("conclusion") == "success"
            for r in runs
        )

    def sealed(self, repo: str, sha: str) -> bool:
        try:
            self._run(["gh", "api", f"repos/{repo}/git/ref/tags/ci-ok/{sha}"])
        except subprocess.CalledProcessError:
            return False
        return True

    def behind_by(self, repo: str, base: str, sha: str) -> int:
        out = self._run(["gh", "api", f"repos/{repo}/compare/{base}...{sha}"])
        return int(json.loads(out)["behind_by"])

    def merge(self, repo: str, number: int, sha: str) -> None:
        self._run(
            [
                "gh",
                "pr",
                "merge",
                str(number),
                "--repo",
                repo,
                "--merge",
                "--match-head-commit",
                sha,
            ]
        )

    def update_branch(self, repo: str, number: int, sha: str) -> str:
        try:
            self._run(
                [
                    "gh",
                    "api",
                    "-X",
                    "PUT",
                    f"repos/{repo}/pulls/{number}/update-branch",
                    "-f",
                    f"expected_head_sha={sha}",
                ]
            )
        except subprocess.CalledProcessError as exc:
            if "conflict" in (exc.stderr or "").lower():
                return "conflict"
            raise
        return "updated"

    def flag_needs_human(
        self, repo: str, number: int, label: str, needs_label: str
    ) -> None:
        try:
            self._run(
                [
                    "gh",
                    "label",
                    "create",
                    needs_label,
                    "--repo",
                    repo,
                    "--color",
                    "D93F0B",
                    "--description",
                    "A person must resolve this before the merge train retries",
                ]
            )
        except subprocess.CalledProcessError:
            pass  # most likely the label exists already; the edit below fails loudly if not
        self._run(
            [
                "gh",
                "pr",
                "edit",
                str(number),
                "--repo",
                repo,
                "--add-label",
                needs_label,
                "--remove-label",
                label,
            ]
        )


def classify(pr: dict, cfg: RepoConfig, settings: Settings, gh) -> tuple[str, str]:
    """(state, reason) of one pull request: ready, stale, conflict or skip."""
    if settings.label not in {item.get("name") for item in pr.get("labels", [])}:
        return "skip", "no automerge label"
    if pr.get("isDraft"):
        return "skip", "draft"
    if pr.get("baseRefName") != cfg.base:
        return "skip", f"not into {cfg.base}"
    mergeable = pr.get("mergeable")
    if mergeable == "CONFLICTING":
        return "conflict", "conflicts with the base branch"
    if mergeable != "MERGEABLE":
        return "skip", f"mergeability is {mergeable}"

    sha = pr["headRefOid"]
    if not gh.gate_green(cfg.repo, sha, cfg.gate_check):
        return "skip", f"gate check '{cfg.gate_check}' is not green on its head"
    if cfg.require_seal and not gh.sealed(cfg.repo, sha):
        return "skip", "no ci-ok seal on its head"
    behind = gh.behind_by(cfg.repo, cfg.base, sha)
    if behind > 0:
        return "stale", f"head misses {behind} commit(s) of {cfg.base}"
    return "ready", "ready"


def run_cycle(
    gh,
    configs: list[RepoConfig],
    settings: Settings,
    *,
    dry_run: bool,
    log: Callable[[str], None],
) -> int:
    """One pass over every repository. Returns 0 when nothing went wrong, 1 otherwise."""
    code = 0
    for cfg in configs:
        try:
            _repo_cycle(
                gh, cfg, settings, dry_run, lambda line, r=cfg.repo: log(f"{r} {line}")
            )
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
            detail = (getattr(exc, "stderr", "") or str(exc)).strip()[:300]
            log(f"{cfg.repo}: failed: {detail}")
            code = 1
    return code


def _repo_cycle(gh, cfg: RepoConfig, settings: Settings, dry_run: bool, log) -> None:
    prs = gh.open_prs(cfg.repo, settings.label)
    if not prs:
        return
    states = {}
    for pr in sorted(prs, key=lambda p: p["number"]):
        states[pr["number"]] = (pr, *classify(pr, cfg, settings, gh))
    for number, (_, state, reason) in sorted(states.items()):
        log(f"#{number}: {reason}")

    tag = " (dry run)" if dry_run else ""
    for number, (pr, state, _) in sorted(states.items()):
        if state == "conflict":
            log(f"#{number}: flagged {settings.needs_human_label}{tag}")
            if not dry_run:
                gh.flag_needs_human(
                    cfg.repo, number, settings.label, settings.needs_human_label
                )

    ready = [
        (n, pr) for n, (pr, state, _) in sorted(states.items()) if state == "ready"
    ]
    stale = [
        (n, pr) for n, (pr, state, _) in sorted(states.items()) if state == "stale"
    ]
    for number, _ in ready[1:]:
        log(f"#{number}: waits for the next cycle")

    if ready:
        number, pr = ready[0]
        if dry_run:
            log(f"#{number}: would merge {pr['headRefOid'][:12]} (dry run)")
            return
        try:
            gh.merge(cfg.repo, number, pr["headRefOid"])
        except subprocess.CalledProcessError as exc:
            log(f"#{number}: merge refused: {(exc.stderr or '').strip()[:300]}")
            raise
        log(f"#{number}: merged {pr['headRefOid'][:12]}")
    elif stale:
        number, pr = stale[0]
        if dry_run:
            log(f"#{number}: would merge {cfg.base} into the branch (dry run)")
            return
        if gh.update_branch(cfg.repo, number, pr["headRefOid"]) == "conflict":
            log(f"#{number}: update conflicts, flagged {settings.needs_human_label}")
            gh.flag_needs_human(
                cfg.repo, number, settings.label, settings.needs_human_label
            )
        else:
            log(
                f"#{number}: {cfg.base} merged into the branch; waiting for a new green gate"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("repos.json")),
        help="JSON list of repositories (default: repos.json beside this script)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="say what would happen, change nothing"
    )
    parser.add_argument("--log", help="append to this file instead of printing")
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}"
        if args.log:
            # The scheduled task runs under pythonw, which has no console: the log is the only trace.
            with open(args.log, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        else:
            print(line, flush=True)

    try:
        settings, configs = load_config(args.config)
    except ConfigError as exc:
        log(f"configuration error: {exc}")
        return 2
    return run_cycle(Github(), configs, settings, dry_run=args.dry_run, log=log)


if __name__ == "__main__":
    sys.exit(main())
