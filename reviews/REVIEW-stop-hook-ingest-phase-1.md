# Stop-hook-ingest plan, Phase 1 review: `ingest --detach` (background one-shot ingest + singleton lock)

Verdict: clean
Diff: 8b7254b...HEAD (phase-1-ingest-detach, 9a7208e), 2 files, +164/-0

## Blockers
none

## Major
none

## Minor
- `claude_trail_store.py:1144` `except OSError` in `_acquire_ingest_lock` treats every OSError as "lock held": an environmental failure (ENOLCK on NFS, EPERM, etc) makes every future `--detach` silently return 0 and ingest never runs, with no signal anywhere. Fix: catch `BlockingIOError` (EWOULDBLOCK/EAGAIN, what a held `LOCK_NB` flock raises) and let other OSErrors propagate to the hook's stderr.
- `claude_trail_store.py:1195-1198` The child's `dup2(devnull, 0/1/2)` loop can clobber the lock fd if a std fd was closed at launch (e.g. cron `0<&-`): `os.open` in `_acquire_ingest_lock` returns the lowest free fd, so the lock fd could be 0-2; `dup2` onto it silently drops the flock mid-ingest (the parent already closed its copy). Same launch condition can make `devnull` itself 0-2, where the final `os.close(devnull)` closes a just-redirected std fd. Exotic precondition, but the fix is one line: after acquiring, bump the fd above 2 (`fd = fcntl.fcntl(fd, fcntl.F_DUPFD, 3)` + close the original), or skip `os.close(devnull)` when `devnull < 3`.
- `claude_trail_store.py:1174` `child_argv` construction (strip `--detach`/`--watch`, pass `--compact`/`--file-history-only`/`-v` through) is untestable without entering the child branch, and indeed no test covers it. Fix: extract a one-liner `_detach_child_argv(argv)` and assert `_detach_child_argv(["ingest", "--detach", "--watch", "--compact"]) == ["ingest", "--compact"]`.

## Nits / follow-ups
- `tests/test_store.py:1747` No test for the non-POSIX branch: `monkeypatch.delattr(os, "fork")` then assert `cmd_ingest(["ingest", "--detach"]) == 1` (and that `_acquire_ingest_lock` was never called, i.e. no lock file created). Two lines, pins spec item 4.
- `claude_trail_store.py:1161` The no-`os.fork` branch returns 1; from a PostToolUse hook a nonzero exit can surface as a hook error every turn on such a platform. Returning 0 (logged no-op) may be friendlier. Spec says "return cleanly", which this satisfies, so deferring is fine.
- `claude_trail_store.py:1193-1201` Child failures are fully silent: stderr and `_log` both land in /dev/null, so a crashing ingest (schema migration error, corrupt store) fails invisibly forever while the hook keeps reporting success. The devnull redirect is what the spec mandates; follow-up: point the child's fds 1/2 at `dirname(STORE_PATH)/ingest.log` instead of devnull.
- `claude_trail_store.py:1178` The parent never reaps the child (no `waitpid`). Fine for the CLI (the parent process exits milliseconds later and the child reparents to init), but if `cmd_ingest([... "--detach"])` is ever called from a long-lived process (the TUI), the child lingers as a zombie. Note for future callers; classic fix is double-fork with a `waitpid` on the first child.
- Docs: `CLAUDE.md`'s subcommand synopsis and the hook-setup section do not mention `--detach` yet; presumably a later phase (the diff was scoped to the two files), flagging so it is not forgotten.
- Context only, not a phase finding: `ruff check .` reports 21 pre-existing errors, all outside this diff (`cmd_tokens`/`_print_rows`, conftest, test_claude_trail); the phase diff itself is ruff-clean.

## Notes
- Scope match: exact. All 5 spec items are implemented, nothing extra: `--detach` one-shot with `--watch` stripped and other flags passed through; lock keyed to `dirname(STORE_PATH)/trail-ingest.lock` (not global, so `$CLAUDE_TRAIL_DB` stores are independent and STORE_PATH-patching tests self-isolate); held-lock skip returns 0 without forking; fork/setsid/dup2/`_exit` child; lazy `fcntl` import plus `hasattr(os, "fork")` guard; lock acquisition factored into `_acquire_ingest_lock`.
- The flock+fork reasoning is correct: `fork()` duplicates the fd onto the same open file description, flock is held by the description and releases only when all fds on it close, so the parent's `os.close(fd)` does not drop the child's lock. The child keeps `fd` open through `cmd_ingest` including the `--compact` rewrite (lock file is separate from the store file), releasing only at `os._exit`.
- No infinite fork: the child re-enters `cmd_ingest` with `--detach` stripped, so the `"--detach" in argv` dispatch at `claude_trail_store.py:1214` cannot recurse. `--detach --watch` degrades to one-shot as specified; `--detach --compact` composes (child runs compact after `con.close()`).
- Other child hygiene is right: `os._exit` on both paths so a forked child never runs atexit/pytest/buffer flushes; `except BaseException` also catches `SystemExit`/`KeyboardInterrupt`, appropriate here; fork happens before any `connect()`, so no DuckDB connection crosses the fork; stdout becomes /dev/null so `sys.stdout.isatty()` keeps the Rich bar off; the skip and flock-failure paths close their fds (no leak); `os.makedirs(..., exist_ok=True)` covers a missing store dir.
- Tests: the 3 new tests pin the required behavior (exclusive + reusable lock; no-fork skip returning 0 with nothing written; parent branch closing its fd, returning 0, ingesting nothing, and the lock being free after) and never fork for real (`os.fork` patched to record or to return a fake pid). Full suite passes: 69/69 with the project venv interpreter (`.venv/bin/python`, python 3.11; a system python fails only on missing `duckdb`, unrelated to the diff).
- Conventions: matches the file's comment-heavy style, `_snake` helper naming, no new dependencies (`fcntl` is stdlib, imported lazily inside the detach path so module import stays portable).
