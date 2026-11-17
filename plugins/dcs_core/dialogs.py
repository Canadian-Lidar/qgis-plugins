"""New / open project dialogs shared by every DCS plugin (dark theme)."""
import json
import os
import re

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QVBoxLayout)

from . import theme
from .project import SUFFIX, DcsProject, ProjectError

KEY = "DCS/last_folder"


class NewProjectDialog(QDialog):
    """Job folder (the mapper's project folder, holding its day folders), project name and where the
    project goes (default: the job folder itself: <job>/<name>.dcsproj and <job>/<name>/, layout 2)."""

    def __init__(self, parent=None, plugin="DCS", default_name=""):
        super().__init__(parent)
        self.plugin = plugin
        self.project = None
        self.setWindowTitle("New DCS project")
        theme.apply(self)
        v = QVBoxLayout(self)
        t = QLabel("New processing project")
        t.setObjectName("ppTitle")
        v.addWidget(t)
        h = QLabel("Choose the job folder: the mapper's project folder, the one holding project.json and the "
                   "day folders. The plugins then share one project, <name>.dcsproj and the folder <name>/ "
                   "beside the day folders; the raw data is never changed.")
        h.setObjectName("ppHint")
        h.setWordWrap(True)
        v.addWidget(h)
        f = QFormLayout()
        self.job = QLineEdit(QSettings().value(KEY, ""))
        bj = QPushButton("Browse\u2026")
        bj.clicked.connect(self.pick_job)
        r1 = QHBoxLayout()
        r1.addWidget(self.job, 1)
        r1.addWidget(bj)
        f.addRow("Job folder", r1)
        self.name = QLineEdit(default_name)
        self.name.setPlaceholderText("e.g. SaintJohn_Oct11")
        f.addRow("Project name", self.name)
        self.proc = QLineEdit("")
        self.proc.setPlaceholderText("the job folder")
        bp = QPushButton("Elsewhere\u2026")
        bp.setToolTip("Put the project on another drive, e.g. a fast local disk when the captures are on a USB "
                      "drive or a network share.")
        bp.clicked.connect(self.pick_proc)
        r2 = QHBoxLayout()
        r2.addWidget(self.proc, 1)
        r2.addWidget(bp)
        f.addRow("Processing folder", r2)
        v.addLayout(f)
        self.where = QLabel("")
        self.where.setObjectName("ppHint")
        self.where.setWordWrap(True)
        v.addWidget(self.where)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.create)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        for e in (self.job, self.name, self.proc):
            e.textChanged.connect(self.show_where)
        self._auto = None
        if not default_name:
            self._suggest_name()
        self.job.editingFinished.connect(self._suggest_name)
        self.show_where()
        self.resize(640, 260)
        theme.polish(self)

    def pick_job(self):
        d = QFileDialog.getExistingDirectory(self, "Job folder (holding the mapper captures)", self.job.text())
        if d:
            root, _name = mapper_project(d)
            self.job.setText(root)
            self._suggest_name()

    def _suggest_name(self):
        """<first capture date>_1 (the next free number), unless the user typed a name of their own."""
        job = self.job.text().strip()
        cur = self.name.text().strip()
        if not job or (cur and cur != getattr(self, "_auto", None)):
            return
        self._auto = default_project_name(job, self._root())
        self.name.setText(self._auto)

    def pick_proc(self):
        d = QFileDialog.getExistingDirectory(self, "Where the project goes", self.job.text())
        if d:
            self.proc.setText(d)

    def _root(self):
        job = self.job.text().strip()
        return self.proc.text().strip() or job

    def show_where(self):
        n = self.name.text().strip()
        root = self._root()
        self.where.setText("Project: %s" % os.path.join(root, n + SUFFIX) if root and n else "")

    def create(self):
        job = self.job.text().strip()
        if job:
            job = mapper_project(job)[0]          # typed or pasted: inside a mapper project, use its folder
            self.job.setText(job)
        try:
            self.project = DcsProject.create(job, self.name.text(),
                                             self.proc.text().strip() or None, plugin=self.plugin)
        except (ProjectError, OSError) as e:
            theme.msg_warn(self, "New project", str(e))
            return
        QSettings().setValue(KEY, self.job.text().strip())
        self.accept()


_DATE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")


def first_capture_date(job):
    """The date of the first capture in a job folder (YYYY-MM-DD), from the capture and line folder names
    (layout 2: the day folders YYYYMMDD; mapper 2.10: lines/<P>_L001_YYYYMMDD_HHMMSS, nav/<P>_NAV_YYYYMMDD_...;
    older: YYYYMMDD_HHMMSS_<name>), else the mapper project's created date, else None."""
    dates = []
    for sub in ("lines", "nav", ""):
        d = os.path.join(job, sub) if sub else job
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            m = _DATE.search(n)
            if m:
                dates.append("%s-%s-%s" % m.groups())
        if dates:
            break
    if dates:
        return min(dates)
    try:
        with open(os.path.join(job, "project.json"), encoding="utf-8") as f:
            c = str(json.load(f).get("created") or "")
        if re.match(r"\d{4}-\d{2}-\d{2}", c):
            return c[:10]
    except (OSError, ValueError):
        pass
    return None


def default_project_name(job, proc_root=None):
    """<first capture date>_<n>, n = the first number whose project folder does not exist yet (Sean, 6 Nov 2026:
    a job can have several processing projects, e.g. other bases or settings). proc_root: where the project
    goes (default: the job folder)."""
    import datetime
    base = first_capture_date(job) or datetime.date.today().isoformat()
    root = proc_root or job
    n = 1
    while any(os.path.exists(os.path.join(r, "%s_%d" % (base, n)) + x)
              for r in (root, os.path.join(job, "Processing")) for x in ("", SUFFIX)):
        n += 1
    return "%s_%d" % (base, n)


def mapper_project(folder):
    """A folder chosen as job folder -> (job folder, project name). Inside a mapper project (a day folder or one
    of its NAV/ LIDAR/ CLRAW/ ... folders, layout 2; mapper 2.10: its lines/ or nav/ folder, or a line folder) the
    job folder is the mapper project's folder, the one holding project.json, and the name is its id (the
    project's folder name, safe for file names)."""
    d = os.path.abspath(folder)
    for _ in range(4):
        pj = os.path.join(d, "project.json")
        if os.path.isfile(pj) and (os.path.isdir(os.path.join(d, "lines")) or _has_day_folder(d)):
            try:
                with open(pj, encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                info = {}
            return d, str(info.get("id") or os.path.basename(d))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.abspath(folder), None


_DAY = re.compile(r"^20\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")


def _has_day_folder(d):
    """True when d holds a mapper day folder (YYYYMMDD, layout 2)."""
    try:
        return any(_DAY.match(n) and os.path.isdir(os.path.join(d, n)) for n in os.listdir(d))
    except OSError:
        return False


def new_project(parent=None, plugin="DCS", default_name=""):
    d = NewProjectDialog(parent, plugin, default_name)
    return d.project if d.exec() == QDialog.DialogCode.Accepted else None


def pick_project_file(parent=None, extra_filters=""):
    f, _ = QFileDialog.getOpenFileName(parent, "Open project", QSettings().value(KEY, ""),
                                       "DCS project (*%s)%s;;All files (*)" % (SUFFIX, extra_filters))
    return f or None


def check_raw(parent, project, plugin="DCS"):
    """When captures are missing (drive letter changed, folder moved), ask
    for the job folder once and relocate. Returns True when all are found."""
    missing = project.missing_captures()
    if not missing:
        return True
    r = theme.msg_question(parent, "Raw data not found",
                           "%d raw file(s) of this project are not where they were, e.g.\n%s\n\nPoint to the job "
                           "folder's new location?" % (len(missing), missing[0]))
    from qgis.PyQt.QtWidgets import QMessageBox
    if r != QMessageBox.StandardButton.Yes:
        return False
    d = QFileDialog.getExistingDirectory(parent, "New location of the job folder", "")
    if not d:
        return False
    still = project.relocate(d, plugin)
    if still:
        theme.msg_warn(parent, "Raw data not found", "Not found under %s:\n%s" % (d, "\n".join(still[:5])))
        return False
    return True
