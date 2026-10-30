"""Opt-in Linux-server qualification. Never enabled by the default test command."""
import hashlib
from pathlib import Path
import pytest
from odooupgrader.core import OdooUpgrader
from odooupgrader.jobs import RecoveryError, inventory


@pytest.mark.parametrize('failed_target,checkpoint', [('18.0', '17.0'), ('19.0', '18.0')])
def test_real_checkpoint_restore_resume(tmp_path, docker_test_settings, monkeypatch, failed_target, checkpoint):
    backup, postgres = docker_test_settings
    original = hashlib.sha256(backup.read_bytes()).hexdigest()
    root = tmp_path / 'approved-local-migration-tests'
    first = OdooUpgrader(str(backup), '19.0', postgres_version=postgres, jobs_dir=root)
    first.assert_local_docker()  # Reject any remote context before admitting the test.
    execute = OdooUpgrader.run_upgrade_step
    transitions = []
    def fail_at_transition(self, target):
        transitions.append(target)
        if target == failed_target:
            raise RecoveryError('TEST_INJECTION: isolated failure before pending transition')
        return execute(self, target)
    with monkeypatch.context() as scoped:
        scoped.setattr(OdooUpgrader, 'run_upgrade_step', fail_at_transition)
        with pytest.raises(RecoveryError, match='TEST_INJECTION'):
            first.run()
    checkpoint_path, manifest = first.job.checkpoint()
    assert manifest['version'] == checkpoint
    before = inventory(checkpoint_path)
    second = OdooUpgrader('', '19.0', postgres_version=postgres, jobs_dir=root, job_id=first.job.id)
    def record(self, target):
        transitions.append(target)
        return execute(self, target)
    monkeypatch.setattr(OdooUpgrader, 'run_upgrade_step', record)
    second.run()
    assert transitions.count('17.0') == 1
    if failed_target == '19.0':
        assert transitions.count('18.0') == 1
    assert transitions.count(failed_target) == 2
    assert inventory(checkpoint_path) == before
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original
    assert second.job.data['state'] == 'COMPLETED'
    assert Path(second.job.data['artifact']['path']).is_file()
    # No automatic deletion of retained checkpoints, inputs or Docker volumes.
