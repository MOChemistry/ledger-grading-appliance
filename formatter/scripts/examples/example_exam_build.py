"""Build 'CHEMISTRY: Sample Exam' — MC I + FR I in Ledger format.
Invented sample content (no questions from any real exam); exercises every
exam widget. Continuous numbering: MC 1-10, FR 11-13 (tags 11a..13c)."""
from ledger_lib import Doc, CW, ML, sw, seg, sup, sub, wrap_plain, BODY, LH
import exam_widgets as X

OUT = "SAMPLE_Chemistry_Exam.pdf"
TITLE = "CHEMISTRY: Sample Exam"

d = Doc(OUT, TITLE)
FLOOR = 100          # keep lowest content edge at/above this y


def check(page, label):
    assert page.y >= FLOOR - 4, f"{label}: content too low (y={page.y:.0f})"


def segs_w(segs):
    return sum(sw(t, f, s) for (t, f, s, r) in segs)


def centered_segs(page, segs):
    x = ML + (CW - segs_w(segs)) / 2.0
    page._draw_segs(x, page.y, segs)
    page.y -= LH


def config(*parts):
    """('1s','2','2s','2',...) -> rich segs with superscripts."""
    out = []
    for i in range(0, len(parts), 2):
        out.append(seg(parts[i]))
        out.append(sup(parts[i + 1]))
    return out


def inline_opts(page, items, x0=None, gap=20):
    """One row of lettered options: items = [('A', segs), ...]."""
    x = ML + 22 if x0 is None else x0
    for letter, body in items:
        segs = [seg(f"({letter}) ")] + body
        page._draw_segs(x, page.y, segs)
        x += segs_w(segs) + gap
    page.y -= LH


def mc_bank_q(page, num, text):
    """Stem wrapped left, bubble strip right-aligned on the same rows."""
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
    X.bubble_strip(page, num, x=strip_x)
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

mc_bank_q(p, 1, "A measure of the attraction of an atom for the "
                "electrons it shares in a chemical bond.")
mc_bank_q(p, 2, "The energy change that occurs when a neutral atom in "
                "the gas phase gains an electron to form an anion.")
mc_bank_q(p, 3, "One-half the distance between the nuclei of two "
                "identical atoms that are bonded together.")

p.plain_line([seg("Questions 4 and 5 refer to the photoelectron "
                  "spectrum below.")])
p.gap(50)   # room for the peaks (max height 2 e- x 20 = 40) + headroom
X.pes_figure(p.c, ML + 70, p.y, CW - 140,
             [(0.22, 2, "11.5"), (0.80, 2, "0.90")], unit_h=20.0)
p.gap(38)   # value labels + caption below axis

p.q_text(4, "What is the electron configuration for this element?")
p.gap(2)
inline_opts(p, [
    ("A", config("1s", "2", "2s", "2")),
    ("B", config("1s", "2", "2s", "1")),
    ("C", config("1s", "2", "2s", "2", "2p", "2")),
    ("D", config("1s", "2", "2p", "2")),
    ("E", config("1s", "1", "2s", "1")),
])
p.gap(X.TGAP)
X.bubble_strip(p, 4)
p.gap(X.QGAP)

p.q_text(5, "Identify the element that would produce this spectrum.")
p.gap(2)
inline_opts(p, [("A", [seg("H")]), ("B", [seg("He")]), ("C", [seg("Li")]),
                ("D", [seg("Be")]), ("E", [seg("B")])])
p.gap(X.TGAP)
X.bubble_strip(p, 5)
check(p, "page 1")
p.done()

# ============================ PAGE 2 — MC ============================
p = d.page()
p.q_text(6, "What mass of hydrogen gas is produced when 4.0 mol of "
            "sodium react with excess water according to the process "
            "outlined below?")
p.gap(4)
centered_segs(p, [seg("2 Na + 2 H"), sub("2"), seg("O "), seg("→"),
                  seg(" 2 NaOH + H"), sub("2")])
p.gap(2)
inline_opts(p, [
    ("A", [seg("2.0 g H"), sub("2")]), ("B", [seg("4.0 g H"), sub("2")]),
    ("C", [seg("8.0 g H"), sub("2")]), ("D", [seg("16.0 g H"), sub("2")]),
    ("E", [seg("20.0 g H"), sub("2")])])
p.gap(X.TGAP)
X.bubble_strip(p, 6)
p.gap(X.QGAP)

p.plain_line([seg("Questions 7 – 10 refer to the orbital diagrams "
                  "below.")])
p.gap(14)
AR = [
    ("A", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "6", [2, 2, 2]),
           ("3s", "2", [2]), ("3p", "6", [2, 2, 2])]),
    ("B", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "4", [2, 1, 1])]),
    ("C", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "6", [2, 2, 2]),
           ("3s", "1", [1]), ("3p", "1", [1, 0, 0])]),
    ("D", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "6", [2, 2, 2]),
           ("3s", "2", [2]), ("3p", "6", [2, 2, 2]), ("4s", "2", [2]),
           ("3d", "8", [2, 2, 2, 1, 1])]),
    ("E", [("1s", "2", [2]), ("2s", "2", [2]), ("2p", "6", [2, 2, 2]),
           ("3s", "2", [2]), ("3p", "5", [2, 2, 1])]),
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
N_PEAKS = [(0.14, 2, "28.6"), (0.68, 2, "1.72"), (0.86, 2, "1.09")]
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
p.gap(96)   # headroom for peaks (max 2 e- x 26 = 52)
X.pes_figure(p.c, ML + 70, p.y, CW - 140, N_PEAKS, N_BREAKS, unit_h=26.0)
p.gap(46)

sub_text(p, "a", "Identify the element that would produce the above "
               "spectrum.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
X.id_box(p, "11a")
p.gap(X.QGAP)

sub_text(p, "b", "Label each peak on the spectrum with the numbers and "
               "letters that indicate its associated shell and subshell.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
X.fig_box(p, "11b", N_PEAKS, N_BREAKS)
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
X.fig_box(p, "11c", N_PEAKS, N_BREAKS)
p.gap(X.QGAP)

sub_text(p, "d", "Identify the quantum level (e.g., n = 1, n = 2, …) "
               "that is holding the valence electrons in this element.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
X.id_box(p, "11d")
p.gap(X.QGAP + 6)

p.rich_line(12, [seg("Rb"), sup("+"), seg(" and Se"), sup("2−"),
                 seg(" are isoelectronic.")])
p.gap(6)
sub_text(p, "a", "Which ion has the smaller radius?")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
X.id_box(p, "12a")
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
X.lined_box_fa(p, "12b", fa=False)
p.gap(X.QGAP + 12)

p.q_text(13, "The following questions pertain to titanium.")
p.gap(6)
sub_text(p, "a", "Write the complete ground-state electron configuration "
               "for titanium, Ti.")
X.pts_line(p, PTS1, indent=40)
p.gap(X.TGAP)
X.id_box(p, "13a", long=True)
check(p, "page 5")
p.done()

# ============================ PAGE 6 — FR ============================
p = d.page()
p.plain_line([("(Question 13, continued)", "LSerif-I", 10, 0)])
p.gap(8)

sub_text(p, "b", "How many unpaired electrons does titanium have in its "
               "ground state?  Justify your answer.")
X.pts_line(p, "One point for the correct answer; one point for the "
              "justification.  These points can be earned even if part (a) "
              "was incorrect, so long as your answer is consistent with "
              "the configuration you gave in part (a).", indent=40)
p.gap(X.TGAP)
X.sketch_box(p, "13b", fa=True, fa_hint="NUMBER")
p.gap(X.QGAP)

sub_text(p, "c", "Is a single gaseous titanium atom paramagnetic or "
               "diamagnetic?  Justify your answer.")
X.pts_line(p, "One point for the correct answer; one point for the "
              "justification.", indent=40)
p.gap(X.TGAP)
X.lined_box_fa(p, "13c", fa=True, fa_hint="PARAMAGNETIC / DIAMAGNETIC")
check(p, "page 6")
p.done()

d.save()
print("OK", OUT)
