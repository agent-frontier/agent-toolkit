# agent-toolkit-mcp

Model Context Protocol server exposing the agent-toolkit (`system-janitor`, `system-updater`) to MCP-aware agent runtimes.

## Install

Planned PyPI command:

```bash
pip install agent-toolkit-mcp
```

This package is not on PyPI yet. Today, install it from this repository:

```bash
git clone https://github.com/agent-frontier/agent-toolkit.git
cd agent-toolkit
pip install --user -e mcp-server/
```

You can also run `pip install -e .` from inside `mcp-server/`.

## Claude Desktop integration

Add the server to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-toolkit": {
      "command": "agent-toolkit-mcp"
    }
  }
}
```

If the script is not on your `PATH`, replace `command` with the full executable path.

## Cursor / Continue / Cline integration

Each client documents MCP registration separately, but the pattern is the same: register a stdio MCP server whose command is `agent-toolkit-mcp`. See the MCP setup docs for Cursor, Continue, and Cline for the exact UI or JSON location they expect.

## Tool reference

| Tool | What it does | Parameters |
|---|---|---|
| `janitor_report` | Read-only audit-trail rollup from `system-janitor --report --json`. Returns the parsed report object, including canonical freed-bytes fields and idle streaks. | none |
| `janitor_health` | Read-only trust probe from `system-janitor --health --json`. Returns status, exit code, and the frozen health checks. | none |
| `janitor_doctor` | Read-only installation and self-wiring probe from `system-janitor --doctor --json`. Useful when an agent needs config or path diagnostics. | none |
| `janitor_version` | Read-only capability discovery from `system-janitor --version --json`, including the toolkit cross-reference. | none |
| `janitor_run` | **Warning:** with `dry_run=false` this performs cleanup. By default it runs `system-janitor --dry-run` and returns the parsed `last-run.json`. | `dry_run: bool = true`, `only: str | None = null` |
| `updater_report` | Read-only audit-trail rollup from `system-updater --report --json`. Returns run counts, per-package status counts, and most-recent-run details. | none |
| `updater_health` | Read-only trust probe from `system-updater --health --json`. Returns status, exit code, and the frozen health checks. | none |
| `updater_version` | Read-only capability discovery from `system-updater --version --json`, including the toolkit cross-reference. | none |
| `updater_run` | **Warning:** with `apply=true` this performs package updates and usually requires root. By default it runs preview mode and returns the parsed `updater-last-run.json`. | `apply: bool = false`, `security_only: bool = false` |

## Binary resolution

The server resolves each toolkit binary in this order:

1. `AGENT_TOOLKIT_SYSTEM_JANITOR_BIN` / `AGENT_TOOLKIT_SYSTEM_UPDATER_BIN`
2. `~/.local/bin/system-janitor` / `~/.local/bin/system-updater`
3. `$AGENT_TOOLKIT_HOME/system-janitor.sh` / `system-updater.sh`
4. `~/.config/agent-toolkit/manifest.json` `tools[].binary`
5. `PATH`

Missing binaries do not hide tools. The MCP tool still appears and returns a structured error telling the agent to install the toolkit from the main repository.

## Security note

The destructive tools (`janitor_run` with `dry_run=false`, `updater_run` with `apply=true`) will modify the system. Read the tool descriptions before approving an agent invocation.

## Related docs

- Main repo README: [`../README.md`](../README.md)
- Agent design brief: [`../AGENTS.md`](../AGENTS.md)
- Machine-mode contracts: [`../docs/agents/README.md`](../docs/agents/README.md)
