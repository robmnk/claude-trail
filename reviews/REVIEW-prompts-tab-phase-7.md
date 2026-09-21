# Phase 7 review: Docs + release (prompts tab)

Verdict: changes-requested
Diff: main...HEAD, 6 files, +322/-47 (`861cef9` impl, `764e3a3` plan-only)

## Blockers

- `claude_trail.py:1282-1302` - `parse_ts` closes only part of the 3.10 gap it
  claims to close. DuckDB trims trailing zeros off the fraction, so the store
  emits 1- and 2-digit fractions as well as 3, and Python 3.10's
  `fromisoformat` rejects any fraction that is not exactly 3 or 6 digits
  (`_parse_hh_mm_ss_ff`: `if len_remainder not in (3, 6): raise`). Measured on a
  copy of the live store, over all 172,327 `events` rows: 155,029 have 3 digits,
  **15,579 have 2, 1,568 have 1**, 151 have none. So ~9.9% of store timestamps
  still render `??:??:??` on the declared floor, and a turn page loses its
  duration chip whenever either end falls in that 10%. Verified on
  `~/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10`:

  ```
  OK    2026-07-29 19:18:37.084-03
  FAIL  2026-07-29 19:18:37.08-03  -> ValueError Invalid isoformat string
  FAIL  2026-07-29 19:18:37.5-03   -> ValueError Invalid isoformat string
  ```

  Affected call sites are exactly the ones the phase routed: `claude_trail.py:468`
  (prompts list Time), `:2666` (action table Time), `:2732` (turn page `sent`),
  `:2520` (`_turn_duration`). The hook feed is unaffected (it writes
  `timespec="seconds"`).

  Fix: pad the fraction before widening the offset, in the same function.

  ```python
  _SHORT_FRACTION_RE = re.compile(r"\.(\d{1,6})(?=[+-Zz]|$)")

  def parse_ts(ts: str) -> datetime:
      ts = _SHORT_FRACTION_RE.sub(lambda m: "." + m.group(1).ljust(6, "0"), ts)
      return datetime.fromisoformat(_SHORT_TZ_OFFSET_RE.sub(r"\1\2:00", ts))
  ```

  Then add the two shapes to `TestParseTs`
  (`tests/test_claude_trail.py:151-192`) asserting the right instant (`.08` is
  80 ms, `.5` is 500 ms), since the current six cases only cover 3-digit and
  no-fraction. Until it is fixed, three places assert the opposite and must not
  ship as written: `CLAUDE.md:291`, the `parse_ts` docstring
  (`claude_trail.py:1293-1300`) and the `TestParseTs` docstring
  (`tests/test_claude_trail.py:152-157`).

## Major

- `README.md:196-212` - the worked example is not what the command prints for
  that turn, in two ways a reader would copy:
  - `README.md:199` list row prints `52198` in the `tokens` cell while
    `README.md:206` says the same turn spent 16591 on subagents.
    `_turn_list_cells` (`claude_trail_store.py:2679-2681`) always appends
    `(+N sub)` when `subagent_output_tokens` is non-zero, so the real cell is
    `52198 (+16591 sub)`. As printed, the example teaches that the list's tokens
    column excludes subagent spend.
  - `README.md:208` shows `prompt    ... two main views …`. `_print_turn_prompt`
    (`claude_trail_store.py:2743-2765`) never truncates a line: it prints up to
    `PROMPT_CLI_PROMPT_LINES` (10) preview lines and states both cuts as
    `[+N more lines, M more chars]`. The `…` there is a shape the dump never
    produces. (In the list cell at `:199` the `…` is right - that path does cut,
    at `PROMPT_CLI_PREVIEW_CHARS` = 100.)

  Fix: regenerate the block from a turn whose real text can be published (the
  numbers, session id and event id in it are real; only the project path, the
  prompt and the two action rows were replaced), or keep the substitution and
  make it self-consistent: `52198 (+16591 sub)` in the list row, and a prompt
  line with no `…` plus the `[+N more chars]` note if the preview was cut.

## Minor

- `README.md:14` - "**Minimal footprint** - single Python file, one dependency
  (`rich`)" is false since the store shipped: two modules
  (`claude_trail.py`, `claude_trail_store.py`) and two deps (`pyproject.toml:11`:
  `rich>=13.0`, `duckdb>=1.0`). Pre-existing, but this is the docs+release phase
  and the README now sells a store-backed tab on the strength of that line.
  Fix: "two Python files, two dependencies (`rich`, `duckdb`)".
- `README.md:20-22` - "How It Works" still describes only the hook -> log -> TUI
  path. A reader of the new Prompts tab section has no sentence telling them the
  second data path (transcripts -> `ingest` -> DuckDB -> Prompts tab) exists
  until the CLI section, 160 lines later. One bullet closes it.
- `tests/test_claude_trail.py:194-215` - the `.env.example` pin is weaker than
  CLAUDE.md:29 claims ("pins against every `_env_int` call site"). It asserts
  `f"{key}={default}" in example`, a substring match, so a code default of `8`
  passes against a file value of `80` (verified). It also only scrapes literal
  ints (`_env_int("X", DEFAULT)` would be skipped silently, since `assert pairs`
  only checks the list is non-empty overall) and never checks the reverse
  direction, so a key deleted from the code but left in `.env.example` is not
  caught. Fix: match per line (`re.search(rf"^{key}={default}$", example,
  re.M)`), and assert the set of `CLAUDE_TRAIL_*` keys in the file equals the
  scraped set.
- `CLAUDE.md:289` - "the TUI keeps same-name wrappers": `action_label` is
  same-name (`claude_trail.py:2445`), `short_tool_name` is not - the wrapper is
  `_short_tool_name` (`claude_trail.py:2458`). Fix: "same-name (`action_label`)
  or underscored (`_short_tool_name`) wrappers".
- `.env.example:19` - "The Prompts tab (tab) has its own columns" reads as a
  typo; it means the `Tab` key. Fix: "The Prompts tab (`Tab`)".

## Nits / follow-ups

- `plans/2026-07-29-prompts-tab.md:386` - phase 7 is still `- [ ]`. The
  conductor ticks it at merge.
- `README.md:198-209` - the example keeps a real session UUID and a real event
  UUID while the body around them is invented. If it stays synthetic, make the
  ids synthetic too so nothing in the block is half-real.
- `CLAUDE.md:291` - "`Z` and a full `+HH:MM` pass through untouched" is literally
  true but reads as "both work". On 3.10 a `Z` string still raises; the
  `parse_ts` docstring says so explicitly, CLAUDE.md does not. No store or hook
  path feeds `Z` to `parse_ts` today (`_parse_snapshot_ts`,
  `claude_trail_store.py:1107`, is the only `Z` reader and it is not routed
  through `parse_ts` - correctly, it is a validation, not a render).
- The phase 4 carry-over about `cmd_search`/`cmd_tokens`/`cmd_cat`/`cmd_diff`
  tracebacking on a missing store, and the raw `ModuleNotFoundError` from the
  lazy `duckdb` import, are recorded in the plan as follow-ups and are correctly
  untouched here.

## Notes

**Scope.** The diff is the docs phase plus the one deliberate code deviation
(`parse_ts` instead of bumping `requires-python`), and `pyproject.toml` is
untouched as briefed. No scope creep: the only non-doc edits are `parse_ts`, its
three call sites, and two tests.

**What I verified, and how.** Suite: `.venv/bin/python -m pytest tests/ -q` ->
789 passed. Ruff: `uvx --from 'ruff>=0.15,<0.16' ruff check claude_trail.py
claude_trail_store.py` -> 18, baseline unchanged. Dashes:
`git diff -U0 main...HEAD | grep -P '^\+.*[\x{2013}\x{2014}]'` -> nothing, and no
en/em dash anywhere in README.md, CLAUDE.md, `.env.example` or the plan.

Docs against code, claim by claim:

- Every key in CLAUDE.md's three Controls tables traced to its branch:
  `Tab`/digits/`Enter`/`s`/`o`/`t`/`d`/`q` in `apply_key`
  (`claude_trail.py:3686-3774`), `r`/`x`/`S`/`Enter`/digits/`s`/`o`/`t`/`d` in
  `_apply_prompts_key` (`:3584-3672`), `b`/scroll/`Enter`/`Esc`/`q` and the
  nested prompt view in `_apply_turn_key` (`:3468-3519`). All accurate,
  including the two subtle ones: `S` really does re-query (returns
  `LOAD_PROMPTS`, and `_raw_turn_filter`, `claude_trail_store.py:2289-2310`,
  puts the session bound INSIDE the scan), and `Enter` on a turn with no loaded
  text closes the page instead of drilling in (`:3497`).
- Constants: `PROMPTS_AUTO_REFRESH` 30.0, `PROMPT_TEXT_CHAR_CAP` 100_000,
  `TURN_PROMPT_PREVIEW_LINES` 2, `PROMPT_PREVIEW_LINES` 10,
  `AGENT_ID_LABEL_LEN` 4, `TURN_ACTION_LIMIT` 500, `PROMPT_TURN_RAW_SCAN` 400,
  `PROMPT_TURN_LIMIT` 200, `SESSION_CWD_SCAN_LINES` 50, `PROMPT_CLI_LIMIT` 30 -
  all match the docs.
- Signatures: `prompt_turns(con, limit=200, session_id=None, raw_scan=400,
  since=None)`, `turn_actions(con, session_id, ts, next_ts, limit=500)`,
  `turn_prompt_text(con, event_id)`, `find_turn(con, session_id=None, at=None,
  event_id=None)`, `action_label`, `short_tool_name`, `_turn_project_path`,
  `_short_project_path`, `_turn_time`, `backfill_session_cwd` all exist as
  named. The `short_tool_name` docstring does record the three-`authenticate`
  collision, 7 of 31,279, exactly as CLAUDE.md:289 says.
- `.env.example`: 11 keys, matching all 11 `_env_int` call sites including
  `DIR_LABEL_WIDTH` (14) and `PROJECT_LABEL_WIDTH` (16), with the code's
  defaults. The Tokens comment's `24.2k +3.1k sub` matches
  `_render_turn_tokens` (`claude_trail.py:526-539`) exactly.
- CLI, run against a COPY of the live store (`$CLAUDE_JOB_DIR/tmp/store`, never
  the live file, never `reset-db`): `--at` without `--session` -> 2; `--event-id`
  with `--session` -> 2; `--at` with `--event-id` -> 2; `--at` with `--limit` or
  `--since` -> 2; `--sesion` (unknown flag) -> 2; ambiguous 1-char prefix -> 2
  naming candidates; unresolved turn -> 1; missing store -> 1; empty list -> 0.
  `--at` resolves both a full-precision `ts` from `--json` and a
  second-truncated one from the list. `--json` values are str/int/None on the
  list and add `bool` (`is_error`) on a turn. Control characters are stripped in
  the text path (`_plain_text`) and escaped in `--json`. `_list_turns` widens the
  scan exactly as documented.
- Store-side docs: `_upsert_session` does use `coalesce(sessions.cwd,
  excluded.cwd)` (`claude_trail_store.py:519`); `backfill_session_cwd` runs
  after the sweep and before the file-history pass (`:1614`), logs
  `sessions: N cwd backfilled` (`:1633`), and is skipped under
  `--file-history-only`; `search --project` is now
  `project LIKE ? OR cwd LIKE ?` (`:1790`). The phase 5 plan note the plan-only
  commit corrected is now right.

**Plan sub-bullets.** Phase 4: 3.10 floor **partial** (see the blocker); detail
mock **done** - I re-rendered `build_turn_panel` at width 76 with the mock's own
numbers and the plan block matches it line for line, including
`Actions (N)  1-5 of N` and the footer
`esc / q : back    ↵ : full prompt    j/k g/G : scroll    b : bash only`;
`_short_tool_name` collision **done**. Phase 5: cwd semantics, upgrade path,
`search --project` **done**; the "honest expectation" is **partial** - CLAUDE.md
carries the 117-of-1,703 number but not the plan's point that no rendered
project changes today, which is machine-specific and fine to leave out. Phase 6:
subcommand lines, `prune-raw`, where `action_label`/`short_tool_name` live, the
`prompts` prose bullet **done**; the plan's Verify example still reproduces -
`prompts --session 29dea45c --at "2026-07-27 10:30:38"` on the store copy prints
`73 tool calls: 32 main, 41 across 3 subagents, 31 bash`.

**README quality.** The new sections are user-facing and leak no internals (no
function names, no measured store statistics). The two problems are the worked
example (Major) and the two stale lines from before the store existed (Minor).

**Release readiness.** With the version bump reserved to the user, what is left
to ship: bump `pyproject.toml:6` (0.6.9 -> 0.7.0; this is a feature release and
the panel title reads the INSTALLED package metadata, which is 0.6.6 here, so
reinstall after bumping), and fix the blocker before shipping a release that
declares `requires-python = ">=3.10"`. Nothing else is outstanding: the repo has
no CHANGELOG and no git tags, `requirements.txt` matches `pyproject`
dependencies, and the README install/usage lines are unchanged and still
correct.
