# Private server dashboard

The optional dashboard shows live disk availability and inode usage, CPU,
RAM/swap, uptime, network throughput, top processes, system/user services,
running Docker/Podman containers, and Janitor cache budgets and activity.
It is **read-only**: no cleanup, restart, terminal or arbitrary-command endpoints.
It uses Python 3.10+ and local static assets; no npm build, metrics database,
third-party scripts or telemetry.

## Install on the server

From the repository root:

```bash
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
export PATH="$HOME/.local/bin:$PATH"
ln -s "$PWD/scripts/server-dashboard.py" "$HOME/.local/bin/agent-toolkit-dashboard"
agent-toolkit-dashboard --init
cp examples/systemd/agent-toolkit-dashboard.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now agent-toolkit-dashboard.service
agent-toolkit-dashboard --url
```

`--init` creates an owned, mode-0600 access token under
`$XDG_CONFIG_HOME/agent-toolkit/dashboard-token` (default
`~/.config/agent-toolkit/dashboard-token`). Repeating it keeps the existing token.
`--url` prints a **secret browser link**; do not share or paste it into logs.
The browser removes the token fragment from the address bar and keeps it only
in that tab's session storage. Private JSON requires a bearer token even on
localhost. Missing or insecure token files are explicit startup errors.

The user service binds **only `127.0.0.1:8787`**, not a LAN/public interface.
It runs as the current user with `NoNewPrivileges`, bounded memory/tasks, and
no added groups or socket permissions. Read-only behavior is enforced by its
fixed observation paths and absence of control endpoints, not a read-only home
mount. Filesystem namespacing in unprivileged user units can remap supplementary
groups and hide otherwise-authorized Docker socket access, so it is not enabled.
To start on boot without an interactive login, user lingering must already be
enabled; inspect it with `loginctl show-user "$USER" -p Linger`. Enabling it is a
host administration choice, not something the dashboard changes automatically.

## Open from your own computer

Keep an SSH tunnel running on your computer:

```bash
ssh -N -L 127.0.0.1:8787:127.0.0.1:8787 USER@SERVER
```

Then open the private link printed by `agent-toolkit-dashboard --url` on the
server. Alternatively, visit `http://127.0.0.1:8787` and enter the token.
Do not expose this listener through a public reverse proxy: its intended remote
transport is the encrypted SSH tunnel, not public HTTP.

## What the numbers mean

Observations refresh nominally every five seconds; bounded service/container
timeouts may delay a sample. HTTP reads the cached snapshot, never launches a
scan. CPU and network deltas are unknown until a second observation. Process
CPU may exceed 100% for multithreaded work. Memory uses kernel `MemAvailable`;
disk availability excludes filesystem-reserved space, matching `df` semantics.
Storage covers root plus an allowlist of local disk filesystem types,
deduplicated by device and capped at 30 mounts; it is not every virtual or
network mount and nothing is recursively measured.

Up to 360 observations stay in memory and disappear on restart. There are no
recursive `du` calls or growing metrics files. Cached Go usage is explicitly the
last Janitor observation, not a new scan. Missing first-run records are unknown;
malformed/oversized JSON, non-object record roots, invalid rendered maintenance
fields, inaccessible systemd scopes and failed container queries are shown as
observation gaps. This is not exhaustive validation of every nested audit-schema
field. Failed fetches keep the last values visibly stale. Services cover
**loaded units**, not every installed unit file, capped at 250 per scope.
Process rows are capped at 15 and container inventories at 100 per engine;
truncated inventories are labelled.

The dashboard does not read `/proc/PID/cmdline` or `/proc/PID/environ` or full
service logs. Container API responses can include extra metadata such as
commands; those fields are discarded, never published in snapshots or logs.
It can only observe what its unprivileged user can access. It does not publish
source, loosen [maintenance protections](cron-troubleshooting.md), or change
the five-minute Janitor timer.

Containers are queried with fixed, read-only `GET /containers/json` requests
over an existing local Unix socket, not privileged/Snap container launchers.
Docker checks the user's runtime socket and `/var/run/docker.sock`; Podman uses
the current user's existing `podman/podman.sock` under `$XDG_RUNTIME_DIR`.
No socket or daemon is automatically enabled and no permissions are widened.
An installed engine without an available socket is explicitly **unknown**, not
zero containers. `DOCKER_HOST` / `CONTAINER_HOST` may select a local `unix:///`
endpoint; remote transports are deliberately rejected.

## Machine interface and operations

```bash
agent-toolkit-dashboard --version --json
agent-toolkit-dashboard --snapshot
systemctl --user status agent-toolkit-dashboard.service
journalctl --user -u agent-toolkit-dashboard.service --no-pager -n 30
```

`--snapshot` emits one JSON object without starting a server, creating tokens or
writing state: exit 0 for healthy collection, 4 for source-observation errors
(including unsupported source configuration or denied socket access).
Here, **healthy describes collection, not host health or complete visibility**:
missing first-run records and absent optional engine sockets stay unknown and
do not alone make collection degraded. Invalid CLI arguments, token
configuration and startup errors exit 3. `GET /api/snapshot` returns the cached
JSON with `Authorization: Bearer TOKEN`; missing/invalid auth returns 401,
foreign Host/Origin returns 403, warming-up returns 503, and mutation methods
return 405. Token values never belong in query strings or command arguments.
Port defaults to 8787 (`--port`), sampling to five seconds (`--interval 2..60`).
If changing the service port, use the same `--port` with `--url` and the tunnel.

To rotate a token, stop the service, move its private token file to a private
backup, rerun `--init`, and restart. Existing browser sessions will need the new
link. Stopping/disabling the dashboard leaves all maintenance jobs untouched.

### Stop and disable monitoring

Only run this when you intend to stop the dashboard and disable its startup:

```bash
systemctl --user disable --now agent-toolkit-dashboard.service
```
