"""Which DCS plugins are loaded, and how to open one on a project.

Each plugin calls register("pospro", open_fn) in initGui and unregister in
unload; open_fn(project_path) shows the plugin with that project. The chain
bar uses it to move between plugins on the same job."""
_OPENERS = {}


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
