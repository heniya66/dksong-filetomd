#!/usr/bin/env python3
"""Re-convert 5 representative images with TODAY's enhanced prompt.

Uses fmdw.figure_extractor._DESCRIBE_PROMPT (2026-07-01 enhancement:
component completeness / roster + easy non-expert explanation + anti-fabrication)
via fmdw.ollama_extractor.extract_image (qwen3-vl:32b, localhost gateway only).
Offline enforced by hard local guard in ollama_extractor.
"""
import sys
import pathlib
import time

ROOT = pathlib.Path.home() / "workspace" / "filestomdwgem"
sys.path.insert(0, str(ROOT))

from fmdw.figure_extractor import _DESCRIBE_PROMPT  # noqa: E402
from fmdw.ollama_extractor import (  # noqa: E402
    extract_image,
    OLLAMA_VISION_MODEL,
    OLLAMA_BASE_URL,
    MODEL_CAPTION,
)

ASSETS = ROOT / "_worklog" / "assets"
MAPPING = [
    ("lv_circuit.png", "enhanced2_circuit.md"),
    ("lv_timing.png", "enhanced2_timing.md"),
    ("lv_pin.png", "enhanced2_pin.md"),
    ("lv_elec.png", "enhanced2_elec.md"),
    ("lv_text.png", "enhanced2_text.md"),
]

print(f"[CFG] model={OLLAMA_VISION_MODEL} caption_model={MODEL_CAPTION} "
      f"base_url={OLLAMA_BASE_URL}", flush=True)
print(f"[CFG] prompt_len={len(_DESCRIBE_PROMPT)} chars", flush=True)

for src, out in MAPPING:
    p = ASSETS / src
    if not p.exists():
        print(f"[ERROR] missing source {p}", flush=True)
        continue
    t0 = time.time()
    print(f"[START] {src} -> {out}", flush=True)
    try:
        text = extract_image(_DESCRIBE_PROMPT, p, provider=None, role="caption")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {src}: {type(e).__name__}: {e}", flush=True)
        continue
    text = (text or "").strip()
    (ASSETS / out).write_text(text + "\n", encoding="utf-8")
    dt = time.time() - t0
    print(f"[DONE] {out} ({len(text)} chars, {dt:.1f}s)", flush=True)

print("[ALL DONE]", flush=True)
