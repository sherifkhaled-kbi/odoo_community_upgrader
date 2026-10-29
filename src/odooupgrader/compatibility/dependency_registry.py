"""Pinned OpenUpgrade and openupgradelib revisions.

Revisions are full Git commit ids, not floating branch tips. Versions that are
absent from this registry keep the historical install path: clone the matching
OpenUpgrade branch and install ``requirements.txt`` as published.

Verified on 2026-10-29:

* ``openupgradelib`` 3.13.7 on PyPI is commit ``a6134fa5`` (2026-08-25) and does
  not define ``add_inherits_parent_xmlids``.
* Commit ``5dfe3b48`` (2026-10-27) adds that function. OpenUpgrade 18.0 commit
  ``a393ddbe`` (2026-10-27) calls it from ``base/18.0.1.3/pre-migration.py``.
* OpenUpgrade 19.0 commit ``bf12c3ce`` (2026-10-27) contains the UoM
  post-migration script this project patches. The same openupgradelib commit is
  pinned so a later PyPI release cannot change the image. No 19.0 script is
  required to call ``add_inherits_parent_xmlids``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class DependencySpec:
    """Immutable dependency pin for one OpenUpgrade target series."""

    odoo_version: str
    openupgrade_branch: str
    openupgrade_revision: str
    openupgradelib_revision: str
    required_functions: Tuple[str, ...]
    notes: str


OPENUPGRADE_DEPENDENCIES: Dict[str, DependencySpec] = {
    "18.0": DependencySpec(
        odoo_version="18.0",
        openupgrade_branch="18.0",
        openupgrade_revision="a393ddbe56b6913eea1e77b42be93152ac00b16d",
        openupgradelib_revision="5dfe3b485da5cfaa1f8c528c183cbdd0bc8248b0",
        required_functions=("add_inherits_parent_xmlids",),
        notes=(
            "OpenUpgrade 18.0 calls openupgrade.add_inherits_parent_xmlids during "
            "the base pre-migration. PyPI openupgradelib 3.13.7 does not provide it."
        ),
    ),
    "19.0": DependencySpec(
        odoo_version="19.0",
        openupgrade_branch="19.0",
        openupgrade_revision="bf12c3cee6097145e53efa589b0d99a7e64d6a87",
        openupgradelib_revision="5dfe3b485da5cfaa1f8c528c183cbdd0bc8248b0",
        required_functions=(),
        notes=(
            "Pinned to the 19.0 commit that contains uom/19.0.1.0/post-migration.py. "
            "openupgradelib is pinned to the same immutable commit used for 18.0."
        ),
    ),
}


def get_dependency_spec(odoo_version: str) -> Optional[DependencySpec]:
    """Return the pin for ``odoo_version``, or None when install stays historical."""

    return OPENUPGRADE_DEPENDENCIES.get(odoo_version)


def iter_dependency_specs() -> Mapping[str, DependencySpec]:
    return OPENUPGRADE_DEPENDENCIES
