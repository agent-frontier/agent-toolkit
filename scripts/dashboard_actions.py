"""Opt-in dashboard actions. HTTP authorization/confirmation belong to the caller.

Only fixed operations are accepted here. Inventory callbacks must make fresh
observations; wake merely requests a refresh from the parent's single sampler.
"""

from datetime import datetime, timezone
import errno
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import threading
import time
from typing import Optional, TypedDict
import uuid


HTTP_TIMEOUT = 15
SERVICE_TIMEOUT = 12
UNIT_STATE_TIMEOUT = 2
CLEANUP_UNIT = "system-janitor-catchup.service"
RESPONSE_LIMIT = 2 * 1024 * 1024
AUDIT_LIMIT = 5 * 1024 * 1024
AUDIT_BACKUPS = 8
AUDIT_NAME = "dashboard-actions.jsonl"

PROTECTED_SERVICES = frozenset({
    "agent-toolkit-dashboard.service",
    "system-janitor.service",
    "system-janitor-catchup.service",
    "janitor-maintenance.service",
    "go-with-disk-guard.service",
    "dbus.service",
    "dbus-broker.service",
    "dbus-daemon.service",
    "podman.service",
    "docker.service",
    "containerd.service",
    "systemd-exit.service",
    "systemd-logind.service",
    "systemd-user-sessions.service",
    "systemd-userdbd.service",
    f"user@{os.getuid()}.service",
    f"user-runtime-dir@{os.getuid()}.service",
    "ssh.service",
    "sshd.service",
    "ssh-agent.service",
    "gpg-agent.service",
    "gpg-agent-ssh.service",
    "gpg-agent-extra.service",
    "gpg-agent-browser.service",
})
_CRITICAL_PROCESSES = frozenset({
    "systemd", "(sd-pam)", "dbus-daemon", "dbus-broker",
    "dbus-broker-launch", "dbus-broker-lau", "sshd",
    "ssh-agent", "gpg-agent",
})
_UNIT = re.compile(r"(?:[A-Za-z0-9:_.@-]|\\x[0-9A-Fa-f]{2})+\.service")
_INVOCATION = re.compile(r"[0-9A-Fa-f]{32}")
_UNIT_PROPERTIES = (
    "Id", "LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
    "InvocationID", "CanStart", "RefuseManualStart",
)
RUNNING_STATES = frozenset({"active", "activating", "deactivating", "reloading", "refreshing"})


class UserUnitState(TypedDict):
    unit: str
    scope: str
    status: str
    id: Optional[str]
    load_state: Optional[str]
    active: Optional[str]
    sub: Optional[str]
    result: Optional[str]
    exit_status: Optional[int]
    invocation_id: Optional[str]
    can_start: Optional[bool]
    refuse_manual_start: Optional[bool]
    read_at: str
    error: Optional[str]
    detail: Optional[str]


class ActionError(Exception):
    """Public, secret-free error; unknown means an action may have completed."""

    def __init__(self, status, message, outcome="failed", *, details=None):
        super().__init__(message)
        self.status = status
        self.outcome = outcome
        self.details = details or {}


def _process(pid):
    if type(pid) is not int or pid <= 0:
        raise ActionError(400, "PID must be a positive integer.")
    try:
        directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY
                            | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            fd = os.open("stat", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                         dir_fd=directory)
            try:
                owner = os.fstat(fd).st_uid
                raw = os.read(fd, 8193).decode("utf-8", errors="replace")
            finally:
                os.close(fd)
        finally:
            os.close(directory)
        left, right = raw.index("("), raw.rindex(")")
        words = raw[right + 1:].split()
        if (len(raw) > 8192 or int(raw[:left].strip()) != pid or len(words) < 20
                or int(words[19]) <= 0 or int(words[1]) < 0):
            raise ValueError("invalid proc stat")
        return ({"pid": pid, "name": raw[left + 1:right], "uid": owner,
                 "start_time_ticks": int(words[19])}, int(words[1]))
    except (FileNotFoundError, ProcessLookupError):
        raise ActionError(404, "Process no longer exists.") from None
    except PermissionError:
        raise ActionError(403, "Process identity is not accessible.") from None
    except (OSError, ValueError):
        raise ActionError(503, "Process identity could not be verified.") from None


def process_identity(pid):
    """Read only stat and its ownership, anchored to a freshly opened proc dir."""
    return _process(pid)[0]


def protected_process(pid, name, uid):
    """Unknown ancestry is protected too: observation failure never grants access."""
    if (type(pid) is not int or pid <= 1 or type(uid) is not int or type(name) is not str
            or uid != os.getuid() or name in _CRITICAL_PROCESSES):
        return True
    current, seen = os.getpid(), set()
    for _ in range(1024):
        if current == pid or current in seen:
            return True
        if current <= 1:
            return False
        seen.add(current)
        try:
            _, current = _process(current)
        except ActionError:
            return True
    return True


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unit_gap(unit, error, detail) -> UserUnitState:
    return {
        "unit": unit, "scope": "user", "status": "unknown", "id": None,
        "load_state": None, "active": None, "sub": None, "result": None,
        "exit_status": None, "invocation_id": None, "can_start": None,
        "refuse_manual_start": None, "read_at": _now(), "error": error, "detail": detail,
    }


def user_unit_state(unit: str) -> UserUnitState:
    """Read one exact user unit in two seconds; gaps never become healthy zeroes."""
    if (type(unit) is not str or not 1 < len(unit) <= 255
            or unit.startswith(("-", ".")) or _UNIT.fullmatch(unit) is None):
        raise ValueError("An exact service unit name is required.")
    try:
        response = subprocess.run(
            ["systemctl", "--user", "show", "--no-pager",
             "--property=" + ",".join(_UNIT_PROPERTIES), "--", unit],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, shell=False,
            timeout=UNIT_STATE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return _unit_gap(unit, "timeout", "User-unit state read timed out after two seconds.")
    except UnicodeDecodeError:
        return _unit_gap(unit, "malformed", "User-unit state text decoding failed.")
    except OSError:
        return _unit_gap(unit, "read_failed", "User-unit state could not be read.")
    if type(response.returncode) is not int or response.returncode != 0:
        return _unit_gap(unit, "read_failed", "User-unit state query failed; DBus state is unknown.")
    if type(response.stdout) is not str or len(response.stdout) > RESPONSE_LIMIT:
        return _unit_gap(unit, "malformed", "User-unit state output is invalid or oversized.")
    try:
        properties = {}
        for line in response.stdout.splitlines():
            if "=" not in line:
                raise ValueError("Malformed user-unit property.")
            key, value = line.split("=", 1)
            if key not in _UNIT_PROPERTIES or key in properties:
                raise ValueError("Unexpected or duplicate user-unit property.")
            properties[key] = value
        if set(properties) != set(_UNIT_PROPERTIES):
            raise ValueError("Incomplete user-unit state.")
        load = properties["LoadState"]
        if load not in {"loaded", "not-found", "masked", "stub", "error", "bad-setting", "merged"}:
            raise ValueError("Invalid user-unit load state.")
        identifier = properties["Id"]
        if _UNIT.fullmatch(identifier) is None:
            raise ValueError("Invalid canonical user-unit ID.")
        active, sub, result = (properties[key] for key in ("ActiveState", "SubState", "Result"))
        if active not in RUNNING_STATES | {"inactive", "failed", "maintenance"}:
            raise ValueError("Invalid user-unit active state.")
        if re.fullmatch(r"[a-z][a-z0-9-]*", sub) is None:
            raise ValueError("Invalid user-unit substate.")
        if not (result == "" and load != "loaded") and re.fullmatch(r"[a-z][a-z0-9-]*", result) is None:
            raise ValueError("Invalid user-unit result.")
        raw_exit = properties["ExecMainStatus"]
        if raw_exit == "" and load != "loaded":
            exit_status = None
        elif re.fullmatch(r"[0-9]{1,3}", raw_exit) is not None and int(raw_exit) <= 255:
            exit_status = int(raw_exit)
        else:
            raise ValueError("Invalid or non-finite user-unit exit status.")
        invocation = properties["InvocationID"]
        if invocation and _INVOCATION.fullmatch(invocation) is None:
            raise ValueError("Invalid user-unit invocation ID.")
        flags = []
        for key in ("CanStart", "RefuseManualStart"):
            value = properties[key]
            if value not in {"yes", "no"}:
                raise ValueError("Invalid user-unit start policy.")
            flags.append(value == "yes")
    except ValueError as error:
        return _unit_gap(unit, "malformed", str(error))
    can_start, refuse = flags
    usable = (load == "loaded" and identifier == unit and can_start and not refuse
              and active != "maintenance")
    return {
        "unit": unit, "scope": "user", "status": "available" if usable else "unavailable",
        "id": identifier, "load_state": load, "active": active, "sub": sub,
        "result": result or None, "exit_status": exit_status,
        "invocation_id": invocation.lower() or None, "can_start": can_start,
        "refuse_manual_start": refuse, "read_at": _now(), "error": None,
        "detail": None if usable else "User unit is missing, masked, noncanonical or unusable.",
    }


def _validate(payload):
    if type(payload) is not dict:
        raise ActionError(400, "Action must be a JSON object.")
    kind = payload.get("kind")
    shapes = {
        "container": {"kind", "engine", "id", "action"},
        "process": {"kind", "pid", "start_time_ticks", "action"},
        "service": {"kind", "scope", "unit", "action"},
        "cleanup": {"kind", "action"},
    }
    if type(kind) is not str or kind not in shapes or set(payload) != shapes[kind]:
        raise ActionError(400, "Unknown action kind or unexpected action fields.")
    if type(payload["action"]) is not str:
        raise ActionError(400, "Invalid action.")
    if kind == "container":
        if (type(payload["engine"]) is not str or payload["engine"] not in {"docker", "podman"}
                or type(payload["id"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", payload["id"]) is None
                or payload["action"] not in {"stop", "kill", "remove"}):
            raise ActionError(400, "Invalid container action or full container ID.")
    elif kind == "process":
        if (any(type(payload[key]) is not int or payload[key] <= 0
                for key in ("pid", "start_time_ticks"))
                or payload["action"] not in {"terminate", "kill"}):
            raise ActionError(400, "Invalid process action or identity.")
    elif kind == "service":
        unit = payload["unit"]
        if (type(payload["scope"]) is not str or payload["scope"] != "user"
                or type(unit) is not str or not 1 < len(unit) <= 255
                or unit.startswith(("-", ".")) or _UNIT.fullmatch(unit) is None
                or payload["action"] != "stop"):
            raise ActionError(400, "Only exact user service stop actions are supported.")
    elif payload["action"] != "run":
        raise ActionError(400, "Only guarded cleanup run is supported.")
    return payload.copy()


class _DeadlineSocket(socket.socket):
    def __init__(self, deadline):
        super().__init__(socket.AF_UNIX, socket.SOCK_STREAM)
        self.deadline = deadline

    def _limit(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Local container transport deadline exceeded.")
        self.settimeout(remaining)

    def connect(self, address):
        self._limit()
        return super().connect(address)

    def sendall(self, data, flags=0):
        self._limit()
        return super().sendall(data, flags)

    def recv_into(self, buffer, nbytes=0, flags=0):
        self._limit()
        return super().recv_into(buffer, nbytes, flags)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Even a reconnect must never fall back to a TCP transport."""

    def __init__(self, path, timeout=None):
        budget = HTTP_TIMEOUT if timeout is None else timeout
        super().__init__("localhost", timeout=budget)
        self.path = path
        self.deadline = time.monotonic() + budget

    def connect(self):
        self.sock = _DeadlineSocket(self.deadline)
        self.sock.connect(str(self.path))


def unix_connection(path, timeout=2):
    """Share a total connect/write/header/body deadline with inventory readers."""
    return _UnixHTTPConnection(path, timeout)


class Controller:
    def __init__(self, state_dir: Path, container_socket, container_inventory, services, wake=None):
        self.state_dir = Path(state_dir)
        self.nonce = secrets.token_urlsafe(32)
        self._container_socket = container_socket
        self._container_inventory = container_inventory
        self._services = services
        self._wake = wake
        self._lock = threading.Lock()

    def describe(self):
        return {"enabled": True, "nonce": self.nonce, "owner_uid": os.getuid(),
                "protected_services": sorted(PROTECTED_SERVICES),
                "capabilities": ["cleanup-job-state", "guarded-cleanup"],
                "cleanup": {"kind": "cleanup", "action": "run", "unit": CLEANUP_UNIT,
                            "scope": "user", "dispatch_only": True}}

    def _open_state(self):
        directory, locked = None, False
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY
                                | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise OSError(errno.EPERM, "unsafe action state directory")
            # Also serialize independent controllers sharing this journal, without
            # locking an inode that rotation will replace.
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            return directory
        except BlockingIOError:
            raise ActionError(409, "Another action is already running.") from None
        except OSError:
            raise ActionError(500, "Private action state is unavailable or insecure.") from None
        finally:
            if directory is not None and not locked:
                os.close(directory)

    @staticmethod
    def _private_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise OSError(errno.EPERM, "unsafe action audit file")

    def _audit(self, event, directory):
        fd = None
        try:
            data = (json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
            if len(data) > AUDIT_LIMIT:
                raise OSError(errno.EFBIG, "action audit event too large")
            for number in range(1, AUDIT_BACKUPS + 1):
                try:
                    info = os.stat(f"{AUDIT_NAME}.{number}", dir_fd=directory,
                                   follow_symlinks=False)
                except FileNotFoundError:
                    continue
                self._private_file(info)
            flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            fd = os.open(AUDIT_NAME, flags | os.O_CREAT, 0o600, dir_fd=directory)
            info = os.fstat(fd)
            self._private_file(info)
            if info.st_size + len(data) > AUDIT_LIMIT:
                os.close(fd)
                fd = None
                for number in range(AUDIT_BACKUPS, 0, -1):
                    source = AUDIT_NAME if number == 1 else f"{AUDIT_NAME}.{number - 1}"
                    try:
                        os.replace(source, f"{AUDIT_NAME}.{number}",
                                   src_dir_fd=directory, dst_dir_fd=directory)
                    except FileNotFoundError:
                        if number == 1:
                            raise
                fd = os.open(AUDIT_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600,
                             dir_fd=directory)
                self._private_file(os.fstat(fd))
            if os.write(fd, data) != len(data):
                raise OSError(errno.EIO, "short action audit write")
            os.fsync(fd)
            os.fsync(directory)
        except (OSError, TypeError, ValueError):
            raise ActionError(500, "Private action audit could not be written.") from None
        finally:
            if fd is not None:
                os.close(fd)

    def execute(self, payload):
        request = _validate(payload)
        if not self._lock.acquire(blocking=False):
            raise ActionError(409, "Another action is already running.")
        directory = None
        try:
            directory = self._open_state()
            action_id = uuid.uuid4().hex
            target = {key: value for key, value in request.items() if key not in {"kind", "action"}}
            if request["kind"] == "cleanup":
                target = {"scope": "user", "unit": CLEANUP_UNIT}
            base = {"action_id": action_id, "kind": request["kind"],
                    "action": request["action"], "target": target}
            self._audit(dict(base, event="started", time=_now()), directory)
            error, message, details = None, None, {}
            try:
                message = getattr(self, f"_do_{request['kind']}")(request)
                if request["kind"] == "cleanup":
                    details = {key: value for key, value in message.items() if key != "message"}
                    message = message["message"]
            except ActionError as exc:
                error = exc
                details = exc.details
            if self._wake is not None:
                try:
                    self._wake()
                except (OSError, ValueError, RuntimeError):
                    error = ActionError(500, "Sampler refresh failed after the attempt; result unknown.",
                                        "unknown", details=details)
            completed_at = _now()
            outcome = error.outcome if error else "succeeded"
            if not error and request["kind"] == "cleanup":
                outcome = "accepted" if details["dispatch"]["dispatched"] else "observed"
            try:
                self._audit(dict(base, event=outcome, time=completed_at,
                                 status=error.status if error else 200,
                                 message=str(error) if error else message, **details), directory)
            except ActionError:
                raise ActionError(500, "Action attempted but its outcome could not be audited; "
                                  "result unknown.", "unknown", details=details) from None
            if error is not None:
                raise error
            return {"ok": True, "action_id": action_id, "kind": request["kind"],
                    "action": request["action"], "message": message, "completed_at": completed_at,
                    **details}
        finally:
            if directory is not None:
                os.close(directory)
            self._lock.release()

    @staticmethod
    def _items(value):
        if (not isinstance(value, dict) or not isinstance(value.get("items"), list)
                or any(not isinstance(item, dict) for item in value["items"])):
            raise ActionError(503, "Fresh target inventory is unavailable.")
        return value["items"]

    def _do_cleanup(self, request):
        before = user_unit_state(CLEANUP_UNIT)
        details = {
            "dispatch_only": True,
            "dispatch": {"status": "not_dispatched", "dispatched": False,
                         "unit": CLEANUP_UNIT, "scope": "user"},
            "observation": before,
        }
        if before["status"] != "available":
            status = 404 if before["load_state"] == "not-found" else 503
            raise ActionError(status, f"Guarded cleanup unit {CLEANUP_UNIT} is unavailable: "
                              f"{before['detail']}", details=details)
        if before["active"] in RUNNING_STATES:
            details["dispatch"]["status"] = "already_running"
            return dict(details, message="Guarded cleanup runner is already running or changing "
                        "state; observing it without scheduling a duplicate.")
        try:
            response = subprocess.run(
                ["systemctl", "--user", "start", "--no-block", "--", CLEANUP_UNIT],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, timeout=SERVICE_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            details["dispatch"].update(status="unknown", dispatched=True)
            details["observation"] = user_unit_state(CLEANUP_UNIT)
            raise ActionError(504, "Guarded cleanup scheduling timed out after launch; result "
                              "unknown. Refresh unit state; do not automatically retry.",
                              "unknown", details=details) from None
        except OSError:
            raise ActionError(503, "Could not launch guarded cleanup scheduling.",
                              details=details) from None
        details["dispatch"].update(status="accepted", dispatched=True)
        details["observation"] = user_unit_state(CLEANUP_UNIT)
        if response.returncode < 0:
            details["dispatch"]["status"] = "unknown"
            raise ActionError(502, f"Guarded cleanup scheduling helper was terminated by signal "
                              f"{-response.returncode}; scheduling result unknown. Refresh unit "
                              "state; do not automatically retry.", "unknown", details=details)
        if response.returncode > 0:
            details["dispatch"]["status"] = "rejected"
            raise ActionError(502, f"Guarded cleanup scheduling failed (rc={response.returncode}).",
                              details=details)
        if details["observation"]["status"] != "available":
            status = 504 if details["observation"]["error"] == "timeout" else 503
            raise ActionError(status, "Guarded cleanup scheduling was accepted, but the fresh "
                              "unit observation is unavailable; execution outcome unknown.",
                              "unknown", details=details)
        return dict(details, message="Guarded cleanup scheduling accepted (dispatch only). "
                    "This does not mean cleanup completed.")

    def _do_container(self, request):
        try:
            path = self._container_socket(request["engine"])
            if (not isinstance(path, Path) or not path.is_absolute()
                    or not stat.S_ISSOCK(path.stat().st_mode)):
                raise ActionError(503, "An existing local container Unix socket is required.")
            inventory = self._container_inventory(path)
        except (OSError, ValueError, TypeError, http.client.HTTPException,
                subprocess.SubprocessError):
            raise ActionError(503, "Fresh local container inventory is unavailable.") from None
        if isinstance(inventory, dict) and inventory.get("status", "available") != "available":
            raise ActionError(503, "Fresh local container inventory is unavailable.")
        matches = [item for item in self._items(inventory) if item.get("id") == request["id"]]
        if not matches:
            raise ActionError(404, "Full container ID is absent from the fresh inventory.")
        if len(matches) != 1 or type(matches[0].get("protected", False)) is not bool:
            raise ActionError(503, "Container protection could not be verified.")
        item = matches[0]
        if item.get("protected", False):
            raise ActionError(403, "This container is protected.")
        action, identifier = request["action"], request["id"]
        if action == "remove" and item.get("State", item.get("state")) not in {
                "created", "exited", "stopped"}:
            raise ActionError(409, "Only stopped or created containers may be removed.")
        method, suffix = {
            "stop": ("POST", "/stop?t=10"),
            "kill": ("POST", "/kill?signal=KILL"),
            "remove": ("DELETE", "?v=false&force=false"),
        }[action]
        connection, dispatched = _UnixHTTPConnection(path), False
        try:
            connection.connect()
            dispatched = True
            connection.request(method, f"/containers/{identifier}{suffix}",
                               headers={"Content-Length": "0", "Connection": "close"})
            response = connection.getresponse()
            if 400 <= response.status <= 599:
                detail = "dependency or state conflict" if response.status == 409 else "request refused"
                raise ActionError(response.status,
                                  f"Container daemon {detail} (HTTP {response.status}).")
            if response.status == 304 and action == "stop":
                return "Container already stopped."
            if response.status != 204:
                raise ActionError(502, f"Unexpected container daemon HTTP {response.status}; "
                                  "result unknown.", "unknown")
            return f"Container {action} completed."
        except TimeoutError:
            raise ActionError(504, "Container action timed out; result unknown." if dispatched
                              else "Container connection timed out before dispatch.",
                              "unknown" if dispatched else "failed") from None
        except (OSError, http.client.HTTPException):
            raise ActionError(502 if dispatched else 503,
                              "Container daemon connection failed; result unknown." if dispatched
                              else "Local container daemon is unavailable.",
                              "unknown" if dispatched else "failed") from None
        finally:
            connection.close()

    def _do_process(self, request):
        if not callable(getattr(os, "pidfd_open", None)) \
                or not callable(getattr(signal, "pidfd_send_signal", None)):
            raise ActionError(503, "Safe process signaling requires pidfd support.")
        fd = None
        try:
            # Bind the kernel target BEFORE reading /proc; PID reuse after this
            # point can only make signaling fail, never redirect it to a new PID.
            fd = os.pidfd_open(request["pid"], 0)
            identity = process_identity(request["pid"])
            if identity["start_time_ticks"] != request["start_time_ticks"]:
                raise ActionError(409, "Process identity changed; refresh before retrying.")
            if protected_process(identity["pid"], identity["name"], identity["uid"]):
                raise ActionError(403, "This process is protected or owned by another user.")
            number = signal.SIGTERM if request["action"] == "terminate" else signal.SIGKILL
            signal.pidfd_send_signal(fd, number, None, 0)
            return f"signal sent ({number.name}); the process may still be running or restart."
        except (ProcessLookupError, FileNotFoundError):
            raise ActionError(404, "Process no longer exists.") from None
        except PermissionError:
            raise ActionError(403, "Permission denied signaling this process.") from None
        except (NotImplementedError, OSError, OverflowError):
            raise ActionError(503, "Safe pidfd process signaling is unavailable.") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _do_service(self, request):
        unit = request["unit"]
        if unit in PROTECTED_SERVICES:
            raise ActionError(403, "This user service is protected.")
        try:
            items = self._items(self._services())
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            raise ActionError(503, "Fresh user service inventory is unavailable.") from None
        matches = [item for item in items if item.get("unit") == unit]
        if not matches:
            raise ActionError(404, "Service is absent from the fresh user inventory.")
        if len(matches) != 1 or matches[0].get("scope", "user") != "user":
            raise ActionError(403, "Service user scope could not be verified.")
        try:
            result = subprocess.run(["systemctl", "--user", "stop", "--", unit],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, shell=False,
                                    timeout=SERVICE_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise ActionError(504, f"Stopping user service {unit} timed out; result unknown.",
                              "unknown") from None
        except OSError:
            raise ActionError(503, f"Could not launch user service stop for {unit}.") from None
        if result.returncode:
            raise ActionError(502, f"User service {unit} stop failed (rc={result.returncode}).")
        return f"User service {unit} stop completed."
