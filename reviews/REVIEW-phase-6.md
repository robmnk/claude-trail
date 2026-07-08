# Phase 6 review: Reconstruction CLI (cat / diff)

Verdict: clean
Diff: main...phase-6-cat-diff (b13207d), 2 files, +142/-14

## Blockers
none

## Major
none

## Minor
- `claude_trail_store.py:795` / `claude_trail_store.py:818` - bare `claude-trail cat` (no path) raises IndexError on `args[0]`, and `cat --session S` (path forgotten) silently uses `--session` as the path. `cmd_search` (line 694) prints usage and returns 2 for the same case. Fix: guard `args[0]` the same way (`args[0] if args and not args[0].startswith("-") else None`, print usage, return 2).

## Nits / follow-ups
- `claude_trail_store.py:796-800` - `opt()` is duplicated in both functions and each flag is resolved twice (`if opt(n): ... opt(n)`), three `args.index` scans per flag. Hoist a module-level `_opt(args, n)` and bind the value once (`s = opt("--session")`).
- `claude_trail_store.py:828` - `diff` prints the patch column verbatim, which for Edit events is the raw structuredPatch JSON (Phase 5 stores structuredPatch as JSON, difflib output only as fallback). Readable but dense; follow-up: render hunks as unified-diff lines in `cmd_diff`.
- `claude_trail_store.py:827` - `fetchall()` materializes every version of a path; iterating the cursor would stream. Irrelevant at current data sizes.
- `claude_trail_store.py:828` - the TIMESTAMPTZ header renders in local tz (e.g. `-03:00`). Fine, but a `strftime`-to-UTC would make output machine-independent.

## Notes
- Scope match: exact. Only `claude_trail_store.py` (cmd_cat/cmd_diff, replacing the Phase-4 stubs) and `tests/test_store.py` changed; ingester, search, tokens, TUI, hook untouched. Implementation follows the Task 6 spec code verbatim except one deliberate improvement: both `opt()` helpers add `args.index(n)+1 < len(args)`, fixing the spec's IndexError on a trailing valueless flag (verified: `cat p --session` and `cat p --at` return 1 cleanly instead of crashing).
- cmd_cat: parameterized SQL, inner JOIN on `post_sha` correctly skips patch-only versions (null post_sha), `ORDER BY fv.ts DESC LIMIT 1`, `print(row[0], end="")` is byte-faithful, no-row prints sentinel and returns 1, read-only connection closed in finally.
- cmd_diff: oldest-first `ORDER BY ts`, `patch or '(no patch)'`, 8-char session prefix, read-only, closed in finally. `cli_main` (line 836) routes all six subcommands to real functions.
- Tests: all five required scenarios present and hermetic (synthetic transcript under tmp_path, PROJECTS_DIR + STORE_PATH monkeypatched, writer closed before the read-only reopen, capsys): latest content, `--at` earlier version, unknown path returns 1, diff oldest-first with both headers, and patch-only (cat 1 / diff shows patch). Time assertions are tz-stable: `--at` binds a Z-suffixed ISO string against TIMESTAMPTZ (UTC-parsed regardless of machine tz) and content bytes are asserted, never rendered local timestamps.
- Gate: 328 passed in 2.95s with `~/.pyenv/versions/global/bin/python -m pytest -q`. Live spot-check on `/tmp/trail-p5.duckdb` (personal-fin README.md): cat prints latest content, diff lists 4 versions oldest-first with `(no patch)` on the create.
- No em/en dashes in the diff.
