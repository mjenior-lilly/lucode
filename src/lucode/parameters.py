"""Packaged per-model parameters for Pi and OpenCode.

The JSON files in ``lucode/defaults/`` carry per-model settings that workspace
discovery cannot produce, because they are properties of the model behind the AI
Gateway rather than of the model-services listing:

- Pi: ``contextWindow``, ``maxTokens``, ``thinkingLevelMap``, per-model
  ``compat``, ``input`` modalities, ``reasoning``, and display ``name``.
- OpenCode: ``limit`` (context + output), per-call ``options``, display ``name``.

Discovery returns bare model ids, so without this parameters an agent runs with no
context window, no output cap, and no thinking-level mapping. Those values were
established by testing each model against the gateway; treat them as findings,
not defaults that can be regenerated.

Two rules govern how the parameters is applied, both enforced by callers in
``agents/pi.py`` and ``agents/opencode.py``:

- *Membership* is decided by discovery or by a caller-supplied inventory.
  This module never adds a model an agent was not told to serve.
- *parameters* is layered underneath the user's own config. An existing entry in
  ``models.json`` / ``opencode.json`` always wins, so hand edits survive.

Credential and route fields are stripped at load time (:data:`GENERATED_KEYS`),
so a packaged default can never reintroduce a stale endpoint or a placeholder
API key into a live config.
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from importlib import resources
from typing import Any

# Fields lucode regenerates per workspace on every configure, or that carry a
# credential. Stripped on load: the packaged files are a parameters reference, and
# must not be a source of endpoints or keys.
GENERATED_KEYS = frozenset(
    {
        "baseUrl",
        "baseURL",
        "apiKey",
        "authHeader",
        "api",
        "headers",
        "npm",
        "options",
    }
)

_PI_MODELS = "pi-models.json"
_PI_SETTINGS = "pi-settings.json"
_OPENCODE_MODELS = "opencode-models.json"
_OPENCODE_GENERATED_KEYS = GENERATED_KEYS - {"options"}


def _load(filename: str) -> dict[str, Any]:
    """Read one packaged defaults file, or return {} when it is absent.

    Absence is tolerated rather than fatal: a partial install must not stop an
    agent from launching with discovery-only models.
    """
    try:
        text = (resources.files("lucode.defaults") / filename).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=1)
def _pi_parameters() -> dict[str, dict[str, dict[str, Any]]]:
    """Pi parameters as ``{provider: {model_id: parameters}}``, credentials stripped."""
    providers = _load(_PI_MODELS).get("providers")
    if not isinstance(providers, dict):
        return {}
    params: dict[str, dict[str, dict[str, Any]]] = {}
    for provider, config in providers.items():
        if not isinstance(config, dict):
            continue
        models = config.get("models")
        if not isinstance(models, list):
            continue
        entries: dict[str, dict[str, Any]] = {}
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            entries[model_id] = {k: v for k, v in model.items() if k not in GENERATED_KEYS}
        if entries:
            params[provider] = entries
    return params


@lru_cache(maxsize=1)
def _opencode_parameters() -> dict[str, dict[str, dict[str, Any]]]:
    """OpenCode parameters as ``{provider: {model_id: parameters}}``, credentials stripped.

    Per-model ``options`` is kept here, unlike :data:`GENERATED_KEYS` at provider
    level: OpenCode reads per-call providerOptions from ``models.<id>.options``,
    so it is parameters rather than a credential.
    """
    providers = _load(_OPENCODE_MODELS).get("provider")
    if not isinstance(providers, dict):
        return {}
    params: dict[str, dict[str, dict[str, Any]]] = {}
    for provider, config in providers.items():
        if not isinstance(config, dict):
            continue
        models = config.get("models")
        if not isinstance(models, dict):
            continue
        entries = {
            model_id: {
                key: deepcopy(value)
                for key, value in entry.items()
                if key not in _OPENCODE_GENERATED_KEYS
            }
            for model_id, entry in models.items()
            if isinstance(model_id, str) and model_id and isinstance(entry, dict)
        }
        if entries:
            params[provider] = entries
    return params


@lru_cache(maxsize=1)
def pi_settings_packages() -> tuple[str, ...]:
    """Extension packages Pi should install, from the packaged settings file."""
    packages = _load(_PI_SETTINGS).get("packages")
    if not isinstance(packages, list):
        return ()
    return tuple(p for p in packages if isinstance(p, str) and p)


def pi_parameters(provider: str, model_id: str) -> dict[str, Any]:
    """Packaged Pi parameters for one model, or {} when none is known.

    The returned dict excludes ``id`` so callers can merge it onto an entry they
    already keyed by id.
    """
    parameters = _pi_parameters().get(provider, {}).get(model_id)
    if not parameters:
        return {}
    return {k: deepcopy(v) for k, v in parameters.items() if k != "id"}


def opencode_parameters(provider: str, model_id: str) -> dict[str, Any]:
    """Packaged OpenCode parameters for one model, or {} when none is known."""
    return deepcopy(_opencode_parameters().get(provider, {}).get(model_id, {}))


def pi_params_model_ids(provider: str) -> tuple[str, ...]:
    """Model ids with packaged Pi parameters, in packaged order."""
    return tuple(_pi_parameters().get(provider, {}))


def opencode_params_model_ids(provider: str) -> tuple[str, ...]:
    """Model ids with packaged OpenCode parameters, in packaged order."""
    return tuple(_opencode_parameters().get(provider, {}))
