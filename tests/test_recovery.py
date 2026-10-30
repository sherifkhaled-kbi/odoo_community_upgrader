"""Exercise the real synchronous recovery loop with a deterministic fake DB adapter.

These are orchestration/fault-injection tests, not Docker/Odoo integration tests.
"""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import zipfile
import pytest
from odooupgrader.core import OdooUpgrader
from odooupgrader.jobs import Job, RecoveryError


class FixtureUpgrader(OdooUpgrader):
    calls = []
    fail_at = None
    fail_validation = None
    restore_error = False
    collected = []

    def _get_docker_compose_cmd(self):
        return ['fixture-compose']

    def assert_local_docker(self):
        pass

    def _run_cmd(self, *args, **kwargs):
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def _stop_attempt(self, attempt):
        pass

    def wait_for_db(self):
        pass

    def _normal_image(self, major):
        return 'fixture-image-' + major

    def _sql(self, query, database='database'):
        if query == 'SHOW server_version':
            return '13.fixture'
        raise AssertionError(query)

    def _prepare_restore_input(self, snapshot):
        if snapshot is None:
            source = self.download_or_copy_source()
            from odooupgrader.jobs import digest
            self.job.data['input_sha256'] = digest(source)
            self.restore_file_type = self.process_source_file(source)
        self.pg_selected = self.postgres_version

    def restore_database(self, file_type):
        self.db = json.loads((Path(self.source_dir) / 'dump.sql').read_text())
        shutil.copytree(Path(self.source_dir) / 'filestore', self.filestore_dir, dirs_exist_ok=True)

    def _restore_dump(self, path, database, sql=False):
        if self.restore_error:
            raise RecoveryError('RESTORE_FAILED: fixture')
        self.db = json.loads(Path(path).read_text())
        self.collected.append(dict(self.db))

    def get_current_version(self):
        return self.db['version']

    def _technical(self, expected, filestore, database='database'):
        assert self.db['version'].split('.')[0] == expected.split('.')[0]
        return {'passed': True, 'modules': ['base'], 'version': expected}

    def _probe(self, *args):
        assert not self.db.get('partial')

    def _verify_copy(self, *args):
        assert not self.db.get('partial')

    def _capacity(self):
        pass

    def _checkpoint(self, major, runtime):
        self.job.stage('CHECKPOINTING')
        if self.fail_validation == major:
            raise RecoveryError('VALIDATION_FAILED: fixture')
        def builder(stage):
            (stage / 'database.dump').write_text(json.dumps(self.db))
            shutil.copytree(self.filestore_dir, stage / 'filestore')
            return dict(self._technical(major, self.filestore_dir), restored=True, registry=True)
        return self.job.publish(major, runtime, builder)

    def run_upgrade_step(self, target_version):
        self.calls.append(target_version)
        self.db['version'] = target_version
        if self.fail_at == target_version:
            self.db['partial'] = True  # Simulate a committed module before crash.
            raise RecoveryError('MIGRATION_EXIT: fixture module crashed after commit')
        self.db.pop('partial', None)
        self.current_runtime = {'image_id': 'fixture-image-' + target_version}
        self.attempt['runtime'][target_version] = self.current_runtime
        return True

    def finalize_package(self):
        self.job.checkpoint()
        path = Path(self.output_dir) / 'upgraded.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('dump.sql', json.dumps(self.db))
        self.job.data['artifact'] = {'path': str(path)}
        return path

    def failure_evidence(self):
        return {}


@pytest.fixture
def source(tmp_path):
    FixtureUpgrader.calls = []
    FixtureUpgrader.collected = []
    FixtureUpgrader.fail_at = None
    FixtureUpgrader.fail_validation = None
    FixtureUpgrader.restore_error = False
    path = tmp_path / 'original.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('dump.sql', json.dumps({'version': '16.0'}))
        archive.writestr('filestore/aa/file', 'original attachment')
    return path


@pytest.mark.parametrize(('failure', 'checkpoint', 'calls'), [('18.0', '17.0', ['17.0', '18.0', '18.0', '19.0']), ('19.0', '18.0', ['17.0', '18.0', '19.0', '19.0'])])
def test_resume_only_failed_transition_and_fresh_copy(tmp_path, source, failure, checkpoint, calls):
    before = source.read_bytes()
    first = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    first.fail_at = failure
    with pytest.raises(RecoveryError):
        first.run()
    assert first.job.checkpoint()[1]['version'] == checkpoint
    checkpoint_bytes = (first.job.checkpoint()[0] / 'database.dump').read_bytes()
    previous_attempt = first.attempt['id']
    assert not first.job.data['artifact']
    assert list((first.job.path / 'diagnostics').glob('*.zip'))
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id)
    second.run()
    assert second.attempt['id'] != previous_attempt
    assert second.calls == calls
    assert second.collected[-1] == {'version': checkpoint}
    assert source.read_bytes() == before
    earlier = next(c for c in second.job.data['completed'] if c['version'] == checkpoint)
    assert (second.job.path / 'checkpoints' / earlier['id'] / 'database.dump').read_bytes() == checkpoint_bytes
    assert second.job.data['state'] == 'COMPLETED'


def failed_job(tmp_path, source):
    first = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    first.fail_at = '19.0'
    with pytest.raises(RecoveryError):
        first.run()
    return first


def test_changed_pending_runtime_requires_exact_acceptance(tmp_path, source):
    first = failed_job(tmp_path, source)
    old = first.job.data['completed'][:]
    change = {'19.0': 'a' * 40}
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id, revisions=change)
    plan = second.dry_run()
    with pytest.raises(RecoveryError, match='RUNTIME_CHANGED'):
        second.run()
    third = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id, revisions=change,
                            acceptance=plan['change_sha256'], reason='Reviewed pending upstream correction')
    third.run()
    assert third.job.data['completed'][:len(old)] == old
    assert len(third.job.data['remediations']) == 1
    assert FixtureUpgrader.calls == ['17.0', '18.0', '19.0', '19.0']


def test_completed_runtime_change_blocked(tmp_path, source):
    first = failed_job(tmp_path, source)
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id, revisions={'17.0': 'a'*40})
    with pytest.raises(RecoveryError, match='COMPLETED_RUNTIME'):
        second.run()


def test_dry_run_does_not_mutate_files(tmp_path, source):
    first = failed_job(tmp_path, source)
    before = {str(p): p.read_bytes() for p in first.job.path.rglob('*') if p.is_file()}
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id)
    assert second.dry_run()['version'] == '18.0'
    assert before == {str(p): p.read_bytes() for p in first.job.path.rglob('*') if p.is_file()}


def test_failed_restore_never_migrates(tmp_path, source):
    first = failed_job(tmp_path, source)
    before = FixtureUpgrader.calls[:]
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id)
    second.restore_error = True
    with pytest.raises(RecoveryError, match='RESTORE_FAILED'):
        second.run()
    assert FixtureUpgrader.calls == before
    assert not second.job.data['artifact']


def test_failed_validation_never_publishes_or_finalizes(tmp_path, source):
    first = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    first.fail_validation = '18.0'
    with pytest.raises(RecoveryError, match='VALIDATION_FAILED'):
        first.run()
    assert first.job.checkpoint()[1]['version'] == '17.0'
    assert not first.job.data['artifact']
    assert not list(first.job.path.rglob('upgraded.zip'))


def test_interrupted_process_recorded_and_resumed(tmp_path, source):
    first = failed_job(tmp_path, source)
    first.job.data['state'] = 'RUNNING'
    first.job.data['attempts'][-1]['outcome'] = 'RUNNING'
    first.job.save()
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id)
    second.run()
    assert second.job.data['attempts'][-2]['outcome'] == 'INTERRUPTED'


def test_diagnostics_failure_keeps_original_exception(tmp_path, source, monkeypatch):
    def fail(*args):
        raise ValueError('collector broke')
    monkeypatch.setattr('odooupgrader.recovery.collect', fail)
    first = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    first.fail_at = '18.0'
    with pytest.raises(RecoveryError, match='MIGRATION_EXIT'):
        first.run()
    assert first.job.data['state'] == 'FAILED'


def test_final_checkpoint_resume_runs_packaging_only(tmp_path, source, monkeypatch):
    first = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    def failure():
        raise RecoveryError('ARTIFACT_INVALID: packaging fixture failed')
    monkeypatch.setattr(first, 'finalize_package', failure)
    with pytest.raises(RecoveryError, match='ARTIFACT_INVALID'):
        first.run()
    assert first.job.checkpoint()[1]['version'] == '19.0'
    calls = first.calls[:]
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id)
    second.run()
    assert second.calls == calls
    assert second.collected[-1] == {'version': '19.0'}


def test_missing_checkpoint_failure_is_diagnosed_without_migration(tmp_path, source):
    first = failed_job(tmp_path, source)
    shutil.rmtree(first.job.checkpoint()[0])
    before = first.calls[:]
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id)
    with pytest.raises(RecoveryError, match='CHECKPOINT_INVALID'):
        second.run()
    assert second.calls == before
    assert (second.job.path / 'diagnostics' / (second.attempt['id'] + '.zip')).is_file()


def test_storage_gate_failure_preserves_source_and_checkpoint(tmp_path, source, monkeypatch):
    first = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    original = source.read_bytes()
    def full():
        raise RecoveryError('DISK_SPACE: fixture disk full')
    monkeypatch.setattr(first, '_capacity', full)
    with pytest.raises(RecoveryError, match='DISK_SPACE'):
        first.run()
    assert first.job.checkpoint()[1]['version'] == '16.0'
    assert not first.calls
    assert source.read_bytes() == original
