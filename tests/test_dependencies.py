"""Dependency pins, Dockerfile text, and the openupgradelib function check."""

import types

from odooupgrader.compatibility.dependency_registry import (
    OPENUPGRADE_DEPENDENCIES,
    get_dependency_spec,
)
from odooupgrader.compatibility.dependency_validator import (
    format_missing_function_error,
    missing_required_functions,
)
from odooupgrader.compatibility.dockerfile import render_dockerfile
from odooupgrader.compatibility.patch_registry import EXPECTED_UOM_POST_SHA256


OPENUPGRADELIB_18 = "5dfe3b485da5cfaa1f8c528c183cbdd0bc8248b0"
OPENUPGRADE_18 = "a393ddbe56b6913eea1e77b42be93152ac00b16d"
OPENUPGRADE_19 = "bf12c3cee6097145e53efa589b0d99a7e64d6a87"


def test_odoo_18_pin_requires_inherits_helper():
    spec = get_dependency_spec("18.0")
    assert spec is not None
    assert spec.openupgrade_revision == OPENUPGRADE_18
    assert spec.openupgradelib_revision == OPENUPGRADELIB_18
    assert spec.required_functions == ("add_inherits_parent_xmlids",)
    assert "3.13.7" not in spec.notes or "does not provide" in spec.notes


def test_odoo_19_pin_is_immutable_and_has_no_required_new_functions():
    spec = get_dependency_spec("19.0")
    assert spec is not None
    assert spec.openupgrade_branch == "19.0"
    assert spec.openupgrade_revision == OPENUPGRADE_19
    assert spec.openupgradelib_revision == OPENUPGRADELIB_18
    assert spec.required_functions == ()


def test_older_versions_keep_historical_install():
    assert get_dependency_spec("17.0") is None
    assert "17.0" not in OPENUPGRADE_DEPENDENCIES


def test_missing_required_function_is_reported():
    module = types.SimpleNamespace(logged_query=lambda *args: None)
    missing = missing_required_functions(module, ["add_inherits_parent_xmlids"])
    assert missing == ["add_inherits_parent_xmlids"]
    message = format_missing_function_error("18.0", OPENUPGRADELIB_18, missing)
    assert "add_inherits_parent_xmlids" in message
    assert "18.0" in message
    assert "AttributeError" in message


def test_present_function_passes():
    module = types.SimpleNamespace(add_inherits_parent_xmlids=lambda *args: None)
    assert missing_required_functions(module, ["add_inherits_parent_xmlids"]) == []


def _python_blocks(dockerfile: str):
    chunks = []
    marker = "python3 - <<'PY'\n"
    rest = dockerfile
    while marker in rest:
        rest = rest.split(marker, 1)[1]
        block, rest = rest.split("\nPY", 1)
        chunks.append(block)
    return chunks


def test_dockerfile_for_18_pins_and_validates_function():
    text = render_dockerfile("18.0")
    assert OPENUPGRADE_18 in text
    assert OPENUPGRADELIB_18 in text
    assert "add_inherits_parent_xmlids" in text
    assert "--branch 18.0" not in text
    assert "--force-reinstall --no-deps" in text
    assert "requirements.txt" in text
    assert "uom_migration_repair" not in text
    for block in _python_blocks(text):
        compile(block, "<dockerfile-18>", "exec")


def test_dockerfile_for_19_patches_uom_script():
    text = render_dockerfile("19.0")
    assert OPENUPGRADE_19 in text
    assert OPENUPGRADELIB_18 in text
    assert "patch_manager" in text
    assert "uom_migration_repair" in text
    assert "migration_build/pkg/" in text
    for block in _python_blocks(text):
        compile(block, "<dockerfile-19>", "exec")


def test_dockerfile_for_17_keeps_branch_clone():
    text = render_dockerfile("17.0")
    assert "--branch 17.0" in text
    assert "requirements.txt" in text
    assert OPENUPGRADELIB_18 not in text
    assert "add_inherits_parent_xmlids" not in text
    assert EXPECTED_UOM_POST_SHA256 not in text


def test_custom_addon_commands_remain_before_user_switch():
    extra = "RUN pip3 install --no-cache-dir -r /mnt/custom-addons/requirements.txt"
    text = render_dockerfile("16.0", extra)
    assert text.index(extra) < text.index("USER odoo")
    assert "--branch 16.0" in text
