"""The guided workflow strip under CloudPro's header: the job's three steps with their state and
one Next button that does the next thing.

    [✓ 1 GEOREFERENCE] [○ 2 CLEAN & CLASSIFY] [○ 3 PUBLISH]   Georeference is done: 3 lines.   [NEXT: CLEAN & CLASSIFY ›]

CloudPro is an editor, not a list of pages, so the steps are actions: Georeference (or Update when
the inputs changed), Clean & classify (opens the Automatic filters tab; optional, as long as you
like), Publish for the job. Clicking a step does it too. The wording comes from dcs_core.stepnav,
as in PosPro's and AlignPro's Back / Next bar.
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

STEPS = (("georef", "Georeference"), ("clean", "Clean & classify"), ("publish", "Publish"))
_MARK = {"done": "✓", "warn": "⚠", "error": "✕", "todo": "○", "running": "…"}


def states(project, n_edits, visited_clean, quick_state=None):
    """{step: (state, reason)} for an open CloudPro project. quick_state(p) -> [reasons the inputs
    changed]; n_edits = filter runs + manual edits so far."""
    p = project
    out = {}
    why = (quick_state(p) if (quick_state and p.dcs is not None) else []) or []
    if not p.lines:
        out["georef"] = ("todo", "press NEXT: GEOREFERENCE (or MENU \u203a Georeference)")
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
        out["publish"] = ("todo", "an older stand-alone project: NEXT opens EXPORT")
    else:
        try:
            pst, pwhy = p.dcs.state("cloudpro")
        except Exception:           # noqa: BLE001
            pst, pwhy = "none", []
        if pst == "done":
            out["publish"] = ("done", "published for the job")
        elif pst == "stale":
            out["publish"] = ("warn", "out of date (%s): publish again" % "; ".join(pwhy))
        elif pst == "running":
            out["publish"] = ("running", "")
        else:
            out["publish"] = ("todo", "press NEXT: PUBLISH (or MENU \u203a Publish for the job)")
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
        return "publish", "NEXT: PUBLISH" if st["publish"][0] == "todo" else "PUBLISH AGAIN"
    return None, ""


class WorkflowStrip(QWidget):
    """actions: {"georef": fn, "clean": fn, "publish": fn}."""

    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.setObjectName("cpWorkflow")
        self.actions = actions
        self.visited_clean = False
        self._next = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)
        self.chips = {}
        for i, (k, t) in enumerate(STEPS):
            b = QPushButton("%d %s" % (i + 1, t.upper().replace("&", "&&")))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, key=k: self._do(key))
            lay.addWidget(b)
            self.chips[k] = b
        self.msg = QLabel("")
        self.msg.setObjectName("ppVal")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg, 1)
        self.next = QPushButton("NEXT  ›")
        self.next.setObjectName("ppPrimary")
        self.next.setMinimumWidth(200)
        self.next.clicked.connect(lambda: self._next and self._do(self._next))
        lay.addWidget(self.next)

    def _do(self, key):
        if key == "clean":
            self.visited_clean = True
        fn = self.actions.get(key)
        if fn:
            fn()

    def show_states(self, st):
        from dcs_core.stepnav import advice
        for i, (k, t) in enumerate(STEPS):
            s, why = st[k]
            b = self.chips[k]
            b.setText("%s  %d %s" % (_MARK.get(s, ""), i + 1, t.upper().replace("&", "&&")))
            b.setToolTip("%s: %s" % (t, why) if why else t)
        key, label = next_step(st, self.visited_clean)
        if key is None:
            text, level = "\u2713 The job's point cloud is published. Edit more and publish again " \
                          "whenever you need.", "ok"
            self.next.setVisible(False)
        else:
            title = dict(STEPS)[key]
            s, why = st[key]
            text, level, _ok = advice(title, s, why, optional=(key == "clean"))
            self.next.setText(label.replace("&", "&&") + "  ›")
            self.next.setVisible(True)
            self.next.setEnabled(s != "running")
            self._next = key
            if key == "clean":
                g = st["georef"]
                text = "✓ Georeference is done: %s. Next, clean and classify (optional), then publish." % g[1]
        self.msg.setText(text)
        self.msg.setToolTip(text)
        self.msg.setProperty("level", level)
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)
