"""Shared fixtures for E2E tests + global state-isolation guard."""

from __future__ import annotations

import os

import pytest

from lucode.databricks.auth import get_databricks_token
from lucode.databricks.models import (
    build_shared_base_urls,
    discover_codex_models,
    discover_gemini_models,
    discover_model_services,
)
from lucode.ui import normalize_workspace_url


@pytest.fixture(autouse=True)
def _isolate_lucode_state(tmp_path, monkeypatch):
    """Redirect lucode's state file and APP_DIR to a per-test tmp dir.

    Defense in depth: even if an individual test forgets to patch save_state,
    it can never touch the developer's real ~/.lucode/state.json.
    """
    import lucode.config_io as config_io_mod
    import lucode.databricks.models as databricks_models
    import lucode.state as state_mod

    state_dir = tmp_path / ".lucode"
    state_dir.mkdir()
    monkeypatch.setattr(state_mod, "STATE_PATH", state_dir / "state.json")
    monkeypatch.setattr(config_io_mod, "APP_DIR", state_dir)
    # The model-services listing is memoized for the life of the process, so without this a cached
    # result would leak into the next test and make a stubbed listing look like it was never called.
    databricks_models.clear_model_services_cache()


def _workspace() -> str:
    ws = os.environ.get("lucode_TEST_WORKSPACE", "").strip().rstrip("/")
    return normalize_workspace_url(ws) if ws else ""


@pytest.fixture(scope="session")
def e2e_workspace():
    ws = _workspace()
    if not ws:
        pytest.skip("Set lucode_TEST_WORKSPACE=https://... to run E2E tests")
    return ws


@pytest.fixture(scope="session")
def e2e_token(e2e_workspace):
    return get_databricks_token(e2e_workspace)


@pytest.fixture(scope="session")
def e2e_state(e2e_workspace, e2e_token):
    """Full state dict mirroring what configure_shared_state produces.

    Built from production discovery (``discover_model_services``, falling back to the per-family
    AI Gateway listings), so the fixture stays in step with the two-harness discovery matrix:
    OpenCode consumes Anthropic/Gemini/OSS; Pi consumes Anthropic/OpenAI-Responses/Gemini.
    """
    ms_claude, ms_codex, ms_gemini, ms_oss, _ = discover_model_services(e2e_workspace, e2e_token)
    claude_models = ms_claude
    claude_models.pop("fable", None)
    gemini_models = ms_gemini or discover_gemini_models(e2e_workspace, e2e_token)[0]
    codex_models = ms_codex or discover_codex_models(e2e_workspace, e2e_token)[0]
    oss_models = ms_oss

    opencode_models: dict = {}
    if claude_models:
        opencode_models["anthropic"] = list(claude_models.values())
    if gemini_models:
        opencode_models["gemini"] = gemini_models
    if oss_models:
        opencode_models["oss"] = oss_models

    return {
        "workspace": e2e_workspace,
        "claude_models": claude_models,
        "gemini_models": gemini_models,
        "codex_models": codex_models,
        "oss_models": oss_models,
        "opencode_models": opencode_models,
        "base_urls": build_shared_base_urls(e2e_workspace),
        "managed_configs": {},
    }
