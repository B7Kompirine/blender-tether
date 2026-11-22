"""Tether client and command line: lists the registered apps, runs code or scripts in one of them (cwd, env, blend, clean, grep), takes scene
reports and images (angles, shading, camera, frame strip), opens and closes an app without a visible console window, and keeps a call log (stats).
tether.mcp_server and your own scripts use it.

App selector: "name", "name#pid" or "name:mode" (mode: gui | background). A selector is required when several instances with the same name run."""
import argparse
import base64
import collections
import glob
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .agent import HOME, REGISTRY_DIR, proc_info

AGENT_DIR = Path(__file__).resolve().parent / "agent"
STATS_DIR = Path(os.environ.get("TETHER_STATS_DIR") or HOME / "stats")
_SEEN = {}                      # (app, pid) -> hashes of code lines already sent to it; only used for the repeat ratio, code is never written anywhere


class TetherError(Exception):
    pass


def _send(k, op, timeout, **args):
    with socket.create_connection(("127.0.0.1", k["port"]), timeout=min(timeout, 5)) as s:
        s.settimeout(timeout)
        f = s.makefile("rwb")
        f.write(json.dumps({"id": 1, "token": k["token"], "op": op, **args}).encode("utf-8") + b"\n")
        f.flush()
        line = f.readline()
    if not line:
        raise ConnectionError("the agent closed the connection without answering")
    return json.loads(line)


def apps(prune=True):
    """Live agents. state: "ok" | "no_response" (the process exists but the agent does not answer: a long operation or a hang).
    A record whose process is dead (or whose pid was reused), whose connection is refused, or that belongs to another agent is stale and is deleted."""
    found = []
    for path in sorted(REGISTRY_DIR.glob("*.json")) if REGISTRY_DIR.exists() else []:
        try:
            k = json.loads(path.read_text(encoding="utf-8-sig"))      # -sig: a PowerShell/.NET agent may write a BOM
        except (OSError, ValueError):
            continue                                # it may be mid-write: do not delete
        alive, ft = proc_info(k.get("pid", 0))
        if not alive or (k.get("proc_filetime") and ft and ft != k["proc_filetime"]):   # no network needed: Windows refuses a closed loopback port only after ~2 s
            if prune:
                path.unlink(missing_ok=True)
            continue
        try:
            info = _send(k, "info", 3.5)            # long enough to see the refusal (~2 s)
            if not info.get("ok") or info.get("pid") != k["pid"]:
                raise ConnectionRefusedError("another agent took the port")
            k["info"], k["state"] = info, "ok"
        except ConnectionRefusedError:
            if prune:
                path.unlink(missing_ok=True)
            continue
        except (OSError, ValueError, KeyError):     # including timeouts
            k["info"], k["state"] = None, "no_response"
        found.append(k)
    return found


def _label(k):
    return f"{k['app']}#{k['pid']} ({k['mode']})"


def resolve(selector, listing=None):
    name, mode, pid = str(selector), None, None
    if "#" in name:
        name, pid = name.split("#", 1)
    elif ":" in name:
        name, mode = name.split(":", 1)
    listing = apps() if listing is None else listing
    hits = [k for k in listing if k["app"] == name and (pid is None or str(k["pid"]) == pid) and (mode is None or k["mode"] == mode)]
    if not hits:
        raise TetherError(f"'{selector}' is not running. Running: " + (", ".join(_label(k) for k in listing) or "none") + ". Use open to start it.")
    if len(hits) > 1:
        raise TetherError(f"'{name}' has several instances: " + ", ".join(_label(k) for k in hits) + ". Give 'name#pid' or 'name:mode'.")
    return hits[0]


def _log_call(k, op, args, y, t0):
    """One JSON line per call: sizes, duration, error type, number of code lines sent before. Code and output text are not written; TETHER_STATS=0 turns it off."""
    if op == "info" or os.environ.get("TETHER_STATS") == "0":
        return
    key = (k["app"], k["pid"])
    if op == "reset":
        _SEEN.pop(key, None)
    code = args.get("code") or ""
    digests = [hashlib.sha1(s.strip().encode("utf-8")).hexdigest()[:12] for s in code.splitlines() if len(s.strip()) > 8]
    seen = _SEEN.setdefault(key, set())
    repeat = sum(1 for h in digests if h in seen)
    seen.update(digests)
    try:
        size = len(code) or (Path(args["file"]).stat().st_size if args.get("file") else 0)
    except OSError:
        size = 0
    output = len(y.get("output") or "") + len(y.get("png_b64") or "") + (len(json.dumps(y["result"], ensure_ascii=False)) if "result" in y else 0)
    record = {"t": int(t0), "app": k["app"], "op": op, "ok": bool(y.get("ok")), "ms": int((time.time() - t0) * 1000), "code": size,
              "lines": len(digests), "repeat": repeat, "output": output, "truncated": bool(y.get("truncated")), "error": (y.get("error") or {}).get("type", "")}
    try:
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATS_DIR / (time.strftime("%Y-%m", time.localtime(t0)) + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass                       # the log never breaks a call


def stats(days=30):
    """Summary of the call log: counts, error types, duration, code size, ratio of repeated lines, truncated outputs."""
    cutoff, records = time.time() - days * 86400, []
    for path in sorted(STATS_DIR.glob("*.jsonl")) if STATS_DIR.exists() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("t", 0) >= cutoff:
                records.append(r)
    if not records:
        return f"no calls recorded in the last {days} days ({STATS_DIR})"

    def count(rows, field):
        return ", ".join(f"{a or '-'} {n}" for a, n in collections.Counter(r[field] for r in rows).most_common(6))

    def pct(values, p):
        s = sorted(values)
        return s[min(len(s) - 1, int(len(s) * p))]
    failed = [r for r in records if not r["ok"]]
    ms = [r["ms"] for r in records]
    lines = [f"last {days} days: {len(records)} calls ({STATS_DIR})", f"apps: {count(records, 'app')}", f"ops: {count(records, 'op')}",
             f"errors: {len(failed)}/{len(records)} ({100 * len(failed) / len(records):.1f}%)" + (f": {count(failed, 'error')}" if failed else ""),
             f"time ms: median {pct(ms, 0.5)}, p95 {pct(ms, 0.95)}, max {max(ms)}"]
    coded = [r for r in records if r["code"]]
    if coded:
        total, repeat = sum(r["lines"] for r in coded), sum(r["repeat"] for r in coded)
        lines.append(f"code: {len(coded)} calls, {sum(r['code'] for r in coded)} characters total (mean {sum(r['code'] for r in coded) // len(coded)}); "
                     f"lines already sent before {repeat}/{total} ({100 * repeat / max(total, 1):.0f}%; an upper bound, namespace resets are not known)")
    output = [r["output"] for r in records]
    lines.append(f"output: {sum(output)} characters total, p95 {pct(output, 0.95)}; truncated in {sum(1 for r in records if r['truncated'])} calls")
    return "\n".join(lines)


def request(selector, op, timeout=60, **args):
    """Returns the response dict (an error in the code is also a response). Connectivity problems raise TetherError."""
    k = resolve(selector)
    t0 = time.time()
    try:
        y = _send(k, op, timeout, **args)
    except TimeoutError:
        _log_call(k, op, args, {"error": {"type": "timeout"}}, t0)
        raise TetherError(f"{selector}: no answer within {timeout} s. The job may still be running (long operation, modal dialog); once it finishes its "
                          f"effects remain but its result is not returned by this call. 'status' shows whether it is busy.") from None
    except (OSError, ValueError) as e:
        _log_call(k, op, args, {"error": {"type": "connection"}}, t0)
        raise TetherError(f"{selector}: connection lost ({e}); the app may have closed or crashed.") from None
    _log_call(k, op, args, y, t0)
    return y


def _file_path(path, cwd, what):
    """Absolute path. A relative path is looked up in cwd first, then in the folders of $TETHER_SCRIPT_DIRS (separated by os.pathsep), then relative to this process's cwd."""
    p = Path(path)
    extra = [Path(d) for d in os.environ.get("TETHER_SCRIPT_DIRS", "").split(os.pathsep) if d.strip()]
    candidates = [p] if p.is_absolute() else [*([Path(cwd) / p] if cwd else []), *(d / p for d in extra), Path.cwd() / p]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    raise TetherError(f"{what} not found: {path}. Looked in: " + "; ".join(map(str, candidates)) + ".")


def run(selector, code=None, file=None, args=None, fresh=None, timeout=60, max_output=None, cwd=None, blend=None, clean=False, env=None, grep=None):
    """fresh: None = the agent's default (code uses the persistent namespace, a file a fresh one); True/False chooses explicitly.
    A file runs like a fresh process: cwd, env and sys.path apply to that run only. blend/clean (background Blender): open that file first /
    return to the factory scene first. grep: only the output lines matching this regular expression are kept."""
    if cwd and not Path(cwd).is_dir():
        raise TetherError(f"cwd is not a directory: {cwd}")
    req = {"file": _file_path(file, cwd, "script")} if file else {"code": code}
    if cwd:
        req["cwd"] = str(Path(cwd).resolve())
    if args is not None:
        req["args"] = list(args)
    if fresh is not None:
        req["fresh"] = bool(fresh)
    if max_output:
        req["max_output"] = max_output
    if env:
        req["env"] = {str(k): str(v) for k, v in env.items()}
    if grep:
        req["grep"] = grep
    prepare = {**({"blend": _file_path(blend, cwd, "blend file")} if blend else {}), **({"clean": True} if clean else {})}
    if prepare:
        req["prepare"] = prepare
    return request(selector, "run", timeout=timeout, **req)


def call(selector, name, args=None, timeout=60):
    """Calls a function registered by the app (response dict: ok, result, output, error). An error raises TetherError."""
    y = request(selector, "call", timeout=timeout, name=name, args=args or {})
    if not y.get("ok"):
        h = y.get("error") or {}
        raise TetherError(f"{h.get('type')}: {h.get('message')}" + (f"\nHINT: {h['hint']}" if h.get("hint") else ""))
    return y


def report(selector, fields=None, query="", root="", limit=20, timeout=60):
    """Scene report (text): object counts, the requested fields, health checks. The field names are listed in blender_report.py."""
    return call(selector, "report", {"fields": fields, "query": query, "root": root, "limit": limit}, timeout)["result"]


def api_search(selector, query, timeout=30):
    y = request(selector, "search", timeout=timeout, query=query)
    if not y.get("ok"):
        raise TetherError(y["error"]["message"])
    return y["text"]


def view(selector, width=1024, timeout=60, area=None, caption=False, **options):
    """PNG bytes (caption=True: (PNG, caption)). area: app-specific target (windowed Blender: 'window' or an area type such as NODE_EDITOR).
    options: the app's image options (Blender: mode, shading, target, angles, distance, frames, frame_count, look, image); empty values are dropped."""
    options = {k: v for k, v in options.items() if v not in (None, "", [], 0)}
    y = request(selector, "view", timeout=timeout, width=width, **({"area": area} if area else {}), **({"options": options} if options else {}))
    if not y.get("ok"):
        raise TetherError(y["error"]["message"])
    png = base64.b64decode(y["png_b64"])
    return (png, y.get("caption", "")) if caption else png


def format_run(y):
    """Turns a run response into readable text: (text, is_error)."""
    p = []
    if y.get("output"):
        p.append(y["output"].rstrip("\n"))
    h = y.get("error")
    if h:
        trace = (h.get("trace") or "").rstrip().splitlines()
        if trace and trace[-1].split(":", 1)[0].endswith(str(h.get("type"))):       # the exception line is already in the header
            trace.pop()
        p.append(f"ERROR {h.get('type')}: {h.get('message')}" + ("\n" + "\n".join(trace) if trace else ""))
        if h.get("hint"):
            p.append("HINT: " + h["hint"])
        if y.get("full_output"):
            p.append("FULL OUTPUT: " + y["full_output"])
        if y.get("warning"):
            p.append("WARNING: " + y["warning"])
        return "\n".join(p), True
    if "result" in y:
        p.append("-> " + json.dumps(y["result"], ensure_ascii=False, default=repr))
    notes = [f"{y.get('ms', 0)} ms"]
    if y.get("file"):
        notes.append("script " + y["file"])
    if "exit_code" in y:
        notes.append(f"exit {y['exit_code']}")
    if "grep_total" in y:
        notes.append(f"grep {y['grep_matched']}/{y['grep_total']} lines")
    if y.get("truncated"):
        notes.append("output truncated" + (f", full text in {y['full_output']}" if y.get("full_output") else ""))
    if y.get("result_truncated"):
        notes.append("result truncated")
    p.append("[" + "; ".join(notes) + "]")
    if y.get("warning"):
        p.append("WARNING: " + y["warning"])
    return "\n".join(p), False


# --- opening apps ---

def _launch_detached(argv):
    """Starts a process outside the caller's container and returns its pid.
    Windows: under the Claude desktop app (MSIX) a process that writes to AppData has its writes redirected into the package's private folder; a process
    created through WMI runs outside the container and sees the real install. blender.exe is a console application: a WMI start without flags opens a
    visible terminal window and steals focus. ShowWindow=0 starts it with the console hidden and the handles valid (Blender's C logs are captured);
    DETACHED_PROCESS would cut those logs and WMI rejects CREATE_NO_WINDOW (ReturnValue 21). A windowed Blender keeps its main window visible
    (the official blender-launcher.exe uses the same startup info)."""
    if os.name != "nt":
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True).pid
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
                        "(Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$env:TETHER_CMD; ProcessStartupInformation=$si}).ProcessId"],
                       env={**os.environ, "TETHER_CMD": subprocess.list2cmdline(argv)}, capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL,       # an inherited stdin from an MCP stdio process can hang the child
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))       # no PowerShell window when the parent has no console
    pid = int(r.stdout.strip() or 0)
    if not pid:
        raise TetherError("could not start the process: " + (r.stderr or r.stdout).strip()[:300])
    return pid


def find_blender():
    """Path of the Blender executable: $TETHER_BLENDER, then PATH, then the usual install locations (newest first)."""
    given = os.environ.get("TETHER_BLENDER")
    if given:
        if not Path(given).is_file():
            raise TetherError(f"TETHER_BLENDER is not a file: {given}")
        return given
    found = shutil.which("blender")
    if found:
        return found
    patterns = ([r"C:\Program Files\Blender Foundation\Blender *\blender.exe", r"C:\Program Files (x86)\Blender Foundation\Blender *\blender.exe",
                 r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe"] if os.name == "nt" else
                ["/Applications/Blender.app/Contents/MacOS/Blender", "/usr/bin/blender", "/snap/bin/blender", "/opt/blender*/blender"])
    candidates = sorted((p for pattern in patterns for p in glob.glob(pattern)), reverse=True)
    if not candidates:
        raise TetherError("Blender not found. Set TETHER_BLENDER to the full path of the Blender executable or put it on PATH.")
    return candidates[0]


def _blender_command(file, background, clean, addons, owner_pid, idle_min):
    if clean is None:
        clean = background            # a background daemon starts clean by default: no side effects from user add-ons (a port opened by another add-on, wheel sync), 0.5 s
    if addons is None:
        addons = [a.strip() for a in os.environ.get("TETHER_ADDONS", "").split(",") if a.strip()] if background else []
    if idle_min is None:
        idle_min = 30 if background else 0
    cmd = [find_blender()] + (["-b"] if background else []) + (["--factory-startup"] if clean else [])
    if file:
        cmd.append(str(file))
    cmd += ["--python", str(AGENT_DIR / "blender.py"), "--", "--daemon" if background else "--gui"]
    if background:                                # add-ons, owner and idle timeout only apply to the background daemon
        for a in addons:
            cmd += ["--addon", a]
        if owner_pid:
            cmd += ["--owner-pid", str(owner_pid)]
        if idle_min:
            cmd += ["--idle-s", str(int(idle_min * 60))]
    if os.environ.get("TETHER_HOME"):       # the WMI process does not inherit the environment: pass the state folder to the agent as an argument
        cmd += ["--home-dir", os.environ["TETHER_HOME"]]
    return cmd


LAUNCHERS = {"blender": _blender_command}


def open_app(app, file="", background=False, clean=None, addons=None, owner_pid=0, idle_min=None, wait=120):
    """Starts the app outside the caller's container and waits until its agent registers. {"selector", "pid", "mode", "version", "startup_s"}.
    Blender in the background: clean by default (no user settings, no add-ons unless TETHER_ADDONS or addons), closes after 30 minutes without
    requests or when owner_pid dies (no orphans)."""
    recipe = LAUNCHERS.get(app)
    if not recipe:
        raise TetherError(f"no launch recipe for '{app}'. Known: {', '.join(LAUNCHERS)}. If you started it yourself, the agent must be loaded in it.")
    if file and not Path(file).is_file():      # a value starting with "-" must not be taken for a command-line option
        raise TetherError(f"file not found: {file}")
    t0 = time.time()
    pid = _launch_detached(recipe(file, background, clean, addons, owner_pid, idle_min))
    log = HOME / "startup" / f"{app}-{pid}.log"     # if the agent crashes at startup it writes the reason here (a WMI process has no stdio)
    while time.time() - t0 < wait:
        for k in apps(prune=False):
            if k["app"] == app and k["pid"] == pid:
                return {"selector": f"{app}#{pid}", "pid": pid, "mode": k["mode"], "version": k["version"], "startup_s": round(time.time() - t0, 1)}
        if log.exists():
            raise TetherError(f"{app} (pid {pid}): the agent failed to start:\n" + log.read_text(encoding="utf-8", errors="replace")[-1500:])
        if not proc_info(pid)[0]:
            raise TetherError(f"{app} (pid {pid}) exited before the agent registered.")
        time.sleep(0.5)
    raise TetherError(f"{app} (pid {pid}) did not register an agent within {wait} s; the process is running, the agent may not have loaded.")


def _status_line(k):
    i = k.get("info")
    if not i:
        return f"{_label(k)}  NO RESPONSE (the process exists; long operation or hang)"
    state = f"busy ({i['running']['s']} s: {i['running']['code']})" if i["busy"] else "idle"
    if (i.get("extra") or {}).get("modal_operators"):
        state += f"  MODAL OPERATOR RUNNING ({', '.join(i['extra']['modal_operators'])}): jobs wait until it ends"
    elif i.get("heartbeat_s") is not None and i["heartbeat_s"] > 5 and not i["busy"]:
        state += f"  (the main thread has not pumped for {i['heartbeat_s']} s: blocked or a modal window)"
    return f"{_label(k)}  {i['version']}  {i['lang']}  {state}  queue={i['queue']}  [{', '.join(i['capabilities'])}]"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="tether", description="Drive local apps from the command line: status, run, scene, view, open, close.")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="running agents")
    c = sub.add_parser("run", help="run code in the app")
    c.add_argument("app")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("-c", "--code")
    g.add_argument("-f", "--file")
    c.add_argument("-a", "--args", nargs="*", help="passed to the script's sys.argv after '--'")
    y = c.add_mutually_exclusive_group()
    y.add_argument("--fresh", dest="fresh", action="store_true", default=None, help="fresh namespace (the default for -f)")
    y.add_argument("--persistent", dest="fresh", action="store_false", help="persistent namespace (the default for -c)")
    c.add_argument("--timeout", type=int, default=60)
    c.add_argument("--cwd", help="the script runs in this folder; relative script and blend paths are looked up here first")
    c.add_argument("--blend", help="open this .blend before running (background Blender)")
    c.add_argument("--clean", action="store_true", help="return to the factory scene before running (background Blender)")
    c.add_argument("--env", action="append", metavar="NAME=VALUE", help="environment variable for this run only; repeatable")
    c.add_argument("--grep", help="keep only the output lines matching this regular expression")
    q = sub.add_parser("scene", help="the app's scene report (Blender: objects, size, topology, GTA checks)")
    q.add_argument("app")
    q.add_argument("--fields", nargs="*", help="location size ground rotation scale parent children hidden details materials modifiers animation topology weights gta settings | placement contents health all")
    q.add_argument("--query", default="", help="objects whose name contains this text")
    q.add_argument("--root", default="", help="the hierarchy of this object")
    q.add_argument("--limit", type=int, default=20)
    sub.add_parser("stats", help="summary of the call log (code and output text are not in the log)").add_argument("--days", type=int, default=30)
    s = sub.add_parser("api", help="search the app's API")
    s.add_argument("app")
    s.add_argument("query")
    r = sub.add_parser("view", help="image (PNG)")
    r.add_argument("app")
    r.add_argument("output")
    r.add_argument("--width", type=int, default=1024, help="long side, pixels")
    r.add_argument("--area", help="windowed Blender: 'window' (the whole window) or an area type (NODE_EDITOR, OUTLINER...); empty = the 3D view")
    r.add_argument("--mode", help="viewport | camera | angles | frames (background default: angles)")
    r.add_argument("--shading", help="solid | texture | material | rendered | wireframe | xray")
    r.add_argument("--target", nargs="*", help="object names to frame (with their children)")
    r.add_argument("--angles", nargs="*", help="front back left right top bottom iso, or an x,y,z direction (at most 6)")
    r.add_argument("--distance", type=float, help="metres from the target's centre to the eye")
    r.add_argument("--frames", nargs="*", type=int, help="mode=frames: frame numbers")
    r.add_argument("--frame-count", type=int, help="mode=frames: how many frames to sample (2-12)")
    r.add_argument("--look", help="mode=frames: camera, an angle name or x,y,z")
    r.add_argument("--image", help="show this image instead of the scene: 'Render Result', an image in the file, or a file path")
    o = sub.add_parser("open", help="start the app")
    o.add_argument("app")
    o.add_argument("file", nargs="?", default="")
    o.add_argument("--background", action="store_true")
    t = o.add_mutually_exclusive_group()
    t.add_argument("--clean", dest="clean", action="store_true", default=None, help="start without user settings (--factory-startup; the default in the background)")
    t.add_argument("--user-settings", dest="clean", action="store_false", help="start with the user's add-ons and settings")
    o.add_argument("--addon", action="append", help="add-on to enable in the background (default: $TETHER_ADDONS); repeatable")
    o.add_argument("--owner-pid", type=int, default=0, help="the background daemon closes when this process dies")
    o.add_argument("--idle-min", type=int, help="the background daemon closes after this many minutes without requests (default 30; 0 = never)")
    for name in ("close", "reset", "info"):
        sub.add_parser(name).add_argument("app")
    a = ap.parse_args(argv)
    try:
        if a.command == "status":
            listing = apps()
            print("\n".join(_status_line(k) for k in listing) or f"no agent running (can be opened: {', '.join(LAUNCHERS)})")
        elif a.command == "run":
            env = {}
            for x in a.env or []:
                name, sep, value = x.partition("=")
                if not name or not sep:
                    ap.error(f"--env must look like NAME=VALUE: {x}")
                env[name] = value
            y = run(a.app, code=a.code, file=a.file, args=a.args, fresh=a.fresh, timeout=a.timeout, cwd=a.cwd, blend=a.blend,
                    clean=a.clean, env=env, grep=a.grep)
            text, failed = format_run(y)
            print(text, file=sys.stderr if failed else sys.stdout)
            return 1 if failed else 0
        elif a.command == "scene":
            print(report(a.app, a.fields, a.query, a.root, a.limit))
        elif a.command == "stats":
            print(stats(a.days))
        elif a.command == "api":
            print(api_search(a.app, a.query))
        elif a.command == "view":
            def direction(x):
                return [float(p) for p in x.split(",")] if "," in x else x
            png, caption = view(a.app, a.width, area=a.area, caption=True, mode=a.mode, shading=a.shading, target=a.target,
                                angles=[direction(x) for x in a.angles or []], distance=a.distance, frames=a.frames,
                                frame_count=a.frame_count, look=direction(a.look) if a.look else None, image=a.image)
            Path(a.output).write_bytes(png)
            print(f"{a.output} ({len(png)} bytes)" + (f"\n{caption}" if caption else ""))
        elif a.command == "open":
            print(json.dumps(open_app(a.app, a.file, a.background, a.clean, a.addon, a.owner_pid, a.idle_min), ensure_ascii=False))
        else:
            print(json.dumps(request(a.app, a.command), ensure_ascii=False))
    except TetherError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
