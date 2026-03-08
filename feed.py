#!/usr/bin/env python3
"""bash-feed: realtime TUI showing all Bash commands Claude Code executes."""

import json
import os
import re
import select
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path

from rich.console import Console
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
    r"|git\s+push\b|git\s+reset\b"     # git destructive
    r"|git\s+checkout\s+--"            # git discard
    r"|git\s+clean\b|git\s+branch\s+-[dD]" # git cleanup
    r"|truncate\b|tee\b"              # write/overwrite
    r"|>\s*/"                          # redirect to absolute path
    r"|curl\b.*\|\s*(?:bash|sh)\b"     # pipe to shell
    r"|wget\b|curl\b.*-o\b"           # download/write
    r"|pip\s+install\b|npm\s+install\b" # package install
    r"|docker\s+rm\b|docker\s+rmi\b"   # container removal
    r"|systemctl\b|service\b"          # service control
    r")",
    re.IGNORECASE,
)


def is_dangerous(cmd: str) -> bool:
    return bool(DANGEROUS_PATTERNS.search(cmd))


def short_path(cwd: str) -> str:
    """Show .../last_dir, truncated to 10 chars."""
    name = os.path.basename(cwd) or cwd
    if len(name) > 10:
        return ".../" + name[:10]
    return ".../" + name


def short_session(sid: str) -> str:
    """First 8 chars of session ID."""
    return sid[:8] if sid else "--------"


def truncate_cmd(cmd: str, width: int = 80) -> str:
    """Truncate command, collapse whitespace."""
    cmd = " ".join(cmd.split())
    if len(cmd) > width:
        return cmd[: width - 1] + "\u2026"
    return cmd


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


def build_table(entries: list[dict], max_rows: int) -> Table:
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_edge=False,
        pad_edge=False,
        row_styles=["", "dim"],
    )
    table.add_column("Time", style="cyan", width=8, no_wrap=True)
    table.add_column("Session", style="magenta", width=8, no_wrap=True)
    table.add_column("Directory", style="green", width=14, no_wrap=True)
    table.add_column("Command", style="white", ratio=1)

    # Newest first, limited to what fits the terminal
    visible = entries[-max_rows:] if len(entries) > max_rows else entries
    for entry in reversed(visible):
        cmd = entry.get("command", "")
        cmd_text = Text()
        if is_dangerous(cmd):
            cmd_text.append("* ", style="bold red")
        cmd_text.append(truncate_cmd(cmd, 118 if is_dangerous(cmd) else 120))
        table.add_row(
            format_time(entry.get("timestamp", "")),
            short_session(entry.get("session_id", "")),
            short_path(entry.get("cwd", "")),
            cmd_text,
        )

    return table


def build_panel(entries: list[dict], line_count: int, term_height: int) -> Panel:
    # Panel chrome: 2 border + 2 padding + 1 header + 1 status + 1 blank + 2 table header = ~9 lines
    max_rows = max(term_height - 9, 3)

    if not entries:
        content = Text("Waiting for commands...\n\n", style="dim italic")
        content.append("Make sure the hook is configured in ~/.claude/settings.json\n", style="dim")
        content.append(f"Watching: {LOG_PATH}", style="dim")
    else:
        content = build_table(entries, max_rows)

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

    status = Text()
    status.append(" \u25cf", style="bright_green")
    status.append(f"  {active_count} active", style="bold yellow" if active_count > 0 else "dim")
    status.append(f"  \u00b7  {line_count} commands logged", style="dim")
    status.append("  \u00b7  q to quit  \u00b7  c to clear", style="dim italic")

    from rich.console import Group
    body = Group(content, Text(""), status)

    return Panel(
        body,
        title="[bold cyan] bash-feed [/bold cyan]",
        subtitle=f"[dim]{LOG_PATH}[/dim]",
        box=box.ROUNDED,
        padding=(1, 2),
    )


def tail_file(path: Path, last_pos: int) -> tuple[list[str], int]:
    """Read new lines from file starting at last_pos."""
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if size < last_pos:
        # File was truncated/cleared
        last_pos = 0
    if size == last_pos:
        return [], last_pos
    with open(path, "r") as f:
        f.seek(last_pos)
        lines = f.readlines()
        new_pos = f.tell()
    return lines, new_pos


def main():
    console = Console()
    entries: list[dict] = []
    line_count = 0

    # Load existing entries from file
    if LOG_PATH.exists():
        with open(LOG_PATH, "r") as f:
            for line in f:
                entry = parse_line(line.strip())
                if entry:
                    entries.append(entry)
                    line_count += 1
        last_pos = LOG_PATH.stat().st_size
    else:
        last_pos = 0

    # Keep only last MAX_ENTRIES
    entries = entries[-MAX_ENTRIES:]

    interactive = sys.stdin.isatty()
    if interactive:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    try:
        with Live(
            build_panel(entries, line_count, console.height),
            console=console,
            refresh_per_second=4,
        ) as live:
            try:
                while True:
                    if interactive:
                        ready, _, _ = select.select([sys.stdin], [], [], POLL_INTERVAL)
                        if ready:
                            ch = sys.stdin.read(1)
                            if ch in ("q", "\x03"):
                                break
                            if ch == "c":
                                entries.clear()
                                line_count = 0
                    else:
                        time.sleep(POLL_INTERVAL)

                    new_lines, last_pos = tail_file(LOG_PATH, last_pos)
                    for line in new_lines:
                        entry = parse_line(line.strip())
                        if entry:
                            entries.append(entry)
                            line_count += 1

                    # Trim to max
                    if len(entries) > MAX_ENTRIES:
                        entries = entries[-MAX_ENTRIES:]

                    live.update(build_panel(entries, line_count, console.height))
            except KeyboardInterrupt:
                pass
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
