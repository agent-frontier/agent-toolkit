# cert-watch

`cert-watch` is the third shipped tool in `agent-toolkit`: a read-only TLS
certificate expiry monitor for local cert files and remote TLS endpoints.

It mirrors the toolkit contract used by `system-janitor` and
`system-updater`:

- `--version --json` capability discovery
- append-only JSONL audit trail
- atomic `last-run.json`
- read-only `--report --json`, `--health --json`, `--doctor --json`
- `--health-acknowledge` baseline recovery
- cron-safe single-instance execution via `flock`

Per cert it records subject CN, issuer CN, validity window, days remaining,
SHA-256 fingerprint, source, and a per-cert status of `ok`, `warn`,
`expiring`, or `expired`.

Next:

- [cert-watch-install.md](./cert-watch-install.md)
- [cert-watch-configuration.md](./cert-watch-configuration.md)
- [cert-watch-usage.md](./cert-watch-usage.md)
- [agents/cert-watch-machine-modes.md](./agents/cert-watch-machine-modes.md)
- [agents/cert-watch-contracts.md](./agents/cert-watch-contracts.md)
