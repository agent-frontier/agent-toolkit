# Configuration — cert-watch

`cert-watch` is read-only by default and does nothing until you point it at
local cert files, remote endpoints, or both.

## Precedence

1. Built-in defaults
2. Environment variables (`CERT_WATCH_*`)
3. `$XDG_CONFIG_HOME/cert-watch/config`
4. `--config <path>`

The config file is sourced as bash.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `CERT_WATCH_LOG_DIR` | `$XDG_STATE_HOME/cert-watch` | State dir for `cert-watch.log`, `cert-watch.jsonl`, `last-run.json`, `.health-baseline`, and `cert-watch.lock` |
| `CERT_WATCH_PATHS` | empty | Colon-separated literal paths or glob patterns for local cert files |
| `CERT_WATCH_HOSTS` | empty | Colon-separated `host:port` pairs encoded as `host:port:host:port:...` |
| `CERT_WATCH_WARN_DAYS` | `30` | Upper threshold for `warn` |
| `CERT_WATCH_EXPIRING_DAYS` | `7` | Upper threshold for `expiring` |
| `CERT_WATCH_PROBE_TIMEOUT` | `8` | Timeout in seconds for each remote `openssl s_client` probe |
| `CERT_WATCH_SAFETY_FLOOR_DIRS` | empty | Optional colon-separated dirs whose inode+size must not change during a run |

## Example

```bash
CERT_WATCH_PATHS="/etc/ssl/certs/*.pem:/etc/letsencrypt/live/*/fullchain.pem"
CERT_WATCH_HOSTS="example.com:443:api.internal:8443"
CERT_WATCH_WARN_DAYS=30
CERT_WATCH_EXPIRING_DAYS=7
CERT_WATCH_PROBE_TIMEOUT=8
```
