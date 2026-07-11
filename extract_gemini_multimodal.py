#!/usr/bin/env python3
"""input/pdf/*.pdf → Markdown 일괄 변환기 (멀티모달 시각 분석, 한국어 출력).

내부는 provider-agnostic(기본 ollama_cloud, EXTRACT_PROVIDER=gemini 시 fallback) — 파일명의 'gemini'는 레거시 명칭일 뿐 Gemini 직접 호출이 아니다.

lib.pdf_pipeline.convert_pdf 공통 파이프라인 경유.
input/pdf 의 모든 PDF 를 정렬 순서로 처리하여 output/pdf_md/{stem}.md 생성.
사전 조건: source $HOME/workspace/_shared/keychain-env.sh
provider: ollama_cloud(기본, 키 불필요) | EXTRACT_PROVIDER=gemini 로 fallback.
청크 정책: 20페이지 단위 분할(항상).

임시파일 처리 결정 (동작 보존 근거):
  원본은 청크별 `{stem}_chunk_NNN.md` 임시파일에 쓴 뒤 읽어 병합·삭제했다.
  임시파일을 읽는 외부 다운스트림이 없음을 grep 으로 확인(소비처 0건)했고,
  최종 출력 `{stem}.md` 는 동일 구분자(`\\n\\n---\\n\\n`)로 병합되므로
  convert_pdf 의 메모리 병합 경로로 대체해도 최종 산출 byte 가 동일하다.
  → 임시파일 scaffolding 은 제거(H1 계획서 Phase 3 "임시파일 소멸 제외" 허용).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdw.pdf_pipeline import convert_pdf  # noqa: E402

INPUT_DIR = Path("input/pdf")
OUTPUT_DIR = Path("output/pdf_md")

# 멀티모달 추출 프롬프트 (verbatim — 원본 f-string 과 byte-동일)
PROMPT_TEMPLATE = (
    "Extract the full content of pages {start} to {end} from this PDF. "
    "Output in high-quality Markdown, preserving all text structure and tables (GFM pipe format). "
    "CRITICAL: For any images, diagrams, charts, or graphs, perform a highly detailed visual analysis. "
    "Describe the overall layout, visual elements, data trends, X/Y axis values, relationships, and core concepts. "
    "Write the image descriptions within Markdown blockquotes (>). "
    "IMPORTANT: All extracted text and image descriptions MUST be written in fluent Korean. "
    "Ensure all information is transcribed accurately."
)


def process_pdf(pdf_path: Path):
    """단일 PDF 를 convert_pdf 로 변환 (output/pdf_md/{stem}.md)."""
    return convert_pdf(
        pdf_path, PROMPT_TEMPLATE,
        output_path=OUTPUT_DIR / f"{pdf_path.stem}.md",
        chunk_size=20,
        single_chunk_max=None,   # 항상 20p 단위 분할 (원본 동작 보존)
        rate_limit_s=10,         # 원본 safety delay 10초 보존
        on_failure="abort",
    )


def main():
    if not INPUT_DIR.exists():
        print(f"Error: {INPUT_DIR} not found.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(list(INPUT_DIR.glob("*.pdf")))
    if not pdf_files:
        print(f"No PDF files found in {INPUT_DIR}")
        return

    print(f"Found {len(pdf_files)} PDF(s) to process.")
    for pdf in pdf_files:
        process_pdf(pdf)


if __name__ == "__main__":
    main()
