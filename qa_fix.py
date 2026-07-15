#!/usr/bin/env python3
"""qa_fix.py — 값 불변(value-preserving) 서식 자동수정 도구 (#6, 2026-07-15 신규).

qa_scan.py 가 검출하는 AUTO-FIX 후보(중복 캡션·불릿 정규화·깨진 이미지 링크)를
결정론으로 수정한다. LLM 호출 0. 표 셀 값·본문 텍스트는 절대 불변 — 서식/링크만.

수정 항목:
  F1 duplicate captions : 동일 정규화 텍스트의 비-continued 볼드 캡션 중복 → 첫
     출현만 보존, 이후 정확 일치 라인만 제거(텍스트가 다르면 보존 — 사람 몫).
  F2 bullet normalize   : 리터럴 '•' 불릿 → '- ', '–'(하위) → '  - '
     (fmdw.md FMDW_BULLET_LIST 표준과 동일 규칙. 코드펜스·표 행 제외).
  F3 broken image refs  : 링크 대상 부재 시, out_dir 에 동일 basename 이 유일하게
     실재하면 그 상대경로로 교정(figures/ 우선). 모호/부재 = 미수정(사람 몫).

사용:
  .venv/bin/python qa_fix.py <output_dir> [--md <name.md>] [--pdf <pdf>] [--apply]
기본 dry-run: 제안만 JSON 출력. --apply 시 <md>.bak-qafix 백업(최초 1회 보존) 후
적용하고, --pdf(미지정 시 input/pdf/<stem>.pdf 추정)가 실재하면 doc_audit.py 를
회귀가드로 실행 — FAIL 시 백업 복원 + exit 2.
exit: 0=수정 대상 없음(또는 apply 성공+audit CLEAN), 1=제안 존재(dry-run)/적용됨,
      2=오류 또는 audit 회귀(복원됨).
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
CAPTION_LINE_RE = re.compile(r"^\s*\*\*(Table|Figure)\s+(\d+)\s*[:.].*\*\*\s*$")
CONTINUED_RE = re.compile(r"continued|이어짐|계속", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
IMG_LINK_RE = re.compile(r"(!\[[^\]]*\]\()([^)]+)(\))")
BULLET_RE = re.compile(r"^(\s*)•\s*(\S.*)$")
SUBBULLET_RE = re.compile(r"^(\s*)–\s+(\S.*)$")


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def plan_fixes(md_path: Path, out_dir: Path):
    lines = md_path.read_text(encoding="utf-8").split("\n")
    fixes = []          # {kind, line(1-based), before, after|None(=삭제)}
    in_fence = False
    seen_caps = {}
    for i, l in enumerate(lines):
        if FENCE_RE.match(l):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # F1: 중복 캡션(정확 일치·비-continued)
        cm = CAPTION_LINE_RE.match(l)
        if cm and not CONTINUED_RE.search(l):
            key = norm(l)
            if key in seen_caps:
                fixes.append({"kind": "duplicate_caption", "line": i + 1,
                              "before": l.strip()[:100], "after": None,
                              "first_at_line": seen_caps[key]})
            else:
                seen_caps[key] = i + 1
            continue
        if l.lstrip().startswith("|"):
            continue                      # 표 행 무변경
        # F2: 불릿 정규화
        bm = BULLET_RE.match(l)
        if bm:
            fixes.append({"kind": "bullet", "line": i + 1, "before": l[:100],
                          "after": bm.group(1) + "- " + bm.group(2)})
            continue
        sm = SUBBULLET_RE.match(l)
        if sm:
            fixes.append({"kind": "bullet_sub", "line": i + 1, "before": l[:100],
                          "after": sm.group(1) + "  - " + sm.group(2)})
            continue
        # F3: 깨진 이미지 링크
        for im in IMG_LINK_RE.finditer(l):
            link = im.group(2)
            # qa_scan 과 동일한 '깨짐' 판정: 링크 경로도, basename 도 실재하지 않음
            if (out_dir / link).exists() or (out_dir / Path(link).name).exists():
                continue
            base = Path(link).name
            cands = []
            if (out_dir / "figures" / base).exists():
                cands.append("figures/" + base)
            if (out_dir / base).exists():
                cands.append(base)
            if len(cands) == 1 and cands[0] != link:
                fixes.append({"kind": "broken_image_ref", "line": i + 1,
                              "before": link, "after_link": cands[0],
                              "after": l.replace("(" + link + ")",
                                                 "(" + cands[0] + ")")[:120]})
            elif not (out_dir / link).exists():
                fixes.append({"kind": "broken_image_ref_unfixable", "line": i + 1,
                              "before": link, "after": None,
                              "note": "후보 %d개 — 사람 확인" % len(cands)})
    return lines, fixes


def apply_fixes(lines, fixes):
    drop = set()
    for f in fixes:
        i = f["line"] - 1
        if f["kind"] == "duplicate_caption":
            drop.add(i)
            # 캡션 직후의 빈 줄도 함께 제거(빈 줄 중복 방지)
            if i + 1 < len(lines) and not lines[i + 1].strip():
                drop.add(i + 1)
        elif f["kind"] in ("bullet", "bullet_sub"):
            lines[i] = f["after"]
        elif f["kind"] == "broken_image_ref":
            lines[i] = lines[i].replace("(" + f["before"] + ")",
                                        "(" + f["after_link"] + ")")
    return [l for k, l in enumerate(lines) if k not in drop]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("output_dir")
    ap.add_argument("--md", default=None, help="대상 MD 파일명(기본: 디렉토리 내 전체)")
    ap.add_argument("--pdf", default=None, help="doc_audit 회귀가드용 원본 PDF")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    out_dir = Path(args.output_dir)
    if not out_dir.is_dir():
        print(json.dumps({"error": "output_dir not found: %s" % out_dir}))
        return 2
    mds = [out_dir / args.md] if args.md else sorted(
        p for p in out_dir.glob("*.md")
        if not p.name.startswith("goose_qa_report"))
    report = {"mode": "apply" if args.apply else "dry-run", "files": [],
              "total_fixes": 0}
    rc = 0
    for md in mds:
        if not md.exists():
            print(json.dumps({"error": "md not found: %s" % md}))
            return 2
        lines, fixes = plan_fixes(md, out_dir)
        applicable = [f for f in fixes if f["kind"] != "broken_image_ref_unfixable"]
        entry = {"md": md.name, "fixes": fixes, "applicable": len(applicable)}
        report["total_fixes"] += len(applicable)
        if args.apply and applicable:
            bak = md.with_suffix(md.suffix + ".bak-qafix")
            if not bak.exists():
                shutil.copy2(md, bak)
            new_lines = apply_fixes(list(lines), applicable)
            md.write_text("\n".join(new_lines), encoding="utf-8")
            entry["applied"] = len(applicable)
            # ── doc_audit 회귀가드(PDF 실재 시) ──
            pdf = Path(args.pdf) if args.pdf else \
                BASE / "input" / "pdf" / (md.stem + ".pdf")
            if pdf.exists():
                r = subprocess.run(
                    [sys.executable, str(BASE / "doc_audit.py"), str(md), str(pdf)],
                    capture_output=True, text=True)
                entry["audit_after"] = "CLEAN" if r.returncode == 0 else "FAIL"
                if r.returncode != 0:
                    shutil.copy2(bak, md)
                    entry["audit_after"] += "→REVERTED(백업 복원)"
                    rc = 2
            else:
                entry["audit_after"] = "SKIPPED(pdf 없음: %s)" % pdf
        report["files"].append(entry)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if rc:
        return rc
    # dry-run: 제안 존재 = 1(비차단 신호). apply: 정상 적용 완료 = 0.
    return 1 if (report["total_fixes"] and not args.apply) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))
        sys.exit(2)
