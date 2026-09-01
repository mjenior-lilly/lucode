"""Focused tests for the Databricks transport concern."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

import lucode.databricks.transport as db_mod
from lucode.databricks.transport import (
    _scrub_databrickscfg,
    _scrub_json,
    format_subprocess_result,
    http_get_json,
    workspace_hostname,
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


class TestWorkspaceHostname:
    def test_extracts_hostname(self):
        assert workspace_hostname(WS) == "example.databricks.com"

    def test_handles_path(self):
        assert (
            workspace_hostname("https://foo.azuredatabricks.net/some/path")
            == "foo.azuredatabricks.net"
        )

    def test_invalid_url_raises(self):
        with pytest.raises((RuntimeError, ValueError)):
            workspace_hostname("")


class TestHttpGetJsonReason:
    """The `reason` string returned by `http_get_json` must include the response body
    so callers (e.g. ensure_ai_gateway_v2) can route on it. Before issue #84's fix
    the body was logged only when lucode_DEBUG=1 and dropped from the bubbled error."""

    @staticmethod
    def _http_error(code: int, msg: str, body: str = ""):
        import io
        from unittest.mock import MagicMock
        from urllib.error import HTTPError

        fp = io.BytesIO(body.encode("utf-8")) if body else None
        return HTTPError(url="", code=code, msg=msg, hdrs=MagicMock(), fp=fp)

    def test_reason_includes_body_on_http_error(self):

        exc = self._http_error(400, "Bad Request", body="Invalid Token")
        with patch("lucode.databricks.transport.urllib_request.urlopen", side_effect=exc):
            payload, reason = http_get_json("https://x/y", "tok")
        assert payload is None
        assert "HTTP 400" in reason
        assert "Invalid Token" in reason

    def test_reason_without_body_is_status_only(self):

        exc = self._http_error(404, "Not Found")
        with patch("lucode.databricks.transport.urllib_request.urlopen", side_effect=exc):
            payload, reason = http_get_json("https://x/y", "tok")
        assert payload is None
        assert reason == "HTTP 404 Not Found"


class TestHttpGetJsonTimeout:
    """A socket read timeout raises a bare TimeoutError (an OSError), not a
    URLError. It must be returned as a reason, not propagated — otherwise it
    escapes the best-effort MCP discovery flow and crashes the command."""

    def test_read_timeout_returns_reason_instead_of_raising(self, monkeypatch):
        def raise_timeout(request, timeout=None):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", raise_timeout)

        payload, reason = db_mod.http_get_json(f"{WS}/api/2.0/anything", "tok")

        assert payload is None
        assert reason is not None
        assert "timed out" in reason

    def test_post_read_timeout_returns_reason_instead_of_raising(self, monkeypatch):
        def raise_timeout(request, timeout=None):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", raise_timeout)

        payload, reason = db_mod.http_post_json(f"{WS}/api/2.0/anything", "tok", {"k": "v"})

        assert payload is None
        assert reason is not None
        assert "timed out" in reason

    def test_bytes_read_timeout_returns_reason_instead_of_raising(self, monkeypatch):
        def raise_timeout(request, timeout=None):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", raise_timeout)

        payload, reason = db_mod.http_get_bytes(f"{WS}/api/2.0/anything", "tok")

        assert payload is None
        assert reason is not None
        assert "timed out" in reason


class TestFormatSubprocessResult:
    def test_suppresses_stdout_on_success(self):
        result = subprocess.CompletedProcess(
            args=["databricks", "auth", "token"],
            returncode=0,
            stdout='{"access_token": "dapi-secret-do-not-leak", "token_type": "Bearer"}',
            stderr="",
        )
        formatted = format_subprocess_result(result)
        assert "dapi-secret-do-not-leak" not in formatted
        assert "rc=0" in formatted

    def test_includes_stdout_on_failure(self):
        result = subprocess.CompletedProcess(
            args=["databricks", "auth", "token"],
            returncode=1,
            stdout="useful diagnostic output",
            stderr="error: no matching profile",
        )
        formatted = format_subprocess_result(result)
        assert "rc=1" in formatted
        assert "useful diagnostic output" in formatted
        assert "no matching profile" in formatted

    def test_scrubs_credentials_on_failure(self):
        result = subprocess.CompletedProcess(
            args=["databricks", "auth", "token"],
            returncode=1,
            stdout='{"access_token": "dapiSyntheticToken123", "message": "diagnostic"}',
            stderr=(
                "Authorization: Bearer synthetic-bearer-value and dapiAnotherSyntheticToken456"
            ),
        )
        formatted = format_subprocess_result(result)
        for secret in (
            "dapiSyntheticToken123",
            "synthetic-bearer-value",
            "dapiAnotherSyntheticToken456",
        ):
            assert secret not in formatted
        assert "<redacted>" in formatted
        assert "diagnostic" in formatted


class TestScrubDatabrickscfg:
    def test_redacts_token_value(self):
        text = "[DEFAULT]\nhost = https://example.databricks.com\ntoken = dapi-secret\n"
        scrubbed = _scrub_databrickscfg(text)
        assert "dapi-secret" not in scrubbed
        assert "token = <redacted>" in scrubbed
        assert "host = https://example.databricks.com" in scrubbed

    def test_redacts_various_secret_keys(self):
        text = (
            "[p]\n"
            "client_secret = secret-val-1\n"
            "bearer_token = secret-val-2\n"
            "api_key = secret-val-3\n"
            "password = secret-val-4\n"
            "auth_type = oauth-u2m\n"
        )
        scrubbed = _scrub_databrickscfg(text)
        for secret in ("secret-val-1", "secret-val-2", "secret-val-3", "secret-val-4"):
            assert secret not in scrubbed
        assert "auth_type = oauth-u2m" in scrubbed

    def test_preserves_comments_and_sections(self):
        text = "# comment\n[DEFAULT]\nhost = https://x\n; another comment with token = leak\n"
        scrubbed = _scrub_databrickscfg(text)
        assert "# comment" in scrubbed
        assert "[DEFAULT]" in scrubbed
        assert "; another comment with token = leak" in scrubbed

    def test_key_matching_is_case_insensitive(self):
        text = "[p]\nTOKEN = upper\nAccess_Token = mixed\n"
        scrubbed = _scrub_databrickscfg(text)
        assert "upper" not in scrubbed
        assert "mixed" not in scrubbed


class TestScrubJson:
    def test_redacts_secret_keys(self):
        payload = {
            "access_token": "dapi-secret",
            "host": "https://example.databricks.com",
        }
        scrubbed = _scrub_json(payload)
        assert isinstance(scrubbed, dict)
        assert scrubbed["access_token"] == "<redacted>"
        assert scrubbed["host"] == "https://example.databricks.com"

    def test_recurses_into_nested_structures(self):
        payload = {
            "profiles": [
                {"name": "DEFAULT", "client_secret": "abc"},
                {"name": "other", "password": "pw"},
            ]
        }
        scrubbed = _scrub_json(payload)
        assert scrubbed == {
            "profiles": [
                {"name": "DEFAULT", "client_secret": "<redacted>"},
                {"name": "other", "password": "<redacted>"},
            ]
        }

    def test_passes_through_scalars_and_non_secret_keys(self):
        assert _scrub_json("plain") == "plain"
        assert _scrub_json(42) == 42
        assert _scrub_json({"host": "x", "auth_type": "pat"}) == {
            "host": "x",
            "auth_type": "pat",
        }


class TestHttpDelete:
    """A successful delete returns `google.protobuf.Empty`, so an empty body is success."""

    @staticmethod
    def _empty_response(body: str = ""):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)
        response.read.return_value = body.encode("utf-8")
        response.status = 200
        return response

    def test_empty_body_is_success_not_a_decode_error(self, monkeypatch):
        # Without `allow_empty_body` this would fail with "response was not valid JSON".
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda request, timeout=None: self._empty_response()
        )
        payload, reason = db_mod.http_delete(f"{WS}/api/anything", "tok")
        assert reason is None
        assert payload is None

    def test_empty_json_object_is_also_success(self, monkeypatch):
        monkeypatch.setattr(
            db_mod.urllib_request,
            "urlopen",
            lambda request, timeout=None: self._empty_response("{}"),
        )
        payload, reason = db_mod.http_delete(f"{WS}/api/anything", "tok")
        assert reason is None
        assert payload == {}

    def test_uses_the_delete_verb_and_sends_no_body(self, monkeypatch):
        seen = {}

        def capture(request, timeout=None):
            seen["method"] = request.get_method()
            seen["data"] = request.data
            return self._empty_response()

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", capture)
        db_mod.http_delete(f"{WS}/api/anything", "tok")
        assert seen["method"] == "DELETE"
        assert seen["data"] is None

    def test_http_error_surfaces_the_body(self, monkeypatch):
        import io
        from unittest.mock import MagicMock
        from urllib.error import HTTPError

        body = '{"error_code":"PERMISSION_DENIED","message":"admin required"}'

        def raise_http_error(request, timeout=None):
            raise HTTPError(
                url="", code=403, msg="Forbidden", hdrs=MagicMock(), fp=io.BytesIO(body.encode())
            )

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", raise_http_error)
        _, reason = db_mod.http_delete(f"{WS}/api/anything", "tok")
        assert reason is not None
        assert "403" in reason
        assert "PERMISSION_DENIED" in reason


class TestHttpPatchJson:
    def test_uses_the_patch_verb_and_sends_the_body(self, monkeypatch):
        from unittest.mock import MagicMock

        seen = {}

        def capture(request, timeout=None):
            seen["method"] = request.get_method()
            seen["data"] = request.data
            seen["content_type"] = request.get_header("Content-type")
            response = MagicMock()
            response.__enter__ = lambda s: s
            response.__exit__ = MagicMock(return_value=False)
            response.read.return_value = b'{"name":"coding-agent-configs/x"}'
            response.status = 200
            return response

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", capture)
        payload, reason = db_mod.http_patch_json(f"{WS}/api/anything", "tok", {"k": "v"})
        assert reason is None
        assert payload == {"name": "coding-agent-configs/x"}
        assert seen["method"] == "PATCH"
        assert json.loads(seen["data"]) == {"k": "v"}
        assert seen["content_type"] == "application/json"
