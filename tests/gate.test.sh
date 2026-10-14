#!/usr/bin/env bash
# Tests for the gate step of .github/workflows/gate.yml.
# The script under test is extracted from the workflow, so the file that runs in CI is the file tested.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow="$root/.github/workflows/gate.yml"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

yq -r '.jobs.gate.steps[] | select(.id == "evaluate") | .run' "$workflow" > "$work/evaluate.sh"
[ -s "$work/evaluate.sh" ] || { echo "FAIL: step 'evaluate' not found in $workflow"; exit 1; }

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }

# run_gate <needs-json> [allowed-skipped-json]; prints the exit code, leaves output in $work/out and $work/summary
run_gate() {
  : > "$work/summary"
  NEEDS_JSON="$1" ALLOWED_SKIPPED="${2:-[]}" GITHUB_STEP_SUMMARY="$work/summary" \
    bash "$work/evaluate.sh" > "$work/out" 2>&1
  echo $?
}

ok_needs='{"backend":{"result":"success"},"frontend":{"result":"success"}}'

# 1. every job succeeded
rc="$(run_gate "$ok_needs")"
[ "$rc" = 0 ] && pass "all green passes" || { fail "all green passes (rc=$rc)"; cat "$work/out"; }

# 2. a failed job fails the gate and is named
rc="$(run_gate '{"backend":{"result":"failure"},"frontend":{"result":"success"}}')"
if [ "$rc" != 0 ] && grep -q "backend: failure" "$work/out"; then pass "failure is named"; else fail "failure is named (rc=$rc)"; fi

# 3. a cancelled job fails the gate
rc="$(run_gate '{"backend":{"result":"cancelled"},"frontend":{"result":"success"}}')"
if [ "$rc" != 0 ] && grep -q "backend: cancelled" "$work/out"; then pass "cancelled fails"; else fail "cancelled fails (rc=$rc)"; fi

# 4. a skipped job without a recorded reason fails
rc="$(run_gate '{"backend":{"result":"skipped"},"frontend":{"result":"success"}}')"
if [ "$rc" != 0 ] && grep -q "backend: skipped without a recorded reason" "$work/out"; then pass "unexplained skip fails"; else fail "unexplained skip fails (rc=$rc)"; fi

# 5. a skipped job that the caller declared passes and is listed in the summary
rc="$(run_gate '{"backend":{"result":"skipped"},"frontend":{"result":"success"}}' '["backend"]')"
if [ "$rc" = 0 ] && grep -q "backend" "$work/summary" && grep -qi "skipped (declared)" "$work/summary"; then pass "declared skip passes and is listed"; else fail "declared skip passes and is listed (rc=$rc)"; fi

# 6. declaring a skip never excuses a failure
rc="$(run_gate '{"backend":{"result":"failure"}}' '["backend"]')"
[ "$rc" != 0 ] && pass "declared skip does not excuse a failure" || fail "declared skip does not excuse a failure"

# 7. a gate that needs nothing is a caller mistake
rc="$(run_gate '{}')"
if [ "$rc" != 0 ] && grep -q "no jobs to evaluate" "$work/out"; then pass "empty needs fails"; else fail "empty needs fails (rc=$rc)"; fi

# 8. input that is not JSON fails instead of passing by accident
rc="$(run_gate 'not json')"
if [ "$rc" != 0 ] && grep -q "needs-json is not valid JSON" "$work/out"; then pass "invalid needs JSON fails"; else fail "invalid needs JSON fails (rc=$rc)"; fi
rc="$(run_gate "$ok_needs" 'not json')"
if [ "$rc" != 0 ] && grep -q "allowed-skipped is not a JSON array of strings" "$work/out"; then pass "invalid allowed-skipped fails"; else fail "invalid allowed-skipped fails (rc=$rc)"; fi

# 9. an unknown result value fails
rc="$(run_gate '{"backend":{"result":"mystery"}}')"
if [ "$rc" != 0 ] && grep -q "backend: mystery" "$work/out"; then pass "unknown result fails"; else fail "unknown result fails (rc=$rc)"; fi

# 10. every problem is reported in one run
rc="$(run_gate '{"a":{"result":"failure"},"b":{"result":"cancelled"},"c":{"result":"skipped"}}')"
if [ "$rc" != 0 ] && grep -q "a: failure" "$work/out" && grep -q "b: cancelled" "$work/out" && grep -q "c: skipped" "$work/out"; then
  pass "all problems reported together"; else fail "all problems reported together (rc=$rc)"; fi

# 11. the summary carries a table with every job
rc="$(run_gate "$ok_needs")"
if grep -q "| backend | success |" "$work/summary" && grep -q "| frontend | success |" "$work/summary"; then pass "summary lists every job"; else fail "summary lists every job"; fi

echo
if [ "$failures" -eq 0 ]; then echo "all gate tests passed"; else echo "$failures gate test(s) failed"; exit 1; fi
