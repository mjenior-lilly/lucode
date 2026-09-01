"""HTTP transport, diagnostics, redaction, and workspace URL helpers."""

from __future__ import annotations

import functools
import json
import logging
import logging.handlers
import os
import re
import subprocess
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from ucode.config_io import APP_DIR
from ucode.ui import err_console, normalize_workspace_url


def _debug_enabled() -> bool:
    return os.environ.get("UCODE_DEBUG") == "1"


_DEBUG_LOGGER: logging.Logger | None = None


def _get_debug_logger() -> logging.Logger | None:
    """Lazily configure a rotating file logger when UCODE_DEBUG=1.

    Returns the logger on first call (and caches it), or None if debug is
    disabled or the log file could not be opened. A one-time breadcrumb is
    printed to stderr so the user knows where to tail."""
    global _DEBUG_LOGGER
    if _DEBUG_LOGGER is not None or not _debug_enabled():
        return _DEBUG_LOGGER

    log_path = APP_DIR / "debug.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
        )
    except OSError:
        return None

    logger = logging.getLogger("ucode.debug")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    _DEBUG_LOGGER = logger
    err_console.print(f"[dim]\\[ucode debug] logging to {log_path}[/dim]")
    return _DEBUG_LOGGER


def _debug(label: str, detail: str) -> None:
    """When UCODE_DEBUG=1, append a timestamped entry to ~/.ucode/debug.log."""
    logger = _get_debug_logger()
    if logger is not None:
        logger.debug("%s: %s", label, detail)


_SECRET_KEY_PATTERN = re.compile(r"(token|secret|password|bearer|api_key|apikey)", re.IGNORECASE)


def _format_subprocess_result(
    result: subprocess.CompletedProcess[str],
) -> str:
    """Format a CompletedProcess for the debug log without leaking tokens.

    On success, stdout is suppressed (it often contains the access token).
    On failure, stdout/stderr are included truncated."""
    stderr = (result.stderr or "").strip()[:500]
    if result.returncode == 0:
        return f"rc=0 stderr={stderr!r}"
    stdout = (result.stdout or "").strip()[:500]
    return f"rc={result.returncode} stdout={stdout!r} stderr={stderr!r}"


def _scrub_databrickscfg(text: str) -> str:
    """Redact value of any INI key that looks secret-bearing."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if "=" in stripped and not stripped.startswith(("#", ";")):
            key = stripped.split("=", 1)[0].strip()
            if _SECRET_KEY_PATTERN.search(key):
                indent = line[: len(line) - len(stripped)]
                out.append(f"{indent}{key} = <redacted>")
                continue
        out.append(line)
    return "\n".join(out)


def _scrub_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: (
                "<redacted>"
                if isinstance(k, str) and _SECRET_KEY_PATTERN.search(k)
                else _scrub_json(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    return value


@functools.cache
def _log_auth_diagnostics() -> None:
    """Dump CLI version, profiles, and ~/.databrickscfg (scrubbed) to the debug log.

    No-op unless UCODE_DEBUG=1; cached so it runs at most once per process."""
    if not _debug_enabled():
        return

    try:
        version_result = subprocess.run(
            ["databricks", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = (version_result.stdout or version_result.stderr or "").strip()
        _debug("databricks --version", version[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        _debug("databricks --version", f"exception: {type(exc).__name__}: {exc}")

    try:
        profiles_result = subprocess.run(
            ["databricks", "auth", "profiles", "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        _debug(
            "databricks auth profiles",
            f"rc={profiles_result.returncode} "
            f"stderr={(profiles_result.stderr or '').strip()[:300]!r}",
        )
        if profiles_result.returncode == 0 and profiles_result.stdout:
            try:
                payload = json.loads(profiles_result.stdout)
                _debug("profiles json", json.dumps(_scrub_json(payload))[:2000])
            except json.JSONDecodeError as exc:
                _debug("profiles json", f"decode error: {exc}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _debug("databricks auth profiles", f"exception: {type(exc).__name__}: {exc}")

    cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or "~/.databrickscfg").expanduser()
    try:
        if cfg_path.is_file():
            raw = cfg_path.read_text(encoding="utf-8", errors="replace")
            _debug(f"databrickscfg ({cfg_path})", _scrub_databrickscfg(raw)[:4000])
        else:
            _debug(f"databrickscfg ({cfg_path})", "not present")
    except OSError as exc:
        _debug(f"databrickscfg ({cfg_path})", f"read error: {exc}")


def _http_get_json(
    url: str, token: str, *, timeout: float = 10
) -> tuple[dict | list | None, str | None]:
    """GET a JSON endpoint. Returns (payload, None) on success, (None, reason) on failure.

    Honors UCODE_DEBUG=1 to append status + truncated body to ~/.ucode/debug.log.
    """
    request = urllib_request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        _debug(f"GET {url}", f"HTTP 200, {len(body)} bytes")
        if _debug_enabled():
            _debug("body", body[:4000])
        try:
            return json.loads(body), None
        except json.JSONDecodeError as exc:
            return None, f"response was not valid JSON ({exc.msg})"
    except urllib_error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except Exception:
            body = ""
        _debug(f"GET {url}", f"HTTP {exc.code} {exc.reason}")
        if _debug_enabled() and body:
            _debug("body", body[:4000])
        reason = f"HTTP {exc.code} {exc.reason}"
        # Surface the response body too — gateway auth failures return 400
        # with body `Invalid Token`, which is invisible without this.
        body_excerpt = body.strip()[:200]
        if body_excerpt:
            reason = f"{reason}: {body_excerpt}"
        return None, reason
    except urllib_error.URLError as exc:
        _debug(f"GET {url}", f"URLError: {exc.reason}")
        return None, f"network error: {exc.reason}"
    except OSError as exc:
        # A socket read timeout raises a bare TimeoutError (an OSError), not a
        # URLError, so it must be caught explicitly or it escapes the whole
        # discovery flow. Surface it as a reason like every other failure.
        _debug(f"GET {url}", f"OSError: {exc}")
        return None, f"network error: {exc}"


def _http_send_json(
    method: str,
    url: str,
    token: str,
    payload: dict | None,
    *,
    timeout: int = 10,
    allow_empty_body: bool = False,
) -> tuple[dict | list | None, str | None]:
    """Send a request that may carry a JSON body, and decode a JSON response.

    Shared by `_http_post_json`, `_http_patch_json`, and `_http_delete` — the three differ only in
    verb, whether they send a body, and whether an empty response is success. Returns
    ``(payload, None)`` on success and ``(None, reason)`` on failure, like `_http_get_json`.

    ``allow_empty_body`` is for DELETE, whose success response is ``google.protobuf.Empty`` — an
    empty body there is the expected result, not a decode failure.
    """
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"
    request = urllib_request.Request(url, data=body_bytes, method=method, headers=headers)
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        _debug(f"{method} {url}", f"HTTP {response.status}, {len(body)} bytes")
        if _debug_enabled():
            _debug("body", body[:4000])
        if allow_empty_body and not body.strip():
            return None, None
        try:
            return json.loads(body), None
        except json.JSONDecodeError as exc:
            return None, f"response was not valid JSON ({exc.msg})"
    except urllib_error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except Exception:
            body = ""
        _debug(f"{method} {url}", f"HTTP {exc.code} {exc.reason}")
        if _debug_enabled() and body:
            _debug("body", body[:4000])
        reason = f"HTTP {exc.code} {exc.reason}"
        body_excerpt = body.strip()[:200]
        if body_excerpt:
            reason = f"{reason}: {body_excerpt}"
        return None, reason
    except urllib_error.URLError as exc:
        _debug(f"{method} {url}", f"URLError: {exc.reason}")
        return None, f"network error: {exc.reason}"
    except OSError as exc:
        # See `_http_get_json`: a bare socket timeout is an OSError, not a
        # URLError, and would otherwise escape the caller's error handling.
        _debug(f"{method} {url}", f"OSError: {exc}")
        return None, f"network error: {exc}"


def _http_post_json(
    url: str, token: str, payload: dict, *, timeout: int = 10
) -> tuple[dict | list | None, str | None]:
    """POST a JSON body to an endpoint. Returns (payload, None) on success,
    (None, reason) on failure. Mirrors `_http_get_json`."""
    return _http_send_json("POST", url, token, payload, timeout=timeout)


def _http_patch_json(
    url: str, token: str, payload: dict, *, timeout: int = 10
) -> tuple[dict | list | None, str | None]:
    """PATCH a JSON body to an endpoint. Returns (payload, None) on success,
    (None, reason) on failure."""
    return _http_send_json("PATCH", url, token, payload, timeout=timeout)


def _http_delete(
    url: str, token: str, *, timeout: int = 10
) -> tuple[dict | list | None, str | None]:
    """DELETE a resource. Returns (payload, None) on success, (None, reason) on failure.

    A successful delete returns ``google.protobuf.Empty``, which serializes as ``{}`` or an empty
    body depending on the gateway, so both count as success and yield ``(None, None)``. Callers
    should test ``reason`` rather than the payload.
    """
    return _http_send_json("DELETE", url, token, None, timeout=timeout, allow_empty_body=True)


def _http_get_bytes(url: str, token: str, *, timeout: int = 10) -> tuple[bytes | None, str | None]:
    """GET raw bytes. Returns (body, None) on success, (None, reason) on failure.

    Like `_http_get_json` but leaves the body undecoded, since skill bundles can
    contain binary files.
    """
    request = urllib_request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        _debug(f"GET {url}", f"HTTP 200, {len(body)} bytes")
        return body, None
    except urllib_error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except Exception:
            detail = ""
        _debug(f"GET {url}", f"HTTP {exc.code} {exc.reason}")
        reason = f"HTTP {exc.code} {exc.reason}"
        excerpt = detail.strip()[:200]
        if excerpt:
            reason = f"{reason}: {excerpt}"
        return None, reason
    except urllib_error.URLError as exc:
        _debug(f"GET {url}", f"URLError: {exc.reason}")
        return None, f"network error: {exc.reason}"


def workspace_hostname(workspace: str) -> str:
    parsed = urlparse(normalize_workspace_url(workspace))
    if not parsed.hostname:
        raise RuntimeError(f"Unable to derive hostname from workspace URL: {workspace}")
    return parsed.hostname
