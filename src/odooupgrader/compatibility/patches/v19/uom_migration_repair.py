"""Apply a checked UoM reference repair inside the Odoo 19 migration.

The function is imported by the patched OpenUpgrade post-migration script. It
writes ``relative_uom_id`` only as a single record id. Legacy ``factor`` values
are never updated, and no unit of measure row is deleted.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from odooupgrader.compatibility.validators.uom_validator import (
        STATUS_STOP,
        CategoryDecision,
        UomCategory,
        UomUnit,
        analyze_category,
    )
except ImportError:  # Flat copy path kept for tests that load the module by file.
    from uom_validator import (  # type: ignore
        STATUS_STOP,
        CategoryDecision,
        UomCategory,
        UomUnit,
        analyze_category,
    )

PATCH_ID = "uom_migration_repair"
PATCH_VERSION = 1
LOGGER = logging.getLogger("odooupgrader.compatibility.uom")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REPAIR_LOG_PREFIX = "ODOOUPGRADER_UOM_REPAIR"


class UomRepairError(RuntimeError):
    """Raised when a UoM category cannot be repaired without guessing."""

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(payload.get("explanation") or "UoM migration repair stopped")


def _check_identifier(name: str) -> str:
    if not _IDENT.match(name):
        raise UomRepairError(
            {
                "status": "requires_manual_review",
                "explanation": f"Refusing to interpolate unsafe column name {name!r}.",
            }
        )
    return name


def load_categories(cursor: object, factor_col: str, type_col: str, category_col: str) -> List[UomCategory]:
    """Read legacy UoM rows and XML IDs. Column names come from openupgradelib."""

    factor_col = _check_identifier(factor_col)
    type_col = _check_identifier(type_col)
    category_col = _check_identifier(category_col)
    cursor.execute(
        f"""
        SELECT u.id, u.{category_col}, u.{factor_col}, u.{type_col}, u.name::text
        FROM uom_uom u
        WHERE u.{category_col} IS NOT NULL
        ORDER BY u.{category_col}, u.id
        """
    )
    unit_rows = list(cursor.fetchall())
    cursor.execute(
        """
        SELECT res_id, module, name
        FROM ir_model_data
        WHERE model = 'uom.uom' AND module != '__export__'
        """
    )
    uom_xml = _xmlids_by_res(cursor.fetchall())
    cursor.execute(
        """
        SELECT res_id, module, name
        FROM ir_model_data
        WHERE model = 'uom.category' AND module != '__export__'
        """
    )
    category_xml = _xmlids_by_res(cursor.fetchall())
    grouped: Dict[int, List[UomUnit]] = {}
    for row in unit_rows:
        unit_id, category_id, factor, uom_type, name = row
        grouped.setdefault(int(category_id), []).append(
            UomUnit(
                id=int(unit_id),
                factor=factor,
                uom_type=uom_type,
                xml_ids=tuple(uom_xml.get(int(unit_id), ())),
                name=None if name is None else str(name),
            )
        )
    categories = []
    for category_id, units in grouped.items():
        categories.append(
            UomCategory(
                category_id=category_id,
                units=tuple(units),
                category_xml_ids=tuple(category_xml.get(category_id, ())),
            )
        )
    return categories


def _xmlids_by_res(rows: Iterable[Sequence[object]]) -> Dict[int, Tuple[str, ...]]:
    found: Dict[int, List[str]] = {}
    for res_id, module, name in rows:
        found.setdefault(int(res_id), []).append(f"{module}.{name}")
    return {key: tuple(value) for key, value in found.items()}


def decide_categories(categories: Sequence[UomCategory]) -> List[CategoryDecision]:
    return [analyze_category(category) for category in categories]


def repair_payload(decisions: Sequence[CategoryDecision], openupgrade_revision: str) -> dict:
    stopped = [decision for decision in decisions if decision.stopped]
    repaired = [decision for decision in decisions if decision.status == "repaired"]
    if stopped:
        status = "requires_manual_review"
    elif repaired:
        status = "repaired"
    else:
        status = "unchanged"
    explanations = [decision.explanation for decision in stopped or repaired or decisions[:1]]
    return {
        "compatibility_patch": PATCH_ID,
        "patch_version": PATCH_VERSION,
        "openupgrade_revision": openupgrade_revision,
        "repair_status": status,
        "explanation": " ".join(explanations),
        "categories": [_decision_dict(decision) for decision in decisions if decision.status != "unchanged"],
        "unchanged_categories": sum(1 for decision in decisions if decision.status == "unchanged"),
    }


def _decision_dict(decision: CategoryDecision) -> dict:
    return {
        "legacy_category_id": decision.category_id,
        "repair_decision": decision.repair_decision,
        "status": decision.status,
        "explanation": decision.explanation,
        "canonical_reference_id": decision.canonical_reference_id,
        "reference_candidate_ids": [
            unit["id"] for unit in decision.units if unit.get("uom_type") == "reference"
        ],
        "affected_uom_ids": [unit["id"] for unit in decision.units],
        "units": decision.units,
        "assignments": [
            {
                "uom_id": item.uom_id,
                "relative_uom_id": item.relative_uom_id,
                "relative_factor": format(item.relative_factor, "f"),
                "reason": item.reason,
            }
            for item in decision.assignments
        ],
    }


def apply_decisions(env: object, decisions: Sequence[CategoryDecision]) -> None:
    """Write repair and normal UoM links. Raises before any write when a category stops."""

    if any(decision.stopped for decision in decisions):
        raise UomRepairError(repair_payload(decisions, ""))
    cursor = env.cr
    cursor.execute("SAVEPOINT odooupgrader_uom_repair")
    try:
        Uom = env["uom.uom"]
        for decision in decisions:
            if decision.canonical_reference_id is None:
                continue
            for assignment in decision.assignments:
                if assignment.relative_uom_id != decision.canonical_reference_id:
                    raise UomRepairError(
                        {
                            "repair_status": "requires_manual_review",
                            "explanation": (
                                f"Refusing to point uom.uom {assignment.uom_id} at "
                                "more than one reference."
                            ),
                        }
                    )
                Uom.browse(assignment.uom_id).write(
                    {
                        "relative_uom_id": assignment.relative_uom_id,
                        "relative_factor": float(assignment.relative_factor),
                    }
                )
        cursor.execute("RELEASE SAVEPOINT odooupgrader_uom_repair")
    except Exception:
        cursor.execute("ROLLBACK TO SAVEPOINT odooupgrader_uom_repair")
        raise


def _write_report(payload: dict) -> None:
    path = os.environ.get("ODOOUPGRADER_REPAIR_REPORT", "/var/log/odoo/uom-repair-report.json")
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        LOGGER.warning("Could not write UoM repair report %s: %s", path, exc)


def _log_payload(payload: dict) -> None:
    encoded = json.dumps(payload, sort_keys=True)
    if payload.get("repair_status") == "requires_manual_review":
        LOGGER.error("%s %s", REPAIR_LOG_PREFIX, encoded)
    else:
        LOGGER.info("%s %s", REPAIR_LOG_PREFIX, encoded)


def apply_uom_reference_repairs(env: object, openupgrade: object) -> None:
    """Entry point used by the patched ``uom`` post-migration script."""

    revision = os.environ.get("ODOOUPGRADER_OPENUPGRADE_REVISION", "")
    factor_col = openupgrade.get_legacy_name("factor")
    type_col = openupgrade.get_legacy_name("uom_type")
    category_col = openupgrade.get_legacy_name("category_id")
    categories = load_categories(env.cr, factor_col, type_col, category_col)
    decisions = decide_categories(categories)
    payload = repair_payload(decisions, revision)
    _write_report(payload)
    _log_payload(payload)
    if payload["repair_status"] == "requires_manual_review":
        raise UomRepairError(payload)
    apply_decisions(env, decisions)
