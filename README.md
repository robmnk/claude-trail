# claude-trail

Observability tool for Claude Code: a real-time TUI dashboard that shows every Bash command Claude Code executes across all sessions.

## Features

- **Live command trail** - watches all Claude Code Bash tool calls in real-time
- **Multi-session aware** - tracks commands across concurrent sessions with color-coded IDs
- **Dangerous command highlighting** - flags risky operations (`rm`, `sudo`, `git reset`, etc.) with red markers
- **Subagent attribution** - tags each command with the subagent that ran it and draws a tree gutter attributing rows to subagent runs; `s` opens a per-session modal listing every subagent with live status
- **Active session indicator** - shows how many sessions have been active in the last 5 minutes
- **Minimal footprint** - single Python file, one dependency (`rich`)
- **Cross-platform** - works on Linux and macOS (uses `xdg-open` or `open` automatically)

## How It Works

1. A Claude Code `PostToolUse` hook (`claude-trail hook`) captures every Bash command and appends it as JSON to `~/.claude/command-log.jsonl`
2. `claude-trail` tails the log file and renders a live-updating table using [Rich](https://github.com/Textualize/rich)

## Setup

### 1. Install

Pick one:

```bash
# pipx (recommended, broadly available)
pipx install git+https://github.com/robmnk/claude-trail

# uv
uv tool install git+https://github.com/robmnk/claude-trail

# mise (via its pipx backend)
mise use -g pipx:robmnk/claude-trail

# from a local clone, no install
pip install -r requirements.txt
```

### 2. Register the hook

Add this to the `hooks` section of `~/.claude/settings.json` (merge with any existing `PostToolUse` entries):

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

The hook is built into the `claude-trail` binary, so no path is needed as long as `claude-trail` is on your `PATH` (which `pipx` and `uv tool install` set up automatically; with `mise`, make sure its shims are on your `PATH`).

> Running from a clone instead of installing? Use the absolute path:
> `"command": "python3 /absolute/path/to/claude_trail.py hook"`

### 3. Run

```bash
claude-trail              # if installed via pipx / uv
python3 claude_trail.py   # if running from a clone
```

## Controls

| Key | Action |
|-----|--------|
| `j` / `k` or `↓` / `↑` | Move cursor down / up |
| `g` / `G` | Jump to top / bottom |
| `Enter` | Open the full-command detail view for the selected command |
| `s` | Open the session-detail modal for the selected row's session (identity header + subagent list with live status) |
| `Esc` | Close the open modal, command detail or session detail (also `q` or `Enter` while it is open) |
| `o` | Open the selected session's commands (filtered JSONL) in `$VISUAL` / `$EDITOR`, else the platform launcher (`xdg-open` on Linux, `open` on macOS) |
| `f` | Open the file manager on the folder of files referenced in the selected command |
| `/` | Search the selected row's session recursively (`rg`, else `grep`). `Tab` toggles the root between the transcript folder and the session's cwd; `Enter` runs the query. In results: `j` / `k` browse, `Enter` opens a hit at its line, `f` opens the hit's folder, `/` edits the query, `Esc` / `q` close |
| `1`-`6` | Toggle columns (1=Time, 2=Session, 3=Directory, 4=Files, 5=Command, 6=Agent) |
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
  "timestamp": "2025-01-01T12:00:00-03:00",
  "command": "ls -la",
  "cwd": "/home/user/project",
  "session_id": "abc12345-..."
}
```

Timestamps are in the user's local timezone (ISO 8601 with offset). Commands run inside a subagent also carry optional `agent_id` and `agent_type` fields; their absence means the main agent.

## Notes

### Platform support

Linux and macOS. Uses POSIX `termios` and `tty`. The hook emits second-precision ISO 8601 timestamps in the user's local timezone (with offset). Windows is not supported (no termios). On macOS, file actions go through `open`; on Linux, `xdg-open`. Both honour `$VISUAL` for opening session JSONL files.

### Log rotation

The log file `~/.claude/command-log.jsonl` grows indefinitely. To bound it with `logrotate`, drop a config like this in `/etc/logrotate.d/claude-trail`:

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

## Development

See [ARCHITECTURE.md](ARCHITECTURE.md) for a tour of the internals (data flow,
module map, and the column / `AppState` extension points), and
[CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, style, and PR expectations.

Work from a clone with the dev dependencies installed:

```bash
git clone https://github.com/robmnk/claude-trail
cd claude-trail
pip install -e '.[dev]'   # editable install plus pytest and ruff
```

Run the tests and the linter:

```bash
pytest
ruff check .
```

Run the TUI straight from the checkout without installing:

```bash
python3 claude_trail.py
```

## License

MIT
