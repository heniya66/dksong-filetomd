#!/bin/bash
# goose_convert_one.sh — Goose 오케스트레이터용 단일 PDF 변환 래퍼 (Phase D', 2026-07-13)
# 신규 파일 (기존 fmdw 파일 무수정). env 표준은 convert_md.sh(2026-07-11 표준)와 동일.
# 사용: ./goose_convert_one.sh <PDF절대경로> <output_subdir>
#   → output/<output_subdir>/<stem>.md (+ _figures.json, _qa.json). 완료 시 "[ALL DONE]" 출력.
set -e
cd "$(dirname "$0")"

# --- 100% 로컬 LLM 표준 설정 (convert_md.sh와 동일) ---
export EXTRACT_PROVIDER=ollama_cloud OLLAMA_BASE_URL=http://localhost:11434/v1
export OLLAMA_VISION_MODEL=qwen3-vl:8b-instruct-q8_0 FMDW_MODEL_STRUCTURE=qwen3-vl:8b-instruct-q8_0 FMDW_MODEL_CAPTION=qwen3-vl:8b-instruct-q8_0
export FMDW_BODY_HYBRID=1 FMDW_BODY_PRIMARY_MODEL=glm-ocr FMDW_BODY_FALLBACK_MODEL=qwen3-vl:8b-instruct-q8_0
export EXTRACT_FIGURES=1 EXTRACT_CHUNK_SIZE="${EXTRACT_CHUNK_SIZE:-5}" EXTRACT_RENDER_DPI="${EXTRACT_RENDER_DPI:-300}"
export EXTRACT_DIAGONAL_TABLE=1 EXTRACT_OVERSIZED_MATRIX_TABLES=1 EXTRACT_DESCRIBE_COMPLEX_TABLES=1

PDF="${1:?usage: goose_convert_one.sh <pdf_path> <output_subdir>}"
OUTSUB="${2:-pdf_md}"

# ollama 살아있는지 확인
if ! curl -s -m 5 http://localhost:11434/api/version >/dev/null 2>&1; then
  echo "[!] ollama 응답 없음. ollama serve 확인 필요." >&2; exit 1
fi

echo "[fmdw] 단일 변환 시작: $PDF → output/$OUTSUB/ (dpi=$EXTRACT_RENDER_DPI, chunk=$EXTRACT_CHUNK_SIZE, figures=on)"
.venv/bin/python - "$PDF" "$OUTSUB" <<'PY'
import sys
from pathlib import Path
import extract_all_via_pdf as m

pdf = Path(sys.argv[1])
outsub = sys.argv[2]
try:
    m.setup_dirs()
    m.process_file(pdf, outsub)
    print("[ALL DONE]")
finally:
    # main()과 동일한 종료 시 로컬 LLM 전원 언로드 보장(fmdw 룰: 변환 완료 시 언로드 필수)
    m._unload_all_llms_at_exit()
PY
