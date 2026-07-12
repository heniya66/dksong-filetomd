#!/usr/bin/env python3
"""Feature B 단위테스트 — 페이지/청크 걸침 표·도면 캡션 반복(_apply_caption_continuation).
GPU/모델 불필요(순수 문자열 후처리). FMDW_NO_UNLOAD=1 로 안전 실행."""
import os
os.environ.setdefault("FMDW_NO_UNLOAD", "1")
import importlib

m = importlib.import_module("extract_all_via_pdf")
fn = m._apply_caption_continuation

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name)

# ── Case 1: 표 걸침, 이어지는 표가 동일 헤더 반복 → 캡션만 삽입 ──
md1 = """<!-- page 5 -->

**Table 21: Truth Table**

| A | B | Y |
| :--- | :--- | :--- |
| 0 | 0 | 1 |
| 0 | 1 | 0 |

<!-- page 6 -->

| A | B | Y |
| :--- | :--- | :--- |
| 1 | 0 | 0 |
| 1 | 1 | 1 |

<!-- page 7 -->

Some following prose.
"""
os.environ["FMDW_CAPTION_CONTINUATION"] = "1"
o1 = fn(md1)
check("C1 continuation caption inserted", "**Table 21: Truth Table (continued)**" in o1)
check("C1 caption above page6 table (before data row)",
      o1.index("**Table 21: Truth Table (continued)**") < o1.index("| 1 | 0 | 0 |"))
check("C1 caption sits after page6 marker",
      o1.index("<!-- page 6 -->") < o1.index("**Table 21: Truth Table (continued)**"))
check("C1 no double insertion (exactly one continued)",
      o1.count("(continued)") == 1)
check("C1 all original data rows preserved",
      all(r in o1 for r in ["| 0 | 0 | 1 |", "| 0 | 1 | 0 |", "| 1 | 0 | 0 |", "| 1 | 1 | 1 |"]))

# ── Case 2: 이어지는 표 첫 행이 데이터(GFM 강제 헤더) → 원 헤더 prepend + 데이터 강등 ──
md2 = """<!-- page 5 -->

**Table 8: Registers**

| Reg | Addr | Value |
| :--- | :--- | :--- |
| R0 | 0x00 | 1 |

<!-- page 6 -->

| R1 | 0x04 | 2 |
| :--- | :--- | :--- |
| R2 | 0x08 | 3 |
"""
o2 = fn(md2)
check("C2 continuation caption inserted", "**Table 8: Registers (continued)**" in o2)
check("C2 original header repeated on page6",
      o2.split("<!-- page 6 -->")[1].count("| Reg | Addr | Value |") == 1)
check("C2 demoted row preserved as data", "| R1 | 0x04 | 2 |" in o2)
check("C2 R2 data preserved", "| R2 | 0x08 | 3 |" in o2)

# ── Case 3: 별개 표(다음 페이지 표에 자체 캡션 존재) → 무삽입 ──
md3 = """<!-- page 5 -->

**Table 21: Truth Table**

| A | B | Y |
| :--- | :--- | :--- |
| 0 | 0 | 1 |

<!-- page 6 -->

**Table 22: Pin Map**

| A | B | Y |
| :--- | :--- | :--- |
| 1 | 0 | 0 |
"""
o3 = fn(md3)
check("C3 separate captioned table → no continuation", "(continued)" not in o3)
check("C3 unchanged", o3 == md3)

# ── Case 4: 비연속 페이지(5→8) → 무삽입 ──
md4 = md1.replace("<!-- page 6 -->", "<!-- page 8 -->")
o4 = fn(md4)
check("C4 non-consecutive pages → no continuation", "(continued)" not in o4)

# ── Case 5: 열수 불일치 → 무삽입 ──
md5 = """<!-- page 5 -->

**Table 9: X**

| A | B | Y |
| :--- | :--- | :--- |
| 0 | 0 | 1 |

<!-- page 6 -->

| A | B |
| :--- | :--- |
| 1 | 0 |
"""
o5 = fn(md5)
check("C5 column-count mismatch → no continuation", "(continued)" not in o5)
check("C5 unchanged", o5 == md5)

# ── Case 6: 게이트 OFF → 바이트 동일(회귀 0) ──
os.environ["FMDW_CAPTION_CONTINUATION"] = "0"
o6 = fn(md1)
check("C6 gate OFF byte-identical", o6 == md1)
os.environ["FMDW_CAPTION_CONTINUATION"] = "1"

# ── Case 7: 멱등(두 번 적용 == 한 번) ──
o7 = fn(fn(md1))
check("C7 idempotent (case1)", o7 == o1)
o7b = fn(fn(md2))
check("C7 idempotent (case2)", o7b == o2)

# ── Case 8: 도면 걸침 — 이전 페이지 말미 Figure N 캡션+이미지, 다음 페이지 첫 콘텐츠=bare 이미지 ──
md8 = """<!-- page 5 -->

**Figure 3: System Block Diagram**

![fig](figures/x_p5_fig1.png)

<!-- page 6 -->

![fig](figures/x_p6_fig1.png)

<!-- page 7 -->

text after
"""
o8 = fn(md8)
check("C8 figure continuation caption inserted",
      "**Figure 3: System Block Diagram (continued)**" in o8)
check("C8 caption above continuation image",
      o8.index("(continued)") < o8.index("figures/x_p6_fig1.png"))
check("C8 exactly one continued", o8.count("(continued)") == 1)

# ── Case 9: 도면 — 다음 페이지 첫 콘텐츠가 이미지 아님(텍스트) → 무삽입 ──
md9 = """<!-- page 5 -->

**Figure 3: Block**

![fig](figures/x_p5_fig1.png)

<!-- page 6 -->

Regular paragraph text, not an image.
"""
o9 = fn(md9)
check("C9 non-image next content → no figure continuation", "(continued)" not in o9)
check("C9 unchanged", o9 == md9)

# ── Case 10: 청크(`---`) 경계 걸침 표 → 검출 ──
md10 = """<!-- page 5 -->

**Table 30: Registers**

| Reg | Val |
| :--- | :--- |
| R0 | 1 |

---

<!-- page 6 -->

| Reg | Val |
| :--- | :--- |
| R1 | 2 |
"""
o10 = fn(md10)
check("C10 chunk-boundary straddle detected",
      "**Table 30: Registers (continued)**" in o10)

# ── Case 11: 걸침 없는 일반 표 → 무변화(배포 MD 무해성) ──
md11 = """<!-- page 5 -->

**Table 1: Simple**

| A | B |
| :--- | :--- |
| 1 | 2 |

More prose here that is not a table, so table does not touch page bottom.

<!-- page 6 -->

| C | D |
| :--- | :--- |
| 3 | 4 |
"""
o11 = fn(md11)
check("C11 non-straddling table unchanged", o11 == md11)

# ── Case 12: 3페이지 걸침 → 각 조각에 단일 (continued) ──
md12 = """<!-- page 5 -->

**Table 40: Big**

| A | B |
| :--- | :--- |
| 1 | 2 |

<!-- page 6 -->

| A | B |
| :--- | :--- |
| 3 | 4 |

<!-- page 7 -->

| A | B |
| :--- | :--- |
| 5 | 6 |
"""
o12 = fn(md12)
check("C12 three-page straddle: two continuation captions",
      o12.count("**Table 40: Big (continued)**") == 2)
check("C12 no double-continued suffix", "(continued) (continued)" not in o12)

print("\n=== SUMMARY ===")
passed = sum(1 for _, c in results if c)
print(f"{passed}/{len(results)} checks passed")
raise SystemExit(0 if passed == len(results) else 1)
