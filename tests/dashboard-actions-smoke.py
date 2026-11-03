#!/usr/bin/env python3
"""Disposable-child, fake Unix daemon and mocked user-service action checks."""

import errno
from http.server import BaseHTTPRequestHandler
import importlib.util
import json
import os
from pathlib import Path
import signal
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("dashboard_actions", ROOT / "scripts/dashboard_actions.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)
IDENTIFIER = "a" * 64
UNIT = "smith-worker@demo.service"


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = mock.Mock(return_value=None)
        self.inventory = mock.Mock(return_value={
            "status": "available", "items": [{"id": IDENTIFIER, "State": "running"}],
        })
        self.services = mock.Mock(return_value={"items": [{"unit": UNIT, "scope": "user"}]})
        self.wake = mock.Mock()
        self.controller = self.controller_at(self.root / "state")
        self.container = {"kind": "container", "engine": "podman", "id": IDENTIFIER, "action": "stop"}
        self.service = {"kind": "service", "scope": "user", "unit": UNIT, "action": "stop"}

    def controller_at(self, path):
        return d.Controller(path, self.socket, self.inventory, self.services, self.wake)

    def error(self, payload, status, outcome="failed", controller=None):
        with self.assertRaises(d.ActionError) as caught:
            (controller or self.controller).execute(payload)
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(caught.exception.outcome, outcome)
        return caught.exception

    def events(self, controller=None):
        path = (controller or self.controller).state_dir / d.AUDIT_NAME
        return [json.loads(line) for line in path.read_text().splitlines()]

    def daemon(self):
        records, reply = [], {"status": 204, "body": b"", "delay": 0}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                records.append((self.command, self.path,
                                self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                response = reply.copy()
                time.sleep(response["delay"])
                try:
                    if response.get("trickle_headers"):
                        for byte in b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n":
                            self.connection.sendall(bytes([byte]))
                            time.sleep(response["trickle_headers"])
                        self.close_connection = True
                        return
                    self.send_response(response["status"])
                    self.send_header("Content-Length", str(len(response["body"])))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if response.get("trickle"):
                        for byte in response["body"]:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(response["trickle"])
                    else:
                        self.wfile.write(response["body"])
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The timeout/response-limit tests intentionally close the peer.

            do_DELETE = do_POST

            def log_message(self, *args):
                pass

        path = self.root / "daemon.sock"
        server = socketserver.UnixStreamServer(str(path), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        self.socket.return_value = path
        return path, records, reply

    def child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

        def cleanup():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()  # Only this fixture's disposable child.
            child.wait(timeout=3)

        self.addCleanup(cleanup)
        return child

    def test_exact_payload_shapes_and_manifest(self):
        # Malformed JSON must never reach an observer, subprocess or signaling.
        me = d.process_identity(os.getpid())
        process = {"kind": "process", "pid": me["pid"],
                   "start_time_ticks": me["start_time_ticks"], "action": "terminate"}
        invalid = [
            None, [], {}, {"kind": []}, dict(self.container, extra=True),
            {key: value for key, value in self.container.items() if key != "action"},
            dict(self.container, engine="tcp"), dict(self.container, engine=[]),
            dict(self.container, id=IDENTIFIER[:12]), dict(self.container, id="A" * 64),
            dict(self.container, id=True), dict(self.container, action="prune"),
            dict(self.container, action=[]), dict(process, pid=True),
            dict(process, pid=-1), dict(process, pid=1.5),
            dict(process, start_time_ticks=True), dict(process, start_time_ticks=0),
            dict(process, action="stop"), dict(self.service, scope="system"),
            dict(self.service, scope=True), dict(self.service, unit="../a.service"),
            dict(self.service, unit="--all.service"), dict(self.service, unit="a\n.service"),
            dict(self.service, unit="worker.timer"), dict(self.service, unit=[]),
            dict(self.service, action="restart"), dict(self.service, nonce="not accepted here"),
        ]
        with mock.patch.object(d.subprocess, "run") as run:
            for payload in invalid:
                with self.subTest(payload=payload):
                    self.error(payload, 400)
            run.assert_not_called()
        self.socket.assert_not_called()
        self.services.assert_not_called()
        self.wake.assert_not_called()
        self.assertFalse(self.controller.state_dir.exists())
        manifest = self.controller.describe()
        self.assertEqual(manifest, {"enabled": True, "nonce": self.controller.nonce,
                                   "owner_uid": os.getuid(),
                                   "protected_services": sorted(d.PROTECTED_SERVICES)})
        self.assertGreaterEqual(len(manifest["nonce"]), 43)
        self.assertNotEqual(manifest["nonce"], self.controller_at(self.root / "other").nonce)
        self.assertNotIn(UNIT, d.PROTECTED_SERVICES)

    def test_container_exact_requests_and_volume_retention(self):
        # Exercise real Unix HTTP, full IDs, both engines and no volume/force flags.
        path, records, reply = self.daemon()
        cases = [
            ("docker", "stop", "running", "POST", "/stop?t=10"),
            ("podman", "kill", "running", "POST", "/kill?signal=KILL"),
            ("podman", "remove", "exited", "DELETE", "?v=false&force=false"),
            ("docker", "remove", "created", "DELETE", "?v=false&force=false"),
        ]
        for engine, action, state, method, suffix in cases:
            with self.subTest(engine=engine, action=action, state=state):
                self.inventory.return_value["items"][0]["State"] = state
                result = self.controller.execute(dict(self.container, engine=engine, action=action))
                self.assertEqual(records[-1], (method, f"/containers/{IDENTIFIER}{suffix}", b""))
                self.assertTrue(result["ok"])
                self.assertEqual((result["kind"], result["action"]), ("container", action))
                self.assertRegex(result["completed_at"], r"Z$")
                json.dumps(result, allow_nan=False)
                self.inventory.assert_called_with(path)
                self.socket.assert_called_with(engine)
        self.assertEqual(self.socket.call_count, len(cases))
        self.assertEqual(self.inventory.call_count, len(cases))
        self.assertEqual(self.wake.call_count, len(cases))
        reply["status"] = 304
        self.assertIn("already stopped", self.controller.execute(self.container)["message"])

    def test_container_guards_never_dispatch(self):
        # A stale prefix, protected target or running/unknown remove is not a request.
        _, records, _ = self.daemon()
        for items, action, status in [
            ([{"ID": IDENTIFIER[:12], "State": "exited"}], "stop", 404),
            ([{"id": "b" * 64}], "kill", 404),
            ([{"id": IDENTIFIER, "protected": True}], "stop", 403),
            ([{"id": IDENTIFIER, "protected": "unknown"}], "kill", 503),
            ([{"id": IDENTIFIER, "State": "running"}], "remove", 409),
            ([{"id": IDENTIFIER, "State": "paused"}], "remove", 409),
            ([{"id": IDENTIFIER}], "remove", 409),
            ([{"id": IDENTIFIER}, {"id": IDENTIFIER}], "stop", 503),
        ]:
            with self.subTest(items=items, action=action):
                self.inventory.return_value = {"items": items}
                self.error(dict(self.container, action=action), status)
        for endpoint in (None, "tcp://localhost:2375", self.root / "missing"):
            self.socket.return_value = endpoint
            self.error(self.container, 503)
        regular = self.root / "not-a-socket"
        regular.touch()
        self.socket.return_value = regular
        self.error(self.container, 503)
        self.assertEqual(records, [])

    def test_daemon_errors_limits_and_unknown_timeout(self):
        # Distinguish explicit daemon rejection from an unobserved dispatched result.
        _, records, reply = self.daemon()
        for code in (409, 404, 500):
            reply.update(status=code, body=b'{"message":"secret-daemon-detail"}')
            error = self.error(self.container, code)
            self.assertIn(str(code), str(error))
            self.assertNotIn("secret-daemon-detail", str(error))
        reply.update(status=200, body=b"x" * (d.RESPONSE_LIMIT + 10))
        self.error(self.container, 502, "unknown")
        reply.update(status=204, body=b"", delay=0.1)
        with mock.patch.object(d, "HTTP_TIMEOUT", 0.03):
            self.error(self.container, 504, "unknown")
        self.assertEqual(len(records), 5)
        self.assertEqual(self.events()[-1]["event"], "unknown")
        self.assertEqual(self.wake.call_count, 5)
        self.assertNotIn("secret-daemon-detail", json.dumps(self.events()))

    def test_dbus_launcher_names_are_rejected_without_signaling(self):
        # Linux truncates the core broker launcher comm to15characters.
        child = self.child()
        identity = d.process_identity(child.pid)
        request = {"kind": "process", "pid": child.pid,
                   "start_time_ticks": identity["start_time_ticks"], "action": "kill"}
        for name in ("dbus-broker-launch", "dbus-broker-lau"):
            with self.subTest(name=name), \
                    mock.patch.object(d, "process_identity", return_value=dict(identity, name=name)), \
                    mock.patch.object(d.signal, "pidfd_send_signal") as send:
                self.error(request, 403)
                send.assert_not_called()
        self.assertIsNone(child.poll())

    def test_process_stale_identity_and_protection_without_signals(self):
        # Keep real proc reads and real pidfds: a mismatch must prove no signal.
        child = self.child()
        identity = d.process_identity(child.pid)
        request = {"kind": "process", "pid": child.pid,
                   "start_time_ticks": identity["start_time_ticks"] + 1, "action": "kill"}
        opened, real_open, real_identity = [], d.os.pidfd_open, d.process_identity

        def acquire(pid, flags):
            fd = real_open(pid, flags)
            opened.append(fd)
            return fd

        def observe(pid):
            self.assertTrue(opened)
            os.fstat(opened[-1])  # Acquisition must precede the real identity read.
            return real_identity(pid)

        with mock.patch.object(d.os, "pidfd_open", side_effect=acquire), \
                mock.patch.object(d, "process_identity", side_effect=observe), \
                mock.patch.object(d.signal, "pidfd_send_signal", wraps=signal.pidfd_send_signal) as send:
            self.error(request, 409)
            send.assert_not_called()
        self.assertIsNone(child.poll())
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        own = d.process_identity(os.getpid())
        with mock.patch.object(d.signal, "pidfd_send_signal") as send:
            self.error(dict(request, pid=own["pid"],
                            start_time_ticks=own["start_time_ticks"]), 403)
            send.assert_not_called()
        for pid, name, uid in [
            (0, "worker", os.getuid()), (1, "worker", os.getuid()),
            (child.pid, "worker", os.getuid() + 1),
            (os.getppid(), "worker", os.getuid()),
            *((child.pid, name, os.getuid()) for name in d._CRITICAL_PROCESSES),
        ]:
            self.assertTrue(d.protected_process(pid, name, uid))
        self.assertFalse(d.protected_process(child.pid, identity["name"], identity["uid"]))

    def test_real_child_pidfd_signals_and_explicit_unavailability(self):
        # Real TERM/KILL go only to fixture children; unsupported pidfd never falls back.
        for action, number in (("terminate", signal.SIGTERM), ("kill", signal.SIGKILL)):
            with self.subTest(action=action):
                child = self.child()
                identity = d.process_identity(child.pid)
                request = {"kind": "process", "pid": child.pid,
                           "start_time_ticks": identity["start_time_ticks"], "action": action}
                with mock.patch.object(d.os, "pidfd_open", None):
                    self.error(request, 503)
                with mock.patch.object(d.signal, "pidfd_send_signal", None):
                    self.error(request, 503)
                with mock.patch.object(d.os, "pidfd_open", side_effect=PermissionError):
                    self.error(request, 403)
                with mock.patch.object(d.os, "pidfd_open", side_effect=ProcessLookupError):
                    self.error(request, 404)
                self.assertIsNone(child.poll())
                result = self.controller.execute(request)
                self.assertIn("signal sent", result["message"])
                self.assertNotIn("exited", result["message"])
                self.assertEqual(child.wait(timeout=3), -number)

    def test_user_service_fixed_command_fresh_guards_and_safe_errors(self):
        # Ordinary smith workers remain stoppable; no arbitrary command stderr escapes.
        with mock.patch.object(d.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0)) as run:
            self.controller.execute(self.service)
            run.assert_called_once_with(
                ["systemctl", "--user", "stop", "--", UNIT], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                timeout=12, check=False,
            )
            for unit in d.PROTECTED_SERVICES:
                self.error(dict(self.service, unit=unit), 403)
            self.services.return_value = {"items": []}
            self.error(self.service, 404)
            self.services.return_value = {"items": [{"unit": UNIT, "scope": "system"}]}
            self.error(self.service, 403)
            self.assertEqual(run.call_count, 1)
            self.services.return_value = {"items": [{"unit": UNIT, "scope": "user"}]}
            run.return_value = SimpleNamespace(returncode=7, stderr="secret-service-detail")
            error = self.error(self.service, 502)
            self.assertIn("rc=7", str(error))
            self.assertIn(UNIT, str(error))
            self.assertNotIn("secret-service-detail", str(error))
            run.side_effect = subprocess.TimeoutExpired("systemctl", 12, stderr="secret-timeout")
            self.error(self.service, 504, "unknown")
        self.assertEqual(self.events()[-1]["event"], "unknown")
        self.assertNotIn("secret-", json.dumps(self.events()))

    def test_container_transport_has_total_deadline_not_inactivity_timeout(self):
        # A progressing daemon must not hold the global action lock indefinitely.
        _, records, reply = self.daemon()
        for mode, status in (("body", 502), ("headers", 504)):
            with self.subTest(mode=mode), mock.patch.object(d, "HTTP_TIMEOUT", 0.05):
                time.sleep(0.08)  # Let the previous intentionally closed peer finish.
                reply.update(status=200 if mode == "body" else 204,
                             body=b"x" * 16 if mode == "body" else b"",
                             trickle=0.025 if mode == "body" else 0,
                             trickle_headers=0.025 if mode == "headers" else 0)
                started = time.monotonic()
                self.error(self.container, status, "unknown")
                self.assertLess(time.monotonic() - started, 0.25)
                with mock.patch.object(d.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                    self.assertTrue(self.controller.execute(self.service)["ok"])
        self.assertEqual(len(records), 2)

    def test_audit_is_private_and_started_precedes_dispatch(self):
        # A durable, sanitized intent must exist before the first side effect.
        def dispatch(args, **kwargs):
            events = self.events()
            self.assertEqual(events[-1]["event"], "started")
            self.assertEqual(events[-1]["target"], {"scope": "user", "unit": UNIT})
            path = self.controller.state_dir / d.AUDIT_NAME
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
            return SimpleNamespace(returncode=0)

        with mock.patch.object(d.subprocess, "run", side_effect=dispatch):
            result = self.controller.execute(self.service)
        events = self.events()
        self.assertEqual([event["event"] for event in events], ["started", "succeeded"])
        self.assertTrue(all(event["action_id"] == result["action_id"] for event in events))
        self.assertNotIn(self.controller.nonce, json.dumps(events))
        self.assertNotIn("nonce", json.dumps(events))
        self.wake.assert_called_once_with()

    def test_unsafe_audit_and_initial_write_failure_prevent_actions(self):
        # Never chmod/reuse hostile state, or act without a writable private intent.
        sentinel = self.root / "sentinel"
        sentinel.write_text("unchanged\n")
        real_fstat = os.fstat

        def foreign_file_owner(fd):
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1,
                                       st_nlink=info.st_nlink, st_size=info.st_size)
            return info

        with mock.patch.object(d.subprocess, "run") as run:
            for case in ("symlink", "mode", "hardlink", "owner", "backup", "directory"):
                with self.subTest(case=case):
                    state = self.root / case
                    state.mkdir(mode=0o700)
                    path = state / d.AUDIT_NAME
                    if case == "symlink":
                        path.symlink_to(sentinel)
                    elif case == "hardlink":
                        os.link(sentinel, path)
                    else:
                        path.touch(mode=0o600)
                    if case == "mode":
                        path.chmod(0o644)
                    if case == "backup":
                        (state / f"{d.AUDIT_NAME}.1").symlink_to(sentinel)
                    if case == "directory":
                        state.chmod(0o777)
                    controller = self.controller_at(state)
                    if case == "owner":
                        with mock.patch.object(d.os, "fstat", side_effect=foreign_file_owner):
                            self.error(self.service, 500, controller=controller)
                    else:
                        self.error(self.service, 500, controller=controller)
            with mock.patch.object(d.os, "write", side_effect=OSError(errno.ENOSPC, "full")):
                self.error(self.service, 500)
            run.assert_not_called()
        self.assertEqual(sentinel.read_text(), "unchanged\n")
        self.wake.assert_not_called()

    def test_post_operation_audit_or_refresh_failure_is_unknown(self):
        # Do not fake success after an irreversible effect loses its outcome record.
        writes, real_write = [], os.write

        def fail_outcome(fd, data):
            writes.append(data)
            if len(writes) == 2:
                raise OSError(errno.ENOSPC, "full")
            return real_write(fd, data)

        with mock.patch.object(d.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0)) as run, \
                mock.patch.object(d.os, "write", side_effect=fail_outcome):
            self.error(self.service, 500, "unknown")
            run.assert_called_once()
        self.assertEqual([event["event"] for event in self.events()], ["started"])
        self.wake.assert_called_once_with()
        self.wake.side_effect = RuntimeError("secret-refresh-detail")
        with mock.patch.object(d.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            error = self.error(self.service, 500, "unknown")
        self.assertNotIn("secret-refresh-detail", str(error))
        self.assertEqual(self.events()[-1]["event"], "unknown")

    def test_nonblocking_serialization_and_bounded_rotation(self):
        # Reentrancy and a second controller cannot race dispatch or journal rotation.
        other = self.controller_at(self.controller.state_dir)

        def dispatch(args, **kwargs):
            self.error(self.service, 409)
            self.error(self.service, 409, controller=other)
            return SimpleNamespace(returncode=0)

        with mock.patch.object(d.subprocess, "run", side_effect=dispatch) as run:
            self.controller.execute(self.service)
            run.assert_called_once()
        self.assertEqual(len(self.events()), 2)
        self.wake.assert_called_once_with()
        with mock.patch.object(d, "AUDIT_LIMIT", 1024), \
                mock.patch.object(d.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            for _ in range(25):
                self.controller.execute(self.service)
        paths = list(self.controller.state_dir.iterdir())
        self.assertEqual({path.name for path in paths},
                         {d.AUDIT_NAME, *(f"{d.AUDIT_NAME}.{n}" for n in range(1, 9))})
        for path in paths:
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            for line in path.read_text().splitlines():
                self.assertIn(json.loads(line)["event"], {"started", "succeeded"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
