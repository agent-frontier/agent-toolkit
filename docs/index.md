# agent-toolkit

> Production-grade cleanup and updates for Linux servers, with a machine-readable audit trail every autonomous agent can read.

`agent-toolkit` packages four Bash-first, cron-safe Linux server tools: `system-janitor` for disk and cache cleanup, `system-updater` for apt update sweeps, `cert-watch` for TLS expiry checks, and `log-shipper` for audit export. All four share one agent contract: capability discovery via `--version --json`, structured machine modes, append-only JSONL audit trails, atomic state writes, and explicit dry-run preview modes. Janitor performs configured cleanup unless `--dry-run` is passed; updater previews by default and requires explicit `--apply`.

!!! info "This is the docs site"
    Main repository: [github.com/agent-frontier/agent-toolkit](https://github.com/agent-frontier/agent-toolkit)

## Start here by audience

### DevOps humans

Start with [Install](install.md), then [Cron troubleshooting](cron-troubleshooting.md), [Configuration](configuration.md), and [Usage](usage.md).

### Autonomous agents

Start with [Agent contract](AGENTS.md), then [For agents](agents/README.md), then [Audit trail](audit-trail.md).

### MCP-aware runtimes and tool builders

See the repository's [MCP server README](https://github.com/agent-frontier/agent-toolkit/blob/main/mcp-server/README.md) plus the shared [Capabilities](agents/capabilities.md) and [Contracts](agents/contracts.md).

## Where to go next

<div class="grid cards" markdown>

-   :material-download: **Install**

    ---

    Install all four tools, wire cron, and verify first runs.

    [Janitor](install.md) · [Updater](updater-install.md) · [Cert-watch](cert-watch-install.md) · [Log-shipper](log-shipper-install.md)

    [Diagnose scheduled runs](cron-troubleshooting.md)

-   :material-tune: **Configuration**

    ---

    Learn the `JANITOR_*` and `UPDATER_*` knobs, defaults, and opt-in paths.

    [Open configuration docs](configuration.md)

-   :material-chef-hat: **Cookbook**

    ---

    Copy-pasteable examples for fleet automation, forensics, and integrations.

    [Open the cookbook](cookbook.md)

-   :material-robot: **Agent contract**

    ---

    Machine-mode schemas, exit codes, capabilities, and recovery workflows.

    [Open the agent docs](agents/README.md)

-   :material-connection: **MCP server**

    ---

    Connect the toolkit to Claude Desktop, Cursor, Continue, Cline, and other MCP clients.

    [Open the MCP server README](https://github.com/agent-frontier/agent-toolkit/blob/main/mcp-server/README.md)

</div>

## Project facts

- Repository: [agent-frontier/agent-toolkit](https://github.com/agent-frontier/agent-toolkit)
- License: [Apache 2.0](https://github.com/agent-frontier/agent-toolkit/blob/main/LICENSE)
- Native version for this checkout: `0.2.0`; each tool's `--version --json` is authoritative. Examples pinned to `v0.1.0` target a historical release, not this checkout.
