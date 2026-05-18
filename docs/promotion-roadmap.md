# Promotion roadmap

> What this doc is: the visibility / discoverability / adoption track for
> agent-toolkit. Status of every item is explicit.
>
> What this doc is NOT: the tools catalog (see docs/agents/toolkit-roadmap.md)
> or a public commitment (CHANGELOG owns commitments).

## Status legend

- **shipped** — in main, has tests, agent-visible. Production-ready.
- **in-flight** — branch or unmerged work exists.
- **planned** — accepted, not started.
- **candidate** — under consideration.
- **rejected** — explicitly out of scope (with reason).

## Phase 1: Foundation (agent-visible from day one)

| Item | Status | Notes |
|------|--------|-------|
| `AGENTS.md` at repo root | shipped | Canonical agent design brief, moved from `.github/copilot-instructions.md`. |
| `--doctor` self-check flag | shipped | 4 checks, JSON mode, smoke probes. Cap: `doctor`, `doctor-json`. |
| `install.sh` idempotent installer | shipped | Symlinks + cron + host manifest. Smoke: `tests/install-smoke.sh`. |
| Host manifest at `~/.config/agent-toolkit/manifest.json` | shipped | Cross-tool reference; `toolkit` field in `--version --json`. Cap: `toolkit-manifest`. |
| OSS meta files (CONTRIBUTING, SECURITY, CoC, templates) | shipped | `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md`, `.github/ISSUE_TEMPLATE/*`, `.github/PULL_REQUEST_TEMPLATE.md`. |
| Agent skills bundle (`skills/`) | shipped | 5 SKILL.md playbooks: `using-agent-toolkit`, `extending-the-toolkit`, `incident-response`, `cutting-a-release`, `fleet-audit`. Indexed by `skills/README.md`. |

## Phase 2: Inbound visibility (first impression in 30 seconds)

| Item | Status | Notes |
|------|--------|-------|
| README hook / badges / sample output | shipped | Production-grade hook, badge row, "What it does" + "Sample output" sections with real `--doctor` / `--report --json` / `--version --json` captures. |
| `scripts/bootstrap.sh` curl\|bash one-liner | shipped | Pinned-ref support, root-refusal, idempotency. Smoke: `tests/bootstrap-smoke.sh`. |
| Comparison / positioning doc | shipped | `docs/comparison.md` — honest "not us" positioning vs cron+shell, Ansible/Salt/Puppet, distro updaters, logrotate, cross-OS tools, and building your own MCP server. |
| Asciinema demo cast | candidate | Needs a host with reproducible state. Documented as future work. |
| awesome-* list submissions (awesome-selfhosted, awesome-mcp-servers, awesome-cli-apps) | planned | Track submission status in this file once submitted. |
| Show HN / Lobsters / r/selfhosted post drafts | planned | Draft after v0.1.0 cut. |

## Phase 3: Depth (the second-look payoff)

| Item | Status | Notes |
|------|--------|-------|
| MCP server (`mcp-server/`) | shipped | 9 tools via stdio, Anthropic SDK. Single biggest agent-findability lever. |
| Cookbook / recipes doc | shipped | `docs/cookbook.md` — 6 recipes (Prometheus exporter, Ansible fleet playbook, Slack integrity notifier, jq forensics, manifest reader, Claude Desktop MCP integration). |
| `cert-watch.sh` (TLS expiry monitor) | shipped | Third shipped toolkit tool: read-only TLS expiry monitor with schemas, smoke coverage, and installer integration. |
| `log-shipper.sh` (audit-trail exporter) | shipped | Fourth shipped toolkit tool: read-only audit-trail exporter for Prometheus / NDJSON / OTLP. |

## Phase 4: Compounding (the long game)

| Item | Status | Notes |
|------|--------|-------|
| GitHub Actions release workflow | shipped | `.github/workflows/release.yml` — tag-push trigger, version-consistency check, tarball + SHA256SUMS, CHANGELOG-extracted release body, bootstrap SHA verification block. |
| `docs/releasing.md` runbook | shipped | 199-line release runbook covering pre-flight, cutting, rollback, PyPI publish cross-ref, versioning policy. |
| PyPI publish (mcp-server) | shipped | `mcp-server/pyproject.toml` fully populated (classifiers, keywords, project URLs); `mcp-server/RELEASING.md` runbook; package builds clean (wheel + sdist verified). |
| Docs site (GitHub Pages from `docs/`) | candidate | Defer until docs count justifies it. |
| Versioned tagged releases (v0.1.0+) | planned | `CHANGELOG.md` and README reference `v0.1.0`, but the repo currently has no git tags yet. Workflow is ready; just needs the tag. |

## What we deliberately are NOT doing

- **Cross-OS portability.** See toolkit-roadmap.md decision log. Linux-only by design.
- **A Go rewrite.** Same.
- **A daemon / always-on service.** Cron is the contract; agents read JSONL.
- **Centralized config server / management plane.** Each host owns its state.
- **Telemetry / phone-home.** Audit trail is local-only.

## Open questions for maintainers

- When do we cut v0.1.0? Probably after this round of promotion changes land.
- Which awesome lists should we target first? (Track in this file once decided.)
- Do we want a project landing page beyond the README? (Probably not yet.)
