"""Which DCS plugins are loaded, and how to open one on a project.

Each plugin calls register("pospro", open_fn) in initGui and unregister in
unload; open_fn(project_path) shows the plugin with that project. The chain
bar uses it to move between plugins on the same job.

Publish listeners (API 11): a plugin can ask to be told when any stage publishes a handover
(on_publish(key, fn); fn(project_path, stage) is called after the handover is written, in the publishing thread;
a listener's error never affects the publish). CloudPro uses it to georeference in the background once its
upstream is current.

Focus (API 13, DCS Core 1.12.0): register(stage, open_fn, focus_fn) - focus(stage, project_path, target) opens the
plugin on the project and then calls focus_fn(target), a dict saying what to show, e.g. {"kind": "cloud_area",
"id": "C3"} (PosPro: that problem area in Trajectory Review) or {"kind": "cloud_pair", "lines": ["Line 4",
"Line 5"]} (AlignPro: that pair on the Pairs page). A plugin without focus_fn is just opened."""
import logging

_OPENERS = {}
_FOCUS = {}
_LISTENERS = {}


def register(stage, open_fn, focus_fn=None):
    _OPENERS[stage] = open_fn
    if focus_fn is not None:
        _FOCUS[stage] = focus_fn
    else:
        _FOCUS.pop(stage, None)


def unregister(stage):
    _OPENERS.pop(stage, None)
    _FOCUS.pop(stage, None)


def focus(stage, project_path, target):
    """Open stage's plugin on project_path and show target there (API 13). False when the plugin is not loaded."""
    if not open(stage, project_path):
        return False
    fn = _FOCUS.get(stage)
    if fn is not None:
        try:
            fn(target)
        except Exception:                       # noqa: BLE001 - showing the item is a convenience
            logging.getLogger("dcs_core").exception("focus %s %s", stage, target)
    return True


def has(stage):
    return stage in _OPENERS


def open(stage, project_path):    # noqa: A001 - mirrors the plugin action
    fn = _OPENERS.get(stage)
    if fn is None:
        return False
    fn(project_path)
    return True


def on_publish(key, fn):
    """Call fn(project_path, stage) after every handover published on this computer (replaces key's listener)."""
    _LISTENERS[key] = fn


def off_publish(key):
    _LISTENERS.pop(key, None)


def notify_publish(project_path, stage):
    """Called by handover.publish once the handover is written and the stage is marked done."""
    for key, fn in list(_LISTENERS.items()):
        try:
            fn(project_path, stage)
        except Exception:               # noqa: BLE001 - a listener never breaks a publish
            logging.getLogger("dcs_core").exception("publish listener %s failed", key)
