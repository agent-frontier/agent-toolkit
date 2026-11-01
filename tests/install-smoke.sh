#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/install.sh"
CERTWATCH_SCRIPT="$REPO_ROOT/cert-watch.sh"
LOG_SHIPPER_SCRIPT="$REPO_ROOT/log-shipper.sh"
JANITOR_SCRIPT="$REPO_ROOT/system-janitor.sh"
UPDATER_SCRIPT="$REPO_ROOT/system-updater.sh"
CRON_RUN_SCRIPT="$REPO_ROOT/scripts/cron-run.sh"
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
if not os.path.isfile(os.path.join(install_root, "cert-watch.sh")):
    raise SystemExit(f"install_root does not contain cert-watch.sh: {install_root}")
if not os.path.isfile(os.path.join(install_root, "log-shipper.sh")):
    raise SystemExit(f"install_root does not contain log-shipper.sh: {install_root}")
tools = data["tools"]
if not isinstance(tools, list) or len(tools) != 4:
    raise SystemExit(f"tools must be a list of length 4: {tools!r}")
if [tool.get("name") for tool in tools] != ["cert-watch", "log-shipper", "system-janitor", "system-updater"]:
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
5 2 * * * /usr/bin/echo system-janitor
17 2 * * * '/old/.local/bin/agent-toolkit-cron' '/old/.local/bin/system-janitor'
42 6 * * * '/old/.local/bin/agent-toolkit-cron' '/old/.local/bin/cert-watch'
23 3 * * * '/old/.local/bin/agent-toolkit-cron' '/old/.local/bin/system-updater'
7 * * * * '/old/.local/bin/agent-toolkit-cron' '/old/.local/bin/log-shipper'
# cert-watch — daily TLS certificate inventory (managed by agent-toolkit install.sh)
42 6 * * * $HOME_ONE/system-janitor/cert-watch.sh
# system-janitor — weekly disk cleanup (managed by agent-toolkit install.sh)
17 3 * * 0 $HOME_ONE/system-janitor/system-janitor.sh
# system-updater — daily apt update sweep (managed by agent-toolkit install.sh)
23 4 * * * $HOME_ONE/system-janitor/system-updater.sh
# log-shipper — hourly audit-trail export (managed by agent-toolkit install.sh)
7 * * * * $HOME_ONE/system-janitor/log-shipper.sh
EOF

run_install "$HOME_ONE" > "$WORK_DIR/install.out"
pass "install.sh completed"

CERTWATCH_EXPECTED="$(realpath "$CERTWATCH_SCRIPT")"
LOG_SHIPPER_EXPECTED="$(realpath "$LOG_SHIPPER_SCRIPT")"
JANITOR_EXPECTED="$(realpath "$JANITOR_SCRIPT")"
UPDATER_EXPECTED="$(realpath "$UPDATER_SCRIPT")"
assert_symlink_target "$HOME_ONE/.local/bin/cert-watch" "$CERTWATCH_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/log-shipper" "$LOG_SHIPPER_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/system-janitor" "$JANITOR_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/system-updater" "$UPDATER_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/agent-toolkit-cron" "$(realpath "$CRON_RUN_SCRIPT")"
pass "symlinks created/repaired"

CRON_FILE="$HOME_ONE/.crontab.stub"
CERTWATCH_COMMENT='# cert-watch — hourly TLS certificate inventory (managed by agent-toolkit install.sh)'
CERTWATCH_JOB="42 * * * * '$HOME_ONE/.local/bin/agent-toolkit-cron' '$HOME_ONE/.local/bin/cert-watch'"
LOG_SHIPPER_COMMENT='# log-shipper — quarter-hourly audit-trail export (managed by agent-toolkit install.sh)'
LOG_SHIPPER_JOB="7,22,37,52 * * * * '$HOME_ONE/.local/bin/agent-toolkit-cron' '$HOME_ONE/.local/bin/log-shipper'"
JANITOR_COMMENT='# system-janitor — daily disk cleanup (managed by agent-toolkit install.sh)'
JANITOR_JOB="17 3 * * * '$HOME_ONE/.local/bin/agent-toolkit-cron' '$HOME_ONE/.local/bin/system-janitor'"
UPDATER_COMMENT='# system-updater — daily apt update sweep (managed by agent-toolkit install.sh)'
UPDATER_JOB="23 4 * * * '$HOME_ONE/.local/bin/agent-toolkit-cron' '$HOME_ONE/.local/bin/system-updater'"
assert_one_line "$CERTWATCH_COMMENT" "$CRON_FILE"
assert_one_line "$CERTWATCH_JOB" "$CRON_FILE"
assert_one_line "$LOG_SHIPPER_COMMENT" "$CRON_FILE"
assert_one_line "$LOG_SHIPPER_JOB" "$CRON_FILE"
assert_one_line "$JANITOR_COMMENT" "$CRON_FILE"
assert_one_line "$JANITOR_JOB" "$CRON_FILE"
assert_one_line "$UPDATER_COMMENT" "$CRON_FILE"
assert_one_line "$UPDATER_JOB" "$CRON_FILE"
assert_one_line '5 1 * * * /usr/bin/true' "$CRON_FILE"
assert_one_line '5 2 * * * /usr/bin/echo system-janitor' "$CRON_FILE"
! grep -Fq '/old/.local/bin/' "$CRON_FILE" || fail 'stale quoted launcher jobs survived migration'
pass "cron entries present exactly once; quoted jobs migrated, unrelated jobs preserved"
# Public help must not contradict the installed cadence after a schedule change.
"$JANITOR_SCRIPT" --help | grep -F '17 3 * * *' >/dev/null || fail 'janitor help cadence stale'
"$CERTWATCH_SCRIPT" --help | grep -F '42 * * * *' >/dev/null || fail 'cert help cadence stale'
"$LOG_SHIPPER_SCRIPT" --help | grep -F '7,22,37,52 * * * *' >/dev/null || fail 'export help cadence stale'

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
assert_symlink_target "$HOME_ONE/.local/bin/cert-watch" "$CERTWATCH_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/log-shipper" "$LOG_SHIPPER_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/system-janitor" "$JANITOR_EXPECTED"
assert_symlink_target "$HOME_ONE/.local/bin/system-updater" "$UPDATER_EXPECTED"
assert_one_line "$CERTWATCH_JOB" "$CRON_FILE"
assert_one_line "$LOG_SHIPPER_JOB" "$CRON_FILE"
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
[ ! -e "$HOME_DRY/.local/bin/cert-watch" ] || fail '--dry-run created cert-watch symlink'
[ ! -e "$HOME_DRY/.local/bin/log-shipper" ] || fail '--dry-run created log-shipper symlink'
[ ! -e "$HOME_DRY/.local/bin/system-janitor" ] || fail '--dry-run created janitor symlink'
[ ! -e "$HOME_DRY/.local/bin/system-updater" ] || fail '--dry-run created updater symlink'
[ ! -e "$HOME_DRY/.local/bin/agent-toolkit-cron" ] || fail '--dry-run created cron launcher symlink'
[ ! -e "$HOME_DRY/.crontab.stub" ] || fail '--dry-run wrote crontab stub'
[ ! -e "$HOME_DRY/.config/agent-toolkit/manifest.json" ] || fail '--dry-run created manifest.json'
grep -Fq 'would have created' "$WORK_DIR/dry-run.out" || fail '--dry-run output missing preview text'
grep -Fq 'manifest               : would have written -> ' "$WORK_DIR/dry-run.out" || fail '--dry-run output missing manifest preview'
for expected in \
  "42 * * * * '$HOME_DRY/.local/bin/agent-toolkit-cron' '$HOME_DRY/.local/bin/cert-watch'" \
  "7,22,37,52 * * * * '$HOME_DRY/.local/bin/agent-toolkit-cron' '$HOME_DRY/.local/bin/log-shipper'" \
  "17 3 * * * '$HOME_DRY/.local/bin/agent-toolkit-cron' '$HOME_DRY/.local/bin/system-janitor'" \
  "23 4 * * * '$HOME_DRY/.local/bin/agent-toolkit-cron' '$HOME_DRY/.local/bin/system-updater'"; do
  grep -Fq "$expected" "$WORK_DIR/dry-run.out" || fail 'dry-run omitted an exact scheduled command'
done
pass "--dry-run has no side effects and previews exact commands"

echo "── launcher real-file collision ─────────────────────────────"
HOME_COLLISION="$(make_home home-collision)"
mkdir -p "$HOME_COLLISION/.local/bin"
printf 'user-owned executable\n' > "$HOME_COLLISION/.local/bin/agent-toolkit-cron"
if run_install "$HOME_COLLISION" > "$WORK_DIR/collision.out" 2>&1; then
  fail 'installer overwrote a real launcher file'
fi
grep -Fq 'user-owned executable' "$HOME_COLLISION/.local/bin/agent-toolkit-cron" || fail 'collision file changed'
[ ! -e "$HOME_COLLISION/.crontab.stub" ] || fail 'collision changed cron'
pass "launcher real-file collision refused without clobbering"

echo "── spaced and percent HOME cron execution ─────────────────"
HOME_SPECIAL="$(make_home "home special%slash\\%quote'end")"
run_install "$HOME_SPECIAL" > "$WORK_DIR/special.out"
rm "$HOME_SPECIAL/.local/bin/system-janitor"
cat > "$HOME_SPECIAL/.local/bin/system-janitor" <<'STUB'
#!/usr/bin/env bash
printf 'janitor stub ran\n' > "$HOME/cron-stub-ran"
STUB
chmod +x "$HOME_SPECIAL/.local/bin/system-janitor"
SPECIAL_JOB="$(grep '^17 3 ' "$HOME_SPECIAL/.crontab.stub")"
SPECIAL_COMMAND="${SPECIAL_JOB#* * * * * }"
# Doctor resolves the final token from raw crontab, before cron's unescaping.
SPECIAL_COMMAND="$SPECIAL_COMMAND" HOME_SPECIAL="$HOME_SPECIAL" python3 - <<'PY'
import os
import shlex
assert shlex.split(os.environ["SPECIAL_COMMAND"])[-1] == os.environ["HOME_SPECIAL"] + "/.local/bin/system-janitor"
PY
# Mirror cron's escape-state scanner: even backslash parity before % splits
# commands into stdin. String replacement would conceal this real cron failure.
SPECIAL_COMMAND="$(SPECIAL_COMMAND="$SPECIAL_COMMAND" python3 - <<'PY'
import os
out = []
escaped = False
for ch in os.environ["SPECIAL_COMMAND"]:
    if ch == "\\" and not escaped:
        escaped = True
        continue
    if ch == "%" and not escaped:
        raise SystemExit("unescaped cron percent would split command into stdin")
    if escaped and ch != "%":
        out.append("\\")
    out.append(ch)
    escaped = False
if escaped:
    out.append("\\")
print("".join(out))
PY
)"
HOME="$HOME_SPECIAL" XDG_STATE_HOME="$HOME_SPECIAL/state" PATH=/usr/bin:/bin /bin/sh -c "$SPECIAL_COMMAND"
[ -f "$HOME_SPECIAL/cron-stub-ran" ] || fail 'cron command did not execute stub from spaced/percent HOME'
pass "quoted percent-bearing cron command executes after cron unescaping"

echo "── quiet ────────────────────────────────────────────────────"
HOME_QUIET="$(make_home home-quiet)"
run_install "$HOME_QUIET" --quiet > "$WORK_DIR/quiet.out"
grep -Fq 'manifest               : written -> ' "$WORK_DIR/quiet.out" || fail '--quiet did not print manifest write status'
if grep -Fq 'symlink system-janitor' "$WORK_DIR/quiet.out"; then
  fail '--quiet printed full summary instead of manifest-only status'
fi
assert_symlink_target "$HOME_QUIET/.local/bin/cert-watch" "$CERTWATCH_EXPECTED"
assert_symlink_target "$HOME_QUIET/.local/bin/log-shipper" "$LOG_SHIPPER_EXPECTED"
assert_symlink_target "$HOME_QUIET/.local/bin/system-janitor" "$JANITOR_EXPECTED"
assert_symlink_target "$HOME_QUIET/.local/bin/system-updater" "$UPDATER_EXPECTED"
assert_symlink_target "$HOME_QUIET/.local/bin/agent-toolkit-cron" "$(realpath "$CRON_RUN_SCRIPT")"
[ -f "$HOME_QUIET/.crontab.stub" ] || fail '--quiet run did not install cron entries'
pass "--quiet suppresses regular summary but prints manifest changes"

echo
echo "all install smoke checks passed"
