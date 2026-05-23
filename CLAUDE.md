# claude-trail

Real-time TUI dashboard showing all Bash commands executed by Claude Code sessions.

## Architecture

```
Claude Code session
  └─ PostToolUse hook (`claude-trail hook` subcommand)
       └─ Appends JSON to ~/.claude/command-log.jsonl
            └─ `claude-trail` (TUI) tails the file and renders with Rich
```

## Files

| File | Purpose |
|------|---------|
| `claude_trail.py` | Module: TUI (`main`) and PostToolUse hook (`hook_main`). Tails `command-log.jsonl`, renders 5-column table with cursor navigation, column toggles, and action keys |
| `hook.sh` | Legacy bash version of the hook (deprecated, kept for users who still reference it). New installs should use `claude-trail hook`. |
| `pyproject.toml` | Package metadata and `claude-trail` entry point |
| `requirements.txt` | Python deps (`rich>=13.0`) |

## Key Paths

- **Log file:** `~/.claude/command-log.jsonl`
- **Hook config:** `~/.claude/settings.json` → `hooks.PostToolUse`
- **Session metadata:** `~/.claude/sessions/<pid>.json` (read by the TUI to resolve session names)

## Hook Setup

Register in `~/.claude/settings.json` using the built-in subcommand:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "claude-trail hook" }
        ]
      }
    ]
  }
}
```

## Running

```bash
claude-trail              # if installed via pipx / uv tool install
python3 claude_trail.py   # if running from a clone
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
| 2 | Session | Session name from `~/.claude/sessions/<pid>.json` if set, else first 8 chars of session_id, tinted with a per-session color (see `session_color`) |
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
- Session names are read from `~/.claude/sessions/<pid>.json` (`name` field). Cached for 2s in `load_session_names()`; the cache accumulates so a session's name remains resolvable after Claude Code removes its pid.json on exit.
- Each session ID is assigned a stable color from `SESSION_PALETTE` via `md5(session_id) % len(palette)`, so all rows from the same session share a tint in the Session column. Red is reserved for the dangerous-command marker and is excluded from the palette.
- Column visibility persists during session, status bar shows toggle state as `1 2 3 4 5`
- Platform-specific file launcher: `xdg-open` on Linux, `open` on macOS, selected via `claude_trail._platform_opener()`. Honours `$VISUAL` first if set.
