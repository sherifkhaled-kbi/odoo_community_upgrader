import hashlib
import zipfile
import pytest
from odooupgrader.checks import extract_zip, validate_uom, verify_attachments
from odooupgrader.jobs import RecoveryError


def rows():
    return [{'category': 1, 'type': 'reference', 'factor': 1, 'provider_xmlid': True},
            {'category': 1, 'type': 'bigger', 'factor': 1/24, 'provider_xmlid': False}]


def test_duplicate_references_including_inactive_rows():
    units = rows()
    units.append(dict(units[0], active=False))
    with pytest.raises(RecoveryError, match='UOM_REFERENCE'):
        validate_uom(units, factor_fix=True)


def test_bigger_unit_bug_blocks_before_mutation():
    with pytest.raises(RecoveryError, match='UOM_UPSTREAM_FACTOR'):
        validate_uom(rows())
    validate_uom(rows(), factor_fix=True)
    source_quantity = 24 / 1  # reference -> box uses factor_dest / factor_source
    old_script_relative_factor = 1/24
    corrected_relative_factor = 24
    assert 1 / old_script_relative_factor != 1 / corrected_relative_factor
    source_unit_to_box = rows()[1]['factor'] / rows()[0]['factor']
    assert source_unit_to_box == 1 / corrected_relative_factor


@pytest.mark.parametrize('factor', [0, -1, float('inf'), float('nan'), None])
def test_invalid_uom_factor(factor):
    with pytest.raises(RecoveryError):
        validate_uom([dict(rows()[0], factor=factor)])


def test_missing_reference():
    with pytest.raises(RecoveryError, match='UOM_REFERENCE'):
        validate_uom(rows()[1:])


def test_filestore_hash_and_size(tmp_path):
    (tmp_path / 'aa').mkdir()
    data = b'safe attachment'
    path = tmp_path / 'aa' / 'file'
    path.write_bytes(data)
    entry = {'store_fname': 'aa/file', 'file_size': len(data), 'checksum': hashlib.sha1(data).hexdigest()}
    assert verify_attachments(tmp_path, [entry])['passed']
    path.write_bytes(b'wrong')
    with pytest.raises(RecoveryError, match='FILESTORE_CORRUPT'):
        verify_attachments(tmp_path, [entry])
    path.unlink()
    with pytest.raises(RecoveryError, match='FILESTORE_MISSING'):
        verify_attachments(tmp_path, [entry])


@pytest.mark.parametrize('filename', ['../bad', '/absolute', 'a/../../bad', 'a\\bad', 'C:/bad'])
def test_archive_path_safety(tmp_path, filename):
    source = tmp_path / 'input.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr(filename, 'payload')
    dest = tmp_path / 'dest'
    dest.mkdir()
    with pytest.raises(RecoveryError, match='INPUT_UNSAFE'):
        extract_zip(source, dest)
    assert not list(dest.iterdir())


def test_reviewed_upstream_fix_hash_is_exact():
    from pathlib import Path
    from odooupgrader.checks import FACTOR_FIX_SCRIPT_SHA256
    assert hashlib.sha256((Path(__file__).parent / 'fixtures/uom_pr6054.py').read_bytes()).hexdigest() == FACTOR_FIX_SCRIPT_SHA256
