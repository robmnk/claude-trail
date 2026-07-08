# Phase 7 review: Docs + dependency note (+ cmd_diff pytz fix)

Verdict: changes-requested
Diff: 9e95132 (main)...9ebc747 (phase-7-docs), 6 files, +87/-3

## Blockers

none

## Major

- `pyproject.toml:21` - the wheel target `include = ["claude_trail.py"]` omits `claude_trail_store.py`, so the built wheel ships without the store module. Verified: built the 0.6.0 wheel from this branch; it contains only `claude_trail.py` (the currently installed 0.5.2 tool likewise has no store module and no duckdb). Every store subcommand via the installed entry point (`claude-trail ingest` etc.) dies with `ModuleNotFoundError: claude_trail_store` at `claude_trail.py:1888`. This phase's docs (`CLAUDE.md` "Invoked as `claude-trail <cmd>` (installed)", `README.md` audit-store code block) document exactly that broken path as the primary invocation, and Step 7.4 mandates a reinstall that would ship the broken wheel. Fix: `include = ["claude_trail.py", "claude_trail_store.py"]`, then reinstall per Step 7.4.

## Minor

none

## Nits / follow-ups

- `claude_trail_store.py:795,818` - `cmd_cat`/`cmd_diff` raise IndexError when invoked with no path argument (`args[0]`); `cmd_search` handles the missing-arg case with a usage line. Pre-existing, not introduced here; align later.
- `README.md:120` - the condensed bracket form `[--tool T --session S --project P --since ISO --limit N -i]` reads as one grouped option; CLAUDE.md's per-flag form is clearer. Cosmetic.

## Notes

**cmd_diff pytz fix (verified working).**
- `claude_trail_store.py:826` now selects `strftime(ts,'%Y-%m-%d %H:%M:%S')` instead of raw `ts`, so DuckDB never materializes a TIMESTAMPTZ into Python. Proven on the real store: with `sys.modules['pytz']=None` against `/tmp/trail-p5.duckdb`, `cmd_diff` returned 0 and rendered headers plus patches.
- Grepped all reader paths for remaining raw-TIMESTAMPTZ fetches: `cmd_search` (strftime), `cmd_tokens` (strftime in all five `--by` branches), `cmd_stats` (counts only), `cmd_cat` (blob content only), `cmd_diff` (now strftime). The raw `ts` columns in the `actions`/`command_tokens`/`request_context` view DDL are SQL-side only; no CLI fetches them raw. No other pytz-dependent path remains.
- Regression test `tests/test_store.py:1044` genuinely blocks pytz (`monkeypatch.setitem(sys.modules, "pytz", None)` makes `import pytz` raise), runs `cmd_diff` over a store with a real Edit, asserts rc 0, the rendered header (sid prefix), and the patch content. Mirrors the phase-4 tokens guard. Meaningful, not trivial.

**Doc accuracy (core of the phase) - cross-checked every claim against code.**
- Subcommand/flag grammar in both CLAUDE.md and README matches `claude_trail_store.py` exactly: `ingest [--watch] [--compact]` (649-655), `search <regex> [--tool --session --project --since --limit -i]` (691-706), `stats` no flags (768), `tokens [--session --by session|command|tool|day|model --since --limit]` (719-727), `cat <path> [--session --at]` (790-800), `diff <path> [--session]` (815-822). No phantom flags, no missing flags.
- Behavior claims verified: `--compact` ignored under `--watch` (653-655); 5s poll interval (656); `tokens` default `--by` flips to `command` with `--session` (726); `-i` = `(?i)` prefix (700); `diff` oldest-first (`ORDER BY ts`, 827); store path `~/.claude/trail.duckdb` / `$CLAUDE_TRAIL_DB` via `STORE_PATH` (14); lazy import gate in `claude_trail.py:1887-1888` matches the "TUI and hook never load duckdb" claim.
- Token accounting section is honest and matches: `api_requests` PK on `request_id` (65), synthetic skip (`model == "<synthetic>"`, 260), `command_tokens` even split via `calls_in_request`/`output_tokens_share` (122-123), `request_context` framed as read-back approximation with the negative-across-compaction caveat.
- Reconstruction section states the ~39% full-content coverage limit (figure matches the phase-5 measurement), names patch-only rows (null `post_sha`, matching `cmd_cat`'s JOIN on `post_sha`), and names the file-history backfill follow-up with the correct snapshot path shape from the plan.
- Single-writer / read-only-readers / hook-never-writes model and the transcripts-vs-command-log.jsonl distinction are both present and correct (Step 7.3 satisfied).

**Deps and version.** `requirements.txt` gains `duckdb>=1.0`, no pytz (correct, the store is now pytz-free); `pyproject.toml` bumps 0.5.2 -> 0.6.0 (minor bump per Step 7.4). `duckdb` was already in `[project] dependencies`.

**Style.** Scanned all six changed files for U+2014/U+2013: none.

**Gate.** `python -m pytest -q`: 329 passed in 3.3s.

**Scope.** Matches Task 7 (steps 7.1-7.3 delivered, 7.4 version bump done) plus the bundled cmd_diff fix; no scope creep. The reinstall half of Step 7.4 is exactly what the Major above blocks on.

**What's good.** The docs are unusually honest for docs: exact-vs-approximate token accounting is labeled per view, the 39% reconstruction gap is stated up front instead of hidden, and the pytz fix comes with a test that proves the failure mode rather than asserting trivially.
