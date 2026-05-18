# agent-toolkit

> Production-grade cleanup and updates for Linux servers, with a machine-readable audit trail every autonomous agent can read.

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/agent-frontier/agent-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-frontier/agent-toolkit/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/agent-frontier/agent-toolkit)](https://github.com/agent-frontier/agent-toolkit/releases)
![MCP compatible](https://img.shields.io/badge/MCP-compatible-success)

> **AI agents**: read [`AGENTS.md`](AGENTS.md) for the design brief, contracts, and conventions.

## What it does

Linux servers slowly fill up, packages drift, and unattended maintenance jobs become hard to trust when they mutate a machine without leaving behind a machine-readable trail. Agent-first ops tools for autonomous LLM agents operating Linux servers without supervision. The toolkit solves that by shipping four boring, production-grade cron tools: cleanup, apt updates, certificate expiry checks, and audit-trail export.

Every tool shares one contract: capability discovery via `--version --json`, structured machine-mode output (`--report --json`, `--health --json`), append-only JSONL audit trail, atomic state writes, `flock` single-instance, `--dry-run` default on destructive paths, frozen exit codes, and cron-safe operation. The toolkit is deliberately Bash + Linux-specific. Agents can `cat` the source to understand intent during incident response — no opaque binaries, no build step, no runtime dependencies beyond what every server already ships (bash 4+, coreutils, `flock`, `python3` for JSON output). Humans run them from cron; agents drive them as tools.

The 30-second pitch ends here. Below the fold is for evaluators who want to go deeper.

## Tools shipped today

| Tool | Purpose | State |
|---|---|---|
| [`system-janitor.sh`](system-janitor.sh) | Disk/cache/log cleanup with safety floor and audit-grade logging. Universally-safe ops by default (Docker, Go, NuGet, `/tmp/go-build*`); destructive paths gated behind config. | v0.1.0 |
| [`system-updater.sh`](system-updater.sh) | apt package update sweep with holds, security-only mode, and maintenance windows. `--apply` requires root and is explicit (not the default). | v0.1.0 |
| [`cert-watch.sh`](cert-watch.sh) | Read-only TLS certificate expiry monitor for local cert files and remote `host:port` endpoints, with per-cert status and audit-grade logging. | v0.1.0 |
| [`log-shipper.sh`](log-shipper.sh) | exports the audit trail to Prometheus / NDJSON / OTLP for monitoring | v0.1.0 |
| **Integration** — [`mcp-server/`](mcp-server/README.md) | stdio MCP server exposing `system-janitor` and `system-updater` to Claude Desktop, Cursor, Continue, Cline, and other MCP-aware runtimes. | in repo |

Candidates under consideration live in
[`docs/agents/toolkit-roadmap.md`](docs/agents/toolkit-roadmap.md) —
menu, not commitment.

## Quick install

**One-liner (recommended):**

```bash
curl -fsSL https://raw.githubusercontent.com/agent-frontier/agent-toolkit/main/scripts/bootstrap.sh | bash
```

That clones the repo to `~/agent-toolkit` and runs `install.sh`, which
creates user-level symlinks in `~/.local/bin/` and managed cron entries.

**Pin to a release tag** (recommended for production):

```bash
curl -fsSL https://raw.githubusercontent.com/agent-frontier/agent-toolkit/v0.1.0/scripts/bootstrap.sh | bash -s -- --ref=v0.1.0
```

**Inspect before piping** (recommended for the security-conscious):

```bash
curl -fsSLO https://raw.githubusercontent.com/agent-frontier/agent-toolkit/main/scripts/bootstrap.sh
shasum -a 256 bootstrap.sh        # compare against the SHA in CHANGELOG.md for this release
less bootstrap.sh                  # ~50 lines, no obfuscation, no curl|bash inside
bash bootstrap.sh                  # install once you've read it
```

**Git-clone alternative** (no curl involved):

```bash
git clone https://github.com/agent-frontier/agent-toolkit.git ~/agent-toolkit
cd ~/agent-toolkit
./install.sh
```

**Manual symlink alternative** (matches the original quick-start flow):

```bash
git clone https://github.com/agent-frontier/agent-toolkit.git ~/agent-toolkit
ln -s ~/agent-toolkit/system-janitor.sh ~/.local/bin/system-janitor
ln -s ~/agent-toolkit/system-updater.sh ~/.local/bin/system-updater
chmod +x ~/agent-toolkit/system-janitor.sh ~/agent-toolkit/system-updater.sh
system-janitor --dry-run
system-updater --dry-run
```

After install:

```bash
system-janitor --doctor          # self-check: are we wired up correctly?
system-janitor --dry-run         # preview cleanup
system-updater --dry-run         # preview package updates
```

Per-tool install / config / scheduling: see
[docs/install.md](docs/install.md) (janitor) and
[docs/updater-install.md](docs/updater-install.md) (updater).

## Sample output

> Want to run these live yourself? See [`demos/`](demos/) — three
> self-contained scenarios that spin up temp state, run the toolkit,
> and clean up. Asciinema casts forthcoming.

Sanitized from a live host so the paths are portable.

### `system-janitor --doctor`

```text
system-janitor doctor: healthy (exit 0)
  ok   script_path_resolves   /home/USER/agent-toolkit/system-janitor.sh
  ok   cron_entry_present     17 3 * * 0 /home/USER/.local/bin/system-janitor
  ok   config_parses          /home/USER/.config/system-janitor/config
  ok   configured_dirs_exist  JANITOR_AGED_LOG_DIRS: 1 entry resolved; JANITOR_EXTRA_CLEANUP_DIRS: 3 entries resolved
```

### `system-janitor --report --json` (excerpt)

```json
{
  "total_events": 105,
  "total_runs": 13,
  "real_runs": 8,
  "dry_runs": 5,
  "date_range": {
    "first": "2026-04-29T17:54:23+0000",
    "last": "2026-05-18T18:29:25+0000"
  },
  "total_freed_bytes": 171018633216,
  "per_section": [
    {
      "name": "extra_cleanup",
      "runs": 11,
      "freed_bytes": 80011517952,
      "items_total": 22
    },
    {
      "name": "go_build_cache",
      "runs": 10,
      "freed_bytes": 72974675968,
      "items_total": 0
    },
    {
      "name": "tmp_gobuild_orphans",
      "runs": 9,
      "freed_bytes": 9295339520,
      "items_total": 249
    },
    {
      "name": "docker_prune",
      "runs": 10,
      "freed_bytes": 8501428224,
      "items_total": 22
    }
  ],
  "idle_streaks": []
}
```

168 GiB freed across 8 production runs on one server; per-section accounting; idle-streak detector for silent failures.

### `system-janitor --version --json`

```json
{"name":"system-janitor","version":"0.1.0","capabilities":["aged-logs","doctor","doctor-json","extra-cleanup-globs","health","health-acknowledge","health-json","idle-status","json-schemas","last-run-sections","only","report","report-bytes","report-json","schema-aliases","toolkit-manifest","version","version-json"],"toolkit":{"name":"agent-toolkit","repo":"https://github.com/agent-frontier/agent-toolkit","tools":["cert-watch","log-shipper","system-janitor","system-updater"]}}
```

## Where to go next

- **[docs/](docs/README.md)** — full documentation index, split by audience.
- **Documentation site** — <https://agent-frontier.github.io/agent-toolkit/> (once Pages is enabled on the repo).
- **Humans** start at [docs/install.md](docs/install.md) →
  [docs/configuration.md](docs/configuration.md) →
  [docs/usage.md](docs/usage.md).
- **Autonomous agents** start at [docs/agents/README.md](docs/agents/README.md)
  for the shared contract, machine-mode specs, exit codes, schemas, and
  recovery workflows.
- **Shared reference**: [docs/audit-trail.md](docs/audit-trail.md) documents
  the JSONL event shape, status enum, and `last-run.json` schema that all
  tools build on.

## Project files

- [CHANGELOG.md](CHANGELOG.md) — per-release record of agent-visible changes
  (flags, JSON fields, capabilities, exit codes).
- [schemas/](schemas/) — formal Draft 2020-12 JSON Schemas for each tool's
  `--report --json` and `--health --json`.
- [examples/](examples/) — fully-commented sample configs for each tool.
- [LICENSE](LICENSE) — Apache 2.0.

## Integrations

[`mcp-server/`](mcp-server/README.md) is the bridge from these Bash tools into MCP-aware runtimes. It exposes read-only report, health, doctor, and version probes plus explicitly-gated run actions for `system-janitor` and `system-updater`. The server is a stdio MCP process meant for Claude Desktop, Cursor, Continue, Cline, and similar clients. Install and client-specific registration examples live in [`mcp-server/README.md`](mcp-server/README.md).

## Development

```bash
bash -n system-janitor.sh system-updater.sh   # syntax check
shellcheck system-janitor.sh system-updater.sh  # static analysis
./tests/smoke.sh                              # janitor: 83 assertions
./tests/updater-smoke.sh                      # updater: 42 assertions
```

CI (`.github/workflows/ci.yml`) runs lint + smoke for both tools on every
push and PR. Smoke suites enforce capability completeness — every string
in `--version --json`'s `capabilities[]` has an end-to-end probe. See
[`AGENTS.md`](AGENTS.md) for the contributor contract; [.github/copilot-instructions.md](.github/copilot-instructions.md) points GitHub Copilot at the same brief.

## License

[Apache 2.0](LICENSE).

<!--
Suggested GitHub topics for this repo (set via Settings → Topics):
  cron, sysadmin, linux, bash, devops, observability, cleanup,
  apt, package-manager, audit-log, jsonl, mcp, mcp-server,
  agent-tools, autonomous-agents, llm-tools
-->
