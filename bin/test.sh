#!/usr/bin/env bash
# The checks, described in docs/testing.md.
#   bin/test.sh quick [pytest args]   while working: Ruff on changed Python, the changed test files (last failures
#                                     first) and, when app/ changed, the app tests Vitest finds affected (all of
#                                     them when an app file was deleted). It is no verification: full is.
#                                     Arguments replace the test selection: bin/test.sh quick tests/test_x.py
#   bin/test.sh full [pytest args]    before merge: Ruff, every test with branch coverage, tests_private if present,
#                                     then the app's lint, typecheck, tests and build (FISCALM_TEST_APP=0 skips the app).
# FISCALM_TEST_BASE is what quick compares against (default origin/dev); FISCALM_TEST_WORKERS (default 2).
set -euo pipefail
cd "$(dirname "$0")/.."
mode=${1:-}
[ $# -gt 0 ] && shift
workers=${FISCALM_TEST_WORKERS:-2}
base=${FISCALM_TEST_BASE:-origin/dev}
run() { nice -n 10 "$@"; }
app_deps() {  # npm ci only when the lock file changed since the last install (bin/dev uses the same stamp)
  cmp -s app/package-lock.json app/node_modules/.fiscalm-lock && return
  (cd app && run npm ci --no-audit --no-fund) && cp app/package-lock.json app/node_modules/.fiscalm-lock
}
case "$mode" in
quick)
  git rev-parse --verify -q "$base^{commit}" >/dev/null || {
    echo "Base $base not found: run git fetch origin, or set FISCALM_TEST_BASE." >&2; exit 1; }
  # Every path changed against the base, deleted and renamed ones included, then untracked files. NUL-separated
  # in a file, not a pipe: names stay intact (newlines, non-ASCII) and a failing git stops the script.
  list=$(mktemp)
  trap 'rm -f "$list"' EXIT
  git diff --name-only -z --no-renames "$base" -- >"$list" || { echo 'git diff failed.' >&2; exit 1; }
  git ls-files -z --others --exclude-standard >>"$list" || { echo 'git ls-files failed.' >&2; exit 1; }
  python=() tests=() app=
  while IFS= read -r -d '' file; do
    case $file in
    app/*) app=${app:-changed}; [ -e "$file" ] || app=deleted ;;  # Vitest cannot trace a deleted file
    src/*.py | tests/*.py | tools/*.py | bin/*.py | bin/release)
      if [ -f "$file" ]; then python+=("$file"); fi  # tools need existing paths
      case $file in tests/test_*.py | tests/*/test_*.py) if [ -f "$file" ]; then tests+=("$file"); fi ;; esac ;;
    pyproject.toml) python=(src tests tools bin/*.py bin/release) ;;
    esac
  done <"$list"
  if [ ${#python[@]} -gt 0 ]; then
    run uv run ruff format --check "${python[@]}"
    run uv run ruff check "${python[@]}"
  fi
  if [ $# -gt 0 ]; then
    run uv run pytest -q --ff -n "$workers" "$@"
  elif [ ${#tests[@]} -gt 0 ]; then
    echo "Running only the ${#tests[@]} changed test files; bin/test.sh full verifies the change."
    run uv run pytest -q --ff -n "$workers" "${tests[@]}"
  else
    echo "No test file changed against $base, so no Python test ran; bin/test.sh full verifies the change."
    if grep -q '::' .pytest_cache/v/cache/lastfailed 2>/dev/null; then
      echo "Re-running the tests that failed last time."
      run uv run pytest -q --lf -n "$workers" tests
    fi
  fi
  if [ -n "$app" ]; then
    app_deps
    if [ "$app" = deleted ]; then
      (cd app && run npm test -- --maxWorkers="$workers")
    else
      (cd app && run npm test -- --changed "$base" --maxWorkers="$workers")
    fi
  fi
  ;;
full)
  run uv run ruff format --check src tests tools bin/*.py bin/release
  run uv run ruff check src tests tools bin/*.py bin/release
  run uv run pytest -q -n "$workers" --cov --cov-report= tests "$@"
  total=$(run uv run coverage report --format=total)
  echo "Engine coverage (lines and branches of src/fiscalm): $total%"
  if [ -d tests_private ]; then run uv run pytest -q tests_private "$@"; fi  # real-shaped fixtures, never in git
  [ "${FISCALM_TEST_APP:-1}" = 0 ] && exit 0
  app_deps
  (cd app && run npm run lint && run npm run typecheck && run npm test -- --maxWorkers="$workers" && run npm run build)
  ;;
*)
  echo "usage: bin/test.sh quick|full [pytest arguments]" >&2
  exit 2
  ;;
esac
