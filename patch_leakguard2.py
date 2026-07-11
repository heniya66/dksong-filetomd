#!/usr/bin/env python3
"""Widen 3 over-general Guard-B signatures to verbatim longer slices (2026-07-10).

Advisor NEEDS_FIX: three 3-word slices risked silently dropping normal prose.
Extend each with adjacent unique wording from the real transcription prompt
(lines ~2700/2714/2728) — detection unchanged (real leaks always carry the long
context), false positives eliminated.
"""
import sys
import py_compile

PATH = "extract_all_via_pdf.py"
with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()
orig = src

repls = [
    ('    "reproduce every entry",\n',
     '    "reproduce every entry (including",\n'),
    ('    "never output the same content twice",\n',
     '    "never output the same content twice. transcribe",\n'),
    ('    "no blank shortcuts",\n',
     '    "no blank shortcuts: every",\n'),
]

for old, new in repls:
    if new in src:
        print(f"ALREADY: {new.strip()}")
        continue
    if src.count(old) != 1:
        print(f"FAIL: anchor count={src.count(old)} for {old.strip()}")
        sys.exit(3)
    src = src.replace(old, new, 1)

with open(PATH + ".bak_20260710_leakguard2", "w", encoding="utf-8") as f:
    f.write(orig)
with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

try:
    py_compile.compile(PATH, doraise=True)
except py_compile.PyCompileError as e:
    print("COMPILE FAILED:", e)
    sys.exit(7)

print("PATCH2 OK")
for _, new in repls:
    print("has:", new.strip(), "->", new.strip() in src)
