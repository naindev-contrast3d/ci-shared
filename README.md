# ci-shared

Reusable GitHub Actions workflows and a merge-train script shared by the Contrast3D repositories.

Status: bootstrap. Nothing is published here yet; the design is in
`specs/next-ci-reliability` of `naindev-contrast3d/contrast3d-platform`.

## Planned contents

- `preflight`: checks required tools, memory and disk on the runner before any build job.
- `gate`: one aggregate job that fails if any upstream job failed, was cancelled or was skipped
  without a recorded reason.
- `changes`: changed-file detection inside the workflow, without `paths:` filters.
- `automerge`: a repository-parameterised merge train that requires a sealed head containing `main`.

## Rules

- No secrets, no business logic and no repository-specific values: everything comes in as an input.
- Callers pin these workflows by full commit SHA, never by branch or moving tag.
- Every change passes `actionlint`, `zizmor` and `gitleaks` before merge.
