"""Which DCS plugins are loaded, and how to open one on a project.

Each plugin calls register("pospro", open_fn) in initGui and unregister in
unload; open_fn(project_path) shows the plugin with that project. The chain
bar uses it to move between plugins on the same job.

Publish listeners (API 11): a plugin can ask to be told when any stage publishes a handover
(on_publish(key, fn); fn(project_path, stage) is called after the handover is written, in the publishing thread;
a listener's error never affects the publish). CloudPro uses it to georeference in the background once its
upstream is current."""
import logging

_OPENERS = {}
_LISTENERS = {}


def register(stage, open_fn):
    _OPENERS[stage] = open_fn


def unregister(stage):
    _OPENERS.pop(stage, None)


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
