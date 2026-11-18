# Migration Fix Results

Implements the 8 findings from `docs/reviews/MIGRATION_RELIABILITY_ARCHITECTURE_REVIEW.md`.
Scope: fixes only, per the report's Recommended Fix for each finding. No other
code was refactored, redesigned, or added.

- **Branch:** `codex/local-resume-diagnostics`
- **Base commit:** `518898228897cd9e9f13d7479fa459cb93b285c4`
- **Date:** 2026-11-03

## Results Table

| Finding ID | Files Modified | Fix Applied | Test File | Test Result |
|---|---|---|---|---|
| DB-001 | `src/odooupgrader/jobs.py`, `src/odooupgrader/recovery.py` | Added `Job.prune_checkpoints(keep=1)` (deletes on-disk `checkpoints/<id>/` directories older than the latest, never touches the `completed` ledger); called from `_single_copy_checkpoint()` right after `job.publish()` commits. `Job.checkpoint()`'s chain-verification loop now treats a missing historical checkpoint directory as prior intentional pruning (skips its digest/parent check) instead of raising `CHECKPOINT_INVALID` — required so pruning doesn't break the existing full-chain validation. | `tests/test_jobs.py::test_prune_checkpoints_keeps_only_latest` | PASSED |
| MIG-001 | `src/odooupgrader/recovery.py` | Guarded the reusable-workspace first-time-adoption branch with `elif self.extra_addons and not addon.exists():` (mirrors the sibling non-reusable branch), so a stale `inputs/custom_addons` directory left by a prior interrupted attempt no longer causes `shutil.copytree` to raise `FileExistsError`. | `tests/test_runtime_cli.py::test_resume_extra_addons_skips_recopy_when_already_adopted` | PASSED |
| MIG-002 | `src/odooupgrader/core.py` | Added the missing `else: raise RecoveryError('INPUT_PREPARATION: ...')` branch to `process_extra_addons()` for a value that is neither an http(s) URL, a `.zip` file, nor an existing directory, instead of silently returning with an empty `custom_addons_dir`. | `tests/test_runtime_cli.py::test_process_extra_addons_rejects_unrecognized_source` | PASSED |
| DB-002 | `src/odooupgrader/recovery.py` | `_single_copy_capacity()` now requires `db_bytes + fs_bytes + reserve` (the full uncompressed size of the next checkpoint ZIP, estimated from `pg_database_size` and the live filestore inventory) instead of only `max(2 GiB, db_bytes // 10)`. | `tests/test_runtime_cli.py::test_single_copy_capacity_rejects_checkpoint_that_wont_fit` | PASSED |
| ARCH-003 | `src/odooupgrader/recovery.py` | Moved the "job already has retained extra addons; cannot replace on resume" check into `_configure_recovery()` (the engine), immediately after the job is loaded — raises `RecoveryError('ADDONS_CHANGED: ...')`. The existing `cli.py` `UsageError` pre-check was left in place as the fast CLI-level UX path, per the report ("duplicate-but-consistent is fine; CLI-only is not"). | `tests/test_runtime_cli.py::test_engine_rejects_extra_addons_replacement_without_cli` | PASSED |
| MIG-003 | `src/odooupgrader/recovery.py` | In `_image_module_names()`, reordered both the tar-parse-failure branch and the success branch to drain `process.stderr` **before** calling `process.wait()`, removing the two-PIPE deadlock precondition (child blocked writing a full stderr pipe while the parent blocks in `wait()`). | `tests/test_runtime_cli.py::test_image_module_scan_drains_stderr_before_waiting_on_export_failure` | PASSED |
| ARCH-001 | `src/odooupgrader/core.py` | Removed the dead `self.cwd`/`self.source_dir`/`self.output_dir`/`self.filestore_dir`/`self.custom_addons_dir` assignments from `OdooUpgrader.__init__` (they were always overwritten by `RecoveryMixin._set_attempt_paths()` before use). A future bypass of `_set_attempt_paths()` now raises `AttributeError` immediately instead of silently operating on `os.getcwd()`. | `tests/test_runtime_cli.py::test_cwd_based_paths_are_not_set_before_attempt_paths` | PASSED |
| ARCH-002 | `src/odooupgrader/recovery.py` | Added `RecoveryMixin._addons_fingerprint(files)` (`hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()`) and replaced all 4 inline occurrences of that expression in `run()` with calls to it. | `tests/test_runtime_cli.py::test_addons_fingerprint_matches_manual_hash` | PASSED |

## Exact Test Commands Executed

```bash
# New regression tests, individually, first to confirm they pass with fixes applied:
python3 -m pytest tests/test_jobs.py::test_prune_checkpoints_keeps_only_latest \
  tests/test_runtime_cli.py::test_image_module_scan_drains_stderr_before_waiting_on_export_failure \
  tests/test_runtime_cli.py::test_single_copy_capacity_rejects_checkpoint_that_wont_fit \
  tests/test_runtime_cli.py::test_resume_extra_addons_skips_recopy_when_already_adopted \
  tests/test_runtime_cli.py::test_process_extra_addons_rejects_unrecognized_source \
  tests/test_runtime_cli.py::test_engine_rejects_extra_addons_replacement_without_cli \
  tests/test_runtime_cli.py::test_cwd_based_paths_are_not_set_before_attempt_paths \
  tests/test_runtime_cli.py::test_addons_fingerprint_matches_manual_hash -v
# -> 8 passed

# Same 8 tests re-run against the pre-fix source (via `git stash` of only the
# three src/ files, then `git stash pop` to restore the fixes) to confirm each
# test actually fails without its corresponding fix -> all 8 failed as expected,
# including a reproduction of the exact FileExistsError from MIG-001.

# Full non-integration suite (regression check):
python3 -m pytest tests/ -q --ignore=tests/integration
# -> 131 passed (123 pre-existing + 8 new)

# Integration suite (requires explicit --approve-disposable-docker-tests / Docker;
# not exercised, per the read-only/no-Docker-mutation constraints of this task):
python3 -m pytest tests/integration -q
# -> 6 skipped (unchanged from baseline; no Docker flags were passed)

# Compile check:
python3 -m py_compile src/odooupgrader/core.py src/odooupgrader/recovery.py src/odooupgrader/jobs.py
# -> OK
```

## Summary

- All 8 findings fixed exactly as specified in the report's Recommended Fix sections.
- 8 new regression tests added (one per finding), each verified to (a) fail
  against the pre-fix code and (b) pass against the fixed code.
- Full non-integration test suite: **131 passed, 0 failed, 0 errors** (baseline
  was 123 passed before this change; no pre-existing test needed modification).
- Integration tests: unaffected, still skip without the Docker opt-in flags
  (not run, per the read-only/non-destructive constraints of this task).
- No unrelated files were modified. No new abstractions were introduced beyond
  what each finding's Recommended Fix explicitly called for (`_addons_fingerprint`
  for ARCH-002, `Job.prune_checkpoints` for DB-001).

### Note on scope interpretation (DB-001)

The report's Recommended Fix for DB-001 states pruning "must not touch
`job.data['completed']` history." That constraint is satisfied — pruning only
removes on-disk `checkpoints/<id>/` directories and never modifies the ledger.
However, implementing the prune exposed a direct dependency the original report
did not call out: `Job.checkpoint()` re-verifies every historical checkpoint's
on-disk manifest as part of its tamper-evidence chain walk, and would raise
`CHECKPOINT_INVALID` on the very next call after a prune if left unchanged. A
minimal, 4-line addition to that loop (skip the digest/parent check for an
entry whose directory no longer exists, in `src/odooupgrader/jobs.py`) was
necessary for the prune to not immediately break resume/finalize; this is
reported here for transparency rather than treated as an unrelated change,
since it is a correctness prerequisite of DB-001's own fix, not a separate
improvement. This is covered by the same regression test
(`test_prune_checkpoints_keeps_only_latest`), which asserts `checkpoint()`
still succeeds and resolves the latest entry after pruning has occurred.

### Tests not executed

None of the 8 regression tests, nor any pre-existing test, were skipped,
modified, or left unexecuted. The only tests not run are the pre-existing
Docker/native-PostgreSQL integration tests under `tests/integration/`, which
require explicit opt-in flags (`--approve-disposable-docker-tests`,
`--run-docker-restore-tests`, `--run-native-postgres-tests`) and real
Docker/PostgreSQL resources; they are unrelated to any of the 8 findings'
code paths in a way that would be exercised differently by these changes, and
running them was out of scope for a no-Docker-mutation fix task.
