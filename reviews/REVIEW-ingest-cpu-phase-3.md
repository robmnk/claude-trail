# Phase 3 review: Make the file-history pass incremental

Verdict: needs-fix
Diff: main...phase-3-incremental-file-history (6fc407d), 3 files, +592/-110

## Blockers

none

## Major

1. `claude_trail_store.py:1791` (`full_fh = fh_only or compact or (watch and ...)`): a setup that never passes `--compact` never runs the full pass, so the safety net is gone there. README.md:195 recommends exactly such setups (`claude-trail ingest --detach` from a Stop hook, or cron), and CLAUDE.md:329 says "run it from cron". In them, a snapshot/delta line whose run stored it and advanced the offset but died before `ingest_file_history` (SIGTERM at shutdown, OOM, any exception later in the sweep or in the file-history pass), a backup that failed to read once, an orphan file, and every older path-less row are never processed again. On main the next run recovered all of them. The bytes are then lost when Claude Code deletes the backups: it evicts backups that fall out of a session's last 100 snapshots (`Qzo` in Claude Code 2.1.304, "failed to delete evicted backup") and removes old `file-history` dirs after `cleanupPeriodDays`. The `--watch` rule added here fixes the same gap for one setup only. Even under the wrapper the full pass inherits a skip: the wrapper touches `compact-stamp` before `claude-trail ingest --compact` connects, so a reader-lock skip also pushes the full pass out by another hour.
   Fix: make the full pass time-based for every run instead of flag-based. Add a `<store>.fh-full-stamp` checked like `_checkpoint_due`: take the full pass when the stamp is older than `FILE_HISTORY_FULL_INTERVAL`, and touch it only after the full pass succeeds. Keep `--compact` and `--file-history-only` as forced full passes, and let this replace `next_full_fh` for `--watch`. If you do not want that, at minimum state in README.md:195/233 and CLAUDE.md:329/363 that `--compact` or `--file-history-only` must run periodically. Also fix README.md:233 ("deferred resolution fills the path as more edits are seen"), which is now true only on a full pass.

## Minor

1. `claude_trail_store.py:1432` with `claude_trail_store.py:1287` (an emitted row is never rewritten): when the full pass recovers a version that an ordinary run missed, the later version of the same (session, file) has already been emitted with `pre_sha` NULL and no patch. It keeps that state for good, while a from-scratch full pass (and main, which recovered on the next run) chains it to the recovered version. Probe `probe_crash_chain.py` (scratch dir): v1 line stored by a dead run, v2 by an ordinary run, then `--file-history-only` gives `v2 pre_sha=NULL, patch=NULL` against `pre_sha=v1, patch` from `ingest_all`. Main gives the same result both ways. `cat` is unaffected (it reads `post_sha`); `diff --all-sources` shows a wrong `(no patch)` row. The same divergence follows when the full pass's orphan glob stores a file whose later version an ordinary run already emitted. This also makes "Both passes leave the same rows (pinned by a test)" (CLAUDE.md:363) and "Two modes, the same rows either way" (`claude_trail_store.py:1319`) overclaims.
   Fix: in the full pass, after inserting new rows, delete the already-emitted `fh:` rows of every (session_id, pathhash) group that gained a version below its highest emitted one, and let the emit walk re-insert them (they are derived from `file_history`). Or keep the behaviour and reword both texts to "the same rows unless a run died between storing a line and parsing it".
2. `tests/test_store.py:4037-4346`: four mutations of the new code pass the whole `tests/test_store.py` (172 passed; logs `mut_*_full.log`). Each is behaviour the review questions depend on:
   - (a) Dropping the pathhash check in `take` (`claude_trail_store.py:1151`) passes every test, so nothing pins "every candidate checked against its pathhash". Add a record whose `realParentDir` is the symlink-resolved parent (Claude Code's `N7` is `realpath(dirname)`, the logical path is what it hashes). Assert that route 2 rejects it and the row resolves to the logical path through route 1 or 3, or not at all.
   - (b) Disabling route 2 (`claude_trail_store.py:1159`) passes every test: every test delta with a relative key also has a session cwd, so route 3 resolves it. On the frozen bench transcripts route 2 is the route that resolves all 889 relative keys. Add a relative key in a session with no cwd but with `realParentDir`.
   - (c) Emitting only `inserted` (dropping `resolved_phs` at `claude_trail_store.py:1432`) passes every test. Add an ordinary run that resolves a row an earlier run stored path-less (for example a later delta line with `realParentDir`), and assert that its `fh:` row is emitted in that same run.
   - (d) Removing the `scan_for` gate (`claude_trail_store.py:1179`) passes every test. In `test_incremental_pass_defers_an_unresolvable_row_to_the_full_pass` (`tests/test_store.py:4222`) the second ordinary run carries no file-history line, so `len(scans) == 1` holds trivially. Append a new snapshot line that repeats the unresolved relative key (Claude Code repeats every tracked file in each snapshot), so the gate is what keeps the scan count at 1.

## Nits / follow-ups

1. `claude_trail_store.py:1173-1177`: route 4 takes `min(path)` from earlier rows without the pathhash check, while the docstring (`claude_trail_store.py:1131`) and CLAUDE.md:364 say every candidate is checked. Every stored path was hash-checked when it was written, so there is no data issue. Filter with `_sha(p)[:16] == ph`, or reword.
2. `claude_trail_store.py:1171`: route 3 uses `os.path.join(cwd, key)`, but Claude Code rebuilds the hashed path with Node's `path.join` (`h_e`), which normalizes. Its `eVt` relativizes any path that only starts with the cwd string, so `/home/x/proj-other/f` under `/home/x/proj` becomes `../proj-other/f`, and this candidate fails the hash check. Use `os.path.normpath(os.path.join(cwd, key))`. Route 2 covers these today, since the frozen bench transcripts needed route 3 zero times.

## Notes

- Scope: every bullet of the phase 3 spec is implemented (plan, `plans/2026-10-19-ingest-cpu.md:123`). Two items go beyond it:
  - The hourly full pass inside `--watch` is justified, since a `--watch` process holds the lock and can never get a `--compact`. Major 1 asks to generalize it.
  - Delta-only rows now take `backupTime` as `snapshot_ts`, a direct result of reading delta lines. It is safe. `_insert_file_history` is `DO NOTHING` and the emit skips existing `fh:` ids, so old rows are never rewritten or duplicated. Verified: a branch full pass over a main-built bench store changed 0 rows. The `cat --at` shift is at most 18 ms.
- Pathhash check, verified against the Claude Code 2.1.304 bundle: the backup name is `sha256(path)[:16]@vN` over the absolute path (`oqo`); `realParentDir` is `realpath(dirname(path))` (`N7`); relative keys are relative to the original cwd (`eVt` / `h_e`). `_sha` hashes the same UTF-8 bytes, so routes 1 to 3 and `_known_paths` accept only the exact string Claude Code hashed. Route 4 relies on the check made when the path was first stored (Nit 1). Claude Code writes the backup file before the line (`qxe` awaits `J2t`, then dispatches `track`, then `STs`; `Yxe` awaits every `J2t`, then writes the snapshot line), so the incremental pass does not race the file.
- Real-data equivalence: baseline copy plus a catch-up over the frozen bench transcripts, once with each code. Because `ingest_state` keys differ under the frozen HOME, all 1,022 transcripts were re-read. Results:
  - `blobs` (10,639) are identical.
  - `file_history` (8,261) and the `fh:` `file_versions` rows (8,261 against 8,258) are identical in path, sha, `pre_sha` and patch.
  - Two kinds of difference: 15 real delta-only rows' `snapshot_ts` (at most 18 ms, confirming the implementer's figure), and 3 synthetic rows in session `2b41d099` (`/home/naka/bench/turn-N.py`, lines the implementer appended to the frozen copy). The branch resolves those 3 through `realParentDir`; main leaves them NULL.
  - A branch full pass afterwards found nothing new.
- Idle CPU per run on those copies (driver without the rich import, 3 runs each): main 1.35 s, branch 0.19 s. That supports the CLAUDE.md claim of 0.75 s to 0.21 s.
- Data seen on disk, not caused by this phase: 17 PKs carry two `backupTime`s days apart (session `82c83dfc` reuses version numbers after a resume and overwrites `ph@vN` with new content), and 10 (session, file) groups list versions out of order (v2, v3, then v1). Both passes treat them the same way (first stored wins), but they are one more reason not to promise identical rows (Minor 1).
- Tests: the spec's three cases are covered:
  - `test_idle_ingest_reads_no_snapshot_payload` (`tests/test_store.py:4116`)
  - `test_incremental_pass_stores_and_resolves_a_new_snapshot_in_the_same_run` (`tests/test_store.py:4075`)
  - `test_full_pass_picks_up_an_orphan_and_a_crashed_runs_lines` (`tests/test_store.py:4174`)

  All 10 new or changed tests fail against main's store module: 3 through `_forbid`, the rest on behaviour. The crashed-run test fails on main only at "ordinary run sees none of it", so it pins the new deferral; main recovered on the next run. The full suite passes: 880 passed.
- The user's own wrapper setup is mostly fine: the worst-case delay is 1 h (2 h after a lock-skipped compaction), and loss needs the backup evicted within that time. Major 1 mainly affects the other documented setups.
- CLAUDE.md: the new text is in proportion (two sub-bullets plus edits) and accurate, apart from Major 1's cron and `--detach` gap, Minor 1's "same rows" claim and Nit 1.
- Scratch (rerunnable, each file has a header): `/tmp/dev-orchestrate/phase-3-incremental-file-history/review/`. It holds `probe_fh_lines.py`, `probe_crash_chain.py`, `mutate.sh`, `run_ingest.py`, `dump_fh.py` and their logs. The bench copies `/mnt/fast/claude-trail-bench/review-p3-inc.duckdb` and `review-p3-main.duckdb` can be deleted.
