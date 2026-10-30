import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from click.testing import CliRunner
from odooupgrader.cli import main
from odooupgrader.core import OdooUpgrader
from odooupgrader.jobs import Job, RecoveryError
from odooupgrader.checks import verify_uom_ratios


@pytest.fixture
def upgrader(tmp_path, monkeypatch):
    monkeypatch.setattr(OdooUpgrader, '_get_docker_compose_cmd', lambda self: ['docker', 'compose'])
    u = OdooUpgrader('source.zip', '19.0', jobs_dir=tmp_path)
    u.job = Job.create(tmp_path, '19.0', '13', {'runtime_plan': u._runtime_plan()})
    u.attempt = u._new_attempt()
    u.prepare_environment()
    return u


def test_restore_is_fatal_and_fresh(upgrader):
    calls = []
    upgrader._run_cmd = lambda command, **kwargs: calls.append((command, kwargs))
    upgrader._restore_dump('/tmp/dump', 'database')
    assert 'createdb' in calls[0][0] and 'template0' in calls[0][0]
    assert '--single-transaction' in calls[-1][0]
    assert '--exit-on-error' in calls[-1][0]
    assert all(kw.get('check', True) for _, kw in calls)
    calls.clear()
    upgrader._restore_dump('/tmp/dump', 'database', sql=True)
    assert 'ON_ERROR_STOP=1' in calls[-1][0]
    assert '--single-transaction' in calls[-1][0]


def test_restore_failure_stops_before_pgrestore(upgrader):
    upgrader._run_cmd = Mock(side_effect=subprocess.CalledProcessError(1, 'createdb'))
    with pytest.raises(subprocess.CalledProcessError):
        upgrader._restore_dump('/tmp/dump', 'database')
    assert upgrader._run_cmd.call_count == 1


def test_raw_sql_diagnostics_force_read_only(upgrader):
    calls = []
    upgrader._run_cmd = lambda command, **kwargs: (calls.append(command) or SimpleNamespace(stdout='13'))
    assert upgrader._sql('SHOW server_version') == '13'
    assert 'PGOPTIONS=-c default_transaction_read_only=on' in calls[0]
    assert 'ON_ERROR_STOP=1' in calls[0]


def test_database_compose_is_unique_nonrestarting_and_isolated(upgrader):
    upgrader.create_db_compose_file()
    compose = json.loads((Path(upgrader.cwd) / 'db-composer.yml').read_text())
    db = compose['services']['db-odooupgrade']
    assert db['container_name'] == upgrader.db_container
    assert db['restart'] == 'no'
    assert not db.get('ports')
    assert compose['networks']['migration']['internal']
    assert db['labels']['odooupgrader.job'] == upgrader.job.id


def test_source_under_old_output_preserved(upgrader, tmp_path):
    original = tmp_path / 'output' / 'backup.dump'
    original.parent.mkdir()
    original.write_bytes(b'original')
    upgrader.source = str(original)
    copied = Path(upgrader.download_or_copy_source())
    assert copied.read_bytes() == original.read_bytes() == b'original'
    assert copied != original
    assert copied.stat().st_mode & 0o777 == 0o400


def test_registry_probe_has_no_upgrade_and_is_owned(upgrader):
    commands = []
    upgrader._run_cmd = lambda command, **kwargs: (commands.append(command) or SimpleNamespace(stdout='Modules loaded.', stderr=''))
    upgrader._probe('verify_123', upgrader.filestore_dir, 'sha256:fake')
    command = commands[0]
    assert '--load=base,web' in command
    assert '--no-http' in command
    assert '--max-cron-threads=0' in command
    assert '--update' not in ' '.join(command)
    assert 'openupgrade_framework' not in ' '.join(command)
    assert 'odooupgrader.job=' + upgrader.job.id in command


@pytest.mark.parametrize('log', ['ERROR failed', 'Some modules are not loaded', 'inconsistent states', 'Running upgrade'])
def test_registry_exit_zero_not_sufficient(upgrader, log):
    upgrader._run_cmd = lambda *a, **k: SimpleNamespace(stdout=log, stderr='')
    with pytest.raises(RecoveryError, match='REGISTRY_VALIDATION'):
        upgrader._probe('verify_123', upgrader.filestore_dir, 'fake')


def test_partial_db_never_initializes_registry(upgrader):
    upgrader._rows = lambda select, database='database': [{'name': 'base', 'state': 'installed', 'latest_version': '19.0.1.0'}]
    upgrader._sql = lambda query, database='database': '1'
    with pytest.raises(RecoveryError, match='PARTIAL_DATABASE'):
        upgrader._technical('19.0', upgrader.filestore_dir)


def test_migration_build_dependency_error_prevents_container_execution(upgrader):
    upgrader._uom_preflight = lambda target: None
    calls = []
    def command(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ['docker', 'build']:
            raise subprocess.CalledProcessError(1, cmd, stderr='ModuleNotFoundError: missing dependency')
        raise AssertionError(cmd)
    upgrader._run_cmd = command
    with pytest.raises(subprocess.CalledProcessError):
        upgrader.run_upgrade_step('18.0')
    assert len(calls) == 1
    recipe = (Path(upgrader.cwd) / 'build' / '18.0' / 'Dockerfile').read_text()
    assert '565d0b33391271a4b2ac27eff8ae19f72fe27d77' in recipe
    assert 'git checkout --detach' in recipe
    assert '--branch' not in recipe
    assert not list((Path(upgrader.cwd) / 'build').rglob('*.dump'))


def test_foreign_container_never_stopped(upgrader, monkeypatch):
    monkeypatch.setattr('subprocess.run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps([{'Config': {'Labels': {}}, 'State': {'Running': True}}])))
    with pytest.raises(RecoveryError, match='RESOURCE_OWNER'):
        upgrader._stop_attempt({'id': upgrader.attempt['id'], 'containers': ['unrelated']})


def test_remote_context_rejected(upgrader, monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'ssh://remote')
    with pytest.raises(RecoveryError, match='LOCAL_DOCKER_REQUIRED'):
        upgrader.assert_local_docker()


def test_observed_interruption_is_read_only(upgrader):
    before = (upgrader.job.path / 'job.json').read_bytes()
    assert upgrader.job.observed_state() == 'INTERRUPTED'
    with upgrader.job.lock():
        assert upgrader.job.observed_state() == 'RUNNING'
    assert before == (upgrader.job.path / 'job.json').read_bytes()


def test_legacy_cli_and_new_queries(tmp_path, monkeypatch):
    seen = []
    class Fake:
        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))
        def run(self):
            seen.append('run')
    monkeypatch.setattr('odooupgrader.cli.OdooUpgrader', Fake)
    result = CliRunner().invoke(main, ['--source', 'backup.zip', '--version', '19.0', '--postgres-version', '16', '--extra-addons', 'addons', '--verbose'])
    assert result.exit_code == 0, result.output
    assert seen[0][0] == ('backup.zip', '19.0', 'addons', True, '16')
    assert seen[-1] == 'run'
    assert CliRunner().invoke(main, ['--jobs-dir', str(tmp_path), 'jobs']).exit_code == 0
    assert CliRunner().invoke(main, ['--source', 'backup.zip']).exit_code == 2


def test_cli_unexpected_exception_is_failure_and_redacted(monkeypatch):
    def fail(self):
        raise ValueError('password=secret customer Acme')
    monkeypatch.setattr(OdooUpgrader, 'run', fail)
    result = CliRunner().invoke(main, ['--source', 'backup.zip', '--version', '19.0'])
    assert result.exit_code == 1
    assert 'secret' not in result.output and 'Acme' not in result.output


def test_uom_equivalence_accepts_changed_root_but_rejects_inversion():
    before = [{'id': 1, 'category_id': 7, 'factor': 1}, {'id': 2, 'category_id': 7, 'factor': 1/24}]
    verify_uom_ratios(before, [{'id': 1, 'factor': 1000}, {'id': 2, 'factor': 24000}])
    with pytest.raises(RecoveryError, match='UOM_EQUIVALENCE'):
        verify_uom_ratios(before, [{'id': 1, 'factor': 1}, {'id': 2, 'factor': 1/24}])


@pytest.mark.parametrize('target,helper,expected_failure', [('17.0', False, False), ('18.0', True, False), ('18.0', False, True)])
def test_actual_image_capability_probe_guards_18_without_breaking_17(upgrader, monkeypatch, target, helper, expected_failure):
    import contextlib
    import io
    import importlib.metadata
    import sys
    import types
    from odooupgrader.checks import LIBRARY_REVISION
    module = types.ModuleType('openupgradelib')
    module.openupgrade = SimpleNamespace(**({'add_inherits_parent_xmlids': lambda *a: None} if helper else {}))
    monkeypatch.setitem(sys.modules, 'openupgradelib', module)
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda: [])
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: SimpleNamespace(read_text=lambda f: json.dumps({'vcs_info': {'commit_id': LIBRARY_REVISION}})))
    def command(cmd, **kwargs):
        if '-c' in cmd:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(cmd[-1], {})
            return SimpleNamespace(stdout=output.getvalue())
        if cmd[:3] == ['docker', 'image', 'inspect']:
            return SimpleNamespace(stdout='sha256:test')
        return SimpleNamespace(stdout='', stderr='')
    upgrader._run_cmd = command
    upgrader._sql = lambda *a, **k: '13.fixture'
    upgrader._inspect_owned = lambda *a: {'State': {'ExitCode': 0, 'Running': False}}
    recorded_plan = upgrader._runtime_plan()
    upgrader._runtime_plan = lambda: recorded_plan
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: SimpleNamespace(wait=lambda: 0))
    if expected_failure:
        with pytest.raises(AttributeError, match='add_inherits_parent_xmlids'):
            upgrader.run_upgrade_step(target)
    else:
        assert upgrader.run_upgrade_step(target)


def test_built_dependency_drift_requires_explicit_manifest_acceptance(upgrader):
    first = {'image_id': 'sha256:old', 'environment': {'dependencies': [['package', '1']]}}
    changed = {'image_id': 'sha256:new', 'environment': {'dependencies': [['package', '2']]}}
    upgrader._runtime_guard('19.0', first)
    with pytest.raises(RecoveryError, match='DEPENDENCY_DRIFT'):
        upgrader._runtime_guard('19.0', changed)
    assert upgrader.job.data['runtime_locks']['19.0'] == first
    candidate = upgrader.job.data['runtime_candidates']['19.0']
    upgrader.acceptance = candidate['accept_sha256']
    upgrader.reason = 'Reviewed exact image/dependency diff and regression evidence'
    upgrader._runtime_guard('19.0', changed)
    assert upgrader.job.data['runtime_locks']['19.0'] == changed
    assert '19.0' not in upgrader.job.data['runtime_candidates']
    assert len(upgrader.job.data['remediations']) == 1


def test_odoo_data_root_is_host_owned_and_probe_copy_is_separate(upgrader):
    import os
    root = Path(upgrader.data_dir)
    assert root.stat().st_uid == os.getuid()
    assert Path(upgrader.filestore_dir) == root / 'filestore' / 'database'
    assert root != Path(upgrader.output_dir)
    commands = []
    upgrader._run_cmd = lambda command, **kwargs: (commands.append(command) or SimpleNamespace(stdout='Modules loaded.', stderr=''))
    verification_store = Path(upgrader.cwd) / 'verify_unique' / 'filestore' / 'verify_db'
    verification_store.mkdir(parents=True)
    upgrader._probe('verify_db', verification_store, 'sha256:test')
    assert str(verification_store.parents[1]) + ':/tmp/ou' in commands[0]
    assert str(root) + ':/tmp/ou' not in commands[0]
