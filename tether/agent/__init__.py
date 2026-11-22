"""Tether protocol 1: TCP 127.0.0.1 (port assigned by the OS), one UTF-8 JSON object per line, strictly request-response.

Request {"id", "token", "op", ...}; response {"id", "ok", ...}; error {"ok": false, "error": {"type", "message", "trace", "hint"?}}.
Ops: info · run {code | file, args, fresh, max_output, cwd, env, grep, prepare} · search {query} -> {text} · view {width, area?, options?}
-> {png_b64, caption?} · call {name, args} -> {result, output} (a function registered by the host: scene report, asset import...) · reset · close.
Capability-gated ops (search, view, call, close) are listed in info.capabilities (function names in info.functions); otherwise error.type == "unsupported".
error.type: protocol (the connection is also dropped if the first line is not JSON) · token · op · request · unsupported · cancelled · the exception name for code errors.
A response may carry "warning" (e.g. the namespace was reset). run with a file uses a fresh namespace unless fresh is given;
a run with file/cwd/env behaves like a fresh process (cwd, env and sys.path are restored; modules imported from the script folder are dropped).
run response: ok · output · truncated · ms · result | error · file · exit_code (sys.exit code) · full_output (path of the untruncated output) · grep_matched/grep_total.
prepare: opaque host-specific dict (Blender: blend, clean); applied on the host's main thread before the code runs.
Registry: ~/.tether/apps/<app>-<pid>.json {tether, app, pid, proc_filetime, port, token, version, mode, lang, started}; TETHER_HOME moves the folder.
The token is in the registry file: whoever can read the file can connect. The registry is an untrusted hint: the client first verifies the process
(pid + creation time). JSON is strict (NaN/Infinity become text); results are reduced to plain data on the main thread.
"""
import ast
import atexit
import base64
import contextlib
import hmac
import io
import itertools
import json
import linecache
import os
import queue
import re
import secrets
import select
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

PROTOCOL = 1
HOME = Path(os.environ.get("TETHER_HOME") or Path.home() / ".tether")
REGISTRY_DIR = HOME / "apps"
OUTPUT_DIR = HOME / "outputs"      # full text of truncated outputs; files older than a day are removed when an agent starts
LINE_LIMIT = 64 * 1024 * 1024      # max size of one request line
OUTPUT_LIMIT = 8000                # default output cap; the first 60% and the last 40% are kept (the full text goes to a file)
COLLECT_LIMIT = 32 * 1024 * 1024   # characters collected from one run before truncating and filtering
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")     # without a console (NUL counts as a tty) C logs arrive colour-coded
RESULT_LIMIT = 100000              # max size of the "result" JSON
SOURCE_COUNT = 200                 # code snippets kept so traces can show source lines
IDLE_S = 600                       # a client silent this long is disconnected
FIRST_LINE_S = 5                   # a connection that sends no first line within this time is dropped
CONNECTION_LIMIT = 64              # concurrent connections
ACTIVE = {}                        # app name -> running agent: one agent per process (the host's own registry is wiped on file load, this is not)


def proc_info(pid):
    """(is the process alive, creation time as FILETIME | None). On Windows os.kill(pid, 0) sends CTRL_C_EVENT, so it is not used there."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True, None
        except OSError:
            return False, None
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5, None   # access denied: the process exists
    try:
        code = wintypes.DWORD()
        alive = bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259      # STILL_ACTIVE
        ft = None
        if alive:
            a, b, c, d = (wintypes.FILETIME() for _ in range(4))
            if k.GetProcessTimes(h, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c), ctypes.byref(d)):
                ft = (a.dwHighDateTime << 32) | a.dwLowDateTime
        return alive, ft
    finally:
        k.CloseHandle(h)


class _Collector(io.TextIOBase):
    """Collects a run's output (up to COLLECT_LIMIT); truncating and filtering happen after the run."""

    def __init__(self):
        self.parts, self.n, self.overflowed = [], 0, False

    def writable(self):
        return True

    def write(self, s):
        if self.n < COLLECT_LIMIT:
            self.parts.append(s)
            self.n += len(s)
        else:
            self.overflowed = True
        return len(s)

    def flush(self):
        pass

    def text(self):
        return "".join(self.parts) + ("\n[... output exceeded the collection limit; the rest was not captured ...]" if self.overflowed else "")


class _CStream:
    """Redirects OS-level stdout/stderr (fd 1 and 2) to a temp file during a run, so what the app's C code writes (Blender operator reports,
    CLOG lines such as 'Unable to pack file', add-on logs, child processes) shows up in the same output as Python's print. Process-wide:
    whatever another thread writes meanwhile is captured too. If redirection fails (disk, fd) only the Python output remains. text is filled on exit."""

    def __init__(self):
        self.text, self._file, self._saved = "", None, []

    def __enter__(self):
        try:
            self._file = tempfile.TemporaryFile()
            for fd in (1, 2):
                try:
                    self._saved.append((fd, os.dup(fd)))
                except OSError:
                    self._saved.append((fd, None))          # fd closed (no-console process, e.g. Blender started through WMI): closed again at the end
            for fd in (1, 2):
                os.dup2(self._file.fileno(), fd)
        except OSError:
            self._restore()
            if self._file is not None:
                self._file.close()
            self._file = None
        return self

    def _restore(self):
        for fd, old in self._saved:
            if old is None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            else:
                os.dup2(old, fd)
                os.close(old)
        self._saved = []

    def __exit__(self, *_):
        if self._file is None:
            return False
        try:
            import ctypes
            ctypes.CDLL("ucrtbase" if os.name == "nt" else None).fflush(None)       # let the C stdio buffers reach the file
        except (OSError, AttributeError):
            pass
        self._restore()
        size = self._file.seek(0, os.SEEK_END)
        self._file.seek(0)
        data = self._file.read(min(size, COLLECT_LIMIT))        # read(limit) allocates a buffer of that size: read by actual size
        self.text = ANSI.sub("", data.decode("utf-8", "replace").replace("\r\n", "\n")) + (
            "\n[... C-level output exceeded the collection limit; the rest was not captured ...]" if size > COLLECT_LIMIT else "")
        self._file.close()
        return False


def _truncate(text, limit):
    """(text, omitted): errors are usually at the end, so the first 60% and the last 40% are kept and the middle is dropped."""
    if len(text) <= limit:
        return text, 0
    head = int(limit * 0.6)
    omitted = len(text) - limit
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[-(limit - head):]}", omitted


def _raise_exit(code=0):
    raise SystemExit(code)


@contextlib.contextmanager
def _temp_env(cwd, env, isolate):
    """isolate: cwd and environment variables apply to this run only and sys.path is restored too (like a fresh process: no leaks).
    os._exit behaves like sys.exit: scripts written for a fresh Blender process end with os._exit(0), which would kill the agent here."""
    if not isolate:
        yield
        return
    old_cwd, old_env, old_path, old_exit = os.getcwd(), dict(os.environ), list(sys.path), os._exit
    try:
        os._exit = _raise_exit
        if cwd:
            os.chdir(cwd)
        if env:
            os.environ.update(env)
        yield
    finally:
        os._exit = old_exit
        with contextlib.suppress(OSError):         # the script may have deleted the old cwd: the remaining restores still run
            os.chdir(old_cwd)
        for k in set(os.environ) | set(old_env):
            if os.environ.get(k) != old_env.get(k):
                if k in old_env:
                    os.environ[k] = old_env[k]
                else:
                    del os.environ[k]
        sys.path[:] = old_path


def _drop_modules(before, roots):
    """Modules imported during a run that live under the script folder or cwd are dropped, so the next run sees the sibling file's current content.
    Add-on and library modules are left alone (dropping them breaks registered classes and C extensions)."""
    roots = [os.path.normcase(os.path.abspath(r)) + os.sep for r in roots if r]
    for name in [n for n in sys.modules if n not in before]:
        path = getattr(sys.modules[name], "__file__", None)
        if path and os.path.normcase(os.path.abspath(path)).startswith(tuple(roots)):
            del sys.modules[name]


def _delete_old_outputs():
    for path in OUTPUT_DIR.glob("*.txt"):
        with contextlib.suppress(OSError):
            if time.time() - path.stat().st_mtime > 86400:
                path.unlink()


class _Job:
    __slots__ = ("request", "done", "response", "cancelled", "created")

    def __init__(self, request):
        self.request, self.done, self.response, self.cancelled, self.created = request, threading.Event(), None, False, time.time()


def _default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if isinstance(o, (bytes, bytearray)):
        return f"<bytes {len(o)}>"
    for name in ("tolist", "item"):          # numpy arrays and scalars
        f = getattr(o, name, None)
        if callable(f):
            try:
                return f()
            except Exception:
                pass
    if hasattr(o, "__iter__") and not isinstance(o, (str, dict)):     # mathutils, bpy collections
        return list(itertools.islice(o, 10000))
    return repr(o)[:500]


def _json_value(v):
    """(plain JSON data, truncated). Everything is reduced to plain data here, on the main thread, so the connection thread never touches
    a bpy object. NaN/Infinity are not JSON (JS clients reject them): they become text."""
    try:
        s = json.dumps(v, default=_default)
    except (TypeError, ValueError, RecursionError):
        return repr(v)[:RESULT_LIMIT], False
    if len(s) > RESULT_LIMIT:
        return f"<{type(v).__name__}, {len(s)} characters of JSON; head> " + s[:2000], True
    return json.loads(s, parse_constant=str), False


def _error_info(e):
    tb = e.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename == _SELF:      # the engine's own frames stay out of the trace
        tb = tb.tb_next
    trace = "".join(traceback.format_exception(type(e), e, tb))
    return {"type": type(e).__name__, "message": str(e)[:2000], "trace": trace[-4000:]}


_SELF = _error_info.__code__.co_filename


def _error(kind, message):
    return {"ok": False, "error": {"type": kind, "message": message}}


def _split(code, filename):
    """Splits off the last expression: the body is exec'd and the last expression is eval'd (REPL-like value)."""
    tree = ast.parse(code, filename, "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = compile(ast.Expression(tree.body.pop().value), filename, "eval")
    body = compile(tree, filename, "exec") if tree.body else None
    return body, last


def _dropped(c):
    try:
        r, _, _ = select.select([c], [], [], 0)
        return bool(r) and c.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


class Agent:
    """setup(ns): called when the namespace is created or reset. search(query) -> text. view(width, **opts) -> PNG bytes or (PNG bytes, caption).
    error_hint(exc) -> text | None: an app-specific way out of an error in the code (error.hint). closable=True: can be closed remotely.
    idle_s / owner_pid (loop only): exit when no request arrives for that long or when the owner process dies (no orphan daemons).
    Hooks run on the host's main thread. extra: plain data shown in info.extra (the host updates it on the main thread);
    warn(text): added to the next response as 'warning'. prepare(dict): called before every run, before the code, on the host's main thread,
    with the request's "prepare" (or an empty dict) (e.g. open a file, reset the scene, health check); if it raises, the code does not run."""

    def __init__(self, app, version, lang="python", mode="gui", setup=None, search=None, view=None, error_hint=None,
                 closable=False, hint="", idle_s=0, owner_pid=0, prepare=None, functions=None):
        self.app, self.version, self.lang, self.mode, self.hint = app, str(version), lang, mode, hint
        self._setup, self._search, self._view, self._error_hint, self._closable = setup, search, view, error_hint, closable
        self._prepare, self._output_no, self._functions = prepare, 0, dict(functions or {})
        self.idle_s, self.owner_pid = idle_s, owner_pid
        self.token = secrets.token_hex(16)
        self.port = None
        self.queue = queue.Queue()
        self.running = None                 # (op, start, first line of the code)
        self.stopped = threading.Event()
        self.extra = {}
        self.heartbeat = None               # last turn of the host loop: tells a blocked main thread apart
        self._warnings = []
        self._last_request = time.time()
        self._conn_sem = threading.BoundedSemaphore(CONNECTION_LIMIT)
        self._ns = None
        self._counter = 0
        self._sources = []
        self._sock = None
        self._registry_file = None

    def start(self, direct=False):
        """direct=True: jobs run on a separate thread (no main-thread constraint). Otherwise the host calls pump()/loop()."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):      # no other process may bind the same port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        s.settimeout(0.5)
        self._sock, self.port = s, s.getsockname()[1]
        self._last_request = time.time()
        self._write_registry()
        _delete_old_outputs()
        ACTIVE[self.app] = self
        atexit.register(self.stop)
        threading.Thread(target=self._accept, name="tether-accept", daemon=True).start()
        if direct:
            threading.Thread(target=self.loop, name="tether-work", daemon=True).start()
        print(f"[tether] {self.app} ({self.mode}) listening on 127.0.0.1:{self.port}")
        return self.port

    def stop(self):
        self.stopped.set()
        if ACTIVE.get(self.app) is self:
            del ACTIVE[self.app]
        for action in (lambda: self._sock.close(), lambda: self._registry_file.unlink()):
            try:
                action()
            except Exception:
                pass

    def warn(self, text):
        self._warnings.append(text)

    def namespace(self):
        if self._ns is None:
            self._ns = self._new_namespace()
        return self._ns

    def _new_namespace(self):
        ns = {"__name__": "__main__"}
        if self._setup:
            self._setup(ns)
        return ns

    def _write_registry(self):
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        self._registry_file = REGISTRY_DIR / f"{self.app}-{os.getpid()}.json"
        data = {"tether": PROTOCOL, "app": self.app, "pid": os.getpid(), "proc_filetime": proc_info(os.getpid())[1],
                "port": self.port, "token": self.token, "version": self.version, "mode": self.mode, "lang": self.lang,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        tmp = self._registry_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self._registry_file)

    # --- network side (own threads) ---

    def _accept(self):
        while not self.stopped.is_set():
            try:
                c, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._conn_sem.acquire(blocking=False):      # connection leak/storm: the excess is refused
                c.close()
                continue
            threading.Thread(target=self._connection, args=(c,), name="tether-conn", daemon=True).start()

    def _connection(self, c):
        try:
            c.settimeout(FIRST_LINE_S)
            first = True
            with c, c.makefile("rwb") as f:
                while not self.stopped.is_set():
                    try:
                        line = f.readline(LINE_LIMIT + 1)
                    except OSError:
                        return
                    if not line:
                        return
                    if len(line) > LINE_LIMIT:
                        self._send(f, _error("protocol", "request line too large"))
                        return
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("a request must be an object")
                    except (ValueError, RecursionError) as e:
                        self._send(f, _error("protocol", f"could not parse JSON: {e}"))
                        if first:
                            return                      # first line is not JSON: a foreign protocol (a browser's HTTP request): drop it
                        continue
                    if first:
                        first = False
                        c.settimeout(IDLE_S)
                    response = self._handle(request, c)
                    response["id"] = request.get("id")
                    if not self._send(f, response):
                        return
        finally:
            self._conn_sem.release()

    @staticmethod
    def _send(f, response):
        try:
            try:
                data = json.dumps(response, default=_default, allow_nan=False)
            except ValueError:                      # a non-strict-JSON value leaked: an error response goes out, not an invalid line
                data = json.dumps(_error("protocol", "response could not be converted to strict JSON") | {"id": response.get("id")})
            f.write(data.encode("ascii") + b"\n")
            f.flush()
            return True
        except OSError:
            return False

    def _handle(self, request, c):
        if not hmac.compare_digest(str(request.get("token", "")).encode("utf-8", "replace"), self.token.encode("ascii")):
            return _error("token", "invalid token; re-read the registry file")
        self._last_request = time.time()
        op = request.get("op")
        if op == "info":
            return self._info()                    # answered even while the main thread is busy
        if op not in ("run", "search", "view", "call", "reset", "close"):
            return _error("op", f"unknown op: {op!r}")
        if op == "close" and not self._closable:
            return _error("unsupported", "this app cannot be closed through Tether")
        job = _Job(request)
        self.queue.put(job)
        while not job.done.wait(0.25):
            if _dropped(c):                           # the client gave up: queued work should not run for nothing
                job.cancelled = True
                return _error("cancelled", "connection dropped")
        return job.response

    def _info(self):
        r = self.running
        with self.queue.mutex:
            first = self.queue.queue[0] if self.queue.queue else None
        now = time.time()
        return {"ok": True, "tether": PROTOCOL, "app": self.app, "version": self.version, "mode": self.mode,
                "lang": self.lang, "pid": os.getpid(), "port": self.port,
                "busy": r is not None, "queue": self.queue.qsize(),
                "queue_wait_s": round(now - first.created, 1) if first else 0,
                "heartbeat_s": None if self.heartbeat is None else round(now - self.heartbeat, 1),
                "running": None if r is None else {"op": r[0], "s": round(now - r[1], 1), "code": r[2]},
                "extra": self.extra,
                "capabilities": ["run", "reset"] + (["search"] if self._search else []) + (["view"] if self._view else [])
                + (["call"] if self._functions else []) + (["close"] if self._closable else []),
                "functions": sorted(self._functions), "hint": self.hint}

    # --- execution side (the host's main thread) ---

    def pump(self):
        """Called periodically on the main thread (e.g. by a Blender timer); runs the queued jobs. Returns the number of jobs run."""
        self.heartbeat = time.time()
        n = 0
        while True:
            try:
                job = self.queue.get_nowait()
            except queue.Empty:
                return n
            if not job.cancelled:
                self._process(job)
                n += 1

    def loop(self):
        """Blocks the calling thread: runs jobs until stop(), close, the idle timeout or the owner process's death."""
        last_check = 0
        while not self.stopped.is_set():
            self.heartbeat = time.time()
            if self.heartbeat - last_check > 2:
                last_check = self.heartbeat
                if self.idle_s and self.heartbeat - self._last_request > self.idle_s and not self.queue.qsize():
                    print(f"[tether] no request for {self.idle_s} s: shutting down")
                    break
                if self.owner_pid and not proc_info(self.owner_pid)[0]:
                    print(f"[tether] owner process ({self.owner_pid}) exited: shutting down")
                    break
            try:
                job = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if not job.cancelled:
                self._process(job)
        self.stop()

    def _process(self, job):
        op = job.request["op"]
        first = str(job.request.get("code") or job.request.get("file") or "").strip()[:80].splitlines()
        self.running = (op, time.time(), first[0] if first else "")
        try:
            if op == "run":
                response = self._run(job.request)
            elif op == "search":
                response = self._search_op(job.request)
            elif op == "view":
                response = self._view_op(job.request)
            elif op == "call":
                response = self._call_op(job.request)
            elif op == "reset":
                self._ns = None
                response = {"ok": True}
            else:
                response = {"ok": True}
        except Exception as e:
            response = {"ok": False, "error": _error_info(e)}
        self.running = None
        if self._warnings:
            response["warning"] = " | ".join(self._warnings)
            self._warnings = []
        job.response = response
        job.done.set()
        if op == "close":
            self.stopped.set()

    def _search_op(self, request):
        if not self._search:
            return _error("unsupported", f"{self.app} has no API search; explore with code (dir, help)")
        return {"ok": True, "text": str(self._search(str(request.get("query", ""))))[:OUTPUT_LIMIT]}

    def _view_op(self, request):
        if not self._view:
            return _error("unsupported", f"{self.app} ({self.mode}) does not provide images")
        width, area, options = int(request.get("width") or 1024), request.get("area"), request.get("options")
        kwargs = {**({"area": area} if area else {}), **(options if isinstance(options, dict) else {})}      # host-specific target and options
        out = self._view(width, **kwargs)
        png, caption = out if isinstance(out, tuple) else (out, None)
        return {"ok": True, "png_b64": base64.b64encode(png).decode("ascii"), **({"caption": caption} if caption else {})}

    def _call_op(self, request):
        """Runs a function registered by the host on the main thread; the result is reduced to plain data and the output (Python + C) is captured like run."""
        name, args = request.get("name"), request.get("args") or {}
        f = self._functions.get(name)
        if f is None:
            return _error("unsupported", f"{self.app}: no function '{name}'; available: {', '.join(sorted(self._functions)) or 'none'}")
        if not isinstance(args, dict):
            return _error("request", "args must be a dict")
        collector, c_stream = _Collector(), _CStream()
        t0 = time.perf_counter()
        value, error = None, None
        try:
            with c_stream, contextlib.redirect_stdout(collector), contextlib.redirect_stderr(collector):
                value = f(**args)
        except Exception as e:
            error = _error_info(e)
            try:
                hint = self._error_hint(e) if self._error_hint else None
            except Exception:
                hint = None
            if hint:
                error["hint"] = hint
        raw = collector.text()
        if c_stream.text:
            raw += ("" if not raw or raw.endswith("\n") else "\n") + c_stream.text
        text, omitted = _truncate(raw, OUTPUT_LIMIT)
        response = {"ok": error is None, "output": text, "truncated": bool(omitted), "ms": int((time.perf_counter() - t0) * 1000)}
        if omitted:
            response["full_output"] = self._save_output(raw)
        if error:
            response["error"] = error
        else:
            response["result"], response["result_truncated"] = _json_value(value)
        return response

    def _save_source(self, code):
        """Source lines show up in traces; every call gets its own name so a function defined in an old call shows its own lines."""
        self._counter += 1
        name = f"<tether-{self._counter}>"
        linecache.cache[name] = (len(code), None, code.splitlines(True), name)
        self._sources.append(name)
        if len(self._sources) > SOURCE_COUNT:
            linecache.cache.pop(self._sources.pop(0), None)
        return name

    def _save_output(self, text):
        """The full text of a truncated output, so the middle can be searched later."""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._output_no += 1
        path = OUTPUT_DIR / f"{self.app}-{os.getpid()}-{self._output_no}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _run(self, request):
        file = request.get("file")
        if file:
            code, filename = Path(file).read_text(encoding="utf-8-sig"), str(file)
        else:
            code = request.get("code")
            if not isinstance(code, str):
                return _error("request", "code or file required")
            filename = self._save_source(code)
        try:
            body, last = _split(code, filename)
        except SyntaxError as e:        # only the offending line and caret instead of a trace; stdlib frames are noise
            return {"ok": False, "output": "", "truncated": False, "ms": 0,
                    "error": {"type": type(e).__name__, "message": str(e)[:2000],
                              "trace": "".join(traceback.format_exception_only(type(e), e))}}
        args, cwd, env, grep = request.get("args"), request.get("cwd"), request.get("env"), request.get("grep")
        if args is not None and not isinstance(args, list):
            return _error("request", "args must be a list")
        if cwd is not None and not (isinstance(cwd, str) and os.path.isdir(cwd)):
            return _error("request", f"cwd is not a directory: {cwd!r}")
        if env is not None and not (isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
            return _error("request", "env must be a {name: text} dict")
        try:
            grep = None if grep is None else re.compile(grep)
        except (re.error, TypeError) as e:
            return _error("request", f"grep must be a valid regular expression: {e}")
        limit = max(200, min(int(request.get("max_output") or OUTPUT_LIMIT), 2_000_000))
        prepare = request.get("prepare")
        if prepare and not self._prepare:
            return _error("unsupported", f"{self.app} does not support prepare (blend, clean)")
        t0 = time.perf_counter()
        c_prepare = _CStream()
        if prepare:
            with c_prepare:            # C-level warnings while opening a file (missing library, texture) belong in the output too
                self._prepare(prepare)    # e.g. open a file: the namespace is taken afterwards, because loading a file resets it
        elif self._prepare:
            self._prepare({})          # called even when the request asks for no preparation: the host can run its own health check
        fresh = request.get("fresh")
        if fresh is None:
            fresh = bool(file)          # a script is a separate run (like blender --python): earlier variables must not mix in or leak into the persistent namespace
        ns = self._new_namespace() if fresh else self.namespace()
        ns.pop("result", None)
        if file:
            ns["__file__"] = filename
        else:
            ns.pop("__file__", None)
        collector, c_stream = _Collector(), _CStream()
        old_argv, old_modules = sys.argv, set(sys.modules)
        if args is not None:             # same sys.argv as "blender --python x.py -- <args>"
            sys.argv = [old_argv[0] if old_argv else "", "--", *map(str, args)]
        value, has_last, error, exit_code = None, False, None, None
        try:
            with _temp_env(cwd, env, bool(file or cwd or env)), c_stream, \
                    contextlib.redirect_stdout(collector), contextlib.redirect_stderr(collector):
                if body:
                    exec(body, ns)
                if last:
                    value, has_last = eval(last, ns), True
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else int(e.code is not None)
            if exit_code:                   # sys.exit(0) is the script ending on its own: success
                error = {"type": "SystemExit", "message": f"code called sys.exit/os._exit({e.code!r}); the app did not close", "trace": ""}
        except Exception as e:
            error = _error_info(e)
            try:
                hint = self._error_hint(e) if self._error_hint else None
            except Exception:
                hint = None
            if hint:
                error["hint"] = hint
        finally:
            sys.argv = old_argv
            if file:
                _drop_modules(old_modules, [os.path.dirname(filename), cwd])
        raw = collector.text()
        c_text = c_prepare.text + c_stream.text
        if c_text:                      # C-level output follows the Python output (the order between the two streams is not known)
            raw += ("" if not raw or raw.endswith("\n") else "\n") + c_text
        text, lines, matched = raw, [], []
        if grep:
            lines = raw.splitlines()
            matched = [s for s in lines if grep.search(s)]
            text = "\n".join(matched)
        text, omitted = _truncate(text, limit)
        response = {"ok": error is None, "output": text, "truncated": bool(omitted), "ms": int((time.perf_counter() - t0) * 1000)}
        if file:
            response["file"] = filename
        if exit_code is not None:
            response["exit_code"] = exit_code
        if grep:
            response["grep_matched"], response["grep_total"] = len(matched), len(lines)
        if omitted:
            response["full_output"] = self._save_output(raw)
        if error:
            response["error"] = error
        elif "result" in ns:
            response["result"], response["result_truncated"] = _json_value(ns["result"])
        elif has_last and value is not None:
            response["result"], response["result_truncated"] = _json_value(value)
        return response
