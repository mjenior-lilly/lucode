"""Focused tests for the Databricks auth concern."""

from __future__ import annotations

import os
import subprocess

import pytest

import ucode.databricks.auth as db_mod
from ucode.databricks.auth import (
    _parse_databricks_cli_version,
    _run_databricks_cli_installer,
    build_auth_shell_command,
    build_auth_token_argv,
    build_databricks_cli_env,
    ensure_databricks_cli_version,
    ensure_pat_bearer,
    get_databricks_token,
    install_ai_tools,
)

WS = "https://example.databricks.com"


class TestBuildDatabricksCliEnv:
    def test_sets_databricks_host(self):
        env = build_databricks_cli_env(WS)
        assert env["DATABRICKS_HOST"] == WS

    def test_strips_ambient_profile_without_explicit_profile(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "other-workspace")

        env = build_databricks_cli_env(WS)

        assert env["DATABRICKS_HOST"] == WS
        assert "DATABRICKS_CONFIG_PROFILE" not in env

    def test_preserves_ambient_profile_with_explicit_profile(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "other-workspace")

        env = build_databricks_cli_env(WS, profile="stablebox")

        assert env["DATABRICKS_HOST"] == WS
        assert env["DATABRICKS_CONFIG_PROFILE"] == "other-workspace"


class TestResolvePatToken:
    def test_reads_pat_profile_token_from_cfg(self, monkeypatch, tmp_path):
        cfg = tmp_path / "databrickscfg"
        cfg.write_text(f"[lakebox]\nhost = {WS}\ntoken = dapi-from-cfg\n")
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
        monkeypatch.setattr(
            db_mod,
            "list_profile_entries",
            lambda: [{"name": "lakebox", "host": WS, "auth_type": "pat"}],
        )
        assert db_mod.resolve_pat_token("lakebox") == "dapi-from-cfg"

    def test_default_section_token_does_not_leak_into_named_profiles(self, monkeypatch, tmp_path):
        cfg = tmp_path / "databrickscfg"
        cfg.write_text(
            f"[DEFAULT]\nhost = {WS}\ntoken = dapi-default\n"
            "[other]\nhost = https://other.databricks.com\n"
        )
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
        monkeypatch.setattr(
            db_mod,
            "list_profile_entries",
            lambda: [
                {"name": "DEFAULT", "host": WS, "auth_type": "pat"},
                {"name": "other", "host": "https://other.databricks.com", "auth_type": "pat"},
            ],
        )
        assert db_mod.resolve_pat_token("DEFAULT") == "dapi-default"
        assert db_mod.resolve_pat_token("other") is None

    def test_returns_none_for_oauth_profile(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "list_profile_entries",
            lambda: [{"name": "oauth", "host": WS, "auth_type": "databricks-cli"}],
        )
        assert db_mod.resolve_pat_token("oauth") is None

    def test_returns_none_without_profile(self):
        assert db_mod.resolve_pat_token(None) is None


class TestApplyPatEnvironment:
    @pytest.fixture(autouse=True)
    def _isolated_bearer(self):
        # apply_pat_environment writes os.environ directly; restore it even
        # though monkeypatch can't track writes made by code under test.
        original = os.environ.pop("DATABRICKS_BEARER", None)
        yield
        if original is None:
            os.environ.pop("DATABRICKS_BEARER", None)
        else:
            os.environ["DATABRICKS_BEARER"] = original

    def test_exports_bearer_for_use_pat_state(self, monkeypatch):
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: "dapi-pat")

        db_mod.apply_pat_environment({"use_pat": True, "profile": "DEFAULT"})

        assert os.environ["DATABRICKS_BEARER"] == "dapi-pat"

    def test_noop_without_use_pat(self, monkeypatch):
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: "dapi-pat")

        db_mod.apply_pat_environment({"profile": "DEFAULT"})

        assert "DATABRICKS_BEARER" not in os.environ

    def test_existing_bearer_wins(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_BEARER", "explicit-bearer")
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: "dapi-pat")

        db_mod.apply_pat_environment({"use_pat": True, "profile": "DEFAULT"})

        assert os.environ["DATABRICKS_BEARER"] == "explicit-bearer"


class TestBuildAuthTokenArgv:
    def test_basic_argv(self):
        argv = build_auth_token_argv(WS)
        # First element resolves to the ucode executable; the rest is the
        # cross-platform helper invocation — no `sh`, no `jq`, no shell syntax.
        assert argv[0].endswith("ucode") or argv[0] == "ucode"
        assert argv[1:] == ["auth-token", "--host", WS]

    def test_strips_trailing_slash_from_host(self):
        argv = build_auth_token_argv(WS + "/")
        assert "--host" in argv
        assert argv[argv.index("--host") + 1] == WS

    def test_embeds_profile_when_provided(self):
        argv = build_auth_token_argv(WS, profile="stablebox")
        assert argv[argv.index("--profile") + 1] == "stablebox"

    def test_profile_passed_as_separate_argv_element(self):
        # Metacharacters need no shell quoting — argv is never parsed by a shell.
        argv = build_auth_token_argv(WS, profile="weird name; rm -rf /")
        assert "weird name; rm -rf /" in argv

    def test_use_pat_flag(self):
        argv = build_auth_token_argv(WS, profile="DEFAULT", use_pat=True)
        assert "--use-pat" in argv
        assert argv[argv.index("--profile") + 1] == "DEFAULT"

    def test_no_use_pat_flag_by_default(self):
        assert "--use-pat" not in build_auth_token_argv(WS)


class TestBuildAuthShellCommand:
    def test_contains_workspace(self):
        cmd = build_auth_shell_command(WS)
        assert WS in cmd

    def test_is_ucode_auth_token_invocation(self):
        # The persisted helper now points at the `ucode auth-token` executable
        # on every platform — not a POSIX `databricks ... | jq` pipeline.
        cmd = build_auth_shell_command(WS)
        assert "auth-token" in cmd
        assert "--host" in cmd
        # POSIX-only constructs that broke Windows (#116) must be gone.
        assert "jq" not in cmd
        assert "if [ -n" not in cmd

    def test_embeds_profile_when_provided(self):
        cmd = build_auth_shell_command(WS, profile="stablebox")
        assert "--profile stablebox" in cmd

    def test_quotes_profile_shell_metacharacters(self):
        cmd = build_auth_shell_command(WS, profile="weird name; rm -rf /")
        # On POSIX shlex.join quotes the value so the string form cannot be
        # interpreted as a shell injection if a tool runs it via a shell.
        if os.name != "nt":
            assert "'weird name; rm -rf /'" in cmd

    def test_use_pat_emits_flag(self):
        cmd = build_auth_shell_command(WS, profile="DEFAULT", use_pat=True)
        assert "--use-pat" in cmd
        assert "--profile DEFAULT" in cmd


class TestEnsurePatBearer:
    """ensure_pat_bearer is the empty-aware DATABRICKS_BEARER export used by the
    --use-pat path on configure, launch, and the auth-token helper."""

    @pytest.fixture(autouse=True)
    def _isolated_bearer(self):
        # ensure_pat_bearer writes os.environ directly; restore it even though
        # monkeypatch can't track writes made by code under test.
        original = os.environ.pop("DATABRICKS_BEARER", None)
        yield
        if original is None:
            os.environ.pop("DATABRICKS_BEARER", None)
        else:
            os.environ["DATABRICKS_BEARER"] = original

    def test_exports_pat_when_env_absent(self, monkeypatch):
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: "dapi-pat")
        assert ensure_pat_bearer("p") is True
        assert os.environ["DATABRICKS_BEARER"] == "dapi-pat"

    def test_overwrites_empty_env(self, monkeypatch):
        # The regression: an empty DATABRICKS_BEARER must be treated as absent
        # so the PAT is still exported (old `if [ -n ... ]` parity).
        monkeypatch.setenv("DATABRICKS_BEARER", "")
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: "dapi-pat")
        assert ensure_pat_bearer("p") is True
        assert os.environ["DATABRICKS_BEARER"] == "dapi-pat"

    def test_non_empty_env_wins_without_resolving(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_BEARER", "ci-bearer")
        called = []
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: called.append(p) or "dapi-pat")
        assert ensure_pat_bearer("p") is True
        # Pre-set bearer is honored; we don't even read the PAT.
        assert os.environ["DATABRICKS_BEARER"] == "ci-bearer"
        assert called == []

    def test_returns_false_when_no_pat(self, monkeypatch):
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: None)
        assert ensure_pat_bearer("p") is False
        assert "DATABRICKS_BEARER" not in os.environ

    def test_whitespace_only_env_treated_as_empty(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_BEARER", "   ")
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: "dapi-pat")
        assert ensure_pat_bearer("p") is True
        assert os.environ["DATABRICKS_BEARER"] == "dapi-pat"

    def test_explicit_pat_arg_skips_cfg_read(self, monkeypatch):
        # Callers that already resolved the PAT (configure_shared_state) pass it
        # in; ensure_pat_bearer must use it without re-reading ~/.databrickscfg.
        called = []
        monkeypatch.setattr(db_mod, "resolve_pat_token", lambda p: called.append(p) or "from-cfg")
        assert ensure_pat_bearer("p", "explicit-pat") is True
        assert os.environ["DATABRICKS_BEARER"] == "explicit-pat"
        assert called == []


class TestGetDatabricksToken:
    def _fake_databricks(self, tmp_path, script: str) -> dict:
        fake = tmp_path / "databricks"
        fake.write_text(f"#!/bin/sh\n{script}\n")
        fake.chmod(0o755)
        return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    def test_returns_token_on_success(self, tmp_path, monkeypatch):
        env = self._fake_databricks(
            tmp_path,
            'echo \'{"access_token": "good-token", "token_type": "Bearer"}\'',
        )
        monkeypatch.setattr("os.environ", env)
        token = get_databricks_token(WS)
        assert token == "good-token"

    def test_strips_ambient_profile_when_profile_not_provided(self, tmp_path, monkeypatch):
        profile_log = tmp_path / "profile"
        env = self._fake_databricks(
            tmp_path,
            f'printf "%s" "${{DATABRICKS_CONFIG_PROFILE:-}}" > {profile_log}\n'
            'echo \'{"access_token": "good-token", "token_type": "Bearer"}\'',
        )
        env["DATABRICKS_CONFIG_PROFILE"] = "other-workspace"
        monkeypatch.setattr("os.environ", env)

        token = get_databricks_token(WS)

        assert token == "good-token"
        assert profile_log.read_text() == ""

    def test_has_valid_auth_strips_ambient_profile_without_explicit_profile(
        self, tmp_path, monkeypatch
    ):
        profile_log = tmp_path / "profile"
        env = self._fake_databricks(
            tmp_path,
            f'printf "%s" "${{DATABRICKS_CONFIG_PROFILE:-}}" > {profile_log}\n'
            'echo \'{"access_token": "good-token", "token_type": "Bearer"}\'',
        )
        env["DATABRICKS_CONFIG_PROFILE"] = "other-workspace"
        monkeypatch.setattr("os.environ", env)

        assert db_mod.has_valid_databricks_auth(WS)
        assert profile_log.read_text() == ""

    def test_reauths_and_retries_when_token_empty(self, tmp_path, monkeypatch):
        call_count = tmp_path / "calls"
        call_count.write_text("0")
        env = self._fake_databricks(
            tmp_path,
            f"count=$(cat {call_count})\n"
            f"echo $((count + 1)) > {call_count}\n"
            'case "$*" in\n'
            '  *"auth login"*) exit 0 ;;\n'
            "esac\n"
            'if [ "$count" -eq 0 ]; then\n'
            '  echo \'{"access_token": "", "token_type": "Bearer"}\'\n'
            "else\n"
            '  echo \'{"access_token": "refreshed-token", "token_type": "Bearer"}\'\n'
            "fi",
        )
        monkeypatch.setattr("os.environ", env)
        token = get_databricks_token(WS)
        assert token == "refreshed-token"

    def test_raises_when_reauth_also_fails(self, tmp_path, monkeypatch):
        env = self._fake_databricks(
            tmp_path,
            'echo \'{"access_token": "", "token_type": "Bearer"}\'',
        )
        monkeypatch.setattr("os.environ", env)
        with pytest.raises(RuntimeError, match="no access token"):
            get_databricks_token(WS)

    def test_passes_profile_flag_when_provided(self, tmp_path, monkeypatch):
        # Fake CLI that records its argv to a file so we can assert the
        # --profile flag is forwarded to `databricks auth token`.
        argv_log = tmp_path / "argv"
        env = self._fake_databricks(
            tmp_path,
            f'printf "%s\\n" "$@" >> {argv_log}\n'
            'echo \'{"access_token": "good-token", "token_type": "Bearer"}\'',
        )
        monkeypatch.setattr("os.environ", env)
        token = get_databricks_token(WS, profile="stablebox")
        assert token == "good-token"
        argv = argv_log.read_text().splitlines()
        assert "--profile" in argv
        assert argv[argv.index("--profile") + 1] == "stablebox"

    def test_error_suggests_logout_when_matching_profile_exists(self, tmp_path, monkeypatch):
        env = self._fake_databricks(
            tmp_path,
            'case "$*" in\n'
            '  *"auth profiles"*) echo \'{"profiles": [{"host": "'
            + WS
            + '", "name": "example-profile", "auth_type": "databricks-cli"}]}\'; exit 0 ;;\n'
            '  *"auth login"*) exit 0 ;;\n'
            "esac\n"
            'echo \'{"access_token": "", "token_type": "Bearer"}\'',
        )
        monkeypatch.setattr("os.environ", env)

        with pytest.raises(RuntimeError) as exc_info:
            get_databricks_token(WS)

        message = str(exc_info.value)
        assert "stale or invalid" in message
        assert "databricks auth logout --profile example-profile" in message
        assert f"databricks auth login --host {WS} --profile example-profile" in message


class TestParseDatabricksCliVersion:
    def test_parses_standard_format(self):
        assert _parse_databricks_cli_version("Databricks CLI v0.299.2") == (0, 299, 2)

    def test_parses_without_v_prefix(self):
        assert _parse_databricks_cli_version("Databricks CLI 0.298.0") == (0, 298, 0)

    def test_returns_none_on_garbage(self):
        assert _parse_databricks_cli_version("not a version") is None


class TestEnsureDatabricksCliVersion:
    def _fake_databricks(self, tmp_path, version_output: str) -> dict:
        fake = tmp_path / "databricks"
        fake.write_text(f"#!/bin/sh\necho '{version_output}'\n")
        fake.chmod(0o755)
        return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    def test_passes_when_version_meets_minimum(self, tmp_path, monkeypatch):
        env = self._fake_databricks(tmp_path, "Databricks CLI v1.0.0")
        monkeypatch.setattr("os.environ", env)
        ensure_databricks_cli_version()  # should not raise

    def test_passes_when_version_exceeds_minimum(self, tmp_path, monkeypatch):
        env = self._fake_databricks(tmp_path, "Databricks CLI v1.8.0")
        monkeypatch.setattr("os.environ", env)
        ensure_databricks_cli_version()

    def test_auto_upgrades_when_version_too_old(self, tmp_path, monkeypatch):
        import ucode.databricks.auth as db_mod

        env = self._fake_databricks(tmp_path, "Databricks CLI v0.299.2")
        monkeypatch.setattr("os.environ", env)
        upgraded = []
        monkeypatch.setattr(
            db_mod,
            "_run_databricks_cli_installer",
            lambda brew_subcommand="install": upgraded.append(brew_subcommand),
        )
        # Stop the recursive re-check after upgrade
        call_count = [0]
        original = db_mod.ensure_databricks_cli_version

        def once(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                original()

        monkeypatch.setattr(db_mod, "ensure_databricks_cli_version", once)
        once()
        assert upgraded == ["upgrade"]

    def test_raises_when_version_unparseable(self, tmp_path, monkeypatch):
        env = self._fake_databricks(tmp_path, "completely broken output")
        monkeypatch.setattr("os.environ", env)
        with pytest.raises(RuntimeError, match="Could not parse"):
            ensure_databricks_cli_version()


class TestRunDatabricksCliInstaller:
    @pytest.mark.parametrize("brew_subcommand", ["install", "upgrade"])
    def test_macos_uses_fully_qualified_tap_formula(self, monkeypatch, brew_subcommand):
        calls = []
        monkeypatch.setattr(db_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(db_mod.shutil, "which", lambda cmd: "/opt/homebrew/bin/brew")
        monkeypatch.setattr(db_mod, "run", lambda cmd, **kw: calls.append(cmd))

        _run_databricks_cli_installer(brew_subcommand=brew_subcommand)

        # The fully-qualified formula forces Homebrew to the Databricks CLI in
        # databricks/tap and fails if absent, rather than falling back to the
        # unrelated `databricks` cask.
        assert calls == [["brew", brew_subcommand, "databricks/tap/databricks"]]


class TestInstallAiTools:
    def _capture_run(self, monkeypatch, *, raises=None):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(args, 0, "Installed 1 skill.", "")

        monkeypatch.setattr(db_mod, "run", fake_run)
        return calls

    def test_no_tokens_skips_entirely(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        install_ai_tools([])
        assert calls == []

    def test_invokes_aitools_install(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        install_ai_tools(["opencode"])
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[:3] == ["databricks", "aitools", "install"]
        assert "--agents" in cmd and cmd[cmd.index("--agents") + 1] == "opencode"
        assert "--scope" in cmd and cmd[cmd.index("--scope") + 1] == "global"
        assert "--profile" not in cmd

    def test_passes_profile_when_set(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        install_ai_tools(["opencode"], profile="myprofile")
        cmd = calls[0]
        assert "--profile" in cmd and cmd[cmd.index("--profile") + 1] == "myprofile"

    def test_install_failure_is_non_fatal(self, monkeypatch):
        self._capture_run(monkeypatch, raises=subprocess.CalledProcessError(1, "databricks"))
        # Must not raise — AI Tools are best-effort.
        install_ai_tools(["opencode"])

    def test_timeout_is_non_fatal(self, monkeypatch):
        self._capture_run(monkeypatch, raises=subprocess.TimeoutExpired("databricks", 300))
        install_ai_tools(["opencode"])

    def test_timeout_stderr_bytes_decoded_in_warning(self, monkeypatch):
        # TimeoutExpired.stderr is bytes even with text=True; the warning must
        # decode it, not render a `b'...'` repr.
        err = subprocess.TimeoutExpired("databricks", 300)
        err.stderr = b"resolving agents...\ninstall timed out"
        self._capture_run(monkeypatch, raises=err)
        warnings = []
        monkeypatch.setattr(db_mod, "print_warning", warnings.append)
        install_ai_tools(["opencode"])
        assert len(warnings) == 1
        assert "install timed out" in warnings[0]
        assert "b'" not in warnings[0]

    def test_failure_surfaces_cli_stderr(self, monkeypatch):
        # A modern CLI can still fail (e.g. an agent binary missing from PATH);
        # the warning must show the CLI's real error, not blame the version.
        err = subprocess.CalledProcessError(1, "databricks")
        err.stderr = "resolving agents...\nopencode: cli-not-on-path: could not resolve opencode"
        self._capture_run(monkeypatch, raises=err)
        warnings = []
        monkeypatch.setattr(db_mod, "print_warning", warnings.append)
        install_ai_tools(["opencode"])
        assert len(warnings) == 1
        assert "opencode: cli-not-on-path: could not resolve opencode" in warnings[0]
