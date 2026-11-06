"""format_runner.py — [PATCH:formatter] WO-F1: runs a model-written build
script in a fresh scratch folder and reports what happened.

The AI model never sends a PDF; it sends a short Python build script that
imports the author-approved layout library. This module:

  * creates a fresh scratch folder under the user's
    `~/Ledger Workspace/.Temp-Ledger-Files/`,
  * copies the five library scripts and any source-asset images into it,
  * runs the script with the venv Python, 120-second limit,
  * under bubblewrap (bwrap) when available: no network, write access only
    to the scratch folder; otherwise plainly (reported via `sandboxed`),
  * parses the script's mandatory final line
    `LEDGER_BOXES=<n> LEDGER_PAGES=<n>`.

No GUI imports. Driven by formatter_core.py and by test scripts.
"""
import os
import re
import shutil
import subprocess
import time

VENV_PYTHON = "/opt/ledger/venv/bin/python"
LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
LIB_FILES = ["ledger_lib.py", "widgets.py", "exam_widgets.py", "compact.py",
             "key_lib.py"]
TEMP_BASE = os.path.join(os.path.expanduser("~"), "Ledger Workspace",
                         ".Temp-Ledger-Files")
TIMEOUT_S = 120

_FINAL_LINE = re.compile(r"LEDGER_BOXES=(\d+)\s+LEDGER_PAGES=(\d+)")


def bwrap_available():
    return shutil.which("bwrap") is not None


def _bwrap_cmd(scratch, env_pairs):
    """bwrap: read-only system + venv, read-write scratch, no network.
    /etc is NOT bound wholesale (it holds /etc/paragon); only the pieces
    Python/fontconfig need are."""
    cmd = ["bwrap", "--die-with-parent", "--unshare-all",
           "--ro-bind", "/usr", "/usr",
           "--symlink", "usr/lib", "/lib",
           "--symlink", "usr/bin", "/bin",
           "--symlink", "usr/sbin", "/sbin",
           "--ro-bind", "/opt/ledger/venv", "/opt/ledger/venv",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
           "--bind", scratch, scratch, "--chdir", scratch]
    if os.path.exists("/lib64"):
        cmd += ["--symlink", "usr/lib64", "/lib64"]
    for p in ("/etc/fonts", "/etc/ld.so.cache", "/etc/alternatives"):
        if os.path.exists(p):
            cmd += ["--ro-bind", p, p]
    for k, v in env_pairs.items():
        cmd += ["--setenv", k, v]
    return cmd


def new_scratch():
    os.makedirs(TEMP_BASE, exist_ok=True)
    scratch = os.path.join(TEMP_BASE, f"formatter-{int(time.time() * 1000)}")
    os.makedirs(scratch)
    return scratch


def run_build_script(script_text, asset_paths=None, scratch=None):
    """Run one build script. Returns a dict:
        ok         bool — exited 0 within the limit and produced a PDF
        pdf_path   str|None
        boxes      int|None (from the final line)
        pages      int|None
        stdout / stderr   captured text
        error      str|None — plain-language failure summary
        sandboxed  bool — ran under bwrap
        scratch    the scratch folder used
    """
    if scratch is None:
        scratch = new_scratch()
    for lf in LIB_FILES:
        shutil.copy(os.path.join(LIB_DIR, lf), scratch)
    for ap in (asset_paths or []):
        shutil.copy(ap, scratch)
    script_path = os.path.join(scratch, "build.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(script_text)
    out_pdf = os.path.join(scratch, "out.pdf")
    env_pairs = {
        "LEDGER_OUT_PDF": out_pdf,
        "HOME": scratch,
        "MPLCONFIGDIR": os.path.join(scratch, ".mpl"),
        "PATH": "/usr/bin:/bin",
    }

    sandboxed = bwrap_available()
    if sandboxed:
        cmd = _bwrap_cmd(scratch, env_pairs) + [VENV_PYTHON, script_path]
        run_env = None
    else:
        cmd = [VENV_PYTHON, script_path]
        run_env = dict(os.environ)
        run_env.update(env_pairs)

    try:
        proc = subprocess.run(cmd, cwd=scratch, env=run_env,
                              capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "pdf_path": None, "boxes": None, "pages": None,
                "stdout": (exc.stdout or ""), "stderr": (exc.stderr or ""),
                "error": f"The build script ran longer than {TIMEOUT_S} "
                         "seconds and was stopped.",
                "sandboxed": sandboxed, "scratch": scratch}

    result = {"ok": False, "pdf_path": None, "boxes": None, "pages": None,
              "stdout": proc.stdout, "stderr": proc.stderr, "error": None,
              "sandboxed": sandboxed, "scratch": scratch}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-3000:]
        result["error"] = f"The build script crashed:\n{tail}"
        return result
    if not (os.path.exists(out_pdf) and os.path.getsize(out_pdf) > 500):
        result["error"] = ("The build script exited cleanly but wrote no PDF "
                           "to LEDGER_OUT_PDF.")
        return result
    m = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        m = _FINAL_LINE.search(line)
        if m:
            break
    if not m:
        result["error"] = ("The build script did not print the required "
                           "final line `LEDGER_BOXES=<n> LEDGER_PAGES=<n>`.")
        return result
    result.update(ok=True, pdf_path=out_pdf,
                  boxes=int(m.group(1)), pages=int(m.group(2)))
    return result
