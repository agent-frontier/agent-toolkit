---
name: using-agent-toolkit
description: "Operate agent-toolkit on a Linux host. Use when asked to clean disk space, inspect audit trails, run apt updates, or diagnose janitor/updater health."
---
# Using agent-toolkit

Use this skill when you are operating an installed toolkit on a Linux host.
Stay capability-first.
Prefer machine modes.
Preview destructive work before applying it.

## First rules

- Read [`../../AGENTS.md`](../../AGENTS.md) for the project contract.
- Machine-readable output beats scraping human text.
- `system-janitor` and `system-updater` are sibling tools.
- `system-janitor` has `--doctor`; `system-updater` does not.
- For optional features, probe `capabilities[]` first.
- Do not grep `--help` for feature detection.

## Binary discovery

Resolve binaries in this exact order.

1. Env override:
   - `AGENT_TOOLKIT_SYSTEM_JANITOR_BIN`
   - `AGENT_TOOLKIT_SYSTEM_UPDATER_BIN`
2. User bin:
   - `~/.local/bin/system-janitor`
   - `~/.local/bin/system-updater`
3. Toolkit checkout:
   - `$AGENT_TOOLKIT_HOME/system-janitor.sh`
   - `$AGENT_TOOLKIT_HOME/system-updater.sh`
4. Installer manifest:
   - `~/.config/agent-toolkit/manifest.json`
   - read `tools[].binary`
5. `PATH`

If discovery is ambiguous, prefer the installer-managed path in `~/.local/bin`.
If the manifest exists, use it as the fast answer for install root and source paths.

## Capability discovery pattern

Always start here:

```bash
system-janitor --version --json
system-updater --version --json
```

Read:

- `name`
- `version`
- `capabilities[]`
- top-level `toolkit`

Decision rule:

1. Run `--version --json`.
2. Check `capabilities[]`.
3. Invoke optional flags only when the capability is present.
4. Fall back cleanly when absent.

Example:

```bash
system-janitor --version --json \
| python3 -c 'import json,sys; print("doctor" in json.load(sys.stdin)["capabilities"])'
```

Never do this:

```bash
system-janitor --help | grep doctor
```

## Machine modes you should know

### `system-janitor --report --json`

Use for audit-trail rollups.
Returns a single JSON object.
Key fields include:

- `total_events`
- `total_runs`
- `real_runs`
- `dry_runs`
- `total_freed_bytes`
- `per_section[]`
- `most_recent_run`
- `data_quality`
- `idle_streaks[]`

Use this to answer:

- How much space has janitor freed?
- Which section is doing the work?
- Are any opt-in sections silently stale?
- Did the last run end with `safety_integrity: ok`?

### `system-janitor --health --json`

Use for trust, not history.
Returns:

- `status`
- `exit_code`
- `generated_at`
- `log_dir`
- `checks[]`

Exit codes matter:

- `0`: healthy
- `4`: degraded
- `5`: unknown

Interpretation:

- `healthy`: audit trail is trustworthy.
- `degraded`: tool is usable, but read failures carefully.
- `unknown`: no trustworthy signal yet; this is not the same as failure.

### `system-janitor --doctor --json`

Use for install and wiring diagnostics.
Returns:

- `status`
- `exit_code`
- `generated_at`
- `script_path`
- `checks[]`

The current checks cover:

- `script_path_resolves`
- `cron_entry_present`
- `config_parses`
- `configured_dirs_exist`

Use this when an agent needs to answer:

- Is janitor installed correctly?
- Is cron wired?
- Does config parse?
- Do configured directories still resolve?

### `--version --json`

Use for runtime feature detection.
This is the first command, not the last one.
It returns:

- `name`
- `version`
- `capabilities[]`
- `toolkit`

## Safe run discipline

For cleanup, preview first.
For updates, preview first.

### Janitor preview, then apply

```bash
system-janitor --dry-run
system-janitor
```

Targeted preview is better than disabling many env vars:

```bash
system-janitor --only docker_prune,podman_prune,go_build_cache --dry-run
system-janitor --only docker_prune,podman_prune,go_build_cache
```

### Updater preview, then apply

`system-updater` defaults to preview mode unless `--apply` is present.
Use the explicit form anyway when you want the transcript to be obvious.

```bash
system-updater --dry-run
sudo system-updater --apply
```

If maintenance windows exist, `--force` only bypasses that gate.
It does not bypass root requirements or holds.

## O(1) “what just happened” lookups

Prefer the latest snapshot over reparsing the whole JSONL when you only need the last run.

### Janitor

Default path:

```text
~/.local/state/janitor/last-run.json
```

Read these first:

- `run_id`
- `started_at`
- `ended_at`
- `freed_kb`
- `safety_integrity`
- `dry_run`
- `sections[]`

`sections[]` is the fast per-section summary.
Each entry carries `name`, `status`, `items`, and `freed_bytes`.

### Updater

Default path:

```text
~/.local/state/system-updater/updater-last-run.json
```

Use it for the most recent update sweep before opening the full report.

## Worked example: “disk is filling up”

Treat this as the standard playbook.

1. Diagnose wiring first.

```bash
system-janitor --version --json
system-janitor --doctor --json
```

2. If `doctor` is healthy, preview janitor.

```bash
system-janitor --dry-run
```

3. Inspect the preview snapshot.

```bash
python3 -c 'import json,pathlib; p=pathlib.Path.home()/".local/state/janitor/last-run.json"; d=json.loads(p.read_text()); print(d.get("freed_kb",0), d.get("sections",[]))'
```

4. Eyeball which sections would free the most bytes.
5. If the preview looks safe, run janitor for real.

```bash
system-janitor
```

6. Confirm outcome from machine mode.

```bash
system-janitor --report --json
system-janitor --health --json
```

7. Confirm the host view too.

```bash
df -h
```

If `--health --json` returns exit `4`, inspect `checks[]` before running more cleanups.
If the last run ended with `safety_integrity != "ok"`, stop and investigate.

## Common operator patterns

### Check whether a host has ever run janitor

```bash
system-janitor --health --json
```

Exit `5` means “unknown / never produced trustworthy state here yet”.
Do not treat that as a hard failure.

### Ask what updater can do on this host

```bash
system-updater --version --json
```

Look for:

- `security-only`
- `exclude`
- `maintenance-window`
- `health-acknowledge`

### Read the long history, not just the last run

```bash
system-updater --report --json
system-janitor --report --json
```

### Recover from historical malformed JSONL noise

```bash
system-janitor --health --json
system-janitor --health-acknowledge --json
system-janitor --health --json
```

Only use acknowledgement for historical `jsonl_parses` noise.
Do not use it to hide live problems.

## Cross-references

- [`../../AGENTS.md`](../../AGENTS.md)
- [`../../docs/agents/README.md`](../../docs/agents/README.md)
- [`../../docs/agents/contracts.md`](../../docs/agents/contracts.md)
- [`../../docs/agents/machine-modes.md`](../../docs/agents/machine-modes.md)
- [`../../docs/agents/capabilities.md`](../../docs/agents/capabilities.md)
- [`../../docs/agents/recovery.md`](../../docs/agents/recovery.md)
- [`../../docs/agents/schemas.md`](../../docs/agents/schemas.md)
- [`../../README.md`](../../README.md)
- [`../../mcp-server/README.md`](../../mcp-server/README.md)
