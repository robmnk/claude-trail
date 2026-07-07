#!/usr/bin/env python3
"""claude-trail: realtime TUI showing all Bash commands Claude Code executes.

Pipeline:
    A PostToolUse hook (`claude-trail hook`) appends one JSON line per Bash
    command to CONFIG_DIR/command-log.jsonl (default ~/.claude). The TUI tails
    that file, folds the new lines into an AppState (entries plus cursor/scroll/
    column view state), and Rich renders the state as a newest-first table or a
    full-command detail panel.

Layout: the file is split into `# ==== <name> ====` banners, in file order:
    config/paths, danger + path patterns, columns, session /color parsing,
    danger detection + path extraction, session names, session colors,
    format/parse helpers, rendering (table), side-effecting actions,
    rendering (panels), input decoding, file tailing, hook,
    AppState + apply_key, main.
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
from dataclasses import dataclass, field, replace
from datetime import datetime
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
MAX_ENTRIES = 1000
POLL_INTERVAL = 0.3
CHROME_ROWS = 10  # title, status, padding
# Non-body lines around the search-results table (borders, padding, header/root
# rows, table header, blank spacers, hint); the results window is sized so the
# table body never overflows the panel and crops the cursor row out of view.
SEARCH_CHROME_ROWS = 11
MIN_VISIBLE_ROWS = 3
SESSION_NAME_CACHE_TTL = 2.0  # seconds
SESSION_COLOR_CACHE_TTL = 5.0  # seconds
AGENT_LABEL_CACHE_TTL = 2.0  # seconds
SESSION_MODEL_CACHE_TTL = 2.0  # seconds; keeps the modal's running->done live
AGENT_TOOLUSE_BYTE_CAP = 512 * 1024  # cap per agent transcript when counting tool_use
# Chars of the agent_id shown as an agent's distinct short identifier (feed +
# session modal). Placed first in the label so it survives the Agent column's
# ellipsis: two subagents sharing a description still read as distinct.
AGENT_ID_LABEL_LEN = 4
ACTIVE_WINDOW_SECONDS = 300  # a session counts as active if seen within this window
SEARCH_RESULT_LIMIT = 500  # max hits kept per search; the rest are dropped (capped)
SEARCH_TIMEOUT = 5.0  # seconds before rg/grep is killed so a huge tree can't hang the UI
# Editors that accept a `+<line>` argument to open a file at a line (vi family).
_VI_FAMILY = frozenset({"vi", "vim", "nvim", "view", "vimdiff", "nvi", "gvim"})


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


# ==== Columns ====


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
    and `kwargs` are passed to `Table.add_column`. `render(entry, ctx)` produces
    that column's cell for a single log entry as a `str` or `rich.text.Text`.
    Adding a column is a single entry in the `COLUMNS` list below.
    """

    key: int
    name: str
    style: str
    kwargs: dict
    render: Callable[[LogEntry, RenderCtx], object]


def _danger_prefixed(cmd: str, body: str) -> Text:
    """A `Text` of `body`, prefixed with a bold-red `* ` when `cmd` is dangerous."""
    text = Text()
    if is_dangerous(cmd):
        text.append("* ", style="bold red")
    text.append(body)
    return text


def _render_time(entry: LogEntry, ctx: RenderCtx):
    return format_time(entry.get("timestamp", ""))


def _render_session(entry: LogEntry, ctx: RenderCtx):
    sid = entry.get("session_id", "")
    label = session_label(sid, ctx.name_map)
    color = rich_color(ctx.color_map.get(sid)) if ctx.color_map else None
    return Text(label, style=color) if color else label


def _render_directory(entry: LogEntry, ctx: RenderCtx):
    return short_path(entry.get("cwd", ""))


def _render_files(entry: LogEntry, ctx: RenderCtx):
    return extract_files(entry.get("command", ""))


def _render_command(entry: LogEntry, ctx: RenderCtx):
    cmd = entry.get("command", "")
    return _danger_prefixed(cmd, normalize_cmd(cmd))


def _render_agent(entry: LogEntry, ctx: RenderCtx):
    """The agent tree-gutter cell for one row.

    Main-agent rows (`ctx.agent_label is None`) render blank. A subagent row
    draws a connector glyph chosen by its position in the run: `─` for a
    single-row run, `┌` at the top (newest), `└` at the bottom (oldest), `│`
    for the rows between. The run's label is shown only on its first (`┌`/`─`)
    row; continuation rows carry the connector alone.
    """
    label = ctx.agent_label
    if label is None:
        return Text("")
    glyph = ("─ " if ctx.run_first and ctx.run_last else
             "┌ " if ctx.run_first else
             "└ " if ctx.run_last else "│ ")
    return Text(glyph + (label if ctx.run_first else ""), style="cyan")


COLUMNS = [
    Column(1, "Time", "cyan", {"width": 8, "no_wrap": True}, _render_time),
    Column(2, "Session", "magenta",
           {"width": 20, "no_wrap": True, "overflow": "ellipsis"}, _render_session),
    Column(6, "Agent", "cyan",
           {"width": 24, "no_wrap": True, "overflow": "ellipsis"}, _render_agent),
    Column(3, "Directory", "green", {"width": 14, "no_wrap": True}, _render_directory),
    Column(4, "Files", "yellow",
           {"width": 20, "no_wrap": True, "overflow": "ellipsis"}, _render_files),
    Column(5, "Command", "white",
           {"ratio": 1, "no_wrap": True, "overflow": "ellipsis"}, _render_command),
]

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


def short_path(cwd: str) -> str:
    """Show .../last_dir, truncated to 10 chars."""
    name = os.path.basename(cwd) or cwd
    if len(name) > 10:
        return ".../" + name[:10]
    return ".../" + name


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


def agent_label(entry: dict, agent_map: dict[str, dict]) -> str | None:
    """Feed label for the agent that ran this command, or None for the main agent.

    Formatted `"<short-id> <name>"`, id first so it survives the Agent column's
    ellipsis: two subagents sharing a description are still distinguishable by
    their ids. `name` is the description, falling back to the agent type, and is
    dropped when neither is known (leaving the short id alone). The full
    description/type live on the session-detail modal (`s`)."""
    aid = entry_agent_id(entry)
    if not aid:
        return None
    info = agent_map.get(aid) or {}
    name = info.get("description") or info.get("type") or ""
    short = short_agent_id(aid)
    return f"{short} {name}" if name else short


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


_SUBAGENT_STATUS_ORDER = {"running": 0, "done": 1, "stopped": 2}


def _load_subagents(sid: str, transcript: Path | None, live: bool) -> tuple:
    """Build the sorted `SubagentInfo` tuple for a session (active agents first)."""
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
        infos.append(SubagentInfo(
            agent_id=agent_id,
            agent_type=data.get("agentType", ""),
            description=data.get("description", ""),
            status=status,
            command_count=count,
            command_count_capped=capped,
        ))
    infos.sort(key=lambda s: (_SUBAGENT_STATUS_ORDER.get(s.status, 3),
                              s.description, s.agent_id))
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


def format_time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
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


def build_detail_panel(
    entry: LogEntry,
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


def build_session_panel(
    model: "SessionModel",
    name_map: dict[str, str] | None = None,
    color_map: dict[str, str] | None = None,
    term_height: int = 40,
) -> Panel:
    """Full-screen session-detail view (opened with `s`).

    A metadata header (id, name tinted by the session's `/color` plus its live
    status, `~`-abbreviated folder, full transcript path, version, start time)
    followed by a table of the session's subagents: status glyph
    (`●` running / `✓` done / `✗` stopped), description, agent type, and the
    tool-call count (`N+` when the count was byte-capped). Empty state reads
    "No subagents." `load_session_model` is cached, so rebuilding this each
    frame is cheap and still reflects live running->done transitions.
    """
    sid = model.session_id
    color = rich_color(color_map.get(sid)) if color_map else None

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right", no_wrap=True)
    meta.add_column(ratio=1)

    name = Text(model.name or session_label(sid, name_map), style=color or "")
    if model.status:
        name.append(f"  ({model.status})", style="bold green")
    meta.add_row("Session", sid or "(none)")
    meta.add_row("Name", name)
    meta.add_row("Folder", _tilde(model.cwd))
    meta.add_row("Transcript", Text(model.transcript_path or "(none)", style="dim"))
    meta.add_row("Version", model.version or "(unknown)")
    meta.add_row("Started", _session_started_line(model))

    running = sum(1 for s in model.subagents if s.status == "running")
    header = Text.from_markup(
        f"[bold]Subagents[/bold] [dim]({len(model.subagents)}, {running} running)[/dim]"
    )

    if model.subagents:
        subs = Table(box=None, pad_edge=False, header_style="dim", expand=True)
        subs.add_column(" ", width=1)
        subs.add_column("Subagent", ratio=1, no_wrap=True, overflow="ellipsis")
        subs.add_column("Type", width=16, no_wrap=True, overflow="ellipsis")
        subs.add_column("Status", width=8)
        subs.add_column("Cmds", justify="right", width=5)
        for s in model.subagents:
            glyph, gstyle = _SUBAGENT_GLYPHS.get(s.status, ("?", "dim"))
            count = f"{s.command_count}+" if s.command_count_capped else str(s.command_count)
            count_cell = count if s.command_count else Text(count, style="dim")
            short = short_agent_id(s.agent_id)
            subs.add_row(
                Text(glyph, style=gstyle),
                f"{short}  {s.description}" if s.description else short,
                s.agent_type,
                Text(s.status, style=gstyle),
                count_cell,
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
            tbl.add_row(
                _rel_to(path, base), str(line), text.strip(),
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


def count_active_sessions(
    entries: list[LogEntry], now: datetime, window: int = ACTIVE_WINDOW_SECONDS
) -> int:
    """Count DISTINCT session_ids with an entry within the last `window` seconds.

    `now` should be timezone-aware. Entries whose timestamp fails to parse, or
    whose naive/aware-ness makes the subtraction invalid, are ignored (the
    `datetime.fromisoformat` parse and the elapsed-time check share one
    try/except, matching the original inline logic exactly).
    """
    active = set()
    for entry in entries:
        try:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            if (now - ts).total_seconds() < window:
                active.add(entry.get("session_id", ""))
        except (ValueError, TypeError):
            pass
    return len(active)


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

    Cursor/offset contract matches `build_table`: the display is newest-first,
    the highlighted row is at `highlight_idx = cursor - offset`, and `offset`
    skips the N newest rows. The status bar reports `cursor + 1`/`len(entries)`
    (1-based over the newest-first view).
    """
    max_rows = visible_row_count(term_height)

    if not entries:
        content = Text("Waiting for commands...\n\n", style="dim italic")
        content.append("Make sure the hook is configured in ~/.claude/settings.json\n", style="dim")
        content.append(f"Watching: {LOG_PATH}", style="dim")
    else:
        content = build_table(entries, max_rows, visible_cols, cursor, offset,
                              name_map, color_map, agent_map)

    active_count = count_active_sessions(entries, datetime.now().astimezone())

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
        "  [dim italic]\u00b7  j/k:nav  \u21b5:view  s:session  o:log  f:folder  q:quit  c:clear[/dim italic]"
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
    OPEN_FILES = enum.auto()
    RUN_SEARCH = enum.auto()        # run rg/grep for the open search modal
    OPEN_MATCH = enum.auto()        # open the selected search hit at its line
    OPEN_MATCH_FOLDER = enum.auto()  # open the folder containing the selected hit
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


@dataclass
class AppState:
    """All view state for the TUI, independent of terminal I/O.

    INVARIANT: entries are stored oldest-first (append order); `cursor` and
    `scroll_offset` index the newest-first VIEW, where index 0 = the newest
    entry. `selected_entry()` is the single place that bridges the two by
    reversing `entries`.
    """

    entries: list[LogEntry]
    cursor: int = 0
    scroll_offset: int = 0
    visible_cols: dict[int, bool] = field(
        default_factory=lambda: {c.key: True for c in COLUMNS}
    )
    detail_entry: LogEntry | None = None
    session_detail: str | None = None  # a session_id while the session modal is open
    search: "SearchState | None" = None  # set while the search modal is open

    def modal_open(self) -> bool:
        """True while either full-screen modal (command detail or session detail)
        is open. Only one is ever open at a time; while open, navigation/column/
        clear keys are ignored (only Ctrl-C still quits)."""
        return self.detail_entry is not None or self.session_detail is not None

    def selected_entry(self) -> LogEntry | None:
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


def apply_key(state: AppState, ch: str, max_rows: int) -> Action:
    """Apply one keypress to `state`, returning the Action main() must perform.

    Mutates `state` but spawns no processes and touches no terminal.
    """
    if state.search is not None:
        # The search modal is mutually exclusive with the command/session modals
        # and takes precedence: it owns every key while open.
        return _apply_search_key(state, state.search, ch)
    if state.modal_open():
        # Modal view (command detail or session detail): esc / q / enter return
        # to the table; Ctrl-C still quits. Clearing both fields keeps the two
        # modals mutually exclusive.
        if ch == "\x03":
            return Action.QUIT
        if ch in ("\x1b", "q", "\r", "\n"):
            state.detail_entry = None
            state.session_detail = None
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
    elif ch == "s":
        # Open the session-detail modal for the selected row's session. Opening
        # it precludes the command modal (only one modal open at a time).
        entry = state.selected_entry()
        # Treat an empty/missing session_id (hand-edited or legacy line) as no
        # session so the modal stays closed instead of showing a blank identity.
        state.session_detail = (entry.get("session_id") or None) if entry else None
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
    elif ch == "o":
        return Action.OPEN_SESSION
    elif ch == "f":
        return Action.OPEN_FILES
    return Action.NONE


# ==== Main ====


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
            return build_session_panel(
                load_session_model(state.session_detail),
                name_map, color_map, console.height,
            )
        if state.detail_entry is not None:
            return build_detail_panel(state.detail_entry, console.height, name_map, color_map)
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

            def open_session_log(entry: LogEntry) -> None:
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
                    session_ids = {e.get("session_id", "") for e in state.entries}
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
