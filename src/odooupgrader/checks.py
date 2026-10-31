"""Read-only source validation rules; no source normalization or upstream patch."""
import math
from pathlib import Path, PurePosixPath
import stat
import zipfile
from .jobs import RecoveryError, digest, require_space

OPENUPGRADE_REVISIONS = {
    '17.0': '61daab09ab6c0d21234b96374d9291d3e02ee9e4',
    '18.0': 'a393ddbe56b6913eea1e77b42be93152ac00b16d',
    '19.0': 'bf12c3cee6097145e53efa589b0d99a7e64d6a87',
}
# Stopgap: OCA/openupgradelib#462 ("lift_constraints: skip not-null constraints") fixes
# PG18's lift_constraints('...', 'id', cascade=True) failing with "column is in a primary
# key" (hit in stock_account/19.0.1.1 and hr_recruitment/19.0.1.1 post-migration scripts).
# Unmerged as of 2026-10-31; re-pin to an official revision once it lands upstream.
LIBRARY_REVISION = '327a371f9e176278c01b5d4f013cf4134180233e'
# Exact PR #6054 source tested in fixtures; never selected automatically.
FACTOR_FIX_SCRIPT_SHA256 = '85773a46db54fbeb8d5ed3e8358ca0ccc4c559864501144c3e75413a80d490a3'


def extract_zip(filename, destination, limit=200 * 1024**3):
    destination = Path(destination)
    with zipfile.ZipFile(filename) as archive:
        entries = archive.infolist()
        if len(entries) > 1000000 or sum(x.file_size for x in entries) > limit:
            raise RecoveryError('INPUT_LIMIT: archive exceeds expanded size or entry limit')
        require_space(destination, sum(x.file_size for x in entries))
        seen = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                    or not path.parts or path.parts[0].endswith(':')
                    or path.as_posix() in seen
                    or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))):
                raise RecoveryError('INPUT_UNSAFE: archive path/link/duplicate entry rejected')
            seen.add(path.as_posix())
        archive.extractall(destination)


def validate_uom(rows, factor_fix=False):
    """Rows include inactive units. SQL emits technical values, never business names."""
    categories = {}
    for row in rows:
        categories.setdefault(row['category'], []).append(row)
        factor = row['factor']
        if factor is None or not math.isfinite(float(factor)) or float(factor) <= 0:
            raise RecoveryError('UOM_FACTOR: invalid legacy factor; approved source-data investigation required')
    for units in categories.values():
        references = [x for x in units if x['type'] == 'reference']
        if len(references) != 1:
            raise RecoveryError('UOM_REFERENCE: category has zero/multiple references; review protected source diagnostics; no automatic repair')
        if float(references[0]['factor']) != 1:
            raise RecoveryError('UOM_REFERENCE: non-unit reference ratio is ambiguous')
    if not factor_fix and any(x['type'] == 'bigger' and not x['provider_xmlid'] for x in rows):
        raise RecoveryError('UOM_UPSTREAM_FACTOR: pinned 19.0 script inverts custom bigger units; select a reviewed corrected runtime before retry')


def verify_attachments(root, rows, allow_missing=False):
    root = Path(root).resolve()
    missing = 0
    for row in rows:
        name = row['store_fname']
        path = root / name
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or path.is_symlink():
            raise RecoveryError('FILESTORE_UNSAFE: invalid attachment path')
        if not path.resolve().is_relative_to(root):
            raise RecoveryError('FILESTORE_UNSAFE: attachment path escapes filestore')
        if not path.is_file():
            if not allow_missing:
                raise RecoveryError('FILESTORE_MISSING: referenced attachment missing')
            missing += 1
            continue
        if row.get('file_size') is not None and path.stat().st_size != row['file_size']:
            raise RecoveryError('FILESTORE_CORRUPT: attachment size mismatch')
        if row.get('checksum'):
            import hashlib
            h = hashlib.sha1()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != row['checksum']:
                raise RecoveryError('FILESTORE_CORRUPT: attachment checksum mismatch')
    return {'referenced_files': len(rows), 'missing_files': missing,
            'passed': missing == 0, 'missing_allowed': bool(allow_missing and missing)}


def verify_uom_ratios(before, after):
    """Compare 18 inverse factors to 19 absolute factors, permitting changed roots."""
    mapped = {row['id']: float(row['factor']) for row in after}
    anchors = {}
    for row in before:
        source = float(row['factor'])
        target = mapped.get(row['id'])
        if target is None or not math.isfinite(target) or target <= 0 or not math.isfinite(source) or source <= 0:
            raise RecoveryError('UOM_EQUIVALENCE: missing or invalid migrated unit')
        anchors.setdefault(row['category_id'], (source, target))
        source_anchor, target_anchor = anchors[row['category_id']]
        # source quantity conversion A->anchor = factor_anchor/factor_A;
        # target A->anchor = absolute_A/absolute_anchor.
        if not math.isclose(source_anchor / source, target / target_anchor, rel_tol=1e-9, abs_tol=0):
            raise RecoveryError('UOM_EQUIVALENCE: conversion ratio changed; no checkpoint published')
