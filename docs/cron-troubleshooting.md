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
triggers every 15 minutes (`OnCalendar=*:0/15`, `Persistent=true`):

1. **Daily catch-up:** the latest `03:17` slot (after a `03:30` grace window)
   has no healthy real `last-run.json` or its `agent-toolkit-cron` record failed.
2. **Disk pressure:** root filesystem usage is `>= JANITOR_PRESSURE_THRESHOLD_PCT`
   (default `75%`), gated by a 4-hour post-run cooldown
   (`JANITOR_PRESSURE_COOLDOWN_SEC=14400`) so persistent non-janitor disk usage
   cannot thrash warm caches or inflate opt-in `idle_streaks`.

When neither trigger fires, the unit exits `0` without invoking `system-janitor`.
When triggered, it sweeps stale (`>=1h` deepest-tree `mtime`, unreferenced by
`/proc` `cwd`/`exe`/`fd`/`cmdline`) user-owned `/tmp` orphans and invokes
`agent-toolkit-cron system-janitor`:

```bash
ln -sf "$PWD/scripts/system-janitor-catchup.sh" "$HOME/.local/bin/system-janitor-catchup"
mkdir -p "$HOME/.config/systemd/user"
cp examples/systemd/system-janitor-catchup.service examples/systemd/system-janitor-catchup.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now system-janitor-catchup.timer
system-janitor-catchup --status --json
```
