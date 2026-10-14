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
  `--dry-run` changes nothing.
  Tests: `python3 -m unittest discover -s tests`.

- `train/install.ps1` (available): installs the train as the scheduled task `CI train`, every two
  minutes, from `D:\Development\Tools\ci-train` (a copy of `automerge.py`, a `repos.json` created from
  the example only if missing, and `train.log`). Run it from a normal PowerShell window of the owner's
  session; `-Remove` retires the task.

## Planned contents

- The weekly CI health report (task T16 of the spec).

## Rules

- No secrets, no business logic and no repository-specific values: everything comes in as an input.
- Callers pin these workflows by full commit SHA, never by branch or moving tag.
- Every change passes `actionlint`, `zizmor` and `gitleaks` before merge.
