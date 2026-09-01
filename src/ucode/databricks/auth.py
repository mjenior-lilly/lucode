"""Databricks CLI installation, profiles, authentication, and auth command builders."""

from __future__ import annotations

import configparser
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Literal, overload

from ucode.databricks.transport import (
    debug,
    format_subprocess_result,
    log_auth_diagnostics,
)
from ucode.ui import (
    print_kv,
    print_note,
    print_section,
    print_success,
    print_warning,
    spinner,
)

UNIX_DATABRICKS_INSTALL_URL = (
    "https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh"
)
WINDOWS_DATABRICKS_INSTALL_URL = (
    "https://raw.githubusercontent.com/databricks/setup-cli/main/install.ps1"
)
# v1.0.0 is the release that ships `databricks aitools`.
MIN_DATABRICKS_CLI_VERSION = (1, 0, 0)


@overload
def run(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: Literal[True],
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: Literal[False] = False,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[bytes]: ...


def run(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        env=env,
        timeout=timeout,
    )


def build_databricks_cli_env(workspace: str, profile: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["DATABRICKS_HOST"] = workspace
    if profile is None:
        env.pop("DATABRICKS_CONFIG_PROFILE", None)
    return env


def _parse_databricks_cli_version(output: str) -> tuple[int, int, int] | None:
    # Example output: "Databricks CLI v0.299.2"
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", output)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _run_databricks_cli_installer(brew_subcommand: str = "install") -> None:
    system = platform.system()
    try:
        if system == "Windows":
            run(
                ["powershell", "-Command", f"irm {WINDOWS_DATABRICKS_INSTALL_URL} | iex"],
                timeout=240,
            )
        elif system == "Darwin" and shutil.which("brew"):
            run(["brew", brew_subcommand, "databricks/tap/databricks"], timeout=240)
        elif shutil.which("curl"):
            run(["sh", "-c", f"curl -fsSL {UNIX_DATABRICKS_INSTALL_URL} | sudo sh"], timeout=240)
        elif shutil.which("wget"):
            run(["sh", "-c", f"wget -qO- {UNIX_DATABRICKS_INSTALL_URL} | sudo sh"], timeout=240)
        else:
            raise RuntimeError("Neither curl nor wget is available.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
        raise RuntimeError("Failed to install/upgrade Databricks CLI automatically.") from exc


def ensure_databricks_cli_version() -> None:
    try:
        result = run(
            ["databricks", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Failed to read Databricks CLI version.") from exc

    raw = result.stdout or result.stderr or ""
    output = (raw if isinstance(raw, str) else raw.decode(errors="replace")).strip()
    version = _parse_databricks_cli_version(output)
    if version is None:
        raise RuntimeError(
            f"Could not parse Databricks CLI version from `databricks --version` output: {output!r}"
        )
    if version < MIN_DATABRICKS_CLI_VERSION:
        current = ".".join(str(n) for n in version)
        required = ".".join(str(n) for n in MIN_DATABRICKS_CLI_VERSION)
        print_warning(
            f"Databricks CLI v{current} is too old (need v{required} or newer). Upgrading..."
        )
        _run_databricks_cli_installer(brew_subcommand="upgrade")
        ensure_databricks_cli_version()


def install_databricks_cli() -> None:
    if shutil.which("databricks"):
        ensure_databricks_cli_version()
        return

    print_section("Bootstrap")
    print_warning("`databricks` was not found. Installing Databricks CLI...")
    _run_databricks_cli_installer(brew_subcommand="install")

    if not shutil.which("databricks"):
        raise RuntimeError(
            "Databricks CLI install completed, but `databricks` is still not on PATH."
        )
    ensure_databricks_cli_version()


def install_ai_tools(agent_tokens: list[str], profile: str | None = None) -> None:
    """Install Databricks AI Tools for the given agents (e.g. ``claude-code``).

    Databricks AI Tools is the set of skills and plugins that teach coding
    agents how to work with Databricks (installed via ``databricks aitools``).
    Idempotent and best-effort: any failure only warns (surfacing the CLI's
    own error), since AI Tools aren't required to launch an agent."""
    if not agent_tokens:
        return

    agents_arg = ",".join(agent_tokens)
    try:
        with spinner(f"Installing Databricks AI Tools for {agents_arg}..."):
            run(
                ["databricks", "aitools", "install", "--agents", agents_arg, "--scope", "global"]
                + _profile_args(profile),
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # The CLI version is already guaranteed by ensure_databricks_cli_version,
        # so any failure here is something else (e.g. an agent binary missing
        # from PATH). Surface the CLI's own error rather than guessing a cause.
        detail = getattr(exc, "stderr", None) or ""
        if isinstance(detail, bytes):  # TimeoutExpired.stderr is bytes even with text=True
            detail = detail.decode(errors="replace")
        detail = detail.strip()
        reason = detail.splitlines()[-1] if detail else str(exc)
        print_warning(f"Could not install Databricks AI Tools: {reason}")
    else:
        print_success("Databricks AI Tools installed")


def _profile_args(profile: str | None) -> list[str]:
    """Return ``["--profile", profile]`` when set, otherwise an empty list.

    Centralizing this keeps every `databricks` CLI invocation in this module
    consistent when a workspace's `~/.databrickscfg` has more than one profile
    pointing at the same host."""
    return ["--profile", profile] if profile else []


def has_valid_databricks_auth(workspace: str, profile: str | None = None) -> bool:
    # Honor the CI short-circuit (see ``get_databricks_token``): if a
    # pre-fetched bearer is available, treat auth as valid and skip the
    # `databricks auth token` shell-out (which only knows user-OAuth).
    if os.environ.get("DATABRICKS_BEARER", "").strip():
        return True
    log_auth_diagnostics()
    # Mirror run_databricks_login: when ~/.databrickscfg has multiple
    # profiles for the same host, `databricks auth token --host …` refuses
    # to disambiguate without --profile, so resolve it from the host here.
    profile = profile or find_profile_name_for_host(workspace)
    try:
        env = build_databricks_cli_env(workspace, profile)
        result = run(
            [
                "databricks",
                "auth",
                "token",
                "--host",
                workspace,
                *_profile_args(profile),
                "--output",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        debug(
            "has_valid_databricks_auth",
            format_subprocess_result(result),
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout or "{}")
        return bool(data.get("access_token"))
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as exc:
        debug("has_valid_databricks_auth", f"exception: {type(exc).__name__}: {exc}")
        return False


def list_profile_entries() -> list[dict]:
    """Return raw profile dicts ({"name", "host", "auth_type", ...}) from
    `databricks auth profiles`.

    Returns ``[]`` on any failure (CLI missing, timeout, non-zero exit, JSON
    decode error). When ``UCODE_DEBUG=1`` each dropout path logs *why* the
    result was empty so a silently-disappearing workspace picker is
    diagnosable from ``~/.ucode/debug.log``.
    """
    try:
        result = run(
            ["databricks", "auth", "profiles", "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        debug("list_profile_entries", f"subprocess error: {type(exc).__name__}: {exc}")
        return []
    if result.returncode != 0:
        debug("list_profile_entries", format_subprocess_result(result))
        return []
    try:
        profiles = json.loads(result.stdout or "{}").get("profiles") or []
    except json.JSONDecodeError as exc:
        debug("list_profile_entries", f"json decode error: {exc.msg}")
        return []
    return [p for p in profiles if isinstance(p, dict)]


def get_databricks_profiles() -> list[tuple[str, str]]:
    """Return [(host_url, profile_name), ...] from Databricks CLI profiles."""
    profiles = list_profile_entries()

    # dict dedupes by host (first non-PAT profile wins).
    out: dict[str, str] = {}
    pat = 0
    for p in profiles:
        host = (p.get("host") or "").rstrip("/")
        name = p.get("name")
        if not host or not name:
            continue
        if p.get("auth_type") == "pat":
            pat += 1
            continue
        out.setdefault(host, name)

    debug(
        "get_databricks_profiles",
        f"returned={len(out)} total={len(profiles)} pat={pat}",
    )
    return list(out.items())


def find_profile_name_for_host(workspace: str) -> str | None:
    """Find the Databricks CLI profile name matching a workspace URL."""
    normalized = workspace.rstrip("/")
    for host, name in get_databricks_profiles():
        if host == normalized:
            return name
    return None


def profile_auth_type(profile: str) -> str | None:
    """Return the auth_type of a Databricks CLI profile (e.g. "pat"), or None."""
    for p in list_profile_entries():
        if p.get("name") == profile:
            auth_type = p.get("auth_type")
            return auth_type if isinstance(auth_type, str) else None
    return None


def _read_databrickscfg_token(profile: str) -> str | None:
    """Read the static ``token`` value for a profile from ``~/.databrickscfg``.

    `databricks auth token` only knows OAuth caches; for PAT profiles the PAT
    itself is the credential, stored in the config file. The parser's default
    section is pointed at a name that never appears in the file so a token in
    ``[DEFAULT]`` does not leak into every named profile."""
    cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or "~/.databrickscfg").expanduser()
    parser = configparser.ConfigParser(default_section="@ucode-no-defaults@", interpolation=None)
    try:
        if not parser.read(cfg_path, encoding="utf-8"):
            return None
    except (configparser.Error, OSError):
        return None
    if not parser.has_section(profile):
        return None
    token = (parser.get(profile, "token", fallback="") or "").strip()
    return token or None


def resolve_pat_token(profile: str | None) -> str | None:
    """Return the static PAT of a PAT-type Databricks CLI profile, or None.

    Only consulted when the user explicitly opted in via
    ``ucode configure --profiles <name> --use-pat`` — ucode never picks up a
    PAT implicitly."""
    if profile and profile_auth_type(profile) == "pat":
        return _read_databrickscfg_token(profile)
    return None


def ensure_pat_bearer(profile: str | None, pat: str | None = None) -> bool:
    """Ensure ``DATABRICKS_BEARER`` holds a usable token for a ``--use-pat`` profile.

    If a non-empty bearer is already in the environment it wins (the CI escape
    hatch). Otherwise the profile's static PAT is exported — callers that have
    already resolved it (e.g. ``configure_shared_state``) pass it via ``pat`` to
    skip a redundant ``~/.databrickscfg`` read; everyone else lets this resolve
    it. An exported-but-*empty* ``DATABRICKS_BEARER`` is treated as absent —
    matching ``get_databricks_token``'s own ``.strip()`` check — so a stray
    ``export DATABRICKS_BEARER=`` does not shadow the PAT and silently force the
    OAuth path (which fails for PAT-only profiles).

    Returns ``True`` iff a usable bearer is now present in the environment."""
    if os.environ.get("DATABRICKS_BEARER", "").strip():
        return True
    pat = pat or resolve_pat_token(profile)
    if pat:
        os.environ["DATABRICKS_BEARER"] = pat
        return True
    return False


def apply_pat_environment(state: dict) -> None:
    """Export the configured profile's PAT as ``DATABRICKS_BEARER`` when the
    workspace was configured with ``--use-pat``.

    Every token fetch in this process (and in launched agent subprocesses,
    which inherit the environment) then takes the existing static-bearer
    short-circuit instead of the OAuth-only `databricks auth token` path.
    A non-empty bearer already present in the environment is left untouched."""
    if not state.get("use_pat"):
        return
    ensure_pat_bearer(state.get("profile"))


def run_databricks_login(workspace: str, profile: str | None = None) -> None:
    """Run databricks auth login unconditionally.

    When ``profile`` is provided, it is passed via ``--profile``. Otherwise we
    fall back to looking up an existing profile by host so a stored session is
    refreshed in place rather than overwriting another profile's tokens."""
    print_section("Databricks Login")
    print_kv("Workspace", workspace)
    print_note("A browser may open for `databricks auth login`.")
    try:
        profile_name = profile or find_profile_name_for_host(workspace)
        cmd = [
            "databricks",
            "auth",
            "login",
            "--host",
            workspace,
            *_profile_args(profile_name),
        ]
        run(cmd, env=build_databricks_cli_env(workspace, profile_name), timeout=300)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("`databricks auth login` failed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`databricks auth login` timed out.") from exc
    print_success("Databricks authentication complete")


def ensure_databricks_auth(
    workspace: str, profile: str | None = None, *, quiet: bool = False
) -> None:
    """Check auth and login only if needed (used by launch path).

    ``quiet`` suppresses the "already available" line for a caller that only needs a token before
    some later step re-authenticates and reports it — otherwise the same success prints twice. A
    login that actually runs is never silent.
    """
    with spinner("Checking Databricks auth..."):
        auth_is_valid = has_valid_databricks_auth(workspace, profile)
    if auth_is_valid:
        if not quiet:
            print_success(f"Databricks auth already available for {workspace}")
        return
    run_databricks_login(workspace, profile)


def get_databricks_token(
    workspace: str,
    profile: str | None = None,
    *,
    force_refresh: bool = False,
) -> str:
    # ``DATABRICKS_BEARER`` is the CI escape hatch: when set, skip the
    # `databricks auth token` subprocess entirely and return the pre-fetched
    # bearer directly. Used by the e2e job, where the protected runner has
    # no `databricks auth login` cache and `databricks auth token` only knows
    # how to read user-OAuth caches (not M2M client_credentials). Mirrors the
    # same short-circuit baked into ``build_auth_shell_command``.
    bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
    if bearer:
        debug("get_databricks_token", "using DATABRICKS_BEARER env var")
        return bearer

    log_auth_diagnostics()
    # See has_valid_databricks_auth: resolve the profile from the host when
    # the caller didn't supply one, so duplicate-host cfgs don't break us.
    profile = profile or find_profile_name_for_host(workspace)
    env = build_databricks_cli_env(workspace, profile)
    cmd = [
        "databricks",
        "auth",
        "token",
        "--host",
        workspace,
        *_profile_args(profile),
        "--output",
        "json",
    ]
    if force_refresh:
        cmd.append("--force-refresh")

    debug(
        "get_databricks_token.env",
        "set="
        + ",".join(sorted(k for k in env if k.startswith("DATABRICKS_") or k in {"BUNDLE_PROFILE"}))
        + f" profile={profile or '<none>'}",
    )

    def _fetch() -> str:
        try:
            result = run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            debug("auth token", format_subprocess_result(result))
            if result.returncode == 0:
                return json.loads(result.stdout or "{}").get("access_token", "")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            debug("auth token", f"exception: {type(exc).__name__}: {exc}")
        return ""

    token = _fetch()
    if not token:
        # Session may have expired — attempt non-interactive re-auth and retry once.
        debug("auth token", "empty on first fetch; attempting auth login --no-browser")
        try:
            reauth = run(
                [
                    "databricks",
                    "auth",
                    "login",
                    "--host",
                    workspace,
                    *_profile_args(profile),
                    "--no-browser",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            debug("auth login", format_subprocess_result(reauth))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            debug("auth login", f"exception: {type(exc).__name__}: {exc}")
        token = _fetch()

    if not token:
        profile_name = profile or find_profile_name_for_host(workspace)
        stale_profile_hint = ""
        if profile_name:
            stale_profile_hint = (
                " The saved Databricks CLI profile may be stale or invalid. Try:\n"
                f"  databricks auth logout --profile {profile_name}\n"
                f"  databricks auth login --host {workspace} --profile {profile_name}"
            )
        raise RuntimeError(
            f"Databricks CLI returned no access token for {workspace}. "
            "Run `databricks auth login` to re-authenticate."
            f"{stale_profile_hint}"
        )
    return token


def _ucode_binary() -> str:
    """Resolve the absolute path to the running `ucode` executable.

    Agents persist the auth command into config files and re-run it on every
    token refresh, possibly from launchers without a full PATH (desktop GUIs).
    An absolute path keeps the helper working regardless of PATH. Falls back to
    the bare name when resolution fails."""
    return shutil.which("ucode") or "ucode"


def build_auth_token_argv(
    workspace: str, profile: str | None = None, *, use_pat: bool = False
) -> list[str]:
    """Argv for the cross-platform token helper: `ucode auth-token ...`.

    Unlike the previous POSIX `databricks ... | jq` pipeline, this is a single
    executable with plain arguments — no `sh`, no `jq`, no shell quoting — so it
    runs identically on macOS, Linux, and Windows (issue #116). The DATABRICKS_BEARER
    short-circuit and the PAT path both live inside `auth-token` itself."""
    argv = [_ucode_binary(), "auth-token", "--host", workspace.rstrip("/")]
    if profile:
        argv += ["--profile", profile]
    if use_pat:
        argv.append("--use-pat")
    return argv


def build_mcp_proxy_argv(
    url: str, workspace: str, profile: str | None = None, *, use_pat: bool = False
) -> list[str]:
    """Argv for the stdio MCP bridge: `ucode mcp-proxy --url ... --host ...`.

    Every coding agent registers this single command as a local stdio MCP
    server instead of a per-client HTTP endpoint with a bearer header. The proxy
    forwards to ``url`` and mints a fresh OAuth token on each upstream request,
    so tokens never expire mid-session — the client only ever spawns a process,
    which keeps registration uniform across CLIs that disagree on HTTP-auth
    syntax. Like `build_auth_token_argv`, this resolves the absolute `ucode`
    path and passes plain arguments (no shell), so it runs identically on every
    platform."""
    argv = [_ucode_binary(), "mcp-proxy", "--url", url, "--host", workspace.rstrip("/")]
    if profile:
        argv += ["--profile", profile]
    if use_pat:
        argv.append("--use-pat")
    return argv


def build_auth_shell_command(
    workspace: str, profile: str | None = None, *, use_pat: bool = False
) -> str:
    """Single-line, shell-quoted form of :func:`build_auth_token_argv`.

    Used by the derived Pi state contract, which exposes the helper as one command
    string. On every platform this resolves to the `ucode auth-token` executable
    rather than a POSIX shell pipeline, so no `sh`/`jq` is required."""
    argv = build_auth_token_argv(workspace, profile, use_pat=use_pat)
    if platform.system() == "Windows":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)
