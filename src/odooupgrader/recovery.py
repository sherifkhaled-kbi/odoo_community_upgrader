"""Synchronous recovery hooks for the existing local Docker pipeline."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import zipfile

from .checks import FACTOR_FIX_SCRIPT_SHA256, LIBRARY_REVISION, OPENUPGRADE_REVISIONS, extract_zip, validate_uom, verify_attachments
from .diagnostics import RedactionFilter, collect, redact
from .jobs import Job, RecoveryError, atomic_json, digest, inventory, require_space, sync_dir
from .postgres_restore import (RestoreFailure, inspect_dump, select_runtime, validate_runtime, client_version, version_number, sql_failure_evidence)

logger = logging.getLogger('odooupgrader')


class RecoveryMixin:
    # Odoo's official Debian-packaged image layout; used only as best-effort preflight evidence.
    OFFICIAL_ADDONS_PATH = '/usr/lib/python3/dist-packages/odoo/addons'

    def _configure_recovery(self, jobs_dir, job_id, revisions, acceptance, reason, filestore,
                            allow_missing_attachments, single_copy, source_version,
                            skip_missing_modules_check=False):
        self.jobs_dir = Path(jobs_dir or Path.cwd() / '.odooupgrader' / 'jobs').resolve()
        self.job = Job(self.jobs_dir, job_id) if job_id else None
        if self.job and (self.target_version != self.job.data['target'] or self.postgres_version != self.job.data['postgres']):
            raise RecoveryError('JOB_CONFIGURATION: target and PostgreSQL must match the persisted job')
        self.revisions = dict(self.job.data["config"]["runtime_plan"]["openupgrade"] if self.job else OPENUPGRADE_REVISIONS)
        self.revisions.update(revisions or {})
        for key, sha in self.revisions.items():
            if key not in self.VALID_VERSIONS or not re.fullmatch(r'[0-9a-f]{40}', sha):
                raise RecoveryError('RUNTIME_REVISION: expected VERSION=full-lowercase-commit-SHA')
        self.acceptance, self.reason = acceptance, reason
        self.input_filestore = filestore
        self.allow_missing_attachments = (self.job.data['config'].get('allow_missing_attachments', False)
                                          if self.job else allow_missing_attachments)
        self.single_copy = (self.job.data['config'].get('single_copy', False)
                            if self.job else single_copy)
        self.source_version_hint = (self.job.data['config'].get('source_version')
                                    if self.job else source_version)
        self.skip_missing_modules_check = skip_missing_modules_check
        if self.source_version_hint and int(self.source_version_hint.split('.')[0]) > int(self.target_version.split('.')[0]):
            raise RecoveryError('SOURCE_VERSION: source version cannot exceed target version')
        self._handler = None
        self.db_container = None
        self.network = None
        self.current_runtime = {}
        self.module_baseline = []
        self.uom_baseline = None
        self.uom_rows = None
        self.pg_selected = self.job.data.get('selected_postgres') if self.job else None
        self.pg_runtime = None
        self._database_restored = False
        self._restore_failed = False
        self.prepared_dump_path = None
        self._last_filestore_fingerprint = None

    def _runtime_plan(self):
        package = Path(__file__).parent
        code = hashlib.sha256()
        for path in sorted(package.glob('*.py')):
            code.update(path.name.encode())
            code.update(path.read_bytes())
        try:
            commit = subprocess.run(['git', '-C', str(package), 'rev-parse', 'HEAD'],
                                    check=True, capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = None
        return {'openupgrade': self.revisions, 'openupgradelib': LIBRARY_REVISION,
                'application_commit': commit, 'application_version': '0.4.0',
                'application_fingerprint': code.hexdigest()}

    def _plan_change(self, commit=False, enforce=True):
        proposed = self._runtime_plan()
        recorded = self.job.data['config']['runtime_plan']
        # Only uncompleted transitions are configurable. Completed provenance is immutable.
        completed = {entry['version'] for entry in self.job.data['completed']}
        for major in completed:
            if proposed['openupgrade'].get(major) != recorded['openupgrade'].get(major):
                raise RecoveryError('COMPLETED_RUNTIME: completed transition revision cannot change')
        change = {'before': recorded, 'after': proposed}
        value = hashlib.sha256(json.dumps(change, sort_keys=True).encode()).hexdigest()
        if recorded != proposed:
            if enforce and (self.acceptance != value or not self.reason):
                raise RecoveryError('RUNTIME_CHANGED: review pending runtime diff; acceptance SHA256=' + value)
            if commit:
                # Free text reason stays local; bundles export only a hash.
                self.job.data['remediations'].append({'change_sha256': value,
                    'reason_sha256': hashlib.sha256(self.reason.encode()).hexdigest(), 'time': time.time()})
                self.job.data['config']['runtime_plan'] = proposed
                self.job.save()
        return change, value

    def dry_run(self):
        workspace = self._reusable_workspace(
            allow_interrupted_running=self.job.observed_state() == 'INTERRUPTED')
        if self.job.data['latest_checkpoint']:
            directory, manifest = self.job.checkpoint()
        elif workspace:
            directory, manifest = None, {'version': workspace['workspace_ready']['version'],
                'checkpoint_format': None, 'validation': {'independent_restore': False}}
        else:
            raise RecoveryError('NO_CHECKPOINT: no verified checkpoint or reusable pre-migration workspace')
        change, change_hash = self._plan_change(enforce=False)
        return {'job_id': self.job.id, 'checkpoint': directory.name if directory else None, 'version': manifest['version'],
                'target': self.job.data['target'], 'state': self.job.data['state'],
                'reuse_pre_migration_workspace': bool(workspace),
                'runtime_diff': change, 'change_sha256': change_hash,
                'built_runtime_candidates': self.job.data.get('runtime_candidates', {}),
                'deferred_checks': ['exclusive ownership', 'Docker reconciliation',
                    'workspace identity and data validation' if workspace else 'fresh restore',
                    'registry verification', 'disk capacity'],
                'checkpoint_format': manifest.get('checkpoint_format', 'directory'),
                'independent_restore_verified': manifest['validation'].get('independent_restore', True)}

    def assert_local_docker(self):
        if sys.platform != 'linux':
            raise RecoveryError('LOCAL_LINUX_REQUIRED: run the CLI on the Linux Docker host')
        host = os.environ.get('DOCKER_HOST', '')
        if host and not host.startswith('unix://'):
            raise RecoveryError('LOCAL_DOCKER_REQUIRED: remote Docker endpoints are excluded')
        result = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                                check=True, capture_output=True, text=True)
        if not result.stdout.strip().startswith('unix://'):
            raise RecoveryError('LOCAL_DOCKER_REQUIRED: select the local Docker engine')
        subprocess.run(['docker', 'info', '--format', '{{.ID}}'], check=True, capture_output=True, text=True)

    def _inspect_owned(self, name, attempt_id):
        result = subprocess.run(['docker', 'container', 'inspect', name], capture_output=True, text=True, timeout=10)
        if result.returncode:
            if 'No such' in result.stderr:
                return None
            raise RecoveryError('DOCKER_INSPECT: cannot establish resource ownership')
        info = json.loads(result.stdout)[0]
        labels = info.get('Config', {}).get('Labels', {})
        if labels.get('odooupgrader.job') != self.job.id or labels.get('odooupgrader.attempt') != attempt_id:
            raise RecoveryError('RESOURCE_OWNER: refusing to operate on an unrelated container')
        return info

    def _stop_attempt(self, attempt):
        errors = []
        for name in reversed(attempt.get('containers', [])):
            try:
                info = self._inspect_owned(name, attempt['id'])
                if info and info['State']['Running']:
                    subprocess.run(['docker', 'stop', info['Id']], check=True, capture_output=True, timeout=30)
            except RecoveryError:
                raise
            except Exception as exc:
                errors.append((name, exc))
        if errors:
            names = ', '.join(name for name, _ in errors)
            raise RecoveryError(f'DOCKER_STOP: could not stop or inspect attempt containers: {names}') from errors[0][1]

    @staticmethod
    def _stage_label(name):
        fixed = {'INPUT_PREPARATION': '1/6 Prepare', 'RESTORE_PREFLIGHT': '2/6 Restore',
                 'RESTORING': '2/6 Restore', 'CHECKPOINTING': '6/6 Checkpoint'}
        if name in fixed:
            return fixed[name]
        for prefix, label in (('PREFLIGHT_', '3/6 Preflight'), ('BUILDING_', '4/6 Build'), ('MIGRATING_', '5/6 Migrate')):
            if name.startswith(prefix):
                return f'{label} ({name[len(prefix):]})'
        return None

    def _stage(self, name):
        self.job.stage(name)
        label = self._stage_label(name)
        if label:
            from .core import console
            console.print(f'[bold cyan]Stage {label}[/bold cyan]')

    def _new_attempt(self):
        attempt = self.job.begin()
        work = self.job.path / 'attempts' / attempt['id']
        work.mkdir(mode=0o700)
        attempt['work'] = str(work)
        self._set_attempt_paths(attempt)
        return attempt

    def _set_attempt_paths(self, attempt):
        work = self.job.path / 'attempts' / attempt['id']
        suffix = attempt['id'].replace('-', '')
        self.db_container = 'ou-db-' + suffix
        self.network = 'ou-net-' + suffix
        if 'containers' not in attempt:
            attempt['containers'] = [self.db_container]
            self.job.save()
        self.cwd = str(work)
        self.source_dir = str(work / 'source')
        self.output_dir = str(work / 'output')
        self.data_dir = str(work / 'runtime-data')
        self.filestore_dir = str(work / 'runtime-data' / 'filestore' / 'database')
        self.custom_addons_dir = str(work / 'output' / 'custom_addons')
        self._handler = logging.FileHandler(work / 'migration.log')
        self._handler.addFilter(RedactionFilter())
        self._handler.setLevel(logging.DEBUG)
        logger.addHandler(self._handler)

    def _reusable_workspace(self, allow_interrupted_running=False):
        if not self.single_copy or not self.job or not self.job.data['attempts']:
            return None
        attempt = self.job.data['attempts'][-1]
        ready = attempt.get('workspace_ready')
        allowed = ('FAILED', 'INTERRUPTED', 'RUNNING') if allow_interrupted_running else ('FAILED', 'INTERRUPTED')
        if (attempt['outcome'] not in allowed or not ready
                or attempt.get('migration_started')):
            return None
        checkpoint = self.job.data['latest_checkpoint']
        if not checkpoint and ready['version'] != self.job.data.get('source_version'):
            return None
        if checkpoint and (not self.job.data['completed'] or
                           self.job.data['completed'][-1]['id'] != checkpoint or
                           self.job.data['completed'][-1]['version'] != ready['version']):
            return None
        return attempt

    def _filestore_fingerprint(self):
        files = inventory(self.filestore_dir)
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def _mark_workspace_ready(self, version, filestore_fingerprint=None):
        info = self._inspect_owned(self.db_container, self.attempt['id'])
        if not info or not info['State']['Running']:
            raise RecoveryError('WORKSPACE_INVALID: working PostgreSQL container is not running')
        self.attempt['workspace_ready'] = {
            'version': version, 'db_container_id': info['Id'],
            'filestore_sha256': filestore_fingerprint or self._filestore_fingerprint(),
            'modules': self.module_baseline, 'runtime': self.current_runtime}
        self.attempt['migration_started'] = False
        self.job.save()

    def _reuse_workspace(self):
        ready = self.attempt['workspace_ready']
        info = self._inspect_owned(self.db_container, self.attempt['id'])
        if not info or info['Id'] != ready['db_container_id']:
            raise RecoveryError('WORKSPACE_INVALID: working PostgreSQL container changed or disappeared')
        network = self._run_cmd(['docker', 'network', 'inspect', self.network], capture_output=True)
        attached = json.loads(network.stdout)[0].get('Containers') or {}
        if any(container_id != info['Id'] for container_id in attached):
            raise RecoveryError('WORKSPACE_INVALID: unexpected container is attached to the private network')
        # A stopped migration/verify container from an already-checkpointed transition
        # earlier in this same attempt is retained evidence, not an active writer: the
        # network-attachment check above already proves nothing is currently connected.
        # Only a container that is still RUNNING indicates an unresolved writer.
        for name in self.attempt['containers']:
            if name == self.db_container:
                continue
            info = self._inspect_owned(name, self.attempt['id'])
            if info and info['State']['Running']:
                raise RecoveryError('WORKSPACE_INVALID: migration container exists; restore a checkpoint')
        if self._filestore_fingerprint() != ready['filestore_sha256']:
            raise RecoveryError('WORKSPACE_INVALID: filestore changed; restore a checkpoint')
        self._run_cmd(['docker', 'start', self.db_container], capture_output=True)
        self.wait_for_db()
        self._database_restored = True
        self.pg_runtime = self.attempt.get('postgres_runtime')
        self.module_baseline = ready['modules']
        validation = self._technical(ready['version'], self.filestore_dir)
        if validation['modules'] != ready['modules']:
            raise RecoveryError('WORKSPACE_INVALID: installed modules changed; restore a checkpoint')
        self.current_runtime = ready['runtime']
        return ready['version']

    def prepare_environment(self):
        for directory in (self.source_dir, self.output_dir, self.filestore_dir, self.custom_addons_dir):
            Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)

    def _pg_data_volume_path(self):
        """PostgreSQL 18's official image mounts the named volume at /var/lib/postgresql;
        13-17 mount it at /var/lib/postgresql/data. Used for both the Compose volume
        target and in-container capacity checks, so both stay in sync with the
        actually selected restore runtime."""
        selected = self.pg_selected or self.postgres_version
        return '/var/lib/postgresql' if int(selected.split('.')[0]) >= 18 else '/var/lib/postgresql/data'

    def create_db_compose_file(self):
        labels = {'odooupgrader.job': self.job.id, 'odooupgrader.attempt': self.attempt['id']}
        selected = self.pg_selected or self.postgres_version
        compose = {'services': {'db-odooupgrade': {
            'container_name': self.db_container, 'image': 'postgres:' + selected,
            'labels': labels, 'environment': {'POSTGRES_DB': 'odoo', 'POSTGRES_USER': 'odoo', 'POSTGRES_PASSWORD': 'odoo'},
            'networks': ['migration'], 'volumes': ['postgres_data:' + self._pg_data_volume_path()], 'restart': 'no'}},
            'networks': {'migration': {'name': self.network, 'internal': True}},
            'volumes': {'postgres_data': {'labels': labels}}}
        atomic_json(Path(self.cwd) / 'db-composer.yml', compose)

    def wait_for_db(self):
        for _ in range(30):
            # The official image starts a socket-only temporary server during
            # initialization. Wait for the final TCP server instead.
            ready = self._run_cmd(['docker', 'exec', self.db_container, 'pg_isready',
                                   '-h', '127.0.0.1', '-U', 'odoo', '-d', 'postgres'],
                                  check=False, capture_output=True)
            if ready.returncode == 0:
                query = self._run_cmd(['docker', 'exec', self.db_container, 'psql',
                                       '-X', '-A', '-t', '-U', 'odoo', '-d', 'postgres',
                                       '-c', 'SELECT 1'], check=False, capture_output=True)
                if query.returncode == 0 and query.stdout.strip() == '1':
                    return
            time.sleep(2)
        raise RecoveryError('POSTGRES_STARTUP: final PostgreSQL server did not become ready; inspect this attempt\'s Docker logs')

    def _sql(self, query, database='database', timeout=60):
        return self._run_cmd(['docker', 'exec', '-e', 'PGOPTIONS=-c default_transaction_read_only=on',
            self.db_container, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'odoo', '-d', database,
            '-v', 'VERBOSITY=verbose', '-A', '-t', '-c', query], capture_output=True, timeout=timeout).stdout.strip()

    def _rows(self, select, database='database', timeout=60):
        return json.loads(self._sql("SELECT COALESCE(json_agg(t), '[]'::json) FROM (" + select + ') t', database, timeout=timeout))

    def get_current_version(self):
        return self._sql("SELECT latest_version FROM ir_module_module WHERE name='base' AND state='installed'")

    def _save_restore_evidence(self):
        try:
            self.job.save()
        except Exception:
            logger.error('Restore evidence could not be persisted; original error preserved')

    def _prepare_restore_input(self, snapshot):
        self._stage('RESTORE_PREFLIGHT')
        context = {'status': 'PREFLIGHT', 'requested_postgres': self.postgres_version,
                   'role': 'working', 'dump': None, 'runtime': None}
        self.attempt['restore'] = context
        try:
            if snapshot:
                directory, manifest = snapshot
                if manifest.get('checkpoint_format') == 'zip':
                    extract_zip(directory / 'checkpoint.zip', self.source_dir)
                    self.prepared_dump_path = Path(self.source_dir) / 'dump.sql'
                    path = self.prepared_dump_path
                    self.restore_file_type = 'ZIP'
                else:
                    path = directory / 'database.dump'
                    self.restore_file_type = 'DUMP'
            else:
                source = self.download_or_copy_source()
                self.job.data['input_sha256'] = digest(source)
                self.restore_file_type = self.process_source_file(source)
                path = self.prepared_dump_path
            context['input_container'] = 'checkpoint' if snapshot else ('zip' if self.restore_file_type == 'ZIP' else 'file')
            metadata = inspect_dump(path)
            context['dump'] = metadata
            requested = self.pg_selected or self.postgres_version
            self.pg_selected = select_runtime(requested, metadata)
            self.job.data['selected_postgres'] = self.pg_selected
            context.update(selected_postgres=self.pg_selected, status='PREFLIGHT_PASSED')
            self._save_restore_evidence()
        except RestoreFailure as exc:
            self._restore_failed = True
            if context['dump'] is None:
                context['dump'] = exc.details.get('dump')
            context.update(status='FAILED', reason=exc.reason, relevant_error=str(exc), phase_when_failed='PREFLIGHT')
            if (context.get('dump') or {}).get('required_statement'):
                context['incompatible_statement'] = context['dump']['required_statement']
            self._save_restore_evidence()
            raise
        except OSError as exc:
            self._restore_failed = True
            context.update(status='FAILED', reason='RESTORE_INPUT_UNREADABLE', phase_when_failed='PREFLIGHT')
            self._save_restore_evidence()
            raise RestoreFailure('RESTORE_INPUT_UNREADABLE', 'prepared dump cannot be read; check backup format and permissions') from exc

    def _ensure_pg_runtime(self):
        if self.pg_runtime is None:
            number = self._sql('SHOW server_version_num', database='postgres', timeout=5)
            server_version = self._sql('SHOW server_version', database='postgres', timeout=5)
            psql = self._run_cmd(['docker', 'exec', self.db_container, 'psql', '--version'], capture_output=True, timeout=10).stdout
            restore = self._run_cmd(['docker', 'exec', self.db_container, 'pg_restore', '--version'], capture_output=True, timeout=10).stdout
            info = self._inspect_owned(self.db_container, self.attempt['id'])
            if not info or not number.isdigit() or not version_number(server_version):
                raise RestoreFailure('RUNTIME_VERSION_UNKNOWN', 'cannot establish actual restore container/server identity')
            self.pg_runtime = {'server_version_num': int(number), 'server_version': version_number(server_version),
                'psql_version': client_version(psql), 'pg_restore_version': client_version(restore),
                'image_id': info['Image'], 'image_reference': info['Config']['Image']}
            self.attempt['postgres_runtime'] = self.pg_runtime
        return self.pg_runtime

    def _restore_storage_snapshot(self):
        """Read numeric capacity only, without SQL or customer/file contents."""
        roles = ('postgres_data', 'container_tmp', 'shared_memory')
        paths = (self._pg_data_volume_path(), '/tmp', '/dev/shm')
        try:
            blocks = self._run_cmd(['docker', 'exec', self.db_container, 'df', '-Pk', *paths],
                                   capture_output=True, timeout=10).stdout.splitlines()[1:]
            inodes = self._run_cmd(['docker', 'exec', self.db_container, 'df', '-Pi', *paths],
                                   capture_output=True, timeout=10).stdout.splitlines()[1:]
            if len(blocks) != len(roles) or len(inodes) != len(roles):
                raise ValueError('Unexpected df output')
            usage = {'available': True}
            for role, block, inode in zip(roles, blocks, inodes):
                b, i = block.split(), inode.split()
                usage[role] = {'total_bytes': int(b[1]) * 1024, 'available_bytes': int(b[3]) * 1024,
                               'available_inodes': int(i[3]) if i[3].isdigit() else None}
            return usage
        except Exception:
            return {'available': False, 'reason': 'Container capacity measurement unavailable'}

    def _restore_capacity(self, path, sql):
        usage = self._restore_storage_snapshot()
        context = self.attempt['restore']
        context['storage_before_restore'] = usage
        if not usage['available']:
            raise RestoreFailure('RESTORE_CAPACITY_UNKNOWN', 'cannot inspect PostgreSQL volume capacity; check Docker access and df availability before retry')
        size = Path(path).stat().st_size
        reserve = 256 * 1024 * 1024
        # Admission estimate, not a guarantee: indexes, WAL, extension data and
        # compression can require more. Include a copied custom archive when
        # the container layer and PGDATA share physical backing storage.
        required = {'postgres_data': 3 * size + (0 if sql else size) + reserve,
                    'container_tmp': (0 if sql else size) + reserve}
        context['capacity_estimate'] = {'dump_bytes': size, 'required_free_bytes': required,
                                        'estimate_only': True, 'sql_streamed': sql}
        for role, needed in required.items():
            measurement = usage[role]
            if measurement['available_inodes'] == 0:
                context.update(category='RESOURCE_EXHAUSTION', resource_condition='FILESYSTEM_INODES_EXHAUSTED')
                raise RestoreFailure('RESTORE_STORAGE_EXHAUSTED', 'restore filesystem has no free inodes; expand capacity or perform approved cleanup; no checkpoints are deleted')
            if measurement['available_bytes'] < needed:
                context.update(category='RESOURCE_EXHAUSTION', resource_condition='INSUFFICIENT_RESTORE_CAPACITY')
                raise RestoreFailure('RESTORE_CAPACITY_INSUFFICIENT',
                    f'{role} has {measurement["available_bytes"] // (1024**2)} MiB free; estimated restore requirement is {needed // (1024**2)} MiB. Expand capacity or perform approved cleanup before retry; inputs/checkpoints are retained')
        self._save_restore_evidence()

    def _restore_dump(self, path, database, sql=False):
        context = {'status': 'PREFLIGHT', 'role': 'working' if database == 'database' else 'verification',
                   'requested_postgres': self.postgres_version, 'selected_postgres': self.pg_selected or self.postgres_version,
                   'dump': None, 'runtime': self.pg_runtime,
                   'input_container': (self.attempt.get('restore') or {}).get('input_container', 'file') if database == 'database' else 'checkpoint'}
        self.attempt['restore'] = context
        self.attempt.setdefault('restore_history', []).append(context)
        if database == 'database':
            self._database_restored = False
        try:
            metadata = inspect_dump(path)
            context['dump'] = metadata
            selected = select_runtime(self.pg_selected or self.postgres_version, metadata)
            runtime = self._ensure_pg_runtime()
            context['runtime'] = runtime
            validate_runtime(metadata, runtime, selected)
            if sql != (metadata['format'] == 'plain_sql'):
                raise RestoreFailure('DUMP_FORMAT_MISMATCH', 'restore command does not match the inspected dump bytes')
            self._save_restore_evidence()
            self._restore_capacity(path, sql)
            if not sql:
                self._run_cmd(['docker', 'cp', str(path), self.db_container + ':/tmp/input.dump'], capture_output=True)
                # Read the archive TOC with the actual selected client before CREATE/restore.
                self._run_cmd(['docker', 'exec', self.db_container, 'pg_restore', '--list', '/tmp/input.dump'], capture_output=True)
            self._run_cmd(['docker', 'exec', self.db_container, 'createdb', '-U', 'odoo', '-T', 'template0', database], capture_output=True)
            command = ['docker', 'exec'] + (['-i'] if sql else []) + [self.db_container]
            if sql:
                command += ['psql', '-X', '--echo-errors', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose',
                            '--single-transaction', '-U', 'odoo', '-d', database, '-f', '-']
            else:
                command += ['pg_restore', '--exit-on-error', '--single-transaction', '--no-owner', '--no-privileges',
                            '-U', 'odoo', '-d', database, '/tmp/input.dump']
            context['status'] = 'RESTORING'
            self._save_restore_evidence()
            if sql:
                # Feed the private immutable SQL copy directly to psql; do not
                # duplicate a potentially huge dump in Docker's writable layer.
                with Path(path).open('rb') as source:
                    self._run_cmd(command, capture_output=True, stdin=source)
            else:
                self._run_cmd(command, capture_output=True)
            context['status'] = 'SUCCEEDED'
            if database == 'database':
                self._database_restored = True
            self._save_restore_evidence()
        except (RestoreFailure, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            self._restore_failed = True
            context.update(phase_when_failed=context['status'], status='FAILED', reason=exc.reason if isinstance(exc, RestoreFailure) else 'RESTORE_COMMAND_FAILED')
            if isinstance(exc, RestoreFailure):
                if context['dump'] is None:
                    context['dump'] = exc.details.get('dump')
                context['relevant_error'] = str(exc)
                if (context.get('dump') or {}).get('required_statement'):
                    context['incompatible_statement'] = context['dump']['required_statement']
            stderr = getattr(exc, 'stderr', '') or ''
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors='replace')
            if stderr:
                # Protected raw evidence, never inserted into the portable bundle.
                try:
                    private = Path(self.cwd) / 'restore.private.log'
                    with private.open('a') as stream:
                        os.chmod(private, 0o600)
                        stream.write(stderr[-2 * 1024 * 1024:] + '\n')
                except OSError:
                    logger.error('Private restore log unavailable; original restore error preserved')
                try:
                    context.update(sql_failure_evidence(path, stderr))
                except Exception:
                    context['relevant_error'] = 'Restore failed; statement evidence unavailable; inspect private log'
            if context.get('sqlstate') == '53100':
                context['reason'] = 'RESTORE_STORAGE_EXHAUSTED'
                context['storage_after_failure'] = self._restore_storage_snapshot()
            if isinstance(exc, subprocess.CalledProcessError):
                context['process_exit_code'] = exc.returncode
            self._save_restore_evidence()
            if isinstance(exc, RestoreFailure):
                raise
            if context.get('sqlstate') == '53100':
                raise RestoreFailure('RESTORE_STORAGE_EXHAUSTED', context['relevant_error'] + '. ' + context.get('recommended_action', 'Inspect storage capacity before starting a new restore'), context) from exc
            raise RestoreFailure('RESTORE_COMMAND_FAILED',
                'database restoration failed before migration; inspect restore_diagnostics.json and protected restore.private.log', context) from exc

    def restore_database(self, file_type):
        path = self.prepared_dump_path
        if path is None:
            raise RecoveryError('RESTORE_INPUT: source dump was not prepared')
        if not path.is_file():
            raise RecoveryError('RESTORE_INPUT: expected dump.sql or custom-format database.dump')
        if file_type == 'ZIP' and (Path(self.source_dir) / 'filestore').is_dir():
            # Both paths belong to the same attempt. Move the extracted store
            # into Odoo's data root without duplicating its contents.
            Path(self.filestore_dir).rmdir()
            try:
                os.rename(Path(self.source_dir) / 'filestore', self.filestore_dir)
            except OSError:
                Path(self.filestore_dir).mkdir()
                raise
        elif self.input_filestore:
            original_files = inventory(self.input_filestore)
            shutil.copytree(self.input_filestore, self.filestore_dir, dirs_exist_ok=True)
            if inventory(self.filestore_dir) != original_files:
                raise RecoveryError('FILESTORE_CHANGED: input filestore changed while it was copied')
        self._restore_dump(path, 'database', sql=(self.attempt.get('restore', {}).get('dump') or {}).get('format') == 'plain_sql')
        if self.single_copy and self.source and not self.source.startswith(('http://', 'https://')):
            if digest(self.source) != self.job.data['input_sha256']:
                raise RecoveryError('SOURCE_CHANGED: local backup changed during the restore')
        if self.single_copy and file_type == 'ZIP':
            try:
                path.unlink()  # The original ZIP or completed checkpoint remains available.
            except OSError:
                logger.warning('Restored ZIP input could not be removed from the working directory')

    def _unaccounted_missing_modules(self, missing, installed, module_mapping):
        """Resolve each missing module through OpenUpgrade's own apriori rename/merge
        chain; only a module whose chain ends outside the installed set is unaccounted
        for. This recognizes upstream-documented changes, never a guessed mapping."""
        unaccounted = set()
        for name in missing:
            seen, current = set(), name
            while current in module_mapping and current not in seen:
                seen.add(current)
                current = module_mapping[current]
            if current not in installed:
                unaccounted.add(name)
        return unaccounted

    @staticmethod
    def _scan_manifest_names(path):
        base = Path(path)
        if not base.is_dir():
            return set()
        return {child.name for child in base.iterdir() if child.is_dir() and (child / '__manifest__.py').is_file()}

    def _image_module_names(self, image_ref, path_in_image):
        """Best-effort module inventory read via `docker create`/`cp`; never starts the container."""
        container = None
        dest = Path(self.cwd) / ('addon-scan-' + uuid.uuid4().hex)
        try:
            container = self._run_cmd(['docker', 'create', image_ref], capture_output=True).stdout.strip()
            result = self._run_cmd(['docker', 'cp', f'{container}:{path_in_image}', str(dest)],
                                   check=False, capture_output=True)
            if result.returncode:
                return set()
            return self._scan_manifest_names(dest)
        except Exception:
            logger.warning('Could not inspect %s for addon modules at %s', image_ref, path_in_image)
            return set()
        finally:
            shutil.rmtree(dest, ignore_errors=True)
            if container:
                self._run_cmd(['docker', 'rm', '-f', container], check=False, capture_output=True)

    def _known_addon_names(self, target_version, image_id):
        names = self._image_module_names('odoo:' + target_version, self.OFFICIAL_ADDONS_PATH)
        names |= self._image_module_names(image_id, '/mnt/extra-addons')
        if self.extra_addons:
            names |= self._scan_manifest_names(self.custom_addons_dir)
        return names

    def _missing_module_preflight(self, target_version, prepared):
        """Catch modules the restored database still needs that no addons-path provides,
        before the long migration container runs (rather than a silent REGISTRY_VALIDATION
        failure ~20 minutes in)."""
        rows = self._rows("SELECT name, state, latest_version FROM ir_module_module "
                          "WHERE state IN ('installed', 'to upgrade', 'to install') ORDER BY name", 'database')
        mapping = prepared['environment'].get('module_mapping', {})
        available = self._known_addon_names(target_version, prepared['image_id'])
        missing = [r for r in rows if r['name'] not in available and r['name'] not in mapping]
        if not missing:
            return
        from .core import console
        if self.skip_missing_modules_check:
            logger.warning('Skipping missing-module preflight (--skip-missing-modules-check): %d module(s) '
                           'not found in addons-path for %s: %s', len(missing), target_version,
                           ', '.join(r['name'] for r in missing))
            console.print(f'[yellow]Warning: skipping missing-module check for {len(missing)} module(s) '
                          f'(--skip-missing-modules-check)[/yellow]')
            return
        console.print(f'[bold red]{len(missing)} module(s) installed in the database are missing from the '
                     f'addons-path for {target_version}; the upgrade would fail:[/bold red]')
        console.print(f'{"name":<40}{"state":<14}version')
        for row in missing:
            console.print(f"{row['name']:<40}{row['state']:<14}{row['latest_version']}")
        console.print(f'[yellow]Provide their code via --extra-addons for {target_version}, or pass '
                     f'--skip-missing-modules-check to override.[/yellow]')
        self.attempt['missing_modules'] = [{'name': r['name'], 'state': r['state'], 'version': r['latest_version']} for r in missing]
        self.job.save()
        raise RecoveryError('REGISTRY_VALIDATION: missing module(s) not found in addons-path for ' + target_version +
                            ': ' + ', '.join(r['name'] for r in missing))

    def _technical(self, expected, filestore, database='database', module_mapping=None):
        modules = self._rows('SELECT name, state, latest_version FROM ir_module_module ORDER BY name', database)
        base = [m for m in modules if m['name'] == 'base' and m['state'] == 'installed']
        if len(base) != 1 or base[0]['latest_version'].split('.')[0] != expected.split('.')[0]:
            raise RecoveryError('VERSION_MISMATCH: stored base module is not at expected major')
        if any(m['state'] in ('to install', 'to upgrade', 'to remove') for m in modules):
            raise RecoveryError('MODULE_PENDING: unexpected pending module states')
        installed = {m['name'] for m in modules if m['state'] == 'installed'}
        missing = set(self.module_baseline) - installed
        if missing:
            missing = self._unaccounted_missing_modules(missing, installed, module_mapping or {})
        if missing:
            raise RecoveryError('MODULE_MISSING: installed module(s) disappeared with no upstream rename/merge '
                                'evidence; review protected diagnostics: ' + ', '.join(sorted(missing)))
        if self._sql("SELECT count(*) FROM ir_config_parameter WHERE key='base.partially_updated_database'", database) != '0':
            raise RecoveryError('PARTIAL_DATABASE: registry startup forbidden on partial migration')
        if self._sql('SELECT count(*) FROM pg_constraint WHERE NOT convalidated', database) != '0':
            raise RecoveryError('DATABASE_CONSTRAINT: unvalidated constraints require review')
        rows = self._rows("SELECT store_fname, checksum, file_size FROM ir_attachment WHERE store_fname IS NOT NULL AND store_fname != ''", database)
        attachment_result = verify_attachments(filestore, rows, allow_missing=self.allow_missing_attachments)
        if attachment_result['missing_files']:
            logger.warning('%s referenced attachment files are missing; accepted by --allow-missing-attachments. '
                           'The resulting backup will not contain these files.', attachment_result['missing_files'])
        return {'passed': True, 'version': expected, 'modules': sorted(installed), 'attachments': attachment_result}

    def _uom_preflight(self, target):
        if target != '19.0' or 'uom' not in self.module_baseline:
            return
        rows = self._rows("SELECT u.category_id AS category, u.factor, u.uom_type AS type, EXISTS (SELECT 1 FROM ir_model_data d WHERE d.model='uom.uom' AND d.res_id=u.id AND d.module != '__export__') AS provider_xmlid FROM uom_uom u")
        # No filtering on active: upstream reads inactive rows too.
        validate_uom(rows, factor_fix=True)
        self.uom_rows = rows
        self.uom_baseline = self._rows('SELECT id, category_id, factor FROM uom_uom ORDER BY id')

    def _capacity(self):
        db_bytes = int(self._sql('SELECT pg_database_size(current_database())'))
        fs_bytes = sum(x['size'] for x in inventory(self.filestore_dir).values())
        # Dump+copy+verification+artifact headroom, measured again at every boundary.
        require_space(self.cwd, 3 * db_bytes + 3 * fs_bytes)
        output = self._run_cmd(['docker', 'exec', self.db_container, 'df', '-Pk', self._pg_data_volume_path()], capture_output=True).stdout
        available = int(output.splitlines()[-1].split()[3]) * 1024
        if available < 2 * db_bytes + 256 * 1024**2:
            raise RecoveryError('DISK_SPACE: PostgreSQL volume lacks verification/restore headroom')

    def _single_copy_capacity(self):
        """Keep a modest reserve; no second database or filestore is created."""
        db_bytes = int(self._sql('SELECT pg_database_size(current_database())'))
        reserve = max(2 * 1024**3, db_bytes // 10)
        require_space(self.cwd, reserve)

    def _normal_image(self, major):
        reference = 'odoo:' + major
        self._run_cmd(['docker', 'pull', reference], capture_output=True)
        return self._run_cmd(['docker', 'image', 'inspect', reference, '--format={{.Id}}'], capture_output=True).stdout.strip()

    def _probe(self, database, filestore, image):
        name = 'ou-verify-' + uuid.uuid4().hex
        self.attempt['containers'].append(name)
        self.job.save()
        command = ['docker', 'run', '--rm', '--name', name,
                   '--label', 'odooupgrader.job=' + self.job.id,
                   '--label', 'odooupgrader.attempt=' + self.attempt['id'],
                   '--network', self.network, '--user', f'{os.getuid()}:{os.getgid()}',
                   '--env', 'HOME=/tmp', '--volume', f'{Path(filestore).parents[1]}:/tmp/ou',
                   '--entrypoint', 'odoo', image, '-d', database, '--db_host', self.db_container,
                   '--db_user', 'odoo', '--db_password', 'odoo', '--data-dir=/tmp/ou',
                   '--stop-after-init', '--no-http', '--max-cron-threads=0', '--load=base,web']
        if self.extra_addons:
            command[command.index('--entrypoint'):command.index('--entrypoint')] = ['--volume', self.custom_addons_dir + ':/mnt/custom-addons:ro']
            command += ['--addons-path=/mnt/custom-addons']
        try:
            result = self._run_cmd(command, capture_output=True, timeout=600)
        except BaseException:
            try:
                info = self._inspect_owned(name, self.attempt['id'])
                if info and info['State']['Running']:
                    subprocess.run(['docker', 'stop', info['Id']], check=True, capture_output=True, timeout=30)
            except Exception:
                logger.error('Registry probe could not be quiesced; recovery must reconcile')
            raise
        text = (result.stdout or '') + (result.stderr or '')
        if re.search(r'(Some modules are not loaded|inconsistent states|ERROR|CRITICAL|Running upgrade)', text):
            raise RecoveryError('REGISTRY_VALIDATION: normal registry probe reported errors or migration activity')
        if 'Modules loaded.' not in text:
            raise RecoveryError('REGISTRY_VALIDATION: no successful registry initialization evidence')

    def _checkpoint(self, major, runtime):
        self._stage('CHECKPOINTING')
        self._capacity()
        result = self._technical(major, self.filestore_dir, module_mapping=runtime.get('module_mapping'))
        if major == '19.0' and self.uom_baseline is not None:
            from .checks import verify_uom_ratios
            verify_uom_ratios(self.uom_baseline, self._rows('SELECT id, factor FROM uom_uom ORDER BY id'))
            result['uom_ratios'] = 'passed'
        def build(stage):
            dump = stage / 'database.dump'
            with dump.open('wb') as stream:
                subprocess.run(['docker', 'exec', self.db_container, 'pg_dump', '-Fc', '-U', 'odoo', 'database'],
                               stdout=stream, stderr=subprocess.PIPE, check=True)
            shutil.copytree(self.filestore_dir, stage / 'filestore')
            self._verify_copy(dump, stage / 'filestore', major, runtime)
            return dict(result, restored=True, registry=True)
        return self.job.publish(major, runtime, build)

    def _verify_copy(self, dump, filestore, major, runtime):
        database = 'verify_' + uuid.uuid4().hex
        verify_root = Path(self.cwd) / database
        verify_fs = verify_root / 'filestore' / database
        shutil.copytree(filestore, verify_fs)
        original = None
        try:
            self._restore_dump(dump, database)
            self._technical(major, verify_fs, database, module_mapping=runtime.get('module_mapping'))
            self._probe(database, verify_fs, runtime['image_id'])
            self._technical(major, verify_fs, database, module_mapping=runtime.get('module_mapping'))
        except BaseException as exc:
            original = exc
            raise
        finally:
            try:
                self._run_cmd(['docker', 'exec', self.db_container, 'dropdb', '-U', 'odoo', '--if-exists', database], capture_output=True)
                shutil.rmtree(verify_root)
            except Exception:
                if original is None:
                    raise
                logger.error('Verification cleanup failed; original verification error preserved')

    def _runtime_guard(self, target, runtime):
        """Reject rebuilt image/dependency drift even when Git pins did not change."""
        locks = self.job.data.setdefault('runtime_locks', {})
        previous = locks.get(target)
        if previous is None:
            locks[target] = runtime
            self.job.save()
            return
        if previous == runtime:
            return
        diff = {'target': target, 'before': previous, 'after': runtime}
        fingerprint = hashlib.sha256(json.dumps(diff, sort_keys=True).encode()).hexdigest()
        candidates = self.job.data.setdefault('runtime_candidates', {})
        candidates[target] = {'diff': diff, 'accept_sha256': fingerprint}
        if self.acceptance != fingerprint or not self.reason:
            self.job.save()
            raise RecoveryError('DEPENDENCY_DRIFT: review built_runtime_candidates in dry-run; acceptance SHA256=' + fingerprint)
        self.job.data['remediations'].append({'change_sha256': fingerprint,
            'reason_sha256': hashlib.sha256(self.reason.encode()).hexdigest(), 'time': time.time()})
        locks[target] = runtime
        del candidates[target]
        self.job.save()

    def _pip_check_violations(self, image_ref):
        """Return sorted, deduplicated `pip check` broken-requirement lines for an image.

        Metadata WARNING lines (e.g. malformed dist-info shipped by upstream apt packages)
        are informational, not a dependency conflict, and are excluded.
        """
        result = self._run_cmd(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3',
                                image_ref, '-m', 'pip', 'check'], check=False, capture_output=True)
        if result.returncode not in (0, 1):
            raise RecoveryError(f'DEPENDENCY_PROBE_FAILED: could not run pip check in {image_ref} '
                                f'(exit {result.returncode}): {redact((result.stderr or "")[-4096:])}')
        lines = (result.stdout or '').splitlines() + (result.stderr or '').splitlines()
        return sorted({line.strip() for line in lines if line.strip() and not line.startswith('WARNING:')})

    def _dependency_conflicts(self, base_image, built_image_id):
        """Diff `pip check` violations between the untouched base image and the built one.

        Odoo's official base images ship with pre-existing, upstream-caused dependency
        mismatches (e.g. maxminddb/setuptools) unrelated to OpenUpgrade or custom addons.
        Only conflicts newly introduced by this build's own installs are fatal; conflicts
        already present in the pristine base image are reported but never block migration.
        """
        baseline = self._pip_check_violations(base_image)
        final = self._pip_check_violations(built_image_id)
        new_conflicts = [line for line in final if line not in baseline]
        preexisting = [line for line in final if line in baseline]
        return new_conflicts, preexisting

    def _prepare_migration_image(self, target_version):
        """Build and probe a pinned image without touching the working database."""
        revision = self.revisions.get(target_version)
        if not revision:
            raise RecoveryError('RUNTIME_REVISION: supply --openupgrade-revision for this older transition')
        fingerprint = self._runtime_plan()['application_fingerprint']
        addons_sha256 = self.job.data['config'].get('addons_sha256')
        cached = self.attempt.setdefault('prebuilt_images', {}).get(target_version)
        if cached and (cached['revision'] == revision and cached['application_fingerprint'] == fingerprint
                       and cached['addons_sha256'] == addons_sha256):
            result = self._run_cmd(['docker', 'image', 'inspect', cached['image_id'], '--format={{.Id}}'],
                                   check=False, capture_output=True)
            if result.returncode == 0 and result.stdout.strip() == cached['image_id']:
                return cached
        build = Path(self.cwd) / 'build' / target_version
        if build.exists():
            shutil.rmtree(build)
        build.mkdir(parents=True)
        custom = ''
        if self.extra_addons:
            shutil.copytree(self.custom_addons_dir, build / 'custom_addons')
            custom = ('COPY --chown=odoo:odoo custom_addons /mnt/custom-addons\n'
                      'RUN if pip3 install --help | grep -q -- --break-system-packages; then PIP_BREAK_SYSTEM_PACKAGES=1 pip3 install --no-cache-dir -r /mnt/custom-addons/requirements.txt; else pip3 install --no-cache-dir -r /mnt/custom-addons/requirements.txt; fi\n')
        safe_git = 'git -c safe.directory=/mnt/extra-addons -C /mnt/extra-addons'
        recipe = f'''FROM odoo:{target_version}
USER root
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git init /mnt/extra-addons && {safe_git} remote add origin https://github.com/OCA/OpenUpgrade.git && {safe_git} fetch --depth 1 origin {revision} && {safe_git} checkout --detach FETCH_HEAD && test "$({safe_git} rev-parse HEAD)" = "{revision}"
RUN if pip3 install --help | grep -q -- --break-system-packages; then PIP_BREAK_SYSTEM_PACKAGES=1 pip3 install --no-cache-dir -r /mnt/extra-addons/requirements.txt && PIP_BREAK_SYSTEM_PACKAGES=1 pip3 install --no-deps --force-reinstall "openupgradelib @ git+https://github.com/OCA/openupgradelib.git@{LIBRARY_REVISION}"; else pip3 install --no-cache-dir -r /mnt/extra-addons/requirements.txt && pip3 install --no-deps --force-reinstall "openupgradelib @ git+https://github.com/OCA/openupgradelib.git@{LIBRARY_REVISION}"; fi
{custom}
USER odoo
'''
        (build / 'Dockerfile').write_text(recipe)
        name = 'ou-migrate-' + self.attempt['id'].replace('-', '') + '-' + target_version.replace('.', '-')
        image = name + ':local'
        self._stage('BUILDING_' + target_version)
        self._run_cmd(['docker', 'build', '-t', image, str(build)], capture_output=True)
        image_id = self._run_cmd(['docker', 'image', 'inspect', image, '--format={{.Id}}'], capture_output=True).stdout.strip()
        # Odoo 19's official image packages `odoo` as a namespace package (no
        # __init__.py), so a bare `import odoo` never populates odoo.fields/api the
        # way 17/18's regular package does. openupgradelib's top-level code reads
        # core.fields/core.api at import time and crashes with a bare import. Real
        # Odoo startup always imports the `base` addon (every module depends on it)
        # before loading openupgrade_framework, which populates these attributes as
        # a side effect; importing it here first makes this probe reflect that same
        # order instead of a standalone-script artifact.
        probe = "import odoo.addons.base; import json,importlib,importlib.metadata as m,sys; from openupgradelib import openupgrade; "
        if target_version == '18.0':
            probe += "assert callable(openupgrade.add_inherits_parent_xmlids); "
        probe += f"assert json.loads(m.distribution('openupgradelib').read_text('direct_url.json'))['vcs_info']['commit_id'] == '{LIBRARY_REVISION}'; "
        # OCA's own apriori.py is the upstream-maintained module rename/merge registry
        # (e.g. account_payment_term -> account); absence/import failure degrades to an
        # empty mapping rather than blocking the build, since it is evidence, not a gate.
        probe += ("sys.path.insert(0, '/mnt/extra-addons')\n"
                  "try:\n"
                  "    _apriori = importlib.import_module('openupgrade_scripts.apriori')\n"
                  "    module_mapping = dict(getattr(_apriori, 'renamed_modules', {}))\n"
                  "    module_mapping.update(getattr(_apriori, 'merged_modules', {}))\n"
                  "except Exception:\n"
                  "    module_mapping = {}\n")
        probe += ("packages=[(d.metadata.get('Name') or '<unnamed>', d.metadata.get('Version') or '<unknown-version>') for d in m.distributions()]; "
                  "print(json.dumps({'python':__import__('platform').python_version(),'dependencies':sorted(packages),"
                  "'incomplete_distribution_count':sum(name == '<unnamed>' or version == '<unknown-version>' for name,version in packages),"
                  "'library':json.loads(m.distribution('openupgradelib').read_text('direct_url.json')).get('vcs_info'),"
                  "'module_mapping':module_mapping}))")
        env = json.loads(self._run_cmd(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3', image_id, '-c', probe], capture_output=True).stdout)
        if env['incomplete_distribution_count']:
            logger.warning('%s Python distributions lack Name/Version metadata; placeholders recorded in runtime inventory',
                           env['incomplete_distribution_count'])
        new_conflicts, preexisting_conflicts = self._dependency_conflicts('odoo:' + target_version, image_id)
        if new_conflicts:
            raise RecoveryError('DEPENDENCY_ERROR: OpenUpgrade/custom-addon requirements introduced new package '
                                'conflicts not present in the base odoo:' + target_version + ' image: ' + '; '.join(new_conflicts))
        if preexisting_conflicts:
            logger.warning('Ignoring %d dependency conflict(s) already present in the unmodified base odoo:%s image '
                           '(not introduced by this build): %s', len(preexisting_conflicts), target_version,
                           '; '.join(preexisting_conflicts))
        prepared = {'revision': revision, 'image_id': image_id, 'environment': env,
                    'application_fingerprint': fingerprint, 'addons_sha256': addons_sha256,
                    'preexisting_dependency_conflicts': preexisting_conflicts}
        self.attempt['prebuilt_images'][target_version] = prepared
        self.job.save()
        return prepared

    LOADING_MODULE_RE = re.compile(r"Loading module ([\w.]+) \((\d+)/(\d+)\)")
    REGISTRY_LOADED_RE = re.compile(r"Registry loaded in ([\d.]+)s")

    @classmethod
    def _parse_progress_line(cls, line):
        match = cls.LOADING_MODULE_RE.search(line)
        if match:
            return {'name': match.group(1), 'i': int(match.group(2)), 'n': int(match.group(3))}
        if 'Modules loaded.' in line:
            return {'stage': 'Modules loaded.'}
        match = cls.REGISTRY_LOADED_RE.search(line)
        if match:
            return {'stage': 'Registry loaded in ' + match.group(1) + 's'}
        return None

    @staticmethod
    def _heartbeat_due(now, last_output, last_warned, threshold=120):
        return now - last_output >= threshold and now - last_warned >= threshold

    @staticmethod
    def _format_duration(seconds):
        seconds = max(0, int(seconds))
        return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'

    def _progress_text(self, transition, progress, elapsed, since_output):
        if progress.get('n'):
            percent = round(progress['i'] * 100 / progress['n'])
            detail = f"Module {progress['i']}/{progress['n']} '{progress['name']}' ({percent}%)"
        else:
            detail = progress.get('stage', 'Starting...')
        return (f'[{transition}] {detail} | elapsed {self._format_duration(elapsed)} '
                f'| last output {int(since_output)}s ago')

    def _container_health(self, name):
        try:
            info = self._inspect_owned(name, self.attempt['id'])
            state = info['State']['Status'] if info else 'unknown'
        except Exception:
            state = 'unknown'
        try:
            cpu = self._run_cmd(['docker', 'stats', '--no-stream', '--format', '{{.CPUPerc}}', name],
                                check=False, capture_output=True, timeout=10).stdout.strip() or 'unknown'
        except Exception:
            cpu = 'unknown'
        return state, cpu

    def _run_upgrade_container(self, command, name, target_version):
        """Follow the migration container's output live; never kill it on silence."""
        from .core import console
        transition = self.attempt.get('current_transition', target_version)
        tty = sys.stdout.isatty()
        start = last_output = last_report = last_warned = time.monotonic()
        progress = {}
        with (Path(self.cwd) / 'docker.log').open('a') as stream:
            process = subprocess.Popen(command, cwd=self.cwd, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
            try:
                while True:
                    ready, _, _ = select.select([process.stdout], [], [], 1.0)
                    now = time.monotonic()
                    if ready:
                        line = process.stdout.readline()
                        if line == '' and process.poll() is not None:
                            break
                        if line:
                            stream.write(line)
                            stream.flush()
                            last_output = now
                            update = self._parse_progress_line(line)
                            if update:
                                progress.update(update)
                    elif process.poll() is not None:
                        break
                    if self._heartbeat_due(now, last_output, last_warned):
                        state, cpu = self._container_health(name)
                        message = f'No output for {int(now - last_output)}s; container state={state} cpu={cpu}'
                        logger.warning(message)
                        console.print('[yellow]' + message + '[/yellow]')
                        last_warned = now
                    text = self._progress_text(transition, progress, now - start, now - last_output)
                    if tty:
                        sys.stdout.write('\r' + text.ljust(max(len(text), 100)))
                        sys.stdout.flush()
                    if now - last_report >= 30:
                        logger.info(text)
                        last_report = now
                return_code = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        if tty:
            sys.stdout.write('\n')
        return return_code

    def run_upgrade_step(self, target_version):
        self._uom_preflight(target_version)
        prepared = self._prepare_migration_image(target_version)
        image_id = prepared['image_id']
        self._missing_module_preflight(target_version, prepared)
        name = 'ou-migrate-' + self.attempt['id'].replace('-', '') + '-' + target_version.replace('.', '-')
        self.current_runtime = {'openupgrade': prepared['revision'], 'openupgradelib': LIBRARY_REVISION,
            'image_id': image_id, 'odoo_revision': None, 'odoo_revision_note': 'Official image has no verified Git provenance',
            'application_fingerprint': prepared['application_fingerprint'],
            'postgres': self._sql('SHOW server_version'), 'environment': prepared['environment']}
        # OCA's own apriori registry for this exact pinned revision; used only to recognize
        # an upstream-documented rename/merge, never to invent or guess a mapping.
        self.current_runtime['module_mapping'] = prepared['environment'].get('module_mapping', {})
        self.current_runtime['addons_sha256'] = self.job.data['config'].get('addons_sha256')
        self.current_runtime['application_commit'] = self._runtime_plan()['application_commit']
        self.attempt['runtime'][target_version] = self.current_runtime
        self.job.save()
        if target_version == '19.0' and self.uom_rows is not None:
            script_hash = self._run_cmd(['docker', 'run', '--rm', '--network', 'none',
                '--entrypoint', 'sha256sum', image_id,
                '/mnt/extra-addons/openupgrade_scripts/scripts/uom/19.0.1.0/post-migration.py'], capture_output=True).stdout.split()[0]
            self.current_runtime['uom_script_sha256'] = script_hash
            validate_uom(self.uom_rows, factor_fix=script_hash == FACTOR_FIX_SCRIPT_SHA256)
            self.job.save()
        self._runtime_guard(target_version, self.current_runtime)
        self._stage('MIGRATING_' + target_version)
        if name not in self.attempt['containers']:
            self.attempt['containers'].append(name)
        # Persist the write boundary before Docker can start Odoo. Any failure
        # after this point must restore a checkpoint, never reuse this database.
        self.attempt['migration_started'] = True
        self.job.save()
        command = ['docker', 'run', '--name', name, '--label', 'odooupgrader.job=' + self.job.id,
            '--label', 'odooupgrader.attempt=' + self.attempt['id'], '--network', self.network,
            '--user', f'{os.getuid()}:{os.getgid()}', '--env', 'HOME=/tmp',
            '--volume', self.data_dir + ':/tmp/ou',
            '--entrypoint', 'odoo', image_id, '-d', 'database', '--db_host', self.db_container,
            '--db_user', 'odoo', '--db_password', 'odoo', '--data-dir=/tmp/ou',
            '--upgrade-path=/mnt/extra-addons/openupgrade_scripts/scripts',
            '--addons-path=/mnt/extra-addons' + (',/mnt/custom-addons' if self.extra_addons else ''),
            '--update=all', '--stop-after-init', '--no-http', '--max-cron-threads=0',
            '--load=base,web,openupgrade_framework', '--log-level=info']
        return_code = self._run_upgrade_container(command, name, target_version)
        state = self._inspect_owned(name, self.attempt['id'])['State']
        if return_code or state['ExitCode'] or state.get('OOMKilled') or state.get('Running', True):
            raise RecoveryError('MIGRATION_EXIT: migration container failed; inspect diagnostics bundle')
        return True

    def finalize_package(self):
        self._stage('FINALIZING')
        checkpoint, manifest = self.job.checkpoint()
        # The authoritative checkpoint has already been fully restore/registry tested.
        self._technical(manifest['version'], self.filestore_dir)
        require_space(self.cwd, sum(x['size'] for x in manifest['files'].values()))
        dump = Path(self.output_dir) / 'dump.sql'
        temporary = Path(self.output_dir) / 'upgraded.zip.part'
        destination = Path(self.output_dir) / 'upgraded.zip'
        with dump.open('wb') as stream:
            subprocess.run(['docker', 'exec', self.db_container, 'pg_dump', '-U', 'odoo', 'database'], stdout=stream, stderr=subprocess.PIPE, check=True)
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(dump, 'dump.sql')
            for path in Path(self.filestore_dir).rglob('*'):
                if path.is_file():
                    archive.write(path, 'filestore/' + path.relative_to(self.filestore_dir).as_posix())
            archive.writestr('odooupgrader-checkpoint.json', json.dumps(manifest))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RecoveryError('ARTIFACT_INVALID: final ZIP integrity failed')
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        sync_dir(destination.parent)
        self.job.data['artifact'] = {'path': str(destination), 'sha256': digest(destination)}
        dump.unlink()
        return destination

    def _write_compact_checkpoint_zip(self, destination, version, validation):
        """Stream the working DB and filestore to one immutable checkpoint ZIP."""
        files = inventory(self.filestore_dir)  # Reject links and special files before archiving.
        self._last_filestore_fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        def copy_with_reserve(source, target):
            while block := source.read(1024 * 1024):
                require_space(self.cwd, len(block))
                target.write(block)
        metadata = {'job_id': self.job.id, 'version': version,
                    'source_sha256': self.job.data.get('input_sha256'),
                    'storage_mode': 'single_copy', 'validation': dict(validation, registry=True,
                    independent_restore=False), 'runtime': self.current_runtime}
        command = ['docker', 'exec', self.db_container, 'pg_dump', '-U', 'odoo', 'database']
        private_log = Path(self.cwd) / 'export.private.log'
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            with private_log.open('wb') as errors:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
                try:
                    with archive.open('dump.sql', 'w', force_zip64=True) as stream:
                        copy_with_reserve(process.stdout, stream)
                    process.stdout.close()
                    if process.wait() != 0:
                        raise RecoveryError('EXPORT_FAILED: pg_dump failed; inspect protected export.private.log')
                except BaseException:
                    if process.poll() is None:
                        process.kill()
                    process.stdout.close()
                    process.wait()
                    raise
            for name in files:
                with (Path(self.filestore_dir) / name).open('rb') as source:
                    with archive.open('filestore/' + name, 'w', force_zip64=True) as target:
                        copy_with_reserve(source, target)
            archive.writestr('odooupgrader-run.json', json.dumps(metadata, sort_keys=True))
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise RecoveryError('CHECKPOINT_INVALID: ZIP integrity failed')
        destination.chmod(0o400)

    def _single_copy_checkpoint(self, version):
        """Publish a ZIP after a successful transition without duplicating live data."""
        self._stage('CHECKPOINTING')
        self._single_copy_capacity()
        self._probe('database', self.filestore_dir, self.current_runtime['image_id'])
        validation = self._technical(version, self.filestore_dir, module_mapping=self.current_runtime.get('module_mapping'))
        if version == '19.0' and self.uom_baseline is not None:
            from .checks import verify_uom_ratios
            verify_uom_ratios(self.uom_baseline, self._rows('SELECT id, factor FROM uom_uom ORDER BY id'))
            validation['uom_ratios'] = 'passed'
        validation.update(registry=True, independent_restore=False,
                          checkpoint_format='zip')
        def build(stage):
            self._write_compact_checkpoint_zip(stage / 'checkpoint.zip', version, validation)
            return validation
        self.job.publish(version, self.current_runtime, build)
        return validation

    def finalize_single_copy_package(self, version):
        """Expose the final checkpoint ZIP as the output without copying bytes."""
        self._stage('FINALIZING')
        checkpoint, manifest = self.job.checkpoint()
        if manifest['version'] != version or manifest.get('checkpoint_format') != 'zip':
            raise RecoveryError('CHECKPOINT_INVALID: final ZIP does not match the target version')
        source = checkpoint / 'checkpoint.zip'
        destination = Path(self.output_dir) / 'upgraded.zip'
        if destination.exists():
            if (destination.is_symlink() or destination.stat().st_ino != source.stat().st_ino
                    or destination.stat().st_dev != source.stat().st_dev):
                raise RecoveryError('ARTIFACT_INVALID: existing output is not the completed checkpoint')
        else:
            os.link(source, destination)
        sync_dir(destination.parent)
        self.job.data['artifact'] = {'path': str(destination), 'sha256': manifest['files']['checkpoint.zip']['sha256'],
                                     'independent_restore_verified': False}
        return destination

    def failure_evidence(self):
        evidence = {}
        if self._database_restored and not self._restore_failed:
            try:
                evidence['module_status'] = self._rows('SELECT name, state, latest_version FROM ir_module_module ORDER BY name', timeout=5)
                evidence['database_diagnostics'] = {'available': True, 'postgres': self._sql('SHOW server_version', timeout=5)}
            except Exception:
                evidence['database_diagnostics'] = {'available': False}
        else:
            evidence['module_status'] = {'available': False, 'reason': 'DATABASE_RESTORE_FAILURE' if self._restore_failed else 'DATABASE_NOT_RESTORED'}
            evidence['database_diagnostics'] = {'available': bool(self.pg_runtime), 'postgres_runtime': self.pg_runtime, 'restore': self.attempt.get('restore')}
        states = []
        for name in self.attempt.get('containers', []):
            try:
                info = self._inspect_owned(name, self.attempt['id'])
                if info:
                    states.append({key: info['State'].get(key) for key in ('Status', 'Running', 'ExitCode', 'OOMKilled', 'StartedAt', 'FinishedAt')})
            except Exception:
                states.append({'available': False})
        evidence['docker_status'] = states
        return evidence

    def run(self):
        from .core import console
        if self.target_version not in self.VALID_VERSIONS or (self.postgres_version != 'auto' and not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', self.postgres_version)):
            raise RecoveryError('VERSION: invalid Odoo or PostgreSQL version')
        resume = self.job is not None
        if not resume:
            self.job = Job.create(self.jobs_dir, self.target_version, self.postgres_version,
                {'runtime_plan': self._runtime_plan(), 'extra_addons': bool(self.extra_addons),
                 'allow_missing_attachments': self.allow_missing_attachments,
                 'single_copy': self.single_copy, 'source_version': self.source_version_hint})
        console.print('Job ID: ' + self.job.id)
        if self.single_copy:
            console.print('Storage mode: one working database/filestore; ZIP checkpoint after each successful version')
        with self.job.lock():
            if self.job.data['state'] == 'COMPLETED':
                return self.job.data['artifact']['path']
            previous_attempts = list(self.job.data['attempts'])
            reusable = self._reusable_workspace(allow_interrupted_running=True) if resume else None
            if resume and not reusable and not self.job.data['latest_checkpoint']:
                raise RecoveryError('NO_CHECKPOINT: no verified checkpoint or reusable pre-migration workspace')
            if reusable:
                self.attempt = reusable
                previous_attempts.pop()
                self.attempt['outcome'] = 'RUNNING'
                self.attempt['ended'] = None
                self.attempt['reuse_count'] = self.attempt.get('reuse_count', 0) + 1
                self.job.data.update(state='RUNNING', stage='PREPARING', artifact=None)
                self.job.save()
                self._set_attempt_paths(self.attempt)
            else:
                self.attempt = self._new_attempt()
            console.print('Migration log: ' + str(Path(self.cwd) / 'migration.log'))
            self.attempt['runtime']['plan'] = self._runtime_plan()
            try:
                snapshot = self.job.checkpoint() if resume and self.job.data['latest_checkpoint'] else None
                self._plan_change(commit=True)
                self.assert_local_docker()
                for previous in previous_attempts:
                    self._stop_attempt(previous)
                self.prepare_environment()
                if reusable:
                    self._stop_attempt(self.attempt)
                    addon = self.job.path / 'inputs' / 'custom_addons'
                    if self.job.data['config'].get('extra_addons'):
                        if not addon.is_dir():
                            raise RecoveryError('ADDONS_CHANGED: retained custom addons missing')
                        saved_addons = inventory(addon)
                        saved_hash = hashlib.sha256(json.dumps(saved_addons, sort_keys=True).encode()).hexdigest()
                        if (saved_addons != inventory(self.custom_addons_dir) or
                                saved_hash != self.job.data['config'].get('addons_sha256')):
                            raise RecoveryError('ADDONS_CHANGED: retained custom addons changed')
                        self.extra_addons = str(addon)
                    elif self.extra_addons:
                        # First time this job is given extra addons; adopt and retain them like a fresh job would.
                        self.process_extra_addons()
                        shutil.copytree(self.custom_addons_dir, addon)
                        self.job.data['config'].update(extra_addons=True, addons_sha256=hashlib.sha256(
                            json.dumps(inventory(self.custom_addons_dir), sort_keys=True).encode()).hexdigest())
                        self.job.save()
                    current = self._reuse_workspace()
                else:
                    if self.single_copy and snapshot and snapshot[1]['version'] == self.target_version:
                        artifact = self.finalize_single_copy_package(self.target_version)
                        self.attempt.update(outcome='SUCCEEDED', ended=time.time())
                        self.job.data.update(state='COMPLETED', stage='COMPLETED')
                        self.job.save()
                        console.print('Upgrade complete: ' + str(artifact))
                        console.print('Independent restore verification was not run for this ZIP checkpoint.')
                        return str(artifact)
                    self._stage('INPUT_PREPARATION')
                    addon = self.job.path / 'inputs' / 'custom_addons'
                    if resume and addon.exists():
                        self.extra_addons = str(addon)
                        current_hash = hashlib.sha256(json.dumps(inventory(addon), sort_keys=True).encode()).hexdigest()
                        if current_hash != self.job.data['config'].get('addons_sha256'):
                            raise RecoveryError('ADDONS_CHANGED: retained source bundle is corrupt or modified')
                    elif not resume:
                        self.validate_source_accessibility()
                    # else: resuming a job that never had extra addons; a newly supplied
                    # self.extra_addons (if any) is adopted below, same as a fresh job.
                    self.process_extra_addons()
                    if self.extra_addons and not addon.exists():
                        shutil.copytree(self.custom_addons_dir, addon)
                        self.job.data['config'].update(extra_addons=True, addons_sha256=hashlib.sha256(
                            json.dumps(inventory(self.custom_addons_dir), sort_keys=True).encode()).hexdigest())
                        self.job.save()
                    preflight_version = snapshot[1]['version'] if snapshot else self.source_version_hint
                    source_image_id = self._normal_image(preflight_version) if preflight_version and not snapshot else None
                    if preflight_version and int(preflight_version.split('.')[0]) < int(self.target_version.split('.')[0]):
                        self._prepare_migration_image(self.generate_next_version(preflight_version))
                    self._prepare_restore_input(snapshot)
                    self.create_db_compose_file()
                    self._run_cmd(self.compose_cmd + ['-f', 'db-composer.yml', 'up', '-d'], capture_output=True)
                    self.wait_for_db()
                    self._stage('RESTORING')
                    if snapshot:
                        directory, manifest = snapshot
                        self.module_baseline = manifest['validation']['modules']
                        if manifest.get('checkpoint_format') == 'zip':
                            self.restore_database('ZIP')
                        else:
                            self._restore_dump(directory / 'database.dump', 'database')
                            shutil.copytree(directory / 'filestore', self.filestore_dir, dirs_exist_ok=True)
                        current = manifest['version']
                        self._technical(current, self.filestore_dir)
                        if manifest.get('checkpoint_format') == 'zip':
                            self._probe('database', self.filestore_dir, manifest['runtime']['image_id'])
                        else:
                            # Verify normal startup only on a disposable snapshot copy, never on
                            # the actual attempt DB that the next migration will consume.
                            self._verify_copy(directory / 'database.dump', directory / 'filestore', current, manifest['runtime'])
                        self.current_runtime = manifest['runtime']
                    else:
                        self.restore_database(self.restore_file_type)
                        current = self.get_current_version()
                        if not re.fullmatch(r'\d+\.\d+(?:\.[\w.-]+)*', current):
                            raise RecoveryError('SOURCE_VERSION: could not establish installed base version')
                        current = current.split('.')[0] + '.0'
                        if current not in self.VALID_VERSIONS:
                            raise RecoveryError('SOURCE_VERSION: unsupported source major')
                        if int(current.split('.')[0]) > int(self.target_version.split('.')[0]):
                            raise RecoveryError('DOWNGRADE: source is newer than requested target')
                        if self.source_version_hint and current != self.source_version_hint:
                            raise RecoveryError('SOURCE_VERSION: restored source does not match --source-version')
                        self.job.data['source_version'] = current
                        self.module_baseline = self._technical(current, self.filestore_dir)['modules']
                        self.current_runtime = {'image_id': source_image_id or self._normal_image(current), 'openupgrade': None,
                            'openupgradelib': None, 'odoo_revision': None,
                            'application_fingerprint': self._runtime_plan()['application_fingerprint'],
                            'postgres': self._sql('SHOW server_version')}
                        if not self.single_copy:
                            self._checkpoint(current, self.current_runtime)
                    if self.single_copy:
                        self._mark_workspace_ready(current)
                while int(current.split('.')[0]) < int(self.target_version.split('.')[0]):
                    target = self.generate_next_version(current)
                    transition = {'source': current, 'target': target, 'outcome': 'RUNNING'}
                    self.attempt['transitions'].append(transition)
                    self.attempt['current_transition'] = current + ' -> ' + target
                    self._stage('PREFLIGHT_' + target)
                    if self.single_copy:
                        self._single_copy_capacity()
                    else:
                        self._capacity()
                    self.run_upgrade_step(target)
                    if self.single_copy:
                        validation = self._single_copy_checkpoint(target)
                        self.module_baseline = validation['modules']
                        self._mark_workspace_ready(target, self._last_filestore_fingerprint)
                    else:
                        self._checkpoint(target, self.current_runtime)
                        self.module_baseline = self.job.checkpoint()[1]['validation']['modules']
                    transition['outcome'] = 'SUCCEEDED'
                    current = target
                    self.job.save()
                if self.single_copy and self.job.data['latest_checkpoint'] is None:
                    self._single_copy_checkpoint(current)
                artifact = self.finalize_single_copy_package(current) if self.single_copy else self.finalize_package()
                self.attempt.update(outcome='SUCCEEDED', ended=time.time())
                self.job.data.update(state='COMPLETED', stage='COMPLETED')
                self.job.save()
                console.print('Upgrade complete: ' + str(artifact))
                if self.single_copy:
                    console.print('Independent restore verification was not run for this ZIP checkpoint.')
                return str(artifact)
            except BaseException as exc:
                self.attempt.update(outcome='INTERRUPTED' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'FAILED', ended=time.time())
                if self.attempt['transitions'] and self.attempt['transitions'][-1]['outcome'] == 'RUNNING':
                    self.attempt['transitions'][-1]['outcome'] = self.attempt['outcome']
                self.job.data['state'] = self.attempt['outcome']
                self.job.data['artifact'] = None
                try:
                    self.job.save()
                except Exception:
                    logger.error('Could not persist failure; next invocation must reconcile interrupted attempt')
                try:
                    raw_trace = Path(self.cwd) / 'failure.private.log'
                    raw_trace.write_text(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                    raw_trace.chmod(0o600)
                except Exception:
                    logger.error('Private traceback could not be saved')
                try:
                    bundle = collect(self.job, exc, self.failure_evidence())
                    console.print('Diagnostics bundle: ' + str(bundle))
                except Exception:
                    logger.error('Diagnostics collection failed; original failure preserved')
                raise
            finally:
                try:
                    self._stop_attempt(self.attempt)
                except Exception as exc:
                    logger.error('Owned resources could not be stopped; resume must reconcile before restore (%s: %s)',
                                 type(exc).__name__, exc)
                if self._handler:
                    logger.removeHandler(self._handler)
                    self._handler.close()
