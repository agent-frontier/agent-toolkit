---
name: fleet-audit
description: "Audit a fleet running agent-toolkit. Collect --report --json across hosts and find silent failures, never-run sections, large idle streaks, and integrity violations."
---
# Fleet audit

Use this skill when one host is not enough.
Collect machine-mode output across the fleet.
Aggregate with `jq`.
Escalate outliers into per-host incident response.

## Ground rules

- Prefer `--report --json` for cross-host rollups.
- Prefer `--doctor --json` when you are validating install wiring.
- Prefer `--health --json` when you are validating trust.
- Keep destructive runs out of the fleet audit phase.
- Investigate outliers host-by-host after collection.

## Collection pattern

The basic ad-hoc collection command is:

```bash
ansible all -m shell -a 'system-janitor --report --json'
```

For fuller rollout and verification patterns, see recipe 2 in [`../../docs/cookbook.md`](../../docs/cookbook.md).
Use `system-janitor --doctor --json` after install work.
Use `system-janitor --report --json` for steady-state fleet audits.

If you need updater state too, collect it separately:

```bash
ansible all -m shell -a 'system-updater --report --json'
```

## Audit targets

A good fleet audit answers four questions.

1. Which hosts have silent failures?
2. Which hosts show integrity violations?
3. Which hosts are freeing far more than their peers?
4. Which hosts may never have run a section productively?

## Aggregation: idle streaks

Extract `idle_streaks` per host and look for shared stale config drift.

Example with Ansible JSON callback output:

```bash
ANSIBLE_STDOUT_CALLBACK=json \
ansible all -m shell -a 'system-janitor --report --json' \
| jq -r '
    .plays[].tasks[].hosts
    | to_entries[]
    | .key as $host
    | .value.stdout
    | fromjson
    | .idle_streaks[]?
    | [$host, .section, .consecutive_idle_runs]
    | @tsv
  '
```

Group by section when many hosts share the same drift:

```bash
ANSIBLE_STDOUT_CALLBACK=json \
ansible all -m shell -a 'system-janitor --report --json' \
| jq -s '
    map(.plays[].tasks[].hosts | to_entries[])
    | map({host: .key, report: (.value.stdout | fromjson)})
    | map(.report.idle_streaks[]? as $s | {host, section: $s.section, streak: $s.consecutive_idle_runs})
    | group_by(.section)
    | map({section: .[0].section, hosts: map(.host), max_streak: (map(.streak)|max)})
  '
```

Thresholds:

- `>= 3` consecutive idle runs: suspicious
- `>= 7` consecutive idle runs: almost certainly broken config

## Aggregation: integrity violations

Alert on any host whose most recent janitor run did not end `ok`.

```bash
ANSIBLE_STDOUT_CALLBACK=json \
ansible all -m shell -a 'system-janitor --report --json' \
| jq -r '
    .plays[].tasks[].hosts
    | to_entries[]
    | .key as $host
    | .value.stdout
    | fromjson
    | select((.most_recent_run.safety_integrity // "unknown") != "ok")
    | [$host, (.most_recent_run.safety_integrity // "unknown")]
    | @tsv
  '
```

Any hit here deserves host-level investigation.
Do not keep scheduling cleanup runs blindly.

## Aggregation: freed-bytes outliers

A host freeing 10x the fleet median may have runaway logs, runaway images, or stale buildup.
Find the heavy tail.

```bash
ANSIBLE_STDOUT_CALLBACK=json \
ansible all -m shell -a 'system-janitor --report --json' \
| jq -r '
    .plays[].tasks[].hosts
    | to_entries[]
    | .key as $host
    | .value.stdout
    | fromjson
    | [$host, (.total_freed_bytes // 0)]
    | @tsv
  '
| sort -k2,2n
```

Use this as a ranking pass.
Then inspect the top hosts' `per_section[]` breakdowns.

## Never-run and suspiciously-flat sections

Useful questions:

- does `per_section[]` omit a section you expect to see?
- does a section appear on every host but never free bytes?
- do the same opt-in sections stay idle across a cluster role?

When the pattern clusters by role, suspect config drift in automation.
When it clusters on one host, suspect a local path change.

## What to do with an outlier

Once you find an outlier host:

1. SSH to that host.
2. Run `system-janitor --doctor --json`.
3. Run `system-janitor --health --json`.
4. Follow [`../incident-response/SKILL.md`](../incident-response/SKILL.md).

Do not fix fleet anomalies from the aggregate view alone.
The rollup tells you where to look, not what to delete.

## Worked example: 50 hosts, 3 show `extra_cleanup` idle

Interpretation:

- the section is configured
- it is running
- it has produced no work for multiple real runs
- the config is probably stale on those three hosts

Suggested flow:

1. Collect janitor reports across all 50 hosts.
2. Extract `idle_streaks[]`.
3. Filter for `section == "extra_cleanup"`.
4. Note the 3 affected hosts.
5. SSH to one affected host.
6. Run:

```bash
system-janitor --doctor --json
system-janitor --report --json
system-janitor --dry-run
```

7. Inspect the configured directories.
8. Fix the stale `JANITOR_EXTRA_CLEANUP_DIRS` path in config management.
9. Re-run preview.
10. Let future real runs prove the streak is gone.

Do **not** disable `extra_cleanup` just to silence the report.
The report is telling you the config drift is real.

## Related cookbook recipes

Use these as companions:

- recipe 2: fleet install and verification
- recipe 4: `jq` one-liners for JSONL forensics

Both live in [`../../docs/cookbook.md`](../../docs/cookbook.md).

## Cross-references

- [`../../docs/cookbook.md`](../../docs/cookbook.md)
- [`../../docs/agents/contracts.md`](../../docs/agents/contracts.md)
- [`../../docs/agents/machine-modes.md`](../../docs/agents/machine-modes.md)
- [`../incident-response/SKILL.md`](../incident-response/SKILL.md)
- [`../../AGENTS.md`](../../AGENTS.md)
