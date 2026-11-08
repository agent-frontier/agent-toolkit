#!/usr/bin/env python3
"""Storage boundaries using owned fixtures, original GNU du and real children."""

import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("dashboard_storage", ROOT / "scripts/dashboard_storage.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)
REAL_POPEN = subprocess.Popen
DU = "/usr/bin/du"
FLAGS = ["--one-file-system", "--block-size=1", "--null", "--max-depth=1",
         "--dereference-args", "--"]
TERMINAL = {"complete", "partial", "failed", "cancelled", "timed_out"}


def allocated(path):
    return os.stat(path, follow_symlinks=False).st_blocks * 512


def native(fd, flags=FLAGS):
    return subprocess.run([DU, *flags, f"/proc/self/fd/{fd}"], pass_fds=(fd,),
                          capture_output=True, check=False, env={"LC_ALL": "C"})


def records(raw):
    return {path: int(size) for size, path in
            (record.split(b"\t", 1) for record in raw.rstrip(b"\0").split(b"\0"))}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dashboard-storage-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roots = {name: self.root / name for name in ("home", "root", "tmp")}
        for path in self.roots.values():
            path.mkdir()
        self.scanner = self.manager()

    def manager(self, **changes):
        scanner = d.StorageScanner(enabled=True, roots=changes.pop("roots", self.roots),
                                   wall_seconds=changes.pop("wall_seconds", 3),
                                   terminate_grace=0.12, reap_seconds=0.5, **changes)
        self.addCleanup(scanner.close)
        return scanner

    def wait(self, scanner=None, states=TERMINAL):
        scanner = scanner or self.scanner
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            view = scanner.describe()
            if view["job"] and view["job"]["state"] in states:
                # Do not race the worker's final return when asserting another scan can start.
                if view["job"]["state"] == "finalization_failed" or not scanner._worker.is_alive():
                    return view["job"]
            time.sleep(0.005)
        self.fail(f"owned fixture did not finalize: {scanner.describe()}")

    def reject(self, payload, status, scanner=None):
        with self.assertRaises(d.StorageError) as caught:
            (scanner or self.scanner).start(payload)
        self.assertEqual(caught.exception.status, status)

    def script(self, code):
        children = []

        def launch(argv, **kwargs):
            child = REAL_POPEN([sys.executable, "-B", "-u", "-c", code, argv[-1]], **kwargs)
            children.append(child)
            return child

        return children, mock.patch.object(d.subprocess, "Popen", side_effect=launch)

    def test_import_init_metadata_and_reads_have_no_worker_or_spawn(self):
        # Discovery and ordinary reads must never become an implicit recursive scan.
        with mock.patch.object(d.subprocess, "Popen") as launch:
            observer = d.StorageScanner(enabled=False, roots=self.roots)
            self.addCleanup(observer.close)
            for _ in range(10):
                self.assertIsNone(observer.describe()["job"])
                self.assertTrue(observer.metadata()["supported"])
            self.assertIsNone(observer._worker)
            self.reject({"root": "home"}, 403, observer)
            launch.assert_not_called()
        self.assertEqual(self.scanner.metadata()["limits"]["max_directories"], 128)
        self.assertEqual(d.StorageScanner().metadata()["limits"]["wall_seconds"], 120)

    def test_original_du_fd_flags_allocations_hardlinks_sparse_and_symlinks(self):
        # Mock stdout cannot prove -H, physical child traversal or allocated-byte accounting.
        home = self.roots["home"]
        branch = home / "branch"
        nested = branch / "nested"
        nested.mkdir(parents=True)
        sparse = nested / "sparse"
        with sparse.open("wb") as target:
            target.write(b"x")
            target.seek(64 * 1024 * 1024)
            target.write(b"x")
        os.link(sparse, nested / "hardlink")
        (home / "direct").write_bytes(b"x" * 9000)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "decoy").write_bytes(b"x" * 80000)
        (branch / "escape").symlink_to(outside, target_is_directory=True)
        (home / "child-link").symlink_to(outside, target_is_directory=True)
        expected_branch = (allocated(branch) + allocated(nested) + allocated(sparse)
                           + allocated(branch / "escape"))
        expected = expected_branch + allocated(home) + allocated(home / "direct") + allocated(home / "child-link")
        calls = []

        def launch(argv, **kwargs):
            calls.append((argv, kwargs))
            return REAL_POPEN(argv, **kwargs)

        with mock.patch.object(d.subprocess, "Popen", side_effect=launch):
            self.scanner.start({"root": "home"})
            job = self.wait()
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["root_bytes"], expected)
        self.assertEqual([(row["name"], row["bytes"]) for row in job["directories"]], [("branch", expected_branch)])
        self.assertEqual(job["direct_bytes"], expected - expected_branch)
        argv, kwargs = calls[0]
        fd = kwargs["pass_fds"][0]
        self.assertEqual(argv, [DU, *FLAGS, f"/proc/self/fd/{fd}"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("preexec_fn", kwargs)
        self.assertFalse(kwargs.get("shell", False))
        with self.assertRaises(OSError):
            os.fstat(fd)  # The server's copy of the pinned fd was closed after finalization.
        pinned = os.open(home, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            original = native(pinned)
            self.assertEqual(original.returncode, 0)
            self.assertEqual(records(original.stdout)[os.fsencode(f"/proc/self/fd/{pinned}")], expected)
            without_h = native(pinned, [flag for flag in FLAGS if flag != "--dereference-args"])
            self.assertNotEqual(records(without_h.stdout)[os.fsencode(f"/proc/self/fd/{pinned}")], expected)
            follow_children = native(pinned, [*FLAGS[:-2], "--dereference", "--"])
            self.assertGreater(records(follow_children.stdout)[os.fsencode(f"/proc/self/fd/{pinned}")], expected)
        finally:
            os.close(pinned)
        self.assertLess(allocated(sparse), os.stat(sparse).st_size)

    def test_original_du_nul_newline_tab_invalid_utf_and_parser_chunks(self):
        # First-tab/NUL parsing must preserve unusual directory identities, not split their names.
        home = os.fsencode(self.roots["home"])
        names = [b"line\nwith\ttab", b"invalid-\xff", b"<img onerror=bad>"]
        for name in names:
            os.mkdir(home + b"/" + name)
            fd = os.open(home + b"/" + name + b"/file", os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(fd, b"x" * 100)
            os.close(fd)
        pinned = os.open(home, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            raw = native(pinned).stdout
            alias = os.fsencode(f"/proc/self/fd/{pinned}")
            parser = d.DuParser(alias)
            for index in range(0, len(raw), 3):
                parser.feed(raw[index:index + 3])
            parser.finish()
            self.assertEqual({name for _, name in parser.rows}, set(names))
            self.assertEqual(parser.root_bytes, records(raw)[alias])
        finally:
            os.close(pinned)
        self.scanner.start({"root": "home"})
        job = self.wait()
        self.assertEqual(job["state"], "complete")
        self.assertEqual(len(job["directories"]), 3)
        self.assertTrue(all(row["navigable"] for row in job["directories"]))
        self.assertIn(r"invalid-\xff", [row["name"] for row in job["directories"]])
        self.assertIn(r"line\nwith\ttab", [row["name"] for row in job["directories"]])
        json.dumps(self.scanner.describe(), ensure_ascii=True, allow_nan=False).encode("ascii")

    def test_original_du_pinned_root_survives_rename_but_navigation_rejects_replacement(self):
        # The original fd, not its renamed pathname, must remain du's sole dereferenced argument.
        home = self.roots["home"]
        folder = home / "original"
        folder.mkdir()
        (folder / "file").write_bytes(b"x" * 5000)
        expected = allocated(home) + allocated(folder) + allocated(folder / "file")

        def launch(argv, **kwargs):
            home.rename(home.with_name("original-home"))
            home.mkdir()
            (home / "replacement").write_bytes(b"x" * 90000)
            return REAL_POPEN(argv, **kwargs)

        with mock.patch.object(d.subprocess, "Popen", side_effect=launch):
            self.scanner.start({"root": "home"})
            job = self.wait()
        self.assertEqual(job["root_bytes"], expected)
        self.assertEqual([row["name"] for row in job["directories"]], ["original"])
        with mock.patch.object(d.subprocess, "Popen") as launch:
            self.reject({"job_id": job["id"], "directory_id": job["directories"][0]["id"]}, 409)
            launch.assert_not_called()

    def test_parser_rejects_unanchored_nested_nond_decimal_and_incomplete_output(self):
        # A plausible total cannot sanitize arbitrary paths, nondecimal sizes or torn records.
        alias = b"/proc/self/fd/42"
        bad = [b"1\t/etc\0", b"1\t/proc/self/fd/420/child\0",
               b"1\t/proc/self/fd/42/../escape\0", b"1\t/proc/self/fd/42/a/b\0",
               b"-1\t/proc/self/fd/42\0", b"1.5\t/proc/self/fd/42\0",
               b"9" * 100 + b"\t/proc/self/fd/42\0", b"4\t/proc/self/fd/42",
               b"1\t/proc/self/fd/42\0" * 2]
        for raw in bad:
            with self.subTest(raw=raw[:80]):
                parser = d.DuParser(alias)
                parser.feed(raw)
                with self.assertRaises(ValueError):
                    parser.finish()
        parser = d.DuParser(alias)
        parser.feed(b"x" * (2 * 1024 * 1024))
        self.assertLessEqual(len(parser.buffer), d.MAX_RECORD)
        with self.assertRaises(ValueError):
            parser.finish()

    def test_original_du_top128_full_totals_and_nonoverlapping_buckets(self):
        # Display truncation must not truncate the total or double-count parent plus children.
        home = self.roots["home"]
        for index in range(200):
            folder = home / f"dir-{index:03}"
            folder.mkdir()
            (folder / "file").write_bytes(b"x" * (100 + index * 128))
        (home / "direct").write_bytes(b"x" * 5000)
        fd = os.open(home, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            original = records(native(fd).stdout)
            alias = os.fsencode(f"/proc/self/fd/{fd}")
            sizes = sorted((size for path, size in original.items() if path != alias), reverse=True)
            total = original[alias]
        finally:
            os.close(fd)
        self.scanner.start({"root": "home"})
        job = self.wait()
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["root_bytes"], total)
        self.assertEqual([row["bytes"] for row in job["directories"]], sizes[:128])
        self.assertEqual(job["omitted_directories"], {"count": 72, "bytes": sum(sizes[128:])})
        self.assertEqual(job["direct_bytes"], total - sum(sizes))
        self.assertEqual(sum(row["bytes"] for row in job["directories"])
                         + job["omitted_directories"]["bytes"] + job["direct_bytes"], total)
        self.assertLess(len(json.dumps(self.scanner.describe())), d.VIEW_LIMIT)

    def test_directory_ids_revalidate_components_and_breadcrumbs_before_spawn(self):
        # Renamed/replaced paths and stale IDs must fail before launching a different directory.
        folder = self.roots["home"] / "folder"
        (folder / "nested").mkdir(parents=True)
        (folder / "nested/file").write_bytes(b"x")
        first = self.scanner.start({"root": "home"})["job"]["id"]
        job = self.wait()
        child = job["directories"][0]
        with mock.patch.object(d.subprocess, "Popen") as launch:
            self.reject({"job_id": first, "directory_id": "e" * 32, "path": str(folder)}, 400)
            self.reject({"job_id": "e" * 32, "directory_id": child["id"]}, 409)
            self.reject({"job_id": first, "directory_id": "e" * 32}, 400)
            launch.assert_not_called()
        self.scanner.start({"job_id": first, "directory_id": child["id"]})
        second = self.wait()
        self.assertEqual([crumb["name"] for crumb in second["breadcrumbs"]], ["Home", "folder"])
        self.assertNotEqual(second["id"], first)
        with mock.patch.object(d.subprocess, "Popen") as launch:
            self.reject({"job_id": first, "directory_id": child["id"]}, 409)
            folder.rename(folder.with_name("old-folder"))
            folder.mkdir()
            self.reject({"job_id": second["id"], "directory_id": second["directory_id"]}, 409)
            self.reject({"job_id": second["id"], "directory_id": second["directories"][0]["id"]}, 409)
            launch.assert_not_called()
        self.scanner.start({"job_id": second["id"], "directory_id": second["breadcrumbs"][0]["id"]})
        back = self.wait()
        self.assertEqual([crumb["name"] for crumb in back["breadcrumbs"]], ["Home"])

    def test_root_and_child_symlink_or_other_device_navigation_never_spawns(self):
        # No-follow opens must cover every component; du -x is not a drill-down authorization rule.
        folder = self.roots["home"] / "folder"
        folder.mkdir()
        self.scanner.start({"root": "home"})
        job = self.wait()
        child = job["directories"][0]
        folder.rename(folder.with_name("old"))
        folder.symlink_to(self.roots["tmp"], target_is_directory=True)
        link_root = self.root / "linked"
        link_root.symlink_to(self.roots["home"], target_is_directory=True)
        other = self.manager(roots={**self.roots, "tmp": link_root})
        with mock.patch.object(d.subprocess, "Popen") as launch:
            self.reject({"job_id": job["id"], "directory_id": child["id"]}, 409)
            self.reject({"root": "tmp"}, 400, other)
            launch.assert_not_called()
        folder.unlink()  # Only the owned test symlink; no host path is modified.
        folder.with_name("old").rename(folder)
        original = os.fstat

        def other_device(fd):
            info = original(fd)
            if info.st_ino != os.stat(folder).st_ino:
                return info
            values = list(info)
            values[2] = info.st_dev + 1
            return os.stat_result(values)

        with mock.patch.object(d.os, "fstat", side_effect=other_device), \
                mock.patch.object(d.subprocess, "Popen") as launch:
            self.reject({"job_id": job["id"], "directory_id": child["id"]}, 409)
            launch.assert_not_called()

    def test_fixed_payloads_unknown_fields_and_unavailable_tool_fail_closed(self):
        # Paths/commands and non-object JSON never enter the worker; unavailable is not zero usage.
        bad = [{}, [], None, {"root": "/"}, {"root": True}, {"root": "home", "path": "/etc"},
               {"root": "tmp", "argv": ["rm"]}, {"root": "home", "job_id": "a" * 32},
               {"directory_id": "a" * 32}, {"job_id": 42, "directory_id": "a" * 32}]
        with mock.patch.object(d.subprocess, "Popen") as launch:
            for payload in bad:
                self.reject(payload, 400)
            missing = self.manager(du_path=None)
            self.assertFalse(missing.metadata()["available"])
            self.reject({"root": "home"}, 503, missing)
            launch.assert_not_called()

    def test_original_du_unreadable_child_is_dated_partial_not_complete_or_zero(self):
        # Native traversal failure, including root=0, must expose a lower bound rather than empty success.
        denied = self.roots["home"] / "denied"
        os.mkdir(denied, 0o000)  # Initial mode on an empty owned fixture; no chmod/host permissions.
        self.addCleanup(os.rmdir, denied)
        self.scanner.start({"root": "home"})
        job = self.wait()
        self.assertEqual(job["state"], "partial")
        self.assertEqual(job["coverage"], "lower_bound")
        self.assertIn("Permission denied", job["warning_sample"])
        self.assertTrue(job["permission_denied"])
        self.assertIsNotNone(job["finished_at"])
        scanner = self.manager(roots={**self.roots, "tmp": denied})
        scanner.start({"root": "tmp"})
        metadata_only = self.wait(scanner)
        self.assertEqual(metadata_only["root_bytes"], allocated(denied))
        # tmpfs directories really allocate zero blocks: prove the root=0 failure rule with GNU du too.
        shm = tempfile.TemporaryDirectory(prefix="dashboard-storage-zero-", dir="/dev/shm")
        self.addCleanup(shm.cleanup)
        zero_dir = Path(shm.name) / "denied"
        os.mkdir(zero_dir, 0o000)
        self.addCleanup(os.rmdir, zero_dir)
        self.assertEqual(allocated(zero_dir), 0)
        scanner = self.manager(roots={**self.roots, "tmp": zero_dir})
        scanner.start({"root": "tmp"})
        zero = self.wait(scanner)
        self.assertEqual(zero["state"], "partial")
        self.assertIsNone(zero["root_bytes"])
        self.assertIsNone(zero["direct_bytes"])

    def test_delayed_real_child_start_read_busy_cancel_and_reap(self):
        # Cancel is asynchronous and cannot free the scan slot before the real child is reaped.
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)"
        children, patcher = self.script(code)
        with patcher:
            started = time.monotonic()
            job_id = self.scanner.start({"root": "tmp"})["job"]["id"]
            self.assertLess(time.monotonic() - started, 0.3)
            deadline = time.monotonic() + 1
            while not children and time.monotonic() < deadline:
                time.sleep(0.005)
            time.sleep(0.04)  # Let the real fixture install its TERM handler.
            self.reject({"root": "home"}, 409)
            before = time.monotonic()
            self.scanner.describe()
            self.assertLess(time.monotonic() - before, 0.1)
            cancelled = self.scanner.cancel({"job_id": job_id})["job"]
            self.assertEqual(cancelled["state"], "cancelling")
            self.reject({"root": "home"}, 409)
            done = self.wait()
            self.assertEqual(done["state"], "cancelled")
            self.assertEqual(done["coverage"], "unknown")
            self.assertIsNotNone(children[0].returncode)
            with self.assertRaises(ChildProcessError):
                os.waitpid(children[0].pid, os.WNOHANG)
        self.scanner.start({"root": "tmp"})
        self.assertEqual(self.wait()["state"], "complete")

    def test_real_trickling_child_obeys_total_wall_deadline_not_inactivity(self):
        # Continual output must not renew the deadline or keep a scan alive indefinitely.
        scanner = self.manager(wall_seconds=0.2)
        children, patcher = self.script("import os,time\nwhile True:\n os.write(1,b'1'); time.sleep(.025)")
        with patcher:
            before = time.monotonic()
            scanner.start({"root": "tmp"})
            job = self.wait(scanner)
        self.assertEqual(job["state"], "timed_out")
        self.assertLess(time.monotonic() - before, 1.5)
        self.assertIsNone(job["root_bytes"])
        self.assertIsNotNone(children[0].returncode)

    def test_real_noisy_child_drains_but_bounds_samples_and_malformed_results(self):
        # A pipe-filling native child must not deadlock or allocate its full stdout/stderr in the service.
        code = ("import os,sys\n"
                "for _ in range(512):\n os.write(2,b'warning\\n'*1024)\n"
                "os.write(1,b'x'*1048576+b'\\0')\n"
                "os.write(1,b'0\\t'+os.fsencode(sys.argv[1])+b'\\0')\n")
        children, patcher = self.script(code)
        with patcher:
            self.scanner.start({"root": "tmp"})
            job = self.wait()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error_code"], "malformed_output")
        self.assertIsNone(job["root_bytes"])
        self.assertGreater(job["stdout_bytes"], 1024 * 1024)
        self.assertGreater(job["stderr_bytes"], 1024 * 1024)
        self.assertLessEqual(len(job["warning_sample"].encode()), d.WARNING_LIMIT * 4)
        self.assertLess(len(json.dumps(self.scanner.describe())), d.VIEW_LIMIT)
        self.assertIsNotNone(children[0].returncode)

    def test_real_child_missing_root_and_spawn_failure_are_explicit_and_recoverable(self):
        # Exit zero without a root record, or an exec failure, cannot fabricate an empty complete view.
        children, patcher = self.script("pass")
        with patcher:
            self.scanner.start({"root": "tmp"})
            job = self.wait()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error_code"], "missing_root")
        self.assertIsNone(job["root_bytes"])
        self.assertIsNotNone(children[0].returncode)
        with mock.patch.object(d.subprocess, "Popen", side_effect=OSError(errno.EIO, "fixture exec failure")):
            self.scanner.start({"root": "tmp"})
            failed = self.wait()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error_code"], "spawn_failed")
        self.assertIsNone(self.scanner._process)
        self.scanner.start({"root": "tmp"})
        self.assertEqual(self.wait()["state"], "complete")

    def test_real_signal_terminated_native_child_is_failed_not_permission_partial(self):
        # A killed scanner is not a permission-limited successful traversal; expose the actual signal exit.
        children, patcher = self.script("import os,signal; os.kill(os.getpid(),signal.SIGKILL)")
        with patcher:
            self.scanner.start({"root": "tmp"})
            job = self.wait()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error_code"], "native_failed")
        self.assertEqual(job["exit_code"], -signal.SIGKILL)
        self.assertIsNone(job["root_bytes"])
        self.assertEqual(job["coverage"], "unknown")
        self.assertEqual(children[0].returncode, -signal.SIGKILL)

    def test_original_du_missing_pin_is_dated_partial_with_native_disappearance_warning(self):
        # Omit the actual inherited fd at exec: original du must report ENOENT, not a false zero result.
        code = (f"import os,sys; os.close(int(sys.argv[1].rsplit('/',1)[1])); "
                f"os.execv({DU!r}, [{DU!r}, *{FLAGS!r}, sys.argv[1]])")
        _, patcher = self.script(code)
        with patcher:
            self.scanner.start({"root": "tmp"})
            job = self.wait()
        self.assertEqual(job["state"], "partial")
        self.assertEqual(job["exit_code"], 1)
        self.assertIsNone(job["root_bytes"])
        self.assertIn("No such file or directory", job["warning_sample"])
        self.assertEqual(job["error_code"], "incomplete_root")
        self.assertIsNotNone(job["finished_at"])

    def test_worker_start_failure_closes_pin_and_does_not_leave_a_false_running_job(self):
        # A thread creation failure happens before Popen and must release the directory pin explicitly.
        with mock.patch.object(d.threading.Thread, "start", side_effect=RuntimeError("fixture task limit")), \
                mock.patch.object(d.subprocess, "Popen") as launch:
            self.reject({"root": "tmp"}, 503)
            launch.assert_not_called()
        self.assertEqual(self.scanner.describe()["job"]["state"], "failed")
        self.assertIsNone(self.scanner._process)
        self.scanner.start({"root": "tmp"})
        self.assertEqual(self.wait()["state"], "complete")

    def test_output_setup_failure_after_real_spawn_still_kills_and_reaps(self):
        # Selector/pipe setup is after Popen: treating it as a start error must not abandon the child.
        children, patcher = self.script("import time; time.sleep(10)")
        with patcher, mock.patch.object(self.scanner, "_selector", side_effect=OSError(errno.EMFILE, "fixture fd cap")):
            self.scanner.start({"root": "tmp"})
            self.scanner._worker.join(timeout=2)
            self.assertFalse(self.scanner._worker.is_alive())
            self.assertEqual(self.scanner.describe()["job"]["state"], "failed")
            self.assertEqual(self.scanner.describe()["job"]["error_code"], "read_failed")
            self.assertIsNone(self.scanner._process)
            self.assertIsNotNone(children[0].returncode)
        self.scanner.start({"root": "tmp"})
        self.assertEqual(self.wait()["state"], "complete")

    def test_finalization_failure_retains_real_handle_and_blocks_until_explicit_retry(self):
        # Failed reaping is not terminal cancellation; retry must finalize the same PID, never spawn.
        children, patcher = self.script("import time; time.sleep(10)")
        with patcher:
            job_id = self.scanner.start({"root": "tmp"})["job"]["id"]
            deadline = time.monotonic() + 1
            while not children and time.monotonic() < deadline:
                time.sleep(0.005)
            child = children[0]
            with mock.patch.object(child, "wait", side_effect=subprocess.TimeoutExpired("owned fixture", 0.5)):
                self.scanner.cancel({"job_id": job_id})
                failed = self.wait(states={"finalization_failed"})
            self.assertEqual(failed["state"], "finalization_failed")
            self.assertIs(self.scanner._process, child)
            self.reject({"root": "home"}, 409)
            self.scanner.cancel({"job_id": job_id})
            self.assertEqual(self.wait()["state"], "cancelled")
            self.assertEqual(len(children), 1)
            self.assertIsNone(self.scanner._process)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

    def test_real_process_group_and_shutdown_finalize_only_owned_children(self):
        # Shutdown/Cancel must include an inherited-pipe descendant, not signal unrelated processes.
        code = ("import os,signal,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(10)'])\n"
                "def stop(*_):\n child.wait(timeout=1); sys.exit(0)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "os.write(2,('descendant='+str(child.pid)+'\\n').encode())\n"
                "time.sleep(10)\n")
        children, patcher = self.script(code)
        with patcher:
            self.scanner.start({"root": "tmp"})
            deadline = time.monotonic() + 2
            while "descendant=" not in self.scanner.describe()["job"]["warning_sample"] and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIn("descendant=", self.scanner.describe()["job"]["warning_sample"])
            descendant = int(self.scanner.describe()["job"]["warning_sample"].split("descendant=")[1].splitlines()[0])
            self.assertTrue(self.scanner.close())
        job = self.scanner.describe()["job"]
        self.assertEqual(job["state"], "cancelled")
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)
        self.reject({"root": "home"}, 503)

    def test_real_process_group_signal_failure_retains_ownership_and_explicit_retry(self):
        # A failed group signal must retain a living child; a success-shaped Cancel would abandon it.
        children, patcher = self.script("import time; time.sleep(10)")
        with patcher:
            job_id = self.scanner.start({"root": "tmp"})["job"]["id"]
            deadline = time.monotonic() + 1
            while not children and time.monotonic() < deadline:
                time.sleep(0.005)
            with mock.patch.object(d.os, "killpg", side_effect=PermissionError("fixture signal refused")):
                self.scanner.cancel({"job_id": job_id})
                failed = self.wait(states={"finalization_failed"})
            self.assertEqual(failed["state"], "finalization_failed")
            self.assertIs(self.scanner._process, children[0])
            self.reject({"root": "home"}, 409)
            self.scanner.cancel({"job_id": job_id})
            self.assertEqual(self.wait()["state"], "cancelled")
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].returncode)

    def test_shutdown_signal_failure_retains_real_handle_and_blocks_new_scans(self):
        # A failed shutdown is explicit and retains ownership just like failed Cancel, not silent abandonment.
        children, patcher = self.script("import time; time.sleep(10)")
        with patcher:
            self.scanner.start({"root": "tmp"})
            deadline = time.monotonic() + 1
            while not children and time.monotonic() < deadline:
                time.sleep(0.005)
            with mock.patch.object(d.os, "killpg", side_effect=PermissionError("fixture shutdown refused")):
                self.assertFalse(self.scanner.close())
                self.assertEqual(self.scanner.describe()["job"]["state"], "finalization_failed")
                self.assertIs(self.scanner._process, children[0])
                self.reject({"root": "home"}, 503)
            self.assertTrue(self.scanner.close())
            self.assertIsNotNone(children[0].returncode)
            self.assertEqual(len(children), 1)

    def test_cancel_between_native_reap_and_result_commit_is_not_overwritten_by_complete(self):
        # Cancel can arrive during bounded identity publication after du exits; its accepted intent must survive.
        entered, release = threading.Event(), threading.Event()
        original = self.scanner._result

        def delayed_result(*args):
            entered.set()
            release.wait(timeout=2)
            return original(*args)

        self.addCleanup(release.set)
        with mock.patch.object(self.scanner, "_result", side_effect=delayed_result):
            job_id = self.scanner.start({"root": "tmp"})["job"]["id"]
            self.assertTrue(entered.wait(timeout=1))
            child = self.scanner._process
            self.assertIsNotNone(child.returncode)
            self.assertEqual(self.scanner.cancel({"job_id": job_id})["job"]["state"], "cancelling")
            self.reject({"root": "home"}, 409)
            release.set()
            self.assertEqual(self.wait()["state"], "cancelled")
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
