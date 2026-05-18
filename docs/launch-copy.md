# Launch copy

> Pre-written promotional copy ready to post once `v0.1.0` is tagged.
> Each variant is tuned to its audience.

Tracking: see [`promotion-roadmap.md`](promotion-roadmap.md) Phase 2 for
the submission queue.

---

## Show HN

**Title** (≤80 chars; HN truncates):

```
Show HN: Agent-toolkit – Linux cron tools with a machine-readable audit trail
```

**Alternative titles** (pick whichever lands better):

- `Show HN: Agent-first cron toolkit for Linux servers`
- `Show HN: Cron tools with JSONL audit trail, --dry-run defaults, MCP server`

**URL field**: `https://github.com/agent-frontier/agent-toolkit`

**Body** (HN allows short post; keep tight):

```
Two Bash tools that quietly keep a Linux server alive:

- system-janitor — disk/cache cleanup with safety floor + integrity check
- system-updater — apt update sweep with rollback awareness

What makes them different from the usual cron+shell hack: every run emits
JSONL (append-only, schema-pinned), capabilities are discoverable via
--version --json (no scraping --help), and there's an MCP server so
Claude/Cursor/Continue/Cline can call them directly.

The design north star is "the primary consumer is an autonomous LLM
agent operating without supervision". So:

- --dry-run is default; destructive runs need an explicit flag
- atomic state writes (write-temp-then-rename) so monitors never see torn files
- --doctor self-check that flags dangling symlinks, missing cron, stale config
- --health-acknowledge for recovering from historical noise in the audit log
- exit codes are a public contract (0/1/2/3/4/5, frozen)

Started as a personal-server janitor that ran a few weekends. After
filling 99% on a real host and watching a Claude agent try to clean it
up with `find -delete`, I rebuilt it for agents first, humans second.
Linux-only by design.

Apache 2.0. ~3300 LOC of Bash. Five smoke harnesses in CI.

Curious whether the JSONL-first contract resonates with anyone else
running agents that touch production Linux.
```

**Comment-prepared answers** (likely questions; first-comment yourself
or be ready to answer):

- *Why Bash and not Go?* Decision log lives in
  `docs/agents/toolkit-roadmap.md`. Of 15 candidate features only 3-4
  are genuinely cross-OS; the rest sell a Go veneer over N Linux
  backends. Agents `cat` Bash source during incident response — Go
  binaries are opaque.
- *Why not just unattended-upgrades + logrotate?* They emit
  human-readable logs only. agent-toolkit emits the same logic with a
  schema'd JSON trail an agent can read. See `docs/comparison.md`.
- *MCP server vs MCP client?* It's a server (stdio transport); your
  agent runtime is the client. Tested with Claude Desktop, Cursor,
  Continue, Cline.
- *What about ${distro_X}?* Linux-only is the contract. Tested on
  Debian/Ubuntu apt. Updater is apt-specific today; dnf/zypper are
  candidates in the roadmap.

---

## r/selfhosted

**Title**:

```
[OSS] Agent-toolkit — cron tools for Linux servers, designed so AI agents can read the audit trail
```

**Body**:

```
Hey /r/selfhosted,

I run a few personal Linux servers and the cleanup/update story has
always been a pile of bash one-liners in /etc/cron.daily. Last month I
hit 99% disk on the box that hosts everything, watched an autonomous
agent try to fix it with `find -delete` (which is exactly as scary as
it sounds), and decided to actually do this right.

**agent-toolkit** is two cron-runnable Bash tools:

- **system-janitor** — disk + cache cleanup
  - Docker prune, Go build cache, /tmp/go-build orphans, NuGet temp,
    aged logs, arbitrary user-configured globs
  - Safety floor: inode + disappearance check on dirs you want
    protected — run aborts if a safety-floor dir mutates
  - `--dry-run` is default; you opt in to destruction
  - Emits per-section freed bytes to JSONL audit trail
- **system-updater** — apt updates
  - Preview mode shows what would change; `--apply` does it
  - Same audit-trail contract

Why "agent-toolkit": every run writes machine-readable JSONL, schemas
ship in `schemas/`, capabilities are discoverable via `--version
--json`, and there's an MCP server so Claude/Cursor/Continue/Cline can
call them directly without me writing custom glue.

But it's perfectly usable as just "boring cron tools" too. The agent
stuff stays out of your way until you want it.

**Install**:

```bash
curl -fsSL https://raw.githubusercontent.com/agent-frontier/agent-toolkit/main/scripts/bootstrap.sh | bash
```

(Or git-clone if you'd rather inspect first — bootstrap.sh refuses to
run as root, only ~50 lines, no obfuscation.)

Apache 2.0, smoke tests in CI, Linux-only by design.

Repo: https://github.com/agent-frontier/agent-toolkit

Would love feedback from anyone running tens-to-hundreds of homelab
boxes — the fleet audit story (jq-piping `--report --json` across hosts
to find silent failures) is the part I'm most curious to hear if you'd
actually use.
```

---

## r/devops / r/sre (variant)

Same body as r/selfhosted but with the lead retargeted:

```
Hey /r/devops (and SRE folks),

Built a small Bash toolkit specifically for the case where an
autonomous agent is the operator. Two tools, one contract:

- JSONL audit trail (append-only, schema-pinned, atomic writes for
  state files monitors might tail mid-run)
- Capability discovery via --version --json (no --help scraping)
- --dry-run defaults, --doctor self-check, --health/--health-acknowledge
  for recovering from old noise
- MCP server for Claude/Cursor/Continue/Cline integration
- Idle-streak detector that catches "this section has been silently
  doing nothing for 7 runs"

It's not trying to be Ansible — it's a leaf tool. Your CM system can
deploy it via the curl|bash one-liner.

[same body from r/selfhosted from "agent-toolkit is two..." onward]
```

---

## awesome-selfhosted PR

**File**: `awesome-selfhosted/README.md`

**Section**: `Software` → `Maintenance` or `System Administration`

**Entry format** (matches awesome-selfhosted conventions):

```markdown
- [agent-toolkit](https://github.com/agent-frontier/agent-toolkit) - Agent-first cron toolkit for Linux servers: disk cleanup, apt updates, JSONL audit trail, capability discovery, MCP server for AI-agent integration. `Apache-2.0` `Bash`
```

**PR description**:

```
This PR adds agent-toolkit, a small Bash cron toolkit specifically
designed to be operable by autonomous AI agents (while remaining
perfectly usable for humans).

Why it fits awesome-selfhosted:
- Self-hosted, single-host scope (no SaaS, no central control plane)
- Apache 2.0
- No telemetry, no phone-home
- Single source-readable scripts; no compiled binaries to trust
- Smoke tests in CI; explicit version + capability contract

What it does:
- system-janitor.sh: disk/cache cleanup with safety floor + integrity
- system-updater.sh: apt update sweep
- MCP server for Claude Desktop / Cursor / Continue / Cline

Repo: https://github.com/agent-frontier/agent-toolkit
License: Apache-2.0
Author / maintainer: agent-frontier
```

---

## awesome-mcp-servers PR

**File**: `awesome-mcp-servers/README.md` (or whichever the upstream
canonical is — check `punkpeye/awesome-mcp-servers` and
`appcypher/awesome-mcp-servers` for the active one at submission time)

**Section**: most likely `System Administration` or `DevOps` if the list
has them; otherwise `Other`.

**Entry**:

```markdown
- [agent-toolkit-mcp](https://github.com/agent-frontier/agent-toolkit/tree/main/mcp-server) - 🐍 - 🏠 - MCP server exposing the agent-toolkit (Linux disk cleanup + apt updates) to agent runtimes. 9 tools across system-janitor and system-updater; safe-by-default (destructive tools require explicit `apply=true`).
```

The emoji convention varies by list. Common: 🐍 = Python, 🏠 = local
(stdio), ☁️ = cloud, 🍎 = macOS, 🐧 = Linux, 🪟 = Windows. Adjust to the
target list's legend.

**PR description**:

```
Adds agent-toolkit-mcp, the MCP server for agent-toolkit
(github.com/agent-frontier/agent-toolkit).

What it exposes:
- janitor_report / janitor_health / janitor_doctor / janitor_version
- janitor_run (safe-by-default: dry_run=True; explicit dry_run=False
  for destructive cleanup)
- updater_report / updater_health / updater_version
- updater_run (safe-by-default: apply=False)

Transport: stdio.
Language: Python 3.10+ (Anthropic SDK).
Platform: Linux (the underlying tools are Linux-specific).
Install: `pip install -e mcp-server/` from the repo (PyPI publish
pending v0.1.0).
License: Apache-2.0.

Tested with Claude Desktop. Includes example config snippet in the
mcp-server README.
```

---

## awesome-cli-apps PR

**File**: `awesome-cli-apps/README.md`

**Section**: `System` (most likely) — alternative: `DevOps`.

**Entry**:

```markdown
- [agent-toolkit](https://github.com/agent-frontier/agent-toolkit) - Cron toolkit for Linux servers (disk cleanup, apt updates) with JSONL audit trail, `--dry-run` defaults, `--doctor` self-check, and an MCP server for AI-agent integration.
```

**PR description**: short. Most awesome-cli-apps PRs are one-liners.

---

## Lobsters

Lobsters culture rewards technical-first writeups. Title:

```
agent-toolkit: Bash cron tools designed so autonomous LLM agents can read the audit trail
```

Tag suggestions: `release`, `practices`, `linux`.

Body (~150 words; Lobsters prefers concise):

```
Two Bash tools that share one contract:

- system-janitor — disk + cache cleanup
- system-updater — apt update sweep

The interesting design choice: every run emits JSONL with frozen
schema, capabilities are discoverable via --version --json, and exit
codes are a public contract. The audit trail is append-only and
written via temp-rename so a monitor can tail it mid-run without
seeing torn files.

Why: I built it after a Claude agent tried to clean up a 99%-full
server by running `find -delete`. The bash-script "give the agent a
shell" approach is too dangerous; the JSON-API "wrap everything in a
service" approach loses the affordance of agents reading the source.
Bash + a strict machine-readable contract turned out to be the
sweet spot.

Apache 2.0. ~3300 LOC. Five smoke harnesses. Linux-only by design.

Repo: https://github.com/agent-frontier/agent-toolkit
Comparison vs alternatives:
https://github.com/agent-frontier/agent-toolkit/blob/main/docs/comparison.md
```

---

## Twitter / X / Bluesky / Mastodon (thread format)

**Tweet 1** (pin):

```
new repo: agent-toolkit — cron tools for Linux servers, designed so AI
agents can actually read the audit trail. JSONL events, capability
discovery, --dry-run defaults, MCP server for Claude/Cursor.

https://github.com/agent-frontier/agent-toolkit
```

**Tweet 2**:

```
the design north star: "the primary consumer is an autonomous LLM
agent operating without supervision."

so: no scraping --help (--version --json + capabilities[]), atomic
state writes (no torn files for monitors), --dry-run as the default,
exit codes are a frozen public contract
```

**Tweet 3**:

```
why bash and not go: tested it. of 15 candidate features only 3-4 are
genuinely cross-OS. the rest sell a go veneer over N linux backends.

agents `cat` bash source during incidents. that affordance is more
valuable than the portability story we'd have to fake.
```

**Tweet 4**:

```
ships with an MCP server (stdio transport, anthropic SDK, 9 tools)
so claude desktop / cursor / continue / cline can call it directly.

destructive tools default to dry_run=True. opt-in for actual mutation.
```

**Tweet 5**:

```
apache 2.0. linux-only by design. five smoke harnesses.

repo: github.com/agent-frontier/agent-toolkit
agent contract: github.com/agent-frontier/agent-toolkit/blob/main/AGENTS.md
mcp server: github.com/agent-frontier/agent-toolkit/tree/main/mcp-server
```

---

## Discord / Slack one-liner (for #show-and-tell channels)

```
Just dropped agent-toolkit — Linux cron tools (disk cleanup + apt updates) with a JSONL audit trail and an MCP server so AI agents can read it directly. Apache 2.0, ~3300 LOC of Bash, smoke tests in CI. https://github.com/agent-frontier/agent-toolkit
```

---

## Submission tracker

Update as you post:

| Channel | Status | Posted | URL | Outcome |
|---------|--------|--------|-----|---------|
| Show HN | not posted | — | — | — |
| r/selfhosted | not posted | — | — | — |
| r/devops | not posted | — | — | — |
| Lobsters | not posted | — | — | — |
| awesome-selfhosted | not posted | — | — | — |
| awesome-mcp-servers | not posted | — | — | — |
| awesome-cli-apps | not posted | — | — | — |
| Twitter/X | not posted | — | — | — |
| Mastodon | not posted | — | — | — |
| Bluesky | not posted | — | — | — |

Post-launch checklist:

- [ ] Tag `v0.1.0` BEFORE posting (so the version-pinned bootstrap URL
  resolves).
- [ ] Verify the MCP server `pip install -e mcp-server/` flow on a
  clean machine.
- [ ] Verify the bootstrap.sh one-liner end-to-end on a clean machine.
- [ ] Record + upload at least one asciinema cast (see `demos/`).
- [ ] Pin first-comment-from-author on Show HN with the design rationale.
- [ ] Watch for issues + answer within 24h while the post is live.
