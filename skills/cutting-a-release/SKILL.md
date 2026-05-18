---
name: cutting-a-release
description: "Cut a new tagged release of agent-toolkit. Use when asked to release vX.Y.Z, prepare release notes, or publish to PyPI."
---
# Cutting a release

Use this skill when preparing a tagged toolkit release.
The source of truth is [`../../docs/releasing.md`](../../docs/releasing.md).
This file is the short runbook.

## Release principles

- Tags drive releases.
- Both Bash tools must report the same version.
- `capabilities[]` is append-only and sorted.
- Agent-visible contract changes belong in `CHANGELOG.md`.
- Removing a capability string is a breaking change.

## Pre-flight checklist

Run the full gate before tagging.

```bash
bash -n system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
shellcheck -S warning system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
./tests/smoke.sh
./tests/updater-smoke.sh
./tests/bootstrap-smoke.sh
./tests/install-smoke.sh
./tests/capabilities-check.sh
```

Then confirm release hygiene:

- `VERSION=` matches in both Bash tools
- `system-janitor.sh --version --json` is correct
- `system-updater.sh --version --json` is correct
- both `capabilities[]` arrays are alphabetically sorted
- `CHANGELOG.md` has promoted `Unreleased` into `## [vX.Y.Z] - YYYY-MM-DD`
- a fresh empty `## [Unreleased]` exists above it
- the working tree is clean

## Version-string consistency

The tag must match both script versions exactly after stripping the leading `v`.
Check the scripts, not just the changelog.

Example target:

```bash
readonly VERSION="0.1.0"
```

If versions drift, stop.
Do not tag until both tools agree.

## Tagging flow

Once the tree is ready:

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main vX.Y.Z
```

Push the branch and tag together.
That keeps the release workflow looking at the final commit already on `main`.

## What GitHub Actions does

The GitHub release workflow handles the rest.
It will:

- rerun the smoke gate
- verify the tag matches both `VERSION` constants
- build the source tarball
- compute `SHA256SUMS`
- extract the matching changelog section
- create the GitHub Release

If the tag contains `-`, GitHub marks the release as a prerelease.

## Post-release tasks

After the GitHub Release is live:

1. verify the release page
2. verify release notes came from the correct changelog section
3. verify the bootstrap SHA block is present
4. verify both artifacts are attached
5. publish the MCP package to PyPI manually
6. announce the release in the channels tracked by the roadmap

For the MCP package, use:

- [`../../mcp-server/RELEASING.md`](../../mcp-server/RELEASING.md)
- [`../../mcp-server/README.md`](../../mcp-server/README.md)

Current PyPI flow is manual.
It is not yet tag-automated.

## Manual PyPI follow-up

The MCP package is released separately from the GitHub tag flow.
The short version:

```bash
cd mcp-server
python3 -m pytest
python3 -m pip install --upgrade build twine
python3 -m build
python3 -m twine check dist/*
python3 -m twine upload dist/*
```

Before that upload, confirm:

- `pyproject.toml` version bumped
- `src/agent_toolkit_mcp/__init__.py` version bumped
- `mcp-server/README.md` still matches the MCP tool list

## Rollback

If the tag is bad, delete it locally and remotely.
Do not reuse the same version number.

```bash
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z
```

Then fix forward with the next patch release.
Example: `v0.1.0` was bad, so ship `v0.1.1`.

## Versioning policy

Use semantic versioning.

Interpretation for this repo:

- patch: bug fixes and docs-only fixes without contract changes
- minor: backwards-compatible features
- major: breaking changes after `1.0.0`
- pre-`1.0.0`: minor bumps may still be breaking

For agent-toolkit, these are breaking changes:

- removing a capability string
- renaming or removing documented JSON fields
- changing exit-code meaning

These are additive:

- adding capability strings
- adding JSON fields
- adding non-breaking docs and tests

## Minimal release checklist

If you only need the shortest possible path, do this in order:

1. update both `VERSION=` constants
2. promote `CHANGELOG.md` `Unreleased`
3. run the full smoke/lint/syntax gate
4. commit the release prep
5. create annotated tag
6. push branch plus tag
7. verify GitHub Release artifacts
8. publish `mcp-server` to PyPI if needed

## Cross-references

- [`../../docs/releasing.md`](../../docs/releasing.md)
- [`../../docs/promotion-roadmap.md`](../../docs/promotion-roadmap.md)
- [`../../mcp-server/RELEASING.md`](../../mcp-server/RELEASING.md)
- [`../../mcp-server/README.md`](../../mcp-server/README.md)
- [`../../CHANGELOG.md`](../../CHANGELOG.md)
