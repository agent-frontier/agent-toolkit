#!/usr/bin/env bash
# cert-watch — read-only TLS certificate expiry monitor with audit-grade logging.
#
# Sibling tool to system-janitor and system-updater. Shares the same agent
# contract: capability discovery, append-only JSONL audit trail, atomic
# latest-run snapshot, read-only machine modes, health baseline recovery,
# flock single-instance, and cron-safe operation.
#
# Configuration:
#   ~/.config/cert-watch/config       (XDG_CONFIG_HOME/cert-watch/config)
# or
#   --config <path>                   (override on command line)
#
# Environment variables also accepted; see --help and examples/
# cert-watch.config.example.

set -uo pipefail

# ── Version ────────────────────────────────────────────────────────────
readonly VERSION="0.1.0"
readonly TOOLKIT_NAME="agent-toolkit"
readonly TOOLKIT_REPO="https://github.com/agent-frontier/agent-toolkit"
readonly -a TOOLKIT_TOOLS=(cert-watch system-janitor system-updater)
# ── end Version ────────────────────────────────────────────────────────

# ── Default cron-safe environment ──────────────────────────────────────
export PATH="${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
[ -z "${HOME:-}" ] && { echo "[FATAL] HOME unset" >&2; exit 3; }

# ── XDG paths ──────────────────────────────────────────────────────────
: "${XDG_CONFIG_HOME:=${HOME}/.config}"
: "${XDG_STATE_HOME:=${HOME}/.local/state}"

DEFAULT_CONFIG="${XDG_CONFIG_HOME}/cert-watch/config"

# ── CLI flags ──────────────────────────────────────────────────────────
CONFIG_FILE=""
DRY_RUN="${CERT_WATCH_DRY_RUN:-0}"
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
    --help|-h)    SHOW_HELP=1; shift ;;
    *) echo "[ERROR] unknown flag: $1" >&2; SHOW_HELP=1; shift ;;
  esac
done

# ── --only flag: canonical section list & validation ──────────────────
KNOWN_SECTIONS=(local_certs remote_certs)

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

if [ "$SHOW_HELP" = 1 ]; then
  cat <<'USAGE'
cert-watch — TLS certificate expiry monitor with audit-grade logging.

USAGE:
  cert-watch [--config <path>] [--dry-run] [--only <list>]
             [--report [--json]] [--health [--json]]
             [--doctor [--json]] [--health-acknowledge [--json]]
             [--version [--json]] [--help]

FLAGS:
  --config <path>   Source <path> for configuration (default:
                    $XDG_CONFIG_HOME/cert-watch/config)
  --dry-run, -n     Perform the read-only inventory and probe flow but emit
                    action-section status=dry_run in the audit trail. No
                    filesystem or network mutations are ever attempted; this
                    flag exists for contract symmetry with the toolkit.
  --only <list>     Run ONLY the named section(s); skip all others silently.
                    <list> is comma-separated, no spaces. Valid names:
                    local_certs, remote_certs. Execution order follows the
                    script declaration order, NOT argv order. --sections is
                    accepted as a synonym.
  --report          Print a human-readable summary of past runs from
                    $CERT_WATCH_LOG_DIR/cert-watch.jsonl and exit 0.
                    Read-only: no lock, no log-dir mutation.
  --health          Probe the audit trail and exit:
                      0  healthy
                      4  degraded
                      5  unknown (never run here)
  --doctor          Probe cert-watch self-wiring: script path resolution,
                    cron wiring, config syntax, and configured-target shape.
                    Exit 0/4/5 like --health.
  --health-acknowledge
                    Record the current size of cert-watch.jsonl as a baseline
                    so future --health probes ignore historical malformed
                    lines. Atomic tmp+rename. Pair with --json for a
                    machine-readable response.
  --json            Modifier for --report / --health / --doctor /
                    --health-acknowledge / --version. ASCII-safe JSON.
  --version         Print version. Pair with --json for the capabilities[]
                    feature-detection surface.
  --help, -h        Show this help and exit.

CONFIG (sourced as bash):
  CERT_WATCH_LOG_DIR          state directory
                              (default: $XDG_STATE_HOME/cert-watch)
  CERT_WATCH_PATHS            colon-separated paths or glob patterns to local
                              cert files (.pem/.crt/.cer). Empty by default.
                              Example:
                              /etc/ssl/certs/*.pem:/etc/letsencrypt/live/*/fullchain.pem
  CERT_WATCH_HOSTS            colon-separated host:port pairs encoded as
                              host:port:host:port:... Empty by default.
                              Example:
                              example.com:443:api.internal:8443
  CERT_WATCH_WARN_DAYS        warn threshold in days (default: 30)
  CERT_WATCH_EXPIRING_DAYS    expiring threshold in days (default: 7)
  CERT_WATCH_PROBE_TIMEOUT    remote probe timeout in seconds (default: 8)
  CERT_WATCH_SAFETY_FLOOR_DIRS
                              colon-separated dirs whose inode+size must not
                              change during the run

SECTIONS:
  local_certs     inventory configured local certificate paths/globs
  remote_certs    probe configured remote TLS endpoints (host:port)
  safety_integrity
                  verify configured safety-floor dirs were undisturbed

PER-CERT STATUS ENUM:
  ok        more than 30 days remaining
  warn      7–30 days remaining
  expiring  1–7 days remaining
  expired   already expired (< 1 day remaining)

RUN AGGREGATE STATUS:
  ok        every inventoried cert is status=ok
  warn      at least one cert is warn or expiring, and none are expired
  error     at least one cert is expired, OR at least one remote probe failed

EXIT CODES:
  0   success (default run, --report, --version, healthy doctor/health)
  1   another instance running (lock held)
  2   integrity violation (a configured safety-floor dir was disturbed)
  3   precondition failed (HOME unset, config syntax error, unknown section,
      or --json used without a read-only flag)
  4   --health/--doctor: degraded
  5   --health/--doctor: unknown (foundational signal missing)

EXAMPLES:
  # Daily cron inventory (installer default: 06:42 daily)
  42 6 * * * $HOME/.local/bin/cert-watch

  # One-shot local scan
  CERT_WATCH_PATHS="$HOME/certs/*.crt" cert-watch

  # One-shot remote scan
  CERT_WATCH_HOSTS="example.com:443:api.internal:8443" cert-watch

  # Machine-readable summary
  cert-watch --report --json

  # Feature-detection probe
  cert-watch --version --json

  # Health recovery after a historical malformed line
  cert-watch --health
  cert-watch --health-acknowledge
  cert-watch --health

LOGS:
  $CERT_WATCH_LOG_DIR/cert-watch.log       human-readable
  $CERT_WATCH_LOG_DIR/cert-watch.jsonl     one JSON event per section (append-only)
  $CERT_WATCH_LOG_DIR/last-run.json        latest summary (atomic)
  $CERT_WATCH_LOG_DIR/.health-baseline     byte offset baseline for --health

See https://github.com/agent-frontier/agent-toolkit for full docs.
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

CERT_WATCH_LOG_DIR="${CERT_WATCH_LOG_DIR:-${XDG_STATE_HOME}/cert-watch}"
CERT_WATCH_PATHS="${CERT_WATCH_PATHS:-}"
CERT_WATCH_HOSTS="${CERT_WATCH_HOSTS:-}"
CERT_WATCH_WARN_DAYS="${CERT_WATCH_WARN_DAYS:-30}"
CERT_WATCH_EXPIRING_DAYS="${CERT_WATCH_EXPIRING_DAYS:-7}"
CERT_WATCH_PROBE_TIMEOUT="${CERT_WATCH_PROBE_TIMEOUT:-8}"
CERT_WATCH_SAFETY_FLOOR_DIRS="${CERT_WATCH_SAFETY_FLOOR_DIRS:-}"

# ── Read-only report (must run BEFORE mkdir/flock/exec-redirect) ──────
do_report() {
  local jsonl="${CERT_WATCH_LOG_DIR}/cert-watch.jsonl"
  local mode="text"
  [ "$REPORT_JSON" = 1 ] && mode="json"
  local only_sections_nl=""
  if [ ${#ONLY_SECTIONS[@]} -gt 0 ]; then
    only_sections_nl="$(printf '%s\n' "${ONLY_SECTIONS[@]}")"
  fi

  REPORT_MODE="$mode" REPORT_LOG_DIR="$CERT_WATCH_LOG_DIR" REPORT_JSONL="$jsonl" ONLY_SECTIONS_ENV="$only_sections_nl" \
    python3 - <<'PY'
import datetime, json, os, sys
from collections import defaultdict

mode = os.environ["REPORT_MODE"]
log_dir = os.environ["REPORT_LOG_DIR"]
path = os.environ["REPORT_JSONL"]
only_sections = {s for s in os.environ.get("ONLY_SECTIONS_ENV", "").splitlines() if s}

generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
tracked_sections = {"local_certs", "remote_certs"}
if only_sections:
    tracked_sections &= only_sections


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
        "date_range": {"first": None, "last": None},
        "total_certs_inventoried": 0,
        "by_status": {"ok": 0, "warn": 0, "expiring": 0, "expired": 0},
        "expiring_soon": [],
        "per_source": [],
        "most_recent_run": None,
        "data_quality": {"invalid_lines": 0, "examples": []},
        "idle_streaks": [],
    }


if not os.path.isfile(path):
    if mode == "json":
        emit_json(zero_report())
    else:
        print("cert-watch — report")
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

timestamps = sorted(ev.get("ts", "") for ev in events if ev.get("ts"))
ts_first = timestamps[0] if timestamps else None
ts_last = timestamps[-1] if timestamps else None

dry_run_ids = set()
real_run_ids = set()
for ev in events:
    if ev.get("section") == "run_start":
        rid = ev.get("run_id")
        note = ev.get("note", "") or ""
        if "dry_run=1" in note:
            dry_run_ids.add(rid)
        else:
            real_run_ids.add(rid)
real_run_ids |= (set(run_ids) - dry_run_ids - real_run_ids)

per_source = defaultdict(lambda: {
    "runs": set(),
    "items_total": 0,
    "status_counts": defaultdict(int),
    "cert_status_counts": defaultdict(int),
})
by_status = {"ok": 0, "warn": 0, "expiring": 0, "expired": 0}
latest_expiring = {}
section_events = defaultdict(list)

for ev in events:
    sec = ev.get("section")
    if sec not in tracked_sections:
        continue
    rid = ev.get("run_id")
    per_source[sec]["runs"].add(rid)
    per_source[sec]["status_counts"][ev.get("status", "?")] += 1
    try:
        per_source[sec]["items_total"] += int(ev.get("items") or 0)
    except (TypeError, ValueError):
        pass
    certs = ev.get("certs")
    if isinstance(certs, list):
        for cert in certs:
            if not isinstance(cert, dict):
                continue
            status = cert.get("status")
            if status in by_status:
                by_status[status] += 1
                per_source[sec]["cert_status_counts"][status] += 1
            source = cert.get("source")
            if source:
                prev = latest_expiring.get(source)
                if prev is None or (cert.get("seen_at", ev.get("ts", "")) >= prev.get("seen_at", "")):
                    entry = dict(cert)
                    entry.setdefault("section", sec)
                    entry["seen_at"] = ev.get("ts")
                    latest_expiring[source] = entry
    if rid in real_run_ids:
        section_events[sec].append(ev)

idle_streaks = []
for sec, evs in section_events.items():
    trailing = 0
    last_productive = None
    for ev in sorted(evs, key=lambda e: e.get("ts", "")):
        status = ev.get("status", "")
        try:
            items = int(ev.get("items") or 0)
        except (TypeError, ValueError):
            items = 0
        productive = items > 0
        if productive:
            trailing = 0
            last_productive = {"run_id": ev.get("run_id"), "ts": ev.get("ts")}
        elif status == "idle" or (status == "ok" and items == 0):
            trailing += 1
        else:
            trailing = 0
    if trailing >= 2:
        idle_streaks.append({
            "section": sec,
            "consecutive_idle_runs": trailing,
            "last_productive_run": last_productive,
        })
idle_streaks.sort(key=lambda d: (-d["consecutive_idle_runs"], d["section"]))

expiring_soon = [
    cert for cert in latest_expiring.values()
    if isinstance(cert.get("days_remaining"), int) and cert["days_remaining"] < 30
]
expiring_soon.sort(key=lambda c: (c.get("days_remaining", 999999), c.get("source", "")))

last_end = None
for ev in events:
    if ev.get("section") == "run_end":
        if last_end is None or (ev.get("ts", "") > last_end.get("ts", "")):
            last_end = ev

out = zero_report()
out["total_events"] = len(events)
out["total_runs"] = len(run_ids)
out["real_runs"] = len(real_run_ids)
out["dry_runs"] = len(dry_run_ids)
out["date_range"] = {"first": ts_first, "last": ts_last}
out["total_certs_inventoried"] = sum(by_status.values())
out["by_status"] = by_status
out["expiring_soon"] = expiring_soon
out["per_source"] = [
    {
        "name": sec,
        "runs": len(info["runs"]),
        "items_total": info["items_total"],
        "status_counts": dict(sorted(info["status_counts"].items())),
        "cert_status_counts": {k: info["cert_status_counts"].get(k, 0) for k in ("ok", "warn", "expiring", "expired")},
    }
    for sec, info in sorted(per_source.items(), key=lambda kv: (-kv[1]["items_total"], kv[0]))
]
out["most_recent_run"] = None if last_end is None else {
    "run_id": last_end.get("run_id"),
    "finished": last_end.get("ts"),
    "aggregate_status": last_end.get("status"),
    "total_certs_inventoried": int(last_end.get("total_certs_inventoried") or 0),
    "by_status": last_end.get("by_status") or {"ok": 0, "warn": 0, "expiring": 0, "expired": 0},
    "dry_run": int(last_end.get("dry_run") or 0),
}
out["data_quality"] = {
    "invalid_lines": len(malformed),
    "examples": [{"line": ln, "error": err} for ln, err in malformed[:3]],
}
out["idle_streaks"] = idle_streaks

if mode == "json":
    emit_json(out)
    sys.exit(0)

print("cert-watch — report")
print(f"  log dir : {log_dir}")
if not events:
    print("  events  : 0")
    print()
    print(f"no parseable events in {path}")
    sys.exit(0)
print(f"  events  : {len(events)} (across {len(run_ids)} runs)")
print(f"  range   : {(ts_first or '?')[:10]} .. {(ts_last or '?')[:10]}")
print(f"  real runs: {len(real_run_ids)}    dry runs: {len(dry_run_ids)}")
print()
print("Per-source totals:")
print(f"  {'source':<14} {'runs':>5} {'items':>8}   section-statuses / cert-statuses")
for row in out["per_source"]:
    s_counts = ",".join(f"{k}:{v}" for k, v in sorted(row["status_counts"].items()))
    c_counts = ",".join(f"{k}:{v}" for k, v in row["cert_status_counts"].items())
    print(f"  {row['name']:<14} {row['runs']:>5} {row['items_total']:>8}   {s_counts} / {c_counts}")
print()
print("Cert status totals:")
print("  " + ", ".join(f"{k}={v}" for k, v in by_status.items()))
if expiring_soon:
    print()
    print("Expiring soon (< 30 days, latest sighting per source):")
    for cert in expiring_soon:
        print(f"  {cert.get('source','?'):<32} {cert.get('days_remaining','?'):>3}d  {cert.get('status','?'):<9} {cert.get('subject_cn','?')}")
if out["idle_streaks"]:
    print()
    print("Idle sections (no work in last N consecutive real runs):")
    for entry in out["idle_streaks"]:
        lp = entry["last_productive_run"]
        lp_str = "never" if lp is None else f"{lp.get('run_id','?')} ({lp.get('ts','?')})"
        print(f"  {entry['section']:<14} idle for {entry['consecutive_idle_runs']} runs   last productive: {lp_str}")
print()
print("Most recent run:")
if out["most_recent_run"] is None:
    print("  (none)")
else:
    mr = out["most_recent_run"]
    print(f"  {mr['run_id']}   {mr['finished']}   aggregate={mr['aggregate_status']}   certs={mr['total_certs_inventoried']}")
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
    "name": "cert-watch",
    "version": os.environ["VERSION_ENV"],
    "capabilities": sorted([
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
    printf 'cert-watch %s\n' "$VERSION"
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
  local log_dir="$CERT_WATCH_LOG_DIR"
  local jsonl="${log_dir}/cert-watch.jsonl"
  local last_run="${log_dir}/last-run.json"
  local baseline_file="${log_dir}/.health-baseline"

  HEALTH_MODE="$mode" \
  HEALTH_LOG_DIR="$log_dir" \
  HEALTH_JSONL="$jsonl" \
  HEALTH_LAST_RUN="$last_run" \
  HEALTH_BASELINE_FILE="$baseline_file" \
    python3 - <<'PY'
import datetime, json, os, sys
from collections import defaultdict

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
event_count = 0
lines = []
line_offsets = []
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
        jsonl_detail = "cert-watch.jsonl missing"
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
if jsonl_ok:
    for j, raw in enumerate(lines[parse_start_idx:]):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
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
section_count = None
if last_data is None:
    integrity_detail = "skipped: last_run_parses failed"
else:
    si = last_data.get("safety_integrity")
    secs = last_data.get("sections")
    if isinstance(secs, list):
        section_count = len(secs)
    if si == "ok":
        integrity_ok = True
        integrity_detail = f"safety_integrity=ok, {section_count} sections" if section_count is not None else "safety_integrity=ok"
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

parsed_events = []
if parses_ok:
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                parsed_events.append(obj)
        except Exception:
            pass

dry_run_ids = set()
real_run_ids = set()
run_ids_seen = []
seen = set()
for ev in parsed_events:
    rid = ev.get("run_id")
    if rid and rid not in seen:
        seen.add(rid)
        run_ids_seen.append(rid)
    if ev.get("section") == "run_start":
        if "dry_run=1" in str(ev.get("note", "") or ""):
            dry_run_ids.add(rid)
        else:
            real_run_ids.add(rid)
real_run_ids |= (set(run_ids_seen) - dry_run_ids - real_run_ids)

section_events = defaultdict(list)
for ev in parsed_events:
    sec = ev.get("section")
    if sec not in {"local_certs", "remote_certs"}:
        continue
    if ev.get("run_id") not in real_run_ids:
        continue
    section_events[sec].append(ev)

max_streak = 0
max_section = None
for sec, evs in section_events.items():
    trailing = 0
    for ev in sorted(evs, key=lambda e: e.get("ts", "")):
        try:
            items = int(ev.get("items") or 0)
        except (TypeError, ValueError):
            items = 0
        status = ev.get("status", "")
        if items > 0:
            trailing = 0
        elif status == "idle" or (status == "ok" and items == 0):
            trailing += 1
        else:
            trailing = 0
    if trailing > max_streak:
        max_streak = trailing
        max_section = sec

streak_ok = True
streak_detail = "no real runs to evaluate"
if section_events:
    streak_ok = max_streak < 5
    streak_detail = f"max consecutive_idle={max_streak} ({max_section})" if max_section else "max consecutive_idle=0"
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

if mode == "json":
    sys.stdout.write(json.dumps({
        "status": status,
        "exit_code": exit_code,
        "generated_at": generated_at,
        "log_dir": log_dir,
        "checks": checks
    }, indent=2))
    sys.stdout.write("\n")
else:
    print(f"cert-watch health: {status} (exit {exit_code})")
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

  DOCTOR_MODE="$mode" \
  DOCTOR_ARGV0="$0" \
  DOCTOR_CONFIG_PATH="$config_path" \
  DOCTOR_CONFIG_PARSE_ERROR="${CONFIG_PARSE_ERROR:-}" \
  DOCTOR_PATHS_RAW="${CERT_WATCH_PATHS:-}" \
  DOCTOR_HOSTS_RAW="${CERT_WATCH_HOSTS:-}" \
    python3 - <<'PY'
import datetime, glob, json, os, re, shlex, shutil, subprocess, sys

mode = os.environ["DOCTOR_MODE"]
argv0 = os.environ["DOCTOR_ARGV0"]
config_path = os.environ["DOCTOR_CONFIG_PATH"]
config_parse_error = os.environ.get("DOCTOR_CONFIG_PARSE_ERROR", "")
paths_raw = os.environ.get("DOCTOR_PATHS_RAW", "")
hosts_raw = os.environ.get("DOCTOR_HOSTS_RAW", "")
generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
checks = []

def norm_detail(msg):
    if msg is None:
        return None
    lines = [line.strip() for line in str(msg).splitlines() if line.strip()]
    return " | ".join(lines) if lines else ""

def add(name, ok, detail):
    checks.append({"name": name, "ok": bool(ok), "detail": norm_detail(detail)})


def split_paths(raw):
    if not raw:
        return []
    out = []
    for item in raw.split(":"):
        if not item:
            continue
        out.append(os.path.expandvars(os.path.expanduser(item)))
    return out


def parse_hosts(raw):
    if not raw:
        return []
    tokens = [tok for tok in raw.split(":") if tok]
    pairs = []
    i = 0
    while i < len(tokens):
        host = tokens[i]
        port = tokens[i + 1] if i + 1 < len(tokens) else ""
        pairs.append((host, port))
        i += 2
    return pairs

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
            if script_path and line.endswith(script_path):
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
    add("configured_targets_exist", True, "skipped: config_parses failed")
else:
    details = []
    failures = []
    raw_items = split_paths(paths_raw)
    if not raw_items:
        details.append("CERT_WATCH_PATHS unset")
    else:
        for item in raw_items:
            if any(ch in item for ch in "*?["):
                matches = sorted(glob.glob(item))
                if matches:
                    details.append(f"glob {item}: {len(matches)} match(es)")
                else:
                    details.append(f"glob {item}: no current matches")
            elif os.path.exists(item):
                details.append(f"path {item}: present")
            else:
                failures.append(f"literal path missing: {item}")
    host_pairs = parse_hosts(hosts_raw)
    if not host_pairs:
        details.append("CERT_WATCH_HOSTS unset")
    else:
        host_failures = []
        valid = 0
        for host, port in host_pairs:
            if not host or not port:
                host_failures.append(f"malformed host entry: {host}:{port}")
                continue
            if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
                host_failures.append(f"invalid host syntax: {host}")
                continue
            if not port.isdigit() or not (1 <= int(port) <= 65535):
                host_failures.append(f"invalid port: {host}:{port}")
                continue
            valid += 1
        details.append(f"hosts parsed: {valid}")
        failures.extend(host_failures)
    if failures:
        add("configured_targets_exist", False, "; ".join(failures))
    else:
        add("configured_targets_exist", True, "; ".join(details))

if script_path is None:
    status = "unknown"
    exit_code = 5
elif any(not c["ok"] for c in checks):
    status = "degraded"
    exit_code = 4
else:
    status = "healthy"
    exit_code = 0

if mode == "json":
    sys.stdout.write(json.dumps({
        "status": status,
        "exit_code": exit_code,
        "generated_at": generated_at,
        "script_path": script_path,
        "checks": checks
    }, indent=2))
    sys.stdout.write("\n")
else:
    print(f"cert-watch doctor: {status} (exit {exit_code})")
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
  local log_dir="$CERT_WATCH_LOG_DIR"
  local jsonl="${log_dir}/cert-watch.jsonl"
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
LOG_DIR="$CERT_WATCH_LOG_DIR"
LOG="${LOG_DIR}/cert-watch.log"
JSONL="${LOG_DIR}/cert-watch.jsonl"
LATEST="${LOG_DIR}/last-run.json"
LOCK="${LOG_DIR}/cert-watch.lock"
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
  logger -t cert-watch "skipped — another instance is running" 2>/dev/null || true
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
split_host_pairs() {
  local raw="$1"
  [ -n "$raw" ] || return 0
  IFS=':' read -r -a _parts <<< "$raw"
  local i=0 host port
  while [ "$i" -lt "${#_parts[@]}" ]; do
    host="${_parts[$i]}"
    port=""
    if [ $((i + 1)) -lt "${#_parts[@]}" ]; then
      port="${_parts[$((i + 1))]}"
    fi
    printf '%s:%s\n' "$host" "$port"
    i=$((i + 2))
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

OPTIN_SECTIONS=("local_certs" "remote_certs")

declare -i TOTAL_CERTS=0 TOTAL_OK=0 TOTAL_WARN=0 TOTAL_EXPIRING=0 TOTAL_EXPIRED=0 PROBE_FAILURES=0

ACTION_STATUS="ok"
ACTION_ITEMS=0
ACTION_NOTE=""
ACTION_CERTS_JSON='[]'
ACTION_CERT_STATUS_COUNTS_JSON='{"ok":0,"warn":0,"expiring":0,"expired":0}'
ACTION_OK_COUNT=0
ACTION_WARN_COUNT=0
ACTION_EXPIRING_COUNT=0
ACTION_EXPIRED_COUNT=0

action_reset() {
  ACTION_STATUS="ok"
  ACTION_ITEMS=0
  ACTION_NOTE=""
  ACTION_CERTS_JSON='[]'
  ACTION_CERT_STATUS_COUNTS_JSON='{"ok":0,"warn":0,"expiring":0,"expired":0}'
  ACTION_OK_COUNT=0
  ACTION_WARN_COUNT=0
  ACTION_EXPIRING_COUNT=0
  ACTION_EXPIRED_COUNT=0
}

act_local_certs() {
  local result
  result="$(CERT_WATCH_PATHS_E="$CERT_WATCH_PATHS" CERT_WATCH_WARN_DAYS_E="$CERT_WATCH_WARN_DAYS" CERT_WATCH_EXPIRING_DAYS_E="$CERT_WATCH_EXPIRING_DAYS" python3 - <<'PY'
import datetime, glob, json, math, os, shlex, subprocess

raw = os.environ.get("CERT_WATCH_PATHS_E", "")
warn_days = os.environ.get("CERT_WATCH_WARN_DAYS_E", "30")
expiring_days = os.environ.get("CERT_WATCH_EXPIRING_DAYS_E", "7")


def int_or(value, default):
    try:
        return int(str(value).strip())
    except Exception:
        return default

warn_days = int_or(warn_days, 30)
expiring_days = int_or(expiring_days, 7)
if expiring_days > warn_days:
    expiring_days = warn_days


def split_paths(raw):
    out = []
    for item in raw.split(":"):
        if not item:
            continue
        out.append(os.path.expandvars(os.path.expanduser(item)))
    return out


def iter_targets(raw):
    seen = set()
    for item in split_paths(raw):
        if any(ch in item for ch in "*?["):
            for match in sorted(glob.glob(item)):
                if match not in seen:
                    seen.add(match)
                    yield match
        else:
            if item not in seen:
                seen.add(item)
                yield item


def parse_cn(line):
    value = line.split("=", 1)[1].strip() if "=" in line else line.strip()
    for marker in ("CN =", "CN=", "/CN="):
        idx = value.find(marker)
        if idx != -1:
            rest = value[idx + len(marker):].strip()
            return rest.split(",", 1)[0].split("/", 1)[0].strip()
    return value


def classify(seconds_remaining):
    if seconds_remaining < 0:
        return 0, "expired"
    days_remaining = int(math.ceil(seconds_remaining / 86400.0))
    if days_remaining <= expiring_days:
        return days_remaining, "expiring"
    if days_remaining <= warn_days:
        return days_remaining, "warn"
    return days_remaining, "ok"


def inspect_cert(args, source):
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"openssl rc={proc.returncode}").strip())
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    subject = next((line for line in lines if line.startswith("subject=")), "subject=")
    issuer = next((line for line in lines if line.startswith("issuer=")), "issuer=")
    not_before = next((line.split("=", 1)[1].strip() for line in lines if line.startswith("notBefore=")), "")
    not_after = next((line.split("=", 1)[1].strip() for line in lines if line.startswith("notAfter=")), "")
    fingerprint = next((line.split("=", 1)[1].strip() for line in lines if "Fingerprint=" in line), "")
    dt_after = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y GMT").replace(tzinfo=datetime.timezone.utc)
    seconds_remaining = (dt_after - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    days_remaining, status = classify(seconds_remaining)
    return {
        "subject_cn": parse_cn(subject),
        "issuer_cn": parse_cn(issuer),
        "not_before": not_before,
        "not_after": not_after,
        "days_remaining": days_remaining,
        "sha256_fingerprint": fingerprint,
        "source": source,
        "status": status
    }

certs = []
errors = []
for path in iter_targets(raw):
    if not os.path.isfile(path):
        continue
    try:
        certs.append(inspect_cert([
            "openssl", "x509", "-in", path, "-noout",
            "-subject", "-issuer", "-dates", "-fingerprint", "-sha256"
        ], path))
    except Exception as e:
        errors.append(f"{path}: {str(e)[:120]}")

counts = {"ok": 0, "warn": 0, "expiring": 0, "expired": 0}
for cert in certs:
    counts[cert["status"]] += 1
status = "warn" if errors else "ok"
note = "; ".join(errors[:5]) if errors else ("CERT_WATCH_PATHS unset" if not raw else ("no matching certificate files" if not certs else f"inventoried {len(certs)} local cert(s)"))
print(f"ACTION_STATUS={shlex.quote(status)}")
print(f"ACTION_ITEMS={len(certs)}")
print(f"ACTION_NOTE={shlex.quote(note)}")
print(f"ACTION_CERTS_JSON={shlex.quote(json.dumps(certs, separators=(',', ':')))}")
print(f"ACTION_CERT_STATUS_COUNTS_JSON={shlex.quote(json.dumps(counts, separators=(',', ':')))}")
print(f"ACTION_OK_COUNT={counts['ok']}")
print(f"ACTION_WARN_COUNT={counts['warn']}")
print(f"ACTION_EXPIRING_COUNT={counts['expiring']}")
print(f"ACTION_EXPIRED_COUNT={counts['expired']}")
PY
)" || return 1
  eval "$result"
}

act_remote_certs() {
  local result
  result="$(CERT_WATCH_HOSTS_E="$CERT_WATCH_HOSTS" CERT_WATCH_WARN_DAYS_E="$CERT_WATCH_WARN_DAYS" CERT_WATCH_EXPIRING_DAYS_E="$CERT_WATCH_EXPIRING_DAYS" CERT_WATCH_PROBE_TIMEOUT_E="$CERT_WATCH_PROBE_TIMEOUT" python3 - <<'PY'
import datetime, json, math, os, shlex, subprocess

raw = os.environ.get("CERT_WATCH_HOSTS_E", "")
warn_days = os.environ.get("CERT_WATCH_WARN_DAYS_E", "30")
expiring_days = os.environ.get("CERT_WATCH_EXPIRING_DAYS_E", "7")
timeout_secs = os.environ.get("CERT_WATCH_PROBE_TIMEOUT_E", "8")


def int_or(value, default):
    try:
        return int(str(value).strip())
    except Exception:
        return default

warn_days = int_or(warn_days, 30)
expiring_days = int_or(expiring_days, 7)
probe_timeout = max(int_or(timeout_secs, 8), 1)
if expiring_days > warn_days:
    expiring_days = warn_days


def parse_hosts(raw):
    tokens = [tok for tok in raw.split(":") if tok]
    out = []
    i = 0
    while i < len(tokens):
        host = tokens[i]
        port = tokens[i + 1] if i + 1 < len(tokens) else ""
        out.append((host, port))
        i += 2
    return out


def parse_cn(line):
    value = line.split("=", 1)[1].strip() if "=" in line else line.strip()
    for marker in ("CN =", "CN=", "/CN="):
        idx = value.find(marker)
        if idx != -1:
            rest = value[idx + len(marker):].strip()
            return rest.split(",", 1)[0].split("/", 1)[0].strip()
    return value


def classify(seconds_remaining):
    if seconds_remaining < 0:
        return 0, "expired"
    days_remaining = int(math.ceil(seconds_remaining / 86400.0))
    if days_remaining <= expiring_days:
        return days_remaining, "expiring"
    if days_remaining <= warn_days:
        return days_remaining, "warn"
    return days_remaining, "ok"


def inspect_pem(pem_text, source):
    proc = subprocess.run([
        "openssl", "x509", "-noout", "-subject", "-issuer", "-dates", "-fingerprint", "-sha256"
    ], input=pem_text, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"openssl rc={proc.returncode}").strip())
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    subject = next((line for line in lines if line.startswith("subject=")), "subject=")
    issuer = next((line for line in lines if line.startswith("issuer=")), "issuer=")
    not_before = next((line.split("=", 1)[1].strip() for line in lines if line.startswith("notBefore=")), "")
    not_after = next((line.split("=", 1)[1].strip() for line in lines if line.startswith("notAfter=")), "")
    fingerprint = next((line.split("=", 1)[1].strip() for line in lines if "Fingerprint=" in line), "")
    dt_after = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y GMT").replace(tzinfo=datetime.timezone.utc)
    seconds_remaining = (dt_after - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    days_remaining, status = classify(seconds_remaining)
    return {
        "subject_cn": parse_cn(subject),
        "issuer_cn": parse_cn(issuer),
        "not_before": not_before,
        "not_after": not_after,
        "days_remaining": days_remaining,
        "sha256_fingerprint": fingerprint,
        "source": source,
        "status": status
    }


def extract_leaf(text):
    start = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    try:
        s = text.index(start)
        e = text.index(end, s) + len(end)
    except ValueError:
        return None
    return text[s:e] + "\n"

certs = []
errors = []
for host, port in parse_hosts(raw):
    source = f"{host}:{port}"
    if not host or not port or not port.isdigit():
        errors.append(f"{source}: malformed host entry")
        continue
    try:
        proc = subprocess.run([
            "openssl", "s_client", "-connect", source, "-servername", host, "-showcerts"
        ], input="", capture_output=True, text=True, check=False, timeout=probe_timeout)
    except subprocess.TimeoutExpired:
        errors.append(f"{source}: probe timed out after {probe_timeout}s")
        continue
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or f"s_client rc={proc.returncode}").strip().splitlines()
        errors.append(f"{source}: {(msg[0] if msg else 'probe failed')[:120]}")
        continue
    pem = extract_leaf(proc.stdout)
    if not pem:
        errors.append(f"{source}: no leaf certificate found")
        continue
    try:
        certs.append(inspect_pem(pem, source))
    except Exception as e:
        errors.append(f"{source}: {str(e)[:120]}")

counts = {"ok": 0, "warn": 0, "expiring": 0, "expired": 0}
for cert in certs:
    counts[cert["status"]] += 1
status = "warn" if errors else "ok"
note = "; ".join(errors[:5]) if errors else ("CERT_WATCH_HOSTS unset" if not raw else ("no remote certs inventoried" if not certs else f"inventoried {len(certs)} remote cert(s)"))
print(f"ACTION_STATUS={shlex.quote(status)}")
print(f"ACTION_ITEMS={len(certs)}")
print(f"ACTION_NOTE={shlex.quote(note)}")
print(f"ACTION_CERTS_JSON={shlex.quote(json.dumps(certs, separators=(',', ':')))}")
print(f"ACTION_CERT_STATUS_COUNTS_JSON={shlex.quote(json.dumps(counts, separators=(',', ':')))}")
print(f"ACTION_OK_COUNT={counts['ok']}")
print(f"ACTION_WARN_COUNT={counts['warn']}")
print(f"ACTION_EXPIRING_COUNT={counts['expiring']}")
print(f"ACTION_EXPIRED_COUNT={counts['expired']}")
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
    ACTION_CERTS_JSON='[]'
    ACTION_CERT_STATUS_COUNTS_JSON='{"ok":0,"warn":0,"expiring":0,"expired":0}'
  fi
  emit_status="$ACTION_STATUS"
  if [ "$DRY_RUN" = "1" ] && [ "$emit_status" != "warn" ]; then
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
  echo "[done] section='$name' status=$emit_status items=$ACTION_ITEMS"
  emit_event "$name" "$emit_status" "$ACTION_ITEMS" "$ACTION_NOTE" "\"certs\":$ACTION_CERTS_JSON,\"cert_status_counts\":$ACTION_CERT_STATUS_COUNTS_JSON"
  TOTAL_CERTS=$((TOTAL_CERTS + ACTION_ITEMS))
  TOTAL_OK=$((TOTAL_OK + ACTION_OK_COUNT))
  TOTAL_WARN=$((TOTAL_WARN + ACTION_WARN_COUNT))
  TOTAL_EXPIRING=$((TOTAL_EXPIRING + ACTION_EXPIRING_COUNT))
  TOTAL_EXPIRED=$((TOTAL_EXPIRED + ACTION_EXPIRED_COUNT))
  if [ "$ACTION_STATUS" = "warn" ]; then
    PROBE_FAILURES=$((PROBE_FAILURES + 1))
  fi
}

# ── Run header ─────────────────────────────────────────────────────────
declare -A SAFETY_BEFORE_INODE SAFETY_BEFORE_SIZE
SAFETY_DIRS_LIST=""
if [ -n "$CERT_WATCH_SAFETY_FLOOR_DIRS" ]; then
  while IFS= read -r d; do
    SAFETY_DIRS_LIST="$SAFETY_DIRS_LIST $d"
    SAFETY_BEFORE_INODE["$d"]=$(dir_inode "$d")
    SAFETY_BEFORE_SIZE["$d"]=$(dir_size "$d")
  done < <(split_paths "$CERT_WATCH_SAFETY_FLOOR_DIRS")
fi

echo "════════════════════════════════════════════════════════════════"
echo " cert-watch"
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
run_section "local_certs" act_local_certs
run_section "remote_certs" act_remote_certs

# ── Integrity check on safety-floor dirs ──────────────────────────────
section "Integrity check"
INTEGRITY="ok"
INTEGRITY_NOTE=""
if [ -n "$CERT_WATCH_SAFETY_FLOOR_DIRS" ]; then
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
  done < <(split_paths "$CERT_WATCH_SAFETY_FLOOR_DIRS")
else
  echo "(no safety-floor dirs configured)"
fi
echo "result: $INTEGRITY"
if [ -z "$CERT_WATCH_SAFETY_FLOOR_DIRS" ]; then
  emit_event "safety_integrity" "$INTEGRITY" 0 "no_safety_dirs_configured"
else
  emit_event "safety_integrity" "$INTEGRITY" 0 "${INTEGRITY_NOTE:-clean}"
fi

# ── Final summary ──────────────────────────────────────────────────────
EXIT_CODE=0
AGGREGATE_STATUS="ok"
if [ "$PROBE_FAILURES" -gt 0 ] || [ "$TOTAL_EXPIRED" -gt 0 ]; then
  AGGREGATE_STATUS="error"
elif [ "$TOTAL_WARN" -gt 0 ] || [ "$TOTAL_EXPIRING" -gt 0 ]; then
  AGGREGATE_STATUS="warn"
fi

section "Summary"
echo
echo "run_id                 : $RUN_ID"
echo "aggregate status       : $AGGREGATE_STATUS"
echo "total certs inventoried: $TOTAL_CERTS"
echo "by_status              : ok=$TOTAL_OK warn=$TOTAL_WARN expiring=$TOTAL_EXPIRING expired=$TOTAL_EXPIRED"
echo "safety integrity       : $INTEGRITY"

RUN_ENDED_AT="$(ts)"
TMPSUM="${LATEST}.tmp.$$"
if RUN_ID_E="$RUN_ID" \
   STARTED_AT_E="$RUN_STARTED_AT" \
   ENDED_AT_E="$RUN_ENDED_AT" \
   HOST_E="$HOSTNAME_S" \
   USER_E="$USER_S" \
   DRY_RUN_E="$DRY_RUN" \
   AGGREGATE_STATUS_E="$AGGREGATE_STATUS" \
   TOTAL_CERTS_E="$TOTAL_CERTS" \
   TOTAL_OK_E="$TOTAL_OK" \
   TOTAL_WARN_E="$TOTAL_WARN" \
   TOTAL_EXPIRING_E="$TOTAL_EXPIRING" \
   TOTAL_EXPIRED_E="$TOTAL_EXPIRED" \
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
                "items": items
            })
out = {
    "run_id": os.environ["RUN_ID_E"],
    "finished": os.environ["ENDED_AT_E"],
    "host": os.environ["HOST_E"],
    "user": os.environ["USER_E"],
    "aggregate_status": os.environ["AGGREGATE_STATUS_E"],
    "total_certs_inventoried": int(os.environ.get("TOTAL_CERTS_E", "0") or 0),
    "by_status": {
        "ok": int(os.environ.get("TOTAL_OK_E", "0") or 0),
        "warn": int(os.environ.get("TOTAL_WARN_E", "0") or 0),
        "expiring": int(os.environ.get("TOTAL_EXPIRING_E", "0") or 0),
        "expired": int(os.environ.get("TOTAL_EXPIRED_E", "0") or 0)
    },
    "safety_integrity": os.environ["INTEGRITY_E"],
    "dry_run": int(os.environ.get("DRY_RUN_E", "0") or 0),
    "started_at": os.environ["STARTED_AT_E"],
    "ended_at": os.environ["ENDED_AT_E"],
    "sections": sections
}
print(json.dumps(out))
PY
then
  mv "$TMPSUM" "$LATEST"
else
  {
    printf '{'
    printf '"run_id":%s,' "$(jsonescape "$RUN_ID")"
    printf '"finished":%s,' "$(jsonescape "$RUN_ENDED_AT")"
    printf '"host":%s,' "$(jsonescape "$HOSTNAME_S")"
    printf '"user":%s,' "$(jsonescape "$USER_S")"
    printf '"aggregate_status":%s,' "$(jsonescape "$AGGREGATE_STATUS")"
    printf '"total_certs_inventoried":%s,' "$TOTAL_CERTS"
    printf '"by_status":{"ok":%s,"warn":%s,"expiring":%s,"expired":%s},' "$TOTAL_OK" "$TOTAL_WARN" "$TOTAL_EXPIRING" "$TOTAL_EXPIRED"
    printf '"safety_integrity":%s,' "$(jsonescape "$INTEGRITY")"
    printf '"dry_run":%s' "$DRY_RUN"
    printf '}\n'
  } > "$TMPSUM" && mv "$TMPSUM" "$LATEST"
fi
rm -f "$SECTIONS_TMP"

emit_event "run_end" "$AGGREGATE_STATUS" "$TOTAL_CERTS" "integrity=$INTEGRITY dry_run=$DRY_RUN" "\"aggregate_status\":$(jsonescape "$AGGREGATE_STATUS"),\"total_certs_inventoried\":$TOTAL_CERTS,\"by_status\":{\"ok\":$TOTAL_OK,\"warn\":$TOTAL_WARN,\"expiring\":$TOTAL_EXPIRING,\"expired\":$TOTAL_EXPIRED},\"dry_run\":$DRY_RUN"

if [ "$INTEGRITY" != "ok" ]; then
  logger -t cert-watch -p user.err "INTEGRITY VIOLATION run_id=$RUN_ID result=$INTEGRITY notes=$INTEGRITY_NOTE" 2>/dev/null || true
  echo "[FATAL] integrity violation: $INTEGRITY ($INTEGRITY_NOTE)" >&2
  EXIT_CODE=2
else
  logger -t cert-watch -p user.info "run_id=$RUN_ID aggregate=$AGGREGATE_STATUS certs=$TOTAL_CERTS dry_run=$DRY_RUN integrity=ok" 2>/dev/null || true
fi

echo "════════════════════════════════════════════════════════════════"
exit $EXIT_CODE
