# Contributing

Thanks for helping improve `claude-trail`. It is a small, single-file tool, so
the contribution loop is quick.

## Dev setup

Clone and install with the dev extras (editable install plus `pytest` and
`ruff`):

```bash
git clone https://github.com/robmnk/claude-trail
cd claude-trail
pip install -e '.[dev]'
```

## Running tests

```bash
pytest
```

The suite lives in `tests/` and runs against the module in place. It covers the
pure helpers, the Rich renderers, input decoding, file tailing, and the
`AppState` / `apply_key` state machine.

## Linting

```bash
ruff check .
```

Ruff config lives in `pyproject.toml` (`[tool.ruff]`: `line-length = 100`,
`target-version = "py310"`). Keep `ruff check .` at a clean exit.

## Design constraints

- Stay single-file. All code lives in `claude_trail.py`. The one-file install is
  an advertised feature, not an accident. Do not split it into a package.
- One runtime dependency: `rich`. Do not add runtime dependencies. Test and lint
  tools (`pytest`, `ruff`) belong under the `dev` optional-dependencies group,
  never under `dependencies`.
- Keep the hook path (`claude-trail hook`) cheap and terminal-free: it runs on
  every Bash tool call.

For a tour of the internals and the two extension points (adding a column, and
the `AppState` / `apply_key` state machine), see `ARCHITECTURE.md`.

## House style

- No em dash (U+2014) and no en dash (U+2013), anywhere: not in code, comments,
  docs, commit messages, or PR descriptions. Use a hyphen, comma, colon, or
  parentheses instead. Self-check before you commit.
- Follow the existing patterns. The broad-but-specific `except` tuples (for
  example `except (OSError, json.JSONDecodeError, ValueError)`) are deliberate:
  a TUI and a hook should stay resilient rather than crash on a malformed line.
- Section banners (`# ==== <name> ====`) mark the file's layout. Put new code in
  the right section, or add a banner if it is a genuinely new area.

## Pull requests

Before opening a PR, make sure:

- `pytest` is green.
- `ruff check .` exits clean.
- Behavior stays covered: if you change or add logic, add or update a test for
  it. If you fix a bug, add a test that fails without the fix.
- Docs stay accurate: if you change a key binding, a column, a path, or the log
  format, update `README.md`, `ARCHITECTURE.md`, and `CLAUDE.md` to match.

CI runs `ruff check .` and `pytest` on Python 3.10 and 3.12 for every push and
pull request; a PR needs both green.
