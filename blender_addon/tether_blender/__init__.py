"""Tether add-on for Blender: starts the Tether agent (tether/agent/blender.py) when Blender opens; it can be switched on and off in Preferences.
The add-on is only a shell, the work is done by the tether package.

Install: link this folder into Blender's scripts/addons folder (a junction or symlink keeps it pointing at the repository), then enable Preferences > Add-ons > Tether.
"""
bl_info = {
    "name": "Tether",
    "author": "Muto Systems",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "category": "Development",
    "description": "Local bridge for AI agents and scripts: runs code on 127.0.0.1 and registers in ~/.tether",
}
import sys
from pathlib import Path

import bpy

_ROOT = str(Path(__file__).resolve().parents[2])      # the junction is resolved: blender_addon/tether_blender -> repository root


def _host():
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from tether.agent import blender
    return blender


def _running():
    return _host().active() is not None


class TETHER_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or __name__

    auto_start: bpy.props.BoolProperty(name="Start when Blender opens", default=True)

    def draw(self, context):
        row = self.layout.row()
        a = _host().active()
        if a is not None:
            row.label(text=f"Running: 127.0.0.1:{a.port}", icon="LINKED")
        else:
            row.label(text="Stopped", icon="UNLINKED")
        row.operator("tether.toggle", text="Stop" if a is not None else "Start")
        self.layout.prop(self, "auto_start")
        self.layout.label(text="The token is in the registry file under ~/.tether/apps; local connections only.")


class TETHER_OT_toggle(bpy.types.Operator):
    """Start or stop the bridge"""
    bl_idname = "tether.toggle"
    bl_label = "Tether"

    def execute(self, context):
        if _running():
            _host().stop_gui()
        else:
            _host().start_gui()
        return {"FINISHED"}


def _auto_start():
    p = bpy.context.preferences.addons.get(__package__ or __name__)
    if p is None or p.preferences.auto_start:
        _host().start_gui()


def register():
    bpy.utils.register_class(TETHER_Preferences)
    bpy.utils.register_class(TETHER_OT_toggle)
    if not bpy.app.background:           # a background process starts the agent with its own script (--daemon)
        bpy.app.timers.register(_auto_start, first_interval=1.0)


def unregister():
    if bpy.app.timers.is_registered(_auto_start):
        bpy.app.timers.unregister(_auto_start)
    _host().stop_gui()
    bpy.utils.unregister_class(TETHER_OT_toggle)
    bpy.utils.unregister_class(TETHER_Preferences)
