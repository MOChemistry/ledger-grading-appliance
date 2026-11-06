"""Build 'CHEMISTRY: Sample Practice Exam (KEY)' — identical layout to
the sample practice exam, with scoring-guideline-style answers inserted.
Invented sample content (no questions or answers from any real exam)."""
from ledger_lib import Doc, CW, ML, sw, seg, sup, sub, wrap_plain, BODY, LH
import exam_widgets as X

OUT = "SAMPLE_Chemistry_Practice_Exam_KEY.pdf"
TITLE = "CHEMISTRY: Sample Practice Exam (KEY)"

d = Doc(OUT, TITLE)
FLOOR = 100

MC_KEY = {1: "A", 2: "B", 3: "E", 4: "B", 5: "C",
          6: "B", 7: "A", 8: "B", 9: "C", 10: "B"}


def check(page, label):
    assert page.y >= FLOOR - 4, f"{label}: content too low (y={page.y:.0f})"


def segs_w(segs):
    return sum(sw(t, f, s) for (t, f, s, r) in segs)


def centered_segs(page, segs):
    x = ML + (CW - segs_w(segs)) / 2.0
    page._draw_segs(x, page.y, segs)
    page.y -= LH


def config(*parts):
    out = []
    for i in range(0, len(parts), 2):
        out.append(seg(parts[i]))
        out.append(sup(parts[i + 1]))
    return out


def inline_opts(page, items, x0=None, gap=20):
    x = ML + 22 if x0 is None else x0
    for letter, body in items:
        segs = [seg(f"({letter}) ")] + body
        page._draw_segs(x, page.y, segs)
        x += segs_w(segs) + gap
    page.y -= LH


# ---------------- KEY helpers ----------------
def key_bubble(page, num, x=None):
    """Draw the strip, then fill the correct bubble (black disc inside
    the ring, Scantron style)."""
    y_top = page.y
    X.bubble_strip(page, num, x=x)
    xx = (ML + CW - X.MC_W) if x is None else x
    i = "ABCDE".index(MC_KEY[int(num)])
    pitch = (X.MC_W - 2 * 28) / 4.0
    cx = xx + 28 + i * pitch
    cy = y_top - X.MC_H / 2.0
    c = page.c
    c.setFillColorRGB(0, 0, 0)
    c.circle(cx, cy, 5.2, stroke=0, fill=1)


def center_in_box(page, segs, x, w, y_top, h, size=12):
    scaled = [(t, f, size if s == BODY else s, r) for (t, f, s, r) in segs]
    tx = x + (w - segs_w(scaled)) / 2.0
    page._draw_segs(tx, y_top - h / 2.0 - 4, scaled)


def fill_fa(page, y_top, h, text):
    fx = ML + CW - X.FA_INSET - X.FA_W
    fy = y_top - h + X.FA_INSET
    page.c.setFillColorRGB(0, 0, 0)
    page.c.setFont("LSerif", 12)
    page.c.drawCentredString(fx + X.FA_W / 2.0, fy + 16, text)


def lined_text(page, y_top, lines, x=None, size=11):
    """Write answer lines sitting on a lined box's rules."""
    if x is None:
        x = ML + 14
    for i, segs in enumerate(lines):
        ly = y_top - 35.5 - i * 24
        page._draw_segs(x, ly + 3, [(t, f, size if s == BODY else s, r)
                                    for (t, f, s, r) in segs])


def label_peaks(page, y_top, h, labels, peaks, fig_w=330, unit_h=9.0):
    """Write key labels above each peak inside a fig_box."""
    fx = ML + (CW - fig_w) / 2.0
    fy = y_top - h + 46
    c = page.c
    c.setFillColorRGB(0, 0, 0)
    c.setFont("LSerif", 11)
    for (frac, n_e, _val), lab in zip(peaks, labels):
        px = fx + frac * fig_w
        c.drawCentredString(px, fy + n_e * unit_h + 8, lab)


def mc_bank_q(page, num, text):
    strip_x = ML + CW - X.MC_W
    wrap_w = CW - X.MC_W - 40 - 22
    y_top = page.y
    page.c.setFont("LSerif", BODY)
    page.c.drawString(ML, y_top, f"{num})")
    lines = wrap_plain(text, wrap_w)
    yy = y_top
    for ln in lines:
        page.c.setFont("LSerif", BODY)
        page.c.drawString(ML + 22, yy, ln)
        yy -= LH
    stem_h = len(lines) * LH
    page.y = y_top + 10
    key_bubble(page, num, x=strip_x)
    page.y = min(y_top - stem_h, y_top + 10 - X.MC_H) - X.QGAP


def sub_text(page, letter, text, width=None):
    # [PATCH:formatter-spacing] registers as text so a box after it sits at
    # the visible gap, and so it drops below a box by its cap height.
    base = page._begin_text("LSerif", BODY)
    page.c.setFont("LSerif", BODY)
    page.c.drawString(ML + 18, base, f"{letter}.")
    for ln in wrap_plain(text, (CW - 40) if width is None else width):
        page.c.setFont("LSerif", BODY)
        page.c.drawString(ML + 40, base, ln)
        base -= LH
    page._end_text(base + LH)


PTS1 = "One point for the correct answer."

# ============================ PAGE 1 — MC ============================
p = d.page()
X.section_banner(p, "SECTION I — MULTIPLE CHOICE", [
    ("LSerif-B", 9, "CALCULATORS CANNOT BE USED IN THIS SECTION"),
    ("LSerif-I", 9,
     "Each question is worth one point: one point for the correct answer.  "
     "Completely fill in one bubble per question."),
])
p.gap(14)

p.plain_line([seg("Questions 1 – 3 refer to the following terms.")])
COL1, COL2 = ML + 40, ML + 40 + CW // 2
terms = [("(A) Electron Affinity", "(D) Electronegativity"),
         ("(B) Shielding Effect", "(E) Atomic Radius"),
         ("(C) Ionization Energy", None)]
for left, right in terms:
    p.c.setFont("LSerif", BODY)
    p.c.drawString(COL1, p.y, left)
    if right:
        p.c.drawString(COL2, p.y, right)
    p.y -= LH
p.gap(6)

mc_bank_q(p, 1, "The energy change that occurs when a neutral atom in "
                "the gas phase gains an electron to form an anion.")
mc_bank_q(p, 2, "The reduction of the attractive force between the "
                "nucleus and the valence electrons, caused by the inner "
                "(core) electrons.")
mc_bank_q(p, 3, "One-half the distance between the nuclei of two "
                "identical atoms that are bonded together.")

p.plain_line([seg("Questions 4 and 5 refer to the photoelectron "
                  "spectrum below.")])
p.gap(50)
X.pes_figure(p.c, ML + 70, p.y, CW - 140,
             [(0.22, 2, "6.26"), (0.80, 1, "0.52")], unit_h=20.0)
p.gap(38)

p.q_text(4, "What is the electron configuration for this element?")
p.gap(2)
inline_opts(p, [
    ("A", config("1s", "2", "2s", "2")),
    ("B", config("1s", "2", "2s", "1")),
    ("C", config("1s", "2", "2s", "2", "2p", "1")),
    ("D", config("1s", "2", "2p", "1")),
    ("E", config("1s", "1", "2s", "2")),
])
p.gap(X.TGAP)
key_bubble(p, 4)
p.gap(X.QGAP)

p.q_text(5, "Identify the element that would produce this spectrum.")
p.gap(2)
inline_opts(p, [("A", [seg("H")]), ("B", [seg("He")]), ("C", [seg("Li")]),
                ("D", [seg("Be")]), ("E", [seg("B")])])
p.gap(X.TGAP)
key_bubble(p, 5)
check(p, "page 1")
p.done()

# ============================ PAGE 2 — MC ============================
p = d.page()
p.q_text(6, "What mass of hydrogen gas is produced when 3.0 mol of "
            "magnesium react with excess hydrochloric acid according to "
            "the process outlined below?")
p.gap(4)
centered_segs(p, [seg("Mg + 2 HCl "), seg("→"), seg(" MgCl"),
                  sub("2"), seg(" + H"), sub("2")])
p.gap(2)
inline_opts(p, [
    ("A", [seg("3.0 g H"), sub("2")]), ("B", [seg("6.0 g H"), sub("2")]),
    ("C", [seg("9.0 g H"), sub("2")]), ("D", [seg("12.0 g H"), sub("2")]),
    ("E", [seg("18.0 g H"), sub("2")])])
p.gap(X.TGAP)
key_bubble(p, 6)
p.gap(X.QGAP)

p.plain_line([seg("Questions 7 – 10 refer to the orbital diagrams "
                  "below.")])
p.gap(14)
AR = [
    ("A", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "6", [2, 2, 2]),
           ("3s", "2", [2]), ("3p", "4", [2, 1, 1])]),
    ("B", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "6", [2, 2, 2])]),
    ("C", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "5", [2, 2, 1]),
           ("3s", "1", [1])]),
    ("D", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "6", [2, 2, 2]),
           ("3s", "2", [2]), ("3p", "6", [2, 2, 2]), ("4s", "2", [2]),
           ("3d", "5", [1, 1, 1, 1, 1])]),
    ("E", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "3", [1, 1, 1])]),
]
X.orbital_rows(p, AR, row_h=42)
p.gap(10)

mc_bank_q(p, 7, "Represents an atom with six valence electrons.")
mc_bank_q(p, 8, "Does not violate any rules for filling orbitals and "
                "would not be attracted to a magnetic field.")
mc_bank_q(p, 9, "Is in an excited state.")
mc_bank_q(p, 10, "Represents a noble gas.")
check(p, "page 2")
p.done()

# ============================ PAGE 3 — FR ============================
N_PEAKS = [(0.14, 2, "52.6"), (0.68, 2, "3.12"), (0.86, 4, "1.31")]
N_BREAKS = (0.42,)

p = d.page()
X.section_banner(p, "SECTION II — FREE RESPONSE", [
    ("LSerif-I", 9,
     "Show your work in the open area of each box; write only your final "
     "answer in the white FINAL ANSWER cell where provided."),
    ("LSerif-I", 9, "Point values for each part are printed beneath the "
                    "question."),
])
p.gap(14)

p.q_text(11, "Use the photoelectron spectrum below to answer parts "
             "(a) – (d).")
p.gap(96)
X.pes_figure(p.c, ML + 70, p.y, CW - 140, N_PEAKS, N_BREAKS, unit_h=15.0)
p.gap(46)

sub_text(p, "a", "Identify the element that would produce the above "
               "spectrum.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.id_box(p, "11a")
center_in_box(p, [seg("Oxygen (O)")], X.COL1, X.ID_SHORT_W,
              y_top, X.ID_SHORT_H)
p.gap(X.QGAP)

sub_text(p, "b", "Label each peak on the spectrum with the numbers and "
               "letters that indicate its associated shell and subshell.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.fig_box(p, "11b", N_PEAKS, N_BREAKS, unit_h=9.0)
label_peaks(p, y_top, X.FR_FIG_H, ["1s", "2s", "2p"], N_PEAKS)
check(p, "page 3")
p.done()

# ============================ PAGE 4 — FR ============================
p = d.page()
p.plain_line([("(Question 11, continued)", "LSerif-I", 10, 0)])
p.gap(8)

sub_text(p, "c", "Indicate the number of electrons that are associated "
               "with each peak on the photoelectron spectrum.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.fig_box(p, "11c", N_PEAKS, N_BREAKS, unit_h=9.0)
label_peaks(p, y_top, X.FR_FIG_H,
            ["2 e−", "2 e−", "4 e−"], N_PEAKS)
p.gap(X.QGAP)

sub_text(p, "d", "Identify the quantum level (e.g., n = 1, n = 2, …) "
               "that is holding the valence electrons in this element.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.id_box(p, "11d")
center_in_box(p, [seg("n = 2")], X.COL1, X.ID_SHORT_W, y_top,
              X.ID_SHORT_H)
p.gap(X.QGAP + 6)

p.rich_line(12, [seg("K"), sup("+"), seg(" and S"), sup("2−"),
                 seg(" are isoelectronic.")])
p.gap(6)
sub_text(p, "a", "Which ion has the smaller radius?")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.id_box(p, "12a")
center_in_box(p, [seg("K"), sup("+")], X.COL1, X.ID_SHORT_W, y_top,
              X.ID_SHORT_H)
check(p, "page 4")
p.done()

# ============================ PAGE 5 — FR ============================
p = d.page()
p.plain_line([("(Question 12, continued)", "LSerif-I", 10, 0)])
p.gap(8)

sub_text(p, "b", "Explain why the radii of these two ions are different "
               "sizes.")
X.pts_line(p, "One point for the correct explanation.", indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.lined_box_fa(p, "12b", fa=False)
lined_text(p, y_top, [
    [seg("K"), sup("+"), seg(" and S"), sup("2−"),
     seg(" are isoelectronic: each ion has 18 electrons.")],
    [seg("However, K"), sup("+"), seg(" has 19 protons, while S"),
     sup("2−"), seg(" has only 16.")],
    [seg("By Coulomb’s law, the greater nuclear charge of K"),
     sup("+"), seg(" attracts the same")],
    [seg("number of electrons more strongly, pulling them closer to "
         "the nucleus.")],
    [seg("Therefore K"), sup("+"),
     seg(" has the smaller ionic radius.")],
])
p.gap(X.QGAP + 12)

p.q_text(13, "The following questions pertain to calcium.")
p.gap(6)
sub_text(p, "a", "Write the complete ground-state electron configuration "
               "for calcium, Ca.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.id_box(p, "13a", long=True)
center_in_box(p, config("1s", "2", "2s", "2", "2p", "6", "3s", "2",
                        "3p", "6", "4s", "2"),
              ML + (CW - X.ID_LONG_W) / 2.0, X.ID_LONG_W, y_top,
              X.ID_LONG_H)
check(p, "page 5")
p.done()

# ============================ PAGE 6 — FR ============================
p = d.page()
p.plain_line([("(Question 13, continued)", "LSerif-I", 10, 0)])
p.gap(8)

sub_text(p, "b", "How many unpaired electrons does calcium have in its "
               "ground state?  Justify your answer.")
X.pts_line(p, "One point for the correct answer; one point for the "
              "justification.  These points can be earned even if part (a) "
              "was incorrect, so long as your answer is consistent with "
              "the configuration you gave in part (a).", indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.sketch_box(p, "13b", fa=True, fa_hint="NUMBER")
y_save = p.y
p.y = y_top - 52
X.orbital_rows(p, [("Ca", [("1s", "2", [2]), ("2s", "2", [2]),
                           ("2p", "6", [2, 2, 2]), ("3s", "2", [2]),
                           ("3p", "6", [2, 2, 2]), ("4s", "2", [2])])],
               row_h=42)
p.y = y_top - 122
p.c.setFont("LSerif", 11)
p.c.drawString(ML + 14, p.y,
               "Every sublevel through 4s is completely filled, so all of "
               "calcium’s electrons are paired.")
p.y -= 16
p.c.drawString(ML + 14, p.y, "Calcium has zero unpaired electrons.")
p._mark_ink(y_save)  # [PATCH:formatter-spacing] keep box state
fill_fa(p, y_top, X.FR_SKETCH_H, "0  (zero unpaired electrons)")
p.gap(X.QGAP)

sub_text(p, "c", "Is a single gaseous calcium atom paramagnetic or "
               "diamagnetic?  Justify your answer.")
X.pts_line(p, "One point for the correct answer; one point for the "
              "justification.", indent=40)
p.gap(X.TGAP)
y_top = p._ink_top()  # [PATCH:formatter-spacing] true box top
X.lined_box_fa(p, "13c", fa=True, fa_hint="PARAMAGNETIC / DIAMAGNETIC")
lined_text(p, y_top, [
    [seg("Calcium has no unpaired electrons: every sublevel in its "
         "ground-state configuration")],
    [seg("is completely filled, so the magnetic moments of the paired "
         "electrons cancel.")],
    [seg("With no net magnetic moment, a gaseous Ca atom is not "
         "attracted into a")],
    [seg("magnetic field.")],
])
fill_fa(p, y_top, X.FR_LINED_H, "Diamagnetic")
check(p, "page 6")
p.done()

d.save()
print("OK", OUT)
