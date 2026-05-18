---
name: extending-the-toolkit
description: "Add a cleanup section, capability, or flag to agent-toolkit while preserving its agent contract. Use when modifying system-janitor.sh or system-updater.sh."
---
# Extending the toolkit

Use this skill when you are changing toolkit code, not merely operating it.
Assume AGENTS is the contract.
Prefer additive changes.
Preserve machine-mode behavior.

## First rules

- Read [`../../AGENTS.md`](../../AGENTS.md) first.
- Preserve cron-safety.
- Preserve audit-log shape.
- Preserve exit-code meaning.
- Preserve capability discovery.
- Preserve append-only history.

## Section changes: sync every required surface

AGENTS describes this as the five-place sync plus smoke coverage.
In practice, a new janitor section means these concrete edits:

1. `KNOWN_SECTIONS`
2. defaults block
3. `--help` text
4. README or relevant docs
5. `examples/config.example`
6. `tests/smoke.sh` `required_sections`

If the section is opt-in, also update:

- `OPTIN_SECTIONS`

If the section adds a new agent-visible feature, also do the capability work below.

## The three-piece section pattern

Every janitor section is three coordinated pieces.

1. `act_<name>()`
2. `count_<name>()`
3. `run_section "<name>" "$(count_<name>)" act_<name>`

Rules:

- `act_*` must early-return with a `[skip] ...` note when disabled or missing its tool.
- `act_*` must not `exit` the script.
- `count_*` echoes a shell expression string, not a numeric count.
- Empty count expression means “skip counting”.
- New sections must still flow through `run_section` so dry-run and JSONL accounting stay correct.

## Capability changes: three-part rule

When you add a new agent-visible affordance, do all three steps.

1. Implement the feature.
2. Add its capability string to `do_version()`.
3. Add a smoke probe in the `tests/smoke.sh` capability-completeness stage.

Do not stop at implementation.
A feature with no capability string breaks feature detection.
A capability string with no smoke probe breaks the suite.

## Delimiter banners for parallel-edit safety

New flags must use the banner convention.

```bash
# ── --flagname flag ──
...
# ── end --flagname flag ──
```

Apply the banners consistently in three places:

- CLI parse arm
- validation block
- `do_<flag>()` function

This keeps concurrent edits surgical.

## Read-only vs mutating flags

Read-only flags must run before:

- flock
- log-dir creation
- exec redirect

Current read-only family:

- `--version`
- `--health`
- `--report`
- `--doctor`
- `--health-acknowledge`

Mutating paths lock first.
Normal janitor and updater runs are mutating paths.
Do not move read-only flags below the lock.

## Frozen enums and append-only schemas

The janitor `status` enum is frozen:

- `ok`
- `idle`
- `warn`
- `dry_run`
- `violated_missing`
- `violated_inode_changed`

Adding a new value is a breaking change.
Treat it that way.

The JSONL schema is append-only.

Allowed:

- add fields
- add docs
- add smoke coverage

Breaking:

- rename fields
- remove fields
- change meaning of frozen values

## Worked example: hypothetical `journal_vacuum` section

Use this pattern when adding a janitor section.

### 1. Action function

Add:

```bash
act_journal_vacuum() {
  command -v journalctl >/dev/null 2>&1 || { echo '[skip] journalctl missing'; return 0; }
  [ "${JANITOR_JOURNAL_VACUUM:-no}" = yes ] || { echo '[skip] JANITOR_JOURNAL_VACUUM=no'; return 0; }
  journalctl --vacuum-time="${JANITOR_JOURNAL_VACUUM_TIME:-7d}"
}
```

### 2. Count function

Add:

```bash
count_journal_vacuum() {
  echo "journalctl --disk-usage 2>/dev/null"
}
```

If you cannot count meaningfully, return `""` and let `run_section` skip item counting.

### 3. Section runner

Add the runner in declaration order near the bottom:

```bash
run_section "journal_vacuum" "$(count_journal_vacuum)" act_journal_vacuum
```

### 4. Sync the section list and config surfaces

Update all of these:

- `KNOWN_SECTIONS`
- defaults block with `JANITOR_JOURNAL_VACUUM`
- `--help`
- README or relevant docs
- `examples/config.example`
- `tests/smoke.sh` `required_sections`

If the section is opt-in instead of default-safe, also update `OPTIN_SECTIONS`.

### 5. Decide whether this introduces a capability

If `journal_vacuum` only adds a section and no new agent-visible surface, no new capability is needed.
If it ships a new flag or report field, add the capability string and smoke probe.

### 6. Extend smoke coverage

For a pure section addition, make sure smoke proves:

- the new section appears exactly once in dry-run JSONL
- the dry-run status loop covers it
- `last-run.json.sections[]` includes it

If a new capability was introduced, add a dedicated capability-completeness probe.

## Flag changes checklist

When adding a flag:

1. add CLI parse arm
2. add validation
3. add `do_<flag>()`
4. add help text
5. add docs
6. add capability string if agent-visible
7. add smoke probe
8. keep read-only flags above lock setup when appropriate

## Updater-specific reminders

The updater shares the same contract style.
Preserve:

- explicit `--apply`
- default preview behavior
- maintenance-window exit `6`
- per-package status logging
- `updater-last-run.json` atomic writes

Do not add updater-only semantics to janitor docs unless they are truly shared.

## Tests to run before you claim completion

Run all four requested harnesses:

```bash
./tests/smoke.sh \
&& ./tests/updater-smoke.sh \
&& ./tests/install-smoke.sh \
&& ./tests/bootstrap-smoke.sh
```

Recommended extra gate when capability strings moved:

```bash
./tests/capabilities-check.sh
```

If you changed scripts directly, also run:

```bash
bash -n system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
shellcheck -S warning system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
```

## Cross-references

- [`../../AGENTS.md`](../../AGENTS.md)
- [`../../docs/agents/README.md`](../../docs/agents/README.md)
- [`../../docs/agents/contracts.md`](../../docs/agents/contracts.md)
- [`../../docs/agents/capabilities.md`](../../docs/agents/capabilities.md)
- [`../../docs/agents/machine-modes.md`](../../docs/agents/machine-modes.md)
- [`../../README.md`](../../README.md)
