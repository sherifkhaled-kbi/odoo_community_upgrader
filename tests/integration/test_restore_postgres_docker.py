"""Opt-in real PG17 restore path; no Odoo/customer DB or destructive cleanup."""
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import uuid
import zipfile

import pytest
from odooupgrader.core import OdooUpgrader
from odooupgrader.jobs import Job, digest, atomic_json
from odooupgrader.postgres_restore import inspect_dump


def test_auto_pg17_real_server_clients_plain_and_custom_restore(request):
    if not request.config.getoption('--run-docker-restore-tests'):
        pytest.skip('NOT TESTED: opt in to new isolated Docker restore test')
    root = Path(tempfile.mkdtemp(prefix='ou-restore-docker17-', dir='/tmp'))
    root.chmod(0o700)
    source = root / 'synthetic-backup.zip'
    sql = ('-- PostgreSQL database dump\n-- Dumped from database version 16.15\n'
           '-- Dumped by pg_dump version 17.0\nSET transaction_timeout = 0;\n'
           'CREATE TABLE restore_fixture(id integer);\nINSERT INTO restore_fixture VALUES(7);\n')
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('dump.sql', sql)
    before = digest(source)
    u = OdooUpgrader(str(source), '19.0', postgres_version='auto', jobs_dir=root / 'jobs')
    u.assert_local_docker()
    u.job = Job.create(u.jobs_dir, '19.0', 'auto', {'runtime_plan': u._runtime_plan()})
    try:
        with u.job.lock():
            u.attempt = u._new_attempt()
            u.prepare_environment()
            u._prepare_restore_input(None)
            assert u.pg_selected == '17'
            u.create_db_compose_file()
            u._run_cmd(u.compose_cmd + ['-f', 'db-composer.yml', 'up', '-d'], capture_output=True)
            u.wait_for_db()
            u.restore_database(u.restore_file_type)
            assert u._database_restored
            assert u.pg_runtime['server_version_num'] // 10000 == 17
            assert u.pg_runtime['psql_version'].startswith('17.')
            assert u.pg_runtime['pg_restore_version'].startswith('17.')
            assert u.pg_runtime['image_id'].startswith('sha256:')
            assert u._sql('SELECT id FROM restore_fixture') == '7'
            custom = root / 'actual-pg17.dump'
            with custom.open('wb') as stream:
                subprocess.run(['docker', 'exec', u.db_container, 'pg_dump', '-Fc', '-U', 'odoo', 'database'],
                               stdout=stream, stderr=subprocess.PIPE, check=True, timeout=60)
            metadata = inspect_dump(custom)
            assert metadata['source_server_version'].startswith('17.')
            assert metadata['dump_client_version'].startswith('17.')
            database = 'verify_' + uuid.uuid4().hex
            u._restore_dump(custom, database)
            assert u._sql('SELECT id FROM restore_fixture', database=database) == '7'
            assert digest(source) == before
            atomic_json(root / 'test-result.json', {'job_id': u.job.id, 'runtime': u.pg_runtime,
                        'custom_metadata': metadata, 'source_unchanged': True, 'plain_restore': 'PASS', 'custom_restore': 'PASS'})
    finally:
        if hasattr(u, 'attempt'):
            u._stop_attempt(u.attempt)
        if u._handler:
            logging.getLogger('odooupgrader').removeHandler(u._handler)
            u._handler.close()
        print('Retained stopped Docker restore test:', root)
