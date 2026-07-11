"""가설검증: 도메인 라우팅 OFF(qwen3-vl) + 새 프롬프트로 p1~3 변환 → 표지/마커/목차 나오나."""
from pathlib import Path
from extract_all_via_pdf import extract_chunk

pdf = Path("input/pdf/test_pages/LN08LPU_Design_Manual_A00-V0.9.2.0_testpages.pdf")
r = extract_chunk(pdf, 1, 3, 1)
md = r[0] if isinstance(r, tuple) else r
Path("qwen_p1_3.md").write_text(md or "<EMPTY>", encoding="utf-8")
print("[len]", len(md or ""))
print("[page markers]", (md or "").count("<!-- page"))
for kw in ("Important Notice", "Design Manual", "List of Tables", "Table of Contents", "Audience"):
    print("  %-18s %s" % (kw, "YES" if kw in (md or "") else "no"))
print("[QWEN TEST DONE]")
