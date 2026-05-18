# Demos

These scripts create temporary demo workspaces under `demos/.workspaces/`, run the toolkit against isolated state, and clean up after themselves. They never touch your real state.

## 01-cleanup-flow.sh

- **Name:** [`01-cleanup-flow.sh`](01-cleanup-flow.sh)
- **Summary:** Seeds fake cleanup targets, then runs doctor, dry-run, real cleanup, and report views.
- **Approx. runtime:** ~4 seconds
- **How to run locally:** `./demos/01-cleanup-flow.sh`
- **Asciinema:**

Local cast: [`01-cleanup-flow.cast`](01-cleanup-flow.cast). Replay with:

```bash
asciinema play demos/01-cleanup-flow.cast
```

Upload + share URL: `asciinema upload demos/01-cleanup-flow.cast`

## 02-health-recovery.sh

- **Name:** [`02-health-recovery.sh`](02-health-recovery.sh)
- **Summary:** Shows how `--health-acknowledge` clears historical malformed JSONL lines until a new problem appears.
- **Approx. runtime:** ~2 seconds
- **How to run locally:** `./demos/02-health-recovery.sh`
- **Asciinema:**

Local cast: [`02-health-recovery.cast`](02-health-recovery.cast). Replay with:

```bash
asciinema play demos/02-health-recovery.cast
```

Upload + share URL: `asciinema upload demos/02-health-recovery.cast`

## 03-agent-discovery.sh

- **Name:** [`03-agent-discovery.sh`](03-agent-discovery.sh)
- **Summary:** Demonstrates manifest discovery, per-tool capabilities, shared toolkit metadata, schemas, and doctor-first probing.
- **Approx. runtime:** ~2 seconds
- **How to run locally:** `./demos/03-agent-discovery.sh`
- **Asciinema:**

Local cast: [`03-agent-discovery.cast`](03-agent-discovery.cast). Replay with:

```bash
asciinema play demos/03-agent-discovery.cast
```

Upload + share URL: `asciinema upload demos/03-agent-discovery.cast`

## Run all demos

```bash
./demos/01-cleanup-flow.sh
./demos/02-health-recovery.sh
./demos/03-agent-discovery.sh
```

Reproducibility guarantee: These scripts create temporary demo workspaces and clean up. They never touch your real state.
