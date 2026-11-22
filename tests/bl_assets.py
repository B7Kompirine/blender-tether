"""Asset import: a real glTF (Blender's own export) is downloaded from the fake server and imported with tether.mcp_server.asset_import; the texture
material and HDRI nodes are measured."""
import os
import time

from fakes import FakeAssets, patch_assets, png
from harness import TMP, check, client

GLTF = []          # the real glTF exported here (the windowed test reuses it)


def attempt(f, *a, **k):
    try:
        f(*a, **k)
    except Exception as e:  # noqa: BLE001
        return e
    return None


def run(sel, c):
    from tether import mcp_server
    print("      [daemon] asset import")
    s = FakeAssets()
    assets, undo = patch_assets(s)
    try:
        out = TMP / "gltf_out"
        out.mkdir()
        (TMP / "box_texture.png").write_bytes(png((10, 200, 10), (16, 16)))
        y = c("bpy.ops.wm.read_homefile(use_factory_startup=True)\nm = bpy.data.materials.new('box_mat')\nm.use_nodes = True\n"
              f"img = bpy.data.images.load({(TMP / 'box_texture.png').as_posix()!r})\ntn = m.node_tree.nodes.new('ShaderNodeTexImage')\ntn.image = img\n"
              "m.node_tree.links.new(tn.outputs['Color'], m.node_tree.nodes['Principled BSDF'].inputs['Base Color'])\n"
              "o = bpy.data.objects['Cube']\no.data.materials.clear()\no.data.materials.append(m)\n"
              f"bpy.ops.export_scene.gltf(filepath={(out / 'model_1k.gltf').as_posix()!r}, export_format='GLTF_SEPARATE', export_texture_dir='textures')\n'done'")
        files = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        check("[assets+blender] a real glTF was exported (gltf + bin + texture)",
              y.get("result") == "done" and "model_1k.gltf" in files and any(k.endswith(".bin") for k in files) and any(k.startswith("textures/") for k in files), (y.get("error"), sorted(files)))
        main = files.pop("model_1k.gltf")
        s.ph_model("Box_01", "Box 01", main=main, extras=files, tags=("box",))
        GLTF[:] = [str(out / "model_1k.gltf")]
        c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
        measure = lambda name: c("o = bpy.data.objects[" + repr(name) + "]\nroot = [x for x in [o] + list(o.children_recursive)]\nbpy.context.view_layer.update()\n"  # noqa: E731
                                 "import mathutils as _mu\npts = [x.matrix_world @ _mu.Vector(k) for x in root if x.type == 'MESH' for k in x.bound_box]\n"
                                 "[[round(min(p[i] for p in pts), 3) for i in range(3)], [round(max(p[i] for p in pts), 3) for i in range(3)], "
                                 "sum(len(x.data.polygons) for x in root if x.type == 'MESH'), sorted({m.name for x in root if x.type == 'MESH' for m in x.data.materials if m}), "
                                 "[o.get('asset_source'), o.get('asset_id'), o.get('asset_license'), o.get('asset_attribution')]]")["result"]
        # model: download + import in one call
        t0 = time.time()
        m = mcp_server.asset_import(sel, "polyhaven", "Box_01", target_size=1.0, location=[5, 0, 0])
        print(f"      [daemon] asset_import (download + glTF import): {time.time() - t0:.2f} s")
        low, high, faces, materials, props = measure("Box_01")
        check("[assets+blender] model: under the root 'Box_01'; the 2x2x2 cube becomes 1x1x1 with target_size=1, the centre of the base is at location (5,0,0)",
              low == [4.5, -0.5, 0.0] and high == [5.5, 0.5, 1.0], (low, high, m))
        check("[assets+blender] model: 12 tris (glTF triangles), the material name, the source/license/attribution properties on the root",
              faces in (6, 12) and materials == ["box_mat"] and props[0] == "polyhaven" and props[1] == "Box_01" and props[2] == "CC0" and "Kirill Sannikov" in props[3], (faces, materials, props))
        check("[assets+blender] the asset_import report: source, license, root, tris, size, file path",
              "imported: polyhaven Box_01 (CC0)" in m and "12 tris" in m and "size (1,1,1) m" in m and "Box_01_1k.gltf" in m and "downloaded" in m and "scale ×0.5" in m, m)
        image = c("[(n.image.name, n.image.size[:]) for mm in bpy.data.materials if mm.name == 'box_mat' for n in mm.node_tree.nodes if n.type == 'TEX_IMAGE']")["result"]
        check("[assets+blender] the glTF texture (a png downloaded from a subfolder) is connected to the material and loaded at 16x16", image and image[0][1] == [16, 16], image)
        m2 = mcp_server.asset_import(sel, "polyhaven", "Box_01", snap_to_ground=False)
        check("[assets+blender] the second time: the download comes from the cache (the report says 'from cache'), snap_to_ground=False leaves the model where it is, the new root gets '.001'",
              "from cache" in m2 and "Box_01.001" in m2, m2)
        check("[assets+blender] snap_to_ground=False: scale ×1 (no target_size given) and the location does not change (0,0,0)", "scale ×1)" in m2 and "root location (0,0,0)" in m2, m2)
        try:
            mcp_server.asset_import(sel, "polyhaven", "NoSuch_01")
            ok = False
        except RuntimeError as e:
            ok = "HTTP 404" in str(e)
        check("[assets+blender] an asset that does not exist: the MCP tool gives a clear error (the scene is unchanged)", ok)
        y = client.request(sel, "call", name="import_asset", args={"kind": "model", "name": "x", "file": str(TMP / "missing.gltf")})
        check("[assets+blender] if the model file is missing the import gives a clear error", not y["ok"] and "model file not found" in y["error"]["message"], y)
        (TMP / "x.stl").write_bytes(b"x")
        y = client.request(sel, "call", name="import_asset", args={"kind": "model", "name": "x", "file": str(TMP / "x.stl")})
        check("[assets+blender] an unsupported model format gives a clear error", not y["ok"] and "unsupported model format" in y["error"]["message"], y)
        y = client.request(sel, "call", name="import_asset", args={"kind": "plane", "name": "x"})
        check("[assets+blender] an unknown asset kind gives a clear error", not y["ok"] and "unknown asset kind" in y["error"]["message"], y)
        # PBR texture: material nodes (colour spaces, normal map, AO multiply) and applying to objects
        c("bpy.ops.wm.read_homefile(use_factory_startup=True)\nme = bpy.data.meshes.new('uvless')\nme.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])\n"
          "bpy.context.collection.objects.link(bpy.data.objects.new('uvless', me))\nbpy.ops.object.empty_add()\nbpy.context.active_object.name = 'empty'")
        s.ph_texture("brick_4", "Brick 4", tags=("brick",))
        m = mcp_server.asset_import(sel, "polyhaven", "brick_4", apply_to=["Cube", "uvless", "empty"])
        d = c("mm = bpy.data.materials['polyhaven_brick_4']\nnt = mm.node_tree\n"
              "nodes = {n.label: (n.image.colorspace_settings.name, n.image.size[:]) for n in nt.nodes if n.type == 'TEX_IMAGE'}\n"
              "bsdf = [n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'][0]\n"
              "src = lambda socket: [l.from_node for l in socket.links]\n"
              "[nodes, [n.type for n in src(bsdf.inputs['Base Color'])], [n.blend_type for n in src(bsdf.inputs['Base Color'])], "
              "[n.type for n in src(bsdf.inputs['Normal'])], [n.label for n in src(bsdf.inputs['Roughness'])], mm.use_fake_user, mm.get('asset_license'), "
              "[s.material.name for s in bpy.data.objects['Cube'].material_slots], len(bpy.data.objects['Cube'].data.materials), "
              "[x.name for x in bpy.data.materials if x.name.startswith('polyhaven_brick_4')]]")["result"]
        nodes, base, blend, normal, rough, fake_user, license_, cube_mat, cube_count, materials = d
        check("[assets+blender] texture: color is sRGB; normal/roughness/ao are Non-Color; all loaded at 16x16",
              nodes.get("color", [""])[0] == "sRGB" and all(nodes[k][0] == "Non-Color" for k in ("normal", "roughness", "ao")) and all(v[1] == [16, 16] for v in nodes.values()), nodes)
        check("[assets+blender] texture: Base Color = color x ao (Mix, MULTIPLY); Normal = Normal Map; Roughness = the roughness map",
              base == ["MIX"] and blend == ["MULTIPLY"] and normal == ["NORMAL_MAP"] and rough == ["roughness"], (base, blend, normal, rough))
        check("[assets+blender] texture: the material 'polyhaven_brick_4', with a fake user (kept even outside the scene), the license property, applied to Cube as its only material",
              cube_mat == ["polyhaven_brick_4"] and cube_count == 1 and fake_user and license_ == "CC0" and materials == ["polyhaven_brick_4"], d)
        check("[assets+blender] texture report: the applied objects, the empty skipped, the mesh without UVs warned about",
              "applied: Cube, uvless" in m and "skipped: empty (EMPTY)" in m and "uvless: no UVs" in m, m)
        m = mcp_server.asset_import(sel, "polyhaven", "brick_4")
        check("[assets+blender] the same texture a second time: the same material is reused, no copy is made",
              "already existed, reused" in m and c("len([x for x in bpy.data.materials if x.name.startswith('polyhaven_brick_4')])")["result"] == 1, m)
        s.ph_texture("large_floor_tiles", "Large Floor Tiles", size=(2000, 2000))
        try:
            mcp_server.asset_import(sel, "polyhaven", "large_floor_tiles", apply_to=["NoSuch"])
            ok = False
        except RuntimeError as e:
            ok = "apply_to: no such object" in str(e)
        check("[assets+blender] a missing object in apply_to: a clear error, no material is created at all (checked first)",
              ok and c("'polyhaven_large_floor_tiles' in bpy.data.materials")["result"] is False)
        c("bpy.data.materials.new('polyhaven_large_floor_tiles')")
        m = mcp_server.asset_import(sel, "polyhaven", "large_floor_tiles")
        check("[assets+blender] if another material has the same name it is not overwritten: the new one gets a unique name",
              c("sorted(x.name for x in bpy.data.materials if x.name.startswith('polyhaven_large'))")["result"] == ["polyhaven_large_floor_tiles", "polyhaven_large_floor_tiles.001"], m)
        # HDRI: world nodes
        s.ph_hdri("belfast_sunset", "Belfast Sunset", time_of_day="sunset")
        m = mcp_server.asset_import(sel, "polyhaven", "belfast_sunset")
        d = c("w = bpy.context.scene.world\nnt = w.node_tree\nenv = [n for n in nt.nodes if n.type == 'TEX_ENVIRONMENT'][0]\n"
              "[w.name, env.image.size[:], [l.to_node.type for l in env.outputs['Color'].links], sorted(n.type for n in nt.nodes), w.get('asset_id')]")
        d = d.get("result") or [d.get("error")]
        check("[assets+blender] hdri: the new world 'polyhaven_belfast_sunset'; Environment Texture -> Background -> World Output; the image loaded at 4x4",
              len(d) == 5 and d[0] == "polyhaven_belfast_sunset" and d[1] == [4, 4] and d[2] == ["BACKGROUND"] and d[3] == ["BACKGROUND", "OUTPUT_WORLD", "TEX_ENVIRONMENT"] and d[4] == "belfast_sunset", d)
        old = c("w = bpy.data.worlds['World']\n[w.use_fake_user, len(w.node_tree.nodes) if w.node_tree else 0]")
        check("[assets+blender] hdri: the previous world is not deleted, its nodes are intact, it is kept with a fake user (it does not vanish on save)",
              (old.get("result") or [None])[0] is True and old["result"][1] > 0, old)
        check("[assets+blender] hdri report: the world name; the previous world was not deleted",
              "world lighting: 'polyhaven_belfast_sunset'" in m and "the previous world 'World' was not deleted" in m, m)
        m = mcp_server.asset_import(sel, "polyhaven", "belfast_sunset")
        check("[assets+blender] the same hdri a second time: the same world is reused, no copy is made", c("sorted(w.name for w in bpy.data.worlds)")["result"] == ["World", "polyhaven_belfast_sunset"], m)
        # warning: sources with an arbitrary scale
        s.sk_model("e" * 32, "Chair Scale", zip_files={"scene.gltf": main, **files})
        s.sk_model("b" * 32, "Chair By", slug="by", label="CC Attribution")
        os.environ["SKETCHFAB_TOKEN"] = s.sk_token
        c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
        m = mcp_server.asset_import(sel, "sketchfab", "e" * 32)
        check("[assets+blender] a sketchfab model (zip): downloads, unpacks, imports; the arbitrary-scale warning shows when no target_size is given",
              "imported: sketchfab" in m and "the scale is arbitrary" in m and "scene.gltf" in m, m)
        m = mcp_server.asset_import(sel, "sketchfab", "e" * 32, target_size=0.5)
        check("[assets+blender] with target_size the arbitrary-scale warning does not appear, the size is 0.5", "the scale is arbitrary" not in m and "size (0.5,0.5,0.5) m" in m, m)
        error = attempt(mcp_server.asset_import, sel, "sketchfab", "b" * 32)
        check("[assets+blender] a CC-BY asset is rejected without acceptance in the MCP tool too, no object is added to the scene", error is not None and "not CC0" in str(error))
        # input validation: bad numeric input is rejected before the download and before the import (no raw Python error, no half import)
        c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
        s.ph_model("Box_02", "Box 02", main=main, extras=files, tags=("box",))
        snapshot = lambda: c("import json\njson.dumps({k: sorted(x.name for x in getattr(bpy.data, k)) for k in ('objects', 'meshes', 'materials', 'images', 'worlds', 'actions', 'node_groups')}, sort_keys=True)")["result"]  # noqa: E731
        selection = lambda: c("v = bpy.context.view_layer.objects\n[sorted(o.name for o in v if o.select_get()), v.active and v.active.name, bpy.context.scene.world and bpy.context.scene.world.name]")["result"]  # noqa: E731
        before, selected = snapshot(), selection()
        for label, kw, part in (("a 2-element location", {"location": [1, 2]}, "location"), ("a location that is not numbers", {"location": ["a", "b", "c"]}, "location"),
                                ("a negative target_size", {"target_size": -1}, "target_size"), ("a NaN target_size", {"target_size": float("nan")}, "target_size")):
            error = attempt(mcp_server.asset_import, sel, "polyhaven", "Box_02", **kw)
            check(f"[assets+blender] {label}: a clear error ({part}), not a raw Python error",
                  error is not None and part in str(error) and not any(x in str(error) for x in ("AttributeError", "TypeError", "ValueError")), error)
        check("[assets+blender] bad input is rejected before the download (no files request goes to the server) and the scene stays the same",
              s.count("/dl/ph/Box_02") == 0 and s.count("/ph/files/Box_02") == 0 and snapshot() == before, (s.count("/dl/ph/Box_02"), s.count("/ph/files/Box_02")))
        y = client.request(sel, "call", name="import_asset", args={"kind": "model", "name": "x", "file": GLTF[0], "location": [1, 2]})
        check("[assets+blender] the Blender side validates location before importing too: a clear error, the scene stays the same",
              not y["ok"] and "location" in y["error"]["message"] and snapshot() == before, y)
        # rollback: an unexpected error after the import (here: while writing the root's properties) restores the new data blocks and the selection
        s.ph_texture("brick_5", "Brick 5", tags=("brick",))
        s.ph_hdri("rollback_hdri", "Rollback Hdri", time_of_day="noon")
        c("bpy.ops.object.select_all(action='DESELECT')\nbpy.context.view_layer.objects.active = bpy.data.objects['Cube']\nbpy.data.objects['Cube'].select_set(True)")
        before, selected = snapshot(), selection()
        c("from tether.agent import blender_assets as ba\n_orig_props = ba._props\n\ndef _boom(*a):\n    raise RuntimeError('fake: error after the import')\n\nba._props = _boom")
        try:
            for kind, id_, kw in (("model", "Box_02", {"target_size": 1.0}), ("texture", "brick_5", {}), ("hdri", "rollback_hdri", {})):
                error = attempt(mcp_server.asset_import, sel, "polyhaven", id_, **kw)
                check(f"[assets+blender] {kind}: an error after the import leaves the scene intact (no new object, mesh, material, image or world is left)",
                      error is not None and "fake: error after the import" in str(error) and snapshot() == before, (error, snapshot()))
        finally:
            c("ba._props = _orig_props")
        check("[assets+blender] after the failed imports the selection, the active object and the scene world are back", selection() == selected, (selected, selection()))
    finally:
        undo()
        c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
