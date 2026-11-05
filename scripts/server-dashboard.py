#!/usr/bin/env python3
"""Private Linux server dashboard with opt-in homelab controls."""

import argparse
from collections import deque
import datetime as dt
import hmac
import http.client
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


VERSION = "0.6.0"
LIMIT = 2 * 1024 * 1024
ASSETS = Path(__file__).resolve().parent.parent / "dashboard"
CAPABILITIES = sorted(["authenticated-http", "cached-snapshot", "cleanup-job-state", "container-inventory-all", "control-actions", "guarded-cleanup", "interactive-view", "lan-https", "loopback-only",
                       "read-only", "snapshot-json", "systemd-services", "trusted-lan-readonly"])
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_actions_module = None


def action_module():
    global _actions_module
    if _actions_module is None:
        spec = importlib.util.spec_from_file_location(
            "dashboard_actions", Path(__file__).resolve().with_name("dashboard_actions.py")
        )
        if spec is None or spec.loader is None:
            raise ValueError("dashboard action component is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _actions_module = module
    return _actions_module


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def xdg(variable, fallback):
    return Path(os.environ.get(variable, str(Path.home() / fallback)))


def token_path():
    return xdg("XDG_CONFIG_HOME", ".config") / "agent-toolkit/dashboard-token"


def server_settings(args):
    profile = xdg("XDG_CONFIG_HOME", ".config") / "agent-toolkit/dashboard.json"
    settings = {"bind": "127.0.0.1", "port": 8787, "interval": 5,
                "tls_cert": None, "tls_key": None, "allowed_networks": [], "trusted_lan": False, "actions": False}
    configured = read_state(profile) or {}
    if set(configured) - set(settings):
        raise ValueError("unknown dashboard profile setting")
    settings.update(configured)
    for key in ("port", "interval"):
        if getattr(args, key) is not None:
            settings[key] = getattr(args, key)
    if any(not isinstance(settings[key], int) or isinstance(settings[key], bool)
           for key in ("port", "interval")) or not (
        1024 <= settings["port"] <= 65535 and 2 <= settings["interval"] <= 60
    ):
        raise ValueError("port must be 1024..65535 and interval must be 2..60 seconds")
    if not isinstance(settings["bind"], str):
        raise ValueError("bind must be one explicit IPv4 address")
    address = ipaddress.ip_address(settings["bind"])
    if address.version != 4 or not (
        address.is_loopback or any(address in network for network in PRIVATE_NETWORKS)
    ):
        raise ValueError("bind must be loopback or one RFC1918 LAN IPv4 address, never wildcard/public")
    settings["bind"] = str(address)
    if not isinstance(settings["trusted_lan"], bool):
        raise ValueError("trusted_lan must be true or false")
    if not isinstance(settings["actions"], bool):
        raise ValueError("actions must be true or false")
    if settings["trusted_lan"] and address.is_loopback:
        raise ValueError("trusted_lan requires an explicit private LAN address and client CIDRs")
    raw_networks = settings["allowed_networks"]
    if not isinstance(raw_networks, list) or any(not isinstance(value, str) for value in raw_networks):
        raise ValueError("allowed_networks must be a list of private IPv4 CIDRs")
    networks = [ipaddress.ip_network(value) for value in raw_networks]
    if any(network.version != 4 or not any(network.subnet_of(base) for base in PRIVATE_NETWORKS)
           for network in networks):
        raise ValueError("LAN client CIDRs must stay inside RFC1918 private networks")
    if address.is_loopback:
        networks.append(ipaddress.ip_network("127.0.0.0/8"))
    elif not networks or not any(address in network for network in networks):
        raise ValueError("LAN binding requires explicit allowed_networks containing the server address")
    settings["allowed_networks"] = networks
    cert, key = settings["tls_cert"], settings["tls_key"]
    if (cert is None) != (key is None):
        raise ValueError("TLS requires both tls_cert and tls_key")
    if not address.is_loopback and not (cert and key):
        raise ValueError("LAN access requires HTTPS; configure tls_cert and tls_key")
    if cert is not None:
        if not isinstance(cert, str) or not isinstance(key, str) or not cert or not key:
            raise ValueError("TLS certificate and key must be file paths")
        for name in ("tls_cert", "tls_key"):
            path = Path(settings[name]).expanduser()
            if not path.is_absolute():
                raise ValueError("TLS file paths must be absolute or start with ~/")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("TLS files must be regular files, not symlinks")
            if name == "tls_key" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ValueError("TLS private key must be owned and private (chmod 600)")
            settings[name] = str(path)
    return settings


def tls_context(settings):
    if not settings["tls_cert"]:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(settings["tls_cert"], settings["tls_key"])
    return context


def read_token(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("dashboard token must be an owned, private regular file (chmod 600)")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("dashboard token permissions changed")
        value = source.read(102).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", value):
        raise ValueError("invalid dashboard token; restore the private token file")
    return value


def initialize_token(path):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.exists() and not path.is_symlink():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_urlsafe(32) + "\n")
            out.flush()
            os.fsync(out.fileno())
    read_token(path)


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=2, check=False)
    if result.returncode:
        raise ValueError(f"{args[0]} exited {result.returncode}: {result.stderr.strip()[:160]}")
    if len(result.stdout) > LIMIT:
        raise ValueError(f"{args[0]} output exceeds the monitoring limit")
    return result.stdout


def read_state(path):
    try:
        with path.open("rb") as source:
            raw = source.read(LIMIT + 1)
    except FileNotFoundError:
        return None
    if len(raw) > LIMIT:
        raise ValueError(f"{path.name} exceeds the monitoring limit")
    def reject_constant(value):
        raise ValueError(f"{path.name} contains a non-finite JSON number")
    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            reject_constant(raw)
        return value
    try:
        value = json.loads(raw, parse_constant=reject_constant, parse_float=finite_float)
    except RecursionError as error:
        raise ValueError(f"{path.name} is nested too deeply") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def validate_maintenance(value):
    if value is None:
        return value
    def number(item):
        return item is None or (
            isinstance(item, (int, float)) and not isinstance(item, bool)
            and (isinstance(item, int) or math.isfinite(item)) and item >= 0
        )
    for key in ("cache_bytes", "cache_budget_bytes", "observed_reclaimed_bytes", "cooldown_remaining_sec"):
        if key in value and not number(value[key]):
            raise ValueError(f"invalid maintenance field: {key}")
    for key in ("actions", "workspaces", "caches", "errors"):
        if key in value and (
            not isinstance(value[key], list) or any(not isinstance(item, dict) for item in value[key])
        ):
            raise ValueError(f"invalid maintenance list: {key}")
    for item in value.get("actions", []):
        if not number(item.get("removed_bytes")):
            raise ValueError("invalid cleanup byte count")
    admission = value.get("build_admission")
    if admission is not None and (
        not isinstance(admission, dict) or not isinstance(admission.get("waiting", []), list)
    ):
        raise ValueError("invalid build-admission observation")
    return value


def container_socket(tool):
    configured = os.environ.get("DOCKER_HOST" if tool == "docker" else "CONTAINER_HOST", "")
    if configured:
        if not configured.startswith("unix:///"):
            raise ValueError(f"{tool} monitoring supports local Unix sockets only")
        candidates = [Path(configured.removeprefix("unix://"))]
    else:
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        candidates = ([runtime / "docker.sock", Path("/var/run/docker.sock")] if tool == "docker"
                      else [runtime / "podman/podman.sock"])
    for path in candidates:
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(info.st_mode):
            raise ValueError(f"{tool} endpoint is not a Unix socket")
        return path
    return None


def container_inventory(path):
    connection = action_module().unix_connection(path, timeout=2)
    try:
        connection.request("GET", "/containers/json?all=1")
        response = connection.getresponse()
        raw = response.read(LIMIT + 1)
        if response.status != 200 or len(raw) > LIMIT:
            raise ValueError(f"container socket returned HTTP {response.status} or oversized data")
        rows = json.loads(raw)
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise ValueError("invalid container inventory")
        if any(not isinstance(item.get("Names", []), list)
               or any(not isinstance(name, str) for name in item.get("Names", []))
               for item in rows):
            raise ValueError("invalid container names")
        return {
            "status": "available", "truncated": len(rows) > 100,
            "items": [
                {"ID": str(item.get("Id", ""))[:12], "id": str(item.get("Id", "")),
                 "Names": ", ".join(str(name).lstrip("/") for name in item.get("Names", [])),
                 "Image": str(item.get("Image", "")), "Status": str(item.get("Status", "")),
                 "State": str(item.get("State", "")),
                 "protected": (item.get("Labels") or {}).get("agent-toolkit.protected") == "true"}
                for item in rows[:100]
            ],
        }
    finally:
        connection.close()


def cpu_sample(text):
    lines = text.splitlines()
    fields = lines[0].split() if lines else []
    if not fields or fields[0] != "cpu" or len(fields) < 6:
        raise ValueError("invalid aggregate CPU counters")
    ticks = [int(value) for value in fields[1:9]]
    return sum(ticks), ticks[3] + ticks[4]


def cpu_percent(previous, current):
    if previous is None:
        return None
    elapsed = current[0] - previous[0]
    idle = current[1] - previous[1]
    if elapsed <= 0 or idle < 0 or idle > elapsed:
        return None
    return round(100 * (elapsed - idle) / elapsed, 1)


class Collector:
    def __init__(self, state=None, proc=Path("/proc")):
        self.proc = proc
        self.state = state or xdg("XDG_STATE_HOME", ".local/state")
        self.previous_cpu = None
        self.previous_network = {}
        self.previous_processes = {}
        self.previous_time = None
        self.history = deque(maxlen=360)

    def system(self, now):
        counters = cpu_sample((self.proc / "stat").read_text())
        percent = cpu_percent(self.previous_cpu, counters)
        self.previous_cpu = counters
        memory = {}
        for line in (self.proc / "meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            words = value.split()
            if words:
                memory[key] = int(words[0]) * 1024
        required = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
        if any(key not in memory for key in required) or memory["MemTotal"] <= 0:
            raise ValueError("kernel memory counters unavailable")
        return {
            "cpu_percent": percent, "cpu_count": os.cpu_count(),
            "load_average": list(os.getloadavg()),
            "memory_total_bytes": memory["MemTotal"],
            "memory_available_bytes": memory["MemAvailable"],
            "memory_used_percent": round(100 * (1 - memory["MemAvailable"] / memory["MemTotal"]), 1),
            "swap_total_bytes": memory["SwapTotal"],
            "swap_used_bytes": memory["SwapTotal"] - memory["SwapFree"],
            "uptime_seconds": float((self.proc / "uptime").read_text().split()[0]),
        }

    def filesystems(self):
        mounts = [("/", "root")]
        seen = {os.stat("/").st_dev}
        for line in (self.proc / "self/mountinfo").read_text().splitlines():
            left, separator, right = line.partition(" - ")
            if not separator:
                continue
            fields = left.split()
            kind = right.split()[0]
            if kind not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "vfat", "exfat", "ntfs3"}:
                continue
            mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
            device = os.stat(mount).st_dev
            if device not in seen:
                mounts.append((mount, kind))
                seen.add(device)
        result = []
        for mount, kind in mounts[:30]:
            value = os.statvfs(mount)
            total = value.f_blocks * value.f_frsize
            used = (value.f_blocks - value.f_bfree) * value.f_frsize
            available = value.f_bavail * value.f_frsize
            result.append({
                "mount": mount, "type": kind, "total_bytes": total,
                "used_bytes": used, "available_bytes": available,
                "used_percent": round(100 * used / (used + available), 1) if used + available else None,
                "inode_used_percent": round(100 * (1 - value.f_ffree / value.f_files), 1) if value.f_files else None,
            })
        return result

    def network(self, now):
        current, result = {}, []
        elapsed = now - self.previous_time if self.previous_time is not None else None
        for line in (self.proc / "net/dev").read_text().splitlines()[2:]:
            name, _, raw = line.partition(":")
            name = name.strip()
            if name == "lo":
                continue
            fields = raw.split()
            if len(fields) < 9:
                raise ValueError("invalid network counters")
            counters = int(fields[0]), int(fields[8])
            current[name] = counters
            before = self.previous_network.get(name)
            rates = [
                round((value - old) / elapsed) if elapsed and value >= old else None
                for value, old in zip(counters, before)
            ] if before else [None, None]
            result.append({"name": name, "received_bytes_per_second": rates[0],
                           "sent_bytes_per_second": rates[1]})
        self.previous_network = current
        return result[:40]

    def processes(self, now):
        rows, current = [], {}
        unreadable = 0
        elapsed = now - self.previous_time if self.previous_time is not None else None
        for directory in self.proc.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                raw = (directory / "stat").read_text()
                left, right = raw.index("("), raw.rindex(")")
                words = raw[right + 2:].split()
                if len(words) < 22:
                    raise ValueError("invalid process counters")
                ticks = int(words[11]) + int(words[12])
                started = int(words[19])
                pid = int(directory.name)
                current[pid] = started, ticks
                before = self.previous_processes.get(pid)
                cpu = None
                if elapsed and before and started == before[0] and ticks >= before[1]:
                    cpu = round(100 * (ticks - before[1]) / os.sysconf("SC_CLK_TCK") / elapsed, 1)
                name = raw[left + 1:right][:100]
                uid = directory.stat().st_uid
                rows.append({"pid": pid, "name": name, "uid": uid, "start_time_ticks": started,
                             "controllable": not action_module().protected_process(pid, name, uid),
                             "cpu_percent": cpu,
                             "memory_bytes": max(0, int(words[21])) * os.sysconf("SC_PAGE_SIZE")})
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (PermissionError, ValueError):
                unreadable += 1
        self.previous_processes = current
        rows.sort(key=lambda row: (-(row["cpu_percent"] or 0), -row["memory_bytes"], row["pid"]))
        return {"items": rows[:15], "total_observed": len(rows), "unreadable": unreadable}

    def services(self, user=False):
        args = ["systemctl", *(["--user"] if user else []),
                "list-units", "--all", "--type=service", "--no-pager", "--plain", "--output=json"]
        rows = json.loads(command(args))
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not all(isinstance(row.get(key), str)
                                                for key in ("unit", "active", "sub", "description"))
            for row in rows
        ):
            raise ValueError("invalid systemd service inventory")
        scope = "user" if user else "system"
        result = [{key: row[key] for key in ("unit", "active", "sub", "description")} | {"scope": scope}
                  for row in rows]
        result.sort(key=lambda row: ({"failed": 0, "active": 1}.get(row["active"], 2), row["unit"]))
        return {"items": result[:250], "total": len(result), "truncated": len(result) > 250}

    def containers(self, tool):
        path = container_socket(tool)
        if path is None:
            if not shutil.which(tool):
                return {"status": "not_installed", "items": []}
            return {"status": "socket_unavailable", "items": [],
                    "detail": "Local API socket unavailable; container state is unknown."}
        return container_inventory(path)

    def maintenance(self):
        result = {}
        for name, path in (
            ("maintenance", self.state / "janitor/maintenance-last.json"),
            ("last_run", self.state / "janitor/last-run.json"),
            ("cron", self.state / "agent-toolkit/cron/system-janitor.json"),
        ):
            result[name] = read_state(path)
        result["maintenance"] = validate_maintenance(result["maintenance"])
        actions = action_module()
        result["service"] = actions.user_unit_state(actions.CLEANUP_UNIT)
        saved_id = (result["maintenance"] or {}).get("service_invocation_id")
        result["maintenance_phase_linked"] = (
            type(saved_id) is str and re.fullmatch(r"[0-9a-f]{32}", saved_id) is not None
            and result["service"]["status"] == "available"
            and saved_id == result["service"]["invocation_id"]
        )
        try:
            result["timer"] = dict(
                line.split("=", 1) for line in command([
                    "systemctl", "--user", "show", "system-janitor-catchup.timer",
                    "-p", "ActiveState", "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec",
                ]).splitlines() if "=" in line
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result["timer"] = None
            result["timer_error"] = str(error)[:240]
        return result

    def collect(self):
        now = time.monotonic()
        errors = []

        def observe(name, action):
            try:
                return action()
            except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as error:
                errors.append({"source": name, "detail": str(error)[:240]})
                return None

        snapshot = {
            "version": 1, "generated_at": utc(), "host": socket.gethostname(),
            "system": observe("system", lambda: self.system(now)),
            "filesystems": observe("filesystems", self.filesystems),
            "network": observe("network", lambda: self.network(now)),
            "processes": observe("processes", lambda: self.processes(now)),
            "system_services": observe("system_services", self.services),
            "user_services": observe("user_services", lambda: self.services(user=True)),
            "docker": observe("docker", lambda: self.containers("docker")),
            "podman": observe("podman", lambda: self.containers("podman")),
            "janitor": observe("janitor", self.maintenance),
            "errors": errors,
        }
        janitor = snapshot["janitor"] or {}
        if (janitor.get("service") or {}).get("status") == "unknown":
            errors.append({"source": "janitor_service", "detail": janitor["service"]["detail"]})
        if janitor.get("timer_error"):
            errors.append({"source": "janitor_timer", "detail": janitor["timer_error"]})
        self.previous_time = now
        snapshot["status"] = "degraded" if errors else "healthy"
        system = snapshot["system"] or {}
        disks = snapshot["filesystems"] or []
        self.history.append({
            "ts": snapshot["generated_at"], "cpu": system.get("cpu_percent"),
            "memory": system.get("memory_used_percent"),
            "disk": disks[0]["used_percent"] if disks else None,
        })
        snapshot["history"] = list(self.history)
        return snapshot


class Snapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.body = None

    def publish(self, value):
        body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        with self.lock:
            self.body = body

    def read(self):
        with self.lock:
            return self.body


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, context=None, networks=None):
        self.slots = threading.BoundedSemaphore(12)
        self.tls = context
        self.allowed_networks = networks if networks is not None else [ipaddress.ip_network("127.0.0.0/8")]
        super().__init__(address, handler)

    def verify_request(self, request, client_address):
        peer = ipaddress.ip_address(client_address[0])
        return any(peer in network for network in self.allowed_networks)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            if self.tls is not None:
                try:
                    request = self.tls.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
                    request.do_handshake()
                except OSError as error:
                    logging.warning("TLS connection rejected: %s", type(error).__name__)
                    self.shutdown_request(request)
                    return
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def handler_for(snapshot, token, controls=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-toolkit-dashboard"

        def log_message(self, fmt, *args):
            logging.info("HTTP %s %s", self.command, self.path.split("?", 1)[0])

        def respond(self, code, body, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if code == 405:
                self.send_header("Allow", "GET, HEAD")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self' data:; "
                             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def origin_allowed(self, required=False):
            bind = self.server.server_address[0]
            hosts = {f"{bind}:{self.server.server_port}"}
            if ipaddress.ip_address(bind).is_loopback:
                hosts.add(f"localhost:{self.server.server_port}")
            scheme = "https" if self.server.tls else "http"
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return not (host not in hosts or self.headers.get("Sec-Fetch-Site") == "cross-site" or (
                origin is not None and origin != f"{scheme}://{host}"
            ) or (required and origin is None))

        def authorized(self):
            authorization = self.headers.get("Authorization", "")
            return token is None or hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode())

        def do_GET(self):
            if not self.origin_allowed():
                return self.respond(403, b'{"error":"configured same-origin requests only"}')
            route = urlsplit(self.path)
            if route.query:
                return self.respond(404, b'{"error":"not found"}')
            if route.path == "/api/snapshot":
                if not self.authorized():
                    return self.respond(401, b'{"error":"access token required"}')
                body = snapshot.read()
                return self.respond(200 if body else 503, body or b'{"status":"warming_up"}')
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if route.path not in assets:
                return self.respond(404, b'{"error":"not found"}')
            filename, kind = assets[route.path]
            return self.respond(200, (ASSETS / filename).read_bytes(), kind)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            route = urlsplit(self.path)
            if route.path != "/api/action" or route.query:
                return self.respond(405, b'{"error":"unsupported action route"}')
            if not self.origin_allowed(required=True):
                return self.respond(403, b'{"error":"same-origin action required"}')
            if not self.authorized():
                return self.respond(401, b'{"error":"access token required"}')
            if controls is None:
                return self.respond(403, b'{"error":"homelab controls are disabled"}')
            nonce = self.headers.get("X-Action-Token", "")
            if not hmac.compare_digest(nonce.encode(), controls.nonce.encode()):
                return self.respond(403, b'{"error":"control session expired; refresh the page"}')
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self.respond(415, b'{"error":"JSON action body required"}')
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                return self.respond(400, b'{"error":"one explicit body length required"}')
            try:
                length = int(lengths[0])
            except ValueError:
                return self.respond(400, b'{"error":"invalid body length"}')
            if not 0 < length <= 4096:
                return self.respond(413, b'{"error":"action body must be1..4096bytes"}')
            try:
                raw = self.rfile.read(length)
            except OSError:
                return self.respond(408, b'{"error":"action body timed out"}')
            if len(raw) != length:
                return self.respond(400, b'{"error":"incomplete action body"}')
            try:
                payload = json.loads(raw)
            except (ValueError, RecursionError):
                return self.respond(400, b'{"error":"invalid JSON action"}')
            try:
                self.connection.settimeout(20)
                result = controls.execute(payload)
            except action_module().ActionError as error:
                return self.respond(error.status, json.dumps(
                    {"ok": False, "error": str(error), "outcome": error.outcome, **error.details},
                    ensure_ascii=True, allow_nan=False,
                ).encode())
            self.respond(200, json.dumps(result, ensure_ascii=True, allow_nan=False).encode())

        def unsupported(self):
            self.respond(405, b'{"error":"use the fixed POST action endpoint"}')

        do_PUT = unsupported
        do_PATCH = unsupported
        do_DELETE = unsupported
        do_OPTIONS = unsupported

    return Handler


def serve(settings, token, context):
    for asset in ("index.html", "app.js", "style.css"):
        if not (ASSETS / asset).is_file():
            raise ValueError(f"dashboard asset missing: {asset}")
    cache, stop, wake = Snapshot(), threading.Event(), threading.Event()
    collector = Collector()
    interval = settings["interval"]
    controls = action_module().Controller(
        xdg("XDG_STATE_HOME", ".local/state") / "agent-toolkit",
        container_socket, container_inventory, lambda: collector.services(user=True), wake.set,
    ) if settings["actions"] else None

    def sample():
        while not stop.is_set():
            started = time.monotonic()
            value = collector.collect()
            value.update(interval_seconds=interval, stale_after_seconds=max(20, interval * 3))
            value["actions"] = controls.describe() if controls else {"enabled": False}
            cache.publish(value)
            wake.wait(max(0.1, interval - (time.monotonic() - started)))
            wake.clear()

    server = DashboardServer((settings["bind"], settings["port"]), handler_for(cache, token, controls),
                             context, settings["allowed_networks"])
    worker = threading.Thread(target=sample, name="dashboard-sampler", daemon=True)
    worker.start()
    scheme = "https" if context else "http"
    logging.info("Dashboard listening on %s://%s:%s (controls=%s)", scheme, settings["bind"], settings["port"], bool(controls))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        wake.set()
        server.server_close()
        worker.join(timeout=12)


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main():
    parser = Arguments(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--init", action="store_true", help="create a private access token")
    actions.add_argument("--url", action="store_true", help="print the private browser link; do not share it")
    actions.add_argument("--snapshot", action="store_true", help="print one read-only JSON observation")
    actions.add_argument("--version", action="store_true")
    parser.add_argument("--json", action="store_true", help="machine output with --version or --snapshot")
    parser.add_argument("--port", type=int, help="override profile port (default 8787)")
    parser.add_argument("--interval", type=int, help="override nominal sampling interval, 2..60 (default 5)")
    args = parser.parse_args()
    if (args.port is not None and not 1024 <= args.port <= 65535) or (
        args.interval is not None and not 2 <= args.interval <= 60
    ):
        raise ValueError("port must be 1024..65535 and interval must be 2..60 seconds")
    if args.json and not (args.version or args.snapshot):
        raise ValueError("--json requires --version or --snapshot")
    if args.version:
        value = {"name": "agent-toolkit-dashboard", "version": VERSION, "capabilities": CAPABILITIES}
        print(json.dumps(value) if args.json else f"{value['name']} {VERSION}")
    elif args.init:
        initialize_token(token_path())
        print(json.dumps({"initialized": True, "token_path": str(token_path())}))
    elif args.snapshot:
        value = Collector().collect()
        print(json.dumps(value, ensure_ascii=True, allow_nan=False))
        return 4 if value["status"] == "degraded" else 0
    else:
        settings = server_settings(args)
        context = tls_context(settings)
        token = None
        if not settings["trusted_lan"]:
            try:
                token = read_token(token_path())
            except FileNotFoundError as error:
                raise ValueError("run --init once to create the private dashboard token") from error
        if args.url:
            scheme = "https" if context else "http"
            fragment = f"#token={token}" if token is not None else ""
            print(f"{scheme}://{settings['bind']}:{settings['port']}/{fragment}")
        else:
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            serve(settings, token, context)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"[dashboard] {error}", file=sys.stderr)
        sys.exit(3)
