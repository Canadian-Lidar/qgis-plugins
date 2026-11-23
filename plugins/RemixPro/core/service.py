"""RemixPro's services to the other DCS plugins (DCS Core API 21, registry.provide; RemixPro 1.4.0).

"sonar_to_clraw": PosPro's marine Data page adds SonarView recordings (.svlz / .svlog) to a job. Each is converted
as RemixPro's own window does (make.run, CLRAW): into the job's day folder <job>/<YYYYMMDD>/CLRAW/ (a layout 2
project), else <out_root>/<recording>/clraw/. A recording already converted and unchanged costs nothing.
"""
import os
import re

from . import make, sources

_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d-\d\d-\d\d)", re.I)


def _with_parts(paths):
    """The chosen SonarView files plus the other parts of the same recordings (same time stamp, same folder)."""
    out = set()
    for p in paths:
        p = os.path.abspath(p)
        out.add(p)
        m = _STAMP.match(os.path.basename(p))
        d = os.path.dirname(p)
        if m and os.path.isdir(d):
            for f in os.listdir(d):
                if f.startswith(m.group(1)) and f.lower().endswith((".svlz", ".svlog")):
                    out.add(os.path.join(d, f))
    return sorted(out)


def sonar_to_clraw(paths, job_folder=None, out_root=None, progress=None, cancelled=None):
    """-> {"files": [.clraw made or already current], "notes": [str], "failed": [(path, reason)]}.
    progress(fraction, text); cancelled() -> True stops (make.Cancelled is raised)."""
    chosen = [p for p in paths if str(p).lower().endswith((".svlz", ".svlog"))]
    files, notes, failed = [], [], []
    if not chosen:
        return {"files": files, "notes": notes, "failed": failed}
    want = {os.path.normcase(p) for p in _with_parts(chosen)}
    items = [it for it in sources.scan(_with_parts(chosen)) if it.kind == sources.SONAR
             and {os.path.normcase(os.path.abspath(x)) for x in (it.parts or [it.sonar_file])} & want]
    covered = {os.path.normcase(os.path.abspath(x)) for it in items for x in (it.parts or [it.sonar_file])}
    for p in chosen:
        if os.path.normcase(os.path.abspath(p)) not in covered:
            failed.append((p, "not a SonarView recording RemixPro can read"))
    root = out_root or (os.path.join(job_folder, "RemixPro") if job_folder else None)
    for k, it in enumerate(items):
        it.day_job = job_folder
        if progress:
            progress(k / max(1, len(items)), "converting %s (%d of %d)" % (it.name, k + 1, len(items)))
        if sources.CLRAW not in it.outputs:
            failed.append((it.sonar_file, "; ".join(it.notes) or "cannot be converted"))
            continue
        out = root or os.path.dirname(it.sonar_file)

        def sub(f, k=k):
            if progress:
                progress((k + max(0.0, min(1.0, f))) / len(items), "converting %s (%d of %d)" % (it.name, k + 1,
                                                                                               len(items)))
        try:
            res = make.run(it, [sources.CLRAW], out, progress=sub, cancelled=cancelled)
        except make.Cancelled:
            raise
        except Exception as e:      # noqa: BLE001 - one recording's failure is reported, the others go on
            failed.append((it.sonar_file, str(e)))
            continue
        r = res.get(sources.CLRAW) or {}
        folder = r.get("folder") or out
        fs = [f if os.path.isabs(f) else os.path.join(folder, f) for f in r.get("files", [])]
        files += [f for f in fs if f.lower().endswith(".clraw")]
        notes += ["%s: %s" % (it.name, w) for w in r.get("warnings", [])]
        notes += ["%s: %s" % (it.name, n) for n in it.notes if "TidalCal" in n]
    if progress:
        progress(1.0, "converted")
    return {"files": files, "notes": notes, "failed": failed}
