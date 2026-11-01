# Install — log-shipper

`log-shipper` ships from the same repo and installer as the other toolkit tools.

## Install

```bash
git clone https://github.com/agent-frontier/agent-toolkit.git ~/agent-toolkit
cd ~/agent-toolkit
./install.sh
```

The installer creates or repairs `~/.local/bin/log-shipper` and installs the managed quarter-hourly cron entry:

```cron
# log-shipper — quarter-hourly audit-trail export (managed by agent-toolkit install.sh)
7,22,37,52 * * * * "$HOME/.local/bin/agent-toolkit-cron" "$HOME/.local/bin/log-shipper"
```

## Optional config

```bash
mkdir -p ~/.config/log-shipper
cp ~/agent-toolkit/examples/log-shipper.config.example ~/.config/log-shipper/config
$EDITOR ~/.config/log-shipper/config
```

## Verify

```bash
log-shipper --version
log-shipper --doctor
log-shipper --report --json
```

State lives under `~/.local/state/log-shipper/` by default.
