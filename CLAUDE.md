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
| `claude_trail.py` | Module: TUI (`main`) and PostToolUse hook (`hook_main`). Tails `command-log.jsonl`, renders 6-column table with cursor navigation, column toggles, and action keys |
| `hook.sh` | Legacy bash version of the hook (deprecated, kept for users who still reference it). New installs should use `claude-trail hook`. |
| `pyproject.toml` | Package metadata and `claude-trail` entry point |
| `requirements.txt` | Python deps (`rich>=13.0`) |

## Key Paths

- **Config dir:** all paths derive from `CONFIG_DIR = $CLAUDE_CONFIG_DIR` when set, else `~/.claude`. `LOG_PATH`, `SESSIONS_DIR`, and `PROJECTS_DIR` are module-level constants built from it (so tests can `patch.object` them, and a relocated config still resolves).
- **Log file:** `~/.claude/command-log.jsonl` (`CONFIG_DIR/command-log.jsonl`)
- **Hook config:** `~/.claude/settings.json` → `hooks.PostToolUse`
- **Session metadata:** `~/.claude/sessions/<pid>.json` (read by the TUI to resolve session names)

## Hook Setup

Register in `~/.claude/settings.json` using the built-in subcommand:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "claude-trail hook" }
        ]
      }
    ]
  }
}
```

## Running

```bash
claude-trail              # if installed via pipx / uv tool install
python3 claude_trail.py   # if running from a clone
```

## Controls

| Key | Action |
|-----|--------|
| `j`/`k` or `↓`/`↑` | Move cursor down/up |
| `g`/`G` | Jump to top/bottom |
| `Enter` | Open the full-command detail view for the selected command |
| `s` | Open the session-detail modal for the selected row's session (identity header + subagent list with live status) |
| `d` | Open the audit-store database modal: connection string, DuckDB / schema version, on-disk sizes, block accounting, and per-table row counts (opens the DuckDB store read-only, on demand) |
| `Esc` | Close the open modal, command detail, session detail, or database (also `q` or `Enter` while it is open) |
| `o` | Open selected session's commands (filtered JSONL) in `$VISUAL` or the platform default launcher |
| `f` | Open file manager on folder of files referenced in selected command |
| `/` | Open per-session recursive search for the selected row's session. Type a query then `Enter` to run; `Tab` toggles the root between the transcript folder (`<sid>.jsonl` + `subagents/**`) and the session's cwd. In results: `j`/`k` browse, `Enter` opens the hit at its line, `f` opens the hit's folder, `/` edits the query, `Esc`/`q` close |
| `1`-`6` | Toggle columns: 1=Time, 2=Session, 3=Directory, 4=Files, 5=Command, 6=Agent |
| `c` | Clear display |
| `q` | Quit |

## Columns

Columns are data-driven: a single ordered `COLUMNS` list of frozen `Column`
records is the one source of truth. A `Column` has `key` (the stable digit
1..6 used by the toggle keys and the `1 2 3 4 5 6` status bar), `name`, `style`,
`kwargs` (passed to `Table.add_column`), and `render`. `render(entry, ctx)`
returns that column's cell for one log entry as a `str` or `rich.text.Text`.
Adding a column is a single entry in the list.

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

## Conventions

- Log format is JSONL with fields: `timestamp`, `command`, `cwd`, `session_id`, plus optional `agent_id`/`agent_type` on a subagent's commands (PostToolUse adds them only inside a subagent, so an absent `agent_id` means the main agent). Modeled as the `LogEntry` TypedDict; annotation only, `parse_line` still returns a plain dict at runtime. `hook_main` builds each line via `_new_entry()` to fix the key order.
- The feed attributes each row to the agent that ran it via the forward-only hook `agent_id`: `load_agent_labels(session_ids)` maps `agent_id -> {type, description}` from `PROJECTS_DIR/*/<sid>/subagents/**/agent-*.meta.json` (cached `AGENT_LABEL_CACHE_TTL` = 2s, accumulating), and `agent_label(entry, agent_map)` / `entry_agent_id(entry)` resolve a row's label (`None` for the main agent; otherwise `<short-id> <description>`, the id first so distinct agents with the same description stay distinguishable). The Agent column (`_render_agent`, `key=6`) draws an in-place tree gutter over the time-ordered feed: `build_table` marks each row's position in its consecutive `(session_id, agent_id)` run (`run_first`/`run_last`) and picks the glyph (`─`/`┌`/`│`/`└`), showing the label once per run; rows stay one-per-row so cursor/scroll/tail math is unchanged. Attribution is forward-only (rows logged before the hook enrichment, and every main-agent row, render blank), while the session modal (`s`) is retroactive: it reconstructs every subagent from disk regardless of when the row was logged.
- Dangerous commands (rm, sudo, git reset, chmod, etc.) are flagged with red `*` prefix
- Display shows newest-first, max 50 entries, with active session count = distinct `session_id`s seen within `ACTIVE_WINDOW_SECONDS` (300s), computed by `count_active_sessions(entries, now, window=ACTIVE_WINDOW_SECONDS)`
- Poll interval: 300ms
- File paths extracted by `find_paths(cmd)` (regex matching absolute `/...`, home `~/...`, relative `./...`; order-preserving dedupe via `dict.fromkeys`). `extract_files()` is a thin basename formatter over it and `open_file_folder()` reuses it.
- `Enter` opens an in-TUI detail view (`build_detail_panel()`) showing the selected command in full: untruncated, newlines preserved, with the danger `*` marker and a metadata header (time, session, directory, files). `Esc`/`q`/`Enter` close it. The view is modal: while open, navigation/column/clear keys are ignored (only `Ctrl-C` still quits).
- `s` opens the session-detail modal (`build_session_panel()`) for the selected row's session: an identity header (id, name tinted by the session `/color` plus its live status, `~`-abbreviated folder, transcript path, version, start time) and a table of the session's subagents (status glyph `●` running / `✓` done / `✗` stopped, description, agent type, tool-call count shown as `N+` when byte-capped). It reads `load_session_model(sid)` (cached `SESSION_MODEL_CACHE_TTL` = 2s), which assembles the model from the live `sessions/<pid>.json`, the parent transcript's `<task-notification>` completion events, and each `subagents/**/agent-*.meta.json`; status is derived (completed task-id -> `done`, failed/killed -> `stopped`, else `running` when the session is live else `stopped`). Both modals are mutually exclusive: `AppState.modal_open()` is true while either is open, `apply_key` swallows everything but `Esc`/`q`/`Enter` (which clear both fields) and `Ctrl-C` (quit), and `render_panel` shows the session modal in precedence over the command modal.
- `/` opens the per-session search modal (`SearchState` on `AppState.search`, rendered by `build_search_panel`). Roots come from `search_roots(sid, entry, model)`: `"transcript"` = the `<sid>.jsonl` file plus its sibling `<sid>/` dir (`subagents/**`), `"cwd"` = the live session's launch dir (pid.json) or, for an ended session, the row's cwd; a non-existent path is dropped and `Tab` switches the active root. `run_search(query, paths)` shells out to `rg` when present (`-S` smart-case) else `grep -rInE`, passing the query as argv (never through a shell), capped at `SEARCH_RESULT_LIMIT` (500) with a `SEARCH_TIMEOUT` (5s) so a huge tree can't hang the UI; rg/grep exit 1 (no match) is empty, exit >= 2 (bad regex) returns an `error` string, never raises. The modal has two sub-modes (`INPUT` types the query, `RESULTS` browses hits) so letters and `j`/`k` never conflict, and it is mutually exclusive with (and takes precedence over) the command/session modals: `apply_key` checks `state.search` first and stays pure, returning `RUN_SEARCH`/`OPEN_MATCH`/`OPEN_MATCH_FOLDER` for `main()` to perform. `OPEN_MATCH` reuses `open_session_log`'s suspend-`Live` editor pattern (with a `+<line>` arg for vi-family editors); `OPEN_MATCH_FOLDER` reuses `open_file_folder`'s detached `Popen(_platform_opener())` via `open_containing_folder`.
- Session JSONL written under `tempfile.gettempdir()` (default `/tmp`) as `claude-trail-session-{sanitized-id}.jsonl` on `o`. If `$VISUAL`/`$EDITOR` is set, `open_session_log()` suspends the `Live` display, restores the terminal, runs that editor in the foreground (so terminal editors like nvim get the tty), then resumes; otherwise it hands the file to the GUI launcher detached.
- Arrow keys are read in both CSI (`ESC [ A`/`B`) and SS3 (`ESC O A`/`B`, application-cursor-keys mode) forms by `read_key()`, which parses one complete CSI sequence per call (parameter bytes, one final byte) so modified arrows (`ESC [ 1 ; 5 A`) and function keys never leak stray bytes into the key dispatch; a final byte of `A`/`B` navigates regardless of modifier parameters.
- Panel title shows the installed version (`claude-trail vX.Y.Z`), read from package metadata via `importlib.metadata`; blank when run from a clone without an install.
- Session names are read from `~/.claude/sessions/<pid>.json` (`name` field). Cached for 2s in `load_session_names()`; the cache accumulates so a session's name remains resolvable after Claude Code removes its pid.json on exit.
- Session color comes from the latest `/color <value>` event in `~/.claude/projects/*/<session-id>.jsonl` (`system/local_command` events). `load_session_colors()` tails each transcript incrementally and refreshes at most every 5s. Names like `orange`/`pink`/`gray` are translated to Rich-compatible equivalents (`orange1`, `pink1`, `grey50`) via `CLAUDE_COLOR_ALIASES`.
- Columns are defined once in the module-level `COLUMNS` list of frozen `Column` records (`key`, `name`, `style`, `kwargs`, `render`). `build_table` builds a base `RenderCtx(name_map, color_map, agent_map)`, computes each row's `(session_id, agent_id)` run boundaries, and per row `dataclasses.replace`s the base ctx with that row's `agent_label`/`run_first`/`run_last` before calling `col.render(entry, ctx)` for each visible column in list order; there is no per-column `if col_id == ...` ladder. `build_detail_panel` reuses `_render_session` for the Session tint and `_danger_prefixed` for the Command danger `* ` marker (it keeps the raw, un-normalized command so newlines survive). Default visibility is derived from the list (`{c.key: True for c in COLUMNS}`); the digit-toggle branch in `apply_key` accepts `ch.isdigit() and int(ch) in visible_cols` and still refuses to hide the last visible column.
- Column visibility persists during session, status bar shows toggle state as `1 2 3 4 5 6`
- Platform-specific file launcher: `xdg-open` on Linux, `open` on macOS, selected via `claude_trail._platform_opener()`. Honours `$VISUAL` first if set.

## Audit store

`claude_trail_store.py` is a separate, DuckDB-backed audit trail. It ingests the Claude Code transcripts (`~/.claude/projects/**/*.jsonl`, including `subagents/agent-<id>.jsonl`) into one local file and answers search / token / file-reconstruction queries over the full history. It is independent of the live TUI: `claude_trail.py main()` only lazily imports it when the first argument is one of the store subcommands (so the TUI and hook never load `duckdb`). The one interactive exception is the TUI `d` (database) modal, whose `_load_db_info()` imports the store and opens it read-only on demand; `db_info(con)` returns the (label, value) rows shared by that modal and the `stats` CLI section (connection string, DuckDB / schema version, on-disk sizes, block accounting, per-table counts).

- **Store file:** `~/.claude/trail.duckdb`, overridable via `$CLAUDE_TRAIL_DB` (constant `STORE_PATH`). `connect(read_only=...)` opens it; the parent dir is created on demand.
- **Reads transcripts, not the hook.** The store ingests transcripts, so it covers every tool (Read, Edit, Write, Grep, Task, ...) and gets each call's result and edit patch for free. The live TUI is unchanged and still tails `command-log.jsonl`, which the PostToolUse hook writes and which is Bash-only. Two independent data paths: the hook feeds the TUI, the transcripts feed the store.

### Subcommands

Invoked as `claude-trail <cmd>` (installed) or `python3 claude_trail.py <cmd>` (from a clone):

```
ingest [--watch] [--compact] [--file-history-only]
search <regex> [--tool T] [--session S] [--project P] [--since ISO] [--limit N] [-i]
stats
tokens [--session S] [--by session|command|tool|day|model] [--since ISO] [--limit N]
cat <path> [--session S] [--at ISO]
diff <path> [--session S] [--all-sources]
```

- `ingest` is the **single writer**: it holds DuckDB's one read-write connection, tails each transcript from its last byte offset, and is idempotent (re-ingest inserts nothing). It sweeps transcripts first, then backfills file-history (see below). Run it from cron or leave one `--watch` process polling every 5s. Every other subcommand (and any ad-hoc reader) opens the file **read-only**; the hook never writes DuckDB (many short-lived hook processes would contend on the write lock). `--compact` runs once after the sweep (ignored under `--watch`) and reclaims DuckDB's interspersed free blocks by rewriting the store into a fresh file (a plain `CHECKPOINT` never coalesces them, so a bulk ingest otherwise leaves the file ~2x its packed size). `--file-history-only` runs just the file-history backfill pass (does not touch `ingest_state`; composes with `--compact`).
- `search` is a regex/substring full scan over `events.text`/`stdout`/`stderr` (no FTS index); `-i` is case-insensitive.
- `tokens` defaults to per-session totals; passing `--session` switches the default `--by` to `command`.

### Data model

- **Lossless `raw_events` layer.** Every transcript line is stored verbatim first (all event types, byte-for-byte, malformed lines keyed by content hash). `events`, `api_requests`, `file_versions`, and `sessions` are derived projections of it: drop them and they rebuild from `raw_events`; delete the source transcripts and nothing is lost.
- **Append-only events.** A tool call (`tool_use`) and its result (`tool_result`) are **separate rows** joined on `tool_use_id`, never updated in place (so re-ingest stays idempotent). The `actions` view stitches each call back together with its result.

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
  - **`cat` staleness caveat:** snapshots are per-user-turn checkpoints of the pre-message state, so a file last edited in a session's final turn has no snapshot of its final bytes; `cat` can be one turn stale. Applying the last transcript `structuredPatch` on top of the newest snapshot for exactness is deliberate future work.
- **Migration:** `SCHEMA_VERSION` is `"2"`. `connect()` (writable path only) runs `ALTER TABLE file_versions ADD COLUMN IF NOT EXISTS source ...`, creates `file_history`, and seeds the version with `ON CONFLICT (key) DO UPDATE` (the old `DO NOTHING` would have pinned an existing 0.6.0 DB at `'1'`), so a 0.6.0 store upgrades in place on the next `ingest`.
