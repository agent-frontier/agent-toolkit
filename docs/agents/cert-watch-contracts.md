# cert-watch contracts (deltas)

This page documents how `cert-watch.sh` extends the shared toolkit contract.
Read [contracts.md](./contracts.md) first.

## Environment variable namespace

`cert-watch` uses the `CERT_WATCH_*` namespace.

## Sections

`cert-watch` has two action sections plus the shared integrity section:

- `local_certs`
- `remote_certs`
- `safety_integrity`

`local_certs` and `remote_certs` are opt-in sections. On real runs, a
successful zero-item event is promoted from `ok` to `idle` so agents can
detect stale inventory.

## Per-cert status enum

Each cert record in JSONL events and report output carries:

- `ok` — more than 30 days remaining
- `warn` — 7–30 days remaining
- `expiring` — 1–7 days remaining
- `expired` — already expired

## Run aggregate status

The run-level aggregate is distinct from the per-cert enum:

- `ok` — all inventoried certs are `ok`
- `warn` — at least one cert is `warn` or `expiring`, and none are expired
- `error` — at least one cert is expired, or at least one remote probe failed

The process exit code remains `0` for all three aggregate statuses. Only a
safety-floor violation flips the run to exit `2`.

## Capabilities

```json
[
  "cert-hosts",
  "cert-paths",
  "cert-status-enum",
  "doctor",
  "doctor-json",
  "health",
  "health-acknowledge",
  "health-json",
  "only",
  "report",
  "report-json",
  "schemas",
  "sections",
  "toolkit-manifest",
  "version",
  "version-json"
]
```

`cert-paths` advertises local-file inventory, `cert-hosts` advertises remote
endpoint probing, and `cert-status-enum` advertises the split between
per-cert status and run aggregate status.

## State files

| File | Purpose |
|---|---|
| `$CERT_WATCH_LOG_DIR/cert-watch.jsonl` | Append-only audit trail |
| `$CERT_WATCH_LOG_DIR/last-run.json` | Atomic latest-run snapshot |
| `$CERT_WATCH_LOG_DIR/.health-baseline` | Byte-offset baseline for `--health-acknowledge` |
| `$CERT_WATCH_LOG_DIR/cert-watch.lock` | `flock` single-instance lock |

## JSONL section events

Action-section event shape:

```json
{
  "run_id": "...",
  "ts": "...",
  "host": "...",
  "user": "...",
  "section": "local_certs",
  "status": "ok",
  "items": 4,
  "certs": [
    {
      "subject_cn": "example.com",
      "issuer_cn": "Example Issuer",
      "not_before": "...",
      "not_after": "...",
      "days_remaining": 12,
      "sha256_fingerprint": "AA:BB:...",
      "source": "/etc/ssl/certs/example.pem",
      "status": "warn"
    }
  ]
}
```

`run_start` and `run_end` always bracket a run. `run_end.status` is the
run aggregate (`ok` / `warn` / `error`).
