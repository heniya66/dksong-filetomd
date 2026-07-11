#!/usr/bin/env python3
"""run_net_tracer.py — net_tracer 서브프로세스 격리 러너.

filestomdwgem 메인 프로세스가 pdf-to-kicad 스킬 코드를 직접 import 하지 않도록
**프로세스 경계**를 두는 얇은 러너다. 이 파일만 스킬에 의존하며, filestomdwgem
메인(`lib/net_crosscheck.py`)은 이 러너의 **stdout JSON** 만 소비한다.

[STRICT] pdf-to-kicad 스킬 코드는 read-only — import/실행만, 절대 수정 금지.

사용법:
    python run_net_tracer.py <pdf_path> <page>          # 단일 페이지(1-based, 기존)
    python run_net_tracer.py <pdf_path> <start> <end>   # 페이지 범위(1-based, inclusive)

출력(stdout, JSON):
    - 단일 페이지: 성공/실패 JSON **1줄**(기존 계약 — 소비자는 마지막 줄만 파싱).
    - 페이지 범위: 범위의 **각 페이지마다 JSON 1줄**(start..end 순서, 페이지당 1줄).
      이렇게 하면 PdfVectorParser 를 **1회만 오픈**하고도 페이지별 결과를 모두 낸다
      (M-7: 청크 경로의 페이지별 서브프로세스 재실행 → 단일 호출로 축약).
    성공 라인: {"ok": true, "page": N, "nets": [...], "no_connects": [...],
                "junctions": [...], "stats": {"lines": L, "texts": T, "named_nets": K}}
    실패 라인: {"ok": false, "page": N, "reason": "..."}  (비크래시 — degrade 신호)

stderr 에는 디버그/traceback 만 (stdout JSON 오염 방지).

자료구조 어댑팅(핵심):
    PdfVectorParser.extract_lines(page) -> [(x0, y0, x1, y1), ...]   (튜플)
    PdfVectorParser.extract_texts(page) -> [(text, x, y, font_size), ...]
    그러나 NetTracer 는 dict 형태를 요구한다:
        vector_raw = {"lines": [{"x0","y0","x1","y1"}, ...],
                      "texts": [{"text","x","y"}, ...]}
    이 러너가 그 어댑팅(tuple -> dict)을 수행한다.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

# pdf-to-kicad 스킬 scripts 경로 (read-only 의존). 이 러너 내부에서만 sys.path 에
# 추가하여 import 경계를 격리한다(메인 프로세스 import 오염 회피).
_SKILL_SCRIPTS = os.path.expanduser(
    "~/.claude/skills/pdf-to-kicad/scripts"
)


def _emit(obj: dict) -> None:
    """JSON 한 줄을 stdout 으로 출력(소비자 = net_crosscheck)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _build_vector_raw(parser, page_idx: int) -> dict:
    """PdfVectorParser 튜플 출력 -> NetTracer dict 입력 어댑팅.

    page_idx 는 0-based(파서 규약).
    """
    lines_raw = parser.extract_lines(page_idx)
    texts_raw = parser.extract_texts(page_idx)

    lines = [
        {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)}
        for (x0, y0, x1, y1) in lines_raw
    ]
    texts = [
        {"text": str(text), "x": float(x), "y": float(y)}
        for (text, x, y, *_rest) in texts_raw
    ]
    return {"lines": lines, "texts": texts}


def _trace_one_page(parser, NetTracer, page_1based: int) -> dict:
    """한 페이지를 net_tracer 로 추적 → 결과 dict(_emit 용). 비크래시.

    parser 는 **이미 오픈된** PdfVectorParser(범위 호출 시 1회 오픈 재사용 — M-7).
    page 범위 초과/래스터/추적 실패는 모두 {"ok": False, "page": N, "reason": ...}.
    """
    page_idx = page_1based - 1  # 1-based -> 0-based(파서 규약)
    if page_idx < 0 or page_idx >= parser.page_count:
        return {
            "ok": False,
            "page": page_1based,
            "reason": (
                f"page {page_1based} out of range (page_count={parser.page_count})"
            ),
        }

    vector_raw = _build_vector_raw(parser, page_idx)

    # 래스터/스캔 PDF: 벡터 line 없음 -> net_tracer 무력. degrade 신호.
    if not vector_raw["lines"]:
        return {
            "ok": False,
            "page": page_1based,
            "reason": "no vector lines on page (raster/scanned?) — net_tracer inert",
            "stats": {"lines": 0, "texts": len(vector_raw["texts"])},
        }

    try:
        # trace_and_merge: OCR 중복 net normalize/병합 + 핀이름 추론 포함.
        result = NetTracer(vector_raw).trace_and_merge()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return {
            "ok": False,
            "page": page_1based,
            "reason": f"trace failed: {type(exc).__name__}: {exc}",
        }

    nets = result.get("nets", [])
    named = [n for n in nets if not str(n.get("name", "")).startswith("Net_")]
    return {
        "ok": True,
        "page": page_1based,
        "nets": nets,
        "no_connects": result.get("no_connects", []),
        "junctions": result.get("junctions", []),
        "stats": {
            "lines": len(vector_raw["lines"]),
            "texts": len(vector_raw["texts"]),
            "total_nets": len(nets),
            "named_nets": len(named),
        },
    }


def _parse_args(argv: list[str]):
    """argv → (pdf_path, start_1based, end_1based) 또는 (None, reason).

    - 3개 토큰(`<pdf> <page>`): 단일 페이지(start==end). 기존 계약.
    - 4개 토큰(`<pdf> <start> <end>`): 페이지 범위(start<=end).
    """
    if len(argv) == 3:
        pdf_path, page_arg = argv[1], argv[2]
        try:
            page = int(page_arg)
        except ValueError:
            return None, f"page not an int: {page_arg!r}"
        if page < 1:
            return None, f"page must be >= 1, got {page}"
        return (pdf_path, page, page), None
    if len(argv) == 4:
        pdf_path, start_arg, end_arg = argv[1], argv[2], argv[3]
        try:
            start = int(start_arg)
            end = int(end_arg)
        except ValueError:
            return None, f"start/end not ints: {start_arg!r} {end_arg!r}"
        if start < 1 or end < 1:
            return None, f"start/end must be >= 1, got {start}/{end}"
        if end < start:
            return None, f"end < start ({end} < {start})"
        return (pdf_path, start, end), None
    return None, (
        "usage: run_net_tracer.py <pdf_path> <page(1-based)> "
        "| <pdf_path> <start(1-based)> <end(1-based)>"
    )


def main(argv: list[str]) -> int:
    parsed, reason = _parse_args(argv)
    if parsed is None:
        _emit({"ok": False, "reason": reason})
        return 0  # 비크래시: degrade 신호로만 처리
    pdf_path, start_1based, end_1based = parsed

    if not os.path.isfile(pdf_path):
        # 범위여도 단일 실패 라인으로 충분(소비자는 페이지별 매핑에서 누락 처리).
        _emit({"ok": False, "page": start_1based, "reason": f"pdf not found: {pdf_path}"})
        return 0

    # 스킬 코드 import — 이 러너 내부 경계에서만.
    if _SKILL_SCRIPTS not in sys.path:
        sys.path.insert(0, _SKILL_SCRIPTS)
    try:
        from pdf_vector_parser import PdfVectorParser  # type: ignore
        from net_tracer import NetTracer  # type: ignore
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _emit({"ok": False, "page": start_1based,
               "reason": f"import failed: {type(exc).__name__}: {exc}"})
        return 0

    parser = None
    try:
        # M-7: 파서를 **1회만** 오픈하여 범위의 모든 페이지를 처리(PDF 재파싱 제거).
        parser = PdfVectorParser(pdf_path)
        for page in range(start_1based, end_1based + 1):
            _emit(_trace_one_page(parser, NetTracer, page))
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _emit({"ok": False, "page": start_1based,
               "reason": f"trace failed: {type(exc).__name__}: {exc}"})
        return 0
    finally:
        try:
            if parser is not None:
                parser.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))
