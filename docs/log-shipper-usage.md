# Usage — log-shipper

## Prometheus textfile export

```bash
LOG_SHIPPER_PROM_OUT="/var/lib/node_exporter/textfile_collector/agent_toolkit.prom" \
log-shipper --prom
```

## NDJSON export

```bash
LOG_SHIPPER_NDJSON_OUT=- log-shipper --ndjson
```

## OTLP-shaped JSON export

```bash
LOG_SHIPPER_OTLP_JSON_OUT="$HOME/agent-toolkit-otlp.json" log-shipper --otlp-json
```

## Read the audit trail

```bash
log-shipper --report
log-shipper --report --json
log-shipper --health
log-shipper --health --json
```

## Target a single section

```bash
log-shipper --only discover_sources
log-shipper --sections transform_emit
```

## OTLP mapping

Each shipped event becomes one OTLP log record under `resourceLogs[].scopeLogs[].logRecords[]`.

- `resource.attributes[service.name] = "agent-toolkit"`
- `attributes.tool` = derived source tool (`janitor`, `updater`, `cert-watch`)
- `attributes.section` = source `section` or `stage`
- `attributes.status` = source `status`
- `body.stringValue` = JSON-stringified shipped event (including `tool`, `source_path`, and `shipped_at`)

The export is a static JSON document written to disk; `log-shipper` does not open an OTLP gRPC stream.
