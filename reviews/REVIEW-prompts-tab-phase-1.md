# Phase 1 review: Store: turn model + queries

Verdict: changes-requested
Diff: main...HEAD, 2 files, +673/-10

> Note: this file overwrote the 2026-07-07 audit-store phase-1 review (committed in `df3953c`,
> recover with `git show df3953c:reviews/REVIEW-phase-1.md`). Consider plan-scoped review
> filenames.

## Blockers

none

## Major

**1. `claude_trail_store.py:1979` - the SQL "clean" is not the Python "clean": DuckDB's
`trim()` strips only spaces, Python's `.strip()` strips all whitespace.**

The comment at :1969-1971 claims "the reminder-stripping and noise rules have one definition
each". Only the *regex* is shared; the trimming is not. Verified in DuckDB 1.5.4:
`SELECT trim(e'\n hi \n')` -> `'\n hi \n'` (newlines, tabs and CRs survive).

Three wrong outputs, reproduced in a hermetic store:

| input text | `preview` / `full_len` | `turn_prompt_text` | should be |
|---|---|---|---|
| `fix the bug\n<system-reminder>ctx</system-reminder>` | `'fix the bug\n'` / 12 | `'fix the bug'` | `'fix the bug'` / 11 |
| `<system-reminder>ctx</system-reminder>\n<task-notification>agent done</task-notification>` | **becomes a row**: `'\n<task-notification>agent done</task-notification>'` | - | filtered as noise: the `^` anchor misses because of the surviving `\n`, while `prompt_for_command` on the same row correctly skips it |
| `<system-reminder>ctx</system-reminder>\n` | **becomes a row**: `preview='\n'`, `full_len=1` | `None` | filtered by `clean <> ''` |

Live-store measurement (`~/.claude/trail.duckdb`, 5,442 main-thread user turns): 1,380 turns
have a `sql_clean` that differs from a full strip; 3 of the newest 200 turns reach
`prompt_turns` with an unstripped preview, one of them **starting** with `\n`
(`'\nPiyush Kaila: Right now...'`, `full_len` 2752 vs 2750 for the Python text). Phase 3 renders
"first line of the preview", so that row's Prompt cell will be blank. The noise-leak and
phantom-row variants measure 0 today, so those two are latent, not live.

Fix (verified: `SELECT trim(e'\n a \n', e' \t\n\r')` -> `'a'`):

```sql
trim(regexp_replace(text, ?, '', 'sg'), e' \t\n\r') AS clean
```

Add the two inputs above as tests, asserting `preview == turn_prompt_text(...)` and
`full_len == len(turn_prompt_text(...))` for a reminder-suffixed turn.

## Minor

- `claude_trail_store.py:1847` - `slash_label` uses `.search`, so a `<command-name>` block
  *anywhere* in a prose prompt swallows the whole turn:
  `slash_label("how does <command-name>/compact</command-name> behave when I ask this long question?")`
  -> `'/compact'`. Same for `_SLASH_ARGS_RE`, which takes the first `<command-args>` in the
  string even when it belongs to a later block. Fix: require the block at the start
  (`re.match(r"\s*<command-name>", text)`) and read args only from the head of the text.
- `claude_trail_store.py:2068` - `LEFT JOIN events r ON r.tool_use_id = u.tool_use_id` can fan
  out. Live store: 34 `tool_use_id`s carry 2 `tool_result` rows (all same-session, consistent
  `is_error`), so those calls appear **twice** in `turn_actions`, eat two of the 500 `LIMIT`
  slots, and disagree with `prompt_turns.tools` (which counts `events` with no join). It
  mirrors the pre-existing `actions` view (:128), so it is a convention rather than a
  regression; fix by joining a deduped result set (`SELECT tool_use_id, max(is_error) AS
  is_error FROM events WHERE type='tool_result' GROUP BY 1`).
- `tests/test_store.py:1113` (`test_prompt_turns_window_ends_at_the_next_prompt`) - the two
  bounds the spec is explicit about (`>=` lower, `<` upper) are not pinned: in the fixture no
  `tool_use` shares a timestamp with any prompt (02:00:01 vs 02:00:00, nothing at 02:00:05), so
  flipping `>=`->`>` or `<`->`<=` keeps every test green. I verified the implementation is right
  by probe (a call at exactly the prompt ts counts in that turn; a call at exactly `next_ts`
  counts in the next). Fix: add one `tool_use` at the prompt's ts and one at the next prompt's
  ts, then assert `tools == 2` / `tools == 1`.
- `tests/test_store.py:1354` (`test_turn_noise_pattern_agrees_between_python_and_duckdb`) -
  cannot fail on "drift": both sides read the same `TURN_NOISE_PATTERN` string. The only real
  thing it checks is that the hand-rolled escaping parses in RE2 (worth keeping, but the
  docstring oversells it), and it misses the divergence that actually exists (the Major above).
  Strengthen by running the whole SQL clean pipeline (`trim(regexp_replace(...))` +
  `regexp_matches`) against `clean_prompt_text` + `TURN_NOISE_RE.match` over texts that include
  leading and trailing newlines.
- `claude_trail_store.py:2058` - a falsy `ts` silently drops the lower bound, so
  `turn_actions(con, sid, None, next_ts)` returns the session's entire history before `next_ts`
  rather than one turn. `prompt_turns` never emits a NULL ts (`ts IS NOT NULL` in the raw CTE),
  so no caller hits it today; either reject a falsy `ts` (`return []`, as for `session_id`) or
  document it.

## Nits / follow-ups

- `claude_trail_store.py:1920` - "longest real turn measured: 21,697 chars" is the max of the
  newest 200 turns, not of the store: the store-wide max main-thread user turn is **646,245**
  chars (three copies of a `"You are summarizing a Claude Code session..."` hook prompt). It
  falls outside today's newest-400 window, but `turn_prompt_text` has no cap, so phase 4 needs
  one. Reword to "longest in the scan window".
- `claude_trail_store.py:1976` - `ORDER BY ts DESC LIMIT {raw_scan}` can split a same-`(session,
  ts)` group at the boundary, so the oldest edge's collapse may keep the shorter text. Only
  reachable when more than half the raw scan is noise (then `recent` reaches the raw edge). One
  sentence in the `PROMPT_TURN_RAW_SCAN` comment covers it.
- `arg_max(x, (length(clean), event_id))` is total (`event_id` is the `events` PK; DuckDB 1.5.4
  orders structs lexicographically - checked that `(2,'b')` beats `(2,'a')`), but no test has
  two equal-length texts at one `(session_id, ts)`, so the tie-break half is unexercised.
- `CLAUDE.md:208` still names `_AUTO_TURN_RE`, which no longer exists. Phase 6 must rename it
  and mention the `[Image: source:` fix to the command modal.
- `prompt_turns(con, limit=-1)` raises `BinderException: LIMIT/OFFSET cannot be negative`, so
  phase 5's `--limit` should clamp to `>= 0`. Injection is not reachable: `limit="1; DROP TABLE
  events"` raises `ValueError` at `int()` before any SQL is built.

## Notes

**Scope.** Exactly the two files the phase allowed, and exactly the promised deliverables
(`TURN_NOISE_PREFIXES`/`_PATTERN`/`_RE`, `slash_label`, `prompt_turns`, `turn_actions`,
`turn_prompt_text`, `prompt_for_command` switched over). `action_label` is correctly deferred to
phase 4 despite appearing in the plan's query section. No scope creep. Comment density matches
this file's (heavy) style.

**Disclosed deviations, each verified independently, all fine:**

1. `arg_max(event_id, (length(clean), event_id))` - correct and total; both aggregates order by
   the identical expression, so `event_id` and `clean` always come from one row. The collapse
   test proves the agreement (`turn_prompt_text(event_id) == preview`).
2. Both regexes bound as parameters - parameter order matches textual `?` order (the session
   filter sits in the `raw` CTE, textually before `kept`/`turns`); the session-filter test
   exercises the 3-param path.
3. Hand-rolled escape - `meta = r"\.^$|()[]{}*+?"` covers every Python `re` and RE2
   metacharacter. The `re.escape` justification is accurate: Python 3.11 emits
   `'Base\\ directory\\ for\\ this\\ skill:'`. `^` is not multiline in either engine
   (`regexp_matches(e'hello\n<task-notification>x', '^(<task-notification>)')` -> false), so
   the anchoring semantics agree. `'sg'` reproduces Python's `re.S` sub exactly (checked on a
   multi-line reminder).
4. `ts IS NOT NULL` in the raw CTE - needed; keeps `lead()` and every window comparison sane.
5. `next_ts` None for each session's newest turn - verified, including the raw-scan straddle
   (`raw_scan=4` over a 5-turn fixture returns the truncated session's newest turn with
   `next_ts=None` and chains its predecessor correctly). The suffix-closure argument holds as
   written: a global `ORDER BY ts DESC LIMIT N` implies every turn newer than a fetched turn is
   also fetched, and a session's next prompt is strictly newer.
6. Counts coalesce to 0 - verified; `last_ts` stays None for a request-less turn, as documented.

**`sessions.cwd` is always NULL - confirmed.** `_upsert_session` (`claude_trail_store.py:480`)
inserts only `session_id, project, first_ts, last_ts, event_count`, and it is the *only* write
to `sessions` in the module (`grep "INTO sessions"` -> one hit), so `git_branch`, `title` and
`cwd` are dead columns. **Phase 3's Project column must render `project` alone**; the plan's
"falls back to `cwd`" branch would be unreachable. Keeping `cwd` in the result dict is fine (the
key documents the gap), and the test asserting `cwd is None` will break loudly for the right
reason if a later phase populates it.

**`tools` includes `subcalls`.** Fixture: 1 main-thread Bash + 2 subagent calls -> `tools=3`,
`subcalls=2`. That matches the plan's SQL, but the plan's phase-4 mock header ("23 tool calls
(14 Bash) - 2 subagents, 41 of their calls") reads as additive. Phase 4 must subtract or reword.

**Read-only contract.** No writes, no module state, no cached connection; every function takes a
`con` and returns plain dicts. The only raise paths are caller errors (negative `limit`, non-int
`limit`). All three relations used (`events`, `api_requests`, `sessions`) exist in schema 1, so
no `information_schema` guard is needed for a not-yet-migrated store.

**Measured on the live 1.1 GB store** (read-only, retried around ingest locks): `prompt_turns()`
545ms cold / 119ms warm for 200 rows (plan claimed 113ms); `turn_actions` on the biggest turn in
that window (125 calls) 11ms with no duplicate rows; 51 of 200 turns are zero-call (plan said
54); the newest-200 `full_len` max is 21,697, matching the docstring exactly.

**Test coverage.** Every case the phase checklist asked for is present, plus `raw_scan`, preview
truncation, three `turn_actions` cases, the regex-agreement test and `slash_label`. The tests are
hermetic (the autouse `_isolate_file_history` fixture keeps `ingest_all` off the developer's real
`~/.claude/file-history`) and fast (34 selected tests in 4.1s). Full suite: 484 passed, matching
the conductor. The two tests that would still pass with the behavior broken are called out under
Minor (window inclusivity, regex agreement).

**`prompt_for_command` behavior change.** Return shape and tie-breaking are untouched (same
query, same `PROMPT_LOOKBACK_TURNS` walk, same `{"text", "ts"}`); only the skip-set widened, and
the new image-echo regression test pins the intended fix. Worth knowing: a command run right
after a compaction now reports the last real pre-compaction prompt instead of the 18 KB
carry-over blob (still inside the 16-turn lookback). That is the plan's intent and reads as an
improvement, but it is a user-visible change to the existing modal.
