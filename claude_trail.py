#!/usr/bin/env python3
"""claude-trail: realtime TUI showing all Bash commands Claude Code executes."""

import enum
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

try:
    __version__ = pkg_version("claude-trail")
except PackageNotFoundError:  # running from a clone without an install
    __version__ = ""

LOG_PATH = Path.home() / ".claude" / "command-log.jsonl"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
MAX_ENTRIES = 1000
POLL_INTERVAL = 0.3
CHROME_ROWS = 10  # title, status, padding
MIN_VISIBLE_ROWS = 3
SESSION_NAME_CACHE_TTL = 2.0  # seconds
SESSION_COLOR_CACHE_TTL = 5.0  # seconds

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


@dataclass(frozen=True, eq=False)
class RenderCtx:
    """Per-render state a column cell may need.

    Bundles the session name/color maps so a column's `render` callable has one
    argument for everything table-wide. The module-level helper functions
    (`format_time`, `session_label`, `rich_color`, ...) are called directly.
    """

    name_map: dict[str, str] | None = None
    color_map: dict[str, str] | None = None


@dataclass(frozen=True, eq=False)
class Column:
    """One table column.

    `key` is the stable digit (1..5) the toggle keys / status bar use. `style`
    and `kwargs` are passed to `Table.add_column`. `render(entry, ctx)` produces
    that column's cell for a single log entry as a `str` or `rich.text.Text`.
    Adding a column is a single entry in the `COLUMNS` list below.
    """

    key: int
    name: str
    style: str
    kwargs: dict
    render: Callable[[dict, RenderCtx], object]


def _danger_prefixed(cmd: str, body: str) -> Text:
    """A `Text` of `body`, prefixed with a bold-red `* ` when `cmd` is dangerous."""
    text = Text()
    if is_dangerous(cmd):
        text.append("* ", style="bold red")
    text.append(body)
    return text


def _render_time(entry: dict, ctx: RenderCtx):
    return format_time(entry.get("timestamp", ""))


def _render_session(entry: dict, ctx: RenderCtx):
    sid = entry.get("session_id", "")
    label = session_label(sid, ctx.name_map)
    color = rich_color(ctx.color_map.get(sid)) if ctx.color_map else None
    return Text(label, style=color) if color else label


def _render_directory(entry: dict, ctx: RenderCtx):
    return short_path(entry.get("cwd", ""))


def _render_files(entry: dict, ctx: RenderCtx):
    return extract_files(entry.get("command", ""))


def _render_command(entry: dict, ctx: RenderCtx):
    cmd = entry.get("command", "")
    return _danger_prefixed(cmd, normalize_cmd(cmd))


COLUMNS = [
    Column(1, "Time", "cyan", {"width": 8, "no_wrap": True}, _render_time),
    Column(2, "Session", "magenta",
           {"width": 20, "no_wrap": True, "overflow": "ellipsis"}, _render_session),
    Column(3, "Directory", "green", {"width": 14, "no_wrap": True}, _render_directory),
    Column(4, "Files", "yellow",
           {"width": 20, "no_wrap": True, "overflow": "ellipsis"}, _render_files),
    Column(5, "Command", "white",
           {"ratio": 1, "no_wrap": True, "overflow": "ellipsis"}, _render_command),
]

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


def is_dangerous(cmd: str) -> bool:
    if DANGEROUS_PATTERNS.search(cmd):
        return True
    # Strip quoted spans first so prose like `git commit -m "logs > /var/log"`
    # is not mistaken for a redirect; an unquoted `> /path` still matches.
    return bool(DANGEROUS_REDIRECT.search(QUOTED_SPAN_RE.sub(" ", cmd)))


def extract_files(cmd: str) -> str:
    """Extract file paths referenced in a command."""
    paths = FILE_PATH_RE.findall(cmd)
    if not paths:
        return ""
    seen = set()
    unique = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    short = [os.path.basename(p.rstrip("/")) or p for p in unique[:3]]
    result = ", ".join(short)
    if len(unique) > 3:
        result += f" +{len(unique) - 3}"
    return result


def short_path(cwd: str) -> str:
    """Show .../last_dir, truncated to 10 chars."""
    name = os.path.basename(cwd) or cwd
    if len(name) > 10:
        return ".../" + name[:10]
    return ".../" + name


_session_name_cache: dict[str, str] = {}
_session_name_cache_ts: float = 0.0


def load_session_names() -> dict[str, str]:
    """Map session_id -> name from ~/.claude/sessions/*.json.

    Claude Code writes one JSON file per running session there (named after pid),
    each carrying a `sessionId` and an optional `name`. The file is removed when
    the session exits, so we accumulate known names across calls — once a name has
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
            # file got truncated/rewritten — re-scan from the start
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


def _platform_opener() -> str:
    """Return the platform-default file launcher (`open` on macOS, `xdg-open` elsewhere)."""
    if sys.platform == "darwin":
        return "open"
    return "xdg-open"


def normalize_cmd(cmd: str) -> str:
    """Collapse whitespace in command string."""
    return " ".join(cmd.split())


def parse_line(line: str) -> dict | None:
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


def format_time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # render in the user's local timezone
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "??:??:??"


def get_display_entries(
    entries: list[dict], max_rows: int, offset: int = 0
) -> list[dict]:
    """Return up to max_rows entries in newest-first order, skipping the first `offset`."""
    if not entries:
        return []
    reversed_view = list(reversed(entries))
    return reversed_view[offset : offset + max_rows]


def build_table(
    entries: list[dict],
    max_rows: int,
    visible_cols: dict[int, bool],
    cursor: int,
    offset: int = 0,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
) -> Table:
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_edge=False,
        pad_edge=False,
        row_styles=["", "dim"],
    )

    ctx = RenderCtx(name_map=name_map, color_map=color_map)
    active = [col for col in COLUMNS if visible_cols.get(col.key)]
    for col in active:
        table.add_column(col.name, style=col.style, **col.kwargs)

    display = get_display_entries(entries, max_rows, offset)
    highlight_idx = cursor - offset

    for idx, entry in enumerate(display):
        row_style = "on grey23" if idx == highlight_idx else None
        cells = [col.render(entry, ctx) for col in active]
        table.add_row(*cells, style=row_style)

    return table


def filter_session_log(session_id: str) -> str | None:
    """Filter the command log down to one session, write it to a temp file, and
    return that path (or None if there is nothing to show)."""
    if not session_id or not LOG_PATH.exists():
        return None
    safe_id = re.sub(r"[^a-zA-Z0-9-]", "", session_id[:8]) or "unknown"
    out_path = os.path.join(tempfile.gettempdir(), f"claude-trail-session-{safe_id}.jsonl")
    with open(LOG_PATH, "r", encoding="utf-8") as f_in, open(out_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            entry = parse_line(line.strip())
            if entry and entry.get("session_id", "") == session_id:
                f_out.write(line)
    return out_path


def open_file_folder(entry: dict) -> None:
    """Open the folder containing files referenced in the command."""
    cmd = entry.get("command", "")
    cwd = entry.get("cwd", "")
    paths = FILE_PATH_RE.findall(cmd)

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


def _panel_title(extra: str = "") -> str:
    """Title bar markup: ` claude-trail vX.Y.Z [extra] `."""
    parts = ["[bold cyan] claude-trail [/bold cyan]"]
    if __version__:
        parts.append(f"[dim]v{__version__}[/dim]")
    if extra:
        parts.append(f"[dim] {extra}[/dim]")
    return "".join(parts) + " "


def build_detail_panel(
    entry: dict,
    term_height: int,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
) -> Panel:
    """Full-screen view of a single command: metadata header plus the complete,
    untruncated command text (newlines preserved)."""
    cmd = entry.get("command", "")
    ctx = RenderCtx(name_map=name_map, color_map=color_map)

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right", no_wrap=True)
    meta.add_column(ratio=1)
    meta.add_row("Time", format_time(entry.get("timestamp", "")))
    meta.add_row("Session", _render_session(entry, ctx))
    meta.add_row("Directory", entry.get("cwd", "") or "(none)")
    files = extract_files(cmd)
    if files:
        meta.add_row("Files", files)

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

    hint = Text.from_markup("[dim italic]esc / q / ↵ : back[/dim italic]")
    body = Group(meta, Text(""), cmd_panel, Text(""), hint)

    return Panel(
        body,
        title=_panel_title("· command"),
        subtitle=f"[dim]{LOG_PATH}[/dim]",
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


def visible_row_count(term_height: int) -> int:
    """Rows available for the table body once the panel chrome is accounted for."""
    return max(term_height - CHROME_ROWS, MIN_VISIBLE_ROWS)


def build_panel(
    entries: list[dict],
    term_height: int,
    visible_cols: dict[int, bool],
    cursor: int,
    offset: int = 0,
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
) -> Panel:
    max_rows = visible_row_count(term_height)

    if not entries:
        content = Text("Waiting for commands...\n\n", style="dim italic")
        content.append("Make sure the hook is configured in ~/.claude/settings.json\n", style="dim")
        content.append(f"Watching: {LOG_PATH}", style="dim")
    else:
        content = build_table(entries, max_rows, visible_cols, cursor, offset, name_map, color_map)

    # Count sessions active in the last 5 minutes
    now = datetime.now().astimezone()
    active_sessions = set()
    for entry in entries:
        try:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            if (now - ts).total_seconds() < 300:
                active_sessions.add(entry.get("session_id", ""))
        except (ValueError, TypeError):
            pass
    active_count = len(active_sessions)

    col_toggles = " ".join(
        f"[bold]{k}[/bold]" if visible_cols[k] else f"[dim]{k}[/dim]"
        for k in sorted(visible_cols)
    )

    status = Text()
    status.append(" \u25cf", style="bright_green")
    status.append(f"  {active_count} active", style="bold yellow" if active_count > 0 else "dim")
    if entries:
        status.append(f"  \u00b7  {cursor + 1}/{len(entries)}", style="dim")
    else:
        status.append("  \u00b7  0/0", style="dim")

    status2 = Text.from_markup(
        f"  [dim italic]cols:[/dim italic] {col_toggles}"
        "  [dim italic]\u00b7  j/k:nav  \u21b5:view  o:session  f:folder  q:quit  c:clear[/dim italic]"
    )

    body = Group(content, Text(""), status, status2)

    return Panel(
        body,
        title=_panel_title(),
        subtitle=f"[dim]{LOG_PATH}[/dim]",
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


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


def read_last_entries(path: Path, n: int) -> tuple[list[dict], int]:
    """Read up to n most recent entries from path. Returns (entries, end_pos)."""
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    chunk_size = 8192
    data = b""
    pos = size
    while pos > 0 and data.count(b"\n") < n + 1:
        read_size = min(chunk_size, pos)
        pos -= read_size
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read(read_size) + data
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()[-n:]
    entries = [e for e in (parse_line(l) for l in lines) if e]
    return entries, size


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
    entry = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": tool_input.get("command", ""),
        "cwd": data.get("cwd", ""),
        "session_id": data.get("session_id", ""),
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return 0


class Action(enum.Enum):
    """Outcome of one keypress that main() must act on outside of state mutation."""

    QUIT = enum.auto()
    OPEN_SESSION = enum.auto()
    OPEN_FILES = enum.auto()
    NONE = enum.auto()


@dataclass
class AppState:
    """All view state for the TUI, independent of terminal I/O.

    INVARIANT: entries are stored oldest-first (append order); `cursor` and
    `scroll_offset` index the newest-first VIEW, where index 0 = the newest
    entry. `selected_entry()` is the single place that bridges the two by
    reversing `entries`.
    """

    entries: list[dict]
    cursor: int = 0
    scroll_offset: int = 0
    visible_cols: dict[int, bool] = field(
        default_factory=lambda: {c.key: True for c in COLUMNS}
    )
    detail_entry: dict | None = None

    def selected_entry(self) -> dict | None:
        """The entry the cursor points at in the newest-first view, or None."""
        if not self.entries or self.cursor < 0 or self.cursor >= len(self.entries):
            return None
        return list(reversed(self.entries))[self.cursor]

    def move_up(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def move_down(self) -> None:
        self.cursor = min(max(0, len(self.entries) - 1), self.cursor + 1)

    def goto_top(self) -> None:
        self.cursor = 0
        self.scroll_offset = 0

    def goto_bottom(self) -> None:
        self.cursor = max(0, len(self.entries) - 1)

    def toggle_col(self, col: int) -> None:
        """Flip a column's visibility, but never hide the last visible column."""
        if not self.visible_cols[col] or sum(self.visible_cols.values()) > 1:
            self.visible_cols[col] = not self.visible_cols[col]

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


def apply_key(state: AppState, ch: str, max_rows: int) -> Action:
    """Apply one keypress to `state`, returning the Action main() must perform.

    Mutates `state` but spawns no processes and touches no terminal.
    """
    if state.detail_entry is not None:
        # Modal full-command view: esc / q / enter return to the table;
        # Ctrl-C still quits.
        if ch == "\x03":
            return Action.QUIT
        if ch in ("\x1b", "q", "\r", "\n"):
            state.detail_entry = None
        return Action.NONE
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
    elif ch.isdigit() and int(ch) in state.visible_cols:
        state.toggle_col(int(ch))
    elif ch in ("\r", "\n"):
        state.detail_entry = state.selected_entry()
    elif ch == "o":
        return Action.OPEN_SESSION
    elif ch == "f":
        return Action.OPEN_FILES
    return Action.NONE


def _signal_exit(signum, frame):
    """Fatal-signal handler: raise SystemExit so main()'s finally restores the tty.

    SystemExit is a BaseException, so the loop's `except KeyboardInterrupt`
    does not swallow it and it propagates to the terminal-restoring finally.
    """
    raise SystemExit(128 + signum)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        sys.exit(hook_main())

    console = Console()

    entries, last_pos = read_last_entries(LOG_PATH, MAX_ENTRIES)
    state = AppState(entries=entries)
    name_map = load_session_names()
    color_map = load_session_colors({e.get("session_id", "") for e in state.entries})

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
        if state.detail_entry is not None:
            return build_detail_panel(state.detail_entry, console.height, name_map, color_map)
        return build_panel(
            state.entries, console.height, state.visible_cols,
            state.cursor, state.scroll_offset, name_map, color_map,
        )

    try:
        with Live(
            render_panel(),
            console=console,
            refresh_per_second=4,
        ) as live:

            def open_session_log(entry: dict) -> None:
                out_path = filter_session_log(entry.get("session_id", ""))
                if not out_path:
                    return
                editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
                if editor and interactive:
                    # Terminal editor (nvim, vim, ...): suspend the live display
                    # and hand the tty over, then restore. Launching it detached
                    # would fight the TUI for the terminal and corrupt the screen.
                    live.stop()
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        subprocess.call(shlex.split(editor) + [out_path])
                    except (OSError, ValueError):
                        pass
                    tty.setcbreak(fd)
                    live.start(refresh=True)
                else:
                    # No editor configured: hand off to the GUI file launcher.
                    try:
                        subprocess.Popen(
                            [_platform_opener(), out_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                    except OSError:
                        pass

            try:
                while True:
                    max_rows = visible_row_count(console.height)
                    quit_now = False
                    acted = False
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
                            if action is Action.OPEN_SESSION:
                                entry = state.selected_entry()
                                if entry:
                                    open_session_log(entry)
                            elif action is Action.OPEN_FILES:
                                entry = state.selected_entry()
                                if entry:
                                    open_file_folder(entry)
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

                    name_map = load_session_names()
                    color_map = load_session_colors({e.get("session_id", "") for e in state.entries})

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
