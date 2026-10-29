"""Apply checksum-gated patches to a freshly cloned OpenUpgrade tree."""

from __future__ import annotations

import argparse
import hashlib
import logging
from pathlib import Path
from typing import List, Sequence

from .patch_registry import ScriptPatch, patches_for_version

LOGGER = logging.getLogger("odooupgrader.compatibility.patches")
GENERIC_MARKER = "odooupgrader-patch: uom_migration_repair"


class PatchApplicationError(RuntimeError):
    """Raised when an OpenUpgrade file is not the revision a patch was built for."""


def apply_script_patch(path: Path, patch: ScriptPatch) -> str:
    """Patch ``path`` in place. Returns ``applied`` or ``already-applied``."""

    if not path.is_file():
        raise PatchApplicationError(
            f"OpenUpgrade file {path} does not exist. Expected it from revision "
            f"{patch.openupgrade_revision}."
        )
    original = path.read_bytes()
    text = original.decode("utf-8")
    if patch.marker in text:
        LOGGER.info(
            "Patch %s version %s already present in %s",
            patch.patch_id,
            patch.patch_version,
            path,
        )
        return "already-applied"
    if GENERIC_MARKER in text:
        raise PatchApplicationError(
            f"{path} contains a different {patch.patch_id} patch than version "
            f"{patch.patch_version}. Refusing to patch it twice."
        )
    actual = hashlib.sha256(original).hexdigest()
    if actual != patch.upstream_sha256:
        raise PatchApplicationError(
            "OpenUpgrade source changed unexpectedly. "
            f"File {patch.relative_path} from revision {patch.openupgrade_revision} "
            f"should have sha256 {patch.upstream_sha256} but has {actual}. "
            f"Patch {patch.patch_id} version {patch.patch_version} was not applied."
        )
    needle = '    Uom = env["uom.uom"]\n'
    migrate_at = text.find("\n@openupgrade.migrate()")
    start = text.find(needle)
    if start < 0 or migrate_at < 0 or start > migrate_at:
        raise PatchApplicationError(
            f"{patch.relative_path} matched the checksum but not the expected function shape."
        )
    updated = text[:start] + patch.replacement_body + text[migrate_at + 1 :]
    if patch.marker not in updated or "apply_uom_reference_repairs" not in updated:
        raise PatchApplicationError(f"Patch {patch.patch_id} did not produce a valid script.")
    if "old_reference" in updated.split("@openupgrade.migrate()", 1)[0]:
        raise PatchApplicationError(
            "Patched UoM script still contains the unsafe old_reference assignment."
        )
    path.write_bytes(updated.encode("utf-8"))
    LOGGER.info(
        "Applied %s version %s to %s (OpenUpgrade %s)",
        patch.patch_id,
        patch.patch_version,
        path,
        patch.openupgrade_revision,
    )
    return "applied"


def apply_version_patches(addons_root: Path, odoo_version: str) -> List[str]:
    """Apply every patch registered for ``odoo_version``. Returns status strings."""

    results = []
    for patch in patches_for_version(odoo_version):
        target = addons_root / patch.relative_path
        results.append(apply_script_patch(target, patch))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply OdooUpgrader OpenUpgrade patches")
    parser.add_argument("--addons", required=True, help="Path to the cloned OpenUpgrade tree")
    parser.add_argument("--version", required=True, help="Target Odoo series, for example 19.0")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        statuses = apply_version_patches(Path(args.addons), args.version)
    except PatchApplicationError as exc:
        LOGGER.error("%s", exc)
        return 1
    if not statuses:
        LOGGER.info("No migration patches registered for %s", args.version)
    else:
        LOGGER.info("Patch results: %s", ", ".join(statuses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
