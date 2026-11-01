#!/usr/bin/env bash
# Published cron snippets must execute, not merely resemble generated jobs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
examples = {
    "install.md": ("system-janitor", "17 3 * * *"),
    "updater-install.md": ("system-updater", "23 4 * * *"),
    "cert-watch-install.md": ("cert-watch", "42 * * * *"),
    "log-shipper-install.md": ("log-shipper", "7,22,37,52 * * * *"),
}
with tempfile.TemporaryDirectory(prefix="cron-docs-") as temp:
    home = Path(temp) / "home spaces%"
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True)
    (bindir / "agent-toolkit-cron").symlink_to(root / "scripts/cron-run.sh")
    env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(home / "state"),
               PATH="/usr/bin:/bin", BASH_ENV="")
    for filename, (tool, schedule) in examples.items():
        # Use the real published snippet and installed helper; tools are probes.
        probe = bindir / tool
        probe.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/argv-' + tool + '"\nexit 0\n')
        probe.chmod(0o755)
        doc = (root / "docs" / filename).read_text()
        block = re.search(r"```cron\n(.*?)\n```", doc, re.S).group(1)
        jobs = [line for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        assert len(jobs) == 1, (filename, jobs)
        parts = jobs[0].split(maxsplit=5)
        assert " ".join(parts[:5]) == schedule, filename
        result = subprocess.run(["/bin/sh", "-c", parts[5]], env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, (filename, result.stderr)
        record = json.loads((home / "state/agent-toolkit/cron" / (tool + ".json")).read_text())
        assert record["exit_code"] == 0
        if tool == "system-updater":
            assert (home / ("argv-" + tool)).read_text() == "--dry-run\n"
        print("ok: published", filename, "cron snippet executes with expanded HOME")
    result = subprocess.run([str(bindir / "agent-toolkit-cron"), "--status", "--json"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "healthy"

# These exact stale assertions contradicted the frozen safety-floor asymmetry.
for filename, phrase in [("safety.md", "inode change, total-byte-size change"),
                         ("configuration.md", "inode+size must not change"),
                         ("audit-trail.md", "inode or total byte size changed")]:
    assert phrase not in (root / "docs" / filename).read_text(), filename
assert "dry-run-first behavior" not in (root / "docs/index.md").read_text()
assert "--dry-run` is passed" in (root / "docs/index.md").read_text()
for filename in [*examples, "cron-troubleshooting.md"]:
    assert filename in (root / "mkdocs.yml").read_text(), filename
print("all cron docs smoke checks passed")
PY
