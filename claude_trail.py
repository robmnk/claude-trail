#!/usr/bin/env python3
"""claude-trail: realtime TUI showing all Bash commands Claude Code executes.

Pipeline:
    A PostToolUse hook (`claude-trail hook`) appends one JSON line per Bash
    command to CONFIG_DIR/command-log.jsonl (default ~/.claude). The TUI tails
    that file, folds the new lines into an AppState (entries plus cursor/scroll/
    column view state), and Rich renders the state as a newest-first table or a
    full-command detail panel.

    That feed is the Commands tab. A second top-level tab (Prompts, `TABS`) lists
    one row per user prompt and is read from the audit store instead
    (`claude_trail_store`), which the store subcommands and the token/prompt
    modals already use; `Tab` switches between them and each keeps its own cursor.

Layout: the file is split into `# ==== <name> ====` banners, in file order:
    config/paths, danger + path patterns, columns + tabs, session /color parsing,
    danger detection + path extraction, session names, session colors,
    format/parse helpers, rendering (table), side-effecting actions,
    rendering (panels), input decoding, file tailing, hook,
    AppState (+ per-tab state) + apply_key, main.
"""

import enum
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Callable, TypedDict

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# ==== Config / paths ====

try:
    __version__ = pkg_version("claude-trail")
except PackageNotFoundError:  # running from a clone without an install
    __version__ = ""

# All state lives under CONFIG_DIR. Honor $CLAUDE_CONFIG_DIR so a relocated
# Claude Code config still resolves the log, session names, and /color tints.
CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
LOG_PATH = CONFIG_DIR / "command-log.jsonl"
SESSIONS_DIR = CONFIG_DIR / "sessions"
PROJECTS_DIR = CONFIG_DIR / "projects"


def _load_dotenv() -> dict[str, str]:
    """Parse KEY=VALUE lines from CONFIG_DIR/.env ($CLAUDE_TRAIL_ENV override).
    Blank lines and `#` comments ignored; surrounding quotes stripped. Missing
    file -> empty. Real os.environ is not mutated; callers layer it on top."""
    path = Path(os.environ.get("CLAUDE_TRAIL_ENV", CONFIG_DIR / ".env"))
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except (OSError, ValueError):
        pass
    return out


_DOTENV = _load_dotenv()


def _env_int(key: str, default: int) -> int:
    """os.environ wins, then .env, then default; non-int/<=0 falls back."""
    raw = os.environ.get(key, _DOTENV.get(key))
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


MAX_ENTRIES = 1000
POLL_INTERVAL = 0.3
CHROME_ROWS = 12  # title, tab strip + its blank, status, padding
# Same accounting for the Prompts tab, which adds one line the feed does not
# have: the snapshot header (`as of HH:MM:SS (12s ago)` plus the row counts and
# any store status). Everything else is identical, including the single slack
# line that absorbs the key hints wrapping to a second line on a narrow
# terminal - so like CHROME_ROWS this is the worst case, and a wide terminal
# spends one row on slack it does not need.
PROMPTS_CHROME_ROWS = CHROME_ROWS + 1
# Non-body lines around the search-results table (borders, padding, header/root
# rows, table header, blank spacers, hint); the results window is sized so the
# table body never overflows the panel and crops the cursor row out of view.
SEARCH_CHROME_ROWS = 11
# Same idea for the session modal's subagent table. The metadata header is
# variable-height (an optional Subagent row and an optional Tokens row on top of
# the six always-present rows), so this is the worst-case chrome: 2 borders +
# 2 vertical padding + 8 max meta rows + 1 blank + Subagents header + table
# header + 1 blank + hint. Sizing off the maximum keeps the hint on screen
# regardless of which optional meta rows are present (a few rows go unused when
# they are absent, same trade-off SEARCH_CHROME_ROWS accepts).
SESSION_CHROME_ROWS = 17
# Same idea for the full-prompt view drilled into from the command modal:
# 2 outer borders + 2 outer padding + 3 max meta rows + 1 blank + 1 header row +
# the body panel's own 2 borders + 2 padding. Prompt lines wrap, so a windowful
# can still outgrow its rows; the crop then eats body lines, never the header.
PROMPT_CHROME_ROWS = 14
# Same accounting for the turn-detail page (`↵` on the Prompts tab), whose body
# is an action table under a fixed header - but only the FIXED part of it:
# 2 outer borders + 2 outer padding + 3 meta rows (Session / Work / Tokens) +
# 1 blank + the Actions header + the table's own header row and rule + 1 hint.
# The prompt box on top of that is not a constant, because its height follows the
# prompt: `_prompt_box_rows` measures it, and `_turn_capacity` subtracts both.
# Reserving the box's 6-row worst case here instead (the old `18`) cost the list
# 3 rows on every one-line prompt, which is 135 of the newest 200 turns in a real
# store - a third of the usable rows at h=24. Measured by rendering, not counted:
# one row less crops the hint at every width, this keeps it from 76 columns up
# (below that the meta rows wrap and take the hint's row, the same narrow-terminal
# trade the other panels make).
TURN_CHROME_ROWS = 12
MIN_VISIBLE_ROWS = 3
# Lines of the triggering prompt shown in the command modal before it says how
# many are hidden; `Enter` opens the rest in the scrollable full-prompt view.
PROMPT_PREVIEW_LINES = 10
# The same for the turn page, which quotes the prompt above a table and so can
# spend far fewer rows on it.
TURN_PROMPT_PREVIEW_LINES = 2
# Cap on the prompt text the full-prompt view materializes. `turn_prompt_text`
# has none, and the longest main-thread turn in a real store is 646,245 chars (a
# summarization hook prompt), 30x the 21,697 max of the newest-400 window the
# list scans. The view re-splits that text into lines on every frame to window
# it, so an uncapped one would chew through half a megabyte four times a second.
# What is dropped is stated in the text itself, never silently cut.
PROMPT_TEXT_CHAR_CAP = 100_000
SESSION_NAME_CACHE_TTL = 2.0  # seconds
SESSION_COLOR_CACHE_TTL = 5.0  # seconds
AGENT_LABEL_CACHE_TTL = 2.0  # seconds
SESSION_MODEL_CACHE_TTL = 2.0  # seconds; keeps the modal's running->done live
AGENT_TOOLUSE_BYTE_CAP = 512 * 1024  # cap per agent transcript when counting tool_use
# Chars of the agent_id shown as an agent's distinct short identifier (feed +
# session modal). Placed first in the label so it survives the Agent column's
# ellipsis: two subagents sharing a description still read as distinct.
AGENT_ID_LABEL_LEN = 4
AGENT_STYLE = "cyan"  # tint of the Agent column, reused on the detail pages
# Directory column width; short_path left-truncates the cwd to this, keeping the
# distinctive tail (the actual dir) visible. Feeds both the Directory column and
# short_path's default, so overriding it here keeps that column consistent.
DIR_LABEL_WIDTH = _env_int("CLAUDE_TRAIL_COL_DIRECTORY_WIDTH", 14)
# The Prompts tab's Project column, same deal: `short_path` needs the width to
# left-truncate the project path, so the column and the helper read one constant.
PROJECT_LABEL_WIDTH = _env_int("CLAUDE_TRAIL_PROMPT_COL_PROJECT_WIDTH", 16)
# How stale the Prompts tab's snapshot may get before an idle frame re-reads the
# store. This is the ingest cadence (a systemd timer runs the ingest every 30s,
# see CLAUDE.md "Ingest cadence"), so a faster tick would spend a query to find
# nothing new - and would collide with the ingest's write lock more often.
PROMPTS_AUTO_REFRESH = 30.0
ACTIVE_WINDOW_SECONDS = 300  # a session counts as active if seen within this window
SEARCH_RESULT_LIMIT = 500  # max hits kept per search; the rest are dropped (capped)
SEARCH_TIMEOUT = 5.0  # seconds before rg/grep is killed so a huge tree can't hang the UI
# Editors that accept a `+<line>` argument to open a file at a line (vi family).
_VI_FAMILY = frozenset({"vi", "vim", "nvim", "view", "vimdiff", "nvi", "gvim"})

# Status/hint lines for a store that cannot answer. One definition each, so the
# modals, the prompts header and the empty states never drift apart in wording.
# Up here with the other constants rather than beside the loaders that produce
# them, because the turn page's Tokens row renders one directly.
NO_STORE_HINT = "no audit store yet - run: claude-trail ingest"
PENDING_INGEST_HINT = "pending ingest (store lags the live feed)"
# Matches the CLI's `store busy:` prefix for the same condition (a lock collision
# against the single writer), which is routine at the 30s ingest cadence.
STORE_BUSY_HINT = "store busy: an ingest is writing"
STORE_BUSY_KEPT_HINT = f"{STORE_BUSY_HINT} - kept the last snapshot"
# The turn page's own "no answer" line. Its list query answered moments ago, so
# the store exists; what is left is a store held by the running ingest or one
# that failed mid-read, and `esc` then `r` is the way back from both.
TURN_UNREADABLE_HINT = "could not read this turn's tool calls - esc, then r to retry"


class LogEntry(TypedDict):
    """One appended log line: what the hook writes and the TUI reads back.

    The four keys below are always written. A subagent's tool call also carries
    optional `agent_id`/`agent_type` (see `hook_main`); a main-thread call omits
    them, so an absent `agent_id` means the main agent ran the command.

    Annotation only. `parse_line` returns whatever JSON was on the line (a
    plain dict at runtime), so these keys describe the expected shape,
    not an enforced schema.
    """

    timestamp: str
    command: str
    cwd: str
    session_id: str


# ==== Danger + path patterns ====

DANGEROUS_PATTERNS = re.compile(
    r"\b("
    r"rm\b|rmdir\b|unlink\b"           # remove
    r"|mv\b|cp\b"                       # move/overwrite
    r"|dd\b|mkfs\b|shred\b"            # destructive
    r"|chmod\b|chown\b|chgrp\b"        # permissions
    r"|sudo\b|su\b|doas\b"             # privilege escalation
    r"|kill\b|killall\b|pkill\b"       # process kill
    r"|git\s+reset\b"                  # git destructive
    r"|git\s+checkout\s+--"            # git discard
    r"|git\s+clean\b|git\s+branch\s+-[dD]" # git cleanup
    r"|truncate\b"                     # write/overwrite
    r"|curl\b.*\|\s*(?:bash|sh)\b"     # pipe to shell
    r"|wget\b|curl\b.*-o\b"           # download/write
    r"|pip\s+install\b|npm\s+install\b" # package install
    r"|docker\s+rm\b|docker\s+rmi\b"   # container removal
    # systemctl: flag every subcommand except the read-only ones, so new
    # mutating verbs (poweroff, isolate, unmask, try-restart, ...) are covered
    # automatically and flags may precede the verb (`systemctl --user restart`).
    r"|systemctl\s+(?:--?[\w-]+(?:=\S*)?\s+)*"
    r"(?!(?:status|show|cat|help|list-[\w-]+|is-[\w-]+|get-default)\b)[a-z][\w-]*"
    # SysV service control, scoped to `service <name> <verb>` so that
    # `docker service ls` / `kubectl get service` stay unflagged
    r"|service\s+\S+\s+(?:start|stop|restart|reload|force-reload)\b"
    r")",
    re.IGNORECASE,
)

# A write-redirect to an absolute path (e.g. `> /etc/hosts`, `2>/var/log/x`),
# excluding targets that never touch the disk: /dev/null and the other stream
# devices (/dev/zero, /dev/std*, /dev/tty, /dev/fd/N, /proc/self/fd/N). Pure
# fd-duplication like `2>&1` has no `/` target and never matches. Kept
# separate from DANGEROUS_PATTERNS because that group's leading \b rejects a
# space-separated `> /path`.
DANGEROUS_REDIRECT = re.compile(
    r">>?\s*/(?!(?:dev/(?:null|zero|stdin|stdout|stderr|tty|fd/)|proc/self/fd/)\b)"
)

# Quoted spans, stripped before the redirect check: a redirect operator inside
# quotes is literal text to the shell, not a redirect.
QUOTED_SPAN_RE = re.compile(r"'[^']*'" + r'|"[^"]*"')

# Extract file paths from command strings
FILE_PATH_RE = re.compile(
    r"""(?:^|\s|["'])"""
    r"("
    r"""/[^\s;|&()"'>]+"""             # absolute paths
    r"""|~/[^\s;|&()"'>]+"""           # home paths
    r"""|\.\.?/[^\s;|&()"'>]+"""       # relative paths ./ ../
    r")"
)


# ==== Columns + tabs ====


@dataclass(frozen=True, eq=False)
class RenderCtx:
    """Per-render state a column cell may need.

    Bundles the session name/color maps so a column's `render` callable has one
    argument for everything table-wide. The module-level helper functions
    (`format_time`, `session_label`, `rich_color`, ...) are called directly.

    The last four fields carry the per-row agent-tree state the Agent column
    reads: `agent_map` resolves an agent_id to its label table-wide, while
    `agent_label`/`run_first`/`run_last` are set fresh for each row by
    `build_table` (via `dataclasses.replace`). `run_first`/`run_last` mark a
    row's position in a consecutive `(session_id, agent_id)` run so the tree
    gutter can draw its connectors and show the label just once.
    """

    name_map: dict[str, str] | None = None
    color_map: dict[str, str] | None = None
    agent_map: dict[str, dict] | None = None
    agent_label: str | None = None
    run_first: bool = False
    run_last: bool = False


@dataclass(frozen=True, eq=False)
class Column:
    """One table column.

    `key` is a stable digit the toggle keys / status bar use. `style`
    and `kwargs` are passed to `Table.add_column`. `render(row, ctx)` produces
    that column's cell for a single row as a `str` or `rich.text.Text`.
    Adding a column is a single entry in the `COLUMNS` list below.

    The record is shared by both tabs, so a row is whatever that tab's list holds:
    a `LogEntry` for `COLUMNS` (the feed), a `PromptTurn` for `PROMPT_COLUMNS`.
    A column only ever renders rows from its own list (see `COLUMNS_BY_TAB`), so
    each `render` may assume one of the two shapes.
    """

    key: int
    name: str
    style: str
    kwargs: dict
    render: Callable[["LogEntry | PromptTurn", RenderCtx], object]


# `Table.add_row` runs a bare `str` cell through `console.render_str`, which has
# console markup ENABLED: a path, session name or prompt containing `[bold]` has
# that span silently deleted, and one containing an unmatched `[/tmp/x]` raises
# MarkupError from inside `live.update()` - past main()'s `except
# KeyboardInterrupt`, killing the TUI. `Table.grid` rows and a `Panel`'s
# title/subtitle go through the same render_str, so the rule covers the panels
# too: every cell, metadata row and subtitle that can carry text from outside this
# program is wrapped in `Text`, which is never parsed. A bare `str` is left only
# where the value provably cannot contain `[`: `format_time`'s
# `HH:MM:SS`/`??:??:??`, digit-only counts, `Text.append`ed segments (append does
# not parse), and literals in the source - including the `[dim]`-style markup the
# panel titles and hint lines are built from on purpose.
# TestConsoleMarkupIsNeverParsed pins this per column and per panel.


def _danger_prefixed(cmd: str, body: str) -> Text:
    """A `Text` of `body`, prefixed with a bold-red `* ` when `cmd` is dangerous."""
    text = Text()
    if is_dangerous(cmd):
        text.append("* ", style="bold red")
    text.append(body)
    return text


def _render_time(entry: LogEntry, ctx: RenderCtx):
    return format_time(entry.get("timestamp", ""))


def _session_cell(sid: str, ctx: RenderCtx):
    """The Session cell for `sid`: its label, tinted with the session's `/color`.

    Factored out of `_render_session` so the Prompts tab's Session column is the
    same cell rather than a second copy of the label + tint rules.

    Always a `Text`, tint or none: the label may be a name the user typed into
    `sessions/<pid>.json`, and a bare `str` would be parsed as console markup
    (see the note under `Column`).
    """
    label = session_label(sid, ctx.name_map)
    color = rich_color(ctx.color_map.get(sid)) if ctx.color_map else None
    return Text(label, style=color or "")


def _render_session(entry: LogEntry, ctx: RenderCtx):
    return _session_cell(entry.get("session_id", ""), ctx)


def _render_directory(entry: LogEntry, ctx: RenderCtx):
    return Text(short_path(entry.get("cwd", "")))


def _render_files(entry: LogEntry, ctx: RenderCtx):
    return Text(extract_files(entry.get("command", "")))


def _render_command(entry: LogEntry, ctx: RenderCtx):
    cmd = entry.get("command", "")
    return _danger_prefixed(cmd, normalize_cmd(cmd))


def _agent_gutter(label: str | None, run_first: bool, run_last: bool) -> Text:
    """One tree-gutter cell attributing a row to a subagent run.

    A main-agent row (`label is None`) renders blank. A subagent row draws the
    connector glyph its position in the run calls for: `─` for a single-row run,
    `┌` on the run's first row, `└` on its last, `│` on the rows between. The
    label rides on the first row alone, so a run reads as one block.

    Factored out of `_render_agent` so the turn-detail page draws the same gutter
    over its action list instead of a second copy of these rules. That list is
    oldest-first where the feed is newest-first, which the glyphs do not care
    about: they bracket a run from its first displayed row to its last.
    """
    if label is None:
        return Text("")
    glyph = ("─ " if run_first and run_last else
             "┌ " if run_first else
             "└ " if run_last else "│ ")
    return Text(glyph + (label if run_first else ""), style=AGENT_STYLE)


def _render_agent(entry: LogEntry, ctx: RenderCtx):
    """The feed's agent tree-gutter cell for one row (see `_agent_gutter`), from
    the per-row run state `build_table` puts on the ctx."""
    return _agent_gutter(ctx.agent_label, ctx.run_first, ctx.run_last)


COLUMNS = [
    Column(1, "Time", "cyan",
           {"width": _env_int("CLAUDE_TRAIL_COL_TIME_WIDTH", 8), "no_wrap": True},
           _render_time),
    Column(2, "Session", "magenta",
           {"width": _env_int("CLAUDE_TRAIL_COL_SESSION_WIDTH", 13),
            "no_wrap": True, "overflow": "ellipsis"}, _render_session),
    Column(6, "Agent", AGENT_STYLE,
           {"width": _env_int("CLAUDE_TRAIL_COL_AGENT_WIDTH", 24),
            "no_wrap": True, "overflow": "ellipsis"}, _render_agent),
    Column(3, "Directory", "green", {"width": DIR_LABEL_WIDTH, "no_wrap": True},
           _render_directory),
    Column(4, "Files", "yellow",
           {"width": _env_int("CLAUDE_TRAIL_COL_FILES_WIDTH", 20),
            "no_wrap": True, "overflow": "ellipsis"}, _render_files),
    Column(5, "Command", "white",
           {"ratio": 1, "no_wrap": True, "overflow": "ellipsis"}, _render_command),
]


# ---- Prompts tab columns (rows are PromptTurn, not LogEntry) ----


def _abbrev_count(n: int) -> str:
    """A token count in at most 6 columns: `0`, `912`, `24.2k`, `183k`, `1.34m`.

    The Tokens column is fixed-width, so a turn that generated a million tokens
    must not need more room than one that generated a hundred. Precision drops as
    the magnitude grows, which is the right trade: at 183k the hundreds do not
    change a decision, at 912 they do. Six columns holds for anything up to a
    billion tokens, which is orders of magnitude past what one turn can bill.
    """
    if n < 1000:
        return str(int(n))
    if n < 100_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    if n < 10_000_000:
        return f"{n / 1_000_000:.2f}m"
    return f"{n / 1_000_000:.0f}m"


def _slash_label(text: str) -> str:
    """`claude_trail_store.slash_label(text)`, or `text` if the store is missing.

    A `/compact` turn is stored as a `<command-name>` block, so without this the
    Prompt cell would read as raw markup. The store module is imported here
    rather than at module scope to keep the feed's and the hook's startup free of
    it (importing it does NOT pull in duckdb - `connect` does that - so this
    costs a parse at worst, and by the time a turn row exists the prompts loader
    has already imported the module). An uninstallable store leaves the raw text,
    which is honest rather than blank.
    """
    try:
        from claude_trail_store import slash_label
    except Exception:
        return text
    return slash_label(text)


def _render_turn_time(turn: "PromptTurn", ctx: RenderCtx):
    return format_time(turn.ts)


def _render_turn_session(turn: "PromptTurn", ctx: RenderCtx):
    return _session_cell(turn.session_id, ctx)


def _turn_project_path(turn: "PromptTurn") -> str:
    """The path to show for a turn's session: its exact cwd, else the decoded one.

    `sessions.cwd` is the session's launch dir, copied verbatim from the
    transcript. `sessions.project` is decoded from the transcript DIR NAME, whose
    encoding spends `-` on both the separator and any `-` in a directory's own
    name, so it renders `claude-trail` as `…/claude/trail`, `personal-fin` as
    `…/personal/fin` - paths that do not exist. The lossy half cannot be
    recovered (nothing says which dashes were separators), so `cwd` wins and
    `project` stays the fallback for the sessions that have no cwd. Two kinds of
    session have none: one whose transcript has left the disk, and one whose
    transcript is still there but carries no cwd in its head (10 of the 127 here,
    all sub-300-byte stubs). `claude_trail_store.backfill_session_cwd` fills every
    other pre-column row on the next ingest.
    """
    return turn.cwd or turn.project or ""


def _render_turn_project(turn: "PromptTurn", ctx: RenderCtx):
    """The session's directory, left-truncated like the feed's Directory cell.

    A `Text`, because a project path is outside text (see the note under
    `Column`): a directory named `[wip]` would otherwise be deleted from the cell.
    """
    return Text(short_path(_turn_project_path(turn), PROJECT_LABEL_WIDTH))


def _render_turn_calls(turn: "PromptTurn", ctx: RenderCtx):
    """Tool calls in the turn's window: the total, plus how many were Bash.

    `turn.tools` is already INCLUSIVE of `turn.subcalls`, so the total is `tools`
    and the two are never added. A turn that ran nothing (a question, a `/login`,
    a resumed session) is real, so it renders a dim `-` rather than a bare `0`
    that reads like missing data.
    """
    if not turn.tools:
        return Text("-", style="dim")
    cell = Text(str(turn.tools))
    if turn.bash:
        cell.append(f" ({turn.bash} bash)", style="dim")
    return cell


def _render_turn_agents(turn: "PromptTurn", ctx: RenderCtx):
    """How many subagents ran in the turn, blank when none did (the common case,
    so a column of zeros would be noise)."""
    if not turn.agents:
        return Text("")
    return Text(str(turn.agents), style=AGENT_STYLE)


def _render_turn_tokens(turn: "PromptTurn", ctx: RenderCtx):
    """The turn's generation cost: output tokens, plus the subagent share.

    The subagent part is shown only when subagents actually generated something;
    a `+0 sub` would be noise. A turn whose reply carried no usage row honestly
    reports 0 (the store documents that case), dimmed so it does not read as a
    real number.
    """
    if not turn.output_tokens:
        return Text("0", style="dim")
    cell = Text(_abbrev_count(turn.output_tokens))
    if turn.subagent_output_tokens:
        cell.append(f" +{_abbrev_count(turn.subagent_output_tokens)} sub", style="dim")
    return cell


def _render_turn_prompt(turn: "PromptTurn", ctx: RenderCtx):
    """The turn's first line, which is what a one-row-per-prompt list can show.

    `slash_label` is applied HERE rather than in the query: it collapses a whole
    `<command-name>` block to `/compact`, which is right for a one-line cell and
    wrong for the full text a detail view shows. The store hands back the raw
    cleaned preview and each caller decides (see `slash_label`).

    The first line with content, not literally line one: the store trims the
    preview so line one should already be that line, and taking the first
    non-blank one means a stray leading newline shows the prompt instead of an
    empty cell.

    Always a `Text`: this cell is a human's own prose, the most `[`-prone value in
    the program. As a bare `str` Rich parses it as console markup, so
    `list[int]` loses `[int]` and `look in [/tmp/x] now` raises MarkupError out of
    `live.update()` (see the note under `Column`).
    """
    label = _slash_label(turn.preview or "")
    first = next((line.strip() for line in label.splitlines() if line.strip()), "")
    return Text(first) if first else Text("(empty)", style="dim italic")


PROMPT_COLUMNS = [
    Column(1, "Time", "cyan",
           {"width": _env_int("CLAUDE_TRAIL_PROMPT_COL_TIME_WIDTH", 8),
            "no_wrap": True}, _render_turn_time),
    Column(2, "Session", "magenta",
           {"width": _env_int("CLAUDE_TRAIL_PROMPT_COL_SESSION_WIDTH", 13),
            "no_wrap": True, "overflow": "ellipsis"}, _render_turn_session),
    Column(3, "Project", "green",
           {"width": PROJECT_LABEL_WIDTH, "no_wrap": True}, _render_turn_project),
    # 14, not 12: the widest real cell is `185 (82 bash)` (measured across the
    # newest 200 turns of a 1 GiB store), which 12 truncated to `185 (82 bas…`.
    Column(4, "Calls", "yellow",
           {"width": _env_int("CLAUDE_TRAIL_PROMPT_COL_CALLS_WIDTH", 14),
            "no_wrap": True, "overflow": "ellipsis"}, _render_turn_calls),
    Column(5, "Agents", AGENT_STYLE,
           {"width": _env_int("CLAUDE_TRAIL_PROMPT_COL_AGENTS_WIDTH", 6),
            "no_wrap": True}, _render_turn_agents),
    Column(6, "Tokens", "white",
           {"width": _env_int("CLAUDE_TRAIL_PROMPT_COL_TOKENS_WIDTH", 16),
            "no_wrap": True, "overflow": "ellipsis"}, _render_turn_tokens),
    Column(7, "Prompt", "white",
           {"ratio": 1, "no_wrap": True, "overflow": "ellipsis"}, _render_turn_prompt),
]

# The two top-level views. `AppState.tab` holds one and `Tab` cycles them; the
# feed stays first so the default view is unchanged.
TABS = ("commands", "prompts")

# Which columns each tab shows. One registry keyed by tab is the single source of
# truth for the digit toggles, each tab's default visibility dict, and the status
# bar's `cols:` segment. Each tab's keys are numbered from 1 independently, so
# `1`-`6` toggle the feed's columns and `1`-`7` the prompts list's.
COLUMNS_BY_TAB: dict[str, list[Column]] = {"commands": COLUMNS, "prompts": PROMPT_COLUMNS}


def _default_cols(tab: str) -> dict[int, bool]:
    """Every column registered for `tab`, all visible (a tab's initial state)."""
    return {c.key: True for c in COLUMNS_BY_TAB.get(tab, ())}


# ==== Session /color parsing + aliases ====

# Matches the `/color <value>` slash command as it appears in transcript
# system/local_command events: a single line containing
#   <command-name>/color</command-name> ... <command-args>VALUE</command-args>
COLOR_CMD_RE = re.compile(
    r"<command-name>/color</command-name>.*?<command-args>([^<]+)</command-args>",
    re.DOTALL,
)

# Claude Code's /color accepts plain names like "orange"/"pink"/"gray" that
# Rich's color parser rejects (it expects X11-style suffixes such as "orange1").
# Translate so the cell tint matches what the user sees in Claude's session tag.
CLAUDE_COLOR_ALIASES = {
    "orange": "orange1",
    "pink": "pink1",
    "gray": "grey50",
    "grey": "grey50",
}


def rich_color(claude_color: str | None) -> str | None:
    """Map a /color value to a name Rich understands, or return it unchanged."""
    if not claude_color:
        return None
    return CLAUDE_COLOR_ALIASES.get(claude_color, claude_color)


# ==== Danger detection + path extraction ====


def is_dangerous(cmd: str) -> bool:
    if DANGEROUS_PATTERNS.search(cmd):
        return True
    # Strip quoted spans first so prose like `git commit -m "logs > /var/log"`
    # is not mistaken for a redirect; an unquoted `> /path` still matches.
    return bool(DANGEROUS_REDIRECT.search(QUOTED_SPAN_RE.sub(" ", cmd)))


def find_paths(cmd: str) -> list[str]:
    """File paths referenced in `cmd`, in first-seen order, de-duplicated."""
    return list(dict.fromkeys(FILE_PATH_RE.findall(cmd)))


def extract_files(cmd: str) -> str:
    """Basenames of the paths in `cmd`, comma-joined (max 3, then `+N` overflow)."""
    unique = find_paths(cmd)
    if not unique:
        return ""
    short = [os.path.basename(p.rstrip("/")) or p for p in unique[:3]]
    result = ", ".join(short)
    if len(unique) > 3:
        result += f" +{len(unique) - 3}"
    return result


def short_path(cwd: str, width: int = DIR_LABEL_WIDTH) -> str:
    """The tail of `cwd`, so the distinctive end of the path stays visible.

    Returns the whole path when it fits `width`; otherwise as many trailing
    components as fit after a leading `…`, e.g.
    `/home/naka/Projects/personal/claude-trail` -> `…/claude-trail`. A single
    trailing component too long on its own is left-truncated so its end (the
    part that tells dirs apart) survives: `…ng-directory-name`."""
    path = (cwd or "").rstrip("/")
    if not path:
        return cwd or ""
    if len(path) <= width:
        return path
    parts = [p for p in path.split("/") if p]
    tail = parts[-1] if parts else path
    if len(tail) + 2 > width:  # 2 = leading `…/`; the component alone overflows
        return "…" + tail[-(width - 1):]
    for part in reversed(parts[:-1]):  # grow leftward while `…/<tail>` still fits
        candidate = part + "/" + tail
        if len(candidate) + 2 > width:
            break
        tail = candidate
    return "…/" + tail


# ==== Session names ====

_session_name_cache: dict[str, str] = {}
_session_name_cache_ts: float = 0.0


def load_session_names() -> dict[str, str]:
    """Map session_id -> name from ~/.claude/sessions/*.json.

    Claude Code writes one JSON file per running session there (named after pid),
    each carrying a `sessionId` and an optional `name`. The file is removed when
    the session exits, so we accumulate known names across calls: once a name has
    been observed it stays available even after the session is gone. Cached for
    SESSION_NAME_CACHE_TTL seconds to avoid hitting the filesystem on every render.
    """
    global _session_name_cache_ts
    now = time.monotonic()
    if now - _session_name_cache_ts < SESSION_NAME_CACHE_TTL:
        return _session_name_cache
    try:
        files = list(SESSIONS_DIR.iterdir())
    except (OSError, FileNotFoundError):
        files = []
    for entry in files:
        if entry.suffix != ".json":
            continue
        try:
            with open(entry, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        name = data.get("name")
        if sid and name:
            _session_name_cache[sid] = name
    _session_name_cache_ts = now
    return _session_name_cache


def session_label(sid: str, name_map: dict[str, str] | None = None) -> str:
    """Session name if known, else first 8 chars of its ID."""
    if name_map:
        name = name_map.get(sid)
        if name:
            return name
    return sid[:8] if sid else "--------"


# ==== Session colors ====

_session_color_cache: dict[str, str] = {}
_session_color_cache_ts: float = 0.0
_transcript_path_cache: dict[str, Path] = {}
_transcript_positions: dict[str, int] = {}


def _find_transcript(sid: str) -> Path | None:
    """Locate `~/.claude/projects/*/<sid>.jsonl`. Caches resolved paths."""
    cached = _transcript_path_cache.get(sid)
    if cached is not None and cached.exists():
        return cached
    try:
        for project in PROJECTS_DIR.iterdir():
            if not project.is_dir():
                continue
            candidate = project / f"{sid}.jsonl"
            if candidate.exists():
                _transcript_path_cache[sid] = candidate
                return candidate
    except (OSError, FileNotFoundError):
        pass
    return None


def _scan_chunk_for_color(chunk: str) -> str | None:
    """Scan a transcript chunk for /color system events; return the last value found."""
    color: str | None = None
    for line in chunk.splitlines():
        if "/color" not in line:
            continue
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") != "system" or evt.get("subtype") != "local_command":
            continue
        match = COLOR_CMD_RE.search(evt.get("content", ""))
        if match:
            color = match.group(1).strip()
    return color


def load_session_colors(session_ids) -> dict[str, str]:
    """Map session_id -> the color the user picked with Claude Code's `/color`
    slash command, parsed from each session's transcript under
    ~/.claude/projects/. Transcripts are tailed incrementally (we remember each
    file's last-scanned byte offset) so the cost stays cheap as transcripts grow.
    Refreshed at most every SESSION_COLOR_CACHE_TTL seconds; the cache
    accumulates so a color stays known after the session ends.
    """
    global _session_color_cache_ts
    now = time.monotonic()
    if now - _session_color_cache_ts < SESSION_COLOR_CACHE_TTL:
        return _session_color_cache
    for sid in session_ids:
        if not sid:
            continue
        path = _find_transcript(sid)
        if path is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        last_pos = _transcript_positions.get(sid, 0)
        if size < last_pos:
            # file got truncated/rewritten, re-scan from the start
            last_pos = 0
        if size == last_pos:
            continue
        try:
            with open(path, "rb") as f:
                f.seek(last_pos)
                chunk = f.read(size - last_pos)
        except OSError:
            continue
        _transcript_positions[sid] = size
        color = _scan_chunk_for_color(chunk.decode("utf-8", errors="replace"))
        if color:
            _session_color_cache[sid] = color
    _session_color_cache_ts = now
    return _session_color_cache


# ==== Agent labels + session model ====

# A subagent's meta/transcript live in a per-session sibling of the transcript
# file: <proj>/<sid>/subagents/agent-<id>.jsonl (+ .meta.json). Workflow agents
# nest one level deeper under subagents/workflows/wf_*/. The `subagents/**/`
# glob (pathlib `**` matches zero or more dirs) covers both.
_agent_label_cache: dict[str, dict] = {}
_agent_label_cache_ts: float = 0.0
_session_model_cache: dict[str, tuple] = {}  # sid -> (monotonic_ts, SessionModel)

# Terminal task-notification tags in a parent transcript. For a Task/Agent
# subagent the <task-id> equals its agent_id (empirically confirmed), so this
# doubles as agent_id -> completed/failed/killed. Workflow/background
# notifications carry unrelated task-ids and simply never match an agent.
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_TASK_STATUS_RE = re.compile(r"<status>([a-z]+)</status>")


@dataclass(frozen=True)
class SubagentInfo:
    """One subagent under a session: its identity, derived run status, and how
    many tool calls its transcript holds."""

    agent_id: str
    agent_type: str
    description: str
    status: str  # "running" | "done" | "stopped"
    command_count: int
    command_count_capped: bool = False
    spawn_ts: float = 0.0  # meta-file mtime = spawn time; orders the modal list


@dataclass(frozen=True)
class SessionModel:
    """Everything the session-detail modal shows for one session.

    `live` is True when a `sessions/<pid>.json` still exists for the session
    (so `status`/`version`/`kind`/`started_at` come from it); for an ended
    session those are blank/0 and `cwd`/`name` fall back to the transcript and
    the accumulated name cache. `started_at` is epoch ms (0 if unknown).
    """

    session_id: str
    name: str
    status: str
    live: bool
    cwd: str
    version: str
    kind: str
    started_at: int
    transcript_path: str
    subagents: tuple = ()


def _session_subdir(sid: str) -> Path | None:
    """The `<proj>/<sid>/` dir holding `subagents/` for a session, or None."""
    transcript = _find_transcript(sid)
    if transcript is None:
        return None
    subdir = transcript.parent / sid
    return subdir if subdir.is_dir() else None


def load_agent_labels(session_ids) -> dict[str, dict]:
    """Map agent_id -> {"type", "description"} from each session's
    `subagents/**/agent-*.meta.json`.

    Accumulates across calls like `load_session_names` (a label stays resolvable
    after its session ends) and refreshes at most every AGENT_LABEL_CACHE_TTL
    seconds. Any unreadable/malformed meta file is skipped, never raised.
    """
    global _agent_label_cache_ts
    now = time.monotonic()
    if now - _agent_label_cache_ts < AGENT_LABEL_CACHE_TTL:
        return _agent_label_cache
    for sid in session_ids:
        if not sid:
            continue
        subdir = _session_subdir(sid)
        if subdir is None:
            continue
        for meta in subdir.glob("subagents/**/agent-*.meta.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            agent_id = meta.name[len("agent-"):-len(".meta.json")]
            _agent_label_cache[agent_id] = {
                "type": data.get("agentType", ""),
                "description": data.get("description", ""),
            }
    _agent_label_cache_ts = now
    return _agent_label_cache


def entry_agent_id(entry: dict) -> str | None:
    """The subagent id that ran this command, or None for a main-agent command."""
    return entry.get("agent_id") or None


def short_agent_id(agent_id: str) -> str:
    """An agent's distinct short identifier: the first AGENT_ID_LABEL_LEN chars
    of its id. Shown in the feed and session modal so two subagents that share a
    description (e.g. the same subagent type spawned twice) still read apart."""
    return agent_id[:AGENT_ID_LABEL_LEN]


def agent_id_label(agent_id: str | None, agent_map: dict[str, dict]) -> str | None:
    """Label for one agent id, or None when it is the main agent (a falsy id).

    Formatted `"<short-id> <name>"`, id first so it survives the Agent column's
    ellipsis: two subagents sharing a description are still distinguishable by
    their ids. `name` is the description, falling back to the agent type, and is
    dropped when neither is known (leaving the short id alone). The full
    description/type live on the session-detail modal (`s`).

    Split out of `agent_label` because the turn-detail page already HAS the id
    (the store's `agent_id` column) rather than a log entry to read it out of."""
    if not agent_id:
        return None
    info = agent_map.get(agent_id) or {}
    name = info.get("description") or info.get("type") or ""
    short = short_agent_id(agent_id)
    return f"{short} {name}" if name else short


def agent_label(entry: dict, agent_map: dict[str, dict]) -> str | None:
    """Feed label for the agent that ran this command, or None for the main
    agent (see `agent_id_label`)."""
    return agent_id_label(entry_agent_id(entry), agent_map)


def _find_session_pid_info(sid: str) -> dict | None:
    """The live `sessions/<pid>.json` dict whose sessionId == sid, or None."""
    try:
        files = list(SESSIONS_DIR.iterdir())
    except (OSError, FileNotFoundError):
        return None
    for entry in files:
        if entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") == sid:
            return data
    return None


def _first_transcript_cwd(path: Path) -> str:
    """The first `cwd` recorded in a transcript (ended-session folder fallback)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(evt, dict) and evt.get("cwd"):
                    return evt["cwd"]
    except OSError:
        pass
    return ""


def _scan_terminal_statuses(transcript_text: str) -> dict[str, str]:
    """Map task-id -> terminal status ("completed"/"failed"/"killed") from the
    `<task-notification>` events in a parent transcript.

    Each notification is one JSONL line (its inner newlines are backslash-escaped
    inside the JSON string), so pairing the task-id and status per physical line
    can never mis-associate them across notifications.
    """
    result: dict[str, str] = {}
    for line in transcript_text.splitlines():
        if "task-notification" not in line:
            continue
        tid = _TASK_ID_RE.search(line)
        st = _TASK_STATUS_RE.search(line)
        if tid and st:
            result[tid.group(1)] = st.group(1)
    return result


def _count_tool_uses(path: Path) -> tuple[int, bool]:
    """(#`tool_use` blocks, capped?) in an agent transcript, reading at most
    AGENT_TOOLUSE_BYTE_CAP bytes so a huge transcript can't stall the modal."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            data = f.read(AGENT_TOOLUSE_BYTE_CAP)
    except OSError:
        return 0, False
    return data.count(b'"type":"tool_use"'), size > AGENT_TOOLUSE_BYTE_CAP


def _load_subagents(sid: str, transcript: Path | None, live: bool) -> tuple:
    """Build the `SubagentInfo` tuple for a session, in execution (spawn) order.

    Each `agent-<id>.meta.json` is written once when its subagent spawns, so the
    file's mtime is the spawn time; the list is sorted by `(spawn_ts, agent_id)`
    (id is a stable tiebreak when two metas share an mtime)."""
    subdir = _session_subdir(sid)
    if subdir is None:
        return ()
    terminal: dict[str, str] = {}
    if transcript is not None:
        try:
            terminal = _scan_terminal_statuses(
                transcript.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            terminal = {}
    infos = []
    for meta_path in subdir.glob("subagents/**/agent-*.meta.json"):
        agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        st = terminal.get(agent_id)
        if st == "completed":
            status = "done"
        elif st in ("failed", "killed"):
            status = "stopped"  # spawned, ended without completing
        elif live:
            status = "running"
        else:
            status = "stopped"
        count, capped = _count_tool_uses(meta_path.parent / f"agent-{agent_id}.jsonl")
        try:
            spawn_ts = meta_path.stat().st_mtime
        except OSError:
            spawn_ts = 0.0
        infos.append(SubagentInfo(
            agent_id=agent_id,
            agent_type=data.get("agentType", ""),
            description=data.get("description", ""),
            status=status,
            command_count=count,
            command_count_capped=capped,
            spawn_ts=spawn_ts,
        ))
    infos.sort(key=lambda s: (s.spawn_ts, s.agent_id))
    return tuple(infos)


def _build_session_model(sid: str) -> SessionModel:
    pid_info = _find_session_pid_info(sid)
    live = pid_info is not None
    transcript = _find_transcript(sid)
    transcript_path = str(transcript) if transcript else ""

    if live:
        name = pid_info.get("name") or session_label(sid)
        status = pid_info.get("status", "")
        cwd = pid_info.get("cwd", "")
        version = pid_info.get("version", "")
        kind = pid_info.get("kind", "")
        started_raw = pid_info.get("startedAt") or 0
    else:
        name = load_session_names().get(sid) or session_label(sid)
        status = ""
        cwd = _first_transcript_cwd(transcript) if transcript else ""
        version = ""
        kind = ""
        started_raw = 0

    return SessionModel(
        session_id=sid,
        name=name,
        status=status,
        live=live,
        cwd=cwd,
        version=version,
        kind=kind,
        started_at=int(started_raw) if isinstance(started_raw, (int, float)) else 0,
        transcript_path=transcript_path,
        subagents=_load_subagents(sid, transcript, live),
    )


def load_session_model(sid: str) -> SessionModel:
    """A `SessionModel` for `sid`, cached per-sid for SESSION_MODEL_CACHE_TTL
    seconds so the modal can be rebuilt every frame while still reflecting live
    running->done transitions. Resilient: unreadable files are skipped."""
    now = time.monotonic()
    cached = _session_model_cache.get(sid)
    if cached is not None and now - cached[0] < SESSION_MODEL_CACHE_TTL:
        return cached[1]
    model = _build_session_model(sid)
    _session_model_cache[sid] = (now, model)
    return model


# ==== Search (per-session recursive grep) ====

# One search hit: (absolute path, 1-based line number, matched line text).
Match = tuple[str, int, str]


def _other_root(root: str) -> str:
    """The other of the two search roots ("transcript" <-> "cwd")."""
    return "cwd" if root == "transcript" else "transcript"


def search_roots(
    sid: str, selected_entry: dict | None, model: "SessionModel | None" = None
) -> dict[str, list[Path]]:
    """Resolve the two search roots for a session.

    "transcript": the session transcript `<sid>.jsonl` plus its sibling `<sid>/`
    dir (which holds `subagents/**` and `subagents/workflows/**`), whichever
    exist. "cwd": the session's launch dir (pid.json cwd via a live `model`) or,
    for an ended session, the selected row's `cwd`. A path that does not exist is
    skipped, and a root with no resolvable path is omitted entirely so the panel
    can disable it. All returned paths are absolute (transcript paths come from
    `_find_transcript`, which resolves under `PROJECTS_DIR`).
    """
    roots: dict[str, list[Path]] = {}

    transcript = _find_transcript(sid) if sid else None
    transcript_paths: list[Path] = []
    if transcript is not None and transcript.exists():
        transcript_paths.append(transcript)
        subdir = transcript.parent / sid
        if subdir.is_dir():
            transcript_paths.append(subdir)
    if transcript_paths:
        roots["transcript"] = transcript_paths

    cwd = ""
    if model is not None and model.live and model.cwd:
        cwd = model.cwd
    elif selected_entry and selected_entry.get("cwd"):
        cwd = selected_entry.get("cwd", "")
    elif model is not None and model.cwd:
        cwd = model.cwd
    if cwd:
        cwd_path = Path(cwd)
        if cwd_path.exists():
            roots["cwd"] = [cwd_path]

    return roots


def _parse_grep_line(line: str) -> Match | None:
    """Parse one `path:line:text` result line into a Match, or None if malformed."""
    parts = line.split(":", 2)
    if len(parts) < 3:
        return None
    path, lineno, text = parts
    try:
        return (path, int(lineno), text)
    except ValueError:
        return None


def run_search(
    query: str,
    paths,
    *,
    ignore_case: bool | None = None,
    limit: int = SEARCH_RESULT_LIMIT,
    timeout: float = SEARCH_TIMEOUT,
) -> tuple[list[Match], bool, str | None]:
    """Recursively grep `paths` for `query`, returning `(matches, capped, error)`.

    Uses `rg` when available, else `grep -rInE` (`-I` skip binary, `-n` line
    numbers). Smart-case by default (rg `-S`; grep has no smart-case so it stays
    case-sensitive); an explicit `ignore_case` overrides. The query is passed as
    an argv element (`shell=False`), so there is no shell-injection surface.

    `matches` is truncated to `limit` (with `capped=True` when there were more).
    rg/grep exit 1 (no matches) is an empty result, not an error; a bad regex or
    a timeout returns a short `error` string. Never raises.
    """
    query = query or ""
    path_args = [str(p) for p in paths if str(p)]
    if not query.strip() or not path_args:
        return [], False, None

    if shutil.which("rg"):
        # -H forces the filename prefix. rg (like grep) omits it when given a
        # single explicit file, which is the common transcript-root case (one
        # `<sid>.jsonl` with no `subagents/` dir); without -H the output is
        # `line:text` and _parse_grep_line drops or mis-parses every hit.
        argv = ["rg", "-H", "-n", "--no-heading", "--color=never"]
        if ignore_case is True:
            argv.append("-i")
        elif ignore_case is False:
            argv.append("-s")  # force case-sensitive
        else:
            argv.append("-S")  # smart-case
        argv += ["-e", query, "--", *path_args]
    else:
        # -H guards non-GNU greps that omit the filename for a single file.
        argv = ["grep", "-rHInE"]
        if ignore_case:
            argv.append("-i")
        argv += ["-e", query, "--", *path_args]

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], False, f"timed out after {timeout:g}s"
    except OSError as exc:
        return [], False, str(exc)

    # rg/grep: exit 0 = matches, 1 = no matches, >=2 = a real error (bad regex).
    if proc.returncode >= 2:
        stderr_lines = (proc.stderr or "").strip().splitlines()
        return [], False, stderr_lines[-1] if stderr_lines else "search failed"
    if proc.returncode == 1:
        return [], False, None

    matches: list[Match] = []
    capped = False
    for line in proc.stdout.splitlines():
        parsed = _parse_grep_line(line)
        if parsed is None:
            continue
        if len(matches) >= limit:
            capped = True
            break
        matches.append(parsed)
    return matches, capped, None


# ==== Format / parse helpers ====


def _platform_opener() -> str:
    """Return the platform-default file launcher (`open` on macOS, `xdg-open` elsewhere)."""
    if sys.platform == "darwin":
        return "open"
    return "xdg-open"


def normalize_cmd(cmd: str) -> str:
    """Collapse whitespace in command string."""
    return " ".join(cmd.split())


def parse_line(line: str) -> LogEntry | None:
    # Annotation only: at runtime this returns whatever JSON the line held
    # (a plain dict), so tests asserting plain-dict equality still pass.
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


# A UTC offset written with HOURS ONLY (`...37.084-03`), which is how DuckDB
# renders a TIMESTAMPTZ and therefore how every store timestamp reaches this
# module. `datetime.fromisoformat` only learned to read that shape in 3.11, and
# this package's floor is 3.10, where it raises instead: every Time cell (feed,
# prompts list, turn page) would read `??:??:??` and the turn page's duration
# chip would vanish. The `HH:MM` in front of the offset is required by the
# pattern so a bare date (`2026-07-29`) is never mistaken for one.
_SHORT_TZ_OFFSET_RE = re.compile(r"(\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)([+-]\d{2})$")


def parse_ts(ts: str) -> datetime:
    """`datetime.fromisoformat(ts)` with an hours-only UTC offset widened first.

    The ONE place this module parses a timestamp, so the 3.10 gap above is closed
    once for the feed's own `+HH:MM` hook lines and for the store's
    `ts::VARCHAR` strings alike. `Z` and a full `+HH:MM` are passed through
    untouched (3.11+ reads both; 3.10 reads the second). Raises whatever
    `fromisoformat` raises - every caller already treats an unparseable
    timestamp as "no time".
    """
    return datetime.fromisoformat(_SHORT_TZ_OFFSET_RE.sub(r"\1\2:00", ts))


def format_time(ts: str) -> str:
    try:
        dt = parse_ts(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # render in the user's local timezone
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "??:??:??"


# ==== Rendering: table ====


def get_display_entries(
    entries: list[LogEntry], max_rows: int, offset: int = 0
) -> list[LogEntry]:
    """Return up to max_rows entries in newest-first order, skipping the first `offset`."""
    if not entries:
        return []
    reversed_view = list(reversed(entries))
    return reversed_view[offset : offset + max_rows]


def build_table(
    entries: list[LogEntry],
    max_rows: int,
    visible_cols: dict[int, bool],
    cursor: int,
    offset: int = 0,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    agent_map: dict[str, dict] | None = None,
) -> Table:
    """Render up to `max_rows` entries as a Rich table, newest first.

    Cursor/offset contract: rows are shown newest-first (index 0 = newest).
    `offset` skips the N newest rows (scrolling down through history), and the
    cursor is highlighted at `highlight_idx = cursor - offset` within the
    displayed slice; when the cursor is scrolled out of view that index falls
    outside the slice and no row is highlighted.

    The Agent column reads a per-row `RenderCtx` (built with `replace`): runs of
    adjacent rows sharing `(session_id, agent_id)` are detected in the displayed
    (newest-first) order so the tree gutter connects them and labels each run
    once. Runs never span sessions or agents; a `None` agent_id is the main
    agent and renders a blank gutter.
    """
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_edge=False,
        pad_edge=False,
        row_styles=["", "dim"],
    )

    base_ctx = RenderCtx(name_map=name_map, color_map=color_map, agent_map=agent_map)
    active = [col for col in COLUMNS if visible_cols.get(col.key)]
    for col in active:
        table.add_column(col.name, style=col.style, **col.kwargs)

    display = get_display_entries(entries, max_rows, offset)
    highlight_idx = cursor - offset

    keys = [(e.get("session_id", ""), entry_agent_id(e)) for e in display]
    labels = [agent_label(e, base_ctx.agent_map or {}) for e in display]
    for idx, entry in enumerate(display):
        row_style = "on grey23" if idx == highlight_idx else None
        run_first = idx == 0 or keys[idx] != keys[idx - 1]
        run_last = idx == len(display) - 1 or keys[idx] != keys[idx + 1]
        row_ctx = replace(base_ctx, agent_label=labels[idx],
                          run_first=run_first, run_last=run_last)
        cells = [col.render(entry, row_ctx) for col in active]
        table.add_row(*cells, style=row_style)

    return table


# ==== Side-effecting actions ====


def filter_session_log(session_id: str) -> str | None:
    """Filter the command log down to one session, write it pretty-printed to a
    temp `.json` file, and return that path - or None when there is nothing to
    show: no session id, no log file, or no line in the log for that session.

    That last case is reachable from the Prompts tab, where a row's session may
    never have run a Bash command (the store reads transcripts, the log is
    Bash-only), so nothing is written and `o` stays inert rather than opening an
    empty buffer in the user's editor.

    The output is only ever opened in an editor for a human, so each entry is
    re-emitted indented (blocks joined by a blank line, like
    `format_session_transcript`) instead of as raw single-line JSONL."""
    if not session_id or not LOG_PATH.exists():
        return None
    safe_id = re.sub(r"[^a-zA-Z0-9-]", "", session_id[:8]) or "unknown"
    out_path = os.path.join(tempfile.gettempdir(), f"claude-trail-session-{safe_id}.json")
    blocks: list[str] = []
    with open(LOG_PATH, "r", encoding="utf-8") as f_in:
        for line in f_in:
            entry = parse_line(line.strip())
            if entry and entry.get("session_id", "") == session_id:
                blocks.append(json.dumps(entry, indent=2, ensure_ascii=False))
    if not blocks:
        return None
    with open(out_path, "w", encoding="utf-8") as f_out:
        f_out.write("\n\n".join(blocks))
    return out_path


def format_session_transcript(session_id: str) -> str | None:
    """Pretty-print a session's transcript to a temp `.json` file and return that
    path (or None if the transcript can't be resolved).

    Reads `~/.claude/projects/*/<session_id>.jsonl` (via `_find_transcript`) line
    by line and re-emits each JSON object indented; a line that isn't valid JSON
    is passed through unchanged. Blocks are joined by a blank line rather than
    assembled into one array (readable for large transcripts, which can run to
    many MB), so the output is `.json`, not JSONL.
    """
    transcript = _find_transcript(session_id)
    if transcript is None:
        return None
    safe_id = re.sub(r"[^a-zA-Z0-9-]", "", session_id[:8]) or "unknown"
    out_path = os.path.join(tempfile.gettempdir(), f"claude-trail-transcript-{safe_id}.json")
    blocks: list[str] = []
    with open(transcript, "r", encoding="utf-8") as f_in:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                blocks.append(line)  # keep malformed lines verbatim
                continue
            blocks.append(json.dumps(obj, indent=2, ensure_ascii=False))
    with open(out_path, "w", encoding="utf-8") as f_out:
        f_out.write("\n\n".join(blocks))
    return out_path


def open_file_folder(entry: LogEntry) -> None:
    """Open the folder containing files referenced in the command."""
    cmd = entry.get("command", "")
    cwd = entry.get("cwd", "")
    paths = find_paths(cmd)

    target = None
    for p in paths:
        expanded = os.path.expanduser(p)
        if not os.path.isabs(expanded) and cwd:
            expanded = os.path.join(cwd, expanded)
        parent = os.path.dirname(expanded) if not os.path.isdir(expanded) else expanded
        if os.path.isdir(parent):
            target = parent
            break

    if not target and cwd and os.path.isdir(cwd):
        target = cwd

    if target:
        subprocess.Popen(
            [_platform_opener(), target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def open_containing_folder(path: str) -> str:
    """Open the folder containing `path` in the platform file manager and return
    the folder that was opened (for the search panel's confirmation flash).

    Reuses `open_file_folder`'s detached `Popen(_platform_opener(), ...)` pattern.
    `path` is absolute (search roots are absolute), so `dirname` gives its folder.
    """
    folder = os.path.dirname(path) or path
    try:
        subprocess.Popen(
            [_platform_opener(), folder],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
    return folder


# ==== Rendering: panels ====


def _panel_title(extra: str = "") -> str:
    """Title bar markup: ` claude-trail vX.Y.Z [extra] `."""
    parts = ["[bold cyan] claude-trail [/bold cyan]"]
    if __version__:
        parts.append(f"[dim]v{__version__}[/dim]")
    if extra:
        parts.append(f"[dim] {extra}[/dim]")
    return "".join(parts) + " "


def _format_command_tokens(tokens: dict) -> Text:
    """The Tokens cell for the command-detail modal: the generation cost the
    audit store attributes to this command (that request's output tokens, split
    evenly when the request emitted several parallel tool calls), plus the model.
    The `_tokens_unavailable` sentinel renders as a dim hint instead of a number.
    """
    if "unavailable" in tokens:
        return Text(tokens["unavailable"], style="dim italic")
    share = int(round(tokens.get("share", 0)))
    calls = tokens.get("calls_in_request", 1) or 1
    model = tokens.get("model") or ""
    text = Text()
    text.append(f"{share:,}", style="bold")
    text.append(" output", style="dim")
    if calls > 1:
        text.append(f"  ·  1/{calls} of a {calls}-call request", style="dim")
    if model:
        text.append(f"  ·  {model}", style="dim")
    return text


def _text_lines(text: str | None) -> list[str]:
    """A prompt body as display lines ([] for None or empty).

    One definition, so the command modal (which carries its prompt as a dict) and
    the turn page (which carries a plain string) window identical line lists."""
    return (text or "").splitlines()


def _prompt_lines(prompt: dict | None) -> list[str]:
    """The prompt's text as display lines ([] when there is nothing to show,
    including the `_tokens_unavailable` sentinel, which is a hint, not a
    prompt)."""
    if not prompt or "unavailable" in prompt:
        return []
    return _text_lines(prompt.get("text"))


def _prompt_box(body, padding: tuple[int, int] = (1, 2)) -> Panel:
    """The bordered `prompt` box shared by the command modal's preview, the
    turn page's preview and the full-prompt view, so the three quote a prompt
    the same way. `padding` is the one thing that varies: the turn page packs
    a table under its box and cannot spend two rows on blank padding."""
    return Panel(
        body,
        title="[dim]prompt[/dim]",
        title_align="left",
        border_style="grey50",
        box=box.ROUNDED,
        padding=padding,
    )


def _preview_body(lines: list[str], limit: int, can_open: bool = True,
                  no_wrap: bool = False):
    """The `prompt` box's body: the first `limit` lines plus a dim footer
    counting the ones left out, so a clipped prompt says it is clipped.

    The footer names `↵` only when there is a full text to open (`can_open`):
    on a page whose Enter would close rather than drill in, advertising it would
    be a lie.

    `no_wrap` ellipsizes each quoted line instead of wrapping it, which makes the
    box exactly one row per line at any width. The turn page needs that: it packs
    a windowed table underneath and sizes the window from `_prompt_box_rows`, so
    a prompt whose opening line is 300 characters must not quietly claim three
    rows and crop the list's last row and the key hints. The command modal has no
    list under its box and keeps wrapping (the full text is one `↵` away in both).
    """
    shown, hidden = lines[:limit], max(0, len(lines) - limit)
    if not shown:
        text = Text("(empty)", style="dim italic")
    elif no_wrap:
        text = Text("\n".join(shown), no_wrap=True, overflow="ellipsis")
    else:
        text = Text("\n".join(shown))
    if not hidden:
        return text
    plural = "" if hidden == 1 else "s"
    more = f"+{hidden} more line{plural}"
    if can_open:
        more += "  ·  ↵ : full prompt"
    return Group(text, Text(""), Text.from_markup(f"[dim italic]{more}[/dim italic]"))


def _build_prompt_preview(prompt: dict) -> Panel:
    """The command modal's `prompt` panel: the opening PROMPT_PREVIEW_LINES lines
    of the turn that led to this command, with a dim footer counting the hidden
    lines. The `_tokens_unavailable` sentinel renders as its dim hint instead,
    so a lagging store explains itself rather than showing an empty box."""
    if "unavailable" in prompt:
        body = Text(prompt["unavailable"], style="dim italic")
    else:
        body = _preview_body(_prompt_lines(prompt), PROMPT_PREVIEW_LINES)
    return _prompt_box(body)


def build_detail_panel(
    entry: LogEntry,
    term_height: int,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    agent_map: dict[str, dict] | None = None,
    tokens: dict | None = None,
    prompt: dict | None = None,
) -> Panel:
    """Full-screen view of a single command: metadata header plus the complete,
    untruncated command text (newlines preserved).

    `tokens` (loaded on Enter via the audit store) adds a Tokens row with the
    command's generation cost, or a dim "pending ingest" / "no audit store"
    hint (the `_tokens_unavailable` sentinel) when the store has no matching
    request yet; the row is absent only when `tokens` is None.

    `prompt` (loaded in the same store open) adds a `prompt` panel under the
    command showing the first PROMPT_PREVIEW_LINES lines of the turn that led to
    it; Enter then opens the whole thing (`build_prompt_panel`)."""
    cmd = entry.get("command", "")
    ctx = RenderCtx(name_map=name_map, color_map=color_map)

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right", no_wrap=True)
    meta.add_column(ratio=1)
    meta.add_row("Time", format_time(entry.get("timestamp", "")))
    meta.add_row("Session", _render_session(entry, ctx))
    # Agent row only for subagent rows; agent_label is None for the main agent.
    label = agent_label(entry, agent_map or {})
    if label is not None:
        meta.add_row("Agent", Text(label, style=AGENT_STYLE))
    # Text, not str: a cwd or a basename can carry `[` (see the note under
    # `Column`), and `Table.grid` parses a bare str exactly like `Table` does.
    meta.add_row("Directory", Text(entry.get("cwd", "") or "(none)"))
    files = extract_files(cmd)
    if files:
        meta.add_row("Files", Text(files))
    if tokens:
        meta.add_row("Tokens", _format_command_tokens(tokens))

    # Reuse the Command column's danger marker, but keep the raw (un-normalized)
    # command so newlines are preserved in this full-screen view.
    cmd_text = _danger_prefixed(cmd, cmd or "(empty)")

    cmd_panel = Panel(
        cmd_text,
        title="[dim]command[/dim]",
        title_align="left",
        border_style="grey50",
        box=box.ROUNDED,
        padding=(1, 2),
    )

    parts = [meta, Text(""), cmd_panel]
    if prompt:
        parts += [Text(""), _build_prompt_preview(prompt)]
    # Enter drills into the full prompt when there is one, so it only doubles as
    # "back" when there is nothing to open.
    keys = ("esc / q : back    ↵ : full prompt" if _prompt_lines(prompt)
            else "esc / q / ↵ : back")
    parts += [Text(""), Text.from_markup(f"[dim italic]{keys}[/dim italic]")]
    body = Group(*parts)

    return Panel(
        body,
        title=_panel_title("· command"),
        subtitle=Text(str(LOG_PATH), style="dim"),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


def _prompt_capacity(term_height: int) -> int:
    """Prompt lines that fit in the full-prompt view once its chrome is
    accounted for (mirrors `_session_capacity`)."""
    return max(term_height - PROMPT_CHROME_ROWS, MIN_VISIBLE_ROWS)


def _clamp_prompt_scroll(scroll: int, line_count: int, term_height: int) -> int:
    """Clamp a full-prompt scroll offset to `[0, line_count - capacity]`.

    Shared by `build_prompt_panel` (display safety) and `render_panel`, which
    writes the clamped value back so `G`'s sentinel settles on the true bottom
    and `k` then steps up one line at a time (same contract as
    `_clamp_session_scroll`)."""
    capacity = _prompt_capacity(term_height)
    return max(0, min(scroll, max(0, line_count - capacity)))


def build_prompt_panel(
    session_id: str,
    term_height: int,
    text: str | None,
    ts: str = "",
    agent_id: str | None = None,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    agent_map: dict[str, dict] | None = None,
    scroll: int = 0,
) -> Panel:
    """Full-screen view of one whole prompt (Enter from the command modal, and
    Enter again from a Prompts-tab turn).

    Takes the identity it shows (`session_id`, `agent_id`, `ts`) and the body
    (`text`) as plain values rather than a `LogEntry`, because the two callers
    have different rows: the command modal derives them from its log entry and
    the store's prompt dict, the turn page from the turn itself. `agent_id` is
    None on a main-thread turn, which drops the Agent row.

    A prompt runs to thousands of characters, so the body is windowed around
    `scroll` (j/k/g/G) exactly like the session modal's subagent list rather than
    being cropped by the panel height. The header keeps the row's identity
    (session, subagent, prompt time) visible so it is clear WHICH turn this is.
    """
    ctx = RenderCtx(name_map=name_map, color_map=color_map)
    lines = _text_lines(text)

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right", no_wrap=True)
    meta.add_column(ratio=1)
    meta.add_row("Session", _session_cell(session_id, ctx))
    label = agent_id_label(agent_id, agent_map or {})
    if label is not None:
        meta.add_row("Agent", Text(label, style=AGENT_STYLE))
    # The store hands back its own timestamp string; slice off the sub-second and
    # offset tail rather than parsing it (no fromisoformat, no pytz).
    when = (ts or "")[:19]
    if when:
        meta.add_row("Sent", Text(when))

    n = len(lines)
    scroll = _clamp_prompt_scroll(scroll, n, term_height)
    win_start = scroll
    win_end = min(n, scroll + _prompt_capacity(term_height))

    # The keys live in the header, not under the body: prompt lines wrap, so a
    # windowful can outgrow its rows and a footer hint would be the first thing
    # the panel's height crop ate - exactly when the reader needs the way out.
    header = Text.from_markup(
        f"[bold]Prompt[/bold] [dim]({n} line{'' if n == 1 else 's'})[/dim]")
    if (win_start, win_end) != (0, n):
        header.append(f"  {win_start + 1}-{win_end} of {n}", style="dim")
    header.append("    esc / ↵ : back    q : close    j/k g/G : scroll",
                  style="dim italic")

    body = (Text("\n".join(lines[win_start:win_end])) if lines
            else Text("(empty)", style="dim italic"))

    return Panel(
        Group(meta, Text(""), header, _prompt_box(body)),
        title=_panel_title("· prompt"),
        subtitle=Text(str(LOG_PATH), style="dim"),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


# Status glyph + Rich style per derived subagent run state (see SubagentInfo).
_SUBAGENT_GLYPHS = {
    "running": ("●", "bold green"),  # ●
    "done": ("✓", "green"),          # ✓
    "stopped": ("✗", "red"),         # ✗
}


def _tilde(path: str) -> str:
    """Abbreviate a leading home directory to `~` (folder display)."""
    if not path:
        return "(unknown)"
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _session_started_line(model: "SessionModel") -> str:
    """`HH:MM  ·  kind: <kind>` from a session's start time, or a fallback."""
    when = "(unknown)"
    if model.started_at:
        try:
            when = datetime.fromtimestamp(model.started_at / 1000).strftime("%H:%M")
        except (ValueError, OSError, OverflowError):
            when = "(unknown)"
    if model.kind:
        return f"{when}  ·  kind: {model.kind}"
    return when


def _format_session_tokens(tokens: dict) -> Text:
    """The Tokens cell for the session-detail modal: exact per-session totals
    (generation output, input, combined cache, request count) from the audit
    store; subagent output is called out when any subagent ran. The
    `_tokens_unavailable` sentinel renders as a dim hint instead of totals."""
    if "unavailable" in tokens:
        return Text(tokens["unavailable"], style="dim italic")
    text = Text()
    text.append(f"{tokens['output_tokens']:,}", style="bold")
    text.append(" output", style="dim")
    text.append(f"  ·  {tokens['input_tokens']:,} input", style="dim")
    cache = tokens["cache_read_tokens"] + tokens["cache_creation_tokens"]
    text.append(f"  ·  {cache:,} cache", style="dim")
    text.append(f"  ·  {tokens['requests']:,} req", style="dim")
    if tokens.get("subagent_output_tokens"):
        text.append(f"  ·  {tokens['subagent_output_tokens']:,} by subagents", style="dim")
    return text


def build_session_panel(
    model: "SessionModel",
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    term_height: int = 40,
    tokens: dict | None = None,
    highlight_agent_id: str | None = None,
    agent_map: dict[str, dict] | None = None,
    scroll: int = 0,
) -> Panel:
    """Full-screen session-detail view (opened with `s`).

    A metadata header (id, name tinted by the session's `/color` plus its live
    status, `~`-abbreviated folder, full transcript path, version, start time,
    and, when the audit store has data, a Tokens total row) followed by a table
    of the session's subagents: status glyph (`●` running / `✓` done /
    `✗` stopped), description, agent type, and the tool-call count (`N+` when the
    count was byte-capped). Empty state reads "No subagents."
    `load_session_model` is cached, so rebuilding this each frame is cheap and
    still reflects live running->done transitions.

    When opened from a subagent row, `highlight_agent_id` is that row's agent id:
    a `Subagent` metadata row surfaces its complete name (the thing the Agent
    column truncates) and its row in the subagents table is reversed. Opened from
    a main-agent row it is None, so the page renders exactly as before.

    `scroll` windows the subagent list (nav keys drive it while the modal is
    open) so a long list does not overflow the height-cropped panel; it is
    clamped here for display and the header shows an `n-m of N` range while
    windowed. A list that fits renders whole with no range hint.
    """
    sid = model.session_id
    color = rich_color(color_map.get(sid)) if color_map else None

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right", no_wrap=True)
    meta.add_column(ratio=1)

    name = Text(model.name or session_label(sid, name_map), style=color or "")
    if model.status:
        name.append(f"  ({model.status})", style="bold green")
    # Every value on this page comes from pid.json / the transcript / an agent
    # meta file, so each one is wrapped in Text rather than left a bare str for
    # Rich to parse as markup (see the note under `Column`).
    meta.add_row("Session", Text(sid or "(none)"))
    meta.add_row("Name", name)
    # Opened from a subagent row: surface that subagent's complete name (the
    # Agent column ellipsizes it) right under Name, tinted like the feed.
    if highlight_agent_id is not None:
        info = next(
            (s for s in model.subagents if s.agent_id == highlight_agent_id), None
        )
        if info is not None:
            full = f"{short_agent_id(info.agent_id)}  {info.description}".rstrip()
            if info.agent_type:
                full += f"  ({info.agent_type})"
            meta.add_row("Subagent", Text(full, style=AGENT_STYLE))
    meta.add_row("Folder", Text(_tilde(model.cwd)))
    meta.add_row("Transcript", Text(model.transcript_path or "(none)", style="dim"))
    meta.add_row("Version", Text(model.version or "(unknown)"))
    meta.add_row("Started", Text(_session_started_line(model)))
    if tokens:
        meta.add_row("Tokens", _format_session_tokens(tokens))

    # Window the subagent list around `scroll` so it never overflows the
    # height-cropped panel (mirrors the search panel's result windowing).
    n = len(model.subagents)
    scroll = _clamp_session_scroll(scroll, model, term_height)
    win_start = scroll
    win_end = min(n, scroll + _session_capacity(term_height))
    windowed = model.subagents[win_start:win_end]

    running = sum(1 for s in model.subagents if s.status == "running")
    header = Text.from_markup(
        f"[bold]Subagents[/bold] [dim]({len(model.subagents)}, {running} running)[/dim]"
    )
    if (win_start, win_end) != (0, n):
        header.append(f"  {win_start + 1}-{win_end} of {n}", style="dim")

    if model.subagents:
        subs = Table(box=None, pad_edge=False, header_style="dim", expand=True)
        subs.add_column(" ", width=1)
        subs.add_column("Subagent", ratio=1, no_wrap=True, overflow="ellipsis")
        subs.add_column("Type", width=16, no_wrap=True, overflow="ellipsis")
        subs.add_column("Status", width=8)
        subs.add_column("Cmds", justify="right", width=5)
        for s in windowed:
            glyph, gstyle = _SUBAGENT_GLYPHS.get(s.status, ("?", "dim"))
            count = f"{s.command_count}+" if s.command_count_capped else str(s.command_count)
            count_cell = count if s.command_count else Text(count, style="dim")
            short = short_agent_id(s.agent_id)
            # Reverse the spotlighted subagent's row (opened from its feed row).
            row_style = "reverse" if s.agent_id == highlight_agent_id else None
            subs.add_row(
                Text(glyph, style=gstyle),
                Text(f"{short}  {s.description}" if s.description else short),
                Text(s.agent_type),
                Text(s.status, style=gstyle),
                count_cell,
                style=row_style,
            )
        subagent_block = subs
    else:
        subagent_block = Text("No subagents.", style="dim italic")

    hint = Text.from_markup("[dim italic]esc / q / ↵ : back[/dim italic]")
    body = Group(meta, Text(""), header, subagent_block, Text(""), hint)

    return Panel(
        body,
        title=_panel_title("· session"),
        subtitle=Text(model.transcript_path, style="dim"),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


def build_db_panel(info: list, term_height: int = 40) -> Panel:
    """Full-screen audit-store database view (opened with `d`).

    A right-aligned label / value grid of the store's connection string, DuckDB
    and schema versions, on-disk sizes, block accounting, and per-table row
    counts (from `claude_trail_store.db_info`). The rows are built on demand so
    the normal feed never imports duckdb; an unavailable store renders a single
    status row instead of raising.
    """
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right", no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    for label, value in info:
        # Both sides as Text: the connection string is a path from
        # $CLAUDE_TRAIL_DB and the failure rows carry a raw exception message, so
        # neither is markup-safe (see the note under `Column`).
        grid.add_row(Text(str(label)), Text(str(value)))
    hint = Text.from_markup("[dim italic]esc / q / ↵ : back[/dim italic]")
    body = Group(grid, Text(""), hint)
    return Panel(
        body,
        title=_panel_title("· database"),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


def _search_root_base(search: "SearchState") -> str:
    """The dir to display match paths relative to (the active root's parent).

    For the transcript root the first path is `<sid>.jsonl`, so its parent (the
    project dir) is the base and `subagents/...` hits show relatively. For the
    cwd root the first path is the cwd dir itself, so it is the base.
    """
    paths = search.roots.get(search.root, [])
    if not paths:
        return ""
    first = str(paths[0])
    return first if os.path.isdir(first) else os.path.dirname(first)


def _rel_to(path: str, base: str) -> str:
    """`path` relative to `base` (unchanged if `base` is empty or unrelated)."""
    if not base:
        return path
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def _result_window(cursor: int, total: int, capacity: int) -> tuple[int, int]:
    """`(start, end)` slice of results to render so `cursor` stays on screen.

    Stateless (SearchState keeps no scroll offset): when the list is longer than
    `capacity` the cursor is centered in the viewport, clamped so the window
    never runs past either end. Returns the whole list when it already fits.
    """
    if capacity <= 0 or total <= capacity:
        return 0, total
    start = max(0, min(cursor - capacity // 2, total - capacity))
    return start, start + capacity


def _session_capacity(term_height: int) -> int:
    """Subagent rows that fit in the session modal once its chrome is accounted
    for (mirrors `visible_row_count` / the search panel's capacity math)."""
    return max(term_height - SESSION_CHROME_ROWS, MIN_VISIBLE_ROWS)


def _clamp_session_scroll(scroll: int, model: "SessionModel", term_height: int) -> int:
    """Clamp a session-modal scroll offset to `[0, len(subagents) - capacity]`.

    Shared by `build_session_panel` (display safety) and `render_panel`, which
    writes the clamped value back to `AppState` each frame so `G`'s sentinel
    (10**9) settles on the true bottom and `k` then steps up one row at a time.
    """
    capacity = _session_capacity(term_height)
    return max(0, min(scroll, max(0, len(model.subagents) - capacity)))


def build_search_panel(search: "SearchState", term_height: int = 40) -> Panel:
    """Full-screen per-session search view (opened with `/`).

    Header: the `/ <query>` line (a cursor block in INPUT mode) plus a tag with
    the match count / `capped` marker / error; a root chip row showing which of
    `transcript` / `cwd` is active (a root with no resolvable path is struck
    through). Body: in RESULTS a table of `(root-relative path, line, snippet)`
    with the cursor row highlighted, else an empty-state or the type-a-query
    hint. Footer: mode-specific key hints plus a transient green `flash` line
    confirming the last open-folder target.
    """
    query = Text("/ ", style="bold cyan")
    query.append(search.query or "")
    if search.mode == "INPUT":
        query.append("▊", style="cyan")  # cursor block

    n = len(search.results)
    # Window the results around the cursor so a hit past the first screenful is
    # actually visible (the panel is height-cropped, so every row must fit).
    capacity = max(
        term_height - SEARCH_CHROME_ROWS - (1 if search.flash else 0),
        MIN_VISIBLE_ROWS,
    )
    win_start, win_end = _result_window(search.cursor, n, capacity)

    tag = Text()
    if search.error:
        tag.append(search.error, style="red")
    elif search.mode == "RESULTS":
        tag.append(f"{n} match{'es' if n != 1 else ''}", style="yellow")
        if search.capped:
            tag.append(f"  (capped {SEARCH_RESULT_LIMIT})", style="red")
        if (win_start, win_end) != (0, n):
            tag.append(f"  {win_start + 1}-{win_end} of {n}", style="dim")
    else:
        tag.append("[Tab] switch root", style="dim")

    head = Table.grid(expand=True)
    head.add_column(ratio=1)
    head.add_column(justify="right")
    head.add_row(query, tag)

    roots = Text("  root:  ", style="dim")
    for r in ("transcript", "cwd"):
        if r == search.root:
            style = "bold reverse cyan"
        elif r in search.roots:
            style = "dim"
        else:
            style = "dim strike"  # unavailable root
        roots.append(f" {r} ", style=style)

    if search.mode == "RESULTS" and search.results:
        tbl = Table(box=None, pad_edge=False, header_style="dim", expand=True)
        tbl.add_column("File", width=44, no_wrap=True, overflow="ellipsis")
        tbl.add_column("Line", justify="right", width=6)
        tbl.add_column("Match", ratio=1, no_wrap=True, overflow="ellipsis")
        base = _search_root_base(search)
        for i in range(win_start, win_end):
            path, line, text = search.results[i]
            # A grep hit is the most `[`-dense text in the program (source code,
            # JSON, markdown), so both the path and the snippet are Text rather
            # than a bare str Rich would parse (see the note under `Column`).
            tbl.add_row(
                Text(_rel_to(path, base)), str(line), Text(text.strip()),
                style="on grey23" if i == search.cursor else None,
            )
        body = tbl
    elif search.mode == "RESULTS":
        body = Text("no matches", style="dim italic")
    else:
        body = Text(
            "type a query, then ↵ to search  ·  Tab switches root",
            style="dim italic",
        )

    hint = Text.from_markup(
        "[dim italic]↵ run · tab root · esc cancel[/dim italic]"
        if search.mode == "INPUT"
        else "[dim italic]j/k move · ↵ open · f folder · "
             "/ edit · tab root · esc back[/dim italic]"
    )

    parts = [head, roots, Text(""), body, Text("")]
    if search.flash:
        parts.append(Text(f"  ▸ {search.flash}", style="green"))
    parts.append(hint)

    return Panel(
        Group(*parts),
        title=_panel_title("· search"),
        subtitle=Text(f"session {search.sid[:8]} · {search.root}", style="dim"),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


def visible_row_count(term_height: int) -> int:
    """Rows available for the table body once the panel chrome is accounted for."""
    return max(term_height - CHROME_ROWS, MIN_VISIBLE_ROWS)


def prompts_row_count(term_height: int) -> int:
    """Rows available for the Prompts tab's table body (one more chrome line than
    the feed: its snapshot header). Same floor, so a tiny terminal still shows
    rows rather than none."""
    return max(term_height - PROMPTS_CHROME_ROWS, MIN_VISIBLE_ROWS)


def count_active_sessions(
    entries: list[LogEntry], now: datetime, window: int = ACTIVE_WINDOW_SECONDS
) -> int:
    """Count DISTINCT session_ids with an entry within the last `window` seconds.

    `now` should be timezone-aware. Entries whose timestamp fails to parse, or
    whose naive/aware-ness makes the subtraction invalid, are ignored (the
    `parse_ts` parse and the elapsed-time check share one try/except, matching
    the original inline logic exactly).
    """
    active = set()
    for entry in entries:
        try:
            ts = parse_ts(entry.get("timestamp", ""))
            if (now - ts).total_seconds() < window:
                active.add(entry.get("session_id", ""))
        except (ValueError, TypeError):
            pass
    return len(active)


def _tab_strip(active: str) -> Text:
    """The tab strip: the first line of a panel's body, one chip per `TABS` entry.

    The active tab is `bold reverse` and the others `dim`, so which view is on
    screen is readable without colour.
    """
    strip = Text("  ")
    for tab in TABS:
        strip.append(f" {tab.capitalize()} ",
                     style="bold reverse" if tab == active else "dim")
        strip.append(" ")
    return strip


# Key hints per tab: the tabs answer different keys (the feed has `/`, `f` and
# `c`; the prompts list has `r`, `x` and `S`), so one shared line would advertise
# keys that do nothing on the view you are looking at.
_TAB_HINTS = {
    "commands": ("j/k:nav  ↵:view  s:session  d:db  o:log  t:transcript  "
                 "/:search  f:folder  tab:prompts  q:quit  c:clear"),
    "prompts": ("j/k:nav  ↵:view  r:refresh  x:hide empty  S:session only  "
                "s:session  o:log  t:transcript  d:db  tab:commands  q:quit"),
}


def _tab_status(tab: str, visible_cols: dict[int, bool]) -> Text:
    """The hints line for `tab`, prefixed by its column toggles.

    A tab with no registered columns (empty `visible_cols`) shows the hints
    alone rather than an empty `cols:` label.
    """
    hints = _TAB_HINTS.get(tab, "")
    if not visible_cols:
        return Text.from_markup(f"  [dim italic]{hints}[/dim italic]")
    col_toggles = " ".join(
        f"[bold]{k}[/bold]" if visible_cols[k] else f"[dim]{k}[/dim]"
        for k in sorted(visible_cols)
    )
    return Text.from_markup(
        f"  [dim italic]cols:[/dim italic] {col_toggles}"
        f"  [dim italic]·  {hints}[/dim italic]"
    )


def build_panel(
    entries: list[LogEntry],
    term_height: int,
    visible_cols: dict[int, bool],
    cursor: int,
    offset: int = 0,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    agent_map: dict[str, dict] | None = None,
) -> Panel:
    """Wrap the table (or the empty-state hint) plus the status bar in a Panel.

    This is the Commands tab, so its strip marks that tab active and
    `visible_cols` is that tab's flat `{key: shown}` dict (see
    `AppState.visible_cols_for`); the Prompts tab has `build_prompts_panel`.

    Cursor/offset contract matches `build_table`: the display is newest-first,
    the highlighted row is at `highlight_idx = cursor - offset`, and `offset`
    skips the N newest rows. The status bar reports `cursor + 1`/`len(entries)`
    (1-based over the newest-first view).
    """
    max_rows = visible_row_count(term_height)

    if not entries:
        content = Text("Waiting for commands…\n\n", style="dim italic")
        content.append("Make sure the hook is configured in ~/.claude/settings.json\n", style="dim")
        content.append(f"Watching: {LOG_PATH}", style="dim")
    else:
        content = build_table(entries, max_rows, visible_cols, cursor, offset,
                              name_map, color_map, agent_map)

    active_count = count_active_sessions(entries, datetime.now().astimezone())

    status = Text()
    status.append(" \u25cf", style="bright_green")
    status.append(f"  {active_count} active", style="bold yellow" if active_count > 0 else "dim")
    if entries:
        status.append(f"  \u00b7  {cursor + 1}/{len(entries)}", style="dim")
    else:
        status.append("  \u00b7  0/0", style="dim")

    body = Group(_tab_strip("commands"), Text(""), content, Text(""), status,
                 _tab_status("commands", visible_cols))

    return Panel(
        body,
        title=_panel_title(),
        subtitle=Text(str(LOG_PATH), style="dim"),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


def build_prompts_table(
    rows: list["PromptTurn"],
    max_rows: int,
    visible_cols: dict[int, bool],
    cursor: int,
    offset: int = 0,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
) -> Table:
    """Render up to `max_rows` turns as a Rich table, newest first.

    Cursor/offset contract, and the one way it differs from `build_table`: the
    turn list is ALREADY newest-first (that is the order `prompt_turns` returns
    it), so nothing is reversed here - `cursor` and `offset` index `rows`
    directly. `offset` skips the N newest turns and the cursor is highlighted at
    `cursor - offset` inside the displayed slice; with the cursor scrolled out of
    view that index falls outside the slice and no row is highlighted.

    There is no per-row `RenderCtx`: the agent tree gutter is a feed thing (one
    row per command), while a turn row is one prompt and names its subagents by
    count.
    """
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_edge=False,
        pad_edge=False,
        row_styles=["", "dim"],
    )

    ctx = RenderCtx(name_map=name_map, color_map=color_map)
    active = [col for col in PROMPT_COLUMNS if visible_cols.get(col.key)]
    for col in active:
        table.add_column(col.name, style=col.style, **col.kwargs)

    display = rows[offset : offset + max_rows]
    highlight_idx = cursor - offset
    for idx, turn in enumerate(display):
        row_style = "on grey23" if idx == highlight_idx else None
        table.add_row(*[col.render(turn, ctx) for col in active], style=row_style)

    return table


def _format_age(seconds: float) -> str:
    """A snapshot age as `12s ago` / `4m ago` / `1h 07m ago`."""
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m ago"


def _snapshot_age(stamp: float | None, now: float | None = None) -> float | None:
    """Seconds elapsed since a `PromptsState` stamp, or None if it was never set.

    Both `loaded_at` (data freshness, for the header) and `checked_at` (the
    refresh backoff) go through here. They are `time.monotonic()` values, so the
    answer is immune to a wall-clock jump: an NTP step must not make a fresh
    snapshot look an hour old, nor defer the auto-refresh by an hour.
    """
    if stamp is None:
        return None
    return max(0.0, (time.monotonic() if now is None else now) - stamp)


def _prompts_header(prompts: "PromptsState", now: float | None = None,
                    name_map: dict[str, str] | None = None) -> Text:
    """The line above the turn list: how fresh the snapshot is, how much of it is
    on screen, what it is scoped to, and any store status.

    The `as of` clock time is derived by subtracting the monotonic age from the
    current wall clock, rather than stored alongside it: one stamp then answers
    both "how old" and "as of when", with no second timestamp to drift out of
    sync with it.

    `name_map` is the feed's, so the scope names the session the same way the
    Session column does (`session_label`): the header used to show a raw id prefix
    next to a column of resolved names, which read as two different sessions.
    """
    line = Text("  ")
    age = _snapshot_age(prompts.loaded_at, now)
    if age is None:
        line.append("not read yet", style="dim italic")
    else:
        when = (datetime.now() - timedelta(seconds=age)).strftime("%H:%M:%S")
        line.append(f"as of {when} ", style="dim")
        line.append(f"({_format_age(age)})", style="dim italic")

    shown, hidden = len(prompts.visible_rows()), prompts.hidden_count()
    line.append(f"  ·  {shown} turn{'' if shown == 1 else 's'}", style="dim")
    if hidden:
        line.append(f" ({hidden} hidden)", style="dim italic")
    if prompts.session_filter:
        line.append(f"  ·  session {session_label(prompts.session_filter, name_map)}",
                    style="dim")
    if prompts.status:
        line.append(f"  ·  {prompts.status}", style="yellow")
    return line


def _prompts_empty_state(prompts: "PromptsState") -> Text:
    """The body when no turn is on screen, saying WHY and what would fix it.

    Five different empties, and telling them apart is the point: advising
    `claude-trail ingest` when the store is already current, when the store is
    there but unreadable, or when `x` is simply hiding every row, would be wrong
    advice on a working store.
    """
    text = Text()
    if prompts.status and not prompts.rows:
        # The store could not answer at all (no file, unreadable, or held by the
        # ingest with nothing kept). `status` already says which, and it must be
        # said HERE too: the "no prompts in the store" fallback below would blame
        # an empty store for a read that never happened. Gated on `not rows` so a
        # kept snapshot filtered empty by `x` still gets the un-hide advice.
        text.append("The audit store could not be read.\n\n", style="dim italic")
        text.append(f"{prompts.status}\n\n", style="dim")
        text.append("Press r to try again.", style="dim")
    elif prompts.loaded_at is None:
        text.append("No prompts read yet.\n\n", style="dim italic")
        text.append(
            "This tab reads the audit store, not the live command feed.\n", style="dim")
        text.append("Press r to read it, or fill it with: claude-trail ingest",
                    style="dim")
    elif prompts.rows:  # loaded rows, all filtered out
        hidden = prompts.hidden_count()
        text.append("Every turn is hidden.\n\n", style="dim italic")
        text.append(f"{hidden} turn{'' if hidden == 1 else 's'} ran no tool call; "
                    "press x to show them again.", style="dim")
    elif prompts.session_filter:
        text.append("No prompts for this session.\n\n", style="dim italic")
        text.append("Press S to go back to every session.", style="dim")
    else:
        text.append("No prompts in the audit store.\n\n", style="dim italic")
        text.append("Fill it with: claude-trail ingest", style="dim")
    return text


def build_prompts_panel(
    prompts: "PromptsState",
    term_height: int,
    visible_cols: dict[int, bool] | None = None,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    now: float | None = None,
) -> Panel:
    """The Prompts tab: tab strip, snapshot header, the turn list, and the status
    bar (cursor position plus this tab's key hints).

    Sized from `term_height` exactly like `build_panel`: the body gets
    `prompts_row_count(term_height)` rows, which is what keeps the status line and
    the hints on screen on a short terminal instead of being cropped away.

    The list comes from the audit store rather than the live command log, so a
    stale or absent snapshot is a normal state and the header always says which
    one it is (see `_prompts_header` / `_prompts_empty_state`). `name_map` and
    `color_map` are the feed's, so a session the feed has also seen gets its name
    and `/color` tint and any other falls back to its short id - resolving tints
    for 200 turns would mean tailing 200 transcripts for a colour.
    """
    max_rows = prompts_row_count(term_height)
    rows = prompts.visible_rows()
    # A falsy dict (an explicit empty one, or none passed) means "this tab's
    # default", never "hide every column": a table with no columns is not a view.
    cols = visible_cols or _default_cols("prompts")

    if rows:
        content: Text | Table = build_prompts_table(
            rows, max_rows, cols, prompts.cursor, prompts.scroll_offset,
            name_map, color_map)
    else:
        content = _prompts_empty_state(prompts)

    status = Text()
    status.append(f"  {prompts.cursor + 1}/{len(rows)}" if rows else "  0/0",
                  style="dim")

    body = Group(_tab_strip("prompts"), Text(""),
                 _prompts_header(prompts, now, name_map),
                 content, Text(""), status, _tab_status("prompts", cols))

    return Panel(
        body,
        title=_panel_title(),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


# ---- Prompts tab: one turn's detail page (`↵`) ----

# The two label helpers below are the store's, kept reachable under their old
# names here because this is where the turn page reads them. Both are pure text
# over a stored row and are shared verbatim with `claude-trail prompts`, so the
# store owns them and the TUI imports them (the direction CLAUDE.md states) -
# never the other way round.
def action_label(tool_name: str, text: str | None, tool_input: str | None) -> str:
    """`claude_trail_store.action_label(...)`: what one tool call did, in a line.

    The wording is shared with `claude-trail prompts`, so it lives in the store
    next to the turn model both readers query (with `_ACTION_FIELDS`, the field
    list it reads off `tool_input`). Imported lazily for the reason `_slash_label`
    gives: the feed and the hook must not pay for that module, and by the time a
    turn action exists the prompts loader has already imported it.
    """
    from claude_trail_store import action_label as _action_label
    return _action_label(tool_name, text, tool_input)


def _short_tool_name(tool_name: str) -> str:
    """`claude_trail_store.short_tool_name(...)`: an MCP tool's own name, without
    its server prefix (the Tool column is too narrow to spend on routing).

    Lives in the store with `action_label` for the same reason, and is imported
    the same lazy way."""
    from claude_trail_store import short_tool_name
    return short_tool_name(tool_name)


def _action_tool_cell(action: "TurnAction") -> Text:
    """The Tool cell: the (shortened) tool name, dim red when the call failed.

    `is_error` comes from the joined `tool_result`, so a call still awaiting its
    result renders like a successful one - which is right, since nothing has gone
    wrong yet."""
    name = _short_tool_name(action.tool_name or "?")
    return Text(name, style="dim red" if action.is_error else "")


def _action_detail_cell(action: "TurnAction") -> Text:
    """The Detail cell: what the call did, on one line.

    Bash keeps the feed's danger marker (`_danger_prefixed`), so a `rm -rf` is as
    loud here as it is in the command feed. Whitespace is collapsed like the
    feed's Command column, because a label can be a multi-line heredoc or a
    subagent's paragraph-long description and this is a one-row cell.
    """
    label = action_label(action.tool_name, action.text, action.tool_input)
    if not label:
        return Text("-", style="dim")
    body = normalize_cmd(label)
    if action.tool_name == "Bash":
        return _danger_prefixed(label, body)
    return Text(body)


def _format_duration(seconds: float) -> str:
    """An elapsed time as `12s` / `6m 12s` / `1h 07m`."""
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def _turn_duration(start: str, end: str | None) -> str | None:
    """How long the turn's work ran: from its prompt to the last API request the
    store attributed to its window, or None when there is no such request or
    either end will not parse.

    `last_ts` and not `next_ts`: `next_ts` is when the user next typed, so a turn
    answered in seconds and left on screen overnight would report 14 hours.

    Both ends are store strings, so both carry the hours-only UTC offset
    `parse_ts` widens; parsing them directly is what made this chip vanish on
    Python 3.10.
    """
    if not start or not end:
        return None
    try:
        elapsed = (parse_ts(end) - parse_ts(start)).total_seconds()
    except (ValueError, TypeError):
        return None
    return _format_duration(elapsed) if elapsed >= 0 else None


def _turn_work_line(turn: "PromptTurn") -> Text:
    """The Work row: `64 tool calls: 23 main, 41 across 2 subagents  ·  14 Bash`.

    `turn.tools` is INCLUSIVE of `turn.subcalls` (one main-thread Bash plus two
    subagent calls is `tools=3, subcalls=2`), so the main-thread count is a
    SUBTRACTION. Adding the two would report 105 calls for a turn that made 64.

    The Bash count is window-wide for the same reason the list's Calls column
    says so - the query filters the whole window, subagents included - so it is
    called out on its own rather than nested under the main-thread count, which
    would claim subagent Bash calls for the main thread.
    """
    if not turn.tools:
        return Text("no tool calls", style="dim")
    line = Text()
    line.append(f"{turn.tools} tool call{'' if turn.tools == 1 else 's'}", style="bold")
    if turn.subcalls:
        main = max(0, turn.tools - turn.subcalls)
        line.append(
            f": {main} main, {turn.subcalls} across {turn.agents} "
            f"subagent{'' if turn.agents == 1 else 's'}", style="dim")
    if turn.bash:
        line.append(f"  ·  {turn.bash} Bash", style="dim")
    return line


def _turn_tokens_line(turn: "PromptTurn") -> Text:
    """The Tokens row: the turn's generation cost and how many requests it took.

    The numbers are the ones the list query already computed for this row, so
    opening a turn costs no extra token query. `requests == 0` means the store
    attributed no API request to the window, normally because the ingest has not
    caught up with the turn yet - which is what PENDING_INGEST_HINT says. The
    other half of `_tokens_unavailable` (no store at all) is unreachable here:
    this page only exists because the store answered the list query.
    """
    if not turn.requests:
        return Text(PENDING_INGEST_HINT, style="dim italic")
    line = Text()
    line.append(f"{turn.output_tokens:,}", style="bold")
    line.append(" output", style="dim")
    if turn.subagent_output_tokens:
        line.append(f"  ·  {turn.subagent_output_tokens:,} by subagents", style="dim")
    line.append(f"  ·  {turn.requests:,} request{'' if turn.requests == 1 else 's'}",
                style="dim")
    return line


def _turn_prompt_lines(prompts: "PromptsState") -> list[str]:
    """The prompt lines the turn page quotes: the full text when `↵` loaded one,
    else the row's own 400-char preview.

    One definition, because the page's geometry (`_prompt_box_rows`) and its
    render have to agree on the count - a box measured off the full text while
    the page quotes the shorter preview would mis-size the action list."""
    turn = prompts.detail
    return (_text_lines(prompts.detail_text)
            or _text_lines(turn.preview if turn else None))


def _prompt_box_rows(lines: list[str], limit: int = TURN_PROMPT_PREVIEW_LINES) -> int:
    """Rows the turn page's prompt box occupies: its 2 borders plus the body
    `_preview_body` builds from `lines` (the quoted lines, or the one-row
    `(empty)` placeholder, plus a blank and the `+N more lines` footer when it
    hides any).

    Measured per turn rather than reserved at the worst case, because the worst
    case is rare: 135 of the newest 200 turns in a real store have a one-line
    prompt (151 have two or fewer), where a fixed 6-row reservation left 3 of the
    9 action rows at h=24 permanently blank. It can be exact at any width because
    the page quotes its prompt `no_wrap` - one display row per line (see
    `_preview_body`)."""
    body = min(len(lines), limit) or 1        # `(empty)` still costs its row
    return 2 + body + (2 if len(lines) > limit else 0)


def _turn_capacity(term_height: int, prompt_rows: int) -> int:
    """Action rows that fit under the turn page's header (mirrors
    `_session_capacity`).

    `prompt_rows` is what the quoted prompt costs (`_prompt_box_rows`); it is a
    parameter and not part of TURN_CHROME_ROWS because it varies per turn."""
    return max(term_height - TURN_CHROME_ROWS - prompt_rows, MIN_VISIBLE_ROWS)


def _clamp_turn_scroll(scroll: int, count: int, term_height: int,
                       prompt_rows: int) -> int:
    """Clamp an action-list scroll offset to `[0, count - capacity]`.

    Shared by `build_turn_panel` (display safety) and `render_panel`, which
    writes the clamped value back so `G`'s sentinel settles on the true bottom
    and `k` then steps up one row at a time (same contract as
    `_clamp_session_scroll`). Both pass the same `prompt_rows`, or the clamp
    would stop `G` short of the bottom row the panel actually draws."""
    capacity = _turn_capacity(term_height, prompt_rows)
    return max(0, min(scroll, max(0, count - capacity)))


def build_action_table(
    actions: list["TurnAction"],
    agent_map: dict[str, dict] | None = None,
) -> Table:
    """The turn page's action list: one row per tool call, oldest-first.

    Oldest-first is the order `turn_actions` returns and the order the work
    happened in, which is what makes the page readable as a story - the opposite
    of the feed, which is a tail and so is newest-first.

    The Agent column is the feed's tree gutter (`_agent_gutter`) over runs of
    adjacent calls sharing an `agent_id`, so a subagent's stretch of work reads
    as one bracketed block with its label shown once. Attribution here is
    RETROACTIVE - the ids come from the store's transcripts, so a turn from
    before the hook learned to tag subagent commands still shows them - where the
    feed's is forward-only.

    Runs are computed over exactly the rows passed in (already windowed and
    filtered by the caller), so the bracket closes at the bottom of the screen
    rather than pointing off it.
    """
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_edge=False,
        pad_edge=False,
        row_styles=["", "dim"],
    )
    table.add_column("Time", style="cyan", width=8, no_wrap=True)
    table.add_column("Agent", style=AGENT_STYLE, width=20,
                     no_wrap=True, overflow="ellipsis")
    table.add_column("Tool", style="magenta", width=16,
                     no_wrap=True, overflow="ellipsis")
    table.add_column("Detail", style="white", ratio=1,
                     no_wrap=True, overflow="ellipsis")

    ids = [action.agent_id or None for action in actions]
    labels = [agent_id_label(aid, agent_map or {}) for aid in ids]
    for idx, action in enumerate(actions):
        run_first = idx == 0 or ids[idx] != ids[idx - 1]
        run_last = idx == len(actions) - 1 or ids[idx] != ids[idx + 1]
        table.add_row(
            Text(format_time(action.ts)),
            _agent_gutter(labels[idx], run_first, run_last),
            _action_tool_cell(action),
            _action_detail_cell(action),
        )
    return table


def _action_empty_state(prompts: "PromptsState") -> Text:
    """The body when the action list has no row, saying which of the three
    reasons it is: the store could not answer, the turn genuinely ran nothing, or
    `b` is hiding every call."""
    if prompts.actions is None:
        return Text(TURN_UNREADABLE_HINT, style="dim italic")
    if not prompts.actions:
        text = Text("No tool calls in this turn.\n\n", style="dim italic")
        text.append("A question, a slash command or a resumed session runs none.",
                    style="dim")
        return text
    hidden = len(prompts.actions)
    text = Text("No Bash calls in this turn.\n\n", style="dim italic")
    text.append(f"{hidden} other tool call{'' if hidden == 1 else 's'} hidden - "
                "press b to show everything.", style="dim")
    return text


def build_turn_panel(
    prompts: "PromptsState",
    term_height: int,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    agent_map: dict[str, dict] | None = None,
) -> Panel:
    """One turn's detail page: what the main agent and its subagents did in
    response to a single prompt (`↵` on the Prompts tab).

    Takes the tab's state because everything on the page is in it: `detail` is
    the turn (and must be set - this is only rendered while that modal is open),
    `actions`/`detail_text` are what the one store open on `↵` loaded,
    `bash_only` and `detail_scroll` are the view's own keys.

    Header rows are the turn's identity, the Work line (which SUBTRACTS to get
    the main-thread count, see `_turn_work_line`) and the Tokens line, then the
    prompt quoted in a box, then the action list windowed around `detail_scroll`
    like the session modal's subagent list. The prompt box falls back to the
    row's own 400-char preview if the full text did not load, so the page always
    says which prompt it is describing.

    The box's height follows the prompt (a one-line prompt has no `+N more lines`
    footer), so the window is sized against what the box will actually take
    (`_prompt_box_rows`) rather than against its worst case - which is where a
    short prompt got its three blank rows back.
    """
    turn = prompts.detail
    ctx = RenderCtx(name_map=name_map, color_map=color_map)

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right", no_wrap=True)
    meta.add_column(ratio=1)
    ident = Text()
    ident.append_text(_session_cell(turn.session_id, ctx))
    project = _turn_project_path(turn)   # the exact cwd, else the decoded dir name
    if project:
        # append (not from_markup) does not parse markup, so a project dir named
        # `[wip]` survives (see the note under `Column`).
        ident.append(f"  {short_path(project, PROJECT_LABEL_WIDTH)}", style="dim")
    ident.append(f"  ·  sent {format_time(turn.ts)}", style="dim")
    took = _turn_duration(turn.ts, turn.last_ts)
    if took:
        ident.append(f"  ·  ran {took}", style="dim")
    meta.add_row("Session", ident)
    meta.add_row("Work", _turn_work_line(turn))
    meta.add_row("Tokens", _turn_tokens_line(turn))

    lines = _turn_prompt_lines(prompts)
    prompt_rows = _prompt_box_rows(lines)
    prompt_box = _prompt_box(
        _preview_body(lines, TURN_PROMPT_PREVIEW_LINES,
                      can_open=bool(prompts.detail_text), no_wrap=True),
        padding=(0, 2),
    )

    actions = prompts.visible_actions()
    shown = len(actions)
    scroll = _clamp_turn_scroll(prompts.detail_scroll, shown, term_height, prompt_rows)
    win_start = scroll
    win_end = min(shown, scroll + _turn_capacity(term_height, prompt_rows))

    header = Text.from_markup(f"[bold]Actions[/bold] [dim]({shown})[/dim]")
    if prompts.bash_only:
        hidden = len(prompts.actions or ()) - shown
        header.append(f"  bash only, {hidden} hidden" if hidden else "  bash only",
                      style="dim italic")
    if (win_start, win_end) != (0, shown):
        header.append(f"  {win_start + 1}-{win_end} of {shown}", style="dim")

    content: Text | Table = (
        build_action_table(actions[win_start:win_end], agent_map) if actions
        else _action_empty_state(prompts))

    # `↵` drills into the full prompt when there is one and closes the page when
    # there is not, so the hint has to say which of the two it is doing here.
    keys = ("esc / q : back    ↵ : full prompt" if prompts.detail_text
            else "esc / q / ↵ : back")
    keys += "    j/k g/G : scroll    b : "
    keys += "all tools" if prompts.bash_only else "bash only"

    return Panel(
        Group(meta, Text(""), prompt_box, header, content,
              Text.from_markup(f"[dim italic]{keys}[/dim italic]")),
        title=_panel_title("· turn"),
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


# ==== Input decoding (read_key) ====


def _read_pending_byte(fd: int, timeout: float = 0.02) -> bytes:
    """Return one byte from `fd` if one arrives within `timeout`, else b""."""
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return b""
    try:
        return os.read(fd, 1)
    except OSError:
        return b""


def read_key(fd: int) -> str:
    """Read one keypress from `fd`, decoding arrow-key escape sequences.

    Reads via os.read (unbuffered) instead of sys.stdin on purpose: the poll
    loop calls select() on this same fd, and Python's buffered text stdin would
    read ahead into its own buffer where select() can't see it, dropping the
    trailing bytes of arrow sequences and stalling queued keys (the cause of
    laggy navigation). os.read keeps reads and select() consistent.
    """
    try:
        data = os.read(fd, 1)
    except OSError:
        return ""
    if not data:
        return ""
    if data == b"\x1b":
        # Consume exactly one complete escape sequence so none of its bytes
        # leak into the drain loop as stray keystrokes (a leaked digit would
        # toggle a column). CSI is ESC '[' + parameter/intermediate bytes
        # (0x20-0x3F) + one final byte (0x40-0x7E), which covers modified
        # arrows (ESC [ 1 ; 5 A) and function keys (ESC [ 1 5 ~); SS3 is
        # ESC 'O' + one final byte. Reading one sequence at a time (rather
        # than a greedy os.read(fd, 8)) leaves any following sequence on the
        # fd for the next call, so held arrows don't drop keystrokes.
        intro = _read_pending_byte(fd)
        if intro == b"[":
            seq = intro
            while len(seq) < 16:
                nxt = _read_pending_byte(fd)
                if not nxt:
                    break
                seq += nxt
                if 0x40 <= nxt[0] <= 0x7E:  # CSI final byte
                    break
        elif intro == b"O":
            seq = intro + _read_pending_byte(fd)
        else:
            return "\x1b"
        decoded = seq.decode("utf-8", errors="replace")
        # Final byte A/B is cursor up/down in both CSI and SS3, with or
        # without modifier parameters ("[A", "[1;5A", "OA").
        if decoded.endswith("A"):
            return "up"
        if decoded.endswith("B"):
            return "down"
        return "\x1b"
    return data.decode("utf-8", errors="replace")


# ==== File tailing ====


def tail_file(path: Path, last_pos: int) -> tuple[list[str], int]:
    """Read new complete lines from file starting at last_pos. Partial trailing line is left for next call."""
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if size < last_pos:
        last_pos = 0
    if size == last_pos:
        return [], last_pos
    with open(path, "rb") as f:
        f.seek(last_pos)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        return [], last_pos
    complete = data[: last_nl + 1]
    new_pos = last_pos + len(complete)
    text = complete.decode("utf-8", errors="replace")
    return text.splitlines(keepends=True), new_pos


def read_last_entries(path: Path, n: int) -> tuple[list[LogEntry], int]:
    """Read up to n most recent entries from path. Returns (entries, end_pos)."""
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    chunk_size = 8192
    data = b""
    pos = size
    # Open once and seek backward within the single handle: read fixed-size
    # chunks from the end until we have more than n newlines (or hit the start).
    with open(path, "rb") as f:
        while pos > 0 and data.count(b"\n") < n + 1:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()[-n:]
    entries = [e for e in (parse_line(line) for line in lines) if e]
    return entries, size


# ==== Hook ====


def _new_entry(
    timestamp: str,
    command: str,
    cwd: str,
    session_id: str,
    agent_id: str | None = None,
    agent_type: str | None = None,
) -> LogEntry:
    """Build a log entry in the exact key order the hook emits to JSONL.

    `agent_id`/`agent_type` are appended (in that order) only for a subagent's
    tool call; a main-thread call omits them entirely.
    """
    entry: dict[str, str] = {
        "timestamp": timestamp,
        "command": command,
        "cwd": cwd,
        "session_id": session_id,
    }
    if agent_id:
        entry["agent_id"] = agent_id
        entry["agent_type"] = agent_type or ""
    return entry


def hook_main(stream=None) -> int:
    """PostToolUse hook: read a tool event from stdin and append a Bash log entry.

    Wired up via `claude-trail hook` in ~/.claude/settings.json.
    """
    try:
        data = json.load(stream if stream is not None else sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    tool_input = data.get("tool_input") or {}
    # PostToolUse adds agent_id/agent_type only when the hook fires inside a
    # subagent (Task/Agent/fork/workflow agent); a main-thread call omits them.
    # agent_id matches the subagent's transcript filename id.
    entry = _new_entry(
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        command=tool_input.get("command", ""),
        cwd=data.get("cwd", ""),
        session_id=data.get("session_id", ""),
        agent_id=data.get("agent_id"),
        agent_type=data.get("agent_type"),
    )
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return 0


# ==== AppState + apply_key ====


class Action(enum.Enum):
    """Outcome of one keypress that main() must act on outside of state mutation."""

    QUIT = enum.auto()
    OPEN_SESSION = enum.auto()
    OPEN_TRANSCRIPT = enum.auto()   # open the selected session's pretty transcript
    OPEN_FILES = enum.auto()
    RUN_SEARCH = enum.auto()        # run rg/grep for the open search modal
    OPEN_MATCH = enum.auto()        # open the selected search hit at its line
    OPEN_MATCH_FOLDER = enum.auto()  # open the folder containing the selected hit
    LOAD_DB_INFO = enum.auto()      # load the audit-store properties for the `d` modal
    OPEN_DETAIL = enum.auto()       # command modal just opened; load its token cost + prompt
    OPEN_SESSION_DETAIL = enum.auto()  # session modal just opened; load its token total
    LOAD_PROMPTS = enum.auto()      # (re)read the prompts tab's turn list from the store
    OPEN_PROMPT = enum.auto()       # turn modal just opened; load its actions + full prompt
    NONE = enum.auto()


@dataclass
class SearchState:
    """View state for the per-session search modal (opened with `/`).

    Two sub-modes resolve the type-vs-navigate conflict: in INPUT letters build
    `query` (no navigation); in RESULTS `j`/`k` browse hits (letters do not
    type). `roots` maps "transcript"/"cwd" to the absolute paths grep runs over
    (from `search_roots`); `root` picks the active one and `Tab` flips it.
    `flash` is a transient confirmation line set by main() after an open-folder.
    """

    sid: str
    roots: dict[str, list[Path]]
    root: str = "transcript"
    query: str = ""
    mode: str = "INPUT"  # "INPUT" | "RESULTS"
    results: list[Match] = field(default_factory=list)
    cursor: int = 0
    capped: bool = False
    error: str | None = None
    flash: str | None = None


def _record_from_row(cls, row: dict):
    """Build one frozen record of `cls` from a store result dict.

    Keys the record does not declare are dropped instead of raising: the store
    owns those queries' shapes, and a TUI that died on an added column would be
    worse than one that ignores it.

    The other direction is NOT caught here: a key the query stopped returning
    (renamed, dropped) leaves that field at its default, which renders as a
    plausible zero rather than an error. What pins it is
    `tests/test_store.py::test_prompt_record_fields_match_the_query_keys`, which
    compares these field names against the keys the REAL queries return.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in row.items() if k in known})


@dataclass(frozen=True)
class PromptTurn:
    """One row of the Prompts tab: a user prompt plus what happened in its window.

    Field names and order mirror the keys of `claude_trail_store.prompt_turns`,
    so `from_row` is a straight mapping; the two key sets are pinned equal
    against the real query by a test (see `_record_from_row`, which silently
    drops a key it does not declare). Timestamps arrive as strings (`ts`,
    `next_ts`, `last_ts`); `next_ts` is None for a session's newest turn and
    `last_ts` None for a turn that emitted no API request. Every count is already
    coalesced to 0 by the query.

    Two things worth knowing before rendering these:

    - `tools` is INCLUSIVE of `subcalls`: one main-thread Bash plus two subagent
      calls is `tools=3, subcalls=2`, so a main-thread count is `tools - subcalls`
      (subtract, never add).
    - `cwd` is the session's exact launch dir and outranks `project`, which is
      the lossy decoding of the transcript dir name (`claude-trail` comes back as
      `claude/trail`). `cwd` is None for a session whose transcript has left the
      disk, and for one whose transcript carries no cwd at all; everything that
      shows a path goes through `_turn_project_path`.
    """

    session_id: str
    ts: str
    next_ts: str | None = None
    event_id: str = ""
    preview: str = ""
    full_len: int = 0
    tools: int = 0
    bash: int = 0
    agents: int = 0
    subcalls: int = 0
    output_tokens: int = 0
    subagent_output_tokens: int = 0
    requests: int = 0
    last_ts: str | None = None
    project: str | None = None
    cwd: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "PromptTurn":
        """One turn from a `prompt_turns` result dict (unknown keys dropped)."""
        return _record_from_row(cls, row)


@dataclass(frozen=True)
class TurnAction:
    """One tool call inside a turn's window, for the turn-detail list.

    Field names mirror the keys of `claude_trail_store.turn_actions`. `agent_id`
    is None for a main-thread call and the subagent's id otherwise (what the
    detail page's tree gutter groups by); `text` is None for tools the store has
    no command repr for (Agent, Skill, MCP, ...), which the renderer labels from
    `tool_input` instead; `is_error` comes from the joined `tool_result`.
    """

    ts: str
    agent_id: str | None = None
    tool_name: str = ""
    text: str | None = None
    tool_input: str | None = None
    is_error: bool | None = None

    @classmethod
    def from_row(cls, row: dict) -> "TurnAction":
        """One call from a `turn_actions` result dict (unknown keys dropped)."""
        return _record_from_row(cls, row)


@dataclass
class PromptsState:
    """View state for the Prompts tab (`AppState.prompts`).

    Nested like `SearchState` so `AppState` does not grow a dozen more fields,
    and so this tab's cursor/scroll are separate from the feed's: switching tabs
    leaves each view where the user left it.

    The first block is the list (`rows` newest-first, as the query returns them).
    `cursor`/`scroll_offset` index `visible_rows()` directly - no reversal,
    unlike the feed, whose cursor indexes a reversed view of `entries`. The
    second block is the open turn's modal; `AppState.close_modals` resets it
    through `close_detail` so every modal is still dropped in one place.
    """

    rows: list[PromptTurn] = field(default_factory=list)
    cursor: int = 0
    scroll_offset: int = 0
    # Two monotonic stamps, because "how fresh is this data" and "when did we last
    # try" are different questions once the store can refuse to answer: a lock
    # collision against the running ingest updates `checked_at` (so the retry
    # backs off to the auto-refresh interval instead of hammering a locked file
    # every poll) and leaves `loaded_at` alone (so the age line keeps counting up
    # from the snapshot actually on screen).
    loaded_at: float | None = None      # time.monotonic() of the last ANSWER, for the age line
    checked_at: float | None = None     # time.monotonic() of the last attempt, for the backoff
    status: str | None = None           # why the list is empty/stale (missing store, busy store)
    session_filter: str | None = None   # None = every session, interleaved newest-first
    hide_empty: bool = False            # drop turns with 0 tool calls
    detail: PromptTurn | None = None    # the open turn (modal)
    # None until the store answers, and again if it could not: an empty list is a
    # turn that really ran no tool call, which the page says differently.
    actions: list[TurnAction] | None = None
    detail_text: str | None = None      # full prompt text for the open turn
    detail_scroll: int = 0
    bash_only: bool = False             # `b`: show only the turn's Bash calls
    prompt_open: bool = False           # the nested full-prompt view
    prompt_scroll: int = 0

    def close_detail(self) -> None:
        """Drop the open turn's modal state, keeping the loaded list intact.

        Everything the turn page owns resets, `bash_only` included: the filter
        belongs to the turn being read, not to the tab, so the next turn opens
        showing everything it did."""
        self.detail = None
        self.actions = None
        self.detail_text = None
        self.detail_scroll = 0
        self.bash_only = False
        self.prompt_open = False
        self.prompt_scroll = 0

    def visible_rows(self) -> list[PromptTurn]:
        """The rows actually on screen: `rows`, minus zero-call turns while
        `hide_empty` is on. Every cursor/scroll operation goes through this, so
        `x` and the list agree by construction.

        `x` filters HERE instead of re-querying, for two reasons. The predicate is
        `tools == 0` on a field the row already carries, so a query would spend
        tens to hundreds of milliseconds (measured 0.65s on a 1 GB store), plus a
        chance of colliding with the ingest's write lock, to compute what is
        already in memory. More importantly `prompt_turns` applies its `LIMIT` to
        the raw turn list, so a store-side filter would return a DIFFERENT 200
        turns - reaching further back in history - and toggling `x` twice would not
        come back to the list you started on. Client-side keeps `x` a lens over one
        snapshot, which is what lets the header say `149 turns (51 hidden)` and
        mean it. The session filter is the opposite case and does re-query (see
        `_apply_prompts_key`).
        """
        if not self.hide_empty:
            return self.rows
        return [row for row in self.rows if row.tools]

    def hidden_count(self) -> int:
        """How many loaded rows the current filters keep off screen (0 when none),
        so the header can say what it is not showing."""
        return len(self.rows) - len(self.visible_rows())

    def visible_actions(self) -> list[TurnAction]:
        """The open turn's calls, minus the non-Bash ones while `bash_only` is on.

        Filtered in memory over what `↵` already loaded, exactly like
        `visible_rows`: the predicate is `tool_name == 'Bash'` on a field every
        action carries, so re-querying the store for it would cost a connection
        (and a chance of colliding with the ingest's write lock) to compute what
        is already here. Empty for a turn whose actions never loaded, so the
        panel's empty state - not this - is what tells those two apart.
        """
        if self.actions is None:
            return []
        if not self.bash_only:
            return list(self.actions)
        return [a for a in self.actions if a.tool_name == "Bash"]

    def selected(self) -> PromptTurn | None:
        """The row the cursor points at, or None when nothing is on screen."""
        rows = self.visible_rows()
        if not rows or self.cursor < 0 or self.cursor >= len(rows):
            return None
        return rows[self.cursor]

    def clamp(self, max_rows: int) -> None:
        """Keep `cursor` in `[0, len-1]` and `scroll_offset` so the cursor is
        visible, over the VISIBLE rows.

        Same contract as `AppState.clamp`, and called from the same places: after
        every cursor move, after a reload (which can shorten the list under a
        parked cursor), after `x` (which changes the list's length), and once per
        frame so a terminal resize re-anchors. Without it `G` on a long list
        followed by a shorter reload would leave the cursor past the end,
        selecting nothing and scrolling the window off the list.
        """
        rows = self.visible_rows()
        if not rows:
            self.cursor = 0
            self.scroll_offset = 0
            return
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        if self.cursor < self.scroll_offset:
            self.scroll_offset = self.cursor
        elif self.cursor >= self.scroll_offset + max_rows:
            self.scroll_offset = self.cursor - max_rows + 1
        max_offset = max(0, len(rows) - max_rows)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

    def move_up(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def move_down(self) -> None:
        self.cursor = min(max(0, len(self.visible_rows()) - 1), self.cursor + 1)

    def goto_top(self) -> None:
        self.cursor = 0
        self.scroll_offset = 0

    def goto_bottom(self) -> None:
        self.cursor = max(0, len(self.visible_rows()) - 1)


@dataclass
class AppState:
    """All view state for the TUI, independent of terminal I/O.

    INVARIANT: entries are stored oldest-first (append order); `cursor` and
    `scroll_offset` index the newest-first VIEW, where index 0 = the newest
    entry. `selected_entry()` is the single place that bridges the two by
    reversing `entries`.

    `tab` selects the active top-level view. The feed's row state
    (`entries`/`cursor`/`scroll_offset`/`visible_cols`) is the Commands tab's; the
    Prompts tab keeps its own in `prompts`, so each tab remembers its cursor
    across a switch. Column visibility is per tab too (`visible_cols_for`), while
    the modal fields below are shared: `s`, `d` and the detail views open the same
    panels from either tab.
    """

    entries: list[LogEntry]
    cursor: int = 0
    scroll_offset: int = 0
    visible_cols: dict[int, bool] = field(
        default_factory=lambda: _default_cols("commands")
    )
    tab: str = TABS[0]  # "commands": the feed stays the default view
    prompts: PromptsState = field(default_factory=PromptsState)
    prompt_cols: dict[int, bool] = field(
        default_factory=lambda: _default_cols("prompts")
    )
    detail_entry: LogEntry | None = None
    session_detail: str | None = None  # a session_id while the session modal is open
    session_agent_id: str | None = None  # subagent to spotlight on session page (None = main agent)
    session_scroll: int = 0  # scroll offset over the session modal's subagent list
    search: "SearchState | None" = None  # set while the search modal is open
    db_info: list | None = None  # (label, value) pairs while the database modal is open
    detail_tokens: dict | None = None   # token cost for the open command modal (loaded on Enter)
    session_tokens: dict | None = None  # token total for the open session modal (loaded on `s`)
    detail_prompt: dict | None = None   # prompt behind the open command modal (loaded on Enter)
    detail_prompt_open: bool = False    # the nested full-prompt view is open
    detail_prompt_scroll: int = 0       # scroll offset over the full prompt's lines

    def modal_open(self) -> bool:
        """True while a full-screen modal (command detail, session detail, the
        database view, or a prompts-tab turn) is open. Only one is ever open at a
        time; while open, navigation/column/clear keys are ignored (only Ctrl-C
        still quits). The prompts turn's own sub-state (`actions`, `detail_text`,
        `prompt_open`) only exists while `detail` is set, so `detail` alone
        answers for that modal."""
        return (self.detail_entry is not None or self.session_detail is not None
                or self.db_info is not None or self.prompts.detail is not None)

    def prompt_available(self) -> bool:
        """True when the open command modal has a real prompt to drill into.
        The `_tokens_unavailable` sentinel is a dim hint, not a prompt, so Enter
        keeps its old close-the-modal meaning when there is nothing to open."""
        return bool(self.detail_prompt) and bool(_prompt_lines(self.detail_prompt))

    def close_modals(self) -> None:
        """Drop every modal's state at once, back to the table.

        One place to reset keeps the modals mutually exclusive: leaving any
        field set would render a stale panel over the feed."""
        self.detail_entry = None
        self.session_detail = None
        self.session_agent_id = None
        self.session_scroll = 0
        self.db_info = None
        self.detail_tokens = None
        self.session_tokens = None
        self.detail_prompt = None
        self.detail_prompt_open = False
        self.detail_prompt_scroll = 0
        self.prompts.close_detail()

    def selected_entry(self) -> LogEntry | None:
        """The entry the cursor points at in the newest-first view, or None."""
        if not self.entries or self.cursor < 0 or self.cursor >= len(self.entries):
            return None
        return list(reversed(self.entries))[self.cursor]

    def selected_row(self) -> object | None:
        """The row the cursor points at on the ACTIVE tab, or None when that tab
        has no rows: a `LogEntry` on commands, a `PromptTurn` on prompts. The
        session-scoped keys gate on this so they are inert on an empty list."""
        if self.tab == "prompts":
            return self.prompts.selected()
        return self.selected_entry()

    def selected_session_id(self) -> str | None:
        """The session the cursor points at, whichever tab is active.

        `s`, `o`, `t` need only a session id, so routing them through one
        accessor makes them work on both tabs. None when the tab has no row, or
        when the row carries no session id (a hand-edited/legacy log line), which
        leaves those keys inert rather than acting on a blank session."""
        if self.tab == "prompts":
            turn = self.prompts.selected()
            return (turn.session_id or None) if turn else None
        entry = self.selected_entry()
        return (entry.get("session_id") or None) if entry else None

    def cycle_tab(self, step: int = 1) -> None:
        """Switch to the next tab (`Tab`). Each tab's cursor/scroll live in
        separate fields, so a switch never disturbs the other view."""
        try:
            idx = TABS.index(self.tab)
        except ValueError:
            idx = 0
        self.tab = TABS[(idx + step) % len(TABS)]

    def visible_cols_for(self, tab: str | None = None) -> dict[int, bool]:
        """The `{column key: shown}` dict of `tab` (default: the active tab).

        Kept as one flat dict per tab so `build_table` / `build_panel` keep taking
        exactly what they took before - the active tab's dict."""
        if (tab or self.tab) == "prompts":
            return self.prompt_cols
        return self.visible_cols

    def open_session_detail(self, sid: str | None,
                            agent_id: str | None = None) -> Action:
        """Open the session modal for `sid`, returning the Action main() needs.

        Shared by both tabs' `s` key so the modal's fields are set in one place.
        A falsy `sid` (no row, or a row with no session) leaves the modal closed
        and returns NONE instead of showing a blank identity."""
        if not sid:
            return Action.NONE
        self.session_detail = sid
        self.session_agent_id = agent_id
        self.session_scroll = 0  # start each open at the top of the list
        return Action.OPEN_SESSION_DETAIL

    def move_up(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def move_down(self) -> None:
        self.cursor = min(max(0, len(self.entries) - 1), self.cursor + 1)

    def goto_top(self) -> None:
        self.cursor = 0
        self.scroll_offset = 0

    def goto_bottom(self) -> None:
        self.cursor = max(0, len(self.entries) - 1)

    def toggle_col(self, col: int, tab: str | None = None) -> None:
        """Flip a column's visibility on `tab` (default: the active tab), but
        never hide the last visible column. A key the tab has no column for is a
        no-op, which is what makes the digit keys inert on a tab with no
        registered columns."""
        cols = self.visible_cols_for(tab)
        if col not in cols:
            return
        if not cols[col] or sum(cols.values()) > 1:
            cols[col] = not cols[col]

    def ingest(self, new_lines: list[str]) -> int:
        """Parse and append new log lines; re-anchor the cursor if scrolled away.

        Returns the number of entries added. Trims to MAX_ENTRIES. Does NOT
        clamp: the caller clamps after ingest (preserving the original order).
        """
        new_count = 0
        for line in new_lines:
            entry = parse_line(line.strip())
            if entry:
                self.entries.append(entry)
                new_count += 1
        # If the user scrolled away from the top, keep them anchored to the entry
        # they were viewing as new entries shift the reversed view down.
        if new_count > 0 and self.cursor > 0:
            self.cursor += new_count
            self.scroll_offset += new_count
        if len(self.entries) > MAX_ENTRIES:
            self.entries = self.entries[-MAX_ENTRIES:]
        return new_count

    def clamp(self, max_rows: int) -> None:
        """Keep cursor in [0, len-1] and scroll_offset so the cursor is visible."""
        if not self.entries:
            self.cursor = 0
            self.scroll_offset = 0
            return
        self.cursor = max(0, min(self.cursor, len(self.entries) - 1))
        if self.cursor < self.scroll_offset:
            self.scroll_offset = self.cursor
        elif self.cursor >= self.scroll_offset + max_rows:
            self.scroll_offset = self.cursor - max_rows + 1
        max_offset = max(0, len(self.entries) - max_rows)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))


def _apply_search_key(state: AppState, search: SearchState, ch: str) -> Action:
    """Handle one keypress while the search modal is open (mutation only).

    Stays pure: it mutates the SearchState / closes the modal and returns an
    Action; main() runs the grep (RUN_SEARCH) and the editor / file-manager side
    effects (OPEN_MATCH / OPEN_MATCH_FOLDER). Any keypress clears a stale `flash`.
    """
    if ch == "\x03":
        return Action.QUIT
    search.flash = None
    if search.mode == "INPUT":
        if ch in ("\r", "\n"):
            # A blank query would no-op in run_search but still flip to RESULTS
            # and show "no matches"; stay in INPUT instead.
            if not search.query.strip():
                return Action.NONE
            return Action.RUN_SEARCH
        if ch == "\x1b":
            state.search = None
        elif ch == "\t":
            other = _other_root(search.root)
            if other in search.roots:
                search.root = other
            else:
                search.flash = f"{other} root unavailable"
        elif ch in ("\x7f", "\x08"):
            search.query = search.query[:-1]
        elif len(ch) == 1 and ch.isprintable():
            search.query += ch
        return Action.NONE
    # RESULTS mode: j/k browse, enter/f act, / edits, tab re-runs on the other root
    if ch in ("\x1b", "q"):
        state.search = None
    elif ch == "/":
        search.mode = "INPUT"
    elif ch == "\t":
        other = _other_root(search.root)
        if other in search.roots:
            search.root = other
            return Action.RUN_SEARCH
        search.flash = f"{other} root unavailable"
    elif ch in ("\r", "\n"):
        if search.results:
            return Action.OPEN_MATCH
    elif ch == "f":
        if search.results:
            return Action.OPEN_MATCH_FOLDER
    elif ch in ("j", "down"):
        if search.results:
            search.cursor = min(len(search.results) - 1, search.cursor + 1)
    elif ch in ("k", "up"):
        search.cursor = max(0, search.cursor - 1)
    return Action.NONE


def _apply_turn_key(state: AppState, prompts: PromptsState, ch: str) -> Action:
    """Handle one keypress while a Prompts-tab turn is open (mutation only).

    Two depths, the same contract the command modal's full-prompt view has: on
    the turn page `↵` drills into the whole prompt (when there is one to open),
    `esc` steps back to the list and `q` closes outright; inside the prompt view
    `esc`/`↵` step back ONE level to the turn and `q` still closes everything.
    Ctrl-C quits from either.

    `j`/`k`/`g`/`G` scroll whichever list is on top - the actions or the prompt
    lines - and `b` filters the actions to Bash. Both offsets are clamped for
    display in the builders and written back by `render_panel`, so `G`'s sentinel
    settles on the true bottom and `k` then steps up one row.
    """
    if prompts.prompt_open:
        if ch in ("\x1b", "\r", "\n"):
            prompts.prompt_open = False
            prompts.prompt_scroll = 0
        elif ch == "q":
            state.close_modals()
        elif ch in ("j", "down"):
            prompts.prompt_scroll += 1
        elif ch in ("k", "up"):
            prompts.prompt_scroll = max(0, prompts.prompt_scroll - 1)
        elif ch == "g":
            prompts.prompt_scroll = 0
        elif ch == "G":
            prompts.prompt_scroll = 10**9  # clamped in build_prompt_panel
        return Action.NONE
    if ch in ("\r", "\n") and prompts.detail_text:
        # Enter opens the full prompt when this turn HAS one; with nothing to
        # open it keeps the one-key exit every other modal gives it.
        prompts.prompt_open = True
        prompts.prompt_scroll = 0
        return Action.NONE
    if ch in ("\x1b", "q", "\r", "\n"):
        state.close_modals()
    elif ch in ("j", "down"):
        prompts.detail_scroll += 1
    elif ch in ("k", "up"):
        prompts.detail_scroll = max(0, prompts.detail_scroll - 1)
    elif ch == "g":
        prompts.detail_scroll = 0
    elif ch == "G":
        prompts.detail_scroll = 10**9  # clamped in build_turn_panel
    elif ch == "b":
        # Bash-only, filtered in memory (see `visible_actions`): no store read,
        # so the key is free to be held. The list just changed length, so start
        # the shorter one at the top rather than mid-way down a list that ended.
        prompts.bash_only = not prompts.bash_only
        prompts.detail_scroll = 0
    return Action.NONE


def _apply_modal_key(state: AppState, ch: str) -> Action:
    """Handle one keypress while a full-screen modal is open (mutation only).

    Modal view (command detail, session detail, database, prompts turn): esc / q
    / enter return to the table; Ctrl-C still quits. `close_modals()` clears
    every modal field, which keeps them mutually exclusive.

    Both tabs route here: `s` and `d` open the session / database modals from the
    prompts list too, so one handler answers wherever the modal was opened from
    (otherwise a modal opened on the prompts tab would be a trap, since that
    tab's branch runs before this one).

    The prompts turn has keys of its own (scroll, `b`, its own drill-in), so it
    is handed to `_apply_turn_key` before the shared rules below.
    """
    if ch == "\x03":
        return Action.QUIT
    if state.prompts.detail is not None:
        return _apply_turn_key(state, state.prompts, ch)
    if state.detail_prompt_open:
        # The full-prompt view (drilled into from the command modal) owns its
        # keys: esc / enter step back one level to the command, `q` still
        # closes everything in one key, and j/k/g/G scroll the prompt body.
        if ch in ("\x1b", "\r", "\n"):
            state.detail_prompt_open = False
            state.detail_prompt_scroll = 0
        elif ch == "q":
            state.close_modals()
        elif ch in ("j", "down"):
            state.detail_prompt_scroll += 1
        elif ch in ("k", "up"):
            state.detail_prompt_scroll = max(0, state.detail_prompt_scroll - 1)
        elif ch == "g":
            state.detail_prompt_scroll = 0
        elif ch == "G":
            state.detail_prompt_scroll = 10**9  # clamped in build_prompt_panel
        return Action.NONE
    if ch in ("\r", "\n") and state.detail_entry is not None and state.prompt_available():
        # Enter on a command modal that HAS a prompt opens it in full instead
        # of closing; with no prompt to open it keeps the old one-key exit.
        state.detail_prompt_open = True
        state.detail_prompt_scroll = 0
        return Action.NONE
    if ch in ("\x1b", "q", "\r", "\n"):
        state.close_modals()
        return Action.NONE
    # In the session modal only, the nav keys scroll the subagent list
    # instead of being swallowed. G's sentinel and every offset are clamped
    # for display in build_session_panel (and written back by render_panel).
    # The command-detail / database modals keep full-swallow behavior.
    if state.session_detail is not None:
        if ch in ("j", "down"):
            state.session_scroll += 1
        elif ch in ("k", "up"):
            state.session_scroll = max(0, state.session_scroll - 1)
        elif ch == "g":
            state.session_scroll = 0
        elif ch == "G":
            state.session_scroll = 10**9  # clamped in build_session_panel
    return Action.NONE


def _apply_prompts_key(state: AppState, prompts: PromptsState, ch: str,
                       max_rows: int) -> Action:
    """Handle one keypress while the Prompts tab is active (mutation only).

    Mirrors `_apply_search_key`: it mutates state and returns an Action for
    main() to perform, doing no I/O itself - the store reads all happen in main()
    under `Action.LOAD_PROMPTS`, so this stays pure and testable. An open modal is
    handed to the shared `_apply_modal_key`, since `s` and `d` open those panels
    from this tab too.

    `max_rows` is the body height, needed because every cursor move is paired with
    `prompts.clamp(max_rows)` (the same pairing the feed does with `AppState.clamp`).

    The feed-only keys are deliberately absent: `c` (clear) belongs to the log
    tail, and `/` (search) and `f` (folder) need a row's cwd and a command's file
    list, which a turn row does not carry.
    """
    if state.modal_open():
        return _apply_modal_key(state, ch)
    if ch in ("q", "\x03"):
        return Action.QUIT
    if ch == "\t":
        state.cycle_tab()
    elif ch in ("k", "up"):
        prompts.move_up()
        prompts.clamp(max_rows)
    elif ch in ("j", "down"):
        prompts.move_down()
        prompts.clamp(max_rows)
    elif ch == "g":
        prompts.goto_top()
        prompts.clamp(max_rows)
    elif ch == "G":
        prompts.goto_bottom()
        prompts.clamp(max_rows)
    elif ch == "r":
        # Force a re-read: the store is only as fresh as the last ingest, so this
        # is how the user pulls in a turn that finished since the snapshot.
        return Action.LOAD_PROMPTS
    elif ch == "x":
        # Hide/show the turns that ran no tool call. Filtered in place over the
        # loaded snapshot (see `PromptsState.visible_rows`), so no store read -
        # but the visible list just changed length, so re-anchor the cursor.
        prompts.hide_empty = not prompts.hide_empty
        prompts.clamp(max_rows)
    elif ch == "S":
        # Scope the list to the selected row's session, or back to every session.
        # Unlike `x` this DOES re-query: `prompt_turns` applies the session filter
        # inside its bounded raw scan, so a scoped list reaches back through that
        # session's own history instead of showing whichever of its turns made the
        # global newest-N cut. Start the scoped list at the top; it is a new list.
        if prompts.session_filter:
            prompts.session_filter = None
        else:
            sid = state.selected_session_id()
            if not sid:
                return Action.NONE
            prompts.session_filter = sid
        prompts.cursor = 0
        prompts.scroll_offset = 0
        return Action.LOAD_PROMPTS
    elif ch in ("\r", "\n"):
        # Open the turn's detail page; main() loads its actions and full prompt
        # (OPEN_PROMPT) so this stays pure and never touches duckdb itself. Once
        # `detail` is set `modal_open()` swallows the rest of a held Enter, so a
        # key-repeat burst costs exactly one store read.
        turn = prompts.selected()
        if turn is None:
            return Action.NONE
        prompts.close_detail()   # a fresh page: no scroll, no filter, no stale text
        prompts.detail = turn
        return Action.OPEN_PROMPT
    elif ch.isdigit():
        # toggle_col resolves this tab's own column dict and ignores a key it has
        # no column for, so no digit here can hide a column of the feed.
        state.toggle_col(int(ch))
    elif ch == "s":
        # A turn row is a main-thread turn, so there is no subagent to spotlight.
        return state.open_session_detail(state.selected_session_id())
    elif ch in ("o", "t"):
        # Session-scoped: main() resolves the id with selected_session_id(), so
        # both tabs share one path. Inert when there is no row to act on.
        if state.selected_row() is not None:
            return Action.OPEN_SESSION if ch == "o" else Action.OPEN_TRANSCRIPT
    elif ch == "d":
        # The database modal describes the store, not a session, so it opens from
        # either tab with or without a selected row (see the commands branch).
        return Action.LOAD_DB_INFO
    return Action.NONE


def _tab_row_count(tab: str, term_height: int) -> int:
    """The body height of `tab`'s list: what its cursor clamps against.

    The two tabs have different chrome, so `apply_key`'s `max_rows` is whichever
    one is active (`main()` resolves it through here).
    """
    if tab == "prompts":
        return prompts_row_count(term_height)
    return visible_row_count(term_height)


def apply_key(state: AppState, ch: str, max_rows: int) -> Action:
    """Apply one keypress to `state`, returning the Action main() must perform.

    Mutates `state` but spawns no processes and touches no terminal.

    `max_rows` is the ACTIVE tab's body height (see `_tab_row_count`), used to
    keep that tab's cursor and scroll offset in range.

    Dispatch order: the search modal (it owns every key, including `Tab`, while
    open), then the active tab's handler, then - on the Commands tab - any open
    modal, then the feed's own keys.
    """
    if state.search is not None:
        # The search modal is mutually exclusive with the command/session modals
        # and takes precedence: it owns every key while open.
        return _apply_search_key(state, state.search, ch)
    if state.tab == "prompts":
        return _apply_prompts_key(state, state.prompts, ch, max_rows)
    if state.modal_open():
        return _apply_modal_key(state, ch)
    if ch in ("q", "\x03"):
        return Action.QUIT
    if ch == "c":
        state.entries.clear()
        state.cursor = 0
        state.scroll_offset = 0
    elif ch in ("k", "up"):
        state.move_up()
        state.clamp(max_rows)
    elif ch in ("j", "down"):
        state.move_down()
        state.clamp(max_rows)
    elif ch == "g":
        state.goto_top()
    elif ch == "G":
        state.goto_bottom()
        state.clamp(max_rows)
    elif ch == "\t":
        state.cycle_tab()
        if state.tab == "prompts":
            # Entering the tab reads the store right away, so the list is there
            # on the first frame instead of waiting for the idle auto-refresh.
            return Action.LOAD_PROMPTS
    elif ch.isdigit() and int(ch) in state.visible_cols:
        state.toggle_col(int(ch))
    elif ch in ("\r", "\n"):
        # Open the command-detail modal; main() loads its token cost and prompt
        # (OPEN_DETAIL) so apply_key stays pure and never touches duckdb itself.
        state.detail_entry = state.selected_entry()
        return Action.OPEN_DETAIL
    elif ch == "s":
        # Open the session-detail modal for the selected row's session. Opening
        # it precludes the command modal (only one modal open at a time). An
        # empty/missing session_id (hand-edited or legacy line) leaves the modal
        # closed rather than showing a blank identity.
        entry = state.selected_entry()
        sid = state.selected_session_id()
        # Spotlight the row's subagent on the session page (None for a main-agent
        # row, so it renders exactly as before); only when the modal opens.
        return state.open_session_detail(
            sid, entry_agent_id(entry) if (sid and entry) else None)
    elif ch == "/":
        # Open the per-session search modal for the selected row's session,
        # resolving its transcript-folder and cwd roots up front (S.1). Unlike
        # the `s` handler (which defers all disk reads to render time), this
        # reads pid.json/meta here; the reads are small and cached (2s TTL via
        # load_session_model), so the one-shot cost on the keypress is fine.
        entry = state.selected_entry()
        if entry is not None:
            sid = entry.get("session_id") or ""
            model = load_session_model(sid) if sid else None
            roots = search_roots(sid, entry, model)
            default_root = "transcript" if "transcript" in roots else (
                "cwd" if "cwd" in roots else "transcript")
            state.search = SearchState(sid=sid, roots=roots, root=default_root)
    elif ch in ("o", "t"):
        # Session-scoped: main() resolves the id with selected_session_id(), the
        # same path the prompts tab takes. Inert with no row to act on.
        if state.selected_row() is not None:
            return Action.OPEN_SESSION if ch == "o" else Action.OPEN_TRANSCRIPT
    elif ch == "f":
        return Action.OPEN_FILES
    elif ch == "d":
        # Open the audit-store database modal. The store (and duckdb) is imported
        # lazily by main() when this action fires, so the normal feed never loads
        # duckdb; apply_key stays pure and does no I/O here. It describes the
        # store rather than a session, so it needs no selected row.
        return Action.LOAD_DB_INFO
    return Action.NONE


# ==== Main ====


def _load_db_info() -> list:
    """Load the audit-store properties for the database modal.

    Lazily imports the store (and thus duckdb) only when the `d` modal is opened,
    so the normal feed and the hook never load duckdb. Never raises: a missing
    store, a missing duckdb, or any query error renders as a single status row.
    """
    try:
        from claude_trail_store import STORE_PATH, connect, db_info
    except Exception as e:  # duckdb/module not importable
        return [("database", f"unavailable: {e}")]
    if not STORE_PATH.exists():
        return [("connection", str(STORE_PATH)),
                ("status", "no store yet; run: claude-trail ingest")]
    try:
        con = connect(read_only=True)
        try:
            return db_info(con)
        finally:
            con.close()
    except Exception as e:
        return [("connection", str(STORE_PATH)), ("error", str(e))]


def _with_store(fn, on_error=None):
    """Open the audit store read-only, call `fn(con)`, and return its result.

    Shared plumbing for every on-demand store read (the command/session modals'
    Tokens rows and prompt panel, the prompts list): the store (and duckdb) is
    imported lazily on the keypress, so the normal feed and the hook never load
    duckdb. The connection is closed immediately - DuckDB's file lock excludes in
    BOTH directions, so a reader held open would block the ingest (CLAUDE.md,
    "Why not `ingest --watch`").

    Returns None on any failure (duckdb/module not importable, no store file yet,
    or a query error) so a caller can simply omit the Tokens row rather than
    crash the TUI. `on_error(exc)` is called with the exception first, for the one
    caller that must tell a transient lock collision apart from an empty answer;
    it does not widen what is caught, it only stops the reason being lost. A hook
    that raises is swallowed too: the contract here is "never raise at the caller",
    and a diagnostic callback must not be the one thing that breaks it.
    """
    try:
        from claude_trail_store import STORE_PATH, connect
    except Exception:
        return None
    if not STORE_PATH.exists():
        return None
    try:
        con = connect(read_only=True)
        try:
            return fn(con)
        finally:
            con.close()
    except Exception as e:
        if on_error is not None:
            try:
                on_error(e)
            except Exception:
                pass
        return None


def _tokens_unavailable() -> dict:
    """Sentinel value for when the store has no answer (shared by the Tokens
    rows and the prompt panel): says WHY the data is missing instead of silently
    omitting it, which reads as a broken feature (the store tails transcripts and
    lags the live feed, so "no data yet" is the normal state right after a
    command runs)."""
    try:
        from claude_trail_store import STORE_PATH
        missing = not STORE_PATH.exists()
    except Exception:
        missing = True
    return {"unavailable": NO_STORE_HINT if missing else PENDING_INGEST_HINT}


def _load_command_detail(entry: LogEntry | None) -> tuple[dict | None, dict | None]:
    """`(tokens, prompt)` for `entry`'s command, in ONE read-only store open.

    Loaded once when the command-detail modal opens (see Action.OPEN_DETAIL);
    both rows come from the same connection so opening the modal costs a single
    store open rather than one per row. Each degrades independently to the
    `_tokens_unavailable` sentinel (rendered as a dim hint) when the store has no
    answer; both are None only for a row with no session/command, which omits the
    Tokens row and the prompt panel entirely."""
    if not entry:
        return None, None
    sid = entry.get("session_id") or ""
    cmd = entry.get("command") or ""
    if not sid or not cmd:
        return None, None

    def query(con):
        from claude_trail_store import command_tokens_for, prompt_for_command
        return (
            command_tokens_for(con, sid, cmd, entry.get("timestamp")),
            prompt_for_command(con, sid, entry.get("timestamp"), entry_agent_id(entry)),
        )

    tokens, prompt = _with_store(query) or (None, None)
    return (tokens or _tokens_unavailable(), prompt or _tokens_unavailable())


def _load_session_tokens(sid: str | None) -> dict | None:
    """Per-session token totals from the audit store. Loaded once when the
    session-detail modal opens (see Action.OPEN_SESSION_DETAIL). Returns the
    `_tokens_unavailable` sentinel when the store has no ingested requests for
    the session, and None only when there is no session id."""
    if not sid:
        return None

    def query(con):
        from claude_trail_store import session_token_totals
        return session_token_totals(con, sid)

    return _with_store(query) or _tokens_unavailable()


def _load_prompt_turns(
    session_id: str | None = None,
) -> tuple[list[PromptTurn] | None, str | None]:
    """`(rows, status)` for the Prompts tab, from ONE read-only store open.

    Loaded when Action.LOAD_PROMPTS fires (tab entry, `r`, a session-filter
    toggle, the idle auto-refresh), so `apply_key` stays pure and the store is
    opened, read and closed inside a single call - never held (see `_with_store`).

    Three outcomes, each distinguishable by the caller, because a tab that showed
    the same blank list for all three would look broken:

    - a successful read: `(rows, None)`, possibly an empty list when the store
      genuinely holds no turn for this scope;
    - no store file (or no duckdb): `([], NO_STORE_HINT)` - there is nothing to
      show and the hint says how to create it;
    - the store is locked by the running ingest: `(None, STORE_BUSY_HINT)`. `rows`
      is None to mean "no answer, keep what is on screen": DuckDB's lock excludes
      in both directions, so at the 30s ingest cadence this is routine, and
      blanking a good snapshot for it would be worse than showing a stale one.
    """
    try:
        from claude_trail_store import STORE_PATH, is_lock_conflict
    except Exception:
        return [], NO_STORE_HINT      # no duckdb / no store module: same dead end
    if not STORE_PATH.exists():
        return [], NO_STORE_HINT

    def query(con):
        from claude_trail_store import prompt_turns
        rows = prompt_turns(con, session_id=session_id) if session_id else prompt_turns(con)
        return [PromptTurn.from_row(row) for row in rows]

    # _with_store swallows the exception into None; collect it here so a lock
    # collision can be told from an empty read without widening that catch.
    failure: list = []
    rows = _with_store(query, on_error=failure.append)
    if rows is None:
        exc = failure[0] if failure else None
        if exc is not None and is_lock_conflict(exc):
            return None, STORE_BUSY_HINT
        # Anything else (a vanished file, an unreadable store) is a real failure
        # and says so rather than posing as an empty list of prompts.
        return [], (f"store unreadable: {exc}" if exc else NO_STORE_HINT)
    return rows, None


def _refresh_prompts(prompts: PromptsState, max_rows: int) -> None:
    """Re-read the Prompts tab's snapshot in place (the LOAD_PROMPTS action).

    Kept out of main()'s loop so the three store outcomes land in one testable
    place. `loaded_at` records when the tab last got an ANSWER from the store, so
    NO failure advances it (a busy collision or an unreadable store leaves it, and
    the rows, alone): the header keeps counting up from the last good read, which
    is exactly the staleness the user needs to see. Advancing it for a read that
    answered nothing would date an empty list as fresh - `as of 22:30:04 (0s ago)
    · 0 turns` over a body explaining that the store could not be read.
    """
    rows, status = _load_prompt_turns(prompts.session_filter)
    prompts.checked_at = time.monotonic()
    if rows is None:
        # The store is held by the running ingest: no answer, so keep the rows.
        prompts.status = STORE_BUSY_KEPT_HINT if prompts.rows else status
        return
    prompts.status = status
    if status:
        # A failure that answered nothing (missing store, unreadable store):
        # `_load_prompt_turns` pairs those with an empty list, so writing it here
        # would blank a good snapshot for what may be a transient error - the
        # same snapshot a lock collision above deliberately keeps.
        return
    prompts.rows = rows
    prompts.loaded_at = prompts.checked_at
    prompts.clamp(max_rows)


def _cap_prompt_text(text: str | None, cap: int = PROMPT_TEXT_CHAR_CAP) -> str | None:
    """`text`, truncated to `cap` chars, led by a line saying what was cut.

    `turn_prompt_text` has no cap of its own and the longest main-thread turn
    measured in a real store is 646,245 chars, so this is where the TUI decides
    how much it is willing to hold and re-split every frame. The cut is stated in
    the text rather than made silently: a prompt that just stops mid-sentence
    reads like data loss.

    The notice goes FIRST, and that placement is the whole point. It used to be
    appended, which is the one line `build_prompt_panel` cannot show: the view
    windows by LOGICAL line while the Panel crops by DISPLAY row and prompt lines
    wrap, so the last window renders fewer lines than it holds and a trailing
    notice is the first casualty - measured absent at (h,w) of 24x80, 24x120,
    40x80, 40x120 and 50x100, worse on a taller terminal, and unreachable by any
    key because `_clamp_prompt_scroll` stops the offset at the last screenful.
    418 of the 3,716 turns the list can reach (11.2%) are over the cap, so that
    was a silent cut on better than one turn in ten. The crop eats the BOTTOM of
    the window, so the first line is the one row it can never take, and the view
    opens at scroll 0 (`_apply_turn_key` sets it), which puts the notice on the
    first frame at every geometry down to MIN_VISIBLE_ROWS.
    """
    if not text or len(text) <= cap:
        return text
    dropped = len(text) - cap
    return (f"… showing the first {cap:,} of {len(text):,} characters "
            f"({dropped:,} not shown)\n\n"
            + text[:cap])


def _load_turn_detail(
    turn: PromptTurn | None,
) -> tuple[list[TurnAction] | None, str | None]:
    """`(actions, prompt_text)` for one turn, from ONE read-only store open.

    Loaded when Action.OPEN_PROMPT fires (`↵` on a turn row), so `apply_key`
    stays pure and both halves of the page come from a single connect/query/close
    - the same one-open-per-modal rule `_load_command_detail` follows.

    `actions` is None when the store could not answer at all (missing, busy,
    unreadable) and `[]` when the turn genuinely ran no tool call; the page says
    those differently, so the distinction has to survive this far. `prompt_text`
    is capped (see `_cap_prompt_text`) before it ever reaches the view.
    """
    if turn is None or not turn.session_id or not turn.ts:
        return None, None

    def query(con):
        from claude_trail_store import turn_actions, turn_prompt_text
        rows = turn_actions(con, turn.session_id, turn.ts, turn.next_ts)
        text = turn_prompt_text(con, turn.event_id) if turn.event_id else None
        return [TurnAction.from_row(row) for row in rows], text

    actions, text = _with_store(query) or (None, None)
    return actions, _cap_prompt_text(text)


def _prompts_refresh_due(prompts: PromptsState, now: float | None = None) -> bool:
    """True when the Prompts tab should read the store again: never tried, or the
    last attempt was more than PROMPTS_AUTO_REFRESH ago.

    Checked once per idle frame while that tab is on screen, which is what keeps a
    live session's newest turn appearing without the user pressing `r`. It gates on
    the last ATTEMPT, not the last answer, so a store held by the ingest is retried
    on the same slow cadence rather than on every 300ms poll.
    """
    age = _snapshot_age(prompts.checked_at, now)
    return age is None or age >= PROMPTS_AUTO_REFRESH


@dataclass
class DrainFlags:
    """What one drained burst of keys asks the frame to do ONCE, after the loop.

    main() drains every buffered keystroke before rendering, so holding a key is
    one render instead of one per press. An action whose work is a store open has
    to be coalesced the same way, or a key-repeat burst spends a query per press
    inside a single frozen drain.
    """

    reload_prompts: bool = False   # re-read the tab's turn list (`r`, `S`, tab entry)
    reload_turn: bool = False      # re-read the open turn's actions + full prompt


def _drain_action(action: Action, flags: DrainFlags) -> bool:
    """Fold one drained action into `flags`; True when it was COALESCED.

    True means main() must not perform the action inline - the work happens once
    per frame in `_frame_store_reads`. False means the drain performs it itself,
    which is right for the rest: an editor launch or a search is what the user
    asked for on that press, and `d`/`s` open a modal that swallows the repeat,
    so they are self-limiting already.

    The two coalesced ones are the two that are neither.  `LOAD_PROMPTS` reads
    the list (~275ms; 10 buffered `r` once measured 3.21s frozen inside one
    drain, with no redraw).  `OPEN_PROMPT` reads one turn (~44ms) and is NOT
    self-limiting: a turn whose prompt text has not loaded gives `↵` back its
    close-the-page meaning, so a repeat alternates open/close and would ask for a
    fresh read every other press.
    """
    if action is Action.LOAD_PROMPTS:
        flags.reload_prompts = True
        return True
    if action is Action.OPEN_PROMPT:
        flags.reload_turn = True
        return True
    return False


def _frame_store_reads(state: AppState, flags: DrainFlags, list_rows: int) -> None:
    """Perform the store reads this frame owes: at most one of each.

    The other half of the split `_drain_action` starts, kept out of main()'s loop
    so both halves are testable - a burst of ten `↵` ends in exactly one
    `_load_turn_detail`, and nothing here can be reached from the drain's inline
    branches.

    The prompts snapshot is either an explicit request coalesced out of the drain
    (`r`, `S`, entering the tab) or the idle auto-refresh. `_load_prompt_turns`
    connects, queries and closes on every call (there is no warm-connection
    path), measured ~270-310ms steady state and ~600-660ms cold on a 1.08 GiB
    store. Still synchronous, which the codebase prefers over a background thread
    (state stays pure and main() keeps every side effect); PROMPTS_AUTO_REFRESH is
    the ingest cadence, so the idle case fires rarely enough that the stall is not
    felt. Both are gated on the tab being the one on screen, so a drain that
    entered and left it again pays nothing. Only the idle case is modal-gated: an
    explicit request stands even if a later key in the same drain opened a modal
    over the list (one read, unseen until it closes), while a modal can never make
    the tab poll the store on its own.

    The open turn's page follows the same one-read-per-frame rule, for whatever
    turn the drain ENDED on: a burst that walked through several pays for the
    last one only, and a drain that closed the page again loads nothing
    (`_load_turn_detail(None)` is a no-op returning `None, None`).
    """
    if state.tab == "prompts" and (
            flags.reload_prompts
            or (not state.modal_open() and _prompts_refresh_due(state.prompts))):
        _refresh_prompts(state.prompts, list_rows)
    if flags.reload_turn:
        (state.prompts.actions,
         state.prompts.detail_text) = _load_turn_detail(state.prompts.detail)


def _signal_exit(signum, frame):
    """Fatal-signal handler: raise SystemExit so main()'s finally restores the tty.

    SystemExit is a BaseException, so the loop's `except KeyboardInterrupt`
    does not swallow it and it propagates to the terminal-restoring finally.
    """
    raise SystemExit(128 + signum)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "hook":
        sys.exit(hook_main())
    if argv and argv[0] in ("ingest", "search", "cat", "diff", "stats", "tokens",
                            "prompts", "reset-db", "prune-raw"):
        from claude_trail_store import cli_main  # lazy: only loads duckdb on demand
        sys.exit(cli_main(argv))

    console = Console()

    entries, last_pos = read_last_entries(LOG_PATH, MAX_ENTRIES)
    state = AppState(entries=entries)
    name_map = load_session_names()
    color_map = load_session_colors({e.get("session_id", "") for e in state.entries})
    agent_map = load_agent_labels({e.get("session_id", "") for e in state.entries})

    interactive = sys.stdin.isatty()
    if interactive:
        fd = sys.stdin.fileno()
        # Without these, a SIGTERM (kill/pkill from another pane), SIGHUP
        # (kill -HUP), or SIGQUIT (Ctrl-\, which setcbreak leaves enabled)
        # would terminate the process without unwinding, leaving the terminal
        # in no-echo cbreak mode. Raising SystemExit lets the finally below
        # restore it. Registered before setcbreak so there is no window where
        # the tty is already altered but the handlers are not yet in place.
        for _sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(_sig, _signal_exit)
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    def render_panel() -> Panel:
        if state.search is not None:
            return build_search_panel(state.search, console.height)
        if state.session_detail is not None:
            model = load_session_model(state.session_detail)
            # Write the clamped scroll back so `G`'s sentinel settles on the true
            # bottom and `k` then steps up one row (build_session_panel also
            # clamps for display; keeping state in range makes j/k deterministic).
            state.session_scroll = _clamp_session_scroll(
                state.session_scroll, model, console.height
            )
            return build_session_panel(
                model, name_map, color_map, console.height,
                tokens=state.session_tokens,
                highlight_agent_id=state.session_agent_id, agent_map=agent_map,
                scroll=state.session_scroll,
            )
        if state.detail_entry is not None:
            if state.detail_prompt_open and state.detail_prompt is not None:
                # Write the clamped scroll back so `G`'s sentinel settles on the
                # true bottom and `k` then steps up one line (same contract as
                # the session modal's scroll).
                state.detail_prompt_scroll = _clamp_prompt_scroll(
                    state.detail_prompt_scroll,
                    len(_prompt_lines(state.detail_prompt)),
                    console.height,
                )
                return build_prompt_panel(
                    state.detail_entry.get("session_id", ""), console.height,
                    (state.detail_prompt or {}).get("text"),
                    ts=(state.detail_prompt or {}).get("ts") or "",
                    agent_id=entry_agent_id(state.detail_entry),
                    name_map=name_map, color_map=color_map, agent_map=agent_map,
                    scroll=state.detail_prompt_scroll,
                )
            return build_detail_panel(
                state.detail_entry, console.height, name_map, color_map, agent_map,
                tokens=state.detail_tokens, prompt=state.detail_prompt,
            )
        if state.db_info is not None:
            return build_db_panel(state.db_info, console.height)
        if state.prompts.detail is not None:
            turn = state.prompts.detail
            if state.prompts.prompt_open:
                # Same clamp-and-write-back contract as the command modal's
                # full-prompt view, over this turn's own text.
                state.prompts.prompt_scroll = _clamp_prompt_scroll(
                    state.prompts.prompt_scroll,
                    len(_text_lines(state.prompts.detail_text)),
                    console.height,
                )
                return build_prompt_panel(
                    turn.session_id, console.height, state.prompts.detail_text,
                    ts=turn.ts, name_map=name_map, color_map=color_map,
                    agent_map=agent_map, scroll=state.prompts.prompt_scroll,
                )
            # The same prompt-box height build_turn_panel will measure, so the
            # written-back offset matches the rows it draws.
            state.prompts.detail_scroll = _clamp_turn_scroll(
                state.prompts.detail_scroll,
                len(state.prompts.visible_actions()),
                console.height,
                _prompt_box_rows(_turn_prompt_lines(state.prompts)),
            )
            return build_turn_panel(
                state.prompts, console.height, name_map, color_map, agent_map)
        if state.tab == "prompts":
            return build_prompts_panel(
                state.prompts, console.height, state.visible_cols_for("prompts"),
                name_map, color_map,
            )
        return build_panel(
            state.entries, console.height, state.visible_cols,
            state.cursor, state.scroll_offset, name_map, color_map, agent_map,
        )

    try:
        with Live(
            render_panel(),
            console=console,
            refresh_per_second=4,
        ) as live:

            def open_path_in_editor(path: str) -> None:
                # Open `path` in $VISUAL/$EDITOR, else the GUI launcher. Shared by
                # `o` (filtered command log) and `t` (session transcript). No
                # `+<line>` arg here; only open_match jumps to a line.
                editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
                if editor and interactive:
                    # Terminal editor (nvim, vim, ...): suspend the live display
                    # and hand the tty over, then restore. Launching it detached
                    # would fight the TUI for the terminal and corrupt the screen.
                    live.stop()
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        subprocess.call(shlex.split(editor) + [path])
                    except (OSError, ValueError):
                        pass
                    tty.setcbreak(fd)
                    live.start(refresh=True)
                else:
                    # No editor configured: hand off to the GUI file launcher.
                    try:
                        subprocess.Popen(
                            [_platform_opener(), path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                    except OSError:
                        pass

            def open_session_log(sid: str) -> None:
                # Takes a session id, not a row: both tabs resolve one through
                # AppState.selected_session_id(), so `o` works on either.
                out_path = filter_session_log(sid)
                if out_path:
                    open_path_in_editor(out_path)

            def open_match(path: str, line: int) -> None:
                # Open a search hit at its line, reusing open_session_log's
                # suspend-Live / restore-tty pattern for terminal editors and its
                # detached GUI-launcher fallback. vi-family editors get `+<line>`.
                editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
                if editor and interactive:
                    try:
                        argv = shlex.split(editor)
                    except ValueError:
                        argv = []
                    if argv:
                        if os.path.basename(argv[0]) in _VI_FAMILY:
                            argv.append(f"+{line}")
                        argv.append(path)
                        live.stop()
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                        try:
                            subprocess.call(argv)
                        except (OSError, ValueError):
                            pass
                        tty.setcbreak(fd)
                        live.start(refresh=True)
                        return
                try:
                    subprocess.Popen(
                        [_platform_opener(), path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except OSError:
                    pass

            try:
                while True:
                    # The active tab's body height: each tab's cursor clamps
                    # against its own list, and the two panels differ by a row.
                    max_rows = _tab_row_count(state.tab, console.height)
                    quit_now = False
                    acted = False
                    # Set by LOAD_PROMPTS / OPEN_PROMPT in the drain below
                    # (`_drain_action`), each performed once after it.
                    flags = DrainFlags()
                    if interactive:
                        ready, _, _ = select.select([fd], [], [], POLL_INTERVAL)
                        # Drain every buffered keystroke before rendering, so
                        # holding or fast-typing a key doesn't render once per
                        # key (which feels laggy).
                        while ready:
                            ch = read_key(fd)
                            if not ch:  # EOF / read error - stop draining
                                break
                            acted = True
                            action = apply_key(state, ch, max_rows)
                            if action is Action.QUIT:
                                quit_now = True
                                break
                            if _drain_action(action, flags):
                                # A store read: only FLAGGED here, performed once
                                # per frame below, however many keys asked for it.
                                pass
                            elif action is Action.OPEN_SESSION:
                                sid = state.selected_session_id()
                                if sid:
                                    open_session_log(sid)
                            elif action is Action.OPEN_TRANSCRIPT:
                                sid = state.selected_session_id()
                                if sid:
                                    path = format_session_transcript(sid)
                                    if path:
                                        open_path_in_editor(path)
                            elif action is Action.OPEN_FILES:
                                entry = state.selected_entry()
                                if entry:
                                    open_file_folder(entry)
                            elif action is Action.RUN_SEARCH:
                                s = state.search
                                if s is not None:
                                    results, capped, err = run_search(
                                        s.query, s.roots.get(s.root, []))
                                    s.results, s.capped, s.error = results, capped, err
                                    s.mode, s.cursor = "RESULTS", 0
                            elif action is Action.OPEN_MATCH:
                                s = state.search
                                if s is not None and s.results:
                                    path, line, _ = s.results[s.cursor]
                                    open_match(path, line)
                            elif action is Action.OPEN_MATCH_FOLDER:
                                s = state.search
                                if s is not None and s.results:
                                    folder = open_containing_folder(
                                        s.results[s.cursor][0])
                                    s.flash = f"opened folder: {folder}"
                            elif action is Action.LOAD_DB_INFO:
                                state.db_info = _load_db_info()
                            elif action is Action.OPEN_DETAIL:
                                state.detail_tokens, state.detail_prompt = (
                                    _load_command_detail(state.detail_entry))
                            elif action is Action.OPEN_SESSION_DETAIL:
                                state.session_tokens = _load_session_tokens(
                                    state.session_detail)
                            ready, _, _ = select.select([fd], [], [], 0)
                        if quit_now:
                            break
                    else:
                        time.sleep(POLL_INTERVAL)

                    new_lines, last_pos = tail_file(LOG_PATH, last_pos)
                    new_count = state.ingest(new_lines)
                    # Re-read the height so a resize during the poll wait lands in
                    # this frame's clamp (the old per-clamp code read it live).
                    max_rows = visible_row_count(console.height)
                    state.clamp(max_rows)
                    prompt_rows = prompts_row_count(console.height)
                    state.prompts.clamp(prompt_rows)

                    # Every store read this frame owes, at most one of each: what
                    # the drain flagged, plus the prompts tab's idle refresh.
                    _frame_store_reads(state, flags, prompt_rows)

                    name_map = load_session_names()
                    session_ids = {e.get("session_id", "") for e in state.entries}
                    # An open turn's session is usually NOT in the feed (the
                    # prompts list interleaves every session in the store, the
                    # feed only those that ran a Bash command), so add it while
                    # its page is up: without it the action gutter would have no
                    # descriptions for that session's subagents. Both loaders are
                    # cached and accumulate, so this costs one directory scan per
                    # cache window and the labels stay resolvable afterwards.
                    if state.prompts.detail is not None:
                        session_ids.add(state.prompts.detail.session_id)
                    color_map = load_session_colors(session_ids)
                    agent_map = load_agent_labels(session_ids)

                    # Redraw immediately on input or new log lines so cursor
                    # moves feel instant; let the auto-refresh thread handle the
                    # idle clock/active-count ticks.
                    live.update(render_panel(), refresh=acted or new_count > 0)
            except KeyboardInterrupt:
                pass
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
