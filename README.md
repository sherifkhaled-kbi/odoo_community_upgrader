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

For a backup with known missing attachment files, add `--allow-missing-attachments` to a **new** upgrade command. The default rejects missing files. This option allows missing files only: unsafe attachment paths and size/checksum mismatches on present files still fail. The missing count is printed and recorded under `validation.attachments` in each checkpoint manifest (also included in the final ZIP). Missing files remain unavailable in the output and in Odoo after restore. The choice is stored with the job and reused on resume. A job that failed before its first checkpoint cannot be resumed; start a new job with the option and the original backup.

For limited storage, add `--single-copy` to a **new** upgrade command. This mode reads a local source backup in place, keeps one working PostgreSQL database and one working filestore, and checks that the source did not change during restore. After each successful version transition, it streams the database and filestore into one immutable checkpoint ZIP; the final ZIP is hard-linked to the final checkpoint without duplicating its bytes. A failed job can resume from its latest completed ZIP checkpoint. No second verification database or filestore is created, and the ZIP records `independent_restore: false` in `odooupgrader-run.json`. A failure before the first successful transition still requires a new job. Previous jobs and stopped Docker volumes are never deleted. This mode still needs enough free space for PostgreSQL/WAL growth, Docker images and each checkpoint ZIP, and it does not repair missing attachments.

**PostgreSQL restore compatibility:** use `--postgres-version auto` to inspect the source-server and pg_dump versions before choosing a supported 13–17 image. An explicit older version is rejected before restore; the original dump is never rewritten. Actual server/client versions and image identity are verified. Unknown headers or newer unsupported formats block with actionable diagnostics. See the [restore compatibility correction](docs/implementation/RESTORE_COMPATIBILITY_FIX.md).

Each run prints a UUID. State defaults to `.odooupgrader/jobs` in the invocation directory; use the same absolute `--jobs-dir` across separate shells:

```bash
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs jobs
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs status JOB_UUID
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs resume JOB_UUID --dry-run
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs resume JOB_UUID
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs diagnostics JOB_UUID --output ./failure-review.zip
```

A failed 18→19 transition resumes from the verified 18 checkpoint in a **fresh** database/filestore. Completed 16→17 and 17→18 transitions are retained. Missing/corrupt checkpoints block; there is no silent fallback. Closing or losing the terminal may interrupt execution; use an external terminal multiplexer if desired:

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

Read [recovery and storage](docs/implementation/RECOVERY.md), [diagnostics/privacy](docs/implementation/DIAGNOSTICS.md), [known issues](docs/implementation/KNOWN_ISSUES.md), and [test results/server instructions](docs/implementation/TEST_REPORT.md). The [focused plan](docs/implementation/FOCUSED_PLAN.md) supersedes the earlier architecture proposal.

Safe local tests:

```bash
.venv/bin/python -m pytest -q
```

Real Docker/database tests are opt-in and require explicit authorization for disposable resources. Local unit/fault tests do not certify actual Odoo migrations or business-data correctness.

For PostgreSQL `53100` / `No space left on device`, see [restore storage exhaustion](docs/implementation/RESTORE_STORAGE_FAILURE.md). Plain SQL is streamed instead of copied into Docker, initial capacity is checked, and storage failures have explicit safe diagnostics. Host disk/volume capacity still needs to be sufficient.
