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
from odooupgrader.checks import LIBRARY_REVISION, verify_uom_ratios


@pytest.fixture
def upgrader(tmp_path, monkeypatch):
    monkeypatch.setattr(OdooUpgrader, '_get_docker_compose_cmd', lambda self: ['docker', 'compose'])
    u = OdooUpgrader('source.zip', '19.0', jobs_dir=tmp_path)
    u.job = Job.create(tmp_path, '19.0', '13', {'runtime_plan': u._runtime_plan()})
    u.attempt = u._new_attempt()
    u.prepare_environment()
    u._restore_capacity = lambda *args: None  # Capacity is outside these compatibility/CLI adapter cases.
    return u


def test_restore_is_fatal_and_fresh(upgrader, tmp_path):
    from test_postgres_restore import custom_dump, sql_dump, runtime
    path = tmp_path / 'input.dump'
    path.write_bytes(custom_dump('13.19', '13.19', (1, 14, 0)))
    upgrader.pg_runtime = runtime(13)
    calls = []
    upgrader._run_cmd = lambda command, **kwargs: calls.append((command, kwargs))
    upgrader._restore_dump(path, 'database')
    create = next(command for command, _ in calls if 'createdb' in command)
    assert 'template0' in create
    assert '--list' in calls[1][0]
    assert '--single-transaction' in calls[-1][0]
    assert '--exit-on-error' in calls[-1][0]
    assert all(kw.get('check', True) for _, kw in calls)
    calls.clear()
    path.write_text(sql_dump('13.19', '13.19', timeout=False))
    upgrader._restore_dump(path, 'database', sql=True)
    assert 'ON_ERROR_STOP=1' in calls[-1][0]
    assert '--single-transaction' in calls[-1][0]


def test_restore_failure_stops_before_pgrestore(upgrader, tmp_path):
    from odooupgrader.postgres_restore import RestoreFailure
    from test_postgres_restore import custom_dump, runtime
    path = tmp_path / 'input.dump'
    path.write_bytes(custom_dump('13.19', '13.19', (1, 14, 0)))
    upgrader.pg_runtime = runtime(13)
    calls = []
    def command(cmd, **kwargs):
        calls.append(cmd)
        if 'createdb' in cmd:
            raise subprocess.CalledProcessError(1, cmd)
    upgrader._run_cmd = command
    with pytest.raises(RestoreFailure) as error:
        upgrader._restore_dump(path, 'database')
    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)
    assert len(calls) == 3
    assert not any('pg_restore' in cmd and '--list' not in cmd for cmd in calls)


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


def test_unaccounted_missing_modules_follows_rename_chain_and_detects_cycles(upgrader):
    mapping = {'a': 'b', 'b': 'c'}
    assert upgrader._unaccounted_missing_modules({'a'}, {'c'}, mapping) == set()
    assert upgrader._unaccounted_missing_modules({'a'}, {'b'}, mapping) == {'a'}
    cyclic = {'x': 'y', 'y': 'x'}
    assert upgrader._unaccounted_missing_modules({'x'}, set(), cyclic) == {'x'}


def test_module_merge_mapping_resolves_known_upstream_disappearance(upgrader):
    """Real Odoo 17->18 evidence: OCA's apriori.py merges account_payment_term into
    account; a hard MODULE_MISSING block here would fail every real-world Accounting
    migration. The mapping must come from OpenUpgrade's own registry, never a guess."""
    upgrader.module_baseline = ['base', 'account', 'account_payment_term']
    upgrader._rows = lambda query, database='database': (
        [{'name': 'base', 'state': 'installed', 'latest_version': '18.0.1.3'},
         {'name': 'account', 'state': 'installed', 'latest_version': '18.0.1.3'}]
        if 'ir_module_module' in query else [])
    upgrader._sql = lambda query, database='database': '0'
    with pytest.raises(RecoveryError, match='MODULE_MISSING') as error:
        upgrader._technical('18.0', upgrader.filestore_dir)
    assert 'account_payment_term' in str(error.value)
    result = upgrader._technical('18.0', upgrader.filestore_dir, module_mapping={'account_payment_term': 'account'})
    assert result['passed'] is True


def test_module_mapping_does_not_waive_an_unmapped_or_dangling_disappearance(upgrader):
    upgrader.module_baseline = ['base', 'sale_commission']
    upgrader._rows = lambda query, database='database': (
        [{'name': 'base', 'state': 'installed', 'latest_version': '18.0.1.3'}]
        if 'ir_module_module' in query else [])
    upgrader._sql = lambda query, database='database': '0'
    with pytest.raises(RecoveryError, match='MODULE_MISSING'):
        upgrader._technical('18.0', upgrader.filestore_dir)
    # A mapping exists, but its merge target was itself never installed: still fatal.
    with pytest.raises(RecoveryError, match='sale_commission'):
        upgrader._technical('18.0', upgrader.filestore_dir, module_mapping={'sale_commission': 'sale_commission_oca'})


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
    assert 'safe.directory=/mnt/extra-addons' in recipe
    assert 'checkout --detach FETCH_HEAD' in recipe
    assert '--branch' not in recipe
    assert not list((Path(upgrader.cwd) / 'build').rglob('*.dump'))


def _stub_build_and_probe(calls=None):
    """Fake docker build/inspect/env-probe plumbing shared by dependency-conflict tests."""
    def command(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if cmd[:2] == ['docker', 'build']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if cmd[:3] == ['docker', 'image', 'inspect']:
            return SimpleNamespace(returncode=0, stdout='sha256:builtimage\n', stderr='')
        if '-c' in cmd:
            env = {'python': '3.12.1', 'dependencies': [], 'incomplete_distribution_count': 0,
                   'library': {'commit_id': LIBRARY_REVISION}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(env), stderr='')
        raise AssertionError(cmd)
    return command


def test_capability_probe_imports_odoo_base_before_openupgradelib(upgrader):
    """Real evidence: odoo:19.0 packages `odoo` as a namespace package, so a bare
    `import odoo` never populates odoo.fields/api, and openupgradelib's top-level
    code reads them immediately on import, raising AttributeError. Real Odoo startup
    always imports the `base` addon (every module depends on it) before loading
    openupgrade_framework, which populates these attributes as a side effect; the
    probe must mirror that order or it fails on 19.0 independent of any real
    migration defect."""
    upgrader._uom_preflight = lambda target: None
    probes = []
    base_stub = _stub_build_and_probe()
    def command(cmd, **kwargs):
        if '-c' in cmd:
            probes.append(cmd[-1])
        if cmd[-3:] == ['-m', 'pip', 'check']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return base_stub(cmd, **kwargs)
    upgrader._run_cmd = command
    upgrader._prepare_migration_image('18.0')
    assert probes
    assert probes[0].index('import odoo.addons.base') < probes[0].index('from openupgradelib import openupgrade')


def test_preexisting_upstream_dependency_conflict_does_not_block_build(upgrader):
    """odoo:18.0/19.0 ship with a pre-existing maxminddb/setuptools pip-check conflict
    unrelated to OpenUpgrade; it must not block every 17->18 or 18->19 migration."""
    upgrader._uom_preflight = lambda target: None
    base = _stub_build_and_probe()
    def command(cmd, **kwargs):
        if cmd[-3:] == ['-m', 'pip', 'check']:
            # Same conflict reported for both the pristine base image and the built one.
            return SimpleNamespace(returncode=1,
                stdout='maxminddb 2.5.2 has requirement setuptools>=68.2.2, but you have setuptools 68.1.2.\n',
                stderr="WARNING: Skipping charset_normalizer-3.3.2.dist-info due to invalid metadata entry 'name'\n")
        return base(cmd, **kwargs)
    upgrader._run_cmd = command
    prepared = upgrader._prepare_migration_image('18.0')
    assert prepared['image_id'] == 'sha256:builtimage'
    assert prepared['preexisting_dependency_conflicts'] == [
        'maxminddb 2.5.2 has requirement setuptools>=68.2.2, but you have setuptools 68.1.2.']


def test_new_dependency_conflict_introduced_by_build_is_fatal(upgrader):
    """A conflict absent from the untouched base image but present after our own
    OpenUpgrade/custom-addon installs must still block migration."""
    upgrader._uom_preflight = lambda target: None
    base = _stub_build_and_probe()
    def command(cmd, **kwargs):
        if cmd[-3:] == ['-m', 'pip', 'check']:
            image_ref = cmd[-4]
            if image_ref == 'odoo:18.0':
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            return SimpleNamespace(returncode=1, stdout=
                'openupgrade-custom 1.0 has requirement six>=2.0, but you have six 1.16.0.\n', stderr='')
        return base(cmd, **kwargs)
    upgrader._run_cmd = command
    with pytest.raises(RecoveryError, match='DEPENDENCY_ERROR'):
        upgrader._prepare_migration_image('18.0')


def test_dependency_probe_infra_failure_is_distinct_from_a_real_conflict(upgrader):
    upgrader._uom_preflight = lambda target: None
    base = _stub_build_and_probe()
    def command(cmd, **kwargs):
        if cmd[-3:] == ['-m', 'pip', 'check']:
            return SimpleNamespace(returncode=125, stdout='', stderr='docker: Error response from daemon: No such image')
        return base(cmd, **kwargs)
    upgrader._run_cmd = command
    with pytest.raises(RecoveryError, match='DEPENDENCY_PROBE_FAILED'):
        upgrader._prepare_migration_image('18.0')


def test_reuse_workspace_ignores_stopped_history_but_blocks_a_running_container(upgrader):
    """Real evidence: a single-copy attempt that already completed one transition
    always has a stopped (never auto-removed) migration/verify container recorded in
    attempt['containers']. Treating its mere existence as disqualifying makes
    pre-migration workspace reuse permanently impossible after the first transition,
    defeating the documented --single-copy resume feature. Only a currently RUNNING
    unexpected container is real evidence of an active writer."""
    upgrader.attempt['containers'] = [upgrader.db_container, 'ou-migrate-stopped', 'ou-verify-stopped']
    upgrader.attempt['workspace_ready'] = {'version': '18.0', 'db_container_id': 'dbid',
        'filestore_sha256': 'fshash', 'modules': ['base'], 'runtime': {'image_id': 'sha256:x'}}
    upgrader._run_cmd = lambda cmd, **kw: SimpleNamespace(stdout=json.dumps([{'Containers': {}}]))
    upgrader._filestore_fingerprint = lambda: 'fshash'
    upgrader._technical = lambda version, filestore: {'modules': ['base']}
    upgrader.wait_for_db = lambda: None
    def inspect_stopped(name, attempt_id):
        if name == upgrader.db_container:
            return {'Id': 'dbid', 'State': {'Running': True}}
        return {'Id': 'other', 'State': {'Running': False}}
    upgrader._inspect_owned = inspect_stopped
    assert upgrader._reuse_workspace() == '18.0'

    def inspect_running(name, attempt_id):
        if name == upgrader.db_container:
            return {'Id': 'dbid', 'State': {'Running': True}}
        return {'Id': 'other', 'State': {'Running': True}}
    upgrader._inspect_owned = inspect_running
    with pytest.raises(RecoveryError, match='WORKSPACE_INVALID'):
        upgrader._reuse_workspace()


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


def test_allow_missing_attachments_cli_is_explicit_and_new_job_only(monkeypatch):
    seen = []
    class Fake:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)
        def run(self):
            pass
    monkeypatch.setattr('odooupgrader.cli.OdooUpgrader', Fake)
    result = CliRunner().invoke(main, ['--source', 'backup.zip', '--version', '19.0',
                                       '--allow-missing-attachments'])
    assert result.exit_code == 0, result.output
    assert seen[0]['allow_missing_attachments'] is True
    assert CliRunner().invoke(main, ['--allow-missing-attachments', 'jobs']).exit_code == 2


def test_technical_validation_records_allowed_missing_files(upgrader):
    upgrader._rows = lambda query, database='database': (
        [{'name': 'base', 'state': 'installed', 'latest_version': '19.0.1.0'}]
        if 'ir_module_module' in query else
        [{'store_fname': 'aa/missing', 'checksum': None, 'file_size': 1}])
    upgrader._sql = lambda query, database='database': '0'
    with pytest.raises(RecoveryError, match='FILESTORE_MISSING'):
        upgrader._technical('19.0', upgrader.filestore_dir)
    upgrader.allow_missing_attachments = True
    result = upgrader._technical('19.0', upgrader.filestore_dir)
    assert result['passed'] is True
    assert result['attachments']['missing_files'] == 1
    assert result['attachments']['passed'] is False


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
    # Real Odoo 19 images need `odoo.addons.base` imported before openupgradelib to
    # populate odoo.fields/api (see _prepare_migration_image); fake the module chain
    # so the real probe string's leading `import odoo.addons.base` succeeds here too.
    monkeypatch.setitem(sys.modules, 'odoo', types.ModuleType('odoo'))
    monkeypatch.setitem(sys.modules, 'odoo.addons', types.ModuleType('odoo.addons'))
    monkeypatch.setitem(sys.modules, 'odoo.addons.base', types.ModuleType('odoo.addons.base'))
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
        if cmd[-3:] == ['-m', 'pip', 'check']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
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
