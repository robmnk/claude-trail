import json
import os
import re
import sys
from dataclasses import fields
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_trail as feed
from rich.console import Console
from unittest.mock import patch


class TestIsDangerous:
    def test_rm_rf_root(self):
        assert feed.is_dangerous("rm -rf /") is True

    def test_sudo(self):
        assert feed.is_dangerous("sudo apt update") is True

    def test_git_reset_hard(self):
        assert feed.is_dangerous("git reset --hard HEAD") is True

    def test_chmod_777(self):
        assert feed.is_dangerous("chmod 777 file.sh") is True

    def test_curl_pipe_bash(self):
        assert feed.is_dangerous("curl https://example.com/install.sh | bash") is True

    def test_safe_ls(self):
        assert feed.is_dangerous("ls") is False

    def test_safe_cat(self):
        assert feed.is_dangerous("cat foo") is False

    def test_safe_echo(self):
        assert feed.is_dangerous("echo hi") is False

    def test_safe_git_status(self):
        assert feed.is_dangerous("git status") is False

    def test_git_push_no_longer_flagged(self):
        assert feed.is_dangerous("git push origin main") is False

    def test_tee_no_longer_flagged(self):
        assert feed.is_dangerous("echo hi | tee output.txt") is False

    def test_stderr_discard_not_flagged(self):
        assert feed.is_dangerous("cmd 2>/dev/null") is False

    def test_fd_dup_not_flagged(self):
        assert feed.is_dangerous("python x.py 1>/dev/null 2>&1") is False

    def test_write_to_absolute_path_flagged(self):
        assert feed.is_dangerous("echo hi > /etc/hosts") is True

    def test_stderr_redirect_to_real_file_flagged(self):
        assert feed.is_dangerous("run 2>/var/log/out") is True

    def test_systemctl_status_not_flagged(self):
        assert feed.is_dangerous("systemctl status nginx") is False

    def test_docker_service_ls_not_flagged(self):
        assert feed.is_dangerous("docker service ls") is False

    def test_kubectl_get_service_not_flagged(self):
        assert feed.is_dangerous("kubectl get service") is False

    def test_systemctl_restart_flagged(self):
        assert feed.is_dangerous("systemctl restart nginx") is True

    def test_systemctl_daemon_reload_flagged(self):
        assert feed.is_dangerous("systemctl daemon-reload") is True

    def test_systemctl_flag_before_verb_flagged(self):
        assert feed.is_dangerous("systemctl --user restart foo") is True

    def test_systemctl_now_enable_flagged(self):
        assert feed.is_dangerous("systemctl --now enable bar") is True

    def test_systemctl_poweroff_flagged(self):
        assert feed.is_dangerous("systemctl poweroff") is True

    def test_systemctl_unmask_flagged(self):
        assert feed.is_dangerous("systemctl unmask foo") is True

    def test_systemctl_try_restart_flagged(self):
        assert feed.is_dangerous("systemctl try-restart nginx") is True

    def test_systemctl_isolate_flagged(self):
        assert feed.is_dangerous("systemctl isolate rescue.target") is True

    def test_systemctl_list_units_not_flagged(self):
        assert feed.is_dangerous("systemctl list-units") is False

    def test_systemctl_is_active_not_flagged(self):
        assert feed.is_dangerous("systemctl is-active nginx") is False

    def test_service_restart_flagged(self):
        assert feed.is_dangerous("service nginx restart") is True

    def test_service_stop_flagged(self):
        assert feed.is_dangerous("service ssh stop") is True

    def test_service_status_not_flagged(self):
        assert feed.is_dangerous("service nginx status") is False

    def test_redirect_inside_quotes_not_flagged(self):
        assert feed.is_dangerous('git commit -m "route logs > /var/log"') is False

    def test_arrow_text_inside_quotes_not_flagged(self):
        assert feed.is_dangerous('echo "routes: api -> /var/www"') is False

    def test_unquoted_arrow_redirect_still_flagged(self):
        # unquoted, the shell parses `-> /path` as the word `-` plus a real redirect
        assert feed.is_dangerous("echo api -> /var/www") is True

    def test_dev_stderr_not_flagged(self):
        assert feed.is_dangerous("echo msg > /dev/stderr") is False

    def test_dev_tty_not_flagged(self):
        assert feed.is_dangerous("printf x > /dev/tty") is False

    def test_dev_stdout_not_flagged(self):
        assert feed.is_dangerous("echo x >/dev/stdout") is False

    def test_proc_self_fd_not_flagged(self):
        assert feed.is_dangerous("cmd 2>/proc/self/fd/2") is False

    def test_append_to_absolute_path_flagged(self):
        assert feed.is_dangerous("echo x >> /var/log/app.log") is True

    def test_append_discard_not_flagged(self):
        assert feed.is_dangerous("cmd 2>>/dev/null") is False


class TestNormalizeCmd:
    def test_collapses_runs_of_whitespace(self):
        assert feed.normalize_cmd("ls    -la     foo") == "ls -la foo"

    def test_preserves_single_spaces(self):
        assert feed.normalize_cmd("ls -la foo") == "ls -la foo"

    def test_collapses_tabs_and_newlines(self):
        assert feed.normalize_cmd("echo\thello\nworld") == "echo hello world"

    def test_strips_leading_trailing(self):
        assert feed.normalize_cmd("   ls   ") == "ls"


class TestTokenizeCommand:
    """`tokenize_command`: enough shell to color a command, plus two guarantees.

    LOSSLESS - the tokens concatenate back to the command and their offsets tile
    it end to end, so what a view colors can never differ from what ran.
    NEVER RAISES - an unterminated quote or heredoc, an unbalanced `$(`, a lone
    trailing backslash, a control character, an empty string and a 52 KB one-liner
    all come back as tokens. Both hold over `CASES` and over generated pairs of
    them, not only over the hand-written assertions below.
    """

    # One entry per rule in the plan's token table, plus the shapes the plan
    # measured in the real command log (pipe 57%, `;` 37%, `&&` 21%, `$(` or a
    # backtick 10%, a heredoc 9%). The kind-coverage, losslessness, offset and
    # never-raises sweeps all read this list, so a case added here is covered by
    # every one of them without touching them.
    CASES = (
        "ls -la",
        "git status --short | head -5",
        "cd /home/naka && ls || echo no; date",
        "sudo rm -rf /tmp/x",
        "FOO=bar BAZ=1 python run.py",
        'for f in *.py; do echo "$f"; done',
        "if [ -f x ]; then echo y; fi",
        "{ echo hi; } | ( cd /tmp && ls )",
        "make &> log.txt",
        "python x.py 1>/dev/null 2>&1",
        "cat <<< 'here string'",
        "grep --color=auto -n 'foo bar' bar.txt",
        "curl https://example.com/api?x=1 -o /tmp/out",
        "echo a#b  # a real comment",
        'echo "path $HOME/x ${FOO:-y} $(date)"',
        "echo $((1+2)) $((x > 1)) $(( (1+2) * 3 ))",
        "xargs -n1 echo hi",
        "echo `date` and $(echo $(date) (x))",
        'echo "he said \\"hi\\" and it\'s fine"',
        "echo 'a \" b'",
        "printf $'a\\nb' $1 $? $@ $# $$",
        "ls \\\n  -la",
        "cat <<'EOF'\nbody $x\nEOF",
        "cat <<-EOF\n\tbody\n\tEOF",
        "cat <<A <<B\na\nA\nb\nB",
        "tail -f /var/log/x | grep -v noise &",
        "echo 'unterminated",
        "cat <<EOF\nnever ends",
        "echo $(unbalanced",
        "echo trailing\\",
        "",
        "echo \x01\x02",
    )

    # The broken shapes the plan names, kept apart from CASES so the never-raises
    # sweep reads as the list of things that used to be able to kill a render.
    BROKEN = (
        "echo 'unterminated",
        'echo "unterminated',
        "cat <<EOF\nno terminator ever",
        "echo $(unbalanced",
        "echo ${unbalanced",
        "echo `unbalanced",
        "trailing backslash \\",
        "\\",
        "echo \x00\x01\x1b[2J\x7f",
        "",
        "$" * 500,
        '$"' * 500,          # deep nesting: capped, never raises
        "$(" * 500,
        "(" * 500 + ")" * 500,
        "<<EOF " * 200,
    )

    def _toks(self, cmd):
        """(kind, text) pairs, which is what most assertions below compare."""
        return [(t.kind, t.text) for t in feed.tokenize_command(cmd)]

    def _kinds_of(self, cmd, kind):
        return [t.text for t in feed.tokenize_command(cmd) if t.kind == kind]

    # ---- the two guarantees ----

    def test_every_case_round_trips_losslessly(self):
        for cmd in self.CASES:
            toks = feed.tokenize_command(cmd)
            assert "".join(t.text for t in toks) == cmd, cmd

    def test_offsets_tile_every_case_end_to_end(self):
        # start/end agree with the concatenation: no gap, no overlap, and each
        # token's text is the slice it claims.
        for cmd in self.CASES:
            pos = 0
            for tok in feed.tokenize_command(cmd):
                assert tok.start == pos, (cmd, tok)
                assert tok.end == pos + len(tok.text), (cmd, tok)
                assert cmd[tok.start:tok.end] == tok.text, (cmd, tok)
                pos = tok.end
            assert pos == len(cmd), cmd

    def test_lossless_over_generated_combinations(self):
        # Every pair of cases joined by every operator: a heredoc followed by a
        # pipeline, a comment swallowing what comes after it, a quote left open
        # in front of a `$( )`. Combinations, not the cases alone, are where a
        # scanner loses a character.
        for joiner in (" && ", " | ", "; ", "\n", " || "):
            for left in self.CASES:
                for right in self.CASES:
                    cmd = left + joiner + right
                    toks = feed.tokenize_command(cmd)
                    assert "".join(t.text for t in toks) == cmd, cmd

    def test_never_raises_on_broken_input(self):
        for cmd in self.BROKEN:
            toks = feed.tokenize_command(cmd)          # a raise here IS the bug
            assert "".join(t.text for t in toks) == cmd, cmd

    def test_empty_command_is_no_tokens(self):
        assert feed.tokenize_command("") == []

    def test_a_52kb_command_tokenizes_in_well_under_a_frame(self):
        # The longest command in the real log is 52,509 chars, and the feed
        # re-tokenizes per frame. The bound is 200 ms rather than the plan's
        # 50 ms: it is a BACKSTOP against a scan that is not linear (which takes
        # seconds here), not a benchmark, and 50 ms against a measured ~26 ms on
        # this machine is a 1.9x margin that a slower one can lose. What the
        # plan really asks for is the shape of the curve, and the linearity test
        # below is what pins that. Best-of-5 keeps a scheduling hiccup out.
        import time
        cmd, pipes = self._big_command()
        assert len(cmd) >= 52_000
        assert cmd.count(" | ") == pipes
        best = 1e9
        for _ in range(5):
            t0 = time.perf_counter()
            toks = feed.tokenize_command(cmd)
            best = min(best, time.perf_counter() - t0)
        assert "".join(t.text for t in toks) == cmd
        assert best < 0.200, f"{best * 1000:.1f} ms for {len(cmd)} chars"

    def test_tokenizing_is_linear_in_the_length_of_the_command(self):
        # Doubling the input at most doubles the work, so twice the size must
        # cost well under 3x the time (the slack absorbs machine noise; a
        # quadratic scan costs 4x and a 52 KB command seconds). Two shapes,
        # because they stress different loops: a pipeline is token-dense, while
        # nested `$( )` is what `_find_close_paren` re-reads.
        import time
        for name, build, small_k in (("pipes", self._piped_command, 200),
                                     ("nested $( )", self._nested_subst, 3000)):
            small, large = build(small_k), build(small_k * 2)
            assert len(large) > 1.9 * len(small)
            best_small = best_large = 1e9
            for _ in range(5):     # interleaved, so a slow moment hits both
                t0 = time.perf_counter()
                feed.tokenize_command(small)
                best_small = min(best_small, time.perf_counter() - t0)
                t0 = time.perf_counter()
                feed.tokenize_command(large)
                best_large = min(best_large, time.perf_counter() - t0)
            assert best_large < 3.0 * best_small, (
                f"{name}: {best_small * 1000:.1f} ms at {len(small)} chars, "
                f"{best_large * 1000:.1f} ms at {len(large)} chars")

    def _big_command(self, pipes=500):
        stage = ("grep -n 'foo bar' /tmp/some/path/file.txt --color=auto "
                 '"$HOME/x" && echo done')
        pad = max(0, (52_000 - (len(stage) + 3) * (pipes + 1)) // (pipes + 1))
        stage = stage + " " + "z" * pad
        return " | ".join(stage for _ in range(pipes + 1)), pipes

    def _piped_command(self, stages):
        stage = ("grep -n 'foo bar' /tmp/some/path/file.txt --color=auto "
                 '"$HOME/x" && echo done')
        return " | ".join(stage for _ in range(stages))

    def _nested_subst(self, levels):
        return "$(" * levels + "echo hi" + ")" * levels

    # ---- one case per token kind ----

    def test_the_cases_cover_exactly_the_palette(self):
        # Both directions: no case produces a kind the palette has no style for,
        # and no palette entry is unreachable (a typo in either shows up here).
        seen = set()
        for cmd in self.CASES:
            seen.update(t.kind for t in feed.tokenize_command(cmd))
        assert seen == set(feed.CMD_STYLES)

    def test_command_is_the_first_word(self):
        assert self._toks("ls -la")[0] == ("command", "ls")

    def test_command_restarts_after_an_operator_and_a_newline(self):
        assert self._kinds_of("cd /tmp && ls; date | wc -l", "command") == [
            "cd", "ls", "date", "wc"]
        assert self._kinds_of("ls\ndate", "command") == ["ls", "date"]
        assert self._kinds_of("( ls ) | { date; }", "command") == ["ls", "date"]

    def test_keyword_opens_a_body_so_the_next_word_is_a_command(self):
        kinds = self._toks("if true; then echo y; fi")
        assert ("keyword", "if") in kinds
        assert ("command", "true") in kinds
        assert ("keyword", "then") in kinds
        assert ("command", "echo") in kinds
        assert ("keyword", "fi") in kinds

    def test_keyword_only_in_command_position(self):
        # `done` as an argument is an argument, not the loop terminator.
        assert ("word", "done") in self._toks("echo done")
        assert ("keyword", "done") in self._toks("for f in x; do :; done")

    def test_in_is_a_keyword_only_in_a_for_word_list(self):
        assert ("keyword", "in") in self._toks("for f in *.py; do :; done")
        assert ("word", "in") in self._toks("grep in file.txt")

    def test_command_wrappers_pass_the_command_slot_on(self):
        for wrapper in ("sudo", "nohup", "time", "exec", "env", "xargs"):
            toks = self._toks(f"{wrapper} python run.py")
            assert toks[0] == ("command", wrapper)
            assert ("command", "python") in toks

    def test_a_flag_after_a_wrapper_closes_the_command_slot(self):
        # Pinned, not fixed. The plan says the word after `xargs` names a
        # command; here that word is `-n1`, which is a flag, and a flag ends the
        # command slot, so `echo` renders as a plain argument. 135 of the
        # 67,340 commands the phase 1 review measured (0.2%) read this way. The
        # alternative rule (a flag keeps the slot open) would bold `root` in
        # `sudo -u root ls` and `{}` in `xargs -I {} bash -c`, so the trade is
        # deliberate.
        toks = self._toks("xargs -n1 echo hi")
        assert toks[0] == ("command", "xargs")
        assert ("flag", "-n1") in toks
        assert ("word", "echo") in toks
        assert self._kinds_of("sudo -u root ls", "command") == ["sudo"]

    def test_assign_is_the_name_and_equals_and_keeps_the_slot_open(self):
        toks = self._toks("FOO=bar BAZ=1 python run.py")
        assert toks[0] == ("assign", "FOO=")
        assert toks[1] == ("word", "bar")
        assert ("assign", "BAZ=") in toks
        assert ("command", "python") in toks

    def test_assign_value_may_be_quoted(self):
        toks = self._toks('PATH="/usr/bin:$PATH" make')
        assert toks[0] == ("assign", "PATH=")
        assert ("string", '"/usr/bin:') in toks
        assert ("var", "$PATH") in toks
        assert ("command", "make") in toks

    def test_flag_and_key_value_flag(self):
        assert ("flag", "-la") in self._toks("ls -la")
        assert ("flag", "--short") in self._toks("git status --short")
        # `--key=value` is ONE flag token, not a flag plus an assignment.
        assert ("flag", "--color=auto") in self._toks("grep --color=auto x")

    def test_path_shapes(self):
        toks = self._toks("echo /tmp/x ~/y ./z ../w a/b")
        assert ("path", "/tmp/x") in toks
        assert ("path", "~/y") in toks
        assert ("path", "./z") in toks
        assert ("path", "../w") in toks
        assert ("path", "a/b") in toks

    def test_a_url_is_a_word_not_a_path(self):
        # It has slashes, but `://` says it is not a file.
        assert ("word", "https://example.com/api?x=1") in self._toks(
            "curl https://example.com/api?x=1")
        # A slashed word WITHOUT `://` is still a path, so the test above is
        # about the scheme and not about slashes in general.
        assert ("path", "github.com/o/r") in self._toks("echo github.com/o/r")

    def test_string_forms(self):
        assert ("string", "'a b'") in self._toks("echo 'a b'")
        assert ("string", '"a b"') in self._toks('echo "a b"')
        assert ("string", "$'a\\nb'") in self._toks("printf $'a\\nb'")

    def test_double_quoted_string_splits_around_its_expansions(self):
        assert self._toks('echo "path $HOME/x ${FOO:-y} $(date)"')[2:] == [
            ("string", '"path '),
            ("var", "$HOME"),
            ("string", "/x "),
            ("var", "${FOO:-y}"),
            ("string", " "),
            ("subst", "$("),
            ("command", "date"),
            ("subst", ")"),
            ("string", '"'),
        ]

    def test_escaped_quote_does_not_close_a_double_quoted_string(self):
        cmd = 'echo "he said \\"hi\\" ok"'
        assert ("string", '"he said \\"hi\\" ok"') in self._toks(cmd)

    def test_a_quote_inside_the_other_quote_is_plain_text(self):
        assert ("string", '"it\'s fine"') in self._toks('echo "it\'s fine"')
        assert ("string", "'a \" b'") in self._toks("echo 'a \" b'")

    def test_var_forms(self):
        toks = self._toks("echo $HOME ${FOO} $1 $? $@ $# $$")
        for text in ("$HOME", "${FOO}", "$1", "$?", "$@", "$#", "$$"):
            assert ("var", text) in toks, text

    def test_subst_delimiters_are_tokens_and_the_inside_is_a_command(self):
        assert self._toks("echo $(date +%s)")[2:] == [
            ("subst", "$("), ("command", "date"), ("space", " "),
            ("word", "+%s"), ("subst", ")"),
        ]

    def test_subst_balances_nested_parens(self):
        # The span closes where the shell closes it, and the body is tokenized
        # recursively, so the inner `date` is a command too.
        toks = self._toks("echo $(echo $(date) (x)) tail")
        assert toks[-1] == ("word", "tail")
        assert toks.count(("subst", "$(")) == 2
        assert toks.count(("subst", ")")) == 2
        assert ("command", "date") in toks

    def test_arithmetic_expansion_is_one_var_token(self):
        # `$(( ))` is not a command, so it is NOT read as `$(` plus a subshell:
        # that painted `1+2` like a command name, the `>` like a redirect and
        # found a `1<` redirect in `$((1<2))`.
        assert self._toks("echo $((1+2))")[2:] == [("var", "$((1+2))")]
        assert self._toks("echo $((x > 1))")[2:] == [("var", "$((x > 1))")]
        assert self._toks("echo $((1<2))")[2:] == [("var", "$((1<2))")]
        # Inner parens are balanced, so the span ends at the right `))`.
        assert self._toks("echo $(( (1+2) * 3 )) tail")[2:] == [
            ("var", "$(( (1+2) * 3 ))"), ("space", " "), ("word", "tail")]
        assert self._toks("echo $(($((1+2)) * 3))")[2:] == [
            ("var", "$(($((1+2)) * 3))")]
        # A quoted one keeps its kind, like `${...}` does.
        assert ("var", "$((1+2))") in self._toks('echo "$((1+2))"')

    def test_a_command_substitution_around_a_subshell_is_still_a_subst(self):
        # `$( (a) )` has a space, so it is a substitution, not arithmetic.
        toks = self._toks("cat $( (a) )")
        assert ("subst", "$(") in toks
        assert ("command", "a") in toks
        assert self._kinds_of("cat $( (a) )", "var") == []

    def test_an_unterminated_arithmetic_expansion_degrades_and_never_raises(self):
        # No `))` arrives, so it cannot be told apart from an unbalanced `$(`
        # and is scanned as one: the span stays open, lossless, no raise.
        for cmd in ("echo $((1+2", "$((", "$(()", "echo $(( (1+2)"):
            toks = feed.tokenize_command(cmd)
            assert "".join(t.text for t in toks) == cmd, cmd
            assert self._kinds_of(cmd, "var") == [], cmd

    def test_backticks_are_a_subst_too(self):
        assert self._toks("echo `date` tail")[2:] == [
            ("subst", "`"), ("command", "date"), ("subst", "`"),
            ("space", " "), ("word", "tail"),
        ]

    def test_operator_forms(self):
        assert ("operator", "&&") in self._toks("a && b")
        assert ("operator", "||") in self._toks("a || b")
        assert ("operator", "|") in self._toks("a | b")
        assert ("operator", "|&") in self._toks("a |& b")
        assert ("operator", ";") in self._toks("a; b")
        assert ("operator", ";;") in self._toks("case x in y) a ;; esac")
        assert ("operator", "&") in self._toks("a &")
        assert ("operator", "(") in self._toks("( a )")
        assert ("operator", ")") in self._toks("( a )")
        assert ("operator", "{") in self._toks("{ a; }")
        assert ("operator", "}") in self._toks("{ a; }")

    def test_brace_expansion_stays_inside_its_word(self):
        # `{` groups commands only when a space follows it.
        assert ("word", "x{a,b}") in self._toks("ls x{a,b}")

    def test_redirect_forms(self):
        assert ("redirect", ">") in self._toks("a > out")
        assert ("redirect", ">>") in self._toks("a >> out")
        assert ("redirect", "<") in self._toks("a < in")
        assert ("redirect", "<<<") in self._toks("a <<< 'x'")
        assert ("redirect", ">|") in self._toks("a >| out")
        assert ("redirect", "&>") in self._toks("a &> out")
        assert ("redirect", "1>") in self._toks("a 1>/dev/null")
        assert ("redirect", "2>&1") in self._toks("a 1>/dev/null 2>&1")
        assert ("redirect", "3<&0") in self._toks("exec 3<&0")

    def test_a_redirect_target_never_takes_the_command_slot(self):
        # `> /tmp/out ls` still runs `ls`.
        toks = self._toks("> /tmp/out ls")
        assert ("path", "/tmp/out") in toks
        assert ("command", "ls") in toks

    def test_comment_runs_to_the_end_of_its_line(self):
        toks = self._toks("ls  # look here\ndate")
        assert ("comment", "# look here") in toks
        assert ("command", "date") in toks

    def test_a_hash_inside_a_word_or_a_string_is_not_a_comment(self):
        assert ("word", "a#b") in self._toks("echo a#b")
        assert ("string", '"# not a comment"') in self._toks(
            'echo "# not a comment"')
        assert self._kinds_of("echo a#b", "comment") == []

    def test_space_covers_runs_and_a_line_continuation(self):
        # `\` + newline is whitespace (the shell joins the lines), so it lands
        # in the space token rather than in the word beside it.
        assert self._toks("ls \\\n  -la") == [
            ("command", "ls"), ("space", " \\\n  "), ("flag", "-la")]
        assert ("space", "  \n\t") in self._toks("ls  \n\tdate")

    def test_word_is_everything_else(self):
        assert ("word", "status") in self._toks("git status")
        assert ("word", "*.py") in self._toks("ls *.py")

    # ---- heredocs ----

    def test_quoted_heredoc_body_is_verbatim_through_its_terminator(self):
        assert self._toks("cat <<'EOF'\nbody $x\nEOF") == [
            ("command", "cat"), ("space", " "), ("heredoc_op", "<<'EOF'"),
            ("space", "\n"), ("heredoc_body", "body $x\n"),
            ("heredoc_end", "EOF"),
        ]

    def test_double_quoted_and_bare_heredoc_delimiters(self):
        for opener in ('<<"EOF"', "<<EOF"):
            toks = self._toks(f"cat {opener}\nbody\nEOF")
            assert ("heredoc_op", opener) in toks
            assert ("heredoc_body", "body\n") in toks
            assert ("heredoc_end", "EOF") in toks

    def test_dash_heredoc_allows_leading_tabs_before_the_terminator(self):
        assert self._toks("cat <<-EOF\n\tbody\n\tEOF") == [
            ("command", "cat"), ("space", " "), ("heredoc_op", "<<-EOF"),
            ("space", "\n"), ("heredoc_body", "\tbody\n"),
            ("heredoc_end", "\tEOF"),
        ]
        # Without the dash the tabbed line is body, not the terminator.
        assert self._kinds_of("cat <<EOF\n\tbody\n\tEOF", "heredoc_end") == []

    def test_two_heredocs_on_one_line_open_in_order(self):
        toks = self._toks("cat <<A <<B\na\nA\nb\nB")
        assert [t for t in toks if t[0].startswith("heredoc")] == [
            ("heredoc_op", "<<A"), ("heredoc_op", "<<B"),
            ("heredoc_body", "a\n"), ("heredoc_end", "A"),
            ("heredoc_body", "b\n"), ("heredoc_end", "B"),
        ]

    def test_the_rest_of_the_opening_line_still_tokenizes_as_a_command(self):
        toks = self._toks("python - <<'PY' > /tmp/out 2>&1\nprint(1)\nPY")
        assert ("redirect", ">") in toks
        assert ("path", "/tmp/out") in toks
        assert ("redirect", "2>&1") in toks
        assert ("heredoc_body", "print(1)\n") in toks

    def test_an_unterminated_heredoc_swallows_the_rest_as_body(self):
        toks = self._toks("cat <<EOF\nnever ends")
        assert ("heredoc_body", "never ends") in toks
        assert self._kinds_of("cat <<EOF\nnever ends", "heredoc_end") == []

    def test_a_here_string_is_a_redirect_not_a_heredoc(self):
        assert self._kinds_of("cat <<< 'x'", "heredoc_op") == []
        assert ("redirect", "<<<") in self._toks("cat <<< 'x'")

    # ---- degraded shapes ----

    def test_an_unterminated_quote_degrades_to_a_string(self):
        assert ("string", "'unterminated") in self._toks("echo 'unterminated")
        assert ("string", '"unterminated') in self._toks('echo "unterminated')

    def test_an_unbalanced_subst_stays_open(self):
        toks = self._toks("echo $(unbalanced")
        assert ("subst", "$(") in toks
        assert toks.count(("subst", ")")) == 0

    def test_a_lone_trailing_backslash_is_ordinary_text(self):
        assert self._toks("echo trailing\\")[-1] == ("word", "trailing\\")

    def test_control_characters_are_carried_through(self):
        assert ("word", "\x01\x02") in self._toks("echo \x01\x02")

    def test_a_deep_dollar_quote_chain_is_capped_not_left_to_the_fallback(self):
        # `$"` reopens a double-quoted string, which can hold another `$"`:
        # 3 stack frames per repeat, within ONE scanner, so nothing bounded it
        # and 332 repeats raised RecursionError. The blanket `except` caught
        # that and returned one `word`, which is lossless but uncolored. The
        # `_quote_depth` cap means a real scan comes back instead.
        cmd = '$"' * 2000
        scanner = feed._CommandScanner(cmd, 0, len(cmd), 0)
        scanner.run()                  # a RecursionError here IS the bug
        assert feed._tokens_cover(scanner.out, cmd)
        toks = feed.tokenize_command(cmd)
        assert toks == scanner.out     # the scan, not the fallback
        assert toks != [feed.Token("word", cmd, 0, len(cmd))]
        assert {t.kind for t in toks} == {"string"}

    def test_a_scan_that_does_not_tile_is_thrown_away_for_one_word(self):
        # The other half of the guarantee: `tokenize_command` checks the tiling
        # itself, so a scanner bug can only cost the color, never show text
        # that is not what ran. Here the scan drops ` -la`.
        def truncating_run(self):
            self.out = [feed.Token("command", "ls", 0, 2)]

        with patch.object(feed._CommandScanner, "run", truncating_run):
            assert feed.tokenize_command("ls -la") == [
                feed.Token("word", "ls -la", 0, 6)]
            assert feed.highlight_command("ls -la").spans == []


class TestHighlightCommand:
    """`highlight_command`: the tokens painted with `CMD_STYLES`, never markup.

    The palette is the single source of truth, so the command view (and later
    the feed column and the turn page) cannot drift apart. The `Text` is built
    with `Text.append` only, so a command holding `[bold]` or `[/tmp/x]` renders
    literally instead of losing the span or raising MarkupError out of the live
    update (the rule under `Column`).
    """

    def test_plain_text_is_the_command_verbatim(self):
        for cmd in TestTokenizeCommand.CASES:
            assert feed.highlight_command(cmd).plain == cmd, cmd

    def test_the_palette_is_the_plan_s_table(self):
        # Written out, not derived from `CMD_STYLES`: every other test here
        # reads the palette to build its expectation, so recoloring an entry
        # would leave them all green. Three views share this table today and
        # phase 3 adds two more, so a change to it has to be deliberate in two
        # places. The values are the plan's palette table, character for
        # character (`plans/2026-09-21-command-highlighting.md`, the Highlighter
        # section); `""` means the kind renders unstyled.
        assert feed.CMD_STYLES == {
            "command": "bold",
            "keyword": "bold blue",
            "assign": "yellow",
            "flag": "cyan",
            "path": "blue",
            "string": "green",
            "var": "yellow",
            "subst": "yellow",
            "heredoc_op": "magenta",
            "heredoc_end": "magenta",
            "heredoc_body": "dim",
            "operator": "bold magenta",
            "redirect": "magenta",
            "comment": "dim italic",
            "space": "",
            "word": "",
        }

    def test_spans_are_exactly_the_palette_over_the_tokens(self):
        # The placement of the spans, not their colors: the literal table above
        # is what pins the colors themselves.
        from rich.text import Span
        cmd = "FOO=1 git log --oneline | head -5 > /tmp/out  # note"
        text = feed.highlight_command(cmd)
        expected = [Span(t.start, t.end, feed.CMD_STYLES[t.kind])
                    for t in feed.tokenize_command(cmd)
                    if feed.CMD_STYLES[t.kind]]
        assert text.plain == cmd
        assert text.spans == expected

    def test_unstyled_kinds_carry_no_span(self):
        # `word` and `space` map to "", so `ls -la` gets two spans, not four.
        text = feed.highlight_command("echo hi there")
        assert [s.style for s in text.spans] == ["bold"]

    def test_a_heredoc_is_painted_by_its_own_kinds(self):
        from rich.text import Span
        cmd = "cat <<'EOF'\nbody\nEOF"
        text = feed.highlight_command(cmd)
        styles = {cmd[s.start:s.end]: s.style for s in text.spans}
        assert styles["<<'EOF'"] == "magenta"
        assert styles["body\n"] == "dim"
        assert styles["EOF"] == "magenta"
        assert Span(0, 3, "bold") in text.spans

    def test_markup_in_a_command_is_never_parsed(self):
        # `[bold]` is silently deleted and `[/tmp/x]` raises MarkupError when a
        # bare str reaches Rich; both must survive as literal text here.
        for probe in ("[/tmp/x]", "[bold]", "[/]"):
            text = feed.highlight_command(f"echo {probe}")
            assert probe in text.plain
            console = Console(width=80, record=True)
            console.print(text)              # a MarkupError here IS the bug
            assert probe in console.export_text()

    def test_reflow_is_accepted_and_changes_nothing_yet(self):
        # Phase 2 fills it in; the keyword exists now so the call sites do not
        # change when it lands.
        cmd = "cd /tmp && ls | head -5"
        assert feed.highlight_command(cmd, reflow=True).plain == cmd
        assert (feed.highlight_command(cmd, reflow=True).spans
                == feed.highlight_command(cmd).spans)

    def test_empty_command_is_an_empty_text(self):
        text = feed.highlight_command("")
        assert text.plain == ""
        assert text.spans == []

    def test_broken_input_still_renders(self):
        for cmd in TestTokenizeCommand.BROKEN:
            console = Console(width=80, record=True)
            console.print(feed.highlight_command(cmd))   # never raises

    # ---- the danger prefix, which now takes a Text body ----

    def test_danger_prefix_accepts_a_text_body(self):
        from rich.text import Span, Text
        body = feed.highlight_command("sudo rm -rf /tmp/x")
        out = feed._danger_prefixed("sudo rm -rf /tmp/x", body)
        assert isinstance(out, Text)
        assert out.plain == "* sudo rm -rf /tmp/x"
        assert Span(0, 2, "bold red") in out.spans
        # the body's own styles survive, shifted past the marker
        assert Span(2, 6, "bold") in out.spans

    def test_danger_prefix_still_accepts_a_plain_string(self):
        # Every existing caller passes a str and must be unaffected.
        out = feed._danger_prefixed("ls -la", "ls -la")
        assert out.plain == "ls -la"
        assert out.spans == []

    def test_danger_prefix_does_not_mutate_the_body(self):
        body = feed.highlight_command("rm -rf /tmp/x")
        before = (body.plain, list(body.spans))
        feed._danger_prefixed("rm -rf /tmp/x", body)
        assert (body.plain, list(body.spans)) == before

    def test_danger_prefix_leaves_a_benign_command_alone(self):
        out = feed._danger_prefixed("ls -la", feed.highlight_command("ls -la"))
        assert out.plain == "ls -la"


class TestParseTs:
    """`parse_ts` pads the fraction and widens an hours-only UTC offset first.

    DuckDB renders a TIMESTAMPTZ as `2026-07-29 19:18:37.084-03` and trims the
    fraction's trailing zeros, so store timestamps arrive with an hours-only
    offset and with anything from 0 to 6 fraction digits. `fromisoformat`
    rejects the short offset before Python 3.11, and rejects any fraction that
    is not exactly 3 or 6 digits - both below the package's declared floor,
    where every Time cell read `??:??:??` and the turn page's duration chip
    disappeared. Measured on a copy of the real store: of 172,454 `events` rows
    90% carry 3 digits, 9% carry 2, 1% carry 1 and 151 carry none, and
    `file_history` snapshots add 4-, 5- and 6-digit ones.
    """

    def test_hours_only_offset_keeps_the_instant(self):
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        assert feed.parse_ts("2026-07-29 19:18:37.084-03") == _dt(
            2026, 7, 29, 19, 18, 37, 84000, tzinfo=_tz(_td(hours=-3)))

    def test_positive_hours_only_offset(self):
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        assert feed.parse_ts("2026-07-27 10:30:38+02") == _dt(
            2026, 7, 27, 10, 30, 38, tzinfo=_tz(_td(hours=2)))

    def test_two_digit_fraction_with_offset(self):
        # DuckDB trimmed a trailing zero off `.080`: 80 milliseconds, not 8.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        assert feed.parse_ts("2026-07-29 19:18:37.08-03") == _dt(
            2026, 7, 29, 19, 18, 37, 80000, tzinfo=_tz(_td(hours=-3)))

    def test_one_digit_fraction_with_offset(self):
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        assert feed.parse_ts("2026-07-29 19:18:37.5-03") == _dt(
            2026, 7, 29, 19, 18, 37, 500000, tzinfo=_tz(_td(hours=-3)))

    def test_two_digit_fraction_without_offset(self):
        from datetime import datetime as _dt
        assert feed.parse_ts("2026-07-29 19:18:37.08") == _dt(
            2026, 7, 29, 19, 18, 37, 80000)

    def test_one_digit_fraction_without_offset(self):
        from datetime import datetime as _dt
        assert feed.parse_ts("2026-07-29 19:18:37.5") == _dt(
            2026, 7, 29, 19, 18, 37, 500000)

    def test_five_digit_fraction_with_full_offset(self):
        # The padding runs before the offset is widened, so it has to end the
        # fraction at a `+HH:MM` offset too, not only at an hours-only one.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        assert feed.parse_ts("2026-07-29 19:18:37.01234+02:00") == _dt(
            2026, 7, 29, 19, 18, 37, 12340, tzinfo=_tz(_td(hours=2)))

    def test_over_long_fraction_is_truncated_not_rejected(self):
        # DuckDB stores microseconds and never emits more, but a nanosecond
        # string would raise on every Python version; truncating keeps the
        # instant to the precision `datetime` can hold.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        assert feed.parse_ts("2026-07-29 19:18:37.084123456-03") == _dt(
            2026, 7, 29, 19, 18, 37, 84123, tzinfo=_tz(_td(hours=-3)))

    def test_full_offset_is_left_alone(self):
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        assert feed.parse_ts("2025-01-01T12:00:00-03:00") == _dt(
            2025, 1, 1, 12, 0, 0, tzinfo=_tz(_td(hours=-3)))

    def test_naive_timestamp_is_left_alone(self):
        from datetime import datetime as _dt
        assert feed.parse_ts("2025-01-01T12:34:56") == _dt(2025, 1, 1, 12, 34, 56)

    def test_date_only_is_not_read_as_an_offset(self):
        # `-29` ends the string, but there is no time in front of it: widening it
        # to `2026-07-29:00` would turn a valid date into a parse error.
        from datetime import datetime as _dt
        assert feed.parse_ts("2026-07-29") == _dt(2026, 7, 29)

    def test_format_time_renders_a_store_timestamp(self):
        # The user-visible half of the same fix: this cell is what read
        # `??:??:??` on 3.10 for every row of every table.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        expected = _dt(2026, 7, 29, 19, 18, 37,
                       tzinfo=_tz(_td(hours=-3))).astimezone().strftime("%H:%M:%S")
        assert feed.format_time("2026-07-29 19:18:37.084-03") == expected


class TestEnvExampleDocumentsEveryKey:
    """`.env.example` is where a user learns these keys exist, so the file and
    the `_env_int` call sites must name the same keys with the same defaults.
    A width added to COLUMNS / PROMPT_COLUMNS and never written down is
    undiscoverable, a default changed in only one of the two files is worse than
    either value alone, and a key left in the file after the code stopped
    reading it is a documented setting that does nothing.

    Both assertions are whole-line (`^KEY=N$`): a substring match let a code
    default of `8` pass against a file value of `80`. The set comparison also
    catches the one shape `_pairs` cannot scrape, a non-literal default
    (`_env_int("X", DEFAULT)`), which would otherwise be skipped in silence."""

    def _example(self):
        return (Path(feed.__file__).resolve().parent / ".env.example").read_text(
            encoding="utf-8")

    def _pairs(self):
        source = Path(feed.__file__).resolve().read_text(encoding="utf-8")
        pairs = re.findall(r'_env_int\("(\w+)", (\d+)\)', source)
        assert pairs, "no _env_int call sites found; the pattern went stale"
        return pairs

    def test_every_env_int_key_is_in_the_example(self):
        example = self._example()
        for key, default in self._pairs():
            assert re.search(rf"^{re.escape(key)}={default}$", example, re.M), (
                f"{key}={default} is missing from .env.example")

    def test_every_example_key_is_read_by_the_code(self):
        documented = set(re.findall(r"^(CLAUDE_TRAIL_\w+)=", self._example(), re.M))
        assert documented == {key for key, _ in self._pairs()}, (
            "the .env.example keys and the _env_int call sites have drifted apart")


class TestFormatTime:
    def test_valid_iso(self):
        assert feed.format_time("2025-01-01T12:34:56") == "12:34:56"

    def test_iso_with_z_suffix(self):
        # Either fromisoformat doesn't support Z (Python <3.11, returns "??:??:??"),
        # or it parses as UTC and converts to local time (HH:MM:SS).
        result = feed.format_time("2025-01-01T12:00:00.000Z")
        assert result == "??:??:??" or (len(result) == 8 and result.count(":") == 2)

    def test_iso_with_offset_renders_in_local_tz(self):
        # Same UTC instant but expressed with a +00:00 offset; fromisoformat
        # always supports this. Result must match what 12:00 UTC looks like locally.
        from datetime import datetime as _dt, timezone as _tz
        expected = _dt(2025, 1, 1, 12, 0, 0, tzinfo=_tz.utc).astimezone().strftime("%H:%M:%S")
        assert feed.format_time("2025-01-01T12:00:00+00:00") == expected

    def test_invalid_string(self):
        assert feed.format_time("not a date") == "??:??:??"

    def test_empty_string(self):
        assert feed.format_time("") == "??:??:??"


class TestShortPath:
    def test_whole_path_when_it_fits(self):
        # short enough to show in full (<= DIR_LABEL_WIDTH)
        assert feed.short_path("/tmp/x") == "/tmp/x"

    def test_keeps_trailing_dir_intact(self):
        # the distinctive tail survives; leading `…/` marks the elided parents
        assert (feed.short_path("/home/naka/Projects/personal/claude-trail")
                == "…/claude-trail")

    def test_grows_leftward_to_a_component_boundary(self):
        # more than one trailing component when they fit, never a partial parent
        result = feed.short_path("/home/user/proj", width=14)
        assert result == "…/user/proj"

    def test_long_single_component_keeps_its_end(self):
        result = feed.short_path("/home/user/very-long-directory-name", width=14)
        assert result.startswith("…")
        assert len(result) == 14
        assert result.endswith("name")  # the distinctive end, not the start

    def test_empty(self):
        assert feed.short_path("") == ""


class TestSessionLabel:
    def test_empty_id(self):
        assert feed.session_label("") == "--------"

    def test_no_name_map_uses_id_prefix(self):
        assert feed.session_label("abcdef1234567890") == "abcdef12"

    def test_empty_name_map_falls_back(self):
        assert feed.session_label("abcd1234", {}) == "abcd1234"

    def test_id_present_in_map_returns_name(self):
        assert feed.session_label("abcd1234", {"abcd1234": "my-session"}) == "my-session"

    def test_id_absent_from_map_falls_back_to_prefix(self):
        assert feed.session_label("abcdef1234567890", {"other": "x"}) == "abcdef12"


class TestRichColor:
    def test_none_stays_none(self):
        assert feed.rich_color(None) is None

    def test_empty_string_is_none(self):
        assert feed.rich_color("") is None

    def test_passthrough_for_rich_native_names(self):
        assert feed.rich_color("red") == "red"
        assert feed.rich_color("yellow") == "yellow"
        assert feed.rich_color("magenta") == "magenta"

    def test_orange_mapped_to_orange1(self):
        assert feed.rich_color("orange") == "orange1"

    def test_gray_and_grey_mapped(self):
        assert feed.rich_color("gray") == "grey50"
        assert feed.rich_color("grey") == "grey50"

    def test_pink_mapped(self):
        assert feed.rich_color("pink") == "pink1"

    def test_aliased_names_parse_in_rich(self):
        from rich.style import Style
        for claude_name in feed.CLAUDE_COLOR_ALIASES:
            Style.parse(feed.rich_color(claude_name))  # raises if invalid


class TestScanChunkForColor:
    def _evt(self, color):
        import json
        return json.dumps({
            "type": "system",
            "subtype": "local_command",
            "content": (
                f"<command-name>/color</command-name>\n"
                f"            <command-message>color</command-message>\n"
                f"            <command-args>{color}</command-args>"
            ),
        })

    def test_returns_none_when_no_color_event(self):
        assert feed._scan_chunk_for_color('{"type":"system","content":"hi"}\n') is None

    def test_extracts_color_from_single_event(self):
        assert feed._scan_chunk_for_color(self._evt("yellow")) == "yellow"

    def test_returns_last_color_when_multiple(self):
        chunk = self._evt("red") + "\n" + self._evt("yellow")
        assert feed._scan_chunk_for_color(chunk) == "yellow"

    def test_ignores_non_local_command_events(self):
        bogus = '{"type":"assistant","content":"<command-name>/color</command-name>...<command-args>red</command-args>"}'
        assert feed._scan_chunk_for_color(bogus) is None

    def test_tolerates_malformed_json_lines(self):
        chunk = "not-json-at-all\n" + self._evt("orange")
        assert feed._scan_chunk_for_color(chunk) == "orange"


class TestLoadSessionColors:
    """Covers transcript discovery, incremental tailing, and TTL caching.

    Module-level cache state is cleared before each test by the autouse
    fixture in conftest.py.
    """

    def _color_event(self, sid, color):
        import json
        return json.dumps({
            "type": "system",
            "subtype": "local_command",
            "sessionId": sid,
            "content": (
                f"<command-name>/color</command-name>\n"
                f"            <command-args>{color}</command-args>"
            ),
        }) + "\n"

    def _write_transcript(self, projects_root, sid, lines):
        project = projects_root / "-home-naka-Projects-x"
        project.mkdir(parents=True, exist_ok=True)
        path = project / f"{sid}.jsonl"
        path.write_text("".join(lines), encoding="utf-8")
        return path

    def test_reads_color_from_transcript(self, tmp_path):
        sid = "aaa-111"
        self._write_transcript(tmp_path, sid, [self._color_event(sid, "yellow")])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSION_COLOR_CACHE_TTL", 0.0):
            colors = feed.load_session_colors([sid])
        assert colors == {sid: "yellow"}

    def test_picks_latest_color_when_multiple_set(self, tmp_path):
        sid = "bbb-222"
        self._write_transcript(tmp_path, sid, [
            self._color_event(sid, "red"),
            self._color_event(sid, "blue"),
        ])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSION_COLOR_CACHE_TTL", 0.0):
            colors = feed.load_session_colors([sid])
        assert colors == {sid: "blue"}

    def test_session_without_color_command_returns_empty(self, tmp_path):
        sid = "ccc-333"
        self._write_transcript(tmp_path, sid, [
            '{"type":"user","content":"hello"}\n',
        ])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSION_COLOR_CACHE_TTL", 0.0):
            colors = feed.load_session_colors([sid])
        assert colors == {}

    def test_missing_transcript_is_skipped(self, tmp_path):
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSION_COLOR_CACHE_TTL", 0.0):
            colors = feed.load_session_colors(["never-existed"])
        assert colors == {}

    def test_missing_projects_dir_is_tolerated(self, tmp_path):
        with patch.object(feed, "PROJECTS_DIR", tmp_path / "absent"), \
             patch.object(feed, "SESSION_COLOR_CACHE_TTL", 0.0):
            colors = feed.load_session_colors(["any"])
        assert colors == {}

    def test_picks_up_color_appended_to_transcript(self, tmp_path):
        """Mid-session /color must be observed on the next non-cached refresh."""
        sid = "ddd-444"
        path = self._write_transcript(tmp_path, sid, [self._color_event(sid, "red")])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSION_COLOR_CACHE_TTL", 0.0):
            assert feed.load_session_colors([sid]) == {sid: "red"}
            with open(path, "a", encoding="utf-8") as f:
                f.write(self._color_event(sid, "magenta"))
            assert feed.load_session_colors([sid]) == {sid: "magenta"}

    def test_cache_retains_color_after_transcript_removed(self, tmp_path):
        """If Claude Code rotates or removes a transcript, the last-known color
        should still be available."""
        sid = "eee-555"
        path = self._write_transcript(tmp_path, sid, [self._color_event(sid, "cyan")])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSION_COLOR_CACHE_TTL", 0.0):
            assert feed.load_session_colors([sid]) == {sid: "cyan"}
            path.unlink()
            feed._transcript_path_cache.pop(sid, None)  # invalidate stale path
            assert feed.load_session_colors([sid]) == {sid: "cyan"}


class TestLoadSessionNames:
    """Covers reading ~/.claude/sessions/*.json with mocked SESSIONS_DIR.

    The `_session_name_cache` / `_session_name_cache_ts` globals are cleared
    before each test by the autouse fixture in conftest.py.
    """

    def test_reads_name_from_json(self, tmp_path):
        import json
        (tmp_path / "1234.json").write_text(
            json.dumps({"sessionId": "abc-123", "name": "my-session"})
        )
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {"abc-123": "my-session"}

    def test_skips_sessions_without_name(self, tmp_path):
        import json
        (tmp_path / "1.json").write_text(json.dumps({"sessionId": "a"}))
        (tmp_path / "2.json").write_text(
            json.dumps({"sessionId": "b", "name": "labeled"})
        )
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {"b": "labeled"}

    def test_skips_entry_without_session_id(self, tmp_path):
        import json
        (tmp_path / "orphan.json").write_text(json.dumps({"name": "no-id"}))
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {}

    def test_skips_malformed_json(self, tmp_path):
        import json
        (tmp_path / "broken.json").write_text("not json at all")
        (tmp_path / "good.json").write_text(
            json.dumps({"sessionId": "x", "name": "ok"})
        )
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {"x": "ok"}

    def test_skips_non_json_files(self, tmp_path):
        import json
        (tmp_path / "README").write_text("ignore me")
        (tmp_path / "lock.txt").write_text("ignore me too")
        (tmp_path / "ok.json").write_text(
            json.dumps({"sessionId": "y", "name": "kept"})
        )
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {"y": "kept"}

    def test_missing_directory_returns_empty(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        with patch.object(feed, "SESSIONS_DIR", missing), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {}

    def test_cache_persists_name_after_file_removed(self, tmp_path):
        """A name observed once must remain available after Claude Code removes
        the pid.json on session exit, so the TUI can still label historical rows.
        """
        import json
        pid_file = tmp_path / "999.json"
        pid_file.write_text(json.dumps({"sessionId": "s1", "name": "first"}))
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            assert feed.load_session_names() == {"s1": "first"}
            pid_file.unlink()
            assert feed.load_session_names() == {"s1": "first"}


class TestAgentLabel:
    """entry_agent_id / agent_label resolution (pure, no disk)."""

    def test_main_agent_entry_has_no_label(self):
        assert feed.entry_agent_id({"session_id": "s"}) is None
        assert feed.agent_label({"session_id": "s"}, {}) is None

    def test_empty_agent_id_is_none(self):
        assert feed.entry_agent_id({"agent_id": ""}) is None
        assert feed.agent_label({"agent_id": ""}, {}) is None

    def test_resolves_description(self):
        amap = {"aid1": {"type": "general-purpose", "description": "Implement Phase 4"}}
        # short id prefix, then the description
        assert feed.agent_label({"agent_id": "aid1"}, amap) == "aid1 Implement Phase 4"

    def test_falls_back_to_type_then_id(self):
        amap = {"aid1": {"type": "Explore", "description": ""}}
        assert feed.agent_label({"agent_id": "aid1"}, amap) == "aid1 Explore"
        # unknown agent_id -> the short id alone (no name to append)
        assert feed.agent_label({"agent_id": "abcdef1234"}, {}) == "abcd"

    def test_same_description_stays_distinct_by_id(self):
        # two agents doing the same kind of work still read apart via the id
        amap = {"a1b2c3": {"type": "general-purpose", "description": "Airflow statsd check"},
                "d4e5f6": {"type": "general-purpose", "description": "Airflow statsd check"}}
        one = feed.agent_label({"agent_id": "a1b2c3"}, amap)
        two = feed.agent_label({"agent_id": "d4e5f6"}, amap)
        assert one == "a1b2 Airflow statsd check"
        assert two == "d4e5 Airflow statsd check"
        assert one != two


def _make_session(projects_root, sid, *, agents, completed=(),
                  extra_events=None, proj="-home-naka-Projects-x",
                  cwd="/home/naka/Projects/x"):
    """Build a fake `<proj>/<sid>.jsonl` + `<sid>/subagents/` tree.

    `agents` maps agent_id -> (agent_type, description, n_tool_use); `completed`
    lists agent_ids to mark done via a <task-notification>. Returns the project
    directory path.
    """
    import json
    project = projects_root / proj
    subagents = project / sid / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)

    lines = [json.dumps({"type": "user", "cwd": cwd, "content": "hi"}) + "\n"]
    for aid in completed:
        # inner newlines are escaped by json.dumps, so this stays one physical line
        notif = (f"<task-notification>\n<task-id>{aid}</task-id>\n"
                 f"<tool-use-id>toolu_{aid}</tool-use-id>\n"
                 f"<status>completed</status>\n</task-notification>")
        lines.append(json.dumps({"type": "user", "content": notif}) + "\n")
    for line in (extra_events or []):
        lines.append(line)
    (project / f"{sid}.jsonl").write_text("".join(lines), encoding="utf-8")

    for aid, (atype, desc, n_use) in agents.items():
        meta = {"agentType": atype, "toolUseId": f"toolu_{aid}", "spawnDepth": 1}
        if desc is not None:
            meta["description"] = desc
        (subagents / f"agent-{aid}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
        body = "".join(
            '{"type":"assistant","message":{"content":['
            '{"type":"tool_use","name":"Bash"}]}}\n'
            for _ in range(n_use)
        )
        (subagents / f"agent-{aid}.jsonl").write_text(body, encoding="utf-8")
    return project


class TestLoadAgentLabels:
    def test_reads_meta_including_workflow_agents(self, tmp_path):
        sid = "sid-labels"
        project = _make_session(tmp_path, sid, agents={
            "aaa111": ("general-purpose", "Implement Phase 4", 2),
        })
        # a workflow agent nested one level deeper, no description
        wf = project / sid / "subagents" / "workflows" / "wf_1"
        wf.mkdir(parents=True)
        (wf / "agent-bbb222.meta.json").write_text(
            '{"agentType":"workflow-subagent","spawnDepth":1}', encoding="utf-8")
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "AGENT_LABEL_CACHE_TTL", 0.0):
            labels = feed.load_agent_labels([sid])
        assert labels["aaa111"] == {"type": "general-purpose",
                                    "description": "Implement Phase 4"}
        assert labels["bbb222"] == {"type": "workflow-subagent", "description": ""}

    def test_missing_session_is_skipped(self, tmp_path):
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "AGENT_LABEL_CACHE_TTL", 0.0):
            assert feed.load_agent_labels(["nope", ""]) == {}


class TestLoadSessionModel:
    def test_live_session_reports_running_and_done(self, tmp_path):
        import json
        sid = "sid-live"
        _make_session(tmp_path, sid, agents={
            "done1": ("general-purpose", "Review Phase 4", 3),
            "run1": ("Explore", "Search the tree", 1),
        }, completed=["done1"])
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "1467.json").write_text(json.dumps({
            "pid": 1467, "sessionId": sid, "cwd": "/home/naka/Projects/x",
            "version": "2.1.200", "kind": "interactive", "name": "my-sess",
            "status": "busy", "startedAt": 1783058624928,
        }), encoding="utf-8")
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSIONS_DIR", sessions), \
             patch.object(feed, "SESSION_MODEL_CACHE_TTL", 0.0):
            model = feed.load_session_model(sid)

        assert model.live is True
        assert model.name == "my-sess"
        assert model.status == "busy"
        assert model.cwd == "/home/naka/Projects/x"
        assert model.version == "2.1.200"
        assert model.kind == "interactive"
        assert model.started_at == 1783058624928
        assert model.transcript_path.endswith(f"{sid}.jsonl")

        by_id = {s.agent_id: s for s in model.subagents}
        assert by_id["done1"].status == "done"
        assert by_id["done1"].command_count == 3
        assert by_id["run1"].status == "running"
        assert by_id["run1"].command_count == 1
        # subagents sort in spawn order (meta mtime); done1's meta is written
        # first and also wins the agent_id tiebreak, so it sorts ahead of run1
        assert model.subagents[0].agent_id == "done1"

    def test_ended_session_marks_incomplete_stopped(self, tmp_path):
        sid = "sid-ended"
        _make_session(tmp_path, sid, agents={
            "gone1": ("general-purpose", "Never finished", 0),
        })
        sessions = tmp_path / "sessions"
        sessions.mkdir()  # no pid.json -> ended session
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSIONS_DIR", sessions), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0), \
             patch.object(feed, "SESSION_MODEL_CACHE_TTL", 0.0):
            model = feed.load_session_model(sid)
        assert model.live is False
        assert model.cwd == "/home/naka/Projects/x"  # from first transcript event
        assert model.subagents[0].status == "stopped"

    def test_failed_and_killed_map_to_stopped(self, tmp_path):
        import json
        sid = "sid-terminal"
        # two agents completed via task-notification with non-completed statuses
        extra = []
        for aid, st in (("failA", "failed"), ("killB", "killed")):
            notif = (f"<task-notification>\n<task-id>{aid}</task-id>\n"
                     f"<status>{st}</status>\n</task-notification>")
            extra.append(json.dumps({"type": "user", "content": notif}) + "\n")
        _make_session(tmp_path, sid, agents={
            "failA": ("general-purpose", "boom", 0),
            "killB": ("general-purpose", "zap", 0),
        }, extra_events=extra)
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "1.json").write_text(json.dumps({
            "sessionId": sid, "name": "s", "cwd": "/x",
        }), encoding="utf-8")  # live, but both agents have terminal notifications
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSIONS_DIR", sessions), \
             patch.object(feed, "SESSION_MODEL_CACHE_TTL", 0.0):
            model = feed.load_session_model(sid)
        assert {s.status for s in model.subagents} == {"stopped"}

    def test_session_without_subagents(self, tmp_path):
        sid = "sid-bare"
        _make_session(tmp_path, sid, agents={})
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSIONS_DIR", sessions), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0), \
             patch.object(feed, "SESSION_MODEL_CACHE_TTL", 0.0):
            model = feed.load_session_model(sid)
        assert model.subagents == ()

    def test_command_count_byte_cap(self, tmp_path):
        sid = "sid-cap"
        _make_session(tmp_path, sid, agents={"big": ("general-purpose", "big", 3)})
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        # cap below the transcript size -> capped flag set
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSIONS_DIR", sessions), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0), \
             patch.object(feed, "AGENT_TOOLUSE_BYTE_CAP", 10), \
             patch.object(feed, "SESSION_MODEL_CACHE_TTL", 0.0):
            model = feed.load_session_model(sid)
        assert model.subagents[0].command_count_capped is True

    def test_subagents_ordered_by_spawn_mtime_not_description(self, tmp_path):
        import os
        sid = "sid-order"
        # "zzz-agent" spawns first; "aaa-agent" spawns second. Both its agent_id
        # and its description ("aaa-...") sort ahead alphabetically, so a
        # description- or id-based sort would put the later-spawned one first.
        # Pure spawn (mtime) order must keep zzz-agent (spawned first) ahead.
        project = _make_session(tmp_path, sid, agents={
            "zzz-agent": ("general-purpose", "zzz-desc-earlier", 0),
            "aaa-agent": ("general-purpose", "aaa-desc-later", 0),
        })
        subs = project / sid / "subagents"
        os.utime(subs / "agent-zzz-agent.meta.json", (1000.0, 1000.0))
        os.utime(subs / "agent-aaa-agent.meta.json", (2000.0, 2000.0))
        transcript = project / f"{sid}.jsonl"
        with patch.object(feed, "PROJECTS_DIR", tmp_path):
            infos = feed._load_subagents(sid, transcript, live=True)
        assert [s.agent_id for s in infos] == ["zzz-agent", "aaa-agent"]
        assert [s.spawn_ts for s in infos] == [1000.0, 2000.0]


class TestExtractFiles:
    def test_no_paths(self):
        assert feed.extract_files("ls") == ""

    def test_single_absolute_path(self):
        assert feed.extract_files("cat /etc/hosts") == "hosts"

    def test_multiple_unique_paths(self):
        result = feed.extract_files("cp /tmp/a.txt /tmp/b.txt")
        assert "a.txt" in result
        assert "b.txt" in result

    def test_duplicate_paths_deduplicated(self):
        result = feed.extract_files("diff /tmp/foo.txt /tmp/foo.txt")
        assert result == "foo.txt"

    def test_more_than_three_paths(self):
        result = feed.extract_files("cat /a/1.txt /a/2.txt /a/3.txt /a/4.txt /a/5.txt")
        assert "+2" in result

    def test_home_path_basename(self):
        assert feed.extract_files("cat ~/notes/todo.md") == "todo.md"

    def test_relative_path_basename(self):
        assert feed.extract_files("cat ./src/main.py") == "main.py"

    def test_parent_relative_path_basename(self):
        assert feed.extract_files("cat ../sibling/x.txt") == "x.txt"

    def test_trailing_slash_stripped(self):
        assert feed.extract_files("ls /tmp/logs/") == "logs"

    def test_bare_slash_yields_empty(self):
        # FILE_PATH_RE needs at least one char after the slash, so a lone "/"
        # argument matches nothing and yields "".
        assert feed.extract_files("ls /") == ""


class TestFindPaths:
    """find_paths returns raw paths (not basenames), order-preserving deduped."""

    def test_no_paths(self):
        assert feed.find_paths("ls -la") == []

    def test_absolute_paths_in_order(self):
        assert feed.find_paths("cp /tmp/a.txt /tmp/b.txt") == ["/tmp/a.txt", "/tmp/b.txt"]

    def test_dedupes_preserving_first_seen_order(self):
        assert feed.find_paths("diff /tmp/b /tmp/a /tmp/b") == ["/tmp/b", "/tmp/a"]

    def test_home_and_relative_paths(self):
        assert feed.find_paths("cat ~/notes/todo.md ./src/main.py") == [
            "~/notes/todo.md",
            "./src/main.py",
        ]

    def test_extract_files_is_a_formatter_over_find_paths(self):
        # extract_files basenames exactly the paths find_paths reports.
        cmd = "cp /tmp/a.txt /tmp/b.txt"
        assert feed.extract_files(cmd) == ", ".join(
            os.path.basename(p) for p in feed.find_paths(cmd)
        )


class TestGetDisplayEntries:
    def test_empty_list(self):
        assert feed.get_display_entries([], 10) == []

    def test_fewer_than_max_rows(self):
        entries = [{"id": 1}, {"id": 2}, {"id": 3}]
        result = feed.get_display_entries(entries, 10)
        assert result == [{"id": 3}, {"id": 2}, {"id": 1}]

    def test_more_than_max_rows(self):
        entries = [{"id": i} for i in range(10)]
        result = feed.get_display_entries(entries, 3)
        assert result == [{"id": 9}, {"id": 8}, {"id": 7}]

    def test_offset_skips_newest(self):
        entries = [{"id": i} for i in range(10)]
        # offset=3 skips the 3 newest, max_rows=3 → next 3
        result = feed.get_display_entries(entries, 3, offset=3)
        assert result == [{"id": 6}, {"id": 5}, {"id": 4}]

    def test_offset_past_end_returns_empty(self):
        entries = [{"id": i} for i in range(5)]
        result = feed.get_display_entries(entries, 3, offset=10)
        assert result == []


class TestParseLine:
    def test_valid_json(self):
        result = feed.parse_line('{"foo": "bar"}')
        assert result == {"foo": "bar"}

    def test_invalid_json(self):
        assert feed.parse_line("not json") is None

    def test_empty_string(self):
        assert feed.parse_line("") is None


class TestPlatformOpener:
    def test_darwin_returns_open(self):
        with patch.object(feed.sys, "platform", "darwin"):
            assert feed._platform_opener() == "open"

    def test_linux_returns_xdg_open(self):
        with patch.object(feed.sys, "platform", "linux"):
            assert feed._platform_opener() == "xdg-open"

    def test_other_platforms_default_to_xdg_open(self):
        with patch.object(feed.sys, "platform", "freebsd14"):
            assert feed._platform_opener() == "xdg-open"


class TestConfigDir:
    """CONFIG_DIR and the paths derived from it honor $CLAUDE_CONFIG_DIR."""

    def _reload_with_true_env(self, monkeypatch):
        # Undo our env changes FIRST, then reload, so the module is left
        # consistent with the real outer environment (whatever it is), not with
        # this test's temporary env. Order matters: reloading before the undo
        # would leave the module and the environment disagreeing.
        import importlib
        monkeypatch.undo()
        importlib.reload(feed)

    def test_env_override_reshapes_derived_paths(self, monkeypatch):
        import importlib
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/cfg")
        importlib.reload(feed)
        try:
            assert feed.CONFIG_DIR == Path("/custom/cfg")
            assert feed.LOG_PATH == Path("/custom/cfg/command-log.jsonl")
            assert feed.SESSIONS_DIR == Path("/custom/cfg/sessions")
            assert feed.PROJECTS_DIR == Path("/custom/cfg/projects")
        finally:
            self._reload_with_true_env(monkeypatch)

    def test_empty_env_falls_back_to_home(self, monkeypatch):
        # Empty string must fall back to ~/.claude (the reason CONFIG_DIR uses
        # `or` rather than os.environ.get with a default argument).
        import importlib
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        importlib.reload(feed)
        try:
            assert feed.CONFIG_DIR == Path.home() / ".claude"
        finally:
            self._reload_with_true_env(monkeypatch)

    def test_default_is_home_dot_claude(self, monkeypatch):
        import importlib
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        importlib.reload(feed)
        try:
            assert feed.CONFIG_DIR == Path.home() / ".claude"
            assert feed.LOG_PATH == Path.home() / ".claude" / "command-log.jsonl"
        finally:
            self._reload_with_true_env(monkeypatch)


class TestLoadDotenv:
    """_load_dotenv parses CONFIG_DIR/.env (or $CLAUDE_TRAIL_ENV) into a dict."""

    def test_ignores_comments_and_blank_lines(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# a comment\n"
            "\n"
            "   \n"
            "CLAUDE_TRAIL_COL_TIME_WIDTH=10\n"
            "CLAUDE_TRAIL_COL_FILES_WIDTH = 30 \n"
            "no_equals_line\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(env)}):
            out = feed._load_dotenv()
        assert out == {
            "CLAUDE_TRAIL_COL_TIME_WIDTH": "10",
            "CLAUDE_TRAIL_COL_FILES_WIDTH": "30",
        }

    def test_strips_surrounding_quotes(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            'CLAUDE_TRAIL_COL_TIME_WIDTH="12"\n'
            "CLAUDE_TRAIL_COL_AGENT_WIDTH='9'\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(env)}):
            out = feed._load_dotenv()
        assert out["CLAUDE_TRAIL_COL_TIME_WIDTH"] == "12"
        assert out["CLAUDE_TRAIL_COL_AGENT_WIDTH"] == "9"

    def test_missing_file_returns_empty(self, tmp_path):
        missing = tmp_path / "absent.env"
        with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(missing)}):
            assert feed._load_dotenv() == {}


class TestEnvInt:
    """_env_int layers os.environ over .env over the built-in default; a
    non-integer or <= 0 value falls back to the default."""

    KEY = "CLAUDE_TRAIL_COL_TIME_WIDTH"

    def test_default_when_unset(self):
        with patch.object(feed, "_DOTENV", {}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.KEY, None)
            assert feed._env_int(self.KEY, 8) == 8

    def test_dotenv_over_default(self):
        with patch.object(feed, "_DOTENV", {self.KEY: "11"}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.KEY, None)
            assert feed._env_int(self.KEY, 8) == 11

    def test_environ_over_dotenv(self):
        # A real environment variable wins over the .env value.
        with patch.object(feed, "_DOTENV", {self.KEY: "11"}), \
             patch.dict(os.environ, {self.KEY: "20"}):
            assert feed._env_int(self.KEY, 8) == 20

    def test_junk_value_falls_back(self):
        with patch.object(feed, "_DOTENV", {}), \
             patch.dict(os.environ, {self.KEY: "abc"}):
            assert feed._env_int(self.KEY, 8) == 8

    def test_zero_and_negative_fall_back(self):
        for bad in ("0", "-5"):
            with patch.object(feed, "_DOTENV", {}), \
                 patch.dict(os.environ, {self.KEY: bad}):
                assert feed._env_int(self.KEY, 8) == 8


class TestColumnWidthWiring:
    """The five .env width keys must actually reach COLUMNS / DIR_LABEL_WIDTH.

    TestLoadDotenv / TestEnvInt cover the helpers in isolation; this reloads the
    module end-to-end (the TestConfigDir reload pattern) so a typo in any key
    name at the COLUMNS call sites (or in DIR_LABEL_WIDTH) is caught: such a typo
    would silently no-op that column while every helper test still passed.
    Columns are matched by their stable name, not a positional index, so the
    assertions survive column reordering.
    """

    # Non-default and mutually distinct, so a swapped key name also fails.
    _OVERRIDES = {
        "CLAUDE_TRAIL_COL_TIME_WIDTH": "10",
        "CLAUDE_TRAIL_COL_SESSION_WIDTH": "17",
        "CLAUDE_TRAIL_COL_AGENT_WIDTH": "30",
        "CLAUDE_TRAIL_COL_DIRECTORY_WIDTH": "19",
        "CLAUDE_TRAIL_COL_FILES_WIDTH": "25",
    }

    def _cols_by_name(self):
        return {c.name: c for c in feed.COLUMNS}

    def test_env_widths_reach_columns_and_dir_label(self, tmp_path):
        import importlib
        env = tmp_path / ".env"
        env.write_text(
            "".join(f"{k}={v}\n" for k, v in self._OVERRIDES.items()),
            encoding="utf-8",
        )
        try:
            with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(env)}):
                importlib.reload(feed)
                cols = self._cols_by_name()
                assert cols["Time"].kwargs["width"] == 10
                assert cols["Session"].kwargs["width"] == 17
                assert cols["Agent"].kwargs["width"] == 30
                assert cols["Files"].kwargs["width"] == 25
                assert feed.DIR_LABEL_WIDTH == 19
                # Directory shares DIR_LABEL_WIDTH (its own key drives both).
                assert cols["Directory"].kwargs["width"] == 19
        finally:
            # patch.dict restored CLAUDE_TRAIL_ENV to the conftest-isolated
            # (nonexistent) path; reload so later tests see default widths again.
            importlib.reload(feed)

    def test_clean_reload_reproduces_defaults(self, tmp_path):
        # A clean environment (no .env, no CLAUDE_TRAIL_COL_* env vars) must
        # reproduce the pre-phase literal widths exactly.
        import importlib
        absent = tmp_path / "absent.env"
        with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(absent)}):
            for k in [k for k in os.environ if k.startswith("CLAUDE_TRAIL_COL_")]:
                os.environ.pop(k)
            importlib.reload(feed)
            cols = self._cols_by_name()
            assert cols["Time"].kwargs["width"] == 8
            assert cols["Session"].kwargs["width"] == 13
            assert cols["Agent"].kwargs["width"] == 24
            assert feed.DIR_LABEL_WIDTH == 14
            assert cols["Directory"].kwargs["width"] == 14
            assert cols["Files"].kwargs["width"] == 20


class TestPromptColumnWidthWiring:
    """Same guard for the Prompts tab's six .env width keys: a typo at a
    PROMPT_COLUMNS call site would silently no-op that column while every helper
    test still passed. Matched by column name, so reordering is safe."""

    _OVERRIDES = {
        "CLAUDE_TRAIL_PROMPT_COL_TIME_WIDTH": "11",
        "CLAUDE_TRAIL_PROMPT_COL_SESSION_WIDTH": "18",
        "CLAUDE_TRAIL_PROMPT_COL_PROJECT_WIDTH": "21",
        "CLAUDE_TRAIL_PROMPT_COL_CALLS_WIDTH": "15",
        "CLAUDE_TRAIL_PROMPT_COL_AGENTS_WIDTH": "9",
        "CLAUDE_TRAIL_PROMPT_COL_TOKENS_WIDTH": "23",
    }

    def _cols_by_name(self):
        return {c.name: c for c in feed.PROMPT_COLUMNS}

    def test_env_widths_reach_prompt_columns(self, tmp_path):
        import importlib
        env = tmp_path / ".env"
        env.write_text("".join(f"{k}={v}\n" for k, v in self._OVERRIDES.items()),
                       encoding="utf-8")
        try:
            with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(env)}):
                importlib.reload(feed)
                cols = self._cols_by_name()
                assert cols["Time"].kwargs["width"] == 11
                assert cols["Session"].kwargs["width"] == 18
                assert cols["Calls"].kwargs["width"] == 15
                assert cols["Agents"].kwargs["width"] == 9
                assert cols["Tokens"].kwargs["width"] == 23
                # Project shares PROJECT_LABEL_WIDTH with short_path's truncation.
                assert feed.PROJECT_LABEL_WIDTH == 21
                assert cols["Project"].kwargs["width"] == 21
        finally:
            importlib.reload(feed)

    def test_the_feeds_keys_do_not_move_the_prompts_columns(self, tmp_path):
        # The two tabs have separate keys: setting a feed width must leave this
        # tab alone (a copy-pasted key name would break exactly this).
        import importlib
        env = tmp_path / ".env"
        env.write_text("CLAUDE_TRAIL_COL_TIME_WIDTH=30\n"
                       "CLAUDE_TRAIL_COL_SESSION_WIDTH=31\n", encoding="utf-8")
        try:
            with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(env)}):
                importlib.reload(feed)
                cols = self._cols_by_name()
                assert cols["Time"].kwargs["width"] == 8       # prompts default
                assert cols["Session"].kwargs["width"] == 13
        finally:
            importlib.reload(feed)

    def test_clean_reload_reproduces_prompt_defaults(self, tmp_path):
        import importlib
        absent = tmp_path / "absent.env"
        with patch.dict(os.environ, {"CLAUDE_TRAIL_ENV": str(absent)}):
            for k in [k for k in os.environ if k.startswith("CLAUDE_TRAIL_PROMPT_COL_")]:
                os.environ.pop(k)
            importlib.reload(feed)
            cols = self._cols_by_name()
            assert cols["Time"].kwargs["width"] == 8
            assert cols["Session"].kwargs["width"] == 13
            assert cols["Project"].kwargs["width"] == 16
            # 14 so the widest real cell (`185 (82 bash)`) fits without an ellipsis.
            assert cols["Calls"].kwargs["width"] == 14
            assert cols["Agents"].kwargs["width"] == 6
            assert cols["Tokens"].kwargs["width"] == 16
            assert feed.PROJECT_LABEL_WIDTH == 16


class TestHookMain:
    def _run_hook(self, tmp_path, payload):
        import io
        import json
        log_file = tmp_path / "command-log.jsonl"
        with patch.object(feed, "LOG_PATH", log_file):
            stream = io.StringIO(json.dumps(payload))
            rc = feed.hook_main(stream=stream)
        return rc, log_file

    def test_bash_event_writes_entry(self, tmp_path):
        import json
        rc, log = self._run_hook(tmp_path, {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "cwd": "/work",
            "session_id": "abcdef12",
        })
        assert rc == 0
        line = log.read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        assert entry["command"] == "ls -la"
        assert entry["cwd"] == "/work"
        assert entry["session_id"] == "abcdef12"
        from datetime import datetime as _dt
        parsed = _dt.fromisoformat(entry["timestamp"])
        assert parsed.tzinfo is not None  # tz-aware (local offset)

    def test_non_bash_event_writes_nothing(self, tmp_path):
        rc, log = self._run_hook(tmp_path, {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/foo"},
        })
        assert rc == 0
        assert not log.exists() or log.read_text() == ""

    def test_malformed_json_does_not_crash(self, tmp_path):
        import io
        log = tmp_path / "command-log.jsonl"
        with patch.object(feed, "LOG_PATH", log):
            rc = feed.hook_main(stream=io.StringIO("not json at all"))
        assert rc == 0
        assert not log.exists()

    def test_missing_optional_fields_default_to_empty(self, tmp_path):
        import json
        rc, log = self._run_hook(tmp_path, {"tool_name": "Bash"})
        assert rc == 0
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["command"] == ""
        assert entry["cwd"] == ""
        assert entry["session_id"] == ""

    def test_creates_parent_directory(self, tmp_path):
        import io
        import json
        log = tmp_path / "nested" / "subdir" / "command-log.jsonl"
        with patch.object(feed, "LOG_PATH", log):
            rc = feed.hook_main(stream=io.StringIO(json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            })))
        assert rc == 0
        assert log.exists()

    def test_subagent_event_records_agent(self, tmp_path):
        import json
        rc, log = self._run_hook(tmp_path, {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "cwd": "/work",
            "session_id": "abcdef12",
            "agent_id": "af6978028df59ded3",
            "agent_type": "general-purpose",
        })
        assert rc == 0
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["agent_id"] == "af6978028df59ded3"
        assert entry["agent_type"] == "general-purpose"

    def test_main_agent_event_omits_agent_keys(self, tmp_path):
        # No agent_id in the payload (main thread) -> no agent_* keys written,
        # so absent agent_id can mean "the main agent" downstream.
        import json
        rc, log = self._run_hook(tmp_path, {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "abcdef12",
        })
        assert rc == 0
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert "agent_id" not in entry
        assert "agent_type" not in entry

    def test_agent_type_defaults_to_empty_when_missing(self, tmp_path):
        # agent_id present but agent_type absent -> agent_type stored as "".
        import json
        rc, log = self._run_hook(tmp_path, {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "agent_id": "af69",
        })
        assert rc == 0
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["agent_id"] == "af69"
        assert entry["agent_type"] == ""


class TestReadKey:
    """read_key decodes exactly one escape sequence per call and leaves any
    following bytes on the fd (so queued arrow keys are not dropped)."""

    def _feed(self, data: bytes) -> int:
        import os
        r, w = os.pipe()
        os.write(w, data)
        os.close(w)  # EOF after the queued bytes so a lone-ESC select returns at once
        return r

    def _read_and_close(self, data: bytes, calls: int = 1):
        import os
        r = self._feed(data)
        try:
            return [feed.read_key(r) for _ in range(calls)]
        finally:
            os.close(r)

    def test_plain_char(self):
        assert self._read_and_close(b"j") == ["j"]

    def test_csi_up(self):
        assert self._read_and_close(b"\x1b[A") == ["up"]

    def test_csi_down(self):
        assert self._read_and_close(b"\x1b[B") == ["down"]

    def test_ss3_up(self):
        assert self._read_and_close(b"\x1bOA") == ["up"]

    def test_ss3_down(self):
        assert self._read_and_close(b"\x1bOB") == ["down"]

    def test_lone_esc_returns_esc(self):
        assert self._read_and_close(b"\x1b") == ["\x1b"]

    def test_two_queued_arrows_not_dropped(self):
        assert self._read_and_close(b"\x1b[B\x1b[B", calls=2) == ["down", "down"]

    def test_mixed_arrow_then_char_not_dropped(self):
        assert self._read_and_close(b"\x1b[Aj", calls=2) == ["up", "j"]

    def test_modified_arrow_consumed_whole(self):
        # Ctrl-Up (ESC [ 1 ; 5 A) must not leak ';','5','A' as stray keys
        # ('5' would toggle the Command column); the whole CSI sequence is
        # consumed and the modifier is ignored.
        assert self._read_and_close(b"\x1b[1;5Aj", calls=2) == ["up", "j"]

    def test_shift_down_decodes(self):
        assert self._read_and_close(b"\x1b[1;2B") == ["down"]

    def test_function_key_consumed_whole(self):
        # F5 (ESC [ 1 5 ~) decodes to a bare ESC with nothing left on the fd
        assert self._read_and_close(b"\x1b[15~j", calls=2) == ["\x1b", "j"]

    def test_da_response_consumed_whole(self):
        # a terminal DA response must not leak 'c' (the clear-display key)
        assert self._read_and_close(b"\x1b[?1;2cj", calls=2) == ["\x1b", "j"]


class TestSignalExit:
    def test_raises_system_exit(self):
        import pytest
        with pytest.raises(SystemExit):
            feed._signal_exit(15, None)

    def test_exit_code_is_128_plus_signum(self):
        import pytest
        import signal as _signal
        with pytest.raises(SystemExit) as exc:
            feed._signal_exit(int(_signal.SIGHUP), None)
        assert exc.value.code == 128 + int(_signal.SIGHUP)


class TestTailFile:
    def test_returns_complete_lines_and_offset(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("a\nb\n", encoding="utf-8")
        lines, pos = feed.tail_file(p, 0)
        assert lines == ["a\n", "b\n"]
        assert pos == p.stat().st_size

    def test_partial_trailing_line_withheld_then_returned(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("a\nb", encoding="utf-8")  # 'b' not yet newline-terminated
        lines, pos = feed.tail_file(p, 0)
        assert lines == ["a\n"]
        assert pos == 2
        p.write_text("a\nb\n", encoding="utf-8")  # line completes
        lines2, pos2 = feed.tail_file(p, pos)
        assert lines2 == ["b\n"]
        assert pos2 == p.stat().st_size

    def test_no_newline_yet_returns_nothing(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("partial", encoding="utf-8")
        assert feed.tail_file(p, 0) == ([], 0)

    def test_truncation_resets_to_zero(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("aaaa\nbbbb\n", encoding="utf-8")
        big = p.stat().st_size
        p.write_text("x\n", encoding="utf-8")  # shrinks below last_pos
        lines, pos = feed.tail_file(p, big)
        assert lines == ["x\n"]
        assert pos == p.stat().st_size

    def test_no_change_returns_empty(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("a\n", encoding="utf-8")
        size = p.stat().st_size
        assert feed.tail_file(p, size) == ([], size)

    def test_missing_file(self, tmp_path):
        assert feed.tail_file(tmp_path / "absent.jsonl", 0) == ([], 0)


class TestReadLastEntries:
    def _line(self, i, **extra):
        import json
        d = {"command": f"cmd{i}", "session_id": "s", "cwd": "/w", "timestamp": "t"}
        d.update(extra)
        return json.dumps(d) + "\n"

    def test_fewer_than_n_returns_all(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text(self._line(0) + self._line(1), encoding="utf-8")
        entries, pos = feed.read_last_entries(p, 10)
        assert [e["command"] for e in entries] == ["cmd0", "cmd1"]
        assert pos == p.stat().st_size

    def test_more_than_n_returns_last_n_in_order(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("".join(self._line(i) for i in range(10)), encoding="utf-8")
        entries, _ = feed.read_last_entries(p, 3)
        assert [e["command"] for e in entries] == ["cmd7", "cmd8", "cmd9"]

    def test_last_n_span_chunk_boundary(self, tmp_path):
        # Pad each line so the last 15 lines span more than one 8192-byte
        # backward-read chunk, exercising the reassembly loop.
        p = tmp_path / "log.jsonl"
        p.write_text("".join(self._line(i, pad="x" * 1000) for i in range(20)), encoding="utf-8")
        entries, pos = feed.read_last_entries(p, 15)
        assert len(entries) == 15
        assert entries[0]["command"] == "cmd5"
        assert entries[-1]["command"] == "cmd19"
        assert pos == p.stat().st_size

    def test_malformed_lines_dropped(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text(self._line(0) + "not json\n" + self._line(1), encoding="utf-8")
        entries, _ = feed.read_last_entries(p, 10)
        assert [e["command"] for e in entries] == ["cmd0", "cmd1"]

    def test_missing_file(self, tmp_path):
        assert feed.read_last_entries(tmp_path / "absent", 5) == ([], 0)


class TestRendering:
    ALL = {1: True, 2: True, 3: True, 4: True, 5: True}

    def _entry(self, **kw):
        e = {"timestamp": "2025-01-01T12:00:00+00:00", "command": "ls -la",
             "cwd": "/work", "session_id": "abcd1234"}
        e.update(kw)
        return e

    def _text(self, panel):
        console = Console(width=120, record=True)
        console.print(panel)
        return console.export_text()

    def test_empty_shows_waiting(self):
        out = self._text(feed.build_panel([], 40, self.ALL, 0))
        assert "Waiting for commands…" in out

    def test_dangerous_entry_has_marker(self):
        out = self._text(feed.build_panel([self._entry(command="rm -rf /tmp/x")], 40, self.ALL, 0))
        assert "* " in out

    def test_benign_entry_has_no_marker(self):
        out = self._text(feed.build_panel([self._entry(command="ls -la")], 40, self.ALL, 0))
        assert "ls -la" in out
        assert "* " not in out

    def test_column_toggle_bar_and_hidden_column(self):
        cols = {1: True, 2: True, 3: False, 4: True, 5: True}
        out = self._text(feed.build_panel([self._entry()], 40, cols, 0))
        assert "cols:" in out
        assert "Directory" not in out  # column 3 hidden -> header absent

    def test_cursor_position_in_status(self):
        entries = [self._entry(command=f"cmd{i}") for i in range(3)]
        out = self._text(feed.build_panel(entries, 40, self.ALL, 0))
        assert "1/3" in out

    def test_detail_panel_shows_full_command_and_marker(self):
        out = self._text(feed.build_detail_panel(self._entry(command="sudo rm -rf /"), 40))
        assert "* " in out
        assert "sudo rm -rf /" in out

    def test_detail_panel_shows_agent_row_for_subagent(self):
        amap = {"af6978": {"type": "general-purpose", "description": "Implement Phase 4"}}
        entry = self._entry(agent_id="af6978")
        out = self._text(feed.build_detail_panel(entry, 40, agent_map=amap))
        assert "Agent" in out
        assert "af69 Implement Phase 4" in out  # the full agent_label

    def test_detail_panel_agent_row_uses_agent_style(self):
        from rich.text import Text
        amap = {"af6978": {"type": "general-purpose", "description": "Implement Phase 4"}}
        label = feed.agent_label({"agent_id": "af6978"}, amap)
        console = Console(width=120, record=True, force_terminal=True,
                          color_system="truecolor")
        console.print(feed.build_detail_panel(self._entry(agent_id="af6978"), 40,
                                              agent_map=amap))
        styled = console.export_text(styles=True)
        # the Agent label is tinted with AGENT_STYLE (the shared Agent-column color)
        ref = Console(force_terminal=True, color_system="truecolor")
        with ref.capture() as cap:
            ref.print(Text(label, style=feed.AGENT_STYLE), end="")
        assert cap.get() in styled

    def test_detail_panel_main_agent_has_no_agent_row(self):
        # a main-agent entry (no agent_id) omits the Agent row entirely
        out = self._text(feed.build_detail_panel(self._entry(), 40, agent_map={}))
        assert "Agent" not in out

    def test_detail_panel_shows_tokens_when_present(self):
        tokens = {"output_tokens": 1234, "calls_in_request": 1,
                  "share": 1234.0, "model": "claude-opus-4-8"}
        out = self._text(feed.build_detail_panel(self._entry(), 40, tokens=tokens))
        assert "Tokens" in out
        assert "1,234" in out                # thousands-separated share
        assert "claude-opus-4-8" in out

    def test_detail_panel_omits_tokens_when_none(self):
        out = self._text(feed.build_detail_panel(self._entry(), 40, tokens=None))
        assert "Tokens" not in out

    def test_detail_panel_previews_prompt_and_counts_hidden_lines(self):
        # The prompt panel shows the first PROMPT_PREVIEW_LINES lines and says how
        # many are left, so a long prompt never looks truncated-by-accident.
        prompt = {"text": "\n".join(f"line {i}" for i in range(1, 15)), "ts": ""}
        out = self._text(feed.build_detail_panel(self._entry(), 60, prompt=prompt))
        assert "prompt" in out
        assert "line 10" in out            # last previewed line
        assert "line 11" not in out        # first hidden line
        assert "+4 more lines" in out
        assert "↵ : full prompt" in out

    def test_detail_panel_short_prompt_has_no_more_lines_footer(self):
        prompt = {"text": "just one line", "ts": ""}
        out = self._text(feed.build_detail_panel(self._entry(), 40, prompt=prompt))
        assert "just one line" in out
        assert "more line" not in out

    def test_detail_panel_omits_prompt_when_none(self):
        out = self._text(feed.build_detail_panel(self._entry(), 40, prompt=None))
        assert "prompt" not in out
        assert "esc / q / ↵ : back" in out   # Enter still just closes

    def test_detail_panel_prompt_unavailable_shows_hint(self):
        hint = {"unavailable": "pending ingest (store lags the live feed)"}
        out = self._text(feed.build_detail_panel(self._entry(), 40, prompt=hint))
        assert "pending ingest" in out
        # nothing to drill into: Enter keeps its close meaning
        assert "esc / q / ↵ : back" in out

    def _styled(self, panel):
        console = Console(width=120, record=True, force_terminal=True,
                          color_system="truecolor")
        console.print(panel)
        return console.export_text(styles=True)

    def _rendered(self, text):
        """The ANSI a `Text` renders to, for an `in` test against a panel."""
        from rich.console import Console as _C
        ref = _C(force_terminal=True, color_system="truecolor")
        with ref.capture() as cap:
            ref.print(text, end="")
        return cap.get()

    def test_detail_panel_colors_the_command(self):
        from rich.text import Text
        styled = self._styled(feed.build_detail_panel(
            self._entry(command="cat /tmp/x"), 40))
        # the palette reaches the panel: the command name is bold, the path blue
        assert self._rendered(Text("cat", style=feed.CMD_STYLES["command"])) in styled
        assert self._rendered(Text("/tmp/x", style=feed.CMD_STYLES["path"])) in styled

    def test_detail_panel_keeps_the_marker_before_the_colored_command(self):
        from rich.text import Text
        panel = feed.build_detail_panel(self._entry(command="sudo rm -rf /tmp/x"), 40)
        styled = self._styled(panel)
        assert self._rendered(Text("* ", style="bold red")) in styled
        assert self._rendered(Text("sudo", style=feed.CMD_STYLES["command"])) in styled
        assert "sudo rm -rf /tmp/x" in self._text(panel)   # still readable plain

    def test_detail_panel_preserves_newlines_in_a_colored_command(self):
        # The full-screen view keeps the raw command, so a heredoc still reads
        # line by line (and its body is dim, not colored as shell).
        cmd = "cat <<'EOF'\nline one\nline two\nEOF"
        out = self._text(feed.build_detail_panel(self._entry(command=cmd), 40))
        for line in ("cat <<'EOF'", "line one", "line two", "EOF"):
            assert line in out

    def test_detail_panel_empty_command_keeps_the_placeholder(self):
        out = self._text(feed.build_detail_panel(self._entry(command=""), 40))
        assert "(empty)" in out

    def test_detail_panel_command_markup_is_literal(self):
        # The command is now a Text built by `highlight_command`, so console
        # markup in it is still text: `[bold]` is not swallowed and `[/tmp/x]`
        # does not raise MarkupError out of the live update.
        for probe in ("[/tmp/x]", "[bold]", "[/]"):
            out = self._text(feed.build_detail_panel(
                self._entry(command=f"echo {probe}"), 40))
            assert probe in out, probe

    def test_detail_panel_notes_shared_request(self):
        # A request that emitted 2 parallel tool calls: the share is half, and the
        # panel says so rather than implying the command cost the whole request.
        tokens = {"output_tokens": 200, "calls_in_request": 2,
                  "share": 100.0, "model": "claude-opus-4-8"}
        out = self._text(feed.build_detail_panel(self._entry(), 40, tokens=tokens))
        assert "1/2 of a 2-call request" in out
        assert "100" in out


class TestCountActiveSessions:
    """count_active_sessions: distinct session_ids seen within the window."""

    def _now(self):
        from datetime import datetime as _dt, timezone as _tz
        return _dt(2025, 1, 1, 12, 0, 0, tzinfo=_tz.utc)

    def _entry(self, sid, ts):
        return {"session_id": sid, "timestamp": ts}

    def _ago(self, now, seconds):
        from datetime import timedelta
        return (now - timedelta(seconds=seconds)).isoformat()

    def test_empty_entries(self):
        assert feed.count_active_sessions([], self._now()) == 0

    def test_entry_just_inside_window_counts(self):
        now = self._now()
        assert feed.count_active_sessions([self._entry("s1", self._ago(now, 299))], now) == 1

    def test_entry_at_exact_window_excluded(self):
        # strict `<` means exactly `window` seconds ago is NOT active
        now = self._now()
        assert feed.count_active_sessions([self._entry("s1", self._ago(now, 300))], now) == 0

    def test_entry_just_outside_window_excluded(self):
        now = self._now()
        assert feed.count_active_sessions([self._entry("s1", self._ago(now, 301))], now) == 0

    def test_duplicate_session_ids_counted_once(self):
        now = self._now()
        entries = [
            self._entry("s1", self._ago(now, 10)),
            self._entry("s1", self._ago(now, 20)),
            self._entry("s2", self._ago(now, 30)),
        ]
        assert feed.count_active_sessions(entries, now) == 2

    def test_malformed_timestamp_ignored(self):
        now = self._now()
        entries = [
            self._entry("s1", "not-a-date"),
            self._entry("s2", self._ago(now, 5)),
        ]
        assert feed.count_active_sessions(entries, now) == 1

    def test_naive_timestamp_ignored(self):
        # A naive timestamp (no offset) vs an aware `now` raises TypeError in the
        # subtraction; the shared try/except swallows it so it is not counted.
        now = self._now()
        assert feed.count_active_sessions([self._entry("s1", "2025-01-01T12:00:00")], now) == 0

    def test_custom_window_is_honored(self):
        now = self._now()
        entries = [self._entry("s1", self._ago(now, 50))]
        assert feed.count_active_sessions(entries, now, window=60) == 1
        assert feed.count_active_sessions(entries, now, window=40) == 0

    def test_default_window_is_active_window_seconds(self):
        assert feed.ACTIVE_WINDOW_SECONDS == 300


class TestFilterSessionLog:
    def test_none_when_empty_session_id(self):
        assert feed.filter_session_log("") is None

    def test_none_when_log_missing(self, tmp_path):
        with patch.object(feed, "LOG_PATH", tmp_path / "absent.jsonl"):
            assert feed.filter_session_log("abc12345") is None

    def test_none_when_the_session_has_no_rows_in_the_log(self, tmp_path):
        """A session with no Bash command in the log has nothing to open. Reachable
        from the Prompts tab (the store reads transcripts, the log is Bash-only),
        where returning a path would open a 0-byte buffer in the user's editor
        with no explanation. Nothing is written either, so no empty temp file is
        left behind."""
        import json
        log = tmp_path / "command-log.jsonl"
        log.write_text(json.dumps({"session_id": "aaa", "command": "one"}) + "\n",
                       encoding="utf-8")
        with patch.object(feed, "LOG_PATH", log), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            assert feed.filter_session_log("bbb") is None
        assert not list(tmp_path.glob("claude-trail-session-*.json"))

    def test_filters_to_one_session(self, tmp_path):
        import json
        log = tmp_path / "command-log.jsonl"
        log.write_text(
            json.dumps({"session_id": "aaa", "command": "one"}) + "\n"
            + json.dumps({"session_id": "bbb", "command": "two"}) + "\n"
            + json.dumps({"session_id": "aaa", "command": "three"}) + "\n",
            encoding="utf-8",
        )
        with patch.object(feed, "LOG_PATH", log), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            out = feed.filter_session_log("aaa")
        content = Path(out).read_text(encoding="utf-8")
        assert '"command": "one"' in content
        assert '"command": "three"' in content
        assert '"two"' not in content

    def test_output_is_pretty_printed_json(self, tmp_path):
        """`o` output is for human eyes only: entries are re-emitted indented
        (blank-line separated blocks, like the `t` transcript), not raw JSONL."""
        import json
        log = tmp_path / "command-log.jsonl"
        log.write_text(
            json.dumps({"session_id": "aaa", "command": "one"}) + "\n"
            + json.dumps({"session_id": "aaa", "command": "two"}) + "\n",
            encoding="utf-8",
        )
        with patch.object(feed, "LOG_PATH", log), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            out = feed.filter_session_log("aaa")
        assert out.endswith(".json")                      # not .jsonl anymore
        content = Path(out).read_text(encoding="utf-8")
        assert '\n  "command"' in content                 # indented fields
        blocks = content.split("\n\n")
        assert len(blocks) == 2                           # one block per entry
        assert all(json.loads(b)["session_id"] == "aaa" for b in blocks)

    def test_sanitizes_session_id_in_filename(self, tmp_path):
        import json
        log = tmp_path / "command-log.jsonl"
        log.write_text(json.dumps({"session_id": "../../etc", "command": "x"}) + "\n", encoding="utf-8")
        with patch.object(feed, "LOG_PATH", log), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            out = feed.filter_session_log("../../etc")
        base = os.path.basename(out)
        assert ".." not in base and "/" not in base
        assert base.startswith("claude-trail-session-")


class TestFormatSessionTranscript:
    """format_session_transcript pretty-prints a session transcript to a temp
    `.json` file (one indented block per line, blank-separated) or None when the
    transcript can't be resolved."""

    def _write_transcript(self, projects_root, sid, lines):
        project = projects_root / "-home-naka-Projects-x"
        project.mkdir(parents=True, exist_ok=True)
        path = project / f"{sid}.jsonl"
        path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
        feed._transcript_path_cache.pop(sid, None)  # avoid a stale cached path
        return path

    def test_pretty_output_round_trips_to_source_objects(self, tmp_path):
        import json
        sid = "trans-rt"
        source = [
            {"type": "user", "content": "hello", "n": 1},
            {"type": "assistant", "content": ["a", "b"], "nested": {"x": 2}},
            {"type": "system", "unicode": "café"},
        ]
        self._write_transcript(tmp_path, sid, [json.dumps(o) for o in source])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            out = feed.format_session_transcript(sid)
        content = Path(out).read_text(encoding="utf-8")
        blocks = content.split("\n\n")
        assert [json.loads(b) for b in blocks] == source

    def test_output_is_indented_and_json_extension(self, tmp_path):
        import json
        sid = "trans-fmt"
        self._write_transcript(tmp_path, sid, [json.dumps({"a": 1})])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            out = feed.format_session_transcript(sid)
        assert out.endswith(".json")
        assert os.path.basename(out).startswith("claude-trail-transcript-")
        assert '  "a": 1' in Path(out).read_text(encoding="utf-8")  # indent=2

    def test_malformed_line_survives_verbatim(self, tmp_path):
        import json
        sid = "trans-bad"
        self._write_transcript(tmp_path, sid, [
            json.dumps({"ok": True}),
            "this is not json {{{",
            json.dumps({"ok": False}),
        ])
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            out = feed.format_session_transcript(sid)
        content = Path(out).read_text(encoding="utf-8")
        assert "this is not json {{{" in content

    def test_missing_transcript_returns_none(self, tmp_path):
        feed._transcript_path_cache.pop("never-existed", None)
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed.tempfile, "gettempdir", return_value=str(tmp_path)):
            assert feed.format_session_transcript("never-existed") is None


class TestOpenFileFolder:
    def test_opens_parent_of_absolute_file(self, tmp_path):
        f = tmp_path / "sub" / "file.txt"
        f.parent.mkdir(parents=True)
        f.write_text("x")
        with patch.object(feed.subprocess, "Popen") as popen:
            feed.open_file_folder({"command": f"cat {f}", "cwd": ""})
        popen.assert_called_once()
        assert os.path.realpath(popen.call_args[0][0][-1]) == os.path.realpath(str(f.parent))

    def test_relative_path_joined_with_cwd(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "file.txt").write_text("x")
        with patch.object(feed.subprocess, "Popen") as popen:
            feed.open_file_folder({"command": "cat ./sub/file.txt", "cwd": str(tmp_path)})
        assert os.path.realpath(popen.call_args[0][0][-1]) == os.path.realpath(str(sub))

    def test_falls_back_to_cwd(self, tmp_path):
        with patch.object(feed.subprocess, "Popen") as popen:
            feed.open_file_folder({"command": "echo hi", "cwd": str(tmp_path)})
        assert os.path.realpath(popen.call_args[0][0][-1]) == os.path.realpath(str(tmp_path))

    def test_no_popen_when_nothing_resolves(self, tmp_path):
        with patch.object(feed.subprocess, "Popen") as popen:
            feed.open_file_folder({"command": "echo hi", "cwd": str(tmp_path / "absent")})
        popen.assert_not_called()


class TestColumnRender:
    """Each Column's `render(entry, ctx)` produces the right cell, and adding a
    Column is a single list entry (data-driven columns)."""

    def _entry(self, **kw):
        e = {"timestamp": "2025-01-01T12:00:00+00:00", "command": "ls -la",
             "cwd": "/work", "session_id": "abcd1234"}
        e.update(kw)
        return e

    def _col(self, key):
        return next(c for c in feed.COLUMNS if c.key == key)

    def test_time_column_renders_hms(self):
        from datetime import datetime as _dt, timezone as _tz
        expected = _dt(2025, 1, 1, 12, 0, 0, tzinfo=_tz.utc).astimezone().strftime("%H:%M:%S")
        assert self._col(1).render(self._entry(), feed.RenderCtx()) == expected

    def test_session_column_applies_color_tint(self):
        from rich.text import Text
        # "orange" must be mapped through rich_color -> "orange1"
        ctx = feed.RenderCtx(color_map={"abcd1234": "orange"})
        cell = self._col(2).render(self._entry(), ctx)
        assert isinstance(cell, Text)
        assert cell.style == "orange1"
        assert cell.plain == "abcd1234"

    def test_session_column_untinted_is_an_unstyled_text(self):
        # Text with no style, not a bare str: the label can be a name the user
        # typed, so it must never reach Rich as markup (see the class comment
        # above `_danger_prefixed` and TestConsoleMarkupIsNeverParsed).
        from rich.text import Text
        cell = self._col(2).render(self._entry(), feed.RenderCtx())
        assert isinstance(cell, Text)
        assert cell.plain == "abcd1234"
        assert not cell.style          # no tint

    def test_directory_column_shortens_cwd(self):
        # a swap of _render_directory / _render_files in COLUMNS must fail here
        cell = self._col(3).render(self._entry(cwd="/home/user/project"), feed.RenderCtx())
        assert cell.plain == feed.short_path("/home/user/project")

    def test_files_column_extracts_basenames(self):
        cell = self._col(4).render(self._entry(command="cat /etc/hosts"), feed.RenderCtx())
        assert cell.plain == "hosts"

    def test_command_column_marks_dangerous(self):
        from rich.text import Span, Text
        cell = self._col(5).render(self._entry(command="rm -rf /tmp/x"), feed.RenderCtx())
        assert isinstance(cell, Text)
        assert cell.plain.startswith("* ")
        assert Span(0, 2, "bold red") in cell.spans  # marker keeps its style

    def test_command_column_benign_has_no_marker(self):
        cell = self._col(5).render(self._entry(command="ls -la"), feed.RenderCtx())
        assert not cell.plain.startswith("* ")
        assert cell.plain == "ls -la"

    def test_appending_one_column_shows_in_table(self, monkeypatch):
        """One extra Column entry is enough to add a rendered column end-to-end.

        build_table reads the module-level COLUMNS list directly; monkeypatch
        restores it after the test so no global state leaks.
        """
        marker = "EXTRA_MARK"
        extra = feed.Column(
            key=6, name="Extra", style="white",
            kwargs={"width": 20, "no_wrap": True},
            render=lambda entry, ctx: marker,
        )
        monkeypatch.setattr(feed, "COLUMNS", feed.COLUMNS + [extra])
        cols = {c.key: True for c in feed.COLUMNS}
        console = Console(width=200, record=True)
        console.print(feed.build_table([self._entry()], 20, cols, 0))
        out = console.export_text()
        assert "Extra" in out   # header rendered
        assert marker in out    # cell rendered


class TestAgentTree:
    """The Agent column (key 6): tree glyphs over consecutive
    (session_id, agent_id) runs, label shown once, blank for the main agent."""

    def _entry(self, sid, cmd, aid=None):
        e = {"timestamp": "2025-01-01T12:00:00+00:00", "command": cmd,
             "cwd": "/work", "session_id": sid}
        if aid:
            e["agent_id"] = aid
            e["agent_type"] = "general-purpose"
        return e

    def _agent_col(self):
        return next(c for c in feed.COLUMNS if c.key == 6)

    def _text(self, table, width=140):
        console = Console(width=width, record=True)
        console.print(table)
        return console.export_text()

    def test_main_agent_row_renders_blank(self):
        from rich.text import Text
        ctx = feed.RenderCtx(agent_label=None, run_first=True, run_last=True)
        cell = self._agent_col().render(self._entry("s1", "x"), ctx)
        assert isinstance(cell, Text)
        assert cell.plain == ""

    def test_single_row_run_uses_dash_glyph_with_label(self):
        ctx = feed.RenderCtx(agent_label="Solo", run_first=True, run_last=True)
        cell = self._agent_col().render(self._entry("s1", "x", aid="a1"), ctx)
        assert cell.plain == "─ Solo"

    def test_run_glyphs_and_label_only_on_first_row(self):
        col = self._agent_col()
        top = col.render(self._entry("s1", "x", aid="a1"),
                         feed.RenderCtx(agent_label="Run", run_first=True, run_last=False))
        mid = col.render(self._entry("s1", "x", aid="a1"),
                         feed.RenderCtx(agent_label="Run", run_first=False, run_last=False))
        bot = col.render(self._entry("s1", "x", aid="a1"),
                         feed.RenderCtx(agent_label="Run", run_first=False, run_last=True))
        assert top.plain == "┌ Run"       # label on the first (newest) row only
        assert mid.plain == "│ "          # continuation carries the connector alone
        assert bot.plain == "└ "

    def test_consecutive_run_shows_label_once_between_connectors(self):
        # oldest-first; display reverses to [sub two, sub one, main]
        entries = [
            self._entry("s1", "main cmd"),
            self._entry("s1", "sub one", aid="a1"),
            self._entry("s1", "sub two", aid="a1"),
        ]
        amap = {"a1": {"type": "general-purpose", "description": "reviewer run"}}
        cols = {c.key: True for c in feed.COLUMNS}
        out = self._text(feed.build_table(entries, 10, cols, 0, agent_map=amap))
        assert out.count("reviewer run") == 1  # label shown once per run
        assert out.count("┌") == 1 and out.count("└") == 1  # a single top/bottom pair
        assert "│" not in out  # a two-row run has no middle connector; main row blank

    def test_different_agent_id_breaks_the_run(self):
        # two same-session subagents with different ids must not merge into one run
        entries = [
            self._entry("s1", "one", aid="a1"),
            self._entry("s1", "two", aid="a2"),
        ]
        amap = {"a1": {"type": "gp", "description": "first"},
                "a2": {"type": "gp", "description": "second"}}
        cols = {c.key: True for c in feed.COLUMNS}
        out = self._text(feed.build_table(entries, 10, cols, 0, agent_map=amap))
        assert out.count("─") >= 2  # each is its own single-row run
        assert "first" in out and "second" in out

    def test_same_agent_id_across_sessions_breaks_the_run(self):
        # the run key is (session_id, agent_id): the same agent id in two
        # different sessions must not merge into a single run.
        entries = [
            self._entry("s1", "one", aid="a1"),
            self._entry("s2", "two", aid="a1"),
        ]
        amap = {"a1": {"type": "gp", "description": "shared"}}
        cols = {c.key: True for c in feed.COLUMNS}
        out = self._text(feed.build_table(entries, 10, cols, 0, agent_map=amap))
        assert out.count("─") == 2       # two separate single-row runs
        assert out.count("┌") == 0       # neither is a multi-row run
        assert out.count("shared") == 2  # label re-shown for each run

    def test_scroll_offset_reanchors_run_label_at_slice_top(self):
        # a run longer than the visible slice: with a nonzero offset the label
        # and top connector re-anchor at the top of the slice (slice-local
        # run detection), keeping the label visible while scrolling.
        entries = [self._entry("s1", f"cmd {i}", aid="a1") for i in range(5)]
        amap = {"a1": {"type": "gp", "description": "long run"}}
        cols = {c.key: True for c in feed.COLUMNS}
        out = self._text(
            feed.build_table(entries, 10, cols, 0, offset=2, agent_map=amap))
        assert out.count("long run") == 1  # label shown once, at the slice top
        assert out.count("┌") == 1         # top connector re-anchored at slice edge
        assert out.count("└") == 1         # slice bottom closes the run
        assert "│" in out                  # a middle continuation row remains

    def test_toggle_six_hides_agent_column(self):
        entries = [self._entry("s1", "cmd", aid="a1")]
        amap = {"a1": {"type": "general-purpose", "description": "solo task"}}
        cols = {c.key: True for c in feed.COLUMNS}
        cols[6] = False
        out = self._text(feed.build_table(entries, 10, cols, 0, agent_map=amap))
        assert "Agent" not in out       # header gone
        assert "solo task" not in out   # label not rendered

    def test_apply_key_six_toggles_agent_column(self):
        state = feed.AppState(entries=[])
        assert state.visible_cols[6] is True
        assert feed.apply_key(state, "6", 10) is feed.Action.NONE
        assert state.visible_cols[6] is False

    def test_last_visible_column_guard_holds_for_agent_column(self):
        state = feed.AppState(entries=[], visible_cols={
            1: False, 2: False, 3: False, 4: False, 5: False, 6: True})
        state.toggle_col(6)
        assert state.visible_cols[6] is True  # cannot hide the last visible column


class TestVisibleRowCount:
    def test_subtracts_chrome_rows(self):
        assert feed.visible_row_count(40) == 40 - feed.CHROME_ROWS

    def test_floors_at_min_visible_rows(self):
        assert feed.visible_row_count(1) == feed.MIN_VISIBLE_ROWS

    def _panel_lines(self, width, height=24):
        """Render the Commands panel with every column at `width` and return its
        exported lines. One body row per entry, each carrying the session cell."""
        entries = [{"timestamp": "2025-01-01T12:00:00+00:00", "command": f"cmd{i}",
                    "cwd": "/work", "session_id": "abcd1234"} for i in range(60)]
        cols = {c.key: True for c in feed.COLUMNS}
        console = Console(width=width, record=True)
        console.print(feed.build_panel(entries, height, cols, 0))
        return console.export_text().splitlines()

    def test_chrome_rows_matches_the_rendered_panel(self):
        """CHROME_ROWS must equal the panel's real non-body height, or the row
        math silently disagrees with the render: too low and Rich crops the
        bottom (the key hints go first), too high and a body row is wasted.

        Both widths matter. At 160 the hints fit on one line; at 80 they wrap to
        two, which is what the constant's slack line has to absorb - so 80 is the
        width that actually distinguishes 12 from 11.
        """
        for width in (80, 160):
            lines = self._panel_lines(width)
            out = "\n".join(lines)
            assert len(lines) == 24, width
            assert out.count("abcd1234") == feed.visible_row_count(24), width
            assert "c:clear" in out, width   # the hint tail, first thing a crop eats

    def test_chrome_rows_is_not_one_row_too_many(self):
        """The other side of the pin: CHROME_ROWS must be the SMALLEST value that
        fits, so one less has to overflow. Without this a too-generous constant
        renders cleanly while wasting a body row at every terminal size."""
        with patch.object(feed, "CHROME_ROWS", feed.CHROME_ROWS - 1):
            out = "\n".join(self._panel_lines(80))
            assert "c:clear" not in out


class TestAppState:
    """Pure view-state logic lifted out of main()."""

    def _entries(self, n):
        # oldest-first (append order): c0 is oldest, c{n-1} is newest.
        return [{"command": f"c{i}"} for i in range(n)]

    def test_default_visible_cols_all_true(self):
        state = feed.AppState(entries=[])
        assert state.visible_cols == {c.key: True for c in feed.COLUMNS}

    def test_selected_entry_none_when_empty(self):
        assert feed.AppState(entries=[]).selected_entry() is None

    def test_cursor_zero_selects_newest(self):
        state = feed.AppState(entries=self._entries(5))
        assert state.selected_entry() == {"command": "c4"}

    def test_goto_bottom_selects_oldest(self):
        state = feed.AppState(entries=self._entries(5))
        state.goto_bottom()
        assert state.cursor == 4
        assert state.selected_entry() == {"command": "c0"}

    def test_move_up_at_top_stays_zero(self):
        state = feed.AppState(entries=self._entries(3), cursor=0)
        state.move_up()
        assert state.cursor == 0

    def test_move_down_clamps_to_last(self):
        state = feed.AppState(entries=self._entries(3), cursor=2)
        state.move_down()
        assert state.cursor == 2

    def test_toggle_col_hides_visible_column(self):
        state = feed.AppState(entries=[])
        state.toggle_col(3)
        assert state.visible_cols[3] is False

    def test_toggle_col_refuses_to_hide_final_visible(self):
        state = feed.AppState(
            entries=[], visible_cols={1: True, 2: False, 3: False, 4: False, 5: False}
        )
        state.toggle_col(1)
        assert state.visible_cols[1] is True  # cannot hide the last visible column

    def test_ingest_at_top_keeps_cursor_zero(self):
        state = feed.AppState(entries=self._entries(1))
        added = state.ingest(['{"command": "c1"}\n', '{"command": "c2"}\n'])
        assert added == 2
        assert state.cursor == 0
        assert state.scroll_offset == 0

    def test_ingest_reanchors_when_scrolled(self):
        state = feed.AppState(entries=self._entries(2), cursor=1, scroll_offset=1)
        added = state.ingest(['{"command": "c2"}\n'])
        assert added == 1
        assert state.cursor == 2       # 1 + new_count
        assert state.scroll_offset == 2

    def test_ingest_skips_malformed_lines(self):
        state = feed.AppState(entries=[])
        added = state.ingest(["not json\n", '{"command": "ok"}\n'])
        assert added == 1
        assert state.entries == [{"command": "ok"}]

    def test_ingest_trims_to_max_entries(self):
        state = feed.AppState(entries=self._entries(feed.MAX_ENTRIES))
        state.ingest(['{"command": "overflow"}\n'])
        assert len(state.entries) == feed.MAX_ENTRIES
        assert state.entries[-1] == {"command": "overflow"}

    def test_ingest_does_not_clamp_may_leave_cursor_out_of_bounds(self):
        # ingest re-anchors and trims but deliberately does NOT clamp; the cursor
        # can legitimately exceed len-1 until the caller clamps. Do NOT "fix" this
        # by clamping inside ingest - it would break anchored scrolling.
        n = feed.MAX_ENTRIES
        state = feed.AppState(entries=self._entries(n), cursor=n - 1, scroll_offset=n - 1)
        state.ingest(['{"command": "a"}\n', '{"command": "b"}\n', '{"command": "c"}\n'])
        assert len(state.entries) == n                  # trimmed back to the cap
        assert state.cursor > len(state.entries) - 1    # re-anchored past the end, unclamped
        state.clamp(10)
        assert state.cursor == len(state.entries) - 1   # caller's clamp restores it

    def test_clamp_keeps_cursor_visible_after_shrink(self):
        state = feed.AppState(entries=self._entries(20), cursor=15, scroll_offset=0)
        state.clamp(5)
        assert state.cursor == 15
        assert state.scroll_offset <= state.cursor < state.scroll_offset + 5

    def test_clamp_empty_resets(self):
        state = feed.AppState(entries=[], cursor=9, scroll_offset=4)
        state.clamp(10)
        assert state.cursor == 0
        assert state.scroll_offset == 0


class TestApplyKey:
    def _entries(self, n):
        return [{"command": f"c{i}"} for i in range(n)]

    def test_q_quits(self):
        state = feed.AppState(entries=[])
        assert feed.apply_key(state, "q", 10) is feed.Action.QUIT

    def test_ctrl_c_quits(self):
        state = feed.AppState(entries=[])
        assert feed.apply_key(state, "\x03", 10) is feed.Action.QUIT

    def test_o_returns_open_session_without_moving_cursor(self):
        state = feed.AppState(entries=self._entries(3), cursor=1)
        result = feed.apply_key(state, "o", 10)
        assert result is feed.Action.OPEN_SESSION
        assert state.cursor == 1

    def test_t_returns_open_transcript_without_moving_cursor(self):
        state = feed.AppState(entries=self._entries(3), cursor=1)
        result = feed.apply_key(state, "t", 10)
        assert result is feed.Action.OPEN_TRANSCRIPT
        assert state.cursor == 1

    def test_t_is_inert_while_modal_open(self):
        entries = self._entries(3)
        state = feed.AppState(entries=entries, detail_entry=entries[0])
        result = feed.apply_key(state, "t", 10)
        assert result is feed.Action.NONE
        assert state.detail_entry is entries[0]  # modal stays open

    def test_f_returns_open_files_without_moving_cursor(self):
        state = feed.AppState(entries=self._entries(3), cursor=1)
        result = feed.apply_key(state, "f", 10)
        assert result is feed.Action.OPEN_FILES
        assert state.cursor == 1

    def test_j_moves_cursor_down(self):
        state = feed.AppState(entries=self._entries(3), cursor=0)
        result = feed.apply_key(state, "j", 10)
        assert result is feed.Action.NONE
        assert state.cursor == 1

    def test_enter_opens_detail(self):
        entries = self._entries(3)
        state = feed.AppState(entries=entries, cursor=0)
        result = feed.apply_key(state, "\r", 10)
        # Enter opens the modal AND asks main() to load the command's token cost.
        assert result is feed.Action.OPEN_DETAIL
        assert state.detail_entry == {"command": "c2"}  # newest

    def test_detail_mode_j_is_inert(self):
        entries = self._entries(3)
        state = feed.AppState(entries=entries, cursor=1, detail_entry=entries[0])
        result = feed.apply_key(state, "j", 10)
        assert result is feed.Action.NONE
        assert state.cursor == 1                 # navigation ignored while modal
        assert state.detail_entry is entries[0]  # still open

    def test_detail_mode_esc_closes(self):
        entries = self._entries(3)
        state = feed.AppState(entries=entries, detail_entry=entries[0])
        result = feed.apply_key(state, "\x1b", 10)
        assert result is feed.Action.NONE
        assert state.detail_entry is None

    def test_detail_mode_ctrl_c_still_quits(self):
        entries = self._entries(3)
        state = feed.AppState(entries=entries, detail_entry=entries[0])
        assert feed.apply_key(state, "\x03", 10) is feed.Action.QUIT

    def test_detail_mode_q_closes_not_quits(self):
        # q while the modal is open must CLOSE it, not quit the app: the modal
        # branch is checked before the global q/ctrl-c quit. Guards a reorder.
        entries = self._entries(3)
        state = feed.AppState(entries=entries, detail_entry=entries[0])
        result = feed.apply_key(state, "q", 10)
        assert result is feed.Action.NONE
        assert state.detail_entry is None

    def test_detail_mode_enter_closes_not_reopens(self):
        # With no prompt loaded there is nothing to drill into, so Enter keeps
        # its original one-key exit (see TestPromptView for the drill-in path).
        entries = self._entries(3)
        state = feed.AppState(entries=entries, detail_entry=entries[0])
        result = feed.apply_key(state, "\r", 10)
        assert result is feed.Action.NONE
        assert state.detail_entry is None

    def test_digit_toggles_column(self):
        state = feed.AppState(entries=[])
        result = feed.apply_key(state, "3", 10)
        assert result is feed.Action.NONE
        assert state.visible_cols[3] is False

    def test_clear_empties_entries(self):
        state = feed.AppState(entries=self._entries(5), cursor=3, scroll_offset=2)
        result = feed.apply_key(state, "c", 10)
        assert result is feed.Action.NONE
        assert state.entries == []
        assert state.cursor == 0
        assert state.scroll_offset == 0

    def test_o_is_inert_without_a_row(self):
        # Session-scoped keys need a row to resolve a session from; with an empty
        # feed they return no Action instead of one main() would have to no-op.
        state = feed.AppState(entries=[])
        assert feed.apply_key(state, "o", 10) is feed.Action.NONE
        assert feed.apply_key(state, "t", 10) is feed.Action.NONE


# ---- Prompts tab (phase 2: tab scaffolding) ----


def _turn(sid="sid-p", ts="2026-07-29 10:00:00-03", **kw):
    """A PromptTurn row without a DB (what the frozen record buys the tests)."""
    kw.setdefault("event_id", f"ev-{sid}")
    return feed.PromptTurn(session_id=sid, ts=ts, **kw)


def _prompts_state(rows=1, entries=1):
    """An AppState sitting on the Prompts tab with `rows` turns and `entries` feed
    rows, so a key can be shown to act on one list and not the other."""
    state = feed.AppState(
        entries=[{"command": f"c{i}", "session_id": f"sid-{i}"} for i in range(entries)]
    )
    state.prompts.rows = [_turn(sid=f"sid-p{i}", ts=f"2026-07-29 10:0{i}:00-03")
                          for i in range(rows)]
    state.tab = "prompts"
    return state


class TestTabs:
    """The two top-level views: TABS, the per-tab column registry, `Tab` cycling
    with each tab's own cursor, and the tab strip both panels draw."""

    ALL = {c.key: True for c in feed.COLUMNS}

    def _text(self, panel, width=120):
        console = Console(width=width, record=True)
        console.print(panel)
        return console.export_text()

    def test_tabs_are_ordered_with_commands_default(self):
        assert feed.TABS == ("commands", "prompts")
        assert feed.AppState(entries=[]).tab == "commands"  # the feed stays default

    def test_each_tab_registers_its_own_columns(self):
        # One registry per tab, each numbered from 1 independently: the feed's six
        # and the prompts list's seven.
        assert feed.COLUMNS_BY_TAB["commands"] == feed.COLUMNS
        assert feed.COLUMNS_BY_TAB["prompts"] == feed.PROMPT_COLUMNS
        assert sorted(feed._default_cols("commands")) == [1, 2, 3, 4, 5, 6]
        assert sorted(feed._default_cols("prompts")) == [1, 2, 3, 4, 5, 6, 7]
        assert all(feed._default_cols("prompts").values())  # all visible initially

    def test_prompt_columns_are_named_in_plan_order(self):
        # Position in the list is the on-screen order; `key` is the stable digit.
        assert [(c.key, c.name) for c in feed.PROMPT_COLUMNS] == [
            (1, "Time"), (2, "Session"), (3, "Project"), (4, "Calls"),
            (5, "Agents"), (6, "Tokens"), (7, "Prompt")]
        # The Prompt cell takes the leftover width; every other column is fixed.
        assert feed.PROMPT_COLUMNS[-1].kwargs["ratio"] == 1
        assert all("width" in c.kwargs for c in feed.PROMPT_COLUMNS[:-1])

    def test_visible_cols_for_follows_the_active_tab(self):
        state = feed.AppState(entries=[])
        commands = {c.key: True for c in feed.COLUMNS}
        prompts = {c.key: True for c in feed.PROMPT_COLUMNS}
        assert state.visible_cols_for() == commands
        assert state.visible_cols_for("prompts") == prompts
        state.tab = "prompts"
        assert state.visible_cols_for() == prompts
        assert state.visible_cols_for("commands") == commands

    def test_tab_cycles_commands_prompts_commands(self):
        state = feed.AppState(entries=[{"command": "c", "session_id": "s"}])
        # Entering the prompts tab asks main() to read the store; leaving it does
        # not (the feed tails the log on its own).
        assert feed.apply_key(state, "\t", 10) is feed.Action.LOAD_PROMPTS
        assert state.tab == "prompts"
        assert feed.apply_key(state, "\t", 10) is feed.Action.NONE
        assert state.tab == "commands"

    def test_each_tab_keeps_its_own_cursor_across_switches(self):
        state = _prompts_state(rows=3, entries=5)
        state.tab = "commands"
        state.cursor = 3                       # feed cursor parked mid-list
        feed.apply_key(state, "\t", 10)        # -> prompts
        feed.apply_key(state, "j", 10)         # moves the prompts cursor only
        assert state.prompts.cursor == 1
        assert state.cursor == 3
        feed.apply_key(state, "\t", 10)        # -> back to commands
        assert state.cursor == 3               # feed cursor untouched
        feed.apply_key(state, "j", 10)
        assert state.cursor == 4
        assert state.prompts.cursor == 1       # prompts cursor untouched

    def test_cycle_tab_recovers_from_an_unknown_tab(self):
        state = feed.AppState(entries=[])
        state.tab = "bogus"
        state.cycle_tab()
        assert state.tab == "prompts"          # treated as index 0, then advanced

    def test_tab_strip_styles_the_active_tab_apart(self):
        strip = feed._tab_strip("prompts")
        assert "Commands" in strip.plain and "Prompts" in strip.plain
        styles = {strip.plain[s.start:s.end].strip(): s.style for s in strip.spans}
        assert styles["Prompts"] == "bold reverse"
        assert styles["Commands"] == "dim"
        flipped = feed._tab_strip("commands")
        styles = {flipped.plain[s.start:s.end].strip(): s.style for s in flipped.spans}
        assert styles["Commands"] == "bold reverse"
        assert styles["Prompts"] == "dim"

    def test_both_panels_render_both_tab_names(self):
        feed_out = self._text(feed.build_panel([], 40, self.ALL, 0))
        prompts_out = self._text(feed.build_prompts_panel(feed.PromptsState(), 40))
        for out in (feed_out, prompts_out):
            assert "Commands" in out and "Prompts" in out

    def test_commands_hints_advertise_the_prompts_tab(self):
        out = self._text(feed.build_panel([], 40, self.ALL, 0))
        assert "tab:prompts" in out
        assert "cols:" in out                  # the feed keeps its toggle bar

    def test_prompts_hints_are_per_tab(self):
        out = self._text(feed.build_prompts_panel(feed.PromptsState(), 40))
        assert "tab:commands" in out
        assert "r:refresh" in out and "x:hide empty" in out
        assert "S:session only" in out         # the session filter
        assert "cols:" in out                  # this tab has its own toggles
        assert "/:search" not in out and "c:clear" not in out  # feed-only keys

    def test_prompts_panel_shows_a_status_when_set(self):
        state = feed.PromptsState(status="store busy: an ingest is writing")
        out = self._text(feed.build_prompts_panel(state, 40))
        assert "store busy" in out

    def test_a_digit_toggle_reaches_the_prompts_panel(self):
        """`build_prompts_panel`'s `visible_cols` argument, end to end.

        Ignoring it (falling back to `_default_cols`) used to leave the whole
        suite green: `TestPromptsTable` passes its own dict straight to
        `build_prompts_table`, so nothing pinned that the PANEL forwards the one
        `AppState.visible_cols_for` hands it. Both halves of the panel have to
        follow the toggle: the rendered table and the `cols:` indicator (whose
        digits are always drawn - only bold vs dim says which are on, hence the
        styled export).
        """
        prompts = feed.PromptsState(rows=[_turn(tools=1, project="/p/proj")],
                                    loaded_at=1000.0, checked_at=1000.0)
        cols = {c.key: True for c in feed.PROMPT_COLUMNS}

        def render(visible):
            console = Console(width=120, record=True)
            console.print(feed.build_prompts_panel(prompts, 24, visible, now=1000.0))
            # clear=False: export_text drains the record buffer by default, so the
            # styled export would come back empty.
            return (console.export_text(clear=False),
                    console.export_text(styles=True))

        plain, styled = render(cols)
        assert "Project" in plain
        assert "\x1b[1m3\x1b[0m" in styled            # key 3 shown: bold
        plain, styled = render({**cols, 3: False})
        assert "Project" not in plain                 # column gone from the table
        assert "/p/proj" not in plain                 # and so is its cell
        assert "\x1b[2m3\x1b[0m" in styled            # key 3 hidden: dim
        assert "Prompt" in plain                      # the rest is untouched


class TestPromptRecords:
    """The frozen records the store's turn queries map onto."""

    # The keys `claude_trail_store.prompt_turns` / `turn_actions` document and
    # return, copied so this file needs no duckdb. The copies are pinned against
    # the REAL queries in tests/test_store.py
    # (test_prompt_record_fields_match_the_query_keys) - that is what catches a
    # rename in the SQL; the two fixture tests below only keep these dicts
    # exhaustive, so the mapping tests never skip a field.
    TURN_ROW = {
        "session_id": "s1", "ts": "2026-07-29 10:00:00-03", "next_ts": None,
        "event_id": "ev-1", "preview": "lets plan a feature", "full_len": 19,
        "tools": 3, "bash": 1, "agents": 1, "subcalls": 2,
        "output_tokens": 2400, "subagent_output_tokens": 900, "requests": 4,
        "last_ts": "2026-07-29 10:06:00-03", "project": "/home/naka/x", "cwd": None,
    }
    ACTION_ROW = {
        "ts": "2026-07-29 10:00:05-03", "agent_id": None, "tool_name": "Bash",
        "text": "wc -l claude_trail.py", "tool_input": '{"command": "wc -l"}',
        "is_error": False,
    }

    def test_turn_row_fixture_covers_every_field(self):
        assert {f.name for f in fields(feed.PromptTurn)} == set(self.TURN_ROW)

    def test_action_row_fixture_covers_every_field(self):
        assert {f.name for f in fields(feed.TurnAction)} == set(self.ACTION_ROW)

    def test_from_row_maps_every_value(self):
        turn = feed.PromptTurn.from_row(self.TURN_ROW)
        for key, value in self.TURN_ROW.items():
            assert getattr(turn, key) == value
        action = feed.TurnAction.from_row(self.ACTION_ROW)
        for key, value in self.ACTION_ROW.items():
            assert getattr(action, key) == value

    def test_from_row_drops_an_unknown_key(self):
        # A column added to the query must not crash the TUI.
        turn = feed.PromptTurn.from_row({**self.TURN_ROW, "brand_new": 1})
        assert turn.session_id == "s1"
        assert not hasattr(turn, "brand_new")

    def test_tools_is_inclusive_of_subcalls(self):
        # 1 main-thread Bash + 2 subagent calls -> tools=3, subcalls=2, so the
        # main-thread count is a subtraction. Renderers must not add the two.
        turn = feed.PromptTurn.from_row(self.TURN_ROW)
        assert turn.tools == 3 and turn.subcalls == 2
        assert turn.tools - turn.subcalls == 1

    def test_cwd_outranks_project_and_both_survive_the_mapping(self):
        # Since phase 5 the ingester writes sessions.cwd (the session's exact
        # launch dir), so BOTH path keys arrive and `cwd` is the one to show;
        # `project` is the lossy dir-name decoding kept as the fallback.
        turn = feed.PromptTurn.from_row(
            {**self.TURN_ROW, "cwd": "/home/naka/Projects/personal/claude-trail"})
        assert turn.cwd == "/home/naka/Projects/personal/claude-trail"
        assert turn.project == "/home/naka/x"
        assert feed._turn_project_path(turn) == turn.cwd
        # A row from before the column was written (its transcript is gone, so
        # the backfill cannot fill it) still resolves, via `project`.
        old = feed.PromptTurn.from_row(self.TURN_ROW)
        assert old.cwd is None
        assert feed._turn_project_path(old) == "/home/naka/x"
        assert feed._turn_project_path(feed.PromptTurn(session_id="s", ts="t")) == ""

    def test_defaults_let_a_test_build_a_row_without_a_db(self):
        turn = feed.PromptTurn(session_id="s1", ts="2026-07-29 10:00:00-03")
        assert (turn.tools, turn.bash, turn.agents, turn.subcalls) == (0, 0, 0, 0)
        assert turn.next_ts is None and turn.last_ts is None
        assert feed.TurnAction(ts="t").agent_id is None


class TestPromptColumnRender:
    """Each PROMPT_COLUMNS entry's `render(turn, ctx)` produces the right cell."""

    def _col(self, key):
        return next(c for c in feed.PROMPT_COLUMNS if c.key == key)

    def test_time_column_renders_hms(self):
        # The store hands timestamps back as strings; format_time parses them.
        turn = _turn(ts="2026-07-29 18:58:55+00:00")
        expected = feed.format_time("2026-07-29 18:58:55+00:00")
        assert self._col(1).render(turn, feed.RenderCtx()) == expected
        assert expected != "??:??:??"          # the string really did parse

    def test_session_column_is_the_feeds_session_cell(self):
        from rich.text import Text
        ctx = feed.RenderCtx(name_map={"sid-p": "my-session"},
                             color_map={"sid-p": "orange"})
        cell = self._col(2).render(_turn(sid="sid-p"), ctx)
        assert isinstance(cell, Text)
        assert cell.plain == "my-session"      # name wins over the short id
        assert cell.style == "orange1"         # mapped through rich_color
        # Same cell the feed renders for the same sid, not a second copy of the
        # label + tint rules.
        assert cell.plain == feed._session_cell("sid-p", ctx).plain

    def test_session_column_falls_back_to_the_short_id(self):
        cell = self._col(2).render(_turn(sid="abcd1234-5678"), feed.RenderCtx())
        assert cell.plain == "abcd1234"

    def test_project_column_prefers_the_exact_cwd(self):
        # The dir-name decoding behind `project` spends `-` on the separator, so
        # a `claude-trail` session reads `…/claude/trail` there. `cwd` is the
        # path the session actually ran in and wins.
        turn = _turn(project="/home/naka/Projects/personal/claude/trail",
                     cwd="/home/naka/Projects/personal/claude-trail")
        cell = self._col(3).render(turn, feed.RenderCtx())
        assert cell.plain == feed.short_path(turn.cwd, feed.PROJECT_LABEL_WIDTH)
        assert "claude-trail" in cell.plain
        assert "claude/trail" not in cell.plain

    def test_project_column_falls_back_when_the_row_has_no_cwd(self):
        # Sessions ingested before the column was written whose transcript has
        # since left the disk: `project` still names them.
        turn = _turn(project="/home/naka/Projects/personal", cwd=None)
        assert self._col(3).render(turn, feed.RenderCtx()).plain == \
            feed.short_path(turn.project, feed.PROJECT_LABEL_WIDTH)
        blank = feed.PromptTurn(session_id="s", ts="2026-07-29 10:00:00-03")
        assert self._col(3).render(blank, feed.RenderCtx()).plain == ""

    def test_project_column_is_text_not_markup(self):
        from rich.text import Text
        # A cwd is user-derived: a dir named `[wip]` must survive the cell.
        cell = self._col(3).render(_turn(cwd="/home/naka/[wip]"), feed.RenderCtx())
        assert isinstance(cell, Text)
        assert "[wip]" in cell.plain

    def test_calls_column_shows_the_total_and_the_bash_share(self):
        from rich.text import Text
        # tools=64 is INCLUSIVE of subcalls=41: the cell shows 64, never 105.
        turn = _turn(tools=64, bash=14, subcalls=41)
        cell = self._col(4).render(turn, feed.RenderCtx())
        assert isinstance(cell, Text)
        assert cell.plain == "64 (14 bash)"
        assert "105" not in cell.plain

    def test_calls_column_omits_the_bash_part_when_no_bash_ran(self):
        cell = self._col(4).render(_turn(tools=5, bash=0), feed.RenderCtx())
        assert cell.plain == "5"

    def test_calls_column_zero_is_a_dim_dash(self):
        cell = self._col(4).render(_turn(tools=0), feed.RenderCtx())
        assert cell.plain == "-"
        assert cell.style == "dim"          # a real turn, not missing data

    def test_agents_column_counts_subagents_and_is_blank_at_zero(self):
        assert self._col(5).render(_turn(agents=3), feed.RenderCtx()).plain == "3"
        assert self._col(5).render(_turn(agents=0), feed.RenderCtx()).plain == ""

    def test_tokens_column_abbreviates_and_calls_out_the_subagent_share(self):
        cell = self._col(6).render(
            _turn(output_tokens=24_200, subagent_output_tokens=3_100), feed.RenderCtx())
        assert cell.plain == "24.2k +3.1k sub"

    def test_tokens_column_omits_the_subagent_part_without_subagent_tokens(self):
        # A subagent that generated nothing adds no cost to call out.
        cell = self._col(6).render(
            _turn(output_tokens=24_200, agents=1, subagent_output_tokens=0),
            feed.RenderCtx())
        assert cell.plain == "24.2k"
        assert "sub" not in cell.plain

    def test_tokens_column_zero_is_dim(self):
        # A turn whose reply carried no usage row honestly reports 0.
        cell = self._col(6).render(_turn(output_tokens=0), feed.RenderCtx())
        assert cell.plain == "0"
        assert cell.style == "dim"

    def test_abbrev_count_stays_within_six_columns(self):
        assert feed._abbrev_count(0) == "0"
        assert feed._abbrev_count(912) == "912"
        assert feed._abbrev_count(24_200) == "24.2k"
        assert feed._abbrev_count(183_400) == "183k"
        assert feed._abbrev_count(1_340_000) == "1.34m"
        assert feed._abbrev_count(987_654_321) == "988m"
        # Each magnitude boundary pinned by VALUE, not just by length: a `<`
        # loosened to `<=` keeps every result six columns wide, so the loop below
        # cannot catch it.
        assert feed._abbrev_count(999) == "999"
        assert feed._abbrev_count(1000) == "1.0k"
        assert feed._abbrev_count(99_999) == "100.0k"
        assert feed._abbrev_count(100_000) == "100k"
        assert feed._abbrev_count(999_999) == "1000k"
        assert feed._abbrev_count(1_000_000) == "1.00m"
        assert feed._abbrev_count(9_999_999) == "10.00m"
        assert feed._abbrev_count(10_000_000) == "10m"
        for n in (0, 1, 999, 1000, 9_999, 99_999, 100_000, 999_999, 1_000_000,
                  9_999_999, 10_000_000, 999_999_999):
            assert len(feed._abbrev_count(n)) <= 6, n

    def test_prompt_column_shows_the_first_line_only(self):
        turn = _turn(preview="fix the failing test\nand then run the suite")
        assert self._col(7).render(turn, feed.RenderCtx()).plain \
            == "fix the failing test"

    def test_prompt_column_labels_a_slash_command(self):
        # A /compact turn is stored as a <command-name> block; the cell must read
        # as the command the user actually invoked.
        turn = _turn(preview="<command-name>/compact</command-name>\n"
                             "<command-args></command-args>")
        assert self._col(7).render(turn, feed.RenderCtx()).plain == "/compact"

    def test_prompt_column_keeps_slash_args(self):
        turn = _turn(preview="<command-name>/model</command-name>\n"
                             "<command-args>opus</command-args>")
        assert self._col(7).render(turn, feed.RenderCtx()).plain == "/model opus"

    def test_prompt_column_labels_a_command_message_led_slash_block(self):
        # A plugin/skill command emits <command-message> BEFORE <command-name>;
        # the store's head regex has to accept it or this cell shows raw XML
        # (measured: 3 of the newest 200 turns of the live store).
        turn = _turn(preview="<command-message>dev-orchestrate</command-message>\n"
                             "<command-name>/dev-orchestrate</command-name>\n"
                             "<command-args>--repo ~/x</command-args>")
        assert self._col(7).render(turn, feed.RenderCtx()).plain \
            == "/dev-orchestrate --repo ~/x"

    def test_prompt_column_skips_a_leading_blank_line(self):
        # The store trims the preview, so this should not occur - but a blank
        # first line must show the prompt rather than blank the cell.
        turn = _turn(preview="\n\n  the real ask\nmore")
        assert self._col(7).render(turn, feed.RenderCtx()).plain == "the real ask"

    def test_prompt_column_empty_preview_is_a_dim_hint(self):
        cell = self._col(7).render(_turn(preview=""), feed.RenderCtx())
        assert cell.plain == "(empty)"
        assert self._col(7).render(_turn(preview="  \n \n"), feed.RenderCtx()).plain \
            == "(empty)"


class TestConsoleMarkupIsNeverParsed:
    """Text from outside this program must reach Rich as `Text`, never a bare `str`.

    `Table.add_row`, `Table.grid`'s rows and a `Panel`'s title/subtitle all run a
    bare `str` through `console.render_str`, which has console markup ENABLED. Two
    failure modes, both driven here rather than reasoned about:

    - `[bold]` (a valid open tag) is SILENTLY DELETED. Measured on the live audit
      store, 2 of its 5,452 user turns already lose text this way, one of them
      `"...when we press [enter] claude-trail should..."`.
    - `[/tmp/x]` (an unmatched closing tag) raises MarkupError. In the TUI that
      happens inside `live.update(render_panel(), ...)` in main()'s loop, which is
      past its `except KeyboardInterrupt`, so the whole TUI dies with a traceback.

    So every case below asserts the bracketed value renders LITERALLY: the probes
    are chosen so a regression shows up as a missing needle (deletion) or as the
    MarkupError propagating out of `console.print` (crash).
    """

    BRACKET = "[/tmp/x]"     # unmatched closing tag -> MarkupError
    DELETE = "[bold]"        # valid open tag -> silently swallowed

    FEED_COLS = {c.key: True for c in feed.COLUMNS}
    PROMPT_COLS = {c.key: True for c in feed.PROMPT_COLUMNS}

    def _out(self, renderable, width=200):
        console = Console(width=width, record=True)
        console.print(renderable)      # a MarkupError here IS the bug
        return console.export_text()

    def _literal(self, build, needle=None):
        """`build(probe)` must render `probe` (or `needle(probe)`) untouched, for
        both probe forms - the crashing one and the silently-deleted one."""
        for probe in (self.BRACKET, self.DELETE):
            out = self._out(build(probe))
            assert (needle(probe) if needle else probe) in out, (probe, out)

    def _entry(self, **kw):
        e = {"timestamp": "2026-07-29T10:00:00-03:00", "command": "ls",
             "cwd": "/tmp", "session_id": "s1"}
        e.update(kw)
        return e

    def _model(self, **kw):
        m = dict(session_id="s1", name="n", status="", live=True, cwd="/tmp",
                 version="1.0", kind="startup", started_at=0,
                 transcript_path="/t.jsonl", subagents=())
        m.update(kw)
        return feed.SessionModel(**m)

    def _sub(self, **kw):
        s = dict(agent_id="aid1", agent_type="gp", description="d", status="done",
                 command_count=1, command_count_capped=False)
        s.update(kw)
        return feed.SubagentInfo(**s)

    # ---- the feed's columns ----

    def test_feed_session_name_is_literal(self):
        # A session name comes from `sessions/<pid>.json`, so the user typed it.
        self._literal(lambda p: feed.build_table(
            [self._entry()], 5, self.FEED_COLS, 0, name_map={"s1": f"n{p}"}))
        # Same through the tinted branch, which is a different return path.
        self._literal(lambda p: feed.build_table(
            [self._entry()], 5, self.FEED_COLS, 0,
            name_map={"s1": f"n{p}"}, color_map={"s1": "orange"}))

    def test_feed_directory_and_command_are_literal(self):
        self._literal(lambda p: feed.build_table(
            [self._entry(cwd=f"/tmp/{p}")], 5, self.FEED_COLS, 0))
        self._literal(lambda p: feed.build_table(
            [self._entry(command=f"echo {p}")], 5, self.FEED_COLS, 0))

    def test_feed_files_cell_keeps_a_bracketed_basename(self):
        # A basename cannot carry the crash form (`[/x]` needs a `/`, which ends
        # the basename), but `[bold].txt` is a legal filename and was deleted.
        out = self._out(feed.build_table(
            [self._entry(command="cat /tmp/[bold].txt")], 5, self.FEED_COLS, 0))
        assert "[bold].txt" in out

    def test_feed_agent_label_is_literal(self):
        # An agent description is the Task tool's `description`, i.e. prose.
        self._literal(lambda p: feed.build_table(
            [self._entry(agent_id="aid1")], 5, self.FEED_COLS, 0,
            agent_map={"aid1": {"description": p, "type": "gp"}}))

    # ---- the prompts tab's columns ----

    def test_prompt_cell_is_literal(self):
        # The whole point of this phase: a turn's preview is a human's own prose.
        self._literal(lambda p: feed.build_prompts_table(
            [_turn(preview=f"look in {p} now", tools=1)], 5, self.PROMPT_COLS, 0))

    def test_prompts_project_and_session_cells_are_literal(self):
        self._literal(lambda p: feed.build_prompts_table(
            [_turn(project=f"/p/{p}", tools=1)], 5, self.PROMPT_COLS, 0))
        self._literal(lambda p: feed.build_prompts_table(
            [_turn(tools=1)], 5, self.PROMPT_COLS, 0, name_map={"sid-p": f"n{p}"}))

    def test_every_prompt_column_survives_a_bracketed_row(self):
        # Whole-row sweep: no PROMPT_COLUMNS renderer may hand Rich a bare str,
        # including one added later (this fails on the added column, not on a
        # hand-listed subset).
        turn = _turn(sid=f"s{self.BRACKET}", project=f"/p/{self.BRACKET}",
                     preview=f"ask {self.BRACKET}", tools=3, bash=1, agents=2,
                     output_tokens=1200, subagent_output_tokens=30)
        for col in feed.PROMPT_COLUMNS:
            self._out(feed.build_prompts_table([turn], 5, {col.key: True}, 0))

    # ---- the panels ----

    def test_command_detail_rows_are_literal(self):
        self._literal(lambda p: feed.build_detail_panel(
            self._entry(cwd=f"/tmp/{p}"), 40))
        self._literal(lambda p: feed.build_detail_panel(
            self._entry(command=f"echo {p}"), 40))
        self._literal(lambda p: feed.build_detail_panel(
            self._entry(), 40, prompt={"text": f"ask {p}", "ts": "2026-07-29 10:00"}))
        # The Files metadata row, deletion form only (see the feed's Files test).
        # Counted, not just present: the command body below it renders the same
        # path, so `in out` alone passes even with the Files row swallowed.
        out = self._out(feed.build_detail_panel(
            self._entry(command="cat /tmp/[bold].txt"), 40))
        assert out.count("[bold].txt") == 2      # the Files row AND the command

    def test_full_prompt_panel_is_literal(self):
        self._literal(lambda p: feed.build_prompt_panel(
            "s1", 40, f"ask {p}", ts="2026-07-29 10:00:00"))
        # The `Sent` row is a store timestamp: no `[` is reachable today, but it
        # is outside text and a bare str crashed on one.
        self._literal(lambda p: feed.build_prompt_panel("s1", 40, "b", ts=p))
        # The Session and Agent rows, which both tabs' callers feed from outside
        # values (a pid.json name, a Task description).
        self._literal(lambda p: feed.build_prompt_panel(
            "s1", 40, "b", name_map={"s1": f"n{p}"}))
        self._literal(lambda p: feed.build_prompt_panel(
            "s1", 40, "b", agent_id="aid1",
            agent_map={"aid1": {"description": p, "type": "gp"}}))

    def test_session_panel_rows_are_literal(self):
        for build in (
            lambda p: feed.build_session_panel(self._model(session_id=f"s{p}")),
            lambda p: feed.build_session_panel(self._model(name=f"n{p}")),
            lambda p: feed.build_session_panel(self._model(cwd=f"/tmp/{p}")),
            lambda p: feed.build_session_panel(self._model(version=f"1.0{p}")),
            lambda p: feed.build_session_panel(
                self._model(started_at=1_700_000_000_000, kind=f"k{p}")),
            lambda p: feed.build_session_panel(
                self._model(subagents=(self._sub(description=f"d{p}"),))),
            lambda p: feed.build_session_panel(
                self._model(subagents=(self._sub(agent_type=f"t{p}"),))),
            lambda p: feed.build_session_panel(
                self._model(subagents=(self._sub(description=f"d{p}"),)),
                highlight_agent_id="aid1"),
        ):
            self._literal(build)

    def test_db_panel_rows_are_literal(self):
        # The connection string is a path from $CLAUDE_TRAIL_DB and the failure
        # rows carry a raw exception message.
        self._literal(lambda p: feed.build_db_panel([("connection", f"/tmp/{p}.duckdb")]))
        self._literal(lambda p: feed.build_db_panel([("error", f"boom {p}")]))
        self._literal(lambda p: feed.build_db_panel([(f"rows {p}", "1")]))

    def test_search_hits_are_literal(self):
        # Source code, JSON and markdown are the `[`-densest text in the program,
        # and every grep hit is one of those.
        def hit(**kw):
            s = dict(sid="s1", roots={"cwd": [Path("/tmp")]}, root="cwd",
                     mode="RESULTS", query="q")
            s.update(kw)
            return feed.build_search_panel(feed.SearchState(**s))
        self._literal(lambda p: hit(results=[("/tmp/a.py", 3, f"x = list{p}")]))
        self._literal(lambda p: hit(results=[(f"/tmp/{p}.py", 3, "hit")]))

    # ---- the turn detail page ----

    def test_turn_page_header_rows_are_literal(self):
        self._literal(lambda p: feed.build_turn_panel(
            feed.PromptsState(detail=_turn(project=f"/p/{p}", tools=1)), 40))
        self._literal(lambda p: feed.build_turn_panel(
            feed.PromptsState(detail=_turn(tools=1)), 40, name_map={"sid-p": f"n{p}"}))

    def test_turn_page_prompt_box_is_literal(self):
        # Both sources: the text the store loaded, and the row's own preview,
        # which is what the box falls back to when that load answered nothing.
        self._literal(lambda p: feed.build_turn_panel(
            feed.PromptsState(detail=_turn(tools=1), detail_text=f"ask {p}"), 40))
        self._literal(lambda p: feed.build_turn_panel(
            feed.PromptsState(detail=_turn(preview=f"ask {p}", tools=1)), 40))

    def test_action_cells_are_literal(self):
        # A tool call's label is a command, a path, a regex or a subagent's
        # description - every one of them outside text.
        self._literal(lambda p: feed.build_action_table(
            [feed.TurnAction(ts="2026-07-29 10:00:00-03", tool_name="Bash",
                             text=f"echo {p}")]))
        self._literal(lambda p: feed.build_action_table(
            [feed.TurnAction(ts="2026-07-29 10:00:00-03", tool_name="Agent",
                             tool_input=json.dumps({"description": f"do {p}"}))]))
        self._literal(lambda p: feed.build_action_table(
            [feed.TurnAction(ts="2026-07-29 10:00:00-03", tool_name=f"mcp__x__y{p}")]))
        self._literal(lambda p: feed.build_action_table(
            [feed.TurnAction(ts="2026-07-29 10:00:00-03", agent_id="aid1",
                             tool_name="Read", text="/x/a.py")],
            agent_map={"aid1": {"description": p, "type": "gp"}}))

    def test_every_action_cell_survives_a_bracketed_row(self):
        # Whole-row sweep: every cell of one row carries a probe at once, so a
        # cell added to this table later fails here rather than in whichever
        # test happens to look at it. Width is generous so nothing is ellipsized
        # away and passes for markup-safe.
        for probe in (self.BRACKET, self.DELETE):
            action = feed.TurnAction(
                ts="2026-07-29 10:00:00-03", agent_id="aid1",
                tool_name=f"tool{probe}", text=f"echo {probe}",
                tool_input=json.dumps({"command": f"echo {probe}"}))
            out = self._out(feed.build_action_table(
                [action], agent_map={"aid1": {"description": probe, "type": "gp"}}),
                width=400)
            assert out.count(probe) == 3, (probe, out)   # agent, tool, detail

    def test_prompts_panel_chrome_is_literal(self):
        self._literal(lambda p: feed.build_prompts_panel(
            feed.PromptsState(rows=[_turn(preview=f"ask {p}", tools=1)],
                              loaded_at=1.0, checked_at=1.0), 24, now=1.0))
        self._literal(lambda p: feed.build_prompts_panel(
            feed.PromptsState(status=f"store unreadable: {p}",
                              loaded_at=1.0, checked_at=1.0), 24, now=1.0))

    def test_panel_subtitles_survive_a_bracketed_config_dir(self):
        # LOG_PATH is derived from $CLAUDE_CONFIG_DIR, so it is outside text too,
        # and the subtitles used to interpolate it INTO markup (`[dim]{path}`).
        def subtitled(builder):
            def build(probe):
                with patch.object(feed, "LOG_PATH",
                                  Path(f"/tmp/{probe}/command-log.jsonl")):
                    return builder()
            return build
        self._literal(subtitled(
            lambda: feed.build_panel([self._entry()], 40, self.FEED_COLS, 0)))
        self._literal(subtitled(lambda: feed.build_detail_panel(self._entry(), 40)))
        self._literal(subtitled(lambda: feed.build_prompt_panel(
            "s1", 40, "b", ts="2026-07-29 10:00:00")))

    def test_a_live_driven_render_of_every_panel_survives_a_bracket_prompt(self):
        """The crash exactly where the TUI hits it: `live.update(render_panel())`.

        `console.print` above is the same render path, but this is the one main()
        actually runs, and it is the reason a MarkupError is fatal rather than
        cosmetic - it is raised past `except KeyboardInterrupt`, so the loop
        unwinds and the tool exits with a traceback.
        """
        import io
        from rich.live import Live
        probe = "look in [/tmp/x] now"
        entry = self._entry(command=f"echo {probe}", cwd=f"/tmp/{probe}")
        turn = _turn(preview=probe, project=f"/p/{probe}", tools=2, bash=1,
                     subcalls=1, agents=1, requests=1, output_tokens=9)
        prompts = feed.PromptsState(rows=[turn], loaded_at=1.0, checked_at=1.0)
        open_turn = feed.PromptsState(
            rows=[turn], detail=turn, detail_text=probe,
            actions=[feed.TurnAction(ts="2026-07-29 10:00:00-03", tool_name="Bash",
                                     text=f"echo {probe}"),
                     feed.TurnAction(ts="2026-07-29 10:00:01-03", agent_id="aid1",
                                     tool_name="Agent",
                                     tool_input='{"description": "look in [/tmp/x]"}')])
        panels = [
            feed.build_prompts_panel(prompts, 24, now=1.0),
            feed.build_turn_panel(open_turn, 24,
                                  agent_map={"aid1": {"description": probe, "type": "gp"}}),
            feed.build_panel([entry], 24, self.FEED_COLS, 0),
            feed.build_detail_panel(entry, 24,
                                    prompt={"text": probe, "ts": "2026-07-29 10:00"}),
            feed.build_prompt_panel("s1", 24, probe, ts="2026-07-29 10:00"),
            feed.build_session_panel(self._model(
                cwd=f"/tmp/{probe}", subagents=(self._sub(description=probe),))),
            feed.build_db_panel([("error", probe)]),
            feed.build_search_panel(feed.SearchState(
                sid="s1", roots={"cwd": [Path("/tmp")]}, root="cwd", mode="RESULTS",
                results=[("/tmp/a.py", 3, probe)])),
        ]
        # force_terminal, or Live.refresh() short-circuits on a file console and
        # nothing would be rendered at all.
        console = Console(file=io.StringIO(), width=120, force_terminal=True)
        with Live(panels[0], console=console) as live:
            for panel in panels:
                live.update(panel, refresh=True)


class TestPromptsTable:
    """build_prompts_table: newest-first WITHOUT a reversal (unlike the feed),
    the cursor highlight, the scroll window, and per-column visibility."""

    ALL = {c.key: True for c in feed.PROMPT_COLUMNS}

    def _rows(self, n):
        # Distinguishable at both ends: row 0 is the newest ("turn-0").
        return [_turn(sid=f"sid{i:05d}", ts=f"2026-07-29 10:{59 - i:02d}:00-03",
                      preview=f"turn-{i}", tools=1) for i in range(n)]

    def _text(self, table, width=160):
        console = Console(width=width, record=True)
        console.print(table)
        return console.export_text()

    def test_rows_render_in_list_order_no_reversal(self):
        # The feed reverses `entries` (append order) to show newest first; the
        # prompts query already returns newest-first, so reversing here would
        # silently show the OLDEST turn at the top.
        out = self._text(feed.build_prompts_table(self._rows(3), 10, self.ALL, 0))
        assert out.index("turn-0") < out.index("turn-1") < out.index("turn-2")

    def test_max_rows_truncates_from_the_newest_end(self):
        out = self._text(feed.build_prompts_table(self._rows(5), 2, self.ALL, 0))
        assert "turn-0" in out and "turn-1" in out
        assert "turn-2" not in out

    def test_offset_skips_the_newest_turns(self):
        out = self._text(feed.build_prompts_table(self._rows(5), 2, self.ALL, 3, offset=3))
        assert "turn-0" not in out
        assert "turn-3" in out and "turn-4" in out

    def test_cursor_highlights_the_row_at_cursor_minus_offset(self):
        rows = self._rows(5)
        table = feed.build_prompts_table(rows, 5, self.ALL, 3, offset=1)
        styles = [r.style for r in table.rows]
        assert styles == [None, None, "on grey23", None]  # 4 displayed, cursor 3rd

    def test_cursor_scrolled_out_of_view_highlights_nothing(self):
        table = feed.build_prompts_table(self._rows(5), 2, self.ALL, 4, offset=0)
        assert all(r.style is None for r in table.rows)

    def test_hidden_columns_are_not_rendered(self):
        cols = {**self.ALL, 3: False, 6: False}         # Project, Tokens off
        out = self._text(feed.build_prompts_table(self._rows(1), 5, cols, 0))
        assert "Project" not in out and "Tokens" not in out
        assert "Prompt" in out and "Calls" in out


class TestPromptsTabKeys:
    """Key routing while the Prompts tab is active: its own nav, the feed-only
    keys staying feed-only, and the session-scoped keys working on both tabs."""

    def test_j_k_move_the_prompts_cursor_only(self):
        state = _prompts_state(rows=3, entries=3)
        assert feed.apply_key(state, "j", 10) is feed.Action.NONE
        assert (state.prompts.cursor, state.cursor) == (1, 0)
        feed.apply_key(state, "k", 10)
        assert state.prompts.cursor == 0
        feed.apply_key(state, "k", 10)
        assert state.prompts.cursor == 0            # clamped at the top

    def test_g_and_G_jump_the_prompts_list(self):
        state = _prompts_state(rows=4)
        feed.apply_key(state, "G", 10)
        assert state.prompts.cursor == 3
        feed.apply_key(state, "g", 10)
        assert state.prompts.cursor == 0
        assert state.prompts.scroll_offset == 0

    def test_nav_is_inert_with_an_empty_list(self):
        state = _prompts_state(rows=0)
        for ch in ("j", "k", "g", "G"):
            assert feed.apply_key(state, ch, 10) is feed.Action.NONE
        assert state.prompts.cursor == 0

    def test_q_and_ctrl_c_quit_from_the_prompts_tab(self):
        for ch in ("q", "\x03"):
            state = _prompts_state()
            assert feed.apply_key(state, ch, 10) is feed.Action.QUIT

    def test_c_does_not_clear_the_feed_from_the_prompts_tab(self):
        state = _prompts_state(rows=2, entries=4)
        assert feed.apply_key(state, "c", 10) is feed.Action.NONE
        assert len(state.entries) == 4               # the feed is untouched

    def test_digits_toggle_this_tabs_columns_and_not_the_feeds(self):
        state = _prompts_state()
        for ch in "1234567":
            assert feed.apply_key(state, ch, 10) is feed.Action.NONE
        # Every prompts column but the last toggled off; the feed is untouched.
        assert state.visible_cols == {c.key: True for c in feed.COLUMNS}
        off = [k for k, shown in state.prompt_cols.items() if not shown]
        assert len(off) == len(feed.PROMPT_COLUMNS) - 1

    def test_digits_refuse_to_hide_the_last_prompts_column(self):
        state = _prompts_state()
        for ch in "1234567":
            feed.apply_key(state, ch, 10)
        assert sum(state.prompt_cols.values()) == 1   # one always survives
        # And the survivor cannot be hidden either, however many times it is hit.
        survivor = next(k for k, shown in state.prompt_cols.items() if shown)
        for _ in range(3):
            feed.apply_key(state, str(survivor), 10)
        assert state.prompt_cols[survivor] is True

    def test_a_digit_with_no_prompts_column_is_inert(self):
        # The feed has no key 7 and the prompts list has no key 8: an unregistered
        # digit must not touch either dict.
        state = _prompts_state()
        assert feed.apply_key(state, "8", 10) is feed.Action.NONE
        assert all(state.prompt_cols.values())
        state.tab = "commands"
        assert feed.apply_key(state, "7", 10) is feed.Action.NONE
        assert all(state.visible_cols.values())

    def test_slash_does_not_open_search_from_the_prompts_tab(self):
        # `/` needs a row's cwd, which a turn row does not carry.
        state = _prompts_state()
        assert feed.apply_key(state, "/", 10) is feed.Action.NONE
        assert state.search is None

    def test_f_is_inert_on_the_prompts_tab(self):
        # `f` needs a command's file list, which a turn row does not carry.
        state = _prompts_state()
        assert feed.apply_key(state, "f", 10) is feed.Action.NONE

    def test_prompts_only_keys_are_inert_on_the_commands_tab(self):
        state = feed.AppState(entries=[{"command": "c", "session_id": "s"}])
        for ch in ("r", "x", "S"):
            assert feed.apply_key(state, ch, 10) is feed.Action.NONE
        assert state.tab == "commands"
        assert state.prompts == feed.PromptsState()   # the other tab is untouched

    def test_enter_opens_the_selected_turn_and_asks_for_its_detail(self):
        # apply_key stays pure: it parks the turn in `detail` and hands main()
        # the action that reads the store.
        for ch in ("\r", "\n"):
            state = _prompts_state(rows=3)
            state.prompts.cursor = 1
            assert feed.apply_key(state, ch, 10) is feed.Action.OPEN_PROMPT
            assert state.prompts.detail is state.prompts.rows[1]
            assert state.prompts.actions is None      # main() loads them
            assert state.prompts.detail_text is None
            assert state.modal_open() is True
            assert state.prompts.cursor == 1          # the list keeps its place

    def test_enter_is_inert_with_no_turn_under_the_cursor(self):
        state = _prompts_state(rows=0)
        assert feed.apply_key(state, "\r", 10) is feed.Action.NONE
        assert state.prompts.detail is None
        assert state.modal_open() is False

    def test_opening_a_turn_starts_from_a_clean_page(self):
        """Opening a turn resets the page's sub-state, whatever was left behind.

        The leftovers are set by hand rather than by driving a previous turn,
        because every way OUT of a turn goes through `close_detail` and would
        clean up first: this pins the reset as the invariant it is ("`detail` and
        its sub-state are written together"), so a future path that leaves half a
        page behind cannot show the last turn's calls under this turn's prompt.
        """
        state = _prompts_state(rows=2)
        state.prompts.actions = [feed.TurnAction(ts="t", tool_name="Bash")]
        state.prompts.detail_text = "the previous turn's prompt"
        state.prompts.detail_scroll = 7
        state.prompts.bash_only = True
        state.prompts.prompt_open = True
        state.prompts.prompt_scroll = 4
        feed.apply_key(state, "j", 10)
        assert feed.apply_key(state, "\r", 10) is feed.Action.OPEN_PROMPT
        assert state.prompts.detail is state.prompts.rows[1]
        assert (state.prompts.actions, state.prompts.detail_text) == (None, None)
        assert (state.prompts.detail_scroll, state.prompts.prompt_scroll) == (0, 0)
        assert state.prompts.bash_only is False
        assert state.prompts.prompt_open is False

    def test_a_held_enter_is_not_self_limiting_which_is_why_main_coalesces(self):
        """Opening a turn is NOT self-limiting the way `d` and `s` are.

        Their modals swallow the repeat, so they read the store once. Here, a
        turn whose prompt text has not loaded (the store was busy, or the turn
        cleaned to nothing) gives `↵` back its close-the-page meaning, so a
        key-repeat burst alternates close/open and asks for the read again -
        which is why main() coalesces OPEN_PROMPT out of the drain loop into one
        read per frame, exactly as it does for LOAD_PROMPTS (10 buffered `r` once
        measured 3.21s frozen inside a single drain).

        With the text loaded, `↵` drills into the prompt and back instead, and
        those depths ask for nothing.
        """
        state = _prompts_state(rows=3)
        opens = [feed.apply_key(state, "\r", 10) for _ in range(4)]
        assert opens == [feed.Action.OPEN_PROMPT, feed.Action.NONE,
                         feed.Action.OPEN_PROMPT, feed.Action.NONE]

        feed.apply_key(state, "\r", 10)               # open it once more
        state.prompts.detail_text = "the ask"         # what main() loaded
        for _ in range(6):
            assert feed.apply_key(state, "\r", 10) is feed.Action.NONE
        assert state.prompts.detail is not None       # still on the page

    def test_selected_session_id_resolves_per_tab(self):
        state = _prompts_state(rows=2, entries=3)
        assert state.selected_session_id() == "sid-p0"       # newest turn
        state.tab = "commands"
        assert state.selected_session_id() == "sid-2"        # newest feed row
        assert feed.AppState(entries=[]).selected_session_id() is None

    def test_selected_session_id_is_none_for_a_blank_session(self):
        state = feed.AppState(entries=[{"command": "c", "session_id": ""}])
        assert state.selected_session_id() is None
        state.tab = "prompts"
        state.prompts.rows = [_turn(sid="")]
        assert state.selected_session_id() is None

    def test_s_opens_the_session_modal_from_the_prompts_tab(self):
        state = _prompts_state(rows=2)
        assert feed.apply_key(state, "s", 10) is feed.Action.OPEN_SESSION_DETAIL
        assert state.session_detail == "sid-p0"
        assert state.session_agent_id is None     # turns are main-thread
        assert state.modal_open() is True

    def test_s_is_inert_with_an_empty_prompts_list(self):
        state = _prompts_state(rows=0)
        assert feed.apply_key(state, "s", 10) is feed.Action.NONE
        assert state.session_detail is None
        assert state.modal_open() is False

    def test_o_and_t_act_on_both_tabs(self):
        for tab, sid in (("prompts", "sid-p0"), ("commands", "sid-2")):
            state = _prompts_state(rows=2, entries=3)
            state.tab = tab
            assert feed.apply_key(state, "o", 10) is feed.Action.OPEN_SESSION
            assert feed.apply_key(state, "t", 10) is feed.Action.OPEN_TRANSCRIPT
            assert state.selected_session_id() == sid  # what main() opens

    def test_o_and_t_are_inert_when_the_active_tab_has_no_rows(self):
        state = _prompts_state(rows=0, entries=3)   # feed has rows, prompts does not
        assert feed.apply_key(state, "o", 10) is feed.Action.NONE
        assert feed.apply_key(state, "t", 10) is feed.Action.NONE

    def test_d_opens_the_db_modal_from_either_tab(self):
        # The database modal describes the store, not a session, so it needs no
        # selected row on either tab.
        for tab in ("commands", "prompts"):
            state = _prompts_state(rows=0, entries=0)
            state.tab = tab
            assert feed.apply_key(state, "d", 10) is feed.Action.LOAD_DB_INFO
            assert state.db_info is None            # apply_key stays pure

    def test_session_modal_opened_from_prompts_closes_with_esc(self):
        # The shared modals must not become a trap on this tab: its branch runs
        # before the feed's modal branch, so it has to hand the key over.
        state = _prompts_state(rows=1)
        feed.apply_key(state, "s", 10)
        assert feed.apply_key(state, "j", 10) is feed.Action.NONE
        assert state.session_scroll == 1            # nav scrolls the modal
        assert feed.apply_key(state, "\x1b", 10) is feed.Action.NONE
        assert state.session_detail is None
        assert state.tab == "prompts"               # back to the list, same tab

    def test_db_modal_opened_from_prompts_closes_with_esc(self):
        state = _prompts_state(rows=1)
        state.db_info = [("connection", "/x")]
        assert feed.apply_key(state, "q", 10) is feed.Action.NONE   # closes, not quit
        assert state.db_info is None

    def test_search_modal_still_owns_tab(self):
        # `/` opens search from the feed; while it is open Tab toggles the search
        # root, and must NOT switch tabs.
        state = feed.AppState(entries=[{"command": "c", "session_id": "s1"}])
        state.search = feed.SearchState(
            sid="s1", roots={"transcript": [Path("/t.jsonl")], "cwd": [Path("/w")]})
        assert feed.apply_key(state, "\t", 10) is feed.Action.NONE
        assert state.search.root == "cwd"
        assert state.tab == "commands"

    # ---- refresh / filter keys: every one of them stays pure ----

    def test_only_enter_opens_the_turn_modal(self):
        """`↵` is the ONE key on this tab that may open the turn page.

        `prompts.detail` is counted by `modal_open()`, so any other branch setting
        it turns the tab into a key trap: every key but esc/q/enter dead, and
        (before this page existed) nothing on screen to explain it. Asserted over
        the whole key set in one loop rather than per-key, so a future rewrite of
        any single test cannot quietly drop the guard (it did once - phase 3's
        `r`/`x` rewrite).
        """
        for ch in ("r", "x", "S", "j", "k", "g", "G", "\t",
                   "1", "2", "7", "c", "f", "/", "o", "t"):
            state = _prompts_state(rows=0)
            # tools=1 so the rows survive `x`: with an empty filtered list
            # `selected()` is None and the trap would set `detail = None`, which
            # looks fine while being exactly the bug in one of its two states.
            state.prompts.rows = [_turn(sid=f"sid-p{i}", tools=1) for i in range(3)]
            feed.apply_key(state, ch, 10)
            assert state.prompts.detail is None, ch
            assert state.modal_open() is False, ch

    def test_the_turn_modal_has_a_panel_to_render(self):
        # The other half of the guard above: the key that DOES set `detail` must
        # have a renderer, or it is the same trap with a different trigger.
        state = _prompts_state(rows=0)
        state.prompts.rows = [_turn(sid="sid-p0", tools=1, preview="do the thing")]
        feed.apply_key(state, "\r", 10)
        console = Console(width=120, record=True)
        console.print(feed.build_turn_panel(state.prompts, 24))
        assert "do the thing" in console.export_text()

    def test_r_asks_for_a_reload_without_touching_the_list(self):
        state = _prompts_state(rows=3)
        rows_before, cursor_before = list(state.prompts.rows), state.prompts.cursor
        assert feed.apply_key(state, "r", 10) is feed.Action.LOAD_PROMPTS
        assert state.prompts.rows == rows_before      # main() does the reading
        assert state.prompts.cursor == cursor_before
        assert state.prompts.loaded_at is None        # no I/O happened here
        assert state.modal_open() is False            # and no modal was opened

    def test_x_toggles_hide_empty_without_a_reload(self):
        # Filtered in place over the snapshot, so no store read is requested.
        state = _prompts_state(rows=3)
        assert feed.apply_key(state, "x", 10) is feed.Action.NONE
        assert state.prompts.hide_empty is True
        assert feed.apply_key(state, "x", 10) is feed.Action.NONE
        assert state.prompts.hide_empty is False
        assert state.modal_open() is False            # and no modal was opened

    def test_every_nav_key_pairs_with_the_clamp(self):
        """The deferred finding: `goto_bottom` / `move_down` never touched
        `scroll_offset`, where the feed pairs every move with a clamp. Driven
        through `apply_key` rather than by calling clamp by hand, so what is pinned
        is the PAIRING - remove it after any one key and the cursor walks off the
        rendered window with the offset stuck at 0.
        """
        state = _prompts_state(rows=0)
        state.prompts.rows = [_turn(sid=f"s{i:05d}", tools=1) for i in range(20)]
        feed.apply_key(state, "G", 5)
        assert (state.prompts.cursor, state.prompts.scroll_offset) == (19, 15)
        for _ in range(20):
            feed.apply_key(state, "k", 5)
        assert (state.prompts.cursor, state.prompts.scroll_offset) == (0, 0)
        for _ in range(9):
            feed.apply_key(state, "j", 5)
        assert (state.prompts.cursor, state.prompts.scroll_offset) == (9, 5)
        feed.apply_key(state, "g", 5)
        assert (state.prompts.cursor, state.prompts.scroll_offset) == (0, 0)

    def test_x_reanchors_the_cursor_onto_the_shorter_list(self):
        state = _prompts_state(rows=0)
        state.prompts.rows = [_turn(sid="a", tools=2), _turn(sid="b", tools=0),
                              _turn(sid="c", tools=0)]
        feed.apply_key(state, "G", 10)
        assert state.prompts.cursor == 2
        feed.apply_key(state, "x", 10)                # only one row survives
        assert state.prompts.cursor == 0
        assert state.prompts.selected().session_id == "a"

    def test_S_filters_to_the_selected_session_and_reloads(self):
        state = _prompts_state(rows=3)
        state.prompts.cursor = 2
        assert feed.apply_key(state, "S", 10) is feed.Action.LOAD_PROMPTS
        assert state.prompts.session_filter == "sid-p2"
        # A scoped list is a new list, so it starts at the top.
        assert (state.prompts.cursor, state.prompts.scroll_offset) == (0, 0)
        assert state.prompts.rows                     # apply_key does no I/O

    def test_S_again_clears_the_filter_and_reloads(self):
        state = _prompts_state(rows=2)
        state.prompts.session_filter = "sid-p1"
        assert feed.apply_key(state, "S", 10) is feed.Action.LOAD_PROMPTS
        assert state.prompts.session_filter is None

    def test_S_is_inert_without_a_row_to_take_a_session_from(self):
        state = _prompts_state(rows=0)
        assert feed.apply_key(state, "S", 10) is feed.Action.NONE
        assert state.prompts.session_filter is None


class TestPromptsFilter:
    """`x` (hide zero-call turns): a client-side lens over one snapshot, with the
    panel staying honest about what it is not showing."""

    def _state(self):
        prompts = feed.PromptsState(rows=[
            _turn(sid="busy1", tools=4, bash=2), _turn(sid="quiet1", tools=0),
            _turn(sid="busy2", tools=1), _turn(sid="quiet2", tools=0)])
        prompts.loaded_at = prompts.checked_at = 1000.0
        return prompts

    def _text(self, panel, width=160):
        console = Console(width=width, record=True)
        console.print(panel)
        return console.export_text()

    def test_visible_rows_is_every_row_by_default(self):
        prompts = self._state()
        assert prompts.visible_rows() == prompts.rows
        assert prompts.hidden_count() == 0

    def test_hide_empty_drops_only_zero_call_turns(self):
        prompts = self._state()
        prompts.hide_empty = True
        assert [r.session_id for r in prompts.visible_rows()] == ["busy1", "busy2"]
        assert prompts.hidden_count() == 2
        assert len(prompts.rows) == 4          # the snapshot itself is untouched

    def test_selected_follows_the_filtered_list(self):
        prompts = self._state()
        prompts.hide_empty = True
        prompts.cursor = 1
        assert prompts.selected().session_id == "busy2"   # not the raw rows[1]

    def test_header_counts_only_what_is_shown_and_says_what_is_hidden(self):
        prompts = self._state()
        prompts.hide_empty = True
        out = self._text(feed.build_prompts_panel(prompts, 24, now=1000.0))
        assert "2 turns (2 hidden)" in out
        assert "1/2" in out                    # the cursor line counts the same 2

    def test_header_omits_the_hidden_note_when_nothing_is_hidden(self):
        out = self._text(feed.build_prompts_panel(self._state(), 24, now=1000.0))
        assert "4 turns" in out
        assert "hidden" not in out

    def test_a_fully_filtered_list_says_so_instead_of_advising_ingest(self):
        # The store is current here; telling the user to run an ingest would be
        # wrong advice. The rows are one keypress away, and the text says which.
        prompts = self._state()
        prompts.rows = [_turn(sid="quiet1", tools=0)]
        prompts.hide_empty = True
        out = self._text(feed.build_prompts_panel(prompts, 24, now=1000.0))
        assert "hidden" in out
        assert "press x" in out
        assert "claude-trail ingest" not in out


class TestPromptsClamp:
    """PromptsState.clamp: the contract AppState.clamp has, over the visible rows.
    Every cursor move and every reload pairs with it, or `G` then a shorter list
    leaves the cursor selecting nothing."""

    def _rows(self, n):
        return [_turn(sid=f"sid{i:05d}", preview=f"turn-{i}", tools=1)
                for i in range(n)]

    def test_move_down_scrolls_the_window(self):
        # The deferred bug: move_down/goto_bottom used to leave scroll_offset at 0,
        # so the cursor walked off the bottom of the rendered window.
        prompts = feed.PromptsState(rows=self._rows(20))
        for _ in range(9):
            prompts.move_down()
            prompts.clamp(5)
        assert prompts.cursor == 9
        assert prompts.scroll_offset == 5      # cursor visible in a 5-row window
        assert prompts.scroll_offset <= prompts.cursor < prompts.scroll_offset + 5

    def test_goto_bottom_scrolls_to_the_end(self):
        prompts = feed.PromptsState(rows=self._rows(20))
        prompts.goto_bottom()
        prompts.clamp(5)
        assert prompts.cursor == 19
        assert prompts.scroll_offset == 15
        assert prompts.selected().preview == "turn-19"

    def test_move_up_pulls_the_window_back(self):
        prompts = feed.PromptsState(rows=self._rows(20), cursor=19, scroll_offset=15)
        for _ in range(16):
            prompts.move_up()
            prompts.clamp(5)
        assert (prompts.cursor, prompts.scroll_offset) == (3, 3)

    def test_reload_to_a_shorter_list_reanchors(self):
        prompts = feed.PromptsState(rows=self._rows(30))
        prompts.goto_bottom()
        prompts.clamp(10)
        assert (prompts.cursor, prompts.scroll_offset) == (29, 20)
        prompts.rows = self._rows(4)           # what a re-query can hand back
        prompts.clamp(10)
        assert prompts.cursor == 3
        assert prompts.scroll_offset == 0      # a list that fits never scrolls
        assert prompts.selected() is not None

    def test_reload_to_an_empty_list_resets(self):
        prompts = feed.PromptsState(rows=self._rows(9), cursor=8, scroll_offset=4)
        prompts.rows = []
        prompts.clamp(5)
        assert (prompts.cursor, prompts.scroll_offset) == (0, 0)
        assert prompts.selected() is None

    def test_offset_never_runs_past_the_end(self):
        prompts = feed.PromptsState(rows=self._rows(12), cursor=0, scroll_offset=99)
        prompts.clamp(5)
        assert prompts.scroll_offset == 0      # pulled back to keep row 0 visible
        prompts.cursor, prompts.scroll_offset = 11, 99
        prompts.clamp(5)
        assert prompts.scroll_offset == 7      # len - max_rows, not 99

    def test_cursor_zero_is_the_newest_turn_not_the_oldest(self):
        # The feed's cursor indexes a REVERSED view of `entries`; this one does
        # not. A list whose ends are distinguishable catches the confusion.
        prompts = feed.PromptsState(rows=self._rows(5))
        prompts.clamp(5)
        assert prompts.selected().preview == "turn-0"
        prompts.goto_bottom()
        prompts.clamp(5)
        assert prompts.selected().preview == "turn-4"

    def test_the_rendered_window_starts_at_the_scroll_offset(self):
        # End to end: the clamp's offset is the slice the panel draws.
        prompts = feed.PromptsState(rows=self._rows(40))
        prompts.cursor = 30
        prompts.clamp(feed.prompts_row_count(24))
        console = Console(width=160, record=True)
        console.print(feed.build_prompts_panel(prompts, 24))
        out = console.export_text()
        assert "turn-30" in out                # the cursor row is on screen
        assert "turn-0" not in out             # scrolled past the newest


class TestPromptsPanelGeometry:
    """PROMPTS_CHROME_ROWS pinned from both sides against the rendered panel, the
    way CHROME_ROWS is: too low crops the key hints, too high wastes a body row."""

    def _panel_lines(self, width, height=24, rows=60):
        turns = [_turn(sid=f"sid{i:05d}", tools=1, preview=f"turn-{i}")
                 for i in range(rows)]
        prompts = feed.PromptsState(rows=turns)
        prompts.loaded_at = prompts.checked_at = 500.0
        console = Console(width=width, record=True)
        console.print(feed.build_prompts_panel(prompts, height, now=500.0))
        return console.export_text().splitlines()

    def _body_rows(self, lines):
        # One body row per turn, each carrying its 8-char session label.
        return sum(1 for line in lines if re.search(r"sid\d{5}", line))

    def test_chrome_rows_matches_the_rendered_panel(self):
        """The body must get exactly `prompts_row_count(h)` rows and the panel
        must still end with its hints. Both widths matter for the same reason as
        the feed's: at 160 the hints fit one line, at 80 they wrap to two and the
        constant's single slack line has to absorb that.

        The two counting assertions are weak on their own: the `Panel` is built
        with `height=term_height`, so `len(lines) == 24` holds at chrome 12-15 and
        `body_rows == prompts_row_count(24)` is true by construction of the same
        constant. What pins the constant is the `q:quit` tail here plus
        `test_chrome_rows_is_not_one_row_too_many` at w=80 (the minimal side)."""
        for width in (80, 160):
            lines = self._panel_lines(width)
            out = "\n".join(lines)
            assert len(lines) == 24, width
            assert self._body_rows(lines) == feed.prompts_row_count(24), width
            assert "q:quit" in out, width      # hint tail, first thing a crop eats
            assert "as of" in out, width       # and the snapshot header survives

    def test_chrome_rows_is_not_one_row_too_many(self):
        """The other side of the pin: one less must overflow, so the constant is
        the smallest value that fits rather than a generous guess that silently
        wastes a body row at every terminal size."""
        with patch.object(feed, "PROMPTS_CHROME_ROWS", feed.PROMPTS_CHROME_ROWS - 1):
            out = "\n".join(self._panel_lines(80))
            assert "q:quit" not in out

    def test_chrome_leaves_one_more_row_than_the_feed(self):
        # The difference is exactly the snapshot header line.
        assert feed.PROMPTS_CHROME_ROWS == feed.CHROME_ROWS + 1
        assert feed.prompts_row_count(40) == feed.visible_row_count(40) - 1

    def test_row_count_floors_at_min_visible_rows(self):
        assert feed.prompts_row_count(1) == feed.MIN_VISIBLE_ROWS

    def test_the_body_is_sized_from_term_height(self):
        """The deferred finding: the panel used to ignore `term_height` for content
        sizing, so a short terminal cropped the status line and the hints. The body
        is now `prompts_row_count(height)` rows at every height the row math covers
        - down to PROMPTS_CHROME_ROWS + MIN_VISIBLE_ROWS - and both survive."""
        floor = feed.PROMPTS_CHROME_ROWS + feed.MIN_VISIBLE_ROWS
        for height in (floor, floor + 1, 24, 50):
            lines = self._panel_lines(160, height=height)
            out = "\n".join(lines)
            assert len(lines) == height, height
            assert self._body_rows(lines) == feed.prompts_row_count(height), height
            assert "q:quit" in out and "1/60" in out, height

    def test_below_the_row_floor_it_degrades_like_the_feed(self):
        """Under that floor MIN_VISIBLE_ROWS wins and the chrome is cropped, the
        same trade-off `visible_row_count` makes for the feed (rows first on a tiny
        terminal) - one row earlier here, since this panel carries one more chrome
        line. Recorded so the threshold cannot move unnoticed."""
        assert feed.prompts_row_count(12) == feed.MIN_VISIBLE_ROWS
        assert "q:quit" not in "\n".join(self._panel_lines(160, height=12))
        assert "c:clear" not in "\n".join(TestVisibleRowCount()._panel_lines(160, 12))


class TestModalExclusion:
    """modal_open()/close_modals() with the prompts turn in the mix: one place
    resets every modal field, or a stale panel renders over the list."""

    # The fields close_modals() deliberately leaves alone. Everything NOT listed
    # here is modal state and must be back at its default afterwards, which is
    # what makes the reset test below derived rather than field-by-field.
    # `search` is on the list because the search modal closes through its own
    # handler (`_apply_search_key`), not through close_modals.
    KEPT_APP_FIELDS = {"entries", "cursor", "scroll_offset", "visible_cols",
                       "tab", "prompts", "prompt_cols", "search"}
    KEPT_PROMPTS_FIELDS = {"rows", "cursor", "scroll_offset", "loaded_at",
                           "checked_at", "status", "session_filter", "hide_empty"}

    def test_modal_open_true_for_each_modal(self):
        entry = {"command": "c", "session_id": "s"}
        for kwargs in ({"detail_entry": entry}, {"session_detail": "s"},
                       {"db_info": [("connection", "/x")]}):
            assert feed.AppState(entries=[entry], **kwargs).modal_open() is True
        state = feed.AppState(entries=[entry])
        assert state.modal_open() is False
        state.prompts.detail = _turn()
        assert state.modal_open() is True

    def test_close_modals_clears_every_field(self):
        """Derived from the dataclasses, not a hand-written field list: a modal
        field added to AppState/PromptsState and forgotten in close_detail must
        fail here.

        Two loops make that work. The first asserts the setup dirtied every modal
        field, so a new field nobody thought to set here fails immediately
        instead of passing vacuously (its value would already be the default);
        the second is the actual reset check.
        """
        entry = {"command": "c", "session_id": "s"}
        state = feed.AppState(
            entries=[entry], detail_entry=entry, session_detail="s",
            session_agent_id="aid-9", session_scroll=4,
            db_info=[("connection", "/x")], detail_tokens={"share": 1},
            session_tokens={"requests": 1}, detail_prompt={"text": "hi"},
            detail_prompt_open=True, detail_prompt_scroll=7)
        state.prompts.detail = _turn()
        state.prompts.actions = [feed.TurnAction(ts="t")]
        state.prompts.detail_text = "the whole prompt"
        state.prompts.detail_scroll = 3
        state.prompts.bash_only = True
        state.prompts.prompt_open = True
        state.prompts.prompt_scroll = 5

        app_defaults, prompt_defaults = feed.AppState(entries=[]), feed.PromptsState()
        modal = [(state, app_defaults, f.name) for f in fields(feed.AppState)
                 if f.name not in self.KEPT_APP_FIELDS]
        modal += [(state.prompts, prompt_defaults, f.name)
                  for f in fields(feed.PromptsState)
                  if f.name not in self.KEPT_PROMPTS_FIELDS]
        for obj, defaults, name in modal:
            assert getattr(obj, name) != getattr(defaults, name), f"setup missed {name}"

        state.close_modals()

        for obj, defaults, name in modal:
            assert getattr(obj, name) == getattr(defaults, name), f"not reset: {name}"
        assert state.modal_open() is False

    def test_close_modals_keeps_the_loaded_prompts_list(self):
        # The list is not modal state: closing a turn must not throw away the
        # snapshot (which would cost another store read to get back).
        state = _prompts_state(rows=3)
        state.prompts.cursor = 2
        state.prompts.scroll_offset = 1
        state.prompts.loaded_at = 123.0
        state.prompts.status = "store busy"
        state.prompts.session_filter = "sid-p0"
        state.prompts.hide_empty = True
        state.prompts.detail = state.prompts.rows[2]
        state.close_modals()
        assert len(state.prompts.rows) == 3
        assert state.prompts.cursor == 2
        assert state.prompts.scroll_offset == 1
        assert state.prompts.loaded_at == 123.0
        assert state.prompts.status == "store busy"
        assert state.prompts.session_filter == "sid-p0"
        assert state.prompts.hide_empty is True

    def test_prompts_detail_closes_with_esc_q_and_enter(self):
        for ch in ("\x1b", "q", "\r"):
            state = _prompts_state(rows=2)
            state.prompts.detail = state.prompts.rows[0]
            assert feed.apply_key(state, ch, 10) is feed.Action.NONE
            assert state.prompts.detail is None
            assert state.tab == "prompts"

    def test_prompts_detail_swallows_navigation(self):
        state = _prompts_state(rows=3)
        state.prompts.detail = state.prompts.rows[0]
        assert feed.apply_key(state, "j", 10) is feed.Action.NONE
        assert state.prompts.cursor == 0          # list nav ignored while modal
        assert state.prompts.detail is not None

    def test_prompts_detail_ctrl_c_still_quits(self):
        state = _prompts_state(rows=1)
        state.prompts.detail = state.prompts.rows[0]
        assert feed.apply_key(state, "\x03", 10) is feed.Action.QUIT

    def test_tab_is_swallowed_while_a_modal_is_open(self):
        """An open modal must eat `Tab`, from either tab and for every modal kind.

        The modals are shared but the panels are per tab, so a switch that got
        through would leave the modal's fields set with no renderer for them: the
        other tab's list draws and the only way out is a key that closes a modal
        the user can no longer see.
        """
        modals = ("detail_entry", "session_detail", "db_info", "prompts.detail")
        for tab in feed.TABS:
            for which in modals:
                state = _prompts_state(rows=1)
                state.tab = tab
                if which == "prompts.detail":
                    state.prompts.detail = state.prompts.rows[0]
                elif which == "detail_entry":
                    state.detail_entry = state.selected_entry()
                elif which == "db_info":
                    state.db_info = [("connection", "/x")]
                else:
                    state.session_detail = "sid-p0"
                assert feed.apply_key(state, "\t", 10) is feed.Action.NONE, (tab, which)
                assert state.tab == tab, (tab, which)
                assert state.modal_open() is True, (tab, which)


# ---- Prompts tab: one turn's detail page (phase 4) ----


def _action(ts="2026-07-29 10:00:05-03", **kw):
    """One TurnAction without a DB, defaulting to a main-thread Bash call."""
    kw.setdefault("tool_name", "Bash")
    return feed.TurnAction(ts=ts, **kw)


def _turn_state(turn=None, actions=(), text="the whole ask", **kw):
    """A PromptsState with a turn open, as it looks after main() has loaded it."""
    turn = turn or _turn(tools=1)
    return feed.PromptsState(rows=[turn], detail=turn, detail_text=text,
                             actions=list(actions) if actions is not None else None,
                             **kw)


def _panel_text(panel, width=120):
    console = Console(width=width, record=True)
    console.print(panel)
    return console.export_text()


def _turn_rows(prompts, height):
    """Action rows the turn page has room for, sized off the prompt it quotes -
    the same two calls `build_turn_panel` and `render_panel` make."""
    return feed._turn_capacity(height, feed._prompt_box_rows(
        feed._turn_prompt_lines(prompts)))


def action_of(tool_name, text, tool_input):
    return feed.action_label(tool_name, text, tool_input)


class TestActionLabel:
    """`action_label`: the store's `text` when it has one, else the first known
    field of the raw `tool_input`. That fallback is what makes the text-less
    tools (Agent, Skill, MCP, ...) readable at all."""

    def test_text_wins_when_the_store_has_one(self):
        assert action_of("Bash", "wc -l x.py", '{"command": "other"}') == "wc -l x.py"

    def test_agent_row_falls_back_to_its_description(self):
        # 644 Agent rows in a real store, every one with text NULL and a
        # description in tool_input: the case the fallback exists for.
        label = action_of("Agent", None,
                          '{"description": "explore the store", '
                          '"subagent_type": "Explore", "prompt": "long..."}')
        assert label == "explore the store"

    def test_each_known_field_is_reachable_in_order(self):
        # One case per field, and the order matters: a tool carrying several
        # (Artifact has file_path AND description) must resolve deterministically.
        cases = [
            ("Skill", '{"skill": "dataviz", "args": "x"}', "dataviz"),
            ("ToolSearch", '{"query": "notebook", "max_results": 5}', "notebook"),
            ("WebFetch", '{"url": "https://x.dev", "prompt": "read it"}', "https://x.dev"),
            ("Artifact", '{"file_path": "/tmp/a.html", "description": "a page"}',
             "a page"),
            ("Read", '{"file_path": "/tmp/a.py"}', "/tmp/a.py"),
            ("Grep", '{"pattern": "def build_"}', "def build_"),
            ("NotebookEdit", '{"notebook_path": "/tmp/n.ipynb"}', "/tmp/n.ipynb"),
            ("Monitor", '{"command": "tail -f log"}', "tail -f log"),
            ("mcp__x__y", '{"path": "/srv/x"}', "/srv/x"),
        ]
        for tool, tool_input, expected in cases:
            assert action_of(tool, None, tool_input) == expected, tool

    def test_no_text_and_no_usable_input_is_empty(self):
        # Malformed JSON, a non-object, a missing input, and an object whose keys
        # are all unknown (the MCP long tail: page_id, channel_id, taskId).
        assert action_of("Skill", None, "{not json") == ""
        assert action_of("Skill", None, "[1, 2]") == ""
        assert action_of("Skill", None, '"a string"') == ""
        assert action_of("Skill", None, None) == ""
        assert action_of("Skill", None, "") == ""
        assert action_of("TaskUpdate", None, '{"taskId": "t1", "status": "done"}') == ""

    def test_a_known_field_holding_a_container_is_skipped(self):
        # AskUserQuestion's `questions` is a list; dumping JSON into the cell
        # would be noise, so a non-string value falls through to the next field.
        assert action_of("X", None, '{"description": {"a": 1}, "path": "/tmp/x"}') \
            == "/tmp/x"
        assert action_of("X", None, '{"query": ["a", "b"]}') == ""
        assert action_of("X", None, '{"query": "   "}') == ""      # blank is not a label

    def test_short_tool_name_drops_the_mcp_server_prefix(self):
        assert feed._short_tool_name(
            "mcp__plugin_chrome-devtools-mcp_chrome-devtools__evaluate_script") \
            == "evaluate_script"
        assert feed._short_tool_name("mcp__notion__notion-fetch") == "notion-fetch"
        assert feed._short_tool_name("Bash") == "Bash"     # plain names untouched
        assert feed._short_tool_name("") == ""


class TestTurnPanelHeader:
    """The turn page's three header rows. The Work row is the one with a trap in
    it: `tools` already INCLUDES `subcalls`."""

    def test_work_row_subtracts_to_get_the_main_thread_count(self):
        # The measured shape: 1 main-thread Bash + 2 subagent calls is
        # tools=3, subcalls=2. Adding them would report 5 calls for 3.
        line = feed._turn_work_line(_turn(tools=3, subcalls=2, agents=1, bash=1))
        assert "3 tool calls" in line.plain
        assert "1 main, 2 across 1 subagent" in line.plain
        assert "5 tool calls" not in line.plain

    def test_work_row_scales_to_the_real_example(self):
        # session 29dea45c's turn: 73 calls of which 41 across 3 subagents.
        line = feed._turn_work_line(
            _turn(tools=73, subcalls=41, agents=3, bash=14))
        assert line.plain == "73 tool calls: 32 main, 41 across 3 subagents  ·  14 Bash"

    def test_work_row_omits_the_split_when_no_subagent_ran(self):
        line = feed._turn_work_line(_turn(tools=9, subcalls=0, agents=0, bash=4))
        assert line.plain == "9 tool calls  ·  4 Bash"

    def test_work_row_omits_bash_when_none_ran(self):
        assert feed._turn_work_line(_turn(tools=2, bash=0)).plain == "2 tool calls"

    def test_work_row_says_a_zero_call_turn_ran_nothing(self):
        line = feed._turn_work_line(_turn(tools=0))
        assert line.plain == "no tool calls"
        assert line.style == "dim"

    def test_bash_is_never_claimed_for_the_main_thread(self):
        """`bash` counts the WHOLE window, subagents included (the store's query
        filters `tool_name='Bash'` over every call in the window), so it may not
        be nested under the main-thread count. Here every Bash call was a
        subagent's: `1 main (3 Bash)` would be arithmetically impossible."""
        line = feed._turn_work_line(_turn(tools=4, subcalls=3, agents=1, bash=3))
        assert "1 main, 3 across 1 subagent" in line.plain
        assert "1 main (3 Bash)" not in line.plain
        assert line.plain.endswith("3 Bash")

    def test_tokens_row_reports_output_subagents_and_requests(self):
        line = feed._turn_tokens_line(
            _turn(output_tokens=24_200, subagent_output_tokens=3_100, requests=15))
        assert line.plain == "24,200 output  ·  3,100 by subagents  ·  15 requests"

    def test_tokens_row_omits_the_subagent_part_when_none_generated(self):
        line = feed._turn_tokens_line(_turn(output_tokens=900, requests=1))
        assert line.plain == "900 output  ·  1 request"

    def test_tokens_row_degrades_to_the_dim_hint_with_no_requests(self):
        # The store attributed no API request to the window: the row says why
        # instead of printing a confident 0, reusing the modals' one wording.
        line = feed._turn_tokens_line(_turn(output_tokens=0, requests=0))
        assert line.plain == feed.PENDING_INGEST_HINT
        assert line.style == "dim italic"

    def test_duration_runs_from_the_prompt_to_the_last_request(self):
        assert feed._turn_duration("2026-07-29 18:58:55-03",
                                   "2026-07-29 19:05:07-03") == "6m 12s"
        assert feed._turn_duration("2026-07-29 18:58:55-03",
                                   "2026-07-29 18:59:07-03") == "12s"
        assert feed._turn_duration("2026-07-29 18:58:55-03",
                                   "2026-07-29 20:05:07-03") == "1h 06m"

    def test_duration_is_none_when_it_cannot_be_computed(self):
        assert feed._turn_duration("2026-07-29 18:58:55-03", None) is None  # no request
        assert feed._turn_duration("", "2026-07-29 19:05:07-03") is None
        assert feed._turn_duration("nonsense", "2026-07-29 19:05:07-03") is None
        # A clock that went backwards is not a negative turn.
        assert feed._turn_duration("2026-07-29 19:05:07-03",
                                   "2026-07-29 18:58:55-03") is None

    def test_header_renders_the_identity_the_page_is_about(self):
        turn = _turn(sid="4be57694-c1f0", ts="2026-07-29 18:58:55-03",
                     last_ts="2026-07-29 19:05:07-03",
                     project="/home/naka/Projects/personal/claude-trail",
                     tools=3, subcalls=2, agents=1, bash=1,
                     output_tokens=2400, requests=4)
        out = _panel_text(feed.build_turn_panel(_turn_state(turn), 40))
        assert "4be57694" in out                       # session label
        assert "claude-trail" in out                   # project tail
        assert f"sent {feed.format_time(turn.ts)}" in out
        assert "ran 6m 12s" in out
        assert "1 main, 2 across 1 subagent" in out
        assert "2,400 output" in out

    def test_header_names_the_directory_from_the_cwd(self):
        # Same rule as the list's Project column: the exact cwd, not the lossy
        # dir-name decoding, so the two views never disagree about a project.
        turn = _turn(sid="4be57694-c1f0", ts="2026-07-29 18:58:55-03", tools=1,
                     project="/home/naka/Projects/personal/keypad/macros",
                     cwd="/home/naka/Projects/personal/keypad-macros")
        out = _panel_text(feed.build_turn_panel(_turn_state(turn), 40))
        assert "keypad-macros" in out
        assert "keypad/macros" not in out

    def test_header_drops_the_duration_when_the_turn_made_no_request(self):
        out = _panel_text(feed.build_turn_panel(_turn_state(_turn(tools=1)), 40))
        assert "ran " not in out
        assert feed.PENDING_INGEST_HINT in out

    def test_the_prompt_box_quotes_the_loaded_text_and_counts_the_rest(self):
        text = "\n".join(f"line {i}" for i in range(30))
        out = _panel_text(feed.build_turn_panel(
            _turn_state(_turn(tools=1), text=text), 40))
        assert "line 0" in out and "line 1" in out
        assert "line 5" not in out                      # only the opening lines
        assert f"+{30 - feed.TURN_PROMPT_PREVIEW_LINES} more lines" in out
        assert "↵ : full prompt" in out                 # there IS one to open

    def test_the_prompt_box_falls_back_to_the_rows_own_preview(self):
        # The full text did not load, so the page quotes the 400-char preview the
        # list query already returned - and must NOT advertise `↵`, which up here
        # would close the page rather than open anything.
        turn = _turn(tools=1, preview="do the thing\nthen the other\nand a third\nand more")
        out = _panel_text(feed.build_turn_panel(_turn_state(turn, text=None), 40))
        assert "do the thing" in out
        assert "+2 more lines" in out
        assert "↵ : full prompt" not in out
        assert "esc / q / ↵ : back" in out              # the hint agrees with it


class TestTurnActionTable:
    """The action list: the agent gutter over the real (oldest-first) order, the
    tool and detail cells, and what an empty list says."""

    def _rows(self):
        # main, then a three-call subagent run, then main again.
        return [
            _action("2026-07-29 10:00:01-03", text="wc -l claude_trail.py"),
            _action("2026-07-29 10:00:02-03", agent_id="ag1", tool_name="Read",
                    text="/x/claude_trail.py"),
            _action("2026-07-29 10:00:03-03", agent_id="ag1", tool_name="Grep",
                    text="def build_"),
            _action("2026-07-29 10:00:04-03", agent_id="ag1", tool_name="Read",
                    text="/x/store.py"),
            _action("2026-07-29 10:00:05-03", text="pytest -q"),
        ]

    AGENTS = {"ag1": {"description": "explore", "type": "Explore"}}

    def _gutters(self, actions, agent_map=None):
        table = feed.build_action_table(actions, agent_map or self.AGENTS)
        return [cell.plain for cell in list(table.columns[1].cells)]

    def test_gutter_brackets_a_run_and_labels_it_once(self):
        assert self._gutters(self._rows()) == ["", "┌ ag1 explore", "│ ", "└ ", ""]

    def test_a_single_call_run_gets_the_standalone_glyph(self):
        rows = [_action(agent_id="ag1", tool_name="Read", text="/x/a.py")]
        assert self._gutters(rows) == ["─ ag1 explore"]

    def test_two_subagents_back_to_back_are_separate_runs(self):
        # Adjacent rows with DIFFERENT agent ids must not be joined into one
        # bracket, or two subagents read as one.
        rows = [_action(agent_id="ag1", tool_name="Read", text="a"),
                _action(agent_id="ag2", tool_name="Read", text="b")]
        agents = {**self.AGENTS, "ag2": {"description": "review", "type": "gp"}}
        assert self._gutters(rows, agents) == ["─ ag1 explore", "─ ag2 review"]

    def test_an_unknown_agent_still_gets_its_short_id(self):
        rows = [_action(agent_id="deadbeef", tool_name="Read", text="a")]
        assert self._gutters(rows, {}) == ["─ dead"]

    def test_runs_are_computed_over_the_rows_actually_passed_in(self):
        """The caller windows before calling, so a run cut by the window closes
        at the screen edge rather than pointing off it."""
        window = self._rows()[1:3]        # the first two of the three-call run
        assert self._gutters(window) == ["┌ ag1 explore", "└ "]

    def test_tool_cell_shortens_mcp_names_and_marks_failures(self):
        rows = [_action(tool_name="mcp__plugin_slack_slack__slack_search_public",
                        tool_input='{"query": "deploy"}', is_error=True),
                _action(tool_name="Bash", text="ls", is_error=False)]
        table = feed.build_action_table(rows)
        cells = list(table.columns[2].cells)
        assert cells[0].plain == "slack_search_public"
        assert cells[0].style == "dim red"            # the call failed
        assert cells[1].style == ""                   # it did not

    def test_a_call_with_no_result_yet_is_not_marked_failed(self):
        # is_error is NULL until the tool_result lands; nothing has gone wrong.
        table = feed.build_action_table([_action(text="ls", is_error=None)])
        assert list(table.columns[2].cells)[0].style == ""

    def test_detail_cell_keeps_the_bash_danger_marker(self):
        table = feed.build_action_table(
            [_action(text="rm -rf /tmp/x"), _action(text="ls -la")])
        cells = list(table.columns[3].cells)
        assert cells[0].plain == "* rm -rf /tmp/x"
        assert cells[1].plain == "ls -la"

    def test_the_danger_marker_is_bash_only(self):
        # A file named after a dangerous verb is not a dangerous command.
        table = feed.build_action_table(
            [_action(tool_name="Read", text="/tmp/rm -rf notes.txt")])
        assert list(table.columns[3].cells)[0].plain == "/tmp/rm -rf notes.txt"

    def test_detail_cell_collapses_whitespace_to_one_line(self):
        table = feed.build_action_table(
            [_action(text="git commit -m 'one\n\ntwo'")])
        assert list(table.columns[3].cells)[0].plain == "git commit -m 'one two'"

    def test_detail_cell_falls_back_to_tool_input(self):
        table = feed.build_action_table(
            [_action(tool_name="Agent", text=None,
                     tool_input='{"description": "explore the store"}')])
        assert list(table.columns[3].cells)[0].plain == "explore the store"

    def test_an_unlabellable_call_shows_a_dim_dash(self):
        table = feed.build_action_table(
            [_action(tool_name="TaskList", text=None, tool_input="{}")])
        cell = list(table.columns[3].cells)[0]
        assert cell.plain == "-"
        assert cell.style == "dim"

    def test_rows_render_oldest_first_the_way_the_work_happened(self):
        out = _panel_text(feed.build_turn_panel(
            _turn_state(actions=self._rows()), 40, agent_map=self.AGENTS))
        assert out.index("wc -l claude_trail.py") < out.index("pytest -q")

    def test_unreadable_actions_say_so_rather_than_showing_an_empty_turn(self):
        # actions is None: the store could not answer. Blaming the turn for
        # running nothing would be a different (and wrong) statement.
        out = _panel_text(feed.build_turn_panel(_turn_state(actions=None), 40))
        assert feed.TURN_UNREADABLE_HINT in out
        assert "No tool calls in this turn" not in out

    def test_a_genuinely_empty_turn_says_that_instead(self):
        out = _panel_text(feed.build_turn_panel(_turn_state(actions=[]), 40))
        assert "No tool calls in this turn" in out
        assert feed.TURN_UNREADABLE_HINT not in out


class TestTurnBashFilter:
    """`b`: a lens over the loaded actions, never a second query."""

    ACTIONS = [_action(text="ls"),
               _action(tool_name="Read", text="/x/a.py"),
               _action(text="pytest -q"),
               _action(tool_name="Edit", text="/x/b.py")]

    def _open(self, rows=2):
        state = _prompts_state(rows=rows)
        feed.apply_key(state, "\r", 10)
        state.prompts.actions = list(self.ACTIONS)
        state.prompts.detail_text = "the ask"
        return state

    def test_visible_actions_is_everything_by_default(self):
        prompts = _turn_state(actions=self.ACTIONS)
        assert prompts.visible_actions() == self.ACTIONS

    def test_bash_only_keeps_only_bash_calls(self):
        prompts = _turn_state(actions=self.ACTIONS, bash_only=True)
        assert [a.text for a in prompts.visible_actions()] == ["ls", "pytest -q"]
        assert len(prompts.actions) == 4          # the loaded list is untouched

    def test_visible_actions_is_empty_when_none_loaded(self):
        assert _turn_state(actions=None).visible_actions() == []

    def test_b_toggles_the_filter_without_asking_for_a_store_read(self):
        state = self._open()
        assert feed.apply_key(state, "b", 10) is feed.Action.NONE
        assert state.prompts.bash_only is True
        assert state.prompts.actions == self.ACTIONS      # nothing was re-fetched
        assert feed.apply_key(state, "b", 10) is feed.Action.NONE
        assert state.prompts.bash_only is False

    def test_b_re_anchors_the_scroll_onto_the_shorter_list(self):
        state = self._open()
        feed.apply_key(state, "G", 10)
        state.prompts.detail_scroll = 3
        feed.apply_key(state, "b", 10)
        assert state.prompts.detail_scroll == 0

    def test_the_filter_is_reset_by_the_next_turn(self):
        # `bash_only` belongs to the turn being read, not to the tab.
        state = self._open()
        feed.apply_key(state, "b", 10)
        feed.apply_key(state, "q", 10)
        feed.apply_key(state, "\r", 10)
        assert state.prompts.bash_only is False

    def test_the_panel_shows_what_the_filter_is_hiding(self):
        prompts = _turn_state(actions=self.ACTIONS, bash_only=True)
        out = _panel_text(feed.build_turn_panel(prompts, 40))
        assert "Actions (2)" in out
        assert "bash only, 2 hidden" in out
        assert "/x/a.py" not in out
        assert "b : all tools" in out             # the hint says how to undo it

    def test_the_filter_says_nothing_about_hiding_when_it_hides_nothing(self):
        prompts = _turn_state(actions=[_action(text="ls")], bash_only=True)
        out = _panel_text(feed.build_turn_panel(prompts, 40))
        assert "bash only" in out
        assert "0 hidden" not in out

    def test_a_turn_with_no_bash_says_so_and_how_to_get_back(self):
        prompts = _turn_state(actions=[_action(tool_name="Read", text="/x/a.py")],
                              bash_only=True)
        out = _panel_text(feed.build_turn_panel(prompts, 40))
        assert "No Bash calls in this turn" in out
        assert "1 other tool call hidden" in out         # singular, not "1 calls"
        assert "press b to show everything" in out
        assert "No tool calls in this turn" not in out   # it ran one


class TestTurnScroll:
    """The action list's scroll: the same clamp-and-write-back contract as the
    session modal, driven through apply_key."""

    def _open(self, n=40):
        state = _prompts_state(rows=2)
        feed.apply_key(state, "\r", 10)
        state.prompts.actions = [_action(text=f"cmd {i}") for i in range(n)]
        state.prompts.detail_text = "the ask"
        return state

    def test_j_k_scroll_and_never_go_negative(self):
        state = self._open()
        feed.apply_key(state, "j", 10)
        feed.apply_key(state, "j", 10)
        assert state.prompts.detail_scroll == 2
        feed.apply_key(state, "k", 10)
        assert state.prompts.detail_scroll == 1
        for _ in range(5):
            feed.apply_key(state, "k", 10)
        assert state.prompts.detail_scroll == 0
        feed.apply_key(state, "g", 10)
        assert state.prompts.detail_scroll == 0
        feed.apply_key(state, "G", 10)
        assert state.prompts.detail_scroll == 10**9      # clamped at render

    def test_arrow_keys_scroll_like_j_and_k(self):
        state = self._open()
        feed.apply_key(state, "down", 10)
        assert state.prompts.detail_scroll == 1
        feed.apply_key(state, "up", 10)
        assert state.prompts.detail_scroll == 0

    def test_clamp_bounds_the_offset_to_the_last_screenful(self):
        box = feed._prompt_box_rows(["one line"])          # the smallest box: 3 rows
        capacity = feed._turn_capacity(30, box)
        assert feed._clamp_turn_scroll(10**9, 40, 30, box) == 40 - capacity
        assert feed._clamp_turn_scroll(-5, 40, 30, box) == 0
        assert feed._clamp_turn_scroll(3, 2, 30, box) == 0     # fewer rows than fit
        assert feed._clamp_turn_scroll(0, 0, 30, box) == 0     # no rows at all

    def test_the_clamp_follows_the_prompt_box_the_panel_will_draw(self):
        # A taller box leaves fewer rows, so `G` has to stop FURTHER down. Both
        # callers pass the same `_prompt_box_rows`, or the written-back offset
        # would disagree with the rows build_turn_panel actually draws.
        short = feed._prompt_box_rows(["one line"])
        tall = feed._prompt_box_rows([f"line {i}" for i in range(30)])
        assert tall > short
        assert (feed._clamp_turn_scroll(10**9, 40, 30, tall)
                > feed._clamp_turn_scroll(10**9, 40, 30, short))

    def test_a_shrinking_list_pulls_the_window_back(self):
        # `b` can cut the list under a parked offset; the panel clamps for
        # display and render_panel writes the same value back.
        box = feed._prompt_box_rows(["one line"])
        assert feed._clamp_turn_scroll(35, 40, 30, box) == 40 - feed._turn_capacity(30, box)
        assert feed._clamp_turn_scroll(35, 4, 30, box) == 0

    def test_the_panel_windows_the_rows_and_reports_the_range(self):
        prompts = _turn_state(actions=[_action(text=f"cmd {i}") for i in range(40)],
                              detail_scroll=5)
        out = _panel_text(feed.build_turn_panel(prompts, 30), width=100)
        capacity = _turn_rows(prompts, 30)
        assert "cmd 5" in out and "cmd 4" not in out       # window starts at 5
        assert f"6-{5 + capacity} of 40" in out
        assert "Actions (40)" in out

    def test_a_list_that_fits_gets_no_range_chip(self):
        prompts = _turn_state(actions=[_action(text=f"cmd {i}") for i in range(3)])
        out = _panel_text(feed.build_turn_panel(prompts, 40))
        assert "Actions (3)" in out
        assert "of 3" not in out


class TestTurnDepths:
    """Enter -> turn page -> Enter -> full prompt, and every way back out."""

    def _open(self, text="line a\nline b"):
        state = _prompts_state(rows=3)
        feed.apply_key(state, "\r", 10)
        state.prompts.actions = [_action(text="ls")]
        state.prompts.detail_text = text          # what main() loaded
        return state

    def test_enter_drills_into_the_full_prompt(self):
        state = self._open()
        assert feed.apply_key(state, "\r", 10) is feed.Action.NONE
        assert state.prompts.prompt_open is True
        assert state.prompts.detail is not None   # the turn page stays under it

    def test_enter_closes_the_page_when_there_is_no_prompt_to_open(self):
        # Same rule the command modal follows: with nothing to open, Enter keeps
        # its one-key exit.
        state = self._open(text=None)
        assert feed.apply_key(state, "\r", 10) is feed.Action.NONE
        assert state.prompts.prompt_open is False
        assert state.prompts.detail is None

    def test_esc_steps_back_one_level_at_a_time(self):
        state = self._open()
        feed.apply_key(state, "\r", 10)                    # into the prompt
        feed.apply_key(state, "\x1b", 10)
        assert state.prompts.prompt_open is False
        assert state.prompts.detail is not None            # back on the turn page
        feed.apply_key(state, "\x1b", 10)
        assert state.prompts.detail is None                # back to the list
        assert state.tab == "prompts"
        assert state.prompts.rows                          # the snapshot survives

    def test_enter_also_steps_back_from_the_full_prompt(self):
        state = self._open()
        feed.apply_key(state, "\r", 10)
        feed.apply_key(state, "\r", 10)
        assert state.prompts.prompt_open is False
        assert state.prompts.detail is not None

    def test_q_closes_everything_from_either_depth(self):
        for depth in (0, 1):
            state = self._open()
            for _ in range(depth):
                feed.apply_key(state, "\r", 10)
            assert feed.apply_key(state, "q", 10) is feed.Action.NONE
            assert state.prompts.detail is None
            assert state.prompts.prompt_open is False
            assert state.prompts.detail_text is None
            assert state.modal_open() is False

    def test_ctrl_c_quits_from_either_depth(self):
        for depth in (0, 1):
            state = self._open()
            for _ in range(depth):
                feed.apply_key(state, "\r", 10)
            assert feed.apply_key(state, "\x03", 10) is feed.Action.QUIT

    def test_the_prompt_scroll_resets_when_the_view_reopens(self):
        state = self._open()
        feed.apply_key(state, "\r", 10)
        feed.apply_key(state, "j", 10)
        assert state.prompts.prompt_scroll == 1
        feed.apply_key(state, "\x1b", 10)
        feed.apply_key(state, "\r", 10)
        assert state.prompts.prompt_scroll == 0

    def test_the_two_scrolls_do_not_bleed_into_each_other(self):
        state = self._open()
        feed.apply_key(state, "j", 10)                     # the action list
        feed.apply_key(state, "j", 10)
        feed.apply_key(state, "\r", 10)                    # into the prompt
        feed.apply_key(state, "j", 10)
        assert (state.prompts.detail_scroll, state.prompts.prompt_scroll) == (2, 1)

    def test_the_turn_page_swallows_the_list_keys(self):
        # `r`, `x`, `S` and the digits belong to the list under it.
        state = self._open()
        for ch in ("r", "x", "S", "1", "7", "\t", "/", "f", "c", "s", "d", "o"):
            assert feed.apply_key(state, ch, 10) is feed.Action.NONE, ch
            assert state.prompts.detail is not None, ch
        assert state.prompts.hide_empty is False
        assert state.prompts.session_filter is None
        assert state.tab == "prompts"
        assert all(state.prompt_cols.values())

    def test_the_full_prompt_view_swallows_b(self):
        # `b` filters the list under the prompt; up here it must do nothing.
        state = self._open()
        feed.apply_key(state, "\r", 10)
        assert feed.apply_key(state, "b", 10) is feed.Action.NONE
        assert state.prompts.bash_only is False

    def test_the_full_prompt_renders_the_turns_identity(self):
        state = self._open(text="\n".join(f"line {i}" for i in range(30)))
        feed.apply_key(state, "\r", 10)
        turn = state.prompts.detail
        out = _panel_text(feed.build_prompt_panel(
            turn.session_id, 30, state.prompts.detail_text, ts=turn.ts))
        assert "30 lines" in out
        assert turn.session_id[:8] in out
        assert turn.ts[:19] in out                          # the Sent row


class TestTurnPromptCap:
    """The full-prompt view caps what it materializes, and says what it cut."""

    def test_a_normal_prompt_is_untouched(self):
        assert feed._cap_prompt_text("short") == "short"
        assert feed._cap_prompt_text(None) is None
        assert feed._cap_prompt_text("") == ""
        exact = "x" * feed.PROMPT_TEXT_CHAR_CAP
        assert feed._cap_prompt_text(exact) == exact       # the boundary is kept

    def test_an_oversized_prompt_is_cut_and_says_how_much(self):
        # The store-wide longest main-thread turn measured 646,245 chars, 30x the
        # 21,697 max of the window the list scans - and turn_prompt_text has no
        # cap of its own, so this is the only place that bounds it.
        text = "y" * 646_245
        capped = feed._cap_prompt_text(text)
        assert len(capped) < len(text)
        assert capped.endswith("y" * feed.PROMPT_TEXT_CHAR_CAP)   # notice, then the text
        dropped = 646_245 - feed.PROMPT_TEXT_CHAR_CAP
        assert capped.startswith(
            f"… showing the first {feed.PROMPT_TEXT_CHAR_CAP:,} of 646,245 characters")
        assert f"({dropped:,} not shown)" in capped

    def test_the_cut_is_visible_in_the_view_itself(self):
        """The notice has to survive the VIEW, not just live in the string.

        The body here WRAPS at the test width, which is the case a trailing
        notice could not survive: the view windows by logical line while the
        Panel crops by display row, so the last window renders fewer lines than
        it holds and the notice was always among the ones dropped. `line 20` is
        inside the window and still absent, which is that crop happening - the
        proof this test is not passing on a body that simply fits.
        """
        body = "\n".join(f"line {i} " + "word " * 30 for i in range(200))
        capped = feed._cap_prompt_text(body, cap=5_000)
        assert len(capped.splitlines()) > feed._prompt_capacity(40)   # windowed
        out = _panel_text(feed.build_prompt_panel("s1", 40, capped), width=120)
        assert "not shown" in out                  # the notice, on the first frame
        assert "line 0 " in out                    # the body starts under it
        assert "line 20 " not in out               # ... and the crop ate this one

    def test_the_loader_caps_before_the_text_reaches_the_view(self, tmp_path, monkeypatch):
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        monkeypatch.setattr(feed, "_with_store",
                            lambda fn, **kw: ([], "w" * 300_000))
        _actions, text = feed._load_turn_detail(_turn(tools=0))
        assert len(text) < 300_000
        assert "not shown" in text


class TestTurnDetailLoader:
    """`_load_turn_detail`: one read-only store open per opened turn, and the
    three answers the page tells apart."""

    def _store_at(self, tmp_path, monkeypatch, exists=True):
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        if exists:
            db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        return store

    def test_no_turn_or_a_turn_with_no_key_loads_nothing(self):
        assert feed._load_turn_detail(None) == (None, None)
        assert feed._load_turn_detail(feed.PromptTurn(session_id="", ts="t")) \
            == (None, None)
        assert feed._load_turn_detail(feed.PromptTurn(session_id="s", ts="")) \
            == (None, None)

    def test_both_halves_come_from_one_store_open(self, tmp_path, monkeypatch):
        # A second open per modal would double what `↵` costs.
        self._store_at(tmp_path, monkeypatch)
        opens = []

        def fake_with_store(fn, **kw):
            opens.append(fn)
            return [feed.TurnAction(ts="t", tool_name="Bash")], "the ask"

        monkeypatch.setattr(feed, "_with_store", fake_with_store)
        actions, text = feed._load_turn_detail(_turn(tools=1))
        assert len(opens) == 1
        assert [a.tool_name for a in actions] == ["Bash"]
        assert text == "the ask"

    def test_it_passes_the_turns_own_window_to_the_query(self, tmp_path, monkeypatch):
        # The window bounds and the event id are what make this ONE turn rather
        # than the session's whole history.
        store = self._store_at(tmp_path, monkeypatch)
        seen = {}

        def fake_turn_actions(con, sid, ts, next_ts=None, **kw):
            seen.update(sid=sid, ts=ts, next_ts=next_ts)
            return [{"ts": "2026-07-29 10:00:01-03", "agent_id": None,
                     "tool_name": "Bash", "text": "ls", "tool_input": None,
                     "is_error": False}]

        monkeypatch.setattr(store, "turn_actions", fake_turn_actions)
        monkeypatch.setattr(store, "turn_prompt_text",
                            lambda con, event_id: f"text of {event_id}")
        monkeypatch.setattr(feed, "_with_store", lambda fn, **kw: fn(None))
        turn = _turn(sid="s1", ts="2026-07-29 10:00:00-03",
                     next_ts="2026-07-29 10:30:00-03", event_id="ev-9")
        actions, text = feed._load_turn_detail(turn)
        assert seen == {"sid": "s1", "ts": "2026-07-29 10:00:00-03",
                        "next_ts": "2026-07-29 10:30:00-03"}
        assert [a.text for a in actions] == ["ls"]      # mapped onto the record
        assert text == "text of ev-9"

    def test_a_store_that_cannot_answer_gives_none_not_an_empty_list(
            self, tmp_path, monkeypatch):
        # None and [] are different statements on the page (see the panel tests),
        # so the loader must not flatten them.
        self._store_at(tmp_path, monkeypatch, exists=False)
        assert feed._load_turn_detail(_turn(tools=1)) == (None, None)

    def test_an_empty_turn_answers_with_an_empty_list(self, tmp_path, monkeypatch):
        self._store_at(tmp_path, monkeypatch)
        monkeypatch.setattr(feed, "_with_store", lambda fn, **kw: ([], None))
        assert feed._load_turn_detail(_turn(tools=0)) == ([], None)

    def test_a_turn_with_no_event_id_skips_the_prompt_query(self, tmp_path, monkeypatch):
        store = self._store_at(tmp_path, monkeypatch)
        called = []
        monkeypatch.setattr(store, "turn_actions", lambda *a, **k: [])
        monkeypatch.setattr(store, "turn_prompt_text",
                            lambda *a, **k: called.append(a) or "x")
        monkeypatch.setattr(feed, "_with_store", lambda fn, **kw: fn(None))
        turn = feed.PromptTurn(session_id="s", ts="2026-07-29 10:00:00-03",
                               event_id="")
        assert feed._load_turn_detail(turn) == ([], None)
        assert called == []


class TestTurnPanelGeometry:
    """TURN_CHROME_ROWS plus the per-turn prompt box, measured by rendering
    rather than counted: the page's header is tall, so one row too few silently
    eats the key hints and one row too many leaves the list short."""

    PROMPT_30 = "\n".join(f"line {i}" for i in range(30))

    def _panel(self, height, width, actions=40, text=PROMPT_30):
        prompts = _turn_state(
            actions=[_action(text=f"cmd {i}") for i in range(actions)],
            text=text)
        console = Console(width=width, record=True)
        console.print(feed.build_turn_panel(prompts, height))
        return console.export_text()

    def _rows_drawn(self, out):
        return len(re.findall(r"cmd \d+", out))

    def test_the_hint_and_the_header_survive_at_24_rows(self):
        # `len(splitlines()) == 24` is NOT the pin here (build_turn_panel passes
        # height= to the Panel, so it holds at any chrome); the real pins are
        # test_chrome_one_row_too_small_crops_the_hint below it and the row
        # counts in test_a_short_prompt_gets_its_rows_back.
        for width in (80, 110, 160):
            out = self._panel(24, width)
            assert "b : bash only" in out, width      # the hint line
            assert "Actions (40)" in out, width
            assert "Session" in out, width
            assert len(out.splitlines()) == 24, width

    def test_chrome_one_row_too_small_crops_the_hint(self):
        # The pin from below: at TURN_CHROME_ROWS - 1 the body claims the hint's
        # row at every width, so the current value is minimal, not padded.
        with patch.object(feed, "TURN_CHROME_ROWS", feed.TURN_CHROME_ROWS - 1):
            for width in (80, 110, 160):
                assert "b : bash only" not in self._panel(24, width), width

    def test_a_short_prompt_gets_its_rows_back(self):
        """The pin from ABOVE: the box is sized from the prompt, not reserved.

        A one-line prompt has no `+N more lines` footer and no blank above it, so
        its box is 3 rows where the worst case is 6. Reserving the worst case (the
        old TURN_CHROME_ROWS = 18) left those 3 rows blank on 135 of the newest
        200 turns in a real store - a third of the usable list at h=24. The
        literals below are what the panel DRAWS, so over-reserving again fails
        here even though the hint would still be on screen.
        """
        for width in (80, 160):
            short = self._panel(24, width, text="update the tracker")
            assert self._rows_drawn(short) == 9, width          # was 6
            assert "b : bash only" in short, width
            long = self._panel(24, width, text=self.PROMPT_30)
            assert self._rows_drawn(long) == 6, width           # the worst case
            assert "b : bash only" in long, width

    def test_no_row_is_wasted_between_the_list_and_the_hint(self):
        # The same pin stated as layout: the hint sits DIRECTLY under the last
        # action row. One row of chrome too many puts a blank line in between.
        for width in (80, 160):
            for text, last in (("update the tracker", "cmd 8"),
                               (self.PROMPT_30, "cmd 5")):
                lines = self._panel(24, width, text=text).splitlines()
                hint = next(i for i, ln in enumerate(lines) if "b : bash only" in ln)
                assert last in lines[hint - 1], (width, text)

    def test_a_long_prompt_line_does_not_steal_the_lists_rows(self):
        # The box can only be measured exactly if a quoted line takes exactly one
        # row, so the turn page quotes `no_wrap` (ellipsized). A 300-char opening
        # line would otherwise wrap to 3-4 rows and crop the hint off the page.
        for width in (80, 160):
            out = self._panel(24, width, text="x" * 300)
            assert self._rows_drawn(out) == 9, width
            assert "b : bash only" in out, width

    def test_the_box_is_measured_from_what_the_page_will_quote(self):
        assert feed._prompt_box_rows([]) == 3               # the `(empty)` row
        assert feed._prompt_box_rows(["one"]) == 3
        assert feed._prompt_box_rows(["one", "two"]) == 4   # both fit, no footer
        assert feed._prompt_box_rows(["one", "two", "three"]) == 6  # + blank + footer
        # And it measures the lines the page actually shows: the full text when
        # `↵` loaded one, else the row's own preview.
        turn = _turn(tools=1, preview="a\nb\nc\nd")
        assert feed._turn_prompt_lines(_turn_state(turn, text="just this")) == ["just this"]
        assert feed._turn_prompt_lines(_turn_state(turn, text=None)) == ["a", "b", "c", "d"]

    def test_capacity_grows_with_the_terminal_and_has_a_floor(self):
        box = feed._prompt_box_rows(["one line"])
        # The floor assert is the meaningful half; the first line restates the
        # implementation and is here to name the two things that are subtracted.
        assert feed._turn_capacity(40, box) == 40 - feed.TURN_CHROME_ROWS - box
        assert feed._turn_capacity(10, box) == feed.MIN_VISIBLE_ROWS   # tiny terminal


class TestPromptView:
    """The full-prompt view drilled into from the command modal with Enter:
    key handling (drill in, step back, close) and build_prompt_panel windowing."""

    PROMPT = {"text": "\n".join(f"line {i}" for i in range(1, 41)), "ts": "2026-06-30 09:00:00-03"}

    def _open_detail(self, prompt=None):
        entries = [{"command": "ls", "session_id": "sid-0", "cwd": "/work",
                    "timestamp": "2026-06-30T12:00:00+00:00"}]
        return feed.AppState(entries=entries, detail_entry=entries[0],
                             detail_prompt=prompt if prompt is not None else self.PROMPT)

    def _text(self, panel, width=120):
        console = Console(width=width, record=True)
        console.print(panel)
        return console.export_text()

    def test_enter_opens_full_prompt_instead_of_closing(self):
        state = self._open_detail()
        assert feed.apply_key(state, "\r", 10) is feed.Action.NONE
        assert state.detail_prompt_open is True
        assert state.detail_entry is not None       # the command modal stays under it

    def test_enter_still_closes_when_prompt_unavailable(self):
        # The dim hint sentinel is not a prompt: Enter must not open an empty view.
        state = self._open_detail(prompt={"unavailable": "pending ingest"})
        assert feed.apply_key(state, "\r", 10) is feed.Action.NONE
        assert state.detail_prompt_open is False
        assert state.detail_entry is None

    def test_esc_steps_back_to_the_command_modal(self):
        state = self._open_detail()
        feed.apply_key(state, "\r", 10)
        feed.apply_key(state, "\x1b", 10)
        assert state.detail_prompt_open is False
        assert state.detail_entry is not None       # back one level, not closed

    def test_enter_steps_back_from_the_full_prompt(self):
        state = self._open_detail()
        feed.apply_key(state, "\r", 10)
        feed.apply_key(state, "\r", 10)
        assert state.detail_prompt_open is False
        assert state.detail_entry is not None

    def test_q_closes_everything_from_the_full_prompt(self):
        # q is the one-key exit from any depth, so a deep view is never a trap.
        state = self._open_detail()
        feed.apply_key(state, "\r", 10)
        assert feed.apply_key(state, "q", 10) is feed.Action.NONE
        assert state.detail_prompt_open is False
        assert state.detail_entry is None
        assert state.detail_prompt is None

    def test_ctrl_c_still_quits_from_the_full_prompt(self):
        state = self._open_detail()
        feed.apply_key(state, "\r", 10)
        assert feed.apply_key(state, "\x03", 10) is feed.Action.QUIT

    def test_scroll_keys_move_and_never_go_negative(self):
        state = self._open_detail()
        feed.apply_key(state, "\r", 10)
        feed.apply_key(state, "j", 10)
        feed.apply_key(state, "j", 10)
        assert state.detail_prompt_scroll == 2
        feed.apply_key(state, "k", 10)
        assert state.detail_prompt_scroll == 1
        feed.apply_key(state, "k", 10)
        feed.apply_key(state, "k", 10)
        assert state.detail_prompt_scroll == 0      # clamped at the top
        feed.apply_key(state, "G", 10)
        assert state.detail_prompt_scroll == 10**9  # sentinel, clamped at render

    def test_scroll_resets_when_the_view_reopens(self):
        state = self._open_detail()
        feed.apply_key(state, "\r", 10)
        feed.apply_key(state, "j", 10)
        feed.apply_key(state, "\x1b", 10)           # back to the command modal
        feed.apply_key(state, "\r", 10)             # and in again
        assert state.detail_prompt_scroll == 0

    def test_panel_windows_the_body_and_reports_the_range(self):
        out = self._text(feed.build_prompt_panel(
            "sid-0", 30, self.PROMPT["text"], ts=self.PROMPT["ts"], scroll=5))
        capacity = feed._prompt_capacity(30)
        assert "line 6" in out                       # window starts at the offset
        assert f"6-{5 + capacity} of 40" in out
        assert "line 1\n" not in out                 # scrolled past
        assert "40 lines" in out
        assert "2026-06-30 09:00:00" in out          # Sent row, offset sliced off

    def test_panel_shows_whole_prompt_when_it_fits(self):
        out = self._text(feed.build_prompt_panel("s", 40, "a\nb\nc"))
        assert "a" in out and "b" in out and "c" in out
        assert "of 3" not in out                     # no range chip when unwindowed
        assert "Sent" not in out                     # no ts, no row

    def test_panel_takes_plain_values_so_both_tabs_can_call_it(self):
        """The refactor: identity comes in as `(session_id, agent_id, ts)` rather
        than a LogEntry, because a Prompts-tab turn has no log entry to derive it
        from. Both shapes of caller are exercised here."""
        agent_map = {"aid1": {"description": "explore", "type": "Explore"}}
        subagent = self._text(feed.build_prompt_panel(
            "sid-0", 40, "ask", ts="2026-06-30 09:00:00-03", agent_id="aid1",
            name_map={"sid-0": "my-session"}, agent_map=agent_map))
        assert "my-session" in subagent                  # resolved like the feed
        assert "aid1 explore" in subagent                # Agent row from the id
        # A main-thread turn (the Prompts tab's only kind) has no Agent row.
        assert "Agent" not in self._text(feed.build_prompt_panel(
            "sid-0", 40, "ask", ts="2026-06-30 09:00:00-03", agent_map=agent_map))

    def test_clamp_bounds_the_scroll_to_the_last_screenful(self):
        capacity = feed._prompt_capacity(30)
        assert feed._clamp_prompt_scroll(10**9, 40, 30) == 40 - capacity
        assert feed._clamp_prompt_scroll(-5, 40, 30) == 0
        assert feed._clamp_prompt_scroll(3, 2, 30) == 0   # fewer lines than fit


class TestSessionModal:
    """The `s` session-detail modal: opening state, modal key handling, and the
    build_session_panel rendering."""

    def _entries(self, n):
        return [{"command": f"c{i}", "session_id": f"sid-{i}"} for i in range(n)]

    def test_s_opens_session_detail_for_selected_session(self):
        state = feed.AppState(entries=self._entries(3), cursor=0)
        result = feed.apply_key(state, "s", 10)
        # Opening a session asks main() to load its token total.
        assert result is feed.Action.OPEN_SESSION_DETAIL
        # cursor 0 = newest = last appended (sid-2)
        assert state.session_detail == "sid-2"
        assert state.modal_open() is True

    def test_s_without_selectable_entry_leaves_modal_closed(self):
        state = feed.AppState(entries=[])
        result = feed.apply_key(state, "s", 10)
        assert result is feed.Action.NONE
        assert state.session_detail is None
        assert state.modal_open() is False

    def test_s_on_empty_session_id_leaves_modal_closed(self):
        # a hand-edited/legacy line with an empty session_id must not open a
        # blank modal ("" is falsy, so session_detail stays None)
        state = feed.AppState(entries=[{"command": "c", "session_id": ""}], cursor=0)
        result = feed.apply_key(state, "s", 10)
        assert result is feed.Action.NONE
        assert state.session_detail is None
        assert state.modal_open() is False

    def test_session_modal_swallows_navigation(self):
        state = feed.AppState(entries=self._entries(3), cursor=1, session_detail="sid-0")
        result = feed.apply_key(state, "j", 10)
        assert result is feed.Action.NONE
        assert state.cursor == 1              # navigation ignored while modal open
        assert state.session_detail == "sid-0"  # still open

    def test_session_modal_swallows_s(self):
        # pressing s again while the modal is open must not re-trigger the opener
        state = feed.AppState(entries=self._entries(3), cursor=0, session_detail="sid-0")
        result = feed.apply_key(state, "s", 10)
        assert result is feed.Action.NONE
        assert state.session_detail == "sid-0"

    def test_session_modal_esc_closes(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0")
        result = feed.apply_key(state, "\x1b", 10)
        assert result is feed.Action.NONE
        assert state.session_detail is None

    def test_session_modal_q_closes_not_quits(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0")
        result = feed.apply_key(state, "q", 10)
        assert result is feed.Action.NONE
        assert state.session_detail is None

    def test_session_modal_enter_closes(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0")
        result = feed.apply_key(state, "\r", 10)
        assert result is feed.Action.NONE
        assert state.session_detail is None

    def test_session_modal_ctrl_c_still_quits(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0")
        assert feed.apply_key(state, "\x03", 10) is feed.Action.QUIT

    def test_close_clears_both_modal_fields(self):
        # if both were somehow set, esc clears both so they stay mutually exclusive
        entries = self._entries(3)
        state = feed.AppState(entries=entries, detail_entry=entries[0],
                              session_detail="sid-0")
        feed.apply_key(state, "\x1b", 10)
        assert state.detail_entry is None
        assert state.session_detail is None

    def test_s_on_subagent_row_tracks_agent_id(self):
        # opening from a subagent row records its agent id to spotlight it
        state = feed.AppState(
            entries=[{"command": "c", "session_id": "sid-0", "agent_id": "aid-9"}],
            cursor=0)
        feed.apply_key(state, "s", 10)
        assert state.session_detail == "sid-0"
        assert state.session_agent_id == "aid-9"

    def test_s_on_main_agent_row_leaves_agent_id_none(self):
        # a main-agent row (no agent_id) opens the page with nothing spotlighted
        state = feed.AppState(
            entries=[{"command": "c", "session_id": "sid-0"}], cursor=0)
        feed.apply_key(state, "s", 10)
        assert state.session_detail == "sid-0"
        assert state.session_agent_id is None

    def test_close_clears_session_agent_id(self):
        state = feed.AppState(entries=self._entries(3),
                              session_detail="sid-0", session_agent_id="aid-9")
        feed.apply_key(state, "\x1b", 10)
        assert state.session_detail is None
        assert state.session_agent_id is None


class TestSessionScroll:
    """Scrolling the session modal's subagent list: the `session_scroll` key
    handling (j/k/g/G) while the modal is open, its reset on open/close, and the
    build_session_panel window math (mirrors the search-panel windowing tests)."""

    def _entries(self, n):
        return [{"command": f"c{i}", "session_id": f"sid-{i}"} for i in range(n)]

    def _model(self, n_sub):
        subs = tuple(
            feed.SubagentInfo(
                agent_id=f"a{i:03d}", agent_type="Explore",
                description=f"sub-{i}", status="done", command_count=i)
            for i in range(n_sub)
        )
        return feed.SessionModel(
            session_id="sid-0", name="nm", status="busy", live=True,
            cwd="/home/naka/x", version="2.1", kind="interactive",
            started_at=1783058624928, transcript_path="/t/x.jsonl", subagents=subs)

    def _text(self, panel, width=100):
        console = Console(width=width, record=True)
        console.print(panel)
        return console.export_text()

    # --- key handling (state mutation) ---

    def test_j_scrolls_subagent_list_down_not_the_cursor(self):
        # while the session modal is open, j/k move session_scroll, not the feed
        # cursor, and never close the modal (Action.NONE)
        state = feed.AppState(entries=self._entries(3), cursor=1, session_detail="sid-0")
        assert feed.apply_key(state, "j", 10) is feed.Action.NONE
        assert state.session_scroll == 1
        assert state.cursor == 1                 # feed cursor untouched
        assert state.session_detail == "sid-0"   # modal still open

    def test_k_scrolls_up_and_floors_at_zero(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0",
                              session_scroll=2)
        feed.apply_key(state, "k", 10)
        assert state.session_scroll == 1
        feed.apply_key(state, "k", 10)
        feed.apply_key(state, "k", 10)  # already at 0, must not go negative
        assert state.session_scroll == 0

    def test_arrow_keys_scroll_like_jk(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0")
        feed.apply_key(state, "down", 10)
        assert state.session_scroll == 1
        feed.apply_key(state, "up", 10)
        assert state.session_scroll == 0

    def test_g_scrolls_to_top(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0",
                              session_scroll=5)
        feed.apply_key(state, "g", 10)
        assert state.session_scroll == 0

    def test_G_sets_bottom_sentinel(self):
        # G parks a sentinel that build_session_panel / render clamp to the bottom
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0")
        feed.apply_key(state, "G", 10)
        assert state.session_scroll == 10**9

    def test_open_resets_scroll(self):
        # a stale offset from a previous open must not carry into the new modal
        state = feed.AppState(entries=self._entries(3), cursor=0, session_scroll=7)
        feed.apply_key(state, "s", 10)
        assert state.session_detail == "sid-2"   # cursor 0 = newest
        assert state.session_scroll == 0

    def test_close_resets_scroll(self):
        state = feed.AppState(entries=self._entries(3), session_detail="sid-0",
                              session_scroll=7)
        feed.apply_key(state, "\x1b", 10)
        assert state.session_detail is None
        assert state.session_scroll == 0

    def test_scroll_keys_do_not_leak_to_command_modal(self):
        # the command-detail modal keeps its full-swallow behavior: j is ignored
        # and leaves session_scroll at 0
        entries = self._entries(3)
        state = feed.AppState(entries=entries, detail_entry=entries[0])
        assert feed.apply_key(state, "j", 10) is feed.Action.NONE
        assert state.session_scroll == 0
        assert state.detail_entry is entries[0]  # still open

    # --- clamp helper ---

    def test_clamp_helper_bounds_scroll(self):
        m = self._model(8)               # capacity 3 at term_height 20 -> max 5
        assert feed._clamp_session_scroll(10**9, m, 20) == 5   # G sentinel -> bottom
        assert feed._clamp_session_scroll(-4, m, 20) == 0      # never negative
        assert feed._clamp_session_scroll(2, m, 20) == 2       # in range untouched

    def test_clamp_helper_fits_pins_to_zero(self):
        m = self._model(2)               # fits the window -> only valid offset is 0
        assert feed._clamp_session_scroll(9, m, 20) == 0

    # --- window rendering ---

    def test_long_list_clips_to_window_and_reports_range(self):
        # capacity 3 (term_height 20): the top window shows the first 3 subagents
        # and the header reports the "n-m of N" range
        out = self._text(feed.build_session_panel(self._model(8), term_height=20,
                                                  scroll=0))
        assert "sub-0" in out and "sub-2" in out
        assert "sub-3" not in out and "sub-7" not in out
        assert "1-3 of 8" in out

    def test_G_sentinel_renders_bottom_window(self):
        # the huge sentinel is clamped for display to the last full window
        out = self._text(feed.build_session_panel(self._model(8), term_height=20,
                                                  scroll=10**9))
        assert "sub-5" in out and "sub-7" in out
        assert "sub-4" not in out and "sub-0" not in out
        assert "6-8 of 8" in out

    def test_j_then_render_steps_window_by_one(self):
        out = self._text(feed.build_session_panel(self._model(8), term_height=20,
                                                  scroll=1))
        assert "sub-1" in out and "sub-3" in out
        assert "sub-0" not in out and "sub-4" not in out
        assert "2-4 of 8" in out

    def test_short_list_renders_all_rows_without_range_hint(self):
        out = self._text(feed.build_session_panel(self._model(3), term_height=20,
                                                  scroll=0))
        assert "sub-0" in out and "sub-1" in out and "sub-2" in out
        assert " of 3" not in out                 # no range indicator when it fits
        assert "Subagents (3, 0 running)" in out

    def test_windowing_preserves_hint_footer(self):
        # the back hint must never be pushed off the height-cropped panel, even at
        # the worst-case metadata height (highlight Subagent row + Tokens row)
        tokens = {"requests": 1, "output_tokens": 5, "input_tokens": 1,
                  "cache_read_tokens": 0, "cache_creation_tokens": 0,
                  "subagent_output_tokens": 0}
        out = self._text(feed.build_session_panel(
            self._model(30), term_height=25, tokens=tokens,
            highlight_agent_id="a000", scroll=10**9))
        assert "esc / q" in out                   # footer survived the crop
        assert "23-30 of 30" in out               # bottom window
        assert "Tokens" in out                    # token row not regressed


class TestDatabaseModal:
    """The `d` database modal: the load action, modal key handling, the
    build_db_panel rendering, and the lazy loader's graceful fallback."""

    def _entries(self, n):
        return [{"command": f"c{i}", "session_id": f"sid-{i}"} for i in range(n)]

    def test_d_returns_load_db_info_action(self):
        # apply_key stays pure: it returns the action but does not open the modal
        # or touch duckdb itself (main() performs the lazy load).
        state = feed.AppState(entries=self._entries(3), cursor=0)
        result = feed.apply_key(state, "d", 10)
        assert result is feed.Action.LOAD_DB_INFO
        assert state.db_info is None
        assert state.modal_open() is False

    def test_db_info_set_marks_modal_open(self):
        state = feed.AppState(entries=self._entries(1), db_info=[("connection", "/x")])
        assert state.modal_open() is True

    def test_db_modal_swallows_navigation(self):
        state = feed.AppState(entries=self._entries(3), cursor=1,
                              db_info=[("connection", "/x")])
        result = feed.apply_key(state, "j", 10)
        assert result is feed.Action.NONE
        assert state.cursor == 1               # navigation ignored while modal open
        assert state.db_info is not None       # still open

    def test_db_modal_swallows_d(self):
        # pressing d again while the modal is open must not re-trigger the loader
        state = feed.AppState(entries=self._entries(3), db_info=[("connection", "/x")])
        result = feed.apply_key(state, "d", 10)
        assert result is feed.Action.NONE
        assert state.db_info is not None

    def test_db_modal_esc_closes(self):
        state = feed.AppState(entries=self._entries(1), db_info=[("connection", "/x")])
        result = feed.apply_key(state, "\x1b", 10)
        assert result is feed.Action.NONE
        assert state.db_info is None

    def test_db_modal_q_closes_not_quits(self):
        state = feed.AppState(entries=self._entries(1), db_info=[("connection", "/x")])
        assert feed.apply_key(state, "q", 10) is feed.Action.NONE
        assert state.db_info is None

    def test_build_db_panel_renders_rows(self):
        console = Console(width=100, record=True)
        panel = feed.build_db_panel(
            [("connection", "/home/x/.claude/trail.duckdb"), ("rows events", "5")],
            term_height=20)
        console.print(panel)
        out = console.export_text()
        assert "database" in out               # panel title suffix
        assert "connection" in out
        assert "trail.duckdb" in out
        assert "rows events" in out

    def test_load_db_info_missing_store_reports_hint(self, tmp_path, monkeypatch):
        # No live store: the loader must not raise; it returns a status hint. The
        # `from claude_trail_store import STORE_PATH` inside _load_db_info reads the
        # patched module attribute at call time.
        import claude_trail_store as store
        monkeypatch.setattr(store, "STORE_PATH", tmp_path / "nope.duckdb")
        info = dict(feed._load_db_info())
        assert "no store yet" in info.get("status", "")

    def _model(self, **kw):
        subagents = kw.pop("subagents", (
            feed.SubagentInfo(
                agent_id="run1", agent_type="Explore",
                description="Search the tree", status="running", command_count=1),
            feed.SubagentInfo(
                agent_id="done1", agent_type="general-purpose",
                description="Review Phase 4", status="done", command_count=3),
        ))
        base = dict(
            session_id="4819367e-5b3d-4237-8ea5-67af6bca91de",
            name="merge-phase-2-tests-cleanup",
            status="busy", live=True,
            cwd="/home/naka/Projects/personal/claude-trail",
            version="2.1.200", kind="interactive",
            started_at=1783058624928,
            transcript_path="/home/naka/.claude/projects/-p/4819367e.jsonl",
            subagents=subagents,
        )
        base.update(kw)
        return feed.SessionModel(**base)

    def _text(self, panel, width=100):
        console = Console(width=width, record=True)
        console.print(panel)
        return console.export_text()

    def test_panel_shows_id_transcript_and_subagent_states(self):
        out = self._text(feed.build_session_panel(self._model(), term_height=30))
        assert "4819367e-5b3d-4237-8ea5-67af6bca91de" in out  # session id
        assert "4819367e.jsonl" in out                        # transcript path
        assert "Search the tree" in out and "running" in out  # a running subagent
        assert "Review Phase 4" in out and "done" in out       # a done subagent
        assert "run1" in out  # short agent id, to correlate with the feed
        assert "Subagents (2, 1 running)" in out

    def test_panel_shows_token_total_when_present(self):
        tokens = {"requests": 42, "output_tokens": 12345, "input_tokens": 6789,
                  "cache_read_tokens": 1_000_000, "cache_creation_tokens": 234_567,
                  "subagent_output_tokens": 0}
        out = self._text(feed.build_session_panel(self._model(), term_height=30,
                                                  tokens=tokens))
        assert "Tokens" in out
        assert "12,345" in out               # output total, thousands-separated
        assert "42 req" in out
        assert "by subagents" not in out     # none ran

    def test_panel_omits_tokens_when_none(self):
        out = self._text(feed.build_session_panel(self._model(), term_height=30,
                                                  tokens=None))
        assert "Tokens" not in out

    def test_panel_notes_subagent_output_when_present(self):
        tokens = {"requests": 10, "output_tokens": 5000, "input_tokens": 100,
                  "cache_read_tokens": 0, "cache_creation_tokens": 0,
                  "subagent_output_tokens": 1500}
        out = self._text(feed.build_session_panel(self._model(), term_height=30,
                                                  tokens=tokens))
        assert "1,500 by subagents" in out

    def test_panel_empty_subagents_shows_placeholder(self):
        out = self._text(feed.build_session_panel(self._model(subagents=()),
                                                  term_height=30))
        assert "No subagents." in out
        assert "Subagents (0, 0 running)" in out

    def test_panel_capped_count_marked_with_plus(self):
        capped = (feed.SubagentInfo(
            agent_id="big", agent_type="general-purpose", description="big run",
            status="done", command_count=123, command_count_capped=True),)
        out = self._text(feed.build_session_panel(self._model(subagents=capped),
                                                  term_height=30))
        assert "123+" in out

    def test_panel_bracketed_transcript_path_not_swallowed_as_markup(self):
        # a path with a bracketed segment must render literally in both the
        # meta row and the subtitle border (the subtitle is a Text, not markup)
        model = self._model(transcript_path="/proj[bar]/x.jsonl", subagents=())
        out = self._text(feed.build_session_panel(model, term_height=30))
        assert out.count("proj[bar]") == 2  # meta row + subtitle
        assert "/proj/x.jsonl" not in out    # bracket segment not dropped

    def test_panel_folder_is_tilde_abbreviated(self):
        home = str(Path.home())
        model = self._model(cwd=home + "/Projects/x", live=False, status="",
                            version="", started_at=0, subagents=())
        out = self._text(feed.build_session_panel(model, term_height=30))
        assert "~/Projects/x" in out
        assert home + "/Projects/x" not in out  # absolute home prefix hidden

    # A description far longer than the feed's 6-wide Agent column, so the feed
    # would ellipsize it but the session page surfaces it in full.
    _LONG_DESC = "Refactor the agent attribution pipeline across the whole feed"

    def _highlight_model(self):
        subs = (
            feed.SubagentInfo(
                agent_id="aaaaaaaa-1111", agent_type="code-reviewer",
                description=self._LONG_DESC, status="done", command_count=4),
            feed.SubagentInfo(
                agent_id="bbbbbbbb-2222", agent_type="Explore",
                description="other run", status="running", command_count=1),
        )
        return self._model(subagents=subs)

    def _reverse_segments(self, panel, width=120):
        # Segments carrying reverse=True: the ellipsis-safe way to assert a row
        # is emphasized (export_text drops the reverse SGR on a record console).
        console = Console(width=width)
        return [s for s in console.render(panel, console.options)
                if s.style and s.style.reverse]

    def test_highlight_subagent_surfaces_full_name_and_emphasizes_row(self):
        panel = feed.build_session_panel(
            self._highlight_model(), term_height=30,
            highlight_agent_id="aaaaaaaa-1111")
        out = self._text(panel, width=120)
        # complete description surfaced in a Subagent metadata row, untruncated,
        # with the agent type appended in parens (unique to the metadata row)
        assert self._LONG_DESC in out
        assert "(code-reviewer)" in out
        # the matching subagents-table row is reversed
        rev = self._reverse_segments(panel)
        assert any("Refactor" in s.text for s in rev)

    def test_no_highlight_renders_no_subagent_meta_row(self):
        # opened from a main-agent row (highlight None): no Subagent metadata row
        # and no reversed table row - identical to before this phase
        panel = feed.build_session_panel(
            self._highlight_model(), term_height=30, highlight_agent_id=None)
        out = self._text(panel, width=120)
        assert "(code-reviewer)" not in out   # no metadata Subagent row
        assert self._reverse_segments(panel) == []


class TestStoreSubcommandDispatch:
    """`main()`'s gate: a store subcommand is handed to `cli_main` and the process
    exits, so the TUI is never started for it. The gate is also what keeps the
    feed and the hook free of duckdb - `from claude_trail_store import cli_main`
    is lazy and runs on this path only - so a subcommand missing from it would
    render a dashboard instead of answering."""

    GATED = ("ingest", "search", "cat", "diff", "stats", "tokens", "prompts",
             "reset-db", "prune-raw")

    def _route(self, monkeypatch, argv):
        """Run `main()` on `argv` with cli_main stubbed; returns (code, calls)."""
        import pytest
        import claude_trail_store as store
        seen = []
        monkeypatch.setattr(store, "cli_main", lambda a: seen.append(a) or 0)
        # Reaching the TUI is the failure this gate exists to prevent, so make it
        # loud rather than letting a missed subcommand start a dashboard.
        def no_tui(*args, **kwargs):
            raise AssertionError("main() fell through to the TUI")
        monkeypatch.setattr(feed, "Console", no_tui)
        monkeypatch.setattr(sys, "argv", ["claude-trail", *argv])
        with pytest.raises(SystemExit) as exc:
            feed.main()
        return exc.value.code, seen

    def test_prompts_reaches_the_store_cli(self, monkeypatch):
        code, seen = self._route(monkeypatch, ["prompts", "--limit", "3"])
        assert (code, seen) == (0, [["prompts", "--limit", "3"]])

    def test_every_gated_name_routes(self, monkeypatch):
        for cmd in self.GATED:
            assert self._route(monkeypatch, [cmd]) == (0, [[cmd]]), cmd

    def test_every_gated_name_is_one_cli_main_dispatches(self, monkeypatch):
        # The two tables have to agree: a name main() gates but cli_main does not
        # know raises KeyError instead of running. Every cmd_* is stubbed so this
        # asserts the mapping, not the commands.
        import claude_trail_store as store
        seen = []
        for name in [n for n in vars(store) if n.startswith("cmd_")]:
            monkeypatch.setitem(store.cli_main.__globals__, name,
                                lambda argv: seen.append(argv[0]) or 0)
        for cmd in self.GATED:
            assert store.cli_main([cmd]) == 0, cmd
        assert seen == list(self.GATED)


class TestTokenLoaders:
    """The lazy store loaders behind the command/session modals: they guard bad
    input (None: no Tokens row, no prompt panel) and degrade to the
    `_tokens_unavailable` hint sentinel when the store has no answer, never
    raising into the TUI loop."""

    def test_command_detail_none_entry(self):
        assert feed._load_command_detail(None) == (None, None)

    def test_command_detail_missing_session_or_command(self):
        assert feed._load_command_detail({"command": "ls"}) == (None, None)      # no sid
        assert feed._load_command_detail({"session_id": "s"}) == (None, None)    # no cmd

    def test_command_detail_missing_store_hints_run_ingest(self, tmp_path, monkeypatch):
        # No store file: the loader must not raise; both pieces return the
        # sentinel whose hint says the store does not exist yet (the fix for a
        # row silently vanishing and the feature looking broken). The `from
        # claude_trail_store import STORE_PATH` inside reads the patched
        # attribute at call time.
        import claude_trail_store as store
        monkeypatch.setattr(store, "STORE_PATH", tmp_path / "nope.duckdb")
        entry = {"session_id": "s", "command": "ls", "timestamp": "2026-01-01T00:00:00+00:00"}
        hint = {"unavailable": "no audit store yet - run: claude-trail ingest"}
        assert feed._load_command_detail(entry) == (hint, hint)

    def test_command_detail_store_lagging_hints_pending(self, tmp_path, monkeypatch):
        # Store file exists but has no matching request (the store tails
        # transcripts and lags the live feed): the hint says "pending ingest".
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        monkeypatch.setattr(feed, "_with_store", lambda fn: None)
        entry = {"session_id": "s", "command": "ls", "timestamp": "2026-01-01T00:00:00+00:00"}
        hint = {"unavailable": "pending ingest (store lags the live feed)"}
        assert feed._load_command_detail(entry) == (hint, hint)

    def test_command_detail_opens_the_store_once_for_both_rows(self, tmp_path, monkeypatch):
        # One store open serves the Tokens row and the prompt panel; a second
        # open per modal would double the cost of every Enter.
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        opens = []

        def fake_with_store(fn):
            opens.append(fn)
            return ({"output_tokens": 5, "calls_in_request": 1, "share": 5.0,
                     "model": "m"}, {"text": "the ask", "ts": ""})

        monkeypatch.setattr(feed, "_with_store", fake_with_store)
        entry = {"session_id": "s", "command": "ls", "timestamp": "2026-01-01T00:00:00+00:00"}
        tokens, prompt = feed._load_command_detail(entry)
        assert len(opens) == 1
        assert tokens["output_tokens"] == 5
        assert prompt["text"] == "the ask"

    def test_command_detail_passes_the_row_agent_thread(self, tmp_path, monkeypatch):
        # A subagent row must look up ITS thread's prompt, not the parent's.
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        seen = {}

        def fake_prompt_for_command(con, sid, ts=None, agent_id=None):
            seen.update(sid=sid, ts=ts, agent_id=agent_id)
            return {"text": "agent ask", "ts": ""}

        monkeypatch.setattr(store, "prompt_for_command", fake_prompt_for_command)
        monkeypatch.setattr(store, "command_tokens_for", lambda *a, **k: None)
        monkeypatch.setattr(feed, "_with_store", lambda fn: fn(None))
        entry = {"session_id": "s", "command": "ls", "agent_id": "ag1",
                 "timestamp": "2026-01-01T00:00:00+00:00"}
        _, prompt = feed._load_command_detail(entry)
        assert seen == {"sid": "s", "ts": "2026-01-01T00:00:00+00:00", "agent_id": "ag1"}
        assert prompt["text"] == "agent ask"

    def test_session_tokens_none_sid(self):
        assert feed._load_session_tokens(None) is None
        assert feed._load_session_tokens("") is None

    def test_session_tokens_missing_store_hints_run_ingest(self, tmp_path, monkeypatch):
        import claude_trail_store as store
        monkeypatch.setattr(store, "STORE_PATH", tmp_path / "nope.duckdb")
        assert feed._load_session_tokens("some-sid") == {
            "unavailable": "no audit store yet - run: claude-trail ingest"}

    def test_unavailable_sentinel_renders_as_dim_hint(self):
        # Both formatters render the sentinel as the hint text (not a number),
        # and the detail panel shows the Tokens row carrying it.
        hint = {"unavailable": "pending ingest (store lags the live feed)"}
        assert feed._format_command_tokens(hint).plain == hint["unavailable"]
        assert feed._format_session_tokens(hint).plain == hint["unavailable"]
        entry = {"session_id": "s", "command": "ls",
                 "timestamp": "2026-01-01T00:00:00+00:00", "cwd": "/tmp"}
        panel = feed.build_detail_panel(entry, term_height=30, tokens=hint)
        console = Console(width=120)
        with console.capture() as cap:
            console.print(panel)
        assert "Tokens" in cap.get()
        assert "pending ingest" in cap.get()

    def test_hint_wordings_have_one_definition(self):
        # The modals' sentinel and the prompts tab's status read from the same
        # constants, so the two views can never describe one store differently.
        assert feed.NO_STORE_HINT == "no audit store yet - run: claude-trail ingest"
        assert feed.PENDING_INGEST_HINT == "pending ingest (store lags the live feed)"
        assert feed.STORE_BUSY_HINT == "store busy: an ingest is writing"
        assert feed.STORE_BUSY_KEPT_HINT.startswith(feed.STORE_BUSY_HINT)

    def test_with_store_reports_the_error_it_swallows(self, tmp_path, monkeypatch):
        # `on_error` must not widen the catch: the return value is still None, the
        # exception is just no longer lost. That is what lets the prompts loader
        # tell a lock collision from an empty answer.
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        monkeypatch.setattr(store, "connect", lambda **kw: (_ for _ in ()).throw(
            OSError("Could not set lock on file")))
        seen = []
        assert feed._with_store(lambda con: "unreachable", on_error=seen.append) is None
        assert len(seen) == 1 and "Could not set lock" in str(seen[0])
        # And without the hook the behaviour is byte-for-byte what it was.
        assert feed._with_store(lambda con: "unreachable") is None

    def test_a_raising_on_error_hook_cannot_escape(self, tmp_path, monkeypatch):
        # The contract is "never raise at the caller" - a diagnostic callback must
        # not be the one thing that breaks it, or a second caller's buggy hook
        # takes the TUI down through a path that exists to swallow failures.
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        monkeypatch.setattr(store, "connect", lambda **kw: (_ for _ in ()).throw(
            OSError("boom")))

        def raising(exc):
            raise ValueError("the hook itself is broken")

        assert feed._with_store(lambda con: "unreachable", on_error=raising) is None


class TestPromptTurnLoader:
    """`_load_prompt_turns` / `_refresh_prompts`: the three store states the
    Prompts tab must tell apart, driven through the loader boundary so no test
    needs a real duckdb file."""

    def _store_at(self, tmp_path, monkeypatch, exists=True):
        import claude_trail_store as store
        db = tmp_path / "trail.duckdb"
        if exists:
            db.touch()
        monkeypatch.setattr(store, "STORE_PATH", db)
        return store

    ROW = {"session_id": "s1", "ts": "2026-07-29 10:00:00-03", "next_ts": None,
           "event_id": "ev-1", "preview": "do the thing", "full_len": 12,
           "tools": 2, "bash": 1, "agents": 0, "subcalls": 0,
           "output_tokens": 500, "subagent_output_tokens": 0, "requests": 1,
           "last_ts": None, "project": "/p", "cwd": None}

    def test_missing_store_returns_no_rows_and_the_ingest_hint(self, tmp_path, monkeypatch):
        self._store_at(tmp_path, monkeypatch, exists=False)
        assert feed._load_prompt_turns() == ([], feed.NO_STORE_HINT)

    def test_a_successful_read_maps_rows_and_clears_the_status(self, tmp_path, monkeypatch):
        store = self._store_at(tmp_path, monkeypatch)
        monkeypatch.setattr(store, "prompt_turns", lambda con, **kw: [self.ROW])
        monkeypatch.setattr(feed, "_with_store",
                            lambda fn, on_error=None: fn(None))
        rows, status = feed._load_prompt_turns()
        assert status is None
        assert [r.preview for r in rows] == ["do the thing"]
        assert isinstance(rows[0], feed.PromptTurn)

    def test_an_empty_but_successful_read_is_not_an_error(self, tmp_path, monkeypatch):
        # A current store that holds no turn for this scope: no rows AND no status,
        # which is what lets the panel show "no prompts" instead of blaming ingest.
        store = self._store_at(tmp_path, monkeypatch)
        monkeypatch.setattr(store, "prompt_turns", lambda con, **kw: [])
        monkeypatch.setattr(feed, "_with_store", lambda fn, on_error=None: fn(None))
        assert feed._load_prompt_turns() == ([], None)

    def test_the_session_filter_reaches_the_query(self, tmp_path, monkeypatch):
        store = self._store_at(tmp_path, monkeypatch)
        seen = {}

        def fake_prompt_turns(con, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(store, "prompt_turns", fake_prompt_turns)
        monkeypatch.setattr(feed, "_with_store", lambda fn, on_error=None: fn(None))
        feed._load_prompt_turns("sid-9")
        assert seen == {"session_id": "sid-9"}
        seen.clear()
        feed._load_prompt_turns(None)
        assert seen == {}                       # unscoped: the query's own default

    def test_a_locked_store_returns_none_rows_and_says_busy(self, tmp_path, monkeypatch):
        # None means "no answer, keep what is on screen": DuckDB's lock excludes in
        # both directions, so colliding with the running ingest is routine.
        self._store_at(tmp_path, monkeypatch)

        def busy(fn, on_error=None):
            if on_error is not None:
                on_error(OSError("IO Error: Could not set lock on file"))
            return None

        monkeypatch.setattr(feed, "_with_store", busy)
        assert feed._load_prompt_turns() == (None, feed.STORE_BUSY_HINT)

    def test_a_non_lock_failure_is_reported_not_disguised_as_empty(self, tmp_path, monkeypatch):
        self._store_at(tmp_path, monkeypatch)

        def broken(fn, on_error=None):
            if on_error is not None:
                on_error(RuntimeError("schema is from the future"))
            return None

        monkeypatch.setattr(feed, "_with_store", broken)
        rows, status = feed._load_prompt_turns()
        assert rows == []
        assert "store unreadable" in status and "future" in status

    def test_refresh_stores_the_snapshot_and_stamps_both_clocks(self, monkeypatch):
        turn = feed.PromptTurn.from_row(self.ROW)
        monkeypatch.setattr(feed, "_load_prompt_turns", lambda sid=None: ([turn], None))
        prompts = feed.PromptsState()
        feed._refresh_prompts(prompts, 10)
        assert prompts.rows == [turn]
        assert prompts.status is None
        assert prompts.loaded_at is not None
        assert prompts.checked_at == prompts.loaded_at

    def test_refresh_reanchors_the_cursor_on_a_shorter_snapshot(self, monkeypatch):
        prompts = feed.PromptsState(rows=[_turn(sid=f"s{i}") for i in range(9)],
                                    cursor=8, scroll_offset=5)
        monkeypatch.setattr(feed, "_load_prompt_turns",
                            lambda sid=None: ([_turn(sid="only")], None))
        feed._refresh_prompts(prompts, 5)
        assert (prompts.cursor, prompts.scroll_offset) == (0, 0)
        assert prompts.selected().session_id == "only"

    def test_refresh_keeps_the_last_snapshot_when_the_store_is_busy(self, monkeypatch):
        kept = [_turn(sid="kept-1"), _turn(sid="kept-2")]
        prompts = feed.PromptsState(rows=kept, cursor=1, loaded_at=100.0)
        monkeypatch.setattr(feed, "_load_prompt_turns",
                            lambda sid=None: (None, feed.STORE_BUSY_HINT))
        feed._refresh_prompts(prompts, 10)
        assert prompts.rows == kept              # nothing was blanked
        assert prompts.cursor == 1               # nor re-anchored
        assert prompts.loaded_at == 100.0        # the age keeps counting up
        assert prompts.status == feed.STORE_BUSY_KEPT_HINT
        assert prompts.checked_at is not None    # but the attempt was recorded

    def test_refresh_on_a_busy_store_with_nothing_on_screen_just_says_busy(self, monkeypatch):
        # No previous snapshot to keep, so the status must not claim one was kept.
        prompts = feed.PromptsState()
        monkeypatch.setattr(feed, "_load_prompt_turns",
                            lambda sid=None: (None, feed.STORE_BUSY_HINT))
        feed._refresh_prompts(prompts, 10)
        assert prompts.status == feed.STORE_BUSY_HINT
        assert prompts.loaded_at is None
        assert prompts.rows == []

    def test_refresh_does_not_date_a_read_that_answered_nothing(self, monkeypatch):
        # `loaded_at` dates the last ANSWER. Stamping it on a failure made the
        # header claim `as of 22:30:04 (0s ago) · 0 turns` over a body explaining
        # that the store could not be read - two false statements at once.
        prompts = feed.PromptsState()
        monkeypatch.setattr(feed, "_load_prompt_turns",
                            lambda sid=None: ([], "store unreadable: boom"))
        feed._refresh_prompts(prompts, 10)
        assert prompts.loaded_at is None            # never answered
        assert prompts.checked_at is not None       # but the attempt is recorded
        assert prompts.status == "store unreadable: boom"

    def test_refresh_keeps_a_good_snapshot_through_a_non_lock_failure(self, monkeypatch):
        # The store vanishing between the `exists()` check and `connect` is a
        # transient race; blanking the list for it (where a lock collision keeps
        # it) threw away a perfectly readable snapshot.
        kept = [_turn(sid="kept-1", tools=1), _turn(sid="kept-2", tools=1)]
        prompts = feed.PromptsState(rows=kept, cursor=1, loaded_at=100.0)
        monkeypatch.setattr(feed, "_load_prompt_turns",
                            lambda sid=None: ([], feed.NO_STORE_HINT))
        feed._refresh_prompts(prompts, 10)
        assert prompts.rows == kept                 # still on screen
        assert prompts.cursor == 1
        assert prompts.loaded_at == 100.0           # and still dated by its read
        assert prompts.status == feed.NO_STORE_HINT

    def test_refresh_replaces_the_snapshot_on_a_successful_empty_read(self, monkeypatch):
        # The other side of the previous test: an empty answer is an ANSWER, so it
        # does replace the rows (a session whose turns aged out must go empty).
        prompts = feed.PromptsState(rows=[_turn(sid="old", tools=1)], loaded_at=100.0)
        monkeypatch.setattr(feed, "_load_prompt_turns", lambda sid=None: ([], None))
        feed._refresh_prompts(prompts, 10)
        assert prompts.rows == []
        assert prompts.status is None
        assert prompts.loaded_at != 100.0

    def test_refresh_passes_the_active_session_filter(self, monkeypatch):
        seen = []
        monkeypatch.setattr(feed, "_load_prompt_turns",
                            lambda sid=None: (seen.append(sid), ([], None))[1])
        prompts = feed.PromptsState(session_filter="sid-7")
        feed._refresh_prompts(prompts, 10)
        assert seen == ["sid-7"]

    def test_refresh_due_gates_on_the_last_attempt(self):
        prompts = feed.PromptsState()
        assert feed._prompts_refresh_due(prompts) is True      # never tried
        prompts.checked_at = 1000.0
        assert feed._prompts_refresh_due(prompts, now=1000.0) is False
        assert feed._prompts_refresh_due(
            prompts, now=1000.0 + feed.PROMPTS_AUTO_REFRESH - 1) is False
        assert feed._prompts_refresh_due(
            prompts, now=1000.0 + feed.PROMPTS_AUTO_REFRESH) is True

    def test_refresh_due_backs_off_after_a_busy_collision(self, monkeypatch):
        # A locked store leaves `loaded_at` alone, so gating on it would retry on
        # every 300ms poll for as long as the ingest holds the file.
        prompts = feed.PromptsState(rows=[_turn()])
        monkeypatch.setattr(feed, "_load_prompt_turns",
                            lambda sid=None: (None, feed.STORE_BUSY_HINT))
        monkeypatch.setattr(feed.time, "monotonic", lambda: 2000.0)
        feed._refresh_prompts(prompts, 10)
        assert prompts.loaded_at is None
        assert feed._prompts_refresh_due(prompts, now=2001.0) is False

    def test_the_auto_refresh_matches_the_ingest_cadence(self):
        # A faster tick would spend a query per poll to find nothing new.
        assert feed.PROMPTS_AUTO_REFRESH == 30.0

    def test_snapshot_age_and_its_wording(self):
        assert feed._snapshot_age(None) is None
        assert feed._snapshot_age(100.0, now=112.0) == 12.0
        assert feed._snapshot_age(100.0, now=99.0) == 0.0     # never negative
        assert feed._format_age(0) == "0s ago"
        assert feed._format_age(12.7) == "12s ago"
        assert feed._format_age(60) == "1m ago"
        assert feed._format_age(3600 + 7 * 60) == "1h 07m ago"


class TestDrainCoalescing:
    """`_drain_action` / `_frame_store_reads`: which actions main()'s key drain
    performs inline, and which are flagged and performed ONCE per frame.

    The drain reads every buffered keystroke before rendering, so a held key is
    one render rather than one per press. The two actions that open the store
    have to be coalesced the same way or a key-repeat burst freezes the loop for
    a query per press; this is the split, out of main() so it can be driven.
    """

    # By NAME, not by member: other tests in this file reload the module, and a
    # captured enum member would then be a different object than the one
    # `_drain_action` compares against.
    COALESCED = {"LOAD_PROMPTS": "reload_prompts", "OPEN_PROMPT": "reload_turn"}

    def test_the_two_store_reads_are_flagged_not_performed(self):
        for name, attr in self.COALESCED.items():
            flags = feed.DrainFlags()
            assert feed._drain_action(getattr(feed.Action, name), flags) is True, name
            assert getattr(flags, attr) is True, name

    def test_every_other_action_is_performed_by_the_drain_itself(self):
        # An editor launch or a search is what the user asked for on THAT press,
        # and `d`/`s` open a modal that swallows the repeat, so they are already
        # self-limiting. Only the two above need the frame to hold them.
        for action in feed.Action:
            if action.name in self.COALESCED:
                continue
            flags = feed.DrainFlags()
            assert feed._drain_action(action, flags) is False, action
            assert flags == feed.DrainFlags(), action

    def _state(self, tab="prompts", **kw):
        state = feed.AppState(entries=[], **kw)
        state.tab = tab
        return state

    def test_a_held_enter_costs_one_store_read(self, monkeypatch):
        """Ten buffered `↵` end in ONE `_load_turn_detail`.

        `↵` is not self-limiting: a turn whose prompt text has not loaded gives
        the key back its close-the-page meaning, so a repeat alternates
        open/close and would ask for a fresh read every other press (measured
        44ms each on a 1.2 GiB store).
        """
        calls = []
        monkeypatch.setattr(feed, "_load_turn_detail",
                            lambda turn: calls.append(turn) or ([_action()], "the ask"))
        state = self._state()
        state.prompts.detail = _turn(tools=1)
        flags = feed.DrainFlags()
        for _ in range(10):
            feed._drain_action(feed.Action.OPEN_PROMPT, flags)
        feed._frame_store_reads(state, flags, 10)
        assert len(calls) == 1
        assert calls[0] is state.prompts.detail          # the turn the drain ended on
        assert state.prompts.detail_text == "the ask"    # and it lands on the page

    def test_a_frame_that_flagged_nothing_reads_nothing(self, monkeypatch):
        loads, refreshes = [], []
        monkeypatch.setattr(feed, "_load_turn_detail", lambda turn: loads.append(turn))
        monkeypatch.setattr(feed, "_refresh_prompts",
                            lambda prompts, rows: refreshes.append(rows))
        state = self._state(tab="commands")
        feed._frame_store_reads(state, feed.DrainFlags(), 10)
        assert (loads, refreshes) == ([], [])

    def test_the_list_is_only_read_while_its_tab_is_on_screen(self, monkeypatch):
        # A drain that entered the tab and left it again pays nothing.
        refreshes = []
        monkeypatch.setattr(feed, "_refresh_prompts",
                            lambda prompts, rows: refreshes.append(rows))
        state = self._state(tab="commands")
        feed._frame_store_reads(state, feed.DrainFlags(reload_prompts=True), 10)
        assert refreshes == []
        feed._frame_store_reads(self._state(), feed.DrainFlags(reload_prompts=True), 7)
        assert refreshes == [7]

    def test_an_explicit_request_survives_a_modal_the_idle_refresh_does_not(
            self, monkeypatch):
        # `r` then `s` in one drain: the read stands (unseen until the modal
        # closes). A modal alone must never make the tab poll the store.
        refreshes = []
        monkeypatch.setattr(feed, "_refresh_prompts",
                            lambda prompts, rows: refreshes.append(rows))
        monkeypatch.setattr(feed, "_prompts_refresh_due", lambda prompts: True)
        state = self._state(session_detail="sid-1")
        assert state.modal_open() is True
        feed._frame_store_reads(state, feed.DrainFlags(), 10)
        assert refreshes == []                                   # idle: gated
        feed._frame_store_reads(state, feed.DrainFlags(reload_prompts=True), 10)
        assert refreshes == [10]                                 # explicit: stands

    def test_an_idle_frame_refreshes_when_the_snapshot_is_due(self, monkeypatch):
        refreshes = []
        monkeypatch.setattr(feed, "_refresh_prompts",
                            lambda prompts, rows: refreshes.append(rows))
        state = self._state()
        monkeypatch.setattr(feed, "_prompts_refresh_due", lambda prompts: False)
        feed._frame_store_reads(state, feed.DrainFlags(), 10)
        assert refreshes == []
        monkeypatch.setattr(feed, "_prompts_refresh_due", lambda prompts: True)
        feed._frame_store_reads(state, feed.DrainFlags(), 10)
        assert refreshes == [10]


class TestPromptsPanelStates:
    """What the panel says for each store state: it must never look silently
    broken, and never give advice that is wrong for the state it is in."""

    def _text(self, prompts, width=160, height=24, now=1000.0):
        console = Console(width=width, record=True)
        console.print(feed.build_prompts_panel(prompts, height, now=now))
        return console.export_text()

    def test_never_read_says_so_and_how_to_read_it(self):
        out = self._text(feed.PromptsState())
        assert "not read yet" in out          # the header's age slot
        assert "No prompts read yet" in out
        assert "Press r" in out
        assert "claude-trail ingest" in out
        assert "phase" not in out.lower()     # no unreleased-work references

    def test_a_missing_store_shows_the_same_hint_as_the_modals(self):
        prompts = feed.PromptsState(status=feed.NO_STORE_HINT,
                                    loaded_at=1000.0, checked_at=1000.0)
        out = self._text(prompts)
        assert feed.NO_STORE_HINT in out

    def test_a_failed_read_says_so_instead_of_blaming_an_empty_store(self):
        # The panel used to make two false statements at once here: a header
        # dating a read that never happened, and a body reading "No prompts in the
        # audit store. Fill it with: claude-trail ingest" - wrong advice for a
        # store that IS there and cannot be read.
        prompts = feed.PromptsState(status="store unreadable: schema is from the future",
                                    checked_at=1000.0)
        out = self._text(prompts)
        assert "could not be read" in out
        assert "schema is from the future" in out
        assert "Press r" in out
        assert "No prompts in the audit store" not in out
        assert "as of" not in out                  # nothing was ever answered
        assert "not read yet" in out

    def test_a_kept_snapshot_filtered_empty_still_advises_the_x_key(self):
        # A busy store with a snapshot the `x` filter hides: the store-failure
        # branch must not steal this case, or the way back is unexplained.
        prompts = feed.PromptsState(rows=[_turn(sid="quiet", tools=0)],
                                    hide_empty=True,
                                    status=feed.STORE_BUSY_KEPT_HINT,
                                    loaded_at=940.0, checked_at=1000.0)
        out = self._text(prompts)
        assert "press x" in out
        assert "could not be read" not in out

    def test_a_busy_store_says_busy_over_the_snapshot_it_kept(self):
        prompts = feed.PromptsState(
            rows=[_turn(sid="kept-1", tools=1, preview="the kept turn")],
            status=feed.STORE_BUSY_KEPT_HINT, loaded_at=940.0, checked_at=1000.0)
        out = self._text(prompts)
        assert "store busy" in out
        assert "kept the last snapshot" in out
        assert "the kept turn" in out          # the old snapshot is still readable
        assert "(1m ago)" in out               # and dated, so the staleness shows

    def test_an_empty_successful_read_does_not_blame_the_store(self):
        prompts = feed.PromptsState(loaded_at=1000.0, checked_at=1000.0)
        out = self._text(prompts)
        assert "No prompts in the audit store" in out
        assert "as of" in out                  # it WAS read
        assert "not read yet" not in out

    def test_an_empty_session_scoped_read_points_at_the_filter(self):
        prompts = feed.PromptsState(session_filter="sid-abcdef12",
                                    loaded_at=1000.0, checked_at=1000.0)
        out = self._text(prompts)
        assert "No prompts for this session" in out
        assert "Press S" in out
        assert "session sid-abcd" in out       # the header names the scope
        assert "claude-trail ingest" not in out

    def test_the_scoped_header_names_the_session_the_way_the_column_does(self):
        # The header showed a raw id prefix while the Session column showed the
        # resolved name, which read as two different sessions.
        prompts = feed.PromptsState(rows=[_turn(sid="sid-abcdef12", tools=1)],
                                    session_filter="sid-abcdef12",
                                    loaded_at=1000.0, checked_at=1000.0)
        console = Console(width=160, record=True)
        console.print(feed.build_prompts_panel(
            prompts, 24, name_map={"sid-abcdef12": "my-session"}, now=1000.0))
        out = console.export_text()
        assert "session my-session" in out
        assert "sid-abcd" not in out            # not the raw prefix, in either place
        assert feed._prompts_header(prompts, 1000.0).plain.strip().endswith(
            "session sid-abcd")                 # and it still falls back without one

    def test_the_age_line_reports_the_snapshot_clock(self):
        prompts = feed.PromptsState(rows=[_turn(tools=1)],
                                    loaded_at=1000.0, checked_at=1000.0)
        out = self._text(prompts, now=1012.0)
        assert "(12s ago)" in out
        assert re.search(r"as of \d\d:\d\d:\d\d", out)


class TestSearchRoots:
    """search_roots resolves the transcript folder + cwd roots and skips paths
    that do not exist (a root with no path is omitted so the panel disables it)."""

    def test_resolves_transcript_file_dir_and_cwd(self, tmp_path):
        sid = "sid-search"
        _make_session(tmp_path, sid, agents={"a1": ("gp", "run", 1)})
        cwd_dir = tmp_path / "work"
        cwd_dir.mkdir()
        entry = {"session_id": sid, "cwd": str(cwd_dir)}
        with patch.object(feed, "PROJECTS_DIR", tmp_path):
            roots = feed.search_roots(sid, entry, None)
        tpaths = [str(p) for p in roots["transcript"]]
        assert any(p.endswith(f"{sid}.jsonl") for p in tpaths)  # transcript file
        assert any(p.endswith(sid) for p in tpaths)             # sibling <sid>/ dir
        assert len(roots["transcript"]) == 2
        assert roots["cwd"] == [cwd_dir]

    def test_skips_missing_cwd(self, tmp_path):
        sid = "sid-nocwd"
        _make_session(tmp_path, sid, agents={})
        entry = {"session_id": sid, "cwd": str(tmp_path / "does-not-exist")}
        with patch.object(feed, "PROJECTS_DIR", tmp_path):
            roots = feed.search_roots(sid, entry, None)
        assert "cwd" not in roots       # missing dir dropped
        assert "transcript" in roots

    def test_absent_transcript_yields_only_cwd(self, tmp_path):
        cwd_dir = tmp_path / "w"
        cwd_dir.mkdir()
        entry = {"session_id": "ghost", "cwd": str(cwd_dir)}
        with patch.object(feed, "PROJECTS_DIR", tmp_path):
            roots = feed.search_roots("ghost", entry, None)
        assert roots == {"cwd": [cwd_dir]}  # no transcript on disk -> only cwd

    def test_live_model_cwd_preferred_over_entry(self, tmp_path):
        # a live session's launch dir (model.cwd) wins over the row's cwd
        live_dir = tmp_path / "launch"
        live_dir.mkdir()
        model = feed.SessionModel(
            session_id="s", name="", status="busy", live=True,
            cwd=str(live_dir), version="", kind="", started_at=0,
            transcript_path="", subagents=())
        entry = {"session_id": "s", "cwd": str(tmp_path)}
        with patch.object(feed, "PROJECTS_DIR", tmp_path):
            roots = feed.search_roots("s", entry, model)
        assert roots["cwd"] == [live_dir]


class TestRunSearch:
    """run_search: rg/grep runner. shutil.which is forced to None so these run
    against the always-present grep and exercise the parse/limit/error paths."""

    def _tree(self, tmp_path):
        (tmp_path / "a.txt").write_text(
            "alpha\nbeta hello\ngamma\n", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("hello there\nhello world\n", encoding="utf-8")
        return tmp_path

    def test_parses_matches_with_grep(self, tmp_path):
        root = self._tree(tmp_path)
        with patch.object(feed.shutil, "which", return_value=None):  # force grep
            matches, capped, err = feed.run_search("hello", [root])
        assert err is None and capped is False
        assert {m[2] for m in matches} >= {"beta hello", "hello there", "hello world"}
        assert all(isinstance(m[1], int) for m in matches)  # line numbers parsed
        assert len(matches) == 3

    def test_respects_limit(self, tmp_path):
        root = self._tree(tmp_path)
        with patch.object(feed.shutil, "which", return_value=None):
            matches, capped, err = feed.run_search("hello", [root], limit=2)
        assert len(matches) == 2
        assert capped is True
        assert err is None

    def test_no_match_returns_empty(self, tmp_path):
        root = self._tree(tmp_path)
        with patch.object(feed.shutil, "which", return_value=None):
            matches, capped, err = feed.run_search("zzz-no-such", [root])
        assert matches == [] and capped is False and err is None

    def test_bad_regex_returns_error(self, tmp_path):
        root = self._tree(tmp_path)
        with patch.object(feed.shutil, "which", return_value=None):
            matches, capped, err = feed.run_search("[", [root])
        assert matches == [] and capped is False
        assert err is not None  # exit >= 2 surfaces a message, never raises

    def test_empty_query_or_paths_is_noop(self, tmp_path):
        with patch.object(feed.shutil, "which", return_value=None):
            assert feed.run_search("", [tmp_path]) == ([], False, None)
            assert feed.run_search("   ", [tmp_path]) == ([], False, None)
            assert feed.run_search("hello", []) == ([], False, None)

    def test_parse_grep_line_helper(self):
        assert feed._parse_grep_line("/p/a.py:42:def main():") == (
            "/p/a.py", 42, "def main():")
        assert feed._parse_grep_line("no colons here") is None
        assert feed._parse_grep_line("/p:notanumber:x") is None

    def test_single_file_target_with_grep(self, tmp_path):
        # A transcript root with no subagents dir is one explicit file; -H must
        # keep the filename so _parse_grep_line yields a real path, not a lineno.
        f = tmp_path / "a.txt"
        f.write_text("alpha\nbeta hello\ngamma\n", encoding="utf-8")
        with patch.object(feed.shutil, "which", return_value=None):  # force grep
            matches, capped, err = feed.run_search("hello", [f])
        assert err is None and capped is False
        assert len(matches) == 1
        path, line, text = matches[0]
        assert path == str(f)  # filename retained, not the line number "2"
        assert line == 2
        assert "hello" in text

    def test_single_file_target_with_rg(self, tmp_path):
        # Same single-file case but exercising the real rg branch (the one the
        # bug lived in); skipped when rg is not installed.
        if not feed.shutil.which("rg"):
            import pytest
            pytest.skip("rg not installed")
        f = tmp_path / "a.txt"
        f.write_text("alpha\nbeta hello\ngamma\n", encoding="utf-8")
        matches, capped, err = feed.run_search("hello", [f])
        assert err is None and capped is False
        assert len(matches) == 1
        path, line, text = matches[0]
        assert path == str(f)  # -H keeps the filename even for one file arg
        assert line == 2
        assert "hello" in text


class TestSearchApplyKey:
    """apply_key handling for the search modal: INPUT builds the query, RESULTS
    browses hits, and the feed `/` opener builds a SearchState."""

    def _searching(self, root="transcript", mode="INPUT"):
        state = feed.AppState(entries=[{"command": "c", "session_id": "s1"}])
        state.search = feed.SearchState(
            sid="s1",
            roots={"transcript": [Path("/t.jsonl")], "cwd": [Path("/w")]},
            root=root, mode=mode)
        return state, state.search

    def test_other_root_flips(self):
        assert feed._other_root("transcript") == "cwd"
        assert feed._other_root("cwd") == "transcript"

    # ---- INPUT sub-mode ----
    def test_input_typing_builds_query(self):
        state, s = self._searching()
        for c in "pytest":
            assert feed.apply_key(state, c, 10) is feed.Action.NONE
        assert s.query == "pytest"

    def test_input_backspace_trims(self):
        state, s = self._searching()
        for c in "abc":
            feed.apply_key(state, c, 10)
        feed.apply_key(state, "\x7f", 10)
        assert s.query == "ab"

    def test_input_tab_flips_root(self):
        state, s = self._searching()
        assert feed.apply_key(state, "\t", 10) is feed.Action.NONE
        assert s.root == "cwd"
        feed.apply_key(state, "\t", 10)
        assert s.root == "transcript"

    def test_input_enter_returns_run_search(self):
        state, s = self._searching()
        s.query = "x"
        assert feed.apply_key(state, "\r", 10) is feed.Action.RUN_SEARCH
        assert s.mode == "INPUT"  # apply_key is pure; main() flips to RESULTS

    def test_input_enter_blank_query_is_noop(self):
        # Enter on an empty/whitespace query must not flip to RESULTS (which
        # would render a misleading "no matches" for a search that never ran).
        state, s = self._searching()
        s.query = "   "
        assert feed.apply_key(state, "\r", 10) is feed.Action.NONE
        assert s.mode == "INPUT"
        assert state.search is not None

    def _one_root(self, root="transcript", mode="INPUT"):
        # A SearchState whose "other" root is absent (e.g. ended session with a
        # deleted cwd), so Tab has nowhere valid to switch to.
        state = feed.AppState(entries=[{"command": "c", "session_id": "s1"}])
        roots = {root: [Path("/only")]}
        state.search = feed.SearchState(sid="s1", roots=roots, root=root, mode=mode)
        return state, state.search

    def test_input_tab_to_unavailable_root_is_noop_with_flash(self):
        state, s = self._one_root(root="transcript", mode="INPUT")
        assert feed.apply_key(state, "\t", 10) is feed.Action.NONE
        assert s.root == "transcript"  # did not flip to the absent cwd root
        assert s.flash and "cwd" in s.flash

    def test_results_tab_to_unavailable_root_does_not_rerun(self):
        state, s = self._one_root(root="cwd", mode="RESULTS")
        # No RUN_SEARCH over an empty root (which would show a bogus "no matches").
        assert feed.apply_key(state, "\t", 10) is feed.Action.NONE
        assert s.root == "cwd"
        assert s.flash and "transcript" in s.flash

    def test_input_esc_closes(self):
        state, s = self._searching()
        assert feed.apply_key(state, "\x1b", 10) is feed.Action.NONE
        assert state.search is None

    def test_input_ctrl_c_quits(self):
        state, _ = self._searching()
        assert feed.apply_key(state, "\x03", 10) is feed.Action.QUIT

    def test_search_open_swallows_s_and_enter_openers(self):
        # search takes precedence: `s` types into the query, no session modal opens
        state, s = self._searching()
        feed.apply_key(state, "s", 10)
        assert s.query == "s"
        assert state.session_detail is None
        assert state.detail_entry is None

    # ---- RESULTS sub-mode ----
    def test_results_jk_move_cursor(self):
        state, s = self._searching(mode="RESULTS")
        s.results = [("/f", 1, "a"), ("/f", 2, "b"), ("/f", 3, "c")]
        assert feed.apply_key(state, "j", 10) is feed.Action.NONE
        assert s.cursor == 1
        feed.apply_key(state, "j", 10)
        feed.apply_key(state, "j", 10)
        assert s.cursor == 2  # clamped at the last hit
        feed.apply_key(state, "k", 10)
        assert s.cursor == 1

    def test_results_letters_do_not_type(self):
        state, s = self._searching(mode="RESULTS")
        s.query, s.results = "keep", [("/f", 1, "a")]
        feed.apply_key(state, "z", 10)
        assert s.query == "keep"  # query frozen while browsing

    def test_results_enter_opens_match(self):
        state, s = self._searching(mode="RESULTS")
        s.results = [("/f", 1, "a")]
        assert feed.apply_key(state, "\r", 10) is feed.Action.OPEN_MATCH

    def test_results_enter_without_results_is_noop(self):
        state, _ = self._searching(mode="RESULTS")
        assert feed.apply_key(state, "\r", 10) is feed.Action.NONE

    def test_results_f_opens_match_folder(self):
        state, s = self._searching(mode="RESULTS")
        s.results = [("/f/x.py", 1, "a")]
        assert feed.apply_key(state, "f", 10) is feed.Action.OPEN_MATCH_FOLDER

    def test_results_tab_flips_root_and_reruns(self):
        state, s = self._searching(mode="RESULTS")
        assert feed.apply_key(state, "\t", 10) is feed.Action.RUN_SEARCH
        assert s.root == "cwd"

    def test_results_slash_returns_to_input_keeping_query(self):
        state, s = self._searching(mode="RESULTS")
        s.query = "keep"
        assert feed.apply_key(state, "/", 10) is feed.Action.NONE
        assert s.mode == "INPUT"
        assert s.query == "keep"

    def test_results_esc_closes(self):
        state, _ = self._searching(mode="RESULTS")
        assert feed.apply_key(state, "\x1b", 10) is feed.Action.NONE
        assert state.search is None

    def test_results_q_closes(self):
        state, _ = self._searching(mode="RESULTS")
        assert feed.apply_key(state, "q", 10) is feed.Action.NONE
        assert state.search is None

    def test_any_keypress_clears_flash(self):
        state, s = self._searching(mode="RESULTS")
        s.results, s.flash = [("/f", 1, "a")], "opened folder: /f"
        feed.apply_key(state, "j", 10)  # any move clears the transient flash
        assert s.flash is None

    # ---- feed `/` opener ----
    def test_feed_slash_opens_search_for_selected_session(self, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        state = feed.AppState(entries=[
            {"command": "c0", "session_id": "sid-a", "cwd": "/x"},
            {"command": "c1", "session_id": "sid-b", "cwd": "/y"},
        ], cursor=0)
        with patch.object(feed, "PROJECTS_DIR", tmp_path), \
             patch.object(feed, "SESSIONS_DIR", sessions), \
             patch.object(feed, "SESSION_MODEL_CACHE_TTL", 0.0):
            result = feed.apply_key(state, "/", 10)
        assert result is feed.Action.NONE
        assert state.search is not None
        assert state.search.sid == "sid-b"  # cursor 0 = newest = last appended
        assert state.search.mode == "INPUT"

    def test_feed_slash_on_empty_feed_is_noop(self):
        state = feed.AppState(entries=[])
        assert feed.apply_key(state, "/", 10) is feed.Action.NONE
        assert state.search is None


class TestSearchPanel:
    """build_search_panel rendering for both sub-modes."""

    def _text(self, panel, width=100):
        console = Console(width=width, record=True)
        console.print(panel)
        return console.export_text()

    def test_input_mode_shows_query_and_root_chips(self):
        s = feed.SearchState(
            sid="abcdef123456",
            roots={"transcript": [Path("/t.jsonl")], "cwd": [Path("/w")]},
            query="pytest")
        out = self._text(feed.build_search_panel(s, term_height=20))
        assert "/ pytest" in out
        assert "transcript" in out and "cwd" in out
        assert "abcdef12" in out  # sid[:8] in the subtitle

    def test_results_mode_lists_matches(self):
        s = feed.SearchState(
            sid="s1", roots={"cwd": [Path("/proj")]}, root="cwd",
            query="def", mode="RESULTS",
            results=[("/proj/app.py", 42, "def main():")])
        out = self._text(feed.build_search_panel(s, term_height=20))
        assert "1 match" in out
        assert "app.py" in out and "42" in out and "def main():" in out

    def test_capped_marker_shown(self):
        s = feed.SearchState(
            sid="s1", roots={"cwd": [Path("/p")]}, root="cwd", mode="RESULTS",
            results=[("/p/a", 1, "x")], capped=True)
        assert f"capped {feed.SEARCH_RESULT_LIMIT}" in self._text(
            feed.build_search_panel(s, 20))

    def test_empty_results_shows_no_matches(self):
        s = feed.SearchState(
            sid="s1", roots={"cwd": [Path("/p")]}, root="cwd", mode="RESULTS",
            results=[])
        assert "no matches" in self._text(feed.build_search_panel(s, 20))

    def test_error_is_shown(self):
        s = feed.SearchState(
            sid="s1", roots={"cwd": [Path("/p")]}, root="cwd", mode="RESULTS",
            error="grep: bad regex")
        assert "grep: bad regex" in self._text(feed.build_search_panel(s, 20))

    def test_flash_line_rendered(self):
        s = feed.SearchState(
            sid="s1", roots={"cwd": [Path("/p")]}, root="cwd", mode="RESULTS",
            results=[("/p/a", 1, "x")], flash="opened folder: /p")
        assert "opened folder: /p" in self._text(feed.build_search_panel(s, 20))

    def test_result_window_helper(self):
        # Fits: whole list.
        assert feed._result_window(0, 5, 10) == (0, 5)
        assert feed._result_window(3, 5, 5) == (0, 5)
        # Longer than capacity: cursor stays inside a capacity-sized window.
        start, end = feed._result_window(90, 100, 10)
        assert end - start == 10 and start <= 90 < end
        # Clamped at the ends.
        assert feed._result_window(0, 100, 10)[0] == 0
        assert feed._result_window(99, 100, 10) == (90, 100)

    def test_results_window_follows_cursor(self):
        # 100 hits in a 20-row terminal: the cursor near the bottom must be on
        # screen and the first rows must have scrolled off.
        results = [(f"/p/f{i}.py", i + 1, f"match-{i}") for i in range(100)]
        s = feed.SearchState(
            sid="s1", roots={"cwd": [Path("/p")]}, root="cwd", mode="RESULTS",
            results=results, cursor=90)
        out = self._text(feed.build_search_panel(s, term_height=20))
        assert "match-90" in out       # cursor row rendered
        assert "match-0 " not in out   # top rows windowed out
        assert "of 100" in out         # range indicator in the tag

    def test_results_window_shows_all_when_fitting(self):
        results = [(f"/p/f{i}.py", i + 1, f"m-{i}") for i in range(3)]
        s = feed.SearchState(
            sid="s1", roots={"cwd": [Path("/p")]}, root="cwd", mode="RESULTS",
            results=results, cursor=0)
        out = self._text(feed.build_search_panel(s, term_height=40))
        assert "m-0" in out and "m-1" in out and "m-2" in out
        assert "of 3" not in out  # no range indicator when everything fits


class TestDocsConsistency:
    """Guards the README's description of the agent feed against the misleading
    "indented agent tree" wording (the Agent column is a fixed-width tree gutter
    next to each row; rows and the Command column are never indented)."""

    def _readme(self):
        return (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )

    def test_readme_uses_tree_gutter_not_indented(self):
        readme = self._readme()
        assert "indented agent tree" not in readme
        assert "tree gutter" in readme
