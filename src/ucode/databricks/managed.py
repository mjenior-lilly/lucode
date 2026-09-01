"""Workspace administration, budgets, and managed coding-agent configuration clients."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ucode.databricks.transport import (
    _http_delete,
    _http_get_json,
    _http_patch_json,
    _http_post_json,
    workspace_hostname,
)

# Workspace group whose members are workspace admins. `ucode setup` / `ucode apply` are restricted
# to this group because the coding-agent-config CRUD API enforces the same check server-side.
WORKSPACE_ADMIN_GROUP = "admins"


def _scim_me(workspace: str, token: str) -> dict | None:
    """Return the SCIM `Me` payload for the caller, or None on failure."""
    hostname = workspace_hostname(workspace)
    payload, _ = _http_get_json(f"https://{hostname}/api/2.0/preview/scim/v2/Me", token)
    return payload if isinstance(payload, dict) else None


def is_workspace_admin(workspace: str, token: str) -> bool | None:
    """Whether the caller is a workspace admin, via their SCIM `Me` group membership.

    Returns True/False, or None when the check itself could not be made (SCIM unreachable or a
    malformed response). Callers should treat None as "unknown" and proceed optimistically rather
    than blocking: the API enforces the same check server-side, so a false negative here would
    needlessly stop a legitimate admin, while a false positive just surfaces the server's
    PERMISSION_DENIED later.
    """
    payload = _scim_me(workspace, token)
    if payload is None:
        return None
    groups = payload.get("groups")
    if not isinstance(groups, list):
        # A well-formed `Me` for a user in no groups omits `groups` entirely, so this is a
        # definitive "not an admin" rather than a failed check.
        return False
    return any(
        isinstance(group, dict) and group.get("display") == WORKSPACE_ADMIN_GROUP
        for group in groups
    )


# Workspace-scoped budget listing. Account-level budget APIs need account auth, which ucode does not
# have; this endpoint resolves the workspace server-side from the caller's token.
_WORKSPACE_BUDGETS_API_PATH = "/api/ai-gateway/v2/workspace-metrics/budgets"


def list_workspace_budgets(workspace: str, token: str) -> tuple[list[dict], str | None]:
    """List the AI Gateway budgets that apply to this workspace.

    Returns ``(budgets, reason)`` where each budget is ``{"id": ..., "display_name": ...}``.
    ``reason`` is None on success, otherwise it explains why the list is empty. ucode never creates
    budgets — an admin picks an existing one to attach a spend-routing policy to.
    """
    hostname = workspace_hostname(workspace)
    url = f"https://{hostname}{_WORKSPACE_BUDGETS_API_PATH}"
    payload, reason = _http_get_json(url, token, timeout=30)
    if reason is not None:
        return [], reason
    if not isinstance(payload, dict):
        return [], "workspace budget listing returned an unexpected response shape"
    raw = payload.get("workspace_ai_gateway_budgets")
    if not isinstance(raw, list):
        return [], "workspace budget listing returned no budgets"
    budgets: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        budget_id = entry.get("budget_configuration_id")
        if not isinstance(budget_id, str) or not budget_id:
            continue
        display_name = entry.get("display_name")
        budgets.append(
            {
                "id": budget_id,
                "display_name": display_name if isinstance(display_name, str) else "",
            }
        )
    if not budgets:
        return [], "workspace budget listing returned no budgets"
    return budgets, None


def get_current_user_name(workspace: str, token: str) -> str | None:
    """Return the current user's login (email) via SCIM `Me`, or None on failure.

    Databricks puts the workspace login in `userName`; fall back to the first
    `emails` entry for workspaces that diverge."""
    payload = _scim_me(workspace, token)
    if payload is None:
        return None
    user_name = payload.get("userName")
    if isinstance(user_name, str) and user_name.strip():
        return user_name.strip()
    emails = payload.get("emails")
    if isinstance(emails, list):
        for entry in emails:
            if isinstance(entry, dict) and isinstance(entry.get("value"), str):
                return entry["value"].strip()
    return None


# --- Managed coding-agent config (admin-authored, developer-read) -----------

# The workspace-admin authors a CodingAgentConfig via the AI Gateway; developers read it
# (non-admin) through the List endpoint and apply it locally.
_CODING_AGENT_CONFIGS_API_PATH = "/api/ai-gateway/v2/coding-agent-configs"


def fetch_managed_coding_agent_configs(workspace: str, token: str) -> tuple[list[dict], str | None]:
    """List the workspace's managed CodingAgentConfig(s) via the AI Gateway."""
    hostname = workspace_hostname(workspace)
    url = f"https://{hostname}{_CODING_AGENT_CONFIGS_API_PATH}"
    payload, reason = _http_get_json(url, token, timeout=30)
    if reason is not None:
        return [], reason
    if isinstance(payload, dict):
        configs = payload.get("coding_agent_configs") or []
    elif isinstance(payload, list):
        configs = payload
    else:
        return [], "coding-agent-configs listing returned an unexpected response shape"
    if not isinstance(configs, list):
        return [], "coding-agent-configs listing returned an unexpected response shape"
    return [c for c in configs if isinstance(c, dict)], None


def fetch_model_recommendation(workspace: str, token: str) -> tuple[dict, str | None]:
    """Ask the AI Gateway which agent and model the caller's budget tier allows.

    The request takes no parameters: the server matches the caller's live spend against the managed
    config's budget tiers and resolves the agent first, then that agent's model.
    """
    hostname = workspace_hostname(workspace)
    url = f"https://{hostname}{_CODING_AGENT_CONFIGS_API_PATH}:recommendModel"
    payload, reason = _http_post_json(url, token, {}, timeout=30)
    if reason is not None:
        return {}, reason
    if not isinstance(payload, dict):
        return {}, "recommendModel returned an unexpected response shape"
    return payload, None


# Every field ucode's manifest can set, as `update_mask` paths for a PATCH. The server rejects a
# missing or empty mask, and rejects paths outside its own mutable set — this is that set minus the
# fields ucode doesn't author: `budget_id` (deprecated in favour of `budget_policy.budget_id`, and
# rejected on write) and `default_options`/`tiers` (the legacy model-only shape superseded by
# `enabled_agents`/`budget_policy`). Sending every path ucode owns, rather than only the ones
# currently populated, is what lets a re-run *clear* a field the admin removed: the server merges
# per path, so an omitted path leaves the old value in place.
MANAGED_CONFIG_UPDATE_MASK_PATHS: tuple[str, ...] = (
    "display_name",
    "default_agent",
    "enabled_agents",
    "mcp_servers",
    "skills",
    "budget_policy",
)


def _coding_agent_config_url(workspace: str, name: str | None = None) -> str:
    """The collection URL, or one config's resource URL when ``name`` is given.

    ``name`` is the server-assigned resource name (``coding-agent-configs/{id}``), which the Get and
    Update paths template directly, so it is appended as-is rather than rebuilt from an id.
    """
    hostname = workspace_hostname(workspace)
    base = f"https://{hostname}{_CODING_AGENT_CONFIGS_API_PATH}"
    if name is None:
        return base
    # The resource name already carries the collection segment, so join on the API root.
    root = base.rsplit("/coding-agent-configs", 1)[0]
    return f"{root}/{name.strip().strip('/')}"


def create_coding_agent_config(
    workspace: str, token: str, config: dict
) -> tuple[dict | None, str | None]:
    """Create the workspace's managed CodingAgentConfig.

    v0 allows at most one config per workspace, so this fails with ALREADY_EXISTS when one is
    already defined; callers should update that one instead of creating a second.
    """
    url = _coding_agent_config_url(workspace)
    payload, reason = _http_post_json(url, token, config, timeout=30)
    if reason is not None:
        return None, reason
    if not isinstance(payload, dict):
        return None, "coding-agent-config create returned an unexpected response shape"
    return payload, None


def update_coding_agent_config(
    workspace: str,
    token: str,
    name: str,
    config: dict,
    *,
    update_mask: tuple[str, ...] = MANAGED_CONFIG_UPDATE_MASK_PATHS,
) -> tuple[dict | None, str | None]:
    """Update an existing managed CodingAgentConfig in place.

    Preferred over delete-then-create: the server applies the mask inside a single entity-store
    update, so the workspace is never left without a config if the write fails partway. ``name``
    identifies the config and is echoed in the body, which is what the API's path template expects.

    ``update_mask`` goes in the query string, not the body. The RPC's HTTP binding is
    ``patch: "…/{coding_agent_config.name=coding-agent-configs/*}"`` with ``body:
    "coding_agent_config"`` — the config *is* the whole body, so a mask nested inside it is parsed
    as an unknown config field and the server reports the mask as missing:

        Field 'update_mask' is required and must contain at least one subfield with a non-default
        value!

    It is also a ``google.protobuf.FieldMask``, whose JSON/query form is one comma-separated string
    rather than a ``{"paths": [...]}`` object.
    """
    query = urlencode({"update_mask": ",".join(update_mask)})
    url = f"{_coding_agent_config_url(workspace, name)}?{query}"
    body = {**config, "name": name}
    payload, reason = _http_patch_json(url, token, body, timeout=30)
    if reason is not None:
        return None, reason
    if not isinstance(payload, dict):
        return None, "coding-agent-config update returned an unexpected response shape"
    return payload, None


def delete_coding_agent_config(workspace: str, token: str, name: str) -> str | None:
    """Delete a managed CodingAgentConfig by resource name. Returns None on success, else a reason.

    Returns only the failure reason: a successful delete responds with ``Empty``, so there is no
    payload worth handing back.
    """
    url = _coding_agent_config_url(workspace, name)
    _, reason = _http_delete(url, token, timeout=30)
    return reason


CODING_AGENT_RECOMMEND_MODEL_PATH = "/api/ai-gateway/v2/coding-agent-configs:recommendModel"


def resolve_current_budget_spend(
    workspace: str,
    token: str,
    *,
    timeout: int = 10,
) -> tuple[tuple[Decimal, Decimal] | None, str | None]:
    """Fetch the caller's coding-agent budget spend and alert threshold.

    Reads them off `recommendModel`, which returns the spend its model
    recommendation was based on. `available_models` is empty since we want the
    spend, not the recommendation.

    Returns `((spend, threshold), None)` or `(None, reason)`. Absence is
    routine — the endpoint needs a per-org SAFE flag (default off) and a
    coding-agent config — so it never raises.
    """
    url = f"https://{workspace_hostname(workspace)}{CODING_AGENT_RECOMMEND_MODEL_PATH}"
    payload, reason = _http_post_json(url, token, {"available_models": []}, timeout=timeout)
    if payload is None:
        return None, reason or "unknown error"
    if not isinstance(payload, dict):
        return None, "response was not a JSON object"

    # Per the server's BudgetSpend.fromProto, a spend with no threshold to
    # measure against counts as no spend.
    spend = _parse_decimal(payload.get("current_spend"))
    threshold = _parse_decimal(payload.get("effective_threshold"))
    if spend is None or threshold is None:
        return None, "workspace reported no coding-agent budget spend"
    return (spend, threshold), None


def _parse_decimal(value: object) -> Decimal | None:
    if isinstance(value, str) and value.strip():
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    if isinstance(value, int):
        return Decimal(value)
    return None
