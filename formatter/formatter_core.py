"""formatter_core.py — [PATCH:formatter] WO-F1: the assignment formatter.

All intake, model-loop, run and check logic for the FORMAT ASSIGNMENT
window, with NO GUI imports, so a test script can drive the whole thing.
The window only calls into this module.

How it works: the AI model writes a short
Python build script that uses the layout library in
/opt/ledger/formatter/scripts/. This box runs that script
(format_runner.py), checks the result with the evaluation pipeline's own
readers (format_check.py), and shows the PDF to the teacher. The model
never sends a PDF; it sends a script.

Cost shape (work-order §2.7): a follow-up never resends the whole chat.
Each model call carries: the byte-stable cached system prompt, the source
assignment, the current build script, the teacher's earlier instructions
as a short list, and the new instruction.
"""
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import format_runner  # noqa: E402
import format_check   # noqa: E402

FORMATTER_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser("~")
WORKSPACE_DIR = os.path.join(HOME_DIR, "Ledger Workspace")
FORMATTED_DIR = os.path.join(WORKSPACE_DIR, "06 Formatted Assignments")

# The daemon port. LEDGER_PORT is the test seam (tests run their own daemon
# copy on a spare port); production never sets it.
DAEMON_PORT = int(os.environ.get("LEDGER_PORT") or "5000")
DAEMON_URL = f"http://127.0.0.1:{DAEMON_PORT}"

MAX_MODEL_CALLS = 5          # hard cap per teacher message (work order §2.6)
MAX_CRASH_FIXES = 2
POLL_INTERVAL_S = 2.0
JOB_TIMEOUT_S = 1500.0
PAGE_RENDER_DPI = 150        # source pages shown to the model
RESULT_RENDER_DPI = 110      # finished pages for the looks check / previews

_PY_BLOCK = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# System prompt — assembled ONCE, byte-stable, so it caches (§2.4)
# ---------------------------------------------------------------------------
_ROLE_AND_CONTRACT = """\
You are the Ledger Assignment Formatter. A teacher hands you an existing \
assignment (page images, extracted text, and sometimes the original PDF and \
its embedded pictures as named files). You re-typeset it in the strict \
Ledger format by writing a Python build script that uses the author-approved \
layout library whose full source appears below. You never send a PDF; the \
machine you run on executes your script and checks the result.

OUTPUT CONTRACT — follow it exactly on every reply that delivers a script:

1. Reply with a SHORT plain-English note to the teacher (what you did, and \
anything you were unsure of), then exactly ONE fenced Python code block \
(```python ... ```) containing the COMPLETE build script. No other code \
blocks, no partial scripts, no diffs.
2. The script runs in a working directory that already contains the five \
library files (ledger_lib.py, widgets.py, exam_widgets.py, compact.py, \
key_lib.py) and any named asset image files. Begin it like the examples: \
insert its own directory on sys.path, then import from the library.
3. The script writes its PDF to the path in the LEDGER_OUT_PDF environment \
variable and prints, as its one final line: \
LEDGER_BOXES=<number of answer boxes drawn> LEDGER_PAGES=<page count>. \
Count one per tagged answer box you draw (every page.box / bubble_strip / \
grid_problem / id_box / work_box / sketch_box / lined_box_fa / fig_box). \
Nested FINAL ANSWER cells and purely printed figures do not count.
4. Change LAYOUT ONLY. Keep the teacher's questions word for word. If you \
fix an obvious typo, say so in the note.
5. Number the questions 1..n if the source is unnumbered.
6. The ID label printed in the header comes from the request (for example \
"Bartonian ID" or "Smithian ID"); NEVER hard-code one. Set it with \
`import ledger_lib` then `ledger_lib.ID_LABEL = "<label from the request>"` \
BEFORE creating the Doc.
7. Produce an answer key ONLY when the request asks for one. A key mirrors \
the student layout and adds "(KEY)" to the title.
8. OVERRIDE of the library skill's exam rule: do NOT produce a parallel \
practice exam unless the teacher asks for one. One document per request.
9. Size answer boxes generously for the expected handwriting — undersized \
boxes are the most common teacher complaint. Use ONLY the box constants the \
library defines; same-type boxes are identical document-wide. When in doubt \
between two allowed sizes, take the bigger one.
10. NEVER carry over a Name / Period / Date line, a source website footer, \
or any other printed text in the footer zone. The Ledger header and the \
black-square page-number footer replace them.
11. Pictures: a picture a question depends on must be placed with \
page.image() using the exact asset file name given in the request. Pictures \
that are only decoration may be left out — but then say so in your note.
12. The script must be deterministic, finish well inside 120 seconds, and \
use no network.
13. Every place a student writes an answer must be a tagged answer box the \
Ledger can detect — including hypothesis lines, data tables (draw the \
table's writing area inside ONE tagged box, with the table's own grid lines \
drawn inside it), graphs (use a tagged grid box), and each separately \
evaluated part (tag sub-parts 5a, 5b, ... so every tag is unique).
14. When the teacher asks to make an answer box "bigger" or "larger" or \
wants "more room", move THAT box to the NEXT standard size up among the \
library's box constants (the worksheet ladder: SMALL 180x42 -> SHORT \
320x50 -> LONG 460x54 -> full-width lined LINED_H 104; exam boxes step up \
their own constants likewise). EXACTLY ONE box changes, by ONE step - it \
goes to a full-page box only when the teacher says so in words ("full \
page", "as big as possible"). If the teacher names a question that has \
several sub-boxes, pick the one they most likely mean, change only it, \
and flag the choice in your note. The other boxes keep their sizes (the \
same-type rule then applies to the changed box's NEW type). Your note \
must say which size the box moved to.

The rest of this prompt is the Ledger format skill, the hard format rules, \
the full library source (your API reference — import it, never re-derive \
layout), and four accepted example build scripts.
"""
# [PATCH:formatter] WO-F2 Part A: output-contract rule 14 above — a
# "bigger"/"larger"/"more room" request moves that one box to the NEXT
# standard size up; full-page only when the teacher says so in words.


def _read_kit(relpath):
    with open(os.path.join(FORMATTER_DIR, relpath), "r",
              encoding="utf-8") as fh:
        return fh.read()


_SYSTEM_PROMPT_CACHE = None


def assemble_system_prompt():
    """Order per work-order §2.4; assembled once, byte-stable."""
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        parts = [_ROLE_AND_CONTRACT]
        parts.append("\n\n===== SKILL.md =====\n\n" + _read_kit("SKILL.md"))
        parts.append("\n\n===== references/format-spec.md =====\n\n"
                     + _read_kit(os.path.join("references", "format-spec.md")))
        for lf in ["ledger_lib.py", "widgets.py", "exam_widgets.py",
                   "compact.py", "key_lib.py"]:
            parts.append(f"\n\n===== scripts/{lf} =====\n\n"
                         + _read_kit(os.path.join("scripts", lf)))
        for ex in ["example_build.py",
                   os.path.join("examples", "example_exam_build.py"),
                   os.path.join("examples", "example_practice_build.py"),
                   os.path.join("examples", "example_key_build.py")]:
            parts.append(f"\n\n===== scripts/{ex} =====\n\n"
                         + _read_kit(os.path.join("scripts", ex)))
        _SYSTEM_PROMPT_CACHE = "".join(parts)
    return _SYSTEM_PROMPT_CACHE


# ---------------------------------------------------------------------------
# Intake (§2.5)
# ---------------------------------------------------------------------------
def _b64_file(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _media_type(path):
    ext = os.path.splitext(path)[1].lower()
    return {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp"}.get(ext, "image/png")


def load_source(file_path=None, pasted_text=None, work_dir=None):
    """Take in the teacher's assignment. Returns a source dict:
        kind          'pdf' | 'image' | 'text'
        pdf_path      path of the (possibly converted) PDF, or None
        text          extracted text ('' when none)
        page_images   [png paths] one per page
        assets        [(file_name, path)] embedded pictures pulled from the
                      source PDF, saved as image files for the build script
        notes         [str] anything the teacher should know about intake
    Raises ValueError with a plain-language message on unusable input.
    """
    import fitz

    if work_dir is None:
        work_dir = format_runner.new_scratch()
    os.makedirs(work_dir, exist_ok=True)
    notes = []

    if file_path is None and pasted_text:
        return {"kind": "text", "pdf_path": None, "text": pasted_text,
                "page_images": [], "assets": [], "notes": notes,
                "work_dir": work_dir}
    if file_path is None:
        raise ValueError("Choose a file or paste the questions as text first.")

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".docx":
        if shutil.which("libreoffice") is None and shutil.which("soffice") is None:
            raise ValueError("Please save it as a PDF first.")
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        proc = subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                               "--outdir", work_dir, file_path],
                              capture_output=True, text=True, timeout=180)
        converted = os.path.join(
            work_dir,
            os.path.splitext(os.path.basename(file_path))[0] + ".pdf")
        if proc.returncode != 0 or not os.path.exists(converted):
            raise ValueError("The .docx could not be converted. "
                             "Please save it as a PDF first.")
        notes.append("The Word file was converted to PDF for intake.")
        file_path = converted
        ext = ".pdf"

    if ext in (".txt", ".md"):
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return {"kind": "text", "pdf_path": None, "text": fh.read(),
                    "page_images": [], "assets": [], "notes": notes,
                    "work_dir": work_dir}

    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif",
               ".tiff"):
        dest = os.path.join(work_dir, "source_page_1.png")
        from PIL import Image
        with Image.open(file_path) as im:
            im.convert("RGB").save(dest)
        return {"kind": "image", "pdf_path": None, "text": "",
                "page_images": [dest], "assets": [], "notes": notes,
                "work_dir": work_dir}

    if ext != ".pdf":
        raise ValueError("Choose a PDF, image, or text file "
                         "(or paste the questions as text).")

    doc = fitz.open(file_path)
    page_images, text_parts, assets = [], [], []
    seen_xrefs = set()
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=PAGE_RENDER_DPI)
        p_path = os.path.join(work_dir, f"source_page_{i + 1}.png")
        pix.save(p_path)
        page_images.append(p_path)
        t = page.get_text().strip()
        if t:
            text_parts.append(f"--- source page {i + 1} ---\n{t}")
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                info = doc.extract_image(xref)
                img_ext = info.get("ext", "png")
                if info.get("width", 0) < 40 or info.get("height", 0) < 40:
                    continue
                name = f"source_image_p{i + 1}_{len(assets) + 1}.{img_ext}"
                a_path = os.path.join(work_dir, name)
                with open(a_path, "wb") as fh:
                    fh.write(info["image"])
                assets.append((name, a_path))
            except Exception:  # noqa: BLE001 — a bad embedded image never blocks intake
                continue
    doc.close()
    return {"kind": "pdf", "pdf_path": file_path,
            "text": "\n\n".join(text_parts), "page_images": page_images,
            "assets": assets, "notes": notes, "work_dir": work_dir}


# ---------------------------------------------------------------------------
# Daemon client
# ---------------------------------------------------------------------------
def _plain_error(message):
    """Map a daemon/provider error onto the window's plain-English wording."""
    low = (message or "").lower()
    if ("not_found" in low or "404" in low
            or ("model" in low and ("not found" in low or "not exist" in low
                                    or "unknown" in low or "invalid" in low))):
        return ("That model name is no longer available. Type the new one in "
                "Formatter Model below.")
    if "ledger_anthropic_api_key" in low or ("anthropic" in low and "key" in low):
        return ("This model needs the LEDGER_ANTHROPIC_API_KEY on this box, "
                "and it is missing. The box owner must add it to "
                "/etc/paragon/.env and restart the daemon.")
    if "prepayment credits are depleted" in low or ("402" in low and "resource_exhausted" in low):
        return ("Google says the prepaid credits on this box's Google AI "
                "Studio account are used up. The box owner must add credit "
                "or enable billing at ai.studio/projects, or use a claude "
                "model instead.")
    if ("google" in low or "gemini" in low) and ("credential" in low or "key" in low
                                                 or "no google" in low):
        return ("This model needs the LEDGER_GOOGLE_API_KEY on this box, and "
                "it is missing. The box owner must add it to "
                "/etc/paragon/.env and restart the daemon.")
    return message


# [PATCH:formatter] WO-F2 Part B plumbing: Vertex enforces per-minute
# quotas and answers 429 RESOURCE_EXHAUSTED mid-conversation. Those are
# transient — wait and retry before giving up. A depleted-credit error is
# NOT transient and is never retried.
_TRANSIENT_RETRIES = 2
_TRANSIENT_WAIT_S = 45.0


def _is_transient(message):
    low = (message or "").lower()
    if "depleted" in low or "402" in low:
        return False
    return any(m in low for m in ("429", "resource_exhausted",
                                  "resource exhausted", "try again later",
                                  "overloaded", "503", "unavailable"))


def call_model(model_name, messages, log=None):
    """One formatter call through the daemon. Returns (text, usage).
    Raises RuntimeError with a plain-language message on failure.
    [PATCH:formatter] transient provider rate-limits retry with backoff."""
    last_exc = None
    for attempt in range(_TRANSIENT_RETRIES + 1):
        if attempt:
            wait = _TRANSIENT_WAIT_S * attempt
            if log:
                log(f"the provider is rate-limiting; waiting {int(wait)}s "
                    f"and retrying ({attempt}/{_TRANSIENT_RETRIES})...")
            time.sleep(wait)
        try:
            return _call_model_once(model_name, messages, log=log)
        except RuntimeError as exc:
            if _is_transient(str(exc)) and attempt < _TRANSIENT_RETRIES:
                last_exc = exc
                continue
            raise
    raise last_exc


def _call_model_once(model_name, messages, log=None):
    import requests

    payload = {"model_name": model_name,
               "system": assemble_system_prompt(),
               "messages": messages}
    resp = requests.post(f"{DAEMON_URL}/api/formatter/start", json=payload,
                         timeout=120)
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"The local daemon gave no usable reply "
                           f"(HTTP {resp.status_code}).")
    if resp.status_code != 200 or data.get("status") != "success":
        raise RuntimeError(_plain_error(data.get("message",
                                                 f"HTTP {resp.status_code}")))
    job_id = data["job_id"]
    deadline = time.time() + JOB_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        st = requests.get(f"{DAEMON_URL}/api/formatter/status",
                          params={"job_id": job_id}, timeout=30).json()
        state = st.get("state")
        if state == "done":
            if log:
                log(f"model call finished (usage: {st.get('usage')})")
            return st.get("text") or "", st.get("usage") or {}
        if state == "error":
            raise RuntimeError(_plain_error(st.get("message", "model error")))
    raise RuntimeError("The model took too long and the request was "
                       "abandoned. Try Send again.")


def parse_reply(text):
    """Split a model reply into (note, script|None). Exactly one fenced
    python block is expected; with several, the last complete one wins."""
    blocks = _PY_BLOCK.findall(text or "")
    script = blocks[-1].strip() + "\n" if blocks else None
    note = _PY_BLOCK.sub("", text or "").strip()
    return note, script


# ---------------------------------------------------------------------------
# Message assembly (§2.5 / §2.7 — flat, never the whole conversation)
# ---------------------------------------------------------------------------
def _source_content(source, include_document=True):
    content = []
    if source["text"]:
        content.append({"type": "text",
                        "text": "FULL TEXT EXTRACTED FROM THE SOURCE "
                                "ASSIGNMENT:\n\n" + source["text"]})
    if source["pdf_path"] and include_document:
        content.append({"type": "document",
                        "media_type": "application/pdf",
                        "data": _b64_file(source["pdf_path"])})
    for i, p in enumerate(source["page_images"], 1):
        content.append({"type": "text", "text": f"Source page {i} as an image:"})
        content.append({"type": "image", "media_type": "image/png",
                        "data": _b64_file(p)})
    if source["assets"]:
        content.append({"type": "text", "text":
                        "The following picture files were pulled out of the "
                        "source and will be PRESENT IN YOUR SCRIPT'S WORKING "
                        "DIRECTORY under exactly these names. Place any "
                        "picture a question depends on with page.image(); "
                        "purely decorative ones may be left out (say so in "
                        "your note)."})
        for name, path in source["assets"]:
            content.append({"type": "text", "text": f"Asset file: {name}"})
            content.append({"type": "image", "media_type": _media_type(path),
                            "data": _b64_file(path)})
    return content


def _request_header(state, new_instruction):
    doc_type = state.get("doc_type", "Assignment")
    lines = [
        "FORMAT REQUEST",
        f"Title: {state['title']}",
        f"Document type: {doc_type}",
        f"ID label for the header: {state['id_label']}  "
        f"(set ledger_lib.ID_LABEL to exactly this string)",
        f"Answer key requested: {'YES — produce the key form, title gains (KEY)' if state.get('make_key') else 'no'}",
    ]
    if state.get("instructions"):
        lines.append("")
        lines.append("The teacher's earlier instructions, all still in force:")
        for k, ins in enumerate(state["instructions"], 1):
            lines.append(f"  {k}. {ins}")
    lines.append("")
    lines.append(f"NEW INSTRUCTION FROM THE TEACHER: {new_instruction}")
    return "\n".join(lines)


def _base_message(state, new_instruction, current_script=None,
                  extra_text=None, extra_images=None):
    content = [{"type": "text", "text": _request_header(state,
                                                        new_instruction)}]
    content += _source_content(state["source"])
    if current_script:
        content.append({"type": "text", "text":
                        "THE CURRENT BUILD SCRIPT (the latest accepted "
                        "version — start from it):\n\n```python\n"
                        + current_script + "\n```"})
    if extra_text:
        content.append({"type": "text", "text": extra_text})
    for img in (extra_images or []):
        content.append({"type": "image", "media_type": "image/png",
                        "data": _b64_file(img)})
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------------------
# The conversation/session object the window drives
# ---------------------------------------------------------------------------
def _safe_title(title):
    t = re.sub(r'[^A-Za-z0-9 _()\-]', '', title).strip()
    return t or "Untitled Assignment"


def list_recent():
    """[(folder_name, path)] of saved formatter conversations, newest first."""
    if not os.path.isdir(FORMATTED_DIR):
        return []
    out = []
    for name in os.listdir(FORMATTED_DIR):
        p = os.path.join(FORMATTED_DIR, name)
        if os.path.isfile(os.path.join(p, "conversation.json")):
            out.append((name, p))
    out.sort(key=lambda np: os.path.getmtime(os.path.join(np[1],
                                                          "conversation.json")),
              reverse=True)
    return out


class FormatterSession:
    """One assignment's formatter conversation, persisted under
    `~/Ledger Workspace/06 Formatted Assignments/<Title>/`."""

    def __init__(self, folder):
        self.folder = folder
        self.state = {"title": "", "id_label": "", "doc_type": "Assignment",
                      "make_key": False, "model": "claude-fable-5",
                      "instructions": [], "versions": [], "transcript": [],
                      "source": None, "usage_log": []}

    # -- creation / persistence ------------------------------------------
    @classmethod
    def create(cls, title, id_label, doc_type, make_key, model,
               file_path=None, pasted_text=None):
        os.makedirs(FORMATTED_DIR, exist_ok=True)
        folder = os.path.join(FORMATTED_DIR, _safe_title(title))
        os.makedirs(folder, exist_ok=True)
        sess = cls(folder)
        st = sess.state
        st.update(title=title, id_label=id_label, doc_type=doc_type,
                  make_key=bool(make_key), model=model)
        src_dir = os.path.join(folder, "source")
        os.makedirs(src_dir, exist_ok=True)
        if file_path:
            kept = os.path.join(src_dir, os.path.basename(file_path))
            if os.path.abspath(kept) != os.path.abspath(file_path):
                shutil.copy(file_path, kept)
            file_path = kept
        elif pasted_text:
            kept = os.path.join(src_dir, "pasted_questions.txt")
            with open(kept, "w", encoding="utf-8") as fh:
                fh.write(pasted_text)
        st["source"] = load_source(file_path=file_path,
                                   pasted_text=pasted_text,
                                   work_dir=src_dir)
        sess.save()
        return sess

    @classmethod
    def load(cls, folder):
        sess = cls(folder)
        with open(os.path.join(folder, "conversation.json"), "r",
                  encoding="utf-8") as fh:
            sess.state = json.load(fh)
        return sess

    def save(self):
        with open(os.path.join(self.folder, "conversation.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1)

    # -- bookkeeping ------------------------------------------------------
    def _next_version(self):
        return len(self.state["versions"]) + 1

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def _store_version(self, script_text, pdf_path, note, check):
        n = self._next_version()
        sp = os.path.join(self.folder, f"build_v{n}.py")
        with open(sp, "w", encoding="utf-8") as fh:
            fh.write(script_text)
        vp = os.path.join(self.folder, f"v{n}.pdf")
        shutil.copy(pdf_path, vp)
        previews = self._render_previews(vp, n)
        self.state["versions"].append({
            "n": n, "script_path": sp, "pdf_path": vp, "note": note,
            "check": check, "previews": previews,
            "at": datetime.datetime.now().isoformat(timespec="seconds")})
        self.save()
        return self.state["versions"][-1]

    def _render_previews(self, pdf_path, n):
        import fitz
        pv_dir = os.path.join(self.folder, ".previews")
        os.makedirs(pv_dir, exist_ok=True)
        out = []
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            p = os.path.join(pv_dir, f"v{n}_p{i + 1}.png")
            page.get_pixmap(dpi=RESULT_RENDER_DPI).save(p)
            out.append(p)
        doc.close()
        return out

    def _log_usage(self, usage):
        if usage:
            self.state["usage_log"].append(usage)

    # -- the loop (§2.6) --------------------------------------------------
    def send(self, instruction, log=None):
        """One teacher message -> up to MAX_MODEL_CALLS model calls ->
        a result dict:
            ok           bool — a version was produced
            version      the stored version dict (or None)
            note         the model's plain-English note
            check_text   the check result in plain words
            warnings     [str] what the box could not confirm
            error        str|None — nothing could be produced
        """
        def say(msg):
            if log:
                log(msg)

        st = self.state
        model = st["model"]
        calls = 0
        warnings = []
        note = ""
        script = None
        run_res = None
        check = None
        best = None  # (script, run_res, check|None, note)

        def ask(messages, what):
            nonlocal calls
            calls += 1
            say(f"model call {calls}/{MAX_MODEL_CALLS}: {what}")
            text, usage = call_model(model, messages, log=log)
            self._log_usage(usage)
            return parse_reply(text)

        def run(sc):
            say("running the build script...")
            assets = [p for _n, p in st["source"]["assets"]]
            return format_runner.run_build_script(sc, asset_paths=assets)

        current = None
        if st["versions"]:
            current = self._read(st["versions"][-1]["script_path"])

        try:
            # 1. ask for a script and run it
            note, script = ask(_base_message(st, instruction,
                                             current_script=current),
                               "formatting")
            if script is None:
                note2, script = ask(_base_message(
                    st, instruction, current_script=current,
                    extra_text="Your previous reply contained no Python code "
                               "block. Reply now with the short note and "
                               "exactly ONE fenced ```python block holding "
                               "the COMPLETE build script."),
                    "asking again for the script")
                note = note2 or note
            if script is None:
                raise RuntimeError("The model did not return a build script.")
            run_res = run(script)

            # 2. crash -> up to 2 corrected full scripts
            crash_fixes = 0
            while (not run_res["ok"] and crash_fixes < MAX_CRASH_FIXES
                   and calls < MAX_MODEL_CALLS):
                crash_fixes += 1
                say("script failed; asking for a correction "
                    f"({crash_fixes}/{MAX_CRASH_FIXES})...")
                n2, s2 = ask(_base_message(
                    st, instruction, current_script=script,
                    extra_text="THE SCRIPT ABOVE FAILED when this box ran "
                               "it. Reply with the note and the corrected "
                               "COMPLETE script.\n\nError:\n"
                               + (run_res["error"] or "")), "fixing a crash")
                if s2:
                    note, script = (n2 or note), s2
                    run_res = run(script)
            if not run_res["ok"]:
                raise RuntimeError(run_res["error"] or
                                   "The build script kept failing.")
            best = (script, run_res, None, note)

            # 3. the Ledger read test; one corrected try on failure
            check = format_check.check_pdf(run_res["pdf_path"],
                                           run_res["boxes"],
                                           run_res["pages"])
            if not check["passed"] and calls < MAX_MODEL_CALLS:
                say("Ledger read test failed; sending findings back...")
                findings = "\n".join("- " + p for p in check["problems"])
                n2, s2 = ask(_base_message(
                    st, instruction, current_script=script,
                    extra_text="THE LEDGER READ TEST FAILED on the PDF your "
                               "script produced. The machine reader found:\n"
                               + findings +
                               "\n\nReply with the note and the corrected "
                               "COMPLETE script."), "fixing the read test")
                if s2:
                    rr2 = run(s2)
                    if rr2["ok"]:
                        ck2 = format_check.check_pdf(rr2["pdf_path"],
                                                     rr2["boxes"],
                                                     rr2["pages"])
                        if ck2["passed"] or not check["passed"]:
                            note, script, run_res, check = (n2 or note), s2, rr2, ck2
            if not check["passed"]:
                warnings.append("The Ledger read test did not fully pass: "
                                + "; ".join(check["problems"]))
            best = (script, run_res, check, note)

            # 4. looks check — once, with the finished page images
            if calls < MAX_MODEL_CALLS:
                say("looks check: showing the model its finished pages...")
                pages_dir = format_runner.new_scratch()
                import fitz
                doc = fitz.open(run_res["pdf_path"])
                page_pngs = []
                for i, page in enumerate(doc):
                    p = os.path.join(pages_dir, f"look_p{i + 1}.png")
                    page.get_pixmap(dpi=RESULT_RENDER_DPI).save(p)
                    page_pngs.append(p)
                doc.close()
                n2, s2 = ask(_base_message(
                    st, instruction, current_script=script,
                    extra_text="LOOKS CHECK. These images are the rendered "
                               "pages your script just produced. Inspect "
                               "them for: text cut off, overlapping "
                               "elements, content jammed against a box "
                               "edge, a crowded footer zone, missing or "
                               "misworded questions, undersized answer "
                               "boxes. If everything is right, reply with "
                               "exactly OK. Otherwise reply with the note "
                               "and the corrected COMPLETE script.",
                    extra_images=page_pngs), "looks check")
                if s2 and calls <= MAX_MODEL_CALLS:
                    rr2 = run(s2)
                    if rr2["ok"]:
                        ck2 = format_check.check_pdf(rr2["pdf_path"],
                                                     rr2["boxes"],
                                                     rr2["pages"])
                        if ck2["passed"] or not check["passed"]:
                            note = n2 or note
                            script, run_res, check = s2, rr2, ck2
                            best = (script, run_res, check, note)
                        else:
                            warnings.append("The model suggested a visual "
                                            "fix, but the fixed version "
                                            "failed the Ledger read test; "
                                            "keeping the readable one.")
                    else:
                        warnings.append("The model suggested a visual fix, "
                                        "but it would not run; keeping the "
                                        "working version.")

        except RuntimeError as exc:
            if best is None:
                return {"ok": False, "version": None, "note": note,
                        "check_text": "", "warnings": warnings,
                        "error": str(exc)}
            warnings.append(str(exc))

        if calls >= MAX_MODEL_CALLS and (check is None or not check["passed"]):
            warnings.append(f"The box used all {MAX_MODEL_CALLS} model calls "
                            "for this request; this is the best version it "
                            "could confirm.")

        script, run_res, check, note = best
        if check is None:
            check = format_check.check_pdf(run_res["pdf_path"],
                                           run_res["boxes"], run_res["pages"])
        st["instructions"].append(instruction)
        version = self._store_version(script, run_res["pdf_path"], note, {
            "passed": check["passed"], "boxes_found": check["boxes_found"],
            "n_pages": check["n_pages"], "problems": check["problems"]})
        if not run_res["sandboxed"]:
            warnings.append("Note: bubblewrap was unavailable, so the build "
                            "script ran without a sandbox.")
        if check["passed"]:
            check_text = (f"The Ledger can read this: "
                          f"{check['boxes_found']} answer boxes, "
                          f"page numbers OK.")
        else:
            check_text = ("The Ledger could NOT fully read this version: "
                          + "; ".join(check["problems"]))
        self.save()
        return {"ok": True, "version": version, "note": note,
                "check_text": check_text, "warnings": warnings, "error": None}
