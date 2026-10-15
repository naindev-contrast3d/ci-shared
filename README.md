# ci-shared

Reusable GitHub Actions workflows and a merge-train script shared by the Contrast3D repositories.

Status: in progress. The design is in `specs/next-ci-reliability` of
`naindev-contrast3d/contrast3d-platform`. Licensed under MIT.

## Contents

- `preflight` (available): checks required tools, a reachable Docker daemon, memory and disk on the
  runner before any build job, and labels a failure `infra`. Inputs: `runs-on`, `required-tools`,
  `min-memory-mb`, `min-disk-mb`.

- `gate` (available): one aggregate job to make the only required check. It fails if any job it
  depends on failed, was cancelled or was skipped without being declared in `allowed-skipped`.
  Inputs: `needs-json` (the caller's `toJSON(needs)`), `allowed-skipped`, `runs-on`. The caller
  job needs `if: ${{ always() }}`.

- `supply-chain` (available): the scan every repository runs, in one place. zizmor audits the
  workflow files online with the job's read-only token and gitleaks scans the history of the commit
  for secrets, both from images pinned by digest, so raising a digest is one change here and a
  caller gets it by raising its pin. Inputs: `runs-on` (a JSON label or array, default
  `"ubuntu-latest"`) and `paths` (what zizmor audits, default `.github/`). The caller job needs
  `permissions: contents: read`, and the runner needs Docker. `tests/supply-chain.test.sh` checks
  that every image is pinned by digest and that the flags that decide what is scanned stay.

- `changes` (available): changed-file detection inside the workflow, without `paths:` filters.
  Inputs: `filters` (area -> glob patterns), `global-patterns` (default `.github/**`), `runs-on`.
  Outputs: `areas` (area -> true/false) and `reason`. When the diff cannot be known with certainty
  (no base commit, base not fetchable, empty diff, global pattern matched) every area is reported
  as changed. Skipped jobs must be declared in the gate's `allowed-skipped`.

- `train/automerge.py` (available): the merge train. It is not a workflow: a scheduled task on the
  owner's desktop runs it with `gh`. One cycle goes over every repository of a JSON list
  (`train/repos.example.json`), and per repository merges at most one pull request labelled
  `automerge` whose gate check is green (plus the `ci-ok/<sha>` seal when `require_seal` is set) and
  whose head contains the base branch. A stale head gets the base merged in through the API, without
  force-push; a conflict gets `needs-human` and loses `automerge`. `merge_method` (`merge`,
  `squash` or `rebase`, default `merge`) follows what each repository allows. Each cycle it also
  logs once any workflow run that has been queued longer than `queue_alert_minutes` (default 30):
  a runner that is offline or saturated otherwise holds jobs for up to 24 hours before they fail.
  The runs already reported are remembered in `queue-alerts.json` beside the configuration.
  After every merge it comments `<!-- merge-train:merged -->` on the pull request, so a merge made
  around the train can be counted, and it writes a `heartbeat:` line (repositories, labelled pull
  requests, version of the script) at most once an hour as proof of life.
  `--dry-run` changes nothing.
  Tests: `python3 -m unittest discover -s tests`.

- `train/install.ps1` (available): installs the train as the scheduled task `CI train`, every two
  minutes, from `D:\Development\Tools\ci-train` (a copy of `automerge.py`, a `repos.json` created from
  the example only if missing, and `train.log`). Run it from a normal PowerShell window of the owner's
  session; `-Remove` retires the task.

- `report/ci_report.py` (available): the CI health report. Per repository and period it prints the
  failure rate of conclusive runs, the share of failures caused by the infrastructure (a job that
  never reached its steps, or the shared preflight) and not the code, the median duration and the
  median queue time, followed by a snapshot of today: runners online, offline and busy, queued runs
  and open pull requests. Use it to plan from the current state, not from memory:
  `py -3 report/ci_report.py --config train/repos.json --repo naindev-contrast3d/contrast-hub --days 7`.
  For the repositories of `--config` (the shared train) the snapshot also counts the pull requests
  merged outside the train since `--train-since`, and `--train-dir D:\Development\Tools\ci-train`
  adds a section on the installed train: age of its log (silent after 90 minutes) and whether its
  script differs from the one in `ci-shared` main.

- `report/weekly_report.py` (available): the weekly report to a file. It runs `ci_report.py` over every
  private repository of the organisation that is not archived and writes `ci-report-<date>.md` (and
  `latest.md`) to `--out-dir`. When the week deviates, with a failure rate above 25 % over at least
  five runs, a p90 wait for a runner above five minutes, or more than a fifth of the failures caused by
  the infrastructure, the deviations go at the top of the report and one toast is raised through
  `--notify`; a healthy week raises nothing. The thresholds are options. `report/install-weekly-report.ps1`
  registers it as the task `CI weekly report` (Mondays 08:00), from a normal PowerShell window of the
  owner's session.

- `report/runner_watch.py` (available): the runner watchdog. The report says how the queue behaved
  over a period; this says that it is wrong now. Run from a scheduled task every few minutes
  (`py -3 report/runner_watch.py --org naindev-contrast3d --notify <path of notify.ps1>`), it reads
  the runners of the organisation and of each private repository, plus the queued runs, and raises one
  toast per finding: a runner offline for `--offline-min` minutes (default 10; GitHub does not say
  since when, so the first sighting is remembered in a state file) or a run queued for `--queue-min`
  (default 15). A finding is reported once and again only after it has cleared; `--ignore NAME` mutes
  a runner that is offline on purpose; an unreadable answer is skipped, never reported. Exit code 1
  when it reported something. `report/install-runner-watch.ps1` registers it as the scheduled task
  `CI runner watch` (every five minutes, hidden, the owner's `gh` login, toasts through
  `workstation-maintenance\notify.ps1`); run it from a normal PowerShell window of the owner's
  session, with `-Ignore <runner>` for a runner that is offline on purpose, and `-Remove` to retire it.
  `--until 2026-10-14T11:00` ends the period in the past, so a baseline can be reproduced after later
  changes; `--limit` raises the number of runs read per repository (default 300).

- `standards/audit.py` (available): audits every repository of the organisation against
  `standards/standards.json` and prints each deviation as `error`, `warning` or `info`: branch
  deletion after merge, Dependabot, CODEOWNERS, secret scanning and a recognised license on public
  repositories, a CI that calls the shared gate pinned by a full SHA with a `# vX.Y.Z` comment, too
  many leftover branches, and the organisation's two-factor requirement. An exception names a
  repository and a rule, gives a reason and expires; an expired one is itself an error.
  `py -3 standards/audit.py [--fail-on error]`, or `report/ci_report.py --audit` to append it to the
  weekly report.

- `standards/bootstrap.py` (available): applies the organisation's repository settings to a new
  repository: branch deletion after merge, secret scanning and push protection where they are free,
  Dependabot alerts and security updates, and the `automerge` and `needs-human` labels the merge
  train uses. A dry run by default; `--apply` makes the changes.
  `py -3 standards/bootstrap.py owner/name [--apply]`.

## Rules

- No secrets, no business logic and no repository-specific values: everything comes in as an input.
- Callers pin these workflows by full commit SHA, never by branch or moving tag.
- Every change passes `actionlint`, `zizmor` and `gitleaks` before merge.
