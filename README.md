# ci-shared

Reusable GitHub Actions workflows shared by the maintainers' repositories.

Status: in progress. The design is kept in the maintainers' own repository. Licensed under MIT.

Tooling that is specific to one organisation (CI reports, a standards audit, a merge train) is not
here: it lives in a private repository, because it works on that organisation's own repositories.

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

## Rules

- No secrets, no business logic and no repository-specific values: everything comes in as an input.
- Callers pin these workflows by full commit SHA, never by branch or moving tag.
- Every change passes `actionlint`, `zizmor` and `gitleaks` before merge.
