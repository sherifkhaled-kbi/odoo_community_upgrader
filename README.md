# OdooUpgrader

A synchronous CLI installed and run **on the Linux server hosting Docker**. It upgrades backup copies through OCA OpenUpgrade, retains verified version checkpoints, and generates portable failure diagnostics. Operators arrange SSH and optionally `tmux`/`screen` externally. No remote execution, worker, service or web/API is included.

Install from the checkout using Python 3.10+:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/odooupgrader --help
```

The existing invocation remains valid and waits synchronously:

```bash
odooupgrader --source /srv/backups/test-backup.zip --version 19.0 --postgres-version auto
```

`--extra-addons`, `--verbose`, `--log-file` and URL sources remain available. A custom-format dump can supply `--filestore /srv/backups/matching-filestore`. Sources are copied, never deleted. Installed custom modules need compatible sources/dependencies for **every** intermediate version; unsupported missing modules block verification. Official Odoo image Git revisions may be unavailable and are explicitly recorded as unknown; actual image IDs and dependency inventories are recorded.

For a backup with known missing attachment files, add `--allow-missing-attachments` to a **new** upgrade command. The default rejects missing files. This option allows missing files only: unsafe attachment paths and size/checksum mismatches on present files still fail. The missing count is printed and recorded under `validation.attachments` in each checkpoint manifest (also included in the final ZIP). Missing files remain unavailable in the output and in Odoo after restore. The choice is stored with the job and reused on resume. A failure before the first checkpoint can resume only if a verified pre-migration working database was retained; otherwise start a new job from the original backup.

For limited storage, add `--single-copy` to a **new** upgrade command. This mode reads a local source backup in place, keeps one working PostgreSQL database and one working filestore, and checks that the source did not change during restore. After each successful version transition, it streams the database and filestore into one immutable checkpoint ZIP; the final ZIP is hard-linked to the final checkpoint without duplicating its bytes. No second verification database or filestore is created, and the ZIP records `independent_restore: false` in `odooupgrader-run.json`. Previous jobs and stopped Docker volumes are never deleted. This mode still needs enough free space for PostgreSQL/WAL growth, Docker images and each checkpoint ZIP, and it does not repair missing attachments.

For a known source major, add `--source-version 17.0` (adjust the version) to build and probe the first OpenUpgrade image **before** extracting or restoring the backup. The restored database version must match. Without this option, the first image is prepared after restore. In single-copy mode, a failure after restore but before Odoo starts can now resume on the same stopped working database and filestore after checking container identity, filestore hashes and technical state. The write boundary is persisted before Odoo starts. A failure after that boundary restores the last successful ZIP checkpoint; if no transition has succeeded yet, a new job from the original backup is still required. An old job created before this feature has no reusable-workspace marker and follows its existing checkpoint rules.

```bash
odooupgrader --source /opt/odoo17_backup.zip --source-version 17.0 --version 19.0 \
  --postgres-version auto --jobs-dir /opt/odooupgrader-jobs \
  --allow-missing-attachments --single-copy
```

Migration-image builds trust only the exact `/mnt/extra-addons` Git directory for the OpenUpgrade fetch. This handles Odoo base images where that pre-existing directory is owned by the `odoo` user while the build runs as root; no system-wide Git safe-directory setting is changed.

The migration-image probe records installed Python distributions even when a package has incomplete `Name` or `Version` metadata. Such entries get explicit placeholders and a count in the runtime inventory; the pinned `openupgradelib` commit check still must pass.

**PostgreSQL restore compatibility:** use `--postgres-version auto` to inspect the source-server and pg_dump versions before choosing a supported 13–18 image. A dump made by pg_dump 18 requires a PostgreSQL 18 restore server even if its source server was 16. PostgreSQL 18 uses the official image's `/var/lib/postgresql` volume target; older images use `/var/lib/postgresql/data`. An explicit older version is rejected before restore; the original dump is never rewritten. Actual server/client versions and image identity are verified. Unknown headers or newer unsupported formats block with actionable diagnostics. See the [restore compatibility correction](docs/implementation/RESTORE_COMPATIBILITY_FIX.md).

On a fresh PostgreSQL container, readiness waits for the final TCP server and a successful maintenance-database query. The image's temporary socket-only initialization server is not treated as ready for a restore.

Each run prints a UUID. State defaults to `.odooupgrader/jobs` in the invocation directory; use the same absolute `--jobs-dir` across separate shells:

```bash
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs jobs
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs status JOB_UUID
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs storage JOB_UUID
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs resume JOB_UUID --dry-run
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs resume JOB_UUID
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs diagnostics JOB_UUID --output ./failure-review.zip
```

A failed 18→19 transition resumes from the verified 18 checkpoint in a **fresh** database/filestore. A pre-migration failure can reuse the same verified working database instead. Completed 16→17 and 17→18 transitions are retained. Missing/corrupt checkpoints block; there is no silent fallback. `storage` reports allocated job/checkpoint/attempt bytes, accessible job-owned PostgreSQL volume and WAL bytes, and known Docker image virtual sizes. Image sizes include shared layers and must not be added as exact physical usage. Closing or losing the terminal may interrupt execution; use an external terminal multiplexer if desired:

```bash
tmux new -s odoo-upgrade
# Run the CLI inside tmux; detach with Ctrl-B then D.
# Later: tmux attach -t odoo-upgrade
```

Completed output is printed as a per-job attempt path ending in `output/upgraded.zip`. It is no longer written over the invocation directory's `output/`; this protects original backups and previous runs. Exit 0 means completed migration (or successful query/dry-run), 1 means execution/recovery failure, 2 means CLI usage error. Ctrl-C aborts the foreground run; a subsequent explicit resume reconciles labeled resources. `status` reports recorded state and observed interruption separately.

OpenUpgrade 17/18/19 defaults are exact research revisions listed in [known issues](docs/implementation/KNOWN_ISSUES.md). Older transitions require an explicit `--openupgrade-revision VERSION=FULL_SHA` and have not been integration-qualified. Full-image reproducibility is limited by official image tags and resolver inputs; checkpoints preserve actual image IDs and resolved provenance, and resume requires the producer image to remain available for verification.

Pending runtime corrections require a visible diff and exact acceptance:

```bash
odooupgrader resume JOB_UUID --dry-run --openupgrade-revision 19.0=REVIEWED_FULL_SHA
odooupgrader resume JOB_UUID --openupgrade-revision 19.0=REVIEWED_FULL_SHA \
  --accept-runtime-change DIFF_SHA256_FROM_DRY_RUN --reason 'Reviewed correction and regression evidence'
```

If a rebuilt image/dependency inventory differs, the run stops before migration and records `built_runtime_candidates`; review and accept that exact diff separately using the same acceptance option. There is no force switch or automatic UoM normalization. Duplicate/absent reference UoMs block. The default 19.0 script also blocks affected custom bigger units; the inspected PR #6054 correction is only usable when explicitly selected and its exact script hash matches. It remains an unmerged candidate requiring server-side qualification, not a default upgrade. Do not alter source factors to work around it.

A `pip check` conflict already present in the pristine `odoo:<version>` base image (unrelated to this application) no longer blocks migration; only a conflict newly introduced by this application's own build is fatal. The capability probe and module-disappearance check also recognize OpenUpgrade's own pinned module rename/merge registry (`apriori.py`), so an upstream-documented merge like `account_payment_term` into `account` is not treated as data loss. A real, open upstream defect remains unresolved: 18.0→19.0 on PostgreSQL 17+ fails `stock_account`'s post-migration script via `openupgradelib`'s `lift_constraints` ([OCA/openupgradelib#462](https://github.com/OCA/openupgradelib/pull/462), unmerged); see [the runtime/module fix notes](docs/implementation/RUNTIME_DEPENDENCY_AND_MODULE_FIXES.md).

Read [recovery and storage](docs/implementation/RECOVERY.md), [diagnostics/privacy](docs/implementation/DIAGNOSTICS.md), [known issues](docs/implementation/KNOWN_ISSUES.md), [runtime/dependency/module fixes](docs/implementation/RUNTIME_DEPENDENCY_AND_MODULE_FIXES.md), and [test results/server instructions](docs/implementation/TEST_REPORT.md). The [focused plan](docs/implementation/FOCUSED_PLAN.md) supersedes the earlier architecture proposal.

Safe local tests:

```bash
.venv/bin/python -m pytest -q
```

Real Docker/database tests are opt-in and require explicit authorization for disposable resources. Local unit/fault tests do not certify actual Odoo migrations or business-data correctness.

For PostgreSQL `53100` / `No space left on device`, see [restore storage exhaustion](docs/implementation/RESTORE_STORAGE_FAILURE.md). Plain SQL is streamed instead of copied into Docker, initial capacity is checked, and storage failures have explicit safe diagnostics. Host disk/volume capacity still needs to be sufficient.
