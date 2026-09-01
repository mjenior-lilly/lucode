"""Pi coding agent: writes the isolated ~/.ucode/pi-home/.pi/agent/models.json.

Pi (https://pi.dev) is a multi-provider coding agent. Workspace discovery can
render three native-API providers in its `models.json`:

- `databricks-claude`  (api: anthropic-messages)       → /ai-gateway/anthropic
- `databricks-openai`  (api: openai-responses)         → /ai-gateway/codex/v1
- `databricks-gemini`  (api: google-generative-ai)     → /ai-gateway/gemini/v1beta

The repository template also defines user-maintained OSS and foundation models
under `databricks-mlflow` (api: openai-completions) at /ai-gateway/mlflow/v1.

API-specific `compat` flags align Pi's requests with the gateway routes:

- Claude disables eager tool input streaming because the Anthropic translator
  rejects `tools[].eager_input_streaming`; Pi sends the accepted legacy beta
  header instead. Adaptive thinking is enabled only on models that require it.
- OpenAI enables strict function-tool schemas supported by the Responses route.
- MLflow uses `max_tokens`, a `system` prompt role, and omits unsupported or
  undocumented OpenAI Chat Completions fields.

Additional compat keys may be set manually in models.json; ucode preserves
them across token refreshes and config rewrites. Provider model lists are also
preserved as user-defined configuration; workspace discovery never adds model
entries. OSS and foundation entries require the correct per-model output caps.

The bearer token is baked into the file and refreshed by a background thread
while the session runs (same pattern as OpenCode/Copilot).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading

from ucode.agent_updates import available_npm_package_update
from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    write_json_file,
)
from ucode.databricks.auth import get_databricks_token
from ucode.databricks.models import (
    ANTHROPIC_FAMILIES,
    TOKEN_REFRESH_INTERVAL_SECONDS,
    build_pi_base_urls,
    classify_model_family,
)
from ucode.state import mark_tool_managed, save_state
from ucode.telemetry import agent_version, ucode_version

PI_UCODE_HOME = APP_DIR / "pi-home"
PI_CONFIG_DIR = PI_UCODE_HOME / ".pi" / "agent"
PI_CONFIG_PATH = PI_CONFIG_DIR / "models.json"
PI_SETTINGS_PATH = PI_CONFIG_DIR / "settings.json"
PI_BACKUP_PATH = APP_DIR / "pi-models.backup.json"
PI_SETTINGS_BACKUP_PATH = APP_DIR / "pi-settings.backup.json"

SPEC: ToolSpec = {
    "binary": "pi",
    "package": "@earendil-works/pi-coding-agent",
    "display": "Pi",
    "config_path": PI_CONFIG_PATH,
    "backup_path": PI_BACKUP_PATH,
}

PROVIDER_NAMES = (
    "databricks-claude",
    "databricks-openai",
    "databricks-gemini",
)

# databricks-mlflow is user-configured rather than rendered by this module, but
# its credential follows the same Databricks token lifecycle.
TOKEN_PROVIDER_NAMES = (*PROVIDER_NAMES, "databricks-mlflow")

PROVIDER_KEYS: list[list[str]] = [["providers", name] for name in PROVIDER_NAMES]

# Old provider names earlier ucode versions wrote; cleaned up on each write so
# users don't end up with stale entries pointing at routes that 400.
LEGACY_PROVIDER_NAMES = ("databricks-anthropic", "databricks-codex", "databricks-oss")


def is_update_available() -> tuple[str, str] | None:
    return available_npm_package_update(SPEC["package"])


def _resolve_model_selector(
    model: str,
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
) -> str:
    """Return a Pi model selector in `<provider>/<model>` form when possible."""
    for name in PROVIDER_NAMES:
        if model.startswith(f"{name}/"):
            return model
    if model in claude_models.values():
        return f"databricks-claude/{model}"
    if model in codex_models:
        return f"databricks-openai/{model}"
    if model in gemini_models:
        return f"databricks-gemini/{model}"
    return model


def render_overlay(
    model: str,
    token: str,
    pi_base_urls: dict[str, str],
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
    existing_config: dict | None = None,
    managed_provider_models: dict[str, list[str]] | None = None,
) -> tuple[dict, list[list[str]]]:
    """Return the isolated Pi models overlay and ucode-owned key paths.

    Discovery preserves user-maintained model arrays. An explicit managed
    policy replaces those arrays with the exact servable policy inventory.
    """
    providers: dict[str, dict[str, object]] = {}
    keys: list[list[str]] = [["model"]]
    # Pi expands header values that match an env var name. Our UA contains
    # `/` and a space so it can never collide — safe to pass as a literal.
    ua_headers = {"User-Agent": f"ucode/{ucode_version()} pi/{agent_version('pi')}"}

    existing_providers = (existing_config or {}).get("providers") or {}

    claude_ids = sorted(set(claude_models.values()))
    if claude_ids:
        existing_claude = existing_providers.get("databricks-claude", {})
        # Preserve user-added compat flags (e.g. supportsLongCacheRetention,
        # supportsStrictTools) while ensuring our required key is set.
        compat = {**existing_claude.get("compat", {}), "supportsEagerToolInputStreaming": False}
        providers["databricks-claude"] = {
            "baseUrl": pi_base_urls["claude"],
            "api": "anthropic-messages",
            "apiKey": token,
            "authHeader": True,
            # Gateway's Anthropic translator rejects per-tool
            # `eager_input_streaming` on the streaming + tools path. Pi sends
            # the legacy beta header instead when this is false.
            "compat": compat,
            "headers": ua_headers,
        }
        models = (
            [{"id": model_id} for model_id in managed_provider_models["databricks-claude"]]
            if managed_provider_models is not None
            else existing_claude.get("models")
        )
        if isinstance(models, list):
            providers["databricks-claude"]["models"] = models
        keys.append(["providers", "databricks-claude"])
    if codex_models:
        existing_openai = existing_providers.get("databricks-openai", {})
        compat = {**existing_openai.get("compat", {}), "supportsStrictMode": True}
        providers["databricks-openai"] = {
            "baseUrl": pi_base_urls["openai"],
            "api": "openai-responses",
            "apiKey": token,
            "authHeader": True,
            # The Responses route accepts strict JSON-schema function tools.
            "compat": compat,
            "headers": ua_headers,
        }
        models = (
            [{"id": model_id} for model_id in managed_provider_models["databricks-openai"]]
            if managed_provider_models is not None
            else existing_openai.get("models")
        )
        if isinstance(models, list):
            providers["databricks-openai"]["models"] = models
        keys.append(["providers", "databricks-openai"])
    if gemini_models:
        existing_gemini = existing_providers.get("databricks-gemini", {})
        providers["databricks-gemini"] = {
            "baseUrl": pi_base_urls["gemini"],
            "api": "google-generative-ai",
            "apiKey": token,
            "authHeader": True,
            "headers": ua_headers,
        }
        compat = existing_gemini.get("compat")
        if isinstance(compat, dict):
            providers["databricks-gemini"]["compat"] = dict(compat)
        models = (
            [{"id": model_id} for model_id in managed_provider_models["databricks-gemini"]]
            if managed_provider_models is not None
            else existing_gemini.get("models")
        )
        if isinstance(models, list):
            providers["databricks-gemini"]["models"] = models
        keys.append(["providers", "databricks-gemini"])
    overlay: dict = {
        "model": _resolve_model_selector(model, claude_models, codex_models, gemini_models),
    }
    if providers:
        overlay["providers"] = providers
    return overlay, keys


def _update_provider_api_keys(config: dict, token: str) -> bool:
    """Set existing apiKey fields for providers backed by the Databricks token."""
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return False
    changed = False
    for name in TOKEN_PROVIDER_NAMES:
        provider = providers.get(name)
        if isinstance(provider, dict) and "apiKey" in provider and provider["apiKey"] != token:
            provider["apiKey"] = token
            changed = True
    return changed


def _managed_provider_models(state: dict) -> dict[str, list[str]] | None:
    """Translate a managed Pi allowlist into exact native-provider inventories."""
    raw_models = state.get("pi_models")
    if not isinstance(raw_models, list) or not raw_models:
        return None
    providers = {name: [] for name in PROVIDER_NAMES}
    for model_id in raw_models:
        if not isinstance(model_id, str) or not model_id:
            continue
        family = classify_model_family(model_id)
        if family in ANTHROPIC_FAMILIES:
            providers["databricks-claude"].append(model_id)
        elif family == "codex":
            providers["databricks-openai"].append(model_id)
        elif family == "gemini":
            providers["databricks-gemini"].append(model_id)
    return providers if any(providers.values()) else None


def _managed_model_families(
    state: dict,
) -> tuple[dict[str, str], list[str], list[str]] | None:
    """Return managed models in Pi's discovery-family shape when any are servable."""
    providers = _managed_provider_models(state)
    if providers is None:
        return None
    claude: dict[str, str] = {}
    for model_id in providers["databricks-claude"]:
        family = classify_model_family(model_id)
        if family in ANTHROPIC_FAMILIES:
            claude.setdefault(family, model_id)
    return (
        claude,
        providers["databricks-openai"],
        providers["databricks-gemini"],
    )


def write_tool_config(
    state: dict,
    model: str,
    token: str | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[dict, str]:
    backup_existing_file(PI_CONFIG_PATH, PI_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(
            state["workspace"], state.get("profile"), force_refresh=force_refresh
        )
    pi_base_urls = state.get("base_urls", {}).get("pi") or build_pi_base_urls(state["workspace"])
    existing = read_json_safe(PI_CONFIG_PATH)
    managed_families = _managed_model_families(state)
    managed_provider_models = _managed_provider_models(state) if managed_families else None
    if managed_families is None:
        claude_models = state.get("claude_models") or {}
        codex_models = state.get("codex_models") or []
        gemini_models = state.get("gemini_models") or []
    else:
        claude_models, codex_models, gemini_models = managed_families
    overlay, managed_keys = render_overlay(
        model,
        token,
        pi_base_urls,
        claude_models,
        codex_models,
        gemini_models,
        existing_config=existing,
        managed_provider_models=managed_provider_models,
    )
    providers = existing.get("providers")
    if isinstance(providers, dict):
        for stale in (*LEGACY_PROVIDER_NAMES, *PROVIDER_NAMES):
            providers.pop(stale, None)
    merged = deep_merge_dict(existing, overlay)
    _update_provider_api_keys(merged, token)
    write_json_file(PI_CONFIG_PATH, merged)
    _write_settings(overlay["model"])
    state = mark_tool_managed(state, "pi", managed_keys)
    save_state(state)
    return state, token


def _write_settings(model_selector: str) -> None:
    # Pin defaultProvider/defaultModel in settings.json so Pi doesn't fall
    # through to an env-key-backed provider (e.g. HF_TOKEN exposing
    # huggingface) in `findInitialModel` when no --model is passed.
    provider, _, model_id = model_selector.partition("/")
    if not model_id:
        return
    backup_existing_file(PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH)
    existing = read_json_safe(PI_SETTINGS_PATH)
    merged = deep_merge_dict(existing, {"defaultProvider": provider, "defaultModel": model_id})
    write_json_file(PI_SETTINGS_PATH, merged)


def _refresh_token_in_file(token: str) -> None:
    """Update provider apiKey fields backed by the Databricks token, preserving other config."""
    existing = read_json_safe(PI_CONFIG_PATH)
    if _update_provider_api_keys(existing, token):
        write_json_file(PI_CONFIG_PATH, existing)


def default_model(state: dict) -> str | None:
    """Select Pi's default deterministically from the current resolved state."""
    managed_default = state.get("pi_default_model")
    if isinstance(managed_default, str) and managed_default:
        return managed_default

    managed_models = state.get("pi_models")
    if isinstance(managed_models, list):
        for model in managed_models:
            if (
                isinstance(model, str)
                and model
                and classify_model_family(model) in (*ANTHROPIC_FAMILIES, "codex", "gemini")
            ):
                return model

    claude_models = state.get("claude_models") or {}
    if isinstance(claude_models, dict):
        for family in ANTHROPIC_FAMILIES:
            model = claude_models.get(family)
            if isinstance(model, str) and model:
                return model

    for key in ("codex_models", "gemini_models"):
        models = state.get(key) or []
        if isinstance(models, list):
            for model in models:
                if isinstance(model, str) and model:
                    return model
    return None


def _refresh_token_once(
    state: dict, *, force_refresh: bool = False, token_only: bool = False
) -> str:
    model = default_model(state)
    if not model:
        raise RuntimeError("No Pi model is available on this workspace.")
    token = get_databricks_token(
        state["workspace"], state.get("profile"), force_refresh=force_refresh
    )
    if token_only:
        _refresh_token_in_file(token)
    else:
        write_tool_config(state, model, token=token)
    return token


def _refresh_forever(state: dict, stop_event: threading.Event) -> None:
    while not stop_event.wait(TOKEN_REFRESH_INTERVAL_SECONDS):
        try:
            _refresh_token_once(state, force_refresh=True, token_only=True)
        except RuntimeError:
            continue


def build_runtime_env(token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OAUTH_TOKEN"] = token
    env["HOME"] = str(PI_UCODE_HOME)
    return env


def launch(state: dict, tool_args: list[str]) -> None:
    token = _refresh_token_once(state)
    env = build_runtime_env(token)

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
        refresher.join(timeout=1)

    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--print", "say hi in 5 words or less"]


def validate_env(state: dict) -> dict[str, str]:
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("No workspace configured.")
    return build_runtime_env(get_databricks_token(workspace, state.get("profile")))
