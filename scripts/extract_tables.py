"""
AVA_1.md Page 12, AVA_2.md Page 11 표 재추출
provider 추상화(lib.ollama_extractor) 경유 — 기본 ollama_cloud(로컬 게이트웨이, 키 불필요),
EXTRACT_PROVIDER=gemini 로 기존 Gemini File API 경로 fallback.
"""
import os
import sys
import time
import warnings
from pathlib import Path
warnings.filterwarnings("ignore", category=FutureWarning)

# 공통 추출 모듈(fmdw.ollama_extractor) import 경로 보장 — 워크스페이스 루트 추가.
# 이 파일은 scripts/ 하위이므로 루트는 parent.parent.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from fmdw import ollama_extractor as ox  # noqa: E402

def extract_table_from_pdf(pdf_path: str, page_num: int, label: str) -> str:
    """PDF 특정 페이지의 표를 GFM(GitHub Flavored Markdown) pipe table로 추출"""
    print(f"[{label}] 처리 중: {pdf_path} (provider: {ox.provider_label()})")

    prompt = f"""이 PDF의 {page_num}페이지에서 모든 표(table)를 GFM(GitHub Flavored Markdown) pipe table로 빠짐없이 전사하라.
모든 행과 모든 열을 포함하고, 어떤 셀도 `...`로 생략하지 말 것.
빨강 강조 셀은 **bold** 처리. 표 외 본문은 출력하지 말 것.
표만 출력하고, 표가 여러 개이면 표 사이에 빈 줄."""

    print(f"[{label}] 추출 요청 중 (page {page_num})...")
    result = ox.extract_pdf_single_page(prompt, pdf_path, page_num).strip()
    print(f"[{label}] 추출 완료. 라인 수: {len(result.splitlines())}")

    return result


if __name__ == "__main__":
    base = str(_ROOT)

    # AVA_1.md Page 12
    result_ava1 = extract_table_from_pdf(
        pdf_path=f"{base}/input/pdf/AVA_1.pdf",
        page_num=12,
        label="AVA_1 Page12"
    )
    out1 = f"{base}/output/pdf_md/extracted_ava1_p12.txt"
    with open(out1, "w", encoding="utf-8") as f:
        f.write(result_ava1)
    print(f"[AVA_1 Page12] 저장: {out1}")

    time.sleep(2)

    # AVA_2.md Page 11
    result_ava2 = extract_table_from_pdf(
        pdf_path=f"{base}/input/pdf/AVA_2.pdf",
        page_num=11,
        label="AVA_2 Page11"
    )
    out2 = f"{base}/output/pdf_md/extracted_ava2_p11.txt"
    with open(out2, "w", encoding="utf-8") as f:
        f.write(result_ava2)
    print(f"[AVA_2 Page11] 저장: {out2}")

    print("\n=== 완료 ===")
    print(f"AVA_1 Page12 라인수: {len(result_ava1.splitlines())}")
    print(f"AVA_2 Page11 라인수: {len(result_ava2.splitlines())}")
