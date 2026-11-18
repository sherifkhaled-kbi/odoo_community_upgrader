"""Covers the <jobs-dir>/Complete/ publication step added on top of the existing
finalize/checkpoint pipeline (RecoveryMixin._publish_artifact / _success_summary).
"""
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from odooupgrader.jobs import Job, RecoveryError, digest

from test_recovery import FixtureUpgrader, source  # noqa: F401  (source is a pytest fixture)


def make_job_upgrader(tmp_path, target='19.0', single_copy=False):
    jobs_dir = tmp_path / 'jobs'
    job = Job.create(jobs_dir, target, '13', {
        'runtime_plan': {'openupgrade': {}}, 'extra_addons': False,
        'allow_missing_attachments': False, 'single_copy': single_copy, 'source_version': None})
    return FixtureUpgrader('', target, jobs_dir=jobs_dir, job_id=job.id)


def make_fake_zip(tmp_path, name='upgraded.zip', content=b'finalized-artifact-payload'):
    path = tmp_path / name
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('dump.sql', content)
    return path


def test_publish_creates_zip_and_checksum(tmp_path):
    upgrader = make_job_upgrader(tmp_path)
    fake_zip = make_fake_zip(tmp_path)
    record = upgrader._publish_artifact(fake_zip, '19.0')
    destination = Path(record['path'])
    checksum_path = Path(record['checksum_path'])
    assert destination.parent == tmp_path / 'jobs' / 'Complete'
    assert destination.is_file()
    assert checksum_path.is_file()
    assert upgrader.job.id in destination.name
    assert '19.0' in destination.name
    assert digest(destination) == digest(fake_zip) == record['sha256']
    assert record['sha256'] in checksum_path.read_text()
    assert upgrader.job.data['published_artifact'] == record
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None


def test_publish_is_idempotent(tmp_path):
    upgrader = make_job_upgrader(tmp_path)
    fake_zip = make_fake_zip(tmp_path)
    first = upgrader._publish_artifact(fake_zip, '19.0')
    complete_dir = tmp_path / 'jobs' / 'Complete'
    before = sorted(p.name for p in complete_dir.iterdir())
    second = upgrader._publish_artifact(fake_zip, '19.0')
    after = sorted(p.name for p in complete_dir.iterdir())
    assert first == second
    assert before == after


def test_publish_refuses_to_overwrite_conflicting_file(tmp_path, monkeypatch):
    monkeypatch.setattr('odooupgrader.recovery.time.strftime', lambda fmt, t=None: '20260102T030405Z')
    upgrader = make_job_upgrader(tmp_path)
    fake_zip = make_fake_zip(tmp_path)
    complete_dir = tmp_path / 'jobs' / 'Complete'
    complete_dir.mkdir(parents=True)
    colliding = complete_dir / f'{upgrader.job.id}__19.0__20260102T030405Z.zip'
    colliding.write_bytes(b'not the real artifact')
    with pytest.raises(RecoveryError, match='ARTIFACT_PUBLISH_CONFLICT'):
        upgrader._publish_artifact(fake_zip, '19.0')
    assert not upgrader.job.data.get('published_artifact')
    assert colliding.read_bytes() == b'not the real artifact'


def test_publish_disk_space_exhausted(tmp_path, monkeypatch):
    upgrader = make_job_upgrader(tmp_path)
    fake_zip = make_fake_zip(tmp_path)
    monkeypatch.setattr('odooupgrader.jobs.shutil.disk_usage',
                        lambda path: SimpleNamespace(total=0, used=0, free=0))
    with pytest.raises(RecoveryError, match='DISK_SPACE'):
        upgrader._publish_artifact(fake_zip, '19.0')
    assert not upgrader.job.data.get('published_artifact')
    assert not list((tmp_path / 'jobs' / 'Complete').glob('*.zip'))


def test_publish_detects_corrupted_copy_and_leaves_no_partial_file(tmp_path, monkeypatch):
    upgrader = make_job_upgrader(tmp_path)
    fake_zip = make_fake_zip(tmp_path)

    def corrupt_copy(src, dst):
        Path(dst).write_bytes(b'corrupted-during-copy')

    monkeypatch.setattr('odooupgrader.recovery.shutil.copyfile', corrupt_copy)
    with pytest.raises(RecoveryError, match='ARTIFACT_INVALID'):
        upgrader._publish_artifact(fake_zip, '19.0')
    complete_dir = tmp_path / 'jobs' / 'Complete'
    assert not list(complete_dir.glob('*.zip'))
    assert not list(complete_dir.glob('.*.part'))
    assert not upgrader.job.data.get('published_artifact')


def test_run_publishes_final_artifact_to_complete_dir(tmp_path, source):
    upgrader = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    result = upgrader.run()
    complete_dir = tmp_path / 'jobs' / 'Complete'
    zips = list(complete_dir.glob('*.zip'))
    assert len(zips) == 1
    assert digest(zips[0]) == upgrader.job.data['published_artifact']['sha256']
    assert result == upgrader.job.data['artifact']['path']  # return contract unchanged


def test_already_completed_job_is_published_lazily_on_next_touch(tmp_path, source):
    first = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    first.run()
    complete_dir = tmp_path / 'jobs' / 'Complete'
    # Simulate a job that finished before this feature shipped: no published_artifact,
    # and nothing in Complete/ yet.
    first.job.data.pop('published_artifact', None)
    first.job.save()
    for path in complete_dir.glob('*'):
        path.unlink()
    second = FixtureUpgrader('', '19.0', jobs_dir=tmp_path / 'jobs', job_id=first.job.id)
    result = second.run()
    assert list(complete_dir.glob('*.zip'))
    assert result == first.job.data['artifact']['path']


def test_publish_failure_never_reports_false_success(tmp_path, source, monkeypatch):
    def boom(self, source_path, version):
        raise RecoveryError('ARTIFACT_PUBLISH_CONFLICT: forced for test')

    monkeypatch.setattr(FixtureUpgrader, '_publish_artifact', boom)
    upgrader = FixtureUpgrader(str(source), '19.0', jobs_dir=tmp_path / 'jobs')
    with pytest.raises(RecoveryError, match='ARTIFACT_PUBLISH_CONFLICT'):
        upgrader.run()
    assert upgrader.job.data['state'] == 'FAILED'
    assert upgrader.job.data['artifact'] is None
    assert not upgrader.job.data.get('published_artifact')
    assert list((upgrader.job.path / 'diagnostics').glob('*.zip'))
