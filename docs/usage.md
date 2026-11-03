# Usage

Day-to-day operations for humans. For the full machine-readable interface
(JSON output schemas, exit-code contract, capability strings), see the
[agent track](./agents/README.md).

## Run

```bash
# Run now (real, modifies state)
system-janitor

# Preview a run — log what would happen, modify nothing
system-janitor --dry-run
```

`--dry-run` still reports item counts and pre-state, so you can review
exactly what a real run would touch.

## Watch cleanup live

Terminal runs automatically show numbered sections, cleanup targets with
allocated sizes, live command output, and reclaimed-space totals. Use
`--verbose` (`-v`) to keep this output in pipes or non-interactive SSH:

```bash
system-janitor --dry-run --verbose   # inspect targets; nothing deleted
system-janitor --verbose            # real cleanup, logged and streamed
```

`--quiet` (`-q`) writes only to the log; cron and redirected runs stay quiet
by default. The last `-v`/`-q` wins. Color is terminal-only; `NO_COLOR=1`
disables it. JSON probes are unchanged.

Target sizes are allocated usage, **not guaranteed savings**. Reclaimed
totals measure the observed change on `/`, so concurrent writes, other
filesystems, and shared or open files can affect them. Dry-runs always
report zero reclaimed space. Failed removals appear as warnings in stdout
and the audit trail; check `warning sections` before trusting the total.

## Targeted cleanup with `--only`

`--only` runs just the named section(s) and silently skips the rest.
Useful when you want to free a specific toolchain's cache without waiting
for the full sweep.

```bash
# Preview unused Podman image pruning (containers and volumes are preserved)
system-janitor --only podman_prune --dry-run

# Multiple sections (comma-separated, no spaces)
system-janitor --only docker_prune,podman_prune,go_build_cache --dry-run
```

Notes:

- Valid names: `docker_prune`, `podman_prune`, `go_build_cache`,
  `tmp_gobuild_orphans`, `workspace_binobj`, `extra_cleanup`, `aged_logs`,
  `nuget_http_temp`.
- Execution order follows the **script's declaration order**, not the
  order you list them on the command line.
- `--only` composes with `--dry-run` and with the `JANITOR_*` config
  knobs: `--only` narrows the candidate set, and config still gates
  execution within that set.
- `run_start`, `run_end`, and `safety_integrity` always run.
- Unknown section names exit `3`.
- `--sections` is accepted as a synonym.

## Summarize history with `--report`

`--report` rolls up the full JSONL audit trail into a human-readable
summary (per-section totals, idle-streak detection, integrity issues).
Read-only — it does not acquire the lock, so it is safe to run while a
cleanup is in progress.

```bash
system-janitor --report
```

For agents and pipelines, `--report --json` emits the same data as a
single pretty-printed JSON object. See
[agents/machine-modes.md](./agents/machine-modes.md#--report---json) for the
full schema.

## Trust probe with `--health`

`--health` is a read-only check over `$JANITOR_LOG_DIR` that exits with a
code summarizing trust in the audit trail. It does not acquire the lock,
create the log dir, or write any file — safe to run any time, including
concurrently with a cleanup.

```bash
system-janitor --health
echo $?
# 0 healthy   — every check passes
# 4 degraded  — log dir exists, one or more downstream checks failed
# 5 unknown   — log dir missing or last-run.json absent (never run here)
```

`--health --json` emits a structured response — see
[agents/machine-modes.md](./agents/machine-modes.md#--health---json) for the
schema and [agents/recovery.md](./agents/recovery.md) for clearing
historical issues via `--health-acknowledge`.

## Version

```bash
system-janitor --version
# → system-janitor 0.2.0
```

`--version --json` is the agent-facing feature-detection surface — see
[agents/machine-modes.md](./agents/machine-modes.md#--version---json) and
[agents/contracts.md](./agents/contracts.md) for the capability contract.

## Inspect the audit trail

```bash
# Tail the human narrative log live
tail -f ~/.local/state/janitor/janitor.log

# Latest run summary (machine-readable, atomic-overwrite)
cat ~/.local/state/janitor/last-run.json | python3 -m json.tool

# Recent syslog records
journalctl -t system-janitor --since '30 days ago'
```

The full on-disk audit-log contract (JSONL event shape, status enum,
`last-run.json` schema) is documented in
[audit-trail.md](./audit-trail.md).

## Power-user raw queries

`--report` is the supported path, but `jq` over the raw JSONL works fine
for one-offs:

```bash
# Total bytes freed across all runs
jq -s '[.[] | select(.section=="run_end")] | map(.freed_kb) | add' \
   ~/.local/state/janitor/janitor.jsonl

# Any integrity violations, ever?
grep '"violated' ~/.local/state/janitor/janitor.jsonl
```

Logs rotate automatically at 5 MB with 8 backups kept.
