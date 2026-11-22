"""Blender checks shared by the background daemon and the windowed Blender, and the namespace helpers (tether.select, delete, save, ...)."""
import os
import time
from pathlib import Path

import harness
from harness import TMP, check, client


def common(sel, c, mode):
    """Checks shared by the background and the windowed Blender; c(code) -> response."""
    copy = (TMP / f"copy_{mode}.blend").as_posix()
    c("x = 'persistent'")
    check(f"[{mode}] bpy is ready, the Blender version is readable", c("bpy.app.version_string").get("result", "").count(".") >= 1)
    y = c("[np.__name__, os.__name__, sys.__name__, json.__name__, re.__name__, time.__name__, math.__name__, Path('a/b').name, Counter('aab')['a'], "
          "defaultdict(int)['x'], collections.__name__, BVHTree.__name__, Vector((1, 2, 3)).length > 3, D is bpy.data, C is bpy.context]")
    check(f"[{mode}] the ready names work without imports (np, os, sys, json, re, time, math, Path, Counter, defaultdict, BVHTree, D, C)",
          y.get("result") == ["numpy", "os", "sys", "json", "re", "time", "math", "b", 2, 0, "collections", "BVHTree", True, True, True], y)
    check(f"[{mode}] the helpers are ready under `tether`",
          c("all(callable(getattr(tether, n)) for n in ('select', 'delete', 'save', 'backup', 'mode', 'edit', 'export_sollumz', 'api', 'view3d', 'brief', 'refresh'))").get("result") is True)
    check(f"[{mode}] the scene report works (host function 'report'); info.functions lists it",
          client.report(sel).startswith("scene '") and "report" in client.request(sel, "info")["functions"])
    y = c(f"bpy.ops.wm.save_as_mainfile(filepath={copy!r}, copy=True)")
    check(f"[{mode}] a copy is saved", y["ok"], y.get("error"))
    # a file load drops the namespace: a stale sub-structure reference (mesh.vertices) can kill Blender with an ACCESS_VIOLATION
    c("me = bpy.data.meshes.new('tether_t')\nme.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])\nv0 = me.vertices[0]")
    y = c(f"bpy.ops.wm.open_mainfile(filepath={copy!r})")
    time.sleep(1.5)
    check(f"[{mode}] open_mainfile: the call returns, the warning says the namespace was reset and which names dropped",
          y["ok"] and "RESET" in y.get("warning", "") and "me" in y["warning"], y)
    y = c("me.vertices[0].co")
    check(f"[{mode}] the stale mesh reference is gone: access does not crash, it gives NameError", not y["ok"] and y["error"]["type"] == "NameError", y)
    check(f"[{mode}] the NameError of a dropped name says why (a file load)", "was dropped when a file loaded" in y["error"].get("hint", ""), y.get("error"))
    check(f"[{mode}] the bridge is alive after open_mainfile (in the GUI the timer must not die); the namespace is clean but ready",
          c("x").get("ok") is False and c("bpy.app.version_string").get("ok") is True)
    c("y = 5")
    y = c("bpy.ops.wm.read_homefile(use_empty=True)")
    time.sleep(1.5)
    check(f"[{mode}] read_homefile: the namespace is reset and the bridge lives", "RESET" in y.get("warning", "") and c("y").get("ok") is False and c("1").get("result") == 1, y)
    y = c("bpy.context.view_layer.objects.active = None\nbpy.context.active_object.name")
    check(f"[{mode}] the 'no active object' hint", "there is no active object" in y["error"].get("hint", ""), y.get("error"))
    c("bpy.data.objects.new('Box', None)")
    y = c("bpy.data.objects['Boxx']")
    check(f"[{mode}] a data name that does not exist: the hint gives the collection, the close name and the entry count",
          y["error"]["type"] == "KeyError" and "'Boxx' is not in bpy.data.objects" in y["error"].get("hint", "") and "close: Box" in y["error"]["hint"], y.get("error"))
    check(f"[{mode}] the KeyError text does not print the exception line twice", client.format_run(y)[0].count("KeyError") == 1, client.format_run(y)[0])
    c("bpy.data.objects.remove(bpy.data.objects['Box'])")
    check(f"[{mode}] api_search: an exact name dumps the parameters", "size: float" in client.api_search(sel, "bpy.ops.mesh.primitive_cube_add"))
    check(f"[{mode}] api_search: a word search gives matches and a 'next' hint",
          "extrude" in client.api_search(sel, "extrude").lower() and "next:" in client.api_search(sel, "extrude"))
    typo = client.api_search(sel, "bpy.ops.mesh.primitive_kube_add")
    check(f"[{mode}] api_search: a typo gets a close-name suggestion", "bpy.ops.mesh.primitive_cube_add" in typo, typo)
    check(f"[{mode}] api_search: no far (noise) names among the suggestions", "monkey_add" not in typo, typo)
    check(f"[{mode}] api_search: no methods line on an operator, one on a data type",
          "methods" not in client.api_search(sel, "bpy.ops.mesh.primitive_cube_add") and "methods" in client.api_search(sel, "bpy.types.Object"))
    c("class TETHER_OT_t(bpy.types.Operator):\n    bl_idname = 'mesh.tether_test'\n    bl_label = 't'\n    bl_description = 'index freshness'\n"
      "    def execute(self, context): return {'FINISHED'}\nbpy.utils.register_class(TETHER_OT_t)")
    check(f"[{mode}] api_search: an operator registered later is found (the index does not go stale)", "tether_test" in client.api_search(sel, "tether_test"))
    c("bpy.utils.unregister_class(TETHER_OT_t)")
    y = c("tether.brief({'a': list(range(1000)), 'v': Vector((1.23456789, 2, 3))})")
    check(f"[{mode}] brief(): a long list and a Vector are shortened", "…(+992)" in str(y.get("result")) and y["result"]["v"][0] == 1.23457, y)
    y = c("print('x' * 5_000_000)")
    check(f"[{mode}] a 5 MB output is truncated", y["truncated"] and len(y["output"]) < 10000)
    t = time.time()
    for _ in range(30):
        c("1")
    avg = (time.time() - t) / 30 * 1000
    print(f"      [{mode}] 30 consecutive calls: {avg:.1f} ms on average")
    check(f"[{mode}] call latency < 100 ms", avg < 100, f"{avg:.1f} ms")


def helpers(c):
    """Namespace helpers: each one reproduces the trap with the raw API first, then checks that the helper catches it."""
    print("      [daemon] helpers: select, delete, save, backup, mode, edit, export_sollumz")
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
    y = c("[o.name for o in tether.select('Cube')]")
    check("[helper] select: selects the object, makes it active, counts the selection",
          y.get("result") == ["Cube"] and c("[o.name for o in C.view_layer.objects if o.select_get()] == ['Cube'] and C.view_layer.objects.active.name == 'Cube'").get("result") is True, y)
    c("lock = bpy.data.objects.new('locked', None)\nC.collection.objects.link(lock)\nlock.hide_select = True\nlock.select_set(True)")
    check("[helper] trap reproduced: select_set silently does nothing on a hide_select object", c("lock.select_get()").get("result") is False)
    y = c("tether.select('locked')")
    check("[helper] select: on a locked object a clear error with the reason", not y["ok"] and "hide_select" in y["error"]["message"] and "locked" in y["error"]["message"], y.get("error"))
    y = c("[o.name for o in tether.select('locked', unhide=True)]")
    check("[helper] select(unhide=True): clears the blocker, the object really gets selected", y.get("result") == ["locked"] and c("lock.hide_select").get("result") is False, y)
    c("hid = bpy.data.objects.new('hid', None)\nC.collection.objects.link(hid)\nhid.hide_set(True)\nhid.select_set(True)")
    check("[helper] trap reproduced: select_set does nothing on a hidden object", c("hid.select_get()").get("result") is False)
    y = c("tether.select('hid')")
    check("[helper] select: on a hidden object a clear error with the reason", not y["ok"] and "hidden" in y["error"]["message"], y.get("error"))
    y = c("tether.select('no_such')")
    check("[helper] select: a name that does not exist -> KeyError and the names in the scene", not y["ok"] and y["error"]["type"] == "KeyError" and "Cube" in y["error"]["message"], y.get("error"))
    check("[helper] select(extend=True) adds to the current selection; select() drops everything",
          c("tether.select('Cube')\ntether.select('Light', extend=True)\n[o.name for o in C.view_layer.objects if o.select_get()]").get("result") == ["Cube", "Light"]
          and c("tether.select()\n[o.name for o in C.view_layer.objects if o.select_get()]").get("result") == [])
    y = c("def _measure():\n    names = {}\n    for kind in ('loop', 'bulk'):\n        l = [bpy.data.objects.new(f'{kind}{i}', None) for i in range(3000)]\n"
          "        for o in l:\n            C.collection.objects.link(o)\n        names[kind] = [o.name for o in l]\n"
          "    t = time.perf_counter()\n    for n in names['loop']:\n        bpy.data.objects.remove(bpy.data.objects[n], do_unlink=True)\n    loop = time.perf_counter() - t\n"
          "    t = time.perf_counter()\n    n = tether.delete(*names['bulk'])\n    return [loop, time.perf_counter() - t, n]\nresult = _measure()")
    loop, bulk, n = y["result"]
    print(f"      [daemon] deleting 3000 objects: one by one {loop:.3f} s, delete() {bulk:.3f} s ({loop / max(bulk, 1e-6):.0f}x)")
    check("[helper] delete: bulk delete is at least 3x faster than one by one, 3000 objects go, no None is left in the view layer",
          n == 3000 and bulk * 3 < loop and c("[o for o in C.view_layer.objects if o is None] == [] and not any(x.name.startswith('bulk') for x in bpy.data.objects)").get("result") is True,
          (loop, bulk, n))
    check("[helper] delete: a name that does not exist is skipped (returns 0)", c("tether.delete('no_such_object')").get("result") == 0)
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
    path = (TMP / "helper" / "sub" / "x.blend").as_posix()
    y = c(f"tether.save({path!r})")
    check("[helper] save: creates the folder, writes the file and measures it (returns the bytes), the open file becomes that one",
          bool(y.get("result")) and y["result"][1] > 1000 and Path(path).is_file() and os.path.normcase(c("bpy.data.filepath")["result"]) == os.path.normcase(os.path.abspath(path)), y)
    y = c("[bpy.data.filepath, tether.backup('test'), bpy.data.filepath]")
    check("[helper] backup: writes a copy without touching the open file",
          y.get("result") and y["result"][0] == y["result"][2] and Path(y["result"][1]).is_file() and Path(y["result"][1]).stat().st_size > 1000, y)
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
    y = c("tether.save()")
    check("[helper] save: on a never-saved scene it asks for a path (a clear error)", not y["ok"] and "give a path" in y["error"]["message"], y.get("error"))
    y = c("[tether.mode('Cube', 'EDIT'), bpy.data.objects['Cube'].mode, tether.mode('Cube', 'OBJECT'), bpy.data.objects['Cube'].mode]")
    check("[helper] mode: makes the object active and switches the mode, returns the previous mode", y.get("result") == ["OBJECT", "EDIT", "EDIT", "OBJECT"], y)
    y = c("C.view_layer.objects.active = None\nbpy.ops.object.mode_set(mode='EDIT')")
    check("[helper] trap reproduced: with no active object the raw mode_set raises a poll error", not y["ok"] and "active object" in y["error"]["message"], y.get("error"))
    check("[helper] mode: assigns the active object itself (no poll error)",
          c("C.view_layer.objects.active = None\ntether.mode('Cube', 'EDIT')\ntether.mode('Cube')\nbpy.data.objects['Cube'].mode").get("result") == "OBJECT")
    y = c("o = bpy.data.objects['Cube']\no.data.transform(Matrix.Translation((10, 0, 0)))\n[round(o.bound_box[0][0], 3), round(min(v.co.x for v in o.data.vertices), 3)]")
    check("[helper] trap reproduced: after mesh.transform the bound_box is stale (-1), the real vertex is 9", y.get("result") == [-1.0, 9.0], y)
    y = c("with tether.edit('Cube') as b:\n    for v in b.verts:\n        v.co.x += 10\no = bpy.data.objects['Cube']\n[round(o.bound_box[0][0], 3), round(min(v.co.x for v in o.data.vertices), 3)]")
    check("[helper] edit: writes to the mesh on exit and refreshes the bound_box (same as the real vertex)", y.get("result") == [19.0, 19.0], y)
    y = c("with tether.edit('Cube') as b:\n    for v in b.verts:\n        v.co.x += 100\n    raise ValueError('on purpose')")
    check("[helper] edit: if the block raises nothing is written to the mesh and the error propagates",
          not y["ok"] and y["error"]["type"] == "ValueError" and c("round(min(v.co.x for v in bpy.data.objects['Cube'].data.vertices), 3)").get("result") == 19.0, y.get("error"))
    y = c("tether.mode('Cube', 'EDIT')\nwith tether.edit('Cube') as b:\n    for v in b.verts:\n        v.co.z += 1\ntether.mode('Cube')\nround(max(v.co.z for v in bpy.data.objects['Cube'].data.vertices), 3)")
    check("[helper] edit: in edit mode it works through from_edit_mesh", y.get("result") == 2.0, y)
    if not harness.STATE["sollumz"]:
        print("      [helper] export_sollumz: Sollumz is not available, skipped")
        return
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
    c("from Sollumz.ydr.shader_materials import create_shader\no = bpy.data.objects['Cube']\no.name = 'box'\no.data.materials.clear()\n"
      "o.data.materials.append(create_shader('default.sps'))\no.data.uv_layers.active.name = 'UVMap 0'\ntether.select('box')\nbpy.ops.sollumz.converttodrawable()")
    k1 = (TMP / "export" / "sub").as_posix()
    y = c(f"tether.export_sollumz({k1!r}, ['box'])")
    check("[helper] export_sollumz: creates the missing folder, writes the drawable, measures the written file",
          y.get("ok") and y["result"] and y["result"][0][0].startswith("box.ydr") and y["result"][0][1] > 0, y)
    k2 = (TMP / "export2").as_posix()
    os.makedirs(k2, exist_ok=True)
    c("bpy.data.objects['box.model'].sz_lods.high.mesh = None\ntether.select('box')")
    y = c(f"bpy.ops.sollumz.export_assets(directory={k2!r} + os.sep, direct_export=True, use_custom_settings=True, limit_to_selected=True, "
          "target_formats={'NATIVE'}, target_versions={'GEN8'})")
    check("[helper] trap reproduced: with sz_lods.high.mesh empty the raw export says FINISHED but writes NO file",
          y.get("result") == ["FINISHED"] and not list(Path(k2).iterdir()), y)
    y = c(f"tether.export_sollumz({k2!r}, ['box'])")
    check("[helper] export_sollumz: fills an empty sz_lods.high.mesh, the file is written and it says so in the output",
          y.get("ok") and y["result"] and y["result"][0][1] > 0 and "empty sz_lods.high.mesh" in y.get("output", ""), y)
    c("flat = bpy.data.objects.new('flat', bpy.data.meshes.new('flat_me'))\nC.collection.objects.link(flat)")
    y = c(f"tether.export_sollumz({(TMP / 'export3').as_posix()!r}, ['flat'])")
    check("[helper] export_sollumz: an object without sollum_type writes no file -> a clear error with likely causes",
          not y["ok"] and "no file was written" in y["error"]["message"] and "sollum_type" in y["error"]["message"], y.get("error"))
    c("bpy.ops.mesh.primitive_cube_add()\nbpy.context.view_layer.objects.active.name = 'no_material'\ntether.select('no_material')\nbpy.ops.sollumz.converttodrawable()")
    y = c(f"tether.export_sollumz({(TMP / 'export4').as_posix()!r}, ['no_material'])")
    check("[helper] export_sollumz: with no Sollumz material the operator may say FINISHED but it is an error, with the Sollumz log",
          not y["ok"] and "no file was written" in y["error"]["message"] and "no Sollumz materials" in y["error"]["message"], y.get("error"))
    c("bpy.data.objects['box'].hide_select = True")
    y = c(f"tether.export_sollumz({(TMP / 'export5').as_posix()!r}, ['box'])")
    check("[helper] export_sollumz: a locked drawable cannot be selected -> the clear error of select", not y["ok"] and "hide_select" in y["error"]["message"], y.get("error"))
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
