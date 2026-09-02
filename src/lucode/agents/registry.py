"""Agent registry, metadata, and direct dispatch for Pi and OpenCode."""

from __future__ import annotations

from lucode.config import ToolSpec

from . import opencode, pi

_MODULES = {"opencode": opencode, "pi": pi}
TOOL_SPECS: dict[str, ToolSpec] = {name: module.SPEC for name, module in _MODULES.items()}
TOOL_ALIASES = {"opencode": "opencode", "pi": "pi"}


def normalize_tool(tool: str) -> str:
    normalized = TOOL_ALIASES.get(tool.strip().lower())
    if not normalized:
        raise RuntimeError(f"Unsupported tool '{tool}'. Use one of: opencode, pi.")
    return normalized


def default_model_for_tool(tool: str, state: dict) -> str | None:
    return _MODULES[tool].default_model(state)


def resolve_launch_model(
    tool: str, state: dict, explicit_model: str | None
) -> tuple[dict, str | None]:
    model = explicit_model or default_model_for_tool(tool, state)
    if not model:
        raise RuntimeError(
            f"No models available for {tool}. Run `lucode configure` to set up your workspace."
        )
    return state, model


def configure_tool(tool: str, state: dict, model: str | None = None) -> dict:
    if not model:
        raise RuntimeError(f"A {tool} model must be selected before configuration.")
    result = _MODULES[tool].write_tool_config(state, model)
    return result[0] if isinstance(result, tuple) else result


def launch(tool: str, state: dict, tool_args: list[str]) -> None:
    _MODULES[tool].launch(state, tool_args)
