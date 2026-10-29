"""Failure reports and the decision to keep a database volume."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_SECRET_RE = re.compile(
    r"(?i)((?:postgres_)?(?:password|passwd)|secret|token|api[_-]?key)(\s*[=:]\s*)(\S+)"
)
_SCRIPT_RE = re.compile(r"scripts/([a-z0-9_]+)/([0-9.]+/[\w./-]+)")
_FIELD_RE = re.compile(r"Wrong value for ([^:\s]+)")
_ERROR_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):")


def should_preserve_database(
    keep_db_on_failure: bool,
    succeeded: bool,
    db_started: bool,
    already_retained: bool,
) -> bool:
    """Return whether cleanup must leave the PostgreSQL volume in place."""

    if already_retained:
        return True
    return bool(keep_db_on_failure and (not succeeded) and db_started)


def retention_commands(container: str, compose_project: str, compose_file: str) -> Dict[str, str]:
    """Exact commands for inspecting and later deleting a retained database."""

    return {
        "inspect_database": f"docker exec -it {container} psql -U odoo -d database",
        "database_logs": f"docker logs {container}",
        "migration_log": "output/odoo.log",
        "report_markdown": "output/migration-report.md",
        "report_json": "output/migration-report.json",
        "cleanup": (
            f"docker compose -p {compose_project} -f {compose_file} down -v"
            " && rm -f output/retained-database.json"
        ),
    }


def redact(text: str) -> str:
    """Remove credential-like assignments. Database names and SQL stay intact."""

    return _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)


def parse_odoo_failure(log_text: str) -> Dict[str, Optional[str]]:
    """Pull the last migration error out of an Odoo log."""

    redacted = redact(log_text or "")
    result: Dict[str, Optional[str]] = {
        "failed_module": None,
        "migration_script": None,
        "error_type": None,
        "error_message": None,
        "error_field": None,
    }
    script_matches = list(_SCRIPT_RE.finditer(redacted))
    if script_matches:
        last = script_matches[-1]
        result["failed_module"] = last.group(1)
        result["migration_script"] = f"{last.group(1)}/{last.group(2)}"
    field_matches = list(_FIELD_RE.finditer(redacted))
    if field_matches:
        result["error_field"] = field_matches[-1].group(1)
    lines = [line.strip() for line in redacted.splitlines() if line.strip()]
    for line in reversed(lines):
        match = _ERROR_LINE_RE.match(line)
        if match:
            result["error_type"] = match.group(1).split(".")[-1]
            result["error_message"] = line
            break
    if result["error_type"] is None and "AttributeError" in redacted:
        result["error_type"] = "AttributeError"
        for line in reversed(lines):
            if "AttributeError" in line:
                result["error_message"] = line
                break
    return result


def load_uom_repair_report(path: str) -> Optional[dict]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


@dataclass
class MigrationFailure:
    """Structured result of a migration that did not finish."""

    source_version: Optional[str]
    target_version: str
    failed_step: Optional[str]
    failed_module: Optional[str] = None
    migration_script: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    error_field: Optional[str] = None
    compatibility_patch: Optional[str] = None
    repair_status: Optional[str] = None
    detail: Optional[str] = None
    uom_categories: List[dict] = field(default_factory=list)
    commands: Optional[Dict[str, str]] = None
    openupgrade_revision: Optional[str] = None
    openupgradelib_revision: Optional[str] = None

    def as_dict(self) -> dict:
        payload = {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "failed_step": self.failed_step,
            "failed_module": self.failed_module,
            "migration_script": self.migration_script,
            "error_type": self.error_type,
            "error_message": redact(self.error_message or ""),
            "error_field": self.error_field,
            "compatibility_patch": self.compatibility_patch,
            "repair_status": self.repair_status,
            "detail": redact(self.detail or ""),
            "openupgrade_revision": self.openupgrade_revision,
            "openupgradelib_revision": self.openupgradelib_revision,
        }
        if self.uom_categories:
            payload["uom_categories"] = self.uom_categories
        if self.commands:
            payload["commands"] = self.commands
        return payload


def render_markdown(failure: MigrationFailure) -> str:
    data = failure.as_dict()
    lines = [
        "# Migration report",
        "",
        f"- Source version: {data['source_version']}",
        f"- Target version: {data['target_version']}",
        f"- Failed step: {data['failed_step']}",
        f"- Failed module: {data['failed_module']}",
        f"- Migration script: {data['migration_script']}",
        f"- Error type: {data['error_type']}",
        f"- Error field: {data['error_field']}",
        f"- Compatibility patch: {data['compatibility_patch']}",
        f"- Repair status: {data['repair_status']}",
        f"- OpenUpgrade revision: {data['openupgrade_revision']}",
        f"- openupgradelib revision: {data['openupgradelib_revision']}",
        "",
        "## Error",
        "",
        data["error_message"] or data["detail"] or "The migration process returned a failure.",
        "",
        "Migration log: `output/odoo.log`",
        "",
    ]
    if failure.uom_categories:
        lines.extend(["## UoM categories", ""])
        for category in failure.uom_categories:
            lines.append(
                f"### Category {category.get('legacy_category_id')} "
                f"({category.get('repair_decision')})"
            )
            lines.append("")
            lines.append(str(category.get("explanation") or ""))
            lines.append("")
            lines.append(f"- Canonical reference: {category.get('canonical_reference_id')}")
            lines.append(f"- Reference candidates: {category.get('reference_candidate_ids')}")
            lines.append(f"- Affected UoM ids: {category.get('affected_uom_ids')}")
            lines.append("")
            lines.append("| id | type | factor | factor problem | xml ids |")
            lines.append("| --- | --- | --- | --- | --- |")
            for unit in category.get("units") or []:
                xml_ids = ", ".join(unit.get("xml_ids") or [])
                lines.append(
                    f"| {unit.get('id')} | {unit.get('uom_type')} | {unit.get('factor')} | "
                    f"{unit.get('factor_problem')} | {xml_ids} |"
                )
            lines.append("")
    if failure.commands:
        lines.extend(["## Retained database", ""])
        lines.append("The PostgreSQL volume was kept. Inspect it with:")
        lines.append("")
        lines.append("```bash")
        lines.append(failure.commands["inspect_database"])
        lines.append(failure.commands["database_logs"])
        lines.append("```")
        lines.append("")
        lines.append(f"Migration log: `{failure.commands['migration_log']}`")
        lines.append("")
        lines.append("After troubleshooting, remove the retained database with:")
        lines.append("")
        lines.append("```bash")
        lines.append(failure.commands["cleanup"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def write_failure_report(output_dir: str, failure: MigrationFailure) -> Dict[str, str]:
    """Write ``migration-report.json`` and ``migration-report.md``."""

    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "migration-report.json")
    md_path = os.path.join(output_dir, "migration-report.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(failure.as_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(failure))
    return {"json": json_path, "markdown": md_path}
