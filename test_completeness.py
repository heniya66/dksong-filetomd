"""Hybrid completeness guard (general token-overlap) unit tests — run from ~/workspace/filetomd."""
import os
import sys

sys.path.insert(0, os.getcwd())
import extract_all_via_pdf as eap

FAILS = []


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL") + f" :: {name}" + (f"  | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def L(text, x=43, y=100):
    return [x, y, x + 200, y + 12, text]


COPY = [
    L("Copyright ⓒ 2023-2025 Samsung Electronics Co., Ltd.", 43, 601),
    L("Samsung Electronics Co., Ltd.", 43, 619),
    L("1, Samsungjeonja-ro, Hwaseong-si,", 43, 637),
    L("Gyeonggi-Do, Korea 445-330", 43, 655),
    L("Contact Us: tom82.kim@samsung.com", 43, 673),
    L("TEL: +82-31-325-5191", 43, 691),
    L("Home Page: http://www.samsung.com/semiconductor", 43, 709),
]


# ── (a) 7-line block fully missing → all recovered, in order, no dup ──────────
body_missing = "<!-- page 2 -->\n\nSome unrelated body prose about widgets and gadgets here."
rec, _sp, _cv, total = eap._recover_absent_blocks(COPY, body_missing)
flat = [l for run in rec for l in run]
check("(a) 7 lines recovered", total == 7 and len(flat) == 7, repr(total))
check("(a) reading order preserved", flat[0].startswith("Copyright") and flat[-1].startswith("Home Page"))
check("(a) no duplicates", len(set(flat)) == 7)


# ── (b) block already present (reworded, same tokens) → NOT re-added ──────────
body_has = ("<!-- page 2 -->\n\nThis document copyright 2023 2025 is owned by Samsung "
            "Electronics Co Ltd located at 1 Samsungjeonja ro Hwaseong si Gyeonggi Do Korea "
            "445 330 and you may contact us at tom82 kim samsung com or by tel 82 31 325 5191 "
            "and the home page is at http www samsung com semiconductor for reference.")
rec_b, _sp_b, _cv_b, total_b = eap._recover_absent_blocks(COPY, body_has)
check("(b) reworded-present block NOT recovered (dedup)", total_b == 0, repr(rec_b))


# ── (c) NEW: plain-prose block (no hard signal) absent → recovered ────────────
PROSE = [
    L("The threshold voltage of the device is set by gate oxide thickness and channel doping.", 40, 300),
    L("Under nominal operating conditions leakage current stays within the specified bounds.", 40, 314),
    L("Designers must account for temperature variation across the full operating range.", 40, 328),
]
body_c = "<!-- page 5 -->\n\nCompletely different material regarding register maps and memory clocks."
rec_c, _sp_c, _cv_c, total_c = eap._recover_absent_blocks(PROSE, body_c)
flat_c = [l for run in rec_c for l in run]
check("(c) plain-prose block recovered (general case)", total_c == 3 and len(flat_c) == 3, repr(rec_c))
check("(c) prose order preserved", "threshold voltage" in flat_c[0] and "temperature variation" in flat_c[2])


# ── (d) glm reworded a paragraph (same tokens, different order) → NOT recovered ─
PARA = [L("The gate oxide thickness determines the threshold voltage of the transistor device.", 40, 300)]
body_d = ("<!-- page 5 -->\n\nThe threshold voltage of the transistor device is determined by the "
          "gate oxide thickness value.")
rec_d, _sp_d, _cv_d, total_d = eap._recover_absent_blocks(PARA, body_d)
check("(d) reworded-same-tokens NOT recovered (>=60% overlap)", total_d == 0, repr(rec_d))


# ── (e) no missing block → nothing recovered ─────────────────────────────────
CLEAN_E = [L("All the content here is present", 40, 100),
           L("within the body text already", 40, 114)]
body_e = "<!-- page 1 -->\n\nAll the content here is present within the body text already and more."
rec_e, _sp_e, _cv_e, total_e = eap._recover_absent_blocks(CLEAN_E, body_e)
check("(e) no missing → nothing recovered", total_e == 0, repr(rec_e))


# ── over-recovery guard: run with no >=3-sig-token line dropped ───────────────
STRAY = [L("go now", 40, 100)]  # 2 sig tokens, no line >=3 → dropped
rec_s, _sp_s, _cv_s, total_s = eap._recover_absent_blocks(STRAY, "unrelated content entirely")
check("(guard) stray short run dropped (<3 sig tokens)", total_s == 0, repr(rec_s))
# a <2 sig-token line treated as covered (skip)
NUMS = [L("42", 40, 100), L("7", 40, 114)]
check("(guard) <2 sig-token lines not recovered", eap._recover_absent_blocks(NUMS, "x")[3] == 0)


# ── idempotence: recover, append, re-run → nothing more ──────────────────────
rec1, _s1, _c1, _t1 = eap._recover_absent_blocks(COPY, body_missing)
new_body = body_missing + "\n\n" + "\n\n".join("\n".join(r) for r in rec1)
rec2, _s2, _c2, total2 = eap._recover_absent_blocks(COPY, new_body)
check("(idem) second pass recovers nothing", total2 == 0, repr(rec2))


# ── gate off ─────────────────────────────────────────────────────────────────
os.environ["FMDW_HYBRID_COMPLETENESS"] = "0"
check("(gate) off → _apply returns body unchanged",
      eap._apply_hybrid_completeness("nonexistent.pdf", 2, "body text") == "body text")
os.environ.pop("FMDW_HYBRID_COMPLETENESS", None)


# ── REAL PDF integration: page 2 copyright block recovered, no dup ────────────
fake_body = ("<!-- page 2 -->\n\n## Important Notice\n\n"
             "Samsung Electronics Co. Ltd. reserves the right to make changes to the "
             "information in this publication at any time without prior notice.")
out = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p1-4.pdf", 2, fake_body)
for probe in ["Copyright", "Samsungjeonja-ro", "tom82.kim@samsung.com", "TEL:", "Home Page", "445-330"]:
    check(f"(real) recovered '{probe}'", probe in out, repr(probe))
check("(real) email exactly once (no dup)", out.count("tom82.kim@samsung.com") == 1)
check("(real) no CJK falsely recovered (skipped, <2 latin tokens)", "警告" not in out)

# ══════════ PLACEMENT (TASK A): monotonic reading-position insert vs trailing append ══════════
# top-of-page uncovered block, then a covered heading → block recovered + span before the covered line
clean_top = [
    L("One 1x thin wiring level in low-k dielectric", 40, 97),
    L("Up to three 1x thin wiring levels in ultralow-k dielectric", 40, 120),
    L("3.6.1 Metal Stack Naming Convention", 40, 188),   # covered (in body below)
]
body_top = ("<!-- page 2 -->\n\n### 3.6.1 Metal Stack Naming Convention\n\n"
            "The metal stack naming options use the following conventions.")
_r, _spans, _cov, _t = eap._recover_absent_blocks(clean_top, body_top)
check("PL1 top block recovered", _t >= 1 and any("wiring level" in l for run in _r for l in run))
check("PL2 covered flags: bullets uncovered, heading covered",
      _cov == [False, False, True], repr(_cov))

# trailing block: uncovered at page end, no following covered line
clean_tail = [
    L("Important Notice heading here", 40, 100),         # covered
    L("Copyright 2023 Samsung Electronics Co Ltd", 43, 601),   # uncovered (trailing)
    L("Contact Us tom82 kim samsung com", 43, 673),            # uncovered
]
body_tail = "<!-- page 2 -->\n\nImportant Notice heading here and some legal text about it."
_r2, _spans2, _cov2, _t2 = eap._recover_absent_blocks(clean_tail, body_tail)
check("PL3 trailing block recovered", _t2 >= 1 and any("Copyright" in l for run in _r2 for l in run))
check("PL4 no covered line after trailing block (→ append)",
      _cov2[0] is True and not any(_cov2[1:]), repr(_cov2))

# ── real-PDF integration ──
# p85-87 page 2: BEOL wiring bullets absent → inserted BEFORE 3.6.1 (not appended at end)
fake_p2 = ("<!-- page 2 -->\n\n### 3.6.1 Metal Stack Naming Convention\n\n"
           "The metal stack naming options use the following conventions.")
out_p2 = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p85-87.pdf", 2, fake_p2)
check("PL5 p85-87: recovered wiring bullets placed BEFORE 3.6.1",
      "wiring" in out_p2.lower() and "3.6.1" in out_p2
      and out_p2.lower().index("wiring") < out_p2.index("3.6.1"),
      f"wiring@{out_p2.lower().find('wiring')} 3.6.1@{out_p2.find('3.6.1')}")

# p1-4 page 2 REGRESSION: copyright/contact block stays at BOTTOM, CONTIGUOUS (not split)
fake_p14 = ("<!-- page 2 -->\n\n## Important Notice\n\n"
            "Samsung Electronics Co. Ltd. reserves the right to make changes to the "
            "information in this publication at any time without prior notice.")
out_p14 = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p1-4.pdf", 2, fake_p14)
check("PL6 p1-4: copyright recovered", "tom82.kim@samsung.com" in out_p14)
check("PL7 p1-4: whole copyright block AFTER the notice (bottom)",
      out_p14.index("Copyright") > out_p14.index("reserves the right")
      and out_p14.index("tom82.kim@samsung.com") > out_p14.index("reserves the right"))
# ★ NOT split: 'Copyright ⓒ' line and 'Samsungjeonja-ro' address are contiguous (≤ a few lines apart)
_ol = out_p14.split("\n")
_ci = next((i for i, l in enumerate(_ol) if "Copyright" in l), -1)
_si = next((i for i, l in enumerate(_ol) if "Samsungjeonja-ro" in l), -1)
check("PL8 copyright block CONTIGUOUS (not split top/bottom)",
      _ci >= 0 and _si >= 0 and (_si - _ci) <= 3, f"Copyright@{_ci} Samsungjeonja@{_si}")
_tail = out_p14.rstrip().split("\n")[-1]
check("PL8b copyright block is the trailing content",
      "samsung.com/semiconductor" in _tail or "TEL:" in _tail or "Home Page" in _tail, repr(_tail))

# idempotent
_o = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p85-87.pdf", 2, fake_p2)
check("PL9 placement idempotent", eap._apply_hybrid_completeness("input/pdf/LN08LPU_p85-87.pdf", 2, _o) == _o)

# ══════════ BULLET-GLYPH MERGE (FOLLOW-UP): lone • span + text → "• text" → F9 "- text" ══════════
check("BM1 • is top marker", eap._lone_bullet_marker("•") == "•")
check("BM1b ● ▪ ‣ · are top markers", all(eap._lone_bullet_marker(g) == "•" for g in ["●", "▪", "‣", "·"]))
check("BM2 – (en-dash) is sub marker", eap._lone_bullet_marker("–") == "–")
check("BM2b - (hyphen) is sub marker", eap._lone_bullet_marker("-") == "–")
check("BM3 real text is NOT a lone bullet", eap._lone_bullet_marker("One 1x wiring") is None)
check("BM3b '• text' (glyph+text in one span) is NOT lone", eap._lone_bullet_marker("• text") is None)

# real geometry (probed): lone • x=42.5 + text x=56.7 same y=97 → "• One 1x ..."
blines = [
    [42.5, 97.0, 46.0, 110.8, "•"],
    [56.7, 97.0, 245.6, 110.8, "One 1x (thin) wiring level in low-k dielectric"],
    [42.5, 110.5, 46.0, 124.3, "•"],
    [56.7, 110.5, 300.6, 124.3, "Up to three 1x (thin) wiring levels in ultralow-k dielectric"],
]
merged = eap._merge_bullet_glyph_lines(blines)
check("BM4 two lone bullets merged → two lines", len(merged) == 2, f"len={len(merged)}")
check("BM5 merged text = '• One 1x ...'",
      merged[0][4] == "• One 1x (thin) wiring level in low-k dielectric", repr(merged[0][4]))
check("BM5b merged keeps bullet indent x0=42.5", merged[0][0] == 42.5)

# sub-bullet – merge → "– text"
msub = eap._merge_bullet_glyph_lines(
    [[60.0, 200.0, 63.0, 212.0, "–"], [74.0, 200.0, 250.0, 212.0, "deeper item text"]])
check("BM6 sub – merged → '– deeper item text'",
      len(msub) == 1 and msub[0][4] == "– deeper item text", repr(msub))

# normal two-word line (no bullet glyph) → UNCHANGED
normal = [[56.7, 97.0, 245.6, 110.8, "Normal text line"], [56.7, 111.0, 200.0, 123.0, "another line"]]
check("BM7 non-bullet lines unchanged", eap._merge_bullet_glyph_lines(normal) == normal)

# cross-column guard: lone • but next text far right (gap > 40) → NOT merged
check("BM8 far-right (cross-column) NOT merged",
      len(eap._merge_bullet_glyph_lines(
          [[42.5, 97.0, 46.0, 110.8, "•"], [300.0, 97.0, 450.0, 110.8, "right column text"]])) == 2)

# different-y guard: lone • then text on a different line → NOT merged
check("BM9 different-y NOT merged",
      len(eap._merge_bullet_glyph_lines(
          [[42.5, 97.0, 46.0, 110.8, "•"], [56.7, 130.0, 245.6, 142.0, "text far below"]])) == 2)

# end-to-end via F9: merged "• text" → "- text"; "– text" → "  - text"
_f9top = eap._normalize_bullets("intro\n\n• One 1x (thin) wiring level\n• Up to three levels")
check("BM10 F9 turns '• ' → '- '",
      "- One 1x (thin) wiring level" in _f9top and "- Up to three levels" in _f9top)
check("BM11 F9 turns '– ' → '  - '", "  - child item" in eap._normalize_bullets("- parent\n– child item"))

# real-PDF integration: p85-87 p2 recovered bullets now carry the • glyph → F9 makes '- '
_obm = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p85-87.pdf", 2, fake_p2)
check("BM12 recovered p85-87 bullets carry '• ' glyph", "• One 1x (thin) wiring level" in _obm)
check("BM13 after F9 → '- One 1x ...' markdown bullet",
      "- One 1x (thin) wiring level" in eap._normalize_bullets(_obm))
# p1-4 copyright must stay PLAIN (no bullet glyph merged onto it)
_o14 = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p1-4.pdf", 2, fake_p14)
check("BM14 p1-4 copyright stays plain (no '• Copyright')",
      "• Copyright" not in _o14 and "- Copyright" not in eap._normalize_bullets(_o14))

# ══════════ AMBIGUOUS-MARKER GUARD (Advisor hardening 2026-07-10): no table-cell false-merge ══════════
# helper predicates
check("AG1 – / - / · are ambiguous", all(eap._is_ambiguous_bullet_glyph(g) for g in ["–", "-", "·"]))
check("AG1b • ● ▪ ‣ are NOT ambiguous (clear)", not any(eap._is_ambiguous_bullet_glyph(g) for g in ["•", "●", "▪", "‣"]))
check("AG2 prose: short code NOT prose", not eap._is_prose_right_text("IB") and not eap._is_prose_right_text("2")
      and not eap._is_prose_right_text("GI") and not eap._is_prose_right_text("N/A"))
check("AG2b prose: ≥2 sig tokens IS prose", eap._is_prose_right_text("Continued from previous section"))
check("AG2c prose: single long word (not table-cell-like) IS prose", eap._is_prose_right_text("Inductors"))

# ambiguous – + short table-cell code → NOT merged (stays 2 lines)
check("AG3 '– IB' (table cell) NOT merged",
      len(eap._merge_bullet_glyph_lines(
          [[290.2, 100.0, 294.0, 112.0, "–"], [300.0, 100.0, 315.0, 112.0, "IB"]])) == 2)
check("AG3b '– 2' (table cell) NOT merged",
      len(eap._merge_bullet_glyph_lines(
          [[290.2, 100.0, 294.0, 112.0, "–"], [300.0, 100.0, 310.0, 112.0, "2"]])) == 2)
check("AG3c '- N/A' (hyphen cell) NOT merged",
      len(eap._merge_bullet_glyph_lines(
          [[290.2, 100.0, 294.0, 112.0, "-"], [300.0, 100.0, 320.0, 112.0, "N/A"]])) == 2)
# ambiguous – + PROSE right text → merged as sub-bullet "– ..."
_agm = eap._merge_bullet_glyph_lines(
    [[46.0, 100.0, 49.0, 112.0, "–"], [60.0, 100.0, 300.0, 112.0, "Continued from previous section"]])
check("AG4 '– <prose>' merged → '– Continued...'",
      len(_agm) == 1 and _agm[0][4] == "– Continued from previous section", repr(_agm))
# CLEAR • + single word → merged (no regression)
_agc = eap._merge_bullet_glyph_lines(
    [[42.5, 100.0, 46.0, 112.0, "•"], [56.7, 100.0, 120.0, 112.0, "Inductors"]])
check("AG5 clear '• Inductors' (single word) STILL merged",
      len(_agc) == 1 and _agc[0][4] == "• Inductors", repr(_agc))

# real-PDF: ZERO ambiguous+short-code false-merges across all 3 pages of p85-87
import re as _re, fitz as _fitz
_doc = _fitz.open("input/pdf/LN08LPU_p85-87.pdf")
_fm = 0
for _pno in range(len(_doc)):
    for _l in eap._twocol_page_lines(_doc[_pno]):
        _m = _re.match(r"^([-–·])\s+(.+)$", _l[4])
        if _m:
            _r = _m.group(2).strip()
            if len(eap._sig_tokens(_r)) < 2 and " " not in _r and len(_r) <= 3:
                _fm += 1
_doc.close()
check("AG6 p85-87 line-level ambiguous false-merges == 0 (was 28)", _fm == 0, f"count={_fm}")
# and the clear • BEOL bullets STILL merge (not regressed by the guard)
check("AG7 p85-87 '• One 1x' still merges after guard",
      any("• One 1x (thin) wiring level" in _l[4] for _l in eap._twocol_page_lines(_fitz.open("input/pdf/LN08LPU_p85-87.pdf")[1])))

# ══════════ PLACEMENT table/fence guard (Advisor non-blocking rec) ══════════
_tbl = ["intro", "| a | b |", "| --- | --- |", "| 1 | 2 |", "end heading"]
check("PGT1 anchor inside table (sep row) → before table (idx1)", eap._anchor_outside_table_fence(_tbl, 2) == 1)
check("PGT2 anchor on table data row → before table (idx1)", eap._anchor_outside_table_fence(_tbl, 3) == 1)
check("PGT3 anchor outside table unchanged", eap._anchor_outside_table_fence(_tbl, 4) == 4)
_fen = ["intro", "```py", "code x", "code y", "```", "end"]
check("PGT4 anchor inside code fence → before opening fence (idx1)", eap._anchor_outside_table_fence(_fen, 3) == 1)
check("PGT5 anchor after fence closes unchanged", eap._anchor_outside_table_fence(_fen, 5) == 5)

# ══════════ F11 PATH 3 (Advisor 2026-07-10): glm-as-bullets grid → GFM table ══════════
# p85-87 p2 real grids: table0 = 'where:' 5x3 (glm rendered as bullets), table1 = Table 19 9x12.
_p2body = ("<!-- page 2 -->\n\n"
    "- One 1x (thin) wiring level in low-k dielectric\n"
    "- Up to three 1x (thin) wiring levels in ultralow-k dielectric\n"
    "- Up to six 1.7x wiring levles in ultralow-k dielectric\n"
    "- Up to two 2.7x wiring levels in low-k dielectric\n"
    "- One 8x (thick) wiring level in FTEOS/TEOS dielectric\n"
    "- Up to two 15x (thick) wiring level in FTEOS/TEOS dielectric\n\n"
    "### 3.6.1 Metal Stack Naming Convention\n\n"
    "The metal stack naming options use the following conventions:\n\n"
    "wM_xA_yB..._LM\n\n"
    "where:\n"
    "- w = Total number of metal levels in the option.\n"
    "- x = Number of metal levels of pitch A.\n"
    "- y = Number of metal levels of pitch B.\n"
    "- LM = Last metal level (for technologies that have multiple last metal possibilities).\n")
_p3out = eap._apply_grid_tables("input/pdf/LN08LPU_p85-87.pdf", 2, _p2body)
check("P3-1 where: bullets → GFM table (path3)",
      "| where: |" in _p3out and "| w | = | Total number of metal levels in the option. |" in _p3out)
check("P3-2 BEOL 6 bullets NOT tabled (genuine list preserved)",
      "- One 1x (thin) wiring level in low-k dielectric" in _p3out and "| One 1x" not in _p3out
      and sum(1 for l in _p3out.split("\n") if l.startswith("- ") and "wiring" in l) == 6)
check("P3-3 where: run no longer bullets", "- w = Total number" not in _p3out and "- LM = Last metal" not in _p3out)
# strong-overlap guard: unrelated bullets (low two-way overlap) NOT replaced even if length≈rows
_noise = ("<!-- page 2 -->\n\n### 3.6.1 Metal Stack Naming Convention\n\nwhere:\n"
    "- alpha beta gamma delta epsilon\n- zeta eta theta iota kappa\n"
    "- lambda mu nu xi omicron sigma\n- pi rho tau upsilon phi chi\n")
_p3n = eap._apply_grid_tables("input/pdf/LN08LPU_p85-87.pdf", 2, _noise)
check("P3-4 strong-guard: unrelated bullets (low overlap) NOT replaced",
      "| where: |" not in _p3n and "- alpha beta gamma delta epsilon" in _p3n)
# idempotent: re-applying path3 to its own output does not double-render
check("P3-5 path3 idempotent (table stays table)",
      eap._apply_grid_tables("input/pdf/LN08LPU_p85-87.pdf", 2, _p3out).count("| where: |") == 1)

# ══════════ GROUPED-HEADER FLATTEN (Advisor 2026-07-10): fully-qualified cols, drop bold subrow ══════════
_gg = [["Mask Level", "Description", "Minimum Dimension", ""],
       ["", "", "Line", "Space"],
       ["FN", "FinFET mandrel.", "0.032", "0.052"],
       ["FC", "Active cut.", "0.064", "0.042"]]
_gr = eap._render_grid_gfm(_gg)
check("GF1 grouped → fully-qualified header (group+sub)",
      _gr[0] == "| Mask Level | Description | Minimum Dimension Line | Minimum Dimension Space |", _gr[0])
check("GF2 no bold sub-label row emitted", not any("**" in l for l in _gr))
check("GF3 data rows intact", "| FN | FinFET mandrel. | 0.032 | 0.052 |" in _gr)
# spurious empty-header col with REDUNDANT data → dropped (no data loss)
_grd = eap._render_grid_gfm([["A", "", "B"], ["x", "x", "y"], ["p", "", "q"]])
check("GF4 spurious empty-col (redundant data) dropped",
      _grd[0] == "| A | B |" and "| x | y |" in _grd, repr(_grd[:3]))
# empty-header col with UNIQUE data → kept (no data loss)
_gru = eap._render_grid_gfm([["A", "", "B"], ["x", "z", "y"]])
check("GF5 empty-col with UNIQUE data kept (no loss)",
      _gru[0] == "| A |  | B |" and "| x | z | y |" in _gru, repr(_gru[:3]))
# non-grouped plain 2-col table unchanged
_grp = eap._render_grid_gfm([["Terminology", "Description"], ["BEOL", "Back end of line."]])
check("GF6 non-grouped plain table unchanged",
      _grp[0] == "| Terminology | Description |" and "| BEOL | Back end of line. |" in _grp)


def _render_real_grouped(pdf, page):
    pg = _fitz.open(pdf)[page - 1]
    for t in pg.find_tables().tables:
        gr = [[eap._grid_clean_cell(pg, c) for c in r.cells] for r in t.rows]
        if (eap._table_well_formed(gr) and len(gr) >= 2
                and any(not c.strip() for c in gr[0]) and not gr[1][0].strip()):
            return eap._render_grid_gfm(gr)
    return []


_t19 = _render_real_grouped("input/pdf/LN08LPU_p85-87.pdf", 2)
check("GF7 Table19 fully-qualified 'Metallization Option 9..20'",
      bool(_t19) and "Metallization Option 9" in _t19[0] and "Metallization Option 20" in _t19[0])
check("GF8 Table19 no bold subrow", bool(_t19) and not any("**" in l for l in _t19))
check("GF9 Table19 stray empty col dropped (11 cols → 12 pipes)",
      bool(_t19) and _t19[0].count("|") == 12, _t19[0] if _t19 else "none")
check("GF9b Table19 Total 1x = 3×7 intact", bool(_t19) and any("Total 1x levels |  |  | - | 3 | 3 | 3 | 3 | 3 | 3 | 3 |" in l for l in _t19))
_t2 = _render_real_grouped("input/pdf/LN08LPU_p16-18.pdf", 3)
check("GF10 Table2 'Minimum Dimension Line (µm)'/'Space (µm)'",
      bool(_t2) and "Minimum Dimension Line (µm)" in _t2[0] and "Minimum Dimension Space (µm)" in _t2[0])
check("GF11 Table2 no bold subrow + RX row intact",
      bool(_t2) and not any("**" in l for l in _t2) and any(l.startswith("| RX |") for l in _t2))

# ══════════ Y-ORDER PLACEMENT (2026-07-11): 회수 블록을 원본 y 위치에 삽입 ══════════
# 목적: glm 이 앵커(예: Figure 캡션)를 본문 말미로 재배치해도 회수 블록이 함께 밀려나지
# 않고 원본 읽기순 위치에 놓이는지 + 단조(읽기순) body 에서 구 알고리즘과 완전 동일(회귀 0).

def _place_legacy(clean_lines, covered, run_spans, recovered, body_lines):
    """구(舊) forward-match 배치 알고리즘의 정확한 복제 — 차등 회귀 기준."""
    body_line_tokens = [set(eap._sig_tokens(bl)) for bl in body_lines]
    start_to_ri = {run_spans[ri][0]: ri for ri in range(len(recovered))}
    inserts_before = {}
    append_blocks = []
    bpos = -1
    n = len(clean_lines)
    i = 0
    while i < n:
        if i in start_to_ri:
            ri = start_to_ri[i]
            _a, b = run_spans[ri]
            anchor_bidx = None
            for k in range(b, n):
                if covered[k]:
                    m = eap._best_forward_match(clean_lines[k][4], body_line_tokens, bpos)
                    if m is not None:
                        anchor_bidx = m
                        break
            if anchor_bidx is not None:
                anchor_bidx = eap._anchor_outside_table_fence(body_lines, anchor_bidx)
                inserts_before.setdefault(anchor_bidx, []).append(recovered[ri])
            else:
                append_blocks.append(recovered[ri])
            i = b
        else:
            if covered[i]:
                m = eap._best_forward_match(clean_lines[i][4], body_line_tokens, bpos)
                if m is not None:
                    bpos = m
            i += 1
    out = []
    for idx, bl in enumerate(body_lines):
        if idx in inserts_before:
            for block in inserts_before[idx]:
                out.extend(block)
                out.append("")
        out.append(bl)
    text = "\n".join(out).rstrip("\n")
    if append_blocks:
        adds = "\n\n".join("\n".join(b) for b in append_blocks)
        return text + "\n\n" + adds + "\n"
    return text + "\n"


def _diff(clean_lines, body):
    """(구 배치, 신 배치) 반환 — 동일 입력에서."""
    rec, spans, cov, _t = eap._recover_absent_blocks(clean_lines, body)
    bl = body.split("\n")
    return (_place_legacy(clean_lines, cov, spans, rec, bl),
            eap._place_recovered_blocks(clean_lines, cov, spans, rec, bl))


# ── YP1: 단조(읽기순) body — 상단 누락 블록 + 하위 covered 헤딩 → 구=신 동일 ──
_m1_clean = [
    L("One 1x thin wiring level in low-k dielectric", 40, 97),
    L("Up to three 1x thin wiring levels in ultralow-k dielectric", 40, 120),
    L("3.6.1 Metal Stack Naming Convention", 40, 188),
]
_m1_body = ("### 3.6.1 Metal Stack Naming Convention\n\n"
            "The metal stack naming options use the following conventions.")
_lg1, _ng1 = _diff(_m1_clean, _m1_body)
check("YP1 monotonic top-block: legacy == new (회귀 0)", _lg1 == _ng1)
check("YP1b new: wiring block BEFORE 3.6.1",
      "wiring" in _ng1.lower() and _ng1.lower().index("wiring") < _ng1.index("3.6.1"))

# ── YP2: 단조 body — 후미 누락 블록(하위에 covered 없음) → 둘 다 말미 append, 동일 ──
_m2_clean = [
    L("Section body intro line about widgets", 40, 100),
    L("Copyright 2023 Samsung Electronics Co Ltd all rights reserved here", 43, 601),
    L("Contact address one Samsungjeonja ro Hwaseong si Gyeonggi Do Korea", 43, 619),
]
_m2_body = "Section body intro line about widgets and other descriptive prose content."
_lg2, _ng2 = _diff(_m2_clean, _m2_body)
check("YP2 monotonic trailing-block: legacy == new (회귀 0)", _lg2 == _ng2)
check("YP2b new: copyright is trailing (append)", "Copyright" in _ng2
      and _ng2.index("Copyright") > _ng2.index("widgets"))

# ── YP3: 단조 body — 다중 누락 블록(상단 + 중간) → 구=신 동일 ──
_m3_clean = [
    L("Alpha uncovered lead paragraph describing the first topic here", 40, 90),
    L("2.1 First Covered Heading", 40, 150),
    L("Body content under first heading region describing details thoroughly", 40, 170),
    L("Beta uncovered middle paragraph inserted between two covered sections", 40, 220),
    L("2.2 Second Covered Heading", 40, 280),
]
_m3_body = ("## 2.1 First Covered Heading\n\n"
            "Body content under first heading region describing details thoroughly.\n\n"
            "## 2.2 Second Covered Heading")
_lg3, _ng3 = _diff(_m3_clean, _m3_body)
check("YP3 monotonic multi-block: legacy == new (회귀 0)", _lg3 == _ng3)

# ── YP4 (핵심 버그): glm 이 Figure 캡션을 본문 말미로 재배치 → intro 가 함께 밀리는 역전 ──
# 원본 읽기순: intro(y97) → Fig77 캡션(y383) → 4.2.9(y432) → DRC(y453)
# glm body 순서: 4.2.9 → DRC → (캡션 말미). intro 는 glm 이 누락 → 회수.
_bug_clean = [
    L("All contact and via relationships can be described by one of these three definitions", 40, 97),
    L("When necessary shapes can be identified by referring to those not part of a distinct group", 40, 109),
    L("For example referring to vias not fully aligned is a reference to all vias partially aligned", 40, 121),
    L("Figure 77: Design Rule Syntax Contact and Via Alignment Definitions", 40, 383),
    L("4.2.9  Centerline Definition", 40, 432),
    L("DRC uses the following algorithm to generate the centerline", 40, 453),
]
_bug_body = ("### 4.2.9 Centerline Definition\n\n"
             "DRC uses the following algorithm to generate the centerline.\n\n"
             "1. Create mb areas mb edges and mb line ends for all discrete width polygons.\n\n"
             "**Figure 77: Design Rule Syntax Contact and Via Alignment Definitions**")
_lg4, _ng4 = _diff(_bug_clean, _bug_body)
_ng4l = _ng4.lower()
check("YP4 new: intro placed at TOP (before 4.2.9)",
      "all contact and via" in _ng4l and _ng4l.index("all contact and via") < _ng4.index("4.2.9"),
      f"intro@{_ng4l.find('all contact and via')} 4.2.9@{_ng4.find('4.2.9')}")
check("YP4b new: intro before DRC and before Figure 77 caption",
      _ng4l.index("all contact and via") < _ng4l.index("drc uses")
      and _ng4l.index("all contact and via") < _ng4l.index("**figure 77"))
# 문서화: 구 알고리즘은 intro 를 DRC 뒤(캡션 앞)로 잘못 배치했음(회귀 아님 — 개선 증명)
check("YP4c legacy MISplaced intro after DRC (bug being fixed)",
      _lg4.lower().index("all contact and via") > _lg4.lower().index("drc uses"))

# ── YP5: 실제 PDF end-to-end — p149-151 p1, 캡션 말미 body → intro 원위치 삽입 ──
_p149_body = ("<!-- page 1 -->\n\n### 4.2.9 Centerline Definition\n\n"
              "DRC uses the following algorithm to generate the centerline.\n\n"
              "1. Create mb areas, mb edges, and mb line ends for all discrete width polygons.\n\n"
              "**Figure 77: Design Rule Syntax Contact and Via Alignment Definitions**")
_p149_out = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p149-151.pdf", 1, _p149_body)
_po = _p149_out.lower()
check("YP5 p149 p1: intro recovered", "all contact and via relationships" in _po)
check("YP5b p149 p1: intro BEFORE 4.2.9 (원본 읽기순 복원)",
      "all contact and via relationships" in _po
      and _po.index("all contact and via relationships") < _po.index("4.2.9"),
      f"intro@{_po.find('all contact and via relationships')} 4.2.9@{_po.find('4.2.9')}")
check("YP5c p149 p1: intro before DRC algorithm",
      _po.index("all contact and via relationships") < _po.index("drc uses the following"))
# idempotent (두 번 적용해도 동일)
check("YP5d p149 placement idempotent",
      eap._apply_hybrid_completeness("input/pdf/LN08LPU_p149-151.pdf", 1, _p149_out) == _p149_out)

# ── YP6: 실 PDF 회귀 재확인 — p85-87(불릿 원위치) / p1-4(저작권 말미) 신 배치 유지 ──
_yp_p85 = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p85-87.pdf", 2,
    "<!-- page 2 -->\n\n### 3.6.1 Metal Stack Naming Convention\n\n"
    "The metal stack naming options use the following conventions.")
check("YP6 p85-87 bullets still BEFORE 3.6.1",
      "wiring" in _yp_p85.lower() and _yp_p85.lower().index("wiring") < _yp_p85.index("3.6.1"))
_yp_p14 = eap._apply_hybrid_completeness("input/pdf/LN08LPU_p1-4.pdf", 2,
    "<!-- page 2 -->\n\n## Important Notice\n\nSamsung Electronics Co. Ltd. reserves the "
    "right to make changes to the information in this publication at any time without prior notice.")
check("YP6b p1-4 copyright still trailing (append)",
      "tom82.kim@samsung.com" in _yp_p14
      and _yp_p14.index("Copyright") > _yp_p14.index("reserves the right"))


# ── R13(2026-07-16): 표 셀 텍스트가 loose-prose 로 회수되는 dup-prose 방지 ──────────
# (a) exclude_mask: True 인 인덱스는 강제 covered → 복구 제외. None 이면 기존 동작 불변.
_r_none, _s_none, _c_none, _t_none = eap._recover_absent_blocks(COPY, body_missing)
check("R13 exclude_mask=None → 기존 동작 유지(7 recovered)", _t_none == 7)
_mask_all = [True] * len(COPY)
_rm, _sm, _cm, _tm = eap._recover_absent_blocks(COPY, body_missing, exclude_mask=_mask_all)
check("R13 exclude_mask all-True → 0 recovered", _tm == 0, repr(_tm))
_mask_partial = [i >= 4 for i in range(len(COPY))]   # 마지막 3줄만 마스크
_rp, _sp2, _cp, _tp = eap._recover_absent_blocks(COPY, body_missing, exclude_mask=_mask_partial)
_flatp = [l for run in _rp for l in run]
check("R13 exclude_mask partial → 마스크 라인 미복구",
      _tp == 4 and all(not l.startswith(("Contact", "TEL", "Home")) for l in _flatp),
      repr(_flatp))

# (b) 실 PDF 회귀(중복 유발 픽스처): grid 치환에서 Table 10 이 드롭된 body(설명 셀이
#     본문에 부재)에 완전성 가드를 걸어도, 표 셀 문장이 loose-prose 로 회수되지 않는다.
#     패치 전에는 3개 설명 문장이 표 밖 평문으로 회수돼 dup-prose 가 발생했다.
_dm_after = (
    "3.2.7 Reserved Design and Utility Levels CAD Layer Table\n\n"
    "| CAD Level | GDSII / OASIS Layer No. | GDSII / OASIS Data Type | Description |\n"
    "| :--- | ---: | ---: | :--- |\n"
    "| ADM | 212 | 3 | Reserved. |\n"
    "| SCHKY | 212 | 59 | Reserved. |\n"
)
_dm_out = eap._apply_hybrid_completeness("input/pdf/DM_p0018-0047.pdf", 28, _dm_after)
check("R13 DM p28 dup-prose 제거: 'Boundary Layer' 문장 미회수",
      "Boundary Layer for cell Level dummy generation." not in _dm_out)
check("R13 DM p28 dup-prose 제거: TRANSISTORCELL 설명 문장 미회수",
      "transistor-like dummy cell" not in _dm_out
      and "generated level by PDK fill deck" not in _dm_out)

# (c) _table_region_mask: grid 셀 안 라인은 True(설명 라인 마스킹) — bbox 계약 검증.
import fitz as _fitz  # noqa: E402
_doc = _fitz.open("input/pdf/DM_p0018-0047.pdf")
_clean28 = eap._twocol_page_lines(_doc[27])
_doc.close()
_mask28 = eap._table_region_mask("input/pdf/DM_p0018-0047.pdf", 28, _clean28)
_desc_masked = bool(_mask28) and all(
    _mask28[i] for i, l in enumerate(_clean28)
    if ("Boundary Layer for cell Level" in l[4]
        or "TRANSISTORCELL is generated" in l[4]))
check("R13 _table_region_mask 가 표 셀 라인을 True 로 표시", _desc_masked)


print("\n=== SUMMARY ===")
print(f"FAILURES: {FAILS if FAILS else 'NONE — ALL PASS'}")
sys.exit(1 if FAILS else 0)
