"""widgets.py - Ledger house standards + specialized page elements.
All values and code below are author-approved (Sep 2026)."""
import math
from ledger_lib import CW, ML

# ---- house sizing standards: same-type boxes IDENTICAL document-wide ----
LONG_W, LONG_H = 460, 54      # long-form config / full-sentence answers
SHORT_W, SHORT_H = 320, 50    # short-form answers
SMALL_W, SMALL_H = 180, 42    # single-fact sub-answers (pairs, yes/no, ions)
LINED_H = 104                 # ALL full-width lined free-response boxes
# [PATCH:formatter-spacing] WO-F2-S: gaps are VISIBLE WHITE SPACE on the
# page (ink edge to ink edge) — ledger_lib places text after a box by its
# cap top and lifts a box to sit exactly gap() below the text above it.
# Below a box always gets ≥6 pt more white than a label-to-its-own-box gap,
# so every label visibly owns the box BELOW it.
QGAP = 22                     # white below a box, before the next question
TGAP = 10                     # white between question text and its own box
RGAP = 16                     # white below a box, before the next sub-label
COL1 = ML + 40                # left column x (labels AND boxes share it)
COL2 = ML + 40 + CW // 2      # right column x; single leftovers use COL1

# ---- orbital diagram template (aufbau ladder students add arrows to) ----
LEVELS = [
    ("1s", 1, "s", 0.015), ("2s", 1, "s", 0.135), ("2p", 3, "p", 0.195),
    ("3s", 1, "s", 0.305), ("3p", 3, "p", 0.365), ("4s", 1, "s", 0.445),
    ("3d", 5, "d", 0.495), ("4p", 3, "p", 0.545), ("5s", 1, "s", 0.585),
    ("4d", 5, "d", 0.625), ("5p", 3, "p", 0.665), ("6s", 1, "s", 0.705),
    ("4f", 7, "f", 0.745), ("5d", 5, "d", 0.785), ("6p", 3, "p", 0.825),
    ("7s", 1, "s", 0.860), ("5f", 7, "f", 0.895), ("6d", 5, "d", 0.935),
    ("7p", 3, "p", 0.970),
]
GROUP_X = {"s": 8, "p": 58, "d": 140, "f": 272}
LINE_W, LINE_GAP = 19, 6

def orbital_template(page, tag, box_w=470, box_h=280):
    """One orbital-diagram question per page; sub-part b (lined) fits beneath."""
    c = page.c
    x = ML + (CW - box_w) / 2.0
    y_top = page._ink_top()      # [PATCH:formatter-spacing] true box top
    page.box(box_w, box_h, tag, center=True)
    inner_bot, inner_h = y_top - box_h + 16, box_h - 34
    c.setLineWidth(1.0)
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    for name, n, grp, e in LEVELS:
        ly = inner_bot + e * inner_h
        gx = x + GROUP_X[grp]
        for i in range(n):
            lx = gx + i * (LINE_W + LINE_GAP)
            c.line(lx, ly, lx + LINE_W, ly)
        mid = gx + (n * (LINE_W + LINE_GAP) - LINE_GAP) / 2.0
        c.setFont("LSerif", 8)
        c.drawCentredString(mid, ly - 9.5, name)

# ---- Bohr shell-model figures (pairs, Element A / Element B) ----
def shell_model(c, cx, cy, shells, label):
    radii = [24 + 21 * i for i in range(len(shells))]
    c.setLineWidth(1.1)
    c.setStrokeColorRGB(0.0, 0.55, 0.0)
    for r in radii:
        c.circle(cx, cy, r, stroke=1, fill=0)
    c.setFillColorRGB(0, 0, 0)
    c.circle(cx, cy, 5.5, stroke=0, fill=1)
    c.setFillColorRGB(0.16, 0.16, 0.78)
    for n_e, r in zip(shells, radii):
        for k in range(n_e):
            a = math.radians(90 + k * 360.0 / n_e)
            c.circle(cx + r * math.cos(a), cy + r * math.sin(a), 3.4, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("LSerif", 6.5)
    for i, r in enumerate(radii):
        a = math.radians(28)
        c.drawString(cx + (r - 9) * math.cos(a) + 3, cy + (r - 9) * math.sin(a) + 3,
                     f"n = {i + 1}")
    c.setFont("LSerif-B", 8)
    c.drawCentredString(cx, cy - radii[-1] - 14, label)

def shell_figure(page, shells_a, shells_b, fig_h=170):
    cy = page.y - fig_h / 2.0 + 12
    shell_model(page.c, ML + CW * 0.30, cy, shells_a, "Element A")
    shell_model(page.c, ML + CW * 0.70, cy, shells_b, "Element B")
    page.y -= fig_h

# ---- full-page fine-grid problem (vector quizzes etc.) ----
GRID_H, STEP = 545, 13          # uniform outer box; small graph squares
FA_W, FA_H, FA_INSET = 214, 72, 12

def grid_problem(page, num, text):
    """Question text, then a full-page tagged grid box with a nested,
    white-filled Final Answer box (outer tag ONLY, one combined box)."""
    page.q_text(num, text)
    page.gap(TGAP)
    c = page.c
    x = ML
    y_top = page._ink_top()      # [PATCH:formatter-spacing] true box top
    c.setLineWidth(0.4)
    c.setStrokeColorRGB(0.72, 0.72, 0.72)
    gx = x + STEP
    while gx < x + CW - 0.5:
        c.line(gx, y_top - GRID_H + 1, gx, y_top - 1); gx += STEP
    gy = y_top - STEP
    while gy > y_top - GRID_H + 0.5:
        c.line(x + 1, gy, x + CW - 1, gy); gy -= STEP
    c.setStrokeColorRGB(0, 0, 0)
    page.box(CW, GRID_H, str(num), center=False, x=x)
    fx = x + CW - FA_INSET - FA_W
    fy = y_top - GRID_H + FA_INSET
    c.setFillColorRGB(1, 1, 1)
    c.setLineWidth(1.8)
    c.rect(fx, fy, FA_W, FA_H, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("LSerif-I", 8)
    c.drawString(fx + 6, fy + FA_H - 12, "Final Answer")
