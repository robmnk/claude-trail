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
| `Esc` | Close the open modal, command detail or session detail (also `q` or `Enter` while it is open) |
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
| 3 | Directory | Abbreviated cwd (`short_path`) |
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
