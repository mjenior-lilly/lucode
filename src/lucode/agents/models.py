"""Pure default-model selection shared by agent writers and persisted-state hydration."""

from __future__ import annotations

from lucode.databricks.models import ANTHROPIC_FAMILIES, classify_model_family


def pi_default_model(state: dict) -> str | None:
    """Select Pi's default deterministically from resolved workspace state."""
    managed_default = state.get("pi_default_model")
    if isinstance(managed_default, str) and managed_default:
        return managed_default

    managed_models = state.get("pi_models")
    if isinstance(managed_models, list):
        for model in managed_models:
            if (
                isinstance(model, str)
                and model
                and classify_model_family(model) in (*ANTHROPIC_FAMILIES, "codex", "gemini")
            ):
                return model

    claude_models = state.get("claude_models") or {}
    if isinstance(claude_models, dict):
        for family in ANTHROPIC_FAMILIES:
            model = claude_models.get(family)
            if isinstance(model, str) and model:
                return model

    for key in ("codex_models", "gemini_models"):
        models = state.get(key) or []
        if isinstance(models, list):
            for model in models:
                if isinstance(model, str) and model:
                    return model
    return None


def opencode_default_model(state: dict) -> str | None:
    """Select OpenCode's default deterministically from resolved workspace state."""
    managed_default = state.get("opencode_default_model")
    if isinstance(managed_default, str) and managed_default:
        return managed_default
    opencode_models = state.get("opencode_models") or {}
    if not isinstance(opencode_models, dict):
        return None
    for provider in ("anthropic", "gemini", "oss"):
        models = opencode_models.get(provider) or []
        if isinstance(models, list) and models:
            model = models[0]
            if isinstance(model, str) and model:
                return model
    return None
