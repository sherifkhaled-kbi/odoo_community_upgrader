> Historical Phase 2 proposal. Implementation scope is superseded by [the focused local CLI plan](../implementation/FOCUSED_PLAN.md); worker, SSH, systemd and asynchronous-service recommendations are excluded.

# OpenUpgrade integration hardening

**PROPOSED; no dependency update, image build or migration performed.** Exact observed revisions and evidence classifications are in [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md). Observed upstream heads are research snapshots, not release-qualified pins. Runtime changes require explicit plan acceptance and the tests below.

## Current integration and responsibility boundary

Current [core.py:446–601](../../src/odooupgrader/core.py#L446-L601) generates Dockerfiles/Compose, uses `odoo:<major>` and a shallow clone of the corresponding OpenUpgrade branch, installs requirements, and pins only openupgradelib. Docker cache and moving branches mean the branch head observed today does not establish the commit used by an earlier remote build. Installed custom addons are exposed only at the final transition, while custom Python requirements are installed at every transition. The wrapper supplies OpenUpgrade's script path and framework; it does not implement its own migration manager.

| Responsibility | Required treatment |
| --- | --- |
| OdooUpgrader | Durable jobs, input custody, pinned runtime contracts, dependency checks, fresh restore, module-coverage admission, process supervision, verification, checkpoint publication, diagnostics and explicit plan changes |
| OpenUpgrade/Odoo upstream | Version-specific schema/data migration algorithms, module migration scripts, framework behavior and ORM semantics. Follow upstream execution; do not copy or bypass its migration manager |
| Customer/module maintainer | Supported addon sources and migration coverage for each intermediate version, licensed Enterprise sources if applicable, documented business invariants and approved resolution of ambiguous data |
| Joint qualification | Exact Odoo/OCA/library/Python/PG combination, custom module closure, known-fix regressions and complete representative upgrade chains |

## Immutable per-step runtime contract

Propose a reviewed, versioned JSON runtime lock consumed by new `dependencies.py` and `runtime/images.py`. A published contract contains:

- Contract ID/schema, source and target Odoo major, edition, architecture, PostgreSQL server/client version and image digest, locale/encoding/extension requirements, and validation adapter version.
- Exact application package/version/commit, Odoo source commit, OpenUpgrade branch label **and full commit**, openupgradelib full commit, optional patch SHA-256, ordered addon/upgrade paths and hashes, and module rename/merge mapping evidence.
- Base image digest, final migration image digest and verification image digest. Both images must share identical Odoo source and normal dependency inputs; verification excludes OpenUpgrade loading and upgrade flags.
- Python interpreter and platform, resolved package versions and wheel/source hashes, operating-system package inventory and reproducible build inputs, custom-addon hashes per transition, build recipe digest and tested feature probes.
- Qualification report/fixture digests, supported installed-module profiles, known blockers, approval identity, creation date and provenance document hash. No secrets or customer records.

Resolve dependencies before admitting customer data. Build a pinned wheelhouse and install from the approved lock; record package hashes and `pip check`/import results. Pin OS/base inputs and repository snapshots where available. Record build output digests: immutable image reuse is the execution guarantee; do not promise byte-identical rebuilds merely from pinned Python versions. Fail conflicting transitive requirements instead of masking them with `--no-deps` or modifying the live container. Fetch/build stages have network access under policy; migration stages use pre-staged images and isolated networking.

Require a verifiable Odoo source SHA. A Docker image digest alone identifies image bytes, not a Git commit. If the official image cannot establish source provenance, qualify a controlled image built from the official source commit and required dependencies. This adds build maintenance and must be approved as part of the release model. Do not relabel unknown provenance as the latest branch SHA. Verify actual image/package/source inventory before dispatch, including cached images, and bind it to the attempt record.

Keep a separate source-version verification runtime for CP0. Test PostgreSQL dump/restore format and extensions with each contract; do not assume the current PG 13 default is suitable for every Odoo version or source dump. An incompatible `--postgres-version` fails admission with the supported contract choices. Never perform a PG major upgrade in place on an old attempt volume.

## Supported paths, modules and ordering

Initial qualification targets **Community 16→17→18→19**, one major at a time. Source versions 16, 17 and 18 require CP0 adapters; all target versions require checkpoint adapters. This is a proposed qualification scope, not an assertion that every module in these releases is already migrated upstream. Do not allow a path until every installed module is either supported at every step or covered by an explicitly reviewed rename/merge/removal mapping. The existing CLI's 10–15 options remain syntactically recognizable but unsupported until separately qualified; same-version and downgrade requests fail clearly.

Generate an installed-module inventory from the restored source and resolve the transitive dependency graph against pinned manifests for each intermediate version. Record module source provider, expected target name/version and migration evidence. A module absent from a coverage page is not automatically broken; one present on a page is not automatically validated for this database. Missing source, unexpected module removal, unresolved transitive dependencies, or incompatible custom schema blocks admission. Do not uninstall modules or clear states to pass. Custom addons require a versioned bundle at **each** transition; expose them on `addons_path`, and restrict server-wide `--load` to required framework modules rather than loading all business addons indiscriminately.

Enterprise is outside the first qualified profile. It requires authorized source availability, compatible custom/Enterprise module migration paths and explicit qualification; Community OpenUpgrade availability does not establish Enterprise coverage. Avoid a generic edition flag that implies support without these inputs.

At the inspected Odoo 19 commit, [migration.py](https://github.com/odoo/odoo/blob/a5447ae78fa15f25e69ce44fd154bf1d2f1e383a/odoo/modules/migration.py) collects module and additional upgrade paths, filters version ranges, orders stages `pre`, `post`, `end`, and sorts same-version stage files by basename. Files from several paths are collected; a second path is **not** a reliable override mechanism. Its special `0.0.0` ordering also needs regression coverage. These are verified 19.0 source behaviors; qualification must check corresponding 16–18/OCA adapters rather than assume identical internals.

For additional scripts: use a reviewed path allowlist, enumerate the effective scripts and hashes before execution, and reject duplicate `(module, version, stage, basename)` identities or unintended overlap with OCA scripts. Test the effective loader including OpenUpgrade patches. Prefer wrapper preflight/approved clone normalization before Odoo starts, so an extra path does not compete with the standard UoM migration. Last-resort patches produce a separate immutable image with base SHA and patch digest; no runtime edit, duplicate script injection, or skipped upstream script.

## Known fixes and upgrade policy

| Item | Verified status at research snapshot | Qualification and rollback |
| --- | --- | --- |
| K001 duplicate reference UoMs | Inspected 19.0 script can pass several records to a Many2one; no verified ambiguity resolution | E1/D3 on faithful legacy data. Block ambiguous roots; approved clone-only rule must meet all conditions in the error registry. Discard failed clone and restore 18 checkpoint |
| K002 custom bigger-unit factor | Inspected 19.0 script uses the wrong direction; PR #6054 remains open/unmerged | Compare original revision and proposed fix on UI-style `factor_inv` fixtures, prices and quantities. Prefer accepted upstream revision after review; an unmerged patch needs explicit exception and pin. Revert pending runtime plan and restore 18 checkpoint; never undo migrated DB with SQL |
| K003 inherited XML-ID helper | Configured library pin contains `add_inherits_parent_xmlids`; no historical traceback or tested runtime supplied | Import and invoke helper on cron/server-action fixture in 18 runtime. Test all intended consumers; do not infer universal compatibility from one helper's existence. Revert runtime contract and fresh restore |
| Generic missing dependencies/schema/modules | No individual customer occurrence verified | E3–E5 with minimized reproducer and exact manifest. Correct source/runtime contract, then fresh attempt. Ad hoc schema patches are excluded |

Links: [PR #6054](https://github.com/OCA/OpenUpgrade/pull/6054), [historical 14.0 PR #2666](https://github.com/OCA/OpenUpgrade/pull/2666), [known error registry](MIGRATION_ERROR_REGISTRY.md). PR status is time-sensitive; recheck before selecting a release. A proposed PR fix is not a passing migration test.

For every proposed fix follow: already fixed in exact runtime → newer qualified upstream → supported configuration/dependency correction → provable source normalization → reusable wrapper rule → pinned upstream patch last. Maintain a small reviewed issue register linking error IDs, affected runtime contracts, upstream issue/PR, reproducer, qualification report and retirement condition for any patch. Review upstream changes during dependency qualification; no background auto-update of a running job.

## Dependency changes during resume

Partition provenance into immutable **completed prefix** and proposed **pending steps**. A changed OpenUpgrade SHA, image, library, addon, validation rule or configuration is always visible. Default resume rejects unaccepted drift (R7). A reviewed correction to the pending 18→19 contract may be accepted by its exact diff digest without invalidating verified 17/18 checkpoints or rerunning them. Recheck checkpoint compatibility and restore into a fresh attempt; record old/new contracts and reason. Source changes or changes to the executed prefix require a new job, not rewritten history.

App/control-schema upgrades require backup, tested schema migration, reader compatibility and an approved rollback path; a package downgrade cannot blindly read a newer ledger. Stop dispatch during control-schema maintenance. Roll back the application with a compatible metadata backup while preserving later immutable files for reconciliation; never erase evidence or mark a later checkpoint successful merely because its directory exists.

## Release gates and scope

H1: image/build/import provenance; H2: resolved dependency and API contract; H3: installed-module coverage; H4: script order and collision checks; H5: clean full-chain and known-error regressions; H6: accepted-plan resume retaining completed-prefix dispatch counts. All are specified in [TEST_STRATEGY.md](TEST_STRATEGY.md). Any failed mandatory gate leaves the runtime unqualified. Rollout starts with isolated fixtures, then an explicitly authorized disposable customer-backup trial; no live production connection is required or proposed.

Unresolved before qualification: actual image provenance on Contabo, exact installed module/edition profile, PG/extension requirements, authoritative fixes for K001/K002 and older-version registry adapter details. These do not prevent building the job/checkpoint foundation after approval, but they prevent declaring a particular customer path production ready.
