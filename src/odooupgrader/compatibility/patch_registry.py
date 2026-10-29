"""Checksums and markers for OpenUpgrade scripts this project knows how to patch."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .dependency_registry import get_dependency_spec

PATCH_ID = "uom_migration_repair"
PATCH_VERSION = 1
UOM_SCRIPT = "openupgrade_scripts/scripts/uom/19.0.1.0/post-migration.py"
FIXTURE_NAME = "uom_19.0.1.0_post-migration.py"
# SHA-256 of OpenUpgrade 19.0 commit bf12c3cee6097145e53efa589b0d99a7e64d6a87
# file openupgrade_scripts/scripts/uom/19.0.1.0/post-migration.py.
EXPECTED_UOM_POST_SHA256 = "d5022cfb79edcae33fae3c712c87837c730012467e61f7213ad69a17bf77d84c"
COMPAT_IMAGE_ROOT = "/opt/odooupgrader_pkg"


@dataclass(frozen=True)
class ScriptPatch:
    """One checksum-gated edit of a file inside a pinned OpenUpgrade tree."""

    patch_id: str
    patch_version: int
    odoo_version: str
    openupgrade_revision: str
    relative_path: str
    upstream_sha256: str
    marker: str
    replacement_body: str


def fixture_bytes() -> bytes:
    path = Path(__file__).resolve().parent / "fixtures" / FIXTURE_NAME
    return path.read_bytes()


def uom_marker(revision: str) -> str:
    return (
        f"odooupgrader-patch: {PATCH_ID} version={PATCH_VERSION} "
        f"openupgrade={revision}"
    )


def uom_replacement_body(revision: str) -> str:
    """Body that replaces the unsafe reference assignment.

    The original loop is not left in place: a multi-record ``old_reference``
    would still be written to ``relative_uom_id`` if it ran after the hook.
    """

    marker = uom_marker(revision)
    return (
        f"    # {marker}\n"
        "    import sys as _odooupgrader_sys\n"
        f"    _odooupgrader_sys.path.insert(0, {COMPAT_IMAGE_ROOT!r})\n"
        "    from odooupgrader.compatibility.patches.v19.uom_migration_repair import (\n"
        "        apply_uom_reference_repairs as _odooupgrader_uom_repair,\n"
        "    )\n"
        "    _odooupgrader_uom_repair(env, openupgrade)\n"
    )


def uom_script_patch() -> ScriptPatch:
    spec = get_dependency_spec("19.0")
    if spec is None:
        raise RuntimeError("Odoo 19.0 dependency pin is missing")
    digest = hashlib.sha256(fixture_bytes()).hexdigest()
    if digest != EXPECTED_UOM_POST_SHA256:
        raise RuntimeError(
            "Vendored UoM post-migration fixture does not match the pinned checksum "
            f"(expected {EXPECTED_UOM_POST_SHA256}, got {digest})."
        )
    return ScriptPatch(
        patch_id=PATCH_ID,
        patch_version=PATCH_VERSION,
        odoo_version="19.0",
        openupgrade_revision=spec.openupgrade_revision,
        relative_path=UOM_SCRIPT,
        upstream_sha256=EXPECTED_UOM_POST_SHA256,
        marker=uom_marker(spec.openupgrade_revision),
        replacement_body=uom_replacement_body(spec.openupgrade_revision),
    )


def patches_for_version(odoo_version: str) -> tuple:
    if odoo_version == "19.0":
        return (uom_script_patch(),)
    return ()
