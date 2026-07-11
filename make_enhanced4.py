#!/usr/bin/env python3
"""Re-convert ONLY the circuit schematic (lv_circuit.png) with the
ACCURACY-FIRST (hallucination-0) FIGURE_RULES.

enhanced3 regressed: the "list EVERY designator / omission=error" pressure
pushed qwen3-vl to fabricate a sequential run (C23..C33 all "100nF") plus
non-existent parts (M32, D3) on the ultra-dense low-voltage circuit page.
extract_all_via_pdf.FIGURE_RULES has now been rebalanced so ACCURACY
(invent nothing, never fill numbers sequentially) dominates completeness.

Model: qwen3-vl:32b (local llama-server, localhost gateway only).
Offline enforced by the hard local guard in fmdw.ollama_extractor.
max_tokens=16384, timeout=3600s (pinned here regardless of any config object).
"""
import sys
import pathlib
import time

ROOT = pathlib.Path.home() / "workspace" / "filestomdwgem"
sys.path.insert(0, str(ROOT))

from extract_all_via_pdf import FIGURE_RULES  # noqa: E402
import fmdw.ollama_extractor as oe  # noqa: E402
from fmdw.ollama_extractor import (  # noqa: E402
    extract_image,
    OLLAMA_VISION_MODEL,
    OLLAMA_BASE_URL,
    MODEL_STRUCTURE,
)

# Pin budget/timeout regardless of _cfg or env resolution order.
oe.OLLAMA_MAX_TOKENS = 16384
oe.OLLAMA_TIMEOUT = 3600

ASSETS = ROOT / "_worklog" / "assets"
SRC = "lv_circuit.png"
OUT = "enhanced4_circuit.md"

print(f"[CFG] model={OLLAMA_VISION_MODEL} structure_model={MODEL_STRUCTURE} "
      f"base_url={OLLAMA_BASE_URL}", flush=True)
print(f"[CFG] prompt=FIGURE_RULES prompt_len={len(FIGURE_RULES)} chars "
      f"role=structure max_tokens={oe.OLLAMA_MAX_TOKENS} "
      f"timeout={oe.OLLAMA_TIMEOUT}s", flush=True)

p = ASSETS / SRC
if not p.exists():
    print(f"[ERROR] missing source {p}", flush=True)
    sys.exit(1)

t0 = time.time()
print(f"[START] {SRC} -> {OUT}", flush=True)
try:
    # max_tokens/timeout are enforced via the pinned module constants above
    # (extract_image -> _ollama_vision reads OLLAMA_MAX_TOKENS/OLLAMA_TIMEOUT).
    text = extract_image(FIGURE_RULES, p, provider=None, role="structure")
except Exception as e:  # noqa: BLE001
    print(f"[ERROR] {SRC}: {type(e).__name__}: {e}", flush=True)
    sys.exit(2)

text = (text or "").strip()
(ASSETS / OUT).write_text(text + "\n", encoding="utf-8")
dt = time.time() - t0
print(f"[DONE] {OUT} ({len(text)} chars, {dt:.1f}s)", flush=True)
print("[ALL DONE]", flush=True)
