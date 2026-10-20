# Ingest CPU, part 2: the fixed cost of every run and the 5-minute flush

The 30s ingest timer (plus the Stop hook, about 128 runs an hour) is still one of the
biggest CPU users on the box. Part 1, the shutdown-checkpoint fix, halved it. This plan
covers what is left.

Scope: `claude_trail_store.py` (ingest path, blob storage) + tests + CLAUDE.md. The TUI is
untouched, apart from the store's blob reads that `cat` and `stats` use.

## Where it stands (measured 2026-10-19)

| | CPU per run | Share of one core |
|---|---|---|
| Before part 1 (09:00 to 09:54, 93 runs) | 5.5 s | about 16% |
| After part 1 (12 runs) | 2.6 s | about 9% |

Part 1 is `PRAGMA disable_checkpoint_on_shutdown` on a skipped flush, in the skipped-flush
branch of `cmd_ingest` (`claude_trail_store.py:1670`). Without it DuckDB checkpointed on
`con.close()`, so the 300s throttle saved nothing. It is not committed yet.

What still costs:

- **Every run:** 1.8 to 2.3 s CPU, even when the run stores almost nothing.
- **The flush every 5 minutes:** 4.7 s CPU, 16 s wall and a 1.1 GB peak.
- **The hourly `--compact`:** 18 to 20 s CPU and a 6 GB peak.

Store size: 3.1 GB in total.

| Table or input | Size |
|---|---|
| `raw_events` | 658k rows |
| `events` | 336k rows |
| `blobs` | 10,421 rows, 762 MB of file contents |
| `file_history` | 8,046 rows, 0 unresolved |
| `ingest_state` | 3,625 rows |
| Transcripts on disk | 1,003 |
| `~/.claude/file-history` | 3,894 files |

### Where an ordinary run's CPU goes

From cProfile of one run at 15:48. The run took 3.3 s under the profiler and is about 2 s
without it.

| Cost | Profiled | Where |
|---|---|---|
| One `SELECT last_offset` per transcript: 1,008 queries per run, even for unchanged files | 0.77 s | `ingest_transcript`, `claude_trail_store.py:385` |
| DuckDB tries `import pandas` (not installed) twice per parameterised `execute` and per `executemany` row: 4,233 failed imports, each a filesystem search | 0.64 s | measured: blocking it cut 1,000 parameterised queries from 417 to 290 ms |
| The file-history driver re-reads all 6,609 snapshot payloads (32 MB) and re-parses 110k records every run | 0.71 s | `_snapshot_records`, `claude_trail_store.py:1019`, called from `ingest_file_history`, `claude_trail_store.py:1172` |
| Reverse path index rebuilt whenever one snapshot is new: two `DISTINCT json_extract_string` scans over `events` | 0.23 s | `_known_paths`, `claude_trail_store.py:1047` |
| Orphan glob over every file-history file | small | `claude_trail_store.py:1231` |
| Interpreter start, imports (`claude_trail.py` pulls in rich: 84 ms; duckdb: 72 ms) and connect (0.1 s) | about 0.3 s | entry point `claude_trail:main` |

The pandas probes overlap with the per-file lookups, since each lookup is a parameterised
`execute`.

### Why the flush and the compaction are expensive (to confirm in phase 0)

The background agent's inference, not measured directly: a CHECKPOINT rewrites the whole
row group that a new row lands in. All 10,421 `blobs` rows (762 MB) sit in one row group, so
one new blob means rewriting all of it.

Evidence:
- Before part 1, close took a median of 13 s on runs that inserted a blob, against about 1 s
  on runs that did not.
- The file grows from 1.9 to 2.9 GB within minutes after each compaction.

## Phases

- [x] 0. Measure (no code)
  - Results (2026-10-19, DuckDB 1.5.6, bench copy kept at
    `/mnt/fast/claude-trail-bench/baseline.duckdb`):
    - All 10,434 `blobs` rows sit in 1 row group (733 MiB, uncompressed). On the bench copy,
      CHECKPOINT after one 7.5 KB blob writes 769 MB, against 38 MB after 100 `raw_events`
      rows: 2.32 against 0.42 s CPU. A plain `con.close()` costs the same as the explicit
      CHECKPOINT. In production, the 38 flushes whose window held a new blob took a median
      12 s, and the 17 without one took 1 s. Phase 4 gate: passed.
    - Blobs are not the only rewrite: an ordinary run's `events` append rewrites that table's
      last row group (90k rows, about 237 MB). Phase 4 alone cuts a typical flush from 1,072
      to 303 MB written and from 5.2 to 1.8 s CPU, not to near zero.
    - `ATTACH ... (ROW_GROUP_SIZE 2048)` brought a typical flush to 0.60 s CPU and 81 MB
      written, but the setting is not saved in the file, so every writer open must pass it.
    - Over the last 7 days, 553 of 659 new `file_history` rows (84%) resolve from their own
      absolute key. The other 106 need no `_known_paths` scan either: 54 through
      `realParentDir`, 50 through `sessions.cwd` plus the relative key, 2 through an earlier
      row with the same pathhash. 0 stay unresolved. Phase 3 gate: passed.
    - `file-history-delta` lines carry their own `realParentDir`, but the store does not keep
      their payload; they enter only through the orphan glob. If phase 3 moves the glob to
      the hourly pass without reading these lines, about 8% of new snapshots wait up to 1 h.
    - Journal baseline, 09:55 to 10:55 after the part 1 deploy: 113 runs, 2.83 s CPU per run
      on average (median 2.02), 9.0% of one core. 104 skipped-flush runs averaged 2.03 s, 8
      flushes 8.92 s, and the one compaction 36.8 s at a 6.45 GB peak.
    - Open: production blob flushes take 12 s wall against 1.2 to 2.5 s on the bench with
      the same CPU, so the extra time is waiting. CPU contention was not tested.
  - Copy the store to `/mnt/fast/claude-trail-bench/` (342 GB free). On the copy, time a
    `CHECKPOINT` after inserting (a) one blob, (b) 100 `raw_events` rows with no blob, and
    (c) both. Record wall time, CPU and the file growth.
  - Count how many new snapshots resolve from their own `trackedFileBackups` key, with no
    `_known_paths` scan. This decides whether phase 3 can skip the scan in the common case.
  - Record one hour of `Consumed` lines from the journal as the baseline (see Verify).
  - Gate: phase 4 goes ahead only if (a) costs far more than (b).
- [x] 1. One state query per sweep instead of one per transcript
  - `_sweep_transcripts` (`claude_trail_store.py:701`) loads `ingest_state` once into a
    `{path: offset}` dict and passes each transcript its offset. A transcript whose size
    equals its offset is skipped after its `stat`, with no query.
  - Keep the existing edge cases: a truncated file (size below the offset) rescans from 0,
    and a new file starts at 0.
  - Tests:
    - An idle sweep over N unchanged transcripts runs no per-file query (count calls through
      a wrapping connection).
    - Truncation and new-file behaviour are unchanged.
  - Expected saving: about 0.4 s per run.
- [x] 2. Stop DuckDB's pandas probe in the ingest process
  - Before the first `import duckdb` in `cmd_ingest`: if
    `importlib.util.find_spec("pandas")` is None, set `sys.modules["pandas"] = None`. A
    blocked import then fails at once instead of searching `sys.path`. If pandas is
    installed, leave it alone.
  - Tests:
    - With pandas absent, the entry is set and an ingest round-trips.
    - With a fake pandas importable, nothing is blocked.
  - Expected saving: 0.1 to 0.3 s per run. It grows with the rows inserted, since
    `executemany` probes once per row.
  - Optional: report it upstream, since DuckDB does not cache a failed optional import.
- [x] 3. Make the file-history pass incremental
  - `ingest_transcript` already sees every `file-history-snapshot` line it inserts. Collect
    those payloads in the sweep and hand them to `ingest_file_history`, which parses only
    them and no longer calls `_snapshot_records` on every run.
  - Collect the `file-history-delta` lines the same way (decided 2026-10-19). They carry
    their own `realParentDir`, and today they enter only through the orphan glob, so without
    this about 8% of new snapshots would wait up to an hour for the full pass.
  - Resolve new rows from their own absolute key first, then through `realParentDir`,
    `sessions.cwd` plus the relative key, or an earlier row with the same pathhash. Phase 0
    found these cover all 659 new rows of the last 7 days. Run the full `_known_paths` scan
    only when a row is still unresolved after that.
  - The full pass moves to the runs that can afford it: `--compact` (hourly) and
    `--file-history-only`. The full pass is every snapshot payload plus the orphan glob at
    `claude_trail_store.py:1231`. It also catches anything the incremental path missed, for
    example after a crash between the insert and the parse.
  - Tests:
    - An idle sweep reads no snapshot payload.
    - A new snapshot is stored and resolved in the same run.
    - The full pass still picks up an orphan file and a snapshot inserted by an earlier
      crashed run.
  - Expected saving: 0.4 to 0.6 s per run.
- [x] 4. Move blob content out of DuckDB (only if phase 0 confirms the cause)
  - Store content in SQLite (stdlib `sqlite3`) at `<store>.blobs.sqlite`: table
    `blobs(sha TEXT PRIMARY KEY, size INTEGER, content BLOB)`, zlib-compressed, WAL mode.
    An insert costs the size of that blob, and nothing gets rewritten at checkpoint.
  - DuckDB keeps only the `sha` references it already has. Migration runs on the first
    writer connect at `SCHEMA_VERSION` 3 (`claude_trail_store.py:18`): copy the rows, drop
    the DuckDB `blobs` table, then compact.
  - Call sites to change:
    - `_put_blob` (`claude_trail_store.py:800`)
    - the two content reads in `_emit_file_history_versions`
      (`claude_trail_store.py:1153`)
    - `stats` blob counts (`claude_trail_store.py:3081`)
    - the coverage join (`claude_trail_store.py:3116`)
    - the `cat` join (`claude_trail_store.py:3149`)
    - `reset-db` (`claude_trail_store.py:1680`), which must delete the new file too
    - `db_info` (`claude_trail_store.py:3003`), which shows its size
  - Rejected alternatives:
    - A second DuckDB file through ATTACH has the same row-group rewrite.
    - One file per blob means 10k+ files and no transaction with the index.
  - Tests: migration from a schema 2 store, `cat` and `diff` byte-identical before and
    after, and an idempotent re-ingest. 39 tests in `tests/test_store.py` mention blobs.
  - Expected:
    - The 5-minute flush drops from 5.2 to about 1.8 s CPU and from 1,072 to 303 MB
      written (phase 0). The rest is the `events` row-group rewrite, which phase 4b targets.
    - The hourly compaction drops from 18 to 20 s CPU and a 6 GB peak to a few seconds.
    - The store shrinks from 3.1 GB to about 2 GB, plus a blob file of about 0.2 GB.
- [x] 4b. Smaller row groups on the writer (decided 2026-10-19, runs after phase 4)
  - Phase 0: an ordinary flush also rewrites the last row group of `events` (90k rows, about
    237 MB). `ATTACH ... (ROW_GROUP_SIZE 2048)` cut a typical flush on the bench copy to
    0.60 s CPU and 81 MB written. The setting is not stored in the file, so every writer
    open (`connect_writer`, `compact_db`) must pass it.
  - Pick the size by measurement on the bench copy (2048 and one or two larger values):
    flush CPU and bytes written, plus the read latency of `prompt_turns`, `turn_actions`,
    `search`, `tokens` and `stats` against today's layout. Keep the change only if no read
    gets more than 20% slower; otherwise record the numbers here and drop the phase.
  - Check what `--compact` does to the layout, and that read-only opens need nothing new.
  - Tests: every writer open passes the setting, and a flush after one appended row rewrites
    only the small last group (by bytes written or `pragma_storage_info`).
  - Results (2026-10-20, bench copies of the store just after the phase 4 migration, layouts
    after 3 to 5 hourly-style compactions at each size):

    | Size | Flush CPU | Flush written | Compaction CPU | Compaction peak | Worst read |
    |---|---|---|---|---|---|
    | default 122,880 | 2.98 s | 378 MB | 13.8 s | 2.9 GB | baseline |
    | 2048 | 0.52 s | 59 MB | 21.8 s | 2.7 GB | `turn_actions` +43%, `stats` +26% |
    | 8192 (chosen) | 0.58 s | 87 MB | 14.7 s | 2.04 GB | `search` +12% |
    | 16384 | 0.91 s | 105 MB | 15.4 to 18.0 s | 2.1 GB | `search` +11% |

    - 2048 looked fine after one compaction and failed from the second on, as its small
      segments spread over many more blocks.
    - At 8192, `prompt_turns` is 75% faster (314 to 78 ms). The review measured reads the
      bench missed: `prompt_for_command` on subagent rows +17 to +20% (about 4 ms),
      `session_token_totals` +12%, `command_tokens_for` +10%, `stats` up to +15%; the whole
      command modal +12 to +15%, so within the 20% rule.
    - A plain read-write open that writes anything merges every table back to 122,880-row
      groups (about one full rewrite); the next `--compact` splits them again.
- [ ] 5. Exit early when nothing changed (optional)
  - Before importing duckdb, scan `~/.claude/projects` with `os.scandir` (about 1k files,
    a few ms) and compare the newest transcript mtime with a run stamp. If nothing is newer
    and no flush is due (stamp age plus WAL presence, the rules of `_checkpoint_due`), log
    one line and exit. This saves about 0.3 s on idle runs, which is most of the night.
  - Dispatch the store subcommands before `claude_trail.py` imports rich, or point the
    wrapper at the store module directly. This saves about 80 ms per run.

Expected end state: about 0.5 s CPU per ordinary run and about 1 s per flush, so the ingest
falls from about 9% of a core to about 2%.

## Verify

Tests run in the project venv, never through `uv run`. On 2026-10-19 `uv run` replaced
`.venv` with one that lacked the dev extras. The venv was rebuilt the same day with
`uv venv .venv --python 3.12` and `uv pip install --python .venv/bin/python -e '.[dev]'`.

```bash
cd /home/naka/Projects/personal/claude-trail
TMPDIR=$HOME/.cache/pytest-tmp .venv/bin/python -m pytest tests/ -q
```

Deploy: the timer runs the uv tool, not the clone. Run
`uv tool install --force --reinstall ~/Projects/personal/claude-trail` after each phase.

Measure per phase: average CPU per run from the journal, over at least 30 minutes after the
deploy.

```bash
journalctl --user -u claude-trail-ingest.service --since "-30min" --no-pager \
  | grep -oP 'Consumed \K[0-9.]+(ms|s)(?= CPU)' \
  | awk '{v=$1; if (v ~ /ms$/) {sub(/ms/,"",v); v/=1000} else {sub(/s/,"",v)}; n++; s+=v}
         END {printf "runs=%d avg=%.2fs cpu/run\n", n, s/n}'
```

Profile one run under the same lock the timer uses. `CLAUDE_TRAIL_DB` must point at
`/mnt/fast/claude-trail/trail.duckdb`, or the clone writes a second store under `~/.claude`.

```bash
CLAUDE_TRAIL_DB=/mnt/fast/claude-trail/trail.duckdb flock -w 60 /mnt/fast/claude-trail/trail-ingest.lock \
  .venv/bin/python -m cProfile -o /tmp/ingest.prof claude_trail.py ingest
```

## Conventions for this run

- Review docs: `reviews/REVIEW-ingest-cpu-phase-N.md`.
- Do not bump the version in `pyproject.toml`.
- Plan edits go in plan-only commits.
- Part 1 is committed on its own before phase 1 starts.

## Open decisions

1. Decided 2026-10-19: blobs go to SQLite (phase 4), then smaller row groups (phase 4b).
   Phase 3 also reads `file-history-delta` lines.
2. Whether phase 5's early exit is worth a second code path, once phases 1 to 3 have
   brought an idle run down to about 0.5 s.
3. Keep the 30s cadence. Phases 1 to 3 make a run cheap enough that a slower timer would
   only cost freshness.
