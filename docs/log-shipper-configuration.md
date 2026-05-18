# Configuration — log-shipper

## Precedence

1. Built-in defaults
2. Environment variables (`LOG_SHIPPER_*`)
3. `$XDG_CONFIG_HOME/log-shipper/config`
4. `--config <path>`

The config file is sourced as bash.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `LOG_SHIPPER_LOG_DIR` | `$XDG_STATE_HOME/log-shipper` | State dir for `log-shipper.log`, `log-shipper.jsonl`, `last-run.json`, `.health-baseline`, and `log-shipper.lock` |
| `LOG_SHIPPER_SOURCES` | empty | Colon-separated literal paths or glob patterns for source JSONL files; when empty, sources auto-discover from `~/.config/agent-toolkit/manifest.json` |
| `LOG_SHIPPER_PROM_OUT` | empty | Destination `.prom` file for Prometheus textfile export |
| `LOG_SHIPPER_NDJSON_OUT` | empty | Destination NDJSON file, or `-` for stdout |
| `LOG_SHIPPER_OTLP_JSON_OUT` | empty | Destination OTLP-shaped JSON file |
| `LOG_SHIPPER_LOOKBACK_HOURS` | `24` | Ignore source events older than this window |
| `LOG_SHIPPER_SAFETY_FLOOR_DIRS` | empty | Optional colon-separated dirs whose inode+size must not change during a run |

## Auto-discovery

When `LOG_SHIPPER_SOURCES` is empty, `log-shipper` reads `~/.config/agent-toolkit/manifest.json` and looks for the default state-file locations of the sibling tools:

- `~/.local/state/janitor/janitor.jsonl`
- `~/.local/state/system-updater/updater.jsonl`
- `~/.local/state/cert-watch/cert-watch.jsonl`

`log-shipper` does **not** ship its own audit trail back into itself.

## Example

```bash
LOG_SHIPPER_PROM_OUT="/var/lib/node_exporter/textfile_collector/agent_toolkit.prom"
LOG_SHIPPER_LOOKBACK_HOURS=24
```
