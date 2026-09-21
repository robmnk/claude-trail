# Phase 6 review: CLI `prompts` subcommand + dispatch wiring, with tests

Verdict: changes-requested
Diff: main...HEAD (`c6554dd`), 4 files, +938/-57

## Blockers

none

## Major

- `claude_trail_store.py:2303` - `--at` breaks its own "at or before" rule when the bound
  carries sub-second precision, and silently returns a **later** turn. The predicate is
  `date_trunc('second', ts) <= try_cast(? AS TIMESTAMPTZ)`: the turn's ts is truncated but
  the bound is not, so a turn at `21:13:45.777` matches a bound of `21:13:45.671`.
  `prompts --json` prints `ts` at full precision, and `_turn_time`'s docstring
  (`claude_trail_store.py:2477`) advertises exactly that chaining ("copy a list row's time,
  paste it back"), so this is a wrong answer on the documented workflow. Measured on a copy
  of the live store: feeding each row's own `ts` back through `find_turn` resolves to a
  different turn for **9 of the newest 400** turns store-wide (2.25%) and **13 of 200** in
  the longest session (6.5%) - every observed case is the `/compact` pair (the typed
  `/compact ...` turn and the `<command-name>/compact</command-name>` block, ~100ms apart),
  where the earlier row's real window is ~100ms and the one returned is the open-ended one.
  Fix: truncate only when the bound itself is whole-second, e.g. decide in `find_turn`
  (the string is already in hand) between
  `ts <= try_cast(? AS TIMESTAMPTZ)` and the current truncated form, or bind both:
  `ts <= ?::TIMESTAMPTZ OR (date_trunc('second', ts) <= ?::TIMESTAMPTZ AND ?::TIMESTAMPTZ = date_trunc('second', ?::TIMESTAMPTZ))`.
  Add a test with two turns in the same second: the exact ts of the earlier one must resolve
  to the earlier one. (The remaining second-precision ambiguity - two rows printing the same
  `HH:MM:SS` - is inherent and fine, but `find_turn`'s docstring should name it and point at
  `--event-id`, which it currently does not.)

## Minor

- `claude_trail_store.py:2448-2460` (`_tui_text_helpers`) - the reflection import is correct
  under every entry point I tested (see Notes), but it inverts the layering CLAUDE.md states
  ("`claude_trail_store.py` ... is independent of the live TUI") and reaches for a private
  name of another module at `claude_trail_store.py:2651` (`tui._short_tool_name`). The
  codebase's own precedent runs the other way: `slash_label` is a pure text helper that lives
  in the store and the TUI imports it lazily (`_slash_label`) - and this CLI calls
  `slash_label` directly, proving the shape works. Concrete follow-up for phase 7 (not this
  phase): move `action_label` + `_ACTION_FIELDS` and `_short_tool_name` down into the store
  (both self-contained, ~40 lines) and have the TUI import them lazily. `short_path` and
  `short_agent_id` should stay put: their defaults come from `_env_int`/`DIR_LABEL_WIDTH`,
  so moving them drags the `.env` width machinery down too; the CLI can pass an explicit
  width and slice the id itself. If the move is deferred, at least rename `_short_tool_name`
  to a public `short_tool_name` so the cross-module call is not reaching into a private.
- `claude_trail_store.py:2434` - `--at` + `--event-id` is refused, but `--session` +
  `--event-id` is not: the session is silently overridden by the one `find_turn` reads off
  the event (`claude_trail_store.py:2299`). Verified: `prompts --session bad20b44 --event-id
  <id from 42a3efa3>` dumps the 42a3efa3 turn with no warning. Either refuse the pair or say
  in the usage line that `--event-id` ignores `--session`.
- `claude_trail_store.py:2690-2727` - `--since` and `--limit` are validated and then silently
  dropped on the `--at`/`--event-id` path. One line in the usage block, or a refusal, would
  keep a scripted caller honest.
- `claude_trail_store.py:2727` - `raw_scan=max(PROMPT_TURN_RAW_SCAN, opts["limit"] * 2)` does
  not do what its comment claims. Measured on the store copy: `--limit 500` scans 1000 raw
  turns and returns **466** rows, because ~53% of raw user turns are noise. The x2 factor is
  a guess that is already short at the first limit that exercises it. Either say so in the
  comment ("a big `--limit` can still come back short; noise turns are dropped after the
  scan") or scan until the limit is filled. This mutation **survived** the suite (see Notes),
  so nothing pins it either way.
- `claude_trail_store.py:2718` and `:2729` - `default=str` in both `json.dumps` calls is
  unreachable: every value `_turn_rows` and `turn_actions` return is already `str`/`int`/
  `bool`/`None` (both timestamps are `::VARCHAR`). Dropping it left the suite green. Either
  delete it or add a row that needs it; as written it is a guard nobody can trip and nobody
  can verify.
- `claude_trail_store.py:2652-2656` - a zero-call turn prints the `time agent tool detail`
  header and then `(no tool calls in this turn)`. Skip the header when `actions` is empty.
- `claude_trail_store.py:2534` (`_print_columns`) - prompt text goes to stdout verbatim, so a
  prompt containing ANSI escapes or a BEL is emitted raw. Reproduced: a turn whose text is
  `\x1b[31mRED\x1b[0m and a \x07bell` prints the live escapes. The TUI is safe here because
  Rich escapes for it. Not new to this CLI as a class (`cat` prints file bytes), but this is
  the first surface that prints *prompt* text, which is the most likely place to find pasted
  terminal output. A `str.translate` of C0 controls in `_turn_preview_line` would close it.

## Nits / follow-ups

- Two different ellipses in one list row: `short_path` emits `…` (U+2026) for the Project
  cell while `_turn_preview_line` (`claude_trail_store.py:2498`) emits ASCII `...`. Pick one
  for a plain-text CLI.
- `_print_columns` pads with `str.ljust`, i.e. by code points. A CJK or emoji project/cwd
  would misalign the columns. Only the Project cell can contain one (the Prompt cell is last
  and never padded), so this is cosmetic.
- `find_turn`'s docstring (`claude_trail_store.py:2271`) says an event_id that "LOST the
  same-timestamp collapse" resolves to nothing - accurate, since the collapse keeps
  `arg_max(event_id, (length(clean), event_id))`, but worth one clause naming the tie-break
  (longest text, then highest event_id) so a reader does not go looking for `min(event_id)`
  from the plan's sketch.
- Phase 7 docs debt, checked against the tree: CLAUDE.md's `Subcommands` code block
  (`CLAUDE.md:232-240`) needs a `prompts [--session S] [--limit N] [--since ISO] [--at ISO]
  [--event-id E] [--json]` line, plus a prose bullet next to the `search`/`tokens` ones; the
  same block is still missing `prune-raw` (pre-existing). README's block (`README.md:141-149`)
  needs the same line and a worked example. Nothing in either doc enumerates the lazy-import
  tuple, so there is no "nine-name tuple" to update - only the two subcommand lists.
- One item on the implementer's own phase 7 list is wrong and should not be "fixed": the
  plan's named example turn is **not** stale. `prompts --session 29dea45c --at "2026-07-27
  10:30:38"` on a copy of the live store prints `work 73 tool calls: 32 main, 41 across 3
  subagents, 31 bash` - exactly the plan's numbers (`plans/2026-07-29-prompts-tab.md:435`).
  The phase 4 review's separate point about the detail-page *mock* (`32 main (31 Bash)` is
  arithmetically impossible because `bash` is window-wide) still stands and is unrelated.
- Also stale in the plan, carried from the phase 5 review
  (`plans/2026-07-29-prompts-tab.md:410-412`): "CLAUDE.md's Subcommands section does not spell out
  `--project` today" - it does, at `CLAUDE.md:234`.

## Notes

**Scope.** Exactly the phase 6 spec, nothing more. `claude_trail.py` is +3/-1: `"prompts"`
added to `main()`'s store-subcommand tuple, nothing else. The new store surface
(`_turn_rows`, `find_turn`, the `since` kwarg, the `cmd_prompts` block) is what `--at` /
`--event-id` / `--since` require. `_resolve_session_prefix` and `_castable_ts` are beyond the
literal spec but are parity with `search`/`tokens` and are small; I would not cut them.

**`prompt_turns` is unchanged for the TUI (the bar the conductor set, met).**
- Generated SQL text, captured from both revisions through a fake connection: identical
  except the `ORDER BY ts DESC LIMIT 400` moving onto the `WHERE` line. Bind params
  identical. The implementer's "byte-identical" is really "whitespace-differs, token-for-token
  identical".
- Results on a copy of the live 1.6 GB store (`/home/naka/.claude/jobs/825d48d0/tmp/trail-copy.duckdb`):
  `prompt_turns(limit=200)`, `prompt_turns(limit=200, session_id=...)` and
  `prompt_turns(limit=5)` serialize to the **same SHA-256** on main and on HEAD
  (`21e7661c...`, 200 / 11 / 5 rows). Timing unchanged (0.28s vs 0.30s cold).
- `_load_prompt_turns` (`claude_trail.py:3947-3950`) calls `prompt_turns(con)` /
  `prompt_turns(con, session_id=...)` and never passes `since`, so the TUI takes the
  identical path.

**`since` and suffix closure (verified, correct).** `ts >= since` sits inside `raw`, so the
kept set stays suffix-closed per session: any kept turn's next prompt is newer and therefore
also kept. Measured on the longest session (441 raw turns), bounding at the 51st-newest turn:
51 rows returned, **0 aggregate differences** against the unbounded list for every shared
event_id, and the oldest kept turn has `next_ts` **not** NULL (`2026-09-16 19:34:24.024-03`,
13 tool calls, same 13 as unbounded). The window does not become open-ended and does not
swallow the previous turn's calls. `try_cast` makes an unreadable `--since` match nothing,
and `cmd_prompts` rejects it before that with exit 2.

**`find_turn` agrees with the list.** For every one of the 200 turns of the longest session,
`find_turn(event_id=...)` returns a dict **equal** to the `prompt_turns` row, aggregates
included. Uncapped scan cost on that session: 113ms for `--at`, 82ms for `--event-id`,
against 84ms for the session-scoped list. No reason to cap it.
Other cases checked on real data: a time inside a turn's window steps back to that turn; a
time before the session's first prompt returns None (exit 1); a subagent `user` event id
returns None; an ambiguous prefix (`--session b`, 114 matches) is exit 2 with a sample; an
unknown prefix is an empty list at exit 0. The `--at` **printed-second** case is the Major
above.

**Dispatch and store discipline (all verified).**
- `import claude_trail` leaves both `duckdb` and `claude_trail_store` out of `sys.modules`;
  so does a full `hook_main()` run.
- `python3 claude_trail.py prompts` resolves the helpers to `__main__` - instrumented, no
  second execution of `claude_trail.py`, no separate `claude_trail` import, no Console.
- The installed console script (`.venv/bin/claude-trail prompts`) works and resolves
  `claude_trail` from `sys.modules`.
- A bare `import claude_trail_store` in a fresh interpreter takes the `import claude_trail`
  fallback successfully (it pulls `rich` in with it). That branch is **not** covered by the
  suite: `tests/test_store.py` imports `claude_trail` at module level, so `sys.modules`
  always has it by the time a `cmd_prompts` test runs.
- `cli_main`'s lock-conflict guard wraps `cmd_prompts` like every other read command, and
  `test_cli_main_dispatches_prompts` pins it.
- Read-only: running `prompts` against the store copy left mtime, size and inode unchanged
  and created no `.wal`. A missing store exits 1 with a message and creates **nothing** -
  not even the parent directory (`connect`'s `mkdir` is never reached).
- Exit codes as specified: 0 for a list (including an empty one), 1 for a missing store and
  an unresolvable turn, 2 for every argument error (with the usage block on stderr), 0 for
  `-h/--help` (usage on stdout).

**Output.** Widths come from content only; `shutil` is not touched on this path. Verified on
a purpose-built fixture: a multi-line prompt shows only its first non-blank line in the list
and is quoted line-by-line under the `prompt` label in the dump; a 100k-char prompt shows the
400-char preview with `[+99600 more chars]`; a `<command-name>/model</command-name>` block
renders `/model opus` via `slash_label`; an all-whitespace turn is dropped by the query, not
by the printer. `[error]` sits at the end of the line so the Detail column keeps alignment.

**Tests.** `.venv/bin/python -m pytest tests/ -q` -> **778 passed in 23.9s** (baseline 754;
+21 store, +3 TUI). The `_build_turn_store` fixture gained an `Agent` and an MCP
`authenticate` tool_use on `sid2`; no existing assertion pinned `sid2`'s tool count, so
nothing was loosened to absorb it (the diff changes one comment and no assertion).
Mutation-tested in a scratch copy, 8 of 11 caught:

| Mutation | Result |
|---|---|
| `since` -> Python post-filter | caught (3) |
| second-precision truncation dropped | caught (1) |
| `--at` without `--session` allowed | caught (1) |
| ambiguous-prefix check dropped | caught (1) |
| `[error]` marker dropped | caught (1) |
| missing-store guard dropped | caught (1) |
| `prompts` out of `main()`'s tuple | caught (2) |
| `prompts` out of `cli_main`'s dict | caught (16) |
| `raw_scan` pinned at 400 | **survived** |
| `default=str` dropped (list) | **survived** |
| `default=str` dropped (single turn) | **survived** |

**Ruff.** `uvx --from 'ruff>=0.15,<0.16' ruff check claude_trail.py claude_trail_store.py`:
18 on main, 18 on HEAD. Compared rule-by-rule with line numbers normalized away - the same 18
pre-existing E701/E702 in the `cmd_search`/`cmd_cat`/`cmd_diff` argument parsers, just shifted
down. No new finding, and the new code adds none.
