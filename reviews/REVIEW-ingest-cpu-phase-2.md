# Ingest CPU phase 2 review: stop DuckDB's pandas probe in the ingest process

Verdict: clean
Diff: main...phase-2-pandas-probe (526efe9), 3 files, +108/-0

## Blockers

none

## Major

none

## Minor

- `tests/test_store.py:2922` and `tests/test_store.py:2952`: both new tests assume no `pandas` entry is in `sys.modules`. In a venv with pandas installed, DuckDB's probe imports it on the first parameterised query of any earlier test, `_block_missing_pandas` then short-circuits on `"pandas" in sys.modules`, and both tests fail (`seen == [<module 'pandas'>]`; reproduced with the `fake_pandas_loaded` plugin in the scratch dir). Test 1 already patches `find_spec` to be environment-independent, so finish that: add `monkeypatch.delitem(sys.modules, "pandas", raising=False)` at the top of each test (the autouse fixture still restores the original entry). Today's dev venv and the deployed uv tool env both lack pandas, so the suite is green.

## Nits / follow-ups

- `tests/test_store.py:2927`: the `find_spec` stand-in `lambda name, *a:` drops keyword arguments, so any caller of `importlib.util.find_spec(name, package=...)` during the ingest would get a TypeError. Use `lambda name, *a, **k: None if name == "pandas" else real_find_spec(name, *a, **k)`.
- `tests/test_store.py:46`: `_restore_pandas_entry` covers `tests/test_store.py` only. No other module runs a real `cmd_ingest` today (`tests/test_claude_trail.py` stubs `cli_main`), but a future one would leak the None entry; the failure would be loud (the "leaves an importable pandas alone" test would see `[None]`). Moving the fixture to `tests/conftest.py` would close it for every module.
- Plan's optional upstream report (DuckDB does not cache a failed optional import) is not done; it is marked optional in the plan, so no action for this phase.

## Notes

- Scope matches the phase spec, plan `plans/2026-10-19-ingest-cpu.md:112`: `_block_missing_pandas` (`claude_trail_store.py:1579`), one call in `cmd_ingest` before `connect_writer` (`claude_trail_store.py:1616`), the two planned tests, one CLAUDE.md sub-bullet. No scope creep; the optional upstream report is not done.
- Placement, every ingest path (end-to-end subprocesses with an isolated `HOME`, counting `pandas` lookups on `sys.meta_path`, `probes/e2e_modes.py`): branch makes 1 search in each of normal, `--file-history-only`, `--compact`, `--watch` and the `--detach` worker (that 1 is the `find_spec` check itself); main makes 6,762 in the same normal, `--compact`, `--watch` and detach runs. The `--detach` parent returns before the block and runs no query; the worker re-enters `cmd_ingest` without `--detach` and blocks. `compact_db` runs later in the same process, so the entry still holds.
- Reader paths: `_block_missing_pandas` has one call site. The TUI (`claude_trail.py`) never calls `cmd_ingest`; its modals and the Prompts tab open the store read-only through their own imports, and `search`/`cat`/`stats`/`tokens`/`prompts` dispatch to their own `cmd_*`.
- Side effects of `sys.modules["pandas"] = None`: `import pandas` raises `ModuleNotFoundError` (an `ImportError`), the same class as a missing package, and `importlib.util.find_spec("pandas")` returns None. The runtime deps (rich, pygments, markdown-it, mdurl) never mention pandas, and the store uses no pandas-backed DuckDB API (`.df()`, `fetchdf`, `register`). With the block, DuckDB probes no other module per call for str, int, float, None, bytes, bool and timestamp parameters (`probes/probe_param_types.py`). The dev venv and the deployed uv tool env (`~/.local/share/uv/tools/claude-trail`, DuckDB 1.5.6, Python 3.11) both lack pandas, so the block is active in production.
- DuckDB behaviour behind the docstring, re-measured (`probes/probe_counts.py`): 2 pandas probes per parameterised `execute`, 4 per `executemany` row, 0 per unparameterised `execute`; 1,000 SELECTs plus 1,000 rows went from 6,000 searches to 0 and from 0.76 s to 0.41 s here (docstring says 0.9 s to 0.45 s, same ratio). DuckDB does cache a successful import (`probes/probe_success_cache.py`), which confirms the reason test 2 stubs the store open.
- Tests: full suite 872 passed (`TMPDIR=$HOME/.cache/pytest-tmp .venv/bin/python -m pytest tests/ -q`). Against main's store module (`main-code/`), the block test fails on `seen == [None]`, and with that line removed it fails on `counter.hits == 0` (166 searches), so both assertions carry weight. The "leaves an importable pandas alone" test passes on main by design (it guards against an unconditional block). ruff: no new findings (21 before and after, all pre-existing).
- Fixture: `_restore_pandas_entry` restores correctly. The run of all of `tests/test_store.py` followed by `probes/test_zz_leak_check.py` leaves no `pandas` entry, and with the fixture disabled (`no-fixture/`) the leave-alone test and the leak check both fail after one older `cmd_ingest` test, so the fixture is load-bearing and order cannot leak state today. The 13 older `cmd_ingest` calls in `tests/test_store.py` (3 of them `--detach` with a stubbed fork) are all covered by it; `tests/test_claude_trail.py` runs no real ingest.
- CLAUDE.md sentence (`CLAUDE.md:331`) is accurate: placement, mechanism, the per-query and per-row probe, the uncached failure, and that readers keep the normal import all match the code and the measurements.
- Scratch: `/tmp/dev-orchestrate/phase-2-pandas-probe/review/` (`probes/` scripts with one-line headers, `main-code/` and `no-fixture/` mutation copies).
