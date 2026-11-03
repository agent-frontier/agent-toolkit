#!/usr/bin/env python3
"""Idle-only cache budgets and remotely recoverable build-artifact maintenance."""

import argparse
import datetime as dt
import fcntl
import glob
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse


GIB = 1024**3
DEFAULTS = {
    "go_cache_dirs": ["$HOME/.cache/go-build", "$HOME/.cache/smith-gocache"],
    "session_roots": ["$HOME/.copilot/session-state"],
    "archive_roots": [],
    "worktree_roots": [],
    "archive_catalogs": ["$HOME/.local/state/janitor/project-archives-*.json"],
    "shared_cache_budget_gib": 16,
    "session_cache_budget_gib": 2,
    "total_cache_budget_gib": 32,
    "cache_idle_seconds": 300,
    "artifact_idle_seconds": 3600,
    "max_actions": 8,
    "max_run_seconds": 90,
    "build_min_free_gib": 20,
    "build_max_used_pct": 85,
}
GENERATED = {
    "node_modules", ".next", ".turbo", ".venv", "venv", "target", "CMakeFiles",
    "__pycache__", ".pytest_cache", "dist", "coverage", "playwright-report", "test-results",
}
SKIP_TREES = {".git", ".jj", ".smith", ".copilot", ".secrets", "captures", "assets", "data"}
GO_TOOLS = {"go", "compile", "link", "asm", "cgo"}
SESSION_MANAGERS = {"systemd", "(sd-pam)", "sshd", "ssh-agent", "gpg-agent"}
PROTECTED_ARTIFACT_DIRS = {".git", ".jj", ".secrets", ".ssh", ".aws", ".azure",
                           "captures", "recordings", "uploads", "user-data", "userdata"}
PROTECTED_ARTIFACT_FILES = {"credentials.json", "credentials.yaml", "secrets.json",
                            "secrets.yaml", "auth.json", "id_rsa", "id_ed25519",
                            ".npmrc", ".pypirc", ".netrc", ".git-credentials",
                            "cookies.json", "storage-state.json"}
PROTECTED_ARTIFACT_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3",
                               ".db", ".webm", ".mp4", ".mov", ".mkv", ".png", ".jpg",
                               ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
                               ".avif", ".heic", ".ico", ".svg"}


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        with Path(path).open(encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError:
        return default


def expand(value):
    if not isinstance(value, str):
        raise ValueError("policy paths must be strings")
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        raise ValueError(f"policy path must be absolute: {value}")
    return path


def policy_at(path):
    supplied = read_json(path, {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise ValueError("maintenance policy has an invalid shape or unknown keys")
    policy = dict(DEFAULTS, **supplied)
    for key in ("go_cache_dirs", "session_roots", "archive_roots", "worktree_roots", "archive_catalogs"):
        if not isinstance(policy[key], list):
            raise ValueError(f"{key} must be a list")
        policy[key] = [expand(value) for value in policy[key]]
    for key in set(DEFAULTS) - {"go_cache_dirs", "session_roots", "archive_roots", "worktree_roots", "archive_catalogs"}:
        if type(policy[key]) is not int or policy[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if policy["build_max_used_pct"] > 100:
        raise ValueError("build_max_used_pct must be in 1..100")
    return policy


def run(args, *, env=None, timeout=30):
    return subprocess.run(
        list(map(str, args)), env=env, capture_output=True, timeout=timeout,
        check=True,
    ).stdout


def git(root, *args, env=None):
    environment = dict(os.environ if env is None else env, GIT_OPTIONAL_LOCKS="0")
    return run(["git", "-C", root, *args], env=environment)


def size(path, *, timeout=30):
    if not path.exists():
        return 0
    return int(run(["du", "-sk", "--", path], timeout=timeout).split(maxsplit=1)[0]) * 1024


def disk_used_pct():
    info = os.statvfs("/")
    used = info.f_blocks - info.f_bfree
    available = used + info.f_bavail
    return (used * 100 + available - 1) // available if available else 0


def cache_build_lock(state, cache):
    import hashlib
    return state / "cache-build-locks" / (hashlib.sha256(os.fsencode(cache.resolve())).hexdigest() + ".lock")


def private_owned(path):
    info = path.lstat()
    return (
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
        and path.resolve() == path and path not in {Path("/"), Path.home()}
    )


def contains_reference(text, path):
    prefix = str(path)
    offset = text.find(prefix)
    while offset >= 0:
        end = offset + len(prefix)
        if end == len(text) or text[end] in "/\x00\n":
            return True
        offset = text.find(prefix, offset + 1)
    return False


def process_go_cache(environment):
    value = environment.get(b"GOCACHE")
    if value is None:
        home = environment.get(b"HOME")
        if home is None:
            return None
        config_root = environment.get(b"XDG_CONFIG_HOME", home + b"/.config")
        env_file = environment.get(b"GOENV", config_root + b"/go/env")
        if env_file != b"off":
            try:
                for line in Path(os.fsdecode(env_file)).read_bytes().splitlines():
                    if line.startswith(b"GOCACHE="):
                        value = line.split(b"=", 1)[1]
            except FileNotFoundError:
                pass
            except PermissionError:
                return None
        if value is None:
            value = environment.get(b"XDG_CACHE_HOME", home + b"/.cache") + b"/go-build"
    if value == b"off":
        return Path("/dev/null")
    path = Path(os.fsdecode(value))
    return path.resolve() if path.is_absolute() else None


class Activity:
    def __init__(self):
        ancestors = {os.getpid()}
        parent = os.getppid()
        while parent > 1 and parent not in ancestors:
            ancestors.add(parent)
            try:
                status = Path(f"/proc/{parent}/status").read_text()
                parent = int(re.search(r"^PPid:\s+(\d+)", status, re.M).group(1))
            except FileNotFoundError:
                break
        self.references = []
        self.go_caches = []
        self.unknown_go = False
        self.unreadable_owned = False
        self.unreadable_processes = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) in ancestors:
                continue
            name = ""
            try:
                owned = proc.stat().st_uid == os.getuid()
                name = (proc / "comm").read_text().strip()
                go_tool = name in GO_TOOLS or name.startswith("go.agent-tool")
                if go_tool and Path(os.readlink(proc / "exe").removesuffix(" (deleted)")).name.startswith(("python", "bash")):
                    go_tool = False
                if go_tool:
                    try:
                        environment = dict(
                            token.split(b"=", 1) for token in (proc / "environ").read_bytes().split(b"\0")
                            if b"=" in token
                        )
                        cache = process_go_cache(environment)
                        if cache is None:
                            self.unknown_go = True
                        elif cache != Path("/dev/null"):
                            self.go_caches.append(cache)
                    except PermissionError:
                        self.unknown_go = True
                if not owned:
                    continue
                for link_name in ("cwd", "exe"):
                    try:
                        self.references.append(os.readlink(proc / link_name).removesuffix(" (deleted)"))
                    except FileNotFoundError:
                        pass
                self.references.extend(os.fsdecode((proc / "cmdline").read_bytes()).split("\0"))
                for fd in (proc / "fd").iterdir():
                    try:
                        self.references.append(os.readlink(fd).removesuffix(" (deleted)"))
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                continue
            except PermissionError:
                try:
                    # Session/auth managers hide metadata; their worker children
                    # are separate processes and are still inspected.
                    if proc.stat().st_uid == os.getuid() and name not in SESSION_MANAGERS:
                        self.unreadable_owned = True
                        self.unreadable_processes.append({"pid": int(proc.name), "name": name})
                    if name in GO_TOOLS or name.startswith("go.agent-tool"):
                        self.unknown_go = True
                except FileNotFoundError:
                    continue

    def busy(self, path, *, cache=False):
        return (
            (self.unreadable_owned and not cache)
            or (cache and (self.unknown_go or path in self.go_caches))
            or any(contains_reference(text, path) for text in self.references)
        )


def recent(path, seconds):
    cutoff = time.time() - seconds
    if path.stat().st_mtime >= cutoff:
        return True
    for folder, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            try:
                if (Path(folder) / name).lstat().st_mtime >= cutoff:
                    return True
            except FileNotFoundError:
                continue
    return False


def cache_dirs(policy):
    found = {path: "shared" for path in policy["go_cache_dirs"] if path.is_dir()}
    for base in policy["session_roots"]:
        if not base.is_dir():
            continue
        for folder, dirs, files in os.walk(base, followlinks=False):
            path = Path(folder)
            depth = len(path.relative_to(base).parts)
            dirs[:] = [name for name in dirs if name not in {".git", ".jj", "node_modules", ".venv"} and depth < 6]
            if "README" not in files or "trim.txt" not in files:
                continue
            if (path / "README").read_bytes().startswith(
                b"This directory holds cached build artifacts from the Go build system."
            ):
                found[path] = "session"
                dirs.clear()
    return found


def scan_caches(policy, activity):
    rows = []
    for path, kind in cache_dirs(policy).items():
        if not private_owned(path):
            continue
        marker = path / "README"
        if marker.is_symlink() or not marker.is_file() or not marker.read_bytes().startswith(
            b"This directory holds cached build artifacts from the Go build system."
        ):
            continue
        amount = size(path)
        budget = policy["shared_cache_budget_gib" if kind == "shared" else "session_cache_budget_gib"] * GIB
        rows.append({
            "path": str(path), "kind": kind, "bytes": amount, "budget_bytes": budget,
            "active": activity.busy(path, cache=True), "eligible": False, "reason": "within_budget",
        })
    total = sum(row["bytes"] for row in rows)
    projected = total
    pressure = (
        disk_used_pct() >= policy["build_max_used_pct"]
        or shutil.disk_usage("/").free < policy["build_min_free_gib"] * GIB
    )
    for row in sorted(rows, key=lambda item: item["bytes"], reverse=True):
        if row["bytes"] < 128 * 1024**2:
            continue
        if not pressure and row["bytes"] <= row["budget_bytes"] and projected <= policy["total_cache_budget_gib"] * GIB:
            continue
        if row["active"]:
            row["reason"] = "active_build"
            projected -= max(0, row["bytes"] - row["budget_bytes"])
        elif recent(Path(row["path"]), policy["cache_idle_seconds"]):
            row["reason"] = "recent_cache_activity"
        else:
            row.update(eligible=True, reason="disk_pressure" if pressure else "over_budget")
            projected -= row["bytes"]
    return rows, total


def source_file(name):
    parts = Path(name).parts
    return not any(part in GENERATED | SKIP_TREES for part in parts) and not name.endswith((".log", ".pyc"))


def source_matches(root, record, state):
    snapshot = record["snapshot"]
    if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", snapshot):
        raise ValueError("invalid archive commit")
    with tempfile.TemporaryDirectory(prefix="janitor-maintenance-index-", dir="/tmp") as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"), GIT_TERMINAL_PROMPT="0")
        git(root, "read-tree", snapshot, env=env)
        changed = git(root, "diff", "--no-ext-diff", "--name-only", "-z", env=env).split(b"\0")
        untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z", env=env).split(b"\0")
        retained = {r["path"] for r in record.get("archive_redactions", []) if Path(r["path"]).suffix in {".md", ".txt"}}
        if any(source_file(os.fsdecode(name)) and os.fsdecode(name) not in retained for name in changed + untracked if name):
            return False
        expected_index = record.get("staged_snapshot") or record.get("head") or snapshot
        compared = subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--quiet", expected_index],
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
            capture_output=True, timeout=30, check=False,
        )
        if compared.returncode == 1:
            return False
        compared.check_returncode()
    return True


def archive_records(policy, activity, deadline):
    records = {}
    for pattern in policy["archive_catalogs"]:
        for filename in sorted(glob.glob(str(pattern))):
            doc = read_json(filename)
            if not isinstance(doc, dict) or not isinstance(doc.get("projects"), list):
                raise ValueError(f"invalid archive catalog: {filename}")
            for record in doc["projects"]:
                if not isinstance(record, dict):
                    raise ValueError(f"invalid project record: {filename}")
                if record.get("status") in {"archived-verified", "verified-existing"}:
                    if not all(isinstance(record.get(key), str) for key in ("path", "snapshot", "remote")):
                        raise ValueError(f"incomplete project record: {filename}")
                    root = expand(record["path"])
                    if any(base == root or base in root.parents for base in policy["archive_roots"]):
                        records[root] = record
    for base in policy["worktree_roots"]:
        if not base.is_dir():
            continue
        for root in sorted(base.iterdir()):
            if time.monotonic() >= deadline:
                break
            if not root.is_dir() or root.is_symlink() or not (root / ".git").exists():
                continue
            if root in records:
                continue
            lease = root.with_name(root.name + ".lock")
            if activity.busy(root) or (lease.exists() and recent(lease, policy["artifact_idle_seconds"])):
                records[root] = {
                    "protected": "protected_inspection_uncertain" if activity.unreadable_owned
                    and not (lease.exists() and recent(lease, policy["artifact_idle_seconds"]))
                    else "protected_active"
                }
                continue
            try:
                dirty = git(root, "status", "--porcelain=v1", "--untracked-files=normal").strip()
                if dirty:
                    records[root] = {"protected": "protected_unarchived_changes"}
                    continue
                head = git(root, "rev-parse", "HEAD").decode().strip()
                remote = git(root, "remote", "get-url", "origin").decode().strip()
                records[root] = {"snapshot": head, "remote": remote, "ref": head}
            except subprocess.CalledProcessError as error:
                records[root] = {"protected": "protected_git_error", "exit_code": error.returncode}
    return records


def regenerated(path, root):
    name = path.name
    if name in {".venv", "venv"}:
        return (path / "pyvenv.cfg").is_file()
    if name == "target":
        return (path.parent / "Cargo.toml").is_file() or (path / "CACHEDIR.TAG").is_file()
    if name == "CMakeFiles":
        return (path.parent / "CMakeCache.txt").is_file()
    if name in {"__pycache__", ".pytest_cache"}:
        return True
    return (path.parent / "package.json").is_file() or (root / "package.json").is_file()


def protected_artifact_contents(path):
    def uncertain(error):
        raise error
    for nested, dirs, files in os.walk(path, followlinks=False, onerror=uncertain):
        if os.path.ismount(nested):
            return True
        if any(name in PROTECTED_ARTIFACT_DIRS for name in dirs + files):
            return True
        for name in files:
            if (
                name.startswith(".env") or name in PROTECTED_ARTIFACT_FILES
                or Path(name).suffix.lower() in PROTECTED_ARTIFACT_SUFFIXES
            ):
                return True
    return False


def target_is_disposable(root, path, snapshot, idle):
    if path.resolve() != path or root not in path.parents or path.name not in GENERATED:
        return False
    if not regenerated(path, root) or recent(path, idle) or protected_artifact_contents(path):
        return False
    relative = str(path.relative_to(root))
    ignored = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", "--", relative],
        capture_output=True, timeout=15, check=False,
    )
    if ignored.returncode == 1:
        return False
    ignored.check_returncode()
    return not (
        git(root, "ls-files", "--", relative).strip()
        or git(root, "ls-tree", "-r", "--name-only", snapshot, "--", relative).strip()
    )


def artifact_paths(root, snapshot, idle):
    result = []
    for folder, dirs, _ in os.walk(root, followlinks=False):
        parent = Path(folder)
        depth = len(parent.relative_to(root).parts)
        for name in list(dirs):
            path = parent / name
            if name in SKIP_TREES or path.is_symlink() or depth >= 4:
                dirs.remove(name)
                continue
            if name not in GENERATED:
                continue
            dirs.remove(name)
            if protected_artifact_contents(path):
                raise ValueError("generated directory contains protected data")
            if not target_is_disposable(root, path, snapshot, idle):
                continue
            amount = size(path)
            if amount >= 1024**2:
                result.append({"path": str(path), "bytes": amount})
    return result


def require_network_remote(remote):
    if not (
        re.match(r"^(https|ssh|git)://[^\s]+$", remote)
        or re.match(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[^\s]+$", remote)
    ):
        raise ValueError("remote must use a supported network Git transport")
    parsed = urllib.parse.urlsplit(remote)
    if "://" in remote and (parsed.password is not None or (parsed.scheme != "ssh" and parsed.username)):
        raise ValueError("remote credentials must use Git's credential helper, not the URL")


def verify_remote(record, state):
    remote = record["remote"]
    require_network_remote(remote)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes")
    with tempfile.TemporaryDirectory(prefix=".maintenance-verify-", dir=state) as temp:
        run(["git", "init", "--bare", "--quiet", temp])
        effective = run(["git", f"--git-dir={temp}", "ls-remote", "--get-url", remote],
                        env=env).decode().strip()
        require_network_remote(effective)
        run(["git", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
             f"--git-dir={temp}", "fetch", "--quiet", "--depth=1", "--no-tags",
             remote, record["snapshot"]], env=env)
        actual = run(["git", f"--git-dir={temp}", "rev-parse", "FETCH_HEAD"]).decode().strip()
        if actual != record["snapshot"]:
            raise ValueError("remote commit does not match the saved snapshot")
        run(["git", f"--git-dir={temp}", "fsck", "--connectivity-only", "--no-dangling"])


def atomic_json(path, doc):
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(doc, out, ensure_ascii=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def cleanup_cooldown(previous, args):
    remaining = args.cooldown_remaining
    if previous.get("last_cleanup_at"):
        ended = dt.datetime.fromisoformat(previous["last_cleanup_at"])
        if ended.tzinfo is None:
            raise ValueError("maintenance cleanup timestamp must include a timezone")
        remaining = max(
            remaining,
            int(args.cooldown - (dt.datetime.now(dt.timezone.utc) - ended).total_seconds()),
        )
    return max(0, remaining)


def main(args):
    started = time.monotonic()
    overrides = [
        key for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
        if os.environ.get(key)
    ]
    if overrides:
        raise ValueError("inherited Git repository/index overrides are not allowed: " + ", ".join(overrides))
    if not 0 <= args.cooldown <= 86400 or args.cooldown_remaining < 0:
        raise ValueError("invalid cleanup cooldown")
    policy = policy_at(args.policy)
    state = expand(args.state_dir)
    previous = read_json(state / "maintenance-last.json", {})
    if not isinstance(previous, dict):
        raise ValueError("invalid maintenance state")
    remaining = cleanup_cooldown(previous, args)
    activity = Activity()
    caches, cache_total = scan_caches(policy, activity)
    doc = {
        "version": 1, "generated_at": utc(), "status": "idle", "mode": args.mode,
        "cache_budget_bytes": policy["total_cache_budget_gib"] * GIB,
        "cache_bytes": cache_total, "caches": caches, "workspaces": [], "actions": [],
        "errors": [], "removed_bytes": 0, "observed_reclaimed_bytes": 0,
        "cooldown_remaining_sec": max(0, remaining),
        "last_cleanup_at": previous.get("last_cleanup_at"),
        "last_workspace_scan_at": previous.get("last_workspace_scan_at"),
        "uninspectable_workspace_processes": getattr(activity, "unreadable_processes", []),
    }
    doc["build_admission"] = {
        "minimum_free_bytes": policy["build_min_free_gib"] * GIB,
        "maximum_used_pct": policy["build_max_used_pct"],
        "waiting": [
            read_json(path) for path in sorted((state / "waiting-go-builds").glob("*.json"))
            if path.stem.isdigit() and Path("/proc", path.stem).exists()
        ],
    }
    planned = []
    deadline = started + policy["max_run_seconds"]
    records = archive_records(policy, activity, deadline) if args.mode != "status" and remaining <= 0 else {}
    if args.mode == "status" or remaining > 0:
        doc["workspaces"] = previous.get("workspaces", [])
        doc["workspace_scan_cached"] = True
    else:
        doc["last_workspace_scan_at"] = utc()
    for root, record in records.items():
        if time.monotonic() >= deadline:
            doc["scan_budget_exhausted"] = True
            break
        if record.get("protected"):
            doc["workspaces"].append({"path": str(root), "status": record["protected"]})
            continue
        if not root.is_dir() or not private_owned(root) or record.get("orphaned_git_pointer_preserved"):
            continue
        if root == Path(__file__).resolve().parent.parent or root == Path.cwd() or root in Path.cwd().parents:
            doc["workspaces"].append({"path": str(root), "status": "protected_current_workspace"})
            continue
        lease = root.with_name(root.name + ".lock")
        if activity.busy(root) or (lease.exists() and recent(lease, policy["artifact_idle_seconds"])):
            reason = "protected_inspection_uncertain" if activity.unreadable_owned and not (
                lease.exists() and recent(lease, policy["artifact_idle_seconds"])
            ) else "protected_active"
            doc["workspaces"].append({"path": str(root), "status": reason})
            continue
        try:
            targets = artifact_paths(root, record["snapshot"], policy["artifact_idle_seconds"])
            if not targets:
                continue
            if not source_matches(root, record, state):
                doc["workspaces"].append({"path": str(root), "status": "protected_unarchived_changes"})
                continue
            entry = {"path": str(root), "status": "pending_remote_verification", "targets": targets}
            doc["workspaces"].append(entry)
            planned.append((root, record, targets, entry))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            doc["workspaces"].append({"path": str(root), "status": "protected_unverified",
                                      "error": type(error).__name__})
    if remaining > 0:
        doc["status"] = "cooldown"
    elif any(row["eligible"] for row in caches):
        doc["status"] = "ready"
    elif planned:
        doc["status"] = "ready"
    elif any(row["reason"] != "within_budget" for row in caches):
        doc["status"] = "deferred"
    if args.mode != "run":
        return doc, 0
    state.mkdir(parents=True, exist_ok=True)
    locks = []
    try:
        for lock in [state / "janitor.lock", Path(args.cron_lock)]:
            lock.parent.mkdir(parents=True, exist_ok=True)
            handle = lock.open("a")
            locks.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                doc["status"] = "busy"
                return doc, 0
        latest = read_json(state / "maintenance-last.json", {})
        if not isinstance(latest, dict):
            raise ValueError("invalid maintenance state")
        remaining = cleanup_cooldown(latest, args)
        doc["cooldown_remaining_sec"] = remaining
        doc["last_cleanup_at"] = latest.get("last_cleanup_at")
        if remaining:
            doc["status"] = "cooldown"
            doc["workspaces"] = latest.get("workspaces", [])
            doc["workspace_scan_cached"] = True
        free_before = shutil.disk_usage("/").free
        if remaining <= 0:
            go = shutil.which("go")
            if go is None:
                go = next((str(path) for path in [Path.home() / "go/bin/go", Path("/usr/local/go/bin/go")]
                           if path.is_file() and os.access(path, os.X_OK)), None)
            for row in caches:
                if time.monotonic() >= deadline:
                    doc["execution_budget_exhausted"] = True
                    break
                if not row["eligible"] or len(doc["actions"]) >= policy["max_actions"]:
                    continue
                path = Path(row["path"])
                lock_path = cache_build_lock(state, path)
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a") as cache_lock:
                    try:
                        fcntl.flock(cache_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        row.update(eligible=False, reason="participating_build")
                        continue
                    if not private_owned(path) or Activity().busy(path, cache=True) or recent(path, policy["cache_idle_seconds"]):
                        row.update(eligible=False, reason="active_build")
                        continue
                    if go is None:
                        doc["errors"].append({"path": str(path), "error": "Go executable unavailable"})
                        continue
                    try:
                        timeout = min(60, max(0.001, deadline - time.monotonic()))
                        run([go, "clean", "-cache", "-testcache"], env=dict(os.environ, GOCACHE=str(path)), timeout=timeout)
                        action = {
                            "kind": "go_cache", "path": str(path),
                            "cleanup_completed": True, "removed_bytes": None,
                        }
                        doc["actions"].append(action)
                        doc["last_cleanup_at"] = utc()
                        timeout = max(0.001, deadline - time.monotonic())
                        after = size(path, timeout=timeout)
                        removed = max(0, row["bytes"] - after)
                        doc["removed_bytes"] += removed
                        action["removed_bytes"] = removed
                    except (OSError, subprocess.SubprocessError) as exc:
                        doc["errors"].append({"path": str(path), "error": type(exc).__name__})
            for root, record, targets, entry in planned:
                if time.monotonic() - started >= policy["max_run_seconds"] or len(doc["actions"]) >= policy["max_actions"]:
                    break
                try:
                    if not source_matches(root, record, state):
                        entry["status"] = "protected_unarchived_changes"
                        continue
                    verify_remote(record, state)
                    lease = root.with_name(root.name + ".lock")
                    if (
                        Activity().busy(root) or not source_matches(root, record, state)
                        or (lease.exists() and recent(lease, policy["artifact_idle_seconds"]))
                    ):
                        entry["status"] = "protected_changed"
                        continue
                    for target in targets:
                        path = Path(target["path"])
                        if root not in path.parents or path.resolve() != path or path.name not in GENERATED:
                            raise ValueError("unsafe generated-artifact target")
                        if len(doc["actions"]) >= policy["max_actions"]:
                            break
                        if (
                            Activity().busy(root) or recent(path, policy["artifact_idle_seconds"])
                            or not source_matches(root, record, state)
                            or (lease.exists() and recent(lease, policy["artifact_idle_seconds"]))
                            or not target_is_disposable(root, path, record["snapshot"], policy["artifact_idle_seconds"])
                        ):
                            entry["status"] = "protected_changed"
                            break
                        shutil.rmtree(path)
                        doc["removed_bytes"] += target["bytes"]
                        doc["actions"].append({"kind": "build_artifact", **target, "snapshot": record["snapshot"]})
                    else:
                        entry["status"] = "verified_pruned"
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    entry.update(status="protected_unverified", error=type(exc).__name__)
        doc["observed_reclaimed_bytes"] = max(0, shutil.disk_usage("/").free - free_before)
        if doc["actions"]:
            doc["last_cleanup_at"] = utc()
            doc["status"] = "cleaned"
        if doc["errors"]:
            doc["status"] = "error"
        elif not doc["actions"] and any(
            row["status"].startswith("protected_") for row in doc["workspaces"]
        ) and remaining <= 0:
            doc["status"] = "deferred"
        doc["ended_at"] = utc()
        atomic_json(state / "maintenance-last.json", doc)
        history = state / "maintenance.jsonl"
        if history.exists() and history.stat().st_size >= 5 * 1024**2:
            for index in range(7, 0, -1):
                old = history.with_name(history.name + f".{index}")
                if old.exists():
                    os.replace(old, history.with_name(history.name + f".{index + 1}"))
            os.replace(history, history.with_name(history.name + ".1"))
        event = {key: doc[key] for key in (
            "version", "generated_at", "ended_at", "status", "actions", "errors",
            "removed_bytes", "observed_reclaimed_bytes", "last_cleanup_at",
        )}
        event["protected_workspace_counts"] = {
            value: sum(row["status"] == value for row in doc["workspaces"])
            for value in {row["status"] for row in doc["workspaces"]}
        }
        fd = os.open(history, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as out:
            out.write(json.dumps(event, ensure_ascii=True) + "\n")
        return doc, 4 if doc["errors"] else 0
    finally:
        for handle in reversed(locks):
            handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["status", "dry_run", "run"], default="status")
    parser.add_argument("--policy", default=os.path.expanduser("~/.config/system-janitor/maintenance.json"))
    parser.add_argument("--state-dir", default=os.path.expanduser("~/.local/state/janitor"))
    parser.add_argument("--cron-lock", default=os.path.expanduser("~/.local/state/agent-toolkit/cron/system-janitor.lock"))
    parser.add_argument("--cooldown", type=int, default=900)
    parser.add_argument("--cooldown-remaining", type=int, default=0)
    options = parser.parse_args()
    try:
        document, code = main(options)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        document, code = {"status": "error", "error": str(error), "generated_at": utc()}, 3
    print(json.dumps(document, ensure_ascii=True))
    sys.exit(code)
