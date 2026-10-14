#!/usr/bin/env bash
# Tests for the preflight step of .github/workflows/preflight.yml.
# The script under test is extracted from the workflow, so the file that runs in CI is the file tested.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow="$root/.github/workflows/preflight.yml"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

yq -r '.jobs.preflight.steps[] | select(.id == "check") | .run' "$workflow" > "$work/check.sh"
[ -s "$work/check.sh" ] || { echo "FAIL: step 'check' not found in $workflow"; exit 1; }

# A PATH that holds only the tools the script itself needs, so each test decides what else exists.
base_bin="$work/base-bin"
mkdir -p "$base_bin"
for t in awk df; do
  real="$(command -v "$t")" || { echo "FAIL: host lacks $t"; exit 1; }
  printf '#!/bin/sh\nexec "%s" "$@"\n' "$real" > "$base_bin/$t"
  chmod +x "$base_bin/$t"
done

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }

# run_case <name> <tools-present> <env assignments...>; prints the exit code, leaves output in $work/out
run_case() {
  local present="$1"; shift
  local bin="$work/bin-$RANDOM"
  mkdir -p "$bin"
  cp "$base_bin"/* "$bin"/
  for t in $present; do
    case "$t" in
      docker-down) printf '#!/bin/sh\n[ "$1" = "info" ] && exit 1\nexit 0\n' > "$bin/docker" ;;
      *) printf '#!/bin/sh\nexit 0\n' > "$bin/$t" ;;
    esac
    chmod +x "$bin/${t/docker-down/docker}"
  done
  : > "$work/summary"
  env -i PATH="$bin" GITHUB_STEP_SUMMARY="$work/summary" "$@" /bin/bash "$work/check.sh" > "$work/out" 2>&1
  echo $?
}

printf 'MemAvailable:    4194304 kB\n' > "$work/meminfo-ok"
printf 'MemAvailable:     524288 kB\n' > "$work/meminfo-low"
common_ok=(MEMINFO="$work/meminfo-ok" DISK_PATH="$work" MIN_MEMORY_MB=2048 MIN_DISK_MB=1)

# 1. healthy runner passes
rc="$(run_case "git gh docker" REQUIRED_TOOLS="git gh docker" "${common_ok[@]}")"
[ "$rc" = 0 ] && pass "healthy runner passes" || { fail "healthy runner passes (rc=$rc)"; cat "$work/out"; }

# 2. a missing tool fails and the message names it
rc="$(run_case "git docker" REQUIRED_TOOLS="git gh docker" "${common_ok[@]}")"
if [ "$rc" != 0 ] && grep -q "missing tool: gh" "$work/out"; then pass "missing gh is named"; else fail "missing gh is named (rc=$rc)"; fi

# 3. the failure is labelled infra, in the annotation and in the summary
if grep -q "infra" "$work/out" && grep -q "failure class: infra" "$work/summary"; then pass "failure is labelled infra"; else fail "failure is labelled infra"; fi

# 4. low memory fails and names the numbers
rc="$(run_case "git" REQUIRED_TOOLS="git" MEMINFO="$work/meminfo-low" DISK_PATH="$work" MIN_MEMORY_MB=2048 MIN_DISK_MB=1)"
if [ "$rc" != 0 ] && grep -q "available memory 512 MB is below 2048 MB" "$work/out"; then pass "low memory is named"; else fail "low memory is named (rc=$rc)"; fi

# 5. low disk fails
rc="$(run_case "git" REQUIRED_TOOLS="git" MEMINFO="$work/meminfo-ok" DISK_PATH="$work" MIN_MEMORY_MB=1 MIN_DISK_MB=999999999)"
if [ "$rc" != 0 ] && grep -q "free disk" "$work/out"; then pass "low disk is named"; else fail "low disk is named (rc=$rc)"; fi

# 6. docker present but daemon unreachable fails
rc="$(run_case "git docker-down" REQUIRED_TOOLS="git docker" "${common_ok[@]}")"
if [ "$rc" != 0 ] && grep -q "docker daemon" "$work/out"; then pass "unreachable docker daemon is named"; else fail "unreachable docker daemon is named (rc=$rc)"; fi

# 7. every problem is reported in one run, not only the first
rc="$(run_case "" REQUIRED_TOOLS="git gh" MEMINFO="$work/meminfo-low" DISK_PATH="$work" MIN_MEMORY_MB=2048 MIN_DISK_MB=1)"
if [ "$rc" != 0 ] && grep -q "missing tool: git" "$work/out" && grep -q "missing tool: gh" "$work/out" && grep -q "available memory" "$work/out"; then
  pass "all problems reported together"; else fail "all problems reported together (rc=$rc)"; fi

# 8. a tool name that is not a plain word is rejected, never executed
rc="$(run_case "git" REQUIRED_TOOLS='git;touch /tmp/pwned' "${common_ok[@]}")"
if [ "$rc" != 0 ] && grep -q "invalid tool name" "$work/out"; then pass "invalid tool name is rejected"; else fail "invalid tool name is rejected (rc=$rc)"; fi

echo
if [ "$failures" -eq 0 ]; then echo "all preflight tests passed"; else echo "$failures preflight test(s) failed"; exit 1; fi
