"""Tests for MCP picker behavior."""

import lucode.mcp.picker as picker


def test_back_sentinel_is_distinct_from_cancel_and_empty_selection():
    assert picker._BACK is not None
    assert picker._BACK != []
