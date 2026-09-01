"""Persistent state for lucode (per-workspace, versioned)."""

from __future__ import annotations

import json

from lucode.config import APP_DIR, AUTH_REFRESH_INTERVAL_MS, file_lock, is_dry_run, write_json_file
from lucode.databricks.auth import build_auth_shell_command
from lucode.databricks.models import build_shared_base_urls

STATE_PATH = APP_DIR / "state.json"
STATE_VERSION = 3


def load_full_state() -> dict:
    """Load the entire state file. Returns empty structure if missing or wrong version."""
    if not STATE_PATH.exists():
        return {"state_version": STATE_VERSION, "current_workspace": None, "workspaces": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state_version": STATE_VERSION, "current_workspace": None, "workspaces": {}}
    if (
        not isinstance(data, dict)
        or data.get("state_version") != STATE_VERSION
        or not isinstance(data.get("workspaces"), dict)
    ):
        return {"state_version": STATE_VERSION, "current_workspace": None, "workspaces": {}}
    return data


def load_state() -> dict:
    """Load the current workspace's state as a flat dict."""
    full = load_full_state()
    workspace = full.get("current_workspace")
    if not workspace:
        return {}
    ws_state = full.get("workspaces", {}).get(workspace, {})
    ws_state["workspace"] = workspace
    return hydrate_state(ws_state)


def save_state(state: dict) -> None:
    """Save workspace state back into the per-workspace structure."""
    if is_dry_run():
        return
    with file_lock("state"):
        full = load_full_state()
        workspace = state.get("workspace") or full.get("current_workspace")
        if workspace:
            full["current_workspace"] = workspace
            full["workspaces"][workspace] = hydrate_state(state)
        write_json_file(STATE_PATH, full)


def set_current_workspace(workspace: str | None) -> None:
    """Set ``current_workspace`` without touching the per-workspace blocks."""
    if is_dry_run():
        return
    with file_lock("state"):
        full = load_full_state()
        if full.get("current_workspace") == workspace:
            return
        full["current_workspace"] = workspace
        write_json_file(STATE_PATH, full)


def hydrate_state(state: dict) -> dict:
    """Normalize a workspace state entry and add derived harness config.

    :param state: Raw workspace state entry from ``state.json``.
    :returns: Hydrated workspace state with stable ``managed_configs``,
        ``base_urls``, and per-agent ``agents`` entries.
    """
    if not isinstance(state, dict):
        return {}

    hydrated = dict(state)
    managed_configs = hydrated.get("managed_configs")
    if not isinstance(managed_configs, dict):
        managed_configs = {}
    normalized: dict[str, dict] = {}
    for tool, entry in managed_configs.items():
        if isinstance(entry, dict):
            keys = entry.get("keys") if isinstance(entry.get("keys"), list) else []
            normalized[tool] = {"keys": keys}
        elif entry:
            normalized[tool] = {"keys": []}
    hydrated["managed_configs"] = normalized

    workspace = hydrated.get("workspace")
    if workspace:
        hydrated["base_urls"] = build_shared_base_urls(workspace)
        hydrated["agents"] = build_agent_state(hydrated)
    else:
        hydrated["base_urls"] = {}
        hydrated["agents"] = {}

    return hydrated


def build_agent_state(state: dict) -> dict[str, dict]:
    """Build per-agent harness configuration for a workspace.

    The returned shape is intended for downstream tools that want to reuse
    lucode's configured gateway URLs and auth command without duplicating
    endpoint construction logic.

    :param state: Hydrated workspace state containing ``workspace``,
        ``base_urls``, and discovered model lists.
    :returns: Mapping from agent name to its reusable configuration.
    """
    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return {}

    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    base_urls_value = state.get("base_urls")
    base_urls = base_urls_value if isinstance(base_urls_value, dict) else {}
    use_pat = bool(state.get("use_pat"))
    auth_command = build_auth_shell_command(workspace, profile, use_pat=use_pat)
    # Import after state initialization because lucode.agents dispatch imports this module.
    from lucode.agents.models import pi_default_model

    pi_model = pi_default_model(state)
    config = {
        "model": pi_model,
        "base_urls": base_urls.get("pi") if isinstance(base_urls.get("pi"), dict) else {},
        "auth_command": auth_command,
        "auth_refresh_interval_ms": AUTH_REFRESH_INTERVAL_MS,
    }
    return {"pi": {key: value for key, value in config.items() if value is not None}}


def clear_state() -> None:
    """Remove the current workspace entry from state."""
    with file_lock("state"):
        full = load_full_state()
        workspace = full.get("current_workspace")
        if workspace:
            full.get("workspaces", {}).pop(workspace, None)
            full["current_workspace"] = None
        write_json_file(STATE_PATH, full)


def mark_tool_managed(state: dict, tool: str, managed_keys: list) -> dict:
    managed_configs = dict(state.get("managed_configs") or {})
    managed_configs[tool] = {"keys": list(managed_keys)}
    state["managed_configs"] = managed_configs
    state["last_tool"] = tool
    return state
