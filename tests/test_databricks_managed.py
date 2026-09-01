"""Focused tests for the Databricks managed concern."""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qs

import pytest

import lucode.databricks.managed as db_mod
from lucode.databricks.managed import (
    CODING_AGENT_RECOMMEND_MODEL_PATH,
    resolve_current_budget_spend,
)

WS = "https://example.databricks.com"


class _FakeResponse:
    """Minimal urlopen context manager returning a JSON body."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestIsWorkspaceAdmin:
    """Admin detection reuses the SCIM `Me` payload, which carries group membership."""

    @staticmethod
    def _stub(monkeypatch, payload):
        monkeypatch.setattr(db_mod, "_scim_me", lambda ws, tok: payload)

    def test_true_when_in_the_admins_group(self, monkeypatch):
        self._stub(monkeypatch, {"groups": [{"display": "users"}, {"display": "admins"}]})
        assert db_mod.is_workspace_admin("https://w", "tok") is True

    def test_false_without_the_admins_group(self, monkeypatch):
        self._stub(monkeypatch, {"groups": [{"display": "users"}]})
        assert db_mod.is_workspace_admin("https://w", "tok") is False

    def test_none_when_the_check_could_not_be_made(self, monkeypatch):
        # An unreachable SCIM is "unknown", not "not an admin" — the caller must not send a real
        # admin down the non-admin dead end.
        self._stub(monkeypatch, None)
        assert db_mod.is_workspace_admin("https://w", "tok") is None

    @pytest.mark.parametrize("payload", [{}, {"groups": "not-a-list"}])
    def test_false_when_the_payload_names_no_groups(self, monkeypatch, payload):
        # A well-formed `Me` for a user in no groups omits `groups` entirely.
        self._stub(monkeypatch, payload)
        assert db_mod.is_workspace_admin("https://w", "tok") is False


class TestCodingAgentConfigUrls:
    def test_collection_url(self):
        assert db_mod._coding_agent_config_url(WS) == f"{WS}/api/ai-gateway/v2/coding-agent-configs"

    def test_resource_url_appends_the_server_assigned_name(self):
        # The API templates Get/Update/Delete on `{name=coding-agent-configs/*}`, so the resource
        # name already carries the collection segment and must not be duplicated.
        url = db_mod._coding_agent_config_url(WS, "coding-agent-configs/abc123")
        assert url == f"{WS}/api/ai-gateway/v2/coding-agent-configs/abc123"

    def test_stray_slashes_are_tolerated(self):
        url = db_mod._coding_agent_config_url(WS, "/coding-agent-configs/abc123/")
        assert url == f"{WS}/api/ai-gateway/v2/coding-agent-configs/abc123"


class TestCodingAgentConfigCrudClients:
    CONFIG = {"default_agent": "CODING_AGENT_CLAUDE_CODE"}

    def test_create_posts_the_config_to_the_collection(self, monkeypatch):
        seen = {}

        def fake_post(url, token, payload, *, timeout=10):
            seen.update(url=url, payload=payload)
            return {"name": "coding-agent-configs/new"}, None

        monkeypatch.setattr(db_mod, "http_post_json", fake_post)
        config, reason = db_mod.create_coding_agent_config(WS, "tok", self.CONFIG)
        assert reason is None
        assert config == {"name": "coding-agent-configs/new"}
        assert seen["url"] == f"{WS}/api/ai-gateway/v2/coding-agent-configs"
        assert seen["payload"] == self.CONFIG

    def test_create_surfaces_the_failure_reason(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_post_json",
            lambda *a, **k: (None, 'HTTP 400: {"error_code":"ALREADY_EXISTS"}'),
        )
        config, reason = db_mod.create_coding_agent_config(WS, "tok", self.CONFIG)
        assert config is None
        assert "ALREADY_EXISTS" in reason

    def test_update_patches_the_resource_with_a_mask(self, monkeypatch):
        seen = {}

        def fake_patch(url, token, payload, *, timeout=10):
            seen.update(url=url, payload=payload)
            return {"name": "coding-agent-configs/abc"}, None

        monkeypatch.setattr(db_mod, "http_patch_json", fake_patch)
        config, reason = db_mod.update_coding_agent_config(
            WS, "tok", "coding-agent-configs/abc", self.CONFIG
        )
        assert reason is None
        assert config == {"name": "coding-agent-configs/abc"}
        # The mask rides in the query string: the RPC binds `body: "coding_agent_config"`, so the
        # config is the whole body and a mask nested inside it is read as an unknown config field —
        # the server then reports the mask as missing. A FieldMask's JSON form is one
        # comma-separated string, not a `{"paths": [...]}` object.
        url, _, query = seen["url"].partition("?")
        assert url == f"{WS}/api/ai-gateway/v2/coding-agent-configs/abc"
        mask = parse_qs(query)["update_mask"][0].split(",")
        assert mask == list(db_mod.MANAGED_CONFIG_UPDATE_MASK_PATHS)
        assert "update_mask" not in seen["payload"]
        # `name` still goes in the body: the API's path template reads it from the config.
        assert seen["payload"]["name"] == "coding-agent-configs/abc"
        assert seen["payload"]["default_agent"] == "CODING_AGENT_CLAUDE_CODE"

    def test_update_mask_never_names_a_field_the_server_rejects(self):
        # The server's mutable set is the upper bound; `budget_id` is in it but deprecated and
        # rejected on write, so lucode must not name it. `default_options`/`tiers` are the legacy
        # model-only shape lucode never authors.
        assert "budget_id" not in db_mod.MANAGED_CONFIG_UPDATE_MASK_PATHS
        assert "default_options" not in db_mod.MANAGED_CONFIG_UPDATE_MASK_PATHS
        assert "tiers" not in db_mod.MANAGED_CONFIG_UPDATE_MASK_PATHS

    def test_update_mask_covers_every_field_the_manifest_can_set(self):
        # A path lucode omits is a field a re-run silently cannot clear, since the server merges per
        # path. Derive the expectation from the serializer rather than restating it, so adding a
        # manifest field fails here instead of shipping a mask that can't clear it.
        from lucode.managed.setup import serialize_managed_config

        emitted = set(
            serialize_managed_config(
                {
                    "display_name": "org config",
                    "default_agent": "pi",
                    "enabled_agents": {
                        "pi": {"model_config": {"default_model": "system.ai.claude-opus-5"}}
                    },
                    "mcp_servers": [{"name": "databricks-sql", "type": "sql"}],
                    "skills": {"names": ["main.default"]},
                    "budget_policy": {
                        "budget_id": "11111111-1111-1111-1111-111111111111",
                        "tiers": [],
                    },
                }
            )
        )
        assert emitted == set(db_mod.MANAGED_CONFIG_UPDATE_MASK_PATHS)

    def test_delete_returns_only_a_reason(self, monkeypatch):
        seen = {}

        def fake_delete(url, token, *, timeout=10):
            seen["url"] = url
            return None, None

        monkeypatch.setattr(db_mod, "http_delete", fake_delete)
        assert db_mod.delete_coding_agent_config(WS, "tok", "coding-agent-configs/abc") is None
        assert seen["url"] == f"{WS}/api/ai-gateway/v2/coding-agent-configs/abc"

    def test_delete_surfaces_the_failure_reason(self, monkeypatch):
        monkeypatch.setattr(db_mod, "http_delete", lambda *a, **k: (None, "HTTP 404 Not Found"))
        reason = db_mod.delete_coding_agent_config(WS, "tok", "coding-agent-configs/abc")
        assert reason == "HTTP 404 Not Found"


class TestResolveCurrentBudgetSpend:
    def test_parses_spend_and_threshold(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_post_json",
            lambda url, token, payload, timeout=10: (
                {"current_spend": "12.34", "effective_threshold": "100"},
                None,
            ),
        )
        spend, reason = resolve_current_budget_spend("https://ws", "token")
        assert spend == (Decimal("12.34"), Decimal("100"))
        assert reason is None

    def test_posts_to_recommend_model_with_no_available_models(self, monkeypatch):
        captured = {}

        def fake_post(url, token, payload, timeout=10):
            captured["url"] = url
            captured["payload"] = payload
            return {"current_spend": "1", "effective_threshold": "2"}, None

        monkeypatch.setattr(db_mod, "http_post_json", fake_post)
        resolve_current_budget_spend("https://ws.example.com", "token")
        assert captured["url"] == (f"https://ws.example.com{CODING_AGENT_RECOMMEND_MODEL_PATH}")
        # Empty list applies no availability filter; we want the spend only.
        assert captured["payload"] == {"available_models": []}

    def test_ignores_recommended_models(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_post_json",
            lambda url, token, payload, timeout=10: (
                {
                    "recommended_models": ["system.ai.claude-sonnet-4-5"],
                    "current_spend": "12.34",
                    "effective_threshold": "100",
                },
                None,
            ),
        )
        spend, reason = resolve_current_budget_spend("https://ws", "token")
        assert spend == (Decimal("12.34"), Decimal("100"))
        assert reason is None

    def test_recommendation_without_spend_is_no_spend(self, monkeypatch):
        # A config with no matching budget still recommends models, but both
        # spend fields come back unset.
        monkeypatch.setattr(
            db_mod,
            "http_post_json",
            lambda url, token, payload, timeout=10: (
                {"recommended_models": ["system.ai.claude-sonnet-4-5"]},
                None,
            ),
        )
        spend, reason = resolve_current_budget_spend("https://ws", "token")
        assert spend is None
        assert "no coding-agent budget spend" in reason

    def test_feature_disabled_returns_reason(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_post_json",
            lambda url, token, payload, timeout=10: (
                None,
                "HTTP 400 Bad Request: FEATURE_DISABLED",
            ),
        )
        spend, reason = resolve_current_budget_spend("https://ws", "token")
        assert spend is None
        assert "FEATURE_DISABLED" in reason

    def test_unset_fields_treated_as_no_spend(self, monkeypatch):
        monkeypatch.setattr(
            db_mod, "http_post_json", lambda url, token, payload, timeout=10: ({}, None)
        )
        spend, reason = resolve_current_budget_spend("https://ws", "token")
        assert spend is None
        assert "no coding-agent budget spend" in reason

    def test_spend_without_threshold_is_no_spend(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_post_json",
            lambda url, token, payload, timeout=10: ({"current_spend": "12.34"}, None),
        )
        spend, _ = resolve_current_budget_spend("https://ws", "token")
        assert spend is None

    def test_malformed_decimal_is_no_spend(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "http_post_json",
            lambda url, token, payload, timeout=10: (
                {"current_spend": "not-a-number", "effective_threshold": "100"},
                None,
            ),
        )
        spend, _ = resolve_current_budget_spend("https://ws", "token")
        assert spend is None

    def test_non_object_payload_is_no_spend(self, monkeypatch):
        monkeypatch.setattr(
            db_mod, "http_post_json", lambda url, token, payload, timeout=10: ([], None)
        )
        spend, reason = resolve_current_budget_spend("https://ws", "token")
        assert spend is None
        assert "not a JSON object" in reason
