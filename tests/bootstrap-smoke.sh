#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOOTSTRAP_SCRIPT="$REPO_ROOT/scripts/bootstrap.sh"
WORK_DIR="$REPO_ROOT/tests/.bootstrap-smoke.$$"
trap 'rm -rf "$WORK_DIR"' EXIT
mkdir -p "$WORK_DIR"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "ok: $*"
}

echo "── syntax check ─────────────────────────────────────────────"
bash -n "$BOOTSTRAP_SCRIPT"
bash -n "$REPO_ROOT/tests/bootstrap-smoke.sh"
pass "bash -n scripts/bootstrap.sh tests/bootstrap-smoke.sh"

echo "── --help ──────────────────────────────────────────────────"
HELP_OUT="$WORK_DIR/help.out"
"$BOOTSTRAP_SCRIPT" --help >"$HELP_OUT"
grep -Fq 'Usage: bootstrap.sh' "$HELP_OUT" || fail '--help missing usage header'
grep -Fq -- '--ref=' "$HELP_OUT" || fail '--help missing --ref='
grep -Fq -- '--prefix=' "$HELP_OUT" || fail '--help missing --prefix='
pass "--help exits 0 and documents --ref= / --prefix="

echo "── root refusal ────────────────────────────────────────────"
ROOT_OUT="$WORK_DIR/root.out"
set +e
bash -c 'id() { echo 0; }; export -f id; "$1"' _ "$BOOTSTRAP_SCRIPT" >"$ROOT_OUT" 2>&1
ROOT_RC=$?
set -e
[ "$ROOT_RC" -ne 0 ] || fail 'root-refusal probe unexpectedly succeeded'
grep -Fq 'do not run as root; user-level install' "$ROOT_OUT" || fail 'root-refusal message missing'
pass "root refusal triggers before any install work"

echo "── optional network clone ─────────────────────────────────"
if [ -n "${BOOTSTRAP_SMOKE_NETWORK:-}" ]; then
  HOME_DIR="$WORK_DIR/home"
  mkdir -p "$HOME_DIR/bin"
  cat > "$HOME_DIR/bin/crontab" <<'STUB'
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
  chmod +x "$HOME_DIR/bin/crontab"
  HOME="$HOME_DIR" PATH="$HOME_DIR/bin:$PATH" "$BOOTSTRAP_SCRIPT" --prefix="$WORK_DIR/clone" --ref=main --dry-run --quiet >"$WORK_DIR/network.out"
  [ -d "$WORK_DIR/clone/.git" ] || fail 'network clone did not create git repo'
  grep -Fq '[plan]' "$WORK_DIR/network.out" || fail 'network run missing [plan] output'
  pass "optional network clone + dry-run install succeeded"
else
  echo "skip: set BOOTSTRAP_SMOKE_NETWORK=1 to exercise live clone/update"
fi

echo
echo "all bootstrap smoke checks passed"
