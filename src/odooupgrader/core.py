import os
import shutil
import subprocess
import sys
import time
import zipfile
import logging
from typing import Optional, List
from pathlib import Path
from .recovery import RecoveryMixin
from .checks import extract_zip
from .jobs import RecoveryError, require_space, inventory
from .diagnostics import redact

import requests
from packaging import version
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

console = Console()
logger = logging.getLogger("odooupgrader")


class OdooUpgrader(RecoveryMixin):
    VALID_VERSIONS = ["10.0", "11.0", "12.0", "13.0", "14.0", "15.0", "16.0", "17.0", "18.0", "19.0"]

    def __init__(self, source: str, target_version: str, extra_addons: Optional[str] = None, verbose: bool = False,
                 postgres_version: str = "13", jobs_dir=None, job_id=None, revisions=None,
                 acceptance=None, reason=None, filestore=None, allow_missing_attachments=False,
                 single_copy=False):
        self.source = source
        self.target_version = target_version
        self.extra_addons = extra_addons
        self.verbose = verbose
        self.postgres_version = postgres_version
        self.cwd = os.getcwd()
        self.source_dir = os.path.join(self.cwd, 'source')
        self.output_dir = os.path.join(self.cwd, 'output')
        self.filestore_dir = os.path.join(self.output_dir, 'filestore')
        self.custom_addons_dir = os.path.join(self.output_dir, 'custom_addons')
        self.compose_cmd = self._get_docker_compose_cmd()
        self._configure_recovery(jobs_dir, job_id, revisions, acceptance, reason, filestore,
                                 allow_missing_attachments, single_copy)

    def _run_cmd(self, cmd: List[str], check: bool = True, capture_output: bool = False, timeout=None, stdin=None) -> subprocess.CompletedProcess:
        """Executes a subprocess command and logs it."""
        cmd_str = redact(" ".join(cmd))
        logger.debug(f"Executing: {cmd_str}")

        try:
            result = subprocess.run(
                cmd,
                check=check,
                text=True,
                capture_output=capture_output,
                cwd=self.cwd,
                timeout=timeout,
                stdin=stdin
            )
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {cmd_str}")
            if capture_output and e.stderr:
                logger.error("Command stderr: %s", redact(e.stderr[-65536:]))
            raise

    def _get_docker_compose_cmd(self) -> List[str]:
        """Determines if 'docker compose' or 'docker-compose' is available."""
        try:
            subprocess.run(["docker", "compose", "version"], check=True, capture_output=True)
            return ["docker", "compose"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            try:
                subprocess.run(["docker-compose", "--version"], check=True, capture_output=True)
                return ["docker-compose"]
            except (subprocess.CalledProcessError, FileNotFoundError):
                return ["docker", "compose"]

    def _cleanup_dir(self, path: str):
        """Safely removes a directory."""
        if os.path.exists(path):
            try:
                shutil.rmtree(path)
                logger.debug(f"Removed directory: {path}")
            except Exception as e:
                msg = f"Warning: Could not remove {path}: {e}"
                console.print(f"[yellow]{msg}[/yellow]")
                logger.warning(msg)

    def validate_source_accessibility(self):
        """Checks if source file or URL is valid using Requests."""
        console.print("[blue]Validating source accessibility...[/blue]")
        logger.info("Validating source input")

        if self.source.startswith("http://") or self.source.startswith("https://"):
            try:
                with requests.get(self.source, stream=True, timeout=30) as response:
                    response.raise_for_status()
                console.print("[green]Source URL is accessible.[/green]")
            except requests.RequestException as e:
                console.print(redact(f"[bold red]Error:[/bold red] Source URL is not accessible: {e}"))
                logger.error(f"Source URL invalid: {e}")
                raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')
        else:
            if not os.path.exists(self.source):
                console.print(f"[bold red]Error:[/bold red] Source file not found: {self.source}")
                logger.error(f"Source file not found: {self.source}")
                raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')
            console.print("[green]Source file exists.[/green]")

        if self.extra_addons:
            console.print("[blue]Validating extra addons...[/blue]")
            if "://" in self.extra_addons:
                if not (self.extra_addons.startswith("http://") or self.extra_addons.startswith("https://")):
                    console.print(
                        f"[bold red]Error:[/bold red] Invalid protocol for addons URL. Only http/https supported.")
                    raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')

                try:
                    with requests.head(self.extra_addons, timeout=30, allow_redirects=True) as response:
                        if response.status_code >= 400:
                            raise requests.RequestException("Status code error")
                except requests.RequestException:
                    console.print(f"[bold red]Error:[/bold red] Extra addons URL is not accessible.")
                    logger.error("Extra addons URL invalid")
                    raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')
            else:
                if not os.path.exists(self.extra_addons):
                    console.print(f"[bold red]Error:[/bold red] Extra addons path not found: {self.extra_addons}")
                    logger.error(f"Extra addons path not found: {self.extra_addons}")
                    raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')


    def download_file(self, url: str, dest_path: str, description: str = "Downloading..."):
        """Generic download helper."""
        logger.info("Downloading source asset")
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("Content-Length", 0))
                require_space(Path(dest_path).parent, total_size)

                with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        BarColumn(),
                        TaskProgressColumn(),
                        "•",
                        TimeElapsedColumn(),
                        console=console
                ) as progress:
                    task = progress.add_task(f"[cyan]{description}", total=total_size)
                    with open(dest_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            require_space(Path(dest_path).parent, len(chunk))
                            f.write(chunk)
                            progress.update(task, advance=len(chunk))
        except requests.RequestException as e:
            console.print(redact(f"[bold red]Download failed:[/bold red] {e}"))
            logger.error(f"Download failed: {e}")
            raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')

    def download_or_copy_source(self) -> str:
        """Retain the input, or read a local single-copy input in place."""
        from urllib.parse import urlsplit
        is_url = self.source.startswith(('http://', 'https://'))
        if self.single_copy and not is_url:
            source = Path(self.source).resolve(strict=True)
            if not source.is_file():
                raise RecoveryError('RESTORE_INPUT: source must be a regular file')
            return str(source)
        extension = '.zip' if urlsplit(self.source).path.lower().endswith('.zip') else '.dump'
        destination = self.job.path / 'inputs' / ('source' + extension)
        if destination.exists():
            raise RecoveryError('INPUT_EXISTS: refusing to overwrite retained source')
        if is_url:
            self.download_file(self.source, str(destination), 'Downloading source DB...')
        else:
            require_space(destination.parent, Path(self.source).stat().st_size)
            shutil.copyfile(self.source, destination)
        os.chmod(destination, 0o400)
        return str(destination)

    def process_extra_addons(self):
        """Downloads, copies, and extracts extra addons to output/custom_addons."""
        if not self.extra_addons:
            return

        console.print("[blue]Processing custom addons...[/blue]")
        logger.info("Processing custom addons...")

        if self.extra_addons.startswith("http://") or self.extra_addons.startswith("https://"):
            zip_path = os.path.join(self.source_dir, "addons.zip")
            self.download_file(self.extra_addons, zip_path, "Downloading extra addons...")
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    extract_zip(zip_path if self.extra_addons.startswith(("http://", "https://")) else self.extra_addons, self.custom_addons_dir)
                os.remove(zip_path)
            except zipfile.BadZipFile:
                console.print("[bold red]Error:[/bold red] Downloaded addons file is not a valid zip.")
                logger.error("Invalid addons zip file")
                raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')

        elif os.path.isfile(self.extra_addons) and self.extra_addons.lower().endswith('.zip'):
            try:
                with zipfile.ZipFile(self.extra_addons, 'r') as zip_ref:
                    extract_zip(zip_path if self.extra_addons.startswith(("http://", "https://")) else self.extra_addons, self.custom_addons_dir)
            except zipfile.BadZipFile:
                console.print("[bold red]Error:[/bold red] Addons file is not a valid zip.")
                raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')

        elif os.path.isdir(self.extra_addons):
            try:
                inventory(self.extra_addons)
                shutil.copytree(self.extra_addons, self.custom_addons_dir, dirs_exist_ok=True)
            except Exception as e:
                console.print(redact(f"[bold red]Error:[/bold red] Failed to copy local addons: {e}"))
                raise RecoveryError('INPUT_PREPARATION: source/addon preparation failed; inspect protected logs')

        # Handle directory nesting (common in GitHub downloads), ignoring hidden files
        items = [i for i in os.listdir(self.custom_addons_dir) if not i.startswith('.')]
        if len(items) == 1:
            single_item_path = os.path.join(self.custom_addons_dir, items[0])
            if os.path.isdir(single_item_path):
                sub_items = os.listdir(single_item_path)
                is_module = any(x in sub_items for x in ['__manifest__.py', '__openerp__.py'])

                if not is_module:
                    logger.info(f"Detected wrapper directory '{items[0]}'. Flattening structure...")
                    for sub in sub_items:
                        src_sub = os.path.join(single_item_path, sub)
                        dst_sub = os.path.join(self.custom_addons_dir, sub)
                        if not os.path.exists(dst_sub):
                            shutil.move(src_sub, dst_sub)
                    try:
                        os.rmdir(single_item_path)
                    except OSError:
                        pass

        # Check for flat structure (single module at root)
        items = os.listdir(self.custom_addons_dir)
        has_manifest = any(x in items for x in ['__manifest__.py', '__openerp__.py'])

        if has_manifest:
            logger.info("Detected flat addon structure. Reorganizing...")
            sub_folder_name = "downloaded_module"
            sub_folder_path = os.path.join(self.custom_addons_dir, sub_folder_name)
            os.makedirs(sub_folder_path, exist_ok=True)

            for item in items:
                src = os.path.join(self.custom_addons_dir, item)
                dst = os.path.join(sub_folder_path, item)
                if src != sub_folder_path:
                    shutil.move(src, dst)

        # Ensure requirements.txt exists
        req_path = os.path.join(self.custom_addons_dir, "requirements.txt")
        if not os.path.exists(req_path):
            with open(req_path, "w") as f:
                f.write("")
        elif os.path.getsize(req_path) == 0:
            logger.warning("Empty requirements.txt found in custom addons.")

        # Standardize permissions for Docker compatibility
        logger.info("Standardizing addon permissions...")
        for root, dirs, files in os.walk(self.custom_addons_dir):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o755)
                except Exception:
                    pass
            for f in files:
                try:
                    if f.endswith('.sh'):
                        os.chmod(os.path.join(root, f), 0o755)
                    else:
                        os.chmod(os.path.join(root, f), 0o644)
                except Exception:
                    pass

        console.print("[green]Custom addons prepared.[/green]")

    def _get_custom_module_names(self) -> str:
        """Scans the custom_addons_dir for valid Odoo modules and returns a comma-separated string."""
        if not self.extra_addons or not os.path.exists(self.custom_addons_dir):
            return ""

        modules = []
        for item in os.listdir(self.custom_addons_dir):
            item_path = os.path.join(self.custom_addons_dir, item)
            if os.path.isdir(item_path):
                if (os.path.exists(os.path.join(item_path, '__manifest__.py')) or
                        os.path.exists(os.path.join(item_path, '__openerp__.py'))):
                    modules.append(item)

        if modules:
            return "," + ",".join(modules)
        return ""

    def process_source_file(self, filepath: str) -> str:
        """Extracts ZIP or prepares DUMP file."""
        if zipfile.is_zipfile(filepath):
            console.print("[blue]Extracting ZIP file...[/blue]")
            logger.info("Extracting ZIP file...")
            extract_zip(filepath, self.source_dir)
            self.prepared_dump_path = Path(self.source_dir) / 'dump.sql'
            return "ZIP"
        else:
            console.print("[blue]Processing DUMP file...[/blue]")
            logger.info("Processing DUMP file...")
            # download_or_copy_source already retained one private input copy.
            self.prepared_dump_path = Path(filepath)
            return "DUMP"





    def get_version_info(self, ver_str: str) -> version.Version:
        """Parses version string securely using packaging.version."""
        try:
            clean_ver = ver_str.strip()
            return version.parse(clean_ver)
        except Exception:
            return version.parse("0.0")

    def generate_next_version(self, current: str) -> str:
        """Calculates next major version (e.g. 15.0 -> 16.0)."""
        try:
            major = int(current.split('.')[0])
            return f"{major + 1}.0"
        except Exception:
            v = version.parse(current)
            return f"{v.major + 1}.0"
