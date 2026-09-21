# Prompts tab: the same trail, grouped by prompt

Add a second top-level view to the TUI. `Commands` is today's flat, newest-first feed of
Bash rows. `Prompts` lists one row per user prompt, and its detail page shows everything
the main agent and its subagents did in response to that prompt.

Scope: `claude_trail.py` (TUI) + `claude_trail_store.py` (read-only queries) + tests + docs.
The hook and the ingest path are untouched.

## Why the prompts tab must be store-backed

The hook feed (`command-log.jsonl`) has no prompt in it at all, and it only sees Bash. The
store already has both halves: `events` carries `type='user'` turns and every `tool_use`
(Read, Edit, Grep, Task, MCP, ...) with an `agent_id`, and subagent transcripts ingest under
the parent `sessionId`. So the prompts tab is a **store view**, which makes it the first
non-modal part of the TUI that reads DuckDB.

Consequences, all accepted:

- It is as fresh as the last ingest (30s timer + Stop hook), not 300ms fresh like the feed.
  The header states the snapshot age; `r` forces a reload.
- It covers **all** tools, not just Bash. That is an upside: the grouped view answers "what
  did it actually do for me", which Bash alone cannot.
- With no store (or no `duckdb`) the tab renders the existing dim hint instead of a blank
  table, reusing `_tokens_unavailable`'s wording.
- The connection is opened read-only per refresh and closed immediately. It must NOT be held
  open: DuckDB's file lock excludes in both directions, so a held reader would block ingest
  (see CLAUDE.md, "Why not `ingest --watch`"). A lock collision degrades to a `store busy`
  status line, never a traceback.

## Turn model

A **turn** is a user prompt plus every event in the same session from that prompt's
timestamp up to the next prompt's timestamp (`lead(ts)`), with an open-ended window for the
newest prompt in each session.

- Subagent work lands inside the window automatically: a subagent's tool calls carry the
  parent `session_id` and a non-null `agent_id`, and they are timestamped while the turn
  runs. `agent_id` is what splits main-thread from subagent rows in the detail view.
- A mid-turn user message (Claude Code delivers those as a new `user` event while the turn is
  still running) starts a new group. That is the correct reading of "grouped by prompt", and
  the doc says so rather than pretending turns are atomic.
- Only main-thread turns (`agent_id IS NULL`) become rows. A subagent's own turns are its
  task prompt, which belongs under its parent's turn, not next to it.
- A turn with zero tool calls is real (a question, `/login`, a resumed session) and renders
  with a dim `-`. Measured: 54 of the newest 200 turns. `x` toggles hiding them.

### Turn text: noise filter and same-timestamp collapse

`prompt_for_command`'s `_AUTO_TURN_RE` is not enough for a *list* of turns. Measured against
the real store, these harness-generated `user` events also need excluding:

| Prefix | What it is |
|--------|------------|
| `[Image: source:` | attachment echo, emitted at the same ts as the real typed text |
| `Base directory for this skill:` | a skill body injected as a user turn after the `/slash` turn |
| `This session is being continued from a previous conversation` | compaction carry-over (18 KB) |

Fold these into the existing prefix set and rename it `TURN_NOISE_RE`, used by **both**
`prompt_for_command` and the new query. This fixes a real (if quiet) bug in the existing
command modal: an `[Image: source: ...]` echo shares its timestamp with the text the human
typed, so `ORDER BY ts DESC LIMIT 1` could already return the echo as "the prompt".

Several kept turns can still share one `(session_id, ts)` (an image plus its caption, a
`<command-name>/compact</command-name>` block plus a bare `/compact`). Left alone they
produce phantom zero-width windows, so they are collapsed to one row per `(session_id, ts)`
with `arg_max(clean, length(clean))`.

Slash-command turns stay in (invoking one is what the user did, per the existing convention)
but get a display helper: `slash_label(text)` turns a `<command-name>` / `<command-args>`
block into `/compact` or `/model opus`. `COLOR_CMD_RE` is the precedent for parsing those
blocks. Used by the prompts list, and by the command modal's prompt panel for free.

## Queries (validated against the live 1.13 GB store)

Store shape today: 80,091 `events`, 5,433 main-thread user turns, 28,450 `tool_use`,
23,934 `api_requests`, 1,520 sessions.

Bounding matters. Cleaning every user turn's text scans 5,433 large strings and costs
**1.96s**; taking the newest 400 raw turns first and cleaning those costs **74ms**. The
newest-400 window is safe because it is suffix-closed per session: the next prompt of any
fetched turn is itself fetched, except each session's newest, which correctly has no
`next_ts`.

### `prompt_turns(con, limit=200, session_id=None, raw_scan=400)`

One query returns the list plus its aggregates. Measured **113ms** cold, on top of a 16ms
warm connection open (371ms cold).

```sql
WITH raw AS (SELECT session_id, ts, text, event_id FROM events
             WHERE type='user' AND agent_id IS NULL ORDER BY ts DESC LIMIT 400),
kept AS (SELECT session_id, ts, event_id,
                trim(regexp_replace(text, '<system-reminder>.*?</system-reminder>', '', 'sg')) AS clean
         FROM raw),
turns AS (SELECT session_id, ts, min(event_id) AS event_id,
                 arg_max(clean, length(clean)) AS clean
          FROM kept WHERE clean <> '' AND NOT regexp_matches(clean, <TURN_NOISE_RE>)
          GROUP BY session_id, ts),
win AS (SELECT session_id, ts, event_id, substr(clean, 1, 400) AS preview, length(clean) AS full_len,
               lead(ts) OVER (PARTITION BY session_id ORDER BY ts) AS next_ts FROM turns),
recent AS (SELECT * FROM win ORDER BY ts DESC LIMIT 200),
acts AS (SELECT r.event_id, count(*) AS tools,
                count(*) FILTER (a.tool_name='Bash')   AS bash,
                count(DISTINCT a.agent_id)             AS agents,
                count(*) FILTER (a.agent_id IS NOT NULL) AS subcalls
         FROM recent r JOIN events a
           ON a.session_id=r.session_id AND a.type='tool_use'
          AND a.ts >= r.ts AND (r.next_ts IS NULL OR a.ts < r.next_ts)
         GROUP BY 1),
tok  AS (SELECT r.event_id, sum(q.output_tokens) AS out_tok,
                sum(CASE WHEN q.agent_id IS NOT NULL THEN q.output_tokens ELSE 0 END) AS sub_tok,
                count(*) AS requests, max(q.ts) AS last_ts
         FROM recent r JOIN api_requests q
           ON q.session_id=r.session_id
          AND q.ts >= r.ts AND (r.next_ts IS NULL OR q.ts < r.next_ts)
         GROUP BY 1)
SELECT r.*, coalesce(a.tools,0), ..., s.project, s.cwd
FROM recent r LEFT JOIN acts a USING (event_id) LEFT JOIN tok t USING (event_id)
LEFT JOIN sessions s ON s.session_id = r.session_id
ORDER BY r.ts DESC;
```

Returns a list of dicts (one per turn): `session_id, ts, next_ts, event_id, preview,
full_len, tools, bash, agents, subcalls, output_tokens, subagent_output_tokens, requests,
last_ts, project, cwd`. `session_id=` filters to one session; `limit` caps rows.

Note the token numbers are the turn's generation cost by the same window rule the store
already documents for `command_tokens` (exact per request, attributed by time), and a turn
whose assistant line carried no usage row honestly reports 0 (measured: one 21 KB paste
whose reply at 14:00:07 has no `api_requests` row).

### `turn_actions(con, session_id, ts, next_ts, limit=500)`

The detail page's list, one row per tool call in the window:

```sql
SELECT u.ts, u.agent_id, u.tool_name, u.text, u.tool_input, r.is_error
FROM events u LEFT JOIN events r ON r.tool_use_id = u.tool_use_id AND r.type='tool_result'
WHERE u.type='tool_use' AND u.session_id = ?
  AND u.ts >= ? AND (?::TIMESTAMPTZ IS NULL OR u.ts < ?)
ORDER BY u.ts, u.event_id
LIMIT ?;
```

Indexed by `events_session_ts`. `is_error` lets the detail mark failed calls.

`events.text` is NULL for tools with no entry in `_COMMAND_FIELD` (`Agent` 611 rows,
`ToolSearch`, `WebFetch`, `WebSearch`, `Skill`, MCP tools). `tool_input` is populated for
every single `tool_use` row (0 NULLs), so the label falls back to it at **render** time:

```python
_ACTION_FIELDS = ("description", "file_path", "path", "pattern", "query", "url",
                  "skill", "notebook_path", "command")

def action_label(tool_name: str, text: str | None, tool_input: str | None) -> str:
    """`text` when the store has a repr, else the first known field of tool_input."""
```

Render-time (not ingest-time) on purpose: extending `_COMMAND_FIELD` would only label newly
ingested rows, and `events` can no longer be re-derived from `raw_events` (the raw payload is
pruned for non-snapshot lines), so old rows would stay blank until a full re-ingest.

### `turn_prompt_text(con, event_id)`

Full cleaned text for the selected turn, loaded only when the detail opens (previews are
capped at 400 chars in the list query; measured max turn length 21,697 chars).

## TUI shape

### Tabs

```python
TABS = ("commands", "prompts")   # ordered; AppState.tab holds one
```

- `Tab` cycles tabs at top level. It is currently unbound there (only the search modal
  consumes `\t`), so nothing is displaced.
- A tab strip renders as the first line of the panel body, above the table:
  `  Commands   Prompts ` with the active tab `bold reverse` and the other `dim`. Panel
  title stays `claude-trail vX.Y.Z`.
- The status bar's key hints become per-tab. Prompts tab:
  `j/k:nav  ↵:view  r:refresh  x:hide empty  s:session  o:log  t:transcript  d:db  tab:commands  q:quit`.
- `AppState.tab` starts at `"commands"`; the feed stays the default view.

### State

`PromptsState` mirrors `SearchState` (a nested view-state dataclass, so `AppState` does not
grow ten more fields):

```python
@dataclass
class PromptsState:
    rows: list[PromptTurn] = field(default_factory=list)
    cursor: int = 0
    scroll_offset: int = 0
    loaded_at: float | None = None      # time.monotonic(), for the age line
    status: str | None = None           # "store busy ...", "no audit store yet - run: claude-trail ingest"
    session_filter: str | None = None   # None = every session, interleaved newest-first
    hide_empty: bool = False            # `x`: drop turns with 0 tool calls
    detail: PromptTurn | None = None    # the open turn (modal)
    actions: list[TurnAction] | None = None
    detail_text: str | None = None      # full prompt text for the open turn
    detail_scroll: int = 0
    prompt_open: bool = False           # nested full-prompt view
    prompt_scroll: int = 0
```

`PromptTurn` and `TurnAction` are frozen dataclasses built from the query dicts, so render
callables get attribute access and tests can build rows without a DB.

`AppState` gains `tab: str = "commands"` and `prompts: PromptsState = field(...)`, and
`modal_open()` / `close_modals()` learn about the prompts detail so the mutual exclusion
invariant still holds with one place to reset.

### Keys

`apply_key` dispatch order becomes: `state.search` (unchanged) -> `state.tab == "prompts"`
-> today's commands-tab path. `_apply_prompts_key` handles the prompts list and its own
modal and stays pure, returning Actions for `main()` to perform:

```python
LOAD_PROMPTS      # (re)run prompt_turns: tab entry, `r`, auto-refresh, filter toggle
OPEN_PROMPT       # load turn_actions + full text for the selected turn
```

Session-scoped keys work on both tabs by routing through one accessor:

```python
def selected_session_id(self) -> str | None:   # AppState
    """The session the cursor points at, whichever tab is active."""
```

`s` (session modal), `o` (filtered command log), `t` (transcript) and `d` (db modal) all
need only a sid, so they keep working on the prompts tab unchanged. `/` (search) and `f`
(file folder) stay commands-only: they need a row's cwd and a command's file list.

### Columns

A second data-driven list, same `Column` record, whose `render` callables take a
`PromptTurn`:

| # | Name | Content |
|---|------|---------|
| 1 | Time | `HH:MM:SS` of the prompt |
| 2 | Session | `session_label` + `/color` tint (reuses `_render_session`'s logic on a sid) |
| 3 | Project | `short_path` of `sessions.cwd`, the session's exact launch dir, written at ingest since phase 5 (first `cwd` on the main transcript, never follows a `cd`, never rewritten; subagent transcripts never contribute). Falls back to the lossy `_decode_project` of `sessions.project` when `cwd` is NULL: the transcript left the disk before the backfill ran, or it carries no `cwd` in its first `SESSION_CWD_SCAN_LINES` lines (10 of 127 on-disk transcripts, measured in the phase 5 review). Before phase 5 the column was `project` only, because `_upsert_session` never wrote `cwd` |
| 4 | Calls | `23` total, dim `(14 bash)` |
| 5 | Agents | subagent count, blank when 0 |
| 6 | Tokens | `24.2k` output, dim `+3.1k sub` when a subagent ran |
| 7 | Prompt | first line of the preview, `ratio=1`, ellipsis |

Widths follow the existing `.env` convention with new keys
(`CLAUDE_TRAIL_PROMPT_COL_*_WIDTH`), resolved once at import via `_env_int`. Digit keys
`1`-`7` toggle them on this tab, against a second visibility dict, so
`toggle_col`/`visible_cols` become per-tab (a `visible_cols_for(tab)` accessor) rather than
one global dict.

### Refresh policy

- On tab entry, on `r`, and on a filter toggle: `LOAD_PROMPTS` (one ~130ms query).
- While the tab is active and idle, auto-refresh when the snapshot is older than
  `PROMPTS_AUTO_REFRESH = 30.0` (the ingest cadence, so a faster tick would find nothing new).
- The header shows `as of HH:MM:SS (12s ago)`, and `store busy: an ingest is writing` on a
  lock collision, keeping the last good snapshot on screen.
- No background thread. Every refresh is a synchronous ~130ms call in the key loop, on a tab
  the user is looking at, which keeps the codebase's pure-state + `main()`-side-effects shape
  and keeps the new code testable without concurrency.

### Detail page

```
╭─ claude-trail v0.7.0 - prompt ─────────────────────────────────────────╮
│ Session   4be5769  claude-trail   Sent  18:58:55   Turn  6m 12s        │
│ Work      64 tool calls: 23 main (14 Bash), 41 across 2 subagents      │
│ Tokens    24.2k output (3.1k subagent) - 15 requests                   │
│                                                                        │
│ ╭─ prompt ───────────────────────────────────────────────── +14 lines ╮│
│ │ lets plan a new feature. claude-trail will have two mains views ...  ││
│ ╰─────────────────────────────────────────────────────────────────────╯│
│  Time      Agent           Tool    Detail                              │
│  18:58:57                  Bash    wc -l claude_trail.py               │
│  18:59:02  ┌ 3f2a explore  Read    claude_trail.py                     │
│  18:59:04  │ 3f2a          Grep    def build_                          │
│  18:59:09  └ 3f2a          Read    claude_trail_store.py               │
│  18:59:14                  Edit    plans/2026-07-29-prompts-tab.md     │
│  esc:back  ↵:full prompt  j/k:scroll  b:bash only                      │
╰────────────────────────────────────────────────────────────────────────╯
```

- `prompt_turns.tools` is INCLUSIVE of `subcalls` (phase 1 review, measured on a fixture: 1
  main-thread Bash plus 2 subagent calls gives `tools=3, subcalls=2`). The header must subtract
  rather than add, as the mock above does.
- `turn_prompt_text` has no length cap and the store-wide longest main-thread turn is 646,245
  chars (a summarization hook prompt), well past the 21,697 max of the newest-400 scan window. The
  full-prompt view caps what it materializes and says how much it dropped.
- The Agent gutter reuses `_render_agent`'s glyph rules (`─`/`┌`/`│`/`└`, label once per
  run) over the time-ordered action list, and `load_agent_labels` for the descriptions, so
  attribution here is retroactive (disk-derived), not forward-only like the feed.
- Bash rows keep the danger marker via `_danger_prefixed`; failed calls (`is_error`) get a
  dim red tool name.
- `j`/`k`/`g`/`G` scroll the action list with the same clamp-and-write-back contract as the
  session modal (`_clamp_*_scroll` written back in `render_panel`).
- `Enter` drills into the full prompt. `build_prompt_panel` is refactored to take
  `(session_id, agent_id, ts, prompt, ...)` instead of a `LogEntry`, so both tabs share it;
  the commands-tab caller passes entry-derived values. `Esc` steps back one level, `q`
  closes everything (`close_modals`).
- `b` filters the list to Bash only (pure state, no requery).

## CLI symmetry

`claude-trail prompts [--session S] [--limit N] [--since ISO] [--json]` prints the same
list, and `claude-trail prompts --at ISO --session S` (or `--event-id`) dumps one turn's
tool calls. Same read-only path as `search`/`tokens`, wired into `cli_main`'s dispatch and
`claude_trail.main`'s lazy-import tuple. This gives the turn model coverage that does not
depend on the TUI.

## Phases

- [x] 1. Store: turn model + queries
  - `TURN_NOISE_RE` (rename + widen `_AUTO_TURN_RE`), `slash_label`, `prompt_turns`,
    `turn_actions`, `turn_prompt_text`.
  - `prompt_for_command` switches to the shared noise regex.
  - Tests (extend `_build_prompt_store`): window bounds, subagent calls attributed to the
    parent turn, mid-turn message splits the group, noise turns excluded, same-ts turns
    collapsed to one row, zero-call turn returns 0 not NULL, token split main vs subagent,
    `--session` filter, `prompt_for_command` no longer returns an `[Image: source:` echo.
- [x] 2. TUI: tab scaffolding
  - `TABS`, `AppState.tab`, `PromptsState`, `PromptTurn`/`TurnAction`, per-tab
    `visible_cols`, `selected_session_id()`, `Tab` cycling, tab strip, per-tab status hints,
    `modal_open`/`close_modals` coverage. Prompts tab renders an empty-state hint only.
  - Tests: `Tab` cycles and preserves each tab's cursor, prompts keys are inert on the
    commands tab and vice versa, `s`/`o`/`t`/`d` resolve a sid on both tabs.
- [x] 3. TUI: prompts list
  - `PROMPT_COLUMNS` + renderers, `build_prompts_table` / `build_prompts_panel`,
    `_load_prompt_turns` via `_with_store`, `LOAD_PROMPTS`, age line, `r`, `x`,
    `session_filter`, unavailable/busy hints.
  - Carried over from the phase 2 review: `PromptsState.goto_bottom` / `move_down` never touch
    `scroll_offset` (the feed pairs every move with `clamp(max_rows)`), and
    `build_prompts_panel` ignores `term_height` for content sizing, so at 12 rows the status
    line and hints are cropped. Both are inert until this phase renders a row window; this
    phase owns the clamp.
  - Tests: column rendering per row shape, hide-empty filter, cursor clamp on reload,
    store-missing and store-busy hints.
- [x] 4. TUI: prompt detail
  - `build_turn_panel`, `OPEN_PROMPT`, `action_label`, agent gutter over actions, `b`
    toggle, scroll clamp, `Enter` into the refactored `build_prompt_panel`, `Esc`/`q` depth
    rules.
  - Carried over from the phase 2 review: `modal_open()` already counts `prompts.detail` but
    `render_panel` has no branch for it, so setting that field before this phase would swallow
    every key but esc/q/enter with no visible change. Add the renderer in the SAME change that
    first sets the field.
  - Tests: gutter glyphs over a main+subagent action list, `action_label` fallback for an
    `Agent` row (text NULL, description in `tool_input`), bash-only toggle, scroll clamp,
    Enter/Esc/q depth transitions.
- [x] 5. Store: populate `sessions.cwd` at ingest, prefer it for the Project column
  - Phase 3's review overturned this plan's earlier claim that no better source exists. Every
    transcript line carries an exact top-level `cwd` (verified in a live transcript);
    `sessions.cwd` is dead only because `_upsert_session` never writes it. Meanwhile
    `_decode_project` replaces every `-` with `/`, so 3 of the 6 projects visible in the newest
    200 turns render as paths that do not exist: `…/claude/trail` for `claude-trail`,
    `…/personal/fin` for `personal-fin`, `…/keypad/macros` for `keypad-macros`. The primary new
    view currently names half this user's projects wrongly.
  - `_upsert_session` writes `cwd` from the transcript `cwd`; `prompt_turns` already returns the
    key; `_render_turn_project` prefers `cwd` and falls back to `project`. Keep
    `_decode_project` as the fallback rather than trying to disambiguate its lossy encoding.
  - Backfill: existing rows have `cwd` NULL and the raw payload is pruned for non-snapshot
    lines, so re-deriving needs a re-ingest from the transcripts on disk. Decide and document
    whether that is a `reset-db` + ingest, or an in-place sweep that re-reads transcripts for
    session rows whose `cwd` is NULL, and say which in the docs phase.
  - Tests: a transcript whose dir name contains a `-` gets the true path (not the `/`-substituted
    one), the Project column prefers `cwd`, the fallback still renders when `cwd` is NULL, and the
    phase 1 test that pins `cwd is None` is updated deliberately rather than deleted.
- [ ] 6. CLI `prompts` subcommand + dispatch wiring, with tests.
- [ ] 7. Docs + release: CLAUDE.md (Controls, Columns, Conventions, Audit store), README,
      `.env.example` widths (including the six `CLAUDE_TRAIL_PROMPT_COL_*` keys), the `S` key,
      version bump. The plan, CLAUDE.md's Controls table and the README all currently disagree
      with the code on the prompts-tab key set; this phase reconciles all three.
  - Carried from the phase 4 review: `requires-python = ">=3.10"` is a false floor. The store's
    timestamps carry a 2-digit UTC offset (`...084-03`), which `datetime.fromisoformat` only
    accepts on 3.11+, so on the declared minimum `format_time` returns `??:??:??` for EVERY Time
    cell (feed, prompts list and action table alike) and the turn page's duration chip silently
    vanishes. Verified on a real 3.10 interpreter. Pre-existing, not introduced by this feature.
    Either bump the floor to `>=3.11` or normalize `-03` to `-03:00` before parsing; do not ship
    the release claiming 3.10 support without one of them.
  - Also from the phase 4 review: the plan's own detail-page mock is arithmetically impossible on
    the example it cites (73 calls / 31 Bash is 3 main + 28 subagent, so `32 main (31 Bash)`
    cannot happen). `prompt_turns.bash` counts Bash across the whole window, subagents included.
    Fix the mock to match the shipped wording, not the other way round.
  - `_short_tool_name` collapses three distinct MCP `authenticate` tools into one label (7 of
    31,279 calls). Accepted deliberately; the docstring records the limit.
  - Carried from the phase 5 review, the `sessions.cwd` docs debt (CLAUDE.md Audit store / Data
    model, Columns table, README):
    - `sessions.cwd` is the session's LAUNCH dir: the first top-level `cwd` on the main
      transcript, never rewritten, so it does not follow a `cd` (12 of the newest 40 transcripts
      carry several); subagent transcripts never contribute. NULL is two cases, not one: the
      transcript left the disk before the backfill ran, or it is present with no `cwd` in its
      first `SESSION_CWD_SCAN_LINES` (50) lines (10 of 127 on this machine).
    - Upgrade path: nothing to run. The next ordinary `claude-trail ingest` (Stop hook or the 30s
      timer) backfills `sessions.cwd` in place, before the transcript sweep, and logs
      `sessions: N cwd backfilled` once; `--file-history-only` skips it. Do NOT tell users to
      `reset-db`: on a store older than its transcripts that deletes sessions it cannot rebuild
      (1,703 sessions vs 127 transcripts on disk here).
    - Honest expectation, measured on a copy of the live store: 117 of 1,703 rows fill on the
      first ingest and no rendered project changes today, because the hyphenated-project
      transcripts this plan cites (`claude-trail`, `personal-fin`, `keypad-macros`) have left the
      disk and stay on the `project` fallback. The win is forward-looking; the "3 of the 6
      projects" measurement above no longer reproduces.
    - `search --project P` matched only the lossy `sessions.project`, so the name the column now
      shows found nothing. The phase 5 fix widened it to `project LIKE ? OR cwd LIKE ?` (same
      substring match, both columns). CLAUDE.md's Subcommands block already lists `--project`
      (`CLAUDE.md:234`), so this is a correction to existing text (the phase 6 review caught an
      earlier note here claiming otherwise).
  - Carried from the phase 6 review and fix, the `prompts` CLI docs debt:
    - CLAUDE.md's `Subcommands` code block (`CLAUDE.md:232-240`) and README's (`README.md:141-149`)
      gain `prompts [--session S] [--limit N] [--since ISO] [--json]` and
      `prompts (--at ISO --session S | --event-id E) [--json]`, plus a prose bullet next to the
      `search`/`tokens` ones: the Prompts tab as plain text over the same read-only path, one line
      per main-thread turn newest first with the tab's own numbers (calls with the Bash share,
      subagent count, output tokens with the subagent part, first prompt line through
      `slash_label`); `--limit` defaults to 30 and is filled by widening the raw scan until the raw
      turns run out; `--session` takes an id prefix (ambiguous is exit 2); `--since` is a lower
      bound applied inside the query; widths come from the content, never the terminal, so a pipe
      prints what a tty prints; `--json` is the query's own dicts (str/int/None only). `--at`
      resolves the newest turn at or before the bound, exact for a full-precision `ts` pasted from
      `--json` and second-truncated for a time copied from the text list, and requires
      `--session`; `--event-id` (the id `--json` prints) refuses `--session`; `--since`/`--limit`
      refuse the one-turn forms. The one-turn dump: identity/work/tokens header, the quoted
      preview capped at 10 lines, then every call oldest-first as `time agent tool detail` with
      `main` or the 4-char subagent id and a trailing `[error]` on a failed call. Exit codes: 2
      for a refused argument (including an unknown flag, unlike `search`/`tokens`), 1 for a
      missing store or an unresolved turn, 0 otherwise including an empty list. Terminal control
      characters in prompt text are stripped from the text output and kept in `--json`. README
      gets one worked example: `prompts --limit 20`, then
      `prompts --session <8 chars> --at "<the time it printed>"`.
    - The same Subcommands block is also missing `prune-raw` (pre-existing); add it.
    - `action_label` and `short_tool_name` now live in `claude_trail_store.py`, with lazy
      same-name wrappers in the TUI (the `_slash_label` pattern); `short_path` stays in the TUI
      and the CLI reads it upward through `_short_project_path`, because its default width comes
      from the `.env` machinery. Reconcile wherever CLAUDE.md's Conventions name them.
    - This plan's Verify example turn (`29dea45c`, 2026-07-27 10:30:38) still reproduces exactly
      (73 calls, 3 subagents, 41 subagent calls) on the current store; it is not stale.
    - Follow-ups, not docs: `cmd_search`/`cmd_tokens`/`cmd_cat`/`cmd_diff` traceback on a missing
      store (`prompts` and `stats` guard); one guard in `cli_main` before dispatch would fix all
      four. And the lazy `duckdb` import ends in a raw `ModuleNotFoundError` under an interpreter
      without it (the pyenv `python3` here); a one-line message would cover every store
      subcommand.

## Conventions for this run

Review docs are plan-scoped: `reviews/REVIEW-prompts-tab-phase-N.md`. The bare
`reviews/REVIEW-phase-N.md` names belong to the 2026-06-30 audit-store plan and must not be
reused (phase 1's reviewer overwrote `REVIEW-phase-1.md`; it was restored from `df3953c`).

## Verify

Run the suite with the PROJECT VENV, not the system interpreter: `duckdb` is installed only in
`.venv`, so `python3 -m pytest` (a pyenv shim onto system 3.14) fails 85 store tests with
`ModuleNotFoundError: No module named 'duckdb'` while still reporting the other 643 as passed.
That reads as a real regression and is not one. Caught during phase 4.

```bash
cd /home/naka/Projects/personal/claude-trail
.venv/bin/python -m pytest tests/ -q
.venv/bin/python claude_trail.py prompts --limit 20
.venv/bin/python claude_trail.py prompts --session <sid> --limit 5
.venv/bin/python claude_trail.py prompts --session 29dea45c --at "2026-07-27 10:30:38"
.venv/bin/python claude_trail.py            # Tab into Prompts, Enter a turn with subagents
```

Sanity checks against the real store: a turn with subagents shows their calls under the
gutter (measured example: session `29dea45c`, 2026-07-27 10:30:38, 73 calls, 3 subagents,
41 subagent calls); the newest turn of the running session grows between two `r` presses.

## Open decisions

1. Default tab stays `commands`. Flip if the grouped view proves to be the one you live in.
2. The list interleaves every session newest-first (1,520 sessions in the store, newest 200
   turns shown). The alternative, scoping to sessions present in the live feed, is one filter
   line away if the interleaving reads as noise.
3. The detail page lists all tools with `b` for Bash only. The inverse default (Bash first,
   `a` for all) is a one-line change.
