"""Compatible synchronous CLI and local recovery commands."""
import json
import logging
from pathlib import Path
import shutil
import click
from rich.logging import RichHandler

from .core import OdooUpgrader
from .diagnostics import RedactionFilter
from .jobs import Job, RecoveryError

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
@click.option('--postgres-version', default='13', show_default=True, help='Numeric PostgreSQL version or auto (inspect dump; supported runtimes 13–17)')
@click.option('--log-file', type=click.Path())
@click.option('--jobs-dir', type=click.Path(path_type=Path), default=Path('.odooupgrader/jobs'), show_default=True)
@click.option('--filestore', type=click.Path(exists=True, file_okay=False), help='Matching filestore for a custom-format dump')
@click.option('--allow-missing-attachments', is_flag=True,
              help='Continue when database attachments reference missing filestore files; still verify files that exist')
@click.option('--openupgrade-revision', multiple=True, metavar='VERSION=SHA', help='Exact reviewed OpenUpgrade revision for a transition')
@click.pass_context
def main(ctx, source, version, extra_addons, verbose, postgres_version, log_file, jobs_dir, filestore,
         allow_missing_attachments, openupgrade_revision):
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
        if source or version or extra_addons or filestore or allow_missing_attachments:
            raise click.UsageError('Source/version/addons/filestore/attachment options apply only to a new upgrade')
        return
    if not source or not version:
        raise click.UsageError('A new upgrade requires --source and --version; use --help for recovery commands')
    try:
        upgrader = OdooUpgrader(source, version, extra_addons, verbose, postgres_version,
                               jobs_dir=jobs_dir, revisions=ctx.obj['revisions'], filestore=filestore,
                               allow_missing_attachments=allow_missing_attachments)
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


@main.command('resume')
@click.argument('job_id')
@click.option('--dry-run', is_flag=True, help='Read/hash only; fresh restore/registry checks are deferred')
@click.option('--openupgrade-revision', multiple=True, metavar='VERSION=SHA')
@click.option('--accept-runtime-change', metavar='SHA256', help='Accept the exact reviewed dry-run diff')
@click.option('--reason', help='Required explanation for an accepted pending runtime correction')
@click.pass_obj
def resume(options, job_id, dry_run, openupgrade_revision, accept_runtime_change, reason):
    """Restore the latest verified checkpoint and retry only the pending transition."""
    try:
        job = Job(options['jobs_dir'], job_id)
        overrides = dict(options['revisions'])
        overrides.update(revisions(openupgrade_revision))
        upgrader = OdooUpgrader('', job.data['target'], verbose=options['verbose'],
            postgres_version=job.data['postgres'], jobs_dir=options['jobs_dir'], job_id=job_id,
            revisions=overrides, acceptance=accept_runtime_change, reason=reason)
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
