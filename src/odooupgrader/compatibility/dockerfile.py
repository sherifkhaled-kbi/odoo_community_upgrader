"""Render the OpenUpgrade image Dockerfile for one target Odoo series."""

from __future__ import annotations

from .dependency_registry import DependencySpec, get_dependency_spec
from .patch_registry import patches_for_version

COMPAT_IMAGE_ROOT = "/opt/odooupgrader_pkg"


def render_openupgrade_fetch(target_version: str) -> str:
    """Clone instructions. Pinned series fetch one commit; others keep branch clones."""

    spec = get_dependency_spec(target_version)
    # The official image already owns /mnt/extra-addons as the odoo user.
    # Git refuses to use that directory until it is marked safe.
    safe = "git config --global --add safe.directory /mnt/extra-addons"
    if spec is None:
        return (
            f"RUN {safe} \\\n"
            " && git clone https://github.com/OCA/OpenUpgrade.git "
            f"--depth 1 --branch {target_version} /mnt/extra-addons \\\n"
            " && chown -R odoo:odoo /mnt/extra-addons"
        )
    revision = spec.openupgrade_revision
    return (
        f"RUN {safe} \\\n"
        " && git init /mnt/extra-addons \\\n"
        " && git -C /mnt/extra-addons remote add origin https://github.com/OCA/OpenUpgrade.git \\\n"
        f" && git -C /mnt/extra-addons fetch --depth 1 origin {revision} \\\n"
        " && git -C /mnt/extra-addons checkout --detach FETCH_HEAD \\\n"
        " && chown -R odoo:odoo /mnt/extra-addons"
    )


def render_pip_install(target_version: str) -> str:
    """Install OpenUpgrade requirements, then force the pinned openupgradelib commit.

    ``requirements.txt`` lists ``openupgradelib`` without a version. Installing it
    first pulls the library's own dependencies from PyPI. The pinned commit is
    then installed with ``--no-deps`` so the image does not rebuild Odoo's
    dependency set, and so a later unpinned requirement cannot replace the pin.
    """

    spec = get_dependency_spec(target_version)
    pin = ""
    if spec is not None:
        pin = (
            " \\\n"
            " && pip3 install $PIP_FLAGS --no-cache-dir --force-reinstall --no-deps \\\n"
            f'      "git+https://github.com/OCA/openupgradelib.git@{spec.openupgradelib_revision}"'
        )
    return (
        "RUN if pip3 install --help | grep -q -- '--break-system-packages'; then \\\n"
        '      PIP_FLAGS="--break-system-packages"; \\\n'
        "    else \\\n"
        '      PIP_FLAGS=""; \\\n'
        "    fi; \\\n"
        "    pip3 install $PIP_FLAGS --no-cache-dir -r /mnt/extra-addons/requirements.txt"
        f"{pin}"
    )


def render_dependency_check(spec: DependencySpec) -> str:
    required = ", ".join(repr(name) for name in spec.required_functions)
    return (
        "RUN python3 - <<'PY'\n"
        "import sys\n"
        "try:\n"
        "    # Odoo 19 exposes these only after the submodule is imported.\n"
        "    import odoo.api  # noqa: F401\n"
        "    import odoo.exceptions  # noqa: F401\n"
        "    import odoo.fields  # noqa: F401\n"
        "    import odoo.tools  # noqa: F401\n"
        "except Exception as exc:\n"
        "    sys.stderr.write('Could not import Odoo before openupgradelib: %s\\n' % exc)\n"
        "    sys.exit(1)\n"
        "from openupgradelib import openupgrade\n"
        f"required = [{required}]\n"
        "missing = [name for name in required if not hasattr(openupgrade, name)]\n"
        "if missing:\n"
        "    sys.stderr.write(\n"
        f'        "openupgradelib compatibility check failed for Odoo {spec.odoo_version}.\\n"\n'
        f'        "Revision {spec.openupgradelib_revision} does not provide: %s\\n"\n'
        '        % ", ".join(missing)\n'
        "    )\n"
        "    sys.stderr.write(\n"
        '        "The Docker image was not built because a required migration function is missing.\\n"\n'
        "    )\n"
        "    sys.exit(1)\n"
        f'print("openupgradelib OK for Odoo {spec.odoo_version}")\n'
        "PY"
    )


def render_patch_steps(target_version: str) -> str:
    if not patches_for_version(target_version):
        return ""
    return (
        f"COPY migration_build/pkg/ {COMPAT_IMAGE_ROOT}/\n"
        "RUN PYTHONPATH=/opt/odooupgrader_pkg python3 -m "
        "odooupgrader.compatibility.patch_manager "
        f"--addons /mnt/extra-addons --version {target_version}\n"
        "RUN python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "path = Path('/mnt/extra-addons/openupgrade_scripts/scripts/uom/19.0.1.0/post-migration.py')\n"
        "text = path.read_text(encoding='utf-8')\n"
        "if 'odooupgrader-patch: uom_migration_repair version=1' not in text:\n"
        "    raise SystemExit('UoM migration repair patch marker is missing')\n"
        "if 'apply_uom_reference_repairs' not in text:\n"
        "    raise SystemExit('UoM migration repair hook is missing')\n"
        "if 'def _uom_relative_uom_id_relative_factor' not in text:\n"
        "    raise SystemExit('UoM post-migration function is missing')\n"
        "print('UoM migration repair patch OK')\n"
        "PY"
    )


def render_dockerfile(target_version: str, extra_addons_cmds: str = "") -> str:
    """Full Dockerfile. ``extra_addons_cmds`` is the existing custom-addon block."""

    spec = get_dependency_spec(target_version)
    header = f"# OdooUpgrader migration image for {target_version}\n"
    if spec is not None:
        header += (
            f"# OpenUpgrade {spec.openupgrade_branch} @ {spec.openupgrade_revision}\n"
            f"# openupgradelib @ {spec.openupgradelib_revision}\n"
        )
    else:
        header += f"# OpenUpgrade branch {target_version} (unpinned historical install)\n"
    extra = extra_addons_cmds.strip()
    extra_block = f"\n{extra}\n" if extra else "\n"
    check = ""
    if spec is not None:
        check = "\n" + render_dependency_check(spec) + "\n"
    patches = render_patch_steps(target_version)
    patch_block = f"\n{patches}\n" if patches else "\n"
    return (
        f"{header}"
        f"FROM odoo:{target_version}\n"
        "USER root\n"
        "RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*\n"
        f"{render_openupgrade_fetch(target_version)}\n"
        f"{render_pip_install(target_version)}\n"
        f"{check}"
        f"{patch_block}"
        f"{extra_block}"
        "USER odoo\n"
    )
