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
import hashlib
import importlib.util
import json
import statistics
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

RUN_FIELDS = "databaseId,conclusion,status,workflowName,createdAt,startedAt,updatedAt,event,headBranch"
INFRA_STEPS = ("Set up job", "Complete job")
# The comment the merge train writes on every pull request it merges (train/automerge.py).
TRAIN_MARK = "<!-- merge-train:merged -->"
TRAIN_SILENT_AFTER_MIN = 90  # the train writes a heartbeat every 60 minutes


def text_version(text: str) -> str:
    """A short fingerprint of a script, the same for LF and CRLF copies (as in the train)."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:12]


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

    def job_timings(self, repo: str, run_id: int) -> list[dict]:
        """The jobs of a run with `created_at` (when each became ready) and `started_at`: the
        difference is the time it waited for a runner. `gh run view` does not give the former."""
        try:
            out = self._run(
                ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]
            )
        except subprocess.CalledProcessError:
            return []  # the run is gone or unreadable: no figure is better than a wrong one
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

    def merged_prs(self, repo: str, limit: int) -> list[dict]:
        out = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "merged",
                "--limit",
                str(limit),
                "--json",
                "number,mergedAt,comments",
            ]
        )
        return json.loads(out or "[]")

    def file_text(self, repo: str, path: str) -> str | None:
        try:
            return self._run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github.raw+json",
                    f"repos/{repo}/contents/{path}",
                ]
            )
        except subprocess.CalledProcessError:
            return None

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
    # Only for repositories served by the shared merge train; None elsewhere.
    merged_total: int | None = None
    merged_by_hand: int | None = None
    # Time each job of the sampled runs waited for a runner (created_at to started_at).
    jobs_sampled: int = 0
    job_wait_median_s: float | None = None
    job_wait_p90_s: float | None = None
    job_wait_max_s: float | None = None
    # (job name, median wait in seconds, jobs seen), the three that waited longest.
    slowest_jobs: list[tuple[str, float, int]] = field(default_factory=list)

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


def job_waits(timings: list[dict]) -> list[tuple[str, float]]:
    """(job name, seconds it waited for a runner) for the jobs that actually started."""
    waits = []
    for item in timings:
        if item.get("conclusion") == "skipped":
            continue
        created, started = item.get("created_at"), item.get("started_at")
        if not created or not started:
            continue
        seconds = (_parse(started) - _parse(created)).total_seconds()
        waits.append((item.get("name") or "unnamed job", max(seconds, 0)))
    return waits


def percentile(values: list[float], pct: int) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil without floats
    return ordered[min(rank, len(ordered)) - 1]


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
    train_repos: frozenset[str] | set[str] = frozenset(),
    train_since: dt.datetime | None = None,
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

    by_job: dict[str, list[float]] = {}
    for r in sorted(conclusive, key=lambda item: item["createdAt"], reverse=True)[
        :sample
    ]:
        for name, seconds in job_waits(gh.job_timings(repo, r["databaseId"])):
            by_job.setdefault(name, []).append(seconds)
    every = [seconds for group in by_job.values() for seconds in group]
    if every:
        stats.jobs_sampled = len(every)
        stats.job_wait_median_s = statistics.median(every)
        stats.job_wait_p90_s = percentile(every, 90)
        stats.job_wait_max_s = max(every)
        ranked = sorted(
            ((n, statistics.median(g), len(g)) for n, g in by_job.items()),
            key=lambda item: (-item[1], item[0]),
        )
        stats.slowest_jobs = ranked[:3]

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

    if repo in train_repos:
        floor = max(since, train_since) if train_since else since
        merged = [
            m for m in gh.merged_prs(repo, 200) if floor <= _parse(m["mergedAt"]) <= end
        ]
        stats.merged_total = len(merged)
        stats.merged_by_hand = sum(
            1
            for m in merged
            if not any(
                TRAIN_MARK in (c.get("body") or "") for c in m.get("comments", [])
            )
        )
    return stats


@dataclass
class TrainHealth:
    log_age_min: (
        int | None
    )  # minutes since the log was last written; None without a log
    silent: bool  # no log line for too long
    copy: str  # current, stale, missing or unknown
    installed_version: str | None = None
    repository_version: str | None = None


def train_health(
    train_dir: Path,
    gh,
    now: dt.datetime,
    repo: str = "naindev-contrast3d/ci-shared",
) -> TrainHealth:
    """Is the installed train alive and is it the script of `ci-shared` main?"""
    train_dir = Path(train_dir)
    script = train_dir / "automerge.py"
    log = train_dir / "train.log"

    age = None
    if log.exists():
        modified = dt.datetime.fromtimestamp(log.stat().st_mtime, dt.UTC)
        age = int((now - modified).total_seconds() // 60)
    silent = age is not None and age > TRAIN_SILENT_AFTER_MIN

    if not script.exists():
        return TrainHealth(age, silent, "missing")
    installed = text_version(script.read_text(encoding="utf-8"))
    published = gh.file_text(repo, "train/automerge.py")
    if published is None:
        return TrainHealth(age, silent, "unknown", installed_version=installed)
    current = text_version(published)
    return TrainHealth(
        age, silent, "current" if installed == current else "stale", installed, current
    )


def render_train(health: TrainHealth) -> str:
    lines = ["## Merge train", ""]
    if health.copy == "missing":
        lines.append("- Installed copy not found (the train is not installed here).")
        return "\n".join(lines)
    if health.log_age_min is None:
        lines.append("- No log yet.")
    elif health.silent:
        lines.append(
            f"- Warning: no log line for {health.log_age_min} min "
            f"(the train writes a heartbeat every 60 minutes)."
        )
    else:
        lines.append(f"- Last log activity {health.log_age_min} min ago.")
    if health.copy == "current":
        lines.append("- The installed script matches ci-shared main.")
    elif health.copy == "stale":
        lines.append(
            f"- Warning: the installed script differs from ci-shared main "
            f"(installed {health.installed_version}, repository {health.repository_version})."
        )
    else:
        lines.append("- Could not read the script of ci-shared main to compare.")
    return "\n".join(lines)


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
    if any(s.jobs_sampled for s in stats):
        lines += [
            "",
            "## Queue time per job",
            "",
            "Time each job of the most recent runs waited for a runner, from the moment it was "
            "ready to the moment it started. A job waits when every runner slot is taken, so a "
            "high figure with few runners online asks for more slots; one with plenty of idle "
            "runners points at the runner service instead.",
            "",
            "| Repository | Jobs | Median wait | p90 | Max | Slowest to start | Runners online |",
            "| --- | ---: | ---: | ---: | ---: | --- | ---: |",
        ]
        for s in stats:
            slowest = ", ".join(
                f"{name} ({fmt_duration(wait)})" for name, wait, _ in s.slowest_jobs[:1]
            )
            lines.append(
                f"| {s.repo} | {s.jobs_sampled} | {fmt_duration(s.job_wait_median_s)}"
                f" | {fmt_duration(s.job_wait_p90_s)} | {fmt_duration(s.job_wait_max_s)}"
                f" | {slowest or '-'} | {s.runners_online} |"
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
        if s.merged_by_hand is not None:
            lines.append(
                f"- Merged outside the train: {s.merged_by_hand} of {s.merged_total}"
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
    parser.add_argument(
        "--train-dir",
        help="folder of the installed merge train (automerge.py and train.log): adds its health",
    )
    parser.add_argument(
        "--train-since",
        help="UTC date the shared train started serving the repositories of --config; "
        "earlier merges are not counted as made around it",
    )
    parser.add_argument("--output", help="write the report here instead of printing it")
    args = parser.parse_args(argv)

    repos = list(args.repo)
    train_repos: set[str] = set()
    if args.config:
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        train_repos = {entry["repo"] for entry in data.get("repos", [])}
        repos += sorted(train_repos)
    if not repos:
        print("give at least one --repo or a --config", file=sys.stderr)
        return 2

    now = dt.datetime.now(dt.UTC)
    gh = Github()
    until = parse_moment(args.until) if args.until else None
    train_since = parse_moment(args.train_since) if args.train_since else None
    stats = [
        collect(
            gh,
            repo,
            args.workflow,
            args.days,
            now,
            until=until,
            limit=args.limit,
            train_repos=train_repos,
            train_since=train_since,
        )
        for repo in repos
    ]
    text = render(stats, args.days, now, until=until)
    if args.train_dir:
        text += "\n\n" + render_train(train_health(Path(args.train_dir), gh, now))
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
