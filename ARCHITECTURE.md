# Architecture

A tour of how `claude-trail` works internally, for contributors. The tool is one
Python file (`claude_trail.py`) with one runtime dependency (`rich`). This
document explains the data flow, how the file is laid out, and the two places
you are most likely to extend it.

## Data flow

```
Claude Code session
  |
  |  runs a Bash tool call
  v
PostToolUse hook  ("claude-trail hook")   <- hook_main()
  |
  |  appends one JSON line per Bash command
  v
CONFIG_DIR/command-log.jsonl              <- LOG_PATH
  |
  |  tailed incrementally (tail_file)
  v
TUI  ("claude-trail")                     <- main()
  |
  |  folds new lines into AppState (AppState.ingest)
  v
Rich render (build_panel / build_detail_panel)
```

Every path derives from `CONFIG_DIR`, which is `$CLAUDE_CONFIG_DIR` when set and
`~/.claude` otherwise. `LOG_PATH`, `SESSIONS_DIR`, and `PROJECTS_DIR` are
module-level constants built from it, so a relocated Claude Code config still
resolves the log, session names, and `/color` tints, and tests can
`patch.object` them.

Each log line is one JSON object with four keys (`timestamp`, `command`, `cwd`,
`session_id`), modeled by the `LogEntry` TypedDict. The TypedDict is annotation
only: `parse_line` returns whatever JSON was on the line (a plain dict at
runtime), so it documents the expected shape rather than enforcing a schema.

## Module map

The single file is split into `# ==== <name> ====` section banners, in this
order:

| Section | Contents |
|---------|----------|
| Config / paths | `CONFIG_DIR`, `LOG_PATH`, `SESSIONS_DIR`, `PROJECTS_DIR`, tuning constants, `LogEntry` |
| Danger + path patterns | `DANGEROUS_PATTERNS`, `DANGEROUS_REDIRECT`, `QUOTED_SPAN_RE`, `FILE_PATH_RE` |
| Columns | `RenderCtx`, `Column`, the per-column `_render_*` callables, the `COLUMNS` list |
| Session /color parsing + aliases | `COLOR_CMD_RE`, `CLAUDE_COLOR_ALIASES`, `rich_color` |
| Danger detection + path extraction | `is_dangerous`, `find_paths`, `extract_files`, `short_path` |
| Session names | `load_session_names`, `session_label`, plus the name cache globals |
| Session colors | `_find_transcript`, `_scan_chunk_for_color`, `load_session_colors`, plus the color cache globals |
| Format / parse helpers | `_platform_opener`, `normalize_cmd`, `parse_line`, `format_time` |
| Rendering: table | `get_display_entries`, `build_table` |
| Side-effecting actions | `filter_session_log`, `open_file_folder` |
| Rendering: panels | `_panel_title`, `build_detail_panel`, `visible_row_count`, `count_active_sessions`, `build_panel` |
| Input decoding (read_key) | `_read_pending_byte`, `read_key` |
| File tailing | `tail_file`, `read_last_entries` |
| Hook | `_new_entry`, `hook_main` |
| AppState + apply_key | `Action`, `AppState`, `apply_key` |
| Main | `_signal_exit`, `main` |

## Hook vs TUI split

`main()` has two roles selected by `sys.argv`:

- `claude-trail hook` dispatches to `hook_main()`. It reads one PostToolUse
  event from stdin, and if `tool_name == "Bash"` it appends a log line built by
  `_new_entry()` (which fixes the key order). It never touches the terminal, so
  it is cheap to run on every Bash call. Malformed input (bad JSON, a non-Bash
  event, missing fields) returns `0` rather than raising, so a bad event never
  breaks Claude Code's tool pipeline.
- `claude-trail` with no subcommand runs the TUI: read the tail of the log,
  build an `AppState`, put the terminal into cbreak mode, then loop over
  poll / drain-keys / tail / clamp / render.

The two share the file format and the pure helpers, nothing else.

## The render loop

`main()` reads the last `MAX_ENTRIES` lines with `read_last_entries` (a
backward chunked scan), seeds an `AppState`, and enters a `Live` loop:

1. `select()` on stdin for up to `POLL_INTERVAL` (300 ms).
2. Drain every buffered keystroke with `read_key`, feeding each to
   `apply_key(state, ch, max_rows)` and acting on the returned `Action`.
3. `tail_file` reads new complete lines; `AppState.ingest` parses and appends
   them (a partial trailing line is withheld until its newline arrives).
4. `AppState.clamp(max_rows)` keeps the cursor on screen after input, new
   lines, or a terminal resize.
5. Refresh the session name/color maps, then `live.update(render_panel())`.

Rendering is side-effect-free (`build_panel` reads the wall clock once for the
active-session count, but writes nothing): it takes state and returns a Rich
`Panel`, so both `build_panel` and `build_detail_panel` are unit-testable
against a recording console with no terminal.

### Cursor / offset contract

Entries are stored oldest-first (append order). `cursor` and `scroll_offset`
index the newest-first VIEW, where index 0 is the newest entry.
`AppState.selected_entry()` is the single place that bridges the two by
reversing `entries`. In `build_table`, the highlighted row is at
`highlight_idx = cursor - offset`; when the cursor scrolls out of view that
index falls outside the displayed slice and no row is highlighted.

## Detail-view modality

Pressing `Enter` sets `state.detail_entry` to the selected entry.
`render_panel()` then renders `build_detail_panel` instead of `build_panel`:
a full-screen view of one command, untruncated with newlines preserved, a
metadata header (time, session, directory, files), and the danger `*` marker.
While `detail_entry` is set the view is modal: `apply_key` short-circuits at
the top and ignores navigation, column, and clear keys. Only `Esc`, `q`, or
`Enter` (which clear `detail_entry`) and `Ctrl-C` (which quits) do anything.

## Session-name caching

`load_session_names()` maps `session_id -> name` from
`~/.claude/sessions/<pid>.json` (Claude Code writes one JSON file per running
session, each with a `sessionId` and optional `name`). Claude Code removes that
file when the session exits, so the cache accumulates: once a name has been
seen it stays resolvable after the pid file is gone. The map is cached for
`SESSION_NAME_CACHE_TTL` (2 s) to avoid hitting the filesystem on every render.
`session_label` falls back to the first 8 characters of the session id when no
name is known.

## Session-color parsing

`load_session_colors()` maps `session_id` to the color the user picked with
Claude Code's `/color` slash command. It reads each session transcript at
`~/.claude/projects/*/<session-id>.jsonl` and looks for `system` /
`local_command` events whose content matches `COLOR_CMD_RE` (the
`<command-name>/color</command-name> ... <command-args>VALUE</command-args>`
shape), keeping the last value seen. Transcripts are tailed incrementally
(`_transcript_positions` remembers each file's last-scanned byte offset) so the
cost stays flat as transcripts grow, and the result is cached for
`SESSION_COLOR_CACHE_TTL` (5 s).

Claude Code's `/color` accepts plain names like `orange`, `pink`, and `gray`
that Rich's color parser rejects (it wants X11-style names such as `orange1`).
`rich_color()` translates those through `CLAUDE_COLOR_ALIASES` so the Session
column tint matches what the user sees in Claude's own session tag; unknown
values pass through unchanged.

## read_key: CSI / SS3 escape parsing

`read_key(fd)` reads keypresses with `os.read` (not buffered `sys.stdin`),
because the poll loop calls `select()` on the same fd and Python's buffered
text stdin would read ahead into a buffer that `select()` cannot see, dropping
the trailing bytes of arrow sequences. On a lone `\x1b` it consumes exactly one
complete escape sequence:

- CSI: `ESC [` then parameter/intermediate bytes (`0x20`-`0x3F`) then one final
  byte (`0x40`-`0x7E`). This covers plain arrows (`ESC [ A`), modified arrows
  (`ESC [ 1 ; 5 A`), and function keys (`ESC [ 1 5 ~`).
- SS3: `ESC O` then one final byte (`ESC O A`), the application-cursor-keys
  form.

Reading one sequence at a time (rather than a greedy fixed-size read) leaves
any following sequence on the fd for the next call, so a held arrow key does not
drop keystrokes. A final byte of `A` maps to `"up"` and `B` to `"down"`,
regardless of modifier parameters; anything else returns `"\x1b"`.

## _platform_opener

`_platform_opener()` returns the platform's default file launcher: `open` on
macOS (`sys.platform == "darwin"`), `xdg-open` elsewhere. The `f` action
(`open_file_folder`) and the no-editor branch of the `o` action
(`open_session_log`) both launch through it, detached via `start_new_session`.
The `o` action prefers `$VISUAL` / `$EDITOR` when set and interactive: it
suspends the `Live` display, restores the terminal, runs the editor in the
foreground so a terminal editor like nvim gets the tty, then resumes.

## Extension points

These are the two spots a contributor is most likely to touch.

### Adding a table column

Columns are data-driven: the ordered `COLUMNS` list is the single source of
truth. A `Column` is a frozen dataclass with `key` (the stable digit 1..5 used
by the toggle keys and the `1 2 3 4 5` status bar), `name`, `style`, `kwargs`
(passed to `Table.add_column`), and `render`. `render(entry, ctx)` returns that
column's cell for one log entry as a `str` or `rich.text.Text`. `ctx` is a
frozen `RenderCtx` carrying the per-render `name_map` and `color_map`; render
callables call the module-level helpers (`format_time`, `session_label`,
`rich_color`, `short_path`, `extract_files`, `is_dangerous`, `normalize_cmd`)
directly.

To add a column, write a `_render_*` callable and append one `Column(key, name,
style, kwargs, render)` to `COLUMNS`. Everything else follows: `build_table`
loops the visible columns in list order calling `col.render(entry, ctx)` (there
is no per-column `if key == ...` ladder), default visibility is derived from the
list (`{c.key: True for c in COLUMNS}`), and the digit-toggle branch in
`apply_key` accepts `ch.isdigit() and int(ch) in visible_cols`. Use the next
free digit as the `key` so the toggle key and status bar pick it up.

### AppState + apply_key state machine

`AppState` is a dataclass holding all view state (`entries`, `cursor`,
`scroll_offset`, `visible_cols`, `detail_entry`), independent of terminal I/O.
Its methods are the state transitions: `move_up` / `move_down`, `goto_top` /
`goto_bottom`, `toggle_col` (which refuses to hide the last visible column),
`ingest` (append + re-anchor the cursor when scrolled away + trim to
`MAX_ENTRIES`), `clamp` (keep cursor and scroll on screen), and
`selected_entry`.

`apply_key(state, ch, max_rows) -> Action` is the keypress dispatcher. It
mutates `state` but spawns no processes and touches no terminal, so it is fully
unit-testable. It returns an `Action` enum for the outcomes `main()` must
handle outside pure state mutation: `QUIT`, `OPEN_SESSION` (the `o` action),
`OPEN_FILES` (the `f` action), or `NONE`. The process-spawning branches stay
thin wrappers in `main()`; the transition itself stays pure.

To add a key binding, add a branch to `apply_key`. If it is pure view state,
mutate `state` and return `Action.NONE`. If it needs a side effect (spawning a
process, touching the terminal), add an `Action` variant and handle it in the
`main()` drain loop.

## Tests

`tests/test_claude_trail.py` covers the pure helpers, the renderers (against a
recording `Console`), input decoding, file tailing, and the `AppState` /
`apply_key` state machine. `tests/conftest.py` has an autouse fixture that
clears every module-level cache global before each test; any new cache global
added to `claude_trail.py` must also be reset there.
