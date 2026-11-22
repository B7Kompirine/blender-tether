"""Blender scene report (read-only): one header line (object counts, active, selected, mode) + the requested fields per object (placement, size, ground,
rotation, scale, parent, children, hidden, details, materials, modifiers, animation), mesh/armature health (topology: quads/tris/ngons, boundary and
non-manifold edges, loose vertices, poles; weights: vertices a deform bone does not move, deform bones without a vertex group), a scene settings line and
the GTA/Sollumz checks (UVMap 0, Color 1, sz_lods, Sollumz shader, missing textures, unapplied transforms, .001 suffix).

Host function 'report' (MCP tool scene): report(fields=['placement', 'topology'], query='door', root='Name', limit=20)
"""
import collections
import math
import os
import re

import bmesh
import bpy
from mathutils import Vector

GROUPS = {
    "placement": ["location", "size", "ground", "rotation", "scale", "parent"],
    "contents": ["children", "hidden", "details", "materials", "modifiers", "animation"],
    "health": ["topology", "weights"],
}
ALL_FIELDS = [a for g in GROUPS.values() for a in g] + ["gta", "settings"]
DEFAULT = ["location", "size", "children", "hidden"]
GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL"}
TOPOLOGY_LIMIT = 200_000        # faces: beyond this the Python loop freezes the UI for long
WEIGHT_LIMIT = 150_000          # vertices
DESCENDANT_LIMIT = 5000         # most descendant objects that count toward a bounding box
LIMIT_MAX = 200
OUTPUT_LIMIT = 14000            # characters


def _s(x, n=3):
    t = f"{x:.{n}f}"
    if "." in t:                    # without decimals (n=0) the zeros are significant: 40 must not become "4"
        t = t.rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def _v(v, n=3):
    return "(" + ",".join(_s(x, n) for x in v) + ")"


def _resolve_fields(fields):
    if not fields:
        return list(DEFAULT)
    out = []
    for a in fields:
        a = str(a).strip().lower()
        if a == "all":
            out += ALL_FIELDS
        elif a in GROUPS:
            out += GROUPS[a]
        elif a in ALL_FIELDS:
            out.append(a)
        else:
            raise ValueError(f"unknown field: {a!r}. Valid: {', '.join(ALL_FIELDS)}; groups: {', '.join(GROUPS)}; all")
    return list(dict.fromkeys(out))


def _bounds(o, dg):
    """World bounding box of the object and its descendants: (min, max, approximate?) or None (no geometry)."""
    descendants = list(o.children_recursive)
    approx = len(descendants) > DESCENDANT_LIMIT
    pts = []
    for m in [o, *descendants[:DESCENDANT_LIMIT]]:
        if m.type not in GEOMETRY:
            continue
        src = m.evaluated_get(dg) if m.modifiers else m
        pts += [m.matrix_world @ Vector(c) for c in src.bound_box]
    if not pts:
        return None
    return (Vector(min(p[i] for p in pts) for i in range(3)), Vector(max(p[i] for p in pts) for i in range(3)), approx)


def _ground(z):
    if abs(z) < 0.001:
        return "ok"
    return f"floating +{_s(z)}" if z > 0 else f"below {_s(z)}"


def _hidden(o):
    parts = []
    try:
        if o.hide_get() or o.hide_viewport:
            parts.append("viewport")
    except RuntimeError:
        parts.append("not in layer")
    if o.hide_render:
        parts.append("render")
    if o.hide_select:
        parts.append("locked")
    return "+".join(parts) or "-"


def _tris(me):
    me.calc_loop_triangles()
    return len(me.loop_triangles)


def _details(o, dg):
    t = o.type
    if t == "MESH":
        me = o.data
        s = f"faces={len(me.polygons)} tris={_tris(me)} verts={len(me.vertices)}"
        if o.modifiers:
            ev = o.evaluated_get(dg).data
            s += f" (evaluated: faces={len(ev.polygons)} verts={len(ev.vertices)})"
        return s
    if t == "ARMATURE":
        bones = o.data.bones
        return f"bones={len(bones)} deform={sum(1 for b in bones if b.use_deform)}"
    if t == "LIGHT":
        return f"type={o.data.type} power={_s(o.data.energy, 1)}W"
    if t == "CAMERA":
        return f"lens={_s(o.data.lens, 1)}mm"
    if t == "EMPTY":
        return f"empty={o.empty_display_type}"
    return f"type={t}"


def _materials(o):
    if not hasattr(o, "material_slots"):
        return "-"
    names = [(s.material.name if s.material else "—") for s in o.material_slots]
    unique = list(dict.fromkeys(names))
    return "[" + ",".join(unique[:6]) + (f",+{len(unique) - 6}" if len(unique) > 6 else "") + "]" if unique else "-"


def _animation(o):
    ad = o.animation_data
    if ad is None or (ad.action is None and not ad.nla_tracks):
        return "none"
    s = []
    if ad.action:
        a = ad.action.frame_range
        s.append(f"{ad.action.name}({_s(a[0], 0)}-{_s(a[1], 0)})")
    if ad.nla_tracks:
        s.append(f"nla={len(ad.nla_tracks)}")
    return ",".join(s)


def _topology(o):
    me = o.data
    if len(me.polygons) > TOPOLOGY_LIMIT:
        return f"topology=skipped(>{TOPOLOGY_LIMIT} faces)"
    editing = o.mode == "EDIT"
    bm = bmesh.from_edit_mesh(me) if editing else bmesh.new()
    try:
        if not editing:
            bm.from_mesh(me)
        quads = tris = ngons = 0
        for f in bm.faces:
            k = len(f.verts)
            if k == 4:
                quads += 1
            elif k == 3:
                tris += 1
            else:
                ngons += 1
        boundary = non_manifold = 0
        for e in bm.edges:
            k = len(e.link_faces)
            if k == 1:
                boundary += 1
            elif k != 2:
                non_manifold += 1
        loose = sum(1 for v in bm.verts if not v.link_edges)
        p3 = sum(1 for v in bm.verts if len(v.link_edges) == 3)
        p5 = sum(1 for v in bm.verts if len(v.link_edges) >= 5)
    finally:
        if not editing:
            bm.free()
    return (f"quads={quads} tris={tris} ngons={ngons} boundary_edges={boundary} non_manifold={non_manifold} "
            f"loose_verts={loose} poles=3:{p3},5+:{p5}")


def _armature_of(o):
    for m in o.modifiers:
        if m.type == "ARMATURE" and m.object:
            return m.object
    return o.parent if o.parent and o.parent.type == "ARMATURE" else None


def _weights(o):
    if o.type == "MESH":
        arm = _armature_of(o)
        if arm is None:
            return None
        me = o.data
        if len(me.vertices) > WEIGHT_LIMIT:
            return f"weights=skipped(>{WEIGHT_LIMIT} verts)"
        deform = {b.name for b in arm.data.bones if b.use_deform}
        names = {g.index: g.name for g in o.vertex_groups}
        unweighted = 0
        for v in me.vertices:
            if not any(names.get(g.group) in deform and g.weight > 1e-6 for g in v.groups):
                unweighted += 1
        return f"unweighted_verts={unweighted}/{len(me.vertices)} armature={arm.name}"
    if o.type == "ARMATURE":
        groups = set()
        for m in bpy.context.scene.objects:
            if m.type == "MESH" and _armature_of(m) is o:
                groups |= {g.name for g in m.vertex_groups}
        missing = [b.name for b in o.data.bones if b.use_deform and b.name not in groups]
        return f"deform_bones_without_group={len(missing)}" + (f" [{','.join(missing[:8])}{',…' if len(missing) > 8 else ''}]" if missing else "")
    return None


def _missing_textures(o):
    missing = []
    for s in o.material_slots:
        m = s.material
        if not (m and m.node_tree):
            continue
        for n in m.node_tree.nodes:
            im = getattr(n, "image", None) if n.type == "TEX_IMAGE" else None
            if im is None or im.packed_file or im.source == "GENERATED":
                continue
            path = bpy.path.abspath(im.filepath) if im.filepath else ""
            if not path or not os.path.isfile(path):
                missing.append(im.name)
    return list(dict.fromkeys(missing))


def _gta(o):
    """Sollumz/GTA traps: every flag means a silently broken export or a file that is not written."""
    sollum = str(getattr(o, "sollum_type", "") or "")
    s = [f"sollum={sollum.replace('sollumz_', '') if sollum and sollum != 'sollumz_none' else 'none'}"]
    context = sollum not in ("", "sollumz_none")
    if re.search(r"\.\d{3}$", o.name):
        s.append(".001 suffix (name matching breaks)")
    if o.hide_select:
        s.append("hide_select (select_set does nothing)")
    if o.type == "MESH":
        me = o.data
        mats = [m.material for m in o.material_slots if m.material]
        sz = [m for m in mats if getattr(m, "sollum_type", "") == "sollumz_material_shader"]
        context = context or bool(sz)
        if context:
            if "UVMap 0" not in [u.name for u in me.uv_layers]:
                s.append(f"UVMap 0 missing (has: {','.join(u.name for u in me.uv_layers) or '-'})")
            if "Color 1" not in [c.name for c in me.color_attributes]:
                s.append("Color 1 missing (Colour0 is written black)")
            if sollum == "sollumz_drawable_model" and hasattr(o, "sz_lods") and o.sz_lods.high.mesh is None:
                s.append("sz_lods.high.mesh empty (the export writes no file)")
            other = [m.name for m in mats if m not in sz]
            if other:
                s.append(f"material without a Sollumz shader: {','.join(other[:4])}")
            if sz:
                s.append("shader=" + ",".join(dict.fromkeys(m.shader_properties.filename for m in sz))[:80])
            if (any(abs(x - 1) > 1e-4 for x in o.scale) or any(abs(r) > 1e-4 for r in o.rotation_euler)) and sollum == "sollumz_drawable_model":
                s.append("unapplied transform (scale/rotation leaks into the export)")
            if any(x < 0 for x in o.scale):
                s.append("negative scale (normals flipped)")
            if len(me.vertices) > 65535:
                s.append(f"verts={len(me.vertices)}>65535 (Sollumz splits)")
            if "bound" in sollum and (len(me.vertices) > 32767 or len(me.polygons) > 32767):
                s.append("BVH limit (32767 verts/primitives)")
            missing = _missing_textures(o)
            if missing:
                s.append(f"texture missing: {','.join(missing[:4])}")
    return "gta=[" + "; ".join(s) + "]"


def _field_text(o, field, dg, bounds):
    if field == "location":
        return "location=" + _v(o.matrix_world.translation)
    if field == "size":
        if bounds is None:
            return "size=(0,0,0)"
        return "size=" + ("~" if bounds[2] else "") + _v(bounds[1] - bounds[0])
    if field == "ground":
        return "ground=" + (_ground(bounds[0].z) if bounds else "-")
    if field == "rotation":
        return "rotation=" + _v([math.degrees(a) for a in o.matrix_world.to_euler()], 1)
    if field == "scale":
        return "scale=" + _v(o.matrix_world.to_scale())
    if field == "parent":
        return "parent=" + (o.parent.name if o.parent else "-")
    if field == "children":
        n, t = len(o.children), len(o.children_recursive)
        return f"children={n}" + (f"(total {t})" if t > n else "")
    if field == "hidden":
        return "hidden=" + _hidden(o)
    if field == "details":
        return _details(o, dg)
    if field == "materials":
        return "materials=" + _materials(o)
    if field == "modifiers":
        return "modifiers=" + (",".join(m.type for m in o.modifiers) if o.modifiers else "-")
    if field == "animation":
        return "animation=" + _animation(o)
    if field == "topology":
        return _topology(o) if o.type == "MESH" else None
    if field == "weights":
        return _weights(o)
    if field == "gta":
        return _gta(o)
    return None


def _header(scene, vl):
    objects = list(scene.objects)
    kinds = collections.Counter(o.type for o in objects)
    selected = [o for o in vl.objects if o.select_get()]
    active = vl.objects.active
    faces = 0
    meshes = {o.data.name: o.data for o in objects if o.type == "MESH"}
    for me in meshes.values():
        faces += len(me.polygons)
    return (f"scene '{scene.name}': {len(objects)} objects [{', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))}] · "
            f"collections {len(bpy.data.collections)} · faces {faces} · materials {len(bpy.data.materials)} · images {len(bpy.data.images)} · "
            f"active {active.name if active else '-'} · selected {len(selected)} · mode {active.mode if active else 'OBJECT'}")


def _settings(scene):
    r = scene.render
    hdri = "none"
    w = scene.world
    if w and w.use_nodes:
        for n in w.node_tree.nodes:
            if n.type == "TEX_ENVIRONMENT" and n.image:
                hdri = n.image.name
                break
    file = bpy.data.filepath or "unsaved"
    return (f"settings: file={file} · engine={r.engine} · frames={scene.frame_start}-{scene.frame_end}@{_s(r.fps / r.fps_base, 2)} · "
            f"resolution={r.resolution_x}x{r.resolution_y}@{r.resolution_percentage}% · camera={scene.camera.name if scene.camera else 'none'} · "
            f"hdri={hdri} · units={scene.unit_settings.system}×{_s(scene.unit_settings.scale_length, 4)}")


def _hierarchy(root):
    out = []

    def walk(o, d):
        out.append((o, d))
        for c in sorted(o.children, key=lambda x: x.name):
            walk(c, d + 1)
    walk(root, 0)
    return out


def report(fields=None, query="", root="", limit=20):
    """Scene report (read-only). fields: location, size, ground, rotation, scale, parent, children, hidden, details, materials, modifiers, animation,
    topology, weights, gta, settings or the groups placement/contents/health, all (default: location, size, children, hidden). The default listing is the
    top-level objects; root='Name' lists that object's hierarchy, query='text' every object with the text in its name."""
    fields = _resolve_fields(fields)
    limit = max(1, min(int(limit or 20), LIMIT_MAX))
    scene, vl = bpy.context.scene, bpy.context.view_layer
    vl.update()
    dg = bpy.context.evaluated_depsgraph_get()
    lines = [_header(scene, vl)]
    if "settings" in fields:
        lines.append(_settings(scene))
    fields = [a for a in fields if a != "settings"]
    if root:
        k = bpy.data.objects.get(root)
        if k is None:
            raise KeyError(f"no such object: {root!r}. First names in the scene: " + ", ".join(list(bpy.data.objects.keys())[:12]))
        listing, label = _hierarchy(k), f"'{root}' hierarchy"
    elif query:
        listing = [(o, 0) for o in sorted(scene.objects, key=lambda x: x.name) if query.lower() in o.name.lower()]
        label = f"with '{query}' in the name"
    else:
        listing = [(o, 0) for o in sorted(scene.objects, key=lambda x: x.name) if o.parent is None]
        label = "top-level"
    total, looked, written = len(listing), 0, 0
    if fields:
        for o, d in listing:
            if written >= limit:
                break
            looked += 1
            bounds = _bounds(o, dg) if ("size" in fields or "ground" in fields) else None
            parts = [p for p in (_field_text(o, a, dg, bounds) for a in fields) if p]
            if not parts:                      # a health field does not apply to this object type (e.g. topology on a camera): no line
                continue
            lines.append("  " * d + f"{o.name} [{o.type}] " + " ".join(parts))
            written += 1
    else:
        lines += ["  " * d + f"{o.name} [{o.type}]" for o, d in listing[:limit]]
        looked = min(limit, total)
    if total > looked:
        lines.append(f"… {total - looked} more objects ({label}, limit={limit}); narrow or widen with root='name', query='text' or limit")
    elif total == 0:
        lines.append(f"(no objects: {label})")
    text = "\n".join(lines)
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + f"\n… output truncated at {OUTPUT_LIMIT} characters: lower the limit or reduce the fields"
