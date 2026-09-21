# claude-trail

Real-time TUI dashboard showing all Bash commands executed by Claude Code sessions.

## Architecture

```
Claude Code session
  └─ PostToolUse hook (`claude-trail hook` subcommand)
       └─ Appends JSON to ~/.claude/command-log.jsonl
            └─ `claude-trail` (TUI) tails the file and renders with Rich
```

## Files

| File | Purpose |
|------|---------|
| `claude_trail.py` | Module: TUI (`main`) and PostToolUse hook (`hook_main`). Two tabs: **Commands** tails `command-log.jsonl` into a 6-column table, **Prompts** lists one row per user prompt from the audit store. Cursor navigation, per-tab column toggles, and action keys |
| `hook.sh` | Legacy bash version of the hook (deprecated, kept for users who still reference it). New installs should use `claude-trail hook`. |
| `pyproject.toml` | Package metadata and `claude-trail` entry point |
| `requirements.txt` | Python deps (`rich>=13.0`) |

## Key Paths

- **Config dir:** all paths derive from `CONFIG_DIR = $CLAUDE_CONFIG_DIR` when set, else `~/.claude`. `LOG_PATH`, `SESSIONS_DIR`, and `PROJECTS_DIR` are module-level constants built from it (so tests can `patch.object` them, and a relocated config still resolves).
- **Log file:** `~/.claude/command-log.jsonl` (`CONFIG_DIR/command-log.jsonl`)
- **Hook config:** `~/.claude/settings.json` → `hooks.PostToolUse`
- **Session metadata:** `~/.claude/sessions/<pid>.json` (read by the TUI to resolve session names)
- **Column widths (`.env`):** `CONFIG_DIR/.env` (default `~/.claude/.env`, overridable via `$CLAUDE_TRAIL_ENV`) can set the fixed-width column widths without editing code. A tiny built-in parser (`_load_dotenv`, no python-dotenv dependency) reads `KEY=VALUE` lines once at import into `_DOTENV`; `_env_int(key, default)` resolves each width with precedence `os.environ` > `.env` > built-in default (a real process env var of the same name wins over the file), falling back to the default on a missing/non-integer/`<= 0` value. A missing file is fine (all defaults). Keys (all int, positive) with defaults: `CLAUDE_TRAIL_COL_TIME_WIDTH` (8), `CLAUDE_TRAIL_COL_SESSION_WIDTH` (13), `CLAUDE_TRAIL_COL_AGENT_WIDTH` (24), `CLAUDE_TRAIL_COL_DIRECTORY_WIDTH` (14, this is `DIR_LABEL_WIDTH`), `CLAUDE_TRAIL_COL_FILES_WIDTH` (20). The Command column has no key (it takes the remaining width via `ratio=1`). The Prompts tab has its own six: `CLAUDE_TRAIL_PROMPT_COL_TIME_WIDTH` (8), `CLAUDE_TRAIL_PROMPT_COL_SESSION_WIDTH` (13), `CLAUDE_TRAIL_PROMPT_COL_PROJECT_WIDTH` (16, this is `PROJECT_LABEL_WIDTH`), `CLAUDE_TRAIL_PROMPT_COL_CALLS_WIDTH` (14), `CLAUDE_TRAIL_PROMPT_COL_AGENTS_WIDTH` (6), `CLAUDE_TRAIL_PROMPT_COL_TOKENS_WIDTH` (16); its Prompt column is the `ratio=1` one. Colors are not configurable. See `.env.example`, which a test pins against every `_env_int` call site.

## Hook Setup

Two independent hooks in `~/.claude/settings.json`, one per data path (see
[Audit store](#audit-store)): `PostToolUse` feeds the live TUI, `Stop` keeps the
audit store fresh.

**Live feed (`PostToolUse`, Bash).** Appends each Bash command to
`command-log.jsonl` for the TUI:

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "command": "claude-trail hook" } ] }
    ]
  }
}
```

**Store refresh (`Stop`).** Runs an incremental ingest when the main agent
finishes a turn, so the TUI token modals (and `search`/`cat`) reflect just-run
commands. Point it at the `claude-trail-ingest.sh` wrapper, which backgrounds a
**singleton, timed, logged** ingest and folds in periodic compaction.

> The Stop hook only fires **between** turns, so during a long turn the store
> falls minutes behind (measured: a 13m gap on one turn, and 3m24s of live lag
> mid-turn). A systemd user timer runs the same wrapper every 30s to cover the
> inside of a turn - see [Ingest cadence](#ingest-cadence).

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "bash ~/.claude/hooks/claude-trail-ingest.sh" } ] }
    ]
  }
}
```

The wrapper is `setsid sh -c "flock -n LOCK timeout 300 claude-trail ingest [--compact] >> LOG 2>&1" &`:

- `setsid` + `&` return instantly and outlive the hook process;
- `flock -n` makes it a singleton (a still-running ingest is skipped; ingest is
  idempotent, so the next turn catches up);
- `timeout 300` keeps a hung ingest from holding the lock and silently stalling
  every future update;
- `>> LOG` (`<store>.ingest.log`, tail-capped) makes a crash/hang **visible**
  instead of vanishing into `/dev/null`;
- every 1h it adds `--compact` (marker-gated on `<store>.compact-stamp`; the
  stamp is touched inside the flock, only when the compacting run actually
  starts, so a lock-busy skip is not recorded as a compaction). Each
  `CHECKPOINT` re-expands the file to ~2x its packed size, so the store
  steady-states around 2x; compaction (an atomic `os.replace` rewrite) packs it
  back. Bloat is bounded, not unbounded.
- `--foreground` (or `CLAUDE_TRAIL_INGEST_FG=1`) skips the `setsid`/`&` and runs
  the ingest inline. The systemd timer needs this: under `Type=oneshot` systemd
  tears the cgroup down when the main process exits, which would kill a
  backgrounded ingest mid-run. It exits 0 even on a `flock` skip, so a busy lock
  is never reported as a unit failure.

`claude-trail ingest --detach` is the minimal, portable equivalent (it does its
own `fork`/`flock`, needing no `setsid`/`flock`/`timeout` binaries) and appends
its output to the same `<store>.ingest.log`; the wrapper still adds the hang
`timeout` and the log tail-capping, so prefer it where those binaries exist.

### Ingest cadence

Two triggers drive the same wrapper, and the `flock` singleton keeps them from
overlapping:

| Trigger | Covers |
|---------|--------|
| `Stop` hook | the moment a turn ends (immediate refresh) |
| `claude-trail-ingest.timer` (30s) | the **inside** of a long turn, where the Stop hook alone left the store minutes stale |

The units are `~/.config/systemd/user/claude-trail-ingest.{service,timer}`
(`OnUnitActiveSec=30s`, `AccuracySec=1s`, `Nice=10`, `IOSchedulingClass=idle`).

**Why not `ingest --watch`.** It opens one read-write connection and holds it for
its whole life, and DuckDB does not let a reader share a held write lock: another
process opening the store read-only fails outright (`IO Error: Could not set lock
on file ... Conflicting lock is held`). A `--watch` daemon would therefore make
the TUI's `Tokens` rows, prompt panel, and `d` modal permanently unavailable.
Periodic one-shots hold the lock only for their own run.

**Why the CHECKPOINT is throttled.** That lock window is the whole cost of a
short cadence: a one-shot run on a 1 GB store measured 6.0s wall, of which ~1.8s
was the sweep and ~3.5s the closing `CHECKPOINT`. At 6s per 30s the store would
be locked ~20% of the time and roughly one modal open in five would show the dim
hint for data that is actually there. So `cmd_ingest` flushes only when it is due
(`_checkpoint_due`): never on a sweep that stored nothing, otherwise at most once
per `CHECKPOINT_MIN_INTERVAL` (300s), tracked by `<store>.checkpoint-stamp`.
`--compact` and `--watch` force the flush (a rewrite follows; a shutdown flush is
the point). Skipping is safe: the WAL is durable and replays on the next open,
and DuckDB auto-checkpoints on its own WAL-size threshold, so a skipped flush
costs a slightly larger `.wal`, never data.

**Lock collisions are routine, not faults.** DuckDB's file lock excludes in BOTH
directions: an ingest cannot open a reader-held store any more than a reader can
open a writer-held one. At 30s that happens regularly against an open TUI, and it
used to end the ingest with an unhandled `IOException` traceback in the log
(observed 04:52:47, which cost that cycle). `is_lock_conflict(exc)` identifies it
by message, and:

- `connect_writer()` (used by `cmd_ingest`) retries a few times, then returns
  None; the run logs `skipped: store is held by a reader; the next run catches
  up` and **exits 0**, so a routine collision is never recorded as a unit failure
  and the idempotent next run covers it;
- `cli_main` turns the reverse case into `store busy: an ingest is writing ...`
  on stderr with exit 1, instead of a traceback out of `search` / `cat` / `stats`.

Any non-lock error still propagates unchanged.

## Running

```bash
claude-trail              # if installed via pipx / uv tool install
python3 claude_trail.py   # if running from a clone
```

## Controls

`Tab` cycles the two top-level views (`TABS = ("commands", "prompts")`, the feed
first). Each tab keeps its own cursor, scroll offset and column visibility, so
switching back lands where you left it. The table below is the Commands tab (the
live feed); the Prompts tab and the pages it opens follow it.

`s`, `o`, `t` and `d` work on BOTH tabs: they need only the selected row's
session id, which `AppState.selected_session_id()` resolves whichever tab is
active. `/` and `f` stay Commands-only - they need a row's cwd and a command's
file list, which a turn row does not carry - and `c` belongs to the log tail.

| Key | Action |
|-----|--------|
| `Tab` | Switch to the other tab (Commands ⇄ Prompts); entering Prompts reads the store right away |
| `j`/`k` or `↓`/`↑` | Move cursor down/up |
| `g`/`G` | Jump to top/bottom |
| `Enter` | Open the full-command detail view for the selected command (includes the command's token cost and the first `PROMPT_PREVIEW_LINES` lines of the prompt that led to it, from the audit store when available). `Enter` again opens the whole prompt in a scrollable view (`j`/`k`/`g`/`G`); `Esc` steps back to the command, `q` closes both |
| `s` | Open the session-detail modal for the selected row's session (identity header + token total + subagent list with live status) |
| `d` | Open the audit-store database modal: connection string, DuckDB / schema version, on-disk sizes, block accounting, and per-table row counts (opens the DuckDB store read-only, on demand) |
| `Esc` | Close the open modal, command detail, session detail, or database (also `q` or `Enter` while it is open; in the full-prompt view `Esc`/`Enter` step back one level and `q` closes outright) |
| `o` | Open selected session's commands (pretty-printed, filtered to the session) in `$VISUAL` or the platform default launcher |
| `t` | Open a pretty-printed copy of the selected session's transcript (`<sid>.jsonl`) in `$VISUAL` or the platform default launcher |
| `f` | Open file manager on folder of files referenced in selected command |
| `/` | Open per-session recursive search for the selected row's session. Type a query then `Enter` to run; `Tab` toggles the root between the transcript folder (`<sid>.jsonl` + `subagents/**`) and the session's cwd. In results: `j`/`k` browse, `Enter` opens the hit at its line, `f` opens the hit's folder, `/` edits the query, `Esc`/`q` close |
| `1`-`6` | Toggle this tab's columns: 1=Time, 2=Session, 3=Directory, 4=Files, 5=Command, 6=Agent. The digits are per tab (see Columns), so they never touch the Prompts tab's list |
| `c` | Clear display |
| `q` | Quit |

### Prompts tab

One row per user prompt, read from the audit store (see [Audit store](#audit-store)).
Keys the feed does not have, plus the shared ones above:

| Key | Action |
|-----|--------|
| `Enter` | Open the selected turn's page: what the main agent and its subagents did in response to that prompt |
| `r` | Re-read the store now (the list is only as fresh as the last ingest) |
| `x` | Hide/show the turns that ran no tool call. Filtered in memory over the loaded snapshot, so it is a lens over one list, not a new query |
| `S` | Scope the list to the selected row's session, or back to every session. Unlike `x` this DOES re-query: `prompt_turns` applies the filter inside its bounded raw scan, so a scoped list reaches back through that session's own history instead of showing whichever of its turns made the global newest-200 cut |
| `1`-`7` | Toggle this tab's columns: 1=Time, 2=Session, 3=Project, 4=Calls, 5=Agents, 6=Tokens, 7=Prompt |
| `j`/`k`, `g`/`G`, `s`, `o`, `t`, `d`, `Tab`, `q` | As on the feed |

### Turn page (`Enter` on a turn row)

| Key | Action |
|-----|--------|
| `j`/`k` or `↓`/`↑`, `g`/`G` | Scroll the action list |
| `b` | Show only the turn's Bash calls, or everything again (in memory, no requery) |
| `Enter` | Drill into the whole prompt, when the full text loaded; with nothing to open it closes the page instead |
| `Esc`/`q` | Back to the list |

In the nested full-prompt view (`Enter` again): `Esc`/`Enter` step back one level
to the turn page, `q` closes everything, `j`/`k`/`g`/`G` scroll. Same depth rules
as the command modal's full-prompt view.

## Columns

Columns are data-driven: one ordered list of frozen `Column` records per tab is
the source of truth (`COLUMNS` for the feed, `PROMPT_COLUMNS` for the Prompts
tab). A `Column` has `key` (the stable digit used by the toggle keys and the
`1 2 3 …` status bar), `name`, `style`, `kwargs` (passed to `Table.add_column`),
and `render`. `render(row, ctx)` returns that column's cell for one row as a
`str` or `rich.text.Text`. Adding a column is a single entry in the list.

Per-tab visibility: `COLUMNS_BY_TAB` maps each tab to its list, `_default_cols(tab)`
derives that tab's all-visible dict from it, and `AppState.visible_cols_for(tab)`
returns the dict for the active tab (the feed's lives in `AppState.visible_cols`,
the Prompts tab's in its own dict). Each tab's keys are numbered from 1
independently, so `1`-`6` toggle the feed's columns and `1`-`7` the prompts
list's, and `toggle_col` ignores a digit the active tab has no column for. Both
tabs still refuse to hide their last visible column.

A render callable must return a `Text` for any value that is not program-generated
(a prompt, a project path): a bare `str` is parsed as Rich console markup, so
`list[int]` would lose `[int]` and an unbalanced `[` would raise out of the live
update.

Each fixed-width column's width is overridable from `CONFIG_DIR/.env` (see Key
Paths): the `COLUMNS` widths (and `DIR_LABEL_WIDTH`, which the Directory column
and `short_path`'s default share), plus the `PROMPT_COLUMNS` widths (and
`PROJECT_LABEL_WIDTH`, which the Project column and its `short_path` call share),
are resolved once at import via `_env_int("CLAUDE_TRAIL_COL_*_WIDTH", <default>)`
and `_env_int("CLAUDE_TRAIL_PROMPT_COL_*_WIDTH", <default>)`, so the defaults
reproduce the current widths exactly and both lists stay fixed for the process
lifetime (never rebuilt per frame). Only widths are configurable, not colors.

`ctx` is a frozen `RenderCtx` carrying the per-render state cells need
(`name_map`, `color_map`, `agent_map`, plus the per-row `agent_label`/`run_first`/`run_last` the Agent column reads); render callables call the module-level helpers
(`format_time`, `session_label`, `rich_color`, `short_path`, `extract_files`,
`is_dangerous`, `normalize_cmd`) directly.

| # | Name | Content |
|---|------|---------|
| 1 | Time | HH:MM:SS timestamp (`format_time`) |
| 2 | Session | Session name from `~/.claude/sessions/<pid>.json` if set, else first 8 chars of session_id, tinted with the color the user picked via Claude Code's `/color` command (parsed from the session transcript) |
| 6 | Agent | Tree gutter attributing the row to a subagent run (blank for the main agent); glyphs `─`/`┌`/`│`/`└` connect a consecutive `(session_id, agent_id)` run with the label shown once, drawn right after Session (`_render_agent`). The label is `<short-id> <description>` (`short_agent_id` = first `AGENT_ID_LABEL_LEN` chars of the id, placed first so it survives the ellipsis), so two subagents sharing a description are still distinct; full description/type live on the `s` modal |
| 3 | Directory | Tail of the cwd (`short_path`): the distinctive end of the path, left-truncated with a leading `…` to `DIR_LABEL_WIDTH`, kept at a component boundary when it fits (`…/claude-trail`) |
| 4 | Files | File paths extracted from command (basenames, max 3 shown) |
| 5 | Command | Full command text, dangerous commands prefixed with red `*` |

### Prompts tab columns

`PROMPT_COLUMNS`, whose `render` callables take a `PromptTurn` (a frozen record
built from one `prompt_turns` row) instead of a `LogEntry`. There is no per-row
`RenderCtx` state here: the agent tree gutter is a feed thing (one row per
command), while a turn row is one prompt and names its subagents by count.

| # | Name | Content |
|---|------|---------|
| 1 | Time | `HH:MM:SS` the prompt was sent (`format_time` over the store's timestamp) |
| 2 | Session | Same cell as the feed's Session column (`_session_cell`): name if the feed has seen the session, else the id prefix, tinted by `/color` |
| 3 | Project | `short_path` of `sessions.cwd` to `PROJECT_LABEL_WIDTH`: the session's EXACT launch dir. Falls back to `_decode_project(sessions.project)` when `cwd` is NULL, which is lossy (see Data model) |
| 4 | Calls | Tool calls in the turn's window, with the Bash share: `23 (14 bash)`. A turn that ran none renders a dim `-`, not a `0` that reads as missing data |
| 5 | Agents | How many subagents ran, blank when none did |
| 6 | Tokens | Output tokens abbreviated (`24.2k`), with a dim `+3.1k sub` when subagents generated any |
| 7 | Prompt | First non-blank line of the 400-char preview, through `slash_label` (so a `<command-name>` block reads `/compact`); `ratio=1`, ellipsis |

`_turn_project_path(turn)` is the one place the Project rule lives: `turn.cwd or
turn.project or ""`. The turn page's header calls it too, so the list and the
page can never name the same session two different ways.

## Conventions

- Log format is JSONL with fields: `timestamp`, `command`, `cwd`, `session_id`, plus optional `agent_id`/`agent_type` on a subagent's commands (PostToolUse adds them only inside a subagent, so an absent `agent_id` means the main agent). Modeled as the `LogEntry` TypedDict; annotation only, `parse_line` still returns a plain dict at runtime. `hook_main` builds each line via `_new_entry()` to fix the key order.
- The feed attributes each row to the agent that ran it via the forward-only hook `agent_id`: `load_agent_labels(session_ids)` maps `agent_id -> {type, description}` from `PROJECTS_DIR/*/<sid>/subagents/**/agent-*.meta.json` (cached `AGENT_LABEL_CACHE_TTL` = 2s, accumulating), and `agent_label(entry, agent_map)` / `entry_agent_id(entry)` resolve a row's label (`None` for the main agent; otherwise `<short-id> <description>`, the id first so distinct agents with the same description stay distinguishable). The Agent column (`_render_agent`, `key=6`) draws an in-place tree gutter over the time-ordered feed: `build_table` marks each row's position in its consecutive `(session_id, agent_id)` run (`run_first`/`run_last`) and picks the glyph (`─`/`┌`/`│`/`└`), showing the label once per run; rows stay one-per-row so cursor/scroll/tail math is unchanged. Attribution is forward-only (rows logged before the hook enrichment, and every main-agent row, render blank), while the session modal (`s`) is retroactive: it reconstructs every subagent from disk regardless of when the row was logged.
- Dangerous commands (rm, sudo, git reset, chmod, etc.) are flagged with red `*` prefix
- Display shows newest-first, max 50 entries, with active session count = distinct `session_id`s seen within `ACTIVE_WINDOW_SECONDS` (300s), computed by `count_active_sessions(entries, now, window=ACTIVE_WINDOW_SECONDS)`
- Poll interval: 300ms
- File paths extracted by `find_paths(cmd)` (regex matching absolute `/...`, home `~/...`, relative `./...`; order-preserving dedupe via `dict.fromkeys`). `extract_files()` is a thin basename formatter over it and `open_file_folder()` reuses it.
- `Enter` opens an in-TUI detail view (`build_detail_panel()`) showing the selected command in full: untruncated, newlines preserved, with the danger `*` marker and a metadata header (time, session, directory, files, and a `Tokens` row when the audit store has data). `Esc`/`q`/`Enter` close it. The view is modal: while open, navigation/column/clear keys are ignored (only `Ctrl-C` still quits). The `Tokens` row is the command's generation cost: `Enter` returns `Action.OPEN_DETAIL`, which makes `main()` call `_load_command_tokens(entry)` once (opening the store read-only via `_with_store`, so the feed/hook still never load duckdb); it maps the row to `claude_trail_store.command_tokens_for(session_id, command, ts)`. When there is no match the row shows a dim hint instead of vanishing (`pending ingest (store lags the live feed)` when the store exists but has not caught up, `no audit store yet - run: claude-trail ingest` when the file is absent; the `_tokens_unavailable` sentinel) so the feature never looks silently broken. When the emitting request made several parallel tool calls the row shows the even per-call share and says so.
- The detail view also carries a `prompt` panel: the first `PROMPT_PREVIEW_LINES` (10) lines of the turn that led to the command, with a dim `+N more lines` footer. `Action.OPEN_DETAIL` loads it in the SAME read-only store open as the Tokens row (`_load_command_detail(entry)` returns `(tokens, prompt)`), so opening the modal costs one store open, not two; a missing answer reuses the `_tokens_unavailable` sentinel. The prompt comes from `claude_trail_store.prompt_for_command(con, session_id, ts, agent_id)`: the newest `type='user'` event at or before the row's timestamp **in the same `(session_id, agent_id)` thread**, so a main-agent row reports the user's turn and a subagent row reports that subagent's own turn (its task prompt on the first turn). It needs only what the hook entry already carries, not the command's own `tool_use` row, so it answers even while ingest lags the command. `<system-reminder>` blocks are stripped (`clean_prompt_text`) and harness-generated turns are stepped over (`TURN_NOISE_RE`, shared with the store's turn model: task notifications, local-command caveat/stdout, `<bash-stdout>`, interrupt markers, an `[Image: source: ...]` attachment echo, an injected skill body, a compaction carry-over blob) so the panel shows what a human actually asked; slash commands and `!` bash input are kept, since invoking one IS the user's turn. The widened skip-set also fixes the modal reporting the `[Image: source: ...]` echo as the prompt, which shares its timestamp with the text the user typed and could win the `ORDER BY ts DESC` tie.
- `Enter` on a detail view that has a prompt drills into the full-prompt view (`build_prompt_panel()`) instead of closing: identity header (session, subagent, `Sent` time), then the whole prompt windowed around `AppState.detail_prompt_scroll` with `j`/`k`/`g`/`G` (same clamp-and-write-back contract as the session modal, via `_clamp_prompt_scroll`). The builder takes `(session_id, term_height, text, ts, agent_id, ...)` as plain values rather than a `LogEntry`, so the Prompts tab's turn page drills into the SAME view with its own row's values; `agent_id=None` drops the Agent row. `Esc`/`Enter` step back to the command detail, `q` closes everything from any depth (`AppState.close_modals()`), `Ctrl-C` still quits. With no prompt to open (the dim sentinel), `Enter` keeps its original close-the-modal meaning. The key hints live in that view's header rather than under the body: prompt lines wrap, so a footer would be the first thing the panel's height crop removed.
- `s` opens the session-detail modal (`build_session_panel()`) for the selected row's session: an identity header (id, name tinted by the session `/color` plus its live status, `~`-abbreviated folder, transcript path, version, start time) and a table of the session's subagents (status glyph `●` running / `✓` done / `✗` stopped, description, agent type, tool-call count shown as `N+` when byte-capped). It reads `load_session_model(sid)` (cached `SESSION_MODEL_CACHE_TTL` = 2s), which assembles the model from the live `sessions/<pid>.json`, the parent transcript's `<task-notification>` completion events, and each `subagents/**/agent-*.meta.json`; status is derived (completed task-id -> `done`, failed/killed -> `stopped`, else `running` when the session is live else `stopped`). The header also carries a `Tokens` total (exact per-session output / input / cache / request count, subagent output called out when any subagent ran): pressing `s` returns `Action.OPEN_SESSION_DETAIL`, which makes `main()` call `_load_session_tokens(sid)` once against `claude_trail_store.session_token_totals(sid)` (same read-only `_with_store` path); when the store has no ingested requests for the session the row shows the same dim `_tokens_unavailable` hint as the command modal. Both modals are mutually exclusive: `AppState.modal_open()` is true while either is open, `apply_key` swallows everything but `Esc`/`q`/`Enter` (which clear both fields) and `Ctrl-C` (quit), and `render_panel` shows the session modal in precedence over the command modal.
- `/` opens the per-session search modal (`SearchState` on `AppState.search`, rendered by `build_search_panel`). Roots come from `search_roots(sid, entry, model)`: `"transcript"` = the `<sid>.jsonl` file plus its sibling `<sid>/` dir (`subagents/**`), `"cwd"` = the live session's launch dir (pid.json) or, for an ended session, the row's cwd; a non-existent path is dropped and `Tab` switches the active root. `run_search(query, paths)` shells out to `rg` when present (`-S` smart-case) else `grep -rInE`, passing the query as argv (never through a shell), capped at `SEARCH_RESULT_LIMIT` (500) with a `SEARCH_TIMEOUT` (5s) so a huge tree can't hang the UI; rg/grep exit 1 (no match) is empty, exit >= 2 (bad regex) returns an `error` string, never raises. The modal has two sub-modes (`INPUT` types the query, `RESULTS` browses hits) so letters and `j`/`k` never conflict, and it is mutually exclusive with (and takes precedence over) the command/session modals: `apply_key` checks `state.search` first and stays pure, returning `RUN_SEARCH`/`OPEN_MATCH`/`OPEN_MATCH_FOLDER` for `main()` to perform. `OPEN_MATCH` reuses `open_session_log`'s suspend-`Live` editor pattern (with a `+<line>` arg for vi-family editors); `OPEN_MATCH_FOLDER` reuses `open_file_folder`'s detached `Popen(_platform_opener())` via `open_containing_folder`.
- The **Prompts tab** (`Tab`) is a store view, not a feed view: `build_prompts_panel` renders `PromptsState.rows`, which `_load_prompt_turns` fills from `claude_trail_store.prompt_turns` through the same read-only `_with_store` open the modals use (connect, query, close - never held, see "Why not `ingest --watch`"). So it is as fresh as the last ingest, not 300ms fresh, and the header always says which: `as of HH:MM:SS (12s ago) · 149 turns (51 hidden)`, plus the session scope and any store status. `_load_prompt_turns` returns three distinguishable outcomes - rows, `([], NO_STORE_HINT)` for a missing store or a missing `duckdb`, and `(None, STORE_BUSY_HINT)` for a lock collision against the running ingest, where `None` means "no answer, keep what is on screen" so a routine collision never blanks a good snapshot (`_refresh_prompts` then shows `store busy: an ingest is writing - kept the last snapshot` and leaves `loaded_at` alone). `_prompts_empty_state` tells the five empties apart, so `claude-trail ingest` is never advised for a store that is already current. Refresh is synchronous and has no background thread: on tab entry, `r` and a session-filter toggle (`LOAD_PROMPTS`), plus an idle auto-refresh once the last ATTEMPT is older than `PROMPTS_AUTO_REFRESH` (30.0s = the ingest cadence, so a faster tick would find nothing new and collide with the write lock more often). `PromptsState` keeps two monotonic stamps for that: `loaded_at` (the last answer, for the age line) and `checked_at` (the last attempt, for the backoff).
- Store reads out of a key burst are COALESCED, because main() drains every buffered keystroke before rendering: `_drain_action` folds `LOAD_PROMPTS` / `OPEN_PROMPT` into `DrainFlags` and returns True so the drain does NOT perform them inline, and `_frame_store_reads` performs at most one of each after the loop. Ten buffered `r` is one query, not ten (measured 3.21s frozen inside one drain before this); ten `↵` is one turn read, for whatever turn the drain ended on. The other actions stay inline: an editor launch or a search is what the user asked for on that press, and `s`/`d` open a modal that swallows the repeat. The idle auto-refresh is additionally gated on no modal being open, while an explicit request stands even if a later key in the same drain opened one.
- `Enter` on a turn row opens the **turn page** (`build_turn_panel`), loaded by `_load_turn_detail` in ONE store open: `turn_actions` (every `tool_use` in the window, oldest-first, `LIMIT 500`) plus `turn_prompt_text` (the full cleaned prompt). Three header rows - Session (session cell, project path, `sent`, and `ran <duration>` from the prompt to the last API request the store attributed to the window, never to `next_ts`, which is when the user next typed), Work, Tokens. The **Work row SUBTRACTS**: `prompt_turns.tools` is INCLUSIVE of `subcalls`, so `64 tool calls: 23 main, 41 across 2 subagents  ·  14 Bash` is `tools`, `tools - subcalls`, `subcalls`; adding them would report 105 calls for a turn that made 64. The Bash count is window-wide (the query filters `tool_name='Bash'` over the whole window, subagents included), so it stands on its own rather than nested under the main-thread count, which would claim subagent Bash calls for the main thread. Then the prompt quoted in a box and the action list windowed around `detail_scroll`. The box's height is MEASURED per turn (`_prompt_box_rows`: 2 borders + up to `TURN_PROMPT_PREVIEW_LINES` (2) quoted lines + the `+N more lines` footer when it hides any) instead of reserved at its worst case, because 135 of the newest 200 turns in a real store have a one-line prompt, where a fixed reservation left 3 of the 9 action rows at h=24 permanently blank. The action table's Agent column reuses the feed's tree gutter (`_agent_gutter`), but attribution here is RETROACTIVE: the ids come from the store's transcripts, so a turn from before the hook learned to tag subagent commands still shows them. `b` filters to Bash only in memory (`visible_actions`, no requery), and `Enter` drills into the shared full-prompt view. The text it opens is capped at `PROMPT_TEXT_CHAR_CAP` (100,000 chars; the longest main-thread turn measured in a real store is 646,245) by `_cap_prompt_text`, whose notice LEADS the text: the view windows by logical line while the Panel crops by display row, so a trailing notice is the first thing cropped and is unreachable by any key.
- `action_label(tool_name, text, tool_input)` and `short_tool_name(tool_name)` live in `claude_trail_store.py` with the turn model both readers query, and the TUI keeps same-name wrappers that import them lazily (the `_slash_label` pattern; importing the store module does not pull in `duckdb`). `action_label` falls back to the first `_ACTION_FIELDS` entry of the raw `tool_input` when `events.text` is NULL (every tool with no `_COMMAND_FIELD` entry: Agent, Skill, MCP, ...), resolved at render time because old rows can no longer be re-derived from `raw_events`. `short_tool_name` drops an MCP tool's server prefix; the known, accepted cost is a collision between tools whose own names match (three MCP `authenticate` tools, 7 of 31,279 calls), recorded in its docstring rather than fixed, since disambiguating would cost every MCP row its legible label. `short_path` goes the other way: it stays in the TUI (its default width comes from the `.env` machinery) and the CLI reads it upward through `_short_project_path`.
- `TURN_NOISE_RE` (and `TURN_NOISE_PATTERN`, its SQL twin bound into the turn query) is ONE definition of "this `user` event was written by the harness, not by a human", used by `prompt_for_command`, `prompt_turns` and `find_turn`, so the command modal's prompt and the Prompts tab can never disagree about what counts as a turn.
- Timestamps are parsed in one place, `parse_ts`, which widens an hours-only UTC offset (`...37.084-03` to `...37.084-03:00`) before `datetime.fromisoformat`. That is how DuckDB renders a `TIMESTAMPTZ`, and `fromisoformat` only learned to read it in 3.11: on the declared 3.10 floor every Time cell rendered `??:??:??` and the turn page's duration chip vanished. `format_time`, `count_active_sessions` and `_turn_duration` all route through it; `Z` and a full `+HH:MM` pass through untouched. The CLI does not parse at all - `_turn_time` slices the string DuckDB already rendered.
- Session commands written pretty-printed (indented JSON blocks, blank-line separated, same shape as the `t` transcript) under `tempfile.gettempdir()` (default `/tmp`) as `claude-trail-session-{sanitized-id}.json` on `o`; the output is only ever read by a human in an editor, so there is no raw-JSONL variant. If `$VISUAL`/`$EDITOR` is set, `open_session_log()` suspends the `Live` display, restores the terminal, runs that editor in the foreground (so terminal editors like nvim get the tty), then resumes; otherwise it hands the file to the GUI launcher detached.
- Arrow keys are read in both CSI (`ESC [ A`/`B`) and SS3 (`ESC O A`/`B`, application-cursor-keys mode) forms by `read_key()`, which parses one complete CSI sequence per call (parameter bytes, one final byte) so modified arrows (`ESC [ 1 ; 5 A`) and function keys never leak stray bytes into the key dispatch; a final byte of `A`/`B` navigates regardless of modifier parameters.
- Panel title shows the installed version (`claude-trail vX.Y.Z`), read from package metadata via `importlib.metadata`; blank when run from a clone without an install.
- Session names are read from `~/.claude/sessions/<pid>.json` (`name` field). Cached for 2s in `load_session_names()`; the cache accumulates so a session's name remains resolvable after Claude Code removes its pid.json on exit.
- Session color comes from the latest `/color <value>` event in `~/.claude/projects/*/<session-id>.jsonl` (`system/local_command` events). `load_session_colors()` tails each transcript incrementally and refreshes at most every 5s. Names like `orange`/`pink`/`gray` are translated to Rich-compatible equivalents (`orange1`, `pink1`, `grey50`) via `CLAUDE_COLOR_ALIASES`.
- Columns are defined once in the module-level `COLUMNS` list of frozen `Column` records (`key`, `name`, `style`, `kwargs`, `render`). `build_table` builds a base `RenderCtx(name_map, color_map, agent_map)`, computes each row's `(session_id, agent_id)` run boundaries, and per row `dataclasses.replace`s the base ctx with that row's `agent_label`/`run_first`/`run_last` before calling `col.render(entry, ctx)` for each visible column in list order; there is no per-column `if col_id == ...` ladder. `build_detail_panel` reuses `_render_session` for the Session tint and `_danger_prefixed` for the Command danger `* ` marker (it keeps the raw, un-normalized command so newlines survive). Default visibility is derived from each tab's list (`_default_cols(tab)` over `COLUMNS_BY_TAB`); the digit-toggle branch resolves the ACTIVE tab's dict, ignores a digit that tab has no column for, and still refuses to hide the last visible column. `build_prompts_table` is the same loop over `PROMPT_COLUMNS` with a `PromptTurn` per row and no per-row ctx.
- Column visibility persists during the session and is per tab; the status bar shows the active tab's toggles (`1 2 3 4 5 6` on the feed, `1 2 3 4 5 6 7` on the Prompts tab) next to that tab's own key hints (`_TAB_HINTS`), so neither line advertises a key the view on screen does not answer
- Platform-specific file launcher: `xdg-open` on Linux, `open` on macOS, selected via `claude_trail._platform_opener()`. Honours `$VISUAL` first if set.

## Audit store

`claude_trail_store.py` is a separate, DuckDB-backed audit trail. It ingests the Claude Code transcripts (`~/.claude/projects/**/*.jsonl`, including `subagents/agent-<id>.jsonl`) into one local file and answers search / token / file-reconstruction queries over the full history. It is independent of the live TUI: `claude_trail.py main()` only lazily imports it when the first argument is one of the store subcommands (so the TUI and hook never load `duckdb`). The interactive exceptions are the TUI `d` (database) modal, whose `_load_db_info()` imports the store and opens it read-only on demand (`db_info(con)` returns the (label, value) rows shared by that modal and the `stats` CLI section: connection string, DuckDB / schema version, on-disk sizes, block accounting, per-table counts); the `Enter` / `s` modals' `Tokens` rows and prompt panel, which open the store read-only via `_with_store` and read `command_tokens_for(con, session_id, command, ts)` / `prompt_for_command(con, session_id, ts, agent_id)` / `session_token_totals(con, session_id)`; and the whole **Prompts tab**, which is a store view - its list reads `prompt_turns(con, limit=200, session_id=None, raw_scan=400, since=None)` and its turn page reads `turn_actions(con, session_id, ts, next_ts, limit=500)` + `turn_prompt_text(con, event_id)`, each in its own read-only open. All of these are read-only, hold the connection for one call, and return None (or an empty list) to omit what they cannot answer rather than raising.

- **Store file:** `~/.claude/trail.duckdb`, overridable via `$CLAUDE_TRAIL_DB` (constant `STORE_PATH`). `connect(read_only=...)` opens it; the parent dir is created on demand.
- **Reads transcripts, not the hook.** The store ingests transcripts, so it covers every tool (Read, Edit, Write, Grep, Task, ...) and gets each call's result and edit patch for free. The live TUI is unchanged and still tails `command-log.jsonl`, which the PostToolUse hook writes and which is Bash-only. Two independent data paths: the hook feeds the TUI, the transcripts feed the store.

### Subcommands

Invoked as `claude-trail <cmd>` (installed) or `python3 claude_trail.py <cmd>` (from a clone):

```
ingest [--watch] [--detach] [--compact] [--file-history-only] [-v]
search <regex> [--tool T] [--session S] [--project P] [--since ISO] [--limit N] [-i]
stats
tokens [--session S] [--by session|command|tool|day|model] [--since ISO] [--limit N]
prompts [--session S] [--limit N] [--since ISO] [--json]
prompts (--at ISO --session S | --event-id E) [--json]
cat <path> [--session S] [--at ISO]
diff <path> [--session S] [--all-sources]
reset-db [-y]
prune-raw [-y] [--no-compact]
```

- `ingest` is the **single writer**: it holds DuckDB's one read-write connection, tails each transcript from its last byte offset, and is idempotent (re-ingest inserts nothing). It sweeps transcripts first, then backfills file-history (see below). Run it from cron or leave one `--watch` process polling every 5s. Every other subcommand (and any ad-hoc reader) opens the file **read-only**; the hook never writes DuckDB (many short-lived hook processes would contend on the write lock). `--compact` runs once after the sweep (ignored under `--watch`) and reclaims DuckDB's interspersed free blocks by rewriting the store into a fresh file (a plain `CHECKPOINT` never coalesces them, so a bulk ingest otherwise leaves the file ~2x its packed size). `--file-history-only` runs just the file-history backfill pass (does not touch `ingest_state`; composes with `--compact`). `--detach` forks a one-shot ingest into the background and returns immediately (for a Claude Code `Stop` hook, so the store stays current without a `--watch` daemon or cron): it takes a per-store `flock` singleton (`<store-dir>/trail-ingest.lock`, keyed to `STORE_PATH` so distinct `$CLAUDE_TRAIL_DB` stores are independent) and skips (exit 0, no fork) if another ingest already holds it, since the next turn catches up. `--detach` implies not-`--watch`; every other flag passes through, so `--detach --compact` backgrounds a sweep+compact (`_ingest_detach`: double-fork -- parent `waitpid`s the intermediate child and returns 0, the worker `setsid`s and is reparented to init so no zombie is ever left, even for a long-lived caller; the worker's stdin comes from `/dev/null` and stdout/stderr append to `<store>.ingest.log` (`_ingest_log_path`), the same file the hook wrapper writes, so a crashing detached ingest stays diagnosable; `fcntl` is imported lazily and a platform without `os.fork` gets a logged no-op returning 0, so the module still imports and a hook never errors).
  - **Progress + logging.** On a tty, `ingest` shows a live Rich progress bar (`_IngestProgress`) for the two long phases, sized by transcript-file count and by the number of new file-history snapshots (immutable, already-stored PKs are skipped before the bar is sized). Off a tty (cron/pipe) the bar is disabled and it prints timestamped lines instead (`_log`): `ingest start`, per-sweep `transcripts: changed/total`, `file-history: N new / M resolved`, a `stored: +rows` per-table delta (`_table_counts` before/after), and `sweep done` with per-phase timing; `-v`/`--verbose` adds one line per changed transcript when the bar is off. Under `--watch` an idle sweep logs nothing. `ingest_file_history` stays silent when a progress `tick` is attached (so it cannot corrupt the bar); its own one-line summary prints only for standalone callers (`ingest_all`, tests).
- `reset-db` deletes the store file and its WAL (confirmation prompt unless `-y`/`--yes`). The store is a derived cache (rebuildable from `~/.claude/projects`, plus file-history blobs re-ingestable while `~/.claude/file-history` exists), so a reset loses nothing a later `ingest` cannot rebuild. Run that first rebuild manually (`claude-trail ingest`): the Stop-hook wrapper's `timeout 300` would chunk a full-history rebuild into 5-minute slices across turns.
- `search` is a regex/substring full scan over `events.text`/`stdout`/`stderr` (no FTS index); `-i` is case-insensitive. `--project P` matches EITHER path column of `sessions` (`project LIKE ? OR cwd LIKE ?`, the same substring match on both): `project` is the lossy dir-name decoding and `cwd` the exact launch dir the Prompts tab shows, so the project name on screen is the name that filters. Matching `project` alone meant `--project claude-trail` found nothing.
- `tokens` defaults to per-session totals; passing `--session` switches the default `--by` to `command`.
- `prune-raw` clears the verbatim `raw_events` payload for every line except file-history snapshots and malformed ones (the `type <> '...'` filter is NULL-safe on purpose, so a malformed row keeps its raw), then compacts (`--no-compact` leaves the freed blocks for a later `--compact`). Typically ~2/3 of the store, and it breaks no feature: `search`/`tokens`/`cat`/`diff` read the derived tables, and the transcripts on disk stay the source of truth. `ingest` no longer writes those payloads, so this is a one-shot cleanup of rows written before that change.
- `prompts` is the Prompts tab as plain text, over the same read-only path. The LIST prints one line per main-thread turn, newest first, with the tab's own numbers: `time`, `sess` (8 chars), `project`, `calls` with the Bash share, `agents` (blank when none), `tokens` with the subagent part, and the first prompt line through `slash_label`. `--limit` defaults to 30 and is FILLED, not approximated: `_list_turns` starts the raw scan at `--limit` (never below `PROMPT_TURN_RAW_SCAN`, whose cost is the measured one) and doubles it towards the raw turn count until `limit` rows come back or the scan covers every candidate, because the noise filter drops turns AFTER the scan and no single scan size makes `--limit` mean what it says. `--session` takes an id prefix (an ambiguous one is exit 2 naming the candidates); `--since` is a lower bound applied INSIDE the query. Column widths come from the content, never from the terminal, so a pipe prints what a tty prints, and `--json` is the query's own dicts (str/int/bool/None only, no `default=` stringifier).
  - ONE TURN: `--at ISO --session S` resolves the newest turn AT OR BEFORE the bound, exactly for a full-precision `ts` pasted from `--json` and widened to the whole second for a time copied from the text list (both rules live in one SQL predicate, decided by the bound's own shape). `--at` requires `--session`, because turns from different sessions interleave. `--event-id E` (the id `--json` prints) reads the session off the event and therefore REFUSES `--session`; `--at` and `--event-id` refuse each other, and both refuse `--since`/`--limit`, which bound the list. The dump is an identity/work/tokens header (the Work line subtracts subcalls, exactly as the TUI's does), the quoted preview capped at 10 lines with both cuts stated, then every call oldest-first as `time agent tool detail`, where `agent` is `main` or the subagent's 4-char id and a failed call ends in `[error]` (marked at the END so the columns stay aligned and `grep '\[error\]'` finds all of them).
  - Terminal control characters in stored text are stripped from the TEXT output (a pasted `\x1b[2J` in a prompt would otherwise be obeyed by the terminal reading the list; tab becomes a space so a cell's printed width stays its `len`) and kept in `--json`, where `json.dumps` escapes them. Exit codes: 2 for a refused argument - including an unknown flag, unlike the older `search`/`tokens` parsers, because `--sesion X` quietly listing every session is worse than a usage line - 1 for a missing store or an unresolved turn, 0 otherwise, including an empty list, which is an answer and not a failure.

### Data model

- **Lossless `raw_events` layer.** Every transcript line is stored verbatim first (all event types, byte-for-byte, malformed lines keyed by content hash). `events`, `api_requests`, `file_versions`, and `sessions` are derived projections of it: drop them and they rebuild from `raw_events`; delete the source transcripts and nothing is lost.
- **Append-only events.** A tool call (`tool_use`) and its result (`tool_result`) are **separate rows** joined on `tool_use_id`, never updated in place (so re-ingest stays idempotent). The `actions` view stitches each call back together with its result.
- **Turn model.** A turn is a MAIN-THREAD user prompt (`agent_id IS NULL`) plus every event in the same session from that prompt's timestamp up to the next prompt's (`lead(ts)`), open-ended for each session's newest. Subagent work lands inside the window automatically: a subagent's calls carry the parent `session_id` and a non-null `agent_id`, which is also what splits the two apart on the turn page. A subagent's OWN turns never become rows (that is its task prompt, which belongs under its parent's turn), a mid-turn user message starts a NEW group, and a turn with zero tool calls is real (a question, a `/login`, a resumed session). Harness-written turns are dropped by `TURN_NOISE_RE`, and several kept turns sharing one `(session_id, ts)` - an image plus its caption, a `<command-name>` block plus a bare `/compact` - collapse to the longest cleaned text, because left alone they produce phantom zero-width windows. The list query cleans only the newest `PROMPT_TURN_RAW_SCAN` (400) raw turns before windowing, which is load-bearing: cleaning every main-thread turn of a real store (5,433 of them) costs 1.96s against 74ms for the newest 400, and the newest-N set is suffix-closed per session, so every window is still bounded correctly. `find_turn(con, session_id=None, at=None, event_id=None)` resolves ONE turn through the same query with a different pick, so a turn opened out of the list can never disagree with it about where that turn starts or ends.
- **`sessions.cwd` is the session's LAUNCH dir.** The first top-level `cwd` on the MAIN transcript, written once and never rewritten (`_upsert_session` uses `coalesce(sessions.cwd, excluded.cwd)`), so it does NOT follow a `cd`: Claude Code rewrites the top-level cwd after one, and 12 of the newest 40 transcripts here carry several (up to 27 distinct values, including a `/tmp/.../scratchpad`), which would otherwise drift the Project column mid-session and flip it on every sweep. Subagent transcripts never contribute - a subagent runs under the PARENT session id but in its own cwd - and an incremental read that started mid-file writes no cwd at all, since its first cwd is whatever the run had `cd`-ed to by then. It exists because `sessions.project`, decoded from the transcript DIR NAME, spends `-` on both the separator and any `-` inside a directory's own name, so it renders `claude-trail` as `…/claude/trail` and `personal-fin` as `…/personal/fin` - paths that do not exist, and nothing says which dashes were separators, so the lossy half cannot be recovered. `cwd` is therefore preferred and `project` kept as the fallback. NULL is TWO cases, not one: the transcript has left the disk, or it is still there and carries no `cwd` in its first `SESSION_CWD_SCAN_LINES` (50) lines (10 of the 127 transcripts here, all sub-300-byte stubs).
- **Upgrade path for `sessions.cwd`: nothing to run.** The next ordinary `claude-trail ingest` (Stop hook or the 30s timer) calls `backfill_session_cwd` IN PLACE, right after the transcript sweep and before the file-history pass, and logs `sessions: N cwd backfilled` on the run that fills any (silent afterwards). It reads only the HEAD of a transcript whose session row is still NULL, so it is cheap and idempotent: measured on the real store, 117 rows filled in 0.142s on the upgrade run, then 2.8-3.0ms per sweep. `--file-history-only` skips it (that mode touches no transcript). Expect it to fill only the sessions whose transcript is still on disk: 117 of 1,703 rows here, the other ~1,586 staying NULL forever and rendering from `project`. **Do NOT tell anyone to `reset-db` for this.** On a store older than its transcripts that is lossy, not slow: it would delete 1,703 sessions to rebuild the 127 whose transcripts remain, and `raw_events` cannot answer it either (the verbatim payload is kept only for file-history-snapshot lines), so the transcripts on disk are the only source in both cases.

### Token accounting

The API bills per request, not per tool call, so the store is explicit about exact vs derived:

- `api_requests` is one row per API request, deduped on `request_id` (a request is written as several content-block lines with identical usage). Synthetic messages (`model == "<synthetic>"`) are skipped. Subagent transcripts are ingested under the parent `sessionId`, and `agent_id` splits main-thread vs subagent spend.
- `session_tokens` view: **exact** per-session totals.
- `command_tokens` view: **exact generation cost per command** (the `output_tokens` of the request that emitted the `tool_use`, including any thinking/text in the same message). When one request emits N parallel tool calls it exposes `calls_in_request` and an even `output_tokens_share` split.
- `request_context` view: **read-back approximation.** A command's output is paid for as input on the next request; this view reports each request's context size and its growth over the previous request in the same (session, agent) thread. It goes negative across a compaction boundary, which is real signal, not an error.

### File reconstruction

- Edits are captured as content-addressed **blobs** (full file content, deduped by `sha256`) plus the `structuredPatch` diff, in `file_versions` (`pre_sha`/`post_sha`/`patch`). Each row carries a `source` (`'transcript'` | `'file-history'`) provenance marker.
- `cat <path> [--at ISO]` **rewinds** a file: it reconstructs the file's exact bytes as of `--at` (or latest), so you can recover or roll back to an earlier version. `diff <path>` lists that path's edits oldest-first with their patches.
- **Two content sources.** Transcripts alone give full content for only ~39% of edits (those whose `originalFile`/`content` is present); the rest are patch-only. The ingest sweep also backfills `~/.claude/file-history/<session_id>/<pathhash>@vN` snapshots (Phase 8), which lifts full-content coverage to ~96% of the patch-only paths (measured: 123/129 patch-only paths gain reconstructable content).
  - The authoritative `(path, version, timestamp)` mapping comes from the `file-history-snapshot` events' `snapshot.trackedFileBackups` (already in `raw_events`); the `sha256(abs_path)[:16]` reverse-index only absolutizes relative keys and handles orphaned dirs. Snapshot events carry **no** top-level `sessionId`/timestamp, so the session id is derived from the transcript filename and the timestamp from `backupTime`.
  - The new **`file_history`** table (`session_id, pathhash, version, path, sha, snapshot_ts, note`, PK `(session_id, pathhash, version)`) records every snapshot. Snapshot **bytes are preserved as blobs even when the path is not yet resolvable** (file-history is the one store input not rebuildable from `raw_events`, so this is irreversible-loss insurance); deferred resolution fills `path` on later sweeps as more edits are seen. Resolved rows also emit synthetic `source='file-history'` `file_versions` rows (`event_id = 'fh:<sid>:<pathhash>@v<N>'`) so `cat` sees them. `pre_sha` is the highest **present** lower version's blob (chains are non-contiguous; never `v-1`). Non-UTF-8 snapshots (0 today) are recorded with `sha=NULL, note='binary-skipped'`, no blob.
  - `cat` reads all sources; `diff` stays transcript-only by default (a snapshot spans a whole user turn, so including it would double-list every edit and spam `(no patch)` for headless first snapshots), `--all-sources` opts the file-history deltas in. `stats` adds a reconstruction-coverage block (cat-able paths, split by source, plus file_history row/byte counts), guarded via `information_schema` so a not-yet-migrated 0.6.0 DB opened read-only does not raise.
  - **`cat` roll-forward:** snapshots are per-user-turn checkpoints of the pre-message state, so a file last edited in a session's final turn has no snapshot of its final bytes; `cat` used to be one turn stale there. It now rolls trailing patch-only edits forward: after picking the newest bytes-bearing version it applies any newer `structuredPatch` rows in order via `_apply_structured_patch` (STRICT: every context/deletion line must match at the stated position; the `patch` column holds the hunks as JSON). A patch that no longer applies cleanly (file changed outside Claude, interleaved session) stops the chain with a `stale by N edit(s)` warning on stderr and prints the last consistent bytes -- stale-but-consistent, never a half-applied mix.
- **Migration:** `SCHEMA_VERSION` is `"2"`. `connect()` (writable path only) runs `ALTER TABLE file_versions ADD COLUMN IF NOT EXISTS source ...`, creates `file_history`, and seeds the version with `ON CONFLICT (key) DO UPDATE` (the old `DO NOTHING` would have pinned an existing 0.6.0 DB at `'1'`), so a 0.6.0 store upgrades in place on the next `ingest`.
