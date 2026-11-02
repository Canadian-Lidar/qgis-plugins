"""New / open project dialogs shared by every DCS plugin (dark theme)."""
import os

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QVBoxLayout)

from . import theme
from .project import SUFFIX, DcsProject, ProjectError

KEY = "DCS/last_folder"


class NewProjectDialog(QDialog):
    """Job folder (the folder holding the mapper captures), project name and
    where the Processing folder goes (default: inside the job folder)."""

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
        h = QLabel("Choose the job folder that holds the mapper captures. PosPro, AlignPro and CloudPro then share "
                   "one project in Processing/<name>/ there; the raw data is never changed.")
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
        self.proc.setPlaceholderText("inside the job folder (Processing/)")
        bp = QPushButton("Elsewhere\u2026")
        bp.setToolTip("Put the Processing folder on another drive, e.g. a fast local disk when the captures are on "
                      "a USB drive or a network share.")
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
        self.show_where()
        self.resize(640, 260)
        theme.polish(self)

    def pick_job(self):
        d = QFileDialog.getExistingDirectory(self, "Job folder (holding the mapper captures)", self.job.text())
        if d:
            self.job.setText(d)
            if not self.name.text().strip():
                self.name.setText(os.path.basename(d.rstrip("/\\")))

    def pick_proc(self):
        d = QFileDialog.getExistingDirectory(self, "Where the Processing folder goes", self.job.text())
        if d:
            self.proc.setText(d)

    def _root(self):
        job = self.job.text().strip()
        return self.proc.text().strip() or (os.path.join(job, "Processing") if job else "")

    def show_where(self):
        n = self.name.text().strip()
        root = self._root()
        self.where.setText("Project folder: %s" % os.path.join(root, n) if root and n else "")

    def create(self):
        try:
            self.project = DcsProject.create(self.job.text().strip(), self.name.text(),
                                             self.proc.text().strip() or None, plugin=self.plugin)
        except (ProjectError, OSError) as e:
            theme.msg_warn(self, "New project", str(e))
            return
        QSettings().setValue(KEY, self.job.text().strip())
        self.accept()


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
