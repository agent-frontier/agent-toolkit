#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO_BASE="$REPO_ROOT/demos/.workspaces"
WORKSPACE="$DEMO_BASE/cleanup-flow-$$-$(date +%s)"
STATE_DIR="$WORKSPACE/state"
HOME_DIR="$WORKSPACE/home"
CONFIG_PATH="$WORKSPACE/config"
TARGET_DIR="$WORKSPACE/fake-target"
FAKE_CRONTAB_DIR="$WORKSPACE/bin"
JANITOR_SCRIPT="$REPO_ROOT/system-janitor.sh"

cleanup() {
  rm -rf "$WORKSPACE"
  rmdir "$DEMO_BASE" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$STATE_DIR" "$HOME_DIR" "$TARGET_DIR" "$FAKE_CRONTAB_DIR"

cat > "$CONFIG_PATH" <<EOF
JANITOR_EXTRA_CLEANUP_DIRS="$TARGET_DIR/*"
EOF

cat > "$FAKE_CRONTAB_DIR/crontab" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-l" ]; then
  printf '17 3 * * 0 %s\n' "$JANITOR_DEMO_SCRIPT"
  exit 0
fi
printf 'demo crontab stub only supports -l\n' >&2
exit 1
EOF
chmod +x "$FAKE_CRONTAB_DIR/crontab"

for i in $(seq 1 50); do
  dd if=/dev/urandom of="$TARGET_DIR/blob-$i.bin" bs=204800 count=1 status=none
done

seed_bytes="$(du -sb "$TARGET_DIR" | awk '{print $1}')"

echo "Demo workspace: $WORKSPACE"
echo "Seeded fake-target bytes: $seed_bytes"
echo

echo '=== Step 1: Doctor check ==='
PATH="$FAKE_CRONTAB_DIR:$PATH" \
HOME="$HOME_DIR" \
JANITOR_DEMO_SCRIPT="$JANITOR_SCRIPT" \
JANITOR_LOG_DIR="$STATE_DIR" \
"$JANITOR_SCRIPT" --config "$CONFIG_PATH" --doctor

echo

echo '=== Step 2: Dry-run preview ==='
PATH="$FAKE_CRONTAB_DIR:$PATH" \
HOME="$HOME_DIR" \
JANITOR_LOG_DIR="$STATE_DIR" \
"$JANITOR_SCRIPT" --config "$CONFIG_PATH" --only extra_cleanup --dry-run
printf 'target_bytes_preview=%s\n' "$seed_bytes"
jq '{dry_run, freed_kb, freed_bytes_preview: (.freed_kb * 1024), sections}' "$STATE_DIR/last-run.json"

echo

echo '=== Step 3: Apply ==='
PATH="$FAKE_CRONTAB_DIR:$PATH" \
HOME="$HOME_DIR" \
JANITOR_LOG_DIR="$STATE_DIR" \
"$JANITOR_SCRIPT" --config "$CONFIG_PATH" --only extra_cleanup
printf 'remaining_files=%s\n' "$(find "$TARGET_DIR" -type f | wc -l | tr -d ' ')"

echo

echo '=== Step 4: Audit trail ==='
python3 -m json.tool "$STATE_DIR/last-run.json"

echo

echo '=== Step 5: Report summary ==='
JANITOR_LOG_DIR="$STATE_DIR" "$JANITOR_SCRIPT" --report --json | jq .
