"""Runner watchdog: a toast when a self-hosted runner is offline or a run waits too long in the queue.

    py -3 runner_watch.py --org naindev-contrast3d --notify D:\\...\\notify.ps1

Run it from a scheduled task every few minutes. Each cycle reads, over `gh`, the runners of the
organisation and of every private repository in it, plus the runs still queued. It reports a runner
that has been seen offline for `--offline-min` minutes (GitHub does not say since when, so the first
sighting is remembered in the state file) and a run queued for `--queue-min` minutes. Every finding
is reported once, and again only after it has gone away and come back. An unreadable answer is
skipped, never reported: a watchdog that cries wolf on a network blip gets muted.

The report in this folder (`ci_report.py`) says how the queue behaved over a period; this says that
it is wrong now.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_report  # same folder; its Github class does the `gh` reads

STAMP = "%Y-%m-%dT%H:%M:%SZ"
OFFLINE_MIN = 10
QUEUE_MIN = 15


@dataclass
class Alert:
    key: str
    title: str
    body: str


class Github(ci_report.Github):
    """The reads of the report plus the two the watchdog needs at organisation scope."""

    def org_runners(self, org: str) -> list[dict]:
        out = self._run(["gh", "api", f"orgs/{org}/actions/runners?per_page=100"])
        return json.loads(out or "{}").get("runners", [])

    def org_repos(self, org: str) -> list[dict]:
        out = self._run(
            [
                "gh",
                "repo",
                "list",
                org,
                "--limit",
                "100",
                "--json",
                "name,visibility,isArchived",
            ]
        )
        return json.loads(out or "[]")


def collect(gh, org: str) -> dict:
    """What GitHub says now: (scope, runner name, status) and (repository, run id, created at)."""
    runners: list[tuple[str, str, str]] = []
    queued: list[tuple[str, int, str]] = []
    try:
        for runner in gh.org_runners(org):
            runners.append((org, runner["name"], runner["status"]))
    except subprocess.CalledProcessError:
        pass
    try:
        repos = gh.org_repos(org)
    except subprocess.CalledProcessError:
        repos = []
    for repo in repos:
        if repo.get("isArchived") or repo.get("visibility") != "PRIVATE":
            continue
        scope = f"{org}/{repo['name']}"
        try:
            for runner in gh.runners(scope):
                runners.append((scope, runner["name"], runner["status"]))
        except subprocess.CalledProcessError:
            pass
        try:
            for item in gh.queued_runs(scope):
                queued.append((scope, item["databaseId"], item["createdAt"]))
        except subprocess.CalledProcessError:
            pass
    return {"runners": runners, "queued": queued}


def _moment(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def evaluate(
    snapshot: dict,
    state: dict,
    now: dt.datetime,
    offline_min: int = OFFLINE_MIN,
    queue_min: int = QUEUE_MIN,
    ignore=frozenset(),
) -> tuple[list[Alert], dict]:
    """The alerts that are new now, and the state to keep for the next cycle."""
    alerted = set(state.get("alerted", []))
    seen_offline = dict(state.get("offline_since", {}))
    new_offline: dict[str, str] = {}
    active: set[str] = set()
    alerts: list[Alert] = []

    for scope, name, status in snapshot["runners"]:
        if name in ignore or status == "online":
            continue
        key = f"{scope}/{name}"
        since = seen_offline.get(key) or now.strftime(STAMP)
        new_offline[key] = since
        minutes = int((now - _moment(since)).total_seconds() // 60)
        if minutes < offline_min:
            continue
        alert_key = f"offline:{key}"
        active.add(alert_key)
        if alert_key not in alerted:
            alerts.append(
                Alert(
                    alert_key,
                    "Runner offline",
                    f"{name} ({scope}) has been offline for {minutes} min.",
                )
            )

    for scope, run_id, created in snapshot["queued"]:
        minutes = int((now - _moment(created)).total_seconds() // 60)
        if minutes < queue_min:
            continue
        alert_key = f"queue:{scope}/{run_id}"
        active.add(alert_key)
        if alert_key not in alerted:
            alerts.append(
                Alert(
                    alert_key,
                    "CI run waiting for a runner",
                    f"A run of {scope} has been queued for {minutes} min.",
                )
            )

    kept = sorted((alerted & active) | {a.key for a in alerts})
    return alerts, {"offline_since": new_offline, "alerted": kept}


def notify(
    alerts: list[Alert], notify_path: str, runner: Callable[[list[str]], object]
) -> None:
    """One toast per alert through the desktop's own `notify.ps1`; a failing toast never raises."""
    for alert in alerts:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            notify_path,
            "-Title",
            alert.title,
            "-Body",
            alert.body,
        ]
        try:
            runner(command)
        except (OSError, subprocess.SubprocessError):
            pass


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(
        json.dumps(state, indent=1, sort_keys=True), encoding="utf-8", newline="\n"
    )
    scratch.replace(path)


def _run_hidden(command: list[str]) -> None:
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(command, check=False, capture_output=True, creationflags=no_window)


def run(
    gh,
    org: str,
    state_path: Path,
    now: dt.datetime,
    *,
    notify_path: str | None,
    offline_min: int = OFFLINE_MIN,
    queue_min: int = QUEUE_MIN,
    ignore=frozenset(),
    runner: Callable[[list[str]], object] = _run_hidden,
) -> list[Alert]:
    alerts, state = evaluate(
        collect(gh, org), load_state(state_path), now, offline_min, queue_min, ignore
    )
    if notify_path:
        notify(alerts, notify_path, runner)
    save_state(state_path, state)
    return alerts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", required=True, help="the organisation to watch")
    parser.add_argument(
        "--state", help="JSON remembered between cycles (default: beside this file)"
    )
    parser.add_argument(
        "--notify", help="path of notify.ps1: one toast per new finding"
    )
    parser.add_argument("--offline-min", type=int, default=OFFLINE_MIN)
    parser.add_argument("--queue-min", type=int, default=QUEUE_MIN)
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="a runner name never reported; repeatable",
    )
    args = parser.parse_args(argv)
    state = (
        Path(args.state)
        if args.state
        else Path(__file__).resolve().with_name("runner-watch.json")
    )
    alerts = run(
        Github(),
        args.org,
        state,
        dt.datetime.now(dt.UTC),
        notify_path=args.notify,
        offline_min=args.offline_min,
        queue_min=args.queue_min,
        ignore=frozenset(args.ignore),
    )
    for alert in alerts:
        print(f"{alert.title}: {alert.body}")
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
