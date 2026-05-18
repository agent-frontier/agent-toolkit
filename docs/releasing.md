# Releasing agent-toolkit
This runbook covers how to cut a tagged GitHub release for `agent-toolkit`.
The release path is intentionally simple: update both script versions, promote
`CHANGELOG.md`, push an annotated `vX.Y.Z` tag, and let
[`.github/workflows/release.yml`](https://github.com/agent-frontier/agent-toolkit/blob/main/.github/workflows/release.yml) build the
artifacts and publish the GitHub Release.

The workflow is tag-driven (`push.tags: ["v*"]`) and has two jobs:

- `smoke` reruns the repository's syntax checks, `shellcheck`, and all four
  smoke harnesses before anything ships.
- `release` verifies the tag matches both script `VERSION` constants, archives
  the repo, computes `SHA256SUMS`, extracts the matching changelog section, and
  creates the GitHub Release.

## Pre-release checklist

Before you cut a tag, confirm all of the following:

- Local smoke tests pass:
  - `bash -n system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh`
  - `shellcheck -S warning system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh`
  - `./tests/smoke.sh`
  - `./tests/updater-smoke.sh`
  - `./tests/bootstrap-smoke.sh`
  - `./tests/install-smoke.sh`
  - `./tests/capabilities-check.sh`
- `system-janitor.sh --version --json` and `system-updater.sh --version --json`
  still emit alphabetically sorted `capabilities[]` arrays.
- `CHANGELOG.md` is ready to ship:
  - move the current `## [Unreleased]` notes under a release heading
    `## [vX.Y.Z] - YYYY-MM-DD`
  - add a fresh empty `## [Unreleased]` section above the new release entry
  - keep entries focused on shipped behavior, especially agent-visible surface
    area (flags, JSON fields, schemas, capabilities, exit codes)
- `AGENTS.md` is still accurate for the shipped behavior.
- The release diff is clean of placeholder debt (`TODO`, `XXX`, `FIXME`).
- The working tree is committed and `git status --short` is clean.

## Cutting the release

1. **Update both version constants.**

   Search for `VERSION=` near the top of both scripts and set them to the same
   value:

   - `system-janitor.sh`
   - `system-updater.sh`

   Example target:

   ```bash
   readonly VERSION="0.1.0"
   ```

   The release workflow strips the leading `v` from the Git tag and fails if
   the tag version does not exactly match both script constants.

2. **Promote the changelog entry.**

   In `CHANGELOG.md`:

   - rename the current `## [Unreleased]` section to
     `## [vX.Y.Z] - YYYY-MM-DD`
   - insert a new empty `## [Unreleased]` heading above it
   - make sure the release notes you want on GitHub live inside that section

   The release workflow extracts everything between the matching release heading
   and the next `## ` heading. It accepts both `## [vX.Y.Z] - YYYY-MM-DD` and
   the legacy `## [X.Y.Z]` style, but new entries should use the `v`-prefixed
   heading.

3. **Run the release checklist locally one last time.**

   ```bash
   bash -n system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
   shellcheck -S warning system-janitor.sh system-updater.sh install.sh scripts/bootstrap.sh
   ./tests/smoke.sh
   ./tests/updater-smoke.sh
   ./tests/bootstrap-smoke.sh
   ./tests/install-smoke.sh
   ./tests/capabilities-check.sh
   ```

4. **Commit the release prep.**

   ```bash
   git add system-janitor.sh system-updater.sh CHANGELOG.md
   git commit -m "chore: release v0.1.0"
   ```

5. **Create and push the annotated tag.**

   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin main v0.1.0
   ```

   Push the branch and tag together so the release workflow sees the final
   commit already on `main`.

6. **Watch the GitHub Actions run.**

   The release workflow should:

   - rerun the smoke gate successfully
   - confirm the tag matches both `VERSION` constants
   - upload `agent-toolkit-X.Y.Z.tar.gz`
   - upload `SHA256SUMS`
   - publish a GitHub Release named `vX.Y.Z`
   - mark prereleases automatically when the tag contains `-` (for example
     `v0.2.0-rc1`)

7. **Verify the published release page.**

   Check that:

   - the release notes include the `## Verify the installer` block
   - the bootstrap SHA matches `scripts/bootstrap.sh` from the tagged commit
   - both artifacts are attached (`agent-toolkit-X.Y.Z.tar.gz`, `SHA256SUMS`)
   - the extracted changelog section is the expected one

8. **Post-release promotion.**

   Announce the release in the places tracked by `docs/promotion-roadmap.md`
   (awesome lists, Show HN, blog posts, changelog feeds, etc.). If that roadmap
   is not present yet, use the same destination list when it lands.

## Rollback
If a bad tag goes out, do not try to mutate the existing GitHub Release into a
new version. Remove the bad tag, cut a fix, and publish the next patch release.

### Delete a bad tag locally and remotely

```bash
git tag -d v0.1.0
git push origin :refs/tags/v0.1.0
```

Then delete the corresponding GitHub Release from the UI or with `gh release
 delete v0.1.0` if it already exists.

### Publish a hotfix release

Use the next patch version rather than reusing the bad version number. Example:
if `v0.1.0` is wrong, fix forward to `v0.1.1`.

1. Commit the fix on `main`.
2. Update both `VERSION` constants to the hotfix version if needed.
3. Add a `CHANGELOG.md` entry for the hotfix.
4. Repeat the normal release process with the new tag.

This keeps tags immutable and preserves a clean audit trail for downstream
agents and operators.

## PyPI publish (mcp-server)
If the repository also ships the `mcp-server` package, follow the Python release
instructions in `mcp-server/RELEASING.md` after the GitHub release is live.
Keep the GitHub tag and the package version aligned when they describe the same
shipped feature set.

## Versioning policy
`agent-toolkit` uses Semantic Versioning.

- Patch releases fix bugs and documentation without changing the contract.
- Minor releases add backwards-compatible features.
- Major releases are for breaking changes once the project is past `1.0.0`.
- While the project is still `< 1.0.0`, minor bumps may include breaking
  changes if the agent-facing contract changes.

For this repo, the agent-facing contract defines breakage. In particular:

- removing a capability string from `capabilities[]` is breaking
- renaming or removing documented JSON fields is breaking
- changing exit-code meaning is breaking
- adding capability strings or new JSON fields is additive and non-breaking

Treat `capabilities[]` as append-only. A capability removal requires a breaking
version bump and coordinated documentation updates.

## SHA verification flow

Each GitHub Release publishes two artifacts:

- `agent-toolkit-X.Y.Z.tar.gz`
- `SHA256SUMS`

The workflow also prepends a `## Verify the installer` block to the release
notes with the exact SHA256 for `scripts/bootstrap.sh` from the tagged commit.
That block supports the README's “inspect before piping” workflow:

1. download `scripts/bootstrap.sh` from the tag
2. verify it against the published SHA256 value
3. run it only after the checksum matches

`SHA256SUMS` extends the same trust path to the source tarball. Users can verify
both the installer and the archive before executing or unpacking anything, and
automation can consume one checksum file instead of scraping ad hoc hashes from
release notes.
