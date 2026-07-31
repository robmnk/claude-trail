# Phase 4 review: TUI: prompt detail

Verdict: changes-requested
Diff: main...HEAD, 4 files, +1592/-109

## Blockers

none

## Major

**1. `claude_trail.py:3920` (`_cap_prompt_text`) - the truncation notice is put on the LAST
line, which is exactly the line the full-prompt view's crop eats. On real capped prompts the
cut is silent, and the test that claims to check it cannot fail.**

`build_prompt_panel` windows by LOGICAL line (`lines[win_start:win_end]`, capacity =
`term_height - PROMPT_CHROME_ROWS`) but the Panel crops by DISPLAY row, and prompt lines wrap.
The last window therefore renders fewer lines than it contains, and the notice is the first
casualty. `_clamp_prompt_scroll` caps the offset at `count - capacity`, so there is no key
sequence that scrolls past it.

Self-contained repro (no store):

```python
body   = "\n".join("word " * 30 for _ in range(30000))
capped = feed._cap_prompt_text(body)          # 100,067 chars, 665 lines
Console(width=120).print(feed.build_prompt_panel("s1", 40, capped, scroll=10**9))
# "more characters not shown" is NOT in the output
```

Measured: absent at (h,w) = (24,80), (24,120), (40,80), (40,120), (50,100); present only at
(60,200). **Taller terminals are worse**, because a bigger window holds more logical lines to
wrap. At h=40/w=120 I probed every offset in the last 200 lines: none renders it.

Reproduced on live data too - the store's longest main-thread turn (`150e3b73-44ff…`,
2026-07-03 12:10:59, 646,244 chars) caps to 100,063 chars / 1,158 lines and the notice is
unreachable at h=40/w=120 and h=60/w=200.

Not a corner case: **418 of the 3,716 turns `prompt_turns` can reach (11.2%) have
`full_len > 100_000`**, so more than one turn in ten drops text with nothing on screen saying
so. That breaks the constant's own stated contract at `:145-149` ("What is dropped is stated
in the text itself, never silently cut") and the plan's Detail-page bullet ("caps what it
materializes and says how much it dropped").

The wrap-crop itself is pre-existing (I confirmed `main`'s command modal loses the tail of any
wrapped prompt too), so this is not a regression - but the feature this phase added depends on
it and does not survive it.

`tests/test_claude_trail.py:4066` (`test_the_cut_is_visible_in_the_view_itself`) passes only
because it dodges the case: `_cap_prompt_text("z" * 300_000, cap=200)` is one 200-char line
plus the notice, which fits without wrapping at width 120. Mutating the notice away is caught
by the sibling string test, not by this one.

Fix: move the notice to the FRONT of the capped text (`f"… showing the first {cap:,} of
{len(text):,} characters\n\n" + text[:cap]`) or put it in the panel's range chip, where the
crop cannot reach it. Then re-point the test at a wrapping body, e.g.

```python
capped = feed._cap_prompt_text("\n".join("word " * 30 for _ in range(30000)))
out = _panel_text(feed.build_prompt_panel("s1", 40, capped, scroll=10**9), width=120)
assert "characters not shown" in out
```

(Verified: fails today, passes with the notice moved to the front.)

## Minor

- `claude_trail.py:135` (`TURN_CHROME_ROWS = 18`) - correct for the worst case, but it
  over-reserves **3 action rows on the majority of real turns**. `_preview_body` emits the
  `+N more lines` footer and its blank only when the prompt has more than
  `TURN_PROMPT_PREVIEW_LINES` (2) lines; below that the box is 3-4 rows instead of 6 while
  `_turn_capacity` still subtracts 18. Measured on the live store: **135 of the newest 200
  turns have a 1-line prompt and 151 have <= 2** (full text, not the preview). Rendered at
  h=24 that is 6 action rows plus 3 blank ones (a third of the usable list); confirmed on a
  real turn ("update the tracker", 419 actions, h=30 -> 12 rows shown, 3 blank). The comment
  at `:129-137` says "a short prompt loses the box's footer and its blank, and those two rows
  go unused" - that is 2 for a 2-line prompt but 3 for the 1-line case, which is 68% of turns.
  Fix: size the box from the actual content
  (`min(len(lines), TURN_PROMPT_PREVIEW_LINES) + (2 if hidden else 0) + 2`) and subtract that,
  or pad `_preview_body` to a fixed height. Consistent with `SESSION_CHROME_ROWS`'s trade, but
  the biggest such waste in the codebase.
- `claude_trail.py:4225` - the `OPEN_PROMPT` coalescing (`reload_turn = True`) is unguarded.
  Mutation-verified: replacing it with an inline
  `(state.prompts.actions, state.prompts.detail_text) = _load_turn_detail(state.prompts.detail)`
  in the drain loop leaves all 728 tests green. This is the fix for phase 3's Major 1, and it
  is the one thing in the phase with no test.
  `test_a_held_enter_is_not_self_limiting_which_is_why_main_coalesces`
  (`tests/test_claude_trail.py:3662`) proves the *premise* (opens alternate `OPEN_PROMPT` /
  `NONE`, so the key is not self-limiting) but pins nothing about `main()`. Exposure is real
  but milder than phase 3's: `_load_turn_detail` measured **44ms** on the live 1.2 GiB store
  (`turn_actions` is an indexed lookup, not a 400-turn clean), so a 10-press held Enter costs
  ~220ms uncoalesced, not the 3.2s `r` did. Fix: extract the drain's action dispatch into a
  pure `_drain_action(action, flags)` so the flag-vs-perform split is testable, or accept it
  and say so in the phase notes.
- `pyproject.toml:10` (`requires-python = ">=3.10"`) vs `claude_trail.py:2483`
  (`_turn_duration`) - the store's timestamps do not parse on the declared minimum, so the
  `ran 6m 12s` chip is dead there. Verified on the real interpreter
  (`~/.local/share/uv/python/cpython-3.10.20-linux-x86_64-gnu/bin/python3.10`):
  `datetime.fromisoformat('2026-07-29 19:18:37.084-03')` -> `ValueError: Invalid isoformat
  string` (a 2-digit UTC offset is 3.11+). `_turn_duration` catches it and returns None, so
  the row silently vanishes; `format_time` (pre-existing) returns `??:??:??` for every Time
  cell on the prompts list AND the action table. Inherited from phase 3, not introduced here.
  Fix in phase 7: bump `requires-python` to `>=3.11`, or normalize `-03` -> `-03:00` before
  parsing.
- `claude_trail.py:2434` (`_short_tool_name`) - collides three genuinely distinct tools.
  `mcp__plugin_linear_linear__authenticate`, `mcp__plugin_Notion_notion__authenticate` and
  `mcp__plugin_slack_slack__authenticate` all render `authenticate` (7 rows in the live store);
  the unshortened names would have stayed distinguishable at width 16
  (`mcp__plugin_lin…` / `…Not…` / `…sla…`). Two further collisions (`notion-fetch`,
  `notion-update-page`) are one tool under two server aliases, so harmless. 7 of 31,279 calls
  = 0.02%, and shortening is still the right default (18 of 77 distinct tool names would
  otherwise all read `mcp__plugin…`). Fix, if wanted: keep the server segment when it
  disambiguates, or leave it and note the limit in the docstring.

## Nits / follow-ups

- `tests/test_claude_trail.py:4183` - `assert len(out.splitlines()) == 24` is tautological:
  `build_turn_panel` passes `height=term_height` to the `Panel`, so I measured 24 lines at
  `TURN_CHROME_ROWS` 12, 18, 24 **and** 30. Same nit phase 3's review raised for
  `build_prompts_panel`. The real pin is `test_chrome_one_row_too_small_crops_the_hint`; say so
  in the docstring. Likewise `_turn_capacity(40) == 40 - feed.TURN_CHROME_ROWS` (`:4195`)
  restates the implementation - the floor assert on the next line is the meaningful half.
- `claude_trail.py:4045` vs `:3466` - `render_panel` renders `detail_entry` and `db_info`
  BEFORE `prompts.detail`, while `_apply_modal_key` dispatches `prompts.detail` FIRST. Both
  orders are correct in isolation and unreachable together (BFS over every 3-key sequence from
  both tabs found zero states with two modal kinds set), but if a future path ever sets both,
  keys go to the turn while the screen shows the command modal. One comment, or reorder
  `render_panel` to match.
- `claude_trail.py:4293` - `session_ids.add(state.prompts.detail.session_id)` is unguarded;
  deleting it leaves all 728 tests green (the gutter would then show bare short ids for a
  session absent from the feed). Same untested-`main()` class as the Minor above.
- Agent DESCRIPTIONS can lag one `AGENT_LABEL_CACHE_TTL` (2s) on first open, and the
  disclosure is accurate rather than papered over: `load_agent_labels` returns the whole cache
  unconditionally inside the TTL regardless of whether `session_ids` grew, so the newly added
  session is not scanned until it expires. Glyphs and short ids are right immediately, and
  `Live`'s 4 Hz thread redraws, so it self-heals in <= ~2.3s. Acceptable.
- `claude_trail.py:145-149` - the cap's rationale ("an uncapped one would chew through half a
  megabyte four times a second") overstates the cost: rendering the real uncapped 646k-char
  prompt measured **5.1 ms/frame** vs 2.9 ms capped. The cap is still worth having (memory,
  and it bounds the worst case); the comment should quote what was measured.
- `claude_trail.py:3790` (`_load_command_detail`) is NOT capped, so the command modal keeps the
  exact exposure the new path was capped for - `prompt_for_command` returned 646,244 chars for
  that same turn. Asymmetry, not a defect; worth one line either way.
- `turn_actions`'s `LIMIT 500` is not disclosed on the page. A synthetic 640-call turn renders
  `Work  640 tool calls` over `Actions (500)` with nothing saying the list was capped.
  Unreachable today (max 419 across the newest 400 turns), and the page already has
  `turn.tools` to compare against if it ever matters.
- `build_turn_panel:2656` dereferences `prompts.detail` with no guard (`TypeError` if ever
  called with `detail=None`). The contract is documented and every caller honours it.
- The gutter-label deviation (once per run, vs the mock's repeated id) is the right call: it
  matches `_render_agent`, and because runs are computed over the WINDOWED rows the label is
  never scrolled off. The one cost is that the `┌` at the top of a window claims a run start
  that may have happened earlier - disclosed in the docstring, visible in the live render at
  `detail_scroll=60`.

## Notes

**Scope: exact.** Every checklist item is present (`build_turn_panel`, `OPEN_PROMPT`,
`action_label`, the agent gutter over actions, `b`, the scroll clamp, `Enter` into the
refactored `build_prompt_panel`, the `Esc`/`q` depth rules) and the carried phase-2 finding is
honoured - the renderer lands in the same change that first sets `prompts.detail`, and
`test_the_turn_modal_has_a_panel_to_render` pins the other half of that guard. No store
**source** change (`claude_trail_store.py` is untouched; `tests/test_store.py` gains one
integration test). Nothing from phase 5/6/7 leaked in. `_short_tool_name`, `_cap_prompt_text`,
`_action_empty_state` and `TURN_UNREADABLE_HINT` are not named in the plan but each is required
by a plan bullet. `claude_trail.py` is clean under `ruff` before and after; the 24 repo-wide
findings are all pre-existing and in other files.

**Every extraction is provably behaviour-preserving for the Commands tab. I diffed rendered
output between `main` and HEAD rather than reading the code.**

| extraction | matrix | diffs |
|---|---|---|
| `_agent_gutter` out of `_render_agent` | `build_table` over 11 entry sets (main-only, single/multi-row runs, two adjacent subagents, unknown agent, agent with no description, same agent id across two sessions, empty feed, markup command, danger row) x 3 widths x 2 cursors x 3 column sets | **0** |
| same, panel level | `build_panel` x 11 sets x 2 widths x 2 heights | **0** |
| `_text_lines`/`_prompt_box`/`_preview_body`, `_prompt_preview` deleted | `build_detail_panel` x 8 entries x 12 prompt shapes (none, empty text, 3/10/11/40 lines, no ts, missing ts key, the `unavailable` sentinel, markup, missing text key, `text=None`) x 2 widths x 2 heights x 3 token shapes = 1,152 renders | **0** |
| `build_prompt_panel(LogEntry) -> (session_id, ts, agent_id, text)` | 8 entries x 10 prompts x 2 widths x 2 heights x 3 scrolls, calling HEAD **exactly as `main()` does** (`entry.get("session_id","")`, `(detail_prompt or {}).get("text")`, `entry_agent_id(entry)`) | **0** |

The sentinel case is preserved: `main`'s `_prompt_lines` returned `[]` for
`{"unavailable": ...}`, HEAD's `.get("text")` is None -> `_text_lines(None)` -> `[]`.

**Rich markup: clean.** I probed every new cell and panel with `[/tmp/x]` (crash),
`[bold]` (deletion), `[[wiki]]`, `list[int]`, `- [x] done` and `[/]`: the turn page's project /
session-name / prompt-text / preview-fallback, all four action cells (time, gutter, tool,
detail), the agent description, the agent id, the tool name, the `tool_input` fallback, the
bash-filter empty state, the capped-text path, and the refactored full-prompt view's text / ts
/ name / agent / session id. Zero crashes, zero deletions. The whole-row sweep
(`out.count(probe) == 3` at width 400) holds for both probes. Driven through a real
`Live.update` for four panel shapes x two probes: no `MarkupError`. The two apparent
"deletions" my probe reported are truncation, not markup: `session_label` cuts a sid to 8 chars
and `short_agent_id` to 4, and the surviving prefixes (`[/tmp/x]`, `[/tm`, `[bol`) are all
present.

**The Work-row deviation from the plan's mock is right, and the plan is wrong on its own
example.** `prompt_turns`'s `bash` is `count(*) FILTER (WHERE a.tool_name = 'Bash')` over the
whole window with no `agent_id` predicate (`claude_trail_store.py:2046`), so it counts subagent
Bash too. Measured on the plan's own documented turn (`29dea45c…`, 2026-07-27 10:30:38): 73
calls, 41 across 3 subagents, 31 Bash - of which **3 were the main thread's and 28 the
subagents'**. The mock's `32 main (31 Bash)` would credit the main thread with 31 Bash calls it
did not make. Worse on the largest live turn (`29c24a5a…`, 419 calls / 364 subcalls / 5
subagents): 208 Bash split **39 main / 169 subagent**. The rendered
`419 tool calls: 55 main, 364 across 5 subagents  ·  208 Bash` is the honest statement, and
`agents` is `count(DISTINCT agent_id)` (NULLs excluded), so the subagent count is right too.
**Phase 7 must fix the mock.** If the mock's shape is actually wanted, it needs a store change
(`count(*) FILTER (WHERE tool_name='Bash' AND agent_id IS NULL) AS main_bash`) - that is the
one thing in this phase that arguably belonged in the store, and deferring it was correct.

**`action_label`'s coverage claims are exact.** Against the live store (1.2 GiB copy, read-only):
31,279 `tool_use` rows, 2,820 with `text` NULL, **1,936 labelled by `_ACTION_FIELDS` (68.7%)**,
**30,395 labelled overall (97.2%)** - the docstring says 1,935 / 68.7% / 97%. The unlabelled
tail is what it says it is (`AskUserQuestion` 136, `mcp__notion__notion-fetch` 124, `TaskUpdate`
80, `Workflow` 61, …), all rendering the dim `-`. `isinstance(value, str)` keeps containers out
(mutating it to `if value is not None` is caught). Longest label produced is 9,035 chars, which
`normalize_cmd` + `no_wrap`/`ellipsis` handle; a 22-row window of the longest labels in a real
turn renders in 26 ms vs 10 ms typical vs 10.8 ms for the existing feed panel, so the worst case
is bounded.

**Store discipline: clean.** `_load_turn_detail` is the only new store caller, goes through the
unchanged `_with_store` (`try/finally con.close()`), and is coalesced out of the drain. Measured
end to end against the live-store copy: **one** `store.connect(read_only=True)`, 44 ms, and a
re-open immediately afterwards succeeds (proving it was closed). The `or (None, None)` guard is
safe - a successful `([], None)` is a truthy 2-tuple, so only `_with_store`'s `None` failure
sentinel triggers it, and the None-vs-`[]` distinction the page depends on survives
(mutation-verified). `prompts.detail` is assigned in exactly one place (`:3587`) and that branch
returns `OPEN_PROMPT`, so the coalescing covers every path. **`duckdb` and `claude_trail_store`
are still absent from `sys.modules` after `import claude_trail` and after a real
`hook_main()` run.**

**Geometry: `TURN_CHROME_ROWS = 18` is exactly right for the case it was sized for, pinned from
both sides by rendering.** At h=24/w=90 with a 30-line prompt the hint lands on line 21 and line
22 is the Panel's own bottom padding - zero waste. At 17 the hint is cropped at every width; at
19 one row goes blank; at 20, two. Body rows scale cleanly (h=24 -> 6, h=30 -> 12, h=40 -> 22)
and the hint survives from 76 columns up at all three heights, matching the comment. The
short-prompt case is where it costs (Minor 1).

**Depth, purity and state isolation: no leaks found.** BFS over every key sequence up to depth 3
from a prompts-tab state (22 keys, with `main()`'s load simulated between presses) produced
**zero** states with two modal kinds open. `Esc` steps back exactly one level (prompt -> turn ->
list, with `rows` and `tab` intact), `q` closes everything from either depth, `Ctrl-C` quits
from either. Opening turn B after scrolling, filtering and drilling into turn A resets
`bash_only`, `detail_scroll`, `prompt_scroll`, `prompt_open`, `actions` and `detail_text` -
driven through `apply_key`, not by poking fields. The clamp holds under all four asked-for
cases: 0 actions -> 0, `G` then a shrink to 2 -> 0, 40 actions `G` -> 34, negative -> 0, and
`b` at the bottom of a 31-row list re-anchors to 0 with the single Bash row rendered.

**Test quality: strong. 34 mutations run against a sandbox copy of the repo (the tree was never
modified); 32 caught, 2 survived.** Caught, among others: adding instead of subtracting in the
Work row, dropping the Bash callout, three separate gutter-run mutations, `b` not re-anchoring
the scroll, `close_detail` forgetting `bash_only`, `Enter` skipping `close_detail`, the loader
flattening None to `[]`, the cap becoming a no-op, the cap dropping its message, `action_label`
ignoring `tool_input` / preferring it over `text` / accepting containers, `_short_tool_name`
keeping the prefix, `is_error` never marking a failure, the danger marker dropped, the empty
state conflating unreadable with empty, a negative duration, a confident `0` instead of the
pending hint, a clamp off-by-one, `visible_actions` ignoring the filter, `Enter` never drilling
in, `esc` closing everything from the prompt view, `Enter` returning `NONE`, the preview
fallback removed, `can_open` forced True, the window ignoring the scroll, `_apply_modal_key` not
routing to the turn handler, `_agent_gutter` dropping the label, `TURN_PROMPT_PREVIEW_LINES`
bumped, and `_turn_capacity` losing its floor. **Both survivors are in `main()`** (the
`OPEN_PROMPT` coalescing and the `session_ids.add`), consistent with `main()` being untested
throughout this codebase; both are written up above. Beyond those I found one test that passes
for the wrong reason (Major 1) and two tautological assertions (nit). The new store-backed test
(`test_turn_page_renders_what_the_real_queries_return`) is the right shape: it drives the TUI
renderers off actual `prompt_turns`/`turn_actions` output rather than hand-built records.

**The manual verification was not weakened by the substitute turn - but the stated reason is
wrong.** `29dea45c…`'s 2026-07-27 10:30:38 turn has NOT aged out: it is still in the DEFAULT
`prompt_turns(con)` list (limit=200) on the current store, and trivially reachable with
`session_id=`. I opened it end to end and it renders correctly with exactly the documented
numbers (73 calls, 3 subagents, 41 subagent calls, `ran 10m 09s`, 52,198 output / 16,591 by
subagents / 49 requests) - and, as noted above, it is itself a counter-example to the plan's
mock. The `cp` of the store to dodge the ingest lock is legitimate (I hit the same
`Conflicting lock is held` on a read-only open and did the same); the substitute turn was a
strictly stronger case. Correct the note; the conclusion stands.

**Suite: 728 passed in 12.4s with `.venv/bin/python` (643 on `main`, so +85).** The plan's
verify-command fix (`d6ca77e`) is correct and worth having: `python3 -m pytest` is a pyenv shim
onto system 3.14 with no duckdb and reports 643 passed / 85 failed, which reads exactly like
this phase's own test count. Working tree left clean - every mutation ran in
`/tmp/.../scratchpad/mut`, never in the repo.
