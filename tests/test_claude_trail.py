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
        assert feed.agent_label({"agent_id": "aid1"}, amap) == "Implement Phase 4"

    def test_falls_back_to_type_then_id(self):
        amap = {"aid1": {"type": "Explore", "description": ""}}
        assert feed.agent_label({"agent_id": "aid1"}, amap) == "Explore"
        # unknown agent_id -> first 8 chars of the id
        assert feed.agent_label({"agent_id": "abcdef1234"}, {}) == "abcdef12"


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
        # active agents sort first
        assert model.subagents[0].agent_id == "run1"

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

    def test_directory_column_shortens_cwd(self):
        # a swap of _render_directory / _render_files in COLUMNS must fail here
        cell = self._col(3).render(self._entry(cwd="/home/user/project"), feed.RenderCtx())
        assert cell == feed.short_path("/home/user/project")

    def test_files_column_extracts_basenames(self):
        cell = self._col(4).render(self._entry(command="cat /etc/hosts"), feed.RenderCtx())
        assert cell == "hosts"

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
        assert result is feed.Action.NONE
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


class TestSessionModal:
    """The `s` session-detail modal: opening state, modal key handling, and the
    build_session_panel rendering."""

    def _entries(self, n):
        return [{"command": f"c{i}", "session_id": f"sid-{i}"} for i in range(n)]

    def test_s_opens_session_detail_for_selected_session(self):
        state = feed.AppState(entries=self._entries(3), cursor=0)
        result = feed.apply_key(state, "s", 10)
        assert result is feed.Action.NONE
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
        assert "Subagents (2, 1 running)" in out

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
