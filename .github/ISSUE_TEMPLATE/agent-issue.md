---
name: Agent contract issue
about: Report a machine-mode mismatch, schema drift, or agent integration defect
labels: ["agent"]
---

## Tool and invocation

Which tool did you call, and with which flags?

```bash
system-janitor --report --json
```

## Runtime version

Paste the version payload for the tool you invoked.

```bash
system-janitor --version --json
```

Include the schema / capability version information from that output.

## Expected JSON shape

Describe the JSON shape, field, enum, or exit-code behavior you expected.

## Actual JSON shape

Describe what you received instead.

## Raw JSON

Copy-paste the raw JSON exactly as returned.

```json
{}
```

## Validation result

If you validated against a schema, include the schema file name and validator
output.

## Additional context

Anything else an agent maintainer should know?
