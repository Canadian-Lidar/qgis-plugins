"""Back / Next under a plugin's step pages, so the user always knows whether a step is finished
and what comes next.

    [‹ BACK: BASE]   ● Installation is done: antenna 0.08, 0.49, 0.11 m.   [NEXT: PROCESSING ›]

Each step page already reports (state, short reason) for its sidebar button: done, warn, error,
todo or running. StepNav turns that into one sentence and a Next button:

- done                 "<Step> is done: <reason>."                    Next (green) enabled
- warn                 "<Step>: <reason>. Check it, or continue."     Next enabled
- error                "<Step>: <reason>. Fix this to continue."      Next disabled (required steps)
- todo, required       "<Step> is not done yet: <reason>."            Next disabled
- todo, optional       "Optional: <reason>. You can skip it."         "SKIP TO <NEXT> ›" enabled
- running              "<Step> is running…"                           Next disabled

On the last step Next becomes "CONTINUE IN <PLUGIN> ›" (set_continue), which opens the next
plugin of the chain on the same project (registry.open), once this plugin's results are published.
The sidebar step buttons still work as before: Next only guides, it never locks a page.

Plugin code (PosPro, AlignPro):
    self.nav = StepNav(titles, self.goto_page, optional={"Review", "Calibration"})
    ... content.addWidget(self.nav)
    self.nav.update(index, state, reason)                       # from goto_page / update_steps
    self.nav.set_continue("alignpro", ready, why, project_path)  # for the last step
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from . import registry

NEXT_PLUGIN = {"pospro": ("alignpro", "AlignPro"), "alignpro": ("cloudpro", "CloudPro")}
_MARK = {"done": "✓", "warn": "⚠", "error": "✕", "todo": "○", "running": "…"}
_LEVEL = {"done": "ok", "warn": "warn", "error": "bad", "todo": "", "running": ""}


def advice(title, state, reason, optional=False, last=False):
    """(sentence, level, next_enabled) for a step in a state. Pure, for tests and for plugins
    that lay out their own guidance (CloudPro)."""
    reason = (reason or "").strip()
    tail = (": " + reason) if reason else ""
    if state == "running":
        return "%s %s is running…" % (_MARK["running"], title), "", False
    if state == "done":
        return "%s %s is done%s." % (_MARK["done"], title, tail), "ok", True
    if state == "warn":
        return ("%s %s%s. Check it, or continue." % (_MARK["warn"], title, tail)), "warn", True
    if state == "error":
        if optional:
            return "%s %s%s. You can skip it." % (_MARK["error"], title, tail), "bad", True
        return "%s %s%s. Fix this to continue." % (_MARK["error"], title, tail), "bad", False
    # todo
    if optional or last:
        more = "" if "skip" in reason.lower() else " You can skip it."
        return "%s Optional%s.%s" % (_MARK["todo"], tail, more), "", True
    return "%s %s is not done yet%s." % (_MARK["todo"], title, tail), "", False


class StepNav(QWidget):
    """titles: the step names in order; goto(k): show step k; optional: titles that may be
    skipped. stage: this plugin's stage key, for "Continue in <next plugin>" on the last step."""

    def __init__(self, titles, goto, optional=(), stage=None, parent=None):
        super().__init__(parent)
        self.setObjectName("ppStepNav")
        self.titles = list(titles)
        self.goto = goto
        self.optional = set(optional)
        self.stage = stage
        self.index = 0
        self._cont = None              # (stage, ready, why, project_path)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 2)
        lay.setSpacing(10)
        self.back = QPushButton("‹  BACK")
        self.back.setObjectName("ppNavBack")
        self.back.clicked.connect(self._back)
        self.msg = QLabel("")
        self.msg.setObjectName("ppVal")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.next = QPushButton("NEXT  ›")
        self.next.setObjectName("ppPrimary")
        self.next.setMinimumWidth(190)
        self.next.clicked.connect(self._next)
        lay.addWidget(self.back)
        lay.addWidget(self.msg, 1)
        lay.addWidget(self.next)

    # ------------------------------------------------------------------ state
    def is_last(self, k=None):
        return (self.index if k is None else k) >= len(self.titles) - 1

    def update(self, index, state, reason="", todo=None):   # noqa: D401 - Qt name clash is harmless here
        """Show step index in its state (done / warn / error / todo / running) with its reason.
        todo: what to do to finish the step, shown instead of the reason while it is not done
        (e.g. "press READ"); the reason itself is what the sidebar shows."""
        if todo and state in ("todo", "error"):
            reason = todo
        self.index = max(0, min(int(index), len(self.titles) - 1))
        k, title = self.index, self.titles[self.index]
        last = self.is_last()
        text, level, ok = advice(title, state, reason, optional=title in self.optional, last=last)
        self._text, self._level = text, level
        self.back.setVisible(k > 0)
        if k > 0:
            self.back.setText("‹  BACK: %s" % self.titles[k - 1].upper())
        if last:
            self._show_continue(text, level)
            return
        nxt = self.titles[k + 1].upper()
        skip = state in ("todo", "error") and ok
        self.next.setText(("SKIP TO %s  ›" if skip else "NEXT: %s  ›") % nxt)
        self.next.setEnabled(ok)
        self.next.setVisible(True)
        self.next.setToolTip("" if ok else "Finish %s first (the step list on the left still opens any step)"
                             % title)
        self._set_msg(text, level)

    def set_continue(self, stage, ready, why="", project_path=None):
        """For the last step: open the next plugin (stage key) on this project when ready."""
        self._cont = (stage, bool(ready), why or "", project_path)
        if self.is_last():
            self._show_continue(getattr(self, "_text", ""), getattr(self, "_level", ""))

    def _show_continue(self, text, level):
        nxt = NEXT_PLUGIN.get(self.stage)
        if not nxt and not self._cont:
            self.next.setVisible(False)
            self._set_msg(text, level)
            return
        stage, ready, why, path = self._cont or (nxt[0], False, "", None)
        label = {s: n for s, n in NEXT_PLUGIN.values()}.get(stage, stage)
        self.next.setVisible(True)
        self.next.setText("CONTINUE IN %s  ›" % label.upper())
        can = ready and path is not None and registry.has(stage)
        self.next.setEnabled(can)
        if not registry.has(stage):
            hint = "%s is not installed or not enabled." % label
        elif not ready:
            hint = why or "Publish this step's results first."
        else:
            hint = "Results are published: continue in %s." % label
        self.next.setToolTip(hint)
        self._set_msg((text + "  " + hint).strip(), level if ready else (level or "warn"))

    def _set_msg(self, text, level):
        self.msg.setText(text)
        self.msg.setToolTip(text)
        self.msg.setProperty("level", level)
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)

    # ------------------------------------------------------------------ actions
    def _back(self):
        if self.index > 0:
            self.goto(self.index - 1)

    def _next(self):
        if not self.is_last():
            self.goto(self.index + 1)
        elif self._cont and self._cont[1] and self._cont[3]:
            registry.open(self._cont[0], self._cont[3])
