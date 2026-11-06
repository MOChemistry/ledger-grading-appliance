# The Ledger Format — Hard Requirements (rev. Oct 2026, merged)

Author-verified rules. `ledger_lib.py`, `widgets.py`, `compact.py` and
`exam_widgets.py` implement all of them — this file exists for verification,
not re-implementation.

## Non-negotiables (author has corrected each of these in past runs)
1. **Liberation Serif for everything** — body, tags, page numbers, figure text.
2. **Page numbers NOT bold**: white regular digit on a snug 13 pt black square,
   centered at (306, 34), IDENTICAL coordinates on every page.
3. **Footer clearance**: the Ledger crops the footer region and fails if any
   stray line enters the crop. Keep the footer well away (≥ ~30 pt) from the
   lowest answer-box edge. Never let content approach y < 70. Every page closes
   through `compact.finish_page`, which asserts y ≥ 72 and records the low
   point — print the per-page lows with the build. On exams keep the lowest
   content edge at y ≥ 100 and assert it after every page.
4. **Tags connect seamlessly to box corners** — black tag drawn flush over the
   outer top-left corner of the border; zero white gap.
5. **Same-type answer boxes are IDENTICAL in size document-wide.** Use the
   constants in widgets.py / exam_widgets.py. Never improvise sizes — in
   compact mode too; density never changes a box.
6. **Sub-part boxes sit on the fixed column grid** (COL1/COL2); label and box
   share the same left edge; single leftover items go in COL1, never offset.
7. **Spacing — every gap is VISIBLE WHITE SPACE** (rev. Nov 2026): gap()
   values are measured ink edge to ink edge on the rendered page — below a
   box, from the bottom border to the top of the next capital letters; above
   a box, from the bottom of the text to the border. The engine does this
   placement itself; do NOT add compensation of your own. The values:
   spacious/exam QGAP=22 / TGAP=10 / RGAP=16; compact QGAP_C=22 / TGAP_C=10 /
   RGAP_C=16 / LABGAP_C=10 (compact.py). Below a box there is ALWAYS at
   least 6 pt more white than between a label and its own box, so a reader
   sees at a glance which box each label owns. Draw every label and heading
   with the library text paths (q_text / plain_line / rich_line / sub_row /
   part_lines / pts_line / section_banner), never with raw canvas
   drawString, or the engine cannot keep this rule and the spacing check
   will fail the page. Never go tighter than the compact values — jammed
   spacing reads as unprofessional, and so do half-empty pages: spread
   leftover space with deliberate gaps. NOTE: if an exemplar PDF in
   references/exemplars/ shows tighter spacing below boxes, it predates
   this rule — follow these numbers, not an exemplar's below-box gaps.
8. **Content centered**: symmetric 72 pt margins; boxes/figures centered
   (sub-part boxes on the column grid; full-width exam boxes run margin to
   margin; ID short boxes sit on COL1).

## Page anatomy
Letter, 1" margins. Header every page: bold title left, `Bartonian ID _______`
bold right; centered small bold instruction line with "final answers only"
bold-italic underlined and "Bartonian ID" bold-italic:
`Write neatly ◦ Boxes are reserved for final answers only ◦ Bartonian ID must be written on each page ◦ Do not staple.`

## Document modes
**Worksheet / quiz / homework** (widgets.py, compact.py): numbered questions
with sub-part boxes on the column grid; compact density by default.
**Exam / practice exam** (exam_widgets.py): section banners, Scantron-style MC
bubble strips, free-response boxes chosen by response type. The modes share
the engine and every rule above and may be mixed in one document.

## Density (worksheet mode)
Compact (default): multiple questions per page, figures ~200–215 pt wide,
students work on scratch paper. Spacious (on request, or when work must be
shown on the page): one question per page, figures ~285–330 pt wide. Page
budget for packing: ~625 pt of content below the header. Estimates carry
~±15 pt, so the plan is a draft and the finish_page assertion is the hard
gate; a near-miss pairing is worth trying, freed by shaving a figure 5–10 pt.
Heights for `compact.est_question`: body line 15; part-prompt line 17; boxed
sub-answer row 62.5 + 16 between rows (pair per row, odd one rides alone in
COL1); compact line graph ~160 + 22 figure gaps; compact step graph ~150 +
22; key work line 10.5 + 14 per block. Pack with the KEY estimates when a key will be built, so
both renderings paginate identically. Pair graph questions with text
questions; two 5-part graph questions never share a page.

## Exam structure (exam mode)
- MC section first, then free response. Continuous numbering across the exam
  (MC 1–10, FR 11…); FR sub-parts tag `11a`, `11b`, … so every tag is unique.
- Each section opens with `section_banner()`: centered bold title
  (`SECTION I — MULTIPLE CHOICE`), then small bold/italic instruction lines.
  Preserve source instructions like "CALCULATORS CANNOT BE USED IN THIS
  SECTION".
- Point values: MC stated ONCE in the section banner ("Each question is worth
  one point…"). FR parts each get an italic 10 pt `pts_line()` under the
  question text ("One point for the correct answer; one point for the
  justification."). The rubric lives in the question instructions — never
  reveal answers. No LO tags.
- When a question's sub-parts continue onto a new page, open the page with an
  italic `(Question N, continued)` line.

### Multiple choice — bubble strips, dense pages
- `bubble_strip()` draws the tagged box with circles Ⓐ–Ⓔ the student fills
  in. Many MC questions per page is CORRECT — never two-per-page padding.
- Strips all sit in one consistent right-hand column (x = ML+CW−MC_W).
  Bank-style questions (several stems sharing one option list or figure):
  strip right of the stem, top-aligned, stem wrapped to the narrower width.
  Option-list questions: options on compact inline row(s) under the stem,
  strip on its own row below, still right-aligned. Keep inline option rows
  inside the right margin — wrap rather than overflow.
- Term banks print once, in two columns on the COL1/COL2 grid.

### Free response — pick the box by RESPONSE TYPE (sizes identical doc-wide)
| Response type | Widget | Size |
|---|---|---|
| identification (one word/value) | `id_box()` at COL1 | 320×50 |
| long symbolic answer (e⁻ config) | `id_box(long=True)` centered | 460×54 |
| numeric/derivation work | `work_box()` full-width | CW×200 |
| draw-to-justify (orbital diagram etc.) | `sketch_box()` full-width | CW×240 |
| verbal explanation only | `lined_box_fa(fa=False)` | CW×220 |
| verbal justification + short answer | `lined_box_fa(fa=True, fa_hint=…)` | CW×220 |
| label a printed figure | `fig_box()` | CW×200 |
Read what each part REALLY asks for: "How many unpaired electrons? Justify"
is a draw-to-justify item, not a short-answer item. Identification items get
answer-only boxes — no work hint, no nested cell: the box IS the final
answer. Draw-to-justify boxes are OPEN (lines fight drawings); lined boxes
are ONLY for verbal responses. When in doubt on space, go bigger — but keep
box AREA modest overall, because grading cost scales with box area.
FINAL ANSWER cells (240×48, white, bottom-right, overlaying lines where
present) take a gray right-aligned hint naming the expected answer form:
"NUMBER", "PARAMAGNETIC / DIAMAGNETIC", "LETTER", "ELEMENT", or the unit.

## Boxes (all modes)
Final answers only go in boxes; work is shown in open page space (in compact
density that space is deliberately small — scratch paper carries the work) or
in the open area of exam work/sketch boxes, with the nested white FINAL
ANSWER cell carrying the graded answer.
- lined=True ONLY for verbal (non-math) responses
- grid boxes when drawing/graphing is required
- Full-page grid convention (vector-style quizzes): question tag on the OUTER
  grid box only; nested white Final Answer box bottom-right, ONE combined box.

## Sub/superscripts and notation
Rich segments only (never Unicode sub/superscript characters). Charges use
the true minus sign U+2212. Configurations via the `config()` helper pattern
in the example exam builds.

## Figures
Recreate faithfully; preserve source data labels verbatim including trailing
zeros ("0.2830"). Graphs students read values from: every vertex/step edge ON
a gridline; in compact sizes, labeled ticks may thin to every other gridline
(gridlines stay) — `compact.line_graph` / `compact.step_graph` do this.
Orbital diagrams: one per page (never split a/b across pages), any density.

### Chemistry figures (PES, orbital banks)
- PES (photoelectron) spectra: peak heights STRICTLY proportional to electron
  count — 2p⁶ is three times the height of 2s²; 1s²:2s¹ is 2:1. Never copy a
  source figure's wrong heights. `pes_figure()` enforces this
  (height = n_electrons × unit_h; scale unit_h so the tallest peak fits its
  headroom). Keep axis values verbatim with axis-break marks (») where energy
  gaps are compressed.
- Authentic PES binding energies (MJ/mol), for authoring and practice swaps:
  H 1.31 | He 2.37 | Li 6.26, 0.52 | Be 11.5, 0.90 | B 19.3, 1.36, 0.80 |
  C 28.6, 1.72, 1.09 | N 39.6, 2.45, 1.40 | O 52.6, 3.12, 1.31 |
  F 67.2, 3.88, 1.68 | Ne 84.0, 4.68, 2.08 | Na 104, 6.84, 3.67, 0.50 |
  Mg 126, 9.07, 5.31, 0.74 | Al 151, 12.1, 7.19, 1.09, 0.58 |
  Si 178, 15.1, 10.3, 1.46, 0.79 | P 208, 18.7, 13.5, 1.95, 1.01 |
  S 239, 22.7, 16.5, 2.05, 1.00 | Cl 273, 26.8, 20.2, 2.44, 1.36 |
  Ar 309, 31.5, 24.1, 2.82, 1.52
- For label-the-figure FR parts, reproduce the spectrum INSIDE the tagged
  answer box (`fig_box`) so the student labels the copy.
- Printed orbital-diagram banks (rows A–E): `orbital_rows()`. When
  reformatting, transcribe source arrow patterns exactly (zoom the source
  raster first). When authoring, include the classic distractors: a Hund
  violation, an excited state, a noble gas, a valid paramagnetic atom.

## Practice exam policy — ALWAYS (exam mode)
Every real exam is delivered TOGETHER WITH a parallel form titled
`<same title> Practice Exam`. Identical in EVERY structural way — sections,
question count, question types, wording patterns, box types/sizes, page
plan — with ALL identities, data, and answers swapped so memorized answers do
not transfer:
- different elements/ions/compounds/objects/numbers and a different balanced
  reaction or scenario;
- correct answers land on DIFFERENT letters/values than the real exam
  wherever possible; flip qualitative answers too (real exam's element
  paramagnetic with 3 unpaired → pick a diamagnetic one, e.g., As → Zn);
- shared option banks (terms, elements) may stay; the stems move the correct
  answers;
- verify all practice content before typesetting (PES table above, balanced
  equations, configurations, mental-math numbers where calculators are
  banned, numeric answers recomputed).

## Answer keys (only when asked — The Ledger needs no key)
Identical page layout and pagination to the student document; title gains
"(KEY)". MC: correct bubble filled (black disc inside the ring). FR: answers
inserted where the student would write them — centered in ID boxes, labels
drawn above the peaks in fig boxes, explanation written ON the lines of lined
boxes, drawn orbital diagram + one-line conclusion in sketch boxes, FINAL
ANSWER cells filled. Phrase answers scoring-guideline style: the claim plus
the causal reasoning that earns the point. Keep lined-box text to the upper
lines so it clears the FINAL ANSWER cell. Worksheet keys (key_lib.py,
compact.py): full-page grid problems get a work block on a white panel, a
to-scale diagram with stated scale + labeled legs + dashed R, and the final
answer filled in; standard compact questions get answers inside the boxes and
a 9 pt `compact.work_block` under the question's last row (it raises if a
line would cross the right margin — rebalance the line, never shrink the
font). Verify every number computationally BEFORE typesetting. Sig figs:
finals to the sig figs of the governing data (angles usually 3 sf);
intermediates carry one guard digit; VERIFY numerically that
rounded-intermediate and full-precision paths give the same rounded final.

## Mandatory verification — never skip
Rasterize every page (pdftoppm -png -r 80) and view; crop at 300 dpi and view
one tag/corner seam and one page number; confirm rules 1–8 and, in compact
density, that no label or work line collides with a box or margin. Exam
additions: zoom one bubble strip with a two-digit tag (bubbles must clear the
tag), confirm PES height ratios match electron counts, confirm no inline
option row crosses the right margin. Run the full pass on EVERY form produced
(real, practice, and key if requested). Only then deliver.
