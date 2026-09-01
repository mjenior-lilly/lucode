"""Tests for ui.py — pure helpers that don't touch I/O or prompts."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from rich.console import Console

from lucode.ui import (
    normalize_workspace_url,
    prompt_for_percentage,
    prompt_for_text,
    prompt_for_workspace,
    prompt_yes_no_default,
    status_badge,
)


class TestPromptYesNoDefault:
    def _answer(self, monkeypatch, value):
        # value: a string the user "types", or EOFError to simulate closed stdin.
        def fake_input(_prompt):
            if value is EOFError:
                raise EOFError
            return value

        monkeypatch.setattr("lucode.ui.console.input", fake_input)

    def test_empty_takes_default_true(self, monkeypatch):
        self._answer(monkeypatch, "")
        assert prompt_yes_no_default("go?", default=True) is True

    def test_empty_takes_default_false(self, monkeypatch):
        self._answer(monkeypatch, "")
        assert prompt_yes_no_default("go?", default=False) is False

    def test_eof_takes_default(self, monkeypatch):
        # Non-interactive / closed stdin must not abort — it takes the default.
        self._answer(monkeypatch, EOFError)
        assert prompt_yes_no_default("go?", default=True) is True

    def test_explicit_no_overrides_default_yes(self, monkeypatch):
        self._answer(monkeypatch, "n")
        assert prompt_yes_no_default("go?", default=True) is False

    def test_explicit_yes_overrides_default_false(self, monkeypatch):
        self._answer(monkeypatch, "yes")
        assert prompt_yes_no_default("go?", default=False) is True


def _visible(markup: str) -> str:
    """What the user actually sees, with Rich markup resolved.

    Asserting on the raw markup string is what let a swallowed default ship: `[tiered]` is present
    in the markup and absent from the output, because Rich reads it as a style tag.
    """
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    console.print(markup)
    return console.file.getvalue().rstrip()


class TestDefaultsAreLabelledAsAcceptable:
    """A shown default must say that enter takes it, or it reads as a format example."""

    def test_text_default_says_enter_accepts_it(self):
        with patch("lucode.ui.console.input", return_value="") as inp:
            assert prompt_for_text("Policy name", default="tiered") == "tiered"
        rendered = _visible(inp.call_args[0][0])
        assert "[tiered]" in rendered
        assert "enter to accept" in rendered

    def test_a_word_like_default_is_not_eaten_as_markup(self):
        # Rich treats `[coding-agents-tiered-routing]` as a style tag and renders nothing for it, so
        # the real wizard default vanished from the prompt while `[80]` survived.
        with patch("lucode.ui.console.input", return_value="") as inp:
            prompt_for_text("Policy name", default="coding-agents-tiered-routing")
        assert "[coding-agents-tiered-routing]" in _visible(inp.call_args[0][0])

    def test_a_dotted_default_is_not_eaten_as_markup(self):
        with patch("lucode.ui.console.input", return_value="") as inp:
            prompt_for_text("Skills location", default="main.default")
        assert "[main.default]" in _visible(inp.call_args[0][0])

    def test_percentage_default_says_enter_accepts_it(self):
        with patch("lucode.ui.console.input", return_value="") as inp:
            assert prompt_for_percentage("at what percent?", default=0.8) == 0.8
        rendered = _visible(inp.call_args[0][0])
        # Prompted in percent even though the API takes a fraction.
        assert "[80]" in rendered
        assert "enter to accept" in rendered

    def test_no_default_shows_no_hint(self):
        with patch("lucode.ui.console.input", return_value="typed") as inp:
            assert prompt_for_text("Model") == "typed"
        assert "enter to accept" not in _visible(inp.call_args[0][0])

    def test_typing_still_overrides_the_default(self):
        with patch("lucode.ui.console.input", return_value="mine"):
            assert prompt_for_text("Policy name", default="tiered") == "mine"


class TestClosedStdinAborts:
    """Ctrl-D must reach the CLI as an abort, not as a traceback."""

    def test_percentage_without_a_default_raises_keyboard_interrupt(self):
        # `lucode setup`'s tier prompt passes no default. EOFError has no handler above this call —
        # the setup command catches only RuntimeError and KeyboardInterrupt — so a bare EOFError
        # reached the admin as a raw traceback.
        with patch("lucode.ui.console.input", side_effect=EOFError):
            with pytest.raises(KeyboardInterrupt):
                prompt_for_percentage("Tier 1: activates at what percent of budget?")

    def test_percentage_with_a_default_still_takes_it(self):
        with patch("lucode.ui.console.input", side_effect=EOFError):
            assert prompt_for_percentage("at what percent?", default=0.8) == 0.8


class TestNormalizeWorkspaceUrl:
    def test_adds_https_when_missing(self):
        assert normalize_workspace_url("example.databricks.com") == "https://example.databricks.com"

    def test_strips_trailing_slash(self):
        assert (
            normalize_workspace_url("https://example.databricks.com/")
            == "https://example.databricks.com"
        )

    def test_strips_multiple_trailing_slashes(self):
        assert (
            normalize_workspace_url("https://example.databricks.com///")
            == "https://example.databricks.com"
        )

    def test_preserves_https(self):
        assert (
            normalize_workspace_url("https://foo.azuredatabricks.net")
            == "https://foo.azuredatabricks.net"
        )

    def test_preserves_http(self):
        assert normalize_workspace_url("http://localhost:8080") == "http://localhost:8080"

    def test_strips_whitespace(self):
        assert (
            normalize_workspace_url("  https://example.databricks.com  ")
            == "https://example.databricks.com"
        )

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            normalize_workspace_url("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty"):
            normalize_workspace_url("   ")


class TestStatusBadge:
    def test_ok_is_green(self):
        assert "green" in status_badge("OK", "ok")

    def test_warn_is_yellow(self):
        assert "yellow" in status_badge("Warning", "warn")

    def test_error_is_red(self):
        assert "red" in status_badge("Error", "error")

    def test_unknown_kind_uses_bold(self):
        result = status_badge("X", "unknown")
        assert "bold" in result
        assert "X" in result

    def test_text_is_included(self):
        assert "MyText" in status_badge("MyText", "ok")


class TestPromptForWorkspace:
    """Cover the three things `questionary.select(...).ask()` can return:
    a (host, profile) tuple, None (cancel or "Enter a different URL"),
    or — in some questionary versions — the choice's title string."""

    PROFILES = [("https://a.databricks.com", "prof-a"), ("https://b.databricks.com", "prof-b")]

    def test_returns_selected_profile_tuple(self):
        with patch("lucode.ui.questionary.select") as mock_select:
            mock_select.return_value.ask.return_value = (
                "https://a.databricks.com",
                "prof-a",
            )
            url, profile = prompt_for_workspace("desc", profiles=self.PROFILES)
        assert url == "https://a.databricks.com"
        assert profile == "prof-a"

    def test_none_falls_through_to_manual_prompt(self):
        with (
            patch("lucode.ui.questionary.select") as mock_select,
            patch("lucode.ui.console.input", return_value="https://manual.databricks.com"),
        ):
            mock_select.return_value.ask.return_value = None
            url, profile = prompt_for_workspace("desc", profiles=self.PROFILES)
        assert url == "https://manual.databricks.com"
        assert profile is None

    def test_string_value_falls_through_to_manual_prompt(self):
        # Regression: if questionary returns the choice title (e.g. "Enter a
        # different URL") instead of its value, we must not try to unpack it.
        with (
            patch("lucode.ui.questionary.select") as mock_select,
            patch("lucode.ui.console.input", return_value="https://manual.databricks.com"),
        ):
            mock_select.return_value.ask.return_value = "Enter a different URL"
            url, profile = prompt_for_workspace("desc", profiles=self.PROFILES)
        assert url == "https://manual.databricks.com"
        assert profile is None

    def test_no_profiles_goes_straight_to_manual_prompt(self):
        with patch("lucode.ui.console.input", return_value="example.databricks.com"):
            url, profile = prompt_for_workspace("desc", profiles=None)
        assert url == "https://example.databricks.com"
        assert profile is None
