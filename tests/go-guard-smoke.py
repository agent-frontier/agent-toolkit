#!/usr/bin/env python3
"""Verify low-disk Go admission without blocking existing/nested builds."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("go_guard", ROOT / "scripts/go-with-disk-guard.py")
g = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g)


class Launched(Exception):
    pass


class Queued(Exception):
    pass


class GoGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="go-admission-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / "state"
        self.cache = self.home / "cache"
        self.cache.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "GOCACHE": str(self.cache),
                                         "JANITOR_GO_INFLIGHT": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.policy = {"build_min_free_gib": 20, "build_max_used_pct": 85}

    def test_metadata_and_cleanup_never_wait_for_space(self):
        for args in (["version"], ["clean", "-cache"], ["-C", "/tmp", "env", "GOCACHE"],
                     ["mod", "edit", "-json"], ["mod", "graph"],
                     ["-C", "mod", "mod", "edit", "-print"]):
            with patch.object(g.os, "execv", side_effect=Launched), \
                 patch.object(g, "enough_space", side_effect=AssertionError("metadata was gated")):
                with self.assertRaises(Launched):
                    g.launch(Path("/bin/true"), args, self.policy, self.state)
        self.assertFalse(self.state.exists())

    def test_nested_running_build_does_not_deadlock_maintenance(self):
        with patch.object(g, "running_go_ancestor", return_value=True), \
             patch.object(g.os, "execv", side_effect=Launched), \
             patch.object(g, "enough_space", side_effect=AssertionError("nested build was gated")):
            with self.assertRaises(Launched):
                g.launch(Path("/bin/true"), ["test"], self.policy, self.state)

    def test_inherited_marker_without_live_go_ancestor_cannot_bypass(self):
        with patch.dict(os.environ, {"JANITOR_GO_INFLIGHT": "1"}), \
             patch.object(g, "running_go_ancestor", return_value=False), \
             patch.object(g, "enough_space", return_value=False), \
             patch.object(g.time, "sleep", side_effect=Queued), \
             patch.object(g.os, "execv", side_effect=AssertionError("stale marker bypassed admission")):
            with self.assertRaises(Queued):
                g.launch(Path("/bin/true"), ["test"], self.policy, self.state)

    def test_low_disk_queues_then_launches_with_a_shared_cache_lock(self):
        observations = []
        def wait(_):
            queued = self.state / "waiting-go-builds" / f"{os.getpid()}.json"
            doc = json.loads(queued.read_text())
            self.assertEqual(doc["reason"], "disk_pressure")
            self.assertNotIn("ldflags", json.dumps(doc))
            observations.append(doc)
        def execute(path, args, env):
            lock_path = g.m.cache_build_lock(self.state, self.cache)
            with lock_path.open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(env["JANITOR_GO_INFLIGHT"], "1")
            raise Launched
        with patch.object(g, "running_go_ancestor", return_value=False), \
             patch.object(g, "enough_space", side_effect=[False, True, True]), \
             patch.object(g.time, "sleep", side_effect=wait), \
             patch.object(g.os, "execve", side_effect=execute):
            with self.assertRaises(Launched):
                g.launch(Path("/bin/true"), ["test", "-ldflags", "private-argument"], self.policy, self.state)
        self.assertTrue(observations)
        self.assertFalse((self.state / "waiting-go-builds" / f"{os.getpid()}.json").exists())

    def test_shared_lock_survives_real_exec_until_the_build_exits(self):
        config = self.home / ".config/system-janitor"
        config.mkdir(parents=True)
        ready, release = self.home / "ready", self.home / "release"
        real = self.home / "real-go"
        real.write_text(
            "#!/usr/bin/env python3\nimport os,time\nfrom pathlib import Path\n"
            "assert os.environ['JANITOR_GO_INFLIGHT']=='1'\n"
            f"Path({str(ready)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n"
        )
        real.chmod(0o755)
        (config / "go-real-path").write_text(str(real) + "\n")
        (config / "maintenance.json").write_text(json.dumps({
            "build_min_free_gib": 1, "build_max_used_pct": 100,
        }))
        state = self.home / ".local/state/janitor"
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.home / ".config"),
                   XDG_STATE_HOME=str(self.home / ".local/state"))
        build = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/go-with-disk-guard.py"), "test"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            for _ in range(200):
                if ready.exists():
                    break
                if build.poll() is not None:
                    self.fail(build.communicate()[1].decode())
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            lock_path = g.m.cache_build_lock(state, self.cache)
            with lock_path.open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.touch()
            self.assertEqual(build.wait(timeout=5), 0)
            with lock_path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if build.poll() is None:
                build.terminate()
                build.wait(timeout=5)
            build.communicate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
