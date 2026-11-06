"""format_check.py — [PATCH:formatter] WO-F1: the "Ledger read test".

Proves a freshly formatted PDF is machine-readable by the evaluation
pipeline, using the evaluation pipeline's OWN code:

  * rasterizes each page exactly the way the pipeline rasterizes a scan
    (fitz.Matrix(4.16, 4.16), alpha=False),
  * blank-page test: the pipeline's ImageStat stddev < 15 rule,
  * answer-box detection: `LedgerGUI.extract_boxes_with_opencv` from
    engine/ledger-main-minimus.py, called unbound on a tiny stub (the method
    only touches self.log_message) — NOT a second detector,
  * footer page-number OCR: the engine's `_read_boxed_digits` with the same
    crop (Settings-DB `crop_footer` via `LedgerGUI.get_crop_coordinates`)
    and the same contour preprocessing as `extract_with_silo_1`.

The engine module imports cleanly without starting the GUI (the GUI is only
constructed under `__main__`), so NO engine change was needed for this.

Passes when: boxes found == expected, every footer page number reads as its
1-based position, and no page is blank.

[PATCH:formatter-spacing] WO-F2-S adds a spacing check: on the rendered
pages, the visible white space below each answer box (bottom border to the
next ink beneath it) must be at least MIN_BELOW_BOX_PT, so no label or
question ever reads as belonging to the box ABOVE it. A failure goes back
to the model like any other read-test finding, with the box tag named.
"""
import importlib.util
import os
import re
import sys

# [PATCH:formatter-spacing] minimum VISIBLE white below any answer box (pt).
# Targets are 16 (before a sub-part label/heading) and 22 (before the next
# question); 13 is the hard floor with rendering tolerance.
MIN_BELOW_BOX_PT = 13.0
_RASTER_SCALE = 4.16          # the pipeline's own Matrix(4.16, 4.16)
_FOOTER_ZONE_PT = 60.0        # ink below y=60 is the footer, not "next item"
_TAG_RE = re.compile(r"^\d{1,2}[a-z]?$")

ENGINE_PATH = "/opt/ledger/engine/ledger-main-minimus.py"
_engine = None


def _load_engine():
    """Import the engine file once (its name has hyphens, hence importlib)."""
    global _engine
    if _engine is None:
        spec = importlib.util.spec_from_file_location("ledger_engine",
                                                      ENGINE_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ledger_engine"] = mod
        spec.loader.exec_module(mod)
        _engine = mod
    return _engine


# [PATCH:formatter-spacing] ------------------------------------------------
def _page_boxes(page):
    """Outermost stroked rects (the answer boxes) of a fitz page, with the
    tag text found at each top-left corner. Nested cells (FINAL ANSWER)
    are skipped. Geometry comes from the PDF vectors; the white space
    itself is measured on the rendered raster."""
    import fitz
    rects = []
    for d in page.get_drawings():
        if d.get("width") is None or d["width"] < 1.2:
            continue
        for item in d["items"]:
            if item[0] == "re":
                r = fitz.Rect(item[1])
                if r.width >= 40 and r.height >= 18:
                    rects.append(r)
    outer = [r for r in rects
             if not any(o != r and o.contains(r) for o in rects)]
    words = page.get_text("words")
    out = []
    for r in outer:
        tag = "(untagged)"
        for w in words:
            cx, cy = (w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0
            if (abs(cx - r.x0) < 30 and r.y0 - 15 < cy < r.y0 + 14
                    and _TAG_RE.match(w[4])):
                tag = w[4]
                break
        out.append((tag, r))
    return out


def _spacing_problems(page, png_path, page_num):
    """Visible white below each box, measured on the rendered page."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(cv2.imread(png_path), cv2.COLOR_BGR2GRAY)
    ph, pw = gray.shape
    floor_px = int((page.rect.height - _FOOTER_ZONE_PT) * _RASTER_SCALE)
    problems = []
    boxes = _page_boxes(page)
    for tag, r in boxes:
        # start just below the border stroke (path y1 + half stroke width)
        y_start = int((r.y1 + 1.2) * _RASTER_SCALE) + 2
        x0 = max(int((r.x0 + 2) * _RASTER_SCALE), 0)
        x1 = min(int((r.x1 - 2) * _RASTER_SCALE), pw)
        win = gray[y_start:min(floor_px, ph), x0:x1]
        if win.size == 0:
            continue
        ink = np.where((win < 128).sum(axis=1) >= 2)[0]
        if len(ink) == 0:
            continue              # nothing below the box on this page
        ink_y_pt = (ink[0] + y_start) / _RASTER_SCALE
        # a box stacked directly below another (e.g. a bubble-strip bank)
        # has no label to mis-own: when the first ink is another box's top
        # edge or its tag, there is no finding.
        if any(r2 is not r and r2.y0 - 13.5 <= ink_y_pt <= r2.y0 + 2
               and r2.x0 < r.x1 and r2.x1 > r.x0
               for _t2, r2 in boxes):
            continue
        white_pt = ink_y_pt - (r.y1 + 0.9)
        if white_pt < MIN_BELOW_BOX_PT:
            problems.append(
                f"Page {page_num}: box {tag} has only {white_pt:.0f} pt of "
                f"white space below it before the next item (minimum "
                f"{MIN_BELOW_BOX_PT:.0f} pt — below-box gaps are QGAP_C=22 "
                f"before a question, RGAP_C=16 before a label/heading).")
    return problems
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal stand-in for the LedgerGUI instance: the engine methods the
    check calls use only self.log_message."""
    def __init__(self):
        self.lines = []

    def log_message(self, message):
        self.lines.append(str(message))


def check_pdf(pdf_path, expected_boxes, expected_pages=None):
    """Run the Ledger read test. Returns a dict:
        passed       bool
        boxes_found  int (across all pages)
        page_reads   [int|None] footer page number read per page, in order
        blank_pages  [int] 1-based page numbers the pipeline would skip
        n_pages      int
        problems     [str] plain-language findings (empty when passed)
    """
    import fitz
    import cv2
    from PIL import Image, ImageStat

    eng = _load_engine()
    stub = _EngineStub()
    os.makedirs(eng.TEMP_DIR, exist_ok=True)

    import format_runner
    scratch = format_runner.new_scratch()

    problems = []
    page_reads = []
    blank_pages = []
    boxes_found = 0

    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    if expected_pages is not None and n_pages != expected_pages:
        problems.append(f"The PDF has {n_pages} page(s) but the build script "
                        f"reported LEDGER_PAGES={expected_pages}.")
    f_coords = eng.LedgerGUI.get_crop_coordinates(stub, 'crop_footer')
    hi = int(os.environ.get("LEDGER_FOOTER_MAX_PAGE", "99"))

    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16), alpha=False)
        png_path = os.path.join(scratch, f"check_page_{i + 1}.png")
        pix.save(png_path)

        # 1. blank test — the pipeline's rule verbatim
        if ImageStat.Stat(Image.open(png_path).convert("L")).stddev[0] < 15.0:
            blank_pages.append(i + 1)
            problems.append(f"Page {i + 1} reads as BLANK to the pipeline.")
            page_reads.append(None)
            continue

        # 2. answer boxes — the pipeline's own detector
        snippets = eng.LedgerGUI.extract_boxes_with_opencv(stub, png_path,
                                                           i + 1)
        boxes_found += len(snippets)

        # 3. footer page number — same crop + preprocessing as
        #    extract_with_silo_1, same OCR core (_read_boxed_digits)
        img = cv2.imread(png_path)
        x, y, w, h = f_coords
        footer = img[y:y + h, x:x + w]
        read_val = None
        if footer.size:
            gray_f = cv2.cvtColor(footer, cv2.COLOR_BGR2GRAY)
            _, thresh_f = cv2.threshold(gray_f, 0, 255,
                                        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            contours, _ = cv2.findContours(thresh_f, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                c = max(contours, key=cv2.contourArea)
                if cv2.contourArea(c) > 500:
                    read_val, _raw = eng._read_boxed_digits(gray_f, c,
                                                            thresh_f,
                                                            lo=1, hi=hi)
        page_reads.append(read_val)
        if read_val != i + 1:
            problems.append(f"Page {i + 1}: footer page number read as "
                            f"{read_val!r}, expected {i + 1}.")

        # 4. [PATCH:formatter-spacing] visible white space below each box
        problems.extend(_spacing_problems(page, png_path, i + 1))
    doc.close()

    if boxes_found != expected_boxes:
        problems.append(f"The Ledger found {boxes_found} answer box(es) but "
                        f"the build script reported "
                        f"LEDGER_BOXES={expected_boxes}.")

    return {"passed": not problems, "boxes_found": boxes_found,
            "page_reads": page_reads, "blank_pages": blank_pages,
            "n_pages": n_pages, "problems": problems}
