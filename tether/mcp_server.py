"""Tether MCP server (an AI agent -> local apps): status, open, close, run, api_search, scene, view, asset_search, asset_import. The work is done by
tether.client and tether.assets; this file only holds the tool definitions and the output format ('title' labels are removed from the schemas: tokens).

Usage: claude mcp add tether -- python -m tether.mcp_server
"""
import io
import os
import sys
from pathlib import Path

if __package__ in (None, ""):          # started as a script: python tether/mcp_server.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tether import assets, client
else:
    from . import assets, client
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from PIL import Image as PILImage

INSTRUCTIONS = (
    "You send code to local apps in their own language (Blender for now). Order: status -> open if needed -> api_search -> run -> read the result back.\n"
    "• app = 'blender' or the selector from status, 'blender#pid' (the pid is required when several are open).\n"
    "• Do not write names and signatures from memory; verify them from the running version with api_search.\n"
    "• The namespace persists (functions, numbers, strings and lists live on) but is reset when a file loads: define a function once, then call it by name; "
    "do not resend the same code. Do not hold bpy objects (undo, file loads and mode changes make them stale and can crash Blender): keep the name and "
    "fetch bpy.data.objects[name] on every call.\n"
    "• The result is the `result` variable or the last expression. Do not dump big data: tether.brief(x). Output is cut at 8k characters "
    "(the full text is written to a file and its path is given).\n"
    "• Measure after writing (counts, file sizes, read-backs): not raising an error is not verification. An error may have changed the scene halfway; "
    "read the state before retrying.\n"
    "• Read matrix_world/bound_box only after tether.refresh().\n"
    "• To understand the scene without writing code: scene (text: placement, size, topology, weights, GTA checks) and view (the default in the "
    "background is 4 angles; shading=wireframe/xray/texture; target=['Name'] frames).\n"
    "• No imports are needed in Blender (the ready names are in status.hint) and verified helpers are ready under `tether`: tether.select, tether.delete, "
    "tether.save, tether.backup, tether.mode, tether.edit, tether.export_sollumz. Use them instead of raw select_set/export: they raise with the reason "
    "instead of failing silently.\n"
    "• Instead of the old 'blender -b X.blend --python b.py -- a': run(file='b.py', args=['a'], cwd=..., blend='X.blend') on a background agent; "
    "narrow long output with grep='regex'.")

mcp = FastMCP("tether", instructions=INSTRUCTIONS)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
LAUNCH = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def _fail(e):
    raise RuntimeError(str(e)) from None


@mcp.tool(annotations=READ)
def status() -> dict:
    """Running apps: selector ('name#pid'), mode, version, busy or not, whether a modal operator is running, capabilities. Call this first."""
    running = []
    for k in client.apps():
        i = k.get("info")
        running.append({"selector": f"{k['app']}#{k['pid']}", "mode": k["mode"], "lang": k["lang"], "version": k["version"],
                        "state": "no response" if not i else (f"busy: {i['running']['code']}" if i["busy"] else "idle"),
                        "queue": i["queue"] if i else None, "extra": i.get("extra") if i else None,
                        "capabilities": i["capabilities"] if i else None, "hint": i["hint"] if i else ""})
    return {"running": running, "launchable": list(client.LAUNCHERS)}


@mcp.tool(annotations=WRITE)
def run(app: str, code: str = "", file: str = "", args: list[str] | None = None, fresh: bool | None = None, reset: bool = False,
        timeout: int = 0, cwd: str = "", blend: str = "", clean: bool = False, env: dict[str, str] | None = None, grep: str = "") -> str:
    """Runs code in the app's own language (Blender: Python; bpy, bmesh, mathutils ready). The namespace persists (reset=True clears it, fresh=True makes
    this call a one-off). Value: the last expression or `result`; print output and a line-numbered error trace come back too. Script (instead of
    'blender -b --python x.py -- a b'): file = path (relative: looked up in cwd first, then in the server's working directory; the answer says which one
    ran), args = what follows '--', cwd, env = environment variables; it runs in a fresh namespace and cwd/env/sys.path are restored afterwards.
    Background Blender: blend = open that .blend first, clean = go back to the factory scene first (both refused in a windowed Blender). grep = regex:
    only the matching lines of the output are kept; if the output is cut the full text is written to a file and its path is given.
    timeout = seconds (code 60, script 300): when it expires the call returns but the job continues (status says 'busy'); its result is not returned."""
    if not code and not file:
        raise ValueError("give code or file")
    try:
        if reset:
            client.request(app, "reset")
        y = client.run(app, code=code or None, file=file or None, args=args, fresh=fresh, timeout=max(1, min(timeout or (300 if file else 60), 3600)),
                       cwd=cwd or None, blend=blend or None, clean=clean, env=env, grep=grep or None)
    except client.TetherError as e:
        _fail(e)
    text, failed = client.format_run(y)
    if failed:
        raise RuntimeError(text)
    return text


@mcp.tool(annotations=READ)
def api_search(app: str, query: str) -> str:
    """Searches the app's API from the running version; use it before writing code. Blender: bpy.ops and bpy.types, open add-ons included. An exact
    name ('bpy.ops.mesh.primitive_cube_add') dumps the parameters and enum values."""
    try:
        return client.api_search(app, query)
    except client.TetherError as e:
        _fail(e)


@mcp.tool(annotations=READ)
def scene(app: str, fields: list[str] | None = None, query: str = "", root: str = "", limit: int = 20) -> str:
    """Scene report (read-only): the header has object/type counts, active, selected, mode; then fields per object (default: top-level objects,
    location+size+children+hidden). fields: location, size (world box including children), ground (ok/floating/below), rotation, scale, parent, children,
    hidden, details (faces/tris/verts, bones, light), materials, modifiers, animation; topology (quads/tris/ngons, boundary and non-manifold edges, loose
    vertices, poles); weights (vertices a deform bone does not move, bones without a vertex group); gta (Sollumz: UVMap 0, Color 1, sz_lods, shader, missing
    textures, unapplied transforms, .001 suffix); settings (file, engine, frames, resolution, camera, HDRI, units). Groups: placement, contents, health, all.
    root='Name': that object's hierarchy; query='text': the objects with it in their name; limit at most 200 lines."""
    try:
        return client.report(app, fields, query, root, limit)
    except client.TetherError as e:
        _fail(e)


@mcp.tool(annotations=READ, structured_output=False)
def view(app: str, width: int = 1024, area: str = "", mode: str = "", shading: str = "", target: list[str] | None = None,
         angles: list[str | list[float]] | None = None, distance: float = 0, frames: list[int] | None = None, frame_count: int = 0,
         look: str | list[float] | None = None, image: str = "") -> list:
    """One PNG of the scene + a caption line (which tile is which angle). width: pixels of the long side (of the whole image when combined).
    mode: viewport (windowed Blender: the user's view without the UI; background: the camera or an automatic frame), camera (at the render aspect),
    angles (frames the target from several directions; the background default), frames (an animation strip). shading: solid, texture, material, rendered
    (EEVEE), wireframe (edges over the surface), xray (transparent, bones visible). target: objects to frame, with their children (default everything
    visible; a big object such as a floor enlarges the frame). angles: at most 6 of front, back, left, right, top, bottom, iso or an [x, y, z] direction
    (default front, right, top, iso). distance: metres from the target to the eye. frames: frame numbers or frame_count (2-12, default 6); look: camera,
    an angle name or [x, y, z]; frames steps through the frames and returns to the old frame at the end. image: instead of the scene, 'Render Result',
    an open image name or a file path. area (windowed): 'window' = the whole window, 'NODE_EDITOR'/'OUTLINER'... = a screenshot of that area (the splash
    window may cover it); it cannot be combined with the other options. Temporary settings are restored."""
    width = max(64, min(width, 4096))
    try:
        png, caption = client.view(app, width, area=area or None, caption=True, mode=mode, shading=shading, target=target, angles=angles,
                                   distance=distance, frames=frames, frame_count=frame_count, look=look, image=image)
    except client.TetherError as e:
        _fail(e)
    im = PILImage.open(io.BytesIO(png))
    im.thumbnail((width, width))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return [Image(data=buf.getvalue(), format="png"), *([caption] if caption else [])]


def _xyz(v):
    return "(" + ",".join(f"{x:g}" for x in v) + ")"


def _asset_report(m, r, target_size):
    s = []
    if r["kind"] == "model":
        s.append(f"imported: {m['source']} {m['id']} ({m['license']}) -> '{r['root']}' ({r['objects']} objects, {r['tris']} tris, "
                 f"{len(r['materials'])} materials, scale ×{r['scale']:g})")
        if r["size"]:
            s.append(f"size {_xyz(r['size'])} m; min {_xyz(r['min'])}, max {_xyz(r['max'])}; root location {_xyz(r['location'])}")
        if r["animations"]:
            s.append("animations: " + ", ".join(r["animations"][:6]))
        if m["source"] in ("sketchfab", "polypizza") and not target_size:
            s.append("warning: the scale is arbitrary in this source: check the size or give target_size")
    elif r["kind"] == "texture":
        s.append(f"{'already existed, reused' if r['reused'] else 'material created'}: '{r['material']}' ({m['source']} {m['id']}, {m['license']}; "
                 f"maps: {', '.join(r['maps'])})")
        s.append("applied: " + (", ".join(r["applied"]) or "none (give apply_to=['Name'] to put it on an object; it is kept with a fake user)"))
        s += [f"skipped: {a}" for a in r["skipped"]] + [f"warning: {u}" for u in r["warnings"]]
    else:
        s.append(f"world lighting: '{r['world']}' ({m['source']} {m['id']}, {m['license']}, {r['size'][0]}x{r['size'][1]}); "
                 + (f"the previous world '{r['previous_world']}' was not deleted (fake user)" if r["previous_world"] else "there was no previous world or it was the same"))
    s.append(f"file: {m.get('main_file') or m['folder']} ({'from cache' if m['cached'] else 'downloaded'}{', ' + m['resolution'] if m.get('resolution') else ''})")
    if m.get("warning"):
        s.append(f"LICENSE: {m['license']}: {m['warning']} | {m['attribution']}")
    return "\n".join(s)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True), structured_output=False)
def asset_search(source: str = "all", query: str = "", kind: str = "", category: str = "", license: str = "cc0", limit: int = 10,
                 attrs: dict[str, str] | None = None, min_size_m: float = 0, max_faces: int = 0, animated: bool = False, previews: int = 0) -> list:
    """Searches ready-made 3D assets on the internet: models, PBR textures, HDRIs. source: all (default, in parallel), polyhaven (all CC0, no key), ambientcg
    (textures/hdris, CC0), sketchfab (models; a token is needed to download), polypizza (low-poly models; an API key). The query is English ('couch' also
    finds sofa). kind: model, texture, hdri. license: CC0 only by default; 'cc0,by' also brings CC-BY (attribution required). category: a text filter.
    polyhaven only: attrs={'weather': 'clear'} (hdri), min_size_m (the real size of a texture; 2+ for floors and walls). max_faces: polygon cap
    (polyhaven, sketchfab). animated: sketchfab, polypizza. previews=N (at most 6): small images of the first N results as one numbered image.
    Line: source id | kind | name | category | faces | size (m) | highest resolution | license | author. Pass the id to asset_import."""
    try:
        results, notes = assets.search(source, query, kind, category, license, limit, attrs, min_size_m, max_faces, animated)
        out = [assets.format_results(results, notes)]
        if previews and results:
            out.append(Image(data=assets.preview_image(results, previews), format="png"))
        return out
    except assets.AssetError as e:
        _fail(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True), structured_output=False)
def asset_import(app: str, source: str, id: str, kind: str = "", resolution: str = "1k", format: str = "", target_size: float = 0,
                 location: list[float] | None = None, snap_to_ground: bool = True, apply_to: list[str] | None = None, accept_license: bool = False) -> str:
    """Downloads a found asset and brings it into Blender (the download happens on this computer, from allow-listed hosts, with a size cap, into a
    cache; it is not downloaded twice). model: arrives under a root empty; target_size = the largest dimension in metres (chair 1.0, car 4.5, cup 0.12;
    sketchfab/polypizza arrive at an arbitrary scale, always give it); snap_to_ground puts the centre of the base at location ([x, y, z], default 0,0,0).
    texture: a Principled material named 'source_id' (kept even if it is outside the scene); apply_to=['Object'] replaces those meshes' material
    (warns if there are no UVs). hdri: assigned as a new world (the old one is kept). resolution: 1k (default), 2k, 4k, 8k; format: model gltf/fbx,
    texture jpg/png, hdri hdr/exr. accept_license: take a non-CC0 asset knowingly. Returns: bounding box, tris, materials, file, license; look at it
    with view afterwards. The root gets the properties asset_source/asset_license/asset_attribution."""
    try:
        target_size, location = assets.placement(target_size, location)         # a bad number is rejected before downloading
        m = assets.download(source, id, kind, resolution, format, accept_license)
        r = client.call(app, "import_asset", {"kind": m["kind"], "name": m["id"], "file": m.get("main_file", ""), "maps": m.get("maps"),
                                               "source": m["source"], "license": m["license"], "attribution": m["attribution"], "target_size": target_size,
                                               "location": location, "snap_to_ground": snap_to_ground, "apply_to": apply_to}, timeout=300)["result"]
    except (assets.AssetError, client.TetherError) as e:
        _fail(e)
    return _asset_report(m, r, target_size)


@mcp.tool(name="open", annotations=LAUNCH)
def open_app(app: str, file: str = "", background: bool = False, clean: bool | None = None, addons: list[str] | None = None) -> dict:
    """Starts the app outside the caller's container and waits for its agent. Returns: selector, pid, mode, version, startup_s (seconds).
    Blender: background=True is a headless daemon: the factory scene (cube, camera, light), no user settings and no add-ons unless addons=['Name', ...]
    (add-on module names) or the TETHER_ADDONS environment variable are given; it closes after 30 minutes idle or when this session ends.
    background=False is windowed: it starts with the user's settings and the splash window shows. file = a .blend to load at startup; clean = start
    without user settings."""
    try:
        return client.open_app(app, file, background, clean, addons, owner_pid=os.getpid())
    except client.TetherError as e:
        _fail(e)


@mcp.tool(annotations=WRITE)
def close(app: str) -> str:
    """Closes only a background agent. The user closes a windowed Blender; close a window you opened with open only by calling
    bpy.ops.wm.quit_blender() through run (the connection drops, which is not counted as an error)."""
    try:
        y = client.request(app, "close")
    except client.TetherError as e:
        _fail(e)
    if not y.get("ok"):
        raise RuntimeError(y["error"]["message"])
    return "closed"


def _strip_titles(schema):
    """The 'title' labels in the schemas add nothing for the model but cost tokens every session: they are removed."""
    if isinstance(schema, dict):
        schema.pop("title", None)
        for key, v in schema.items():
            for sub in (v.values() if key == "properties" else [v]):
                _strip_titles(sub)
    elif isinstance(schema, list):
        for v in schema:
            _strip_titles(v)


for _tool in getattr(getattr(mcp, "_tool_manager", None), "_tools", {}).values():      # if the SDK internals change the schemas stay as they are
    _strip_titles(_tool.parameters)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
