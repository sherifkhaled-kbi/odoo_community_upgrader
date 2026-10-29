"""Patch application, failure reports, and the keep-database decision."""

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from odooupgrader.cli import main
from odooupgrader.compatibility.build_context import prepare_image_package
from odooupgrader.compatibility.patch_manager import (
    PatchApplicationError,
    apply_script_patch,
    apply_version_patches,
)
from odooupgrader.compatibility.patch_registry import (
    EXPECTED_UOM_POST_SHA256,
    fixture_bytes,
    uom_script_patch,
)
from odooupgrader.compatibility.reporting import (
    MigrationFailure,
    parse_odoo_failure,
    redact,
    should_preserve_database,
    write_failure_report,
)


def test_fixture_matches_pinned_openupgrade_checksum():
    assert hashlib.sha256(fixture_bytes()).hexdigest() == EXPECTED_UOM_POST_SHA256
    patch = uom_script_patch()
    assert patch.openupgrade_revision == "bf12c3cee6097145e53efa589b0d99a7e64d6a87"
    assert patch.patch_id == "uom_migration_repair"
    assert patch.patch_version == 1


def _tree_with_upstream(tmp_path: Path) -> Path:
    target = tmp_path / uom_script_patch().relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(fixture_bytes())
    return tmp_path


def test_patch_applies_once_and_is_idempotent(tmp_path):
    root = _tree_with_upstream(tmp_path)
    target = root / uom_script_patch().relative_path
    assert apply_script_patch(target, uom_script_patch()) == "applied"
    patched = target.read_text(encoding="utf-8")
    assert "apply_uom_reference_repairs" in patched
    assert "old_reference" not in patched.split("@openupgrade.migrate()", 1)[0]
    assert "load_data" in patched
    assert apply_script_patch(target, uom_script_patch()) == "already-applied"
    assert target.read_text(encoding="utf-8") == patched
    assert apply_version_patches(root, "19.0") == ["already-applied"]
    assert apply_version_patches(root, "18.0") == []


def test_patch_refuses_unexpected_upstream(tmp_path):
    root = _tree_with_upstream(tmp_path)
    target = root / uom_script_patch().relative_path
    target.write_text(target.read_text(encoding="utf-8") + "\n# upstream edit\n", encoding="utf-8")
    with pytest.raises(PatchApplicationError, match="changed unexpectedly"):
        apply_script_patch(target, uom_script_patch())
    assert "upstream edit" in target.read_text(encoding="utf-8")


def test_build_context_contains_the_patch_package(tmp_path):
    package = prepare_image_package(tmp_path)
    assert (package / "compatibility" / "patch_manager.py").is_file()
    assert (package / "compatibility" / "fixtures" / "uom_19.0.1.0_post-migration.py").is_file()
    assert not (package / "__init__.py").read_text(encoding="utf-8").startswith("from .core")


def test_failure_report_redacts_secrets_and_keeps_uom_detail(tmp_path):
    log = """
    File "/mnt/extra-addons/openupgrade_scripts/scripts/uom/19.0.1.0/post-migration.py", line 48
    ValueError: Wrong value for uom.uom.relative_uom_id: uom.uom(1, 2)
    POSTGRES_PASSWORD=odoo
    token = abcd
    """
    parsed = parse_odoo_failure(log)
    assert parsed["failed_module"] == "uom"
    assert parsed["migration_script"] == "uom/19.0.1.0/post-migration.py"
    assert parsed["error_type"] == "ValueError"
    assert parsed["error_field"] == "uom.uom.relative_uom_id"
    redacted = redact(log)
    assert "abcd" not in redacted
    assert "POSTGRES_PASSWORD=odoo" not in redacted

    failure = MigrationFailure(
        source_version="17.0",
        target_version="19.0",
        failed_step="18.0 -> 19.0",
        failed_module=parsed["failed_module"],
        migration_script=parsed["migration_script"],
        error_type=parsed["error_type"],
        error_message=parsed["error_message"],
        error_field=parsed["error_field"],
        compatibility_patch="uom_migration_repair",
        repair_status="requires_manual_review",
        detail="password=secret",
        uom_categories=[
            {
                "legacy_category_id": 4,
                "repair_decision": "ambiguous_reference",
                "explanation": "Several reference units have factor 1.",
                "canonical_reference_id": None,
                "reference_candidate_ids": [30, 31],
                "affected_uom_ids": [30, 31],
                "units": [
                    {
                        "id": 30,
                        "uom_type": "reference",
                        "factor": "1",
                        "factor_problem": None,
                        "xml_ids": [],
                    }
                ],
            }
        ],
    )
    paths = write_failure_report(str(tmp_path), failure)
    payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert payload["failed_step"] == "18.0 -> 19.0"
    assert payload["repair_status"] == "requires_manual_review"
    assert payload["compatibility_patch"] == "uom_migration_repair"
    assert payload["uom_categories"][0]["legacy_category_id"] == 4
    assert "secret" not in Path(paths["json"]).read_text(encoding="utf-8")
    markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "Category 4" in markdown
    assert "output/odoo.log" in markdown


def test_preserve_database_only_for_failed_started_runs():
    assert should_preserve_database(False, False, True, False) is False
    assert should_preserve_database(True, True, True, False) is False
    assert should_preserve_database(True, False, False, False) is False
    assert should_preserve_database(True, False, True, False) is True
    assert should_preserve_database(False, True, True, True) is True


def test_cli_advertises_keep_db_on_failure():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--keep-db-on-failure" in result.output
    assert "--verbose" in result.output
    assert "--log-file" in result.output
    assert "--postgres-version" in result.output
