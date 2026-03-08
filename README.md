# bash-feed

Real-time TUI dashboard that shows every Bash command Claude Code executes across all sessions.

![TUI showing command feed with timestamps, sessions, and dangerous command highlighting]

## Features

- **Live command feed** — watches all Claude Code Bash tool calls in real-time
- **Multi-session aware** — tracks commands across concurrent sessions with color-coded IDs
- **Dangerous command highlighting** — flags risky operations (`rm`, `sudo`, `git push`, etc.) with red markers
- **Active session indicator** — shows how many sessions have been active in the last 5 minutes
- **Minimal footprint** — single Python file, one dependency (`rich`)

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
| `q` | Quit |
| `c` | Clear display |

## Dangerous Command Detection

Commands matching these patterns are flagged:

- File operations: `rm`, `mv`, `cp`, `dd`, `shred`, `truncate`
- Permissions: `chmod`, `chown`, `sudo`, `su`
- Git destructive: `git push`, `git reset`, `git clean`, `git checkout --`
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

## License

MIT
