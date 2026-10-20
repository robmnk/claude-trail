# claude-trail

Observability tool for Claude Code: a real-time TUI dashboard that shows every Bash command Claude Code executes across all sessions.

## Features

- **Live command trail** - watches all Claude Code Bash tool calls in real-time
- **Prompts tab** - `Tab` switches to a second view: one row per prompt you sent, and `Enter` shows everything the main agent and its subagents did in answer to it (every tool, not just Bash)
- **Multi-session aware** - tracks commands across concurrent sessions with color-coded IDs
- **Dangerous command highlighting** - flags risky operations (`rm`, `sudo`, `git reset`, etc.) with red markers
- **Prompt behind each command** - the detail view shows the turn that led to the command (the user's ask for a main-agent row, the subagent's own task prompt for an agent row); `Enter` opens it in full
- **Subagent attribution** - tags each command with the subagent that ran it and draws a tree gutter attributing rows to subagent runs; `s` opens a per-session modal listing every subagent with live status
- **Active session indicator** - shows how many sessions have been active in the last 5 minutes
- **Minimal footprint** - two Python files, two dependencies (`rich`, `duckdb`)
- **Cross-platform** - works on Linux and macOS (uses `xdg-open` or `open` automatically)

## How It Works

There are two data paths, and they are independent:

1. A Claude Code `PostToolUse` hook (`claude-trail hook`) captures every Bash command and appends it as JSON to `~/.claude/command-log.jsonl`
2. `claude-trail` tails the log file and renders a live-updating table using [Rich](https://github.com/Textualize/rich)
3. Separately, `claude-trail ingest` reads Claude Code's own transcripts (`~/.claude/projects/**/*.jsonl`) into a local DuckDB store at `~/.claude/trail.duckdb` (`$CLAUDE_TRAIL_DB` moves it). That store covers every tool, not just Bash, and is what the Prompts tab, the token numbers and the `search` / `tokens` / `cat` / `diff` / `prompts` subcommands read. It is as fresh as the last ingest, so a `Stop` hook or a timer keeps it current (see [Audit store](#audit-store))

## Setup

### 1. Install

Pick one:

```bash
# pipx (recommended, broadly available)
pipx install git+https://github.com/robmnk/claude-trail

# uv
uv tool install git+https://github.com/robmnk/claude-trail

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

The hook is built into the `claude-trail` binary, so no path is needed as long as `claude-trail` is on your `PATH` (which `pipx` and `uv tool install` set up automatically).

> Running from a clone instead of installing? Use the absolute path:
> `"command": "python3 /absolute/path/to/claude_trail.py hook"`

### 3. Run

```bash
claude-trail              # if installed via pipx / uv
python3 claude_trail.py   # if running from a clone
```

## Controls

`Tab` switches between the two views: **Commands** (the live feed, below) and
**Prompts**. Each remembers its own cursor and columns.

| Key | Action |
|-----|--------|
| `Tab` | Switch to the Prompts tab (and back) |
| `j` / `k` or `↓` / `↑` | Move cursor down / up |
| `g` / `G` | Jump to top / bottom |
| `Enter` | Open the full-command detail view for the selected command: the whole command, its token cost, and the first 10 lines of the prompt that led to it. `Enter` again opens the full prompt (`j` / `k` / `g` / `G` to scroll, `Esc` back to the command, `q` to close) |
| `s` | Open the session-detail modal for the selected row's session (identity header + subagent list with live status) |
| `d` | Open the audit-store database modal: connection string, DuckDB / schema version, on-disk sizes, block accounting, and per-table row counts (opens the DuckDB store read-only, on demand) |
| `Esc` | Close the open modal (command detail, session detail, or database) (also `q` or `Enter` while it is open; in the full-prompt view `Esc` / `Enter` step back one level and `q` closes outright) |
| `o` | Open the selected session's commands (pretty-printed, filtered to the session) in `$VISUAL` / `$EDITOR`, else the platform launcher (`xdg-open` on Linux, `open` on macOS) |
| `t` | Open a pretty-printed copy of the selected session's transcript (`<sid>.jsonl`) in `$VISUAL` / `$EDITOR`, else the platform launcher |
| `f` | Open the file manager on the folder of files referenced in the selected command |
| `/` | Search the selected row's session recursively (`rg`, else `grep`). `Tab` toggles the root between the transcript folder and the session's cwd; `Enter` runs the query. In results: `j` / `k` browse, `Enter` opens a hit at its line, `f` opens the hit's folder, `/` edits the query, `Esc` / `q` close |
| `1`-`6` | Toggle columns (1=Time, 2=Session, 3=Directory, 4=Files, 5=Command, 6=Agent) |
| `c` | Clear display |
| `q` | Quit |

## Prompts tab

`Tab` opens the same trail grouped by prompt. One row per prompt you sent,
newest first, across every session: when you sent it, which session and project
it went to, how many tool calls it took (with the Bash share), how many
subagents ran, what it cost in output tokens, and its first line.

This view reads the audit store (below), not the live log, so it is as fresh as
the last `ingest` rather than 300ms fresh. The header says how old the snapshot
is; `r` re-reads it. With no store yet, the tab says so and how to make one.

| Key | Action |
|-----|--------|
| `Enter` | Open the turn: what the main agent and its subagents did in answer to that prompt |
| `r` | Re-read the store now |
| `x` | Hide / show the prompts that ran no tool call |
| `S` | Show only the selected row's session, or every session again |
| `1`-`7` | Toggle columns (1=Time, 2=Session, 3=Project, 4=Calls, 5=Agents, 6=Tokens, 7=Prompt) |
| `j` / `k`, `g` / `G`, `s`, `o`, `t`, `d`, `Tab`, `q` | As on the Commands tab |

The turn page lists every tool call in order, with a tree gutter bracketing each
subagent's stretch of work and the danger marker on Bash rows. `b` narrows it to
Bash only, `j` / `k` / `g` / `G` scroll, and `Esc` or `q` goes back to the list.
`Enter` opens the whole prompt; from there `Esc` or `Enter` steps back to the
turn and `q` closes everything.

## Configuration

The fixed-width columns can be resized without editing code. claude-trail reads a
`.env` file (a tiny built-in parser, no extra dependency) from `$CLAUDE_CONFIG_DIR/.env`
(default `~/.claude/.env`), or from the path in `$CLAUDE_TRAIL_ENV` if set. Copy
[`.env.example`](.env.example) and uncomment the keys you want to change:

| Key | Column | Default |
|-----|--------|---------|
| `CLAUDE_TRAIL_COL_TIME_WIDTH` | Time | 8 |
| `CLAUDE_TRAIL_COL_SESSION_WIDTH` | Session | 13 |
| `CLAUDE_TRAIL_COL_AGENT_WIDTH` | Agent | 24 |
| `CLAUDE_TRAIL_COL_DIRECTORY_WIDTH` | Directory | 14 |
| `CLAUDE_TRAIL_COL_FILES_WIDTH` | Files | 20 |
| `CLAUDE_TRAIL_PROMPT_COL_TIME_WIDTH` | Prompts: Time | 8 |
| `CLAUDE_TRAIL_PROMPT_COL_SESSION_WIDTH` | Prompts: Session | 13 |
| `CLAUDE_TRAIL_PROMPT_COL_PROJECT_WIDTH` | Prompts: Project | 16 |
| `CLAUDE_TRAIL_PROMPT_COL_CALLS_WIDTH` | Prompts: Calls | 14 |
| `CLAUDE_TRAIL_PROMPT_COL_AGENTS_WIDTH` | Prompts: Agents | 6 |
| `CLAUDE_TRAIL_PROMPT_COL_TOKENS_WIDTH` | Prompts: Tokens | 16 |

Each value is a positive integer of terminal columns; a missing, non-integer, or
`<= 0` value falls back to the default. Precedence is real environment variable >
`.env` value > built-in default (an env var of the same name overrides the file).
A missing `.env` file is fine (everything uses defaults). The Command column (and
the Prompts tab's Prompt column) has no key: it takes the remaining width. Only
widths are configurable, not colors.

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

## Audit store

Alongside the live TUI, `claude-trail` ships a DuckDB-backed audit store that ingests the Claude Code transcripts (`~/.claude/projects/**/*.jsonl`, subagents included) into a local DuckDB file, `~/.claude/trail.duckdb` (override with `$CLAUDE_TRAIL_DB`), with the stored file contents in a SQLite file next to it, `<store>.blobs.sqlite`. Because it reads transcripts instead of the hook, it covers **every** tool (not just Bash) and captures each call's result and file-edit patch.

A single `ingest` process is the only writer; every other subcommand opens the file read-only:

```bash
claude-trail ingest [--watch] [--detach] [--compact] [--file-history-only] [-v]  # tail transcripts + file-history into the store (single writer)
claude-trail search <regex> [--tool T --session S --project P --since ISO --limit N -i]
claude-trail stats                               # database properties + session / event / token / reconstruction-coverage counts
claude-trail tokens [--session S] [--by session|command|tool|day|model] [--since ISO] [--limit N]
claude-trail prompts [--session S] [--limit N] [--since ISO] [--json]   # the Prompts tab as text
claude-trail prompts (--at ISO --session S | --event-id E) [--json]     # one turn's tool calls
claude-trail cat <path> [--session S] [--at ISO] # reconstruct a file's bytes (rewind with --at)
claude-trail diff <path> [--session S] [--all-sources]  # list a path's edits oldest-first
claude-trail reset-db [-y]                        # delete the store and blob files (rebuilds from transcripts on next ingest)
claude-trail prune-raw [-y] [--no-compact]        # drop the verbatim raw_events payload and compact
```

Run `ingest` from cron or leave one `--watch` process polling; it is idempotent (byte-offset tailing), and `--compact` reclaims free blocks with a full rewrite. Ordinary runs read only the file-history lines they just stored; whichever run finds the last full file-history pass an hour old (or none, tracked by `<store>.fh-full-stamp`) also re-reads every stored snapshot line and the whole `~/.claude/file-history` dir, so every setup (cron, `--detach`, the wrapper, `--watch`) picks up what a crashed run or a missing line left behind without needing `--compact`. The lowest-friction option is a Claude Code `Stop` hook that backgrounds a singleton ingest after every turn, keeping the store fresh with no daemon or cron: `claude-trail ingest --detach` (built-in: its own `fork`/`flock`), or a small `setsid`/`flock`/`timeout` wrapper that adds a hang timeout and folds in periodic `--compact` (see the Hook Setup section in `CLAUDE.md`); either way the detached run appends its output to `<store>.ingest.log`, so a failing ingest stays diagnosable. Each `CHECKPOINT` re-expands the file to ~2x its packed size, so the store steady-states around 2x; periodic `--compact` packs it back (bounded, not unbounded). On a terminal `ingest` shows a live Rich progress bar for the transcript and file-history phases; off a tty (cron/pipe) it prints timestamped progress instead (start, per-phase timing, stored-row deltas, final store size), and `-v` adds one line per changed transcript. `reset-db` deletes the store file and the blob file (and their WAL files) after a confirmation prompt (`-y` skips it); the store is a derived cache, so the next `ingest` rebuilds it. `tokens` reports exact per-session and per-command spend; `cat` / `diff` reconstruct edited files from content-addressed blobs plus `structuredPatch` diffs. `prune-raw` is a one-shot cleanup that drops the verbatim transcript payload (typically ~2/3 of the store) without breaking any command, since the transcripts on disk stay the source of truth.

**`prompts` is the Prompts tab without a terminal.** It lists the same turns as plain columns, then dumps one of them. List first, then copy a row's time back to open it. Both blocks below are real output: the list is scoped to one session (`--limit` defaults to 30 without it), and the dump is piped through `head` because that turn made 43 calls and the dump prints every one of them.

```
$ claude-trail prompts --session 478629d3 --limit 5
time                 sess      project         calls         agents  tokens              prompt
2026-07-06 19:56:06  478629d3  …/claude/trail  1 (1 bash)            1275                save this plan
2026-07-05 04:21:15  478629d3  …/claude/trail  43 (21 bash)  2       44549 (+32216 sub)  first review the plan. than lets finish the specs
2026-07-05 04:20:43  478629d3  …/claude/trail  -                     0                   /effort high
2026-07-05 04:20:38  478629d3  …/claude/trail  -                     0                   /model
2026-07-05 04:17:17  478629d3  …/claude/trail  1                     5664                do the plans/2026-06-30-audit-trail-store.md file contains all the specs for the feature?

$ claude-trail prompts --session 478629d3 --at "2026-07-05 04:21:15" | head -14
session   478629d3-fde1-49a9-a42b-325f7c4240e4  /home/naka/Projects/personal/claude/trail
sent      2026-07-05 04:21:15   last request 2026-07-05 04:31:31
work      43 tool calls: 6 main, 37 across 2 subagents, 21 bash
tokens    44549 output, 32216 by subagents, 43 requests
event-id  d2f94df9-1212-4231-95f6-e7622667f501
prompt    first review the plan. than lets finish the specs

time      agent  tool             detail
04:21:56  main   Agent            Explore worktree current state
04:22:00  a02a   Bash             find /home/naka/Projects/personal/claude-trail-audit-store -type f -name "*.py" | head -20
04:22:08  a02a   Bash             ls -la /home/naka/Projects/personal/claude-trail-audit-store/
04:22:12  a02a   Bash             cd /home/naka/Projects/personal/claude-trail-audit-store && git status && echo "---" && git branch -vv
04:22:15  a02a   Bash             find /home/naka/Projects/personal/claude-trail-audit-store/plans -type f 2>/dev/null
04:22:19  a02a   Read             /home/naka/Projects/personal/claude-trail-audit-store/claude_trail.py
```

The list's `tokens` cell breaks out what the subagents generated (`44549 (+32216 sub)`), which is the same split the dump's `tokens` line prints, and its `calls` cell is a plain `-` for a turn that ran no tool at all, rather than a `0` that reads as missing data. The `project` cell reads `…/claude/trail` here, not `…/claude-trail`, because this old session's transcript is gone, so the store has no launch directory for it and falls back to decoding the transcript's directory name, which cannot tell a separator `-` from one inside a name (see Upgrading below).

The dump prints the turn's header (session, work, tokens, event id), the prompt, and every tool call oldest-first as `time agent tool detail`, with `main` or the subagent's short id in the agent column and a trailing `[error]` on a failed call. The `work` line subtracts: 43 calls is 6 on the main thread plus 37 across the 2 subagents, and the 21 Bash calls are counted over the whole turn, subagents included. A prompt too long to quote is cut to its first 10 lines and the cut is stated rather than silent, as `[+N more lines, M more chars]` (each half only when that cut happened). `--session` takes an id prefix, `--at` finds the newest turn at or before the time you give it (and needs `--session`, since sessions interleave), `--event-id` opens a turn by the id `--json` prints, and `--json` gives a script the raw query rows. Exit codes: 2 for a bad argument, 1 for a missing store or a turn that did not resolve, 0 otherwise.

**Upgrading?** Nothing to run. The Prompts tab's Project column prefers each session's exact launch directory (`sessions.cwd`), which older stores did not record; the next ordinary `claude-trail ingest` fills it in place for every session whose transcript is still on disk, and logs `sessions: N cwd backfilled` once. Sessions whose transcript is gone keep the old, approximate project name. Do **not** run `reset-db` for this: on a store older than its transcripts that deletes history it cannot rebuild. Likewise, the first `ingest` on a store from before schema 3 moves the file contents out of DuckDB into `<store>.blobs.sqlite`, checks every row, then drops the DuckDB copy and compacts; a run that is killed half way resumes on the next one.

**Two content sources.** Transcripts alone carry full edit content for only ~39% of edits (the ones whose `originalFile`/`content` is present); the rest are patch-only. The `ingest` sweep also backfills `~/.claude/file-history/<session_id>/<pathhash>@vN` snapshots as a second content source, which lifts full-content `cat` coverage to ~96% of patch-only paths. Details:

- Snapshot bytes are preserved as content-addressed blobs **even when the path is not yet resolvable** (file-history is the one store input not rebuildable from `raw_events`; the path is filled once an edit or read reveals it, at the latest by the next hourly full file-history pass).
- `cat` reads all sources, so a formerly patch-only file reconstructs. `diff` stays transcript-only by default (a snapshot spans a whole user turn, so including it would double-list every edit); `--all-sources` opts the file-history deltas in.
- `--file-history-only` runs just the backfill pass (does not touch the transcript offsets).
- **`cat` roll-forward:** snapshots are per-user-turn checkpoints of the pre-message state, so a file last edited in a session's final turn has no snapshot of its final bytes. `cat` therefore applies any newer patch-only `structuredPatch` edits on top of the newest stored bytes, strictly (context must match exactly); if a patch no longer applies (the file changed outside Claude), it prints the last consistent version and warns `stale by N edit(s)` on stderr instead of emitting a half-patched mix.

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
