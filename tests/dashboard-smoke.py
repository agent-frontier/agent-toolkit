#!/usr/bin/env python3
"""Focused collector, access-boundary and cached-HTTP regressions."""

import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("dashboard", ROOT / "scripts/server-dashboard.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
