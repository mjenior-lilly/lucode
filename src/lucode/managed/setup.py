"""Admin-authored managed coding-agent config: model catalogs, validation, and serialization.

This module owns the admin-write half of the managed config, mirroring the developer-read half in
:mod:`lucode.managed.config`:

- which models an admin may pick for each agent (:func:`model_options_for_agent`),
- validating a manifest before it is published (:func:`validate_manifest`),
- serializing lucode's internal manifest shape into proto-JSON ``CodingAgentConfig``
  (:func:`serialize_managed_config`), and
- persisting the authored manifest to ``~/.lucode/managed-settings.json``.

The manifest shape here is exactly the one :func:`lucode.managed.config.normalize_managed_config`
produces, so ``serialize`` then ``normalize`` round-trips to the input. The enum maps are derived by
inverting that module's maps rather than restated, so a new agent or MCP type only has to be added
once.

``managed-settings.json`` (authored by an admin, published by ``lucode apply``) is distinct from
``managed-state.json`` (pulled from the workspace by a developer, owned by
:mod:`lucode.managed.config`).

The interactive wizard that calls these helpers, and the publish step, live in later changes; this
module deliberately stops at "catalogs + validate + serialize + persist".
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import cast

import lucode.config as app_config
from lucode.databricks.models import ANTHROPIC_FAMILIES
from lucode.managed.config import (
    AGENT_ENUM_TO_TOOL,
    MCP_TYPE_ENUM_TO_TAG,
)

MANAGED_SETTINGS_PATH = app_config.APP_DIR / "managed-settings.json"

# lucode tool name -> CodingAgent proto enum, and lucode MCP type tag -> McpServerType proto enum.
# Inverted from the read side's maps so the two directions cannot drift: adding an agent to
# `lucode.managed.config.AGENT_ENUM_TO_TOOL` makes it serializable here automatically.
AGENT_TOOL_TO_ENUM: dict[str, str] = {tool: enum for enum, tool in AGENT_ENUM_TO_TOOL.items()}
MCP_TAG_TO_TYPE_ENUM: dict[str, str] = {tag: enum for enum, tag in MCP_TYPE_ENUM_TO_TAG.items()}

# Both surviving agents use flat repeated model lists.
_FLAT_MODEL_LIST_AGENTS = frozenset({"opencode", "pi"})
_AGENT_MODEL_FAMILIES: dict[str, tuple[str, ...]] = {
    "opencode": ("claude", "gemini", "oss"),
    "pi": ("claude", "codex", "gemini"),
}


def _as_dict(value: object) -> dict[str, object]:
    """Return ``value`` as a ``dict[str, object]`` when it is a dict, else an empty dict.

    Mirrors the read side's helper: a bare ``isinstance(x, dict)`` narrows to ``dict[Never, Never]``,
    which rejects string keys, so ``.get("name")`` on an untyped manifest fails type-checking without
    this.
    """
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def model_families_for_agent(tool: str) -> tuple[str, ...]:
    """The discovered model families ``tool`` can be configured with."""
    return _AGENT_MODEL_FAMILIES.get(tool, ())


def model_options_for_agent(tool: str, state: dict) -> list[str]:
    """Models an admin may pick for ``tool``, drawn from the workspace's discovered inventory.

    ``state`` is a hydrated workspace state (as ``configure_shared_state`` produces): ``claude_models``
    is a family->id dict, while ``codex_models`` / ``gemini_models`` / ``oss_models`` are lists.
    Returns a de-duplicated list in a stable order (Claude newest-family first, then the family
    order in :data:`_AGENT_MODEL_FAMILIES`) — empty when nothing was discovered for those families,
    in which case the caller should fall back to free-text entry.
    """
    options: list[str] = []
    for family in model_families_for_agent(tool):
        if family == "claude":
            claude_models = state.get("claude_models")
            if isinstance(claude_models, dict):
                # ANTHROPIC_FAMILIES is newest-tier-first, so slot order is meaningful.
                for name in ANTHROPIC_FAMILIES:
                    model = claude_models.get(name)
                    if isinstance(model, str) and model:
                        options.append(model)
            continue
        key = {"codex": "codex_models", "gemini": "gemini_models", "oss": "oss_models"}[family]
        models = state.get(key)
        if isinstance(models, list):
            options.extend(m for m in models if isinstance(m, str) and m)
    # dict.fromkeys de-duplicates while preserving first-seen order (a model can match more than
    # one family bucket, e.g. an OSS id that also appears in the codex listing).
    return list(dict.fromkeys(options))


def _model_config_payload(tool: str, model_config: dict) -> dict:
    """Build a flat-list ``AgentModelConfig`` variant for a surviving tool."""
    body: dict = {}
    default_model = model_config.get("default_model")
    if isinstance(default_model, str) and default_model:
        body["default_model"] = default_model
    models = model_config.get("models")
    if tool in _FLAT_MODEL_LIST_AGENTS and isinstance(models, list):
        model_list = [m for m in models if isinstance(m, str) and m]
        if model_list:
            body["models"] = model_list
    return body


def _enabled_agent_payload(tool: str, agent_config: dict) -> dict:
    """Build one ``EnabledAgent`` entry (agent enum + its ``AgentConfig``)."""
    config: dict = {}
    use_as_global = agent_config.get("use_as_global_settings")
    if isinstance(use_as_global, bool):
        config["use_as_global_settings"] = use_as_global
    headers = agent_config.get("custom_headers")
    if isinstance(headers, dict):
        clean = {k: v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)}
        if clean:
            config["custom_headers"] = clean
    model_config = agent_config.get("model_config")
    if isinstance(model_config, dict):
        body = _model_config_payload(tool, model_config)
        if body:
            config["model_config"] = {tool: body}

    entry: dict = {"agent": AGENT_TOOL_TO_ENUM[tool]}
    if config:
        entry["config"] = config
    return entry


def _budget_policy_payload(budget_policy: dict) -> dict:
    """Build the ``BudgetPolicy`` body, dropping tiers that name an unknown agent.

    ``spending_percentage`` is passed through as-is: it is a fraction in [0, 1] both in lucode's
    manifest and in the proto (the server validates that range). Callers prompting an admin in
    percent must divide before building the manifest.
    """
    payload: dict = {}
    display_name = budget_policy.get("display_name")
    if isinstance(display_name, str) and display_name:
        payload["display_name"] = display_name
    budget_id = budget_policy.get("budget_id")
    if isinstance(budget_id, str) and budget_id:
        payload["budget_id"] = budget_id

    tiers: list[dict] = []
    raw_tiers = budget_policy.get("tiers")
    for tier in raw_tiers if isinstance(raw_tiers, list) else []:
        if not isinstance(tier, dict):
            continue
        pct = tier.get("spending_percentage")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        tier_payload: dict = {"spending_percentage": float(pct)}
        agent_enum = AGENT_TOOL_TO_ENUM.get(str(tier.get("default_agent") or ""))
        if agent_enum:
            tier_payload["default_agent"] = agent_enum
        default_model = tier.get("default_model")
        if isinstance(default_model, str) and default_model:
            tier_payload["default_model"] = default_model
        tiers.append(tier_payload)
    if tiers:
        payload["tiers"] = tiers
    return payload


def serialize_managed_config(manifest: dict) -> dict:
    """Serialize lucode's internal manifest into a proto-JSON ``CodingAgentConfig``.

    The exact inverse of :func:`lucode.managed.config.normalize_managed_config`: tool names become
    ``CODING_AGENT_*`` enums, MCP type tags become ``MCP_SERVER_TYPE_*``, and each agent's model
    config is wrapped in its matching ``AgentModelConfig`` oneof variant. Agents and MCP types this
    build doesn't recognize are dropped, mirroring the read side.

    Output-only proto fields (``workspace_id``, timestamps, user ids) are never emitted. ``name`` is
    carried through when present so an update path can address an existing resource; ``lucode apply``
    omits it on create and lets the server assign one.
    """
    payload: dict = {}

    name = manifest.get("name")
    if isinstance(name, str) and name:
        payload["name"] = name
    display_name = manifest.get("display_name")
    if isinstance(display_name, str) and display_name:
        payload["display_name"] = display_name

    default_agent = AGENT_TOOL_TO_ENUM.get(str(manifest.get("default_agent") or ""))
    if default_agent:
        payload["default_agent"] = default_agent

    enabled_agents = manifest.get("enabled_agents")
    if isinstance(enabled_agents, dict):
        entries = [
            _enabled_agent_payload(tool, agent_config)
            for tool, agent_config in enabled_agents.items()
            if tool in AGENT_TOOL_TO_ENUM and isinstance(agent_config, dict)
        ]
        if entries:
            payload["enabled_agents"] = entries

    mcp_servers = manifest.get("mcp_servers")
    if isinstance(mcp_servers, list):
        servers: list[dict] = []
        for server in mcp_servers:
            if not isinstance(server, dict):
                continue
            server_name = server.get("name")
            type_enum = MCP_TAG_TO_TYPE_ENUM.get(str(server.get("type") or ""))
            if isinstance(server_name, str) and server_name and type_enum:
                servers.append({"name": server_name, "type": type_enum})
        if servers:
            payload["mcp_servers"] = servers

    skills = manifest.get("skills")
    if isinstance(skills, dict):
        names = skills.get("names")
        if isinstance(names, list):
            skill_names = [n for n in names if isinstance(n, str) and n]
            if skill_names:
                payload["skills"] = {"names": skill_names}

    budget_policy = manifest.get("budget_policy")
    if isinstance(budget_policy, dict):
        policy = _budget_policy_payload(budget_policy)
        if policy:
            payload["budget_policy"] = policy

    return payload


def _manifest_default_model(agent_config: dict) -> str | None:
    """The ``default_model`` on an agent's model config, or None when unset/empty."""
    model_config = agent_config.get("model_config")
    if not isinstance(model_config, dict):
        return None
    default_model = model_config.get("default_model")
    return default_model if isinstance(default_model, str) and default_model else None


def _known_models(state: dict) -> set[str]:
    """Every model id discovered on the workspace, across all families.

    Empty when discovery found nothing (or no state was passed), which callers treat as "can't
    check" rather than "nothing is valid".

    ``claude_models`` holds only the newest id per family (the launch path pins one model per family
    alias), so on its own it would reject the older versions the per-family prompts legitimately
    offer. ``all_claude_models`` carries the full listing when the caller has it.
    """
    known: set[str] = set()
    claude_models = state.get("claude_models")
    if isinstance(claude_models, dict):
        known.update(m for m in claude_models.values() if isinstance(m, str) and m)
    for key in ("codex_models", "gemini_models", "oss_models", "all_claude_models"):
        models = state.get(key)
        if isinstance(models, list):
            known.update(m for m in models if isinstance(m, str) and m)
    return known


def _validate_agent_models(tool: str, agent_config: dict, known: set[str]) -> list[str]:
    """Check one agent's configured models against the workspace inventory."""
    model_config = agent_config.get("model_config")
    if not isinstance(model_config, dict):
        return []
    referenced: list[str] = []
    default_model = model_config.get("default_model")
    if isinstance(default_model, str) and default_model:
        referenced.append(default_model)
    models = model_config.get("models")
    if isinstance(models, dict):
        referenced.extend(m for m in models.values() if isinstance(m, str) and m)
    elif isinstance(models, list):
        referenced.extend(m for m in models if isinstance(m, str) and m)

    errors = [
        f"{tool}: model '{model}' is not available on this workspace."
        for model in dict.fromkeys(referenced)
        if known and model not in known
    ]
    errors.extend(
        f"{tool}: Fable model '{model}' is not supported."
        for model in dict.fromkeys(referenced)
        if "fable" in model.lower()
    )
    return errors


def validate_manifest(manifest: dict, state: dict | None = None) -> list[str]:
    """Validate a manifest before publishing; returns human-readable errors (empty when valid).

    Mirrors the server's ``validateStoredConfig`` so an admin sees problems locally instead of via
    an ``INVALID_PARAMETER_VALUE`` round-trip:

    - ``default_agent`` is required once any agent configuration is present, must appear in
      ``enabled_agents``, and that agent must have a non-empty ``default_model``;
    - every ``enabled_agents`` key must be an agent this lucode build knows;
    - each MCP server needs a name and a recognized type; skill names must be non-empty;
    - a ``budget_policy`` needs a ``budget_id``, and each tier needs a ``spending_percentage`` in
      [0, 1] (unique across tiers), a ``default_agent`` that appears in ``enabled_agents``, and a
      ``default_model``.

    When ``state`` is provided, configured models are additionally checked against the workspace's
    discovered inventory.
    """
    errors: list[str] = []

    enabled_agents_raw = manifest.get("enabled_agents")
    enabled_agents: dict[str, dict] = {}
    if isinstance(enabled_agents_raw, dict):
        for tool, agent_config in enabled_agents_raw.items():
            if tool not in AGENT_TOOL_TO_ENUM:
                valid = ", ".join(sorted(AGENT_TOOL_TO_ENUM))
                errors.append(
                    f"enabled_agents: '{tool}' is not a supported agent (valid: {valid})."
                )
                continue
            if not isinstance(agent_config, dict):
                errors.append(f"enabled_agents: '{tool}' config must be an object.")
                continue
            enabled_agents[tool] = agent_config

    default_agent = manifest.get("default_agent")
    budget_policy = manifest.get("budget_policy")
    has_agent_selection = bool(default_agent or enabled_agents_raw or budget_policy)
    if has_agent_selection:
        if not default_agent:
            errors.append("default_agent is required when agent configuration is present.")
        elif default_agent not in enabled_agents:
            errors.append(f"default_agent '{default_agent}' must appear in enabled_agents.")
        elif not _manifest_default_model(enabled_agents[default_agent]):
            errors.append(
                f"default_agent '{default_agent}' must have a non-empty model_config.default_model."
            )

    known = _known_models(state or {})
    for tool, agent_config in enabled_agents.items():
        errors.extend(_validate_agent_models(tool, agent_config, known))

    mcp_servers = manifest.get("mcp_servers")
    if isinstance(mcp_servers, list):
        for index, raw_server in enumerate(mcp_servers, start=1):
            if not isinstance(raw_server, dict):
                errors.append(f"mcp_servers[{index}] must be an object.")
                continue
            server = _as_dict(raw_server)
            if not server.get("name"):
                errors.append(f"mcp_servers[{index}]: name is required.")
            server_type = str(server.get("type") or "")
            if server_type not in MCP_TAG_TO_TYPE_ENUM:
                valid = ", ".join(sorted(MCP_TAG_TO_TYPE_ENUM))
                errors.append(
                    f"mcp_servers[{index}]: type '{server_type}' is not recognized "
                    f"(valid: {valid})."
                )
    elif "mcp_servers" in manifest:
        errors.append("mcp_servers must be a list.")

    skills = manifest.get("skills")
    if isinstance(skills, dict):
        names = skills.get("names")
        if isinstance(names, list):
            if any(not isinstance(name, str) or not name for name in names):
                errors.append("skills.names must not contain empty names.")
        elif "names" in skills:
            errors.append("skills.names must be a list.")
    elif "skills" in manifest:
        errors.append("skills must be an object.")

    if isinstance(budget_policy, dict):
        errors.extend(_validate_budget_policy(budget_policy, enabled_agents))
    elif "budget_policy" in manifest:
        errors.append("budget_policy must be an object.")

    return errors


def _agent_model_ids(agent_config: dict) -> set[str]:
    """Every model id an agent is configured with — its list plus its default.

    Models use a flat list. Returns an empty set when nothing is configured, which callers treat as
    "can't check" rather than "nothing is allowed".
    """
    model_config = agent_config.get("model_config")
    if not isinstance(model_config, dict):
        return set()
    ids: set[str] = set()
    raw = model_config.get("models")
    if isinstance(raw, list):
        ids.update(m for m in raw if isinstance(m, str) and m)
    default_model = model_config.get("default_model")
    if isinstance(default_model, str) and default_model:
        ids.add(default_model)
    return ids


def _validate_budget_policy(budget_policy: dict, enabled_agents: dict[str, dict]) -> list[str]:
    """Validate a ``budget_policy`` against the agents the manifest enables.

    Tier positions are reported 0-based to match the server's own messages, which index with
    ``zipWithIndex`` — an admin comparing the two error sources should see the same number.
    """
    errors: list[str] = []
    budget_id = budget_policy.get("budget_id")
    if not budget_id:
        errors.append("budget_policy.budget_id is required.")
    else:
        # The server requires a parseable UUID here. The wizard can only offer real
        # `budget_configuration_id`s, but `--from-file` and hand-edited manifests can carry
        # anything, and catching it locally beats an INVALID_PARAMETER_VALUE round-trip.
        try:
            uuid.UUID(str(budget_id))
        except ValueError:
            errors.append(
                f"budget_policy.budget_id must be a UUID (got '{budget_id}'). Use the "
                "budget_configuration_id from the workspace's AI Gateway budgets."
            )

    percentages: list[float] = []
    tiers = budget_policy.get("tiers")
    if "tiers" in budget_policy and not isinstance(tiers, list):
        errors.append("budget_policy.tiers must be a list.")
    for index, tier in enumerate(tiers if isinstance(tiers, list) else []):
        if not isinstance(tier, dict):
            errors.append(f"budget_policy.tiers[{index}] must be an object.")
            continue
        pct = tier.get("spending_percentage")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            errors.append(f"budget_policy.tiers[{index}]: spending_percentage is required.")
        elif not 0 <= float(pct) <= 1:
            errors.append(
                f"budget_policy.tiers[{index}]: spending_percentage must be a fraction "
                f"between 0 and 1 (got {pct})."
            )
        else:
            percentages.append(float(pct))
        tier_agent = tier.get("default_agent")
        tier_model = tier.get("default_model")
        if not tier_agent:
            errors.append(f"budget_policy.tiers[{index}]: default_agent is required.")
        elif tier_agent not in enabled_agents:
            errors.append(
                f"budget_policy.tiers[{index}]: default_agent '{tier_agent}' must appear "
                "in enabled_agents."
            )
        elif tier_model:
            # The server only checks that the tier's agent is enabled, not that it has the model —
            # so without this a tier can activate and hand the developer a model their agent was
            # never configured with. Skipped when the agent lists no models (it then has only a
            # default).
            available = _agent_model_ids(enabled_agents[tier_agent])
            if available and tier_model not in available:
                errors.append(
                    f"budget_policy.tiers[{index}]: default_model '{tier_model}' is not one of the "
                    f"models configured for '{tier_agent}' ({', '.join(sorted(available))})."
                )
        if not tier_model:
            errors.append(f"budget_policy.tiers[{index}]: default_model is required.")

    if len(set(percentages)) != len(percentages):
        errors.append("budget_policy tier spending_percentage values must be unique.")
    return errors


def save_managed_settings(workspace: str, manifest: dict) -> None:
    """Persist the authored manifest to ``~/.lucode/managed-settings.json``. No-op in dry-run.

    Stored alongside its workspace so ``lucode apply`` can refuse to publish a manifest that was
    authored against a different workspace.
    """
    if app_config.is_dry_run():
        return
    payload = {"workspace": workspace, "config": manifest}
    app_config.ensure_parent_dir(MANAGED_SETTINGS_PATH)
    try:
        MANAGED_SETTINGS_PATH.write_text(
            json.dumps(payload, indent=app_config.JSON_INDENT) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise RuntimeError(
            f"Failed to write managed settings file: {MANAGED_SETTINGS_PATH}"
        ) from exc
    _restrict_permissions(MANAGED_SETTINGS_PATH)


def _restrict_permissions(path: Path) -> None:
    """Best-effort chmod 0600, matching how :mod:`lucode.managed.config` protects its state file.

    An unpublished manifest can name internal catalogs, budgets, and MCP servers, so it should not
    be group- or world-readable. No-op where unsupported (e.g. Windows).
    """
    try:
        os.chmod(path, app_config.PRIVATE_FILE_MODE)
    except (OSError, NotImplementedError):
        pass


def load_managed_settings(workspace: str | None = None) -> dict | None:
    """Load the authored manifest, or None when absent (or authored for another workspace).

    Passing ``workspace`` scopes the read the way :func:`lucode.managed.config.load_managed_state`
    does, so a manifest left over from a different workspace is ignored rather than published to the
    wrong place. Omit it to read whatever is on disk.
    """
    data = app_config.read_json_safe(MANAGED_SETTINGS_PATH)
    if not data:
        return None
    if workspace is not None and data.get("workspace") != workspace:
        return None
    manifest = data.get("config")
    return manifest if isinstance(manifest, dict) else None


def managed_settings_workspace() -> str | None:
    """The workspace the on-disk manifest was authored for, or None when there is no manifest."""
    workspace = app_config.read_json_safe(MANAGED_SETTINGS_PATH).get("workspace")
    return workspace if isinstance(workspace, str) and workspace else None
