"""
NEXUS Local Development Control Center - Reliability Verification Suite
Verifies the 10 core invariants of Nexus:
1. Start all when stopped
2. Duplicate prevention for frontend
3. Duplicate prevention for backend
4. Preflight detection of healthy running services without restart
5. Redis optional development fallback
6. PostgreSQL offline preflight failure handling
7. External process port conflict identification without killing
8. Stop services terminates only Nexus-owned PIDs
9. Restart returns services to online state
10. Sanitized log output capture
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nexus import NexusCore, scrub_sensitive_data


@pytest.fixture
def temp_core(tmp_path):
    """Provides a NexusCore instance configured with an isolated test directory."""
    test_state_file = tmp_path / "nexus_state.json"
    test_logs_dir = tmp_path / "logs"
    test_logs_dir.mkdir(parents=True, exist_ok=True)

    core = NexusCore(project_root=tmp_path)
    core.state_file = test_state_file
    core.logs_dir = test_logs_dir
    return core


def test_01_start_all_when_stopped(temp_core):
    """1. Start all when stopped: starts missing services and transitions state to healthy."""
    with patch.object(temp_core, "check_postgres", return_value=(True, "Connected (Port 5432)", {"port": 5432})), \
         patch.object(temp_core, "check_redis", return_value=(False, "Unavailable — optional in development", {"port": 6379})), \
         patch.object(temp_core, "start_backend", return_value=(True, "Backend online (PID 1111)")), \
         patch.object(temp_core, "start_frontend", return_value=(True, "Frontend online (PID 2222)")), \
         patch.object(temp_core, "check_proxy", return_value=True):

        logs = []
        result = temp_core.start_all(log_callback=logs.append)
        assert result["success"] is True
        assert "Backend online (PID 1111)" in result["backend"]
        assert "Frontend online (PID 2222)" in result["frontend"]
        assert any("All required local services are online" in l for l in logs)


def test_02_duplicate_prevention_frontend(temp_core):
    """2. Duplicate prevention: frontend already healthy on 3000 is not spawned again."""
    with patch.object(temp_core, "is_port_in_use", return_value=True), \
         patch.object(temp_core, "get_pid_listening_on_port", return_value=9999), \
         patch.object(temp_core, "is_owned_pid", return_value=True), \
         patch("subprocess.Popen") as mock_popen:

        started, msg = temp_core.start_frontend()
        assert started is True
        assert "already running under Nexus management" in msg
        mock_popen.assert_not_called()


def test_03_duplicate_prevention_backend(temp_core):
    """3. Duplicate prevention: backend already healthy on 8000 is not spawned again."""
    with patch.object(temp_core, "check_postgres", return_value=(True, "Connected", {})), \
         patch.object(temp_core, "is_port_in_use", return_value=True), \
         patch.object(temp_core, "get_pid_listening_on_port", return_value=8888), \
         patch.object(temp_core, "is_owned_pid", return_value=True), \
         patch("subprocess.Popen") as mock_popen:

        started, msg = temp_core.start_backend()
        assert started is True
        assert "already running under Nexus management" in msg
        mock_popen.assert_not_called()


def test_04_preflight_detection_of_healthy_services(temp_core):
    """4. Preflight detection: identifies existing running services and preserves them."""
    with patch.object(temp_core, "is_port_in_use", side_effect=lambda p: p in (5432, 8000)), \
         patch.object(temp_core, "get_pid_listening_on_port", side_effect=lambda p: 7812 if p == 5432 else 8001), \
         patch.object(temp_core, "get_process_name", side_effect=lambda pid: "postgres.exe" if pid == 7812 else "uvicorn.exe"):

        pg_ok, pg_status, pg_meta = temp_core.check_postgres()
        assert pg_ok is True
        assert pg_meta["pid"] == 7812
        assert "Connected" in pg_status

        be_ok, be_status, be_meta = temp_core.check_backend()
        assert be_ok is True
        assert be_meta["pid"] == 8001


def test_05_redis_optional_development_fallback(temp_core):
    """5. Redis optional development fallback: Redis offline does not block startup when REDIS_REQUIRED = False."""
    with patch.object(temp_core, "is_port_in_use", return_value=False), \
         patch("subprocess.run", return_value=MagicMock(returncode=1)):

        redis_ok, redis_status, redis_meta = temp_core.check_redis()
        # Should report optional fallback in development
        assert "optional" in redis_status.lower()
        assert redis_ok is False

        # Verify start_all continues despite Redis being offline
        with patch.object(temp_core, "check_postgres", return_value=(True, "Connected", {})), \
             patch.object(temp_core, "start_backend", return_value=(True, "Backend online")), \
             patch.object(temp_core, "start_frontend", return_value=(True, "Frontend online")), \
             patch.object(temp_core, "check_proxy", return_value=True):

            res = temp_core.start_all()
            assert res["success"] is True


def test_06_postgres_offline_preflight_failure_handling(temp_core):
    """6. PostgreSQL offline preflight: start_all aborts immediately and does not start backend or frontend."""
    with patch.object(temp_core, "check_postgres", return_value=(False, "Offline (Port 5432 Closed)", {})), \
         patch.object(temp_core, "start_backend") as mock_be, \
         patch.object(temp_core, "start_frontend") as mock_fe:

        logs = []
        res = temp_core.start_all(log_callback=logs.append)
        assert res["success"] is False
        assert "Offline" in res["error"]
        mock_be.assert_not_called()
        mock_fe.assert_not_called()


def test_07_external_process_port_conflict_without_killing(temp_core):
    """7. External port conflict: foreign unowned PID triggers port conflict without killing the process."""
    with patch.object(temp_core, "check_postgres", return_value=(True, "Connected", {})), \
         patch.object(temp_core, "is_port_in_use", return_value=True), \
         patch.object(temp_core, "get_pid_listening_on_port", return_value=99999), \
         patch.object(temp_core, "is_owned_pid", return_value=False), \
         patch.object(temp_core, "check_backend", return_value=(False, "Port Conflict", {"http_healthy": False})), \
         patch.object(temp_core, "get_process_name", return_value="foreign_tool.exe"), \
         patch("subprocess.run") as mock_run:

        started, err = temp_core.start_backend()
        assert started is False
        assert "already occupied by external process PID 99999" in err
        # Ensure taskkill was NOT called on foreign_tool
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0] if call_args[0] else []
            assert "taskkill" not in cmd


def test_08_stop_services_terminates_only_nexus_owned_pids(temp_core):
    """8. Stop services: terminates only Nexus-owned PIDs and cleans state."""
    # Write owned PIDs to state
    temp_core.save_state({
        "backend": {"pid": 4444, "port": 8000, "started_at": "2026-11-10T00:00:00"},
        "frontend": {"pid": 5555, "port": 3000, "started_at": "2026-11-10T00:00:00"},
    })

    killed_pids = []

    def fake_is_alive(pid):
        return pid not in killed_pids

    def fake_run(cmd, *args, **kwargs):
        if "taskkill" in cmd:
            idx = cmd.index("/PID") + 1
            killed_pids.append(int(cmd[idx]))
        return MagicMock(returncode=0)

    with patch.object(temp_core, "is_pid_alive", side_effect=fake_is_alive), \
         patch("subprocess.run", side_effect=fake_run):

        stopped = temp_core.stop_all()
        assert 4444 in killed_pids
        assert 5555 in killed_pids
        assert "Stopped backend" in stopped["backend"]
        assert "Stopped frontend" in stopped["frontend"]

        # State file should now be cleared
        state = temp_core.load_state()
        assert state == {}


def test_09_restart_returns_services_to_online(temp_core):
    """9. Restart returns services to online state."""
    with patch.object(temp_core, "stop_all", return_value={"backend": "Stopped", "frontend": "Stopped"}) as mock_stop, \
         patch.object(temp_core, "is_port_in_use", return_value=False), \
         patch.object(temp_core, "start_all", return_value={"success": True, "backend": "online", "frontend": "online"}) as mock_start:

        res = temp_core.restart_all()
        mock_stop.assert_called_once()
        mock_start.assert_called_once()
        assert res["success"] is True


def test_10_sanitized_log_output_capture():
    """10. Sanitized log capture: sensitive tokens, passwords, and API keys are scrubbed."""
    raw_sample = (
        "Connecting to postgresql://postgres:SecretPassword123!@localhost:5432/support_db with key "
        "AIzaSyD-fakeKeyAbc12345678901234567890 and Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-x123 "
        "and sk-proj1234567890abcdef1234567890"
    )
    scrubbed = scrub_sensitive_data(raw_sample)

    assert "SecretPassword123!" not in scrubbed
    assert "AIzaSyD-fakeKeyAbc12345678901234567890" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-x123" not in scrubbed
    assert "sk-proj1234567890abcdef1234567890" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_11_descendant_pid_ownership_detection(temp_core):
    """11. Reopened NEXUS detects owned descendant listener PIDs (uvicorn reloader / npm -> node)."""
    temp_core.save_state({
        "backend": {"pid": 1000, "listener_pid": 1002, "port": 8000},
        "frontend": {"pid": 2000, "port": 3000},
    })
    with patch.object(temp_core, "is_pid_alive", return_value=True), \
         patch("nexus._get_windows_parent_pid_map", return_value={2002: 2001, 2001: 2000}):
        assert temp_core.is_owned_pid("backend", 1000) is True
        assert temp_core.is_owned_pid("backend", 1002) is True
        assert temp_core.is_owned_pid("frontend", 2002) is True
        assert temp_core.is_owned_pid("frontend", 99999) is False


def test_12_open_dashboard_reuses_existing_browser_window(temp_core):
    """12. OPEN DASHBOARD focuses existing browser window before falling back to webbrowser.open."""
    with patch("nexus._focus_existing_dashboard_window", return_value=True) as mock_focus, \
         patch("webbrowser.open") as mock_open:
        ok, msg = temp_core.open_dashboard("http://localhost:3000")
        assert ok is True
        assert "Focused existing" in msg
        mock_focus.assert_called_once()
        mock_open.assert_not_called()

    with patch("nexus._focus_existing_dashboard_window", return_value=False), \
         patch("webbrowser.open", return_value=True) as mock_open:
        ok, msg = temp_core.open_dashboard("http://localhost:3000")
        assert ok is True
        mock_open.assert_called_once_with("http://localhost:3000", new=0, autoraise=True)


def test_13_backend_health_timeout_returns_failure_not_success(temp_core):
    """13. Timed-out startup health check fails cleanly and cleans up spawned process."""
    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.poll.return_value = None

    with patch.object(temp_core, "check_postgres", return_value=(True, "Connected", {})), \
         patch.object(temp_core, "is_port_in_use", return_value=False), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch.object(temp_core, "check_backend", return_value=(True, "Port Open (PID 7777)", {"http_healthy": False})), \
         patch.object(temp_core, "stop_service") as mock_stop:
        ok, msg = temp_core.start_backend(timeout_seconds=0.1)
        assert ok is False
        assert "timed out" in msg.lower()
        mock_stop.assert_called_once()


def test_14_start_all_emits_progress_callbacks_and_blocks_browser_on_failure(temp_core):
    """14. start_all emits progress steps and reports failure without ready platform state."""
    progress_events = []
    with patch.object(temp_core, "check_postgres", return_value=(True, "Connected (Port 5432)", {})), \
         patch.object(temp_core, "check_redis", return_value=(False, "Unavailable — optional in development", {})), \
         patch.object(temp_core, "start_backend", return_value=(False, "Port 8000 conflict: external PID 999")):
        res = temp_core.start_all(progress_callback=lambda pct, txt: progress_events.append((pct, txt)))
        assert res["success"] is False
        assert res["step"] == "backend"
        assert any(pct == 45 and "Failed" in txt for pct, txt in progress_events)


def test_15_gui_browser_not_opened_when_startup_fails():
    """15. GUI control panel displays Last Error and never opens browser when startup fails."""
    import tkinter as tk
    import nexus

    orig_mainloop = tk.Tk.mainloop
    captured_texts = []

    def inspect_failure(self_tk):
        import time
        deadline = time.monotonic() + 5.0
        def poll():
            self_tk.update()
            texts = []
            def walk(w):
                try:
                    t = w.cget("text")
                    if t:
                        texts.append(t)
                except Exception:
                    pass
                for c in w.winfo_children():
                    walk(c)
            walk(self_tk)
            if any("Offline" in t for t in texts) or time.monotonic() > deadline:
                captured_texts.extend(texts)
                self_tk.destroy()
                return
            self_tk.after(100, poll)
        self_tk.after(300, poll)
        orig_mainloop(self_tk)

    with patch.object(nexus.NexusCore, "check_postgres", return_value=(False, "Offline (Port 5432 Closed)", {"port": 5432})), \
         patch.object(nexus.NexusCore, "check_redis", return_value=(False, "Unavailable — optional in development", {"port": 6379})), \
         patch.object(nexus.NexusCore, "check_backend", return_value=(False, "Stopped", {"port": 8000, "http_healthy": False})), \
         patch.object(nexus.NexusCore, "check_frontend", return_value=(False, "Stopped", {"port": 3000, "http_healthy": False})), \
         patch("webbrowser.open") as mock_browser, \
         patch("nexus._focus_existing_dashboard_window", return_value=False), \
         patch.object(tk.Tk, "mainloop", inspect_failure):
        nexus.run_gui(auto_start=True)

    mock_browser.assert_not_called()
    assert any("Offline (Port 5432 Closed)" in t for t in captured_texts)
    assert "Failed" in captured_texts


def test_16_gui_detects_healthy_services_and_closing_leaves_running():
    """16. GUI detects already-healthy services, opens dashboard, and closing never calls stop_all."""
    import tkinter as tk
    import nexus

    orig_mainloop = tk.Tk.mainloop
    opened = []

    def inspect_healthy(self_tk):
        import time
        deadline = time.monotonic() + 5.0
        def poll():
            self_tk.update()
            if opened or time.monotonic() > deadline:
                # Invoke WM_DELETE_WINDOW close handler cleanly
                self_tk.destroy()
                return
            self_tk.after(100, poll)
        self_tk.after(250, poll)
        orig_mainloop(self_tk)

    with patch.object(nexus.NexusCore, "check_postgres", return_value=(True, "Connected (Port 5432)", {"port": 5432, "pid": 7212})), \
         patch.object(nexus.NexusCore, "check_redis", return_value=(False, "Unavailable — optional in development", {"port": 6379})), \
         patch.object(nexus.NexusCore, "check_backend", return_value=(True, "Online", {"port": 8000, "pid": 1111, "is_owned": True, "http_healthy": True})), \
         patch.object(nexus.NexusCore, "check_frontend", return_value=(True, "Online", {"port": 3000, "pid": 2222, "is_owned": True, "http_healthy": True})), \
         patch.object(nexus.NexusCore, "start_all") as mock_start_all, \
         patch.object(nexus.NexusCore, "stop_all") as mock_stop_all, \
         patch("webbrowser.open", side_effect=lambda url, **kw: opened.append(url) or True), \
         patch("nexus._focus_existing_dashboard_window", return_value=False), \
         patch.object(tk.Tk, "mainloop", inspect_healthy):
        nexus.run_gui(auto_start=True)

    mock_start_all.assert_not_called()
    mock_stop_all.assert_not_called()
    assert opened == ["http://localhost:3000"]


def test_17_read_sanitized_log_tail_enforces_containment_and_redaction(temp_core):
    """17. In-app log viewer rejects path traversal and redacts secrets before rendering."""
    secret_log = temp_core.logs_dir / "nexus.log"
    secret_log.write_text("Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig\nAIzaSyFake12345678901234567890\n", encoding="utf-8")

    sanitized = temp_core.read_sanitized_log_tail("nexus.log")
    assert "eyJhbGciOiJIUzI1NiJ9" not in sanitized
    assert "AIzaSyFake12345678901234567890" not in sanitized
    assert "[REDACTED]" in sanitized

    traversal = temp_core.read_sanitized_log_tail("../../.env")
    assert "does not exist" in traversal.lower() or "unable" in traversal.lower()


def test_18_hidden_subprocess_kwargs_and_no_shell_true():
    """18. Every subprocess call uses CREATE_NO_WINDOW + SW_HIDE and avoids shell=True."""
    import nexus

    kwargs = nexus._hidden_subprocess_kwargs()
    if sys.platform == "win32":
        assert kwargs["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
        assert kwargs["startupinfo"].wShowWindow == 0  # SW_HIDE
        assert kwargs["startupinfo"].dwFlags & 0x00000001  # STARTF_USESHOWWINDOW

    source = Path(nexus.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source


def test_19_native_polling_avoids_netstat_tasklist_spawns(temp_core):
    """19. Status polling queries Win32 socket/process APIs natively without spawning netstat/tasklist."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific native polling test")

    with patch("subprocess.check_output") as mock_check_output:
        pid = temp_core.get_pid_listening_on_port(5432)
        if pid:
            pname = temp_core.get_process_name(pid)
            alive = temp_core.is_pid_alive(pid)
            assert pname != "unknown"
            assert alive is True
        mock_check_output.assert_not_called()


def test_20_shortcut_targets_pythonw_console_free():
    """20. Desktop shortcut generator configures console-free pythonw.exe launch when available."""
    import nexus

    captured_cmd = []

    def fake_run(cmd, *args, **kwargs):
        captured_cmd.append((cmd, kwargs))
        return MagicMock(returncode=0, stderr="")

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(Path, "exists", return_value=True):
        ok, _ = nexus.create_desktop_shortcut()
        assert ok is True
        ps_script = captured_cmd[0][0][-1]
        assert "pythonw.exe" in ps_script
        if sys.platform == "win32":
            assert captured_cmd[0][1]["creationflags"] & 0x08000000



