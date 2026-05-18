# log-shipper

`log-shipper` is the fourth shipped tool in `agent-toolkit`: a read-only audit-trail exporter for the sibling toolkit tools.

It reads `janitor.jsonl`, `updater.jsonl`, and `cert-watch.jsonl`, then re-emits the recent window into:

- Prometheus textfile format
- NDJSON
- OTLP-shaped JSON

Next:

- [log-shipper-install.md](./log-shipper-install.md)
- [log-shipper-configuration.md](./log-shipper-configuration.md)
- [log-shipper-usage.md](./log-shipper-usage.md)
- [agents/log-shipper-machine-modes.md](./agents/log-shipper-machine-modes.md)
- [agents/log-shipper-contracts.md](./agents/log-shipper-contracts.md)
