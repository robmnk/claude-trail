#!/usr/bin/env python3
"""claude-trail: realtime TUI showing all Bash commands Claude Code executes."""

import json
import os
import re
import select
import subprocess
import sys
import tempfile
import termios
import time
import tty
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

LOG_PATH = Path.home() / ".claude" / "command-log.jsonl"
MAX_ENTRIES = 50
POLL_INTERVAL = 0.3

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
    r"|>\s*/"                          # redirect to absolute path
    r"|curl\b.*\|\s*(?:bash|sh)\b"     # pipe to shell
    r"|wget\b|curl\b.*-o\b"           # download/write
    r"|pip\s+install\b|npm\s+install\b" # package install
    r"|docker\s+rm\b|docker\s+rmi\b"   # container removal
    r"|systemctl\b|service\b"          # service control
    r")",
    re.IGNORECASE,
)

# Extract file paths from command strings
FILE_PATH_RE = re.compile(
    r"""(?:^|\s|["'])"""
    r"("
    r"""/[^\s;|&()"'>]+"""             # absolute paths
    r"""|~/[^\s;|&()"'>]+"""           # home paths
    r"""|\.\.?/[^\s;|&()"'>]+"""       # relative paths ./ ../
    r")"
)

COLUMNS = {
    1: ("Time", "cyan", {"width": 8, "no_wrap": True}),
    2: ("Session", "magenta", {"width": 8, "no_wrap": True}),
    3: ("Directory", "green", {"width": 14, "no_wrap": True}),
    4: ("Files", "yellow", {"width": 20, "no_wrap": True, "overflow": "ellipsis"}),
    5: ("Command", "white", {"ratio": 1, "no_wrap": True, "overflow": "ellipsis"}),
}


def is_dangerous(cmd: str) -> bool:
    return bool(DANGEROUS_PATTERNS.search(cmd))


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


def short_session(sid: str) -> str:
    """First 8 chars of session ID."""
    return sid[:8] if sid else "--------"


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
) -> Table:
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_edge=False,
        pad_edge=False,
        row_styles=["", "dim"],
    )

    for col_id in sorted(visible_cols):
        if not visible_cols[col_id]:
            continue
        name, style, kwargs = COLUMNS[col_id]
        table.add_column(name, style=style, **kwargs)

    display = get_display_entries(entries, max_rows, offset)
    highlight_idx = cursor - offset

    for idx, entry in enumerate(display):
        cmd = entry.get("command", "")
        row_style = "on grey23" if idx == highlight_idx else None

        cells = []
        for col_id in sorted(visible_cols):
            if not visible_cols[col_id]:
                continue
            if col_id == 1:
                cells.append(format_time(entry.get("timestamp", "")))
            elif col_id == 2:
                cells.append(short_session(entry.get("session_id", "")))
            elif col_id == 3:
                cells.append(short_path(entry.get("cwd", "")))
            elif col_id == 4:
                cells.append(extract_files(cmd))
            elif col_id == 5:
                cmd_text = Text()
                if is_dangerous(cmd):
                    cmd_text.append("* ", style="bold red")
                cmd_text.append(normalize_cmd(cmd))
                cells.append(cmd_text)

        table.add_row(*cells, style=row_style)

    return table


def open_session_jsonl(session_id: str) -> None:
    """Filter log by session_id, write to a temp file, open with $VISUAL or the platform default launcher."""
    if not session_id or not LOG_PATH.exists():
        return
    safe_id = re.sub(r"[^a-zA-Z0-9-]", "", session_id[:8]) or "unknown"
    out_path = os.path.join(tempfile.gettempdir(), f"claude-trail-session-{safe_id}.jsonl")
    with open(LOG_PATH, "r", encoding="utf-8") as f_in, open(out_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            entry = parse_line(line.strip())
            if entry and entry.get("session_id", "") == session_id:
                f_out.write(line)
    opener = os.environ.get("VISUAL") or _platform_opener()
    subprocess.Popen(
        [opener, out_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


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


def build_panel(
    entries: list[dict],
    term_height: int,
    visible_cols: dict[int, bool],
    cursor: int,
    offset: int = 0,
) -> Panel:
    max_rows = max(term_height - 10, 3)

    if not entries:
        content = Text("Waiting for commands...\n\n", style="dim italic")
        content.append("Make sure the hook is configured in ~/.claude/settings.json\n", style="dim")
        content.append(f"Watching: {LOG_PATH}", style="dim")
    else:
        content = build_table(entries, max_rows, visible_cols, cursor, offset)

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
        "  [dim italic]\u00b7  j/k:nav  \u21b5:session  f:folder  q:quit  c:clear[/dim italic]"
    )

    body = Group(content, Text(""), status, status2)

    return Panel(
        body,
        title="[bold cyan] claude-trail [/bold cyan]",
        subtitle=f"[dim]{LOG_PATH}[/dim]",
        box=box.ROUNDED,
        padding=(1, 2),
        height=term_height,
    )


def read_key() -> str:
    """Read a keypress, handling escape sequences for arrow keys."""
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if ready:
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "up"
                elif ch3 == "B":
                    return "down"
        return ch
    return ch


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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        sys.exit(hook_main())

    console = Console()
    cursor = 0
    scroll_offset = 0
    visible_cols = {1: True, 2: True, 3: True, 4: True, 5: True}

    entries, last_pos = read_last_entries(LOG_PATH, MAX_ENTRIES)

    interactive = sys.stdin.isatty()
    if interactive:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    def calc_max_rows():
        return max(console.height - 10, 3)

    def clamp_view():
        """Keep cursor in [0, len(entries)-1] and scroll_offset so cursor is visible."""
        nonlocal cursor, scroll_offset
        if not entries:
            cursor = 0
            scroll_offset = 0
            return
        cursor = max(0, min(cursor, len(entries) - 1))
        max_rows = calc_max_rows()
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + max_rows:
            scroll_offset = cursor - max_rows + 1
        max_offset = max(0, len(entries) - max_rows)
        scroll_offset = max(0, min(scroll_offset, max_offset))

    def selected_entry() -> dict | None:
        if not entries or cursor < 0 or cursor >= len(entries):
            return None
        return list(reversed(entries))[cursor]

    try:
        with Live(
            build_panel(entries, console.height, visible_cols, cursor, scroll_offset),
            console=console,
            refresh_per_second=4,
        ) as live:
            try:
                while True:
                    if interactive:
                        ready, _, _ = select.select([sys.stdin], [], [], POLL_INTERVAL)
                        if ready:
                            ch = read_key()
                            if ch in ("q", "\x03"):
                                break
                            elif ch == "c":
                                entries.clear()
                                cursor = 0
                                scroll_offset = 0
                            elif ch in ("k", "up"):
                                cursor = max(0, cursor - 1)
                                clamp_view()
                            elif ch in ("j", "down"):
                                cursor = min(max(0, len(entries) - 1), cursor + 1)
                                clamp_view()
                            elif ch == "g":
                                cursor = 0
                                scroll_offset = 0
                            elif ch == "G":
                                cursor = max(0, len(entries) - 1)
                                clamp_view()
                            elif ch in ("1", "2", "3", "4", "5"):
                                col = int(ch)
                                if not visible_cols[col] or sum(visible_cols.values()) > 1:
                                    visible_cols[col] = not visible_cols[col]
                            elif ch in ("\r", "\n"):
                                entry = selected_entry()
                                if entry:
                                    open_session_jsonl(entry.get("session_id", ""))
                            elif ch == "f":
                                entry = selected_entry()
                                if entry:
                                    open_file_folder(entry)
                    else:
                        time.sleep(POLL_INTERVAL)

                    new_lines, last_pos = tail_file(LOG_PATH, last_pos)
                    new_count = 0
                    for line in new_lines:
                        entry = parse_line(line.strip())
                        if entry:
                            entries.append(entry)
                            new_count += 1

                    # If user scrolled away from the top, keep them anchored to the
                    # entry they were viewing as new entries shift the reversed view down.
                    if new_count > 0 and cursor > 0:
                        cursor += new_count
                        scroll_offset += new_count

                    if len(entries) > MAX_ENTRIES:
                        entries = entries[-MAX_ENTRIES:]

                    clamp_view()

                    live.update(
                        build_panel(entries, console.height, visible_cols, cursor, scroll_offset)
                    )
            except KeyboardInterrupt:
                pass
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
