# Ingest CPU phase 1 review: one state query per sweep instead of one per transcript

Verdict: clean
Diff: main...phase-1-sweep-state-dict (31d5535), 2 files, +143/-6

## Blockers

none

## Major

none

## Minor

- `claude_trail_store.py:718` (comment "Each path is visited once per sweep, so no entry goes stale mid-loop"): true for `_transcript_paths()` (its two globs are disjoint and sorted, so no duplicates), but `_sweep_transcripts` accepts any `paths` and does not enforce it. A path listed twice now re-reads from the stale preloaded offset; the inserts are idempotent, but `_upsert_session` adds `len(raw_rows)` to `sessions.event_count` a second time, and `changed`/`lines` are over-counted. Probe `dup_path_probe.py`: new code returns `(2, 2, 2)` with event_count 3 for a 2-line file; main returns `(2, 1, 1)` with event_count 2. No current caller passes duplicates (`cmd_ingest:1623` and `ingest_all` both use `_transcript_paths()`). Fix: state "`paths` must not repeat a path" in the `_sweep_transcripts` docstring, or dedupe with `paths = list(dict.fromkeys(paths))` before the loop.

## Nits / follow-ups

- `claude_trail_store.py:389-393`: the parameter `last_offset` is reassigned and then copied into `last`. Writing the lookup straight into `last` (`last = last_offset if last_offset is not None else <lookup>`) avoids reassigning the parameter. Cosmetic.
- Follow-up, not this phase: `ingest_state` is never pruned (3,625 rows for 1,003 transcripts on disk), and the sweep now loads every row on each run. Cost measured below is negligible even at 50k rows, so this matters only if a later phase wants the table tidy.

## Notes

- Scope: matches the plan's phase 1 (`plans/2026-10-19-ingest-cpu.md:77`). One bulk `SELECT transcript_path, last_offset FROM ingest_state` per sweep; each path gets `offsets.get(str(path), 0)`; an unchanged file returns after its `stat` with no query. The truncation rescan (`size < last`) and the new-file start at 0 are unchanged. `ingest_transcript(con, path)` with no offset keeps the old per-file lookup, so standalone callers keep their contract. No scope creep, no pyproject or CLAUDE.md change (CLAUDE.md never described the per-file lookup, so nothing in it went stale). The commit is authored as robmnk@gmail.com. The diff has no em or en dashes.
- Key form: the dict key and the stored key are both `str(path)` of the same `PROJECTS_DIR.glob(...)` result, and `ingest_transcript` is the only writer of `ingest_state` (`claude_trail_store.py:459`). Subagent paths (`*/*/subagents/*.jsonl`) take the same route, and the idle test covers one. Nothing resolves or relativises a path, so the key forms cannot diverge. A symlinked or relocated `PROJECTS_DIR` changes the key in old and new code alike.
- Staleness: the dict is local to `_sweep_transcripts` and is rebuilt on every `--watch` iteration (`cmd_ingest:1626` calls the sweep inside the loop). Within a sweep nothing else writes `ingest_state`: there is one writer connection, and the timer and the hook share the flock. The only stale case is a repeated path (Minor above).
- Callers: `ingest_transcript` is called only from `_sweep_transcripts` and from tests. `_sweep_transcripts` is called from `ingest_all` and from `cmd_ingest`. No module outside the store depends on the per-file lookup. `--file-history-only` skips the sweep and therefore the bulk query.
- Tests: the full suite passes, 870 tests in 26s, run with `.venv/bin/python -m pytest` and TMPDIR under `~/.cache`. The three new tests run against main's `claude_trail_store.py` (`run_new_tests_on_old_code.sh`): the idle-sweep test FAILS there with 5 queries against 1, so it proves "no per-file query". The truncation/new-file test and the standalone test pass on main, which is right, since they guard behaviour that must not change. Five mutants were run through `mutate.sh` against the new tests plus the existing truncation and incremental tests, and all were killed: a Path key instead of a str key, passing None, dropping the `size < last` reset, a new-file default of 1, and ignoring the passed offset. `_CountingCon` wraps `execute` and `executemany`, which are the only connection methods the store calls (no `con.sql`, `cursor` or `query`), so the count is complete.
- CPU: no regression. On a synthetic DuckDB (`bench_state_load.py`, project venv, pandas absent as on the timer), the bulk load costs 2.1 ms CPU for 3,625 rows and 26 ms for 50,000 rows. The old 1,008 parameterised lookups cost 640 ms CPU. The real store at /mnt/fast was not touched.
- Scratch, rerunnable: `/tmp/dev-orchestrate/phase-1-sweep-state-dict/review/` (`run_new_tests_on_old_code.sh`, `mutate.sh <name> <old> <new>`, `dup_path_probe.py <module-dir>`, `bench_state_load.py`).
