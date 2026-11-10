"""CloudPro's workflow, shown as the header's step breadcrumbs (CloudPro 2.0.0):

    GEOREFERENCE  ›  CLEAN & CLASSIFY  ›  FINALIZE

CloudPro is an editor, not a list of pages, so the steps are actions: Georeference (or Update when
the inputs changed), Clean & classify (opens the Automatic filters tab; optional, as long as you
like), Finalize for the job. Each step is coloured by its state, the next one to do is marked, its
reason is the tooltip, and clicking a step does it.
"""
STEPS = (("georef", "Georeference"), ("clean", "Clean & classify"), ("publish", "Finalize"))


def states(project, n_edits, visited_clean, quick_state=None):
    """{step: (state, reason)} for an open CloudPro project. quick_state(p) -> [reasons the inputs
    changed]; n_edits = filter runs + manual edits so far."""
    p = project
    out = {}
    why = (quick_state(p) if (quick_state and p.dcs is not None) else []) or []
    if not p.lines:
        out["georef"] = ("todo", "click GEOREFERENCE (or project menu \u203a Georeference)")
    elif why:
        out["georef"] = ("warn", "inputs changed (%s): update" % "; ".join(why))
    else:
        out["georef"] = ("done", "%d line%s" % (len(p.lines), "" if len(p.lines) == 1 else "s"))
    if not p.lines:
        out["clean"] = ("todo", "after georeferencing")
    elif n_edits:
        out["clean"] = ("done", "%d filter run%s and edits" % (n_edits, "" if n_edits == 1 else "s"))
    elif visited_clean:
        out["clean"] = ("todo", "optional: run a filter group, or skip")
    else:
        out["clean"] = ("todo", "optional: automatic filters and manual edits")
    if p.dcs is None:
        out["publish"] = ("todo", "an older stand-alone project: FINALIZE opens Export")
    else:
        try:
            pst, pwhy = p.dcs.state("cloudpro")
        except Exception:           # noqa: BLE001
            pst, pwhy = "none", []
        if pst == "done":
            out["publish"] = ("done", "finalized for the job")
        elif pst == "stale":
            out["publish"] = ("warn", "out of date (%s): finalize again" % "; ".join(pwhy))
        elif pst == "running":
            out["publish"] = ("running", "")
        else:
            out["publish"] = ("todo", "click FINALIZE (or project menu \u203a Finalize for the job)")
    return out


def next_step(st, visited_clean):
    """(step key, button label) for the Next button, or (None, "") when the job is finished."""
    g = st["georef"][0]
    if g == "todo":
        return "georef", "NEXT: GEOREFERENCE"
    if g == "warn":
        return "georef", "UPDATE GEOREFERENCING"
    if st["clean"][0] != "done" and not visited_clean:
        return "clean", "NEXT: CLEAN & CLASSIFY"
    if st["publish"][0] in ("todo", "warn"):
        return "publish", "NEXT: FINALIZE" if st["publish"][0] == "todo" else "FINALIZE AGAIN"
    return None, ""
