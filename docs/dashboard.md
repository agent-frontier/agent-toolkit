# Private server dashboard

The optional dashboard shows live disk availability and inode usage, CPU,
RAM/swap, uptime, network throughput, top processes, system/user services,
Docker/Podman containers in all states, and Janitor cache budgets and activity.
It is **read-only by default**. Explicit homelab controls add fixed
Stop/Kill/Remove actions, not a terminal or arbitrary-command endpoint.
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

Without a profile, the user service binds **only `127.0.0.1:8787`**.
Direct LAN access is an explicit HTTPS-only option below; wildcard and public
addresses are rejected.
It runs as the current user with `NoNewPrivileges`, bounded memory/tasks, and
no added groups or socket permissions. With `actions=false`, only observation
paths are enabled. Opt-in controls add the fixed, scoped operations below;
neither mode relies on a read-only home mount. Filesystem namespacing in
unprivileged user units can remap supplementary
groups and hide otherwise-authorized Docker socket access, so it is not enabled.
To start on boot without an interactive login, user lingering must already be
enabled; inspect it with `loginctl show-user "$USER" -p Linger`. Enabling it is a
host administration choice, not something the dashboard changes automatically.

## Open directly from a phone on the LAN

No SSH tunnel is needed in this mode. Configure
`$XDG_CONFIG_HOME/agent-toolkit/dashboard.json` (default
`~/.config/agent-toolkit/dashboard.json`) with your server's actual LAN IP,
client subnet and certificate paths:

```json
{
  "bind": "192.168.1.10",
  "allowed_networks": ["192.168.1.0/24"],
  "tls_cert": "~/.config/agent-toolkit/dashboard-lan.crt",
  "tls_key": "~/.config/agent-toolkit/dashboard-lan.key"
}
```

Only explicit RFC1918 IPv4 addresses/CIDRs are accepted. LAN mode requires a
certificate and an owned private regular key file (mode0600), never plaintext
HTTP. Use an existing suitable certificate or generate a local one **once**,
changing the example IP to the server's address. Do not overwrite an existing
certificate/key. For a nondefault XDG config directory, adjust the profile's
paths to the actual generated files.

```bash
cfg="${XDG_CONFIG_HOME:-$HOME/.config}/agent-toolkit"
umask 077
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -subj "/CN=Private dashboard" -addext "subjectAltName=IP:192.168.1.10" \
  -keyout "$cfg/dashboard-lan.key" -out "$cfg/dashboard-lan.crt"
systemctl --user restart agent-toolkit-dashboard.service
agent-toolkit-dashboard --url
```

Open the printed private HTTPS link on your phone on the same LAN; you can also
open `https://192.168.1.10:8787` and enter the token. A self-signed local
certificate needs a one-time browser trust decision: verify its fingerprint
against the server before trusting it, or use a certificate your phone already
trusts. Inspect the fingerprint with
`openssl x509 -in "$cfg/dashboard-lan.crt" -noout -fingerprint -sha256`.
By default, the application still requires the token after TLS trust. Clients outside the
configured subnets, wrong Host/Origin values and public/wildcard binds are denied.
The dashboard does not enable a firewall, router forwarding or public access.

For a trusted home LAN where the plain URL should open immediately, add
`"trusted_lan": true` to this profile and restart the dashboard. **Anyone on the
allowed subnet can then view the stats without logging in.** HTTPS,
explicit private binding, client CIDRs, Host/Origin checks and fixed endpoint
allowlists remain mandatory. No token file is needed in this mode;
`--url` prints the plain address. Removing the setting restores token access.
The browser attempts to load the dashboard automatically and only shows the
login prompt when the server actually requires one.

If the address comes from DHCP, reserve it in your router to keep the bookmark
stable. A changed/unavailable bind address fails visibly rather than silently
falling back to a public listener. To return to localhost mode, restore/remove
the optional profile and restart only the dashboard. Do not leave unused LAN
configuration behind when reverting.

## Open through SSH instead (default localhost mode)

Keep an SSH tunnel running on your computer:

```bash
ssh -N -L 127.0.0.1:8787:127.0.0.1:8787 USER@SERVER
```

Then open the private link printed by `agent-toolkit-dashboard --url` on the
server. Alternatively, visit `http://127.0.0.1:8787` and enter the token.
Do not expose this listener through a public reverse proxy. Supported remote
transports are the SSH tunnel or the explicit, subnet-limited LAN HTTPS profile.

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

Maintenance also observes the current user's fixed
`system-janitor-catchup.service` in a separate two-second, exact-unit read:
load state, canonical ID, active/substate, result, integer exit status,
invocation ID and server read timestamp. Missing optional installation is
**unavailable**, not a healthy zero. Failed, incomplete or malformed unit reads
are visible observation gaps, while saved maintenance/native records retain
their own dates. Timer and service state are separate.

Containers are queried with fixed, read-only `GET /containers/json?all=1` requests
over an existing local Unix socket, not privileged/Snap container launchers.
Docker checks the user's runtime socket and `/var/run/docker.sock`; Podman uses
the current user's existing `podman/podman.sock` under `$XDG_RUNTIME_DIR`.
No socket or daemon is automatically enabled and no permissions are widened.
An installed engine without an available socket is explicitly **unknown**, not
zero containers. `DOCKER_HOST` / `CONTAINER_HOST` may select a local `unix:///`
endpoint; remote transports are deliberately rejected.
Both running and stopped/created containers are included. Docker and the
current user's Podman inventory have separate headings and engine/state filters.
For user Podman, an operator can enable the private local socket with
`systemctl --user enable --now podman.socket`; this does not start containers.
The dashboard itself never enables sockets or starts containers. It does not
change container state with `actions=false`; enabled homelab controls can
stop/kill/remove the selected target as documented below. Rootful/system Podman
is not silently combined with the user's inventory.

## Interactive viewing

Use **Refresh now** to fetch the latest cached snapshot. **Pause updates** freezes
this device's view without stopping server-side collection, maintenance or other
clients. A manual refresh while paused fetches once and remains paused; Resume
returns to live polling. Only one snapshot request runs at a time.

Services can be searched, filtered and sorted; failed-first remains the default.
Processes can be searched by name/PID and sorted by CPU, RAM or name **within
the displayed top-process sample**, not as a new server-wide memory ranking.
Docker/Podman filters include created and stopped containers.
Chart ranges are relative to the most recent observation, including while paused.

Tap a service, process or container name for dated snapshot details, or
**Maintenance details** for the named runner and separately dated budget/native
Janitor results (**View last cleanup** on older backends). These are inspection controls
only while controls are disabled. All detail text is rendered as text, not HTML.

## Homelab Stop / Kill / Remove

Set `"actions": true` in the same private profile and restart the dashboard to
enable controls. Default installations remain observers. In `trusted_lan` mode
**anyone on the allowed LAN can use these controls**; no extra password, token
entry or terminal step is imposed on the phone.

Tap an item, choose its action, then confirm the exact named target:

- Docker/Podman: **Stop gracefully** gives the daemon up to 10 seconds before it
  may force-kill a container that has not exited. **Kill now** skips that grace
  period. **Remove container** applies once stopped/created and never deletes
  volumes or recursively removes dependents.
- Processes: **Terminate** (SIGTERM) or **Force kill** (SIGKILL), only for the
  dashboard user's processes. Fresh PID start-time and pidfd checks prevent
  killing a reused PID. The dashboard, its ancestors and core session processes
  are protected. A service manager may restart a killed process.
- Services: **Stop service** for noncritical, loaded **user** units. System units,
  this dashboard, the disk guard and protected infrastructure stay view-only.
  No root service privileges are granted.

### Run guarded cleanup

With controls enabled, **Run guarded cleanup** opens the same named Maintenance
detail/confirmation flow. Cancel sends no request; repeated Confirm taps send
only one. It schedules only the already-installed
`system-janitor-catchup.service` for the dashboard's user. No new unit installation
or wrapper changes are needed; the existing guard's eligibility, leases, locks,
budgets, cooldown and native Janitor phase remain unchanged. Busy work can defer,
and a real check can legitimately reclaim zero bytes. There is no force mode,
bare Janitor sweep, arbitrary unit/path/mode or root fallback.

The fixed `POST /api/action` accepts exactly:

```json
{"kind":"cleanup","action":"run"}
```

It freshly verifies the canonical loaded, unmasked, manually startable user unit,
then issues only `systemctl --user start --no-block -- system-janitor-catchup.service`
with the existing 12-second command bound. An active/activating/deactivating
runner (also reloading/refreshing) is observed without dispatching another start.
Missing, masked, unusable and unknown state is explicit. The same current
CIDR, Host/Origin, authentication-policy, nonce, JSON/body and shared action
serialization gates apply.

Cleanup success includes boolean `dispatch_only: true`, a `dispatch` receipt
(`status: "accepted"` with `dispatched: true`, or `"already_running"` with
`dispatched: false`), and a separate fresh `observation` containing the unit
fields above. `completed_at` dates the API attempt, **not cleanup completion**.
Scheduling acceptance is not reclamation. A timeout after launch, lost outcome
audit or failed post-launch observation is **unknown**, even when scheduling was
accepted; inspect/refresh unit state rather than automatically replaying.
Signal-terminated scheduling helpers also leave scheduling unknown, not rejected,
even when a fresh read observes the runner activating. Undecodable unit output
is a dated malformed observation gap: before launch it blocks dispatch; after
accepted scheduling it returns JSON unknown and an unknown audit outcome.

The live runner badge takes precedence over an old cached `cleaned` result.
A finished unit observation describes that invocation only, not application
health or guaranteed reclaimed bytes. The producer's optional
`service_invocation_id` is a validated 32-hex `INVOCATION_ID` (otherwise null/
unverified). `janitor.maintenance_phase_linked` is true only for equal nonempty
verified service/phase IDs. Linked saved results describe the **budget phase
only**: the wrapper may still be running native Janitor. Legacy, missing and
mismatched IDs remain dated/unlinked results; native Janitor outcomes remain
separate. Removed artifact bytes and concurrent observed free-space change
are displayed separately, never added into a guaranteed reclaim total.

New assets require the approved `actions.cleanup` metadata (fixed kind/action,
unit, user scope and boolean dispatch-only marker), both action capabilities,
and a fresh loaded service observation before offering or posting cleanup.
Freshness uses the authoritative HTTP `Date` header plus locally elapsed
monotonic time, never a comparison with the phone's wall clock. Header precision,
request transit and parsing delay count conservatively against the existing
stale limit for both the cached snapshot and unit read. Missing/invalid server
age evidence is explicitly unknown. Paused views retain their displayed
receipt; discarded responses cannot refresh it or make old observations fresh.
An older backend without that metadata retains legacy viewing and workload
controls; it never receives an unsupported cleanup request.

### Shared action safeguards

Actions use one fixed JSON POST endpoint with source CIDR, Host/Origin,
authentication-policy and per-server nonce checks. Cross-site forms, arbitrary
commands, signal numbers, paths and raw container API forwarding are rejected.
Only one action runs at a time; the result is explicit and the single background
sampler is woken afterward. Timeouts/interrupted requests can leave completion
unknown: refresh before retrying instead of assuming nothing happened.
Container mutation transport has a total 15-second monotonic budget, not an
inactivity timeout that resets on each byte. Inventory reads use a separate
two-second total transport budget. Unexpected mutation statuses are rejected
without reading their response bodies.

Private `dashboard-actions.jsonl` under
`~/.local/state/agent-toolkit/` records an action ID, sanitized target,
started event and succeeded/failed/unknown outcome; cleanup receipts instead
use accepted/observed (or failed/unknown), never a cleanup-completed claim.
It rotates at 5MiB with eight
backups; tokens, nonces, environments and command arguments are never logged.
Failure to write the initial `started` record prevents dispatch. If writing the
outcome fails after an attempt, the action may already have completed while the
journal retains only `started`; a missing terminal record is not proof of
failure. The client receives unknown completion: inspect fresh state before
retrying.
Disable `"actions"` to restore observer mode without changing LAN access.

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
do not alone make collection degraded; a missing optional guarded service is
likewise explicitly unavailable. Failed unit-state reads do degrade collection.
Invalid CLI arguments, token
configuration and startup errors exit 3. `GET /api/snapshot` returns the cached
JSON with `Authorization: Bearer TOKEN`; missing/invalid auth returns 401,
foreign Host/Origin returns 403, warming-up returns 503, and mutation methods
return 405. Token values never belong in query strings or command arguments.
Port defaults to 8787 (`--port`), sampling to five seconds (`--interval 2..60`).
The shared profile may also set `port` and `interval`; explicit CLI values
override them. `--url` reads the same profile and prints the configured scheme,
address and port, so phones do not receive a stale localhost link. If overriding
the service port on its command line, use the same `--port` with `--url` and any
tunnel. Discovery retains the default-loopback capability and adds `lan-https`.
Version `0.6.0` additionally advertises `guarded-cleanup` and `cleanup-job-state`
in the alphabetically sorted, append-only capability list; read-only/default
capability strings and observer defaults are retained.

To rotate a token, stop the service, move its private token file to a private
backup, rerun `--init`, and restart. Existing browser sessions will need the new
link. Stopping/disabling the dashboard leaves all maintenance jobs untouched.

### Stop and disable monitoring

Only run this when you intend to stop the dashboard and disable its startup:

```bash
systemctl --user disable --now agent-toolkit-dashboard.service
```
