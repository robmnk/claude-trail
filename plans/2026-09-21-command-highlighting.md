# Command highlighting: parse, format and color the Bash command

Make the Bash command readable to a human: a tokenizer that understands enough shell to
tell a command name from a flag, a path, a string, a variable, an operator, a redirect and
a heredoc; a formatter that breaks a long one-liner at its top-level operators; and one
palette that colors the result in the command view, the feed's Command column and the turn
page's Bash rows.

Scope: `claude_trail.py` (rendering only) + tests + docs. The hook, the store and the
ingest path are untouched. No new dependency: pygments is importable through rich, but
measured on a real command it colors only builtins (`cd`, `echo`) and keywords, and leaves
`git`, `head`, every flag, every path and every operator plain, so it is not used.

Decided with the user on 2026-09-21: reflow at operators (not color-only), and the colors
apply in the command view, the feed column and the turn page (not the command view alone).

## Measured shapes

66,685 logged commands in `~/.claude/command-log.jsonl` on 2026-09-21: median 186 chars,
p90 864, max 52,509. Pipe in 57%, `;` in 37%, more than one line in 28%, `&&` in 21%,
`$(` or a backtick in 10%, a heredoc in 9%, over 1,000 chars in 8%, `||` in 4%, a
`for`/`while`/`if` at the start in 2%. The tokenizer and the formatter are tested against
these shapes, and the tokenizer must stay linear: a 52 KB command has to tokenize in well
under a frame.

## Tokenizer: `tokenize_command(cmd) -> list[Token]`

`Token` is a frozen `(kind, text, start, end)`. LOSSLESS: the concatenation of every token's
`text` is `cmd`, byte for byte, so nothing rendered can differ from what ran. It is a display
heuristic, not a shell parser: it must never raise, and anything it does not understand is a
plain `word`.

Kinds and the rule that produces each:

| kind | rule |
|------|------|
| `command` | the first word of each simple command: at the start, after an `operator`, a newline, `(`, `{`, or a `keyword` that opens a body (`do`, `then`, `else`, `elif`, `if`, `while`, `until`); also the word after `sudo`, `nohup`, `time`, `exec`, `env`, `xargs` and after leading `assign` tokens |
| `keyword` | `if then else elif fi for while until do done case esac in function select` when in command position |
| `assign` | `NAME=value` in command position (`value` may be quoted); the `NAME=` part is the token, the value tokenizes as usual |
| `flag` | a word starting with `-` (`-x`, `--long`, `--key=value` is one flag token) |
| `path` | a word starting with `/`, `~`, `./`, `../`, or containing `/` without `://` (a URL is a `word`) |
| `string` | `'...'`, `"..."` (escapes honoured), `$'...'`; a double-quoted string is split so `$VAR`, `${...}` and `$(...)` inside it are `var`/`subst` tokens between `string` parts |
| `var` | `$NAME`, `${...}`, `$1`, `$?`, `$@`, `$#`, `$$` |
| `subst` | `$(` ... `)` with balanced parens, and backtick spans; the delimiters are `subst` tokens and the inside is tokenized as a command (recursively) |
| `heredoc_op` | `<<`, `<<-` plus the delimiter word (quoted or not); `<<<` is a `redirect` |
| `heredoc_body` | verbatim from the next line through the line before the terminator; the terminator line is `heredoc_end`. `<<-` strips leading tabs from the terminator match. Several heredocs on one line open in order. The rest of the opening line still tokenizes as a command |
| `operator` | `&&`, `\|\|`, `\|`, `\|&`, `;`, `;;`, `&`, `(`, `)`, `{`, `}` |
| `redirect` | `>`, `>>`, `<`, `<<<`, `>\|`, `&>`, `N>`, `N>>`, `N>&M`, `<&N` |
| `comment` | `#` at the start of a word through the end of the line (a `#` inside a word or a string is not a comment) |
| `space` | runs of spaces, tabs and newlines |
| `word` | anything else (arguments) |

Word boundaries follow the shell: a word ends at whitespace or at an operator/redirect
character outside quotes; a backslash escapes the next character, including a newline
(`\` + newline is `space`, a line continuation).

## Formatter: `format_command(cmd) -> str`

Breaks a one-liner at its top-level `&&`, `||`, `|`, `|&` and `;` so each stage reads on
its own line, with the operator leading an indented continuation line:

```
cd /home/naka/Projects/x
  && git status --short
  | head -5
  ; python3 - <<'PY'
print("hi")
PY
  && for f in *.py; do echo "$f"; done
```

Rules:

- Split only at depth 0: not inside `$( )`, `( )`, `{ }`, backticks, quotes, heredoc bodies,
  and not inside a compound statement (`for`/`while`/`until` ... `done`, `if` ... `fi`,
  `case` ... `esac`). A compound is one unit and stays on its line, as in the mock.
- The split replaces the whitespace run around the operator with `\n  ` + operator + ` `.
  Nothing else changes: `normalize_cmd(format_command(cmd)) == normalize_cmd(cmd)` for every
  command, and the token sequence minus `space` tokens is identical before and after.
- A newline the author wrote stays; a `;` at the end of a line, before a newline, or before
  nothing is left alone; `&` (background) never splits; a continuation line that already
  begins with the operator is not split again, so `format_command` is idempotent.
- Heredoc bodies are verbatim, at column 0, whatever the surrounding indent.
- Applied ONLY in the command view (`build_detail_panel`). The feed column and the turn page
  color without reflow.

## Highlighter: `highlight_command(cmd, reflow=False) -> Text`

Builds a `rich.text.Text` from the tokens (`format_command` first when `reflow=True`) with
one palette, `CMD_STYLES`, the single source of truth:

| kind | style |
|------|-------|
| `command` | `bold` |
| `keyword` | `bold blue` |
| `flag` | `cyan` |
| `path` | `blue` |
| `string` | `green` |
| `var`, `subst` delimiters, `assign` | `yellow` |
| `heredoc_op`, `heredoc_end` | `magenta` |
| `heredoc_body` | `dim` |
| `operator` | `bold magenta` |
| `redirect` | `magenta` |
| `comment` | `dim italic` |
| `word`, `space` | none |

`Text.append(text, style=...)` only, never markup, so a `[` in a command is safe (the
existing rule under `Column`). The danger marker stays: `_danger_prefixed` learns to take a
`Text` body (or a sibling that does), so the red `* ` still leads a dangerous command.

Where it renders:

- Command view (`build_detail_panel`): `highlight_command(cmd, reflow=True)` in the
  `command` panel, replacing the plain `cmd_text`.
- Feed Command column (`_render_command`): `highlight_command(normalize_cmd(cmd))`, cached
  per command string (`functools.lru_cache`, a few hundred entries, on a helper that returns
  the immutable `(plain, spans)`; the render builds a fresh `Text` each time, because Rich may
  mutate a cell during truncation and a cached `Text` would drift). The column keeps
  `no_wrap` + ellipsis; spans survive truncation. Measure the feed frame with 50 rows before
  and after (the phase 4 review measured ~10 ms per feed panel).
- Turn page (`_action_detail_cell`): rows whose tool is `Bash` get `highlight_command` on
  their label; other tools unchanged.

## Phases

- [ ] 1. Tokenizer + highlighter, applied in the command view (no reflow yet)
  - `Token`, `tokenize_command`, `CMD_STYLES`, `highlight_command(cmd)`, a `Text`-taking
    danger prefix, `build_detail_panel` uses it.
  - Tests: one case per token kind; the measured shapes (pipes, `;`, `&&`/`||`, quoted /
    unquoted / `<<-` heredocs and two heredocs on one line, `$( )` with nested parens,
    backticks, a double-quoted string holding `$var` and `$( )`, `\"` inside double quotes,
    `'` inside double quotes and `"` inside single, `#` as a comment vs inside a word and a
    string, `2>&1` and `&>`, `--key=value`, a URL that is not a path, `sudo rm -rf`, an
    `assign` before the command, a `\` line continuation, an unterminated quote and an
    unterminated heredoc (never raise, degrade to `word`/`string`)); losslessness
    (`"".join(t.text for t in tokens) == cmd`) over every case and over a generated
    combination of them; markup safety with `[/tmp/x]` and `[bold]`; performance (a 52 KB
    command with 500 pipes tokenizes under 50 ms); `build_detail_panel` renders the styled
    text and keeps the `* ` marker.
- [ ] 2. Formatter, and the command view reflows
  - `format_command`, `highlight_command(cmd, reflow=True)`, `build_detail_panel` passes
    `reflow=True`.
  - Carried from the phase 1 review: process substitution `<(a | b)` and `>(a)` tokenizes as
    a `redirect` followed by plain `operator` parens (168 real commands), so the formatter
    must count those parens as depth and never split at the `|` inside them.
  - Tests: a split at each depth-0 operator; no split inside quotes, a heredoc body, `$( )`,
    `( )`, `{ }`, `<( )`, backticks, and inside `for`/`while`/`if`/`case` compounds; an author's
    newline survives; a trailing `;` is left alone; `&` never splits; idempotence;
    `normalize_cmd` equality and token-sequence equality over every phase 1 case; the
    heredoc body stays at column 0 under an indented continuation; the rendered panel shows
    the continuation lines.
- [ ] 3. Feed column and turn page, cache, docs
  - `_render_command` and `_action_detail_cell` (Bash rows only) use the cached highlighter;
    the cache returns immutable data and the render builds a fresh `Text`.
  - Tests: the column cell carries the spans and the `* ` marker; truncation at a narrow
    width keeps the styles; a non-Bash action row is unchanged; the cache is hit on a repeated
    command and bounded; a 50-row feed panel renders within 1.5x the pre-change time
    (measure both in the test's own process, or pin the per-cell cost instead if timing is
    flaky).
  - Docs: CLAUDE.md `Conventions` (the tokenizer, the formatter, the palette, where each
    applies, the never-raises and lossless guarantees) and the `Columns` Command row; README
    feature bullet. The version bump is the user's; `pyproject.toml` stays untouched.

## Conventions for this run

Review docs are plan-scoped: `reviews/REVIEW-command-highlighting-phase-N.md`. Plain
English in docs and comments, no em dash or en dash anywhere. `pyproject.toml` is never
edited by the loop.

## Verify

Run the suite with the PROJECT VENV, never `python3` (no `duckdb` there):

```bash
cd /home/naka/Projects/personal/claude-trail
.venv/bin/python -m pytest tests/ -q
.venv/bin/python claude_trail.py    # Enter on a long command with &&, |, ; and a heredoc
```

Ruff baseline: `uvx --from 'ruff>=0.15,<0.16' ruff check claude_trail.py claude_trail_store.py`
reports 18 pre-existing findings (E701/E702 in the old CLI parsers); a phase adds none.

## Open decisions

None. Reflow and scope were decided with the user before the plan was written.
