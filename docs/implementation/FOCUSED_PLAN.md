# Approved focused implementation

This scope supersedes the broader Phase 2 architecture. OdooUpgrader remains a synchronous CLI installed directly on the Linux Docker host. SSH, remote execution, workers, services, queues, web/API preparation and new authentication are excluded.

1. Keep existing CLI options and Docker/OpenUpgrade transition loop. Pin and record actual OpenUpgrade/library revisions. Validate the inherited XML-ID helper before 17→18; block ambiguous 18→19 UoMs and affected bigger-unit profiles with actionable diagnostics until a reviewed upstream correction is selected. No customer-specific normalization or upstream patch is assumed.
2. Add one atomic JSON job ledger per local job, protected by Linux flock, immutable attempt history and isolated Compose resources. Keep original backups untouched. Preserve per-job logs and evidence; propagate errors.
3. Add immutable paired PostgreSQL/filestore checkpoints, integrity checks, disposable restore/registry verification, capacity guards and explicit resume of the next transition only. Interrupted/failed working volumes never become resume sources. Reviewed pending-runtime changes preserve completed history.
4. Automatically create a redacted portable failure bundle; collection is best-effort and cannot replace the original failure. Export only allowlisted metadata and sanitized logs, never backups/customer records or Docker environment secrets.
5. Add safe local automated tests for blockers, recovery, publication and diagnostics. No destructive Docker/database integration tests or deployment without explicit approval; provide exact isolated server instructions and identify untested cases.

Integration points: existing core.OdooUpgrader and cli.main. Small local helpers: jobs.py (ledger/lock/checkpoint files), diagnostics.py (safe evidence), checks.py (validation/UoM rules). No asynchronous service or external state store.
