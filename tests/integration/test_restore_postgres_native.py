"""Real synthetic restores in a new private cluster; never attach to an existing DB.

No DROP operations or customer data. pg_ctl stops only this fixture's own cluster;
its files are retained for inspection. Enable explicitly with --run-native-postgres-tests.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import zipfile

import pytest
from odooupgrader.core import OdooUpgrader
from odooupgrader.jobs import Job, digest
from odooupgrader.postgres_restore import (RestoreFailure, inspect_dump, select_runtime,
                                          client_version, version_number)
from odooupgrader.diagnostics import collect


@pytest.fixture(scope='module')
def native_pg(request):
    if not request.config.getoption('--run-native-postgres-tests'):
        pytest.skip('NOT TESTED: opt in to isolated native PostgreSQL tests')
    binary = Path('/usr/lib/postgresql/16/bin')
    if not (binary / 'initdb').is_file() or os.getuid() == 0:
        pytest.skip('Native PG16 initdb and an unprivileged account are required')
    root = Path(tempfile.mkdtemp(prefix='ou-restore-pg16-', dir='/tmp'))
    root.chmod(0o700)
    data, socket = root / 'data', root / 'socket'
    socket.mkdir(mode=0o700)
    env = {'PATH': str(binary) + ':/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'}
    def run(program, args, **kwargs):
        return subprocess.run([str(binary / program), *args], env=env, check=True,
                              capture_output=True, text=True, timeout=60, **kwargs)
    run('initdb', ['-D', str(data), '--username=odoo', '--auth=trust', '--no-locale', '--encoding=UTF8'])
    started = False
    try:
        run('pg_ctl', ['-D', str(data), '-l', str(root / 'postgres.log'), '-o',
                      f"-c listen_addresses='' -k {socket} -p 55432 -c unix_socket_permissions=0700", '-w', 'start'])
        started = True
        def client(program, args):
            return run(program, ['-h', str(socket), '-p', '55432', '-U', 'odoo', *args])
        yield {'client': client, 'binary': binary, 'run': run, 'root': root}
    finally:
        if started:
            run('pg_ctl', ['-D', str(data), '-m', 'fast', '-w', 'stop'])
        print('Retained stopped disposable cluster:', root)


@pytest.fixture
def native_upgrader(native_pg, tmp_path, monkeypatch):
    monkeypatch.setattr(OdooUpgrader, '_get_docker_compose_cmd', lambda self: ['not-used'])
    u = OdooUpgrader('not-used', '19.0', postgres_version='16', jobs_dir=tmp_path / 'jobs')
    u.job = Job.create(u.jobs_dir, '19.0', '16', {'runtime_plan': u._runtime_plan()})
    u.attempt = u._new_attempt()
    u.prepare_environment()
    u.pg_selected = '16'
    u.pg_runtime = {'server_version_num': int(native_pg['client']('psql', ['-XAt', '-d', 'postgres', '-c', 'SHOW server_version_num']).stdout),
                    'server_version': version_number(native_pg['client']('psql', ['-XAt', '-d', 'postgres', '-c', 'SHOW server_version']).stdout.strip()),
                    'psql_version': client_version(native_pg['run']('psql', ['--version']).stdout),
                    'pg_restore_version': client_version(native_pg['run']('pg_restore', ['--version']).stdout),
                    'image_id': None, 'image_reference': 'native-PG16-test-only'}
    u.attempt['postgres_runtime'] = u.pg_runtime
    u.attempt['containers'] = []
    def command(cmd, **kwargs):
        if cmd[:2] == ['docker', 'cp']:
            u._native_input = cmd[2]
            return None
        assert cmd[:3] == ['docker', 'exec', u.db_container]
        program = cmd[3]
        args = [u._native_input if x == '/tmp/input.dump' else x for x in cmd[4:]]
        return native_pg['client'](program, args)
    u._run_cmd = command
    return u


def header(source='16.4', exporter='16.4'):
    return ('-- PostgreSQL database dump\n'
            f'-- Dumped from database version {source}\n-- Dumped by pg_dump version {exporter}\n')


def test_pg16_reproduces_transaction_timeout_failure_and_atomic_rollback(native_pg, tmp_path):
    database = 'negative_' + uuid.uuid4().hex
    native_pg['client']('createdb', ['-T', 'template0', database])
    path = tmp_path / 'immutable.sql'
    path.write_text(header(exporter='17.2') + 'CREATE TABLE rollback_sentinel (id int);\nSET transaction_timeout = 0;\n')
    before = digest(path)
    assert select_runtime('auto', inspect_dump(path)) == '17'
    with pytest.raises(RestoreFailure):
        select_runtime('16', inspect_dump(path))
    with pytest.raises(subprocess.CalledProcessError) as error:
        native_pg['client']('psql', ['-X', '--single-transaction', '-v', 'ON_ERROR_STOP=1', '-d', database, '-f', str(path)])
    assert 'unrecognized configuration parameter "transaction_timeout"' in error.value.stderr
    result = native_pg['client']('psql', ['-XAt', '-d', database, '-c', "SELECT to_regclass('rollback_sentinel') IS NULL"])
    assert result.stdout.strip() == 't'
    assert digest(path) == before


def test_real_plain_and_custom_pg16_restores_using_pipeline(native_pg, native_upgrader, tmp_path):
    u = native_upgrader
    path = tmp_path / 'input.sql'
    path.write_text(header() + 'CREATE TABLE synthetic_fixture(id int);\nINSERT INTO synthetic_fixture VALUES(7);\n')
    before = digest(path)
    u._restore_dump(path, 'database', sql=True)
    assert u._database_restored
    assert native_pg['client']('psql', ['-XAt', '-d', 'database', '-c', 'SELECT id FROM synthetic_fixture']).stdout.strip() == '7'
    custom = tmp_path / 'actual.dump'
    native_pg['client']('pg_dump', ['-Fc', '-f', str(custom), 'database'])
    metadata = inspect_dump(custom)
    assert metadata['format'] == 'custom'
    assert metadata['dump_client_version'].startswith('16.')
    assert metadata['source_server_version'].startswith('16.')
    u._restore_dump(custom, 'verify_' + uuid.uuid4().hex)
    assert u.attempt['restore']['status'] == 'SUCCEEDED'
    assert digest(path) == before


def test_real_restore_failure_diagnostics_without_secondary_odoo_queries(native_pg, native_upgrader, tmp_path):
    u = native_upgrader
    path = tmp_path / 'bad.sql'
    path.write_text(header() + 'CREATE TABLE rollback_sentinel(id int);\nTHIS IS NOT VALID SQL;\n')
    before = digest(path)
    database = 'bad_' + uuid.uuid4().hex
    with pytest.raises(RestoreFailure) as failure:
        u._restore_dump(path, database, sql=True)
    u._rows = lambda *a, **k: pytest.fail('Odoo diagnostics must not run after failed restore')
    u._sql = lambda *a, **k: pytest.fail('Do not query even version through a failed working DB')
    evidence = u.failure_evidence()
    assert evidence['module_status']['available'] is False
    archive = collect(u.job, failure.value, evidence)
    with zipfile.ZipFile(archive) as bundle:
        assert json.loads(bundle.read('failure_summary.json'))['classification'] == 'DATABASE_RESTORE_FAILURE'
        assert json.loads(bundle.read('restore_diagnostics.json'))['current']['sqlstate'] == '42601'
    assert native_pg['client']('psql', ['-XAt', '-d', database, '-c', "SELECT to_regclass('rollback_sentinel') IS NULL"]).stdout.strip() == 't'
    assert u.job.data['latest_checkpoint'] is None
    assert digest(path) == before
