import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from odooupgrader.core import OdooUpgrader
from odooupgrader.jobs import Job
from odooupgrader.postgres_restore import (RestoreFailure, inspect_dump, select_runtime,
                                          validate_runtime, sql_failure_evidence)
from odooupgrader.diagnostics import collect


def sql_dump(source='16.4', exporter='17.2', timeout=True):
    return ('--\n-- PostgreSQL database dump\n--\n'
            f'-- Dumped from database version {source}\n'
            f'-- Dumped by pg_dump version {exporter}\n\n'
            + ('SET transaction_timeout = 0;\n' if timeout else '')
            + 'CREATE TABLE restore_fixture (id integer);\nINSERT INTO restore_fixture VALUES (7);\n')


def custom_dump(source='16.4', exporter='17.2', archive=(1, 16, 0)):
    def integer(number):
        return bytes([int(number < 0)]) + abs(number).to_bytes(4, 'little')
    def string(value):
        return integer(len(value.encode())) + value.encode()
    compression = b'\x00' if archive >= (1, 15, 0) else integer(0)
    return (b'PGDMP' + bytes(archive) + bytes([4, 8, 1]) + compression
            + b''.join(integer(0) for _ in range(7))
            + string('not-exported-business-name') + string(source) + string(exporter))


def runtime(major):
    return {'server_version_num': major * 10000 + 2, 'server_version': f'{major}.2',
            'psql_version': f'{major}.2', 'pg_restore_version': f'{major}.2',
            'image_id': 'sha256:test', 'image_reference': f'postgres:{major}'}


@pytest.fixture
def upgrader(tmp_path, monkeypatch):
    monkeypatch.setattr(OdooUpgrader, '_get_docker_compose_cmd', lambda self: ['docker', 'compose'])
    u = OdooUpgrader('backup.zip', '19.0', postgres_version='17', jobs_dir=tmp_path / 'jobs')
    u.job = Job.create(u.jobs_dir, '19.0', '17', {'runtime_plan': u._runtime_plan()})
    u.attempt = u._new_attempt()
    u.prepare_environment()
    u.pg_selected = '17'
    u.pg_runtime = runtime(17)
    u._inspect_owned = lambda *a: None
    return u


def test_source_server_16_dump_client_17_requires_17_and_is_not_rewritten(tmp_path):
    path = tmp_path / 'without-extension'
    data = sql_dump()
    path.write_text(data)
    metadata = inspect_dump(path)
    assert metadata['source_server_version'] == '16.4'
    assert metadata['dump_client_version'] == '17.2'
    assert metadata['format'] == 'plain_sql'
    assert select_runtime('auto', metadata) == '17'
    with pytest.raises(RestoreFailure, match='requested 16'):
        select_runtime('16', metadata)
    assert path.read_text() == data


@pytest.mark.parametrize('archive,exporter,selected', [((1, 14, 0), '13.19', '13'), ((1, 15, 0), '16.4', '16'), ((1, 16, 0), '17.2', '17')])
def test_custom_metadata_uses_exporter_version_not_archive_minor(tmp_path, archive, exporter, selected):
    path = tmp_path / 'custom.dump'
    path.write_bytes(custom_dump(source='12.9', exporter=exporter, archive=archive))
    metadata = inspect_dump(path)
    assert metadata['format'] == 'custom'
    assert metadata['archive_version'] == '.'.join(map(str, archive))
    assert select_runtime('auto', metadata) == selected
    assert 'not-exported-business-name' not in json.dumps(metadata)


@pytest.mark.parametrize('payload', [b'PGDMP\x01', custom_dump(archive=(1, 17, 0)), b'not a dump', b'\x1f\x8bcompressed', b'-- PostgreSQL database dump\nSET transaction_timeout = 0;'])
def test_unknown_or_unsupported_dump_rejected(tmp_path, payload):
    path = tmp_path / 'input'
    path.write_bytes(payload)
    with pytest.raises(RestoreFailure):
        select_runtime('auto', inspect_dump(path))


def test_no_transaction_timeout_does_not_make_downgrade_safe(tmp_path):
    path = tmp_path / 'input.sql'
    path.write_text(sql_dump(exporter='17.2', timeout=False))
    with pytest.raises(RestoreFailure, match='requires PostgreSQL 17'):
        select_runtime('16', inspect_dump(path))


def test_newer_source_and_unsupported_runtime_rejected(tmp_path):
    path = tmp_path / 'input'
    path.write_text(sql_dump('18.1', '18.1'))
    with pytest.raises(RestoreFailure, match='supports 13–17'):
        select_runtime('auto', inspect_dump(path))
    path.write_text(sql_dump('16.4', '17.2'))
    with pytest.raises(RestoreFailure):
        select_runtime('18', inspect_dump(path))


def test_actual_server_and_both_clients_checked(tmp_path):
    path = tmp_path / 'input'
    path.write_text(sql_dump())
    metadata = inspect_dump(path)
    validate_runtime(metadata, runtime(17), '17')
    with pytest.raises(RestoreFailure):
        validate_runtime(metadata, runtime(16), '17')
    with pytest.raises(RestoreFailure):
        validate_runtime(metadata, dict(runtime(17), pg_restore_version='16.4'), '17')


def test_incompatible_server_refused_before_create_or_copy(upgrader, tmp_path):
    path = tmp_path / 'input.sql'
    path.write_text(sql_dump())
    upgrader.pg_runtime = runtime(16)
    upgrader._run_cmd = Mock()
    with pytest.raises(RestoreFailure):
        upgrader._restore_dump(path, 'database', sql=True)
    upgrader._run_cmd.assert_not_called()
    assert not upgrader._database_restored
    assert upgrader._restore_failed


def test_successful_restore_records_versions_and_enables_module_diagnostics(upgrader, tmp_path):
    path = tmp_path / 'input.sql'
    path.write_text(sql_dump())
    calls = []
    upgrader._run_cmd = lambda cmd, **kw: calls.append(cmd)
    upgrader._restore_dump(path, 'database', sql=True)
    assert upgrader._database_restored
    assert '--single-transaction' in calls[-1]
    assert '--echo-errors' in calls[-1] and 'ON_ERROR_STOP=1' in calls[-1]
    assert upgrader.attempt['restore']['status'] == 'SUCCEEDED'
    assert upgrader.attempt['restore']['runtime']['server_version'] == '17.2'


def test_failed_restore_bundle_has_primary_error_without_odoo_queries(upgrader, tmp_path):
    path = tmp_path / 'input.sql'
    path.write_text(sql_dump())
    line = next(i for i, value in enumerate(path.read_text().splitlines(), 1) if value.startswith('SET'))
    stderr = f'psql:/tmp/input.dump:{line}: ERROR: 42704: unrecognized configuration parameter "transaction_timeout"\nSTATEMENT: SET transaction_timeout = 0;\n'
    def command(cmd, **kwargs):
        if 'psql' in cmd:
            raise subprocess.CalledProcessError(3, cmd, stderr=stderr)
        return SimpleNamespace(stdout='', returncode=0)
    upgrader._run_cmd = command
    upgrader._sql = Mock(side_effect=AssertionError('must not query Odoo tables'))
    upgrader._rows = Mock(side_effect=AssertionError('must not query Odoo tables'))
    with pytest.raises(RestoreFailure) as failure:
        upgrader._restore_dump(path, 'database', sql=True)
    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)
    evidence = upgrader.failure_evidence()
    upgrader._sql.assert_not_called()
    upgrader._rows.assert_not_called()
    assert evidence['module_status'] == {'available': False, 'reason': 'DATABASE_RESTORE_FAILURE'}
    bundle = collect(upgrader.job, failure.value, evidence)
    with zipfile.ZipFile(bundle) as archive:
        summary = json.loads(archive.read('failure_summary.json'))
        restore = json.loads(archive.read('restore_diagnostics.json'))['current']
        assert summary['classification'] == 'DATABASE_RESTORE_FAILURE'
        assert summary['dump_format'] == 'plain_sql'
        assert summary['process_exit_code'] == 3
        assert restore['failing_statement'] == 'SET transaction_timeout = 0;'
        assert 'transaction_timeout' in restore['relevant_error']
        assert json.loads(archive.read('module_status.json'))['available'] is False
    assert (Path(upgrader.cwd) / 'restore.private.log').is_file()


def test_custom_archive_list_failure_never_creates_db(upgrader, tmp_path):
    path = tmp_path / 'input.dump'
    path.write_bytes(custom_dump())
    calls = []
    def command(cmd, **kwargs):
        calls.append(cmd)
        if '--list' in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr='pg_restore: error: corrupt archive')
    upgrader._run_cmd = command
    with pytest.raises(RestoreFailure):
        upgrader._restore_dump(path, 'database')
    assert not any('createdb' in cmd for cmd in calls)


def test_private_business_sql_and_errors_never_exported(tmp_path):
    path = tmp_path / 'input.sql'
    path.write_text("INSERT INTO partner VALUES ('Acme', 'password=topsecret');\n")
    result = sql_failure_evidence(path, 'psql:/tmp/input.dump:1: ERROR: 23505: Acme already exists\n')
    assert 'Acme' not in json.dumps(result)
    assert 'topsecret' not in json.dumps(result)
    assert result['sqlstate'] == '23505'


def test_exact_server_version_and_image_are_captured_without_odoo_query(upgrader):
    upgrader.pg_runtime = None
    calls = []
    def sql(query, database, timeout):
        calls.append((query, database))
        return '160015' if query.endswith('_num') else '16.15 (Ubuntu)'
    upgrader._sql = sql
    upgrader._run_cmd = lambda cmd, **kwargs: SimpleNamespace(stdout=f'{cmd[3]} (PostgreSQL) 16.15 (Ubuntu)')
    upgrader._inspect_owned = lambda *a: {'Image': 'sha256:actual', 'Config': {'Image': 'postgres:16'}}
    actual = upgrader._ensure_pg_runtime()
    assert actual['server_version'] == '16.15'
    assert actual['server_version_num'] == 160015
    assert actual['image_id'] == 'sha256:actual'
    assert actual['psql_version'] == actual['pg_restore_version'] == '16.15'
    assert all(database == 'postgres' for _, database in calls)


def test_auto_selection_is_persisted_before_docker_and_reused_for_checkpoint(upgrader, tmp_path):
    upgrader.postgres_version = 'auto'
    upgrader.pg_selected = None
    source = tmp_path / 'backup.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('dump.sql', sql_dump())
    original = source.read_bytes()
    upgrader.source = str(source)
    upgrader._prepare_restore_input(None)
    assert upgrader.pg_selected == '17'
    assert upgrader.job.read()['selected_postgres'] == '17'
    assert upgrader.attempt['restore']['input_container'] == 'zip'
    upgrader.create_db_compose_file()
    compose = json.loads((Path(upgrader.cwd) / 'db-composer.yml').read_text())
    assert compose['services']['db-odooupgrade']['image'] == 'postgres:17'
    checkpoint = tmp_path / 'checkpoint'
    checkpoint.mkdir()
    (checkpoint / 'database.dump').write_bytes(custom_dump('17.2', '17.2'))
    upgrader._prepare_restore_input((checkpoint, {}))
    assert upgrader.pg_selected == '17'
    assert source.read_bytes() == original


def test_full_failed_preflight_never_starts_docker_or_queries_odoo(tmp_path, monkeypatch):
    monkeypatch.setattr(OdooUpgrader, '_get_docker_compose_cmd', lambda self: ['not-executed'])
    source = tmp_path / 'odoo17_backup'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('dump.sql', sql_dump())
    original = source.read_bytes()
    u = OdooUpgrader(str(source), '19.0', postgres_version='16', jobs_dir=tmp_path / 'jobs')
    u.assert_local_docker = lambda: None
    u._stop_attempt = lambda *a: None
    u._inspect_owned = lambda *a: None
    u._run_cmd = Mock(side_effect=AssertionError('no Docker resources before dump admission'))
    u._rows = Mock(side_effect=AssertionError('no Odoo queries'))
    u._sql = Mock(side_effect=AssertionError('no database queries'))
    with pytest.raises(RestoreFailure, match='requires PostgreSQL 17'):
        u.run()
    u._run_cmd.assert_not_called()
    u._rows.assert_not_called()
    u._sql.assert_not_called()
    assert u.job.data['state'] == 'FAILED'
    assert u.job.data['latest_checkpoint'] is None
    assert u.job.data['artifact'] is None
    archive = u.job.path / 'diagnostics' / (u.attempt['id'] + '.zip')
    with zipfile.ZipFile(archive) as bundle:
        summary = json.loads(bundle.read('failure_summary.json'))
        assert summary['classification'] == 'DATABASE_RESTORE_FAILURE'
        assert summary['incompatible_statement'] == 'SET transaction_timeout = 0;'
        assert summary['failing_statement'] is None  # Refused before execution.
        assert json.loads(bundle.read('module_status.json'))['available'] is False
    assert source.read_bytes() == original


def test_restore_does_not_assign_an_odoo_module_from_previous_logs(upgrader, tmp_path):
    path = tmp_path / 'input.sql'
    path.write_text(sql_dump())
    (Path(upgrader.cwd) / 'docker.log').write_text('module uom: Running upgrade post-migration\n')
    upgrader.pg_runtime = runtime(16)
    with pytest.raises(RestoreFailure) as error:
        upgrader._restore_dump(path, 'database', sql=True)
    bundle = collect(upgrader.job, error.value, upgrader.failure_evidence())
    with zipfile.ZipFile(bundle) as archive:
        assert json.loads(archive.read('failure_summary.json'))['failing_module'] is None


@pytest.mark.parametrize('collector_error', [OSError('unreadable'), ValueError('parser failed')])
def test_failure_collectors_cannot_replace_original_restore_exception(upgrader, tmp_path, monkeypatch, collector_error):
    path = tmp_path / 'input.sql'
    path.write_text(sql_dump())
    original = subprocess.CalledProcessError(3, ['psql'], stderr='psql:/tmp/input.dump:7: ERROR: failure\n')
    def command(cmd, **kwargs):
        if 'psql' in cmd:
            raise original
    upgrader._run_cmd = command
    monkeypatch.setattr('odooupgrader.recovery.sql_failure_evidence', Mock(side_effect=collector_error))
    with pytest.raises(RestoreFailure) as failure:
        upgrader._restore_dump(path, 'database', sql=True)
    assert failure.value.__cause__ is original


def test_newer_runtime_does_not_hide_unsupported_sql_or_schema(upgrader, tmp_path):
    path = tmp_path / 'input.sql'
    path.write_text(sql_dump('16.4', '16.4', timeout=False))
    def command(cmd, **kwargs):
        if 'psql' in cmd:
            raise subprocess.CalledProcessError(3, cmd, stderr='ERROR: 42601: unsupported SQL syntax\n')
    upgrader._run_cmd = command
    with pytest.raises(RestoreFailure):
        upgrader._restore_dump(path, 'database', sql=True)
    assert upgrader.attempt['restore']['sqlstate'] == '42601'
    assert upgrader.job.data['latest_checkpoint'] is None
    assert upgrader.job.data['artifact'] is None


def test_unsupported_archive_still_records_detected_format(upgrader, tmp_path):
    path = tmp_path / 'future.dump'
    path.write_bytes(custom_dump(archive=(1, 17, 0)))
    with pytest.raises(RestoreFailure):
        upgrader._restore_dump(path, 'database')
    assert upgrader.attempt['restore']['dump'] == {'format': 'custom', 'archive_version': '1.17.0'}
