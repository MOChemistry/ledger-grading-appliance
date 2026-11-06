"""exam_widgets.py — new Ledger widgets for exam-style documents.

Adds to the house standards in widgets.py:
  * bubble_strip()   — compact Scantron-style MC answer box (tagged)
  * pes_figure()     — photoelectron spectrum, peak heights PROPORTIONAL to
                       electron count (2 e- : 1 e-  ->  2 : 1 height)
  * id_box()         — answer-only box for identification items (no work
                       area, no nested cell: the box IS the final answer)
  * work_box()       — large open work box + nested white Final Answer cell
  * sketch_box()     — open justification box (diagram or prose) + FA cell
  * lined_box_fa()   — lined verbal-response box, optional Final Answer cell
  * fig_box()        — label-the-figure box (PES drawn inside)
  * orbital_rows()   — printed orbital-diagram answer bank (rows A..E)
  * section_banner() — centered section title + instruction lines

Box-type selection rule (one style per RESPONSE TYPE, identical sizes
document-wide):
  identification (one word/value)      -> id_box SHORT (320x50) at COL1
  long symbolic answer (e- config)     -> id_box LONG  (460x54) centered
  numeric/derivation work              -> work_box + FINAL ANSWER cell
  draw-to-justify (orbital diagram)    -> sketch_box + FINAL ANSWER cell
  verbal explanation only              -> lined_box_fa(fa=False)
  verbal justification + short answer  -> lined_box_fa(fa=True, fa_hint=...)
  label a printed figure               -> fig_box (no cell)
"""
import math
from ledger_lib import CW, ML, sw, seg, sup, wrap_plain, BODY, LH, STROKE

# ---- exam house sizing (same-type elements IDENTICAL document-wide) ----
MC_W, MC_H = 186, 30          # bubble-strip box, 5 choices A-E
ID_SHORT_W, ID_SHORT_H = 320, 50   # identification answers
ID_LONG_W, ID_LONG_H = 460, 54     # long symbolic answers (configs)
FR_SHORT_H = 200              # open work box for worked answers
FR_LINED_H = 220              # lined verbal-response box
FR_SKETCH_H = 240             # open draw-to-justify box
FR_FIG_H = 200                # label-the-figure box (PES inside)
FA_W, FA_H, FA_INSET = 240, 48, 10   # nested Final Answer cell
# [PATCH:formatter-spacing] WO-F2-S: gaps are VISIBLE WHITE SPACE (ink edge
# to ink edge) — see the note in widgets.py / ledger_lib.py.
QGAP = 22   # white below a box, before the next question
TGAP = 10   # white between question text and its own box
PTS_SIZE = 10                 # italic point-value line
COL1 = ML + 40                # sub-part column grid (house rule)


def pts_line(page, text, indent=22):
    """Italic point-value note under a question/part."""
    base = page._begin_text("LSerif-I", PTS_SIZE)  # [PATCH:formatter-spacing]
    for ln in wrap_plain(text, CW - indent, "LSerif-I", PTS_SIZE):
        page.c.setFont("LSerif-I", PTS_SIZE)
        page.c.drawString(ML + indent, base, ln)
        base -= 13
    page._end_text(base + 13, "LSerif-I", PTS_SIZE, leading=13)


def section_banner(page, title, lines):
    """Centered bold section title + small bold/italic instruction lines."""
    c = page.c
    c.setFillColorRGB(0, 0, 0)
    base = page._begin_text("LSerif-B", 12)        # [PATCH:formatter-spacing]
    c.setFont("LSerif-B", 12)
    c.drawCentredString(ML + CW / 2.0, base, title)
    base -= 16
    lf, ls = "LSerif-B", 12
    for f, s, t in lines:
        c.setFont(f, s)
        c.drawCentredString(ML + CW / 2.0, base, t)
        base -= 13
        lf, ls = f, s
    page._end_text(base + 13, lf, ls, leading=13)


def bubble_strip(page, num, x=None):
    """Tagged MC answer box containing bubbles (A)-(E) to fill in."""
    c = page.c
    if x is None:
        x = ML + CW - MC_W
    y_top = page._ink_top()      # [PATCH:formatter-spacing] true strip top
    c.setLineWidth(STROKE)
    c.setStrokeColorRGB(0, 0, 0)
    c.rect(x, y_top - MC_H, MC_W, MC_H, stroke=1, fill=0)
    # tag flush over outer top-left corner (zero gap)
    th = 13.5
    tag = str(num)
    tw = sw(tag, "LSerif", 9) + 9
    ox, oy = x - STROKE / 2.0, y_top + STROKE / 2.0
    c.setFillColorRGB(0, 0, 0)
    c.rect(ox, oy - th, tw, th, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("LSerif", 9)
    c.drawCentredString(ox + tw / 2.0, oy - th + 3.6, tag)
    c.setFillColorRGB(0, 0, 0)
    # five bubbles, letters inside, inset clear of the tag
    r = 7.2
    cy = y_top - MC_H / 2.0
    pitch = (MC_W - 2 * 28) / 4.0
    c.setLineWidth(1.0)
    for i, letter in enumerate("ABCDE"):
        cx = x + 28 + i * pitch
        c.circle(cx, cy, r, stroke=1, fill=0)
        c.setFont("LSerif", 8)
        c.drawCentredString(cx, cy - 2.8, letter)
    page._mark_ink(y_top - MC_H)  # [PATCH:formatter-spacing]


def id_box(page, tag, long=False):
    """Answer-only box for identification items. Short boxes sit on COL1;
    long boxes (e.g., full electron configurations) are centered."""
    if long:
        page.box(ID_LONG_W, ID_LONG_H, tag, center=True)
    else:
        page.box(ID_SHORT_W, ID_SHORT_H, tag, center=False, x=COL1)


def _hint(c, x, y, text):
    """Light-gray letterspaced hint: chars spaced within words, wider
    gaps between words."""
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.setFont("LSerif", 7)
    c.drawString(x, y, "   ".join(" ".join(w) for w in text.split()))
    c.setFillColorRGB(0, 0, 0)


def _fa_cell(c, x, y_top, h, fa_hint=None):
    fx = x + CW - FA_INSET - FA_W
    fy = y_top - h + FA_INSET
    c.setFillColorRGB(1, 1, 1)
    c.setLineWidth(1.8)
    c.rect(fx, fy, FA_W, FA_H, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("LSerif-B", 8)
    c.drawString(fx + 6, fy + FA_H - 12, "FINAL ANSWER")
    if fa_hint:
        c.setFillColorRGB(0.55, 0.55, 0.55)
        c.setFont("LSerif", 7)
        c.drawRightString(fx + FA_W - 6, fy + FA_H - 12, fa_hint)
        c.setFillColorRGB(0, 0, 0)


def work_box(page, tag, h=FR_SHORT_H, fa=True, fa_hint=None):
    """Full-width open work box; nested white Final Answer cell
    bottom-right; light 'SHOW YOUR WORK BELOW' hint under the tag."""
    c = page.c
    x, y_top = ML, page._ink_top()  # [PATCH:formatter-spacing] true box top
    page.box(CW, h, tag, center=False, x=x)
    _hint(c, x + 8, y_top - 24, "SHOW YOUR WORK BELOW")
    if fa:
        _fa_cell(c, x, y_top, h, fa_hint)


def sketch_box(page, tag, h=FR_SKETCH_H, fa=True, fa_hint=None):
    """Open draw-to-justify box: diagram or written explanation, no lines
    (lines fight a drawn diagram); Final Answer cell bottom-right."""
    c = page.c
    x, y_top = ML, page._ink_top()  # [PATCH:formatter-spacing] true box top
    page.box(CW, h, tag, center=False, x=x)
    _hint(c, x + 8, y_top - 24,
          "JUSTIFY BELOW — ORBITAL DIAGRAM OR EXPLANATION")
    if fa:
        _fa_cell(c, x, y_top, h, fa_hint)


def lined_box_fa(page, tag, h=FR_LINED_H, fa=False, fa_hint=None):
    """Lined verbal-response box; optional white Final Answer cell that
    sits on top of the lines (bottom-right)."""
    c = page.c
    x, y_top = ML, page._ink_top()  # [PATCH:formatter-spacing] true box top
    page.box(CW, h, tag, lined=True, center=False, x=x)
    if fa:
        _fa_cell(c, x, y_top, h, fa_hint)


def _peak(c, x0, y0, h, half_w):
    """Sharp PES cusp centered at x0, base y0, height h."""
    c.setLineWidth(1.6)
    p = c.beginPath()
    p.moveTo(x0 - half_w, y0)
    p.curveTo(x0 - half_w * 0.35, y0 + h * 0.04,
              x0 - half_w * 0.10, y0 + h * 0.55, x0, y0 + h)
    p.curveTo(x0 + half_w * 0.10, y0 + h * 0.55,
              x0 + half_w * 0.35, y0 + h * 0.04, x0 + half_w, y0)
    c.drawPath(p, stroke=1, fill=0)


def pes_figure(c, x, y_base, w, peaks, breaks=(), unit_h=24.0, half_w=16,
               label_offset=0.0):
    """Photoelectron spectrum. peaks = [(frac_x, n_electrons, 'value'), ...]
    Height = n_electrons * unit_h  (STRICT proportionality).
    breaks = fractions of w where the axis-break marks go."""
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    axis_y = y_base
    c.setLineWidth(1.4)
    c.line(x, axis_y, x + w, axis_y)
    for fx, n_e, val in peaks:
        px = x + fx * w
        _peak(c, px, axis_y, n_e * unit_h, half_w)
        c.setFont("LSerif", 9)
        c.drawCentredString(px, axis_y - 11, val)
    for bf in breaks:
        bx = x + bf * w
        c.setFont("LSerif-B", 12)
        c.drawCentredString(bx, axis_y - 4, "»")
    # caption: left arrow + text, centered under axis
    cap = " Ionization Energy (MJ/mol)"
    c.setFont("LSerif", 11)
    capw = sw(cap, "LSerif", 11) + 12
    cx0 = x + (w - capw) / 2.0
    ay = axis_y - 26
    c.setLineWidth(1.1)
    c.line(cx0, ay + 3.5, cx0 + 11, ay + 3.5)
    p = c.beginPath()
    p.moveTo(cx0 + 7, ay + 7); p.lineTo(cx0, ay + 3.5); p.lineTo(cx0 + 7, ay)
    c.drawPath(p, stroke=1, fill=0)
    c.drawString(cx0 + 14, ay, cap.strip())


def fig_box(page, tag, peaks, breaks=(), h=FR_FIG_H, unit_h=18.0,
            fig_w=330):
    """Tagged box containing a PES spectrum for the student to label.
    Room is left ABOVE the peaks for the student's labels."""
    c = page.c
    x, y_top = ML, page._ink_top()  # [PATCH:formatter-spacing] true box top
    page.box(CW, h, tag, center=False, x=x)
    _hint(c, x + 8, y_top - 24, "LABEL THE SPECTRUM BELOW")
    fx = x + (CW - fig_w) / 2.0
    fy = y_top - h + 46
    pes_figure(c, fx, fy, fig_w, peaks, breaks, unit_h=unit_h, half_w=13)


# ---- printed orbital-diagram answer bank (rows A..E) ----
ARROW_H = 11.0
O_LINE_W, O_LINE_GAP, O_GRP_GAP = 20, 5, 14


def _arrow(c, x, y, up=True):
    c.setLineWidth(0.9)
    if up:
        c.line(x, y, x, y + ARROW_H)
        p = c.beginPath()
        p.moveTo(x - 2.2, y + ARROW_H - 3.4); p.lineTo(x, y + ARROW_H)
        p.lineTo(x + 2.2, y + ARROW_H - 3.4)
        c.drawPath(p, stroke=1, fill=0)
    else:
        c.line(x, y + ARROW_H, x, y)
        p = c.beginPath()
        p.moveTo(x - 2.2, y + 3.4); p.lineTo(x, y); p.lineTo(x + 2.2, y + 3.4)
        c.drawPath(p, stroke=1, fill=0)


def orbital_rows(page, rows, row_h=40):
    """rows = [(letter, [ (label_main, label_sup, [occ,...]), ...]), ...]
    occ: 2 = paired, 1 = single up, 0 = empty orbital line."""
    c = page.c
    c.setFillColorRGB(0, 0, 0)
    def grp_w(g):
        return len(g[2]) * (O_LINE_W + O_LINE_GAP) - O_LINE_GAP
    x0 = ML + 30
    for letter, groups in rows:
        y_line = page.y - row_h + 16
        c.setFont("LSerif", BODY)
        c.drawString(x0 - 28, y_line + 2, f"({letter})")
        gx = x0
        for label_main, label_sup, occ in groups:
            for i, o in enumerate(occ):
                lx = gx + i * (O_LINE_W + O_LINE_GAP)
                c.setLineWidth(1.0)
                c.line(lx, y_line, lx + O_LINE_W, y_line)
                mid = lx + O_LINE_W / 2.0
                if o == 2:
                    _arrow(c, mid - 2.6, y_line + 1, up=True)
                    _arrow(c, mid + 2.6, y_line + 1, up=False)
                elif o == 1:
                    _arrow(c, mid, y_line + 1, up=True)
            w = grp_w((None, None, occ))
            mid = gx + w / 2.0
            lw = sw(label_main, "LSerif", 9) + sw(label_sup, "LSerif", 6.5)
            c.setFont("LSerif", 9)
            c.drawString(mid - lw / 2.0, y_line - 12, label_main)
            c.setFont("LSerif", 6.5)
            c.drawString(mid - lw / 2.0 + sw(label_main, "LSerif", 9),
                         y_line - 12 + 3.5, label_sup)
            gx += w + O_GRP_GAP
        page.y -= row_h
