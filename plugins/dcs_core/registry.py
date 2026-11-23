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
"Line 5"]} (AlignPro: that pair on the Pairs page). A plugin without focus_fn is just opened.

Leave (API 20, DCS Core 1.19.0): leave(widget) closes the plugin window holding widget. Every hand-over to another
plugin (Continue, the chain breadcrumbs, CloudPro's Open in PosPro / AlignPro / Fix in StripPro) opens the next
plugin on the project and then closes the one being left.

Services (API 21, DCS Core 1.20.0): provide(name, fn) / withdraw(name) / service(name) - one plugin offers a job
another can ask for without importing it. RemixPro provides "sonar_to_clraw": fn(paths, job_folder=None,
out_root=None, progress=None, cancelled=None) -> {"files": [.clraw], "notes": [str], "failed": [(path, reason)]}
(PosPro's marine Data page adds SonarView .svlz / .svlog recordings with it)."""
import logging
import sys

# 1.19.2 (Sean, 18 Nov 2026: Continue greyed out in every plugin, StripPro gone from the chain): QGIS reloads DCS
# Core when it is upgraded, which gave a new, empty registry while PosPro, AlignPro, CloudPro and StripPro had
# registered with the old one (they register once, in initGui). The tables live on the sys module, so a reloaded
# DCS Core keeps them.
_STATE = sys.__dict__.setdefault("_dcs_core_registry", {"openers": {}, "focus": {}, "listeners": {}})
_STATE.setdefault("services", {})
_OPENERS = _STATE["openers"]
_FOCUS = _STATE["focus"]
_LISTENERS = _STATE["listeners"]
_SERVICES = _STATE["services"]


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


def leave(widget):
    """Close the plugin window holding widget (API 20, DCS Core 1.19.0; Sean, 18 Nov 2026): moving on to another
    plugin closes the one being left, so only the plugin in use is open. Closing it is the same as its close button
    (CloudPro saves and frees its edit lock). True when a window was closed."""
    try:
        from .window import PluginWindow
    except Exception:                           # noqa: BLE001 - no Qt (tests of the registry alone)
        return False
    w = widget
    while w is not None and not isinstance(w, PluginWindow):
        w = w.parentWidget()
    if w is None:
        return False
    try:
        w.close()
    except RuntimeError:                        # already deleted
        return False
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


def provide(name, fn):
    """Offer a service under name (API 21): replaces an earlier provider of the same name."""
    _SERVICES[name] = fn


def withdraw(name):
    _SERVICES.pop(name, None)


def service(name):
    """The function offered under name, or None when no loaded plugin offers it."""
    return _SERVICES.get(name)
