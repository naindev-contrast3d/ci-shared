"""Weekly CI report to a file, with a toast only when the week deviates.

    py -3 weekly_report.py --org naindev-contrast3d --out-dir D:\\...\\reports --notify D:\\...\\notify.ps1

Run it from a scheduled task once a week. It reads, over `gh`, every private repository of the
organisation that is not archived and writes `ci-report-<date>.md` (and a copy as `latest.md`) in the
format of `ci_report.py`: failure rate by cause, durations, queue time per job and a snapshot of today.
A week that deviates (a failure rate, a queue or an infrastructure share above its threshold) puts the
deviations at the top of the report and raises one toast; a healthy week raises nothing, because a
notification that always fires is one nobody reads. An unreadable repository is skipped and said in the
report, never fatal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_report
import runner_watch

FAIL_RATE_MAX = 0.25
WAIT_P90_MAX_S = 300
INFRA_SHARE_MAX = 0.2
MIN_RUNS = 5  # below this, a rate is noise
MIN_FAILURES = 3  # below this, an infrastructure share is noise


class Result(NamedTuple):
    path: Path
    deviations: list[str]


def find_deviations(
    stats: list[ci_report.RepoStats],
    fail_rate_max: float = FAIL_RATE_MAX,
    wait_p90_max: float = WAIT_P90_MAX_S,
    infra_share_max: float = INFRA_SHARE_MAX,
    min_runs: int = MIN_RUNS,
) -> list[str]:
    """One sentence per repository and finding that is over its threshold."""
    found: list[str] = []
    for s in stats:
        rate = s.fail_rate
        if rate is not None and s.conclusive >= min_runs and rate > fail_rate_max:
            found.append(
                f"{s.repo}: {ci_report._pct(rate)} of the runs failed ({s.failed} of {s.conclusive})"
            )
        if s.job_wait_p90_s is not None and s.job_wait_p90_s > wait_p90_max:
            found.append(
                f"{s.repo}: the p90 wait for a runner was {ci_report.fmt_duration(s.job_wait_p90_s)}"
            )
        share = s.infra_share
        if share is not None and s.failed >= MIN_FAILURES and share > infra_share_max:
            found.append(
                f"{s.repo}: {s.infra_failures} of {s.failed} failures were the infrastructure"
            )
    return found


def collect_all(gh, org: str, days: int, now: dt.datetime, limit: int):
    """Stats of every private, non-archived repository; the unreadable ones are named apart."""
    stats: list[ci_report.RepoStats] = []
    skipped: list[str] = []
    for repo in gh.org_repos(org):
        if repo.get("isArchived") or repo.get("visibility") != "PRIVATE":
            continue
        scope = f"{org}/{repo['name']}"
        try:
            stats.append(ci_report.collect(gh, scope, "CI", days, now, limit=limit))
        except (subprocess.CalledProcessError, ValueError, KeyError) as exc:
            skipped.append(f"{scope}: {type(exc).__name__}")
    return stats, skipped


def run(
    gh,
    org: str,
    out_dir: Path,
    now: dt.datetime,
    *,
    days: int = 7,
    limit: int = 300,
    notify_path: str | None = None,
    fail_rate_max: float = FAIL_RATE_MAX,
    wait_p90_max: float = WAIT_P90_MAX_S,
    infra_share_max: float = INFRA_SHARE_MAX,
    runner: Callable[[list[str]], object] = runner_watch._run_hidden,
) -> Result:
    stats, skipped = collect_all(gh, org, days, now, limit)
    deviations = find_deviations(stats, fail_rate_max, wait_p90_max, infra_share_max)
    text = ci_report.render(stats, days, now)
    head = ""
    if deviations:
        head = "## Deviations\n\n" + "\n".join(f"- {d}" for d in deviations) + "\n\n"
    if skipped:
        head += "## Not read\n\n" + "\n".join(f"- {s}" for s in skipped) + "\n\n"
    if head:
        first, _, rest = text.partition("\n\n")
        text = first + "\n\n" + head + rest
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ci-report-{now.strftime('%Y-%m-%d')}.md"
    path.write_text(text, encoding="utf-8", newline="\n")
    (out_dir / "latest.md").write_text(text, encoding="utf-8", newline="\n")
    if deviations and notify_path:
        shown = "; ".join(deviations[:3]) + (" ..." if len(deviations) > 3 else "")
        runner_watch.notify(
            [
                runner_watch.Alert(
                    f"weekly:{now.strftime('%Y-%m-%d')}",
                    f"CI weekly report: {len(deviations)} deviation(s)",
                    shown,
                )
            ],
            notify_path,
            runner,
        )
    return Result(path, deviations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", required=True)
    parser.add_argument(
        "--out-dir",
        help="where the reports go (default: a reports folder beside this file)",
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--limit", type=int, default=300, help="runs read per repository"
    )
    parser.add_argument(
        "--notify", help="path of notify.ps1: one toast when the week deviates"
    )
    parser.add_argument("--fail-rate-max", type=float, default=FAIL_RATE_MAX)
    parser.add_argument(
        "--wait-p90-max", type=float, default=WAIT_P90_MAX_S, help="seconds"
    )
    parser.add_argument("--infra-share-max", type=float, default=INFRA_SHARE_MAX)
    args = parser.parse_args(argv)
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else Path(__file__).resolve().with_name("reports")
    )
    result = run(
        runner_watch.Github(),
        args.org,
        out_dir,
        dt.datetime.now(dt.UTC),
        days=args.days,
        limit=args.limit,
        notify_path=args.notify,
        fail_rate_max=args.fail_rate_max,
        wait_p90_max=args.wait_p90_max,
        infra_share_max=args.infra_share_max,
    )
    print(f"report: {result.path}")
    for deviation in result.deviations:
        print(f"deviation: {deviation}")
    return 1 if result.deviations else 0


if __name__ == "__main__":
    sys.exit(main())
