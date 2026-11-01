#!/usr/bin/env bash
# Persistent-timer + disk-pressure companion for system-janitor:
# - Checks every 15 minutes via systemd user timer (Persistent=true).
# - Triggers cleanup via agent-toolkit-cron ONLY when:
#   (1) root filesystem usage >= JANITOR_PRESSURE_THRESHOLD_PCT (default: 75%), OR
#   (2) the latest daily 03:17 cron slot (after 03:30 grace) was missed or failed.
# - Staying idle-silent when healthy (<75% and daily slot satisfied) preserves warm
#   build caches and avoids inflating janitor.jsonl opt-in idle streaks.
set -uo pipefail

MODE="run"
JSON_OUT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --status)
      MODE="status"
      shift
      ;;
    --dry-run|-n)
      MODE="dry_run"
      shift
      ;;
    --json)
      JSON_OUT=1
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage: system-janitor-catchup [--status] [--dry-run|-n] [--json]

Evaluates two triggers on a 15-minute systemd user timer:
  1. Disk pressure: root filesystem Use% >= JANITOR_PRESSURE_THRESHOLD_PCT (default 75),
     gated by JANITOR_PRESSURE_COOLDOWN_SEC (default 14400s / 4h) after a real run.
  2. Daily catch-up: latest daily 03:17 slot (after 03:30 grace) missed or failed.
When triggered, sweeps stale (>=JANITOR_ORPHAN_MIN_AGE_SEC deepest-mtime) unreferenced
user /tmp orphans and runs agent-toolkit-cron system-janitor.
EOF
      exit 0
      ;;
    *)
      echo "[error] unknown argument: $1" >&2
      exit 3
      ;;
  esac
done

if [ "$JSON_OUT" -eq 1 ] && [ "$MODE" = "run" ]; then
  echo "[error] --json requires --status or --dry-run" >&2
  exit 3
fi

if [ -z "${HOME:-}" ]; then
  echo "[error] HOME is not set" >&2
  exit 3
fi

readonly STATE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}"
readonly CRON_WRAPPER="${JANITOR_CRON_WRAPPER:-${HOME}/.local/bin/agent-toolkit-cron}"
readonly JANITOR="${JANITOR_BIN:-${HOME}/.local/bin/system-janitor}"
readonly LAST_RUN="${JANITOR_LAST_RUN:-${STATE_ROOT}/janitor/last-run.json}"
readonly CRON_STATE="${JANITOR_CRON_STATE:-${STATE_ROOT}/agent-toolkit/cron/system-janitor.json}"
readonly THRESHOLD_PCT="${JANITOR_PRESSURE_THRESHOLD_PCT:-75}"
readonly COOLDOWN_SEC="${JANITOR_PRESSURE_COOLDOWN_SEC:-14400}"
readonly TMP_ROOT="${JANITOR_TMP_ROOT:-/tmp}"
readonly ORPHAN_MIN_AGE_SEC="${JANITOR_ORPHAN_MIN_AGE_SEC:-3600}"

if [ ! -x "$JANITOR" ]; then
  echo "[error] system-janitor not executable: $JANITOR" >&2
  exit 1
fi
if [ ! -x "$CRON_WRAPPER" ]; then
  echo "[error] agent-toolkit-cron not executable: $CRON_WRAPPER" >&2
  exit 1
fi
case "$THRESHOLD_PCT" in
  ''|*[!0-9]*)
    echo "[error] JANITOR_PRESSURE_THRESHOLD_PCT must be an integer in 1..100 (got: $THRESHOLD_PCT)" >&2
    exit 3
    ;;
esac
if [ "$THRESHOLD_PCT" -lt 1 ] || [ "$THRESHOLD_PCT" -gt 100 ]; then
  echo "[error] JANITOR_PRESSURE_THRESHOLD_PCT must be in 1..100 (got: $THRESHOLD_PCT)" >&2
  exit 3
fi
case "$COOLDOWN_SEC" in
  ''|*[!0-9]*)
    echo "[error] JANITOR_PRESSURE_COOLDOWN_SEC must be an integer in 0..86400 (got: $COOLDOWN_SEC)" >&2
    exit 3
    ;;
esac
if [ "$COOLDOWN_SEC" -gt 86400 ]; then
  echo "[error] JANITOR_PRESSURE_COOLDOWN_SEC must be in 0..86400 (got: $COOLDOWN_SEC)" >&2
  exit 3
fi
case "$ORPHAN_MIN_AGE_SEC" in
  ''|*[!0-9]*)
    echo "[error] JANITOR_ORPHAN_MIN_AGE_SEC must be an integer >= 60 (got: $ORPHAN_MIN_AGE_SEC)" >&2
    exit 3
    ;;
esac
if [ "$ORPHAN_MIN_AGE_SEC" -lt 60 ]; then
  echo "[error] JANITOR_ORPHAN_MIN_AGE_SEC must be >= 60 (got: $ORPHAN_MIN_AGE_SEC)" >&2
  exit 3
fi
if [ ! -d "$TMP_ROOT" ]; then
  echo "[error] JANITOR_TMP_ROOT is not a directory: $TMP_ROOT" >&2
  exit 3
fi

eval_out="$(python3 - "$LAST_RUN" "$CRON_STATE" "$THRESHOLD_PCT" "$COOLDOWN_SEC" "$TMP_ROOT" "$ORPHAN_MIN_AGE_SEC" "$MODE" "$JSON_OUT" <<'PY'
import datetime
import fcntl
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time

last_run_path = sys.argv[1]
cron_state_path = sys.argv[2]
threshold_pct = int(sys.argv[3])
cooldown_sec = int(sys.argv[4])
tmp_root = os.path.abspath(sys.argv[5])
min_age_sec = int(sys.argv[6])
mode = sys.argv[7]
json_out = int(sys.argv[8])


def is_lock_held(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    fd = None
    try:
        fd = os.open(path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def discover_active_tmp_roots(base_tmp: str) -> set[str]:
    prefix = base_tmp.rstrip("/") + "/"
    roots: set[str] = set()
    for pat in ("/proc/[0-9]*/cwd", "/proc/[0-9]*/exe", "/proc/[0-9]*/fd/*"):
        for p in glob.glob(pat):
            try:
                t = os.readlink(p)
                if t.startswith(prefix):
                    rel = t[len(prefix):]
                    top = rel.split("/", 1)[0]
                    if top:
                        roots.add(top)
            except OSError:
                pass
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(p, "rb") as fh:
                raw = fh.read(16384).decode("utf-8", errors="ignore")
            for tok in raw.split("\x00"):
                idx = tok.find(prefix)
                if idx != -1:
                    rel = tok[idx + len(prefix):]
                    top = rel.split("/", 1)[0].strip()
                    if top:
                        roots.add(top)
        except OSError:
                pass
    return roots


def newest_mtime_in_tree(path: str, initial_mtime: float, cutoff: float) -> float:
    newest = initial_mtime
    if newest >= cutoff:
        return newest
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        for name in dirs + files:
            child = os.path.join(root, name)
            try:
                st = os.lstat(child)
                if st.st_mtime > newest:
                    newest = st.st_mtime
                    if newest >= cutoff:
                        return newest
            except OSError:
                continue
    return newest


def scan_candidates(state_dir: str, base_tmp: str, min_age: int) -> tuple[list[str], list[tuple[str, bool]]]:
    now_ts = time.time()
    cutoff = now_ts - min_age
    dead_sections: list[str] = []
    if os.path.isdir(state_dir):
        for p in sorted(glob.glob(os.path.join(state_dir, ".sections.*"))):
            parts = os.path.basename(p).rsplit(".", 1)
            pid = parts[1] if len(parts) == 2 and parts[1].isdigit() else None
            if not pid or not os.path.exists(f"/proc/{pid}"):
                dead_sections.append(p)

    preserve_exact = {
        "smith-selftest-specs",
        "gopath",
        ".X11-unix",
        ".XIM-unix",
        ".ICE-unix",
        ".font-unix",
        ".Test-unix",
    }
    preserve_prefixes = (
        "tmux-",
        "copilot-detached-",
        "dotnet-diagnostic-",
        "systemd-private-",
        "go-build",
        "scratchd-",
        "smith-gocache",
        "janitor",
        "updater",
        "cert-watch",
        "log-shipper",
        ".X",
    )
    active_roots = discover_active_tmp_roots(base_tmp)
    uid = os.getuid()
    stale_tmp: list[tuple[str, bool]] = []
    try:
        entries = sorted(os.scandir(base_tmp), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        name = entry.name
        if (
            name in preserve_exact
            or name in active_roots
            or any(name.startswith(px) for px in preserve_prefixes)
        ):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if st.st_uid != uid or st.st_mtime >= cutoff:
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        if is_dir and newest_mtime_in_tree(entry.path, st.st_mtime, cutoff) >= cutoff:
            continue
        stale_tmp.append((entry.path, is_dir))
    return dead_sections, stale_tmp


def execute_orphan_sweep(dead_sections: list[str], stale_tmp: list[tuple[str, bool]]) -> tuple[int, int]:
    for p in dead_sections:
        try:
            os.unlink(p)
        except OSError as exc:
            print(f"[warn] failed to remove dead section temp {p}: {exc}", file=sys.stderr)

    reaped_count = 0
    failed_count = 0
    for path, is_dir in stale_tmp:
        try:
            if is_dir:
                shutil.rmtree(path)
            else:
                os.unlink(path)
            reaped_count += 1
        except PermissionError as exc:
            if is_dir and shutil.which("podman"):
                res = subprocess.run(
                    ["podman", "unshare", "rm", "-rf", path],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0 and not os.path.lexists(path):
                    reaped_count += 1
                else:
                    failed_count += 1
                    err_detail = (res.stderr or "").strip() or str(exc)
                    print(
                        f"[warn] failed to remove stale orphan {path} via podman unshare: {err_detail}",
                        file=sys.stderr,
                    )
            else:
                failed_count += 1
                print(f"[warn] permission denied removing stale orphan {path}: {exc}", file=sys.stderr)
        except OSError as exc:
            failed_count += 1
            print(f"[warn] failed to remove stale orphan {path}: {exc}", file=sys.stderr)

    return reaped_count, failed_count


state_dir = os.path.dirname(os.path.abspath(last_run_path))
janitor_lock = os.path.join(state_dir, "janitor.lock")
cron_lock = os.path.join(os.path.dirname(os.path.abspath(cron_state_path)), "system-janitor.lock")
run_in_progress = is_lock_held(janitor_lock) or is_lock_held(cron_lock)

st = os.statvfs("/")
used_blocks = st.f_blocks - st.f_bfree
user_total_blocks = used_blocks + st.f_bavail
used_pct = int(math.ceil(used_blocks * 100.0 / user_total_blocks)) if user_total_blocks > 0 else 0
free_bytes = st.f_bavail * st.f_frsize

now = datetime.datetime.now().astimezone()
today_slot = datetime.datetime.combine(now.date(), datetime.time(3, 17), tzinfo=now.tzinfo)
grace_cutoff = datetime.datetime.combine(now.date(), datetime.time(3, 30), tzinfo=now.tzinfo)
latest_slot = today_slot if now >= grace_cutoff else (today_slot - datetime.timedelta(days=1))

last_run_ok = False
last_run_healthy = False
last_run_completed_at = None
completed_dt = None
try:
    with open(last_run_path, encoding="utf-8") as fh:
        last_run = json.load(fh)
    finished = last_run.get("ended_at") or last_run.get("finished")
    if finished:
        completed = datetime.datetime.fromisoformat(finished.replace("Z", "+00:00"))
        if completed.tzinfo is None:
            completed = completed.astimezone()
        completed_dt = completed.astimezone(now.tzinfo)
        last_run_completed_at = completed_dt.isoformat()
        if int(last_run.get("dry_run", 1)) == 0 and last_run.get("safety_integrity") == "ok":
            last_run_healthy = True
            if completed_dt >= latest_slot:
                last_run_ok = True
except Exception:
    pass

cron_ok = True
cron_exit_code = None
if os.path.isfile(cron_state_path):
    try:
        with open(cron_state_path, encoding="utf-8") as fh:
            cron_state = json.load(fh)
        cron_exit_code = cron_state.get("exit_code")
        ended = cron_state.get("ended_at")
        if ended is None:
            run_in_progress = True
        else:
            c_end = datetime.datetime.fromisoformat(ended.replace("Z", "+00:00")).astimezone(now.tzinfo)
            if cron_exit_code != 0:
                if completed_dt is None or c_end >= completed_dt:
                    cron_ok = False
            elif c_end < latest_slot and not last_run_ok:
                cron_ok = False
    except Exception:
        if not last_run_ok:
            cron_ok = False

daily_slot_satisfied = bool(run_in_progress or (last_run_ok and cron_ok))
pressure_exceeded = used_pct >= threshold_pct

cooldown_remaining_sec = 0
if last_run_healthy and cron_ok and completed_dt is not None and cooldown_sec > 0:
    elapsed = (now - completed_dt).total_seconds()
    if 0 <= elapsed < cooldown_sec:
        cooldown_remaining_sec = int(math.ceil(cooldown_sec - elapsed))

if run_in_progress:
    status = "healthy"
    would_trigger = False
    reason = "run_in_progress"
elif not daily_slot_satisfied:
    status = "needs_run"
    would_trigger = True
    reason = "missed_daily_slot"
elif pressure_exceeded and cooldown_remaining_sec > 0:
    status = "cooldown"
    would_trigger = False
    reason = "pressure_cooldown"
elif pressure_exceeded:
    status = "needs_run"
    would_trigger = True
    reason = "disk_pressure"
else:
    status = "healthy"
    would_trigger = False
    reason = "none"

dead_sections: list[str] = []
stale_tmp: list[tuple[str, bool]] = []
if mode in ("status", "dry_run") or json_out == 1 or would_trigger:
    dead_sections, stale_tmp = scan_candidates(state_dir, tmp_root, min_age_sec)

reaped_count = 0
failed_count = 0
if mode == "run" and would_trigger:
    reaped_count, failed_count = execute_orphan_sweep(dead_sections, stale_tmp)

out = {
    "status": status,
    "would_trigger": would_trigger,
    "reason": reason,
    "used_pct": used_pct,
    "threshold_pct": threshold_pct,
    "cooldown_sec": cooldown_sec,
    "cooldown_remaining_sec": cooldown_remaining_sec,
    "free_bytes": free_bytes,
    "daily_slot": latest_slot.isoformat(),
    "daily_slot_satisfied": daily_slot_satisfied,
    "last_run_completed_at": last_run_completed_at,
    "cron_exit_code": cron_exit_code,
    "dead_section_temps": len(dead_sections),
    "stale_tmp_candidates": len(stale_tmp),
    "orphan_reaped": reaped_count,
    "orphan_failed": failed_count,
}
print(json.dumps(out))
print(
    f"{1 if would_trigger else 0}\t{reason}\t{used_pct}\t{cooldown_remaining_sec}\t"
    f"{reaped_count}\t{failed_count}\t{status}\t{daily_slot_satisfied}\t"
    f"{len(stale_tmp)}\t{len(dead_sections)}"
)
PY
)" || exit $?

if [ -z "$eval_out" ]; then
  echo "[error] failed to evaluate janitor catch-up status" >&2
  exit 1
fi

{
  IFS= read -r eval_json
  IFS=$'\t' read -r would_trigger reason used_pct cooldown_rem reaped_cnt failed_cnt status_str daily_ok stale_cnt dead_cnt
} <<< "$eval_out"

if [ "$MODE" = "status" ] || [ "$MODE" = "dry_run" ]; then
  if [ "$JSON_OUT" -eq 1 ]; then
    printf '%s\n' "$eval_json"
  else
    echo "[status] status=${status_str} would_trigger=${would_trigger} reason=${reason} used_pct=${used_pct}% (threshold=${THRESHOLD_PCT}%, cooldown_rem=${cooldown_rem}s) daily_slot_satisfied=${daily_ok} stale_tmp=${stale_cnt} dead_sections=${dead_cnt}"
  fi
  exit 0
fi

if [ "$would_trigger" != "1" ]; then
  if [ "$JSON_OUT" -eq 1 ]; then
    printf '%s\n' "$eval_json"
  elif [ "$reason" = "pressure_cooldown" ]; then
    echo "[warn] disk pressure (${used_pct}% >= ${THRESHOLD_PCT}%) persists within ${COOLDOWN_SEC}s cooldown (${cooldown_rem}s remaining); skipping to prevent cache/idle-streak thrash"
  else
    echo "[skip] no cleanup needed (reason=${reason}, used=${used_pct}%, threshold=${THRESHOLD_PCT}%)"
  fi
  exit 0
fi

echo "[trigger] reason=${reason} used_pct=${used_pct}% threshold=${THRESHOLD_PCT}%; cleaning stale orphans and invoking agent-toolkit-cron"
echo "[orphan-sweep] reaped=${reaped_cnt} failed=${failed_cnt} root=${TMP_ROOT}"

"$CRON_WRAPPER" "$JANITOR"
rc=$?
if [ "$rc" -eq 1 ]; then
  echo "[skip] another Janitor run already holds the lock"
  exit 0
fi
exit "$rc"
