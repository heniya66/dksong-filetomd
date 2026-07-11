"""F12 deterministic runaway/truncation cleanup unit tests — run from ~/workspace/filetomd."""
import os
import sys

sys.path.insert(0, os.getcwd())
import extract_all_via_pdf as eap

FAILS = []


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL") + f" :: {name}" + (f"  | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


# ── runaway detection ────────────────────────────────────────────────────────
runaway = "PSPI opening | " + " | ".join(["DV/LV"] * 1531)
check("D1 runaway (DV/LV ×1531) detected", eap._is_runaway_line(runaway) is True)
degrow = "| Foo | " + " | ".join(["X"] * 30) + " |"
check("D2 degenerate |-row (X ×30) detected", eap._is_runaway_line(degrow) is True)
check("D2b 15× identical cells PRESERVED (< 20, Advisor #1)",
      eap._is_runaway_line("| Foo | " + " | ".join(["X"] * 15) + " |") is False)
# legit F11 grid PSPI row (empties break the run → max DV/LV consecutive ≤4)
legit_pspi = ("| PSPI opening |  | DV/LV |  |  | DV/LV | DV/LV | DV/LV | DV/LV | "
              "DV/ LV | DV/ LV | DV/ LV |")
check("D3 legit grid PSPI row NOT flagged", eap._is_runaway_line(legit_pspi) is False, repr(legit_pspi[:50]))
# legit 12-col row with 7 consecutive identical values
legit_row = "| Total 1x levels |  |  |  | - | 3 | 3 | 3 | 3 | 3 | 3 | 3 |"
check("D4 legit 12-col row (seven 3s) NOT flagged", eap._is_runaway_line(legit_row) is False)
check("D5 legit prose (word repeated 2-3×) NOT flagged",
      eap._is_runaway_line("the result is good good good indeed") is False)
check("D6 normal prose NOT flagged",
      eap._is_runaway_line("Physical designs consist of a defined combination of mask levels.") is False)
# ★ min_rep=20 (Advisor #1): truth-table/register rows with 10-19 identical cells preserved
check("D7 truth-table row 12× identical NOT flagged (< 20)",
      eap._is_runaway_line("| in | " + " | ".join(["1"] * 12) + " |") is False)
check("D7b 19× NOT flagged (below threshold 20)",
      eap._is_runaway_line("x | " + " | ".join(["AB"] * 19)) is False)
check("D8 20× flagged (at threshold)",
      eap._is_runaway_line("x | " + " | ".join(["AB"] * 20)) is True)
check("D8b real runaway 1531× flagged", eap._is_runaway_line(runaway) is True)
# ★ GFM separator of a WIDE (≥20-col) table must NOT be flagged even above threshold
sep24 = "| " + " | ".join([":---"] * 24) + " |"
check("D9 wide GFM separator (:--- ×24, ≥threshold) NOT flagged", eap._is_runaway_line(sep24) is False, repr(sep24[:40]))
check("D9b guard genuinely needed: raw repeat detected but separator excluded",
      eap._consec_short_repeat(sep24.split("|")) is True and eap._is_gfm_separator(sep24) is True)
check("D9c separator variants (---, :---:) detected",
      eap._is_gfm_separator("| --- | :---: | :--- | --- | --- | --- | --- | --- | --- | --- | --- | --- |") is True)
check("D9d a data row of dashes-like codes NOT a separator",
      eap._is_gfm_separator("| MD | MD | MD | MD | MD | MD | MD | MD | MD | MD | MD | MD |") is False)


# ── TRUNCATED marker + line removal ─────────────────────────────────────────
md = ("## 3.6.2 BEOL Metallization Options\n\n"
      "| Total copper metal levels |  |  |  | - | 11 | 12 | 12 | 13 | 9 | 10 | 10 |\n\n"
      + runaway + "\n"
      "<!-- TRUNCATED: finish_reason=length -->\n\n"
      "Some trailing prose after.")
out = eap._clean_runaway(md)
check("F1 runaway line removed", runaway not in out and "DV/LV | DV/LV | DV/LV | DV/LV | DV/LV | DV/LV | DV/LV | DV/LV | DV/LV | DV/LV" not in out)
check("F2 TRUNCATED marker removed", "TRUNCATED" not in out and "finish_reason" not in out)
check("F3 heading preserved", "## 3.6.2 BEOL Metallization Options" in out)
check("F4 legit table row preserved", "| Total copper metal levels |  |  |  | - | 11 | 12 | 12 | 13 | 9 | 10 | 10 |" in out)
check("F5 trailing prose preserved", "Some trailing prose after." in out)

# TRUNCATED marker variants
check("F6 bare TRUNCATED marker removed", eap._clean_runaway("<!-- TRUNCATED -->") == "")
check("F7 TRUNCATED-with-detail removed", eap._clean_runaway("<!-- TRUNCATED pages 1-3: length -->") == "")

# normal doc untouched (byte-identical)
normal = "line one\nline two\n\n| a | b |\n| :--- | :--- |\n| c | d |\n\nlast line"
check("F8 normal doc unchanged (byte-identical)", eap._clean_runaway(normal) == normal)

# idempotent
o1 = eap._clean_runaway(md)
check("F9 idempotent", eap._clean_runaway(o1) == o1)

# gate off
os.environ["FMDW_DERUNAWAY"] = "0"
check("F10 gate off → unchanged", eap._clean_runaway(md) == md)
os.environ.pop("FMDW_DERUNAWAY", None)

# ── FIX A2: prompt section-label leak line removal (2026-07-10) ──────────────
_a2_in = (
    "<!-- page 4 -->\n\n"
    "FIGURES:\n"
    "**Figure 2: A width with run length**\n\n"
    "TABLE OF CONTENTS pages:\n\n"
    "Note: The terms are defined by common use.\n\n"
    "Table 1: Terminology\n\n"
    "| Syntax | Description |\n| :--- | :--- |\n| A op B | assigns level |\n\n"
    "FIGURES: see appendix for the full set of figures.\n\n"
    "```\nFIGURES:\n```"
)
_a2_out = eap._strip_prompt_leak(_a2_in)
check("A2-1 bare FIGURES: label line removed", "FIGURES:\n**Figure 2:" not in _a2_out)
check("A2-1b caption directly follows page marker after removal",
      "<!-- page 4 -->\n\n**Figure 2: A width with run length**" in _a2_out)
check("A2-2 TABLE OF CONTENTS pages: label line removed",
      "TABLE OF CONTENTS pages:" not in _a2_out)
check("A2-3 figure caption preserved", "**Figure 2: A width with run length**" in _a2_out)
check("A2-4 'Note:' line with content preserved",
      "Note: The terms are defined by common use." in _a2_out)
check("A2-5 caption 'Table 1: Terminology' preserved", "Table 1: Terminology" in _a2_out)
check("A2-6 table row preserved", "| A op B | assigns level |" in _a2_out)
check("A2-7 'FIGURES: <content>' (colon has content) preserved",
      "FIGURES: see appendix for the full set of figures." in _a2_out)
check("A2-8 FIGURES: inside code fence preserved", "```\nFIGURES:\n```" in _a2_out)
check("A2-9 idempotent", eap._strip_prompt_leak(_a2_out) == _a2_out)
check("A2-10 doc with no labels unchanged (byte-identical)",
      eap._strip_prompt_leak("hello\n\nworld\n\n| a | b |") == "hello\n\nworld\n\n| a | b |")

print("\n=== SUMMARY ===")
print(f"FAILURES: {FAILS if FAILS else 'NONE — ALL PASS'}")
sys.exit(1 if FAILS else 0)
