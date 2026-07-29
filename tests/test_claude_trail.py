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
        entry = {"command": "ls", "session_id": "sid-0", "cwd": "/work",
                 "timestamp": "2026-06-30T12:00:00+00:00"}
        out = self._text(feed.build_prompt_panel(entry, 30, self.PROMPT, scroll=5))
        capacity = feed._prompt_capacity(30)
        assert "line 6" in out                       # window starts at the offset
        assert f"6-{5 + capacity} of 40" in out
        assert "line 1\n" not in out                 # scrolled past
        assert "40 lines" in out
        assert "2026-06-30 09:00:00" in out          # Sent row, offset sliced off

    def test_panel_shows_whole_prompt_when_it_fits(self):
        short = {"text": "a\nb\nc", "ts": ""}
        out = self._text(feed.build_prompt_panel({"session_id": "s"}, 40, short))
        assert "a" in out and "b" in out and "c" in out
        assert "of 3" not in out                     # no range chip when unwindowed
        assert "Sent" not in out                     # no ts, no row

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
