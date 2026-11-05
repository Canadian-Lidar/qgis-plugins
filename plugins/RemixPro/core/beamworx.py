"""BeamworX .bwxraw files from one survey line's .clraw files.

One .bwxraw per .clraw, the same base name, with the survey configuration .xml beside it, laid out as
NavAQ writes them: navigation from 3 s before the start of each file (run-in) to 3 s after its end
(run-out). This is the mapper's clraw.to_bwxraw (2.10.0) with the reading done by DCS Core: the records
written are the same, byte for byte (tests/test_same_as_mapper.py).

A damaged .clraw chunk costs that chunk only; the count is returned. Navigation recordings have no Lidar
data and are never converted (sources.py does not offer them).
"""
import json
import os

from dcs_core import clraw as dclraw

from . import bwxraw, surveyconfig

NS = 1000000000
CONFIG_NAME = "beamworx_survey_config.xml"
SENSOR = "Hesai Laser Scanner"


class Cancelled(Exception):
    """The user pressed Cancel."""


class BeamworxError(ValueError):
    pass


def _bytes(p):
    return p.tobytes() if isinstance(p, memoryview) else bytes(p)


def recording_times(files):
    """(start_ns, stop_ns) of a recording from the recorder's own events (None when absent): where the
    recorded data begins and ends; the run-in and run-out lie outside."""
    start = stop = None
    for cf in files:
        for _ch, lt, _pt, data in cf.iter_messages({dclraw.CH_EVENTS}):
            try:
                e = json.loads(_bytes(data))
            except ValueError:
                continue
            if e.get("event") == "clraw_start" and start is None:
                start = lt
            elif e.get("event") == "clraw_stop":
                stop = lt
    return start, stop


def survey_config(cf):
    """(configuration XML bytes, where it came from) for a line: the file's own attachment, else built from
    its hardware.json."""
    xml = cf.attachments.get(CONFIG_NAME)
    if xml:
        return bytes(xml), "recorded"
    hw = cf.hardware
    if not hw:
        raise BeamworxError("%s carries neither a BeamworX survey configuration nor hardware.json" % cf.name)
    return surveyconfig.from_hardware(hw, cf.metadata.get("vehicle")), "built from hardware.json"


def to_bwxraw(clraw_paths, out_dir, config_xml=None, vehicle=None, progress=None, cancelled=None):
    """Write the .bwxraw files of one recording into out_dir. Returns a dict: files (paths written, in
    order), damaged_chunks, config (where the survey configuration came from), points, navigation."""
    paths = sorted(clraw_paths, key=os.path.basename)
    if not paths:
        return {"files": [], "damaged_chunks": 0, "config": None, "points": 0, "navigation": 0}
    files = [dclraw.ClrawFile(p) for p in paths]
    first = files[0]
    if first.is_navigation:
        raise BeamworxError("%s is a navigation recording (no Lidar data): BeamworX files are made from survey "
                            "lines only" % first.name)
    md = first.metadata
    source = "given"
    if config_xml is None:
        config_xml, source = survey_config(first)
    vehicle = vehicle or md.get("vehicle") or "Mobile Mapper"
    line = int(md.get("line") or 0)
    digits = int(md.get("line_digits") or 3)
    start_ns, stop_ns = recording_times(files)
    os.makedirs(out_dir, exist_ok=True)

    class Series(bwxraw.Recorder):
        def _open(self, t):
            name = self.pending_name
            number = int(name[-len("0001.bwxraw"):-len(".bwxraw")])
            self.number = number
            w = bwxraw.Writer(os.path.join(self.folder, name), self.config_xml, self.vehicle, self.sensor,
                              self.line, number, t, run_in=self.nav.snapshot(), el_of=self.el_of)
            w.opened = t
            self.files.append(name)
            with open(os.path.join(self.folder, name[:-len(".bwxraw")] + ".xml"), "wb") as f:
                f.write(self.config_xml)
            return w

        def split(self, t):
            if self.cur is not None:
                self._end_file(t)

    rec = Series(out_dir, config_xml, vehicle, SENSOR, line, digits=digits)
    t0 = {"v": None}
    rec.el_of = lambda t: t - t0["v"] if t0["v"] is not None else 0.0
    total = sum(cf.size for cf in files) or 1
    done = 0
    stopped = started = False
    counts = {"points": 0, "navigation": 0}
    want = {dclraw.CH_POINTS, dclraw.CH_STATUS, dclraw.CH_ANPP}
    bad0 = sum(cf.bad_chunks for cf in files)      # the events were read once already: count this pass only
    try:
        for cf in files:
            rec.pending_name = cf.name[:-len(".clraw")] + ".bwxraw"
            new_file = True
            n = 0
            for ch, lt, _pt, data in cf.iter_messages(want):
                n += 1
                if cancelled is not None and n % 20000 == 0 and cancelled():
                    raise Cancelled()
                data = _bytes(data)
                if ch == dclraw.CH_ANPP:
                    if len(data) < 5:
                        continue
                    dev, code = bwxraw.CERTUS_DEVICE, bwxraw.anpp_code(data[1])
                else:
                    dev, code = bwxraw.HESAI_DEVICE, bwxraw.hesai_code(len(data))
                if code is None:
                    continue
                arr = lt / NS
                if t0["v"] is None:
                    t0["v"] = arr
                rec_ = (dev, code, arr, arr - t0["v"], data)
                if not started:
                    if start_ns is not None and lt < start_ns and ch != dclraw.CH_POINTS:   # before Start: run-in
                        rec.nav.add(rec_)
                        continue
                    started = True
                if stop_ns is not None and lt > stop_ns and not stopped and ch != dclraw.CH_POINTS:
                    rec.stop(stop_ns / NS)
                    stopped = True
                if new_file and not stopped:
                    rec.split(arr)
                    new_file = False
                rec.put(*rec_[:3], data, elapsed=rec_[3])
                counts["points" if ch == dclraw.CH_POINTS else "navigation"] += 1
            done += cf.size
            if progress:
                progress(done / total)
    except BaseException:
        _abandon(rec)
        raise
    names = rec.close()
    return {"files": [os.path.join(out_dir, n) for n in names], "damaged_chunks": sum(cf.bad_chunks for cf in files) - bad0,
            "config": source, "points": counts["points"], "navigation": counts["navigation"]}


def _abandon(rec):
    """Close whatever is open after a cancel or an error (the caller removes the work folder)."""
    for w in [rec.cur] + [t[0] for t in rec.tails]:
        if w is not None:
            try:
                w.f.close()
            except Exception:       # noqa: BLE001
                pass
