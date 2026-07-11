"""수동 진단 도구 — PDF 페이지별 이미지 상세 정보 출력 (xref, w, h, colorspace, bpc).

자동 파이프라인(extract_all_via_pdf.py) 미연결. 수동 실행 전용.
타일 슬라이스 vs 실제 figure 판별, 배경 xref 번호 확인 등에 사용.

사용법:
    python3 scripts/inspect_images.py --pdf path/to/file.pdf [--label MY-LABEL]
"""
import argparse
import fitz
from pathlib import Path


def inspect_pdf(pdf_path: Path, label: str = "") -> None:
    """PDF 페이지별 이미지 상세 정보(xref/크기/colorspace/bpc) 출력."""
    doc = fitz.open(str(pdf_path))
    print(f"\n{'='*70}")
    print(f"[{label}] {pdf_path.name}")
    print(f"{'='*70}")
    for page_num in range(len(doc)):
        page = doc[page_num]
        images = page.get_images(full=True)
        if not images:
            continue
        seen: set[int] = set()
        valid = []
        for img in images:
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            w, h = img[2], img[3]
            cs = img[5]   # colorspace name
            bpc = img[7]  # bits per component
            valid.append((xref, w, h, cs, bpc))

        # 150x120 이상만
        big = [(x, w, h, cs, bpc) for x, w, h, cs, bpc in valid if w >= 150 and h >= 120]

        # 페이지 텍스트 길이
        txt_len = len(page.get_text().strip())

        print(f"\n--- Page {page_num+1} (txt_chars={txt_len}) ---")
        print(f"  전체 이미지: {len(valid)}개 / 150x120이상: {len(big)}개")
        for xref, w, h, cs, bpc in big:
            print(f"  xref={xref:5d}  {w:5d}x{h:5d}  cs={cs}  bpc={bpc}")

    doc.close()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "수동 진단 도구: PDF 페이지별 이미지 상세 정보 출력.\n"
            "자동 파이프라인 미연결 — 수동 실행 전용.\n\n"
            "예시:\n"
            "  python3 scripts/inspect_images.py --pdf input/pdf/MyDoc.pdf\n"
            "  python3 scripts/inspect_images.py --pdf input/pdf/EDA.pdf --label A-EDA"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--pdf", required=True, type=Path,
                   help="검사 대상 PDF 파일 경로")
    p.add_argument("--label", default="",
                   help="출력 헤더에 표시할 레이블 (예: A-EDA)")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if not args.pdf.exists():
        parser.error(f"PDF 파일을 찾을 수 없음: {args.pdf}")

    inspect_pdf(args.pdf, label=args.label)
