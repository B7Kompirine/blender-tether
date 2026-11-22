"""Shared test setup: a temporary state folder (the real ~/.tether is never touched), the check() recorder, a fake app and a fake agent.
Import this module before tether: it sets the environment first."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="tether_test_"))
os.environ["TETHER_HOME"] = str(TMP)
os.environ["TETHER_STATS_DIR"] = str(TMP / "stats")
os.environ["TETHER_ASSETS_DIR"] = str(TMP / "assets")
sys.path.insert(0, str(ROOT))
from tether import client  # noqa: E402
from tether.agent import REGISTRY_DIR  # noqa: E402

RESULTS = []
STATE = {"sollumz": False}               # set by the Blender tests: the Sollumz (GTA) checks only run when the add-on is available
for _stream in (sys.stdout, sys.stderr):      # the Windows console code page (cp1254, cp1252...) cannot write every character
    _stream.reconfigure(encoding="utf-8", errors="replace")


class _NoWindowPopen(subprocess.Popen):
    """From a process without a console (a background job, MCP) every python/node/taskkill child opens a black console window and steals focus: none may open."""

    def __init__(self, *a, **k):
        k.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
        super().__init__(*a, **k)


subprocess.Popen = _NoWindowPopen         # subprocess.run uses it too


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(("OK     " if condition else "FAIL   ") + name + (f"   <- {detail}" if detail and not condition else ""))


def pid_alive(pid):
    from tether.agent import proc_info
    return proc_info(pid)[0]


def kill_pid(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def start_app(name, mode="background"):
    p = subprocess.Popen([sys.executable, str(ROOT / "tether" / "agent" / "example_app.py"), name, mode], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 10:
        if any(k["pid"] == p.pid for k in client.apps(prune=False)):
            return p
        time.sleep(0.1)
    p.kill()
    raise RuntimeError("the example app did not register")


def fake_agent(reply):
    """A fake agent that accepts connections and answers with reply(); returns (port, stop). A reply of None never answers."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    s.settimeout(0.3)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                c, _ = s.accept()
            except OSError:
                continue
            f = c.makefile("rwb")
            f.readline()
            r = reply()
            if r is not None:
                f.write(json.dumps(r).encode() + b"\n")
                f.flush()
            else:
                time.sleep(4)
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    return s.getsockname()[1], lambda: (stop.set(), s.close())


def write_registry(name, pid, port, ft=None):
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    (REGISTRY_DIR / f"{name}-{pid}.json").write_text(json.dumps(
        {"tether": 1, "app": name, "pid": pid, "proc_filetime": ft, "port": port, "token": "a" * 32, "version": "0", "mode": "gui", "lang": "x"}), encoding="utf-8")
