"""Tests against a real Blender: a headless daemon (scene report, images, asset import, helpers, lifecycle) and, with --gui, a windowed Blender.
Needs Blender, numpy and Pillow. The Sollumz (GTA) checks only run when that add-on is available."""
import base64
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import bl_assets
import bl_common
import bl_report
import bl_view
import harness
from harness import TMP, check, client, kill_pid, pid_alive


def daemon_tests():
    print("--- Blender background daemon")
    t0 = time.time()
    s = client.open_app("blender", background=True, addons=["Sollumz"], wait=90)
    sel, pid = s["selector"], s["pid"]
    print(f"      ready: {time.time() - t0:.1f} s (default: no user settings; add-on: Sollumz if available)")
    owner = s2 = s3 = None
    try:
        c = lambda code, **kw: client.run(sel, code=code, **kw)  # noqa: E731
        first = c("bpy.app.background")
        sollumz = harness.STATE["sollumz"] = "could not be enabled" not in first.get("warning", "")
        check("[daemon] mode background", client.request(sel, "info")["mode"] == "background" and first.get("result") is True)
        if sollumz:
            check("[daemon] no warning at start (the add-ons opened completely)", not first.get("warning"), first.get("warning"))
            check("[daemon] clean start: Sollumz is on (api_search finds it)", "sollumz" in client.api_search(sel, "sollumz").lower())
            y = c("import szio\nhasattr(bpy.ops.sollumz, 'import_assets') and hasattr(bpy.ops.sollumz, 'export_assets')")
            check("[daemon] Sollumz fully loaded: szio imports, the import/export operators are registered (not the missing-dependency mode)", y.get("result") is True, y)
        else:
            print("      Sollumz is not available: the GTA checks are skipped")
        outside = c("import sys, os\nlocal = os.path.normcase(bpy.utils.resource_path('LOCAL'))\n"
                    "[n for n in bpy.context.preferences.addons.keys() if not os.path.normcase(getattr(sys.modules.get(n), '__file__', '') or '').startswith(local)]").get("result")
        check("[daemon] no side effect of user add-ons: outside Blender's own install only the requested add-on is enabled", outside is not None and set(outside) <= {"Sollumz"}, outside)
        real = Path(os.environ.get("APPDATA", "")) / "Blender Foundation" / "Blender" / c("'.'.join(map(str, bpy.app.version[:2]))")["result"] / "config" / "userpref.blend"
        before = real.read_bytes() if real.is_file() else None
        y = c("r = bpy.ops.wm.save_userpref()\nimport os\n[bpy.utils.user_resource('CONFIG'), os.path.exists(os.path.join(bpy.utils.user_resource('CONFIG'), 'userpref.blend'))]")
        check("[daemon] wm.save_userpref writes to a private folder, not the real config; the user's userpref.blend stays the same",
              y.get("ok") and "blender_config" in y["result"][0] and y["result"][1] is True and (before is None or real.read_bytes() == before), y)
        y = c("bpy.ops.wm.read_homefile()\n[len(bpy.data.objects), bpy.data.filepath]")
        check("[daemon] read_homefile is hermetic: the user's startup.blend is not loaded (the factory scene)", y.get("ok") and y["result"][0] <= 3 and y["result"][1] == "", y)
        # script runner: instead of the old 'blender -b X.blend --python b.py -- a'
        project = TMP / "blender_project"
        project.mkdir()
        c(f"bpy.ops.mesh.primitive_cube_add()\nbpy.context.object.name = 'MARK'\nbpy.ops.wm.save_as_mainfile(filepath={(project / 'scene.blend').as_posix()!r})")
        (project / "script.py").write_text("import sys, os, bpy\nprint('file', os.path.basename(bpy.data.filepath), 'cwd', os.path.basename(os.getcwd()), "
                                           "'args', sys.argv[sys.argv.index('--') + 1:])\nprint('MARK' in bpy.data.objects)\nresult = os.environ.get('KT_BLEND')\n", encoding="utf-8")
        y = client.run(sel, file="script.py", cwd=str(project), blend="scene.blend", args=["x"], env={"KT_BLEND": "v"})
        check("[daemon] file+cwd+blend+args+env: the scene opens, relative paths resolve against cwd",
              y["ok"] and "file scene.blend cwd blender_project args ['x']" in y["output"] and "True" in y["output"] and y["result"] == "v", y)
        (project / "mutate.py").write_text("import bpy\nbpy.data.objects['MARK'].name = 'CHANGED'\nprint('mutation done')\n", encoding="utf-8")
        y1 = client.run(sel, file="mutate.py", cwd=str(project), blend="scene.blend")
        y2 = client.run(sel, file="mutate.py", cwd=str(project), blend="scene.blend")
        check("[daemon] blend opens the file fresh on every run (the second run finds MARK too)", y1["ok"] and y2["ok"], y2)
        y3 = client.run(sel, file="mutate.py", cwd=str(project))
        check("[daemon] without blend the scene stays as it is (the earlier CHANGED is still there)", not y3["ok"] and y3["error"]["type"] == "KeyError", y3)
        y = client.run(sel, code="[len(bpy.data.objects), bpy.data.filepath]", clean=True)
        check("[daemon] clean=True returns to the factory scene" + ("; Sollumz stays fully loaded" if sollumz else ""),
              y["ok"] and y["result"][0] <= 3 and y["result"][1] == "" and (c("hasattr(bpy.ops.sollumz, 'export_assets')").get("result") is True if sollumz else True), y)
        if sollumz:
            c("bpy.ops.wm.read_factory_settings(use_empty=True)")
            y = c("'Sollumz' in bpy.context.preferences.addons")
            check("[daemon] when a script calls wm.read_factory_settings Sollumz drops out of the preferences; the bridge enables it again on the next call and warns",
                  y.get("result") is True and "Sollumz had been closed" in y.get("warning", ""), y)
            c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
            check("[daemon] since it was enabled again the Sollumz operators also survive the next file load (they would have vanished otherwise)",
                  c("hasattr(bpy.ops.sollumz, 'export_assets')").get("result") is True)
        try:
            client.run(sel, code="1", blend="missing.blend", cwd=str(project))
            ok = False
        except client.TetherError as e:
            ok = "blend file not found" in str(e)
        check("[daemon] a blend that does not exist: the client raises a clear error, the daemon is not affected", ok and c("1").get("result") == 1)
        bl_common.common(sel, c, "daemon")
        import struct
        from PIL import Image as PILImage
        settings = lambda: c("r = bpy.context.scene.render\n[r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath, r.use_file_extension, "  # noqa: E731
                             "bpy.context.scene.camera and bpy.context.scene.camera.name, len(bpy.data.objects), len(bpy.data.cameras), len(bpy.data.images)]")["result"]
        bl_common.helpers(c)
        bl_report.run(sel, c)
        c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
        y = c("r = bpy.context.scene.render\nr.engine = 'BLENDER_WORKBENCH'\nr.resolution_x = r.resolution_y = 64\n"
              f"r.filepath = {(TMP / 'c_render.png').as_posix()!r}\nbpy.ops.render.render(write_still=True)")
        check("[daemon] Blender's C-level output ('Saved:' from the render) is captured (in a console-less WMI process too)", y["ok"] and "Saved:" in y.get("output", ""), y)
        c("bpy.ops.wm.read_homefile(use_factory_startup=True)")
        before = settings()
        t = time.time()
        png = client.view(sel, 640)
        first_call = time.time() - t
        t = time.time()
        client.view(sel, 640)
        print(f"      [daemon] view 640 px: first call {first_call:.2f} s, second {time.time() - t:.2f} s")
        w, h = struct.unpack(">II", png[16:24])
        colors = PILImage.open(io.BytesIO(png)).convert("RGB").getcolors(maxcolors=1 << 20)
        check("[daemon] view: a default PNG, long side 640, the content is not one colour", png[:8] == b"\x89PNG\r\n\x1a\n" and max(w, h) == 640 and len(colors) > 3, (w, h))
        check("[daemon] view: render settings, objects, camera and image data are restored", settings() == before, (before, settings()))
        c("bpy.data.objects.remove(bpy.data.objects['Camera'])\nbpy.context.scene.camera = None")
        before = settings()
        png = client.view(sel, 480)
        check("[daemon] view: a scene without a camera is framed too (no temporary object, same settings)", png[:8] == b"\x89PNG\r\n\x1a\n" and settings() == before, (before, settings()))
        c("bpy.ops.wm.read_homefile(use_empty=True)")
        y = client.request(sel, "view", width=320)
        check("[daemon] view: an empty scene gives a clear error", not y["ok"] and "nothing to show" in y["error"]["message"], y)
        y = client.request(sel, "view", area="window")
        check("[daemon] view: area cannot be chosen in the background (a clear error)", not y["ok"] and "area cannot be chosen" in y["error"]["message"], y)
        bl_view.run(sel, c)
        bl_assets.run(sel, c)
        y = c("bpy.ops.view3d.view_all()")
        check("[daemon] a 3D view operator in the background: the context hint", "temp_override" in y["error"].get("hint", ""), y.get("error"))
        client.request(sel, "close")
        for _ in range(50):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        check("[daemon] close: the process exits, the record is deleted", not pid_alive(pid) and client.apps() == [])
        # no orphans: it closes when the owner process dies; with no requests it closes through the idle timeout
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4)"])
        s2 = client.open_app("blender", background=True, owner_pid=owner.pid, wait=60)
        owner.wait()
        for _ in range(120):
            if not pid_alive(s2["pid"]):
                break
            time.sleep(0.1)
        check("[daemon] when the owner process dies the daemon closes itself (no orphans)", not pid_alive(s2["pid"]))
        s3 = client.open_app("blender", background=True, idle_min=0.1, wait=60)
        for _ in range(150):
            if not pid_alive(s3["pid"]):
                break
            time.sleep(0.1)
        check("[daemon] the idle timeout (no request for 6 s) closes the daemon", not pid_alive(s3["pid"]))
    finally:
        for p in (pid, s2 and s2["pid"], s3 and s3["pid"]):
            if p and pid_alive(p):
                kill_pid(p)


def gui_tests():
    print("--- Blender windowed (a Blender window opens and closes)")
    from PIL import Image
    t0 = time.time()
    s = client.open_app("blender", background=False, wait=120)
    sel, pid = s["selector"], s["pid"]
    print(f"      ready: {time.time() - t0:.1f} s")
    try:
        c = lambda code, **kw: client.run(sel, code=code, **kw)  # noqa: E731
        check("[gui] mode gui, a window exists", client.request(sel, "info")["mode"] == "gui" and c("len(bpy.context.window_manager.windows)").get("result", 0) >= 1)
        bl_common.common(sel, c, "gui")
        n0 = c("len(bpy.data.objects)")["result"]
        y1 = client.run(sel, code="1", clean=True)
        y2 = client.run(sel, code="1", blend=str(TMP / "copy_gui.blend"))
        check("[gui] clean/blend does not wipe the windowed Blender's open scene: a clear error, the scene is the same",
              not y1["ok"] and not y2["ok"] and "windowed Blender" in y1["error"]["message"] and c("len(bpy.data.objects)")["result"] == n0, (y1, y2))
        im = Image.open(io.BytesIO(client.view(sel, 1024)))
        check("[gui] view: the 3D view, not 1x1", im.size[0] > 200 and im.size[1] > 200, im.size)
        y = client.request(sel, "view", width=512)
        im2 = Image.open(io.BytesIO(base64.b64decode(y["png_b64"]))) if y["ok"] else None
        check("[gui] view: GPU offscreen drawing works (it does not fall back to a screenshot), long side 512",
              y["ok"] and not y.get("warning") and max(im2.size) == 512 and len(im2.convert("RGB").getcolors(maxcolors=1 << 20)) > 3, y.get("warning") or y.get("error"))
        y = client.request(sel, "view", area="window")
        window = Image.open(io.BytesIO(base64.b64decode(y["png_b64"]))) if y["ok"] else None
        check("[gui] view area='window': the whole window including the UI (larger than the 3D view image)", y["ok"] and max(window.size) > 512 and window.size[0] >= 640, y.get("error") or (window and window.size))
        y = client.request(sel, "view", area="OUTLINER")
        area = Image.open(io.BytesIO(base64.b64decode(y["png_b64"]))) if y["ok"] else None
        check("[gui] view area='OUTLINER': a screenshot of that area", y["ok"] and area.size[0] > 50 and area.size[1] > 50, y.get("error") or (area and area.size))
        y = client.request(sel, "view", area="NO_SUCH_AREA")
        check("[gui] view: an area type that does not exist gives a clear error and lists the open area types", not y["ok"] and "VIEW_3D" in y["error"]["message"], y)
        # image options in the windowed Blender: the user's view, shading and selection are the same after the draw
        c("bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))\nbpy.context.active_object.name = 'gui_box'")
        gui_state = lambda: c("v = [a for w in bpy.context.window_manager.windows for a in w.screen.areas if a.type == 'VIEW_3D'][0].spaces.active\n"  # noqa: E731
                              "[v.shading.type, v.shading.show_xray, v.shading.color_type, v.overlay.show_overlays, v.overlay.show_floor, v.overlay.show_wireframes, "
                              "v.overlay.show_extras, sorted(o.name for o in bpy.context.selected_objects), bpy.context.view_layer.objects.active.name, bpy.context.scene.frame_current]")["result"]
        before = gui_state()
        png, caption = client.view(sel, 512, caption=True, shading="wireframe", angles=["front", "top"], target=["gui_box"])
        im = Image.open(io.BytesIO(png))
        check("[gui] view: angles + shading='wireframe' + target work in the windowed Blender (2 angles, 512x255), the caption says so",
              im.size == (512, 255) and "2 angles" in caption and "shading=wireframe" in caption and len(im.convert("RGB").getcolors(maxcolors=1 << 20)) > 10, (im.size, caption))
        png, caption = client.view(sel, 512, caption=True, shading="xray")
        im = Image.open(io.BytesIO(png))
        check("[gui] view: shading='xray' draws the user's view (window aspect), long side 512, 'window view' in the caption",
              max(im.size) == 512 and "window view" in caption and "shading=xray" in caption, (im.size, caption))
        has_camera, rx, ry = c("r = bpy.context.scene.render\n[bpy.context.scene.camera is not None, r.resolution_x, r.resolution_y]")["result"]
        y = client.request(sel, "view", width=512, options={"mode": "camera"})
        if has_camera:
            im = Image.open(io.BytesIO(base64.b64decode(y["png_b64"]))) if y["ok"] else None
            check("[gui] view: mode='camera' at the scene's render aspect, long side 512", y["ok"] and max(im.size) == 512 and abs(im.size[0] / im.size[1] - rx / ry) < 0.02, y.get("error") or im.size)
        else:
            check("[gui] view: mode='camera' in a scene without a camera gives a clear error", not y["ok"] and "the scene has no camera" in y["error"]["message"], y)
        check("[gui] after the image options the view shading, overlay lines, selection, active object and frame are the same", gui_state() == before, (before, gui_state()))
        y = client.request(sel, "view", width=256, area="OUTLINER", options={"shading": "xray"})
        check("[gui] view: area cannot be combined with image options (a clear error)", not y["ok"] and "cannot be combined" in y["error"]["message"], y)
        if bl_assets.GLTF:
            r = client.call(sel, "import_asset", {"kind": "model", "name": "gui_asset", "file": bl_assets.GLTF[0], "target_size": 2.0})["result"]
            check("[gui] import_asset: a glTF enters the windowed Blender (2x2x2, root 'gui_asset') and works in object mode",
                  r["size"] == [2.0, 2.0, 2.0] and r["root"] == "gui_asset" and c("bpy.data.objects.get('gui_asset') is not None")["result"] is True, r)
        y = c("with bpy.context.temp_override(**tether.view3d()):\n    bpy.ops.view3d.view_all()\n'done'")
        check("[gui] tether.view3d() lets a 3D view operator run", y.get("result") == "done", y.get("error"))
        th = threading.Thread(target=lambda: c("import time\ntime.sleep(2.5)"))
        th.start()
        time.sleep(0.7)
        b = client.request(sel, "info")
        th.join()
        check("[gui] info is answered while busy", b["busy"] and b["running"]["code"] == "import time", b)
        # undo: stale bpy references are removed (undo_pre); if ed.undo does not work in this environment a note is printed
        c("me2 = bpy.data.meshes.new('tether_u')\nbpy.ops.ed.undo_push(message='a')\nbpy.data.meshes.new('tether_u2')\nbpy.ops.ed.undo_push(message='b')")
        y = c("with bpy.context.temp_override(**tether.view3d()):\n    bpy.ops.ed.undo()\n'done'")
        if y["ok"]:
            check("[gui] undo: stale bpy references are removed from the namespace and the warning says so", "undo/redo" in y.get("warning", "") and "me2" in y["warning"], y)
        else:
            print("      [gui] ed.undo did not work, the undo cleanup could not be verified:", y["error"]["message"][:90])
        # while a modal operator is running no job is run from the timer (a known crash source): the job waits, info.extra reports the operator
        c("o2 = bpy.data.objects.new('tether_m', None)\nbpy.context.collection.objects.link(o2)\nbpy.context.view_layer.objects.active = o2\no2.select_set(True)")
        y = c("with bpy.context.temp_override(**tether.view3d()):\n    r = bpy.ops.transform.translate('INVOKE_DEFAULT')\nr")
        time.sleep(0.6)
        b = client.request(sel, "info")
        modal = (b.get("extra") or {}).get("modal_operators")
        if modal:
            try:
                client.run(sel, code="1", timeout=3)
                waited = False
            except client.TetherError:
                waited = True
            time.sleep(0.5)
            check("[gui] while a modal operator (move) is running the job is not run from the timer; info.extra reports the modal operator", waited, modal)
        else:
            print("      [gui] the modal operator did not start, the modal guard could not be verified:", y.get("result"), y.get("error"))
    finally:
        kill_pid(pid)
        time.sleep(1)
    check("[gui] when the process is killed the stale record is deleted", client.apps() == [])


def run_all(gui=False):
    daemon_tests()
    if gui:
        gui_tests()
