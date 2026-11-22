"""Blender image engine: draws the scene or the 3D view to a GPU offscreen buffer (in the background too); view direction and framing, shading
(solid, texture, material, rendered, wireframe, xray), multi-angle images, an animation strip, and an open image, all returned as one PNG.

Usage: blender.py calls draw(width, view3d, fallback, mode=, shading=, target=, angles=, distance=, frames=, frame_count=, look=, image=) -> (png, caption)
"""
import contextlib
import difflib
import math
import os
import struct
import tempfile
import zlib

import bpy
import numpy as np
from mathutils import Matrix, Vector

OPTIONS = {"mode", "shading", "target", "angles", "distance", "frames", "frame_count", "look", "image"}
MODES = {"viewport": "viewport", "camera": "camera", "angles": "angles", "frames": "frames"}
SHADINGS = {"solid": "solid", "texture": "texture", "material": "material", "rendered": "rendered", "render": "rendered", "wireframe": "wireframe",
            "wire": "wireframe", "xray": "xray", "x_ray": "xray"}
DIRECTIONS = {"front": (0, -1, 0), "back": (0, 1, 0), "left": (-1, 0, 0), "right": (1, 0, 0), "top": (0, 0, 1), "bottom": (0, 0, -1),
              "iso": (1, -1, 0.8), "three_quarter": (1, -1, 0.8)}
ANGLE_NAMES = "front, back, left, right, top, bottom, iso"
DEFAULT_ANGLES = ["front", "right", "top", "iso"]
_MODE_OPTION = {"angles": "angles", "frames": "frames", "frame_count": "frames", "look": "frames"}      # options that only make sense in that mode
MAX_VIEWS = 6
MAX_FRAMES = 12
GAP = 2                           # gap between tiles (pixels)
GAP_COLOR = (200, 200, 200)
FOV = math.radians(39.6)          # horizontal field of view for the long side (50 mm / 36 mm)
MARGIN = 1.08                     # margin around the framed content
_BOUNDED = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL", "ARMATURE", "LATTICE"}
_OVERLAY_CLEAN = ("show_floor", "show_axis_x", "show_axis_y", "show_axis_z", "show_cursor", "show_text", "show_stats", "show_extras",
                  "show_relationship_lines", "show_outline_selected", "show_object_origins", "show_motion_paths")


class NoGpu(RuntimeError):
    """GPU offscreen drawing could not be started (no GPU/driver)."""


def _norm(s):
    return str(s).lower().replace("-", "_").replace(" ", "_")


def _direction(v):
    if isinstance(v, str):
        k = DIRECTIONS.get(_norm(v))
        if k is None:
            raise RuntimeError(f"unknown angle '{v}'; valid: {ANGLE_NAMES} or an [x, y, z] direction (from the target to the eye)")
        return Vector(k).normalized()
    try:
        d = Vector(tuple(float(x) for x in v))
    except (TypeError, ValueError):
        d = None
    if d is None or len(d) != 3 or d.length < 1e-9:
        raise RuntimeError(f"an angle direction must be a name ({ANGLE_NAMES}) or a non-zero [x, y, z], got: {v!r}")
    return d.normalized()


def _shading_settings(shading):
    """(shading, overlay) properties. wireframe = edges over a plain surface (unlike shading.type=WIREFRAME the surface stays visible)."""
    solid = {"type": "SOLID", "show_xray": False, "light": "STUDIO", "color_type": "MATERIAL", "show_shadows": False, "show_cavity": False}
    if shading == "solid":
        return solid, {}
    if shading == "texture":
        return {**solid, "color_type": "TEXTURE"}, {}
    if shading == "material":
        return {"type": "MATERIAL", "show_xray": False}, {}
    if shading == "rendered":
        return {"type": "RENDERED", "show_xray": False}, {}
    if shading == "wireframe":
        return ({**solid, "color_type": "SINGLE", "single_color": (0.8, 0.8, 0.8)},
                {"show_wireframes": True, "wireframe_threshold": 1.0, "wireframe_opacity": 1.0})
    return {**solid, "show_xray": True, "xray_alpha": 0.5}, {}


@contextlib.contextmanager
def _set(obj, values):
    """Temporarily sets properties; a property that does not exist (another Blender version) is skipped; everything is restored on exit."""
    saved = {}
    try:
        for name, v in values.items():
            if hasattr(obj, name):
                saved[name] = getattr(obj, name)
                setattr(obj, name, v)
        yield
    finally:
        for name, v in saved.items():
            setattr(obj, name, v)


@contextlib.contextmanager
def _material_engine(scene, shading):
    """When the scene engine is Workbench the enum of shading.type has no MATERIAL (rna_View3DShading_type_itemf): EEVEE during the draw, the old engine afterwards."""
    ro = scene.render
    old, temp = ro.engine, None
    if shading == "material" and old == "BLENDER_WORKBENCH":
        temp = next((e.identifier for e in ro.bl_rna.properties["engine"].enum_items if e.identifier.startswith("BLENDER_EEVEE")), None)
        if temp is None:
            raise RuntimeError("the scene engine is Workbench: there is no material mode and EEVEE was not found; use solid, texture or rendered")
        ro.engine = temp
    try:
        yield
    finally:
        if temp:
            ro.engine = old


@contextlib.contextmanager
def _no_selection():
    """The orange outline and bone colour of a selected object spoil a clean image: the selection is cleared during the draw and restored exactly (edit mode is left alone)."""
    layer = bpy.context.view_layer
    selected = [] if bpy.context.mode != "OBJECT" else [o for o in layer.objects if o.select_get()]
    active = layer.objects.active
    for o in selected:
        o.select_set(False)
    if selected:
        layer.update()                # the selection flag reaches the draw copy only after an update
    try:
        yield
    finally:
        for o in selected:
            o.select_set(True)
        layer.objects.active = active
        if selected:
            layer.update()


def _persp(tx, ty, near, far):
    return Matrix(((1 / tx, 0, 0, 0), (0, 1 / ty, 0, 0), (0, 0, -(far + near) / (far - near), -2 * far * near / (far - near)), (0, 0, -1, 0)))


def _up(f):
    """Up hint for view direction f: +Z; when (almost) vertical it follows Blender's Top (+Y up) and Bottom (-Y up, right +X) views."""
    return Vector((0, 0, 1)) if abs(f.z) < 0.999 else Vector((0, -1 if f.z > 0 else 1, 0))


def _view_matrix(eye, target):
    f = (target - eye).normalized()
    up = _up(f)
    r = f.cross(up).normalized()
    u = r.cross(f)
    return Matrix(((r.x, u.x, -f.x, eye.x), (r.y, u.y, -f.y, eye.y), (r.z, u.z, -f.z, eye.z), (0, 0, 0, 1))).inverted()


def _tangent(w, h):
    """Tangent of the half field of view (horizontal, vertical): the FOV applies to the long side."""
    t = math.tan(FOV / 2)
    return (t, t * h / w) if w >= h else (t * w / h, t)


def _points(objects):
    """Corners of the objects' world bounding boxes (N x 3). With no geometry, a 1 m cube around each object's location (lights, empties)."""
    corners, locations = [], []
    for o in objects:
        locations.append(tuple(o.matrix_world.translation))
        bb = o.bound_box if o.type in _BOUNDED else None
        if bb is not None:
            m = np.array(o.matrix_world)
            corners.append(np.array([tuple(c) for c in bb]) @ m[:3, :3].T + m[:3, 3])
    if corners:
        return np.concatenate(corners)
    cube = np.array([(x, y, z) for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)])
    return (np.array(locations, dtype=float).reshape(-1, 1, 3) + cube).reshape(-1, 3)


def _frame(pts, direction, w, h, distance=None):
    """Fits the points into the frame as seen from `direction` (target to eye): (view matrix, projection matrix, distance)."""
    tx, ty = _tangent(w, h)
    center = Vector((pts.min(axis=0) + pts.max(axis=0)) / 2)
    f = -direction
    up = _up(f)
    r = f.cross(up).normalized()
    u = r.cross(f)
    rel = pts - np.array(center)
    if distance is None:
        px, py, pz = rel @ np.array(r), rel @ np.array(u), rel @ np.array(f)
        d = float(np.max(np.maximum(np.abs(px) * MARGIN / tx, np.abs(py) * MARGIN / ty) - pz))
    else:
        d = float(distance)
    d = max(d, 1e-3)
    radius = float(np.linalg.norm(rel, axis=1).max())
    return _view_matrix(center + direction * d, center), _persp(tx, ty, max(d * 0.01, 1e-3), d + 2 * radius + 1.0), d


def _targets(scene, target):
    """(objects, description): the named objects + their children; if empty, all visible objects."""
    if not target:
        objects = [o for o in scene.objects if o.visible_get()]
        return objects, f"all ({len(objects)} objects)"
    names = [target] if isinstance(target, str) else list(target)
    objects = []
    for name in names:
        o = scene.objects.get(name)
        if o is None:
            similar = difflib.get_close_matches(str(name), [x.name for x in scene.objects], 3, 0.4)
            raise RuntimeError(f"target '{name}' is not in the scene" + (f"; similar: {', '.join(similar)}" if similar else ""))
        objects += [o, *o.children_recursive]
    objects = list({o.name: o for o in objects}.values())
    return objects, f"{', '.join(names)[:60]} ({len(objects)} objects)"


def _grid(n, width, ratio):
    """(columns, rows, tile width, tile height) for n tiles: the long side of the combined image becomes `width`."""
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    if cols * ratio >= rows:
        tw = (width - GAP * (cols - 1)) // cols
        th = round(tw / ratio)
    else:
        th = (width - GAP * (rows - 1)) // rows
        tw = round(th * ratio)
    if min(tw, th) < 32:
        raise RuntimeError(f"{n} tiles do not fit this size (tile {tw}x{th}): increase width or ask for fewer views")
    return cols, rows, max(1, tw), max(1, th)


def _compose(tiles, cols, rows, tw, th):
    """Tiles (th, tw, 4) uint8, row 0 on top -> one RGB array."""
    canvas = np.empty((rows * th + GAP * (rows - 1), cols * tw + GAP * (cols - 1), 3), np.uint8)
    canvas[:] = GAP_COLOR
    for i, t in enumerate(tiles):
        y, x = divmod(i, cols)
        canvas[y * (th + GAP):y * (th + GAP) + th, x * (tw + GAP):x * (tw + GAP) + tw] = t[:, :, :3]
    return canvas


def _png(rgb):
    """RGB uint8 (h, w, 3) -> PNG (without Pillow)."""
    h, w, _ = rgb.shape
    raw = np.zeros((h, 1 + w * 3), np.uint8)
    raw[:, 1:] = rgb.reshape(h, w * 3)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 3)) + chunk(b"IEND", b""))


def _offscreen(w, h):
    try:
        import gpu
        if bpy.app.background:
            gpu.init()
        return gpu.types.GPUOffScreen(w, h)
    except Exception as e:
        raise NoGpu(f"GPU offscreen drawing is not available: {e!r}") from e


def _draw(ofs, scene, v3d, rg, w, h, vm, pm):
    import gpu
    with ofs.bind():
        fb = gpu.state.active_framebuffer_get()
        ofs.draw_view3d(scene, bpy.context.view_layer, v3d, rg, vm, pm, do_color_management=True)
        buf = fb.read_color(0, 0, w, h, 4, 0, "UBYTE")
    return np.asarray(buf, dtype=np.uint8).reshape(h, w, 4)[::-1]


def _frame_numbers(scene, frames, count):
    if frames:
        k = [int(x) for x in frames]
        if len(k) > MAX_FRAMES:
            raise RuntimeError(f"at most {MAX_FRAMES} frames can be requested, got {len(k)}")
        return k
    count = max(2, min(MAX_FRAMES, int(count)))
    start, end = scene.frame_start, scene.frame_end
    if end <= start:
        return [start]
    return sorted({round(start + (end - start) * i / (count - 1)) for i in range(count)})


def _user_eye(rg):
    """Direction from the target to the eye of the user's view (windowed); None if the region has no data."""
    rv = rg.data
    return (rv.view_matrix.inverted().to_3x3() @ Vector((0, 0, 1))).normalized() if rv is not None else None


def draw(width, view3d, fallback=None, **options):
    """(png, caption). view3d: the blender.view3d function (3D view context). fallback: a Workbench function for the default image when there is no GPU."""
    unknown = set(options) - OPTIONS
    if unknown:
        raise RuntimeError(f"unknown image option: {', '.join(sorted(unknown))}; valid: {', '.join(sorted(OPTIONS))}")
    if options.get("image"):
        return _image(width, options["image"])
    background = bpy.app.background
    default = "frames" if options.get("frames") or options.get("frame_count") else "angles" if options.get("angles") or background else "viewport"
    mode = MODES.get(_norm(options.get("mode") or default))
    if mode is None:
        raise RuntimeError("unknown mode; valid: viewport, camera, angles, frames")
    mismatch = [p for p, m in _MODE_OPTION.items() if options.get(p) and m != mode]
    if mismatch:
        raise RuntimeError(f"{', '.join(mismatch)} only apply with mode='{_MODE_OPTION[mismatch[0]]}' (now mode='{mode}')")
    shading = options.get("shading")
    if shading:
        shading = SHADINGS.get(_norm(shading))
        if shading is None:
            raise RuntimeError("unknown shading; valid: solid, texture, material, rendered, wireframe, xray")
    elif mode != "viewport" or background:
        shading = "solid"
    scene = bpy.context.scene
    g = view3d()
    if not g:
        if background and fallback and not options:
            return fallback(width), "no 3D view: Workbench preview"
        raise RuntimeError("no open 3D view (the view is closed)")
    v3d, rg = g["area"].spaces.active, g["region"]
    bpy.context.view_layer.update()
    users_view = (not background) and mode == "viewport"
    sh, ov = _shading_settings(shading) if shading else ({}, {})
    if not users_view:
        ov = {**{a: False for a in _OVERLAY_CLEAN}, "show_overlays": True, "show_bones": True, **ov}
    ro = scene.render
    render_ratio = ro.resolution_x * ro.pixel_aspect_x / (ro.resolution_y * ro.pixel_aspect_y)
    objects, target_text = _targets(scene, options.get("target"))
    distance = options.get("distance") or None
    try:
        with _material_engine(scene, shading), _set(v3d.shading, sh), _set(v3d.overlay, ov), (contextlib.nullcontext() if users_view else _no_selection()):
            if mode == "camera":
                png, caption = _camera(width, scene, v3d, rg, render_ratio)
            elif mode == "angles":
                png, caption = _angles(width, scene, v3d, rg, objects, options.get("angles"), distance)
            elif mode == "frames":
                png, caption = _series(width, scene, v3d, rg, objects, options, render_ratio, distance)
            else:
                png, caption = _single(width, scene, v3d, rg, objects, options.get("target"), distance, users_view, render_ratio)
    except NoGpu as e:
        if background and fallback and not options:
            return fallback(width), f"{e}; a Workbench preview was returned (angles and shading were not applied)"
        raise
    extra = [f"shading={shading}"] if shading else []
    if mode in ("angles", "frames") or options.get("target"):
        extra.append(f"target={target_text}")
    return png, " · ".join([caption, *extra])


def _empty_scene():
    return RuntimeError("the scene has no camera and no geometry: nothing to show")


def _camera_matrices(scene, w, h):
    cam = scene.camera
    if cam is None:
        raise RuntimeError("the scene has no camera (mode='camera'): add a camera or use mode='angles'")
    dg = bpy.context.evaluated_depsgraph_get()
    return cam.matrix_world.inverted(), cam.calc_matrix_camera(dg, x=w, y=h, scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y)


def _size(width, ratio):
    return (width, max(1, round(width / ratio))) if ratio >= 1 else (max(1, round(width * ratio)), width)


def _single_tile(width, ratio, drawer):
    """One view: drawer(ofs, w, h) -> (h, w, 4)."""
    w, h = _size(width, ratio)
    ofs = _offscreen(w, h)
    try:
        return _png(drawer(ofs, w, h)[:, :, :3])
    finally:
        ofs.free()


def _camera(width, scene, v3d, rg, ratio):
    def drawer(ofs, w, h):
        vm, pm = _camera_matrices(scene, w, h)
        return _draw(ofs, scene, v3d, rg, w, h, vm, pm)
    return _single_tile(width, ratio, drawer), f"camera '{scene.camera.name}'"


def _single(width, scene, v3d, rg, objects, target, distance, users_view, render_ratio):
    if users_view:
        ratio = rg.width / rg.height
        direction = _user_eye(rg) if target else None

        def drawer(ofs, w, h):
            if direction is None:
                return _draw(ofs, scene, v3d, rg, w, h, rg.data.view_matrix, rg.data.window_matrix)
            pts = _points(objects)
            vm, pm, _ = _frame(pts, direction, w, h, distance)
            return _draw(ofs, scene, v3d, rg, w, h, vm, pm)
        return _single_tile(width, ratio, drawer), "window view"
    if scene.camera is not None and not target:
        return _camera(width, scene, v3d, rg, render_ratio)
    pts = _points(objects)
    if not len(pts):
        raise _empty_scene()
    direction = _direction("iso")

    def drawer(ofs, w, h):
        vm, pm, _ = _frame(pts, direction, w, h, distance)
        return _draw(ofs, scene, v3d, rg, w, h, vm, pm)
    return _single_tile(width, render_ratio, drawer), "iso"


def _angles(width, scene, v3d, rg, objects, angles, distance):
    items = [angles] if isinstance(angles, str) else list(angles or DEFAULT_ANGLES)
    if not 1 <= len(items) <= MAX_VIEWS:
        raise RuntimeError(f"1-{MAX_VIEWS} angles can be requested, got {len(items)}")
    directions = [_direction(a) for a in items]
    pts = _points(objects)
    if not len(pts):
        raise _empty_scene()
    cols, rows, tw, th = _grid(len(directions), width, 1.0)
    ofs = _offscreen(tw, th)
    try:
        tiles, distances = [], []
        for d in directions:
            vm, pm, dist = _frame(pts, d, tw, th, distance)
            tiles.append(_draw(ofs, scene, v3d, rg, tw, th, vm, pm))
            distances.append(dist)
    finally:
        ofs.free()
    png = _png(tiles[0][:, :, :3] if len(tiles) == 1 else _compose(tiles, cols, rows, tw, th))
    names = [a if isinstance(a, str) else str([round(x, 2) for x in a]) for a in items]
    order = f" ({cols}x{rows}, left to right, top to bottom)" if len(names) > 1 else ""
    return png, f"{len(names)} angle{'s' if len(names) > 1 else ''}{order}: {', '.join(names)} · distance {min(distances):.3g}-{max(distances):.3g} m"


def _series(width, scene, v3d, rg, objects, options, render_ratio, distance):
    frames = _frame_numbers(scene, options.get("frames"), options.get("frame_count") or 6)
    look = options.get("look")
    use_camera = isinstance(look, str) and MODES.get(_norm(look)) == "camera"
    if use_camera:
        cols, rows, tw, th = _grid(len(frames), width, render_ratio)
        direction, name = None, "camera"
    else:
        direction = _direction(look) if look else (_user_eye(rg) if not bpy.app.background and _user_eye(rg) is not None else _direction("iso"))
        cols, rows, tw, th = _grid(len(frames), width, 1.0)
        name = look if isinstance(look, str) else (str([round(x, 2) for x in look]) if look else "window view")
    saved = (scene.frame_current, scene.frame_subframe)
    ofs = _offscreen(tw, th)
    try:
        if direction is not None:
            pts = []
            for k in frames:
                scene.frame_set(k)
                bpy.context.view_layer.update()
                pts.append(_points(_targets(scene, options.get("target"))[0]))
            pts = np.concatenate(pts)
            if not len(pts):
                raise _empty_scene()
            vm, pm, _ = _frame(pts, direction, tw, th, distance)
        tiles = []
        for k in frames:
            scene.frame_set(k)
            bpy.context.view_layer.update()
            if direction is None:
                vm, pm = _camera_matrices(scene, tw, th)
            tiles.append(_draw(ofs, scene, v3d, rg, tw, th, vm, pm))
    finally:
        ofs.free()
        scene.frame_set(saved[0], subframe=saved[1])
    png = _png(tiles[0][:, :, :3] if len(tiles) == 1 else _compose(tiles, cols, rows, tw, th))
    order = f" ({cols}x{rows}, left to right, top to bottom)" if len(frames) > 1 else ""
    return png, f"{len(frames)} frame{'s' if len(frames) > 1 else ''}{order}: {', '.join(map(str, frames))} · look: {name}"


def _image(width, name):
    """An open image ('Render Result', another image in the file) or a file path; the long side is shrunk to `width`."""
    scene = bpy.context.scene
    img = bpy.data.images.get(name)
    loaded = None
    if img is None:
        path = os.path.abspath(bpy.path.abspath(name))
        if not os.path.isfile(path):
            open_images = [i.name for i in bpy.data.images][:12]
            raise RuntimeError(f"image '{name}' is neither an open image nor a file path; open images: {', '.join(open_images) or 'none'}")
        img = loaded = bpy.data.images.load(path)
    settings = scene.render.image_settings
    saved = (settings.file_format, settings.color_mode, settings.color_depth)
    t1 = os.path.join(tempfile.gettempdir(), f"tether_{os.getpid()}_a.png")
    t2 = os.path.join(tempfile.gettempdir(), f"tether_{os.getpid()}_b.png")
    small = None
    try:
        settings.file_format, settings.color_mode, settings.color_depth = "PNG", "RGB", "8"
        try:                           # in the background Render Result's .size is always (0, 0): being able to write the file is how emptiness shows
            img.save_render(t1, scene=scene)
        except RuntimeError:
            pass
        if not os.path.isfile(t1):
            raise RuntimeError(f"'{img.name}' could not be written: it looks empty (render first for Render Result)")
        w, h = struct.unpack(">II", open(t1, "rb").read(24)[16:24])
        if max(w, h) > width:
            small = bpy.data.images.load(t1)
            o = width / max(w, h)
            w, h = max(1, round(w * o)), max(1, round(h * o))
            small.scale(w, h)
            small.save_render(t2, scene=scene)
            final = t2
        else:
            final = t1
        with open(final, "rb") as f:
            return f.read(), f"image '{img.name}' {w}x{h}"
    finally:
        settings.file_format, settings.color_mode, settings.color_depth = saved
        for i in (small, loaded):
            if i is not None:
                bpy.data.images.remove(i)
        for t in (t1, t2):
            if os.path.exists(t):
                os.remove(t)
