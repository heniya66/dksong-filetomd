"""수동 진단 도구 — 배경 타일 제외한 논리적 figure 수 + MD 블록 대사표 출력 (v2).

자동 파이프라인(extract_all_via_pdf.py) 미연결. 수동 실행 전용.

v1(analyze_figures.py) 대비 개선:
  - 동일 크기 이미지가 연속 xref로 2개 이상이면 타일 슬라이스로 판단 → 1개로 집계.
  - --bg-xrefs 로 알려진 배경 타일 xref 번호를 명시적으로 제외 가능.

사용법:
    python3 scripts/analyze_figures_v2.py \\
        --pdf path/to/file.pdf \\
        --md  path/to/file.md

    # 알려진 배경 타일 xref 제외 시:
    python3 scripts/analyze_figures_v2.py \\
        --pdf input/pdf/EDA.pdf \\
        --md  output/pdf_md/EDA.md \\
        --bg-xrefs 4 5 6 42 43 44 \\
        --label A-EDA
"""
import argparse
import fitz
import re
from collections import defaultdict
from pathlib import Path

MIN_W = 150
MIN_H = 120


def is_tile_slice_group(images_sorted: list[tuple]) -> set[int]:
    """동일 width/height를 가진 이미지가 연속 xref로 2개 이상이면 타일 슬라이스로 판단.

    Returns:
        타일로 판단된 xref 집합 (그룹 내 첫 번째 xref는 대표로 살려둠).
    """
    size_groups: dict = defaultdict(list)
    for xref, w, h in images_sorted:
        size_groups[(w, h)].append(xref)

    tile_xrefs: set[int] = set()
    for (w, h), xrefs in size_groups.items():
        if len(xrefs) >= 2:
            sorted_xrefs = sorted(xrefs)
            groups: list[list[int]] = []
            cur = [sorted_xrefs[0]]
            for x in sorted_xrefs[1:]:
                if x - cur[-1] <= 10:
                    cur.append(x)
                else:
                    groups.append(cur)
                    cur = [x]
            groups.append(cur)
            for g in groups:
                if len(g) >= 2:
                    for x in g[1:]:
                        tile_xrefs.add(x)
    return tile_xrefs


def count_logical_figures(
    page: fitz.Page,
    bg_xrefs: set[int] | None = None,
) -> tuple[int, list[tuple]]:
    """페이지의 논리적 figure 수 (배경타일/슬라이스 제외).

    Returns:
        (count, [(xref, w, h), ...]) 튜플.
    """
    images = page.get_images(full=True)
    seen: set[int] = set()
    valid: list[tuple] = []
    for img in images:
        xref = img[0]
        if xref in seen:
            continue
        seen.add(xref)
        w, h = img[2], img[3]
        if w >= MIN_W and h >= MIN_H:
            if bg_xrefs and xref in bg_xrefs:
                continue
            valid.append((xref, w, h))

    tile_xrefs = is_tile_slice_group(valid)
    logical = [(xref, w, h) for xref, w, h in valid if xref not in tile_xrefs]
    return len(logical), logical


def count_pdf_figures(
    pdf_path: Path,
    bg_xrefs: set[int] | None = None,
) -> tuple[dict[int, int], dict[int, list[tuple]]]:
    """페이지별 논리적 figure 수 + 상세 반환.

    Returns:
        ({page: count}, {page: [(xref, w, h), ...]}) 튜플.
    """
    doc = fitz.open(str(pdf_path))
    result: dict[int, int] = {}
    details: dict[int, list[tuple]] = {}
    for page_num in range(len(doc)):
        page = doc[page_num]
        count, imgs = count_logical_figures(page, bg_xrefs)
        result[page_num + 1] = count
        details[page_num + 1] = imgs
    doc.close()
    return result, details


def count_md_blocks(md_path: Path) -> dict[int, int]:
    """페이지별 이미지 분석 블록 수 반환 {page_num(1-based): count}."""
    text = Path(md_path).read_text(encoding='utf-8')
    sections = re.split(r'(## Page \d+)', text)
    result: dict[int, int] = {}
    current_page: int | None = None
    for part in sections:
        m = re.match(r'## Page (\d+)', part)
        if m:
            current_page = int(m.group(1))
            result[current_page] = 0
        elif current_page is not None:
            blocks = len(re.findall(r'>\s*\*\*\[이미지', part))
            result[current_page] = blocks
    return result


def print_table(
    pdf_path: Path,
    md_path: Path,
    bg_xrefs: set[int] | None = None,
    label: str = "",
) -> list[int]:
    """대사표 출력 후 누락/부족 의심 페이지 목록 반환."""
    pdf_figures, details = count_pdf_figures(pdf_path, bg_xrefs)
    md_blocks = count_md_blocks(md_path)

    all_pages = sorted(set(list(pdf_figures.keys()) + list(md_blocks.keys())))
    missing_pages: list[int] = []

    print(f"\n{'='*65}")
    print(f"[{label}] {Path(pdf_path).name}")
    print(f"{'='*65}")
    print(f"{'Page':>6} | {'원본 figure':>11} | {'MD 블록':>7} | {'판정':>8}")
    print(f"{'-'*6}-+-{'-'*11}-+-{'-'*7}-+-{'-'*8}")

    for p in all_pages:
        fig = pdf_figures.get(p, 0)
        blk = md_blocks.get(p, 0)
        if fig > 0 and blk == 0:
            verdict = "누락의심"
            missing_pages.append(p)
        elif fig > 0 and blk < fig:
            verdict = "부족의심"
            missing_pages.append(p)
        else:
            verdict = "OK"
        marker = ""
        if fig > 0:
            xrefs_str = ",".join(str(x) for x, w, h in details.get(p, []))
            marker = f"  [xref:{xrefs_str}]"
        print(f"{p:>6} | {fig:>11} | {blk:>7} | {verdict:>8}{marker}")

    print(f"\n총 논리 figure: {sum(pdf_figures.values())}")
    print(f"총 MD 블록: {sum(md_blocks.values())}")
    print(f"누락/부족 의심 페이지: {missing_pages}")
    return missing_pages


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "수동 진단 도구 (v2): 배경 타일 제외한 논리적 figure 수 + MD 블록 대사표.\n"
            "자동 파이프라인 미연결 — 수동 실행 전용.\n\n"
            "예시:\n"
            "  python3 scripts/analyze_figures_v2.py \\\n"
            "      --pdf input/pdf/EDA.pdf \\\n"
            "      --md  output/pdf_md/EDA.md \\\n"
            "      --bg-xrefs 4 5 6 42 43 44 --label A-EDA"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--pdf", required=True, type=Path,
                   help="분석 대상 PDF 파일 경로")
    p.add_argument("--md", required=True, type=Path,
                   help="대사할 Markdown 파일 경로")
    p.add_argument("--bg-xrefs", nargs="*", type=int, default=None,
                   metavar="XREF",
                   help="제외할 배경 타일 xref 번호 목록 (예: --bg-xrefs 4 5 6 42 43 44)")
    p.add_argument("--label", default="",
                   help="출력 표 헤더에 표시할 레이블 (예: A-EDA)")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if not args.pdf.exists():
        parser.error(f"PDF 파일을 찾을 수 없음: {args.pdf}")
    if not args.md.exists():
        parser.error(f"MD 파일을 찾을 수 없음: {args.md}")

    bg_xrefs = set(args.bg_xrefs) if args.bg_xrefs else None
    missing = print_table(args.pdf, args.md, bg_xrefs=bg_xrefs, label=args.label)
    print(f"\n=== 최종 요약 ===")
    print(f"누락/부족 의심 페이지: {missing}")
