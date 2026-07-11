#!/usr/bin/env python3
"""Sim_Platform.pdf → Markdown 변환기 (멀티모달 이미지 인라인 상세 분석).

lib.pdf_pipeline.convert_pdf 공통 파이프라인 경유.
대상: <워크스페이스>/input/pdf/Sim_Platform.pdf
출력: <워크스페이스>/output/pdf_md/Sim_Platform.md
사용법: python3 extract_sim_platform.py
사전 조건: source $HOME/workspace/_shared/keychain-env.sh
provider: ollama_cloud(기본, 키 불필요) | EXTRACT_PROVIDER=gemini 로 fallback.
청크 정책: 20페이지 단위 분할(항상).
post_process: renumber_images — chunk별 이미지 순번을 전체 문서 통합 순번으로 재부여.
  원본은 청크별로 running offset 으로 재부여 후 병합했으나, 구분자에 `[이미지`
  패턴이 없으므로 "병합 후 1회 재부여"와 byte-동일(검증 완료) → convert_pdf 의
  post_process(병합 결과에 1회 적용) 경로로 보존.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdw.pdf_pipeline import convert_pdf  # noqa: E402

_HERE = Path(__file__).resolve().parent
PDF_PATH = _HERE / "input/pdf/Sim_Platform.pdf"
OUTPUT_DIR = _HERE / "output/pdf_md"
OUTPUT_PATH = OUTPUT_DIR / "Sim_Platform.md"

# 멀티모달 이미지 인라인 상세 분석 프롬프트 (verbatim — byte-동일 보존)
PROMPT_TEMPLATE = """PDF 페이지 {start}~{end} 전체를 고품질 Markdown으로 추출하라.

- 페이지 헤더: `## Page N` (N은 반드시 {start}~{end} 범위의 실제 PDF 페이지번호. 1부터 리셋 절대 금지)
- {start}~{end} 범위 밖 페이지는 출력하지 말 것 (중복 전사 금지)
- 모든 텍스트·표(GFM pipe, 셀 생략 금지)·번호·서식 보존
- 이미지(다이어그램/블록도/플로우차트/그래프/스크린샷/표이미지/시스템도/타임라인)가 등장하는 본문 위치에 인라인 분석 삽입:
  > **[이미지 N · <유형>]**
  > **제목/캡션**: ... / **유형 식별**: ...
  > **상세 분석**: (한 그림을 하나의 단위로 종합 서술 — 구성요소를 개별 블록으로 쪼개지 말 것. 위치·흐름·축·범례·식별번호)
  > **포함된 텍스트/라벨**: 내부 텍스트(파일경로·로그 포함) OCR(Optical Character Recognition) 전사
  > **본문 연계**: ...
- 한 그림이 슬라이스/조각나도 1개 블록으로 통합. 페이지당 figure가 많아도 의미있는 독립 그림 단위로만 (구성요소 남발 금지). 로고/헤더/푸터/배경음영 제외.
- 색상 의미 명시.
- 이미지 순번은 해당 chunk 내 등장 순서로 부여 (전체 문서 통합 순번은 후처리)
"""


def renumber_images(text: str, offset: int) -> tuple[str, int]:
    """chunk별 이미지 순번을 전체 문서 통합 순번으로 재부여."""
    count = 0
    def replacer(m):
        nonlocal count, offset
        count += 1
        new_num = offset + count
        return m.group(0).replace(m.group(1), str(new_num), 1)
    # [이미지 N · ...] 패턴 치환
    result = re.sub(r'\[이미지 (\d+)\s*·', replacer, text)
    return result, offset + count


def _renumber_post_process(combined: str) -> str:
    """convert_pdf post_process 어댑터 — 병합 결과 전체에 1회 재부여(offset=0).

    구분자 `\\n\\n---\\n\\n` 에는 `[이미지` 패턴이 없으므로, 병합 후 1회 적용이
    원본의 "청크별 running-offset 재부여 후 병합"과 byte-동일하다(검증 완료).
    """
    return renumber_images(combined, 0)[0]


def main():
    if not PDF_PATH.exists():
        print(f"[!] 파일 없음: {PDF_PATH}")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = convert_pdf(
        PDF_PATH, PROMPT_TEMPLATE,
        output_path=OUTPUT_PATH,
        chunk_size=20,
        single_chunk_max=None,   # 항상 20p 단위 분할 (원본 동작 보존)
        rate_limit_s=15,
        post_process=_renumber_post_process,
        on_failure="abort",
    )
    if result:
        size = result.stat().st_size
        img_count = result.read_text(encoding="utf-8").count("[이미지")
        print(f"\n=== 변환 완료 ===")
        print(f"출력: {result}")
        print(f"크기: {size:,} bytes")
        print(f"이미지 분석 블록 수: {img_count}")
    else:
        print("[!] 변환 실패")


if __name__ == "__main__":
    main()
