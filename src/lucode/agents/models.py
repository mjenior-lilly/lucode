"""Pure default-model selection shared by agent writers and persisted-state hydration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy

from lucode.databricks.models import ANTHROPIC_FAMILIES, classify_model_family


def resolve_model_ids(
    managed_ids: Iterable[str] | None,
    existing_ids: Iterable[str] | None,
    discovered_ids: Iterable[str],
) -> list[str]:
    """Resolve membership from managed inventory, user config, then discovery."""
    source = managed_ids if managed_ids is not None else existing_ids
    if source is None:
        source = discovered_ids
    return [model_id for model_id in source if isinstance(model_id, str) and model_id]


def layer_model_entries(
    model_ids: Iterable[str],
    existing_entries: Mapping[str, dict],
    packaged_parameters: Callable[[str], dict],
) -> dict[str, dict]:
    """Layer packaged tuning under user entries for the resolved membership."""
    layered: dict[str, dict] = {}
    for model_id in model_ids:
        entry = packaged_parameters(model_id)
        existing = existing_entries.get(model_id)
        if isinstance(existing, dict):
            entry.update(deepcopy(existing))
        layered[model_id] = entry
    return layered


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
    for key in ("opencode_managed_models", "opencode_models"):
        opencode_models = state.get(key) or {}
        if not isinstance(opencode_models, dict):
            continue
        for provider in ("anthropic", "gemini", "oss"):
            models = opencode_models.get(provider) or []
            if isinstance(models, list) and models:
                model = models[0]
                if isinstance(model, str) and model:
                    return model
    return None
