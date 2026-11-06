"""Re-georeferencing never loses points or edits (audit C1, I3, I6).

The new points are built beside the old ones; on an error, a Cancel or a failure while carrying the edits over,
the previous store, edits and settings stay exactly as they were. A successful update carries every edit over
through the on-disk edit spool.

    python3 -m CloudPro.tests.test_rebuild_safety          (from the plugins folder)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np                                                  # noqa: E402

from CloudPro.tests import _job                                     # noqa: E402


def georef(p, dcs, **kw):
    from CloudPro.core import chain, importer as IM
    inp = chain.resolve(dcs, override=p.data.get("override"))
    return IM.import_chain(p, inp, IM.ImportOptions(decimate=4), **kw), inp


def edit_points(p):
    """Reject all of line 2 by hand, classify a third of line 1 as ground."""
    for k in p.store.keys():
        a = p.store.tile_array(k, writable=True)
        a["flag"][a["line"] == 1] |= 0x81
        m = (a["line"] == 0) & (np.arange(len(a)) % 3 == 0)
        a["cls"][m] = 2
        a["flag"][m] |= 0x20
    p.store.sync()


def same_as_before(p, dcs, before, lines_before, inputs_before, crs_before):
    from CloudPro.core.project import Project
    for q in (p, Project.from_dcs(dcs)):             # in memory and as saved on disk
        assert _job.counts(q) == before, (_job.counts(q), before)
        assert len(q.lines) == lines_before
        assert q.data.get("inputs") == inputs_before
        assert q.crs == crs_before
    for d in ("store.new", "lines.new", "edits_pending"):
        assert not os.path.exists(os.path.join(p.work, d)), d
    assert all(os.path.exists(p.track_path(l["id"])) for l in p.lines)


def main():
    from CloudPro.core import chain, importer as IM
    dcs, p = _job.fresh_job("rebuild")
    (out, _), _inp = georef(p, dcs)
    assert len(out) == 2, out
    edit_points(p)
    before = _job.counts(p)
    assert before[1] > 1000 and before[2] > 1000, before
    lines0, inputs0, crs0 = len(p.lines), dict(p.data["inputs"]), p.crs
    print("georeferenced 2 lines, %d points, %d rejected, %d classified" % before)

    # A. PosPro publishes a trajectory that covers line 1 only: refused, nothing changes (was: line 2 and all
    #    its edits deleted)
    tr = _job.realtime_track(dcs.survey_clraw_files())
    sub = tr.subset(p.lines[0]["t0"], p.lines[0]["t1"])
    _job.publish_sbet(dcs, sub)
    try:
        georef(p, dcs, settings={"crs": "EPSG:2961"})
        raise AssertionError("an update that cannot place line 2 must fail")
    except ValueError as ex:
        assert "not covered" in str(ex), ex
    same_as_before(p, dcs, before, lines0, inputs0, crs0)
    print("A. trajectory not covering a georeferenced line: refused, points, edits and CRS kept")

    # B. Cancel during the import of the second line
    os.unlink(os.path.join(dcs.stage_dir("pospro", "out"), "trajectory.json"))     # back to the real-time track
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return len(p.lines) >= 2 if getattr(p, "_staged", None) else False
    try:
        georef(p, dcs, cancel=cancel)
        raise AssertionError("Cancel must raise Cancelled")
    except IM.Cancelled:
        pass
    same_as_before(p, dcs, before, lines0, inputs0, crs0)
    print("B. cancelled while importing: previous points and edits kept")

    # C. a failure while carrying the edits over (after both lines were imported)
    real = IM.apply_edits

    def boom(*a, **k):
        raise RuntimeError("disk full")
    IM.apply_edits = boom
    try:
        georef(p, dcs)
        raise AssertionError("must fail")
    except RuntimeError:
        pass
    finally:
        IM.apply_edits = real
    same_as_before(p, dcs, before, lines0, inputs0, crs0)
    print("C. failure while restoring edits: previous points and edits kept")

    # D. a successful update carries every edit over, and applies the new CRS with the new points
    (out, rest), inp = georef(p, dcs, settings={"crs": "EPSG:2961"})
    after = _job.counts(p)
    assert after == before, (after, before)
    assert rest == before[1] + before[2] - _overlap(p), rest
    assert p.crs == "EPSG:2961" and p.data["inputs"]["crs"] == "EPSG:2961"
    assert p.store.origin[0] < 1e6, p.store.origin           # UTM 20N easting, not NB Stereographic 2.5e6
    assert not chain.georef_state(p, inp), chain.georef_state(p, inp)
    assert not os.path.exists(os.path.join(p.work, "edits_pending"))
    print("D. update: %d edited points carried over, new CRS applied" % rest)

    # E. the edit spool finds exactly the edited keys, bucket by bucket
    sp = IM.EditSpool(os.path.join(p.work, "spool_test"))
    rng = np.random.default_rng(3)
    t = 1.79e9 + np.sort(rng.uniform(0, 120, 200000))
    keys = IM.edit_key(t, rng.integers(0, 32, len(t)), rng.integers(0, 3, len(t)))
    keys = np.unique(keys)
    ed = rng.random(len(keys)) < 0.3
    sp.add(keys[ed][::-1], np.full(int(ed.sum()), 0x81, np.uint8), np.full(int(ed.sum()), 2, np.uint8),
           np.zeros(int(ed.sum()), np.int16))
    sp.finish()
    pos, rec = sp.lookup(keys)
    assert np.array_equal(np.sort(pos), np.flatnonzero(ed)) and np.all(rec["cls"] == 2)
    sp.close()
    print("E. edit spool: %d of %d keys found exactly" % (len(pos), len(keys)))
    print("test_rebuild_safety: OK")


def _overlap(p):
    """Points both rejected and classified (counted once by apply_edits)."""
    n = 0
    for k in p.store.keys():
        a = p.store.tile_array(k)
        n += int((((a["flag"] & 0x80) != 0) & (a["cls"] != 1)).sum())
    return n


if __name__ == "__main__":
    main()
