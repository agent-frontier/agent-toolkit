# Usage — cert-watch

## Inventory local certs

```bash
CERT_WATCH_PATHS="$HOME/certs/*.crt" cert-watch
```

## Probe remote endpoints

```bash
CERT_WATCH_HOSTS="example.com:443:api.internal:8443" cert-watch
```

## Read the audit trail

```bash
cert-watch --report
cert-watch --report --json
cert-watch --health
cert-watch --health --json
```

## Target a single section

```bash
cert-watch --only local_certs
cert-watch --sections remote_certs
```

## Recovery after historical malformed JSONL

```bash
cert-watch --health
cert-watch --health-acknowledge --json
cert-watch --health
```

The tool never renews, rotates, or edits certificates. It only reports.
