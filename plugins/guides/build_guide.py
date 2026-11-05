#!/usr/bin/env python3
"""Build a DCS plugin user guide PDF from its HTML source, the same way for every plugin.

    python guides/build_guide.py pospro        (or alignpro, cloudpro, remixpro)

Reads guides/<plugin>/guide.html (the body: <h1> sections, <figure> images from guides/<plugin>/fig/),
the plugin's name and version from plugins/<plugin>/metadata.txt (or ../<plugin>/metadata.txt beside
this folder), and writes <plugin folder>/docs/<Name>_User_Guide_<version>.pdf, removing older guides
there. The PDF has a cover page (graphite band, maple logo, DATA SYSTEMS, plugin name, version),
a running header "<Name> <version> — User Guide · Canadian Lidar" and page numbers on every body page.

Needs Python with playwright (Chromium) and pypdf: pip install playwright pypdf; playwright install chromium.
The plugins themselves do not use any of this.
"""
import datetime as dt
import glob
import html
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = {"pospro": ("PosPro", "GNSS/INS trajectory post-processing", "Stage 01"),
         "alignpro": ("AlignPro", "Lidar boresight calibration (patch test)", "Stage 02"),
         "cloudpro": ("CloudPro", "Point cloud georeferencing, cleaning, classification and delivery", "Stage 03"),
         "remixpro": ("RemixPro", "Files for BeamworX, Inertial Explorer and Qinertia", "Side tool")}


def plugin_dir(key):
    for cand in (os.path.join(os.path.dirname(HERE), "plugins"), os.path.dirname(HERE)):
        if not os.path.isdir(cand):
            continue
        for d in os.listdir(cand):
            if d.lower() == key and os.path.isfile(os.path.join(cand, d, "metadata.txt")):
                return os.path.join(cand, d)
    raise SystemExit("build_guide: no plugin folder for %s" % key)


def core_dir():
    return plugin_dir("dcs_core")


def version_of(folder):
    with open(os.path.join(folder, "metadata.txt"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("version="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("no version= in %s/metadata.txt" % folder)


def css(fonts):
    face = ""
    for fam, w, fn in (("Rajdhani", 500, "rajdhani-latin-500-normal.ttf"), ("Rajdhani", 600, "rajdhani-latin-600-normal.ttf"),
                       ("Rajdhani", 700, "rajdhani-latin-700-normal.ttf"), ("Outfit", 300, "outfit-latin-300-normal.ttf"),
                       ("Outfit", 400, "outfit-latin-400-normal.ttf"), ("Outfit", 600, "outfit-latin-600-normal.ttf"),
                       ("Source Code Pro", 400, "source-code-pro-latin-400-normal.ttf")):
        face += "@font-face{font-family:'%s';font-weight:%d;src:url('file://%s/%s')}\n" % (fam, w, fonts, fn)
    return face + """
:root{--ink:#1d2024;--quiet:#5a636b;--rule:#d5d9dd;--accent:#00a87d;--accent-ink:#007a5c;--maple:#c8102e;
      --graphite:#26292d;--panel:#f3f5f6}
@page{size:A4;margin:22mm 17mm 18mm 17mm}
html{font-family:'Outfit',sans-serif;font-weight:400;font-size:10.2pt;line-height:1.48;color:var(--ink)}
body{margin:0}
h1,h2,h3{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.02em;line-height:1.15;color:var(--graphite)}
h1{font-size:22pt;margin:0 0 6mm;padding-bottom:2.5mm;border-bottom:2px solid var(--accent);break-before:page}
h1:first-of-type{break-before:auto}
h1 .num{color:var(--accent-ink);margin-right:3mm}
h2{font-size:14.5pt;margin:7mm 0 2mm;break-after:avoid}
h3{font-size:11.5pt;margin:5mm 0 1.5mm;break-after:avoid;text-transform:uppercase;letter-spacing:.06em;color:#3d444b}
p{margin:0 0 2.4mm}
ul,ol{margin:0 0 3mm;padding-left:6mm}
li{margin:0 0 1.2mm}
b,strong{font-weight:600}
code,kbd,.mono{font-family:'Source Code Pro',monospace;font-size:8.8pt}
kbd{border:1px solid var(--rule);border-radius:2px;padding:0 1.2mm;background:#fff}
.ui{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.05em;text-transform:uppercase;font-size:9.6pt;
    color:#2b3036;background:#e8ecee;border-radius:2px;padding:.2mm 1.3mm;white-space:nowrap}
figure{margin:4mm 0 5mm;break-inside:avoid;text-align:center}
figure img{max-width:100%;max-height:150mm;border:1px solid #c9ced3;border-radius:3px}
figure.half img{max-width:72%}
figure.small img{max-width:52%}
figcaption{font-size:8.8pt;color:var(--quiet);margin-top:1.6mm;text-align:left}
figcaption b{color:var(--ink)}
table{border-collapse:collapse;width:100%;margin:2mm 0 4mm;font-size:9pt;break-inside:auto}
th,td{border-bottom:1px solid var(--rule);padding:1.4mm 2mm;text-align:left;vertical-align:top}
th{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.04em;text-transform:uppercase;font-size:8.8pt;
   background:var(--panel);color:#3d444b}
tr{break-inside:avoid}
.note,.tip,.warn{border-left:3px solid var(--accent);background:var(--panel);padding:2.2mm 3.2mm;margin:3mm 0;
    break-inside:avoid}
.warn{border-color:#d98a0b}
.note b:first-child,.tip b:first-child,.warn b:first-child{font-family:'Rajdhani',sans-serif;letter-spacing:.05em;
    text-transform:uppercase}
.steps{counter-reset:s;list-style:none;padding-left:0}
.steps>li{counter-increment:s;padding-left:8mm;position:relative}
.steps>li:before{content:counter(s);position:absolute;left:0;top:.2mm;width:5mm;height:5mm;border-radius:50%;
    background:var(--accent);color:#fff;font-family:'Rajdhani',sans-serif;font-weight:700;font-size:8.5pt;
    text-align:center;line-height:5mm}
.toc{columns:2;column-gap:10mm;font-size:9.6pt;margin-bottom:4mm}
.toc a{color:var(--ink);text-decoration:none}
.toc div{break-inside:avoid;margin-bottom:1mm}
.toc .n{color:var(--accent-ink);font-family:'Rajdhani',sans-serif;font-weight:700;display:inline-block;width:7mm}
a{color:var(--accent-ink)}
"""


def cover_html(name, subtitle, stage, version, logo, fonts, group):
    today = dt.date.today().strftime("%B %Y")
    return """<!doctype html><html><head><meta charset="utf-8"><style>%s
@page{size:A4;margin:0}
body{margin:0;width:210mm;height:297mm;position:relative;background:#fff}
.band{position:absolute;left:0;right:0;top:0;height:118mm;background:linear-gradient(135deg,#3a1a20 0%%,#26292d 46%%,#26292d 100%%)}
.logo{position:absolute;left:22mm;top:30mm;width:34mm}
.grp{position:absolute;left:62mm;top:33mm;font-family:Rajdhani;font-weight:700;color:#fff;font-size:18pt;letter-spacing:.1em}
.nm{position:absolute;left:62mm;top:44mm;font-family:Rajdhani;font-weight:700;color:#00e5aa;font-size:44pt;letter-spacing:.02em}
.st{position:absolute;left:22mm;top:84mm;right:22mm;font-family:Outfit;font-weight:300;color:#e3e7ea;font-size:15pt}
.chain{position:absolute;left:22mm;top:98mm;font-family:Rajdhani;font-weight:600;color:#a0a9b1;font-size:11pt;letter-spacing:.08em}
.ug{position:absolute;left:22mm;top:140mm;font-family:Rajdhani;font-weight:700;font-size:34pt;color:#26292d}
.ver{position:absolute;left:22mm;top:158mm;font-family:Outfit;font-size:13pt;color:#5a636b}
.foot{position:absolute;left:22mm;bottom:20mm;right:22mm;font-family:Outfit;font-size:9pt;color:#5a636b;border-top:1px solid #d5d9dd;padding-top:3mm}
</style></head><body>
<div class="band"></div><img class="logo" src="file://%s">
<div class="grp">%s</div><div class="nm">%s</div>
<div class="st">%s</div>
<div class="chain">%s OF THE DCS CHAIN &nbsp;·&nbsp; POSPRO → ALIGNPRO → CLOUDPRO</div>
<div class="ug">User Guide</div><div class="ver">For %s %s &nbsp;·&nbsp; %s</div>
<div class="foot">Canadian Lidar &nbsp;·&nbsp; QGIS plugin, part of the Data Collection Systems chain. Needs the DCS Core plugin.</div>
</body></html>""" % (css(fonts), logo, html.escape(group.upper()), html.escape(name), html.escape(subtitle),
                     stage.upper(), html.escape(name), version, today)


def assemble(text, name, key):
    """Includes, variables, chapter and figure numbers, contents.

    <!--#vars {"KEY": "value", ...}-->   values for {KEY} placeholders (JSON, at the top)
    <!--#include install-->             guides/common/install.html (also window, projects)
    <!--#contents-->                    a contents list of the chapters (h1)
    {N} inside a chapter becomes its number, in order of the <h1>s."""
    import json
    import re
    var = {"NAME": name, "KEY": key}
    m = re.search(r"<!--#vars(.*?)-->", text, re.S)
    if m:
        var.update(json.loads(m.group(1)))
        text = text.replace(m.group(0), "")

    def inc(mm):
        with open(os.path.join(HERE, "common", mm.group(1).strip() + ".html"), encoding="utf-8") as f:
            return f.read()
    text = re.sub(r"<!--#include\s+([\w-]+)\s*-->", inc, text)
    for _ in range(3):                                   # values may hold placeholders too
        for k, v in var.items():
            text = text.replace("{%s}" % k, v)
    parts = re.split(r"(?=<h1[\s>])", text)
    out, toc, n = [], [], 0
    for part in parts:
        if part.startswith("<h1"):
            n += 1
            part = part.replace("{N}", str(n))
            hm = re.match(r"<h1([^>]*)>(.*?)</h1>", part, re.S)
            if hm:
                ident = re.search(r'id="([^"]+)"', hm.group(1))
                if not ident:
                    anchor = "ch%d" % n
                    part = part.replace("<h1%s>" % hm.group(1), '<h1 id="%s"%s>' % (anchor, hm.group(1)), 1)
                else:
                    anchor = ident.group(1)
                title = re.sub(r"<span class=\"num\">.*?</span>", "", hm.group(2))
                toc.append('<div><span class="n">%d</span><a href="#%s">%s</a></div>' % (n, anchor, title.strip()))
        out.append(part)
    text = "".join(out)
    return text.replace("<!--#contents-->", '<h2 style="margin-top:0">Contents</h2><div class="toc">%s</div>' % "".join(toc))


def build(key):
    from playwright.sync_api import sync_playwright
    from pypdf import PdfReader, PdfWriter
    name, subtitle, stage = NAMES[key]
    pdir = plugin_dir(key)
    cdir = core_dir()
    version = version_of(pdir)
    fonts = os.path.join(cdir, "fonts")
    logo = os.path.join(cdir, "icons", "logo.png")
    src = os.path.join(HERE, key, "guide.html")
    with open(src, encoding="utf-8") as f:
        body = assemble(f.read(), name, key).replace("{VERSION}", version).replace("{CORE_VERSION}", version_of(cdir))
    group = "Data Systems"
    try:
        with open(os.path.join(cdir, "menu.py"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("GROUP ="):
                    group = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    base = os.path.join(HERE, key) + "/"
    page = ("<!doctype html><html><head><meta charset='utf-8'><base href='file://%s'><style>%s</style></head>"
            "<body>%s</body></html>" % (base, css(fonts), body))
    tmp_body = os.path.join(HERE, key, "_body.html")
    tmp_cover = os.path.join(HERE, key, "_cover.html")
    with open(tmp_body, "w", encoding="utf-8") as f:
        f.write(page)
    with open(tmp_cover, "w", encoding="utf-8") as f:
        f.write(cover_html(name, subtitle, stage, version, logo, fonts, group))
    head = ("<div style='width:100%%;font-family:Outfit,sans-serif;font-size:7.5pt;color:#7a838b;padding:0 17mm;"
            "display:flex;justify-content:space-between'><span>%s %s — User Guide</span><span>Canadian Lidar</span></div>"
            % (name, version))
    foot = ("<div style='width:100%;font-family:Outfit,sans-serif;font-size:7.5pt;color:#7a838b;text-align:center'>"
            "<span class='pageNumber'></span></div>")
    out_body = os.path.join(HERE, key, "_body.pdf")
    out_cover = os.path.join(HERE, key, "_cover.pdf")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.goto("file://" + tmp_cover)
        pg.wait_for_load_state("networkidle")
        pg.pdf(path=out_cover, format="A4", print_background=True)
        pg.goto("file://" + tmp_body)
        pg.wait_for_load_state("networkidle")
        pg.pdf(path=out_body, format="A4", print_background=True, display_header_footer=True,
               header_template=head, footer_template=foot,
               margin={"top": "22mm", "bottom": "18mm", "left": "17mm", "right": "17mm"})
        b.close()
    w = PdfWriter()
    for part in (out_cover, out_body):
        for p in PdfReader(part).pages:
            w.add_page(p)
    w.add_metadata({"/Title": "%s %s — User Guide" % (name, version), "/Author": "Canadian Lidar",
                    "/Subject": subtitle})
    docs = os.path.join(pdir, "docs")
    os.makedirs(docs, exist_ok=True)
    for old in glob.glob(os.path.join(docs, "%s_User_Guide_*.pdf" % name)):
        os.remove(old)
    out = os.path.join(docs, "%s_User_Guide_%s.pdf" % (name, version))
    with open(out, "wb") as f:
        w.write(f)
    for t in (tmp_body, tmp_cover, out_body, out_cover):
        os.remove(t)
    print(out, len(w.pages), "pages")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in NAMES:
        raise SystemExit(__doc__)
    build(sys.argv[1])
