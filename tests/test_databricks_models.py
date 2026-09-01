"""Focused tests for the Databricks models concern."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import lucode.databricks.models as db_mod
from lucode.databricks.models import (
    AI_GATEWAY_V2_DOCS_URL,
    build_opencode_base_urls,
    build_tool_base_url,
    classify_model_family,
)

WS = "https://example.databricks.com"


def _model_service(model_id: str) -> dict:
    """A model-services entry whose `name` strips to `model_id`."""
    return {"name": f"model-services/{model_id}"}


def _foundation_models_payload(names):
    return {
        "endpoints": [
            {
                "name": name,
                "config": {
                    "served_entities": [
                        {
                            "foundation_model": {
                                "ai_gateway_v2_supported": True,
                                "api_types": ["gemini/v1/generateContent"],
                            }
                        }
                    ]
                },
            }
            for name in names
        ]
    }


class TestBuildToolBaseUrl:
    def test_codex(self):
        url = build_tool_base_url("codex", WS)
        assert url == f"{WS}/ai-gateway/codex/v1"

    def test_claude(self):
        url = build_tool_base_url("claude", WS)
        assert url == f"{WS}/ai-gateway/anthropic"

    def test_gemini(self):
        url = build_tool_base_url("gemini", WS)
        assert url == f"{WS}/ai-gateway/gemini"

    def test_opencode_raises(self):
        with pytest.raises(RuntimeError, match="multiple base URLs"):
            build_tool_base_url("opencode", WS)

    def test_unsupported_tool_raises(self):
        with pytest.raises(RuntimeError, match="Unsupported"):
            build_tool_base_url("unknown", WS)


class TestBuildOpencodeBaseUrls:
    def test_returns_anthropic_gemini_and_oss(self):
        urls = build_opencode_base_urls(WS)
        assert urls["anthropic"] == f"{WS}/ai-gateway/anthropic/v1"
        assert urls["gemini"] == f"{WS}/ai-gateway/gemini/v1beta"
        assert urls["oss"] == f"{WS}/ai-gateway/mlflow/v1"


class TestModelTokenLimits:
    def test_glm_is_capped(self):
        assert db_mod.model_token_limits("system.ai.glm-5-2") == {
            "context": 200_000,
            "output": 25_000,
        }

    def test_glm_matches_any_version(self):
        assert db_mod.model_token_limits("system.ai.glm-4-6-flash") == {
            "context": 200_000,
            "output": 25_000,
        }

    def test_uncapped_model_returns_none(self):
        assert db_mod.model_token_limits("system.ai.kimi-k2-7-code") is None


class TestDiscoverModelServices:
    def test_buckets_families_by_name(self, monkeypatch):
        payload = {
            "model_services": [
                _model_service("system.ai.claude-fable-5"),
                _model_service("system.ai.claude-opus-4-7"),
                _model_service("system.ai.claude-opus-4-8"),
                _model_service("system.ai.claude-sonnet-4-6"),
                _model_service("system.ai.gpt-5"),
                _model_service("system.ai.gemini-2-5-flash"),
                _model_service("system.ai.gemini-3-5-flash"),
                _model_service("system.ai.kimi-k2-7-code"),
                _model_service("system.ai.glm-5-2"),
                _model_service("system.ai.llama-4-maverick"),
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=10: (payload, None))

        claude, codex, gemini, oss, reason = db_mod.discover_model_services(WS, "token")

        assert reason is None
        # Fable is excluded; newest opus wins; sonnet is retained; haiku is absent.
        assert claude == {
            "opus": "system.ai.claude-opus-4-8",
            "sonnet": "system.ai.claude-sonnet-4-6",
        }
        assert codex == ["system.ai.gpt-5"]
        # Gemini ordered newest-first via the shared sort key.
        assert gemini[0] == "system.ai.gemini-3-5-flash"
        # kimi and glm are the allowlisted OSS families; llama is not.
        assert oss == ["system.ai.glm-5-2", "system.ai.kimi-k2-7-code"]

    def test_oss_allowlist_drops_unsupported_families(self, monkeypatch):
        # Only kimi/glm are allowlisted; other families are dropped.
        payload = {
            "model_services": [
                _model_service("system.ai.glm-5-2"),
                _model_service("system.ai.kimi-k2-7-code"),
                _model_service("system.ai.qwen-3-coder"),
                _model_service("system.ai.deepseek-v3"),
                _model_service("system.ai.gte-large-embed"),
                _model_service("system.ai.bge-reranker-v2"),
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=10: (payload, None))

        claude, codex, gemini, oss, reason = db_mod.discover_model_services(WS, "token")

        assert reason is None
        assert (claude, codex, gemini) == ({}, [], [])
        assert oss == ["system.ai.glm-5-2", "system.ai.kimi-k2-7-code"]

    def test_paginates_via_next_page_token(self, monkeypatch):
        pages = {
            None: {
                "model_services": [_model_service("system.ai.gpt-5")],
                "next_page_token": "tok2",
            },
            "tok2": {
                "model_services": [_model_service("system.ai.claude-opus-4-8")],
            },
        }

        def fake_get(url, token, timeout=10):
            token_param = None
            if "page_token=" in url:
                token_param = url.split("page_token=")[1].split("&")[0]
            return pages[token_param], None

        monkeypatch.setattr(db_mod, "http_get_json", fake_get)

        claude, codex, _, _, reason = db_mod.discover_model_services(WS, "token")

        assert reason is None
        assert codex == ["system.ai.gpt-5"]
        assert claude == {"opus": "system.ai.claude-opus-4-8"}

    def test_http_failure_returns_reason(self, monkeypatch):
        monkeypatch.setattr(
            db_mod, "http_get_json", lambda url, token, timeout=10: (None, "HTTP 500 Server Error")
        )

        claude, codex, gemini, oss, reason = db_mod.discover_model_services(WS, "token")

        assert (claude, codex, gemini, oss) == ({}, [], [], [])
        assert reason == "HTTP 500 Server Error"

    def test_no_matching_families_reports_sample(self, monkeypatch):
        payload = {"model_services": [_model_service("system.ai.llama-4-maverick")]}
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=10: (payload, None))

        claude, codex, gemini, oss, reason = db_mod.discover_model_services(WS, "token")

        assert (claude, codex, gemini, oss) == ({}, [], [], [])
        assert reason is not None and "llama-4-maverick" in reason

    def test_ignores_non_system_ai_schemas(self, monkeypatch):
        # The metastore listing returns services from every schema; only
        # system.ai.* foundation models should be picked up.
        payload = {
            "model_services": [
                _model_service("system.ai.gpt-5"),
                _model_service("main.schema3.gpt-5-5"),
                _model_service("temp.erni.kimi-k2-7-code"),
                _model_service("temp.erni.claude-opus-4-8"),
                _model_service("dnasi_agent_cuj.default.dnasi-gpt55-test"),
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token, timeout=10: (payload, None))

        claude, codex, gemini, oss, reason = db_mod.discover_model_services(WS, "token")

        assert reason is None
        assert codex == ["system.ai.gpt-5"]
        assert claude == {}  # temp.erni.claude-* must not be bucketed
        assert gemini == []
        assert oss == []

    def test_requests_bounded_page_size(self, monkeypatch):
        # The endpoint 499s without a bounded page_size, so every request must
        # carry one.
        urls: list[str] = []

        def fake_get(url, token, timeout=10):
            urls.append(url)
            return {"model_services": [_model_service("system.ai.gpt-5")]}, None

        monkeypatch.setattr(db_mod, "http_get_json", fake_get)

        ids, reason = db_mod.list_model_services(WS, "token")

        assert ids == ["system.ai.gpt-5"]
        assert reason is None
        assert all("page_size=" in u for u in urls)

    def test_retries_page_before_giving_up(self, monkeypatch):
        payload = {"model_services": [_model_service("system.ai.gpt-5")]}
        calls = {"n": 0}

        def flaky_get(url, token, timeout=10):
            calls["n"] += 1
            if calls["n"] < 3:
                return None, "HTTP 499 Unknown"
            return payload, None

        monkeypatch.setattr(db_mod, "http_get_json", flaky_get)

        ids, reason = db_mod.list_model_services(WS, "token")

        assert reason is None
        assert ids == ["system.ai.gpt-5"]
        assert calls["n"] == 3


class TestModelVersionSortKey:
    def test_orders_newest_version_first(self):
        names = [
            "databricks-gemini-2-5-flash",
            "databricks-gemini-2-5-pro",
            "databricks-gemini-3-1-flash-lite",
            "databricks-gemini-3-1-pro",
            "databricks-gemini-3-5-flash",
            "databricks-gemini-3-flash",
            "databricks-gemini-3-pro",
        ]
        ordered = sorted(names, key=db_mod.model_version_sort_key)
        assert ordered[0] == "databricks-gemini-3-5-flash"

    def test_treats_bare_major_as_dot_zero(self):
        # 3-flash is 3.0, so 3-5-flash (3.5) must sort ahead of it.
        names = ["databricks-gemini-3-flash", "databricks-gemini-3-5-flash"]
        ordered = sorted(names, key=db_mod.model_version_sort_key)
        assert ordered == [
            "databricks-gemini-3-5-flash",
            "databricks-gemini-3-flash",
        ]

    def test_unversioned_names_sort_last_alphabetically(self):
        names = ["databricks-gemini-2-5-flash", "custom-endpoint", "another-endpoint"]
        ordered = sorted(names, key=db_mod.model_version_sort_key)
        assert ordered[0] == "databricks-gemini-2-5-flash"
        assert ordered[1:] == ["another-endpoint", "custom-endpoint"]


class TestDiscoverGeminiModels:
    def test_returns_newest_flash_first(self, monkeypatch):
        payload = _foundation_models_payload(
            [
                "databricks-gemini-2-5-flash",
                "databricks-gemini-3-5-flash",
                "databricks-gemini-3-flash",
            ]
        )
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token: (payload, None))

        models, reason = db_mod.discover_gemini_models(WS, "token")

        assert reason is None
        assert models[0] == "databricks-gemini-3-5-flash"

    def test_codex_discovery_keeps_alphabetical_order(self, monkeypatch):
        # Codex passes no sort_key, so ordering must stay the plain alphabetical
        # default — guarding against the gemini change leaking across tools.
        payload = {
            "endpoints": [
                {
                    "name": name,
                    "config": {
                        "served_entities": [
                            {
                                "foundation_model": {
                                    "ai_gateway_v2_supported": True,
                                    "api_types": ["openai/v1/responses"],
                                }
                            }
                        ]
                    },
                }
                for name in ["databricks-gpt-5-2-codex", "databricks-gpt-4-1"]
            ]
        }
        monkeypatch.setattr(db_mod, "http_get_json", lambda url, token: (payload, None))

        models, reason = db_mod.discover_codex_models(WS, "token")

        assert reason is None
        assert models == ["databricks-gpt-4-1", "databricks-gpt-5-2-codex"]


class TestEnsureAiGatewayV2:
    """Test ensure_ai_gateway_v2 without real network calls.

    The probe is `GET /api/ai-gateway/v2/endpoints`: a successful JSON
    response means v2 is wired up (even if `endpoints` is empty), while
    404/401/403/network errors all raise a RuntimeError with the docs URL.
    """

    @staticmethod
    def _mock_json_response(body: str):
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = body.encode("utf-8")
        return mock_resp

    @staticmethod
    def _http_error(code: int, msg: str, body: str = ""):
        import io
        from unittest.mock import MagicMock
        from urllib.error import HTTPError

        fp = io.BytesIO(body.encode("utf-8")) if body else None
        return HTTPError(url="", code=code, msg=msg, hdrs=MagicMock(), fp=fp)

    def test_raises_on_404(self):

        exc = self._http_error(404, "Not Found")
        with patch("lucode.databricks.transport.urllib_request.urlopen", side_effect=exc):
            from lucode.databricks.models import ensure_ai_gateway_v2

            with pytest.raises(RuntimeError, match=AI_GATEWAY_V2_DOCS_URL) as excinfo:
                ensure_ai_gateway_v2(WS, "fake-token")
            assert "not enabled" in str(excinfo.value)

    def test_raises_on_401_with_auth_hint(self):

        exc = self._http_error(401, "Unauthorized")
        with patch("lucode.databricks.transport.urllib_request.urlopen", side_effect=exc):
            from lucode.databricks.models import ensure_ai_gateway_v2

            with pytest.raises(RuntimeError, match="401") as excinfo:
                ensure_ai_gateway_v2(WS, "fake-token")
            message = str(excinfo.value)
            assert "rejected" in message.lower()
            assert "databricks auth login" in message

    def test_raises_on_400_invalid_token_with_auth_hint(self):
        """400 + body `Invalid Token` is the misleading-error case from issue #84."""

        exc = self._http_error(400, "Bad Request", body="Invalid Token")
        with patch("lucode.databricks.transport.urllib_request.urlopen", side_effect=exc):
            from lucode.databricks.models import ensure_ai_gateway_v2

            with pytest.raises(RuntimeError) as excinfo:
                ensure_ai_gateway_v2(WS, "fake-token")
            message = str(excinfo.value)
            # The bug we are fixing: must NOT collapse to the generic
            # "v2 not available" message — must call out the auth failure
            # and point at re-login.
            assert "Invalid Token" in message
            assert "rejected" in message.lower()
            assert "databricks auth login" in message

    def test_400_without_invalid_token_falls_through_to_generic(self):
        """A 400 that is *not* an auth failure should still surface the body."""

        exc = self._http_error(400, "Bad Request", body="some other detail")
        with patch("lucode.databricks.transport.urllib_request.urlopen", side_effect=exc):
            from lucode.databricks.models import ensure_ai_gateway_v2

            with pytest.raises(RuntimeError, match=AI_GATEWAY_V2_DOCS_URL) as excinfo:
                ensure_ai_gateway_v2(WS, "fake-token")
            assert "some other detail" in str(excinfo.value)

    def test_raises_on_url_error(self):
        from urllib.error import URLError

        with patch(
            "lucode.databricks.transport.urllib_request.urlopen",
            side_effect=URLError("connection refused"),
        ):
            from lucode.databricks.models import ensure_ai_gateway_v2

            with pytest.raises(RuntimeError, match=AI_GATEWAY_V2_DOCS_URL):
                ensure_ai_gateway_v2(WS, "fake-token")

    def test_succeeds_with_endpoints_list(self):

        with patch(
            "lucode.databricks.transport.urllib_request.urlopen",
            return_value=self._mock_json_response('{"endpoints": [{"name": "foo"}]}'),
        ):
            from lucode.databricks.models import ensure_ai_gateway_v2

            ensure_ai_gateway_v2(WS, "fake-token")  # should not raise

    def test_succeeds_with_empty_endpoints_list(self):

        # A 200 with no endpoints still means v2 is wired up on this workspace —
        # downstream discovery will surface "no models" with a clearer reason.
        with patch(
            "lucode.databricks.transport.urllib_request.urlopen",
            return_value=self._mock_json_response('{"endpoints": []}'),
        ):
            from lucode.databricks.models import ensure_ai_gateway_v2

            ensure_ai_gateway_v2(WS, "fake-token")


class TestClassifyModelFamily:
    """Recovers the bucket a model would land in from discovery, so a managed config's flat list
    can be translated into the per-family state each agent reads."""

    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("system.ai.claude-opus-4-8", "opus"),
            ("system.ai.claude-sonnet-5", "sonnet"),
            ("databricks-claude-haiku-4-5", "haiku"),
            ("system.ai.claude-fable-5", None),
            ("system.ai.gpt-5-3-codex", "codex"),
            ("system.ai.gemini-3-flash", "gemini"),
            ("system.ai.kimi-k2-7-code", "oss"),
            ("system.ai.glm-4-6", "oss"),
            ("something-unrecognized", None),
        ],
    )
    def test_buckets_by_family(self, model_id, expected):
        assert classify_model_family(model_id) == expected


class TestModelServicesCache:
    """A successful listing is memoized per workspace: several callers want different views of the
    same paginated walk (bucketed families vs the raw Claude ids), so one configuration run would
    otherwise page the whole catalog twice."""

    @staticmethod
    def _counting_page(calls: dict):
        def page(url, token):
            calls["n"] = calls.get("n", 0) + 1
            return {
                "model_services": [
                    {"name": "model-services/system.ai.claude-opus-5"},
                    {"name": "model-services/system.ai.claude-opus-4-8"},
                ]
            }, None

        return page

    def test_repeat_listings_hit_the_api_once(self, monkeypatch):
        calls: dict = {}
        db_mod.clear_model_services_cache()
        monkeypatch.setattr(db_mod, "_get_model_services_page", self._counting_page(calls))
        first, _ = db_mod.list_model_services(WS, "tok")
        second, _ = db_mod.list_model_services(WS, "tok")
        assert first == second
        assert calls["n"] == 1

    def test_use_cache_false_forces_a_fresh_walk(self, monkeypatch):
        calls: dict = {}
        db_mod.clear_model_services_cache()
        monkeypatch.setattr(db_mod, "_get_model_services_page", self._counting_page(calls))
        db_mod.list_model_services(WS, "tok")
        db_mod.list_model_services(WS, "tok", use_cache=False)
        assert calls["n"] == 2

    def test_each_workspace_is_cached_separately(self, monkeypatch):
        calls: dict = {}
        db_mod.clear_model_services_cache()
        monkeypatch.setattr(db_mod, "_get_model_services_page", self._counting_page(calls))
        db_mod.list_model_services(WS, "tok")
        db_mod.list_model_services("https://other.databricks.com", "tok")
        assert calls["n"] == 2

    def test_partial_pagination_returns_reason_and_is_not_cached(self, monkeypatch):
        calls = {"n": 0}

        def partial(url, token):
            calls["n"] += 1
            if "page_token=" not in url:
                return {
                    "model_services": [_model_service("system.ai.gpt-5")],
                    "next_page_token": "next",
                }, None
            return None, "HTTP 500 Server Error"

        db_mod.clear_model_services_cache()
        monkeypatch.setattr(db_mod, "_get_model_services_page", partial)

        first, reason = db_mod.list_model_services(WS, "tok")
        second, second_reason = db_mod.list_model_services(WS, "tok")

        assert first == second == ["system.ai.gpt-5"]
        assert reason == second_reason == "HTTP 500 Server Error"
        assert calls["n"] == 4

    def test_repeated_page_token_marks_result_partial(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "_get_model_services_page",
            lambda url, token: (
                {
                    "model_services": [_model_service("system.ai.gpt-5")],
                    "next_page_token": "same",
                },
                None,
            ),
        )

        _, reason = db_mod.list_model_services(WS, "tok", use_cache=False)

        assert reason is not None and "repeated" in reason

    def test_failures_are_not_cached(self, monkeypatch):
        # A transient error must not poison the rest of the process into believing there are no
        # models on the workspace.
        calls: dict = {}

        def failing(url, token):
            calls["n"] = calls.get("n", 0) + 1
            return None, "HTTP 500 Server Error"

        db_mod.clear_model_services_cache()
        monkeypatch.setattr(db_mod, "_get_model_services_page", failing)
        ids, reason = db_mod.list_model_services(WS, "tok")
        assert ids == [] and reason is not None

        monkeypatch.setattr(db_mod, "_get_model_services_page", self._counting_page(calls))
        ids, reason = db_mod.list_model_services(WS, "tok")
        assert reason is None
        assert ids
