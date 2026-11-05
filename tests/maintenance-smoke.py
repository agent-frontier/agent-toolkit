#!/usr/bin/env python3
"""Exercise safe recurring maintenance against isolated caches and a real Git server."""

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("maintenance", ROOT / "scripts/janitor-maintenance.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class Quiet:
    unreadable_owned = False

    def busy(self, path, *, cache=False):
        return False


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maintenance-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.policy = self.base / "policy.json"
        self.settings = {
            "go_cache_dirs": [], "session_roots": [], "archive_roots": [],
            "worktree_roots": [], "archive_catalogs": [],
            "shared_cache_budget_gib": 1, "session_cache_budget_gib": 1,
            "total_cache_budget_gib": 2, "cache_idle_seconds": 300,
            "artifact_idle_seconds": 3600,
        }
        self.save_policy()
        self.args = argparse.Namespace(
            mode="run", policy=str(self.policy), state_dir=str(self.state),
            cron_lock=str(self.base / "cron/lock"), cooldown=900, cooldown_remaining=0,
        )

    def save_policy(self):
        self.policy.write_text(json.dumps(self.settings))

    def call(self, *, mode="run", activity=None):
        self.args.mode = mode
        with patch.object(m, "Activity", return_value=activity or Quiet()):
            return m.main(self.args)

    def old(self, path):
        before = time.time() - 7200
        for folder, dirs, files in os.walk(path, topdown=False):
            for name in files + dirs:
                os.utime(Path(folder) / name, (before, before))
            os.utime(folder, (before, before))

    def cache(self):
        cache = self.base / "cache"
        (cache / "00").mkdir(parents=True)
        (cache / "README").write_text("This directory holds cached build artifacts from the Go build system.\n")
        (cache / "trim.txt").write_text("0\n")
        (cache / "00/payload-d").write_bytes(b"x" * 2 * 1024**2)
        self.old(cache)
        self.settings["go_cache_dirs"] = [str(cache)]
        self.save_policy()
        return cache

    @contextlib.contextmanager
    def cache_tools(self, cache, *, fail=False):
        tool = self.base / "go"
        tool.write_text(
            "#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\n"
            "assert sys.argv[1:] == ['clean','-cache','-testcache']\n"
            + ("sys.exit(23)\n" if fail else "Path(os.environ['GOCACHE'],'00/payload-d').unlink()\n")
        )
        tool.chmod(0o755)
        original_size, original_which = m.size, shutil.which
        with patch.object(m, "size", side_effect=lambda p, **kw: (
            2 * m.GIB if p == cache and (cache / "00/payload-d").exists() else original_size(p, **kw)
        )), patch.object(m.shutil, "which", side_effect=lambda name: (
            str(tool) if name == "go" else original_which(name)
        )):
            yield

    def repository(self):
        root = self.base / "projects/app"
        root.mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()
        git("init", "--quiet", "-b", "main")
        git("config", "user.name", "Maintenance fixture")
        git("config", "user.email", "fixture@example.invalid")
        (root / "source.txt").write_text("keep this source\n")
        (root / "package.json").write_text('{"private":true}\n')
        (root / ".gitignore").write_text("node_modules/\ncaptures/\n")
        git("add", ".")
        git("commit", "--quiet", "-m", "fixture")
        head = git("rev-parse", "HEAD")
        remotes = self.base / "remotes"
        remote = remotes / "app.git"
        remotes.mkdir()
        subprocess.check_call(["git", "init", "--bare", "--quiet", str(remote)])
        subprocess.check_call(["git", "--git-dir=" + str(remote), "fetch", "--quiet",
                               str(root), "main:main"])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(
            ["git", "daemon", "--reuseaddr", "--export-all", "--listen=127.0.0.1",
             "--port=" + str(port), "--base-path=" + str(remotes)],
            cwd=remotes, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        def stop():
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=5)
        self.addCleanup(stop)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            self.fail("fixture Git server did not start")
        url = f"git://127.0.0.1:{port}/app.git"
        target = root / "node_modules"
        target.mkdir()
        (target / "compiled.bin").write_bytes(b"x" * 2 * 1024**2)
        self.old(target)
        (root / "captures").mkdir()
        (root / "captures/keep.bin").write_bytes(b"personal evidence")
        catalog = self.base / "archives.json"
        record = {"path": str(root), "status": "archived-verified",
                  "snapshot": head, "remote": url, "ref": "refs/heads/main"}
        catalog.write_text(json.dumps({"projects": [record]}))
        self.settings.update(archive_roots=[str(root.parent)], archive_catalogs=[str(catalog)])
        self.save_policy()
        return root, target, head, record, catalog, git

    def test_read_only_cache_modes_do_not_manufacture_state_or_delete(self):
        cache = self.cache()
        with self.cache_tools(cache):
            for mode in ("status", "dry_run"):
                doc, code = self.call(mode=mode)
                self.assertEqual(code, 0)
                self.assertTrue(doc["caches"][0]["eligible"])
                self.assertTrue((cache / "00/payload-d").exists())
                self.assertFalse(self.state.exists())
                self.assertTrue(json.dumps(doc, ensure_ascii=True).isascii())

    def test_only_idle_over_budget_canonical_cache_is_cleaned(self):
        cache = self.cache()
        with self.cache_tools(cache):
            doc, code = self.call()
        self.assertEqual(code, 0)
        self.assertEqual(doc["status"], "cleaned")
        self.assertFalse((cache / "00/payload-d").exists())
        self.assertTrue((cache / "README").exists())
        self.assertEqual(json.loads((self.state / "maintenance-last.json").read_text())["status"], "cleaned")
        for line in (self.state / "maintenance.jsonl").read_text().splitlines():
            json.loads(line)

    def test_busy_or_fresh_cache_is_preserved(self):
        cache = self.cache()
        busy = Quiet()
        busy.busy = lambda path, cache=False: True
        with self.cache_tools(cache):
            doc, _ = self.call(activity=busy)
            self.assertEqual(doc["caches"][0]["reason"], "active_build")
            self.assertTrue((cache / "00/payload-d").exists())
            os.utime(cache / "00/payload-d", None)
            doc, _ = self.call()
            self.assertEqual(doc["caches"][0]["reason"], "recent_cache_activity")
            self.assertTrue((cache / "00/payload-d").exists())

    def test_failed_cleaner_is_not_success_shaped(self):
        cache = self.cache()
        with self.cache_tools(cache, fail=True):
            doc, code = self.call()
        self.assertEqual(code, 4)
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["actions"], [])
        self.assertTrue((cache / "00/payload-d").exists())

    def test_fifteen_minute_cooldown_prevents_repeat_cleanup(self):
        cache = self.cache()
        with self.cache_tools(cache):
            self.call()
            (cache / "00/payload-d").write_bytes(b"new")
            self.old(cache)
            doc, _ = self.call()
        self.assertEqual(doc["status"], "cooldown")
        self.assertGreater(doc["cooldown_remaining_sec"], 890)
        self.assertTrue((cache / "00/payload-d").exists())

    def test_lock_contender_does_not_replace_owner_state(self):
        cache = self.cache()
        self.state.mkdir()
        saved = self.state / "maintenance-last.json"
        saved.write_text('{"owner":"keep"}\n')
        before = saved.read_bytes()
        with (self.state / "janitor.lock").open("a") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            with self.cache_tools(cache):
                doc, _ = self.call()
        self.assertEqual(doc["status"], "busy")
        self.assertEqual(saved.read_bytes(), before)
        self.assertTrue((cache / "00/payload-d").exists())

    def test_remote_round_trip_required_before_artifact_deletion(self):
        root, target, _, _, _, _ = self.repository()
        before = (root / ".git/index").read_bytes()
        preview, code = self.call(mode="dry_run")
        self.assertEqual(code, 0)
        self.assertEqual(preview["workspaces"][0]["status"], "pending_remote_verification")
        self.assertTrue(target.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual((root / ".git/index").read_bytes(), before)
        doc, code = self.call()
        self.assertEqual(code, 0)
        self.assertFalse(target.exists())
        self.assertTrue((root / "source.txt").exists())
        self.assertTrue((root / ".git").exists())
        self.assertTrue((root / "captures/keep.bin").exists())
        self.assertEqual(doc["workspaces"][0]["status"], "verified_pruned")

    def test_unarchived_source_or_fresh_lease_preserves_dependencies(self):
        root, target, _, _, _, _ = self.repository()
        (root / "source.txt").write_text("unbacked change\n")
        doc, _ = self.call()
        self.assertTrue(target.exists())
        self.assertEqual(doc["workspaces"][0]["status"], "protected_unarchived_changes")
        root.with_name(root.name + ".lock").touch()
        doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_active")
        self.assertTrue(target.exists())

    def test_staged_only_source_is_unbacked_even_when_worktree_matches(self):
        root, target, _, _, _, git = self.repository()
        (root / "source.txt").write_text("staged-only unbacked source\n")
        git("add", "source.txt")
        (root / "source.txt").write_text("keep this source\n")
        doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_unarchived_changes")
        self.assertTrue(target.exists())

    def test_credentials_and_recordings_inside_generated_tree_survive(self):
        _, target, _, _, _, _ = self.repository()
        (target / ".env").write_text("TEST_VALUE=not-a-live-credential\n")
        (target / "captures").mkdir()
        (target / "captures/recording.webm").write_bytes(b"keep recording")
        self.old(target)
        doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_unverified")
        self.assertTrue((target / ".env").exists())
        self.assertTrue((target / "captures/recording.webm").exists())

    def test_npm_credentials_inside_ignored_dependencies_survive(self):
        _, target, _, _, _, _ = self.repository()
        (target / ".npmrc").write_text("fixture-registry-auth=not-a-live-token\n")
        self.old(target)
        self.call()
        self.assertTrue((target / ".npmrc").exists())

    def test_captured_image_in_test_results_is_not_disposable(self):
        root, target, head, record, catalog, git = self.repository()
        (root / ".gitignore").write_text("node_modules/\ncaptures/\ntest-results/\n")
        git("add", ".gitignore")
        git("commit", "--quiet", "-m", "ignore test artifacts")
        record["snapshot"] = git("rev-parse", "HEAD")
        catalog.write_text(json.dumps({"projects": [record]}))
        subprocess.check_call([
            "git", "--git-dir=" + str(self.base / "remotes/app.git"),
            "fetch", "--quiet", str(root), "main:main",
        ])
        capture = root / "test-results/run-001"
        capture.mkdir(parents=True)
        (capture / "screenshot.png").write_bytes(b"x" * 2 * 1024**2)
        self.old(root / "test-results")
        self.call()
        self.assertTrue((capture / "screenshot.png").exists())

    def test_force_staging_target_during_fetch_prevents_removal(self):
        _, target, _, _, _, git = self.repository()
        original_verify = m.verify_remote
        def verify_then_stage(record, state):
            original_verify(record, state)
            git("add", "-f", "node_modules/compiled.bin")
        with patch.object(m, "verify_remote", side_effect=verify_then_stage):
            doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_changed")
        self.assertTrue(target.exists())

    def test_inherited_alternate_index_cannot_hide_real_staging(self):
        root, target, head, _, _, git = self.repository()
        alternate = self.base / "alternate.index"
        subprocess.check_call(
            ["git", "-C", str(root), "read-tree", head],
            env=dict(os.environ, GIT_INDEX_FILE=str(alternate)),
        )
        (root / "source.txt").write_text("real staged-only work")
        git("add", "source.txt")
        (root / "source.txt").write_text("keep this source\n")
        with patch.dict(os.environ, {"GIT_INDEX_FILE": str(alternate)}):
            with self.assertRaises(ValueError):
                self.call()
        self.assertTrue(target.exists())

    def test_cooldown_is_reloaded_after_mutex_acquisition(self):
        cache = self.cache()
        original_lock = m.fcntl.flock
        published = []
        def lock_after_other_run(handle, operation):
            if str(handle.name) == self.args.cron_lock and not published:
                m.atomic_json(self.state / "maintenance-last.json", {"last_cleanup_at": m.utc()})
                published.append(True)
            return original_lock(handle, operation)
        with self.cache_tools(cache), patch.object(m.fcntl, "flock", side_effect=lock_after_other_run):
            doc, _ = self.call()
        self.assertEqual(doc["status"], "cooldown")
        self.assertTrue((cache / "00/payload-d").exists())

    def test_slow_cache_actions_respect_budget_and_publish_completed_work(self):
        cache = self.cache()
        other = self.base / "second-cache"
        shutil.copytree(cache, other)
        self.settings["go_cache_dirs"].append(str(other))
        self.save_policy()
        clock = [0.0]
        original_run, original_size = m.run, m.size
        def measured_size(path, **kwargs):
            if path in {cache, other} and (path / "00/payload-d").exists():
                return 2 * m.GIB
            return original_size(path, **kwargs)
        def slow_run(args, **kwargs):
            if str(args[0]) == str(self.base / "go"):
                timeout = kwargs["timeout"]
                clock[0] += min(50, timeout)
                if timeout < 50:
                    raise subprocess.TimeoutExpired(args, timeout)
                (Path(kwargs["env"]["GOCACHE"]) / "00/payload-d").unlink()
                return b""
            return original_run(args, **kwargs)
        with self.cache_tools(cache), patch.object(m, "size", side_effect=measured_size), \
             patch.object(m, "run", side_effect=slow_run), \
             patch.object(m.time, "monotonic", side_effect=lambda: clock[0]):
            doc, code = self.call()
        self.assertLessEqual(clock[0], 90.01)
        self.assertEqual(code, 4)
        self.assertEqual(len(doc["actions"]), 1)
        saved = json.loads((self.state / "maintenance-last.json").read_text())
        self.assertEqual(len(saved["actions"]), 1)

    def test_completed_cleanup_survives_post_size_measurement_failure(self):
        cache = self.cache()
        original_size = m.size
        def measurement(path, **kwargs):
            if path == cache:
                if (cache / "00/payload-d").exists():
                    return 2 * m.GIB
                raise subprocess.TimeoutExpired(["du", str(path)], kwargs.get("timeout", 30))
            return original_size(path, **kwargs)
        with self.cache_tools(cache), patch.object(m, "size", side_effect=measurement):
            doc, code = self.call()
        self.assertEqual(code, 4)
        self.assertFalse((cache / "00/payload-d").exists())
        self.assertTrue(doc["actions"][0]["cleanup_completed"])
        self.assertIsNone(doc["actions"][0]["removed_bytes"])
        self.assertIsNotNone(doc["last_cleanup_at"])
        saved = json.loads((self.state / "maintenance-last.json").read_text())
        self.assertEqual(len(saved["actions"]), 1)
        followup, _ = self.call(mode="status")
        self.assertEqual(followup["status"], "cooldown")

    def test_worktree_discovery_dry_run_does_not_refresh_real_index(self):
        root, target, _, _, _, _ = self.repository()
        self.settings.update(archive_roots=[], archive_catalogs=[], worktree_roots=[str(root.parent)])
        self.save_policy()
        os.utime(root / "source.txt", None)
        before = (root / ".git/index").read_bytes()
        self.call(mode="dry_run")
        self.assertEqual((root / ".git/index").read_bytes(), before)
        self.assertTrue(target.exists())

    def test_local_commit_not_on_remote_is_not_trusted(self):
        root, target, _, record, catalog, git = self.repository()
        (root / "source.txt").write_text("local-only commit\n")
        git("add", "source.txt")
        git("commit", "--quiet", "-m", "unpublished")
        record["snapshot"] = git("rev-parse", "HEAD")
        catalog.write_text(json.dumps({"projects": [record]}))
        doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_unverified")
        self.assertTrue(target.exists())

    def test_network_url_rewritten_to_local_source_cannot_prove_backup(self):
        root, target, _, record, catalog, git = self.repository()
        (root / "source.txt").write_text("never pushed\n")
        git("add", "source.txt")
        git("commit", "--quiet", "-m", "unpublished")
        record["snapshot"] = git("rev-parse", "HEAD")
        catalog.write_text(json.dumps({"projects": [record]}))
        config = self.base / "rewrites.gitconfig"
        config.write_text(f'[url "{root.as_uri()}"]\n\tinsteadOf = {record["remote"]}\n')
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}):
            doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_unverified")
        self.assertTrue(target.exists())

    def test_embedded_unbacked_repository_is_not_disposable(self):
        _, target, _, _, _, _ = self.repository()
        (target / "hand-edited-package/.git").mkdir(parents=True)
        (target / "hand-edited-package/source.txt").write_text("unbacked work")
        self.old(target)
        doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_unverified")
        self.assertTrue((target / "hand-edited-package/source.txt").exists())

    def test_worker_lease_acquired_during_fetch_prevents_deletion(self):
        root, target, _, _, _, _ = self.repository()
        original_verify = m.verify_remote
        def verify_then_start_worker(record, state):
            original_verify(record, state)
            root.with_name(root.name + ".lock").touch()
        with patch.object(m, "verify_remote", side_effect=verify_then_start_worker):
            doc, _ = self.call()
        self.assertEqual(doc["workspaces"][0]["status"], "protected_changed")
        self.assertTrue(target.exists())

    def test_active_process_detection_uses_real_cwd_and_cache_environment(self):
        cache = self.cache()
        bindir = self.base / "active"
        bindir.mkdir()
        tool = bindir / "go"
        shutil.copyfile("/bin/sleep", tool)
        tool.chmod(0o755)
        actor = subprocess.Popen([str(tool), "30"], cwd=self.base, env=dict(os.environ, GOCACHE=str(cache)))
        try:
            for _ in range(50):
                if m.Activity().busy(cache, cache=True):
                    break
                time.sleep(0.01)
            else:
                self.fail("live Go cache was not protected")
        finally:
            actor.terminate()
            actor.wait(timeout=5)

    def test_private_session_managers_do_not_disable_all_cleanup(self):
        class Proc:
            name = "999999999"
            def __init__(self, process):
                self.process = process
            def stat(self):
                return types.SimpleNamespace(st_uid=os.getuid())
            def __truediv__(self, field):
                return types.SimpleNamespace(read_text=lambda: self.process)

        real_path = m.Path
        for name, blocked in (("gpg-agent", False), ("private-builder", True), ("go", True)):
            proc = Proc(name)
            fake_root = types.SimpleNamespace(iterdir=lambda: [proc])
            with patch.object(m, "Path", side_effect=lambda p: fake_root if p == "/proc" else real_path(p)), \
                 patch.object(m.os, "readlink", side_effect=PermissionError):
                activity = m.Activity()
            self.assertEqual(activity.unreadable_owned, blocked)
            self.assertEqual(activity.busy(self.base, cache=True), name == "go")
            if name == "private-builder":
                self.assertTrue(activity.busy(self.base))

    def test_go_defaults_and_persisted_cache_paths_are_resolved(self):
        env = {b"HOME": os.fsencode(self.base)}
        self.assertEqual(m.process_go_cache(env), self.base / ".cache/go-build")
        saved = self.base / ".config/go/env"
        saved.parent.mkdir(parents=True)
        custom = self.base / "shared-go"
        saved.write_text(f"GOCACHE={custom}\n")
        self.assertEqual(m.process_go_cache(env), custom)
        env[b"GOCACHE"] = os.fsencode(self.base / "override")
        self.assertEqual(m.process_go_cache(env), self.base / "override")

    def test_invalid_policy_fails_instead_of_silently_disabling_limits(self):
        self.settings["total_cache_budget_gib"] = False
        self.save_policy()
        with self.assertRaises(ValueError):
            self.call(mode="status")
        self.assertFalse(self.state.exists())

    def test_service_invocation_metadata(self):
        # Optional execution context links only the budget phase, without changing policy or atomic writes.
        valid = "b" * 32
        before = None
        real_replace = os.replace
        for context, expected in (
            (valid, valid), (valid.upper(), valid), (None, None), ("", None),
            ("b" * 31, None), ("b" * 33, None), ("z" * 32, None),
            (" " + valid, None), (valid + "\n", None), ("NaN", None),
        ):
            replacements = []
            def observe_replace(source, target):
                if target == self.state / "maintenance-last.json":
                    self.assertEqual(json.loads(source.read_text())["service_invocation_id"], expected)
                    self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                    if before is not None:
                        self.assertEqual(json.loads(target.read_text()), before)
                    replacements.append((source, target))
                return real_replace(source, target)
            with self.subTest(context=context), patch.dict(os.environ), \
                    patch.object(m.os, "replace", side_effect=observe_replace), \
                    patch.object(m.shutil, "disk_usage", return_value=types.SimpleNamespace(free=20*m.GIB)):
                os.environ.pop("INVOCATION_ID", None)
                if context is not None:
                    os.environ["INVOCATION_ID"] = context
                doc, code = self.call()
            self.assertEqual(code, 0)
            self.assertEqual(doc["service_invocation_id"], expected)
            self.assertEqual(doc["actions"], [])
            self.assertEqual(doc["removed_bytes"], 0)
            self.assertEqual(doc["observed_reclaimed_bytes"], 0)
            self.assertEqual(doc["cache_budget_bytes"], 2*m.GIB)
            self.assertEqual(json.loads((self.state / "maintenance-last.json").read_text()), doc)
            self.assertEqual(len(replacements), 1)
            self.assertFalse(list(self.state.glob("maintenance-last.json.tmp.*")))
            self.assertNotIn("service_invocation_id", json.loads((self.state / "maintenance.jsonl").read_text().splitlines()[-1]))
            before = doc

    def test_naive_cleanup_timestamp_does_not_disable_cooldown(self):
        self.state.mkdir()
        (self.state / "maintenance-last.json").write_text(
            '{"last_cleanup_at":"2026-01-01T00:00:00"}'
        )
        with self.assertRaises(ValueError):
            self.call(mode="status")

    def test_recurring_history_is_compact_and_bounded(self):
        self.state.mkdir()
        history = self.state / "maintenance.jsonl"
        history.write_bytes(b"x" * (5 * 1024**2))
        self.call()
        self.assertEqual(history.with_name("maintenance.jsonl.1").stat().st_size, 5 * 1024**2)
        event = json.loads(history.read_text())
        self.assertNotIn("uninspectable_workspace_processes", event)
        self.assertLess(history.stat().st_size, 4096)

    def test_low_free_bytes_also_make_idle_caches_eligible(self):
        cache = self.cache()
        self.settings.update(shared_cache_budget_gib=16, total_cache_budget_gib=32)
        self.save_policy()
        with self.cache_tools(cache), patch.object(m, "disk_used_pct", return_value=81), \
             patch.object(m.shutil, "disk_usage", return_value=types.SimpleNamespace(free=19*m.GIB)):
            doc, _ = self.call(mode="status")
        self.assertTrue(doc["caches"][0]["eligible"])
        self.assertEqual(doc["caches"][0]["reason"], "disk_pressure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
