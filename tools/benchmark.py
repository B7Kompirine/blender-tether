"""Reproducible benchmark and demo recorder. It drives the Tether MCP server over stdio (the way an AI agent does) and measures: a cold `blender -b` start,
the background daemon's start, the round trip of a call, the scene report and the multi-angle image. Then it builds a small diorama and writes the real tool
outputs (text, timing, PNG) to the output folder.

Usage: python tools/benchmark.py [--cold 8] [--calls 300] [--out tools/out]
Needs: mcp, Pillow, Blender. The cold runs use --factory-startup: no user add-ons are loaded and nothing is opened that could disturb a running Blender.
"""
import argparse
import asyncio
import base64
import io
import json
import os
import platform
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
from tether import client  # noqa: E402

SCENE_CODE = r"""
import math
def mat(name, r, g, b):
    x = D.materials.get(name) or D.materials.new(name); x.diffuse_color = (r, g, b, 1); return x
def part(name, op, material, parent, **k):
    op(**k); o = C.active_object; o.name = name; o.data.materials.append(material); o.parent = parent; return o
def new_empty(name):
    o = D.objects.new(name, None); C.collection.objects.link(o); return o
for o in list(D.objects): D.objects.remove(o)
GREEN, WALL, ROOF, BROWN = mat('green', .36, .62, .3), mat('wall', .93, .86, .7), mat('roof', .72, .2, .16), mat('brown', .42, .27, .16)
LEAF, YELLOW, GRAY, DARK, BALL = mat('leaf', .18, .45, .22), mat('yellow', 1, .85, .3), mat('gray', .5, .5, .52), mat('dark', .2, .13, .1), mat('ball', .9, .3, .5)
part('ground', bpy.ops.mesh.primitive_cylinder_add, GREEN, new_empty('ground_root'), vertices=40, radius=6.2, depth=.02, location=(0, 0, .01))
house = new_empty('house')
part('wall', bpy.ops.mesh.primitive_cube_add, WALL, house, size=1, scale=(3, 2.4, 1.5), location=(0, 0, .75))
part('roof', bpy.ops.mesh.primitive_cone_add, ROOF, house, vertices=4, radius1=2.6, depth=1.3, location=(0, 0, 2.15), rotation=(0, 0, math.pi / 4))
part('door', bpy.ops.mesh.primitive_cube_add, DARK, house, size=1, scale=(.55, .08, 1), location=(0, -1.22, .5))
for i, x in enumerate((-1.0, 1.0)): part(f'window_{i + 1}', bpy.ops.mesh.primitive_cube_add, YELLOW, house, size=1, scale=(.5, .08, .5), location=(x, -1.22, .95))
part('chimney', bpy.ops.mesh.primitive_cube_add, GRAY, house, size=1, scale=(.35, .35, .9), location=(.9, .5, 2.2))
for i, (x, y) in enumerate(((-3.6, 1.6), (3.9, 1.2), (-3.0, -3.0), (4.0, -2.6))):
    tree = new_empty(f'tree_{i + 1}')
    part(f'trunk_{i + 1}', bpy.ops.mesh.primitive_cylinder_add, BROWN, tree, vertices=8, radius=.14, depth=.7, location=(x, y, .35))
    part(f'leaves_a_{i + 1}', bpy.ops.mesh.primitive_cone_add, LEAF, tree, vertices=8, radius1=.9, depth=1.5, location=(x, y, 1.4))
    part(f'leaves_b_{i + 1}', bpy.ops.mesh.primitive_cone_add, LEAF, tree, vertices=8, radius1=.65, depth=1.2, location=(x, y, 2.2))
rocks = new_empty('rocks')
for i, (x, y, s) in enumerate(((-2.2, -2.2, .5), (2.6, -3.6, .4), (-4.4, -.6, .55))):
    part(f'rock_{i + 1}', bpy.ops.mesh.primitive_ico_sphere_add, GRAY, rocks, subdivisions=1, radius=s, location=(x, y, s * .6), scale=(1, .8, .6))
path = new_empty('path')
for i in range(5): part(f'step_{i + 1}', bpy.ops.mesh.primitive_cube_add, GRAY, path, size=1, scale=(.9, .5, .05), location=(.15 * (i % 2), -1.9 - .8 * i, .025))
lamp = new_empty('lamp')
part('post', bpy.ops.mesh.primitive_cylinder_add, GRAY, lamp, vertices=8, radius=.05, depth=1.2, location=(2.3, -1.8, 1.2))
part('bulb', bpy.ops.mesh.primitive_uv_sphere_add, YELLOW, lamp, segments=12, ring_count=6, radius=.14, location=(2.3, -1.8, 1.9))
ball = part('ball', bpy.ops.mesh.primitive_uv_sphere_add, BALL, new_empty('ball_root'), segments=10, ring_count=6, radius=.25, location=(3.2, 0, .25))
for k, a in enumerate((0, 90, 180, 270, 360)):
    ball.location = (3.2 * math.cos(math.radians(a)), 3.2 * math.sin(math.radians(a)), .25); ball.keyframe_insert('location', frame=1 + 12 * k)
C.scene.frame_end = 49
len(D.objects)
"""
FIX_CODE = "D.objects['lamp'].location.z -= .6\nD.objects['lamp'].location.z"


class Session:
    """Drives the MCP server over stdio and returns the wall-clock time of every call in ms."""

    async def __aenter__(self):
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "tether" / "mcp_server.py")],
                                       env={k: v for k, v in os.environ.items() if not k.startswith("TETHER_")})
        self._cm = stdio_client(params)
        read, write = await self._cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *a):
        await self._session.__aexit__(*a)
        await self._cm.__aexit__(*a)

    async def call(self, name, **a):
        t = time.perf_counter()
        y = await self._session.call_tool(name, {k: v for k, v in a.items() if v is not None})
        ms = (time.perf_counter() - t) * 1000
        text = "\n".join(c.text for c in y.content if c.type == "text")
        images = [Image.open(io.BytesIO(base64.b64decode(c.data))).convert("RGB") for c in y.content if c.type == "image"]
        if getattr(y, "is_error", None) or getattr(y, "isError", None):         # SDK 2.x / SDK 1.x attribute name
            raise RuntimeError(f"{name}: {text[:300]}")
        return text, images, ms


def summary(values):
    values = sorted(values)
    return {"n": len(values), "median": statistics.median(values), "p95": values[min(len(values) - 1, int(len(values) * .95))], "min": values[0], "max": values[-1]}


def cold(args, n):
    """A new `blender -b` process for every job: start + script + exit, wall clock (seconds)."""
    blender, times = client.find_blender(), []
    for _ in range(n):
        t = time.perf_counter()
        subprocess.run([blender, "-b", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=180)
        times.append(time.perf_counter() - t)
    return times


def cpu_name():
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def value_of(text):
    """The value line of a run response ('-> <json>'), parsed; the raw text if there is none."""
    for line in text.splitlines():
        if line.startswith("-> "):
            return json.loads(line[3:])
    return text.strip()


async def main(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = {"environment": {"blender": None, "os": platform.platform(), "cpu": cpu_name(), "gpu": None, "date": time.strftime("%Y-%m-%d")}}
    print("cold `blender -b` (the first run is dropped: it warms the file cache)")
    cold(["--factory-startup", "--python-expr", "pass"], 1)
    result["cold_factory"] = [round(x, 3) for x in cold(["--factory-startup", "--python-expr", "pass"], args.cold)]
    print("  factory settings:", summary(result["cold_factory"]))
    async with Session() as s:
        opened, selector = [], None
        for i in range(5):
            text, _, ms = await s.call("open", app="blender", background=True)
            d = json.loads(text)
            opened.append({"total_ms": round(ms), "startup_s": d["startup_s"]})
            result["environment"]["blender"] = d["version"]
            if i < 4:
                await s.call("close", app=d["selector"])
                await asyncio.sleep(2.0)
            selector = d["selector"]
        result["daemon_open"] = opened
        print("  daemon start:", opened)
        passes, queries = [], []
        for _ in range(args.calls):
            _, _, ms = await s.call("run", app=selector, code="pass")
            passes.append(ms)
        for _ in range(args.calls // 3):
            _, _, ms = await s.call("run", app=selector, code="len(D.objects)")
            queries.append(ms)
        result["call_pass_ms"], result["call_query_ms"] = summary(passes), summary(queries)
        print("  run 'pass' round trip ms:", result["call_pass_ms"])
        # --- demo recording: a clean daemon, real outputs ---
        await s.call("close", app=selector)
        await asyncio.sleep(2.0)
        record = []

        async def step(name, show, **arg):
            text, images, ms = await s.call(name, **arg)
            k = {"tool": name, "show": show, "ms": round(ms, 1), "text": text, "code": arg.get("code", ""), "image": None}
            if images:
                path = out / f"step_{len(record) + 1:02d}.png"
                images[0].save(path)
                k["image"] = path.name
            record.append(k)
            print(f"  {name:12} {ms:8.1f} ms  {text[:90]!r}")
            return text

        await step("status", "status")
        text = await step("open", "open app=blender background=true", app="blender", background=True)
        selector = json.loads(text)["selector"]
        await step("run", "run   # build the scene in Python", app=selector, code=SCENE_CODE)
        await step("scene", "scene fields=[ground]", app=selector, fields=["ground"], limit=30)
        await step("run", "run   # lower the lamp", app=selector, code=FIX_CODE)
        await step("scene", "scene query=lamp fields=[ground]", app=selector, query="lamp", fields=["ground"])
        await step("view", "view   # default: 4 angles, 1024 px", app=selector)
        await step("view", "view shading=wireframe target=[house]", app=selector, shading="wireframe", target=["house"])
        await step("view", "view mode=frames frame_count=4 look=top", app=selector, mode="frames", frame_count=4, look="top", width=1024)
        result["recording"] = record
        views, scenes = [], []
        for _ in range(10):
            _, _, ms = await s.call("view", app=selector)
            views.append(ms)
        for _ in range(20):
            _, _, ms = await s.call("scene", app=selector)
            scenes.append(ms)
        result["view_4angles_1024_ms"], result["scene_ms"] = summary(views), summary(scenes)
        print("  view 4 angles 1024 px ms:", result["view_4angles_1024_ms"])
        print("  scene ms:", result["scene_ms"])
        try:       # ask Blender which GPU the headless daemon draws on; no guessing
            result["environment"]["gpu"] = value_of((await s.call("run", app=selector, code="import gpu\ngpu.platform.renderer_get()"))[0])
        except Exception as e:  # noqa: BLE001
            print("  could not read the GPU name:", e)
        print("  environment:", result["environment"])
        await s.call("close", app=selector)
    (out / "benchmark.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written:", out / "benchmark.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold", type=int, default=8, help="how many cold `blender -b` runs")
    ap.add_argument("--calls", type=int, default=300, help="round trips to time")
    ap.add_argument("--out", default=str(ROOT / "tools" / "out"))
    asyncio.run(main(ap.parse_args()))
