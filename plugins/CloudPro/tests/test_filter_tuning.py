"""CloudPro 2.15.0 (Sean, 23 Nov 2026): filters are tuned on the Inspection Area and run on the whole project last.
Changing a setting starts the live preview by itself (unless paused); choosing another group does not; a ready-made
group saved in the job can be updated to the current version from the group menu.

    QT_QPA_PLATFORM=offscreen python3 CloudPro/tests/test_filter_tuning.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
import harness  # noqa: E402

app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
from CloudPro.core import filters as F  # noqa: E402
from CloudPro.ui import filterpanel, theme  # noqa: E402

fp = filterpanel.FilterPanel()
folder = tempfile.mkdtemp()
fp.load(folder, marine=True)
asked = []
fp.previewRequested.connect(lambda: asked.append(1))
fp._fill_groups(1)                                   # another group: no preview
harness.settle()
assert not fp.btn_prev.isChecked() and not asked
fp._toggle_filter(False)                             # an edit: the area preview starts
harness.settle()
assert fp.btn_prev.isChecked() and asked, "editing did not start the preview"
fp.btn_prev.setChecked(False)
fp.btn_pause.setChecked(True)                        # paused: editing does not start it
asked.clear()
fp._toggle_filter(True)
harness.settle()
assert not fp.btn_prev.isChecked() and not asked
print("an edit starts the Inspection Area preview; choosing a group or a paused panel does not")

# a group saved before 2.14.0, updated from the menu (the question answered Update)
old = F.FilterGroup.from_dict({"name": "Marine - standard clean (IHO Order 1a)", "filters": [
    {"key": "reset", "values": {}}, {"key": "m_gates", "values": {"outboard": 65.0, "dmax": 35.0}},
    {"key": "isolated", "values": {}}, {"key": "m_cube", "values": {}}, {"key": "m_outliers", "values": {}},
    {"key": "m_spline", "values": {}}]})
fp.groups[0] = old
fp._fill_groups(0)
theme.msg_choice = lambda *a, **k: 0
filterpanel.theme.msg_choice = theme.msg_choice
fp.update_group()
g = fp.groups[0]
assert [f.key for f in g.filters].count("m_first_bottom") == 1 and g.filters[1].values["outboard"] == 85.0
assert g.filters[1].values["dmax"] == 35.0
assert os.path.exists(os.path.join(folder, "groups.json"))
print("Update to the current version: %s" % "; ".join(fp.updated_text))
print("test_filter_tuning: OK")
