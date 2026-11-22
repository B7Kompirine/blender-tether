"""Helpers in the Blender namespace (available as tether.select, tether.delete, tether.save, tether.backup, tether.mode, tether.edit, tether.export_sollumz).
Each one validates and raises a clear error instead of failing silently: select_set doing nothing on a locked/hidden object, the Sollumz export saying
FINISHED without writing a file while sz_lods is empty, a stale bound_box, the cost of deleting one object at a time."""
import contextlib
import io
import os
import re
import time

import bmesh
import bpy

from tether.agent import HOME


def _obj(n):
    """Name or object -> object; if it does not exist, a clear error with the names that do."""
    if isinstance(n, bpy.types.Object):
        return n
    o = bpy.data.objects.get(n)
    if o is None:
        raise KeyError(f"no such object: {n!r}. First names in the scene: " + ", ".join(list(bpy.data.objects.keys())[:12]))
    return o


def _layer(lc, coll):
    if lc.collection == coll:
        return lc
    for c in lc.children:
        r = _layer(c, coll)
        if r is not None:
            return r
    return None


def _blockers(o):
    """Situations in which select_set SILENTLY does nothing: hidden, locked, collection/layer hidden or excluded."""
    vl = bpy.context.view_layer
    if o.name not in vl.objects:
        return ["not in this view layer (is its collection linked to the scene?)"]
    problems = []
    if o.hide_get():
        problems.append("hidden (hide_set)")
    if o.hide_viewport:
        problems.append("hide_viewport")
    if o.hide_select:
        problems.append("hide_select")
    for coll in o.users_collection:
        if coll.hide_select:
            problems.append(f"collection '{coll.name}' hide_select")
        if coll.hide_viewport:
            problems.append(f"collection '{coll.name}' hide_viewport")
        lc = _layer(vl.layer_collection, coll)
        if lc is not None and (lc.exclude or lc.hide_viewport):
            problems.append(f"collection '{coll.name}' is excluded/hidden in this view layer")
    return problems


def _unhide(o):
    vl = bpy.context.view_layer
    for coll in o.users_collection:
        coll.hide_select = coll.hide_viewport = False
        lc = _layer(vl.layer_collection, coll)
        if lc is not None:
            lc.exclude = lc.hide_viewport = False
    o.hide_viewport = o.hide_select = False
    o.hide_set(False)


def select(*objects, unhide=False, extend=False):
    """Selects only these objects (extend=True: adds to the current selection), makes the first one active and COUNTS the selection. On a locked/hidden
    object select_set silently does nothing and the Sollumz export says 'No Sollumz objects selected': this raises with the reason instead;
    unhide=True clears the blockers. Deselects with select_set(False) per object instead of select_all(DESELECT). Returns the selected objects."""
    vl = bpy.context.view_layer
    targets = [_obj(n) for n in objects]
    if not extend:
        for o in vl.objects:
            o.select_set(False)
    for o in targets:
        problems = _blockers(o)
        if problems:
            if not unhide:
                raise RuntimeError(f"{o.name}: {', '.join(problems)}; select_set silently does nothing here. select(..., unhide=True) clears the blockers")
            _unhide(o)
        o.select_set(True)
    if targets:
        vl.objects.active = targets[0]
    selected = {o.name for o in vl.objects if o.select_get()}
    missing = [o.name for o in targets if o.name not in selected]
    if missing:
        raise RuntimeError(f"the selection did not stick: {missing}" + ("; the scene was never saved (select_set may not stick in an unsaved scene: save() first)"
                                                                          if not bpy.data.filepath else ""))
    return [o for o in vl.objects if o.select_get()]


def delete(*objects):
    """Bulk delete: bpy.data.batch_remove (removing objects one by one is 38x slower at 3000 objects and takes minutes at 68k) +
    view_layer.update() (so deleted objects do not linger in the list across consecutive exports). Unknown names are skipped; returns the number deleted."""
    found = []
    for n in objects:
        o = bpy.data.objects.get(n) if isinstance(n, str) else n
        if o is not None:
            found.append(o)
    bpy.data.batch_remove(found)
    bpy.context.view_layer.update()
    return len(found)


def save(path=None, compress=False):
    """save_as_mainfile + create the folder + verify the file was really written. Without a path: the open file. Returns (path, bytes)."""
    path = os.path.abspath(path) if path else bpy.data.filepath
    if not path:
        raise ValueError("save(path): the scene was never saved, give a path")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=compress)
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    if size == 0:
        raise RuntimeError(f"save: {path} was not written or is 0 bytes")
    return path, size


def backup(name="scene"):
    """Copies the open file to ~/.tether/backup without touching it (copy=True); the last 20 stay. To go back: open_mainfile(path)."""
    folder = HOME / "backup"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{re.sub(r'[^\w.-]', '_', name)}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, compress=True)
    for old in sorted(folder.glob("*.blend"), key=lambda p: p.stat().st_mtime)[:-20]:
        old.unlink(missing_ok=True)
    return str(path)


def mode(obj, target="OBJECT"):
    """Makes the object active and switches its mode (mode_set fails its poll without an active object) and verifies the switch. Returns the previous mode."""
    o = _obj(obj)
    vl = bpy.context.view_layer
    if o.name not in vl.objects:
        raise RuntimeError(f"{o.name}: not in this view layer")
    previous = o.mode
    vl.objects.active = o
    if previous != target:
        bpy.ops.object.mode_set(mode=target)
    if o.mode != target:
        raise RuntimeError(f"{o.name}: mode {target} did not take (now {o.mode}); is it hidden/locked, or does this type not support the mode?")
    return previous


@contextlib.contextmanager
def edit(obj):
    """with tether.edit('Cube') as b: ...  In object mode: bmesh.new() + from_mesh, on exit to_mesh + free + view_layer.update, so bound_box is not
    stale (mesh.update() does not refresh it). If an error occurs nothing is written to the mesh and the bmesh is still freed. In edit mode it
    uses from_edit_mesh/update_edit_mesh."""
    o = _obj(obj)
    if o.type != "MESH":
        raise TypeError(f"{o.name}: not a MESH ({o.type})")
    me = o.data
    if o.mode == "EDIT":
        b = bmesh.from_edit_mesh(me)
        yield b
        bmesh.update_edit_mesh(me)
    else:
        b = bmesh.new()
        try:
            b.from_mesh(me)
            yield b
            b.to_mesh(me)
        finally:
            b.free()
        me.update()
    bpy.context.view_layer.update()


def _files(folder):
    d = {}
    for root, _, names in os.walk(folder):
        for n in names:
            p = os.path.join(root, n)
            s = os.stat(p)
            d[p] = (s.st_mtime_ns, s.st_size)
    return d


def export_sollumz(folder, objects=None, fmt="NATIVE", version="GEN8", unhide=False, **extra):
    """Sollumz export with the verified recipe: the folder is created (otherwise Blender hangs on exit), the objects are selected with select() and
    COUNTED, sz_lods.high.mesh is filled if empty (with it empty the export says FINISHED but skips the drawable entirely), use_custom_settings=True
    (False ignores the given settings), then the WRITTEN FILES are measured: if the operator says FINISHED but no file exists or one is 0 bytes it
    raises with likely causes. Without objects: the current selection. Returns [(path relative to the folder, bytes)]; extra goes to export_assets."""
    folder = os.path.abspath(folder)
    os.makedirs(folder, exist_ok=True)
    if objects is None:
        objects = [o.name for o in bpy.context.view_layer.objects if o.select_get()]
    if not objects:
        raise ValueError("export_sollumz: give objects or select first")
    targets = select(*objects, unhide=unhide)
    for o in targets:
        for m in (o, *o.children_recursive):
            if getattr(m, "sollum_type", "") == "sollumz_drawable_model" and m.sz_lods.high.mesh is None:
                m.sz_lods.high.mesh = m.data
                print(f"export_sollumz: {m.name} had an empty sz_lods.high.mesh, mesh assigned")
    before = _files(folder)
    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        result = bpy.ops.sollumz.export_assets(directory=folder + os.sep, direct_export=True, use_custom_settings=True, limit_to_selected=True,
                                               target_formats={fmt}, target_versions={version}, **extra)
    text = log.getvalue()
    print(text, end="")
    after = _files(folder)
    written = sorted((os.path.relpath(p, folder), v[1]) for p, v in after.items() if before.get(p) != v)
    if not written or any(b == 0 for _, b in written):
        lines = [s for s in text.splitlines() if any(a in s for a in ("no Sollumz materials", "No Sollumz objects", "Aborting", "Failed", "ERRORS"))]
        raise RuntimeError(f"export_sollumz: the operator returned {sorted(result)} but {'no file was written' if not written else 'a 0-byte file exists'} ({folder}). "
                           f"Sollumz log: {lines[:3] or 'none'}. Likely causes: the object has no sollum_type (objects built with bpy.data.objects.new do "
                           "not), the mesh has no Sollumz material, a hidden/locked selection.")
    return written
