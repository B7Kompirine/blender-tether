"""Same-machine comparison of Tether with the `blender-mcp` server (PyPI package of the same name, "MCP for Blender" add-on). Both are driven through the
MCP stdio protocol exactly as an agent does, with the same tasks, and the wall-clock time of every call is recorded together with a sample of the answer.
It makes no claim beyond what it measures: the tools return different content (a text report is not a screenshot), so read the samples next to the times.

Usage: python tools/compare.py [--out tools/out] [--old-blender PATH] [--tether background|window|both] [--skip-old] [--skip-assets]
Needs: mcp, Pillow, Blender, `uvx` with the `blender-mcp` package and its Blender add-on enabled in the user preferences (the old tool needs a windowed
Blender; the script starts one minimized, without taking focus, and closes it at the end).
"""
import argparse
import asyncio
import base64
import io
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from benchmark import SCENE_CODE  # noqa: E402
from tether import client as tclient  # noqa: E402

PREFIX = "import bpy, math\nD = bpy.data\nC = bpy.context\n"
BUILD = PREFIX + SCENE_CODE + "\nprint(len(D.objects))\n"
OLD_PORT = 9876


class Server:
    """One MCP server over stdio; call() returns (text, image sizes, ms, is_error)."""

    def __init__(self, command, args, env=None):
        self.params = StdioServerParameters(command=command, args=args, env=env)

    async def __aenter__(self):
        self._cm = stdio_client(self.params)
        read, write = await self._cm.__aenter__()
        self._s = ClientSession(read, write)
        await self._s.__aenter__()
        await self._s.initialize()
        return self

    async def __aexit__(self, *a):
        await self._s.__aexit__(*a)
        await self._cm.__aexit__(*a)

    async def call(self, name, **a):
        t = time.perf_counter()
        y = await self._s.call_tool(name, {k: v for k, v in a.items() if v is not None})
        ms = (time.perf_counter() - t) * 1000
        text = "\n".join(c.text for c in y.content if c.type == "text")
        sizes = [Image.open(io.BytesIO(base64.b64decode(c.data))).size for c in y.content if c.type == "image"]
        return text, sizes, ms, bool(getattr(y, "is_error", None) or getattr(y, "isError", None))


def stats(ms):
    ms = sorted(ms)
    if not ms:
        return {"n": 0}
    return {"n": len(ms), "median": round(statistics.median(ms), 1), "p95": round(ms[min(len(ms) - 1, int(len(ms) * .95))], 1),
            "min": round(ms[0], 1), "max": round(ms[-1], 1)}


async def bench(call, n, warm=1):
    """Runs `call` warm+n times; the warm-up calls are dropped. Returns the timing of the successful calls, the error count and one sample answer."""
    times, errors, sample, first = [], 0, {}, None
    for i in range(warm + n):
        text, sizes, ms, err = await call()
        first = ms if first is None else first
        bad = err or text.lower().startswith("error")
        if i >= warm:
            errors += bad
            if not bad:
                times.append(ms)
        sample = {"text": text[:300], "images": sizes, "error": bad}
    return {**stats(times), "first_ms": round(first, 1), "errors": errors, "sample": sample}


def tasks(kind, sel, assets):
    """The task list for one tool: (name, repeats, async call)."""
    if kind == "old":
        run = lambda code: (lambda: sel.call("execute_blender_code", code=code, user_prompt="benchmark"))      # noqa: E731
        t = [("ping", 30, run("print(1 + 1)")), ("build_scene", 5, run(BUILD)),
             ("scene_info", 20, lambda: sel.call("get_scene_info", user_prompt="benchmark")),
             ("image_1024", 10, lambda: sel.call("get_viewport_screenshot", max_size=1024, user_prompt="benchmark")),
             ("error_raise", 3, run("raise ValueError('boom')")), ("error_name", 3, run("print(undefined_name)"))]
        if assets:
            t += [("asset_search", 3, lambda: sel.call("search_polyhaven_assets", asset_type="textures", categories="floor", user_prompt="benchmark")),
                  ("asset_import", 1, lambda: sel.call("download_polyhaven_asset", asset_id="aerial_asphalt_01", asset_type="textures", resolution="1k",
                                                       user_prompt="benchmark"))]
        return t
    app = sel.selector
    run = lambda code: (lambda: sel.call("run", app=app, code=code))      # noqa: E731
    t = [("ping", 30, run("print(1 + 1)")), ("build_scene", 5, run(BUILD)),
         ("scene_info", 20, lambda: sel.call("scene", app=app)),
         ("image_1024", 10, lambda: sel.call("view", app=app, width=1024, **({"mode": "viewport"} if sel.windowed else {"angles": ["iso"]}))),
         ("error_raise", 3, run("raise ValueError('boom')")), ("error_name", 3, run("print(undefined_name)"))]
    if not sel.windowed:
        t.append(("image_4_angles_1024", 10, lambda: sel.call("view", app=app, width=1024)))
    if assets:
        t += [("asset_search", 3, lambda: sel.server.call("asset_search", source="polyhaven", kind="texture", category="floor")),
              ("asset_import", 1, lambda: sel.call("asset_import", app=app, source="polyhaven", id="aerial_asphalt_01", kind="texture"))]
    return t


class Bound:
    """A tether session bound to one opened Blender, so the task list can call its tools with the app selector."""

    def __init__(self, server, selector, windowed):
        self.server, self.selector, self.windowed = server, selector, windowed

    async def call(self, name, **a):
        return await self.server.call(name, **a)


async def run_tasks(label, kind, sel, assets, out):
    res = {}
    for name, n, call in tasks(kind, sel, assets):
        res[name] = await bench(call, n, warm=0 if name.startswith(("error", "asset")) else 1)
        r = res[name]
        print(f"  {label:18} {name:20} n={r['n']:>2} median={r.get('median', '-')} ms errors={r['errors']}")
        if name.startswith("image") and r["sample"].get("images"):
            pass
    return res


def port_open(port):
    with socket.socket() as s:
        s.settimeout(.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch_old_blender(blender):
    """Blender with the user's preferences, minimized without activation (SW_SHOWMINNOACTIVE) so it does not take focus."""
    if port_open(OLD_PORT):
        raise RuntimeError("port %d is already in use: another Blender is running" % OLD_PORT)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7
    t = time.perf_counter()
    p = subprocess.Popen([blender], startupinfo=si, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    while not port_open(OLD_PORT):
        if time.perf_counter() - t > 90 or p.poll() is not None:
            p.kill()
            raise RuntimeError("the old add-on never started listening on port %d" % OLD_PORT)
        time.sleep(.25)
    return p, time.perf_counter() - t


async def main(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    blender = a.old_blender or tclient.find_blender()
    result = {"date": time.strftime("%Y-%m-%d"), "tasks": "same code and the same MCP stdio harness for both; first call of each task is a dropped warm-up"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("TETHER_")}
    async with Server(sys.executable, [str(ROOT / "tether" / "mcp_server.py")], env) as tserver:
        for mode in (["background", "window"] if a.tether == "both" else [a.tether]):
            text, _, ms, _ = await tserver.call("open", app="blender", background=(mode == "background"))
            d = json.loads(text)
            print(f"tether {mode}: ready in {ms / 1000:.1f} s (agent reports {d.get('startup_s')} s)")
            sel = Bound(tserver, d["selector"], mode == "window")
            result[f"tether_{mode}"] = {"ready_s": round(ms / 1000, 2), "version": d.get("version"), "tasks": await run_tasks("tether " + mode, "new", sel, not a.skip_assets, out)}
            if mode == "background":
                await tserver.call("close", app=d["selector"])
            else:                       # `close` only closes a background agent: end the window we opened so the old add-on gets a Blender of its own
                subprocess.run(["taskkill", "/PID", d["selector"].split("#")[-1], "/F"], capture_output=True)
            await asyncio.sleep(3)
    if not a.skip_old:
        proc, ready = launch_old_blender(blender)
        try:
            print(f"old add-on: listening after {ready:.1f} s (a Blender window with the add-on, started minimized)")
            async with Server("uvx", ["blender-mcp"], os.environ.copy()) as old:
                if not a.skip_assets:       # the add-on keeps its asset commands off until the panel's Poly Haven box is ticked: do what the panel does
                    await old.call("execute_blender_code", code="import bpy\nbpy.context.scene.blendermcp_use_polyhaven = True", user_prompt="benchmark")
                result["old_blender_mcp"] = {"ready_s": round(ready, 2), "tasks": await run_tasks("blender-mcp", "old", old, not a.skip_assets, out)}
        finally:
            proc.kill()
    (out / "compare.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print("written:", out / "compare.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tools" / "out"))
    ap.add_argument("--old-blender", default="")
    ap.add_argument("--tether", default="background", choices=["background", "window", "both"])
    ap.add_argument("--skip-old", action="store_true")
    ap.add_argument("--skip-assets", action="store_true")
    asyncio.run(main(ap.parse_args()))
