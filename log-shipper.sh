#!/usr/bin/env bash
# log-shipper — read-only audit-trail exporter for agent-toolkit.
#
# Reads append-only JSONL audit logs produced by sibling toolkit tools and
# re-emits them into monitoring-friendly static exports:
#   - Prometheus textfile collector (.prom)
#   - NDJSON event stream
#   - OTLP-shaped JSON
#
# Configuration:
#   ~/.config/log-shipper/config       (XDG_CONFIG_HOME/log-shipper/config)
# or
#   --config <path>                    (override on command line)
#
# Environment variables also accepted; see --help and
# examples/log-shipper.config.example.

set -uo pipefail

# ── Version ────────────────────────────────────────────────────────────
readonly VERSION="0.2.0"
readonly TOOLKIT_NAME="agent-toolkit"
readonly TOOLKIT_REPO="https://github.com/agent-frontier/agent-toolkit"
readonly -a TOOLKIT_TOOLS=(cert-watch log-shipper system-janitor system-updater)
# ── end Version ────────────────────────────────────────────────────────

# ── Default cron-safe environment ──────────────────────────────────────
export PATH="${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
[ -z "${HOME:-}" ] && { echo "[FATAL] HOME unset" >&2; exit 3; }

# ── XDG paths ──────────────────────────────────────────────────────────
: "${XDG_CONFIG_HOME:=${HOME}/.config}"
: "${XDG_STATE_HOME:=${HOME}/.local/state}"

DEFAULT_CONFIG="${XDG_CONFIG_HOME}/log-shipper/config"
MANIFEST_PATH_DEFAULT="${XDG_CONFIG_HOME}/agent-toolkit/manifest.json"

# ── CLI flags ──────────────────────────────────────────────────────────
CONFIG_FILE=""
DRY_RUN="${LOG_SHIPPER_DRY_RUN:-0}"
SHOW_HELP=0
DO_REPORT=0
REPORT_JSON=0
# ── --health flag ──────────────────────────────────────────────────────
DO_HEALTH=0
# ── end --health flag ──────────────────────────────────────────────────
# ── --doctor flag ──────────────────────────────────────────────────────
DO_DOCTOR=0
# ── end --doctor flag ──────────────────────────────────────────────────
# ── --health-acknowledge flag ──────────────────────────────────────────
DO_HEALTH_ACK=0
# ── end --health-acknowledge flag ──────────────────────────────────────
# ── --version flag ─────────────────────────────────────────────────────
DO_VERSION=0
# ── end --version flag ─────────────────────────────────────────────────
# ── --only flag ────────────────────────────────────────────────────────
ONLY_SECTIONS=()
# ── end --only flag ────────────────────────────────────────────────────
# ── export-mode flags ──────────────────────────────────────────────────
MODE_PROM=0
MODE_NDJSON=0
MODE_OTLP_JSON=0
OUTPUT_OVERRIDE=""
# ── end export-mode flags ──────────────────────────────────────────────

while [ $# -gt 0 ]; do
  case "$1" in
    --config)     CONFIG_FILE="$2"; shift 2 ;;
    --config=*)   CONFIG_FILE="${1#*=}"; shift ;;
    --dry-run|-n) DRY_RUN=1; shift ;;
    --report)     DO_REPORT=1; shift ;;
    # ── --health flag ──
    --health)     DO_HEALTH=1; shift ;;
    # ── end --health flag ──
    # ── --doctor flag ──
    --doctor)     DO_DOCTOR=1; shift ;;
    # ── end --doctor flag ──
    # ── --health-acknowledge flag ──
    --health-acknowledge) DO_HEALTH_ACK=1; shift ;;
    # ── end --health-acknowledge flag ──
    # ── --version flag ──
    --version)    DO_VERSION=1; shift ;;
    # ── end --version flag ──
    --json)       REPORT_JSON=1; shift ;;
    # ── --only flag ──
    --only|--sections)
      [ $# -ge 2 ] || { echo "[ERROR] $1 requires a value" >&2; exit 3; }
      IFS=',' read -r -a _only_parts <<< "$2"
      for _p in "${_only_parts[@]}"; do
        [ -n "$_p" ] && ONLY_SECTIONS+=("$_p")
      done
      shift 2 ;;
    --only=*|--sections=*)
      IFS=',' read -r -a _only_parts <<< "${1#*=}"
      for _p in "${_only_parts[@]}"; do
        [ -n "$_p" ] && ONLY_SECTIONS+=("$_p")
      done
      shift ;;
    # ── end --only flag ──
    # ── export-mode flags ──
    --prom)      MODE_PROM=1; shift ;;
    --ndjson)    MODE_NDJSON=1; shift ;;
    --otlp-json) MODE_OTLP_JSON=1; shift ;;
    --out)
      [ $# -ge 2 ] || { echo "[ERROR] --out requires a value" >&2; exit 3; }
      OUTPUT_OVERRIDE="$2"; shift 2 ;;
    --out=*)
      OUTPUT_OVERRIDE="${1#*=}"; shift ;;
    # ── end export-mode flags ──
    --help|-h)    SHOW_HELP=1; shift ;;
    *) echo "[ERROR] unknown flag: $1" >&2; SHOW_HELP=1; shift ;;
  esac
done

# ── --only flag: canonical section list & validation ──────────────────
KNOWN_SECTIONS=(discover_sources transform_emit manifest_sync)

if [ ${#ONLY_SECTIONS[@]} -gt 0 ]; then
  _unknown=()
  for _s in "${ONLY_SECTIONS[@]}"; do
    _found=0
    for _k in "${KNOWN_SECTIONS[@]}"; do
      [ "$_k" = "$_s" ] && { _found=1; break; }
    done
    [ "$_found" -eq 0 ] && _unknown+=("$_s")
  done
  if [ ${#_unknown[@]} -gt 0 ]; then
    _join_unknown="" _join_valid="" _sep=""
    for _s in "${_unknown[@]}"; do
      _join_unknown+="${_sep}${_s}"; _sep=", "
    done
    _sep=""
    for _s in "${KNOWN_SECTIONS[@]}"; do
      _join_valid+="${_sep}${_s}"; _sep=", "
    done
    echo "[ERROR] unknown section(s): ${_join_unknown}. Valid: ${_join_valid}" >&2
    exit 3
  fi
fi

_should_run_section() {
  [ ${#ONLY_SECTIONS[@]} -eq 0 ] && return 0
  local s
  for s in "${ONLY_SECTIONS[@]}"; do
    [ "$s" = "$1" ] && return 0
  done
  return 1
}
# ── end --only flag ────────────────────────────────────────────────────

if [ "$REPORT_JSON" = 1 ] && [ "$DO_REPORT" != 1 ] && [ "$DO_HEALTH" != 1 ] && [ "$DO_DOCTOR" != 1 ] && [ "$DO_VERSION" != 1 ] && [ "$DO_HEALTH_ACK" != 1 ]; then
  echo "[ERROR] --json requires --report, --health, --doctor, --health-acknowledge, or --version" >&2
  exit 3
fi

_selected_mode_count=$((MODE_PROM + MODE_NDJSON + MODE_OTLP_JSON))
if [ -n "$OUTPUT_OVERRIDE" ] && [ "$_selected_mode_count" -gt 1 ]; then
  echo "[ERROR] --out may only be used with a single export mode" >&2
  exit 3
fi

if [ "$SHOW_HELP" = 1 ]; then
  cat <<'USAGE'
log-shipper — read-only audit-trail exporter for agent-toolkit.

USAGE:
  log-shipper [--config <path>] [--dry-run] [--only <list>]
              [--prom|--ndjson|--otlp-json] [--out <path>]
              [--report [--json]] [--health [--json]]
              [--doctor [--json]] [--health-acknowledge [--json]]
              [--version [--json]] [--help]

FLAGS:
  --config <path>   Source <path> for configuration (default:
                    $XDG_CONFIG_HOME/log-shipper/config)
  --dry-run, -n     Compute discovery and transform results but do not write
                    any export output files. Action sections still emit
                    status=dry_run in log-shipper.jsonl.
  --only <list>     Run ONLY the named section(s); skip all others silently.
                    <list> is comma-separated, no spaces. Valid names:
                    discover_sources, transform_emit, manifest_sync.
                    Execution order follows the script declaration order, NOT
                    argv order. --sections is accepted as a synonym.
  --prom            Emit Prometheus textfile output. Destination comes from
                    LOG_SHIPPER_PROM_OUT or --out <path>.
  --ndjson          Emit NDJSON. Destination comes from LOG_SHIPPER_NDJSON_OUT,
                    or defaults to stdout when no destination is configured.
  --otlp-json       Emit OTLP-shaped JSON. Destination comes from
                    LOG_SHIPPER_OTLP_JSON_OUT or --out <path>.
  --out <path>      Override the destination for the selected export mode.
                    May be used only when exactly one of --prom / --ndjson /
                    --otlp-json is selected. Use --out - for NDJSON stdout.
  --report          Print a human-readable summary of past runs from
                    $LOG_SHIPPER_LOG_DIR/log-shipper.jsonl and exit 0.
                    Read-only: no lock, no log-dir mutation.
  --health          Probe the audit trail and exit:
                      0  healthy
                      4  degraded
                      5  unknown (never run here)
  --doctor          Probe log-shipper self-wiring: script path resolution,
                    cron wiring, config syntax, configured output paths, and
                    source discovery shape. Exit 0/4/5 like --health.
  --health-acknowledge
                    Record the current size of log-shipper.jsonl as a baseline
                    so future --health probes ignore historical malformed
                    lines. Atomic tmp+rename. Pair with --json for a
                    machine-readable response.
  --json            Modifier for --report / --health / --doctor /
                    --health-acknowledge / --version. ASCII-safe JSON.
  --version         Print version. Pair with --json for the capabilities[]
                    feature-detection surface.
  --help, -h        Show this help and exit.

CONFIG (sourced as bash):
  LOG_SHIPPER_LOG_DIR       state directory
                            (default: $XDG_STATE_HOME/log-shipper)
  LOG_SHIPPER_SOURCES       colon-separated source JSONL paths or glob
                            patterns. Empty by default; when empty the tool
                            auto-discovers toolkit audit logs from
                            ~/.config/agent-toolkit/manifest.json.
  LOG_SHIPPER_PROM_OUT      path to Prometheus textfile output (.prom)
  LOG_SHIPPER_NDJSON_OUT    path to NDJSON output, or - for stdout
  LOG_SHIPPER_OTLP_JSON_OUT path to OTLP-shaped JSON output
  LOG_SHIPPER_LOOKBACK_HOURS
                            ignore events older than this many hours
                            (default: 24)
  LOG_SHIPPER_SAFETY_FLOOR_DIRS
                            colon-separated dirs whose inode+size must not
                            change during the run

SECTIONS:
  discover_sources  enumerate readable source JSONL files
  transform_emit    read source events, transform, and emit configured outputs
  manifest_sync     compare discovered sources to manifest tool entries
  safety_integrity  verify configured safety-floor dirs were undisturbed

EXIT CODES:
  0   success (default run, --report, --version, healthy doctor/health)
  1   another instance running (lock held)
  2   integrity violation (a configured safety-floor dir was disturbed)
  3   precondition failed (HOME unset, config syntax error, unknown section,
      invalid output-mode combination, or --json used without a read-only flag)
  4   --health/--doctor: degraded
  5   --health/--doctor: unknown (foundational signal missing)

EXAMPLES:
  # Quarter-hourly cron export (installer default: minutes 7,22,37,52)
  7,22,37,52 * * * * "$HOME/.local/bin/agent-toolkit-cron" "$HOME/.local/bin/log-shipper"

  # One-shot Prometheus export
  LOG_SHIPPER_SOURCES="$HOME/.local/state/janitor/janitor.jsonl" \
  LOG_SHIPPER_PROM_OUT="$HOME/agent-toolkit.prom" \
  log-shipper --prom

  # One-shot NDJSON to stdout
  LOG_SHIPPER_SOURCES="$HOME/.local/state/janitor/janitor.jsonl" \
  log-shipper --ndjson --out -

  # Machine-readable summary
  log-shipper --report --json

  # Feature-detection probe
  log-shipper --version --json

LOGS:
  $LOG_SHIPPER_LOG_DIR/log-shipper.log        human-readable
  $LOG_SHIPPER_LOG_DIR/log-shipper.jsonl      one JSON event per section (append-only)
  $LOG_SHIPPER_LOG_DIR/last-run.json          latest summary (atomic)
  $LOG_SHIPPER_LOG_DIR/.health-baseline       byte offset baseline for --health
USAGE
  exit 0
fi

# ── Load configuration ────────────────────────────────────────────────
load_config() {
  local f="${CONFIG_FILE:-$DEFAULT_CONFIG}" err
  CONFIG_PARSE_ERROR=""
  if [ -f "$f" ]; then
    if [ "${DO_DOCTOR:-0}" = 1 ]; then
      err="$(bash -n "$f" 2>&1)" || {
        CONFIG_LOADED="$f"
        CONFIG_PARSE_ERROR="$err"
        return 0
      }
    fi
    # shellcheck disable=SC1090
    if ! source "$f"; then
      echo "[FATAL] config file invalid: $f" >&2
      exit 3
    fi
    CONFIG_LOADED="$f"
  else
    CONFIG_LOADED=""
  fi
}
load_config

LOG_SHIPPER_LOG_DIR="${LOG_SHIPPER_LOG_DIR:-${XDG_STATE_HOME}/log-shipper}"
LOG_SHIPPER_SOURCES="${LOG_SHIPPER_SOURCES:-}"
LOG_SHIPPER_PROM_OUT="${LOG_SHIPPER_PROM_OUT:-}"
LOG_SHIPPER_NDJSON_OUT="${LOG_SHIPPER_NDJSON_OUT:-}"
LOG_SHIPPER_OTLP_JSON_OUT="${LOG_SHIPPER_OTLP_JSON_OUT:-}"
LOG_SHIPPER_LOOKBACK_HOURS="${LOG_SHIPPER_LOOKBACK_HOURS:-24}"
LOG_SHIPPER_SAFETY_FLOOR_DIRS="${LOG_SHIPPER_SAFETY_FLOOR_DIRS:-}"
LOG_SHIPPER_MANIFEST_PATH="${LOG_SHIPPER_MANIFEST_PATH:-$MANIFEST_PATH_DEFAULT}"

# ── Read-only report (must run BEFORE mkdir/flock/exec-redirect) ──────
do_report() {
  local jsonl="${LOG_SHIPPER_LOG_DIR}/log-shipper.jsonl"
  local mode="text"
  [ "$REPORT_JSON" = 1 ] && mode="json"

  REPORT_MODE="$mode" REPORT_LOG_DIR="$LOG_SHIPPER_LOG_DIR" REPORT_JSONL="$jsonl" \
    python3 - <<'PY'
import datetime, json, os, sys
from collections import defaultdict

mode = os.environ["REPORT_MODE"]
log_dir = os.environ["REPORT_LOG_DIR"]
path = os.environ["REPORT_JSONL"]
generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def emit_json(obj):
    sys.stdout.write(json.dumps(obj, indent=2))
    sys.stdout.write("\n")


def zero_report():
    return {
        "log_dir": log_dir,
        "jsonl_path": path,
        "generated_at": generated_at,
        "total_events": 0,
        "total_runs": 0,
        "real_runs": 0,
        "dry_runs": 0,
        "total_sources": 0,
        "sources": [],
        "by_tool": {},
        "outputs": [],
        "idle_streaks": [],
        "manifest_drift": [],
        "most_recent_run": None,
        "data_quality": {"invalid_lines": 0, "examples": []},
    }


if not os.path.isfile(path):
    out = zero_report()
    if mode == "json":
        emit_json(out)
    else:
        print("log-shipper — report")
        print(f"  log dir : {log_dir}")
        print()
        print(f"no runs found at {path}")
    sys.exit(0)


events = []
malformed = []
with open(path, "r", errors="replace") as fh:
    for lineno, raw in enumerate(fh, 1):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except Exception as e:
            malformed.append((lineno, str(e)[:120]))
            continue
        events.append(obj)

run_ids = []
seen = set()
for ev in events:
    rid = ev.get("run_id")
    if rid and rid not in seen:
        seen.add(rid)
        run_ids.append(rid)

dry_run_ids = set()
real_run_ids = set()
for ev in events:
    if ev.get("section") == "run_start":
        rid = ev.get("run_id")
        note = str(ev.get("note", "") or "")
        if "dry_run=1" in note:
            dry_run_ids.add(rid)
        else:
            real_run_ids.add(rid)
real_run_ids |= (set(run_ids) - dry_run_ids - real_run_ids)

latest_transform = None
latest_discover = None
latest_manifest = None
latest_run_end = None
transform_real = []
for ev in events:
    sec = ev.get("section")
    if sec == "discover_sources":
        if latest_discover is None or ev.get("ts", "") >= latest_discover.get("ts", ""):
            latest_discover = ev
    elif sec == "transform_emit":
        if latest_transform is None or ev.get("ts", "") >= latest_transform.get("ts", ""):
            latest_transform = ev
        if ev.get("run_id") in real_run_ids:
            transform_real.append(ev)
    elif sec == "manifest_sync":
        if latest_manifest is None or ev.get("ts", "") >= latest_manifest.get("ts", ""):
            latest_manifest = ev
    elif sec == "run_end":
        if latest_run_end is None or ev.get("ts", "") >= latest_run_end.get("ts", ""):
            latest_run_end = ev

idle_streaks = []
trailing = 0
last_productive = None
for ev in sorted(transform_real, key=lambda e: e.get("ts", "")):
    status = ev.get("status", "")
    try:
        items = int(ev.get("items") or 0)
    except (TypeError, ValueError):
        items = 0
    productive = status not in ("idle", "dry_run") and items > 0
    if productive:
        trailing = 0
        last_productive = {"run_id": ev.get("run_id"), "ts": ev.get("ts")}
    elif status == "idle":
        trailing += 1
    else:
        trailing = 0
if trailing >= 2:
    idle_streaks.append({
        "section": "transform_emit",
        "consecutive_idle_runs": trailing,
        "last_productive_run": last_productive,
    })

out = zero_report()
out["total_events"] = len(events)
out["total_runs"] = len(run_ids)
out["real_runs"] = len(real_run_ids)
out["dry_runs"] = len(dry_run_ids)
if latest_transform is not None:
    out["total_sources"] = int(latest_transform.get("total_sources") or 0)
    out["sources"] = latest_transform.get("sources") or []
    out["by_tool"] = latest_transform.get("by_tool") or {}
    out["outputs"] = latest_transform.get("outputs") or []
elif latest_discover is not None:
    out["total_sources"] = int(latest_discover.get("total_sources") or 0)
    out["sources"] = latest_discover.get("sources") or []
if latest_manifest is not None:
    out["manifest_drift"] = latest_manifest.get("manifest_drift") or []
out["idle_streaks"] = idle_streaks
out["most_recent_run"] = None if latest_run_end is None else {
    "run_id": latest_run_end.get("run_id"),
    "finished": latest_run_end.get("ts"),
    "status": latest_run_end.get("status"),
    "dry_run": int(latest_run_end.get("dry_run") or 0),
    "total_sources": int(latest_run_end.get("total_sources") or 0),
    "total_events_shipped": int(latest_run_end.get("total_events_shipped") or 0),
}
out["data_quality"] = {
    "invalid_lines": len(malformed),
    "examples": [{"line": ln, "error": err} for ln, err in malformed[:3]],
}

if mode == "json":
    emit_json(out)
    sys.exit(0)

print("log-shipper — report")
print(f"  log dir : {log_dir}")
print(f"  events  : {len(events)} (across {len(run_ids)} runs)")
print(f"  real runs: {len(real_run_ids)}    dry runs: {len(dry_run_ids)}")
print(f"  total sources: {out['total_sources']}")
print()
print("Sources:")
if out["sources"]:
    for row in out["sources"]:
        print(f"  {row.get('tool','?'):<11} {row.get('events_in_window',0):>6} events  {row.get('path','?')}")
else:
    print("  (none)")
print()
print("Outputs:")
if out["outputs"]:
    for row in out["outputs"]:
        print(f"  {row.get('format','?'):<10} {row.get('bytes',0):>8} bytes  {row.get('path','?')}")
else:
    print("  (none)")
if out["manifest_drift"]:
    print()
    print("Manifest drift:")
    for item in out["manifest_drift"]:
        print(f"  - {item}")
if idle_streaks:
    print()
    print("Idle sections:")
    for row in idle_streaks:
        print(f"  {row['section']}: {row['consecutive_idle_runs']} consecutive idle real runs")
print()
print("Most recent run:")
if out["most_recent_run"] is None:
    print("  (none)")
else:
    mr = out["most_recent_run"]
    print(f"  {mr['run_id']}  {mr['finished']}  status={mr['status']}  events={mr['total_events_shipped']}")
print()
print("Data-quality issues:")
if malformed:
    print(f"  {len(malformed)} malformed line(s):")
    for ln, err in malformed[:5]:
        print(f"    line {ln}: {err[:60]}")
else:
    print("  (none)")
PY
}

if [ "$DO_REPORT" = 1 ]; then
  do_report
  exit 0
fi

# ── --version flag ─────────────────────────────────────────────────────
do_version() {
  if [ "$REPORT_JSON" = 1 ]; then
    local toolkit_tools_nl
    toolkit_tools_nl="$(printf '%s\n' "${TOOLKIT_TOOLS[@]}")"
    VERSION_ENV="$VERSION" \
    TOOLKIT_NAME_ENV="$TOOLKIT_NAME" \
    TOOLKIT_REPO_ENV="$TOOLKIT_REPO" \
    TOOLKIT_TOOLS_ENV="$toolkit_tools_nl" \
    python3 - <<'PY'
import json, os
print(json.dumps({
    "name": "log-shipper",
    "version": os.environ["VERSION_ENV"],
    "capabilities": sorted([
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
        "version-json"
    ]),
    "toolkit": {
        "name": os.environ["TOOLKIT_NAME_ENV"],
        "repo": os.environ["TOOLKIT_REPO_ENV"],
        "tools": [tool for tool in os.environ["TOOLKIT_TOOLS_ENV"].splitlines() if tool]
    }
}, indent=2))
PY
  else
    printf 'log-shipper %s\n' "$VERSION"
  fi
}

if [ "$DO_VERSION" = 1 ]; then
  do_version
  exit 0
fi
# ── end --version flag ─────────────────────────────────────────────────

# ── --health flag ──────────────────────────────────────────────────────
do_health() {
  local mode="text"
  [ "$REPORT_JSON" = 1 ] && mode="json"
  local log_dir="$LOG_SHIPPER_LOG_DIR"
  local jsonl="${log_dir}/log-shipper.jsonl"
  local last_run="${log_dir}/last-run.json"
  local baseline_file="${log_dir}/.health-baseline"

  HEALTH_MODE="$mode" \
  HEALTH_LOG_DIR="$log_dir" \
  HEALTH_JSONL="$jsonl" \
  HEALTH_LAST_RUN="$last_run" \
  HEALTH_BASELINE_FILE="$baseline_file" \
    python3 - <<'PY'
import datetime, json, os, sys

mode = os.environ["HEALTH_MODE"]
log_dir = os.environ["HEALTH_LOG_DIR"]
jsonl = os.environ["HEALTH_JSONL"]
last_run = os.environ["HEALTH_LAST_RUN"]
baseline_file = os.environ["HEALTH_BASELINE_FILE"]
generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
checks = []


def add(name, ok, detail=None):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


log_dir_ok = os.path.isdir(log_dir)
add("log_dir_exists", log_dir_ok, log_dir if log_dir_ok else f"{log_dir} (missing)")

baseline = 0
if log_dir_ok and os.path.isfile(baseline_file):
    try:
        with open(baseline_file, "r", errors="replace") as fh:
            baseline = int((fh.read().strip() or "0"))
        if baseline < 0:
            baseline = 0
    except Exception:
        baseline = 0

jsonl_ok = False
jsonl_detail = None
lines = []
line_offsets = []
event_count = 0
if log_dir_ok:
    if os.path.isfile(jsonl) and os.path.getsize(jsonl) > 0:
        try:
            with open(jsonl, "rb") as fh:
                raw = fh.read()
            off = 0
            for bl in raw.splitlines(keepends=True):
                line_offsets.append(off)
                off += len(bl)
                lines.append(bl.decode("utf-8", errors="replace"))
            event_count = sum(1 for line in lines if line.strip())
            jsonl_ok = event_count > 0
            jsonl_detail = f"{event_count} events" if jsonl_ok else "file present but empty"
        except OSError as e:
            jsonl_detail = f"read error: {e}"
    elif os.path.isfile(jsonl):
        jsonl_detail = "file present but empty"
    else:
        jsonl_detail = "log-shipper.jsonl missing"
else:
    jsonl_detail = "log dir missing"
add("jsonl_present", jsonl_ok, jsonl_detail)

parse_start_idx = 0
if baseline > 0:
    parse_start_idx = len(line_offsets)
    for i, off in enumerate(line_offsets):
        if off >= baseline:
            parse_start_idx = i
            break
excluded_events = sum(1 for line in lines[:parse_start_idx] if line.strip())

parses_ok = False
parses_detail = None
malformed = []
parsed_events = []
if jsonl_ok:
    for j, raw in enumerate(lines[parse_start_idx:]):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
            parsed_events.append(obj)
        except Exception as e:
            malformed.append((parse_start_idx + j + 1, str(e)[:100]))
    if not malformed:
        parses_ok = True
        considered = event_count - excluded_events
        if baseline > 0:
            parses_detail = f"0 invalid lines among {considered} events checked (baseline={baseline} bytes, {excluded_events} events excluded)"
        else:
            parses_detail = f"0 invalid lines ({event_count} events checked)"
    else:
        ln, err = malformed[0]
        n = len(malformed)
        if baseline > 0:
            parses_detail = f"{n} invalid line{'s' if n != 1 else ''} since baseline (line {ln}, {event_count - excluded_events} events checked, {excluded_events} events excluded)"
        else:
            parses_detail = f"{n} invalid line{'s' if n != 1 else ''} (line {ln}: {err})"
else:
    parses_detail = "skipped: jsonl_present failed"
add("jsonl_parses", parses_ok, parses_detail)

last_data = None
last_parses_ok = False
last_parses_detail = None
if not log_dir_ok:
    last_parses_detail = "skipped: log dir missing"
elif not os.path.isfile(last_run):
    last_parses_detail = "last-run.json missing"
else:
    try:
        with open(last_run, "r", errors="replace") as fh:
            last_data = json.load(fh)
        last_parses_ok = True
    except Exception as e:
        last_parses_detail = f"parse error: {str(e)[:100]}"
add("last_run_parses", last_parses_ok, last_parses_detail)

integrity_ok = False
integrity_detail = None
if last_data is None:
    integrity_detail = "skipped: last_run_parses failed"
else:
    si = last_data.get("safety_integrity")
    if si == "ok":
        integrity_ok = True
        integrity_detail = "safety_integrity=ok"
    else:
        integrity_detail = f"safety_integrity={si!r}"
add("last_run_integrity_ok", integrity_ok, integrity_detail)

sections_ok = True
sections_detail = None
if last_data is None:
    sections_detail = "skipped: last_run_parses failed"
elif "sections" not in last_data:
    sections_detail = "skipped: schema older than v0.1 (no sections[])"
else:
    secs = last_data.get("sections")
    if not isinstance(secs, list):
        sections_ok = False
        sections_detail = f"sections is not an array (got {type(secs).__name__})"
    else:
        bad = []
        for i, entry in enumerate(secs):
            if not isinstance(entry, dict):
                bad.append((i, "not an object"))
                continue
            missing = [k for k in ("name", "status", "items") if k not in entry]
            if missing:
                bad.append((i, f"missing {missing}"))
                continue
            if not isinstance(entry.get("items"), int):
                bad.append((i, "items not int"))
        if bad:
            idx, err = bad[0]
            sections_ok = False
            sections_detail = f"{len(bad)} malformed entr{'ies' if len(bad) != 1 else 'y'} (index {idx}: {err})"
        else:
            sections_detail = f"{len(secs)} section{'s' if len(secs) != 1 else ''} well-formed"
add("last_run_parses_sections", sections_ok, sections_detail)

transform_real = []
if parses_ok:
    dry_ids = set()
    real_ids = set()
    run_ids_seen = []
    seen = set()
    for ev in parsed_events:
        rid = ev.get("run_id")
        if rid and rid not in seen:
            seen.add(rid)
            run_ids_seen.append(rid)
        if ev.get("section") == "run_start":
            note = str(ev.get("note", "") or "")
            if "dry_run=1" in note:
                dry_ids.add(rid)
            else:
                real_ids.add(rid)
    real_ids |= (set(run_ids_seen) - dry_ids - real_ids)
    for ev in parsed_events:
        if ev.get("section") == "transform_emit" and ev.get("run_id") in real_ids:
            transform_real.append(ev)

max_streak = 0
for ev in sorted(transform_real, key=lambda e: e.get("ts", "")):
    status = ev.get("status", "")
    if status == "idle":
        max_streak += 1
    else:
        max_streak = 0

streak_ok = True
streak_detail = "no real runs to evaluate"
if transform_real:
    streak_ok = max_streak < 5
    streak_detail = f"max consecutive_idle={max_streak} (transform_emit)"
elif not parses_ok:
    streak_detail = "skipped: jsonl_parses failed"
add("no_long_idle_streaks", streak_ok, streak_detail)

last_run_missing = log_dir_ok and not os.path.isfile(last_run)
if not log_dir_ok or last_run_missing:
    status = "unknown"
    exit_code = 5
elif any(not c["ok"] for c in checks):
    status = "degraded"
    exit_code = 4
else:
    status = "healthy"
    exit_code = 0

out = {
    "status": status,
    "exit_code": exit_code,
    "generated_at": generated_at,
    "log_dir": log_dir,
    "checks": checks,
}
if mode == "json":
    sys.stdout.write(json.dumps(out, indent=2))
    sys.stdout.write("\n")
else:
    print(f"log-shipper health: {status} (exit {exit_code})")
    width = max(len(c["name"]) for c in checks)
    for c in checks:
        glyph = "OK" if c["ok"] else "XX"
        detail = c["detail"] or ""
        print(f"  [{glyph}] {c['name']:<{width}}  {detail}")

sys.exit(exit_code)
PY
}

if [ "$DO_HEALTH" = 1 ]; then
  do_health
  exit $?
fi
# ── end --health flag ──────────────────────────────────────────────────

# ── --doctor flag ──────────────────────────────────────────────────────
do_doctor() {
  local mode="text"
  [ "$REPORT_JSON" = 1 ] && mode="json"
  local config_path="${CONFIG_FILE:-$DEFAULT_CONFIG}"
  local selected_modes=""
  [ "$MODE_PROM" = 1 ] && selected_modes+="prom\n"
  [ "$MODE_NDJSON" = 1 ] && selected_modes+="ndjson\n"
  [ "$MODE_OTLP_JSON" = 1 ] && selected_modes+="otlp-json\n"

  DOCTOR_MODE="$mode" \
  DOCTOR_ARGV0="$0" \
  DOCTOR_CONFIG_PATH="$config_path" \
  DOCTOR_CONFIG_PARSE_ERROR="${CONFIG_PARSE_ERROR:-}" \
  DOCTOR_MANIFEST_PATH="$LOG_SHIPPER_MANIFEST_PATH" \
  DOCTOR_SOURCES_RAW="$LOG_SHIPPER_SOURCES" \
  DOCTOR_PROM_OUT="$LOG_SHIPPER_PROM_OUT" \
  DOCTOR_NDJSON_OUT="$LOG_SHIPPER_NDJSON_OUT" \
  DOCTOR_OTLP_OUT="$LOG_SHIPPER_OTLP_JSON_OUT" \
  DOCTOR_OUTPUT_OVERRIDE="$OUTPUT_OVERRIDE" \
  DOCTOR_SELECTED_MODES="$selected_modes" \
    python3 - <<'PY'
import datetime, glob, json, os, shlex, shutil, subprocess, sys

mode = os.environ["DOCTOR_MODE"]
argv0 = os.environ["DOCTOR_ARGV0"]
config_path = os.environ["DOCTOR_CONFIG_PATH"]
config_parse_error = os.environ.get("DOCTOR_CONFIG_PARSE_ERROR", "")
manifest_path = os.environ["DOCTOR_MANIFEST_PATH"]
sources_raw = os.environ.get("DOCTOR_SOURCES_RAW", "")
prom_out = os.environ.get("DOCTOR_PROM_OUT", "")
ndjson_out = os.environ.get("DOCTOR_NDJSON_OUT", "")
otlp_out = os.environ.get("DOCTOR_OTLP_OUT", "")
out_override = os.environ.get("DOCTOR_OUTPUT_OVERRIDE", "")
selected_modes = [m for m in os.environ.get("DOCTOR_SELECTED_MODES", "").splitlines() if m]
generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
checks = []


def norm_detail(msg):
    if msg is None:
        return None
    lines = [line.strip() for line in str(msg).splitlines() if line.strip()]
    return " | ".join(lines) if lines else ""


def add(name, ok, detail):
    checks.append({"name": name, "ok": bool(ok), "detail": norm_detail(detail)})


def parse_sources(raw):
    out = []
    seen = set()
    for item in raw.split(":"):
        if not item:
            continue
        item = os.path.expandvars(os.path.expanduser(item))
        matches = []
        if any(ch in item for ch in "*?["):
            matches = sorted(glob.glob(item))
        else:
            matches = [item]
        for match in matches:
            if match not in seen:
                seen.add(match)
                out.append(match)
    return out


def manifest_expected_paths(path):
    out = []
    names = []
    if not os.path.isfile(path):
        return names, out
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return names, out
    mapping = {
        "cert-watch": os.path.join(os.path.expanduser("~/.local/state/cert-watch"), "cert-watch.jsonl"),
        "system-janitor": os.path.join(os.path.expanduser("~/.local/state/janitor"), "janitor.jsonl"),
        "system-updater": os.path.join(os.path.expanduser("~/.local/state/system-updater"), "updater.jsonl"),
    }
    for tool in data.get("tools", []):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if name in mapping:
            names.append(name)
            out.append(mapping[name])
    return names, out


script_path = None
try:
    resolved = subprocess.run(["readlink", "-f", argv0], capture_output=True, text=True, check=False)
    candidate = resolved.stdout.strip()
except OSError:
    candidate = ""
    resolved = None
if candidate and os.path.isfile(candidate):
    script_path = candidate
    add("script_path_resolves", True, candidate)
else:
    detail = candidate or (resolved.stderr.strip() if resolved and resolved.stderr else f"could not resolve {argv0}")
    add("script_path_resolves", False, detail)

if script_path is None:
    add("cron_entry_present", False, "skipped: script_path_resolves failed")
else:
    crontab_path = shutil.which("crontab")
    if crontab_path is None:
        add("cron_entry_present", True, "crontab not installed (skip)")
    else:
        proc = subprocess.run([crontab_path, "-l"], capture_output=True, text=True, check=False)
        lines = proc.stdout.splitlines() if proc.stdout else []
        found = False
        matched = None
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith(script_path):
                found = True
                matched = line
                break
            try:
                tokens = shlex.split(line)
            except ValueError:
                tokens = line.split()
            if not tokens:
                continue
            tail = tokens[-1]
            if tail == script_path:
                found = True
                matched = line
                break
            try:
                tail_resolved = subprocess.run(["readlink", "-f", tail], capture_output=True, text=True, check=False).stdout.strip()
            except OSError:
                tail_resolved = ""
            if tail_resolved and tail_resolved == script_path:
                found = True
                matched = line
                break
        add("cron_entry_present", True, matched if found else "no crontab entry references this script")

if not config_parse_error:
    add("config_parses", True, config_path if os.path.isfile(config_path) else f"{config_path} absent (ok)")
else:
    add("config_parses", False, config_parse_error)

if config_parse_error:
    add("configured_outputs_writable", True, "skipped: config_parses failed")
else:
    requested = []
    if selected_modes:
        for mode_name in selected_modes:
            requested.append(mode_name)
    else:
        requested = ["prom", "ndjson", "otlp-json"]
    destinations = []
    for name in requested:
        if name == "prom":
            dest = out_override if out_override and requested == ["prom"] else prom_out
        elif name == "ndjson":
            dest = out_override if out_override and requested == ["ndjson"] else ndjson_out
        else:
            dest = out_override if out_override and requested == ["otlp-json"] else otlp_out
        if dest:
            destinations.append((name, dest))
    if not destinations:
        add("configured_outputs_writable", True, "no outputs configured")
    else:
        failures = []
        details = []
        for name, dest in destinations:
            if dest == "-":
                details.append(f"{name}: stdout")
                continue
            parent = os.path.dirname(dest) or "."
            if not os.path.isdir(parent):
                failures.append(f"{name}: parent missing ({parent})")
            elif not os.access(parent, os.W_OK):
                failures.append(f"{name}: parent not writable ({parent})")
            else:
                details.append(f"{name}: {parent} writable")
        add("configured_outputs_writable", not failures, "; ".join(failures or details))

explicit_sources = parse_sources(sources_raw)
manifest_tools, manifest_paths = manifest_expected_paths(manifest_path)
existing_sources = [p for p in (explicit_sources or manifest_paths) if os.path.isfile(p)]
if explicit_sources:
    detail = f"explicit paths: {len(explicit_sources)} configured, {len(existing_sources)} present"
elif manifest_tools:
    detail = f"manifest tools: {len(manifest_tools)} entry(ies), {len(existing_sources)} source(s) currently present"
else:
    detail = "warning: no sources discovered yet (fresh host is ok)"
add("sources_discoverable", True, detail)

if script_path is None:
    status = "unknown"
    exit_code = 5
elif any(not c["ok"] for c in checks):
    status = "degraded"
    exit_code = 4
else:
    status = "healthy"
    exit_code = 0

out = {
    "status": status,
    "exit_code": exit_code,
    "generated_at": generated_at,
    "script_path": script_path,
    "checks": checks,
}
if mode == "json":
    sys.stdout.write(json.dumps(out, indent=2))
    sys.stdout.write("\n")
else:
    print(f"log-shipper doctor: {status} (exit {exit_code})")
    width = max(len(c["name"]) for c in checks)
    for c in checks:
        state = "ok" if c["ok"] else "FAIL"
        detail = c["detail"] or ""
        print(f"  {state:<4} {c['name']:<{width}}  {detail}")

sys.exit(exit_code)
PY
}

if [ "$DO_DOCTOR" = 1 ]; then
  do_doctor
  exit $?
fi
# ── end --doctor flag ──────────────────────────────────────────────────

# ── --health-acknowledge flag ──────────────────────────────────────────
do_health_acknowledge() {
  local mode="text"
  [ "$REPORT_JSON" = 1 ] && mode="json"
  local log_dir="$LOG_SHIPPER_LOG_DIR"
  local jsonl="${log_dir}/log-shipper.jsonl"
  local baseline_file="${log_dir}/.health-baseline"

  ACK_MODE="$mode" \
  ACK_LOG_DIR="$log_dir" \
  ACK_JSONL="$jsonl" \
  ACK_BASELINE_FILE="$baseline_file" \
    python3 - <<'PY'
import json, os, sys
mode = os.environ["ACK_MODE"]
log_dir = os.environ["ACK_LOG_DIR"]
jsonl = os.environ["ACK_JSONL"]
baseline_file = os.environ["ACK_BASELINE_FILE"]
size = 0
event_count = 0
if os.path.isfile(jsonl):
    try:
        size = os.path.getsize(jsonl)
        with open(jsonl, "rb") as fh:
            for ln in fh:
                if ln.strip():
                    event_count += 1
    except OSError:
        size = 0
        event_count = 0
try:
    os.makedirs(log_dir, exist_ok=True)
except OSError as e:
    sys.stderr.write(f"[ERROR] cannot create {log_dir}: {e}\n")
    sys.exit(3)
tmp = baseline_file + ".tmp"
try:
    with open(tmp, "w") as fh:
        fh.write(f"{size}\n")
    os.replace(tmp, baseline_file)
except OSError as e:
    sys.stderr.write(f"[ERROR] cannot write {baseline_file}: {e}\n")
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(3)
if mode == "json":
    sys.stdout.write(json.dumps({"acknowledged": True, "baseline_bytes": size, "excluded_events": event_count}, indent=2))
    sys.stdout.write("\n")
else:
    print(f"acknowledged: baseline set to {size} bytes ({event_count} existing events excluded from future --health checks)")
sys.exit(0)
PY
}

if [ "$DO_HEALTH_ACK" = 1 ]; then
  do_health_acknowledge
  exit $?
fi
# ── end --health-acknowledge flag ──────────────────────────────────────

for d in "${HOME}/.dotnet" "${HOME}/.local/bin" "${HOME}/go/bin" /snap/bin; do
  [ -d "$d" ] && case ":${PATH}:" in *":${d}:"*) ;; *) PATH="${d}:${PATH}";; esac
done
export PATH

# ── Paths and identifiers ──────────────────────────────────────────────
LOG_DIR="$LOG_SHIPPER_LOG_DIR"
LOG="${LOG_DIR}/log-shipper.log"
JSONL="${LOG_DIR}/log-shipper.jsonl"
LATEST="${LOG_DIR}/last-run.json"
LOCK="${LOG_DIR}/log-shipper.lock"
mkdir -p "$LOG_DIR"

RUN_ID="$(date -u '+%Y%m%dT%H%M%SZ')-$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || echo deadbeef)"
SECTIONS_TMP="${LOG_DIR}/.sections.${RUN_ID}.$$"
: > "$SECTIONS_TMP"
RUN_STARTED_AT=""
RUN_ENDED_AT=""
HOSTNAME_S="$(hostname)"
USER_S="${USER:-$(id -un)}"

# ── Single-instance lock ───────────────────────────────────────────────
exec 9>"$LOCK"
if ! flock -n 9; then
  logger -t log-shipper "skipped — another instance is running" 2>/dev/null || true
  echo "[skip] another instance is running" >&2
  exit 1
fi

# ── Log rotation (5 MB threshold, 8 backups) ───────────────────────────
rotate() {
  local f="$1"
  [ -f "$f" ] || return 0
  [ "$(stat -c%s "$f" 2>/dev/null || echo 0)" -le 5242880 ] && return 0
  for i in 7 6 5 4 3 2 1; do
    [ -f "${f}.${i}" ] && mv "${f}.${i}" "${f}.$((i+1))"
  done
  mv "$f" "${f}.1"
}
rotate "$LOG"
rotate "$JSONL"

exec 3>&1
exec >>"$LOG" 2>&1

# ── Helpers ────────────────────────────────────────────────────────────
ts()            { date '+%Y-%m-%dT%H:%M:%S%z'; }
dir_inode()     { stat -c '%i' "$1" 2>/dev/null || echo missing; }
dir_size()      { du -sb "$1" 2>/dev/null | awk '{print $1}'; }
jsonescape() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1" 2>/dev/null \
    || printf '"%s"' "${1//\"/\\\"}"
}
split_paths() {
  local raw="$1" item
  IFS=':' read -r -a _items <<< "$raw"
  for item in "${_items[@]}"; do
    [ -z "$item" ] && continue
    item="${item/#\~/$HOME}"
    eval "item=\"$item\""
    printf '%s\n' "$item"
  done
}
split_paths_glob() {
  local raw="$1" item match
  IFS=':' read -r -a _items <<< "$raw"
  for item in "${_items[@]}"; do
    [ -z "$item" ] && continue
    item="${item/#\~/$HOME}"
    eval "item=\"$item\""
    case "$item" in
      *[\*\?\[]*)
        local _saved_nullglob; _saved_nullglob=$(shopt -p nullglob)
        shopt -s nullglob
        # shellcheck disable=SC2086,SC2206
        local _expanded=($item)
        eval "$_saved_nullglob"
        for match in "${_expanded[@]}"; do
          printf '%s\n' "$match"
        done
        ;;
      *)
        printf '%s\n' "$item"
        ;;
    esac
  done
}

emit_event() {
  local section="$1" status="$2" items="${3:-0}" note="${4:-}" extra_json="${5:-}"
  items="${items//[^0-9]/}"
  items=$((10#${items:-0} + 0))
  printf '{"run_id":"%s","ts":"%s","host":"%s","user":"%s","section":%s,"status":"%s","items":%s,"note":%s%s}\n' \
    "$RUN_ID" "$(ts)" "$HOSTNAME_S" "$USER_S" \
    "$(jsonescape "$section")" "$status" "$items" "$(jsonescape "$note")" \
    "${extra_json:+,${extra_json}}" >>"$JSONL"
  case "$section" in
    run_start|run_end) : ;;
    *)
      printf '{"name":%s,"status":"%s","items":%s}\n' \
        "$(jsonescape "$section")" "$status" "$items" >>"$SECTIONS_TMP" 2>/dev/null || true
      ;;
  esac
}

section() { echo; echo "── $* ── $(ts)"; }

OPTIN_SECTIONS=("transform_emit")

DISCOVERY_READY=0
DISCOVER_SOURCE_COUNT=0
DISCOVERED_SOURCES_JSON='[]'
DISCOVER_MANIFEST_TOOLS_JSON='[]'
DISCOVER_MANUAL=0

TRANSFORM_TOTAL_EVENTS=0
TRANSFORM_TOTAL_SOURCES=0
TRANSFORM_SOURCES_JSON='[]'
TRANSFORM_BY_TOOL_JSON='{}'
TRANSFORM_OUTPUTS_JSON='[]'
MANIFEST_DRIFT_JSON='[]'

ACTION_STATUS="ok"
ACTION_ITEMS=0
ACTION_NOTE=""
ACTION_EXTRA_JSON=""

action_reset() {
  ACTION_STATUS="ok"
  ACTION_ITEMS=0
  ACTION_NOTE=""
  ACTION_EXTRA_JSON=""
}

select_destinations() {
  local use_prom="$MODE_PROM" use_ndjson="$MODE_NDJSON" use_otlp="$MODE_OTLP_JSON"
  local prom_out="$LOG_SHIPPER_PROM_OUT" ndjson_out="$LOG_SHIPPER_NDJSON_OUT" otlp_out="$LOG_SHIPPER_OTLP_JSON_OUT"
  local count=$((use_prom + use_ndjson + use_otlp))

  if [ "$count" -eq 0 ]; then
    [ -n "$prom_out" ] && use_prom=1
    [ -n "$ndjson_out" ] && use_ndjson=1
    [ -n "$otlp_out" ] && use_otlp=1
  fi

  if [ -n "$OUTPUT_OVERRIDE" ]; then
    if [ "$count" -eq 0 ]; then
      echo "[ERROR] --out requires exactly one explicit export mode" >&2
      exit 3
    fi
    if [ "$use_prom" = 1 ]; then
      prom_out="$OUTPUT_OVERRIDE"
    elif [ "$use_ndjson" = 1 ]; then
      ndjson_out="$OUTPUT_OVERRIDE"
    else
      otlp_out="$OUTPUT_OVERRIDE"
    fi
  fi

  if [ "$use_ndjson" = 1 ] && [ -z "$ndjson_out" ]; then
    ndjson_out="-"
  fi
  if [ "$use_prom" = 1 ] && [ -z "$prom_out" ]; then
    echo "[ERROR] --prom requires LOG_SHIPPER_PROM_OUT or --out <path>" >&2
    exit 3
  fi
  if [ "$use_otlp" = 1 ] && [ -z "$otlp_out" ]; then
    echo "[ERROR] --otlp-json requires LOG_SHIPPER_OTLP_JSON_OUT or --out <path>" >&2
    exit 3
  fi

  ACTIVE_PROM="$use_prom"
  ACTIVE_NDJSON="$use_ndjson"
  ACTIVE_OTLP_JSON="$use_otlp"
  ACTIVE_PROM_OUT="$prom_out"
  ACTIVE_NDJSON_OUT="$ndjson_out"
  ACTIVE_OTLP_JSON_OUT="$otlp_out"
}

ensure_discovery() {
  [ "$DISCOVERY_READY" = 1 ] && return 0
  local result
  result="$(
    LOG_SHIPPER_SOURCES_E="$LOG_SHIPPER_SOURCES" \
    LOG_SHIPPER_MANIFEST_PATH_E="$LOG_SHIPPER_MANIFEST_PATH" \
    XDG_STATE_HOME_E="$XDG_STATE_HOME" \
    python3 - <<'PY'
import glob, json, os, shlex

raw = os.environ.get("LOG_SHIPPER_SOURCES_E", "")
manifest_path = os.environ.get("LOG_SHIPPER_MANIFEST_PATH_E", "")
xdg_state_home = os.environ.get("XDG_STATE_HOME_E", os.path.expanduser("~/.local/state"))

mapping = {
    "cert-watch": ("cert-watch", os.path.join(xdg_state_home, "cert-watch", "cert-watch.jsonl")),
    "system-janitor": ("janitor", os.path.join(xdg_state_home, "janitor", "janitor.jsonl")),
    "system-updater": ("updater", os.path.join(xdg_state_home, "system-updater", "updater.jsonl")),
}


def tool_from_path(path):
    base = os.path.basename(path)
    if base == "janitor.jsonl":
        return "janitor"
    if base == "updater.jsonl":
        return "updater"
    if base == "cert-watch.jsonl":
        return "cert-watch"
    if "cert-watch" in path:
        return "cert-watch"
    if "janitor" in path:
        return "janitor"
    if "updater" in path:
        return "updater"
    return "unknown"

sources = []
seen = set()
manual = False
if raw:
    manual = True
    for item in raw.split(":"):
        if not item:
            continue
        item = os.path.expandvars(os.path.expanduser(item))
        matches = sorted(glob.glob(item)) if any(ch in item for ch in "*?[") else [item]
        for match in matches:
            if match in seen:
                continue
            seen.add(match)
            if os.path.isfile(match) and os.access(match, os.R_OK):
                sources.append({"path": match, "tool": tool_from_path(match), "readable": True})
else:
    if os.path.isfile(manifest_path):
        try:
            manifest = json.load(open(manifest_path, encoding="utf-8"))
        except Exception:
            manifest = {}
        for tool in manifest.get("tools", []):
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if name not in mapping:
                continue
            label, candidate = mapping[name]
            if candidate in seen:
                continue
            seen.add(candidate)
            if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
                sources.append({"path": candidate, "tool": label, "readable": True})

manifest_tools = []
if os.path.isfile(manifest_path):
    try:
        manifest = json.load(open(manifest_path, encoding="utf-8"))
    except Exception:
        manifest = {}
    for tool in manifest.get("tools", []):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if name in mapping:
            label, candidate = mapping[name]
            manifest_tools.append({"manifest_name": name, "tool": label, "expected_path": candidate})

sources.sort(key=lambda row: (row.get("tool", ""), row.get("path", "")))
manifest_tools.sort(key=lambda row: row.get("tool", ""))
paths_nl = "\n".join(row["path"] for row in sources)

print(f"DISCOVER_SOURCE_COUNT={len(sources)}")
print(f"DISCOVERED_SOURCES_JSON={shlex.quote(json.dumps(sources, separators=(',', ':')))}")
print(f"DISCOVER_MANIFEST_TOOLS_JSON={shlex.quote(json.dumps(manifest_tools, separators=(',', ':')))}")
print(f"DISCOVER_MANUAL={1 if manual else 0}")
PY
  )" || return 1
  eval "$result"
  DISCOVERY_READY=1
}

act_discover_sources() {
  ensure_discovery || return 1
  ACTION_ITEMS="$DISCOVER_SOURCE_COUNT"
  if [ "$DISCOVER_SOURCE_COUNT" -eq 0 ]; then
    ACTION_STATUS="idle"
    if [ "$DISCOVER_MANUAL" = 1 ]; then
      ACTION_NOTE="0 readable sources from LOG_SHIPPER_SOURCES"
    else
      ACTION_NOTE="0 readable sources discovered from manifest"
    fi
  else
    ACTION_STATUS="ok"
    if [ "$DISCOVER_MANUAL" = 1 ]; then
      ACTION_NOTE="discovered ${DISCOVER_SOURCE_COUNT} readable source(s) from LOG_SHIPPER_SOURCES"
    else
      ACTION_NOTE="discovered ${DISCOVER_SOURCE_COUNT} readable source(s) from manifest"
    fi
  fi
  ACTION_EXTRA_JSON="\"total_sources\":$DISCOVER_SOURCE_COUNT,\"sources\":$DISCOVERED_SOURCES_JSON"
}

act_transform_emit() {
  ensure_discovery || return 1
  select_destinations
  local result
  result="$(
    DISCOVERED_SOURCES_JSON_E="$DISCOVERED_SOURCES_JSON" \
    LOOKBACK_HOURS_E="$LOG_SHIPPER_LOOKBACK_HOURS" \
    DRY_RUN_E="$DRY_RUN" \
    ACTIVE_PROM_E="$ACTIVE_PROM" \
    ACTIVE_NDJSON_E="$ACTIVE_NDJSON" \
    ACTIVE_OTLP_JSON_E="$ACTIVE_OTLP_JSON" \
    ACTIVE_PROM_OUT_E="$ACTIVE_PROM_OUT" \
    ACTIVE_NDJSON_OUT_E="$ACTIVE_NDJSON_OUT" \
    ACTIVE_OTLP_JSON_OUT_E="$ACTIVE_OTLP_JSON_OUT" \
    python3 - <<'PY'
import collections, datetime, json, os, pathlib, shlex

sources = json.loads(os.environ.get("DISCOVERED_SOURCES_JSON_E", "[]"))
lookback_hours = int(str(os.environ.get("LOOKBACK_HOURS_E", "24") or "24"))
dry_run = str(os.environ.get("DRY_RUN_E", "0") or "0") == "1"
active_prom = os.environ.get("ACTIVE_PROM_E", "0") == "1"
active_ndjson = os.environ.get("ACTIVE_NDJSON_E", "0") == "1"
active_otlp = os.environ.get("ACTIVE_OTLP_JSON_E", "0") == "1"
prom_out = os.environ.get("ACTIVE_PROM_OUT_E", "")
ndjson_out = os.environ.get("ACTIVE_NDJSON_OUT_E", "")
otlp_out = os.environ.get("ACTIVE_OTLP_JSON_OUT_E", "")
now = datetime.datetime.now(datetime.timezone.utc)
shipped_at = now.strftime("%Y-%m-%dT%H:%M:%S%z")
cutoff = now - datetime.timedelta(hours=max(lookback_hours, 0))


def parse_ts(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except ValueError:
            continue
    return None


def to_unix_nano(dt):
    if dt is None:
        dt = now
    return str(int(dt.timestamp() * 1_000_000_000))


def prometheus_escape(value):
    return str(value).replace('\\', r'\\').replace('\n', r'\n').replace('"', r'\"')


def tool_last_run_status(tool_name, events):
    latest = None
    for ev in events:
        sec = ev.get("section")
        stage = ev.get("stage")
        if sec == "run_end" or stage == "run_end":
            if latest is None or ev.get("ts", "") >= latest.get("ts", ""):
                latest = ev
    if latest is None:
        return None, None
    return latest.get("status"), latest.get("ts")


def tool_safety_ok(tool_name, events):
    latest = None
    for ev in events:
        sec = ev.get("section")
        stage = ev.get("stage")
        if sec == "safety_integrity" or stage == "integrity":
            if latest is None or ev.get("ts", "") >= latest.get("ts", ""):
                latest = ev
    if latest is None:
        return 0
    return 1 if latest.get("status") == "ok" else 0


def tool_idle_streak_len(tool_name, events):
    if tool_name in ("janitor", "cert-watch"):
        run_types = {}
        for ev in events:
            if ev.get("section") == "run_start":
                run_types[ev.get("run_id")] = "dry" if "dry_run=1" in str(ev.get("note", "") or "") else "real"
        max_streak = 0
        by_section = collections.defaultdict(list)
        for ev in events:
            sec = ev.get("section")
            if sec in ("run_start", "run_end", "safety_integrity"):
                continue
            if run_types.get(ev.get("run_id"), "real") != "real":
                continue
            by_section[sec].append(ev)
        for evs in by_section.values():
            trailing = 0
            for ev in sorted(evs, key=lambda row: row.get("ts", "")):
                status = ev.get("status", "")
                try:
                    items = int(ev.get("items") or 0)
                except (TypeError, ValueError):
                    items = 0
                if status == "idle" or (status == "ok" and items == 0):
                    trailing += 1
                else:
                    trailing = 0
            max_streak = max(max_streak, trailing)
        return max_streak
    if tool_name == "updater":
        run_types = {}
        for ev in events:
            if ev.get("stage") == "run_start":
                dr = ev.get("dry_run")
                run_types[ev.get("run_id")] = "dry" if dr in (1, "1", True, "true", "yes") else "real"
        max_streak = 0
        by_package = collections.defaultdict(list)
        for ev in events:
            if ev.get("stage") != "package":
                continue
            if run_types.get(ev.get("run_id"), "real") != "real":
                continue
            pkg = ev.get("package") or ""
            if pkg:
                by_package[pkg].append(ev)
        for evs in by_package.values():
            trailing = 0
            for ev in sorted(evs, key=lambda row: row.get("ts", "")):
                status = ev.get("status", "")
                if status in ("held", "excluded", "filtered_non_security", "idle"):
                    trailing += 1
                else:
                    trailing = 0
            max_streak = max(max_streak, trailing)
        return max_streak
    return 0


def enrich_event(tool_name, source_path, ev):
    obj = dict(ev)
    obj["tool"] = tool_name
    obj["source_path"] = source_path
    obj["shipped_at"] = shipped_at
    return obj


def severity_for(ev):
    status = str(ev.get("status", "") or "")
    if status in ("warn", "failed", "expired", "violated_missing", "violated_inode_changed"):
        return "WARN"
    if status in ("error",):
        return "ERROR"
    return "INFO"


source_rows = []
by_tool = {}
enriched_events = []
raw_by_tool = collections.defaultdict(list)
malformed_sources = []
freed_series = []
status_counts = collections.Counter()

for row in sources:
    path = row.get("path")
    tool = row.get("tool") or "unknown"
    valid_events = []
    malformed = 0
    last_ts = None
    if not path or not os.path.isfile(path):
        continue
    with open(path, "r", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("not an object")
            except Exception:
                malformed += 1
                continue
            dt = parse_ts(obj.get("ts"))
            if dt is None or dt < cutoff:
                continue
            valid_events.append(obj)
            raw_by_tool[tool].append(obj)
            if last_ts is None or obj.get("ts", "") >= last_ts:
                last_ts = obj.get("ts")
            enriched = enrich_event(tool, path, obj)
            enriched_events.append(enriched)
            section = obj.get("section") or obj.get("stage") or "unknown"
            status = obj.get("status") or "unknown"
            status_counts[(tool, section, status)] += 1
            freed_bytes = None
            try:
                if obj.get("freed_bytes") is not None:
                    freed_bytes = int(obj.get("freed_bytes") or 0)
                elif obj.get("freed_kb") is not None:
                    freed_bytes = int(obj.get("freed_kb") or 0) * 1024
            except (TypeError, ValueError):
                freed_bytes = 0
            if freed_bytes is not None:
                freed_series.append((tool, section, obj.get("run_id") or "unknown", freed_bytes))
    source_rows.append({
        "path": path,
        "tool": tool,
        "events_in_window": len(valid_events),
        "last_event_ts": last_ts,
    })
    if malformed:
        malformed_sources.append({"path": path, "tool": tool, "invalid_lines": malformed})

source_rows.sort(key=lambda row: (row.get("tool", ""), row.get("path", "")))
for tool, events in raw_by_tool.items():
    last_status, last_ts = tool_last_run_status(tool, events)
    by_tool[tool] = {
        "events": sum(1 for ev in events if parse_ts(ev.get("ts")) and parse_ts(ev.get("ts")) >= cutoff),
        "last_run_status": last_status or "unknown",
        "last_success_ts": last_ts,
        "safety_integrity_ok": tool_safety_ok(tool, events),
        "idle_streak_len": tool_idle_streak_len(tool, events),
    }

prom_lines = []
prom_bytes = 0
if active_prom:
    prom_lines.extend([
        "# HELP agent_toolkit_run_freed_bytes Bytes freed per section per run",
        "# TYPE agent_toolkit_run_freed_bytes gauge",
    ])
    for tool, section, run_id, freed_bytes in freed_series:
        prom_lines.append(
            f'agent_toolkit_run_freed_bytes{{tool="{prometheus_escape(tool)}",section="{prometheus_escape(section)}",run_id="{prometheus_escape(run_id)}"}} {freed_bytes}'
        )
    prom_lines.extend([
        "# HELP agent_toolkit_run_status_total Run status counts within the export lookback window",
        "# TYPE agent_toolkit_run_status_total counter",
    ])
    for (tool, section, status), count in sorted(status_counts.items()):
        prom_lines.append(
            f'agent_toolkit_run_status_total{{tool="{prometheus_escape(tool)}",section="{prometheus_escape(section)}",status="{prometheus_escape(status)}"}} {count}'
        )
    prom_lines.extend([
        "# HELP agent_toolkit_last_success_timestamp_seconds Unix timestamp of the latest observed completed run",
        "# TYPE agent_toolkit_last_success_timestamp_seconds gauge",
        "# HELP agent_toolkit_safety_integrity_ok 1 when the latest observed integrity event reports ok",
        "# TYPE agent_toolkit_safety_integrity_ok gauge",
        "# HELP agent_toolkit_idle_streak_len Trailing consecutive idle real runs observed for the tool",
        "# TYPE agent_toolkit_idle_streak_len gauge",
    ])
    for tool, info in sorted(by_tool.items()):
        last_dt = parse_ts(info.get("last_success_ts"))
        prom_lines.append(
            f'agent_toolkit_last_success_timestamp_seconds{{tool="{prometheus_escape(tool)}"}} {0 if last_dt is None else int(last_dt.timestamp())}'
        )
        prom_lines.append(
            f'agent_toolkit_safety_integrity_ok{{tool="{prometheus_escape(tool)}"}} {int(info.get("safety_integrity_ok") or 0)}'
        )
        prom_lines.append(
            f'agent_toolkit_idle_streak_len{{tool="{prometheus_escape(tool)}"}} {int(info.get("idle_streak_len") or 0)}'
        )
    prom_text = "\n".join(prom_lines) + "\n"
    prom_bytes = len(prom_text.encode("utf-8"))
else:
    prom_text = ""

ndjson_text = "\n".join(json.dumps(ev, separators=(",", ":")) for ev in enriched_events)
if active_ndjson and ndjson_text:
    ndjson_text += "\n"
ndjson_bytes = len(ndjson_text.encode("utf-8")) if active_ndjson else 0

otlp_payload = {
    "resourceLogs": [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "agent-toolkit"}},
                    {"key": "service.namespace", "value": {"stringValue": "agent-toolkit"}},
                ]
            },
            "scopeLogs": []
        }
    ]
}
if active_otlp:
    grouped = collections.defaultdict(list)
    for ev in enriched_events:
        grouped[ev.get("tool") or "unknown"].append(ev)
    for tool, evs in sorted(grouped.items()):
        scope = {"scope": {"name": "log-shipper", "version": "0.2.0"}, "logRecords": []}
        for ev in evs:
            dt = parse_ts(ev.get("ts"))
            scope["logRecords"].append({
                "timeUnixNano": to_unix_nano(dt),
                "observedTimeUnixNano": to_unix_nano(now),
                "severityText": severity_for(ev),
                "body": {"stringValue": json.dumps(ev, separators=(",", ":"))},
                "attributes": [
                    {"key": "tool", "value": {"stringValue": tool}},
                    {"key": "section", "value": {"stringValue": str(ev.get("section") or ev.get("stage") or "unknown")}},
                    {"key": "status", "value": {"stringValue": str(ev.get("status") or "unknown")}},
                ],
            })
        otlp_payload["resourceLogs"][0]["scopeLogs"].append(scope)
otlp_text = json.dumps(otlp_payload, indent=2) + "\n" if active_otlp else ""
otlp_bytes = len(otlp_text.encode("utf-8")) if active_otlp else 0

outputs = []


def atomic_write(path, text):
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


if active_prom:
    outputs.append({"format": "prom", "path": prom_out, "last_write_ts": None if dry_run else shipped_at, "bytes": 0 if dry_run else prom_bytes})
    if not dry_run:
        atomic_write(prom_out, prom_text)
if active_ndjson:
    outputs.append({"format": "ndjson", "path": ndjson_out or "-", "last_write_ts": None if dry_run else shipped_at, "bytes": 0 if dry_run else ndjson_bytes})
    if not dry_run:
        if ndjson_out == "-":
            os.write(3, ndjson_text.encode('utf-8'))
        else:
            atomic_write(ndjson_out, ndjson_text)
if active_otlp:
    outputs.append({"format": "otlp-json", "path": otlp_out, "last_write_ts": None if dry_run else shipped_at, "bytes": 0 if dry_run else otlp_bytes})
    if not dry_run:
        atomic_write(otlp_out, otlp_text)

status = "ok"
if malformed_sources:
    status = "warn"
elif not outputs:
    status = "idle"

note = f"shipped {len(enriched_events)} event(s) from {len(source_rows)} source(s) to {len(outputs)} output(s)"
if not outputs:
    note = f"no outputs configured; observed {len(enriched_events)} event(s) across {len(source_rows)} source(s)"
if malformed_sources:
    note += f"; malformed lines in {len(malformed_sources)} source(s)"

print(f"ACTION_STATUS={shlex.quote(status)}")
print(f"ACTION_ITEMS={len(enriched_events)}")
print(f"ACTION_NOTE={shlex.quote(note)}")
print(f"TRANSFORM_TOTAL_EVENTS={len(enriched_events)}")
print(f"TRANSFORM_TOTAL_SOURCES={len(source_rows)}")
print(f"TRANSFORM_SOURCES_JSON={shlex.quote(json.dumps(source_rows, separators=(',', ':')))}")
print(f"TRANSFORM_BY_TOOL_JSON={shlex.quote(json.dumps(by_tool, separators=(',', ':')))}")
print(f"TRANSFORM_OUTPUTS_JSON={shlex.quote(json.dumps(outputs, separators=(',', ':')))}")
print(f"ACTION_EXTRA_JSON={shlex.quote('"total_sources":' + str(len(source_rows)) + ',"total_events_shipped":' + str(len(enriched_events)) + ',"sources":' + json.dumps(source_rows, separators=(',', ':')) + ',"by_tool":' + json.dumps(by_tool, separators=(',', ':')) + ',"outputs":' + json.dumps(outputs, separators=(',', ':')) + ',"malformed_sources":' + json.dumps(malformed_sources, separators=(',', ':')))}")
PY
  )" || return 1
  eval "$result"
  if [ "$TRANSFORM_TOTAL_SOURCES" -eq 0 ]; then
    ACTION_STATUS="idle"
    ACTION_NOTE="no readable source JSONL files discovered"
    ACTION_EXTRA_JSON="\"total_sources\":0,\"total_events_shipped\":0,\"sources\":[],\"by_tool\":{},\"outputs\":[],\"malformed_sources\":[]"
  fi
  if [ "$DRY_RUN" = 1 ] && [ "$ACTION_STATUS" != "warn" ]; then
    ACTION_STATUS="dry_run"
  fi
}

act_manifest_sync() {
  ensure_discovery || return 1
  local result
  result="$(
    DISCOVERED_SOURCES_JSON_E="$DISCOVERED_SOURCES_JSON" \
    DISCOVER_MANIFEST_TOOLS_JSON_E="$DISCOVER_MANIFEST_TOOLS_JSON" \
    python3 - <<'PY'
import json, shlex

sources = json.loads(os.environ.get("DISCOVERED_SOURCES_JSON_E", "[]"))
manifest_tools = json.loads(os.environ.get("DISCOVER_MANIFEST_TOOLS_JSON_E", "[]"))

source_tools = {row.get("tool") for row in sources if row.get("tool")}
expected = {row.get("tool") for row in manifest_tools if row.get("tool")}
drift = []
for row in manifest_tools:
    tool = row.get("tool")
    path = row.get("expected_path")
    if tool not in source_tools:
        drift.append(f"manifest tool missing source: {tool} ({path})")
for tool in sorted(source_tools - expected):
    drift.append(f"source not declared in manifest: {tool}")

status = "ok" if not drift else "warn"
note = "manifest sources synchronized" if not drift else "; ".join(drift[:5])
print(f"ACTION_STATUS={shlex.quote(status)}")
print(f"ACTION_ITEMS={len(drift)}")
print(f"ACTION_NOTE={shlex.quote(note)}")
print(f"MANIFEST_DRIFT_JSON={shlex.quote(json.dumps(drift, separators=(',', ':')))}")
print(f"ACTION_EXTRA_JSON={shlex.quote('"manifest_drift":' + json.dumps(drift, separators=(',', ':')))}")
PY
  )" || return 1
  eval "$result"
}

run_section() {
  local name="$1"; shift
  local fn="$1"
  local emit_status="ok"
  _should_run_section "$name" || return 0
  action_reset
  section "$name"
  if "$fn"; then
    :
  else
    local rc=$?
    ACTION_STATUS="warn"
    ACTION_ITEMS=0
    ACTION_NOTE="action exited non-zero (rc=$rc)"
    ACTION_EXTRA_JSON=""
  fi
  emit_status="$ACTION_STATUS"
  if [ "$DRY_RUN" = "1" ] && [ "$emit_status" = "ok" ] && [ "$name" = "transform_emit" ]; then
    emit_status="dry_run"
  elif [ "$emit_status" = "ok" ] && [ "$ACTION_ITEMS" = "0" ]; then
    local s
    for s in "${OPTIN_SECTIONS[@]}"; do
      if [ "$s" = "$name" ]; then
        emit_status="idle"
        break
      fi
    done
  fi
  ACTION_STATUS="$emit_status"
  echo "[done] section='$name' status=$emit_status items=$ACTION_ITEMS"
  emit_event "$name" "$emit_status" "$ACTION_ITEMS" "$ACTION_NOTE" "$ACTION_EXTRA_JSON"
}

# ── Run header ─────────────────────────────────────────────────────────
declare -A SAFETY_BEFORE_INODE SAFETY_BEFORE_SIZE
SAFETY_DIRS_LIST=""
if [ -n "$LOG_SHIPPER_SAFETY_FLOOR_DIRS" ]; then
  while IFS= read -r d; do
    SAFETY_DIRS_LIST="$SAFETY_DIRS_LIST $d"
    SAFETY_BEFORE_INODE["$d"]=$(dir_inode "$d")
    SAFETY_BEFORE_SIZE["$d"]=$(dir_size "$d")
  done < <(split_paths "$LOG_SHIPPER_SAFETY_FLOOR_DIRS")
fi

echo "════════════════════════════════════════════════════════════════"
echo " log-shipper"
echo " run_id : $RUN_ID"
echo " host   : $HOSTNAME_S"
echo " user   : $USER_S"
echo " start  : $(ts)"
echo " config : ${CONFIG_LOADED:-<none — using defaults>}"
echo " dryrun : $DRY_RUN"
echo " safety : ${SAFETY_DIRS_LIST:-<none>}"
echo "════════════════════════════════════════════════════════════════"

RUN_STARTED_AT="$(ts)"
emit_event "run_start" "ok" 0 "config=${CONFIG_LOADED:-defaults} dry_run=$DRY_RUN"

# ── Sections ───────────────────────────────────────────────────────────
run_section "discover_sources" act_discover_sources
run_section "transform_emit" act_transform_emit
run_section "manifest_sync" act_manifest_sync

# ── Integrity check on safety-floor dirs ──────────────────────────────
section "Integrity check"
INTEGRITY="ok"
INTEGRITY_NOTE=""
if [ -n "$LOG_SHIPPER_SAFETY_FLOOR_DIRS" ]; then
  while IFS= read -r d; do
    after_inode=$(dir_inode "$d")
    after_size=$(dir_size "$d")
    before_inode="${SAFETY_BEFORE_INODE[$d]}"
    before_size="${SAFETY_BEFORE_SIZE[$d]}"
    line="$d before=(inode=$before_inode size=$before_size) after=(inode=$after_inode size=$after_size)"
    if [ "$after_inode" = "missing" ] && [ "$before_inode" != "missing" ]; then
      INTEGRITY="violated_missing"; INTEGRITY_NOTE="$INTEGRITY_NOTE $d:missing"
    elif [ "$before_inode" != "$after_inode" ]; then
      INTEGRITY="violated_inode_changed"; INTEGRITY_NOTE="$INTEGRITY_NOTE $d:inode_changed"
    elif [ -n "$before_size" ] && [ -n "$after_size" ] && [ "$before_size" != "$after_size" ]; then
      line="$line [info: size changed]"
    fi
    echo "$line"
  done < <(split_paths "$LOG_SHIPPER_SAFETY_FLOOR_DIRS")
else
  echo "(no safety-floor dirs configured)"
fi
echo "result: $INTEGRITY"
if [ -z "$LOG_SHIPPER_SAFETY_FLOOR_DIRS" ]; then
  emit_event "safety_integrity" "$INTEGRITY" 0 "no_safety_dirs_configured"
else
  emit_event "safety_integrity" "$INTEGRITY" 0 "${INTEGRITY_NOTE:-clean}"
fi

# ── Final summary ──────────────────────────────────────────────────────
EXIT_CODE=0
RUN_STATUS="ok"
if [ "$INTEGRITY" != "ok" ]; then
  RUN_STATUS="$INTEGRITY"
fi

section "Summary"
echo
echo "run_id              : $RUN_ID"
echo "status              : $RUN_STATUS"
echo "total sources       : ${TRANSFORM_TOTAL_SOURCES:-0}"
echo "events shipped      : ${TRANSFORM_TOTAL_EVENTS:-0}"
echo "manifest drift items: $(python3 - <<'PY' "$MANIFEST_DRIFT_JSON"
import json, sys
try:
    arr = json.loads(sys.argv[1])
    print(len(arr) if isinstance(arr, list) else 0)
except Exception:
    print(0)
PY
)"
echo "safety integrity    : $INTEGRITY"

RUN_ENDED_AT="$(ts)"
TMPSUM="${LATEST}.tmp.$$"
if RUN_ID_E="$RUN_ID" \
   STARTED_AT_E="$RUN_STARTED_AT" \
   ENDED_AT_E="$RUN_ENDED_AT" \
   HOST_E="$HOSTNAME_S" \
   USER_E="$USER_S" \
   DRY_RUN_E="$DRY_RUN" \
   RUN_STATUS_E="$RUN_STATUS" \
   TOTAL_SOURCES_E="${TRANSFORM_TOTAL_SOURCES:-0}" \
   TOTAL_EVENTS_SHIPPED_E="${TRANSFORM_TOTAL_EVENTS:-0}" \
   SOURCES_JSON_E="$TRANSFORM_SOURCES_JSON" \
   BY_TOOL_JSON_E="$TRANSFORM_BY_TOOL_JSON" \
   OUTPUTS_JSON_E="$TRANSFORM_OUTPUTS_JSON" \
   MANIFEST_DRIFT_JSON_E="$MANIFEST_DRIFT_JSON" \
   INTEGRITY_E="$INTEGRITY" \
   SECTIONS_FILE_E="$SECTIONS_TMP" \
   python3 - > "$TMPSUM" <<'PY'
import json, os
sections = []
sf = os.environ.get("SECTIONS_FILE_E", "")
if sf and os.path.isfile(sf):
    with open(sf, "r", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            try:
                items = int(obj.get("items", 0) or 0)
            except (TypeError, ValueError):
                items = 0
            sections.append({
                "name": str(obj.get("name", "")),
                "status": str(obj.get("status", "")),
                "items": items,
            })
out = {
    "run_id": os.environ["RUN_ID_E"],
    "finished": os.environ["ENDED_AT_E"],
    "host": os.environ["HOST_E"],
    "user": os.environ["USER_E"],
    "status": os.environ["RUN_STATUS_E"],
    "total_sources": int(os.environ.get("TOTAL_SOURCES_E", "0") or 0),
    "total_events_shipped": int(os.environ.get("TOTAL_EVENTS_SHIPPED_E", "0") or 0),
    "sources": json.loads(os.environ.get("SOURCES_JSON_E", "[]") or "[]"),
    "by_tool": json.loads(os.environ.get("BY_TOOL_JSON_E", "{}") or "{}"),
    "outputs": json.loads(os.environ.get("OUTPUTS_JSON_E", "[]") or "[]"),
    "manifest_drift": json.loads(os.environ.get("MANIFEST_DRIFT_JSON_E", "[]") or "[]"),
    "safety_integrity": os.environ["INTEGRITY_E"],
    "dry_run": int(os.environ.get("DRY_RUN_E", "0") or 0),
    "started_at": os.environ["STARTED_AT_E"],
    "ended_at": os.environ["ENDED_AT_E"],
    "sections": sections,
}
print(json.dumps(out))
PY
then
  mv "$TMPSUM" "$LATEST"
else
  rm -f "$TMPSUM"
fi
rm -f "$SECTIONS_TMP"

emit_event "run_end" "$RUN_STATUS" "${TRANSFORM_TOTAL_EVENTS:-0}" "integrity=$INTEGRITY dry_run=$DRY_RUN" "\"total_sources\":${TRANSFORM_TOTAL_SOURCES:-0},\"total_events_shipped\":${TRANSFORM_TOTAL_EVENTS:-0},\"dry_run\":$DRY_RUN"

if [ "$INTEGRITY" != "ok" ]; then
  logger -t log-shipper -p user.err "INTEGRITY VIOLATION run_id=$RUN_ID result=$INTEGRITY notes=$INTEGRITY_NOTE" 2>/dev/null || true
  echo "[FATAL] integrity violation: $INTEGRITY ($INTEGRITY_NOTE)" >&2
  EXIT_CODE=2
else
  logger -t log-shipper -p user.info "run_id=$RUN_ID status=$RUN_STATUS events=${TRANSFORM_TOTAL_EVENTS:-0} dry_run=$DRY_RUN integrity=ok" 2>/dev/null || true
fi

echo "════════════════════════════════════════════════════════════════"
exit $EXIT_CODE
