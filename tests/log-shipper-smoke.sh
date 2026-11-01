#!/usr/bin/env bash
# tests/log-shipper-smoke.sh — hermetic smoke test for log-shipper.sh.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/log-shipper.sh"
WORK="$REPO_ROOT/tests/.log-shipper-smoke.$$"
trap 'rm -rf "$WORK"' EXIT INT TERM
mkdir -p "$WORK"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; ASSERTIONS=$((ASSERTIONS + 1)); }
warn() { echo "WARN: $*" >&2; }
section() { echo; echo "$*"; }
ASSERTIONS=0

run_shipper() {
  local log_dir="$1"
  shift
  LOG_SHIPPER_LOG_DIR="$log_dir" "$SCRIPT" "$@"
}

make_fixture_sources() {
  local root="$1"
  mkdir -p "$root/janitor" "$root/system-updater" "$root/cert-watch"
  cat > "$root/janitor/janitor.jsonl" <<'EOF'
{"run_id":"j1","ts":"2026-05-18T10:00:00+0000","section":"run_start","status":"ok","items":0,"note":"dry_run=0"}
{"run_id":"j1","ts":"2026-05-18T10:00:01+0000","section":"docker_prune","status":"ok","freed_kb":1024,"items":2,"note":""}
{"run_id":"j1","ts":"2026-05-18T10:00:02+0000","section":"extra_cleanup","status":"ok","freed_kb":2048,"items":1,"note":""}
{"run_id":"j1","ts":"2026-05-18T10:00:03+0000","section":"safety_integrity","status":"ok","items":0,"note":"clean"}
{"run_id":"j1","ts":"2026-05-18T10:00:04+0000","section":"run_end","status":"ok","freed_kb":3072,"items":0,"note":"integrity=ok"}
EOF
  cat > "$root/system-updater/updater.jsonl" <<'EOF'
{"run_id":"u1","ts":"2026-05-18T11:00:00+0000","stage":"run_start","status":"ok","dry_run":0}
{"run_id":"u1","ts":"2026-05-18T11:00:01+0000","stage":"package","package":"pkg-clean","status":"ok"}
{"run_id":"u1","ts":"2026-05-18T11:00:02+0000","stage":"integrity","status":"ok"}
{"run_id":"u1","ts":"2026-05-18T11:00:03+0000","stage":"run_end","status":"ok"}
EOF
  cat > "$root/cert-watch/cert-watch.jsonl" <<'EOF'
{"run_id":"c1","ts":"2026-05-18T12:00:00+0000","section":"run_start","status":"ok","items":0,"note":"dry_run=0"}
{"run_id":"c1","ts":"2026-05-18T12:00:01+0000","section":"local_certs","status":"warn","items":1,"note":"","certs":[{"subject_cn":"example.com","issuer_cn":"issuer","not_before":"a","not_after":"b","days_remaining":4,"sha256_fingerprint":"AA","source":"/cert.pem","status":"expiring"}]}
{"run_id":"c1","ts":"2026-05-18T12:00:02+0000","section":"safety_integrity","status":"ok","items":0,"note":"clean"}
{"run_id":"c1","ts":"2026-05-18T12:00:03+0000","section":"run_end","status":"warn","items":1,"note":"integrity=ok","aggregate_status":"warn","total_certs_inventoried":1,"by_status":{"ok":0,"warn":0,"expiring":1,"expired":0},"dry_run":0}
EOF
}

assert_report_shape() {
  local report_json="$1"
  python3 - "$report_json" <<'PY'
import json, sys
required = {"log_dir", "jsonl_path", "generated_at", "total_events", "total_runs", "real_runs", "dry_runs", "total_sources", "sources", "by_tool", "outputs", "idle_streaks", "manifest_drift", "most_recent_run", "data_quality"}
data = json.load(open(sys.argv[1]))
missing = required - set(data)
assert not missing, missing
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
expected = {"script_path_resolves", "cron_entry_present", "config_parses", "configured_outputs_writable", "sources_discoverable"}
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

section "─── 02: fixtures ─────────────────────────────────────────────"
FIX="$WORK/fixtures"
make_fixture_sources "$FIX"
SOURCES="$FIX/janitor/janitor.jsonl:$FIX/system-updater/updater.jsonl:$FIX/cert-watch/cert-watch.jsonl"
pass "synthetic source logs created"

section "─── 03: --version ───────────────────────────────────────────"
"$SCRIPT" --version > "$WORK/version.out"
grep -Eq '^log-shipper 0\.2\.0$' "$WORK/version.out" || fail "--version output mismatch: $(cat "$WORK/version.out")"
pass "--version exits 0 and prints exact banner"
"$SCRIPT" --version --json > "$WORK/version.json"
python3 - "$WORK/version.json" <<'PY' || fail "--version --json contract failed"
import json, sys
obj = json.load(open(sys.argv[1]))
expected = [
  "auto-discover-sources",
  "doctor",
  "doctor-json",
  "health",
  "health-acknowledge",
  "health-json",
  "ndjson-export",
  "only",
  "otlp-json-export",
  "prometheus-textfile",
  "report",
  "report-json",
  "schemas",
  "sections",
  "toolkit-manifest",
  "version",
  "version-json",
]
assert obj["capabilities"] == expected, (obj["capabilities"], expected)
assert obj["toolkit"]["tools"] == ["cert-watch", "log-shipper", "system-janitor", "system-updater"], obj["toolkit"]
PY
pass "--version --json exposes exact 17-capability set and 4-tool manifest"

section "─── 04: --doctor ────────────────────────────────────────────"
DOCTOR_LOG="$WORK/doctor-log"
run_shipper "$DOCTOR_LOG" --doctor > "$WORK/doctor.out"
grep -q "log-shipper doctor: healthy (exit 0)" "$WORK/doctor.out" || fail "--doctor text output unexpected"
pass "--doctor exits 0 on clean config"
run_shipper "$DOCTOR_LOG" --doctor --json > "$WORK/doctor.json"
assert_doctor_shape "$WORK/doctor.json"
pass "--doctor --json shape correct"

section "─── 05: prom export ─────────────────────────────────────────"
PROM_LOG="$WORK/prom-log"
PROM_OUT="$WORK/out/agent_toolkit.prom"
mkdir -p "$WORK/out"
LOG_SHIPPER_SOURCES="$SOURCES" LOG_SHIPPER_PROM_OUT="$PROM_OUT" LOG_SHIPPER_LOOKBACK_HOURS=100000 run_shipper "$PROM_LOG" --prom >/dev/null
[ -s "$PROM_OUT" ] || fail ".prom output missing"
grep -q '^# HELP agent_toolkit_run_freed_bytes ' "$PROM_OUT" || fail "prom HELP missing"
grep -q '^# TYPE agent_toolkit_run_status_total counter$' "$PROM_OUT" || fail "prom TYPE missing"
grep -q 'agent_toolkit_run_freed_bytes{tool="janitor",section="docker_prune",run_id="j1"} 1048576' "$PROM_OUT" || fail "prom freed-bytes metric missing"
pass "--prom writes Prometheus textfile format"

section "─── 06: ndjson export ───────────────────────────────────────"
NDJSON_LOG="$WORK/ndjson-log"
LOG_SHIPPER_SOURCES="$SOURCES" LOG_SHIPPER_LOOKBACK_HOURS=100000 run_shipper "$NDJSON_LOG" --ndjson --out - > "$WORK/export.ndjson"
python3 - "$WORK/export.ndjson" <<'PY' || fail "ndjson output invalid"
import json, sys
lines = [line.rstrip("\n") for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
assert lines, "no ndjson lines"
objs = [json.loads(line) for line in lines]
assert all(isinstance(obj, dict) for obj in objs)
assert {obj.get("tool") for obj in objs} >= {"janitor", "updater", "cert-watch"}
assert all("shipped_at" in obj for obj in objs)
PY
pass "--ndjson emits one JSON object per line"

section "─── 07: otlp-json export ────────────────────────────────────"
OTLP_LOG="$WORK/otlp-log"
OTLP_OUT="$WORK/out/agent_toolkit-otlp.json"
LOG_SHIPPER_SOURCES="$SOURCES" LOG_SHIPPER_OTLP_JSON_OUT="$OTLP_OUT" LOG_SHIPPER_LOOKBACK_HOURS=100000 run_shipper "$OTLP_LOG" --otlp-json >/dev/null
python3 - "$OTLP_OUT" <<'PY' || fail "otlp output invalid"
import json, sys
obj = json.load(open(sys.argv[1]))
assert "resourceLogs" in obj and obj["resourceLogs"], obj
scope_logs = obj["resourceLogs"][0]["scopeLogs"]
assert scope_logs and scope_logs[0]["logRecords"], scope_logs
PY
pass "--otlp-json writes OTLP-shaped JSON"

section "─── 08: report + health ─────────────────────────────────────"
run_shipper "$PROM_LOG" --report > "$WORK/report.out"
grep -q 'log-shipper — report' "$WORK/report.out" || fail "--report missing header"
pass "--report exits 0"
run_shipper "$PROM_LOG" --report --json > "$WORK/report.json"
assert_report_shape "$WORK/report.json"
pass "--report --json shape correct"
run_shipper "$PROM_LOG" --health --json > "$WORK/health.json"
assert_health_shape "$WORK/health.json"
pass "--health --json shape correct"

section "─── 09: health acknowledge ──────────────────────────────────"
run_shipper "$PROM_LOG" --health-acknowledge --json > "$WORK/ack.json"
python3 - "$WORK/ack.json" <<'PY' || fail "--health-acknowledge --json keys missing"
import json, sys
obj = json.load(open(sys.argv[1]))
assert set(obj) == {"acknowledged", "baseline_bytes", "excluded_events"}
assert obj["acknowledged"] is True
PY
[ -f "$PROM_LOG/.health-baseline" ] || fail ".health-baseline not written"
pass "--health-acknowledge writes baseline and JSON keys"

section "─── 10: --only / --sections ────────────────────────────────"
ONLY_LOG="$WORK/only-log"
LOG_SHIPPER_SOURCES="$SOURCES" LOG_SHIPPER_PROM_OUT="$WORK/out/only.prom" LOG_SHIPPER_LOOKBACK_HOURS=100000 run_shipper "$ONLY_LOG" --only transform_emit --prom >/dev/null
count=$(grep -c '"section":"transform_emit"' "$ONLY_LOG/log-shipper.jsonl" || true)
[ "$count" -eq 1 ] || fail "--only transform_emit did not emit transform_emit exactly once"
count=$(grep -c '"section":"discover_sources"' "$ONLY_LOG/log-shipper.jsonl" || true)
[ "$count" -eq 0 ] || fail "--only transform_emit emitted discover_sources"
pass "--only filters sections"
SECTIONS_LOG="$WORK/sections-log"
LOG_SHIPPER_SOURCES="$SOURCES" run_shipper "$SECTIONS_LOG" --sections discover_sources >/dev/null
count=$(grep -c '"section":"discover_sources"' "$SECTIONS_LOG/log-shipper.jsonl" || true)
[ "$count" -eq 1 ] || fail "--sections discover_sources did not emit discover_sources exactly once"
pass "--sections alias works"
set +e
run_shipper "$WORK/bad-only" --only not_a_section > "$WORK/bad-only.out" 2> "$WORK/bad-only.err"
rc=$?
set -e
[ "$rc" -eq 3 ] || fail "unknown --only section exit=$rc (expected 3)"
pass "unknown --only name exits 3"

section "─── 11: auto-discover from manifest ────────────────────────"
AUTO_HOME="$WORK/auto-home"
AUTO_CONFIG="$AUTO_HOME/.config/agent-toolkit"
AUTO_STATE="$AUTO_HOME/.local/state"
mkdir -p "$AUTO_CONFIG" "$AUTO_STATE/janitor" "$AUTO_STATE/system-updater" "$AUTO_STATE/cert-watch"
cp "$FIX/janitor/janitor.jsonl" "$AUTO_STATE/janitor/janitor.jsonl"
cp "$FIX/system-updater/updater.jsonl" "$AUTO_STATE/system-updater/updater.jsonl"
cp "$FIX/cert-watch/cert-watch.jsonl" "$AUTO_STATE/cert-watch/cert-watch.jsonl"
cat > "$AUTO_CONFIG/manifest.json" <<EOF
{
  "name": "agent-toolkit",
  "version": "0.2.0",
  "tools": [
    {"name": "cert-watch"},
    {"name": "log-shipper"},
    {"name": "system-janitor"},
    {"name": "system-updater"}
  ]
}
EOF
AUTO_LOG="$WORK/auto-log"
HOME="$AUTO_HOME" XDG_CONFIG_HOME="$AUTO_HOME/.config" XDG_STATE_HOME="$AUTO_HOME/.local/state" LOG_SHIPPER_LOG_DIR="$AUTO_LOG" "$SCRIPT" --report --json > "$WORK/auto-empty-report.json"
HOME="$AUTO_HOME" XDG_CONFIG_HOME="$AUTO_HOME/.config" XDG_STATE_HOME="$AUTO_HOME/.local/state" LOG_SHIPPER_LOG_DIR="$AUTO_LOG" LOG_SHIPPER_PROM_OUT="$WORK/out/auto.prom" "$SCRIPT" --prom >/dev/null
python3 - "$AUTO_LOG/log-shipper.jsonl" <<'PY' || fail "auto-discover did not find 3 sources"
import json, sys
found = None
for line in open(sys.argv[1], encoding='utf-8'):
    obj = json.loads(line)
    if obj.get('section') == 'discover_sources':
        found = obj
assert found is not None
assert found['total_sources'] == 3, found
PY
pass "manifest auto-discovery finds sibling toolkit sources"

section "─── 12: schemas ──────────────────────────────────────────────"
python3 - <<'PY' || fail "log-shipper schemas do not parse as JSON"
import json
for path in (
    'schemas/log-shipper-report.schema.json',
    'schemas/log-shipper-health.schema.json',
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
  auto-discover-sources doctor doctor-json health health-acknowledge
  health-json ndjson-export only otlp-json-export prometheus-textfile
  report report-json schemas sections toolkit-manifest version version-json
)
if ! diff <(printf '%s\n' "${EXPECTED_CAPS[@]}") "$WORK/caps.txt" >/dev/null; then
  fail "capability list drift"
fi
for cap in "${EXPECTED_CAPS[@]}"; do
  case "$cap" in
    auto-discover-sources) [ -f "$WORK/out/auto.prom" ] || fail "$cap probe missing" ;;
    doctor) grep -q 'log-shipper doctor:' "$WORK/doctor.out" || fail "$cap probe missing" ;;
    doctor-json) assert_doctor_shape "$WORK/doctor.json" || fail "$cap probe missing" ;;
    health) [ -f "$WORK/health.json" ] || fail "$cap probe missing" ;;
    health-acknowledge) [ -f "$PROM_LOG/.health-baseline" ] || fail "$cap probe missing" ;;
    health-json) assert_health_shape "$WORK/health.json" || fail "$cap probe missing" ;;
    ndjson-export) [ -f "$WORK/export.ndjson" ] || fail "$cap probe missing" ;;
    only) [ -f "$ONLY_LOG/log-shipper.jsonl" ] || fail "$cap probe missing" ;;
    otlp-json-export) [ -f "$OTLP_OUT" ] || fail "$cap probe missing" ;;
    prometheus-textfile) [ -f "$PROM_OUT" ] || fail "$cap probe missing" ;;
    report) grep -q 'log-shipper — report' "$WORK/report.out" || fail "$cap probe missing" ;;
    report-json) assert_report_shape "$WORK/report.json" || fail "$cap probe missing" ;;
    schemas) [ -f "$REPO_ROOT/schemas/log-shipper-report.schema.json" ] || fail "$cap probe missing" ;;
    sections) [ -f "$SECTIONS_LOG/log-shipper.jsonl" ] || fail "$cap probe missing" ;;
    toolkit-manifest) grep -q 'log-shipper' "$WORK/version.json" || fail "$cap probe missing" ;;
    version) grep -q '^log-shipper 0\.2\.0$' "$WORK/version.out" || fail "$cap probe missing" ;;
    version-json) [ -f "$WORK/version.json" ] || fail "$cap probe missing" ;;
    *) fail "unexpected capability $cap" ;;
  esac
done
pass "capability completeness probes cover all 17 capabilities"

echo
echo "all log-shipper smoke checks passed (${ASSERTIONS} assertions)"
