from tkinter import ttk
import os
import time
import logging
import threading
import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk
import fitz  # PyMuPDF
from PIL import Image, ImageStat
import cv2  # NEW: OpenCV for cropping
import numpy as np # NEW: Matrix math for OpenCV
import pytesseract # NEW: Local OCR engine
import json
import json_repair  # SURGICAL INSERTION: The forgiving parser
import csv
import sqlite3
import subprocess
import shutil
import re
import base64
import io
import datetime
import random  # [PATCH:silo2-bounded-retry] full-jitter backoff
import requests
import decimal
import math
import numbers


# --- [PATCH:score-format] ---------------------------------------------------
# Summing model-returned points in binary floating point yields values like
# 3.5999999999999996, which then reached teachers verbatim in the conspectus,
# Tabula and gradebook. Every score shown to a human goes through this ONE
# helper: at most two decimals, trailing zeros trimmed (3.6, 3.55, 3 -- never
# a forced ".00"). Ties round half-up on the value's shortest repr, so 2.675
# shows as 2.68 the way a teacher would round it. FORMAT TIME ONLY: stored
# values, DB writes and grading math never see the rounded string.
# Non-numeric input ("-", None, "DEFERRED") passes through as str(value).
def format_score(value):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return str(value)
    if not isinstance(value, numbers.Integral) and not math.isfinite(value):
        return str(value)  # nan / inf: show as-is rather than crash a report
    try:
        d = decimal.Decimal(str(float(value))) if not isinstance(value, numbers.Integral) \
            else decimal.Decimal(int(value))
        d = d.quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    except (decimal.InvalidOperation, ValueError, OverflowError):
        return str(value)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s == "-0" else s
# --- end [PATCH:score-format] -----------------------------------------------


# --- [PATCH:overflow-detector] ----------------------------------------------
# xelatex exits 0 even when an unbreakable box is taller than the page body
# and runs off the paper (the feedback tail is silently lost). This scans a
# compile .log for "Overfull \vbox (NNNpt too high)" lines above a small
# threshold so the batch log can warn the teacher. WARN-ONLY by contract:
# the detector never blocks, alters or fails a PDF, and every call site must
# swallow its exceptions.
def detect_vbox_overflows(log_text, threshold_pt=5.0):
    """Return the list of Overfull \\vbox heights (pt) above threshold_pt."""
    hits = re.findall(r"Overfull \\vbox \(([0-9.]+)pt too high\)", log_text)
    out = []
    for h in hits:
        try:
            v = float(h)
        except ValueError:
            continue
        if v > threshold_pt:
            out.append(v)
    return out
# --- end [PATCH:overflow-detector] --------------------------------------------


# --- [PATCH:boxed-digit-ocr] -----------------------------------------------
# Unified OCR for Ledger's white-digit-in-dark-box marks (footer page number).
# Never crops-and-inverts a rectangle: the box's own dark rim would invert
# into a black stroke flush against the digits, which tesseract reads as
# '|' / '[' / ']' / '0'. Instead, fill the box contour into a mask and take
# the digits as the LIGHT pixels INSIDE it. The rim is dark, so it can never
# enter the crop.
#
# [PATCH:boxed-digit-ocr-v3] (Rev jjj) Grayscale-first + multi-PSM vote.
# v2 failed on thin-stroke serif footers (LibreOffice / Liberation Serif,
# Gilded Age Test: 12/16, incl. SILENT '16'->'1'): binarizing at native
# resolution + a 2x2 MORPH_OPEN deleted ~1 px strokes, and the fixed 9 px
# mask erosion clipped digits near the rim of small boxes. v3:
#   1. mask erosion is box-proportional (never > box/8, floor 2 px)
#   2. digits stay GRAYSCALE through the upscale; Otsu binarization happens
#      only at target height, followed by CLOSE (heals hairline breaks;
#      never OPEN, which amputates thin strokes)
#   3. speck removal by connected-component area, not morphology
#   4. tesseract votes across several PSMs; winner = highest per-word
#      confidence, agreement count breaks ties (fixes the chronic '11'->'1'
#      weakness of lone PSM 8, all fonts)
# Validation: 16/16 real Gilded Age crops; 384/384 synthetic (Liberation
# Serif + Liberation Sans Bold, box heights 55/90/140 px, clean + scan-
# degraded, values 1-40). v2 baseline on same corpus: 12/16 real, latent
# silent misreads 5->3, 14->4, 21->27 on small serif.
#
# Tunables (env, all optional):
#   LEDGER_DIGIT_ERODE_PX   inner-margin CAP, px    (default 9; actual erode
#                                                    = min(cap, box/8), >= 2)
#   LEDGER_DIGIT_TARGET_H   upscale target height   (default 96)
#   LEDGER_DIGIT_PAD_PX     white quiet zone, px    (default 25)
#   LEDGER_DIGIT_PSM_LIST   comma list of PSMs to vote (default "7,8,6")

_BOXED_DIGIT_WL = "-c tessedit_char_whitelist=0123456789"


def build_run_summary(stats):
    """[PATCH:run-summary] WO-COS2 T6: the end-of-run summary, display-free
    (count_check_* helper pattern) so tests can drive it. Takes the plain
    dict _collect_run_summary_stats builds (also what rides in a batch job
    record) and returns the lines to print, [] when there is nothing to
    print. Any line whose count is zero is omitted, except "Pages:"."""
    if not stats or not stats.get("ran"):
        return []
    lines = ["=== RUN SUMMARY ==="]
    students = stats.get("students", []) or []
    n_students = len(students)
    discards = stats.get("discards", []) or []
    student_word = "student" if n_students == 1 else "students"
    lines.append(
        f"  Pages: {stats.get('pages_scanned', 0)} scanned · "
        f"{stats.get('blank_pages', 0)} blank · "
        f"{stats.get('pages_sorted', 0)} sorted to {n_students} {student_word} · "
        f"{len(discards)} discarded")
    fa = stats.get("footers_attempted", 0)
    fr = stats.get("footers_read", 0)
    if fa:
        f_line = f"  Footers read: {fr} of {fa}."
        if fr < fa and stats.get("footer_saving", True):
            f_line += " Unreadable crops saved in .Diagnostic-Log/Footer-Rejects."
        lines.append(f_line)
    if stats.get("turned_upright", 0):
        lines.append(f"  Pages turned upright: {stats['turned_upright']}")
    if stats.get("sideways", 0):
        lines.append(f"  Sideways pages: {stats['sideways']}")
    for d in discards:
        if d.get("second") is None:
            what = f"ID read {d.get('first')}"
        else:
            what = f"ID read {d.get('first')}, re-read {d.get('second')}"
        lines.append(f"  Discarded: page {d.get('page')} of {d.get('file')} — "
                     f"{what} (not on the roster)")
    for r in stats.get("rereads", []) or []:
        lines.append(f"  ID re-read accepted: page {r.get('page')} of "
                     f"{r.get('file')} — first {r.get('first')}, then "
                     f"{r.get('second')}. Please verify.")
    for e in stats.get("read_errors", []) or []:
        lines.append(f"  {e.get('file')}: reading stopped at an error near "
                     f"page {e.get('page')}. Later pages in this file were "
                     f"NOT read.")
    # Page-count comparison: WORKSHEET mode only. A student with MORE pages
    # than the master is flagged too (a page may be filed under the wrong ID).
    master_pages = stats.get("master_pages")
    if stats.get("grading_mode", "boxed") != "essay" and master_pages:
        for s in students:
            n = s.get("pages", 0)
            if n != master_pages:
                page_word = "page" if n == 1 else "pages"
                lines.append(
                    f"  Page count: ID {s.get('id')} ({s.get('name')}) has "
                    f"{n} {page_word}; the master has {master_pages}. "
                    f"Please check.")
    return lines


def pass1_capacity_error(err_str):
    """[PATCH:pass1-pacing] WO-COS2 T5: the SAME capacity test Silo 2 uses
    (the is_capacity check in evaluate_grid_with_silo_2), on lowercased
    error text. Used by Silo 1 to flip Pass 1 into slow pacing."""
    return ("resource exhausted" in err_str or "429" in err_str
            or "503" in err_str or "service unavailable" in err_str)


def footer_summary_line(footers_read, footers_attempted, saving_enabled=True):
    """[PATCH:footer-reader] WO-COS2 T3: the end-of-Pass-1 footer line,
    display-free (count_check_* helper pattern) so tests can drive it.
    The Footer-Rejects pointer only appears when saving is enabled --
    with LEDGER_FOOTER_DEBUG=0 no crops were saved, so no pointer."""
    line = f"Footers read: {footers_read} of {footers_attempted} pages."
    if footers_read < footers_attempted and saving_enabled:
        line += " Unreadable crops saved in .Diagnostic-Log/Footer-Rejects."
    return line


def _read_boxed_digits(gray, contour, thresh, lo=None, hi=None):
    """Return (value:int|None, raw:str).

    gray    -- single-channel uint8 image containing the box
    contour -- the box's external contour
    thresh  -- the THRESH_BINARY_INV+OTSU threshold of `gray` (box == 255)
    lo, hi  -- inclusive validity range; a read outside it is rejected

    value is None when unreadable OR out of range. Callers must treat both
    the same way: do not trust the read.
    """
    target_h = int(os.environ.get("LEDGER_DIGIT_TARGET_H", "96"))
    pad_px = int(os.environ.get("LEDGER_DIGIT_PAD_PX", "25"))
    erode_cap = int(os.environ.get("LEDGER_DIGIT_ERODE_PX", "9"))
    psm_list = [p.strip() for p in
                os.environ.get("LEDGER_DIGIT_PSM_LIST", "7,8,6").split(",")
                if p.strip()]

    _, _, w, h = cv2.boundingRect(contour)
    erode_px = max(2, min(erode_cap, h // 8, w // 8))

    mask = np.zeros_like(thresh)
    # Fill the CONVEX HULL, not the raw contour: a digit glyph wider than
    # its box (Chemistry Final p.4) punctures the rim; the hull bridges the
    # breach. For an intact box, hull == contour.
    cv2.drawContours(mask, [cv2.convexHull(contour)], -1, 255, -1)
    mask = cv2.erode(mask, np.ones((erode_px, erode_px), np.uint8))
    if not mask.any():
        return None, ""

    # Digits stay grayscale: invert (light digits -> dark ink on light
    # field), blank everything outside the eroded mask to paper white.
    inv = 255 - gray
    inv[mask == 0] = 255
    ys, xs = np.where(mask > 0)
    inv = inv[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    inv = cv2.normalize(inv, None, 0, 255, cv2.NORM_MINMAX)

    # Upscale the GRAYSCALE, then binarize at comfortable size. Binarizing
    # a ~2 px serif stroke at native resolution shreds it (the v2 defect).
    scale = target_h / float(inv.shape[0])
    big = cv2.resize(inv, None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (3, 3), 0)
    _, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bw = 255 - cv2.morphologyEx(255 - bw, cv2.MORPH_CLOSE,
                                np.ones((3, 3), np.uint8))

    # Speck filter by component area (>= 2% of the largest component or
    # 9 px). Replaces the destructive 2x2 MORPH_OPEN.
    n, lab, stats, _ = cv2.connectedComponentsWithStats(255 - bw)
    if n <= 1:
        return None, ""
    areas = stats[1:, cv2.CC_STAT_AREA]
    min_area = max(9, int(0.02 * areas.max()))
    clean = np.full_like(bw, 255)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            clean[lab == i] = 0
    bw = clean
    ys, xs = np.where(bw == 0)
    if ys.size == 0:
        return None, ""
    bw = bw[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    bw = cv2.copyMakeBorder(bw, pad_px, pad_px, pad_px, pad_px,
                            cv2.BORDER_CONSTANT, value=255)

    # Multi-PSM vote. PSM 8 alone chronically drops a digit from '11'/'16';
    # line modes (7, 6) recover it. Winner = highest per-word confidence;
    # agreement count breaks ties. Out-of-range reads never receive a vote.
    votes = {}
    raw_seen = []
    for psm in psm_list:
        data = pytesseract.image_to_data(
            bw, config="--psm {0} {1}".format(psm, _BOXED_DIGIT_WL),
            output_type=pytesseract.Output.DICT)
        txt = "".join(t for t in data["text"] if t.strip())
        confs = [float(c) for c, t in zip(data["conf"], data["text"])
                 if t.strip()]
        raw_seen.append(txt if txt else "-")
        if not txt.isdigit():
            continue
        val = int(txt)
        if lo is not None and val < lo:
            continue
        if hi is not None and val > hi:
            continue
        votes.setdefault(val, []).append(min(confs) if confs else 0.0)

    if not votes:
        return None, "/".join(raw_seen)
    best = max(votes.items(), key=lambda kv: (max(kv[1]), len(kv[1])))
    return best[0], str(best[0])
# --- end [PATCH:boxed-digit-ocr] -------------------------------------------
import getpass
import grp

# Identify the active user's home directory for data siloing
HOME_DIR = os.path.expanduser("~")

# Dynamically bind the Engine directory to the script's actual location in the unified vault
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))

# The Teacher's Siloed Workspace
LEDGER_DIR = os.path.join(HOME_DIR, "Ledger Workspace")

# Siloed App Data (Hidden with a '.' to keep the teacher's visible workspace clean)
TEMP_DIR = os.path.join(LEDGER_DIR, ".Temp-Ledger-Files")
DIAGNOSTIC_DIR = os.path.join(LEDGER_DIR, ".Diagnostic-Log")
# [PATCH:footer-reader] WO-COS2 T3: failed footer crops land here (NOT in the
# temp folder, which is wiped at the end of every run). Emptied at the START
# of Pass 1 of each run, so the last run's crops survive until the next run.
FOOTER_REJECTS_DIR = os.path.join(DIAGNOSTIC_DIR, "Footer-Rejects")
# [PATCH:box-rejects] WO-BM1 T5: rejected box-recovery crops and the ID
# crops of pages discarded for an off-roster ID land here (same lifecycle
# as Footer-Rejects: emptied at the START of Pass 1, kept until the next
# run). LEDGER_BOX_DEBUG=0 disables the saving only; logging stays.
BOX_REJECTS_DIR = os.path.join(DIAGNOSTIC_DIR, "Box-Rejects")
ID_REJECTS_DIR = os.path.join(DIAGNOSTIC_DIR, "ID-Rejects")
LOG_FILE = os.path.join(DIAGNOSTIC_DIR, "ledger-diagnostics.log")
DB_PATH = os.path.join(LEDGER_DIR, ".ledger-database.db")

# Shared Global Assets (Read strictly from the unified vault)
WATERMARK_FILE = os.path.join(ENGINE_DIR, "Ledger-Watermark.png")

# [PATCH:cosmetics] WO-COS1 T6 / WO-COS2 T1: ONE title constant used by BOTH
# root.title() and the wmctrl raise in __main__ (wmctrl -F -a matches the full
# window title exactly, so sharing the constant guarantees the single-instance
# raise finds this window and only this window).
MAIN_WINDOW_TITLE = "THE LEDGER"

# [PATCH:batch] WO-B1 test seam, mirroring the daemon's LEDGER_PORT: tests
# point the GUI at a second daemon copy on a spare port. Production never
# sets it, so the live URL is byte-identical to before.
DAEMON_BASE = f"http://127.0.0.1:{os.environ.get('LEDGER_DAEMON_PORT') or '5000'}"

# [PATCH:batch] A pending batch job lives in the WORKSPACE, outside the temp
# folder, because temp is scrubbed at startup, shutdown and run end and a
# pending job must survive all three.
BATCH_JOB_DIR = os.path.join(LEDGER_DIR, ".Pending-Batch-Job")
BATCH_JOB_FILE = os.path.join(BATCH_JOB_DIR, "job.json")

# Visible Workspace Directories
MASTER_DIR = os.path.join(LEDGER_DIR, "01 Master Assignments")
UNEVALUATED_DIR = os.path.join(LEDGER_DIR, "02 Inbox Scans")
COURSES_DIR = os.path.join(LEDGER_DIR, "03 Classes")
SIS_EXPORTS_DIR = os.path.join(LEDGER_DIR, "04 SIS Exports")
ORIGINAL_SCANS_DIR = os.path.join(LEDGER_DIR, "05 Archive Originals")
# [PATCH:formatter] WO-F1: formatter work lives beside the other workspace
# folders; the core logic lives in /opt/ledger/formatter (no GUI imports).
FORMATTED_DIR = os.path.join(LEDGER_DIR, "06 Formatted Assignments")
FORMATTER_CODE_DIR = os.path.join(os.path.dirname(ENGINE_DIR), "formatter")

PERIODS = [
    "1st Period", "2nd Period", "3rd Period", "4th Period", 
    "5th Period", "6th Period", "7th Period", "Unknown-Student"
]

# [PATCH:no-default-prompt] WO3: no grading prompt ships with the Ledger (like
# the web app). The teacher types or pastes the grading instructions into the
# GRADING INSTRUCTIONS box (EDIT LEDGER PROMPT); they live in the Settings DB
# under 'grading_instructions'. RUN refuses to grade while they are empty.
NO_INSTRUCTIONS_BANNER = "NO GRADING INSTRUCTIONS"                            # red status line
NO_INSTRUCTIONS_LOG = "NO GRADING INSTRUCTIONS — enter them before running"  # log line

# --- [PATCH:wizard-devapi] (WO-A7) ------------------------------------------
# Display-free pieces of the SYSTEM INITIALIZATION wizard: the provider
# completeness rule, the on-screen copy, and the measured window geometry.
# Kept at module level so tests/ can import and drive them without a display.
# [PATCH:aistudio-prefix-aq] WO2 D3: AI Studio issues "AQ." keys as well as
# the older "AIza". Prefix sanity stays warn-only, so a future format can
# never be refused -- it only draws the warning.
AI_STUDIO_PREFIXES = ("AQ.", "AIza")
ANTHROPIC_PREFIX = "sk-ant-"
WIZARD_INSTITUTION_VALUE = "owner-operator"   # fixed; no school name is collected
# Measured on the live display (1360x768, work area 735) during WO-A7 -- see
# the WO-A7 report for the raw winfo_req* numbers. Largest state + margin.
# Wizard body content requests 700x632 (Advanced collapsed) / 719x838
# (expanded) + 16 px scrollbar + 8 px padding -> largest state 743x846.
WIZARD_GEOMETRY = (800, 850)
# Disclosure Toplevel requests 920x528 with its text box sized to its 21
# rendered lines (see open_disclosure): 920x560 = request + 32 px margin.
DISCLOSURE_GEOMETRY = (920, 560)
WIZARD_SCREEN_ALLOWANCE = 100       # same screen clamp the dashboard uses
WIZARD_LEVEL_COLORS = {"block": "#c0392b", "warn": "#e67e22", "ready": "#1e8449", "idle": "#7f8c8d"}
WIZARD_DEFAULT_RULE = (
    "Provide GOOGLE (AI Studio API key, or Project ID + service-account JSON under "
    "Advanced), ANTHROPIC (API key), or BOTH. At least one complete provider is required."
)
WIZARD_BILLING_REQUIRED = "Billing confirmation required for Google AI Studio keys."

# [PATCH:evaluation-wording] Evaluation-only terms (see DISCLAIMER.md).
WIZARD_LEGAL_TEXT = (
    "EXPERIMENTAL SOFTWARE -- EVALUATION TERMS AND ASSUMPTION OF RISK\n\n"

    "The Ledger is experimental software published for evaluation by school and "
    "district IT professionals. It is not a finished product, it is not certified "
    "or approved for use with student data, and it is not intended for use by "
    "individual teachers. Its author and Paragon Electrical Contracting, LLC "
    "(d/b/a Paragon, LLC and ParagonK12, LLC) do not recommend or encourage its "
    "use by anyone. By entering provider credentials and initializing this "
    "machine, I, the operator named above, agree to the following:\n\n"

    "    1. APPROVAL AND COMPLIANCE: I am solely responsible for obtaining every "
    "approval my school, district, or institution requires before this software "
    "is installed or used, and for compliance with every applicable law, "
    "regulation, contract, and policy, including FERPA, state student-privacy "
    "law, and any data-privacy agreement my institution requires.\n\n"

    "    2. DATA SENT TO THIRD PARTIES: This software sends images to the AI "
    "provider(s) I configure (Google and/or Anthropic): the blank master pages, "
    "a title crop, an ID crop from every page, and either cropped answer boxes "
    "(WORKSHEET mode) or WHOLE PAGES (ESSAY mode). What a provider does with "
    "those images is governed by its terms, which I have read and accept.\n\n"

    "    3. NOT HARDENED: This machine has no disk encryption, no hardware-key "
    "login, and no kiosk lockdown. Securing it is my responsibility.\n\n"

    "    4. AI OUTPUT CAN BE WRONG: Every score and comment must be reviewed by a "
    "licensed educator before it is used for any purpose.\n\n"

    "    5. COSTS: I am solely responsible for every charge incurred on the "
    "provider account(s) I enter.\n\n"

    "    6. NO WARRANTY, NO LIABILITY: This software is provided AS IS, without "
    "warranty of any kind, under the GNU Affero General Public License v3.0 and "
    "the additional terms in DISCLAIMER.md. To the fullest extent permitted by "
    "law, the author and Paragon Electrical Contracting, LLC (d/b/a Paragon, LLC "
    "and ParagonK12, LLC) shall not be liable for any claim, loss, or damage of "
    "any kind arising from this software or its use, including disclosure of "
    "data, inaccurate scores, regulatory penalties, employment consequences, or "
    "cloud-service charges. I use it entirely at my own risk."
)

# §4e body text. [PATCH:evaluation-wording] Deliberately does NOT describe or
# interpret Google's terms; it points the operator to them.
BILLING_DISCLOSURE_TEXT = (
    "This software sends images of scanned pages to Google's Gemini service: a "
    "crop of the ID box on every page, and either cropped answer boxes (WORKSHEET "
    "mode) or WHOLE PAGES (ESSAY mode). Anything written on a page, including a "
    "name, can be sent. Treat everything you test with as a student record.\n\n"

    "Google offers the Gemini API under separate terms for unpaid (free-tier) and "
    "paid use, and those terms treat submitted material differently. This software "
    "does not describe or interpret Google's terms, and they can change at any time. "
    "You and your institution must read them and decide whether they are acceptable "
    "before any real student work is processed.\n\n"

    "Which of Google's terms apply is set by your Google account and project, not "
    "by this software. Confirm the status of your own project with Google.\n\n"

    "BEFORE CONTINUING: open Google AI Studio, go to Settings > Plan, and enable "
    "billing on the project that owns this key. All charges appear on that account "
    "and are your responsibility. If you have not done this, do not proceed -- the "
    "setup is reversible, and the key can be entered after billing is on.\n\n"

    "By checking the box below you confirm that billing is enabled on the key you "
    "entered, that you have read Google's terms, and that you, not the author or "
    "Paragon, are responsible for deciding whether they are acceptable.\n\n"

    "Reference: https://ai.google.dev/gemini-api/terms  (sections \"Unpaid Services\" "
    "and \"Paid Services\")"
)


def wizard_provider_state(ai_key, gcp_id, has_creds, anth_key, billing_confirmed):
    """Pure provider-completeness rule for the SYSTEM INITIALIZATION wizard.

    Google is complete as EITHER an AI Studio key OR (Project ID + service-account
    JSON); Anthropic as an sk-ant- key; at least one complete provider is required.
    Both Google forms at once -> block (one Google route per box). A half-entered
    Vertex pair -> block. A malformed Anthropic key -> block. An AI Studio key
    without the billing attestation -> block. Prefix sanity (AQ./AIza / sk-ant- in the
    wrong field) -> warn only.

    Returns a dict: ok (bool), level ('block'|'warn'|'ready'|'idle'), message
    (status-line text), warnings (list), use_ai_studio, use_vertex, use_anthropic.
    """
    ai_key = (ai_key or "").strip()
    gcp_id = (gcp_id or "").strip()
    anth_key = (anth_key or "").strip()
    has_creds = bool(has_creds)

    ai_present = bool(ai_key)
    vertex_complete = bool(gcp_id) and has_creds
    vertex_partial = bool(gcp_id) != has_creds
    both_google = ai_present and (bool(gcp_id) or has_creds)
    anth_complete = anth_key.startswith(ANTHROPIC_PREFIX)
    anth_malformed = bool(anth_key) and not anth_complete

    warnings = []
    if ai_present and ai_key.startswith(ANTHROPIC_PREFIX):
        warnings.append("The GOOGLE AI STUDIO field holds an Anthropic key (sk-ant-...). "
                        "Move it to the ANTHROPIC field.")
    elif ai_present and not ai_key.startswith(AI_STUDIO_PREFIXES):
        warnings.append("AI Studio keys normally begin with AQ. or AIza -- check the paste.")

    def _out(ok, level, message, use_ai=False, use_vertex=False, use_anth=False):
        return {"ok": ok, "level": level, "message": message, "warnings": warnings,
                "use_ai_studio": use_ai, "use_vertex": use_vertex, "use_anthropic": use_anth}

    if both_google:
        return _out(False, "block",
                    "Choose ONE Google route: an AI Studio key OR Project ID + "
                    "service-account JSON (Advanced), not both.")
    if vertex_partial:
        return _out(False, "block",
                    "Google Cloud (Advanced) is half-entered: Project ID and "
                    "service-account JSON are BOTH required. Complete the pair, clear "
                    "it, or use an AI Studio key instead.")
    if anth_malformed:
        if anth_key.startswith(AI_STUDIO_PREFIXES):
            return _out(False, "block",
                        "The ANTHROPIC field holds a Google AI Studio key (AQ... or AIza...). "
                        "Move it to the GOOGLE AI STUDIO field.")
        return _out(False, "block",
                    "ANTHROPIC key doesn't look right: it must begin with sk-ant- "
                    "(copy the whole key from the Anthropic Console).")
    if ai_present and not billing_confirmed:
        return _out(False, "block", WIZARD_BILLING_REQUIRED)

    google_complete = ai_present or vertex_complete
    if not (google_complete or anth_complete):
        return _out(False, "idle", WIZARD_DEFAULT_RULE)

    names = []
    if ai_present:
        names.append("GOOGLE (AI Studio)")
    if vertex_complete:
        names.append("GOOGLE (Vertex)")
    if anth_complete:
        names.append("ANTHROPIC (direct API)")
    ready = ("Providers ready: " if len(names) > 1 else "Provider ready: ") + " + ".join(names) + "."
    if warnings:
        return _out(True, "warn", ready + " WARNING: " + warnings[0],
                    ai_present, vertex_complete, anth_complete)
    return _out(True, "ready", ready, ai_present, vertex_complete, anth_complete)
# --- end [PATCH:wizard-devapi] ----------------------------------------------


# --- [PATCH:count-check] -----------------------------------------------------
# WO-B1: a model reply that silently drops an answer box must never commit a
# score. These helpers are module-level and display-free (the
# wizard_provider_state pattern) so tests/ can import and drive them.
def count_check_norm_item(value):
    """Normalize one item number for comparison: trim spaces, lowercase,
    remove a single trailing ')'. '3A )' and '3a' compare equal."""
    s = str(value).strip().lower()
    if s.endswith(")"):
        s = s[:-1].strip()
    return s


def count_check_item_list(items):
    """Normalized item numbers of a parsed reply, in reply order."""
    return [count_check_norm_item(it.get("item_number", ""))
            for it in items if isinstance(it, dict)]


def count_check_pages_label(footer_pages):
    """Plain-words page label for dashboard lines: 'Pages 3-4', 'Page 3',
    or 'Pages unknown' when the footer could not be read."""
    if not footer_pages:
        return "Pages unknown"
    pp = sorted(footer_pages)
    if len(pp) == 1:
        return f"Page {pp[0]}"
    if pp == list(range(pp[0], pp[-1] + 1)):
        return f"Pages {pp[0]}-{pp[-1]}"
    return "Pages " + ",".join(str(p) for p in pp)
# --- end [PATCH:count-check] -------------------------------------------------


# --- [PATCH:id-compose] WO-APP1 T3 -------------------------------------------
# The formatter ID field takes the NAME only ("Smithian"); the printed label
# composes as "<name> ID". Saved settings and reopened sessions may still
# hold a full label ("Smithian ID") -- strip ONE trailing " ID" word on read
# so nothing ever composes to "Smithian ID ID". Module-level and display-free
# (the wizard_provider_state pattern) so tests/ can import and drive them.
# Matches the browser edition's behavior.
def id_label_strip(text):
    """Bare name from an ID label: remove ONE trailing, space-separated
    'ID' word, any case, any spacing. 'Smithian ID' -> 'Smithian';
    'Smithian' and 'DAVID' (no separate ID word) come back unchanged."""
    s = str(text or "").strip()
    return re.sub(r"\s+id\s*$", "", s, flags=re.IGNORECASE)


def id_label_compose(text):
    """Printed header label from whatever the teacher typed: '<name> ID'.
    A full label composes to itself (never 'Smithian ID ID'); an empty or
    blank entry composes to '' (the caller refuses to send)."""
    bare = id_label_strip(text)
    return bare + " ID" if bare else ""
# --- end [PATCH:id-compose] --------------------------------------------------


# --- [PATCH:fmt-ticker] WO-APP1 T6 -------------------------------------------
def formatter_elapsed_label(seconds):
    """Elapsed-time text for the formatter status line: '37s', '2m 40s',
    '1h 02m'. Module-level and display-free so tests/ can drive it."""
    s = max(0, int(seconds))
    if s < 60:
        return "%ds" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%dm %02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh %02dm" % (h, m)
# --- end [PATCH:fmt-ticker] --------------------------------------------------


# --- [PATCH:batch] -----------------------------------------------------------
# The Silo 2 per-call prompt line and response schema, moved VERBATIM out of
# evaluate_grid_with_silo_2 so the batch submit path carries the identical
# model-facing text. Do not reword either (WO-B1 ground rule).
SILO2_PROMPT = "Evaluate this grid of answers based strictly on your cached instructions."
EVALUATION_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "item_number": {"type": "STRING"},
            "student_answer": {"type": "STRING"},
            "status": {"type": "STRING"},
            "feedback": {"type": "STRING"},
            "points_earned": {"type": "NUMBER"}
        },
        "required": ["item_number", "student_answer", "status", "feedback", "points_earned"]
    }
}
# --- end [PATCH:batch] -------------------------------------------------------

class LedgerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title(MAIN_WINDOW_TITLE)
        # [PATCH:topbar-fit] 1100 px cannot seat the essay-mode control row
        # (RUN + MAX SCORE + MODE selector + SHUT DOWN) -- the SHUT DOWN
        # button was clipped clean off the right edge. Open at 1615x920 (the
        # verified-fit size), clamped to the physical screen so a smaller
        # district monitor still gets the largest window that fits.
        _sw = self.root.winfo_screenwidth()
        _sh = self.root.winfo_screenheight()
        self.root.geometry(f"{min(1615, _sw - 40)}x{min(920, _sh - 100)}")
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.is_running = False
        self.calibrated_this_session = False   # [PATCH:calib-session-skip] set by SAVE in either studio
        self.master_pdf_path = None
        self.cached_silo2_content = None
        self.master_group_key = None
        self.master_display_title = None
        
        self.roster = self.load_roster()
        
        self.header_font = ctk.CTkFont(family="Ubuntu", size=24, weight="bold")
        self.main_font = ctk.CTkFont(family="Ubuntu", size=18)
        self.log_font = ctk.CTkFont(family="Courier", size=16)
        self.selector_font = ctk.CTkFont(family="Ubuntu", size=14)   # [PATCH:cosmetics] MODE/SCORING segmented buttons
        self.log_font_bold = ctk.CTkFont(family="Courier", size=16, weight="bold")   # [PATCH:cosmetics] "===" log lines
        
        self.setup_ui()
        self.verify_directories()
        self.initialize_database()
        self.clean_temp_directory() 
        self.setup_logging()
        self.check_first_run()
        # [PATCH:batch] a pending batch job greets the teacher with the
        # RESUME / CANCEL BATCH choice once the window is up.
        self.root.after(800, self.check_pending_batch_job)

    def load_roster(self):
        """Loads the roster into memory directly from the SQLite Database."""
        roster = {}
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            # Fills the memory dictionary using the DB so the pipeline functions normally
            cursor.execute("SELECT Bartonian_ID, Last_Name, First_Name FROM Roster")
            for row in cursor.fetchall():
                roster[str(row[0])] = {"last": row[1], "first": row[2]}
            conn.close()
        except Exception as e:
            self.log_message(f"[!] DB Roster Load Error: {e}")
        return roster

    def setup_ui(self):
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self.root, width=280, corner_radius=0, fg_color="black")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        
        logo_path = os.path.join(ENGINE_DIR, "Boot Menu Logo1.png")
        try:
            logo_img = ctk.CTkImage(light_image=Image.open(logo_path), dark_image=Image.open(logo_path), size=(220, 220))
            self.logo_label = ctk.CTkLabel(self.sidebar, image=logo_img, text="")
            self.logo_label.grid(row=0, column=0, padx=30, pady=(40, 20))
        except Exception:
            self.logo_label = ctk.CTkLabel(self.sidebar, text="[Logo Missing]", font=self.header_font)
            self.logo_label.grid(row=0, column=0, padx=30, pady=(40, 20))

        self.status_label = ctk.CTkLabel(self.sidebar, text="SYSTEM: IDLE", font=self.header_font, text_color="#1e8449", wraplength=240)
        self.status_label.grid(row=1, column=0, padx=20, pady=10)

        # NEW UI: Blank Master PDF Uploader [cite: 92, 95]
        self.master_btn = ctk.CTkButton(self.sidebar, text="LOAD MASTER PDF", font=self.main_font, fg_color="#333333", hover_color="#555555", text_color="white", command=self.load_master_pdf)
        self.master_btn.grid(row=2, column=0, padx=20, pady=10)
        
        self.master_status_label = ctk.CTkLabel(self.sidebar, text="MASTER: NOT LOADED", font=self.main_font, text_color="white")
        self.master_status_label.grid(row=3, column=0, padx=20, pady=5)

        # NEW: Gradebook Launcher Button
        self.gradebook_btn = ctk.CTkButton(self.sidebar, text="OPEN GRADEBOOK", font=self.main_font, fg_color="#333333", hover_color="#555555", command=self.open_gradebook)
        self.gradebook_btn.grid(row=4, column=0, padx=20, pady=(20, 10))
        
        # SURGERY: ROSTER MANAGER BUTTON
        self.roster_btn = ctk.CTkButton(
            self.sidebar, 
            text="MANAGE ROSTER", 
            font=self.main_font, 
            fg_color="#333333", 
            hover_color="#555555", 
            command=self.open_roster_manager
        )
        self.roster_btn.grid(row=5, column=0, padx=20, pady=(10, 10))

        # SURGERY: ASSIGNMENT MANAGER BUTTON
        self.assignment_btn = ctk.CTkButton(
            self.sidebar, 
            text="MANAGE ASSIGNMENTS", 
            font=self.main_font, 
            fg_color="#333333", 
            hover_color="#555555", 
            command=self.open_assignment_manager
        )
        self.assignment_btn.grid(row=6, column=0, padx=20, pady=(10, 10))

        # CALIBRATE SCANNER BUTTON
        self.calibrate_btn = ctk.CTkButton(
            self.sidebar, 
            text="CALIBRATE SCANNER", 
            font=self.main_font, 
            fg_color="#333333", 
            hover_color="#555555", 
            command=self.open_calibration_tool
        )
        self.calibrate_btn.grid(row=7, column=0, padx=20, pady=(10, 10))

        # ALL-USERS AI MODEL PICKER (per-user; persists in each user's own DB)
        self.model_btn = ctk.CTkButton(
            self.sidebar,
            text="AI MODEL",
            font=self.main_font,
            fg_color="#333333",
            hover_color="#555555",
            command=self.open_model_picker
        )
        self.model_btn.grid(row=9, column=0, padx=20, pady=(10, 10))

        # --- OS-LEVEL GROUP AUTHENTICATION FOR SYSTEM CONTROLS ---
        try:
            current_user = getpass.getuser()
            admin_groups = ['faculty-admin', 'district-admin', 'sudo']
            user_groups = [g.gr_name for g in grp.getgrall() if current_user in g.gr_mem]
            
            # EDIT LEDGER PROMPT - available to ALL tiers; each account edits its OWN prompt copy.
            self.edit_prompt_btn = ctk.CTkButton(
                self.sidebar,
                text="GRADING INSTRUCTIONS",
                font=self.main_font,
                fg_color="#333333",
                hover_color="#555555",
                command=self.open_prompt_editor
            )
            self.edit_prompt_btn.grid(row=8, column=0, padx=20, pady=(10, 10))

            # ADVANCED / MANAGE OS USERS stay admin-gated (factory reset + account mgmt).
            if current_user == 'paragon' or any(group in user_groups for group in admin_groups):
                
                # ADVANCED SETTINGS BUTTON
                self.advanced_btn = ctk.CTkButton(
                    self.sidebar, 
                    text="ADVANCED", 
                    font=self.main_font, 
                    fg_color="#333333", 
                    hover_color="#555555", 
                    command=self.open_advanced_settings
                )
                self.advanced_btn.grid(row=10, column=0, padx=20, pady=(10, 10))
        except Exception as e:
            self.log_message(f"[!] OS Group Authentication Check Failed: {e}")

        # --- ROLE-BASED FIDO2 & USER MANAGEMENT ---
        try:
            current_user = getpass.getuser()
            admin_groups = ['faculty-admin', 'district-admin', 'sudo']
            user_groups = [g.gr_name for g in grp.getgrall() if current_user in g.gr_mem]
            is_admin = current_user == 'paragon' or any(group in user_groups for group in admin_groups)
            # Keys & Accounts buttons exist only on the hardened appliance build (keymgr GUI + usermgr helper).
            appliance_tools = (os.path.exists("/opt/ledger/engine/ledger-keymgr-gui.py")
                               and os.path.exists("/usr/local/sbin/ledger-usermgr"))

            if appliance_tools and is_admin:
                self.manage_users_btn = ctk.CTkButton(
                    self.sidebar, text="MANAGE OS USERS", font=self.main_font, 
                    fg_color="#333333", hover_color="#555555", command=self.open_keys_and_accounts
                )
                self.manage_users_btn.grid(row=11, column=0, padx=20, pady=(10, 10))
            elif appliance_tools:
                self.enroll_key_btn = ctk.CTkButton(
                    self.sidebar, text="REGISTER MY KEY", font=self.main_font, 
                    fg_color="#333333", hover_color="#555555", command=self.open_keys_and_accounts
                )
                self.enroll_key_btn.grid(row=11, column=0, padx=20, pady=(10, 10))
        except Exception as e:
            self.log_message(f"[!] Role-Based Sidebar Error: {e}")

        # [PATCH:formatter] FORMAT ASSIGNMENT — visible to every account,
        # styled like its neighbors. Sits under the existing rows (0-11);
        # the spacer moves from row 12 to row 13 so nothing above shifts.
        self.formatter_btn = ctk.CTkButton(
            self.sidebar,
            text="FORMAT ASSIGNMENT",
            font=self.main_font,
            fg_color="#333333",
            hover_color="#555555",
            command=self.open_formatter_window
        )
        self.formatter_btn.grid(row=12, column=0, padx=20, pady=(10, 10))

        # Spacer  ([PATCH:formatter] moved 12 -> 13 for the new button)
        self.sidebar.grid_rowconfigure(13, weight=1)

        self.main_area = ctk.CTkFrame(self.root, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        # [PATCH:batch] log area moved from grid row 2 to 3: the SCORING
        # control row sits between the top row and the log.
        self.main_area.grid_rowconfigure(3, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        self.control_frame = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.control_frame.grid(row=0, column=0, sticky="ew", pady=(0, 20))

        self.run_btn = ctk.CTkButton(self.control_frame, text="RUN", font=self.header_font, height=55, width=160, fg_color="#1e8449", hover_color="#27ae60", command=self.start_ledger)
        self.run_btn.pack(side="left", padx=(0, 30))

        # [PATCH:topbar-fit] SHUT DOWN is created and PACKED EARLY, anchored
        # to the RIGHT edge. Tk pack priority is call order: earlier-packed
        # widgets keep their space when the frame is squeezed, so on a window
        # too narrow for the whole row it is the MODE selector (packed last)
        # that clips -- never the safety-critical SHUT DOWN button.
        self.shutdown_btn = ctk.CTkButton(self.control_frame, text="STOP", font=self.header_font, height=55, width=120, fg_color="#c0392b", hover_color="#e74c3c", command=self.shut_down)
        self.shutdown_btn.pack(side="right", padx=(10, 0))

        self.score_label = ctk.CTkLabel(self.control_frame, text="MAX SCORE:", font=self.header_font)
        self.score_label.pack(side="left", padx=(0, 10))

        self.max_score_entry = ctk.CTkEntry(self.control_frame, font=self.header_font, width=90, height=45)
        self.max_score_entry.pack(side="left", padx=(0, 30))

        # [PATCH:essay-mode] Teacher-facing pipeline selector. The choice is
        # persisted in the Settings DB the moment it is tapped, and is read
        # ONCE per run (in start_ledger) so it can never change mid-batch.
        self.mode_label = ctk.CTkLabel(self.control_frame, text="MODE:", font=self.header_font)
        self.mode_label.pack(side="left", padx=(0, 10))

        self.mode_selector = ctk.CTkSegmentedButton(
            self.control_frame,
            values=["WORKSHEET", "ESSAY"],
            font=self.selector_font,
            height=38,
            command=self.on_mode_selected
        )
        self.mode_selector.set("ESSAY" if self.get_grading_mode() == "essay" else "WORKSHEET")
        self.mode_selector.pack(side="left", padx=(0, 10))
        # [PATCH:topbar-fit] shutdown_btn moved above (packed early, side="right").

        # [PATCH:batch] SCORING: LIVE | BATCH rides on its OWN row under the
        # top row: on a 1360x768 display the top row cannot seat another
        # control without clipping (SHUT DOWN must never clip), so the WO-B1
        # fallback applies. The choice persists in Settings and is read once
        # per run, exactly like MODE. The label beside it carries batch
        # progress while a submitted job is being collected.
        self.control_frame2 = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.control_frame2.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        self.scoring_label = ctk.CTkLabel(self.control_frame2, text="SCORING:", font=self.header_font)
        self.scoring_label.pack(side="left", padx=(0, 10))

        self.scoring_selector = ctk.CTkSegmentedButton(
            self.control_frame2,
            values=["LIVE", "BATCH"],
            font=self.selector_font,
            height=38,
            command=self.on_scoring_selected
        )
        self.scoring_selector.set("BATCH" if self.get_scoring_mode() == "batch" else "LIVE")
        self.scoring_selector.pack(side="left", padx=(0, 10))

        self.batch_progress_label = ctk.CTkLabel(self.control_frame2, text="",
                                                 font=self.main_font, anchor="w")
        self.batch_progress_label.pack(side="left", padx=(16, 0))

        self.log_label = ctk.CTkLabel(self.main_area, text="DIAGNOSTIC LOG:", font=self.main_font)
        self.log_label.grid(row=2, column=0, sticky="nw", pady=(0, 5))

        self.log_text = ctk.CTkTextbox(self.main_area, font=self.log_font, text_color="#2ecc71", fg_color="#121212", wrap="word", corner_radius=10)
        self.log_text.grid(row=3, column=0, sticky="nsew")

        # [PATCH:cosmetics] WO-COS1 T7: color-coded log lines. Tags live on
        # the underlying tk widget; "[!]" errors render red, "===" section
        # banners white bold Courier, everything else keeps the green default.
        _log_tk = getattr(self.log_text, "_textbox", self.log_text)
        _log_tk.tag_config("ledger_err", foreground="#e74c3c")
        _log_tk.tag_config("ledger_hdr", foreground="#ffffff", font=self.log_font_bold)

        # SURGICAL INSERTION: Universal Right-Click Menu for the Diagnostic Log
        def custom_log_paste():
            try:
                # Forcefully grab the main OS clipboard contents
                clipboard_text = self.root.clipboard_get()
                
                # Purge any highlighted text before pasting
                if self.log_text.tag_ranges("sel"):
                    self.log_text.delete("sel.first", "sel.last")
                    
                # Insert clipboard at the current cursor position
                self.log_text.insert("insert", clipboard_text)
            except tk.TclError:
                pass # Fails gracefully if the clipboard is completely empty

        log_menu = tk.Menu(self.log_text, tearoff=False, bg="#2b2b2b", fg="white")
        log_menu.add_command(label="Copy", command=lambda: self.log_text.event_generate("<<Copy>>"))
        log_menu.add_command(label="Paste", command=custom_log_paste)
        log_menu.add_command(label="Select All", command=lambda: self.log_text.tag_add("sel", "1.0", "end"))
        
        self.log_text.bind("<Button-3>", lambda event: log_menu.tk_popup(event.x_root, event.y_root))

        # Robust, consistent mouse-only Copy/Paste for the streaming terminal.
        self._add_textbox_menu(self.log_text)

    def load_master_pdf(self):
        try:
            # Force the native Linux Mint file picker
            result = subprocess.run(
                ['zenity', '--file-selection', f'--filename={MASTER_DIR}/', '--title=SELECT BLANK MASTER PDF', '--file-filter=PDF files | *.pdf'],
                capture_output=True, text=True
            )
            file_path = result.stdout.strip()
        except FileNotFoundError:
            # Safety fallback to Tkinter if zenity fails
            file_path = filedialog.askopenfilename(initialdir=MASTER_DIR, title="SELECT BLANK MASTER PDF", filetypes=[("PDF Files", "*.pdf")])
            
        if file_path:
            self.master_pdf_path = file_path
            filename = os.path.basename(file_path)
            self.master_status_label.configure(text=f"MASTER: {filename}", text_color="#1e8449")
            self.log_message(f"Master Copy loaded: {filename}. Ready for batch verification.")

    def run_batch_extraction(self, target_pdf):
        """Extracts the title using dynamic coordinates, registers assignment, and ignites pipeline."""
        self.log_message("Extracting official assignment title using calibrated zones...")
        try:
            doc = fitz.open(target_pdf)
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16), alpha=False)
            
            temp_master_img_path = os.path.join(TEMP_DIR, "temp_title_extract_source.png")
            pix.save(temp_master_img_path)
            doc.close()

            img = cv2.imread(temp_master_img_path)
            
            # Execute array slice using dynamic database coordinates
            t_coords = self.get_crop_coordinates('crop_title')
            title_crop = img[t_coords[1] : t_coords[1]+t_coords[3], t_coords[0] : t_coords[0]+t_coords[2]]
            
            temp_crop_path = os.path.join(TEMP_DIR, "temp_master_title_crop.png")
            cv2.imwrite(temp_crop_path, title_crop)

            base64_img = self.image_to_base64_png(temp_crop_path)

            prompt = (
                "Extract the assignment/lab title from this cropped header image. "
                "Return ONLY a valid, flat JSON object using EXACTLY this template: "
                '{"assignment_title": "Exact Title Here"}'
            )

            payload = {
                "model_name": self.get_silo2_model(),
                "prompt": prompt,
                "image_b64": base64_img
            }
            
            time.sleep(10) # Network buffer before establishing Silo 1 session
            response = requests.post(DAEMON_BASE + "/api/silo1", json=payload, timeout=60)
            
            if response.status_code != 200:
                raise Exception(f"Daemon HTTP {response.status_code}: {response.text}")
                
            data = response.json()
            
            if data.get("status") == "success":
                raw = data["text"].strip().replace('```json', '').replace('```', '').strip()
                title_data = json_repair.loads(raw)
            else:
                raise Exception(data.get("message", "Daemon returned an error."))

            raw_title = title_data.get("assignment_title", "Unknown Assignment")
            self.master_group_key, self.master_display_title = self.process_assignment_names(raw_title)
            self.master_original_title = raw_title
            
            self.log_message(f"-> Official Title Locked: {self.master_display_title}")
            
            # Register the assignment and officially launch the main batch thread
            self.register_assignment_in_db(self.master_group_key, self.master_display_title, self.global_max_score)
            threading.Thread(target=self.process_pipeline, daemon=True).start()

        except Exception as e:
            self.log_message(f"[!] Critical Error extracting Title/Igniting Engine: {e}")
            self.shut_down()
        
    def initialize_database(self):
        """Ensures the core database tables exist for a fresh Paragon unit."""
        try:
            conn = sqlite3.connect(DB_PATH)
            
            # Enable foreign key support in SQLite (required for cascading deletes)
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.cursor()
            
            # 1. Core Appliance Tables
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Roster (
                    Bartonian_ID INTEGER PRIMARY KEY,
                    First_Name TEXT,
                    Last_Name TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Assignments (
                    Assignment_Key TEXT PRIMARY KEY,
                    Display_Title TEXT,
                    Max_Score REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Grades (
                    Bartonian_ID INTEGER,
                    Assignment_Key TEXT,
                    Score_Earned REAL,
                    PRIMARY KEY (Bartonian_ID, Assignment_Key),
                    FOREIGN KEY (Bartonian_ID) REFERENCES Roster(Bartonian_ID) ON DELETE CASCADE,
                    FOREIGN KEY (Assignment_Key) REFERENCES Assignments(Assignment_Key) ON DELETE CASCADE
                )
            ''')
            
            # 2. Hidden Settings Table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Settings (
                    Setting_Key TEXT PRIMARY KEY,
                    Setting_Value TEXT
                )
            ''')
            
            # 3. Inject default demonym
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('id_demonym', 'Bartonian')
            ''')
            
            # 4. [PATCH:no-temperature] No Silo 2 temperature default: models
            #    run at vendor defaults. A legacy 'silo2_temperature' row in an
            #    existing DB is simply never read.

            # 5. Inject default AI model
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('silo2_model', 'gemini-3.5-pro-preview')
            ''')
            
            # 6. Inject default Scanner Calibration Coordinates (X, Y, W, H)
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('crop_title', '259,214,741,148')
            ''')
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('crop_id', '1647,164,691,181')
            ''')
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('crop_footer', '1200,3093,132,159')
            ''')
            # [PATCH:calib-seed] Title/ID seeds = the operator's own Letter calibration
            # of the Ledger form. The old ID seed (2380,236,1050,250) began at 93% of a
            # Letter page and ran off its right edge; the old title seed reached 70% of
            # the width and would overlap a correctly placed ID box. A DB whose three
            # rows still equal the old seeds was never calibrated, so it gets the new
            # seeds; a DB with any saved calibration is left alone.
            cursor.execute("SELECT Setting_Key, Setting_Value FROM Settings WHERE Setting_Key IN ('crop_title', 'crop_id', 'crop_footer')")
            if dict(cursor.fetchall()) == {'crop_title': '192,207,1598,119', 'crop_id': '2380,236,1050,250', 'crop_footer': '1200,3093,132,159'}:
                cursor.execute("UPDATE Settings SET Setting_Value = '259,214,741,148' WHERE Setting_Key = 'crop_title'")
                cursor.execute("UPDATE Settings SET Setting_Value = '1647,164,691,181' WHERE Setting_Key = 'crop_id'")
            
            # 7. [PATCH:essay-mode] Inject default grading mode (boxed = the
            # original stitched-grid pipeline; essay = FULL_PAGE pipeline)
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('grading_mode', 'boxed')
            ''')

            # 8. [PATCH:batch] Inject default scoring mode (live = today's
            # immediate calls; batch = half-price submit-and-collect)
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('scoring_mode', 'live')
            ''')
            
            conn.commit()
            conn.close()
        except Exception as e:
            self.log_message(f"[!] Database Initialization Error: {e}")

    def _is_initializer(self):
        """True ONLY for the District Administrator tier or the owner.
        These are the only roles allowed to see SYSTEM INITIALIZATION."""
        try:
            current_user = getpass.getuser()
            init_groups = ['district-admin', 'sudo']
            user_groups = [g.gr_name for g in grp.getgrall() if current_user in g.gr_mem]
            return current_user == 'paragon' or any(gn in user_groups for gn in init_groups)
        except Exception:
            return False

    def check_first_run(self):
        """Show SYSTEM INITIALIZATION only to a District Administrator (or owner),
        and only while the appliance is not yet initialized. The authoritative,
        system-wide 'initialized' signal is the world-readable marker
        /etc/ledger-initialized (NOT the per-user database, which only records
        who signed in on THIS account)."""
        system_initialized = os.path.exists('/etc/ledger-initialized')
        if system_initialized:
            return  # Already set up -> every tier just runs.
        if self._is_initializer():
            self.lock_system_for_setup()
        else:
            self.show_awaiting_setup_notice()

    def show_awaiting_setup_notice(self):
        """Non-administrator on an un-initialized appliance: NO wizard, just a
        plain notice and a disabled RUN button."""
        try:
            self.run_btn.configure(text="LOCKED", fg_color="#c0392b",
                                   hover_color="#c0392b", state="disabled")
            self.status_label.configure(text="AWAITING SETUP", text_color="#c0392b")
            notice = ctk.CTkToplevel(self.root)
            notice.title("SYSTEM NOT YET INITIALIZED")
            self._clamp_toplevel(notice, 560, 270)
            notice.transient(self.root)
            ctk.CTkLabel(notice, text="SYSTEM NOT YET INITIALIZED",
                         font=self.header_font).pack(pady=(30, 15))
            ctk.CTkLabel(
                notice,
                text=("This Ledger has not been set up yet.\n\n"
                      "First-time setup runs from the owner (administrator) account.\n"
                      "Evaluation is unavailable until then."),
                font=self.main_font, justify="center"
            ).pack(pady=10, padx=20)
            ctk.CTkButton(notice, text="OK", font=self.main_font, width=120,
                          command=notice.destroy).pack(pady=20)
            self._make_text_copyable(notice)
            notice.after(200, notice.grab_set)
        except Exception as e:
            self.log_message(f"[!] Awaiting-setup notice error: {e}")

    def lock_system_for_setup(self):
        self.run_btn.configure(text="AUTHORIZE", fg_color="#c0392b", hover_color="#e74c3c", command=self.launch_setup_gui)
        self.status_label.configure(text="SYSTEM: LOCKED", text_color="#c0392b")
        self.root.after(500, self.launch_setup_gui)

    def launch_setup_gui(self):
        """First-time setup for the owner-operator: provider credentials,
        identity, and the liability terms. [PATCH:wizard-anthropic] added the
        Anthropic route. [PATCH:wizard-devapi] (WO-A7) makes a Google AI Studio
        key the PRIMARY Google option, demotes the Vertex inputs (Project ID +
        service-account JSON + root CA) to a collapsed Advanced section, drops
        the institution field (a single operator initializes this box), and
        adds the blocking billing-disclosure screen for AI Studio keys. The
        completeness rule lives in wizard_provider_state() (module level,
        display-free) so tests/ can drive it."""
        import tkinter.font as tkfont
        setup_win = ctk.CTkToplevel(self.root)
        setup_win.title("SYSTEM INITIALIZATION")
        # [PATCH:wizard-devapi] Geometry measured on the live display during
        # WO-A7 (see WIZARD_GEOMETRY). Clamped to the physical screen the same
        # way the dashboard is; the body is a scrollable frame, so a clamped
        # window scrolls instead of clipping the AUTHORIZE button.
        _sw, _sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        _w = min(WIZARD_GEOMETRY[0], _sw - 40)
        _h = min(WIZARD_GEOMETRY[1], _sh - WIZARD_SCREEN_ALLOWANCE)
        setup_win.geometry(f"{_w}x{_h}")
        setup_win.minsize(_w, _h)
        setup_win.transient(self.root)

        body = ctk.CTkScrollableFrame(setup_win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=4, pady=4)
        small_font = ctk.CTkFont(family="Ubuntu", size=11)
        note_font = ctk.CTkFont(family="Ubuntu", size=12)

        header = ctk.CTkLabel(body, text="EVALUATION TERMS AND ASSUMPTION OF RISK", font=self.header_font)
        header.pack(pady=(16, 12))

        # Measurement / test seam (WO-A7 geometry check). Never read at runtime.
        hooks = {}
        self._wizard_hooks = hooks

        def _paste_into(entry):
            try:
                entry.insert("insert", setup_win.clipboard_get())
            except tk.TclError:
                pass

        def _masked_row(parent, label):
            """One masked credential entry with a SHOW toggle and a right-click
            Paste menu (the key IS the credential; SHOW lets the operator verify
            the paste). Returns (entry, StringVar)."""
            frame = ctk.CTkFrame(parent, fg_color="transparent")
            frame.pack(pady=5)
            ctk.CTkLabel(frame, text=label, font=self.main_font).pack(side="left", padx=10)
            var = ctk.StringVar(value="")
            entry = ctk.CTkEntry(frame, font=self.main_font, width=350, show="•", textvariable=var)
            entry.pack(side="left")
            show_var = ctk.StringVar(value="off")
            ctk.CTkCheckBox(frame, text="SHOW", variable=show_var, onvalue="on", offvalue="off",
                            width=60, font=small_font,
                            command=lambda: entry.configure(
                                show="" if show_var.get() == "on" else "•")).pack(side="left", padx=8)
            menu = tk.Menu(entry, tearoff=False, bg="#2b2b2b", fg="white")
            menu.add_command(label="Paste", command=lambda: _paste_into(entry))
            entry.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
            return entry, var

        # --- GOOGLE AI STUDIO KEY (primary Google option) ---
        ai_entry, ai_var = _masked_row(body, "GOOGLE AI STUDIO API KEY:")
        # --- ANTHROPIC KEY ([PATCH:wizard-anthropic]) ---
        anth_entry, anth_var = _masked_row(body, "ANTHROPIC API KEY:")

        # One-line provider rule so the operator knows what unlocks the button.
        provider_rule = ctk.CTkLabel(body, text=WIZARD_DEFAULT_RULE, font=note_font,
                                     text_color="#7f8c8d", wraplength=700, justify="center")
        provider_rule.pack(pady=(0, 4))

        disclosure_btn = ctk.CTkButton(
            body, text="BILLING DISCLOSURE (REQUIRED FOR AI STUDIO KEYS)", font=self.main_font,
            fg_color="#1f538d", hover_color="#14375e", state="disabled",
            command=lambda: open_disclosure())
        disclosure_btn.pack(pady=(4, 8))

        # --- ADVANCED: institutional Google Cloud (Vertex) inputs, collapsed ---
        # Functionality unchanged from the district build; only demoted.
        adv_state = {"open": False}
        _ADV_CLOSED = "▸  ADVANCED — INSTITUTIONAL GOOGLE CLOUD ACCOUNTS"
        _ADV_OPEN = "▾  ADVANCED — INSTITUTIONAL GOOGLE CLOUD ACCOUNTS"
        adv_btn = ctk.CTkButton(body, text=_ADV_CLOSED, font=self.main_font, width=600,
                                fg_color="#333333", hover_color="#555555",
                                command=lambda: toggle_advanced())
        adv_btn.pack(pady=(4, 4))
        adv_frame = ctk.CTkFrame(body, fg_color="#1e1e1e")
        ctk.CTkLabel(
            adv_frame,
            text=("Only for an institution-managed Google Cloud project (Vertex AI). Use either "
                  "this or the AI Studio key above -- never both."),
            font=note_font, text_color="#7f8c8d", wraplength=660, justify="center"
        ).pack(pady=(10, 4), padx=10)

        gcp_frame = ctk.CTkFrame(adv_frame, fg_color="transparent")
        gcp_frame.pack(pady=5)
        ctk.CTkLabel(gcp_frame, text="GOOGLE CLOUD PROJECT ID:", font=self.main_font).pack(side="left", padx=10)
        gcp_var = ctk.StringVar(value="")
        key_entry = ctk.CTkEntry(gcp_frame, font=self.main_font, width=350, textvariable=gcp_var)
        key_entry.pack(side="left")
        entry_menu = tk.Menu(key_entry, tearoff=False, bg="#2b2b2b", fg="white")
        entry_menu.add_command(label="Copy", command=lambda: key_entry.event_generate("<<Copy>>"))
        entry_menu.add_command(label="Paste", command=lambda: _paste_into(key_entry))
        key_entry.bind("<Button-3>", lambda e: entry_menu.tk_popup(e.x_root, e.y_root))

        # A Project ID alone does NOT authenticate to Vertex. A service-account
        # JSON key is the browser-free credential; it is staged in the per-user
        # temp dir and moved into /etc/paragon by save_and_boot in the SAME
        # privileged step (one password prompt).
        creds_state = {'path': None}

        def import_google_creds():
            try:
                # Release the modal grab BEFORE the GTK picker opens. A held Tk
                # grab plus an external dialog deadlocks X input (the whole
                # screen freezes). The finally re-grabs so the lock always
                # comes back, even if the picker errors out.
                setup_win.grab_release()
                try:
                    result = subprocess.run(
                        ['zenity', '--file-selection',
                         '--title=SELECT GOOGLE SERVICE-ACCOUNT KEY (.json)',
                         '--file-filter=Google credentials | *.json'],
                        capture_output=True, text=True
                    )
                finally:
                    setup_win.grab_set()
                picked = result.stdout.strip()
                if picked:
                    creds_state['path'] = picked
                    creds_status.configure(text=f"Loaded: {os.path.basename(picked)}",
                                           text_color="#1e8449")
                    self.log_message(f"Google credentials selected: {os.path.basename(picked)}")
                    validate_inputs()
            except Exception as e:
                self.log_message(f"[!] Credential selection error: {e}")

        creds_btn = ctk.CTkButton(adv_frame, text="IMPORT SERVICE-ACCOUNT JSON (.json)",
                                  font=self.main_font, fg_color="#1f538d",
                                  hover_color="#14375e", command=import_google_creds)
        creds_btn.pack(pady=(5, 5))
        creds_status = ctk.CTkLabel(adv_frame, text="No service-account JSON loaded",
                                    font=self.main_font, text_color="#c0392b")
        creds_status.pack(pady=(0, 6))

        # --- ROOT CA INJECTION (only matters behind a TLS-intercepting proxy) ---
        def inject_ssl_cert():
            try:
                setup_win.grab_release()
                try:
                    result = subprocess.run(
                        ['zenity', '--file-selection', '--title=SELECT ROOT CA CERTIFICATE (.crt/.pem)', '--file-filter=Certificates | *.crt *.pem'],
                        capture_output=True, text=True
                    )
                finally:
                    setup_win.grab_set()
                cert_path = result.stdout.strip()
                if cert_path:
                    self.log_message(f"Installing root CA certificate: {os.path.basename(cert_path)}")

                    # Release UI lock so the OS pkexec password prompt doesn't freeze under the window
                    setup_win.grab_release()

                    cert_name = os.path.basename(cert_path)
                    # Debian/Ubuntu systems require a .crt extension to process the certificate
                    if not cert_name.endswith('.crt'):
                        cert_name += '.crt'

                    cmd = f"cp '{cert_path}' '/usr/local/share/ca-certificates/{cert_name}' && update-ca-certificates"
                    subprocess.run(['pkexec', 'sh', '-c', cmd], check=True)

                    self.log_message("-> SSL Certificate installed successfully.")
                    setup_win.grab_set() # Re-engage UI lock
            except subprocess.CalledProcessError:
                self.log_message("[!] ERROR: Password prompt failed or was cancelled. Certificate not installed.")
                setup_win.grab_set()
            except Exception as e:
                self.log_message(f"[!] SSL Injection Error: {e}")
                setup_win.grab_set()

        ssl_btn = ctk.CTkButton(adv_frame, text="INSTALL ROOT CA CERT (OPTIONAL)", font=self.main_font,
                                fg_color="#1f538d", hover_color="#14375e", command=inject_ssl_cert)
        ssl_btn.pack(pady=(4, 12))

        def toggle_advanced():
            adv_state["open"] = not adv_state["open"]
            if adv_state["open"]:
                adv_frame.pack(after=adv_btn, pady=(0, 8), padx=20, fill="x")
                adv_btn.configure(text=_ADV_OPEN)
            else:
                adv_frame.pack_forget()
                adv_btn.configure(text=_ADV_CLOSED)
        hooks["toggle_advanced"] = toggle_advanced

        # --- IDENTITY ---
        info_frame = ctk.CTkFrame(body, fg_color="transparent")
        info_frame.pack(pady=10)
        ctk.CTkLabel(info_frame, text="OPERATOR NAME:", font=self.main_font).grid(row=0, column=0, sticky="e", padx=10, pady=5)
        rep_var = ctk.StringVar(value="")
        rep_entry = ctk.CTkEntry(info_frame, font=self.main_font, width=300, textvariable=rep_var)
        rep_entry.grid(row=0, column=1, sticky="w", pady=5)

        # --- LEGAL TEXT BOX ---
        legal_box = ctk.CTkTextbox(body, width=700, height=200, wrap="word", font=ctk.CTkFont(family="Ubuntu", size=13))
        legal_box.pack(pady=8)
        legal_box.insert("0.0", WIZARD_LEGAL_TEXT)
        legal_box.configure(state="disabled") # Lock the text from being edited

        # --- THE LIABILITY MECHANISM ---
        check_var = ctk.StringVar(value="off")
        liability_cb = ctk.CTkCheckBox(
            body,
            text="I HAVE READ AND AGREE TO THESE TERMS. I UNDERSTAND THIS IS EXPERIMENTAL SOFTWARE FOR EVALUATION, PROVIDED AS IS WITH NO WARRANTY, AND I USE IT ENTIRELY AT MY OWN RISK.",
            variable=check_var, onvalue="on", offvalue="off",
            font=ctk.CTkFont(family="Ubuntu", size=11, weight="bold"),
            text_color="#c0392b"
        )
        # [PATCH:cosmetics] WO-COS1 T12: dynamic wrap instead of hand-placed
        # \n breaks (probed: wrapped request 656x28 vs 745x28 manual).
        try:
            liability_cb._text_label.configure(wraplength=700, justify="left")
        except Exception:
            pass
        liability_cb.pack(pady=(10, 14))

        submit_btn = ctk.CTkButton(
            body, text="AUTHORIZE APPLIANCE", font=self.header_font, height=50,
            fg_color="#333333", state="disabled"
        )
        submit_btn.pack(pady=(4, 14))

        # --- BILLING DISCLOSURE SCREEN ([PATCH:wizard-devapi] §4e) ---
        # Shown once an AI Studio key is entered (paste, or leaving the field).
        # The attestation is tied to the key text: editing the key clears it.
        billing_var = ctk.StringVar(value="off")
        disc = {"win": None, "shown_for": None, "confirmed_key": None}

        def _close_disclosure():
            w = disc["win"]
            disc["win"] = None
            if w is not None:
                try:
                    w.grab_release()
                    w.destroy()
                except tk.TclError:
                    pass
            try:
                setup_win.grab_set()
            except tk.TclError:
                pass
            validate_inputs()

        def _on_billing_toggle():
            disc["confirmed_key"] = ai_var.get().strip() if billing_var.get() == "on" else None
            validate_inputs()

        def open_disclosure():
            if disc["win"] is not None:
                try:
                    disc["win"].lift()
                    disc["win"].focus_force()
                    return
                except tk.TclError:
                    disc["win"] = None
            d = ctk.CTkToplevel(setup_win)
            disc["win"] = d
            d.title("Billing and Student Work — Required Reading")
            dw = min(DISCLOSURE_GEOMETRY[0], _sw - 40)
            dh = min(DISCLOSURE_GEOMETRY[1], _sh - WIZARD_SCREEN_ALLOWANCE)
            d.geometry(f"{dw}x{dh}")
            d.minsize(dw, dh)
            d.transient(setup_win)
            d.protocol("WM_DELETE_WINDOW", _close_disclosure)
            ctk.CTkLabel(d, text="BILLING AND STUDENT WORK — REQUIRED READING",
                         font=self.header_font).pack(pady=(18, 8))
            box = ctk.CTkTextbox(d, width=dw - 60, wrap="word",
                                 font=ctk.CTkFont(family="Ubuntu", size=13))
            box.pack(pady=(0, 6), padx=30)
            box.insert("0.0", BILLING_DISCLOSURE_TEXT)
            box.configure(state="disabled")
            # Size the box to its rendered lines so nothing scrolls off-panel.
            d.update_idletasks()
            d.update()
            try:
                # count() returns display-line BOUNDARIES, i.e. lines - 1.
                n_lines = box._textbox.count("1.0", "end-1c", "displaylines")[0] + 1
                line_h = tkfont.Font(font=box._textbox.cget("font")).metrics("linespace")
                box.configure(height=n_lines * line_h + 32)
            except Exception:
                box.configure(height=460)
            cb = ctk.CTkCheckBox(
                d, text="I have enabled billing on this Google AI Studio key.",
                variable=billing_var, onvalue="on", offvalue="off",
                font=ctk.CTkFont(family="Ubuntu", size=13, weight="bold"),
                text_color="#c0392b", command=_on_billing_toggle)
            cb.pack(pady=(8, 8))
            ctk.CTkButton(d, text="CONTINUE", font=self.header_font, height=44, width=220,
                          command=_close_disclosure).pack(pady=(0, 16))
            hooks["disclosure_win"] = d
            hooks["disclosure_textbox"] = box
            self._make_text_copyable(d)
            setup_win.grab_release()
            d.after(200, d.grab_set)
        hooks["open_disclosure"] = open_disclosure
        hooks.update(ai_var=ai_var, anth_var=anth_var, gcp_var=gcp_var, rep_var=rep_var,
                     creds_state=creds_state, billing_var=billing_var, check_var=check_var,
                     submit_btn=submit_btn, provider_rule=provider_rule, disc=disc,
                     on_billing_toggle=_on_billing_toggle, close_disclosure=_close_disclosure)

        ai_prev = {"len": 0}

        def _on_ai_change(*_):
            val = ai_var.get().strip()
            pasted = (len(val) - ai_prev["len"]) > 1
            ai_prev["len"] = len(val)
            if not val:
                disc["shown_for"] = None
            elif pasted and disc["shown_for"] != val:
                disc["shown_for"] = val
                setup_win.after(50, open_disclosure)
            validate_inputs()

        def _on_ai_focus_out(_e=None):
            val = ai_var.get().strip()
            if val and disc["shown_for"] != val and disc["win"] is None:
                disc["shown_for"] = val
                open_disclosure()

        # --- DYNAMIC VALIDATION LOGIC ---
        def validate_inputs(*args):
            """Identity + liability are always required. The provider rule is
            wizard_provider_state(): at least ONE complete provider, one Google
            route only, prefix sanity warnings, and a confirmed billing
            attestation whenever an AI Studio key is present."""
            ai_key = ai_var.get().strip()
            if billing_var.get() == "on" and ai_key != disc["confirmed_key"]:
                billing_var.set("off")
                disc["confirmed_key"] = None
            st = wizard_provider_state(ai_key, gcp_var.get(), creds_state['path'] is not None,
                                       anth_var.get(), billing_var.get() == "on")
            disclosure_btn.configure(state="normal" if ai_key else "disabled")
            provider_rule.configure(text=st["message"], text_color=WIZARD_LEVEL_COLORS[st["level"]])
            has_rep = len(rep_var.get().strip()) > 0
            is_checked = check_var.get() == "on"
            if has_rep and is_checked and st["ok"]:
                submit_btn.configure(state="normal", fg_color="#1e8449", hover_color="#27ae60")
            else:
                submit_btn.configure(state="disabled", fg_color="#333333")

        ai_var.trace_add("write", _on_ai_change)
        ai_entry.bind("<FocusOut>", _on_ai_focus_out)
        for _v in (anth_var, gcp_var, rep_var):
            _v.trace_add("write", validate_inputs)
        liability_cb.configure(command=validate_inputs)

        # --- SECURE WRITE & BOOT EXECUTION ---
        def save_and_boot():
            # Provider-aware install. validate_inputs already guaranteed a
            # complete provider set; this writes only the lines/files that
            # belong to the providers given.
            ai_key = ai_var.get().strip()
            new_key = gcp_var.get().strip()
            anth_key = anth_var.get().strip()
            rep_name = rep_var.get().strip()
            timestamp = datetime.datetime.now().isoformat()

            st = wizard_provider_state(ai_key, new_key, creds_state['path'] is not None,
                                       anth_key, billing_var.get() == "on")
            use_ai = st["use_ai_studio"]
            use_vertex = st["use_vertex"]
            use_anthropic = st["use_anthropic"]

            if st["ok"] and rep_name:
                # 1. Stage .env, the service-account key (Vertex route only),
                #    and the init marker in the per-user temp dir. Paths are
                #    handled in Python so spaces / odd characters in the picked
                #    file are safe. No credential value is ever logged.
                temp_env = os.path.join(TEMP_DIR, ".env.temp")
                temp_sa = os.path.join(TEMP_DIR, "sa.json.temp")
                temp_marker = os.path.join(TEMP_DIR, "initialized.temp")
                with open(temp_env, "w") as f:
                    if use_ai:
                        # Daemon v2.9 builds genai.Client(api_key=...) from this
                        # (Gemini Developer API). Same custody as the other keys:
                        # /etc/paragon/.env, root:paragon 0640, never the DB.
                        f.write(f'LEDGER_GOOGLE_API_KEY="{ai_key}"\n')
                    if use_vertex:
                        f.write(f'GCP_PROJECT_ID="{new_key}"\n')
                        f.write('GCP_LOCATION="global"\n')
                        f.write('GOOGLE_APPLICATION_CREDENTIALS="/etc/paragon/sa.json"\n')
                    if use_anthropic:
                        # Daemon v2.4 reads this and routes claude-* models to
                        # the DIRECT Anthropic API.
                        f.write(f'LEDGER_ANTHROPIC_API_KEY="{anth_key}"\n')
                if use_vertex:
                    try:
                        shutil.copy(creds_state['path'], temp_sa)
                    except Exception as e:
                        self.log_message(f"[!] ERROR: Could not read the credentials file: {e}")
                        return
                with open(temp_marker, "w") as f:
                    f.write(f"initialized_by={rep_name}\n")
                    f.write(f"initialized_at={timestamp}\n")

                # 2. ONE privileged step: create /etc/paragon, install .env (and
                #    sa.json when the Vertex route is used) with daemon-readable
                #    perms (root:<owner group> 0640), drop the world-readable
                #    'initialized' marker, restart the daemon. A non-Vertex
                #    install also REMOVES any stale sa.json so a re-initialized
                #    box can't silently keep a prior Google Cloud identity.
                self.log_message("Requesting your password to secure credentials...")

                # Release the GUI input lock so the OS password prompt is usable
                setup_win.grab_release()

                try:
                    # Credential install is fatal-on-error (set -e); the daemon
                    # (re)start is best-effort -- it must NOT undo a good save if
                    # the broker isn't placed yet (system map register #1).
                    # [PATCH:owner-from-sudo-user] the daemon-readable group is the running
                    # user's primary group, not the literal "paragon" (computed once).
                    owner_group = grp.getgrgid(os.getgid()).gr_name
                    sa_step = (
                        f"mv '{temp_sa}' /etc/paragon/sa.json; "
                        f"chown root:{owner_group} /etc/paragon/sa.json; chmod 640 /etc/paragon/sa.json; "   # [PATCH:owner-from-sudo-user]
                        if use_vertex else
                        "rm -f /etc/paragon/sa.json; "
                    )
                    combined_command = (
                        "set -e; "
                        f"install -d -o root -g {owner_group} -m 0750 /etc/paragon; "   # [PATCH:owner-from-sudo-user]
                        f"mv '{temp_env}' /etc/paragon/.env; "
                        f"chown root:{owner_group} /etc/paragon/.env; chmod 640 /etc/paragon/.env; "   # [PATCH:owner-from-sudo-user]
                        + sa_step +
                        f"mv '{temp_marker}' /etc/ledger-initialized; "
                        "chown root:root /etc/ledger-initialized; chmod 644 /etc/ledger-initialized; "
                        "set +e; systemctl restart ledger-daemon.service; exit 0"
                    )
                    subprocess.run(['pkexec', 'sh', '-c', combined_command], check=True)

                except subprocess.CalledProcessError:
                    self.log_message("[!] ERROR: Password prompt failed or was cancelled. Keys not saved.")
                    # Re-engage the lock if the password prompt fails or is canceled
                    setup_win.grab_set()
                    return

                # 3. Record the authorization in the SQLite database. The
                #    liability_institution key is kept for schema compatibility
                #    (factory reset scrubs it) and holds a fixed value on this
                #    build -- no school name is collected.
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.executemany('''
                        INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value)
                        VALUES (?, ?)
                    ''', [
                        ('liability_signer', rep_name),
                        ('liability_institution', WIZARD_INSTITUTION_VALUE),
                        ('liability_timestamp', timestamp)
                    ])
                    conn.commit()
                    conn.close()
                except Exception as e:
                    self.log_message(f"[!] Database Write Error (Authorization Record): {e}")

                # 4. Finalizing Log
                providers = " + ".join(
                    p for p, on in (("GOOGLE (AI Studio)", use_ai), ("GOOGLE (Vertex)", use_vertex),
                                    ("ANTHROPIC", use_anthropic)) if on
                )
                if use_ai:
                    self.log_message("Google (AI Studio): billing confirmed by operator")
                self.log_message(f"-> AUTHORIZATION EXECUTED. Terms accepted by {rep_name}. Providers: {providers}.")

                # --- Unlock and restore the main GUI ---
                self.run_btn.configure(text="RUN", fg_color="#1e8449", hover_color="#27ae60", command=self.start_ledger)
                self.status_label.configure(text="SYSTEM: IDLE", text_color="#1e8449")

                # Release the hostage window
                _close_disclosure_quiet = disc["win"]
                if _close_disclosure_quiet is not None:
                    try:
                        _close_disclosure_quiet.destroy()
                    except tk.TclError:
                        pass
                setup_win.grab_release()
                setup_win.destroy()

        submit_btn.configure(command=save_and_boot)

        # Lock the window focus ONLY after all widgets have safely mapped to the screen
        self._make_text_copyable(setup_win)
        setup_win.grab_set()

    def get_custom_id_label(self):
        """Fetches the current pseudonym system label from the database."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'custom_id_label'")
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else "Bartonian ID"
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Pseudonym): {e}")
            return "Bartonian ID"

    def get_grading_mode(self):
        """[PATCH:essay-mode] Fetches the pipeline mode from the Settings DB.
        Returns 'boxed' (the original stitched-grid pipeline; default and
        fail-safe) or 'essay' (the FULL_PAGE pipeline). Any unexpected value
        collapses to 'boxed' so a corrupt row can never change chemistry."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'grading_mode'")
            result = cursor.fetchone()
            conn.close()
            return "essay" if (result and result[0] == "essay") else "boxed"
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Grading Mode): {e}")
            return "boxed"

    def get_scoring_mode(self):
        """[PATCH:batch] Fetches the scoring mode from the Settings DB.
        Returns 'live' (default and fail-safe) or 'batch'. Any unexpected
        value collapses to 'live' so a corrupt row can never strand a run
        in batch mode."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'scoring_mode'")
            result = cursor.fetchone()
            conn.close()
            return "batch" if (result and result[0] == "batch") else "live"
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Scoring Mode): {e}")
            return "live"

    def on_scoring_selected(self, selected_value):
        """[PATCH:batch] Persists the scoring choice immediately, like the
        MODE selector; it is read ONCE per run in start_ledger."""
        mode = "batch" if "BATCH" in str(selected_value).upper() else "live"
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('scoring_mode', ?)
            ''', (mode,))
            conn.commit()
            conn.close()
            self.log_message(f"-> Scoring mode set to: {mode.upper()}.")
        except Exception as e:
            self.log_message(f"[!] Database Write Error (Scoring Mode): {e}")

    def get_grading_instructions(self):
        """[PATCH:no-default-prompt] WO3: the teacher's grading instructions from
        the Settings DB. Nothing ships built in: no row (fresh install) or a
        read error returns '' and RUN refuses to grade."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'grading_instructions'")
            result = cursor.fetchone()
            conn.close()
            return result[0] if (result and result[0]) else ""
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Grading Instructions): {e}")
            return ""

    def on_mode_selected(self, selected_value):
        """[PATCH:essay-mode] Persists the teacher's mode choice immediately."""
        mode = "essay" if "ESSAY" in str(selected_value).upper() else "boxed"
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('grading_mode', ?)
            ''', (mode,))
            conn.commit()
            conn.close()
            self.log_message(f"-> Grading mode set to: {'ESSAY' if mode == 'essay' else 'WORKSHEET'}.")
        except Exception as e:
            self.log_message(f"[!] Database Write Error (Grading Mode): {e}")

    def register_assignment_in_db(self, assignment_key, display_title, max_score):
        """Ensures the assignment exists in the DB before grades are written."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO Assignments (Assignment_Key, Display_Title, Max_Score)
                VALUES (?, ?, ?)
            ''', (assignment_key, display_title, float(max_score)))
            conn.commit()
            conn.close()
        except Exception as e:
            self.log_message(f"[!] DB Assignment Registration Error: {e}")

    def write_grade_to_db(self, b_id, assignment_key, score_earned):
        """Writes or updates a student's grade for a specific assignment."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            # INSERT OR REPLACE handles late work effortlessly
            cursor.execute('''
                INSERT OR REPLACE INTO Grades (Bartonian_ID, Assignment_Key, Score_Earned)
                VALUES (?, ?, ?)
            ''', (int(b_id), assignment_key, float(score_earned))) # SURGICAL INSERTION: Accept partial credit
            conn.commit()
            conn.close()
            self.log_message(f"  -> Score committed to SQLite Database.")
        except Exception as e:
            self.log_message(f"[!] Database Write Error for ID {b_id}: {e}")

    def open_system_settings(self):
        """Launches the System Settings interface for the Paragon appliance."""
        set_win = ctk.CTkToplevel(self.root)
        set_win.title("SYSTEM SETTINGS")
        set_win.geometry("500x300")
        set_win.transient(self.root) 
        
        current_demonym = self.get_demonym()

        header_label = ctk.CTkLabel(set_win, text="APPLIANCE SETTINGS", font=self.header_font)
        header_label.pack(pady=(30, 20))

        input_frame = ctk.CTkFrame(set_win, fg_color="transparent")
        input_frame.pack(pady=10)

        demo_label = ctk.CTkLabel(input_frame, text="Classroom Demonym (e.g., Bartonian):", font=self.main_font)
        demo_label.pack(side="left", padx=(0, 10))

        self.demo_entry = ctk.CTkEntry(input_frame, font=self.main_font, width=150)
        self.demo_entry.insert(0, current_demonym)
        self.demo_entry.pack(side="left")

        save_btn = ctk.CTkButton(
            set_win, 
            text="SAVE CONFIGURATION", 
            font=self.header_font,
            height=45,
            fg_color="#1e8449", # Green for safety/confirmation
            hover_color="#27ae60",
            command=lambda: self.save_settings(set_win)
        )
        save_btn.pack(pady=(30, 0))
        self._make_text_copyable(set_win)

    def save_settings(self, window):
        """Commits the updated settings to the SQLite database."""
        new_demonym = self.demo_entry.get().strip()
        if not new_demonym:
            new_demonym = "Bartonian" # Fallback if they leave it blank
            
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE Settings 
                SET Setting_Value = ? 
                WHERE Setting_Key = 'id_demonym'
            ''', (new_demonym,))
            conn.commit()
            conn.close()
            self.log_message(f"-> System Settings Updated: Demonym set to '{new_demonym}'.")
            window.destroy()
        except Exception as e:
            self.log_message(f"[!] Database Update Error (Settings): {e}")

    def get_silo2_model(self):
        """Fetches the current active model from the database."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'silo2_model'")
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else "gemini-3.5-pro-preview"
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Model): {e}")
            return "gemini-3.5-pro-preview"

    def get_crop_coordinates(self, key):
        """Fetches a specific bounding box from the database as a tuple of ints (x, y, w, h)."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = ?", (key,))
            result = cursor.fetchone()
            conn.close()
            if result:
                return tuple(map(int, result[0].split(',')))
        except Exception as e:
            self.log_message(f"[!] DB Error reading {key}: {e}")
        # Failsafe defaults mirroring the initial DB creation
        defaults = {'crop_title': (259, 214, 741, 148), 'crop_id': (1647, 164, 691, 181), 'crop_footer': (1200, 3093, 132, 159)}
        return defaults.get(key, (0, 0, 100, 100))

    def set_user_model(self, model_name):
        """Persist this user's chosen model to their own siloed DB (UPSERT)."""
        model_name = (model_name or "").strip() or "gemini-3.5-pro-preview"
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Settings (Setting_Key, Setting_Value) VALUES ('silo2_model', ?) "
                "ON CONFLICT(Setting_Key) DO UPDATE SET Setting_Value = excluded.Setting_Value",
                (model_name,)
            )
            conn.commit()
            conn.close()
            self.log_message(f"-> AI model set to '{model_name}' for this account.")
            return True
        except Exception as e:
            self.log_message(f"[!] Could not save model preference: {e}")
            return False

    def open_model_picker(self):
        """All-users window to choose the Vertex model by EXACT id. No
        dropdown -- type the precise Model Garden id, e.g.
        gemini-3.5-flash, gemini-3.1-pro-preview, claude-opus-4-8,
        meta/llama-3.1-405b-instruct-maas. Persists per-user."""
        win = ctk.CTkToplevel(self.root)
        win.title("AI MODEL")
        self._clamp_toplevel(win, 640, 460)
        win.minsize(620, 440)
        win.resizable(True, True)
        win.transient(self.root)

        ctk.CTkLabel(win, text="CHOOSE YOUR AI MODEL", font=self.header_font).pack(pady=(24, 6))
        ctk.CTkLabel(
            win,
            text=("Type the EXACT model id.\n"
                  "Examples: gemini-4-argon, gemini-3.5-flash, claude-opus-4-8"),
            font=self.main_font, justify="center"
        ).pack(pady=(0, 14), padx=20)

        entry = ctk.CTkEntry(win, font=self.main_font, width=360)
        entry.insert(0, self.get_silo2_model())
        entry.pack(pady=(0, 16))

        # AI-MODEL-FIELD right-click copy/paste menu (mouse-driven; unique names avoid collisions).
        model_tkentry = getattr(entry, "_entry", entry)
        model_field_menu = tk.Menu(win, tearoff=False, bg="#2b2b2b", fg="white")
        model_field_menu.add_command(label="Cut", command=lambda: model_tkentry.event_generate("<<Cut>>"))
        model_field_menu.add_command(label="Copy", command=lambda: model_tkentry.event_generate("<<Copy>>"))
        model_field_menu.add_command(label="Paste", command=lambda: model_tkentry.event_generate("<<Paste>>"))
        model_field_menu.add_separator()

        def _model_field_select_all():
            try:
                model_tkentry.select_range(0, "end")
                model_tkentry.icursor("end")
            except tk.TclError:
                pass

        model_field_menu.add_command(label="Select All", command=_model_field_select_all)
        model_tkentry.bind("<Button-3>", lambda e: model_field_menu.tk_popup(e.x_root, e.y_root))

        def save_and_close():
            self.set_user_model(entry.get())
            win.destroy()

        ctk.CTkButton(win, text="SAVE MY MODEL", font=self.header_font, height=46,
                      fg_color="#1e8449", hover_color="#27ae60",
                      command=save_and_close).pack(pady=(18, 0))
        self._make_text_copyable(win)

    def open_advanced_settings(self):
        """Admin 'danger zone': Factory Reset only. The AI model is now set in ONE
        place -- the all-users AI MODEL button (open_model_picker) -- so this
        window no longer changes the model."""
        adv_win = ctk.CTkToplevel(self.root)
        adv_win.title("ADVANCED SETTINGS")
        self._clamp_toplevel(adv_win, 600, 430)
        adv_win.minsize(560, 300)
        adv_win.transient(self.root)

        # [PATCH:keymgr] WO-APP1 T5: key management sits with the other
        # admin-gated controls, gated like SYSTEM INITIALIZATION.
        if self._is_initializer():
            ctk.CTkButton(
                adv_win, text="MANAGE API KEYS", font=self.main_font,
                fg_color="#1f538d", hover_color="#14375e",
                command=self.open_manage_api_keys
            ).pack(pady=(24, 0))

        # The Scary Warning Frame
        warn_frame = ctk.CTkFrame(adv_win, fg_color="#c0392b", corner_radius=10)  # Blaze Red
        warn_frame.pack(fill="x", padx=30, pady=(30, 20))

        warn_label = ctk.CTkLabel(
            warn_frame,
            text=("WARNING\n\nResetting authorization removes this appliance's AI\n"
                  "credentials and re-locks it until an administrator\n"
                  "re-initializes it. Grades, rosters and settings are kept."),
            font=ctk.CTkFont(family="Ubuntu", size=16, weight="bold"),
            text_color="white"
        )
        warn_label.pack(pady=20)

        # --- ADMIN-ONLY AUTHORIZATION RESET (function names unchanged) ---
        try:
            current_user = getpass.getuser()
            sudo_group = grp.getgrnam('sudo').gr_mem

            # Checks if the user is the master 'paragon' account or has sudo privileges
            if current_user == 'paragon' or current_user in sudo_group:
                reset_btn = ctk.CTkButton(
                    adv_win,
                    text="RESET AUTHORIZATION",
                    font=self.main_font,
                    fg_color="#8B0000",  # Dark Red
                    hover_color="#A52A2A",
                    command=lambda: self.execute_factory_reset(adv_win)
                )
                reset_btn.pack(pady=(20, 0))
        except Exception as e:
            self.log_message(f"[!] Admin verification check failed: {e}")

        self._make_text_copyable(adv_win)

    def execute_factory_reset(self, window):
        """Wipes the secure .env and database liability signatures to lock the system."""
        # [PATCH:batch] WO-B1-2 Task 7: a factory reset wipes the API keys,
        # so a batch job still pending at the provider could afterwards be
        # neither collected nor cancelled -- student work would sit at the
        # provider with no record of it on this box. Say so in plain words
        # and let the operator cancel the batch first, or back out of the reset.
        if os.path.exists(BATCH_JOB_FILE):
            self._factory_reset_batch_guard(window)
            return
        self._do_factory_reset(window)

    def _factory_reset_batch_guard(self, window):
        """[PATCH:batch] WO-B1-2 Task 7: the pending-batch dialog shown
        instead of an immediate factory reset."""
        try:
            job = self._batch_job_load()
        except Exception:
            job = {}
        n = sum(1 for s in (job.get("students") or {}).values()
                if s.get("status") == "pending")
        msg = (f"A BATCH scoring job is still waiting at the AI provider "
               f"({n} student(s) not collected yet).\n\n"
               "An authorization reset now would erase this machine's keys and "
               "its record of that job -- the student work at the provider "
               "could then be neither collected nor cancelled.\n\n"
               "CANCEL THE BATCH FIRST cancels the job at the provider, "
               "defers the uncollected students (their scans stay in "
               "'02 Inbox Scans'), and then continues with the authorization "
               "reset.\n\nBACK OUT stops the reset and leaves the batch job "
               "waiting.")
        win = ctk.CTkToplevel(self.root)
        win.title("A BATCH JOB IS STILL PENDING")
        self._clamp_toplevel(win, 780, 420)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        ctk.CTkLabel(win, text="A BATCH JOB IS STILL PENDING",
                     font=self.header_font).pack(pady=(24, 10))
        ctk.CTkLabel(win, text=msg, font=self.main_font, wraplength=700,
                     justify="left").pack(pady=8, padx=30)
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=18)

        def cancel_first():
            try:
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass
            self.log_message("-> Authorization reset: cancelling the pending "
                             "batch at the provider first...")
            threading.Thread(target=self._factory_reset_cancel_then_reset,
                             args=(job, window), daemon=True).start()

        def back_out():
            try:
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass
            self.log_message("-> Authorization reset backed out. The pending "
                             "batch job is untouched.")

        ctk.CTkButton(row, text="CANCEL THE BATCH FIRST", font=self.main_font,
                      height=48, fg_color="#c0392b", hover_color="#e74c3c",
                      command=cancel_first).pack(side="left", padx=12)
        ctk.CTkButton(row, text="BACK OUT", font=self.main_font, height=48,
                      fg_color="#1e8449", hover_color="#27ae60",
                      command=back_out).pack(side="left", padx=12)
        self._make_text_copyable(win)
        win.after(200, win.grab_set)

    def _factory_reset_cancel_then_reset(self, job, window):
        """[PATCH:batch] WO-B1-2 Task 7 worker: cancel at the provider FIRST
        (the reset is about to wipe the keys the daemon needs for that),
        then run the reset on the UI thread."""
        self._cancel_batch_job(job)
        self.root.after(0, lambda: self._do_factory_reset(window))

    def _do_factory_reset(self, window):
        """The factory reset itself (split out of execute_factory_reset by
        [PATCH:batch] WO-B1-2 Task 7; the body is unchanged)."""
        self.log_message("Requesting administrative privileges for the authorization reset...")
        try:
            # 1. Purge the API keys securely via pkexec
            subprocess.run(['pkexec', 'sh', '-c', 'set -e; rm -f /etc/paragon/.env /etc/paragon/sa.json /etc/ledger-initialized; set +e; systemctl stop ledger-daemon.service; exit 0'], check=True)
            
            # 2. Scrub the signature from the SQLite database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM Settings WHERE Setting_Key IN ('liability_signer', 'liability_institution', 'liability_timestamp')")
            conn.commit()
            conn.close()
            
            self.log_message("-> AUTHORIZATION RESET SUCCESSFUL. System locked.")
            window.destroy()
            
            # 3. Re-evaluate first-run state THROUGH the tier gate (an initializer
            #    gets the wizard; a lower tier gets the awaiting-setup notice).
            self.check_first_run()
            
        except subprocess.CalledProcessError:
            self.log_message("[!] ERROR: Administrator authorization failed. Reset aborted.")
        except Exception as e:
            self.log_message(f"[!] Database Write Error (Authorization Reset): {e}")
    def open_manage_api_keys(self):
        """[PATCH:keymgr] WO-APP1 T5: add, replace or clear the DIRECT-route
        provider keys (Google AI Studio, Anthropic) without a factory reset.

        Status comes from the daemon's health endpoint (the GUI cannot read
        /etc/paragon/.env — root-owned by design). Writes follow the
        wizard's custody pattern exactly: stage in the per-user temp dir,
        ONE pkexec step (engine/ledger_keyenv.py) that read-modify-writes
        /etc/paragon/.env AS ROOT preserving every untouched line verbatim,
        sets root:<owner group> 0640, restarts ledger-daemon.service; then the
        GUI re-polls health and says what happened in plain words. Key
        values never touch logs, the database, or any other file. The
        Vertex route is read-only here (managed in SYSTEM INITIALIZATION)."""
        import shlex
        VENV_PY = "/opt/ledger/venv/bin/python"
        KEYENV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ledger_keyenv.py")

        win = ctk.CTkToplevel(self.root)
        win.title("MANAGE API KEYS")
        self._clamp_toplevel(win, 880, 640)
        win.minsize(820, 560)
        win.transient(self.root)

        ctk.CTkLabel(win, text="MANAGE API KEYS",
                     font=self.header_font).pack(pady=(18, 4))
        intro = ("Add or replace the keys the Ledger uses to reach its AI "
                 "providers — direct routes only: a Google AI Studio key "
                 "(starts AQ. or AIza) and an Anthropic key (starts "
                 "sk-ant-). Saving a change asks for your password, then "
                 "restarts the Ledger's engine. Key values are never shown "
                 "again after they are saved.")
        ctk.CTkLabel(win, text=intro, font=self.main_font, wraplength=780,
                     justify="left").pack(pady=(0, 10), padx=30)

        action_status = ctk.CTkLabel(win, text="", font=self.main_font,
                                     wraplength=780, justify="left",
                                     text_color="#e67e22")
        st = {"health": None}
        rows = {}

        def say(text, color="#e67e22"):
            action_status.configure(text=text, text_color=color)

        def make_row(provider, title_text, placeholder):
            frame = ctk.CTkFrame(win, corner_radius=10)
            frame.pack(fill="x", padx=30, pady=6)
            ctk.CTkLabel(frame, text=title_text,
                         font=ctk.CTkFont(family="Ubuntu", size=15,
                                          weight="bold"),
                         anchor="w").pack(fill="x", padx=14, pady=(10, 0))
            status = ctk.CTkLabel(frame,
                                  text="Checking the appliance's current "
                                       "setup...",
                                  font=self.main_font, wraplength=760,
                                  justify="left", anchor="w",
                                  text_color="#7f8c8d")
            status.pack(fill="x", padx=14, pady=(2, 6))
            entry_row = ctk.CTkFrame(frame, fg_color="transparent")
            entry_row.pack(fill="x", padx=14, pady=(0, 10))
            entry = ctk.CTkEntry(entry_row, font=self.main_font, width=340,
                                 show="•", placeholder_text=placeholder)
            entry.pack(side="left", padx=(0, 8))

            def toggle_show():
                hidden = entry.cget("show") == "•"
                entry.configure(show="" if hidden else "•")
                show_btn.configure(text="HIDE" if hidden else "SHOW")

            show_btn = ctk.CTkButton(entry_row, text="SHOW", width=70,
                                     font=self.main_font, fg_color="#333333",
                                     hover_color="#555555",
                                     command=toggle_show)
            show_btn.pack(side="left", padx=(0, 8))
            save_btn = ctk.CTkButton(entry_row, text="SAVE KEY", width=105,
                                     font=self.main_font, fg_color="#1e8449",
                                     hover_color="#27ae60",
                                     command=lambda: on_save(provider))
            save_btn.pack(side="left", padx=(0, 8))
            clear_btn = ctk.CTkButton(entry_row, text="CLEAR KEY", width=105,
                                      font=self.main_font, fg_color="#8B0000",
                                      hover_color="#A52A2A",
                                      command=lambda: on_clear(provider))
            clear_btn.pack(side="left")
            rows[provider] = {"status": status, "entry": entry,
                              "entry_row": entry_row, "save": save_btn,
                              "clear": clear_btn, "show": show_btn}

        make_row("google", "GOOGLE (AI STUDIO)",
                 "Paste the new Google AI Studio key")
        make_row("anthropic", "ANTHROPIC",
                 "Paste the new Anthropic key (sk-ant-...)")

        action_status.pack(fill="x", padx=30, pady=(4, 2))
        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(pady=(4, 16))
        ctk.CTkButton(btn_row, text="RE-CHECK STATUS", font=self.main_font,
                      fg_color="#1f538d", hover_color="#14375e",
                      command=lambda: refresh_status()).pack(side="left",
                                                             padx=8)
        ctk.CTkButton(btn_row, text="CLOSE", font=self.main_font,
                      fg_color="#333333", hover_color="#555555",
                      command=win.destroy).pack(side="left", padx=8)

        def apply_health(h):
            st["health"] = h
            g, a = rows["google"], rows["anthropic"]
            if h is None:
                for r in (g, a):
                    r["status"].configure(
                        text="The Ledger daemon is not answering right now, "
                             "so the current setup cannot be shown. Try "
                             "RE-CHECK STATUS in a moment.")
                return
            groute = h.get("google_route")
            aroute = h.get("anthropic_route")
            if groute == "vertex":
                # [PATCH:keymgr] 5f: the Vertex route is OUT OF SCOPE here.
                g["status"].configure(
                    text="This appliance uses your institution's Google "
                         "Cloud (Vertex) setup for Google models. That is "
                         "managed in SYSTEM INITIALIZATION, not here, and "
                         "is shown for information only.")
                g["entry_row"].pack_forget()
            else:
                g["entry_row"].pack(fill="x", padx=14, pady=(0, 10))
                if groute == "developer-api":
                    g["status"].configure(
                        text="Configured — a direct AI Studio key is "
                             "installed and in use.")
                    g["clear"].configure(state="normal")
                else:
                    g["status"].configure(
                        text="Not configured — no Google key on this "
                             "appliance.")
                    g["clear"].configure(state="disabled")
            if aroute == "direct-api":
                a["status"].configure(
                    text="Configured — a direct Anthropic key is installed "
                         "and in use.")
                a["clear"].configure(state="normal")
            elif aroute == "vertex":
                a["status"].configure(
                    text="Claude models currently run through your "
                         "institution's Google Cloud (Vertex) setup. Adding "
                         "a direct Anthropic key here will take over for "
                         "Claude models.")
                a["clear"].configure(state="disabled")
            else:
                a["status"].configure(
                    text="Not configured — no Anthropic key on this "
                         "appliance.")
                a["clear"].configure(state="disabled")

        def refresh_status():
            def _worker():
                try:
                    h = requests.get(DAEMON_BASE + "/", timeout=5).json()
                except Exception:
                    h = None
                self.root.after(0, lambda: apply_health(h))
            threading.Thread(target=_worker, daemon=True).start()

        def write_change(updates_lines):
            """The ONE privileged write: wizard custody pattern."""
            os.makedirs(TEMP_DIR, exist_ok=True)
            upath = os.path.join(TEMP_DIR, "keymgr-updates.temp")
            fd = os.open(upath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(updates_lines) + "\n")
            say("Requesting your password to secure the key change...")
            self.log_message("Requesting your password to secure the key "
                             "change...")   # never the value
            cmd = ("set -e; %s %s apply %s; "
                   "set +e; systemctl restart ledger-daemon.service; exit 0"
                   % (shlex.quote(VENV_PY), shlex.quote(KEYENV),
                      shlex.quote(upath)))
            try:
                subprocess.run(['pkexec', 'sh', '-c', cmd], check=True,
                               capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                try:
                    os.remove(upath)        # keyenv removes it; belt-and-braces
                except OSError:
                    pass
                detail = (e.stderr or "").strip().splitlines()
                detail = detail[-1] if detail else ""
                say(detail or "The password prompt failed or was cancelled. "
                              "Nothing was changed.", "#c0392b")
                return
            for r in rows.values():
                r["entry"].delete(0, "end")
            say("Change saved. Restarting the Ledger's engine and "
                "re-checking...", "#1e8449")

            def _wait_worker():
                deadline = time.time() + 30
                h = None
                while time.time() < deadline and h is None:
                    try:
                        h = requests.get(DAEMON_BASE + "/", timeout=4).json()
                    except Exception:
                        time.sleep(2.0)

                def _done():
                    apply_health(h)
                    if h is None:
                        say("The change was saved, but the Ledger's engine "
                            "has not come back up yet. Give it a moment, "
                            "then press RE-CHECK STATUS.", "#e67e22")
                    else:
                        say("Done. The rows above show the appliance's "
                            "current setup.", "#1e8449")
                self.root.after(0, _done)
            threading.Thread(target=_wait_worker, daemon=True).start()

        def open_billing_confirm(key_value, warn_text):
            """[PATCH:keymgr] 5e: adding an AI Studio key keeps the
            initialization wizard's billing confirmation, same text."""
            d = ctk.CTkToplevel(win)
            d.title("Billing and Student Work — Required Reading")
            self._clamp_toplevel(d, 920, 640)
            d.transient(win)
            ctk.CTkLabel(d, text="BILLING AND STUDENT WORK — REQUIRED "
                                 "READING",
                         font=self.header_font).pack(pady=(18, 8))
            box = ctk.CTkTextbox(d, width=840, height=380, wrap="word",
                                 font=ctk.CTkFont(family="Ubuntu", size=13))
            box.pack(pady=(0, 6), padx=30)
            box.insert("0.0", BILLING_DISCLOSURE_TEXT)
            box.configure(state="disabled")
            bvar = ctk.StringVar(value="off")
            ctk.CTkCheckBox(
                d,
                text="I have enabled billing on this Google AI Studio key.",
                variable=bvar, onvalue="on", offvalue="off",
                font=ctk.CTkFont(family="Ubuntu", size=13, weight="bold"),
                text_color="#c0392b").pack(pady=(8, 8))
            brow = ctk.CTkFrame(d, fg_color="transparent")
            brow.pack(pady=(0, 16))

            def go():
                if bvar.get() != "on":
                    return
                d.destroy()
                if warn_text:
                    self.log_message("[keymgr] " + warn_text)
                write_change(['LEDGER_GOOGLE_API_KEY="%s"' % key_value])
            ctk.CTkButton(brow, text="SAVE THIS KEY", font=self.header_font,
                          height=44, width=220, fg_color="#1e8449",
                          hover_color="#27ae60",
                          command=go).pack(side="left", padx=10)
            ctk.CTkButton(brow, text="CANCEL", font=self.header_font,
                          height=44, width=160, fg_color="#333333",
                          hover_color="#555555",
                          command=d.destroy).pack(side="left", padx=10)
            self._make_text_copyable(d)

        def on_save(provider):
            key = rows[provider]["entry"].get().strip()
            if not key:
                say("Paste the key first.")
                return
            if '"' in key or "\n" in key or "\r" in key:
                say("That doesn't look like a key — it contains a quote or "
                    "a line break. Check the paste.", "#c0392b")
                return
            if provider == "google":
                if key.startswith(ANTHROPIC_PREFIX):
                    say("That looks like an Anthropic key (sk-ant-...). "
                        "Paste it in the ANTHROPIC row instead.", "#c0392b")
                    return
                warn = None
                if not key.startswith(AI_STUDIO_PREFIXES):
                    warn = ("AI Studio keys normally begin with AQ. or "
                            "AIza — double-check the paste.")
                    say(warn)
                open_billing_confirm(key, warn)
            else:
                if key.startswith(AI_STUDIO_PREFIXES):
                    say("That looks like a Google AI Studio key. Paste it "
                        "in the GOOGLE (AI STUDIO) row instead.", "#c0392b")
                    return
                if not key.startswith(ANTHROPIC_PREFIX):
                    say("Anthropic keys begin with sk-ant- — copy the whole "
                        "key from the Anthropic Console.", "#c0392b")
                    return
                write_change(['LEDGER_ANTHROPIC_API_KEY="%s"' % key])

        def on_clear(provider):
            h = st["health"] or {}
            groute = h.get("google_route")
            aroute = h.get("anthropic_route")
            # A Vertex route counts as a working provider: clearing the AI
            # Studio key on a box that also holds a GCP project falls back
            # to Vertex. The rewriter reads the real file and refuses a
            # genuine last-provider clear even if this pre-check is stale.
            other_alive = (aroute in ("direct-api", "vertex")
                           if provider == "google"
                           else groute in ("developer-api", "vertex"))
            if not other_alive:
                say("This is the appliance's only working AI provider. The "
                    "Ledger cannot run with no provider at all — add the "
                    "other provider's key first, then clear this one.",
                    "#c0392b")
                return
            name = ("Google AI Studio" if provider == "google"
                    else "Anthropic")
            env_key = ("LEDGER_GOOGLE_API_KEY" if provider == "google"
                       else "LEDGER_ANTHROPIC_API_KEY")
            d = ctk.CTkToplevel(win)
            d.title("CLEAR THIS KEY?")
            self._clamp_toplevel(d, 640, 300)
            d.transient(win)
            ctk.CTkLabel(d, text="CLEAR THIS KEY?",
                         font=self.header_font).pack(pady=(22, 8))
            ctk.CTkLabel(d, text=("This removes the %s key from the "
                                  "appliance. Models from this provider "
                                  "will stop working until a new key is "
                                  "added. The other provider is not "
                                  "touched." % name),
                         font=self.main_font, wraplength=560,
                         justify="left").pack(pady=6, padx=30)
            drow = ctk.CTkFrame(d, fg_color="transparent")
            drow.pack(pady=16)

            def go():
                d.destroy()
                write_change(["%s=" % env_key])
            ctk.CTkButton(drow, text="CLEAR THE KEY", font=self.main_font,
                          height=44, fg_color="#8B0000",
                          hover_color="#A52A2A",
                          command=go).pack(side="left", padx=10)
            ctk.CTkButton(drow, text="KEEP IT", font=self.main_font,
                          height=44, fg_color="#1e8449",
                          hover_color="#27ae60",
                          command=d.destroy).pack(side="left", padx=10)
            self._make_text_copyable(d)

        refresh_status()
        # Test seam, same pattern as self._wizard_hooks / self._fmt_hooks.
        self._keymgr_hooks = {"win": win, "rows": rows, "say": say,
                              "apply_health": apply_health,
                              "refresh_status": refresh_status,
                              "on_save": on_save, "on_clear": on_clear,
                              "write_change": write_change,
                              "action_status": action_status}
        self._make_text_copyable(win)

    def open_keys_and_accounts(self):
        """Launch the tier-aware Keys & Accounts GUI (ledger-keymgr-gui.py).

        This replaces the old in-app open_user_manager / open_fido2_enrollment
        windows. Those drove the retired 03-Admin-Scripts/02-enroll-fido2-user.sh,
        which ran 'dracut -f' after enrollment -- the documented cause of the June
        boot incident. The new GUI drives ledger-keytool / ledger-usermgr, which
        write only the on-disk LUKS2 header and NEVER touch the boot image
        (see system map Rev f, registers #5/#6/#23). Folding this in here also
        removes the need for the separate "Ledger Keys & Accounts" desktop launcher.
        """
        # Shared engine published to /opt/ledger (system map §11.8).
        keymgr_python = "/opt/ledger/venv/bin/python"
        keymgr_script = "/opt/ledger/engine/ledger-keymgr-gui.py"

        if not (os.path.exists(keymgr_python) and os.path.exists(keymgr_script)):
            self.log_message(
                "[!] Keys & Accounts GUI not found. Expected: "
                f"{keymgr_python} {keymgr_script}"
            )
            return

        try:
            # Run as the logged-in user (NOT sudo); the GUI calls the privileged
            # helpers itself via the prompt-free /etc/sudoers.d/ledger drop-in.
            subprocess.Popen([keymgr_python, keymgr_script])
            self.log_message("-> Opened Keys & Accounts.")
        except Exception as e:
            self.log_message(f"[!] Could not open Keys & Accounts: {e}")

    def open_fido2_enrollment(self, target_user=None):
        """Launches the FIDO2 enrollment sequence with a safety confirmation."""
        if not target_user:
            target_user = getpass.getuser()

        enroll_win = ctk.CTkToplevel(self.root)
        enroll_win.title("SYSTEM LOCKDOWN")
        enroll_win.geometry("750x450")
        enroll_win.configure(fg_color="#8B0000") 
        enroll_win.transient(self.root)
        enroll_win.protocol("WM_DELETE_WINDOW", lambda: None) 

        warning_text = (
            f"CRITICAL HARDWARE SECURING IN PROGRESS FOR: {target_user.upper()}\n\n"
            "ACTION REQUIRED: Please ensure the FIDO2 key is inserted.\n"
            "You are binding this key to both the User Profile and the Encrypted Drive.\n\n"
            "When the gold pad lights up SOLID, tap it.\n"
            "You will need to wait for the solid light and tap TWICE during this process.\n\n"
            "DO NOT REMOVE POWER OR UNPLUG THE APPLIANCE."
        )

        warn_label = ctk.CTkLabel(
            enroll_win, text=warning_text, font=ctk.CTkFont(family="Ubuntu", size=16, weight="bold"),
            text_color="white", wraplength=700
        )
        warn_label.pack(pady=(40, 20))

        progress = ctk.CTkProgressBar(enroll_win, width=500, mode="indeterminate", fg_color="#550000", progress_color="white")
        
        def enrollment_worker():
            try:
                script_path = os.path.join(ENGINE_DIR, "03-Admin-Scripts", "02-enroll-fido2-user.sh")
                cmd = ['sudo', os.path.abspath(script_path), target_user]
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                self.root.after(0, lambda: success_callback(result.stdout))
            except subprocess.CalledProcessError as e:
                self.root.after(0, lambda: error_callback(e.stderr))
            except Exception as e:
                self.root.after(0, lambda: error_callback(str(e)))

        def success_callback(log_output):
            progress.stop()
            enroll_win.destroy()
            self.log_message(f"-> FIDO2 ENROLLMENT SUCCESSFUL for {target_user}.")

        def error_callback(error_msg):
            progress.stop()
            enroll_win.destroy()
            self.log_message(f"[!] FIDO2 ENROLLMENT FAILED for {target_user}.")
            if error_msg: self.log_message(f"[!] {error_msg}")

        def confirm_action():
            proceed_btn.pack_forget()
            progress.pack(pady=20)
            progress.start()
            threading.Thread(target=enrollment_worker, daemon=True).start()

        proceed_btn = ctk.CTkButton(
            enroll_win, text="I UNDERSTAND. PROCEED WITH BINDING.", font=self.header_font, 
            height=50, fg_color="#333333", hover_color="#555555", command=confirm_action
        )
        proceed_btn.pack(pady=20)
        
        self._make_text_copyable(enroll_win)
        enroll_win.grab_set()

    def open_user_manager(self):
        """Admin dashboard for OS User creation, deletion, and key management."""
        mgr_win = ctk.CTkToplevel(self.root)
        mgr_win.title("OS USER MANAGEMENT")
        mgr_win.geometry("800x500")
        mgr_win.transient(self.root)

        header_label = ctk.CTkLabel(mgr_win, text="OS USER ARCHITECTURE", font=self.header_font)
        header_label.pack(pady=(20, 10))

        # Treeview Configuration
        columns = ("username", "group")
        tree = ttk.Treeview(mgr_win, columns=columns, show="headings", height=8)
        self._style_dark_treeview()

        tree.heading("username", text="System Username")
        tree.column("username", width=400, anchor="center")
        tree.heading("group", text="Primary Access")
        tree.column("group", width=350, anchor="center")
        tree.pack(fill="x", padx=20, pady=10)

        selected_user = ctk.StringVar()

        def load_users():
            for item in tree.get_children(): tree.delete(item)
            try:
                dp_group = grp.getgrnam('district-personnel')
                for user in dp_group.gr_mem:
                    tree.insert("", "end", values=(user, "district-personnel"))
            except KeyError:
                pass

        def on_tree_select(event):
            selected = tree.selection()
            if selected:
                selected_user.set(tree.item(selected[0], 'values')[0])

        tree.bind('<<TreeviewSelect>>', on_tree_select)

        # Input & Button Frame
        action_frame = ctk.CTkFrame(mgr_win, fg_color="transparent")
        action_frame.pack(pady=10)

        new_user_entry = ctk.CTkEntry(action_frame, placeholder_text="New Username (no spaces)", width=200)
        new_user_entry.grid(row=0, column=0, padx=10, pady=10)

        def run_admin_script(action, target):
            if not target: return
            try:
                script_path = os.path.join(ENGINE_DIR, "03-Admin-Scripts", "04-manage-os-users.sh")
                cmd = ['sudo', os.path.abspath(script_path), action, target]
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                self.log_message(f"-> OS Action [{action}] successful for {target}.")
                load_users()
            except subprocess.CalledProcessError as e:
                self.log_message(f"[!] OS Action [{action}] FAILED: {e.stderr}")

        ctk.CTkButton(action_frame, text="ADD USER", fg_color="#1e8449", hover_color="#27ae60", 
                      command=lambda: run_admin_script("CREATE", new_user_entry.get().strip().lower())).grid(row=0, column=1, padx=5)
        ctk.CTkButton(action_frame, text="DELETE USER", fg_color="#c0392b", hover_color="#e74c3c", 
                      command=lambda: run_admin_script("DELETE", selected_user.get())).grid(row=0, column=2, padx=5)

        # FIDO2 Management Row
        fido_frame = ctk.CTkFrame(mgr_win, fg_color="transparent")
        fido_frame.pack(pady=10)

        ctk.CTkButton(fido_frame, text="ENROLL KEY FOR SELECTED USER", fg_color="#1f538d", hover_color="#14375e", 
                      command=lambda: self.open_fido2_enrollment(target_user=selected_user.get()) if selected_user.get() else None).pack(side="left", padx=10)
        ctk.CTkButton(fido_frame, text="REVOKE DESKTOP KEY (LOST KEY)", fg_color="#d35400", hover_color="#e67e22", 
                      command=lambda: run_admin_script("REVOKE_PAM", selected_user.get())).pack(side="left", padx=10)

        load_users()
        self._make_text_copyable(mgr_win)

    # ==========================================
    # SURGERY AREA 5: THE VISUAL CALIBRATION STUDIO
    # ==========================================
    def open_calibration_tool(self, is_intercept=False):
        """Launches the interactive visual canvas to lock in OpenCV array slices."""
        
        # 1. Locate the target document (Student Scan > Master PDF > Abort)
        scan_files = [f for f in os.listdir(UNEVALUATED_DIR) if f.lower().endswith('.pdf')]
        
        target_pdf = None
        if scan_files:
            target_pdf = os.path.join(UNEVALUATED_DIR, scan_files[0])
            if not is_intercept: self.log_message(f"Calibrating using Student Scan: {scan_files[0]}")
        else:
            self.log_message("No student scans found. Calibration requires a physical scan.")
            
            # Launch a native CustomTkinter informational modal
            info_win = ctk.CTkToplevel(self.root)
            info_win.title("SCAN REQUIRED")
            info_win.geometry("500x220")
            info_win.transient(self.root)
            
            info_label = ctk.CTkLabel(
                info_win, 
                text="There is no scanned document to calibrate.\n\nPlease drop a scanned student assignment into the\n'02 Inbox Scans' folder before calibrating.", 
                font=self.main_font,
                text_color="white"
            )
            info_label.pack(pady=(40, 20))
            
            ok_btn = ctk.CTkButton(
                info_win, 
                text="OK", 
                font=self.header_font,
                height=45,
                fg_color="#1e8449", 
                hover_color="#27ae60", 
                command=info_win.destroy
            )
            ok_btn.pack(pady=(10, 0))
            
            self._make_text_copyable(info_win)
            info_win.grab_set() # Lock focus to this popup
            if is_intercept: self.shut_down()   # [PATCH:calib-close-reset] D1b: RUN cannot proceed
            return

        # 2. Render the massive native image
        try:
            doc = fitz.open(target_pdf)
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16), alpha=False)
            temp_img_path = os.path.join(TEMP_DIR, "temp_calibration_render.png")
            pix.save(temp_img_path)
            doc.close()
        except Exception as e:
            self.log_message(f"[!] Error rendering calibration preview: {e}")
            if is_intercept: self.shut_down()   # [PATCH:calib-close-reset] D1b: RUN cannot proceed
            return

        # 3. Calculate scaling math to fit UI
        from PIL import ImageTk
        raw_img = Image.open(temp_img_path)
        native_w, native_h = raw_img.size
        
        # [PATCH:calib-fit] fit the studio to the screen (the dashboard's clamp):
        # at a fixed 800 px the page-number box and SAVE sat below the bottom of
        # a 768-px display. Stored coordinates are native pixels, so unaffected.
        target_ui_height = min(800, self.root.winfo_screenheight() - WIZARD_SCREEN_ALLOWANCE - 120)
        scale_factor = target_ui_height / native_h
        ui_w = int(native_w * scale_factor)
        
        ui_img = raw_img.resize((ui_w, target_ui_height), Image.Resampling.LANCZOS)
        
        # 4. Build the UI Canvas
        calib_win = ctk.CTkToplevel(self.root)
        calib_win.title("SCANNER CALIBRATION")
        calib_win.geometry(f"{ui_w + 40}x{target_ui_height + 120}")
        calib_win.transient(self.root)
        
        # [PATCH:calib-close-reset] WO2 D1: when RUN opened this studio, closing
        # it with the window's X left is_running=True and RUN disabled, so the
        # batch could never start (CALIBRATE SCANNER opens the studio save-only).
        # The X now cancels the run, exactly as SHUT DOWN does.
        if is_intercept:
            def cancel_intercept():
                calib_win.destroy()
                self.log_message("Calibration closed without saving -- run cancelled.")
                self.shut_down()
            calib_win.protocol("WM_DELETE_WINDOW", cancel_intercept)

        header_label = ctk.CTkLabel(calib_win, text="ADJUST DOCUMENT CROP ZONES", font=self.header_font)
        header_label.pack(pady=(10, 5))
        
        canvas = tk.Canvas(calib_win, width=ui_w, height=target_ui_height, bg="black", highlightthickness=0)
        canvas.pack(pady=5)
        
        # Keep a persistent reference to the image to prevent garbage collection
        calib_win.tk_img = ImageTk.PhotoImage(ui_img)
        canvas.create_image(0, 0, anchor="nw", image=calib_win.tk_img)

        # 5. Interactive LibreOffice-Style Box Logic
        class CalibrationBox:
            def __init__(self, cv, name, x, y, w, h, color):
                self.cv = cv
                self.s = 5 # Handle radius
                
                # INITIAL PLACEMENT RESTRAINT: Prevent boxes from starting off-screen
                if x + w > ui_w:
                    w = ui_w - x - 2 # 2px visual buffer
                if y + h > target_ui_height:
                    h = target_ui_height - y - 2
                
                # Floating Label (Pinned 10px above the top-left corner)
                self.label_id = cv.create_text(x, y - 10, text=name, fill=color, font=("Ubuntu", 12, "bold"), anchor="sw")
                
                # Main Body
                self.rect_id = cv.create_rectangle(x, y, x+w, y+h, outline=color, width=3, fill="", activeoutline="white")
                # 4 Corner Nodes
                self.tl = cv.create_rectangle(x-self.s, y-self.s, x+self.s, y+self.s, fill=color)
                self.tr = cv.create_rectangle(x+w-self.s, y-self.s, x+w+self.s, y+self.s, fill=color)
                self.bl = cv.create_rectangle(x-self.s, y+h-self.s, x+self.s, y+h+self.s, fill=color)
                self.br = cv.create_rectangle(x+w-self.s, y+h-self.s, x+w+self.s, y+h+self.s, fill=color)
                
                # Event Bindings
                for item in (self.rect_id, self.tl, self.tr, self.bl, self.br, self.label_id):
                    cv.tag_bind(item, "<ButtonPress-1>", self.start_action)
                    
                cv.tag_bind(self.rect_id, "<B1-Motion>", self.move_all)
                cv.tag_bind(self.label_id, "<B1-Motion>", self.move_all) # Label drag moves the box too
                cv.tag_bind(self.tl, "<B1-Motion>", lambda e: self.resize(e, "tl"))
                cv.tag_bind(self.tr, "<B1-Motion>", lambda e: self.resize(e, "tr"))
                cv.tag_bind(self.bl, "<B1-Motion>", lambda e: self.resize(e, "bl"))
                cv.tag_bind(self.br, "<B1-Motion>", lambda e: self.resize(e, "br"))

            def start_action(self, event):
                self.last_x, self.last_y = event.x, event.y

            def move_all(self, event):
                dx, dy = event.x - self.last_x, event.y - self.last_y
                
                # THE DRAG WALL: Check future coordinates to prevent pushing off canvas
                x1, y1, x2, y2 = self.cv.coords(self.rect_id)
                
                # Clamp dx to screen width
                if x1 + dx < 0: dx = -x1
                elif x2 + dx > ui_w: dx = ui_w - x2
                
                # Clamp dy (Leaves a 20px buffer at the top so the label doesn't get crushed)
                if y1 + dy < 20: dy = 20 - y1 
                elif y2 + dy > target_ui_height: dy = target_ui_height - y2
                
                for item in (self.rect_id, self.tl, self.tr, self.bl, self.br, self.label_id):
                    self.cv.move(item, dx, dy)
                    
                self.last_x, self.last_y = self.last_x + dx, self.last_y + dy

            def resize(self, event, node):
                x1, y1, x2, y2 = self.cv.coords(self.rect_id)
                
                # THE RESIZE WALL: Clamp the incoming mouse coordinates
                nx = max(0, min(event.x, ui_w))
                ny = max(20, min(event.y, target_ui_height)) # 20px buffer for label clearance
                
                # Collision logic prevents inverting the box
                if node == "tl" and nx < x2-20 and ny < y2-20: x1, y1 = nx, ny
                elif node == "tr" and nx > x1+20 and ny < y2-20: x2, y1 = nx, ny
                elif node == "bl" and nx < x2-20 and ny > y1+20: x1, y2 = nx, ny
                elif node == "br" and nx > x1+20 and ny > y1+20: x2, y2 = nx, ny
                else: return

                self.cv.coords(self.rect_id, x1, y1, x2, y2)
                self.cv.coords(self.label_id, x1, y1 - 10) # Keep label pinned during resize
                self.cv.coords(self.tl, x1-self.s, y1-self.s, x1+self.s, y1+self.s)
                self.cv.coords(self.tr, x2-self.s, y1-self.s, x2+self.s, y1+self.s)
                self.cv.coords(self.bl, x1-self.s, y2-self.s, x1+self.s, y2+self.s)
                self.cv.coords(self.br, x2-self.s, y2-self.s, x2+self.s, y2+self.s)
                
            def get_coords(self):
                x1, y1, x2, y2 = self.cv.coords(self.rect_id)
                return (int(x1), int(y1), int(x2 - x1), int(y2 - y1))

        # 6. Fetch native coords and generate the boxes
        # [PATCH:calib-round] round(), not int(), here and in save_and_proceed:
        # truncating both ways cost ~4 native px per open+save, so untouched
        # boxes crept up-left and shrank on every RUN.
        t_coords = self.get_crop_coordinates('crop_title')
        id_coords = self.get_crop_coordinates('crop_id')
        f_coords = self.get_crop_coordinates('crop_footer')
        
        box_title = CalibrationBox(canvas, "Title", round(t_coords[0]*scale_factor), round(t_coords[1]*scale_factor), round(t_coords[2]*scale_factor), round(t_coords[3]*scale_factor), "#3498db") # Blue
        box_id = CalibrationBox(canvas, "ID", round(id_coords[0]*scale_factor), round(id_coords[1]*scale_factor), round(id_coords[2]*scale_factor), round(id_coords[3]*scale_factor), "#2ecc71") # Green
        box_footer = CalibrationBox(canvas, "Page Number", round(f_coords[0]*scale_factor), round(f_coords[1]*scale_factor), round(f_coords[2]*scale_factor), round(f_coords[3]*scale_factor), "#e74c3c") # Red
        
        # 7. Save Protocol
        def save_and_proceed():
                # Get UI coords and mathematically scale them back up to massive native resolution
                t_ui = box_title.get_coords()
                id_ui = box_id.get_coords()
                f_ui = box_footer.get_coords()
                
                t_nat = f"{round(t_ui[0]/scale_factor)},{round(t_ui[1]/scale_factor)},{round(t_ui[2]/scale_factor)},{round(t_ui[3]/scale_factor)}"
                id_nat = f"{round(id_ui[0]/scale_factor)},{round(id_ui[1]/scale_factor)},{round(id_ui[2]/scale_factor)},{round(id_ui[3]/scale_factor)}"
                f_nat = f"{round(f_ui[0]/scale_factor)},{round(f_ui[1]/scale_factor)},{round(f_ui[2]/scale_factor)},{round(f_ui[3]/scale_factor)}"
                
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE Settings SET Setting_Value = ? WHERE Setting_Key = 'crop_title'", (t_nat,))
                    cursor.execute("UPDATE Settings SET Setting_Value = ? WHERE Setting_Key = 'crop_id'", (id_nat,))
                    cursor.execute("UPDATE Settings SET Setting_Value = ? WHERE Setting_Key = 'crop_footer'", (f_nat,))
                    conn.commit()
                    conn.close()
                    self.log_message("-> Calibration Zones Locked & Saved.")
                    self.calibrated_this_session = True   # [PATCH:calib-session-skip]
                except Exception as e:
                    self.log_message(f"[!] DB Error saving coordinates: {e}")
                    
                calib_win.destroy()
                
                # Hook logic for Phase 3 Batch Intercept
                if is_intercept:
                    self.log_message("Calibration settings saved. Starting Ledger engine...")
                    # ARCHITECTURAL FIX: Push the blocking API extraction into a background thread
                    threading.Thread(target=self.run_batch_extraction, args=(target_pdf,), daemon=True).start()
                
        save_btn = ctk.CTkButton(calib_win, text="SAVE & PROCEED" if is_intercept else "SAVE", font=self.header_font, height=50, fg_color="#1e8449", hover_color="#27ae60", command=save_and_proceed)
        save_btn.pack(pady=(10, 20))

        self._make_text_copyable(calib_win)
        calib_win.grab_set()

        # [PATCH:formatter] Test seam, same pattern as self._wizard_hooks and
        # self._fmt_hooks: lets the WO-F2 acceptance driver drag the REAL
        # calibration boxes (through their own mouse handlers) and press the
        # REAL save path. Never read in production.
        self._calib_hooks = {"win": calib_win, "scale": scale_factor,
                             "boxes": {"title": box_title, "id": box_id,
                                       "footer": box_footer},
                             "save": save_and_proceed,
                             "is_intercept": is_intercept}

    def open_roster_manager(self):
        """Launches the dedicated appliance GUI for managing the SQLite Roster and FERPA Pseudonyms."""
        rost_win = ctk.CTkToplevel(self.root)
        rost_win.title("ROSTER MANAGER")
        self._clamp_toplevel(rost_win, 1100, 850) # Expanded height to fit the legal block
        rost_win.transient(self.root)
        
        custom_label = self.get_custom_id_label()
        
        header_label = ctk.CTkLabel(rost_win, text="ROSTER & PRIVACY MANAGEMENT", font=self.header_font)
        header_label.pack(pady=(20, 10))

        # --- FERPA & PSEUDONYM HEADER BLOCK ---
        ferpa_frame = ctk.CTkFrame(rost_win, fg_color="#1a1a1a", corner_radius=10)
        ferpa_frame.pack(fill="x", padx=20, pady=(0, 15))
        
        # 1. Input Field & Tooltip
        input_row = ctk.CTkFrame(ferpa_frame, fg_color="transparent")
        input_row.pack(fill="x", padx=20, pady=(15, 5))
        
        ctk.CTkLabel(input_row, text="Pseudonym System:", font=self.main_font).pack(side="left")
        ctk.CTkLabel(input_row, text="(e.g., \"Ms. Brown's Class Code\" or \"Brownian ID\")", font=ctk.CTkFont(family="Ubuntu", size=12, slant="italic"), text_color="gray").pack(side="left", padx=10)
        
        custom_label_entry = ctk.CTkEntry(input_row, font=self.main_font, width=300)
        custom_label_entry.insert(0, custom_label)
        custom_label_entry.pack(side="left", padx=10)
        
        # Subtext Warning
        warning_label = ctk.CTkLabel(
            ferpa_frame, 
            text="*Do not name your pseudonym system \"STUDENT ID.\" It must be YOUR OWN numbering system, NOT students' DISTRICT-ISSUED student IDs.", 
            font=ctk.CTkFont(family="Ubuntu", size=13, weight="bold"), 
            text_color="#c0392b"
        )
        warning_label.pack(anchor="w", padx=20, pady=(0, 10))
        
        # 2. FERPA Legal Text Box
        ferpa_text = (
            "DATA PRIVACY AND FERPA COMPLIANCE\n\n"
            "The Ledger is designed to process student artifacts while maintaining strict data anonymity. To ensure compliance "
            "with the Family Educational Rights and Privacy Act (FERPA) and local data privacy policies, the use of Personally "
            "Identifiable Information (PII) is strictly prohibited. You must assign randomized or localized proxy numbers to your students.\n\n"
            "You may not under any circumstances utilize students' real names, partial names, or district-issued identification "
            "numbers as the primary identifier within this system."
        )
        ferpa_box = ctk.CTkTextbox(ferpa_frame, height=130, wrap="word", font=ctk.CTkFont(family="Ubuntu", size=12))
        ferpa_box.pack(fill="x", padx=20, pady=(0, 10))
        ferpa_box.insert("0.0", ferpa_text)
        ferpa_box.configure(state="disabled")
        
        # 3. Liability Checkbox & Dynamic Button
        action_row = ctk.CTkFrame(ferpa_frame, fg_color="transparent")
        action_row.pack(fill="x", padx=20, pady=(0, 15))
        
        check_var = ctk.StringVar(value="off")
        
        def toggle_update_btn():
            if check_var.get() == "on":
                save_label_btn.configure(state="normal", fg_color="#1e8449", hover_color="#27ae60")
            else:
                save_label_btn.configure(state="disabled", fg_color="#333333")
                
        def on_entry_edit(*args):
            # Uncheck box and disable button if they alter the text
            check_var.set("off")
            toggle_update_btn()
            
        custom_label_entry.bind("<KeyRelease>", on_entry_edit)

        liability_cb = ctk.CTkCheckBox(
            action_row,
            text="I have read the FERPA compliance statement and understand that students’ personally identifying information (PII) must be pseudonymized before using The Ledger system. I accept full legal responsibility for ensuring that my chosen Pseudonym System does not contain PII or district-issued ID numbers.",
            variable=check_var, onvalue="on", offvalue="off",
            font=ctk.CTkFont(family="Ubuntu", size=11, weight="bold"),
            text_color="white",
            command=toggle_update_btn
        )
        # [PATCH:cosmetics] WO-COS1 T12: dynamic wrap instead of hand-placed \n breaks.
        try:
            liability_cb._text_label.configure(wraplength=820, justify="left")
        except Exception:
            pass
        liability_cb.pack(side="left", fill="x", expand=True, padx=(0, 10))
        
        def save_pseudonym():
            new_label = custom_label_entry.get().strip()
            if not new_label: new_label = "Student Pseudonym" 
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                # Insert or replace seamlessly handles the new database key
                cursor.execute("INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value) VALUES ('custom_id_label', ?)", (new_label,))
                conn.commit()
                conn.close()
                self.log_message(f"-> Pseudonym System Updated to: '{new_label}'. Liability accepted.")
                
                # Dynamically update the tree header and input label without rebooting the window
                tree.heading("b_id", text=new_label)
                b_id_label.configure(text=f"{new_label}:")
                
                # Reset friction mechanism
                check_var.set("off")
                toggle_update_btn()
                
            except Exception as e:
                self.log_message(f"[!] Database Update Error (Pseudonym): {e}")

        save_label_btn = ctk.CTkButton(
            action_row, 
            text="SAVE & UPDATE", 
            font=self.header_font,
            height=45,
            width=170,
            fg_color="#333333", # Starts disabled
            state="disabled",
            command=save_pseudonym
        )
        save_label_btn.pack(side="right")
        # ----------------------------------------
        
        # Treeview Configuration
        columns = ("b_id", "first", "last")
        tree = ttk.Treeview(rost_win, columns=columns, show="headings", height=10)
        
        self._style_dark_treeview()
        
        tree.heading("b_id", text=custom_label)
        tree.column("b_id", width=160, anchor="center")
        tree.heading("first", text="First Name")
        tree.column("first", width=180)
        tree.heading("last", text="Last Name")
        tree.column("last", width=180)
                
        tree.pack(fill="x", padx=20, pady=10)
        
        # Input Controls Frame
        input_frame = ctk.CTkFrame(rost_win, fg_color="transparent")
        input_frame.pack(fill="x", padx=20, pady=10)
        
        # Row 1 Inputs
        row1 = ctk.CTkFrame(input_frame, fg_color="transparent")
        row1.pack(fill="x", pady=5)
        
        b_id_label = ctk.CTkLabel(row1, text=f"{custom_label}:", width=140, anchor="e")
        b_id_label.pack(side="left", padx=5)
        b_id_entry = ctk.CTkEntry(row1, width=100)
        b_id_entry.pack(side="left", padx=5)
        
        ctk.CTkLabel(row1, text="First Name:", width=80, anchor="e").pack(side="left", padx=5)
        first_entry = ctk.CTkEntry(row1, width=150)
        first_entry.pack(side="left", padx=5)
        
        ctk.CTkLabel(row1, text="Last Name:", width=80, anchor="e").pack(side="left", padx=5)
        last_entry = ctk.CTkEntry(row1, width=150)
        last_entry.pack(side="left", padx=5)
                             
        # Action Buttons
        btn_frame = ctk.CTkFrame(rost_win, fg_color="transparent")
        btn_frame.pack(pady=10)

        def apply_entry_menu(entry_widget):
            def custom_entry_paste():
                try:
                    entry_widget.insert("insert", rost_win.clipboard_get())
                except tk.TclError:
                    pass
            menu = tk.Menu(entry_widget, tearoff=False, bg="#2b2b2b", fg="white")
            menu.add_command(label="Copy", command=lambda: entry_widget.event_generate("<<Copy>>"))
            menu.add_command(label="Paste", command=custom_entry_paste)
            entry_widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        for entry in [b_id_entry, first_entry, last_entry, custom_label_entry]:
            apply_entry_menu(entry)

        def load_roster_data():
            for item in tree.get_children():
                tree.delete(item)
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT Bartonian_ID, First_Name, Last_Name FROM Roster ORDER BY Last_Name, First_Name")
                for row in cursor.fetchall():
                    tree.insert("", "end", values=row)
                conn.close()
            except Exception as e:
                self.log_message(f"[!] Roster DB Read Error: {e}")

        def on_tree_select(event):
            selected = tree.selection()
            if selected:
                values = tree.item(selected[0], 'values')
                b_id_entry.delete(0, 'end'); b_id_entry.insert(0, values[0])
                first_entry.delete(0, 'end'); first_entry.insert(0, values[1])
                last_entry.delete(0, 'end'); last_entry.insert(0, values[2])
                
        def add_student():
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO Roster (Bartonian_ID, First_Name, Last_Name)
                    VALUES (?, ?, ?)
                ''', (b_id_entry.get().strip(), first_entry.get().strip(), last_entry.get().strip()))
                conn.commit()
                conn.close()
                load_roster_data() 
                self.roster = self.load_roster() 
                self.log_message(f"-> Added ID {b_id_entry.get()} to database.")
            except sqlite3.IntegrityError:
                self.log_message(f"[!] Cannot Add: Pseudonym ID must be unique.")
            except Exception as e:
                self.log_message(f"[!] Roster Add Error: {e}")

        def update_student():
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE Roster 
                    SET First_Name=?, Last_Name=?
                    WHERE Bartonian_ID=?
                ''', (first_entry.get().strip(), last_entry.get().strip(), b_id_entry.get().strip()))
                conn.commit()
                conn.close()
                load_roster_data()
                self.roster = self.load_roster() 
                self.log_message(f"-> Updated ID {b_id_entry.get()} in database.")
            except Exception as e:
                self.log_message(f"[!] Roster Update Error: {e}")

        def delete_student():
            b_id = b_id_entry.get().strip()
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Roster WHERE Bartonian_ID=?", (b_id,))
                cursor.execute("DELETE FROM Grades WHERE Bartonian_ID=?", (b_id,))
                conn.commit()
                conn.close()
                load_roster_data()
                self.roster = self.load_roster() 
                self.log_message(f"-> Safely purged ID {b_id} and associated grades from system.")
            except Exception as e:
                self.log_message(f"[!] Roster Delete Error: {e}")

        tree.bind('<<TreeviewSelect>>', on_tree_select)
        
        ctk.CTkButton(btn_frame, text="ADD STUDENT", fg_color="#1f538d", hover_color="#14375e", command=add_student).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="UPDATE STUDENT", fg_color="#d35400", hover_color="#e67e22", command=update_student).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="DELETE STUDENT", fg_color="#c0392b", hover_color="#e74c3c", command=delete_student).pack(side="left", padx=10)
        
        load_roster_data()
        self._make_text_copyable(rost_win)

    def open_assignment_manager(self):
        """Launches the dedicated appliance GUI for managing the SQLite Assignments."""
        assn_win = ctk.CTkToplevel(self.root)
        assn_win.title("ASSIGNMENT MANAGER")
        self._clamp_toplevel(assn_win, 800, 500)
        assn_win.transient(self.root)

        header_label = ctk.CTkLabel(assn_win, text="ASSIGNMENT MANAGEMENT", font=self.header_font)
        header_label.pack(pady=(20, 10))

        # Treeview Configuration
        columns = ("a_key", "title", "max_score")
        tree = ttk.Treeview(assn_win, columns=columns, show="headings", height=8)

        self._style_dark_treeview()

        tree.heading("a_key", text="System Key")
        tree.column("a_key", width=0, stretch=False) # Hides internal key from user
        tree.heading("title", text="Display Title")
        tree.column("title", width=400)
        tree.heading("max_score", text="Max Score")
        tree.column("max_score", width=150, anchor="center")

        tree.pack(fill="x", padx=20, pady=10)

        # Input Controls Frame
        input_frame = ctk.CTkFrame(assn_win, fg_color="transparent")
        input_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(input_frame, text="Display Title:", width=100, anchor="e").pack(side="left", padx=5)
        title_entry = ctk.CTkEntry(input_frame, width=300)
        title_entry.pack(side="left", padx=5)

        ctk.CTkLabel(input_frame, text="Max Score:", width=80, anchor="e").pack(side="left", padx=5)
        score_entry = ctk.CTkEntry(input_frame, width=80)
        score_entry.pack(side="left", padx=5)

        # SURGICAL INSERTION: Universal Right-Click Menu for Assignment Entries
        def apply_assn_menu(entry_widget):
            def custom_assn_paste():
                try:
                    entry_widget.insert("insert", assn_win.clipboard_get())
                except tk.TclError:
                    pass
            menu = tk.Menu(entry_widget, tearoff=False, bg="#2b2b2b", fg="white")
            menu.add_command(label="Copy", command=lambda: entry_widget.event_generate("<<Copy>>"))
            menu.add_command(label="Paste", command=custom_assn_paste)
            entry_widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        for entry in [title_entry, score_entry]:
            apply_assn_menu(entry)

        # Hidden variable to track which assignment is selected for DB queries
        selected_key = ctk.StringVar()

        # Action Buttons
        btn_frame = ctk.CTkFrame(assn_win, fg_color="transparent")
        btn_frame.pack(pady=20)

        # --- INTERNAL LOGIC FUNCTIONS ---
        def load_assignment_data():
            for item in tree.get_children():
                tree.delete(item)
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT Assignment_Key, Display_Title, Max_Score FROM Assignments ORDER BY Display_Title")
                for row in cursor.fetchall():
                    tree.insert("", "end", values=row)
                conn.close()
            except Exception as e:
                self.log_message(f"[!] Assignment DB Read Error: {e}")

        def on_tree_select(event):
            selected = tree.selection()
            if selected:
                values = tree.item(selected[0], 'values')
                selected_key.set(values[0])
                title_entry.delete(0, 'end'); title_entry.insert(0, values[1])
                # [PATCH:score-format] Intentionally UNformatted: this field is editable and round-trips to storage via float().
                score_entry.delete(0, 'end'); score_entry.insert(0, values[2])

        def update_assignment():
            a_key = selected_key.get()
            if not a_key: return
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE Assignments
                    SET Display_Title=?, Max_Score=?
                    WHERE Assignment_Key=?
                ''', (title_entry.get().strip(), float(score_entry.get().strip()), a_key))
                conn.commit()
                conn.close()
                load_assignment_data()
                self.log_message(f"-> Updated Assignment: {title_entry.get()}")
            except Exception as e:
                self.log_message(f"[!] Assignment Update Error: {e}")

        def delete_assignment():
            a_key = selected_key.get()
            if not a_key: return
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                # Cascading delete automatically wipes associated grades from Grades table
                cursor.execute("DELETE FROM Assignments WHERE Assignment_Key=?", (a_key,))
                conn.commit()
                conn.close()
                
                load_assignment_data()
                title_entry.delete(0, 'end')
                score_entry.delete(0, 'end')
                selected_key.set("")
                self.log_message(f"-> Safely purged Assignment and all associated grades.")
            except Exception as e:
                self.log_message(f"[!] Assignment Delete Error: {e}")

        tree.bind('<<TreeviewSelect>>', on_tree_select)

        ctk.CTkButton(btn_frame, text="UPDATE ASSIGNMENT", fg_color="#d35400", hover_color="#e67e22", command=update_assignment).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="DELETE ASSIGNMENT", fg_color="#c0392b", hover_color="#e74c3c", command=delete_assignment).pack(side="left", padx=10)

        load_assignment_data()
        self._make_text_copyable(assn_win)

    def open_prompt_editor(self):
        """Launches the GRADING INSTRUCTIONS editor (the teacher's prompt)."""
        editor_win = ctk.CTkToplevel(self.root)
        editor_win.title("GRADING INSTRUCTIONS")
        self._clamp_toplevel(editor_win, 900, 700)
        editor_win.transient(self.root) 
        
        # Grid Configuration (Weight shifted to row 2)
        editor_win.grid_rowconfigure(2, weight=1)
        editor_win.grid_columnconfigure(0, weight=1)

        # Header
        header_label = ctk.CTkLabel(editor_win, text="GRADING INSTRUCTIONS", font=self.header_font)
        header_label.grid(row=0, column=0, pady=(20, 10))

        # Row 1 is intentionally empty ([PATCH:no-temperature] the sampling
        # control that lived there was removed); an empty grid row has zero
        # height, so the editor sits directly under the header.

        # The Text Editor Area (Shifted to row 2)
        self.prompt_textbox = ctk.CTkTextbox(
            editor_win, 
            font=ctk.CTkFont(family="Ubuntu", size=14),
            wrap="word",
            corner_radius=10
        )
        self.prompt_textbox.grid(row=2, column=0, sticky="nsew", padx=20, pady=10)

        # SURGICAL INSERTION: Native Right-Click Menu (Linux Robust Version)
        def custom_paste():
            try:
                # Forcefully grab the main OS clipboard contents
                clipboard_text = editor_win.clipboard_get()
                
                # Purge any highlighted text before pasting
                if self.prompt_textbox.tag_ranges("sel"):
                    self.prompt_textbox.delete("sel.first", "sel.last")
                    
                # Insert clipboard at the current cursor position
                self.prompt_textbox.insert("insert", clipboard_text)
            except tk.TclError:
                pass # Fails gracefully if the clipboard is completely empty

        editor_menu = tk.Menu(self.prompt_textbox, tearoff=False, bg="#2b2b2b", fg="white")
        editor_menu.add_command(label="Copy", command=lambda: self.prompt_textbox.event_generate("<<Copy>>"))
        editor_menu.add_command(label="Paste", command=custom_paste)
        editor_menu.add_command(label="Select All", command=lambda: self.prompt_textbox.tag_add("sel", "1.0", "end"))
        
        self.prompt_textbox.bind("<Button-3>", lambda event: editor_menu.tk_popup(event.x_root, event.y_root))

        # [PATCH:no-default-prompt] Load the saved instructions from the DB.
        # Empty on a fresh install; a read error leaves the box empty (it is
        # logged) so error text can never be saved back as a prompt.
        self.prompt_textbox.insert("0.0", self.get_grading_instructions())

        # Save Button
        save_btn = ctk.CTkButton(
            editor_win, 
            text="SAVE PROMPT", 
            font=self.header_font,
            height=50,
            fg_color="#333333", 
            hover_color="#555555",
            command=lambda: self.save_prompt(editor_win)
        )
        save_btn.grid(row=3, column=0, pady=(10, 20), padx=20, sticky="ew")
        self._make_text_copyable(editor_win)

    def save_prompt(self, window):
        """Writes the textbox contents to the Settings DB ('grading_instructions')."""
        new_prompt_text = self.prompt_textbox.get("0.0", "end").strip()

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('grading_instructions', ?)
            ''', (new_prompt_text,))
            conn.commit()
            conn.close()

            self.log_message(f"-> Grading instructions saved ({len(new_prompt_text)} characters).")
            if not self.is_running:
                self.update_indicator("idle")   # clears a NO GRADING INSTRUCTIONS banner
            window.destroy()
        except Exception as e:
            self.log_message(f"[!] Database Write Error (Grading Instructions): {e}")

    def open_gradebook(self):
        """Launches the Visual SIS interface and populates it from SQLite."""
        gb_win = ctk.CTkToplevel(self.root)
        gb_win.title("MASTER GRADEBOOK")
        self._clamp_toplevel(gb_win, 1000, 600)
        gb_win.transient(self.root) # Keeps window on top
        
        ctrl_frame = ctk.CTkFrame(gb_win, fg_color="transparent")
        ctrl_frame.pack(fill="x", padx=20, pady=10)
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute("SELECT Assignment_Key, Display_Title, Max_Score FROM Assignments")
            assignments = cursor.fetchall()
            
            cursor.execute("SELECT Bartonian_ID, Last_Name, First_Name FROM Roster ORDER BY Last_Name, First_Name")
            students = cursor.fetchall()
            
            cursor.execute("SELECT Bartonian_ID, Assignment_Key, Score_Earned FROM Grades")
            grades = cursor.fetchall()
            conn.close()
        except Exception as e:
            self.log_message(f"[!] Gradebook DB Error: {e}")
            return
            
        # Structure grades into a dictionary: grades_dict[bartonian_id][assignment_key] = score
        grades_dict = {}
        for b_id, a_key, score in grades:
            if b_id not in grades_dict: grades_dict[b_id] = {}
            grades_dict[b_id][a_key] = score
            
        # Build the Treeview Spreadsheet
        # [PATCH:cosmetics] WO-COS1 T13: the tree lives in its own frame with
        # vertical AND horizontal scrollbars, so a semester with many
        # assignments (or many students) stays reachable.
        tree_frame = ctk.CTkFrame(gb_win, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        columns = ["b_id", "last", "first"] + [a[0] for a in assignments]
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings")

        # Style the Treeview for a dark theme
        style = self._style_dark_treeview()
        for _sbs in ("Ledger.Vertical.TScrollbar", "Ledger.Horizontal.TScrollbar"):
            style.configure(_sbs, background="#333333", troughcolor="#1a1a1a",
                            bordercolor="#1a1a1a", arrowcolor="#bbbbbb")
            style.map(_sbs, background=[('active', '#555555')])

        custom_label = self.get_custom_id_label()
        tree.heading("b_id", text=custom_label)
        tree.column("b_id", width=120, anchor="center")
        tree.heading("last", text="Last Name")
        tree.column("last", width=150)
        tree.heading("first", text="First Name")
        tree.column("first", width=150)
        
        assignments_map = {"Select Assignment...": None}
        for a_key, title, max_val in assignments:
            tree.heading(a_key, text=f"{title} ({format_score(max_val)})")  # [PATCH:score-format]
            # stretch=False keeps each assignment column readable at 150 px;
            # extra columns scroll horizontally instead of squeezing.
            tree.column(a_key, width=150, anchor="center", stretch=False)
            assignments_map[title] = a_key
            
        for stu in students:
            b_id = stu[0]
            row_data = [stu[0], stu[1], stu[2]]
            for a in assignments:
                a_key = a[0]
                score = grades_dict.get(b_id, {}).get(a_key, "-")
                row_data.append(format_score(score))  # [PATCH:score-format] display only
            tree.insert("", "end", values=row_data)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview,
                            style="Ledger.Vertical.TScrollbar")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview,
                            style="Ledger.Horizontal.TScrollbar")
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # --- SIS Export Controls ---
        export_label = ctk.CTkLabel(ctrl_frame, text="SIS Export:", font=self.main_font)
        export_label.pack(side="left", padx=(0, 10))
        
        selected_assignment = ctk.StringVar(value="Select Assignment...")

        def toggle_export_btn(choice):
            if choice == "Select Assignment...":
                export_btn.configure(state="disabled", fg_color="#333333")
            else:
                export_btn.configure(state="normal", fg_color="#1f538d", hover_color="#14375e")

        dropdown = ctk.CTkOptionMenu(ctrl_frame, variable=selected_assignment, values=list(assignments_map.keys()), width=200, command=toggle_export_btn)
        dropdown.pack(side="left", padx=(0, 10))
        
        export_btn = ctk.CTkButton(ctrl_frame, text="GENERATE CSV", state="disabled", fg_color="#333333", 
                                   command=lambda: self.export_to_sis(selected_assignment.get(), assignments_map))
        export_btn.pack(side="left", padx=(0, 10))
        self._make_text_copyable(gb_win)
        
    def export_to_sis(self, selected_title, assignments_map):
        if selected_title == "Select Assignment...":
            self.log_message("[!] Please select an assignment to export.")
            return

        a_key = assignments_map[selected_title]

        # --- THE INTERCEPTION MODAL ---
        warn_win = ctk.CTkToplevel(self.root)
        warn_win.title("EDUCATOR VERIFICATION REQUIRED")
        warn_win.geometry("850x400") # Widened to ensure the first sentence fits on one line
        warn_win.transient(self.root)

        legal_text = (
            "THE LEDGER IS AN AUTOMATED EVALUATION SYSTEM POWERED BY ARTIFICIAL INTELLIGENCE (AI).\n\n"
            "AI MODELS CAN MAKE MISTAKES. THIS SYSTEM IS DESIGNED TO ASSIST, NOT REPLACE, CERTIFIED "
            "EDUCATORS. THE VERACITY AND COMPLETENESS OF SCORES PRODUCED BY THE LEDGER MUST BE "
            "INDEPENDENTLY EVALUATED BY THE LICENSED TEACHER OF RECORD."
        )

        warn_label = ctk.CTkLabel(
            warn_win, 
            text=legal_text, 
            font=ctk.CTkFont(family="Ubuntu", size=15, weight="bold"),
            text_color="#c0392b",
            wraplength=780 # Increased to prevent "(AI)" from becoming an orphan word
        )
        warn_label.pack(pady=(40, 20))

        # --- THE RESTORED EXPORT LOGIC ---
        def execute_export():
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT Grades.Bartonian_ID, Grades.Score_Earned, Roster.Last_Name, Roster.First_Name 
                    FROM Grades 
                    JOIN Roster ON Grades.Bartonian_ID = Roster.Bartonian_ID 
                    WHERE Grades.Assignment_Key = ?
                    ORDER BY Roster.Last_Name, Roster.First_Name
                ''', (a_key,))
                export_data = cursor.fetchall()
                conn.close()
                
                if not export_data:
                    self.log_message(f"[!] No grades found in database for {selected_title}")
                    warn_win.destroy()
                    return
                    
                export_path = os.path.join(SIS_EXPORTS_DIR, f"Grade_Export_{a_key}.csv")
                with open(export_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([self.get_custom_id_label(), "Score", "Last Name", "First Name"])
                    for row in export_data:
                        writer.writerow([row[0], format_score(row[1]), row[2], row[3]])  # [PATCH:score-format] export text only; DB untouched
                            
                self.log_message(f"-> Local Grade Export isolated and saved: {export_path}")
            except Exception as e:
                self.log_message(f"[!] Export Error: {e}")
            finally:
                # Always release the window focus, even if the export fails
                warn_win.grab_release()
                warn_win.destroy()

        # --- LIABILITY CHECKBOX MECHANISM ---
        check_var = ctk.StringVar(value="off")
        
        def toggle_export_btn():
            if check_var.get() == "on":
                # Unlocks the button and turns it the system's safety green
                confirm_btn.configure(state="normal", fg_color="#1e8449", hover_color="#27ae60")
            else:
                # Locks the button and grays it out
                confirm_btn.configure(state="disabled", fg_color="#333333")

        liability_cb = ctk.CTkCheckBox(
            warn_win,
            text="I UNDERSTAND THE LIMITATIONS OF THIS AI SYSTEM. I HAVE INDEPENDENTLY VERIFIED THE ACCURACY OF THESE SCORES AND ASSUME FULL LIABILITY FOR EXPORTING THEM.",
            variable=check_var, onvalue="on", offvalue="off",
            font=ctk.CTkFont(family="Ubuntu", size=12, weight="bold"),
            text_color="white",
            command=toggle_export_btn
        )
        # [PATCH:cosmetics] WO-COS1 T12: dynamic wrap instead of hand-placed \n breaks.
        try:
            liability_cb._text_label.configure(wraplength=760, justify="left")
        except Exception:
            pass
        liability_cb.pack(pady=(10, 25))

        confirm_btn = ctk.CTkButton(
            warn_win, 
            text="ACCEPT & EXPORT", 
            font=self.header_font, 
            height=50, 
            fg_color="#333333", # Starts disabled
            state="disabled",
            command=execute_export
        )
        confirm_btn.pack()
        
        self._make_text_copyable(warn_win)
        warn_win.grab_set() # Forces focus ONLY AFTER everything is fully drawn

    def setup_logging(self):
        os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
        logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.log_message("EXPERIMENTAL EVALUATION SOFTWARE. Provided AS IS, with no warranty. "
                         "Use only with your institution's approval. Every score must be reviewed "
                         "by a licensed educator. See DISCLAIMER.md.")  # [PATCH:evaluation-wording]
        self.log_message("System initialized. Awaiting Master PDF and Max Score.")

    def log_message(self, message):
        logging.info(message)
        self.root.after(0, self._append_to_gui, message)
        
    def _append_to_gui(self, message):
        # [PATCH:cosmetics] WO-COS1 T7: pick the tag from the MESSAGE prefix
        # (the timestamp is prepended after the decision, so it shares the
        # line's color). No tag = the textbox's green default.
        msg = str(message)
        if msg.startswith("[!]"):
            tags = ("ledger_err",)
        elif msg.startswith("==="):
            tags = ("ledger_hdr",)
        else:
            tags = ()
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n", tags)
        self.log_text.see("end")

    # ==========================================================================
    # UNIVERSAL MOUSE-ONLY COPY / PASTE
    # ------------------------------------------------------------------------
    # One mechanism for every window: a right-click menu on every text-bearing
    # widget -- editable entries, textboxes (including read-only/"disabled"
    # ones), and hardcoded message labels. No Ctrl+C / Ctrl+V needed. Call
    # self._make_text_copyable(window) ONCE at the very end of a window builder,
    # AFTER every widget is created and any .configure(state="disabled") has run
    # (so read-only textboxes are seen as read-only). The walk is fully
    # defensive: a failure on any single widget never blocks the window.
    # ==========================================================================
    def _add_entry_menu(self, widget):
        """Right-click Cut/Copy/Paste/Select-All for a CTkEntry (mouse only)."""
        tkw = getattr(widget, "_entry", widget)
        menu = tk.Menu(tkw, tearoff=False, bg="#2b2b2b", fg="white")
        menu.add_command(label="Cut", command=lambda: tkw.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: tkw.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: tkw.event_generate("<<Paste>>"))
        menu.add_separator()

        def _select_all():
            try:
                tkw.select_range(0, "end")
                tkw.icursor("end")
            except tk.TclError:
                pass

        menu.add_command(label="Select All", command=_select_all)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tkw.bind("<Button-3>", _popup)

    def _add_textbox_menu(self, widget, editable=True):
        """Right-click menu for a CTkTextbox (mouse only). Works even when the
        textbox is read-only ('disabled'): if nothing is selected, Copy grabs
        the entire contents, so baked-in disclaimer text is always copyable."""
        tkw = getattr(widget, "_textbox", widget)
        win = widget.winfo_toplevel()
        menu = tk.Menu(tkw, tearoff=False, bg="#2b2b2b", fg="white")

        def _copy():
            try:
                text = tkw.get("sel.first", "sel.last")
            except tk.TclError:
                text = tkw.get("1.0", "end-1c")  # nothing selected -> copy it all
            if text:
                win.clipboard_clear()
                win.clipboard_append(text)

        menu.add_command(label="Copy", command=_copy)

        if editable:
            def _paste():
                try:
                    clip = win.clipboard_get()
                except tk.TclError:
                    return
                try:
                    if tkw.tag_ranges("sel"):
                        tkw.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                tkw.insert("insert", clip)

            def _cut():
                _copy()
                try:
                    if tkw.tag_ranges("sel"):
                        tkw.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass

            menu.add_command(label="Cut", command=_cut)
            menu.add_command(label="Paste", command=_paste)

        menu.add_separator()

        def _select_all():
            try:
                tkw.tag_add("sel", "1.0", "end-1c")
            except tk.TclError:
                pass

        menu.add_command(label="Select All", command=_select_all)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tkw.bind("<Button-3>", _popup)

    def _add_label_copy(self, widget):
        """Right-click 'Copy text' for a hardcoded message label (mouse only).
        Labels can't be highlighted, so this copies the label's full text."""
        tkw = getattr(widget, "_label", widget)
        win = widget.winfo_toplevel()
        menu = tk.Menu(tkw, tearoff=False, bg="#2b2b2b", fg="white")

        def _copy():
            try:
                text = widget.cget("text")
            except Exception:
                text = ""
            if text:
                win.clipboard_clear()
                win.clipboard_append(text)

        menu.add_command(label="Copy text", command=_copy)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tkw.bind("<Button-3>", _popup)

    def _make_text_copyable(self, container):
        """Recursively give every entry, textbox, and message label inside a
        window a mouse-only right-click copy/paste menu. Call once, last, in a
        window builder. Buttons, checkboxes, and other controls are skipped."""
        try:
            if isinstance(container, ctk.CTkEntry):
                self._add_entry_menu(container)
            elif isinstance(container, ctk.CTkTextbox):
                try:
                    editable = str(container.cget("state")) != "disabled"
                except Exception:
                    editable = True
                self._add_textbox_menu(container, editable=editable)
            elif isinstance(container, ctk.CTkLabel):
                try:
                    txt = container.cget("text")
                except Exception:
                    txt = ""
                if txt and txt.strip():
                    self._add_label_copy(container)
        except Exception:
            pass

        try:
            for child in container.winfo_children():
                self._make_text_copyable(child)
        except Exception:
            pass

    def _clamp_toplevel(self, window, width, height):
        """[PATCH:cosmetics] WO-COS1 T11: size a Toplevel to the requested
        geometry, clamped to the physical screen the same way the dashboard
        and wizard already are (WIZARD_SCREEN_ALLOWANCE below the screen
        height, 40 px inside its width), so popups never clip on the
        1360x768 target display."""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            window.geometry(f"{min(width, sw - 40)}x{min(height, sh - WIZARD_SCREEN_ALLOWANCE)}")
        except Exception:
            window.geometry(f"{width}x{height}")

    def _style_dark_treeview(self):
        """[PATCH:cosmetics] WO-COS1 T14: the ONE dark ttk Treeview style,
        formerly repeated verbatim in the roster, gradebook, assignment and
        user manager windows. Pixel-identical to those inline blocks."""
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", rowheight=25)
        style.configure("Treeview.Heading", background="#1f538d", foreground="white", font=('Ubuntu', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#1e8449')])
        return style

    def _raise_daemon_error(self, response):
        """[PATCH:error-passthrough] The broker daemon wraps EVERY upstream
        failure as HTTP 500 + {"status":"error","message": <real reason>}.
        raise_for_status() raised on the status code BEFORE reading that body,
        so the GUI's classifier only ever saw the literal string '500':
        a real upstream 429 got the FAST 3s transport retries instead of the
        long capacity backoff, and the on-screen log could never show the true
        cause. This reads the body FIRST and raises the daemon's actual
        message, so _translate_api_error and the is_capacity check finally see
        what really happened upstream (Anthropic or Google alike)."""
        if response.status_code < 400:
            return
        try:
            detail = (response.json() or {}).get("message", "")
        except Exception:
            detail = (response.text or "")[:300]
        # [PATCH:no-500-mask] The daemon wraps EVERY upstream failure as its
        # own HTTP 500, so prefixing the detail with that transport code meant
        # the classifier's "500" branch matched first and the TRUE upstream
        # cause (413 payload cap, 400 bad request, 404 model, auth) was never
        # classified or displayed. Raise the detail alone -- it already carries
        # the real upstream code. Keep our code only when there is no detail.
        raise Exception(detail if detail
                        else f"Daemon HTTP {response.status_code} (no detail)")

    def _translate_api_error(self, raw_error):
        """Translates technical Vertex AI infrastructure messages into concise, non-alarming prose."""
        error_str = str(raw_error).lower()
        
        # [PATCH:client-timeout-coherence] A LOCAL read timeout (the GUI
        # hanging up on 127.0.0.1:5000) carries 'port=5000', whose substring
        # "500" false-matched the 500 branch below and printed a provider
        # outage that never happened (2026-07-08, ID 105). Classify it FIRST.
        if "read timed out" in error_str or "read timeout" in error_str:
            return (
                "The local grading daemon did not reply within the client's wait "
                "window; the grade may still be completing upstream. If this "
                "recurs after the timeout fix, check ledger-daemon.service. "
                "(Local - Client Timeout)"
            )

        # [PATCH:wizard-devapi] The Gemini Developer API answers a bad AI Studio
        # key with HTTP 400 + "API key not valid" (reason API_KEY_INVALID). The
        # generic 400 branch below would call that a document-format problem
        # and retry. Classify it FIRST: it is permanent (see is_permanent in
        # evaluate_grid_with_silo_2) and the real reason belongs on screen.
        if "api key not valid" in error_str or "api_key_invalid" in error_str:
            return (
                "The Google AI Studio key on this machine was rejected by Google "
                "(API_KEY_INVALID). Re-check the key in Google AI Studio, then "
                "ADVANCED -> RESET AUTHORIZATION and re-initialize with the correct key. "
                "(Code 400 - Key Rejected)"
            )

        if "resource exhausted" in error_str or "429" in error_str:
            return (
                "High cloud traffic detected. Pacing output and resuming shortly. "
                "(Code 429 - Queue Full)"
            )
            
        elif "503" in error_str or "service unavailable" in error_str:
            return (
                "Temporary cloud congestion. Pausing to allow servers to catch up. "
                "(Code 503 - Server Pacing)"
            )
            
        elif ("413" in error_str or "request_too_large" in error_str
              or "prompt is too long" in error_str):
            return (
                "Request exceeds the provider's payload/context ceiling -- retrying "
                "cannot succeed. Reduce pages per call or image resolution. "
                "(Code 413 - Payload Over Cap)"
            )

        elif "500" in error_str or "internal server error" in error_str:
            return (
                "The AI provider had a brief internal error. Retrying "
                "automatically. (Code 500 - Provider Error)"
            )
            
        elif "403" in error_str or "permission denied" in error_str:
            return (
                "The provider rejected the credentials. Check that the API key is valid "
                "and billing is enabled on its project. (Code 403 - Key Rejected)"
            )
            
        elif "400" in error_str or "bad request" in error_str:
            return (
                "The provider rejected the request's format. "
                "(Code 400 - Request Rejected)"
            )
            
        else:
            return (
                "Minor network variation detected. Re-attempting connection. "
                "(System - Network Pacing Engaged)"
            )

    def verify_directories(self):
        dirs = [LEDGER_DIR, ENGINE_DIR, DIAGNOSTIC_DIR, MASTER_DIR, COURSES_DIR, UNEVALUATED_DIR, ORIGINAL_SCANS_DIR, TEMP_DIR, SIS_EXPORTS_DIR, FORMATTED_DIR]  # [PATCH:formatter]
        for period in PERIODS:
            dirs.extend([
                os.path.join(COURSES_DIR, period), 
                os.path.join(COURSES_DIR, period, "Evaluated Assignments")
            ])
        for d in dirs:
            if not os.path.exists(d): 
                os.makedirs(d)
    
    def clean_temp_directory(self):
        """Sweeps the Temp Ledger Files directory clean. [cite: 335]"""
        if os.path.exists(TEMP_DIR):
            for filename in os.listdir(TEMP_DIR):
                file_path = os.path.join(TEMP_DIR, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    self.log_message(f"[!] Temp Cleanup Failed: {file_path}. Reason: {e}")
        else:
            os.makedirs(TEMP_DIR)

    def update_indicator(self, state):
        if state == "running":
            self.status_label.configure(text="SYSTEM: RUNNING", text_color="#e74c3c")
        else:
            self.status_label.configure(text="SYSTEM: IDLE", text_color="#1e8449")

    def start_ledger(self):
        # [PATCH:formatter] RUN cannot start while a format job is running
        # (and the formatter window refuses to Send while RUN is evaluating).
        if getattr(self, "formatter_busy", False):
            self.log_message("[!] A formatting job is running. Wait for it "
                             "to finish before starting an evaluation.")
            return

        # [PATCH:no-default-prompt] Empty (or whitespace-only) instructions:
        # refuse before anything starts -- no calibration window, no batch,
        # nothing sent to the daemon. The system never leaves idle.
        if not self.is_running and not self.get_grading_instructions().strip():
            self.status_label.configure(text=NO_INSTRUCTIONS_BANNER, text_color="#c0392b")
            self.log_message(f"[!] {NO_INSTRUCTIONS_LOG}")
            return

        # [PATCH:batch] ONE pending job at a time: while a submitted batch job
        # is waiting to be collected, RUN refuses in BOTH modes -- the pending
        # job's scans are still in the inbox, and a second run would score
        # them twice. The system stays idle.
        if os.path.exists(BATCH_JOB_FILE):
            self.status_label.configure(text="BATCH JOB PENDING", text_color="#c0392b")
            self.log_message("[!] A submitted batch job is still pending. RUN is "
                             "unavailable until it is resumed and collected (or "
                             "cancelled) -- restart The Ledger to get the "
                             "RESUME / CANCEL BATCH choice.")
            return

        # [PATCH:batch] Read the scoring choice ONCE per run, like the mode.
        self.scoring_mode = self.get_scoring_mode()
        if self.scoring_mode == "batch":
            cap_ok, cap_note = self._batch_capability_check(self.get_silo2_model())
            if not cap_ok:
                # Refuse loudly; never silently fall back to live.
                self.status_label.configure(text="BATCH UNAVAILABLE", text_color="#c0392b")
                self.log_message(f"[!] BATCH scoring is unavailable: {cap_note} "
                                 "The system stays idle; switch SCORING to LIVE "
                                 "to score now.")
                return
            if cap_note:
                self.log_message(f"-> Batch note: {cap_note}")

        max_score_str = self.max_score_entry.get().strip()
        if not max_score_str.isdigit() or int(max_score_str) <= 0:
            self.log_message("[!] Error: Enter a valid Max Score.")
            return
            
        if not self.master_pdf_path:
            self.log_message("[!] Error: You must load a Master PDF before running Ledger.")
            return

        self.global_max_score = int(max_score_str)

        # [PATCH:essay-mode] Read the pipeline mode ONCE per run so a mid-batch
        # tap on the selector can never change strategy under a running job.
        self.grading_mode = self.get_grading_mode()
        self.log_message(f"Pipeline mode for this run: "
                         f"{'ESSAY' if self.grading_mode == 'essay' else 'WORKSHEET'}.")

        # [PATCH:count-check] learned omission patterns live for ONE run;
        # every RUN starts with a clean slate.
        self._count_check_learned = {}

        # [PATCH:box-map] answer-box notices and page shift/scale estimates
        # live for ONE run too.
        self._box_map_notices = []
        self._box_map_page_stats = []

        # [PATCH:footer-reader] WO-COS2 T3: footer counters live for ONE run.
        self._footers_attempted = 0
        self._footers_read = 0

        # [PATCH:auto-orient] WO-COS2 T4: orientation counters, per run.
        self._pages_turned_upright = 0
        self._sideways_pages = 0

        # [PATCH:pass1-pacing] WO-COS2 T5: pacing flag + re-read notices.
        self._reset_pass1_run_state()

        # [PATCH:essay-tag-quiet] The corner-tag OCR exists ONLY to group
        # pages into essays for CHUNKED packing (LEDGER_ESSAY_MAX_PAGES_PER_CALL
        # > 0). In single-call mode (budget 0, the default) every page of a
        # student travels in ONE call and the model groups items from the
        # printed tags itself -- the local OCR result is never consulted.
        # Resolve that ONCE per run: in single-call mode the OCR is skipped
        # entirely, so the teacher never sees per-page "Essay Tag: unreadable"
        # noise about a value the pipeline does not use.
        try:
            self.essay_tag_ocr_needed = int(os.environ.get(
                "LEDGER_ESSAY_MAX_PAGES_PER_CALL", "0")) > 0
        except (TypeError, ValueError):
            self.essay_tag_ocr_needed = False
        if self.grading_mode == "essay" and not self.essay_tag_ocr_needed:
            self.log_message("Essay grouping: single-call mode -- each student's "
                             "pages travel together in one call (corner-tag OCR skipped).")

        if not self.is_running:
            self.is_running = True
            self.run_btn.configure(state="disabled")
            self.max_score_entry.configure(state="disabled")
            self.mode_selector.configure(state="disabled")
            self.scoring_selector.configure(state="disabled")   # [PATCH:batch]
            self.master_btn.configure(state="disabled")
            self.update_indicator("running")
            
            # INTERCEPT: Open Calibration Studio for visual check instead of instant run
            # [PATCH:calib-session-skip] ...unless zones were SAVED in this grader
            # session (either studio); a restart clears that. No scan -> studio,
            # which shows SCAN REQUIRED and resets the run.
            scan_files = [f for f in os.listdir(UNEVALUATED_DIR) if f.lower().endswith('.pdf')]
            if self.calibrated_this_session and scan_files:
                target_pdf = os.path.join(UNEVALUATED_DIR, scan_files[0])
                self.log_message("Calibration already saved this session. Starting Ledger engine...")
                threading.Thread(target=self.run_batch_extraction, args=(target_pdf,), daemon=True).start()
            else:
                self.open_calibration_tool(is_intercept=True)

    def shut_down(self):
        self.is_running = False
        self.update_indicator("idle")
        self.run_btn.configure(state="normal")
        self.max_score_entry.configure(state="normal")
        self.mode_selector.configure(state="normal")   # [PATCH:essay-mode]
        self.scoring_selector.configure(state="normal")   # [PATCH:batch]
        self.master_btn.configure(state="normal")
        self.clean_temp_directory()
        self.log_message("Stop requested. Temp folder scrubbed. System is Idle.")
        # [PATCH:batch] SHUT DOWN during a pending job stops the polling and
        # nothing else: the job record lives OUTSIDE the temp folder and the
        # provider keeps working. Nothing paid is lost.
        if os.path.exists(BATCH_JOB_FILE):
            self.batch_progress_label.configure(text="")
            self.log_message("A submitted batch job is still pending. It was NOT "
                             "cancelled; reopen The Ledger and choose RESUME to "
                             "collect it.")

    def image_to_base64_png(self, image_path):
        buffer = io.BytesIO()
        with Image.open(image_path) as img:
            if img.mode != 'RGBA' and img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    # ==========================================
    # SURGERY AREA 1: VERTEX CONTEXT CACHING
    # ==========================================
    def initialize_silo_2_cache(self, master_pdf_path):
        self.log_message("Rendering Master PDF into images for context cache...")
        master_parts_b64 = []
        # [PATCH:silo2-per-chunk-context] keep the rendered master page FILES
        # (and a per-batch b64 cache) so boxed chunks can carry their own pages.
        self.s2ctx_master_files = []
        self._s2ctx_b64_cache = {}
        try:
            doc = fitz.open(master_pdf_path)
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16))
                temp_master_path = os.path.join(TEMP_DIR, f"master_page_cache_{i}.png")
                pix.save(temp_master_path)
                self.s2ctx_master_files.append(temp_master_path)
                master_parts_b64.append(self.image_to_base64_png(temp_master_path))
            doc.close()
            # [PATCH:run-summary] WO-COS2 T6: the master's page count in its
            # OWN attribute, captured when the master is rendered --
            # s2ctx_master_files is emptied when per-chunk context is off.
            self._master_page_count = len(self.s2ctx_master_files)
        except Exception as e:
            self.log_message(f"[!] Failed to render Master PDF images: {e}")
            return False

        # [PATCH:box-map] Master box map: run the SAME box finder over each
        # rendered master page so Pass 1 can notice when a scan box the
        # finder missed exists on the master, and recover it from the
        # master's layout. Stored in its OWN attribute (with the file paths)
        # because s2ctx_master_files is emptied when per-chunk context is
        # off. Local OpenCV only; no AI call. LEDGER_BOX_MAP=0 skips the map
        # entirely, which also disables all matching and recovery downstream.
        self._box_map = {}
        self._box_map_page_stats = []
        if (os.environ.get("LEDGER_BOX_MAP", "1") != "0"
                and getattr(self, "grading_mode", "boxed") != "essay"):
            try:
                for i, mp in enumerate(list(self.s2ctx_master_files)):
                    mimg = cv2.imread(mp)
                    rects = self._box_map_find_rects(mimg)
                    self._box_map[i] = {"rects": rects,
                                        "size": (mimg.shape[1], mimg.shape[0]),
                                        "file": mp}
                _n_boxes = sum(len(v["rects"]) for v in self._box_map.values())
                self.log_message(f"-> Master box map: {_n_boxes} answer box(es) "
                                 f"across {len(self._box_map)} master page(s).")
            except Exception as e:
                self._box_map = {}
                self.log_message(f"[!] Master box map could not be built ({e}); "
                                 "box recovery is off for this run.")

        self.log_message("Uploading Context Cache via Root Daemon...")
        try:
            live_prompt = self.get_grading_instructions()
            if not live_prompt.strip():
                # Cleared after RUN was pressed (e.g. during calibration).
                self.log_message(f"[!] {NO_INSTRUCTIONS_LOG}")
                return False
            self.log_message(f"-> Grading instructions: {len(live_prompt)} characters.")

            payload = {
                "model_name": self.get_silo2_model(),
                "system_instruction": live_prompt,
                "images_b64": master_parts_b64
            }
            
            time.sleep(10) # Network buffer before uploading massive cache payload
            response = requests.post(DAEMON_BASE + "/api/cache/build", json=payload, timeout=120)
            self._raise_daemon_error(response)

            self.cached_silo2_content = True # Just a local flag now
            # [PATCH:cosmetics] WO-COS1 T2: the success line names what the
            # daemon actually did ({"text": <route>}), instead of always
            # claiming a 12-hour Google cache. GUI wording only; no payload
            # or daemon change.
            try:
                _cache_route = (response.json() or {}).get("text", "")
            except Exception:
                _cache_route = ""
            if _cache_route == "anthropic-local":
                self.log_message("-> Evaluation context staged (Anthropic prompt cache — refreshed on each call).")
            elif _cache_route == "google-inline":
                # [PATCH:cosmetics] WO-BM3 T4: the daemon returns the SAME
                # "google-inline" sentinel for two causes -- a master below
                # the Google cache minimum, or a model (e.g. Gemini 4
                # Argon) with no explicit cache at all. Name both
                # truthfully instead of always claiming the first.
                self.log_message("-> Evaluation context staged inline (no Google server-side cache for this model or master — the daemon log names the reason).")
            else:
                self.log_message("-> Google context cache built — valid for 12 hours.")
            # [PATCH:silo2-per-chunk-context] Dashboard status + daemon capability
            # gate. An older daemon would silently IGNORE master_images_b64 (grades
            # still correct, but full-weight) -- gate it OFF loudly here instead,
            # so an A/B arm can never be silently invalid.
            if os.environ.get("LEDGER_S2CTX_ENABLE", "1") != "1":
                self.log_message("-> Per-chunk master context: DISABLED (LEDGER_S2CTX_ENABLE=0) - every chunk carries the FULL master (A-arm).")
                self.s2ctx_master_files = []
            else:
                try:
                    _health = requests.get(DAEMON_BASE + "/", timeout=10).json()
                except Exception:
                    _health = {}
                if _health.get("per_chunk_context"):
                    self.log_message("-> Per-chunk master context: ACTIVE - each boxed chunk carries only its own master page(s) plus the standing include page(s); unreadable footers fall back to the FULL master.")
                else:
                    self.log_message("[!] Per-chunk master context: this daemon predates it (need v2.6) - every chunk carries the FULL master. Grades unaffected; weight reduction inactive.")
                    self.s2ctx_master_files = []
            return True
        except Exception as e:
            self.log_message(f"[!] Failed to build the evaluation context via the daemon: {e}")
            return False

    def delete_cache(self):
        try:
            requests.post(DAEMON_BASE + "/api/cache/delete", timeout=15)
            self.cached_silo2_content = False
            self.log_message("-> Evaluation context cleared.")
        except Exception as e:
            self.log_message(f"[!] Failed to delete cache via Daemon: {e}")
     
    # ==========================================
    # SURGERY AREA 2: OPENCV BOX EXTRACTION (GEOMETRY UPGRADE)
    # ==========================================
    def _box_map_find_rects(self, img):
        """[PATCH:box-map] The contour-finding half of extract_boxes_with_opencv,
        moved verbatim (same thresholds, filters and sort) so the SAME finder
        can also map the master's answer boxes. Takes a BGR image, returns the
        sorted list of (x, y, w, h). Any change here changes both sides."""
        # 1. Read image in grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 2. Adaptive Thresholding: Dynamically handles scanner shadows/gradients
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 51, 15
        )

        # 3. Morphological Close: Leverages your 0.04" line thickness to fuse
        # borders into unbroken solid shapes, making them mathematically perfect.
        kernel = np.ones((5,5), np.uint8)
        closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

        # 4. Find Contours
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # 5. The Geometry & Extent Filter
        valid_boxes = []
        for cnt in contours:
            area = cv2.contourArea(cnt)

            # Filter 1: Dust filter (No maximum size limit)
            if area > 10000:
                perimeter = cv2.arcLength(cnt, True)
                # Smoothed from 0.02 to 0.03 to ignore minor handwriting bumps
                approx = cv2.approxPolyDP(cnt, 0.03 * perimeter, True)

               # Filter 2: Allow up to 15 corners to accommodate handwriting bumps/tails.
                if 4 <= len(approx) <= 15:
                    x, y, w, h = cv2.boundingRect(approx)
                    rect_area = w * h

                    # Filter 3: The Height Check (The Paragraph Assassin)
                    # Rejects fused text strings and rogue equations (usually < 50px tall)
                    if h > 75:

                        # Filter 4: The Extent Check
                        # Lowered to 0.65 to allow students' handwriting to cross box boundaries.
                        extent = float(area) / rect_area
                        if extent > 0.65:
                            valid_boxes.append((x, y, w, h))

        # 6. Spatial Sorting Math (Top-to-Bottom, Left-to-Right)
        def get_sort_key(box):
            x, y, w, h = box
            return (y // 150, x)

        valid_boxes.sort(key=get_sort_key)
        return valid_boxes

    def extract_boxes_with_opencv(self, full_page_img_path, page_num):
        """Finds LibreOffice boxes using adaptive thresholding, strict geometry, and extent."""
        snippets = []
        # [PATCH:box-map] remember this page's rectangles and size so Pass 1
        # can match them against the master's box map; None = finder errored.
        self._box_map_last = None
        try:
            img = cv2.imread(full_page_img_path)
            valid_boxes = self._box_map_find_rects(img)
            self._box_map_last = {"rects": list(valid_boxes),
                                  "size": (img.shape[1], img.shape[0])}

            # 7. Crop and Save
            box_count = 0
            for x, y, w, h in valid_boxes:
                cropped = img[y:y+h, x:x+w]
                snippet_path = os.path.join(TEMP_DIR, f"snippet_p{page_num}_b{box_count}.png")
                cv2.imwrite(snippet_path, cropped)
                snippets.append(snippet_path)
                box_count += 1
            
            self.log_message(f"    -> OpenCV isolated and sorted {box_count} perfect rectangles on page {page_num}.")
            return snippets
            
        except Exception as e:
            self.log_message(f"[!] OpenCV Error on {full_page_img_path}: {e}")
            return []

    # ==========================================
    # [PATCH:box-map] WO-B1-4: MASTER BOX MAP, MATCH AND RECOVERY
    # A faint scanner line can fuse with a box border into one contour that
    # the extent filter rejects -- the box silently vanishes and the count
    # check cannot see it (it compares reply items with boxes SENT). The map
    # of the MASTER's boxes says how many boxes each page must have and
    # where; a missing one is re-cropped from the master's rectangle and
    # verified before use. Boxed mode only. LEDGER_BOX_MAP=0 turns all of
    # this off; matched-clean pages take the untouched byte-identical path.
    # ==========================================
    def _box_map_record_notice(self, line):
        """Log a teacher-facing notice now AND keep it for the end-of-run
        summary (repeated after the closing lines, LIVE and BATCH)."""
        self.log_message(f"  -> [BOX MAP] {line}")
        if not hasattr(self, "_box_map_notices"):
            self._box_map_notices = []
        self._box_map_notices.append(line)

    def _box_map_save_reject(self, crop, b_id, footer_num, box_no, detail):
        """[PATCH:box-rejects] WO-BM1 T5: keep a REJECTED recovery crop so
        the rejection can be diagnosed afterward (the Footer-Rejects
        pattern: emptied at the start of Pass 1, kept until the next run).
        LEDGER_BOX_DEBUG=0 disables the saving only; logging stays."""
        if os.environ.get("LEDGER_BOX_DEBUG", "1") == "0":
            return
        try:
            m = re.search(r"frame ([0-9.]+|n/a), tag ([0-9.]+|n/a)", detail)
            f_s = (m.group(1) if m else "na").replace("n/a", "na")
            t_s = (m.group(2) if m else "na").replace("n/a", "na")
            os.makedirs(BOX_REJECTS_DIR, exist_ok=True)
            dest = os.path.join(
                BOX_REJECTS_DIR,
                f"ID{b_id}-page{footer_num}-box{box_no}"
                f"-frame{f_s}-tag{t_s}.png")
            if crop is not None and getattr(crop, "size", 0):
                cv2.imwrite(dest, crop)
                self._box_rejects_saved = \
                    getattr(self, "_box_rejects_saved", 0) + 1
        except Exception as e:
            self.log_message(f"    -> [!] Could not save box reject crop: {e}")

    def _id_save_reject(self, temp_id_path, pdf_file, page_display, read_id):
        """[PATCH:box-rejects] WO-BM1 T5: keep the ID crop of a page
        discarded for an off-roster ID (after the re-read). The crop shows
        the handwritten ID number only -- no names. LEDGER_BOX_DEBUG=0
        disables the saving only."""
        if os.environ.get("LEDGER_BOX_DEBUG", "1") == "0":
            return
        try:
            os.makedirs(ID_REJECTS_DIR, exist_ok=True)
            dest = os.path.join(
                ID_REJECTS_DIR,
                f"{pdf_file}-page{page_display}-read{read_id}.png")
            if os.path.exists(temp_id_path):
                shutil.copy(temp_id_path, dest)
                self._id_rejects_saved = \
                    getattr(self, "_id_rejects_saved", 0) + 1
        except Exception as e:
            self.log_message(f"    -> [!] Could not save ID reject crop: {e}")

    @staticmethod
    def _box_map_median(vals):
        s = sorted(vals)
        n = len(s)
        if not n:
            return 0.0
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    def _box_map_verify(self, scan_crop_gray, master_crop_gray, margin=0):
        """Does this crop show the SAME printed structure as the master's box?
        Two tests, both blind to handwriting:
        1. FRAME: binarize both; ask what fraction of the master's printed
           border-band pixels are also dark in the (dilated) scan. Extra ink
           from handwriting can only ADD dark pixels, so writing across the
           border never lowers this score; a crop in the wrong place has its
           frame elsewhere and scores low.
        2. TAG: the only printed ink INSIDE a master box is its tag number.
           Template-match that tag region in the scan crop; a crop of the
           wrong spot (or the wrong box) does not contain this exact tag.
        Returns (ok, detail_string). Thresholds set from the WO-B1-4 tests.
        [PATCH:tag-recovery] WO-BM1 T3: the measurements now live in
        _box_map_scores; this wrapper applies the ORIGINAL two-test gate
        with the original +/-20px tag window, behavior-identically (it is
        the LEDGER_BOX_RECOVER_TAG=0 path and the no-tag fallback)."""
        fr, tag, _dx, _dy, err = self._box_map_scores(
            scan_crop_gray, master_crop_gray, margin=margin, win=20)
        if err:
            return False, err
        # Thresholds measured in tests/test_box_map.py: the right box
        # scores tag >= 0.89 even rotated+blurred; the NEIGHBOR box
        # (same frame, different printed tag) peaks at 0.62. 0.75
        # separates them with margin on both sides; frame 0.80 rejects
        # off-box crops (<= 0.07 on blank paper).
        ok = fr >= 0.80 and (tag is None or tag >= 0.75)
        detail = (f"frame {fr:.2f}, tag "
                  f"{'n/a' if tag is None else format(tag, '.2f')}")
        return ok, detail

    def _box_map_scores(self, scan_crop_gray, master_crop_gray, margin=0,
                        win=20):
        """[PATCH:tag-recovery] WO-BM1 T3: the frame/tag measurements behind
        _box_map_verify, with the tag template search window as a parameter
        and the tag's measured position offset returned. Returns
        (frame_recall, tag_score, tag_dx, tag_dy, err): tag_dx/tag_dy are
        the tag's measured offset from its expected spot, in master-crop
        pixels (the scan crop is resized to the master crop before
        matching); err is a reason string when nothing could be measured,
        else None. With win=20 the measurements are exactly the old ones."""
        try:
            mh, mw = master_crop_gray.shape[:2]
            if mh < 20 or mw < 20 or scan_crop_gray.size == 0:
                return None, None, None, None, "degenerate crop"
            scan = cv2.resize(scan_crop_gray, (mw, mh),
                              interpolation=cv2.INTER_AREA)
            _, mbin = cv2.threshold(master_crop_gray, 0, 255,
                                    cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            _, sbin = cv2.threshold(scan, 0, 255,
                                    cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            sfat = cv2.dilate(sbin, np.ones((11, 11), np.uint8))
            # both crops carry `margin` px of page around the box, so the
            # printed frame sits ~margin px inside the edge: the band must
            # reach past it (margin + border thickness + slack).
            band = int(margin) + max(16, int(round(0.08 * min(mw, mh))))
            band = min(band, (min(mw, mh) - 4) // 2)
            frame = np.zeros((mh, mw), dtype=bool)
            frame[:band, :] = True
            frame[-band:, :] = True
            frame[:, :band] = True
            frame[:, -band:] = True
            fm = (mbin > 0) & frame
            denom = int(fm.sum())
            if denom < 100:
                return None, None, None, None, "master crop shows no printed frame"
            frame_recall = float(((sfat > 0) & fm).sum()) / denom

            tag_score = None
            tag_dx = None
            tag_dy = None
            inner = mbin.copy()
            inner[frame] = 0
            ys, xs = np.nonzero(inner)
            if len(xs) >= 30:
                pad = 6
                tx0 = max(0, int(xs.min()) - pad)
                tx1 = min(mw, int(xs.max()) + pad)
                ty0 = max(0, int(ys.min()) - pad)
                ty1 = min(mh, int(ys.max()) + pad)
                tmpl = master_crop_gray[ty0:ty1, tx0:tx1]
                sx0 = max(0, tx0 - win)
                sx1 = min(mw, tx1 + win)
                sy0 = max(0, ty0 - win)
                sy1 = min(mh, ty1 + win)
                search = scan[sy0:sy1, sx0:sx1]
                if (search.shape[0] >= tmpl.shape[0]
                        and search.shape[1] >= tmpl.shape[1]
                        and tmpl.size > 0):
                    res = cv2.matchTemplate(search, tmpl,
                                            cv2.TM_CCOEFF_NORMED)
                    tag_score = float(res.max())
                    _, _, _, maxloc = cv2.minMaxLoc(res)
                    # measured tag position minus its expected position,
                    # in master-crop pixels
                    tag_dx = (sx0 + maxloc[0]) - tx0
                    tag_dy = (sy0 + maxloc[1]) - ty0
            return frame_recall, tag_score, tag_dx, tag_dy, None
        except Exception as e:
            return None, None, None, None, f"verification error: {e}"

    def _box_map_master_tag(self, mgray, rect):
        """[PATCH:tag-fit] WO-BM2 T2: the printed tag of a master box, as a
        template plus its PAGE position. The tag is the only printed ink
        inside the box (the same region _box_map_scores matches); it sits
        at the box's top-left corner. Returns (template, tag_x, tag_y) in
        master-page pixels, or None when the box has no tag ink."""
        try:
            mx, my, mw, mh = rect
            crop = mgray[my:my + mh, mx:mx + mw]
            if crop.shape[0] < 20 or crop.shape[1] < 20:
                return None
            _, mbin = cv2.threshold(crop, 0, 255,
                                    cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            band = max(16, int(round(0.08 * min(mw, mh))))
            band = min(band, (min(mw, mh) - 4) // 2)
            frame = np.zeros((mh, mw), dtype=bool)
            frame[:band, :] = True
            frame[-band:, :] = True
            frame[:, :band] = True
            frame[:, -band:] = True
            inner = mbin.copy()
            inner[frame] = 0
            ys, xs = np.nonzero(inner)
            if len(xs) < 30:
                return None
            pad = 6
            tx0 = max(0, int(xs.min()) - pad)
            tx1 = min(mw, int(xs.max()) + pad)
            ty0 = max(0, int(ys.min()) - pad)
            ty1 = min(mh, int(ys.max()) + pad)
            tmpl = crop[ty0:ty1, tx0:tx1]
            if tmpl.size == 0:
                return None
            return tmpl, mx + tx0, my + ty0
        except Exception:
            return None

    def _box_map_find_tag_on_page(self, gray, tmpl, ex, ey, win, a, b):
        """[PATCH:page-tag] WO-BM2 T2: template-match a master tag on the
        FULL scan page, in a region of +/-win page pixels around its
        expected top-left (ex, ey). The template is resized by the page's
        scale (a, b). Returns (x, y, score) of the best match's top-left
        in scan-page pixels, or None when the region cannot be searched.
        Handwriting cannot move printed ink, so the tag is searched on the
        page itself -- never only inside a crop that may have missed it."""
        try:
            th, tw = tmpl.shape[:2]
            tws = max(1, int(round(tw * a)))
            ths = max(1, int(round(th * b)))
            t = cv2.resize(tmpl, (tws, ths), interpolation=cv2.INTER_AREA)
            x0 = max(0, int(round(ex)) - win)
            y0 = max(0, int(round(ey)) - win)
            x1 = min(gray.shape[1], int(round(ex)) + tws + win)
            y1 = min(gray.shape[0], int(round(ey)) + ths + win)
            search = gray[y0:y1, x0:x1]
            if (search.shape[0] < ths or search.shape[1] < tws
                    or t.size == 0):
                return None
            res = cv2.matchTemplate(search, t, cv2.TM_CCOEFF_NORMED)
            _, maxv, _, maxloc = cv2.minMaxLoc(res)
            return x0 + maxloc[0], y0 + maxloc[1], float(maxv)
        except Exception:
            return None

    def _box_map_tag_fit(self, gray, mgray, srects, mrects, s_to_m,
                         sx_pg, sy_pg):
        """[PATCH:tag-fit] WO-BM2 T2: the page's shift and scale fitted
        from PRINTED TAG positions (scan = scale*master + shift). For each
        matched box, its master tag is template-matched in the found
        rectangle's neighborhood; with >= 2 such tag fixes the scale comes
        from median pairwise position ratios (per axis, only pairs at
        least 100px apart on that axis -- otherwise the page-size ratio)
        and the shift from median offsets. Rectangle DIMENSIONS are never
        used: handwriting stretches rectangles but cannot move printed
        ink, so this estimate cannot be poisoned the way the height/width
        ratio medians were on the 19:06 run (b drifted to ~1.158).
        Returns (a, b, tx, ty), or None with fewer than 2 tag fixes."""
        fixes = []
        for i, j in s_to_m.items():
            info = self._box_map_master_tag(mgray, mrects[j])
            if info is None:
                continue
            tmpl, mtx, mty = info
            mx, my = mrects[j][0], mrects[j][1]
            ex = srects[i][0] + (mtx - mx) * sx_pg
            ey = srects[i][1] + (mty - my) * sy_pg
            hit = self._box_map_find_tag_on_page(gray, tmpl, ex, ey, 40,
                                                 sx_pg, sy_pg)
            if hit is None or hit[2] < 0.75:
                continue
            fixes.append((float(mtx), float(mty), float(hit[0]),
                          float(hit[1])))
        if len(fixes) < 2:
            return None
        med = self._box_map_median
        MIN_SPREAD = 100.0
        ar, br = [], []
        for k in range(len(fixes)):
            for l in range(k + 1, len(fixes)):
                dmx = fixes[l][0] - fixes[k][0]
                dmy = fixes[l][1] - fixes[k][1]
                if abs(dmx) >= MIN_SPREAD:
                    ar.append((fixes[l][2] - fixes[k][2]) / dmx)
                if abs(dmy) >= MIN_SPREAD:
                    br.append((fixes[l][3] - fixes[k][3]) / dmy)
        a = med(ar) if ar else sx_pg
        b = med(br) if br else sy_pg
        tx = med([fx[2] - a * fx[0] for fx in fixes])
        ty = med([fx[3] - b * fx[1] for fx in fixes])
        return a, b, tx, ty

    def _box_map_filtered_est(self, srects, mrects, s_to_m, sx_pg, sy_pg):
        """[PATCH:tag-fit] WO-BM2 T2: the rect-ratio estimator, but with
        dimension ratios outside 0.90-1.10 of the page scale EXCLUDED (a
        handwriting-stretched rectangle cannot set the scale). The
        fallback when fewer than 2 tag fixes exist on a page. Returns
        (a, b, tx, ty) or None when nothing usable remains."""
        if not s_to_m:
            return None
        med = self._box_map_median
        if len(s_to_m) == 1:
            i, j = next(iter(s_to_m.items()))
            a, b = sx_pg, sy_pg
            tx = (srects[i][0] + srects[i][2] / 2.0) \
                - a * (mrects[j][0] + mrects[j][2] / 2.0)
            ty = (srects[i][1] + srects[i][3] / 2.0) \
                - b * (mrects[j][1] + mrects[j][3] / 2.0)
            return a, b, tx, ty
        wr = [srects[i][2] / float(mrects[j][2]) for i, j in s_to_m.items()
              if 0.90 * sx_pg <= srects[i][2] / float(mrects[j][2])
              <= 1.10 * sx_pg]
        hr = [srects[i][3] / float(mrects[j][3]) for i, j in s_to_m.items()
              if 0.90 * sy_pg <= srects[i][3] / float(mrects[j][3])
              <= 1.10 * sy_pg]
        if not wr or not hr:
            return None
        a, b = med(wr), med(hr)
        tx = med([(srects[i][0] + srects[i][2] / 2.0)
                  - a * (mrects[j][0] + mrects[j][2] / 2.0)
                  for i, j in s_to_m.items()])
        ty = med([(srects[i][1] + srects[i][3] / 2.0)
                  - b * (mrects[j][1] + mrects[j][3] / 2.0)
                  for i, j in s_to_m.items()])
        return a, b, tx, ty

    def _box_map_master_cut(self, page_snippets, b_id, footer_num,
                            full_page_img_path, scan_page_num):
        """[PATCH:box-source] WO-BM3 T1: tag-registered cropping. With
        LEDGER_BOX_SOURCE=master, every answer box is CUT at the MASTER's
        coordinates (size = master box x the tag-fitted scale, each crop
        anchored on its OWN matched printed tag) instead of trusting the
        finder's contours, which handwriting stretches. The finder still
        runs as a CROSS-CHECK: its rectangles are paired to the cut crops,
        today's stray handling is kept, and a material disagreement is
        logged. Returns the snippet list in master reading order, or None
        when this page must take the finder path UNCHANGED: unreadable
        footer (999), a page outside the master's map, fewer than 2 tag
        fixes on the page (by design the ONLY guard for wrong-assignment
        pages), or any error."""
        try:
            bmap = getattr(self, "_box_map", None)
            last = getattr(self, "_box_map_last", None)
            if not bmap or last is None:
                return None
            try:
                offset = int(os.environ.get("LEDGER_S2CTX_PAGE_OFFSET", "0"))
                idx = int(footer_num) - 1 + offset
            except (TypeError, ValueError):
                return None
            if int(footer_num) == 999 or idx not in bmap:
                return None
            master = bmap[idx]
            mrects = master["rects"]
            srects = last["rects"]
            pw_s, ph_s = last["size"]
            pw_m, ph_m = master["size"]
            if not mrects or not pw_m or not ph_m:
                return None
            sx_pg = pw_s / float(pw_m)
            sy_pg = ph_s / float(ph_m)

            img = cv2.imread(full_page_img_path)
            mimg = cv2.imread(master["file"])
            if img is None or mimg is None:
                return None
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            mgray = cv2.cvtColor(mimg, cv2.COLOR_BGR2GRAY)

            try:
                tag_win = int(os.environ.get(
                    "LEDGER_BOX_TAG_SEARCH_PX", "150"))
            except (TypeError, ValueError):
                tag_win = 150

            # --- 1. TAGS FIRST: each master box's printed tag is searched
            # on the FULL page around the spot the page-size scale predicts.
            # Handwriting stretches rectangles but cannot move printed ink.
            tag_hits = {}    # master index -> (px, py, score, mtx, mty)
            fixes = []
            for j, rect in enumerate(mrects):
                info = self._box_map_master_tag(mgray, rect)
                if info is None:
                    continue
                tmpl, mtx, mty = info
                hit = self._box_map_find_tag_on_page(
                    gray, tmpl, mtx * sx_pg, mty * sy_pg, tag_win,
                    sx_pg, sy_pg)
                if hit is not None and hit[2] >= 0.75:
                    tag_hits[j] = (hit[0], hit[1], hit[2], mtx, mty)
                    fixes.append((float(mtx), float(mty),
                                  float(hit[0]), float(hit[1])))

            # --- 2. FIT from tags, the WO-BM2 way. Fewer than 2 tag fixes:
            # the whole page falls back to the finder path unchanged.
            if len(fixes) < 2:
                return None
            med = self._box_map_median
            MIN_SPREAD = 100.0
            ar, br = [], []
            for k in range(len(fixes)):
                for l in range(k + 1, len(fixes)):
                    dmx = fixes[l][0] - fixes[k][0]
                    dmy = fixes[l][1] - fixes[k][1]
                    if abs(dmx) >= MIN_SPREAD:
                        ar.append((fixes[l][2] - fixes[k][2]) / dmx)
                    if abs(dmy) >= MIN_SPREAD:
                        br.append((fixes[l][3] - fixes[k][3]) / dmy)
            a = med(ar) if ar else sx_pg
            b = med(br) if br else sy_pg
            tx = med([fx[2] - a * fx[0] for fx in fixes])
            ty = med([fx[3] - b * fx[1] for fx in fixes])

            # --- the finder's pairing (today's rule, incl. the legacy
            # switch) -- used for the gate-failure fallback and the strays.
            diag = (pw_m * pw_m + ph_m * ph_m) ** 0.5
            pairs = []
            if os.environ.get("LEDGER_BOX_PAIRING", "overlap") == "legacy":
                TOL_SHIFT = 0.045 * diag
                TOL_SIZE = 0.15
                for i, (sx, sy, sw, sh) in enumerate(srects):
                    for j, (mx, my, mw, mh) in enumerate(mrects):
                        if abs(sw / sx_pg - mw) > TOL_SIZE * mw:
                            continue
                        if abs(sh / sy_pg - mh) > TOL_SIZE * mh:
                            continue
                        dcx = (sx + sw / 2.0) / sx_pg - (mx + mw / 2.0)
                        dcy = (sy + sh / 2.0) / sy_pg - (my + mh / 2.0)
                        d = (dcx * dcx + dcy * dcy) ** 0.5
                        if d <= TOL_SHIFT:
                            pairs.append((d, i, j))
            else:
                for j, (mx, my, mw, mh) in enumerate(mrects):
                    gx0, gy0 = mx * sx_pg, my * sy_pg
                    gx1, gy1 = (mx + mw) * sx_pg, (my + mh) * sy_pg
                    marea = (gx1 - gx0) * (gy1 - gy0)
                    if marea <= 0:
                        continue
                    for i, (sx, sy, sw, sh) in enumerate(srects):
                        ix = min(sx + sw, gx1) - max(sx, gx0)
                        iy = min(sy + sh, gy1) - max(sy, gy0)
                        if ix <= 0 or iy <= 0:
                            continue
                        cover = (ix * iy) / marea
                        if cover >= 0.5:
                            pairs.append((-cover, i, j))
            pairs.sort()
            s_to_m = {}
            m_to_s = {}
            for d, i, j in pairs:
                if i in s_to_m or j in m_to_s:
                    continue
                s_to_m[i] = j
                m_to_s[j] = i

            # --- 3. CUT EVERY BOX at the master's coordinates.
            delivered = []    # (master_index, path, cut_rect, how)
            lost = []
            n_tag = n_fit = n_fb = 0
            for j, (mx, my, mw, mh) in enumerate(mrects):
                w = a * mw
                h = b * mh
                margin = max(8, int(round(0.04 * min(w, h))))
                mmx = max(1, int(round(margin / a)))
                mmy = max(1, int(round(margin / b)))
                mx0 = max(0, mx - mmx)
                my0 = max(0, my - mmy)
                mx1 = min(mimg.shape[1], mx + mw + mmx)
                my1 = min(mimg.shape[0], my + mh + mmy)
                mcrop = mgray[my0:my1, mx0:mx1]
                hit = tag_hits.get(j)
                if hit is not None:
                    px, py, tscore, mtx, mty = hit
                    bx0 = int(round(px - a * (mtx - mx)))
                    by0 = int(round(py - b * (mty - my)))
                    x0 = max(0, bx0 - margin)
                    y0 = max(0, by0 - margin)
                    x1 = min(img.shape[1], int(round(bx0 + w)) + margin)
                    y1 = min(img.shape[0], int(round(by0 + h)) + margin)
                else:
                    cx = a * (mx + mw / 2.0) + tx
                    cy = b * (my + mh / 2.0) + ty
                    x0 = max(0, int(round(cx - w / 2.0)) - margin)
                    y0 = max(0, int(round(cy - h / 2.0)) - margin)
                    x1 = min(img.shape[1],
                             int(round(cx + w / 2.0)) + margin)
                    y1 = min(img.shape[0],
                             int(round(cy + h / 2.0)) + margin)
                ok = False
                detail = ""
                if x1 - x0 >= 20 and y1 - y0 >= 20:
                    if hit is not None:
                        # anchored on its own printed tag: accepted (the
                        # WO-BM2 rule); the frame is measured for the log.
                        fr, _tg, _d1, _d2, err = self._box_map_scores(
                            gray[y0:y1, x0:x1], mcrop, margin=margin,
                            win=20)
                        frs = ("n/a" if err is not None or fr is None
                               else format(fr, ".2f"))
                        ok = True
                        detail = f"frame {frs}, tag {tscore:.2f}"
                    else:
                        # no tag fix of its own: cut at the fitted
                        # coordinates, today's frame >= 0.80 gate.
                        fr, _tg, _d1, _d2, err = self._box_map_scores(
                            gray[y0:y1, x0:x1], mcrop, margin=margin,
                            win=20)
                        if err:
                            detail = err
                        else:
                            ok = fr >= 0.80
                            detail = f"frame {fr:.2f}, tag n/a"
                else:
                    detail = "degenerate crop"
                if ok:
                    snippet_path = os.path.join(
                        TEMP_DIR,
                        f"snippet_p{scan_page_num}_m{j}.png")
                    cv2.imwrite(snippet_path, img[y0:y1, x0:x1])
                    delivered.append((j, snippet_path, (x0, y0, x1, y1)))
                    if hit is not None:
                        n_tag += 1
                    else:
                        n_fit += 1
                        self.log_message(
                            f"    -> [BOX MAP] ID {b_id}, page "
                            f"{footer_num}: box {j + 1} cut at the fitted "
                            f"coordinates ({detail}).")
                    continue
                # gate failed: the finder's crop for this box, if it has one
                i = m_to_s.get(j)
                if i is not None and i < len(page_snippets):
                    sx, sy, sw, sh = srects[i]
                    delivered.append((j, page_snippets[i],
                                      (sx, sy, sx + sw, sy + sh)))
                    n_fb += 1
                    self.log_message(
                        f"    -> [BOX MAP] ID {b_id}, page {footer_num}: "
                        f"master-coordinate crop for box {j + 1} failed "
                        f"verification ({detail}); the finder's crop was "
                        "used instead.")
                    continue
                # nothing to send for this box: reject + save + notice below
                lost.append(j)
                self.log_message(
                    f"    -> [BOX MAP] ID {b_id}, page {footer_num}: "
                    f"master-coordinate crop for box {j + 1} REJECTED "
                    f"({detail}).")
                self._box_map_save_reject(
                    img[y0:y1, x0:x1] if (x1 > x0 and y1 > y0) else None,
                    b_id, footer_num, j + 1, detail)

            self.log_message(
                f"    -> [BOX MAP] ID {b_id}, page {footer_num}: "
                f"{len(delivered)} of {len(mrects)} box(es) cut at the "
                f"master's coordinates ({n_tag} tag-anchored, {n_fit} at "
                f"fitted coordinates, {n_fb} from the finder).")

            # --- 4. the finder as CROSS-CHECK: today's stray handling.
            extras = [i for i in range(len(srects)) if i not in s_to_m]
            drop_extras = os.environ.get(
                "LEDGER_BOX_DROP_EXTRAS", "1") != "0"
            n_lost = len(lost)

            def _cut_key(r):
                return (r[1] // 150, r[0])

            out = [(_cut_key(rect), path) for _j, path, rect in delivered]
            if extras:
                if drop_extras and n_lost == 0:
                    for _ in extras:
                        self._box_map_record_notice(
                            f"ID {b_id}, page {footer_num}: a stray "
                            "rectangle (likely drawn by the student) was "
                            "ignored.")
                else:
                    for i in extras:
                        if i < len(page_snippets):
                            sx, sy, sw, sh = srects[i]
                            out.append(((sy // 150, sx), page_snippets[i]))
                        self._box_map_record_notice(
                            f"ID {b_id}, page {footer_num}: an extra "
                            "rectangle on the page was treated as an "
                            "answer box. Please verify.")
                    out.sort(key=lambda t: t[0])
            # a PAIRED finder rectangle that overlaps no cut crop is a
            # material disagreement worth a notice (an unpaired one was
            # already reported as a stray/extra just above).
            for i, (sx, sy, sw, sh) in enumerate(srects):
                if i not in s_to_m:
                    continue
                overlaps = False
                for _j, _path, (cx0, cy0, cx1, cy1) in delivered:
                    ix = min(sx + sw, cx1) - max(sx, cx0)
                    iy = min(sy + sh, cy1) - max(sy, cy0)
                    if ix > 0 and iy > 0 and \
                            (ix * iy) >= 0.5 * (sw * sh):
                        overlaps = True
                        break
                if not overlaps:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: the box finder "
                        "found a rectangle that no master-coordinate crop "
                        "covers. Please verify this page.")

            if n_lost:
                matched_n = len(mrects) - n_lost
                total_n = len(mrects)
                if n_lost == 1:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: found {matched_n} "
                        f"of {total_n} answer boxes and could not recover "
                        "the missing one. That answer was NOT scored. "
                        "Please check this page by hand.")
                else:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: found {matched_n} "
                        f"of {total_n} answer boxes and could not recover "
                        f"{n_lost} missing ones. Those answers were NOT "
                        "scored. Please check this page by hand.")

            # the same page shift/scale statistic the finder path records,
            # so pages that FALL BACK later in the run see identical
            # run-median behavior in both modes.
            est = None
            if len(s_to_m) >= 2:
                ea = self._box_map_median(
                    [srects[i][2] / float(mrects[j][2])
                     for i, j in s_to_m.items()])
                eb = self._box_map_median(
                    [srects[i][3] / float(mrects[j][3])
                     for i, j in s_to_m.items()])
                etx = self._box_map_median(
                    [(srects[i][0] + srects[i][2] / 2.0)
                     - ea * (mrects[j][0] + mrects[j][2] / 2.0)
                     for i, j in s_to_m.items()])
                ety = self._box_map_median(
                    [(srects[i][1] + srects[i][3] / 2.0)
                     - eb * (mrects[j][1] + mrects[j][3] / 2.0)
                     for i, j in s_to_m.items()])
                est = (ea, eb, etx, ety)
            elif len(s_to_m) == 1:
                i, j = next(iter(s_to_m.items()))
                ea, eb = sx_pg, sy_pg
                etx = (srects[i][0] + srects[i][2] / 2.0) \
                    - ea * (mrects[j][0] + mrects[j][2] / 2.0)
                ety = (srects[i][1] + srects[i][3] / 2.0) \
                    - eb * (mrects[j][1] + mrects[j][3] / 2.0)
                est = (ea, eb, etx, ety)
            if est is not None:
                if not hasattr(self, "_box_map_page_stats"):
                    self._box_map_page_stats = []
                self._box_map_page_stats.append(est)

            return [path for _key, path in out]
        except Exception as e:
            self.log_message(f"[!] [BOX MAP] master-source cut failed on "
                             f"page {footer_num} (ID {b_id}): {e} -- the "
                             "finder path is used.")
            return None

    def _box_map_reconcile(self, page_snippets, b_id, footer_num,
                           full_page_img_path, scan_page_num):
        """Match this scan page's found boxes against the master's box map.
        Clean page (every master box matched, nothing extra): returns the
        list UNTOUCHED -- byte-identical path. An extra scan box is kept, as
        today, with a notice. A missing master box is re-cropped from the
        scan at the master's rectangle (shift/scale estimated from the
        matched boxes, else from earlier pages), VERIFIED against the
        master's printed structure, and inserted in master reading order; an
        unrecoverable box gets a plain-words NOT-scored notice."""
        try:
            if os.environ.get("LEDGER_BOX_MAP", "1") == "0":
                return page_snippets
            # [PATCH:box-source] WO-BM3 T1: LEDGER_BOX_SOURCE=master cuts
            # every box at the master's coordinates (tag-registered).
            # WO-BM3b T2 made "master" the DEFAULT (unset = master) after
            # the quiz-corpus proof; LEDGER_BOX_SOURCE=finder -- or any
            # other explicit non-"master" value -- restores the finder
            # path below bit-identically, with no code change. A None
            # from the master path (unreadable footer, out-of-map page,
            # < 2 tag fixes, any error) also falls through to the finder
            # path unchanged.
            if os.environ.get("LEDGER_BOX_SOURCE", "master") == "master":
                out = self._box_map_master_cut(
                    page_snippets, b_id, footer_num, full_page_img_path,
                    scan_page_num)
                if out is not None:
                    return out
            bmap = getattr(self, "_box_map", None)
            last = getattr(self, "_box_map_last", None)
            if not bmap or last is None:
                return page_snippets
            try:
                offset = int(os.environ.get("LEDGER_S2CTX_PAGE_OFFSET", "0"))
                idx = int(footer_num) - 1 + offset
            except (TypeError, ValueError):
                return page_snippets
            if int(footer_num) == 999 or idx not in bmap:
                return page_snippets

            master = bmap[idx]
            mrects = master["rects"]
            srects = last["rects"]
            pw_s, ph_s = last["size"]
            pw_m, ph_m = master["size"]
            if not pw_m or not ph_m:
                return page_snippets
            sx_pg = pw_s / float(pw_m)
            sy_pg = ph_s / float(ph_m)
            diag = (pw_m * pw_m + ph_m * ph_m) ** 0.5

            # --- match scan boxes to master boxes ---
            # [PATCH:box-pairing] WO-BM1 T2: pair by OVERLAP. A rectangle
            # stretched by handwriting still COVERS its master box (measured
            # on the 16:18 run: ID 108 p2 box 6 was 17.4% too wide for the
            # legacy size test while its center was only 46px off and it
            # covered 95% of the box). A scan rect is a candidate when the
            # intersection covers >= 0.5 of the master box's scaled area;
            # assigned greedily by largest coverage, one-to-one. Everything
            # downstream is unchanged. LEDGER_BOX_PAIRING=legacy restores
            # the old size+center rule exactly.
            pairs = []
            if os.environ.get("LEDGER_BOX_PAIRING", "overlap") == "legacy":
                TOL_SHIFT = 0.045 * diag   # ~0.6 inch at 300 dpi
                TOL_SIZE = 0.15
                for i, (sx, sy, sw, sh) in enumerate(srects):
                    for j, (mx, my, mw, mh) in enumerate(mrects):
                        if abs(sw / sx_pg - mw) > TOL_SIZE * mw:
                            continue
                        if abs(sh / sy_pg - mh) > TOL_SIZE * mh:
                            continue
                        dcx = (sx + sw / 2.0) / sx_pg - (mx + mw / 2.0)
                        dcy = (sy + sh / 2.0) / sy_pg - (my + mh / 2.0)
                        d = (dcx * dcx + dcy * dcy) ** 0.5
                        if d <= TOL_SHIFT:
                            pairs.append((d, i, j))
            else:
                for j, (mx, my, mw, mh) in enumerate(mrects):
                    gx0, gy0 = mx * sx_pg, my * sy_pg
                    gx1, gy1 = (mx + mw) * sx_pg, (my + mh) * sy_pg
                    marea = (gx1 - gx0) * (gy1 - gy0)
                    if marea <= 0:
                        continue
                    for i, (sx, sy, sw, sh) in enumerate(srects):
                        ix = min(sx + sw, gx1) - max(sx, gx0)
                        iy = min(sy + sh, gy1) - max(sy, gy0)
                        if ix <= 0 or iy <= 0:
                            continue
                        cover = (ix * iy) / marea
                        if cover >= 0.5:
                            # negated so the shared ascending sort + greedy
                            # loop below takes the LARGEST coverage first
                            pairs.append((-cover, i, j))
            pairs.sort()
            s_to_m = {}
            m_to_s = {}
            for d, i, j in pairs:
                if i in s_to_m or j in m_to_s:
                    continue
                s_to_m[i] = j
                m_to_s[j] = i

            # --- this page's shift/scale estimate (scan = scale*master + shift) ---
            est = None
            if len(s_to_m) >= 2:
                a = self._box_map_median([srects[i][2] / float(mrects[j][2])
                                          for i, j in s_to_m.items()])
                b = self._box_map_median([srects[i][3] / float(mrects[j][3])
                                          for i, j in s_to_m.items()])
                tx = self._box_map_median(
                    [(srects[i][0] + srects[i][2] / 2.0)
                     - a * (mrects[j][0] + mrects[j][2] / 2.0)
                     for i, j in s_to_m.items()])
                ty = self._box_map_median(
                    [(srects[i][1] + srects[i][3] / 2.0)
                     - b * (mrects[j][1] + mrects[j][3] / 2.0)
                     for i, j in s_to_m.items()])
                est = (a, b, tx, ty)
            elif len(s_to_m) == 1:
                i, j = next(iter(s_to_m.items()))
                a, b = sx_pg, sy_pg
                tx = (srects[i][0] + srects[i][2] / 2.0) \
                    - a * (mrects[j][0] + mrects[j][2] / 2.0)
                ty = (srects[i][1] + srects[i][3] / 2.0) \
                    - b * (mrects[j][1] + mrects[j][3] / 2.0)
                est = (a, b, tx, ty)
            if est is not None:
                if not hasattr(self, "_box_map_page_stats"):
                    self._box_map_page_stats = []
                self._box_map_page_stats.append(est)

            extras = [i for i in range(len(srects)) if i not in s_to_m]
            missing = [j for j in range(len(mrects)) if j not in m_to_s]

            # Clean page: the byte-identical path. Nothing logged, nothing touched.
            if not extras and not missing:
                return page_snippets

            # [PATCH:drop-extras] WO-BM1 T4: a scan rectangle paired with no
            # master box (e.g. one the student drew around their own work)
            # is DROPPED from what is sent, but ONLY when every master box
            # on this page is accounted for (matched, or recovered-and-
            # accepted) -- decided AFTER recovery below. On any other page
            # it is kept and sent exactly as today, because it might BE the
            # missing box. LEDGER_BOX_DROP_EXTRAS=0 restores today's
            # keep-everything behavior exactly.
            drop_extras = os.environ.get("LEDGER_BOX_DROP_EXTRAS", "1") != "0"
            if not drop_extras:
                for _ in extras:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: an extra rectangle on the "
                        "page was treated as an answer box. Please verify.")
                if not missing:
                    return page_snippets

            # --- no estimate from this page: borrow the run-wide median ---
            if est is None:
                stats = getattr(self, "_box_map_page_stats", [])
                if len(stats) >= 5:
                    est = tuple(self._box_map_median([s[k] for s in stats])
                                for k in range(4))

            matched_n = len(m_to_s)
            total_n = len(mrects)
            recovered = []     # (master_index, snippet_path)
            if est is not None and missing:
                a, b, tx, ty = est
                img = cv2.imread(full_page_img_path)
                mimg = cv2.imread(master["file"])
                if img is None or mimg is None:
                    est = None
                else:
                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                    mgray = cv2.cvtColor(mimg, cv2.COLOR_BGR2GRAY)
                    # [PATCH:tag-recovery] WO-BM1 T3: recovery is anchored
                    # on the box's printed tag number. The tag template is
                    # searched in a wider window; found at >= 0.75, the crop
                    # is RE-CUT centered on the measured tag position and
                    # ACCEPTED (the frame score is still computed and
                    # logged, but no longer rejects a tag-verified crop --
                    # measured on the 16:18 run: 5 of 8 rejections had tag
                    # >= 0.93 with frame <= 0.30, i.e. the crop was merely
                    # 10-15px off target). A master box with NO printed tag
                    # ink keeps the old frame >= 0.80 gate exactly; a page
                    # whose tag cannot be found anywhere in the window is
                    # REJECTED -- the protection against wrong-assignment
                    # pages. LEDGER_BOX_RECOVER_TAG=0 restores the old
                    # two-test gate with the old +/-20px window exactly.
                    recover_tag = os.environ.get(
                        "LEDGER_BOX_RECOVER_TAG", "1") != "0"
                    # [PATCH:page-tag] WO-BM2 T2: by default the tag is
                    # searched on the FULL PAGE (+/-150 page px around the
                    # predicted spot) and the crop is anchored on it;
                    # LEDGER_BOX_TAG_PAGE=0 restores the WO-BM1 behavior
                    # (search inside the predicted crop, +/-80) exactly.
                    tag_page = os.environ.get(
                        "LEDGER_BOX_TAG_PAGE", "1") != "0"
                    default_win = 150 if (recover_tag and tag_page) else 80
                    try:
                        tag_win = int(os.environ.get(
                            "LEDGER_BOX_TAG_SEARCH_PX", str(default_win)))
                    except (TypeError, ValueError):
                        tag_win = default_win
                    # [PATCH:tag-fit] WO-BM2 T2: anchor the page estimate
                    # on printed tags, never on found-rectangle dimensions
                    # (stretched rectangles poisoned the height medians on
                    # the 19:06 run). LEDGER_BOX_TAG_FIT=0 restores the
                    # rect-ratio estimator alone.
                    recover_missing = list(missing)
                    if recover_tag and os.environ.get(
                            "LEDGER_BOX_TAG_FIT", "1") != "0":
                        fit = self._box_map_tag_fit(
                            gray, mgray, srects, mrects, s_to_m,
                            sx_pg, sy_pg)
                        if fit is None:
                            fit = self._box_map_filtered_est(
                                srects, mrects, s_to_m, sx_pg, sy_pg)
                        if fit is None:
                            stats = getattr(self, "_box_map_page_stats", [])
                            if len(stats) >= 5:
                                fit = tuple(
                                    self._box_map_median([s[k] for s in
                                                          stats])
                                    for k in range(4))
                        if fit is not None:
                            a, b, tx, ty = fit
                        else:
                            # no usable estimate at all: nothing to anchor
                            # a recovery on; the NOT-scored notices below
                            # say so in plain words
                            recover_missing = []
                    for j in recover_missing:
                        mx, my, mw, mh = mrects[j]
                        w = a * mw
                        h = b * mh
                        cx = a * (mx + mw / 2.0) + tx
                        cy = b * (my + mh / 2.0) + ty
                        margin = max(8, int(round(0.04 * min(w, h))))
                        x0 = max(0, int(round(cx - w / 2.0)) - margin)
                        y0 = max(0, int(round(cy - h / 2.0)) - margin)
                        x1 = min(img.shape[1], int(round(cx + w / 2.0)) + margin)
                        y1 = min(img.shape[0], int(round(cy + h / 2.0)) + margin)
                        if x1 - x0 < 20 or y1 - y0 < 20:
                            continue
                        # master crop with the EQUIVALENT margin so the two
                        # structures line up for verification
                        mmx = max(1, int(round(margin / a)))
                        mmy = max(1, int(round(margin / b)))
                        mx0 = max(0, mx - mmx)
                        my0 = max(0, my - mmy)
                        mx1 = min(mimg.shape[1], mx + mw + mmx)
                        my1 = min(mimg.shape[0], my + mh + mmy)
                        scan_crop = img[y0:y1, x0:x1]
                        if not recover_tag:
                            ok, detail = self._box_map_verify(
                                gray[y0:y1, x0:x1], mgray[my0:my1, mx0:mx1],
                                margin=margin)
                        elif not tag_page:
                            mcrop = mgray[my0:my1, mx0:mx1]
                            fr, tag, tdx, tdy, err = self._box_map_scores(
                                gray[y0:y1, x0:x1], mcrop, margin=margin,
                                win=tag_win)
                            if err:
                                ok, detail = False, err
                            elif tag is None:
                                # no printed tag ink in this master box:
                                # today's frame gate, unchanged
                                ok = fr >= 0.80
                                detail = f"frame {fr:.2f}, tag n/a"
                            elif tag >= 0.75:
                                # re-cut the crop from the page, shifted by
                                # the tag's measured offset (master-crop px
                                # converted back to scan px), then accept
                                cw, ch = x1 - x0, y1 - y0
                                dx = int(round(tdx * cw
                                               / float(mx1 - mx0 or 1)))
                                dy = int(round(tdy * ch
                                               / float(my1 - my0 or 1)))
                                x0 = max(0, min(img.shape[1] - cw, x0 + dx))
                                y0 = max(0, min(img.shape[0] - ch, y0 + dy))
                                x1, y1 = x0 + cw, y0 + ch
                                scan_crop = img[y0:y1, x0:x1]
                                fr2, tag2, _d1, _d2, err2 = \
                                    self._box_map_scores(
                                        gray[y0:y1, x0:x1], mcrop,
                                        margin=margin, win=tag_win)
                                ok = True
                                if err2 is None and tag2 is not None:
                                    detail = (f"frame {fr2:.2f}, tag "
                                              f"{tag2:.2f}; crop re-centered"
                                              f" on the printed tag "
                                              f"({dx:+d},{dy:+d}px)")
                                else:
                                    detail = (f"frame {fr:.2f}, tag "
                                              f"{tag:.2f}; crop re-centered"
                                              f" on the printed tag "
                                              f"({dx:+d},{dy:+d}px)")
                            else:
                                ok = False
                                detail = (f"frame {fr:.2f}, tag {tag:.2f}; "
                                          "the box's printed tag number was "
                                          f"not found within {tag_win}px of "
                                          "the expected spot")
                        else:
                            # [PATCH:page-tag] WO-BM2 T2: search the box's
                            # printed tag on the PAGE, +/-tag_win page px
                            # around its predicted spot. The tag sits at
                            # the box's top-left corner, so a crop that
                            # landed low pushes it off the crop's EDGE --
                            # searching inside the crop (WO-BM1) could
                            # never see it. Found at >= 0.75: the final
                            # crop is cut anchored on the matched tag,
                            # sized from the MASTER box times the fitted
                            # scale, and ACCEPTED (frame logged only).
                            # Not found anywhere in the region: REJECTED
                            # -- the guard against wrong-assignment pages.
                            mcrop = mgray[my0:my1, mx0:mx1]
                            info = self._box_map_master_tag(mgray, mrects[j])
                            if info is None:
                                # no printed tag ink in this master box:
                                # today's frame gate, unchanged
                                fr, _tg, _d1, _d2, err = \
                                    self._box_map_scores(
                                        gray[y0:y1, x0:x1], mcrop,
                                        margin=margin, win=20)
                                if err:
                                    ok, detail = False, err
                                else:
                                    ok = fr >= 0.80
                                    detail = f"frame {fr:.2f}, tag n/a"
                            else:
                                tmpl, mtx, mty = info
                                ex = a * mtx + tx
                                ey = b * mty + ty
                                hit = self._box_map_find_tag_on_page(
                                    gray, tmpl, ex, ey, tag_win, a, b)
                                if hit is not None and hit[2] >= 0.75:
                                    px, py, tscore = hit
                                    bx0 = int(round(px - a * (mtx - mx)))
                                    by0 = int(round(py - b * (mty - my)))
                                    x0 = max(0, bx0 - margin)
                                    y0 = max(0, by0 - margin)
                                    x1 = min(img.shape[1],
                                             int(round(bx0 + w)) + margin)
                                    y1 = min(img.shape[0],
                                             int(round(by0 + h)) + margin)
                                    scan_crop = img[y0:y1, x0:x1]
                                    fr2, _tg2, _d1, _d2, err2 = \
                                        self._box_map_scores(
                                            gray[y0:y1, x0:x1], mcrop,
                                            margin=margin, win=20)
                                    ok = True
                                    frs = ("n/a" if err2 is not None
                                           or fr2 is None
                                           else format(fr2, ".2f"))
                                    dx = int(round(px - ex))
                                    dy = int(round(py - ey))
                                    detail = (f"frame {frs}, tag "
                                              f"{tscore:.2f}; crop anchored"
                                              f" on the printed tag, found "
                                              f"{dx:+d},{dy:+d}px from the "
                                              "predicted spot")
                                else:
                                    best = 0.0 if hit is None else hit[2]
                                    fr, _tg, _d1, _d2, err = \
                                        self._box_map_scores(
                                            gray[y0:y1, x0:x1], mcrop,
                                            margin=margin, win=20)
                                    frs = ("n/a" if err is not None
                                           or fr is None
                                           else format(fr, ".2f"))
                                    ok = False
                                    detail = (f"frame {frs}, tag "
                                              f"{best:.2f}; the box's "
                                              "printed tag number was not "
                                              f"found within {tag_win}px "
                                              "of the expected spot on "
                                              "the page")
                        self.log_message(
                            f"    -> [BOX MAP] ID {b_id}, page {footer_num}: "
                            f"recovery crop for master box {j + 1} "
                            f"{'VERIFIED' if ok else 'REJECTED'} ({detail}).")
                        if not ok:
                            # [PATCH:box-rejects] WO-BM1 T5: keep the crop
                            self._box_map_save_reject(
                                scan_crop, b_id, footer_num, j + 1, detail)
                            continue
                        snippet_path = os.path.join(
                            TEMP_DIR,
                            f"snippet_p{scan_page_num}_b"
                            f"{len(page_snippets) + len(recovered)}.png")
                        cv2.imwrite(snippet_path, scan_crop)
                        recovered.append((j, snippet_path))

            # --- insert verified crops in master reading order ---
            out = list(page_snippets)
            ranks = [s_to_m.get(i) for i in range(len(page_snippets))]
            for j, snippet_path in sorted(recovered):
                pos = 0
                for k, r in enumerate(ranks):
                    if r is not None and r < j:
                        pos = k + 1
                out.insert(pos, snippet_path)
                ranks.insert(pos, j)

            n_rec = len(recovered)
            n_lost = len(missing) - n_rec
            # [PATCH:drop-extras] WO-BM1 T4: the stray decision, now that
            # recovery has settled whether every master box is accounted for.
            if drop_extras and extras:
                if n_lost == 0:
                    stray = {page_snippets[i] for i in extras}
                    out = [p for p in out if p not in stray]
                    for _ in extras:
                        self._box_map_record_notice(
                            f"ID {b_id}, page {footer_num}: a stray "
                            "rectangle (likely drawn by the student) was "
                            "ignored.")
                else:
                    for _ in extras:
                        self._box_map_record_notice(
                            f"ID {b_id}, page {footer_num}: an extra "
                            "rectangle on the page was treated as an answer "
                            "box. Please verify.")
            if n_rec:
                if n_rec == 1:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: 1 answer box was not "
                        "found automatically and was recovered from the "
                        "master's layout. Please verify.")
                else:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: {n_rec} answer boxes "
                        "were not found automatically and were recovered from "
                        "the master's layout. Please verify.")
            if n_lost:
                if n_lost == 1:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: found {matched_n} of "
                        f"{total_n} answer boxes and could not recover the "
                        "missing one. That answer was NOT scored. Please "
                        "check this page by hand.")
                else:
                    self._box_map_record_notice(
                        f"ID {b_id}, page {footer_num}: found {matched_n} of "
                        f"{total_n} answer boxes and could not recover "
                        f"{n_lost} missing ones. Those answers were NOT "
                        "scored. Please check this page by hand.")
            return out
        except Exception as e:
            # Never let the safety net break the pipeline it protects.
            self.log_message(f"[!] [BOX MAP] check failed on page "
                             f"{footer_num} (ID {b_id}): {e} -- page sent "
                             "as found.")
            return page_snippets

    # ==========================================
    # SURGERY AREA 3: THE LOCAL FLASHCARD STITCH (UPGRADED TO 2D GRID)
    # ==========================================
    def _stitch_paths_to_image(self, snippet_paths):
        """[PATCH:silo2-chunking] Row-packs a list of snippet image paths into a
        single PIL Image (balanced 2D grid, width-thresholded). Returns the
        canvas, or None if empty. Does not save to disk."""
        if not snippet_paths:
            return None
        images = [Image.open(p) for p in snippet_paths]

        MAX_ROW_WIDTH = 1600
        PADDING = 15

        rows = []
        current_row = []
        current_row_width = 0
        current_row_height = 0

        for img in images:
            if current_row_width + img.width + PADDING > MAX_ROW_WIDTH and current_row:
                rows.append({"images": current_row,
                             "width": current_row_width - PADDING,
                             "height": current_row_height})
                current_row = []
                current_row_width = 0
                current_row_height = 0
            current_row.append(img)
            current_row_width += img.width + PADDING
            current_row_height = max(current_row_height, img.height)

        if current_row:
            rows.append({"images": current_row,
                         "width": current_row_width - PADDING,
                         "height": current_row_height})

        if not rows:
            return None

        total_width = max(row["width"] for row in rows)
        total_height = sum(row["height"] for row in rows) + (PADDING * (len(rows) - 1))

        canvas = Image.new('RGB', (total_width, total_height), color='white')
        current_y = 0
        for row in rows:
            current_x = 0
            for img in row["images"]:
                canvas.paste(img, (current_x, current_y))
                current_x += img.width + PADDING
            current_y += row["height"] + PADDING
        return canvas

    def stitch_snippets_to_grid(self, snippet_paths, b_id, master_page_map=None):
        """[PATCH:silo2-chunking] Returns a LIST of chunk descriptors (was: a
        list of bare paths). Chunks on PAGE boundaries -- the page number is
        parsed from each snippet filename ('snippet_p<page>_b<n>.png') -- and
        NEVER splits a page, so multi-box / cross-referenced items on a page
        stay together. Whole page grids are packed vertically until
        LEDGER_SILO2_MAX_CHUNK_PX, then a new chunk starts; a single page
        taller than the budget becomes its own chunk.

        [PATCH:silo2-per-chunk-context] Each descriptor is a dict:
            {"path": <chunk png>, "footer_pages": [printed page nums] | None}
        footer_pages comes from master_page_map (snippet path -> the FOOTER
        page number OCR'd in Pass 1). The p<page> in the snippet filename is
        the SCAN-STACK index and is used ONLY for stitch geometry -- it is
        never used to pick master pages, because a scan stack is multi-student
        and order-independent. Any page whose footer read is missing or 999
        makes the whole chunk's footer_pages None -> FULL-master fallback
        (an OCR miss may cost weight, never correctness).
        """
        if not snippet_paths:
            return []
        try:
            MAX_CHUNK_HEIGHT = int(os.environ.get("LEDGER_SILO2_MAX_CHUNK_PX", "3500"))
            PADDING = 15

            # 1. Group snippets by scan page, preserving first-seen order, and
            #    collect each scan page's footer-derived master page number.
            pages = {}
            page_order = []
            page_footers = {}
            for p in snippet_paths:
                m = re.search(r'snippet_p(\d+)_b\d+', os.path.basename(p))
                pg = int(m.group(1)) if m else 0
                if pg not in pages:
                    pages[pg] = []
                    page_order.append(pg)
                    page_footers[pg] = set()
                pages[pg].append(p)
                page_footers[pg].add(master_page_map.get(p) if master_page_map else None)

            # [PATCH:page-order] WO-B1-3: order this student's scan pages by
            # their printed (footer-derived) page number before packing, so
            # every student produces the SAME chunk groupings and the prompt
            # cache is written once, not once per scan order. Local reorder
            # only -- box order within a page, the packing rule, the height
            # limit and the per-chunk master logic are untouched. A page
            # whose footer is missing or 999 KEEPS its scanned position
            # (never guess a number); two pages with the same number stay in
            # scanned order (stable sort) with one warning. LEDGER_SORT_PAGES=0
            # restores scan order byte-identically.
            if os.environ.get("LEDGER_SORT_PAGES", "1") != "0" and master_page_map:
                def _footer_num(pg):
                    fs = page_footers.get(pg)
                    if fs and len(fs) == 1:
                        f = next(iter(fs))
                        if isinstance(f, int) and 0 < f < 999:
                            return f
                    return None
                sortable = [pg for pg in page_order if _footer_num(pg) is not None]
                placed = iter(sorted(sortable, key=_footer_num))
                new_order = [pg if _footer_num(pg) is None else next(placed)
                             for pg in page_order]
                dup_nums = {}
                for pg in sortable:
                    dup_nums.setdefault(_footer_num(pg), []).append(pg)
                for num, pgs in sorted(dup_nums.items()):
                    if len(pgs) > 1:
                        self.log_message(f"    -> WARNING (ID {b_id}): {len(pgs)} pages "
                                         f"share printed page number {num}; keeping both, "
                                         f"in scanned order.")
                if new_order != page_order:
                    self.log_message(f"    -> Pages for ID {b_id} reordered by printed "
                                     f"page number before chunking.")
                page_order = new_order

            # 2. Stitch each page into its own grid image (true heights).
            # [PATCH:count-check] remember how many box crops each page grid
            # holds; the chunk packer sums them onto the descriptor.
            page_imgs = []
            for pg in page_order:
                canvas = self._stitch_paths_to_image(pages[pg])
                if canvas is not None:
                    page_imgs.append((canvas, page_footers[pg], len(pages[pg])))
            if not page_imgs:
                return []

            # 3. Greedily pack whole page grids into vertical chunks under budget.
            # [PATCH:count-check] each chunk also carries how many box crops
            # went into it, so the reply's item count can be verified.
            chunks = []
            cur = []
            cur_f = set()
            cur_n = 0
            cur_h = 0
            for img, fset, n_boxes in page_imgs:
                add = img.height + (PADDING if cur else 0)
                if cur and (cur_h + add) > MAX_CHUNK_HEIGHT:
                    chunks.append((cur, cur_f, cur_n))
                    cur = []
                    cur_f = set()
                    cur_n = 0
                    cur_h = 0
                    add = img.height
                cur.append(img)
                cur_f = cur_f | fset
                cur_n += n_boxes
                cur_h += add
            if cur:
                chunks.append((cur, cur_f, cur_n))

            # 4. Render + save each chunk; resolve its footer page set.
            chunk_descs = []
            for i, (imgs, fset, n_boxes) in enumerate(chunks):
                w = max(im.width for im in imgs)
                h = sum(im.height for im in imgs) + PADDING * (len(imgs) - 1)
                canvas = Image.new('RGB', (w, h), color='white')
                y = 0
                for im in imgs:
                    canvas.paste(im, (0, y))
                    y += im.height + PADDING
                out_path = os.path.join(TEMP_DIR, f"stitched_grid_{b_id}_chunk{i + 1}.png")
                canvas.save(out_path)
                footer_pages = None
                if fset and all(isinstance(f, int) and 0 < f < 999 for f in fset):
                    footer_pages = sorted(fset)
                # [PATCH:count-check] box_count rides on the descriptor so the
                # count check can compare reply items against boxes sent.
                chunk_descs.append({"path": out_path, "footer_pages": footer_pages,
                                    "box_count": n_boxes})

            self.log_message(f"    -> Stitched {len(snippet_paths)} snippets across {len(page_imgs)} page(s) "
                             f"into {len(chunk_descs)} chunk(s) (page-aligned, <= {MAX_CHUNK_HEIGHT}px tall).")
            # [PATCH:silo2-per-chunk-context] Dashboard: the context each chunk
            # will carry, decided here from the footer reads.
            for i, ch in enumerate(chunk_descs, 1):
                if ch["footer_pages"] is not None:
                    self.log_message(f"       chunk {i}: master page(s) {ch['footer_pages']} (footer-derived)")
                else:
                    self.log_message(f"       chunk {i}: footer page number unreadable -> FULL-master context for this chunk")
            return chunk_descs

        except Exception as e:
            self.log_message(f"[!] Stitching Error for {b_id}: {e}")
            return []

    # ==========================================
    # [PATCH:essay-mode] FULL_PAGE PIPELINE HELPERS
    # ==========================================
    def _ocr_essay_tag(self, page_img_path):
        """Locally OCRs the printed corner tag (white item number in a dark
        box, top-left of the response box) so pages can be grouped into
        essays WITHOUT an API call. Offline, like the footer page-number OCR.

        [PATCH:essay-tag-v2] The tag square sits TOUCHING the response-box
        border, so a plain threshold fuses tag + border into one giant blob
        and 'largest dark contour' selects the box corner, not the tag (the
        v1 failure: every page 'unreadable'). v2 therefore (a) applies a
        morphological OPEN wider than the border stroke, which erases thin
        border lines while the solid tag square survives, then (b) selects
        by tag GEOMETRY -- solid (extent), square-ish (aspect), sized -- and
        scores the best candidate rather than the biggest blob.

        Tunables (environment):
          LEDGER_ESSAY_TAG_REGION   x,y,w,h page fractions searched
                                    (default 0.00,0.05,0.30,0.22)
          LEDGER_ESSAY_TAG_OPEN_PX  opening kernel; must exceed the printed
                                    border stroke width in pixels
                                    (default 15; border is ~12 px at 300 DPI)
          LEDGER_ESSAY_TAG_DEBUG    '1' saves the OCR'd crop to TEMP_DIR as
                                    tagdebug_<page>.png for field diagnosis
        Returns an int, or None when no tag-like candidate reads as digits."""
        try:
            frac = os.environ.get("LEDGER_ESSAY_TAG_REGION", "0.00,0.05,0.30,0.22")
            fx, fy, fw, fh = [float(v) for v in frac.split(",")]
            img = cv2.imread(page_img_path)
            H, W = img.shape[:2]
            x0, y0 = int(W * fx), int(H * fy)
            x1, y1 = min(W, x0 + int(W * fw)), min(H, y0 + int(H * fh))
            region = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)

            _, thresh = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

            # (a) First CLOSE to fill the white digit's slits so the tag is a
            # solid square (otherwise the digit strokes can fragment it under
            # erosion), then OPEN wider than the border stroke: thin lines
            # (box border, ruled lines, most text) are erased while the
            # solid tag square survives erosion and is restored by dilation.
            open_px = int(os.environ.get("LEDGER_ESSAY_TAG_OPEN_PX", "15"))
            close_px = max(3, open_px // 2)
            solid = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE,
                                     np.ones((close_px, close_px), np.uint8))
            kernel = np.ones((open_px, open_px), np.uint8)
            opened = cv2.morphologyEx(solid, cv2.MORPH_OPEN, kernel)

            contours, _ = cv2.findContours(opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # (b) Geometry gate: the tag is a SOLID, roughly SQUARE mark of
            # modest size. Border remnants are long/thin (aspect/extent fail);
            # page-scale structures are too big (area cap).
            best = None
            best_score = 0.0
            region_area = float(region.shape[0] * region.shape[1])
            for c in contours:
                area = cv2.contourArea(c)
                if area < 500 or area > 0.10 * region_area:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                if w == 0 or h == 0:
                    continue
                aspect = w / float(h)
                if not (0.4 <= aspect <= 2.5):
                    continue
                extent = area / float(w * h)
                if extent < 0.55:
                    continue
                score = extent * area
                if score > best_score:
                    best_score = score
                    best = (x, y, w, h)
            if best is None:
                return None

            x, y, w, h = best
            # Re-crop the ORIGINAL grayscale (pre-opening), INSET a few px
            # INSIDE the tag square: sampling past the square's edge pulls in
            # a ring of white page that inverts into a black frame around the
            # digit, and tesseract reads frame edges as strokes (a '1' beside
            # a frame line reads as '4'). Strictly-inside sampling inverts to
            # a clean dark digit on a light field.
            inset = 6
            ry0, ry1 = y + inset, y + h - inset
            rx0, rx1 = x + inset, x + w - inset
            if (ry1 - ry0) < 10 or (rx1 - rx0) < 10:
                ry0, ry1, rx0, rx1 = y, y + h, x, x + w
            roi = region[ry0:ry1, rx0:rx1]
            roi_inv = cv2.bitwise_not(roi)
            # Tesseract is unreliable on tiny lone glyphs (a printed '1' can
            # read as '4'); upscale the crop 4x so the digit presents at a
            # comfortable size, and pad with a white margin.
            roi_inv = cv2.resize(roi_inv, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
            # Binarize so faded/half-toner print presents crisp strokes
            # instead of gray smears (an open-bowled '3' smears into an '8').
            _, roi_inv = cv2.threshold(roi_inv, 0, 255,
                                       cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            roi_inv = cv2.copyMakeBorder(roi_inv, 20, 20, 20, 20,
                                         cv2.BORDER_CONSTANT, value=255)
            if os.environ.get("LEDGER_ESSAY_TAG_DEBUG", "0") == "1":
                dbg = os.path.join(TEMP_DIR, f"tagdebug_{os.path.basename(page_img_path)}")
                cv2.imwrite(dbg, roi_inv)
            text = pytesseract.image_to_string(
                roi_inv, config='--psm 8 -c tessedit_char_whitelist=0123456789').strip()
            return int(text) if text else None
        except Exception as e:
            self.log_message(f"    -> [!] Essay tag OCR failed on {os.path.basename(page_img_path)}: {e}")
            return None

    def _resize_page_for_essay(self, page_img_path, b_id, seq):
        """Downscales one response page to the provider ingest ceiling
        (LEDGER_ESSAY_PAGE_MAX_EDGE, default 1568 px on the long edge --
        providers resize past this anyway, so shipping more is pure payload).
        Returns the temp path of the resized copy; pages already under the
        ceiling are copied through untouched."""
        max_edge = int(os.environ.get("LEDGER_ESSAY_PAGE_MAX_EDGE", "1568"))
        out_path = os.path.join(TEMP_DIR, f"essay_page_{b_id}_{seq:02d}.png")
        with Image.open(page_img_path) as img:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            long_edge = max(img.width, img.height)
            if max_edge > 0 and long_edge > max_edge:
                scale = max_edge / float(long_edge)
                img = img.resize((int(img.width * scale), int(img.height * scale)),
                                 Image.Resampling.LANCZOS)
            img.save(out_path)
        return out_path

    def assemble_essay_pages(self, full_pages, b_id):
        """[PATCH:essay-mode] The FULL_PAGE counterpart of
        stitch_snippets_to_grid. Takes the student's full_pages records
        (path + footer page number + locally-OCR'd essay tag), and returns a
        LIST of chunks where each chunk is a LIST of resized page paths --
        the shape evaluate_chunks_with_silo_2 already iterates, and the shape
        evaluate_grid_with_silo_2 turns into an images_b64 payload.

        Grouping rules:
          * Pages are ordered by footer page number (scan-order independent).
          * Consecutive pages sharing an essay tag are ONE essay group; a
            page whose tag could not be read INHERITS the previous page's
            essay (the continuation heuristic -- a second essay page is the
            most likely reason a fresh tag is absent/unreadable), and is
            logged so the teacher can verify.
          * Chunks are packed from WHOLE essay groups, never splitting a
            group, up to LEDGER_ESSAY_MAX_PAGES_PER_CALL pages per call
            (0 = unlimited: the entire packet travels as one call, the
            preferred setting on the Anthropic route)."""
        if not full_pages:
            return []
        try:
            max_pages = int(os.environ.get("LEDGER_ESSAY_MAX_PAGES_PER_CALL", "0"))

            ordered = sorted(full_pages, key=lambda p: p.get("lowest_q", 999))

            # [PATCH:essay-mode] A page whose footer number failed local OCR
            # carries the 999 sentinel and sorts LAST. In single-call mode
            # (budget 0) that is harmless -- the model reads the printed tags
            # and regroups. In CHUNKED mode a misplaced page can land in the
            # wrong call, separated from its essay, so say so loudly.
            unnumbered = [p for p in ordered if p.get("lowest_q", 999) >= 999]
            if unnumbered and max_pages > 0:
                self.log_message(
                    f"    -> [!] WARNING (ID {b_id}): {len(unnumbered)} page(s) have no "
                    f"readable footer page number and will sort LAST. With a per-call "
                    f"page budget active, a misplaced page can be graded apart from its "
                    f"essay. Verify this student's conspectus, or re-run with "
                    f"LEDGER_ESSAY_MAX_PAGES_PER_CALL=0.")

            # 1. Group pages into essays via the corner tag + continuation rule.
            groups = []          # list of lists of page dicts
            last_tag = None
            for pg in ordered:
                tag = pg.get("essay_num")
                if tag is None and groups:
                    # [PATCH:essay-tag-quiet] The continuation heuristic only
                    # steers CHUNKED packing; in single-call mode grouping is
                    # moot (one chunk regardless), so say nothing.
                    if max_pages > 0:
                        self.log_message(
                            f"    -> Page {pg.get('lowest_q', '?')} (ID {b_id}): no readable "
                            f"essay tag; treating as a CONTINUATION of essay {last_tag}.")
                    groups[-1].append(pg)
                    continue
                if tag is not None and groups and tag == last_tag:
                    groups[-1].append(pg)
                    continue
                groups.append([pg])
                last_tag = tag

            # 2. Resize every page once, preserving order.
            seq = 0
            sized_groups = []
            for g in groups:
                sized = []
                for pg in g:
                    seq += 1
                    sized.append(self._resize_page_for_essay(pg["path"], b_id, seq))
                sized_groups.append(sized)

            # 3. Pack whole essay groups into chunks under the page budget.
            chunks = []
            cur = []
            for g in sized_groups:
                if max_pages > 0 and cur and (len(cur) + len(g)) > max_pages:
                    chunks.append(cur)
                    cur = []
                cur.extend(g)
            if cur:
                chunks.append(cur)

            self.log_message(
                f"    -> Assembled {len(ordered)} page(s) into {len(groups)} essay group(s), "
                f"{len(chunks)} Silo 2 call(s) (essay-aligned"
                f"{', <= ' + str(max_pages) + ' pages/call' if max_pages > 0 else ', single call'}).")
            return chunks

        except Exception as e:
            self.log_message(f"[!] Essay assembly error for {b_id}: {e}")
            return []

    def read_footer_crop(self, footer_path, announce=True, scan_file=None, page_no=None):
        """[PATCH:footer-reader] WO-COS2 T3: THE single footer reader. The
        local footer OCR that lived inline in extract_with_silo_1, moved
        VERBATIM (same Otsu threshold, same largest-contour rule, same > 500
        area gate, same _read_boxed_digits(lo=1, hi=LEDGER_FOOTER_MAX_PAGE)).

        Returns (page_num, outcome, raw):
          page_num  the page number, or 999 when unreadable
          outcome   'ok' | 'no_square' | 'rejected' | 'error'
          raw       raw OCR text ('rejected'), exception text ('error'),
                    else None/str of the value

        announce=True logs failures, counts the attempt for the run summary
        and saves the failed crop to Footer-Rejects. A caller probing a crop
        (orientation check) passes announce=False and announces only the
        final result via _footer_announce, so each page is counted once.
        """
        # [PATCH:boxed-digit-ocr] was: boundingRect + bitwise_not, which
        # inverted the box rim into ink beside the digits (6/25 correct on
        # the Algebra 1 EOC sample; two silent misreads, 200 and 307).
        page_num = 999
        outcome = "no_square"
        raw = None
        try:
            footer_img = cv2.imread(footer_path)
            gray_f = cv2.cvtColor(footer_img, cv2.COLOR_BGR2GRAY)
            _, thresh_f = cv2.threshold(gray_f, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            contours, _ = cv2.findContours(thresh_f, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if contours:
                c = max(contours, key=cv2.contourArea)
                if cv2.contourArea(c) > 500:
                    hi = int(os.environ.get("LEDGER_FOOTER_MAX_PAGE", "99"))
                    # [PATCH:footer-cap] WO-BM3 T3: a footer can only name
                    # a page the MASTER has, so the accepted range is
                    # capped at the master's page count (set in
                    # initialize_silo_2_cache, before Pass 1 reads any
                    # footer). A read above the cap is rejected -> 999 ->
                    # the existing full-master fallback.
                    # LEDGER_FOOTER_CAP_MASTER=0 restores the env-only cap.
                    if os.environ.get("LEDGER_FOOTER_CAP_MASTER",
                                      "1") != "0":
                        _mpc = getattr(self, "_master_page_count", None)
                        if isinstance(_mpc, int) and _mpc > 0:
                            hi = min(hi, _mpc)
                    val, raw = _read_boxed_digits(gray_f, c, thresh_f, lo=1, hi=hi)
                    if val is None:
                        # A corrupt read is now REJECTED, not silently cast.
                        # int("307") used to sail straight through.
                        outcome = "rejected"
                    else:
                        page_num = val
                        outcome = "ok"
        except Exception as e:
            outcome = "error"
            raw = str(e)
        if announce:
            self._footer_announce(footer_path, page_num, outcome, raw,
                                  scan_file=scan_file, page_no=page_no)
        return page_num, outcome, raw

    def _footer_announce(self, footer_path, page_num, outcome, raw,
                         scan_file=None, page_no=None):
        """[PATCH:footer-reader] WO-COS2 T3: one announcement per page --
        counts the attempt for the run summary, logs a failure in the
        established wording, and saves the failed crop (exactly as the
        reader saw it) to Footer-Rejects. LEDGER_FOOTER_DEBUG=0 disables
        the saving only; logging stays."""
        self._footers_attempted = getattr(self, "_footers_attempted", 0) + 1
        if outcome == "ok":
            self._footers_read = getattr(self, "_footers_read", 0) + 1
            return
        if outcome == "rejected":
            self.log_message(
                f"    -> [!] Footer OCR rejected {raw!r} on "
                f"{os.path.basename(footer_path)} (page -> 999)")
        elif outcome == "no_square":
            self.log_message(
                f"    -> [!] Footer OCR found no page-number square on "
                f"{os.path.basename(footer_path)} (page -> 999)")
        elif outcome == "error":
            self.log_message(f"    -> [!] Local Page Number OCR Failed: {raw}")
        if os.environ.get("LEDGER_FOOTER_DEBUG", "1") == "0":
            return
        try:
            os.makedirs(FOOTER_REJECTS_DIR, exist_ok=True)
            label_scan = scan_file if scan_file else os.path.basename(footer_path)
            label_page = page_no if page_no is not None else "X"
            dest = os.path.join(
                FOOTER_REJECTS_DIR, f"{label_scan}-page{label_page}-{outcome}.png")
            if os.path.exists(footer_path):
                shutil.copy(footer_path, dest)
        except Exception as e:
            self.log_message(f"    -> [!] Could not save footer reject crop: {e}")

    def _reset_pass1_run_state(self):
        """[PATCH:pass1-pacing] WO-COS2 T5: per-run Pass 1 pacing/re-read
        state, reset at the start of every run (called from start_ledger
        with the other per-run resets)."""
        self._pass1_slow_mode = False
        self._pass1_id_reread_accepted = []
        # [PATCH:box-rejects] WO-BM1 T5: per-run reject-saving tallies
        self._box_rejects_saved = 0
        self._id_rejects_saved = 0
        # [PATCH:run-summary] WO-COS2 T6: raw Pass 1 tallies (the
        # _box_map_notices pattern) + the printed-once summary state.
        self._pass1_ran = False
        self._run_summary_stats = None
        self._run_stats = {"pages_scanned": 0, "blank_pages": 0,
                           "pages_sorted": 0, "pages_per_student": {},
                           "discards": [], "read_errors": []}

    def _collect_run_summary_stats(self):
        """[PATCH:run-summary] WO-COS2 T6: one JSON-safe snapshot of the
        Pass 1 tallies, for the printer and for the batch job record (it
        must survive a shutdown and RESUME). Master page count comes from
        its own attribute set when the master is rendered -- NOT from
        s2ctx_master_files, which is emptied when per-chunk context is off."""
        raw = getattr(self, "_run_stats", None) or {}
        students = []
        roster = getattr(self, "roster", {}) or {}
        for b_id in sorted(raw.get("pages_per_student", {}),
                           key=lambda k: str(k)):
            info = roster.get(b_id, {})
            name = f"{info.get('last', 'Unknown')}, {info.get('first', 'Student')}"
            students.append({"id": b_id, "name": name,
                             "pages": raw["pages_per_student"][b_id]})
        return {
            "ran": bool(getattr(self, "_pass1_ran", False)),
            "pages_scanned": raw.get("pages_scanned", 0),
            "blank_pages": raw.get("blank_pages", 0),
            "pages_sorted": raw.get("pages_sorted", 0),
            "students": students,
            "discards": list(raw.get("discards", [])),
            "read_errors": list(raw.get("read_errors", [])),
            "rereads": list(getattr(self, "_pass1_id_reread_accepted", [])),
            "turned_upright": getattr(self, "_pages_turned_upright", 0),
            "sideways": getattr(self, "_sideways_pages", 0),
            "footers_read": getattr(self, "_footers_read", 0),
            "footers_attempted": getattr(self, "_footers_attempted", 0),
            "footer_saving": os.environ.get("LEDGER_FOOTER_DEBUG", "1") != "0",
            "master_pages": getattr(self, "_master_page_count", None),
            "grading_mode": getattr(self, "grading_mode", "boxed"),
        }

    def _pass1_pause(self):
        """[PATCH:pass1-pacing] WO-COS2 T5: the per-page Pass 1 pause.
        LEDGER_PASS1_PACING_S seconds (default 3) -- setting it to 30
        restores today's timing. After any Silo 1 capacity error this run,
        LEDGER_PASS1_SLOW_PACING_S seconds (default 30) for the rest of the
        run. Sleeps in 1 s slices so STOP stays responsive."""
        try:
            pace = float(os.environ.get("LEDGER_PASS1_PACING_S", "3"))
        except (TypeError, ValueError):
            pace = 3.0
        if getattr(self, "_pass1_slow_mode", False):
            try:
                pace = float(os.environ.get("LEDGER_PASS1_SLOW_PACING_S", "30"))
            except (TypeError, ValueError):
                pace = 30.0
        slept = 0.0
        while slept < pace and self.is_running:
            time.sleep(min(1.0, pace - slept))
            slept += 1.0

    def _pass1_note_capacity_error(self, err):
        """[PATCH:pass1-pacing] WO-COS2 T5: a Silo 1 capacity error flips
        this run into slow pacing, announced ONCE."""
        if not pass1_capacity_error(str(err).lower()):
            return
        if getattr(self, "_pass1_slow_mode", False):
            return
        self._pass1_slow_mode = True
        try:
            slow = int(float(os.environ.get("LEDGER_PASS1_SLOW_PACING_S", "30")))
        except (TypeError, ValueError):
            slow = 30
        self.log_message(
            f"High traffic reported by the provider. Pass 1 will pause "
            f"{slow} seconds between pages for the rest of this run.")

    def _pass1_resolve_id(self, silo_data, temp_id_path, temp_footer_path,
                          footer_result, pdf_file, page_display):
        """[PATCH:id-reask] WO-COS2 T5: ONE re-read of an off-roster ID
        (same crop, same prompt, after the normal pause). Returns
        (silo_data, b_id) when a read is on the roster, else (None, None)
        -> the caller discards the page. LEDGER_ID_REASK=0 restores the
        immediate discard exactly."""
        b_id = str(silo_data.get("bartonian_id", 0))
        if b_id in self.roster:
            return silo_data, b_id
        if os.environ.get("LEDGER_ID_REASK", "1") == "0":
            self.log_message(f"  -> [!] Invalid/missing ID ({b_id}). Discarding page.")
            self._run_stats_discard(pdf_file, page_display, b_id, None)
            # [PATCH:box-rejects] WO-BM1 T5: keep the discarded page's ID crop
            self._id_save_reject(temp_id_path, pdf_file, page_display, b_id)
            return None, None
        self._pass1_pause()
        silo_data2 = self.extract_with_silo_1(
            temp_id_path, temp_footer_path, footer_result=footer_result)
        b_id2 = str(silo_data2.get("bartonian_id", 0)) if silo_data2 else "0"
        if silo_data2 and b_id2 in self.roster:
            self.log_message(
                f"  -> ID re-read on page {page_display} of {pdf_file}: "
                f"first read {b_id} is not on the roster; second read {b_id2} "
                f"accepted. Please verify.")
            if not hasattr(self, "_pass1_id_reread_accepted"):
                self._pass1_id_reread_accepted = []
            self._pass1_id_reread_accepted.append(
                {"file": pdf_file, "page": page_display,
                 "first": b_id, "second": b_id2})
            return silo_data2, b_id2
        self.log_message(
            f"  -> [!] Invalid/missing ID (read {b_id}, re-read {b_id2}). "
            f"Discarding page {page_display} of {pdf_file}.")
        self._run_stats_discard(pdf_file, page_display, b_id, b_id2)
        # [PATCH:box-rejects] WO-BM1 T5: keep the discarded page's ID crop
        # (named by the FIRST read; the re-read value is in the log and the
        # run summary)
        self._id_save_reject(temp_id_path, pdf_file, page_display, b_id)
        return None, None

    def _run_stats_discard(self, pdf_file, page_display, first, second):
        """[PATCH:run-summary] WO-COS2 T6: record a discarded page for the
        end-of-run summary (second is None when the re-read was disabled)."""
        raw = getattr(self, "_run_stats", None)
        if raw is not None:
            raw.setdefault("discards", []).append(
                {"file": pdf_file, "page": page_display,
                 "first": first, "second": second})

    def _auto_orient_page(self, temp_full_img_path, img_for_crop, scan_file,
                          page_no, total_pages):
        """[PATCH:auto-orient] WO-COS2 T4: local 180-degree upside-down
        correction, Pass 1 only, zero API calls. Returns
        (img_for_crop, footer_result):

          footer_result  (page_num, outcome, raw) when this method already
                         read the footer (announced once, so the page is
                         counted once); None when the caller must crop and
                         read the footer itself (kill switch off-state or a
                         sideways page -- today's flow exactly).

        Upright page: the footer (calibrated crop_footer window) reads a
        valid page number on the first try; the page file on disk is NOT
        rewritten. Unreadable: the full page is rotated 180 in memory and
        the same window re-read; a valid read means the page was upside
        down -- the temp PNG is overwritten so EVERYTHING downstream (ID
        crop, footer, box finder, box map, essay pages, conspectus scan
        pages) uses the upright page. Neither read: left exactly as
        scanned (the T3 footer failure line is the only alarm). Sideways
        (wider than tall): left as scanned, no rotation attempt.
        Kill switch: LEDGER_AUTO_ORIENT=0 skips all of this."""
        if os.environ.get("LEDGER_AUTO_ORIENT", "1") == "0":
            return img_for_crop, None
        h_img, w_img = img_for_crop.shape[:2]
        if w_img > h_img:
            self.log_message(
                f"  -> Page {page_no}/{total_pages} is sideways; left as scanned.")
            self._sideways_pages = getattr(self, "_sideways_pages", 0) + 1
            return img_for_crop, None
        f_coords = self.get_crop_coordinates('crop_footer')
        safe_scan_name = os.path.basename(temp_full_img_path).replace(
            "-temp-full-page-", "-temp-footer-")
        probe_path = os.path.join(os.path.dirname(temp_full_img_path), safe_scan_name)
        as_scanned_crop = img_for_crop[f_coords[1]:f_coords[1] + f_coords[3],
                                       f_coords[0]:f_coords[0] + f_coords[2]]
        cv2.imwrite(probe_path, as_scanned_crop)
        res_upright = self.read_footer_crop(probe_path, announce=False)
        if res_upright[1] == "ok":
            # Upright: page file untouched (byte-identical). Count the read.
            self._footer_announce(probe_path, *res_upright,
                                  scan_file=scan_file, page_no=page_no)
            return img_for_crop, res_upright
        rotated = cv2.rotate(img_for_crop, cv2.ROTATE_180)
        rotated_crop = rotated[f_coords[1]:f_coords[1] + f_coords[3],
                               f_coords[0]:f_coords[0] + f_coords[2]]
        cv2.imwrite(probe_path, rotated_crop)
        res_rotated = self.read_footer_crop(probe_path, announce=False)
        if res_rotated[1] == "ok":
            cv2.imwrite(temp_full_img_path, rotated)
            self.log_message(
                f"  -> Page {page_no}/{total_pages} was upside down; "
                f"turned upright locally.")
            self._pages_turned_upright = getattr(self, "_pages_turned_upright", 0) + 1
            self._footer_announce(probe_path, *res_rotated,
                                  scan_file=scan_file, page_no=page_no)
            return rotated, res_rotated
        # Neither read: leave the page exactly as scanned; restore the
        # as-scanned footer crop on disk and announce ITS failure (one
        # footer line, one reject crop, exactly the page as the run sees it).
        cv2.imwrite(probe_path, as_scanned_crop)
        self._footer_announce(probe_path, *res_upright,
                              scan_file=scan_file, page_no=page_no)
        return img_for_crop, res_upright

    def extract_with_silo_1(self, header_path, footer_path, footer_result=None):
        # --- LOCAL OFFLINE OCR (FOOTER / PAGE NUMBER) ---
        # [PATCH:footer-reader] WO-COS2 T3: the OCR itself lives in
        # read_footer_crop. A caller that already read this crop passes the
        # result in and the footer is never OCR'd twice; absent, this
        # behaves exactly as before.
        if footer_result is None:
            footer_result = self.read_footer_crop(footer_path)
        page_num = footer_result[0]

        # --- GEMINI API (BARTONIAN ID ONLY) ---
        delay = 15
        while self.is_running:
            try:
                # The image is already perfectly cropped by the dynamic coordinates
                base64_header = self.image_to_base64_png(header_path)
                
                prompt = (
                    "You are receiving a cropped image showing the top-right corner of an assignment containing a Bartonian ID.\n"
                    "Return ONLY a valid, flat JSON object using EXACTLY this template: "
                    '{"bartonian_id": 125} \n\n'
                    "CRITICAL OCR RULES FOR BARTONIAN ID:\n"
                    "1. The Bartonian ID is always a continuous integer. Output it as a single integer (0 if missing).\n"
                    "2. VISION HEURISTIC: Students often write the digit '1' as a simple vertical stroke. "
                    "Therefore, any character in the ID field that resembles a vertical bar (|), a slash (/), "
                    "the letter 'l', or the letter 'I' MUST be interpreted as an integer.\n"
                    "3. FUSION MANDATE: Do not treat vertical marks as delimiters or separators. You must fuse "
                    "them into the resulting integer. If you see '1 | 25', '| 25', or '1 25', the "
                    "output MUST be 125. Never discard a leading or middle vertical stroke.\n"
                    "4. Strip any non-numeric symbols that are clearly not digits.\n"
                    "5. Bartonian IDs are always three-digit numbers between 101-799."
                )
                
                payload = {
                    "model_name": self.get_silo2_model(),
                    "prompt": prompt,
                    "image_b64": base64_header
                }
                
                response = requests.post(DAEMON_BASE + "/api/silo1", json=payload, timeout=60)
                self._raise_daemon_error(response)
                data = response.json()
                
                if data.get("status") == "success":
                    raw = data["text"].strip().replace('```json', '').replace('```', '').strip()
                    silo_data = json_repair.loads(raw)
                    silo_data["page_num"] = page_num
                    return silo_data
                else:
                    raise Exception(data.get("message", "Daemon returned an error."))
                
            except Exception as e:
                # [PATCH:pass1-pacing] WO-COS2 T5: a capacity error flips the
                # run to slow pacing (flag + one log line). The retry loop
                # itself is unchanged -- it still retries until it succeeds,
                # which is what guarantees a 429 never costs a page.
                self._pass1_note_capacity_error(e)
                logging.info(f"[!] Silo 1 Error. Retrying in {delay} seconds... Details: {e}")
                friendly_err = self._translate_api_error(e)
                self.root.after(0, self._append_to_gui, f"Silo 1 Status: {friendly_err} (Retrying in {delay}s)")
                time.sleep(delay)
                delay = min(delay + 15, 300)
        return None
   
    def _s2ctx_master_images_b64(self, footer_pages):
        """[PATCH:silo2-per-chunk-context] Resolve a chunk's footer-derived
        page numbers into base64 payloads of the locally rendered master
        pages. Returns a list for the daemon's 'master_images_b64' key, or
        None -> the caller omits the key and the daemon grades against the
        FULL cached master exactly as before (fail-safe: an unreadable or
        implausible footer may cost weight, never correctness).

        Env knobs (GUI-side -- LAUNCHER environment ONLY, per the Rev-fff
        placement split; a value in /etc/paragon/.env silently does nothing):
          LEDGER_S2CTX_ENABLE        "1" (default) | "0" = full-master A-arm
          LEDGER_S2CTX_PAGE_OFFSET   int, default "0": master idx = footer-1+offset
          LEDGER_S2CTX_ADJACENCY     int, default "0": attach +/- N neighbors
          LEDGER_S2CTX_INCLUDE_PAGES default "1": comma list of 1-based master
                                     pages ALWAYS attached (cover/instructions
                                     -- adjacency policy (c), Rev hhh / #79)
        """
        try:
            if os.environ.get("LEDGER_S2CTX_ENABLE", "1") != "1":
                return None
            masters = getattr(self, "s2ctx_master_files", None)
            if not masters or not footer_pages:
                return None
            n = len(masters)
            offset = int(os.environ.get("LEDGER_S2CTX_PAGE_OFFSET", "0"))
            radius = int(os.environ.get("LEDGER_S2CTX_ADJACENCY", "0"))
            include = os.environ.get("LEDGER_S2CTX_INCLUDE_PAGES", "1")
            idxs = set()
            for fp in footer_pages:
                base = int(fp) - 1 + offset
                if base < 0 or base >= n:
                    self.log_message(f"    -> [S2CTX] footer page {fp} resolves outside the "
                                     f"{n}-page master -> FULL-master context for this chunk.")
                    return None
                for d in range(-radius, radius + 1):
                    j = base + d
                    if 0 <= j < n:
                        idxs.add(j)
            for tok in include.split(","):
                tok = tok.strip()
                if tok:
                    j = int(tok) - 1
                    if 0 <= j < n:
                        idxs.add(j)
            if not idxs:
                return None
            # [PATCH:silo2-per-chunk-context v2] SMALL-FORM SHORT-CIRCUIT: if the
            # resolved set covers the WHOLE master (the normal case for 2-3 page
            # worksheets once the include page is added), inlining it per call is
            # strictly worse than the cached path -- identical weight, no cache
            # discount. Return None -> the daemon grades against the full cached
            # master, byte-path identical to the pre-#79 pipeline. Per-chunk
            # context therefore engages only where it pays: multi-page masters
            # whose chunks cover a SUBSET of the pages.
            if len(idxs) >= n:
                return None
            cache = getattr(self, "_s2ctx_b64_cache", None)
            if cache is None:
                cache = self._s2ctx_b64_cache = {}
            out = []
            for j in sorted(idxs):
                if j not in cache:
                    cache[j] = self.image_to_base64_png(masters[j])
                out.append(cache[j])
            return out
        except Exception as e:
            self.log_message(f"    -> [S2CTX] context resolution failed ({e}) -> FULL-master context for this chunk.")
            return None

    def evaluate_chunks_with_silo_2(self, chunk_paths):
        """[PATCH:silo2-chunking] Grades each grid chunk via Silo 2 and merges the
        JSON arrays into one evaluation. Returns the merged list, or None if ANY
        chunk defers (capacity/Stop) -- a partial grade would silently under-score
        a student, so one deferred chunk defers the whole student (scan retained,
        re-run recovers). A small inter-chunk pace avoids self-inflicted 429s."""
        if not chunk_paths:
            return []
        pace = float(os.environ.get("LEDGER_SILO2_CHUNK_PACING_S", "60"))  # [PATCH:silo2-pacing-60] 8->60; metering not weather; env override still wins
        merged = []
        total = len(chunk_paths)
        for idx, cp in enumerate(chunk_paths, 1):
            if not self.is_running:
                return None
            # [PATCH:silo2-per-chunk-context] Dashboard mirrors the context this
            # chunk will carry: its own master page(s), or the FULL master when a
            # footer read was unreadable (fail-safe) or the feature is off.
            ctx_label = ""
            if isinstance(cp, dict):
                _fp = cp.get("footer_pages")
                if _fp and os.environ.get("LEDGER_S2CTX_ENABLE", "1") == "1" and getattr(self, "s2ctx_master_files", None):
                    ctx_label = f" [master pp. {','.join(str(x) for x in _fp)} + incl.]"
                else:
                    ctx_label = " [FULL master]"
            if total > 1:
                self.root.after(0, self._append_to_gui, f"Silo 2: grading chunk {idx} of {total}{ctx_label}...")
            if idx > 1 and pace > 0:
                # [PATCH:silo2-pacing-60] Sleep in 1s slices so the Stop button
                # stays responsive (same convention as the retry loop below).
                # Stop during pacing defers the student, per contract.
                slept = 0.0
                while slept < pace and self.is_running:
                    time.sleep(min(1.0, pace - slept))
                    slept += 1.0
                if not self.is_running:
                    return None
            result = self.evaluate_grid_with_silo_2(cp)
            if result is None:
                return None  # capacity-deferred or Stop -> defer the whole student
            # [PATCH:count-check] boxed chunks (dict descriptors) get the box
            # count verified before the merge; None defers the whole student,
            # exactly like a failed grading call. Essay mode never produces a
            # dict descriptor, so it is untouched.
            if isinstance(result, list) and isinstance(cp, dict):
                result = self._count_check_chunk(cp, result)
                if result is None:
                    return None
            if isinstance(result, list):
                merged.extend(result)
        return merged

    def _count_check_chunk(self, chunk_desc, items):
        """[PATCH:count-check] WO-B1: compare the number of items a chunk's
        reply returned with the number of answer boxes sent.

          * equal -> pass; more -> accept with one log line (not a drop).
          * fewer -> either a dropped question or a deliberate omission
            (lab-sheet data boxes the instructions say not to score). The run
            works it out: a short set already learned for these pages passes;
            otherwise re-ask live with the identical request (up to
            LEDGER_COUNT_CHECK_REASKS, default 2). A complete reply, or two
            replies agreeing on the same item set, is accepted; two agreeing
            short replies are remembered for these pages for the rest of the
            run. No agreement and no complete reply -> return None, which
            defers the student through the existing path. A score with a
            silently dropped question is never committed.

        LEDGER_COUNT_CHECK=0 in the launcher environment disables the check.
        Chunks whose printed page numbers could not be read still get the
        re-ask logic, but nothing is learned from them. A learned pattern is
        never replaced by a smaller one; a later student whose two replies
        agree on fewer items is accepted for that student only."""
        if os.environ.get("LEDGER_COUNT_CHECK", "1") == "0":
            return items
        boxes = chunk_desc.get("box_count") if isinstance(chunk_desc, dict) else None
        if not boxes:
            return items  # nothing recorded to check against
        learned = getattr(self, "_count_check_learned", None)
        if learned is None:
            learned = self._count_check_learned = {}
        fp = chunk_desc.get("footer_pages")
        pages_key = tuple(fp) if fp else None
        label = count_check_pages_label(fp)
        try:
            reasks = int(os.environ.get("LEDGER_COUNT_CHECK_REASKS", "2"))
        except (TypeError, ValueError):
            reasks = 2

        reply = items
        seen_sets = []
        attempt = 0
        while True:
            nums = count_check_item_list(reply)
            n = len(nums)
            s = frozenset(nums)
            if len(s) < n:
                # Duplicates within one reply: warning only, never a failure.
                dups = sorted({x for x in nums if nums.count(x) > 1})
                self.log_message(f"    -> [COUNT CHECK] WARNING ({label}): "
                                 f"duplicate item number(s) in one reply: {dups}.")
            if n == boxes:
                if attempt:
                    self.log_message(f"    -> [COUNT CHECK] {label}: complete reply "
                                     f"({n} of {boxes} boxes) on re-ask - accepted.")
                return reply
            if n > boxes:
                self.log_message(f"    -> [COUNT CHECK] {label}: reply returned {n} "
                                 f"items for {boxes} boxes - accepted (not a drop).")
                return reply
            # Fewer items than boxes.
            if pages_key is not None and learned.get(pages_key) == s:
                self.log_message(f"    -> [COUNT CHECK] {label}: short reply matches "
                                 f"the pattern learned for these pages - accepted.")
                return reply
            if s in seen_sets:
                missing = boxes - n
                if pages_key is not None:
                    prev = learned.get(pages_key)
                    if prev is None or len(s) >= len(prev):
                        learned[pages_key] = s
                        self.log_message(f"{label}: {missing} box(es) not scored on "
                                         f"two tries; treated as data boxes. Please verify.")
                    else:
                        self.log_message(f"    -> [COUNT CHECK] {label}: two replies "
                                         f"agree on fewer items than the learned pattern - "
                                         f"accepted for this student only.")
                else:
                    self.log_message(f"    -> [COUNT CHECK] {label}: two replies agree "
                                     f"on the same short set - accepted (pages unreadable, "
                                     f"nothing learned).")
                return reply
            seen_sets.append(s)
            if attempt >= reasks:
                self.log_message(f"    -> [COUNT CHECK] {label}: reply covered {n} of "
                                 f"{boxes} boxes and re-asks are exhausted - this student "
                                 f"will be deferred, never scored short.")
                return None
            attempt += 1
            self.log_message(f"    -> [COUNT CHECK] {label}: reply covered {n} of "
                             f"{boxes} boxes - asking again ({attempt} of {reasks}).")
            reply = self.evaluate_grid_with_silo_2(chunk_desc)
            if reply is None:
                return None
            if not isinstance(reply, list):
                reply = []

    def _parse_silo2_text(self, text):
        """[PATCH:batch] The ONE parse choke point for a Silo 2 reply text,
        shared by the live path and batch collection: strip ```json fences,
        json_repair-parse, then normalize the literal \\n\\n sequence.

        [PATCH:literal-newline-normalize] Some models (first seen:
        claude-opus-4-8) obey the prompt's backslash-doubling rule so
        literally that paragraph breaks arrive as the LITERAL 4-char
        sequence \\n\\n instead of a real blank line. Downstream, the
        paragraph splitter (re.split on real blank lines) then finds
        no paragraphs, and TeX tokenizes '\\n\\nThe' as undefined
        control words -- eating the break AND the first word of the
        next paragraph. Normalize ONLY the doubled sequence here, at
        the single parse choke point, so the DB, Tabula, and report
        all store clean text. Real newlines (Gemini's output) contain
        no literal \\n\\n, so this is a byte-level no-op on the Google
        route; no legitimate LaTeX in the loaded packages can produce
        the sequence either (no macro named \\n exists)."""
        raw = text.strip().replace('```json', '').replace('```', '').strip()
        parsed = json_repair.loads(raw)
        if isinstance(parsed, list):
            for _it in parsed:
                if isinstance(_it, dict) and isinstance(_it.get("student_answer"), str):
                    _it["student_answer"] = _it["student_answer"].replace("\\n\\n", "\n\n")
        return parsed

    def _batch_capability_check(self, model_name):
        """[PATCH:batch] Can the chosen model's route submit a batch through
        this daemon? Returns (ok, note). An older daemon (no batch_endpoints
        flag) or an unsupported route gives a clear refusal, never a crash
        and never a silent fallback to live."""
        try:
            health = requests.get(DAEMON_BASE + "/", timeout=10).json()
        except Exception as e:
            return False, f"The scoring daemon is not reachable ({e})."
        if not health.get("batch_endpoints"):
            return False, ("This machine's scoring daemon predates batch "
                           "scoring (needs v2.11). Update and restart "
                           "ledger-daemon.service.")
        m = (model_name or "").lower()
        route = ("anthropic" if (m.startswith("claude") or "anthropic/" in m
                                 or m.startswith("publishers/anthropic"))
                 else "google")
        cap = (health.get("batch") or {}).get(route) or {}
        if not cap.get("supported"):
            return False, (cap.get("reason")
                           or f"The {route} route cannot submit batches here.")
        return True, cap.get("reason")

    def evaluate_grid_with_silo_2(self, grid_image_path):
        """Sends ONLY the stitched grid. Context is pulled from daemon cache.

        [PATCH:silo2-bounded-retry] Bounded-retry / defer-and-continue contract:
          * returns a list  -> a real evaluation (possibly empty if the model
                               genuinely returned no items).
          * returns None    -> bounded retries were exhausted against Google
                               capacity (HTTP 429 / RESOURCE_EXHAUSTED / 503),
                               OR the user pressed Stop. The CALLER must treat
                               None as 'deferred': do NOT write a grade and do
                               NOT build a conspectus.

        A 429 returns fast and healthy, so the daemon watchdog never fires on
        it; the only thing that can turn a transient 429 into an unbounded hang
        is an unbounded retry loop. This loop is therefore bounded, has full
        jitter (so multiple field units do not resync into a thundering herd),
        and backs off long on capacity (429/503) but fast on transport (5xx).
        """
        # [PATCH:silo2-per-chunk-context] Boxed chunks now arrive as dicts
        # ({path, footer_pages}); essay mode still passes a LIST of page paths
        # and legacy callers a bare path -- both byte-path identical to before.
        master_ctx_b64 = None
        if isinstance(grid_image_path, dict):
            master_ctx_b64 = self._s2ctx_master_images_b64(grid_image_path.get("footer_pages"))
            grid_image_path = grid_image_path["path"]
        MAX_ATTEMPTS = int(os.environ.get("LEDGER_SILO2_MAX_ATTEMPTS", "10"))
        BASE_BACKOFF = 15.0   # seconds; capacity (429/503) exponential base
        MAX_BACKOFF  = 300.0  # seconds; ceiling on any single wait
        FAST_BACKOFF = 5.0    # seconds; transport/5xx is not capacity -> retry fast

        attempt = 0
        while self.is_running and attempt < MAX_ATTEMPTS:
            attempt += 1
            try:
                # [PATCH:essay-mode] grid_image_path is a single path (boxed
                # grid -> legacy image_b64 payload) or a LIST of page paths
                # (essay mode -> images_b64 payload, daemon v2.3+). Legacy
                # callers are byte-path identical.
                if isinstance(grid_image_path, (list, tuple)):
                    image_payload = {"images_b64": [self.image_to_base64_png(p) for p in grid_image_path]}
                else:
                    image_payload = {"image_b64": self.image_to_base64_png(grid_image_path)}

                # [PATCH:batch] prompt line and schema moved VERBATIM to the
                # module constants SILO2_PROMPT / EVALUATION_SCHEMA so the
                # batch path sends the identical model-facing text.
                payload = {
                    "prompt": SILO2_PROMPT,
                    "schema": EVALUATION_SCHEMA
                }
                payload.update(image_payload)
                # [PATCH:silo2-per-chunk-context] attach this chunk's own master
                # page(s); when the key is absent the daemon grades against the
                # FULL cached master exactly as before.
                if master_ctx_b64:
                    payload["master_images_b64"] = master_ctx_b64

                response = requests.post(DAEMON_BASE + "/api/silo2", json=payload, timeout=(10, float(os.environ.get("LEDGER_SILO2_CLIENT_TIMEOUT_S", "330"))))  # [PATCH:client-timeout-coherence] read must OUTWAIT the daemon's 300s upstream budget; 2026-07-08 journal: real Fable 5 essay grades ran 121-151s and were discarded at the old 120s hang-up
                self._raise_daemon_error(response)
                data = response.json()

                if data.get("status") == "success":
                    # [PATCH:batch] fence-stripping, json_repair and the
                    # literal-\n\n normalization live in _parse_silo2_text so
                    # batch collection parses results IDENTICALLY.
                    return self._parse_silo2_text(data["text"])
                else:
                    raise Exception(data.get("message"))

            except Exception as e:
                err_str = str(e).lower()
                is_capacity = ("resource exhausted" in err_str or "429" in err_str
                               or "503" in err_str or "service unavailable" in err_str)

                # [PATCH:permanent-error-fast-defer] Deterministic 4xx failures
                # (payload over the provider cap, malformed request, unknown
                # model, auth) return the SAME answer on every retry. Burning
                # six attempts on them wasted ~2 min/student, and the old
                # hardcoded deferral text blamed "Google capacity (429)" for
                # errors that were neither Google nor capacity. Defer at once
                # and echo the REAL reason into the on-screen log.
                is_permanent = (not is_capacity and
                                ("api key not valid" in err_str          # [PATCH:wizard-devapi]
                                 or "api_key_invalid" in err_str
                                 or "413" in err_str or "request_too_large" in err_str
                                 or "prompt is too long" in err_str
                                 or "invalid_request" in err_str
                                 or "not_found" in err_str
                                 or "permission denied" in err_str
                                 or "authentication" in err_str
                                 or "400" in err_str or "404" in err_str
                                 or "401" in err_str or "403" in err_str))

                # Retries used up, or provably unretriable -> DEFER (return None).
                if attempt >= MAX_ATTEMPTS or is_permanent:
                    logging.info(f"[!] Silo 2 deferring student (attempt {attempt}/{MAX_ATTEMPTS}, permanent={is_permanent}). Details: {e}")
                    reason = ("unretriable provider error" if is_permanent
                              else f"error persisted after {MAX_ATTEMPTS} tries")
                    self.root.after(0, self._append_to_gui,
                                    f"Silo 2 Status: {reason} - student deferred for re-run.")
                    self.root.after(0, self._append_to_gui,
                                    f"Silo 2 Detail: {str(e)[:300]}")
                    return None

                # Capacity backs off exponentially (long); transport retries fast.
                # Full jitter on the low side keeps most of the intended backoff
                # while de-correlating simultaneous units.
                if is_capacity:
                    wait = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
                else:
                    wait = FAST_BACKOFF
                wait = random.uniform(wait * 0.5, wait)

                logging.info(f"[!] Silo 2 Error (attempt {attempt}/{MAX_ATTEMPTS}, retry in {wait:.0f}s). Details: {e}")
                friendly_err = self._translate_api_error(e)
                self.root.after(0, self._append_to_gui,
                                f"Silo 2 Status: {friendly_err} (Attempt {attempt}/{MAX_ATTEMPTS}, retrying in {wait:.0f}s)")

                # Sleep in 1s slices so the Stop button stays responsive.
                slept = 0.0
                while slept < wait and self.is_running:
                    time.sleep(min(1.0, wait - slept))
                    slept += 1.0

        # is_running went False (user pressed Stop) before any success.
        return None

    def get_period_folder(self, b_id):
        b_id_str = str(b_id).strip()
        if not b_id_str or b_id_str == "0" or len(b_id_str) < 1: 
            return "Unknown-Student"
        first_char = b_id_str[0]
        if first_char in ['1', '2', '3', '4', '5', '6', '7']:
            return f"{first_char}st Period" if first_char == '1' else (f"{first_char}nd Period" if first_char == '2' else (f"{first_char}rd Period" if first_char == '3' else f"{first_char}th Period"))
        return "Unknown-Student"

    def process_assignment_names(self, title):
        internal_group_key = re.sub(r'[^a-zA-Z0-9]', '', title).lower()
        display_name = re.sub(r'[^a-zA-Z0-9]', '-', title)
        display_name = re.sub(r'-+', '-', display_name).strip('-')
        return internal_group_key, display_name if display_name else "Unnamed-Assignment"

    def generate_latex_conspectus(self, b_id, student_data, assignment_title, score, max_score, evaluation_data, original_images, output_dir, is_final_attempt=True):
        
        # --- LaTeX variable sanitization ---
        safe_tex_title = str(assignment_title).replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
        
        student_name = f"{student_data['first']} {student_data['last']}"
        safe_tex_name = student_name.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
        
        tex_filename = f"{b_id}-{student_data['last']}-{student_data['first']}-Conspectus.tex"
        tex_path = os.path.join(output_dir, tex_filename)
        
        tex_content = f"\\documentclass[11pt]{{article}}\n"
        tex_content += f"\\usepackage{{fontspec}}\n"
        tex_content += f"\\setmainfont{{Liberation Serif}}\n"
        tex_content += f"\\usepackage[margin=0.85in]{{geometry}}\n"
        tex_content += f"\\usepackage{{graphicx}}\n"
        tex_content += f"\\usepackage{{eso-pic}}\n"
        tex_content += f"\\usepackage[version=4]{{mhchem}}\n"
        tex_content += f"\\usepackage{{amsmath}}\n"
        tex_content += f"\\usepackage{{amssymb}}\n" # SURGICAL INSERTION: Advanced Math Symbols 
        tex_content += f"\\usepackage{{xcolor}}\n"
        tex_content += f"\\usepackage{{environ}}\n"
        tex_content += f"\\usepackage{{needspace}}\n\n"

        # [PATCH:overflow-wrapper] (WO-A4) An unbreakable minipage taller than
        # the page body (~650pt letter) runs off the paper: xelatex exits 0,
        # the feedback tail is lost (repro: tests/fixtures/overflow_repro/).
        # conspectusitem measures the item once; if it fits one page it emits
        # the SAME minipage box that was measured (identical metrics, identical
        # page breaks), and only a too-tall item is re-emitted as plain
        # breakable text. INVARIANT: item bodies must stay side-effect-free
        # (no \label, \footnote or counter steps) -- a too-tall \BODY is
        # typeset twice, once to measure and once to emit.
        # [PATCH:overflow-stickiness] The breakable rendering shares \BODY with
        # the fitting path, so its label/status lines cannot carry inline
        # stickiness macros. Equivalents, applied ONLY in the breakable branch:
        #   * label line = first line of the \BODY paragraph -> \clubpenalty
        #     10000 (the \nopagebreak-after-label guarantee)
        #   * STATUS line = the body's one \textcolor use -> a local \textcolor
        #     wrapper drops \vadjust{\penalty 10000} after that line (the \\*
        #     guarantee: STATUS is never last on a page)
        #   * feedback tail -> \widowpenalty 10000
        # The fitting path emits the untouched measured box: no stickiness.
        tex_content += f"% [PATCH:overflow-wrapper] keep-together when it fits, breakable only when it cannot\n"
        tex_content += f"\\newsavebox{{\\conspectusbox}}\n"
        tex_content += f"\\NewEnviron{{conspectusitem}}{{%\n"
        tex_content += f"\\sbox{{\\conspectusbox}}{{\\begin{{minipage}}{{\\textwidth}}\\BODY\\end{{minipage}}}}%\n"
        tex_content += f"\\ifdim\\dimexpr\\ht\\conspectusbox+\\dp\\conspectusbox\\relax>\\dimexpr\\textheight+1pt\\relax\n"
        tex_content += "{\\clubpenalty=10000 \\widowpenalty=10000\n"
        tex_content += "\\let\\LedgerRealTextcolor\\textcolor\n"
        tex_content += "\\def\\textcolor##1##2{\\vadjust{\\penalty 10000}\\LedgerRealTextcolor{##1}{##2}}%\n"
        tex_content += f"\\noindent\\ignorespaces\\BODY\\par}}%\n"
        tex_content += f"\\else\n"
        tex_content += f"\\noindent\\usebox{{\\conspectusbox}}%\n"
        tex_content += f"\\fi}}\n\n"

        tex_content += f"% Watermark setup\n"
        tex_content += f"\\newcommand\\BackgroundPic{{%\n"
        tex_content += f"\\put(0,0){{%\n"
        tex_content += f"\\parbox[b][\\paperheight]{{\\paperwidth}}{{%\n"
        tex_content += f"\\vfill\n"
        tex_content += f"\\centering\n"
        if os.path.exists(WATERMARK_FILE):  # [PATCH:wm-guard] missing watermark must not error every page
            tex_content += f"\\includegraphics[width=0.5\\paperwidth,keepaspectratio]{{{WATERMARK_FILE}}}%\n"
        tex_content += f"\\vfill\n"
        tex_content += f"}}}}}}\n\n"

        tex_content += f"\\begin{{document}}\n"
        tex_content += f"\\AddToShipoutPictureFG{{\\BackgroundPic}}\n\n"

        # [PATCH:cos2b-sep] WO-COS2b: when feedback ends in a display
        # equation the separator cancels \belowdisplayskip (see
        # item_separator below) -- but LaTeX uses the SHORT display skips
        # instead whenever the line before the display is short (measured:
        # that is the common case), so above/below must each be equal to
        # its short variant for the cancellation to be exact either way.
        # [PATCH:cos2b-dskip] WO-COS2b commit 2: all four display skips set
        # to a modest 4pt (defaults at 11pt: 11pt/6.5pt, and 0pt ABOVE a
        # display after a short line -- the equation nearly touched the
        # "Answer:" line while wasting space everywhere else). Equations
        # stay centred; only the glue around them changes. Set AFTER
        # \begin{document} because \normalsize (run at \begin{document})
        # resets the display skips.
        tex_content += f"\\abovedisplayskip=4pt plus 1pt minus 1pt\n"
        tex_content += f"\\belowdisplayskip=4pt plus 1pt minus 1pt\n"
        tex_content += f"\\abovedisplayshortskip=4pt plus 1pt minus 1pt\n"
        tex_content += f"\\belowdisplayshortskip=4pt plus 1pt minus 1pt\n\n"

        tex_content += f"\\noindent\\textbf{{Name:}} {safe_tex_name}\\\\\n"
        tex_content += f"\\textbf{{Assignment:}} {safe_tex_title}\\\\\n"
        
        # Fetch and safely sanitize the custom pseudonym for LaTeX
        raw_label = self.get_custom_id_label()
        safe_tex_label = raw_label.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
        tex_content += f"\\textbf{{{safe_tex_label}: }} {b_id}\\\\\n"
        # [PATCH:cosmetics] WO-COS1 T1: Final Score renders \large, and one
        # thin full-width rule closes the header. Nothing else moves.
        tex_content += f"{{\\large\\textbf{{Final Score: }} {format_score(score)}/{format_score(max_score)}}}\\par\n"
        # [PATCH:cos2b-sep] WO-COS2b: the old trailing "\vspace{0.5cm}" was
        # issued in HORIZONTAL mode after the full-width rule, so the
        # paragraph broke into an empty second line carrying a whole
        # \baselineskip on top of the 0.5cm (measured: 28.6pt of white
        # under the header rule). \par first, \nointerlineskip (the next
        # element gets no interline glue), then a vertical-mode 10pt.
        tex_content += f"\\noindent\\rule{{\\textwidth}}{{0.4pt}}\\par\\nopagebreak\\nointerlineskip\n"
        tex_content += f"\\vspace{{10pt}}\n\n"

        # [PATCH:ss-conspectus] Unselected optional prompts collapse into ONE
        # neutral gray line -- never a wall of red INCORRECTs for a student who
        # followed the choose-your-own directions. Normalizer tolerates the
        # model emitting "NOT SELECTED" / "not-selected" variants.
        def _is_not_selected(it):
            s = str(it.get('status', '')).strip().upper().replace(' ', '_').replace('-', '_')
            return s == "NOT_SELECTED"

        not_selected_items = [item for item in evaluation_data if _is_not_selected(item)]
        graded_items = [item for item in evaluation_data if not _is_not_selected(item)]

        if not_selected_items:
            ns_nums = ", ".join(str(i.get('item_number', '?')).strip().rstrip(')') for i in not_selected_items)
            ns_plural = "prompts" if len(not_selected_items) > 1 else "prompt"
            tex_content += f"\\noindent\\textbf{{\\textcolor{{gray}}{{Optional {ns_plural} not selected: {ns_nums}}}}}\n"
            tex_content += f"\\vspace{{0.5cm}}\n\n"

        # [PATCH:essay-item-pages] In ESSAY mode every graded item opens on a
        # FRESH page: item 1 shares page 1 with the score header, each later
        # item is preceded by \clearpage. An item longer than one page still
        # flows onto the next (quotation blocks remain page-breakable -- the
        # ss-paragraphs guarantee), but no page ever mixes two items. BOXED
        # mode is untouched: a 20-item chemistry worksheet must not become a
        # 20-page conspectus.
        _one_item_per_page = getattr(self, "grading_mode", "boxed") == "essay"
        _first_graded_item = True

        for _gi_idx, item in enumerate(graded_items):
            # [PATCH:cosmetics] WO-COS1b T2b: the LAST graded item gets no
            # trailing separator rule -- the \clearpage to the scan pages is
            # the section break, and a rule can never land at a page boundary.
            _is_last_graded = (_gi_idx == len(graded_items) - 1)
            if _one_item_per_page and not _first_graded_item:
                tex_content += "\\clearpage\n\n"
            _first_graded_item = False
            status = str(item.get('status', '')).strip()
            points = item.get('points_earned', 0) # SURGICAL INSERTION: Grab the points
            
            # SURGICAL INSERTION: 3-way color logic
            if status == "CORRECT":
                color = "green!60!black"
            elif status == "PARTIAL":
                color = "orange" # Changed to perfectly align with the visual language
            else:
                color = "red"
            
            student_answer = str(item.get('student_answer', '')).strip().replace('\r\n', '\n')
            feedback = str(item.get('feedback', '')).strip()
            item_number = str(item.get('item_number', '')).strip()

            # [PATCH:cosmetics] WO-COS2 T2: verdict line = plain black item
            # number OUTSIDE the \textcolor (the overflow wrapper's breakable
            # branch hooks \textcolor, so the body's ONE \textcolor must wrap
            # only the colored status text), then colored, regular-weight
            # status + points. Points pluralized on the formatted value
            # (1 -> "pt").
            _pts_str = format_score(points)
            _pts_unit = "pt" if _pts_str == "1" else "pts"
            status_line = f"{item_number}~~\\textcolor{{{color}}}{{{status} · {_pts_str} {_pts_unit}}}"

            # [PATCH:ss-paragraphs] Essay-length transcriptions keep the student's
            # paragraph structure (blank-line separated in the JSON string) and
            # render OUTSIDE a minipage so LaTeX can break them across pages --
            # a giant minipage cannot break and silently overflows the page.
            # Short items keep the original minipage block verbatim.
            paragraphs = [p.strip() for p in re.split(r'\n\s*\n', student_answer) if p.strip()]
            # [PATCH:essay-render-gate] The breakable quotation layout is
            # an ESSAY-mode presentation. In WORKSHEET (BOXED) mode every
            # item -- regardless of length -- keeps the original
            # unbreakable minipage block, restoring the pre-Rev-yy
            # guarantee that a boxed response and its feedback never
            # split across a page boundary. The multi-paragraph / >600-
            # char heuristic alone was routing long MATH transcriptions
            # (LaTeX is verbose) into the essay path. In essay mode the
            # boolean is unchanged (True and x == x).
            is_essay = (getattr(self, "grading_mode", "boxed") == "essay") and \
                       (len(paragraphs) > 1 or len(student_answer) > 600)

            # [PATCH:cosmetics] WO-COS1 T1 / [PATCH:cos2b-sep] WO-COS2b: item
            # separator = a thin light-gray rule with a sliver of space each
            # side. The rule is a vertical-mode \hrule, NOT a \rule text
            # line: a text line carries \baselineskip interline glue above
            # it, and a trailing \vspace issued in horizontal mode broke the
            # paragraph into an empty extra line below it (measured 16.3pt /
            # 20.4pt of white; now ~6pt each side -- TeX inserts no
            # interline glue around rules). When the feedback ends in a
            # display equation, the glue LaTeX already hung below the
            # display is cancelled first (\belowdisplayshortskip is
            # equalized to \belowdisplayskip at \begin{document}, so the
            # dimexpr cancels whichever one applied). The rule keeps \color
            # INSIDE a group, never \textcolor: the overflow wrapper's
            # breakable branch hooks \textcolor to pin a page-break penalty
            # to the ONE status line, so the body must contain exactly one
            # \textcolor. Side-effect-free (glue/penalty/rule only; no
            # labels/counters), so a too-tall \BODY can still be typeset
            # twice. Page breaks: the \nopagebreak penalty forbids a break
            # between an item's last line and its rule; the only legal
            # breakpoint is at the glue AFTER the rule (glue preceded by a
            # rule), so the rule can never be orphaned from the item it
            # closes.
            _fb_tail = feedback.rstrip()
            _ends_in_display = _fb_tail.endswith("\\]") or _fb_tail.endswith("$$")
            _sep_space_above = (
                "\\vspace{\\dimexpr-\\belowdisplayskip+6pt\\relax}"
                if _ends_in_display else "\\vspace{6pt}"
            )
            item_separator = (
                f"\\par\\nopagebreak{_sep_space_above}\n"
                "{\\color{gray!40}\\hrule height 0.4pt}\n"
                "\\vspace{5pt}\n"
            )

            if is_essay:
                answer_body = "\n\\par\\smallskip\n".join(paragraphs)
                # [PATCH:cosmetics] WO-COS1 T1: STATUS line comes FIRST. \\*
                # keeps the "Answer:" lead-in on the status line's page and
                # \nopagebreak keeps the quotation's first line with it, so
                # the status line is never alone at a page bottom; the
                # quotation block itself stays page-breakable. \needspace
                # still keeps "Feedback:" off a page's last line.
                tex_content += f"\\noindent{status_line} \\\\*[3pt]\n"
                tex_content += f"Answer:\n"
                tex_content += f"\\nopagebreak\n"
                # [PATCH:essay-first-indent] The quotation environment indents
                # EVERY paragraph by default; the old explicit \noindent here
                # suppressed exactly the first one, so a student who indented
                # all her paragraphs saw her first reprinted flush-left. Let
                # quotation do its job -- all paragraphs now indent uniformly.
                tex_content += f"\\begin{{quotation}}{answer_body}\\end{{quotation}}\n"
                tex_content += f"\\needspace{{2\\baselineskip}}\n"
                tex_content += f"\\noindent\\textit{{Feedback:}} {feedback}\n"
                if not _is_last_graded:
                    tex_content += item_separator
                tex_content += "\n"
            else:
                answer_newline = "" if student_answer.endswith("\\]") else " \\\\[3pt]"
                # [PATCH:overflow-wrapper] the \] answer_newline quirk is kept:
                # a line break may not follow display math, so the Feedback
                # line then starts its own paragraph instead.
                tex_content += f"\\begin{{conspectusitem}}\n"
                tex_content += f"{status_line} \\\\[3pt]\n"
                tex_content += f"Answer: {student_answer}{answer_newline}\n"
                tex_content += f"\\textit{{Feedback:}} {feedback}\n"
                if not _is_last_graded:
                    tex_content += item_separator
                tex_content += f"\\end{{conspectusitem}}\n\n"
            
        tex_content += "\\clearpage\n\n"
        
        for img in original_images:
            tex_content += f"\\AddToShipoutPictureBG*{{%\n"
            tex_content += f"  \\AtPageLowerLeft{{%\n"
            tex_content += f"    \\includegraphics[width=\\paperwidth,height=\\paperheight]{{{img}}}%\n"
            tex_content += f"  }}%\n"
            tex_content += f"}}\n"
            # [PATCH:cosmetics] WO-COS1b T2c: scan pages carry only the scan
            # background and the watermark (an FG shipout picture, unaffected
            # by page style) -- no page number.
            tex_content += f"\\mbox{{}}\\thispagestyle{{empty}}\\clearpage\n"
            
        tex_content += "\\end{document}"
        
        success = False
        try:
            with open(tex_path, 'w', encoding='utf-8') as f:
                f.write(tex_content)
        except Exception as e:
            self.log_message(f"[!] Critical File Write Error for {b_id}: {e}")
            return False
            
        self.log_message(f"--> Compiling Conspectus PDF for {student_name} (ID: {b_id})...")
        try:
            result = subprocess.run(
                ['xelatex', '-interaction=nonstopmode', tex_filename], 
                cwd=output_dir, 
                capture_output=True, 
                text=True,
                timeout=900
            )

            # [PATCH:overflow-detector] Warn-only overflow scan of the compile
            # log. Runs on success AND failure (the defect compiles "fine").
            # Never blocks, alters or fails the PDF; any error here is logged
            # and swallowed.
            try:
                log_path = os.path.join(output_dir, tex_filename.replace('.tex', '.log'))
                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8', errors='replace') as _lf:
                        _overflows = detect_vbox_overflows(_lf.read())
                    if _overflows:
                        self.log_message(
                            f"[!] OVERFLOW: conspectus for {student_name} (ID: {b_id}, "
                            f"'{assignment_title}') has {len(_overflows)} page overflow(s) up to "
                            f"{max(_overflows):.1f}pt -- content may be cut off the bottom of the page."
                        )
            except Exception as _oe:
                try:
                    self.log_message(f"[!] Overflow detector error (ignored): {_oe}")
                except Exception:
                    pass

            expected_pdf = os.path.join(output_dir, tex_filename.replace('.tex', '.pdf'))
            # [PATCH:pdf-validity] a file existing is not enough -- xelatex can leave a damaged PDF
            pdf_ok = False
            if os.path.exists(expected_pdf) and os.path.getsize(expected_pdf) > 1000:
                try:
                    _sz = os.path.getsize(expected_pdf)
                    with open(expected_pdf, 'rb') as _pf:
                        _head = _pf.read(8)
                        _pf.seek(max(0, _sz - 2048))
                        _tail = _pf.read()
                    pdf_ok = _head.startswith(b'%PDF-') and (b'%%EOF' in _tail)
                except Exception:
                    pdf_ok = False
            if pdf_ok:
                self.log_message(f"--> Success! Conspectus saved for ID: {b_id}.")
                success = True
            else:
                if os.path.exists(expected_pdf):
                    self.log_message(f"[!] Conspectus PDF for {b_id} compiled but is invalid/corrupt. Treating as failure.")
                else:
                    self.log_message(f"[!] Compilation Failed for {b_id}.")
                if is_final_attempt:
                    self.log_message("Generating Plain Text Fallback...")
                    fallback_path = os.path.join(output_dir, tex_filename.replace('.tex', '-FALLBACK.txt'))
                    with open(fallback_path, 'w', encoding='utf-8') as fb:
                        fb.write(f"Name: {student_name}\nID: {b_id}\nScore: {format_score(score)}/{format_score(max_score)}\n\n")
                        for item in evaluation_data:
                            fb.write(f"Question {item.get('item_number', '')}\n")
                            fb.write(f"Answer: {item.get('student_answer', '')}\n")
                            fb.write(f"Status: {item.get('status', '')}\n")
                            fb.write(f"Feedback: {item.get('feedback', '')}\n")
                            fb.write("-" * 40 + "\n")
                    self.log_message(f"--> Fallback text report saved for ID: {b_id}.")
                success = False
                
        except subprocess.TimeoutExpired:
            self.log_message(f"[!] LaTeX Compilation Exceeded 15 Minutes for {b_id}. Process killed to protect batch.")
            success = False
        except Exception as e:
            self.log_message(f"[!] Compilation Subprocess Error for {b_id}: {e}")
            success = False
                    
        # [PATCH:diag-retention] keep the LaTeX source + compiler log when a conspectus fails
        if success:
            for ext in ['.aux', '.log', '.tex']:
                clean_path = os.path.join(output_dir, tex_filename.replace('.tex', ext))
                if os.path.exists(clean_path): os.remove(clean_path)
        else:
            diag_dir = os.path.join(output_dir, '.Conspectus-Diagnostics')
            try:
                os.makedirs(diag_dir, exist_ok=True)
                for ext in ['.aux', '.log', '.tex', '.pdf']:
                    src = os.path.join(output_dir, tex_filename.replace('.tex', ext))
                    if os.path.exists(src):
                        shutil.move(src, os.path.join(diag_dir, os.path.basename(src)))
                self.log_message(f"    -> Diagnostics for {b_id} saved to .Conspectus-Diagnostics/")
            except Exception as _e:
                self.log_message(f"[!] Could not preserve conspectus diagnostics for {b_id}: {_e}")
            for ext in ['.aux', '.log', '.tex']:
                leftover = os.path.join(output_dir, tex_filename.replace('.tex', ext))
                if os.path.exists(leftover): os.remove(leftover)
        
        return success

    def process_pipeline(self):
        # 1. Establish Cache
        success = self.initialize_silo_2_cache(self.master_pdf_path)
        if not success:
            self.finish_processing()
            return

        self.log_message("Scanning '02 Inbox Scans' folder...")
        pdf_files = [f for f in os.listdir(UNEVALUATED_DIR) if f.lower().endswith('.pdf')]
        
        if not pdf_files:
            self.log_message("No unevaluated PDFs found.")
            self.finish_processing()
            return
            
        global_batch_data = {}
        pdf_to_period_map = {}
        tabula_records_by_eval_dir = {}
        
        # [PATCH:footer-reader] WO-COS2 T3: empty Footer-Rejects at the START
        # of Pass 1 (not at app startup) -- the previous run's crops stay
        # available for diagnosis until the next run begins.
        # [PATCH:box-rejects] WO-BM1 T5: Box-Rejects and ID-Rejects share
        # that lifecycle.
        for _rej_dir in (FOOTER_REJECTS_DIR, BOX_REJECTS_DIR, ID_REJECTS_DIR):
            try:
                if os.path.isdir(_rej_dir):
                    for _fr_name in os.listdir(_rej_dir):
                        _fr_path = os.path.join(_rej_dir, _fr_name)
                        if os.path.isfile(_fr_path):
                            os.remove(_fr_path)
            except Exception as _fr_e:
                self.log_message(f"[!] Could not empty "
                                 f"{os.path.basename(_rej_dir)}: {_fr_e}")

        # [PATCH:run-summary] WO-COS2 T6: Pass 1 is actually running -- the
        # end-of-run summary prints only for runs that reached this point.
        self._pass1_ran = True

        self.log_message(f"=== INITIATING LEDGER PASS 1: EXTRACTION & CROPPING ({len(pdf_files)} Files) ===")
        
        for pdf_file in pdf_files:
            if not self.is_running: break
            pdf_path = os.path.join(UNEVALUATED_DIR, pdf_file)
            self.log_message(f"Reading document: {pdf_file}")
            
            first_period_found = None
            _pass1_err_page = 0  # [PATCH:run-summary] T6: last page being read

            try:
                doc = fitz.open(pdf_path)
                total_pages = len(doc)
                # [PATCH:run-summary] WO-COS2 T6: tally only, no flow change.
                if hasattr(self, "_run_stats"):
                    self._run_stats["pages_scanned"] = \
                        self._run_stats.get("pages_scanned", 0) + total_pages

                for page_num in range(total_pages):
                    if not self.is_running: break
                    
                    page = doc.load_page(page_num)
                    pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16), alpha=False) 
                    # NOTE: Images saved to TEMP_DIR, parked safely away from Ledger Engine. 
                    
                    # THE FIX: Replace spaces in the PDF filename with underscores for LaTeX
                    safe_pdf_name = pdf_file.replace(" ", "_")
                    temp_full_img_path = os.path.join(TEMP_DIR, f"{safe_pdf_name}-temp-full-page-{page_num + 1}.png")
                    
                    pix.save(temp_full_img_path)
                    
                    _pass1_err_page = page_num + 1  # [PATCH:run-summary] T6

                    if ImageStat.Stat(Image.open(temp_full_img_path).convert("L")).stddev[0] < 15.0:
                        self.log_message(f"  -> Page {page_num + 1}/{total_pages} is blank. Skipping.")
                        if hasattr(self, "_run_stats"):  # [PATCH:run-summary] T6
                            self._run_stats["blank_pages"] = \
                                self._run_stats.get("blank_pages", 0) + 1
                        continue
                        
                    # THE FIX: Silo 1 now runs on every single page to catch out-of-order scans!
                    # 1. Dynamic Extraction of ID and Footer using locked Database Coordinates
                    img_for_crop = cv2.imread(temp_full_img_path)

                    # [PATCH:auto-orient] WO-COS2 T4: correct an upside-down
                    # page locally BEFORE anything is cropped or sent. When
                    # the orient step read the footer, its result is reused
                    # below -- an upright page costs ONE footer read, as
                    # today. LEDGER_AUTO_ORIENT=0 restores today's flow.
                    img_for_crop, oriented_footer_result = self._auto_orient_page(
                        temp_full_img_path, img_for_crop, pdf_file,
                        page_num + 1, total_pages)

                    id_coords = self.get_crop_coordinates('crop_id')
                    id_crop = img_for_crop[id_coords[1] : id_coords[1]+id_coords[3], id_coords[0] : id_coords[0]+id_coords[2]]
                    temp_id_path = os.path.join(TEMP_DIR, f"{safe_pdf_name}-temp-id-{page_num + 1}.png")
                    cv2.imwrite(temp_id_path, id_crop)

                    f_coords = self.get_crop_coordinates('crop_footer')
                    temp_footer_path = os.path.join(TEMP_DIR, f"{safe_pdf_name}-temp-footer-{page_num + 1}.png")
                    # [PATCH:auto-orient] WO-COS2 T4: when the orient step
                    # already cropped + read this page's footer (same window,
                    # same file), reuse its result -- never OCR twice.
                    if oriented_footer_result is None:
                        footer_crop = img_for_crop[f_coords[1] : f_coords[1]+f_coords[3], f_coords[0] : f_coords[0]+f_coords[2]]
                        cv2.imwrite(temp_footer_path, footer_crop)

                        # 2. Extract Data (Local OCR for Footer, API for ID)
                        # [PATCH:footer-reader] WO-COS2 T3: the footer is read
                        # ONCE here and the result handed to extract_with_silo_1
                        # (same single OCR per page as before).
                        footer_result = self.read_footer_crop(
                            temp_footer_path, scan_file=pdf_file, page_no=page_num + 1)
                    else:
                        footer_result = oriented_footer_result
                    # [PATCH:pass1-pacing] WO-COS2 T5: was a hardcoded
                    # time.sleep(30) ("to prevent 429 quota exhaustion" --
                    # ~50 min of sleep per 100 pages against a 2,000 req/min
                    # limit). Now LEDGER_PASS1_PACING_S (default 3), with an
                    # automatic slow-down to LEDGER_PASS1_SLOW_PACING_S
                    # (default 30) after any capacity error this run.
                    self._pass1_pause()
                    silo_data = self.extract_with_silo_1(temp_id_path, temp_footer_path, footer_result=footer_result)

                    if silo_data:
                        # [PATCH:id-reask] WO-COS2 T5: an off-roster ID gets
                        # ONE re-read before the page is discarded.
                        silo_data, b_id = self._pass1_resolve_id(
                            silo_data, temp_id_path, temp_footer_path,
                            footer_result, pdf_file, page_num + 1)
                        if silo_data is None:
                            continue

                        # [PATCH:run-summary] WO-COS2 T6: this page WILL be
                        # appended to b_id below (both modes) -- tally it.
                        if hasattr(self, "_run_stats"):
                            self._run_stats["pages_sorted"] = \
                                self._run_stats.get("pages_sorted", 0) + 1
                            _pps = self._run_stats.setdefault("pages_per_student", {})
                            _pps[b_id] = _pps.get(b_id, 0) + 1

                        if not first_period_found:
                            first_period_found = self.get_period_folder(b_id)
                            
                        # Use the globally locked Master Title variables here
                        if self.master_group_key not in global_batch_data:
                            global_batch_data[self.master_group_key] = {
                                "display_title": self.master_display_title,
                                "original_title": self.master_original_title,
                                "max_possible_score": self.global_max_score,
                                "students": {}
                            }
                            
                        assignment_ref = global_batch_data[self.master_group_key]
                        
                        if b_id not in assignment_ref["students"]:
                            assignment_ref["students"][b_id] = {
                                "full_pages": [],  
                                "snippets": []     
                            }
                            
                        # 3. NEW: Get the page number from the Silo 1 JSON
                        silo_page_number = silo_data.get("page_num", 999)
                        
                        # [PATCH:essay-mode] FULL_PAGE mode never crops boxes:
                        # the page itself is the payload. Instead, locally OCR
                        # the printed corner tag so pages can be grouped into
                        # essays (and continuations detected) with zero API
                        # calls. BOXED mode is byte-path identical to before.
                        if getattr(self, "grading_mode", "boxed") == "essay":
                            # [PATCH:essay-tag-quiet] OCR the corner tag ONLY
                            # when chunked packing will consult it; in
                            # single-call mode skip the work and the noise.
                            if getattr(self, "essay_tag_ocr_needed", False):
                                essay_tag = self._ocr_essay_tag(temp_full_img_path)
                                tag_note = (f", Essay Tag: "
                                            f"{essay_tag if essay_tag is not None else 'not detected'}")
                            else:
                                essay_tag = None
                                tag_note = ""
                            assignment_ref["students"][b_id]["full_pages"].append({
                                "path": temp_full_img_path,
                                "lowest_q": silo_page_number,
                                "essay_num": essay_tag
                            })
                            self.log_message(f"  -> Page sorted to ID {b_id} "
                                             f"(Page Number Read: {silo_page_number}{tag_note})")
                        else:
                            assignment_ref["students"][b_id]["full_pages"].append({
                                "path": temp_full_img_path, 
                                "lowest_q": silo_page_number # Reusing existing variable key for seamless sorting
                            })
                            
                            page_snippets = self.extract_boxes_with_opencv(temp_full_img_path, page_num + 1)
                            # [PATCH:box-map] compare what the finder found
                            # with the master's box map for this footer page;
                            # recover (verified) or report any missing box.
                            # Clean pages return UNTOUCHED (byte-identical).
                            page_snippets = self._box_map_reconcile(
                                page_snippets, b_id, silo_page_number,
                                temp_full_img_path, page_num + 1)
                            assignment_ref["students"][b_id]["snippets"].extend(page_snippets)
                            # [PATCH:silo2-per-chunk-context] Record the FOOTER-derived
                            # master page number for every snippet cropped from this
                            # physical page. The scan-stack index in the snippet
                            # filename is NOT the master page; only the printed
                            # footer number is. 999 (unreadable) is recorded as-is
                            # and triggers the FULL-master fallback downstream.
                            _smap = assignment_ref["students"][b_id].setdefault("snippet_master_pages", {})
                            for _sp in page_snippets:
                                _smap[_sp] = silo_page_number
                            
                            self.log_message(f"  -> Page sorted to ID {b_id} (Page Number Read: {silo_page_number})")
                                                                            
                doc.close()
                pdf_to_period_map[pdf_file] = first_period_found if first_period_found else "Unknown-Student"
                
            except Exception as e:
                self.log_message(f"[!] Error reading {pdf_file}: {e}")
                # [PATCH:run-summary] WO-COS2 T6: LOGGING ONLY -- the
                # try/except and the loop's control flow are unchanged.
                if hasattr(self, "_run_stats"):
                    self._run_stats.setdefault("read_errors", []).append(
                        {"file": pdf_file, "page": _pass1_err_page})
                continue
                
        # [PATCH:footer-reader] WO-COS2 T3: one visible line on how the
        # footers fared, before the warnings scroll away.
        _fa = getattr(self, "_footers_attempted", 0)
        _fr = getattr(self, "_footers_read", 0)
        if _fa:
            self.log_message(footer_summary_line(
                _fr, _fa,
                saving_enabled=os.environ.get("LEDGER_FOOTER_DEBUG", "1") != "0"))

        # [PATCH:box-rejects] WO-BM1 T5: one plain pointer per folder that
        # received anything this run (the Footer-Rejects pattern).
        if getattr(self, "_box_rejects_saved", 0):
            self.log_message("Rejected box-recovery crops saved in "
                             ".Diagnostic-Log/Box-Rejects.")
        if getattr(self, "_id_rejects_saved", 0):
            self.log_message("ID crops of discarded pages saved in "
                             ".Diagnostic-Log/ID-Rejects.")

        # PASS 2: Evaluation & Conspectus Generation
        self.log_message("=== PASS 1 COMPLETE. INITIATING LEDGER PASS 2: BATCH EVALUATION ===")

        # [PATCH:batch] SCORING: BATCH submits Pass 2 through the provider's
        # half-price batch services and collects the results; the loop below
        # is the LIVE path, byte-identical to before.
        if getattr(self, "scoring_mode", "live") == "batch":
            try:
                self._run_batch_pass2(global_batch_data, pdf_to_period_map,
                                      tabula_records_by_eval_dir)
            except Exception as e:
                self.log_message(f"[!] BATCH scoring failed: {e}")
                self.finish_processing()
            return

        deferred_ids = set()  # [PATCH:silo2-bounded-retry] students skipped on capacity (429)
        for group_key, data in global_batch_data.items():
            for b_id, student_data in data["students"].items():
                if not self.is_running: break

                # [PATCH:essay-mode] Assemble the Silo 2 payload for this
                # student. BOXED: stitched grid chunks (single image per call,
                # unchanged). ESSAY: essay-aligned groups of resized full
                # pages (one page LIST per call; evaluate_grid_with_silo_2
                # ships a list as an images_b64 payload).
                if getattr(self, "grading_mode", "boxed") == "essay":
                    chunk_paths = self.assemble_essay_pages(student_data["full_pages"], b_id)
                else:
                    chunk_paths = self.stitch_snippets_to_grid(student_data["snippets"], b_id,
                                                               student_data.get("snippet_master_pages"))

                if not chunk_paths:
                    self.log_message(f"[!] No snippets found for {b_id}. Skipping evaluation.")
                    continue

                # Pre-sort original pages and pull student info outside the evaluation loop
                student_data["full_pages"].sort(key=lambda x: x["lowest_q"])
                sorted_page_paths = [page["path"] for page in student_data["full_pages"]]
                student_info = self.roster.get(b_id, {"last": "Unknown", "first": "Student"})

                # [PATCH:batch] the per-student finishing path is extracted
                # into _finish_student so BATCH collection can reuse it
                # unchanged; LIVE behavior is identical to before.
                self._finish_student(group_key, data, b_id, student_info,
                                     sorted_page_paths, chunk_paths,
                                     tabula_records_by_eval_dir, deferred_ids)
                if not self.is_running: break # Safety exit for shutdown button

        # Tabula Generation
        if self.is_running:
            self._write_tabulas(tabula_records_by_eval_dir)

        # Original Scan Cleanup
        self._move_scans(pdf_to_period_map, deferred_ids)

        self.finish_processing()

    def _finish_student(self, group_key, data, b_id, student_info,
                        sorted_page_paths, chunk_paths,
                        tabula_records_by_eval_dir, deferred_ids,
                        first_evaluation=None):
        """The per-student finishing path: evaluate, sort, score, write the
        DB, build the conspectus (the existing 3-attempt loop that re-scores
        LIVE on a compile failure), and append the Tabula record.

        [PATCH:batch] Extracted verbatim from the Pass 2 loop so BATCH
        collection finishes a student through the IDENTICAL code. With
        first_evaluation set (the batch results), attempt 1 uses it without
        the live pacing sleeps (pacing applies to live calls only); a failed
        compile falls back to live re-scoring exactly as today."""
        max_possible_score = data["max_possible_score"]
        safe_title = data["display_title"]
        period_folder = self.get_period_folder(b_id)

        # --- FOLDER NOMENCLATURE SURGERY ---
        if period_folder == "Unknown-Student":
            folder_suffix = "-Unknown"
        else:
            # Splits "7th Period" at the space and grabs just "7th"
            folder_suffix = f"-{period_folder.split()[0]}"

        # Append the suffix for the folder name, leaving safe_title intact for LaTeX
        appended_folder_name = f"{safe_title}{folder_suffix}"

        eval_dir = os.path.join(COURSES_DIR, period_folder, "Evaluated Assignments", appended_folder_name)
        # -----------------------------------

        os.makedirs(eval_dir, exist_ok=True)

        # --- NEW RETRY LOOP INITIATED ---
        max_attempts = 3
        attempt = 1
        compilation_success = False

        while attempt <= max_attempts and self.is_running:
            self.log_message(f"Evaluating stitched grid for ID {b_id} via cached Silo 2 (Attempt {attempt} of {max_attempts})...")

            if first_evaluation is not None and attempt == 1:
                # [PATCH:batch] attempt 1 of a batch-collected student uses
                # the already-fetched results; no pacing sleep (live only).
                full_evaluation = first_evaluation
            else:
                if attempt > 1:
                    time.sleep(15) # Increased retry pacing
                else:
                    time.sleep(30) # Increased to 30s to stay under TPM limits

                full_evaluation = self.evaluate_chunks_with_silo_2(chunk_paths)

            # [PATCH:silo2-bounded-retry] None == bounded retries exhausted on Google
            # capacity (429), OR the user pressed Stop. Defer this student: do NOT write
            # a zero grade or build a conspectus from empty results.
            if full_evaluation is None:
                if self.is_running:
                    self.log_message(f"  -> [DEFERRED] ID {b_id}: grading call failed (see Silo 2 Detail above) - not graded. Scan retained for re-run.")
                    deferred_ids.add(b_id)
                break  # leave the LaTeX-attempt loop for this student

            # NEW GUARD: Prevent fall-through crash on NoneType objects if API request fails
            if not isinstance(full_evaluation, list):
                full_evaluation = []

            # Post-evaluation sorting (Alphanumeric)
            if full_evaluation:
                prefix_map = {}
                for item in full_evaluation:
                    m = re.match(r'^(\d+)([a-z]+)', str(item.get("item_number", "")).strip().lower())
                    if m:
                        prefix = int(m.group(1))
                        prefix_map[prefix] = prefix_map.get(prefix, 0) + 1
                best_prefix = max(prefix_map.keys(), key=lambda k: prefix_map[k]) if prefix_map else 0

                def get_item_sort_key(item):
                    s = str(item.get("item_number", "")).strip().lower()
                    m_num = re.match(r'^(\d+)([a-z]*)', s)
                    if m_num: return (int(m_num.group(1)), m_num.group(2))
                    m_let = re.match(r'^([a-z]+)', s)
                    if m_let: return (best_prefix if best_prefix > 0 else float('inf'), m_let.group(1))
                    return (float('inf'), s)
                full_evaluation.sort(key=get_item_sort_key)

            earned_points = sum(item.get("points_earned", 0) for item in full_evaluation)
            self.log_message(f"  -> Score Computed: {format_score(earned_points)}/{format_score(max_possible_score)}")

            # NEW: Write the final score to the database
            self.write_grade_to_db(b_id, group_key, earned_points)

            # Conspectus generation with success tracking
            is_final = (attempt == max_attempts)
            compilation_success = self.generate_latex_conspectus(
                b_id, student_info, data["original_title"], earned_points,
                max_possible_score, full_evaluation, sorted_page_paths, eval_dir,
                is_final_attempt=is_final
            )

            if compilation_success:
                break # Success! Exit the retry loop.
            else:
                self.log_message(f"[!] LaTeX Compilation Failed. Re-triggering Silo 2...")
                attempt += 1

        # [PATCH:silo2-bounded-retry] Deferred students were never graded. Record
        # them in the Tabula as DEFERRED so the teacher sees exactly who needs a
        # re-run, then skip the normal score append below.
        if b_id in deferred_ids:
            if eval_dir not in tabula_records_by_eval_dir:
                tabula_records_by_eval_dir[eval_dir] = []
            tabula_records_by_eval_dir[eval_dir].append({
                "last": student_info['last'], "first": student_info['first'],
                "b_id": b_id, "score_str": "DEFERRED"
            })
            return
        if not self.is_running:
            return  # Stop pressed mid-student: no half record

        # Tabula Generation proceeds with the final evaluation result
        if eval_dir not in tabula_records_by_eval_dir:
            tabula_records_by_eval_dir[eval_dir] = []
        tabula_records_by_eval_dir[eval_dir].append({
            "last": student_info['last'], "first": student_info['first'],
            "b_id": b_id, "score_str": f"{format_score(earned_points)}/{format_score(max_possible_score)}"
        })

    def _write_tabulas(self, tabula_records_by_eval_dir):
        """Tabula generation, extracted verbatim so BATCH collection can call
        it too. [PATCH:batch]"""
        if True:
            for eval_dir, records in tabula_records_by_eval_dir.items():
                tabula_path = os.path.join(eval_dir, "Tabula.csv")
                merged_data = {}
                
                # 1. Load existing data to prevent overwriting previous batches
                if os.path.exists(tabula_path):
                    try:
                        with open(tabula_path, 'r', encoding='utf-8') as f:
                            reader = csv.reader(f)
                            next(reader, None) # Skip the header row
                            for row in reader:
                                if len(row) >= 4:
                                    # SURGICAL INSERTION: Aggressively strip anything that isn't a digit
                                    b_id = ''.join(filter(str.isdigit, row[2]))
                                    merged_data[b_id] = {
                                        "last": row[0], "first": row[1], 
                                        "b_id": b_id, "score_str": row[3].replace("'", "")
                                    }
                    except Exception as e:
                        self.log_message(f"[!] Warning: Could not read existing Tabula: {e}")

                # 2. Merge new records (safely overwriting duplicates via ID key)
                for rec in records:
                    merged_data[rec["b_id"]] = rec
                    
                # [PATCH:final-records] materialize the dedupe dict the sort/write below expect
                final_records = list(merged_data.values())
                
                # 3. Sort the combined master list alphabetically (Falls back to ID for blank names/ties)
                final_records.sort(key=lambda x: (x["last"].lower(), x["first"].lower(), x["b_id"]))
                final_records.sort(key=lambda x: (x["last"].lower(), x["first"].lower()))
                
                # 4. Write the combined data back to the CSV
                try:
                    with open(tabula_path, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow(["Last Name", "First Name", "Bartonian ID", "Final Score"])
                        for rec in final_records:
                            # Prepending an apostrophe to force left-alignment (text format) in Calc without visible quotes
                            writer.writerow([rec["last"], rec["first"], f"'{rec['b_id']}", f"'{rec['score_str']}"])
                except PermissionError:
                    self.log_message(f"[!] CRITICAL: Could not save Tabula.csv. Is it currently open in another program?")
                except Exception as e:
                    self.log_message(f"[!] Error writing Tabula: {e}")

    def _move_scans(self, pdf_to_period_map, deferred_ids):
        """Original scan cleanup, extracted verbatim so BATCH collection can
        call it too. [PATCH:batch]"""
        self.log_message("=== PASS 2 COMPLETE. MOVING ORIGINAL SCANS ===")
        if deferred_ids:
            self.log_message(f"=== {len(deferred_ids)} STUDENT(S) DEFERRED (grading call failed). Scans RETAINED in '02 Inbox Scans' for re-run; grades use INSERT OR REPLACE and the Tabula de-dupes by ID, so re-running the batch is idempotent: {sorted(deferred_ids)} ===")
        for pdf_file, dest_period in pdf_to_period_map.items():
            if deferred_ids:
                continue  # [PATCH:silo2-bounded-retry] retain scans while any student is deferred
            source_path = os.path.join(UNEVALUATED_DIR, pdf_file)
            if os.path.exists(source_path):
                try:
                    dest_path = os.path.join(ORIGINAL_SCANS_DIR, pdf_file)
                    shutil.move(source_path, dest_path)
                except Exception as e:
                    self.log_message(f"[!] Could not move scan {pdf_file}: {e}")

    # ======================================================================
    # [PATCH:batch] WO-B1: BATCH SCORING (SCORING: BATCH)
    # Pass 1 and the context build are untouched. In BATCH, Pass 2 submits
    # every chunk as half-price provider batch requests through the daemon,
    # keeps a job record in the WORKSPACE (outside the scrubbed temp folder),
    # polls, then finishes every student through the SAME code path as live
    # (parse -> count check -> merge -> sort -> score -> DB -> report ->
    # Tabula -> scan move). One pending job at a time; SHUT DOWN only stops
    # the polling; startup offers RESUME / CANCEL BATCH.
    # ======================================================================
    def _batch_job_save(self, job):
        """Atomic job-record write: everything needed to finish the run later."""
        os.makedirs(BATCH_JOB_DIR, exist_ok=True)
        tmp = BATCH_JOB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=1)
        os.replace(tmp, BATCH_JOB_FILE)

    def _batch_job_load(self):
        with open(BATCH_JOB_FILE, encoding="utf-8") as f:
            return json.load(f)

    def _batch_progress(self, text):
        self.root.after(0, lambda: self.batch_progress_label.configure(text=text))

    def _ask_choice(self, title, message, options):
        """[PATCH:batch] Modal choice shown from a worker thread; blocks the
        worker (NOT the UI) until one option is pressed. Returns the label."""
        result = {}
        ev = threading.Event()

        def build():
            win = ctk.CTkToplevel(self.root)
            win.title(title)
            self._clamp_toplevel(win, 760, 360)
            win.transient(self.root)
            win.protocol("WM_DELETE_WINDOW", lambda: None)  # must choose
            ctk.CTkLabel(win, text=title, font=self.header_font).pack(pady=(24, 10))
            ctk.CTkLabel(win, text=message, font=self.main_font,
                         wraplength=680, justify="left").pack(pady=10, padx=30)
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(pady=20)

            def choose(opt):
                result["choice"] = opt
                try:
                    win.grab_release()
                    win.destroy()
                except tk.TclError:
                    pass
                ev.set()

            for i, opt in enumerate(options):
                color = "#1e8449" if i == 0 else "#c0392b"
                hover = "#27ae60" if i == 0 else "#e74c3c"
                ctk.CTkButton(row, text=opt, font=self.main_font, height=48,
                              fg_color=color, hover_color=hover,
                              command=lambda o=opt: choose(o)).pack(side="left", padx=12)
            self._make_text_copyable(win)
            win.after(200, win.grab_set)

        self.root.after(0, build)
        ev.wait()
        return result.get("choice", options[-1])

    def _batch_request_body(self, custom_id, chunk_desc):
        """One silo2-shaped request for /api/batch/spool: same payload keys,
        same prompt, same schema, same per-chunk masters as the live call."""
        body = {"custom_id": custom_id, "prompt": SILO2_PROMPT,
                "schema": EVALUATION_SCHEMA}
        if isinstance(chunk_desc, (list, tuple)):
            # Essay mode: the chunk is a list of page paths.
            body["images_b64"] = [self.image_to_base64_png(p) for p in chunk_desc]
        else:
            body["image_b64"] = self.image_to_base64_png(chunk_desc["path"])
            master_ctx = self._s2ctx_master_images_b64(chunk_desc.get("footer_pages"))
            if master_ctx:
                body["master_images_b64"] = master_ctx
        return body

    def _batch_abs_chunk(self, rec):
        """Job-record chunk entry -> the chunk shape the evaluators expect."""
        if "pages" in rec:
            return [os.path.join(BATCH_JOB_DIR, p) for p in rec["pages"]]
        return {"path": os.path.join(BATCH_JOB_DIR, rec["path"]),
                "footer_pages": rec.get("footer_pages"),
                "box_count": rec.get("box_count")}

    def _run_batch_pass2(self, global_batch_data, pdf_to_period_map,
                         tabula_records_by_eval_dir):
        """BATCH Pass 2: assemble, warm the cache (Anthropic), submit, then
        poll and collect. Pacing sleeps apply to live calls only, never to
        building batch requests."""
        deferred_ids = set()
        if not global_batch_data:
            self.log_message("No students found to evaluate.")
            self.finish_processing()
            return
        group_key, data = next(iter(global_batch_data.items()))
        model = self.get_silo2_model()
        m = model.lower()
        is_anthropic = (m.startswith("claude") or "anthropic/" in m
                        or m.startswith("publishers/anthropic"))

        # 1. Assemble every student's chunks with the existing functions.
        assembled = {}
        for b_id, student_data in data["students"].items():
            if not self.is_running:
                self.finish_processing()
                return
            if getattr(self, "grading_mode", "boxed") == "essay":
                chunks = self.assemble_essay_pages(student_data["full_pages"], b_id)
            else:
                chunks = self.stitch_snippets_to_grid(
                    student_data["snippets"], b_id,
                    student_data.get("snippet_master_pages"))
            if not chunks:
                self.log_message(f"[!] No snippets found for {b_id}. Skipping evaluation.")
                continue
            student_data["full_pages"].sort(key=lambda x: x["lowest_q"])
            assembled[b_id] = {
                "chunks": chunks,
                "pages": [p["path"] for p in student_data["full_pages"]],
            }
        if not assembled:
            self.log_message("No students found to evaluate.")
            self.finish_processing()
            return

        order = list(assembled.keys())
        # 2. Anthropic only: score the FIRST student live through the existing
        # path -- this writes the prompt and master pages into Anthropic's
        # prompt cache so the batched requests can read them. Google skips
        # this (its batch and cache discounts do not combine).
        first_live = None
        if is_anthropic:
            first_live = order[0]
            self.log_message(f"-> BATCH: scoring the first student (ID {first_live}) "
                             "live to warm the provider's prompt cache...")
            info = self.roster.get(first_live, {"last": "Unknown", "first": "Student"})
            self._finish_student(group_key, data, first_live, info,
                                 assembled[first_live]["pages"],
                                 assembled[first_live]["chunks"],
                                 tabula_records_by_eval_dir, deferred_ids)
            if not self.is_running:
                self.finish_processing()
                return

        # 3. Build the job record and move the run's files into the job
        # folder (the temp folder is scrubbed at startup, shutdown and run
        # end; a pending job must survive all three).
        job_id = datetime.datetime.now().strftime("job-%Y%m%d-%H%M%S")
        os.makedirs(os.path.join(BATCH_JOB_DIR, "chunks"), exist_ok=True)
        os.makedirs(os.path.join(BATCH_JOB_DIR, "pages"), exist_ok=True)
        master_copy = os.path.join(BATCH_JOB_DIR, "master.pdf")
        try:
            shutil.copy(self.master_pdf_path, master_copy)
        except Exception as e:
            self.log_message(f"[!] Could not copy the master into the job folder: {e}")
            self.finish_processing()
            return
        job = {
            "version": 1,
            "job_id": job_id,
            "created": datetime.datetime.now().isoformat(),
            "state": "building",
            "model": model,
            "grading_mode": getattr(self, "grading_mode", "boxed"),
            "master_pdf": master_copy,
            "assignment": {"group_key": group_key,
                           "display_title": data["display_title"],
                           "original_title": data["original_title"],
                           "max_score": data["max_possible_score"]},
            "pdf_to_period_map": pdf_to_period_map,
            "first_student_live": first_live,
            # [PATCH:box-map] Pass-1 answer-box notices ride in the job so the
            # end-of-run summary still prints after a shutdown and RESUME.
            "box_map_notices": list(getattr(self, "_box_map_notices", [])),
            # [PATCH:run-summary] WO-COS2 T6: the run summary rides in the
            # job the same way (read back with .get, so an older job record
            # without the key still loads).
            "run_summary": self._collect_run_summary_stats(),
            "submissions": [],
            "requests": {},
            "students": {},
            "tabula": [[d, r] for d, recs in tabula_records_by_eval_dir.items()
                       for r in recs],
            "deferred": sorted(deferred_ids),
        }
        for b_id in order:
            if b_id == first_live:
                continue
            chunk_recs = []
            cids = []
            for i, ch in enumerate(assembled[b_id]["chunks"]):
                cid = f"{b_id}-c{i}"
                if isinstance(ch, (list, tuple)):
                    rels = []
                    for j, p in enumerate(ch):
                        rel = os.path.join("chunks", f"{b_id}_c{i}_p{j}.png")
                        shutil.move(p, os.path.join(BATCH_JOB_DIR, rel))
                        rels.append(rel)
                    rec = {"pages": rels}
                else:
                    rel = os.path.join("chunks", f"{b_id}_c{i}.png")
                    shutil.move(ch["path"], os.path.join(BATCH_JOB_DIR, rel))
                    rec = {"path": rel, "footer_pages": ch.get("footer_pages"),
                           "box_count": ch.get("box_count")}
                job["requests"][cid] = {"b_id": b_id, "chunk_index": i,
                                        "chunk": rec}
                chunk_recs.append(rec)
                cids.append(cid)
            page_rels = []
            for j, p in enumerate(assembled[b_id]["pages"]):
                rel = os.path.join("pages", f"{b_id}_p{j:02d}.png")
                try:
                    shutil.move(p, os.path.join(BATCH_JOB_DIR, rel))
                except Exception:
                    shutil.copy(p, os.path.join(BATCH_JOB_DIR, rel))
                page_rels.append(rel)
            job["students"][b_id] = {"chunks": cids, "pages": page_rels,
                                     "status": "pending"}
        self._batch_job_save(job)

        # 4. Spool and submit, splitting so no provider batch passes its cap
        # and no single local request uses unreasonable memory.
        try:
            group_n = int(os.environ.get("LEDGER_BATCH_SPOOL_GROUP", "4"))
        except (TypeError, ValueError):
            group_n = 4
        default_cap = "200" if is_anthropic else "700"
        try:
            cap_bytes = float(os.environ.get("LEDGER_BATCH_MAX_SUBMISSION_MB",
                                             default_cap)) * 1e6
        except (TypeError, ValueError):
            cap_bytes = float(default_cap) * 1e6
        all_cids = [cid for b in job["students"].values() for cid in b["chunks"]]
        self.log_message(f"-> BATCH: submitting {len(all_cids)} request(s) for "
                         f"{len(job['students'])} student(s) at the provider's "
                         "half price...")
        sub_n = 0
        current = None
        try:
            idx = 0
            while idx < len(all_cids):
                if current is None:
                    sub_n += 1
                    current = {"submission_id": f"{job_id}-s{sub_n}",
                               "state": "spooling", "custom_ids": [],
                               "batch_id": None}
                    job["submissions"].append(current)
                    self._batch_job_save(job)
                group = all_cids[idx:idx + group_n]
                idx += len(group)
                reqs = [self._batch_request_body(
                    cid, self._batch_abs_chunk(job["requests"][cid]["chunk"]))
                    for cid in group]
                r = requests.post(DAEMON_BASE + "/api/batch/spool",
                                  json={"submission_id": current["submission_id"],
                                        "requests": reqs}, timeout=600)
                self._raise_daemon_error(r)
                spooled = r.json()
                current["custom_ids"].extend(group)
                self._batch_job_save(job)
                if spooled.get("bytes", 0) >= cap_bytes or idx >= len(all_cids):
                    # Submit this spool as ONE provider batch. The record is
                    # saved BEFORE (state submitting) and AFTER (batch id), so
                    # a crash in between is detectable and never re-submitted
                    # silently.
                    current["state"] = "submitting"
                    self._batch_job_save(job)
                    r = requests.post(DAEMON_BASE + "/api/batch/submit",
                                      json={"submission_id": current["submission_id"]},
                                      timeout=1800)
                    self._raise_daemon_error(r)
                    current["batch_id"] = r.json().get("batch_id")
                    current["state"] = "submitted"
                    self._batch_job_save(job)
                    self.log_message(f"-> BATCH: submission "
                                     f"{current['submission_id']} accepted by the "
                                     f"provider ({len(current['custom_ids'])} "
                                     f"request(s)).")
                    current = None
        except Exception as e:
            submitted = [s for s in job["submissions"] if s.get("batch_id")]
            self.log_message(f"[!] BATCH submission failed: {e}")
            if not submitted:
                self.log_message("[!] Nothing reached the provider. The job was "
                                 "abandoned; scans remain in '02 Inbox Scans' -- "
                                 "run again (LIVE or BATCH).")
                shutil.rmtree(BATCH_JOB_DIR, ignore_errors=True)
                self.finish_processing()
                return
            self.log_message("[!] Some submissions DID reach the provider; the "
                             "job stays pending so the paid work is not lost. "
                             "Unsubmitted students will be offered live scoring "
                             "or deferral at collection.")
        job["state"] = "pending"
        self._batch_job_save(job)

        # 5-7. Poll, collect, and finish on this same thread.
        self._batch_poll_and_collect(job)

    def _batch_poll_and_collect(self, job):
        """Poll the daemon for status every LEDGER_BATCH_POLL_S seconds, show
        progress, then fetch results and finish each student with the
        existing code path. Runs on a worker thread; the operator clicks nothing."""
        try:
            poll_s = float(os.environ.get("LEDGER_BATCH_POLL_S", "120"))
        except (TypeError, ValueError):
            poll_s = 120.0
        submitted = [s for s in job["submissions"] if s.get("batch_id")]
        ids = [s["batch_id"] for s in submitted]
        total = sum(len(s["custom_ids"]) for s in submitted)
        try:
            t0 = datetime.datetime.fromisoformat(job["created"])
        except Exception:
            t0 = datetime.datetime.now()

        # [PATCH:batch] WO-B1-2 Task 3: while an ANTHROPIC batch is pending,
        # renew every distinct cached prefix the job uses, every
        # LEDGER_BATCH_KEEPALIVE_S seconds (default 2700). The cached prompt
        # lasts one hour; a batch that runs longer would pay the cache-WRITE
        # price on its remaining requests, erasing most of the batch saving.
        # A keep-alive failure is logged and never stops or delays collection.
        _m = (job.get("model") or "").lower()
        keepalive_stop = threading.Event()
        if ids and (_m.startswith("claude") or "anthropic/" in _m
                    or _m.startswith("publishers/anthropic")):
            threading.Thread(target=self._batch_keepalive_loop,
                             args=(job, keepalive_stop), daemon=True).start()

        while self.is_running and ids:
            try:
                r = requests.get(DAEMON_BASE + "/api/batch/status",
                                 params={"ids": ",".join(ids)}, timeout=120)
                self._raise_daemon_error(r)
                batches = r.json().get("batches", [])
            except Exception as e:
                self.log_message(f"[!] Batch status check failed: {e} -- will "
                                 "try again at the next poll.")
                batches = []
            finished = 0
            all_done = bool(batches)
            for b in batches:
                if b.get("error"):
                    all_done = False
                    continue
                counts = b.get("counts") or {}
                done_here = sum(v for k, v in counts.items()
                                if k != "processing" and isinstance(v, int))
                if b.get("done"):
                    finished += done_here or 0
                else:
                    finished += done_here or 0
                    all_done = False
            elapsed = datetime.datetime.now() - t0
            hh, rem = divmod(int(elapsed.total_seconds()), 3600)
            mm = rem // 60
            self._batch_progress(f"BATCH: {finished} of {total} requests "
                                 f"finished · {hh}h {mm:02d}m elapsed")
            if all_done:
                break
            slept = 0.0
            while slept < poll_s and self.is_running:
                time.sleep(min(1.0, poll_s - slept))
                slept += 1.0
        # [PATCH:batch] WO-B1-2 Task 3: the batch has ended (or the run was
        # shut down / cancelled) -- stop the keep-alives either way.
        keepalive_stop.set()
        if not self.is_running:
            # SHUT DOWN during a pending job: stop polling and nothing else.
            return

        # Collect results.
        self.log_message("-> BATCH: all provider batches have ended. Collecting "
                         "results and finishing each student...")
        job["state"] = "collecting"
        self._batch_job_save(job)
        results_by_cid = {}
        gone = False
        reused_n = succeeded_n = 0
        for bid_ in ids:
            try:
                r = requests.post(DAEMON_BASE + "/api/batch/results",
                                  json={"id": bid_}, timeout=1800)
                payload = r.json() if r.content else {}
                if r.status_code >= 400:
                    if payload.get("reason") == "results_gone":
                        gone = True
                        self.log_message(f"[!] Results for {bid_} are GONE at the "
                                         f"provider: {payload.get('message', '')}")
                        continue
                    raise Exception(payload.get("message") or f"HTTP {r.status_code}")
                for res in payload.get("results", []):
                    if res.get("custom_id"):
                        results_by_cid[res["custom_id"]] = res
                # [PATCH:batch] WO-B1-2 Task 4: per-batch prompt-reuse counts
                # (the daemon logs the same with the job's token totals).
                reused_n += int(payload.get("reused_prompt") or 0)
                succeeded_n += int(payload.get("succeeded") or 0)
            except Exception as e:
                self.log_message(f"[!] Could not fetch results for {bid_}: {e}. "
                                 "Its requests fall back to the live path below.")
        # [PATCH:batch] WO-B1-2 Task 4: one plain line on prompt reuse, on
        # every route where a stored prompt applies: always on the Anthropic
        # route (an explicit stored prompt is written by the live warm-up
        # student), and on a Google route only when the provider reported
        # cached tokens (Google keeps no explicit stored prompt in BATCH).
        _is_anth_job = (_m.startswith("claude") or "anthropic/" in _m
                        or _m.startswith("publishers/anthropic"))
        if succeeded_n and (_is_anth_job or reused_n):
            self.log_message(f"Batch finished: {reused_n} of {succeeded_n} "
                             "requests reused the stored prompt.")

        # Requests with NO result at all -- results gone at the provider, a
        # submission that may or may not have reached the provider, or a
        # failed fetch. Never re-score those live on our own: say so plainly
        # and let the operator choose. (Requests the provider explicitly reports as
        # errored/expired/cancelled are different: they get one automatic
        # pass through the existing live bounded-retry path below.)
        live_ok_for_missing = True
        missing = [cid for cid in job["requests"] if cid not in results_by_cid]
        if missing:
            n_students = len({job["requests"][cid]["b_id"] for cid in missing})
            if gone:
                why = ("The stored results for some requests were already "
                       "deleted by the provider or the storage bucket "
                       "(results are kept only a limited time). The batch "
                       "price for them is spent either way.")
            else:
                why = ("Some requests have no results to collect -- a "
                       "submission that may or may not have reached the "
                       "provider, or a fetch that failed. Nothing is ever "
                       "re-submitted automatically.")
            choice = self._ask_choice(
                "SOME BATCH RESULTS ARE UNAVAILABLE",
                (f"{why}\n\nThis affects {n_students} student(s).\n\n"
                 "SCORE LIVE NOW evaluates those students again immediately "
                 "at the full live price. DEFER keeps their scans in "
                 "'02 Inbox Scans' for a later run and scores nobody twice."),
                ["SCORE LIVE NOW (full price)", "DEFER THOSE STUDENTS"])
            live_ok_for_missing = choice.startswith("SCORE LIVE")

        # Finish each student through the existing path.
        tabula_records_by_eval_dir = {}
        for d, rec in job.get("tabula", []):
            tabula_records_by_eval_dir.setdefault(d, []).append(rec)
        deferred_ids = set(job.get("deferred", []))
        data = {"display_title": job["assignment"]["display_title"],
                "original_title": job["assignment"]["original_title"],
                "max_possible_score": job["assignment"]["max_score"]}
        group_key = job["assignment"]["group_key"]

        for b_id, srec in job["students"].items():
            if not self.is_running:
                return
            if srec.get("status") == "finished":
                continue
            info = self.roster.get(b_id, {"last": "Unknown", "first": "Student"})
            pages_abs = [os.path.join(BATCH_JOB_DIR, p) for p in srec["pages"]]
            chunk_descs = [self._batch_abs_chunk(job["requests"][cid]["chunk"])
                           for cid in srec["chunks"]]
            merged = []
            student_ok = True
            for cid in srec["chunks"]:
                res = results_by_cid.get(cid)
                desc = self._batch_abs_chunk(job["requests"][cid]["chunk"])
                if res and res.get("state") == "succeeded":
                    try:
                        parsed = self._parse_silo2_text(res.get("text") or "")
                    except Exception as e:
                        self.log_message(f"[!] Could not parse batch result "
                                         f"{cid}: {e}")
                        parsed = []
                    if not isinstance(parsed, list):
                        parsed = []
                else:
                    # Errored, expired, cancelled, or never collected: one
                    # pass through the existing live bounded-retry path --
                    # unless the operator chose to defer the gone ones.
                    state = (res or {}).get("state", "missing")
                    if not live_ok_for_missing and res is None:
                        self.log_message(f"  -> [DEFERRED] ID {b_id}: result "
                                         f"{cid} unavailable and live scoring "
                                         "was declined.")
                        student_ok = False
                        break
                    why = (res or {}).get("message", "")
                    self.log_message(f"-> BATCH: request {cid} came back "
                                     f"'{state}' {why[:160]} -- evaluating that "
                                     "chunk live instead.")
                    parsed = self.evaluate_grid_with_silo_2(desc)
                    if parsed is None:
                        student_ok = False
                        break
                    if not isinstance(parsed, list):
                        parsed = []
                # The count check runs on batch results exactly as on live
                # replies; its re-asks go live through the daemon.
                if isinstance(desc, dict):
                    parsed = self._count_check_chunk(desc, parsed)
                    if parsed is None:
                        student_ok = False
                        break
                merged.extend(parsed)
            if not student_ok:
                if self.is_running:
                    self.log_message(f"  -> [DEFERRED] ID {b_id}: this student's "
                                     "batch results could not be completed - "
                                     "not scored. Scan retained for re-run.")
                    deferred_ids.add(b_id)
                    srec["status"] = "deferred"
                    job["deferred"] = sorted(deferred_ids)
                    self._batch_job_save(job)
                continue
            self._finish_student(group_key, data, b_id, info, pages_abs,
                                 chunk_descs, tabula_records_by_eval_dir,
                                 deferred_ids, first_evaluation=merged)
            srec["status"] = "deferred" if b_id in deferred_ids else "finished"
            job["deferred"] = sorted(deferred_ids)
            job["tabula"] = [[d, rec] for d, recs in
                             tabula_records_by_eval_dir.items() for rec in recs]
            self._batch_job_save(job)
            if not self.is_running:
                return

        # Wrap up exactly like a live run.
        self._write_tabulas(tabula_records_by_eval_dir)
        self._move_scans(job.get("pdf_to_period_map", {}), deferred_ids)
        for bid_ in ids:
            try:
                requests.post(DAEMON_BASE + "/api/batch/cleanup",
                              json={"id": bid_}, timeout=300)
            except Exception:
                pass
        shutil.rmtree(BATCH_JOB_DIR, ignore_errors=True)
        self.log_message("=== BATCH JOB COLLECTED AND CLOSED. ===")
        # [PATCH:box-map] the job record is authoritative for the notices
        # (this may be a RESUME in a fresh session); finish_processing prints
        # the summary after the closing lines.
        self._box_map_notices = list(job.get("box_map_notices", []))
        # [PATCH:run-summary] WO-COS2 T6: the job record is authoritative for
        # the run summary too; an older record without the key -> None, and
        # finish_processing simply prints no summary.
        self._run_summary_stats = job.get("run_summary", None)
        self.finish_processing()

    def _batch_keepalive_prefixes(self, job):
        """[PATCH:batch] WO-B1-2 Task 3: the distinct cached prefixes this
        job's requests read. None stands for the daemon's full-master
        context; a list is a per-chunk master page set (master pages only --
        a keep-alive never carries student content). Built with the same
        helper the live request uses, so the prefix is byte-identical."""
        prefixes = [None]
        seen = {None}
        for rec in (job.get("requests") or {}).values():
            ch = rec.get("chunk") or {}
            fp = ch.get("footer_pages") if isinstance(ch, dict) else None
            m = self._s2ctx_master_images_b64(fp) if fp else None
            key = tuple(m) if m else None
            if key not in seen:
                seen.add(key)
                prefixes.append(m)
        return prefixes

    def _batch_keepalive_loop(self, job, stop_event):
        """[PATCH:batch] WO-B1-2 Task 3: worker loop. Renews each distinct
        cached prefix every LEDGER_BATCH_KEEPALIVE_S seconds (default 2700;
        0 disables) until stop_event is set. Every outcome is logged; no
        failure ever stops or delays polling or collection."""
        try:
            interval = float(os.environ.get("LEDGER_BATCH_KEEPALIVE_S", "2700"))
        except (TypeError, ValueError):
            interval = 2700.0
        if interval <= 0:
            return
        try:
            prefixes = self._batch_keepalive_prefixes(job)
        except Exception as e:
            self.log_message(f"[!] BATCH keep-alive disabled for this job: {e}")
            return
        while not stop_event.wait(interval):
            for m in prefixes:
                if stop_event.is_set():
                    return
                try:
                    payload = {"master_images_b64": m} if m else {}
                    r = requests.post(DAEMON_BASE + "/api/batch/keepalive",
                                      json=payload, timeout=600)
                    data = r.json() if r.content else {}
                    if r.status_code >= 400 or data.get("status") != "success":
                        raise Exception(data.get("message")
                                        or f"HTTP {r.status_code}")
                    u = data.get("usage") or {}
                    self.log_message("-> BATCH keep-alive: prompt cache "
                                     f"renewed (cache_read="
                                     f"{u.get('cache_read')} tokens).")
                except Exception as e:
                    self.log_message("[!] BATCH keep-alive failed (collection "
                                     f"is unaffected): {e}")

    def check_pending_batch_job(self):
        """[PATCH:batch] Startup: a pending job gets a RESUME / CANCEL BATCH
        dialog. Resuming never repeats a paid call and never re-submits."""
        if not os.path.exists(BATCH_JOB_FILE):
            return
        try:
            job = self._batch_job_load()
        except Exception as e:
            self.log_message(f"[!] A pending batch job record exists but could "
                             f"not be read ({e}). It was left untouched at "
                             f"{BATCH_JOB_FILE} -- nothing will run until it is "
                             "resolved.")
            return
        n_students = sum(1 for s in job.get("students", {}).values()
                         if s.get("status") == "pending")
        ambiguous = [s for s in job.get("submissions", [])
                     if not s.get("batch_id")]
        msg = (f"A BATCH scoring job from {job.get('created', '?')[:16]} is "
               f"still pending: {n_students} student(s), model "
               f"{job.get('model', '?')}, assignment "
               f"'{job.get('assignment', {}).get('display_title', '?')}'.\n\n"
               "RESUME goes back to polling and collecting the finished "
               "results (nothing is paid twice).\n\nCANCEL BATCH cancels the "
               "job at the provider, defers the uncollected students and "
               "keeps their scans in '02 Inbox Scans'.")
        if ambiguous:
            msg += ("\n\nNOTE: one submission may or may not have reached the "
                    "provider (the record shows it mid-submit). On RESUME it "
                    "is NEVER re-submitted; its students will be offered live "
                    "scoring or deferral when results are collected.")
        choice = {}

        def build():
            win = ctk.CTkToplevel(self.root)
            win.title("PENDING BATCH JOB")
            self._clamp_toplevel(win, 780, 420)
            win.transient(self.root)
            win.protocol("WM_DELETE_WINDOW", lambda: None)
            ctk.CTkLabel(win, text="A BATCH JOB IS WAITING",
                         font=self.header_font).pack(pady=(24, 10))
            ctk.CTkLabel(win, text=msg, font=self.main_font, wraplength=700,
                         justify="left").pack(pady=8, padx=30)
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(pady=18)

            def pick(what):
                choice["v"] = what
                try:
                    win.grab_release()
                    win.destroy()
                except tk.TclError:
                    pass
                if what == "resume":
                    self.is_running = True
                    self.run_btn.configure(state="disabled")
                    self.max_score_entry.configure(state="disabled")
                    self.mode_selector.configure(state="disabled")
                    self.scoring_selector.configure(state="disabled")
                    self.master_btn.configure(state="disabled")
                    self.update_indicator("running")
                    threading.Thread(target=self._resume_batch_job,
                                     args=(job,), daemon=True).start()
                else:
                    threading.Thread(target=self._cancel_batch_job,
                                     args=(job,), daemon=True).start()

            ctk.CTkButton(row, text="RESUME", font=self.header_font, height=52,
                          width=200, fg_color="#1e8449", hover_color="#27ae60",
                          command=lambda: pick("resume")).pack(side="left", padx=14)
            ctk.CTkButton(row, text="CANCEL BATCH", font=self.header_font,
                          height=52, width=220, fg_color="#c0392b",
                          hover_color="#e74c3c",
                          command=lambda: pick("cancel")).pack(side="left", padx=14)
            self._make_text_copyable(win)
            win.after(200, win.grab_set)

        self.root.after(0, build)

    def _resume_batch_job(self, job):
        """Resume polling/collecting. Any live call made while finishing
        needs the daemon's scoring context; after a restart that context is
        gone, so rebuild it from the saved master path FIRST."""
        try:
            self.scoring_mode = "batch"
            self.grading_mode = job.get("grading_mode", "boxed")
            self._count_check_learned = {}
            self.master_pdf_path = job.get("master_pdf")
            self.global_max_score = job.get("assignment", {}).get("max_score", 0)
            self.master_group_key = job.get("assignment", {}).get("group_key")
            self.master_display_title = job.get("assignment", {}).get("display_title")
            self.master_original_title = job.get("assignment", {}).get("original_title")
            self.log_message("-> RESUME: rebuilding the evaluation context from "
                             "the saved master before any live call...")
            if not self.initialize_silo_2_cache(self.master_pdf_path):
                self.log_message("[!] The evaluation context could not be "
                                 "rebuilt. Collection continues, but any chunk "
                                 "that needs a live call will defer its student.")
            self._batch_poll_and_collect(job)
        except Exception as e:
            self.log_message(f"[!] RESUME failed: {e}. The job record was left "
                             "in place; restart The Ledger to try again.")
            self.finish_processing()

    def _cancel_batch_job(self, job):
        """CANCEL BATCH: cancel at the provider, mark the uncollected
        students deferred, keep their scans."""
        for s in job.get("submissions", []):
            bid_ = s.get("batch_id")
            if not bid_:
                continue
            try:
                r = requests.post(DAEMON_BASE + "/api/batch/cancel",
                                  json={"id": bid_}, timeout=300)
                self._raise_daemon_error(r)
                self.log_message(f"-> Cancelled at the provider: {bid_}")
            except Exception as e:
                self.log_message(f"[!] Could not cancel {bid_}: {e}")
        pending = sorted(b for b, s in job.get("students", {}).items()
                         if s.get("status") == "pending")
        if pending:
            self.log_message(f"-> {len(pending)} student(s) deferred: {pending}. "
                             "Their scans stay in '02 Inbox Scans'; run again "
                             "whenever you like.")
        shutil.rmtree(BATCH_JOB_DIR, ignore_errors=True)
        self.log_message("=== BATCH JOB CANCELLED AND CLEARED. System is Idle. ===")

    def finish_processing(self):
        self.update_indicator("idle")
        self.run_btn.configure(state="normal")
        self.max_score_entry.configure(state="normal")
        self.mode_selector.configure(state="normal")   # [PATCH:essay-mode]
        self.scoring_selector.configure(state="normal")   # [PATCH:batch]
        self.batch_progress_label.configure(text="")      # [PATCH:batch]
        self.master_btn.configure(state="normal")
        self.delete_cache()
        self.clean_temp_directory()
        self.is_running = False
        self.log_message("=== BATCH PROCESSING CONCLUDED. TEMP FOLDER SCRUBBED. ===")
        # [PATCH:run-summary] WO-COS2 T6: the end-of-run summary, AFTER the
        # closing line and BEFORE the ANSWER BOX NOTICES block (which stays
        # last and unchanged). Printed only when Pass 1 actually ran (or a
        # resumed batch job carries its summary), then cleared -- the
        # box_map_notices print-once pattern. Never blocks the finish.
        try:
            _rs = getattr(self, "_run_summary_stats", None)
            if _rs is None and getattr(self, "_pass1_ran", False):
                _rs = self._collect_run_summary_stats()
            if _rs and _rs.get("ran"):
                for _rs_line in build_run_summary(_rs):
                    self.log_message(_rs_line)
            self._run_summary_stats = None
            self._pass1_ran = False
        except Exception as _rs_e:
            try:
                self.log_message(f"[!] Run summary error (ignored): {_rs_e}")
            except Exception:
                pass
        # [PATCH:box-map] repeat every answer-box notice AFTER the final
        # closing lines, LIVE and BATCH alike, so they are the last thing
        # the teacher reads. Printed once, then cleared.
        _notices = getattr(self, "_box_map_notices", None)
        if _notices:
            self.log_message("=== ANSWER BOX NOTICES -- PLEASE VERIFY THESE PAGES ===")
            for _line in _notices:
                self.log_message(f"  {_line}")
            self._box_map_notices = []

    # ======================================================================
    # [PATCH:formatter] WO-F1: FORMAT ASSIGNMENT
    # New code only. The heavy lifting lives in /opt/ledger/formatter/
    # (formatter_core.py — no GUI imports); this window just drives it.
    # ======================================================================
    def get_formatter_setting(self, key, default=""):
        """Read one per-account Settings value (formatter keys only)."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = ?", (key,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if (result and result[0]) else default
        except Exception as e:
            self.log_message(f"[!] Database Read Error ({key}): {e}")
            return default

    def set_formatter_setting(self, key, value):
        """Write one per-account Settings value (formatter keys only)."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value) VALUES (?, ?)",
                (key, str(value)))
            conn.commit()
            conn.close()
        except Exception as e:
            self.log_message(f"[!] Database Write Error ({key}): {e}")

    def _formatter_core(self):
        """Lazy import of formatter_core (keeps GUI startup unchanged)."""
        import importlib
        import sys as _sys
        if FORMATTER_CODE_DIR not in _sys.path:
            _sys.path.insert(0, FORMATTER_CODE_DIR)
        return importlib.import_module("formatter_core")

    def _formatter_set_master(self, pdf_path):
        """Load a formatted PDF through the existing LOAD MASTER PDF path:
        same state, same labels, same downstream behavior."""
        self.master_pdf_path = pdf_path
        filename = os.path.basename(pdf_path)
        self.master_status_label.configure(text=f"MASTER: {filename}", text_color="#1e8449")
        self.log_message(f"Master Copy loaded: {filename}. Ready for batch verification.")

    def open_formatter_window(self):
        if getattr(self, "_fmt_win", None) is not None:
            try:
                if self._fmt_win.winfo_exists():
                    self._fmt_win.lift()
                    return
            except tk.TclError:
                pass
        try:
            core = self._formatter_core()
        except Exception as e:
            self.log_message(f"[!] The formatter is not installed correctly: {e}")
            return

        win = ctk.CTkToplevel(self.root)
        self._fmt_win = win
        win.title("FORMAT ASSIGNMENT")
        # The reference display is 1360x768; everything must fit in 1280x700.
        _sw, _sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        win.geometry(f"{min(1270, _sw - 40)}x{min(690, _sh - 60)}")
        win.minsize(1000, 620)

        ui_font = ctk.CTkFont(family="Ubuntu", size=14)
        small_font = ctk.CTkFont(family="Ubuntu", size=12)
        conv_font = ctk.CTkFont(family="Ubuntu", size=16)

        st = {"session": None, "file": None, "pasted": None, "view": None,
              "previews": [],
              # [PATCH:fmt-ticker] WO-APP1 T6: elapsed-time ticker state
              "job_started": None, "job_note": ""}
        self.formatter_busy = False

        win.grid_columnconfigure(0, weight=1)
        win.grid_columnconfigure(1, weight=0)
        win.grid_rowconfigure(1, weight=1)

        # ---------------- top strip ----------------
        top = ctk.CTkFrame(win, fg_color="#1a1a1a")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))

        rowA = ctk.CTkFrame(top, fg_color="transparent")
        rowA.pack(fill="x", padx=8, pady=(8, 2))

        file_label = ctk.CTkLabel(rowA, text="No file chosen", font=small_font,
                                  text_color="#9a9a9a", width=190, anchor="w")

        def choose_file():
            try:
                result = subprocess.run(
                    ['zenity', '--file-selection', '--title=CHOOSE THE ASSIGNMENT',
                     '--file-filter=Assignments | *.pdf *.png *.jpg *.jpeg *.txt *.docx'],
                    capture_output=True, text=True)
                path = result.stdout.strip()
            except FileNotFoundError:
                path = filedialog.askopenfilename(
                    title="CHOOSE THE ASSIGNMENT",
                    filetypes=[("Assignments", "*.pdf *.png *.jpg *.jpeg *.txt *.docx")])
            if not path:
                return
            if path.lower().endswith(".docx") and not (shutil.which("libreoffice") or shutil.which("soffice")):
                say_ledger("Please save it as a PDF first.")
                return
            st["file"] = path
            st["pasted"] = None
            file_label.configure(text=os.path.basename(path), text_color="white")

        def paste_text_dialog():
            d = ctk.CTkToplevel(win)
            d.title("Paste the questions")
            d.geometry("640x440")
            d.transient(win)
            box = ctk.CTkTextbox(d, font=ui_font, wrap="word")
            box.pack(fill="both", expand=True, padx=12, pady=(12, 6))

            def ok():
                text = box.get("1.0", "end").strip()
                if text:
                    st["pasted"] = text
                    st["file"] = None
                    file_label.configure(text=f"pasted text ({len(text)} characters)",
                                         text_color="white")
                d.destroy()
            ctk.CTkButton(d, text="USE THIS TEXT", font=ui_font,
                          fg_color="#1e8449", hover_color="#27ae60",
                          command=ok).pack(pady=(0, 12))
            self._make_text_copyable(d)
            d.after(200, d.grab_set)

        ctk.CTkButton(rowA, text="Choose File", font=ui_font, width=110,
                      fg_color="#1f538d", hover_color="#14375e",
                      command=choose_file).pack(side="left", padx=(0, 6))
        ctk.CTkButton(rowA, text="Paste Text", font=ui_font, width=90,
                      fg_color="#333333", hover_color="#555555",
                      command=paste_text_dialog).pack(side="left", padx=(0, 6))
        file_label.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(rowA, text="Title:", font=ui_font).pack(side="left")
        title_entry = ctk.CTkEntry(rowA, font=ui_font, width=260)
        title_entry.pack(side="left", padx=(4, 10))

        recent_values = ["Recent..."] + [name for name, _p in core.list_recent()][:12]
        recent_var = ctk.StringVar(value="Recent...")

        def open_recent(choice):
            if choice == "Recent...":
                return
            recent_var.set("Recent...")
            for name, p in core.list_recent():
                if name == choice:
                    load_session(p)
                    return

        ctk.CTkOptionMenu(rowA, variable=recent_var, values=recent_values,
                          font=small_font, width=160,
                          command=open_recent).pack(side="right")

        rowB = ctk.CTkFrame(top, fg_color="transparent")
        rowB.pack(fill="x", padx=8, pady=(2, 2))

        ctk.CTkLabel(rowB, text="Your ID Name:", font=ui_font).pack(side="left")
        # [PATCH:id-compose] WO-APP1 T3: the field takes the NAME only; the
        # printed label composes as "<name> ID" (id_label_compose). A hint
        # under the field shows live what will print. Matches the browser.
        id_entry = ctk.CTkEntry(rowB, font=ui_font, width=190,
                                placeholder_text="Example: Smithian")
        id_entry.pack(side="left", padx=(4, 2))
        id_hint = ctk.CTkLabel(rowB, text="", font=small_font,
                               text_color="#7f8c8d", anchor="w")
        id_hint.pack(side="left", padx=(0, 12))

        def _update_id_hint(_event=None):
            composed = id_label_compose(id_entry.get())
            id_hint.configure(
                text=f"Will print: {composed}" if composed else "")
        id_entry.bind("<KeyRelease>", _update_id_hint)

        # Prefill: this account's saved formatter name first; otherwise the
        # roster label UNLESS it is still the factory default (then the field
        # stays empty and Send is refused until a name is typed). Old saved
        # values and roster labels may be FULL labels -- strip on read.
        _saved_fmt_id = self.get_formatter_setting("formatter_id_label", "")
        _roster_id = self.get_custom_id_label()
        if _saved_fmt_id:
            id_entry.insert(0, id_label_strip(_saved_fmt_id))
        elif _roster_id != "Bartonian ID":
            id_entry.insert(0, id_label_strip(_roster_id))
        _update_id_hint()

        type_selector = ctk.CTkSegmentedButton(
            rowB, values=["Assignment", "Quiz", "Exam"], font=ui_font, height=30)
        type_selector.set("Assignment")
        type_selector.pack(side="left", padx=(0, 12))

        key_var = ctk.StringVar(value="off")
        ctk.CTkCheckBox(rowB, text="Make Answer Key", font=ui_font,
                        variable=key_var, onvalue="on", offvalue="off").pack(side="left")

        roster_note = ctk.CTkLabel(top, text="", font=small_font,
                                   text_color="#7f8c8d", anchor="w")
        roster_note.pack(fill="x", padx=10, pady=(0, 6))

        # ---------------- left: the conversation ----------------
        left = ctk.CTkFrame(win, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=4)
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        conv = ctk.CTkTextbox(left, font=conv_font, wrap="word",
                              fg_color="#121212", corner_radius=10)
        conv.grid(row=0, column=0, columnspan=2, sticky="nsew")
        conv_tk = getattr(conv, "_textbox", conv)
        conv_tk.tag_config("teacher", foreground="#7fb3ff")
        conv_tk.tag_config("ledger", foreground="#2ecc71")
        conv_tk.tag_config("quiet", foreground="#8a8a8a")
        conv.configure(state="disabled")

        def _append_conv(text, tag):
            conv.configure(state="normal")
            conv_tk.insert("end", text + "\n\n", tag)
            conv.configure(state="disabled")
            conv_tk.see("end")

        def say_teacher(text):
            _append_conv("You:  " + text, "teacher")

        def say_ledger(text):
            _append_conv("Ledger:  " + text, "ledger")

        def say_quiet(text):
            _append_conv(text, "quiet")

        entry_box = ctk.CTkTextbox(left, font=conv_font, height=64, wrap="word")
        entry_box.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        entry_tk = getattr(entry_box, "_textbox", entry_box)

        send_btn = ctk.CTkButton(left, text="Send", font=self.main_font,
                                 width=90, height=60, fg_color="#1e8449",
                                 hover_color="#27ae60")
        send_btn.grid(row=1, column=1, sticky="ns", padx=(6, 0), pady=(6, 0))

        # ---------------- right: previews ----------------
        right = ctk.CTkFrame(win, fg_color="#1a1a1a", width=400)
        right.grid(row=1, column=1, sticky="nsew", padx=(4, 8), pady=4)
        right.grid_propagate(False)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        view_label = ctk.CTkLabel(right, text="No version yet", font=ui_font)
        view_label.grid(row=0, column=0, pady=(8, 2))

        pv_frame = ctk.CTkScrollableFrame(right, fg_color="#101010")
        pv_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=2)

        btn_row = ctk.CTkFrame(right, fg_color="transparent")
        btn_row.grid(row=2, column=0, pady=(4, 8))

        def _shown_version():
            sess = st["session"]
            if not sess or not sess.state["versions"]:
                return None
            idx = st["view"]
            if idx is None:
                idx = len(sess.state["versions"]) - 1
            return sess.state["versions"][idx]

        def show_version(idx=None):
            sess = st["session"]
            for w in pv_frame.winfo_children():
                w.destroy()
            st["previews"] = []
            if not sess or not sess.state["versions"]:
                view_label.configure(text="No version yet")
                return
            n_total = len(sess.state["versions"])
            if idx is None:
                idx = n_total - 1
            idx = max(0, min(idx, n_total - 1))
            st["view"] = idx
            v = sess.state["versions"][idx]
            view_label.configure(text=f"Version {v['n']} of {n_total}")
            for p in v.get("previews", []):
                try:
                    img = Image.open(p)
                    w = 352
                    h = int(img.height * w / img.width)
                    cimg = ctk.CTkImage(light_image=img, dark_image=img, size=(w, h))
                    st["previews"].append(cimg)
                    ctk.CTkLabel(pv_frame, image=cimg, text="").pack(pady=4)
                except Exception:
                    continue

        def open_pdf():
            v = _shown_version()
            if v:
                subprocess.Popen(["xdg-open", v["pdf_path"]])

        def previous_version():
            sess = st["session"]
            if not sess or not sess.state["versions"]:
                return
            n_total = len(sess.state["versions"])
            idx = st["view"] if st["view"] is not None else n_total - 1
            show_version((idx - 1) % n_total)

        def use_as_master():
            v = _shown_version()
            if not v:
                return
            sess = st["session"]
            base = re.sub(r'[^A-Za-z0-9 _()\-]', '', sess.state["title"]).strip() or "Formatted Assignment"
            dest = os.path.join(MASTER_DIR, f"{base} v{v['n']}.pdf")
            try:
                shutil.copy(v["pdf_path"], dest)
            except Exception as e:
                say_ledger(f"Could not copy into 01 Master Assignments: {e}")
                return
            self._formatter_set_master(dest)
            say_ledger(f"Version {v['n']} was copied into 01 Master Assignments "
                       "and loaded as the master.")

        ctk.CTkButton(btn_row, text="Open PDF", font=small_font, width=100,
                      fg_color="#1f538d", hover_color="#14375e",
                      command=open_pdf).pack(side="left", padx=3)
        ctk.CTkButton(btn_row, text="Previous Version", font=small_font, width=120,
                      fg_color="#333333", hover_color="#555555",
                      command=previous_version).pack(side="left", padx=3)
        ctk.CTkButton(btn_row, text="Use As Master", font=small_font, width=110,
                      fg_color="#1e8449", hover_color="#27ae60",
                      command=use_as_master).pack(side="left", padx=3)

        # ---------------- bottom edge ----------------
        bottom = ctk.CTkFrame(win, fg_color="transparent")
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkLabel(bottom, text="Formatter Model:", font=small_font).pack(side="left")
        model_entry = ctk.CTkEntry(bottom, font=small_font, width=200)
        model_entry.insert(0, self.get_formatter_setting("formatter_model",
                                                         "claude-fable-5"))
        model_entry.pack(side="left", padx=(4, 12))
        status_label = ctk.CTkLabel(bottom, text="", font=small_font,
                                    text_color="#e67e22", anchor="w")
        status_label.pack(side="left", fill="x", expand=True)

        # ---------------- transcript helpers ----------------
        def record(role, text):
            sess = st["session"]
            if sess is not None:
                sess.state.setdefault("transcript", []).append(
                    {"role": role, "text": text})
                sess.save()

        def load_session(folder):
            try:
                sess = core.FormatterSession.load(folder)
            except Exception as e:
                say_ledger(f"Could not reopen that one: {e}")
                return
            st["session"] = sess
            st["view"] = None
            conv.configure(state="normal")
            conv_tk.delete("1.0", "end")
            conv.configure(state="disabled")
            title_entry.delete(0, "end")
            title_entry.insert(0, sess.state.get("title", ""))
            id_entry.delete(0, "end")
            # [PATCH:id-compose] old sessions hold the full label -- show
            # the bare name; the label re-composes on Send.
            id_entry.insert(0, id_label_strip(sess.state.get("id_label", "")))
            _update_id_hint()
            type_selector.set(sess.state.get("doc_type", "Assignment"))
            key_var.set("on" if sess.state.get("make_key") else "off")
            model_entry.delete(0, "end")
            model_entry.insert(0, sess.state.get("model", "claude-fable-5"))
            file_label.configure(text="(reopened)", text_color="white")
            st["file"] = "(loaded)"
            for turn in sess.state.get("transcript", []):
                if turn["role"] == "teacher":
                    say_teacher(turn["text"])
                elif turn["role"] == "quiet":
                    say_quiet(turn["text"])
                else:
                    say_ledger(turn["text"])
            show_version()

        # ---------------- send ----------------
        # [PATCH:fmt-ticker] WO-APP1 T6: while a job runs, the status line
        # ticks the elapsed time every couple of seconds (a long first
        # model call otherwise looks hung). Any note from the worker's log
        # callback rides along after the elapsed time.
        def _compose_status():
            elapsed = formatter_elapsed_label(time.time() - st["job_started"])
            text = ("Working on it. This usually takes a few minutes.  "
                    "Elapsed: " + elapsed)
            if st["job_note"]:
                text += "  (" + st["job_note"] + ")"
            return text

        def _tick_status():
            if not self.formatter_busy or st["job_started"] is None:
                return                      # job ended: stop cleanly
            try:
                status_label.configure(text=_compose_status())
                self.root.after(2000, _tick_status)
            except tk.TclError:
                pass                        # window closed mid-job

        def _set_job_note(note):
            st["job_note"] = note
            if self.formatter_busy and st["job_started"] is not None:
                try:
                    status_label.configure(text=_compose_status())
                except tk.TclError:
                    pass

        def _finish_job(result_text_fn):
            self.formatter_busy = False
            st["job_started"] = None        # [PATCH:fmt-ticker] stop ticker
            st["job_note"] = ""
            send_btn.configure(state="normal")
            status_label.configure(text="")
            result_text_fn()

        def _worker(instruction, new_session_args):
            core_mod = core
            try:
                if new_session_args is not None:
                    sess = core_mod.FormatterSession.create(**new_session_args)
                    st["session"] = sess
                else:
                    sess = st["session"]
                # keep per-send choices in the session
                sess.state["model"] = new_model[0]
                sess.state["id_label"] = new_label[0]
                sess.state["doc_type"] = new_type[0]
                sess.state["make_key"] = new_key[0]
                result = sess.send(instruction,
                                   log=lambda m: self.root.after(
                                       0, lambda mm=m: _set_job_note(mm)))
            except Exception as e:
                msg = str(e)

                def fail():
                    say_ledger(msg)
                    record("ledger", msg)
                self.root.after(0, _finish_job, fail)
                return

            def done():
                if not result["ok"]:
                    say_ledger(result["error"] or "Something went wrong.")
                    record("ledger", result["error"] or "Something went wrong.")
                    return
                reply_parts = []
                if result["note"]:
                    reply_parts.append(result["note"])
                reply_parts.append(result["check_text"])
                for wmsg in result["warnings"]:
                    reply_parts.append(wmsg)
                reply_parts.append("Let me know if you need to make "
                                   "additional formatting adjustments.")
                reply = "\n\n".join(reply_parts)
                say_ledger(reply)
                record("ledger", reply)
                st["view"] = None
                show_version()
            self.root.after(0, _finish_job, done)

        new_model = [""]
        new_label = [""]
        new_type = ["Assignment"]
        new_key = [False]

        def send_message(event=None):
            if self.formatter_busy:
                return "break"
            if self.is_running:
                say_ledger("The Ledger is evaluating right now. The formatter "
                           "can start when the run finishes.")
                return "break"
            # [PATCH:id-compose] compose the printed label from the typed
            # name; strip-then-compose means a pasted full label stays sane.
            id_name = id_label_strip(id_entry.get())
            id_label = id_label_compose(id_entry.get())
            if not id_label:
                say_ledger("Type your ID name first. It prints at the top of "
                           "every page.")
                return "break"
            instruction = entry_box.get("1.0", "end").strip()
            new_session_args = None
            if st["session"] is None:
                title = title_entry.get().strip()
                if not (st["file"] or st["pasted"]):
                    say_ledger("Choose your assignment file (or paste the "
                               "questions as text) first.")
                    return "break"
                if not title:
                    say_ledger("Type a title first. It prints at the top of "
                               "every page.")
                    return "break"
                new_session_args = {
                    "title": title, "id_label": id_label,
                    "doc_type": type_selector.get(),
                    "make_key": key_var.get() == "on",
                    "model": model_entry.get().strip() or "claude-fable-5",
                    "file_path": st["file"] if st["file"] not in (None, "(loaded)") else None,
                    "pasted_text": st["pasted"],
                }
                if not instruction:
                    instruction = "Format this assignment in Ledger format."
            elif not instruction:
                say_ledger("Type what you would like changed, then press Send.")
                return "break"

            # persist the per-account preferences
            self.set_formatter_setting("formatter_model",
                                       model_entry.get().strip() or "claude-fable-5")
            # [PATCH:id-compose] stored value going forward: the BARE name
            # (composed at use, the browser's way). Old full-label values
            # are stripped on read, so both generations coexist.
            self.set_formatter_setting("formatter_id_label", id_name)
            _roster = self.get_custom_id_label()
            # [PATCH:id-compose] compare the COMPOSED label with the roster's
            if _roster != id_label:
                note_text = (f'Your roster uses "{_roster}". Change it in '
                             "ROSTER if you want reports to match.")
                roster_note.configure(text=note_text)

            new_model[0] = model_entry.get().strip() or "claude-fable-5"
            new_label[0] = id_label
            new_type[0] = type_selector.get()
            new_key[0] = key_var.get() == "on"

            say_teacher(instruction)
            record("teacher", instruction)
            entry_box.delete("1.0", "end")
            self.formatter_busy = True
            send_btn.configure(state="disabled")
            # [PATCH:fmt-ticker] start the elapsed-time ticker
            st["job_started"] = time.time()
            st["job_note"] = ""
            _tick_status()
            threading.Thread(target=_worker,
                             args=(instruction, new_session_args),
                             daemon=True).start()
            return "break"

        send_btn.configure(command=send_message)
        entry_tk.bind("<Return>", send_message)
        entry_tk.bind("<Shift-Return>", lambda e: None)

        say_ledger("Choose your assignment, add any special instructions, "
                   "and press Send.")

        # Test seam, same pattern as self._wizard_hooks: lets the WO-F1
        # acceptance driver press the real buttons. Never read in production.
        self._fmt_hooks = {"send": send_message, "entry": entry_box,
                           "id": id_entry, "id_hint": id_hint,  # [PATCH:id-compose]
                           "title": title_entry,
                           "model": model_entry, "conv": conv,
                           "status": status_label, "load_session": load_session,
                           "show_version": show_version,
                           "use_as_master": use_as_master,
                           "previous_version": previous_version,
                           "file_label": file_label, "state": st, "win": win}

        self._make_text_copyable(win)

if __name__ == "__main__":
    import socket
    import sys
    import subprocess
    
    # --- PARAGON SINGLE INSTANCE LOCK ---
    try:
        # Create a Linux Abstract Socket (denoted by the leading \0)
        # It exists only in RAM and is instantly destroyed if the app or OS crashes.
        single_instance_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        single_instance_socket.bind('\0paragon_ledger_lock')
    except socket.error:
        # The socket is already claimed. Bring the existing window to the front.
        try:
            # -F -a matches the FULL window title exactly (not a substring, so a
            # browser tab titled "The Ledger ..." is never raised by mistake),
            # switches workspaces if needed, and raises the window
            subprocess.run(['wmctrl', '-F', '-a', MAIN_WINDOW_TITLE], check=False)
        except FileNotFoundError:
            pass # Fails gracefully if wmctrl happens to be missing
        
        sys.exit(0)
        
    root = ctk.CTk()
    app = LedgerGUI(root)
    root.mainloop()
