#!/usr/bin/env python3
"""R13b 단위테스트 — 연속 동일제목 헤딩 자동제거(_dedup_consecutive_headings).
GPU/모델 불필요(순수 문자열 후처리). FMDW_NO_UNLOAD=1 로 안전 실행."""
import os
os.environ.setdefault("FMDW_NO_UNLOAD", "1")
import importlib

m = importlib.import_module("extract_all_via_pdf")
fn = m._dedup_consecutive_headings

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (f" ({extra})" if extra and not cond else ""))

os.environ["FMDW_HEADING_DEDUP"] = "1"

# ── Case 1: 번호 있는 제목 + 무번호(동일) 제목 → 뒤엣것 제거, 단일 공백줄만 남음 ──
md1 = """| PM | 749 | 0 | desc |

## 3.2.3 SRAM Design and Utility Levels CAD Layer Table

### SRAM Design and Utility Levels CAD Layer Table

**Table 5: SRAM Design and Utility Levels CAD Layer Table**

| A | B |
"""
o1 = fn(md1)
check("C1 duplicate heading removed",
      "### SRAM Design and Utility Levels CAD Layer Table" not in o1, repr(o1))
check("C1 first (numbered) heading kept",
      "## 3.2.3 SRAM Design and Utility Levels CAD Layer Table" in o1)
check("C1 single blank line between kept heading and content",
      "## 3.2.3 SRAM Design and Utility Levels CAD Layer Table\n\n**Table 5:" in o1,
      repr(o1))
check("C1 no double-blank artifact", "\n\n\n" not in o1)

# ── Case 2: 서로 다른 제목 → 둘 다 보존(비제거) ──
md2 = """## 3.2.3 SRAM Design and Utility Levels CAD Layer Table

### A Completely Different Subsection Title

Some content.
"""
o2 = fn(md2)
check("C2 different titles both kept",
      "## 3.2.3 SRAM Design and Utility Levels CAD Layer Table" in o2
      and "### A Completely Different Subsection Title" in o2)
check("C2 unchanged (no removal)", o2 == md2)

# ── Case 3: 사이에 비공백 내용(본문)이 있으면 비제거 ──
md3 = """## 3.2.3 SRAM Design and Utility Levels CAD Layer Table

Some intervening prose that must block dedup.

### SRAM Design and Utility Levels CAD Layer Table

More content.
"""
o3 = fn(md3)
check("C3 content between headings blocks dedup (unchanged)", o3 == md3, repr(o3))

# ── Case 4: 코드펜스 내부의 동일제목 헤딩류 라인은 대상에서 제외(비제거) ──
md4 = """## Setup Guide

```
## Setup Guide

### Setup Guide
```

Real content after fence.
"""
o4 = fn(md4)
check("C4 fenced pseudo-headings untouched (unchanged)", o4 == md4, repr(o4))

# ── Case 5: 페이지 마커가 사이에 개재하면 비제거(페이지 마커=내용으로 취급) ──
md5 = """## 3.2.3 SRAM Design and Utility Levels CAD Layer Table

<!-- page 9 -->

### SRAM Design and Utility Levels CAD Layer Table

Content.
"""
o5 = fn(md5)
check("C5 page-marker-separated headings both kept (unchanged)", o5 == md5, repr(o5))

# ── Case 6: 게이트 OFF(FMDW_HEADING_DEDUP=0) → 완전 비활성(회귀 탈출구) ──
os.environ["FMDW_HEADING_DEDUP"] = "0"
o6 = fn(md1)
check("C6 gate off leaves md unchanged", o6 == md1, repr(o6))
os.environ["FMDW_HEADING_DEDUP"] = "1"

# ── Case 7: 3중 체인(같은 제목 헤딩이 연속 3번) → 첫 번째만 남고 나머지 전부 제거 ──
md7 = """## Chain Title

### Chain Title

#### Chain Title

Real content.
"""
o7 = fn(md7)
check("C7 chain of 3 collapses to first only",
      o7.count("Chain Title") == 1 and "## Chain Title" in o7, repr(o7))

# ── Case 8: 빈 문자열/None 안전 처리 ──
check("C8 empty string passthrough", fn("") == "")

# ── Case 9(2026-07-16 Advisor 정정): 양쪽 다 번호 있고 번호가 다르면(1 vs 1.1) 절대
#   미제거 — 장 표제 직후 동명 첫 절(정당한 구조)을 오제거하던 버그 회귀 가드 ──
md9 = """# 1 Overview

## 1.1 Overview

Some real body content.
"""
o9 = fn(md9)
check("C9 numbered chapter + numbered first-section (different numbers) both kept",
      o9 == md9, repr(o9))
check("C9 no accidental removal of '## 1.1 Overview'", "## 1.1 Overview" in o9)

# ── Case 9b: 규칙(b) 원문 완전동일(레벨만 다름, 번호 포함 동일) → 여전히 제거 ──
md9b = """## 3.2.3 SRAM Design and Utility Levels CAD Layer Table

### 3.2.3 SRAM Design and Utility Levels CAD Layer Table

Content.
"""
o9b = fn(md9b)
check("C9b rule(b) exact-title-incl-number duplicate still removed",
      o9b.count("SRAM Design and Utility Levels CAD Layer Table") == 1
      and "## 3.2.3 SRAM Design and Utility Levels CAD Layer Table" in o9b, repr(o9b))

# ── Case 10: qa_fix.py F4(scan_heading_dedup/apply_fixes) 도 동일 결과를 내는지 교차검증 ──
qf = importlib.import_module("qa_fix")
lines1 = md1.split("\n")
fixes1 = qf.scan_heading_dedup(lines1)
applied1 = "\n".join(qf.apply_fixes(list(lines1), fixes1))
check("C10 qa_fix F4 removes duplicate heading",
      "### SRAM Design and Utility Levels CAD Layer Table" not in applied1, repr(applied1))
check("C10 qa_fix F4 matches extract_all_via_pdf dedup output", applied1 == o1,
      f"applied1={applied1!r} o1={o1!r}")
fixes2 = qf.scan_heading_dedup(md2.split("\n"))
check("C10b qa_fix F4 no false positive on different titles", fixes2 == [])
fixes3 = qf.scan_heading_dedup(md3.split("\n"))
check("C10c qa_fix F4 no removal when content intervenes", fixes3 == [])
fixes9 = qf.scan_heading_dedup(md9.split("\n"))
check("C10d qa_fix F4 SSoT: numbered chapter/section (different numbers) → no fixes",
      fixes9 == [], repr(fixes9))
lines9b = md9b.split("\n")
fixes9b = qf.scan_heading_dedup(lines9b)
applied9b = "\n".join(qf.apply_fixes(list(lines9b), fixes9b))
check("C10e qa_fix F4 SSoT matches extract_all_via_pdf on rule(b) case",
      applied9b == o9b, f"applied9b={applied9b!r} o9b={o9b!r}")

print("\n=== SUMMARY ===")
passed = sum(1 for _, c in results if c)
print(f"{passed}/{len(results)} checks passed")
raise SystemExit(0 if passed == len(results) else 1)
