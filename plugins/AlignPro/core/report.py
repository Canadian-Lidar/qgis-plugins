"""ASCII calibration report, the HTML used for the PDF, and the calibration history scan."""
import datetime as dt
import html
import os
import re

TITLE = "AlignPro Lidar patch test report"


def default_name(project, ext):
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return os.path.join(project.report_dir, "Report_%s_%s.%s" % (stamp, project.name, ext))


def _f(v, fmt="%.4f"):
    return "-" if v is None else fmt % v


def ascii_report(project, header=True):
    c = project.cfg
    r = c.get("results") or {}
    L = []
    L.append(TITLE)
    L.append("=" * len(TITLE))
    L.append("Project      : %s" % project.name)
    if project.dcs is not None:
        L.append("Project file : %s" % project.dcs.path)
    L.append("Created      : %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if header:
        for k in ("vehicle", "operator", "client", "location"):
            if c["report"].get(k):
                L.append("%-13s: %s" % (k.capitalize(), c["report"][k]))
    t0 = min((l.window[0] for l in project.lines), default=None)
    if t0:
        L.append("Data date    : %s UTC" % dt.datetime.utcfromtimestamp(t0).strftime("%Y-%m-%d %H:%M:%S"))
    L.append("Trajectory   : %s" % (project.traj.source if project.traj else c["trajectory"].get("type")))
    ti = getattr(project, "traj_info", {}) or {}
    if ti.get("handover_id"):
        L.append("Trajectory id: %s" % ti["handover_id"])
    L.append("Lever arm    : F %.3f  S %.3f  D %.3f m  (Certus reference point to Lidar optical centre)"
             % tuple(c["lever"]))
    L.append("Latency      : %.2f ms" % c["latency_ms"])
    L.append("Convention   : mapper, M = Rz(h) Ry(p) Rx(-r) Z0; roll + stbd down, "
             "pitch + bow up, heading + clockwise")
    L.append("")
    L.append("Lidar mounting angles (deg)        Roll       Pitch     Heading")
    pre = c["mount_preset"]
    L.append("Recorded in .clraw           : %10.4f  %10.4f  %10.4f" % tuple(pre))
    if r:
        mi = r["mount_in"]
        L.append("Start of calibration         : %10.4f  %10.4f  %10.4f" % tuple(mi))
        a, s = r["advised"], r["sdev"]
        L.append("ADVISED                      : %10.4f  %10.4f  %10.4f" % (a["roll"], a["pitch"], a["heading"]))
        L.append("SDEV (1 sigma, combinations) : %10.4f  %10.4f  %10.4f" % (s["roll"], s["pitch"], s["heading"]))
        L.append("Change from recorded         : %10.4f  %10.4f  %10.4f" % (
            a["roll"] - pre[0], a["pitch"] - pre[1], a["heading"] - pre[2]))
        if r.get("bore"):
            L.append("Boresight about vehicle axes : %10.4f  %10.4f  %10.4f   (from the start angles)" % tuple(r["bore"]))
            L.append("  SDEV                       : %10.4f  %10.4f  %10.4f" % tuple(r["bore_sdev"]))
        L.append("")
        L.append("Mean point-to-plane error: %.2f cm before, %.2f cm after" % (
            r["error_before_cm"], r["error_after_cm"]))
        p = r["patches"]
        L.append("Planar patches: %d walls, %d sloped, %d flat" % (p["walls"], p["sloped"], p["flat"]))
        L.append("")
        L.append("Combinations")
        L.append("%-22s %10s %10s %10s %9s %9s %8s" % ("Combi", "Roll", "Pitch", "Heading", "Before", "After",
                                                     "Points"))
        for row in r["combis"]:
            if not row.get("ok"):
                L.append("%-22s  (did not converge)" % row["label"])
                continue
            L.append("%-22s %10.4f %10.4f %10.4f %8.2fcm %8.2fcm %8d" % (
                row["label"], row["roll"], row["pitch"], row["heading"], row["before_cm"], row["after_cm"],
                row["points"]))
        L.append("")
        L.append("Lines")
        L.append("%-6s %-4s %7s %7s %7s %7s %8s %8s %9s %9s" % ("Line", "Use", "COG", "SOG", "Length", "RTK%",
                                                             "PPS%", "Lost", "Err pre", "Err post"))
        for i, ln in enumerate(project.lines):
            st = ln.stats
            eb = r["line_err_before_cm"][i] if i < len(r["line_err_before_cm"]) else None
            ea = r["line_err_after_cm"][i] if i < len(r["line_err_after_cm"]) else None
            L.append("%-6s %-4s %7s %7s %7s %7s %8s %8s %9s %9s" % (
                ln.name, "yes" if ln.use else "no", _f(st.get("cog"), "%.1f"), _f(st.get("sog"), "%.1f"),
                _f(st.get("length"), "%.0f"), _f(st.get("fix_rtk_pct"), "%.0f"), _f(st.get("pps_lock_pct"), "%.0f"),
                st.get("lost", "-"), _f(eb, "%.2f"), _f(ea, "%.2f")))
        L.append("")
        L.append("Pairs")
        L.append("%-10s %-15s %7s %7s %8s %8s %9s %9s" % ("Pair", "Type", "dHdg", "dSpeed", "Overlap", "Patches",
                                                        "Err pre", "Err post"))
        for pr in r["pairs"]:
            L.append("%-10s %-15s %7.1f %7.1f %7.0f%% %8d %9s %9s" % (
                "%s-%s" % (r["lines"][pr["a"]], r["lines"][pr["b"]]), pr["type"], pr["dhdg"], pr["dspeed"],
                pr["overlap"], pr["shared"], _f(pr.get("e_before"), "%.2f"), _f(pr.get("e_after"), "%.2f")))
        if r.get("warnings"):
            L.append("")
            L.append("Warnings")
            for w in r["warnings"]:
                L.append("- " + w)
    if project.warnings:
        L.append("")
        L.append("Data warnings")
        for w in project.warnings:
            L.append("- " + w)
    L.append("")
    L.append("Files")
    for ln in project.lines:
        L.append("%-6s %s" % (ln.name, ", ".join(ln.info["files"])))
    L.append("")
    if c["report"].get("notes"):
        L.append("Notes")
        L.append(c["report"]["notes"])
        L.append("")
    L.append("Enter the advised angles in the mapper: Settings > Hardware > Survey sensor > Mounting angles.")
    return "\n".join(L) + "\n"


def html_report(project, images=()):
    txt = ascii_report(project)
    e = html.escape
    c = project.cfg
    r = c.get("results") or {}
    css = ("body{font-family:'Segoe UI',Arial,sans-serif;font-size:9.5pt;color:#111}"
           "h1{font-size:16pt;margin:0 0 4px 0}h2{font-size:11.5pt;margin:14px 0 4px 0;border-bottom:1px solid #999}"
           "table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:2px 6px;text-align:right}"
           "th{background:#e3efec}td.l,th.l{text-align:left}.big td{font-size:12pt;font-weight:bold}"
           ".warn{color:#a40000}")
    h = ["<html><head><style>%s</style></head><body>" % css,
         "<p style='color:#c8102e; font-weight:bold; letter-spacing:2px; margin:0'>Data Collection Systems</p>"
         "<p style='color:#0f8b8d; font-weight:bold; letter-spacing:1px; margin:0 0 6px 0'>AlignPro \u00b7 Lidar "
         "Boresight Calibration</p>", "<h1>%s</h1>" % e(TITLE)]
    h.append("<table>")
    rows = [("Project", project.name), ("Report created", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))]
    for k in ("vehicle", "operator", "client", "location"):
        if c["report"].get(k):
            rows.append((k.capitalize(), c["report"][k]))
    rows.append(("Trajectory", project.traj.source if project.traj else ""))
    rows.append(("Lever arm (F, S, D)", "%.3f, %.3f, %.3f m" % tuple(c["lever"])))
    rows.append(("Convention", "mapper: M = Rz(h) Ry(p) Rx(&minus;r) Z0; roll + stbd down, pitch + bow up, "
                               "heading + clockwise"))
    for k, v in rows:
        h.append("<tr><th class=l>%s</th><td class=l>%s</td></tr>" % (e(k), v if k == "Convention" else e(str(v))))
    h.append("</table>")
    if r:
        a, s, pre, mi = r["advised"], r["sdev"], c["mount_preset"], r["mount_in"]
        h.append("<h2>Lidar mounting angles (deg)</h2><table><tr><th class=l></th><th>Roll</th><th>Pitch</th>"
                 "<th>Heading</th></tr>")
        h.append("<tr><td class=l>Recorded in .clraw</td>%s</tr>" % "".join("<td>%.4f</td>" % v for v in pre))
        h.append("<tr><td class=l>Start of calibration</td>%s</tr>" % "".join("<td>%.4f</td>" % v for v in mi))
        h.append("<tr class=big><td class=l>ADVISED</td>%s</tr>" % "".join(
            "<td>%.4f</td>" % a[k] for k in ("roll", "pitch", "heading")))
        h.append("<tr><td class=l>SDEV (1&sigma;)</td>%s</tr>" % "".join(
            "<td>%.4f</td>" % s[k] for k in ("roll", "pitch", "heading")))
        if r.get("bore"):
            h.append("<tr><td class=l>Boresight about vehicle axes</td>%s</tr>" % "".join(
                "<td>%+.4f &plusmn; %.4f</td>" % (b, d) for b, d in zip(r["bore"], r["bore_sdev"])))
        h.append("</table><p>Mean point-to-plane error <b>%.2f cm</b> before, <b>%.2f cm</b> after. "
                 "Patches: %d walls, %d sloped, %d flat.</p>" % (
                     r["error_before_cm"], r["error_after_cm"], r["patches"]["walls"], r["patches"]["sloped"],
                     r["patches"]["flat"]))
        h.append("<h2>Combinations</h2><table><tr><th class=l>Combination</th><th>Roll</th><th>Pitch</th>"
                 "<th>Heading</th><th>Before cm</th><th>After cm</th><th>Points</th></tr>")
        for row in r["combis"]:
            if row.get("ok"):
                h.append("<tr><td class=l>%s</td><td>%.4f</td><td>%.4f</td><td>%.4f</td><td>%.2f</td><td>%.2f</td>"
                         "<td>%d</td></tr>" % (e(row["label"]), row["roll"], row["pitch"], row["heading"],
                                               row["before_cm"], row["after_cm"], row["points"]))
        h.append("</table>")
        h.append("<h2>Lines</h2><table><tr><th class=l>Line</th><th>Use</th><th>COG</th><th>SOG m/s</th>"
                 "<th>Length m</th><th>RTK fixed %</th><th>PPS lock %</th><th>Lost pkts</th><th>Err before cm</th>"
                 "<th>Err after cm</th></tr>")
        for i, ln in enumerate(project.lines):
            st = ln.stats
            eb = r["line_err_before_cm"][i] if i < len(r["line_err_before_cm"]) else None
            ea = r["line_err_after_cm"][i] if i < len(r["line_err_after_cm"]) else None
            h.append("<tr><td class=l><span style='color:%s'>&#9632;</span> %s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                         ln.color, e(ln.name), "yes" if ln.use else "no", _f(st.get("cog"), "%.1f"),
                         _f(st.get("sog"), "%.1f"), _f(st.get("length"), "%.0f"), _f(st.get("fix_rtk_pct"), "%.0f"),
                         _f(st.get("pps_lock_pct"), "%.0f"), st.get("lost", "-"), _f(eb, "%.2f"), _f(ea, "%.2f")))
        h.append("</table><h2>Pairs</h2><table><tr><th class=l>Pair</th><th class=l>Type</th><th>&Delta;Hdg</th>"
                 "<th>Overlap %</th><th>Patches</th><th>Err before cm</th><th>Err after cm</th></tr>")
        for pr in r["pairs"]:
            h.append("<tr><td class=l>%s-%s</td><td class=l>%s</td><td>%.1f</td><td>%.0f</td><td>%d</td><td>%s</td>"
                     "<td>%s</td></tr>" % (r["lines"][pr["a"]], r["lines"][pr["b"]], pr["type"], pr["dhdg"],
                                           pr["overlap"], pr["shared"], _f(pr.get("e_before"), "%.2f"),
                                           _f(pr.get("e_after"), "%.2f")))
        h.append("</table>")
        for w in r.get("warnings", []) + project.warnings:
            h.append("<p class=warn>&#9888; %s</p>" % e(w))
    if c["report"].get("notes"):
        h.append("<h2>Notes</h2><p>%s</p>" % e(c["report"]["notes"]).replace("\n", "<br>"))
    for title, path in images:
        from pathlib import Path
        h.append("<h2 style='page-break-before: always'>%s</h2><img src='%s' width=680>"
                 % (e(title), e(Path(path).resolve().as_uri())))
    h.append("<h2>Files</h2><pre style='font-size:8pt'>%s</pre>" % e(
        "\n".join("%s  %s" % (ln.name, ", ".join(ln.info["files"])) for ln in project.lines)))
    h.append("</body></html>")
    return "\n".join(h), txt


_ADV = re.compile(r"ADVISED\s*:\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)")
_SD = re.compile(r"SDEV.*:\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)")


def _report_files(parent, depth=6):
    """Report_*.txt under parent, down to `depth` folders (job folders / Processing / project /
    02_AlignPro / out), without descending into work/ caches."""
    parent = os.path.abspath(parent)
    base = parent.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(parent):
        if root.count(os.sep) - base >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d.lower() not in ("work", "log", "lidar", "ins", "clraw", "processed",
                                                         "__pycache__")]
        for f in files:
            if f.startswith("Report_") and f.endswith(".txt"):
                yield os.path.join(root, f)


def history(parent):
    """Calibration reports of this and sibling projects: everything under `parent` (by default the
    folder above the job folders), so each job's Processing/<Name>/02_AlignPro/out is found."""
    out = []
    pats = [list(_report_files(parent))]
    seen = set()
    for pat in pats:
        for p in pat:
            if p in seen:
                continue
            seen.add(p)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    t = f.read()
            except OSError:
                continue
            if "AlignPro" not in t:
                continue
            m = _ADV.search(t)
            if not m:
                continue
            sd = _SD.search(t)
            dd = re.search(r"Data date\s*:\s*(.+?) UTC", t)
            pr = re.search(r"Project\s*:\s*(.+)", t)
            out.append({"path": p, "pdf": p[:-4] + ".pdf" if os.path.isfile(p[:-4] + ".pdf") else None,
                        "project": pr.group(1).strip() if pr else "", "data_date": dd.group(1) if dd else "",
                        "roll": float(m.group(1)), "pitch": float(m.group(2)), "heading": float(m.group(3)),
                        "sd": [float(x) for x in sd.groups()] if sd else None,
                        "mtime": os.path.getmtime(p)})
    out.sort(key=lambda r: (r["data_date"], r["mtime"]))
    return out
