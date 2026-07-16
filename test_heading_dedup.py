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

# ── Case 11(R13c): doc_audit.py 의 _scan_heading_dup(검출 전용, SSoT 동치) ──
da = importlib.import_module("doc_audit")

dup11_1 = da._scan_heading_dup(md1.split("\n"))
check("C11a doc_audit detects rule(a) duplicate heading (md1)",
      len(dup11_1) == 1 and dup11_1[0][1] ==
      "SRAM Design and Utility Levels CAD Layer Table", repr(dup11_1))

dup11_2 = da._scan_heading_dup(md2.split("\n"))
check("C11b doc_audit no false positive on different titles (md2)", dup11_2 == [])

dup11_3 = da._scan_heading_dup(md3.split("\n"))
check("C11c doc_audit no detect when content intervenes (md3)", dup11_3 == [])

dup11_4 = da._scan_heading_dup(md4.split("\n"))
check("C11d doc_audit ignores fenced pseudo-headings (md4)", dup11_4 == [])

dup11_5 = da._scan_heading_dup(md5.split("\n"))
check("C11e doc_audit no detect when page marker intervenes (md5)", dup11_5 == [])

dup11_9 = da._scan_heading_dup(md9.split("\n"))
check("C11f doc_audit SSoT: numbered chapter/section (different numbers) → no detect",
      dup11_9 == [], repr(dup11_9))

dup11_9b = da._scan_heading_dup(md9b.split("\n"))
check("C11g doc_audit detects rule(b) exact-title-incl-number duplicate (md9b)",
      len(dup11_9b) == 1 and dup11_9b[0][1] ==
      "3.2.3 SRAM Design and Utility Levels CAD Layer Table", repr(dup11_9b))

dup11_7 = da._scan_heading_dup(md7.split("\n"))
check("C11h doc_audit chain of 3 → 2 duplicates detected (2nd/3rd)",
      len(dup11_7) == 2, repr(dup11_7))

# _page_of_line: 라인 인덱스 → 소속 페이지(마커 map 기반) 매핑 검증.
mk_probe = {1: 0, 2: 5, 3: 12}
check("C11i _page_of_line before first marker -> None",
      da._page_of_line(mk_probe, -1) is None)
check("C11j _page_of_line on/after marker -> owning page",
      da._page_of_line(mk_probe, 0) == 1
      and da._page_of_line(mk_probe, 4) == 1
      and da._page_of_line(mk_probe, 5) == 2
      and da._page_of_line(mk_probe, 20) == 3)

# ── Case 12(R13c): doc_audit.py 전체 실행 — heading_dup WARN 채널 스모크 ──
#    ①합성 중복헤딩 MD(가짜 페이지마커 포함, PDF 없이 함수 직접 호출로 대체는
#      이미 C11 커버) ②확정본(DM_p0018-0047)은 이미 R13b 로 정리된 산출물이므로
#      heading_dup 0·기존 status CLEAN 유지가 기대값(회귀 가드).
import json
import subprocess
import sys as _sys
from pathlib import Path as _Path

BASE_DIR = _Path(__file__).parent
md_confirmed = BASE_DIR / "output" / "pdf_md" / "DM_p0018-0047.md"
pdf_confirmed = BASE_DIR / "input" / "pdf" / "DM_p0018-0047.pdf"
if md_confirmed.exists() and pdf_confirmed.exists():
    r = subprocess.run(
        [_sys.executable, str(BASE_DIR / "doc_audit.py"),
         str(md_confirmed), str(pdf_confirmed)],
        capture_output=True, text=True)
    out = r.stdout
    out = out[out.index("{"):] if "{" in out else "{}"
    d = json.loads(out)
    check("C12a confirmed doc doc_audit exit code unchanged (0=CLEAN)",
          r.returncode == 0, f"returncode={r.returncode}")
    check("C12b confirmed doc status still CLEAN (heading_dup WARN is non-blocking)",
          d.get("status") == "CLEAN", repr(d.get("status")))
    check("C12c confirmed doc has 0 heading_dup warns (already R13b-clean)",
          d.get("warn_summary", {}).get("heading_dup", 0) == 0,
          repr(d.get("warn_summary")))
else:
    check("C12 confirmed doc fixtures present (skipped if absent)", True,
          "skipped: md/pdf fixture missing")

print("\n=== SUMMARY ===")
passed = sum(1 for _, c in results if c)
print(f"{passed}/{len(results)} checks passed")
raise SystemExit(0 if passed == len(results) else 1)
