"""UoM reference repair decisions. These tests must not treat a crash as success."""

from decimal import Decimal

import pytest

from odooupgrader.compatibility.patches.v19.uom_migration_repair import (
    UomRepairError,
    apply_decisions,
    apply_uom_reference_repairs,
    decide_categories,
)
from odooupgrader.compatibility.validators.uom_validator import (
    UomCategory,
    UomUnit,
    analyze_category,
    openupgrade_relative_factor,
    preserved_relative_factor,
)


def unit(unit_id, factor, uom_type, xml_ids=(), name=None):
    return UomUnit(
        id=unit_id,
        factor=factor,
        uom_type=uom_type,
        xml_ids=tuple(xml_ids),
        name=name,
    )


def category(category_id, units, xml_ids=()):
    return UomCategory(
        category_id=category_id,
        units=tuple(units),
        category_xml_ids=tuple(xml_ids),
    )


def test_single_reference_uses_openupgrade_formula():
    decision = analyze_category(
        category(
            3,
            [
                unit(1, 1, "reference", ("uom.product_uom_kgm",), "kg"),
                unit(2, Decimal("0.001"), "bigger", name="tonne-like"),
                unit(3, 1000, "smaller", name="g-like"),
                unit(4, 2.20462, "smaller", ("uom.product_uom_lb",), "lb"),
            ],
            ("uom.product_uom_categ_kgm",),
        )
    )
    assert decision.status == "unchanged"
    assert decision.canonical_reference_id == 1
    by_id = {item.uom_id: item for item in decision.assignments}
    assert set(by_id) == {2, 3}
    assert by_id[2].relative_uom_id == 1
    assert by_id[2].relative_factor == openupgrade_relative_factor(Decimal("0.001"), "bigger")
    assert by_id[3].relative_factor == openupgrade_relative_factor(1000, "smaller")
    assert all(isinstance(item.relative_uom_id, int) for item in decision.assignments)


def test_duplicate_references_with_one_factor_one_preserve_ratio():
    decision = analyze_category(
        category(
            9,
            [
                unit(10, 1, "reference", name="base"),
                unit(11, 4, "reference", name="mislabeled-smaller"),
                unit(12, Decimal("0.5"), "reference", name="mislabeled-bigger"),
                unit(13, 8, "smaller", name="already-smaller"),
            ],
        )
    )
    assert decision.status == "repaired"
    assert decision.canonical_reference_id == 10
    by_id = {item.uom_id: item for item in decision.assignments}
    assert by_id[11].relative_factor == preserved_relative_factor(4)
    assert by_id[11].relative_factor == Decimal("0.25")
    assert by_id[12].relative_factor == preserved_relative_factor(Decimal("0.5"))
    assert by_id[12].relative_factor == Decimal("2")
    assert by_id[13].relative_factor == openupgrade_relative_factor(8, "smaller")
    assert {item.relative_uom_id for item in decision.assignments} == {10}


def test_equivalent_references_use_standard_xmlid_without_resetting_factor():
    custom_ids = list(range(20, 31))
    units = [unit(8, 1, "reference", ("uom.product_uom_unit",), "Units")]
    units.extend(unit(unit_id, 1, "reference", name=f"custom-{unit_id}") for unit_id in custom_ids)
    decision = analyze_category(
        category(1, units, ("uom.product_uom_categ_unit",))
    )
    assert decision.status == "repaired"
    assert decision.canonical_reference_id == 8
    assert {item.uom_id for item in decision.assignments} == set(custom_ids)
    assert {item.relative_factor for item in decision.assignments} == {Decimal("1")}
    assert {item.relative_uom_id for item in decision.assignments} == {8}


def test_ambiguous_equivalent_references_stop():
    decision = analyze_category(
        category(
            4,
            [
                unit(30, 1, "reference", name="a"),
                unit(31, 1, "reference", name="b"),
                unit(32, 10, "smaller", name="child"),
            ],
        )
    )
    assert decision.status == "stop"
    assert decision.assignments == ()
    assert decision.repair_decision == "ambiguous_reference"
    assert "30" in decision.explanation and "31" in decision.explanation


def test_two_standard_references_in_one_category_stop():
    decision = analyze_category(
        category(
            5,
            [
                unit(1, 1, "reference", ("uom.product_uom_kgm",)),
                unit(2, 1, "reference", ("uom.product_uom_unit",)),
                unit(3, 1000, "smaller"),
            ],
            ("uom.product_uom_categ_kgm",),
        )
    )
    assert decision.status == "stop"
    assert decision.assignments == ()
    assert "standard" in decision.explanation.lower()


def test_missing_reference_without_authority_stops():
    decision = analyze_category(
        category(6, [unit(40, 1, "smaller"), unit(41, 10, "bigger")])
    )
    assert decision.status == "stop"
    assert decision.repair_decision == "missing_reference"
    assert decision.assignments == ()
    assert "first" in decision.explanation.lower()


def test_missing_reference_restores_authoritative_unit():
    decision = analyze_category(
        category(
            7,
            [
                unit(50, 1, "smaller", ("uom.product_uom_meter",), "m"),
                unit(51, 1000, "smaller", name="mm"),
            ],
            ("uom.uom_categ_length",),
        )
    )
    assert decision.status == "repaired"
    assert decision.canonical_reference_id == 50
    assert decision.assignments[0].uom_id == 51
    assert decision.assignments[0].relative_uom_id == 50
    assert decision.assignments[0].relative_factor == Decimal("0.001")


def test_zero_null_and_negative_factors_stop_without_substitution():
    base = unit(1, 1, "reference")
    zero = analyze_category(category(1, [base, unit(2, 0, "smaller")]))
    null = analyze_category(category(1, [base, unit(2, None, "smaller")]))
    negative = analyze_category(category(1, [base, unit(2, -5, "bigger")]))
    for decision in (zero, null, negative):
        assert decision.status == "stop"
        assert decision.repair_decision == "invalid_conversion_factor"
        assert decision.assignments == ()
        assert "1" not in decision.explanation or "not substituted" in decision.explanation or "cannot" in decision.explanation


def test_xml_ids_do_not_make_an_ambiguous_category_safe():
    decision = analyze_category(
        category(
            11,
            [
                unit(60, 1, "reference", ("custom.box",)),
                unit(61, 1, "reference", ("custom.case",)),
            ],
        )
    )
    assert decision.status == "stop"
    assert decision.assignments == ()


def test_repeat_analysis_is_stable():
    snapshot = category(
        12,
        [
            unit(70, 1, "reference", ("uom.product_uom_litre",)),
            unit(71, 1, "reference"),
            unit(72, 33.814, "smaller"),
        ],
        ("uom.product_uom_categ_vol",),
    )
    first = analyze_category(snapshot)
    second = analyze_category(snapshot)
    assert first.status == second.status == "repaired"
    assert [(item.uom_id, item.relative_uom_id, item.relative_factor) for item in first.assignments] == [
        (item.uom_id, item.relative_uom_id, item.relative_factor) for item in second.assignments
    ]
    by_id = {item.uom_id: item for item in first.assignments}
    assert by_id[71].relative_factor == Decimal("1")
    assert by_id[72].relative_factor == openupgrade_relative_factor(33.814, "smaller")


def test_legacy_factor_is_not_part_of_the_write():
    decision = analyze_category(
        category(13, [unit(80, 1, "reference"), unit(81, 4, "reference")])
    )
    assert decision.status == "repaired"
    assert decision.assignments[0].relative_factor == Decimal("0.25")


class _Cursor:
    def __init__(self, fetches):
        self.fetches = list(fetches)
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append(query)

    def fetchall(self):
        if not self.fetches:
            return []
        return self.fetches.pop(0)


class _Record:
    def __init__(self, model, record_id):
        self.model = model
        self.record_id = record_id

    def write(self, values):
        if self.model.fail:
            raise RuntimeError("write failed")
        self.model.writes.append((self.record_id, values))


class _Model:
    def __init__(self):
        self.writes = []
        self.fail = False

    def browse(self, record_id):
        assert isinstance(record_id, int)
        return _Record(self, record_id)


class _Env:
    def __init__(self, cursor):
        self.cr = cursor
        self.model = _Model()

    def __getitem__(self, name):
        assert name == "uom.uom"
        return self.model


class _OpenUpgrade:
    def get_legacy_name(self, column):
        return f"openupgrade_legacy_18_0_{column}"


def test_stop_rolls_back_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOOUPGRADER_REPAIR_REPORT", str(tmp_path / "uom-repair-report.json"))
    cursor = _Cursor(
        [
            [(1, 4, 1, "reference", "a"), (2, 4, 1, "reference", "b")],
            [],
            [],
        ]
    )
    env = _Env(cursor)
    with pytest.raises(UomRepairError) as caught:
        apply_uom_reference_repairs(env, _OpenUpgrade())
    assert env.model.writes == []
    assert not any("SAVEPOINT" in statement for statement in cursor.statements)
    assert caught.value.payload["repair_status"] == "requires_manual_review"
    report = (tmp_path / "uom-repair-report.json").read_text(encoding="utf-8")
    assert "ambiguous" in report or "guess" in report
    assert "password" not in report.lower()


def test_safe_repair_writes_a_single_parent_and_can_roll_back(monkeypatch, tmp_path):
    monkeypatch.setenv("ODOOUPGRADER_REPAIR_REPORT", str(tmp_path / "repair.json"))
    cursor = _Cursor(
        [
            [
                (8, 1, 1, "reference", "Units"),
                (9, 1, 1, "reference", "Alias"),
            ],
            [(8, "uom", "product_uom_unit")],
            [(1, "uom", "product_uom_categ_unit")],
        ]
    )
    env = _Env(cursor)
    apply_uom_reference_repairs(env, _OpenUpgrade())
    assert env.model.writes == [
        (9, {"relative_uom_id": 8, "relative_factor": 1.0}),
    ]
    assert any(statement.startswith("SAVEPOINT") for statement in cursor.statements)
    assert any(statement.startswith("RELEASE SAVEPOINT") for statement in cursor.statements)

    decisions = decide_categories(
        [
            category(
                1,
                [
                    unit(8, 1, "reference", ("uom.product_uom_unit",)),
                    unit(9, 1, "reference"),
                ],
                ("uom.product_uom_categ_unit",),
            )
        ]
    )
    env.model.fail = True
    cursor.statements.clear()
    with pytest.raises(RuntimeError, match="write failed"):
        apply_decisions(env, decisions)
    assert any(statement.startswith("ROLLBACK TO SAVEPOINT") for statement in cursor.statements)


def test_unsafe_column_name_is_rejected():
    cursor = _Cursor([])

    class BadOpenUpgrade:
        def get_legacy_name(self, column):
            return "factor; drop table uom_uom"

    with pytest.raises(UomRepairError):
        apply_uom_reference_repairs(_Env(cursor), BadOpenUpgrade())
    assert cursor.statements == []
