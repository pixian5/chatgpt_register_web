#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time


ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
VENV_UVICORN = os.path.join(ROOT, ".venv", "bin", "uvicorn")
LOG_PATH = os.path.join(ROOT, "uvicorn.log")


def _pick_python():
    return VENV_PY if os.path.exists(VENV_PY) else sys.executable


def _uvicorn_cmd():
    if os.path.exists(VENV_UVICORN):
        return [VENV_UVICORN]
    return [_pick_python(), "-m", "uvicorn"]


def _list_uvicorn_pids():
    try:
        out = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_str, cmd = line.split(None, 1)
        except ValueError:
            continue
        if "uvicorn" in cmd and "web_app:app" in cmd:
            try:
                pids.append(int(pid_str))
            except ValueError:
                continue
    return pids


def _is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_pids(pids, sig, timeout):
    if not pids:
        return True
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_is_alive(pid) for pid in pids):
            return True
        time.sleep(0.2)
    return False


def _remove_old_log():
    try:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
    except Exception:
        pass


def _build():
    py = _pick_python()
    subprocess.check_call([py, "-m", "compileall", "-q", ROOT])


def _start():
    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "52789")
    cmd = _uvicorn_cmd() + [
        "web_app:app",
        "--host", host,
        "--port", str(port),
        "--reload",
    ]
    log = open(LOG_PATH, "a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    return proc.pid


def main():
    pids = _list_uvicorn_pids()
    if pids:
        ok = _stop_pids(pids, signal.SIGTERM, timeout=4)
        if not ok:
            _stop_pids(pids, signal.SIGKILL, timeout=2)
    _remove_old_log()
    _build()
    pid = _start()
    print(f"Started uvicorn pid={pid}, log={LOG_PATH}")


if __name__ == "__main__":
    main()
