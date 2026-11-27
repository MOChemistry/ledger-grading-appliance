"""Minimal example: python3 example_build.py /tmp/out.pdf

Shows BOTH worksheet idioms: the compact captioned two-column box grid
(the REQUIRED default for Assignment/Quiz sub-parts — see the format
spec) and a full-width lined box (free-response/essay items only)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_lib import Doc, CW
from widgets import LINED_H, TGAP
from compact import q_block, finish_page, QGAP_C

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ledger_example.pdf"
d = Doc(out, "SAMPLE: Ledger Format")
lows = []
p = d.page()
q_block(p, 1, "A 24.5 g sample of sodium chloride reacts with 41.3 g of "
              "fluorine gas according to the balanced equation on the "
              "board.",
        [("a", "limiting reactant"), ("b", "grams of chlorine gas")],
        prompts=["a.  What is the limiting reactant?  Justify your answer.",
                 "b.  How many grams of chlorine gas are produced?"])
p.gap(QGAP_C)
p.q_text(2, "Explain why atomic radius decreases across a period.  "
            "Justify your answer.")
p.gap(TGAP); p.box(CW, LINED_H, "2", lined=True)
finish_page(p, lows)
d.save()
print("built", out, "lows:", lows)
