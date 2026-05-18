#!/usr/bin/env bash
# demos/tour.sh — run all three demos back-to-back.
#
# Intended for agent onboarding: a single command that exercises the
# representative flows (cleanup, health recovery, agent discovery) against
# isolated temp state. Nothing on the host's real state is touched — each
# child demo creates and tears down its own workspace under
# `demos/.workspaces/`.
#
# Exit codes:
#   0  all three demos succeeded
#   1  one or more demos failed (last non-zero exit code is summarized)
#
# Usage:
#   ./demos/tour.sh           # run all three with default verbosity
#   ./demos/tour.sh --quiet   # suppress per-demo headers (still shows summary)

set -uo pipefail

QUIET=0
case "${1:-}" in
  --quiet|-q) QUIET=1 ;;
  --help|-h)
    sed -n '2,18p' "$0"
    exit 0
    ;;
  '') ;;
  *)
    echo "[ERROR] unknown argument: $1" >&2
    exit 2
    ;;
esac

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
DEMOS=(
  "01-cleanup-flow.sh"
  "02-health-recovery.sh"
  "03-agent-discovery.sh"
)

banner() {
  [ "$QUIET" = "1" ] && return 0
  printf '\n════════════════════════════════════════════════════════════════\n'
  printf '  %s\n' "$1"
  printf '════════════════════════════════════════════════════════════════\n'
}

failures=0
declare -a results
for demo in "${DEMOS[@]}"; do
  banner "running: $demo"
  if "$DEMO_DIR/$demo"; then
    results+=("ok    $demo")
  else
    rc=$?
    results+=("FAIL  $demo (exit $rc)")
    failures=$((failures + 1))
  fi
done

banner "tour summary"
for line in "${results[@]}"; do
  printf '  %s\n' "$line"
done

if [ "$failures" -gt 0 ]; then
  printf '\n[FAIL] %d demo(s) failed\n' "$failures" >&2
  exit 1
fi
printf '\n[OK] all %d demos passed\n' "${#DEMOS[@]}"
exit 0
