#!/usr/bin/env python3
"""Focused collector, access-boundary and cached-HTTP regressions."""

import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("dashboard", ROOT / "scripts/server-dashboard.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)
INVOCATION = "b" * 32


def unit_state(**changes):
    value = {
        "unit": "system-janitor-catchup.service", "scope": "user", "status": "available",
        "id": "system-janitor-catchup.service", "load_state": "loaded", "active": "inactive",
        "sub": "dead", "result": "success", "exit_status": 0, "invocation_id": INVOCATION,
        "can_start": True, "refuse_manual_start": False, "read_at": d.action_module()._now(),
        "error": None, "detail": None,
    }
    return dict(value, **changes)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        for path in ("self", "net", "11"):
            (self.proc / path).mkdir(parents=True)
        (self.proc / "stat").write_text("cpu 1000 0 0 1000 0 0 0 0 300 0\n")
        (self.proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 100 kB\nSwapFree: 80 kB\n")
        (self.proc / "uptime").write_text("200 20\n")
        (self.proc / "self/mountinfo").write_text("")
        (self.proc / "net/dev").write_text("header\nheader\neth0: 1000 0 0 0 0 0 0 0 2000 0\n")
        self.process(100, 30)
        self.collector = d.Collector(state=self.root / "state", proc=self.proc)
        # Every collector DBus observation is a fixture; no host unit is queried.
        patcher = mock.patch.object(d.action_module(), "user_unit_state", return_value=unit_state())
        self.unit_reader = patcher.start()
        self.addCleanup(patcher.stop)

    def process(self, start, ticks):
        fields = ["0"] * 22
        fields[0], fields[11], fields[19], fields[21] = "R", str(ticks), str(start), "3"
        (self.proc / "11/stat").write_text("11 (worker (nested)) " + " ".join(fields))

    def test_real_counter_deltas_and_pid_reuse(self):
        # A first sample, counter reset or reused PID must not look like zero load.
        first = self.collector.system(1)
        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(first["memory_used_percent"], 50)
        (self.proc / "stat").write_text("cpu 1100 0 0 1100 0 0 0 0 400 0\n")
        self.assertEqual(self.collector.system(2)["cpu_percent"], 50)
        self.assertIsNone(d.cpu_percent((400, 200), (100, 50)))
        self.assertIsNone(self.collector.network(1)[0]["received_bytes_per_second"])
        self.assertIsNone(self.collector.processes(1)["items"][0]["cpu_percent"])
        self.collector.previous_time = 1
        self.process(100, 130)
        self.assertGreater(self.collector.processes(2)["items"][0]["cpu_percent"], 0)
        self.process(101, 140)
        self.assertIsNone(self.collector.processes(3)["items"][0]["cpu_percent"])

    def test_failed_inventory_is_degraded_not_empty_healthy(self):
        # Unavailable DBus must remain a visible observation gap.
        with mock.patch.object(d, "command", side_effect=subprocess.TimeoutExpired("systemctl", 2)), \
                mock.patch.object(self.collector, "containers", return_value={"status": "not_installed", "items": []}):
            value = self.collector.collect()
        self.assertEqual(value["status"], "degraded")
        self.assertIsNone(value["system_services"])
        self.assertEqual(value["system"]["memory_used_percent"], 50)
        self.assertIn("system_services", [error["source"] for error in value["errors"]])

    def test_state_is_read_only_and_missing_records_are_unknown(self):
        # Monitoring must neither rewrite audit state nor fabricate first-run data.
        state = self.collector.state / "janitor"
        state.mkdir(parents=True)
        path = state / "maintenance-last.json"
        raw = b'{"status":"deferred","cache_bytes":1234}\n'
        path.write_bytes(raw)
        with mock.patch.object(d, "command", return_value="ActiveState=active\n"):
            value = self.collector.maintenance()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(value["maintenance"]["cache_bytes"], 1234)
        self.assertIsNone(value["last_run"])
        path.write_text("invalid JSON")
        with self.assertRaises(ValueError):
            self.collector.maintenance()
        # Non-finite JSON must not kill the sampler during strict serialization.
        path.write_text('{"cache_bytes": NaN}')
        with self.assertRaises(ValueError):
            d.read_state(path)
        # Structurally corrupt rendered fields must not crash the browser.
        for invalid in ({"actions": {}}, {"actions": [{"removed_bytes": "not bytes"}]},
                        {"build_admission": {"waiting": {}}}):
            path.write_text(json.dumps(invalid))
            with self.subTest(invalid=invalid), \
                    mock.patch.object(d, "command", return_value="ActiveState=active\n"):
                with self.assertRaises(ValueError):
                    self.collector.maintenance()

    def test_services_are_typed_scoped_and_failed_first(self):
        # Keep user/system inventories distinct and failures visible before truncation.
        rows = [{"unit": f"test-{n}.service", "active": "active", "sub": "running", "description": "test"}
                for n in range(260)]
        rows.append({"unit": "broken.service", "active": "failed", "sub": "failed", "description": "broken"})
        with mock.patch.object(d, "command", return_value=json.dumps(rows)) as run:
            result = self.collector.services(user=True)
        self.assertEqual(result["items"][0]["unit"], "broken.service")
        self.assertEqual(result["items"][0]["scope"], "user")
        self.assertEqual(result["total"], 261)
        self.assertTrue(result["truncated"])
        self.assertIn("--user", run.call_args.args[0])

    def test_current_unit_state_and_only_equal_nonempty_budget_phase_link(self):
        # Saved phases keep their own date and never stand in for wrapper/native completion.
        state = self.collector.state / "janitor"
        state.mkdir(parents=True)
        native = {"ended_at": "2020-01-01T00:00:00Z", "freed_kb": 7, "safety_integrity": "ok"}
        (state / "last-run.json").write_text(json.dumps(native))
        self.unit_reader.return_value = unit_state(active="activating", sub="start")
        for saved_id, linked in (
            (None, False), ("", False), ("legacy", False), ("c" * 32, False),
            (INVOCATION, True), (INVOCATION.upper(), False), (True, False),
        ):
            phase = {"status": "cleaned", "generated_at": "2020-01-02T00:00:00Z",
                     "cache_bytes": 123, "actions": [], "service_invocation_id": saved_id}
            path = state / "maintenance-last.json"
            raw = json.dumps(phase)
            path.write_text(raw)
            with self.subTest(saved_id=saved_id), mock.patch.object(
                    d, "command", return_value="ActiveState=active\n"):
                value = self.collector.maintenance()
            self.assertEqual(value["maintenance_phase_linked"], linked)
            self.assertEqual(value["maintenance"], phase)
            self.assertEqual(value["last_run"], native)
            self.assertEqual(value["service"]["active"], "activating")
            self.assertEqual(value["timer"]["ActiveState"], "active")
            self.assertEqual(path.read_text(), raw)
        self.unit_reader.return_value = unit_state(invocation_id=None)
        with mock.patch.object(d, "command", return_value="ActiveState=active\n"):
            self.assertFalse(self.collector.maintenance()["maintenance_phase_linked"])
        self.unit_reader.assert_called_with("system-janitor-catchup.service")

    def test_unit_observation_gaps_preserve_saved_records_and_optional_installation(self):
        # A failed live read is visible; missing optional installation is unavailable, not zero.
        state = self.collector.state / "janitor"
        state.mkdir(parents=True)
        phase = {"status": "deferred", "cache_bytes": 123, "actions": []}
        (state / "maintenance-last.json").write_text(json.dumps(phase))
        for service, expected_status in (
            (unit_state(status="unknown", load_state=None, active=None, sub=None, result=None,
                        exit_status=None, invocation_id=None, error="timeout",
                        detail="User-unit state read timed out after two seconds."), "degraded"),
            (unit_state(status="unavailable", load_state="not-found", result=None,
                        exit_status=None, invocation_id=None, detail="Optional unit not installed."), "healthy"),
        ):
            self.unit_reader.return_value = service
            with self.subTest(status=service["status"]), \
                    mock.patch.object(self.collector, "services", return_value={"items": []}), \
                    mock.patch.object(self.collector, "containers", return_value={"status": "not_installed", "items": []}), \
                    mock.patch.object(d, "command", return_value="ActiveState=active\n"):
                value = self.collector.collect()
            self.assertEqual(value["status"], expected_status)
            self.assertEqual(value["janitor"]["service"], service)
            self.assertEqual(value["janitor"]["maintenance"], phase)
            self.assertIsNone(value["janitor"]["service"]["exit_status"])
            self.assertFalse(value["janitor"]["maintenance_phase_linked"])
            if service["status"] == "unknown":
                self.assertIn("janitor_service", [error["source"] for error in value["errors"]])
        self.unit_reader.return_value = unit_state(active="active", sub="running")
        with mock.patch.object(d, "command", side_effect=subprocess.TimeoutExpired("systemctl", 2)):
            value = self.collector.maintenance()
        self.assertEqual(value["service"]["active"], "active")
        self.assertEqual(value["maintenance"], phase)
        self.assertIsNone(value["timer"])
        self.assertTrue(value["timer_error"])

    def test_history_is_bounded(self):
        # A dashboard left open cannot create an ever-growing metrics database.
        self.collector.history.extend({"ts": str(n)} for n in range(1000))
        with mock.patch.object(self.collector, "services", return_value={"items": []}), \
                mock.patch.object(self.collector, "maintenance", return_value={}), \
                mock.patch.object(self.collector, "containers", return_value={"status": "not_installed", "items": []}):
            result = self.collector.collect()
        self.assertEqual(len(result["history"]), 360)
        self.assertEqual(len(self.collector.history), 360)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def fixture_http(self, controls, token=None):
        cache = d.Snapshot()
        cache.publish({"actions": controls.describe()})
        server = d.DashboardServer(("127.0.0.1", 0), d.handler_for(cache, token, controls))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        def request(path="/api/action", method="POST", payload=None, headers=None):
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            body = json.dumps(payload) if isinstance(payload, dict) else payload
            if headers is None:
                headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                           "Content-Type": "application/json", "X-Action-Token": controls.nonce}
                if token is not None:
                    headers["Authorization"] = "Bearer " + token
            try:
                client.request(method, path, body=body, headers=headers)
                response = client.getresponse()
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
            finally:
                client.close()
        return server, request

    def test_control_http_gates_reject_cross_site_and_unconfirmed_requests(self):
        # A LAN viewer must not become a form/CSRF/arbitrary-method control proxy.
        calls = []
        class Controls:
            nonce = d.secrets.token_urlsafe(32)
            def execute(self, payload):
                calls.append(payload)
                return {"ok": True, "message": "fixture action"}
        controls = Controls()
        token = d.secrets.token_urlsafe(32)
        cache = d.Snapshot()
        cache.publish({"status": "healthy"})
        server = d.DashboardServer(("127.0.0.1", 0), d.handler_for(cache, token, controls))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            valid = {"Origin": origin, "Authorization": "Bearer " + token,
                     "X-Action-Token": controls.nonce, "Content-Type": "application/json"}
            body = json.dumps({"kind": "service", "scope": "user", "unit": "fixture.service", "action": "stop"})
            def request(headers, payload=body, method="POST", path="/api/action"):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    client.request(method, path, body=payload, headers=headers)
                    response = client.getresponse()
                    response.read()
                    return response.status
                finally:
                    client.close()
            invalid = [
                ({key: value for key, value in valid.items() if key != "Origin"}, 403),
                (dict(valid, Origin="https://foreign.invalid"), 403),
                (dict(valid, Host="foreign.invalid"), 403),
                (dict(valid, **{"Sec-Fetch-Site": "cross-site"}), 403),
                (dict(valid, **{"X-Action-Token": "invalid"}), 403),
                ({key: value for key, value in valid.items() if key != "Authorization"}, 401),
                (dict(valid, **{"Content-Type": "text/plain"}), 415),
                (dict(valid, **{"Content-Length": "4097"}), 413),
            ]
            for headers, status in invalid:
                with self.subTest(status=status, headers=list(headers)):
                    self.assertEqual(request(headers), status)
            self.assertEqual(request(valid, payload="{bad"), 400)
            self.assertEqual(request(valid, method="DELETE"), 405)
            self.assertEqual(request(valid, path="/api/other"), 405)
            self.assertEqual(calls, [])
            self.assertEqual(request(valid), 200)
            self.assertEqual(calls, [json.loads(body)])
            controls.nonce = d.secrets.token_urlsafe(32)
            self.assertEqual(request(valid), 403)
            server.RequestHandlerClass = d.handler_for(cache, token)
            self.assertEqual(request(dict(valid, **{"X-Action-Token": controls.nonce})), 403)
            self.assertEqual(len(calls), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_guarded_cleanup_real_endpoint_gates_dispatch_receipt_and_unknown_state(self):
        # Cross the real HTTP/Controller boundary with fake unit reads and a fake start only.
        actions = d.action_module()
        state = Path(self.temp.name) / "action-state"
        wake = mock.Mock()
        controls = actions.Controller(state, mock.Mock(), mock.Mock(), mock.Mock(), wake)
        token = d.secrets.token_urlsafe(32)
        cache = d.Snapshot()
        cache.publish({"actions": controls.describe()})
        server = d.DashboardServer(("127.0.0.1", 0), d.handler_for(cache, token, controls))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            valid = {"Origin": origin, "Authorization": "Bearer " + token,
                     "X-Action-Token": controls.nonce, "Content-Type": "application/json"}
            cleanup = {"kind": "cleanup", "action": "run"}
            def request(headers=valid, payload=None, duplicate_length=False):
                body = json.dumps(cleanup if payload is None else payload)
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    if duplicate_length:
                        client.putrequest("POST", "/api/action")
                        for key, value in headers.items():
                            client.putheader(key, value)
                        client.putheader("Content-Length", str(len(body)))
                        client.putheader("Content-Length", str(len(body)))
                        client.endheaders(body.encode())
                    else:
                        client.request("POST", "/api/action", body=body, headers=headers)
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()
            def dispatch(args, **kwargs):
                events = [json.loads(line) for line in (state / actions.AUDIT_NAME).read_text().splitlines()]
                self.assertEqual(events[-1]["event"], "started")
                self.assertEqual(events[-1]["target"], {"scope": "user", "unit": actions.CLEANUP_UNIT})
                self.assertEqual(args, ["systemctl", "--user", "start", "--no-block", "--", actions.CLEANUP_UNIT])
                self.assertEqual(kwargs["timeout"], 12)
                return mock.Mock(returncode=0)
            with mock.patch.object(actions, "user_unit_state", return_value=unit_state()) as read, \
                    mock.patch.object(actions.subprocess, "run", side_effect=dispatch) as run:
                for headers, status in (
                    ({key: value for key, value in valid.items() if key != "Origin"}, 403),
                    (dict(valid, Origin="https://foreign.invalid"), 403),
                    (dict(valid, Host="foreign.invalid"), 403),
                    (dict(valid, **{"Sec-Fetch-Site": "cross-site"}), 403),
                    (dict(valid, **{"X-Action-Token": "expired-fixture"}), 403),
                    ({key: value for key, value in valid.items() if key != "Authorization"}, 401),
                    (dict(valid, **{"Content-Type": "text/plain"}), 415),
                    (dict(valid, **{"Content-Length": "4097"}), 413),
                    (dict(valid, **{"Content-Length": "0"}), 413),
                    (dict(valid, **{"Content-Length": "bad"}), 400),
                    (dict(valid, **{"Transfer-Encoding": "chunked"}), 400),
                ):
                    with self.subTest(status=status, headers=list(headers)):
                        self.assertEqual(request(headers)[0], status)
                self.assertEqual(request(duplicate_length=True)[0], 400)
                for payload in (
                    dict(cleanup, unit=actions.CLEANUP_UNIT), dict(cleanup, path="/tmp"),
                    dict(cleanup, mode="force"), dict(cleanup, action="stop"),
                ):
                    self.assertEqual(request(payload=payload)[0], 400)
                read.assert_not_called()
                run.assert_not_called()
                self.assertFalse(state.exists())
                read.side_effect = [unit_state(), unit_state(active="activating", sub="start")]
                status, result = request()
                self.assertEqual(status, 200)
                self.assertIs(result["dispatch_only"], True)
                self.assertEqual(result["dispatch"]["status"], "accepted")
                self.assertEqual(result["observation"]["active"], "activating")
                self.assertRegex(result["observation"]["read_at"], r"Z$")
                self.assertEqual(run.call_count, 1)
                self.assertEqual(read.call_count, 2)
                wake.assert_called_once_with()
                read.side_effect = None
                read.return_value = unit_state(active="active", sub="running")
                # Trusted-LAN authentication policy remains tokenless, but still same-origin/nonce.
                server.RequestHandlerClass = d.handler_for(cache, None, controls)
                status, result = request({key: value for key, value in valid.items() if key != "Authorization"})
                self.assertEqual(status, 200)
                self.assertEqual(result["dispatch"]["status"], "already_running")
                self.assertIs(result["dispatch"]["dispatched"], False)
                self.assertEqual(run.call_count, 1)
                gap = unit_state(status="unknown", active=None, sub=None, exit_status=None,
                                 invocation_id=None, error="timeout", detail="Fixture state timeout.")
                read.side_effect = [unit_state(), gap]
                status, result = request()
                self.assertEqual((status, result["outcome"]), (504, "unknown"))
                self.assertIs(result["dispatch_only"], True)
                self.assertEqual(result["dispatch"]["status"], "accepted")
                self.assertEqual(result["observation"]["status"], "unknown")
                self.assertEqual(run.call_count, 2)
                self.assertEqual(wake.call_count, 3)
                events = (state / actions.AUDIT_NAME).read_text()
                self.assertNotIn(token, events)
                self.assertNotIn(controls.nonce, events)
                server.allowed_networks = [d.ipaddress.ip_network("192.168.1.0/24")]
                with self.assertRaises((OSError, http.client.HTTPException)):
                    request()
                self.assertEqual(run.call_count, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_cleanup_discovery_is_additive_and_keeps_read_only_defaults(self):
        # Clients discover new powers without losing any prior capability contract.
        output = subprocess.check_output(
            [sys.executable, "-B", str(ROOT / "scripts/server-dashboard.py"), "--version", "--json"],
            text=True, timeout=3,
        )
        value = json.loads(output)
        old = {"authenticated-http", "cached-snapshot", "container-inventory-all", "control-actions",
               "interactive-view", "lan-https", "loopback-only", "read-only", "snapshot-json",
               "systemd-services", "trusted-lan-readonly"}
        self.assertEqual(value["version"], "0.8.0")
        self.assertEqual(set(value["capabilities"]), old | {
            "guarded-cleanup", "cleanup-job-state", "action-history",
            "observed-action-state", "workload-recovery", "storage-scan",
        })
        self.assertEqual(value["capabilities"], sorted(value["capabilities"]))
        self.assertEqual(len(value["capabilities"]), len(set(value["capabilities"])))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.temp.name) / "defaults")}):
            self.assertFalse(d.server_settings(d.argparse.Namespace(port=None, interval=None))["actions"])

    def test_recovery_http_user_service_stop_start_restart_from_retained_identity(self):
        # Drive real HTTP/Controller/show/VERB/show, including a stopped unit disappearing from the capped list.
        actions, unit = d.action_module(), "fixture-worker.service"
        services = mock.Mock(return_value={"items": [{"unit": unit, "scope": "user"}]})
        controls = actions.Controller(Path(self.temp.name) / "service-actions", mock.Mock(),
                                      mock.Mock(), services, mock.Mock())
        _, request = self.fixture_http(controls)
        active = "active"
        def systemctl(args, **kwargs):
            nonlocal active
            self.assertEqual(args[:2], ["systemctl", "--user"])
            self.assertEqual(args[-2:], ["--", unit])
            if args[2] == "show":
                self.assertEqual(kwargs["timeout"], 2)
                raw = (f"Id={unit}\nLoadState=loaded\nActiveState={active}\n"
                       f"SubState={'running' if active == 'active' else 'dead'}\n"
                       "Result=success\nExecMainStatus=0\nInvocationID=\nCanStart=yes\nRefuseManualStart=no\n")
                return mock.Mock(returncode=0, stdout=raw)
            self.assertIn(args[2], {"stop", "start", "restart"})
            self.assertEqual(args, ["systemctl", "--user", args[2], "--", unit])
            self.assertEqual(kwargs, {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                    "stderr": subprocess.DEVNULL, "shell": False, "timeout": 12, "check": False})
            active = "inactive" if args[2] == "stop" else "active"
            services.return_value = {"items": [] if active == "inactive" else [{"unit": unit, "scope": "user"}],
                                     "truncated": True}
            return mock.Mock(returncode=0)
        ids = []
        with mock.patch.object(actions.subprocess, "run", side_effect=systemctl) as run:
            for verb in ("stop", "start", "restart", "stop"):
                status, result = request(payload={"kind": "service", "scope": "user", "unit": unit, "action": verb})
                self.assertEqual(status, 200)
                self.assertTrue(result["ok"])
                self.assertEqual(result["observation"]["state"], active)
                self.assertEqual(result["observation"]["unit"], unit)
                self.assertRegex(result["action_id"], r"^[0-9a-f]{32}$")
                ids.append(result["action_id"])
            self.assertEqual([call.args[0][2] for call in run.call_args_list],
                             [part for verb in ("stop", "start", "restart", "stop")
                              for part in ("show", verb, "show")])
            for _ in range(2):
                status, history = request("/api/actions", "GET")
                self.assertEqual(status, 200)
                self.assertEqual(len(history["events"]), 8)
                self.assertEqual({event["action_id"] for event in history["events"]}, set(ids))
            self.assertEqual(run.call_count, 12)  # History/refresh never replays a command.
        self.assertEqual(services.call_count, 4)

    def test_recovery_http_both_unix_engines_fixed_verbs_observe_exact_id_and_keep_volumes(self):
        # No host daemon/CLI is used: the actual HTTP -> Controller -> Unix requests cross a disposable peer.
        actions, identifier = d.action_module(), "a" * 64
        records, state = [], {"value": "created"}
        class Peer(d.BaseHTTPRequestHandler):
            def do_POST(self):
                records.append((self.command, self.path, self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                state["value"] = "exited" if self.path.endswith("/stop?t=10") else "running"
                self.send_response(204)
                self.end_headers()
            def do_DELETE(self):
                records.append((self.command, self.path, b""))
                state["value"] = "absent"
                self.send_response(204)
                self.end_headers()
            def do_GET(self):
                records.append((self.command, self.path, b""))
                # Exact 404 needs no body: the reader correctly discards error
                # metadata and may close before a fake peer writes any of it.
                raw = (b"" if state["value"] == "absent" else json.dumps({
                    "Id": identifier, "State": {"Status": state["value"]},
                    "Config": {"Labels": {}, "Env": ["secret-env"], "Cmd": ["secret-args"]},
                }).encode())
                self.send_response(404 if state["value"] == "absent" else 200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)
            def log_message(self, *args):
                pass
        path = Path(self.temp.name) / "recovery.sock"
        peer = socketserver.UnixStreamServer(str(path), Peer)
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        def close():
            peer.shutdown()
            peer.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        inventory = mock.Mock(side_effect=lambda endpoint: {"items": [
            {"id": identifier, "State": state["value"], "protected": False}]})
        endpoint = mock.Mock(return_value=path)
        controls = actions.Controller(Path(self.temp.name) / "container-actions", endpoint, inventory,
                                      mock.Mock(), mock.Mock())
        _, request = self.fixture_http(controls)
        ids = []
        for engine in ("docker", "podman"):
            state["value"] = "created"
            for verb, method, suffix in (
                ("start", "POST", "/start"), ("restart", "POST", "/restart?t=10"),
                ("stop", "POST", "/stop?t=10"), ("remove", "DELETE", "?v=false&force=false"),
            ):
                status, result = request(payload={"kind": "container", "engine": engine,
                                                  "id": identifier, "action": verb})
                self.assertEqual(status, 200)
                self.assertEqual(result["observation"]["state"], state["value"])
                self.assertEqual(result["observation"]["id"], identifier)
                self.assertNotIn("secret-", json.dumps(result))
                self.assertEqual(records[-2:], [
                    (method, f"/containers/{identifier}{suffix}", b""),
                    ("GET", f"/containers/{identifier}/json", b""),
                ])
                ids.append(result["action_id"])
        status, history = request("/api/actions", "GET")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["events"]), 16)
        self.assertEqual({event["action_id"] for event in history["events"]}, set(ids))
        self.assertNotIn("secret-", json.dumps(history))
        self.assertEqual((inventory.call_count, endpoint.call_count), (8, 8))

    def test_action_history_get_auth_origin_optin_quality_and_no_side_effects(self):
        # The new fixed GET shares GET authority, needs opt-in but no nonce, and cannot create/repair history.
        actions = d.action_module()
        controls = actions.Controller(Path(self.temp.name) / "history-only", mock.Mock(),
                                      mock.Mock(), mock.Mock(), mock.Mock())
        token = d.secrets.token_urlsafe(32)
        server, request = self.fixture_http(controls, token)
        auth = {"Authorization": "Bearer " + token}
        with mock.patch.object(controls, "history", wraps=controls.history) as read, \
                mock.patch.object(Path, "mkdir") as mkdir, mock.patch.object(actions.os, "write") as write, \
                mock.patch.object(actions.fcntl, "flock") as flock:
            self.assertEqual(request("/api/actions", "GET", headers={})[0], 401)
            for headers in (dict(auth, Host="foreign.invalid"), dict(auth, Origin="https://foreign.invalid"),
                            dict(auth, **{"Sec-Fetch-Site": "cross-site"})):
                self.assertEqual(request("/api/actions", "GET", headers=headers)[0], 403)
            self.assertEqual(request("/api/actions?limit=100", "GET", headers=auth)[0], 404)
            self.assertEqual(request("/api/actions/other", "GET", headers=auth)[0], 404)
            self.assertEqual(request("/api/actions", "POST", payload={}, headers=auth)[0], 405)
            read.assert_not_called()
            status, history = request("/api/actions", "GET", headers=auth)
            self.assertEqual(status, 200)
            self.assertEqual(history["status"], "no_history_yet")
            self.assertEqual(history["events"], [])
            self.assertEqual(request("/api/actions", "HEAD", headers=auth), (200, None))
            self.assertFalse(controls.state_dir.exists())
            mkdir.assert_not_called()
            write.assert_not_called()
            flock.assert_not_called()
            server.RequestHandlerClass = d.handler_for(d.Snapshot(), token)
            self.assertEqual(request("/api/actions", "GET", headers=auth)[0], 403)
            self.assertEqual(read.call_count, 2)
            server.RequestHandlerClass = d.handler_for(d.Snapshot(), None, controls)
            self.assertEqual(request("/api/actions", "GET", headers={})[0], 200)
        controls.state_dir.mkdir(mode=0o700)
        path = controls.state_dir / actions.AUDIT_NAME
        path.touch(mode=0o600)
        path.write_bytes(b"\xff\n{bad}\npartial")
        status, history = request("/api/actions", "GET", headers={})
        self.assertEqual((status, history["status"]), (200, "partial"))
        self.assertEqual(history["quality"]["invalid_lines"], 2)
        self.assertTrue(history["quality"]["incomplete_tail"])
        self.assertNotIn(token, json.dumps(history))
        self.assertNotIn(controls.nonce, json.dumps(history))
        path.chmod(0o644)
        self.assertEqual(request("/api/actions", "GET", headers={})[1]["status"], "unavailable")
        controls._services.assert_not_called()
        controls._container_socket.assert_not_called()

    def test_recovery_http_malformed_scope_decode_gaps_and_post_read_are_structured(self):
        # Fresh callback/decoder failures stay JSON; failed post-reading does not turn a successful command into failure.
        actions, unit = d.action_module(), "fixture-worker.service"
        services = mock.Mock(return_value={"items": [{"unit": unit, "scope": "user"}]})
        controls = actions.Controller(Path(self.temp.name) / "structured-actions", mock.Mock(),
                                      mock.Mock(), services, mock.Mock())
        _, request = self.fixture_http(controls)
        payload = {"kind": "service", "scope": "user", "unit": unit, "action": "start"}
        good = unit_state(unit=unit, id=unit)
        with mock.patch.object(actions.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(actions, "user_unit_state", return_value=good) as read:
            for raw in (b"\xff", b"[" * 1100 + b"]" * 1100):
                self.assertEqual(request(payload=raw)[0], 400)
            for scope in (None, True, [], "system"):
                services.return_value = {"items": [{"unit": unit, "scope": scope}]}
                self.assertEqual(request(payload=payload)[0], 403)
            read.assert_not_called()
            run.assert_not_called()
            services.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "secret-UTF")
            status, result = request(payload=payload)
            self.assertEqual(status, 503)
            self.assertNotIn("secret-", json.dumps(result))
            services.side_effect = None
            services.return_value = {"items": []}
            read.return_value = unit_state(unit=unit, id="agent-toolkit-dashboard.service", status="unavailable")
            self.assertEqual(request(payload=payload)[0], 403)
            run.assert_not_called()
            read.side_effect = [good, unit_state(unit=unit, id=None, status="unknown",
                                                error="malformed", detail="Fixture text decode gap.")]
            status, result = request(payload=payload)
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(result["observation"]["state"], "unknown")
            self.assertEqual(result["observation"]["error"], "malformed")
            run.assert_called_once_with(
                ["systemctl", "--user", "start", "--", unit], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, timeout=12, check=False)
            events = [json.loads(line) for line in (controls.state_dir / actions.AUDIT_NAME).read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "succeeded")
            self.assertEqual(events[-1]["action_id"], result["action_id"])

    def test_cleanup_real_signal_exit_returns_http_unknown_with_observation(self):
        # Cross actual HTTP/Controller and original subprocess runner; never launch a host unit.
        actions, original_run = d.action_module(), subprocess.run
        state = Path(self.temp.name) / "signal-actions"
        wake = mock.Mock()
        controls = actions.Controller(state, mock.Mock(), mock.Mock(), mock.Mock(), wake)
        server = d.DashboardServer(("127.0.0.1", 0), d.handler_for(d.Snapshot(), None, controls))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                       "Content-Type": "application/json", "X-Action-Token": controls.nonce}
            for number in (signal.SIGTERM, signal.SIGKILL):
                with self.subTest(signal=number):
                    launched, starts = False, []
                    def systemctl(args, **kwargs):
                        nonlocal launched
                        self.assertEqual(args[:2], ["systemctl", "--user"])
                        self.assertEqual(args[-2:], ["--", actions.CLEANUP_UNIT])
                        if args[2] == "show":
                            raw = (
                                f"Id={actions.CLEANUP_UNIT}\nLoadState=loaded\n"
                                f"ActiveState={'activating' if launched else 'inactive'}\n"
                                f"SubState={'start' if launched else 'dead'}\nResult=success\n"
                                f"ExecMainStatus=0\nInvocationID={INVOCATION if launched else ''}\n"
                                "CanStart=yes\nRefuseManualStart=no\n"
                            ).encode()
                            return original_run(
                                [sys.executable, "-B", "-c",
                                 f"import sys; sys.stdout.buffer.write({raw!r})"],
                                **kwargs,
                            )
                        self.assertEqual(args, ["systemctl", "--user", "start", "--no-block",
                                                "--", actions.CLEANUP_UNIT])
                        self.assertEqual(json.loads((state / actions.AUDIT_NAME).read_text().splitlines()[-1])["event"],
                                         "started")
                        launched = True
                        starts.append(args)
                        result = original_run(
                            [sys.executable, "-B", "-c",
                             f"import os,signal; os.kill(os.getpid(), signal.{number.name})"], **kwargs,
                        )
                        self.assertEqual(result.returncode, -number)
                        return result
                    wake.reset_mock()
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        with mock.patch.object(actions.subprocess, "run", side_effect=systemctl) as run:
                            client.request("POST", "/api/action", body='{"kind":"cleanup","action":"run"}',
                                           headers=headers)
                            response = client.getresponse()
                            body = json.loads(response.read())
                    finally:
                        client.close()
                    self.assertEqual((response.status, body["outcome"]), (502, "unknown"))
                    self.assertIs(body["ok"], False)
                    self.assertIs(body["dispatch_only"], True)
                    self.assertEqual(body["dispatch"]["status"], "unknown")
                    self.assertIs(body["dispatch"]["dispatched"], True)
                    self.assertEqual(body["observation"]["active"], "activating")
                    self.assertEqual(body["observation"]["invocation_id"], INVOCATION)
                    self.assertEqual(len(starts), 1)
                    self.assertEqual([call.args[0][2] for call in run.call_args_list], ["show", "start", "show"])
                    events = [json.loads(line) for line in (state / actions.AUDIT_NAME).read_text().splitlines()]
                    self.assertEqual([event["event"] for event in events[-2:]], ["started", "unknown"])
                    self.assertEqual(events[-1]["observation"], body["observation"])
                    self.assertNotIn("rejected", json.dumps(events))
                    wake.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_cleanup_real_decode_failure_returns_json_before_and_after_launch(self):
        # Byte0xff comes from real child stdout through original text=True decoding, not a fake exception.
        actions, original_run = d.action_module(), subprocess.run
        state = Path(self.temp.name) / "decode-actions"
        wake = mock.Mock()
        controls = actions.Controller(state, mock.Mock(), mock.Mock(), mock.Mock(), wake)
        server = d.DashboardServer(("127.0.0.1", 0), d.handler_for(d.Snapshot(), None, controls))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                       "Content-Type": "application/json", "X-Action-Token": controls.nonce}
            for phase in ("prelaunch", "postlaunch"):
                with self.subTest(phase=phase):
                    launched, starts = False, []
                    def systemctl(args, **kwargs):
                        nonlocal launched
                        self.assertEqual(args[:2], ["systemctl", "--user"])
                        self.assertEqual(args[-2:], ["--", actions.CLEANUP_UNIT])
                        if args[2] == "show":
                            self.assertIs(kwargs["text"], True)
                            self.assertIs(kwargs["capture_output"], True)
                            self.assertEqual(kwargs["timeout"], 2)
                            raw = (
                                f"Id={actions.CLEANUP_UNIT}\nLoadState=loaded\nActiveState=inactive\n"
                                "SubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\n"
                                "CanStart=yes\nRefuseManualStart=no\n"
                            ).encode()
                            code = ("import os; os.write(1, bytes([255]))" if phase == "prelaunch" or launched
                                    else f"import sys; sys.stdout.buffer.write({raw!r})")
                            return original_run(
                                [sys.executable, "-B", "-c", code], **kwargs,
                            )
                        self.assertEqual(args, ["systemctl", "--user", "start", "--no-block",
                                                "--", actions.CLEANUP_UNIT])
                        self.assertEqual(json.loads((state / actions.AUDIT_NAME).read_text().splitlines()[-1])["event"],
                                         "started")
                        starts.append(args)
                        launched = True
                        return original_run([sys.executable, "-B", "-c", "pass"], **kwargs)
                    wake.reset_mock()
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        with mock.patch.object(actions.subprocess, "run", side_effect=systemctl) as run:
                            client.request("POST", "/api/action", body='{"kind":"cleanup","action":"run"}',
                                           headers=headers)
                            response = client.getresponse()
                            body = json.loads(response.read())
                    finally:
                        client.close()
                    self.assertEqual(response.status, 503)
                    self.assertIs(body["ok"], False)
                    self.assertIs(body["dispatch_only"], True)
                    self.assertEqual(body["outcome"], "failed" if phase == "prelaunch" else "unknown")
                    self.assertEqual(body["dispatch"]["status"],
                                     "not_dispatched" if phase == "prelaunch" else "accepted")
                    self.assertIs(body["dispatch"]["dispatched"], phase == "postlaunch")
                    self.assertEqual((body["observation"]["status"], body["observation"]["error"]),
                                     ("unknown", "malformed"))
                    self.assertRegex(body["observation"]["read_at"], r"Z$")
                    self.assertIsNone(body["observation"]["exit_status"])
                    self.assertIn("decod", body["observation"]["detail"].lower())
                    self.assertEqual(len(starts), 0 if phase == "prelaunch" else 1)
                    self.assertEqual([call.args[0][2] for call in run.call_args_list],
                                     ["show"] if phase == "prelaunch" else ["show", "start", "show"])
                    events = [json.loads(line) for line in (state / actions.AUDIT_NAME).read_text().splitlines()]
                    self.assertEqual([event["event"] for event in events[-2:]],
                                     ["started", "failed" if phase == "prelaunch" else "unknown"])
                    self.assertEqual(events[-1]["observation"], body["observation"])
                    self.assertNotIn("0xff", json.dumps(events))
                    wake.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def tls_pair(self):
        cert, key = Path(self.temp.name) / "server.crt", Path(self.temp.name) / "server.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=dashboard-test", "-addext", "subjectAltName=IP:127.0.0.1",
             "-keyout", str(key), "-out", str(cert)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15,
        )
        key.chmod(0o600)
        return cert, key

    def test_lan_profile_fails_closed_and_url_uses_same_settings(self):
        # A phone must not receive a plaintext, wildcard or stale localhost link.
        cert, key = self.tls_pair()
        config = Path(self.temp.name) / "config"
        profile = config / "agent-toolkit/dashboard.json"
        profile.parent.mkdir(parents=True)
        valid = {"bind": "192.168.1.10", "allowed_networks": ["192.168.1.0/24"],
                 "tls_cert": str(cert), "tls_key": str(key)}
        args = d.argparse.Namespace(port=None, interval=None)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            self.assertEqual(d.server_settings(args)["bind"], "127.0.0.1")
            for invalid in (
                dict(valid, bind="0.0.0.0"), dict(valid, bind="8.8.8.8"),
                dict(valid, bind="::"), dict(valid, allowed_networks=["0.0.0.0/0"]),
                dict(valid, allowed_networks=[]), dict(valid, tls_key=None),
                dict(valid, tls_cert=None, tls_key=None),
                dict(valid, unexpected=True), {"tls_cert": False, "tls_key": False},
                {"trusted_lan": True}, dict(valid, trusted_lan="yes"),
                dict(valid, actions="true"),
                dict(valid, trusted_lan=True, tls_cert=None, tls_key=None),
            ):
                profile.write_text(json.dumps(invalid))
                with self.subTest(invalid=list(invalid)):
                    with self.assertRaises((ValueError, OSError)):
                        d.server_settings(args)
            profile.write_text(json.dumps(valid))
            key.chmod(0o644)
            with self.assertRaises(ValueError):
                d.server_settings(args)
            key.chmod(0o600)
            settings = d.server_settings(args)
            self.assertEqual(settings["bind"], valid["bind"])
            d.initialize_token(d.token_path())
            output = subprocess.check_output(
                [str(ROOT / "scripts/server-dashboard.py"), "--url"],
                env=dict(os.environ), text=True,
            )
            self.assertTrue(output.startswith("https://192.168.1.10:8787/#token="))
            profile.write_text(json.dumps(dict(valid, trusted_lan=True)))
            d.token_path().unlink()
            output = subprocess.check_output(
                [str(ROOT / "scripts/server-dashboard.py"), "--url"],
                env=dict(os.environ), text=True,
            )
            self.assertEqual(output.strip(), "https://192.168.1.10:8787/")

    def test_real_https_auth_origin_acl_and_stalled_handshake(self):
        # TLS negotiation belongs in bounded workers, never the accept loop.
        cert, key = self.tls_pair()
        context = d.tls_context({"tls_cert": str(cert), "tls_key": str(key)})
        client_context = ssl.create_default_context(cafile=str(cert))
        token = d.secrets.token_urlsafe(32)
        cache = d.Snapshot()
        cache.publish({"private": "fixture"})
        server = d.DashboardServer(("127.0.0.1", 0), d.handler_for(cache, token), context)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        stalled = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
        try:
            def request(headers=None):
                client = http.client.HTTPSConnection(
                    "127.0.0.1", server.server_port, context=client_context, timeout=2,
                )
                try:
                    client.request("GET", "/api/snapshot", headers=headers or {})
                    response = client.getresponse()
                    response.read()
                    return response.status
                finally:
                    client.close()
            self.assertEqual(request(), 401)
            auth = {"Authorization": "Bearer " + token}
            self.assertEqual(request(auth), 200)
            self.assertEqual(request(dict(auth, Host="foreign.invalid")), 403)
            self.assertEqual(request(dict(auth, Origin=f"http://127.0.0.1:{server.server_port}")), 403)
            self.assertEqual(request(dict(auth, Origin=f"https://127.0.0.1:{server.server_port}")), 200)
            server.RequestHandlerClass = d.handler_for(cache, None)
            self.assertEqual(request(), 200)
            self.assertEqual(request({"Host": "foreign.invalid"}), 403)
            self.assertEqual(request({"Origin": "https://foreign.invalid"}), 403)
            server.allowed_networks = [d.ipaddress.ip_network("192.168.1.0/24")]
            with self.assertRaises((OSError, http.client.HTTPException)):
                request(auth)
        finally:
            stalled.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_published_install_and_diagnostics_with_minimal_path(self):
        # Published commands must work from a fresh shell and not stop monitoring.
        home = Path(self.temp.name) / "home"
        shim = home / "shims"
        shim.mkdir(parents=True)
        for name in ("systemctl", "journalctl"):
            target = shim / name
            target.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/control-calls"\n')
            target.chmod(0o755)
        env = dict(os.environ, HOME=str(home), PATH=f"{shim}:/usr/bin:/bin",
                   XDG_CONFIG_HOME=str(home / ".config"),
                   XDG_STATE_HOME=str(home / ".local/state"), BASH_ENV="")
        blocks = re.findall(r"```bash\n(.*?)\n```", (ROOT / "docs/dashboard.md").read_text(), re.S)
        diagnostics = next(block for block in blocks if "--version --json" in block)
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", blocks[0] + "\n" + diagnostics],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / ".config/agent-toolkit/dashboard-token").is_file())
        self.assertNotIn("disable", (home / "control-calls").read_text())

    def test_private_token_idempotence_and_symlink_rejection(self):
        # Never replace an existing secret or follow a token symlink.
        path = Path(self.temp.name) / "config/token"
        d.initialize_token(path)
        before = path.read_bytes()
        d.initialize_token(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            d.read_token(path)
        path.chmod(0o600)
        link = path.with_name("link")
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            d.read_token(link)

    def test_container_socket_reads_are_bounded_and_do_not_leak_commands(self):
        # Hardened services cannot rely on Snap/setuid or rootless namespace setup.
        requests = []
        class Endpoint(d.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path))
                payload = json.dumps([{"Id": "123456789012345", "Names": ["/worker"],
                                       "Image": "test", "Status": "Created", "State": "created",
                                       "Command": "private argument, never expose"}]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        path = Path(self.temp.name) / "engine.sock"
        with socketserver.UnixStreamServer(str(path), Endpoint) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = d.container_inventory(path)
                self.assertEqual(result["items"][0]["Names"], "worker")
                self.assertEqual(result["items"][0]["State"], "created")
                self.assertNotIn("Command", result["items"][0])
                self.assertEqual(requests, [("GET", "/containers/json?all=1")])
            finally:
                server.shutdown()
                thread.join(timeout=3)
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "tcp://example.invalid:2375"}):
            with self.assertRaises(ValueError):
                d.container_socket("docker")
        with mock.patch.object(d, "container_socket", return_value=None), \
                mock.patch.object(d.shutil, "which", return_value="/usr/bin/podman"):
            self.assertEqual(d.Collector().containers("podman")["status"], "socket_unavailable")

    def test_real_http_access_boundaries_and_cached_snapshot(self):
        # Exercise the actual HTTP handler, not a proxy that bypasses auth or routing.
        token = d.secrets.token_urlsafe(32)
        cache = d.Snapshot()
        cache.publish({"status": "healthy", "private": "fixture"})
        server = d.DashboardServer(("127.0.0.1", 0), d.handler_for(cache, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(path="/api/snapshot", method="GET", headers=None):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    client.request(method, path, headers=headers or {})
                    response = client.getresponse()
                    return response.status, response.read(), dict(response.getheaders())
                finally:
                    client.close()

            auth = {"Authorization": "Bearer " + token}
            self.assertEqual(request()[0], 401)
            self.assertEqual(request(headers={"Authorization": "Bearer wrong"})[0], 401)
            self.assertEqual(request(headers={"Authorization": "Bearer \u00e9"})[0], 401)
            first = request(headers=auth)
            self.assertEqual(first[0], 200)
            self.assertEqual(json.loads(first[1])["private"], "fixture")
            self.assertEqual(request(headers=auth)[1], first[1])
            self.assertEqual(first[2]["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", first[2]["Content-Security-Policy"])
            for headers in (
                dict(auth, Host="foreign.invalid"),
                dict(auth, Origin="https://foreign.invalid"),
                dict(auth, **{"Sec-Fetch-Site": "cross-site"}),
            ):
                with self.subTest(headers=list(headers)):
                    self.assertEqual(request(headers=headers)[0], 403)
            for path in ("/../AGENTS.md", "/%2e%2e/AGENTS.md", "/api/snapshot?token=wrong"):
                self.assertEqual(request(path, headers=auth)[0], 404)
            for method in ("POST", "DELETE", "PUT", "PATCH"):
                self.assertEqual(request(method=method, headers=auth)[0], 405)
            self.assertEqual(request("/", "HEAD")[1], b"")
            self.assertIn(b"Server observatory", request("/")[1])
            cache.body = None
            self.assertEqual(request(headers=auth)[0], 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class StorageAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dashboard-storage-http-")
        self.addCleanup(self.temp.cleanup)
        self.roots = {name: Path(self.temp.name) / name for name in ("home", "root", "tmp")}
        for path in self.roots.values():
            path.mkdir()
        self.storage = d.storage_module()
        self.scanner = self.storage.StorageScanner(enabled=True, roots=self.roots, wall_seconds=2)
        self.addCleanup(self.scanner.close)
        self.controls = mock.Mock(nonce="fixture-storage-nonce")
        self.token = "fixture-private-access-" + "a" * 40
        self.cache = d.Snapshot()
        self.cache.publish({"status": "healthy", "actions": {"enabled": True, "nonce": self.controls.nonce}})
        self.server = d.DashboardServer(("127.0.0.1", 0),
                                       d.handler_for(self.cache, self.token, self.controls, self.scanner))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_http)
        self.headers = {"Authorization": "Bearer " + self.token,
                        "Origin": f"http://127.0.0.1:{self.server.server_port}",
                        "Content-Type": "application/json", "X-Action-Token": self.controls.nonce}

    def close_http(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, path="/api/storage-scan", method="GET", payload=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        body = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
        try:
            client.request(method, path, body=body, headers=self.headers if headers is None else headers)
            response = client.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, dict(response.getheaders())
        finally:
            client.close()

    def wait_job(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, value, _ = self.request()
            self.assertEqual(status, 200)
            if value["job"]["state"] in {"complete", "partial", "failed", "cancelled", "timed_out"}:
                return value["job"]
            time.sleep(0.005)
        self.fail("owned HTTP fixture failed to finalize")

    def test_storage_discovery_cached_get_head_and_sampler_never_spawn(self):
        # The original handler serves cheap cached metadata, never a scan on GET/HEAD/snapshot.
        with mock.patch.object(self.storage.subprocess, "Popen") as launch:
            for method in ("GET", "HEAD"):
                status, value, headers = self.request(method=method)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                if method == "GET":
                    self.assertIsNone(value["job"])
                    self.assertTrue(value["supported"])
                    self.assertEqual(value["capabilities"], ["storage-scan"])
                    self.assertEqual(value["api_version"], 1)
            status, snapshot, _ = self.request("/api/snapshot")
            self.assertEqual(status, 200)
            self.assertTrue(snapshot["storage_scan"]["supported"])
            self.assertNotIn("job", snapshot["storage_scan"])
            self.assertEqual([root["id"] for root in snapshot["storage_scan"]["roots"]], ["home", "root", "tmp"])
            self.assertIsNone(self.scanner._worker)
            launch.assert_not_called()
        self.controls.execute.assert_not_called()
        self.server.RequestHandlerClass = d.handler_for(self.cache, self.token, self.controls)
        self.assertEqual(self.request()[0], 404)
        self.assertNotIn("storage_scan", self.request("/api/snapshot")[1])

    def test_storage_post_original_shared_intake_gates_prevent_spawn(self):
        # Storage must cross the same auth/Origin/opt-in/nonce/type/length/JSON prefix as /api/action.
        valid = self.headers
        bad = [
            ({key: value for key, value in valid.items() if key != "Origin"}, 403),
            (dict(valid, Origin="https://foreign.invalid"), 403),
            (dict(valid, Host="foreign.invalid"), 403),
            (dict(valid, **{"Sec-Fetch-Site": "cross-site"}), 403),
            ({key: value for key, value in valid.items() if key != "Authorization"}, 401),
            (dict(valid, **{"X-Action-Token": "expired"}), 403),
            (dict(valid, **{"Content-Type": "text/plain"}), 415),
            (dict(valid, **{"Content-Length": "4097"}), 413),
            (dict(valid, **{"Content-Length": "0"}), 413),
            (dict(valid, **{"Content-Length": "not-decimal"}), 400),
            (dict(valid, **{"Transfer-Encoding": "chunked", "Content-Length": "2"}), 400),
        ]
        with mock.patch.object(self.storage.subprocess, "Popen") as launch:
            for route in ("/api/storage-scan/start", "/api/storage-scan/cancel"):
                for headers, expected in bad:
                    with self.subTest(route=route, expected=expected):
                        self.assertEqual(self.request(route, "POST", {"root": "home"}, headers)[0], expected)
                for payload in ("{bad", "[]", '{"root":"home","path":"/etc"}', '{"command":"du"}'):
                    self.assertEqual(self.request(route, "POST", payload)[0], 400)
            client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                client.putrequest("POST", "/api/storage-scan/start")
                for key, value in valid.items():
                    client.putheader(key, value)
                client.putheader("Content-Length", "2")
                client.putheader("Content-Length", "2")
                client.endheaders(b"{}")
                response = client.getresponse()
                response.read()
                self.assertEqual(response.status, 400)
            finally:
                client.close()
            self.server.RequestHandlerClass = d.handler_for(self.cache, self.token, None, self.scanner)
            self.assertEqual(self.request("/api/storage-scan/start", "POST", {"root": "home"})[0], 403)
            self.server.RequestHandlerClass = d.handler_for(self.cache, self.token, self.controls, self.scanner)
            self.scanner.enabled = False
            self.assertEqual(self.request("/api/storage-scan/start", "POST", {"root": "home"})[0], 403)
            launch.assert_not_called()
        self.controls.execute.assert_not_called()

    def test_storage_real_http_start_drill_stale_ids_and_action_separation(self):
        # Nonempty real HTTP requests must reach original du; navigation authority comes only from cached IDs.
        folder = self.roots["home"] / "folder"
        (folder / "nested").mkdir(parents=True)
        (folder / "nested/file").write_bytes(b"x" * 9000)
        self.assertEqual(self.request("/api/storage-scan/start", "POST", {"root": "home"})[0], 202)
        first = self.wait_job()
        self.assertEqual(first["state"], "complete")
        self.assertGreater(first["root_bytes"], 9000)
        child = first["directories"][0]
        self.assertEqual(self.request("/api/storage-scan/start", "POST", {
            "job_id": first["id"], "directory_id": child["id"],
        })[0], 202)
        second = self.wait_job()
        self.assertEqual([crumb["name"] for crumb in second["breadcrumbs"]], ["Home", "folder"])
        with mock.patch.object(self.storage.subprocess, "Popen") as launch:
            self.assertEqual(self.request("/api/storage-scan/start", "POST", {
                "job_id": first["id"], "directory_id": child["id"],
            })[0], 409)
            folder.rename(folder.with_name("old"))
            folder.mkdir()
            self.assertEqual(self.request("/api/storage-scan/start", "POST", {
                "job_id": second["id"], "directory_id": second["directory_id"],
            })[0], 409)
            launch.assert_not_called()
        self.controls.execute.assert_not_called()
        self.assertEqual(self.request("/api/storage-scan?root=home")[0], 404)
        self.assertEqual(self.request("/api/storage-scan/start")[0], 404)
        self.assertEqual(self.request("/api/storage-scan/start", "DELETE")[0], 405)

    def test_storage_real_delayed_http_responsiveness_cancel_and_no_journal_or_log_payload(self):
        # Actual delayed Popen, not CompletedProcess, proves a prompt POST and independent ordinary HTTP reads.
        original = subprocess.Popen
        children = []

        def launch(argv, **kwargs):
            child = original([sys.executable, "-B", "-c", "import time; time.sleep(10)"], **kwargs)
            children.append(child)
            return child

        with mock.patch.object(self.storage.subprocess, "Popen", side_effect=launch), \
                mock.patch.object(d.logging, "info") as log:
            before = time.monotonic()
            status, value, _ = self.request("/api/storage-scan/start", "POST", {"root": "tmp"})
            self.assertEqual(status, 202)
            self.assertLess(time.monotonic() - before, 0.3)
            job_id = value["job"]["id"]
            before = time.monotonic()
            self.assertEqual(self.request("/api/snapshot")[0], 200)
            self.assertEqual(self.request()[0], 200)
            self.assertLess(time.monotonic() - before, 0.3)
            self.assertEqual(self.request("/api/storage-scan/start", "POST", {"root": "home"})[0], 409)
            self.assertEqual(self.request("/api/storage-scan/cancel", "POST", {"job_id": "f" * 32})[0], 409)
            self.assertEqual(self.request("/api/storage-scan/cancel", "POST", {"job_id": job_id})[0], 202)
            self.assertEqual(self.wait_job()["state"], "cancelled")
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].returncode)
            self.assertNotIn(self.controls.nonce, str(log.call_args_list))
            self.assertNotIn(job_id, str(log.call_args_list))
            self.assertNotIn(self.token, str(log.call_args_list))
        self.controls.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
