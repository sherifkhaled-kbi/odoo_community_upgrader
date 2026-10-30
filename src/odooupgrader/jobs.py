"""Small host-local job ledger and immutable checkpoint file protocol."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid


class RecoveryError(RuntimeError):
    """An actionable safety gate failed; never retry mutable working data."""


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(root):
    """Hash all regular files; reject links and special files, including directories."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise RecoveryError('CHECKPOINT_INVALID: missing or linked directory')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise RecoveryError('CHECKPOINT_INVALID: unsafe snapshot entry')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {'sha256': digest(path), 'size': path.stat().st_size}
    return result


def require_space(path, required, reserve=256 * 1024 * 1024):
    if shutil.disk_usage(path).free < required + reserve:
        raise RecoveryError('DISK_SPACE: insufficient space; retain checkpoints and free capacity explicitly')


def allocated_bytes(root, seen=None):
    """Read allocated blocks without following links or reading file contents."""
    root = Path(root)
    if not root.exists():
        return 0
    seen = set() if seen is None else seen
    total = 0
    if root.is_symlink():
        return 0
    root_info = root.stat(follow_symlinks=False)
    root_identity = (root_info.st_dev, root_info.st_ino)
    if root_identity not in seen:
        seen.add(root_identity)
        total += root_info.st_blocks * 512
    if not root.is_dir():
        return total
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            info = path.stat(follow_symlinks=False)
            if path.is_symlink():
                directories[:] = [item for item in directories if item != name]
                continue
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                seen.add(identity)
                total += info.st_blocks * 512
    return total


class Job:
    SCHEMA = 1

    def __init__(self, root, job_id):
        try:
            if str(uuid.UUID(job_id)) != job_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise RecoveryError('JOB_ID: expected canonical UUID') from exc
        self.path = Path(root).resolve() / job_id
        self.id = job_id
        self.data = self.read()

    @classmethod
    def create(cls, root, target, postgres, config):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        identifier = str(uuid.uuid4())
        path = root / identifier
        path.mkdir(mode=0o700)
        for directory in ('attempts', 'checkpoints', 'diagnostics', 'inputs'):
            (path / directory).mkdir(mode=0o700)
        atomic_json(path / 'job.json', {
            'schema': cls.SCHEMA, 'job_id': identifier, 'target': target,
            'postgres': postgres, 'source_version': None, 'stage': 'CREATED',
            'state': 'CREATED', 'created': time.time(), 'attempts': [],
            'completed': [], 'latest_checkpoint': None, 'config': config,
            'remediations': [], 'artifact': None,
        })
        return cls(root, identifier)

    def read(self):
        try:
            data = json.loads((self.path / 'job.json').read_text())
            if data['schema'] != self.SCHEMA or data['job_id'] != self.id:
                raise ValueError()
            return data
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryError('JOB_METADATA: missing, corrupt or unsupported ledger') from exc

    def save(self):
        atomic_json(self.path / 'job.json', self.data)

    @contextlib.contextmanager
    def lock(self):
        # Never unlink this inode. A stale heartbeat is not ownership evidence.
        with (self.path / 'job.lock').open('a') as stream:
            os.chmod(self.path / 'job.lock', 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecoveryError('JOB_BUSY: another invocation owns this job') from exc
            try:
                self.data = self.read()
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def begin(self):
        if self.data['state'] == 'RUNNING' and self.data['attempts']:
            self.data['attempts'][-1]['outcome'] = 'INTERRUPTED'
            self.data['attempts'][-1]['ended'] = time.time()
        attempt = {'id': str(uuid.uuid4()), 'started': time.time(), 'outcome': 'RUNNING',
                   'transitions': [], 'runtime': {}, 'input_checkpoint': self.data['latest_checkpoint']}
        self.data['attempts'].append(attempt)
        self.data.update(state='RUNNING', stage='PREPARING', artifact=None)
        self.save()
        return attempt

    def stage(self, name):
        self.data['stage'] = name
        if self.data['attempts']:
            self.data['attempts'][-1]['stage'] = name
        self.save()

    def checkpoint(self):
        identifier = self.data['latest_checkpoint']
        if not identifier:
            raise RecoveryError('NO_CHECKPOINT: start a new job from the retained original input')
        # Resolve only the authoritative latest entry; never scan for an older one.
        try:
            uuid.UUID(identifier)
            if self.data['completed'][-1]['id'] != identifier:
                raise ValueError()
            parent = None
            for item in self.data['completed']:
                if str(uuid.UUID(item['id'])) != item['id']:
                    raise ValueError()
                saved = self.path / 'checkpoints' / item['id'] / 'manifest.json'
                if digest(saved) != item['manifest_sha256']:
                    raise ValueError()
                prior = json.loads(saved.read_text())
                if prior['parent'] != parent or prior['job_id'] != self.id:
                    raise ValueError()
                parent = item['id']
            entry = self.data['completed'][-1]
            directory = self.path / 'checkpoints' / identifier
            manifest_path = directory / 'manifest.json'
            if digest(manifest_path) != entry['manifest_sha256']:
                raise ValueError()
            manifest = json.loads(manifest_path.read_text())
            if (manifest['job_id'] != self.id or manifest['id'] != identifier
                    or manifest['version'] != entry['version'] or not manifest['validation']['passed']):
                raise ValueError()
            actual = inventory(directory)
            del actual['manifest.json']
            if actual != manifest['files']:
                raise ValueError()
            return directory, manifest
        except (OSError, ValueError, KeyError, StopIteration, TypeError, IndexError) as exc:
            raise RecoveryError('CHECKPOINT_INVALID: latest snapshot missing or corrupt; restore its exact backup') from exc

    def publish(self, version, runtime, builder):
        identifier = str(uuid.uuid4())
        parent = self.path / 'checkpoints'
        stage = parent / ('.staging-' + identifier)
        stage.mkdir(mode=0o700)
        try:
            validation = builder(stage)
        except BaseException:
            if self.data['config'].get('single_copy'):
                # An incomplete streamed ZIP is not a checkpoint and may be large.
                try:
                    shutil.rmtree(stage)
                except OSError:
                    pass  # Preserve the original export failure for diagnostics.
            raise
        if not validation.get('passed'):
            raise RecoveryError('VALIDATION_FAILED: checkpoint candidate did not pass')
        files = inventory(stage)
        manifest = {'schema': 1, 'id': identifier, 'job_id': self.id, 'version': version,
                    'parent': self.data['latest_checkpoint'], 'attempt': self.data['attempts'][-1]['id'],
                    'runtime': runtime, 'source_sha256': self.data.get('input_sha256'),
                    'validation': validation, 'files': files, 'created': time.time()}
        if validation.get('checkpoint_format') == 'zip':
            manifest['checkpoint_format'] = 'zip'
        atomic_json(stage / 'manifest.json', manifest)
        for path in stage.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        for directory in sorted((p for p in stage.rglob('*') if p.is_dir()), reverse=True):
            sync_dir(directory)
        sync_dir(stage)
        destination = parent / identifier
        os.rename(stage, destination)
        sync_dir(parent)
        # Rename precedes ledger commit. Unreferenced directories are never adopted.
        entry = {'id': identifier, 'version': version, 'manifest_sha256': digest(destination / 'manifest.json')}
        self.data['completed'].append(entry)
        self.data['latest_checkpoint'] = identifier
        transitions = self.data['attempts'][-1]['transitions']
        if transitions and transitions[-1]['target'] == version:
            transitions[-1].update(outcome='SUCCEEDED', checkpoint=identifier)
        self.save()
        return entry

    def observed_state(self):
        """Read-only liveness hint; execution always rechecks under the exclusive lock."""
        lock = self.path / 'job.lock'
        if self.data['state'] != 'RUNNING':
            return self.data['state']
        if not lock.exists():
            return 'INTERRUPTED'
        with lock.open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'RUNNING'
            fcntl.flock(stream, fcntl.LOCK_UN)
        return 'INTERRUPTED'
