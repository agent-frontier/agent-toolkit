# Machine modes — log-shipper

Every machine mode is read-only, runs before `flock` / `mkdir $LOG_DIR` / log redirects, and is safe to invoke concurrently with an in-progress run.

`--json` is only valid with `--report`, `--health`, `--doctor`, `--health-acknowledge`, or `--version`.

## `--report`

Rollup over `$LOG_SHIPPER_LOG_DIR/log-shipper.jsonl`.

- Invocation: `log-shipper --report` or `log-shipper --report --json`
- Exit code: `0`
- Schema: [`../../schemas/log-shipper-report.schema.json`](../../schemas/log-shipper-report.schema.json)

Top-level JSON keys:

- `log_dir`, `jsonl_path`, `generated_at`
- `total_events`, `total_runs`, `real_runs`, `dry_runs`
- `total_sources`, `sources`, `by_tool`, `outputs`
- `idle_streaks`, `manifest_drift`
- `most_recent_run`
- `data_quality`

## `--health`

Audit-trail trust probe.

- Invocation: `log-shipper --health` or `... --health --json`
- Exit codes: `0` healthy, `4` degraded, `5` unknown
- Schema: [`../../schemas/log-shipper-health.schema.json`](../../schemas/log-shipper-health.schema.json)

Frozen check names:

- `log_dir_exists`
- `jsonl_present`
- `jsonl_parses`
- `last_run_parses`
- `last_run_integrity_ok`
- `last_run_parses_sections`
- `no_long_idle_streaks`

## `--doctor`

Self-wiring probe.

- Invocation: `log-shipper --doctor` or `... --doctor --json`
- Exit codes: `0` healthy, `4` degraded, `5` unknown

Frozen check names:

- `script_path_resolves`
- `cron_entry_present`
- `config_parses`
- `configured_outputs_writable`
- `sources_discoverable`

## `--health-acknowledge`

Writes `$LOG_SHIPPER_LOG_DIR/.health-baseline` atomically.

JSON shape:

```json
{
  "acknowledged": true,
  "baseline_bytes": 12345,
  "excluded_events": 24
}
```

## `--version`

Capability discovery surface.

```json
{
  "name": "log-shipper",
  "version": "0.1.0",
  "capabilities": [
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
  ],
  "toolkit": {
    "name": "agent-toolkit",
    "repo": "https://github.com/agent-frontier/agent-toolkit",
    "tools": ["cert-watch", "log-shipper", "system-janitor", "system-updater"]
  }
}
```
