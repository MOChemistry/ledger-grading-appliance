"""key_lib.py - answer-key page renderer for full-page grid vector problems."""
import math, re
from ledger_lib import CW, ML, sw

DEG, MIN, TH, SQ = "\u00b0", "\u2212", "\u03b8", "\u221a"
GRID_H, STEP = 545, 13
FA_W, FA_H, FA_INSET = 214, 72, 12


def rich_segs(line, size=10):
    segs = []
    for tok in re.split(r'(_[^_]+_|\^[^^]+\^)', line):
        if not tok:
            continue
        if tok.startswith('_') and tok.endswith('_'):
            segs.append((tok[1:-1], "LSerif", size * 0.7, -2))
        elif tok.startswith('^') and tok.endswith('^'):
            segs.append((tok[1:-1], "LSerif", size * 0.7, size * 0.35))
        else:
            segs.append((tok, "LSerif", size, 0))
    return segs


def draw_segs(c, x, y, segs):
    for (t, f, s, r) in segs:
        c.setFont(f, s)
        c.drawString(x, y + r, t)
        x += sw(t, f, s)


def arrow(c, x1, y1, x2, y2, dashed=False):
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    c.setLineWidth(1.4)
    if dashed:
        c.setDash(4, 3)
    c.line(x1, y1, x2 - ux * 5, y2 - uy * 5)
    c.setDash()
    hx, hy = x2 - ux * 7, y2 - uy * 7
    c.setFillColorRGB(0, 0, 0)
    p = c.beginPath()
    p.moveTo(x2, y2)
    p.lineTo(hx - uy * 3, hy + ux * 3)
    p.lineTo(hx + uy * 3, hy - ux * 3)
    p.close()
    c.drawPath(p, stroke=0, fill=1)


def key_problem(page, num, text, work, legs_units, labels, ppu, scale_note,
                origin_col, base_off, fa_lines, r_label=(0.5, 1), leg_sides=None):
    if leg_sides is None:
        leg_sides = [1] * len(legs_units)
    page.q_text(num, text)
    page.gap(10)  # [PATCH:formatter-spacing] visible white, matches TGAP
    c = page.c
    x, y_top = ML, page._ink_top()  # [PATCH:formatter-spacing] true box top
    y_bot = y_top - GRID_H
    # 1. fine grid
    c.setLineWidth(0.4)
    c.setStrokeColorRGB(0.72, 0.72, 0.72)
    gx = x + STEP
    while gx < x + CW - 0.5:
        c.line(gx, y_bot + 1, gx, y_top - 1); gx += STEP
    gy = y_top - STEP
    while gy > y_bot + 0.5:
        c.line(x + 1, gy, x + CW - 1, gy); gy -= STEP
    c.setStrokeColorRGB(0, 0, 0)
    # 2. work panel (white, flush to border) + solution lines
    ph = len(work) * 14.5 + 14
    c.setFillColorRGB(1, 1, 1)
    c.rect(x + 2, y_top - 2 - ph, CW - 4, ph, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    wy = y_top - 24
    for line in work:
        draw_segs(c, x + 16, wy, rich_segs(line))
        wy -= 14.5
    # 3. to-scale head-to-tail diagram
    ox = x + STEP * origin_col
    oy = y_bot + STEP * round(base_off / STEP)
    cx0, cy0 = ox, oy
    for (dxu, dyu), lab, side in zip(legs_units, labels, leg_sides):
        nx, ny = cx0 + dxu * ppu, cy0 + dyu * ppu
        arrow(c, cx0, cy0, nx, ny)
        L = math.hypot(nx - cx0, ny - cy0)
        ux, uy = (nx - cx0) / L, (ny - cy0) / L
        mx, my = (cx0 + nx) / 2 - uy * 9 * side, (cy0 + ny) / 2 + ux * 9 * side
        c.setFont("LSerif-B", 9)
        c.drawCentredString(mx, my - 3, lab)
        cx0, cy0 = nx, ny
    arrow(c, ox, oy, cx0, cy0, dashed=True)
    L = math.hypot(cx0 - ox, cy0 - oy)
    ux, uy = (cx0 - ox) / L, (cy0 - oy) / L
    t, sR = r_label
    c.setFont("LSerif-B", 9)
    c.drawCentredString(ox + (cx0 - ox) * t + uy * 10 * sR,
                        oy + (cy0 - oy) * t - ux * 10 * sR - 3, "R")
    # scale chip
    c.setFont("LSerif-I", 8)
    tw = sw(scale_note, "LSerif-I", 8)
    c.setFillColorRGB(1, 1, 1)
    c.rect(ox - 6, oy - 24, tw + 10, 13, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(ox - 1, oy - 20, scale_note)
    # 4. outer border + tag
    page.y = y_top  # [PATCH:formatter-spacing] box exactly where steps 1-3 drew
    page.box(CW, GRID_H, str(num), center=False, x=x)
    # 5. final-answer box, filled
    fx, fy = x + CW - FA_INSET - FA_W, y_bot + FA_INSET
    c.setFillColorRGB(1, 1, 1)
    c.setLineWidth(1.8)
    c.rect(fx, fy, FA_W, FA_H, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("LSerif-I", 8)
    c.drawString(fx + 6, fy + FA_H - 12, "Final Answer")
    ay = fy + FA_H - 30
    for ln in fa_lines:
        draw_segs(c, fx + 14, ay, rich_segs(ln, size=10.5))
        ay -= 15
