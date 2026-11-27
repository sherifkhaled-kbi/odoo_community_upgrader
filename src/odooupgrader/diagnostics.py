"""Portable allowlisted failure evidence. Raw customer logs never leave the job."""
import json
import logging
import os
from pathlib import Path
import platform
import re
import traceback
import zipfile
from .jobs import RecoveryError, atomic_json


def redact(value):
    text = str(value)
    text = re.sub(r'https?://\S+|postgres(?:ql)?://\S+', '[URL REDACTED]', text, flags=re.I)
    text = re.sub(r'(?i)(Bearer\s+)\S+', r'\1[REDACTED]', text)
    pattern = r"""(?i)((?:["']?(?:password|passwd|pwd|token|secret|api[_-]?key|authorization)["']?)\s*(?:[:=]\s*|\s+))(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
    return re.sub(pattern, r'\1[REDACTED]', text)


class RedactionFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def structural_log(text):
    """Do not guess whether arbitrary SQL, exception payloads or lines contain PII."""
    lines = []
    for line in text.splitlines():
        match = re.search(r'\bmodule ([a-zA-Z_][a-zA-Z_0-9]*): (Running upgrade|loading|creating|updating)', line)
        if match:
            lines.append(match.group(0))
        elif re.search(r'\b(?:Traceback|[A-Za-z_][A-Za-z_0-9]*(?:Error|Exception))\b', line):
            match = re.search(r'\b([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception))\b', line)
            lines.append(match.group(1) + ': [payload withheld]' if match else 'Traceback (payload withheld)')
        elif re.match(r'\s*File "', line):
            match = re.match(r'\s*File "([^"]+)", line (\d+), in ([\w<>]+)', line)
            if match:
                lines.append(f'File "{Path(match[1]).name}", line {match[2]}, in {match[3]}')
    return '\n'.join(lines[-10000:])


def safe_trace(exc):
    result = []
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        result.append('Traceback (all frame locations; source literals and payloads withheld):')
        for frame in traceback.extract_tb(current.__traceback__):
            result.append(f'  File "{Path(frame.filename).name}", line {frame.lineno}, in {frame.name}')
        result.append(type(current).__name__ + ': [payload withheld; inspect protected local log]')
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return '\n'.join(result)


def collect(job, exc, evidence=None):
    """Best effort is enforced by the caller; never exports arbitrary state or raw logs."""
    attempt = job.data['attempts'][-1]
    directory = job.path / 'diagnostics' / attempt['id']
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = {key: job.data.get(key) for key in
                ('job_id', 'source_version', 'target', 'state', 'stage', 'latest_checkpoint', 'completed')}
    atomic_json(directory / 'job_metadata.json', metadata)
    message = str(exc).split(':', 1)[0]
    allowed_codes = {
        'ADDONS_CHANGED', 'ARTIFACT_INVALID', 'ARTIFACT_PUBLISH_CONFLICT', 'CHECKPOINT_INVALID', 'COMPLETED_RUNTIME',
        'DATABASE_CONSTRAINT', 'DATABASE_RESTORE_FAILURE', 'DEPENDENCY_DRIFT', 'DISK_SPACE', 'DOCKER_INSPECT', 'DOWNGRADE',
        'FILESTORE_CORRUPT', 'FILESTORE_MISSING', 'FILESTORE_UNSAFE', 'INPUT_EXISTS', 'INPUT_LIMIT',
        'INPUT_PREPARATION', 'INPUT_UNSAFE', 'JOB_BUSY', 'JOB_CONFIGURATION', 'JOB_ID', 'JOB_METADATA',
        'LOCAL_DOCKER_REQUIRED', 'LOCAL_LINUX_REQUIRED', 'MIGRATION_EXIT', 'MODULE_MISSING',
        'MODULE_PENDING', 'MODULE_SCAN_FAILED', 'NO_CHECKPOINT', 'PARTIAL_DATABASE', 'POSTGRES_STARTUP', 'REGISTRY_VALIDATION',
        'RESOURCE_OWNER', 'RESTORE_INPUT', 'RUNTIME_CHANGED', 'RUNTIME_REVISION', 'SOURCE_VERSION',
        'UOM_EQUIVALENCE', 'UOM_FACTOR', 'UOM_REFERENCE', 'UOM_UPSTREAM_FACTOR', 'VALIDATION_FAILED',
        'VERSION', 'VERSION_MISMATCH'}
    code = message if isinstance(exc, RecoveryError) and message in allowed_codes else 'UNKNOWN_FAILURE'
    summary = {'job_id': job.id, 'attempt_id': attempt['id'], 'stage': job.data['stage'],
               'error_type': type(exc).__name__, 'code': code,
               'message': 'Review structural traceback and protected local logs; payload withheld for privacy.',
               'transition': attempt.get('current_transition'), 'root_cause': 'Not established by bundle generation'}
    error_text = getattr(exc, 'stderr', '') or ''
    sqlstate = re.search(r'ERROR:\s+([0-9A-Z]{5}):', error_text)
    summary['sqlstate'] = sqlstate[1] if sqlstate else None
    summary['process_exit_code'] = getattr(exc, 'returncode', None)
    restore = attempt.get('restore', {})
    if code == 'DATABASE_RESTORE_FAILURE':
        summary.update(classification=code, restore_reason=restore.get('reason'),
                       dump_format=(restore.get('dump') or {}).get('format'),
                       postgres_runtime=restore.get('runtime'),
                       failing_statement=restore.get('failing_statement'), incompatible_statement=restore.get('incompatible_statement'),
                       relevant_error=restore.get('relevant_error'),
                       sqlstate=restore.get('sqlstate'), process_exit_code=restore.get('process_exit_code'),
                       category=restore.get('category'), resource_condition=restore.get('resource_condition'),
                       recommended_action=restore.get('recommended_action'),
                       storage_before_restore=restore.get('storage_before_restore'),
                       storage_after_failure=restore.get('storage_after_failure'))
        if restore.get('category') == 'RESOURCE_EXHAUSTION':
            summary['message'] = restore.get('relevant_error') or 'Restore capacity check failed; inspect storage evidence'
            summary['root_cause'] = restore.get('relevant_error') or 'Insufficient measured restore filesystem capacity'
    summary['failing_module'] = None
    summary['missing_modules'] = attempt.get('missing_modules')
    docker_log = Path(attempt['work']) / 'docker.log'
    if code != 'DATABASE_RESTORE_FAILURE' and docker_log.is_file():
        with docker_log.open('rb') as stream:
            stream.seek(max(0, docker_log.stat().st_size - 2 * 1024 * 1024))
            matches = re.findall(r'module ([a-zA-Z_][a-zA-Z_0-9]*): Running upgrade', stream.read().decode(errors='replace'))
        if matches:
            summary['failing_module'] = matches[-1]
    atomic_json(directory / 'failure_summary.json', summary)
    atomic_json(directory / 'restore_diagnostics.json', {'current': restore, 'history': attempt.get('restore_history', [])})
    atomic_json(directory / 'environment.json', {'python': platform.python_version(), 'platform': platform.system(), 'postgres_runtime': attempt.get('postgres_runtime')})
    atomic_json(directory / 'migration_steps.json', [
        {k: a.get(k) for k in ('id', 'started', 'ended', 'outcome', 'input_checkpoint', 'transitions', 'current_transition')}
        for a in job.data['attempts']])
    atomic_json(directory / 'openupgrade_revision.json', attempt.get('runtime', {}))
    atomic_json(directory / 'remediation_history.json', job.data['remediations'])
    (directory / 'error_traceback.txt').write_text(safe_trace(exc))
    for name in ('migration.log', 'docker.log'):
        source = Path(attempt['work']) / ('migration.log' if name == 'migration.log' else 'docker.log')
        if source.is_file():
            # Bound collection; never load an unbounded customer log into memory.
            with source.open('rb') as stream:
                stream.seek(max(0, source.stat().st_size - 2 * 1024 * 1024))
                text = stream.read().decode('utf-8', errors='replace')
            (directory / name).write_text(structural_log(text))
        else:
            (directory / name).write_text('Unavailable\n')
    safe = evidence or {}
    # Evidence producer returns only fixed-schema technical SQL/inspect output.
    for name in ('module_status', 'database_diagnostics', 'docker_status'):
        atomic_json(directory / (name + '.json'), safe.get(name, {'available': False}))
    (directory / 'README.md').write_text(
        '# Failure diagnostics\n\n'
        'Start with failure_summary.json, revisions and checkpoint history. Root cause is unverified. '
        'Review the exact upstream script/module, dependency inventory and failed stage before retry. '
        'Use resume --dry-run; never retry a partially migrated database.\n\n'
        'This archive contains technical metadata and structural log/traceback evidence only. '
        'Exception payloads, source literals, business SQL/data, paths, URLs and raw logs are withheld. '
        'Restore diagnostics allow only known noncustomer timeout SET statements; other SQL remains in restore.private.log locally. '
        'Full raw logs stay in protected local attempt storage and require private review. '
        'No dump, filestore, environment-variable list or customer records are exported. '
        'Unavailable collectors are explicitly marked; collection cannot establish a diagnosis.\n')
    destination = job.path / 'diagnostics' / (attempt['id'] + '.zip')
    temporary = destination.with_suffix('.zip.part')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.iterdir()):
            os.chmod(path, 0o600)
            archive.write(path, path.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    return destination


REPORT_LOG_LINES = 40
REPORT_JSON_LINES = 60


def _fmt(value):
    if value is None or value == []:
        return '-'
    if isinstance(value, list):
        return ', '.join(str(item) for item in value)
    return str(value)


def _render(archive, bundle_path, work_dir):
    def text(name):
        try:
            return archive.read(name).decode('utf-8', errors='replace')
        except Exception:
            return None

    def data(name):
        try:
            return json.loads(text(name))
        except Exception:
            return None

    def tail(name):
        content = text(name)
        if content is None or content.strip() == 'Unavailable':
            return None
        return content.splitlines()[-REPORT_LOG_LINES:]

    out = []

    def section(title, body):
        out.append(f'=== {title} ===')
        out.extend(body or ['unavailable'])
        out.append('')

    summary = data('failure_summary.json')
    body = []
    if isinstance(summary, dict):
        rows = [('Job', 'job_id'), ('Attempt', 'attempt_id'), ('Transition', 'transition'), ('Stage', 'stage'),
                ('Error code', 'code'), ('Failing module', 'failing_module'), ('Missing modules', 'missing_modules'),
                ('SQLSTATE', 'sqlstate'), ('Exit code', 'process_exit_code')]
        rows += [(label, key) for label, key in
                 (('Restore category', 'category'), ('Relevant error', 'relevant_error'),
                  ('Recommended action', 'recommended_action'))
                 if summary.get(key) is not None]
        body = [f'  {label + ":":<20}{_fmt(summary.get(key))}' for label, key in rows]
    section('SUMMARY', body)

    section('TRACEBACK', (text('error_traceback.txt') or '').rstrip('\n').splitlines() or None)

    body = []
    for name in ('docker.log', 'migration.log'):
        body.append(f'-- {name} (last {REPORT_LOG_LINES} lines)')
        body.extend(tail(name) or ['unavailable'])
        body.append('')
    section('LAST LOG LINES', body[:-1])

    steps = data('migration_steps.json')
    body = []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            transitions = '; '.join(f"{t.get('source')} -> {t.get('target')}: {t.get('outcome')}"
                                    for t in step.get('transitions') or [] if isinstance(t, dict))
            body.append(f"{step.get('id')}  outcome={step.get('outcome')}  {transitions or 'no transitions'}")
    section('MIGRATION STEPS', body or None)

    body = []
    for name in ('module_status.json', 'docker_status.json'):
        parsed = data(name)
        body.append(f'-- {name}')
        if parsed is None:
            body.append('unavailable')
        else:
            lines = json.dumps(parsed, indent=2, sort_keys=True).splitlines()
            body.extend(lines[:REPORT_JSON_LINES])
            if len(lines) > REPORT_JSON_LINES:
                body.append('... (truncated)')
        body.append('')
    section('MODULE / DOCKER STATUS', body[:-1])

    body = []
    try:
        if work_dir is not None and Path(work_dir).is_dir():
            body.append('Unredacted private logs for local review only; do not share or upload them.')
            for path in sorted(Path(work_dir).iterdir()):
                if path.is_file() and not path.is_symlink():
                    body.append(f'  {path.resolve()}  ({path.stat().st_size / 1024:.1f} KB)')
            if len(body) == 1:
                body.append('  no regular files')
    except OSError:
        body = []
    section('RAW LOCAL LOGS', body or ['not available'])

    out.append(f'Full diagnostics ZIP: {bundle_path.resolve()}')
    return '\n'.join(out)


def render_report(bundle_path, work_dir=None):
    """Readable failure report from a bundle; never raises, unreadable parts are shown as unavailable."""
    bundle_path = Path(bundle_path)
    try:
        archive = zipfile.ZipFile(bundle_path)
    except Exception:
        archive = None
    try:
        if archive is None:
            raise OSError('bundle could not be opened')
        return _render(archive, bundle_path, work_dir)
    except Exception:
        return f'Report unavailable: bundle could not be read.\n\nFull diagnostics ZIP: {bundle_path}'
    finally:
        if archive is not None:
            archive.close()
