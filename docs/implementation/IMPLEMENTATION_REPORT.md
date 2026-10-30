> Initial CLI implementation report. For the subsequent PostgreSQL restore correction and current validation, see [RESTORE_COMPATIBILITY_FIX.md](RESTORE_COMPATIBILITY_FIX.md).

# Focused implementation handover

The approved scope replaces the earlier remote-worker proposal. The application remains a synchronous Linux-host CLI using local Docker. No SSH, remote orchestration, worker, systemd service, queue, external job database, web/API or authentication feature was added.

## Delivered

- [Focused plan](FOCUSED_PLAN.md): dependencies and integration points for the three approved capabilities.
- [CLI usage](../../README.md): original options plus jobs/status/resume/dry-run/diagnostics, revision acceptance and external tmux use.
- [Recovery/checkpoints](RECOVERY.md): local atomic JSON authority, kernel lock, immutable verified snapshots, safe fresh restore, attempt history and retention.
- [Diagnostics specification](DIAGNOSTICS.md): automatic failure ZIP, protected raw evidence, allowlisted portable output, readonly DB evidence and best-effort collection.
- [Known-issue report](KNOWN_ISSUES.md): exact source revisions, root causes, implemented corrections, tests, evidence-based blockers and rollback for each fix.
- [Test execution/server instructions](TEST_REPORT.md): 71 passed, 2 explicitly skipped real Docker cases; exact approval-gated server commands and limitations.

## Files changed

| Files | Purpose |
| --- | --- |
| Existing `src/odooupgrader/core.py` | Retain input/addon preparation and version helpers; integrate synchronous recovery hooks, safe extraction/custody and fatal errors; remove destructive old run/cleanup path |
| Existing `src/odooupgrader/cli.py` | Preserve legacy upgrade flags and foreground execution; add local query/resume/diagnostics commands and clear nonzero failures |
| Existing `src/odooupgrader/__init__.py` | Version 0.4.0 matching package metadata |
| New `src/odooupgrader/recovery.py` | Existing Docker pipeline's execution/recovery hooks, exact OCA/library provenance, owned resources, restore verification, registry/filestore gates and runtime drift acceptance |
| New `src/odooupgrader/jobs.py` | Atomic local ledger, attempts, flock, immutable snapshot files/manifests, authoritative publication and corruption/liveness checks |
| New `src/odooupgrader/checks.py` | Safe ZIP extraction, UoM admission/equivalence and attachment checks |
| New `src/odooupgrader/diagnostics.py` | Redaction, structural traceback/log evidence, automatic allowlisted portable archive |
| New `pyproject.toml`; existing `README.md`, `.gitignore` | Installable CLI/test metadata, documented behavior and private-job exclusion from Git |
| New `tests/test_*.py`, `tests/conftest.py`, `tests/integration/test_local_docker.py`, `tests/fixtures/uom_*.py` | Unit/fault tests, actual pinned upstream-function regressions and opt-in server qualification; upstream fixtures retain licensing |
| New `docs/implementation/*.md`; historical `docs/phase2/*.md` banners | Current scoped plan/usage/reports; clearly supersede excluded worker/SSH architecture while retaining prior evidence |

## Decisions and compatibility

Atomic JSON plus one persistent per-job flock is sufficient for this foreground, single-host application. Each attempt has isolated Docker resources; no external control database or worker is required. Database dump plus complete filestore is the durable boundary. Registry checks run on disposable restored copies, never the partially migrated failed DB. Unknown/unpublished files never advance the pointer; corrupted latest checkpoints block instead of falling back.

The legacy command remains synchronous. Artifacts now live in private per-job attempt output, with the exact path printed, rather than overwriting CWD `output/`. Downgrade requests fail. Earlier CLI target choices remain recognized, but older transitions require an explicit OCA commit and are not integration-qualified. Custom addons must be compatible at all intermediate stages; retained addon changes require a new job. Unexpected module disappearance blocks pending a reviewed upstream mapping; no generic rename/merge implementation is claimed.

OpenUpgrade/library commits, actual migration image IDs and Python/dependency/PG inventories are recorded. Official Odoo Git revision is explicitly unavailable when unprovable. Planned pending changes and actual rebuilt-image/dependency drift need exact review acceptance, with immutable completed history. The candidate UoM fix is hash-recognized but never automatically selected; no upstream patch or source-data normalization was installed.

## Verification and limits

Executed: **72 passed, 2 skipped**, Python syntax compilation, package/version/entry-point consistency, CLI help and whitespace checks. No dependencies were installed. Package wheel build is untested because build tooling is absent. No Docker container, database restore/migration, deployment, customer/production modification or destructive integration test was performed by Codex.

The local tests establish recovery/control-flow and source-script mechanics. They do not establish actual Docker/Odoo/PG compatibility, database restoreability on the server, financial/stock integrity or final Odoo backup-manager import compatibility. Those remain explicit server qualification gates. Ambiguous UoM data and the historical 17→18 incident need protected source evidence before they can be called resolved. Stopped resources/checkpoints accumulate until an approved retention action; no automatic pruning or backup service is included.

Implementation is ready for an explicitly authorized disposable Linux-server qualification. It is not a claim of production migration readiness. Do not deploy, run destructive recovery tests or modify production data without the approval required by the scope correction.
