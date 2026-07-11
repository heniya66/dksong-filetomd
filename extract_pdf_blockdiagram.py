#!/usr/bin/env python3
"""PDF → Markdown 변환기 (블록도/도면 인라인 상세 분석 포함).

lib.pdf_pipeline.convert_pdf 공통 파이프라인 경유.
사용법: python3 extract_pdf_blockdiagram.py "<pdf 경로>"
사전 조건: source $HOME/workspace/_shared/keychain-env.sh
provider: ollama_cloud(기본, 키 불필요) | EXTRACT_PROVIDER=gemini 로 fallback.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdw.pdf_pipeline import convert_pdf  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "output/pdf_md"

# 블록도/도면 인라인 상세 분석 프롬프트 (verbatim — byte-동일 보존)
PROMPT_TEMPLATE = """이 PDF의 {start}~{end}페이지 전체 내용을 고품질 Markdown으로 추출하라.

일반 규칙:
- 모든 텍스트·표(GFM pipe format)·번호·서식 보존
- 페이지 헤더: `## Page N`
- 특허 청구항·식별번호·도면번호([도1], [도2] 등) 원문 표기 유지

[핵심] 블록도/구성도/플로우차트/시스템도/회로도 등 시각 요소 처리 규칙:
- 도면이 등장하는 **정확한 본문 위치**에 인라인 삽입 (별첨 금지)
- 형식:

  > **[도면 N · 블록도 분석]**
  >
  > **제목**: <캡션 원문>
  >
  > **구성 요소(블록)**:
  > - **블록 A** (위치: 좌상단): <역할·입력·출력>
  > - **블록 B** (위치: 중앙): <역할·연결관계>
  > - ... (모든 블록을 위치 정보와 함께 열거)
  >
  > **신호/데이터 흐름**:
  > 1. A → B: <전달되는 신호/데이터 명세>
  > 2. B → C: ...
  >
  > **계층/그룹**: <점선 박스·서브시스템 묶음 설명>
  >
  > **주석/라벨**: <화살표 라벨, 식별번호 매핑>
  >
  > **본문 연계**: 본 도면은 <본문 단락/청구항 N>에서 참조됨

- 블록 위치 표현은 상대 좌표(좌상/중앙/우하 등)와 계층(상위/하위) 둘 다 사용
- 식별번호(예: 100, 110, 120)가 있으면 모든 블록에 매핑하여 명시
- 화살표 방향·라벨·점선/실선 구분 보존
- 표 형식이 더 적합하면 마크다운 표로 블록 목록 작성 가능
"""


def main():
    if len(sys.argv) < 2:
        print("사용법: python3 extract_pdf_blockdiagram.py <pdf_path>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"[!] 파일 없음: {pdf_path}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = convert_pdf(
        pdf_path, PROMPT_TEMPLATE,
        output_path=OUTPUT_DIR / f"{pdf_path.stem}.md",
        chunk_size=20,
        single_chunk_max=None,
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
