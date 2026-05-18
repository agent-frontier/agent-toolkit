# Contributing to agent-toolkit

Thanks for contributing to `agent-toolkit`.

This project is agent-first, contract-oriented, and intentionally small:
two single-file Bash tools, shared machine-mode contracts, and smoke tests
that pin the agent-visible surface. Keep changes surgical, explicit, and easy
to audit.

## Quick orientation

Before changing code, read [`AGENTS.md`](AGENTS.md). Treat it as required
reading, not optional background.

Two repo rules matter on nearly every code change:

- **Three-part capability contract.** If you add an agent-visible feature
  (flag, capability, schema file, status enum value, report field, health
  field), you must implement it, add the capability string to
  `--version --json` `capabilities[]`, and add a smoke probe that proves the
  claim end to end.
- **Five-place sync rule.** If you add a new cleanup section, keep the section
  definition, defaults, `--help`, README/docs/examples, and smoke coverage in
  sync. `AGENTS.md` calls out the exact places.

If your change affects contracts, also review:

- [`README.md`](README.md) for the public project surface
- [`CHANGELOG.md`](CHANGELOG.md) for agent-visible release notes
- [`docs/agents/`](docs/agents/README.md) for machine-mode details

## Local development

Clone the repository and work from the repo root:

```bash
git clone https://github.com/agent-frontier/agent-toolkit.git
cd agent-toolkit
```

There is no build system. The smoke harnesses are the canonical local check:

```bash
./tests/smoke.sh
./tests/updater-smoke.sh
./tests/install-smoke.sh
./tests/bootstrap-smoke.sh
```

Useful additional checks:

```bash
bash -n system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
shellcheck -S warning system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
```

Run the smoke harnesses before you open a PR. If your change is scoped to one
tool, run that tool's smoke test first; before submission, run the full set.

## Style

This repo is Bash-first and cron-safe by design.

- Use `set -uo pipefail`; do not rely on `set -e` for control flow.
- Guard every external tool with `command -v ... >/dev/null 2>&1` before use.
- Preserve single-instance behavior with `flock` where the tool already uses it.
- Use atomic writes for monitored state (`*.tmp.$$` + `mv`), never append and
  truncate.
- Keep machine-mode outputs ANSI-free, stable, and parseable. No emoji, no
  decorative Unicode, no human-only formatting in JSON or JSONL paths.
- ShellCheck at `-S warning` is part of the contract, not a suggestion.

When in doubt, prefer explicitness over cleverness. The primary reader may be
an autonomous agent consuming output non-interactively.

## Commits

Keep commits small, reviewable, and contract-aware.

Good commits usually do one thing:

- add one feature and its capability/smoke/docs updates,
- fix one bug with a regression probe,
- or tighten one contract with matching schema/help/changelog updates.

If a PR adds an agent-visible feature, it is incomplete unless it also:

1. updates `capabilities[]` in the relevant `do_version()` output, and
2. adds or extends a smoke probe that exercises the new surface.

## Reviewing

Before asking for review, walk this checklist:

- Did I update the capability list if the surface changed?
- Did I add or update the smoke probe for that capability?
- If I added a section, did I honor the five-place sync rule?
- Are README, docs, examples, and `--help` aligned with the implementation?
- If schemas or JSON fields changed, did I update the schema files too?
- Did I add an appropriate [`CHANGELOG.md`](CHANGELOG.md) entry under
  `Unreleased` for agent-visible changes?
- Did I run the relevant smoke harnesses locally?

## AI agents contributing

AI agents are welcome contributors here.

If you are an autonomous or semi-autonomous coding agent:

- read [`AGENTS.md`](AGENTS.md) first,
- make the smallest complete change that satisfies the request,
- run the smoke harnesses,
- and produce a PR that passes CI.

Agent-generated changes are held to the same contract as human-written ones:
capabilities must be discoverable, machine-mode outputs must remain stable, and
smoke coverage must prove every claimed feature.
