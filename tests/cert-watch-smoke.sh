#!/usr/bin/env bash
# tests/cert-watch-smoke.sh — hermetic smoke test for cert-watch.sh.
#
# Exercises syntax, version/doctor/report/health machine modes, local and
# remote cert inventory, health acknowledgement, --only/--sections, and the
# capability-completeness contract. Uses only repo-local scratch dirs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/cert-watch.sh"
WORK="$REPO_ROOT/tests/.cert-watch-smoke.$$"
trap 'rm -rf "$WORK"' EXIT INT TERM
mkdir -p "$WORK"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; ASSERTIONS=$((ASSERTIONS + 1)); }
warn() { echo "WARN: $*" >&2; }
section() { echo; echo "$*"; }

ASSERTIONS=0

run_watch() {
  local log_dir="$1"
  shift
  CERT_WATCH_LOG_DIR="$log_dir" "$SCRIPT" "$@"
}

make_cert() {
  local cn="$1" days="$2" key="$3" cert="$4"
  openssl req -x509 -newkey rsa:2048 -days "$days" -keyout "$key" -out "$cert" -subj "/CN=$cn" -nodes >/dev/null 2>&1
}

make_expired_cert() {
  local cn="$1" key="$2" cert="$3"
  openssl genrsa -out "$key" 2048 >/dev/null 2>&1
  openssl x509 -new -key "$key" -out "$cert" -subj "/CN=$cn" -not_before 20240101000000Z -not_after 20240102000000Z >/dev/null 2>&1
}

pick_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

start_tls_server() {
  local cert="$1" key="$2" port="$3" pid_file="$4"
  openssl s_server -accept "$port" -cert "$cert" -key "$key" -quiet -www >"$WORK/s_server.out" 2>"$WORK/s_server.err" &
  echo $! > "$pid_file"
  sleep 1
}

stop_tls_server() {
  local pid_file="$1"
  if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file")"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    rm -f "$pid_file"
  fi
}

assert_report_shape() {
  local report_json="$1"
  python3 - "$report_json" <<'PY'
import json, sys
required = {"log_dir", "jsonl_path", "generated_at", "total_events", "total_runs", "real_runs", "dry_runs", "date_range", "total_certs_inventoried", "by_status", "expiring_soon", "per_source", "most_recent_run", "data_quality", "idle_streaks"}
data = json.load(open(sys.argv[1]))
missing = required - set(data)
assert not missing, missing
assert sorted(data["by_status"].keys()) == ["expired", "expiring", "ok", "warn"]
PY
}

assert_health_shape() {
  local health_json="$1"
  python3 - "$health_json" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1]))
assert obj["status"] in ("healthy", "degraded", "unknown")
assert obj["exit_code"] in (0, 4, 5)
checks = {c["name"] for c in obj["checks"]}
expected = {"log_dir_exists", "jsonl_present", "jsonl_parses", "last_run_parses", "last_run_integrity_ok", "last_run_parses_sections", "no_long_idle_streaks"}
assert checks == expected, (checks, expected)
PY
}

assert_doctor_shape() {
  local doctor_json="$1"
  python3 - "$doctor_json" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1]))
assert obj["status"] in ("healthy", "degraded", "unknown")
assert obj["exit_code"] in (0, 4, 5)
checks = {c["name"] for c in obj["checks"]}
expected = {"script_path_resolves", "cron_entry_present", "config_parses", "configured_targets_exist"}
assert checks == expected, (checks, expected)
PY
}

section "─── 01: syntax + shellcheck ─────────────────────────────────"
bash -n "$SCRIPT"
pass "bash -n clean"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -S warning "$SCRIPT"
  pass "shellcheck -S warning clean"
else
  warn "shellcheck not installed — skipping lint"
fi

section "─── 02: --version ───────────────────────────────────────────"
"$SCRIPT" --version > "$WORK/version.out"
grep -Eq '^cert-watch 0\.1\.0$' "$WORK/version.out" || fail "--version output mismatch: $(cat "$WORK/version.out")"
pass "--version exits 0 and prints exact banner"
"$SCRIPT" --version --json > "$WORK/version.json"
python3 - "$WORK/version.json" <<'PY' || fail "--version --json contract failed"
import json, sys
obj = json.load(open(sys.argv[1]))
expected = [
  "cert-hosts",
  "cert-paths",
  "cert-status-enum",
  "doctor",
  "doctor-json",
  "health",
  "health-acknowledge",
  "health-json",
  "only",
  "report",
  "report-json",
  "schemas",
  "sections",
  "toolkit-manifest",
  "version",
  "version-json",
]
assert obj["capabilities"] == expected, (obj["capabilities"], expected)
assert obj["toolkit"]["tools"] == ["cert-watch", "system-janitor", "system-updater"], obj["toolkit"]
PY
pass "--version --json exposes exact 16-capability set and 3-tool manifest"

section "─── 03: --doctor ────────────────────────────────────────────"
DOCTOR_LOG="$WORK/doctor-log"
run_watch "$DOCTOR_LOG" --doctor > "$WORK/doctor.out"
grep -q "cert-watch doctor: healthy (exit 0)" "$WORK/doctor.out" || fail "--doctor text output unexpected"
pass "--doctor exits 0 with no targets configured"
run_watch "$DOCTOR_LOG" --doctor --json > "$WORK/doctor.json"
assert_doctor_shape "$WORK/doctor.json"
pass "--doctor --json shape correct"

section "─── 04: dry-run baseline ─────────────────────────────────────"
DRY_LOG="$WORK/dry-log"
run_watch "$DRY_LOG" --dry-run >/dev/null
JSONL="$DRY_LOG/cert-watch.jsonl"
LATEST="$DRY_LOG/last-run.json"
[ -s "$JSONL" ] || fail "cert-watch.jsonl missing or empty"
[ -s "$LATEST" ] || fail "last-run.json missing or empty"
required_sections=(run_start local_certs remote_certs safety_integrity run_end)
for s in "${required_sections[@]}"; do
  count=$(grep -c "\"section\":\"${s}\"" "$JSONL" || true)
  [ "$count" -eq 1 ] || fail "section '$s' appeared $count times"
done
for s in local_certs remote_certs; do
  grep -q "\"section\":\"${s}\",\"status\":\"dry_run\"" "$JSONL" || fail "section '$s' did not report dry_run"
done
pass "--dry-run writes JSONL and required sections with dry_run status"

section "─── 05: report + health ──────────────────────────────────────"
run_watch "$DRY_LOG" --report > "$WORK/report.out"
grep -q "cert-watch — report" "$WORK/report.out" || fail "--report missing header"
pass "--report exits 0"
run_watch "$DRY_LOG" --report --json > "$WORK/report.json"
assert_report_shape "$WORK/report.json"
pass "--report --json shape correct"
run_watch "$DRY_LOG" --health --json > "$WORK/health.json"
assert_health_shape "$WORK/health.json"
pass "--health --json shape correct"

section "─── 06: health acknowledge ──────────────────────────────────"
run_watch "$DRY_LOG" --health-acknowledge --json > "$WORK/ack.json"
python3 - "$WORK/ack.json" <<'PY' || fail "--health-acknowledge --json keys missing"
import json, sys
obj = json.load(open(sys.argv[1]))
assert set(obj) == {"acknowledged", "baseline_bytes", "excluded_events"}
assert obj["acknowledged"] is True
PY
[ -f "$DRY_LOG/.health-baseline" ] || fail ".health-baseline not written"
pass "--health-acknowledge writes baseline and JSON keys"

section "─── 07: --only / --sections ─────────────────────────────────"
ONLY_LOG="$WORK/only-log"
run_watch "$ONLY_LOG" --only local_certs >/dev/null
count=$(grep -c '"section":"local_certs"' "$ONLY_LOG/cert-watch.jsonl" || true)
[ "$count" -eq 1 ] || fail "--only local_certs did not emit local_certs exactly once"
count=$(grep -c '"section":"remote_certs"' "$ONLY_LOG/cert-watch.jsonl" || true)
[ "$count" -eq 0 ] || fail "--only local_certs emitted remote_certs"
pass "--only filters sections"
SECTIONS_LOG="$WORK/sections-log"
run_watch "$SECTIONS_LOG" --sections remote_certs >/dev/null
count=$(grep -c '"section":"remote_certs"' "$SECTIONS_LOG/cert-watch.jsonl" || true)
[ "$count" -eq 1 ] || fail "--sections remote_certs did not emit remote_certs exactly once"
pass "--sections alias works"
set +e
run_watch "$WORK/bad-only" --only not_a_section > "$WORK/bad-only.out" 2> "$WORK/bad-only.err"
rc=$?
set -e
[ "$rc" -eq 3 ] || fail "unknown --only section exit=$rc (expected 3)"
pass "unknown --only name exits 3"

section "─── 08: local expiring cert ──────────────────────────────────"
LOCAL_LOG="$WORK/local-log"
make_cert test-expiring 1 "$WORK/local.key" "$WORK/local.crt"
CERT_WATCH_PATHS="$WORK/local.crt" run_watch "$LOCAL_LOG" >/dev/null
run_watch "$LOCAL_LOG" --report --json > "$WORK/local-report.json"
python3 - "$WORK/local-report.json" <<'PY' || fail "local expiring cert contract failed"
import json, sys
obj = json.load(open(sys.argv[1]))
assert obj["total_certs_inventoried"] == 1, obj
assert obj["by_status"]["expiring"] == 1, obj["by_status"]
assert obj["most_recent_run"]["aggregate_status"] == "warn", obj["most_recent_run"]
PY
pass "1-day local cert reports expiring and aggregate warn"

section "─── 09: expired cert ─────────────────────────────────────────"
EXPIRED_LOG="$WORK/expired-log"
make_expired_cert test-expired "$WORK/expired.key" "$WORK/expired.crt"
CERT_WATCH_PATHS="$WORK/expired.crt" run_watch "$EXPIRED_LOG" >/dev/null
run_watch "$EXPIRED_LOG" --report --json > "$WORK/expired-report.json"
python3 - "$WORK/expired-report.json" <<'PY' || fail "expired cert contract failed"
import json, sys
obj = json.load(open(sys.argv[1]))
assert obj["by_status"]["expired"] == 1, obj["by_status"]
assert obj["most_recent_run"]["aggregate_status"] == "error", obj["most_recent_run"]
PY
pass "expired cert reports expired and aggregate error"

section "─── 10: nonexistent path idles ───────────────────────────────"
IDLE_LOG="$WORK/idle-log"
CERT_WATCH_PATHS="$WORK/does-not-exist.crt" run_watch "$IDLE_LOG" >/dev/null
python3 - "$IDLE_LOG/cert-watch.jsonl" <<'PY' || fail "missing path did not yield local_certs idle"
import json, sys
found = None
for line in open(sys.argv[1]):
    obj = json.loads(line)
    if obj.get("section") == "local_certs":
        found = obj
        break
assert found is not None
assert found["status"] == "idle", found
PY
pass "missing literal path yields idle, not error"

section "─── 11: remote host probe ────────────────────────────────────"
REMOTE_LOG="$WORK/remote-log"
PORT="$(pick_port)"
PID_FILE="$WORK/server.pid"
start_tls_server "$WORK/local.crt" "$WORK/local.key" "$PORT" "$PID_FILE"
CERT_WATCH_HOSTS="localhost:$PORT" run_watch "$REMOTE_LOG" >/dev/null
stop_tls_server "$PID_FILE"
run_watch "$REMOTE_LOG" --report --json > "$WORK/remote-report.json"
python3 - "$WORK/remote-report.json" <<'PY' || fail "remote host probe contract failed"
import json, sys
obj = json.load(open(sys.argv[1]))
assert obj["total_certs_inventoried"] == 1, obj
source_rows = {row["name"]: row for row in obj["per_source"]}
assert source_rows["remote_certs"]["items_total"] == 1, source_rows
assert obj["expiring_soon"][0]["source"].startswith("localhost:"), obj["expiring_soon"]
PY
pass "remote host probe inventories remote certificate"

section "─── 12: schemas ──────────────────────────────────────────────"
python3 - <<'PY' || fail "cert-watch schemas do not parse as JSON"
import json
for path in (
    'schemas/cert-watch-report.schema.json',
    'schemas/cert-watch-health.schema.json',
):
    json.load(open(path))
PY
pass "schema files parse as JSON"

section "─── 13: capability completeness ──────────────────────────────"
python3 - "$WORK/version.json" <<'PY' > "$WORK/caps.txt"
import json, sys
for cap in json.load(open(sys.argv[1]))["capabilities"]:
    print(cap)
PY
EXPECTED_CAPS=(
  cert-hosts cert-paths cert-status-enum doctor doctor-json health
  health-acknowledge health-json only report report-json schemas
  sections toolkit-manifest version version-json
)
if ! diff <(printf '%s\n' "${EXPECTED_CAPS[@]}") "$WORK/caps.txt" >/dev/null; then
  fail "capability list drift"
fi
for cap in "${EXPECTED_CAPS[@]}"; do
  case "$cap" in
    cert-hosts) [ -f "$WORK/remote-report.json" ] || fail "$cap probe missing" ;;
    cert-paths) [ -f "$WORK/local-report.json" ] || fail "$cap probe missing" ;;
    cert-status-enum) grep -q 'aggregate_status' "$WORK/expired-report.json" || fail "$cap probe missing" ;;
    doctor) grep -q 'cert-watch doctor:' "$WORK/doctor.out" || fail "$cap probe missing" ;;
    doctor-json) assert_doctor_shape "$WORK/doctor.json" || fail "$cap probe missing" ;;
    health) [ -f "$WORK/health.json" ] || fail "$cap probe missing" ;;
    health-acknowledge) [ -f "$DRY_LOG/.health-baseline" ] || fail "$cap probe missing" ;;
    health-json) assert_health_shape "$WORK/health.json" || fail "$cap probe missing" ;;
    only) [ -f "$ONLY_LOG/cert-watch.jsonl" ] || fail "$cap probe missing" ;;
    report) grep -q 'cert-watch — report' "$WORK/report.out" || fail "$cap probe missing" ;;
    report-json) assert_report_shape "$WORK/report.json" || fail "$cap probe missing" ;;
    schemas) [ -f "$REPO_ROOT/schemas/cert-watch-report.schema.json" ] || fail "$cap probe missing" ;;
    sections) [ -f "$SECTIONS_LOG/cert-watch.jsonl" ] || fail "$cap probe missing" ;;
    toolkit-manifest) grep -q 'system-updater' "$WORK/version.json" || fail "$cap probe missing" ;;
    version) grep -q '^cert-watch 0\.1\.0$' "$WORK/version.out" || fail "$cap probe missing" ;;
    version-json) [ -f "$WORK/version.json" ] || fail "$cap probe missing" ;;
    *) fail "unexpected capability $cap" ;;
  esac
done
pass "capability completeness probes cover all 16 capabilities"

echo
echo "all cert-watch smoke checks passed (${ASSERTIONS} assertions)"
