# Cookbook

Copy-pasteable fleet-ops recipes for `agent-toolkit`. These are aimed at
operators and sibling agents that need to consume the toolkit's machine-mode
outputs safely. Read [AGENTS.md](AGENTS.md) first for the design brief, then
cross-check field names in [docs/agents/machine-modes.md](./agents/machine-modes.md),
[docs/agents/contracts.md](./agents/contracts.md),
[docs/audit-trail.md](./audit-trail.md), and the formal schemas in
[`https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/`](https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/).

The examples below assume the default janitor state dir
`~/.local/state/janitor` and the default installer layout from
[`install.sh`](https://github.com/agent-frontier/agent-toolkit/blob/main/install.sh) / [`scripts/bootstrap.sh`](https://github.com/agent-frontier/agent-toolkit/blob/main/scripts/bootstrap.sh).
Where a recipe parses JSON, prefer the documented contracts over ad-hoc grep.

---

## 1) Prometheus exporter for `last-run.json`

**Recommended**: use `log-shipper.sh` (ships with the toolkit). See [docs/log-shipper.md](./log-shipper.md).

**Why you'd want this**: this turns the O(1) `last-run.json` snapshot into cheap Prometheus signals so you can alert on silent failure (the tool stopped running, or a section has gone stale) without reparsing the full audit log on every scrape.

**Alternative (standalone writer)**

Prerequisites: `jq`, a completed janitor `last-run.json`, and node-exporter
configured with a textfile collector at `/var/lib/node_exporter/textfile_collector`.
The cron user needs write access to that directory; the setup commands below
require `sudo`. Install/enable node-exporter using your distribution's package
instructions before using this recipe; `curl` is optional for checking its
metrics endpoint. Adjust the collector path if your host uses another location.
For the toolkit exporter instead, see [log-shipper installation](log-shipper-install.md).

Create a tiny textfile-collector writer. It reads
`~/.local/state/janitor/last-run.json`, converts the canonical janitor fields
from [docs/audit-trail.md](./audit-trail.md#last-runjson-schema-v02), and writes
an atomic `.prom` file into node_exporter's textfile collector directory.

```bash
sudo install -d -m 0755 /usr/local/lib/agent-toolkit
sudo tee /usr/local/lib/agent-toolkit/janitor-last-run-prometheus.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SRC="${HOME}/.local/state/janitor/last-run.json"
DEST="/var/lib/node_exporter/textfile_collector/agent_toolkit.prom"
TMP="${DEST}.tmp.$$"

[ -f "$SRC" ] || exit 0

jq -r '
  def ts: strptime("%Y-%m-%dT%H:%M:%S%z") | mktime;
  [
    "# HELP agent_toolkit_last_run_freed_bytes Bytes freed by the most recent janitor run.",
    "# TYPE agent_toolkit_last_run_freed_bytes gauge",
    ("agent_toolkit_last_run_freed_bytes{tool=\"janitor\"} " + (((.freed_kb // 0) * 1024) | tostring)),
    "# HELP agent_toolkit_last_run_timestamp_seconds Unix timestamp of the most recent janitor completion.",
    "# TYPE agent_toolkit_last_run_timestamp_seconds gauge",
    ("agent_toolkit_last_run_timestamp_seconds{tool=\"janitor\"} " + (((.ended_at // .finished) | ts) | tostring)),
    "# HELP agent_toolkit_last_run_safety_integrity_ok 1 when the most recent janitor run ended with safety_integrity=ok.",
    "# TYPE agent_toolkit_last_run_safety_integrity_ok gauge",
    ("agent_toolkit_last_run_safety_integrity_ok{tool=\"janitor\"} " + (if .safety_integrity == "ok" then "1" else "0" end))
  ]
  + [
      (.sections[]? |
        "agent_toolkit_last_run_section_freed_bytes{tool=\"janitor\",section=\""
        + (.name | @json | .[1:-1])
        + "\"} "
        + ((.freed_bytes // 0) | tostring))
    ]
  | .[]
' "$SRC" > "$TMP"

mv "$TMP" "$DEST"
EOF
sudo chmod 0755 /usr/local/lib/agent-toolkit/janitor-last-run-prometheus.sh
```

Run it after janitor's normal cron:

```cron
# Refresh Prometheus textfile after janitor's daily cron
22 3 * * * /usr/local/lib/agent-toolkit/janitor-last-run-prometheus.sh
```

Prometheus queries worth keeping around:

```promql
# Detect hosts whose last-run metric stayed at 0 for a week
max_over_time(agent_toolkit_last_run_freed_bytes{tool="janitor"}[7d]) == 0

# Last completion timestamp per host
max by (instance) (agent_toolkit_last_run_timestamp_seconds{tool="janitor"})

# Hosts whose latest run tripped the safety floor
agent_toolkit_last_run_safety_integrity_ok{tool="janitor"} == 0
```

Cross-checks: [docs/audit-trail.md](./audit-trail.md), [docs/agents/contracts.md#unit-canon](./agents/contracts.md#unit-canon), and [docs/agents/machine-modes.md#--health](./agents/machine-modes.md#--health).

**Gotchas**

- This is a **poll**, not a push; freshness is limited by the cron interval.
- `last-run.json` top-level still exposes `freed_kb`, not `freed_bytes`; convert with `* 1024` exactly as documented in [docs/agents/contracts.md](./agents/contracts.md#unit-canon).
- `sections[]` may be absent on pre-v0.2 snapshots; `.sections[]?` keeps top-level metrics working on older files.
- Write the `.prom` file atomically in the same directory so node_exporter never scrapes a partial file.

---

## 2) Ansible playbook deploying to a fleet

**Why you'd want this**: this gives you a repeatable, fail-fast rollout path for the toolkit across many hosts while refusing to treat an unhealthy install as a successful deployment.

**Copy-paste**

Inventory:

```ini
[linux_servers]
web-01
web-02
db-01
```

Playbook (`agent-toolkit.yml`):

```yaml
---
- name: Install and verify agent-toolkit
  hosts: linux_servers
  gather_facts: true
  become: true
  vars:
    toolkit_ref: v0.1.0
    bootstrap_user: "{{ ansible_user_id }}"

  pre_tasks:
    - name: Refuse to bootstrap as root
      ansible.builtin.fail:
        msg: >-
          Connect as an unprivileged user and use become for package installs;
          scripts/bootstrap.sh exits when run as root.
      when: bootstrap_user == 'root'

    - name: Ensure git and bash are installed
      ansible.builtin.package:
        name:
          - git
          - bash
        state: present

  tasks:
    - name: Bootstrap agent-toolkit checkout and install
      become: false
      ansible.builtin.script: scripts/bootstrap.sh --ref={{ toolkit_ref }}
      register: bootstrap_result
      changed_when: >-
        'already-present' not in bootstrap_result.stdout and
        'already-current' not in bootstrap_result.stdout

    - name: Run system-janitor doctor in JSON mode
      become: false
      ansible.builtin.command: system-janitor --doctor --json
      register: janitor_doctor
      changed_when: false
      failed_when: >-
        janitor_doctor.rc != 0 or
        ((janitor_doctor.stdout | from_json).status != 'healthy')

    - name: Show doctor output for healthy hosts too
      ansible.builtin.debug:
        msg: "{{ janitor_doctor.stdout | from_json }}"
```

Run it:

```bash
ansible-playbook -i inventory.ini agent-toolkit.yml
```

To harvest audit summaries after rollout and find hosts with non-empty
`idle_streaks`, use ad-hoc Ansible plus `jq`:

```bash
ANSIBLE_STDOUT_CALLBACK=json \
ansible linux_servers -i inventory.ini -m shell -a 'system-janitor --report --json' \
| jq -r '
    .plays[].tasks[].hosts
    | to_entries[]
    | .key as $host
    | .value.stdout
    | fromjson
    | select(.idle_streaks | length > 0)
    | {host: $host, idle_streaks}
  '
```

That maps directly onto the `idle` / `idle_streaks` contract in [docs/agents/contracts.md](./agents/contracts.md#status-enum) and [docs/agents/machine-modes.md](./agents/machine-modes.md#--report).

**Gotchas**

- `scripts/bootstrap.sh` explicitly refuses root; install packages with `become: true`, then run bootstrap and `system-janitor --doctor` with `become: false`.
- The `script` module runs a local file from the controller. Run the playbook from a checkout of this repo so `scripts/bootstrap.sh` exists locally.
- `system-janitor --doctor --json` is the right post-install probe here, not `--health --json`.
- A host can install successfully but still be unhealthy. Keep the `failed_when` guard; don't just check `rc == 0`.

---

## 3) Slack notifier on safety-integrity violation

**Why you'd want this**: exit code `2` and `last-run.json.safety_integrity != "ok"` mean the janitor safety floor was disturbed, which is the kind of failure you want paged immediately instead of discovering days later.

**Copy-paste**

Create a small cron-adjacent checker that posts only when the latest run ended
with a janitor integrity violation:

```bash
install -d -m 0755 ~/.local/lib/agent-toolkit
cat > ~/.local/lib/agent-toolkit/janitor-slack-integrity.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${HOME}/.local/state/janitor"
LAST_RUN="${STATE_DIR}/last-run.json"
WEBHOOK_URL="${SLACK_WEBHOOK_URL:?set SLACK_WEBHOOK_URL first}"

[ -f "$LAST_RUN" ] || exit 0

status="$(jq -r '.safety_integrity // "unknown"' "$LAST_RUN")"
[ "$status" = "ok" ] && exit 0

run_id="$(jq -r '.run_id // "unknown"' "$LAST_RUN")"
finished="$(jq -r '.ended_at // .finished // "unknown"' "$LAST_RUN")"
freed_kb="$(jq -r '.freed_kb // 0' "$LAST_RUN")"
host="$(hostname -f 2>/dev/null || hostname)"

payload="$(jq -n \
  --arg host "$host" \
  --arg run_id "$run_id" \
  --arg finished "$finished" \
  --arg status "$status" \
  --argjson freed_kb "$freed_kb" \
  '{text: (":warning: system-janitor safety_integrity=" + $status
           + " on " + $host
           + " (run_id=" + $run_id
           + ", finished=" + $finished
           + ", freed_kb=" + ($freed_kb|tostring) + ")")}')"

curl -fsS -X POST -H 'Content-Type: application/json' \
  --data "$payload" \
  "$WEBHOOK_URL" >/dev/null
EOF
chmod 0755 ~/.local/lib/agent-toolkit/janitor-slack-integrity.sh
```

Cron it a few minutes after janitor's normal daily run:

```cron
# Notify Slack if the latest janitor run tripped the safety floor
26 3 * * * SLACK_WEBHOOK_URL='https://hooks.slack.com/services/XXX/YYY/ZZZ' $HOME/.local/lib/agent-toolkit/janitor-slack-integrity.sh
```

Interpretation rules: janitor exit code `2` means integrity violation (see [docs/agents/contracts.md](./agents/contracts.md#exit-codes)), and `last-run.json.safety_integrity` carries the final state (`ok`, `violated_missing`, `violated_inode_changed`, etc.) per [docs/audit-trail.md](./audit-trail.md#last-runjson-schema-v02).

**Gotchas**

- This keys off the **latest completed run**. If janitor is still running, wait for it to finish.
- Run the notifier after janitor's cron entry, not at the same minute. `last-run.json` is written atomically at end-of-run.
- Keep the webhook in cron env or a sourced file, not in the repo.
- If you need duplicate suppression across repeated checks, persist the last notified `run_id` in the same state directory.

---

## 4) `jq` one-liners for JSONL forensics

**Why you'd want this**: sometimes you do not need a full parser or dashboard; a few correct `jq` one-liners over `janitor.jsonl` are enough to answer “what happened?” on a live host.

**Copy-paste**

Assume the default audit trail:

```bash
cd ~/.local/state/janitor
```

1. Runs whose final `run_end` event freed more than 10 GiB:

```bash
jq -c 'select(.section=="run_end" and (.freed_kb // 0) > 10485760)' janitor.jsonl
```

2. Per-section freed totals over the last 30 events:

```bash
tail -30 janitor.jsonl | jq -s 'group_by(.section) | map({section: .[0].section, total_freed_kb: (map(.freed_kb // 0) | add)})'
```

3. Sections that have only ever emitted `idle` in the log you have on disk:

```bash
jq -s 'group_by(.section) | map(select(length > 0 and all(.[]; .status == "idle")) | .[0].section)' janitor.jsonl
```

4. Slowest runs from `run_start.ts` / `run_end.ts`:

```bash
jq -s 'group_by(.run_id) | map({run_id: .[0].run_id, started_at: (map(select(.section == "run_start") | .ts) | first), ended_at: (map(select(.section == "run_end") | .ts) | first)} | select(.started_at and .ended_at) | .duration_seconds = ((.ended_at | strptime("%Y-%m-%dT%H:%M:%S%z") | mktime) - (.started_at | strptime("%Y-%m-%dT%H:%M:%S%z") | mktime))) | sort_by(.duration_seconds) | reverse | .[:10]' janitor.jsonl
```

5. All warning events with their notes:

```bash
jq -c 'select(.status=="warn") | {run_id, section, note}' janitor.jsonl
```

6. Safety-floor outcomes only:

```bash
jq -c 'select(.section=="safety_integrity") | {ts, run_id, status, note}' janitor.jsonl
```

7. Latest event seen for each section name:

```bash
jq -s 'sort_by(.ts) | group_by(.section) | map(.[-1] | {section, ts, status, freed_kb, items})' janitor.jsonl
```

8. Total freed bytes by section (`freed_kb * 1024` for JSONL events):

```bash
jq -s 'group_by(.section) | map({section: .[0].section, freed_bytes: ((map(.freed_kb // 0) | add) * 1024)}) | sort_by(.freed_bytes) | reverse' janitor.jsonl
```

9. Runs that had at least one warning anywhere in the run:

```bash
jq -s 'group_by(.run_id) | map(select(any(.[]; .status == "warn")) | {run_id: .[0].run_id, sections: map(.section)})' janitor.jsonl
```

10. Event counts by status and section:

```bash
jq -s 'group_by(.section) | map({section: .[0].section, counts: (group_by(.status) | map({key: .[0].status, value: length}) | from_entries)})' janitor.jsonl
```

These complement `system-janitor --report --json`; use the full report when you want the canonical rollup shape from [`schemas/report.schema.json`](https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/report.schema.json).

**Gotchas**

- `janitor.jsonl` events still use `freed_kb`, not `freed_bytes`; convert using the unit canon in [docs/agents/contracts.md](./agents/contracts.md#unit-canon).
- `run_start` and `run_end` are meta events; filter them out when you only want action sections.
- Historical alias names can exist forever in JSONL. If you need canonical grouping across long history, prefer `system-janitor --report --json`, which applies alias remapping before obsolete/meta filtering.
- `tail -30 janitor.jsonl` means “last 30 events”, **not** “last 30 runs”. One janitor run emits multiple events.

---

## 5) Reading the toolkit manifest from another agent

**Why you'd want this**: a sibling agent can use the installer-written manifest as the fast answer to “which toolkit binaries are present on this host, and where did they come from?” without shelling out to discovery logic first.

**Copy-paste**

Read the manifest directly from the default installer location:

```bash
python3 -c '
import json, pathlib
manifest_path = pathlib.Path.home() / ".config/agent-toolkit/manifest.json"
m = json.loads(manifest_path.read_text())
print("toolkit", m.get("name"), m.get("version", "unknown"), m.get("install_root"))
for t in m.get("tools", []):
    print(t["name"], t["binary"], t.get("source", "unknown"))
'
```

Current `install.sh` writes this shape:

```json
{
  "name": "agent-toolkit",
  "version": "0.2.0",
  "install_root": "/home/example/agent-toolkit",
  "generated_at": "2026-05-18T19:52:26Z",
  "tools": [
    {
      "name": "cert-watch",
      "binary": "/home/example/.local/bin/cert-watch",
      "source": "/home/example/agent-toolkit/cert-watch.sh"
    },
    {
      "name": "log-shipper",
      "binary": "/home/example/.local/bin/log-shipper",
      "source": "/home/example/agent-toolkit/log-shipper.sh"
    },
    {
      "name": "system-janitor",
      "binary": "/home/example/.local/bin/system-janitor",
      "source": "/home/example/agent-toolkit/system-janitor.sh"
    },
    {
      "name": "system-updater",
      "binary": "/home/example/.local/bin/system-updater",
      "source": "/home/example/agent-toolkit/system-updater.sh"
    }
  ],
  "docs": {
    "agents_md": "/home/example/agent-toolkit/AGENTS.md",
    "agents_dir": "/home/example/agent-toolkit/docs/agents/"
  },
  "repo_url": "https://github.com/agent-frontier/agent-toolkit"
}
```

Field guide: `name` and `version` identify the toolkit release, `install_root` and `generated_at` tell you where/when the install was written, `tools[]` carries per-tool `name`/`binary`/`source`, `docs.*` points back to [AGENTS.md](AGENTS.md) and [docs/agents/](./agents/README.md), and `repo_url` records the canonical upstream.

If you need richer per-tool metadata after resolving the binary, chain this with machine-mode discovery:

```bash
system-janitor --version --json
system-updater --version --json
```

Those payloads advertise `capabilities[]` and the top-level `toolkit` object; see [docs/agents/machine-modes.md](./agents/machine-modes.md#--version) and [docs/agents/contracts.md](./agents/contracts.md#capability-contract).

**Gotchas**

- The manifest is an installer artifact, not a formal JSON Schema contract yet; read it defensively and ignore unknown future fields.
- The current installer writes `source`, not `config`, `state_dir`, or per-tool `version`. If you need config/state inspection, use `system-janitor --doctor` or the documented default locations.
- `version` is top-level in the current shape. Do not assume every `tools[]` entry also carries its own version.
- On a host installed by an older or hand-edited setup, the manifest may be missing or stale; fall back to `$PATH` / `--version --json` discovery if needed.
- The MCP server also consults the manifest during binary resolution; see [mcp-server/README.md#binary-resolution](https://github.com/agent-frontier/agent-toolkit/blob/main/mcp-server/README.md#binary-resolution).

---

## 6) MCP server integration with Claude Desktop

**Why you'd want this**: this lets a desktop agent reason over janitor and updater state through read-only MCP tools first, with destructive actions kept behind explicit opt-in parameters and warning text.

**Copy-paste**

Install the MCP server from this repo checkout today:

```bash
pip install --user -e mcp-server/
agent-toolkit-mcp --version
```

Once published, the install becomes:

```bash
pip install --user agent-toolkit-mcp
```

Add it to Claude Desktop.

Linux (`~/.config/Claude/claude_desktop_config.json`) and macOS (`~/Library/Application Support/Claude/claude_desktop_config.json`) both use the same server entry:

```json
{
  "mcpServers": {
    "agent-toolkit": {
      "command": "agent-toolkit-mcp"
    }
  }
}
```

Restart Claude Desktop, then test with a read-only prompt:

```text
What does my disk look like? Please call janitor_report and summarize total_freed_bytes, most_recent_run, and any idle_streaks.
```

Good end-to-end prompts:

```text
Call janitor_health and tell me whether the janitor audit trail is trustworthy.
```

```text
Run janitor_run in preview mode only for docker_prune and summarize the returned last-run.json.
```

```text
Call updater_report and tell me whether there are packages repeatedly held or failing.
```

Default-safe rule to preserve:

- `janitor_run` is safe by default because `dry_run=true` unless explicitly set false.
- `updater_run` is safe by default because `apply=false` unless explicitly set true.
- The tool descriptions in [mcp-server/README.md](https://github.com/agent-frontier/agent-toolkit/blob/main/mcp-server/README.md) warn that `janitor_run --dry-run=false` and `updater_run --apply=true` are destructive.

Equivalent MCP docs for other clients:

- **Cursor**: register `agent-toolkit-mcp` as a stdio MCP server in Cursor's MCP config (`~/.cursor/mcp.json` or project `.cursor/mcp.json`). Official docs: <https://cursor.com/docs/mcp>.
- **Continue**: drop the server definition into Continue's MCP config and keep it in agent mode. Official docs: <https://docs.continue.dev/customize/deep-dives/mcp>.
- **Cline**: add the stdio server under Cline's MCP server configuration. Official docs: <https://docs.cline.bot/mcp/mcp-overview>.

Relevant repo docs: [mcp-server/README.md](https://github.com/agent-frontier/agent-toolkit/blob/main/mcp-server/README.md), [docs/agents/machine-modes.md](./agents/machine-modes.md), [`schemas/report.schema.json`](https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/report.schema.json), and [`schemas/health.schema.json`](https://github.com/agent-frontier/agent-toolkit/tree/main/schemas/health.schema.json).

**Gotchas**

- If `agent-toolkit-mcp` is not on Claude's PATH, replace `"command"` with the full executable path from your user bin dir.
- Claude Desktop needs a full restart after config changes.
- Binary resolution prefers env overrides, then `~/.local/bin`, then `$AGENT_TOOLKIT_HOME`, then `~/.config/agent-toolkit/manifest.json`, then `PATH`; see [mcp-server/README.md#binary-resolution](https://github.com/agent-frontier/agent-toolkit/blob/main/mcp-server/README.md#binary-resolution).
- Keep destructive flags explicit in chat. “Run janitor” is ambiguous; “run `janitor_run` with `dry_run=true`” is not.
- Test the integration with a read-only tool first (`janitor_report`, `janitor_health`, `janitor_version`) before allowing any mutating tool call.
