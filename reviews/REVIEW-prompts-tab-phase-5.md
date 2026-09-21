# Phase 5 review: Store: populate `sessions.cwd` at ingest, prefer it for the Project column

Verdict: changes-requested
Diff: main...HEAD (`b53ba2b`), 4 files, +433/-50

## Blockers

none

## Major

- `claude_trail_store.py:706-708` and `claude_trail_store.py:1588-1594` - the backfill runs
  **after** the sweep, so the upgrade run can write a post-`cd` cwd and freeze it forever.
  On the first ingest after this lands, a session that is still being appended to has a row
  with `cwd IS NULL`; `_sweep_transcripts` ingests only the **tail chunk** from the stored
  byte offset, `ingest_transcript` takes the first cwd *in that chunk* (`claude_trail_store.py:426`),
  and `coalesce(sessions.cwd, excluded.cwd)` then makes that value permanent. The backfill
  never sees the row (it is no longer NULL), and nothing ever rewrites it. The invariant the
  comment at `claude_trail_store.py:495` states ("the FIRST cwd seen on a main transcript
  (the launch dir)") is therefore not what the code implements.
  Reproduced (`/tmp/ct-review/probe_order.py`): pre-phase-5 store shape (`UPDATE sessions SET
  cwd=NULL`, offsets current), append one post-`cd` line, `ingest_all` -> `cwd` becomes
  `/tmp/scratchpad`, not the launch dir. Exposure on this machine is small but real: 1-2 main
  transcripts are being appended to at any moment (10 in the last hour), and 12 of the newest
  40 carry more than one cwd.
  Fix: call `backfill_session_cwd` **before** `_sweep_transcripts` at both sites (the head read
  wins, then coalesce protects it), or pass the cwd only for a full read
  (`_upsert_session(con, path, raw_rows, cwd if last == 0 else None)`). I applied the reorder in
  a scratch copy: the probe flips to OK and all 750 tests still pass - which is the second half
  of this finding, **no test pins the ordering**. Add one: ingest, NULL the column, append a
  post-`cd` line, ingest again, assert the launch dir.

## Minor

- `claude_trail.py:484-486`, `claude_trail.py:3047-3049`, `claude_trail_store.py:2124-2126` -
  all three say `cwd` is NULL **only** for a session whose transcript has left the disk. Not
  true: measured over the 127 main transcripts on disk, 10 carry no top-level cwd on any line
  (118-267 byte stubs), so their sessions stay NULL with the transcript present. Drop "only",
  or name the second case.
- `claude_trail_store.py:650-652` - "once no row has a NULL cwd it does one scan of the small
  `sessions` table and returns, reading no files" describes a state the real store never
  reaches: 1,586 of 1,703 rows can never be filled (transcript gone), so the early return is
  dead there and every sweep runs the loop. Harmless in cost - measured on a copy of the live
  store: first run 117 rows in 0.142s, then 2.8-3.0ms per call re-reading 2.2 KB of stub heads -
  but write the measured steady state instead of the unreachable one.
- `claude_trail_store.py:38-39` - `cwd` is the only column in `sessions` with no DDL comment,
  and `project`'s comment ("decoded from transcript dir / event cwd") now actively misleads:
  it is never the event cwd. Fix both lines while the column is fresh.
- `claude_trail_store.py:1767` - `search --project P` still matches the lossy
  `sessions.project` only. After this phase the TUI shows `claude-trail` in the Project column
  while `--project claude-trail` matches nothing (the stored value is `claude/trail`). Either
  widen to `(project LIKE ? OR cwd LIKE ?)` or say so in the phase 7 docs; not phase 5 scope,
  but it is this phase that makes the mismatch visible.
- Test gaps found by mutation (see Notes for the full table): dropping
  `or bool(cwd_filled)` from `stored_anything` (`claude_trail_store.py:1605`), dropping the
  `backfill_session_cwd(con)` call from `ingest_all` (`claude_trail_store.py:708`), and
  dropping the `path.stem not in pending` pre-filter (`claude_trail_store.py:667`) all leave
  750 tests passing. The first two are one assertion each (`assert "checkpoint: skipped" not in
  out` in the existing `cmd_ingest` test; an `ingest_all`-only backfill test); the pre-filter
  needs a `_head_cwd` call counter.

## Nits / follow-ups

- `claude_trail_store.py:604-606` - "the first cwd-bearing line is line 2..21". Measured across
  all 127 transcripts today: 3..22. The 50-line bound is unaffected; just update the number if
  you touch the comment.
- `claude_trail_store.py:670` (`sid = sid or path.stem`) and `:673` (`AND cwd IS NULL`) are both
  unreachable by construction (the pre-filter already matched the stem; `pending` is built from
  NULL rows). Fine as defense, but both mutations survive the suite, so nothing states the
  intent.
- `claude_trail_store.py:675` - `filled += 1` counts intent, not `rowcount`. With the single
  writer it cannot diverge; only worth a word if the log line ever becomes load-bearing.
- `plans/2026-07-29-prompts-tab.md:247` - the plan's own Columns table still reads
  "NOT `cwd`: `_upsert_session` never writes that column". Phase 7 reconciles the docs; the
  plan needs the same correction.

## Notes

**Scope.** Exactly the phase 5 bullets. No CLI subcommand (phase 6), no CLAUDE.md / README /
`.env.example` change (phase 7), no version bump, no unrelated refactor. The one extraction,
`_main_transcript_paths()` (`claude_trail_store.py:590`), exists because the backfill needs it.
The `build_turn_panel` header change (`claude_trail.py:2745-2749`) is the deliberate overreach
and it is justified: leaving the header on `turn.project` would make the list and the page it
opens disagree about the same session, for 3 lines and 1 test. Keep it.

**Ingest rules.** Re-ingest stays idempotent (`size == last` returns before the upsert; the
backfill returns 0 with nothing NULL). Single writer unchanged. `--file-history-only` skips the
backfill and cannot print the new log line (`cwd_filled` stays 0). Lock-collision handling
untouched. The `stored_anything` fold is right in principle (the backfill is an UPDATE, so no
table count moves), though on a real store it only changes the outcome when the last flush was
over `CHECKPOINT_MIN_INTERVAL` ago - and a skipped flush is documented as safe either way.
Subagent transcripts cannot seed the parent (`claude_trail_store.py:426`), and
`_transcript_paths()` orders main files before subagents, so ingest order is not a hazard.

**TUI purity.** After `import claude_trail` and after `hook_main()`, neither `duckdb` nor
`claude_trail_store` is in `sys.modules` (checked directly). `_turn_project_path` is pure.

**Markup and truncation.** Rendered a turn with `cwd="/home/naka/[wip]/a[/]b"`: the column cell
is a `Text` whose plain is `…/[wip]/a[/]b`, and the turn-page header line reads
`Session  4be57694  …/[wip]/a[/]b  ·  sent 18:58:55`. Nothing is eaten. `short_path` left-truncates
a long single component to `…ingle-component` at `PROJECT_LABEL_WIDTH` 16.

**Measured on a copy of the live store** (`cp` first; the live file at
`/mnt/fast/claude-trail/trail.duckdb` was never opened read-write): 1,703 sessions, all 1,703
with `cwd IS NULL` today. The backfill fills 117 in 0.142s, then 0 in ~2.8ms per sweep.
Worth telling the user before the docs phase: of the 117 filled rows, **zero** change what the
Project column renders - none of the 127 transcripts currently on disk lives under a path with a
`-` in it, and the sessions the plan cites (`claude-trail`, `personal-fin`, `keypad-macros`) have
lost their transcripts and stay NULL forever. The newest 200 turns resolve to 3 project dirs,
all of which already decoded correctly. The win here is forward-looking, not retroactive; the
plan's "3 of the 6 projects" measurement no longer reproduces.

**Tests.** `.venv/bin/python -m pytest tests/ -q` -> 750 passed in 13.6s (baseline on `main`
is 740). No test function was deleted without a successor: the two renamed away
(`test_cwd_is_always_none_project_is_the_populated_one`,
`test_project_column_renders_project_and_never_cwd`) keep their `cwd is None` / fallback
assertions inside the new pair, which is the "updated deliberately" the phase asked for.
Ruff (`uvx --from 'ruff>=0.15,<0.16' ruff check`) reports 18 findings on `main` and 18 on HEAD:
no new lint.

Mutation results on a scratch copy of the repo (`/tmp/ct-review/repo`, full suite per mutation):

| Mutation | Result |
|---|---|
| `coalesce` dropped (latest wins) | caught (1) |
| subagent guard dropped (`agent_id is None`) | caught (1) |
| 50-line head bound dropped | caught (1) |
| TUI `project` fallback dropped | caught (4) |
| preference flipped (`project` first) | caught (3) |
| `Text()` -> plain markup string | caught (5) |
| turn-page header back on `turn.project` | caught (1) |
| backfill dropped from `cmd_ingest` | caught (1) |
| backfill dropped from `ingest_all` | **survived** |
| `or bool(cwd_filled)` dropped | **survived** |
| backfill name pre-filter dropped | **survived** |
| `sid = sid or path.stem` -> `path.stem` | **survived** |
| `AND cwd IS NULL` dropped from the UPDATE | **survived** |
| backfill moved before the sweep (the Major's fix) | **survived** (nothing pins the order) |

**Docs debt for phase 7.** The implementer's four items stand (`sessions.cwd` semantics, the
"nothing to run" upgrade path, the transcript-gone limit, the Columns table wording, the
`sessions: N cwd backfilled` log line). Add:

1. `cwd` is the **launch** dir and never follows a `cd` - a user reading the column will
   otherwise assume it tracks the session's current directory.
2. The NULL case is two cases, not one: transcript gone **and** transcript present with no cwd
   in its first 50 lines (10 of 127 here).
3. `--file-history-only` does not run the backfill.
4. The honest upgrade expectation, measured: 117 of 1,703 rows fill on the first ingest and no
   rendered project changes today; the rest keep the `project` fallback.
5. The `search --project` mismatch (Minor above), whichever way it is resolved.
6. `plans/2026-07-29-prompts-tab.md:247` contradicts the shipped behavior and needs the same
   edit as CLAUDE.md.
