# Changelog

All notable changes to claude-trail. Versions follow the version in pyproject.toml.

## Unreleased

## 0.8.0 (2026-10-20)

### Features

- Command view (`Enter`): the command is now colored by part: command names, flags, paths, strings, variables, operators, redirects, heredocs and comments. The red danger marker stays. The text is never changed, so you see exactly what ran, and a command the colorer cannot read is shown as plain text.
- `stats` and the `d` database modal: list the size of the new blob file (`<store>.blobs.sqlite`) and show `missing` if it is gone. `cat` prints one clear line and exits 1 when the blob file is missing.

### Bug fixes

- A file whose content held a lone surrogate (half of a UTF-16 pair) made `ingest` fail on it. It is now stored, and `cat` prints it byte for byte.
- Snapshots recorded only as `file-history-delta` lines were stored with the backup file's modification time. They now carry the backup time Claude Code recorded (at most 18 ms apart on a real store).

### Performance

- `ingest` on a 30 s timer: average CPU per run fell from 5.5 s to 1.2 s (systemd journal, real store). A flush every 5 minutes used 8.9 s CPU, about 16 s and 1.1 GB of memory; the first one after this release used 1.06 s, 1.08 s and 143 MB.
- Where the time went: a skipped flush still ran when the store closed, every run looked up each transcript's offset with its own query (about 1,000 per run), DuckDB tried and failed to `import pandas` about 4,200 times per run, every run re-read all file-history snapshots, and every flush rewrote all stored file contents. Each of these is fixed. The full file-history pass now runs once an hour.
- Store size: a 3.1 GB store became 1.3 GB plus a 291 MB file of stored file contents.
- Hourly `ingest --compact`: peak memory fell from 5.2 GB to about 2.0 GB (measured on copies of the store).
- Reads: the Prompts list query is 4 times faster (314 ms to 78 ms). Other reads are up to 15% slower, and the command view on a subagent row about 20% (4 ms) slower, because the store now uses row groups of 8192 rows.

### Upgrade notes

- The first `ingest` after the upgrade moves the stored file contents out of DuckDB into `<store>.blobs.sqlite` (schema 3). It copies every row, checks each one byte for byte, then drops the DuckDB copy and compacts. On a copy of a real store (10,434 blobs, 729 MB) this took 70 s and 2.9 GB of memory. Nothing to run: it happens on the next `claude-trail ingest`, whatever starts it (the `Stop` hook, a timer or you).
- Any program that opens the store for writing without the 8192-row setting (an older claude-trail, or your own `duckdb` session that writes) merges the row groups back into large ones. Nothing breaks; the next `ingest --compact` splits them again.
- A move that is killed half way resumes on the next run. A move that fails keeps the DuckDB copy and keeps ingesting into it. Every run then logs `error: blobs not moved ...` and the move is retried once an hour. Fix the cause named in that line, then delete `<store>.blob-move-failed` to retry on the next run.
- The store is now two files. Keep the DuckDB file and the `.blobs.sqlite` file together. Do not delete the blob file by hand: after the move it holds the only copy of the stored file contents. `reset-db` now deletes both files.

## 0.7.0 (2026-09-21)

### Features

- `Tab`: switches between the Commands tab and a new Prompts tab. Each remembers its own cursor and columns. The Prompts tab has one row per prompt you sent, newest first, across all sessions: time, session, project, tool calls (with the Bash share), subagents, output tokens, and the first line of the prompt. `s`, `o`, `t` and `d` work on both tabs.
- Prompts tab keys: `Enter` opens the turn, `r` re-reads the store, `x` hides prompts that ran no tool, `S` shows one session only, and `1` to `7` toggle columns. The list refreshes itself every 30 s and the header shows how old the snapshot is.
- Turn page (`Enter` on a prompt): the session, when it was sent, how long it took, the split of work between the main agent and subagents, the tokens, the prompt, and every tool call in order (not only Bash). A tree gutter shows each subagent's calls. `b` shows only Bash calls, `Enter` opens the whole prompt, `Esc` goes back one level and `q` closes everything. A prompt over 100,000 characters is cut and the cut is stated.
- `claude-trail prompts [--session S] [--limit N] [--since ISO] [--json]`: the Prompts tab as plain text.
- `claude-trail prompts --at ISO --session S` (or `--event-id E`): dumps one turn's tool calls as plain text, with the main agent and subagent split and `[error]` on failed calls.
- Project column: shows the exact folder the session was started in, not a guess decoded from the transcript folder name. A project with a hyphen in its name now shows as the path that exists.
- `search --project`: matches the launch folder or the project name shown in the column.
- `.env` keys `CLAUDE_TRAIL_PROMPT_COL_TIME_WIDTH`, `_SESSION_WIDTH`, `_PROJECT_WIDTH`, `_CALLS_WIDTH`, `_AGENTS_WIDTH` and `_TOKENS_WIDTH`: set the Prompts tab column widths.

### Bug fixes

- Command view prompt panel: it could show an `[Image: source: ...]` line instead of the text you typed. It now skips such lines.
- Square brackets in text no longer break the display: a prompt, a search hit or a config path containing something like `[/tmp/x]` could crash or garble the TUI.

### Upgrade notes

- Nothing to run. The next ordinary `ingest` fills in each session's launch folder for every session whose transcript is still on disk, and logs `sessions: N cwd backfilled` once. Sessions whose transcript is gone keep the old, approximate project name.
- Do not run `reset-db` for this. On a store older than its transcripts it deletes history that cannot be rebuilt.
- The Prompts tab and `prompts` read the audit store, so they are only as fresh as the last `ingest`.

## 0.6.9 (2026-07-29)

_Covers 0.6.0, 0.6.2, 0.6.4 to 0.6.7 and 0.6.9._

### Features

- `claude-trail ingest`: new audit store. It reads Claude Code's own transcripts (`~/.claude/projects/**/*.jsonl`, subagents included) into a local DuckDB file, `~/.claude/trail.duckdb` (`$CLAUDE_TRAIL_DB` moves it). It covers every tool, not only Bash, and keeps each call's result and edit patch. It tails by byte offset, so it is safe to run again and again. On a terminal it shows a progress bar. Off a terminal it prints timestamped progress, and `-v` adds one line per changed transcript.
- `search <regex>`, `tokens` and `stats`: search tool calls and results (`--tool`, `--session`, `--project`, `--since`, `--limit`, `-i`), report exact token spend (`--by session|command|tool|day|model`), and show database properties plus row and coverage counts. The `d` key opens the same database view in the TUI.
- `cat <path> [--at ISO]` and `diff <path>`: rebuild a file as Claude edited it, at any point in time, and list its edits oldest first. `ingest` also reads `~/.claude/file-history` snapshots as a second source of content, which raises full-content coverage from about 39% to about 96% of patch-only paths. `diff --all-sources` includes those snapshots, and `ingest --file-history-only` runs only that pass.
- `ingest --detach`: starts one background ingest and returns at once, so a Claude Code `Stop` hook can keep the store fresh. Only one runs at a time, and its output goes to `<store>.ingest.log`.
- `ingest --compact`, `prune-raw` and `reset-db`: `--compact` packs a store that grew to about twice its size, `prune-raw` drops the stored copy of every transcript line, and `reset-db` deletes the store (it is rebuilt from the transcripts on the next ingest).
- Tokens in the TUI: the `Enter` command view shows the output tokens of the request that made the call (with the model), and the `s` session view shows the session total (output, input, cache, requests, with subagent output called out).
- Command view: shows the first lines of the prompt that led to the command, and `Enter` opens the full prompt in a scrollable view.
- `t`: opens a pretty-printed copy of the selected session's transcript. `o` now opens pretty-printed JSON blocks instead of one raw line per command.
- `.env` keys `CLAUDE_TRAIL_COL_TIME_WIDTH`, `_SESSION_WIDTH`, `_AGENT_WIDTH`, `_DIRECTORY_WIDTH` and `_FILES_WIDTH`: set the Commands tab column widths without editing code.
- Session view (`s`): subagents are listed in the order they started, their names are tinted like the columns, the selected subagent's full name is shown, and the list scrolls. The Session column is one character narrower (14 to 13).

### Bug fixes

- `cat` could return a file one turn stale when its last edit was in a session's final turn (34 of 813 paths, behind by 1 to 12 edits). It now applies the newer edits on top, and warns `stale by N edit(s)` on stderr if one no longer applies.
- `ingest --detach`: a hung or crashing ingest used to leave no trace (output went to `/dev/null`). It now writes to `<store>.ingest.log`. It also no longer leaves zombie processes, and on a system without `fork` it exits 0 instead of reporting an error on every turn.
- A busy store no longer ends in a traceback. If the TUI holds the store, `ingest` logs `skipped: store is held by a reader` and exits 0. If an ingest holds it, `search`, `cat` and `stats` print `store busy: an ingest is writing ...` and exit 1.
- The Tokens row in the command and session views was missing when the store lagged behind the live feed, which looked broken. It now shows a hint: `pending ingest` or `no audit store yet - run: claude-trail ingest`.

### Performance

- The store no longer keeps a copy of every transcript line, only the lines that cannot be rebuilt. This removed about two thirds of a real store (about 655 MB).
- `ingest --compact` on a 1.9 GB store gave 949 MB, with the same rows.
- The closing flush of `ingest` runs at most once every 300 s. A one-shot ingest on a 1 GB store took 6.0 s, of which 3.5 s was the flush. At a 30 s cadence that would have locked the store about 20% of the time.

### Upgrade notes

- Reinstall to pick up the new `duckdb` dependency (`duckdb>=1.0`).
- The audit store is optional and starts empty. Run `claude-trail ingest` once, then keep it fresh with a `Stop` hook that runs `claude-trail ingest --detach` (see Audit store in the README).
- A store made by 0.6.0 is upgraded in place on the next `ingest`. On an existing store, run `claude-trail prune-raw` once to drop the old transcript copies and get the smaller size.
- Do not leave `ingest --watch` running next to the TUI. It holds the store's write lock, so the TUI cannot read the store and the Tokens rows and the `d` view stay empty.

## 0.5.2 (2026-07-07)

_Covers 0.5.0 to 0.5.2._

### Features

- Agent column: each label now starts with a short id (4 characters) of the subagent, so two subagents with the same description can be told apart. The `s` session view starts its Subagent cell with the same id.
- Session column: narrower, 20 to 14 characters.

### Bug fixes

- Directory column: it cut the end of a long folder name (`claude-trail` showed as `claude-tra`). It now keeps the end of the path (`…/claude-trail`).

## 0.4.0 (2026-07-05)

### Features

- `Agent` column (key `6`): a tree gutter next to each row shows which subagent ran the command. The agent label is shown once per run of commands.
- `s`: opens a session view with the session id, name and status (tinted by its `/color`), folder, transcript path, version and start time, plus a list of its subagents with status (running, done, stopped), description, type and tool-call count.
- `/`: searches the selected row's session recursively with `rg` (or `grep`). `Tab` switches the search root between the transcript folder and the session's working folder, and `Enter` runs the query. In the results, `j` and `k` browse, `Enter` opens a hit at its line, and `f` opens its folder. Results are capped at 500 hits and 5 s.

### Upgrade notes

- The hook now records `agent_id` and `agent_type` for commands run by a subagent. Reinstall so the installed `claude-trail hook` has this change. Attribution in the feed covers commands logged after the upgrade only, because older log lines have no agent id and show as the main agent. The `s` view reads transcripts, so it works for old sessions too.

## 0.3.7 (2026-07-04)

_Covers 0.3.0, 0.3.2 to 0.3.5 and 0.3.7._

### Features

- `Enter`: opens a full-command view with the whole command (newlines kept), the session details and the danger marker. `Esc`, `q` or `Enter` closes it.
- Panel title: shows the installed version (`claude-trail vX.Y.Z`).
- `$CLAUDE_CONFIG_DIR`: when set, the log, sessions and projects folders are read from it instead of `~/.claude`.
- Feed buffer: raised from the last 50 commands to the last 1,000.

### Bug fixes

- Arrow keys were sometimes dropped, and queued keys stalled. Arrow keys now work every time, also in terminals that use application-cursor mode.
- Modified arrows, function keys and terminal replies leaked into the keys the TUI saw: `Ctrl-Up` toggled a column, `F5` toggled a column, a terminal reply cleared the display, and the extra `Esc` closed the full-command view. They are now read as one key, and modified arrows move the cursor.
- `o` with a terminal editor (`nvim`, `vim`) crashed the TUI. The editor now runs in the foreground and the TUI comes back when you quit it.
- The terminal was left in no-echo mode after `kill`, `SIGHUP` or `Ctrl-\`. It is now restored, and the exit codes are 143, 129 and 131.
- Danger marker false alarms: `2>/dev/null`, writes to `/dev/stderr`, `/dev/tty` and `/proc/self/fd/N`, and quoted text such as `git commit -m "route logs > /var/log"` are no longer flagged. A spaced write such as `> /etc/hosts` is now flagged.
- Danger marker for services: `systemctl status`, `show`, `cat`, `list-*` and `is-*` are no longer flagged, and neither are `docker service ls` and `kubectl get service`. Changing forms are still flagged, including with flags first (`systemctl --user restart foo`) and `service nginx restart`.

### Performance

- Cursor moves show on screen at once. Before, the screen could lag by up to about 250 ms behind the cursor. Held and fast keys are read together and drawn once per frame.

### Upgrade notes

- `Enter` no longer opens the session's commands. That action moved to `o`.

## 0.2.3 (2026-05-23)

_Covers 0.1.0 and 0.2.0 to 0.2.3 (first releases)._

### Features

- Live feed: a Rich TUI that tails `~/.claude/command-log.jsonl` and shows every Bash command Claude Code runs, across all sessions, with Time, Session, Directory, Files and Command columns. `1` to `5` toggle columns and `c` clears the display.
- Navigation: `j` and `k` (or the arrow keys) scroll through the whole buffer of 50 commands, `g` and `G` jump to the ends, and the title shows the cursor position (for example `5/50`). New commands keep the selected row in place while you are scrolled.
- Danger marker: a red `*` flags risky commands such as `rm`, `sudo` and `git reset`. The status bar shows how many sessions were active in the last 5 minutes.
- `claude-trail hook`: the `PostToolUse` hook is part of the program, so settings only need `claude-trail hook` and no script path. Install with `pipx install` or `uv tool install` from the git URL.
- Timestamps use your local time zone (ISO 8601 with offset). Old UTC entries in the log are shown in local time too.
- Session column: shows the session name from Claude Code's session files, and falls back to the id. Names are remembered after Claude Code removes the file when a session ends. Rows are tinted with the session's `/color` choice (`orange`, `pink` and `gray` are mapped to terminal colors).
- `Enter`: opens the selected session's commands in `$VISUAL`, else `xdg-open` (Linux) or `open` (macOS). `f` opens the file manager on the folder of the files named in the command.
- macOS support.

### Bug fixes

- A line the hook was still writing could be read half way. The feed now waits for the full line.
- `git push` and `tee` are no longer flagged as dangerous.

### Upgrade notes

- The project was renamed from `bash-feed` to `claude-trail`. The command, the package and the temp file names changed. Register the hook as `claude-trail hook`. The old `hook.sh` stays in the repo as a deprecated fallback.
