"""Per-agent modules and dispatch helpers for Pi and OpenCode."""

from __future__ import annotations

import json
import shutil
import subprocess

from ucode.config_io import ToolSpec
from ucode.databricks.auth import install_ai_tools, install_databricks_cli
from ucode.state import load_state, save_state
from ucode.telemetry import agent_version
from ucode.ui import (
    console,
    is_low_verbosity,
    print_err,
    print_note,
    print_section,
    print_success,
    print_warning,
    prompt_yes_no,
    spinner,
)

from . import opencode, pi

_MODULES = {"opencode": opencode, "pi": pi}
TOOL_SPECS: dict[str, ToolSpec] = {name: module.SPEC for name, module in _MODULES.items()}
TOOL_ALIASES = {"opencode": "opencode", "pi": "pi"}
AITOOLS_AGENT_TOKENS = {"opencode": "opencode"}


def install_ai_tools_for_agents(tools: list[str], state: dict) -> None:
    """Install Databricks AI Tools for supported coding agents."""
    if state.get("databricks_ai_tools_enabled", True) is False:
        return
    agents = [AITOOLS_AGENT_TOKENS[tool] for tool in tools if tool in AITOOLS_AGENT_TOKENS]
    install_ai_tools(agents, state.get("profile"))


def normalize_tool(tool: str) -> str:
    normalized = TOOL_ALIASES.get(tool.strip().lower())
    if not normalized:
        raise RuntimeError(f"Unsupported tool '{tool}'. Use one of: opencode, pi.")
    return normalized


def _update_installed_tool_binary(tool: str, version: str | None = None) -> bool:
    spec = TOOL_SPECS[tool]
    target = f"{spec['package']}@{version}" if version else spec["package"]
    if not shutil.which("npm"):
        print_warning(f"`npm` is not available to update {spec['display']}; continuing.")
        return False
    print_note(f"Updating {spec['display']}...")
    try:
        subprocess.run(["npm", "install", "-g", target], check=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print_warning(f"Could not update {spec['display']}; continuing.")
        return False
    print_success(f"{spec['display']} is up to date")
    agent_version.cache_clear()
    return bool(shutil.which(spec["binary"]))


def _minimum_version_error(tool: str) -> str | None:
    checker = getattr(_MODULES[tool], "minimum_version_error", None)
    return checker() if callable(checker) else None


def _required_update_message(tool: str) -> str | None:
    checker = getattr(_MODULES[tool], "required_update_message", None)
    return checker() if callable(checker) else None


def _confirm_update_installed_tool_binary(tool: str) -> bool:
    spec = TOOL_SPECS[tool]
    update = _MODULES[tool].is_update_available()
    if not update:
        return False
    current, latest = update
    return prompt_yes_no(f"(Optional) Update {spec['display']} from {current} to {latest}?")


def install_tool_binary(
    tool: str,
    *,
    strict: bool = True,
    update_existing: bool = False,
    prompt_optional_updates: bool = True,
) -> bool:
    spec = TOOL_SPECS[tool]
    binary = spec["binary"]
    if shutil.which(binary):
        if update_existing:
            required_update = _required_update_message(tool)
            if required_update:
                print_warning(required_update)
                if not _update_installed_tool_binary(tool):
                    raise RuntimeError(_minimum_version_error(tool) or required_update)
            elif prompt_optional_updates and _confirm_update_installed_tool_binary(tool):
                _update_installed_tool_binary(tool)
        version_error = _minimum_version_error(tool)
        if version_error:
            raise RuntimeError(version_error)
        return True
    if not shutil.which("npm"):
        message = f"`{binary}` is not installed and npm is not available to install it."
        if strict:
            raise RuntimeError(message)
        print_warning(message)
        return False
    print_section("Bootstrap")
    print_warning(f"`{binary}` was not found. Installing {spec['display']}...")
    try:
        subprocess.run(["npm", "install", "-g", spec["package"]], check=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        message = f"Failed to install {spec['display']} automatically."
        if strict:
            raise RuntimeError(message) from exc
        print_warning(f"{message} Continuing without it.")
        return False
    if not shutil.which(binary):
        message = f"{spec['display']} install completed, but `{binary}` is still not on PATH."
        if strict:
            raise RuntimeError(message)
        print_warning(f"{message} Continuing without it.")
        return False
    return True


def ensure_tool_binary_available(tool: str) -> None:
    spec = TOOL_SPECS[tool]
    if shutil.which(spec["binary"]):
        return
    raise RuntimeError(
        f"{spec['display']} is not installed (`{spec['binary']}` was not found on PATH). "
        f"Install it with `npm install -g {spec['package']}` or run "
        "`ucode configure` to try automatic installation."
    )


def ensure_bootstrap_dependencies(
    tool: str, *, update_existing: bool = False, prompt_optional_updates: bool = True
) -> None:
    install_databricks_cli()
    install_tool_binary(
        tool,
        strict=True,
        update_existing=update_existing,
        prompt_optional_updates=prompt_optional_updates,
    )


def default_model_for_tool(tool: str, state: dict) -> str | None:
    return _MODULES[tool].default_model(state)


def resolve_launch_model(
    tool: str, state: dict, explicit_model: str | None
) -> tuple[dict, str | None]:
    model = explicit_model or default_model_for_tool(tool, state)
    if not model:
        raise RuntimeError(
            f"No models available for {tool}. Run `ucode configure` to set up your workspace."
        )
    return state, model


def configure_tool(tool: str, state: dict, model: str | None = None) -> dict:
    if not model:
        raise RuntimeError(f"A {tool} model must be selected before configuration.")
    result = _MODULES[tool].write_tool_config(state, model)
    return result[0] if isinstance(result, tuple) else result


def launch(tool: str, state: dict, tool_args: list[str]) -> None:
    _MODULES[tool].launch(state, tool_args)


def check_gateway_endpoint(state: dict, tool: str) -> bool:
    """Return whether discovery found at least one model usable by ``tool``."""
    if tool == "opencode":
        return any(state.get("opencode_models", {}).values())
    if tool == "pi":
        return default_model_for_tool("pi", state) is not None
    return False


_TOOL_DISCOVERY_SOURCES = {
    "opencode": ("claude", "gemini", "oss"),
    "pi": ("claude", "codex", "gemini"),
}


def _availability_failure_detail(tool: str, state: dict) -> str:
    reasons = state.get("_discovery_reasons") or {}
    parts = [
        f"{source} discovery: {reasons[source]}"
        for source in _TOOL_DISCOVERY_SOURCES.get(tool, ())
        if reasons.get(source)
    ]
    return " (" + "; ".join(parts) + ")" if parts else ""


def _configure_one(tool: str, state: dict) -> dict:
    state, model = resolve_launch_model(tool, state, None)
    return configure_tool(tool, state, model)


def configure_single_tool(tool: str, state: dict) -> dict:
    with spinner(f"Checking {TOOL_SPECS[tool]['display']} availability..."):
        ok = check_gateway_endpoint(state, tool)
    if not ok:
        raise RuntimeError(
            f"{TOOL_SPECS[tool]['display']} is not available on this workspace."
            f"{_availability_failure_detail(tool, state)}"
        )
    state = _configure_one(tool, state)
    state["available_tools"] = list(set((state.get("available_tools") or []) + [tool]))
    save_state(state)
    install_ai_tools_for_agents([tool], state)
    return state


def configure_selected_tools(state: dict, tools: list[str]) -> dict:
    for tool in tools:
        state = _configure_one(tool, state)
    state["available_tools"] = sorted(set(state.get("available_tools") or []) | set(tools))
    save_state(state)
    install_ai_tools_for_agents(tools, state)
    return state


def configure_all_tools(state: dict) -> dict:
    available = []
    for tool in TOOL_SPECS:
        with spinner(f"Checking {TOOL_SPECS[tool]['display']} availability..."):
            ok = check_gateway_endpoint(state, tool)
        if ok:
            available.append(tool)
        else:
            print_err(f"{TOOL_SPECS[tool]['display']} is not available on this workspace")
    return configure_selected_tools(state, available)


def ensure_provider_state(tool: str) -> dict:
    """Validate that a workspace and the selected tool are configured."""
    state = load_state()
    if not state.get("workspace"):
        raise RuntimeError("No workspace configured. Run `ucode configure` first.")
    if tool not in (state.get("available_tools") or []):
        raise RuntimeError(
            f"{TOOL_SPECS[tool]['display']} is not available on this workspace. "
            "Run `ucode configure` to set up your agents."
        )
    return state


def validate_tool(tool: str) -> tuple[bool, str]:
    spec = TOOL_SPECS[tool]
    module = _MODULES[tool]
    cmd = module.validate_cmd(spec["binary"])
    env = None
    if hasattr(module, "validate_env"):
        try:
            env = module.validate_env(load_state())
        except RuntimeError:
            pass
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True, ""
        output = (result.stderr or result.stdout or "").strip()
        for line in output.splitlines():
            if "error" in line.lower() and ("message" in line.lower() or ":" in line):
                msg = line.strip()
                if "error_code" in msg:
                    try:
                        payload = json.loads(msg[msg.index("{") : msg.rindex("}") + 1])
                        return False, payload.get("message", msg)
                    except (json.JSONDecodeError, ValueError):
                        pass
                return False, msg
        return False, output.splitlines()[-1] if output else "unknown error"
    except OSError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, "timed out"


def validate_all_tools(state: dict) -> None:
    from rich.panel import Panel

    from ucode.agents.pi import PI_SETTINGS_BACKUP_PATH, PI_SETTINGS_PATH
    from ucode.config_io import restore_file

    low_verbosity = is_low_verbosity()
    console.print()
    if low_verbosity:
        console.print("[bold blue]Validating...[/bold blue]")
    else:
        console.print(
            Panel(
                "Testing each tool with a quick message...",
                title="Validating",
                style="bold blue",
                expand=False,
            )
        )
    results = []
    available_tools = list(state.get("available_tools") or [])
    for tool, spec in TOOL_SPECS.items():
        if tool not in available_tools:
            continue
        with spinner(f"Validating {spec['display']}..."):
            ok, err = validate_tool(tool)
        results.append((tool, ok))
        if ok:
            print_success(f"{spec['display']} is working")
        else:
            print_err(f"{spec['display']}: {err}")
            managed = bool(state.get("managed_configs", {}).get(tool))
            restore_file(spec["config_path"], spec["backup_path"], managed)
            if tool == "pi":
                restore_file(PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH, managed)
            available_tools.remove(tool)
    state["available_tools"] = available_tools
    save_state(state)
    success_tools = [tool for tool, ok in results if ok]
    if success_tools and not low_verbosity:
        lines = [
            f"[green]✓[/green] [bold]{TOOL_SPECS[tool]['display']}[/bold] — run with [cyan]ucode {tool}[/cyan]"
            for tool in success_tools
        ]
        console.print(Panel("\n".join(lines), title="Ready", style="green", expand=False))
