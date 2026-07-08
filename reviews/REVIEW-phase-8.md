# Phase 8 review: File-history backfill

Verdict: changes-requested
Diff: main...phase-8-file-history (f35031f), 5 files, +782/-20

## Blockers

none

## Major

1. `claude_trail_store.py:1170-1171` - `cmd_diff`'s default filter references the `source` column unguarded, so `diff <path>` on a not-yet-migrated 0.6.0 DB opened read-only raises `BinderException: Referenced column "source" not found` (reproduced against a schema-1 fixture DB). This is the exact reader-must-not-raise scenario the plan guards `stats` against (Step 8.7 / Risks "Read-only migration"); readers run no DDL, so the crash persists until the user runs a writable `ingest`. Fix: apply the same `information_schema.columns` check `cmd_stats` uses (claude_trail_store.py:1092) and append the `source IS DISTINCT FROM 'file-history'` clause only when the column exists; a pre-migration DB has only transcript rows, so skipping the filter is semantically identical.
2. `claude_trail_store.py:709,844` - a non-string `trackedFileBackups.backupTime` (or a malformed ISO string) is passed straight into the `snapshot_ts TIMESTAMPTZ` bind and raises `ConversionException` inside `_insert_file_history` (reproduced: `{"weird": "dict"}` aborts `ingest_all`). This violates the stated contracts ("Never raises on a malformed line", `_snapshot_records`; "Never raises on a bad file", `ingest_file_history`), and because the bad entry lives permanently in `raw_events` and `_snapshot_records` re-reads all snapshot events every sweep, one malformed entry wedges every future `ingest` run with no self-heal. Every sibling field gets an isinstance guard (`meta`, `bfn`, `tfb`); `backupTime` is the one input that skipped it. Fix: in `_snapshot_records` (or at `_fh_row` build), keep `btime` only when `isinstance(btime, str)` and it parses (e.g. `datetime.fromisoformat(btime.replace("Z", "+00:00"))` in try/except, binding the datetime or None); NULL `snapshot_ts` already flows through `epoch_ms`/`cat` safely.

## Minor

1. `claude_trail_store.py:873-874` - the promotion trigger counts `path IS NULL AND sha IS NOT NULL`, so binary-skipped rows (`sha IS NULL`) never have their `path` promoted once all text rows are resolved and no new snapshots arrive; `stats` then reports them "unresolved" forever even after the path becomes known. Fix: count `path IS NULL` without the sha condition (the emit query still filters `sha IS NOT NULL`, so nothing else changes).
2. `claude_trail_store.py:822-823` - `ingest_file_history` returns early when `FILE_HISTORY_DIR` is missing, which also skips the promotion/emit pass for already-stored unresolved rows. If the directory is pruned (the exact loss scenario the phase insures against), deferred resolution of the preserved snapshots stops working. Fix: gate only the disk-reading passes on the dir's existence; run promotion + emit regardless.
3. `tests/test_store.py` - no test opens a schema-1 DB read-only and exercises the reader guards (`cmd_stats`, `cmd_diff`); such a test would have caught Major 1. Add one: build the schema-1 fixture from `test_file_history_schema1_migration`, open via `connect(read_only=True)` semantics (call `cmd_stats(["stats"])` and `cmd_diff(["diff", path])`), assert no raise.

## Nits / follow-ups

- `claude_trail_store.py:831-833` - `records.setdefault` first-seen-wins over an unordered `raw_events` scan is nondeterministic if duplicate entries ever disagree; benign today (duplicates carry identical `backupTime`), worth a comment or `ORDER BY ts`.
- `claude_trail_store.py:893` - the progress line's "M resolved" is actually the count of newly emitted `file_versions` rows, not newly resolved paths; consider "M emitted" or renaming the variable.
- Untested paths: the on-disk fallback pass (orphaned dir, mtime timestamp, lines 847-866) and the `--file-history-only` CLI flag. Both are small; a test each would pin them.
- `cmd_diff` requires the path as `args[0]`, so `diff --all-sources <path>` misparses; consistent with the existing subcommand style (`cat`, `search`), so noted only.
- The gate env has `pytz` installed, so the suite never exercises the no-pytz read-back; the `epoch_ms` int + stdlib tz-aware datetime rebox is sound regardless (same dodge as `cmd_diff`/`cmd_tokens`).

## Notes

- Scope match: complete. Steps 8.1-8.9 all delivered: investigation comment block with re-measured numbers and two honest adaptations (no top-level `sessionId` on snapshot events; epoch_ms read-back), schema + migration + B2 plumbing, trackedFileBackups-primary resolution with reverse-index fallback, ingest pass with PK skip set, M6 ordering, `--file-history-only`, M5 diff behavior, guarded stats block, 11 hermetic tests, docs, 0.7.0 bump. No scope creep found.
- Focus-area verification (all confirmed in code, not the summary): B2 - `source` is in `_FV_COLS`, stamped in both builders, and the stage insert lists columns explicitly, so no fresh row can land `source IS NULL`; B1 - emit orders by `(session_id, pathhash, version)` and chains `prev[(sid, ph)]`, first present version gets `pre_sha NULL`/no patch (gap test covers v2+v4); M3 - snake_case `$.file_path`/`$.notebook_path` over `events.tool_input`, null `backupFileName` skipped, relative keys absolutized via the reverse index, plus a sound extra: self-describing absolute keys are hash-verified before augmenting the index; `_fh_session_id` derivation (stem, subagent-aware) is correct for the layout; M2 - ALTER + CREATE + `ON CONFLICT DO UPDATE` seed in the writable path only, migration test asserts backfill and version 2; M4 - the PK set is checked before any byte read, and a steady sweep with 0 unresolved rows skips index/promotion/emit entirely (verified on real data: second sweep printed no file-history line); M6 - pass ordering as specified.
- Focus 10 (done-skip assumption): assessed safe for realistic orderings. Promotion resolves all rows of a pathhash at once and emit covers every resolved-but-unemitted row, so no resolved row can be permanently skipped. The residual case is a LOWER version arriving after a higher one was emitted (transient `OSError` on read, or a pruned file restored); the higher version then keeps `pre_sha NULL` forever. `cat` correctness is unaffected (post_sha right), only `diff --all-sources` loses one delta; acceptable, listed as a known limitation rather than a finding.
- Real-data verify reproduced against a throwaway scratch DB (never the real store): 1557 snapshots ingested, 1557 resolved, 0 unresolved, 96.3MB preserved, 680/682 edited paths cat-able (622 via transcript, 494 via file-history); re-ingest added nothing (idempotent).
- Gate: 340 passed. No em dash / en dash characters anywhere in the diff.
- What's strong: the investigation block documents where reality diverged from the plan instead of papering over it; the test fixtures mirror the real event shape (no top-level sessionId) rather than a convenient one; blob-first preservation with deferred resolution is implemented exactly as the loss-insurance framing demands; and the B1 chaining logic (advance `prev` even for done-skipped rows) is subtle and correct.
