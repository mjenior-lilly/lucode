"""End-to-end integration tests that require a live Databricks workspace.

Run with:
    LUCODE_TEST_WORKSPACE=https://your-workspace.databricks.com uv run pytest tests/test_e2e.py -v

All tests in this file are skipped automatically when the env var is not set.
The agent-launch tests are also skipped per-agent/model when the binary is not
installed or no models are available.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from lucode.databricks.auth import has_valid_databricks_auth
from lucode.databricks.models import (
    build_shared_base_urls,
    ensure_ai_gateway_v2,
)
from lucode.databricks.transport import workspace_hostname
from lucode.ui import normalize_workspace_url


def _ws() -> str:
    raw = os.environ.get("LUCODE_TEST_WORKSPACE", "").strip().rstrip("/")
    return normalize_workspace_url(raw) if raw else ""


def _run_agent(
    cmd: list[str], env: dict | None = None, timeout: int = 60
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL
    )


def _launchable_model_items(models: dict) -> list[tuple[str, str]]:
    return [(family, model_id) for family, model_id in models.items() if model_id]


class TestDatabricksAuth:
    def test_has_valid_auth(self, e2e_workspace):
        assert has_valid_databricks_auth(e2e_workspace), (
            "No valid Databricks auth found. Run `databricks auth login` first."
        )

    def test_get_token_returns_non_empty_string(self, e2e_token):
        assert isinstance(e2e_token, str) and len(e2e_token) > 10


class TestAiGatewayV2:
    def test_ensure_ai_gateway_v2_does_not_raise(self, e2e_workspace, e2e_token):
        ensure_ai_gateway_v2(e2e_workspace, e2e_token)

    def test_workspace_hostname_resolves(self, e2e_workspace):
        hostname = workspace_hostname(e2e_workspace)
        assert "." in hostname


class TestModelDiscovery:
    def test_discovers_model_families(self, e2e_workspace, e2e_token):
        from lucode.databricks.models import discover_model_services

        anthropic, openai, gemini, oss, reason = discover_model_services(e2e_workspace, e2e_token)
        if not any((anthropic, openai, gemini, oss)):
            pytest.skip(f"No supported model services found: {reason}")
        assert "fable" not in anthropic


class TestUrlBuilders:
    def test_shared_base_urls_only_surviving_tools(self, e2e_workspace):
        urls = build_shared_base_urls(e2e_workspace)
        assert set(urls) == {"opencode", "pi"}
        assert e2e_workspace in urls["opencode"]["anthropic"]
        assert e2e_workspace in urls["pi"]["openai"]


class TestStateRoundTrip:
    def test_configure_shared_state_and_reload(
        self, tmp_path, monkeypatch, e2e_state, e2e_workspace
    ):
        import lucode.config as config_mod
        import lucode.state as state_mod
        from lucode.state import load_state, save_state

        monkeypatch.setattr(state_mod, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        save_state(e2e_state)
        loaded = load_state()
        assert loaded["workspace"] == e2e_workspace
        assert loaded["claude_models"] == e2e_state["claude_models"]
        assert loaded["base_urls"]["pi"]["openai"] == f"{e2e_workspace}/ai-gateway/codex/v1"


def _require_binary(binary: str):
    if not shutil.which(binary):
        pytest.skip(f"`{binary}` is not installed")


class TestOpencodeLaunch:
    """Run opencode against every available opencode model (anthropic + gemini)."""

    SKIP_MODELS: frozenset[str] = frozenset(
        {"databricks-gemini-3-1-flash-lite", "databricks-gemini-3-1-flash-lite-image"}
    )

    def _all_models(self, e2e_state: dict) -> list[tuple[str, str]]:
        """Return [(provider, model_id), ...] for all opencode models."""
        opencode_models: dict = e2e_state.get("opencode_models") or {}
        out: list[tuple[str, str]] = []
        for provider, models in opencode_models.items():
            for model in models or []:
                if model in self.SKIP_MODELS:
                    continue
                out.append((provider, model))
        return out

    def test_launch_opencode_per_model(
        self, tmp_path, monkeypatch, e2e_state, e2e_workspace, e2e_token
    ):
        import lucode.agents.opencode as opencode
        import lucode.config as config_mod

        _require_binary("opencode")
        models = self._all_models(e2e_state)
        if not models:
            pytest.skip("No OpenCode models available on this workspace")
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        xdg = tmp_path / "opencode-xdg"
        config_path = xdg / "opencode" / "opencode.json"
        backup_path = tmp_path / "opencode-config.backup.json"
        monkeypatch.setattr(opencode, "OPENCODE_XDG_CONFIG_HOME", xdg)
        monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", config_path)
        monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", backup_path)
        import sys
        import time

        print(f"\n[opencode-per-model] {len(models)} models to test", flush=True)
        failures = []
        for provider, model in models:
            if config_path.exists():
                config_path.unlink()
            with pytest.MonkeyPatch().context() as mp:
                mp.setattr("lucode.state.save_state", lambda s: None)
                mp.setattr(
                    "lucode.agents.opencode.get_databricks_token",
                    lambda ws, profile=None, **kwargs: e2e_token,
                )
                opencode.write_tool_config(
                    {**e2e_state, "workspace": e2e_workspace}, model, token=e2e_token
                )
            cmd = opencode.validate_cmd("opencode")
            print(f"[opencode-per-model] -> {provider}/{model}", flush=True)
            t0 = time.monotonic()
            try:
                result = _run_agent(cmd, env=opencode.build_runtime_env(e2e_token), timeout=180)
            except subprocess.TimeoutExpired as exc:
                elapsed = time.monotonic() - t0
                partial_stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
                partial_stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
                print(
                    f"[opencode-per-model] {provider}/{model} TIMEOUT after {elapsed:.1f}s\n  partial stdout: {partial_stdout[:500]!r}\n  partial stderr: {partial_stderr[:500]!r}",
                    flush=True,
                    file=sys.stderr,
                )
                failures.append(
                    f"provider={provider} model={model} TIMEOUT after {elapsed:.1f}s stderr={partial_stderr[:300]!r}"
                )
                continue
            elapsed = time.monotonic() - t0
            combined = (result.stdout + result.stderr).strip()
            status = "OK" if result.returncode == 0 and combined else f"FAIL rc={result.returncode}"
            print(f"[opencode-per-model] {provider}/{model} {status} ({elapsed:.1f}s)", flush=True)
            if result.returncode != 0 or not combined:
                failures.append(
                    f"provider={provider} model={model} rc={result.returncode} elapsed={elapsed:.1f}s stdout={result.stdout[:300]!r} stderr={result.stderr[:300]!r}"
                )
        assert not failures, "OpenCode launch failures:\n" + "\n".join(failures)


class TestPiLaunch:
    """Run pi against every available model across all four providers.

    Pi has dedicated providers per family (claude, codex, gemini, oss); this
    test exercises each one end-to-end through the validation path.
    """

    def _all_models(self, e2e_state: dict) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        claude_models: dict = e2e_state.get("claude_models") or {}
        for family, model_id in _launchable_model_items(claude_models):
            out.append((f"claude-{family}", model_id))
        for model in e2e_state.get("codex_models") or []:
            out.append(("codex", model))
        for model in e2e_state.get("gemini_models") or []:
            out.append(("gemini", model))
        return out

    def test_launch_pi_per_model(self, tmp_path, monkeypatch, e2e_state, e2e_workspace, e2e_token):
        import lucode.agents.pi as pi
        import lucode.config as config_mod

        _require_binary("pi")
        models = self._all_models(e2e_state)
        if not models:
            pytest.skip("No Pi-compatible models available on this workspace")
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        pi_home = tmp_path / "pi-home"
        pi_dir = pi_home / ".pi" / "agent"
        config_path = pi_dir / "models.json"
        backup_path = tmp_path / "pi-models.backup.json"
        monkeypatch.setattr(pi, "PI_lucode_HOME", pi_home)
        monkeypatch.setattr(pi, "PI_CONFIG_PATH", config_path)
        monkeypatch.setattr(pi, "PI_BACKUP_PATH", backup_path)
        failures = []
        for family, model in models:
            if config_path.exists():
                config_path.unlink()
            with pytest.MonkeyPatch().context() as mp:
                mp.setattr("lucode.state.save_state", lambda s: None)
                mp.setattr(
                    "lucode.agents.pi.get_databricks_token",
                    lambda ws, profile=None, **kwargs: e2e_token,
                )
                pi.write_tool_config(
                    {**e2e_state, "workspace": e2e_workspace}, model, token=e2e_token
                )
            env = pi.build_runtime_env(e2e_token)
            cmd = pi.validate_cmd("pi")
            result = _run_agent(cmd, env=env, timeout=120)
            combined = (result.stdout + result.stderr).strip()
            if result.returncode != 0 or not combined:
                failures.append(
                    f"family={family} model={model} rc={result.returncode} stdout={result.stdout[:300]!r} stderr={result.stderr[:300]!r}"
                )
        assert not failures, "Pi launch failures:\n" + "\n".join(failures)
