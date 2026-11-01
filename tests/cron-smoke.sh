#!/usr/bin/env bash
# Prove scheduler observability with harmless executables, never real maintenance.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT/scripts/cron-run.sh" <<'PY'
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

launcher = str(Path(sys.argv[1]).resolve())
names = ["cert-watch", "log-shipper", "system-janitor", "system-updater"]
limits = dict(zip(names, [7200, 1800, 172800, 172800]))
checked = set()

with tempfile.TemporaryDirectory(prefix="toolkit-cron-") as temp:
    home = Path(temp) / "home with spaces%"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(home / "state"),
               PATH="/usr/bin:/bin", BASH_ENV="")
    state = home / "state/agent-toolkit/cron"
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True)

    def run(*args, rc=0):
        result = subprocess.run([launcher, *args], env=env, capture_output=True, text=True, timeout=12)
        assert result.returncode == rc, (args, result.returncode, result.stdout, result.stderr)
        return result

    def status(rc):
        result = run("--status", "--json", rc=rc)
        doc = json.loads(result.stdout)
        assert doc["exit_code"] == rc and len(doc["tools"]) == 4
        assert "\x1b" not in result.stdout and result.stdout.isascii()
        return {item["name"]: item for item in doc["tools"]}

    def stub(name, body="exit 0\n"):
        p = bindir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
        return p

    # Status/help/version and rejected inputs must not manufacture state or success.
    version = json.loads(run("--version", "--json").stdout)
    capabilities = version["capabilities"]
    assert capabilities == sorted(set(capabilities))
    run("--help")
    assert all(item["status"] == "missing" for item in status(5).values())
    for args in [("bad",), ("--json",), ("system-updater", "--apply"), ("/bin/true",)]:
        run(*args, rc=3)
    assert not state.exists()
    checked.update(["missing-runs", "status-json"])
    print("ok: read-only missing status and precondition guards")

    # Missing/permission-denied executables leave observable exact startup failures.
    run("cert-watch", rc=127)
    assert status(4)["cert-watch"]["exit_code"] == 127
    assert "launcher error" in (state / "cert-watch.log").read_text()
    denied = stub("cert-watch")
    denied.chmod(0o644)
    run("cert-watch", rc=126)
    assert status(4)["cert-watch"]["exit_code"] == 126
    for name in names:
        stub(name, 'printf "%s\\n" "$@" > "$HOME/updater-args"\nexit 0\n' if name == "system-updater" else "exit 0\n")
        run(str(bindir / name))
    assert (home / "updater-args").read_text() == "--dry-run\n"
    assert all(item["status"] == "ok" for item in status(0).values())
    checked.update(["cron-launch", "updater-dry-run"])
    print("ok: installed-path whitelist, minimal PATH, safe updater and startup recovery")

    # A tool failing before its own log setup must preserve stdout/stderr and rc.
    for rc in [3, 7]:
        stub("cert-watch", f"echo distinctive-stdout\necho distinctive-stderr >&2\nexit {rc}\n")
        run("cert-watch", rc=rc)
        assert status(4)["cert-watch"]["exit_code"] == rc
    text = (state / "cert-watch.log").read_text()
    assert "distinctive-stdout" in text and "distinctive-stderr" in text
    stub("cert-watch")
    run("cert-watch")
    assert status(0)["cert-watch"]["status"] == "ok"
    checked.add("exit-code-propagation")
    print("ok: failures captured and exact exit codes preserved")

    # Corrupt/old/future records are not silently trusted; boundaries do not sleep.
    path = state / "cert-watch.json"
    good = json.loads(path.read_text())
    def at(seconds):
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()
    for name, limit in limits.items():
        p = state / (name + ".json")
        saved = p.read_text()
        for age, expected, rc in [(limit - 3, "ok", 0), (limit + 3, "stale", 4)]:
            p.write_text(json.dumps({"tool": name, "started_at": at(age + 1),
                                     "ended_at": at(age), "exit_code": 0}))
            assert status(rc)[name]["status"] == expected
        p.write_text(saved)
    for bad in ["{broken", "[]", json.dumps(dict(good, exit_code=True)),
                json.dumps(dict(good, ended_at="yesterday")),
                json.dumps(dict(good, started_at=at(-600), ended_at=at(-601))),
                json.dumps(dict(good, started_at="2026-01-01T00:00:00"))]:
        path.write_text(bad)
        assert status(4)["cert-watch"]["status"] == "invalid"
    path.write_text(json.dumps(dict(good, started_at=at(-20), ended_at=at(-21))))
    assert status(0)["cert-watch"]["age_seconds"] == 0
    run("cert-watch")
    checked.add("stale-runs")
    print("ok: freshness boundaries, corrupt/future timestamps and recovery")

    # Lock ownership and atomic reads are exercised during real overlapping processes.
    stub("cert-watch", 'touch "$HOME/ready"\nwhile [ ! -e "$HOME/release" ]; do sleep 0.05; done\nexit 0\n')
    owner = subprocess.Popen([launcher, "cert-watch"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if (home / "ready").exists():
            break
        time.sleep(0.05)
    assert (home / "ready").exists(), "owner did not start"
    owned = path.read_bytes()
    assert status(5)["cert-watch"]["status"] == "running"
    run("cert-watch", rc=1)
    assert path.read_bytes() == owned, "contender replaced owner record"
    errors = []
    stop = threading.Event()
    reads = []
    def reader():
        while not stop.is_set():
            try:
                json.loads(path.read_text())
                reads.append(1)
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
    thread = threading.Thread(target=reader)
    thread.start()
    (home / "release").touch()
    assert owner.wait(timeout=6) == 0
    owner.communicate()
    stop.set()
    thread.join()
    assert reads and not errors, errors
    assert status(0)["cert-watch"]["status"] == "ok"
    checked.add("atomic-state")
    print("ok: owner lock, read-only running status and concurrent atomic JSON reads")

    # Interrupted launch cannot be published as a successful completion.
    (home / "ready").unlink()
    (home / "release").unlink()
    owner = subprocess.Popen([launcher, "cert-watch"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if (home / "ready").exists():
            break
        time.sleep(0.05)
    assert (home / "ready").exists()
    owner.send_signal(signal.SIGTERM)
    assert owner.wait(timeout=8) == 143
    owner.communicate()
    assert status(4)["cert-watch"]["exit_code"] == 143
    stub("cert-watch")
    run("cert-watch")
    status(0)
    # The bounded five-second kill fallback also covers a child ignoring TERM.
    stub("cert-watch", 'trap "" TERM INT\ntouch "$HOME/ready-ignore"\nwhile :; do sleep 1; done\n')
    owner = subprocess.Popen([launcher, "cert-watch"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if (home / "ready-ignore").exists():
            break
        time.sleep(0.05)
    assert (home / "ready-ignore").exists()
    owner.send_signal(signal.SIGTERM)
    assert owner.wait(timeout=8) == 143
    owner.communicate()
    assert status(4)["cert-watch"]["exit_code"] == 143
    stub("cert-watch")
    run("cert-watch")
    print("ok: signal propagation, ignored-TERM kill fallback and lock recovery")

    # A noisy single launch must remain bounded, not just rotate on the next run.
    log = state / "cert-watch.log"
    stub("cert-watch", 'dd if=/dev/zero bs=1048576 count=6 2>/dev/null\necho noisy-tail\nexit 0\n')
    run("cert-watch")
    assert "noisy-tail" in log.read_text()
    assert all(p.stat().st_size <= 1024 * 1024 for p in state.glob("cert-watch.log*"))
    stub("cert-watch")
    # Private bounded startup logs prevent high-frequency cron from unbounded growth.
    for _ in range(5):
        log.write_text("x" * (1024 * 1024))
        run("cert-watch")
    assert all(Path(str(log) + f".{n}").exists() for n in [1, 2, 3])
    assert not Path(str(log) + ".4").exists()
    for p in [log, path, state / "cert-watch.lock"]:
        assert p.stat().st_mode & 0o077 == 0, (p, oct(p.stat().st_mode))
    before = {str(p): p.read_bytes() for p in state.iterdir() if p.is_file()}
    status(0)
    after = {str(p): p.read_bytes() for p in state.iterdir() if p.is_file()}
    assert before == after, "status mutated launcher state"
    # Per-tool state initialization failures must not leave an old healthy signal.
    for suffix in [".log", ".lock", ".json", ".log.1"]:
        obstacle = state / ("cert-watch" + suffix)
        obstacle.unlink(missing_ok=True)
        obstacle.mkdir()
        run("cert-watch", rc=3)
        assert status(4)["cert-watch"]["status"] == "invalid"
        obstacle.rmdir()
        run("cert-watch")
        assert status(0)["cert-watch"]["status"] == "ok"
    # Broken symlinks are unusable state, unlike deleted files which can be recreated.
    for suffix in [".json", ".log", ".lock", ".log.1"]:
        artifact = state / ("cert-watch" + suffix)
        artifact.unlink(missing_ok=True)
        artifact.symlink_to(home / "nonexistent-target")
        run("cert-watch", rc=3)
        assert status(4)["cert-watch"]["status"] == "invalid"
        assert not (home / "nonexistent-target").exists()
        artifact.unlink()
        run("cert-watch")
    for suffix in [".log", ".lock"]:
        artifact = state / ("cert-watch" + suffix)
        artifact.unlink()
        run("cert-watch")
        assert artifact.is_file() and status(0)["cert-watch"]["status"] == "ok"
    assert state.stat().st_mode & 0o077 == 0
    # Read-only status must expose an unwritable state location, not old success.
    if os.geteuid() != 0:
        print("ok: non-root state permission failure exercised")
        state.chmod(0o500)
        assert all(item["status"] == "invalid" for item in status(4).values())
        state.chmod(0o700)
    blocked = home / "blocked"
    blocked.write_text("not a directory")
    old_state = env["XDG_STATE_HOME"]
    env["XDG_STATE_HOME"] = str(blocked / "state")
    assert all(item["status"] == "invalid" for item in status(4).values())
    env["XDG_STATE_HOME"] = old_state
    checked.add("bounded-logs")
    assert checked == set(capabilities), (checked, capabilities)
    print("ok: rotation, private state, read-only status and capability completeness")

    # Verify optional systemd catch-up and disk-pressure companion script.
    catchup = Path(launcher).with_name("system-janitor-catchup.sh")
    janitor_state = Path(env["XDG_STATE_HOME"]) / "janitor"
    janitor_state.mkdir(parents=True, exist_ok=True)
    last_run_file = janitor_state / "last-run.json"
    tmp_sandbox = home / "tmp-catchup"
    tmp_sandbox.mkdir(parents=True, exist_ok=True)
    stale_dir = tmp_sandbox / "stale-build"
    stale_dir.mkdir()
    old_ts = time.time() - 7200
    os.utime(stale_dir, (old_ts, old_ts))
    nested_dir = tmp_sandbox / "nested-active-build"
    nested_sub = nested_dir / "sub"
    nested_sub.mkdir(parents=True)
    (nested_sub / "fresh.o").write_text("fresh")
    os.utime(nested_dir, (old_ts, old_ts))
    os.utime(nested_sub, (old_ts, old_ts))
    catchup_env = dict(
        env,
        JANITOR_CRON_WRAPPER=str(launcher),
        JANITOR_BIN=str(bindir / "system-janitor"),
        JANITOR_LAST_RUN=str(last_run_file),
        JANITOR_CRON_STATE=str(state / "system-janitor.json"),
        JANITOR_TMP_ROOT=str(tmp_sandbox),
    )
    # 1. Missing last-run.json triggers missed_daily_slot, sweeps stale_dir, preserves nested_dir.
    res = subprocess.run([str(catchup)], env=catchup_env, capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr
    assert "[trigger] reason=missed_daily_slot" in res.stdout, res.stdout
    assert not stale_dir.exists() and nested_dir.exists()
    # 2. Fresh healthy last-run.json with high pressure stays in pressure_cooldown.
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    last_run_file.write_text(json.dumps({"ended_at": now_iso, "dry_run": 0, "safety_integrity": "ok"}))
    (state / "system-janitor.json").write_text(json.dumps({
        "tool": "system-janitor", "started_at": now_iso, "ended_at": now_iso, "exit_code": 0
    }))
    cd_env = dict(catchup_env, JANITOR_PRESSURE_THRESHOLD_PCT="1", JANITOR_PRESSURE_COOLDOWN_SEC="14400")
    res_cd = subprocess.run([str(catchup), "--status", "--json"], env=cd_env, capture_output=True, text=True, timeout=15)
    cd_data = json.loads(res_cd.stdout)
    assert cd_data["status"] == "cooldown" and cd_data["reason"] == "pressure_cooldown" and not cd_data["would_trigger"], cd_data
    print("ok: system-janitor-catchup daily catch-up, cooldown guard, and deepest-mtime /tmp sweep")

print("all cron smoke checks passed")
PY
