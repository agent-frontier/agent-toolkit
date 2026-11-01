#!/usr/bin/env bash
set -uo pipefail

QUIET=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [--dry-run] [--quiet]

Create or repair ~/.local/bin symlinks for cert-watch, log-shipper,
system-janitor, system-updater, and the cron launcher, and install the
managed cron entries for all four tools.

Options:
  --dry-run   Preview changes without modifying symlinks or crontab
  --quiet     Suppress non-error output
  -h, --help  Show this help
EOF
}

log() {
  if [ "$QUIET" -eq 0 ]; then
    printf '%s\n' "$*"
  fi
}

warn() {
  printf 'install.sh: %s\n' "$*" >&2
}

die() {
  warn "$*"
  exit 1
}

resolve_path() {
  local path="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath "$path"
    return $?
  fi
  if command -v readlink >/dev/null 2>&1; then
    readlink -f "$path"
    return $?
  fi
  return 1
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --quiet) QUIET=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $arg"
      ;;
  esac
done

[ -n "${HOME:-}" ] || die 'HOME must be set'
[[ "$HOME" = /* && "$HOME" != *$'\n'* && "$HOME" != *$'\r'* ]] || die 'HOME must be an absolute, single-line path'
command -v python3 >/dev/null 2>&1 || die 'python3 command not found'

SCRIPT_SELF="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SELF")" && pwd)"
REPO_DIR="$(resolve_path "$SCRIPT_DIR")" || die 'failed to resolve repository path (need realpath or readlink -f)'
CERTWATCH_TARGET="$(resolve_path "$REPO_DIR/cert-watch.sh")" || die 'cert-watch.sh not found next to install.sh'
JANITOR_TARGET="$(resolve_path "$REPO_DIR/system-janitor.sh")" || die 'system-janitor.sh not found next to install.sh'
UPDATER_TARGET="$(resolve_path "$REPO_DIR/system-updater.sh")" || die 'system-updater.sh not found next to install.sh'
LOG_SHIPPER_TARGET="$(resolve_path "$REPO_DIR/log-shipper.sh")" || die 'log-shipper.sh not found next to install.sh'
CRON_RUN_TARGET="$(resolve_path "$REPO_DIR/scripts/cron-run.sh")" || die 'scripts/cron-run.sh not found next to install.sh'
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
case "$CONFIG_HOME" in
  /*) ;;
  *) CONFIG_HOME="$HOME/$CONFIG_HOME" ;;
esac
BIN_DIR="$HOME/.local/bin"
CERTWATCH_LINK="$BIN_DIR/cert-watch"
LOG_SHIPPER_LINK="$BIN_DIR/log-shipper"
JANITOR_LINK="$BIN_DIR/system-janitor"
UPDATER_LINK="$BIN_DIR/system-updater"
CRON_RUN_LINK="$BIN_DIR/agent-toolkit-cron"
MANIFEST_DIR="$CONFIG_HOME/agent-toolkit"
MANIFEST_PATH="$MANIFEST_DIR/manifest.json"
cron_quote() {
  local value="$1" out="'" char i
  for ((i = 0; i < ${#value}; i++)); do
    char="${value:i:1}"
    case "$char" in
      "'") out+="'\\''" ;;
      # Unquoted escaped percent between quoted segments is recognized by
      # both cron's parity scanner and native doctor's shlex path resolution.
      '%') out+="'\\%'" ;;
      *) out+="$char" ;;
    esac
  done
  printf '%s' "$out'"
}

CERTWATCH_COMMENT='# cert-watch — hourly TLS certificate inventory (managed by agent-toolkit install.sh)'
CERTWATCH_JOB="42 * * * * $(cron_quote "$CRON_RUN_LINK") $(cron_quote "$CERTWATCH_LINK")"
LOG_SHIPPER_COMMENT='# log-shipper — quarter-hourly audit-trail export (managed by agent-toolkit install.sh)'
LOG_SHIPPER_JOB="7,22,37,52 * * * * $(cron_quote "$CRON_RUN_LINK") $(cron_quote "$LOG_SHIPPER_LINK")"
JANITOR_COMMENT='# system-janitor — daily disk cleanup (managed by agent-toolkit install.sh)'
JANITOR_JOB="17 3 * * * $(cron_quote "$CRON_RUN_LINK") $(cron_quote "$JANITOR_LINK")"
UPDATER_COMMENT='# system-updater — daily apt update sweep (managed by agent-toolkit install.sh)'
UPDATER_JOB="23 4 * * * $(cron_quote "$CRON_RUN_LINK") $(cron_quote "$UPDATER_LINK")"

extract_version() {
  local script_path="$1"
  grep -E '(^VERSION=|^readonly[[:space:]]+VERSION=)' "$script_path" | head -1 | cut -d'"' -f2
}

TOOLKIT_VERSION="$(extract_version "$JANITOR_TARGET")"
[ -n "$TOOLKIT_VERSION" ] || die 'failed to extract VERSION from system-janitor.sh'
CERTWATCH_VERSION="$(extract_version "$CERTWATCH_TARGET")"
if [ -z "$CERTWATCH_VERSION" ]; then
  warn 'failed to extract VERSION from cert-watch.sh for consistency check'
elif [ "$CERTWATCH_VERSION" != "$TOOLKIT_VERSION" ]; then
  warn "VERSION mismatch: cert-watch=$CERTWATCH_VERSION system-janitor=$TOOLKIT_VERSION"
fi
UPDATER_VERSION="$(extract_version "$UPDATER_TARGET")"
if [ -z "$UPDATER_VERSION" ]; then
  warn 'failed to extract VERSION from system-updater.sh for consistency check'
elif [ "$UPDATER_VERSION" != "$TOOLKIT_VERSION" ]; then
  warn "VERSION mismatch: system-janitor=$TOOLKIT_VERSION system-updater=$UPDATER_VERSION"
fi
LOG_SHIPPER_VERSION="$(extract_version "$LOG_SHIPPER_TARGET")"
if [ -z "$LOG_SHIPPER_VERSION" ]; then
  warn 'failed to extract VERSION from log-shipper.sh for consistency check'
elif [ "$LOG_SHIPPER_VERSION" != "$TOOLKIT_VERSION" ]; then
  warn "VERSION mismatch: system-janitor=$TOOLKIT_VERSION log-shipper=$LOG_SHIPPER_VERSION"
fi

append_line() {
  local line="$1"
  if [ -n "${APPEND_BUFFER:-}" ]; then
    APPEND_BUFFER+=$'\n'
  fi
  APPEND_BUFFER+="$line"
}

ensure_bin_dir() {
  if [ -d "$BIN_DIR" ]; then
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi
  mkdir -p "$BIN_DIR" || die "failed to create $BIN_DIR"
}

build_manifest_content() {
  python3 - "$TOOLKIT_VERSION" "$REPO_DIR" "$CERTWATCH_LINK" "$CERTWATCH_TARGET" "$LOG_SHIPPER_LINK" "$LOG_SHIPPER_TARGET" "$JANITOR_LINK" "$JANITOR_TARGET" "$UPDATER_LINK" "$UPDATER_TARGET" "$CRON_RUN_LINK" "$CRON_RUN_TARGET" "$REPO_DIR/AGENTS.md" "$REPO_DIR/docs/agents/" <<'PY'
import datetime
import json
import sys

version, install_root, certwatch_bin, certwatch_src, log_shipper_bin, log_shipper_src, janitor_bin, janitor_src, updater_bin, updater_src, cron_bin, cron_src, agents_md, agents_dir = sys.argv[1:]
manifest = {
    "name": "agent-toolkit",
    "version": version,
    "install_root": install_root,
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tools": [
        {"name": "cert-watch", "binary": certwatch_bin, "source": certwatch_src},
        {"name": "log-shipper", "binary": log_shipper_bin, "source": log_shipper_src},
        {"name": "system-janitor", "binary": janitor_bin, "source": janitor_src},
        {"name": "system-updater", "binary": updater_bin, "source": updater_src},
    ],
    "supporting": {
        "cron_launcher": {"binary": cron_bin, "source": cron_src},
    },
    "docs": {
        "agents_md": agents_md,
        "agents_dir": agents_dir,
    },
    "repo_url": "https://github.com/agent-frontier/agent-toolkit",
}
sys.stdout.write(json.dumps(manifest, indent=2))
sys.stdout.write("\n")
PY
}

write_manifest() {
  local manifest_content compare_output compare_rc manifest_status tmp_manifest

  manifest_content="$(build_manifest_content)" || die 'failed to generate manifest JSON'
  manifest_status='written'

  if [ -f "$MANIFEST_PATH" ]; then
    compare_output="$(MANIFEST_CANDIDATE="$manifest_content" python3 - "$MANIFEST_PATH" <<'PY' 2>&1
import json
import os
import sys

SENTINEL = "__normalized_generated_at__"


def normalized_text(doc):
    if not isinstance(doc, dict):
        raise ValueError("manifest root must be a JSON object")
    out = dict(doc)
    out["generated_at"] = SENTINEL
    return json.dumps(out, indent=2) + "\n"

with open(sys.argv[1], encoding="utf-8") as fh:
    existing = json.load(fh)
candidate = json.loads(os.environ["MANIFEST_CANDIDATE"])
sys.exit(0 if normalized_text(existing) == normalized_text(candidate) else 1)
PY
)"
    compare_rc=$?
    case "$compare_rc" in
      0) manifest_status='already-current' ;;
      1) manifest_status='rewritten' ;;
      *)
        warn "existing manifest at $MANIFEST_PATH is invalid or unreadable; rewriting (${compare_output:-comparison failed})"
        manifest_status='rewritten'
        ;;
    esac
  fi

  MANIFEST_STATUS="$manifest_status"
  MANIFEST_SUMMARY="$(with_dry_prefix "$manifest_status")"

  if [ "$manifest_status" = 'already-current' ] || [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi

  mkdir -p "$MANIFEST_DIR" || die "failed to create $MANIFEST_DIR"
  tmp_manifest="${MANIFEST_PATH}.tmp.$$"
  if ! printf '%s' "$manifest_content" > "$tmp_manifest"; then
    rm -f "$tmp_manifest"
    die "failed to write manifest temp file $tmp_manifest"
  fi
  mv "$tmp_manifest" "$MANIFEST_PATH" || {
    rm -f "$tmp_manifest"
    die "failed to install manifest at $MANIFEST_PATH"
  }
}

ensure_symlink() {
  local link_path="$1"
  local target_path="$2"
  local result_var="$3"
  local status='already-present'
  local current_resolved=''

  if [ -L "$link_path" ]; then
    current_resolved="$(resolve_path "$link_path" 2>/dev/null || true)"
    if [ "$current_resolved" = "$target_path" ]; then
      status='already-present'
    else
      status='repaired'
      if [ "$DRY_RUN" -eq 0 ]; then
        ln -sfn "$target_path" "$link_path" || die "failed to repoint $link_path"
      fi
    fi
  elif [ -e "$link_path" ]; then
    die "$link_path exists and is not a symlink"
  else
    status='created'
    if [ "$DRY_RUN" -eq 0 ]; then
      ln -s "$target_path" "$link_path" || die "failed to create symlink $link_path"
    fi
  fi

  if [ "$DRY_RUN" -eq 1 ] && [ "$status" != 'already-present' ]; then
    printf -v "$result_var" 'would have %s' "$status"
  else
    printf -v "$result_var" '%s' "$status"
  fi
}

tool_matches_cron_line() {
  local line="$1"
  local tool="$2"

  case "$line" in
    ''|'#'*) return 1 ;;
  esac

  python3 - "$line" "$tool" <<'PY'
import os
import shlex
import sys

line, tool = sys.argv[1:]
expected = {"cert-watch": "cert-watch", "janitor": "system-janitor",
            "updater": "system-updater", "log-shipper": "log-shipper"}.get(tool)
try:
    tokens = shlex.split(line)
except ValueError:
    sys.exit(1)  # Never remove a job whose shell syntax we cannot recognize.
command = tokens[1:] if tokens and tokens[0].startswith("@") else tokens[5:]
if not command or not expected:
    sys.exit(1)
name = os.path.basename(command[0])
if name == "agent-toolkit-cron":
    name = os.path.basename(command[1]) if len(command) > 1 else ""
sys.exit(0 if name in (expected, expected + ".sh") else 1)
PY
}

load_current_crontab() {
  local output rc
  output="$(crontab -l 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    CURRENT_CRONTAB="$output"
    return 0
  fi
  case "$output" in
    *'no crontab for '*|*'no crontab for'*)
      CURRENT_CRONTAB=''
      return 0
      ;;
    *)
      die "failed to read current crontab: $output"
      ;;
  esac
}

analyze_crontab() {
  local current="$1"
  local line=''

  CERTWATCH_COMMENT_COUNT=0
  CERTWATCH_JOB_COUNT=0
  CERTWATCH_RELATED_COUNT=0
  CERTWATCH_STALE_COUNT=0
  JANITOR_COMMENT_COUNT=0
  JANITOR_JOB_COUNT=0
  JANITOR_RELATED_COUNT=0
  JANITOR_STALE_COUNT=0
  UPDATER_COMMENT_COUNT=0
  UPDATER_JOB_COUNT=0
  UPDATER_RELATED_COUNT=0
  UPDATER_STALE_COUNT=0
  LOG_SHIPPER_COMMENT_COUNT=0
  LOG_SHIPPER_JOB_COUNT=0
  LOG_SHIPPER_RELATED_COUNT=0
  LOG_SHIPPER_STALE_COUNT=0
  APPEND_BUFFER=''

  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$CERTWATCH_COMMENT" ]; then
      CERTWATCH_COMMENT_COUNT=$((CERTWATCH_COMMENT_COUNT + 1))
      continue
    fi
    if [ "$line" = "$JANITOR_COMMENT" ]; then
      JANITOR_COMMENT_COUNT=$((JANITOR_COMMENT_COUNT + 1))
      continue
    fi
    if [ "$line" = "$UPDATER_COMMENT" ]; then
      UPDATER_COMMENT_COUNT=$((UPDATER_COMMENT_COUNT + 1))
      continue
    fi
    if [ "$line" = "$LOG_SHIPPER_COMMENT" ]; then
      LOG_SHIPPER_COMMENT_COUNT=$((LOG_SHIPPER_COMMENT_COUNT + 1))
      continue
    fi
    # Drop stale comments left over from older installations (pre-rename
    # paths, hand-written comments). Recognized by referencing one of the
    # tool names in a `# …` line; counted as stale so we report 'repaired'.
    case "$line" in
      \#*cert-watch*)
        CERTWATCH_RELATED_COUNT=$((CERTWATCH_RELATED_COUNT + 1))
        CERTWATCH_STALE_COUNT=$((CERTWATCH_STALE_COUNT + 1))
        continue
        ;;
      \#*system-janitor*)
        JANITOR_RELATED_COUNT=$((JANITOR_RELATED_COUNT + 1))
        JANITOR_STALE_COUNT=$((JANITOR_STALE_COUNT + 1))
        continue
        ;;
      \#*system-updater*)
        UPDATER_RELATED_COUNT=$((UPDATER_RELATED_COUNT + 1))
        UPDATER_STALE_COUNT=$((UPDATER_STALE_COUNT + 1))
        continue
        ;;
      \#*log-shipper*)
        LOG_SHIPPER_RELATED_COUNT=$((LOG_SHIPPER_RELATED_COUNT + 1))
        LOG_SHIPPER_STALE_COUNT=$((LOG_SHIPPER_STALE_COUNT + 1))
        continue
        ;;
    esac
    if tool_matches_cron_line "$line" cert-watch; then
      CERTWATCH_RELATED_COUNT=$((CERTWATCH_RELATED_COUNT + 1))
      if [ "$line" = "$CERTWATCH_JOB" ]; then
        CERTWATCH_JOB_COUNT=$((CERTWATCH_JOB_COUNT + 1))
      else
        CERTWATCH_STALE_COUNT=$((CERTWATCH_STALE_COUNT + 1))
      fi
      continue
    fi
    if tool_matches_cron_line "$line" janitor; then
      JANITOR_RELATED_COUNT=$((JANITOR_RELATED_COUNT + 1))
      if [ "$line" = "$JANITOR_JOB" ]; then
        JANITOR_JOB_COUNT=$((JANITOR_JOB_COUNT + 1))
      else
        JANITOR_STALE_COUNT=$((JANITOR_STALE_COUNT + 1))
      fi
      continue
    fi
    if tool_matches_cron_line "$line" updater; then
      UPDATER_RELATED_COUNT=$((UPDATER_RELATED_COUNT + 1))
      if [ "$line" = "$UPDATER_JOB" ]; then
        UPDATER_JOB_COUNT=$((UPDATER_JOB_COUNT + 1))
      else
        UPDATER_STALE_COUNT=$((UPDATER_STALE_COUNT + 1))
      fi
      continue
    fi
    if tool_matches_cron_line "$line" log-shipper; then
      LOG_SHIPPER_RELATED_COUNT=$((LOG_SHIPPER_RELATED_COUNT + 1))
      if [ "$line" = "$LOG_SHIPPER_JOB" ]; then
        LOG_SHIPPER_JOB_COUNT=$((LOG_SHIPPER_JOB_COUNT + 1))
      else
        LOG_SHIPPER_STALE_COUNT=$((LOG_SHIPPER_STALE_COUNT + 1))
      fi
      continue
    fi
    append_line "$line"
  done <<< "$current"

  if [ "$CERTWATCH_COMMENT_COUNT" -eq 1 ] && [ "$CERTWATCH_JOB_COUNT" -eq 1 ] && [ "$CERTWATCH_RELATED_COUNT" -eq 1 ] && [ "$CERTWATCH_STALE_COUNT" -eq 0 ]; then
    CERTWATCH_CRON_STATUS='already-present'
  elif [ "$CERTWATCH_COMMENT_COUNT" -eq 0 ] && [ "$CERTWATCH_RELATED_COUNT" -eq 0 ]; then
    CERTWATCH_CRON_STATUS='created'
  else
    CERTWATCH_CRON_STATUS='repaired'
  fi

  if [ "$JANITOR_COMMENT_COUNT" -eq 1 ] && [ "$JANITOR_JOB_COUNT" -eq 1 ] && [ "$JANITOR_RELATED_COUNT" -eq 1 ] && [ "$JANITOR_STALE_COUNT" -eq 0 ]; then
    JANITOR_CRON_STATUS='already-present'
  elif [ "$JANITOR_COMMENT_COUNT" -eq 0 ] && [ "$JANITOR_RELATED_COUNT" -eq 0 ]; then
    JANITOR_CRON_STATUS='created'
  else
    JANITOR_CRON_STATUS='repaired'
  fi

  if [ "$UPDATER_COMMENT_COUNT" -eq 1 ] && [ "$UPDATER_JOB_COUNT" -eq 1 ] && [ "$UPDATER_RELATED_COUNT" -eq 1 ] && [ "$UPDATER_STALE_COUNT" -eq 0 ]; then
    UPDATER_CRON_STATUS='already-present'
  elif [ "$UPDATER_COMMENT_COUNT" -eq 0 ] && [ "$UPDATER_RELATED_COUNT" -eq 0 ]; then
    UPDATER_CRON_STATUS='created'
  else
    UPDATER_CRON_STATUS='repaired'
  fi

  if [ "$LOG_SHIPPER_COMMENT_COUNT" -eq 1 ] && [ "$LOG_SHIPPER_JOB_COUNT" -eq 1 ] && [ "$LOG_SHIPPER_RELATED_COUNT" -eq 1 ] && [ "$LOG_SHIPPER_STALE_COUNT" -eq 0 ]; then
    LOG_SHIPPER_CRON_STATUS='already-present'
  elif [ "$LOG_SHIPPER_COMMENT_COUNT" -eq 0 ] && [ "$LOG_SHIPPER_RELATED_COUNT" -eq 0 ]; then
    LOG_SHIPPER_CRON_STATUS='created'
  else
    LOG_SHIPPER_CRON_STATUS='repaired'
  fi
}

with_dry_prefix() {
  local status="$1"
  if [ "$DRY_RUN" -eq 1 ] && [ "$status" != 'already-present' ]; then
    printf 'would have %s' "$status"
  else
    printf '%s' "$status"
  fi
}

write_managed_crontab() {
  local rebuilt="$APPEND_BUFFER"
  if [ -n "$rebuilt" ]; then
    rebuilt+=$'\n\n'
  fi
  rebuilt+="$CERTWATCH_COMMENT"
  rebuilt+=$'\n'
  rebuilt+="$CERTWATCH_JOB"
  rebuilt+=$'\n\n'
  rebuilt+="$JANITOR_COMMENT"
  rebuilt+=$'\n'
  rebuilt+="$JANITOR_JOB"
  rebuilt+=$'\n\n'
  rebuilt+="$UPDATER_COMMENT"
  rebuilt+=$'\n'
  rebuilt+="$UPDATER_JOB"
  rebuilt+=$'\n\n'
  rebuilt+="$LOG_SHIPPER_COMMENT"
  rebuilt+=$'\n'
  rebuilt+="$LOG_SHIPPER_JOB"

  if [ "$DRY_RUN" -eq 0 ]; then
    printf '%s\n' "$rebuilt" | crontab - || die 'failed to install updated crontab'
  fi
}

command -v crontab >/dev/null 2>&1 || die 'crontab command not found'
load_current_crontab
analyze_crontab "$CURRENT_CRONTAB"

ensure_bin_dir
ensure_symlink "$CERTWATCH_LINK" "$CERTWATCH_TARGET" CERTWATCH_LINK_STATUS
ensure_symlink "$LOG_SHIPPER_LINK" "$LOG_SHIPPER_TARGET" LOG_SHIPPER_LINK_STATUS
ensure_symlink "$JANITOR_LINK" "$JANITOR_TARGET" JANITOR_LINK_STATUS
ensure_symlink "$UPDATER_LINK" "$UPDATER_TARGET" UPDATER_LINK_STATUS
ensure_symlink "$CRON_RUN_LINK" "$CRON_RUN_TARGET" CRON_RUN_LINK_STATUS

CERTWATCH_CRON_SUMMARY="$(with_dry_prefix "$CERTWATCH_CRON_STATUS")"
LOG_SHIPPER_CRON_SUMMARY="$(with_dry_prefix "$LOG_SHIPPER_CRON_STATUS")"
JANITOR_CRON_SUMMARY="$(with_dry_prefix "$JANITOR_CRON_STATUS")"
UPDATER_CRON_SUMMARY="$(with_dry_prefix "$UPDATER_CRON_STATUS")"

if [ "$CERTWATCH_CRON_STATUS" != 'already-present' ] || [ "$LOG_SHIPPER_CRON_STATUS" != 'already-present' ] || [ "$JANITOR_CRON_STATUS" != 'already-present' ] || [ "$UPDATER_CRON_STATUS" != 'already-present' ]; then
  write_managed_crontab
fi

write_manifest

log "agent-toolkit install summary:"
log "  symlink cert-watch     : $CERTWATCH_LINK_STATUS -> $CERTWATCH_TARGET"
log "  symlink log-shipper    : $LOG_SHIPPER_LINK_STATUS -> $LOG_SHIPPER_TARGET"
log "  symlink system-janitor : $JANITOR_LINK_STATUS -> $JANITOR_TARGET"
log "  symlink system-updater : $UPDATER_LINK_STATUS -> $UPDATER_TARGET"
log "  symlink cron launcher  : $CRON_RUN_LINK_STATUS -> $CRON_RUN_TARGET"
log "  cron cert-watch        : $CERTWATCH_CRON_SUMMARY"
log "  cron log-shipper       : $LOG_SHIPPER_CRON_SUMMARY"
log "  cron system-janitor    : $JANITOR_CRON_SUMMARY"
log "  cron system-updater    : $UPDATER_CRON_SUMMARY"
log "  job cert-watch         : $CERTWATCH_JOB"
log "  job log-shipper        : $LOG_SHIPPER_JOB"
log "  job system-janitor     : $JANITOR_JOB"
log "  job system-updater     : $UPDATER_JOB"
log "  manifest               : $MANIFEST_SUMMARY -> $MANIFEST_PATH"

if [ "$QUIET" -eq 1 ] && [ "$MANIFEST_STATUS" != 'already-current' ]; then
  printf '  manifest               : %s -> %s\n' "$MANIFEST_SUMMARY" "$MANIFEST_PATH"
fi
