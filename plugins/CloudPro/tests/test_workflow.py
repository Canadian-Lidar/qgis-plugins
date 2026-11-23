"""CloudPro 2.15.0: steps Georeference › Check › Clean & classify › Finalize (Sean, 23 Nov 2026: check the line
separation before cleaning, so an AlignPro / StripPro fix does not mean cleaning again).

    python3 CloudPro/tests/test_workflow.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from CloudPro.ui.workflow import STEPS, next_step, states  # noqa: E402


class D:
    def state(self, _s):
        return "none", []


class P:
    def __init__(self, n):
        self.lines = [{}] * n
        self.dcs = D()


assert [k for k, _ in STEPS] == ["georef", "check", "clean", "publish"]
st = states(P(0), 0, False)
assert next_step(st, False)[0] == "georef"
st = states(P(3), 0, False, sep=None)                                  # georeferenced, not checked
assert st["check"][0] == "todo" and next_step(st, False) == ("check", "NEXT: CHECK")
st = states(P(3), 0, False, sep=(True, True, 0, 0))                    # checked, the lines agree
assert st["check"][0] == "done" and next_step(st, False)[0] == "clean"
st = states(P(3), 0, False, sep=(True, True, 4, 1))                    # findings: shown, cleaning not blocked
assert st["check"][0] == "warn" and "4 problem areas" in st["check"][1] and next_step(st, False)[0] == "clean"
st = states(P(3), 0, False, sep=(True, False, 0, 0))                   # georeferenced again since
assert next_step(st, False) == ("check", "CHECK AGAIN")
st = states(P(1), 0, False)                                            # one line: nothing to compare
assert st["check"][0] == "done" and next_step(st, False)[0] == "clean"
st = states(P(3), 2, True, sep=(True, True, 0, 0))
assert next_step(st, True) == ("publish", "NEXT: FINALIZE")
print("test_workflow: OK")
