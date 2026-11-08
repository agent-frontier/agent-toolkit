"""Explicit, read-only GNU du scans. Discovery never creates a thread or child."""

import copy
import datetime as dt
import errno
import heapq
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time


MAX_ROWS = 128
MAX_COMPONENTS = 64
MAX_RECORD = 4096
WARNING_LIMIT = 4096
VIEW_LIMIT = 512 * 1024
CHUNK = 32 * 1024
ROOT_LABELS = {"home": "Home", "root": "Root filesystem", "tmp": "/tmp"}
ACTIVE = {"starting", "running", "cancelling", "finalization_failed"}
_DEFAULT_DU = object()


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def display_name(raw):
    # Keep raw bytes only in private identities. JSON/DOM names visibly escape controls and invalid UTF-8.
    value = raw.decode("utf-8", "backslashreplace")
    return "".join({"\n": r"\n", "\t": r"\t", "\r": r"\r"}.get(char,
                   f"\\u{ord(char):04x}" if ord(char) < 32 or ord(char) == 127
                   or 0x202a <= ord(char) <= 0x202e or 0x2066 <= ord(char) <= 0x2069 else char)
                   for char in value)


class StorageError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class DuParser:
    """Drain NUL records with a bounded buffer/heap, retaining full directory sums."""

    def __init__(self, alias):
        self.alias = alias
        self.buffer = bytearray()
        self.discarding = False
        self.error = None
        self.root_bytes = None
        self.child_bytes = 0
        self.child_count = 0
        self.heap = []

    @property
    def rows(self):
        return sorted(self.heap, key=lambda row: (-row[0], row[1]))

    def record(self, raw):
        size, separator, path = raw.partition(b"\t")
        if not separator or not re.fullmatch(rb"[0-9]{1,20}", size) or int(size) > 2**64 - 1:
            self.error = self.error or "Invalid decimal allocated-byte record."
            return
        size = int(size)
        if path == self.alias:
            if self.root_bytes is not None:
                self.error = self.error or "Duplicate root record."
            self.root_bytes = size
            return
        prefix = self.alias + b"/"
        name = path[len(prefix):] if path.startswith(prefix) else b""
        if not name or len(name) > 255 or b"/" in name or name in (b".", b".."):
            self.error = self.error or "Output path is not an immediate child of the pinned directory."
            return
        self.child_count += 1
        self.child_bytes += size
        entry = (size, name)
        if len(self.heap) < MAX_ROWS:
            heapq.heappush(self.heap, entry)
        elif entry > self.heap[0]:
            heapq.heapreplace(self.heap, entry)

    def feed(self, chunk):
        # Never split on lines: both newlines and tabs may be literal filename bytes.
        for part in chunk.split(b"\0")[:-1]:
            if not self.discarding and len(self.buffer) + len(part) <= MAX_RECORD:
                self.buffer.extend(part)
                self.record(bytes(self.buffer))
            else:
                self.error = self.error or "Storage output record exceeds the bounded parser limit."
            self.buffer.clear()
            self.discarding = False
        tail = chunk.rsplit(b"\0", 1)[-1]
        if self.discarding or len(self.buffer) + len(tail) > MAX_RECORD:
            self.error = self.error or "Storage output record exceeds the bounded parser limit."
            self.buffer.clear()
            self.discarding = True
        else:
            self.buffer.extend(tail)

    def finish(self):
        if self.buffer or self.discarding:
            self.error = self.error or "Incomplete NUL-terminated storage output."
        if self.root_bytes is not None and self.root_bytes < self.child_bytes:
            self.error = self.error or "Root total is smaller than the reported directory subtotal."
        if self.error:
            raise ValueError(self.error)


class Output:
    def __init__(self, alias):
        self.parser = DuParser(alias)
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.sample = bytearray()
        self.lines = 0
        self.trailing_line = False
        self.permission_denied = False
        self.stderr_tail = b""

    def accept(self, channel, raw):
        if channel == "stdout":
            self.stdout_bytes += len(raw)
            self.parser.feed(raw)
        else:
            self.stderr_bytes += len(raw)
            self.sample.extend(raw[:max(0, WARNING_LIMIT - len(self.sample))])
            self.lines += raw.count(b"\n")
            self.trailing_line = not raw.endswith(b"\n")
            self.permission_denied |= b"Permission denied" in self.stderr_tail + raw
            self.stderr_tail = raw[-32:]

    def fields(self):
        return {"stdout_bytes": self.stdout_bytes, "stderr_bytes": self.stderr_bytes,
                "warning_count": self.lines + int(self.trailing_line),
                "warning_sample": self.sample.decode("utf-8", "backslashreplace"),
                "warning_truncated": self.stderr_bytes > WARNING_LIMIT,
                "permission_denied": self.permission_denied}


class StorageScanner:
    def __init__(self, enabled=False, roots=None, wall_seconds=120, du_path=_DEFAULT_DU,
                 terminate_grace=0.25, reap_seconds=1):
        self.enabled = enabled
        self.roots = roots if roots is not None else {"home": Path.home(), "root": Path("/"), "tmp": Path("/tmp")}
        self.du_path = shutil.which("du") if du_path is _DEFAULT_DU else du_path
        self.available = sys.platform == "linux" and self.du_path is not None
        self.wall_seconds = wall_seconds
        self.terminate_grace = terminate_grace
        self.reap_seconds = reap_seconds
        self.lock = threading.Lock()
        self._job = None
        self._targets = {}
        self._worker = None
        self._process = None
        self._output = None
        self._cancel = threading.Event()
        self._closing = False
        self._started = None
        self._finished = None

    def metadata(self):
        return {"supported": True, "api_version": 1, "enabled": bool(self.enabled),
                "available": self.available, "capabilities": ["storage-scan"],
                "roots": [{"id": name, "label": label} for name, label in ROOT_LABELS.items()],
                "limits": {"wall_seconds": self.wall_seconds, "max_directories": MAX_ROWS,
                           "max_components": MAX_COMPONENTS, "record_bytes": MAX_RECORD,
                           "warning_sample_bytes": WARNING_LIMIT, "view_bytes": VIEW_LIMIT},
                "detail": None if self.available else "Linux GNU du is unavailable; no fallback scan."}

    def _view(self):
        job = copy.deepcopy(self._job)
        if job is not None:
            job["elapsed_seconds"] = round(max(0, (self._finished or time.monotonic()) - self._started), 3)
        return {**self.metadata(), "read_at": utc(), "job": job}

    def describe(self):
        with self.lock:
            return self._view()

    def _open(self, preset, expected=None):
        flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parts = expected[1:] if expected else [
            (part, None, None) for part in os.fsencode(self.roots[preset]).split(b"/") if part
        ]
        if not os.path.isabs(self.roots[preset]) or len(parts) >= MAX_COMPONENTS or any(
            part[0] in (b".", b"..") for part in parts
        ):
            raise StorageError(400, "Preset or navigation exceeds the fixed directory boundary.")
        fd = None
        components = []
        try:
            fd = os.open(b"/", flags)
            info = os.fstat(fd)
            components.append((b"", info.st_dev, info.st_ino))
            if expected and components[0] != expected[0]:
                raise StorageError(409, "Root directory identity changed; select a preset for a fresh scan.")
            for name, device, inode in parts:
                next_fd = os.open(name, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode):
                    raise StorageError(400, "Only no-follow directories may be scanned.")
                if expected and (info.st_dev, info.st_ino) != (device, inode):
                    raise StorageError(409, "Directory identity changed; select a preset for a fresh scan.")
                components.append((name, info.st_dev, info.st_ino))
            return fd, tuple(components)
        except OSError as error:
            if fd is not None:
                os.close(fd)
            status = 409 if expected else 403 if error.errno in (errno.EACCES, errno.EPERM) else 400
            raise StorageError(status, "Directory unavailable, replaced, or contains a symlink; no scan started.") from error
        except StorageError:
            if fd is not None:
                os.close(fd)
            raise

    def _target(self, preset, components, root_depth):
        identifier = secrets.token_hex(16)
        self._targets[identifier] = (preset, components, root_depth)
        return identifier

    def start(self, payload):
        if not self.enabled:
            raise StorageError(403, "Storage scan launch is disabled in observer mode.")
        if type(payload) is not dict or set(payload) not in ({"root"}, {"job_id", "directory_id"}):
            raise StorageError(400, "Use one root preset OR the current job_id and directory_id; no other fields.")
        if "root" in payload:
            if type(payload["root"]) is not str or payload["root"] not in ROOT_LABELS:
                raise StorageError(400, "Unknown storage root preset.")
        elif any(type(payload[key]) is not str or not re.fullmatch(r"[0-9a-f]{32}", payload[key])
                 for key in ("job_id", "directory_id")):
            raise StorageError(400, "Invalid storage job or directory ID.")
        with self.lock:
            if self._closing:
                raise StorageError(503, "Storage scanner is shutting down.")
            if self._process is not None or self._worker is not None and self._worker.is_alive():
                raise StorageError(409, "One owned storage scan is already active or awaiting finalization.")
            if not self.available:
                raise StorageError(503, "Linux GNU du is unavailable; no fallback scan.")
            if "root" in payload:
                preset = payload["root"]
                fd, components = self._open(preset)
                root_depth = len(components)
            else:
                if self._job is None or payload["job_id"] != self._job["id"]:
                    raise StorageError(409, "Stale storage job ID; refresh the current result.")
                target = self._targets.get(payload["directory_id"])
                if target is None:
                    raise StorageError(400, "Unknown current-job directory ID.")
                preset, expected, root_depth = target
                fd, components = self._open(preset, expected)
                if any(component[1] != components[root_depth - 1][1] for component in components[root_depth:]):
                    os.close(fd)
                    raise StorageError(409, "Navigation cannot cross the selected filesystem.")
            self._targets = {}
            breadcrumbs = []
            for depth in range(root_depth, len(components) + 1):
                identifier = self._target(preset, components[:depth], root_depth)
                breadcrumbs.append({"id": identifier, "name": ROOT_LABELS[preset] if depth == root_depth
                                    else display_name(components[depth - 1][0])})
            self._started, self._finished = time.monotonic(), None
            self._cancel = threading.Event()
            self._job = {"id": secrets.token_hex(16), "root": preset, "state": "starting",
                         "directory_id": breadcrumbs[-1]["id"], "breadcrumbs": breadcrumbs,
                         "started_at": utc(), "finished_at": None, "root_bytes": None,
                         "directories": [], "omitted_directories": {"count": 0, "bytes": 0},
                         "direct_bytes": None, "coverage": "unknown", "display_truncated": False,
                         "error": None, "error_code": None, "stop_reason": None, "exit_code": None,
                         **Output(b"").fields()}
            self._worker = threading.Thread(target=self._run, args=(fd, preset, components, root_depth),
                                            name="dashboard-storage-scan", daemon=True)
            try:
                self._worker.start()
            except RuntimeError as error:
                os.close(fd)
                self._terminal("failed", "worker_start_failed", "Storage worker could not start.")
                raise StorageError(503, "Storage worker could not start; no child was launched.") from error
            return self._view()

    def cancel(self, payload):
        if type(payload) is not dict or set(payload) != {"job_id"} or type(payload["job_id"]) is not str \
                or not re.fullmatch(r"[0-9a-f]{32}", payload["job_id"]):
            raise StorageError(400, "Cancel requires only the current storage job_id.")
        with self.lock:
            if self._job is None or self._job["id"] != payload["job_id"]:
                raise StorageError(409, "Stale storage job ID; no child signalled.")
            if self._job["state"] not in ACTIVE:
                raise StorageError(409, "This storage job is already finalized.")
            self._cancel.set()
            self._job["stop_reason"] = "shutdown" if self._closing else "cancel"
            self._job["state"] = "cancelling"
            if self._process is not None and self._worker is not None and not self._worker.is_alive():
                self._worker = threading.Thread(target=self._retry_finalize, name="dashboard-storage-finalizer",
                                                daemon=True)
                try:
                    self._worker.start()
                except RuntimeError as error:
                    self._job.update(state="finalization_failed", error="Finalizer could not start; ownership retained.",
                                     error_code="finalization_failed")
                    raise StorageError(503, self._job["error"]) from error
            return self._view()

    def _terminal(self, state, code=None, error=None):
        self._finished = time.monotonic()
        self._job.update(state=state, finished_at=utc(), error_code=code, error=error)

    def _progress(self, output):
        with self.lock:
            self._job.update(output.fields())

    def _selector(self, process):
        selector = selectors.DefaultSelector()
        try:
            for name in ("stdout", "stderr"):
                pipe = getattr(process, name)
                if not pipe.closed:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
        except (OSError, ValueError):
            selector.close()
            raise
        return selector

    def _drain(self, selector, output, delay):
        if selector is None:
            time.sleep(delay)
            return
        for key, _ in selector.select(delay):
            try:
                raw = os.read(key.fd, CHUNK)
            except BlockingIOError:
                continue
            if raw:
                output.accept(key.data, raw)
                self._progress(output)
            else:
                selector.unregister(key.fileobj)
                key.fileobj.close()

    @staticmethod
    def _exited(process):
        # Peek without reaping: reserve the owned PID/group until after the final group signal.
        return process.returncode is not None or \
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    @staticmethod
    def _signal(process, signum):
        if process.returncode is not None:
            return  # Once reaped, never signal a numeric PID/group which could have been reused.
        try:
            os.killpg(process.pid, signum)  # Popen(start_new_session=True) owns exactly this group.
        except ProcessLookupError:
            pass

    def _finalize(self, process, selector, output):
        self._signal(process, signal.SIGTERM)
        grace = time.monotonic() + self.terminate_grace
        while time.monotonic() < grace:
            if self._exited(process) and (selector is None or not selector.get_map()):
                break
            self._drain(selector, output, min(0.02, max(0, grace - time.monotonic())))
        self._signal(process, signal.SIGKILL)
        process.wait(timeout=self.reap_seconds)
        if selector is None:
            for pipe in (process.stdout, process.stderr):
                if not pipe.closed:
                    pipe.close()
        deadline = time.monotonic() + self.reap_seconds
        while True:
            try:
                os.killpg(process.pid, 0)
                group_present = True
            except ProcessLookupError:
                group_present = False
            if not group_present and (selector is None or not selector.get_map()):
                return
            if time.monotonic() >= deadline:
                raise ValueError("Owned process group or output pipes remain after reaping; ownership retained.")
            self._drain(selector, output, 0.02)

    def _cleanup_failed(self, error):
        with self.lock:
            # This is deliberately nonterminal. Never release the process handle or admit a new scan.
            self._job.update(state="finalization_failed", error_code="finalization_failed",
                             error=f"Owned scan finalization failed ({type(error).__name__}); ownership retained.",
                             finished_at=None, exit_code=self._process.returncode if self._process else None)

    def _run(self, fd, preset, components, root_depth):
        process = selector = None
        output = Output(os.fsencode(f"/proc/self/fd/{fd}"))
        reason = None
        run_error = None
        finalized = False
        try:
            if self._cancel.is_set():
                reason = "cancel"
            else:
                try:
                    process = subprocess.Popen(
                        [self.du_path, "--one-file-system", "--block-size=1", "--null", "--max-depth=1",
                         "--dereference-args", "--", f"/proc/self/fd/{fd}"],
                        pass_fds=(fd,), start_new_session=True, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                        env={"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"},
                    )
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    run_error = ("spawn_failed", f"GNU du could not start ({type(error).__name__}); no result.")
                if process is not None:
                    with self.lock:
                        self._process, self._output = process, output
                        if not self._cancel.is_set():
                            self._job["state"] = "running"
                    exited_at = None
                    try:
                        selector = self._selector(process)
                        while True:
                            now = time.monotonic()
                            if self._cancel.is_set():
                                reason = "shutdown" if self._closing else "cancel"
                                break
                            if now >= self._started + self.wall_seconds:
                                reason = "timeout"
                                break
                            if self._exited(process):
                                if not selector.get_map():
                                    break
                                exited_at = exited_at or now
                                if now - exited_at >= self.terminate_grace:
                                    run_error = ("child_group", "Child exited but inherited output remained open.")
                                    break
                            self._drain(selector, output, min(0.05, self._started + self.wall_seconds - now))
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        run_error = ("read_failed", f"Native storage output unavailable ({type(error).__name__}).")
                    with self.lock:
                        if reason:
                            self._job.update(state="cancelling", stop_reason=reason)
                    try:
                        self._finalize(process, selector, output)
                        finalized = True
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        self._cleanup_failed(error)
                        return
            self._result(fd, preset, components, root_depth, output, process, reason, run_error)
        finally:
            os.close(fd)
            if selector is not None:
                selector.close()
            if finalized:
                for pipe in (process.stdout, process.stderr):
                    if not pipe.closed:
                        pipe.close()
                with self.lock:
                    self._process = self._output = None

    def _result(self, fd, preset, components, root_depth, output, process, reason, run_error):
        parser = output.parser
        try:
            parser.finish()
        except ValueError as error:
            run_error = run_error or ("malformed_output", str(error))
        state = "cancelled" if reason in ("cancel", "shutdown") else "timed_out" if reason == "timeout" else None
        exit_code = process.returncode if process is not None else None
        if state is None:
            if run_error:
                state = "failed"
            elif exit_code < 0 or exit_code > 1:
                state, run_error = "failed", ("native_failed", f"GNU du exited {exit_code}; traversal failed, not complete.")
            elif parser.root_bytes is None and exit_code == 0:
                state, run_error = "failed", ("missing_root", "GNU du emitted no complete root record; total unknown.")
            else:
                state = "partial" if exit_code or output.stderr_bytes else "complete"
                if parser.root_bytes is None:
                    run_error = ("incomplete_root", f"GNU du exited {exit_code} before a root total; measured children are lower bounds.")
        valid = parser.error is None
        root_bytes = parser.root_bytes if valid else None
        if state != "complete" and root_bytes == 0:
            root_bytes = None  # du emits zero for an unreadable root; that is not an empty filesystem.
        rows = []
        if valid:
            for size, name in parser.rows:
                identifier, detail = None, None
                child_fd = None
                try:
                    if len(components) >= MAX_COMPONENTS:
                        detail = "Navigation depth limit reached."
                    else:
                        child_fd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                        info = os.fstat(child_fd)
                        if not stat.S_ISDIR(info.st_mode) or info.st_dev != components[-1][1]:
                            detail = "Not a same-filesystem no-follow directory; no drill-down."
                        else:
                            with self.lock:
                                identifier = self._target(preset, components + ((name, info.st_dev, info.st_ino),), root_depth)
                except OSError as error:
                    detail = f"Directory vanished, changed or is inaccessible ({type(error).__name__}); no drill-down."
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
                rows.append({"id": identifier, "name": display_name(name), "bytes": size,
                             "navigable": identifier is not None, "detail": detail})
        omitted = {"count": parser.child_count - len(rows), "bytes": parser.child_bytes - sum(row["bytes"] for row in rows)} \
            if valid else {"count": 0, "bytes": 0}
        with self.lock:
            # Serialize the last commit with Cancel: a post-reap publication race cannot overwrite accepted intent.
            if self._cancel.is_set():
                state = "cancelled"
                if root_bytes == 0:
                    root_bytes = None
            self._job.update(root_bytes=root_bytes, directories=rows, omitted_directories=omitted,
                             direct_bytes=root_bytes - parser.child_bytes if root_bytes is not None else None,
                             exit_code=exit_code,
                             display_truncated=omitted["count"] > 0,
                             coverage="complete" if state == "complete" else "lower_bound"
                             if state == "partial" or root_bytes is not None or rows else "unknown", **output.fields())
            self._terminal(state, *(run_error or (None, None)))

    def _retry_finalize(self):
        process, output = self._process, self._output
        selector = None
        try:
            try:
                selector = self._selector(process)
            except (OSError, ValueError):
                pass  # A failed output setup must not prevent signalling/reaping the owned child.
            self._finalize(process, selector, output)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self._cleanup_failed(error)
        else:
            with self.lock:
                self._process = self._output = None
                self._job.update(exit_code=process.returncode, **output.fields())
                self._terminal("cancelled")
        finally:
            if selector is not None:
                selector.close()

    def close(self):
        with self.lock:
            self._closing = True
            job_id = self._job["id"] if self._job and self._job["state"] in ACTIVE else None
        if job_id is not None:
            try:
                self.cancel({"job_id": job_id})
            except StorageError:
                return False
        worker = self._worker
        if worker is not None:
            worker.join(timeout=self.terminate_grace + 2 * self.reap_seconds + 0.5)
        with self.lock:
            return self._process is None and (worker is None or not worker.is_alive())
