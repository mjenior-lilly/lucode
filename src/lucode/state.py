"""Persistent state for lucode (per-workspace, versioned)."""

from __future__ import annotations

import json

from lucode.agent_models import pi_default_model
from lucode.config_io import APP_DIR, is_dry_run
from lucode.databricks.auth import build_auth_shell_command
from lucode.databricks.models import build_shared_base_urls

STATE_PATH = APP_DIR / "state.json"
STATE_VERSION = 3
# Transient key holding the developer's own values for whatever a managed config layered over them.
# Present only in memory: the layered values render the agent settings files, while `save_state`
# restores what's under it so `state.json` keeps recording the developer's own configuration.
MANAGED_OVERLAY_KEY = "_managed_overlay"
AUTH_REFRESH_INTERVAL_MS = 900_000


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
    """Save workspace state back into the per-workspace structure.

    Values a managed config layered over the developer's own are stripped first (see
    ``MANAGED_OVERLAY_KEY``), so an admin-published config takes effect through the generated agent
    settings files without overwriting what the developer configured for themselves.
    """
    if is_dry_run():
        return
    full = load_full_state()
    workspace = state.get("workspace") or full.get("current_workspace")
    if workspace:
        full["current_workspace"] = workspace
        full["workspaces"][workspace] = hydrate_state(_without_managed_overlay(state))
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(full, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write state file: {STATE_PATH}") from exc


def _without_managed_overlay(state: dict) -> dict:
    """Return ``state`` with managed-config values swapped back for the developer's own.

    Returns a new dict and leaves ``state`` untouched, so the caller keeps the layered values it
    needs for rendering and repeated saves stay idempotent.
    """
    overlay = state.get(MANAGED_OVERLAY_KEY)
    if not isinstance(overlay, dict):
        return state
    persisted = {key: value for key, value in state.items() if key != MANAGED_OVERLAY_KEY}
    for key, value in overlay.items():
        # A key the developer never set is dropped rather than persisted as None.
        if value is None:
            persisted.pop(key, None)
        else:
            persisted[key] = value
    return persisted


def set_current_workspace(workspace: str | None) -> None:
    """Set ``current_workspace`` without touching the per-workspace blocks."""
    if is_dry_run():
        return
    full = load_full_state()
    if full.get("current_workspace") == workspace:
        return
    full["current_workspace"] = workspace
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(full, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write state file: {STATE_PATH}") from exc


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
    full = load_full_state()
    workspace = full.get("current_workspace")
    if workspace:
        full.get("workspaces", {}).pop(workspace, None)
        full["current_workspace"] = None
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(full, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to clear state file: {STATE_PATH}") from exc


def mark_tool_managed(state: dict, tool: str, managed_keys: list) -> dict:
    managed_configs = dict(state.get("managed_configs") or {})
    managed_configs[tool] = {"keys": list(managed_keys)}
    state["managed_configs"] = managed_configs
    state["last_tool"] = tool
    return state
