#!/usr/bin/env bash
# Tests for .github/workflows/supply-chain.yml.
# The scanners run in containers a test cannot start, so these check what makes the scan trustworthy:
# every image pinned by digest, every flag that decides what is scanned, and the shape of the job.
# The workflow under test is the file that runs in CI, read with yq.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow="$root/.github/workflows/supply-chain.yml"
[ -f "$workflow" ] || { echo "FAIL: $workflow does not exist"; exit 1; }

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }
check() { # check <description> <command...>
  local description="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}

zizmor="$(yq -r '.jobs."supply-chain".steps[] | select(.name | test("zizmor")) | .run' "$workflow")"
gitleaks="$(yq -r '.jobs."supply-chain".steps[] | select(.name | test("gitleaks")) | .run' "$workflow")"

check "the workflow is a reusable one" test "$(yq -r '.on | has("workflow_call")' "$workflow")" = true
check "runs-on is an input that defaults to a hosted runner" \
  test "$(yq -r '.on.workflow_call.inputs."runs-on".default' "$workflow")" = '"ubuntu-latest"'
check "the job runs with read-only contents" test "$(yq -r '.jobs."supply-chain".permissions.contents' "$workflow")" = read
check "the job is bounded in time" test "$(yq -r '.jobs."supply-chain"."timeout-minutes"' "$workflow")" != null

checkout_uses="$(yq -r '.jobs."supply-chain".steps[0].uses' "$workflow")"
check "checkout is pinned by a full commit SHA" grep -Eq '@[0-9a-f]{40}( |$)' <<<"$checkout_uses"
check "checkout fetches the whole history" test "$(yq -r '.jobs."supply-chain".steps[0].with."fetch-depth"' "$workflow")" = 0
check "checkout leaves no credentials behind" \
  test "$(yq -r '.jobs."supply-chain".steps[0].with."persist-credentials"' "$workflow")" = false

# Every image the scan runs is pinned by digest; a tag can be moved under the pin.
images="$(grep -Eho 'ghcr\.io/[a-z0-9._/-]+(@sha256:[0-9a-f]{64}|:[A-Za-z0-9._-]+)' "$workflow" || true)"
check "the workflow names the two scanner images" test "$(wc -l <<<"$images")" -ge 2
check "every scanner image is pinned by digest" bash -c '! grep -qv "@sha256:" <<<"$1"' _ "$images"
check "the zizmor image is zizmor" grep -q 'zizmorcore/zizmor@sha256:' "$workflow"
check "the gitleaks image is gitleaks" grep -q 'gitleaks/gitleaks@sha256:' "$workflow"

# zizmor: online audits with the job token, the workflow files read-only, output as annotations.
check "zizmor gets the job token" grep -q 'GH_TOKEN' "$workflow"
check "zizmor reads the workspace read-only" grep -q ':/repo:ro' <<<"$zizmor"
check "zizmor reports as annotations" grep -q -- '--format github' <<<"$zizmor"
check "zizmor audits the paths input" grep -q 'inputs.paths' "$workflow"

# gitleaks: the history of the commit being checked, redacted, failing the job on a finding.
check "gitleaks runs even when zizmor failed" test "$(yq -r '.jobs."supply-chain".steps[] | select(.name | test("gitleaks")) | .if' "$workflow")" = '${{ !cancelled() }}'
check "gitleaks fails on a finding" grep -q -- '--exit-code 1' <<<"$gitleaks"
check "gitleaks never prints a secret" grep -q -- '--redact' <<<"$gitleaks"
check "gitleaks scans the history of this commit only" grep -q -- '--log-opts="--full-history HEAD"' <<<"$gitleaks"
check "gitleaks trusts the mounted directory" grep -q 'safe.directory' <<<"$gitleaks"

# What the caller cannot influence: the images and flags are not inputs.
check "no input changes an image" bash -c '! yq -r ".on.workflow_call.inputs | keys | .[]" "$1" | grep -qi "image"' _ "$workflow"
check "the job runs as the runner's user, not as root" grep -q -- '--user "$(id -u):$(id -g)"' <<<"$zizmor"

echo
if [ "$failures" -eq 0 ]; then echo "all supply-chain checks passed"; else echo "$failures supply-chain check(s) failed"; exit 1; fi
