from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import psutil

from .config import AppSpec

STATE_DIR = Path(os.environ.get("LOCALCTL_STATE", Path.home() / ".localctl"))
# Rotate a log file to <name>.old once it grows past this size (single-level rotation,
# so disk usage caps at ~2x this per stream). Override via LOCALCTL_LOG_MAX_BYTES.
LOG_MAX_BYTES = int(os.environ.get("LOCALCTL_LOG_MAX_BYTES", str(5 * 1024 * 1024)))


def _app_dir(name: str) -> Path:
    d = STATE_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file(name: str) -> Path:
    return _app_dir(name) / "pid.json"


def _log_files(name: str) -> tuple[Path, Path]:
    d = _app_dir(name)
    return d / "stdout.log", d / "stderr.log"


@dataclass
class RunState:
    pid: int
    started_at: float
    cmd: list[str]
    port: int | None = None
    # OS-reported process creation time — pinned to detect PID reuse.
    create_time: float | None = None


def _read_state(name: str) -> RunState | None:
    pf = _pid_file(name)
    if not pf.exists():
        return None
    try:
        data = json.loads(pf.read_text())
        return RunState(
            pid=data["pid"],
            started_at=data["started_at"],
            cmd=data["cmd"],
            port=data.get("port"),
            create_time=data.get("create_time"),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def _write_state(name: str, state: RunState) -> None:
    _pid_file(name).write_text(
        json.dumps({
            "pid": state.pid,
            "started_at": state.started_at,
            "cmd": state.cmd,
            "port": state.port,
            "create_time": state.create_time,
        })
    )


def _clear_state(name: str) -> None:
    pf = _pid_file(name)
    if pf.exists():
        pf.unlink()


def _verify_proc(state: RunState) -> psutil.Process | None:
    """Return the live process matching `state`, or None.

    Guards against PID reuse: if the kernel recycled the PID for an unrelated
    process, its create_time won't match what we recorded, so we treat the
    original as dead. 1-second tolerance because clock resolution varies."""
    try:
        p = psutil.Process(state.pid)
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return None
        if state.create_time is not None:
            try:
                if abs(p.create_time() - state.create_time) > 1.0:
                    return None  # PID reuse — different process now
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
        return p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _port_listening(port: int, timeout: float = 0.3) -> bool:
    """True if anyone is accepting connections on `port` (IPv4 or IPv6 loopback).
    Vite and some Node servers bind to ::1 only on Windows, so we try both."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((addr, port))
                return True
        except OSError:
            continue
    return False


def _http_ok(port: int, path: str, timeout: float = 0.6) -> bool:
    """GET http://<loopback>:<port><path>; True if status is 2xx or 3xx."""
    if not path.startswith("/"):
        path = "/" + path
    for host in ("127.0.0.1", "[::1]"):
        try:
            req = urllib.request.Request(f"http://{host}:{port}{path}", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 400:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue
        except Exception:
            continue
    return False


def _port_free(port: int) -> bool:
    """True if no process is listening on `port` (any interface) and we can bind to it."""
    if _port_owner(port) is not None:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _port_owner(port: int) -> tuple[int, str] | None:
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                if conn.pid:
                    try:
                        return (conn.pid, psutil.Process(conn.pid).name())
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        return (conn.pid, "?")
    except (psutil.AccessDenied, PermissionError):
        return None
    return None


@dataclass
class PortPlan:
    chosen: int
    preferred: int
    reassigned: bool
    conflict: tuple[int, str] | None


def _plan_port(app: AppSpec) -> PortPlan | None:
    h = app.health
    if h.port is None:
        return None
    if _port_free(h.port):
        return PortPlan(chosen=h.port, preferred=h.port, reassigned=False, conflict=None)
    owner = _port_owner(h.port)
    if not h.auto or h.range is None:
        return PortPlan(chosen=h.port, preferred=h.port, reassigned=False, conflict=owner)
    lo, hi = h.range
    for p in range(lo, hi + 1):
        if p == h.port:
            continue
        if _port_free(p):
            return PortPlan(chosen=p, preferred=h.port, reassigned=True, conflict=owner)
    raise RuntimeError(
        f"no free port in [{lo},{hi}] for app {app.name} (preferred {h.port} held by {owner})"
    )


def _apply_port(cmd: list[str], env: dict[str, str], app: AppSpec, port: int) -> list[str]:
    h = app.health
    if h.env:
        env[h.env] = str(port)
    if h.placeholder:
        return [arg.replace(h.placeholder, str(port)) for arg in cmd]
    return list(cmd)


def _rotate_if_large(path: Path) -> None:
    """Single-level rotation: if path exists and is bigger than the cap, move it
    to <path>.old (overwriting the previous .old). Keeps disk use bounded."""
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            old = path.with_suffix(path.suffix + ".old")
            try:
                if old.exists():
                    old.unlink()
            except OSError:
                pass
            path.replace(old)
    except OSError:
        pass


def is_ready(app: AppSpec, st: "Status") -> bool:
    """Single source of truth for 'is this app actually serving traffic?'
    Prefer HTTP probe when configured, else fall back to TCP-listening check."""
    if not st.running or st.port is None:
        return False
    if app.health.http:
        return _http_ok(st.port, app.health.http)
    return bool(st.port_listening)


def wait_until_ready(app: AppSpec, timeout: float = 30.0, poll: float = 0.3) -> bool:
    """Poll until is_ready() or timeout expires. Returns True on ready, False on timeout
    or process death."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = status(app)
        if not st.running:
            return False
        if is_ready(app, st):
            return True
        time.sleep(poll)
    return False


@dataclass
class Status:
    name: str
    running: bool
    pid: int | None
    uptime_s: float | None
    port: int | None
    preferred_port: int | None
    port_listening: bool | None
    port_conflict: tuple[int, str] | None = None
    http_ok: bool | None = None  # only populated when health.http is configured


def status(app: AppSpec) -> Status:
    state = _read_state(app.name)
    proc = _verify_proc(state) if state else None
    if state and not proc:
        _clear_state(app.name)
        state = None
    running = proc is not None
    preferred = app.health.port
    actual = (state.port if state and state.port else preferred) if running else preferred
    port_ok = _port_listening(actual) if (running and actual) else None
    http_ok = None
    if running and actual and app.health.http:
        http_ok = _http_ok(actual, app.health.http)
    conflict = None
    if not running and preferred is not None and not _port_free(preferred):
        conflict = _port_owner(preferred)
    return Status(
        name=app.name,
        running=running,
        pid=state.pid if running and state else None,
        uptime_s=(time.time() - state.started_at) if running and state else None,
        port=actual,
        preferred_port=preferred,
        port_listening=port_ok,
        port_conflict=conflict,
        http_ok=http_ok,
    )


@dataclass
class StartResult:
    status: Status
    plan: PortPlan | None
    error: str | None = None


def start(app: AppSpec) -> StartResult:
    existing = status(app)
    if existing.running:
        return StartResult(status=existing, plan=None)

    cwd = Path(app.cwd)
    if not cwd.exists():
        raise FileNotFoundError(f"cwd does not exist: {cwd}")

    plan = _plan_port(app)
    if plan and plan.conflict and not plan.reassigned:
        owner_pid, owner_name = plan.conflict
        return StartResult(
            status=existing,
            plan=plan,
            error=f"port {plan.preferred} is held by pid={owner_pid} ({owner_name}); "
                  f"set health.auto=true with a range to auto-reassign",
        )

    env = os.environ.copy()
    env.update(app.env)
    cmd = list(app.cmd)
    if plan:
        cmd = _apply_port(cmd, env, app, plan.chosen)

    out_path, err_path = _log_files(app.name)
    _rotate_if_large(out_path)
    _rotate_if_large(err_path)
    out_f = open(out_path, "ab")
    err_f = open(err_path, "ab")

    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (0x08000000): if any process in the chain calls
        #   AllocConsole(), the new console is invisible. Stronger than
        #   DETACHED_PROCESS, which `node.exe` sometimes circumvents by
        #   explicitly allocating its own (visible) console.
        # CREATE_NEW_PROCESS_GROUP (0x00000200): isolate so closing the parent
        #   shell doesn't deliver CTRL+C to the child.
        creationflags = 0x08000000 | 0x00000200

    popen_kwargs: dict = dict(
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=out_f,
        stderr=err_f,
        close_fds=True,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    # Capture create_time immediately so PID-reuse checks have a reference.
    create_time: float | None = None
    try:
        create_time = psutil.Process(proc.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    _write_state(
        app.name,
        RunState(
            pid=proc.pid,
            started_at=time.time(),
            cmd=cmd,
            port=plan.chosen if plan else None,
            create_time=create_time,
        ),
    )
    time.sleep(0.2)
    return StartResult(status=status(app), plan=plan)


def stop(app: AppSpec, timeout: float = 10.0) -> bool:
    state = _read_state(app.name)
    if not state:
        return False
    proc = _verify_proc(state)
    if not proc:
        _clear_state(app.name)
        return False

    # First pass: terminate everything we currently see.
    try:
        descendants = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        descendants = []
    for p in [*descendants, proc]:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Re-snapshot: SIGTERM may have triggered new fork()s before children die.
    # We wait for original targets, then sweep any new descendants and kill remnants.
    psutil.wait_procs([proc, *descendants], timeout=timeout)
    try:
        late = proc.children(recursive=True) if proc.is_running() else []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        late = []
    for p in [*late, proc]:
        try:
            if p.is_running():
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    _clear_state(app.name)
    return True


def reclaim_port(port: int, timeout: float = 5.0) -> tuple[int, str] | None:
    owner = _port_owner(port)
    if not owner:
        return None
    pid, name = owner
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None
    try:
        p.terminate()
        _, alive = psutil.wait_procs([p], timeout=timeout)
        for q in alive:
            q.kill()
    except psutil.NoSuchProcess:
        pass
    return (pid, name)


def log_paths(app: AppSpec) -> tuple[Path, Path]:
    return _log_files(app.name)


def clear_logs(app: AppSpec) -> list[Path]:
    """Truncate stdout/stderr log files for `app`. Returns paths that were touched."""
    touched: list[Path] = []
    for p in _log_files(app.name):
        try:
            # Open in write mode to truncate; create if missing for consistency.
            p.write_bytes(b"")
            touched.append(p)
        except OSError:
            pass
    # Also drop any rotated .old files — explicit clear means "really empty".
    for p in _log_files(app.name):
        old = p.with_suffix(p.suffix + ".old")
        try:
            if old.exists():
                old.unlink()
                touched.append(old)
        except OSError:
            pass
    return touched
