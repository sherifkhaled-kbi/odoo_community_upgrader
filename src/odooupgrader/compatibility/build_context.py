"""Copy the compatibility package into the Docker build context.

The running tool may be installed from a wheel, so the image must not read
``src/`` from the developer's checkout or from ``/tmp``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

COMPAT_PACKAGE = Path(__file__).resolve().parent


def prepare_image_package(context_dir: Path) -> Path:
    """Write ``migration_build/pkg/odooupgrader`` under ``context_dir``."""

    package_root = context_dir / "migration_build" / "pkg" / "odooupgrader"
    if package_root.exists():
        shutil.rmtree(package_root)
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text(
        '"""Compatibility package copied into the migration image."""\n',
        encoding="utf-8",
    )
    shutil.copytree(
        COMPAT_PACKAGE,
        package_root / "compatibility",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return package_root
