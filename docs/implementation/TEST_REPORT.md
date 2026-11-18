> Initial CLI implementation report. For the subsequent PostgreSQL restore correction and current validation, see [RESTORE_COMPATIBILITY_FIX.md](RESTORE_COMPATIBILITY_FIX.md).

# Test execution and server qualification

## Executed safely in this development workspace

The workspace is not established as the migration server. Docker context inspection reported `default`, `unix:///var/run/docker.sock`; no Docker/database integration commands were run. At initial inspection the workspace filesystem had approximately 246 GiB free. This is not Contabo capacity evidence.

Command:

```bash
.venv/bin/python -m pytest -q
```

Current result: **72 passed, 2 skipped**. The two opt-in real Docker/Odoo chain cases are explicitly NOT TESTED. Local suite uses temporary files, a real kernel/multiprocess lock, deterministic database/runtime adapters and exact upstream-function fixtures. It does not create Docker resources or contact a database.

Executed coverage:

- UoM duplicate/missing/inactive-reference guards and invalid factors; exact affected upstream function reproduction and exact PR-candidate factor comparison; anchor-normalized equivalence with changed roots/inversion rejection.
- Actual generated helper capability probe with/without the 18 helper, including unchanged 17 behavior; dependency-build failure stops migration; fatal restore command construction and readonly SQL diagnostics.
- Atomic ledger failure, checkpoint publication/orphans, missing/corrupt latest checkpoint and no fallback; parallel ownership using two processes.
- 17→18 and 18→19 failures after simulated partial commits, fresh resume with distinct attempts, completed transition counts/hashes preserved, original ZIP unchanged.
- Interrupted state detection, dry-run with no file changes, reviewed pending plan and built image/dependency drift, failed restore/disk admission/validation, final-checkpoint packaging-only retry.
- Python/OpenUpgrade-style, PostgreSQL, Docker, dependency and unknown diagnostic failures; collector failure preserves original exception; archive readability and secret/customer-payload exclusion.
- CLI legacy options, usage/failure exit, local-only endpoint rejection, unique non-restarting resources, partial DB and exit-zero registry refusal.

`python -m compileall -q src` and `git diff --check` also run as syntax/whitespace checks. Packaging metadata and CLI help are checked locally; wheel building is NOT TESTED because build/setuptools are absent from the existing virtual environment. No dependency installation was performed by Codex. No percentage coverage is claimed (coverage tooling is not installed).

## Explicitly untested / blocked

- Real source backup restore, full 16→17→18→19, exact container image builds/library installation and normal registry adapters.
- SIGKILL/reboot of a real migration, PostgreSQL partial-commit recovery, actual disk exhaustion/permissions and producer image retention.
- Final ZIP import through target Odoo backup manager, custom/Enterprise modules or external attachment stores.
- Rounded UoM quantity/price conversions, stock quantities/valuation, accounting balances and customer UAT.
- Historical 17→18 incident and ambiguous customer reference-UoM remediation: protected incident evidence was not supplied.

No skipped scenario is called successful. Current implementation is ready for isolated server qualification, not certified for production migration.

## Exact server-side opt-in procedure

These commands are instructions only. Obtain explicit approval for disposable Docker/database test operations before running the opt-in command. Do not use the original customer database or an unapproved customer backup. Provide a **synthetic disposable Odoo 16 ZIP** containing `dump.sql` and its matching `filestore/`, initialized with base/web only for the first qualification. Keep the original fixture read-only and record its SHA-256. Richer UoM/account/stock/custom fixtures require separate qualification and reviewed business assertions.

On the actual Linux Docker host, first inspect without mutation:

```bash
cat /etc/os-release
docker context show
docker context inspect --format '{{.Endpoints.docker.Host}}'
docker info --format '{{.DockerRootDir}}'
docker ps -a --format '{{.ID}} {{.Names}} {{.Status}}'
df -h .
df -i .
```

Check actual Docker storage filesystem capacity as well as the job/test directory. Confirm no remote endpoint/proxy is selected, test resources will be new and labeled, and capacity supports all retained checkpoints, working/verification databases, filestores and build layers. The original PG 13 default is retained for compatibility; choose an explicitly verified source/target-compatible PG version and extensions for your fixture. Do not assume this report has qualified that combination.

After authorized installation from this checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Only after disposable-test approval, run:

```bash
.venv/bin/python -m pytest tests/integration/test_local_docker.py -vv \
  --approve-disposable-docker-tests \
  --odoo16-test-backup /srv/disposable-odoo-tests/odoo16-base.zip \
  --test-postgres-version 13 \
  --basetemp /srv/disposable-odoo-tests/pytest-run-UNIQUE-EMPTY-DIRECTORY
```

Choose a **new empty basetemp** for every run: pytest may delete an existing basetemp directory. It must never contain backups or existing jobs. The integration tests execute real checkpoint dumps/restores/registry probes and inject a controlled failure immediately before 17→18 or 18→19, then resume and assert dispatch counts, immutable checkpoint hashes and unchanged input bytes. They retain Docker resources and evidence for inspection; no broad prune/automatic deletion is implemented.

For process-loss qualification on another explicitly approved disposable job, run the CLI inside external `tmux`; capture its PID/job ID, terminate only that CLI process under an approved test procedure, then run `status`, `resume --dry-run`, and explicit `resume`. Verify the previous attempt is recorded interrupted, owned writers stop, new DB/filestore resources are allocated, and no earlier completed transition repeats. A real reboot test needs separate host approval and is not performed by the suite.

For missing/corrupt checkpoint tests, use a **copy of an inactive disposable job**, retain its original ledger/snapshot backup, corrupt only that copied test snapshot under approval, and assert `resume --dry-run` blocks. Never alter the authoritative customer checkpoint. For full-disk tests use an approved bounded disposable filesystem/quota; never fill the host's real root filesystem. Unit fault injection already covers these decisions safely.

Record exact app fingerprint/commit, image IDs, OCA/library/dependency inventories, fixture SHA, commands, outputs, peak capacity and business assertions in the qualification record. Recheck upstream PR status before selecting any optional candidate. Export only the sanitized diagnostics ZIP for review. Do not recommend production use until all required real-server and business checks have executed and passed.
