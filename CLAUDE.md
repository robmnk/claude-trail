# claude-trail

Real-time TUI dashboard showing all Bash commands executed by Claude Code sessions.

## Architecture

```
Claude Code session
  └─ PostToolUse hook (hook.sh)
       └─ Appends JSON to ~/.claude/command-log.jsonl
            └─ claude_trail.py tails the file and renders with Rich
```

## Files

| File | Purpose |
|------|---------|
| `claude_trail.py` | Rich-based TUI — tails `command-log.jsonl`, renders 5-column table with cursor navigation, column toggles, and action keys |
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
        "command": "/home/naka/Projects/personal/claude-trail/hook.sh"
      }
    ]
  }
}
```

## Running

```bash
cd ~/Projects/personal/claude-trail
python3 claude_trail.py
```

## Controls

| Key | Action |
|-----|--------|
| `j`/`k` or `↓`/`↑` | Move cursor down/up |
| `g`/`G` | Jump to top/bottom |
| `Enter` | Open selected session's commands (filtered JSONL) in `$VISUAL` or the platform default launcher |
| `f` | Open file manager on folder of files referenced in selected command |
| `1`–`5` | Toggle columns: 1=Time, 2=Session, 3=Directory, 4=Files, 5=Command |
| `c` | Clear display |
| `q` | Quit |

## Columns

| # | Name | Content |
|---|------|---------|
| 1 | Time | HH:MM:SS timestamp |
| 2 | Session | First 8 chars of session_id |
| 3 | Directory | Abbreviated cwd |
| 4 | Files | File paths extracted from command (basenames, max 3 shown) |
| 5 | Command | Full command text, dangerous commands prefixed with red `*` |

## Conventions

- Log format is JSONL with fields: `timestamp`, `command`, `cwd`, `session_id`
- Dangerous commands (rm, sudo, git reset, chmod, etc.) are flagged with red `*` prefix
- Display shows newest-first, max 50 entries, with active session count
- Poll interval: 300ms
- File paths extracted via regex matching absolute (`/...`), home (`~/...`), and relative (`./...`) paths
- Session JSONL written under `tempfile.gettempdir()` (default `/tmp`) as `claude-trail-session-{sanitized-id}.jsonl` on Enter
- Column visibility persists during session, status bar shows toggle state as `1 2 3 4 5`
- Platform-specific file launcher: `xdg-open` on Linux, `open` on macOS, selected via `claude_trail._platform_opener()`. Honours `$VISUAL` first if set.
