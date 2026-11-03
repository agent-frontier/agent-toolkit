# Cron scheduling and troubleshooting

`./install.sh` installs all four tools and the supporting `agent-toolkit-cron`
launcher in `~/.local/bin`. Cron uses local host time:

| Tool | Managed cadence | Stale after |
|---|---|---|
| system-janitor | Daily, 03:17 | 48 hours |
| system-updater | Daily, 04:23, always `--dry-run` | 48 hours |
| cert-watch | Hourly, minute 42 | 2 hours |
| log-shipper | Minutes 07, 22, 37, 52 of every hour | 30 minutes |

Janitor still performs its configured cleanup; only its frequency changes.
Updater never applies packages through this launcher. Cert-watch needs configured
certificate targets for useful inventory; log-shipper needs configured outputs
for export. The installer does not invent either configuration.

## Preview and install

```bash
cd ~/agent-toolkit
./install.sh --dry-run     # inspect exact cron commands before changing anything
crontab -l > "$HOME/crontab.before-agent-toolkit"
./install.sh
crontab -l
```

The installer migrates older managed/direct tool entries, preserves unrelated
jobs, and is idempotent. It manages user cron, not root cron or systemd timers.
Reinstalling restores the managed defaults; configure custom schedules separately
with that behavior in mind. On an installation without an existing crontab,
`crontab -l` returns nonzero; keep any existing backup rather than overwriting it.

## One-command launcher status

```bash
agent-toolkit-cron --version --json  # sorted capability discovery
agent-toolkit-cron --status --json
```

The status command is read-only: it neither creates directories nor launches
maintenance. Missing records on first installation are expected until jobs run.
The first status result exits 5; its JSON includes the following excerpt:

```json
{
  "status": "unknown",
  "exit_code": 5,
  "tools": [
    {"name": "cert-watch", "status": "missing"},
    {"name": "log-shipper", "status": "missing"},
    {"name": "system-janitor", "status": "missing"},
    {"name": "system-updater", "status": "missing"}
  ]
}
```

It reports **launcher invocations**, not the integrity or productivity of the
four native audit trails. A fresh exit-0 launch can still contain section warnings
or no work. Check native health separately:

```bash
system-janitor --health --json
system-updater --health --json
cert-watch --health --json
log-shipper --health --json
```

Status JSON keys: `status`, `exit_code`, `generated_at`, `state_dir`, `tools`.
Each `tools[]` entry contains `name`, `status`, `started_at`, `ended_at`,
`exit_code`, `age_seconds`, `max_age_seconds`, `summary_path`, `log_path`,
and `detail`. Unknown timestamps/ages/exit codes are `null`, not invented zeros.
Timestamps are timezone-aware ISO 8601. New fields may be added compatibly.

| Per-tool status | Meaning / recovery |
|---|---|
| `missing` | No launcher record. Check cron and installed paths, then wait for a scheduled launch. |
| `running` | Started but not completed, within freshness limit. Wait or inspect process and log. |
| `ok` | Fresh completed launch exited 0. Check native health independently. |
| `failed` | Fresh completed launch exited nonzero. Inspect startup log and fix its cause. |
| `stale` | Age exceeds the table's limit, based on completion (or start if running). Check cron daemon, offline time, or hung job. |
| `invalid` | Record is unreadable/malformed, exit code is invalid, timestamps are inconsistent/naive, or clock is >300 seconds in the future. Inspect state and clock. |

A successful new launch replaces a failed/stale record; never edit records to
manufacture success. The status command allows up to 300 seconds of clock skew,
clamping a small future age to zero. Freshness uses strict `age > limit`.
A stale record takes precedence over an old failure. Cron cannot catch up missed
runs while the host was offline.

**Status exits:** 0 `healthy` (all four fresh successes); 4 `degraded` (any
failed/invalid/stale record); 5 `unknown` (missing/running, with no degraded record).
JSON `exit_code` matches the process exit code. Human status is also available
without `--json`.

## Startup errors and bounded state

Launcher state defaults to `~/.local/state/agent-toolkit/cron/` (or
`$XDG_STATE_HOME/agent-toolkit/cron/` when an absolute XDG state path is set).
Each tool has `<tool>.json`, `<tool>.log`, and `<tool>.lock`. JSON is replaced
atomically; logs are append-only between rotations. Files are private to the
user. Logs rotate while streaming at 1 MiB, retaining three backups, so even a noisy
single launch stays within 4 MiB per tool. Oversized historical launcher files
are trimmed to their last 1 MiB before reuse.

```bash
tail -n 80 "${XDG_STATE_HOME:-$HOME/.local/state}/agent-toolkit/cron/system-updater.log"
systemctl is-active cron   # some distributions name the service crond
journalctl -u cron --since '2 hours ago'
```

The launcher captures stdout/stderr even if native config or log setup fails.
It accepts exactly one whitelisted tool name or its exact installed
`$HOME/.local/bin/<tool>` path, never arbitrary executables or extra arguments.
The installed path is the final cron argument so native doctor can resolve it.

**Launch exits:** original tool exit code; 1 concurrent launcher already owns
that tool; 3 launcher precondition/state failure; 126 executable denied; 127
executable missing; 128+signal on interruption. SIGTERM/SIGINT are forwarded to
the child process group; the launcher waits up to five seconds before killing
an unresponsive child and records the interruption, not a fake success.
SIGKILL/power loss may leave `running` until it becomes stale.

A broken launcher symlink or missing Python cannot create a fresh record; status
will remain missing or become stale. If launcher state itself is unwritable,
the error goes to cron stderr/mail rather than a nonexistent log; read-only
status also reports an unwritable or read-only state location as `invalid`.
Unusable per-tool log, lock, or backup paths are also `invalid`; an old successful
record does not hide a new inability to initialize those artifacts. Repair the
installation/state permissions and verify a subsequent launch. Do not treat
`--health-acknowledge` as a fix for missing jobs or idle workspace sections.

## Non-destructive verification

```bash
./tests/cron-smoke.sh      # isolated HOME, harmless stub executables
./tests/install-smoke.sh   # isolated crontab stub, migration and idempotency
```

Do not run `agent-toolkit-cron system-janitor` merely to test scheduling: that
performs real configured cleanup. Use `system-janitor --dry-run` for manual
preview; manual native runs do not create launcher evidence. To revert a live
schedule, inspect and restore the saved crontab with `crontab <backup-file>`.

## Optional systemd catch-up and disk-pressure guard

Standard cron cannot catch up missed runs when a laptop or workstation was
suspended across `03:17`, and a once-daily schedule cannot react to intra-day
`/tmp` build/container spikes. For systemd hosts, the repository ships an
optional companion script (`scripts/system-janitor-catchup.sh`) and user units
(`examples/systemd/system-janitor-catchup.{service,timer}`) that evaluate two
triggers every 5 minutes (`OnCalendar=*:0/5`, `Persistent=true`):

1. **Daily catch-up:** the latest `03:17` slot (after a `03:30` grace window)
   has no healthy real `last-run.json` or its `agent-toolkit-cron` record failed.
2. **Disk pressure:** root filesystem usage is `>= JANITOR_PRESSURE_THRESHOLD_PCT`
   (default `75%`), with a 15-minute post-cleanup cooldown
   (`JANITOR_PRESSURE_COOLDOWN_SEC=900`).

The fast path enforces **soft idle-only Go cache budgets**, including below
the pressure threshold: 16 GiB per shared cache, 2 GiB per session cache,
32 GiB combined. A cache needs five minutes without recent writes and no
Go process or open reference using it. Active caches can exceed the budget;
they are reported as deferred, never removed underneath a build.

Generated workspace directories must be ignored, at least an hour old,
and backed by a matching archive snapshot or clean, pushed worktree.
Immediately before removal the helper independently fetches the exact commit
and rechecks source, staging, processes and worker leases. Unsaved source,
unknown remote state, `.git`/`.jj` repositories, captured media and volumes
are preserved. No source is automatically published.
Generated trees containing credential configurations, databases, keys,
recordings or image files are conservatively retained, including dependency
trees that happen to bundle such files.

Pressure runs use this guarded path, **not repeated full Janitor sweeps**.
Only missed daily slots invoke `agent-toolkit-cron system-janitor`.
Daily catch-up also sweeps stale, unreferenced user-owned `/tmp` orphans;
source checkouts and mounted filesystems are excluded.

```bash
ln -sf "$PWD/scripts/system-janitor-catchup.sh" "$HOME/.local/bin/system-janitor-catchup"
mkdir -p "$HOME/.config/systemd/user"
cp examples/systemd/system-janitor-catchup.service examples/systemd/system-janitor-catchup.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now system-janitor-catchup.timer
system-janitor-catchup --status --json
```

Configure roots and budgets in
`~/.config/system-janitor/maintenance.json`; start from
[`examples/maintenance.example.json`](https://github.com/agent-frontier/agent-toolkit/blob/main/examples/maintenance.example.json).
`archive_roots` bounds receipt-backed projects, while `worktree_roots` lists
directories containing completed worker checkouts. New worktrees must be
clean and recoverable from their existing `origin` before artifacts qualify.
Dirty receipt-backed projects qualify only while source and staged trees
still match their saved snapshots.

`--status --json` is read-only and includes current cache budgets/activity
plus the previous workspace scan. `--dry-run --json` additionally plans
workspace artifacts without deleting them. Results go to
`~/.local/state/janitor/maintenance-last.json` (atomic) and
`maintenance.jsonl` (compact append-only events, rotated at 5 MiB with eight
backups), with actions, protected paths, errors and
observed reclaimed bytes. Missing tools and failed cleanup are visible errors,
not successful empty sweeps. Existing daily cron schedules stay unchanged.

When the guarded helper owns build cleanup on a continuously busy host, also
set these values in `~/.config/system-janitor/config`:

```bash
JANITOR_GO_CLEAN=no
JANITOR_TMP_GOBUILD_ORPHANS=no
JANITOR_WORKSPACE_DIRS=""
JANITOR_EXTRA_CLEANUP_DIRS=""
```

The native daily sections do not share the helper's activity, lease or archive
checks; leaving them enabled would bypass those protections overnight.
Container and aged-log cleanup retain their existing settings. Only eligible
roots in `maintenance.json` are managed by the helper; other build trees are
retained, not silently added to its deletion scope.

### Keeping continuous builds from defeating cleanup

An always-busy swarm can keep a cache above its soft budget indefinitely.
The optional user Go launcher, `scripts/go-with-disk-guard.py`, queues **new**
build/test/run/install work when root usage reaches `build_max_used_pct`
(default 85%) or free space falls below `build_min_free_gib` (20 GiB).
Existing and genuinely nested builds finish normally; a stale inherited
environment marker does not bypass admission. `go env`, `version`, `help`,
`clean`, and read-only module metadata commands never wait. No process is killed.

The launcher and cleaner use per-cache shared/exclusive locks, preventing
new participating builds from starting during cache removal. At the admission
ceiling, idle caches of at least 128 MiB can be reclaimed even below their
individual budgets. This leaves an idle window instead of repeatedly deferring.

Installing the launcher is opt-in: first preserve the original user Go
executable beside it as `go.agent-toolkit-real`, verify identical version
and `GOROOT`, and record its absolute path in
`~/.config/system-janitor/go-real-path`. Then point the user Go launcher at
the executable guard script. Never overwrite the original without a verified
backup. Restoring that backup removes the admission guard.

Queued builds expose only PID, working directory, command name and reason
under `~/.local/state/janitor/waiting-go-builds/`; command arguments and
credentials are not recorded. The catch-up status includes these records.
If protected data still leaves insufficient space, new work remains queued
rather than evicting active builds or unbacked source.
