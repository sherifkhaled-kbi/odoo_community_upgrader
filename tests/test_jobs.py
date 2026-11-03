import json
import multiprocessing
from pathlib import Path
import shutil
from unittest.mock import patch
import pytest
from odooupgrader.jobs import Job, RecoveryError, atomic_json, inventory, require_space


def job(tmp_path):
    instance = Job.create(tmp_path, '19.0', '13', {})
    instance.begin()
    return instance


def snapshot(stage):
    (stage / 'database.dump').write_bytes(b'restored-fixture')
    (stage / 'filestore').mkdir()
    (stage / 'filestore' / 'file').write_bytes(b'attachment')
    return {'passed': True, 'restored': True, 'registry': True}


def test_checkpoint_and_corruption(tmp_path):
    j = job(tmp_path)
    j.publish('17.0', {}, snapshot)
    latest = j.publish('18.0', {}, snapshot)
    path, manifest = j.checkpoint()
    assert manifest['version'] == '18.0'
    (path / 'database.dump').write_bytes(b'corrupt')
    with pytest.raises(RecoveryError, match='CHECKPOINT_INVALID'):
        j.checkpoint()
    assert j.data['latest_checkpoint'] == latest['id']
    assert len(j.data['completed']) == 2


def test_prune_checkpoints_keeps_only_latest(tmp_path):
    """DB-001 regression: single-copy mode must not accumulate one full checkpoint
    per completed transition forever; pruning must leave the ledger history intact
    (checkpoint() still resolves the latest entry correctly) while removing the
    on-disk payload of older ones."""
    j = job(tmp_path)
    j.publish('17.0', {}, snapshot)
    j.prune_checkpoints(keep=1)
    j.publish('18.0', {}, snapshot)
    j.prune_checkpoints(keep=1)
    latest = j.publish('19.0', {}, snapshot)
    j.prune_checkpoints(keep=1)

    remaining = [p.name for p in (j.path / 'checkpoints').iterdir()]
    assert remaining == [latest['id']]
    assert len(j.data['completed']) == 3  # Ledger history is never touched by pruning.
    path, manifest = j.checkpoint()
    assert manifest['version'] == '19.0'
    assert path.name == latest['id']


def test_missing_checkpoint(tmp_path):
    j = job(tmp_path)
    j.publish('18.0', {}, snapshot)
    shutil.rmtree(j.checkpoint()[0])
    with pytest.raises(RecoveryError, match='CHECKPOINT_INVALID'):
        j.checkpoint()


def test_unpublished_directory_never_adopted(tmp_path):
    j = job(tmp_path)
    initial = j.publish('17.0', {}, snapshot)
    with patch.object(j, 'save', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            j.publish('18.0', {}, snapshot)
    fresh = Job(tmp_path, j.id)
    assert fresh.checkpoint()[1]['version'] == '17.0'
    assert fresh.data['latest_checkpoint'] == initial['id']


def test_failed_builder_does_not_publish(tmp_path):
    j = job(tmp_path)
    def fail(stage):
        snapshot(stage)
        raise RuntimeError('restore failed')
    with pytest.raises(RuntimeError):
        j.publish('18.0', {}, fail)
    assert j.data['latest_checkpoint'] is None
    assert not j.data['completed']


def test_atomic_metadata_failure_retains_previous_bytes(tmp_path):
    path = tmp_path / 'job.json'
    atomic_json(path, {'state': 'FAILED'})
    with patch('odooupgrader.jobs.os.replace', side_effect=OSError('full')):
        with pytest.raises(OSError):
            atomic_json(path, {'state': 'COMPLETED'})
    assert json.loads(path.read_text()) == {'state': 'FAILED'}


def test_two_processes_cannot_own_job(tmp_path):
    j = job(tmp_path)
    def contender(root, identifier, queue):
        try:
            with Job(root, identifier).lock():
                queue.put('owned')
        except RecoveryError:
            queue.put('blocked')
    ctx = multiprocessing.get_context('fork')
    queue = ctx.Queue()
    with j.lock():
        process = ctx.Process(target=contender, args=(tmp_path, j.id, queue))
        process.start()
        process.join(3)
        assert process.exitcode == 0
        assert queue.get(timeout=1) == 'blocked'
    assert (j.path / 'job.lock').exists()


def test_disk_and_symlink_guard(tmp_path):
    with patch('odooupgrader.jobs.shutil.disk_usage', return_value=shutil._ntuple_diskusage(10, 9, 1)):
        with pytest.raises(RecoveryError, match='DISK_SPACE'):
            require_space(tmp_path, 2)
    (tmp_path / 'link').symlink_to('/etc/passwd')
    with pytest.raises(RecoveryError, match='unsafe'):
        inventory(tmp_path)
