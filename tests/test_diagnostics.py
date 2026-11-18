import json
import subprocess
import zipfile
import pytest
from odooupgrader.diagnostics import collect, redact, structural_log
from odooupgrader.jobs import Job


@pytest.mark.parametrize('exc', [ValueError('customer Acme 123 secret'),
    RuntimeError('OpenUpgrade post-migration failed'),
    subprocess.CalledProcessError(1, ['psql', 'password=hidden']),
    FileNotFoundError('docker'), ImportError('private_customer_module'),
    Exception('unknown business record'), ValueError('ACME_CUSTOMER')])
def test_bundle_archive_no_customer_payloads_or_secrets(tmp_path, exc):
    j = Job.create(tmp_path, '19.0', '13', {})
    a = j.begin()
    work = j.path / 'attempts' / a['id']
    work.mkdir()
    a['work'] = str(work)
    (work / 'migration.log').write_text('password=topsecret\ncustomer Acme invoice 123\nValueError: Customer Acme\n')
    (work / 'docker.log').write_text('postgresql://u:password@host/db\nmodule uom: Running upgrade [19.0] post-migration\n')
    j.save()
    try:
        raise exc
    except Exception as original:
        path = collect(j, original)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        content = '\n'.join(archive.read(name).decode() for name in archive.namelist())
        assert 'Acme' not in content and 'ACME_CUSTOMER' not in content
        assert 'topsecret' not in content
        assert 'postgresql://' not in content
        assert 'hidden' not in content
        assert 'uom' in content
        assert {'failure_summary.json', 'error_traceback.txt', 'job_metadata.json', 'module_status.json', 'README.md'} <= set(archive.namelist())
        assert type(exc).__name__ in archive.read('error_traceback.txt').decode()
    assert path.stat().st_mode & 0o777 == 0o600


def test_redaction_and_sql_payload_filtering():
    result = redact('password=123 token=abc Bearer xyz https://user:pass@host/a?q=secret')
    assert all(value not in result for value in ['123', 'abc', 'xyz', 'user:pass', 'q=secret'])
    assert 'customer' not in structural_log('ERROR SQL INSERT INTO customer VALUES (123)')


@pytest.mark.parametrize('text', ['--db_password hidden', 'POSTGRES_PASSWORD=hidden', '"password": "hidden value"', "api_key='hidden value'", 'Authorization: Bearer hidden'])
def test_redact_cli_json_and_quoted_credentials(text):
    assert 'hidden' not in redact(text)
