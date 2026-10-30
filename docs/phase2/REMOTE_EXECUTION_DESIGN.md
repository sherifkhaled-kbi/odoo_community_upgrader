> Historical Phase 2 proposal. Implementation scope is superseded by [the focused local CLI plan](../implementation/FOCUSED_PLAN.md); worker, SSH, systemd and asynchronous-service recommendations are excluded.

# Remote execution on Contabo

**PROPOSED.** User-confirmed current topology: local CLI controlling a remote Docker daemon on a Contabo server described as Ubuntu “24.4”. Treat 24.04 as an assumption pending `/etc/os-release` verification during authorized deployment. Remote Docker transport, systemd, filesystem, credentials, capacity and backup arrangements have not been inspected.

## Current limitation and target topology

Current source launches local subprocesses and writes files under local CWD ([core.py:29–46](../../src/odooupgrader/core.py#L29-L46), [499–535](../../src/odooupgrader/core.py#L499-L535)). It contains no SSH orchestration. A Docker context can direct commands elsewhere, but `./output` bind mounts resolve on the daemon host; matching local filenames do not transfer that content ([Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)). This makes remote filestore/log handling a confirmed architectural risk; actual remote contents and transport failures are unverified.

Use a local submission/observation CLI and a server-resident worker. SSH carries short authenticated control requests and bounded input/output streams. The worker and all mutable DB/filestore paths live on the Docker host. No migration's lifetime is tied to SSH, the local terminal, or a remote `docker compose up` stream. Reuse OpenSSH authentication and known-host verification; do not expose a new public HTTP API, PostgreSQL port, or Docker TCP listener for this feature.

### Submission and transfer protocol

The proposed `remote.py` client invokes one fixed installed server helper, `remote_agent.py`, via an administrator-configured SSH profile. Send versioned structured requests over stdin, never interpolate source paths, job IDs or user arguments into a remote shell command. The helper allowlists operations and validates IDs, lengths, paths, access and request schema. Operators may invoke only this helper as the service identity; direct Docker access remains an administrator privilege. Bind operator identity to administrator-controlled SSH credentials/forced-command mapping, not a caller-supplied JSON identity. Enforce job access on the helper for every operation. Disable shell/port/agent forwarding for dedicated helper credentials; administrator access is separate. First release is a trusted-operator service, not a multi-tenant customer isolation boundary.

1. Allocate a job and request ID through the helper. Store the request ID locally before transmission; if the reply is lost, query/retry the same ID, never invent a new one automatically.
2. Stream source/addon/filestore assets into per-upload `.part` files. Each bounded chunk has offset, length and checksum; server acknowledges only flushed bytes. Reconnection asks for verified offset; mismatched bytes restart that upload explicitly, not the migration. Server enforces input quotas, permitted types, path containment, no symlinks/devices and expanded archive limits.
3. Finalize asset by exact total length and SHA-256, copy/import into worker-private storage and fsync before marking input ready. No unverified asset is mountable. Local URL sources are downloaded by the client and uploaded as bytes; authenticated URL parameters are never persisted. An explicit `--server-source` imports an allowed remote backup path into private input storage without modifying it.
4. `submit` transaction binds all immutable asset digests to a job and sets QUEUED. Only after durable commit return `accepted=true` and job ID. A duplicate request with identical input returns the original job; different input under the same request ID is a conflict.
5. Status/log requests are short, read-only and permission checked. Artifact downloads come from published manifests, use offset/checksum restart, and finalize locally by atomic rename only after hash verification.

The versioned helper protocol needs compatibility negotiation and a stable error envelope. Connection loss before acceptance is `acceptance unknown`; the client queries by request ID. Connection loss during status/follow never signals cancellation. Partial uploads expire only under the configured intake retention policy; no job starts while they are incomplete.

## Process model

Propose one administrator-installed **system** unit `odooupgrader-worker.service`, running a foreground worker as a dedicated `odooupgrader` account. Use supported directives from the [systemd 255 service manual](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html): `Type=exec`, `Restart=on-failure`, bounded restart backoff/start limits and graceful stop timeout. Order after local mounts and Docker; verify Docker availability before dispatch. Enable at boot. Do not depend on a user session, `nohup`, a transient scope or user lingering.

The worker holds a host-wide kernel lock, polls the durable queue, and initially runs one attempt at a time. Keep migrations as supervised child processes with both streams drained; store progress by stage rather than a guessed percentage. Use explicit timeouts for dependency acquisition, startup, restore and probes, and administrator-configured migration limits with visible cancellation; avoid an arbitrary short timeout for large legitimate upgrades.

Docker migration and verification containers use `restart: no`. PostgreSQL attempt containers also start only under worker control. `Restart=on-failure` restarts the **worker**, not a failed migration. Host restart reconciles state, then awaits an explicit resume for an interrupted attempt. New submitted jobs remain durable in the queue and are eligible after reconciliation. Resource labels/IDs and ownership generation are checked before every stop/remove; no broad `docker rm`, `compose down -v`, or Docker prune is allowed.

Docker access is effectively host-administrator power on a rootful daemon. Do not claim the dedicated worker account is a strong security boundary from that host. Use a dedicated migration server/VM, restricted operator keys and trusted migration/addon images. Do not mount the Docker socket inside Odoo/PG containers. Do not run builds with customer backups in the build context. Container egress is disabled except the required isolated DB path; this prevents scheduled integrations or hooks from reaching production services. Runtime images/dependencies must be staged before the data phase.

## Persistent storage and ownership

Proposed layout (does not exist as deployed infrastructure):

```text
/var/lib/odooupgrader/
  state/                        jobs.sqlite3, schema backup, immutable ledger exports
  locks/                        fixed lock files, ownership diagnostics
  incoming/<upload-id>/         bounded incomplete uploads
  jobs/<job-id>/
    inputs/                     immutable original backup and supplied files
    metadata/                   immutable plan revisions and checkpoint manifests
    checkpoints/.staging/<id>/  unpublished snapshot candidates
    checkpoints/<id>/          complete immutable published snapshots
    attempts/<attempt-id>/     working filestore, protected runtime config
    logs/<attempt-id>/          Odoo and process logs, rotation index
    reports/<attempt-id>/       validation, diagnostics, remediation evidence
    artifacts/.staging/<id>/    incomplete final package
    artifacts/<id>/            published verified output
  backups/                      completed metadata backup manifests (not sole backup)
/etc/odooupgrader/              administrator-managed nonsecret profiles/policies
/run/odooupgrader/              transient credentials and socket/temporary runtime data
```

Job root and metadata default to service ownership, umask `0077`, directories `0700`, files `0600`. Writable working filestore mounts need explicit mapped Odoo UID/GID ownership or narrowly scoped ACLs, verified per pinned image; parent metadata/checkpoints remain unmounted. PostgreSQL's actual data resides in labeled attempt volumes whose host storage capacity is accounted for separately. A checkpoint is a dump/file bundle on persistent host storage, never an image layer or a raw copy of live PGDATA. Mount durable storage before worker startup and fail if mount identity differs; an empty fallback directory must not be treated as lost jobs/new installation.

Credential files are generated per attempt in protected runtime storage, mounted read-only and referenced in configs. The source backup itself may contain credentials and customer data: protect inputs, logs, snapshots and exports equally, restrict backups, and redact public diagnostics. Do not put secret values in fingerprints; hash sanitized config and record secret-manager reference/version. Log redaction is defense in depth; raw restricted logs may still contain customer data and must not be public.

## Capacity, retention and backups

Before intake, restore, each step, checkpoint and export, reserve estimated bytes and check free bytes/inodes on **each actual backing filesystem**, including Docker's data root. Use an explicit estimate, not a fixed compression ratio:

`peak = retained inputs + retained checkpoints + working DB/filestore + candidate dump/filestore + verification DB/filestore + artifact staging + images/build cache + WAL/temp/log allowance + safety reserve`.

Existing terms already occupying disk are not charged twice; reservations track remaining growth per filesystem. Capture actual sizes after restore and each step to refine estimates. A compressed source cannot prove the restored size: use an operator-supplied upper bound and monitored expansion until measured. Unknown/unreserved capacity blocks scheduling; disk/quota monitoring continues during work. Low-watermark breach cancels the owned attempt safely, preserves published checkpoints and reports needed space. If SQLite cannot persist due to a full disk, write bounded system journal diagnostics, stop owned writers and block until state is reconciled; do not announce failure as durably recorded when it is not.

Initial policy: never automatically delete original inputs, published checkpoints, unresolved failed evidence or final artifacts to make room. Explicit cleanup shows job/resource IDs, sizes and referenced checkpoint graph; it refuses active resources or any checkpoint required by a job. Temporary upload expiry and log rotation are bounded and configurable. Production rollout requires approved retention periods, capacity limits and off-host backup destination; absent a policy, stop on capacity pressure instead of pruning.

Use SQLite's [online backup API](https://www.sqlite.org/backup.html), not a live file copy. Back up immutable checkpoint directories, matching state backup, plans, reports, runtime provenance and credentials-reference inventory as one versioned recovery set. Publish the backup manifest only when every referenced object is copied and verified. An off-host encrypted backup plus tested reconstruction is required for protection against Contabo host/disk loss; local checkpoints protect attempts, not total server loss. Restore control metadata offline, reconcile generations and container identities, and require explicit resume. Never reconstruct authoritative state automatically by scanning filenames.

## Failure reconciliation

| Event | Required behavior |
| --- | --- |
| SSH/local client disappears | Worker continues; local `wait` can reconnect by job/request ID |
| Worker killed while Odoo runs | New worker reacquires kernel lock, captures labeled resource state, quiesces only proven orphan writers, marks INTERRUPTED and preserves last checkpoint; no use of mutable attempt for retry |
| Server reboot or Docker restart | Boot/daemon IDs checked; Docker containers do not auto-run migrations; journal/SQLite recovered first; partial candidates quarantined or verified publication reconciled |
| Container exits 0 but modules pending | Validation fails; no checkpoint advancement or final artifact |
| Metadata missing/corrupt | Stop dispatch, verify SQLite integrity, restore approved backup offline; no blank job-store initialization over existing job files |
| Stale heartbeat | Investigate PID/start time/boot ID, kernel lock and exact containers; timeout alone never permits takeover |
| Lock conflict/two resumes | SQL uniqueness/CAS and OS ownership admit only one; second request reports active attempt |
| Unknown container with similar name | Do not remove it; labels, exact IDs and generation must prove ownership; otherwise RECOVERY_BLOCKED |
| Interrupted dump/copy/export | `.staging`/`.part` remains unpublished; last verified checkpoint and previous artifact unchanged |
| Wrong mount/permissions/disk | Fail admission or current stage with resource evidence; retain durable recovery boundary |

Remote qualification must use an isolated Ubuntu test VM and Docker daemon, including actual SSH disconnect, worker kill and reboot tests. No destructive experiment is authorized on the existing Contabo server during Phase 2. Remaining deployment prerequisites: verify OS/service versions, SSH route/authorization, filesystem and mount identity, Docker root/context, disk quota/free space, image UID mappings, server backup destination and retention owner.
