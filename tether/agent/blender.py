"""Blender host adapter for the Tether agent. In the GUI it runs commands on the main thread through bpy.app.timers, in the background (-b) through a blocking
loop. It puts ready names in the namespace (bpy, np, D, C... and the `tether` helper object), drops stale bpy references when a file loads or undo runs, serves
images (blender_view: GPU offscreen, angles/shading; fallback: Workbench) and prepares blend/clean runs.

Usage: blender [-b] --python tether/agent/blender.py -- --gui | --daemon [--addon NAME] [--owner-pid N] [--idle-s N] [--home-dir DIR]
"""
import difflib
import itertools
import math
import os
import re
import sys
import tempfile
import time
import types

import bpy
from bpy.app.handlers import persistent
from mathutils import Vector

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "--home-dir" in sys.argv:           # the client's state folder; set before the engine is imported
    os.environ["TETHER_HOME"] = sys.argv[sys.argv.index("--home-dir") + 1]
from tether.agent import ACTIVE, HOME, Agent  # noqa: E402
from tether.agent import blender_assets, blender_report, blender_view  # noqa: E402
from tether.agent import blender_helpers as helpers  # noqa: E402

HINT = ("python; no imports needed: bpy, bmesh, mathutils, np, os, sys, json, re, time, math, Path, collections, Counter, defaultdict, "
        "Vector, Matrix, Euler, Quaternion, BVHTree, D=bpy.data, C=bpy.context; the namespace persists but is reset when a file is loaded; "
        "tether.api('...') searches the live API; tether.view3d() gives the operator context, tether.brief(x) shortens, tether.refresh() refreshes "
        "matrix_world/bound_box; verified helpers that raise instead of failing silently: tether.select(*names, unhide=False) selects and counts, "
        "tether.delete(*names) bulk delete, tether.save(path), tether.backup(), tether.mode(name, 'EDIT'), with tether.edit(name) as b (bmesh), "
        "tether.export_sollumz(folder, [names]) Sollumz export that measures the written files")
MODAL_WAIT_S = 60              # if the user stays in a modal operator this long (a permanently modal add-on), jobs run anyway
FUNCTIONS = {"report": blender_report.report, "import_asset": blender_assets.import_asset}      # host functions (op 'call'): the MCP tools scene and asset_import
_index = {}
_modal = {"since": None, "warned": False}


def view3d(area_type="VIEW_3D"):
    """with bpy.context.temp_override(**tether.view3d()): bpy.ops... — view3d.* operators need a 3D view context (area_type: another area type,
    e.g. 'NODE_EDITOR'). A window exists in the background too; returns an empty dict if none is found."""
    chosen = None
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            if a.type == area_type and (chosen is None or a.width * a.height > chosen[1].width * chosen[1].height):
                chosen = (w, a)
    if chosen is None:
        return {}
    w, a = chosen
    return {"window": w, "screen": w.screen, "area": a, "region": next(r for r in a.regions if r.type == "WINDOW")}


def refresh():
    """After a location/scale change matrix_world and bound_box are stale; call this before reading them."""
    bpy.context.view_layer.update()


def brief(x, n=8, d=3):
    """Shortens big data without shipping it to the model: list/dict/Vector/Matrix/ID -> JSON-compatible, long ones end in '…(+N)'."""
    if x is None or isinstance(x, (bool, int)):
        return x
    if isinstance(x, float):
        return round(x, 5)
    if isinstance(x, str):
        return x if len(x) <= 120 else x[:120] + "…"
    if isinstance(x, bpy.types.ID):
        return f"{type(x).__name__}({x.name!r})"
    if d <= 0:
        return repr(x)[:60]
    if isinstance(x, dict):
        s = {str(k): brief(v, n, d - 1) for k, v in itertools.islice(x.items(), n)}
        if len(x) > n:
            s["…"] = f"+{len(x) - n}"
        return s
    try:
        total = len(x)
        head = [brief(v, n, d - 1) for v in itertools.islice(iter(x), n)]
    except TypeError:
        return repr(x)[:80]
    return head + ([f"…(+{total - n})"] if total > n else [])


_GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL"}


def _view_fallback(width):
    """Fallback for a background environment without GPU drawing: renders the scene with Workbench. If the scene has no camera a temporary camera
    frames all the geometry. Every changed setting, the temporary object and 'Render Result' are restored."""
    scene = bpy.context.scene
    r, img = scene.render, scene.render.image_settings
    refresh()
    points = [o.matrix_world @ Vector(c) for o in scene.objects if o.type in _GEOMETRY and not o.hide_render for c in o.bound_box]
    if scene.camera is None and not points:
        raise RuntimeError("the scene has no camera and no geometry: nothing to show")
    saved = (r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath, img.file_format, img.color_mode,
             r.use_file_extension, scene.camera)
    had_result = "Render Result" in bpy.data.images
    path = os.path.join(tempfile.gettempdir(), f"tether_{os.getpid()}.png")
    temp = None
    try:
        ratio = r.resolution_x / r.resolution_y
        r.resolution_x, r.resolution_y = (width, max(1, round(width / ratio))) if ratio >= 1 else (max(1, round(width * ratio)), width)
        r.resolution_percentage = 100
        if scene.camera is None:
            data = bpy.data.cameras.new("tether_camera")
            temp = bpy.data.objects.new("tether_camera", data)
            scene.collection.objects.link(temp)
            low = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
            high = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
            center, radius = (low + high) / 2, max((high - low).length / 2, 1e-3)
            t = math.tan(data.angle / 2)
            fov = 2 * math.atan(min(t, t / ratio if ratio >= 1 else t * ratio))
            data.clip_start, data.clip_end = radius / 100, radius * 100
            temp.location = center + Vector((1.0, -1.4, 0.9)).normalized() * (1.1 * radius / math.sin(fov / 2))
            temp.rotation_euler = (center - temp.location).to_track_quat("-Z", "Y").to_euler()
            scene.camera = temp
        r.engine, r.filepath, img.file_format, img.color_mode, r.use_file_extension = "BLENDER_WORKBENCH", path, "PNG", "RGB", False
        bpy.ops.render.render(write_still=True)
        with open(path, "rb") as f:
            return f.read()
    finally:
        (r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath, img.file_format, img.color_mode,
         r.use_file_extension, scene.camera) = saved
        if temp is not None:
            data = temp.data
            bpy.data.objects.remove(temp)
            bpy.data.cameras.remove(data)
        if not had_result and "Render Result" in bpy.data.images:
            bpy.data.images.remove(bpy.data.images["Render Result"])
        if os.path.exists(path):
            os.remove(path)


def _screenshot(operator, **context):
    """Screen pixels (the splash window and open menus show too): calling wm.redraw_timer would return 1x1."""
    path = os.path.join(tempfile.gettempdir(), f"tether_{os.getpid()}.png")
    try:
        with bpy.context.temp_override(**context):
            operator(filepath=path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


def view(width, area="", **options):
    """options: blender_view.OPTIONS (mode, shading, target, angles, distance, frames, frame_count, look, image). area (windowed only): empty = the 3D view
    (offscreen), 'window' = the whole window including the UI, anything else = a screenshot of that area type (NODE_EDITOR, OUTLINER, DOPESHEET_EDITOR...)."""
    if bpy.app.background:
        if area:
            raise RuntimeError("area cannot be chosen in the background (no window): leave area empty, the scene is drawn offscreen")
        return blender_view.draw(width, view3d, _view_fallback, **options)
    if area and options:
        raise RuntimeError("area cannot be combined with image options (mode, shading...): area is a screenshot")
    if area == "window":
        w = bpy.context.window_manager.windows[0]
        return _screenshot(bpy.ops.screen.screenshot, window=w, screen=w.screen)
    if area:
        g = view3d(area.upper())
        if not g:
            open_types = sorted({a.type for w in bpy.context.window_manager.windows for a in w.screen.areas})
            raise RuntimeError(f"no open area of type '{area}'. Open area types: {', '.join(open_types)}")
        return _screenshot(bpy.ops.screen.screenshot_area, **g)
    g = view3d()
    if not g:
        raise RuntimeError("no open 3D view (the view is closed)")
    try:
        return blender_view.draw(width, view3d, None, **options)
    except Exception as e:        # no offscreen drawing (GPU/driver): screenshot of the area instead; the splash window may cover it
        if options:
            raise
        a = active()
        if a is not None:
            a.warn(f"view: offscreen drawing did not work ({e!r}); a window screenshot was returned, the splash window may cover it")
        return _screenshot(bpy.ops.screen.screenshot_area, **g)


# --- api(): the running Blender's own API (identical to its version and enabled add-ons) ---

def _ops_lines():
    lines = []
    for m in dir(bpy.ops):
        if m.startswith("_"):
            continue
        module = getattr(bpy.ops, m)
        for o in dir(module):
            if o.startswith("_"):
                continue
            try:
                r = getattr(module, o).get_rna_type()
            except Exception:
                continue
            lines.append((f"bpy.ops.{m}.{o}", (r.description or r.name or "").strip(), r))
    return lines


def _type_lines():
    """Operator/menu/panel/header/list classes (…_OT_…) are the same thing as ops or the UI; the data types remain."""
    lines = []
    for name in dir(bpy.types):
        if re.search(r"_(OT|MT|PT|HT|UL)_", name):
            continue
        try:
            rna = getattr(getattr(bpy.types, name, None), "bl_rna", None)
        except Exception:
            continue
        if rna is not None:
            lines.append((f"bpy.types.{name}", (rna.description or "").strip(), rna))
    return lines


def _signature():
    """Every operator is a bpy.types class: the count changes when an add-on is enabled/disabled or a class is registered."""
    return len(dir(bpy.types))


def _properties(rna):
    return [p for p in rna.properties if p.identifier != "rna_type" and "_OT_" not in p.identifier]   # …_OT_… is a macro step


def _property(p):
    kind = p.type.lower() + (f"[{p.array_length}]" if getattr(p, "array_length", 0) else "")
    if p.type == "ENUM":
        kind += "{" + "|".join(e.identifier for e in list(p.enum_items)[:12]) + "}"
    elif p.type in ("POINTER", "COLLECTION") and getattr(p, "fixed_type", None):
        kind += f"<{p.fixed_type.identifier}>"
    return f"{p.identifier}: {kind} — {(p.description or p.name or '').strip()}"[:200]


def _detail(name, description, rna):
    props = _properties(rna)
    base = rna.base
    base_names = {p.identifier for p in base.properties} if base else set()
    own = [p for p in props if p.identifier not in base_names]
    inherited = [p.identifier for p in props if p.identifier in base_names]
    s = [f"{name} — {description}"] + ["  " + _property(p) for p in own[:60]]
    if len(own) > 60:
        s.append(f"  ... {len(own) - 60} more properties")
    if inherited:
        s.append(f"  (inherited from {base.identifier}: " + ", ".join(inherited[:30]) + (", ..." if len(inherited) > 30 else "") + ")")
    methods = [] if name.startswith("bpy.ops.") else [f.identifier for f in getattr(rna, "functions", [])]     # meaningless on an operator (bl_system_properties_get)
    if methods:
        s.append("  methods: " + ", ".join(methods[:25]) + (", ..." if len(methods) > 25 else ""))
    return "\n".join(s)


def _line(name, description, rna):
    if not name.startswith("bpy.ops."):
        return f"{name} — {description[:100]}"
    params = [p.identifier for p in _properties(rna)]
    return f"{name}({', '.join(params[:6])}{', ...' if len(params) > 6 else ''}) — {description[:100]}"


def api(query, max_hits=8):
    """Searches the names/descriptions of bpy.ops operators and bpy.types data types (open add-ons included). Give an exact name and it dumps the
    parameters/properties: 'bpy.ops.mesh.primitive_cube_add', 'bpy.types.Object'."""
    sig = _signature()
    if _index.get("sig") != sig:
        _index.update(sig=sig, ops=_ops_lines(), types=_type_lines())
    query = query.strip()
    everything = _index["ops"] + _index["types"]
    exact = next((s for s in everything if s[0].lower() == query.lower()), None)
    if exact:
        return _detail(*exact)
    tokens = [t for t in re.split(r"[\s._]+", query.lower()) if t]
    if not tokens:
        return "query is empty"
    hits = []
    for name, description, rna in everything:
        name_l, desc_l = name.lower(), description.lower()
        if all(t in name_l or t in desc_l for t in tokens):
            score = sum(3 for t in tokens if t in name_l) + sum(1 for t in tokens if t in desc_l)
            hits.append((-score, len(name), name, description, rna))
    hits.sort(key=lambda x: x[:3])
    if not hits:
        names = {s[0].lower(): s[0] for s in everything}
        candidates = difflib.get_close_matches(query.lower(), list(names), n=3, cutoff=0.6)
        ratio = lambda a: difflib.SequenceMatcher(None, query.lower(), a).ratio()  # noqa: E731
        close = [names[a] for a in candidates if ratio(a) >= ratio(candidates[0]) - 0.04]     # a shared prefix inflates the ratio: far-from-best names are noise
        return (f"no match for '{query}' ({len(_index['ops'])} operators, {len(_index['types'])} types scanned)"
                + (f"; close names: {', '.join(close)}" if close else "")
                + ". If it is an add-on operator check that the add-on is enabled; it may have been removed in a newer version.")
    lines = [_line(name, description, rna) for _, _, name, description, rna in hits[:max_hits]]
    if len(hits) > max_hits:
        lines.append(f"... {len(hits) - max_hits} more matches; narrow the query")
    lines.append("next: search the exact name for its parameters/properties, e.g. '" + hits[0][2] + "'")
    return "\n".join(lines)


def error_hint(e):
    m = str(e)
    last_line = ""
    if e.__traceback__ is not None:
        import traceback
        last_line = traceback.extract_tb(e.__traceback__)[-1].line or ""
    if isinstance(e, AttributeError) and "NoneType" in m and "active" in last_line:
        return ("there is no active object (empty at start in the background and in a new file): assign one with "
                "bpy.context.view_layer.objects.active = bpy.data.objects['Name'] and select it")
    if isinstance(e, ReferenceError) and "StructRNA" in m:
        return "the bpy object reference is stale (a file was loaded, undo ran or the object was deleted); get the object again from bpy.data"
    if isinstance(e, RuntimeError) and ("poll()" in m or "context is incorrect" in m):
        return ("the operator context is not suitable: try with bpy.context.temp_override(**tether.view3d()): ... (there is no window in the "
                "background; then work with bpy.data/bmesh); also check the mode and the selection")
    if isinstance(e, NameError) and getattr(e, "name", None) in getattr(active(), "dropped", ()):
        return f"'{e.name}' was dropped when a file loaded (the namespace is reset on every load); define it again"
    if isinstance(e, KeyError):
        collection, key = re.search(r"bpy\.data\.(\w+)\[", last_line), re.search(r'key "(.*)" not found', m)
        items = getattr(bpy.data, collection.group(1), None) if collection else None
        if key and hasattr(items, "keys"):
            names = list(items.keys())
            close = difflib.get_close_matches(key.group(1), names, n=3, cutoff=0.5)
            return (f"'{key.group(1)}' is not in bpy.data.{collection.group(1)} ({len(names)} entries)"
                    + (f"; close: {', '.join(close)}" if close else "") + ("; first names: " + ", ".join(names[:12]) if names else ""))
    return None


def setup_namespace(ns):
    """Ready names: the imports that real calls use most (bpy, mathutils, numpy). D = bpy.data and C = bpy.context stay valid after a file load.
    No collection aliases (O = bpy.data.objects): a stale sub-structure reference can crash Blender."""
    import bmesh
    import collections
    import json
    import mathutils
    import numpy
    from mathutils.bvhtree import BVHTree
    from pathlib import Path
    tether = types.SimpleNamespace(api=api, view3d=view3d, brief=brief, refresh=refresh, select=helpers.select, delete=helpers.delete, save=helpers.save,
                                   backup=helpers.backup, mode=helpers.mode, edit=helpers.edit, export_sollumz=helpers.export_sollumz)
    ns.update(bpy=bpy, bmesh=bmesh, mathutils=mathutils, math=math, Vector=mathutils.Vector, Matrix=mathutils.Matrix,
              Euler=mathutils.Euler, Quaternion=mathutils.Quaternion, BVHTree=BVHTree, np=numpy, os=os, sys=sys, json=json, re=re,
              time=time, Path=Path, collections=collections, Counter=collections.Counter, defaultdict=collections.defaultdict,
              D=bpy.data, C=bpy.context, tether=tether)


def prepare(p):
    """Runs before every run. In the background, add-ons requested at startup are re-enabled if they dropped out of the preferences: a script that
    calls wm.read_factory_settings drops e.g. Sollumz from the preferences and its operators vanish at the NEXT file load (read_homefile/open_mainfile)
    (measured). blend = open that file, clean = return to the factory startup scene (like a fresh 'blender -b --factory-startup'); both are refused
    in a windowed Blender: the user's open scene would silently vanish."""
    a = active()
    if (p.get("blend") or p.get("clean")) and a is not None and a.mode == "gui":
        raise RuntimeError("blend/clean would wipe the windowed Blender's open scene: use a background Blender (open with background=True) "
                           "(or call bpy.ops.wm.open_mainfile in the code if you really mean it)")
    closed = [e for e in getattr(a, "addons", ()) if e not in bpy.context.preferences.addons]
    if closed:
        errors = _enable_addons(closed)
        a.warn(f"{', '.join(closed)} had been closed (a script may have called wm.read_factory_settings): enabled again"
               + (f" but could not be enabled: {', '.join(errors)}" if errors else ""))
    if p.get("blend"):
        bpy.ops.wm.open_mainfile(filepath=p["blend"], load_ui=False)
    elif p.get("clean"):
        bpy.ops.wm.read_homefile(use_factory_startup=True)


# --- stale bpy references ---
# When a file loads or undo runs, ID references raise a clean ReferenceError, but sub-structure references (the mesh.vertices proxy, a vertex)
# silently go stale and touching them can kill Blender with an ACCESS_VIOLATION. References are still valid in load_pre; the namespace is
# cleared at that moment (load_post is too late). References hidden in closures/attributes/default arguments cannot be scanned: on a file load
# the WHOLE namespace is dropped for that reason, on undo only the names that were scanned.

def _is_rna(v):
    return (isinstance(v, (bpy.types.bpy_struct, bpy.types.bpy_prop_collection, bpy.types.bpy_prop_array))
            or (type(v).__module__ == "mathutils" and getattr(v, "owner", None) is not None))


def _contains_rna(v, d=0):
    if _is_rna(v):
        return True
    if d < 3:
        if isinstance(v, dict):
            return any(_contains_rna(x, d + 1) for x in list(v.values())[:200])
        if isinstance(v, (list, tuple, set, frozenset)):
            return any(_contains_rna(x, d + 1) for x in list(v)[:200])
    return False


def _user_names(a):
    ready = set(a._new_namespace())
    return [k for k in a._ns if k not in ready and not k.startswith("__")]


@persistent
def _load_pre(*_):
    a = active()
    if a is None or a._ns is None:
        return
    names = _user_names(a)
    a._ns = None
    if names:
        a.dropped = set(names)                # error_hint: the next NameError is caused by this load
        a.warn("namespace RESET (a file was loaded; a stale bpy reference can crash Blender). Dropped names: " + ", ".join(names[:20]))


@persistent
def _undo_pre(*_):
    a = active()
    if a is None or a._ns is None:
        return
    stale = [k for k in _user_names(a) if _contains_rna(a._ns[k])]
    for k in stale:
        del a._ns[k]
    if stale:
        a.warn("undo/redo: stale bpy references were removed from the namespace: " + ", ".join(stale[:20]))


def _add_handlers():
    for handlers, f in ((bpy.app.handlers.load_pre, _load_pre), (bpy.app.handlers.undo_pre, _undo_pre), (bpy.app.handlers.redo_pre, _undo_pre)):
        if f not in handlers:
            handlers.append(f)


def _remove_handlers():
    for handlers, f in ((bpy.app.handlers.load_pre, _load_pre), (bpy.app.handlers.undo_pre, _undo_pre), (bpy.app.handlers.redo_pre, _undo_pre)):
        if f in handlers:
            handlers.remove(f)


# --- lifecycle ---
# The agent is kept in tether.agent.ACTIVE, NOT in bpy.app.driver_namespace: driver_namespace is reset on every file load (open_mainfile,
# read_homefile); if the pump looked the agent up there, the timer would remove itself and kill the bridge.

def active():
    a = ACTIVE.get("blender")
    return a if a is not None and not a.stopped.is_set() else None


def _modal_operators():
    try:
        return [m.bl_idname for w in bpy.context.window_manager.windows for m in w.modal_operators]
    except Exception:
        return []


def _pump():
    a = active()
    if a is None:
        return None
    modal = _modal_operators()
    a.extra = {"modal_operators": modal[:5]} if modal else {}
    if modal:
        # the user is in the middle of a modal operator such as move/paint: running an operator from a timer is a known crash source;
        # jobs wait until the modal operator ends (queue_wait_s and info.extra show it)
        a.heartbeat = time.time()
        now = time.time()
        if _modal["since"] is None:
            _modal["since"] = now
        if now - _modal["since"] < MODAL_WAIT_S:
            return 0.1
        if not _modal["warned"]:
            _modal["warned"] = True
            a.warn(f"modal operator ({', '.join(modal[:3])}) has been running for {MODAL_WAIT_S} s; jobs were run anyway")
    else:
        _modal["since"], _modal["warned"] = None, False
    try:
        a.pump()
    except BaseException as e:                    # if a timer raises, Blender removes it: the bridge would die silently
        print(f"[tether] pump error: {e!r}")
    return 0.002 if a.queue.qsize() else 0.005


def start_gui():
    """Starts the agent in a windowed Blender; returns the existing one if it is already running."""
    existing = active()
    if existing is not None:
        return existing
    a = Agent("blender", bpy.app.version_string, mode="gui", setup=setup_namespace, search=api, view=view, error_hint=error_hint, hint=HINT,
              prepare=prepare, functions=FUNCTIONS)
    a.start()
    _add_handlers()
    bpy.app.timers.register(_pump, persistent=True)
    return a


def stop_gui():
    a = active()
    if a is not None:
        a.stop()
    _remove_handlers()


def _enable_addons(names):
    errors = []
    for name in names:
        try:
            if bpy.ops.preferences.addon_enable(module=name) != {"FINISHED"}:
                errors.append(name)
        except Exception as e:
            errors.append(f"{name} ({e})")
    return errors


def daemon(addons=(), owner_pid=0, idle_s=0):
    """Background Blender: blocks the main thread and runs commands until close, the idle timeout or the owner process's death."""
    # The config path is read at call time: the daemon's wm.save_userpref (under --factory-startup it writes factory settings into the user's
    # userpref.blend), the startup.blend read and the recent-files writes should go to a private folder, not the real config.
    # Sollumz's szio dependency lives in the user's config/sollumz/data/lib: its path is taken before redirecting, otherwise Sollumz starts in
    # "missing dependency" mode (the import/export operators stay unregistered).
    real_config = bpy.utils.user_resource("CONFIG")
    config = HOME / "blender_config"
    config.mkdir(parents=True, exist_ok=True)
    os.environ["BLENDER_USER_CONFIG"] = str(config)
    if "Sollumz" in addons:
        szio = os.path.join(real_config, "sollumz", "data", "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages")
        if os.path.isdir(szio) and szio not in sys.path:
            sys.path.append(szio)
    errors = _enable_addons(addons)
    if "Sollumz" in addons and not hasattr(bpy.ops.sollumz, "import_assets"):
        errors.append("Sollumz (dependency missing: no import/export operators)")
    a = Agent("blender", bpy.app.version_string, mode="background", setup=setup_namespace, search=api, view=view, error_hint=error_hint,
              closable=True, hint=HINT, idle_s=idle_s, owner_pid=owner_pid, prepare=prepare, functions=FUNCTIONS)
    a.addons = [e for e in addons if not any(h == e or h.startswith(e + " (") for h in errors)]     # an add-on that failed to enable is not retried on every call
    a.start()
    _add_handlers()
    if errors:
        a.warn("add-on could not be enabled: " + ", ".join(errors))
    a.loop()
    time.sleep(0.3)                                # let the close response reach the client


def _arg(name, multiple=False):
    k = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    values = [k[i + 1] for i, x in enumerate(k[:-1]) if x == name]
    return values if multiple else (values[0] if values else None)


def _write_startup_error():
    import traceback
    folder = HOME / "startup"
    folder.mkdir(parents=True, exist_ok=True)       # a process started through WMI has no stdio: the client reads the reason from here
    (folder / f"blender-{os.getpid()}.log").write_text(traceback.format_exc(), encoding="utf-8")


if __name__ == "__main__":
    try:
        _a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
        if "--daemon" in _a:
            daemon(_arg("--addon", True), int(_arg("--owner-pid") or 0), float(_arg("--idle-s") or 0))
        elif "--gui" in _a:
            start_gui()
    except BaseException:
        _write_startup_error()
        raise
