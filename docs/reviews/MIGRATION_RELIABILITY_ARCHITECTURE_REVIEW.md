# OdooUpgrader — Migration Reliability & Architecture Review

## Executive Summary

The codebase shows an unusually high baseline of defensive engineering for this
problem space: atomic job-ledger writes (`atomic_json`), a cryptographically
verified checkpoint chain (`Job.checkpoint`/`Job.publish`), `--single-transaction`
restores, ownership-scoped Docker operations (`_inspect_owned`), and a
dependency-drift gate (`_runtime_guard`) that refuses to silently rebuild a
different image for a version already pinned. Most "obvious" failure modes
(partial restores, unowned container operations, checkpoint tampering, PG
version mismatches) are already closed with evidence-based raises rather than
best-effort logging.

The issues found are concentrated in two places that is newer and less
exercised than the rest of the system:

1. **The single-copy ("storage-constrained") checkpoint/resume path** —
   specifically the just-added "adopt `--extra-addons` on resume" feature and
   the single-copy checkpoint's disk-capacity accounting — which has
   measurably weaker test coverage than the rest of `recovery.py` and contains
   a crash-on-resume bug and a capacity-model gap that undermines the
   storage-constrained value proposition of the mode across multi-hop
   migrations (the headline use case for this tool, per `COMPATIBILITY_MATRIX.md`).
2. **Small duplications and dual-initialization patterns** in `core.py` /
   `recovery.py` that are not bugs today but are exactly the shape of thing
   that produces a silent-wrong-path bug the next time either file is touched.

No evidence of silent data corruption, inverted business values, or false
"success" was found in the core restore/migrate/checkpoint path itself — the
module-state validation in `_technical()` (installed-set diff, pending-state
check, constraint-validation check) acts as a strong independent backstop even
when an upstream signal (container exit code) would not by itself be
sufficient. That backstop is good architecture and is called out positively
below so it is not accidentally "refactored away" in future work.

## Repository Snapshot

- **Branch:** `codex/local-resume-diagnostics`
- **Commit:** `518898228897cd9e9f13d7479fa459cb93b285c4` (2026-10-31 23:03:53 +0200)
- **Review date:** 2026-11-03
- **Scope:** `src/odooupgrader/*.py` (2,734 LOC) — `recovery.py`, `core.py`,
  `jobs.py`, `postgres_restore.py`, `checks.py`, `diagnostics.py`, `cli.py`.
  Test suite (`tests/*`, 1,968 LOC) consulted for coverage gaps only, not for
  defects of its own.

## Architecture Overview

- **`cli.py`** — Click entry points (`run`/`resume`/`storage`/`diagnostics`);
  translates `RecoveryError` into `click.ClickException`/`UsageError`.
- **`core.py` (`OdooUpgrader`)** — source/addon acquisition (download, ZIP/dir
  extraction, nested-directory flattening), mixed into `RecoveryMixin`.
- **`recovery.py` (`RecoveryMixin`, 1,425 LOC)** — the actual engine: job/attempt
  lifecycle, Docker container/network orchestration, PostgreSQL restore
  invocation, per-major-version OpenUpgrade image build, migration execution,
  checkpoint publication, and the `run()` state machine that ties all of it
  together under `Job.lock()`.
- **`jobs.py`** — host-local, lock-protected job ledger (`job.json`) and an
  immutable, hash-chained checkpoint directory protocol (`Job.checkpoint`/`publish`).
- **`postgres_restore.py`** — pure, read-only dump-header parsing and
  cross-version compatibility admission rules (no I/O side effects besides the
  initial file read).
- **`checks.py`** — path-safety (`extract_zip`), attachment integrity, and UoM
  factor/ratio validation used around the documented 18→19 UoM inversion issue.
- **`diagnostics.py`** — allowlisted, redacted failure-evidence export; the
  single place raw exception text is allowed to leave the job directory (and
  even then only as structurally-stripped tracebacks/sqlstates).

Two storage models exist end-to-end: **normal mode** (a full directory copy —
`database.dump` + `filestore/` — published per completed major version) and
**single-copy mode** (one live DB/filestore reused across the whole run, with a
compact `checkpoint.zip` published per version instead of a directory copy).
Single-copy mode additionally supports **resuming a still-running or
interrupted pre-migration workspace** in place (`_reusable_workspace`) rather
than restoring a checkpoint from scratch — this is the path most of the
findings below live in, because it is also the path with the least test
coverage (`grep` for `extra_addons`/`extra-addons` across `tests/` finds no
case exercising `run()`'s new resume-adoption branches at all).

## Migration Reliability Findings

### MIG-001 — Resume can crash with an unhandled `FileExistsError` instead of a clean error

- **Category:** Migration
- **Severity:** High
- **Confidence:** Confirmed
- **Location:** `src/odooupgrader/recovery.py`, `RecoveryMixin.run()`, lines 1282–1288 (reusable-workspace branch)

**Root Cause.** When resuming a **single-copy** job whose reusable
pre-migration workspace exists but which has never had extra addons
(`job.data['config']['extra_addons']` is `False`) and the caller now supplies
`--extra-addons`, the code does:

```python
elif self.extra_addons:
    # First time this job is given extra addons; adopt and retain them like a fresh job would.
    self.process_extra_addons()
    shutil.copytree(self.custom_addons_dir, addon)
    self.job.data['config'].update(extra_addons=True, addons_sha256=...)
    self.job.save()
```

`shutil.copytree(..., addon)` is called with **no existence guard** on
`addon` (`self.job.path / 'inputs' / 'custom_addons'`). Twenty-five lines
later, the structurally identical non-reusable-workspace branch for the same
feature *does* guard it:

```python
if self.extra_addons and not addon.exists():
    shutil.copytree(self.custom_addons_dir, addon)
    ...
```

The asymmetry is the evidence that the guard was intentionally added in one
place and simply missed in its sibling.

**Evidence.** `recovery.py:1272-1288` vs. `recovery.py:1300-1315` (diff
`HEAD~5..HEAD`, commit `c6b70c8`).

**Failure Scenario.** A resume with `--extra-addons PATH` is interrupted
(process killed, OOM, host reboot) *after* `shutil.copytree` succeeds but
*before* `self.job.data['config'].update(...)` / `self.job.save()` commits —
a realistic window since no fsync/atomic-rename protects this particular
directory copy the way `atomic_json` protects the ledger write. On the next
`resume JOB --extra-addons PATH` attempt, `job.data['config']['extra_addons']`
still reads `False` (the save never happened), so the same `elif
self.extra_addons:` branch runs again, and `shutil.copytree` now raises
`FileExistsError` against the directory left by the previous attempt — an
unhandled Python exception, not a `RecoveryError`, surfacing a raw traceback
to the operator in a codebase whose last five commits are explicitly about
*not* doing that (`19da212 Fail loudly instead of silently...`).

**Impact.** A job that should be resumable gets stuck requiring manual
filesystem surgery (`rm -rf inputs/custom_addons`) to recover, defeating the
purpose of the resume feature for exactly the crash scenario it exists to
handle.

**Recommended Fix.** Mirror the non-reusable branch's guard:
`elif self.extra_addons and not addon.exists():`. If `addon` already exists
but `config['extra_addons']` is `False` (the crash-recovery case), that is
recoverable evidence, not a hard conflict — the safest minimal fix is the
guard above, which simply treats "already copied" as already-done and lets
the (idempotent) `config.update()`/`save()` complete.

**Regression Test.** Unit test: seed a job directory with
`inputs/custom_addons/` already populated and `config['extra_addons']=False`
(simulating the crash window), mark the attempt `workspace_ready`, then call
`resume(..., extra_addons=...)` and assert it completes (or at least does not
raise `FileExistsError`) instead of crashing.

---

### MIG-002 — Newly-supplied `--extra-addons` on resume bypasses source-accessibility validation and can silently adopt an empty addons set

- **Category:** Migration
- **Severity:** High
- **Confidence:** Confirmed
- **Location:** `src/odooupgrader/recovery.py:1282-1288` and `:1306-1315`; `src/odooupgrader/core.py:91-132` (`validate_source_accessibility`), `:185-264` (`process_extra_addons`)

**Root Cause.** The fresh-job path validates `self.extra_addons` up front via
`validate_source_accessibility()` (checks URL reachability / path existence
*before* attempting extraction) and only then calls `process_extra_addons()`.
The new resume-adoption branches call `process_extra_addons()` directly,
skipping `validate_source_accessibility()` entirely (it can't be reused
as-is, since `self.source` is `''` on resume and would fail its own check).
`process_extra_addons()` itself has no `else: raise` for an input that is
neither an http(s) URL, a `.zip` file, nor an existing directory — it simply
returns having done nothing, leaving `custom_addons_dir` empty.

**Evidence.**
```python
# core.py process_extra_addons()
elif os.path.isdir(self.extra_addons):
    ...
# no else/raise — a bad path falls through silently
```
combined with `recovery.py`'s resume branches never calling the one method
(`validate_source_accessibility`) that would have caught a bad path first.

**Failure Scenario.** `odooupgrader resume JOB --extra-addons /typo/path`
(path doesn't exist, isn't a URL, isn't a `.zip`): `process_extra_addons()`
matches none of its branches and returns; the code then unconditionally
copies the now-empty `custom_addons_dir` into `inputs/custom_addons`, hashes
it, and marks `job.data['config']['extra_addons'] = True`. The job is now
permanently recorded as "has retained extra addons" with zero actual modules
in it. Worse: `cli.py`'s own new guard (`if extra_addons and
job.data['config'].get('extra_addons'): raise UsageError('... cannot replace
them on resume')`) now permanently blocks any future attempt to supply the
*correct* addons path for this job — the only way out is to abandon the job
or hand-edit `job.json`.

**Impact.** A typo or a moved/renamed addons directory turns into a silent,
unrecoverable (via the documented CLI) misconfiguration rather than an
immediate, actionable error — directly contrary to this session's own
"fail loudly" principle applied elsewhere (`_image_module_names`,
`_missing_module_preflight`).

**Recommended Fix.** Either (a) factor an addon-only accessibility check out
of `validate_source_accessibility()` (it already has a `self.extra_addons`
branch in lines 112-132 that is independent of the `self.source` branch
above it) and call it before `process_extra_addons()` in both new branches,
or (b) add the missing `else: raise RecoveryError('INPUT_PREPARATION: ...')`
to `process_extra_addons()` itself, which closes the gap for every caller at
once (fresh job included) and is the smaller, more durable fix.

**Regression Test.** `resume(..., extra_addons='/does/not/exist')` must raise
a `RecoveryError`/`ClickException`, and `job.data['config']['extra_addons']`
must remain `False` afterward (so a corrected `--extra-addons` can still be
supplied on a later resume).

---

### MIG-003 — Potential subprocess deadlock when `docker export` emits a large stderr payload during module-inventory scanning

- **Category:** Migration
- **Severity:** Medium
- **Confidence:** Suspected (plausible, not reproduced)
- **Location:** `src/odooupgrader/recovery.py:605-640` (`_image_module_names`)

**Root Cause.** Both the tar-parse-failure branch and the success branch call
`process.wait()` **before** draining `process.stderr`, with both
`stdout`/`stderr` opened as `subprocess.PIPE`:

```python
except Exception as exc:
    process.stdout.close()
    process.wait()                       # <- before stderr is read
    stderr = (process.stderr.read() or b'').decode(errors='replace')
```

This is the classic two-PIPE deadlock precondition: if the child has written
more to stderr than the OS pipe buffer (commonly 64 KiB) while the parent
isn't reading it, the child blocks on the write and `wait()` blocks on the
child — forever.

**Failure Scenario.** `docker export <container>` fails in a way that emits a
large/looping diagnostic to stderr (e.g. a daemon-side race where the
container is removed mid-export and retried diagnostics repeat), while the
tar parser aborts early on stdout. `process.wait()` then hangs, stalling the
whole migration run with no timeout.

**Impact.** Full job hang requiring manual `kill`/`docker kill` intervention;
masquerades as "stuck migration" rather than a clear error.

**Recommended Fix.** Read stderr (or drain both streams via a background
thread / `communicate()`) before or concurrently with `wait()`, consistent
with the pattern `subprocess.Popen.communicate()` already implements safely.

**Regression Test.** Hard to unit-test deadlocks deterministically; a
reasonable regression test fakes a `Popen` whose `stderr` is a pipe that
blocks on a `.read()` call until a buffer threshold, and asserts the scan
completes under a wall-clock bound, or simply switches the implementation to
`communicate()` and relies on that API's documented deadlock-freedom instead
of asserting behavior directly.

## Database & Data Integrity Findings

### DB-001 — Single-copy checkpoints are never pruned, undermining the mode's own capacity model across multi-hop migrations

- **Category:** Database & Data Integrity
- **Severity:** Critical
- **Confidence:** Confirmed
- **Location:** `src/odooupgrader/recovery.py:726-730` (`_single_copy_capacity`), `:1171-1187` (`_single_copy_checkpoint`), `:1364-1384` (`run()` transition loop); `src/odooupgrader/jobs.py:172-252` (`Job.checkpoint`/`publish`, no deletion path exists anywhere in the ledger)

**Root Cause.** `_single_copy_checkpoint()` is called once **per major-version
transition** in the `run()` loop (line 1376) and always calls
`self.job.publish(...)`, which always creates a brand-new
`checkpoints/<uuid>/checkpoint.zip` (a full compressed `pg_dump` + filestore)
and appends it to `job.data['completed']`. Neither `Job.publish` nor anything
else in the codebase (confirmed via `grep -n "rmtree\|unlink" recovery.py
jobs.py`) ever removes a previously-published checkpoint. Meanwhile,
`_single_copy_capacity()` — the pre-flight space check run before *each* new
checkpoint — only requires:

```python
reserve = max(2 * 1024**3, db_bytes // 10)   # ~10% of current DB size, or 2 GiB
```

with the comment *"Keep a modest reserve; no second database or filestore is
created"*. That comment is only true for the *live working copy*; it is not
true for the accumulated history of checkpoint ZIPs, each of which is
comparable in size to a full DB+filestore dump.

**Evidence.** `jobs.py` `Job.checkpoint()` (lines 172-207) only ever resolves
`self.data['latest_checkpoint']` — the code never reads from, or falls back
to, an older checkpoint — so retaining every historical checkpoint provides
**no functional benefit** today; it is pure unbounded accumulation with
nothing downstream designed to use it.

**Failure Scenario.** This tool's own compatibility matrix spans PostgreSQL
13–18 / Odoo up to 19.0 across multiple major-version hops (e.g. 15→16→17→
18→19 is 4 transitions). In single-copy mode — the mode explicitly marketed
for storage-constrained hosts (`console.print('Storage mode: one working
database/filestore; ZIP checkpoint after each successful version')`) — each
of those 4 transitions leaves a full compressed DB+filestore ZIP on disk
forever. A host sized for "the live DB plus ~10%" per the capacity check
will run out of disk on, typically, the 2nd–4th transition of a long chain,
producing a `DISK_SPACE` failure **inside the feature whose entire purpose is
to avoid exactly that** on constrained hosts.

**Impact.** Silent, slow-building disk exhaustion risk for the long-chain
upgrade scenarios this tool is built for; when it does trigger, it looks like
an unrelated `DISK_SPACE` failure rather than a pointer back to the root
cause (unbounded checkpoint retention).

**Recommended Fix.** Prune checkpoints that are no longer the single
`latest_checkpoint` once a new one for a job in single-copy mode is
successfully published and the ledger entry recorded — i.e. keep at most the
most recent N (N=1 is consistent with "no fallback to an older checkpoint
exists" above). This must happen *after* `Job.publish()`'s atomic rename and
ledger commit (never before — the ledger write is the correctness boundary),
and must not touch `job.data['completed']` history (which is append-only
provenance, not storage) — only unlink the on-disk `checkpoints/<old-id>/`
directory. Alternatively, size `_single_copy_capacity()` for the true
worst case (number of remaining hops × average checkpoint size) if retention
is intentional for forensic/rollback-to-any-version reasons; as written,
neither the capacity check nor any code path backs that intention.

**Regression Test.** Simulate a 3-hop single-copy `run()` (via the existing
`upgrader` test fixture pattern in `tests/test_runtime_cli.py`) and assert
that `len(list((job.path / 'checkpoints').iterdir()))` does not grow
unboundedly with the number of completed transitions (e.g. stays at 1, or
at whatever the chosen retention policy specifies).

---

### DB-002 — `_single_copy_capacity()`'s reserve is not sized to the checkpoint it is about to create

- **Category:** Database & Data Integrity
- **Severity:** Medium
- **Confidence:** High Confidence
- **Location:** `src/odooupgrader/recovery.py:726-730`

**Root Cause.** Even for a *single* checkpoint write (ignoring DB-001's
accumulation), `_single_copy_capacity()`'s `max(2 GiB, db_bytes // 10)`
reserve has no relationship to the size of the ZIP about to be streamed by
`_write_compact_checkpoint_zip` (a full `pg_dump` plus every filestore file).
Contrast with the normal-mode `_capacity()`, which explicitly demands `3 *
db_bytes + 3 * fs_bytes`. The single-copy path instead relies entirely on the
per-block `require_space()` call inside `copy_with_reserve()` to fail
*during* the write if space runs out — which is safe (no corruption; `Job.publish`
does clean up the aborted `.staging-*` directory on `BaseException`, per
`jobs.py:214-223`), but means the upfront "capacity" check is close to a
no-op for its stated purpose and gives operators no early warning before a
multi-minute `pg_dump`+ZIP stream is attempted and aborted partway through.

**Evidence.** Compare `_capacity()` (recovery.py:716-724, `3 * db_bytes + 3 *
fs_bytes`) to `_single_copy_capacity()` (recovery.py:726-730, `max(2 GiB,
db_bytes // 10)`), and the `_write_compact_checkpoint_zip` body
(recovery.py:1132-1169) which has no size estimate check before starting.

**Impact.** Wasted time (a long `pg_dump` + file streaming attempt that was
knowable in advance to fail) rather than an immediate, informative
`DISK_SPACE` pre-flight error; moderate severity because the failure mode is
still safe (no corruption), just late and uninformative.

**Recommended Fix.** Estimate filestore size (`inventory(self.filestore_dir)`
sum, already computed inside `_write_compact_checkpoint_zip` itself — hoist
it) plus `db_bytes`, and require that much (minus expected ZIP compression,
conservatively assume none) before starting the stream, consistent with how
`_capacity()` already does this for normal mode.

**Regression Test.** Unit test `_single_copy_capacity()` with a mocked
`_sql`/`inventory` returning a DB+filestore size larger than
`shutil.disk_usage` free space, and assert it raises `DISK_SPACE` before any
`docker exec pg_dump` command is invoked.

## Architecture & Maintainability Findings

### ARCH-001 — Duplicate, order-dependent initialization of working-directory paths between `core.py.__init__` and `recovery.py._set_attempt_paths`

- **Category:** Architecture
- **Severity:** Medium
- **Confidence:** Confirmed
- **Location:** `src/odooupgrader/core.py:36-40`; `src/odooupgrader/recovery.py:210-227` (`_set_attempt_paths`)

**Root Cause.** `OdooUpgrader.__init__` sets `self.cwd`, `self.source_dir`,
`self.output_dir`, `self.filestore_dir`, `self.custom_addons_dir` from
`os.getcwd()`. Whenever a `Job` is involved (always, once `_configure_recovery`
attaches one), `_set_attempt_paths()` immediately overwrites every one of
those five attributes with attempt-work-directory-relative values. The
`__init__`-time values are therefore dead code on every real code path today,
but nothing in the type system or a single source of truth prevents a future
change (e.g. a new early-return path, or a refactor that defers calling
`_set_attempt_paths`) from silently using the wrong (cwd-based, cross-job)
directory for destructive operations like `shutil.copytree`/`rmtree`.

**Evidence.** `core.py:36-40` vs. `recovery.py:210-227` define the exact same
five attribute names independently; `_set_attempt_paths` is only reachable
from `_new_attempt()` and the `reusable` branch of `run()`.

**Impact.** Maintainability/fragility risk rather than an active bug: the
two initializations can silently drift (e.g. a path added in one and not the
other), and any new code path that reads these attributes before
`_set_attempt_paths()` runs would operate on the wrong directory without any
error — a dangerous *silent-wrong-location* class of bug given several of
these paths are passed to `shutil.rmtree`/`copytree`.

**Recommended Fix.** Remove the cwd-based assignments from `__init__`
entirely and raise `AttributeError`-by-design (or use a sentinel/property
that raises a clear `RecoveryError` if accessed before
`_set_attempt_paths()` has run), so a future bypass fails loudly instead of
quietly operating on the wrong directory.

**Regression Test.** Not independently testable as a behavior today (no
observable difference exists); covered implicitly if ARCH work adds the
fail-fast accessor and a test asserts `AttributeError`/`RecoveryError`
pre-`_set_attempt_paths`.

---

### ARCH-002 — Addons-inventory SHA-256 computation duplicated across four call sites in `run()`

- **Category:** Architecture
- **Severity:** Low
- **Confidence:** Confirmed
- **Location:** `src/odooupgrader/recovery.py:1277-1279, 1286-1287, 1303-1304, 1313-1314`

**Root Cause.** The expression
`hashlib.sha256(json.dumps(inventory(path), sort_keys=True).encode()).hexdigest()`
is repeated near-verbatim four times within `run()` (two pre-existing, two
newly added by the `--extra-addons`-on-resume feature), rather than factored
into a single `_addons_fingerprint(path)` helper.

**Impact.** Not a bug today, but a change to the fingerprinting scheme (hash
algorithm, path normalization, symlink policy) requires editing four
scattered call sites ~10-30 lines apart across two branches; missing one
(easy, given MIG-001/MIG-002 show this exact code region is already
error-prone) silently leaves an inconsistent fingerprint definition for one
resume path versus another.

**Recommended Fix.** Extract a private helper (e.g.
`self._addons_fingerprint(path)`) and use it at all four sites.

**Regression Test.** N/A for behavior; a refactor-safety test could assert
all four call sites produce identical digests for identical directory
contents (would have caught were they ever to silently diverge).

---

### ARCH-003 — The resume/`--extra-addons` replacement safety rule is enforced only in the CLI layer, not in the engine

- **Category:** Architecture
- **Severity:** Medium
- **Confidence:** Confirmed
- **Location:** `src/odooupgrader/cli.py:202-203`; `src/odooupgrader/recovery.py:1273-1288`

**Root Cause.** The invariant "a job that already has retained extra addons
cannot have them replaced via `--extra-addons` on resume" is checked exactly
once, in `cli.py`'s `resume()` command, before `OdooUpgrader` is even
constructed. `RecoveryMixin.run()` itself has no equivalent check: it simply
trusts that if `job.data['config'].get('extra_addons')` is already `True`,
`self.extra_addons` must be `None`/falsy, and otherwise takes the
newly-supplied value at face value. Every other safety invariant of similar
importance in this codebase (ownership checks, checkpoint chain validation,
runtime-drift gates) is enforced at the engine level, not the CLI level, so
this is an inconsistency with the codebase's own pattern, not just a
theoretical gap.

**Evidence.** `cli.py:202-203` is the only occurrence of the string
"`already has retained extra addons`" / this check in the repository; `grep
-rn "job.data\['config'\].get('extra_addons')" src/odooupgrader/recovery.py`
shows `run()` reading but never asserting-then-rejecting a conflicting input.

**Impact.** Any future caller of `OdooUpgrader`/`RecoveryMixin.run()` that
does not go through `cli.py resume` (a test harness, a future API wrapper, a
batch-reprocessing script) silently bypasses this safety rule; depending on
timing, this could re-trigger MIG-001/MIG-002's failure modes from a
different entry point with no guard at all.

**Recommended Fix.** Move the check into `run()` (or `_configure_recovery`)
itself, immediately after the job is loaded, and have `cli.py` continue to
pre-check as a fast UX path (duplicate-but-consistent is fine; CLI-only is
not).

**Regression Test.** Construct `OdooUpgrader` directly (bypassing `cli.py`)
against a job with `config['extra_addons']=True` and a non-empty
`extra_addons=` argument, call `.run()`, and assert it raises before doing
any addon I/O.

## Findings Summary

| ID | Severity | Category | File:Line | Finding | Priority | Confidence |
|---|---|---|---|---|---|---|
| DB-001 | Critical | Database & Data Integrity | recovery.py:726-730, 1171-1187, 1364-1384 | Single-copy checkpoints accumulate forever; capacity check doesn't account for it | P0 | Confirmed |
| MIG-001 | High | Migration | recovery.py:1282-1288 | Resume+`--extra-addons` can crash with unhandled `FileExistsError` after a partial failure | P1 | Confirmed |
| MIG-002 | High | Migration | recovery.py:1282-1315; core.py:91-264 | Bad/missing `--extra-addons` on resume silently adopted as empty, permanently misconfiguring the job | P1 | Confirmed |
| DB-002 | Medium | Database & Data Integrity | recovery.py:726-730 | Single-copy capacity reserve not sized to the checkpoint being written | P1 | High Confidence |
| ARCH-003 | Medium | Architecture | cli.py:202-203; recovery.py:1273-1288 | Extra-addons-replacement safety rule enforced only in CLI, not engine | P2 | Confirmed |
| MIG-003 | Medium | Migration | recovery.py:605-640 | Possible subprocess deadlock draining stderr after stdout parse failure | P2 | Suspected |
| ARCH-001 | Medium | Architecture | core.py:36-40; recovery.py:210-227 | Duplicate, order-dependent path initialization (cwd-based vs. job-based) | P2 | Confirmed |
| ARCH-002 | Low | Architecture | recovery.py (4 sites) | Duplicated addons-fingerprint hashing logic | P3 | Confirmed |

## Prioritized Remediation Roadmap

**P0 — Immediate critical risk**
- DB-001: Add checkpoint pruning (or correctly size capacity for retention) in
  single-copy mode before any customer runs a long multi-hop chain on a
  storage-constrained host — this is the mode's headline use case failing on
  its own terms.

**P1 — High-priority reliability fixes**
- MIG-001: Guard the reusable-workspace `elif self.extra_addons:` branch with
  `not addon.exists()`, matching its sibling branch.
- MIG-002: Validate `self.extra_addons` accessibility before
  `process_extra_addons()` on resume, or add the missing `else: raise` inside
  `process_extra_addons()` itself.
- DB-002: Size `_single_copy_capacity()` to the DB+filestore bytes actually
  about to be streamed.

**P2 — Maintainability and resilience improvements**
- ARCH-003: Move the extra-addons-replacement invariant into the engine.
- MIG-003: Drain stderr before/while waiting on the `docker export` process in
  `_image_module_names`, or switch to `communicate()`.
- ARCH-001: Remove dead `__init__`-time path assignments or make them fail
  loudly if read before `_set_attempt_paths()`.

**P3 — Lower-priority improvements**
- ARCH-002: Factor the repeated addons-fingerprint hashing into one helper.

## Regression Test Recommendations

- A resume-with-`--extra-addons` test matrix is currently **absent** from
  `tests/test_runtime_cli.py`/`tests/test_recovery.py` despite being the
  single largest source of findings in this review. At minimum, add: (a)
  happy path — first-time adoption on a reusable workspace and on a
  non-reusable resume; (b) crash-recovery — `addon` directory already present
  but `config['extra_addons']` still `False` (MIG-001); (c) invalid input —
  nonexistent path / unreachable URL (MIG-002); (d) conflict — resuming with
  `--extra-addons` when the job already has them, exercised against the
  engine directly and not just the CLI (ARCH-003).
- A multi-hop single-copy `run()` test asserting checkpoint directory count
  does not grow unboundedly (DB-001).
- A `_single_copy_capacity()` unit test with a deliberately undersized free-space
  mock relative to DB+filestore size (DB-002).

## Review Coverage

**Fully reviewed (read in detail):**
- `src/odooupgrader/jobs.py` (267 LOC, 100%)
- `src/odooupgrader/postgres_restore.py` (198 LOC, 100%)
- `src/odooupgrader/checks.py` (99 LOC, 100%)
- `src/odooupgrader/diagnostics.py` (153 LOC, 100%)
- `src/odooupgrader/recovery.py` — approx. 90%: all of job/attempt lifecycle,
  restore (`_restore_dump`, `restore_database`, `_prepare_restore_input`),
  checkpoint publication (both modes), the `run()` state machine end-to-end,
  module-inventory scanning, dependency-conflict detection, image build
  (`_prepare_migration_image`), and the live-progress container runner.
- `src/odooupgrader/core.py` — key paths: `__init__`, `_run_cmd`,
  `validate_source_accessibility`, `process_extra_addons`,
  `generate_next_version`.
- `src/odooupgrader/cli.py` — the `resume` command and its interaction with
  `recovery.py`'s new resume branches.

**Not independently re-verified in this pass (lower risk / out of the stated
reliability scope, or already covered by a prior review session on this
branch):**
- `cli.py`'s `run`/`storage`/`diagnostics` commands beyond their direct
  interaction with the findings above.
- `core.py`'s directory-flattening/requirements.txt heuristics in
  `process_extra_addons()` past the validation gap already identified in MIG-002.
- `tests/integration/*` (Docker-dependent integration tests) — not executed
  (read-only audit; no containers were started).
- Full line-by-line review of `tests/*.py` — consulted only via `grep` to
  confirm/deny coverage of specific findings (notably the complete absence
  of resume+`--extra-addons` coverage).

**Methodology note / skill limitation.** The installed `code-review` skill's
output contract is a fixed-schema JSON finding list intended for diff review,
not a prioritized Markdown architecture report against an entire codebase, so
it was not invoked as a subprocess for this task; its methodology (multi-angle
hypothesis generation, then independent verification against source before
accepting a finding) was applied manually instead, per finding above. Equally,
`diagnosing-bugs` targets one reported symptom and `research` targets external
documentation — neither fits a static multi-file reliability audit, so no
findings in this report were produced by those skills.

## Remaining Risks

- **DB-001/DB-002 real-world impact is reasoned from code, not measured.**
  No integration run was performed (per the read-only constraint); the actual
  ZIP compression ratio and therefore how many hops a given host can sustain
  before `DISK_SPACE` triggers was not empirically measured.
- **MIG-003 is unreproduced.** It is a plausible deadlock precondition from
  code inspection, not a confirmed hang; flagged as Suspected accordingly.
- **UoM 18→19 and 17→18 XML-ID logic** (`checks.py` `validate_uom`,
  `verify_uom_ratios`) were read and found internally consistent with the
  documented known-issue scope (project memory:
  `odoo_upgrader_known_issues.md`), but this review did not attempt to
  independently derive whether that documented scope (only `type == 'bigger'`
  units gated by `provider_xmlid`) is itself complete — that determination
  requires Odoo/OpenUpgrade domain evidence beyond static code reading and
  was treated as already-settled project knowledge rather than re-litigated here.
- Items under "Not independently re-verified" above could contain additional
  findings; they were deprioritized based on lower blast-radius relative to
  the explicit review priorities (restore integrity, resume/checkpoint,
  Docker lifecycle) rather than confirmed to be clean.

## Final Recommendations

1. Fix DB-001 before relying on single-copy mode for any multi-hop migration
   on a storage-constrained host — it is the one finding that can turn "the
   tool is working as designed" into an in-flight failure on exactly the
   deployment profile it targets.
2. Close MIG-001/MIG-002 together — both sit in the same few lines of `run()`,
   are both consequences of the same feature having effectively no test
   coverage, and a single focused PR with the regression tests in "Regression
   Test Recommendations" above resolves both.
3. Treat ARCH-003 as a cheap insurance fix: move one `if` statement from
   `cli.py` into `run()` so the engine is safe regardless of entry point.
4. The remaining architecture items (ARCH-001, ARCH-002) are worth doing
   opportunistically the next time either file is touched, not urgent on
   their own.
