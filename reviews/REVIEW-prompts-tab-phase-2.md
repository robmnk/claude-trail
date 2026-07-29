# Phase 2 review: TUI: tab scaffolding

Verdict: changes-requested
Diff: main...HEAD, 2 files, +896/-95

## Blockers

none

## Major

**1. `claude_trail.py:2157` + `tests/test_claude_trail.py:2101` - "a rename in the query breaks
loudly here" is false, and the test that claims to enforce it compares the dataclass against a
literal copy of the keys.**

`_record_from_row` (:2141) drops unknown keys, and every `PromptTurn` field except
`session_id`/`ts` has a default, so a *renamed* store key silently produces a default value.
Verified against a fixture store (`tests/test_store.py::_build_prompt_store`): renaming
`preview` -> `preview_text` in the `prompt_turns` result gives `PromptTurn.preview == ''` with no
error, and all 534 tests stay green - `test_prompt_turn_fields_match_the_query_keys` (:2115)
compares `fields(PromptTurn)` to `TURN_ROW`, a dict declared 14 lines above it in the same file,
so nothing about `claude_trail_store` is under test.

Which failure mode is right: tolerating an **added** key is correct (the store owns the query
shape); silently defaulting a **missing** one is not, because phase 3's renderers will read
`preview`/`tools`/`output_tokens` and a drifted store shows a plausible-looking row of zeros
instead of an error.

Fix: keep the drop-unknown constructor, and pin the key set against the real query instead of a
copy of it:

```python
sid = _build_prompt_store(tmp_path, monkeypatch)          # tests/test_store.py helper
rows = store.prompt_turns(con, limit=10)
assert set(rows[0]) == {f.name for f in fields(feed.PromptTurn)}
assert set(acts[0]) == {f.name for f in fields(feed.TurnAction)}
```

I checked both set equalities hold today (16 turn keys, 6 action keys), so the test lands green.
Then reword the docstring: unknown keys are dropped, a rename is caught by the key-set test, not
by this constructor.

## Minor

- `claude_trail.py:1103-1115` - `filter_session_log` promises "or None if there is nothing to
  show" but returns the path to a **0-byte** file when the session has no rows in
  `command-log.jsonl`, so `o` opens an empty buffer in `$VISUAL` with no explanation. Phase 2
  makes this reachable: `o:log` is advertised in the prompts hints and wired through
  `selected_session_id()`, and on the live machine 21 of the newest 200 turns (20 of 33 sessions)
  have no rows in the log. Verified end to end on three of them: `s` and `t` work,
  `filter_session_log(sid)` returns `/tmp/claude-trail-session-<sid>.json` with `size=0`. Fix:
  `return out_path if blocks else None`.
- `tests/test_claude_trail.py:1761` (`TestVisibleRowCount`) - nothing pins the panel geometry to
  `CHROME_ROWS`, so the hand-bumped 10 -> 12 is unguarded. With `CHROME_ROWS` left at 10 the key
  hints vanish at every width (rendered h=24: `cols:` absent at both w=80 and w=160) and the full
  suite still passes; at 11 the tail (`c:clear`) is cropped at w=80. Fix: one render assertion -
  `build_panel(entries, 24, ALL, 0)` at width 160 must export exactly 24 lines, exactly
  `visible_row_count(24)` body rows, and still contain `c:clear`. (Verified: passes at 12, fails
  at 10 and 11.)
- `tests/test_claude_trail.py:2319` (`test_close_modals_clears_every_field`) - field-by-field, so
  a modal field added to `PromptsState` and forgotten in `close_detail` keeps it green. The six
  fields that exist today are all covered. Fix: derive the check - after `close_modals()`, every
  `PromptsState` field NOT in an explicit list-state allowlist (`rows, cursor, scroll_offset,
  loaded_at, status, session_filter, hide_empty`) must equal its `PromptsState()` default.
- No test for the invariant that keeps the shared modals from becoming a trap: `Tab` must be
  swallowed while a modal is open. It is (probed: all four modal kinds x both tabs return
  `NONE` with `state.tab` unchanged), but nothing pins it, and `_apply_modal_key` growing a
  `\t` branch later would silently strand a modal. Fix: one test with `session_detail` set
  asserting `apply_key(state, "\t", 10) is Action.NONE and state.tab == "prompts"`.

## Nits / follow-ups

- Narrow-geometry thresholds moved by the 2-line chrome, both pre-existing crop classes: the
  commands hint tail (`q:quit  c:clear`) is now cropped at 62..69 columns where it used to render
  (old cropped only below 62), and the hints line disappears at height <= 13 instead of <= 11
  (w=160). Inherent to a taller chrome; recording the numbers, not asking for a change.
- `claude_trail.py:1912-1915` - the empty state tells the user to run `claude-trail ingest` even
  when the store is already current, because this phase loads nothing. Correct once phase 3 lands;
  wrong advice on `main` in between. The text itself is clean (no phase/unreleased references).
- `claude_trail.py:2324` - `modal_open()` counts `prompts.detail`, but `render_panel`
  (main:2894-2899) has no branch for it, so setting it before phase 4 would swallow every key but
  esc/q/enter with no visible change. Unreachable today (`_apply_prompts_key` never sets it);
  phase 4 should add the renderer in the same change that sets the field.
- `claude_trail.py:2272` - `PromptsState.goto_top` zeroes `scroll_offset` but `goto_bottom` and
  `move_down` never touch it, where the feed pairs every move with `state.clamp(max_rows)`. Inert
  while nothing renders the offset; phase 3 needs the clamp.
- `claude_trail.py:1896` - `build_prompts_panel` ignores `term_height` for content sizing, so on a
  12-row terminal the status line and hints are cropped (verified). Phase 3's row window fixes it.
- `tests/test_claude_trail.py:2013` - `COLUMNS_BY_TAB["commands"] is feed.COLUMNS` asserts object
  identity on an implementation detail; `==` says the same thing without pinning the aliasing.
- `claude_trail.py:2662` - the commands digit branch still reads `state.visible_cols` directly
  while the prompts branch goes through `toggle_col`/`visible_cols_for`. Same dict today; the
  accessor is the new convention.
- `claude_trail.py:2385` - `visible_cols_for` hardcodes the two-tab `if`, and an unknown `tab`
  silently returns the commands dict. A third tab would need edits in `TABS`, `COLUMNS_BY_TAB`,
  `_TAB_HINTS`, `visible_cols_for` and `render_panel`; fine at two, worth a `dict` if it ever
  grows.
- `claude_trail.py:2141` - `_record_from_row(cls, row: dict)` has no return annotation in a module
  that annotates everything else.

## Notes

**Scope: exact.** The two allowed files, and exactly the checklist items (`TABS`, `AppState.tab`,
`PromptsState`, `PromptTurn`/`TurnAction`, per-tab `visible_cols`, `selected_session_id()`, `Tab`
cycling, tab strip, per-tab hints, `modal_open`/`close_modals` coverage, empty-state only). No
store import on the prompts path, no `PROMPT_COLUMNS`, no detail view, no `LOAD_PROMPTS` /
`OPEN_PROMPT` Action members. `build_prompts_panel` is required by the empty state, not creep.
The `_TAB_HINTS["prompts"]` string matches the plan's character for character.

**The `_apply_modal_key` extraction is provably behavior-preserving.** I ran a differential of
`main`'s `apply_key` against HEAD's over 14 state shapes (plain, scrolled, command detail, detail
with prompt, detail with the `_tokens_unavailable` sentinel, full-prompt view open, session modal
with scroll, db modal, search INPUT, search RESULTS, empty feed, blank `session_id`, missing
`session_id` key) x 73 keys, comparing the returned `Action.name` plus 14 state fields
(`cursor`, `scroll_offset`, `visible_cols`, `detail_entry`, `session_detail`, `session_agent_id`,
`session_scroll`, `db_info`, `detail_tokens`, `session_tokens`, `detail_prompt`,
`detail_prompt_open`, `detail_prompt_scroll`, search tuple). **Two** differences, both the
disclosed deviation (b): `o`/`t` on an **empty** feed now return `NONE` where they returned
`OPEN_SESSION`/`OPEN_TRANSCRIPT`, which `main()` then no-op'd (`entry = selected_entry(); if
entry:`), so net behavior is identical. The full-prompt depth rules, the sentinel case for
`prompt_available()`, the session modal's j/k/g/G, and Ctrl-C are byte-identical. The `s` refactor
into `open_session_detail` is also equivalent: it skips the `session_scroll = 0` write on the
no-row path, and `session_scroll` is provably already 0 there (`close_modals` zeroes it and only
the open session modal increments it).

**Modal reachability: no trap, no stale state.** Probed all four modal kinds (command detail,
session, db, prompts turn) from both tabs: `Esc`/`q`/`Enter` close each one and leave `state.tab`
untouched; `j` scrolls the session modal and is swallowed by the others; `Ctrl-C` still quits; and
`\t` returns `NONE` without switching tabs from either tab, so no tab switch can orphan a modal.
The search modal still owns `\t` (root toggle) ahead of the tab dispatch.

**`CHROME_ROWS` 10 -> 12 is exactly right, verified by rendering.** Chrome is 11 lines (2 border,
2 padding, strip, its blank, table header, heavy rule, spacer, status, hints) plus 1 slack line,
and the slack is precisely what absorbs the hint wrapping to a second line at width <= 130. So
disclosed deviation (c) costs nothing at 80/100/120 columns: at h=24 the panel exports exactly 24
lines with 12 body rows and the full hint at all three widths, cropping neither the status line nor
a body row. The wrap threshold is w<=130 (1 line at >= 135); the crop only starts when a third
hint line is needed (below 70). Row counts confirm the +2 is the strip and its blank and nothing
else: 14->12 at h=24, 20->18 at h=30, 40->38 at h=50.

**Per-tab cursor state is not conflated.** `AppState.clamp` touches only the feed's
`cursor`/`scroll_offset`; `PromptsState.cursor` indexes `rows` directly (documented at :2227) and
`selected()` bounds-checks both ends; `selected_row`/`selected_session_id` branch on `tab` and
reach the reversed view only through `selected_entry()`. The dedicated test
(`test_each_tab_keeps_its_own_cursor_across_switches`) drives it through `apply_key`, not by
poking fields.

**`visible_cols`/`prompt_cols` holds up for phase 3.** `COLUMNS_BY_TAB` is the single registry,
`_default_cols(tab)` derives the visibility dict from it, `toggle_col(col, tab=None)` keeps the
last-visible-column guard per tab (`if col not in cols: return`, then the `sum(...) > 1` check on
that tab's dict), and the digit branch is a no-op for an unregistered key. Phase 3 needs one
assignment (`COLUMNS_BY_TAB["prompts"] = PROMPT_COLUMNS`) plus a `build_prompts_table`; nothing
here bakes in a commands-only assumption. `AppState.prompt_cols` resolves the registry at instance
construction, so `PROMPT_COLUMNS` must exist by import time (it will).

**Deviation (a) confirmed pre-existing:** `main`'s `d` branch already returned `LOAD_DB_INFO`
unconditionally, so not gating it on a row is consistency, not a new decision.

**Session-scoped keys on the prompts tab, measured on the live store:** for three sampled
newest-200 turn sessions that are absent from `command-log.jsonl`, `s` renders the session panel
fine (`live=False`, name falls back to the short id) and `t` resolves the transcript. Only `o`
degrades - see the Minor.

**`int(ch)` after `ch.isdigit()` cannot raise** on the new unguarded prompts branch: `read_key`
reads one byte and decodes with `errors="replace"`, so a multi-byte key (e.g. `²`, whose
`isdigit()` is True) arrives as U+FFFD, and `"�".isdigit()` is False.

**Tests: 46 new, 534 total, green (9.5s).** No existing test was modified or re-baselined (the
diff is additive apart from one import); `TestVisibleRowCount` was left alone, which is exactly
why the `CHROME_ROWS` change is unguarded (see Minor). The tab-strip test inspects real span
styles rather than just the plain text; the purity checks are shallow but adequate (`db_info is
None` after `d`, `search is None` after `/`, feed untouched after `c`); the inert-key test covers
`↵`/`r`/`x` and asserts no half-open modal. The three weak ones are called out above.

**Advertising `↵`/`r`/`x` while they are inert** is acceptable for an intra-plan phase (the plan
fixes the hint string and phases 3-4 wire the keys before the phase-6 version bump), but it does
mean `main` carries a tab with three dead hints and a "run ingest" instruction that does nothing
until phase 3 merges.
