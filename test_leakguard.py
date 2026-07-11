"""Unit tests for the two leak guards added 2026-07-10 (leakguard).

Guard A: _translation_looks_meta — translation-model meta-response detection.
Guard B: _strip_prompt_leak (FIX A3) — prompt-directive line-level filter.

Verifies the two observed LN08LPU p98-102 leaks are removed and that normal
body (definition-table cells, Note:, captions, plain sentences, code fences)
is preserved. Pure-function tests — no network / no ollama needed.
"""
import extract_all_via_pdf as E


# ── Guard A: translation meta-response ──────────────────────────────────────
def test_meta_observed_leak():
    # The exact meta line injected into p98-102.
    assert E._translation_looks_meta(
        "Please provide the Markdown text you would like me to translate. "
        "It appears that no content was included after your instructions."
    ) is True


def test_meta_variants():
    for s in [
        "I need the text to translate.",
        "There is no text to translate.",
        "It seems you haven't provided any content.",
        "Please provide the content you would like me to translate.",
    ]:
        assert E._translation_looks_meta(s) is True, s


def test_meta_empty_is_not_meta():
    # Empty is handled by the existing `if out` guard, not the meta guard.
    assert E._translation_looks_meta("") is False
    assert E._translation_looks_meta("   ") is False


def test_meta_normal_translation_preserved():
    for s in [
        "The power supply must be within the specified voltage range.",
        "Assigns level A to the operands B1, B2, and B3 in the netlist.",
        "This chapter describes the signal timing and reset behavior.",
        "A operator (B1, B2, B3) | Assigns level A to each listed net.",
    ]:
        assert E._translation_looks_meta(s) is False, s


def test_meta_long_text_not_flagged():
    # A long, genuine translation that coincidentally quotes a phrase must NOT
    # be dropped — the length guard protects real paragraphs.
    long = ("The following section explains the procedure in detail. " * 12
            + "please provide the text as needed.")
    assert len(long) > 400
    assert E._translation_looks_meta(long) is False


# ── Guard B: prompt-directive line filter (FIX A3) ──────────────────────────
def test_strip_observed_directive_leak_line():
    md = (
        "Some real body text on this page.\n"
        "TABLE OF CONTENTS pages: When a page region shows a left column of "
        "short bold syntax/terms with its description in a right column, "
        "transcribe it as a GFM table `| Syntax | Description |` header for "
        "this page's rows.\n"
        "More real body text.\n"
    )
    out = E._strip_prompt_leak(md)
    assert "When a page region shows a left column of short bold" not in out
    assert "transcribe it as a GFM table" not in out
    assert "Some real body text on this page." in out
    assert "More real body text." in out


def test_strip_various_directive_lines():
    # Directive lines interleaved with real body (blank-line-separated paragraphs,
    # matching how these leaks actually appear in transcription output). A3 removes
    # the directive lines line-by-line; the real sentences survive.
    md = (
        "Real sentence one about the clock tree.\n"
        "TWO-COLUMN TERM/DESCRIPTION LAYOUT: emit a table.\n"
        "\n"
        "Never output the same content twice. Transcribe each block once.\n"
        "Keep this real sentence about the reset controller.\n"
    )
    out = E._strip_prompt_leak(md)
    assert "TWO-COLUMN TERM/DESCRIPTION LAYOUT" not in out
    assert "Never output the same content twice. Transcribe" not in out
    assert "Real sentence one about the clock tree." in out
    assert "Keep this real sentence about the reset controller." in out


def test_strip_preserves_definition_table_and_note_and_caption():
    md = (
        "| Syntax | Description |\n"
        "| --- | --- |\n"
        "| A operator (B1, B2, B3) | Assigns level A to the listed nets. |\n"
        "| A length | The longer of the two dimensions. |\n"
        "\n"
        "**Note:** This applies only to metal layers.\n"
        "**Table 5: Design rules summary**\n"
        "**Figure 1: Cross section of the device**\n"
        "The operator assigns a logic level to each net in reading order.\n"
    )
    out = E._strip_prompt_leak(md)
    for keep in [
        "| A operator (B1, B2, B3) | Assigns level A to the listed nets. |",
        "| A length | The longer of the two dimensions. |",
        "**Note:** This applies only to metal layers.",
        "**Table 5: Design rules summary**",
        "**Figure 1: Cross section of the device**",
        "The operator assigns a logic level to each net in reading order.",
    ]:
        assert keep in out, keep


def test_strip_preserves_normal_prose_with_short_slices():
    # Advisor NEEDS_FIX: the 3-word slices must NOT drop normal prose that merely
    # contains those common words. The widened signatures require the long prompt
    # context, so these plain sentences survive.
    md = (
        "The tool must reproduce every entry in the exclusion list.\n"
        "The device must never output the same content twice per frame.\n"
        "There are no blank shortcuts on the front panel of the board.\n"
        "This procedure describes the reset sequence in detail.\n"
    )
    out = E._strip_prompt_leak(md)
    for keep in [
        "The tool must reproduce every entry in the exclusion list.",
        "The device must never output the same content twice per frame.",
        "There are no blank shortcuts on the front panel of the board.",
        "This procedure describes the reset sequence in detail.",
    ]:
        assert keep in out, keep


def test_strip_still_removes_long_context_leaks():
    # The actual leak forms (verbatim prompt context) must STILL be removed.
    md = (
        "Real body line about the PLL.\n"
        "\n"
        "reproduce EVERY entry (including the very first ones) in page-number order.\n"
        "\n"
        "Never output the same content twice. Transcribe each block exactly once.\n"
        "\n"
        "NO BLANK SHORTCUTS: Every page in this document contains content.\n"
        "\n"
        "Another real body line.\n"
    )
    out = E._strip_prompt_leak(md)
    assert "reproduce EVERY entry (including" not in out
    assert "Never output the same content twice. Transcribe" not in out
    assert "NO BLANK SHORTCUTS: Every page" not in out
    assert "Real body line about the PLL." in out
    assert "Another real body line." in out


def test_strip_preserves_code_fence_contents():
    md = (
        "```\n"
        "transcribe it as a GFM table  # this is inside a code fence, keep it\n"
        "```\n"
        "Real body sentence outside the fence.\n"
    )
    out = E._strip_prompt_leak(md)
    assert "transcribe it as a GFM table  # this is inside a code fence, keep it" in out
    assert "Real body sentence outside the fence." in out


def test_a4_removes_figures_label_dup_of_caption():
    # Observed p106-112 p6 leak: FIGURES: line duplicates the following caption.
    md = (
        "FIGURES: A center to center space, A center to center space to B\n"
        "\n"
        "**Figure 17: A center to center space, A center to center space to B**\n"
        "\n"
        "![Figure 17: A center to center space, A center to center space to B]"
        "(figures/x.png)\n"
    )
    out = E._strip_prompt_leak(md)
    assert "FIGURES: A center to center space" not in out
    # The real caption and image MUST be preserved.
    assert "**Figure 17: A center to center space, A center to center space to B**" in out
    assert "![Figure 17: A center to center space" in out


def test_a4_removes_tables_label_dup():
    md = (
        "TABLES: Metallization option summary\n"
        "**Table 5: Metallization option summary**\n"
    )
    out = E._strip_prompt_leak(md)
    assert "TABLES: Metallization option summary" not in out
    assert "**Table 5: Metallization option summary**" in out


def test_a4_preserves_unique_figures_line():
    # A FIGURES: line with content NOT matching any nearby line must be kept
    # (no proven duplication -> no removal).
    md = (
        "FIGURES: this is unique standalone content about layout\n"
        "\n"
        "**Figure 9: a completely different caption topic**\n"
        "\n"
        "Some ordinary body sentence follows here.\n"
    )
    out = E._strip_prompt_leak(md)
    assert "FIGURES: this is unique standalone content about layout" in out
    assert "**Figure 9: a completely different caption topic**" in out


def test_a4_removes_distant_h3_caption_dup():
    # Observed p118-121: FIGURES: line duplicates an ### Figure caption 7 lines
    # away, with a separator and orphan-bucket marker in between (past old window).
    md = (
        "**Figure 34: A overlap past B with coinciding permitted**\n"
        "\n"
        "![Figure 34: A overlap past B with coinciding permitted](figures/a.png)\n"
        "\n"
        "FIGURES: A C-overlap past B on both sides, A P-overlap past B on both sides\n"
        "\n"
        "---\n"
        "\n"
        "<!-- fmdw:extracted-figures -->\n"
        "\n"
        "### Figure 35: A C-overlap past B on both sides, A P-overlap past B on both sides\n"
        "\n"
        "![Figure 35: A C-overlap past B on both sides, A P-overlap past B on both sides](figures/b.png)\n"
    )
    out = E._strip_prompt_leak(md)
    assert "FIGURES: A C-overlap past B on both sides" not in out
    # Both real captions preserved.
    assert "**Figure 34: A overlap past B with coinciding permitted**" in out
    assert "### Figure 35: A C-overlap past B on both sides, A P-overlap past B on both sides" in out


def test_a4_preserves_unique_figures_line_docwide():
    # No caption anywhere matches -> the FIGURES: line is kept (zero false removal).
    md = (
        "FIGURES: unique standalone layout guidance with no matching caption\n"
        "\n"
        "**Figure 8: a totally different subject matter here**\n"
        "\n"
        "### Figure 9: yet another unrelated caption line\n"
    )
    out = E._strip_prompt_leak(md)
    assert "FIGURES: unique standalone layout guidance with no matching caption" in out
    assert "**Figure 8: a totally different subject matter here**" in out
    assert "### Figure 9: yet another unrelated caption line" in out


def test_a4_idempotent_on_clean_captions():
    clean = (
        "**Figure 3: cross section**\n"
        "\n"
        "![Figure 3: cross section](figures/y.png)\n"
    )
    assert E._strip_prompt_leak(clean) == clean


def test_strip_idempotent_on_clean_body():
    clean = (
        "# 4.2.1 Operators\n\n"
        "| Syntax | Description |\n"
        "| --- | --- |\n"
        "| A operator (B1, B2, B3) | Assigns level A... |\n\n"
        "**Note:** normal note.\n"
    )
    assert E._strip_prompt_leak(clean) == clean


# ── _twocol_parse_run: long multi-line left TERM must still form a table row ──
def _ln(text, x, y, h=12, w=150):
    return [float(x), float(y), float(x + w), float(y + h), text]


def _twocol_overlap_lines(long_term_lines):
    # Synthetic p118-like layout: left terms (x0=46) + right descriptions (x0=301).
    # r_x detection needs >=4 right-column lines; left col confined (x1<r_x-6).
    lines = []
    # row 1: short term + 3 desc lines
    lines.append(_ln("A overlap past B", 46, 100, w=120))
    for k, dy in enumerate((100, 112, 124)):
        lines.append(_ln(f"desc one part {k} of the first overlap rule", 301, dy, w=240))
    # row 2: LONG multi-line term (2-4 lines) + 2 desc lines
    for i, t in enumerate(long_term_lines):
        lines.append(_ln(t, 46, 160 + 12 * i, w=240))
    lines.append(_ln("See A overlap past B, the value N of the four", 301, 160, w=240))
    lines.append(_ln("directions can be defined between A and B.", 301, 172, w=240))
    # row 3
    lines.append(_ln("A C-overlap past B", 46, 210, w=120))
    lines.append(_ln("C-overlap description first line here", 301, 210, w=240))
    lines.append(_ln("C-overlap description second line here", 301, 222, w=240))
    # row 4
    lines.append(_ln("A P-overlap past B", 46, 250, w=120))
    lines.append(_ln("P-overlap description first line here", 301, 250, w=240))
    lines.append(_ln("P-overlap description second line here", 301, 262, w=240))
    return lines


def test_twocol_long_3line_term_renders_table():
    # 3-line, ~122-char left term (the p118 'A minimum overlap past B ...' case)
    # must NOT abort the page — it is a real row, gated by fill<0.55.
    long_term = [
        "A minimum overlap past B for the second side is greater",
        "than N with the third side and the fourth side each also",
        "greater than N (rectangular enclosure).",
    ]
    joined = " ".join(long_term)
    assert 120 < len(joined) <= 200  # exceeds old cap, within new
    segs = E._twocol_analyze(_twocol_overlap_lines(long_term))
    assert segs is not None
    rows = [p for k, p in segs if k == "table"]
    allrows = [r for tbl in rows for r in tbl]
    syntaxes = [s for s, d in allrows]
    assert any(s.startswith("A overlap past B") for s in syntaxes)
    assert any("rectangular enclosure" in s for s in syntaxes)
    assert any(s.startswith("A C-overlap past B") for s in syntaxes)
    assert any(s.startswith("A P-overlap past B") for s in syntaxes)


def test_twocol_4line_left_entry_still_aborts():
    # A >3-line left entry (strong 2-col-prose signal) must still ABORT -> None.
    four = [
        "This is a running prose paragraph line number one here",
        "and it continues on to a second full width prose line",
        "and then a third prose line keeps going without stop",
        "and finally a fourth prose line making it clearly prose",
    ]
    segs = E._twocol_analyze(_twocol_overlap_lines(four))
    assert segs is None


# ── Defect 1: page-straddle table-cell continuation merge ────────────────────
_STRADDLE_TABLE = (
    "| Syntax | Description |\n"
    "| :--- | :--- |\n"
    "| A P-overlap past B | A P-overlap past B, the spacing of all inside P-edges "
    "of shapes on level A; coinciding is prohibited. Measured in an opposite |"
)


def test_straddle_merges_lowercase_continuation():
    md = (
        _STRADDLE_TABLE + "\n\n---\n\n<!-- page 2 -->\n\n"
        "fashion and distance is checked with run length > 0µm unless otherwise "
        "specified. B P-overlap past A is not identical (not follow commutative law).\n\n"
        "| Syntax | Description |\n| :--- | :--- |\n| A overlap | Some other desc. |"
    )
    out = E._merge_straddle_continuation(md)
    # continuation appended to the last cell of the page-1 table
    assert "Measured in an opposite fashion and distance is checked" in out
    assert "commutative law). |" in out
    # standalone floating paragraph removed (only appears inside the cell now)
    assert out.count("fashion and distance is checked") == 1
    assert "\nfashion and distance is checked" not in out
    # page-2 table untouched
    assert "| A overlap | Some other desc. |" in out


def test_straddle_preserves_uppercase_new_paragraph():
    md = (
        _STRADDLE_TABLE + "\n\n---\n\n<!-- page 2 -->\n\n"
        "Fashion here starts a brand new uppercase sentence unrelated to the table.\n"
    )
    out = E._merge_straddle_continuation(md)
    assert "Fashion here starts a brand new uppercase sentence" in out
    # not merged into the cell
    assert "opposite Fashion here" not in out


def test_straddle_preserves_after_completed_cell():
    # If the last cell already ends with terminal punctuation, a following
    # lowercase paragraph is genuine new content -> not merged.
    complete = (
        "| Syntax | Description |\n| :--- | :--- |\n"
        "| A overlap | fully complete sentence ending here. |"
    )
    md = complete + "\n\n<!-- page 2 -->\n\nlowercase note that is its own content here.\n"
    out = E._merge_straddle_continuation(md)
    assert "lowercase note that is its own content here." in out
    assert "ending here. lowercase note" not in out


# ── Defect 2: figure-internal label leaked into body prose ───────────────────
def test_figlabel_drops_caption_duplicate_label():
    md = (
        "Figure 34: A overlap past B with coinciding permitted\n\n"
        "A C-overlap past B on both sides $\\geq x$ ($x > 0$)\n\n"
        "A P-overlap past B on both sides $\\geq x$ ($x > 0$)\n\n"
        "Figure 35: A C-overlap past B on both sides, A P-overlap past B on both sides\n\n"
        "The spacing of all inside edges of shapes on level A to level B is checked."
    )
    out = E._drop_figure_label_leak(md)
    assert "A C-overlap past B on both sides $" not in out
    assert "A P-overlap past B on both sides $" not in out
    # captions preserved
    assert "Figure 35: A C-overlap past B on both sides, A P-overlap past B on both sides" in out
    assert "Figure 34: A overlap past B with coinciding permitted" in out
    # normal prose preserved
    assert "The spacing of all inside edges of shapes on level A to level B is checked." in out


def test_figlabel_preserves_definition_prose():
    # A real definition/prose paragraph that is NOT a substring of any caption
    # must never be dropped, even adjacent to a figure caption.
    md = (
        "Figure 40: cross section of the device layer stack\n\n"
        "The threshold voltage of the device is set by gate oxide thickness and channel "
        "doping under nominal operating conditions.\n\n"
        "Another ordinary sentence about layout rules and spacing constraints here."
    )
    out = E._drop_figure_label_leak(md)
    assert "The threshold voltage of the device is set by gate oxide thickness" in out
    assert "Another ordinary sentence about layout rules" in out


def test_figlabel_idempotent_and_no_caption_noop():
    md = "Just a plain paragraph.\n\nAnother plain paragraph with no figures at all."
    assert E._drop_figure_label_leak(md) == md


if __name__ == "__main__":
    import sys
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
