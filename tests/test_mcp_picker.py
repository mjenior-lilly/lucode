"""Tests for MCP picker behavior."""

import pytest
import questionary
from prompt_toolkit.keys import Keys
from questionary.prompts.common import InquirerControl

import lucode.mcp.picker as picker


def test_back_sentinel_is_distinct_from_cancel_and_empty_selection():
    assert picker._BACK is not None
    assert picker._BACK != []


@pytest.mark.parametrize(
    ("available_argument", "name", "selection_prefix", "label"),
    [
        (
            "available_vector_search_servers",
            "databricks-vector-search-main-default",
            picker.VECTOR_SEARCH_SELECTION_PREFIX,
            "Vector Search",
        ),
        (
            "available_uc_functions_servers",
            "databricks-functions-main-default",
            picker.UC_FUNCTIONS_SELECTION_PREFIX,
            "UC Functions",
        ),
    ],
)
def test_catalog_schema_choices_preserve_existing_and_new_entries(
    available_argument, name, selection_prefix, label
):
    server = {
        "name": name,
        "catalog": "main",
        "schema": "default",
        "url": "https://example/server",
    }
    kwargs = {available_argument: [server, {"name": "malformed"}]}

    new_choices = picker.build_mcp_picker_choices([], [], [], [], **kwargs)
    existing_choices = picker.build_mcp_picker_choices([], [], [], [server], **kwargs)

    new = next(choice for choice in new_choices if choice.title.startswith(label))
    existing = next(choice for choice in existing_choices if choice.title.startswith(label))
    assert (new.title, new.value, new.checked) == (
        f"{label}: main.default",
        f"{picker.MCP_ADD_PREFIX}{selection_prefix}main.default",
        False,
    )
    assert (existing.title, existing.value, existing.checked) == (
        f"{label}: main.default",
        name,
        True,
    )
    assert sum(choice.title.startswith(label) for choice in new_choices) == 1


def _binding_keys(allow_back: bool) -> set[object]:
    control = InquirerControl(
        [questionary.Choice("one", value="one")],
        pointer="›",
        show_description=False,
    )
    bindings = picker._checkbox_key_bindings(
        control,
        lambda: [],
        lambda: True,
        allow_back=allow_back,
    )
    return {binding.keys[0] for binding in bindings.bindings}


def test_checkbox_bindings_keep_navigation_filter_submission_and_abort_keys():
    keys = _binding_keys(allow_back=False)

    assert {
        Keys.ControlC,
        Keys.ControlQ,
        Keys.ControlA,
        Keys.ControlH,
        Keys.Down,
        Keys.Up,
        Keys.ControlN,
        Keys.ControlP,
        Keys.ControlM,
        Keys.Any,
        " ",
        "a",
    } <= keys
    assert Keys.Left not in keys


def test_checkbox_back_binding_is_opt_in():
    assert Keys.Left in _binding_keys(allow_back=True)
