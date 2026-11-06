"""ledger_lib.py - Layout engine for Ledger-format worksheets (author-approved)."""
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

_FD = "/usr/share/fonts/truetype/liberation/"
pdfmetrics.registerFont(TTFont("LSerif", _FD + "LiberationSerif-Regular.ttf"))
pdfmetrics.registerFont(TTFont("LSerif-B", _FD + "LiberationSerif-Bold.ttf"))
pdfmetrics.registerFont(TTFont("LSerif-I", _FD + "LiberationSerif-Italic.ttf"))
pdfmetrics.registerFont(TTFont("LSerif-BI", _FD + "LiberationSerif-BoldItalic.ttf"))

W, H = letter
# [PATCH:formatter] ID label seam (WO-F1). The header text was hard-coded
# "Bartonian ID"; the formatter must print each teacher's own label. Build
# scripts set ledger_lib.ID_LABEL = "Smithian ID" BEFORE creating Doc().
# Default preserves the author-approved output byte-for-byte.
ID_LABEL = "Bartonian ID"
ML = MR = 72
CW = W - ML - MR
BODY = 12
LH = 15
STROKE = 1.8
FOOTER_Y = 34
CONTENT_BOTTOM = 70


def sw(t, f="LSerif", s=BODY):
    return pdfmetrics.stringWidth(t, f, s)


# [PATCH:formatter-spacing] WO-F2-S: gaps are VISIBLE WHITE SPACE.
# Text draws with its BASELINE at page.y, but a box's border IS visible ink
# at page.y, so baseline-measured gaps looked backwards on the page (tight
# below a box, wide above one). The Page now tracks what it last drew:
# after a box or image the next text drops by its cap height, and a box
# after text rises to sit exactly gap() below the visible bottom of that
# text. Manual positioning (page.y = ...) switches the tracking off, so
# hand-placed elements behave exactly as before.
def cap_height(font="LSerif", size=BODY):
    """Height of capital letters (pt) — the visible TOP of a text line."""
    return (pdfmetrics.getFont(font).face.capHeight or 660) * size / 1000.0


def text_descent(font="LSerif", size=BODY):
    """Descender depth (pt) below the baseline — the visible BOTTOM."""
    return -pdfmetrics.getFont(font).face.descent * size / 1000.0


def wrap_plain(text, width, font="LSerif", size=BODY):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if sw(t, font, size) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def sup(t):
    return (t, "LSerif", 7.5, 4.5)


def sub(t):
    return (t, "LSerif", 7.5, -2.5)


def seg(t, f="LSerif", s=BODY):
    return (t, f, s, 0)


class Doc:
    def __init__(self, path, title):
        # initialFontName: ReportLab's page-start boilerplate otherwise selects
        # Helvetica (a non-embedded Type 1 ref; it draws no glyphs but fails
        # strict font audits). With this, the PDF references ONLY embedded
        # Liberation Serif — the footer page numbers audit clean.
        self.c = canvas.Canvas(path, pagesize=letter,
                               initialFontName="LSerif", initialFontSize=12)
        self.c.setTitle(title)
        self.title = title
        self.pagenum = 0

    def page(self):
        self.pagenum += 1
        return Page(self)

    def save(self):
        self.c.save()


class Page:
    def __init__(self, doc):
        self.d = doc
        self.c = doc.c
        self.num = doc.pagenum
        # [PATCH:formatter-spacing] element tracking (see note at top)
        self._y = 0.0
        self._prev = None            # None | 'text' | 'ink'
        self._lift = LH - text_descent()
        self.y = self._header()

    # [PATCH:formatter-spacing] --------------------------------------------
    @property
    def y(self):
        return self._y

    @y.setter
    def y(self, value):
        self._y = value
        self._prev = None            # manual positioning: legacy behaviour

    def _begin_text(self, font="LSerif", size=BODY):
        """Baseline for the next text line. After a box or image, drop by
        the cap height so the gap above the text is VISIBLE white space."""
        if self._prev == 'ink':
            self._y -= cap_height(font, size)
        self._prev = None
        return self._y

    def _end_text(self, last_baseline, font="LSerif", size=BODY, leading=LH):
        """Close a text element. page.y keeps its legacy value (baseline −
        leading) so existing scripts read the same y, but the page remembers
        where the text's visible bottom is for the next box()."""
        self._y = last_baseline - leading
        self._prev = 'text'
        self._lift = leading - text_descent(font, size)

    def _mark_ink(self, bottom):
        """Close an element whose bottom edge is visible ink (box, image)."""
        self._y = bottom
        self._prev = 'ink'

    def _ink_top(self):
        """Top y for an element whose top edge is visible ink. After text,
        rise so the gap below that text is VISIBLE white space."""
        if self._prev == 'text':
            return self._y + self._lift
        return self._y
    # ------------------------------------------------------------------

    def _header(self):
        c = self.c
        y = H - 55
        c.setFillColorRGB(0, 0, 0)
        c.setFont("LSerif-B", 12)
        c.drawString(ML, y, self.d.title)
        c.drawRightString(W - MR, y, ID_LABEL + " _______")  # [PATCH:formatter]
        iy, S = y - 16, 8
        segs = [
            ("Write neatly  ", "LSerif-B", S, 0), ("\u25e6", "LSerif-B", S, 0),
            ("  Boxes are reserved for ", "LSerif-B", S, 0),
            ("final answers only", "LSerif-BI", S, 0),
            ("  ", "LSerif-B", S, 0), ("\u25e6", "LSerif-B", S, 0), ("  ", "LSerif-B", S, 0),
            (ID_LABEL, "LSerif-BI", S, 0),  # [PATCH:formatter]
            (" must be written on each page  ", "LSerif-B", S, 0),
            ("\u25e6", "LSerif-B", S, 0), ("  Do not staple.", "LSerif-B", S, 0),
        ]
        x0 = (W - sum(sw(t, f, s) for (t, f, s, r) in segs)) / 2.0
        self._draw_segs(x0, iy, segs)
        ux = x0 + sum(sw(t, f, s) for (t, f, s, r) in segs[:3])
        uw = sw("final answers only", "LSerif-BI", S)
        c.setLineWidth(0.6)
        c.line(ux, iy - 1.5, ux + uw, iy - 1.5)
        return iy - 24

    def _footer(self):
        c, size = self.c, 13
        cx, cy = W / 2.0, FOOTER_Y
        c.setFillColorRGB(0, 0, 0)
        c.rect(cx - size / 2, cy - size / 2, size, size, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("LSerif", 9)
        c.drawCentredString(cx, cy - 3.2, str(self.num))
        c.setFillColorRGB(0, 0, 0)

    def _draw_segs(self, x, y, segs):
        for (t, f, s, r) in segs:
            self.c.setFont(f, s)
            self.c.drawString(x, y + r, t)
            x += sw(t, f, s)
        return x

    def q_text(self, num, text, indent=22):
        base = self._begin_text("LSerif", BODY)  # [PATCH:formatter-spacing]
        self.c.setFont("LSerif", BODY)
        self.c.drawString(ML, base, f"{num})")
        for ln in wrap_plain(text, CW - indent):
            self.c.setFont("LSerif", BODY)
            self.c.drawString(ML + indent, base, ln)
            base -= LH
        self._end_text(base + LH)

    def rich_line(self, num, segs, indent=22):
        base = self._begin_text("LSerif", BODY)  # [PATCH:formatter-spacing]
        self.c.setFont("LSerif", BODY)
        self.c.drawString(ML, base, f"{num})")
        self._draw_segs(ML + indent, base, segs)
        self._end_text(base)

    def plain_line(self, segs, x=None):
        f, s = (segs[0][1], segs[0][2]) if segs else ("LSerif", BODY)
        base = self._begin_text(f, s)            # [PATCH:formatter-spacing]
        self._draw_segs(ML if x is None else x, base, segs)
        self._end_text(base, f, s)

    def gap(self, g):
        self._y -= g        # [PATCH:formatter-spacing] keeps element tracking

    def image(self, path, w, h):
        x = ML + (CW - w) / 2.0
        y_top = self._ink_top()                  # [PATCH:formatter-spacing]
        self.c.drawImage(ImageReader(path), x, y_top - h, w, h)
        self._mark_ink(y_top - h)

    def box(self, w, h, tag, lined=False, grid=False, center=True, x=None):
        c = self.c
        if x is None:
            x = ML + (CW - w) / 2.0 if center else ML
        y_top = self._ink_top()                  # [PATCH:formatter-spacing]
        c.setLineWidth(STROKE)
        c.setStrokeColorRGB(0, 0, 0)
        c.rect(x, y_top - h, w, h, stroke=1, fill=0)
        if grid:
            c.setLineWidth(0.4)
            c.setStrokeColorRGB(0.62, 0.62, 0.62)
            step = 18
            gx = x + step
            while gx < x + w - 1:
                c.line(gx, y_top - h + 1, gx, y_top - 1)
                gx += step
            gy = y_top - step
            while gy > y_top - h + 1:
                c.line(x + 1, gy, x + w - 1, gy)
                gy -= step
            c.setStrokeColorRGB(0, 0, 0)
        th = 13.5
        tw = sw(tag, "LSerif", 9) + 9
        ox, oy = x - STROKE / 2.0, y_top + STROKE / 2.0
        c.setFillColorRGB(0, 0, 0)
        c.rect(ox, oy - th, tw, th, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("LSerif", 9)
        c.drawCentredString(ox + tw / 2.0, oy - th + 3.6, tag)
        c.setFillColorRGB(0, 0, 0)
        if lined:
            c.setLineWidth(0.8)
            ly = y_top - th - 22
            while ly > y_top - h + 8:
                c.line(x + 10, ly, x + w - 10, ly)
                ly -= 24
        self._mark_ink(y_top - h)                # [PATCH:formatter-spacing]

    def done(self):
        self._footer()
        self.c.showPage()
