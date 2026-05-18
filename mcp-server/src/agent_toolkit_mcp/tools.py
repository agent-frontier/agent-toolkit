from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp.server.fastmcp import FastMCP

REPO_URL = "https://github.com/agent-frontier/agent-toolkit"


@dataclass(frozen=True)
class BinarySpec:
    key: str
    display_name: str
    env_var: str
    local_bin: str
    script_name: str
    manifest_name: str


@dataclass(frozen=True)
class ResolvedBinary:
    spec: BinarySpec
    path: Path | None
    source: str | None

    @property
    def display_name(self) -> str:
        return self.spec.display_name


JANITOR_SPEC = BinarySpec(
    key="janitor",
    display_name="system-janitor",
    env_var="AGENT_TOOLKIT_SYSTEM_JANITOR_BIN",
    local_bin="~/.local/bin/system-janitor",
    script_name="system-janitor.sh",
    manifest_name="system-janitor",
)
UPDATER_SPEC = BinarySpec(
    key="updater",
    display_name="system-updater",
    env_var="AGENT_TOOLKIT_SYSTEM_UPDATER_BIN",
    local_bin="~/.local/bin/system-updater",
    script_name="system-updater.sh",
    manifest_name="system-updater",
)
BINARY_SPECS: dict[str, BinarySpec] = {
    JANITOR_SPEC.key: JANITOR_SPEC,
    UPDATER_SPEC.key: UPDATER_SPEC,
}


class BinaryResolver:
    """Resolve toolkit binaries in the documented order."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self._cache: dict[str, ResolvedBinary] = {}

    def resolve_all(self) -> dict[str, ResolvedBinary]:
        return {key: self.resolve(key) for key in BINARY_SPECS}

    def resolve(self, key: str) -> ResolvedBinary:
        if key not in self._cache:
            spec = BINARY_SPECS[key]
            self._cache[key] = self._resolve_spec(spec)
        return self._cache[key]

    def _resolve_spec(self, spec: BinarySpec) -> ResolvedBinary:
        for candidate, source in self._candidate_paths(spec):
            if not candidate:
                continue
            expanded = candidate.expanduser()
            if expanded.is_file() and os.access(expanded, os.X_OK):
                return ResolvedBinary(spec=spec, path=expanded.resolve(), source=source)
        return ResolvedBinary(spec=spec, path=None, source=None)

    def _candidate_paths(self, spec: BinarySpec) -> list[tuple[Path | None, str]]:
        candidates: list[tuple[Path | None, str]] = []
        env_path = self.environ.get(spec.env_var)
        if env_path:
            candidates.append((Path(env_path), f"${spec.env_var}"))

        candidates.append((Path(spec.local_bin), spec.local_bin))

        toolkit_home = self.environ.get("AGENT_TOOLKIT_HOME")
        if toolkit_home:
            candidates.append((Path(toolkit_home) / spec.script_name, "$AGENT_TOOLKIT_HOME"))

        manifest_data = self._load_manifest()
        if manifest_data:
            manifest_path, manifest = manifest_data
            tool_path = _manifest_binary(manifest, spec.manifest_name)
            if tool_path:
                candidates.append((Path(tool_path), str(manifest_path)))

        which_path = shutil.which(spec.display_name, path=self.environ.get("PATH"))
        if which_path:
            candidates.append((Path(which_path), "PATH"))

        return candidates

    def _load_manifest(self) -> tuple[Path, dict[str, Any]] | None:
        manifest_path = _manifest_path(self.environ)
        if not manifest_path.is_file():
            return None
        try:
            return manifest_path, json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


class ToolkitTool:
    name: str
    description: str

    def register(self, server: FastMCP) -> None:
        server.add_tool(
            self.invoke,
            name=self.name,
            description=self.description,
            structured_output=True,
        )


@dataclass
class CommandTool(ToolkitTool):
    resolver: BinaryResolver
    binary_key: str
    name: str
    description: str

    @property
    def binary(self) -> ResolvedBinary:
        return self.resolver.resolve(self.binary_key)

    def _binary_missing(self) -> dict[str, Any]:
        return {
            "error": (
                f"{self.binary.display_name} binary not found; install via {REPO_URL}"
            ),
            "exit_code": None,
            "stderr": "",
        }

    def invoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


class JsonCommandTool(CommandTool):
    schema_relative_path: str | None = None

    def _run_json_command(self, args: list[str], *, accept_nonzero_json: bool = True) -> dict[str, Any]:
        resolved = self.binary
        if not resolved.path:
            return self._binary_missing()

        completed = subprocess.run(
            [str(resolved.path), *args],
            capture_output=True,
            text=True,
            env=dict(self.resolver.environ),
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr.strip()

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return {
                "error": f"failed to parse JSON output from {resolved.display_name}",
                "parse_error": True,
                "exit_code": completed.returncode,
                "stderr": stderr,
                "stdout": stdout,
                "detail": str(exc),
            }

        validation_error = _validate_schema(parsed, self.schema_relative_path, self.resolver.environ)
        if validation_error:
            return {
                "error": f"schema validation failed for {resolved.display_name}",
                "parse_error": True,
                "exit_code": completed.returncode,
                "stderr": stderr,
                "stdout": stdout,
                "detail": validation_error,
            }

        if completed.returncode != 0 and not accept_nonzero_json:
            return {
                "error": f"{resolved.display_name} exited with code {completed.returncode}",
                "exit_code": completed.returncode,
                "stderr": stderr,
                "stdout": stdout,
            }
        return parsed


class StatefulRunTool(CommandTool):
    state_env_var: str
    state_dir_name: str
    state_file_name: str

    def _state_path(self) -> Path:
        env_dir = self.resolver.environ.get(self.state_env_var)
        if env_dir:
            return Path(env_dir).expanduser() / self.state_file_name
        state_home = Path(
            self.resolver.environ.get(
                "XDG_STATE_HOME",
                str(_home_dir(self.resolver.environ) / ".local/state"),
            )
        ).expanduser()
        return state_home / self.state_dir_name / self.state_file_name

    def _run_and_load_state(self, args: list[str]) -> dict[str, Any]:
        resolved = self.binary
        if not resolved.path:
            return self._binary_missing()

        completed = subprocess.run(
            [str(resolved.path), *args],
            capture_output=True,
            text=True,
            env=dict(self.resolver.environ),
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr.strip()
        if completed.returncode != 0:
            return {
                "error": f"{resolved.display_name} exited with code {completed.returncode}",
                "exit_code": completed.returncode,
                "stderr": stderr,
                "stdout": stdout,
            }

        state_path = self._state_path()
        if not state_path.is_file():
            return {
                "error": f"expected state file was not found: {state_path}",
                "exit_code": completed.returncode,
                "stderr": stderr,
                "stdout": stdout,
            }

        raw_state = state_path.read_text(encoding="utf-8")
        try:
            return json.loads(raw_state)
        except json.JSONDecodeError as exc:
            return {
                "error": f"failed to parse state file {state_path.name}",
                "parse_error": True,
                "exit_code": completed.returncode,
                "stderr": stderr,
                "stdout": raw_state,
                "detail": str(exc),
            }


class JanitorReportTool(JsonCommandTool):
    schema_relative_path = "schemas/report.schema.json"

    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="janitor",
            name="janitor_report",
            description=(
                "Returns the audit-trail summary from system-janitor: total events, "
                "per-section freed bytes, idle streaks, and most-recent-run details. "
                "Read-only and safe to call freely; validates against schemas/report.schema.json."
            ),
        )

    def invoke(self) -> dict[str, Any]:
        return self._run_json_command(["--report", "--json"])


class JanitorHealthTool(JsonCommandTool):
    schema_relative_path = "schemas/health.schema.json"

    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="janitor",
            name="janitor_health",
            description=(
                "Returns the read-only janitor health probe over the audit trail: status, exit_code, "
                "and the frozen health checks. Safe to call freely; validates against schemas/health.schema.json."
            ),
        )

    def invoke(self) -> dict[str, Any]:
        return self._run_json_command(["--health", "--json"])


class JanitorDoctorTool(JsonCommandTool):
    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="janitor",
            name="janitor_doctor",
            description=(
                "Runs system-janitor --doctor --json to probe self-wiring, config resolution, and path setup. "
                "Read-only and safe to call freely; use it when an agent needs installation diagnostics."
            ),
        )

    def invoke(self) -> dict[str, Any]:
        return self._run_json_command(["--doctor", "--json"])


class JanitorVersionTool(JsonCommandTool):
    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="janitor",
            name="janitor_version",
            description=(
                "Returns system-janitor capability discovery JSON: name, version, capabilities, and the toolkit cross-reference. "
                "Read-only and safe to call freely."
            ),
        )

    def invoke(self) -> dict[str, Any]:
        return self._run_json_command(["--version", "--json"])


class JanitorRunTool(StatefulRunTool):
    state_env_var = "JANITOR_LOG_DIR"
    state_dir_name = "janitor"
    state_file_name = "last-run.json"

    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="janitor",
            name="janitor_run",
            description=(
                "WARNING: setting dry_run=false performs destructive cleanup through system-janitor. "
                "By default this runs in dry-run mode and returns the parsed last-run.json summary for the selected sections."
            ),
        )

    def invoke(self, dry_run: bool = True, only: str | None = None) -> dict[str, Any]:
        args: list[str] = []
        if dry_run:
            args.append("--dry-run")
        if only:
            args.append(f"--only={only}")
        return self._run_and_load_state(args)


class UpdaterReportTool(JsonCommandTool):
    schema_relative_path = "schemas/updater-report.schema.json"

    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="updater",
            name="updater_report",
            description=(
                "Returns the system-updater audit-trail summary: total runs, per-package status counts, idle streaks, and most-recent-run details. "
                "Read-only and safe to call freely; validates against schemas/updater-report.schema.json."
            ),
        )

    def invoke(self) -> dict[str, Any]:
        return self._run_json_command(["--report", "--json"])


class UpdaterHealthTool(JsonCommandTool):
    schema_relative_path = "schemas/updater-health.schema.json"

    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="updater",
            name="updater_health",
            description=(
                "Returns the read-only updater health probe over the audit trail and apt subsystem, including the frozen health checks. "
                "Safe to call freely; validates against schemas/updater-health.schema.json."
            ),
        )

    def invoke(self) -> dict[str, Any]:
        return self._run_json_command(["--health", "--json"])


class UpdaterVersionTool(JsonCommandTool):
    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="updater",
            name="updater_version",
            description=(
                "Returns system-updater capability discovery JSON: name, version, capabilities, and the toolkit cross-reference. "
                "Read-only and safe to call freely."
            ),
        )

    def invoke(self) -> dict[str, Any]:
        return self._run_json_command(["--version", "--json"])


class UpdaterRunTool(StatefulRunTool):
    state_env_var = "UPDATER_LOG_DIR"
    state_dir_name = "system-updater"
    state_file_name = "updater-last-run.json"

    def __init__(self, resolver: BinaryResolver) -> None:
        super().__init__(
            resolver=resolver,
            binary_key="updater",
            name="updater_run",
            description=(
                "WARNING: setting apply=true performs package updates through system-updater and usually requires root. "
                "Default preview mode leaves apply=false and returns the parsed updater-last-run.json summary."
            ),
        )

    def invoke(self, apply: bool = False, security_only: bool = False) -> dict[str, Any]:
        args: list[str] = ["--apply"] if apply else ["--dry-run"]
        if security_only:
            args.append("--security-only")
        return self._run_and_load_state(args)


def build_tool_definitions(resolver: BinaryResolver | None = None) -> list[ToolkitTool]:
    active_resolver = resolver or BinaryResolver()
    return [
        JanitorReportTool(active_resolver),
        JanitorHealthTool(active_resolver),
        JanitorDoctorTool(active_resolver),
        JanitorVersionTool(active_resolver),
        JanitorRunTool(active_resolver),
        UpdaterReportTool(active_resolver),
        UpdaterHealthTool(active_resolver),
        UpdaterVersionTool(active_resolver),
        UpdaterRunTool(active_resolver),
    ]


def _home_dir(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("HOME", str(Path.home())))


def _manifest_path(environ: Mapping[str, str]) -> Path:
    config_home = Path(environ.get("XDG_CONFIG_HOME", str(_home_dir(environ) / ".config"))).expanduser()
    return config_home / "agent-toolkit" / "manifest.json"


def _manifest_binary(manifest: dict[str, Any], tool_name: str) -> str | None:
    tools = manifest.get("tools")
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == tool_name and isinstance(tool.get("binary"), str):
            return tool["binary"]
    return None


def _find_toolkit_root(environ: Mapping[str, str]) -> Path | None:
    candidates: list[Path] = []
    toolkit_home = environ.get("AGENT_TOOLKIT_HOME")
    if toolkit_home:
        candidates.append(Path(toolkit_home).expanduser())

    for parent in Path(__file__).resolve().parents:
        candidates.append(parent)

    manifest_data = BinaryResolver(environ)._load_manifest()
    if manifest_data:
        install_root = manifest_data[1].get("install_root")
        if isinstance(install_root, str):
            candidates.append(Path(install_root).expanduser())

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "system-janitor.sh").is_file() and (candidate / "system-updater.sh").is_file():
            return candidate
    return None


def _validate_schema(payload: dict[str, Any], schema_relative_path: str | None, environ: Mapping[str, str]) -> str | None:
    if not schema_relative_path:
        return None

    root = _find_toolkit_root(environ)
    if root is None:
        return None

    schema_path = root / schema_relative_path
    if not schema_path.is_file():
        return None

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(payload)
        return None
    except ValidationError as exc:
        return exc.message
    except (OSError, json.JSONDecodeError):
        return None
