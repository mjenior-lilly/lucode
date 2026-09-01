"""End-to-end test that the User-Agent header lucode injects actually reaches the wire.

We don't talk to a real Databricks workspace here — instead we stand up a
tiny HTTP capture server on localhost, point each agent's *_BASE_URL at it,
launch the agent, and assert on the User-Agent the server saw.

The server returns a canned error so the agent itself fails; we don't care
about the agent's exit code, only the headers that arrived before it bailed.
This is the cheapest way to verify "lucode wired the UA into the request"
end-to-end without TLS, real models, or workspace credentials.

Skipped per-agent when the binary isn't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lucode.telemetry import agent_version, lucode_version


def _require_binary(binary: str):
    if not shutil.which(binary):
        pytest.skip(f"`{binary}` is not installed")


class _CapturedRequest:
    """Bag of fields recorded by the capture server for one inbound request."""

    def __init__(self, method: str, path: str, headers: dict[str, str]):
        self.method = method
        self.path = path
        self.headers = headers


class _CaptureServer:
    """HTTP server that records every inbound request's method/path/headers
    and replies 401 with a JSON error so the agent fails fast and exits."""

    def __init__(self):
        self.requests: list[_CapturedRequest] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        captured = self.requests

        class Handler(BaseHTTPRequestHandler):
            def _record_and_reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        self.rfile.read(length)
                    except Exception:
                        pass
                captured.append(
                    _CapturedRequest(
                        method=self.command, path=self.path, headers=dict(self.headers.items())
                    )
                )
                body = json.dumps(
                    {"error": {"type": "invalid_api_key", "message": "lucode test capture"}}
                ).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._record_and_reply()

            def do_POST(self):
                self._record_and_reply()

            def log_message(self, format, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def first_request_with_path_prefix(self, prefix: str) -> _CapturedRequest | None:
        for req in self.requests:
            if req.path.startswith(prefix):
                return req
        return None


@pytest.fixture
def capture_server():
    server = _CaptureServer()
    server.start()
    yield server
    server.stop()


def _expected_ua(agent_name: str, binary: str) -> str:
    return f"lucode/{lucode_version()} {agent_name}/{agent_version(binary)}"


def _assert_ua(req: _CapturedRequest, expected: str) -> None:
    ua = req.headers.get("User-Agent") or req.headers.get("user-agent")
    assert ua == expected, f"User-Agent mismatch.\n  got:      {ua!r}\n  expected: {expected!r}"


def _run_until_first_request(
    cmd: list[str], env: dict[str, str], timeout: int = 20
) -> subprocess.CompletedProcess | None:
    """Spawn the agent. We only need it to fire its first HTTP request; some
    agents retry on 401 indefinitely. Swallow timeouts — the capture server
    has what we need by then. Returns the CompletedProcess (or None on
    timeout) so callers can surface stderr on failure."""
    try:
        return subprocess.run(
            cmd, env=env, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return None


def _no_request_msg(server: _CaptureServer, result: subprocess.CompletedProcess | None) -> str:
    if result is None:
        return "Agent timed out before any request reached the capture server."
    stderr = (result.stderr or b"").decode(errors="replace")[:600]
    stdout = (result.stdout or b"").decode(errors="replace")[:300]
    return f"No request reached the capture server.\n  paths: {[r.path for r in server.requests]}\n  rc:    {result.returncode}\n  stderr: {stderr!r}\n  stdout: {stdout!r}"


class TestOpencodeUserAgent:
    def test_user_agent_arrives_at_gateway(self, tmp_path, monkeypatch, capture_server):
        import lucode.config as config_mod
        from lucode.agents import opencode

        _require_binary("opencode")
        xdg = tmp_path / "xdg"
        opencode_dir = xdg / "opencode"
        opencode_dir.mkdir(parents=True)
        config_path = opencode_dir / "opencode.json"
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", config_path)
        monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", tmp_path / "opencode.backup.json")
        state = {
            "workspace": capture_server.base_url,
            "opencode_models": {"anthropic": ["test-claude-model"]},
            "base_urls": {
                "opencode": {
                    "anthropic": f"{capture_server.base_url}/ai-gateway/anthropic/v1",
                    "gemini": f"{capture_server.base_url}/ai-gateway/gemini/v1beta",
                }
            },
        }
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("lucode.state.save_state", lambda s: None)
            mp.setattr(
                "lucode.agents.opencode.get_databricks_token",
                lambda ws, profile=None, **kwargs: "test-token",
            )
            opencode.write_tool_config(state, "test-claude-model", token="test-token")
        env = {**os.environ, "OAUTH_TOKEN": "test-token", "XDG_CONFIG_HOME": str(xdg)}
        result = _run_until_first_request(opencode.validate_cmd("opencode"), env)
        req = capture_server.first_request_with_path_prefix("/ai-gateway/anthropic")
        assert req is not None, _no_request_msg(capture_server, result)
        ua = req.headers.get("User-Agent") or req.headers.get("user-agent") or ""
        expected_prefix = _expected_ua("opencode", "opencode")
        assert ua.startswith(expected_prefix), (
            f"OpenCode UA missing lucode prefix.\n  got:    {ua!r}\n  prefix: {expected_prefix!r}"
        )


class TestPiUserAgent:
    def test_user_agent_arrives_at_gateway(self, tmp_path, monkeypatch, capture_server):
        import lucode.config as config_mod
        from lucode.agents import pi

        _require_binary("pi")
        pi_home = tmp_path / "pi-home"
        pi_dir = pi_home / ".pi" / "agent"
        config_path = pi_dir / "models.json"
        monkeypatch.setattr(config_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr(pi, "PI_lucode_HOME", pi_home)
        monkeypatch.setattr(pi, "PI_CONFIG_DIR", pi_dir)
        monkeypatch.setattr(pi, "PI_CONFIG_PATH", config_path)
        monkeypatch.setattr(pi, "PI_SETTINGS_PATH", pi_dir / "settings.json")
        monkeypatch.setattr(pi, "PI_BACKUP_PATH", tmp_path / "pi.backup.json")
        monkeypatch.setattr(pi, "PI_SETTINGS_BACKUP_PATH", tmp_path / "pi-settings.backup.json")
        # Pi model membership is user-owned, so seed the native inventory that
        # this runtime test expects the generated provider to serve.
        pi_dir.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {"providers": {"databricks-claude": {"models": [{"id": "test-claude-model"}]}}}
            ),
            encoding="utf-8",
        )
        state = {
            "workspace": capture_server.base_url,
            "claude_models": {"sonnet": "test-claude-model"},
            "codex_models": [],
            "gemini_models": [],
            "base_urls": {
                "pi": {
                    "claude": f"{capture_server.base_url}/ai-gateway/anthropic",
                    "openai": f"{capture_server.base_url}/ai-gateway/codex/v1",
                    "gemini": f"{capture_server.base_url}/ai-gateway/gemini/v1beta",
                }
            },
        }
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("lucode.state.save_state", lambda s: None)
            mp.setattr(
                "lucode.agents.pi.get_databricks_token",
                lambda ws, profile=None, **kwargs: "test-token",
            )
            pi.write_tool_config(state, "test-claude-model", token="test-token")
        env = pi.build_runtime_env("test-token")
        result = _run_until_first_request(pi.validate_cmd("pi"), env)
        req = capture_server.first_request_with_path_prefix("/ai-gateway/anthropic")
        assert req is not None, _no_request_msg(capture_server, result)
        _assert_ua(req, _expected_ua("pi", "pi"))
