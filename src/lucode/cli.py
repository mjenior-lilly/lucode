#!/usr/bin/env python3
"""CLI entry point for lucode."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Annotated

import typer
from rich.panel import Panel

from lucode.agents.configuration import configure_single_tool, ensure_provider_state
from lucode.agents.install import ensure_bootstrap_dependencies
from lucode.agents.pi import (
    PI_MODES_CONFIG_PATH,
    PI_SETTINGS_BACKUP_PATH,
    PI_SETTINGS_PATH,
)
from lucode.agents.registry import (
    TOOL_SPECS,
    configure_tool,
    normalize_tool,
    resolve_launch_model,
)
from lucode.agents.registry import (
    launch as launch_agent,
)
from lucode.agents.validation import validate_tool
from lucode.config import restore_file, set_dry_run
from lucode.databricks.auth import (
    apply_pat_environment,
    ensure_pat_bearer,
    get_databricks_token,
    install_databricks_cli,
)
from lucode.fetch import configure_fetch_command
from lucode.mcp.commands import configure_mcp_command
from lucode.mcp.config import MCP_CLIENTS, revert_mcp_configs
from lucode.mcp.skills import SKILLS_MCP_KIND, configure_skills_mcp_command
from lucode.provisioning import (
    ConfigurationRequest,
    _prompt_for_configuration,
    configure_shared_state,
    run_configuration,
)
from lucode.state import (
    STATE_PATH,
    clear_state,
    load_state,
    load_workspace_state,
    save_state,
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
    prompt_yes_no,
    prompt_yes_no_default,
    set_verbosity,
    spinner,
    status_badge,
)


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
prompts_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(prompts_app, name="prompts", help="Manage revisioned shared prompts.")


@app.command("init")
def init_cmd(
    extensions: Annotated[bool | None, typer.Option("--extensions/--no-extensions")] = None,
    project_trust: Annotated[
        bool | None, typer.Option("--project-trust/--no-project-trust")
    ] = None,
    revert: Annotated[bool, typer.Option("--revert")] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite a user-edited or unrecognized Pi modes config after confirmation.",
        ),
    ] = False,
) -> None:
    """Initialize Pi preferences and install the extension-consented modes package."""
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
    try:
        result = initialize(
            extensions=extensions,
            project_trust=project_trust,
            force=force,
            confirm=prompt_yes_no,
        )
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    if result.definitions_outcome:
        detail = (
            f"; {result.definitions_backup_count} displaced files backed up"
            if result.definitions_backup_count
            else ""
        )
        print_success(f"Pi mode definitions {result.definitions_outcome}{detail}")
    print_success(f"Initialization complete ({len(result.owned)} settings added)")
    if result.modes_outcome == "written":
        print_success("Pi modes configuration written")
    elif result.modes_outcome == "refreshed":
        print_success("Pi modes configuration refreshed")
    elif result.modes_outcome == "forced":
        print_success(
            f"Pi modes configuration replaced; previous content moved to {result.modes_backup_path}"
        )
    elif result.modes_outcome in {"skipped_user_modified", "skipped_foreign"}:
        reason = (
            "has local edits"
            if result.modes_outcome == "skipped_user_modified"
            else "was not written by lucode"
        )
        print_warning(
            f"Skipped {PI_MODES_CONFIG_PATH}: it {reason}. "
            "Run `lucode init --force` to replace it after confirmation."
        )
    elif result.modes_outcome == "force_declined":
        print_warning(f"Left {PI_MODES_CONFIG_PATH} unchanged; overwrite was declined")


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


def _auto_configure_tool(tool: str, existing: dict | None = None) -> dict:
    """First-time setup for a single tool — mirrors configure_workspace_command."""
    existing = load_state() if existing is None else existing
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
    return state


def _launch_tool(
    tool_name: str,
    ctx: typer.Context,
    skip_preflight: bool = False,
    workspace: str | None = None,
) -> None:
    try:
        tool = normalize_tool(tool_name)
        if workspace:
            workspace = normalize_workspace_url(workspace)
            existing = load_workspace_state(workspace) or {"workspace": workspace}
        else:
            existing = load_state()
        apply_pat_environment(existing)
        needs_auto_configure = not existing.get("workspace") or tool not in (
            existing.get("available_tools") or []
        )
        ensure_bootstrap_dependencies(tool, update_existing=needs_auto_configure)
        if needs_auto_configure:
            existing = _auto_configure_tool(tool, existing)
        state = ensure_provider_state(tool, existing)
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

LaunchDryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Print config files without writing them."),
]


@dataclass(frozen=True)
class LaunchOptions:
    workspace: str | None = None
    skip_preflight: bool = False
    dry_run: bool = False


def _resolved_launch_options(
    ctx: typer.Context,
    workspace: str | None,
    skip_preflight: bool,
    dry_run: bool,
) -> LaunchOptions:
    global_options = ctx.find_root().obj
    if not isinstance(global_options, LaunchOptions):
        global_options = LaunchOptions()
    return LaunchOptions(
        workspace=workspace or global_options.workspace,
        skip_preflight=skip_preflight or global_options.skip_preflight,
        dry_run=dry_run or global_options.dry_run,
    )


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
    dry_run: LaunchDryRunOption = False,
    skip_preflight: SkipPreflightOption = False,
    workspace: WorkspaceOption = None,
) -> None:
    """Configure and launch coding agents through Databricks AI Gateway."""
    ctx.obj = LaunchOptions(workspace, skip_preflight, dry_run)
    if ctx.invoked_subcommand is not None:
        return
    set_dry_run(dry_run)
    console.print(ctx.get_help())


@app.command(
    "opencode", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def opencode_cmd(
    ctx: typer.Context,
    skip_preflight: SkipPreflightOption = False,
    workspace: WorkspaceOption = None,
    dry_run: LaunchDryRunOption = False,
) -> None:
    """Launch OpenCode via Databricks."""
    options = _resolved_launch_options(ctx, workspace, skip_preflight, dry_run)
    set_dry_run(options.dry_run)
    _launch_tool(
        "opencode",
        ctx,
        skip_preflight=options.skip_preflight,
        workspace=options.workspace,
    )


@app.command("pi", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def pi_cmd(
    ctx: typer.Context,
    skip_preflight: SkipPreflightOption = False,
    workspace: WorkspaceOption = None,
    dry_run: LaunchDryRunOption = False,
) -> None:
    """Launch Pi coding agent via Databricks."""
    options = _resolved_launch_options(ctx, workspace, skip_preflight, dry_run)
    set_dry_run(options.dry_run)
    _launch_tool(
        "pi",
        ctx,
        skip_preflight=options.skip_preflight,
        workspace=options.workspace,
    )


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
            help="Don't prompt to optionally upgrade already-installed agent CLIs.",
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
    try:
        install_databricks_cli()
        run_configuration(
            ConfigurationRequest(
                dry_run=dry_run,
                agent=agent,
                agents=agents,
                workspaces=workspaces,
                profiles=profiles,
                use_pat=use_pat,
                skip_validate=skip_validate,
                databricks_ai_tools_enabled=enable_databricks_ai_tools,
                mcp=mcp,
                skip_upgrade=skip_upgrade,
            )
        )
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
