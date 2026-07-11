"""수동 진단/보수 도구 — 누락/부족 페이지만 멀티모달로 재추출하여 MD 보강.

자동 파이프라인(extract_all_via_pdf.py) 미연결. 수동 실행 전용.

사용법:
    python3 scripts/reextract_figures.py \\
        --pdf path/to/file.pdf \\
        --md  path/to/file.md  \\
        --missing-pages 2 4 5 6

주의:
    이 도구는 --md 의 ## Page N 섹션 끝에 이미지 블록을 추가(append)한다.
    동일 페이지를 반복 실행하면 블록이 중복 삽입될 수 있으므로,
    클린 상태의 MD에 실행하거나 --skip-existing 플래그를 사용할 것.
    (--skip-existing: 해당 페이지 섹션에 이미지 블록이 이미 있으면 스킵)

provider 추상화(lib.ollama_extractor) 경유 — 기본 ollama_cloud(로컬 게이트웨이, 키 불필요).
EXTRACT_PROVIDER=gemini 로 기존 Gemini File API 경로 fallback.
"""
import argparse
import os
import re
import time
import sys
from pathlib import Path

# 공통 추출 모듈(fmdw.ollama_extractor) import 경로 보장 — 워크스페이스 루트(scripts의 상위) 추가.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fmdw import ollama_extractor as ox  # noqa: E402

EXTRACT_PROMPT = """이 PDF의 {page}페이지를 통째로 보고, 독자에게 설명 가치가 있는 모든 독립적 그림을 식별하라.

식별 규칙:
- 포함: 다이어그램, 블록도, 플로우차트, 그래프, 스크린샷, 표이미지, 시스템도
- 제외: 로고, 헤더, 푸터, 배경장식, 단순 구분선
- 한 그림이 여러 조각으로 슬라이스된 경우 → 1개로 통합하여 분석
- 페이지에 의미있는 figure가 없으면 "FIGURE 없음"만 출력하라

각 figure를 아래 형식으로 분석하고, 페이지 본문 텍스트도 함께 추출하라:

> **[이미지 · <유형>]**
> **제목/캡션**: ...
> **유형 식별**: 블록도/플로우차트/그래프/시스템도/스크린샷/표이미지/다이어그램/타임라인/기타
> **상세 분석**: (구성요소·위치·축·범례·단계·흐름·식별번호)
> **포함된 텍스트/라벨**: 이미지 내부 모든 텍스트 OCR(Optical Character Recognition) 전사
> **본문 연계**: ...

분석 형식의 블록을 독립적 figure 개수만큼만 반환하라."""


def extract_page(pdf_path: Path, page_num: int) -> str:
    """특정 페이지 figure 추출 (provider 추상화 경유 — 해당 페이지만 렌더/전송)."""
    prompt = EXTRACT_PROMPT.format(page=page_num)
    return ox.extract_pdf_single_page(prompt, pdf_path, page_num).strip()


def get_existing_image_blocks(section_text: str) -> list[str]:
    """섹션 텍스트에서 기존 이미지 블록 제목/캡션 추출 (중복 방지용).

    Returns:
        기존 이미지 블록 마커 문자열 리스트 (비어있으면 해당 섹션에 블록 없음).
    """
    return re.findall(r'>\s*\*\*\[이미지[^\]]*\]\*\*', section_text)


def inject_into_md(
    md_path: Path,
    page_num: int,
    new_blocks_text: str,
    skip_existing: bool = False,
) -> bool:
    """MD 파일의 ## Page N 섹션에 새 이미지 블록 삽입.

    Args:
        md_path: 보강 대상 Markdown 파일 경로.
        page_num: 삽입 대상 페이지 번호(1-based).
        new_blocks_text: 추출된 이미지 블록 텍스트.
        skip_existing: True 이면 해당 섹션에 이미 이미지 블록이 있을 때 스킵(멱등성 보장).

    Returns:
        True if insertion was performed, False otherwise.
    """
    text = Path(md_path).read_text(encoding='utf-8')

    # ## Page N 섹션 찾기
    pattern = rf'(## Page {page_num}\b.*?)(?=\n## Page |\Z)'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        print(f"  WARNING: ## Page {page_num} 섹션을 찾을 수 없음")
        return False

    section_text = match.group(1)
    section_end = match.end()

    # 기존 이미지 블록 확인 — get_existing_image_blocks 로 실제 wire
    existing_blocks = get_existing_image_blocks(section_text)
    existing_count = len(existing_blocks)

    if skip_existing and existing_count > 0:
        print(f"  Page {page_num}: 기존 블록 {existing_count}개 있음 → --skip-existing 으로 스킵")
        return False

    # 새 블록 파싱: > **[이미지 로 시작하는 블록만 추출
    new_image_blocks = re.findall(
        r'(>\s*\*\*\[이미지.*?)(?=\n>\s*\*\*\[이미지|\Z)',
        new_blocks_text,
        re.DOTALL
    )

    if not new_image_blocks:
        # 단일 블록 전체가 이미지 블록인 경우
        if re.search(r'>\s*\*\*\[이미지', new_blocks_text):
            new_image_blocks = [new_blocks_text]

    if not new_image_blocks:
        print(f"  Page {page_num}: 추출된 이미지 블록 없음 (그림 없음 응답)")
        return False

    to_insert = len(new_image_blocks)
    print(f"  Page {page_num}: 기존 {existing_count}개 → +{to_insert}개 삽입 예정")

    insert_text = "\n\n" + "\n\n".join(b.strip() for b in new_image_blocks)
    new_text = text[:section_end] + insert_text + text[section_end:]
    Path(md_path).write_text(new_text, encoding='utf-8')
    print(f"  Page {page_num}: 삽입 완료")
    return True


def process_file(
    pdf_path: Path,
    md_path: Path,
    missing_pages: list[int],
    skip_existing: bool = False,
) -> int:
    """지정 페이지를 재추출하여 MD에 보강.

    Returns:
        성공적으로 삽입된 페이지 수.
    """
    print(f"\n{'='*60}")
    print(f"처리: {pdf_path.name}")
    print(f"누락 페이지: {missing_pages}")
    print(f"{'='*60}")

    if not missing_pages:
        print("  보강 불필요")
        return 0

    print(f"  provider: {ox.provider_label()}")

    inserted_count = 0
    for page_num in missing_pages:
        print(f"\n  [Page {page_num}] 멀티모달 추출 중...")
        try:
            result = extract_page(pdf_path, page_num)
            print(f"  응답 길이: {len(result)}자")

            if "FIGURE 없음" in result or "그림 없음" in result or len(result) < 20:
                print(f"  Page {page_num}: FIGURE 없음으로 응답 → 스킵")
                continue

            if inject_into_md(md_path, page_num, result, skip_existing=skip_existing):
                inserted_count += 1
            time.sleep(1)
        except Exception as e:
            print(f"  Page {page_num} 오류: {e}")
            time.sleep(3)

    return inserted_count


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "수동 보수 도구: 누락/부족 페이지만 멀티모달로 재추출하여 MD 보강.\n"
            "자동 파이프라인 미연결 — 수동 실행 전용.\n\n"
            "예시:\n"
            "  python3 scripts/reextract_figures.py \\\n"
            "      --pdf output/pdf_md/../input/pdf/MyDoc.pdf \\\n"
            "      --md  output/pdf_md/MyDoc.md \\\n"
            "      --missing-pages 2 4 5 6"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--pdf", required=True, type=Path,
                   help="원본 PDF 파일 경로")
    p.add_argument("--md", required=True, type=Path,
                   help="보강 대상 Markdown 파일 경로")
    p.add_argument("--missing-pages", nargs="+", type=int, required=True,
                   metavar="N",
                   help="재추출할 페이지 번호 목록 (1-based, 공백 구분). 예: --missing-pages 2 4 5")
    p.add_argument("--skip-existing", action="store_true",
                   help="해당 섹션에 이미지 블록이 이미 있으면 스킵 (멱등 재실행 시 권장)")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    pdf_path = args.pdf
    md_path = args.md

    if not pdf_path.exists():
        parser.error(f"PDF 파일을 찾을 수 없음: {pdf_path}")
    if not md_path.exists():
        parser.error(f"MD 파일을 찾을 수 없음: {md_path}")

    print(f"[reextract_figures] provider: {ox.provider_label()}")
    n = process_file(pdf_path, md_path, args.missing_pages,
                     skip_existing=args.skip_existing)
    print(f"\n완료: +{n}페이지 보강")
