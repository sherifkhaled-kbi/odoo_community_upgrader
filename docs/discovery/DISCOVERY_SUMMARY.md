# Discovery summary and planning readiness

**Date:** 2026-10-30. **Application:** `cursor/pin-openupgradelib-commit` at `49c16285eb160b11d8a977bba278bf3a3bc333f1`. This review is static and read-only. It inspected all tracked application Python files, the four tracked workflows, and relevant OCA/OpenUpgrade, openupgradelib, and official Odoo 18/19 source. It did **not** execute a migration, touch a database, build Docker images, install dependencies, or verify CI runs.

## Executive answers

1. **What it does.** `CONFIRMED` Accepts a ZIP/pg dump, restores it into a temporary PostgreSQL container, runs one Odoo/OpenUpgrade step per major version, and emits `output/upgraded.zip` with SQL dump and filestore ([architecture](SYSTEM_ARCHITECTURE.md)).
2. **How it works.** `CONFIRMED` A Click command calls a single Python orchestration class, which generates Docker Compose and Dockerfile files and shells out to PostgreSQL, Docker and Odoo ([pipeline](MIGRATION_PIPELINE.md)).
3. **OpenUpgrade dependency.** `CONFIRMED` Uses `odoo:<target>` plus an unpinned OpenUpgrade branch tip and pinned `openupgradelib` commit; Odoo discovers and executes OpenUpgrade scripts through `--upgrade-path` ([integration contract](OPENUPGRADE_INTEGRATION.md)). **Exact runtime OpenUpgrade SHA is unknown** without image/build evidence.
4. **Stable parts.** `CONFIRMED` The code has a clear linear control path, an explicit target-version list, and an explicit OpenUpgrade invocation contract. **Operational stability is NOT VERIFIED**; no path has a passing live test in this review.
5. **Fragile parts.** `CONFIRMED` Shared fixed Docker resources, ignored restore errors, deletion of previous output and the temporary DB, floating upstream dependencies, and missing artifact/business integrity checks ([audit](CODEBASE_AUDIT.md)).
6. **Most important confirmed problems.** Restore errors can be ignored; concurrent runs can conflict; unexpected exceptions can yield a zero process exit; filestore copy errors can still produce a package; no tracked package manifest or tests exist.
7. **Most serious risks.** Partial or incorrect customer database output, lost attachments, cross-run interference, non-reproducible migrations, and no safe resume path. These are code-backed risks; actual customer impact was not measured.
8. **Investigation gaps.** Actual failing DB/traceback, Docker image digests, runtime OCA SHAs, branch coverage across all modules/versions, Enterprise/custom addon sources, clean CI status, runtime transaction behavior, and end-to-end integrity.
9. **Potential improvements.** Immutable execution manifest, preflight data checks, per-run isolation, fatal restore/integrity gates, and checkpoint strategy; these are discovery directions, not a roadmap ([opportunities](IMPROVEMENT_OPPORTUNITIES.md)).
10. **Planning readiness.** **Ready for a bounded formal planning session focused on safety and evidence collection. Not ready to promise migration compatibility or approve production use.** Before solution design for the 18→19 incident, obtain a sanitized failing DB and full trace plus the actual OCA/image revisions.

## Investigation coverage

| Component | Coverage | Evidence / limitation |
| --- | --- | --- |
| Tracked CLI and core application | **Inspected** | All 805 lines of tracked Python inspected; call trace documented |
| CI/workflows and tracked package files | **Inspected** | Four workflows, gitignore, empty README, absence of tracked tests/manifest |
| OpenUpgrade 19.0 UoM pre/post and test | **Inspected** | Snapshot `bf12c3ce`; relevant files reviewed; no runtime execution |
| OpenUpgrade framework and library | **Partially inspected** | Framework initialization/patches and needed helper APIs at pinned lib SHA; not every script/API in upstream repo |
| Official Odoo 18/19 | **Partially inspected** | UoM models and 19 migration manager/Many2one conversion; not complete core or image builds |
| OpenUpgrade 16–18 scripts / full module coverage | **Partially inspected** | Branch identity and command selection; specific module paths not audited |
| PRs and upstream issue history | **Partially inspected** | PR #6054 and #2666 source/status checked; targeted issue search found no matching 19.0 multi-reference issue; exhaustive issue/discussion review not done |
| Runtime Docker/PostgreSQL/customer DB | **Inaccessible / not tested** | No build, DB credentials, image provenance, or authorized disposable fixture |
| Enterprise and custom addons | **Inaccessible / not tested** | No versioned source or installed-module manifest supplied |

## Deliverables

- [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md): components, ownership, architecture and sequence diagrams.
- [OPENUPGRADE_INTEGRATION.md](OPENUPGRADE_INTEGRATION.md): source revisions, framework mechanics, contract.
- [MIGRATION_PIPELINE.md](MIGRATION_PIPELINE.md): stage trace, failure and recovery behavior.
- [CODEBASE_AUDIT.md](CODEBASE_AUDIT.md): severity-ranked confirmed findings and risks.
- [COMPATIBILITY_MATRIX.md](COMPATIBILITY_MATRIX.md): wrapper paths versus verified support.
- [KNOWN_ISSUES_ANALYSIS.md](KNOWN_ISSUES_ANALYSIS.md): UoM mechanism, upstream PRs and evidence gaps.
- [IMPROVEMENT_OPPORTUNITIES.md](IMPROVEMENT_OPPORTUNITIES.md): evidence-led candidates without a roadmap.
- This document: executive summary and coverage.
