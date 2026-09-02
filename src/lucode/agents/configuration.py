"""Agent configuration and availability orchestration."""

from __future__ import annotations

from lucode.state import load_state, save_state
from lucode.ui import spinner

from .install import install_ai_tools_for_agents
from .registry import (
    TOOL_DISCOVERY_SOURCES,
    TOOL_SPECS,
    configure_tool,
    default_model_for_tool,
    resolve_launch_model,
)


def check_gateway_endpoint(state: dict, tool: str) -> bool:
    """Return whether discovery found at least one model usable by ``tool``."""
    if tool == "opencode":
        return any(state.get("opencode_models", {}).values())
    if tool == "pi":
        return default_model_for_tool("pi", state) is not None
    return False


def _availability_failure_detail(tool: str, state: dict) -> str:
    reasons = state.get("_discovery_reasons") or {}
    parts = [
        f"{source} discovery: {reasons[source]}"
        for source in TOOL_DISCOVERY_SOURCES.get(tool, ())
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


def ensure_provider_state(tool: str, state: dict | None = None) -> dict:
    """Validate that a workspace and the selected tool are configured."""
    state = load_state() if state is None else state
    if not state.get("workspace"):
        raise RuntimeError("No workspace configured. Run `lucode configure` first.")
    if tool not in (state.get("available_tools") or []):
        raise RuntimeError(
            f"{TOOL_SPECS[tool]['display']} is not available on this workspace. "
            "Run `lucode configure` to set up your agents."
        )
    return state
