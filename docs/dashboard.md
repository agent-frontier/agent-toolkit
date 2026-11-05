# Private server dashboard

The optional dashboard shows live disk availability and inode usage, CPU,
RAM/swap, uptime, network throughput, top processes, system/user services,
Docker/Podman containers in all states, and Janitor cache budgets and activity.
It is **read-only by default**. Explicit homelab controls add fixed
Start/Restart/Stop/Kill/Remove and guarded cleanup actions, not a terminal or
arbitrary-command endpoint.
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

**Cached cleanup context** names `maintenance-last.json`, its saved mode/date,
server-relative age (unknown without valid HTTP receipt evidence), and the
coverage of its cache/workspace lists. Per-cache bytes, soft budgets, activity,
eligibility and deferral reasons are saved observations, not a new scan or
deletion permission. Missing fields/lists mean **unknown**, not zero or safe.
Workspace protection records can be carried forward from a previous phase;
their displayed saved state is not a fresh verification.
A budget is not an estimate of reclaimable bytes. Previously reclaimed bytes
are not available to reclaim again. Root free space, removed artifact bytes
and observed free-space change are separate metrics: never add/subtract
overlapping categories to invent complete-disk attribution or a reclaim total.

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
The dashboard itself never enables sockets. Observer mode (`actions=false`)
never starts or changes containers; enabled homelab controls can
start/restart/stop/kill/remove only the selected existing target as documented
below. No pull/create/unpause/prune or root fallback is offered. Rootful/system Podman
is not silently combined with the user's inventory.

## Interactive viewing

Use **Refresh now** to fetch the latest cached snapshot. **Pause updates** freezes
this device's view without stopping server-side collection, maintenance or other
clients. A manual refresh while paused fetches once and remains paused; Resume
returns to live polling. Only one snapshot request runs at a time.
If a paused manual read fails, the dated old values remain visibly stale;
**Refresh now** or **Resume updates** is required. There is no automatic retry
while paused. Explicit post-action reads also leave Pause enabled.

The section links above the overview jump directly to Maintenance, Containers,
Services, Storage, Processes and the chart without replacing panels or filters.
Links, row inspection buttons and other controls have at least44px targets.
Phone service/process/filesystem rows present identities and labelled values
vertically rather than splitting them across narrow columns. Container rows
name their engine, current-user Podman scope and observed state.

Services can be searched, filtered and sorted; failed-first remains the default.
Processes can be searched by name/PID and sorted by CPU, RAM or name **within
the displayed top-process sample**, not as a new server-wide memory ranking.
Docker/Podman filters include created and stopped containers.
Chart ranges are relative to the most recent observation, including while paused.

Tap a service, process or container name for dated snapshot details, or
**Maintenance details** for the named runner and separately dated budget/native
Janitor results (**View last cleanup** on older backends). These are inspection controls
only while controls are disabled. All detail text is rendered as text, not HTML.
The named native dialog initially focuses **Close**; confirmations focus
**Cancel**, name the exact engine/scope/target and send nothing on Cancel/Escape.
During dispatch, dismissal is blocked; closing afterward cannot undo a request.
Refresh preserves the exact focused identity (engine + full container ID,
service scope + unit, or PID + start ticks). Closing/Escape restores the actual
matching inspection opener, including a rebuilt row; a vanished identity falls
back to its focusable section heading, never a matching name or reused PID.

**Historical snapshot details** stay fixed until reopened. A separate
**exact post-attempt state** gives the server read date and does not claim
application health; **request receipts** describe the API attempt independently.
Known exact state still supports retained user-service Start after Stop.
Older/partial backend metadata is labelled, preserves legacy fixed controls and
does not authorize unsupported cleanup/recovery requests or history reads.

## Homelab fixed controls

Set `"actions": true` in the same private profile and restart the dashboard to
enable controls. Default installations remain observers. In `trusted_lan` mode
**anyone on the allowed LAN can use these controls**; no extra password, token
entry or terminal step is imposed on the phone.

Tap an item, choose its action, then confirm the exact named target:

- Docker/current-user Podman: **Start container** accepts only
  created/exited/stopped containers. **Restart container** accepts only running
  containers and disrupts the workload: the daemon waits up to **10 seconds**,
  then may **force-kill** before starting it again. The confirmation discloses
  this grace/escalation. **Stop gracefully** gives the daemon up to 10 seconds before it
  may force-kill a container that has not exited. **Kill now** skips that grace
  period. **Remove container** applies once stopped/created and never deletes
  volumes or recursively removes dependents. Every request uses the full
  64-character ID, freshly checked state/protection and a fixed local route:
  POST `/containers/ID/start`, `/containers/ID/restart?t=10`,
  `/containers/ID/stop?t=10`, `/containers/ID/kill?signal=KILL`, or
  DELETE `/containers/ID?v=false&force=false`. Starting an application can change
  its own data; retaining volumes is not a promise that their contents stay unchanged.
- Processes: **Terminate** (SIGTERM) or **Force kill** (SIGKILL), only for the
  dashboard user's processes. Fresh PID start-time and pidfd checks prevent
  killing a reused PID. The dashboard, its ancestors and core session processes
  are protected. A service manager may restart a killed process.
- Services: **Start service**, **Restart service** and **Stop service** for
  noncritical, loaded **user** units. Start is offered for inactive/failed units;
  Restart disrupts the service. Each dispatch freshly reads the exact user
  manager's state, rejects missing/masked/unverified units and checks both
  requested and canonical names against protection. Noncanonical aliases are
  refused too; an alias never grants protected access. Start/Restart require
  `CanStart=yes` and `RefuseManualStart=no`; a valid Stop does **not** require
  manual-start usability. The only command is
  `systemctl --user VERB -- UNIT`, bounded to **12 seconds**, with fixed verbs.
  A stopped unit may leave the capped loaded inventory: its open details retain
  the identity, and Start/Restart revalidate it through the exact user-manager
  read instead of treating sampled-list absence as permission or disappearance.
  System units,
  this dashboard, the disk guard and protected infrastructure stay view-only.
  No root, enable or unmask privileges are granted.

The fixed `POST /api/action` retains exact body shapes: containers use
`kind`, `engine`, `id`, `action`; services use `kind`, `scope:"user"`, `unit`,
`action`; processes retain their PID/start-ticks body. Unknown fields,
client-selected commands/methods/paths and unsupported verbs are rejected.
The advertised `actions.container_actions` / `actions.service_actions` lists
gate choices and dispatch. When an older backend lacks those lists, new assets
retain legacy Stop/Kill/Remove and user-service Stop only; they never post new
recovery verbs or fetch unsupported action history.

### Exact post-action observations

Workload results retain their command message, action ID and `completed_at`
receipt fields, adding a separate `observation` with `status`, `state`,
`read_at` (server read time), `detail` and the exact target identity.
After an attempted command, containers use bounded
GET `/containers/FULL_ID/json` over the same existing Unix socket with an
independent **two-second total deadline** and the existing 2MiB response bound.
Returned IDs must equal the original full ID; only narrow state/protection
fields survive. Environments, command arguments and arbitrary metadata are discarded.
An exact HTTP404 is `state:"absent"` / `status:"absent"`, **not stopped or healthy**.
Capped-list disappearance, matching names and phone/server clock comparisons
are never proof of target state.

Processes are read using the original PID and start ticks: `present` means that
identity still exists, not that it is running or healthy; `missing`, `replaced`
and `unknown` stay distinct. A replacement is never re-signalled. Services use
only a fresh exact user-unit read, preserving load/active/substate and typed
result metadata. A stopped service can Start from retained details even if its
row vanishes. Known post-state redraws available controls; an unknown/absent/
replaced observation grants no unsafe action.

Failed reads (including malformed JSON/Unicode) produce an explicit
`observation.state:"unknown"` without turning a real successful workload
command receipt into a failure or claiming application health. Actual command
timeouts, signal-terminated helpers and lost outcome audits remain uncertain;
an observed target state does not erase that uncertainty. Cleanup retains its
separate dispatch-only/unknown semantics below. No observation automatically
replays a mutation.

### Run guarded cleanup

With controls enabled, **Run guarded cleanup** opens the same named Maintenance
detail/confirmation flow. Cancel sends no request; repeated Confirm taps send
only one. It schedules only the already-installed
`system-janitor-catchup.service` for the dashboard's user. An existing runner needs
no wrapper changes; the guard's eligibility, leases, locks,
budgets, cooldown and native Janitor phase remain unchanged. Busy work can defer,
and a real check can legitimately reclaim zero bytes. There is no force mode,
bare Janitor sweep, arbitrary unit/path/mode or root fallback.

Dashboard installation **does not install this optional guarded runner**.
Without an independently installed, usable runner, cleanup stays unavailable;
the dashboard does not provision a replacement or bypass its protections.
To investigate a disabled cleanup control, inspect its user-manager facts:

```bash
systemctl --user show --no-pager \
  --property=Id,LoadState,ActiveState,SubState,CanStart,RefuseManualStart \
  -- system-janitor-catchup.service
```

`LoadState=loaded` alone is not permission to run. The dashboard also checks
canonical identity, manual-start policy and fresh state. Failed or malformed
reads remain explicitly unknown, never a fallback cleanup.

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
inactivity timeout that resets on each byte. Inventory and exact container post-reads each use a separate
two-second total transport budget. Unexpected mutation statuses are rejected
without reading their response bodies.

Private `$XDG_STATE_HOME/agent-toolkit/dashboard-actions.jsonl` (default
`$HOME/.local/state/agent-toolkit/dashboard-actions.jsonl` when the variable is
unset) records an action ID, sanitized target,
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

### Bounded actual action history

With controls enabled, fixed GET `/api/actions` uses the **same GET Host/Origin,
CIDR and authentication policy** as snapshots; it needs no action nonce.
Disabled controls return403 (missing authentication still returns401 under
token policy). There are no query selectors, arbitrary log paths or mutation
methods. The response contains `events` (at most **20 actual journal events**,
newest retained events in file order), `status`, server `read_at`, `truncated`,
`detail`, and a `quality` object:

- `tail_truncated`, `events_truncated`: boolean bounds/coverage indicators;
- `invalid_lines`: integer count of invalid complete entries, including
  invalid UTF/JSON, excessive nesting and malformed numeric fields;
- `incomplete_first_line`, `incomplete_tail`: skipped partial boundary lines;
- `changed_during_read`: visible concurrent file-size change or short read.

Only the current file's **64KiB bounded tail** is read; rotated files are not
searched. The reader reuses owned mode-0600 regular-file, no-symlink/no-hardlink
checks and the writer's directory safety checks. It never mkdirs, flocks,
writes, rotates, repairs or reads the whole growing log. `status:"no_history_yet"`
(`NoHistoryYet`) is distinct from `"unavailable"` (unsafe/unreadable history)
and `"partial"` (quality/coverage gaps), not a fabricated healthy empty result.

Curated fields are actual action ID, kind, fixed verb, exact target, event/time,
integer HTTP status, a message capped at512 characters and optional narrow
observation/cleanup dispatch fields (detail capped at240 characters). Tokens,
nonces, environments, arguments and arbitrary extras are never published.
Cleanup events retain `accepted`/`observed` scheduling semantics, never a
cleanup-completed event. A `started` record with no terminal event in the
bounded window remains **unresolved**, not failed or completed.

Recent activity may relate events only by an equal **actual action ID**.
A lost response or action ID is an **UNLINKED browser attempt**; it is never
attached to a journal event by target name or time. Refreshing, reopening a
dialog, resuming updates and reading history never replay actions.
This reuses the existing journal: no new logs, database or dependencies.

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
Version `0.7.0` retains `guarded-cleanup` / `cleanup-job-state` and appends literal
`action-history`, `observed-action-state` and `workload-recovery` to the
alphabetically sorted, append-only capability list and enabled
`actions.capabilities`. Enabled metadata also advertises
`container_actions:["kill","remove","restart","start","stop"]` and
`service_actions:["restart","start","stop"]`. Older lists absent means legacy
choices only and no history fetch; read-only/default capability strings,
observer defaults and the existing LAN/TLS access policy are retained.

To rotate a token, stop the service, move its private token file to a private
backup, rerun `--init`, and restart. Existing browser sessions will need the new
link. Stopping/disabling the dashboard leaves all maintenance jobs untouched.

### Stop and disable monitoring

Only run this when you intend to stop the dashboard and disable its startup:

```bash
systemctl --user disable --now agent-toolkit-dashboard.service
```
