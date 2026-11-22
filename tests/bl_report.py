"""Scene report checked field by field against known geometry (a cube has 6 quads, a plane 4 boundary edges, a cylinder 2 ngons...)."""
import time

import harness
from harness import check, client

BASE = (
    "import bmesh as _bm\nimport math\n"
    "bpy.ops.wm.read_homefile(use_factory_startup=True)\nA = bpy.context.view_layer.objects\n"
    "def named(name):\n    o = A.active\n    o.name = name\n    return o\n"
    "bpy.ops.mesh.primitive_cube_add(location=(3, 0, 5)); named('floating_box')\n"
    "bpy.ops.mesh.primitive_cylinder_add(vertices=5, location=(6, 0, 1)); named('cylinder')\n"
    "bpy.ops.mesh.primitive_plane_add(location=(9, 0, 0)); named('plane')\n"
    "bpy.ops.mesh.primitive_cone_add(vertices=6, end_fill_type='TRIFAN', location=(12, 0, 1)); named('cone')\n"
    "bpy.ops.object.armature_add(location=(15, 0, 0)); arm = named('armature')\n"
    "bpy.ops.object.mode_set(mode='EDIT'); eb = arm.data.edit_bones; eb[0].name = 'b1'; b2 = eb.new('b2'); b2.head = (0, 0, 1); b2.tail = (0, 0, 2)\n"
    "bpy.ops.object.mode_set(mode='OBJECT')\n"
    "bpy.ops.mesh.primitive_cube_add(location=(15, 0, 1)); g = named('body')\n"
    "g.vertex_groups.new(name='b1').add([0, 1, 2, 3], 1.0, 'REPLACE')\nmd = g.modifiers.new('Armature', 'ARMATURE'); md.object = arm; g.parent = arm\n"
    "bpy.ops.mesh.primitive_cube_add(location=(0, -4, 0)); o = named('rotated'); o.rotation_euler = (0, 0, math.radians(90)); o.scale = (2, 1, 1)\n"
    "bpy.ops.mesh.primitive_cube_add(location=(0, 8, 0)); o = named('animated'); o.keyframe_insert('location', frame=1); o.location = (5, 8, 0); o.keyframe_insert('location', frame=24)\n"
    "bpy.ops.mesh.primitive_cube_add(location=(0, 12, 0)); o = named('smooth'); o.modifiers.new('Subsurf', 'SUBSURF').levels = 1\n"
    "e1 = bpy.data.objects.new('hidden_object', None); C.collection.objects.link(e1); e1.hide_set(True); e1.hide_render = True\n"
    "e2 = bpy.data.objects.new('locked', None); C.collection.objects.link(e2); e2.hide_select = True\n"
    "e3 = bpy.data.objects.new('door.001', None); C.collection.objects.link(e3)\n"
    "b = _bm.new(); v = [b.verts.new(p) for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1))]\n"
    "b.faces.new((v[0], v[1], v[2])); b.faces.new((v[0], v[1], v[3])); b.faces.new((v[0], v[1], v[4]))\n"
    "me = bpy.data.meshes.new('t_me'); b.to_mesh(me); b.free(); C.collection.objects.link(bpy.data.objects.new('t_edge', me))\n"
    "b = _bm.new(); v = [b.verts.new(p) for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (5, 5, 5))]; b.faces.new(v[:3])\n"
    "me = bpy.data.meshes.new('g_me'); b.to_mesh(me); b.free(); C.collection.objects.link(bpy.data.objects.new('loose', me))\n")
SOLLUMZ_BOX = ("bpy.ops.mesh.primitive_cube_add(location=(0, 4, 0)); k = named('box_gta')\n"
               "k.data.materials.clear(); k.data.materials.append(create_shader('default.sps'))\ntether.select('box_gta')\nbpy.ops.sollumz.converttodrawable()\n")


def run(sel, c):
    print("      [daemon] scene report")
    sollumz = harness.STATE["sollumz"]
    setup = ("from Sollumz.ydr.shader_materials import create_shader\n" if sollumz else "") + BASE + (SOLLUMZ_BOX if sollumz else "") + "len(bpy.data.objects)"
    y = c(setup)
    expected = 19 if sollumz else 17
    check(f"[report] the test scene was built (factory 3 + objects{' + the empty of the drawable conversion' if sollumz else ''})", y.get("ok") and y["result"] == expected, y.get("error") or y)
    line = lambda m, name: next((x.strip() for x in m.splitlines() if x.strip().startswith(name + " [")), "")  # noqa: E731
    m = client.report(sel, limit=60)
    n = c("len(bpy.data.objects)")["result"]
    head = m.splitlines()[0]
    check("[report] header: object count, type counts, active, mode", f"{n} objects" in head and "MESH " in head and "ARMATURE 1" in head and "mode OBJECT" in head, head)
    check("[report] default fields: the cube's location (0,0,0), size (2,2,2), children, hidden",
          all(x in line(m, "Cube") for x in ("location=(0,0,0)", "size=(2,2,2)", "children=0", "hidden=-")), line(m, "Cube"))
    if sollumz:
        check("[report] size: with children (the drawable root is an empty, its size comes from the model)", "size=(2,2,2)" in line(m, "box_gta") and "children=1" in line(m, "box_gta"), line(m, "box_gta"))
    m2 = client.report(sel, fields=["placement", "contents"], limit=60)
    check("[report] ground: floating +4, ok, below -1",
          "ground=floating +4" in line(m2, "floating_box") and "ground=ok" in line(m2, "plane") and "ground=below -1" in line(m2, "Cube"),
          (line(m2, "floating_box"), line(m2, "plane"), line(m2, "Cube")))
    check("[report] rotation (degrees) and scale (world)", "rotation=(0,0,90)" in line(m2, "rotated") and "scale=(2,1,1)" in line(m2, "rotated"), line(m2, "rotated"))
    check("[report] hidden: viewport+render, locked", "hidden=viewport+render" in line(m2, "hidden_object") and "hidden=locked" in line(m2, "locked"),
          (line(m2, "hidden_object"), line(m2, "locked")))
    check("[report] details: faces/tris/verts, bones, light power",
          "faces=6 tris=12 verts=8" in line(m2, "Cube") and "bones=2 deform=2" in line(m2, "armature") and "power=1000W" in line(m2, "Light"),
          (line(m2, "Cube"), line(m2, "armature"), line(m2, "Light")))
    check("[report] modifiers and evaluated faces (subsurf: 6 -> 24)", "modifiers=SUBSURF" in line(m2, "smooth") and "evaluated: faces=24" in line(m2, "smooth"), line(m2, "smooth"))
    check("[report] materials and animation range", "materials=[Material]" in line(m2, "Cube") and "(1-24)" in line(m2, "animated"), (line(m2, "Cube"), line(m2, "animated")))
    ml = client.report(sel, root="armature", fields=["parent", "children"])
    check("[report] root: an indented hierarchy, parent and children", "armature [ARMATURE] parent=- children=1" in ml and "\n  body [MESH] parent=armature children=0" in ml, ml)
    mq = client.report(sel, query="BOX", fields=["location"])
    check("[report] query: the objects with it in their name (case-insensitive), the others are not there",
          "floating_box" in mq and ("box_gta" in mq if sollumz else True) and "Cube [" not in mq, mq)
    ms = client.report(sel, fields=["location"], limit=3)
    check("[report] limit: 3 lines and a 'more' note", len([x for x in ms.splitlines() if " [" in x and "location=" in x]) == 3 and "more objects" in ms, ms)
    mt = client.report(sel, fields=["topology"], limit=60)
    check("[report] topology: a cube has 6 quads, poles 3:8", "quads=6 tris=0 ngons=0 boundary_edges=0 non_manifold=0 loose_verts=0 poles=3:8,5+:0" in line(mt, "Cube"), line(mt, "Cube"))
    check("[report] topology: the plane's 4 boundary edges", "quads=1" in line(mt, "plane") and "boundary_edges=4" in line(mt, "plane"), line(mt, "plane"))
    check("[report] topology: the cylinder has 5 quads + 2 ngons; the cone 12 tris and 2 poles 5+",
          "quads=5 tris=0 ngons=2" in line(mt, "cylinder") and "tris=12" in line(mt, "cone") and "poles=3:0,5+:2" in line(mt, "cone"), (line(mt, "cylinder"), line(mt, "cone")))
    check("[report] topology: an edge shared by 3 faces is non-manifold (1), 6 boundary edges; a loose vertex (1)",
          "non_manifold=1" in line(mt, "t_edge") and "boundary_edges=6" in line(mt, "t_edge") and "loose_verts=1" in line(mt, "loose"), (line(mt, "t_edge"), line(mt, "loose")))
    check("[report] topology opens no line for a camera/light/empty (no noise)", "Camera [" not in mt and "Light [" not in mt and "locked [" not in mt, mt)
    ma = client.report(sel, fields=["weights"], limit=60)
    mag = client.report(sel, fields=["weights"], query="body")
    check("[report] weights: 4 of 8 vertices are not moved by a deform bone; the deform bone b2 has no vertex group",
          "unweighted_verts=4/8 armature=armature" in line(mag, "body") and "deform_bones_without_group=1 [b2]" in line(ma, "armature"), (line(mag, "body"), line(ma, "armature")))
    mg = client.report(sel, fields=["gta"], limit=60)
    check("[report] gta: a plain Blender object does not count in a GTA context (no UVMap/Color flag; a Sollumz material is told apart by sollum_type)",
          "sollum=none" in line(mg, "Cube") and "UVMap" not in line(mg, "Cube") and "Color 1" not in line(mg, "Cube"), line(mg, "Cube"))
    check("[report] gta: the .001 suffix and the locked object flag", ".001 suffix" in line(mg, "door.001") and "hide_select" in line(mg, "locked"), (line(mg, "door.001"), line(mg, "locked")))
    if sollumz:
        gm = line(client.report(sel, fields=["gta"], query="box_gta.model"), "box_gta.model")
        check("[report] gta: UVMap 0 and Color 1 missing, the Sollumz shader name", "UVMap 0 missing (has: UVMap)" in gm and "Color 1 missing" in gm and "shader=default.sps" in gm and "sollum=drawable_model" in gm, gm)
        c("o = bpy.data.objects['box_gta.model']\no.data.uv_layers[0].name = 'UVMap 0'\no.data.color_attributes.new('Color 1', 'BYTE_COLOR', 'CORNER')\n"
          "img = bpy.data.images.new('absent', 4, 4)\nimg.source = 'FILE'\nimg.filepath = '//missing/no_such_texture.png'\nm = bpy.data.materials.new('textured')\nm.use_nodes = True\n"
          "m.node_tree.nodes.new('ShaderNodeTexImage').image = img\no.data.materials.append(m)\no.scale = (2, 2, 2)\no.sz_lods.high.mesh = None")
        gm = line(client.report(sel, fields=["gta"], query="box_gta.model"), "box_gta.model")
        check("[report] gta: once fixed the UVMap/Color flags go; a missing texture, a material without a Sollumz shader, an unapplied transform, an empty sz_lods",
              "UVMap 0 missing" not in gm and "Color 1 missing" not in gm and "texture missing: absent" in gm and "material without a Sollumz shader: textured" in gm
              and "unapplied transform" in gm and "sz_lods.high.mesh empty" in gm, gm)
    mset = client.report(sel, fields=["settings"], limit=1)
    check("[report] settings line: file, engine, frames, resolution, camera, units",
          "file=unsaved" in mset and "resolution=1920x1080@100%" in mset and "camera=Camera" in mset and "frames=1-250@24" in mset and "units=METRIC×1" in mset, mset)
    state = lambda: c("sorted((o.name, tuple(round(x, 4) for x in o.matrix_world.translation), o.select_get(), o.hide_get()) for o in bpy.data.objects)")["result"]  # noqa: E731
    before = state()
    client.report(sel, fields=["all"], limit=200)
    check("[report] read-only: the scene (location, selection, visibility) does not change even when every field is requested", before == state())
    for kwargs, expected_text in ((dict(fields=["nonsense"]), "unknown field"), (dict(root="no_such"), "no such object")):
        try:
            client.report(sel, **kwargs)
            ok = False
        except client.TetherError as e:
            ok = expected_text in str(e)
        check(f"[report] error: {expected_text}", ok)
    c("for i in range(2000):\n    o = bpy.data.objects.new(f'empty{i}', None)\n    C.collection.objects.link(o)\nlen(bpy.data.objects)")
    t = time.time()
    m = client.report(sel)
    elapsed = time.time() - t
    print(f"      [daemon] report: the default call in a scene with 2000+ objects takes {elapsed:.2f} s, {len(m)} characters")
    check("[report] the default call with 2000+ objects takes < 2 s and < 3000 characters (the token cap)", elapsed < 2 and len(m) < 3000, (elapsed, len(m)))
    # animation range: frame numbers that end in zero must match Blender's action.frame_range
    ranges = ((3, 40), (0, 100), (1, 250), (0, 30), (10, 20), (1, 120), (5, 12))
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\nfor i, (a, b) in enumerate(" + repr(ranges) + "):\n"
      "    bpy.ops.mesh.primitive_cube_add(location=(i * 3, 0, 0)); o = bpy.context.active_object; o.name = f'an{i}'\n"
      "    o.keyframe_insert('location', frame=a); o.location.x += 1; o.keyframe_insert('location', frame=b)")
    ma = client.report(sel, fields=["animation"], query="an", limit=20)
    real = c("[tuple(bpy.data.objects[f'an{i}'].animation_data.action.frame_range) for i in range(" + str(len(ranges)) + ")]")["result"]
    written = [next((x.strip() for x in ma.splitlines() if x.strip().startswith(f"an{i} [")), "") for i in range(len(ranges))]
    check("[report] animation range: frames ending in zero (40, 100, 250, 30, 20, 120) are written the same as Blender's frame_range",
          all(f"({int(g[0])}-{int(g[1])})" in y for g, y in zip(real, written)) and [tuple(map(int, g)) for g in real] == [tuple(a) for a in ranges], (real, written))
    fmt = c("from tether.agent import blender_report as br\n[br._s(40, 0), br._s(100, 0), br._s(0, 0), br._s(-10, 0), br._s(250, 0), br._s(2.5, 3), br._s(10.0, 3), br._s(0.1, 3), br._s(-0.0004, 3), br._s(1200.5, 1)]")["result"]
    check("[report] number format: zeros are kept without decimals (40, 100, 250), with decimals the trailing zero and the dot are dropped",
          fmt == ["40", "100", "0", "-10", "250", "2.5", "10", "0.1", "0", "1200.5"], fmt)
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
