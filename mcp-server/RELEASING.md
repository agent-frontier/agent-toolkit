# Releasing agent-toolkit-mcp
This runbook covers the current manual PyPI publish flow for `mcp-server/`.
Manual build + upload with Twine is the supported path today; trusted
publishing is the recommended future improvement.

## Pre-flight
Before cutting a release:

1. Bump `version` in `pyproject.toml`.
2. Bump `src/agent_toolkit_mcp/__init__.py` so `__version__` matches.
3. Confirm `README.md` still reflects the current MCP tool list.
4. Run tests:

   ```bash
   cd mcp-server
   python3 -m pytest
   ```

5. Verify capabilities and reported version:

   ```bash
   cd mcp-server
   agent-toolkit-mcp --version
   # or, without an installed console script:
   python3 -m agent_toolkit_mcp --version
   ```

6. Make sure the repo `CHANGELOG.md` entry is ready.

## Build

Build both the wheel and source distribution from a clean tree:

```bash
cd mcp-server
rm -rf dist/ build/ *.egg-info/
python3 -m pip install --upgrade build twine
python3 -m build       # produces dist/agent_toolkit_mcp-X.Y.Z-py3-none-any.whl + .tar.gz
python3 -m twine check dist/*
```

If `twine check` fails, stop and fix packaging metadata before uploading.

## Test install in a clean venv

Smoke-test the built wheel in a fresh virtual environment:

```bash
python3 -m venv /tmp/atk-mcp-test
/tmp/atk-mcp-test/bin/pip install dist/agent_toolkit_mcp-X.Y.Z-py3-none-any.whl
/tmp/atk-mcp-test/bin/agent-toolkit-mcp --version
rm -rf /tmp/atk-mcp-test
```

Optional: point an MCP client at that install and confirm the janitor/updater
tool set still matches the README.

## TestPyPI (recommended first)
Publish to TestPyPI before the first real PyPI upload, or whenever packaging
changes need an end-to-end rehearsal.

```bash
python3 -m twine upload --repository testpypi dist/*
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ agent-toolkit-mcp==X.Y.Z
```

`--extra-index-url https://pypi.org/simple/` is usually required so TestPyPI
installs can still resolve dependencies such as `mcp`.

## Production PyPI

Once TestPyPI looks good, upload the same `dist/*` artifacts to PyPI:

```bash
python3 -m twine upload dist/*
```

Manual uploads require PyPI credentials or an API token configured for Twine.
Store credentials outside the repository.

## Trusted publishing (recommended longer-term)

Manual upload is fine to start, but GitHub Actions + PyPI trusted publishing is
recommended longer-term because it removes stored PyPI secrets and binds
publishing to tagged workflow runs.

Suggested setup:

1. In PyPI, create a trusted publisher for this repository.
2. Restrict it to the release workflow file and tag pattern.
3. Add a GitHub Actions workflow that builds `mcp-server/` and publishes on tag
   push.
4. Keep `python -m build` and `twine check` in the workflow.

Placeholder workflow stub:

```yaml
name: Publish mcp-server
on:
  push:
    tags:
      - 'mcp-v*'
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Build package
        run: |
          cd mcp-server
          python -m pip install --upgrade build twine
          python -m build
          python -m twine check dist/*
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          packages-dir: mcp-server/dist/
```

Treat this as a future enhancement; manual upload is still the active process.

## Post-release
After the upload succeeds:

1. Tag the commit (`v0.1.0` for repo-wide releases, `mcp-v0.1.0` for MCP-only
   releases).
2. Update `mcp-server/README.md` install guidance from editable install
   instructions to `pip install agent-toolkit-mcp` once the first public PyPI
   release exists.
3. Announce the release in the repo `CHANGELOG.md`.

## Versioning policy

`mcp-server` follows semantic versioning.

- Patch: bug fixes, packaging-only changes, docs improvements, or other
  backwards-compatible changes.
- Minor: backwards-compatible new MCP tools, options, or metadata.
- Major: breaking changes.

The MCP tool interface is a public contract. Renaming a tool, changing argument
names, or changing behavior incompatibly is a breaking change and requires a
major version bump.
