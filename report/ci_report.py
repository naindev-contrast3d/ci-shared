"""Weekly CI health report: failure rate by cause, durations, queue time and a snapshot of today.

    py -3 ci_report.py --repo owner/name --repo owner/other --days 7
    py -3 ci_report.py --config ..\\train\\repos.json --days 30 --output report.md

Per repository, over the period: conclusive runs (success or failure) of one workflow, how many
failed, which share of those failures was the infrastructure and not the code, the median duration
and the median time a run waited for its first job to start. Cancelled runs are counted apart, never
as failures. The snapshot reports what is true now (runners, queue, open pull requests), because a
plan made from remembered state is how stale diagnoses happen.

A failed run counts as infrastructure only when every failed job never reached its own steps
(`Set up job`, a job with no steps) or is the shared runner preflight. Anything else is code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import statistics
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

RUN_FIELDS = "databaseId,conclusion,status,workflowName,createdAt,startedAt,updatedAt,event,headBranch"
INFRA_STEPS = ("Set up job", "Complete job")


def run_gh(command: list[str]) -> str:
    # Every command is built in this file from fixed words and GitHub's own numbers.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command, check=True, capture_output=True, text=True, creationflags=no_window
    ).stdout


class Github:
    """The few GitHub reads the report needs, over the `gh` command line."""

    def __init__(self, run: Callable[[list[str]], str] = run_gh):
        self._run = run

    def runs(self, repo: str, limit: int) -> list[dict]:
        out = self._run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--limit",
                str(limit),
                "--json",
                RUN_FIELDS,
            ]
        )
        return json.loads(out or "[]")

    def jobs(self, repo: str, run_id: int) -> list[dict]:
        out = self._run(
            ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "jobs"]
        )
        return json.loads(out or "{}").get("jobs", [])

    def runners(self, repo: str) -> list[dict]:
        try:
            out = self._run(["gh", "api", f"repos/{repo}/actions/runners"])
        except subprocess.CalledProcessError:
            return []  # no permission to list runners, or none registered
        return json.loads(out or "{}").get("runners", [])

    def queued_runs(self, repo: str) -> list[dict]:
        out = self._run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--status",
                "queued",
                "--json",
                "databaseId,createdAt",
                "--limit",
                "50",
            ]
        )
        return json.loads(out or "[]")

    def open_prs(self, repo: str) -> list[dict]:
        out = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,labels,createdAt",
                "--limit",
                "100",
            ]
        )
        return json.loads(out or "[]")


@dataclass
class RepoStats:
    repo: str
    conclusive: int = 0
    failed: int = 0
    cancelled: int = 0
    infra_failures: int = 0
    median_duration_s: float | None = None
    median_queue_s: float | None = None
    runners_online: int = 0
    runners_busy: int = 0
    runners_offline: int = 0
    queued_now: int = 0
    oldest_queued_min: int | None = None
    open_prs: int = 0
    automerge_prs: int = 0

    @property
    def fail_rate(self) -> float | None:
        return self.failed / self.conclusive if self.conclusive else None

    @property
    def infra_share(self) -> float | None:
        return self.infra_failures / self.failed if self.failed else None


def classify_failure(jobs: list[dict]) -> str:
    """'infra' when every failed job never reached its own steps, otherwise 'code'."""
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    if not failed:
        return "code"
    return "infra" if all(_is_infra(j) for j in failed) else "code"


def _is_infra(job: dict) -> bool:
    if "preflight" in (job.get("name") or "").lower():
        return True
    failed_steps = [
        s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"
    ]
    return not failed_steps or failed_steps[0] in INFRA_STEPS


def _parse(moment: str) -> dt.datetime:
    return dt.datetime.fromisoformat(moment)


def parse_moment(text: str) -> dt.datetime:
    """A date, or a date and time, read as UTC."""
    moment = dt.datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def collect(
    gh,
    repo: str,
    workflow: str,
    days: int,
    now: dt.datetime,
    sample: int = 30,
    until: dt.datetime | None = None,
    limit: int = 300,
) -> RepoStats:
    """Metrics for runs created in the `days` before `until` (default `now`).

    The snapshot (runners, queue, pull requests) is always about `now`.
    """
    end = until or now
    since = end - dt.timedelta(days=days)
    runs = [
        r
        for r in gh.runs(repo, limit)
        if r.get("workflowName") == workflow and since <= _parse(r["createdAt"]) <= end
    ]
    conclusive = [r for r in runs if r.get("conclusion") in ("success", "failure")]
    stats = RepoStats(repo=repo)
    stats.conclusive = len(conclusive)
    stats.cancelled = sum(1 for r in runs if r.get("conclusion") == "cancelled")

    for r in conclusive:
        if r["conclusion"] != "failure":
            continue
        stats.failed += 1
        if classify_failure(gh.jobs(repo, r["databaseId"])) == "infra":
            stats.infra_failures += 1

    durations = [
        (_parse(r["updatedAt"]) - _parse(r["startedAt"])).total_seconds()
        for r in conclusive
    ]
    if durations:
        stats.median_duration_s = statistics.median(durations)

    waits = []
    for r in sorted(conclusive, key=lambda item: item["createdAt"], reverse=True)[
        :sample
    ]:
        starts = [
            j["startedAt"] for j in gh.jobs(repo, r["databaseId"]) if j.get("startedAt")
        ]
        if starts:
            first = min(_parse(s) for s in starts)
            waits.append((first - _parse(r["createdAt"])).total_seconds())
    if waits:
        stats.median_queue_s = statistics.median(waits)

    for runner in gh.runners(repo):
        if runner.get("status") == "online":
            stats.runners_online += 1
            stats.runners_busy += 1 if runner.get("busy") else 0
        else:
            stats.runners_offline += 1

    queue = gh.queued_runs(repo)
    stats.queued_now = len(queue)
    if queue:
        stats.oldest_queued_min = int(
            max((now - _parse(q["createdAt"])).total_seconds() for q in queue) // 60
        )

    prs = gh.open_prs(repo)
    stats.open_prs = len(prs)
    stats.automerge_prs = sum(
        1
        for p in prs
        if "automerge" in {label["name"] for label in p.get("labels", [])}
    )
    return stats


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{round(value * 100)}%"


def render(
    stats: list[RepoStats],
    days: int,
    now: dt.datetime,
    until: dt.datetime | None = None,
) -> str:
    end = until or now
    since = (end - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    lines = [
        "# CI health report",
        "",
        (
            f"Period: {since} to {end.strftime('%Y-%m-%d')} ({days} days). "
            "Conclusive runs only; cancelled runs are not failures."
            + (
                f" The snapshot is about now ({now.strftime('%Y-%m-%d %H:%M')} UTC)."
                if until
                else ""
            )
        ),
        "",
        "| Repository | Runs | Failed | Fail rate | Infra share | Median duration | Median queue |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for s in stats:
        lines.append(
            f"| {s.repo} | {s.conclusive} | {s.failed} | {_pct(s.fail_rate)} | {_pct(s.infra_share)}"
            f" | {fmt_duration(s.median_duration_s)} | {fmt_duration(s.median_queue_s)} |"
        )
    lines += ["", "## Snapshot", ""]
    for s in stats:
        lines.append(f"### {s.repo}")
        if s.runners_online or s.runners_offline:
            lines.append(
                f"- Runners: {s.runners_online} online ({s.runners_busy} busy), {s.runners_offline} offline"
            )
        else:
            lines.append("- Runners: none registered (GitHub-hosted)")
        oldest = (
            f" (oldest {s.oldest_queued_min} min)"
            if s.oldest_queued_min is not None
            else ""
        )
        lines.append(f"- Queued now: {s.queued_now}{oldest}")
        lines.append(
            f"- Open pull requests: {s.open_prs} ({s.automerge_prs} labelled automerge)"
        )
        lines.append("")
    return "\n".join(lines)


def load_audit():
    """The standards audit module, found beside this report (`../standards/audit.py`)."""
    path = Path(__file__).resolve().parent.parent / "standards" / "audit.py"
    spec = importlib.util.spec_from_file_location("standards_audit", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["standards_audit"] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def audit_section(audit_text: str) -> str:
    """The audit report as a section of this one: its title becomes a second-level heading."""
    lines = audit_text.split("\n")
    if lines and lines[0] == "# Standards audit":
        lines[0] = "## Standards audit"
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", action="append", default=[], help="owner/name; repeatable"
    )
    parser.add_argument("--config", help="train repos.json: its repositories are added")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--workflow", default="CI")
    parser.add_argument(
        "--until",
        help="end of the period, UTC (2026-10-14 or 2026-10-14T11:00); default now. "
        "Use it to reproduce a baseline after later changes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=300,
        help="runs read per repository (gh allows up to 1000)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="append the standards audit of the organisation",
    )
    parser.add_argument("--output", help="write the report here instead of printing it")
    args = parser.parse_args(argv)

    repos = list(args.repo)
    if args.config:
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        repos += [entry["repo"] for entry in data.get("repos", [])]
    if not repos:
        print("give at least one --repo or a --config", file=sys.stderr)
        return 2

    now = dt.datetime.now(dt.UTC)
    gh = Github()
    until = parse_moment(args.until) if args.until else None
    stats = [
        collect(gh, repo, args.workflow, args.days, now, until=until, limit=args.limit)
        for repo in repos
    ]
    text = render(stats, args.days, now, until=until)
    if args.audit:
        audit = load_audit()
        standard_path = Path(audit.__file__).with_name("standards.json")
        text += "\n\n" + audit_section(
            audit.render(audit.run(standard_path, now=now), now)
        )
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
