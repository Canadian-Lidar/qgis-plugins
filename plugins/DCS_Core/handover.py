"""Handover files: what one stage publishes for the next.

publish() writes the data files' id and the handover JSON last, then marks
the stage done in the project with what it was built from. read() returns a
stage's handover with absolute file paths, refusing an unknown schema. No Qt.
"""
import os

from . import files
from .project import STAGE, ProjectError, now


class HandoverError(ProjectError):
    pass


def out_dir(project, stage):
    return project.stage_dir(stage, "out")


def publish(project, stage, data_files, fields, plugin, version="", engine=""):
    """data_files: {"sbet": abs path, ...} all inside the stage's out/ folder
    and already complete. fields: the stage-specific content (time, frame,
    quality, ...). Returns the handover dict."""
    od = out_dir(project, stage)
    key, folder, label, schema, fname = STAGE[stage]
    rel = {}
    for k, p in data_files.items():
        p = os.path.abspath(p)
        if not os.path.exists(p):
            raise HandoverError("%s: %s was not written" % (label, os.path.basename(p)))
        if os.path.dirname(p) != os.path.abspath(od):
            raise HandoverError("%s: %s is not in %s" % (label, p, od))
        rel[k] = os.path.basename(p)
    hid = files.content_id([data_files[k] for k in sorted(data_files)])
    built = project.built_from_now(stage)
    doc = {"schema": schema, "id": hid, "producer": {"plugin": plugin, "version": version, "engine": engine},
           "created": now(), "project": project.name, "built_from": built, "files": rel}
    doc.update(fields)
    path = os.path.join(od, fname)
    files.atomic_write_json(path, doc)
    project.update_stage(stage, by=plugin, status="done", last_run="done", handover=files.portable(path, project.folder),
                         handover_id=hid, built_from=built, finished=now(), version=version, error="")
    return doc


def read(project, stage, required=True):
    """The published handover of a stage, with "paths" = absolute data file
    paths. None (or HandoverError when required) if the stage has not
    published."""
    s = project.stage(stage)
    label, schema = STAGE[stage][2], STAGE[stage][3]
    if not s.get("handover"):
        if required:
            raise HandoverError("%s has not been run for this project yet" % label)
        return None
    path = files.resolve(s["handover"], project.folder)
    try:
        doc = files.read_json(path)
    except (OSError, ValueError) as e:
        raise HandoverError("cannot read the %s results (%s): %s" % (label, path, e))
    got = str(doc.get("schema", ""))
    if got.split("/")[0] != schema.split("/")[0]:
        raise HandoverError("%s is not a %s file" % (path, schema))
    if int(got.split("/")[1]) > int(schema.split("/")[1]):
        raise HandoverError("the %s results were written by a newer version; update DCS Core" % label)
    od = os.path.dirname(path)
    doc["paths"] = {k: os.path.join(od, v) for k, v in (doc.get("files") or {}).items()}
    missing = [p for p in doc["paths"].values() if not os.path.exists(p)]
    if missing:
        raise HandoverError("%s results are incomplete: %s missing" % (label, ", ".join(os.path.basename(m)
                                                                                         for m in missing)))
    doc["handover_path"] = path
    return doc
