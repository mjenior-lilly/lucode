"""Model classification, discovery, Gateway probing, and agent base URL builders."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlencode

from lucode.databricks.transport import debug, http_get_json, workspace_hostname

AI_GATEWAY_V2_DOCS_URL = "https://docs.databricks.com/aws/en/ai-gateway/overview-beta"
TOKEN_REFRESH_INTERVAL_SECONDS = 1800
# A model-service's `name` is `model-services/system.ai.<model-name>`; the
# part after the prefix is exactly the model string agents send (no
# `databricks-` infix — that only appears on the inner destination name).
_MODEL_SERVICE_NAME_PREFIX = "model-services/"
# The metastore-scope listing returns services from EVERY schema (e.g.
# `main.user.foo`, `temp.*`, internal DLT schemas). We only want the
# Databricks-managed foundation models under `system.ai`.
_MODEL_SERVICE_REQUIRED_PREFIX = "system.ai."

# Supported OSS chat families, matched by name substring. Add an entry to
# support a new family.
_OSS_MODEL_FAMILIES = ("kimi-", "glm-")

# Claude model families lucode buckets, newest tier first. Add an entry to
# support a new family in both discovery paths (`claude-<family>-*` via the
# model-services listing and `databricks-claude-<family>-*` via the AI Gateway).
# Fable is deliberately excluded: neither surviving harness supports it.
ANTHROPIC_FAMILIES = ("opus", "sonnet", "haiku")


def classify_model_family(model_id: str) -> str | None:
    """Bucket a model FQN into the family lucode keys its state by, or None if unrecognized.

    Mirrors how discovery buckets a model-services listing (see `discover_model_services`), so a
    model named in a managed config lands in the same bucket it would have from discovery. Returns
    one of ``ANTHROPIC_FAMILIES``, ``"codex"``, ``"gemini"``, or ``"oss"``. Matching is by name
    substring because neither the listing nor the config records a model's API dialect.
    """
    for family in ANTHROPIC_FAMILIES:
        if f"claude-{family}-" in model_id:
            return family
    if "gpt-" in model_id:
        return "codex"
    if "gemini-" in model_id:
        return "gemini"
    if any(oss in model_id for oss in _OSS_MODEL_FAMILIES):
        return "oss"
    return None


# Per-family token limits (context window + max output tokens). These are a
# property of the model + its `/ai-gateway/mlflow/v1` route (the gateway rejects
# requests whose output exceeds the cap), not of any one agent — so every agent
# that serves OSS models reads this single table and translates it into its own
# config dialect. Both fields are provided because agents like OpenCode require
# context and output together. Keyed by family substring; add an entry to bound
# a new model.
_MODEL_TOKEN_LIMITS: dict[str, dict[str, int]] = {
    # GLM-4.6: 200k context, but the gateway caps output well below the model's
    # native 128k — pin 25k so requests aren't rejected.
    "glm": {"context": 200_000, "output": 25_000},
}


def model_token_limits(model_id: str) -> dict[str, int] | None:
    """Return ``{"context": ..., "output": ...}`` limits for ``model_id``, or None.

    Matches by family substring (e.g. any ``*glm*`` id). None means the model
    has no known limits and the agent should not pin any."""
    for family, limits in _MODEL_TOKEN_LIMITS.items():
        if family in model_id:
            return dict(limits)
    return None


def _model_service_id(service: dict) -> str | None:
    """Extract the `system.ai.<model-name>` id from one model-service entry.

    Returns None for services in any other schema, so user/internal model
    services don't leak into the family buckets."""
    name = service.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if name.startswith(_MODEL_SERVICE_NAME_PREFIX):
        name = name[len(_MODEL_SERVICE_NAME_PREFIX) :]
    if not name.startswith(_MODEL_SERVICE_REQUIRED_PREFIX):
        return None
    return name or None


# The model-services metastore listing REQUIRES a bounded `page_size`:
# unparameterized or large-page requests (verified against
# eng-ml-agent-platform.staging 2026-06-14) return `HTTP 499` with an empty
# body, while pages of 10–100 come back reliably. A page can still 499
# intermittently under load, so each gets a few retries before we give up.
_MODEL_SERVICES_PAGE_SIZE = 100
_MODEL_SERVICES_PAGE_RETRIES = 4


def _get_model_services_page(
    url: str, token: str, *, retries: int = _MODEL_SERVICES_PAGE_RETRIES
) -> tuple[dict | list | None, str | None]:
    """GET one model-services page, retrying on failure.

    The endpoint intermittently 499/504s under load; a retry usually succeeds.
    Returns the same (payload, reason) shape as ``http_get_json`` — the last
    attempt's result when all retries are exhausted."""
    payload: dict | list | None = None
    reason: str | None = None
    for attempt in range(retries):
        payload, reason = http_get_json(url, token, timeout=30)
        if payload is not None:
            return payload, None
        debug("model-services page", f"attempt {attempt + 1}/{retries} failed: {reason}")
    return payload, reason


# Successful model-service listings for this process, keyed by workspace. The listing is a paginated
# walk of the whole metastore catalog, and several callers want different views of the same result,
# so a single `lucode setup` run would otherwise page it twice. Cached per process, not persisted: a
# long-lived process is not a thing here, and a new model appearing mid-command is not worth a
# second walk. Failures are never cached, so a transient error still retries.
_MODEL_SERVICES_CACHE: dict[str, list[str]] = {}


def clear_model_services_cache() -> None:
    """Forget cached model-service listings (used by tests, and after a workspace switch)."""
    _MODEL_SERVICES_CACHE.clear()


def list_model_services(
    workspace: str,
    token: str,
    *,
    page_size: int = _MODEL_SERVICES_PAGE_SIZE,
    max_pages: int = 100,
    use_cache: bool = True,
) -> tuple[list[str], str | None]:
    """List all `system.ai.*` model ids via the UC model-services API.

    Pages through ``/api/2.1/unity-catalog/model-services`` (metastore scope)
    with a bounded ``page_size`` (the endpoint 499s without one) and returns the
    de-duplicated, sorted list of ``system.ai.<model-name>`` ids. Returns
    (ids, reason); reason is None on a complete walk. A partial list is returned
    with a reason describing why pagination stopped and is never cached.

    A successful result is memoized per workspace for the life of the process; pass
    ``use_cache=False`` to force a fresh walk.
    """
    if use_cache:
        cached = _MODEL_SERVICES_CACHE.get(workspace)
        if cached is not None:
            return list(cached), None

    hostname = workspace_hostname(workspace)
    ids: list[str] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    last_reason: str | None = None
    for _ in range(max_pages):
        params: dict[str, str] = {"page_size": str(page_size)}
        if page_token:
            params["page_token"] = page_token
        url = f"https://{hostname}/api/2.1/unity-catalog/model-services?{urlencode(params)}"
        payload, reason = _get_model_services_page(url, token)
        if payload is None:
            last_reason = reason
            break
        data = cast(dict, payload) if isinstance(payload, dict) else {}
        for service in data.get("model_services", []):
            if isinstance(service, dict):
                model_id = _model_service_id(service)
                if model_id:
                    ids.append(model_id)
        page_token = data.get("next_page_token") or None
        if not page_token:
            last_reason = None
            break
        if page_token in seen_tokens:
            last_reason = "model-services pagination repeated a page token"
            break
        seen_tokens.add(page_token)
    else:
        if page_token:
            last_reason = f"model-services pagination exceeded {max_pages} pages"

    deduped = sorted(set(ids))
    if deduped:
        if use_cache and last_reason is None:
            _MODEL_SERVICES_CACHE[workspace] = list(deduped)
        return deduped, last_reason
    return [], last_reason or "model-services listing returned no models"


def discover_model_services(
    workspace: str, token: str
) -> tuple[dict[str, str], list[str], list[str], list[str], str | None]:
    """Discover models via UC model-services and bucket them by family name.

    Returns (claude_models, codex_models, gemini_models, oss_models, reason):

    - ``claude_models`` maps ``opus``/``sonnet``/``haiku`` to the
      newest matching ``system.ai.claude-*`` id (mirrors
      ``discover_claude_models``).
    - ``codex_models`` is the list of ``system.ai.*gpt-*`` ids.
    - ``gemini_models`` is the list of ``system.ai.*gemini-*`` ids, newest first.
    - ``oss_models`` is the list of OSS-model ``system.ai.*`` ids.

    ``reason`` is None after a complete walk; partial family buckets may be
    returned with a reason. Family bucketing is by name substring because the model-services API does not
    expose per-model API dialects.
    """
    ids, reason = list_model_services(workspace, token)
    if not ids:
        return {}, [], [], [], reason

    claude_models: dict[str, str] = {}
    for family in ANTHROPIC_FAMILIES:
        candidates = sorted(
            [m for m in ids if f"claude-{family}-" in m],
            reverse=True,
        )
        if candidates:
            claude_models[family] = candidates[0]

    codex_models = [m for m in ids if "gpt-" in m]
    gemini_models = sorted([m for m in ids if "gemini-" in m], key=model_version_sort_key)

    oss_models = [m for m in ids if any(family in m for family in _OSS_MODEL_FAMILIES)]

    if not (claude_models or codex_models or gemini_models or oss_models):
        sample = ", ".join(ids[:5])
        return (
            {},
            [],
            [],
            [],
            (
                "model-services returned model ids but none matched "
                f"claude/gpt/gemini/oss families (got: {sample})"
            ),
        )
    return claude_models, codex_models, gemini_models, oss_models, reason


def discover_claude_models(workspace: str, token: str) -> tuple[dict[str, str], str | None]:
    """Discover Claude families on this workspace's AI Gateway.

    Returns (models_by_family, reason). reason is None on success; otherwise it
    describes why the dict is empty (HTTP error, network error, or no models
    matching the expected naming convention).
    """
    hostname = workspace_hostname(workspace)
    payload, reason = http_get_json(f"https://{hostname}/ai-gateway/anthropic/v1/models", token)
    if payload is None:
        return {}, reason

    data = cast(dict, payload) if isinstance(payload, dict) else {}
    raw_ids = [
        m["id"]
        for m in data.get("data", [])
        if isinstance(m.get("id"), str) and not m["id"].endswith("-anthropic")
    ]

    result: dict[str, str] = {}
    for family in ANTHROPIC_FAMILIES:
        candidates = sorted(
            [m for m in raw_ids if f"databricks-claude-{family}-" in m],
            reverse=True,
        )
        if candidates:
            result[family] = candidates[0]
    if result:
        return result, None
    if not raw_ids:
        return {}, "AI Gateway returned no Claude model ids"
    sample = ", ".join(raw_ids[:5])
    families = ",".join(ANTHROPIC_FAMILIES)
    return {}, (
        "AI Gateway returned model ids but none matched "
        f"`databricks-claude-{{{families}}}-*` (got: {sample})"
    )


def model_version_sort_key(name: str) -> tuple:
    """Sort endpoint names so newer model versions come first.

    Endpoint names embed a dotted version as dash-separated digits, e.g.
    `databricks-gemini-3-5-flash` (3.5) or `databricks-gemini-3-flash` (3.0).
    Plain alphabetical sorting buries `3-5-flash` below `2-5-flash`; this key
    groups by the non-numeric prefix, orders by version descending, then falls
    back to the remaining text so ties stay stable and deterministic.
    """
    tokens = name.split("-")
    start = next((i for i, tok in enumerate(tokens) if tok.isdigit()), None)
    if start is None:
        # No version segment — sort these after versioned ones, alphabetically.
        # The leading 1 keeps the whole group below every versioned name (0).
        return (1, name, (), "")
    end = start
    while end < len(tokens) and tokens[end].isdigit():
        end += 1
    version = tuple(int(tok) for tok in tokens[start:end])
    # Pad to a fixed width so (3,) compares as (3, 0) — i.e. 3.0 < 3.5.
    padded = (version + (0, 0, 0))[:3]
    prefix = "-".join(tokens[:start])
    suffix = "-".join(tokens[end:])
    # Negate version components for descending order within a prefix group.
    return (0, prefix, tuple(-v for v in padded), suffix)


def discover_endpoints_with_api_type(
    workspace: str,
    token: str,
    api_type: str,
    *,
    sort_key=None,
) -> tuple[list[str], str | None]:
    """List endpoint names whose served_entities expose api_type with v2 support.

    Returns (endpoints, reason). reason is None on success; otherwise it
    describes why the list is empty. `sort_key` overrides the default
    alphabetical ordering of the returned names.
    """
    hostname = workspace_hostname(workspace)
    payload, reason = http_get_json(
        f"https://{hostname}/api/2.0/serving-endpoints:foundation-models", token
    )
    if payload is None:
        return [], reason

    data = cast(dict, payload) if isinstance(payload, dict) else {}
    endpoints = data.get("endpoints", [])
    out: list[str] = []
    saw_endpoint_without_v2 = False
    for ep in endpoints:
        name = ep.get("name", "")
        entities = ep.get("config", {}).get("served_entities", [])
        api_types: set[str] = set()
        any_v2 = False
        for se in entities:
            fm = se.get("foundation_model", {})
            if fm.get("ai_gateway_v2_supported") is True:
                any_v2 = True
                api_types.update(fm.get("api_types", []))
        if not any_v2 and entities:
            saw_endpoint_without_v2 = True
        if api_type in api_types:
            out.append(name)
    if out:
        return sorted(out, key=sort_key), None
    if not endpoints:
        return [], "foundation-models listing returned no endpoints"
    if saw_endpoint_without_v2:
        return [], (
            f"no endpoint exposes api_type `{api_type}` with "
            "`ai_gateway_v2_supported=true` (workspace has v1-only endpoints)"
        )
    return [], f"no endpoint exposes api_type `{api_type}`"


def discover_gemini_models(workspace: str, token: str) -> tuple[list[str], str | None]:
    # Order newest model version first so `default_model()` (which picks the
    # first entry) launches e.g. gemini-3.5-flash rather than gemini-2.5-flash.
    return discover_endpoints_with_api_type(
        workspace, token, "gemini/v1/generateContent", sort_key=model_version_sort_key
    )


def discover_codex_models(workspace: str, token: str) -> tuple[list[str], str | None]:
    return discover_endpoints_with_api_type(workspace, token, "openai/v1/responses")


def ensure_ai_gateway_v2(workspace: str, token: str) -> None:
    """Probe AI Gateway v2 and raise if unavailable.

    Uses the dedicated v2 listing endpoint `GET /api/ai-gateway/v2/endpoints`:
    a 200 response (even with an empty list) means v2 is wired up on this
    workspace — a "no endpoints provisioned" case will surface naturally in
    downstream discovery. Failure branches:

    - 401 / 403 / 400 with `Invalid Token`: the token is bad for *this*
      workspace.
    - 404: AI Gateway V2 is not enabled on this workspace — point at the docs.
    - other (5xx, network errors): surface the reason verbatim.
    """
    hostname = workspace_hostname(workspace)
    url = f"https://{hostname}/api/ai-gateway/v2/endpoints?page_size=1"
    payload, reason = http_get_json(url, token)
    if payload is not None:
        return
    reason_str = reason or "unknown error"
    if _looks_like_auth_failure(reason_str):
        raise RuntimeError(
            f"Databricks rejected the access token for {workspace} ({reason_str}). "
            f"Try:\n"
            f"  databricks auth logout --host {workspace}\n"
            f"  databricks auth login --host {workspace}"
        )
    if "HTTP 404" in reason_str:
        raise RuntimeError(
            "Databricks Unity AI Gateway is not enabled on this workspace "
            f"({reason_str}). See {AI_GATEWAY_V2_DOCS_URL}"
        )
    raise RuntimeError(
        "Databricks Unity AI Gateway probe failed on this workspace "
        f"({reason_str}). See {AI_GATEWAY_V2_DOCS_URL}"
    )


def _looks_like_auth_failure(reason: str) -> bool:
    """True when the gateway response signals the token is not accepted.

    Covers 401/403 directly and the gateway's 400 + `Invalid Token` body
    (which happens when the bearer is valid but issued for a different
    workspace)."""
    if "HTTP 401" in reason or "HTTP 403" in reason:
        return True
    if "HTTP 400" in reason and "invalid token" in reason.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# URL builders (AI Gateway v2 only — no fallback to /serving-endpoints)
# ---------------------------------------------------------------------------


def build_tool_base_url(tool: str, workspace: str) -> str:
    if tool == "codex":
        return f"{workspace}/ai-gateway/codex/v1"
    if tool == "claude":
        return f"{workspace}/ai-gateway/anthropic"
    if tool == "gemini":
        return f"{workspace}/ai-gateway/gemini"
    if tool == "opencode":
        raise RuntimeError(
            "OpenCode has multiple base URLs — use build_opencode_base_urls() instead."
        )
    if tool == "pi":
        raise RuntimeError("Pi has multiple base URLs — use build_pi_base_urls() instead.")
    raise RuntimeError(f"Unsupported tool '{tool}'.")


def build_opencode_base_urls(workspace: str) -> dict[str, str]:
    return {
        "anthropic": build_tool_base_url("claude", workspace) + "/v1",
        "gemini": build_tool_base_url("gemini", workspace) + "/v1beta",
        "oss": f"{workspace}/ai-gateway/mlflow/v1",
    }


def build_pi_base_urls(workspace: str) -> dict[str, str]:
    # Pi speaks each model family's native API dialect to its dedicated gateway
    # path (verified end-to-end). Each `api` type appends its own path suffix:
    #
    # - anthropic-messages       appends `/v1/messages`
    # - openai-responses         appends `/responses`
    # - google-generative-ai     appends `/v1beta/models/{id}:streamGenerateContent`
    # - openai-completions       appends `/chat/completions`
    #
    # So the baseUrls below stop just before the suffix Pi will tack on.
    # Compat flags applied per-provider in agents/pi.py; required for `oss`
    # only (MLflow rejects `store` and `tools[].function.strict`).
    return {
        "claude": build_tool_base_url("claude", workspace),
        "openai": build_tool_base_url("codex", workspace),
        "gemini": build_tool_base_url("gemini", workspace) + "/v1beta",
    }


def build_shared_base_urls(workspace: str) -> dict[str, str | dict[str, str]]:
    urls: dict[str, str | dict[str, str]] = {
        "opencode": build_opencode_base_urls(workspace),
        "pi": build_pi_base_urls(workspace),
    }
    return urls
