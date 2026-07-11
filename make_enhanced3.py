#!/usr/bin/env python3
"""Re-convert 5 representative images with the CORRECT completeness prompt.

Fix for enhanced2 failure: enhanced2 used fmdw.figure_extractor._DESCRIBE_PROMPT
(short "easy description" prompt, 688 chars) which does NOT enforce an exhaustive
component roster -> circuit designator completeness regressed.

This script uses extract_all_via_pdf.FIGURE_RULES, which contains:
  - FIGURE TYPE GATE + SCHEMATIC TRANSCRIPTION STRATEGY (REGION INVENTORY)
  - MANDATORY COMPONENT ROSTER (completeness gate: list EVERY readable designator)
  - PLAIN-LANGUAGE EXPLANATION (non-expert easy walk-through, additive)
  - ANTI-FABRICATION (highest priority: readable only, mark [판독 불가], never invent)

Model: qwen3-vl:32b (local llama-server, localhost gateway only).
Offline enforced by hard local guard in fmdw.ollama_extractor.
"""
import sys
import pathlib
import time

ROOT = pathlib.Path.home() / "workspace" / "filestomdwgem"
sys.path.insert(0, str(ROOT))

# FIGURE_RULES lives at module level in extract_all_via_pdf.py; the module is
# guarded by `if __name__ == "__main__"` so importing has no side effects beyond
# constant/function definitions.
from extract_all_via_pdf import FIGURE_RULES  # noqa: E402
from fmdw.ollama_extractor import (  # noqa: E402
    extract_image,
    OLLAMA_VISION_MODEL,
    OLLAMA_BASE_URL,
    MODEL_STRUCTURE,
)

ASSETS = ROOT / "_worklog" / "assets"
MAPPING = [
    ("lv_circuit.png", "enhanced3_circuit.md"),
    ("lv_timing.png", "enhanced3_timing.md"),
    ("lv_pin.png", "enhanced3_pin.md"),
    ("lv_elec.png", "enhanced3_elec.md"),
    ("lv_text.png", "enhanced3_text.md"),
]

print(f"[CFG] model={OLLAMA_VISION_MODEL} structure_model={MODEL_STRUCTURE} "
      f"base_url={OLLAMA_BASE_URL}", flush=True)
print(f"[CFG] prompt=FIGURE_RULES prompt_len={len(FIGURE_RULES)} chars "
      f"role=structure", flush=True)

for src, out in MAPPING:
    p = ASSETS / src
    if not p.exists():
        print(f"[ERROR] missing source {p}", flush=True)
        continue
    t0 = time.time()
    print(f"[START] {src} -> {out}", flush=True)
    try:
        text = extract_image(FIGURE_RULES, p, provider=None, role="structure")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {src}: {type(e).__name__}: {e}", flush=True)
        continue
    text = (text or "").strip()
    (ASSETS / out).write_text(text + "\n", encoding="utf-8")
    dt = time.time() - t0
    print(f"[DONE] {out} ({len(text)} chars, {dt:.1f}s)", flush=True)

print("[ALL DONE]", flush=True)
