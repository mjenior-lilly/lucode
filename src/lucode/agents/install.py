"""Agent binary and Databricks AI Tools installation."""

from __future__ import annotations

import shutil
import subprocess

from lucode.config import AGENT_PACKAGE_INSTALL_TIMEOUT_SECONDS, NPM_REGISTRY
from lucode.databricks.auth import install_ai_tools, install_databricks_cli
from lucode.telemetry import agent_version
from lucode.ui import print_note, print_section, print_success, print_warning, prompt_yes_no

from .registry import _MODULES, TOOL_SPECS

AITOOLS_AGENT_TOKENS = {"opencode": "opencode"}


def install_ai_tools_for_agents(tools: list[str], state: dict) -> None:
    """Install Databricks AI Tools for supported coding agents."""
    if state.get("databricks_ai_tools_enabled", True) is False:
        return
    agents = [AITOOLS_AGENT_TOKENS[tool] for tool in tools if tool in AITOOLS_AGENT_TOKENS]
    install_ai_tools(agents, state.get("profile"))


def _update_installed_tool_binary(tool: str, version: str | None = None) -> bool:
    spec = TOOL_SPECS[tool]
    target = f"{spec['package']}@{version}" if version else spec["package"]
    if not shutil.which("npm"):
        print_warning(f"`npm` is not available to update {spec['display']}; continuing.")
        return False
    print_note(f"Updating {spec['display']}...")
    try:
        subprocess.run(
            ["npm", "install", "-g", target, f"--registry={NPM_REGISTRY}"],
            check=True,
            timeout=AGENT_PACKAGE_INSTALL_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print_warning(f"Could not update {spec['display']}; continuing.")
        return False
    print_success(f"{spec['display']} is up to date")
    agent_version.cache_clear()
    return bool(shutil.which(spec["binary"]))


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
        if (
            update_existing
            and prompt_optional_updates
            and _confirm_update_installed_tool_binary(tool)
        ):
            _update_installed_tool_binary(tool)
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
        subprocess.run(
            ["npm", "install", "-g", spec["package"], f"--registry={NPM_REGISTRY}"],
            check=True,
            timeout=AGENT_PACKAGE_INSTALL_TIMEOUT_SECONDS,
        )
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
        "`lucode configure` to try automatic installation."
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
