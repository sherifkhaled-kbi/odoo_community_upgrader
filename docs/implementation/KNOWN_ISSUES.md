# Focused known-issue resolution report

Evidence date: 2026-10-30. Local source was based on application commit `49c16285eb160b11d8a977bba278bf3a3bc333f1`; new code also records its dirty source fingerprint. No historical migration logs, customer traceback or incident database was present in the checkout. Phase 2's 18 registry entries are not 18 observed incidents. Only the following reported/code-backed blockers are addressed in the focused implementation.

## 17→18 inherited XML-ID helper

**Evidence:** OpenUpgrade 18 `a393ddbe56b6913eea1e77b42be93152ac00b16d`, base pre-migration, calls `add_inherits_parent_xmlids`. Library pin `565d0b33391271a4b2ac27eff8ae19f72fe27d77` contains that helper. The original application already configured this pin; the exact historical runtime/error is unknown.

**Correction:** retain the pin, checkout OCA by full commit, verify installed library VCS provenance, run dependency consistency checks and assert helper availability in the built 18 image **before** migration. Python requirement errors now fail rather than falling through to restore/migration. The 17 image is not required to expose the 18-specific helper.

**Files/tests:** `src/odooupgrader/recovery.py`, `checks.py`; `tests/test_runtime_cli.py` executes the actual generated capability probe against present/missing helper contracts and checks 17 behavior/build failure. **Verified:** local capability/error handling. **Blocked/NOT TESTED:** historical customer reproduction and real 17→18 runtime qualification. No claim that a supplied incident was fixed end-to-end.

Sources: [OCA base pre-migration](https://github.com/OCA/OpenUpgrade/blob/a393ddbe56b6913eea1e77b42be93152ac00b16d/openupgrade_scripts/scripts/base/18.0.1.3/pre-migration.py), [pinned helper](https://github.com/OCA/openupgradelib/blob/565d0b33391271a4b2ac27eff8ae19f72fe27d77/openupgradelib/openupgrade.py).

## 18→19 multiple legacy UoM references

**Reported signature:** `ValueError: Wrong value for uom.uom.relative_uom_id` during UoM post-migration. **Verified mechanism:** inspected OCA 19 SHA `bf12c3cee6097145e53efa589b0d99a7e64d6a87` aggregates references and assigns the resulting recordset to a Many2one. A faithful minimal ORM-contract reproduction executes the exact upstream function and raises on multiple references. Odoo 18's write path conditionally skips uniqueness checks; duplicate origin cannot be assigned to a customer/direct SQL without evidence.

**Correction:** read-only SQL diagnostics include inactive rows, require exactly one reference per category, reject nonfinite/nonpositive factors and non-unit references, and block ambiguity before any 19 data migration. No first-row selection, merge/delete, factor change or source normalization is implemented. Exception suppression/script skipping is excluded.

**Files/tests:** `checks.py`, `recovery.py`; `tests/test_checks.py`, `test_upstream_uom.py`, exact licensed upstream fixture `fixtures/uom_affected.py`. **Verified:** mechanism and safe blocking locally. **Blocked:** ambiguous source resolution needs a reviewed canonical mapping and disposable customer-specific proof. The PR below does not resolve semantic ambiguity merely by joining each reference separately.

Sources: [OCA UoM post-migration](https://github.com/OCA/OpenUpgrade/blob/bf12c3cee6097145e53efa589b0d99a7e64d6a87/openupgrade_scripts/scripts/uom/19.0.1.0/post-migration.py), [Odoo 19 Many2one converter](https://github.com/odoo/odoo/blob/a5447ae78fa15f25e69ce44fd154bf1d2f1e383a/odoo/orm/fields_relational.py), [Odoo 18 UoM write](https://github.com/odoo/odoo/blob/c35f52004d75c19c296e49e9a99e9b8d4447d67f/addons/uom/models/uom_uom.py).

## 18→19 custom bigger-unit inversion

**Verified mechanism:** source 18 stores a UI-created box-of-24 with `factor=1/24`; inspected 19 migration writes this directly as `relative_factor`, producing the inverse of the intended quantity. This can fail silently. Provider-owned XML-ID units are handled separately upstream.

**Correction:** default affected runtime blocks custom bigger units. [PR #6054](https://github.com/OCA/OpenUpgrade/pull/6054) remained **open/unmerged** when rechecked; its exact head is `5720f589e0760db3584cc19d19105feaf8fa9870`. Its candidate uses `1/factor`; tests execute that exact function and compare the affected function. Runtime support recognizes only the tested corrected script SHA-256 `85773a46db54fbeb8d5ed3e8358ca0ccc4c559864501144c3e75413a80d490a3`; it does not select the PR or patch upstream automatically. A reviewer may explicitly select that pending commit after qualification and exact plan acceptance. The actual built script hash is checked before migration. An unknown/new corrected script requires independent review/tests and an updated recognized hash; selecting an arbitrary SHA does not disable the guard.

Post-migration checks compare all original unit IDs and category-anchor ratios under the new absolute-factor semantics. A changed global root is permitted; inverted ratios block checkpoint publication. This establishes unrounded mathematical equivalence, not complete rounded ORM price/quantity, stock or accounting correctness.

**Files/tests:** `checks.py`, `recovery.py`, `tests/test_upstream_uom.py`, `test_checks.py`, `test_runtime_cli.py`, licensed `fixtures/uom_pr6054.py`. **Verified:** pinned script reproductions and ratio guard locally. **NOT TESTED:** actual Odoo 18→19, rounded conversions, financial/stock outcomes and customer trial. No controlled upstream patch was installed. Historical PR #2666 is a 14.0 change and is not adopted as a 19.0 fix.

## Wrapper defects required for recovery

| Root cause | Implemented correction / files | Executed verification / remaining risk |
| --- | --- | --- |
| Restore results ignored; SQL did not stop on error | Fatal clean-database restore, template0, custom archive single-transaction/exit-on-error, SQL ON_ERROR_STOP; `recovery.py` | Command/error tests in `test_runtime_cli.py`; real dump/extension compatibility NOT TESTED |
| Shared names and cleanup destroyed recoverability | Unique full-attempt resources/labels, job lock, retained immutable snapshots and owned-stop-only cleanup; `jobs.py`, `recovery.py`, existing `core.py` integration | Real multiprocess lock, corruption/publication and fresh-copy orchestration tests; Docker crash/reboot behavior NOT TESTED |
| Exception swallowed; exit 0 treated as sufficient | Re-raise original failure; CLI failure exit; SQL/module/attachment and disposable registry gates | Exit-zero-invalid-registry, partial-marker, failed-validation tests; normal registry behavior on actual images NOT TESTED |
| No durable history/filestore verification | Atomic per-job JSON, manifest chains/hashes, complete filestore plus attachment checks, fresh restore | R1/R2-style orchestration and hash/missing-file tests; real DB restores NOT TESTED |
| Pipe deadlock and inadequate evidence | Migration stdout/stderr merged to private file; automatic allowlisted ZIP and private traceback | Diagnostics exception categories, privacy/archive and collector-failure tests; high-volume real Docker run NOT TESTED |
| Floating OCA and rebuilt dependency drift | Full OCA/library pins, actual image/package provenance, runtime locks and explicit built-runtime diff acceptance | Plan/actual-dependency drift tests; official Odoo source SHA remains unknown; image build NOT TESTED |
| Input/output deletion endangered original backup | Private retained input copy, per-attempt output, no directory-wide cleanup | Original bytes/path preservation tests; original customer database never contacted |

Default OCA revisions: 17 `61daab09ab6c0d21234b96374d9291d3e02ee9e4`; 18 `a393ddbe56b6913eea1e77b42be93152ac00b16d`; 19 `bf12c3cee6097145e53efa589b0d99a7e64d6a87`. These are inspected pinned sources, not certified runtime releases. Existing historical remote builds used floating branches and their actual SHAs are unavailable.

## Rollback for every correction

Stop the foreground invocation; retain the ledger, diagnostics and latest published checkpoint. Revert a pending runtime only through a reviewed diff/acceptance and restore a new working copy. Never invert SQL or rerun on a partial DB. A code downgrade cannot assume compatibility with a newer ledger schema; preserve/restore a compatible whole-job backup while execution is stopped. There is no automatic cleanup, source rewrite or production-data rollback operation. With a missing/corrupt latest checkpoint, restore that exact backup or start a new job explicitly; never edit the pointer to force older recovery.
