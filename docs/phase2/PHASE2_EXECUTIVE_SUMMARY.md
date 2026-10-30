> Historical Phase 2 proposal. Implementation scope is superseded by [the focused local CLI plan](../implementation/FOCUSED_PLAN.md); worker, SSH, systemd and asynchronous-service recommendations are excluded.

# Phase 2 executive summary

Date: 2026-10-30. **Architecture and planning complete; implementation is not started.** This package reviews all eight Phase 1 documents, rechecks critical local source behavior, and verifies relevant upstream code at recorded SHAs. It is an implementation plan, not migration certification. No customer database, remote server, application source or deployment configuration was changed during Phase 2.

## Recommended architecture

Keep the local CLI, but move execution and durable state onto the remote Docker host. Use a **single systemd-supervised Python worker, SQLite on host-local storage, SSH submission/status/transfer, and immutable PostgreSQL dump plus filestore checkpoints**. Start with one active attempt. No new public API or message broker is needed.

Every failed transition resumes by restoring a **fresh** working database and filestore from the explicit latest verified checkpoint. A checkpoint is published only after database/module/registry/attachment checks, applicable business comparisons, a successful snapshot restore, hashes and a durable publication transaction. Resume never relies on a partially migrated database, silently changes a dependency, or substitutes an older checkpoint.

For the main requirement: when 16→17 and 17→18 succeeded but 18→19 failed, retain the verified 18 checkpoint; accept a reviewed correction to the pending 18→19 runtime/resolution, restore that checkpoint, and run only 18→19. Completed transitions and their provenance stay unchanged. If the final target checkpoint already exists and packaging failed, retry final verification/packaging only.

## Existing, missing, proposed and verified

| Classification | Finding / implication |
| --- | --- |
| Existing, confirmed in application source | Synchronous Click CLI, Docker/PostgreSQL orchestration, OpenUpgrade branch-based image builds, one pinned openupgradelib revision, sequential upgrades and final ZIP creation |
| Missing in current application | Persistent jobs/attempts, verified checkpoint publication, resume commands, job ownership fencing, remote SSH orchestration, durable worker and three-level validation. Tracked packaging and test sources are also absent |
| User-confirmed deployment | Local CLI controls a remote Docker daemon on Contabo; server described as Ubuntu “24.4”. 24.04 is a planning assumption; actual OS/storage/service versions were not inspected |
| Verified upstream behavior | Odoo 19 commits within module upgrades; failed runs may contain partial committed state. Normal registry startup can react to a partial-upgrade marker, so inspect SQL first and probe only a disposable restore. Stored module version must be distinguished from computed disk version |
| Verified UoM mechanism | Inspected 19.0 migration can assign multiple reference records to a Many2one; a separate bigger-unit ratio issue can silently alter conversions. No supplied incident database/traceback was reproduced |
| Proposed functionality | Server worker, SQLite authority, paired immutable snapshots, exact runtime locks, per-step module profiles, semantic validation and reviewed clone-only resolutions. None is implemented by these documents |
| Unknown / NOT TESTED | Actual Contabo image SHAs, mount layout, SSH/storage/backup setup, customer edition/modules and incident lineage; complete upgrade paths, runtime compatibility, remote recovery and business-data outcomes |

The source-evidence register pins local application SHA `49c16285eb160b11d8a977bba278bf3a3bc333f1`, observed OpenUpgrade 17/18/19 commits, the configured library commit and researched Odoo commits. Observed upstream branch heads are **not** established remote runtime revisions or tested release pins. Full citations are in the [architecture review](ARCHITECTURE_REVIEW.md).

## Most important changes and failure resolution

1. Protect immutable inputs and verified recovery boundaries; replace CWD deletion, shared resource names and unconditional failure cleanup with per-job/attempt storage and ownership checks.
2. Make restore errors fatal, drain process logs safely, propagate failures and require module/registry/filestore validation. Exit 0 cannot establish successful migration.
3. Pin complete runtime provenance and per-step custom module coverage. Permit an explicitly accepted pending-step correction while preserving completed-prefix history.
4. Separate local observation from remote execution. SSH loss leaves accepted jobs running; reboot reconciliation preserves checkpoints and requires explicit fresh resume for interrupted work.
5. Compare UoM, inventory, accounting and attachments where applicable, both per step and against the initial semantic baseline. Unknown or unimplemented required validation blocks completion.

The [error registry](MIGRATION_ERROR_REGISTRY.md) records **18 incidents or code-backed failure conditions**, with confidence labels; it does not claim 18 observed customer failures. Prospective error classes cover dependencies, upstream scripts, source data, schema/modules, PostgreSQL, resources, infrastructure, checkpoints and unknown exceptions.

For UoM, first diagnose all active/inactive references and conversion semantics. Never choose the first reference, delete duplicates or change source ratios to accommodate a broken script. Ambiguity blocks until an approved canonical mapping and full clone trial establish a safe resolution. The separate bigger-unit fix requires qualified upstream code. [OpenUpgrade PR #6054](https://github.com/OCA/OpenUpgrade/pull/6054) was open/unmerged at inspection and does not itself settle duplicate-reference semantics; recheck and test before adoption. Historical [PR #2666](https://github.com/OCA/OpenUpgrade/pull/2666) is 14.0 work, not a verified 19.0 solution. The configured library contains the inherited XML-ID helper, but its runtime installation and historical failure resolution remain untested.

## Development order and complexity

Start with packaging/outcomes, durable state/ownership, pinned runtimes and minimum source/registry validation. Next deliver restore-tested checkpoints and safe resume, the highest product priority. Then qualify remote execution/reboot/backup recovery, richer diagnostics, UoM corrections and full business validation. Tests accompany every task; final release qualification runs the full chain and remote failure scenarios.

Overall complexity is **High to Very High**. The difficult parts are crash-consistent snapshot publication, ownership/reconciliation, version-specific registry validation, source provenance and business-semantic UoM/stock/accounting proof. SQLite/systemd keep infrastructure modest; full dumps and restore testing add storage and runtime cost that must be measured. No delivery dates, staffing or universal performance estimates are proposed.

## Decisions requiring approval before Phase 3

| Decision | Recommendation | Consequence |
| --- | --- | --- |
| A1 execution/state model | Server-resident single worker + systemd + SQLite, local SSH client | Replaces direct local control of remote Docker. Installation/host changes need separate authorized rollout after isolated tests |
| A2 initial qualification scope | Community 16→17→18→19; custom modules only with every-step coverage | Existing 10–15 syntax remains recognizable but blocked until qualified; Enterprise unqualified by default |
| A3 CLI behavior | New asynchronous `upgrade`, durable jobs/resume; legacy command waits; explicit artifact export | Closing CLI no longer cancels a job; `jobs cancel` is explicit. Automatic overwrite of CWD output is removed |
| A4 runtime provenance | Exact Odoo/OCA/library/image/dependency contracts; controlled official-source build when image source SHA cannot be established | Additional build/qualification work; no unreviewed branch/image updates during resume |
| A5 repair/retry policy | Explicit fresh resume; clone-only human-approved data remediation, pending-plan diff acceptance; unknown errors blocked | No automatic migration reruns or ambiguous UoM fixes. Last-resort upstream patch requires explicit approval and tests |

Operational policy decisions before deployment: approved server storage root/mount, capacity/reserve and retention limits, operator access, off-host encrypted backup destination/owner and recovery objectives. The proposed `/var/lib/odooupgrader` is not an assertion about current server layout. Without approved retention, stop on capacity pressure instead of deleting checkpoints.

## Remaining information and readiness

Verify the actual OS/systemd/Docker/SSH setup, filesystem durability and mount identity, available capacity and Docker data root on Contabo during an authorized inventory. Obtain the intended source edition/module inventory, custom addon sources for intermediate versions, PostgreSQL/extension details and matching filestore/storage adapter. To resolve the reported customer incident conclusively, use its protected traceback and an explicitly authorized disposable backup or minimized reproducer; none was supplied for execution here.

These unknowns do not block implementing the isolated job/checkpoint foundation **after explicit Phase 3 approval**. They do block declaring a customer migration path or the existing server production ready. Older-version registry adapters, final export compatibility and the chosen UoM correction must be verified in executable qualification. No implementation, deployment or live migration is implicitly approved by this plan.

## Deliverable index and validation boundary

- [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md): current source evidence, corrections to Phase 1, exact revisions and architecture alternatives.
- [RESUME_ENGINE_DESIGN.md](RESUME_ENGINE_DESIGN.md): persistent schema, legal state machine, checkpoint publication/reconciliation, fingerprints, ownership and exact CLI contract.
- [REMOTE_EXECUTION_DESIGN.md](REMOTE_EXECUTION_DESIGN.md): SSH protocol, server worker, storage, permissions, capacity, backups and reboot behavior.
- [MIGRATION_ERROR_REGISTRY.md](MIGRATION_ERROR_REGISTRY.md): confidence-labeled known failures, classifications, UoM rules and upstream-first resolution.
- [OPENUPGRADE_HARDENING_PLAN.md](OPENUPGRADE_HARDENING_PLAN.md): immutable runtimes, dependency/module/script contracts and reviewed change control.
- [DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md): eight epics, file-level tasks, dependency graph, acceptance tests, risks and rollback.
- [TEST_STRATEGY.md](TEST_STRATEGY.md): V1/V2/V3 validation, all R1–R8/E1–E8/D1–D6 scenarios, publication/security/remote and runtime qualification suites.
- [PHASE2_EXECUTIVE_SUMMARY.md](PHASE2_EXECUTIVE_SUMMARY.md): this decision and readiness summary.

Phase 2 checks found all eight deliverables, no broken local document links, and all 22 required R/E/D scenario rows. SHA-256 comparison against the start-of-phase baseline confirmed all 16 application/workflow/Phase 1 files unchanged; tracked Git content has no modifications. Application tests, migration reproductions, remote tests and customer-data validation are **NOT TESTED** in this phase. The requested next gate is approval of this plan; work stops here before Phase 3.
