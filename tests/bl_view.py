"""Image engine: view directions measured in pixels, shading, how full the frame is, camera match, the frame strip, an image, restoring settings."""
import io
import time

from harness import TMP, check, client


def run(sel, c):
    import numpy as np
    from PIL import Image as PILImage, ImageFilter, ImageStat
    print("      [daemon] image engine")
    state = lambda: c("r = bpy.context.scene.render\nv = [a for w in bpy.context.window_manager.windows for a in w.screen.areas if a.type == 'VIEW_3D'][0].spaces.active\n"  # noqa: E731
                      "[r.engine, r.resolution_x, r.resolution_y, r.filepath, bpy.context.scene.camera and bpy.context.scene.camera.name, len(bpy.data.objects), "
                      "len(bpy.data.cameras), len(bpy.data.images), bpy.context.scene.frame_current, v.shading.type, v.shading.show_xray, v.shading.color_type, "
                      "v.shading.light, v.overlay.show_overlays, v.overlay.show_floor, v.overlay.show_axis_x, v.overlay.show_extras, v.overlay.show_outline_selected, "
                      "v.overlay.show_wireframes, v.overlay.show_bones]")["result"]

    def grab(width=300, **kw):
        png, caption = client.view(sel, width, caption=True, **kw)
        return np.asarray(PILImage.open(io.BytesIO(png)).convert("RGB")), caption

    def red(a):
        return (a[..., 0] > 140) & (a[..., 1] < 100) & (a[..., 2] < 100)

    def from_center(a):
        ys, xs = np.nonzero(red(a))
        return (xs.mean() - (a.shape[1] - 1) / 2, ys.mean() - (a.shape[0] - 1) / 2) if len(xs) else None

    def fill(a):
        diff = np.abs(a.astype(int) - a[2, 2]).max(axis=2) > 12
        y, x = np.nonzero(diff)
        return ((x.max() - x.min() + 1) / a.shape[1], (y.max() - y.min() + 1) / a.shape[0]) if len(x) else (0, 0)

    def err(**opts):
        y = client.request(sel, "view", width=200, options=opts)
        return "" if y.get("ok") else y["error"]["message"]

    red_setup = "m = bpy.data.materials.new('red'); m.diffuse_color = (1, 0, 0, 1)\n"
    mark = ("bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(3, 4, 2)); o = bpy.context.active_object; o.name = 'mark'; o.data.materials.append(m)")
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\n" + red_setup + mark)
    # view directions: the red mark at (3, 4, 2), the gray cube at the origin; in every direction the side the mark goes to on screen must match Blender's standard views
    # (bottom = Ctrl+Numpad7: right +X, up -Y; Blender source view3d_utils.cc view3d_quat_axis BOTTOM (0, -1, 0, 0))
    expected = {"front": (1, 1), "back": (-1, 1), "left": (-1, 1), "right": (1, 1), "top": (1, 1), "bottom": (1, -1)}
    for name, (sx, sy) in expected.items():
        a, _ = grab(300, angles=[name], target=["Cube", "mark"])
        d = from_center(a)
        check(f"[view] angle '{name}': the mark is on the {'right' if sx > 0 else 'left'} and {'up' if sy > 0 else 'down'} on screen (same direction as Blender's standard view)",
              d is not None and d[0] * sx > 5 and -d[1] * sy > 5, d)
    a, _ = grab(300, angles=[[0, 0, -1]], target=["Cube", "mark"])
    d = from_center(a)
    check("[view] angle [0,0,-1] (an eye below) is the same as 'bottom': the mark is on the right and down", d is not None and d[0] > 5 and d[1] > 5, d)
    a, _ = grab(300, angles=["iso"], target=["Cube", "mark"])
    check("[view] angle 'iso': the mark is in the frame", from_center(a) is not None)
    ref = from_center(grab(300, angles=["front"], target=["Cube", "mark"])[0])
    same = []
    for v in ("FRONT", [0, -1, 0], "Front", "front"):
        d = from_center(grab(300, angles=[v], target=["Cube", "mark"])[0])
        same.append(d is not None and abs(d[0] - ref[0]) < 1.5 and abs(d[1] - ref[1]) < 1.5)
    check("[view] angle: upper/lower case and an [x, y, z] direction give the same view (front = FRONT = [0,-1,0])", all(same), same)
    # default background image: 4 angles, grid layout and size
    png, caption = client.view(sel, 640, caption=True)
    a = np.asarray(PILImage.open(io.BytesIO(png)).convert("RGB"))
    check("[view] background default: 4 angles (front, right, top, iso) in 2x2, 640x640, the caption gives the order",
          a.shape[:2] == (640, 640) and "4 angles" in caption and "front, right, top, iso" in caption and "2x2" in caption, (a.shape, caption))
    check("[view] a 2-pixel light gray line between the tiles", (a[100, 319:321] == 200).all() and (a[319:321, 100] == 200).all(), (a[100, 318:322], a[318:322, 100]))
    a, caption = grab(900, angles=["front", "back", "left", "right", "top", "bottom"])
    check("[view] 6 angles: a 3x2 grid, the long side close to width (<= width)", a.shape[:2] == (598, 898) and "3x2" in caption, a.shape)
    a, caption = grab(300, angles=["front"])
    check("[view] a single angle: width x width, no line", a.shape[:2] == (300, 300) and "1 angle:" in caption, a.shape)
    # frame fill and distance
    a, caption = grab(400, angles=["front"], target=["Cube"])
    w, h = fill(a)
    a_far, caption_far = grab(400, angles=["front"], target=["Cube"], distance=30)
    w_far, _ = fill(a_far)
    check("[view] the target is framed: the cube fills 80-99% of the frame; distance=30 moves it away", 0.8 < max(w, h) < 0.99 and w_far < w / 2 and "distance 30" in caption_far, (w, h, w_far, caption_far))
    # shading: wireframe draws edges over the surface, solid does not
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\nbpy.data.objects.remove(bpy.data.objects['Cube'])\nbpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12)")
    edges = lambda a: ImageStat.Stat(PILImage.fromarray(a).filter(ImageFilter.FIND_EDGES).convert("L")).mean[0]  # noqa: E731
    solid, _ = grab(400, angles=["front"], target=["Sphere"], shading="solid")
    wire, caption = grab(400, angles=["front"], target=["Sphere"], shading="wireframe")
    print(f"      [daemon] edge density: solid {edges(solid):.2f}, wireframe {edges(wire):.2f}")
    check("[view] shading='wireframe': the edge density is at least 2.5x solid's; the surface is visible too (the centre is not the background colour)",
          edges(wire) > 2.5 * edges(solid) and np.abs(wire[200, 200].astype(int) - wire[2, 2]).max() > 12 and "shading=wireframe" in caption, (edges(solid), edges(wire), wire[200, 200], wire[2, 2]))
    sphere_state = c("[bpy.data.objects['Sphere'].select_get(), bpy.context.view_layer.objects.active.name]")["result"]
    check("[view] the orange highlight of the selected object is not drawn (edges are dark), the selection comes back after the draw",
          not ((wire[..., 0].astype(int) - wire[..., 2] > 60) & (wire[..., 0] > 180)).any() and sphere_state == [True, "Sphere"], sphere_state)
    # xray: the red ball behind the gray cube in front shows only in xray
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\nfor o in list(bpy.data.objects):\n    bpy.data.objects.remove(o)\n" + red_setup +
      "g = bpy.data.materials.new('gray'); g.diffuse_color = (0.5, 0.5, 0.5, 1)\n"
      "bpy.ops.mesh.primitive_cube_add(size=2, location=(0, -3, 0)); o = bpy.context.active_object; o.name = 'front_box'; o.data.materials.append(g)\n"
      "bpy.ops.mesh.primitive_uv_sphere_add(radius=0.6, location=(0, 3, 0)); o = bpy.context.active_object; o.name = 'back_ball'; o.data.materials.append(m)")
    center = lambda a: a[145:155, 145:155].reshape(-1, 3).mean(axis=0)  # noqa: E731
    solid, _ = grab(300, angles=["front"], shading="solid")
    xray, caption = grab(300, angles=["front"], shading="xray")
    redness = lambda a: float(center(a)[0] - center(a)[2])  # noqa: E731
    check("[view] shading='xray': the red ball behind the cube in front is visible (redness above +4, 6 more than solid); it is not visible in solid",
          redness(solid) < 3 and redness(xray) > 4 and redness(xray) - redness(solid) > 6 and "shading=xray" in caption, (center(solid), center(xray)))
    # every shading works and the view settings are restored
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\n" + red_setup + mark)
    before = state()
    times = {}
    for name in ("solid", "texture", "material", "rendered", "wireframe", "xray"):
        t = time.time()
        a, caption = grab(512, shading=name)
        times[name] = round(time.time() - t, 2)
        check(f"[view] shading='{name}': works, the content is not one colour, the name is in the caption",
              len(np.unique(a.reshape(-1, 3), axis=0)) > 20 and f"shading={name}" in caption, (caption, len(np.unique(a.reshape(-1, 3), axis=0))))
    print(f"      [daemon] image times (512 px, 4 angles): {times}")
    check("[view] after shading/angles the render settings, object/camera/image counts, view shading and overlay settings are restored", state() == before, (before, state()))
    # with a Workbench scene engine Material Preview cannot be assigned to shading.type (it is not in the enum): EEVEE during the draw, same output as an EEVEE scene, the engine comes back
    material_eevee, _ = grab(512, shading="material")
    c("bpy.context.scene.render.engine = 'BLENDER_WORKBENCH'")
    before = state()
    error_wb = err(shading="material")
    diff, caption = None, error_wb
    if not error_wb:
        material_wb, caption = grab(512, shading="material")
        diff = float(np.abs(material_wb.astype(int) - material_eevee.astype(int)).mean())
    check("[view] with a Workbench scene engine shading='material' does not fail and equals the output of an EEVEE scene (mean difference < 3); the engine and view settings are restored",
          not error_wb and "shading=material" in caption and diff < 3 and before[0] == "BLENDER_WORKBENCH" and state() == before, (caption, diff, before[0]))
    c("bpy.context.scene.render.engine = 'BLENDER_EEVEE'")
    # an environment without GPU drawing (faked): the default image falls back to Workbench and says so; a request with options does not silently give something else
    c("from tether.agent import blender_view as bv\n_orig = bv._offscreen\n\ndef _none(w, h):\n    raise bv.NoGpu('fake: no GPU')\n\nbv._offscreen = _none")
    try:
        png, caption = client.view(sel, 320, caption=True)
        fallback_error = err(shading="xray")
    finally:
        c("bv._offscreen = _orig")
    check("[view] without GPU drawing the default image falls back to a Workbench preview and says so (angles/shading were not applied)",
          png[:8] == b"\x89PNG\r\n\x1a\n" and "Workbench preview" in caption and "not applied" in caption, caption)
    check("[view] without GPU drawing a request with options does not silently give another image, it reports the GPU error as is", "fake: no GPU" in fallback_error, fallback_error)
    # camera: mask overlap with a real Workbench render (is the projection matrix right)
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\n" + red_setup + "bpy.data.objects['Cube'].data.materials.clear(); bpy.data.objects['Cube'].data.materials.append(m)")
    a, caption = grab(640, mode="camera")
    check("[view] mode='camera': at the render aspect (1920x1080 -> 640x360), the camera name in the caption", a.shape[:2] == (360, 640) and "camera 'Camera'" in caption, (a.shape, caption))
    c("r = bpy.context.scene.render\nr.engine = 'BLENDER_WORKBENCH'\nr.resolution_x, r.resolution_y, r.resolution_percentage = 640, 360, 100\n"
      f"r.filepath = {(TMP / 'cam_ref.png').as_posix()!r}\nbpy.ops.render.render(write_still=True)")
    ref = np.asarray(PILImage.open(TMP / "cam_ref.png").convert("RGB"))
    m1, m2 = red(a), red(ref)
    iou = (m1 & m2).sum() / max((m1 | m2).sum(), 1)
    check("[view] mode='camera': the red cube overlaps the real Workbench render by 90%+", ref.shape[:2] == (360, 640) and m2.sum() > 500 and iou > 0.9, (iou, m1.sum(), m2.sum()))
    # frame strip: a ball moving across a fixed frame goes left to right; the frame comes back
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\n" + red_setup +
      "bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 3)); o = bpy.context.active_object; o.name = 'mover'; o.data.materials.append(m)\n"
      "o.location.x = 0; o.keyframe_insert('location', frame=1); o.location.x = 10; o.keyframe_insert('location', frame=11)\nbpy.context.scene.frame_set(5)")
    before = state()
    a, caption = grab(400, mode="frames", frames=[1, 6, 11], look="front")
    kw = (400 - 2) // 2
    tile = lambda i: a[(i // 2) * (kw + 2):(i // 2) * (kw + 2) + kw, (i % 2) * (kw + 2):(i % 2) * (kw + 2) + kw]  # noqa: E731
    xs = [from_center(tile(i)) for i in range(3)]
    xs = [d[0] if d is not None else None for d in xs]
    check("[view] mode='frames': at frames 1, 6, 11 the ball moves left to right (a fixed frame); the caption gives the frames",
          None not in xs and xs[0] < xs[1] < xs[2] and xs[2] - xs[0] > 40 and "3 frames" in caption and "1, 6, 11" in caption, (xs, caption))
    check("[view] mode='frames': after the strip the frame and all settings are back (frame 5)", state() == before and before[8] == 5, (before, state()))
    a, caption = grab(640, mode="frames", frames=[1, 6, 11], look="camera")
    check("[view] frames + look='camera': tiles at the render aspect (640x360)", a.shape[:2] == (360, 640) and "look: camera" in caption, (a.shape, caption))
    a, caption = grab(400, mode="frames", frame_count=4)
    check("[view] frame_count=4: an even sample of the frame range (1, 84, 167, 250)", "1, 84, 167, 250" in caption, caption)
    # errors: a clear message, the scene and the view are not disturbed
    before = state()
    for opts, expected_text in ((dict(mode="nonsense"), "unknown mode"), (dict(shading="nonsense"), "unknown shading"), (dict(angles=["side"]), "unknown angle"),
                                (dict(angles=["front"] * 7), "1-6 angles"), (dict(angles=[[0, 0, 0]]), "non-zero"), (dict(target=["Cubee"]), "is not in the scene"),
                                (dict(frames=list(range(1, 14))), "at most 12 frames"), (dict(foo=1), "unknown image option"),
                                (dict(image="no_such.png"), "neither an open image nor a file path")):
        message = err(**opts)
        check(f"[view] error: {expected_text}", expected_text in message, message)
    check("[view] error: a close name is suggested (Cubee -> Cube)", "Cube" in err(target=["Cubee"]))
    c("bpy.data.objects.remove(bpy.data.objects['Camera'])\nbpy.context.scene.camera = None")
    check("[view] error: mode='camera' without a camera says so", "the scene has no camera" in err(mode="camera"))
    check("[view] when no mode is given the options decide it (frames=[1, 6] -> a 2-frame strip)", "2 frames" in grab(300, frames=[1, 6])[1])
    check("[view] conflicting options give a clear error (mode='camera' + angles)", "only apply with mode='angles'" in err(mode="camera", angles=["front"]), err(mode="camera", angles=["front"]))
    # image: Render Result, an image in the file and a file path; scaling down, colour is not converted twice, no temporary image is left
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)\nr = bpy.context.scene.render\nr.engine = 'BLENDER_WORKBENCH'\n"
      "r.resolution_x, r.resolution_y, r.resolution_percentage = 200, 100, 100\nbpy.ops.render.render()")
    before = state()
    a, caption = grab(100, image="Render Result")
    check("[view] image='Render Result': 200x100 -> 100x50, the name and size in the caption", a.shape[:2] == (50, 100) and "image 'Render Result' 100x50" in caption, (a.shape, caption))
    PILImage.new("RGB", (64, 32), (128, 128, 128)).save(TMP / "gray.png")
    a, caption = grab(32, image=str(TMP / "gray.png"))
    check("[view] image=file path: 64x32 -> 32x16, the mid gray does not change (the colour transform is not applied twice)",
          a.shape[:2] == (16, 32) and np.abs(a.astype(int) - 128).max() <= 2, (a.shape, a[8, 16]))
    a, _ = grab(100, image=str(TMP / "gray.png"))
    check("[view] image: asking for a width above the source does not enlarge it (stays 64x32)", a.shape[:2] == (32, 64), a.shape)
    check("[view] image: temporary loaded images are deleted, the settings are restored", state() == before, (before, state()))
    c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
