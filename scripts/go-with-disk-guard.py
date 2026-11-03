#!/usr/bin/env python3
"""Reversible user Go launcher: queue new builds while disk space is scarce."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time


SPEC = importlib.util.spec_from_file_location(
    "maintenance", Path(__file__).resolve().with_name("janitor-maintenance.py")
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
BUILD_COMMANDS = {"build", "test", "run", "install", "generate", "get", "mod"}


def go_words(args):
    i = 0
    while i < len(args):
        if args[i] == "-C":
            i += 2
        elif args[i].startswith("-C="):
            i += 1
        else:
            return args[i:]
    return []


def go_command(args):
    words = go_words(args)
    return words[0] if words else ""


def needs_admission(args):
    words = go_words(args)
    command = words[0] if words else ""
    if command != "mod":
        return command in BUILD_COMMANDS
    return len(words) > 1 and words[1] in {"download", "tidy", "vendor"}


def running_go_ancestor():
    parent = os.getppid()
    seen = set()
    while parent > 1 and parent not in seen:
        seen.add(parent)
        try:
            proc = Path(f"/proc/{parent}")
            name = (proc / "comm").read_text().strip()
            if name == "go" or name.startswith("go.agent-tool"):
                return True
            status = (proc / "status").read_text()
            parent = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except FileNotFoundError:
            break
    return False


def enough_space(policy):
    return (
        shutil.disk_usage("/").free >= policy["build_min_free_gib"] * m.GIB
        and m.disk_used_pct() < policy["build_max_used_pct"]
    )


def launch(real_go, args, policy, state):
    if not needs_admission(args) or running_go_ancestor():
        os.execv(str(real_go), [str(real_go), *args])
    environment = {os.fsencode(k): os.fsencode(v) for k, v in os.environ.items()}
    cache = m.process_go_cache(environment)
    if cache is None:
        raise ValueError("cannot determine Go cache for build coordination")
    if cache == Path("/dev/null"):
        os.execv(str(real_go), [str(real_go), *args])
    lock_path = m.cache_build_lock(state, cache)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    queue = state / "waiting-go-builds" / f"{os.getpid()}.json"
    first_wait = None
    try:
        with lock_path.open("a") as lock:
            while True:
                reason = "disk_pressure"
                if enough_space(policy):
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        if enough_space(policy):
                            queue.unlink(missing_ok=True)
                            os.set_inheritable(lock.fileno(), True)
                            env = dict(os.environ, JANITOR_GO_INFLIGHT="1")
                            os.execve(str(real_go), [str(real_go), *args], env)
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    except BlockingIOError:
                        reason = "cache_maintenance"
                if first_wait is None:
                    first_wait = m.utc()
                    print(
                        "[janitor-go] queuing new build for disk/cache maintenance; "
                        "running builds are not interrupted", file=sys.stderr, flush=True,
                    )
                queue.parent.mkdir(parents=True, exist_ok=True)
                m.atomic_json(queue, {
                    "pid": os.getpid(), "cwd": os.getcwd(), "command": go_command(args),
                    "queued_at": first_wait, "reason": reason,
                })
                time.sleep(5)
    finally:
        queue.unlink(missing_ok=True)


if __name__ == "__main__":
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "system-janitor"
    try:
        real = Path((config / "go-real-path").read_text().strip())
        if not real.is_absolute() or real.resolve() == Path(__file__).resolve() or not os.access(real, os.X_OK):
            raise ValueError("original Go executable is unavailable; restore the recorded launcher backup")
        if not needs_admission(sys.argv[1:]) or running_go_ancestor():
            os.execv(str(real), [str(real), *sys.argv[1:]])
        policy = m.policy_at(config / "maintenance.json")
        state_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "janitor"
        launch(real, sys.argv[1:], policy, state_dir)
    except (OSError, ValueError) as error:
        print(f"[janitor-go] {error}", file=sys.stderr)
        sys.exit(3)
