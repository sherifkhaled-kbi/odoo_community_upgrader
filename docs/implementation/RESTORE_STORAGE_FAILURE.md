# Restore storage exhaustion: SQLSTATE 53100

## Confirmed incident reason

The operator's job `1e5bb85a-3fe9-4da7-9f2e-1ea35b2116ec` failed during initial RESTORING, before any Odoo transition or verified checkpoint. PostgreSQL server, psql and pg_restore were all 17.11. The source server was 16.7 and pg_dump was 17.3; the earlier version mismatch was resolved correctly.

The private error line supplied by the operator was:

```text
ERROR: 53100: could not extend file "base/16389/18412" with FileFallocate(): No space left on device
```

PostgreSQL could not allocate more space for a database data file. SQLSTATE 53100 is `disk_full`. This specific message concerns the PostgreSQL data filesystem, not an Odoo model, transaction_timeout or Docker /dev/shm. Free blocks, backing storage limits and quotas must be checked on the filesystem containing the Docker PostgreSQL volume. Inode checks are also included in operational inspection, but the supplied error does not establish which host capacity limit was reached.

Sources: [PostgreSQL error codes](https://www.postgresql.org/docs/17/errcodes-appendix.html). Docker shared-memory exhaustion can also surface as 53100, but would mention shared memory; this incident's actual error names a database file.

## Application correction

1. Plain SQL is streamed from the private input copy into `docker exec -i ... psql -f -`. The application no longer stores an additional full SQL dump in Docker's writable `/tmp` layer. The original backup and retained input remain unchanged. Fatal SQL handling, single-transaction rollback and fresh databases are preserved. Custom archives still use a copied archive for TOC/restore access.
2. Before copying/restoring, measure free bytes and inodes in the PostgreSQL data filesystem, container scratch filesystem and shared memory. Block missing measurements, zero available inodes and insufficient estimated restore capacity with an actionable error. The data-volume estimate is three input-file sizes plus reserve (and custom-archive staging allowance); it is a conservative admission heuristic, not proof of final size. Compressed archives, indexes, WAL, temp files and concurrent disk use can exceed it. No automatic deletion occurs.
3. Restore failure evidence recognizes 53100 from the primary PostgreSQL ERROR line and distinguishes database-file, shared-memory, quota and unspecified exhaustion. Portable summaries expose a safe resource explanation and recommended action without leaking customer SQL/records. Capacity snapshots before restore and after failure are included when available; the post-failure snapshot may show reclaimed space after rollback, so it does not override the original OS error.
4. The primary classification remains `DATABASE_RESTORE_FAILURE`, with `RESOURCE_EXHAUSTION` category and `RESTORE_STORAGE_EXHAUSTED` reason where appropriate. Odoo module diagnostics remain unavailable after restore failure. No checkpoint or successful artifact is published.

Changed application files: `core.py`, `recovery.py`, `postgres_restore.py`, `diagnostics.py`. Existing test adapters were aligned with the streaming/capacity interfaces but **not executed**, as explicitly requested. Validation was limited to AST syntax parsing and whitespace/source review; no migration, test file or database operation was run for this correction.

## Required server action

A code patch cannot expand a full Contabo disk. Inspect these locations on the server (read-only commands):

```bash
df -h /opt /var/lib/docker
df -i /opt /var/lib/docker
docker system df

docker inspect ou-db-965f683a0f3d4f7eafdeed1e28fa2996 \
  --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}'
```

Run `df -h` and `df -i` against the PostgreSQL mount source printed by the last command. If Docker uses a different data root, inspect `docker info --format '{{.DockerRootDir}}'` and check that backing filesystem as well. Review applicable volume/storage-driver or host quotas if free host bytes do not explain the allocation failure.

Expand the relevant disk/filesystem, or approve removal of specifically identified unneeded resources. Do not run broad Docker prune, remove original backups, or delete retained checkpoints. Old stopped failed containers can retain large `/tmp/input.dump` copies and partial database/WAL volumes, but their deletion is a separate explicit retention decision. The patch does not remove these automatically.

After capacity is sufficient and this patch is deployed, start a new job:

```bash
cd /opt/odoo_community_upgrader
.venv/bin/odooupgrader \
  --source /opt/odoo17_backup \
  --version 19.0 \
  --postgres-version auto \
  --jobs-dir /opt/odooupgrader-jobs
```

This failed job has `latest_checkpoint: null` and `completed: []`, so it has no verified resume point. Do not resume its partially restored volume. Keep the old diagnostics and use a fresh job after resolving host storage capacity. Restorability and complete migration remain unverified for this server until that retry succeeds.
