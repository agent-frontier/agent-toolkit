---
name: incident-response
description: "Respond to Linux host incidents with agent-toolkit. Use during disk-full events, update failures, safety-integrity violations, or silent-failure idle-streak alerts."
---
# Incident response

Use this skill during an active host problem.
Bias toward read-only probes first.
Do not improvise destructive cleanup.
Do not hide integrity failures.

## Initial stance

- Read [`../../AGENTS.md`](../../AGENTS.md) before mutating anything.
- Start with host reality, then consult the toolkit.
- Use machine modes before reading raw logs.
- Keep cleanup preview-first.
- Preserve the safety floor.

## Disk-full triage

Run the host-level checks first.

```bash
df -h
du -xhd1 / | sort -h
```

Goal:

- confirm which filesystem is full
- identify the biggest top-level consumer
- decide whether janitor is relevant

Before running janitor:

- consult [`../../AGENTS.md`](../../AGENTS.md)
- do **not** manually delete from safety-floor directories
- do **not** delete random paths just because they are large

Then use janitor safely.

```bash
system-janitor --version --json
system-janitor --doctor --json
system-janitor --dry-run
```

If the preview looks sane, apply it:

```bash
system-janitor
```

Verify both views after the real run:

```bash
df -h
system-janitor --report --json
```

If free space did not improve, stop guessing.
Re-evaluate the large consumer.
Janitor only handles the sections it knows about.

## Safety-integrity violation

Janitor exit `2` means a safety-floor failure.
Interpret it literally.

Meaning:

- a safety-floor directory disappeared, or
- a safety-floor directory changed inode during the run

Response:

1. Stop running more cleanups.
2. Read the latest summary.
3. Identify which directory moved or vanished.
4. Investigate why.
5. Only rerun after the underlying issue is fixed.

Useful commands:

```bash
system-janitor --report --json
system-janitor --health --json
python3 -c 'import json,pathlib; p=pathlib.Path.home()/".local/state/janitor/last-run.json"; d=json.loads(p.read_text()); print(d.get("safety_integrity"), d.get("sections", []))'
```

Do **not** wave this through.
Do **not** treat exit `2` like a transient warning.

## Update failure

For updater incidents, start from its audit trail.

```bash
system-updater --version --json
system-updater --report --json
system-updater --health --json
```

Look for:

- failed packages in the most recent run
- held or excluded packages that explain “nothing happened”
- maintenance-window refusal
- degraded health that makes the audit trail suspect

The updater audit log is `updater.jsonl`.
Use janitor's `janitor.jsonl` only when the incident is about cleanup, not package updates.

If the operator wants to retry updates, preview again first:

```bash
system-updater --dry-run
```

Only apply intentionally:

```bash
sudo system-updater --apply
```

## Silent failure: idle streak detection

An opt-in janitor section reporting `idle` on real runs is a signal.
A trailing streak `>= 2` means likely stale config.

Interpretation:

- the section ran
- it found zero work
- it freed zero bytes
- the path is likely wrong or obsolete

Action:

1. inspect `idle_streaks[]` in `system-janitor --report --json`
2. identify the stale section
3. fix the config path
4. rerun preview
5. confirm the streak stops on future runs

Do **not** “fix” this by disabling the section unless the workload is truly gone.
The intended fix is config repair.

Thresholds worth using:

- `>= 2`: suspicious
- `>= 5`: degraded health signal
- `>= 7`: almost certainly broken config

## `--health-acknowledge` recovery flow

Use acknowledgement only for one class of issue:

- historical malformed JSONL lines pinned in `jsonl_parses`

Workflow:

```bash
system-janitor --health --json
system-janitor --health-acknowledge --json
system-janitor --health --json
```

Policy:

- acknowledge historical noise only
- do not acknowledge live failures
- do not acknowledge integrity violations
- do not acknowledge long idle streaks
- do not acknowledge corrupt `last-run.json`

If `--health --json` is degraded for anything except historical `jsonl_parses`, fix the cause instead.

## Worked example: page says a host is at 97%

Use this sequence.

1. Confirm pressure.

```bash
df -h
```

2. Locate the biggest top-level consumer.

```bash
du -xhd1 / | sort -h
```

3. If the pressure looks like caches, containers, Go build output, or configured janitor targets, consult janitor.

```bash
system-janitor --version --json
system-janitor --doctor --json
system-janitor --dry-run
```

4. Read the preview snapshot.
Check which sections would free meaningful space.

5. If the preview is safe, run janitor for real.

```bash
system-janitor
```

6. Verify.

```bash
df -h
system-janitor --report --json
system-janitor --health --json
```

What **not** to do:

- do not `rm -rf` inside safety-floor directories
- do not keep rerunning janitor after exit `2`
- do not use `--health-acknowledge` to hide active failures
- do not skip the preview when the host is still responsive enough to run one

## Cross-references

- [`../../AGENTS.md`](../../AGENTS.md)
- [`../../docs/agents/recovery.md`](../../docs/agents/recovery.md)
- [`../../docs/agents/contracts.md`](../../docs/agents/contracts.md)
- [`../../docs/agents/machine-modes.md`](../../docs/agents/machine-modes.md)
- [`../../README.md`](../../README.md)
