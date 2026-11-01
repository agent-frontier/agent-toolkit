# Install — cert-watch

`cert-watch` ships from the same repo and installer as `system-janitor` and
`system-updater`.

## Install

```bash
git clone https://github.com/agent-frontier/agent-toolkit.git ~/agent-toolkit
cd ~/agent-toolkit
./install.sh
```

The installer creates or repairs `~/.local/bin/cert-watch` and installs the
managed hourly cron entry:

```cron
# cert-watch — hourly TLS certificate inventory (managed by agent-toolkit install.sh)
42 * * * * "$HOME/.local/bin/agent-toolkit-cron" "$HOME/.local/bin/cert-watch"
```

## Optional config

Install a config only if you want non-empty target inventory:

```bash
mkdir -p ~/.config/cert-watch
cp ~/agent-toolkit/examples/cert-watch.config.example ~/.config/cert-watch/config
$EDITOR ~/.config/cert-watch/config
```

## Verify

```bash
cert-watch --version
cert-watch --doctor
cert-watch --report --json
```

State lives under `~/.local/state/cert-watch/` by default.
