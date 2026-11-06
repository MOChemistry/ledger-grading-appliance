"""compact.py - space-conscious layout add-on for Ledger worksheets (Oct 2026).

Layers on ledger_lib + widgets; NEVER changes box sizes or the COL1/COL2 grid.
Compact density is the default for quizzes and practice quizzes. Use spacious
(one question per page) only when the user wants work shown on the page.

Workflow: estimate each question with est_question(), pack pages with pack(),
render with the helpers below, and close every page with finish_page() so the
footer-clearance assertion runs. Field-proven values (Oct 2026): a 6-question
kinematics quiz that rendered at 6 pages spacious fits 4 pages compact.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger_lib import CW, ML, BODY, LH, wrap_plain
from widgets import SMALL_W, SMALL_H, COL1, COL2
from key_lib import rich_segs, draw_segs

# ---- compact spacing constants (do not shrink further; jammed reads cheap) --
# [PATCH:formatter-spacing] WO-F2-S: every gap below is VISIBLE WHITE SPACE
# on the rendered page (ink edge to ink edge) — ledger_lib places text after
# a box by its cap top and lifts a box to sit exactly gap() below the text
# above it. Do not add compensation of your own. Hard rule: below a box
# always comes ≥6 pt MORE white than between a label and its own box, so
# every label visibly owns the box BELOW it.
QGAP_C = 22     # white below a box, before the next numbered question
RGAP_C = 16     # white below a box, before the next sub-part label/heading
LABGAP_C = 10   # white between a sub-part label and its OWN box
TGAP_C = 10     # white between question text and its own figure/box
IMGAP_C = 12    # white between a figure and the first sub-part row
LABEL_H = 10.5  # cap height + descent of one BODY label line (est_question)
WORK_SIZE = 9   # answer-key work font
WORK_LH = 10.5  # answer-key work leading
WORK_GAP = 14   # white below the question's last box, before key work lines
FLOOR = 72      # lowest allowed content y (Ledger crops below ~70)

# ---- graph display widths (pt). Smaller than spacious; keep fonts legible by
# generating figures ~3.2-3.4 in wide so text scales to ~11 pt effective. ----
XT_GRAPH_W = 215    # line graphs (e.g. position-time)
STEP_GRAPH_W = 200  # step graphs (e.g. acceleration-time)

# ---- page budget ------------------------------------------------------------
# Content starts at y ~= 697 (below header); floor is 72 -> ~625 pt usable.
# Estimates carry ~±15 pt, so the plan is a draft and finish_page is the hard
# gate: when a pairing you want narrowly misses, try it anyway and free points
# by shaving a figure 5-10 pt or rebalancing a work line.
PAGE_BUDGET = 625

# height table (pt) used by est_question:
# [PATCH:formatter-spacing] updated for visible-white gaps:
#   body text line        LH (15)
#   sub-answer row        LABEL_H + LABGAP_C + SMALL_H (= 62.5)
#                         + RGAP_C (16) BETWEEN rows (not after the last)
#   compact x-t graph     ~160     compact step graph  ~150
#   key work line         WORK_LH  (+ WORK_GAP per work block)


def est_question(text_lines, n_parts, graph_h=0, extra_lines=0,
                 key_work_lines=0):
    """Estimated height (pt) of one rendered question.

    text_lines: wrapped lines of the stem; extra_lines: part-description lines
    under the stem; n_parts: boxed sub-answers (2-column rows, odd one rides
    alone); key_work_lines: >0 only when estimating the answer-key rendering.
    """
    h = text_lines * LH
    if extra_lines:
        h += extra_lines * (LH + 2)
    if graph_h:
        h += TGAP_C + graph_h + IMGAP_C
    else:
        h += TGAP_C
    r = (n_parts + 1) // 2
    if r:  # [PATCH:formatter-spacing] RGAP_C between rows only
        h += r * (LABEL_H + LABGAP_C + SMALL_H) + (r - 1) * RGAP_C
    if key_work_lines:
        h += WORK_GAP + key_work_lines * WORK_LH
    return h


def pack(heights, budget=PAGE_BUDGET, tol=12):
    """Greedy in-order packing -> list of lists of question indices.

    Estimate with the KEY heights when a key will be built, so student and key
    pagination match, and with MEASURED figure heights (render the PNGs first,
    image_dims gives the display height). Pair a graph question with a text
    question; two heavy (5-part) graph questions never share a page - the
    numbers enforce this. Reordering questions before packing is allowed and
    often what unlocks a page; renumber 1..n afterward.
    """
    pages, cur, used = [], [], 0.0
    for i, h in enumerate(heights):
        need = h if not cur else h + QGAP_C
        if cur and used + need > budget + tol:
            pages.append(cur)
            cur, used, need = [], 0.0, h
        cur.append(i)
        used += need
    if cur:
        pages.append(cur)
    return pages


# ---- rendering helpers ------------------------------------------------------
def sub_row(page, items, trailing_gap=True):
    """items: [(tag, label, answer-or-None)] x1 or x2, on the COL1/COL2 grid.

    [PATCH:formatter-spacing] the labels register as text, so each label
    sits LABGAP_C of VISIBLE white above its own box, and the row's boxes
    register as ink, so the next label or question drops correctly below
    them. trailing_gap=False lets rows() leave the gap after the LAST row
    to the caller (QGAP_C before the next question)."""
    from ledger_lib import text_descent
    base = page._begin_text("LSerif", BODY)
    for x, (tag, label, ans) in zip([COL1, COL2], items):
        page.c.setFont("LSerif", BODY)
        page.c.drawString(x, base, label)
    ytop = base - text_descent("LSerif", BODY) - LABGAP_C
    for x, (tag, label, ans) in zip([COL1, COL2], items):
        page.y = ytop
        page.box(SMALL_W, SMALL_H, tag, x=x)
        if ans is not None:
            page.c.setFont("LSerif", BODY)
            page.c.drawString(x + 14, ytop - SMALL_H / 2.0 - 4, ans)
    page._mark_ink(ytop - SMALL_H)
    if trailing_gap:
        page.gap(RGAP_C)


def rows(page, qnum, parts, answers=None):
    """parts: [(letter, label)]; answers (key only) parallel list of strings.

    [PATCH:formatter-spacing] RGAP_C goes BETWEEN rows only; after the last
    row the caller adds QGAP_C before the next question."""
    chunks = [parts[i:i + 2] for i in range(0, len(parts), 2)]
    for k, chunk in enumerate(chunks):
        sub_row(page, [(f"{qnum}{lt}", f"{lt}.  {lab}",
                        answers[2 * k + j] if answers else None)
                       for j, (lt, lab) in enumerate(chunk)],
                trailing_gap=(k < len(chunks) - 1))


def part_lines(page, lines, gap=2):
    """Short per-part prompts under a stem, wrapped at the question indent."""
    for txt in lines:
        base = page._begin_text("LSerif", BODY)  # [PATCH:formatter-spacing]
        for ln in wrap_plain(txt, CW - 22):
            page.c.setFont("LSerif", BODY)
            page.c.drawString(ML + 22, base, ln)
            base -= LH
        page._end_text(base + LH)
        page.gap(gap)


def work_block(page, lines):
    """Key-only worked solution, 9 pt, WORK_GAP of visible white under the
    question's last box. Raises if a line would cross the right margin -
    rebalance, don't shrink."""
    from ledger_lib import sw
    page.gap(WORK_GAP)                           # [PATCH:formatter-spacing]
    base = page._begin_text("LSerif", WORK_SIZE)
    for ln in lines:
        segs = rich_segs(ln, size=WORK_SIZE)
        w = sum(sw(t, f, s) for (t, f, s, r) in segs)
        if w > CW - 12:
            raise ValueError(f"work line {w:.0f} pt > {CW - 12:.0f}: {ln[:60]}")
        draw_segs(page.c, ML + 6, base, segs)
        base -= WORK_LH
    page._end_text(base + WORK_LH, "LSerif", WORK_SIZE, leading=WORK_LH)


def finish_page(page, lows=None):
    """MANDATORY page close: asserts footer clearance, records the low point."""
    if lows is not None:
        lows.append((page.num, round(page.y, 1)))
    assert page.y >= FLOOR, (
        f"page {page.num} content reached y={page.y:.1f} (< {FLOOR}); "
        "shrink a graph, rebalance work lines, or move a question")
    page.done()


# ---- house figure style (matplotlib), homework-textbook look ----------------
_FONTS_READY = False


def setup_graph_fonts():
    """Liberation Serif everywhere, true minus signs, italic math variables."""
    global _FONTS_READY
    if _FONTS_READY:
        return
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot as plt
    fd = "/usr/share/fonts/truetype/liberation/"
    for f in ("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf",
              "LiberationSerif-Italic.ttf", "LiberationSerif-BoldItalic.ttf"):
        font_manager.fontManager.addfont(fd + f)
    plt.rcParams.update({
        "font.family": "Liberation Serif",
        "mathtext.fontset": "custom",
        "mathtext.rm": "Liberation Serif",
        "mathtext.it": "Liberation Serif:italic",
        "axes.unicode_minus": True,
    })
    _FONTS_READY = True


def _frame(ax, xmax, ylim, xticks, yticks, xpad, ypad, yticks_minor=None):
    """Axes through zero, grid behind data, top/right spines off."""
    ax.set_xlim(-0.06 * xmax, xmax + xpad)
    ax.set_ylim(ylim[0] - 0.02 * (ylim[1] - ylim[0]), ylim[1] + ypad)
    if yticks_minor:
        ax.set_yticks(yticks_minor, minor=True)
        ax.grid(True, which="minor", color="0.90", linewidth=0.7)
        ax.tick_params(which="minor", length=0)
    ax.grid(True, which="major", color="0.84", linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_position(("data", 0))
    ax.spines["bottom"].set_position(("data", 0))
    for s in ("left", "bottom"):
        ax.spines[s].set_linewidth(1.1)
        ax.spines[s].set_color("black")
        ax.spines[s].set_zorder(3)
    ax.set_xticks(xticks)
    ax.set_yticks(yticks)
    ax.tick_params(axis="both", labelsize=13, length=4, width=1.1,
                   color="black")


def line_graph(path, verts, xmax, ylim, xlabel="$t$ (s)", ylabel="$x$ (m)",
               ystep=2, figsize=(3.4, 2.35)):
    """Piecewise-linear graph (e.g. x-t). verts on gridlines ONLY, so answers
    read exactly. Labeled y ticks thin to every other gridline when the y range
    is wide; the gridlines themselves stay at ystep."""
    setup_graph_fonts()
    from matplotlib import pyplot as plt
    fig, ax = plt.subplots(figsize=figsize, dpi=300)
    xticks = list(range(1, xmax + 1))
    full = [y for y in range(ylim[0], ylim[1] + 1, ystep) if y != 0]
    wide = (ylim[1] - ylim[0]) > 5 * ystep
    major = [y for y in full if (y % (2 * ystep) == 0)] if wide else full
    minor = [y for y in full if y not in major]
    _frame(ax, xmax, ylim, xticks, major, xpad=0.55, ypad=0.6,
           yticks_minor=minor)
    ax.plot([t for t, x in verts], [x for t, x in verts], color="black",
            linewidth=2.0, solid_joinstyle="miter",
            solid_capstyle="projecting", zorder=4, clip_on=False)
    ax.text(0.10, ylim[1] + 0.9, ylabel, fontsize=14, ha="left", va="bottom")
    ax.text(xmax + 0.62, -0.1, xlabel, fontsize=14, ha="left", va="center")
    fig.savefig(path, bbox_inches="tight", pad_inches=0.04, facecolor="white")
    plt.close(fig)


def step_graph(path, steps, xmax, ylim, xlabel="$t$ (s)",
               ylabel="$a$ (m/s$^2$)", xticks=None, figsize=(3.2, 2.3)):
    """Step profile with vertical risers (e.g. a-t). steps: [(t0, t1, value)]."""
    setup_graph_fonts()
    from matplotlib import pyplot as plt
    fig, ax = plt.subplots(figsize=figsize, dpi=300)
    if xticks is None:
        xticks = [x for x in range(5, xmax + 1, 5)]
    yticks = [y for y in range(ylim[0], ylim[1] + 1) if y != 0]
    _frame(ax, xmax, ylim, xticks, yticks, xpad=1.4, ypad=0.35)
    xs, ys = [steps[0][0]], [0]
    for (t0, t1, v) in steps:
        xs += [t0, t1]
        ys += [v, v]
    xs.append(steps[-1][1])
    ys.append(0)
    ax.plot(xs, ys, color="black", linewidth=2.0, solid_joinstyle="miter",
            solid_capstyle="projecting", zorder=4, clip_on=False)
    ax.text(0.25, ylim[1] + 0.45, ylabel, fontsize=14, ha="left", va="bottom")
    ax.text(xmax + 1.55, -0.02, xlabel, fontsize=14, ha="left", va="center")
    fig.savefig(path, bbox_inches="tight", pad_inches=0.04, facecolor="white")
    plt.close(fig)


def image_dims(path, display_w):
    """Display (w, h) preserving a PNG's aspect ratio."""
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
    return display_w, display_w * h / w
