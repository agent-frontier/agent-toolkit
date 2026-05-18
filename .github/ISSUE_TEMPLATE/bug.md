---
name: Bug report
about: Report a defect in a toolkit script, installer, or machine-mode contract
labels: ["bug"]
---

## Summary

What broke?

## Tool and version

Which tool is affected?

- `system-janitor`
- `system-updater`
- `install.sh`
- `scripts/bootstrap.sh`
- `mcp-server`

Paste the version output:

```bash
system-janitor --version --json
```

## Environment

- OS / distro:
- Bash version (`bash --version`):
- Installation method:

## Smoke-test result

Paste the result of the closest relevant harness.

```bash
./tests/smoke.sh
```

If the issue is not janitor-specific, include the matching harness instead.

## Expected behavior

What did you expect to happen?

## Actual behavior

What happened instead?

## Reproduction

List the exact commands, config, and inputs needed to reproduce the problem.

1. 
2. 
3. 

## Audit trail

Paste any relevant `janitor.jsonl` / `updater.jsonl` events and/or a minimal
`last-run.json` snippet.

```json
{}
```

## Additional context

Anything else that would help triage?
