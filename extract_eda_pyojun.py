#!/usr/bin/env python3
"""EDA 실행 표준 구조.pdf → Markdown 변환기.

lib.pdf_pipeline.convert_pdf 공통 파이프라인 경유.
이미지 유형별 차별화 분석 + 본문 위치 인라인 삽입.
사전 조건: source $HOME/workspace/_shared/keychain-env.sh
provider: ollama_cloud(기본, 키 불필요) | EXTRACT_PROVIDER=gemini 로 fallback.
청크 정책: ≤25페이지 → 단일 청크 / 초과 → 20페이지 단위 분할.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdw.pdf_pipeline import convert_pdf  # noqa: E402

_HERE = Path(__file__).resolve().parent
PDF_PATH = _HERE / "input/pdf/EDA 실행 표준 구조.pdf"
OUTPUT_DIR = _HERE / "output/pdf_md"

# 이미지 유형별 차별화 분석 프롬프트 (verbatim — byte-동일 보존)
PROMPT_TEMPLATE = """PDF 페이지 {start}~{end} 전체를 고품질 Markdown으로 추출하라.

- 페이지 헤더: `## Page N` (실제 PDF(Portable Document Format) 페이지번호, 리셋 금지)
- 모든 텍스트·표(GFM(GitHub Flavored Markdown) pipe, 셀 생략 금지)·번호·서식 보존
- 이미지(다이어그램/블록도/플로우차트/그래프/스크린샷/표이미지/시스템도/타임라인)가 등장하는 본문 위치에 인라인 분석 삽입:

> **[이미지 N · <유형>]**
> **제목/캡션**: ... / **유형 식별**: ...
> **상세 분석**: (한 그림을 하나의 단위로 종합 서술 — 구성요소를 개별 블록으로 쪼개지 말 것. 위치·흐름·축·범례·식별번호)
> **포함된 텍스트/라벨**: 내부 텍스트 OCR(Optical Character Recognition) 전사
> **본문 연계**: ...

- 한 그림이 슬라이스/조각나도 1개 블록으로 통합. 로고/헤더/푸터/배경음영은 figure 아님.
- 표 배경 음영은 표 → GFM(GitHub Flavored Markdown) 표로 추출.
- 색상 의미 명시.
"""


def main():
    if not PDF_PATH.exists():
        print(f"[!] 파일 없음: {PDF_PATH}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = convert_pdf(
        PDF_PATH, PROMPT_TEMPLATE,
        output_path=OUTPUT_DIR / f"{PDF_PATH.stem}.md",
        chunk_size=20,
        single_chunk_max=25,   # ≤25페이지 → 단일 청크 (기존 동작 보존)
        rate_limit_s=15,
        on_failure="abort",
    )
    if result:
        print(f"\n=== 완료 ===\n출력: {result}\n크기: {result.stat().st_size} bytes")
    else:
        print("[!] 변환 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
