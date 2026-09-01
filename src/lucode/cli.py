#!/usr/bin/env python3
"""CLI entry point for lucode."""

from __future__ import annotations

import sys
from typing import Annotated

import typer
from rich.panel import Panel

from lucode.agents import (
    TOOL_SPECS,
    check_gateway_endpoint,
    configure_selected_tools,
    configure_single_tool,
    configure_tool,
    ensure_bootstrap_dependencies,
    ensure_provider_state,
    install_tool_binary,
    normalize_tool,
    resolve_launch_model,
    validate_all_tools,
    validate_tool,
)
from lucode.agents import (
    launch as launch_agent,
)
from lucode.agents.pi import PI_SETTINGS_BACKUP_PATH, PI_SETTINGS_PATH
from lucode.config import restore_file, set_dry_run
from lucode.databricks.auth import (
    apply_pat_environment,
    ensure_databricks_auth,
    ensure_pat_bearer,
    find_profile_name_for_host,
    get_databricks_profiles,
    get_databricks_token,
    install_databricks_cli,
    list_profile_entries,
    resolve_pat_token,
    run_databricks_login,
)
from lucode.databricks.models import (
    build_shared_base_urls,
    discover_claude_models,
    discover_codex_models,
    discover_gemini_models,
    discover_model_services,
    ensure_ai_gateway_v2,
)
from lucode.fetch import configure_fetch_command
from lucode.mcp.commands import configure_mcp_command
from lucode.mcp.config import MCP_CLIENTS, purge_cross_workspace_mcp_residue, revert_mcp_configs
from lucode.mcp.skills import SKILLS_MCP_KIND, configure_skills_mcp_command
from lucode.state import (
    STATE_PATH,
    clear_state,
    load_state,
    save_state,
    set_current_workspace,
)
from lucode.ui import (
    console,
    heading,
    normalize_workspace_url,
    print_err,
    print_heading,
    print_kv,
    print_note,
    print_section,
    print_success,
    print_warning,
    prompt_for_tools,
    prompt_for_workspace,
    prompt_yes_no,
    prompt_yes_no_default,
    set_verbosity,
    spinner,
    status_badge,
)

_DISCOVERY_CONSUMERS: dict[str, tuple[str, ...]] = {
    "claude": ("opencode", "pi"),
    "codex": ("pi",),
    "gemini": ("opencode", "pi"),
    "oss": ("opencode",),
}


def _print_discovery_diagnostics(state: dict) -> None:
    """Surface per-source reasons after a failed discovery so the user knows
    which API call returned what — instead of the generic 'no agents' line."""
    reasons = state.get("_discovery_reasons") or {}
    if not reasons:
        return
    labels = {
        "claude": "Anthropic models",
        "codex": "OpenAI Responses models",
        "gemini": "Gemini models",
        "oss": "OSS models",
    }
    for source, reason in reasons.items():
        consumers = ", ".join(_DISCOVERY_CONSUMERS.get(source, ()))
        label = labels.get(source, source)
        if reason:
            print_note(f"{label} (needed for: {consumers}): {reason}")
        else:
            print_note(f"{label} (needed for: {consumers}): no models returned")
    print_note(
        "Re-run with `lucode_DEBUG=1` to log raw discovery responses to ~/.lucode/debug.log."
    )


def _prompt_for_configuration(tool: str | None = None) -> tuple[str, str | None]:
    if tool is None:
        desc = "Configure your Databricks workspace"
    else:
        desc = f"Configure {TOOL_SPECS[tool]['display']} to use your Databricks endpoint."
    with spinner("Loading Databricks workspaces and profiles..."):
        profiles = get_databricks_profiles()
    return prompt_for_workspace(desc, profiles)


def _parse_agents_option(agents: str) -> list[str]:
    tools: list[str] = []
    for raw_tool in agents.split(","):
        raw_tool = raw_tool.strip()
        if not raw_tool:
            continue
        tool = normalize_tool(raw_tool)
        if tool not in tools:
            tools.append(tool)
    if not tools:
        raise RuntimeError(
            "No agents provided for --agents. Use a comma-separated list like `--agents opencode,pi`."
        )
    return tools


def _parse_skill_locations(location: str | None) -> list[str]:
    """Parse a comma-separated `--location` into `<catalog>.<schema>` refs,
    dropping duplicates while preserving order. `None`/empty yields `[]` (the
    schema-less, utility-tools-only connection)."""
    locations: list[str] = []
    for raw in (location or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(".")
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise RuntimeError(f"--location entries must be `<catalog>.<schema>`, got `{raw}`.")
        if raw not in locations:
            locations.append(raw)
    return locations


def _parse_workspaces_option(workspaces: str) -> list[tuple[str, str | None]]:
    """Parse `--workspaces` into [(url, profile_name | None), ...].

    `--workspaces` supplies bare URLs; the matching profile (if any) is
    resolved later via `find_profile_name_for_host`.
    """
    workspace_entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for raw_workspace in workspaces.split(","):
        raw_workspace = raw_workspace.strip()
        if not raw_workspace:
            continue
        try:
            workspace = normalize_workspace_url(raw_workspace)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if workspace not in seen:
            seen.add(workspace)
            workspace_entries.append((workspace, None))
    if not workspace_entries:
        raise RuntimeError(
            "No workspaces provided for --workspaces. Use a comma-separated list like "
            "`--workspaces https://workspace.databricks.com`."
        )
    return workspace_entries


def _parse_profiles_option(profiles: str) -> list[tuple[str, str | None]]:
    """Parse `--profiles` into [(url, profile_name), ...].

    Each name must be an existing Databricks CLI profile; its host supplies
    the workspace URL. Auth behaves the same as `--workspaces`: OAuth login is
    forced unless `--use-pat` is also passed."""
    available = {str(p.get("name")): p for p in list_profile_entries() if p.get("name")}
    workspace_entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for raw_name in profiles.split(","):
        name = raw_name.strip()
        if not name:
            continue
        entry = available.get(name)
        if entry is None:
            known = ", ".join(sorted(available)) or "none"
            raise RuntimeError(
                f"Databricks CLI profile '{name}' was not found (available: {known}). "
                "Check `databricks auth profiles` or add the profile to ~/.databrickscfg."
            )
        host = str(entry.get("host") or "").strip()
        if not host:
            raise RuntimeError(
                f"Databricks CLI profile '{name}' has no host configured in ~/.databrickscfg."
            )
        try:
            workspace = normalize_workspace_url(host)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if workspace not in seen:
            seen.add(workspace)
            workspace_entries.append((workspace, name))
    if not workspace_entries:
        raise RuntimeError(
            "No profiles provided for --profiles. Use a comma-separated list like "
            "`--profiles DEFAULT`."
        )
    return workspace_entries


def configure_shared_state(
    workspace: str,
    profile: str | None = None,
    tools: list[str] | None = None,
    force_login: bool = False,
    use_pat: bool | None = None,
    skip_model_discovery: bool = False,
    skip_preflight: bool = False,
    databricks_ai_tools_enabled: bool | None = None,
) -> dict:
    """Log into Databricks, enforce AI Gateway v2, fetch model lists, persist state.

    If tools is provided, only fetch models for those tools. Otherwise fetch all.
    If force_login is True, always run databricks auth login (used by explicit configure).
    If use_pat is True (explicit `configure --profiles <name> --use-pat`), the
    profile's personal access token from ~/.databrickscfg is used instead of
    OAuth and no interactive login ever runs. ``None`` means "inherit": a
    launch re-run keeps the mode the workspace was configured with.
    ``profile`` is the Databricks CLI profile name to address — passed via
    ``--profile`` to every CLI invocation so ambiguous `~/.databrickscfg`
    entries (e.g. DEFAULT and a named profile both pointing at the same host)
    don't error out. If ``None``, we resolve it from the host after login.
    If skip_preflight is True, skip the entire preflight block below — auth
    validation, the AI Gateway probe, and model discovery — trusting a prior
    ``lucode configure``. The PAT/bearer is already exported (``apply_pat_environment``
    in ``_launch_tool``) and the gateway was verified by that earlier configure.
    Only the local profile resolution and the shared state assembly still run;
    the saved model lists are preserved.
    """
    workspace = normalize_workspace_url(workspace)
    prior_state = load_state()
    previous_workspace = prior_state.get("workspace")
    if use_pat is None:
        use_pat = bool(prior_state.get("use_pat")) and previous_workspace == workspace
    if databricks_ai_tools_enabled is None:
        # Opt-out: on by default. With no flag, keep this workspace's prior
        # choice but don't inherit another workspace's opt-out.
        disabled = (
            prior_state.get("databricks_ai_tools_enabled") is False
            and previous_workspace == workspace
        )
        databricks_ai_tools_enabled = not disabled
    fetch_all = tools is None

    # Assemble the shared workspace state that doesn't depend on model discovery:
    # workspace, profile, auth mode, base URLs. `profile` may still be None here;
    # each path below resolves it once, where a host->profile lookup is reliable
    # (the skip branch trusts the prior configure; the preflight resolves after
    # login). --skip-preflight persists exactly this and returns, trusting a prior
    # `lucode configure` — it already validated auth + the AI Gateway and saved the
    # model lists (carried over by load_state, left untouched).
    state = load_state()
    state["workspace"] = workspace
    if profile:
        state["profile"] = profile
    else:
        state.pop("profile", None)
    # UC discovery is now always-on; drop any flag persisted by older versions.
    state.pop("uc_enabled", None)
    # Persist the auth mode so launches rebuild the same (PAT-based) agent
    # auth command; an explicit re-configure without --use-pat clears it.
    if use_pat:
        state["use_pat"] = True
    else:
        state.pop("use_pat", None)
    # Fable is not supported by either surviving harness.
    state.pop("fable_enabled", None)
    state["databricks_ai_tools_enabled"] = databricks_ai_tools_enabled
    state["base_urls"] = build_shared_base_urls(workspace)

    if skip_preflight:
        # A prior `lucode configure` created the profile; resolve it locally (no
        # login needed) and persist it so launches disambiguate.
        if profile is None:
            profile = find_profile_name_for_host(workspace)
            if profile:
                state["profile"] = profile
        save_state(state)
        # Scrub MCP entries lucode wrote for a previous workspace.
        if previous_workspace and previous_workspace != workspace:
            purge_cross_workspace_mcp_residue(state, workspace)
        # Diagnostic reasons are transient (attached after save_state so they
        # don't land on disk). No discovery ran, so there is nothing to report.
        state["_discovery_reasons"] = {"claude": None, "gemini": None, "codex": None, "oss": None}
        return state

    # ── Preflight (bypassed above under --skip-preflight): validate Databricks
    #    auth + the AI Gateway, then discover the available models. ──
    if use_pat:
        if not profile:
            raise RuntimeError(
                "--use-pat requires a Databricks CLI profile. Pass one via `--profiles <name>`."
            )
        pat = resolve_pat_token(profile)
        if not pat:
            raise RuntimeError(
                f"--use-pat: profile '{profile}' has no personal access token in "
                "~/.databrickscfg (its auth_type must be `pat`). Add a `token = <PAT>` "
                f"entry under [{profile}], or re-run without --use-pat to use OAuth."
            )
        # Export the PAT for this process and launched agent subprocesses so
        # every token fetch takes the static-bearer path. ensure_pat_bearer
        # keeps a non-empty pre-set bearer (CI escape hatch) but treats an
        # empty one as absent, so it never shadows the PAT. Pass the validated
        # token to avoid re-reading ~/.databrickscfg.
        ensure_pat_bearer(profile, pat)
        ensure_databricks_auth(workspace, profile)
    elif force_login:
        run_databricks_login(workspace, profile)
    else:
        ensure_databricks_auth(workspace, profile)
    # After login the profile exists in ~/.databrickscfg, so a host->profile
    # lookup is reliable even when it returned nothing above.
    if profile is None:
        profile = find_profile_name_for_host(workspace)
        if profile:
            state["profile"] = profile
    with spinner("Verifying Unity AI Gateway..."):
        token = get_databricks_token(workspace, profile)
        ensure_ai_gateway_v2(workspace, token)
    print_success("Unity AI Gateway detected")

    want_claude = fetch_all or "opencode" in tools or "pi" in tools
    want_gemini = fetch_all or "opencode" in tools or "pi" in tools
    want_codex = fetch_all or "pi" in tools
    want_oss = fetch_all or "opencode" in tools

    claude_reason: str | None = None
    gemini_reason: str | None = None
    codex_reason: str | None = None
    oss_reason: str | None = None
    claude_models = {}
    gemini_models = []
    codex_models = []
    oss_models = []
    opencode_models: dict[str, list[str]] = {}
    model_services_partial = False
    persisted_model_keys = (
        "claude_models",
        "gemini_models",
        "codex_models",
        "oss_models",
        "opencode_models",
    )
    prior_model_values = {key: state[key] for key in persisted_model_keys if key in state}
    if not skip_model_discovery:
        # UC-first, best-effort: one UC model-services call yields all families
        # as `system.ai.<model-name>` ids, bucketed by name. If a family comes
        # back empty (workspace without UC model-services, or the listing
        # failed), fall back to the per-family AI Gateway listing for that
        # family only.
        with spinner("Fetching available models..."):
            ms_claude, ms_codex, ms_gemini, ms_oss, ms_reason = discover_model_services(
                workspace, token
            )
            model_services_partial = bool(
                ms_reason and (ms_claude or ms_codex or ms_gemini or ms_oss)
            )
            if want_claude:
                claude_models, claude_reason = ms_claude, ms_reason
                if not claude_models:
                    claude_models, claude_reason = discover_claude_models(workspace, token)
                claude_models.pop("fable", None)
            if want_gemini:
                gemini_models, gemini_reason = ms_gemini, ms_reason
                if not gemini_models:
                    gemini_models, gemini_reason = discover_gemini_models(workspace, token)
            if want_codex:
                codex_models, codex_reason = ms_codex, ms_reason
                if not codex_models:
                    codex_models, codex_reason = discover_codex_models(workspace, token)
            if want_oss:
                oss_models, oss_reason = ms_oss, ms_reason
        if claude_models:
            opencode_models["anthropic"] = list(claude_models.values())
        if gemini_models:
            opencode_models["gemini"] = gemini_models
        if oss_models:
            opencode_models["oss"] = oss_models
        if model_services_partial:
            print_warning(
                f"Model-service discovery was incomplete: {ms_reason}. "
                "Using partial results for this run without replacing saved model lists."
            )

    if not skip_model_discovery:
        if want_claude:
            state["claude_models"] = claude_models
        if want_gemini:
            state["gemini_models"] = gemini_models
        if want_codex:
            state["codex_models"] = codex_models
        if want_oss:
            state["oss_models"] = oss_models
        if fetch_all or "opencode" in tools:
            state["opencode_models"] = opencode_models
    if model_services_partial:
        state_to_save = state.copy()
        for key in persisted_model_keys:
            if key in prior_model_values:
                state_to_save[key] = prior_model_values[key]
            else:
                state_to_save.pop(key, None)
        save_state(state_to_save)
    else:
        save_state(state)
    # Scrub MCP entries that lucode wrote for the previous workspace so the new
    # workspace's agent configs aren't stale.
    if previous_workspace and previous_workspace != workspace:
        purge_cross_workspace_mcp_residue(state, workspace)
    # Diagnostic reasons are transient — attach after save_state so they don't
    # land on disk but are available to the caller for this run.
    state["_discovery_reasons"] = {
        "claude": claude_reason,
        "gemini": gemini_reason,
        "codex": codex_reason,
        "oss": oss_reason,
    }
    return state


def _configure_shared_workspace_states(
    workspaces: list[tuple[str, str | None]],
    tools: list[str] | None,
    *,
    force_login: bool,
    use_pat: bool = False,
    databricks_ai_tools_enabled: bool | None = None,
) -> list[dict]:
    if not workspaces:
        raise RuntimeError("At least one workspace must be provided.")
    states: list[dict] = []
    for workspace, profile in workspaces:
        states.append(
            configure_shared_state(
                workspace,
                profile=profile,
                tools=tools,
                force_login=force_login,
                use_pat=use_pat,
                databricks_ai_tools_enabled=databricks_ai_tools_enabled,
            )
        )
    return states


def configure_workspace_command(
    tool: str | None = None,
    selected_tools: list[str] | None = None,
    workspaces: list[tuple[str, str | None]] | None = None,
    *,
    prompt_optional_updates: bool = True,
    use_pat: bool = False,
    skip_validate: bool = False,
    databricks_ai_tools_enabled: bool | None = None,
) -> int:
    if tool is not None and selected_tools is not None:
        raise RuntimeError("Use either --agent or --agents, not both.")

    workspace_entries = workspaces or [_prompt_for_configuration(tool)]

    if tool is not None:
        states = _configure_shared_workspace_states(
            workspace_entries,
            [tool],
            force_login=True,
            use_pat=use_pat,
            databricks_ai_tools_enabled=databricks_ai_tools_enabled,
        )
        state = states[0]
        state = configure_single_tool(tool, state)
        spec = TOOL_SPECS[tool]
        console.print(
            Panel(
                f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]\n"
                f"[bold]{spec['display']}:[/bold] [green]configured[/green] "
                "[dim](Provider: Databricks)[/dim]",
                title="Configuration Complete",
                style="green",
                expand=False,
            )
        )
        if skip_validate:
            print_note(f"Skipping {spec['display']} validation (--skip-validate).")
            return 0
        with spinner(f"Validating {spec['display']}..."):
            ok, err = validate_tool(tool)
        if ok:
            print_success(f"{spec['display']} is working")
        else:
            print_err(f"{spec['display']}: {err}")
            managed = bool(state.get("managed_configs", {}).get(tool))
            restore_file(spec["config_path"], spec["backup_path"], managed)
            available_tools = [t for t in (state.get("available_tools") or []) if t != tool]
            state["available_tools"] = available_tools
            save_state(state)
            raise RuntimeError(f"{spec['display']} validation failed — config reverted.")
        return 0

    states = _configure_shared_workspace_states(
        workspace_entries,
        selected_tools,
        force_login=True,
        use_pat=use_pat,
        databricks_ai_tools_enabled=databricks_ai_tools_enabled,
    )
    state = states[0]
    save_state(state)

    available_on_workspace: list[str] = []
    tools_to_check = selected_tools or list(TOOL_SPECS)
    for tool_name in tools_to_check:
        with spinner(f"Checking {TOOL_SPECS[tool_name]['display']} availability..."):
            if check_gateway_endpoint(state, tool_name):
                available_on_workspace.append(tool_name)

    if not available_on_workspace:
        print_err("No coding agents are available on this workspace.")
        _print_discovery_diagnostics(state)
        return 1

    if selected_tools is None:
        picked = prompt_for_tools([(t, TOOL_SPECS[t]["display"]) for t in available_on_workspace])
    else:
        unavailable_tools = [
            tool_name for tool_name in selected_tools if tool_name not in available_on_workspace
        ]
        if unavailable_tools:
            _print_discovery_diagnostics(state)
            displays = ", ".join(
                TOOL_SPECS[tool_name]["display"] for tool_name in unavailable_tools
            )
            raise RuntimeError(f"Requested agent(s) not available on this workspace: {displays}.")
        picked = selected_tools

    if not picked:
        print_note("No coding agents selected — nothing to configure.")
        return 0

    for tool_name in picked:
        install_tool_binary(
            tool_name,
            strict=False,
            update_existing=True,
            prompt_optional_updates=prompt_optional_updates,
        )

    # Last question in the interactive flow: opt out of AI Tools. When a flag
    # already decided it, configure_shared_state persisted that; skip the prompt.
    # The default is the resolved prior choice, so Enter won't undo a past opt-out.
    if databricks_ai_tools_enabled is None and selected_tools is None:
        state["databricks_ai_tools_enabled"] = prompt_yes_no_default(
            "Install Databricks AI Tools for your coding agents? "
            "This adds Databricks skills and plugins.",
            default=state.get("databricks_ai_tools_enabled", True),
        )

    state = configure_selected_tools(state, picked)

    summary_lines = [f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]"]
    for tool_name in picked:
        spec = TOOL_SPECS[tool_name]
        summary_lines.append(
            f"[bold]{spec['display']}:[/bold] [green]configured[/green] "
            "[dim](Provider: Databricks)[/dim]"
        )
    console.print(
        Panel(
            "\n".join(summary_lines),
            title="Configuration Complete",
            style="green",
            expand=False,
        )
    )

    if skip_validate:
        print_note("Skipping agent validation (--skip-validate).")
        return 0
    # Limit validation to just-configured tools so we don't re-validate
    # previously-configured tools the user didn't touch this run.
    validate_state = {**state, "available_tools": picked}
    validate_all_tools(validate_state)
    return 0


def status() -> int:
    state = load_state()
    workspace = state.get("workspace")
    managed_configs = state.get("managed_configs") or {}
    mcp_servers = state.get("mcp_servers") or []
    configured_tools = set(state.get("available_tools") or managed_configs.keys())

    console.print(heading("lucode status"))
    console.print(
        f"  {status_badge('Configured', 'ok') if workspace else status_badge('Not Configured', 'warn')}"
    )

    print_heading("Provider")
    print_kv("Workspace URL", workspace or "not configured")
    profile = state.get("profile")
    if profile:
        print_kv("CLI profile", profile)

    print_heading("Coding Agents")
    for tool, spec in TOOL_SPECS.items():
        configured = tool in configured_tools
        base_url = (
            state.get("base_urls", {}).get(tool, "not configured")
            if configured
            else "not configured"
        )
        config_path = spec["config_path"]
        print_kv("Coding Agent", spec["display"])
        print_kv("Configured", "yes" if configured else "no")
        print_kv("Base URL", base_url)
        if configured and tool in MCP_CLIENTS:
            tool_mcp_servers = [
                str(server.get("name"))
                for server in mcp_servers
                if tool in (server.get("clients") or [])
                and server.get("name")
                and server.get("kind") != SKILLS_MCP_KIND
            ]
            print_kv("MCP list command", str(MCP_CLIENTS[tool]["list_command"]))
            print_kv(
                "MCP servers",
                ", ".join(tool_mcp_servers) if tool_mcp_servers else "none saved by lucode",
            )
        print_kv("Config file", str(config_path) if config_path.exists() else "missing")
        console.print()

    print_heading("Skills")
    skill_mcp_entry = next((s for s in mcp_servers if s.get("kind") == SKILLS_MCP_KIND), None)
    if not skill_mcp_entry:
        print_kv("Skills", "not configured")
    else:
        locations = skill_mcp_entry.get("skill_locations") or []
        print_kv(
            "Skill MCP Locations",
            ", ".join(locations) if locations else "none — utility tools only",
        )
        configured_agents = [
            str(MCP_CLIENTS[client]["display"])
            for client in (skill_mcp_entry.get("clients") or [])
            if client in MCP_CLIENTS
        ]
        print_kv("Configured", ", ".join(configured_agents) if configured_agents else "none")

    print_heading("State")
    print_kv("State file", str(STATE_PATH) if STATE_PATH.exists() else "missing")
    print_note("Use `lucode configure` to update workspace settings or configure new tools.")
    print_note(
        "Use `lucode configure mcp` to add Databricks MCP servers to configured coding tools."
    )
    print_note(
        "Use `lucode configure skills` to set up Unity Catalog Skills for configured coding tools."
    )
    print_note("Use `lucode revert` to clear lucode configs and restore prior files.")
    return 0


def revert() -> int:
    state = load_state()
    managed_configs = state.get("managed_configs") or {}
    mcp_results = revert_mcp_configs(state)

    results: dict[str, bool] = {
        tool: restore_file(
            spec["config_path"], spec["backup_path"], bool(managed_configs.get(tool))
        )
        for tool, spec in TOOL_SPECS.items()
    }
    pi_settings_restored = restore_file(
        PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH, bool(managed_configs.get("pi"))
    )
    clear_state()

    print_heading("Revert")
    print_kv("Workspace", state.get("workspace") or "none")
    for tool, spec in TOOL_SPECS.items():
        print_kv(f"{spec['display']} config", "restored" if results[tool] else "unchanged")
    print_kv("Pi settings", "restored" if pi_settings_restored else "unchanged")
    for client, spec in MCP_CLIENTS.items():
        print_kv(
            f"{spec['display']} MCP config",
            "restored" if mcp_results.get(client) else "unchanged",
        )
    print_success("lucode state cleared")
    return 0


# ---------------------------------------------------------------------------
# typer app
# ---------------------------------------------------------------------------


app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
configure_app = typer.Typer(add_completion=False, no_args_is_help=False)
app.add_typer(configure_app, name="configure", help="Configure workspace and tool settings.")
mcp_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(mcp_app, name="mcp", help="MCP servers exposed by lucode.")
prompts_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(prompts_app, name="prompts", help="Manage revisioned shared prompts.")


@app.command("init")
def init_cmd(
    extensions: Annotated[bool | None, typer.Option("--extensions/--no-extensions")] = None,
    project_trust: Annotated[
        bool | None, typer.Option("--project-trust/--no-project-trust")
    ] = None,
    revert: Annotated[bool, typer.Option("--revert")] = False,
) -> None:
    """Initialize append-only Pi preferences with explicit security consent."""
    from lucode.bootstrap import initialize
    from lucode.bootstrap import revert as revert_init
    from lucode.parameters import pi_settings_packages

    if revert:
        revert_init()
        print_success("Initialization-owned settings reverted")
        return
    packages = pi_settings_packages()
    if extensions is None:
        console.print("Pi extensions:\n" + "\n".join(f"  {package}" for package in packages))
        extensions = prompt_yes_no_default("Add these Pi extensions?", default=False)
    if project_trust is None:
        project_trust = prompt_yes_no_default(
            "Set Pi defaultProjectTrust to always for all projects?", default=False
        )
    owned = initialize(extensions=extensions, project_trust=project_trust)
    print_success(f"Initialization complete ({len(owned)} settings added)")


@prompts_app.command("status")
def prompts_status_cmd() -> None:
    from lucode.prompts import status

    console.print_json(data=status())


@prompts_app.command("update")
def prompts_update_cmd(resume: Annotated[bool, typer.Option("--resume")] = False) -> None:
    from lucode.prompts import update

    console.print_json(data=update(resume=resume))


@prompts_app.command("rollback")
def prompts_rollback_cmd() -> None:
    from lucode.prompts import rollback

    console.print_json(data=rollback())


def _version_callback(value: bool) -> None:
    if value:
        # Keep version-only startup independent of telemetry's optional runtime work.
        from lucode.telemetry import lucode_version

        print(lucode_version())
        raise typer.Exit()


@app.command("mcp-proxy", hidden=True)
def mcp_proxy_cmd(
    url: Annotated[
        str,
        typer.Option("--url", help="Databricks streamable-HTTP MCP endpoint to forward to."),
    ],
    host: Annotated[
        str | None,
        typer.Option(
            "--host", help="Workspace URL for token minting. Defaults to the saved workspace."
        ),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Databricks CLI profile.")
    ] = None,
    use_pat: Annotated[
        bool, typer.Option("--use-pat", help="Use the profile's static PAT instead of OAuth.")
    ] = False,
) -> None:
    """Bridge a coding agent's stdio MCP transport to a Databricks MCP endpoint.

    Each configured client spawns this as a local stdio MCP server (see
    `lucode configure mcp`); it forwards messages to ``--url`` and injects the
    configured OAuth credential or a PAT activated once before startup. OAuth
    tokens are refreshed through the normal token path per request. Not meant
    for interactive use — the agent manages this process's lifecycle."""
    # The proxy server stack is needed only for this hidden subprocess command.
    from lucode.mcp.proxy import serve

    state = load_state()
    workspace = host or state.get("workspace")
    if not workspace:
        print_err("No workspace configured. Run `lucode configure` first.")
        raise typer.Exit(1)
    profile = profile or state.get("profile")
    serve(url, workspace, profile, use_pat=use_pat or bool(state.get("use_pat")))


@app.command("auth-token", hidden=True)
def auth_token_cmd(
    host: Annotated[
        str | None, typer.Option("--host", help="Workspace URL. Defaults to the saved workspace.")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Databricks CLI profile.")
    ] = None,
    use_pat: Annotated[
        bool, typer.Option("--use-pat", help="Read the profile's static PAT instead of OAuth.")
    ] = False,
) -> None:
    """Print a Databricks bearer token to stdout, then exit.

    This is the cross-platform helper invoked by Pi's auth command on every
    token refresh. It is not meant for interactive use. All token logic
    (DATABRICKS_BEARER short-circuit, PAT
    profiles, OAuth refresh) lives in `get_databricks_token`, so the same
    binary works on macOS, Linux, and Windows without any POSIX shell."""
    import sys

    state = load_state()
    workspace = host or state.get("workspace")
    if not workspace:
        print_err("No workspace configured. Run `lucode configure` first.")
        raise typer.Exit(1)
    profile = profile or state.get("profile")
    if use_pat or state.get("use_pat"):
        # --use-pat explicitly means "serve the profile's static PAT". Fail
        # closed if it can't be read rather than falling through to OAuth —
        # `auth token` cannot serve a PAT-only profile, so that path would
        # surface a misleading stale-login error instead of the real cause.
        if not ensure_pat_bearer(profile):
            print_err(
                f"--use-pat: no personal access token available for profile "
                f"'{profile or '<none>'}'. Add a `token = <PAT>` entry under "
                f"[{profile or 'your-profile'}] in ~/.databrickscfg, or re-run "
                "`lucode configure` without --use-pat to use OAuth."
            )
            raise typer.Exit(1)
    try:
        token = get_databricks_token(workspace, profile)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    # Write the bare token (with trailing newline) to stdout — nothing else may
    # land on stdout or the consuming agent will treat it as part of the token.
    sys.stdout.write(token + "\n")


def _auto_configure_tool(tool: str) -> None:
    """First-time setup for a single tool — mirrors configure_workspace_command."""
    existing = load_state()
    workspace = existing.get("workspace")
    profile = existing.get("profile")
    if not workspace:
        workspace, profile = _prompt_for_configuration(tool)
    state = configure_shared_state(workspace, profile=profile, tools=[tool])

    state = configure_single_tool(tool, state)

    spec = TOOL_SPECS[tool]
    console.print(
        Panel(
            f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]\n"
            f"[bold]{spec['display']}:[/bold] [green]configured[/green] "
            "[dim](Provider: Databricks)[/dim]",
            title="Configuration Complete",
            style="green",
            expand=False,
        )
    )

    with spinner(f"Validating {spec['display']}..."):
        ok, err = validate_tool(tool)
    if ok:
        print_success(f"{spec['display']} is working")
    else:
        print_err(f"{spec['display']}: {err}")
        managed = bool(state.get("managed_configs", {}).get(tool))
        restore_file(spec["config_path"], spec["backup_path"], managed)
        available_tools = [t for t in (state.get("available_tools") or []) if t != tool]
        state["available_tools"] = available_tools
        save_state(state)
        raise RuntimeError(f"{spec['display']} validation failed — config reverted.")


def _launch_tool(
    tool_name: str,
    ctx: typer.Context,
    skip_preflight: bool = False,
    workspace: str | None = None,
) -> None:
    try:
        tool = normalize_tool(tool_name)
        if workspace:
            set_current_workspace(normalize_workspace_url(workspace))
        existing = load_state()
        apply_pat_environment(existing)
        needs_auto_configure = not existing.get("workspace") or tool not in (
            existing.get("available_tools") or []
        )
        ensure_bootstrap_dependencies(tool, update_existing=needs_auto_configure)
        if needs_auto_configure:
            _auto_configure_tool(tool)
        state = ensure_provider_state(tool)
        state = configure_shared_state(
            state["workspace"],
            profile=state.get("profile"),
            tools=[tool],
            skip_preflight=skip_preflight,
        )
        state, resolved_model = resolve_launch_model(tool, state, None)
        state = configure_tool(tool, state, resolved_model)
        print_section(f"lucode with {TOOL_SPECS[tool]['display']}")
        if resolved_model:
            print_kv("Model", resolved_model)
        print_note(
            f"{TOOL_SPECS[tool]['display']} token refresh is managed automatically "
            "every 30 minutes while the session is running."
        )
        print_success(f"Starting {TOOL_SPECS[tool]['display']}")
        launch_agent(tool, state, ctx.args)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


# Launch-only escape hatch for headless launchers (e.g. omnigent) that
# have already run `lucode configure`: skip the ~5-10s per-launch auth + AI
# Gateway re-validation. Distinct from the configure-only `--skip-validate`,
# which skips the model smoke test.
SkipPreflightOption = Annotated[
    bool,
    typer.Option(
        "--skip-preflight",
        help="Skip the per-launch Databricks auth + AI Gateway re-validation, trusting a "
        "prior `lucode configure`.",
    ),
]

# Target this launch at a specific workspace, auto-configuring (and logging in)
# if it hasn't been set up yet — so a launch needs no prior `lucode configure`.
WorkspaceOption = Annotated[
    str | None,
    typer.Option(
        "--workspace",
        help="Databricks workspace URL to launch against; sets up and authenticates it "
        "if not already configured.",
    ),
]


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the lucode version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print config files without writing them."),
    ] = False,
    skip_preflight: SkipPreflightOption = False,
    workspace: WorkspaceOption = None,
) -> None:
    """Configure and launch coding agents through Databricks AI Gateway."""
    if ctx.invoked_subcommand is not None:
        return
    set_dry_run(dry_run)
    console.print(ctx.get_help())


@app.command(
    "opencode", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def opencode_cmd(ctx: typer.Context, skip_preflight: SkipPreflightOption = False) -> None:
    """Launch OpenCode via Databricks."""
    _launch_tool("opencode", ctx, skip_preflight=skip_preflight)


@app.command("pi", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def pi_cmd(ctx: typer.Context, skip_preflight: SkipPreflightOption = False) -> None:
    """Launch Pi coding agent via Databricks."""
    _launch_tool("pi", ctx, skip_preflight=skip_preflight)


@configure_app.callback(invoke_without_command=True)
def configure(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print config files without writing them.")
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="Configure only the named agent (opencode or pi).",
        ),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="Configure a comma-separated list of agents without prompting (e.g. opencode,pi).",
        ),
    ] = None,
    workspaces: Annotated[
        str | None,
        typer.Option(
            "--workspaces",
            help="Configure a comma-separated list of workspaces without prompting.",
        ),
    ] = None,
    profiles: Annotated[
        str | None,
        typer.Option(
            "--profiles",
            help="Configure a comma-separated list of existing Databricks CLI profiles "
            "without the workspace prompt. Each profile's host from ~/.databrickscfg "
            "supplies the workspace URL. Auth behaves like --workspaces: OAuth login "
            "is forced unless --use-pat is also passed.",
        ),
    ] = None,
    use_pat: Annotated[
        bool,
        typer.Option(
            "--use-pat",
            help="Authenticate with the personal access token stored in "
            "~/.databrickscfg for the selected profile(s) instead of OAuth. "
            "Requires --profiles; no interactive login is run. Intended for "
            "CI / headless environments.",
        ),
    ] = False,
    skip_validate: Annotated[
        bool,
        typer.Option(
            "--skip-validate",
            help="Skip the post-configure validation step that sends a quick test "
            "message through each agent. Config files are still written with the "
            "freshly discovered models.",
        ),
    ] = False,
    enable_databricks_ai_tools: Annotated[
        bool | None,
        typer.Option(
            "--enable-databricks-ai-tools/--disable-databricks-ai-tools",
            help="Install Databricks AI Tools (skills + plugins that teach agents to use "
            "Databricks) for the configured agents. Installed by default; pass "
            "--disable-databricks-ai-tools to opt out.",
        ),
    ] = None,
    mcp: Annotated[
        str | None,
        typer.Option(
            "--mcp",
            help="Also register the given Databricks MCP service(s) for the configured "
            "coding agents, in one command. Pass a comma-separated list of fully-qualified "
            "names like `system.ai.slack`. Combine with --agents to set up an agent and its "
            "MCP servers together (e.g. `--agents opencode --mcp system.ai.slack`).",
        ),
    ] = None,
    skip_upgrade: Annotated[
        bool,
        typer.Option(
            "--skip-upgrade",
            help="Don't prompt to upgrade already-installed agent CLIs to a newer version. "
            "Required updates (when an agent is below its minimum supported version) are "
            "still applied.",
        ),
    ] = False,
    verbose: Annotated[
        str,
        typer.Option(
            "--verbose",
            help="Output verbosity: 'normal' (default) renders decorative panels; "
            "'low' prints terse single-line status instead.",
        ),
    ] = "normal",
) -> None:
    """Configure workspace URL and AI Gateway."""
    if ctx.invoked_subcommand is not None:
        return
    if verbose not in ("normal", "low"):
        print_err("--verbose must be one of: normal, low.")
        raise typer.Exit(2)
    set_dry_run(dry_run)
    set_verbosity(verbose)
    prompt_optional_updates = not skip_upgrade
    try:
        install_databricks_cli()
        if agent is not None and agents is not None:
            raise RuntimeError("Use either --agent or --agents, not both.")
        if workspaces is not None and profiles is not None:
            raise RuntimeError("Use either --workspaces or --profiles, not both.")
        if use_pat and profiles is None:
            raise RuntimeError(
                "--use-pat requires --profiles. Pass the PAT-backed Databricks CLI "
                "profile(s) explicitly, e.g. `lucode configure --profiles DEFAULT --use-pat`."
            )
        workspace_entries = _parse_workspaces_option(workspaces) if workspaces is not None else None
        if profiles is not None:
            workspace_entries = _parse_profiles_option(profiles)
        # Only forward the opt-in flags when set so existing call expectations
        # (and defaults) stay unchanged for the common interactive path.
        skip_kwargs: dict = {}
        if use_pat:
            skip_kwargs["use_pat"] = True
        if skip_validate:
            skip_kwargs["skip_validate"] = True
        if enable_databricks_ai_tools is not None:
            skip_kwargs["databricks_ai_tools_enabled"] = enable_databricks_ai_tools
        # Set True only in the fully-interactive branch below; gates the optional
        # MCP setup prompt so flag-driven / scripted runs are never interrupted.
        fully_interactive = False
        if agent is not None:
            tool = normalize_tool(agent)
            install_tool_binary(
                tool,
                strict=True,
                update_existing=True,
                prompt_optional_updates=prompt_optional_updates,
            )
            if workspace_entries is None:
                configure_workspace_command(tool, **skip_kwargs)
            else:
                configure_workspace_command(
                    tool,
                    workspaces=workspace_entries,
                    **skip_kwargs,
                )
        elif agents is not None:
            selected_tools = _parse_agents_option(agents)
            if workspace_entries is None:
                configure_workspace_command(
                    selected_tools=selected_tools,
                    prompt_optional_updates=prompt_optional_updates,
                    **skip_kwargs,
                )
            else:
                configure_workspace_command(
                    selected_tools=selected_tools,
                    workspaces=workspace_entries,
                    prompt_optional_updates=prompt_optional_updates,
                    **skip_kwargs,
                )
        elif mcp is not None:
            # MCP-only: `--mcp` without --agent(s) (adding MCP servers to an
            # already-configured setup).
            # Configure just the workspace — no interactive agent picker — so the
            # `--mcp` registration below has a current workspace to target.
            if workspace_entries is None:
                workspace_entries = [_prompt_for_configuration(None)]
            _configure_shared_workspace_states(
                workspace_entries,
                tools=[],
                force_login=not use_pat,
                use_pat=use_pat,
            )
        else:
            # Tool binaries are installed after the user picks which agents
            # they want, in configure_workspace_command.
            if workspace_entries is None:
                configure_workspace_command(
                    prompt_optional_updates=prompt_optional_updates,
                    **skip_kwargs,
                )
            else:
                configure_workspace_command(
                    workspaces=workspace_entries,
                    prompt_optional_updates=prompt_optional_updates,
                    **skip_kwargs,
                )
            # Only the no-agent, no-workspace path is truly interactive (the user
            # picked agents/workspace via prompts); that's where we offer the MCP
            # step below. Flag-driven runs stay scriptable.
            fully_interactive = workspace_entries is None
        if mcp is not None:
            # The workspace + agents were just configured above, so the current
            # workspace state now lists the agents whose MCP configs we should
            # write. `--mcp` takes fully-qualified service names, which
            # `configure_mcp_command` locates and registers without a picker
            # (bare short names would need --location, which we don't accept here).
            services = {name.strip() for name in mcp.split(",") if name.strip()}
            if not services:
                raise RuntimeError(
                    "--mcp needs at least one fully-qualified MCP service name, e.g. "
                    "`--mcp system.ai.slack`."
                )
            bare = sorted(name for name in services if name.count(".") < 2)
            if bare:
                raise RuntimeError(
                    "--mcp names must be fully qualified `<catalog>.<schema>.<name>` "
                    f"(got: {', '.join(bare)}). Use `lucode configure mcp` for the "
                    "interactive picker."
                )
            configure_mcp_command(services=services)
        # Offer MCP setup as the natural next step of interactive configuration,
        # so users discover it without needing to know `configure mcp` exists.
        # Skipped in dry-run and non-interactive/flag-driven runs (which stay
        # scriptable), and when --dry-run is set.
        if fully_interactive and not dry_run and prompt_yes_no("Configure MCP servers now?"):
            configure_mcp_command()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@configure_app.command("mcp")
def configure_mcp(
    location: Annotated[
        str | None,
        typer.Option(
            "--location",
            help="Non-interactive: replace registered MCPs with exactly the services "
            "in the given Unity Catalog `<catalog>.<schema>` (e.g. `system.ai`) and "
            "exit without showing the picker. Any previously-registered MCPs outside "
            "this location are removed.",
        ),
    ] = None,
    services: Annotated[
        str | None,
        typer.Option(
            "--services",
            help="Configure exactly this comma-separated subset of MCP services (adding and "
            "removing to match) instead of a whole schema. Full names like `system.ai.github` "
            "work on their own; bare short names like `github` need --location to locate them. "
            "Omit --services to configure the whole --location schema; pass an empty string "
            "(with --location) to remove all.",
        ),
    ] = None,
) -> None:
    """Add Databricks MCP servers to installed coding tools."""
    # `--services` absent -> None (whole schema); present (even empty) -> the
    # explicit subset, so `--services ""` deselects everything.
    selected = None if services is None else {s.strip() for s in services.split(",") if s.strip()}
    try:
        configure_mcp_command(location=location, services=selected)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@configure_app.command("skills")
def configure_skills(
    location: Annotated[
        str | None,
        typer.Option("--location", help="Comma-separated `<catalog>.<schema>` skill scopes."),
    ] = None,
    mcp: Annotated[
        bool,
        typer.Option("--mcp", help="Mutate the skills MCP connection instead of downloading."),
    ] = False,
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            help="(download) Existing absolute dir to download into; defaults to your home dir.",
        ),
    ] = None,
    skill: Annotated[
        str | None,
        typer.Option(
            "--skill",
            help="(download) Download only this comma-separated subset of skills (by leaf "
            "name, e.g. `my-skill`) from the schema, instead of every skill. Requires a "
            "single --location; not valid with --mcp.",
        ),
    ] = None,
) -> None:
    """Configure Databricks Skills for your coding tools.

    When ``--location`` is not provided, registers the skills MCP connection with
    utility tools only.

    When ``--location`` is provided: with ``--mcp``, sets the connection's scope to
    exactly the listed schemas (no download); otherwise, downloads every skill in
    each schema to disk (under ``--path``, or your home dir when omitted) and
    registers the MCP connection with utility tools only. ``--skill`` narrows a
    download to a named subset of a single schema's skills (requires exactly one
    ``--location``).
    """
    try:
        locations = _parse_skill_locations(location)
        # `--skill` absent -> None (whole schema); present (even empty) -> the
        # explicit subset, so `--skill ""` downloads nothing.
        selected_skills = (
            None if skill is None else {s.strip() for s in skill.split(",") if s.strip()}
        )
        if mcp and path is not None:
            raise RuntimeError("--path is not valid with --mcp.")
        if mcp and selected_skills is not None:
            raise RuntimeError("--skill is not valid with --mcp; it only applies when downloading.")
        if path is not None and not locations:
            raise RuntimeError("--path only applies when downloading with --location.")
        if selected_skills is not None and not locations:
            raise RuntimeError("--skill only applies when downloading with --location.")
        if selected_skills is not None and len(locations) != 1:
            raise RuntimeError(
                f"--skill requires a single --location (got: {', '.join(locations)})."
            )
        if mcp or not locations:
            configure_skills_mcp_command(locations)
        else:
            configure_fetch_command(locations, path=path, skills=selected_skills)
    except (RuntimeError, ValueError) as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@app.command("status")
def status_cmd() -> None:
    """Show current workspace, tool configs, and saved model selections."""
    try:
        status()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("revert")
def revert_cmd() -> None:
    """Clear lucode state and restore backed-up agent config files."""
    try:
        revert()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("upgrade")
def upgrade_cmd() -> None:
    """Upgrade lucode to the latest version from GitHub."""
    import subprocess

    git_url = "git+https://github.com/mjenior-lilly/lucode"
    print_section("Upgrade")
    print_kv("Source", git_url)
    try:
        subprocess.run(
            ["uv", "tool", "install", "--reinstall", git_url],
            check=True,
        )
    except FileNotFoundError:
        print_err("`uv` was not found on PATH. Install uv to upgrade lucode.")
        raise typer.Exit(1) from None
    except subprocess.CalledProcessError as exc:
        print_err(f"Upgrade failed (exit code {exc.returncode}).")
        raise typer.Exit(1) from None
    print_success("lucode upgraded")


def main() -> None:
    app()


def loc_main() -> None:
    """Launch OpenCode with unchanged arguments."""
    args = sys.argv[1:]
    sys.argv[1:] = ["opencode", *args]
    app(prog_name="loc")


def lpi_main() -> None:
    """Update the shared prompt revision, then launch Pi with unchanged arguments."""
    args = sys.argv[1:]
    if not any(arg in {"--help", "-h", "--version"} for arg in args):
        from lucode.prompts import update

        try:
            result = update()
        except RuntimeError as exc:
            print_err(str(exc))
            raise SystemExit(1) from None
        if str(result.get("last_result", "")).startswith("failed:"):
            print_warning(result["last_result"])
    sys.argv[1:] = ["pi", *args]
    app(prog_name="lpi")


if __name__ == "__main__":
    main()
