#!/usr/bin/env bash
# Tests for the compute step of .github/workflows/changes.yml.
# The script under test is extracted from the workflow, so the file that runs in CI is the file tested.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow="$root/.github/workflows/changes.yml"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

yq -r '.jobs.changes.steps[] | select(.id == "compute") | .run' "$workflow" > "$work/compute.sh"
[ -s "$work/compute.sh" ] || { echo "FAIL: step 'compute' not found in $workflow"; exit 1; }

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }

filters='{"backend":["backend/**"],"docs":["docs/**","*.md"]}'

g() { git -c core.autocrlf=false -c core.hooksPath=/dev/null -c user.name=test -c user.email=test@example.invalid -c commit.gpgsign=false "$@"; }

# new_repo <name>: a repository with one backend file, one docs file and a README, committed once.
new_repo() {
  repo="$work/$1"
  mkdir -p "$repo/backend" "$repo/docs"
  (
    cd "$repo" || exit 1
    g init -q -b main
    echo a > backend/app.py
    echo b > docs/a.txt
    echo c > README.md
    g add -A
    g commit -q -m "chore: initial"
  )
}

# commit_change <repo> <command...>: run the command inside the repo, commit everything.
commit_change() {
  local r="$1"; shift
  (
    cd "$work/$r" || exit 1
    "$@"
    g add -A
    g commit -q --allow-empty -m "chore: change"
  )
}

# run_compute <repo> <base> [filters] [global]; prints the exit code, output in $work/out and $work/ghout
run_compute() {
  local r="$1" base="$2" f="${3:-$filters}" gl="${4:-[\".github/**\"]}"
  : > "$work/ghout"; : > "$work/summary"
  (
    cd "$work/$r" || exit 1
    BASE_SHA="$base" FILTERS_JSON="$f" GLOBAL_PATTERNS="$gl" \
      GITHUB_OUTPUT="$work/ghout" GITHUB_STEP_SUMMARY="$work/summary" \
      bash "$work/compute.sh" > "$work/out" 2>&1
  )
  echo $?
}

areas() { sed -n 's/^areas=//p' "$work/ghout"; }
reason() { sed -n 's/^reason=//p' "$work/ghout"; }
head_before() { (cd "$work/$1" && git rev-parse HEAD~1); }

# 1. docs-only change
new_repo t1; commit_change t1 sh -c 'echo x > docs/new.txt'
rc="$(run_compute t1 "$(head_before t1)")"
[ "$rc" = 0 ] && [ "$(areas)" = '{"backend":false,"docs":true}' ] && pass "docs-only change" || { fail "docs-only change (rc=$rc areas=$(areas))"; cat "$work/out"; }

# 2. backend-only change
new_repo t2; commit_change t2 sh -c 'echo x >> backend/app.py'
rc="$(run_compute t2 "$(head_before t2)")"
[ "$rc" = 0 ] && [ "$(areas)" = '{"backend":true,"docs":false}' ] && pass "backend-only change" || fail "backend-only change (rc=$rc areas=$(areas))"

# 3. both areas
new_repo t3; commit_change t3 sh -c 'echo x >> backend/app.py; echo y > docs/new.txt'
rc="$(run_compute t3 "$(head_before t3)")"
[ "$(areas)" = '{"backend":true,"docs":true}' ] && pass "both areas change" || fail "both areas change (areas=$(areas))"

# 4. a root file matched by a bare pattern
new_repo t4; commit_change t4 sh -c 'echo x >> README.md'
rc="$(run_compute t4 "$(head_before t4)")"
[ "$(areas)" = '{"backend":false,"docs":true}' ] && pass "root file matches *.md" || fail "root file matches *.md (areas=$(areas))"

# 5. a global pattern turns every area on
new_repo t5; commit_change t5 sh -c 'mkdir -p .github/workflows; echo x > .github/workflows/ci.yml'
rc="$(run_compute t5 "$(head_before t5)")"
if [ "$(areas)" = '{"backend":true,"docs":true}' ] && reason | grep -q "global pattern"; then pass "global pattern forces every area"; else fail "global pattern forces every area (areas=$(areas) reason=$(reason))"; fi

# 6. base commit unknown to the repository falls back to everything
new_repo t6; commit_change t6 sh -c 'echo x > docs/new.txt'
rc="$(run_compute t6 "1111111111111111111111111111111111111111")"
if [ "$rc" = 0 ] && [ "$(areas)" = '{"backend":true,"docs":true}' ] && reason | grep -q "base commit not available"; then pass "unknown base falls back to everything"; else fail "unknown base falls back to everything (rc=$rc areas=$(areas) reason=$(reason))"; fi

# 7. no base at all (manual or scheduled run)
rc="$(run_compute t6 "")"
if [ "$(areas)" = '{"backend":true,"docs":true}' ] && reason | grep -q "no base commit"; then pass "empty base falls back to everything"; else fail "empty base falls back to everything (areas=$(areas))"; fi

# 8. all-zero base (first push of a branch)
rc="$(run_compute t6 "0000000000000000000000000000000000000000")"
if [ "$(areas)" = '{"backend":true,"docs":true}' ] && reason | grep -q "no base commit"; then pass "zero base falls back to everything"; else fail "zero base falls back to everything (areas=$(areas))"; fi

# 9. an empty diff never skips work
new_repo t9; commit_change t9 true
rc="$(run_compute t9 "$(head_before t9)")"
if [ "$(areas)" = '{"backend":true,"docs":true}' ] && reason | grep -q "empty diff"; then pass "empty diff runs everything"; else fail "empty diff runs everything (areas=$(areas) reason=$(reason))"; fi

# 10. a file name with spaces is one path
new_repo t10; commit_change t10 sh -c 'echo x > "backend/my file.py"'
rc="$(run_compute t10 "$(head_before t10)")"
[ "$(areas)" = '{"backend":true,"docs":false}' ] && pass "file name with spaces" || fail "file name with spaces (areas=$(areas))"

# 11. a move counts for both the old and the new location
new_repo t11; commit_change t11 sh -c 'git mv backend/app.py docs/app.py'
rc="$(run_compute t11 "$(head_before t11)")"
[ "$(areas)" = '{"backend":true,"docs":true}' ] && pass "a move touches both areas" || fail "a move touches both areas (areas=$(areas))"

# 12. invalid filters fail instead of skipping everything
rc="$(run_compute t1 "$(head_before t1)" 'not json')"
if [ "$rc" != 0 ] && grep -q "filters is not a JSON object" "$work/out"; then pass "invalid filters fail"; else fail "invalid filters fail (rc=$rc)"; fi
rc="$(run_compute t1 "$(head_before t1)" '{"backend":[]}')"
[ "$rc" != 0 ] && pass "an area with no patterns fails" || fail "an area with no patterns fails"

# 13. invalid global patterns fail
rc="$(run_compute t1 "$(head_before t1)" "$filters" 'oops')"
if [ "$rc" != 0 ] && grep -q "global-patterns is not a JSON array of strings" "$work/out"; then pass "invalid global patterns fail"; else fail "invalid global patterns fail (rc=$rc)"; fi

# 13b. the reason and the areas are printed to the job log, not only to outputs
rc="$(run_compute t3 "$(head_before t3)")"
if grep -q "changes: diff of 2 file(s)" "$work/out" && grep -q 'changes: areas={"backend":true,"docs":true}' "$work/out"; then pass "reason and areas are logged"; else fail "reason and areas are logged"; fi
rc="$(run_compute t6 "")"
if grep -q "changes: no base commit" "$work/out"; then pass "fallback reason is logged"; else fail "fallback reason is logged"; fi

# 14. the summary lists every area
rc="$(run_compute t3 "$(head_before t3)")"
if grep -q "| backend | yes |" "$work/summary" && grep -q "| docs | yes |" "$work/summary"; then pass "summary lists every area"; else fail "summary lists every area"; fi

echo
if [ "$failures" -eq 0 ]; then echo "all changes tests passed"; else echo "$failures changes test(s) failed"; exit 1; fi
