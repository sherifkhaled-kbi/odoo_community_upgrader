"""Version-aware OpenUpgrade compatibility pins, patches, and diagnostics."""

from .dependency_registry import OPENUPGRADE_DEPENDENCIES, get_dependency_spec

__all__ = ["OPENUPGRADE_DEPENDENCIES", "get_dependency_spec"]
