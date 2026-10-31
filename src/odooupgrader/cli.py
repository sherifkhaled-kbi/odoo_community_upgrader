"""Compatible synchronous CLI and local recovery commands."""
import json
import logging
from pathlib import Path
import shutil
import subprocess
import click
from rich.logging import RichHandler

from .core import OdooUpgrader
from .diagnostics import RedactionFilter
from .jobs import Job, RecoveryError, allocated_bytes

logging.basicConfig(level=logging.INFO, format='%(message)s',
                    handlers=[RichHandler(rich_tracebacks=False, show_level=False, show_path=False)])


def revisions(values):
    result = {}
    for value in values:
        parts = value.split('=', 1)
        if len(parts) != 2:
            raise click.BadParameter('Use VERSION=full-commit-SHA')
        result[parts[0]] = parts[1]
    return result


def execute(upgrader):
    try:
        return upgrader.run()
    except RecoveryError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        raise click.Abort()
    except Exception as exc:
        raise click.ClickException(f'Migration failed ({type(exc).__name__}); inspect the diagnostics bundle.') from exc


@click.group(invoke_without_command=True)
@click.option('--source', help='Path to local .zip/.dump file or URL')
@click.option('--version', type=click.Choice(OdooUpgrader.VALID_VERSIONS), help='Target Odoo version')
@click.option('--extra-addons', help='Custom addons folder, ZIP or URL')
@click.option('--verbose', is_flag=True)
@click.option('--postgres-version', default='13', show_default=True, help='Numeric PostgreSQL version or auto (inspect dump; supported runtimes 13–18)')
@click.option('--log-file', type=click.Path())
@click.option('--jobs-dir', type=click.Path(path_type=Path), default=Path('.odooupgrader/jobs'), show_default=True)
@click.option('--filestore', type=click.Path(exists=True, file_okay=False), help='Matching filestore for a custom-format dump')
@click.option('--allow-missing-attachments', is_flag=True,
              help='Continue when database attachments reference missing filestore files; still verify files that exist')
@click.option('--single-copy', is_flag=True,
              help='Use one working database and filestore with ZIP checkpoints; no independent restore verification')
@click.option('--source-version', type=click.Choice(OdooUpgrader.VALID_VERSIONS),
              help='Known source major version; prebuild the first migration image before restoring the backup')
@click.option('--openupgrade-revision', multiple=True, metavar='VERSION=SHA', help='Exact reviewed OpenUpgrade revision for a transition')
@click.option('--skip-missing-modules-check', is_flag=True,
              help='Skip the preflight check for installed modules missing from addons-path (unsafe; the upgrade may still fail)')
@click.pass_context
def main(ctx, source, version, extra_addons, verbose, postgres_version, log_file, jobs_dir, filestore,
         allow_missing_attachments, single_copy, source_version, openupgrade_revision, skip_missing_modules_check):
    """Upgrade Odoo locally using Docker; retain verified recovery checkpoints."""
    logger = logging.getLogger('odooupgrader')
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactionFilter())
    if log_file:
        handler = logging.FileHandler(log_file)
        handler.addFilter(RedactionFilter())
        logger.addHandler(handler)
        ctx.call_on_close(lambda: (logger.removeHandler(handler), handler.close()))
    ctx.obj = {'jobs_dir': jobs_dir.resolve(), 'verbose': verbose, 'revisions': revisions(openupgrade_revision)}
    if ctx.invoked_subcommand is not None:
        if (source or version or extra_addons or filestore or allow_missing_attachments or single_copy
                or source_version or skip_missing_modules_check):
            raise click.UsageError('Source/version/addons/filestore/attachment/storage options apply only to a new upgrade')
        return
    if not source or not version:
        raise click.UsageError('A new upgrade requires --source and --version; use --help for recovery commands')
    if skip_missing_modules_check:
        logger.warning('--skip-missing-modules-check is set: the missing-module preflight will be skipped')
    try:
        upgrader = OdooUpgrader(source, version, extra_addons, verbose, postgres_version,
                               jobs_dir=jobs_dir, revisions=ctx.obj['revisions'], filestore=filestore,
                               allow_missing_attachments=allow_missing_attachments, single_copy=single_copy,
                               source_version=source_version, skip_missing_modules_check=skip_missing_modules_check)
        execute(upgrader)
    except RecoveryError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command('jobs')
@click.pass_obj
def jobs(options):
    """List local job IDs and recorded states without contacting Docker."""
    root = options['jobs_dir']
    if root.exists():
        for path in sorted(root.iterdir()):
            if path.is_dir():
                try:
                    job = Job(root, path.name)
                    click.echo(f'{job.id} {job.observed_state()} {job.data["source_version"]} -> {job.data["target"]}')
                except RecoveryError:
                    click.echo(f'{path.name}: unreadable job metadata', err=True)


@main.command('status')
@click.argument('job_id')
@click.pass_obj
def status(options, job_id):
    """Display durable state; RUNNING may mean a terminated CLI needs reconciliation."""
    try:
        job = Job(options['jobs_dir'], job_id)
        click.echo(json.dumps(dict(job.data, observed_state=job.observed_state()), indent=2))
    except RecoveryError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command('storage')
@click.argument('job_id')
@click.pass_obj
def storage(options, job_id):
    """Measure job files and accessible owned Docker volumes without changing them."""
    try:
        job = Job(options['jobs_dir'], job_id)
        sections = {}
        seen = set()
        for name in ('checkpoints', 'inputs', 'attempts', 'diagnostics'):
            sections[name] = allocated_bytes(job.path / name, seen)
        sections['metadata'] = sum(allocated_bytes(path, seen) for path in job.path.iterdir() if path.is_file())
        free = shutil.disk_usage(job.path).free
        volumes = []
        try:
            listed = subprocess.run(['docker', 'volume', 'ls', '--quiet', '--filter', 'label=odooupgrader.job=' + job.id],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            listed = None
        if listed and listed.returncode == 0:
            for name in listed.stdout.splitlines():
                try:
                    inspected = subprocess.run(['docker', 'volume', 'inspect', name],
                                               capture_output=True, text=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    inspected = None
                if not inspected or inspected.returncode:
                    volumes.append({'name': name, 'allocated_bytes': None})
                    continue
                info = json.loads(inspected.stdout)[0]
                if (info.get('Labels') or {}).get('odooupgrader.job') != job.id:
                    continue
                mount = Path(info['Mountpoint'])
                selected = str(job.data.get('selected_postgres') or job.data['postgres'])
                wal_path = (mount / selected.split('.')[0] / 'docker' / 'pg_wal'
                            if selected != 'auto' and int(selected.split('.')[0]) >= 18
                            else mount / 'pg_wal')
                try:
                    volumes.append({'name': name, 'allocated_bytes': allocated_bytes(mount),
                                    'pg_wal_bytes': allocated_bytes(wal_path)})
                except OSError:
                    volumes.append({'name': name, 'allocated_bytes': None})
        image_ids = set()
        for runtime in job.data.get('runtime_locks', {}).values():
            if runtime.get('image_id'):
                image_ids.add(runtime['image_id'])
        for attempt in job.data['attempts']:
            for prepared in attempt.get('prebuilt_images', {}).values():
                if prepared.get('image_id'):
                    image_ids.add(prepared['image_id'])
            ready = attempt.get('workspace_ready') or {}
            if ready.get('runtime', {}).get('image_id'):
                image_ids.add(ready['runtime']['image_id'])
        images = []
        for image_id in sorted(image_ids):
            try:
                inspected = subprocess.run(['docker', 'image', 'inspect', image_id, '--format={{.Size}}'],
                                           capture_output=True, text=True, timeout=15)
                size = int(inspected.stdout.strip()) if inspected.returncode == 0 else None
            except (OSError, subprocess.SubprocessError, ValueError):
                size = None
            images.append({'image_id': image_id, 'virtual_size_bytes': size})
        click.echo(json.dumps({'job_id': job.id, 'filesystem_free_bytes': free,
                               'job_sections_allocated_bytes': sections,
                               'job_total_allocated_bytes': sum(sections.values()),
                               'docker_volumes': volumes, 'docker_volume_inventory_available': bool(listed and listed.returncode == 0),
                               'docker_images': images, 'image_sizes_include_shared_layers': True}, indent=2))
    except (RecoveryError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        raise click.ClickException(f'STORAGE_REPORT: {type(exc).__name__}: {exc}') from exc


@main.command('resume')
@click.argument('job_id')
@click.option('--dry-run', is_flag=True, help='Read/hash only; fresh restore/registry checks are deferred')
@click.option('--openupgrade-revision', multiple=True, metavar='VERSION=SHA')
@click.option('--accept-runtime-change', metavar='SHA256', help='Accept the exact reviewed dry-run diff')
@click.option('--reason', help='Required explanation for an accepted pending runtime correction')
@click.option('--skip-missing-modules-check', is_flag=True,
              help='Skip the preflight check for installed modules missing from addons-path (unsafe; the upgrade may still fail)')
@click.pass_obj
def resume(options, job_id, dry_run, openupgrade_revision, accept_runtime_change, reason, skip_missing_modules_check):
    """Reuse a verified pre-migration workspace or restore the latest ZIP checkpoint."""
    try:
        job = Job(options['jobs_dir'], job_id)
        overrides = dict(options['revisions'])
        overrides.update(revisions(openupgrade_revision))
        if skip_missing_modules_check:
            logging.getLogger('odooupgrader').warning(
                '--skip-missing-modules-check is set: the missing-module preflight will be skipped')
        upgrader = OdooUpgrader('', job.data['target'], verbose=options['verbose'],
            postgres_version=job.data['postgres'], jobs_dir=options['jobs_dir'], job_id=job_id,
            revisions=overrides, acceptance=accept_runtime_change, reason=reason,
            skip_missing_modules_check=skip_missing_modules_check)
        if dry_run:
            click.echo(json.dumps(upgrader.dry_run(), indent=2))
        else:
            execute(upgrader)
    except RecoveryError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command('diagnostics')
@click.argument('job_id')
@click.option('--output', type=click.Path(path_type=Path), help='Copy latest existing bundle to a new path')
@click.pass_obj
def diagnostics(options, job_id, output):
    """Locate or export the latest sanitized failure ZIP; no registry initialization."""
    try:
        job = Job(options['jobs_dir'], job_id)
        bundles = [job.path / 'diagnostics' / (a['id'] + '.zip') for a in reversed(job.data['attempts'])]
        bundle = next((p for p in bundles if p.is_file()), None)
        if bundle is None:
            raise RecoveryError('NO_DIAGNOSTICS: no completed bundle is available')
        if output:
            with output.open('xb') as destination, bundle.open('rb') as source:
                shutil.copyfileobj(source, destination)
            output.chmod(0o600)
            bundle = output
        click.echo(str(bundle.resolve()))
    except (RecoveryError, OSError) as exc:
        raise click.ClickException('Diagnostics export failed: ' + (str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__)) from exc


if __name__ == '__main__':
    main()
