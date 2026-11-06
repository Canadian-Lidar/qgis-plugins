"""Edits with undo/redo. Every edit is written to the tile store at once
(memory-mapped) and recorded as (record codes, old values, new values)."""
import numpy as np

from .store import REJ_AUTO, REJ_MANUAL, REJECTED, ZMOD, CLSMOD

MAX_UNDO_POINTS = 60_000_000


class Action:
    __slots__ = ("label", "codes", "old", "new")

    def __init__(self, label, codes, old, new):
        self.label, self.codes, self.old, self.new = label, codes, old, new

    @property
    def size(self):
        return len(self.codes)


class EditManager:
    def __init__(self, project, on_change=None):
        self.project = project
        self.store = project.store
        self.undo_stack = []
        self.redo_stack = []
        self.on_change = on_change
        self.ps = None                # the loaded working set, kept in sync

    # -- helpers -------------------------------------------------------------
    def _write(self, codes, vals):
        fields = {}
        if "flag" in vals:
            fields["flag"] = vals["flag"]
        if "cls" in vals:
            fields["cls"] = vals["cls"]
        if "dz" in vals:
            fields["dz"] = vals["dz"]
            fields["z"] = vals["z"]
        self.store.write_codes(codes, **fields)
        ps = self.ps
        if ps is not None and len(ps):
            pos = ps.find(codes)
            hit = pos >= 0
            p = pos[hit]
            if "flag" in vals:
                ps.flag[p] = vals["flag"][hit]
            if "cls" in vals:
                ps.cls[p] = vals["cls"][hit]
            if "dz" in vals:
                ps.dz[p] = vals["dz"][hit]
                ps.z[p] = vals["z"][hit] * 1e-3

    def _push(self, act):
        if act.size == 0:
            return None
        self.undo_stack.append(act)
        self.redo_stack.clear()
        total = sum(a.size for a in self.undo_stack)
        while total > MAX_UNDO_POINTS and len(self.undo_stack) > 1:
            total -= self.undo_stack.pop(0).size
        self.project.log("%s (%d points)" % (act.label, act.size))
        if self.on_change:
            self.on_change(act)
        return act

    # -- edits on the working set ------------------------------------------------
    def apply(self, idx, op, value=None, label=None):
        """Edit points idx (positions in the working set).
        op: 'reject' | 'accept' | 'class' | 'shift' (value in m)"""
        ps = self.ps
        idx = np.asarray(idx, np.int64)
        if len(idx) == 0:
            return None
        old_flag = ps.flag[idx].copy()
        if op == "reject":
            m = (old_flag & REJECTED) == 0
            idx = idx[m]
            old = {"flag": old_flag[m]}
            new = {"flag": (old_flag[m] | REJECTED | REJ_MANUAL).astype(np.uint8)}
        elif op == "accept":
            m = (old_flag & REJECTED) != 0
            idx = idx[m]
            old = {"flag": old_flag[m]}
            new = {"flag": (old_flag[m] & ~np.uint8(REJECTED)).astype(np.uint8)}
        elif op == "class":
            m = ps.cls[idx] != value
            idx = idx[m]
            old = {"cls": ps.cls[idx].copy(), "flag": ps.flag[idx].copy()}
            new = {"cls": np.full(len(idx), value, np.uint8), "flag": (ps.flag[idx] | CLSMOD).astype(np.uint8)}
        elif op == "shift":
            dzmm = int(round(value * 1000))
            oz = np.round(ps.z[idx] * 1000).astype(np.int32)
            old = {"dz": ps.dz[idx].copy(), "z": oz, "flag": ps.flag[idx].copy()}
            odz = ps.dz[idx].astype(np.int32)
            ndz = np.clip(odz + dzmm, -32768, 32767)
            # z moves by exactly the change kept in dz, so "original z = z - dz" always holds
            new = {"dz": ndz.astype(np.int16), "z": (oz + (ndz - odz)).astype(np.int32),
                   "flag": (ps.flag[idx] | ZMOD).astype(np.uint8)}
        else:
            raise ValueError(op)
        if len(idx) == 0:
            return None
        codes = ps.code[idx]
        act = Action(label or {"reject": "Manual reject", "accept": "Manual accept", "class": "Classify %s" % value,
                               "shift": "Shift height %+.3f m" % (value or 0)}[op], codes, old, new)
        self._write(codes, new)
        return self._push(act)

    def apply_ctx(self, ctx, label, ps=None, scope=None, write=True):
        """Commit a filter preview: the differences between ctx and the set. write=False only builds the
        Action (the tiled runner writes every tile's changes after all tiles were computed)."""
        ps = ps or self.ps
        f_new = ctx.flag
        changed = (ps.flag != f_new) | (ps.cls != ctx.cls)
        dz_new = np.clip(np.round(ctx.dz * 1000), -32768, 32767).astype(np.int32)
        zch = ps.dz.astype(np.int32) != dz_new
        changed |= zch
        if scope is not None:
            changed &= scope
        idx = np.flatnonzero(changed)
        if len(idx) == 0:
            return None
        flag_new = f_new[idx].copy()
        flag_new[(ps.cls[idx] != ctx.cls[idx])] |= CLSMOD
        flag_new[zch[idx]] |= ZMOD
        oz = np.round(ps.z[idx] * 1000).astype(np.int32)
        nz = (oz + (dz_new[idx] - ps.dz[idx].astype(np.int32))).astype(np.int32)
        old = {"flag": ps.flag[idx].copy(), "cls": ps.cls[idx].copy(), "dz": ps.dz[idx].copy(), "z": oz}
        new = {"flag": flag_new.astype(np.uint8), "cls": ctx.cls[idx].astype(np.uint8),
               "dz": dz_new[idx].astype(np.int16), "z": nz}
        codes = ps.code[idx]
        act = Action(label, codes, old, new)
        if not write:
            return act
        if ps is self.ps:
            self._write(codes, new)
        else:
            self.store.write_codes(codes, flag=new["flag"], cls=new["cls"], dz=new["dz"], z=new["z"])
            if self.ps is not None:
                self._write(codes, new)       # keep the view in sync if those points are loaded
        return act

    def commit(self, act, undoable=True):
        if act is None:
            return None
        if undoable:
            return self._push(act)
        self.project.log("%s (%d points, not undoable)" % (act.label, act.size))
        if self.on_change:
            self.on_change(act)
        return act

    # -- undo / redo -------------------------------------------------------------
    def can_undo(self):
        return bool(self.undo_stack)

    def can_redo(self):
        return bool(self.redo_stack)

    def undo(self):
        if not self.undo_stack:
            return None
        a = self.undo_stack.pop()
        self._write(a.codes, a.old)
        self.redo_stack.append(a)
        self.project.log("Undo: " + a.label)
        if self.on_change:
            self.on_change(a)
        return a

    def redo(self):
        if not self.redo_stack:
            return None
        a = self.redo_stack.pop()
        self._write(a.codes, a.new)
        self.undo_stack.append(a)
        self.project.log("Redo: " + a.label)
        if self.on_change:
            self.on_change(a)
        return a
