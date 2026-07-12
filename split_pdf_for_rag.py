#!/usr/bin/env python3
"""split_pdf_for_rag.py — 대용량 PDF를 RAG(Retrieval-Augmented Generation) 친화적으로
섹션/표·도면 경계에 스냅하여 분할한다 (fmdw 파이프라인 정식 분할 스크립트, 2026-07-12).

임시 '목차 level-1 챕터 경계 + 30p 하드컷' 로직의 정식화 + 개선:
  - 목차(TOC) level 1~3(챕터·섹션·하위섹션) 시작 페이지를 컷 후보 경계로 수집.
  - 목표 target(기본 30) 페이지 근처에서 [min,max] 범위 내 '가장 가까운 섹션 경계'로 스냅.
  - 범위 내 섹션 경계가 없으면 target 로 하드컷하되, 표(PyMuPDF find_tables)·도면
    (fmdw.figure_extractor bbox)이 페이지 경계를 가로지르면 그 객체가 끝나는 페이지까지
    컷을 미뤄 표/도면 절단을 회피한다(유연 상한 내).
  - TOC 없으면 target + 표/도면 경계 스냅만.

결정론: 동일 입력 → 동일 컷(정렬·안정 tie-break). GPU/모델 추론 불필요 — PyMuPDF 벡터/
좌표만 사용(로컬 100%). 사이드카 `{stem}_split_manifest.json` 에 세그먼트별 챕터/섹션/
컷 사유를 기록해 파편의 문서 내 소속을 추적한다.

사용:
  python split_pdf_for_rag.py INPUT.pdf [--target 30] [--min 20] [--max 45]
      [--outdir DIR] [--stem NAME] [--dry-run] [--no-straddle-check]

--dry-run 은 컷 지점(세그먼트 표)만 계산·출력하고 실제 분할 PDF/사이드카를 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import fitz  # PyMuPDF

# fmdw.figure_extractor(도면 bbox 재사용) — import 경로 보장(스크립트 위치 = fmdw 루트).
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from fmdw import figure_extractor as _fx  # type: ignore
except Exception:  # noqa: BLE001
    _fx = None


# ── TOC(목차) 경계 수집 ──────────────────────────────────────────────────────────
def toc_boundaries(doc, max_level: int = 3) -> dict:
    """TOC level 1~max_level 의 {시작페이지(1-based) -> (level, title)}.

    한 페이지에 복수 항목이 시작하면 '가장 상위(작은 level)'를 유지(챕터 우선)."""
    out: dict = {}
    try:
        toc = doc.get_toc(simple=True) or []
    except Exception:  # noqa: BLE001
        toc = []
    for entry in toc:
        try:
            lvl, title, page = int(entry[0]), str(entry[1] or "").strip(), int(entry[2])
        except Exception:  # noqa: BLE001
            continue
        if page < 1 or lvl < 1 or lvl > max_level:
            continue
        if page not in out or lvl < out[page][0]:
            out[page] = (lvl, title)
    return out


def _label_for(bmap: dict, start: int) -> str:
    """세그먼트 시작 페이지의 섹션 라벨 = start 이하 가장 가까운 TOC 경계 제목."""
    best_pg = None
    for pg in bmap:
        if pg <= start and (best_pg is None or pg > best_pg):
            best_pg = pg
    if best_pg is None:
        return ""
    lvl, title = bmap[best_pg]
    return f"L{lvl}:{title}" if title else f"L{lvl}"


# ── 표/도면 경계(straddle) 검출 ──────────────────────────────────────────────────
# 하단 닿음 임계(페이지 높이 대비): 표/도면이 본문 하단부까지 뻗으면 '잘림' 신호. 데이터시트는
# 푸터 여백이 있어 0.85 로 완화. 상단 닿음 0.15(헤더 여백). 오탐 0 을 위해 '양쪽 동시'만.
_TOUCH_BOTTOM_FRAC = 0.85
_TOUCH_TOP_FRAC = 0.15


# 전면 배경/워터마크 raster 커버율 상한 — 이 이상 덮는 raster 는 배경으로 보고 straddle
# 검출에서 제외(전-doc 워터마크 스캔 없이 페이지-로컬로 결정적 필터, 성능·오탐 동시 해결).
_BG_RASTER_COVER = 0.55


def _page_object_boxes(page) -> list:
    """페이지의 표(find_tables) + 도면(raster/vector) bbox[x0,y0,x1,y1] 목록(PDF pt).

    전면 배경 raster(커버율 ≥ _BG_RASTER_COVER)는 제외해 straddle 오탐을 막는다.
    페이지-로컬 판정이라 문서 전체 스캔이 필요 없다(대용량 PDF 성능 보존)."""
    boxes: list = []
    pw, ph = page.rect.width, page.rect.height
    page_area = pw * ph if (pw > 0 and ph > 0) else 0.0
    try:
        tf = page.find_tables()
        for t in tf.tables:
            bb = list(t.bbox)
            if len(bb) == 4:
                boxes.append([float(x) for x in bb])
    except Exception:  # noqa: BLE001
        pass
    if _fx is not None:
        try:
            for b in _fx.raster_figure_rects(page):
                bb = list(b)
                cover = ((bb[2] - bb[0]) * (bb[3] - bb[1]) / page_area) if page_area else 0.0
                if cover < _BG_RASTER_COVER:      # 전면 배경 raster 제외
                    boxes.append(bb)
        except Exception:  # noqa: BLE001
            pass
        try:
            boxes += [list(b) for b in _fx.vector_figure_clusters(page)]
        except Exception:  # noqa: BLE001
            pass
    return boxes


def _touches_bottom(page) -> bool:
    ph = page.rect.height
    if ph <= 0:
        return False
    thr = ph * _TOUCH_BOTTOM_FRAC
    return any(b[3] >= thr for b in _page_object_boxes(page))


def _touches_top(page) -> bool:
    ph = page.rect.height
    if ph <= 0:
        return False
    thr = ph * _TOUCH_TOP_FRAC
    return any(b[1] <= thr for b in _page_object_boxes(page))


def _boundary_clean(doc, end: int) -> bool:
    """페이지 `end`(1-based)와 end+1 사이 컷이 표/도면을 가로지르지 않으면 True.

    보수적 판정: 이전 페이지에 하단까지 닿는 객체 + 다음 페이지에 상단부터 시작하는
    객체가 '동시에' 있을 때만 straddle(절단)로 간주(오탐 0 우선)."""
    if end >= doc.page_count:
        return True
    prev = doc[end - 1]   # 0-based index
    nxt = doc[end]        # page (end+1) → index end
    return not (_touches_bottom(prev) and _touches_top(nxt))


def _snap_away_from_straddle(doc, end: int, start: int, total: int,
                             max_p: int, slack: int = 12):
    """컷(end↔end+1)이 표/도면을 가로지르면, straddle 이 없어질 때까지 end 를 뒤로 민다.

    유연 상한: start+max_p+slack 페이지까지 허용. 그 안에서 clean 경계를 못 찾으면
    원래 end 로 되돌린다(무한 확장 방지). 반환 (new_end, snapped)."""
    ceiling = min(total, start + max_p - 1 + slack)
    e = end
    moved = False
    while e < ceiling and not _boundary_clean(doc, e):
        e += 1
        moved = True
    if not _boundary_clean(doc, e):
        return end, False   # slack 내 clean 실패 → 원래 컷 유지
    return e, moved


# ── 컷 계산 ──────────────────────────────────────────────────────────────────────
def compute_cuts(doc, target: int, min_p: int, max_p: int,
                 straddle_check: bool = True):
    """세그먼트 목록 [(start, end, reason, label)] 계산(1-based, 포함 범위)."""
    total = doc.page_count
    bmap = toc_boundaries(doc)
    # 컷 후보 = level1~3 섹션 '시작 페이지'(=다음 세그먼트 start). page1 은 항상 첫 시작.
    boundaries = sorted(p for p in bmap if 1 < p <= total)

    segments: list = []
    start = 1
    guard = 0
    while start <= total:
        guard += 1
        if guard > total + 5:      # 안전장치(무한 루프 방지)
            segments.append((start, total, "guard-final", _label_for(bmap, start)))
            break
        # 남은 페이지가 상한 이하면 마지막 세그먼트로 흡수.
        if total - start + 1 <= max_p:
            segments.append((start, total, "final", _label_for(bmap, start)))
            break

        # 허용 END 범위 [start+min-1, start+max-1] → next-start b ∈ [start+min, start+max]
        cands = [b for b in boundaries if start + min_p <= b <= start + max_p]
        ideal = start + target      # 이상적 next-start
        end = None
        reason = None
        if cands:
            # 이상 next-start 에 가장 가까운 섹션 경계로 스냅(안정 tie-break=작은 페이지).
            #   TOC level1~3 섹션 시작은 새 헤딩(표/도면 중간이 아님)이라 본질적으로 clean 컷 →
            #   객체 straddle 검사 불필요(대용량 문서 성능 보존). straddle 검사는 하드컷 폴백만.
            b = min(cands, key=lambda x: (abs(x - ideal), x))
            end, reason = b - 1, "section"
        if end is None:
            # 범위 내 섹션 경계 없음 → target 하드컷 + 표/도면 경계 스냅.
            end = min(start + target - 1, total)
            reason = "hardcut"
            if straddle_check:
                new_end, snapped = _snap_away_from_straddle(
                    doc, end, start, total, max_p)
                if snapped:
                    end, reason = new_end, "objsnap"
        segments.append((start, end, reason, _label_for(bmap, start)))
        start = end + 1
    return segments, bmap


def _fmt_segments(stem: str, segments: list) -> list:
    return [
        {
            "file": f"{stem}_p{s:04d}-{e:04d}.pdf",
            "start_page": s,
            "end_page": e,
            "pages": e - s + 1,
            "cut_reason": reason,
            "section_label": label,
        }
        for (s, e, reason, label) in segments
    ]


def split_pdf(input_pdf: Path, outdir: Path, target: int, min_p: int, max_p: int,
              stem: str, dry_run: bool, straddle_check: bool) -> dict:
    doc = fitz.open(input_pdf)
    total = doc.page_count
    segments, bmap = compute_cuts(doc, target, min_p, max_p, straddle_check)
    records = _fmt_segments(stem, segments)
    manifest = {
        "source_pdf": input_pdf.name,
        "total_pages": total,
        "target": target, "min": min_p, "max": max_p,
        "toc_boundaries": sum(1 for _ in bmap),
        "straddle_check": straddle_check,
        "segments": records,
    }

    # 검증(무결성): 커버리지 연속·완전.
    covered = 0
    prev_end = 0
    for r in records:
        assert r["start_page"] == prev_end + 1, f"gap/overlap at {r}"
        prev_end = r["end_page"]
        covered += r["pages"]
    assert prev_end == total, f"last end {prev_end} != total {total}"
    assert covered == total, f"covered {covered} != total {total}"

    if not dry_run:
        outdir.mkdir(parents=True, exist_ok=True)
        for (s, e, _reason, _label) in segments:
            out = fitz.open()
            out.insert_pdf(doc, from_page=s - 1, to_page=e - 1)
            out.save(outdir / f"{stem}_p{s:04d}-{e:04d}.pdf")
            out.close()
        man_path = outdir / f"{stem}_split_manifest.json"
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        manifest["manifest_path"] = str(man_path)
    doc.close()
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="RAG-친화 PDF 섹션/표·도면 경계 스냅 분할")
    ap.add_argument("input_pdf", type=str)
    ap.add_argument("--target", type=int, default=int(os.getenv("SPLIT_TARGET", "30")))
    ap.add_argument("--min", dest="min_p", type=int,
                    default=int(os.getenv("SPLIT_MIN", "20")))
    ap.add_argument("--max", dest="max_p", type=int,
                    default=int(os.getenv("SPLIT_MAX", "45")))
    ap.add_argument("--outdir", type=str, default="")
    ap.add_argument("--stem", type=str, default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-straddle-check", action="store_true")
    args = ap.parse_args(argv)

    inp = Path(args.input_pdf)
    if not inp.exists():
        print(f"[!] not found: {inp}", file=sys.stderr)
        return 2
    stem = args.stem or inp.stem
    outdir = Path(args.outdir) if args.outdir else inp.parent / f"{stem}_split"

    manifest = split_pdf(inp, outdir, args.target, args.min_p, args.max_p,
                         stem, args.dry_run, not args.no_straddle_check)

    mode = "DRY-RUN" if args.dry_run else "WROTE"
    print(f"[{mode}] {inp.name}: {manifest['total_pages']}p → "
          f"{len(manifest['segments'])} segments "
          f"(TOC boundaries={manifest['toc_boundaries']}, target={args.target}, "
          f"range={args.min_p}-{args.max_p})")
    for r in manifest["segments"]:
        print(f"    {r['file']}  ({r['pages']:>3}p)  "
              f"[{r['cut_reason']:<16}] {r['section_label']}")
    if not args.dry_run:
        print(f"[+] manifest: {manifest.get('manifest_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
