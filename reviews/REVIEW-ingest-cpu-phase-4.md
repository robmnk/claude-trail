# Phase 4 review: Move blob content out of DuckDB

Verdict: changes-requested
Diff: main...phase-4-sqlite-blobs (bf62dfe), 4 files, +909/-83

## Blockers

none

## Major

- `tests/test_store.py:5046`: `f"   snapshot bytes  {len('mig v1\n') + len('mig v1\nmig v2\n')}"` puts backslashes inside an f-string expression. That is a SyntaxError before Python 3.12: `ast.parse` fails on 3.11.4, and `ruff check` reports 3 new `invalid-syntax` errors against the py310 target. `requires-python = ">=3.10"` and `.github/workflows/ci.yml` runs pytest on 3.10, so on 3.10 and 3.11 the whole `tests/test_store.py` module (193 tests) fails to collect. The 901 passes come from the 3.12 venv only. Fix: compute the number outside the f-string, e.g. `fh_bytes = len("mig v1\n") + len("mig v1\nmig v2\n")`, then `f"   snapshot bytes  {fh_bytes}"`.

## Minor

- `claude_trail_store.py:1002` (`_unpack_blob`), `:385` (`_sha`), `:3778` (`cmd_cat` print): content with a lone surrogate is stored but cannot be printed, and its sha is computed over different bytes than the ones stored. The probe `/tmp/dev-orchestrate/phase-4-sqlite-blobs/review/probe_surrogate.py` stores a Write of `"x\ud800y\n"` and a Write of `"x?y\n"` to another path. On main the whole ingest crashes on the first (DuckDB cannot cast the str); this phase fixes that. On the branch both ingest, but `cat` of the first exits 1 with a `UnicodeEncodeError` traceback. `cat` of the "?" file tracebacks too: `_sha` hashes `encode("utf-8", "replace")`, so both texts get one sha, the first stored wins, and the "?" file would come back with the other file's bytes. Fix: in `_put_blob`, hash the bytes `_pack_blob` stores (`hashlib.sha256(text.encode("utf-8", "surrogatepass"))`). That equals `_sha` for every text without a surrogate, so no existing sha changes. Have `cat` write `content.encode("utf-8", "surrogatepass")` (or `errors="replace"`) to `sys.stdout.buffer`, and extend `test_put_blob_needs_the_writer_and_keeps_a_lone_surrogate` to call `cmd_cat`. This is rare: the corpus has no such transcript, or main's ingest would already be failing.
- `claude_trail_store.py:185-201` (`connect`) with `:1150` (`_migrate_blobs` refusal): if the move raises (a row that still differs, a SQLite error), `connect()` fails, so every ingest stops, file-history capture included. The next run, 30 s later, repeats the copy pass and the full verify; both read all 729 MB from DuckDB, about 45 s CPU on the bench. The wrapper's `--foreground` path exits 0, so systemd never shows the failure, only the ingest log does. Refusing to drop the table is right. But a refusal that persists burns a core every 30 s, and once Claude Code evicts backups past a session's last 100 snapshots, the uncaptured file-history is gone. Fix: after a refusal, write a stamp (like `_fh_full_due`) and retry the move at most hourly; say in the error message and in CLAUDE.md what the operator should do. The trigger is unlikely, so this is not major.
- `claude_trail_store.py:3747-3751` (the `cmd_cat` candidate walk): no test covers the rule that replaces the old `JOIN blobs`, which skips a newer version whose blob is absent and takes the next older one. Adding `break` after `skipped += 1` (so only the newest candidate is used) still passes all 193 tests in `tests/test_store.py`. The only skip test, `test_readers_name_a_missing_blob_file_instead_of_raising`, has one candidate. Fix: add a test with two post_sha versions of one path where only the older blob is in the file. Assert the older content plus the roll-forward, and the `skipped 1` warning.

## Nits / follow-ups

- `claude_trail_store.py:3755`: the comment "the write order rules it out" is wrong. `_attach_blob_writer` (`:953`) recreates a missing blob file empty, which makes this warning reachable, and the missing-file test triggers exactly that. CLAUDE.md never mentions the writer's recreate-empty behaviour. Fix the comment, and add one sentence to the "Blob file" bullet saying that the warning is logged once and `cat` reports skipped versions from then on.
- `claude_trail_store.py:1107`, `:3701`: two mutations survive the suite. Removing `got[0] != size` from the verify passes, and so does dropping `* n` from the snapshot-bytes sum (no fixture has two file_history rows sharing a sha). Removing `_close_blobs` also passes. Fix: give a fixture a shared sha and add a size-only mismatch case to the verify test.
- `claude_trail_store.py:2216-2218`: the comment "(up to 22s once the blobs table is large)" is stale now that the table is gone. Make it historical or drop it.
- CLAUDE.md, migration bullet: if you roll back to a schema 2 build (reinstall main) after the move, its `SCHEMA_SQL` recreates an empty DuckDB `blobs` table and sets version '2'. From then on, main's `cat` and this branch's readers (which prefer the DuckDB table) miss every older version until a schema 3 writer migrates again. Nothing is lost, because the move copies the new rows and skips the rest. A rollback is the obvious step if phase 4 misbehaves, so add one sentence.

## Notes

- **Scope.** The diff covers every call site in the plan: `_put_blob`, the emit reads, stats, the snapshot-bytes join, `cat`, `reset-db` and `db_info`. `_table_counts` and `_store_size` also changed, which they had to. One side effect not in the plan: a lone surrogate no longer crashes the ingest. On main it does, on every run (confirmed by the probe).
- **No path found that loses blob content or leaves a reference without content.**
  - Write order: the only writers of sha columns are `_insert_file_versions` and `_insert_file_history`. `UPDATE file_history SET path` writes no sha. Both inserts call `_commit_blobs` first. Removing either call fails a test, and the order-check test has a control.
  - Weakref lifetime is correct. The finalizer closes the SQLite connection only after the DuckDB connection is collected. Any puts that roll back then are unreferenced by construction. In the `--detach` worker, `os._exit` skips finalizers, and `_close_blobs` in `cmd_ingest`'s finally covers that.
- **Crash points tested on bench copies** (now deleted; `baseline.duckdb` mtime unchanged):
  - SIGKILL at 20 s, mid-copy: the next run copied the other 5,314 rows and verified all of them in 46 s.
  - SIGKILL right after the "table dropped" log line, which lands inside the post-move compaction: the drop persisted and the DuckDB file kept its 3.3 GB until the next compaction. A 12 KB `compact-tmp` was left over; `compact_db` removes it next time. The third run was clean.
  - A store whose newest blob lived only in the DuckDB WAL migrated fine, and `cat` printed that blob.
  - Independent check (`/tmp/dev-orchestrate/phase-4-sqlite-blobs/review/probe_migrate_verify.py`): all 10,434 baseline rows are byte-identical in the blob file, every referenced sha is present, and no blob is unreferenced.
  - The verify really covers every row: rowids run 0 to max, and they are dense (0 to 10433) on the baseline.
- **Other paths.**
  - Lock-conflict skip: never touches SQLite.
  - `--compact`: leaves the blob file alone (a test pins this).
  - `reset-db`: deletes in a safe order. A stale `-wal` next to a fresh file was ignored by SQLite 3.50.4 (probe `probe_stale_wal.py`).
  - `prune-raw`: writes no shas.
- **Reader parity.** main reading the baseline was compared with the branch reading the migrated copy: 296 `cat` runs (latest and `--at`) plus 148 `diff --all-sources` runs over 148 paths, all byte-identical. Both copies first got the same file-history full pass. Without it, 3 `diff` outputs differ, because phase 3 re-chains rows on the first full pass; phase 4 has nothing to do with it.
- **Fit with the production wrapper.**
  - `claude-trail-ingest.service` has no memory limit (`MemoryMax=infinity`, and user.slice too) on a 64 GB box.
  - Under `nice -n 10 ionice -c 3 timeout 300`, the move took 69.6 s wall and 79.6 s CPU, with a 2.9 GB peak. The peak comes from the compaction (`compact_db` alone peaks at 2.85 GB).
  - The TUI shows "store busy" for about 70 s, once.
  - If the first run after deploy is the hourly `--compact` run, it compacts twice, which adds about 12 s CPU.
- **SQLite.**
  - `mode=ro` readers see the last commit while a write transaction is open (probe and test). They create a 0-byte `-wal` and a `-shm`, which the next writer close removes.
  - A one-shot writer close checkpoints and deletes the WAL. Idle readers hold no snapshot, so they do not pin the WAL. Under `--watch` the WAL is reused, so its size is bounded by the largest transaction.
  - With `synchronous=FULL`, an idle run commits nothing. Idle-run CPU is 0.2 s on both main and the branch.
- **Measured gains.**
  - CHECKPOINT after one 7.5 KB blob plus 100 raw_events rows: main writes 792 MB at 1.3 to 2.6 s CPU; the branch writes 22 MB at 0.15 s.
  - Compaction: main takes 16.9 s CPU with a 5.2 GB peak; the branch takes 12.4 s with a 2.85 GB peak. The plan's "a few seconds" for the hourly compaction is not met, because the rest is `raw_events` and `events`. Record that in the plan or in phase 4b.
- **Tests.** 901 pass on the 3.12 venv. Mutations caught: removing either `_commit_blobs` call, removing the content verify, removing the whole verify, an off-by-one in the rowid range, removing the reader fallback, removing the post-move compaction, re-copying with OR IGNORE, and removing the missing-file warning. Mutations not caught are listed above. The mutation runner and probes are in `/tmp/dev-orchestrate/phase-4-sqlite-blobs/review/` (`mutate.sh`, `probe_*.py`, `probe_*.sh`).
- **Docs.** CLAUDE.md and README are accurate, apart from the nits above. The two new CLAUDE.md bullets are long, but match how dense that file already is.
