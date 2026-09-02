"""Pi coding agent: writes the isolated ~/.lucode/pi-home/.pi/agent/models.json.

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

Additional compat keys may be set manually in models.json; lucode preserves
them across token refreshes and config rewrites.

Model membership comes from workspace discovery or the user's existing
inventory. Per-model tuning (``contextWindow``, ``maxTokens``,
``thinkingLevelMap``, per-model ``compat``) is layered underneath from
:mod:`lucode.parameters`, because discovery returns only bare model ids and
those caps are gateway-verified findings that cannot be rediscovered. An
existing entry in models.json always wins over the packaged tuning, so hand
edits survive every rewrite.

The user-maintained ``databricks-mlflow`` provider is not rendered here; lucode
only refreshes its token and fills in a missing route (see
:func:`_ensure_mlflow_route`).

The bearer token is baked into the file and refreshed by a background thread
while the session runs (same pattern as OpenCode/Copilot).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading

from lucode.agents.models import layer_model_entries, pi_default_model, resolve_model_ids
from lucode.agents.updates import available_npm_package_update
from lucode.config import (
    AGENT_PACKAGE_INSTALL_TIMEOUT_SECONDS,
    APP_DIR,
    NPM_REGISTRY,
    TOKEN_REFRESH_INTERVAL_SECONDS,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    write_json_file,
)
from lucode.databricks.auth import get_databricks_token
from lucode.databricks.models import (
    ANTHROPIC_FAMILIES,
    build_mlflow_base_url,
    build_pi_base_urls,
    classify_model_family,
)
from lucode.parameters import pi_parameters
from lucode.state import mark_tool_managed, save_state
from lucode.telemetry import agent_version, lucode_version
from lucode.ui import BACKGROUND_THREAD_JOIN_TIMEOUT_SECONDS, print_warning

PI_lucode_HOME = APP_DIR / "pi-home"
PI_CONFIG_DIR = PI_lucode_HOME / ".pi" / "agent"
PI_CONFIG_PATH = PI_CONFIG_DIR / "models.json"
PI_SETTINGS_PATH = PI_CONFIG_DIR / "settings.json"
# pi-agent-modes resolves this path from HOME; build_runtime_env pins HOME to
# PI_lucode_HOME, so initialization must not derive it from the ambient shell.
PI_MODES_CONFIG_PATH = PI_lucode_HOME / ".pi" / "modes" / "config.yaml"
PI_BACKUP_PATH = APP_DIR / "pi-models.backup.json"
PI_SETTINGS_BACKUP_PATH = APP_DIR / "pi-settings.backup.json"
PI_MODES_CONFIG_BACKUP_PATH = APP_DIR / "pi-modes-config.backup.yaml"
PI_MODES_FORCE_BACKUP_DIR = APP_DIR / "modes-backups"
PI_AGENT_MODES_PACKAGE = "@neilurk12/pi-agent-modes"
PI_AGENT_MODES_VERSION = "0.4.2"
PI_AGENT_MODES_SOURCE = f"npm:{PI_AGENT_MODES_PACKAGE}@{PI_AGENT_MODES_VERSION}"
PI_AGENT_MODES_ROOT = PI_CONFIG_DIR / "npm" / "node_modules" / "@neilurk12" / "pi-agent-modes"

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

# Old provider names earlier lucode versions wrote; cleaned up on each write so
# users don't end up with stale entries pointing at routes that 400.
LEGACY_PROVIDER_NAMES = ("databricks-anthropic", "databricks-codex", "databricks-oss")


def is_update_available() -> tuple[str, str] | None:
    return available_npm_package_update(SPEC["package"])


def _tuned_models(
    provider: str,
    managed_ids: list[str] | None,
    discovered_ids: list[str],
    existing_provider: dict,
) -> list[dict] | None:
    """Build ``models`` entries, layering per-model tuning under existing config.

    Membership, in precedence order:

    1. ``managed_ids`` when the caller supplies an exact inventory,
    2. otherwise the user's existing ``models`` array, which stays authoritative
       exactly as before this tuning layer existed (an explicitly empty array
       still means "serve nothing"),
    3. otherwise ``discovered_ids``, bootstrapping a provider the user has never
       configured.

    Tuning for each id is then resolved most-authoritative-first: the user's own
    entry, then the packaged tuning in :mod:`lucode.parameters`, then nothing
    (a bare ``{"id": ...}``). Hand edits therefore always win, including over a
    caller-supplied rewrite.

    Returns None only when there is nothing to write, so the caller omits
    ``models`` rather than pinning an empty array Pi would read as "this
    provider serves nothing".
    """
    existing_models = existing_provider.get("models")
    existing_by_id: dict[str, dict] = {}
    if isinstance(existing_models, list):
        for entry in existing_models:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                existing_by_id[entry["id"]] = entry

    existing_ids = (
        [entry["id"] for entry in existing_models if isinstance(entry, dict) and entry.get("id")]
        if isinstance(existing_models, list)
        else None
    )
    model_ids = resolve_model_ids(managed_ids, existing_ids, discovered_ids)
    if not model_ids:
        return [] if isinstance(existing_models, list) and managed_ids is None else None

    entries = layer_model_entries(
        model_ids,
        existing_by_id,
        lambda model_id: pi_parameters(provider, model_id),
    )
    return [{"id": model_id, **entries[model_id]} for model_id in model_ids]


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
    """Return the isolated Pi models overlay and lucode-owned key paths.

    Discovery preserves user-maintained model arrays. An explicit managed
    policy replaces those arrays with the exact servable policy inventory.
    """
    providers: dict[str, dict[str, object]] = {}
    keys: list[list[str]] = [["model"]]
    # Pi expands header values that match an env var name. Our UA contains
    # `/` and a space so it can never collide — safe to pass as a literal.
    ua_headers = {"User-Agent": f"lucode/{lucode_version()} pi/{agent_version('pi')}"}

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
        models = _tuned_models(
            "databricks-claude",
            managed_provider_models["databricks-claude"] if managed_provider_models else None,
            claude_ids,
            existing_claude,
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
        models = _tuned_models(
            "databricks-openai",
            managed_provider_models["databricks-openai"] if managed_provider_models else None,
            codex_models,
            existing_openai,
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
        models = _tuned_models(
            "databricks-gemini",
            managed_provider_models["databricks-gemini"] if managed_provider_models else None,
            gemini_models,
            existing_gemini,
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


def _ensure_mlflow_route(config: dict, workspace: str) -> bool:
    """Fill in a missing ``databricks-mlflow`` route, without ever overwriting one.

    ``databricks-mlflow`` is user-maintained: it is absent from
    :data:`PROVIDER_NAMES`, so :func:`render_overlay` never rebuilds it and its
    entry survives untouched. That leaves seeded tuning unusable, because a
    provider with models but no ``baseUrl``/``api`` cannot serve them, and the
    route is workspace-specific so an installer cannot know it.

    Only absent keys are set. A user pointing this provider at their own route
    keeps it, which is why this is not folded into the rendered providers.
    """
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return False
    mlflow = providers.get("databricks-mlflow")
    if not isinstance(mlflow, dict) or not mlflow.get("models"):
        return False
    changed = False
    if not mlflow.get("baseUrl"):
        mlflow["baseUrl"] = build_mlflow_base_url(workspace)
        changed = True
    if not mlflow.get("api"):
        # MLflow speaks OpenAI chat-completions, not the Responses dialect.
        mlflow["api"] = "openai-completions"
        changed = True
    if "authHeader" not in mlflow:
        mlflow["authHeader"] = True
        changed = True
    return changed


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
    """Translate an exact Pi allowlist into native-provider inventories."""
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
    """Return explicit models in Pi's discovery-family shape when any are servable."""
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
    _ensure_mlflow_route(merged, state["workspace"])
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
    return pi_default_model(state)


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
    refresh_failing = False
    while not stop_event.wait(TOKEN_REFRESH_INTERVAL_SECONDS):
        try:
            _refresh_token_once(state, force_refresh=True, token_only=True)
            refresh_failing = False
        except RuntimeError as exc:
            if not refresh_failing:
                print_warning(f"Pi token refresh failed; will retry: {exc}")
                refresh_failing = True


def build_runtime_env(token: str | None = None) -> dict[str, str]:
    """Build Pi's isolated environment; package installation does not need a token."""
    env = os.environ.copy()
    if token is not None:
        env["OAUTH_TOKEN"] = token
    else:
        env.pop("OAUTH_TOKEN", None)
    # Pi gives PI_CODING_AGENT_DIR precedence over HOME when locating models.json,
    # so pin both to prevent an inherited agent directory from bypassing lucode's config.
    env["HOME"] = str(PI_lucode_HOME)
    env["PI_CODING_AGENT_DIR"] = str(PI_CONFIG_DIR)
    env["NPM_CONFIG_REGISTRY"] = NPM_REGISTRY
    return env


def install_agent_modes_extension() -> None:
    """Install the required modes package through Pi's supported package interface."""
    try:
        result = subprocess.run(
            [SPEC["binary"], "install", PI_AGENT_MODES_SOURCE],
            env=build_runtime_env(),
            capture_output=True,
            text=True,
            timeout=AGENT_PACKAGE_INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Failed to install {PI_AGENT_MODES_SOURCE}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Failed to install {PI_AGENT_MODES_SOURCE}{suffix}")


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
        refresher.join(timeout=BACKGROUND_THREAD_JOIN_TIMEOUT_SECONDS)

    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--print", "say hi in 5 words or less"]


def validate_env(state: dict) -> dict[str, str]:
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("No workspace configured.")
    return build_runtime_env(get_databricks_token(workspace, state.get("profile")))
