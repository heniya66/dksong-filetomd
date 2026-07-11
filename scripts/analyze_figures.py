"""수동 진단 도구 — PDF 페이지별 raster 이미지 수와 MD 분석 블록 수 대사표 출력.

자동 파이프라인(extract_all_via_pdf.py) 미연결. 수동 실행 전용.

사용법:
    python3 scripts/analyze_figures.py \\
        --pdf path/to/file.pdf \\
        --md  path/to/file.md

출력: 페이지별 "원본 figure 수 vs MD 블록 수" 대사표 + 누락/부족 의심 페이지 목록.
"""
import argparse
import fitz
import re
from pathlib import Path

MIN_W = 150
MIN_H = 120


def count_pdf_figures(pdf_path: Path) -> dict[int, int]:
    """페이지별 의미있는 raster 이미지 수 반환 {page_num(1-based): count}."""
    doc = fitz.open(str(pdf_path))
    result: dict[int, int] = {}
    for page_num in range(len(doc)):
        page = doc[page_num]
        images = page.get_images(full=True)
        count = 0
        seen_xrefs: set[int] = set()
        for img in images:
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            w, h = img[2], img[3]
            if w >= MIN_W and h >= MIN_H:
                count += 1
        result[page_num + 1] = count  # 1-based
    doc.close()
    return result


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


def print_table(pdf_path: Path, md_path: Path) -> list[int]:
    """대사표 출력 후 누락/부족 의심 페이지 목록 반환."""
    pdf_figures = count_pdf_figures(pdf_path)
    md_blocks = count_md_blocks(md_path)

    all_pages = sorted(set(list(pdf_figures.keys()) + list(md_blocks.keys())))
    missing_pages: list[int] = []

    print(f"\n{'='*60}")
    print(f"파일: {Path(pdf_path).name}")
    print(f"{'='*60}")
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
        print(f"{p:>6} | {fig:>11} | {blk:>7} | {verdict:>8}")

    print(f"\n총 원본 figure: {sum(pdf_figures.values())}")
    print(f"총 MD 블록: {sum(md_blocks.values())}")
    print(f"누락/부족 의심 페이지: {missing_pages}")
    return missing_pages


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "수동 진단 도구: PDF figure 수와 MD 블록 수 대사표 출력.\n"
            "자동 파이프라인 미연결 — 수동 실행 전용.\n\n"
            "예시:\n"
            "  python3 scripts/analyze_figures.py \\\n"
            "      --pdf input/pdf/MyDoc.pdf \\\n"
            "      --md  output/pdf_md/MyDoc.md"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--pdf", required=True, type=Path,
                   help="분석 대상 PDF 파일 경로")
    p.add_argument("--md", required=True, type=Path,
                   help="대사할 Markdown 파일 경로")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if not args.pdf.exists():
        parser.error(f"PDF 파일을 찾을 수 없음: {args.pdf}")
    if not args.md.exists():
        parser.error(f"MD 파일을 찾을 수 없음: {args.md}")

    missing = print_table(args.pdf, args.md)
    print(f"\n=== 요약 ===")
    print(f"누락/부족 의심 페이지: {missing}")
