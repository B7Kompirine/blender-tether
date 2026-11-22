"""The MCP server through a stdio client: tool list and schemas, calls against the example app, and (with --blender) the real path with a background Blender."""
import asyncio
import json
import sys
import time

from harness import ROOT, TMP, check, kill_pid, pid_alive, start_app


def _params():
    from mcp import StdioServerParameters
    return StdioServerParameters(command=sys.executable, args=[str(ROOT / "tether" / "mcp_server.py")],
                                 env={"TETHER_HOME": str(TMP), "TETHER_STATS_DIR": str(TMP / "stats")})


def _wire(model):
    """The protocol form (isError, inputSchema, mimeType...) of an SDK result: SDK 1.x names the Python attributes like that, SDK 2.x in snake_case."""
    return model.model_dump(by_alias=True, exclude_none=True)


async def _generic(p):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as o:
            await o.initialize()
            tools = [_wire(t) for t in (await o.list_tools()).tools]
            size = sum(len(json.dumps(t, ensure_ascii=False)) for t in tools)
            print(f"      tool definitions: {len(tools)} tools, {size} characters (≈{size // 4} tokens, a characters/4 estimate)")
            by = {t["name"]: t for t in tools}

            def hint(name, key):
                return by[name].get("annotations", {}).get(key)
            check("MCP: 11 tools", sorted(by) == sorted(["status", "run", "api_search", "view", "open", "close", "scene", "asset_search", "asset_import",
                                                          "generate_start", "generate_status"]), sorted(by))
            check("MCP: no 'title' labels in the tool schemas (they cost tokens every session), parameter names and types are intact",
                  '"title"' not in json.dumps([t["inputSchema"] for t in tools]) and by["view"]["inputSchema"]["properties"]["shading"]["type"] == "string"
                  and "app" in by["run"]["inputSchema"]["required"])
            check("MCP: scene and asset_search are marked read-only (no permission prompt); asset_search/asset_import reach the outside world (openWorld), asset_import is not read-only",
                  hint("scene", "readOnlyHint") is True and hint("asset_search", "readOnlyHint") is True
                  and hint("asset_search", "openWorldHint") is True and hint("asset_import", "openWorldHint") is True
                  and hint("asset_import", "readOnlyHint") is False and hint("run", "openWorldHint") is False)

            async def call(name, **a):
                y = _wire(await o.call_tool(name, a))
                return y.get("isError", False), (y["content"][0].get("text", "") if y.get("content") else "")
            e, m = await call("status")
            check("MCP status: the example app is listed", not e and f"example#{p.pid}" in m, m)
            e, m = await call("run", app="example", code="print('mcp')\n40 + 2")
            check("MCP run: output + result", not e and "mcp" in m and "-> 42" in m, m)
            e, m = await call("run", app="example", code="1/0")
            check("MCP run: an error in the code is isError, the trace is in the text", e and "ZeroDivisionError" in m, m)
            e, m = await call("run", app="missing", code="1")
            check("MCP run: a missing app is isError", e and "is not running" in m, m)
            e, m = await call("run", app="example")
            check("MCP run: a call without code is isError and says why", e and "give code or file" in m, m)
            e, m = await call("api_search", app="example", query="hello")
            check("MCP api_search", not e and "hello" in m, m)
            e, m = await call("view", app="example")
            check("MCP view: if unsupported, isError with an explanation", e and "does not provide images" in m, m)
            await call("run", app="example", code="x = 5", reset=True)
            e, m = await call("run", app="example", code="x")
            check("MCP persistent namespace", not e and "-> 5" in m, m)
            e, m = await call("run", app="example", code="x", reset=True)
            check("MCP reset=True clears first", e, m)
            (TMP / "mcp_mid.py").write_text("import os\nprint('FRAME', os.path.basename(os.getcwd()), os.environ.get('KT_MCP'))\nprint('hidden')\n", encoding="utf-8")
            e, m = await call("run", app="example", file="mcp_mid.py", cwd=str(TMP), env={"KT_MCP": "m"}, grep="^FRAME")
            check("MCP run: file + cwd + env + grep", not e and f"FRAME {TMP.name} m" in m and "hidden" not in m and "grep 1/2 lines" in m, m)


def run_generic():
    print("--- MCP server")
    p = start_app("example")
    try:
        asyncio.run(_generic(p))
    finally:
        p.kill()
        p.wait()


async def _blender_session():
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as o:
            await o.initialize()

            async def call(name, **a):
                y = _wire(await o.call_tool(name, a))
                return y.get("isError", False), (y["content"][0].get("text", "") if y.get("content") else "")
            t0 = time.time()
            e, m = await call("open", app="blender", background=True)
            elapsed = time.time() - t0
            check("[mcp+blender] open: from inside the MCP server (stdio) a background daemon starts through WMI", not e and "blender#" in m and elapsed < 30, f"{m} {elapsed:.1f} s")
            if e:
                return None
            selector = json.loads(m)["selector"]
            e, m = await call("run", app=selector, code="print('mcp'); bpy.app.version_string")
            check("[mcp+blender] run: output + version", not e and "mcp" in m and "-> \"5." in m, m)
            e, m = await call("api_search", app=selector, query="bpy.ops.mesh.primitive_cube_add")
            check("[mcp+blender] api_search: the parameters come back", not e and "size: float" in m, m)
            y = _wire(await o.call_tool("view", {"app": selector, "width": 320}))
            c = y.get("content", [])
            check("[mcp+blender] view in the background returns a PNG image (the default 4 angles) followed by a one-line caption",
                  not y.get("isError") and len(c) == 2 and c[0]["type"] == "image" and c[0]["mimeType"] == "image/png"
                  and c[1]["type"] == "text" and "4 angles" in c[1]["text"], y)
            y = _wire(await o.call_tool("view", {"app": selector, "width": 320, "shading": "wireframe", "angles": ["front", [1, 1, 0.5]], "target": ["Cube"]}))
            c = y.get("content", [])
            check("[mcp+blender] view: shading, an angle name + an [x,y,z] direction and the target pass through MCP",
                  not y.get("isError") and c[0]["type"] == "image" and "2 angles" in c[1]["text"] and "shading=wireframe" in c[1]["text"], y)
            y = _wire(await o.call_tool("view", {"app": selector, "width": 320, "shading": "nonsense"}))
            check("[mcp+blender] view: an invalid option is isError with an explanation", y.get("isError") and "unknown shading" in y["content"][0]["text"], y)
            e, m = await call("status")
            check("[mcp+blender] status: the daemon is listed", not e and selector in m, m)
            return int(selector.split("#")[1])


def run_blender():
    print("--- MCP + Blender (the real path)")
    pid = asyncio.run(_blender_session())
    if pid is None:
        return
    for _ in range(120):                      # the MCP server has exited: its owner is dead, the daemon must close itself
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    check("[mcp+blender] when the MCP server (the session) ends the daemon is not orphaned", not pid_alive(pid))
    if pid_alive(pid):
        kill_pid(pid)
