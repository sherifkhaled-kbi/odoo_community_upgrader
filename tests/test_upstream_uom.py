"""Execute exact pinned upstream UoM functions against a minimal ORM contract.

This proves script mechanics, not live Odoo/financial correctness. No patch is
installed in the migration runtime. Both snapshots retain upstream licensing.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


class Unit:
    def __init__(self, identifier):
        self.id = identifier
        self.values = {}

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, Unit) and self.id == other.id

    def write(self, values):
        reference = values['relative_uom_id']
        if len(reference.units) != 1:
            raise ValueError('Wrong value for uom.uom.relative_uom_id')
        self.values = values


class Recordset:
    def __init__(self, units):
        self.units = list(units)

    def __iter__(self):
        return iter(self.units)

    def __sub__(self, other):
        return Recordset(x for x in self if x not in other.units)

    def filtered(self, function):
        return Recordset(filter(function, self))


class Environment:
    def __init__(self, rows):
        self.units = {i: Unit(i) for i in [1, 2, 3]}
        self.rows = rows
        self.calls = 0
        self.cr = self

    def __getitem__(self, name):
        assert name == 'uom.uom'
        return self

    def browse(self, ids):
        return Recordset(self.units[i] for i in ([ids] if isinstance(ids, int) else ids))

    def execute(self, sql):
        self.calls += 1

    def fetchall(self):
        return [] if self.calls == 1 else self.rows


def load_function(filename):
    path = Path(__file__).parent / 'fixtures' / filename
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_uom_relative_uom_id_relative_factor')
    namespace = {'openupgrade': SimpleNamespace(get_legacy_name=lambda value: 'legacy_' + value)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[function.name]


def test_exact_affected_script_duplicate_reference_failure():
    env = Environment([([1, 2, 3], [1, 1, .5], ['reference', 'reference', 'bigger'])])
    with pytest.raises(ValueError, match='relative_uom_id'):
        load_function('uom_affected.py')(env)


def test_exact_affected_script_bigger_ratio_and_candidate_fix():
    old = Environment([([1, 2], [1, 1/24], ['reference', 'bigger'])])
    load_function('uom_affected.py')(old)
    assert old.units[2].values['relative_factor'] == 1/24
    proposed = Environment([(1, [2], [1/24])])
    load_function('uom_pr6054.py')(proposed)
    assert proposed.units[2].values['relative_factor'] == 24
    assert old.units[2].values['relative_factor'] != proposed.units[2].values['relative_factor']
