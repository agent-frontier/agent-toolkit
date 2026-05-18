# Recording demos with asciinema

`asciinema` was not available in this environment, so the demo scripts are committed without `.cast` recordings.

## Install asciinema

```bash
pip install --user asciinema
```

Or use your distro package manager if it ships `asciinema`.

## Record each demo

```bash
asciinema rec --command "./demos/01-cleanup-flow.sh" demos/01-cleanup-flow.cast
asciinema rec --command "./demos/02-health-recovery.sh" demos/02-health-recovery.cast
asciinema rec --command "./demos/03-agent-discovery.sh" demos/03-agent-discovery.cast
```

## Upload

```bash
asciinema upload demos/01-cleanup-flow.cast
asciinema upload demos/02-health-recovery.cast
asciinema upload demos/03-agent-discovery.cast
```

Each upload prints a shareable URL you can paste into `demos/README.md` or the main `README.md`.
