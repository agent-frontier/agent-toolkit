#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [--ref=<tag-or-branch>] [--prefix=<path>] [install.sh args...]

Clone or update agent-toolkit, then run install.sh with any remaining args.

Options:
  --ref=<ref>       Git ref to check out before installing (default: main)
  --prefix=<path>   Clone/update destination (default: ${AGENT_TOOLKIT_HOME:-$HOME/agent-toolkit})
  -h, --help        Show this help

Examples:
  bash bootstrap.sh
  bash bootstrap.sh --ref=v0.1.0 --dry-run
  bash bootstrap.sh --prefix=$HOME/src/agent-toolkit --quiet
EOF
}

log() {
  printf '%s\n' "$*"
}

die() {
  log "bootstrap.sh: $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

checkout_ref() {
  local repo_dir="$1"
  local ref="$2"

  if git -C "$repo_dir" checkout "$ref" >/dev/null 2>&1; then
    return 0
  fi

  git -C "$repo_dir" fetch --tags origin "$ref" >/dev/null 2>&1 || true
  git -C "$repo_dir" checkout "$ref" >/dev/null 2>&1 || die "failed to check out ref: $ref"
}

pull_ref_if_branch() {
  local repo_dir="$1"
  local ref="$2"

  if git -C "$repo_dir" show-ref --verify --quiet "refs/heads/$ref" \
    || git -C "$repo_dir" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
    git -C "$repo_dir" pull --ff-only origin "$ref" || die "failed to fast-forward ref: $ref"
  fi
}

REF="main"
PREFIX="${AGENT_TOOLKIT_HOME:-${HOME:-}/agent-toolkit}"
REPO_URL="https://github.com/agent-frontier/agent-toolkit.git"
INSTALL_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref=*)
      REF="${1#--ref=}"
      ;;
    --prefix=*)
      PREFIX="${1#--prefix=}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      INSTALL_ARGS+=("$1")
      ;;
  esac
  shift
done

[ -n "${HOME:-}" ] || die 'HOME must be set'
[ "$(id -u)" -eq 0 ] && die 'do not run as root; user-level install'
[ -n "$REF" ] || die '--ref requires a non-empty value'
[ -n "$PREFIX" ] || die '--prefix requires a non-empty value'

need_cmd bash
need_cmd git

case "$PREFIX" in
  /*) ;;
  *) PREFIX="$PWD/$PREFIX" ;;
esac

if [ -d "$PREFIX/.git" ] || git -C "$PREFIX" rev-parse --git-dir >/dev/null 2>&1; then
  ACTION="update"
elif [ -e "$PREFIX" ]; then
  die "destination exists and is not a git repo: $PREFIX"
else
  ACTION="clone"
fi

log "[plan] ${ACTION} ${REPO_URL} at ${PREFIX} (ref=${REF}), then exec ./install.sh ${INSTALL_ARGS[*]:-}"

if [ "$ACTION" = 'clone' ]; then
  mkdir -p "$(dirname "$PREFIX")" || die "failed to create parent dir for $PREFIX"
  git clone "$REPO_URL" "$PREFIX" || die 'git clone failed'
  git -C "$PREFIX" fetch --tags origin || die 'git fetch --tags failed after clone'
else
  git -C "$PREFIX" fetch --tags origin || die 'git fetch --tags failed'
fi

checkout_ref "$PREFIX" "$REF"
pull_ref_if_branch "$PREFIX" "$REF"

cd "$PREFIX" || die "failed to enter repo: $PREFIX"
[ -x ./install.sh ] || die "install.sh not found or not executable in $PREFIX"
exec ./install.sh "${INSTALL_ARGS[@]}"
