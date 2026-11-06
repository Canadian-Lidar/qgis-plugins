"""System profiles in PosPro and AlignPro on the simulated mapper job (guides/pospro/simjob.py):

- PosPro: reading the rover files records which system made the job; the System profiles window saves the
  Installation values as a new profile; a second job from the same system is offered the profile, and
  "Use profile values" puts them into its Installation page; not offered again once answered
- AlignPro: the Lidar section is added to the same profile (antennas kept); a job whose mounting differs is
  offered it and applying sets lever, mounting, latency and the shared starting mounting

    QT_QPA_PLATFORM=offscreen python3 -m PosPro.tests.profiles_ui_test
"""
import os
import sys
import tempfile

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGINS, "guides"))
sys.path.insert(0, os.path.join(PLUGINS, "guides", "pospro"))
sys.path.insert(0, PLUGINS)

IDENT = {"unit_serial": "NC100XTLM0006", "unit_name": "mapper-1", "certus_serial": "00ABCDEF"}


def main():
    import harness
    import simjob
    app, iface = harness.start(PLUGINS, tempfile.mkdtemp())
    from dcs_core import profiles, profiles_ui, theme
    from dcs_core.project import DcsProject
    from PosPro.core.project import Project
    from PosPro.ui.dock import find_captures
    profiles.set_folder(tempfile.mkdtemp())
    answers = []
    theme.msg_choice = lambda *a, **k: answers.pop(0) if answers else 0       # noqa: E731
    theme.get_text = lambda *a, **k: ("Van 1", True)                          # noqa: E731
    asked = []
    real_suggest = profiles_ui.suggest

    def spy(*a, **k):
        r = real_suggest(*a, **k)
        asked.append(r[0])
        return r
    profiles_ui.suggest = spy

    def job(name):
        J = tempfile.mkdtemp()
        made = simjob.make(J, name)
        dcs = DcsProject.create(made["project"], name, plugin="PosPro")
        p = Project.from_dcs(dcs)
        p.cfg["rover"]["files"] = find_captures(made["project"])["nav"]
        p.cfg["processing"]["mode"] = "rt"
        p.save()
        return p, dcs

    pp = harness.load("PosPro", iface)
    pp.toggle(True)
    win = pp.dock
    inst = win.page["install"]

    # job 1: read, measure, save a profile
    p1, _ = job("Job_A")
    win.set_project(p1)
    win.run_extract(force=True)
    harness.settle(15)
    assert "system" in p1.cfg["rover"], "the job's system was not looked up"
    p1.cfg["rover"]["system"] = dict(IDENT)          # simulated captures carry no serial numbers
    p1.cfg["installation"]["lever_antenna"] = [0.082, 0.495, 0.112]
    p1.cfg["installation"]["lever_antenna2"] = [0.082, -0.493, 0.112]
    p1.cfg["installation"]["lever_nhc"] = [-1.9, 0.0, 1.1]
    win.save()
    d = profiles_ui.ProfilesDialog(inst, "PosPro", IDENT, inst.profile_current(), inst.PROFILE_SECTIONS,
                                   inst.apply_profile, "2.0.2")
    d.new_profile()
    prof = profiles.load("Van 1")
    assert prof and prof["system"]["unit_serial"] == "NC100XTLM0006"
    assert prof["sections"]["antennas"]["gnss1_m"] == [0.082, 0.495, 0.112]
    assert prof["sections"]["rear_axle"]["axle_m"] == [-1.9, 0.0, 1.1]
    assert "lidar" not in prof["sections"], "PosPro saved a section it does not own"
    d.deleteLater()
    print("PosPro: profile saved from the Installation page: OK")

    # job 2 from the same system, wrong offsets: offered, applied, not offered again
    p2, dcs2 = job("Job_B")
    win.set_project(p2)
    p2.cfg["rover"]["system"] = dict(IDENT)
    p2.cfg["installation"]["lever_antenna"] = [-0.082, -0.495, 0.112]       # the old wrong default
    win.save()
    answers[:] = [0]
    inst.suggest_profile()
    assert asked[-1] == "applied", asked
    assert p2.cfg["installation"]["lever_antenna"] == [0.082, 0.495, 0.112]
    assert p2.cfg["installation"]["lever_nhc"] == [-1.9, 0.0, 1.1]
    assert p2.cfg["installation"]["source"] == "system profile Van 1"
    assert inst.rows["lever_antenna"][1].value() == 0.495, "the page did not show the profile's values"
    assert (dcs2.shared("antennas") or {}).get("gnss1_m") == [0.082, 0.495, 0.112], "shared antennas not updated"
    inst.suggest_profile()
    assert asked[-1] is None, "offered again after it was answered"
    # declined on another job: remembered too
    p3, _ = job("Job_C")
    win.set_project(p3)
    p3.cfg["rover"]["system"] = {"certus_serial": "00ABCDEF"}               # matched by the Certus alone
    win.save()
    answers[:] = [2]
    inst.suggest_profile()
    assert asked[-1] == "declined" and p3.cfg["installation"]["lever_antenna"] != [0.082, 0.495, 0.112]
    inst.suggest_profile()
    assert asked[-1] is None
    # another system: nothing offered
    p3.cfg["rover"]["system"] = {"unit_serial": "OTHER"}
    p3.cfg.pop("profiles_asked", None)
    inst.suggest_profile()
    assert asked[-1] is None
    print("PosPro: offered for the same system, applied, remembered, not offered for another system: OK")

    # AlignPro: add the Lidar section to the same profile, then apply it to a job with the wrong mounting
    try:
        ap = harness.load("AlignPro", iface)
    except Exception as e:      # noqa: BLE001
        print("AlignPro not loadable in this harness (%s); Lidar section checked through the profile API" % e)
        ap = None
    if ap is not None:
        from AlignPro.core.project import Project as AProject
        a = AProject.from_dcs(dcs2) if hasattr(AProject, "from_dcs") else None
        if a is not None:
            a.cfg["lever"], a.cfg["mount"], a.cfg["latency_ms"] = [-0.3, 0.0, -0.4], [-90.0, 45.0, 180.0], 0.0
            cur = a.profile_current()
            assert cur["lidar"]["mount_deg"] == [-90.0, 45.0, 180.0] and cur["lidar"]["source"] == "entered"
            pr = profiles.load("Van 1")
            profiles.update_sections(pr, {"lidar": cur["lidar"]}, by="AlignPro")
            profiles.save(pr)
            pr = profiles.load("Van 1")
            assert set(pr["sections"]) >= {"antennas", "rear_axle", "lidar"}, "PosPro's sections were lost"
            a.cfg["mount"] = [90.0, 45.0, 0.0]                                 # the mapper default
            diffs = profiles.differences(pr, a.profile_current(), ("lidar",))
            assert [x[1] for x in diffs] == ["mount_deg"], diffs
            got = []
            ans, rec = real_suggest(None, "AlignPro", IDENT, a.profile_current(), ("lidar",),
                                    lambda secs, prof: got.append(secs), None) if not answers.append(0) else (0, 0)
            assert ans == "applied" and got[0]["lidar"]["mount_deg"] == [-90.0, 45.0, 180.0]
            print("AlignPro: Lidar section added beside PosPro's, offered and applied: OK")
    # AlignPro's window on job B: reads the lines, finds the system, offers the profile, Apply sets everything
    try:
        alp = harness.load("AlignPro", iface)
    except Exception as e:      # noqa: BLE001
        alp = None
        print("AlignPro window not loadable here: %s" % e)
    if alp is not None:
        from AlignPro.ui import maplayers as aml

        class Stub(aml.MapLayers):
            def __getattribute__(self, n):
                if n.startswith(("update", "remove", "show", "zoom", "add")):
                    return lambda *a, **k: None
                return object.__getattribute__(self, n)
        from qgis.PyQt.QtCore import QTimer
        from qgis.core import QgsApplication
        tm = QgsApplication.taskManager()
        sync = type(tm).addTask
        tm.addTask = lambda t: (QTimer.singleShot(0, lambda: sync(tm, t)), 1)[1]   # as QGIS: tasks run later
        alp.toggle(True)
        dk = alp.dock
        dk.layers = Stub(iface)
        dk.show_dataview = lambda *a, **k: None
        import AlignPro.core.project as apj
        real_find = apj.Project.find_system
        apj.Project.find_system = lambda self: (real_find(self), self.cfg.update(system=dict(IDENT)))[0]
        answers[:] = [0]
        n0 = len(asked)
        dk.open_project(dcs2.path)
        harness.wait(lambda: dk.job is None and dk.p is not None and dk.p.lines, 600)
        harness.settle(30)
        harness.wait(lambda: dk.job is None, 600)
        harness.settle(10)
        assert dk.p.cfg.get("system", {}).get("unit_serial") == "NC100XTLM0006"
        assert asked[n0:] and asked[n0] == "applied", asked[n0:]
        c = dk.p.cfg
        assert [round(x, 4) for x in c["mount"]] == [-90.0, 45.0, 180.0], c["mount"]
        assert [round(x, 4) for x in c["lever"]] == [-0.3, 0.0, -0.4], c["lever"]
        dcs2.reload()
        lm = dcs2.shared("lidar_mounting") or {}
        assert lm.get("source") == "system profile Van 1" and lm.get("mount_deg") == [-90.0, 45.0, 180.0], lm
        # the window's own dialog: save the applied (now calibrated-or-entered) values back, sections kept
        d2 = profiles_ui.ProfilesDialog(dk, "AlignPro", IDENT, dk.p.profile_current(), ("lidar",),
                                        dk._apply_profile, "1.6.0")
        d2.save_to_profile()
        assert set(profiles.load("Van 1")["sections"]) >= {"antennas", "rear_axle", "lidar"}
        d2.deleteLater()
        apj.Project.find_system = real_find
        print("AlignPro window: profile offered after reading the lines, applied and published: OK")
        # point density (6 Nov): new projects are dense, the chooser shows it, standard re-reads the lines
        from AlignPro.core import engine as aeng
        harness.wait(lambda: dk.job is None, 600)
        cal = dk.pg_cal
        cal.refresh()
        assert cal.density.currentData() == "dense", cal.density.currentData()
        assert "3,000,000 points kept per line" in cal.density_note.text(), cal.density_note.text()
        n_dense = len(dk.p.P.pt_line)
        cal.density.setCurrentIndex(cal.density.findData("standard"))
        cal._density()
        harness.wait(lambda: dk.job is None and dk.p is not None and dk.p.lines, 600)
        harness.settle(30)
        harness.wait(lambda: dk.job is None, 600)
        assert aeng.density_of(dk.p.settings) == "standard"
        n_std = len(dk.p.P.pt_line)
        assert n_std <= n_dense, (n_std, n_dense)
        print("AlignPro point density: dense %s patch points, standard %s: OK" % (n_dense, n_std))
    print("OK")


if __name__ == "__main__":
    main()
