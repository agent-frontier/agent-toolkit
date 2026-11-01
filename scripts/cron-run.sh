#!/usr/bin/env bash
# Scheduled-launch evidence is separate from each tool's native audit health.
set -uo pipefail
export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"
command -v python3 >/dev/null 2>&1 || { echo 'agent-toolkit-cron: python3 required' >&2; exit 3; }
exec python3 - "$@" <<'PY'
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import sys
import tempfile

VERSION = "0.2.0"
LIMITS = {"cert-watch": 7200, "log-shipper": 1800,
          "system-janitor": 172800, "system-updater": 172800}
CAPABILITIES = sorted(["atomic-state", "bounded-logs", "cron-launch",
                       "exit-code-propagation", "missing-runs", "stale-runs",
                       "status-json", "updater-dry-run"])


def now():
    return dt.datetime.now(dt.timezone.utc)


def stamp():
    return now().isoformat(timespec="seconds").replace("+00:00", "Z")


def fail(message, rc=3):
    print("agent-toolkit-cron: " + message, file=sys.stderr)
    return rc


def do_version(machine):
    if machine:
        print(json.dumps({"name": "agent-toolkit-cron", "version": VERSION,
                          "capabilities": CAPABILITIES}))
    else:
        print("agent-toolkit-cron " + VERSION)
    return 0


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value


def artifact_problem(state, tool):
    for suffix in (".json", ".log", ".lock", ".log.1", ".log.2", ".log.3"):
        path = state / (tool + suffix)
        if path.is_symlink():
            return "launcher artifacts must not be symlinks: " + str(path)
        if path.exists() and (not path.is_file() or not os.access(path, os.W_OK)):
            return "launcher artifact is not a writable file: " + str(path)
    return None


def do_status(state, machine):
    generated = now()
    entries = []
    state_problem = None
    try:
        ancestor = state
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            state_problem = "launcher state ancestor is not a directory"
        elif not os.access(ancestor, os.W_OK | os.X_OK) or os.statvfs(ancestor).f_flag & os.ST_RDONLY:
            state_problem = "launcher state location is not writable"
    except OSError as exc:
        state_problem = "cannot inspect launcher state location: " + str(exc)
    for tool, limit in LIMITS.items():
        summary = state / (tool + ".json")
        entry = {"name": tool, "status": "missing", "started_at": None,
                 "ended_at": None, "exit_code": None, "age_seconds": None,
                 "max_age_seconds": limit, "summary_path": str(summary),
                 "log_path": str(state / (tool + ".log")),
                 "detail": "no launcher record; scheduled job has not been observed"}
        try:
            with summary.open(encoding="utf-8") as fh:
                record = json.load(fh)
            if not isinstance(record, dict) or record.get("tool") != tool:
                raise ValueError("record must name the expected tool")
            started = parse_time(record["started_at"])
            ended_raw = record["ended_at"]
            rc = record["exit_code"]
            ended = parse_time(ended_raw) if ended_raw is not None else None
            if ended is None:
                if rc is not None:
                    raise ValueError("running record must have null exit_code")
            elif type(rc) is not int or not 0 <= rc <= 255 or ended < started:
                raise ValueError("invalid completion exit_code or timestamp order")
            if (started - generated).total_seconds() > 300 or (
                    ended is not None and (ended - generated).total_seconds() > 300):
                raise ValueError("timestamp more than 300 seconds in the future")
            age = max(0, int((generated - (ended or started)).total_seconds()))
            entry.update(started_at=record["started_at"], ended_at=ended_raw,
                         exit_code=rc, age_seconds=age)
            if age > limit:
                entry.update(status="stale", detail="launcher record exceeds freshness limit")
            elif ended is None:
                entry.update(status="running", detail="launch in progress; no completion yet")
            elif rc != 0:
                entry.update(status="failed", detail="launch exited nonzero; inspect startup log")
            else:
                entry.update(status="ok", detail="fresh successful launch; check native health separately")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            entry.update(status="invalid", detail="invalid launcher record: " + str(exc))
        try:
            problem = state_problem or artifact_problem(state, tool)
        except OSError as exc:
            problem = "cannot inspect launcher artifacts: " + str(exc)
        if problem:
            entry.update(status="invalid", detail=problem)
        entries.append(entry)
    states = {entry["status"] for entry in entries}
    if states & {"failed", "invalid", "stale"}:
        status, rc = "degraded", 4
    elif states & {"missing", "running"}:
        status, rc = "unknown", 5
    else:
        status, rc = "healthy", 0
    doc = {"status": status, "exit_code": rc,
           "generated_at": generated.isoformat(timespec="seconds").replace("+00:00", "Z"),
           "state_dir": str(state), "tools": entries}
    if machine:
        print(json.dumps(doc, indent=2))
    else:
        print(f"agent-toolkit-cron status: {status} (exit {rc})")
        for entry in entries:
            print(f"  {entry['name']}: {entry['status']} - {entry['detail']}")
            print(f"    log: {entry['log_path']}")
    return rc


def write_record(path, record):
    # Replace, never truncate a monitored file; concurrent readers see whole JSON.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".tmp.", delete=False) as fh:
            temp = fh.name
            json.dump(record, fh)
            fh.write("\n")
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            os.unlink(temp)


def rotate(path):
    if path.exists() and path.stat().st_size >= 1024 * 1024:
        for number in range(3, 0, -1):
            source = path if number == 1 else Path(str(path) + f".{number - 1}")
            target = Path(str(path) + f".{number}")
            if source.exists():
                os.replace(source, target)


class BoundedLog:
    limit = 1024 * 1024

    def __init__(self, path):
        self.path = path
        # Repair oversized historical files without reading unbounded input.
        for candidate in [path] + [Path(str(path) + f".{n}") for n in (1, 2, 3)]:
            if candidate.exists():
                candidate.chmod(0o600)
            if candidate.exists() and candidate.stat().st_size > self.limit:
                with candidate.open("r+b") as fh:
                    fh.seek(-self.limit, os.SEEK_END)
                    tail = fh.read(self.limit)
                    fh.seek(0)
                    fh.write(tail)
                    fh.truncate()
        rotate(path)
        self.file = path.open("ab", buffering=0)
        self.size = path.stat().st_size

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        while data:
            if self.size >= self.limit:
                self.file.close()
                rotate(self.path)
                self.file = self.path.open("ab", buffering=0)
                self.size = 0
            chunk, data = data[:self.limit - self.size], data[self.limit - self.size:]
            self.file.write(chunk)
            self.size += len(chunk)

    def close(self):
        self.file.close()


def launch(tool, home, state):
    os.umask(0o077)
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.chmod(0o700)
        problem = artifact_problem(state, tool)
        if problem:
            raise PermissionError(problem)
        lock = (state / (tool + ".lock")).open("a")
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return fail(tool + " launch already in progress", 1)
        log_path = state / (tool + ".log")
        log = BoundedLog(log_path)
    except OSError as exc:
        return fail("cannot initialize launcher state: " + str(exc))
    record = {"tool": tool, "started_at": stamp(), "ended_at": None, "exit_code": None}
    summary = state / (tool + ".json")
    child = None
    received = None

    def interrupted(signum, _frame):
        nonlocal received
        received = signum
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    rc = 3
    try:
        write_record(summary, record)
        log.write(f"{record['started_at']} starting {tool}\n")
        command = [str(home / ".local/bin" / tool)]
        if tool == "system-updater":
            command.append("--dry-run")
        if received is None:
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            deadline = None
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map() or child.poll() is None:
                    if received is not None and deadline is None:
                        deadline = time.monotonic() + 5
                    if deadline is not None and time.monotonic() >= deadline:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        deadline = None
                    for key, _ in selector.select(timeout=0.2):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if chunk:
                            log.write(chunk)
                        else:
                            selector.unregister(key.fileobj)
                rc = child.wait()
            child.stdout.close()
        if received is not None:
            rc = 128 + received
        elif rc < 0:
            rc = 128 - rc
    except OSError as exc:
        rc = 127 if isinstance(exc, FileNotFoundError) else 126 if isinstance(exc, PermissionError) else 3
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        try:
            log.write("launcher error: " + str(exc) + "\n")
        except OSError:
            fail("startup log write failed: " + str(exc))
    finally:
        record.update(ended_at=stamp(), exit_code=rc)
        try:
            write_record(summary, record)
        except OSError as exc:
            fail("launcher summary write failed: " + str(exc))
            rc = 3
        try:
            log.write(f"{record['ended_at']} finished {tool} exit={rc}\n")
        except OSError as exc:
            fail("startup log write failed: " + str(exc))
            rc = 3
        log.close()
        lock.close()
    return rc


def main():
    args = sys.argv[1:]
    if args in (["-h"], ["--help"]):
        print("""Usage: agent-toolkit-cron <tool-name|installed-tool-path>
       agent-toolkit-cron --status [--json]
       agent-toolkit-cron --version [--json]

Runs only cert-watch, log-shipper, system-janitor, or system-updater from
$HOME/.local/bin. Updater is always passed --dry-run. No extra args accepted.
Startup logs and atomic launch records: $XDG_STATE_HOME/agent-toolkit/cron
(default ~/.local/state/agent-toolkit/cron). Logs rotate at 1 MiB, 3 backups.
Status is read-only, and is not a replacement for per-tool --health --json.
Freshness limits: cert-watch 2h, log-shipper 30m, janitor/updater 48h.
Status exits: 0 healthy, 4 degraded (failed/invalid/stale), 5 unknown
(missing/running). Launch exits: tool exit code, 1 overlap, 3 precondition,
126 not executable, 127 missing executable, 128+signal on interruption.""")
        return 0
    if args in (["--version"], ["--version", "--json"]):
        return do_version("--json" in args)
    home_raw = os.environ.get("HOME", "")
    if not home_raw or not Path(home_raw).is_absolute():
        return fail("HOME must be set to an absolute path")
    home = Path(home_raw)
    state_root = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
    if not state_root.is_absolute():
        return fail("XDG_STATE_HOME must be absolute")
    state = state_root / "agent-toolkit/cron"
    if args in (["--status"], ["--status", "--json"]):
        return do_status(state, "--json" in args)
    if len(args) != 1:
        return fail("expected one tool name, --status, or --version; see --help")
    tool = next((name for name in LIMITS if args[0] in (
        name, str(home / ".local/bin" / name))), None)
    if tool is None:
        return fail("unknown tool or non-installed path; see --help")
    return launch(tool, home, state)


sys.exit(main())
PY
