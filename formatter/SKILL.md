---
name: "ledger-worksheet"
description: Create worksheets, quizzes, homework, exams, practice exams, and answer keys in "The Ledger" format — the strict machine-gradable layout (Bartonian ID header, tagged answer boxes, black-square page numbers, Liberation Serif) used by the Ledger AI grading appliance. Covers BOTH the compact worksheet/quiz density AND the exam conventions (Scantron bubble-strip multiple choice, section banners, response-type free-response boxes with FINAL ANSWER cells, proportional PES figures). Use this skill EVERY time the user asks for a worksheet, quiz, test, exam, homework, assessment, practice form, or answer key on any subject, whether they say "Ledger", "Bartonian", or not. Applies to reformatting supplied source questions AND authoring from scratch. Supersedes the separate ledger-ap-chemistry skill. Never hand-build this layout without the skill.
---

# Ledger Worksheet & Exam Generator (rev. Oct 2026, merged)

Produces PDF worksheets, quizzes, exams, practice exams, and answer keys in the
exact Ledger format. ALL layout code here is author-approved output from real,
accepted deliverables — always import and use it; never re-derive the layout.
This revision merges the former `ledger-worksheet` (compact density, graph
figures, font-audit-clean engine) and `ledger-ap-chemistry` (exam widgets,
practice-exam policy) skills into one.

## Workflow
1. Read `references/format-spec.md` (hard rules + verification checklist).
2. Establish: exact title string; source questions vs. from-scratch; continuous
   numbering 1..n (renumber when merging/omitting source questions; on exams
   MC first, then FR, with FR sub-parts tagged `11a`, `11b`, …). Fix source
   typos rather than reproducing them.
3. Pick the DOCUMENT MODE:
   - **Worksheet / quiz / homework** → `widgets.py` boxes, **compact density
     by default** (several questions per page, students work on scratch
     paper). Spacious (one question per page) only on request or when work
     must be shown on the page. If unsure on a quiz, ask once.
   - **Exam / practice exam** → `exam_widgets.py`: `section_banner()`, MC
     `bubble_strip()` strips (dense pages), FR boxes chosen by RESPONSE TYPE
     from the table in the format spec. Keep the lowest content edge at
     y ≥ 100 and assert it after every page.
   Both modes share `ledger_lib` and all non-negotiables; the two widget sets
   may be mixed in one document when the content calls for it.
4. Compact worksheets only: estimate every question with `compact.est_question`
   (use the KEY estimate when a key will be built, so student and key
   pagination match) and plan pages with `compact.pack`. Reorder/renumber so a
   graph-heavy question shares its page with a text question.
5. Choose box types/sizes ONLY from the constants in `widgets.py` /
   `exam_widgets.py` — identical for every box of a type document-wide. Density
   never shrinks a box or leaves the COL1/COL2 grid. Gap values (QGAP, TGAP,
   RGAP, the _C constants) are VISIBLE WHITE SPACE on the page — the engine
   places elements ink edge to ink edge (rev. Nov 2026; an exemplar PDF that
   predates this shows tighter gaps below boxes). Use the library text
   paths (q_text / plain_line / rich_line / sub_row / part_lines) for every
   label and heading, never raw canvas drawString, so below-box spacing
   stays correct.
6. Verify ALL content computationally/factually BEFORE typesetting: numbers,
   sig figs, balanced equations, configurations, PES values (table in the
   spec), mental-math-friendly values where calculators are banned.
7. Write a build script importing `ledger_lib` + the widget module(s) (+
   `compact` for compact density, `key_lib` for keys). Copy the scripts into
   the working directory. Start exams from `scripts/examples/
   example_exam_build.py`; worksheets from `scripts/example_build.py`. Figures
   for compact pages come from `compact.line_graph` / `compact.step_graph`
   (vertices on gridlines only). Plan page breaks manually; never split a
   question's sub-parts (text, point line, box) across pages; orbital-diagram
   questions get one page each; a continued question opens its new page with
   an italic `(Question N, continued)` line.
8. PRACTICE EXAM POLICY — ALWAYS for exams: deliver every real exam together
   with a parallel form titled `<same title> Practice Exam`, identical in every
   structural way with ALL identities, data, and answers swapped (details in
   the format spec).
9. Answer keys only when asked (The Ledger needs no key): identical layout and
   pagination, title gains "(KEY)", answers inserted where the student writes
   them; MC bubbles filled. Verify every number before typesetting; follow the
   sig-fig rules in the spec. Exam keys follow
   `scripts/examples/example_key_build.py`; worksheet keys use `key_lib` +
   `compact.work_block`.
10. Close every page with `compact.finish_page` (asserts footer clearance,
    records the per-page low point — print the lows), then the MANDATORY
    verification pass (rasterize + zoom checks per the spec, on EVERY form
    produced), fix, re-render, and present every PDF.

## Files
- `scripts/ledger_lib.py` — core engine: Doc/Page, header/footer, boxes, tags.
  Canvas is initialised with the embedded Liberation Serif so PDFs pass
  strict font audits (no stray Helvetica reference).
- `scripts/widgets.py` — worksheet standards: LONG/SHORT/SMALL boxes, column
  grid, orbital-diagram template, Bohr shell figures, full-page grid pages.
- `scripts/exam_widgets.py` — exam widgets: bubble_strip, section_banner,
  pts_line, id_box, work_box, sketch_box, lined_box_fa, fig_box, pes_figure
  (proportional peaks), orbital_rows, exam sizing constants.
- `scripts/compact.py` — compact density: spacing constants, page-budget
  estimator/packer, sub-answer rows, key work blocks, footer-clearance
  assertion (`finish_page`), house matplotlib figure style (line/step graphs).
- `scripts/key_lib.py` — answer-key helpers: work panel, to-scale vector
  diagrams, filled Final Answer boxes.
- `scripts/example_build.py` — minimal worksheet example.
- `scripts/examples/` — the three sample exam build scripts (exam, practice
  form, key), with invented sample content. Start new exams from these.
- `references/format-spec.md` — the hard rules; read first, verify against last.
- `references/exemplars/` — FINISHED, accepted PDFs to match visually:
  SAMPLE_Chemistry Exam / Practice Exam / Practice Exam KEY (exam mode; the
  KEY also shows the key convention),
  AP_Chemistry_Atomic_Theory_III.pdf (standard worksheet: config boxes,
  orbital diagrams, shell figures, lined boxes, column grid),
  QUIZ_Vectors.pdf (full-page grid convention). Rasterize and view the
  relevant ones before your first build.
