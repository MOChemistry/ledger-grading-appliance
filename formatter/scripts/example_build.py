"""Minimal example: python3 example_build.py /tmp/out.pdf"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_lib import Doc, CW
from widgets import LONG_W, LONG_H, LINED_H, QGAP, TGAP

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ledger_example.pdf"
d = Doc(out, "SAMPLE: Ledger Format")
p = d.page()
p.q_text(1, "What is the ground state electron configuration of sodium (long form)?")
p.gap(TGAP); p.box(LONG_W, LONG_H, "1"); p.gap(QGAP)
p.q_text(2, "Explain why atomic radius decreases across a period.  Justify your answer.")
p.gap(TGAP); p.box(CW, LINED_H, "2", lined=True)
p.done()
d.save()
print("built", out)
