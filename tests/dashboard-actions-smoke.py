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
INVOCATION = "b" * 32


def unit_response(**changes):
    properties = {
        "Id": d.CLEANUP_UNIT, "LoadState": "loaded", "ActiveState": "inactive",
        "SubState": "dead", "Result": "success", "ExecMainStatus": "0",
        "InvocationID": "", "CanStart": "yes", "RefuseManualStart": "no",
    }
    properties.update(changes)
    return SimpleNamespace(returncode=0, stdout="".join(
        f"{key}={value}\n" for key, value in properties.items()), stderr="")


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
        self.cleanup = {"kind": "cleanup", "action": "run"}

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
        records, reply = [], {"status": 204, "body": b"", "delay": 0,
                              "inspect": {"status": 200, "state": "exited", "delay": 0}}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                records.append((self.command, self.path,
                                self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                response = reply.copy()
                if reply.get("after_mutation"):
                    reply["after_mutation"]()
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

            def do_GET(self):
                records.append((self.command, self.path, b""))
                response = reply["inspect"].copy()
                payload = response.get("body", json.dumps({
                    "Id": IDENTIFIER, "State": {"Status": response["state"]},
                    "Config": {"Labels": {}, "Env": ["secret-environment"], "Cmd": ["secret-args"]},
                }).encode())
                time.sleep(response["delay"])
                try:
                    if response.get("trickle_headers"):
                        for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: close\r\n\r\n":
                            self.connection.sendall(bytes([byte]))
                            time.sleep(response["trickle_headers"])
                        return
                    self.send_response(response["status"])
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if response.get("trickle"):
                        for byte in payload:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(response["trickle"])
                    else:
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Exact-read deadline tests intentionally close this fake peer.

            def log_message(self, *args):
                pass

        path = self.root / "daemon.sock"
        # Inspect has its own deadline: a deliberately trickling mutation handler
        # must not serialize the read behind its discarded response body.
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
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
            dict(self.service, action="unmask"), dict(self.service, nonce="not accepted here"),
            {"kind": "cleanup"}, dict(self.cleanup, action="force"),
            dict(self.cleanup, action=[]), dict(self.cleanup, unit=d.CLEANUP_UNIT),
            dict(self.cleanup, path="/tmp"), dict(self.cleanup, mode="run"),
            dict(self.cleanup, scope="user"), dict(self.cleanup, nonce="not accepted here"),
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
                                   "protected_services": sorted(d.PROTECTED_SERVICES),
                                   "capabilities": ["action-history", "cleanup-job-state", "guarded-cleanup",
                                                    "observed-action-state", "workload-recovery"],
                                   "container_actions": ["kill", "remove", "restart", "start", "stop"],
                                   "service_actions": ["restart", "start", "stop"],
                                   "cleanup": {"kind": "cleanup", "action": "run",
                                               "unit": "system-janitor-catchup.service",
                                               "scope": "user", "dispatch_only": True}})
        self.assertGreaterEqual(len(manifest["nonce"]), 43)
        self.assertNotEqual(manifest["nonce"], self.controller_at(self.root / "other").nonce)
        self.assertNotIn(UNIT, d.PROTECTED_SERVICES)

    def test_user_unit_state_is_strict_typed_bounded_and_canonical(self):
        # A reusable exact-target read must not turn malformed DBus values into zeroes.
        with mock.patch.object(d.subprocess, "run", return_value=unit_response(
                Id=UNIT, ExecMainStatus="7", InvocationID=INVOCATION.upper())) as run:
            state = d.user_unit_state(UNIT)
        run.assert_called_once_with(
            ["systemctl", "--user", "show", "--no-pager",
             "--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,InvocationID,CanStart,RefuseManualStart",
             "--", UNIT], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            shell=False, timeout=2, check=False,
        )
        self.assertEqual(state["status"], "available")
        self.assertEqual((state["unit"], state["id"], state["scope"]), (UNIT, UNIT, "user"))
        self.assertEqual((state["load_state"], state["active"], state["sub"]),
                         ("loaded", "inactive", "dead"))
        self.assertEqual((state["result"], state["exit_status"]), ("success", 7))
        self.assertIs(type(state["exit_status"]), int)
        self.assertEqual(state["invocation_id"], INVOCATION)
        self.assertTrue(state["can_start"])
        self.assertFalse(state["refuse_manual_start"])
        self.assertRegex(state["read_at"], r"Z$")
        json.dumps(state, allow_nan=False)
        for changes in (
            {"Id": "../wrong.service"}, {"LoadState": "unknown"}, {"ActiveState": ""},
            {"ActiveState": "mystery"}, {"SubState": "running\nbroken"}, {"Result": ""},
            {"Result": "bad result"}, {"CanStart": "true"}, {"RefuseManualStart": ""},
            *({"ExecMainStatus": value} for value in ("NaN", "inf", "1e400", "-1", "256", "", "1.5")),
            *({"InvocationID": value} for value in ("bad", "b" * 31, "b" * 33, INVOCATION + "\n")),
        ):
            with self.subTest(changes=changes), mock.patch.object(
                    d.subprocess, "run", return_value=unit_response(**changes)):
                state = d.user_unit_state(d.CLEANUP_UNIT)
                self.assertEqual(state["status"], "unknown")
                self.assertEqual(state["error"], "malformed")
                self.assertIsNone(state["exit_status"])
                self.assertIsNone(state["invocation_id"])
                self.assertTrue(state["detail"])
        with mock.patch.object(d.subprocess, "run", return_value=unit_response(Id=UNIT)):
            state = d.user_unit_state(d.CLEANUP_UNIT)
        self.assertEqual(state["id"], UNIT)
        self.assertEqual(state["status"], "unavailable")
        with mock.patch.object(d.subprocess, "run") as run:
            for unit in ("../a.service", "--all.service", "worker.timer", [], None):
                with self.subTest(unit=unit), self.assertRaises(ValueError):
                    d.user_unit_state(unit)
            run.assert_not_called()

    def test_user_unit_read_failures_and_incomplete_properties_are_visible(self):
        # Failed/partial reads carry a dated gap, never a fabricated healthy exit.
        good = unit_response()
        malformed = [
            good.stdout + "Id=duplicate.service\n", good.stdout + "Unrequested=yes\n",
            "not a property\n", "", b"not text", "x" * (d.RESPONSE_LIMIT + 1),
            *(good.stdout.replace(line + "\n", "") for line in good.stdout.splitlines()),
        ]
        for output in malformed:
            with self.subTest(size=len(output)), mock.patch.object(d.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout=output)):
                state = d.user_unit_state(d.CLEANUP_UNIT)
                self.assertEqual((state["status"], state["error"]), ("unknown", "malformed"))
        for failure in (
            subprocess.TimeoutExpired("systemctl", 2, stderr="secret-timeout"),
            OSError(errno.EACCES, "secret-read-error"),
        ):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                    d.subprocess, "run", side_effect=failure) as run:
                state = d.user_unit_state(d.CLEANUP_UNIT)
                self.assertEqual(state["status"], "unknown")
                self.assertIsNone(state["exit_status"])
                self.assertRegex(state["read_at"], r"Z$")
                self.assertNotIn("secret-", json.dumps(state))
                run.assert_called_once()
        for code in (9, float("nan"), True):
            with self.subTest(code=code), mock.patch.object(d.subprocess, "run",
                    return_value=SimpleNamespace(returncode=code, stdout=good.stdout,
                                                 stderr="secret-dbus-error")):
                state = d.user_unit_state(d.CLEANUP_UNIT)
                self.assertEqual((state["status"], state["error"]), ("unknown", "read_failed"))
                self.assertNotIn("secret-", json.dumps(state))

    def test_guarded_cleanup_fixed_dispatch_only_and_fresh_observation(self):
        # Real action dispatch logic sees a fake unit transition, not a live host start.
        started = False
        def systemctl(args, **kwargs):
            nonlocal started
            self.assertEqual(self.events()[-1]["event"], "started")
            self.assertEqual(self.events()[-1]["target"],
                             {"scope": "user", "unit": d.CLEANUP_UNIT})
            if args[2] == "show":
                return unit_response(ActiveState="activating" if started else "inactive",
                                     SubState="start" if started else "dead",
                                     InvocationID=INVOCATION if started else "")
            self.assertEqual(args, ["systemctl", "--user", "start", "--no-block",
                                    "--", "system-janitor-catchup.service"])
            self.assertEqual(kwargs, {
                "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "shell": False, "timeout": 12, "check": False,
            })
            started = True
            return SimpleNamespace(returncode=0)
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
            result = self.controller.execute(self.cleanup)
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show", "start", "show"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["dispatch_only"])
        self.assertEqual(result["dispatch"], {
            "status": "accepted", "dispatched": True, "unit": d.CLEANUP_UNIT, "scope": "user",
        })
        self.assertEqual(result["observation"]["active"], "activating")
        self.assertEqual(result["observation"]["invocation_id"], INVOCATION)
        self.assertIn("dispatch only", result["message"])
        self.assertIn("does not mean", result["message"])
        self.assertEqual([event["event"] for event in self.events()], ["started", "accepted"])
        self.assertEqual(self.events()[-1]["observation"], result["observation"])
        self.assertTrue(all(event["action_id"] == result["action_id"] for event in self.events()))
        self.assertEqual(stat.S_IMODE((self.controller.state_dir / d.AUDIT_NAME).stat().st_mode), 0o600)
        self.assertNotIn(self.controller.nonce, json.dumps(self.events()))
        self.services.assert_not_called()
        self.socket.assert_not_called()
        self.wake.assert_called_once_with()

    def test_guarded_cleanup_busy_and_unusable_units_never_start(self):
        # Busy, missing, masked, aliased and manually unstartable units have no fallback.
        for active in ("active", "activating", "deactivating", "reloading", "refreshing"):
            with self.subTest(active=active), mock.patch.object(d.subprocess, "run",
                    return_value=unit_response(ActiveState=active, SubState="running",
                                               InvocationID=INVOCATION)) as run:
                result = self.controller.execute(self.cleanup)
                self.assertEqual(result["dispatch"]["status"], "already_running")
                self.assertFalse(result["dispatch"]["dispatched"])
                self.assertEqual(result["observation"]["active"], active)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][2], "show")
                self.assertEqual(self.events()[-1]["event"], "observed")
        for changes, status in (
            ({"LoadState": "not-found", "Result": ""}, 404),
            ({"LoadState": "masked", "Result": ""}, 503),
            ({"LoadState": "error", "Result": ""}, 503),
            ({"LoadState": "bad-setting"}, 503), ({"Id": UNIT}, 503),
            ({"CanStart": "no"}, 503), ({"RefuseManualStart": "yes"}, 503),
            ({"ActiveState": "maintenance"}, 503), ({"InvocationID": "malformed"}, 503),
        ):
            with self.subTest(changes=changes), mock.patch.object(d.subprocess, "run",
                    return_value=unit_response(**changes)) as run:
                error = self.error(self.cleanup, status)
                self.assertFalse(error.details["dispatch"]["dispatched"])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][2], "show")
        self.assertEqual(self.wake.call_count, 14)
        self.services.assert_not_called()
        self.inventory.assert_not_called()

    def test_guarded_cleanup_launch_and_post_launch_gaps_never_retry(self):
        # Accepted dispatch plus an unreadable runner is unknown, not completed cleanup.
        cases = (
            ("launch-timeout", 504, "unknown"), ("launch-error", 503, "failed"),
            ("rejected", 502, "failed"), ("read-timeout", 504, "unknown"),
            ("malformed", 503, "unknown"), ("disappeared", 503, "unknown"),
        )
        for name, status, outcome in cases:
            with self.subTest(case=name):
                controller = self.controller_at(self.root / name)
                reads = 0
                def systemctl(args, **kwargs):
                    nonlocal reads
                    if args[2] == "show":
                        reads += 1
                        if reads > 1:
                            if name == "read-timeout":
                                raise subprocess.TimeoutExpired("systemctl", 2, stderr="secret-state")
                            if name == "malformed":
                                return unit_response(ExecMainStatus="NaN")
                            if name == "disappeared":
                                return unit_response(LoadState="not-found", Result="")
                        return unit_response()
                    self.assertEqual(args, ["systemctl", "--user", "start", "--no-block", "--", d.CLEANUP_UNIT])
                    if name == "launch-timeout":
                        raise subprocess.TimeoutExpired("systemctl", 12, stderr="secret-launch")
                    if name == "launch-error":
                        raise OSError(errno.EACCES, "secret-launch")
                    return SimpleNamespace(returncode=7 if name == "rejected" else 0,
                                           stderr="secret-daemon")
                with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
                    error = self.error(self.cleanup, status, outcome, controller)
                self.assertEqual(sum(call.args[0][2] == "start" for call in run.call_args_list), 1)
                self.assertTrue(error.details["dispatch_only"])
                self.assertNotIn("secret-", json.dumps(self.events(controller)))
                self.assertEqual(self.events(controller)[-1]["event"], outcome)
                if name in {"read-timeout", "malformed", "disappeared"}:
                    self.assertEqual(error.details["dispatch"]["status"], "accepted")
                    self.assertNotEqual(error.details["observation"]["status"], "available")
        self.assertEqual(self.wake.call_count, len(cases))

    def test_guarded_cleanup_private_audit_and_shared_serialization(self):
        # Cleanup inherits both locks and cannot dispatch without durable private intent.
        with mock.patch.object(d.os, "write", side_effect=OSError(errno.ENOSPC, "full")), \
                mock.patch.object(d.subprocess, "run") as run:
            self.error(self.cleanup, 500)
            run.assert_not_called()
        self.wake.assert_not_called()
        other = self.controller_at(self.controller.state_dir)
        def systemctl(args, **kwargs):
            if args[2] == "show":
                return unit_response()
            self.error(self.cleanup, 409)
            self.error(self.service, 409, controller=other)
            return SimpleNamespace(returncode=0)
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
            result = self.controller.execute(self.cleanup)
        self.assertEqual(sum(call.args[0][2] == "start" for call in run.call_args_list), 1)
        self.assertEqual(self.events()[-1]["action_id"], result["action_id"])
        self.wake.assert_called_once_with()
        self.wake.reset_mock()
        real_write, writes = os.write, 0
        def fail_outcome(fd, data):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError(errno.ENOSPC, "full")
            return real_write(fd, data)
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl), \
                mock.patch.object(d.os, "write", side_effect=fail_outcome):
            error = self.error(self.cleanup, 500, "unknown")
        self.assertEqual(error.details["dispatch"]["status"], "accepted")
        self.assertEqual(self.events()[-1]["event"], "started")
        self.wake.assert_called_once_with()

    def test_guarded_cleanup_real_signal_terminated_scheduler_is_unknown(self):
        # A real helper can lose its reply after enqueueing; its signal exit is not a refusal.
        original_run = subprocess.run
        for number in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=number):
                controller = self.controller_at(self.root / f"signal-{number}")
                launched, starts = False, []
                def systemctl(args, **kwargs):
                    nonlocal launched
                    self.assertEqual(args[:2], ["systemctl", "--user"])
                    self.assertEqual(args[-2:], ["--", d.CLEANUP_UNIT])
                    if args[2] == "show":
                        raw = unit_response(
                            ActiveState="activating" if launched else "inactive",
                            SubState="start" if launched else "dead",
                            InvocationID=INVOCATION if launched else "",
                        ).stdout.encode()
                        return original_run(
                            [sys.executable, "-B", "-c",
                             f"import sys; sys.stdout.buffer.write({raw!r})"],
                            **kwargs,
                        )
                    self.assertEqual(args, ["systemctl", "--user", "start", "--no-block",
                                            "--", d.CLEANUP_UNIT])
                    self.assertEqual(self.events(controller)[-1]["event"], "started")
                    launched = True
                    starts.append(args)
                    result = original_run(
                        [sys.executable, "-B", "-c",
                         f"import os,signal; os.kill(os.getpid(), signal.{number.name})"], **kwargs,
                    )
                    self.assertEqual(result.returncode, -number)
                    return result
                with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
                    error = self.error(self.cleanup, 502, "unknown", controller)
                self.assertEqual(len(starts), 1)
                self.assertEqual([call.args[0][2] for call in run.call_args_list],
                                 ["show", "start", "show"])
                self.assertIs(error.details["dispatch_only"], True)
                self.assertEqual(error.details["dispatch"]["status"], "unknown")
                self.assertIs(error.details["dispatch"]["dispatched"], True)
                self.assertEqual(error.details["observation"]["active"], "activating")
                self.assertEqual(error.details["observation"]["invocation_id"], INVOCATION)
                self.assertEqual([event["event"] for event in self.events(controller)],
                                 ["started", "unknown"])
                self.assertEqual(self.events(controller)[-1]["observation"],
                                 error.details["observation"])
                self.assertNotIn("rejected", json.dumps(self.events(controller)))
        self.assertEqual(self.wake.call_count, 2)

    def test_guarded_cleanup_real_undecodable_unit_reads_fail_closed(self):
        # Real bytes must fail inside subprocess's text decoder, before any fake decoded result exists.
        original_run = subprocess.run
        for phase in ("prelaunch", "postlaunch"):
            with self.subTest(phase=phase):
                controller = self.controller_at(self.root / phase)
                launched, starts = False, []
                def systemctl(args, **kwargs):
                    nonlocal launched
                    self.assertEqual(args[:2], ["systemctl", "--user"])
                    self.assertEqual(args[-2:], ["--", d.CLEANUP_UNIT])
                    if args[2] == "show":
                        self.assertIs(kwargs["text"], True)
                        self.assertIs(kwargs["capture_output"], True)
                        self.assertEqual(kwargs["timeout"], 2)
                        raw = unit_response().stdout.encode()
                        code = ("import os; os.write(1, bytes([255]))" if phase == "prelaunch" or launched
                                else f"import sys; sys.stdout.buffer.write({raw!r})")
                        return original_run(
                            [sys.executable, "-B", "-c", code], **kwargs,
                        )
                    self.assertEqual(args, ["systemctl", "--user", "start", "--no-block",
                                            "--", d.CLEANUP_UNIT])
                    self.assertEqual(self.events(controller)[-1]["event"], "started")
                    starts.append(args)
                    launched = True
                    return original_run([sys.executable, "-B", "-c", "pass"], **kwargs)
                self.wake.reset_mock()
                with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
                    error = self.error(self.cleanup, 503,
                                       "failed" if phase == "prelaunch" else "unknown", controller)
                self.assertEqual(len(starts), 0 if phase == "prelaunch" else 1)
                self.assertEqual([call.args[0][2] for call in run.call_args_list],
                                 ["show"] if phase == "prelaunch" else ["show", "start", "show"])
                observation = error.details["observation"]
                self.assertEqual((observation["status"], observation["error"]), ("unknown", "malformed"))
                self.assertIsNone(observation["exit_status"])
                self.assertRegex(observation["read_at"], r"Z$")
                self.assertIn("decod", observation["detail"].lower())
                self.assertIs(error.details["dispatch_only"], True)
                self.assertEqual(error.details["dispatch"]["status"],
                                 "not_dispatched" if phase == "prelaunch" else "accepted")
                self.assertEqual([event["event"] for event in self.events(controller)],
                                 ["started", "failed" if phase == "prelaunch" else "unknown"])
                self.assertNotIn("0xff", json.dumps(self.events(controller)))
                self.wake.assert_called_once_with()

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
                self.assertEqual(records[-2:], [
                    (method, f"/containers/{IDENTIFIER}{suffix}", b""),
                    ("GET", f"/containers/{IDENTIFIER}/json", b""),
                ])
                self.assertTrue(result["ok"])
                self.assertEqual(result["observation"]["state"], "exited")
                self.assertNotIn("secret-", json.dumps(result))
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

    def test_recovery_container_fixed_routes_state_guards_and_exact_inspect(self):
        # Both engines use only full-ID fixed verbs; disappearing capped rows cannot prove state.
        path, records, reply = self.daemon()
        for engine in ("docker", "podman"):
            for action, state, observed, suffix in (
                ("start", "created", "running", "/start"),
                ("start", "exited", "running", "/start"),
                ("start", "stopped", "running", "/start"),
                ("restart", "running", "running", "/restart?t=10"),
            ):
                with self.subTest(engine=engine, action=action, state=state):
                    self.inventory.return_value = {"items": [{"id": IDENTIFIER, "State": state,
                                                               "protected": False}]}
                    reply["inspect"]["state"] = observed
                    reply["after_mutation"] = lambda: setattr(self.inventory, "return_value", {"items": []})
                    result = self.controller.execute(dict(self.container, engine=engine, action=action))
                    self.assertEqual(records[-2:], [
                        ("POST", f"/containers/{IDENTIFIER}{suffix}", b""),
                        ("GET", f"/containers/{IDENTIFIER}/json", b""),
                    ])
                    self.assertEqual(result["observation"]["state"], observed)
                    self.assertEqual(result["observation"]["id"], IDENTIFIER)
                    self.assertEqual(result["observation"]["engine"], engine)
                    self.assertRegex(result["observation"]["read_at"], r"Z$")
                    self.assertNotIn("secret-", json.dumps(result))
                    self.inventory.assert_called_with(path)
        self.assertEqual((self.inventory.call_count, self.socket.call_count), (8, 8))
        records.clear()
        reply.pop("after_mutation")
        for action, state, protected, status in (
            ("start", "running", False, 409), ("start", "paused", False, 409),
            ("start", "restarting", False, 409), ("start", None, False, 409),
            ("start", [], False, 409), ("restart", "exited", False, 409),
            ("restart", "created", False, 409), ("restart", "paused", False, 409),
            ("restart", {}, False, 409), ("start", "exited", True, 403),
            ("restart", "running", True, 403), ("start", "created", "unknown", 503),
        ):
            with self.subTest(action=action, state=state):
                self.inventory.return_value = {"items": [
                    {"id": IDENTIFIER, "State": state, "protected": protected}]}
                self.error(dict(self.container, action=action), status)
        self.assertEqual(records, [])

    def test_exact_container_read_gaps_and_absence_do_not_rewrite_command_receipts(self):
        # Exact 404 is absence, not "stopped"; bad UTF/JSON/identity/metadata stays unknown after success.
        _, records, reply = self.daemon()
        bad = [
            b"\xff", b"{bad", ("[" * 1100 + "]" * 1100).encode(),
            json.dumps({"Id": "b" * 64, "Names": ["same-name"], "State": {"Status": "exited"}}).encode(),
            json.dumps({"Id": IDENTIFIER[:12], "State": {"Status": "exited"}}).encode(),
            json.dumps({"Id": IDENTIFIER, "State": "running"}).encode(),
            json.dumps({"Id": IDENTIFIER, "State": {"Status": "mystery"}}).encode(),
            json.dumps({"Id": IDENTIFIER, "State": {"Status": "running", "Running": "yes"}}).encode(),
            json.dumps({"Id": IDENTIFIER, "State": {"Status": "running"}, "Config": {"Labels": []}}).encode(),
            b"x" * (d.RESPONSE_LIMIT + 1),
        ]
        for raw in bad:
            with self.subTest(size=len(raw)):
                reply["inspect"].update(status=200, body=raw)
                result = self.controller.execute(self.container)
                self.assertTrue(result["ok"])
                self.assertEqual(result["message"], "Container stop completed.")
                self.assertEqual(result["observation"]["state"], "unknown")
                self.assertEqual(self.events()[-1]["event"], "succeeded")
        reply["inspect"].update(status=404, body=b"secret-error")
        result = self.controller.execute(self.container)
        self.assertEqual((result["observation"]["status"], result["observation"]["state"]),
                         ("absent", "absent"))
        self.assertNotIn("secret-", json.dumps(result))
        reply.update(status=409)
        error = self.error(self.container, 409)
        self.assertEqual(error.details["observation"]["state"], "absent")
        self.assertEqual(error.details["action_id"], self.events()[-1]["action_id"])
        self.assertEqual(len(records), 24)
        self.assertEqual(sum(method != "GET" for method, _, _ in records), 12)

    def test_exact_container_read_has_independent_total_deadline(self):
        # Progressing headers/body cannot renew the two-second total read budget or invent command failure.
        _, records, reply = self.daemon()
        original_json = d._json
        for mode in ("headers", "body", "parse"):
            def parse(raw):
                if mode == "parse":
                    time.sleep(0.08)  # Even already-buffered JSON consumes the same total deadline.
                return original_json(raw)
            with self.subTest(mode=mode), mock.patch.object(d, "UNIT_STATE_TIMEOUT", 0.05), \
                    mock.patch.object(d, "_json", side_effect=parse):
                reply["inspect"].update(trickle_headers=0.025 if mode == "headers" else 0,
                                        trickle=0.025 if mode == "body" else 0)
                started = time.monotonic()
                result = self.controller.execute(self.container)
                self.assertLess(time.monotonic() - started, 0.25)
                self.assertTrue(result["ok"])
                self.assertEqual(result["observation"]["state"], "unknown")
                self.assertIn("deadline", result["observation"]["detail"])
                self.assertEqual(self.events()[-1]["event"], "succeeded")
        self.assertEqual(len(records), 6)
        self.assertEqual(sum(method != "GET" for method, _, _ in records), 3)

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
        self.assertEqual(len(records), 10)
        self.assertEqual(sum(method != "GET" for method, _, _ in records), 5)
        self.assertEqual(sum(method == "GET" for method, _, _ in records), 5)
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
        code, failure = 0, None
        def systemctl(args, **kwargs):
            if args[2] == "show":
                return unit_response(Id=UNIT, ActiveState="active")
            if failure:
                raise failure
            return SimpleNamespace(returncode=code, stderr="secret-service-detail")
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
            self.controller.execute(self.service)
            self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show", "stop", "show"])
            self.assertEqual(run.call_args_list[1], mock.call(
                ["systemctl", "--user", "stop", "--", UNIT], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                timeout=12, check=False,
            ))
            for unit in d.PROTECTED_SERVICES:
                self.error(dict(self.service, unit=unit), 403)
            self.services.return_value = {"items": []}
            self.error(self.service, 404)
            self.services.return_value = {"items": [{"unit": UNIT, "scope": "system"}]}
            self.error(self.service, 403)
            self.assertEqual(run.call_count, 3)
            self.services.return_value = {"items": [{"unit": UNIT, "scope": "user"}]}
            code = 7
            error = self.error(self.service, 502)
            self.assertIn("rc=7", str(error))
            self.assertIn(UNIT, str(error))
            self.assertNotIn("secret-service-detail", str(error))
            failure = subprocess.TimeoutExpired("systemctl", 12, stderr="secret-timeout")
            self.error(self.service, 504, "unknown")
            self.assertEqual([call.args[0][2] for call in run.call_args_list],
                             ["show", "stop", "show"] * 3)
        self.assertEqual(self.events()[-1]["event"], "unknown")
        self.assertNotIn("secret-", json.dumps(self.events()))

    def test_recovery_service_signal_exit_preserves_unknown_and_exact_post_read(self):
        # A signalled helper may already have sent the user-manager request; never call it rejected.
        for number in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=number):
                def systemctl(args, **kwargs):
                    if args[2] == "show":
                        return unit_response(Id=UNIT, ActiveState="inactive")
                    self.assertEqual(args, ["systemctl", "--user", "stop", "--", UNIT])
                    return SimpleNamespace(returncode=-number)
                with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
                    error = self.error(self.service, 502, "unknown")
                self.assertEqual([call.args[0][2] for call in run.call_args_list],
                                 ["show", "stop", "show"])
                self.assertEqual(error.details["observation"]["state"], "inactive")
                self.assertRegex(error.details["observation"]["read_at"], r"Z$")
                self.assertEqual(self.events()[-1]["event"], "unknown")
                self.assertNotIn("rejected", json.dumps(self.events()))

    def test_recovery_service_retained_identity_and_per_verb_start_policy(self):
        # A stopped service can leave the capped list; exact manager state, not list absence, gates recovery.
        can_start, active = False, "active"
        def systemctl(args, **kwargs):
            nonlocal active
            if args[2] == "show":
                return unit_response(Id=UNIT, CanStart="yes" if can_start else "no",
                                     ActiveState=active, SubState="running" if active == "active" else "dead")
            self.assertEqual(args[:2], ["systemctl", "--user"])
            self.assertEqual(args[-2:], ["--", UNIT])
            self.assertEqual(kwargs["timeout"], 12)
            self.assertFalse(kwargs["shell"])
            active = "inactive" if args[2] == "stop" else "active"
            return SimpleNamespace(returncode=0)
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
            stopped = self.controller.execute(self.service)
            self.assertEqual(stopped["observation"]["state"], "inactive")
            self.assertFalse(stopped["observation"]["can_start"])
            self.error(dict(self.service, action="start"), 409)
            self.error(dict(self.service, action="restart"), 409)
            can_start = True
            self.services.return_value = {"items": [], "truncated": True}
            started = self.controller.execute(dict(self.service, action="start"))
            restarted = self.controller.execute(dict(self.service, action="restart"))
        self.assertEqual(started["observation"]["state"], "active")
        self.assertEqual(restarted["observation"]["state"], "active")
        self.assertEqual([call.args[0][2] for call in run.call_args_list if call.args[0][2] != "show"],
                         ["stop", "start", "restart"])
        self.assertEqual(self.services.call_count, 5)

    def test_recovery_service_post_read_gap_is_not_command_failure(self):
        # A successful command and a failed exact observation are independent facts, including decoder failures.
        reads = 0
        def systemctl(args, **kwargs):
            nonlocal reads
            if args[2] == "show":
                reads += 1
                if reads == 2:
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "fixture decode")
                return unit_response(Id=UNIT, ActiveState="active")
            return SimpleNamespace(returncode=0)
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
            result = self.controller.execute(self.service)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], f"User service {UNIT} stop completed.")
        self.assertEqual(result["observation"]["state"], "unknown")
        self.assertEqual(result["observation"]["error"], "malformed")
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show", "stop", "show"])
        self.assertEqual(self.events()[-1]["event"], "succeeded")

    def test_recovery_services_refuse_protected_canonical_aliases_and_malformed_scope(self):
        # Manual-stop usability differs from Start, but aliases and missing/unknown user scope never grant authority.
        for action in ("stop", "start", "restart"):
            request = dict(self.service, action=action)
            for canonical in d.PROTECTED_SERVICES:
                with self.subTest(action=action, canonical=canonical), mock.patch.object(
                        d.subprocess, "run", return_value=unit_response(Id=canonical)) as run:
                    self.error(request, 403)
                    self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show"])
            for changes, status in (
                ({"Id": "unprotected-alias.service"}, 503),
                ({"Id": UNIT, "LoadState": "masked"}, 503),
                ({"Id": UNIT, "LoadState": "not-found", "Result": ""}, 404),
                ({"Id": UNIT, "ExecMainStatus": "NaN"}, 503),
                ({"Id": UNIT, "ActiveState": "maintenance"}, 503),
            ):
                with self.subTest(action=action, changes=changes), mock.patch.object(
                        d.subprocess, "run", return_value=unit_response(**changes)) as run:
                    self.error(request, status)
                    self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show"])
            with mock.patch.object(d.subprocess, "run") as run:
                for scope in ("system", None, True, [], {}):
                    self.services.return_value = {"items": [{"unit": UNIT, "scope": scope}]}
                    self.error(request, 403)
                self.services.return_value = {"items": [{"unit": UNIT}]}
                self.error(request, 403)
                self.services.return_value = {"items": [{"unit": UNIT, "scope": "user"}] * 2}
                self.error(request, 403)
                for invalid in (None, [], {"items": None}, {"items": [{}], "status": "unknown"}):
                    self.services.return_value = invalid
                    self.error(request, 503)
                run.assert_not_called()
            self.services.return_value = {"items": [{"unit": UNIT, "scope": "user"}]}
        for refused in ("yes", "no"):
            def systemctl(args, **kwargs):
                return (unit_response(Id=UNIT, CanStart="no", RefuseManualStart=refused)
                        if args[2] == "show" else SimpleNamespace(returncode=0))
            with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
                self.assertTrue(self.controller.execute(self.service)["ok"])
                self.error(dict(self.service, action="start"), 409)
                self.error(dict(self.service, action="restart"), 409)
                self.assertEqual(sum(call.args[0][2] == "stop" for call in run.call_args_list), 1)

    def test_exact_process_observation_never_resignals_replacement_or_invents_exit(self):
        # The post-read keeps the original PID/start pair, even when that PID is already reused or unreadable.
        child = self.child()
        identity = d.process_identity(child.pid)
        request = {"kind": "process", "pid": child.pid, "action": "kill",
                   "start_time_ticks": identity["start_time_ticks"]}
        for after, state in (
            (identity, "present"),
            (dict(identity, start_time_ticks=identity["start_time_ticks"] + 1), "replaced"),
            (d.ActionError(404, "gone"), "missing"),
            (d.ActionError(503, "secret-read"), "unknown"),
            (dict(identity, start_time_ticks=True), "unknown"),
        ):
            with self.subTest(state=state), mock.patch.object(d, "process_identity",
                    side_effect=[identity, after]) as read, \
                    mock.patch.object(d, "protected_process", return_value=False), \
                    mock.patch.object(d.signal, "pidfd_send_signal") as send:
                result = self.controller.execute(request)
                self.assertTrue(result["ok"])
                self.assertEqual(result["observation"]["state"], state)
                self.assertEqual(result["observation"]["pid"], child.pid)
                self.assertEqual(result["observation"]["start_time_ticks"], identity["start_time_ticks"])
                self.assertRegex(result["observation"]["read_at"], r"Z$")
                self.assertEqual(read.call_count, 2)
                send.assert_called_once()
                self.assertIsNone(child.poll())  # Signaling itself is intercepted for every replacement case.
                self.assertNotIn("secret-", json.dumps(result))

    def test_history_actual_cleanup_receipts_are_private_curated_and_read_only(self):
        # Only real journal IDs/accepted/observed semantics are exposed; the GET reader never writes or locks.
        def systemctl(args, **kwargs):
            unit = args[-1]
            return unit_response(Id=unit) if args[2] == "show" else SimpleNamespace(returncode=0)
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl):
            stopped = self.controller.execute(self.service)
            accepted = self.controller.execute(self.cleanup)
        with mock.patch.object(d.subprocess, "run", return_value=unit_response(
                ActiveState="active", SubState="running")):
            observed = self.controller.execute(self.cleanup)
        path = self.controller.state_dir / d.AUDIT_NAME
        original = path.read_bytes()
        events = self.events()
        events[-1]["token"] = "secret-token"
        events[-1]["nonce"] = "secret-nonce"
        events[-1]["target"]["args"] = ["secret-args"]
        events[-1]["observation"]["env"] = ["secret-env"]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        before, info = path.read_bytes(), path.stat()
        with mock.patch.object(Path, "mkdir") as mkdir, \
                mock.patch.object(d.os, "write") as write, mock.patch.object(d.os, "replace") as replace, \
                mock.patch.object(d.fcntl, "flock") as flock:
            self.controller._lock.acquire()
            try:
                history = self.controller.history()
            finally:
                self.controller._lock.release()
            mkdir.assert_not_called()
            write.assert_not_called()
            replace.assert_not_called()
            flock.assert_not_called()
        self.assertEqual(history["status"], "available")
        self.assertEqual(len(history["events"]), 6)
        self.assertEqual({event["action_id"] for event in history["events"]},
                         {stopped["action_id"], accepted["action_id"], observed["action_id"]})
        self.assertEqual([event["event"] for event in history["events"]],
                         ["started", "succeeded", "started", "accepted", "started", "observed"])
        self.assertIs(history["events"][3]["dispatch_only"], True)
        self.assertNotIn("secret-", json.dumps(history))
        self.assertIn("does not mean cleanup completed.", history["events"][3]["message"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((path.stat().st_mtime_ns, path.stat().st_mode), (info.st_mtime_ns, info.st_mode))
        self.assertTrue(original)

    def test_history_boundaries_invalid_numeric_utf_json_and_incomplete_tail_are_visible(self):
        # Bounded tails cannot turn torn/invalid records into a healthy empty history or publish arbitrary extras.
        state = self.controller.state_dir
        state.mkdir(mode=0o700)
        path = state / d.AUDIT_NAME
        path.touch(mode=0o600)
        def event(n):
            return {"action_id": f"{n:032x}", "kind": "service", "action": "stop", "event": "started",
                    "time": "2026-11-05T00:00:00Z", "target": {"scope": "user", "unit": UNIT},
                    "message": "x" * 600, "extra": "secret-extra"}
        rows = [event(n) for n in range(30)]
        raw = b"x" * (d.HISTORY_BYTES + 100) + b"\n" + "".join(
            json.dumps(value) + "\n" for value in rows).encode()
        path.write_bytes(raw)
        real_read = os.read
        with mock.patch.object(d.os, "read", wraps=real_read) as read:
            history = self.controller.history()
        self.assertEqual(read.call_count, 1)
        self.assertLessEqual(read.call_args.args[1], 64 * 1024)
        self.assertEqual(len(history["events"]), 20)
        self.assertEqual([item["action_id"] for item in history["events"]], [f"{n:032x}" for n in range(10, 30)])
        self.assertTrue(history["truncated"])
        self.assertTrue(history["quality"]["tail_truncated"])
        self.assertTrue(history["quality"]["events_truncated"])
        self.assertTrue(history["quality"]["incomplete_first_line"])
        self.assertEqual(history["quality"]["invalid_lines"], 0)
        self.assertEqual(len(history["events"][-1]["message"]), 512)
        self.assertNotIn("secret-", json.dumps(history))
        invalid = [
            dict(event(40), status=True), dict(event(41), status="200"),
            dict(event(42), target={"scope": "system", "unit": UNIT}),
            dict(event(43), kind="process", action="kill", target={"pid": True, "start_time_ticks": 1}),
            dict(event(44), observation={"status": "available", "scope": "user", "unit": UNIT,
                 "read_at": "2026-11-05T00:00:00Z", "state": "inactive", "exit_status": True}),
            {"action_id": "c" * 32, "kind": "cleanup", "action": "run", "event": "succeeded",
             "time": "2026-11-05T00:00:00Z", "target": {"scope": "user", "unit": d.CLEANUP_UNIT}},
        ]
        path.write_bytes((json.dumps(event(0)) + "\n" + "".join(json.dumps(value) + "\n"
                         for value in invalid)).encode() + b"\xff\n{bad}\n" +
                         ("[" * 1100 + "]" * 1100 + "\n").encode() +
                         json.dumps(event(99)).encode())  # Valid JSON without newline is still incomplete.
        history = self.controller.history()
        self.assertEqual(history["status"], "partial")
        self.assertEqual(history["quality"]["invalid_lines"], len(invalid) + 3)
        self.assertTrue(history["quality"]["incomplete_tail"])
        self.assertEqual([item["action_id"] for item in history["events"]], ["0" * 32])
        self.assertNotIn("secret-", json.dumps(history))
        # One byte past the tail bound must be reported, not read or quietly accepted as complete.
        value = event(1)
        value["padding"] = ""
        encoded = (json.dumps(value) + "\n").encode()
        value["padding"] = "x" * (d.HISTORY_BYTES - len(encoded))
        raw = (json.dumps(value) + "\n").encode()
        self.assertEqual(len(raw), d.HISTORY_BYTES)
        path.write_bytes(raw)
        self.assertFalse(self.controller.history()["quality"]["tail_truncated"])
        path.write_bytes(raw[:-1] + b" \n")
        history = self.controller.history()
        self.assertTrue(history["quality"]["tail_truncated"])
        self.assertEqual(history["status"], "partial")

    def test_history_missing_unavailable_and_started_only_remain_distinct(self):
        # A reader neither creates first-run state nor treats unsafe files or lost terminal audits as healthy history.
        with mock.patch.object(Path, "mkdir") as mkdir, mock.patch.object(d.fcntl, "flock") as flock:
            history = self.controller.history()
            self.assertEqual(history["status"], "no_history_yet")
            self.assertFalse(self.controller.state_dir.exists())
            mkdir.assert_not_called()
            flock.assert_not_called()
        sentinel = self.root / "history-sentinel"
        sentinel.write_text("secret-sentinel")
        for case in ("symlink", "mode", "hardlink", "fifo", "directory"):
            state = self.root / f"history-{case}"
            state.mkdir(mode=0o700)
            path = state / d.AUDIT_NAME
            if case == "symlink":
                path.symlink_to(sentinel)
            elif case == "hardlink":
                os.link(sentinel, path)
            elif case == "fifo":
                os.mkfifo(path, 0o600)
            else:
                path.touch(mode=0o600)
            if case == "mode":
                path.chmod(0o644)
            if case == "directory":
                state.chmod(0o777)
            with self.subTest(case=case):
                history = self.controller_at(state).history()
                self.assertEqual(history["status"], "unavailable")
                self.assertEqual(history["events"], [])
                self.assertNotIn("secret-", json.dumps(history))
        self.assertEqual(sentinel.read_text(), "secret-sentinel")
        real_write, writes = os.write, 0
        def fail_terminal(fd, data):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError(errno.ENOSPC, "full")
            return real_write(fd, data)
        def systemctl(args, **kwargs):
            return unit_response(Id=UNIT) if args[2] == "show" else SimpleNamespace(returncode=0)
        with mock.patch.object(d.os, "write", side_effect=fail_terminal), \
                mock.patch.object(d.subprocess, "run", side_effect=systemctl):
            error = self.error(self.service, 500, "unknown")
        history = self.controller.history()
        self.assertEqual([event["event"] for event in history["events"]], ["started"])
        self.assertEqual(history["events"][0]["action_id"], error.details["action_id"])

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
                def systemctl(args, **kwargs):
                    return unit_response(Id=UNIT) if args[2] == "show" else SimpleNamespace(returncode=0)
                with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run:
                    self.assertTrue(self.controller.execute(self.service)["ok"])
                    self.assertEqual([call.args[0][2] for call in run.call_args_list],
                                     ["show", "stop", "show"])
        self.assertEqual(len(records), 4)
        self.assertEqual(sum(method != "GET" for method, _, _ in records), 2)

    def test_audit_is_private_and_started_precedes_dispatch(self):
        # A durable, sanitized intent must exist before the first side effect.
        def dispatch(args, **kwargs):
            events = self.events()
            self.assertEqual(events[-1]["event"], "started")
            self.assertEqual(events[-1]["target"], {"scope": "user", "unit": UNIT})
            path = self.controller.state_dir / d.AUDIT_NAME
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
            return unit_response(Id=UNIT) if args[2] == "show" else SimpleNamespace(returncode=0)

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

        def systemctl(args, **kwargs):
            return unit_response(Id=UNIT) if args[2] == "show" else SimpleNamespace(returncode=0)
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl) as run, \
                mock.patch.object(d.os, "write", side_effect=fail_outcome):
            self.error(self.service, 500, "unknown")
            self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show", "stop", "show"])
        self.assertEqual([event["event"] for event in self.events()], ["started"])
        self.wake.assert_called_once_with()
        self.wake.side_effect = RuntimeError("secret-refresh-detail")
        with mock.patch.object(d.subprocess, "run", side_effect=systemctl):
            error = self.error(self.service, 500, "unknown")
        self.assertNotIn("secret-refresh-detail", str(error))
        self.assertEqual(self.events()[-1]["event"], "unknown")

    def test_nonblocking_serialization_and_bounded_rotation(self):
        # Reentrancy and a second controller cannot race dispatch or journal rotation.
        other = self.controller_at(self.controller.state_dir)

        def dispatch(args, **kwargs):
            self.error(self.service, 409)
            self.error(self.service, 409, controller=other)
            return unit_response(Id=UNIT) if args[2] == "show" else SimpleNamespace(returncode=0)

        with mock.patch.object(d.subprocess, "run", side_effect=dispatch) as run:
            self.controller.execute(self.service)
            self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show", "stop", "show"])
        self.assertEqual(len(self.events()), 2)
        self.wake.assert_called_once_with()
        with mock.patch.object(d, "AUDIT_LIMIT", 1024), \
                mock.patch.object(d.subprocess, "run", side_effect=lambda args, **kwargs:
                                  unit_response(Id=UNIT) if args[2] == "show" else
                                  SimpleNamespace(returncode=0)):
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
