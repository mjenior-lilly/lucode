"""OpenCode agent: writes Databricks-backed providers into native ``opencode.json``.

Existing user model maps define membership and retain custom metadata.
Discovery bootstraps a missing map, while an explicit caller-supplied inventory replaces
membership exactly. Background refreshes update only existing credential fields.

Per-model tuning (``limit``, per-call ``options``, display ``name``) is layered
underneath from :mod:`lucode.parameters` for any model without an existing
entry, including models an explicit inventory introduces. Discovery returns bare
ids, so without that layer OpenCode runs with no context/output caps. A user's
existing entry always wins, and a gateway-verified per-model ``limit`` outranks
the family-substring fallback in :func:`model_token_limits`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from copy import deepcopy

from lucode.agents.models import layer_model_entries, opencode_default_model, resolve_model_ids
from lucode.agents.updates import available_npm_package_update
from lucode.config import (
    APP_DIR,
    TOKEN_REFRESH_INTERVAL_SECONDS,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    write_json_file,
)
from lucode.databricks.auth import get_databricks_token
from lucode.databricks.models import (
    build_opencode_base_urls,
    model_token_limits,
)
from lucode.parameters import opencode_parameters
from lucode.state import mark_tool_managed, save_state
from lucode.telemetry import agent_version, lucode_version
from lucode.ui import BACKGROUND_THREAD_JOIN_TIMEOUT_SECONDS, print_warning

OPENCODE_XDG_CONFIG_HOME = APP_DIR / "opencode-xdg"
OPENCODE_CONFIG_DIR = OPENCODE_XDG_CONFIG_HOME / "opencode"
OPENCODE_CONFIG_PATH = OPENCODE_CONFIG_DIR / "opencode.json"
OPENCODE_BACKUP_PATH = APP_DIR / "opencode-config.backup.json"

SPEC: ToolSpec = {
    "binary": "opencode",
    "package": "opencode-ai",
    "display": "OpenCode",
    "config_path": OPENCODE_CONFIG_PATH,
    "backup_path": OPENCODE_BACKUP_PATH,
}

PROVIDER_KEYS: list[list[str]] = [
    ["provider", "databricks-anthropic"],
    ["provider", "databricks-google"],
    ["provider", "databricks-oss"],
]


def is_update_available() -> tuple[str, str] | None:
    return available_npm_package_update(SPEC["package"])


def _resolve_model_selector(model: str, opencode_models: dict[str, list[str]]) -> str:
    """Return an OpenCode model selector in provider/model form when possible."""
    if model.startswith(("databricks-anthropic/", "databricks-google/", "databricks-oss/")):
        return model

    anthropic_models = opencode_models.get("anthropic") or []
    if model in anthropic_models:
        return f"databricks-anthropic/{model}"

    gemini_models = opencode_models.get("gemini") or []
    if model in gemini_models:
        return f"databricks-google/{model}"

    oss_models = opencode_models.get("oss") or []
    if model in oss_models:
        return f"databricks-oss/{model}"

    return model


def _oss_model_overlay(model: str, ua_header: dict[str, str], has_limit: bool = False) -> dict:
    """Per-model overlay for an OSS model entry.

    All OSS models carry the User-Agent header; models with known token limits
    also pin `limit` (context + output) so OpenCode clamps `max_tokens` to a
    value the gateway accepts. OpenCode's schema requires both fields together,
    so the limits table always supplies both.

    ``has_limit`` suppresses the table lookup when the entry already carries a
    limit from packaged tuning or a user edit. :func:`model_token_limits`
    matches by family substring, so it cannot tell releases apart and would
    otherwise clamp a gateway-verified per-model cap down to a family default.
    """
    overlay: dict = {"headers": ua_header}
    if has_limit:
        return overlay
    limits = model_token_limits(model)
    if limits is not None:
        overlay["limit"] = limits
    return overlay


def render_overlay(
    model: str,
    token: str,
    opencode_base_urls: dict[str, str],
    opencode_models: dict[str, list[str]],
    existing_config: dict | None = None,
    managed_provider_models: dict[str, list[str]] | None = None,
) -> tuple[dict, list[list[str]]]:
    """Return the lucode overlay and owned paths for ``opencode.json``.

    Managed membership wins; otherwise a valid native model map is preserved,
    with discovery used only when that map is absent or invalid.
    """
    auth_headers = {"Authorization": f"Bearer {token}"}
    # OpenCode hardcodes `User-Agent: opencode/<ver>` in session/llm.ts for
    # every provider, after the AI SDK's combineHeaders. The provider-level
    # `headers` are clobbered by that injection, but per-model `headers` are
    # merged AFTER and win — so the UA must live on each model entry.
    ua_header = {
        "User-Agent": f"lucode/{lucode_version()} opencode/{agent_version('opencode')}",
    }

    existing_providers = (existing_config or {}).get("provider")
    if not isinstance(existing_providers, dict):
        existing_providers = {}

    def selected_models(provider_id: str, family: str) -> dict[str, dict]:
        """Resolve this provider's model map: membership, then layered tuning.

        Membership is whatever the caller was told to serve (a managed
        inventory, the user's own map, or discovery). Tuning for each id is
        resolved most-authoritative-first: the user's existing entry, then the
        packaged tuning, then nothing. An explicit inventory therefore replaces
        *which* models are served without discarding *how* each is tuned.
        """
        discovered = opencode_models.get(family) or []
        existing_provider = existing_providers.get(provider_id)
        has_user_map = isinstance(existing_provider, dict) and isinstance(
            existing_provider.get("models"), dict
        )
        existing_map = existing_provider["models"] if has_user_map else {}

        managed = (
            managed_provider_models.get(family) or []
            if managed_provider_models is not None
            else None
        )
        selected = resolve_model_ids(
            managed,
            existing_map if has_user_map else None,
            discovered,
        )
        return layer_model_entries(
            selected,
            existing_map,
            lambda model_id: opencode_parameters(provider_id, model_id),
        )

    anthropic_models = selected_models("databricks-anthropic", "anthropic")
    gemini_models = selected_models("databricks-google", "gemini")
    oss_models = selected_models("databricks-oss", "oss")
    active_families = (
        set(managed_provider_models)
        if managed_provider_models is not None
        else {family for family, models in opencode_models.items() if models}
    )

    providers: dict = {}
    keys: list[list[str]] = [["model"]]
    if "anthropic" in active_families:
        # @ai-sdk/anthropic injects `eager_input_streaming: true` on tool defs;
        # the Databricks gateway's strict validator rejects it. opencode's
        # auto-disable in transform.ts skips models whose id contains "claude",
        # so we opt out per-model. The setting lives in per-call providerOptions,
        # which opencode reads from `models.<m>.options`, not provider `options`.
        anthropic_model_overlay = {
            "headers": ua_header,
            "options": {"toolStreaming": False},
        }
        providers["databricks-anthropic"] = {
            "npm": "@ai-sdk/anthropic",
            "options": {
                "baseURL": opencode_base_urls["anthropic"],
                "apiKey": token,
                "headers": auth_headers,
            },
            "models": {
                model_id: deep_merge_dict(entry, deepcopy(anthropic_model_overlay))
                for model_id, entry in anthropic_models.items()
            },
        }
        keys.append(["provider", "databricks-anthropic"])
    if "gemini" in active_families:
        providers["databricks-google"] = {
            "npm": "@ai-sdk/google",
            "options": {
                "baseURL": opencode_base_urls["gemini"],
                "apiKey": token,
                "headers": auth_headers,
            },
            "models": {
                model_id: deep_merge_dict(entry, {"headers": dict(ua_header)})
                for model_id, entry in gemini_models.items()
            },
        }
        keys.append(["provider", "databricks-google"])
    if "oss" in active_families:
        providers["databricks-oss"] = {
            "npm": "@ai-sdk/openai",
            "options": {
                "baseURL": opencode_base_urls["oss"],
                "apiKey": token,
                "headers": auth_headers,
            },
            "models": {
                model_id: deep_merge_dict(
                    entry,
                    _oss_model_overlay(
                        model_id, dict(ua_header), has_limit=bool(entry.get("limit"))
                    ),
                )
                for model_id, entry in oss_models.items()
            },
        }
        keys.append(["provider", "databricks-oss"])

    selector_models = managed_provider_models or opencode_models
    overlay: dict = {"model": _resolve_model_selector(model, selector_models)}
    if providers:
        overlay["provider"] = providers
    return overlay, keys


def write_tool_config(
    state: dict,
    model: str,
    token: str | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[dict, str]:
    backup_existing_file(OPENCODE_CONFIG_PATH, OPENCODE_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(
            state["workspace"], state.get("profile"), force_refresh=force_refresh
        )
    opencode_base_urls = state.get("base_urls", {}).get("opencode") or build_opencode_base_urls(
        state["workspace"]
    )
    existing = read_json_safe(OPENCODE_CONFIG_PATH)
    overlay, managed_keys = render_overlay(
        model,
        token,
        opencode_base_urls,
        state.get("opencode_models") or {},
        existing_config=existing,
        managed_provider_models=state.get("opencode_managed_models"),
    )
    providers = existing.get("provider")
    if isinstance(providers, dict):
        for stale in (
            "databricks-anthropic",
            "databricks-google",
            "databricks-openai",
            "databricks-oss",
        ):
            providers.pop(stale, None)
    merged = deep_merge_dict(existing, overlay)
    write_json_file(OPENCODE_CONFIG_PATH, merged)
    state = mark_tool_managed(state, "opencode", managed_keys)
    save_state(state)
    return state, token


def build_mcp_server_entry(argv: list[str]) -> dict:
    # A `local` MCP server runs a command over stdio; `command` is the full
    # argv. lucode registers the `lucode mcp-proxy ...` bridge here so OpenCode
    # never speaks HTTP+bearer directly — the proxy mints fresh tokens itself.
    return {
        "type": "local",
        "command": list(argv),
        "enabled": True,
    }


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    backup_existing_file(OPENCODE_CONFIG_PATH, OPENCODE_BACKUP_PATH)
    existing = read_json_safe(OPENCODE_CONFIG_PATH)
    mcp_servers = existing.get("mcp")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    removed = name in mcp_servers
    mcp_servers[name] = build_mcp_server_entry(argv)
    existing["mcp"] = mcp_servers
    write_json_file(OPENCODE_CONFIG_PATH, existing)
    return removed


def remove_mcp_server_config(name: str) -> bool:
    existing = read_json_safe(OPENCODE_CONFIG_PATH)
    mcp_servers = existing.get("mcp")
    if not isinstance(mcp_servers, dict) or name not in mcp_servers:
        return False
    mcp_servers.pop(name)
    existing["mcp"] = mcp_servers
    write_json_file(OPENCODE_CONFIG_PATH, existing)
    return True


def default_model(state: dict) -> str | None:
    return opencode_default_model(state)


def _update_provider_credentials(config: dict, token: str) -> bool:
    """Update existing lucode provider credential fields without creating structure."""
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return False
    changed = False
    for provider_id in ("databricks-anthropic", "databricks-google", "databricks-oss"):
        provider = providers.get(provider_id)
        if not isinstance(provider, dict):
            continue
        options = provider.get("options")
        if not isinstance(options, dict):
            continue
        if "apiKey" in options and options["apiKey"] != token:
            options["apiKey"] = token
            changed = True
        headers = options.get("headers")
        authorization = f"Bearer {token}"
        if (
            isinstance(headers, dict)
            and "Authorization" in headers
            and headers["Authorization"] != authorization
        ):
            headers["Authorization"] = authorization
            changed = True
    return changed


def _refresh_token_in_file(token: str) -> None:
    """Refresh credentials in place so user-owned model configuration is untouched."""
    existing = read_json_safe(OPENCODE_CONFIG_PATH)
    if _update_provider_credentials(existing, token):
        write_json_file(OPENCODE_CONFIG_PATH, existing)


def _refresh_token_once(
    state: dict, *, force_refresh: bool = False, token_only: bool = False
) -> str:
    model = default_model(state)
    if not model:
        raise RuntimeError("No OpenCode model is configured.")
    token = get_databricks_token(
        state["workspace"], state.get("profile"), force_refresh=force_refresh
    )
    if token_only:
        _refresh_token_in_file(token)
    else:
        write_tool_config(state, model, token=token)
    return token


def _refresh_forever(state: dict, stop_event: threading.Event) -> None:
    refresh_failing = False
    while not stop_event.wait(TOKEN_REFRESH_INTERVAL_SECONDS):
        try:
            _refresh_token_once(state, force_refresh=True, token_only=True)
            refresh_failing = False
        except RuntimeError as exc:
            if not refresh_failing:
                print_warning(f"OpenCode token refresh failed; will retry: {exc}")
                refresh_failing = True


def build_runtime_env(token: str, state: dict | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["OAUTH_TOKEN"] = token
    env["XDG_CONFIG_HOME"] = str(OPENCODE_XDG_CONFIG_HOME)
    return env


def launch(state: dict, tool_args: list[str]) -> None:
    """Launch OpenCode after a full write, then refresh credentials in place."""
    token = _refresh_token_once(state)
    env = build_runtime_env(token, state)

    stop_event = threading.Event()
    refresher = threading.Thread(
        target=_refresh_forever,
        args=(state, stop_event),
        daemon=True,
    )
    refresher.start()

    proc = subprocess.Popen([SPEC["binary"], *tool_args], env=env)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()
    finally:
        stop_event.set()
        refresher.join(timeout=BACKGROUND_THREAD_JOIN_TIMEOUT_SECONDS)

    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "run", "say hi in 5 words or less"]


def validate_env(state: dict) -> dict[str, str]:
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("No workspace configured.")
    return build_runtime_env(get_databricks_token(workspace, state.get("profile")), state)
