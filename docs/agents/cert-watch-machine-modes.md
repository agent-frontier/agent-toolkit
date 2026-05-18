# Machine modes — cert-watch

Every machine mode is read-only, runs before `flock` / `mkdir $LOG_DIR` /
log redirects, and is safe to invoke concurrently with an in-progress run.

`--json` is only valid with `--report`, `--health`, `--doctor`,
`--health-acknowledge`, or `--version`.

## `--report`

Rollup over `$CERT_WATCH_LOG_DIR/cert-watch.jsonl`.

- Invocation: `cert-watch --report` or `cert-watch --report --json`
- Exit code: `0`
- Schema: [`../../schemas/cert-watch-report.schema.json`](../../schemas/cert-watch-report.schema.json)
- Composes with `--only` / `--sections` (filters `per_source` and `expiring_soon`)

Top-level JSON keys:

- `log_dir`, `jsonl_path`, `generated_at`
- `total_events`, `total_runs`, `real_runs`, `dry_runs`, `date_range`
- `total_certs_inventoried`
- `by_status` (`ok`, `warn`, `expiring`, `expired`)
- `expiring_soon` (latest cert sighting per source, filtered to `< 30` days)
- `per_source`
- `most_recent_run`
- `data_quality`
- `idle_streaks`

## `--health`

Audit-trail trust probe.

- Invocation: `cert-watch --health` or `... --health --json`
- Exit codes: `0` healthy, `4` degraded, `5` unknown
- Schema: [`../../schemas/cert-watch-health.schema.json`](../../schemas/cert-watch-health.schema.json)

Frozen check names:

- `log_dir_exists`
- `jsonl_present`
- `jsonl_parses`
- `last_run_parses`
- `last_run_integrity_ok`
- `last_run_parses_sections`
- `no_long_idle_streaks`

`jsonl_parses.detail` follows the shared wording contract: malformed branches
must begin with `N invalid line(s)`.

## `--doctor`

Self-wiring probe.

- Invocation: `cert-watch --doctor` or `... --doctor --json`
- Exit codes: `0` healthy, `4` degraded, `5` unknown

Frozen check names:

- `script_path_resolves`
- `cron_entry_present`
- `config_parses`
- `configured_targets_exist`

## `--health-acknowledge`

Writes `$CERT_WATCH_LOG_DIR/.health-baseline` atomically.

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
  "name": "cert-watch",
  "version": "0.1.0",
  "capabilities": [
    "cert-hosts", "cert-paths", "cert-status-enum", "doctor",
    "doctor-json", "health", "health-acknowledge", "health-json",
    "only", "report", "report-json", "schemas", "sections",
    "toolkit-manifest", "version", "version-json"
  ],
  "toolkit": {
    "name": "agent-toolkit",
    "repo": "https://github.com/agent-frontier/agent-toolkit",
    "tools": ["cert-watch", "system-janitor", "system-updater"]
  }
}
```
