# Demos

These scripts create temporary demo workspaces under `demos/.workspaces/`, run the toolkit against isolated state, and clean up after themselves. They never touch your real state.

## 01-cleanup-flow.sh

- **Name:** [`01-cleanup-flow.sh`](01-cleanup-flow.sh)
- **Summary:** Seeds fake cleanup targets, then runs doctor, dry-run, real cleanup, and report views.
- **Approx. runtime:** ~4 seconds
- **How to run locally:** `./demos/01-cleanup-flow.sh`
- **Asciinema:**

```text
embed link here once recorded
```

## 02-health-recovery.sh

- **Name:** [`02-health-recovery.sh`](02-health-recovery.sh)
- **Summary:** Shows how `--health-acknowledge` clears historical malformed JSONL lines until a new problem appears.
- **Approx. runtime:** ~2 seconds
- **How to run locally:** `./demos/02-health-recovery.sh`
- **Asciinema:**

```text
embed link here once recorded
```

## 03-agent-discovery.sh

- **Name:** [`03-agent-discovery.sh`](03-agent-discovery.sh)
- **Summary:** Demonstrates manifest discovery, per-tool capabilities, shared toolkit metadata, schemas, and doctor-first probing.
- **Approx. runtime:** ~2 seconds
- **How to run locally:** `./demos/03-agent-discovery.sh`
- **Asciinema:**

```text
embed link here once recorded
```

## Run all demos

```bash
./demos/01-cleanup-flow.sh
./demos/02-health-recovery.sh
./demos/03-agent-discovery.sh
```

Reproducibility guarantee: These scripts create temporary demo workspaces and clean up. They never touch your real state.
