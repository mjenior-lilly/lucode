"""Tests for skills MCP registration."""

import lucode.mcp.skills as skills


def test_skills_entries_filters_by_kind():
    entry = {"name": "skills", "kind": skills.SKILLS_MCP_KIND}
    assert skills.skills_entries([{"name": "other"}, entry]) == [entry]
