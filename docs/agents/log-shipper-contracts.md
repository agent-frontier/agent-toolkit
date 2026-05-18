# log-shipper contracts (deltas)

This page documents how `log-shipper.sh` extends the shared toolkit contract. Read [contracts.md](./contracts.md) first.

## Environment variable namespace

`log-shipper` uses the `LOG_SHIPPER_*` namespace.

## Sections

`log-shipper` has three action sections plus the shared integrity section:

- `discover_sources`
- `transform_emit`
- `manifest_sync`
- `safety_integrity`

`transform_emit` is the only opt-in section. With no configured output destinations, a real run promotes it from `ok` to `idle`.

`discover_sources` can also emit `idle` explicitly when no readable source JSONL files are found.

## Sources

The tool reads audit trails from the sibling toolkit tools:

- `janitor.jsonl`
- `updater.jsonl`
- `cert-watch.jsonl`

When `LOG_SHIPPER_SOURCES` is empty, the tool auto-discovers those files from `~/.config/agent-toolkit/manifest.json` plus the documented default state-dir layout.

## Doctor checks

`log-shipper` has **five** doctor checks (one more than `cert-watch`):

1. `script_path_resolves`
2. `cron_entry_present`
3. `config_parses`
4. `configured_outputs_writable`
5. `sources_discoverable`

`sources_discoverable` is intentionally non-blocking on a freshly bootstrapped host: zero sources still reports `ok` with warning detail.

## Capabilities

```json
[
  "auto-discover-sources",
  "doctor",
  "doctor-json",
  "health",
  "health-acknowledge",
  "health-json",
  "ndjson-export",
  "only",
  "otlp-json-export",
  "prometheus-textfile",
  "report",
  "report-json",
  "schemas",
  "sections",
  "toolkit-manifest",
  "version",
  "version-json"
]
```

## State files

| File | Purpose |
|---|---|
| `$LOG_SHIPPER_LOG_DIR/log-shipper.jsonl` | Append-only audit trail |
| `$LOG_SHIPPER_LOG_DIR/last-run.json` | Atomic latest-run snapshot |
| `$LOG_SHIPPER_LOG_DIR/.health-baseline` | Byte-offset baseline for `--health-acknowledge` |
| `$LOG_SHIPPER_LOG_DIR/log-shipper.lock` | `flock` single-instance lock |

## Transform outputs

- **Prometheus**: static textfile-collector format, written atomically via tmp+rename
- **NDJSON**: one JSON object per line, enriched with `tool`, `source_path`, and `shipped_at`
- **OTLP JSON**: static JSON payload shaped like `resourceLogs[].scopeLogs[].logRecords[]`
