# Phase 1 review: Tokenizer + highlighter, applied in the command view (no reflow yet)

Verdict: changes-requested
Diff: main...HEAD (4f48b62), 2 files, +1290/-6

## Blockers

none. Both guarantees hold under every probe I could build: 67,319 real commands, 100,000
fuzzed inputs and 15 deep-nesting shapes produced zero losses, zero coverage failures and
zero escaped exceptions (measurements under Notes).

## Major

- `tests/test_claude_trail.py:557` - `test_spans_are_exactly_the_palette_over_the_tokens`
  builds its `expected` list from `feed.CMD_STYLES`, so it is tautological and can never fail
  on a palette change. Mutation-verified: setting `path`, `flag`, `operator`, `redirect`,
  `var`, `subst`, `assign` or `comment` to `""`, or `keyword`/`string` to `"red"`, leaves all
  858 tests green. 10 of the 16 entries are unpinned (only `command`, `space`, `word`,
  `heredoc_op`, `heredoc_end`, `heredoc_body` are pinned to a literal, by
  `test_unstyled_kinds_carry_no_span` and `test_a_heredoc_is_painted_by_its_own_kinds`). The
  plan calls `CMD_STYLES` the single source of truth for three views and phase 3 ships it to
  two more, so a silent drift is cheap to introduce and invisible. Fix: one test asserting
  the whole dict literally against the plan's table, `assert feed.CMD_STYLES == {"command":
  "bold", "keyword": "bold blue", ...}`; keep `test_the_cases_cover_exactly_the_palette` for
  the key set. (The palette itself is correct today: all 16 entries match the plan's table
  character for character.)

## Minor

- `claude_trail.py:1417` - the comment says `_MAX_SUBST_DEPTH` is "Bounded so a pathological
  input cannot exhaust the stack", but the cap is only checked in `_scan_command_subst`
  (`claude_trail.py:1946`) and `_scan_backticks` (`claude_trail.py:1971`). The `$"` path
  recurses uncapped: `_scan_dollar_quoted` (1925) -> `_scan_double_quoted` -> `_scan_dollar`,
  3 frames per `$"`. Measured: 332 repeats of `$"` (664 chars) raises `RecursionError`, which
  the blanket `except Exception` (2024) turns into one `word` token. The behaviour is safe
  (lossless, the module keeps working afterwards, 0 of 171 real commands containing `$"` come
  near it), the comment is not. Fix: test `self.depth >= _MAX_SUBST_DEPTH` in
  `_scan_dollar_quoted` and push the whole span as one `string`, or amend the comment to say
  the cap covers `$( )` and backticks and that a deeper `$"` nest reaches the fallback.

- `claude_trail.py:2022` - the `_tokens_cover` self-check has no test. Deleting the
  `if _tokens_cover(scanner.out, cmd):` guard and returning `scanner.out` unconditionally
  leaves all 858 tests green, so the half of the fallback that the module comment advertises
  most loudly ("checks this before returning") is unverified. Fix: one test that patches
  `_CommandScanner.run` (or `_tokens_cover`) to produce a non-tiling scan and asserts
  `tokenize_command("ls -la") == [Token("word", "ls -la", 0, 6)]`. The `except` half IS
  exercised, by `'$"' * 500` in `BROKEN`, but only for losslessness, not for the token shape.

- `claude_trail.py:1878` (`_scan_dollar`) - arithmetic expansion is read as a command
  substitution wrapping a subshell. `$((1+2))` gives `subst $(`, `operator (`,
  `command 1+2`, `operator )`, `subst )`, so `1+2` renders bold like a command name; inside
  `$((x > 1))` the `>` renders magenta as a redirect and `x` bold, and `$((1<2))` produces a
  `redirect 1<`. The plan's rule is that anything the scanner does not understand degrades to
  `word`; this degrades to the wrong kind instead. 298 of 67,340 real commands (0.44%)
  contain `$((`. Fix: in `_scan_dollar`, when the text at `pos` starts with `$((`, scan to the
  matching `))` and push one `var` token, the same treatment `${...}` already gets. At
  minimum add a `CASES` entry pinning today's reading so phases 2 and 3 cannot change it
  silently.

- `tests/test_claude_trail.py:266` - the 52 KB timing assertion (`best < 0.050`) measures
  26.1 ms on this machine, a 1.9x margin, and phases 2 and 3 re-run it on every change. What
  it defends is linearity, and linearity is exact here (x2.00 per doubling across five
  shapes, table under Notes), so the absolute bound buys little and can flake on slower
  hardware. Fix: raise the bound to ~200 ms (still two orders below a quadratic scan), or
  assert the ratio between two sizes instead of a wall-clock number. The plan names 50 ms, so
  this is spec-compliant as written; the conductor decides.

- `claude_trail.py:1792` (`_emit_plain`, the wrapper rule) - I agree with the implementer's
  reading of `xargs -n1 echo`: the plan says "the word after `sudo`, `nohup`, `time`, `exec`,
  `env`, `xargs`", `-n1` is that word, and it classifies as a flag, which closes the slot.
  Measured cost: 135 of 67,340 real commands (0.20%) put a flag right after a wrapper, so
  `echo` / `kill` / `basename` render as plain words there. If the conductor wants it fixed,
  the rule is: while `expect_command` is still open, a word that classifies as `flag` keeps
  `_keep_command = True`, so the slot survives a flag run. Known counterexamples from the
  real log: `sudo -u root ls` would then bold `root` and `xargs -I {} bash -c` would bold
  `{}`, because those flags take a separate argument. My call is to leave it and pin the
  current reading in `CASES`, but it is a real 0.2% mis-highlight either way.

## Nits / follow-ups

- `claude_trail.py:1610` `_is_brace_operator` returns True when `{` or `}` is the last
  character of the command, so `echo }` paints `}` as a bold-magenta operator and `echo {`
  likewise. Cosmetic, end-of-input only.
- `claude_trail.py:1569` the `#` branch of `_step` runs right after a redirect, so
  `echo >#foo` reads `#foo` as a comment instead of a filename. Zero hits in the real log.
- `claude_trail.py:2054` `reflow` is a dead parameter until phase 2 and `reflow=True`
  silently does nothing. The plan declares this signature, the docstring says so and a test
  pins it, so this is in scope; just do not let anything start passing True before phase 2.
- Process substitution: `<(a)` degrades to `redirect <` + `operator (` + body + `operator )`,
  with the body still tokenized as a command. 168 of 67,340 real commands (0.25%). Agreed,
  fine for phase 1. Carry it to phase 2: those parens arrive as ordinary `operator` tokens,
  so the formatter's depth tracking will see them, but it must not split at a `|` inside
  `<( ... )`.
- `git log --format='%H %s'` splits as `flag --format=` + `string '%H %s'`, while the plan's
  table says `--key=value` is one flag token. The word-splice design makes that unavoidable
  when the value is quoted, and the test only pins the unquoted `--color=auto`. Worth one
  sentence in the phase 3 docs.
- Palette on a dark terminal (opinion, not a finding, the plan fixed these values): plain
  `blue` for `path` is the darkest entry and is the usual readability complaint on dark
  themes. `bright_blue` would be safer; `cyan` is taken by `flag`. `keyword`'s `bold blue` is
  fine, since bold brightens blue on most terminals. On a light terminal the whole table is
  readable, `dim` for `heredoc_body` being the weakest.
- `_word_first` and `_keep_command` are per-word scratch state living on the instance rather
  than threaded as arguments through `_emit_plain`. Not a bug, and `_scan_word` resets both,
  but it is the one place the scanner's state is not obvious from a single method.

## Notes

**Scope: clean, no phase 2 or 3 leak.** Two files, three hunks in `claude_trail.py`: the
`NamedTuple` import (line 42), `_danger_prefixed` widened to `str | Text` (334), the new block
(1336 to 2067) and the one `build_detail_panel` line (2406). `_render_command`,
`_action_detail_cell` (which still hands `_danger_prefixed` a `str` at 3249, so the widened
signature is backwards-compatible in situ), `format_command`, any `lru_cache`,
`claude_trail_store.py`, `pyproject.toml`, `CLAUDE.md` and `README.md` are untouched.
`_danger_prefixed` copies into a fresh `Text` (`append_text`), so the highlighted body is
never mutated, and a mutation that returns the caller's object instead is caught by
`test_danger_prefix_does_not_mutate_the_body`.

**Suite:** `.venv/bin/python -m pytest tests/ -q` -> 858 passed in 23.8s (796 before, +62).
Ruff with the project config: 18 findings on `claude_trail.py` + `claude_trail_store.py` on
both `main` and HEAD, so the phase adds none; the test file's single E741 is pre-existing at
`tests/test_claude_trail.py:2232`. `claude_trail.py` still compiles under 3.11 (the declared
floor is 3.10; `str | Text` in an annotation is runtime-legal from 3.10).

**Real data.** All 67,319 commands in `~/.claude/command-log.jsonl`, run through
`_CommandScanner` directly with NO fallback: 0 coverage failures, 0 exceptions. So the
fallback never fires on real input. Mean 0.104 ms, slowest 3.33 ms, the 52,509-char record
holder 0.39 ms (14 tokens, it is mostly one heredoc body). Every one of the 16 `CMD_STYLES`
kinds is produced on real data and no produced kind lacks a style, in both directions.
`build_detail_panel` re-tokenizes per frame at `POLL_INTERVAL` 0.3s, so the worst real
command costs ~1% of a frame; no cache needed in this phase.

**Adversarial.** 60,000 random concatenations of 80 token-table atoms (quotes, every operator
and redirect shape, heredoc openers, `$(`/backtick/`${`/`$"`, NUL, ESC, CRLF, non-ASCII) and
40,000 random strings over a 130-character alphabet, again against the raw scanner: 0
coverage failures, 0 exceptions, worst single input 0.54 ms. 15 deep-nesting shapes
(`$(`x2000, `${`x5000, backtick x5000, `(`x5000, `{ `x3000, `"`x5000, `'`x5000, `$'`x3000,
`<<E\n`x3000, `\`x5000, 200 KB of `a`): all lossless, only `'$"' * 3000` reaches the fallback
(RecursionError, see the Minor above).

**Linearity** (best of 3, `tokenize_command`):

| shape | size | time | ratio per doubling |
|---|---|---|---|
| `"a \| " * k` | 20k -> 160k | 41 -> 336 ms | x1.92, x2.11, x2.01 |
| `"("*n + ")"*n` | 2.5k -> 20k | 4.8 -> 38.8 ms | x1.99, x2.05, x2.00 |
| `"$("*n + ")"*n` | 3k -> 24k | 2.8 -> 23.4 ms | x2.03, x2.05, x2.02 |
| plan's stage x k | 8k -> 64k | 4.7 -> 37.7 ms | x1.96, x2.04, x2.00 |
| `cat <<E\nbody\nE\n` x k | 7.5k -> 60k | 5.9 -> 48.1 ms | x2.07, x1.93, x2.05 |
| many `<<E` on one line | 1.2k -> 6.8k | 0.7 -> 5.5 ms | x1.92, x2.02, x2.00 |

Strictly linear in every shape, including the two that could plausibly have gone quadratic
(nested parens re-scanned by `_find_close_paren`, and several heredocs draining one queue).
Token-dense input is ~2 us per character, which is why 80 KB of `a | ` takes 167 ms while the
52 KB record holder takes 0.39 ms; irrelevant here (one command per frame) but worth
remembering for phase 3's 50-row feed, where the cache the plan already calls for is what
bounds it.

**Token rules probed** (not read). Correct against the plan's table: `a;b` / `a|b` / `a&&b`
with no spaces, `2>/dev/null` (redirect `2>` then path), `>&2`, `2>&-`, `3<&0`, `&>`, `<<<` as
a redirect not a heredoc, `--` and `echo -- --not-a-flag` as flags, `#` after `;` and at line
start as a comment but not inside a word or a string, `case x in a) ;; esac`, `{ a; b; }`,
`( a && b )`, `x=$(a)`, `A[k]=v`, `A+=v`, `a=b=c` (only `a=` is the assign), `1=x` (a command,
not an assign), `${x:-default}`, `${x:-"a b"}`, `${a[${b}]}`, `"$(echo "nested \"q\"")"`, a
heredoc whose delimiter appears inside the body, a heredoc opened inside `$( )`,
`cat <<EOF | grep x`, `<<'EOF'` / `<<"EOF"` / `<<\EOF` / `<< EOF`, `<<-` tab stripping (and
the tabbed line staying body without the dash), a terminator with a trailing space correctly
NOT matching (bash agrees), two heredocs on one line, CRLF throughout (including a CRLF `<<-`
terminator), a 3-byte UTF-8 path, whitespace-only and empty input, `$1 $? $@ $# $$ $- $* $_`,
`$12` splitting as `$1` + `2`, `\` before a space staying in the word and `\` before a newline
becoming space. Known-degraded but lossless and defensible: `$(( ))` (Minor above),
`<( )`/`>( )` (nit), `for ((i=0;i<3;i++))`, `! cmd` (`!` takes the command slot; 0 occurrences
in the real log), `export FOO=1 cmd` (`export` is not a wrapper, so `FOO=1` is a word, which
is what the plan's table says).

**Rendering.** `build_detail_panel` at 80, 120 and 200 columns with a long multi-operator
one-liner, a heredoc, a dangerous command, `[/tmp/x]` / `[bold]` / `[/]`, a UTF-8 path and the
empty command: no raise, markup literal, newlines preserved, heredoc body dim and terminator
magenta, `(empty)` placeholder intact, and the bold-red `* ` marker leading the styled text.
Rich wraps the styled `Text` at the panel width and carries each span across the wrap, so a
token split by a wrap keeps its colour on both lines; nothing misleading.

**Mutation test** (scratch copy under `/tmp`, 26 mutations, full suite each). Caught (16):
offsets off by one, `--key=value` split in two, `\"` closing a double-quoted string, no
recursion into `$( )`, `_MAX_SUBST_DEPTH = 0`, `#` inside a word becoming a comment, `|`
matched before `||`, heredoc delimiter quotes not stripped, a URL classified as a path, the
blanket `except` narrowed, `Text.from_markup` instead of `append`, `space` given a style,
`command` recoloured, the danger marker dropped from the detail panel, and the danger prefix
mutating its caller's `Text`. Missed (10): the `_tokens_cover` guard, and the styles for
`path`, `flag`, `operator`, `redirect`, `keyword`, `string`, `var`, `subst`, `assign`,
`comment`. Those two gaps are the Major and the second Minor above; everything else the tests
defend well, and the `CASES`/`BROKEN` tuples feeding five sweeps at once (losslessness,
tiling, 4,500 generated pairs, never-raises, kind coverage in both directions) is a good
shape to keep for phases 2 and 3.
