import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
import logging
from pathlib import Path
from typing import Optional, List

import requests
from packaging import version
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

from .compatibility.build_context import prepare_image_package
from .compatibility.dependency_registry import get_dependency_spec
from .compatibility.dockerfile import render_dockerfile
from .compatibility.patch_registry import patches_for_version
from .compatibility.reporting import (
    MigrationFailure,
    load_uom_repair_report,
    parse_odoo_failure,
    redact,
    retention_commands,
    should_preserve_database,
    write_failure_report,
)

console = Console()
logger = logging.getLogger("odooupgrader")


class OdooUpgrader:
    VALID_VERSIONS = ["10.0", "11.0", "12.0", "13.0", "14.0", "15.0", "16.0", "17.0", "18.0", "19.0"]
    COMPOSE_PROJECT = "odooupgrader"
    DB_CONTAINER = "odooupgrader-db"
    ODOO_CONTAINER = "odooupgrader-odoo"
    NETWORK_NAME = "odooupgrader-network"
    DB_COMPOSE_FILE = "db-composer.yml"
    UPGRADE_COMPOSE_FILE = "odoo-upgrade-composer.yml"
    RETAINED_FLAG = "/var/lib/postgresql/data/.odooupgrader-retained"
    VOLUME_NAME = "odooupgrader_postgres_data"

    def __init__(self, source: str, target_version: str, extra_addons: Optional[str] = None, verbose: bool = False,
                 postgres_version: str = "13", keep_db_on_failure: bool = False):
        self.source = source
        self.target_version = target_version
        self.extra_addons = extra_addons
        self.verbose = verbose
        self.postgres_version = postgres_version
        self.keep_db_on_failure = keep_db_on_failure
        self.cwd = os.getcwd()
        self.db_container = self.DB_CONTAINER
        self.odoo_container = self.ODOO_CONTAINER
        self._succeeded = False
        self._db_started = False
        self._database_retained = False
        self._failure_detail: Optional[str] = None
        self._failed_step: Optional[str] = None
        self._source_version: Optional[str] = None
        self._upgrade_stderr = ""
        self.source_dir = os.path.join(self.cwd, 'source')
        self.output_dir = os.path.join(self.cwd, 'output')
        self.filestore_dir = os.path.join(self.output_dir, 'filestore')
        self.custom_addons_dir = os.path.join(self.output_dir, 'custom_addons')
        self.compose_cmd = self._get_docker_compose_cmd()

    def _run_cmd(self, cmd: List[str], check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess:
        """Executes a subprocess command and logs it."""
        cmd_str = " ".join(cmd)
        logger.debug(f"Executing: {cmd_str}")

        try:
            result = subprocess.run(
                cmd,
                check=check,
                text=True,
                capture_output=capture_output
            )
            if capture_output and result.stdout:
                logger.debug(f"Command Output: {result.stdout.strip()}")
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {cmd_str}")
            if capture_output and e.stderr:
                logger.error(f"Error output: {e.stderr.strip()}")
            raise

    def _compose(self, filename: str, *args: str) -> List[str]:
        """Docker Compose command scoped to this project's containers and volumes."""

        return self.compose_cmd + ["-p", self.COMPOSE_PROJECT, "-f", filename, *args]

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
        logger.info(f"Validating source: {self.source}")

        if self.source.startswith("http://") or self.source.startswith("https://"):
            try:
                with requests.get(self.source, stream=True, timeout=30) as response:
                    response.raise_for_status()
                console.print("[green]Source URL is accessible.[/green]")
            except requests.RequestException as e:
                console.print(f"[bold red]Error:[/bold red] Source URL is not accessible: {e}")
                logger.error(f"Source URL invalid: {e}")
                sys.exit(1)
        else:
            if not os.path.exists(self.source):
                console.print(f"[bold red]Error:[/bold red] Source file not found: {self.source}")
                logger.error(f"Source file not found: {self.source}")
                sys.exit(1)
            console.print("[green]Source file exists.[/green]")

        if self.extra_addons:
            console.print("[blue]Validating extra addons...[/blue]")
            if "://" in self.extra_addons:
                if not (self.extra_addons.startswith("http://") or self.extra_addons.startswith("https://")):
                    console.print(
                        f"[bold red]Error:[/bold red] Invalid protocol for addons URL. Only http/https supported.")
                    sys.exit(1)

                try:
                    with requests.head(self.extra_addons, timeout=30, allow_redirects=True) as response:
                        if response.status_code >= 400:
                            raise requests.RequestException("Status code error")
                except requests.RequestException:
                    console.print(f"[bold red]Error:[/bold red] Extra addons URL is not accessible.")
                    logger.error("Extra addons URL invalid")
                    sys.exit(1)
            else:
                if not os.path.exists(self.extra_addons):
                    console.print(f"[bold red]Error:[/bold red] Extra addons path not found: {self.extra_addons}")
                    logger.error(f"Extra addons path not found: {self.extra_addons}")
                    sys.exit(1)

    def prepare_environment(self):
        """Creates necessary directories and cleans old data."""
        logger.info("Preparing environment directories...")
        self._cleanup_dir(self.source_dir)
        self._cleanup_dir(self.output_dir)

        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.filestore_dir, exist_ok=True)
        os.makedirs(self.custom_addons_dir, exist_ok=True)

        if sys.platform != "win32":
            try:
                os.chmod(self.output_dir, 0o777)
                for root, dirs, files in os.walk(self.output_dir):
                    for d in dirs:
                        os.chmod(os.path.join(root, d), 0o777)
                    for f in files:
                        os.chmod(os.path.join(root, f), 0o777)
            except Exception as e:
                logger.warning(f"Could not set broad permissions on output dir: {e}")

    def download_file(self, url: str, dest_path: str, description: str = "Downloading..."):
        """Generic download helper."""
        logger.info(f"Downloading {url} to {dest_path}")
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("Content-Length", 0))

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
                            f.write(chunk)
                            progress.update(task, advance=len(chunk))
        except requests.RequestException as e:
            console.print(f"[bold red]Download failed:[/bold red] {e}")
            logger.error(f"Download failed: {e}")
            sys.exit(1)

    def download_or_copy_source(self) -> str:
        """Downloads file from URL using Requests with Rich progress bar."""
        target_path = ""
        if self.source.startswith("http://") or self.source.startswith("https://"):
            filename = os.path.basename(self.source.split("?")[0]) or "downloaded_db.dump"
            target_path = os.path.join(self.cwd, filename)
            self.download_file(self.source, target_path, "Downloading source DB...")
        else:
            target_path = self.source
        return target_path

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
                    zip_ref.extractall(self.custom_addons_dir)
                os.remove(zip_path)
            except zipfile.BadZipFile:
                console.print("[bold red]Error:[/bold red] Downloaded addons file is not a valid zip.")
                logger.error("Invalid addons zip file")
                sys.exit(1)

        elif os.path.isfile(self.extra_addons) and self.extra_addons.lower().endswith('.zip'):
            try:
                with zipfile.ZipFile(self.extra_addons, 'r') as zip_ref:
                    zip_ref.extractall(self.custom_addons_dir)
            except zipfile.BadZipFile:
                console.print("[bold red]Error:[/bold red] Addons file is not a valid zip.")
                sys.exit(1)

        elif os.path.isdir(self.extra_addons):
            try:
                shutil.copytree(self.extra_addons, self.custom_addons_dir, dirs_exist_ok=True)
            except Exception as e:
                console.print(f"[bold red]Error:[/bold red] Failed to copy local addons: {e}")
                sys.exit(1)

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
        ext = os.path.splitext(filepath)[1].lower()

        if ext == '.zip':
            console.print("[blue]Extracting ZIP file...[/blue]")
            logger.info("Extracting ZIP file...")
            with zipfile.ZipFile(filepath, 'r') as zip_ref:
                zip_ref.extractall(self.source_dir)
            return "ZIP"
        else:
            console.print("[blue]Processing DUMP file...[/blue]")
            logger.info("Processing DUMP file...")
            shutil.copy2(filepath, os.path.join(self.source_dir, "database.dump"))
            return "DUMP"

    def create_db_compose_file(self):
        """Generates the docker-compose file for the database."""
        content = f"""
name: {self.COMPOSE_PROJECT}
services:
  db-odooupgrade:
    container_name: {self.db_container}
    image: postgres:{self.postgres_version}
    labels:
      odooupgrader.managed: "true"
    environment:
      - POSTGRES_DB=odoo
      - POSTGRES_PASSWORD=odoo
      - POSTGRES_USER=odoo
    networks:
      - odooupgrader-network
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

networks:
  odooupgrader-network:
    driver: bridge
    name: {self.NETWORK_NAME}

volumes:
  postgres_data:
    name: {self.VOLUME_NAME}
"""
        with open(self.DB_COMPOSE_FILE, "w", newline='\n') as f:
            f.write(content.strip())

    def wait_for_db(self):
        """Waits until Postgres is ready."""
        console.print("[yellow]Waiting for database to be ready...[/yellow]")
        max_retries = 30
        cmd = ["docker", "exec", self.db_container, "pg_isready", "-U", "odoo", "-d", "odoo"]

        for _ in range(max_retries):
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                console.print("[green]Database is ready.[/green]")
                return
            except subprocess.CalledProcessError:
                time.sleep(2)

        console.print("[bold red]Database failed to start.[/bold red]")
        sys.exit(1)

    def restore_database(self, file_type: str):
        """Restores the database dump."""
        console.print("[blue]Restoring database...[/blue]")
        logger.info("Restoring database...")

        self._run_cmd(["docker", "exec", self.db_container, "createdb", "-U", "odoo", "database"], check=False)

        if file_type == "ZIP":
            dump_path = os.path.join(self.source_dir, "dump.sql")
            if not os.path.exists(dump_path):
                found_sql = [f for f in os.listdir(self.source_dir) if f.endswith('.sql')]
                if found_sql:
                    dump_path = os.path.join(self.source_dir, found_sql[0])
                else:
                    console.print("[bold red]No dump.sql found inside ZIP.[/bold red]")
                    sys.exit(1)

            src_filestore = os.path.join(self.source_dir, "filestore")
            if os.path.exists(src_filestore):
                try:
                    shutil.copytree(src_filestore, self.filestore_dir, dirs_exist_ok=True)
                    if sys.platform != "win32":
                        try:
                            os.chmod(self.filestore_dir, 0o777)
                            for root, dirs, files in os.walk(self.filestore_dir):
                                for d in dirs:
                                    os.chmod(os.path.join(root, d), 0o777)
                                for f in files:
                                    os.chmod(os.path.join(root, f), 0o777)
                        except Exception as e:
                            logger.warning(f"Failed to set filestore permissions: {e}")
                except Exception as e:
                    logger.warning(f"Failed to copy filestore: {e}")

            self._run_cmd(["docker", "cp", dump_path, f"{self.db_container}:/tmp/dump.sql"])
            self._run_cmd(["docker", "exec", "-i", self.db_container, "psql", "-U", "odoo", "-d", "database", "-f",
                           "/tmp/dump.sql"], capture_output=True)

        elif file_type == "DUMP":
            dump_path = os.path.join(self.source_dir, "database.dump")
            self._run_cmd(["docker", "cp", dump_path, f"{self.db_container}:/tmp/database.dump"])

            cmd = [
                "docker", "exec", self.db_container, "pg_restore",
                "-U", "odoo", "-d", "database",
                "--no-owner", "--no-privileges", "--clean", "--if-exists",
                "--disable-triggers", "--single-transaction",
                "/tmp/database.dump"
            ]
            self._run_cmd(cmd, check=False)

    def get_current_version(self) -> str:
        """Queries the database to find the current Odoo version."""
        queries = [
            "SELECT latest_version FROM ir_module_module WHERE name = 'base' AND state = 'installed';",
            "SELECT value FROM ir_config_parameter WHERE key = 'database.latest_version';",
            "SELECT latest_version FROM ir_module_module WHERE name = 'base' ORDER BY id DESC LIMIT 1;"
        ]

        for q in queries:
            cmd = ["docker", "exec", "-i", self.db_container, "psql", "-U", "odoo", "-d", "database", "-t", "-A", "-c",
                   q]
            try:
                res = self._run_cmd(cmd, check=False, capture_output=True)
                ver = res.stdout.strip()
                if ver:
                    return ver
            except Exception:
                continue
        return ""

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

    def run_upgrade_step(self, target_version: str) -> bool:
        """Builds and runs the OpenUpgrade container."""
        logger.info(f"Preparing upgrade step to version {target_version}")

        is_final_step = (target_version == self.target_version)

        extra_addons_cmds = ""
        extra_addons_path_arg = ""
        custom_modules_load = ""

        if self.extra_addons:
            # Force cache invalidation so COPY instructions run every time
            with open(os.path.join(self.custom_addons_dir, ".build_timestamp"), "w") as f:
                f.write(str(time.time()))

            # Optimized Layering: Copy reqs first, then pip, then code
            extra_addons_cmds = """
RUN mkdir -p /mnt/custom-addons
COPY --chown=odoo:odoo ./output/custom_addons/requirements.txt /mnt/custom-addons/requirements.txt
RUN pip3 install --no-cache-dir -r /mnt/custom-addons/requirements.txt
COPY --chown=odoo:odoo ./output/custom_addons/ /mnt/custom-addons/
"""

            if is_final_step:
                logger.info("Target version reached. Injecting custom addons path and modules.")
                extra_addons_path_arg = ",/mnt/custom-addons"
                custom_modules_load = self._get_custom_module_names()
            else:
                logger.info("Intermediate version. Skipping custom addons loading.")

        if patches_for_version(target_version):
            prepare_image_package(Path(self.cwd))
            logger.info("Prepared migration compatibility package for %s", target_version)

        dependency = get_dependency_spec(target_version)
        openupgrade_revision = dependency.openupgrade_revision if dependency else ""
        dockerfile_content = render_dockerfile(target_version, extra_addons_cmds)
        with open("Dockerfile", "w", newline='\n') as f:
            f.write(dockerfile_content.strip() + "\n")

        compose_content = f"""
name: {self.COMPOSE_PROJECT}
services:
  odoo-openupgrade:
    image: odoo-openupgrade
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {self.odoo_container}
    environment:
      - HOST={self.db_container}
      - POSTGRES_USER=odoo
      - POSTGRES_PASSWORD=odoo
      - ODOOUPGRADER_REPAIR_REPORT=/var/log/odoo/uom-repair-report.json
      - ODOOUPGRADER_OPENUPGRADE_REVISION={openupgrade_revision}
    networks:
      - odooupgrader-network
    volumes:
      - ./output/filestore:/var/lib/odoo/filestore/database
      - ./output:/var/log/odoo
    restart: "no"
    entrypoint: /entrypoint.sh
    command: >
      odoo -d database
      --upgrade-path=/mnt/extra-addons/openupgrade_scripts/scripts
      --addons-path=/mnt/extra-addons{extra_addons_path_arg}
      --update all
      --stop-after-init
      --load=base,web,openupgrade_framework{custom_modules_load}
      --log-level=info
      --logfile=/var/log/odoo/odoo.log
networks:
  odooupgrader-network:
    external: true
    name: {self.NETWORK_NAME}
"""
        with open(self.UPGRADE_COMPOSE_FILE, "w", newline='\n') as f:
            f.write(compose_content.strip() + "\n")

        self._run_cmd(["docker", "rm", "-f", self.odoo_container], check=False, capture_output=True)

        cmd_up = self._compose(
            self.UPGRADE_COMPOSE_FILE, "up", "--build", "--abort-on-container-exit"
        )

        with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TimeElapsedColumn(),
                console=console
        ) as progress:
            task = progress.add_task(f"[bold magenta]Upgrading to {target_version}...", total=None)

            try:
                process = subprocess.Popen(
                    cmd_up,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )

                while True:
                    output = process.stdout.readline()
                    if output == '' and process.poll() is not None:
                        break
                    if output:
                        line = output.strip()
                        if self.verbose:
                            console.print(f"[dim]{line}[/dim]")
                        logger.debug(line)

                stderr_output = process.stderr.read() if process.stderr else ""
                self._upgrade_stderr = redact(stderr_output or "")
                if stderr_output:
                    logger.error(f"Container Error Stream: {stderr_output}")

                if process.returncode != 0:
                    console.print("[bold red]Upgrade process failed.[/bold red]")
                    logger.error("Upgrade process returned non-zero exit code.")
                    if not self.verbose and stderr_output:
                        console.print(stderr_output)
                    return False

            except Exception as e:
                console.print(f"[bold red]Error running upgrade:[/bold red] {e}")
                logger.error(f"Exception during upgrade subprocess: {e}")
                return False

        try:
            res = self._run_cmd(["docker", "inspect", self.odoo_container, "--format={{.State.ExitCode}}"],
                                capture_output=True)
            exit_code = int(res.stdout.strip())
            logger.info(f"Container exit code: {exit_code}")

            if exit_code == 0:
                console.print(f"[green]Upgrade to {target_version} successful.[/green]")
                self._run_cmd(self._compose(self.UPGRADE_COMPOSE_FILE, "down"), check=False)
                return True
            else:
                console.print(f"[bold red]Container exited with code {exit_code}[/bold red]")
                return False
        except Exception as e:
            logger.error(f"Error checking exit code: {e}")
            return False

    def finalize_package(self):
        """Dumps final database and zips it."""
        console.print("[blue]Creating final package...[/blue]")
        logger.info("Creating final package...")

        dump_cmd = ["docker", "exec", self.db_container, "pg_dump", "-U", "odoo", "database"]
        try:
            with open(os.path.join(self.output_dir, "dump.sql"), "w") as f:
                subprocess.run(dump_cmd, stdout=f, check=True)
        except Exception as e:
            logger.error(f"Failed to dump database: {e}")
            raise

        zip_name = os.path.join(self.output_dir, "upgraded.zip")
        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(os.path.join(self.output_dir, "dump.sql"), "dump.sql")

            if os.path.exists(self.filestore_dir):
                for root, _, files in os.walk(self.filestore_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, self.output_dir)
                        zipf.write(file_path, arcname)

        console.print(f"[bold green]Upgrade Complete! Package available at: {zip_name}[/bold green]")
        logger.info(f"Upgrade Complete. Package: {zip_name}")
        os.remove(os.path.join(self.output_dir, "dump.sql"))

    def cleanup_artifacts(self):
        """Removes source folder and extracted filestore."""
        logger.info("Cleaning up artifacts...")
        self._cleanup_dir(self.source_dir)
        self._cleanup_dir(self.filestore_dir)
        self._cleanup_dir(self.custom_addons_dir)

    def _container_exists(self, name: str) -> bool:
        result = subprocess.run(
            ["docker", "inspect", name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _database_has_retain_flag(self) -> bool:
        """True when a previous failed run asked for the volume to be kept."""

        marker = os.path.join(self.output_dir, "retained-database.json")
        if self._container_exists(self.db_container) and os.path.isfile(marker):
            return True
        if not self._container_exists(self.db_container):
            return False
        self._run_cmd(["docker", "start", self.db_container], check=False, capture_output=True)
        result = subprocess.run(
            ["docker", "exec", self.db_container, "test", "-f", self.RETAINED_FLAG],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _remove_database_stack(self) -> None:
        """Remove a non-retained database stack left by an interrupted run."""

        logger.info("Removing leftover migration database that was not marked retained")
        if os.path.exists(self.DB_COMPOSE_FILE):
            self._run_cmd(self._compose(self.DB_COMPOSE_FILE, "down", "-v"), check=False)
        self._run_cmd(["docker", "rm", "-f", self.db_container], check=False, capture_output=True)
        self._run_cmd(["docker", "volume", "rm", "-f", self.VOLUME_NAME], check=False, capture_output=True)
        self._run_cmd(["docker", "network", "rm", self.NETWORK_NAME], check=False, capture_output=True)

    def _mark_database_retained(self) -> None:
        self._run_cmd(["docker", "start", self.db_container], check=False, capture_output=True)
        self._run_cmd(
            ["docker", "exec", self.db_container, "touch", self.RETAINED_FLAG],
            check=False,
            capture_output=True,
        )

    def _commands(self) -> dict:
        return retention_commands(
            self.db_container,
            self.COMPOSE_PROJECT,
            self.DB_COMPOSE_FILE,
        )

    def _write_retained_instructions(self) -> None:
        commands = self._commands()
        os.makedirs(self.output_dir, exist_ok=True)
        path = os.path.join(self.output_dir, "retained-database.json")
        payload = {
            "container": self.db_container,
            "compose_project": self.COMPOSE_PROJECT,
            "compose_file": self.DB_COMPOSE_FILE,
            "failed_step": self._failed_step,
            "source_version": self._source_version,
            "target_version": self.target_version,
            "commands": commands,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        console.print("[yellow]Database kept for troubleshooting.[/yellow]")
        console.print(f"Inspect: [bold]{commands['inspect_database']}[/bold]")
        console.print(f"Logs: [bold]{commands['migration_log']}[/bold]")
        console.print(f"Cleanup when finished: [bold]{commands['cleanup']}[/bold]")

    def _write_failure_report(self) -> None:
        log_path = os.path.join(self.output_dir, "odoo.log")
        log_text = ""
        if os.path.isfile(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                    log_text = handle.read()
            except OSError as exc:
                logger.warning("Could not read %s: %s", log_path, exc)
        parsed = parse_odoo_failure(log_text or self._upgrade_stderr)
        detail_parts = [self._failure_detail or "", (self._upgrade_stderr or "")[-4000:]]
        detail = "\n".join(part for part in detail_parts if part)
        repair = load_uom_repair_report(os.path.join(self.output_dir, "uom-repair-report.json"))
        dependency = None
        if self._failed_step and "->" in self._failed_step:
            dependency = get_dependency_spec(self._failed_step.split("->")[-1].strip())
        if dependency is None:
            dependency = get_dependency_spec(self.target_version)
        failure = MigrationFailure(
            source_version=self._source_version,
            target_version=self.target_version,
            failed_step=self._failed_step,
            failed_module=parsed["failed_module"],
            migration_script=parsed["migration_script"],
            error_type=parsed["error_type"],
            error_message=parsed["error_message"],
            error_field=parsed["error_field"],
            compatibility_patch=(repair or {}).get("compatibility_patch"),
            repair_status=(repair or {}).get("repair_status"),
            detail=detail,
            uom_categories=list((repair or {}).get("categories") or []),
            commands=self._commands() if self.keep_db_on_failure and self._db_started else None,
            openupgrade_revision=dependency.openupgrade_revision if dependency else None,
            openupgradelib_revision=dependency.openupgradelib_revision if dependency else None,
        )
        if not failure.error_message and self._failure_detail:
            failure.error_message = self._failure_detail
        try:
            paths = write_failure_report(self.output_dir, failure)
            logger.info("Wrote migration report %s", paths["json"])
            console.print(f"[yellow]Migration report:[/yellow] {paths['markdown']}")
        except OSError as exc:
            logger.error("Could not write migration report: %s", exc)

    def cleanup(self, preserve_database: bool = False):
        """Docker cleanup. A retained database is not removed."""
        console.print("[dim]Cleaning up Docker environment...[/dim]")
        logger.info("Cleaning up Docker environment (preserve_database=%s)", preserve_database)
        self._run_cmd(["docker", "rm", "-f", self.odoo_container], check=False, capture_output=True)
        if preserve_database:
            if not self._database_retained:
                self._mark_database_retained()
                self._write_retained_instructions()
            else:
                commands = self._commands()
                console.print("[yellow]A retained migration database already exists and was not deleted.[/yellow]")
                console.print(f"Inspect: [bold]{commands['inspect_database']}[/bold]")
                console.print(f"Cleanup when finished: [bold]{commands['cleanup']}[/bold]")
            for filename in ["Dockerfile", self.UPGRADE_COMPOSE_FILE]:
                if os.path.exists(filename):
                    os.remove(filename)
            return

        if os.path.exists(self.DB_COMPOSE_FILE):
            self._run_cmd(self._compose(self.DB_COMPOSE_FILE, "down", "-v"), check=False)
        else:
            self._run_cmd(["docker", "rm", "-f", self.db_container], check=False, capture_output=True)

        for filename in ["Dockerfile", self.UPGRADE_COMPOSE_FILE, self.DB_COMPOSE_FILE]:
            if os.path.exists(filename):
                os.remove(filename)

    def run(self):
        try:
            logger.info("Starting OdooUpgrader...")
            if self.target_version not in self.VALID_VERSIONS:
                self._failure_detail = f"Invalid version. Supported: {self.VALID_VERSIONS}"
                console.print(f"[bold red]{self._failure_detail}[/bold red]")
                sys.exit(1)

            if self._database_has_retain_flag():
                self._database_retained = True
                self._failure_detail = (
                    "A retained migration database already exists. "
                    "It was not deleted. Inspect it, then run the cleanup command."
                )
                console.print(f"[bold red]{self._failure_detail}[/bold red]")
                commands = self._commands()
                console.print(f"Inspect: [bold]{commands['inspect_database']}[/bold]")
                console.print(f"Cleanup when finished: [bold]{commands['cleanup']}[/bold]")
                sys.exit(1)
            elif self._container_exists(self.db_container):
                self._remove_database_stack()

            self.validate_source_accessibility()
            self.prepare_environment()
            self.process_extra_addons()

            self.create_db_compose_file()

            self._run_cmd(self._compose(self.DB_COMPOSE_FILE, "up", "-d"))
            self._db_started = True
            self.wait_for_db()

            local_source = self.download_or_copy_source()
            file_type = self.process_source_file(local_source)

            if local_source != self.source and os.path.exists(local_source):
                os.remove(local_source)

            self.restore_database(file_type)

            current_ver_str = self.get_current_version()
            if not current_ver_str:
                self._failure_detail = "Could not determine database version."
                console.print(f"[bold red]{self._failure_detail}[/bold red]")
                logger.error(self._failure_detail)
                sys.exit(1)

            self._source_version = current_ver_str
            console.print(f"[bold blue]Current Database Version: {current_ver_str}[/bold blue]")
            logger.info(f"Current Database Version: {current_ver_str}")

            current_ver = self.get_version_info(current_ver_str)
            target_ver = self.get_version_info(self.target_version)
            min_ver = self.get_version_info("10.0")

            if current_ver < min_ver:
                self._failure_detail = "Source database version is below 10.0. Not supported."
                console.print(f"[bold red]{self._failure_detail}[/bold red]")
                sys.exit(1)

            while True:
                current_ver = self.get_version_info(current_ver_str)

                if current_ver.major == target_ver.major:
                    console.print("[green]Target version reached![/green]")
                    self.finalize_package()
                    self.cleanup_artifacts()
                    self._succeeded = True
                    break
                elif current_ver.major > target_ver.major:
                    console.print("[yellow]Current version is already higher than target.[/yellow]")
                    self.finalize_package()
                    self.cleanup_artifacts()
                    self._succeeded = True
                    break
                else:
                    next_ver_str = self.generate_next_version(current_ver_str)
                    self._failed_step = f"{current_ver.major}.0 -> {next_ver_str}"

                    if not self.run_upgrade_step(next_ver_str):
                        self._failure_detail = (
                            f"Upgrade process failed while migrating {self._failed_step}."
                        )
                        console.print("[bold red]Aborting sequence.[/bold red]")
                        sys.exit(1)

                    current_ver_str = self.get_current_version()
                    console.print(f"[blue]Database is now at version: {current_ver_str}[/blue]")

        except KeyboardInterrupt:
            self._failure_detail = "Operation cancelled by user."
            console.print("[bold red]Operation cancelled by user.[/bold red]")
            logger.info("Operation cancelled by user")
            if self._db_started:
                self._write_failure_report()
        except SystemExit:
            if not self._succeeded and not self._database_retained:
                self._write_failure_report()
            raise
        except Exception as e:
            self._failure_detail = redact(str(e))
            console.print(f"[bold red]Unexpected error:[/bold red] {e}")
            logger.exception("Unexpected error")
            if not self._database_retained:
                self._write_failure_report()
        finally:
            self.cleanup(
                preserve_database=should_preserve_database(
                    self.keep_db_on_failure,
                    self._succeeded,
                    self._db_started,
                    self._database_retained,
                )
            )