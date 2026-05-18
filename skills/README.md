# Skills

This directory contains reusable agent skills for working with `agent-toolkit`.
Each skill is a focused prompt bundle for a specific workflow.

## Table of contents

| Skill | Description | Load when |
|---|---|---|
| [`using-agent-toolkit`](./using-agent-toolkit/SKILL.md) | Operate the agent-toolkit (system-janitor disk cleanup, system-updater apt updates) on a Linux host. Use when the user asks to clean disk space, check audit trail, run system updates, or diagnose toolkit health. | An agent is using the installed tools on a host. |
| [`extending-the-toolkit`](./extending-the-toolkit/SKILL.md) | Add a new cleanup section, capability, or flag to the agent-toolkit while preserving its agent contract. Use when modifying system-janitor.sh or system-updater.sh. | An agent is editing toolkit source code or tests. |
| [`incident-response`](./incident-response/SKILL.md) | Respond to a Linux host incident using agent-toolkit: disk-full, update failure, safety-integrity violation, or silent-failure (idle streak) detection. Use during active incidents. | An agent is handling a live host problem. |
| [`cutting-a-release`](./cutting-a-release/SKILL.md) | Cut a new tagged release of agent-toolkit. Use when the user asks to release vX.Y.Z, prepare release notes, or publish to PyPI. | An agent is preparing or verifying a release. |
| [`fleet-audit`](./fleet-audit/SKILL.md) | Audit a fleet of Linux hosts running agent-toolkit: collect --report --json across hosts and find outliers (silent failures, never-run sections, large idle streaks, integrity violations). Use for fleet-wide health checks. | An agent is collecting and comparing multi-host state. |

## `SKILL.md` format

Each skill lives in its own directory:

```text
skills/<skill-name>/
  SKILL.md
  scripts/   # optional helper scripts referenced by the skill
```

`SKILL.md` uses Claude-style YAML frontmatter followed by a markdown body:

```yaml
---
name: skill-name-kebab-case
description: Imperative sentence telling the runtime when to load this skill.
---
```

Contributor rules:

- keep `name` kebab-case
- keep `description` concrete and agent-discoverable
- start `description` with an imperative verb
- keep `description` at or under 200 characters
- use repo-relative links
- prefer machine-mode examples over human-output scraping
- treat [`../AGENTS.md`](../AGENTS.md) as the source of truth

## Related docs

- [`../AGENTS.md`](../AGENTS.md)
- [`../docs/agents/README.md`](../docs/agents/README.md)
- [`../README.md`](../README.md)
