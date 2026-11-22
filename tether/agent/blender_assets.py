"""Brings a downloaded asset into Blender: a model (glTF/GLB/FBX/OBJ) under a root empty, scaled to a target size and set on the ground; a PBR texture as
a Principled material (optionally applied to objects; kept with a fake user even if it stays out of the scene); an HDRI as a new world (the old one is not
deleted). Source and license are written as custom properties (asset_source, asset_license, asset_attribution). Numeric input is validated before the
import; on an error the new data blocks are deleted and the selection and world are restored.

Host function 'import_asset' (MCP tool asset_import, which downloads on the client side):
import_asset(kind, name, file=, maps=, target_size=, location=, snap_to_ground=, apply_to=)
"""
import math
from pathlib import Path

import bpy
from mathutils import Vector

_BOUNDED = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL"}
_DATA = ("objects", "meshes", "materials", "images", "armatures", "cameras", "lights", "actions", "curves", "node_groups", "collections", "worlds")


def _inputs(target_size, location):
    """Validates the numeric input before importing (same rule as tether.assets.placement on the client): (target_size, location or None)."""
    try:
        size = float(target_size or 0)
        place = None if location is None else [float(x) for x in location]
    except (TypeError, ValueError):
        raise RuntimeError(f"target_size must be a number and location three numbers [x, y, z]: target_size={target_size!r}, location={location!r}") from None
    if not math.isfinite(size) or size < 0:
        raise RuntimeError(f"target_size must be 0 (no scaling) or positive metres, got: {target_size!r}")
    if place is not None and (len(place) != 3 or not all(math.isfinite(x) for x in place)):
        raise RuntimeError(f"location must be three numbers [x, y, z] (metres), got: {location!r}")
    return size, place


def _state():
    """State before the import: data block names, (selected objects, active object), the scene's world."""
    layer, scene = bpy.context.view_layer, bpy.context.scene
    active = layer.objects.active
    return ({k: set(getattr(bpy.data, k).keys()) for k in _DATA}, ([o.name for o in layer.objects if o.select_get()], active.name if active else None),
            scene.world.name if scene.world else None)


def _rollback(before):
    """Deletes the new data blocks of a half-finished import (objects first, so they are left without users); restores the selection, the active object and the world."""
    data, (selected, active), world = before
    for k in _DATA:
        collection = getattr(bpy.data, k)
        for name in [a for a in collection.keys() if a not in data[k]]:
            collection.remove(collection[name])
    layer, scene = bpy.context.view_layer, bpy.context.scene
    for o in layer.objects:
        o.select_set(o.name in selected)
    layer.objects.active = layer.objects.get(active) if active else None
    scene.world = bpy.data.worlds.get(world) if world else None


def _bounds(objects):
    pts = [o.matrix_world @ Vector(c) for o in objects if o.type in _BOUNDED for c in o.bound_box]
    if not pts:
        return None
    return (Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))),
            Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))))


def _v(v):
    return [round(x, 4) for x in v]


def _props(obj, name, source, license, attribution):
    obj["asset_source"], obj["asset_id"], obj["asset_license"], obj["asset_attribution"] = source, name, license, attribution


def _model(name, file, source, license, attribution, target_size, location, snap_to_ground):
    target_size, location = _inputs(target_size, location)
    path = Path(file)
    if not path.is_file():
        raise RuntimeError(f"model file not found: {file}")
    if bpy.context.mode != "OBJECT":
        raise RuntimeError(f"Blender is in '{bpy.context.mode}' mode: the import works in object mode (tether.mode(name, 'OBJECT'))")
    before, actions = set(bpy.data.objects.keys()), set(bpy.data.actions.keys())
    ext = path.suffix.lower()
    if ext in (".gltf", ".glb"):
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=str(path))
    else:
        raise RuntimeError(f"unsupported model format '{ext}' (gltf, glb, fbx, obj)")
    new = [o for o in bpy.data.objects if o.name not in before]
    if not new:
        raise RuntimeError("the import produced no objects")
    root = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(root)
    new_names = {o.name for o in new}
    for o in new:
        if o.parent is None or o.parent.name not in new_names:
            o.parent = root
    bpy.context.view_layer.update()
    bounds = _bounds(new)
    scale = 1.0
    if bounds and target_size:
        longest = max(bounds[1] - bounds[0])
        scale = float(target_size) / longest if longest > 1e-9 else 1.0
        root.scale = (scale, scale, scale)
        bpy.context.view_layer.update()
        bounds = _bounds(new)
    goal = Vector(location or (0, 0, 0))
    if bounds and snap_to_ground:
        root.location += goal - Vector(((bounds[0].x + bounds[1].x) / 2, (bounds[0].y + bounds[1].y) / 2, bounds[0].z))
        bpy.context.view_layer.update()
        bounds = _bounds(new)
    tris, materials = 0, set()
    for o in new:
        if o.type == "MESH":
            o.data.calc_loop_triangles()
            tris += len(o.data.loop_triangles)
            materials.update(m.name for m in o.data.materials if m)
    _props(root, name, source, license, attribution)
    return {"kind": "model", "root": root.name, "objects": len(new), "tris": tris, "materials": sorted(materials), "scale": round(scale, 5),
            "animations": sorted(set(bpy.data.actions.keys()) - actions), "min": _v(bounds[0]) if bounds else None, "max": _v(bounds[1]) if bounds else None,
            "size": _v(bounds[1] - bounds[0]) if bounds else None, "location": _v(root.location)}


def _texture_tree(m, maps):
    nt = m.node_tree
    nt.nodes.clear()
    out, bsdf = nt.nodes.new("ShaderNodeOutputMaterial"), nt.nodes.new("ShaderNodeBsdfPrincipled")
    out.location, bsdf.location = (300, 300), (0, 300)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    row = [0]

    def image(key, space):
        img = bpy.data.images.load(maps[key], check_existing=True)
        img.colorspace_settings.name = space
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image, n.label, n.location = img, key, (-700, 300 - 300 * row[0])
        row[0] += 1
        return n
    color = image("color", "sRGB")
    if "ao" in maps:
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type, mix.blend_type, mix.location = "RGBA", "MULTIPLY", (-350, 400)
        mix.inputs[0].default_value = 1.0
        nt.links.new(color.outputs["Color"], mix.inputs[6])
        nt.links.new(image("ao", "Non-Color").outputs["Color"], mix.inputs[7])
        nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    else:
        nt.links.new(color.outputs["Color"], bsdf.inputs["Base Color"])
    if "roughness" in maps:
        nt.links.new(image("roughness", "Non-Color").outputs["Color"], bsdf.inputs["Roughness"])
    if "metal" in maps:
        nt.links.new(image("metal", "Non-Color").outputs["Color"], bsdf.inputs["Metallic"])
    if "normal" in maps:
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nm.location = (-350, -200)
        nt.links.new(image("normal", "Non-Color").outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])


def _texture(name, maps, source, license, attribution, apply_to):
    if not maps or "color" not in maps:
        raise RuntimeError("a texture needs at least a 'color' map")
    missing = [f"{k}: {v}" for k, v in maps.items() if not Path(v).is_file()]
    if missing:
        raise RuntimeError("map file missing: " + "; ".join(missing))
    targets = []
    for n in apply_to or []:
        o = bpy.data.objects.get(n)
        if o is None:
            raise RuntimeError(f"apply_to: no such object '{n}'")
        targets.append(o)
    m = bpy.data.materials.get(f"{source}_{name}" if source else name)
    if m is not None and m.get("asset_id") != name:
        m = None                                   # a different material with the same name: do not overwrite it, the new one gets a unique name
    reused = m is not None
    if m is None:
        m = bpy.data.materials.new(f"{source}_{name}" if source else name)
        m.use_nodes = True
        _texture_tree(m, maps)
        m.use_fake_user = True
        _props(m, name, source, license, attribution)
    applied, skipped, warnings = [], [], []
    for o in targets:
        if o.type != "MESH":
            skipped.append(f"{o.name} ({o.type})")
            continue
        o.data.materials.clear()
        o.data.materials.append(m)
        applied.append(o.name)
        if not o.data.uv_layers:
            warnings.append(f"{o.name}: no UVs (the texture will not show or will stretch)")
    return {"kind": "texture", "material": m.name, "maps": sorted(maps), "applied": applied, "skipped": skipped, "warnings": warnings, "reused": reused}


def _hdri(name, file, source, license, attribution):
    if not Path(file).is_file():
        raise RuntimeError(f"hdri file not found: {file}")
    scene = bpy.context.scene
    previous = scene.world
    w = bpy.data.worlds.get(f"{source}_{name}" if source else name)
    if w is None or w.get("asset_id") != name:
        w = bpy.data.worlds.new(f"{source}_{name}" if source else name)
        w.use_nodes = True
        nt = w.node_tree
        nt.nodes.clear()
        env, bg, out = nt.nodes.new("ShaderNodeTexEnvironment"), nt.nodes.new("ShaderNodeBackground"), nt.nodes.new("ShaderNodeOutputWorld")
        env.image = bpy.data.images.load(file, check_existing=True)
        env.location, bg.location, out.location = (-400, 0), (-100, 0), (150, 0)
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
        _props(w, name, source, license, attribution)
    scene.world = w
    if previous is not None and previous is not w:
        previous.use_fake_user = True                # the old world leaving the scene must not be dropped on save
    env = next(n for n in w.node_tree.nodes if n.type == "TEX_ENVIRONMENT")
    return {"kind": "hdri", "world": w.name, "previous_world": previous.name if previous and previous is not w else None, "size": list(env.image.size)}


def import_asset(kind, name, file="", maps=None, source="", license="", attribution="", target_size=0.0, location=None, snap_to_ground=True, apply_to=None):
    """Host function 'import_asset'. kind: model | texture | hdri. Returns a plain-data report. On an error the new data blocks are deleted and the
    selection and world are restored."""
    if kind not in ("model", "texture", "hdri"):
        raise RuntimeError(f"unknown asset kind '{kind}' (model, texture, hdri)")
    before = _state()
    try:
        if kind == "model":
            return _model(name, file, source, license, attribution, target_size, location, snap_to_ground)
        if kind == "texture":
            return _texture(name, maps, source, license, attribution, apply_to)
        return _hdri(name, file, source, license, attribution)
    except BaseException as e:
        try:
            _rollback(before)
        except Exception as g:  # noqa: BLE001  the original error must not be hidden
            e.add_note(f"the rollback also failed, the scene may contain a half-finished import: {g!r}")
        raise
