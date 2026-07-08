# Phase 5 review: File versions + content-addressed blobs

Verdict: changes-requested
Diff: main...phase-5-file-versions (5ac6984), 2 files, +314/-1

## Blockers

none

## Major

1. `claude_trail_store.py:443` and `claude_trail_store.py:461-467` - the Step 5.1 findings
   comment is factually wrong against the live corpus, and the file-history skip rests on two
   false premises.
   - "originalFile: full PRE content (str)" for Edit: in the 25 largest transcripts, 830 of
     1118 Edit results have `originalFile: null` (also most Write "update"s: 264 of 274 lack
     it). The curated store confirms the impact: 233 file_versions rows, only 47 with pre_sha
     and 91 with post_sha, so ~60% of rows are patch-only with no reconstructable content.
   - "That mapping is unsolvable without reversing the hash": verified false. The
     `~/.claude/file-history/<uuid>` dir name IS the session_id (76/76 dirs match transcript
     session ids) and `<pathhash>` = `sha256(abs_path)[:16]` (forward-hash a known edited
     path and it matches; confirmed on real sessions). "Redundant with toolUseResult" is
     therefore also false: file-history is exactly the source that fills the null-originalFile
     majority.
   - The code itself handles null pre correctly (row kept, null shas, structuredPatch stored),
     so this is not a crash bug, but the phase's stated purpose ("reconstruction needs full
     content per edit") is delivered at ~39% post coverage while the in-code record claims
     full coverage. Fix now: rewrite the comment block with the real null rates and the real
     file-history mapping (session_id dir, sha256(path)[:16] stems, @vN order), and state the
     coverage limitation explicitly. Whether to ingest file-history snapshots in this phase or
     as a scheduled follow-up task is the conductor's call, but the follow-up must be recorded;
     the current comment forecloses it on false grounds.

2. `claude_trail_store.py:552-558` - `_insert_file_versions` is the only insert path with no
   idempotency, and the justifying comment ("offset-tracked tailing guarantees each transcript
   line is processed once") is false on a designed-for path: `claude_trail_store.py:320`
   (`if size < last: last = 0`, truncated/rewritten transcript) rescans the whole file, and
   every sibling table dedups on re-process (raw_events uuid, events event_id, api_requests
   request_id, blobs sha) while file_versions duplicates every row. The crash window between
   `_insert_file_versions` (line 362) and the ingest_state offset advance (line 363) is the
   same defect with lower probability; spec step 5.2 explicitly said "Keep it in the same
   transaction as the events insert for that batch" and no transaction was used (DuckDB
   autocommits each execute). My call: not an acceptable documented limitation, because the
   rescan path is deterministic, not a crash edge. Fix now, cheapest form: dedup on `event_id`
   exactly like `_insert_events` (stage temp table + `ON CONFLICT`/anti-join; one row per
   event uuid is guaranteed by construction), which covers both the rescan and the crash
   window without touching SCHEMA_SQL. Wrapping the per-transcript batch + offset advance in
   an explicit transaction fixes only the crash window, not the rescan, so prefer the dedup.

## Minor

1. `tests/test_store.py` - no test for the dominant real shape: an Edit with
   `originalFile: null` (74% of corpus Edits). Should assert the row is still emitted with
   null pre_sha/post_sha and the structuredPatch as patch. All fixtures assume pre is present.
2. `tests/test_store.py:684` - no `replaceAll=True` case. `_post_content`'s all-vs-first
   branch is the one reconstruction subtlety and is untested (I validated it against the 2
   real replaceAll events in the corpus; a unit test should pin it).
3. `tests/test_store.py:846-848` - the "idempotent re-ingest" assertion only exercises the
   unchanged-offset path (trivially no reprocessing). It does not cover the truncation-rescan
   path where the dup bug (Major 2) actually fires; add a test that rewrites the transcript
   shorter-then-longer (or resets ingest_state) and asserts no file_versions growth once
   Major 2 is fixed.

## Nits / follow-ups

1. `claude_trail_store.py:476` - `text.encode("utf-8", "replace")` re-encodes what `_sha`
   already encoded; compute the bytes once and pass to both. Trivial.
2. `claude_trail_store.py:350-353` - `_put_blob` runs one autocommitted INSERT per blob inside
   the parse loop; fine at current scale, batch if ingest ever slows.
3. Write results with `userModified: true` store the user-modified final content; worth a note
   in the findings comment when it is rewritten (Major 1).

## Notes

- Scope: exactly the 2 spec files touched; TUI, hook, and SCHEMA_SQL untouched. No em/en
  dashes anywhere in the diff. Full suite: 321 passed.
- replaceAll correctness (explicitly asked): CORRECT. `_post_content` uses
  `pre.replace(old, new)` when replaceAll else `pre.replace(old, new, 1)` (first occurrence
  only), matching Edit semantics. Independently validated per-event by applying each Edit's
  own structuredPatch hunks to originalFile and comparing: 288/288 match, including both
  replaceAll=True events in the corpus, zero mismatches.
- Detector validated on 5272 real toolUseResults: filePath+structuredPatch co-occur on all
  1427 edit/write results; the single filePath-without-structuredPatch case is an ExitPlanMode
  `plan` result, correctly excluded; PDF reads (filePath nested under `file`) and Bash results
  excluded as designed.
- `_put_blob` is correct: sha256 addressing, `ON CONFLICT (sha) DO NOTHING`, None-safe, empty
  string is a real blob. `_render_patch` priority (structuredPatch JSON, difflib fallback,
  None for create) matches spec. `file_version_from_event` never raises on malformed input;
  row keys match `_FV_COLS` and the file_versions DDL.
- Tests are hermetic (tmp_path + monkeypatched STORE_PATH/PROJECTS_DIR) and assert real
  content round-trips, not just row counts.
- What's good: the adaptation from the spec's guessed field names to the real Write/Edit
  shapes is right, the Edit reconstruction is exactly correct, blob dedup works, and the e2e
  test proves a byte-for-byte reconstruction through ingest. The two majors are a wrong
  investigation record plus a missing dedup, both cheap to fix; the extraction core is solid.
