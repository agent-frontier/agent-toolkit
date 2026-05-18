# Security Policy

## Reporting

If you believe you have found a security vulnerability in `agent-toolkit`,
report it privately:

- Open a private GitHub security advisory:
  <https://github.com/agent-frontier/agent-toolkit/security/advisories/new>
- Or email the maintainers as documented by the repository maintainers.

Do **not** open a public GitHub issue for security bugs. Public issues are for
non-sensitive defects, docs, and feature requests.

When reporting, include:

- the affected tool and version (`--version` or `--version --json`),
- the platform and Bash version,
- the command or flags used,
- whether the behavior occurs in `--dry-run` or destructive mode,
- and any relevant JSON/JSONL output, logs, or minimal reproduction steps.

We will review reports privately and coordinate disclosure after triage and a
fix plan.

## Scope

### In scope

- `system-janitor.sh`
- `system-updater.sh`
- `install.sh`
- `scripts/bootstrap.sh`
- `mcp-server/`

### Out of scope

- Vulnerabilities in third-party tools invoked by the toolkit (`docker`, `apt`,
  `go`, `find`, `nuget`, and similar external commands)
- Damage caused by user misconfiguration, including intentionally broad cleanup
  paths or maintenance windows
- Reports that depend on running the tools as root outside documented workflows

## Threat model

`agent-toolkit` assumes a **single-host trust boundary**. These tools are local
ops automation: they mutate the local filesystem, install cron entries, and in
`system-updater`'s case drive the local package manager.

Important consequences of that model:

- Anyone with write access to `~/.config/system-janitor/config` can broaden the
  cleanup scope beyond the safe defaults.
- Safety is enforced partly by configuration defaults and partly by runtime
  checks, not by sandboxing.
- The janitor safety-floor mechanism snapshots inode identity and directory
  presence before the run, then fails the run if a protected path disappears or
  changes inode. Size drift alone is informational.
- Destructive paths are opt-in and the tools emphasize preview modes.
  `system-janitor` uses `--dry-run` for non-destructive verification, and the
  MCP server's destructive tools default to dry-run / preview mode.

The project is designed to be inspectable and agent-readable, but it does not
attempt to defend against a malicious local operator who already controls the
host or the toolkit's config files.

## Supply chain

`scripts/bootstrap.sh` installs from `main` by default. That is convenient for
testing and early adoption, but production users should prefer an explicit tag:

```bash
bash bootstrap.sh --ref=v0.1.0
```

Or, for the hosted bootstrap path:

```bash
curl -fsSL https://raw.githubusercontent.com/agent-frontier/agent-toolkit/v0.1.0/scripts/bootstrap.sh | bash -s -- --ref=v0.1.0
```

Future SHA-pinning work is tracked in `docs/releasing.md`.

## Known not-vulnerabilities

The following behaviors are intentional and, by themselves, are not treated as
security bugs:

- Running `system-updater` as root for real package application. It needs root
  or `sudo`-equivalent privileges for package-manager mutation.
- `install.sh` refusing root or user-level install flows that assume an
  unprivileged account.
- MCP destructive tools defaulting to dry-run / preview mode rather than making
  changes immediately.
- `system-janitor` removing regenerable caches or configured opt-in paths when
  those paths were explicitly supplied by the operator.

If you are unsure whether something is in scope, report it privately anyway and
include why you believe it crosses the project's documented trust boundary.
