#!/usr/bin/env python3
"""qa_scan.py — fmdw 변환 산출물 결정론 QA 스캐너 (Phase D', 2026-07-14 신규 파일).

Goose 변환-QA 오케스트레이터의 DETECT 단계 전용. LLM 호출 0 — 순수 결정론.
설계서(goose-fmdw-convert-qa-orchestrator-260713.md §2 [2]) 스캔 항목 구현.

사용:
    .venv/bin/python qa_scan.py <output_dir> [--log <convert_log>]

출력: stdout에 단일 JSON.
exit code: 0 = 클린, 1 = 결함 검출(비차단), 2 = 사용 오류.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HTML_VIOLATION_RE = re.compile(r"fmdw:html-table-violation(?:\s+p(\d+))?")
COVERAGE_LOW_RE = re.compile(r"fmdw:coverage-low(?:\s+pages?\s+([0-9-]+))?")
CAPTION_RE = re.compile(r"\*\*(Table|Figure)\s+(\d+)")
CONTINUED_RE = re.compile(r"continued|이어짐|계속", re.IGNORECASE)
UNREADABLE = "[판독 불가]"
IMG_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
NON_OVERSIZED_OK_KINDS = {"oversized_table", "diagonal_table"}
REPORT_BASENAME_PREFIX = "goose_qa_report"


def scan_md(md_path: Path):
    """단일 MD의 결정론 마커 스캔."""
    out = {
        "html_table_violations": [],
        "coverage_low": [],
        "missing_markers": [],
        "captions": [],           # (caption, line) — continued 제외
        "unreadable_count": 0,
        "image_links": [],
    }
    for lineno, line in enumerate(md_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = HTML_VIOLATION_RE.search(line)
        if m:
            out["html_table_violations"].append(
                {"file": md_path.name, "line": lineno, "page": int(m.group(1)) if m.group(1) else None})
        m = COVERAGE_LOW_RE.search(line)
        if m:
            out["coverage_low"].append(
                {"file": md_path.name, "line": lineno, "pages": m.group(1)})
        if "MISSING" in line:
            out["missing_markers"].append({"file": md_path.name, "line": lineno})
        if not CONTINUED_RE.search(line):
            for cm in CAPTION_RE.finditer(line):
                out["captions"].append((f"{cm.group(1)} {cm.group(2)}", lineno))
        out["unreadable_count"] += line.count(UNREADABLE)
        for im in IMG_LINK_RE.finditer(line):
            out["image_links"].append(im.group(1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("output_dir")
    ap.add_argument("--log", default=None, help="변환 로그 경로([ALL DONE] 확인용)")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    if not out_dir.is_dir():
        print(json.dumps({"error": f"output_dir not found: {out_dir}"}, ensure_ascii=False))
        return 2

    md_files = sorted(p for p in out_dir.glob("*.md")
                      if not p.name.startswith(REPORT_BASENAME_PREFIX)
                      and not p.name.endswith(".partial.md"))
    partial_files = sorted(p.name for p in out_dir.glob("*.partial.md"))

    result = {
        "output_dir": str(out_dir),
        "scanned_md": [p.name for p in md_files],
        "html_table_violations": [],
        "coverage_low": [],
        "partial_md": partial_files,
        "missing_markers": [],
        "duplicate_captions": [],
        "broken_image_refs": [],
        "unreadable_markers": 0,
        "qa_json": {"present": False},
        "figures_json": {"present": False, "table_image_violations": []},
        "incomplete_conversion": False,
        "incomplete_reasons": [],
    }

    caption_seen = {}
    for md in md_files:
        s = scan_md(md)
        result["html_table_violations"] += s["html_table_violations"]
        result["coverage_low"] += s["coverage_low"]
        result["missing_markers"] += s["missing_markers"]
        result["unreadable_markers"] += s["unreadable_count"]
        for cap, line in s["captions"]:
            caption_seen.setdefault(cap, []).append({"file": md.name, "line": line})
        for link in s["image_links"]:
            if not (out_dir / link).exists() and not (out_dir / Path(link).name).exists():
                result["broken_image_refs"].append({"file": md.name, "link": link})

        stem = md.stem
        # 사이드카 존재 검사 (stem 단위)
        if not (out_dir / f"{stem}_qa.json").exists():
            result["incomplete_reasons"].append(f"{stem}_qa.json 없음")
        if not (out_dir / f"{stem}_figures.json").exists():
            result["incomplete_reasons"].append(f"{stem}_figures.json 없음")

    result["duplicate_captions"] = [
        {"caption": cap, "count": len(locs), "locations": locs}
        for cap, locs in sorted(caption_seen.items()) if len(locs) >= 2
    ]

    # ⑤ qa.json 파싱
    for qa_path in sorted(out_dir.glob("*_qa.json")):
        try:
            data = json.loads(qa_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            result["qa_json"] = {"present": True, "path": qa_path.name, "parse_error": str(e)}
            continue
        result["qa_json"] = {
            "present": True,
            "path": qa_path.name,
            "low_confidence_model": data.get("low_confidence_model"),
            "requires_human_vision_qa": bool(data.get("requires_human_vision_qa")),
            "html_table_violation_pages": [v.get("page") for v in data.get("html_table_violations", [])],
            "reason": data.get("reason"),
        }

    # ⑥ figures.json — 일반표 이미지화 위반 (type==table 且 kind 비-허용)
    for fig_path in sorted(out_dir.glob("*_figures.json")):
        result["figures_json"]["present"] = True
        result["figures_json"]["path"] = fig_path.name
        try:
            figs = json.loads(fig_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            result["figures_json"]["parse_error"] = str(e)
            continue
        for f in figs if isinstance(figs, list) else []:
            if f.get("type") == "table" and f.get("kind") not in NON_OVERSIZED_OK_KINDS:
                result["figures_json"]["table_image_violations"].append(f.get("figure_id") or f.get("caption"))

    # ⑦ 변환 완결성
    if args.log:
        log_path = Path(args.log)
        if not log_path.exists():
            result["incomplete_reasons"].append(f"변환 로그 없음: {log_path.name}")
        elif "ALL DONE" not in log_path.read_text(encoding="utf-8", errors="replace"):
            result["incomplete_reasons"].append("[ALL DONE] 마커 없음")
    if not md_files:
        result["incomplete_reasons"].append("변환 MD 없음")
    result["incomplete_conversion"] = bool(result["incomplete_reasons"])

    # 요약·판정
    counts = {
        "html_table_violations": len(result["html_table_violations"]),
        "coverage_low": len(result["coverage_low"]),
        "partial_md": len(result["partial_md"]),
        "missing_markers": len(result["missing_markers"]),
        "duplicate_captions": len(result["duplicate_captions"]),
        "broken_image_refs": len(result["broken_image_refs"]),
        "table_image_violations": len(result["figures_json"]["table_image_violations"]),
        "unreadable_markers": result["unreadable_markers"],
    }
    result["summary"] = counts
    result["human_gate_required"] = bool(
        result["qa_json"].get("requires_human_vision_qa") or result["unreadable_markers"] > 0
    )
    defects = sum(counts.values()) > 0 or result["incomplete_conversion"]
    result["verdict"] = "defects_found" if defects else "clean"

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
