import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_trail as feed
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


class TestSessionColor:
    def test_empty_returns_dim(self):
        assert feed.session_color("") == "dim"

    def test_returns_value_from_palette(self):
        assert feed.session_color("abcd1234") in feed.SESSION_PALETTE

    def test_deterministic_same_input_same_output(self):
        a = feed.session_color("715dd70e-d772-46ad-b4cb-e9ccacd7340a")
        b = feed.session_color("715dd70e-d772-46ad-b4cb-e9ccacd7340a")
        assert a == b

    def test_different_sessions_can_get_different_colors(self):
        # Collisions are allowed by the modulo mapping, but across many distinct
        # IDs we should see at least two distinct colors — otherwise the palette
        # or the hash is broken.
        ids = [f"session-{i:04d}" for i in range(50)]
        colors = {feed.session_color(sid) for sid in ids}
        assert len(colors) >= 2

    def test_palette_avoids_red(self):
        # Red is reserved for the dangerous-command marker; mixing it into the
        # Session column would be confusing.
        assert "red" not in feed.SESSION_PALETTE
        assert "bright_red" not in feed.SESSION_PALETTE


class TestLoadSessionNames:
    """Covers reading ~/.claude/sessions/*.json with mocked SESSIONS_DIR.

    These tests mutate module-level cache state (`_session_name_cache`,
    `_session_name_cache_ts`); each method resets it explicitly so order
    does not matter.
    """

    def _reset_cache(self):
        feed._session_name_cache.clear()
        feed._session_name_cache_ts = 0.0

    def test_reads_name_from_json(self, tmp_path):
        import json
        self._reset_cache()
        (tmp_path / "1234.json").write_text(
            json.dumps({"sessionId": "abc-123", "name": "my-session"})
        )
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {"abc-123": "my-session"}

    def test_skips_sessions_without_name(self, tmp_path):
        import json
        self._reset_cache()
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
        self._reset_cache()
        (tmp_path / "orphan.json").write_text(json.dumps({"name": "no-id"}))
        with patch.object(feed, "SESSIONS_DIR", tmp_path), \
             patch.object(feed, "SESSION_NAME_CACHE_TTL", 0.0):
            names = feed.load_session_names()
        assert names == {}

    def test_skips_malformed_json(self, tmp_path):
        import json
        self._reset_cache()
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
        self._reset_cache()
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
        self._reset_cache()
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
        self._reset_cache()
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
