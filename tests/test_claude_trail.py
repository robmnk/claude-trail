import os
import sys
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
    def test_short_cwd(self):
        assert feed.short_path("/home/user/proj") == ".../proj"

    def test_long_cwd(self):
        result = feed.short_path("/home/user/very-long-directory-name")
        assert result.startswith(".../")
        # Truncates the name portion to 10 chars
        name_part = result[4:]
        assert len(name_part) == 10

    def test_empty(self):
        assert feed.short_path("") == ".../"


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


class TestHookMain:
    def _run_hook(self, tmp_path, payload):
        import io, json
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
        import io, json
        log = tmp_path / "nested" / "subdir" / "command-log.jsonl"
        with patch.object(feed, "LOG_PATH", log):
            rc = feed.hook_main(stream=io.StringIO(json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            })))
        assert rc == 0
        assert log.exists()


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
        assert "Waiting for commands..." in out

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


class TestFilterSessionLog:
    def test_none_when_empty_session_id(self):
        assert feed.filter_session_log("") is None

    def test_none_when_log_missing(self, tmp_path):
        with patch.object(feed, "LOG_PATH", tmp_path / "absent.jsonl"):
            assert feed.filter_session_log("abc12345") is None

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

    def test_session_column_plain_string_without_color(self):
        cell = self._col(2).render(self._entry(), feed.RenderCtx())
        assert cell == "abcd1234"  # plain str, no tint

    def test_command_column_marks_dangerous(self):
        from rich.text import Text
        cell = self._col(5).render(self._entry(command="rm -rf /tmp/x"), feed.RenderCtx())
        assert isinstance(cell, Text)
        assert cell.plain.startswith("* ")

    def test_command_column_benign_has_no_marker(self):
        cell = self._col(5).render(self._entry(command="ls -la"), feed.RenderCtx())
        assert not cell.plain.startswith("* ")
        assert cell.plain == "ls -la"

    def test_appending_one_column_shows_in_table(self):
        """One extra Column entry is enough to add a rendered column end-to-end.

        Mutates the module-level COLUMNS list and restores it afterward so no
        global state leaks (build_table reads the module-level list directly).
        """
        marker = "EXTRA_MARK"
        extra = feed.Column(
            key=6, name="Extra", style="white",
            kwargs={"width": 20, "no_wrap": True},
            render=lambda entry, ctx: marker,
        )
        original = feed.COLUMNS
        feed.COLUMNS = original + [extra]
        try:
            cols = {c.key: True for c in feed.COLUMNS}
            console = Console(width=200, record=True)
            console.print(feed.build_table([self._entry()], 20, cols, 0))
            out = console.export_text()
            assert "Extra" in out   # header rendered
            assert marker in out    # cell rendered
        finally:
            feed.COLUMNS = original
