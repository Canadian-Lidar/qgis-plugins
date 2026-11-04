#!/usr/bin/env python3
"""Build the AlignPro user guide PDF shipped in the plugin.

    python3 tools/build_guide.py RAW.pdf [VERSION] [OUT.pdf]

RAW.pdf is the "AlignPro — User Guide" doc exported to PDF. VERSION defaults to version= in
metadata.txt; OUT defaults to docs/AlignPro_User_Guide_<VERSION>.pdf.

The result has its own DCS cover page (dark band, maple logo, DATA COLLECTION SYSTEMS / CANADIAN
LIDAR, plugin name and subtitle, "User Guide, for AlignPro <version>"), a running header on every page
reading "AlignPro <version> — User Guide · Canadian Lidar", pages numbered with the cover included,
and author / title in the PDF properties.

Needs reportlab and PyMuPDF on the build machine only (pip install reportlab pymupdf); the plugin itself
does not use them."""
import glob
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = "AlignPro"
SUBTITLE = "Lidar Boresight Calibration"
TAGLINE = "Stage 02 of the DCS chain: PosPro \u2192 AlignPro \u2192 CloudPro"
AUTHOR = "Canadian Lidar"

BG = (0x09, 0x10, 0x0f)
SURF = (0x0e, 0x1a, 0x18)
ACCENT = (0x00, 0xe5, 0xaa)
BRIGHT = (0xe8, 0xf5, 0xf0)
TEXT = (0xb8, 0xd4, 0xce)
DIM = (0x5d, 0x85, 0x7c)
GREY = (0x6b, 0x6b, 0x6b)


def plugin_version():
    with open(os.path.join(ROOT, "metadata.txt"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("version="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("no version= in metadata.txt")


def find_font(bold=False):
    """A TrueType font with the em dash and middle dot (DejaVu, else reportlab's own Vera)."""
    names = ["DejaVuSans-Bold.ttf", "VeraBd.ttf"] if bold else ["DejaVuSans.ttf", "Vera.ttf"]
    dirs = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF", "/Library/Fonts",
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")]
    try:
        import reportlab
        dirs.append(os.path.join(os.path.dirname(reportlab.__file__), "fonts"))
    except ImportError:
        pass
    for n in names:
        for d in dirs:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


def rgb(c):
    return tuple(v / 255.0 for v in c)


def cover(version, size):
    """The cover page as PDF bytes (reportlab)."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    reg, bold = "Helvetica", "Helvetica-Bold"
    fr, fb = find_font(False), find_font(True)
    if fr and fb:
        pdfmetrics.registerFont(TTFont("DcsSans", fr))
        pdfmetrics.registerFont(TTFont("DcsSans-Bold", fb))
        reg, bold = "DcsSans", "DcsSans-Bold"

    w, h = size
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.setTitle("%s %s \u2014 User Guide" % (PLUGIN, version))
    c.setAuthor(AUTHOR)

    band = h * 0.46
    c.setFillColorRGB(*rgb(BG))
    c.rect(0, h - band, w, band, stroke=0, fill=1)
    c.setFillColorRGB(*rgb(ACCENT))
    c.rect(0, h - band - 5, w, 5, stroke=0, fill=1)

    logo = os.path.join(ROOT, "icons", "dcs_logo.png")
    lx, ly = 54, h - 150
    if os.path.exists(logo):
        img = ImageReader(logo)
        iw, ih = img.getSize()
        lh = 74
        c.drawImage(img, lx, ly, width=lh * iw / ih, height=lh, mask="auto")
        tx = lx + lh * iw / ih + 18
    else:
        tx = lx
    c.setFillColorRGB(*rgb(BRIGHT))
    c.setFont(bold, 15)
    c.drawString(tx, ly + 44, "DATA COLLECTION SYSTEMS")
    c.setFillColorRGB(*rgb(ACCENT))
    c.setFont(bold, 12)
    c.drawString(tx, ly + 24, "CANADIAN LIDAR")

    c.setFillColorRGB(*rgb(BRIGHT))
    c.setFont(bold, 50)
    c.drawString(54, h - band + 92, PLUGIN)
    c.setFillColorRGB(*rgb(TEXT))
    c.setFont(reg, 19)
    c.drawString(56, h - band + 58, SUBTITLE)
    c.setFillColorRGB(*rgb(DIM))
    c.setFont(reg, 11)
    c.drawString(56, h - band + 32, TAGLINE)

    c.setFillColorRGB(0.1, 0.12, 0.12)
    c.setFont(bold, 26)
    c.drawString(54, h - band - 90, "User Guide")
    c.setFont(reg, 15)
    c.setFillColorRGB(0.3, 0.33, 0.33)
    c.drawString(54, h - band - 116, "for %s %s" % (PLUGIN, version))

    c.setFillColorRGB(*rgb(BG))
    c.rect(0, 0, w, 46, stroke=0, fill=1)
    c.setFillColorRGB(*rgb(TEXT))
    c.setFont(reg, 9)
    c.drawString(54, 19, AUTHOR)
    c.drawRightString(w - 54, 19, "QGIS plugin \u00b7 needs DCS Core")
    c.showPage()
    c.save()
    return buf.getvalue()


def build(raw, version, out):
    import pymupdf as fitz

    src = fitz.open(raw)
    if src.page_count == 0:
        raise SystemExit("empty PDF: " + raw)
    rect = src[0].rect
    doc = fitz.open("pdf", cover(version, (rect.width, rect.height)))
    doc.insert_pdf(src)
    total = doc.page_count
    header = "%s %s \u2014 User Guide \u00b7 %s" % (PLUGIN, version, AUTHOR)
    fontfile = find_font(False)
    fkw = {"fontname": "dcs", "fontfile": fontfile} if fontfile else {"fontname": "helv"}

    for i in range(1, total):
        page = doc[i]
        top, bottom = None, None
        for x0, y0, x1, y1, word, *_ in page.get_text("words"):
            if y1 < 34:
                top = fitz.Rect(x0, y0, x1, y1) if top is None else top | fitz.Rect(x0, y0, x1, y1)
            elif y0 > rect.height - 34:
                bottom = fitz.Rect(x0, y0, x1, y1) if bottom is None else bottom | fitz.Rect(x0, y0, x1, y1)
        if top is None:
            raise SystemExit("page %d has no running header to replace" % i)
        for r in (top, bottom):
            if r is not None:
                page.add_redact_annot(r + (-1, -1, 1, 1), fill=(1, 1, 1))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        page.insert_text((top.x0, top.y1 - 1.6), header, fontsize=6, color=rgb(GREY), **fkw)
        if bottom is not None:
            label = "Page %d of %d" % (i + 1, total)
            font = fitz.Font(fontfile=fontfile) if fontfile else fitz.Font("helv")
            tl = font.text_length(label, fontsize=6)
            page.insert_text((bottom.x1 - tl, bottom.y1 - 1.6), label, fontsize=6, color=rgb(GREY), **fkw)

    doc.set_metadata({"title": "%s %s \u2014 User Guide" % (PLUGIN, version), "author": AUTHOR,
                      "subject": "%s: %s" % (PLUGIN, SUBTITLE), "creator": "%s build_guide.py" % PLUGIN,
                      "keywords": "Lidar, boresight, patch test, calibration, DCS, Canadian Lidar"})
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    doc.save(out, garbage=4, deflate=True)
    return total


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    raw = argv[1]
    version = argv[2] if len(argv) > 2 else plugin_version()
    out = argv[3] if len(argv) > 3 else os.path.join(ROOT, "docs", "AlignPro_User_Guide_%s.pdf" % version)
    n = build(raw, version, out)
    print("%s (%d pages)" % (out, n))
    others = [p for p in glob.glob(os.path.join(ROOT, "docs", "AlignPro_User_Guide_*.pdf"))
              if os.path.abspath(p) != os.path.abspath(out)]
    if others:
        print("older guides in docs/ (package.py removes them):", ", ".join(os.path.basename(p) for p in others))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
