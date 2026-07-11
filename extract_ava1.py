#!/usr/bin/env python3
"""AVA_1.pdf → Markdown 변환기 (이미지 종류별 인라인 상세 분석 포함).

lib.pdf_pipeline.convert_pdf 공통 파이프라인 경유.
대상: <워크스페이스>/input/pdf/AVA_1.pdf
출력: <워크스페이스>/output/pdf_md/AVA_1.md
사전 조건: source $HOME/workspace/_shared/keychain-env.sh
provider: ollama_cloud(기본, 키 불필요) | EXTRACT_PROVIDER=gemini 로 fallback.
청크 정책: **항상 단일 청크**(페이지 수와 무관 — 원본 동작 byte 보존).
  → single_chunk_max=10**9 로 사실상 무한대 단일청크(docstring "ava1 식").
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdw.pdf_pipeline import convert_pdf  # noqa: E402

_HERE = Path(__file__).resolve().parent
PDF_PATH = _HERE / "input/pdf/AVA_1.pdf"
OUTPUT_DIR = _HERE / "output/pdf_md"
OUTPUT_PATH = OUTPUT_DIR / "AVA_1.md"

# 이미지 종류별 인라인 상세 분석 프롬프트 (verbatim — byte-동일 보존)
PROMPT_TEMPLATE = """PDF 페이지 {start}~{end} 전체 내용을 고품질 Markdown으로 추출하라.

일반 규칙:
- 페이지 헤더: `## Page N`
- 모든 텍스트·표(GFM pipe)·번호·서식 보존
- 식별번호·도면번호([Fig. N], [도 N], [그림 N]) 원문 표기 유지

[핵심] 이미지가 등장하는 본문 위치에 인라인 상세 분석 삽입 (별첨 금지):

> **[이미지 N · <유형>]**
>
> **제목/캡션**: <원문>
> **유형 식별**: 블록도/플로우차트/회로도/시스템도/스크린샷/사진/그래프/표이미지/다이어그램/UML/ERD/지도/일러스트/기타
>
> **상세 분석** (유형별 해당 항목 작성):
> • 블록도/시스템도/회로도: 블록·노드 위치(상단/중앙/하단 + 좌/우), 식별번호, 화살표 방향/라벨, 신호/데이터 흐름, 계층 그룹
> • 플로우차트: 시작/종료, 의사결정 분기 조건, 단계 순서, 루프, 예외 경로
> • 그래프: X축·Y축 라벨/단위/범위, 범례, 추세, 주요 수치(피크/평균/교차점)
> • 스크린샷/UI: 화면 영역, UI 컴포넌트, 표시 텍스트·숫자, 사용 맥락
> • 사진/일러스트: 주체·배경, 색감/구도, 객체 위치·행동
> • 표 이미지: 행·열 헤더 + 모든 셀을 GFM pipe table로 재구성
> • 다이어그램/UML/ERD: 엔티티·관계·카디널리티·메서드 시그니처
>
> **포함된 텍스트/라벨**: 이미지 내부 모든 텍스트·숫자 전사 (OCR 수준)
> **본문 연계**: 본문 어느 단락/섹션에서 참조되며 어떤 주장을 뒷받침하는지 1-2문장

- 색상 의미(빨강=경고 등) 명시
- 작은 텍스트도 OCR 수준 전사
"""


def main():
    if not PDF_PATH.exists():
        print(f"[!] 파일 없음: {PDF_PATH}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = convert_pdf(
        PDF_PATH, PROMPT_TEMPLATE,
        output_path=OUTPUT_PATH,
        chunk_size=20,
        single_chunk_max=10**9,   # 항상 단일 청크 (원본: start,end=1,total_pages)
        on_failure="abort",
    )
    if result:
        print(f"\n=== 완료 ===\n출력: {result}\n크기: {result.stat().st_size} bytes")
    else:
        print("[!] 변환 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
