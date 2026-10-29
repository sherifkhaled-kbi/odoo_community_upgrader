"""Checks that an imported openupgradelib exposes the functions a migration calls."""

from __future__ import annotations

from typing import Iterable, List, Sequence


def missing_required_functions(module: object, required: Sequence[str]) -> List[str]:
    """Return required attribute names that ``module`` does not provide."""

    return [name for name in required if not hasattr(module, name)]


def format_missing_function_error(odoo_version: str, revision: str, missing: Iterable[str]) -> str:
    names = ", ".join(missing)
    return (
        f"openupgradelib compatibility check failed for Odoo {odoo_version}. "
        f"Revision {revision} does not provide: {names}. "
        "The Docker build stops before migration so OpenUpgrade cannot fail later "
        "with AttributeError."
    )
