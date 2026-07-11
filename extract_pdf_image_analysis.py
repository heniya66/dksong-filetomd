#!/usr/bin/env python3
"""PDF → Markdown 변환기 (이미지 종류별 차별화 인라인 상세 분석 포함).

lib.pdf_pipeline.convert_pdf 공통 파이프라인 경유.
멀티모달 시각 분석 — 이미지 유형별 차별화 인라인 삽입.
사용법: python3 extract_pdf_image_analysis.py "<pdf 경로>"
사전 조건: source $HOME/workspace/_shared/keychain-env.sh
provider: ollama_cloud(기본, 키 불필요) | EXTRACT_PROVIDER=gemini 로 fallback.
청크 정책: ≤25페이지 → 단일 청크 / 초과 → 20페이지 단위 분할.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdw.pdf_pipeline import convert_pdf  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "output/pdf_md"

# 이미지 종류별 차별화 인라인 상세 분석 프롬프트 (verbatim — byte-동일 보존)
PROMPT_TEMPLATE = """PDF 페이지 {start}~{end} 전체 내용을 고품질 Markdown으로 추출하라.

일반 규칙:
- 페이지 헤더: `## Page N`
- 모든 텍스트·표(GFM pipe)·번호·서식 보존
- 식별번호·도면번호([Fig. N], [도 N], [그림 N] 등) 원문 표기 유지

[핵심] 이미지가 등장할 때마다 **본문 흐름상의 정확한 위치에 인라인으로** 상세 분석 삽입.
별첨/말미 몰아넣기 금지. 형식:

> **[이미지 N · <유형>]**
>
> **제목/캡션**: <원문>
>
> **유형 식별**: 블록도 / 플로우차트 / 회로도 / 시스템 구성도 / 스크린샷 / 사진 / 그래프(line/bar/scatter) / 표 이미지 / 다이어그램 / UML / ERD / 지도 / 일러스트 / 기타
>
> **상세 분석** (유형에 따라 아래에서 해당 항목 작성, 해당 없는 항목 생략):
>
> • **블록도/시스템도/회로도**: 모든 블록·노드를 위치(상단/중앙/하단 + 좌/우)와 함께 열거, 식별번호 매핑, 연결선/화살표 방향·라벨, 신호/데이터 흐름 순서, 계층/그룹(점선 박스)
> • **플로우차트**: 시작/종료 노드, 의사결정(diamond) 분기 조건, 단계 순서, 루프, 예외 경로
> • **그래프**: X축·Y축 라벨과 단위·범위, 범례(legend) 각 항목, 곡선/막대 추세, 주요 수치(피크·평균·교차점), 축약/주석
> • **스크린샷/UI(User Interface)**: 화면 구성 영역(헤더/사이드바/메인/하단), 주요 UI(User Interface) 컴포넌트(버튼/입력/표), 표시된 텍스트·숫자, 사용 맥락 추정
> • **사진/일러스트**: 주체·배경·전경, 색감/구도, 인물·객체 위치와 행동, 관련 메타 정보(제품명·로고)
> • **표 이미지(텍스트 추출 불가형)**: 행·열 헤더, 모든 셀 데이터를 GFM(GitHub Flavored Markdown) pipe table로 재구성
> • **다이어그램/UML(Unified Modeling Language)/ERD(Entity-Relationship Diagram)**: 엔티티·관계·카디널리티·메서드 시그니처
>
> **포함된 텍스트/라벨**: 이미지 내부 모든 텍스트·숫자 그대로 인용 (라벨, 주석, 워터마크 포함)
>
> **본문 연계**: 본 이미지는 <본문 단락/섹션/문장>에서 참조됨. 본문이 설명하는 핵심 주장을 어떻게 뒷받침하는지 1-2문장.

- 이미지 내부 작은 텍스트도 OCR(Optical Character Recognition) 수준으로 모두 전사
- 색상이 정보 전달 의미를 가지면(빨강=경고 등) 명시
- 사진/캡처 화면의 경우 개인정보·민감정보 포함 시 그대로 전사 (사용자 워크스페이스 자료 가정)
"""


def main():
    if len(sys.argv) < 2:
        print("사용법: python3 extract_pdf_image_analysis.py <pdf_path>")
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
