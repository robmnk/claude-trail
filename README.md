# bash-feed

Real-time TUI dashboard that shows every Bash command Claude Code executes across all sessions.

![TUI showing command feed with timestamps, sessions, and dangerous command highlighting]

## Features

- **Live command feed** — watches all Claude Code Bash tool calls in real-time
- **Multi-session aware** — tracks commands across concurrent sessions with color-coded IDs
- **Dangerous command highlighting** — flags risky operations (`rm`, `sudo`, `git reset`, etc.) with red markers
- **Active session indicator** — shows how many sessions have been active in the last 5 minutes
- **Minimal footprint** — single Python file, one dependency (`rich`)
- **Cross-platform** — works on Linux and macOS (uses `xdg-open` or `open` automatically)

## How It Works

1. A Claude Code `PostToolUse` hook (`hook.sh`) captures every Bash command and appends it as JSON to `~/.claude/command-log.jsonl`
2. `feed.py` tails the log file and renders a live-updating table using [Rich](https://github.com/Textualize/rich)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Register the hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "command": "/path/to/bash-feed/hook.sh"
      }
    ]
  }
}
```

### 3. Run

```bash
python3 feed.py
```

## Controls

| Key | Action |
|-----|--------|
| `j` / `k` or `↓` / `↑` | Move cursor down / up |
| `g` / `G` | Jump to top / bottom |
| `Enter` | Open selected session's commands (filtered JSONL) in `$VISUAL` or `xdg-open` |
| `f` | Open file manager on the folder of files referenced in the selected command |
| `1`–`5` | Toggle columns (1=Time, 2=Session, 3=Directory, 4=Files, 5=Command) |
| `c` | Clear display |
| `q` | Quit |

## Dangerous Command Detection

Commands matching these patterns are flagged:

- File operations: `rm`, `mv`, `cp`, `dd`, `shred`, `truncate`
- Permissions: `chmod`, `chown`, `sudo`, `su`
- Git destructive: `git reset`, `git clean`, `git checkout --`, `git branch -D`
- Process: `kill`, `killall`, `pkill`
- Network: `curl | bash`, `wget`
- Package: `pip install`, `npm install`
- Services: `systemctl`, `docker rm`

## Log Format

Each line in `~/.claude/command-log.jsonl`:

```json
{
  "timestamp": "2025-01-01T12:00:00.000Z",
  "command": "ls -la",
  "cwd": "/home/user/project",
  "session_id": "abc12345-..."
}
```

## Notes

### Platform support

Linux and macOS. Uses POSIX `termios` and `tty`. The hook emits second-precision ISO-8601 timestamps that work on both BSD and GNU `date`. Windows is not supported (no termios). On macOS, file actions go through `open`; on Linux, `xdg-open`. Both honour `$VISUAL` for opening session JSONL files.

### Log rotation

The log file `~/.claude/command-log.jsonl` grows indefinitely. To bound it with `logrotate`, drop a config like this in `/etc/logrotate.d/bash-feed`:

```
/home/YOUR_USER/.claude/command-log.jsonl {
  weekly
  rotate 4
  copytruncate
  missingok
  notifempty
}
```

Or run a periodic truncation manually (e.g. via cron) once the file exceeds a size you're comfortable with.

## License

MIT
