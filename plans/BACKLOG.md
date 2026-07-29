# Backlog

Unscheduled work, newest first. Items graduate to a dated plan in this folder once designed.

## Row coloring should be per session, not per line

`build_table` sets `row_styles=["", "dim"]`, so the feed is zebra-striped by row parity and
the session `/color` tint reaches only the Session cell (`_render_session`). The stripe
carries no information and fights the one signal that does: which session a row belongs to.
With several sessions interleaved (the normal case) you cannot see a session's run at a
glance.

Wanted: the session's `/color` tints the whole row, and the parity stripe goes away.

Design notes for whoever picks this up:

- `Table.add_row(..., style=...)` already carries the per-row style used for the cursor
  highlight (`"on grey23"`), so a row style is the natural place: compose it as
  `f"{session_color} on grey23"` for the cursor row, `session_color` otherwise, so the
  highlight still wins.
- Per-column `style=` in `COLUMNS` currently overrides the row style for Time/Session/etc.
  Tinting the row means those fixed column colors either go away or become dim variants.
  Decide which columns keep their own color (Command probably should stay readable) before
  coding, because this is a visual-identity change, not a one-line patch.
- Sessions with no `/color` set have no tint. Falling back to a stable hash of the session id
  would give every session a color, at the cost of no longer matching what the user picked in
  Claude Code. Pick one rule and document it.
- Column `style` is deliberately not configurable via `.env` today (only widths are). Keep
  that.

## Files column shows things that are not files

`_render_files` -> `extract_files` -> `find_paths` runs `FILE_PATH_RE` over the raw command
text, which matches any `/...`, `~/...` or `./...` token and then shows its basename. Sampled
over the newest 800 log entries: 597 extracted tokens are not files, and 174 entries (22%)
extract nothing at all. Observed failure modes:

| Shown | Actually | From |
|-------|----------|------|
| `meses`, `bin`, `scratchpad` | a directory | `grep -rl ... /path/meses/`, `ls /usr/bin` |
| `*.json`, `*` | a glob, never a real path | `grep -h ... /path/*.json`, `find /proc/*` |
| `DIVERGENCIA` | a grep pattern inside a quoted python one-liner | `python3 -c "... '/DIVERGENCIA' ..."` |
| (blank) | `cat foo.py`, `git show HEAD:file` | bare filenames have no `/` prefix |

So the column is wrong in both directions, and `f` (open file's folder) inherits the same
mistakes since `open_file_folder` reuses `find_paths`.

Three ways out, to be chosen before implementation:

1. **Tighten the extractor.** Drop trailing-slash and glob tokens, require either a known
   extension or an `os.path.isfile` hit (cheap: <= 3 stats per row, only for visible rows),
   and add bare-filename matching for the common verbs (`cat`, `python3`, `vim`, `git show`).
   Keeps the column's meaning, fixes precision, still guesses.
2. **Pivot the column.** The feed is Bash-only, so the store's exact per-tool `file_path` is
   no help here. Replace Files with something the row actually knows: exit status / error
   from the store's `tool_result`, command duration, or the risk marker from the tiered-risk
   plan (`2026-07-14-command-risk-tiers.md`). Frees 20 columns of width for Command.
3. **Split the meaning.** Keep a tightened Files for real files and let the Directory column
   absorb directory arguments, which is what many of the false positives are.

Whatever wins, `find_paths` has two callers (`extract_files` for display, `open_file_folder`
for `f`) and they should keep sharing one definition.
