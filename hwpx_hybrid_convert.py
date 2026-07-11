"""
HWP/HWPX → Markdown 하이브리드 변환기 (CLI)

내부 처리는 fmdw.hybrid_extract.hybrid_convert() 에 위임.

사용법:
  python hwpx_hybrid_convert.py <input_file> <output_md_path> --source-rel "<rel>"
"""

import argparse
import sys
from pathlib import Path

# fmdw 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fmdw.hybrid_extract import hybrid_convert, SUPPORTED_EXTS


def main():
    parser = argparse.ArgumentParser(description="HWP/HWPX/DOCX/PPTX/XLSX → Markdown 하이브리드 변환기")
    parser.add_argument("input_file", help="변환할 파일 경로 (.hwp/.hwpx/.docx/.pptx/.xlsx)")
    parser.add_argument("output_md_path", help="출력 .md 파일 경로")
    parser.add_argument("--source-rel", default="", help="frontmatter source 상대 경로")
    args = parser.parse_args()

    input_path = Path(args.input_file).resolve()
    output_path = Path(args.output_md_path).resolve()

    if not input_path.exists():
        print(f"[ERROR] 파일 없음: {input_path}")
        sys.exit(1)

    if input_path.suffix.lower() not in SUPPORTED_EXTS:
        print(f"[ERROR] 미지원 포맷: {input_path.suffix} (지원: {SUPPORTED_EXTS})")
        sys.exit(1)

    print(f"[START] 입력: {input_path.name}")
    print(f"        출력: {output_path}")

    res = hybrid_convert(input_path, source_rel=args.source_rel)

    # 저장
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(res["md"], encoding="utf-8")

    print("\n[완료] 요약")
    print(f"  텍스트 길이     : {res['text_len']:,} chars")
    print(f"  추출 이미지 수  : {res['n_images']}개")
    print(f"  필터 후 이미지  : {res['n_filtered']}개")
    print(f"  Figure 섹션 수  : {res['n_figures']}개")
    print(f"  출력 MD 경로    : {output_path}")


if __name__ == "__main__":
    main()
