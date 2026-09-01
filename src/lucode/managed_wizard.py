"""Interactive `lucode setup`: author the workspace's managed coding-agent config.

Workspace admins run this to build the ``CodingAgentConfig`` their developers will pull. It walks
the admin through agents, per-agent models, MCP servers, skills, and a spend-routing budget policy,
then writes the manifest to ``~/.lucode/managed-settings.json``. Publishing it to the workspace is
``lucode apply`` (a separate command, so an admin can review the file first).

Serialization, validation, and the per-agent model catalogs live in :mod:`lucode.managed_setup`; this
module is the interaction layer on top of them. Sub-flows an admin already knows — MCP, skills — are
delegated to the existing ``lucode configure <thing>`` commands and their results read back out of
``state.json``, so there is exactly one picker per concern in the codebase.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from lucode.agents import TOOL_SPECS, check_gateway_endpoint
from lucode.config_io import is_dry_run
from lucode.databricks.auth import ensure_databricks_auth, get_databricks_token
from lucode.databricks.managed import (
    create_coding_agent_config,
    delete_coding_agent_config,
    is_workspace_admin,
    list_workspace_budgets,
    update_coding_agent_config,
)
from lucode.managed_config import get_managed_config
from lucode.managed_setup import (
    load_managed_settings,
    model_options_for_agent,
    save_managed_settings,
    serialize_managed_config,
    validate_manifest,
)
from lucode.mcp import (
    SKILLS_MCP_KIND,
    _skill_mcp_locations,
    configure_mcp_command,
    configure_skills_mcp_command,
)
from lucode.state import load_state
from lucode.ui import (
    console,
    kv_line,
    print_err,
    print_heading,
    print_note,
    print_panel,
    print_section,
    print_success,
    print_warning,
    prompt_for_multi_selection,
    prompt_for_percentage,
    prompt_for_selection,
    prompt_for_text,
    prompt_for_tools,
    prompt_yes_no_default,
    spinner,
)

# What `use_as_global_settings` actually does, in plain terms. Admins are choosing between a
# machine-wide managed settings file and a per-user one, which is not obvious from the field name.
GLOBAL_SETTINGS_BLURB = (
    "Write this agent's config to the machine's managed settings file, which applies to every "
    "user on the machine and cannot be overridden locally. Answer no to write the per-user "
    "settings file instead, which developers can still change."
)

BUDGET_POLICY_BLURB = (
    "A budget policy moves developers onto cheaper agents and models as the workspace spends "
    "against a budget — for example Pi on Opus by default, then OpenCode on Kimi at 80%. "
    "It only changes the default; developers can still pick anything "
    "they have access to. Hard caps stay with the budget's own blocking threshold."
)


def _mcp_type_for_url(url: str) -> str | None:
    """Classify a registered MCP server's URL into a managed-config type tag.

    ``state.json`` stores each MCP server's resolved URL but not its type, while the managed config
    stores ``{name, type}`` and lets the developer's lucode rebuild the URL. The URL shape is the only
    signal available, so map it back. Returns None for a URL that matches nothing known, so unknown
    servers are skipped rather than published with a guessed type.
    """
    if "/ai-gateway/mcp-services/" in url:
        return "mcp-service"
    for fragment, tag in (
        ("/api/2.0/mcp/external/", "external"),
        ("/api/2.0/mcp/genie/", "genie-space"),
        ("/api/2.0/mcp/vector-search/", "vector-search"),
        ("/api/2.0/mcp/functions/", "uc-functions"),
    ):
        if fragment in url:
            return tag
    if url.rstrip("/").endswith("/api/2.0/mcp/sql"):
        return "sql"
    # Databricks apps are the residual case: an arbitrary app host with a /mcp suffix.
    if url.rstrip("/").endswith("/mcp"):
        return "app"
    return None


def _mcp_servers_from_state(state: dict) -> list[dict]:
    """The registered MCP servers, as managed-config ``{name, type}`` entries.

    Skips the skills registry connection: skills are published under the manifest's own ``skills``
    field, so including its MCP entry would configure it twice.
    """
    servers: list[dict] = []
    for entry in state.get("mcp_servers") or []:
        if not isinstance(entry, dict) or entry.get("kind") == SKILLS_MCP_KIND:
            continue
        name = entry.get("name")
        url = entry.get("url")
        if not isinstance(name, str) or not name or not isinstance(url, str):
            continue
        tag = _mcp_type_for_url(url)
        if tag is None:
            print_warning(f"Skipping MCP server '{name}': unrecognized URL shape ({url}).")
            continue
        servers.append({"name": name, "type": tag})
    return servers


def _skill_names_from_state(state: dict) -> list[str]:
    """Skill schemas registered on the skills MCP connection (``catalog.schema`` entries)."""
    return [name for name in _skill_mcp_locations(state) if isinstance(name, str) and name]


def _prompt_models_for_agent(tool: str, state: dict) -> dict:
    """Build one agent's ``model_config``. Every agent ends up with a ``default_model``.

    Both surviving agents (Pi and OpenCode) pick from the workspace's discovered models, filtered to
    the families that agent can actually serve, and keep a flat list plus their chosen default.

    An empty selection is re-prompted rather than accepted: an agent with no ``default_model`` cannot
    be the config's ``default_agent`` (the server rejects it) and gives developers nothing to launch,
    so "none" is never a useful answer here. Ctrl-C still aborts the whole flow.

    Model ids are stored bare (e.g. ``system.ai.claude-opus-4-8``), not provider-prefixed: each
    agent's own writer adds whatever prefix its config format needs (see
    ``opencode._resolve_model_selector``), which keeps the manifest agent-neutral.
    """
    display = TOOL_SPECS[tool]["display"]
    model_config: dict = {}
    options = model_options_for_agent(tool, state)
    if not options:
        print_warning(f"No models were discovered for {display} on this workspace.")
        return {"default_model": _require_text(f"Default model for {display}")}

    # Nothing pre-checked: the first option is whatever discovery sorted first, not a
    # recommendation. Pre-checking it made "hit Enter" produce an arbitrary config.
    picked = _require_multi_selection(
        f"Select models for {display}:",
        [(model, model) for model in options],
    )
    if len(picked) == 1:
        model_config["default_model"] = picked[0]
    else:
        model_config["default_model"] = _require_selection(
            f"Default model for {display}:", [(model, model) for model in picked]
        )

    model_config["models"] = picked
    return model_config


# Every picker in this flow chooses a model or a budget — lists that on a real
# workspace run to a dozen-plus entries (16 GPT models on the workspace this was built against), so
# they are all filterable by typing. That trades away j/k navigation, which questionary can't offer
# alongside search; arrow keys still work.
def _require_selection(prompt: str, options: list[tuple[str, str]]) -> str:
    """Single-select that won't take "nothing" for an answer.

    ``prompt_for_selection`` returns None for both Ctrl-C and an empty submission, and the two are
    genuinely indistinguishable here: questionary's ``Question.ask`` catches KeyboardInterrupt
    internally and returns None (v2.1.1, question.py), so nothing propagates for a caller to see.
    A None is therefore treated as an abort rather than re-asked — re-asking looped forever on
    Ctrl-C, printing the error once per keypress and never exiting.
    """
    answer = prompt_for_selection(prompt, options, searchable=True)
    if not answer:
        raise KeyboardInterrupt
    return answer


def _require_multi_selection(
    prompt: str, options: list[tuple[str, str]], preselected: list[str] | None = None
) -> list[str]:
    """Multi-select that requires at least one choice. None (Ctrl-C) still aborts."""
    while True:
        picked = prompt_for_multi_selection(
            prompt, options, preselected=preselected, searchable=True
        )
        if picked is None:
            raise KeyboardInterrupt
        if picked:
            return picked
        print_err("Select at least one model (space to toggle, enter to confirm).")


def _require_text(prompt: str) -> str:
    """Free-text prompt that requires a non-empty answer.

    ``required=True`` makes closed stdin abort instead of returning None. Without it a
    non-interactive run (piped stdin, CI) spun here forever: ``prompt_for_text`` returns its default
    on EOF, the default is None, and the loop re-asked an empty stream. Reachable whenever model
    discovery finds nothing, which is exactly when a run is most likely to be scripted.
    """
    while True:
        answer = prompt_for_text(prompt, required=True)
        if answer:
            return answer
        print_err("Please enter a model id.")


def configured_models_for_agent(agent_config: dict) -> list[str]:
    """Models an agent was configured with, in the manifest's own vocabulary.

    Both surviving agents use a flat ``model_config.models`` list. The ``default_model`` is included
    because an agent may carry only a default with no explicit list.
    """
    model_config = agent_config.get("model_config")
    if not isinstance(model_config, dict):
        return []
    models: list[str] = []
    raw = model_config.get("models")
    if isinstance(raw, list):
        models.extend(m for m in raw if isinstance(m, str) and m)
    default_model = model_config.get("default_model")
    if isinstance(default_model, str) and default_model:
        models.append(default_model)
    # dict.fromkeys de-duplicates while keeping the admin's preference order.
    return list(dict.fromkeys(models))


def _prompt_budget_policy(
    workspace: str, token: str, enabled_agents: dict[str, dict], state: dict
) -> dict | None:
    """Author a spend-routing ``budget_policy``, or None when the admin declines or can't.

    Budgets themselves are created in the Databricks console (they're account-level objects), so the
    admin picks an existing one here. Tiers are prompted in percent and stored as fractions, which is
    what the API validates.

    A tier's model choices come from what the admin configured for that agent earlier in this run —
    not the workspace catalog. Offering the catalog would let a tier point an agent at a model it
    wasn't given, which neither this validation nor the server's would reject: the tier would
    activate and hand the developer a model their agent doesn't have.
    """
    print_section("Budget policy")
    print_note(BUDGET_POLICY_BLURB)
    if not prompt_yes_no_default("Set up a budget policy for this workspace?", default=False):
        return None

    with spinner("Listing workspace budgets..."):
        budgets, reason = list_workspace_budgets(workspace, token)
    if reason is not None or not budgets:
        print_warning(
            "No AI Gateway budgets are visible for this workspace, so there is nothing to attach a "
            "policy to. Create a budget in the Databricks console first, then re-run `lucode setup`."
        )
        return None

    budget_id = prompt_for_selection(
        "Which budget should this policy track?",
        [
            (budget["id"], f"{budget['display_name'] or budget['id']} ({budget['id']})")
            for budget in budgets
        ],
        searchable=True,
    )
    if not budget_id:
        return None

    policy: dict = {"budget_id": budget_id}
    display_name = prompt_for_text("Policy name", default="coding-agents-tiered-routing")
    if display_name:
        policy["display_name"] = display_name

    tiers: list[dict] = []
    seen_percentages: set[float] = set()
    print_note(
        "Add one tier per step-down. Each tier activates once spend reaches its percentage, and "
        "the highest activated tier wins."
    )
    while True:
        index = len(tiers) + 1
        fraction = prompt_for_percentage(f"Tier {index}: activates at what percent of budget?")
        if fraction in seen_percentages:
            print_err("That percentage is already used by another tier; pick a different one.")
            continue
        agent = prompt_for_selection(
            f"Tier {index}: which agent becomes the default?",
            [(tool, TOOL_SPECS[tool]["display"]) for tool in enabled_agents],
        )
        if not agent:
            break
        # Only what this agent was actually configured with; the workspace catalog would offer
        # models the agent doesn't have.
        options = configured_models_for_agent(enabled_agents.get(agent) or {})
        if not options:
            options = model_options_for_agent(agent, state)
        if options:
            model = prompt_for_selection(
                f"Tier {index}: which model?", [(m, m) for m in options], searchable=True
            )
        else:
            model = prompt_for_text(f"Tier {index}: which model?")
        if not model:
            break
        seen_percentages.add(fraction)
        tiers.append(
            {
                "spending_percentage": fraction,
                "default_agent": agent,
                "default_model": model,
            }
        )
        if not prompt_yes_no_default("Add another tier?", default=False):
            break

    if tiers:
        policy["tiers"] = tiers
    return policy


def _render_summary(workspace: str, manifest: dict) -> None:
    """Print the authored config in a box so an admin can eyeball it before publishing.

    Boxed rather than printed as loose lines: this is the one block an admin is meant to read as a
    whole and check against what they intended, and it lands after a long flow of prompts.
    """
    lines: list[str] = [kv_line("Workspace", workspace)]
    default_agent = manifest.get("default_agent")
    if isinstance(default_agent, str):
        lines.append(
            kv_line(
                "Default agent", TOOL_SPECS.get(default_agent, {}).get("display", default_agent)
            )
        )

    for tool, agent_config in (manifest.get("enabled_agents") or {}).items():
        display = TOOL_SPECS.get(tool, {}).get("display", tool)
        model_config = agent_config.get("model_config") or {}
        detail = model_config.get("default_model") or "no model"
        scope = "machine-wide" if agent_config.get("use_as_global_settings") else "per-user"
        lines.append(kv_line(display, f"{detail} ({scope})"))
        # Spell out the model list beyond the one-line default when an admin picked more than one.
        models = model_config.get("models")
        if isinstance(models, list) and len(models) > 1:
            lines.append(kv_line("  models", ", ".join(str(m) for m in models)))

    mcp_servers = manifest.get("mcp_servers") or []
    lines.append(
        kv_line(
            "MCP servers",
            ", ".join(str(server.get("name")) for server in mcp_servers) if mcp_servers else "none",
        )
    )
    skills = (manifest.get("skills") or {}).get("names") or []
    lines.append(kv_line("Skills", ", ".join(skills) if skills else "none"))

    policy = manifest.get("budget_policy")
    if isinstance(policy, dict):
        tiers = policy.get("tiers") or []
        lines.append(
            kv_line("Budget policy", policy.get("display_name") or policy.get("budget_id") or "set")
        )
        for tier in tiers:
            agent = tier.get("default_agent")
            display = TOOL_SPECS.get(agent, {}).get("display", agent)
            percent = float(tier.get("spending_percentage", 0)) * 100
            lines.append(kv_line(f"  at {percent:g}%", f"{display} / {tier.get('default_model')}"))
    else:
        lines.append(kv_line("Budget policy", "none"))

    print_panel("Configuration summary", lines)


def _require_admin(workspace: str, token: str) -> None:
    """Stop unless the caller is a workspace admin.

    An unverifiable check (SCIM unreachable) warns and continues: the API enforces the same rule, so
    the worst case is a clear PERMISSION_DENIED at publish time rather than a false block here.
    """
    with spinner("Checking workspace admin permissions..."):
        admin = is_workspace_admin(workspace, token)
    if admin is False:
        raise RuntimeError(
            f"You are not an admin of {workspace}. `lucode setup` authors the workspace-wide "
            "coding config, so it is restricted to workspace admins."
        )
    if admin is None:
        print_warning(
            "Could not verify workspace admin permissions. Continuing — `lucode apply` will fail "
            "if you lack them."
        )
    else:
        print_success("Admin permissions verified")


def _handle_existing_config(workspace: str, token: str) -> bool:
    """Decide what to do when the workspace already has a published config.

    Returns True to keep authoring a new config (the wizard continues; publishing later replaces the
    existing one) and False to stop (no config exists, the check failed, or the admin chose to delete
    the existing one instead of authoring a replacement).

    Deliberately doesn't itemize what the existing config holds. The admin doesn't need an inventory
    to act on this — the instruction is the same either way ("include everything you want to keep")
    — and `lucode setup show` prints the real thing for anyone who wants to compare.
    """
    with spinner("Checking for an existing managed config..."):
        existing, reason = get_managed_config(workspace, token)
    if reason is not None:
        print_note(f"Could not check for an existing config: {reason}")
        return True
    if existing is None:
        return True

    print_warning(
        "This workspace already has a managed configuration — one config covers every agent, MCP "
        "server, skill, and budget policy for the whole workspace."
    )
    choice = prompt_for_selection(
        "What would you like to do?",
        [
            ("create", "Author a new config (replaces the existing one when you publish)"),
            ("delete", "Delete the existing config (removes it from the workspace, leaves none)"),
        ],
    )
    if choice is None:
        raise KeyboardInterrupt
    if choice == "create":
        print_note("Make sure this run includes everything you want to keep.")
        return True

    _delete_existing_config(workspace, token, existing)
    return False


def _delete_existing_config(workspace: str, token: str, existing: dict) -> None:
    """Delete the workspace's published config after confirming. Raises RuntimeError on failure.

    Deleting leaves the workspace with no managed config, so every developer falls back to their own
    settings on their next lucode run — confirm before doing it, and honor ``--dry-run``.
    """
    name = existing.get("name")
    if not isinstance(name, str):
        raise RuntimeError(
            "This workspace has a managed config but the API didn't return its resource name, so "
            "lucode can't delete it. Delete it in the workspace directly."
        )
    print_warning(
        "Deleting removes the managed config entirely. Every developer falls back to their own "
        "settings on their next lucode run."
    )
    if not prompt_yes_no_default("Delete the existing managed config?", default=False):
        print_note("Nothing was deleted.")
        return
    if is_dry_run():
        print_success("Dry run: the config was not deleted.")
        return
    with spinner("Deleting the managed config..."):
        delete_reason = delete_coding_agent_config(workspace, token, name)
    if delete_reason is not None:
        raise RuntimeError(f"Could not delete the managed config on {workspace}: {delete_reason}.")
    print_success(f"Deleted the managed config from {workspace}")


def setup_from_file(path: str) -> int:
    """Validate an admin-written manifest and save it, skipping the interactive flow.

    The non-interactive path for CI and for admins who'd rather keep the JSON in version control.
    Reads lucode's own manifest shape (the same thing the wizard writes), not proto-JSON.
    """
    manifest_path = Path(path).expanduser()
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read manifest file: {manifest_path}") from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{manifest_path} is not valid JSON: {exc.msg} (line {exc.lineno})."
        ) from None
    if not isinstance(manifest, dict):
        raise RuntimeError(f"{manifest_path} must contain a JSON object.")

    state = load_state()
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError(
            "No workspace is configured. Run `lucode configure` first so lucode knows which "
            "workspace this manifest is for."
        )

    errors = validate_manifest(manifest, state)
    if errors:
        print_err(f"{manifest_path} is not a valid managed config:")
        for error in errors:
            print_note(error)
        return 1

    save_managed_settings(workspace, manifest)
    _render_summary(workspace, manifest)
    print_success(f"Saved to {manifest_path.name} -> ~/.lucode/managed-settings.json")
    _print_next_steps()
    return 0


def _print_next_steps() -> None:
    console.print()
    print_heading("Next steps")
    # Deliberately only `apply`. There is no way yet to try the authored config locally: the
    # manifest describes what developers should get, while `lucode configure --dry-run` previews
    # this machine's own agent configs, so pointing at it implied a local test it doesn't perform.
    print_note("Publish it to the workspace:  lucode apply")


def setup_command(
    from_file: str | None = None,
    *,
    prompt_for_configuration: Callable[[], tuple[str, str | None]] | None = None,
    configure_state: Callable[..., dict] | None = None,
) -> int:
    """Author the workspace's managed coding-agent config interactively.

    Returns a process exit code. Raises RuntimeError for actionable failures (not an admin, no
    agents available) and KeyboardInterrupt when the admin aborts a picker; the CLI maps both.
    """
    if from_file is not None:
        return setup_from_file(from_file)

    if prompt_for_configuration is None or configure_state is None:
        raise RuntimeError("Interactive setup requires CLI configuration callbacks.")

    print_section("lucode setup")
    print_note("Author the managed coding config for this workspace.")
    print_note("Developers pull it automatically when they run lucode.")

    workspace, profile = prompt_for_configuration()
    # `configure_shared_state` below authenticates too and prints its own success line, so this one
    # stays quiet rather than reporting the same thing twice. It still has to run first: the admin
    # gate and the existing-config check both need a token before discovery.
    ensure_databricks_auth(workspace, profile, quiet=True)
    token = get_databricks_token(workspace, profile)

    _require_admin(workspace, token)
    if not _handle_existing_config(workspace, token):
        return 0

    # Discover the workspace's models and gateway URLs. This also logs in and persists local state,
    # which is what lets the admin dry-run the config on their own machine afterwards.
    state = configure_state(workspace, profile=profile, force_login=False)
    workspace = state.get("workspace") or workspace
    profile = state.get("profile") or profile

    available = [tool for tool in TOOL_SPECS if check_gateway_endpoint(state, tool)]
    if not available:
        raise RuntimeError(
            f"No coding agents are available on {workspace}. Check that the workspace's AI Gateway "
            "serves models for at least one agent."
        )

    previous = load_managed_settings(workspace) or {}
    previously_enabled = [
        tool for tool in (previous.get("enabled_agents") or {}) if tool in TOOL_SPECS
    ]
    picked = prompt_for_tools(
        [(tool, TOOL_SPECS[tool]["display"]) for tool in available],
        preselected=previously_enabled or None,
    )
    if not picked:
        print_note("No coding agents selected — nothing to configure.")
        return 0

    default_agent = picked[0]
    if len(picked) > 1:
        chosen = prompt_for_selection(
            "Which agent should launch when a developer runs `lucode`?",
            [(tool, TOOL_SPECS[tool]["display"]) for tool in picked],
        )
        if not chosen:
            raise KeyboardInterrupt
        default_agent = chosen
    print_success(f"Default agent set to {TOOL_SPECS[default_agent]['display']}")

    enabled_agents: dict[str, dict] = {}
    for tool in picked:
        print_heading(TOOL_SPECS[tool]["display"])
        # Always set: `_prompt_models_for_agent` re-prompts rather than returning empty, so every
        # enabled agent carries a default_model and any of them can be the default_agent.
        agent_config: dict = {"model_config": _prompt_models_for_agent(tool, state)}
        agent_config["use_as_global_settings"] = prompt_yes_no_default(
            f"Apply {TOOL_SPECS[tool]['display']} config machine-wide? ({GLOBAL_SETTINGS_BLURB})",
            default=False,
        )
        enabled_agents[tool] = agent_config

    manifest: dict = {"default_agent": default_agent, "enabled_agents": enabled_agents}

    print_section("MCP servers")
    if prompt_yes_no_default("Set up managed MCP servers for this workspace?", default=False):
        configure_mcp_command()
        mcp_servers = _mcp_servers_from_state(load_state())
        if mcp_servers:
            manifest["mcp_servers"] = mcp_servers
            print_success(f"{len(mcp_servers)} MCP server(s) added to the managed config")

    print_section("Skills")
    if prompt_yes_no_default("Set up managed skills for this workspace?", default=False):
        locations = prompt_for_text(
            "Skill schemas to publish, comma-separated `catalog.schema` (blank to skip)",
            default="",
        )
        parsed: list[str] = [item.strip() for item in (locations or "").split(",") if item.strip()]
        if parsed:
            configure_skills_mcp_command(parsed)
            skill_names = _skill_names_from_state(load_state()) or parsed
            manifest["skills"] = {"names": skill_names}
            print_success(f"{len(skill_names)} skill schema(s) added to the managed config")

    budget_policy = _prompt_budget_policy(workspace, token, enabled_agents, state)
    if budget_policy:
        manifest["budget_policy"] = budget_policy

    errors = validate_manifest(manifest, state)
    if errors:
        # A validation failure here is a wizard bug, not admin error — the pickers only offer valid
        # choices. Surface it plainly rather than writing a manifest that `apply` would reject.
        print_err("The generated config is not valid:")
        for error in errors:
            print_note(error)
        return 1

    save_managed_settings(workspace, manifest)
    _render_summary(workspace, manifest)
    console.print()
    print_success("Saved to ~/.lucode/managed-settings.json")
    _print_next_steps()
    return 0


def show_command() -> int:
    """Print the authored manifest and the proto-JSON `lucode apply` would publish."""
    workspace = load_state().get("workspace")
    manifest = load_managed_settings(workspace)
    if manifest is None:
        print_note("No managed config has been authored yet. Run `lucode setup` to create one.")
        return 0
    _render_summary(workspace or "unknown", manifest)
    console.print()
    print_heading("Payload for `lucode apply`")
    console.print(json.dumps(serialize_managed_config(manifest), indent=2))
    return 0


# Server-side failures an admin is actually likely to hit, mapped to something they can act on. The
# raw reasons are `HTTP <code> <reason>: <body>` strings from the transport, and the body carries the
# API's `error_code`, so matching on that is more robust than on status codes alone.
def _explain_publish_failure(reason: str) -> str:
    lowered = reason.lower()
    if "feature_disabled" in lowered:
        return (
            "Managed coding-agent configs aren't enabled on this workspace yet. Ask your Databricks "
            "contact to enable the `codingAgentConfigCrudEnabled` flag for it, then re-run "
            "`lucode apply`."
        )
    if "permission_denied" in lowered or "http 403" in lowered:
        return (
            "Publishing a managed config requires workspace admin. Your account can read the "
            "workspace but not author its coding config."
        )
    if "already_exists" in lowered:
        return (
            "This workspace already has a managed config, but lucode couldn't read it to update in "
            "place. Run `lucode apply` again — if it keeps failing, the existing config may need to "
            "be deleted by hand."
        )
    if "invalid_parameter_value" in lowered:
        # The server names the offending field; passing it through beats paraphrasing.
        return f"The workspace rejected the config: {reason}"
    return f"Could not publish the managed config: {reason}"


def apply_command(
    *,
    yes: bool = False,
    prompt_for_configuration: Callable[[], tuple[str, str | None]] | None = None,
) -> int:
    """Publish the authored manifest to the workspace.

    Updates the existing config in place when there is one, rather than deleting and recreating it:
    a failed recreate would leave the workspace with no managed config at all, and every developer
    would silently fall back to their own settings. Returns a process exit code.
    """
    print_section("lucode apply")

    state = load_state()
    workspace = state.get("workspace")
    profile = state.get("profile")
    if not workspace:
        if prompt_for_configuration is None:
            raise RuntimeError(
                "Apply requires a CLI configuration callback when no workspace exists."
            )
        workspace, profile = prompt_for_configuration()

    manifest = load_managed_settings(workspace)
    if manifest is None:
        raise RuntimeError(
            "No managed config has been authored for this workspace. Run `lucode setup` first "
            "(or `lucode setup --from-file <json>`)."
        )

    # Auth first: publishing needs a token, and nothing is written until well below this point.
    ensure_databricks_auth(workspace, profile)

    errors = validate_manifest(manifest, state)
    if errors:
        print_err("The authored config is not valid, so it was not published:")
        for error in errors:
            print_note(error)
        print_note("Re-run `lucode setup` to fix it, or edit ~/.lucode/managed-settings.json.")
        return 1

    token = get_databricks_token(workspace, profile)
    _require_admin(workspace, token)

    payload = serialize_managed_config(manifest)
    _render_summary(workspace, manifest)

    # Read before writing: the resource name tells us whether to create or update, and shows the
    # admin what they are about to overwrite.
    with spinner("Checking for an existing managed config..."):
        existing, reason = get_managed_config(workspace, token)
    if reason is not None:
        raise RuntimeError(
            f"Could not check whether {workspace} already has a managed config: {reason}. "
            "Refusing to publish without knowing, since that could overwrite a config silently."
        )

    existing_name = (existing or {}).get("name")
    if existing is not None and not isinstance(existing_name, str):
        raise RuntimeError(
            "This workspace has a managed config but the API didn't return its resource name, so "
            "lucode can't update it in place. Delete it in the workspace and re-run `lucode apply`."
        )

    console.print()
    if existing is None:
        print_note(f"This will create a new managed config on {workspace}.")
    else:
        agents = ", ".join((existing.get("enabled_agents") or {}).keys()) or "no agents"
        print_warning(
            f"This will replace the config already published on {workspace} (currently: {agents}). "
            "Every developer picks the new one up on their next lucode run."
        )
    if not yes and not prompt_yes_no_default("Publish this config?", default=False):
        print_note("Nothing was published.")
        return 1

    if existing is None:
        with spinner("Publishing the managed config..."):
            published, publish_reason = create_coding_agent_config(workspace, token, payload)
    else:
        with spinner("Updating the managed config..."):
            published, publish_reason = update_coding_agent_config(
                workspace, token, cast("str", existing_name), payload
            )
    if publish_reason is not None:
        raise RuntimeError(_explain_publish_failure(publish_reason))

    name = (published or {}).get("name") or existing_name or "coding-agent-configs/?"
    print_success(f"Published {name} to {workspace}")
    print_note("Developers pick this up on their next lucode run.")
    return 0


__all__ = ["apply_command", "setup_command", "setup_from_file", "show_command"]
