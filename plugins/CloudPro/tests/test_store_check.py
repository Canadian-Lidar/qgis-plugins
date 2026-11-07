"""The point store's check on opening (CloudPro 1.8.0): after a crash or a full disk while writing, a part
record at the end of a tile is cut off, records written but not yet counted are taken into the index, a tile
whose file is gone is dropped (and reported), and the store is consistent afterwards; a clean store reports
nothing.

    python3 -m CloudPro.tests.test_store_check          (from the plugins folder)
"""
import os
import sys

PLUGINS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PLUGINS)

import numpy as np                                                  # noqa: E402

from CloudPro.tests import _job                                     # noqa: E402


def main():
    from CloudPro.core import chain, importer as IM
    from CloudPro.core.project import Project
    from CloudPro.core.store import REC
    dcs, p = _job.fresh_job("check")
    IM.import_chain(p, chain.resolve(dcs), IM.ImportOptions())
    p.save()
    st = p.store
    assert st.check() == [], "a clean store reported findings"
    keys = st.keys()
    counts = dict(st.counts)
    st.close()
    with open(st.tile_path(keys[0]), "ab") as f:
        f.write(b"\0" * 10)                                         # a part record
    a = np.fromfile(st.tile_path(keys[1]), REC)
    np.concatenate([a, a[:5]]).tofile(st.tile_path(keys[1]))        # written, not yet counted
    os.remove(st.tile_path(keys[2]))                                # gone
    p2 = Project.from_dcs(dcs)
    f = p2.store_findings
    assert len(f) == 3 and "part point" in f[0] and "in the index" in f[1] and "missing" in f[2], f
    s2 = p2.store
    assert s2.counts[keys[0]] == counts[keys[0]]
    assert s2.counts[keys[1]] == counts[keys[1]] + 5
    assert s2.counts[keys[2]] == 0
    assert s2.check() == [], "still inconsistent after the repair"
    assert len(s2.tile_array(keys[0])) == counts[keys[0]]
    print("store check: %d findings repaired, consistent afterwards: OK" % len(f))


if __name__ == "__main__":
    main()
    print("test_store_check: OK")
