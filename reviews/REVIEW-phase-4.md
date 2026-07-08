# Phase 4 review: Search, stats, and tokens CLI

Verdict: changes-requested
Diff: main...HEAD (phase-4-cli), 2 files, +243/-7

## Blockers

- `claude_trail_store.py:556-559` - the `--by session` branch (the DEFAULT view of `claude-trail tokens`, the spec's headline "tokens per session" question) selects raw `min(ts), max(ts)` (TIMESTAMPTZ). DuckDB's Python client requires the optional `pytz` package to materialize a TIMESTAMPTZ into a Python datetime; without it the fetch raises `InvalidInputException: Required module 'pytz' failed to import`. Reproduced with pytz import-blocked: raw ts fetch fails, `strftime(min(ts),...)` succeeds. `pytz` is not declared anywhere (`pyproject.toml` deps are `rich, duckdb`; `requirements.txt` is `rich` only) and duckdb does not pull it in, so on a clean install `claude-trail tokens` crashes. The test gate passes only because pytz was pip-installed into the local pyenv, which masks the bug. Fix (recommended, option b): wrap the two columns as `strftime(min(ts),'%Y-%m-%d %H:%M')` / `strftime(max(ts),'%Y-%m-%d %H:%M')`. This removes the optional-dependency footgun entirely, matches what the command/day branches already do, and fixes the ugly wrapped raw-datetime rendering visible on the real store. Option (a), declaring `pytz` as a dependency, works but adds a dep for two display columns; do not prefer it. Verified the other branches are safe without pytz: `command` and `day` strftime their ts, `tool` and `model` select no ts, and `cmd_search`/`cmd_stats` never fetch a raw TIMESTAMPTZ.

## Major

none

## Minor

- `claude_trail_store.py:502` - `import re as _re` is dead code (no `_re.` reference anywhere in the module; the spec's snippet carried it but nothing uses it). Remove the import.
- `claude_trail_store.py:521-522` (and 546-547) - `opt()` does `args[args.index(name)+1]`, so a trailing flag with no value (`claude-trail search foo --tool`) raises IndexError instead of printing usage. Cheap fix: guard `args.index(name)+1 < len(args)` and fall back to the usage message. Spec-verbatim code, so fix-now is optional but the guard is two lines.

## Nits / follow-ups

- `claude_trail_store.py:627-629` - `cli_main` dict-dispatch raises KeyError on an unknown subcommand. Unreachable today because `claude_trail.py:1887` gates argv[0] to exactly the six keys, but a `.get` with a usage message would make the function safe standalone.
- `claude_trail_store.py:529,550` - `int(opt("--limit", ...))` tracebacks on non-numeric input. Cosmetic.
- Tests cover session/command/day/model but not `--by tool` (the `output_tokens_share` sum) or `-i` case-insensitivity. Worth adding when convenient.
- Follow-up for the pytz fix: add a regression test that blocks `pytz` via a `sys.meta_path` hook and asserts `cmd_tokens(["tokens"])` returns 0, so the raw-TIMESTAMPTZ footgun cannot come back while the dev env happens to have pytz installed.

## Notes

- Scope match: exact. The diff implements Step 4.1 verbatim (`_print_rows`, `cmd_search`, `cmd_tokens`, `cmd_stats`), adds the spec-sanctioned `cmd_cat`/`cmd_diff` Phase-6 stubs (spec line 753 explicitly allows "stub them to return 0"), and replaces the Phase-3 minimal `cli_main` with the six-way dispatch whose keys match the `claude_trail.py` gate. Only the 2 intended files touched; TUI and hook untouched. No em/en dashes in the diff.
- Correctness: `cmd_search` binds the pattern (and all filter values) as parameters, so no SQL injection through the pattern; `limit` is f-string-interpolated but coerced through `int()` first. `-i` prepends `(?i)` to all three regexp_matches params. Results are newest-first (`ORDER BY ts DESC`). All three cmd_* open `connect(read_only=True)` and close in `finally`.
- `cmd_tokens` `--by` default logic matches spec (`command` when `--session` else `session`); each headers list matches its branch's SELECT arity (7/5/3/4/4); `tool` sums `output_tokens_share` per the even-split model; unknown `--by` returns 2 with usage. `cmd_stats` includes the pinned `api requests / output tokens` row.
- Tests: hermetic and meaningful. `_build_cli_store` monkeypatches `PROJECTS_DIR` and `STORE_PATH` to tmp_path, ingests the shared synthetic fixture, and closes the writer before the read-only reopen; never touches the live corpus. Assertions check search match, non-match, and tool-filter exclusion; tokens per-session (50-token total) and per-command (`--session` -> command breakdown); by-day/by-model output; stats blocks; stub behavior. The by-day test's tz-stable `2026-06` assertion shows awareness of local-tz strftime. Gate: 315 passed.
- Smoke-tested all five `--by` branches plus `search` against the real read-only store at `/tmp/trail-p3.duckdb` (pytz present): all produce sensible output; the session branch's raw datetimes wrap badly, which the strftime fix also cleans up.
