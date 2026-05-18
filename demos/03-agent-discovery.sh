#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO_BASE="$REPO_ROOT/demos/.workspaces"
WORKSPACE="$DEMO_BASE/agent-discovery-$$-$(date +%s)"
HOME_DIR="$WORKSPACE/home"
BIN_DIR="$WORKSPACE/bin"
JANITOR_SCRIPT="$REPO_ROOT/system-janitor.sh"
UPDATER_SCRIPT="$REPO_ROOT/system-updater.sh"
MANIFEST_PATH="$HOME/.config/agent-toolkit/manifest.json"

cleanup() {
  rm -rf "$WORKSPACE"
  rmdir "$DEMO_BASE" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$HOME_DIR" "$BIN_DIR"

cat > "$BIN_DIR/crontab" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-l" ]; then
  printf '17 3 * * 0 %s\n' "$JANITOR_DEMO_SCRIPT"
  exit 0
fi
printf 'demo crontab stub only supports -l\n' >&2
exit 1
EOF
chmod +x "$BIN_DIR/crontab"

echo '=== Step 1: Host manifest ==='
if [ -f "$MANIFEST_PATH" ]; then
  echo '# installer-written manifest found on this host'
  jq . "$MANIFEST_PATH"
else
  echo '# no host manifest found; using a simulated example with the documented shape'
  jq . <<EOF
{
  "name": "agent-toolkit",
  "version": "0.1.0",
  "install_root": "$REPO_ROOT",
  "generated_at": "2026-05-18T19:52:26Z",
  "tools": [
    {
      "name": "system-janitor",
      "binary": "$REPO_ROOT/system-janitor.sh",
      "source": "$REPO_ROOT/system-janitor.sh"
    },
    {
      "name": "system-updater",
      "binary": "$REPO_ROOT/system-updater.sh",
      "source": "$REPO_ROOT/system-updater.sh"
    }
  ],
  "docs": {
    "agents_md": "$REPO_ROOT/AGENTS.md",
    "agents_dir": "$REPO_ROOT/docs/agents/"
  },
  "repo_url": "https://github.com/agent-frontier/agent-toolkit"
}
EOF
fi

echo

echo '=== Step 2: Per-tool capability discovery ==='
echo '# agents read these fields before invoking optional flags or sibling tools'
for tool_name in system-janitor system-updater; do
  case "$tool_name" in
    system-janitor) tool_cmd="$JANITOR_SCRIPT" ;;
    system-updater) tool_cmd="$UPDATER_SCRIPT" ;;
    *)
      echo "unknown demo tool: $tool_name" >&2
      exit 1
      ;;
  esac
  printf '\n# %s --version --json\n' "$tool_name"
  "$tool_cmd" --version --json | jq '{name, version, toolkit, capabilities}'
done

echo

echo '=== Step 3: Cross-reference (toolkit field) ==='
JANITOR_TOOLKIT="$($JANITOR_SCRIPT --version --json | jq -c '.toolkit')" \
UPDATER_TOOLKIT="$($UPDATER_SCRIPT --version --json | jq -c '.toolkit')" \
python3 - <<'PY'
import json
import os

janitor = json.loads(os.environ['JANITOR_TOOLKIT'])
updater = json.loads(os.environ['UPDATER_TOOLKIT'])
print(json.dumps({
    'identical': janitor == updater,
    'toolkit': janitor,
    'why_it_matters': 'agents discover sibling tools from one version probe',
}, indent=2))
PY

echo

echo '=== Step 4: Schemas published ==='
ls "$REPO_ROOT/schemas"
if python3 -c 'import jsonschema' >/dev/null 2>&1; then
  REPORT_JSON="$(JANITOR_LOG_DIR="$WORKSPACE/absent-state" "$JANITOR_SCRIPT" --report --json)"
  REPORT_JSON="$REPORT_JSON" python3 - "$REPO_ROOT/schemas/report.schema.json" <<'PY'
import json
import os
import sys
import jsonschema

with open(sys.argv[1], encoding='utf-8') as fh:
    schema = json.load(fh)
payload = json.loads(os.environ['REPORT_JSON'])
jsonschema.validate(payload, schema)
print('report.schema.json validation: PASS')
PY
else
  echo 'jsonschema not installed; schema file exists and can be validated locally.'
fi

echo

echo '=== Step 5: Doctor as a first-call ==='
echo '# agents check doctor output before risky work so wiring issues fail fast'
set +e
PATH="$BIN_DIR:$PATH" \
HOME="$HOME_DIR" \
JANITOR_DEMO_SCRIPT="$JANITOR_SCRIPT" \
"$JANITOR_SCRIPT" --doctor --json | jq .
doctor_rc=$?
set -e
[ "$doctor_rc" -eq 0 ]
