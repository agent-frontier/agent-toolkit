#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO_BASE="$REPO_ROOT/demos/.workspaces"
WORKSPACE="$DEMO_BASE/health-recovery-$$-$(date +%s)"
STATE_DIR="$WORKSPACE/state"
JANITOR_SCRIPT="$REPO_ROOT/system-janitor.sh"

cleanup() {
  rm -rf "$WORKSPACE"
  rmdir "$DEMO_BASE" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$STATE_DIR"

python3 - "$STATE_DIR/janitor.jsonl" "$STATE_DIR/last-run.json" <<'PY'
import json
import sys
from pathlib import Path

jsonl_path = Path(sys.argv[1])
last_run_path = Path(sys.argv[2])
valid_events = []
for idx in range(1, 9):
    valid_events.append({
        "run_id": f"demo-run-{idx}",
        "ts": f"2026-05-{idx:02d}T03:17:00+0000",
        "host": "demo-host",
        "user": "demo",
        "section": "extra_cleanup",
        "status": "ok",
        "freed_kb": 128,
        "items": 1,
        "note": "seeded demo event",
    })

with jsonl_path.open("w", encoding="utf-8") as fh:
    for event in valid_events[:5]:
        fh.write(json.dumps(event) + "\n")
    fh.write("{this is not json\n")
    for event in valid_events[5:]:
        fh.write(json.dumps(event) + "\n")

last_run = {
    "run_id": "demo-run-8",
    "started_at": "2026-05-08T03:17:00+0000",
    "ended_at": "2026-05-08T03:18:00+0000",
    "finished": "2026-05-08T03:18:00+0000",
    "freed_kb": 128,
    "safety_integrity": "ok",
    "start_used_kb": 1000,
    "end_used_kb": 872,
    "dry_run": False,
    "sections": [
        {
            "name": "extra_cleanup",
            "status": "ok",
            "items": 1,
            "freed_bytes": 131072,
        }
    ],
}
last_run_path.write_text(json.dumps(last_run, indent=2) + "\n", encoding="utf-8")
PY

echo "Demo workspace: $WORKSPACE"
echo

echo '=== Step 1: Initial health check (expect degraded) ==='
set +e
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --health
health_rc=$?
set -e
printf 'exit_code=%s\n' "$health_rc"
[ "$health_rc" -eq 4 ]
set +e
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --health --json | jq .
health_rc=$?
set -e
[ "$health_rc" -eq 4 ]

echo

echo '=== Step 2: Acknowledge baseline ==='
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --health-acknowledge --json | jq .
printf 'baseline_file=%s\n' "$(cat "$STATE_DIR/.health-baseline")"

echo

echo '=== Step 3: Re-check health (expect healthy) ==='
set +e
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --health
health_rc=$?
set -e
printf 'exit_code=%s\n' "$health_rc"
[ "$health_rc" -eq 0 ]
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --health --json | jq .

echo

echo '=== Step 4: New malformed line should re-degrade ==='
printf '{still not json\n' >> "$STATE_DIR/janitor.jsonl"
set +e
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --health
health_rc=$?
set -e
printf 'exit_code=%s\n' "$health_rc"
[ "$health_rc" -eq 4 ]
set +e
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --health --json | jq .
health_rc=$?
set -e
[ "$health_rc" -eq 4 ]
