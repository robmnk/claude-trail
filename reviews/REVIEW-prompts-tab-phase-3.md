# Phase 3 review: TUI: prompts list

Verdict: changes-requested
Diff: main...HEAD, 2 files, +1434/-80

## Blockers

**1. `claude_trail.py:473` (`_render_turn_prompt`) - the Prompt cell is a bare `str`, so Rich
parses user prose as console markup. Text is silently deleted, and an unmatched closing tag
crashes the TUI.**

`Table.add_row` renders a `str` through `console.render_str`, which has markup enabled. The feed
never hit this because `_render_command` returns `Text`; this phase is the first to put user
prose in a cell.

Crash, reproduced end to end (not reasoned about):

```python
turn = PromptTurn(session_id="s1", ts="2026-07-29 10:00:00-03", preview="look in [/tmp/x] now", tools=1)
with Live(build_prompts_panel(PromptsState(rows=[turn], loaded_at=1.0, checked_at=1.0), 24)) as live:
    live.update(build_prompts_panel(...), refresh=True)
# rich.errors.MarkupError: closing tag '[/tmp/x]' at position 8 doesn't match any open tag
```

It is raised inside `live.update(render_panel(), refresh=...)` in `main()`'s loop, so it escapes
past `except KeyboardInterrupt` and the TUI dies with a traceback. Also triggered by
`"check [/usr/bin] please"`, `"the [/] marker"`, `"run [/compact] first"`.

Silent deletion, every one verified by rendering:

| preview | Prompt cell |
|---|---|
| `python list[int] anno` | `python list anno` |
| `grep '[a-z]' file` | `grep '' file` |
| `read [docs](http://x)` | `read (http://x)` |
| `- [x] done` | `-  done` |
| `[[wikilink]] please` | `[] please` |
| `use [bold] here` | `use  here` |

Live store, all 5,452 main-thread user turns: 2 already mangle today, one of them about this
project - `"...when we press [enter] claude-trail should show the full command."` renders as
`"...when we press  claude-trail should show the full command."`. The crash class measures 0
today but needs only one `[/...]` in one prompt.

Fix: `return Text(first) if first else Text("(empty)", style="dim italic")`. Same class in
`_render_turn_project:415` (`short_path` returns `str`) and `_session_cell:302`'s untinted branch
(a session `name` comes from pid.json, so it is user-chosen) - wrap all three in `Text(...)`.
Note the fix breaks six assertions that compare against a bare `str`
(`test_prompt_column_shows_the_first_line_only` at `tests/test_claude_trail.py:2389` and its
three siblings, `test_project_column_renders_project_and_never_cwd`,
`test_session_column_falls_back_to_the_short_id`); switch them to `.plain` and add
`"look in [/tmp/x] now"` as a regression case.

## Major

**1. `claude_trail.py:3583` - `LOAD_PROMPTS` runs one ~275ms synchronous store read per buffered
keystroke, inside the loop whose stated job is to coalesce buffered keystrokes into one render.**

`r` returns `LOAD_PROMPTS` on every press and `Tab` on every other press, and neither is
self-limiting (unlike `d`/`Enter`/`s`, whose modal makes `modal_open()` swallow the repeat, so
they load exactly once). Measured on the live 1.07 GiB store: `_load_prompt_turns()` is 602ms
cold, 271-280ms steady state (each call reopens the connection, so there is no warm-connection
path). Replaying `main()`'s drain loop:

```
10 buffered 'r'  -> 10 store reads, 3.21s inside ONE drain loop (no redraw)
20 buffered Tab  -> 10 store reads, 2.83s inside ONE drain loop (no redraw)
```

Auto-key-repeat produces those bursts from a held key, so holding `r` freezes the UI for
~7s per second held and falls further behind while held.

Fix: coalesce to at most one refresh per frame - set `reload_prompts = True` in the drain loop
and run `_refresh_prompts(state.prompts, prompts_row_count(console.height))` once after the
drain, next to the existing `state.prompts.clamp(prompt_rows)`. That matches the loop's existing
drain-then-render-once design and makes the tab-entry / `r` / `S` paths all cost one query per
frame.

**2. `claude_trail_store.py:1829` (`_SLASH_HEAD_RE`) - plugin/skill slash commands emit
`<command-message>` first, so `slash_label` does not match and the Prompt cell shows raw XML.
Confirmed: 3 of the newest 200 turns.**

Rendered against the live store, two of the top 15 rows read
`<command-message>dev-orchestrate<…` instead of `/dev-orchestrate --repo ~/...`. The three real
payloads are `<command-message>dev-orchestrate</command-message>\n<command-name>/dev-orchestrate
</command-name>\n<command-args>...`, twice, plus one `m2-standup`.

The implementer's claim that the fix belongs in `_SLASH_HEAD_RE` is right: its trailing group
`(?:\s*<command-[a-z-]+>[^<]*</command-[a-z-]+>)*` already accepts sibling blocks *after*
`<command-name>`, so the change is to allow them before it too, e.g.

```python
r"\s*(?:<command-[a-z-]+>[^<]*</command-[a-z-]+>\s*)*?<command-name>\s*([^<]+?)\s*</command-name>"
r"(?:\s*<command-[a-z-]+>[^<]*</command-[a-z-]+>)*"
```

`_SLASH_ARGS_RE.search(m.group(0))` keeps working. This is a phase-1 defect that phase 3 makes
visible; it needs a scope waiver for `claude_trail_store.py` now, or an explicit carry into phase
5/6 with a test.

**3. `tests/test_claude_trail.py:2643,2651` - the `r`/`x` rewrites dropped phase 2's
half-open-modal guard, and it is the exact trap phase 2's review flagged.**

The phase-2 test asserted `state.prompts.detail is None and state.modal_open() is False` after
`r` and `x`. `test_enter_is_still_inert_on_the_prompts_tab` kept those assertions for `\r`/`\n`
only; `test_r_asks_for_a_reload_without_touching_the_list` and
`test_x_toggles_hide_empty_without_a_reload` check rows/cursor/`loaded_at`/`hide_empty` but not
`detail`. Mutation-verified: adding `prompts.detail = prompts.selected()` to either the `r` or
the `x` branch leaves all 618 tests green, and in the TUI it makes `modal_open()` true with no
`render_panel` branch for it - every key but esc/q/enter dead, nothing on screen to explain it.
Fix: add `assert state.modal_open() is False` to both tests.

## Minor

- `claude_trail.py:486` - `Calls` at width 12 truncates mid-word on real data: 5 of the newest
  200 turns render `185 (82 bas…` / `383 (193 ba…` / `377 (231 ba…` (max cell length 14). Widen
  the default to 14, or shorten the suffix (`185 · 82 b`).
- `claude_trail.py:3341` + `:2174` - `_refresh_prompts` stamps `loaded_at` on a read that
  produced no answer, and `_prompts_empty_state` has no branch for a failure `status`, so the
  panel makes two false statements at once. Rendered for `status="store unreadable: schema is
  from the future"`: header `as of 22:30:04 (0s ago) · 0 turns`, body `No prompts in the audit
  store.` / `Fill it with: claude-trail ingest`. Same wrong body for `NO_STORE_HINT`. Worse, a
  non-lock failure discards a good snapshot (`rows = []`) where a lock collision keeps it, so a
  transient error (store deleted between the `exists()` check and `connect`) blanks the list.
  Fix: do not advance `loaded_at` when `status` is a failure, and add a `prompts.status` branch to
  `_prompts_empty_state` before the "no prompts in the store" fallback. The docstring at :2174
  already promises exactly this ("advising `claude-trail ingest` when the store is already current
  ... would be wrong advice").
- `claude_trail.py:2228` - `build_prompts_panel`'s `visible_cols` argument is untested.
  Mutation-verified: replacing `cols = visible_cols or _default_cols("prompts")` with
  `cols = _default_cols("prompts")` leaves 618 tests green, so nothing pins that a digit toggle
  changes the rendered panel *or* the `cols: 1 2 3 …` indicator.
  `TestPromptsTable.test_hidden_columns_are_not_rendered` covers `build_prompts_table` only. Fix:
  one panel-level assertion (`build_prompts_panel(prompts, 24, {**ALL, 3: False})` -> `"Project"
  not in out`).
- `tests/test_claude_trail.py:2378` - the `_abbrev_count` magnitude boundaries are pinned only by
  length, not by value: mutating `if n < 1000` to `n <= 1000` (so `_abbrev_count(1000) == "1000"`
  instead of `"1.0k"`) survives the whole suite. The `n < 1_000_000` boundary *is* caught. Add
  `assert feed._abbrev_count(1000) == "1.0k"`.
- `tests/conftest.py:22` - confirmed: the prefix strip is `CLAUDE_TRAIL_COL_`, which does not
  cover the six new `CLAUDE_TRAIL_PROMPT_COL_*` keys. Not just cosmetic once phase 6 documents
  them: `TestPromptsPanelGeometry._body_rows` counts body rows by matching `sid\d{5}`, so a
  developer with `CLAUDE_TRAIL_PROMPT_COL_SESSION_WIDTH=5` exported would ellipsize the label to
  `sid…` and every geometry test would fail on their machine. Fix: strip
  `CLAUDE_TRAIL_COL_`-or-`CLAUDE_TRAIL_PROMPT_COL_`, or just `CLAUDE_TRAIL_` minus
  `CLAUDE_TRAIL_ENV`.
- `claude_trail.py:3604` - "measured ~70-120ms warm, up to ~0.5s right after an ingest write"
  measures `prompt_turns` on a held connection, not what `main()` calls. `_load_prompt_turns`
  (connect + query + close) is 271-280ms steady state, 602ms cold, on the 1.07 GiB store. Reword
  to the number the loop actually pays.
- `claude_trail_store.py:352` (`_decode_project`) - confirmed: `name.replace("-", "/", 1)
  .replace("-", "/")` renders 3 of the 6 distinct projects in the newest 200 as paths that do not
  exist - `…/claude/trail` for `claude-trail`, `…/personal/fin` for `personal-fin`,
  `…/keypad/macros` for `keypad-macros`. The column is right to render `project` (it is the only
  populated source: `SELECT count(*) FROM sessions WHERE cwd IS NOT NULL` -> 0), so this is not a
  phase-3 defect, but a **better source does exist and the plan's dismissal of it was wrong for
  the wrong reason**: every transcript line carries an exact top-level `cwd`
  (`/home/naka/Projects/personal/claude-trail`, checked in a live transcript). `sessions.cwd` is
  dead because `_upsert_session` never writes it, not because the data is missing. Carry into
  phase 5/6: populate `sessions.cwd` from the transcript `cwd` at ingest and prefer it in
  `_render_turn_project`. Until then the primary view names half this user's projects wrongly.

## Nits / follow-ups

- `claude_trail.py:2653` - `move_down`'s `len(self.visible_rows())` is redundant: mutating it to
  `len(self.rows)` survives the suite, because every caller pairs the move with `clamp()`, which
  re-bounds against the visible rows. Keep it (defensive), but nothing pins it.
- The `S` key and the changed `_TAB_HINTS["prompts"]` string are not in the plan's "TUI shape"
  section (phase 2's review noted the old string matched the plan character for character).
  Justified by the checklist's `session_filter` item, but the plan, CLAUDE.md's Controls table and
  the README now disagree with the code; phase 6 owns reconciling all three.
- `claude_trail.py:2168` - the header scope reads `session sid-abcd` (raw id prefix) while the
  Session column shows the resolved name. `_prompts_header` has no `name_map` to fix it with;
  worth one if the scoped view sticks.
- `claude_trail.py:3205` - if an `on_error` callback raises, the exception propagates out of
  `_with_store`. Unreachable today (the only caller passes `list.append`) and the modals pass no
  hook, so their behavior through this function is byte-identical. Worth one `try` if a second
  caller ever appears.
- `tests/test_claude_trail.py:2868` - `assert len(lines) == 24` contributes nothing to the chrome
  pin: `build_prompts_panel` passes `height=term_height` to the `Panel`, so I measured 24 lines at
  chrome 12, 13, 14 *and* 15. `body_rows == prompts_row_count(24)` is tautological for the same
  reason. The real pin is `test_chrome_rows_is_not_one_row_too_many` at w=80 - keep that one and
  say so in the docstring.
- `tests/test_claude_trail.py:2220` (`test_prompts_empty_state_points_at_the_store`) is now fully
  subsumed by `TestPromptsPanelStates.test_never_read_says_so_and_how_to_read_it`.
- `main()`'s auto-refresh gate (`state.tab == "prompts" and not state.modal_open() and
  _prompts_refresh_due(...)`) has no test - `_prompts_refresh_due` is well covered but the
  composition is not. Consistent with `main()` being untested throughout this codebase; noting it
  because it is the only place the modal exclusion is enforced for the store read.

## Notes

**Geometry: `PROMPTS_CHROME_ROWS = 13` is correct, and I verified it by rendering rather than by
reading the tests.** At h=24 the panel is 24 lines with 11 body rows at both w=80 and w=160, and
both the snapshot header (`as of`) and the hint tail (`q:quit`) survive. Pinned from both sides:
at chrome 12 / w=80 `q:quit` is cropped (so 13 is minimal), and 13 = `CHROME_ROWS + 1` is exactly
the header line. The rendered numbers at h=24, w=80: chrome 12 -> 12 body / no `q:quit`; 13 -> 11
body / `q:quit`; 14 -> 10 body / `q:quit`; 15 -> 9 body / `q:quit`. The "too large wastes a body
row" case is caught only by the chrome-1 test, not by the line-count assertions (see the nit).

**Store connection discipline: clean.** `_with_store` keeps the `try/finally con.close()` around
`fn(con)`, and every failure path (module import, missing file, `connect`, the query, `close`
itself) ends in `return None` with the connection already closed. No path leaks a connection, and
nothing holds one across a frame. The feed and the hook still never load duckdb:
`claude_trail_store` has no module-level `import duckdb` (it is lazy inside `connect` at
`claude_trail_store.py:160`), so `_slash_label`'s lazy `from claude_trail_store import
slash_label` costs a parse at worst - and by the time a turn row exists, `_load_prompt_turns` has
already imported the module. Claim verified.

**`_with_store(fn, on_error=None)`: the catch is genuinely unchanged.** `except Exception` is the
same clause, `return None` is the same result, and `on_error` fires only inside it. No new
exception class escapes. The `STORE_PATH.exists()` guard sits *outside* the inner `try`, so a
store that vanishes between `_load_prompt_turns`'s own `exists()` check and `_with_store`'s
returns `None` with `failure` empty -> `([], NO_STORE_HINT)`, which is the right answer for that
race. Modals pass no hook, so their path is byte-identical.

**Busy vs no-data vs empty: the three-state machine is right, with one leak (Minor 2).**
`rows is None` is produced only by `is_lock_conflict`; `[]` from a successful empty read is
correctly distinguished (`if rows is None` never sees a falsy list). A busy collision on the
*first* load correctly avoids claiming a kept snapshot (`STORE_BUSY_KEPT_HINT if prompts.rows else
status`), and the age line can never count from a replaced snapshot on the busy path -
`loaded_at` and `rows` are written together or not at all. The one dishonest state is the non-lock
failure and the missing-store case, where `loaded_at` advances for a read that answered nothing.

**`apply_key` purity holds on every new key.** `r`, `x`, `S`, `1`-`7` and `\t` mutate only state
and spawn nothing; `S` reads `selected_session_id()`, which is pure. The feed's clamp math is
untouched - `state.clamp(max_rows)` still runs against `visible_row_count(console.height)` in the
per-frame block, and `_tab_row_count` only changes what `apply_key` receives. One benign wrinkle:
keys buffered *after* a `\t` in the same drain are clamped with the previous tab's height (12 vs
11), which puts the cursor one row outside the window; the per-frame `state.prompts.clamp(
prompt_rows)` runs before `render_panel()`, so it self-heals with nothing visible on screen.
Verified by tracing the `G`-after-`Tab` case.

**Cursor/window correctness under filtering: no off-by-one found.** I probed `x` hiding the row
under the cursor, a reload shrinking the list, `S` narrowing to 3 rows from a cursor at 200, and
`G` then a shrink; every case re-anchors through `clamp` over `visible_rows()`. `selected()`,
`move_*`, `goto_*` and the panel's slice all index the same visible list, and the status line
(`1/200`) plus the header (`2 turns (2 hidden)`) count that same list. Rendered against the live
store at w=130/h=28: `200 turns`, `1/200`, cursor row highlighted, `-` for zero-call turns, blank
Agents at 0.

**Column facts from phase 1 all respected.** `tools` is rendered as the total and never summed
with `subcalls` (`64 (14 bash)`, never `105`); `project` renders and `cwd` never does (pinned by a
sneaky-row test); the Prompt cell applies `slash_label` and shows one line. `_abbrev_count` holds
6 columns at every boundary I checked (999 -> `999`, 1000 -> `1.0k`, 99_999 -> `100.0k`, 999_999
-> `1000k`, 1_000_000 -> `1.00m`, 987_654_321 -> `988m`). Against the live 200 rows, only `Calls`
overflows its width (Minor 1); Time/Project/Tokens max out at exactly 8/15/16.

**Time column is timezone-correct.** The store hands back `'2026-07-29 21:40:52.813-03'` and
`format_time`'s `.astimezone()` renders `21:40:52`, matching the feed's own rows on the same
clock. No UTC/local split between tabs.

**The six rewritten phase-2 tests, one by one.** Five preserved or strengthened the original
intent; one weakened a guard (Major 3).

| test | verdict |
|---|---|
| `test_only_the_commands_tab_has_columns_registered` -> `test_each_tab_registers_its_own_columns` | stronger (adds the independent 1..6 / 1..7 key numbering) |
| `test_visible_cols_for_follows_the_active_tab` | preserved; also swaps phase 2's `is feed.COLUMNS` identity nit for `==` |
| `test_tab_cycles_commands_prompts_commands` | preserved; still pins that leaving the tab returns `NONE` |
| `test_prompts_hints_are_per_tab` | preserved + `S:session only`; dropped the explicit `visible_cols={}` argument, which is now the only reason the panel's `cols` fallback is unpinned (Minor 3) |
| `test_digits_do_not_toggle_columns_from_the_prompts_tab` -> `test_digits_toggle_this_tabs_columns_and_not_the_feeds` | stronger, plus two new siblings (last-column guard, unregistered digit) |
| `test_prompts_only_keys_are_inert_on_the_commands_tab` | stronger (`S` added, `prompts == PromptsState()`) |
| `test_advertised_but_unwired_keys_are_inert_on_the_prompts_tab` -> `test_enter_is_still_inert_on_the_prompts_tab` | **weakened**: `r`/`x` lost the modal assertion (Major 3) |

**Test quality: strong. 24 mutations run, 21 caught.** I did not trust the reported table; I
mutated the source myself and ran the full suite each time. Caught: summing `tools + subcalls`,
inverting the `hide_empty` predicate, stamping `loaded_at` on a busy collision, `clamp`'s
`- max_rows + 1` off-by-one, gating `_prompts_refresh_due` on `loaded_at`, reversing the table
rows, `prompts_row_count` reading `CHROME_ROWS`, skipping `slash_label`, dropping `S`'s cursor
reset, returning `[]` instead of `None` on busy, dropping the clamp after `x`, dropping the clamp
in `_refresh_prompts`, counting raw rows in the header, `hidden_count` -> 0, dropping the session
filter in the loader, `selected()` over raw rows, `_abbrev_count`'s 1M boundary, "always blame
ingest" in the empty state, the status line's row count, `x` never toggling back, `_format_age`
minutes, and collapsing the prompt cell to all lines. Survivors: `move_down`'s redundant
`visible_rows()` (behaviourally equivalent through the clamp), `_abbrev_count(1000)`,
`build_prompts_panel`'s `visible_cols` argument, and `r`/`x` setting `prompts.detail` - the last
three are written up above. No tautologies or trivially-true assertions found beyond the
line-count nit; the tests drive behaviour through `apply_key` and rendered output rather than
poking fields.

**Scope: matches the checklist, no phase-4 leakage.** Everything on the list is present
(`PROMPT_COLUMNS` + seven renderers, `build_prompts_table`/`build_prompts_panel`,
`_load_prompt_turns` via `_with_store`, `LOAD_PROMPTS`, the age line, `r`, `x`, `session_filter`,
the unavailable/busy hints), and both carried findings are fixed and tested (`clamp` paired with
every move, the body sized from `term_height`). Phase 4's pieces are all absent: no
`build_turn_panel`, no `OPEN_PROMPT`, no `action_label`, no `b`, and `Enter` still returns `NONE`
rather than setting `prompts.detail` with no renderer for it. The one addition beyond the plan's
written TUI shape is the `S` key (see the nit). The `NO_STORE_HINT` / `PENDING_INGEST_HINT` /
`STORE_BUSY_HINT` extraction from `_tokens_unavailable` is a real de-duplication, not creep.

**Auto-refresh: cannot fire under a modal, cannot hammer a locked store.** The gate is
`state.tab == "prompts" and not state.modal_open() and _prompts_refresh_due(...)`, and
`_prompts_refresh_due` reads `checked_at`, so a lock collision backs off the full 30s instead of
retrying every 300ms poll (mutation-verified). `state.search` is not counted by `modal_open()`,
but `/` is unreachable on this tab and the search modal owns `\t`, so `tab == "prompts"` with
`search` set is not reachable. A slow query cannot drop keystrokes (they stay in the tty buffer;
`select` picks them up next frame) and cannot corrupt `Live` (nothing writes during the query) -
the problem is the *number* of queries, not one of them (Major 1).

**Suite: 618 passed in 10.6s, tree left clean** (I mutated `claude_trail.py` during review and
restored it from a byte copy; `git status --short` and `git diff --stat` are both empty).
