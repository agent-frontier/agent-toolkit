# Agent track

This is the agent-facing track. The primary consumer of the agent-toolkit
project is autonomous LLM agents. Pages in this directory are
contract-shaped: regex-scrapeable, tabular, terse.

If you are a human looking for prose and onboarding, start at
[`../index.md`](../index.md) and [`../index.md`](../index.md).

## Pages

| Page | What's here |
|---|---|
| [`machine-modes.md`](./machine-modes.md) | Invocation, exit code, and stdout shape for every `--*-json` mode (`--report`, `--health`, `--health-acknowledge`, `--version`, plus `--only`/`--sections` composition). |
| [`contracts.md`](./contracts.md) | Frozen contracts: exit codes, `status` enum, capability discovery rules, the bytes/kb unit canon, and `SECTION_ALIASES` / `OBSOLETE_SECTIONS`. |
| [`recovery.md`](./recovery.md) | The `--health-acknowledge` recovery workflow: how to move from `degraded` back to `healthy` without rewriting an append-only audit log. |
| [`schemas.md`](./schemas.md) | JSON Schema files in [`schemas/`](https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/), how to validate, forward-compat policy. |
| [`capabilities.md`](./capabilities.md) | Single source of truth for every `--version --json` `capabilities[]` string across tools. Includes the JSON registry, semantics tables for shared/per-tool capabilities, and the "no-collision" guarantee enforced by [`tests/capabilities-check.sh`](https://github.com/agent-frontier/agent-toolkit/blob/main/tests/capabilities-check.sh). |
| [`toolkit-roadmap.md`](./toolkit-roadmap.md) | Candidate sibling tools that fit the ethos, plus the historical wedge that produced shipped `cert-watch.sh`. Selection criteria, 15-tool catalog, and next-tool menu. |

## Cross-references

- [`../audit-trail.md`](../audit-trail.md) — `janitor.jsonl` and
  `last-run.json` on-disk shape (the data these machine modes summarize).
- [`https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/`](https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/) — formal Draft 2020-12 JSON Schemas
  for `--report --json` and `--health --json`.
- [`../AGENTS.md`](../AGENTS.md) —
  design north star and architecture brief for agents working *on* the
  script (not just consuming its output).
- [`../CHANGELOG.md`](../CHANGELOG.md) — per-release record of
  agent-visible changes (flags, JSON fields, capabilities, exit codes).
