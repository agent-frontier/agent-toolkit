#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/install.sh"
JANITOR_SCRIPT="$REPO_ROOT/system-janitor.sh"
UPDATER_SCRIPT="$REPO_ROOT/system-updater.sh"
WORK_DIR="$REPO_ROOT/tests/.install-smoke.$$"
trap 'rm -rf "$WORK_DIR"' EXIT
mkdir -p "$WORK_DIR"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "ok: $*"
}

make_home() {
  local name="$1"
  local home_dir="$WORK_DIR/$name"
  mkdir -p "$home_dir/bin"
  cat > "$home_dir/bin/crontab" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
CRON_FILE="$HOME/.crontab.stub"
case "${1:-}" in
  -l)
    if [ -f "$CRON_FILE" ]; then
      cat "$CRON_FILE"
      exit 0
    fi
    echo "no crontab for ${USER:-stub}" >&2
    exit 1
    ;;
  -)
    cat > "$CRON_FILE"
    ;;
  *)
    echo "unsupported crontab args: $*" >&2
    exit 2
    ;;
esac
STUB
  chmod +x "$home_dir/bin/crontab"
  printf '%s' "$home_dir"
}

run_install() {
  local home_dir="$1"
  shift
  XDG_CONFIG_HOME="$home_dir/.config" HOME="$home_dir" PATH="$home_dir/bin:$PATH" "$INSTALL_SCRIPT" "$@"
}

assert_symlink_target() {
  local path="$1"
  local expected="$2"
  [ -L "$path" ] || fail "$path is not a symlink"
  local resolved
  if command -v realpath >/dev/null 2>&1; then
    resolved="$(realpath "$path")"
  else
    resolved="$(readlink -f "$path")"
  fi
  [ "$resolved" = "$expected" ] || fail "$path resolved to $resolved (expected $expected)"
}

assert_one_line() {
  local needle="$1"
  local file="$2"
  local count
  count=$(grep -Fxc "$needle" "$file" || true)
  [ "$count" -eq 1 ] || fail "expected exactly one line '$needle' in $file (found $count)"
}

assert_manifest_shape() {
  local manifest_path="$1"
  local expected_root="$2"
  python3 - "$manifest_path" "$expected_root" <<'PY'
import json
import os
import sys

manifest_path, expected_root = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as fh:
    data = json.load(fh)
required = {"name", "version", "install_root", "tools", "docs", "repo_url"}
missing = sorted(required - set(data))
if missing:
    raise SystemExit(f"missing top-level keys: {missing}")
if data["name"] != "agent-toolkit":
    raise SystemExit(f"unexpected name: {data['name']}")
install_root = data["install_root"]
if not os.path.isabs(install_root):
    raise SystemExit(f"install_root is not absolute: {install_root}")
if os.path.realpath(install_root) != os.path.realpath(expected_root):
    raise SystemExit(f"install_root mismatch: {install_root} != {expected_root}")
if not os.path.isfile(os.path.join(install_root, "system-janitor.sh")):
    raise SystemExit(f"install_root does not contain system-janitor.sh: {install_root}")
tools = data["tools"]
if not isinstance(tools, list) or len(tools) != 2:
    raise SystemExit(f"tools must be a list of length 2: {tools!r}")
if [tool.get("name") for tool in tools] != ["system-janitor", "system-updater"]:
    raise SystemExit(f"unexpected tool names: {[tool.get('name') for tool in tools]!r}")
for tool in tools:
    if not os.path.isabs(tool.get("binary", "")):
        raise SystemExit(f"tool binary is not absolute: {tool!r}")
    if not os.path.isabs(tool.get("source", "")):
        raise SystemExit(f"tool source is not absolute: {tool!r}")
docs = data["docs"]
if not isinstance(docs, dict):
    raise SystemExit(f"docs must be an object: {docs!r}")
for key in ("agents_md", "agents_dir"):
    value = docs.get(key, "")
    if not os.path.isabs(value):
        raise SystemExit(f"docs.{key} is not absolute: {value!r}")
if data["repo_url"] != "https://github.com/agent-frontier/agent-toolkit":
    raise SystemExit(f"unexpected repo_url: {data['repo_url']}")
PY
}

normalize_manifest_without_generated_at() {
  local manifest_path="$1"
  local output_path="$2"
  python3 - "$manifest_path" "$output_path" <<'PY'
import json
import sys

manifest_path, output_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as fh:
    data = json.load(fh)
data["generated_at"] = "__normalized_generated_at__"
with open(output_path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
}

echo "── syntax check ─────────────────────────────────────────────"
bash -n "$INSTALL_SCRIPT"
bash -n "$REPO_ROOT/tests/install-smoke.sh"
pass "bash -n install.sh tests/install-smoke.sh"

echo "── create + repair + idempotency ────────────────────────────"
HOME_ONE="$(make_home home-one)"
mkdir -p "$HOME_ONE/.local/bin"
ln -s "$HOME_ONE/system-janitor/system-janitor.sh" "$HOME_ONE/.local/bin/system-janitor"
cat > "$HOME_ONE/.crontab.stub" <<EOF
# existing unrelated job
5 1 * * * /usr/bin/true
# system-janitor — weekly disk cleanup (managed by agent-toolkit install.sh)
17 3 * * 0 $HOME_ONE/system-janitor/system-janitor.sh
# system-updater — daily apt update sweep (managed by agent-toolkit install.sh)
23 4 * * * $HOME_ONE/system-janitor/system-updater.sh
EOF

run_install "$HOME_ONE" > "$WORK_DIR/install.out"
pass "install.sh completed"

JANITOR_EXPECTED="$(realpath "$JANITOR_SCRIPT")"
UPDATER_EXPECTED="$(realpath "$UPDATER_SCRIPT")"
assert_symlink_target "$HOME_ONE/.local/bin/system-janitor" "$JANITOR_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/system-updater" "$UPDATER_EXPECTED"
pass "symlinks created/repaired"

CRON_FILE="$HOME_ONE/.crontab.stub"
JANITOR_COMMENT='# system-janitor — weekly disk cleanup (managed by agent-toolkit install.sh)'
JANITOR_JOB="17 3 * * 0 $HOME_ONE/.local/bin/system-janitor"
UPDATER_COMMENT='# system-updater — daily apt update sweep (managed by agent-toolkit install.sh)'
UPDATER_JOB="23 4 * * * $HOME_ONE/.local/bin/system-updater"
assert_one_line "$JANITOR_COMMENT" "$CRON_FILE"
assert_one_line "$JANITOR_JOB" "$CRON_FILE"
assert_one_line "$UPDATER_COMMENT" "$CRON_FILE"
assert_one_line "$UPDATER_JOB" "$CRON_FILE"
pass "cron entries present exactly once"

grep -Fq 'repaired' "$WORK_DIR/install.out" || fail 'expected repair summary in install output'
pass "summary reports repairs"

MANIFEST_PATH_ONE="$HOME_ONE/.config/agent-toolkit/manifest.json"
[ -f "$MANIFEST_PATH_ONE" ] || fail "manifest was not created at $MANIFEST_PATH_ONE"
python3 -c "import json; json.load(open('$MANIFEST_PATH_ONE', encoding='utf-8'))" || fail 'manifest.json did not parse as JSON'
assert_manifest_shape "$MANIFEST_PATH_ONE" "$REPO_ROOT"
normalize_manifest_without_generated_at "$MANIFEST_PATH_ONE" "$WORK_DIR/manifest-first.normalized.json"
pass "manifest written with expected shape"

cp "$CRON_FILE" "$WORK_DIR/cron.before"
run_install "$HOME_ONE" > "$WORK_DIR/install-second.out"
cmp -s "$WORK_DIR/cron.before" "$CRON_FILE" || fail 'second install changed crontab contents'
assert_symlink_target "$HOME_ONE/.local/bin/system-janitor" "$JANITOR_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/system-updater" "$UPDATER_EXPECTED"
assert_one_line "$JANITOR_JOB" "$CRON_FILE"
assert_one_line "$UPDATER_JOB" "$CRON_FILE"
pass "second run is idempotent"

grep -Fq 'already-present' "$WORK_DIR/install-second.out" || fail 'expected already-present summary on second run'
normalize_manifest_without_generated_at "$MANIFEST_PATH_ONE" "$WORK_DIR/manifest-second.normalized.json"
cmp -s "$WORK_DIR/manifest-first.normalized.json" "$WORK_DIR/manifest-second.normalized.json" || fail 'manifest content changed across idempotent install (excluding generated_at)'
pass "second run reports already-present"
pass "manifest remains byte-identical excluding generated_at"

echo "── dry-run ──────────────────────────────────────────────────"
HOME_DRY="$(make_home home-dry)"
run_install "$HOME_DRY" --dry-run > "$WORK_DIR/dry-run.out"
[ ! -e "$HOME_DRY/.local/bin/system-janitor" ] || fail '--dry-run created janitor symlink'
[ ! -e "$HOME_DRY/.local/bin/system-updater" ] || fail '--dry-run created updater symlink'
[ ! -e "$HOME_DRY/.crontab.stub" ] || fail '--dry-run wrote crontab stub'
[ ! -e "$HOME_DRY/.config/agent-toolkit/manifest.json" ] || fail '--dry-run created manifest.json'
grep -Fq 'would have created' "$WORK_DIR/dry-run.out" || fail '--dry-run output missing preview text'
grep -Fq 'manifest               : would have written -> ' "$WORK_DIR/dry-run.out" || fail '--dry-run output missing manifest preview'
pass "--dry-run has no side effects"

echo "── quiet ────────────────────────────────────────────────────"
HOME_QUIET="$(make_home home-quiet)"
run_install "$HOME_QUIET" --quiet > "$WORK_DIR/quiet.out"
grep -Fq 'manifest               : written -> ' "$WORK_DIR/quiet.out" || fail '--quiet did not print manifest write status'
if grep -Fq 'symlink system-janitor' "$WORK_DIR/quiet.out"; then
  fail '--quiet printed full summary instead of manifest-only status'
fi
assert_symlink_target "$HOME_QUIET/.local/bin/system-janitor" "$JANITOR_EXPECTED"
assert_symlink_target "$HOME_QUIET/.local/bin/system-updater" "$UPDATER_EXPECTED"
[ -f "$HOME_QUIET/.crontab.stub" ] || fail '--quiet run did not install cron entries'
pass "--quiet suppresses regular summary but prints manifest changes"

echo
echo "all install smoke checks passed"
