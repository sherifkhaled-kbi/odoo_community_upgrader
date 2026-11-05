"""Terminal output: shared console, header, and final success/failure summaries.

rich.Console auto-disables ANSI styling when its output isn't a terminal (redirected
to a file, piped, non-interactive), so every print_* function here stays readable
without any extra branching for that case.
"""
from rich.console import Console

console = Console()


def format_duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def print_header(job_id, source_version, target_version, storage_mode):
    console.print()
    console.print('[bold]ODOOUPGRADER[/bold]')
    console.print(f'Job ID: {job_id}')
    console.print(f'Source Version: {source_version or "(detecting...)"}')
    console.print(f'Target Version: {target_version}')
    console.print(f'Storage Mode: {storage_mode}')
    console.print()


def print_success_summary(*, job_id, upgrade_path, final_version, duration_seconds,
                           final_zip, checksum_path, independent_restore_verified, storage_mode):
    if storage_mode == 'single_copy':
        restore_status = 'NOT PERFORMED'
    else:
        restore_status = 'VERIFIED' if independent_restore_verified else 'NOT PERFORMED'
    console.print()
    console.print('[bold green]ODOOUPGRADER — UPGRADE COMPLETED SUCCESSFULLY[/bold green]')
    console.print()
    console.print('[bold]Status:[/bold] SUCCESS')
    console.print(f'[bold]Upgrade Path:[/bold] Odoo {upgrade_path}')
    console.print(f'[bold]Final Version:[/bold] {final_version}')
    console.print(f'[bold]Job ID:[/bold] {job_id}')
    console.print(f'[bold]Duration:[/bold] {format_duration(duration_seconds)}')
    console.print(f'[bold]Final ZIP:[/bold] {final_zip}')
    console.print(f'[bold]SHA-256:[/bold] {checksum_path}')
    console.print('[bold]Migration Checkpoints:[/bold] PASSED')
    console.print(f'[bold]Independent Restore Verification:[/bold] {restore_status}')
    console.print()


def print_failure_summary(*, job_id, failed_stage, error_code, duration_seconds, diagnostics_path):
    console.print()
    console.print('[bold red]ODOOUPGRADER — UPGRADE FAILED[/bold red]')
    console.print()
    console.print('[bold]Status:[/bold] FAILED')
    console.print(f'[bold]Job ID:[/bold] {job_id}')
    console.print(f'[bold]Failed Stage:[/bold] {failed_stage or "(unknown)"}')
    console.print(f'[bold]Error:[/bold] {error_code}')
    console.print(f'[bold]Duration:[/bold] {format_duration(duration_seconds)}')
    console.print(f'[bold]Diagnostics Bundle:[/bold] {diagnostics_path or "(not available)"}')
    console.print()
