#!/usr/bin/env python3
"""NEXUS — One-Click Local Development Control Center.

AI Customer Support Platform
Platform: Windows

Provides unified management for:
- FastAPI Backend (Port 8000)
- React + Vite Frontend (Port 3000)
- PostgreSQL Connectivity (Port 5432 - Required)
- Redis Cache Detection & Fallback (Port 6379 - Optional in dev)

Supports:
- Desktop Control Center GUI (Dark Navy & Cyan Tkinter UI with one-click startup)
- Automatic startup health verification & browser launch when services are ready
- CLI headless control (--start, --stop, --restart, --status)
- Strict process-tree ownership tracking (never kills external processes)
- Sanitized diagnostic logging
- Windows Desktop shortcut creation
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
import webbrowser

# Project Directories
PROJECT_ROOT = Path(__file__).resolve().parent
NEXUS_DIR = PROJECT_ROOT / ".nexus"
LOGS_DIR = NEXUS_DIR / "logs"
STATE_FILE = NEXUS_DIR / "nexus_state.json"
ICON_PATH = NEXUS_DIR / "nexus.ico"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
BACKEND_DIR = PROJECT_ROOT / "backend"
VENV_DIR = PROJECT_ROOT / ".venv"
DASHBOARD_URL = "http://localhost:3000"
DASHBOARD_TITLE_TOKEN = "AI Customer Support Platform"

NEXUS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# 1. Sanitized Logging Utility
# ==============================================================================

SENSITIVE_PATTERNS = [
    re.compile(r"(password\s*[:=]\s*)[^\s,;&'\"]+", re.IGNORECASE),
    re.compile(r"(bearer\s+)[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"(secret\s*[:=]\s*)[^\s,;&'\"]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[:=]\s*)[^\s,;&'\"]+", re.IGNORECASE),
    re.compile(r"(token\s*[:=]\s*)[^\s,;&'\"]+", re.IGNORECASE),
    re.compile(r"(://[^:]+:)([^@]+)(@)", re.IGNORECASE),  # Connection string password
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),  # Google/Gemini API key
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}"),  # OpenAI API key pattern
]


def sanitize_message(msg: str) -> str:
    """Strips secrets, passwords, and tokens from log lines."""
    clean = str(msg)
    for pat in SENSITIVE_PATTERNS:
        if pat.groups == 3:  # connection string
            clean = pat.sub(r"\1[REDACTED]\3", clean)
        elif pat.groups == 1:
            clean = pat.sub(r"\1[REDACTED]", clean)
        else:
            clean = pat.sub("[REDACTED]", clean)
    return clean


scrub_sensitive_data = sanitize_message


def append_log(filename: str, message: str, logs_dir: Optional[Path] = None) -> None:
    """Appends sanitized log entry with UTC timestamp."""
    target_dir = logs_dir or LOGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / filename
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    clean_msg = sanitize_message(message)
    line = f"[{ts}] {clean_msg}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ==============================================================================
# 2. Windows Process Tree, Hidden Subprocess & Browser Window Helpers
# ==============================================================================

def _hidden_subprocess_kwargs(extra_flags: int = 0, include_stdin: bool = True) -> Dict[str, Any]:
    """Returns Windows creationflags/STARTUPINFO kwargs that suppress console windows."""
    if sys.platform != "win32":
        return {"stdin": subprocess.DEVNULL} if include_stdin else {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    kwargs: Dict[str, Any] = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | extra_flags,
        "startupinfo": si,
    }
    if include_stdin:
        kwargs["stdin"] = subprocess.DEVNULL
    return kwargs


def _get_windows_process_snapshot() -> Dict[int, Tuple[int, str]]:
    """Returns {pid: (parent_pid, exe_name)} snapshot on Windows without spawning console tools."""
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        import ctypes.wintypes as w

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", w.DWORD),
                ("cntUsage", w.DWORD),
                ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", w.LONG),
                ("dwFlags", w.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (-1, 0):
            return {}
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            procs: Dict[int, Tuple[int, str]] = {}
            if k32.Process32First(snap, ctypes.byref(entry)):
                while True:
                    exe_name = entry.szExeFile.decode("utf-8", errors="replace")
                    procs[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), exe_name)
                    if not k32.Process32Next(snap, ctypes.byref(entry)):
                        break
            return procs
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return {}


def _get_windows_parent_pid_map() -> Dict[int, int]:
    """Returns {pid: parent_pid} snapshot on Windows using kernel32 Toolhelp32."""
    return {pid: ppid for pid, (ppid, _) in _get_windows_process_snapshot().items()}


def _get_windows_listening_pid_native(port: int) -> Tuple[bool, Optional[int]]:
    """Queries IPv4/IPv6 TCP listening socket tables via iphlpapi without spawning netstat."""
    if sys.platform != "win32":
        return False, None
    try:
        import ctypes
        import ctypes.wintypes as w
        import struct

        iphlpapi = ctypes.windll.iphlpapi
        TCP_TABLE_OWNER_PID_LISTENER = 3

        class MIB_TCPROW_OWNER_PID(ctypes.Structure):
            _fields_ = [
                ("dwState", w.DWORD),
                ("dwLocalAddr", w.DWORD),
                ("dwLocalPort", w.DWORD),
                ("dwRemoteAddr", w.DWORD),
                ("dwRemotePort", w.DWORD),
                ("dwOwningPid", w.DWORD),
            ]

        class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
            _fields_ = [
                ("ucLocalAddr", ctypes.c_ubyte * 16),
                ("dwLocalScopeId", w.DWORD),
                ("dwLocalPort", w.DWORD),
                ("ucRemoteAddr", ctypes.c_ubyte * 16),
                ("dwRemoteScopeId", w.DWORD),
                ("dwRemotePort", w.DWORD),
                ("dwState", w.DWORD),
                ("dwOwningPid", w.DWORD),
            ]

        queried_any = False
        for family, row_cls in ((socket.AF_INET, MIB_TCPROW_OWNER_PID), (socket.AF_INET6, MIB_TCP6ROW_OWNER_PID)):
            size = w.DWORD(0)
            iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_LISTENER, 0)
            if size.value <= 0:
                continue
            buf = ctypes.create_string_buffer(size.value)
            if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_LISTENER, 0) == 0:
                queried_any = True
                num = struct.unpack_from("I", buf.raw, 0)[0]
                rows = (row_cls * num).from_buffer_copy(buf.raw[4:])
                for r in rows:
                    if socket.ntohs(r.dwLocalPort & 0xFFFF) == port:
                        return True, int(r.dwOwningPid)
        return queried_any, None
    except Exception:
        return False, None


def _focus_existing_dashboard_window(title_token: str = DASHBOARD_TITLE_TOKEN) -> bool:
    """Brings an already-open browser window displaying the dashboard to the foreground."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        matched: List[int] = []

        def callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if title_token.lower() in buf.value.lower():
                        matched.append(hwnd)
                        return False
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        if matched:
            hwnd = matched[0]
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


# ==============================================================================
# 3. Nexus Core Process Management Engine
# ==============================================================================

class NexusCore:
    """Manages ports, child processes, readiness probes, and service state."""

    def __init__(self, project_root: Optional[Path] = None):
        self.root = project_root or PROJECT_ROOT
        self.frontend_dir = self.root / "frontend"
        self.backend_dir = self.root / "backend"
        self._docker_cache: Optional[Tuple[float, bool]] = None
        if project_root is not None:
            nexus_dir = self.root / ".nexus"
            self.logs_dir = nexus_dir / "logs"
            self.state_file = nexus_dir / "nexus_state.json"
        else:
            self.logs_dir = LOGS_DIR
            self.state_file = STATE_FILE

    def _log(self, filename: str, message: str) -> None:
        append_log(filename, message, logs_dir=self.logs_dir)

    # --- Port & Socket Utilities ---

    @staticmethod
    def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
        """Returns True if the port is currently accepting TCP connections."""
        try:
            with socket.create_connection((host, port), timeout=0.6):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    @staticmethod
    def get_pid_listening_on_port(port: int) -> Optional[int]:
        """Identifies the PID listening on a given port without flashing console windows."""
        handled, native_pid = _get_windows_listening_pid_native(port)
        if handled:
            return native_pid
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"],
                text=True,
                stderr=subprocess.DEVNULL,
                **_hidden_subprocess_kwargs(),
            )
            port_token = f":{port}"
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in parts and parts[1].endswith(port_token):
                    try:
                        return int(parts[-1])
                    except ValueError:
                        continue
        except Exception:
            pass
        return None

    @staticmethod
    def get_process_name(pid: int) -> str:
        """Retrieves process image name by PID without flashing console windows."""
        snap = _get_windows_process_snapshot()
        if snap and pid in snap:
            return snap[pid][1] or "unknown"
        try:
            out = subprocess.check_output(
                ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                text=True,
                stderr=subprocess.DEVNULL,
                **_hidden_subprocess_kwargs(),
            ).strip()
            if out:
                parts = [p.strip('"\r\n ') for p in out.split(",")]
                if parts and parts[0]:
                    return parts[0]
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def is_pid_alive(pid: int) -> bool:
        """Checks if a process ID is currently alive without spawning console windows."""
        snap = _get_windows_process_snapshot()
        if snap:
            return pid in snap
        try:
            out = subprocess.check_output(
                ["tasklist", "/fi", f"PID eq {pid}", "/nh"],
                text=True,
                stderr=subprocess.DEVNULL,
                **_hidden_subprocess_kwargs(),
            )
            return str(pid) in out
        except Exception:
            return False

    # --- State Persistence (Ownership Tracking) ---

    def load_state(self) -> Dict[str, Any]:
        """Reads persisted Nexus ownership state."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def save_state(self, state: Dict[str, Any]) -> None:
        """Writes persisted Nexus ownership state atomically."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            tmp_file.replace(self.state_file)
        except Exception as e:
            self._log("nexus.log", f"Failed to save state: {e}")

    def is_owned_pid(self, service: str, pid: int) -> bool:
        """Verifies if a given PID or its parent tree was launched and owned by Nexus."""
        if not pid:
            return False
        state = self.load_state()
        svc_state = state.get(service, {})
        if not isinstance(svc_state, dict):
            return False
        root_pid = svc_state.get("pid")
        listener_pid = svc_state.get("listener_pid")
        if pid == root_pid or (listener_pid and pid == listener_pid):
            return True
        if root_pid and self.is_pid_alive(root_pid):
            parent_map = _get_windows_parent_pid_map()
            cur = pid
            for _ in range(12):
                ppid = parent_map.get(cur)
                if not ppid or ppid == cur:
                    break
                if ppid == root_pid:
                    return True
                cur = ppid
        return False

    # --- Service Health Checks ---

    def check_postgres(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Checks connectivity to required PostgreSQL service on port 5432."""
        in_use = self.is_port_in_use(5432)
        listening_pid = self.get_pid_listening_on_port(5432) if in_use else None
        details: Dict[str, Any] = {"port": 5432, "in_use": in_use, "pid": listening_pid}

        if not in_use:
            details["state"] = "Failed"
            msg = "PostgreSQL service is offline (port 5432 closed). Ensure PostgreSQL is started."
            self._log("postgres_check.log", msg)
            return False, "Offline (Port 5432 Closed)", details

        try:
            from backend.app.core.config import settings
            db_url = getattr(settings, "sync_database_url", "")
            details["db_url_configured"] = bool(db_url)
        except Exception:
            pass

        pname = self.get_process_name(listening_pid) if listening_pid else "postgres.exe"
        details["process_name"] = pname
        details["state"] = "Running"
        msg = f"PostgreSQL online on port 5432 (PID: {listening_pid}, Image: {pname})."
        self._log("postgres_check.log", msg)
        return True, "Connected (Port 5432)", details

    def check_redis(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Checks Redis on port 6379, detects Docker availability, and notes fallback."""
        in_use = self.is_port_in_use(6379)
        listening_pid = self.get_pid_listening_on_port(6379) if in_use else None
        details: Dict[str, Any] = {"port": 6379, "in_use": in_use, "pid": listening_pid}

        now = time.monotonic()
        if self._docker_cache is not None and (now - self._docker_cache[0]) < 60.0:
            docker_available = self._docker_cache[1]
        else:
            docker_available = False
            try:
                res = subprocess.run(
                    ["docker", "ps"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    **_hidden_subprocess_kwargs(),
                )
                docker_available = res.returncode == 0
            except Exception:
                docker_available = False
            self._docker_cache = (now, docker_available)
        details["docker_available"] = docker_available

        if in_use:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(("127.0.0.1", 6379))
                    s.sendall(b"PING\r\n")
                    resp = s.recv(16)
                    if b"PONG" in resp:
                        details["ping_response"] = "PONG"
                        details["state"] = "Running"
                        self._log("redis_check.log", "Redis responded to PING handshake on port 6379.")
                        return True, "Connected (Port 6379)", details
            except Exception as e:
                details["ping_error"] = str(e)

            details["state"] = "Starting"
            return True, "Port Open (Handshake Pending)", details

        redis_required = False
        try:
            from backend.app.core.config import settings
            redis_required = getattr(settings, "REDIS_REQUIRED", False)
        except Exception:
            redis_required = False
        details["redis_required"] = redis_required

        if redis_required:
            details["state"] = "Failed"
            msg = "Redis is offline and configured as REQUIRED. Services will not start."
            self._log("redis_check.log", msg)
            return False, "Required but unavailable", details
        else:
            details["state"] = "Stopped"
            msg = "Redis is unavailable — optional in dev (in-memory rate limiting and queue fallback active)."
            self._log("redis_check.log", msg)
            return False, "Unavailable — optional in development", details

    def check_backend(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Checks FastAPI backend readiness on port 8000."""
        in_use = self.is_port_in_use(8000)
        pid = self.get_pid_listening_on_port(8000) if in_use else None
        state = self.load_state().get("backend", {})
        is_owned = self.is_owned_pid("backend", pid) if pid else False

        details: Dict[str, Any] = {
            "port": 8000,
            "in_use": in_use,
            "pid": pid,
            "is_owned": is_owned,
            "started_at": state.get("started_at"),
            "http_healthy": False,
            "state": "Stopped",
        }

        if not in_use:
            return False, "Stopped", details

        # Query real readiness endpoint first, falling back to liveness/docs only on 404
        for url in ("http://127.0.0.1:8000/api/v1/health/ready", "http://127.0.0.1:8000/api/v1/health/live"):
            try:
                start_t = time.perf_counter()
                req = urllib.request.Request(url, headers={"User-Agent": "Nexus-Probe"})
                with urllib.request.urlopen(req, timeout=1.5) as response:
                    latency_ms = round((time.perf_counter() - start_t) * 1000, 1)
                    details["latency_ms"] = latency_ms
                    details["http_status"] = response.status
                    if response.status == 200:
                        details["http_healthy"] = True
                        details["state"] = "Running"
                        status_str = f"Online (PID {pid} · {latency_ms}ms)" if pid else f"Online ({latency_ms}ms)"
                        return True, status_str, details
            except urllib.error.HTTPError as he:
                details["http_status"] = he.code
                details["probe_error"] = f"HTTP {he.code} on {url}"
                if he.code != 404:
                    details["state"] = "Failed" if he.code >= 500 else "Starting"
                    break
            except Exception as e:
                details["probe_error"] = str(e)
                break

        details["state"] = details["state"] if details["state"] != "Stopped" else "Starting"
        return True, f"Port Open (PID {pid})", details

    def check_frontend(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Checks React + Vite frontend readiness on port 3000 and proxy health."""
        in_use = self.is_port_in_use(3000)
        pid = self.get_pid_listening_on_port(3000) if in_use else None
        state = self.load_state().get("frontend", {})
        is_owned = self.is_owned_pid("frontend", pid) if pid else False

        details: Dict[str, Any] = {
            "port": 3000,
            "in_use": in_use,
            "pid": pid,
            "is_owned": is_owned,
            "started_at": state.get("started_at"),
            "http_healthy": False,
            "state": "Stopped",
        }

        if not in_use:
            return False, "Stopped", details

        for host in ["http://127.0.0.1:3000/", "http://localhost:3000/"]:
            try:
                start_t = time.perf_counter()
                with urllib.request.urlopen(host, timeout=1.5) as resp:
                    latency_ms = round((time.perf_counter() - start_t) * 1000, 1)
                    details["latency_ms"] = latency_ms
                    details["http_status"] = resp.status
                    if resp.status == 200:
                        details["http_healthy"] = True
                        details["state"] = "Running"
                        details["proxy_verified"] = self.check_proxy()
                        status_str = f"Online (PID {pid} · {latency_ms}ms)" if pid else f"Online ({latency_ms}ms)"
                        return True, status_str, details
            except Exception as e:
                details["probe_error"] = str(e)

        details["state"] = "Starting"
        return True, f"Port Open (PID {pid})", details

    def check_proxy(self) -> bool:
        """Verifies Vite reverse proxy forwards /api requests to FastAPI."""
        for host in [
            "http://127.0.0.1:3000/api/v1/health/ready",
            "http://localhost:3000/api/v1/health/ready",
        ]:
            try:
                with urllib.request.urlopen(host, timeout=2.0) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
        return False

    def get_all_statuses(self) -> Dict[str, Any]:
        """Aggregates comprehensive health matrix for all services."""
        pg_ok, pg_status, pg_meta = self.check_postgres()
        redis_ok, redis_status, redis_meta = self.check_redis()
        be_ok, be_status, be_meta = self.check_backend()
        fe_ok, fe_status, fe_meta = self.check_frontend()

        return {
            "postgres": {"healthy": pg_ok, "status": pg_status, "meta": pg_meta},
            "redis": {"healthy": redis_ok, "status": redis_status, "meta": redis_meta},
            "backend": {"healthy": be_ok, "status": be_status, "meta": be_meta},
            "frontend": {"healthy": fe_ok, "status": fe_status, "meta": fe_meta},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def are_platform_services_ready(self) -> bool:
        """True only when Postgres, Backend HTTP health, and Frontend HTTP health are all verified."""
        pg_ok, _, _ = self.check_postgres()
        be_ok, _, be_meta = self.check_backend()
        fe_ok, _, fe_meta = self.check_frontend()
        return bool(pg_ok and be_ok and be_meta.get("http_healthy") and fe_ok and fe_meta.get("http_healthy"))

    def open_dashboard(self, url: str = DASHBOARD_URL) -> Tuple[bool, str]:
        """Opens the dashboard in the default browser, reusing an existing browser session where possible."""
        if _focus_existing_dashboard_window():
            self._log("nexus.log", "Focused existing dashboard browser window.")
            return True, "Focused existing dashboard window."
        try:
            opened = webbrowser.open(url, new=0, autoraise=True)
            if opened:
                self._log("nexus.log", f"Opened dashboard in default browser: {url}")
                return True, f"Opened {url}"
            return False, f"Default browser did not accept URL: {url}"
        except Exception as e:
            err = f"Failed to open browser for {url}: {e}"
            self._log("nexus.log", err)
            return False, err

    # --- Start Operations ---

    def start_backend(self, log_callback=None, timeout_seconds: float = 45.0) -> Tuple[bool, str]:
        """Launches FastAPI uvicorn server in a persistent, owned process and waits for HTTP health."""
        def log(msg: str, write_service_log: bool = True):
            if write_service_log:
                self._log("backend.log", msg)
            self._log("nexus.log", msg)
            if log_callback:
                log_callback(msg)

        pg_ok, pg_status, _ = self.check_postgres()
        if not pg_ok:
            err = f"Cannot start backend: PostgreSQL required database is offline ({pg_status})."
            log(err)
            return False, err

        if self.is_port_in_use(8000):
            pid = self.get_pid_listening_on_port(8000)
            if pid and self.is_owned_pid("backend", pid):
                msg = f"Backend already running under Nexus management (PID: {pid})."
                log(msg)
                return True, msg
            be_ok, _, be_meta = self.check_backend()
            if be_ok and be_meta.get("http_healthy"):
                msg = f"Backend already healthy on port 8000 (External PID: {pid}). Preserving existing instance."
                log(msg)
                return True, msg
            pname = self.get_process_name(pid) if pid else "unknown"
            err = f"Port 8000 conflict: already occupied by external process PID {pid} ({pname}). Nexus will not terminate external processes."
            log(err)
            return False, err

        python_exe = VENV_DIR / "Scripts" / "python.exe"
        if not python_exe.exists():
            python_exe = Path(sys.executable)

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        backend_log_path = self.logs_dir / "backend.log"
        backend_log_file = open(backend_log_path, "a", encoding="utf-8")
        backend_log_file.write(f"\n--- NEXUS STARTUP AT {datetime.now(timezone.utc).isoformat()} ---\n")
        backend_log_file.flush()

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.backend_dir)
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [
            str(python_exe),
            "-m", "uvicorn",
            "app.main:app",
            "--host", "127.0.0.1",
            "--port", "8000",
            "--reload",
            "--reload-dir", str(self.backend_dir / "app"),
        ]

        log(f"Launching FastAPI backend: {' '.join(cmd)}", write_service_log=False)

        extra_flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                env=env,
                stdout=backend_log_file,
                stderr=subprocess.STDOUT,
                **_hidden_subprocess_kwargs(extra_flags=extra_flags),
            )
        finally:
            backend_log_file.close()

        state = self.load_state()
        state["backend"] = {
            "pid": proc.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "port": 8000,
            "command": " ".join(cmd),
        }
        self.save_state(state)
        log(f"FastAPI backend spawned (PID {proc.pid}). Waiting for /api/v1/health/ready...", write_service_log=False)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = f"FastAPI backend terminated prematurely (exit code: {proc.returncode}). Check .nexus/logs/backend.log."
                log(err)
                state = self.load_state()
                state.pop("backend", None)
                self.save_state(state)
                return False, err

            be_ok, _, be_meta = self.check_backend()
            if be_ok and be_meta.get("http_healthy"):
                listener_pid = be_meta.get("pid") or self.get_pid_listening_on_port(8000)
                if listener_pid:
                    state = self.load_state()
                    if "backend" in state:
                        state["backend"]["listener_pid"] = listener_pid
                        self.save_state(state)
                log(f"FastAPI backend healthy at http://127.0.0.1:8000 (Root PID: {proc.pid}, Listener PID: {listener_pid}).", write_service_log=False)
                return True, f"Backend running (PID {listener_pid or proc.pid})"

            time.sleep(0.4)

        err = f"Backend health check timed out after {int(timeout_seconds)}s. Check .nexus/logs/backend.log."
        log(err)
        self.stop_service("backend", log_callback=log_callback)
        return False, err

    def start_frontend(self, log_callback=None, timeout_seconds: float = 30.0) -> Tuple[bool, str]:
        """Launches React + Vite dev server in a persistent, owned process and waits for HTTP health."""
        def log(msg: str):
            self._log("frontend.log", msg)
            self._log("nexus.log", msg)
            if log_callback:
                log_callback(msg)

        if self.is_port_in_use(3000):
            pid = self.get_pid_listening_on_port(3000)
            if pid and self.is_owned_pid("frontend", pid):
                msg = f"Frontend already running under Nexus management (PID: {pid})."
                log(msg)
                return True, msg
            fe_ok, _, fe_meta = self.check_frontend()
            if fe_ok and fe_meta.get("http_healthy"):
                msg = f"Frontend already healthy on port 3000 (External PID: {pid}). Preserving existing instance."
                log(msg)
                return True, msg
            pname = self.get_process_name(pid) if pid else "unknown"
            err = f"Port 3000 conflict: already occupied by external process PID {pid} ({pname}). Nexus will not terminate external processes."
            log(err)
            return False, err

        vite_js = self.frontend_dir / "node_modules" / "vite" / "bin" / "vite.js"
        node_exe = shutil.which("node.exe") or shutil.which("node")
        if node_exe and vite_js.exists():
            cmd = [str(node_exe), str(vite_js)]
        else:
            npm_cmd = shutil.which("npm.cmd") or shutil.which("npm") or r"C:\Program Files\nodejs\npm.cmd"
            if not Path(npm_cmd).exists() and not shutil.which("npm"):
                err = "Node/npm executable not found in PATH."
                log(err)
                return False, err
            cmd = [str(npm_cmd), "run", "dev"]

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        frontend_log_path = self.logs_dir / "frontend.log"
        frontend_log_file = open(frontend_log_path, "a", encoding="utf-8")
        frontend_log_file.write(f"\n--- NEXUS STARTUP AT {datetime.now(timezone.utc).isoformat()} ---\n")
        frontend_log_file.flush()

        log(f"Launching Vite frontend: {' '.join(cmd)}")

        extra_flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.frontend_dir),
                stdout=frontend_log_file,
                stderr=subprocess.STDOUT,
                shell=False,
                **_hidden_subprocess_kwargs(extra_flags=extra_flags),
            )
        finally:
            frontend_log_file.close()

        state = self.load_state()
        state["frontend"] = {
            "pid": proc.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "port": 3000,
            "command": " ".join(cmd),
        }
        self.save_state(state)
        log(f"Vite frontend spawned (PID {proc.pid}). Waiting for http://localhost:3000...")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = f"Vite frontend terminated prematurely (exit code: {proc.returncode}). Check .nexus/logs/frontend.log."
                log(err)
                state = self.load_state()
                state.pop("frontend", None)
                self.save_state(state)
                return False, err

            fe_ok, _, fe_meta = self.check_frontend()
            if fe_ok and fe_meta.get("http_healthy"):
                listener_pid = fe_meta.get("pid") or self.get_pid_listening_on_port(3000)
                if listener_pid:
                    state = self.load_state()
                    if "frontend" in state:
                        state["frontend"]["listener_pid"] = listener_pid
                        self.save_state(state)
                log(f"Vite frontend healthy at {DASHBOARD_URL} (Root PID: {proc.pid}, Listener PID: {listener_pid}).")
                if fe_meta.get("proxy_verified"):
                    log("Vite API reverse proxy verified (http://localhost:3000/api -> http://localhost:8000).")
                return True, f"Frontend running (PID {listener_pid or proc.pid})"

            time.sleep(0.4)

        err = f"Frontend health check timed out after {int(timeout_seconds)}s. Check .nexus/logs/frontend.log."
        log(err)
        self.stop_service("frontend", log_callback=log_callback)
        return False, err

    def start_all(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """One-click sequential startup workflow with preflight checks and health verification."""
        def log(msg: str):
            self._log("nexus.log", msg)
            if log_callback:
                log_callback(msg)

        def progress(pct: int, text: str):
            if progress_callback:
                progress_callback(pct, text)

        log("=" * 60)
        log("NEXUS: Starting Complete Local Environment...")
        log("=" * 60)

        # Step 1: PostgreSQL
        progress(15, "Step 1/5: Checking PostgreSQL (Port 5432)...")
        log("Step 1/5: Checking PostgreSQL (Port 5432)...")
        pg_ok, pg_status, _ = self.check_postgres()
        if not pg_ok:
            log(f"ABORT: {pg_status}")
            progress(15, f"Failed: {pg_status}")
            return {"success": False, "step": "postgres", "error": pg_status}
        log(f"PostgreSQL verified: {pg_status}")

        # Step 2: Redis (Optional)
        progress(25, "Step 2/5: Detecting Redis (Port 6379, optional)...")
        log("Step 2/5: Checking Redis (Port 6379)...")
        redis_ok, redis_status, redis_meta = self.check_redis()
        log(f"Redis status: {redis_status}")
        if not redis_ok and redis_meta.get("redis_required"):
            progress(25, f"Failed: {redis_status}")
            return {"success": False, "step": "redis", "error": redis_status}

        # Step 3: FastAPI Backend
        progress(45, "Step 3/5: Starting FastAPI Backend (Port 8000)...")
        log("Step 3/5: Starting FastAPI Backend (Port 8000)...")
        be_ok, be_msg = self.start_backend(log_callback=log_callback)
        if not be_ok:
            log(f"Backend startup failed: {be_msg}")
            progress(45, "Failed: Backend startup error")
            return {"success": False, "step": "backend", "error": be_msg}

        # Step 4: Vite Frontend
        progress(75, "Step 4/5: Starting React + Vite Frontend (Port 3000)...")
        log("Step 4/5: Starting React + Vite Frontend (Port 3000)...")
        fe_ok, fe_msg = self.start_frontend(log_callback=log_callback)
        if not fe_ok:
            log(f"Frontend startup failed: {fe_msg}")
            progress(75, "Failed: Frontend startup error")
            return {"success": False, "step": "frontend", "error": fe_msg}

        # Step 5: Final Proxy Verification
        progress(92, "Step 5/5: Verifying API Proxy & Health Checks...")
        log("Step 5/5: Verifying Vite API Proxy...")
        proxy_ok = self.check_proxy()
        log(f"Proxy communication: {'VERIFIED' if proxy_ok else 'PENDING'}")

        progress(100, "All required local services are Running.")
        log("=" * 60)
        log("SUCCESS: All required local services are online and operational.")
        log("=" * 60)

        return {
            "success": True,
            "postgres": pg_status,
            "redis": redis_status,
            "backend": be_msg,
            "frontend": fe_msg,
            "proxy_verified": proxy_ok,
        }

    # --- Stop Operations ---

    def _terminate_pid_tree(self, pid: int, log: Callable[[str], None]) -> None:
        """Safely terminates a process tree by PID."""
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, timeout=4, **_hidden_subprocess_kwargs())
                time.sleep(0.8)
                if self.is_pid_alive(pid):
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=4, **_hidden_subprocess_kwargs())
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.8)
                if self.is_pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
        except Exception as e:
            log(f"Error terminating PID {pid}: {e}")

    def stop_service(self, service: str, log_callback=None) -> Tuple[bool, str]:
        """Safely stops only Nexus-owned process for a service."""
        def log(msg: str):
            self._log("nexus.log", msg)
            if log_callback:
                log_callback(msg)

        state = self.load_state()
        svc_state = state.get(service)
        if not svc_state or "pid" not in svc_state:
            msg = f"No Nexus-owned process recorded for {service}."
            log(msg)
            return True, msg

        pid = svc_state["pid"]
        listener_pid = svc_state.get("listener_pid")
        log(f"Stopping Nexus-owned {service} process (PID: {pid})...")

        if not self.is_pid_alive(pid):
            log(f"Root process PID {pid} is already terminated.")
        else:
            self._terminate_pid_tree(pid, log)

        # If a known owned child listener survived root tree exit, stop it only if verified alive
        if listener_pid and listener_pid != pid and self.is_pid_alive(listener_pid):
            expected_images = ("python", "uvicorn", "node", "vite")
            pname = self.get_process_name(listener_pid).lower()
            if any(tok in pname for tok in expected_images) or pname == "unknown":
                log(f"Stopping remaining owned {service} listener (PID: {listener_pid})...")
                self._terminate_pid_tree(listener_pid, log)

        state.pop(service, None)
        self.save_state(state)
        log(f"Successfully stopped {service} (PID: {pid}).")
        return True, f"Stopped {service}"

    def stop_all(self, log_callback=None, progress_callback=None) -> Dict[str, Any]:
        """Stops all Nexus-owned processes (Frontend & Backend). Never touches PostgreSQL or Redis."""
        def log(msg: str):
            self._log("nexus.log", msg)
            if log_callback:
                log_callback(msg)

        if progress_callback:
            progress_callback(40, "Stopping owned Frontend & Backend services...")
        log("NEXUS: Stopping all owned local services...")
        _, fe_msg = self.stop_service("frontend", log_callback=log_callback)
        _, be_msg = self.stop_service("backend", log_callback=log_callback)
        log("NEXUS: Service stop completed. PostgreSQL and Redis remained untouched.")
        if progress_callback:
            progress_callback(0, "Stopped — Owned services terminated.")

        return {"frontend": fe_msg, "backend": be_msg}

    def restart_all(self, log_callback=None, progress_callback=None) -> Dict[str, Any]:
        """Safely stops owned services, awaits port release, and restarts."""
        def log(msg: str):
            self._log("nexus.log", msg)
            if log_callback:
                log_callback(msg)

        if progress_callback:
            progress_callback(10, "Stopping existing Nexus-owned services...")
        log("NEXUS: Restarting all local services...")
        self.stop_all(log_callback=log_callback)

        log("Awaiting port release...")
        for _ in range(10):
            if not self.is_port_in_use(8000) and not self.is_port_in_use(3000):
                break
            time.sleep(0.5)

        return self.start_all(log_callback=log_callback, progress_callback=progress_callback)

    def read_sanitized_log_tail(self, filename: str, max_lines: int = 200) -> str:
        """Reads and sanitizes the tail of a log file inside .nexus/logs/."""
        target = (self.logs_dir / filename).resolve()
        if not target.is_relative_to(self.logs_dir.resolve()) or not target.exists():
            return f"Log file '{filename}' does not exist yet."
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(sanitize_message(line) for line in lines[-max_lines:])
        except Exception as e:
            return f"Unable to read log file '{filename}': {e}"


# ==============================================================================
# 4. Windows Desktop Shortcut Integration
# ==============================================================================

def create_desktop_shortcut(project_root: Optional[Path] = None) -> Tuple[bool, str]:
    """Generates a console-free NEXUS desktop shortcut on Windows."""
    root = project_root or PROJECT_ROOT
    pythonw_exe = root / ".venv" / "Scripts" / "pythonw.exe"
    nexus_py = root / "nexus.py"
    bat_path = root / "NEXUS.bat"
    icon_path = ICON_PATH

    desktop = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop"
    if not desktop.exists():
        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"

    if not desktop.exists():
        return False, "Windows Desktop directory could not be located."

    shortcut_path = desktop / "NEXUS.lnk"
    # Launch pythonw.exe directly to eliminate the initial CMD flash from .bat wrappers
    if pythonw_exe.exists() and nexus_py.exists():
        target_path = str(pythonw_exe)
        arguments = f'"{nexus_py}"'
        window_style = 1
    else:
        target_path = str(bat_path)
        arguments = ""
        window_style = 7  # Minimized fallback

    ps_cmd = f"""
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut('{str(shortcut_path)}')
    $Shortcut.TargetPath = '{target_path}'
    $Shortcut.Arguments = '{arguments}'
    $Shortcut.WorkingDirectory = '{str(root)}'
    $Shortcut.WindowStyle = {window_style}
    $Shortcut.Description = 'NEXUS - One-Click Local Control Center'
    if (Test-Path '{str(icon_path)}') {{
        $Shortcut.IconLocation = '{str(icon_path)}, 0'
    }}
    $Shortcut.Save()
    """

    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, **_hidden_subprocess_kwargs())
        if res.returncode == 0 and shortcut_path.exists():
            return True, str(shortcut_path)
        return False, f"PowerShell shortcut creation failed: {res.stderr.strip()}"
    except Exception as e:
        return False, f"Error creating shortcut: {e}"


# ==============================================================================
# 5. Desktop Control Center GUI (Dark Navy & Cyan)
# ==============================================================================

def run_gui(auto_start: bool = True):
    """Launches the compact Dark Navy & Cyan NEXUS Desktop Control Center."""
    import tkinter as tk

    core = NexusCore()

    # Palette (Dark Navy & Cyan Identity)
    BG_ROOT = "#060a13"
    BG_CARD = "#0f172a"
    BG_INNER = "#162033"
    BORDER_SUBTLE = "#24324a"
    ACCENT_CYAN = "#06b6d4"
    ACCENT_CYAN_SOFT = "#22d3ee"
    ACCENT_BLUE = "#3b82f6"
    ACCENT_EMERALD = "#10b981"
    ACCENT_AMBER = "#f59e0b"
    ACCENT_ROSE = "#f43f5e"
    TEXT_PRIMARY = "#f8fafc"
    TEXT_SECONDARY = "#94a3b8"
    TEXT_MUTED = "#64748b"
    CONSOLE_BG = "#03060d"

    root = tk.Tk()
    root.title("NEXUS — Desktop Control Center")
    root.geometry("820x630")
    root.minsize(760, 580)
    root.configure(bg=BG_ROOT)

    if ICON_PATH.exists():
        try:
            root.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

    state_lock = threading.Lock()
    op_state = {"busy": False, "starting_services": set(), "failed_services": {}, "last_error": None}
    closing = threading.Event()
    poll_after_id = [None]

    def safe_after(ms: int, fn: Callable[[], None]):
        if closing.is_set():
            return None
        try:
            return root.after(ms, lambda: None if closing.is_set() else fn())
        except (RuntimeError, tk.TclError):
            return None

    status_vars = {
        "postgres": {"status": tk.StringVar(value="Starting"), "port": tk.StringVar(value="Port 5432 · Probing...")},
        "backend": {"status": tk.StringVar(value="Stopped"), "port": tk.StringVar(value="Port 8000 · Idle")},
        "frontend": {"status": tk.StringVar(value="Stopped"), "port": tk.StringVar(value="Port 3000 · Idle")},
        "redis": {"status": tk.StringVar(value="Stopped"), "port": tk.StringVar(value="Port 6379 · Optional")},
    }
    progress_text_var = tk.StringVar(value="Initializing NEXUS Control Center...")
    progress_pct_var = tk.StringVar(value="0%")
    last_error_var = tk.StringVar(value="None")

    card_dots: Dict[str, Tuple[tk.Canvas, int]] = {}
    status_labels: Dict[str, tk.Label] = {}

    # --- Header with Vector NEXUS Emblem ---
    header = tk.Frame(root, bg=BG_ROOT, padx=22, pady=14)
    header.pack(fill="x")

    logo_canvas = tk.Canvas(header, width=36, height=36, bg=BG_ROOT, highlightthickness=0)
    logo_canvas.pack(side="left", padx=(0, 10))
    # Hexagon emblem + cyan core
    logo_canvas.create_polygon(18, 2, 33, 10, 33, 26, 18, 34, 3, 26, 3, 10, outline=ACCENT_CYAN, fill=BG_CARD, width=2)
    logo_canvas.create_oval(12, 12, 24, 24, fill=ACCENT_CYAN, outline="")
    logo_canvas.create_oval(15, 15, 21, 21, fill=BG_ROOT, outline="")

    title_stack = tk.Frame(header, bg=BG_ROOT)
    title_stack.pack(side="left")

    title_row = tk.Frame(title_stack, bg=BG_ROOT)
    title_row.pack(anchor="w")
    tk.Label(title_row, text="NEXUS", font=("Segoe UI", 18, "bold"), fg=ACCENT_CYAN_SOFT, bg=BG_ROOT).pack(side="left", padx=(0, 8))
    tk.Label(title_row, text="CONTROL CENTER", font=("Segoe UI", 7, "bold"), fg="#042f2e", bg=ACCENT_CYAN, padx=7, pady=2).pack(side="left", pady=3)

    tk.Label(
        title_stack,
        text="AI Customer Support Platform · Local Development Runtime",
        font=("Segoe UI", 8),
        fg=TEXT_SECONDARY,
        bg=BG_ROOT,
    ).pack(anchor="w")

    telemetry_lbl = tk.Label(header, text="● Telemetry Active", font=("Segoe UI", 8, "bold"), fg=ACCENT_EMERALD, bg=BG_ROOT)
    telemetry_lbl.pack(side="right", pady=6)

    # --- Service Status Grid (4 Cards) ---
    grid = tk.Frame(root, bg=BG_ROOT, padx=22)
    grid.pack(fill="x", pady=(0, 10))

    services_meta = [
        ("postgres", "PostgreSQL", "Required Database"),
        ("backend", "FastAPI Backend", "Python Uvicorn API"),
        ("frontend", "React + Vite", "Dashboard Web App"),
        ("redis", "Redis (Optional)", "Cache · Dev Fallback"),
    ]

    for col, (key, title, role_desc) in enumerate(services_meta):
        card = tk.Frame(grid, bg=BG_CARD, highlightbackground=BORDER_SUBTLE, highlightthickness=1, padx=12, pady=10)
        card.grid(row=0, column=col, sticky="nsew", padx=4)
        grid.grid_columnconfigure(col, weight=1)

        top = tk.Frame(card, bg=BG_CARD)
        top.pack(fill="x", pady=(0, 4))

        dot = tk.Canvas(top, width=10, height=10, bg=BG_CARD, highlightthickness=0)
        dot.pack(side="left", padx=(0, 6))
        dot_id = dot.create_oval(1, 1, 9, 9, fill=TEXT_MUTED, outline="")
        card_dots[key] = (dot, dot_id)

        tk.Label(top, text=title, font=("Segoe UI", 9, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(side="left")

        s_lbl = tk.Label(card, textvariable=status_vars[key]["status"], font=("Segoe UI", 10, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD, anchor="w")
        s_lbl.pack(fill="x")
        status_labels[key] = s_lbl

        tk.Label(card, textvariable=status_vars[key]["port"], font=("Segoe UI", 7), fg=TEXT_SECONDARY, bg=BG_CARD, anchor="w").pack(fill="x", pady=(2, 0))
        tk.Label(card, text=role_desc, font=("Segoe UI", 7), fg=TEXT_MUTED, bg=BG_CARD, anchor="w").pack(fill="x")

    # --- Startup Progress & Last Error Card ---
    status_panel = tk.Frame(root, bg=BG_CARD, highlightbackground=BORDER_SUBTLE, highlightthickness=1, padx=14, pady=10)
    status_panel.pack(fill="x", padx=26, pady=(0, 10))

    prog_hdr = tk.Frame(status_panel, bg=BG_CARD)
    prog_hdr.pack(fill="x", pady=(0, 5))
    tk.Label(prog_hdr, text="STARTUP PROGRESS", font=("Segoe UI", 7, "bold"), fg=ACCENT_CYAN_SOFT, bg=BG_CARD).pack(side="left")
    tk.Label(prog_hdr, textvariable=progress_pct_var, font=("Consolas", 8, "bold"), fg=ACCENT_CYAN_SOFT, bg=BG_CARD).pack(side="right")

    prog_step_lbl = tk.Label(status_panel, textvariable=progress_text_var, font=("Segoe UI", 8), fg=TEXT_PRIMARY, bg=BG_CARD, anchor="w")
    prog_step_lbl.pack(fill="x", pady=(0, 5))

    prog_canvas = tk.Canvas(status_panel, height=8, bg=BG_INNER, highlightthickness=0)
    prog_canvas.pack(fill="x")
    prog_fill_rect = prog_canvas.create_rectangle(0, 0, 0, 8, fill=ACCENT_CYAN, outline="")

    def update_progress_bar(pct: int, text: str, error_mode: bool = False):
        def _apply():
            clamped = max(0, min(100, int(pct)))
            progress_pct_var.set(f"{clamped}%")
            progress_text_var.set(text)
            w = max(prog_canvas.winfo_width(), 720)
            fill_w = int((w * clamped) / 100)
            prog_canvas.coords(prog_fill_rect, 0, 0, fill_w, 8)
            prog_canvas.itemconfig(prog_fill_rect, fill=ACCENT_ROSE if error_mode else (ACCENT_EMERALD if clamped == 100 else ACCENT_CYAN))
        safe_after(0, _apply)

    err_row = tk.Frame(status_panel, bg=BG_CARD)
    err_row.pack(fill="x", pady=(8, 0))
    tk.Label(err_row, text="LAST ERROR:", font=("Segoe UI", 7, "bold"), fg=TEXT_MUTED, bg=BG_CARD).pack(side="left", padx=(0, 6))
    err_val_lbl = tk.Label(err_row, textvariable=last_error_var, font=("Segoe UI", 8), fg=TEXT_MUTED, bg=BG_CARD, anchor="w", justify="left", wraplength=660)
    err_val_lbl.pack(side="left", fill="x", expand=True)

    def set_last_error(msg: Optional[str]):
        def _apply():
            if msg:
                last_error_var.set(sanitize_message(msg))
                err_val_lbl.config(fg=ACCENT_ROSE)
            else:
                last_error_var.set("None")
                err_val_lbl.config(fg=TEXT_MUTED)
        safe_after(0, _apply)

    # --- Controls Bar (5 Required Action Buttons) ---
    ctrl = tk.Frame(root, bg=BG_ROOT, padx=26, pady=4)
    ctrl.pack(fill="x")

    def make_btn(parent, text, cmd, bg_col, fg_col="#ffffff", bold=True):
        return tk.Button(
            parent,
            text=text,
            command=cmd,
            bg=bg_col,
            fg=fg_col,
            font=("Segoe UI", 9, "bold" if bold else "normal"),
            activebackground=BORDER_SUBTLE,
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
        )

    left_btns = tk.Frame(ctrl, bg=BG_ROOT)
    left_btns.pack(side="left")

    right_btns = tk.Frame(ctrl, bg=BG_ROOT)
    right_btns.pack(side="right")

    # --- Telemetry Console ---
    console_wrap = tk.Frame(root, bg=BG_ROOT, padx=26, pady=8)
    console_wrap.pack(fill="both", expand=True)

    c_hdr = tk.Frame(console_wrap, bg=BG_ROOT)
    c_hdr.pack(fill="x", pady=(0, 4))
    tk.Label(c_hdr, text="DIAGNOSTICS & TELEMETRY STREAM", font=("Segoe UI", 7, "bold"), fg=TEXT_MUTED, bg=BG_ROOT).pack(side="left")

    console_txt = tk.Text(
        console_wrap,
        bg=CONSOLE_BG,
        fg="#38bdf8",
        insertbackground="#38bdf8",
        font=("Consolas", 8),
        relief="flat",
        highlightbackground=BORDER_SUBTLE,
        highlightthickness=1,
        padx=10,
        pady=8,
    )
    tk.Button(c_hdr, text="Clear", command=lambda: console_txt.delete("1.0", tk.END), font=("Segoe UI", 7), bg=BG_ROOT, fg=TEXT_MUTED, relief="flat", cursor="hand2").pack(side="right")
    console_txt.pack(fill="both", expand=True)

    def log_to_console(message: str):
        clean = sanitize_message(message)
        def _append():
            ts = datetime.now().strftime("%H:%M:%S")
            console_txt.insert(tk.END, f"[{ts}] {clean}\n")
            console_txt.see(tk.END)
        safe_after(0, _append)

    # --- Log Viewer Modal ---
    def open_logs_modal():
        win = tk.Toplevel(root)
        win.title("NEXUS — Sanitized Log Viewer")
        win.geometry("760x500")
        win.configure(bg=BG_ROOT)
        if ICON_PATH.exists():
            try:
                win.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

        top_bar = tk.Frame(win, bg=BG_CARD, padx=14, pady=8)
        top_bar.pack(fill="x")

        selected_file = tk.StringVar(value="nexus.log")
        log_files = ["nexus.log", "backend.log", "frontend.log", "postgres_check.log", "redis_check.log"]

        text_box = tk.Text(win, bg=CONSOLE_BG, fg=TEXT_PRIMARY, font=("Consolas", 8), relief="flat", padx=10, pady=10)
        text_box.pack(fill="both", expand=True, padx=14, pady=10)

        def load_selected():
            content = core.read_sanitized_log_tail(selected_file.get(), max_lines=250)
            text_box.delete("1.0", tk.END)
            text_box.insert("1.0", content)
            text_box.see(tk.END)

        for lf in log_files:
            tk.Radiobutton(
                top_bar,
                text=lf,
                variable=selected_file,
                value=lf,
                command=load_selected,
                bg=BG_CARD,
                fg=ACCENT_CYAN_SOFT,
                selectcolor=BG_INNER,
                activebackground=BG_CARD,
                activeforeground=TEXT_PRIMARY,
                font=("Segoe UI", 8, "bold"),
            ).pack(side="left", padx=(0, 8))

        def open_dir():
            if sys.platform == "win32":
                os.startfile(str(core.logs_dir))
            else:
                webbrowser.open(f"file://{core.logs_dir.resolve()}")

        make_btn(top_bar, "Refresh", load_selected, BG_INNER, fg_col=TEXT_PRIMARY, bold=False).pack(side="right", padx=(6, 0))
        make_btn(top_bar, "Open Folder", open_dir, BG_INNER, fg_col=ACCENT_CYAN_SOFT, bold=False).pack(side="right")
        load_selected()

    # --- Dashboard Action ---
    def handle_open_dashboard():
        def _run():
            fe_ok, _, fe_meta = core.check_frontend()
            if not (fe_ok and fe_meta.get("http_healthy")):
                err = "Dashboard is not running on http://localhost:3000. Click START ALL first."
                set_last_error(err)
                log_to_console(err)
                return
            set_last_error(None)
            ok, msg = core.open_dashboard(DASHBOARD_URL)
            log_to_console(msg if ok else f"Browser launch warning: {msg}")
        threading.Thread(target=_run, daemon=True).start()

    # --- Health Card Rendering ---
    def apply_status_cards(statuses: Dict[str, Any]):
        with state_lock:
            starting = set(op_state["starting_services"])
            failed = dict(op_state["failed_services"])

        # PostgreSQL
        pg = statuses["postgres"]
        pg_meta = pg["meta"]
        if pg["healthy"]:
            status_vars["postgres"]["status"].set("Running")
            status_vars["postgres"]["port"].set(f"Port 5432 · PID {pg_meta.get('pid') or 'Active'}")
            card_dots["postgres"][0].itemconfig(card_dots["postgres"][1], fill=ACCENT_EMERALD)
            status_labels["postgres"].config(fg=ACCENT_EMERALD)
        else:
            status_vars["postgres"]["status"].set("Failed")
            status_vars["postgres"]["port"].set("Port 5432 · Offline")
            card_dots["postgres"][0].itemconfig(card_dots["postgres"][1], fill=ACCENT_ROSE)
            status_labels["postgres"].config(fg=ACCENT_ROSE)

        # Redis (Optional)
        rd = statuses["redis"]
        if rd["healthy"]:
            status_vars["redis"]["status"].set("Running")
            status_vars["redis"]["port"].set("Port 6379 · Connected")
            card_dots["redis"][0].itemconfig(card_dots["redis"][1], fill=ACCENT_EMERALD)
            status_labels["redis"].config(fg=ACCENT_EMERALD)
        elif "optional" in rd["status"].lower():
            status_vars["redis"]["status"].set("Stopped (Optional)")
            status_vars["redis"]["port"].set("Port 6379 · Dev Fallback")
            card_dots["redis"][0].itemconfig(card_dots["redis"][1], fill=ACCENT_AMBER)
            status_labels["redis"].config(fg=ACCENT_AMBER)
        else:
            status_vars["redis"]["status"].set("Failed")
            status_vars["redis"]["port"].set("Port 6379 · Required")
            card_dots["redis"][0].itemconfig(card_dots["redis"][1], fill=ACCENT_ROSE)
            status_labels["redis"].config(fg=ACCENT_ROSE)

        # Backend & Frontend
        for key, port in (("backend", 8000), ("frontend", 3000)):
            svc = statuses[key]
            meta = svc["meta"]
            if key in failed:
                status_vars[key]["status"].set("Failed")
                status_vars[key]["port"].set(f"Port {port} · Error")
                card_dots[key][0].itemconfig(card_dots[key][1], fill=ACCENT_ROSE)
                status_labels[key].config(fg=ACCENT_ROSE)
            elif key in starting:
                status_vars[key]["status"].set("Starting")
                status_vars[key]["port"].set(f"Port {port} · Probing...")
                card_dots[key][0].itemconfig(card_dots[key][1], fill=ACCENT_CYAN)
                status_labels[key].config(fg=ACCENT_CYAN_SOFT)
            elif svc["healthy"] and meta.get("http_healthy"):
                owner = "Managed" if meta.get("is_owned") else "External"
                pid_txt = f"PID {meta.get('pid')}" if meta.get("pid") else "Active"
                status_vars[key]["status"].set("Running")
                status_vars[key]["port"].set(f"Port {port} · {pid_txt} ({owner})")
                card_dots[key][0].itemconfig(card_dots[key][1], fill=ACCENT_EMERALD)
                status_labels[key].config(fg=ACCENT_EMERALD)
            elif svc["healthy"]:
                status_vars[key]["status"].set("Starting")
                status_vars[key]["port"].set(f"Port {port} · Booting")
                card_dots[key][0].itemconfig(card_dots[key][1], fill=ACCENT_AMBER)
                status_labels[key].config(fg=ACCENT_AMBER)
            else:
                status_vars[key]["status"].set("Stopped")
                status_vars[key]["port"].set(f"Port {port} · Idle")
                card_dots[key][0].itemconfig(card_dots[key][1], fill=TEXT_MUTED)
                status_labels[key].config(fg=TEXT_SECONDARY)

    def refresh_health():
        if closing.is_set():
            return
        def _check():
            statuses = core.get_all_statuses()
            safe_after(0, lambda: apply_status_cards(statuses))
        threading.Thread(target=_check, daemon=True).start()

    def set_buttons_state(state_val: str):
        def _apply():
            btn_start.config(state=state_val)
            btn_stop.config(state=state_val)
            btn_restart.config(state=state_val)
        safe_after(0, _apply)

    def run_start_workflow(open_browser_on_success: bool = True):
        with state_lock:
            if op_state["busy"]:
                return
            op_state["busy"] = True
            op_state["failed_services"].clear()
            op_state["starting_services"] = {"backend", "frontend"}
            op_state["last_error"] = None

        set_last_error(None)
        set_buttons_state("disabled")
        refresh_health()

        def worker():
            try:
                res = core.start_all(log_callback=log_to_console, progress_callback=update_progress_bar)
                with state_lock:
                    op_state["starting_services"].clear()
                    if not res.get("success"):
                        step = res.get("step", "startup")
                        err = res.get("error", "Unknown startup failure.")
                        if step in ("backend", "frontend", "postgres", "redis"):
                            op_state["failed_services"][step] = err
                        op_state["last_error"] = err
                if res.get("success") and core.are_platform_services_ready():
                    set_last_error(None)
                    update_progress_bar(100, "Running — Platform ready at http://localhost:3000")
                    if open_browser_on_success:
                        core.open_dashboard(DASHBOARD_URL)
                else:
                    err_msg = res.get("error") or "Health check verification failed. Check logs."
                    set_last_error(err_msg)
                    update_progress_bar(0, f"Startup Failed ({res.get('step', 'health')})", error_mode=True)
                    log_to_console(f"Browser NOT opened because startup failed: {err_msg}")
            except Exception as e:
                err_msg = str(e)
                with state_lock:
                    op_state["starting_services"].clear()
                    op_state["last_error"] = err_msg
                set_last_error(err_msg)
                update_progress_bar(0, "Startup Failed (unexpected error)", error_mode=True)
                log_to_console(f"ERROR: {err_msg}")
            finally:
                with state_lock:
                    op_state["busy"] = False
                set_buttons_state("normal")
                refresh_health()

        threading.Thread(target=worker, daemon=True).start()

    def run_stop_workflow():
        with state_lock:
            if op_state["busy"]:
                return
            op_state["busy"] = True
            op_state["failed_services"].clear()
            op_state["starting_services"].clear()

        set_buttons_state("disabled")

        def worker():
            try:
                core.stop_all(log_callback=log_to_console, progress_callback=update_progress_bar)
                set_last_error(None)
            except Exception as e:
                set_last_error(str(e))
                log_to_console(f"ERROR: {e}")
            finally:
                with state_lock:
                    op_state["busy"] = False
                set_buttons_state("normal")
                refresh_health()

        threading.Thread(target=worker, daemon=True).start()

    def run_restart_workflow():
        with state_lock:
            if op_state["busy"]:
                return
            op_state["busy"] = True
            op_state["failed_services"].clear()
            op_state["starting_services"] = {"backend", "frontend"}

        set_last_error(None)
        set_buttons_state("disabled")
        refresh_health()

        def worker():
            try:
                res = core.restart_all(log_callback=log_to_console, progress_callback=update_progress_bar)
                with state_lock:
                    op_state["starting_services"].clear()
                    if not res.get("success"):
                        step = res.get("step", "restart")
                        op_state["failed_services"][step] = res.get("error", "Restart failed.")
                if res.get("success") and core.are_platform_services_ready():
                    set_last_error(None)
                    update_progress_bar(100, "Running — Restart completed successfully.")
                else:
                    err_msg = res.get("error") or "Restart health check failed."
                    set_last_error(err_msg)
                    update_progress_bar(0, "Restart Failed", error_mode=True)
            except Exception as e:
                set_last_error(str(e))
                update_progress_bar(0, "Restart Failed", error_mode=True)
            finally:
                with state_lock:
                    op_state["busy"] = False
                set_buttons_state("normal")
                refresh_health()

        threading.Thread(target=worker, daemon=True).start()

    btn_start = make_btn(left_btns, "▶ START ALL", lambda: run_start_workflow(open_browser_on_success=True), ACCENT_CYAN, fg_col="#042f2e")
    btn_start.pack(side="left", padx=(0, 6))

    btn_stop = make_btn(left_btns, "■ STOP ALL", run_stop_workflow, "#be123c")
    btn_stop.pack(side="left", padx=(0, 6))

    btn_restart = make_btn(left_btns, "↻ RESTART ALL", run_restart_workflow, "#d97706")
    btn_restart.pack(side="left", padx=(0, 6))

    btn_dash = make_btn(right_btns, "🌐 OPEN DASHBOARD", handle_open_dashboard, ACCENT_BLUE)
    btn_dash.pack(side="left", padx=(0, 6))

    btn_logs = make_btn(right_btns, "📄 VIEW LOGS", open_logs_modal, BG_INNER, fg_col=ACCENT_CYAN_SOFT, bold=True)
    btn_logs.pack(side="left")

    def initial_boot_sequence():
        def _probe_and_boot():
            update_progress_bar(10, "Probing local environment...")
            statuses = core.get_all_statuses()
            safe_after(0, lambda: apply_status_cards(statuses))
            be_ready = statuses["backend"]["healthy"] and statuses["backend"]["meta"].get("http_healthy")
            fe_ready = statuses["frontend"]["healthy"] and statuses["frontend"]["meta"].get("http_healthy")
            pg_ready = statuses["postgres"]["healthy"]

            if pg_ready and be_ready and fe_ready:
                log_to_console("Existing healthy services detected on ports 8000 & 3000.")
                update_progress_bar(100, "Running — Detected healthy services, opening dashboard...")
                set_last_error(None)
                core.open_dashboard(DASHBOARD_URL)
                return

            if auto_start:
                safe_after(0, lambda: run_start_workflow(open_browser_on_success=True))
            else:
                update_progress_bar(0, "Idle — Ready to start services.")

        threading.Thread(target=_probe_and_boot, daemon=True).start()

    def poll_loop():
        if closing.is_set():
            return
        refresh_health()
        poll_after_id[0] = safe_after(3000, poll_loop)

    def on_close():
        # Closing the UI must leave NEXUS-managed backend/frontend processes running.
        closing.set()
        if poll_after_id[0] is not None:
            try:
                root.after_cancel(poll_after_id[0])
            except Exception:
                pass
        core._log("nexus.log", "NEXUS Control Center window closed. Background services untouched.")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    log_to_console("NEXUS Desktop Control Center initialized.")
    log_to_console(f"Project root: {PROJECT_ROOT}")
    safe_after(150, initial_boot_sequence)
    poll_after_id[0] = safe_after(3200, poll_loop)
    root.mainloop()


# ==============================================================================
# 6. CLI Entrypoint & Argument Handling
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="NEXUS: One-Click Local Development Control Center for AI Customer Support Platform."
    )
    parser.add_argument("--start", action="store_true", help="Start all missing required services.")
    parser.add_argument("--stop", action="store_true", help="Stop all Nexus-owned local services.")
    parser.add_argument("--restart", action="store_true", help="Restart all Nexus-owned local services.")
    parser.add_argument("--status", action="store_true", help="Display comprehensive health matrix.")
    parser.add_argument("--create-shortcut", action="store_true", help="Create Windows Desktop shortcut.")
    parser.add_argument("--gui", action="store_true", help="Force launch the Tkinter graphical interface.")
    parser.add_argument("--no-auto-start", action="store_true", help="Open GUI without automatically running START ALL.")

    args = parser.parse_args()
    core = NexusCore()

    if args.status:
        statuses = core.get_all_statuses()
        print("\n" + "=" * 65)
        print(" NEXUS — SERVICE HEALTH MATRIX")
        print("=" * 65)
        for svc in ["postgres", "redis", "backend", "frontend"]:
            s = statuses[svc]
            status_icon = "✔" if s["healthy"] or ("optional" in s["status"].lower()) else "✖"
            print(f" [{status_icon}] {svc.upper():<12} : {s['status']}")
            for k, v in s["meta"].items():
                print(f"     └─ {k}: {v}")
        print("=" * 65 + "\n")
        return

    if args.create_shortcut:
        ok, msg = create_desktop_shortcut()
        if ok:
            print(f"[SUCCESS] Desktop shortcut created: {msg}")
        else:
            print(f"[ERROR] Failed to create shortcut: {msg}")
            sys.exit(1)
        return

    if args.start:
        res = core.start_all(log_callback=print)
        sys.exit(0 if res["success"] else 1)

    if args.stop:
        res = core.stop_all(log_callback=print)
        print(f"[SUCCESS] Stopped services: {res}")
        return

    if args.restart:
        res = core.restart_all(log_callback=print)
        sys.exit(0 if res.get("success") else 1)

    try:
        run_gui(auto_start=not args.no_auto_start)
    except Exception as e:
        print(f"GUI launch failed: {e}. Falling back to CLI status.")
        core.get_all_statuses()


if __name__ == "__main__":
    main()
