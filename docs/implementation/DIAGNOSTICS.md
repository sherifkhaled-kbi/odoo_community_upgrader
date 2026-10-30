# Failure diagnostics bundle specification

Every failure after an attempt is allocated triggers best-effort collection. Missing/corrupt checkpoint, runtime mismatch, Python/OpenUpgrade errors, SQL/restore failure, Docker/dependency failure and interruption all use the same path. If job metadata cannot be created/written at all (for example full disk), evidence may be unavailable. Invalid CLI arguments are usage errors before a migration starts.

The original exception is re-raised with its chain. A collector failure is logged and cannot replace it. Full private Python traceback is saved under the protected attempt directory; raw Docker/SQL logs stay local. The CLI prints the ZIP location. Re-export an existing bundle without Docker or registry access:

```bash
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs diagnostics JOB_UUID
odooupgrader --jobs-dir /srv/odoo-upgrade-jobs diagnostics JOB_UUID --output ./failure-review.zip
```

Export refuses to overwrite an existing path. Archives/directories are private; generated files are `0600`, job parents `0700`. Treat even sanitized bundles as restricted technical evidence.

## Archive members

- `job_metadata.json`: job/source/target/stage/state and completed checkpoint identities/hashes.
- `failure_summary.json`: attempt, transition, error type, stable safety code when available, process exit/SQLSTATE when available, and last identifiable upgrading module. Root cause is explicitly unverified.
- `environment.json`: host Python and platform; runtime Python/PG/dependencies are in the revision evidence when collection reached that stage.
- `migration_steps.json`: attempt outcomes, transition history, timestamps and checkpoint inputs.
- `error_traceback.txt`: complete available Python frame sequence and chained exception types, with basename/line/function; source literals and exception payloads withheld.
- `migration.log`, `docker.log`: structural excerpts (technical module stages, frame locations and exception classes) from the final 2 MiB of each available log. Missing logs are explicit.
- `openupgrade_revision.json`: planned exact OCA/library/app revisions and actual runtime image ID, dependency inventory and PostgreSQL/Python information where available. Odoo Git SHA is explicitly unknown when the official image cannot establish it.
- `module_status.json`: read-only technical module name/state/version query, or explicit unavailable status. No models are instantiated.
- `restore_diagnostics.json`: inspected format/source/producer, requested/selected PG version, actual server/client/image identity, restore phase/reason/exit/SQLSTATE and safe SQL evidence.
- `database_diagnostics.json`: read-only server-version availability, not customer rows or a DB dump.
- `docker_status.json`: allowlisted state/exit/OOM/timestamps from owned containers only; never full inspect/environment output.
- `remediation_history.json`: accepted diff and reason hashes; free-text reasons remain local.
- `README.md`: concise interpretation and next steps, limitations and private-log location guidance.

## Privacy boundary

Redacting arbitrary customer payloads reliably from unrestricted logs is not possible with a password regex alone. Therefore portable logs/tracebacks use an **allowlist**: free-form messages, SQL, source-code literals, URLs/connection strings and business payloads are withheld. Whole raw logs, traceback messages, dumps, filestore, input backups, keys and environment lists are never bundled. This intentionally gives less text than local private logs while preserving the full available frame structure and technical failure identity. Automatic redaction also masks credential assignments, Bearer tokens and HTTP/PostgreSQL URLs in application logs.

Technical module/package identifiers and version inventory can still reveal implementation details. Review the ZIP before sharing. Full customer-specific investigation may require separately authorized private review of `failure.private.log` and raw attempt logs; do not attach these automatically. Custom exception payloads and business records are never inferred safe merely because they resemble a known error.

## Evidence and failure behavior

A failed restore classifies as `DATABASE_RESTORE_FAILURE` and skips all Odoo-table queries; module diagnostics are explicitly unavailable. Known noncustomer timeout SET statements can be portable; full failed SQL/errors remain only in protected `restore.private.log`. Database evidence uses read-only PostgreSQL sessions. There is no Odoo registry startup in failure collection. Each SQL/container evidence source can be unavailable without invalidating the original failure; metadata/log packaging still proceeds where possible. Archive completion uses a `.part` file followed by rename; partial ZIPs are not exportable. Kernel/process termination may prevent collection entirely; explicit resume records the interrupted attempt, reconciles resources and creates new evidence if recovery fails.

Start investigation with the failed stage, exact runtime revisions and structural trace; reproduce on a disposable copy, verify upstream code/fix, review any pending runtime diff, then use `resume --dry-run`. A valid checkpoint is a recovery boundary, not proof that a cause is safe to ignore. There is no automatic repair or retry for unknown failures.

Resource exhaustion during restore records a safe `RESOURCE_EXHAUSTION` category, specific condition and before/after free-byte/inode snapshots. Primary `DATABASE_RESTORE_FAILURE` and unavailable Odoo-module evidence are preserved. The latest storage correction was not test-executed at the user's request; see [the incident note](RESTORE_STORAGE_FAILURE.md).
