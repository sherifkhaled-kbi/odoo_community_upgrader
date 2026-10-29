"""Decide whether a legacy UoM category can be repaired for Odoo 19.

Odoo 18 stores one reference unit per category only as a Python constraint.
``uom.uom.write`` skips that check when the write sets ``uom_type`` to
``reference``, and the SQL constraint only requires a reference factor of 1.
OpenUpgrade 19 then copies ``factor``, ``uom_type`` and ``category_id`` to
legacy columns and deletes ``factor_reference_is_one`` before the post-migration
script assigns every legacy reference row to ``relative_uom_id``. That field is
a many2one, so more than one reference row raises ``ValueError``.

This validator does not invent a reference. A repair is returned only when the
legacy factors and authoritative ``uom`` XML IDs identify one canonical
reference and a quantity-preserving ratio for every other unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

# Standard category → reference unit XML IDs shipped by the Odoo 18 ``uom``
# module. The square-meter XML ID is the name OpenUpgrade writes before the
# 19.0 post-migration runs. The pre-rename id is accepted as well.
CATEGORY_REFERENCE_XMLID: Dict[str, str] = {
    "uom.product_uom_categ_unit": "uom.product_uom_unit",
    "uom.product_uom_categ_kgm": "uom.product_uom_kgm",
    "uom.uom_categ_wtime": "uom.product_uom_day",
    "uom.uom_categ_length": "uom.product_uom_meter",
    "uom.uom_categ_surface": "uom.product_uom_square_meter",
    "uom.product_uom_categ_vol": "uom.product_uom_litre",
    "uom.product_uom_categ_energy": "uom.product_uom_kwh",
}

AUTHORITATIVE_REFERENCE_XMLIDS = frozenset(CATEGORY_REFERENCE_XMLID.values()) | {
    "uom.uom_square_meter",
}

ONE = Decimal("1")
FACTOR_TOLERANCE = Decimal("1e-9")
STATUS_UNCHANGED = "unchanged"
STATUS_REPAIRED = "repaired"
STATUS_STOP = "stop"


@dataclass(frozen=True)
class UomUnit:
    """One legacy unit inside a category."""

    id: int
    factor: object
    uom_type: Optional[str]
    xml_ids: Tuple[str, ...] = ()
    name: Optional[str] = None


@dataclass(frozen=True)
class UomCategory:
    """Legacy category snapshot used for a repair decision."""

    category_id: int
    units: Tuple[UomUnit, ...]
    category_xml_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class UomAssignment:
    """A single many2one write that preserves a known conversion."""

    uom_id: int
    relative_uom_id: int
    relative_factor: Decimal
    reason: str


@dataclass
class CategoryDecision:
    """Audit record for one legacy category."""

    category_id: int
    status: str
    explanation: str
    canonical_reference_id: Optional[int] = None
    assignments: Tuple[UomAssignment, ...] = ()
    units: List[dict] = field(default_factory=list)
    repair_decision: str = ""

    @property
    def stopped(self) -> bool:
        return self.status == STATUS_STOP


def _decimal(value: object) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def factor_problem(value: object) -> Optional[str]:
    """Return why ``value`` cannot be used as a conversion factor."""

    number = _decimal(value)
    if number is None:
        return "null_or_non_numeric"
    if number.is_nan():
        return "not_finite"
    if number.is_infinite():
        return "not_finite"
    if number == 0:
        return "zero"
    if number < 0:
        return "negative"
    return None


def is_factor_one(value: object) -> bool:
    number = _decimal(value)
    if number is None or factor_problem(value):
        return False
    return abs(number - ONE) <= FACTOR_TOLERANCE


def openupgrade_relative_factor(factor: object, uom_type: Optional[str]) -> Decimal:
    """Reproduce the Odoo 19.0.1.0 UoM post-migration formula."""

    number = _decimal(factor)
    if number is None:
        raise ValueError("factor is not numeric")
    if uom_type == "bigger":
        return number
    return ONE / number


def preserved_relative_factor(factor: object, reference_factor: object = ONE) -> Decimal:
    """Ratio that keeps Odoo 18 ``_compute_quantity`` after the UoM becomes a tree.

    Odoo 18 converts with ``qty / factor * reference_factor``. Odoo 19 converts
    a child into its parent with ``qty * relative_factor``. Those agree when
    ``relative_factor = reference_factor / factor`` and the parent is the legacy
    reference unit.
    """

    number = _decimal(factor)
    reference = _decimal(reference_factor)
    if number is None or reference is None:
        raise ValueError("factor is not numeric")
    return reference / number


def _unit_audit(unit: UomUnit) -> dict:
    problem = factor_problem(unit.factor)
    number = _decimal(unit.factor)
    return {
        "id": unit.id,
        "name": unit.name,
        "uom_type": unit.uom_type,
        "factor": None if number is None or problem else format(number, "f"),
        "factor_problem": problem,
        "xml_ids": list(unit.xml_ids),
    }


def _expected_reference_xmlid(category_xml_ids: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    expected = []
    for xmlid in category_xml_ids:
        mapped = CATEGORY_REFERENCE_XMLID.get(xmlid)
        if mapped:
            expected.append(mapped)
    unique = set(expected)
    if len(unique) > 1:
        return None, (
            "Category XML IDs map to more than one standard reference unit: "
            + ", ".join(sorted(unique))
        )
    if len(unique) == 1:
        return expected[0], None
    return None, None


def _authoritative(unit: UomUnit) -> bool:
    return any(xmlid in AUTHORITATIVE_REFERENCE_XMLIDS for xmlid in unit.xml_ids)


def _has_xmlid(unit: UomUnit, xmlid: str) -> bool:
    return xmlid in unit.xml_ids


def _invalid_converted_units(units: Sequence[UomUnit], skipped_ids: Sequence[int]) -> List[UomUnit]:
    skipped = set(skipped_ids)
    invalid = []
    for unit in units:
        if unit.id in skipped:
            continue
        if unit.xml_ids and unit.uom_type != "reference":
            continue
        if factor_problem(unit.factor):
            invalid.append(unit)
    return invalid


def _stop(category: UomCategory, explanation: str, decision: str) -> CategoryDecision:
    return CategoryDecision(
        category_id=category.category_id,
        status=STATUS_STOP,
        explanation=explanation,
        repair_decision=decision,
        units=[_unit_audit(unit) for unit in category.units],
    )


def _select_canonical(category: UomCategory, references: Sequence[UomUnit]) -> Tuple[Optional[UomUnit], Optional[str]]:
    """Return the unique proven reference, or an explanation of why to stop."""

    expected_xmlid, conflict = _expected_reference_xmlid(category.category_xml_ids)
    if conflict:
        return None, conflict

    authoritative = [unit for unit in references if _authoritative(unit)]
    if len(authoritative) > 1:
        ids = ", ".join(str(unit.id) for unit in authoritative)
        return None, (
            "Multiple standard reference XML IDs are reference units in one category "
            f"({ids}). A standard XML ID is not treated as the parent of the other "
            "standard units. No repair was applied."
        )

    invalid = [unit for unit in references if factor_problem(unit.factor)]
    if invalid:
        ids = ", ".join(str(unit.id) for unit in invalid)
        return None, (
            f"Reference units {ids} have a null, zero, negative, or non-finite factor. "
            "The ratio cannot be preserved, and a factor of 1 is not substituted."
        )

    factor_one = [unit for unit in references if is_factor_one(unit.factor)]
    if expected_xmlid:
        matched = [unit for unit in factor_one if _has_xmlid(unit, expected_xmlid)]
        if len(matched) == 1:
            return matched[0], None
        if len(matched) > 1:
            return None, (
                f"More than one factor=1 unit carries the authoritative XML ID {expected_xmlid}."
            )
        return None, (
            f"Category metadata expects {expected_xmlid}, but that unit is not a "
            "factor=1 reference in this category."
        )

    if len(authoritative) == 1:
        if is_factor_one(authoritative[0].factor):
            return authoritative[0], None
        return None, (
            f"Standard reference unit {authoritative[0].id} does not have factor 1. "
            "Its conversions were not rewritten."
        )
    if len(factor_one) == 1:
        return factor_one[0], None
    if not factor_one:
        return None, (
            "No reference unit has the required factor of 1, so none satisfies the "
            "Odoo reference invariant."
        )
    ids = ", ".join(str(unit.id) for unit in factor_one)
    return None, (
        "Several reference units have factor 1 and none has a standard reference "
        f"XML ID ({ids}). They are equivalent, but choosing one as the parent would "
        "be a guess. No repair was applied."
    )


def analyze_category(category: UomCategory) -> CategoryDecision:
    """Classify one legacy category and, when safe, describe the writes to perform."""

    references = [unit for unit in category.units if unit.uom_type == "reference"]
    if len(references) == 1:
        reference = references[0]
        invalid = _invalid_converted_units(category.units, [reference.id])
        if invalid:
            ids = ", ".join(str(unit.id) for unit in invalid)
            return _stop(
                category,
                "Units "
                f"{ids} have a conversion factor that cannot be preserved. "
                "OpenUpgrade would divide by that factor or copy it. No factor was replaced with 1.",
                "invalid_conversion_factor",
            )
        assignments = _assignments_for_regular_units(category.units, reference, set())
        return CategoryDecision(
            category_id=category.category_id,
            status=STATUS_UNCHANGED,
            explanation="Exactly one legacy reference unit. OpenUpgrade's own ratio formula is used.",
            canonical_reference_id=reference.id,
            assignments=tuple(assignments),
            repair_decision="single_reference",
            units=[_unit_audit(unit) for unit in category.units],
        )

    if not references:
        return _analyze_missing_reference(category)

    canonical, reason = _select_canonical(category, references)
    if canonical is None:
        return _stop(category, reason or "Ambiguous reference units.", "ambiguous_reference")

    reclassified = [unit for unit in references if unit.id != canonical.id]
    invalid = _invalid_converted_units(
        category.units,
        [canonical.id],
    )
    if invalid:
        ids = ", ".join(str(unit.id) for unit in invalid)
        return _stop(
            category,
            f"Units {ids} have a conversion factor that cannot be preserved.",
            "invalid_conversion_factor",
        )

    reclassified_ids = {unit.id for unit in reclassified}
    assignments = _assignments_for_regular_units(category.units, canonical, reclassified_ids)
    for unit in reclassified:
        number = _decimal(unit.factor)
        assert number is not None
        assignments.append(
            UomAssignment(
                uom_id=unit.id,
                relative_uom_id=canonical.id,
                relative_factor=preserved_relative_factor(number, ONE),
                reason=(
                    "Duplicate reference. Legacy factor is unchanged and relative_factor "
                    "is reference_factor / factor, which keeps the Odoo 18 quantity ratio."
                ),
            )
        )
    return CategoryDecision(
        category_id=category.category_id,
        status=STATUS_REPAIRED,
        explanation=(
            f"Canonical reference is uom.uom {canonical.id}. "
            f"{len(reclassified)} other reference rows are linked to it without "
            "changing their legacy factors."
        ),
        canonical_reference_id=canonical.id,
        assignments=tuple(assignments),
        repair_decision="repaired_duplicate_references",
        units=[_unit_audit(unit) for unit in category.units],
    )


def _analyze_missing_reference(category: UomCategory) -> CategoryDecision:
    expected_xmlid, conflict = _expected_reference_xmlid(category.category_xml_ids)
    if conflict:
        return _stop(category, conflict, "ambiguous_reference")

    candidates: List[UomUnit] = []
    if expected_xmlid:
        candidates = [
            unit
            for unit in category.units
            if _has_xmlid(unit, expected_xmlid) and is_factor_one(unit.factor)
        ]
    if not candidates:
        candidates = [
            unit for unit in category.units if _authoritative(unit) and is_factor_one(unit.factor)
        ]
    if len(candidates) != 1:
        return _stop(
            category,
            "The category has no legacy reference unit, and authoritative UoM metadata "
            "does not identify exactly one factor=1 reference. The first unit was not selected.",
            "missing_reference",
        )
    canonical = candidates[0]
    invalid = _invalid_converted_units(category.units, [canonical.id])
    if invalid:
        ids = ", ".join(str(unit.id) for unit in invalid)
        return _stop(
            category,
            f"Units {ids} have a conversion factor that cannot be preserved.",
            "invalid_conversion_factor",
        )
    assignments = _assignments_for_regular_units(category.units, canonical, set())
    return CategoryDecision(
        category_id=category.category_id,
        status=STATUS_REPAIRED,
        explanation=(
            f"No row had uom_type reference. {canonical.xml_ids[0] if canonical.xml_ids else canonical.id} "
            "is the authoritative factor=1 unit and is used as the parent."
        ),
        canonical_reference_id=canonical.id,
        assignments=tuple(assignments),
        repair_decision="restored_missing_reference",
        units=[_unit_audit(unit) for unit in category.units],
    )


def _assignments_for_regular_units(
    units: Sequence[UomUnit],
    canonical: UomUnit,
    reclassified_ids: set,
) -> List[UomAssignment]:
    """Assignments that follow OpenUpgrade for units that were not duplicate references."""

    assignments: List[UomAssignment] = []
    for unit in units:
        if unit.id == canonical.id or unit.id in reclassified_ids:
            continue
        if unit.xml_ids:
            continue
        problem = factor_problem(unit.factor)
        if problem:
            continue
        assignments.append(
            UomAssignment(
                uom_id=unit.id,
                relative_uom_id=canonical.id,
                relative_factor=openupgrade_relative_factor(unit.factor, unit.uom_type),
                reason="OpenUpgrade 19.0.1.0 ratio for a non-reference unit without an XML ID.",
            )
        )
    return assignments
