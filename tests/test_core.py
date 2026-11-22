"""Engine, client, script runner, stale registry records, malformed input, lifecycle, the Node agent, the CLI and the call stats. No Blender needed."""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from harness import REGISTRY_DIR, ROOT, TMP, check, client, start_app, fake_agent, write_registry


def engine_and_client():
    print("--- engine + client")
    p = start_app("example")
    listing = client.apps()
    k = listing[0]
    check("registry: the pid matches, state ok", len(listing) == 1 and k["pid"] == p.pid and k["state"] == "ok")
    check("the registry holds a token and a port", len(k["token"]) == 32 and k["port"] > 1024)
    c = lambda code, **kw: client.run("example", code=code, **kw)  # noqa: E731
    check("the value of the last expression is returned", c("x = 41\nx + 1").get("result") == 42)
    check("persistent namespace", c("x").get("result") == 41)
    y = c("x", fresh=True)
    check("fresh=True does not see the persistent namespace", not y["ok"] and y["error"]["type"] == "NameError")
    y = c("a = 1\nb = 2\nc = a / 0\nd = 4")
    check("error: the line number is right (3)", not y["ok"] and "line 3" in y["error"]["trace"] and "c = a / 0" in y["error"]["trace"], y.get("error"))
    c("def f():\n    raise ValueError('inner')")
    y = c("f()")
    check("an error in a function defined by an earlier call shows its own line", "raise ValueError('inner')" in y["error"]["trace"], y.get("error"))
    y = c("def (")
    check("syntax error without stdlib frames", y["error"]["type"] == "SyntaxError" and "ast.py" not in y["error"]["trace"], y.get("error"))
    y = c("1 / 0")
    check("the error_hint hook is written to error.hint and the CLI text shows it",
          y["error"].get("hint") == "the divisor must not be zero" and "HINT: the divisor must not be zero" in client.format_run(y)[0], y.get("error"))
    check("without a hook there is no hint field", "hint" not in c("nope")["error"])
    y = c("print('hello')\nresult = {'a': [1, 2]}")
    check("print + the result variable", y["output"] == "hello\n" and y["result"] == {"a": [1, 2]})
    y = c("print('a' * 30000)")
    check("big output is cut at 8k characters, head and tail are kept",
          y["truncated"] and 7000 < len(y["output"]) < 9000 and y["output"].startswith("aaa") and y["output"].rstrip().endswith("aaa"))
    y = c("import sys\nsys.exit(3)")
    check("sys.exit does not close the app", not y["ok"] and y["error"]["type"] == "SystemExit" and client.apps()[0]["state"] == "ok")
    check("hello() comes from setup", c("hello('k')").get("result") == "hello k")
    client.request("example", "reset")
    check("reset clears the namespace", not c("x")["ok"])
    script = TMP / "script.py"
    script.write_text("import sys\nprint(__name__, sys.argv[sys.argv.index('--') + 1:])\nresult = 7", encoding="utf-8")
    y = client.run("example", file=str(script), args=["a", "b c"])
    check("file + args: __main__ and argv", y["output"] == "__main__ ['a', 'b c']\n" and y["result"] == 7, y)
    c("zz = 5")
    script2 = TMP / "script2.py"
    script2.write_text("print(zz)", encoding="utf-8")
    y = client.run("example", file=str(script2))
    check("a file runs in a fresh namespace by default (does not see persistent variables)", not y["ok"] and y["error"]["type"] == "NameError", y)
    y = client.run("example", file=str(script2), fresh=False)
    check("file + fresh=False uses the persistent namespace", y["ok"] and y["output"] == "5\n", y)
    y = client.request("example", "view")
    check("an app without view support returns a clear error", not y["ok"] and y["error"]["type"] == "unsupported")
    check("search: the app's hook works", "hello(who)" in client.api_search("example", "hello"))
    caps = client.request("example", "info")["capabilities"]
    check("info.capabilities reports search and view correctly", {"search", "run"} <= set(caps) and "view" not in caps)
    y = client._send({**k, "token": "x" * 32}, "info", 5)
    check("a wrong token is rejected", not y["ok"] and y["error"]["type"] == "token")
    c("import threading\nlog = []\nclass Trace:\n    def __iter__(self):\n        log.append(threading.current_thread().name)\n        return iter([1, 2])\nresult = Trace()")
    check("the result is reduced to plain data on the main thread (the connection thread never touches a bpy object)", set(c("log")["result"]) == {"MainThread"}, c("log"))
    y = c("emit_warning('careful')")
    check("agent.warn: the next response carries a warning (once) and format_run shows it",
          y.get("warning") == "careful" and "warning" not in c("1") and "WARNING: careful" in client.format_run(y)[0], y)
    b = client.request("example", "info")
    check("info: the heartbeat_s and extra fields exist", b["heartbeat_s"] is not None and b["heartbeat_s"] < 2 and b["extra"] == {}, b)
    # busy: info + cancel
    t = threading.Thread(target=lambda: c("import time\ntime.sleep(2.5)"))
    t.start()
    time.sleep(0.6)
    b = client.request("example", "info")
    check("info is answered while busy and says what is running", b["busy"] and b["running"]["code"] == "import time", b)
    t.join()
    marker = TMP / "cancel.txt"
    t = threading.Thread(target=lambda: c("import time\ntime.sleep(2)"))
    t.start()
    time.sleep(0.3)
    s = socket.create_connection(("127.0.0.1", k["port"]), timeout=5)
    s.sendall(json.dumps({"id": 9, "token": k["token"], "op": "run", "code": f"open(r'{marker}', 'w').write('x')"}).encode() + b"\n")
    time.sleep(0.3)
    s.close()
    t.join()
    time.sleep(0.8)
    check("when the client gives up the queued job does not run", not marker.exists())
    # selector
    p2 = start_app("example", "gui")
    try:
        client.run("example", code="1")
        check("two instances: a call without a selector raises", False)
    except client.TetherError as e:
        check("two instances: a call without a selector raises and lists the options", f"example#{p.pid}" in str(e) and f"example#{p2.pid}" in str(e), str(e))
    check("name#pid selects", client.run(f"example#{p2.pid}", code="2 + 3").get("result") == 5)
    check("name:mode selects", client.request("example:gui", "info")["pid"] == p2.pid and client.request("example:background", "info")["pid"] == p.pid)
    client.request(f"example#{p2.pid}", "close")
    p2.wait(timeout=5)
    check("close: the process exits and the registry file is deleted", p2.returncode == 0 and not (REGISTRY_DIR / f"example-{p2.pid}.json").exists())
    # stale record: the process is killed
    p.kill()
    p.wait()
    check("the registry record of a killed process is stale: apps() deletes it", client.apps() == [] and not (REGISTRY_DIR / f"example-{p.pid}.json").exists())


def script_runner():
    print("--- script runner (cwd, env, grep, full output, module/path leaks)")
    p = start_app("example")
    try:
        d1, d2 = TMP / "project1", TMP / "project2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "mid.py").write_text("import os\nprint('cwd', os.path.basename(os.getcwd()), 'env', os.environ.get('KT_ENV'))\n"
                                   "result = os.path.basename(__file__)\n", encoding="utf-8")
        y = client.run("example", file="mid.py", cwd=str(d1), env={"KT_ENV": "ab"})
        check("a relative script is found relative to cwd; cwd and env apply in the script; the response says which path ran",
              y["ok"] and y["output"] == "cwd project1 env ab\n" and y["result"] == "mid.py" and Path(y["file"]) == (d1 / "mid.py").resolve(), y)
        y = client.run("example", code="import os\n[os.path.basename(os.getcwd()), os.environ.get('KT_ENV')]")
        check("after the run cwd and env are restored (no leaks)", y["result"][0] != "project1" and y["result"][1] is None, y)
        error = ""
        try:
            client.run("example", file="no_such.py", cwd=str(d1))
        except client.TetherError as e:
            error = str(e)
        check("a missing script: the places looked in are listed", "script not found: no_such.py" in error and str(d1) in error, error)
        (d1 / "grep.py").write_text("for i in range(5):\n    print('FRAME' if i % 2 == 0 else 'info', i)\n", encoding="utf-8")
        y = client.run("example", file=str(d1 / "grep.py"), grep="^FRAME")
        check("grep: only matching lines are kept; matched/total are reported",
              y["output"] == "FRAME 0\nFRAME 2\nFRAME 4" and (y["grep_matched"], y["grep_total"]) == (3, 5) and "grep 3/5 lines" in client.format_run(y)[0], y)
        y = client.run("example", code="1", grep="(")
        check("an invalid grep: a clear error, the code does not run", not y["ok"] and y["error"]["type"] == "request", y)
        y = client.run("example", code="for i in range(4000):\n    print('line', i)")
        full = Path(y.get("full_output", ""))
        check("the full text of a truncated output goes to a file; head and tail are in the response",
              y["truncated"] and full.is_file() and full.read_text(encoding="utf-8").count("\n") == 4000 and "line 0\n" in y["output"]
              and y["output"].rstrip().endswith("line 3999") and "full text" in client.format_run(y)[0], y)
        y = client.run("example", code="for i in range(4000):\n    print('line', i)", grep=r"line 39\d\d$")
        check("grep avoids truncation: no full-output file is needed", not y["truncated"] and "full_output" not in y and y["grep_matched"] == 100, y)
        y = client.run("example", code="import sys\nprint('done')\nsys.exit(0)\nprint('never')")
        check("sys.exit(0) is success: output kept, exit_code=0", y["ok"] and y["exit_code"] == 0 and y["output"] == "done\n", y)
        y = client.run("example", code="import sys\nsys.exit(3)")
        check("sys.exit(3) is an error, exit_code=3", not y["ok"] and y["exit_code"] == 3 and y["error"]["type"] == "SystemExit", y)
        (d1 / "oex.py").write_text("import os, sys\nprint('last')\nsys.stdout.flush()\nos._exit(0)\nprint('never')\n", encoding="utf-8")
        y = client.run("example", file=str(d1 / "oex.py"))
        check("a script that ends with os._exit(0) does not kill the agent: output kept, exit_code=0",
              y["ok"] and y["exit_code"] == 0 and y["output"] == "last\n" and client.apps()[0]["state"] == "ok", y)
        check("os._exit comes back after the run (the real os._exit)", client.run("example", code="import os\nos._exit.__name__")["result"] == "_exit")
        y = client.run("example", code="import os\nprint('py-line')\nos.write(1, b'c-line-1\\n')\nos.write(2, b'c-line-2\\n')")
        check("C-level (fd 1 and 2) output is captured too, in the same output as Python's print",
              y["ok"] and "py-line" in y["output"] and "c-line-1" in y["output"] and "c-line-2" in y["output"], y)
        y = client.run("example", code="import ctypes, sys\nctypes.CDLL('ucrtbase' if sys.platform == 'win32' else None).puts(b'puts-line')")
        check("buffered C stdio (puts) is captured too", "puts-line" in y["output"], y)
        y = client.run("example", code="import subprocess, sys\nsubprocess.run([sys.executable, '-c', \"print('child-line')\"])")
        check("the output of a child process is captured too", "child-line" in y["output"], y)
        y = client.run("example", code="print('next')")
        check("C output does not leak into the next run (the redirection is undone)", y["output"] == "next\n", y)
        y = client.request("example", "call", name="add", args={"a": 2, "b": 40})
        check("call: runs the registered function, the result is plain data", y["ok"] and y["result"] == 42, y)
        y = client.request("example", "call", name="write", args={"text": "abc"})
        check("call: print output is captured, a dict result comes back", y["ok"] and y["output"] == "written: abc\n" and y["result"] == {"length": 3}, y)
        y = client.request("example", "call", name="divide", args={"a": 1, "b": 0})
        check("call: the error type and the error_hint hook",
              not y["ok"] and y["error"]["type"] == "ZeroDivisionError" and y["error"].get("hint") == "the divisor must not be zero", y)
        y = client.request("example", "call", name="missing")
        check("call: an unknown function -> unsupported, with the available ones", not y["ok"] and y["error"]["type"] == "unsupported" and "add" in y["error"]["message"], y)
        y = client.request("example", "call", name="add", args={"a": 1})
        check("call: a missing argument is a TypeError", not y["ok"] and y["error"]["type"] == "TypeError", y)
        y = client.request("example", "call", name="add", args=[1, 2])
        check("call: args must be a dict", not y["ok"] and y["error"]["type"] == "request", y)
        b = client.request("example", "info")
        check("info: the call capability and the function names", "call" in b["capabilities"] and b["functions"] == ["add", "divide", "write"], b)
        check("client.call returns the response dict", client.call("example", "add", {"a": 1, "b": 2})["result"] == 3)
        try:
            client.call("example", "divide", {"a": 1, "b": 0})
            ok = False
        except client.TetherError as e:
            ok = "ZeroDivisionError" in str(e) and "HINT" in str(e)
        check("client.call: an error becomes TetherError (with the hint)", ok)
        y = client.run("example", code="import os\nos.write(1, b'\\x1b[1;33mWARN\\x1b[0m text\\n')")
        check("colour escape sequences (ANSI) in C-level output are stripped", y["output"] == "WARN text\n", y)
        y = client.run("example", code="import sys\nsys.exit('reason')")
        check("sys.exit('text') counts as exit 1, the text is in the message", not y["ok"] and y["exit_code"] == 1 and "reason" in y["error"]["message"], y)
        (d2 / "side_module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (d2 / "main.py").write_text("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\nimport side_module\nprint(side_module.VALUE)\n",
                                    encoding="utf-8")
        y1 = client.run("example", file=str(d2 / "main.py"))
        (d2 / "side_module.py").write_text("VALUE = 222\n", encoding="utf-8")      # change the size so a stale .pyc is not taken as valid
        y2 = client.run("example", file=str(d2 / "main.py"))
        check("a sibling module in the script folder is read fresh on every run", y1["output"] == "1\n" and y2["output"] == "222\n", (y1, y2))
        y = client.run("example", code="import sys\n['side_module' in sys.modules, any('project2' in p for p in sys.path)]")
        check("after the run no module or sys.path entry is left", y["result"] == [False, False], y)
        y = client.run("example", code="1", clean=True)
        check("an app without prepare refuses a clean/blend request (it does not silently ignore it)", not y["ok"] and y["error"]["type"] == "unsupported", y)
        k = client.apps()[0]
        y = client._send(k, "run", 5, code="1", cwd=str(TMP / "missing"))
        check("a missing cwd: a request error", not y["ok"] and y["error"]["type"] == "request", y)
        y = client._send(k, "run", 5, code="1", env={"a": 1})
        check("env values must be text", not y["ok"] and y["error"]["type"] == "request", y)
    finally:
        p.kill()
        p.wait()


def stale_and_hung():
    print("--- stale / hung / foreign agent")
    finished = subprocess.Popen([sys.executable, "-c", "pass"])      # a surely dead pid (Windows ignores the low 2 bits of a pid: an invented pid is not reliable)
    finished.wait()
    write_registry("dead", finished.pid, 1)
    t0 = time.time()
    listing = client.apps()
    elapsed = time.time() - t0
    check("a record whose process is dead is deleted without going to the network (waiting for a refusal would take >= 2 s)",
          listing == [] and not (REGISTRY_DIR / f"dead-{finished.pid}.json").exists() and elapsed < 1.5, f"{elapsed:.2f} s, {listing}")
    write_registry("closed", os.getpid(), 1)      # the process is alive, the port is closed: refused
    t0 = time.time()
    listing = client.apps()
    elapsed = time.time() - t0
    check("a record whose process is alive but whose connection is refused is deleted",
          listing == [] and not (REGISTRY_DIR / f"closed-{os.getpid()}.json").exists() and elapsed < 4, f"{elapsed:.2f} s, {listing}")
    port, stop = fake_agent(lambda: None)
    write_registry("hung", os.getpid(), port)
    t0 = time.time()
    listing = client.apps()
    check("an agent that does not answer is listed as 'no_response', its record is kept",
          len(listing) == 1 and listing[0]["state"] == "no_response" and (REGISTRY_DIR / f"hung-{os.getpid()}.json").exists() and time.time() - t0 < 6)
    stop()
    (REGISTRY_DIR / f"hung-{os.getpid()}.json").unlink()
    port, stop = fake_agent(lambda: {"ok": True, "pid": 1})
    write_registry("foreign", os.getpid(), port)
    check("if another agent took the port the record is deleted", client.apps() == [])
    stop()
    try:
        client.resolve("none")
        check("a missing app raises a clear error", False)
    except client.TetherError as e:
        check("a missing app raises a clear error", "is not running" in str(e))


def malformed_input():
    print("--- malformed input and attacks")
    p = start_app("fz")
    k = next(x for x in client.apps() if x["pid"] == p.pid)

    def raw(data, wait=1.0):
        s = socket.create_connection(("127.0.0.1", k["port"]), timeout=10)

        def send():                          # read concurrently: when the server closes with unread data Windows sends an RST and drops the pending response
            try:
                s.sendall(data)
            except OSError:
                pass
        t = threading.Thread(target=send, daemon=True)
        t.start()
        s.settimeout(wait)
        try:
            r = s.recv(65536)
        except socket.timeout:
            r = b"<TIMEOUT>"
        except (ConnectionResetError, ConnectionAbortedError):
            r = b"<RESET>"
        s.close()
        t.join(5)
        return r

    def request(**kw):
        return json.dumps({"id": 1, "token": k["token"], **kw}).encode() + b"\n"
    r = raw(json.dumps({"id": 1, "token": "é", "op": "info"}).encode("utf-8") + b"\n")
    check("a non-ASCII token: a token error comes back without the thread dying", b'"type": "token"' in r, r)
    r = raw(b"[" * 100000 + b"\n")
    check("very deeply nested JSON: a protocol error comes back, the connection does not close silently", b'"type": "protocol"' in r, r[:120])
    r = raw(request(op="run", code="result = [float('nan'), float('inf'), 1.5]"))
    check("NaN/Infinity are written to strict JSON as text", b'"NaN"' in r and b'"Infinity"' in r and b": NaN" not in r and b", NaN" not in r and b"[NaN" not in r, r)
    r = raw(request(op="run", code="1", args="abc"))
    check("args that is not a list is an error (it is not split into characters)", b'"ok": false' in r and b"list" in r, r)
    marker = TMP / "http_pwned.txt"
    body = json.dumps({"id": 1, "op": "run", "code": f"open(r'{marker}', 'w').write('x')"}).encode() + b"\n"
    raw(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    time.sleep(0.5)
    check("HTTP cross-protocol: code without a token does not run", not marker.exists())
    r = raw(b"x" * (70 * 1024 * 1024), 8)
    check("a line larger than 64 MB is rejected (a protocol error or the connection is reset)", b'"type": "protocol"' in r or r == b"<RESET>", r[:120])
    check("after the attacks the agent is alive and answers correctly", client.run(f"fz#{p.pid}", code="6 * 7").get("result") == 42)
    p.kill()
    p.wait()


def lifecycle():
    print("--- lifecycle and robustness")
    example = str(ROOT / "tether" / "agent" / "example_app.py")

    def start(name, **env):
        p = subprocess.Popen([sys.executable, example, name], env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 10 and not any(k["pid"] == p.pid for k in client.apps(prune=False)):
            time.sleep(0.1)
        return p

    p = start("idle", EXAMPLE_IDLE_S="3")
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < 12:
        time.sleep(0.2)
    check("idle timeout: a daemon that gets no requests closes itself and deletes its record",
          p.poll() == 0 and not (REGISTRY_DIR / f"idle-{p.pid}.json").exists(), f"exit={p.poll()}")
    p = start("idle2", EXAMPLE_IDLE_S="3")
    for _ in range(7):                       # it does not close while requests keep coming
        client.run("idle2", code="1")
        time.sleep(1)
    check("idle timeout: the daemon does not close while requests arrive", p.poll() is None)
    p.kill()
    p.wait()
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    p = start("owned", EXAMPLE_OWNER_PID=str(owner.pid))
    owner.wait()
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < 10:
        time.sleep(0.2)
    check("when the owner process dies the daemon closes itself (no orphans)", p.poll() == 0, f"exit={p.poll()}")
    # a pid taken over by another process (the creation time does not match): the record is stale, no network
    write_registry("pidreuse", os.getpid(), 1, ft=123)
    t0 = time.time()
    listing = client.apps()
    check("the pid is alive but the process creation time differs: the record is stale and deleted without the network",
          listing == [] and time.time() - t0 < 1.5 and not (REGISTRY_DIR / f"pidreuse-{os.getpid()}.json").exists())
    # a record with a BOM (a PowerShell/.NET agent) is read
    p = start_app("bom")
    path = REGISTRY_DIR / f"bom-{p.pid}.json"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    check("a registry file with a BOM is read", [k["app"] for k in client.apps()] == ["bom"])
    k = client.apps()[0]
    # if the first line is not JSON (a browser's HTTP request): one error line and the connection is dropped
    s = socket.create_connection(("127.0.0.1", k["port"]), timeout=5)
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    s.settimeout(2)
    data = b""
    try:
        while True:
            b = s.recv(4096)
            if not b:
                break
            data += b
        closed = True
    except socket.timeout:
        closed = False
    s.close()
    check("if the first line is not JSON one error response comes back and the connection is dropped", closed and data.count(b"\n") == 1 and b'"protocol"' in data, data[:150])
    # connection limit: the excess is refused, the agent lives
    sockets = [socket.create_connection(("127.0.0.1", k["port"]), timeout=5) for _ in range(75)]
    time.sleep(0.6)
    refused = 0
    for so in sockets:
        so.settimeout(0.05)
        try:
            if so.recv(1) == b"":
                refused += 1
        except socket.timeout:
            pass
        except OSError:
            refused += 1
    for so in sockets:
        so.close()
    time.sleep(0.3)
    check("connection limit (64): the excess is refused, the agent lives", refused >= 5 and client.run("bom", code="2 + 2").get("result") == 4, f"refused={refused}")
    p.kill()
    p.wait()


def node_agent():
    node = shutil.which("node")
    if not node:
        print("--- Node agent: node not found, skipped")
        return
    print("--- Node agent (the protocol does not depend on Python)")
    p = subprocess.Popen([node, str(ROOT / "tether" / "agent" / "example_node.js"), "nod"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 10 and not any(k["pid"] == p.pid for k in client.apps(prune=False)):
        time.sleep(0.1)
    k = next((k for k in client.apps() if k["pid"] == p.pid), None)
    check("node: the agent registers, language javascript", k is not None and k["info"]["lang"] == "javascript", k)
    if k is None:
        p.kill()
        return
    c = lambda code, **kw: client.run("nod", code=code, **kw)  # noqa: E731
    check("node: the last expression's value is returned", c("var x = 41; x + 1").get("result") == 42)
    check("node: persistent context", c("x").get("result") == 41)
    c("const k = 5")
    check("node: let/const persist too", c("k * 2").get("result") == 10)
    y = c("console.log('hi'); 1")
    check("node: console.log goes into the output", y["output"] == "hi\n" and y["result"] == 1, y)
    y = c("null.x")
    check("node: error TypeError + a line trace", not y["ok"] and y["error"]["type"] == "TypeError" and "<tether>:1" in y["error"]["trace"], y.get("error"))
    check("node: a wrong token is rejected", client._send({**k, "token": "x" * 32}, "info", 5)["error"]["type"] == "token")
    check("node: the search op works", "no API" in client.api_search("nod", "x"))
    y = client.request("nod", "view")
    check("node: an unsupported view gives a clear error", not y["ok"] and y["error"]["type"] == "unsupported", y)
    client.request("nod", "reset")
    check("node: reset clears the context", not c("x")["ok"] and not c("k")["ok"])
    client.request("nod", "close")
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    check("node: close ends the process and deletes the record", p.returncode == 0 and not (REGISTRY_DIR / f"nod-{p.pid}.json").exists())


def command_line():
    print("--- CLI")
    p = start_app("example")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cli = lambda *a: subprocess.run([sys.executable, "-m", "tether", *a], capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT))  # noqa: E731
    r = cli("status")
    check("status: lists the example", "example#" in r.stdout and "idle" in r.stdout, r.stdout + r.stderr)
    r = cli("run", "example", "-c", "print('cli'); 6 * 7")
    check("run: output + result", r.returncode == 0 and "cli" in r.stdout and "-> 42" in r.stdout, r.stdout + r.stderr)
    r = cli("run", "example", "-c", "1/0")
    check("run: an error gives exit code 1, the trace is on stderr", r.returncode == 1 and "ZeroDivisionError" in r.stderr, r.stdout + r.stderr)
    r = cli("run", "missing", "-c", "1")
    check("a missing app gives exit code 2", r.returncode == 2 and "is not running" in r.stderr, r.stdout + r.stderr)
    (TMP / "cli_mid.py").write_text("import os\nprint('cwd', os.path.basename(os.getcwd()), os.environ.get('KT_ENV'))\nprint('hidden')\n", encoding="utf-8")
    r = cli("run", "example", "-f", "cli_mid.py", "--cwd", str(TMP), "--env", "KT_ENV=cli", "--grep", "^cwd")
    check("CLI: --cwd --env --grep", r.returncode == 0 and f"cwd {TMP.name} cli" in r.stdout and "hidden" not in r.stdout, r.stdout + r.stderr)
    r = cli("run", "example", "-c", "1", "--env", "broken")
    check("CLI: a malformed --env is a usage error (exit 2)", r.returncode == 2, r.stdout + r.stderr)
    p.kill()
    p.wait()


def call_stats():
    print("--- call log and stats")
    folder = TMP / "stats"
    shutil.rmtree(folder, ignore_errors=True)
    p = start_app("stat")
    try:
        code = "secret_variable_name = 'SECRET_TEXT_987654'\nprint(secret_variable_name.upper())"
        client.run("stat", code=code)
        client.run("stat", code=code)
        client.run("stat", code="1/0")
        try:
            client.request("stat", "run", timeout=0.5, code="import time\ntime.sleep(2)")
        except client.TetherError:
            pass
        time.sleep(2.3)
        client.request("stat", "reset")
        client.run("stat", code=code)
        path = next(folder.glob("*.jsonl"))
        text = path.read_text(encoding="utf-8")
        k = [json.loads(s) for s in text.splitlines()]
        check("log: one JSON line per call, 'info' calls are not written (6 calls: 4 run + reset + run)", len(k) == 6 and [r["op"] for r in k] == ["run"] * 4 + ["reset", "run"], [r["op"] for r in k])
        check("log: code length, line count, duration, app fields", k[0]["code"] == len(code) and k[0]["lines"] == 2 and k[0]["ok"] is True and k[0]["app"] == "stat" and k[0]["ms"] >= 0, k[0])
        check("log privacy: neither code nor output text is in the file (numbers only)",
              "SECRET_TEXT" not in text and "secret_variable_name" not in text and "987654" not in text and "print" not in text, text[:300])
        check("log: sending the same code a second time counts as repeat = 2/2 lines; the first time 0", k[0]["repeat"] == 0 and k[1]["repeat"] == 2, (k[0]["repeat"], k[1]["repeat"]))
        check("log: the error type is recorded (ZeroDivisionError), ok=False", k[2]["error"] == "ZeroDivisionError" and k[2]["ok"] is False, k[2])
        check("log: a timeout is recorded (timeout, duration >= 500 ms)", k[3]["error"] == "timeout" and k[3]["ms"] >= 500, k[3])
        check("log: after a reset the same code is not counted as repeated (the namespace was emptied)", k[5]["repeat"] == 0, k[5])
        o = client.stats(1)
        check("stats: call count, op distribution, error rate and types, time, code and repeat summary",
              "6 calls" in o and "run 5" in o and "errors: 2/6 (33.3%)" in o and "ZeroDivisionError 1" in o and "timeout 1" in o and "time ms:" in o
              and "lines already sent before 2/8" in o, o)
        check("stats: with no records it says why", "no calls recorded" in client.stats(0))
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, "-m", "tether", "stats", "--days", "1"], capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT))
        check("CLI: the stats command gives the same summary", r.returncode == 0 and "6 calls" in r.stdout, r.stdout + r.stderr)
        os.environ["TETHER_STATS"] = "0"
        try:
            client.run("stat", code="1 + 1")
        finally:
            del os.environ["TETHER_STATS"]
        check("TETHER_STATS=0 turns the log off", len(path.read_text(encoding="utf-8").splitlines()) == 6)
        original = client.STATS_DIR
        (TMP / "file_blocker").write_text("x", encoding="utf-8")
        client.STATS_DIR = TMP / "file_blocker" / "sub"
        try:
            y = client.run("stat", code="6 * 7")
        finally:
            client.STATS_DIR = original
        check("if the log cannot be written the call still works (the log never breaks a call)", y.get("result") == 42, y)
    finally:
        p.kill()
        p.wait()


def run_all():
    engine_and_client()
    script_runner()
    stale_and_hung()
    malformed_input()
    lifecycle()
    node_agent()
    command_line()
    call_stats()
