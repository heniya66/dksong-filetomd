#!/bin/bash
# fmdw PDF→MD 변환 래퍼 (100% 로컬 LLM, 최신 파이프라인). 2026-07-11.
# 사용법:
#   ./convert_md.sh                 # input/pdf/ 안의 모든 PDF 변환 (기존 완전본은 자동 스킵)
#   ./convert_md.sh a.pdf b.pdf     # 지정한 PDF를 input/pdf/ 로 복사 후 변환
# 결과: output/pdf_md/<이름>.md  (도면은 output/pdf_md/figures/ 에 크롭+본문 인라인)
set -e
cd "$(dirname "$0")"

# --- 100% 로컬 LLM 표준 설정 ---
export EXTRACT_PROVIDER=ollama_cloud OLLAMA_BASE_URL=http://localhost:11434/v1
export OLLAMA_VISION_MODEL=qwen3-vl:8b-instruct-q8_0 FMDW_MODEL_STRUCTURE=qwen3-vl:8b-instruct-q8_0 FMDW_MODEL_CAPTION=qwen3-vl:8b-instruct-q8_0
export FMDW_BODY_HYBRID=1 FMDW_BODY_PRIMARY_MODEL=glm-ocr FMDW_BODY_FALLBACK_MODEL=qwen3-vl:8b-instruct-q8_0
export EXTRACT_FIGURES=1 EXTRACT_CHUNK_SIZE=5 EXTRACT_RENDER_DPI=300
export EXTRACT_DIAGONAL_TABLE=1 EXTRACT_OVERSIZED_MATRIX_TABLES=1 EXTRACT_DESCRIBE_COMPLEX_TABLES=1

# ollama 살아있는지 확인
if ! curl -s -m 5 http://localhost:11434/api/version >/dev/null 2>&1; then
  echo "[!] ollama 응답 없음. ollama serve 확인 필요." >&2; exit 1
fi

# 인자로 준 PDF 를 input/pdf/ 로 스테이징
mkdir -p input/pdf output/pdf_md
for f in "$@"; do
  if [ -f "$f" ]; then cp "$f" input/pdf/ && echo "[+] staged: $(basename "$f")"; else echo "[!] 없음: $f" >&2; fi
done

echo "[fmdw] 변환 시작 (dpi=300, figures=on, 로컬 LLM)"
.venv/bin/python extract_all_via_pdf.py
echo "[완료] → output/pdf_md/ 에서 확인"
