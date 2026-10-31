# Runtime dependency and module-disappearance corrections

## Confirmed incident

Operator job on a Contabo-class disposable test host failed a real 17.0 -> 19.0
`--single-copy` run during `BUILDING_18.0`:

```text
maxminddb 2.5.2 has requirement setuptools>=68.2.2, but you have setuptools 68.1.2.
Command failed: docker run --rm --network none --entrypoint python3 <image> -m pip check
```

## Root cause established before editing

Pulling the pristine, unmodified official images and running `python3 -m pip check`
inside them (no OpenUpgrade clone, no custom addons, no application code involved)
reproduces the exact failure on **both** `odoo:18.0` and `odoo:19.0`:

```text
$ docker run --rm --entrypoint python3 odoo:18.0 -m pip check
maxminddb 2.5.2 has requirement setuptools>=68.2.2, but you have setuptools 68.1.2.
$ docker run --rm --entrypoint python3 odoo:19.0 -m pip check
maxminddb 2.5.2 has requirement setuptools>=68.2.2, but you have setuptools 68.1.2.
$ docker run --rm --entrypoint python3 odoo:17.0 -m pip check
zeep 4.1.0 requires platformdirs, which is not installed.
```

This is an upstream Odoo base-image defect, unrelated to this application, OpenUpgrade,
openupgradelib or any customer backup. The previous `_prepare_migration_image()` ran
`pip check` against the **fully built** image and treated any nonzero exit as fatal,
so every 17->18 and 18->19 migration was unconditionally blocked by a conflict this
project neither introduced nor can fix by editing the official image.

The separate `WARNING: Skipping .../charset_normalizer-3.3.2.dist-info due to invalid
metadata entry 'name'` line is pip's own warning about malformed Debian-packaged
metadata in the base image; it does not affect `pip check`'s exit code and was already
handled as non-fatal via the existing `incomplete_distribution_count` probe.

## Correction

`recovery.py` now runs `pip check` **twice**: once against the untouched `odoo:<target>`
base image (no OpenUpgrade clone, no custom addons -- the same image `FROM` starts from),
and once against the image this project actually built. Only conflicts present in the
built image but **absent from the baseline** are fatal (`DEPENDENCY_ERROR`); conflicts
already shipped in the pristine base image are recorded under
`preexisting_dependency_conflicts` in the runtime manifest and logged, never silently
dropped, and never block migration. A `pip check` invocation that itself cannot run
(bad image reference, Docker-level failure) is classified separately as
`DEPENDENCY_PROBE_FAILED` so it is never confused with a real package conflict. No
package in the image is upgraded, pinned, or patched to make the warning disappear;
the application does not alter upstream Odoo's bundled dependencies.

**Files/tests:** `src/odooupgrader/recovery.py` (`_pip_check_violations`,
`_dependency_conflicts`); `tests/test_runtime_cli.py`
(`test_preexisting_upstream_dependency_conflict_does_not_block_build`,
`test_new_dependency_conflict_introduced_by_build_is_fatal`,
`test_dependency_probe_infra_failure_is_distinct_from_a_real_conflict`). **Verified:**
reproduced against real pulled `odoo:17.0/18.0/19.0` images in this workspace, and
against the operator's actual retained job -- the same backup that previously failed at
`BUILDING_18.0` proceeded through a real `docker build`, a real `pip check` baseline
comparison, and a real Odoo 17->18 OpenUpgrade run after this fix, producing a
published checkpoint.

## Related: PostgreSQL 18 checkpoint capacity check used the wrong data path

`_restore_storage_snapshot()` (restore preflight) was already corrected to use
`/var/lib/postgresql` for PostgreSQL 18+ (see
[restore compatibility correction](RESTORE_COMPATIBILITY_FIX.md)), but `_capacity()` --
the **per-transition checkpoint** capacity gate used by non-`--single-copy` jobs -- still
hardcoded `/var/lib/postgresql/data`. Verified directly:

```text
$ docker run --rm postgres:18 sh -c 'df -Pk /var/lib/postgresql/data'
df: /var/lib/postgresql/data: No such file or directory
```

Any non-`--single-copy` job selecting PostgreSQL 18 (now the default `auto` selection
for many current pg_dump-18-produced backups) would fail `_capacity()` with an
unclassified `CalledProcessError` at the first checkpoint, instead of either passing or
reporting a clean `DISK_SPACE` diagnosis. Both call sites, plus `create_db_compose_file`,
now share one `_pg_data_volume_path()` helper so the PG13-17/PG18+ branching cannot
drift out of sync again. **Files/tests:** `src/odooupgrader/recovery.py`
(`_pg_data_volume_path`); `tests/test_postgres_restore.py`
(`test_pg_data_volume_path_matches_selected_major`,
`test_capacity_check_targets_postgres18_data_mount`,
`test_restore_storage_snapshot_targets_postgres18_data_mount`). This specific operator
job used `--single-copy`, so `_capacity()` was not on its execution path; the defect was
found by code audit and confirmed live against a real `postgres:18` container, not
observed as this job's failure.

## Related: module disappearance has no upstream rename/merge awareness

After the dependency fix above, the same real backup's 17->18 transition completed a
full real Odoo/OpenUpgrade run and then failed the post-migration technical check:

```text
Error: MODULE_MISSING: installed module disappeared; review upstream rename/merge mapping
```

Comparing the pre-migration installed-module baseline to the actual post-migration
`ir_module_module` state in the real migrated database identified exactly one
disappearance: `account_payment_term`. The exact OpenUpgrade 18.0 revision pinned by
this application (`a393ddbe56b6913eea1e77b42be93152ac00b16d`) ships its own
upstream-maintained mapping at `openupgrade_scripts/apriori.py`:

```text
merged_modules = {
    ...
    "account_payment_term": "account",
    ...
}
```

i.e. this is OpenUpgrade's own documented, intentional module merge (72 entries in
`renamed_modules`/`merged_modules` at this revision), not a data-loss defect, and not
something specific to this customer's backup -- `account_payment_term` is a
near-universal Accounting dependency, so the previous hard block would have failed
essentially every real-world Community Accounting 17->18 migration. `_technical()` had
no access to this registry and treated every disappearance identically.

**Correction:** `_prepare_migration_image()` now also imports
`openupgrade_scripts.apriori` from the exact built image (falling back to an empty
mapping if the module is absent or fails to import -- this is evidence used to recognize
an upstream-documented change, never a required gate) and records the combined
`renamed_modules`/`merged_modules` dict as `module_mapping` in the runtime manifest.
`_technical()` resolves each missing module through this mapping's rename/merge chain
(cycle-safe) and only raises `MODULE_MISSING` -- now naming the exact unaccounted
module(s) -- when a module's chain does not end in a currently-installed module. A
module with no mapping entry, or whose merge target was itself never installed, still
blocks exactly as before. No module is uninstalled, no state is cleared, and no mapping
is invented or guessed: the only source of truth is OpenUpgrade's own pinned,
version-exact registry.

**Files/tests:** `src/odooupgrader/recovery.py` (`_unaccounted_missing_modules`,
`_technical` `module_mapping` parameter, `_prepare_migration_image` probe extension);
`tests/test_runtime_cli.py` (`test_unaccounted_missing_modules_follows_rename_chain_and_detects_cycles`,
`test_module_merge_mapping_resolves_known_upstream_disappearance`,
`test_module_mapping_does_not_waive_an_unmapped_or_dangling_disappearance`).
**Verified:** the exact `apriori` import and mapping extraction was executed against the
real built `odoo:18.0`-based migration image for this job; the same backup's 17->18
transition published a verified checkpoint after this fix (see job evidence below).

## Executed verification

```text
.venv/bin/python -m pytest -q
# 114 passed, 6 skipped (skips are opt-in real Docker/PostgreSQL suites; unaffected by this change)
```

Real end-to-end evidence (operator's retained disposable job, Contabo-class
development host, real Docker, real `odoo:17.0`/`odoo:18.0`/`odoo:19.0` images, real
OpenUpgrade 17.0/18.0/19.0 clones at the application's pinned revisions, real
PostgreSQL 18 restore of the actual 17.0 backup): the 17.0 -> 18.0 transition built,
ran the real Odoo/OpenUpgrade upgrade, and published a verified checkpoint using the
fixes above. The 18.0 -> 19.0 transition then reproduced the unresolved
`lift_constraints`/PostgreSQL 17+ defect documented below; it is an upstream blocker,
not a regression from any fix in this document. **Full-chain 17.0 -> 19.0 completion
is NOT established by this evidence** -- see the unresolved section and this
project's final engineering report for the complete status.

## Unresolved: `lift_constraints` incompatible with PostgreSQL 17+ (confirmed, not fixed here)

After the three corrections above, the same real backup's 18.0 -> 19.0 transition ran
a real Odoo/OpenUpgrade migration against PostgreSQL 18 and failed:

```text
module stock_account: Running upgrade
alter table stock_valuation_layer drop constraint if exists stock_valuation_layer_id_not_null cascade,
  drop constraint if exists stock_valuation_layer_pkey cascade
ERROR: column "id" is in a primary key
File "post-migration.py", line 164, in migrate
File "post-migration.py", line 155, in product_value
File "openupgrade.py", line 2999, in lift_constraints
psycopg2.errors.InvalidTableDefinition: column "id" is in a primary key
```

**Root cause (confirmed upstream, not application-caused):** PostgreSQL 17 began
exposing `NOT NULL` as a named, catalogued constraint in `pg_constraint`
(`contype = 'n'`), which did not exist on PostgreSQL <= 16. `openupgradelib`'s pinned
`lift_constraints(cr, table, column, cascade=True)` (at this application's pinned
commit `565d0b33391271a4b2ac27eff8ae19f72fe27d77`) queries `pg_constraint` for every
constraint touching `column` and drops them all; on PostgreSQL 17+ this now also finds
and attempts to drop the column's implicit not-null constraint, which PostgreSQL
refuses when the column is part of a primary key (`column "id" is in a primary key`).
The `stock_account/19.0.1.1/post-migration.py` script's call
`openupgrade.lift_constraints(env.cr, "stock_valuation_layer", "id", cascade=True)`
hits this unconditionally for any database with `stock_account` installed.

This is a real upstream OpenUpgrade/openupgradelib and PostgreSQL version interaction,
already reported and tracked upstream:
[OCA/openupgradelib#462](https://github.com/OCA/openupgradelib/pull/462)
("`lift_constraints`: skip not-null constraints"), head
`327a371f9e176278c01b5d4f013cf4134180233e`, **open and unmerged** as of this
investigation. Its description matches this exact failure mode and mechanism.

**Why it was not patched here:** per this application's upstream-first policy (see
[KNOWN_ISSUES.md](KNOWN_ISSUES.md) and
[OPENUPGRADE_HARDENING_PLAN.md](../phase2/OPENUPGRADE_HARDENING_PLAN.md)), an
unmerged upstream PR is tracked, never silently adopted. Unlike the existing
`FACTOR_FIX_SCRIPT_SHA256` mechanism (a single OpenUpgrade *script* file, hash-gated
and opt-in per job), this fix lives inside `openupgradelib` itself, pinned once
(`LIBRARY_REVISION`) for every Odoo version this application supports; swapping it
conditionally for one target version would be a materially different, unreviewed
architecture change, not a "smallest safe fix." Patching the vendored library in
place, or special-casing `stock_account`'s script, would also violate "do not modify
upstream OpenUpgrade migration logic to hide failures."

**Current (correct) behavior:** the migration container exits nonzero, the wrapper
raises `MIGRATION_EXIT`, no checkpoint is published for 19.0, the prior verified 18.0
checkpoint is untouched, and the sanitized diagnostics bundle correctly identifies
`failing_module: "stock_account"` plus the exact script/library frame locations
(`post-migration.py:164`, `openupgrade.py:2999`) without exposing any customer data.
This is the wrapper behaving exactly as designed when it cannot safely proceed; it is
not a wrapper defect.

**This affects every Community database with `stock_account` installed and any
recorded stock valuation history, migrating 18.0 -> 19.0 through this application
while the attempt's PostgreSQL runtime is 17 or 18** (not specific to this operator's
backup). It is independent of the three fixes above and was not previously documented
in this project.

**Recommended next step (requires human decision, not performed here):** monitor
[PR #462](https://github.com/OCA/openupgradelib/pull/462) for review/merge; once an
upstream maintainer reviews and merges a fix, qualify the new commit (tests, `pip
check`, affected-script regression) and update `LIBRARY_REVISION` in `checks.py`
through the normal reviewed-plan-change process, exactly like any other pinned
revision change. Do not pin the open PR's commit without that review. No automatic
remediation is implemented or recommended as a stopgap.

## Rollback

Revert `recovery.py` to drop the baseline `pip check` diff and the `apriori` mapping;
no checkpoint format, ledger schema, or published checkpoint is affected by either
correction, so existing jobs remain readable. A job that published a checkpoint using
the module-mapping fix recorded the exact `module_mapping` used in that checkpoint's
runtime manifest for later audit; rolling the application back does not rewrite or
invalidate that published checkpoint.
