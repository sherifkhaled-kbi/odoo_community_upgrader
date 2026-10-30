# Local checkpoints and recovery

## Execution and custody

Run the CLI directly on the Linux Docker host. The operator handles SSH, `tmux` and `screen`. A Unix Docker endpoint is required; remote contexts and non-Unix `DOCKER_HOST` are rejected. A terminated CLI may leave containers running; a new explicit resume acquires the job's kernel lock, records interruption and stops only containers whose exact job/attempt labels match. No automatic rerun occurs. All subsequent work uses a new attempt, PostgreSQL volume/network and writable filestore.

Inputs are copied into private per-job storage. Never point the tool at a live database: only ZIP/custom-format backups are supported. All SQL diagnostics use read-only PostgreSQL sessions; normal Odoo registry probes use disposable restored copies after checking module state and partial-upgrade markers. Registry startup is never used to diagnose a failed database.

## Layout and authority

```text
JOBS_DIR/JOB_UUID/
  job.json                    atomic authoritative ledger
  job.lock                    persistent inode, never unlink while active
  inputs/                     retained original backup and addon copy
  attempts/ATTEMPT_UUID/
    source/                   extracted working input
    runtime-data/filestore/database/  mutable attempt copy
    runtime-data/             writable Odoo data root, isolated per attempt
    output/upgraded.zip       final output only after validation
    migration.log             protected local execution log
    docker.log                protected raw migration output
    failure.private.log       full private Python traceback, never bundled
    db-composer.yml            attempt-owned PostgreSQL resources
    build/VERSION/            build context with code only, no backups
  checkpoints/.staging-UUID/   incomplete candidate, never a recovery source
  checkpoints/UUID/           immutable published dump + filestore + manifest
  diagnostics/ATTEMPT_UUID/    sanitized evidence
  diagnostics/ATTEMPT_UUID.zip
```

`job.json` is written to a unique temporary file, fsynced, atomically replaced and its parent fsynced. Linux `flock` excludes duplicate ownership; no heartbeat timeout steals a lock. A read-only status query can show INTERRUPTED when recorded RUNNING has no owner. Resume rechecks under the lock; status is not execution authorization.

Each successful transition and CP0 (verified restored source) has `database.dump` (custom PostgreSQL archive), complete filestore and a manifest containing job/attempt/parent/version, source digest, file lengths/SHA-256, validation results and available runtime provenance. Published files are never rewritten by the application. Retain producer image IDs for later normal-registry validation.

## Checkpoint contract

1. The migration container has exited successfully and is confirmed stopped, with no OOM condition.
2. SQL confirms expected stored base version, installed module coverage, no unexpected pending states/partial marker and no unvalidated DB constraints. Unmapped upstream module renames/removals block for review; no automatic state clearing.
3. Referenced file-backed attachments exist with expected size and stored SHA-1, and the complete filestore inventory is SHA-256 hashed. Database-backed attachments and URLs are not treated as missing files. Custom external-storage providers require separate qualification; generic checks do not certify them.
4. Quiesced database and filestore are dumped/copied together. Restore must succeed in a new verification database using fatal restore handling. A normal Odoo startup on a separate filestore copy has no update flags, OpenUpgrade server-wide loading, HTTP, cron or external egress. Missing-module/error/migration logs or absent startup evidence block publication. Probe changes are discarded.
5. The complete candidate directory is fsynced and atomically renamed on the same filesystem. Only then does one atomic ledger update add the checkpoint, complete the transition and advance the latest pointer.

Files and the JSON ledger are not one transaction. A crash after rename but before ledger update leaves an unreferenced directory; **it is never automatically adopted**. Only the explicit latest pointer with a matching manifest/file inventory is usable. A missing/corrupt latest snapshot blocks; do not edit the ledger to substitute an older snapshot. Preserve evidence and restore the exact checkpoint from an approved backup, or explicitly start a new job from retained source. No checkpoint before failed CP0 means a new job is required.

Normal registry startup is evidence of initialization, not universal functional correctness. UoM unrounded ratio checks compare 18 inverse factors to 19 absolute factors against a stable category anchor, permitting a changed global root. These checks do not replace rounded price/quantity, accounting, inventory or customer UAT qualification on the server.

## Resume and runtime corrections

`resume JOB --dry-run` reads metadata and hashes only; it creates no attempt/container and claims no fresh registry verification. It shows pending-plan before/after and acceptance digest, with ownership/restore/registry/disk checks explicitly deferred.

`resume JOB` requires a valid latest snapshot, exact ownership and accepted pending plan. It restores a fresh DB/filestore, rechecks technical state, tests normal startup on another disposable restore, and dispatches the next transition. A verified target checkpoint with failed finalization repeats packaging only. Completed history and producer runtime remain unchanged. Source/addon corruption blocks. Retained addons are fixed for the job; a different addon bundle currently requires a new job, not manual modification of retained files.

The accepted plan tracks full OpenUpgrade commits, library pin, application commit/version and code fingerprint. A reviewed pending revision/app change requires the exact dry-run diff digest and reason. Completed OpenUpgrade transitions cannot change revision. Source bytes cannot be replaced. Image/package inventories are captured when built; official Odoo Git SHA is recorded as unavailable when not provable. Base images and resolver outputs may change when rebuilding a pending transition, so pins alone do not guarantee reproducibility. Before data migration, the built image and dependency manifest are compared with the recorded runtime lock. Drift blocks and is saved under `built_runtime_candidates` in dry-run output. Review that exact manifest diff, then pass its `accept_sha256` with `--accept-runtime-change` and a reason. A planned revision change can therefore need a second acceptance after the candidate image is built. Historical producer images must not be pruned until their checkpoints are retired.

## Capacity, retention and backup

Before input copy/extraction, transition and checkpoint/export, check space. The checkpoint gate budgets approximately three database sizes plus three filestore sizes on the job filesystem plus a 256 MiB reserve; PostgreSQL volume free space must also support two database sizes plus reserve. These are conservative admission heuristics, not guarantees for arbitrary DB/index/WAL growth or compression. Individual downloads/extractions are checked; running PostgreSQL can still exhaust capacity. Stop and investigate capacity errors; never delete checkpoints to make room automatically.

Keep job storage owner-private on a local filesystem with working fsync/rename/locking semantics. Published/staging paths share a filesystem. No network filesystem qualification is provided. Copy a consistent job directory for off-host backup only while the job lock is unowned and execution is stopped; include images/provenance needed for restore. Verify the backup's hashes and rehearse restoration separately. This release does not implement automated backup or garbage collection.

Stopped attempt containers, networks and PostgreSQL volumes are deliberately retained. `docker inspect`/labels identify them; never run broad Docker prune or delete a job/checkpoint without a separately reviewed retention decision. Old attempt volumes are diagnostic evidence, not checkpoints. Total server/disk loss is unrecoverable without an independent backup.

Final ZIP retains the existing `dump.sql` + `filestore/` layout and adds checkpoint provenance. ZIP integrity is tested; final target Odoo backup-manager import compatibility and business correctness remain NOT TESTED until isolated server qualification. The job ledger must say COMPLETED before treating a final ZIP as published.
