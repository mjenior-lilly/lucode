"""Workspace provisioning and configure-command orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from rich.panel import Panel

from lucode.agents.configuration import (
    check_gateway_endpoint,
    configure_selected_tools,
    configure_single_tool,
)
from lucode.agents.install import install_tool_binary
from lucode.agents.registry import TOOL_DISCOVERY_SOURCES, TOOL_SPECS, normalize_tool
from lucode.agents.validation import validate_all_tools, validate_tool
from lucode.config import restore_file
from lucode.databricks.auth import (
    ensure_databricks_auth,
    ensure_pat_bearer,
    find_profile_name_for_host,
    get_databricks_profiles,
    get_databricks_token,
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
from lucode.mcp.commands import configure_mcp_command
from lucode.mcp.config import purge_cross_workspace_mcp_residue
from lucode.state import load_state, load_workspace_state, save_state
from lucode.ui import (
    console,
    normalize_workspace_url,
    print_err,
    print_note,
    print_success,
    print_warning,
    prompt_for_tools,
    prompt_for_workspace,
    prompt_yes_no,
    prompt_yes_no_default,
    spinner,
)


def _discovery_consumers() -> dict[str, tuple[str, ...]]:
    families = {family for sources in TOOL_DISCOVERY_SOURCES.values() for family in sources}
    return {
        family: tuple(tool for tool, sources in TOOL_DISCOVERY_SOURCES.items() if family in sources)
        for family in families
    }


def _requested_model_families(tools: list[str] | None) -> tuple[str, ...]:
    selected_tools = TOOL_DISCOVERY_SOURCES if tools is None else tools
    return tuple(
        dict.fromkeys(
            family for tool in selected_tools for family in TOOL_DISCOVERY_SOURCES.get(tool, ())
        )
    )


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
    consumers_by_source = _discovery_consumers()
    for source, reason in reasons.items():
        consumers = ", ".join(consumers_by_source.get(source, ()))
        label = labels.get(source, source)
        if reason:
            print_note(f"{label} (needed for: {consumers}): {reason}")
        else:
            print_note(f"{label} (needed for: {consumers}): no models returned")
    print_note(
        "Re-run with `LUCODE_DEBUG=1` to log raw discovery responses to ~/.lucode/debug.log."
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


_MODEL_STATE_KEYS = (
    "claude_models",
    "gemini_models",
    "codex_models",
    "oss_models",
    "opencode_models",
)


def _assemble_initial_state(
    workspace: str,
    profile: str | None,
    use_pat: bool | None,
    databricks_ai_tools_enabled: bool | None,
) -> tuple[dict, str | None, bool, str | None]:
    """Resolve workspace-local defaults and build discovery-independent state."""
    current_state = load_state()
    previous_workspace = current_state.get("workspace")
    prior_state = load_workspace_state(workspace)
    if not prior_state and previous_workspace == workspace:
        prior_state = current_state
    resolved_use_pat = bool(prior_state.get("use_pat")) if use_pat is None else use_pat
    if databricks_ai_tools_enabled is None:
        databricks_ai_tools_enabled = prior_state.get("databricks_ai_tools_enabled") is not False
    state = dict(prior_state)
    state["workspace"] = workspace
    if profile:
        state["profile"] = profile
    else:
        state.pop("profile", None)
    state.pop("uc_enabled", None)
    if resolved_use_pat:
        state["use_pat"] = True
    else:
        state.pop("use_pat", None)
    state.pop("fable_enabled", None)
    state["databricks_ai_tools_enabled"] = databricks_ai_tools_enabled
    state["base_urls"] = build_shared_base_urls(workspace)
    return state, previous_workspace, resolved_use_pat, profile


def _authenticate_and_verify_gateway(
    state: dict,
    profile: str | None,
    *,
    force_login: bool,
    use_pat: bool,
) -> tuple[str, str | None]:
    """Authenticate, resolve the profile, and verify AI Gateway v2."""
    workspace = state["workspace"]
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
        ensure_pat_bearer(profile, pat)
        ensure_databricks_auth(workspace, profile)
    elif force_login:
        run_databricks_login(workspace, profile)
    else:
        ensure_databricks_auth(workspace, profile)
    if profile is None:
        profile = find_profile_name_for_host(workspace)
        if profile:
            state["profile"] = profile
    with spinner("Verifying Unity AI Gateway..."):
        token = get_databricks_token(workspace, profile)
        ensure_ai_gateway_v2(workspace, token)
    print_success("Unity AI Gateway detected")
    return token, profile


def _discover_workspace_models(
    state: dict,
    token: str,
    tools: list[str] | None,
    *,
    skip_model_discovery: bool,
) -> tuple[dict[str, str | None], bool, dict]:
    """Discover requested model families and apply results to state."""
    workspace = state["workspace"]
    fetch_all = tools is None
    requested = dict.fromkeys(_requested_model_families(tools), True)
    reasons: dict[str, str | None] = dict.fromkeys(requested)
    prior_model_values = {key: state[key] for key in _MODEL_STATE_KEYS if key in state}
    if skip_model_discovery:
        return reasons, False, prior_model_values

    with spinner("Fetching available models..."):
        ms_claude, ms_codex, ms_gemini, ms_oss, ms_reason = discover_model_services(
            workspace, token
        )
        partial = bool(ms_reason and (ms_claude or ms_codex or ms_gemini or ms_oss))
        claude_models: dict = {}
        gemini_models: list = []
        codex_models: list = []
        oss_models: list = []
        if requested.get("claude"):
            claude_models, reasons["claude"] = ms_claude, ms_reason
            if not claude_models:
                claude_models, reasons["claude"] = discover_claude_models(workspace, token)
            claude_models.pop("fable", None)
        if requested.get("gemini"):
            gemini_models, reasons["gemini"] = ms_gemini, ms_reason
            if not gemini_models:
                gemini_models, reasons["gemini"] = discover_gemini_models(workspace, token)
        if requested.get("codex"):
            codex_models, reasons["codex"] = ms_codex, ms_reason
            if not codex_models:
                codex_models, reasons["codex"] = discover_codex_models(workspace, token)
        if requested.get("oss"):
            oss_models, reasons["oss"] = ms_oss, ms_reason

    opencode_models: dict[str, list[str]] = {}
    if claude_models:
        opencode_models["anthropic"] = list(claude_models.values())
    if gemini_models:
        opencode_models["gemini"] = gemini_models
    if oss_models:
        opencode_models["oss"] = oss_models
    values = {
        "claude": claude_models,
        "gemini": gemini_models,
        "codex": codex_models,
        "oss": oss_models,
    }
    for family, wanted in requested.items():
        if wanted:
            state[f"{family}_models"] = values[family]
    if fetch_all or "opencode" in tools:
        state["opencode_models"] = opencode_models
    if partial:
        print_warning(
            f"Model-service discovery was incomplete: {ms_reason}. "
            "Using partial results for this run without replacing saved model lists."
        )
    return reasons, partial, prior_model_values


def _persist_workspace_result(
    state: dict,
    previous_workspace: str | None,
    reasons: dict[str, str | None],
    *,
    partial: bool,
    prior_model_values: dict,
) -> dict:
    """Persist complete results or preserve saved models after partial discovery."""
    state_to_save = state.copy()
    if partial:
        for key in _MODEL_STATE_KEYS:
            if key in prior_model_values:
                state_to_save[key] = prior_model_values[key]
            else:
                state_to_save.pop(key, None)
    save_state(state_to_save)
    workspace = state["workspace"]
    if previous_workspace and previous_workspace != workspace:
        purge_cross_workspace_mcp_residue(state, workspace)
    state["_discovery_reasons"] = reasons
    return state


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
    """Provision and persist shared state for one workspace."""
    workspace = normalize_workspace_url(workspace)
    state, previous_workspace, resolved_use_pat, profile = _assemble_initial_state(
        workspace, profile, use_pat, databricks_ai_tools_enabled
    )
    if skip_preflight:
        if profile is None:
            profile = find_profile_name_for_host(workspace)
            if profile:
                state["profile"] = profile
        return _persist_workspace_result(
            state,
            previous_workspace,
            dict.fromkeys(_requested_model_families(tools), "skipped (--skip-preflight)"),
            partial=False,
            prior_model_values={},
        )
    token, _ = _authenticate_and_verify_gateway(
        state, profile, force_login=force_login, use_pat=resolved_use_pat
    )
    reasons, partial, prior_models = _discover_workspace_models(
        state, token, tools, skip_model_discovery=skip_model_discovery
    )
    return _persist_workspace_result(
        state,
        previous_workspace,
        reasons,
        partial=partial,
        prior_model_values=prior_models,
    )


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


@dataclass(frozen=True)
class ConfigurationRequest:
    dry_run: bool = False
    agent: str | None = None
    agents: str | None = None
    workspaces: str | None = None
    profiles: str | None = None
    use_pat: bool = False
    skip_validate: bool = False
    databricks_ai_tools_enabled: bool | None = None
    mcp: str | None = None
    skip_upgrade: bool = False


def run_configuration(request: ConfigurationRequest) -> None:
    """Validate and dispatch one configure request."""
    prompt_optional_updates = not request.skip_upgrade
    if request.agent is not None and request.agents is not None:
        raise RuntimeError("Use either --agent or --agents, not both.")
    if request.workspaces is not None and request.profiles is not None:
        raise RuntimeError("Use either --workspaces or --profiles, not both.")
    if request.use_pat and request.profiles is None:
        raise RuntimeError(
            "--use-pat requires --profiles. Pass the PAT-backed Databricks CLI "
            "profile(s) explicitly, e.g. `lucode configure --profiles DEFAULT --use-pat`."
        )
    workspace_entries = (
        _parse_workspaces_option(request.workspaces) if request.workspaces is not None else None
    )
    if request.profiles is not None:
        workspace_entries = _parse_profiles_option(request.profiles)
    kwargs: dict = {}
    if request.use_pat:
        kwargs["use_pat"] = True
    if request.skip_validate:
        kwargs["skip_validate"] = True
    if request.databricks_ai_tools_enabled is not None:
        kwargs["databricks_ai_tools_enabled"] = request.databricks_ai_tools_enabled
    fully_interactive = False
    if request.agent is not None:
        tool = normalize_tool(request.agent)
        install_tool_binary(
            tool,
            strict=True,
            update_existing=True,
            prompt_optional_updates=prompt_optional_updates,
        )
        if workspace_entries is None:
            configure_workspace_command(tool, **kwargs)
        else:
            configure_workspace_command(tool, workspaces=workspace_entries, **kwargs)
    elif request.agents is not None:
        selected_tools = _parse_agents_option(request.agents)
        if workspace_entries is None:
            configure_workspace_command(
                selected_tools=selected_tools,
                prompt_optional_updates=prompt_optional_updates,
                **kwargs,
            )
        else:
            configure_workspace_command(
                selected_tools=selected_tools,
                workspaces=workspace_entries,
                prompt_optional_updates=prompt_optional_updates,
                **kwargs,
            )
    elif request.mcp is not None:
        if workspace_entries is None:
            workspace_entries = [_prompt_for_configuration(None)]
        _configure_shared_workspace_states(
            workspace_entries,
            tools=[],
            force_login=not request.use_pat,
            use_pat=request.use_pat,
        )
    else:
        if workspace_entries is None:
            configure_workspace_command(
                prompt_optional_updates=prompt_optional_updates,
                **kwargs,
            )
        else:
            configure_workspace_command(
                workspaces=workspace_entries,
                prompt_optional_updates=prompt_optional_updates,
                **kwargs,
            )
        fully_interactive = workspace_entries is None
    if request.mcp is not None:
        services = {name.strip() for name in request.mcp.split(",") if name.strip()}
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
    if fully_interactive and not request.dry_run and prompt_yes_no("Configure MCP servers now?"):
        configure_mcp_command()
