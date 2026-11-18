# PostgreSQL restore compatibility correction

## PostgreSQL 18 extension (2026-10-31)

A later backup reported source server 16.13 but `pg_dump` 18.6 and `SET transaction_timeout = 0;`. The producing client version determines the minimum restore major, so `auto` now selects PostgreSQL 18 for this input. The adapter admits 13–18 and still checks the actual server, `psql`, `pg_restore` and image identity before restore. It does not edit the SQL. The [PostgreSQL 18 pg_dump documentation](https://www.postgresql.org/docs/18/app-pgdump.html) explains why newer dumps are not guaranteed to load into older servers.

The [official PostgreSQL Docker image documentation](https://hub.docker.com/_/postgres) changes `PGDATA` to `/var/lib/postgresql/18/docker` and the volume target to `/var/lib/postgresql` for 18. Compose mount and capacity inspection use that target for 18; the 13–17 target remains `/var/lib/postgresql/data`. The original support and test results below describe the earlier 13–17 release. PostgreSQL 18 restore and full Odoo migration have not been exercised against this customer backup in the development workspace.

## Root cause established before editing

Baseline repository: application commit `96234b3` (`resolve`). The original `RecoveryMixin.create_db_compose_file()` used `postgres:<self.postgres_version>` without inspecting the backup. CLI default was 13; the operator confirmed **16** for this failed job. The image's exact remote patch version/digest was not supplied and was not invented.

The executed `psql -f /tmp/input.dump` path identifies plain SQL (in this application's normal source path, `dump.sql` extracted from a ZIP). The reported `SET transaction_timeout` failure requires PostgreSQL 17-era syntax, which PostgreSQL 16 rejects. This strongly indicates a newer dump producer, but does **not** establish the source database's major: pg_dump 17 can dump PostgreSQL 16 and still emit this setting. The actual customer's dump header is unavailable in this workspace. New preflight records the two versions independently.

`ON_ERROR_STOP=1` plus `--single-transaction` correctly aborted and rolled back the restore. The later `ir_module_module does not exist` arose because original `failure_evidence()` unconditionally queried Odoo module tables despite an unsuccessful initial restore. It is a secondary diagnostic failure, not evidence that an Odoo migration ran.

PostgreSQL explicitly does not guarantee loading newer pg_dump output into an older server, even when the source database was older. Therefore this fix never deletes `transaction_timeout`, rewrites SQL syntax, suppresses errors or retries the partially restored database. [pg_dump compatibility policy](https://www.postgresql.org/docs/17/app-pgdump.html), [transaction_timeout setting](https://www.postgresql.org/docs/17/runtime-config-client.html), [psql error/transaction flags](https://www.postgresql.org/docs/17/app-psql.html).

## Implemented correction

- New `postgres_restore.py` inspects actual bytes, with a bounded header read. It recognizes pg_dump plain SQL and custom `PGDMP` archives; ZIPs are safely extracted to the private working input before inspection. Database names are discarded from custom metadata. The parser follows PostgreSQL's [ReadHead/ReadInt header layout](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/bin/pg_dump/pg_backup_archiver.c).
- Record source server version and producing pg_dump version separately. Both must be identifiable; unknown/truncated headers, compressed plain SQL, tar/directory archives and unsupported archive versions fail closed. A backup manifest's source-only PG version cannot override the actual dump producer.
- Before starting the database container, choose a runtime at least as new as both versions and known required features. Numeric selections are explicit and never silently upgraded. PostgreSQL 16 with a 17-produced dump gets an actionable `DATABASE_RESTORE_FAILURE` directing a new job to `auto` or 17.
- New `--postgres-version auto` selects the compatible minimum within the adapter's supported **13–17** range. CLI default 13 is retained for compatibility; use `auto` for new jobs when the producer version is uncertain. Required newer versions, including 18+, are rejected pending validated adapter support; no assumption is made about their Docker volume layout or Odoo qualification.
- Before each restore, query actual server version/number using the administrative `postgres` database, plus `psql --version` and `pg_restore --version`; record actual image reference/ID. All three majors must match the selected runtime. This is independent of trusting an image tag. No Odoo table is queried for this check.
- Read a custom archive's TOC using the actual selected pg_restore client before creating the target database. Restore into a fresh `template0` database, preserving fatal SQL/custom error handling and a single transaction. Compatible versions do not waive extension, permission, syntax or schema errors.
- Persist selected runtime and restore history in the local job. Automatic selection is reused on resume; producer checkpoint bytes, parent pointers and completed transitions are unchanged. Checkpoint verification uses the same compatibility gate. A failed restore never advances a checkpoint or publishes a final artifact.

A version-compatible runtime is necessary, not universal compatibility proof. Headers cannot establish extension availability, every SQL feature or Odoo business-data correctness; those failures still stop execution. Header-less SQL cannot be safely admitted by assuming that a single statement is its only incompatibility.

## Diagnostics

Failures in preflight or actual restore classify as **DATABASE_RESTORE_FAILURE**, with a specific reason (`POSTGRES_VERSION_MISMATCH`, unsupported format/version, command failure, etc.). `restore_diagnostics.json` contains source/container format, source server/pg_dump versions, requested/selected version, actual runtime where started, phase, exit code/SQLSTATE, and restore history.

`failure_summary.json` carries that classification and restore-specific facts. It does not attribute restore failures to an Odoo module from an earlier log. `module_status.json` is `{available: false, reason: DATABASE_RESTORE_FAILURE}`; no query against `ir_module_module`, `ir_attachment` or other Odoo tables runs after failed restoration. Before a restore has succeeded, even unrelated failure diagnostics mark module evidence unavailable.

SQL restore adds `--echo-errors` and verbose error reporting. Bounded raw failed SQL/error output is retained only in owner-private `restore.private.log`; the original exception chain is preserved. Portable evidence can include the exact known timeout `SET` statement and the `transaction_timeout` error, but business SQL, arbitrary literals and customer error payloads are withheld. Preflight reports an **incompatible** statement, not an executed failing statement. Collector/write failures never replace the restore exception.

## Operator recovery for this incident

After deploying this patch on the server, start a new job from the unchanged backup because the initial restore failed before CP0 existed:

```bash
cd /opt/odoo_community_upgrader
.venv/bin/odooupgrader \
  --source /opt/odoo17_backup \
  --version 19.0 \
  --postgres-version auto \
  --jobs-dir /opt/odooupgrader-jobs
```

If headers confirm a supported 17-produced dump, auto selects 17. Explicit `--postgres-version 17` is also valid for that case. If the dump requires 18+, the application blocks with the reason instead of guessing. If file attachments are stored separately, include the matching `--filestore` directory; custom modules still require the appropriate sources.

Do not edit the failed job's PostgreSQL version or checkpoint ledger. Keep its diagnostics. If a different job already has a valid checkpoint, use its existing `resume --dry-run` and reviewed application/runtime-change acceptance; no completed step needs to be rerun because this code changed. Source or format errors before CP0 require a new explicit job.

## Files and rollback

Changed: `src/odooupgrader/recovery.py`, `cli.py`, `diagnostics.py`; new `postgres_restore.py`. Regression coverage: `tests/test_postgres_restore.py`, updated command/orchestration tests and new native/Docker restore-only tests under `tests/integration/`. README and recovery/diagnostics documentation describe admission and operation.

Rollback: retain job metadata, immutable inputs/checkpoints and actual producer images. Revert the application only with a ledger-compatible version; do not revert PostgreSQL in an existing data volume or edit dumps/ledger pointers. Unknown or incompatible restore state always starts from a new working copy, never the failed database. No remote execution, worker, web/API or customer-specific repair was introduced.

## Executed verification

Final command (with approved access outside the sandbox for private sockets/Docker):

```bash
.venv/bin/python -m pytest -q \
  --run-native-postgres-tests --run-docker-restore-tests -rA
```

**Result: 101 passed, 2 skipped in 68.30 seconds.** The skipped cases are the two full Odoo upgrade chains, which require a separate synthetic Odoo backup and qualification. They are not claimed to pass.

Actual database tests:

- **PostgreSQL 16.15, private temporary native cluster, TCP disabled:** reproduced `transaction_timeout` rejection and confirmed preceding CREATE rolled back; successfully restored a compatible plain dump and a real pg_dump16 custom archive; induced an independent syntax failure and verified primary diagnostics, no Odoo-table queries, no checkpoint publication and unchanged input hashes.
- **Docker `postgres:17`, actual server/psql/pg_restore 17.11:** auto selected 17 from synthetic 17-produced-format SQL; actual low-level restore succeeded and returned the expected synthetic row. A real custom archive generated by that container reported archive 1.16.0/source 17.11/pg_dump 17.11 and restored successfully into another fresh test database.
- Docker image ID: `sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f`. Machine-readable result: `/tmp/ou-restore-docker17-ae484ivq/test-result.json`. Stopped native cluster: `/tmp/ou-restore-pg16-0jdmqlbq`. All are developer-machine test evidence, not the customer's server inventory.
- Unit/fault tests cover requested/actual server-client mismatch, exporter newer than source, removal/absence of transaction_timeout still not permitting downgrade, unknown/truncated/future format, custom TOC failure, failed CREATE/restore, collector failures preserving the primary exception, and a full initial-preflight refusal that creates no Docker resources or Odoo queries. Existing resume/checkpoint/locking/history/privacy regressions pass.

The first native test attempt was blocked by the sandbox's Unix-socket restriction; the same tests passed with approved execution access. Docker access likewise required execution outside the sandbox. No software failure remains in the final run. No customer backup/database was used. Tests created only new synthetic resources and executed no DROP, Docker rm, volume deletion or prune operation; their own servers/containers were stopped and data retained. No deployment or change to the Ubuntu server was performed.

Syntax compilation and `git diff --check` passed. Actual Odoo 17→18→19 migration, customer extensions/schema, complete business validation, PostgreSQL 18+ runtime support and the original remote dump's exact source/exporter versions remain outside these verified results.

To rerun only the real restore checks:

```bash
.venv/bin/python -m pytest tests/integration/test_restore_postgres_native.py \
  --run-native-postgres-tests -q
.venv/bin/python -m pytest tests/integration/test_restore_postgres_docker.py \
  --run-docker-restore-tests -q
```

The native test expects PostgreSQL 16 binaries at `/usr/lib/postgresql/16/bin` and an unprivileged user. The Docker test requires local Docker/Compose, may pull `postgres:17`, creates new labeled test resources and retains them stopped. Repeated runs consume storage; cleanup is a separately approved action, never a broad prune. These opt-in commands do not run customer migrations or use the remote backup automatically.
