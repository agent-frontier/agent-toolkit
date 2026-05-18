# agent-toolkit vs the alternatives

> Honest positioning. We'll tell you where we don't fit before we tell
> you where we do.

This page is for the evaluator asking a real question:

- Do I write a 30-line shell script and move on?
- Do I deploy a config-management system and stop thinking locally?
- Do I use the distro tool that already exists?
- Do I need an agent-readable trail badly enough to care about the contract?

Sometimes the right answer is "not us." That is fine.

## TL;DR table

| Need | Best tool |
|---|---|
| One-off cleanup script on a single laptop | bash + cron (skip us) |
| Fleet of 100+ servers needing identical state | Ansible / Salt / Puppet + agent-toolkit as a leaf |
| Agent runtime needs to know what changed on a host | **agent-toolkit** |
| Multi-OS (Windows/macOS) management | not us (Linux-only by design) |
| Always-on monitoring with alerting | Prometheus + node_exporter; agent-toolkit exports complementary signal |
| Single source of truth for apt/dnf/snap | unattended-upgrades, dnf-automatic; we wrap apt with an agent-readable trail |

## The short version

`agent-toolkit` is not a general management plane. It is not a replacement for
config management, Prometheus, or `logrotate`. It is not cross-platform.

It is a small Linux-first leaf toolset with a strict contract for agents:
capability discovery via `--version --json`, stable machine-readable state,
append-only JSONL audit trail, atomic last-run snapshot writes, dry-run-first
destructive paths, and frozen exit codes and schemas.

If that contract does not matter in your environment, you probably do not need
this project.

## vs. cron + bespoke shell scripts

For one engineer, one host, and one cleanup rule, bespoke shell wins on
simplicity.

If your goal is "free some disk by pruning Docker once a night," the straight
answer is: write the tiny script, add a cron entry, and move on. You will
finish faster than evaluating a toolkit.

Where that starts to break is not scale in the abstract. It is when the
consumer stops being a human reading syslog and starts being an agent that
needs structured truth: what ran, what did not run, what it freed, whether the
run was dry-run or real, whether a configured section has been silently idle
for days, and whether the last snapshot is safe to read mid-run.

A bespoke script can do all of that. It just stops being a 30-line script when
you actually implement it.

### Docker cache cleanup: bespoke script

A common example looks like this:

```bash
#!/usr/bin/env bash
set -euo pipefail
PATH=/usr/local/bin:/usr/bin:/bin

before=$(df -Pk / | awk 'NR==2 {print $3}')
items=$(docker system df -v 2>/dev/null | grep -c 'sha256:' || true)
docker system prune -af --volumes
after=$(df -Pk / | awk 'NR==2 {print $3}')
freed_kb=$((before - after))

echo "$(date -Is) docker_prune items=$items freed_kb=$freed_kb" \
  >> "$HOME/.local/state/docker-prune.log"
```

That is fine. It is also only the beginning.

Missing from the tiny version:

- dry-run preview
- stable JSON output for an agent
- capability discovery
- lock handling
- atomic last-run snapshot
- read-while-running append-only audit log
- integrity check on monitored paths
- stale-config detection across runs
- documented exit-code contract

If you add those, you are building a local version of the problem this repo
already solves.

### The same operation in agent-toolkit

The janitor side does not just prune Docker. It wraps the operation in a
contract an agent can consume without scraping human text:

```bash
system-janitor --version --json
system-janitor --dry-run --only docker_prune
system-janitor --report --json
python3 -c 'import json;print(json.load(open("$HOME/.local/state/janitor/last-run.json"))["sections"])'
```

What the agent gets back is not "some log lines." It gets a capability list
before invocation, a dry-run event with item count and freed-space estimate,
JSONL events safe to tail during the run, `last-run.json` written atomically
after the run, `safety_integrity` checked on every run, and `idle_streaks` in
`--report --json` for opt-in sections that have gone quiet for consecutive
runs.

### The honest trade-off

| Dimension | Bespoke shell | agent-toolkit |
|---|---|---|
| Initial LOC for one cleanup | ~10-30 | 0 custom LOC if our sections fit |
| Human readability | excellent | good |
| Agent readability | you build it | built in |
| Failure modes | your responsibility | contract already defined |
| Silent stale config detection | usually absent | built in (`idle` / `idle_streaks`) |
| Mid-run read safety | you build it | built in |
| Cross-host consistency | ad hoc | same interface on every host |

A rough way to think about it:

- If you want one action on one machine: write the shell.
- If you want an unsupervised agent to reason over repeated runs across multiple
  hosts: use the tool with the contract already there.

## vs. Ansible / Salt / Puppet / Chef

Different problem.

Configuration management answers: what should be installed, what config file
should exist, what cron job or systemd timer should be present, and how 100
hosts should converge on the same desired state.

`agent-toolkit` does not do that. It is a leaf tool that a config-management
system can deploy. That is the right shape.

Use Ansible / Salt / Puppet / Chef to install the toolkit, pin a version, ship
config, and schedule runs. Then let agents consume the local JSONL and
snapshots on each host.

### Ansible example: install as a leaf

```yaml
- name: Install agent-toolkit pinned to a release
  shell: |
    curl -fsSL \
      https://raw.githubusercontent.com/agent-frontier/agent-toolkit/v0.1.0/scripts/bootstrap.sh \
      | bash -s -- --ref=v0.1.0
  args:
    creates: "{{ ansible_env.HOME }}/agent-toolkit/system-janitor.sh"

- name: Configure janitor
  copy:
    src: files/janitor.config
    dest: "{{ ansible_env.HOME }}/.config/system-janitor/config"
    mode: "0644"
```

That is the pattern we recommend. CM owns distribution and desired state.
`agent-toolkit` owns local execution, local audit trail, and the
agent-readable contract.

If you already have Ansible and all you need is periodic package updates or
cache cleanup, you may never need this repo directly. Use it when the host-local
trail matters.

## vs. unattended-upgrades / dnf-automatic / yum-cron

If you want unattended package updates on Debian/Ubuntu and the only consumer is
a human operator reading logs later, `unattended-upgrades` is a great answer.

Same for `dnf-automatic` and the distro-native equivalents in their own worlds.
These tools exist for a reason. They are closer to the package manager, more
mature in that narrow lane, and often already blessed by your distribution.

Where `system-updater.sh` is different is not the backend logic. It is that the
apt run is wrapped in an agent-readable trail:

- `--version --json` for feature detection
- `--report --json` for rollups and per-package history
- `--health --json` for trust checking the local trail
- frozen exit codes for retry / escalate decisions
- formal schemas under `schemas/`
- append-only JSONL plus atomic `updater-last-run.json`

That matters if an agent is deciding which packages were held, which were
excluded, which were security-only filtered, whether the last run was dry-run
or apply, and whether the host now needs a reboot.

It matters much less if your answer to "did updates run?" is "I'll check
`/var/log/apt/` myself."

One important honest detail: today the updater surface is centered on
`--report`, `--health`, `--version`, and the audit trail. If you came here
specifically for a janitor-style `--doctor`, that affordance exists on
`system-janitor` today, not on the updater.

So the real guidance is:

- **Human-only apt automation:** use distro-native tooling first.
- **Agent-readable apt history with a stable contract:** use `system-updater`.
- **Need dnf/zypper today:** not us yet.

## vs. logrotate

For log rotation, compression, copytruncate, size thresholds, postrotate hooks,
and decades of battle hardening, use `logrotate`.

Seriously. That is what `logrotate` is for.

`agent-toolkit` does not replace it.

The janitor's `aged_logs` section is narrower: delete files older than `N` days
from configured directories. That is useful when you have throwaway log trees,
exporter dumps, transient build artifacts, or application-side directories that
are not part of a full logrotate policy.

A good rule:

- Need log lifecycle management for real logs? `logrotate`.
- Need "delete stale files older than 7 days under these scratch directories"
  with the same janitor contract as everything else? `aged_logs`.

Those tools complement each other cleanly.

## vs. cleanmgr (Windows) / cleanup tools on macOS

Not applicable.

This project is Linux-only on purpose. That is not an omission we forgot to get
around to. It is a design choice: the project optimizes for readable-source
Bash tools operating on Linux servers, not for a least-common-denominator
cross-OS veneer.

If you need Windows or macOS fleet management, skip this project and use
something native to those platforms or something genuinely cross-platform.

For the rationale, see the decision log in
[docs/agents/toolkit-roadmap.md](./agents/toolkit-roadmap.md).

## vs. building your own MCP server

If your main need is "surface my own internal tools to an agent runtime through
MCP," you should probably build your own MCP server.

That is especially true when the real thing you need is not cleanup or apt
updates at all, but your deploy system, your service catalog, your incident
runbooks, or your internal ticketing APIs.

What ships here is narrower:

- an MCP server for `system-janitor`
- an MCP server for `system-updater`
- a reference implementation for exposing small host-local tools with a thin,
  typed wrapper

If your runtime already speaks MCP and you want janitor + updater exposed there,
use ours. If you want MCP for your custom deploy script, your in-house
maintenance API, or your bespoke backup verifier, write your own and use
`mcp-server/src/agent_toolkit_mcp/` as a reference.

This is one of the places where "not us" is the correct answer.

## When NOT to use agent-toolkit

- You have one personal machine and 5 minutes of free disk. Use `du | sort -h`
  and skip us.
- You need Windows or macOS support. Linux-only on purpose.
- You need a centralized management plane. Use Ansible / Salt / Puppet; let `agent-toolkit` be the leaf.
- You need real-time monitoring and alerting. Use Prometheus. We emit
  complementary JSONL signal; we are not an alerting system.
- You need a package manager abstraction across apt, dnf, zypper, snap, and
  Homebrew. Not us.
- You want the smallest possible moving part for one cleanup command. A hand-written script is smaller.

## Where agent-toolkit's contract genuinely matters

This project earns its keep only when the contract is the point.

### 1. Autonomous agents reading host state

An agent should not have to scrape `--help` text or regex syslog just to learn
what a tool can do. Here it can feature-detect first via `--version --json`,
`capabilities[]`, and schemas under `schemas/`.

That matters once you have more than one host, more than one version in flight,
or more than one tool in the same family.

### 2. Silent failure detection

Bespoke scripts usually fail in two obvious ways and one dangerous way. The
obvious ways are "command failed" and "cron never ran." The dangerous way is
"the script kept running fine against a path that no longer matches anything,
and nobody noticed for three weeks."

The janitor contract addresses that with `idle` status and `--report --json`
`idle_streaks` for opt-in sections. That is not glamorous. It is exactly the
kind of boring signal that saves an autonomous agent from making bad
assumptions.

### 3. Atomicity guarantees

`last-run.json` is write-temp-then-rename. A monitoring process or agent never
has to guess whether it caught the file halfway through a write.

If you build your own tool and do `> last-run.json` directly, you now own that
edge case.

### 4. Read-while-running JSONL

The audit log is append-only, one event per line. That means a shipper, tailer,
exporter, or agent can consume it while a run is still happening.

Again: boring, operationally useful, easy to get wrong if you improvise.

### 5. Exit codes as control flow

Humans can tolerate fuzzy logs. Unsupervised agents cannot.

A frozen exit-code contract is what lets an agent decide: retry later, fix
invocation, escalate integrity issue, proceed, or treat this as "no signal
yet," not failure.

That is why the repo is opinionated about exit codes and machine modes.

## So when should you actually choose it?

Choose `agent-toolkit` when most of these are true:

- Linux hosts are the target.
- Cron-driven local operations are acceptable.
- You want tiny, readable source, not a daemon and database.
- An agent or other machine consumer needs structured local state.
- You care about auditability more than feature breadth.
- You are happy to let other tools own adjacent concerns.

Do not choose it because you want a universal ops platform. It is intentionally
smaller than that.

## Further reading

- [AGENTS.md](../AGENTS.md) — the full agent contract.
- [docs/agents/](./agents/README.md) — machine-mode specs.
- [docs/cookbook.md](./cookbook.md) — recipes for fleet ops. This file will exist after this round.
- [docs/promotion-roadmap.md](./promotion-roadmap.md) — what's shipped and what's next. This file will exist after this round.
