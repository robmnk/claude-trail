# bash-feed

Real-time TUI dashboard showing all Bash commands executed by Claude Code sessions.

## Architecture

```
Claude Code session
  └─ PostToolUse hook (hook.sh)
       └─ Appends JSON to ~/.claude/command-log.jsonl
            └─ feed.py tails the file and renders with Rich
```

## Files

| File | Purpose |
|------|---------|
| `feed.py` | Rich-based TUI — tails `command-log.jsonl`, renders table with dangerous-command highlighting |
| `hook.sh` | PostToolUse hook — captures Bash tool calls (command, cwd, session_id, timestamp) |
| `requirements.txt` | Python deps (`rich>=13.0`) |

## Key Paths

- **Log file:** `~/.claude/command-log.jsonl`
- **Hook config:** `~/.claude/settings.json` → `hooks.PostToolUse`

## Hook Setup

The hook must be registered in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "command": "/home/naka/Projects/personal/bash-feed/hook.sh"
      }
    ]
  }
}
```

## Running

```bash
cd ~/Projects/personal/bash-feed
python3 feed.py
```

Keys: `q` to quit, `c` to clear display.

## Conventions

- Log format is JSONL with fields: `timestamp`, `command`, `cwd`, `session_id`
- Dangerous commands (rm, sudo, git push, etc.) are flagged with red `*` prefix
- Display shows newest-first, max 50 entries, with active session count
- Poll interval: 300ms
