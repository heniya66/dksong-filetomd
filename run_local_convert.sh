#!/bin/bash
# fmdw 100% 로컬 변환 래퍼 (qwen3-vl:32b via ollama). 2026-06-25.
# 사용: ./run_local_convert.sh --project <프로젝트절대경로> [--domain schematic] [--force] [--figures] [--dry-run]
# 고밀도(회로도/datasheet)는: EXTRACT_RENDER_DPI=300 ./run_local_convert.sh --project <P> --domain schematic
set -euo pipefail

# --- 100% 로컬 ollama (qwen3-vl) 설정 ---
export EXTRACT_PROVIDER="${EXTRACT_PROVIDER:-ollama_cloud}"       # 로컬 ollama 게이트웨이 경유
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434/v1}"
export OLLAMA_VISION_MODEL="${OLLAMA_VISION_MODEL:-qwen3-vl:32b}"  # 단일 모델 기본
export FMDW_MODEL_STRUCTURE="${FMDW_MODEL_STRUCTURE:-qwen3-vl:32b}" # 역할A: 구조 추출
export FMDW_MODEL_CAPTION="${FMDW_MODEL_CAPTION:-qwen3-vl:32b}"     # 역할B: 이미지 설명
export EXTRACT_CHUNK_SIZE="${EXTRACT_CHUNK_SIZE:-5}"               # 무결성(silent truncation 방지)
export EXTRACT_RENDER_DPI="${EXTRACT_RENDER_DPI:-200}"            # 일반 200 / 고밀도 300

# --- 복잡 표(complex_table) 크롭·설명 전역 기본 ON (2026-07-03) ---
# 사선/비정형 표(diagonal_tables)와 오버사이즈 행렬표(oversized_matrix_tables)는 완전히
# 독립된 탐지 경로다(전자=벡터 격자 사선, 후자=raster 이미지 px밀도/점유율) — 둘 다 기본
# ON 이어도 서로의 판정에 관여하지 않는다. 임계값은 실측(LN08LPU Design Manual testpages
# p11~13 "Table 21" 오버사이즈 행렬표 vs p10 정상 벡터 표) 기준 안전마진 포함 기본값.
export EXTRACT_DIAGONAL_TABLE="${EXTRACT_DIAGONAL_TABLE:-1}"                       # 사선/비정형 표 → 이미지
export EXTRACT_OVERSIZED_MATRIX_TABLES="${EXTRACT_OVERSIZED_MATRIX_TABLES:-1}"     # raster 오버사이즈 행렬표 → 이미지
export EXTRACT_OVERSIZED_IMG_MIN_DENSITY="${EXTRACT_OVERSIZED_IMG_MIN_DENSITY:-3.5}"  # 최소 px밀도(px/pt)
export EXTRACT_OVERSIZED_IMG_MIN_COVER="${EXTRACT_OVERSIZED_IMG_MIN_COVER:-0.30}"     # 최소 페이지 점유율
export EXTRACT_DESCRIBE_COMPLEX_TABLES="${EXTRACT_DESCRIBE_COMPLEX_TABLES:-1}"     # complex_table 은 전역 describe OFF 여도 설명 생성
export EXTRACT_FIGURE_DESC_MAX_TOKENS_COMPLEX="${EXTRACT_FIGURE_DESC_MAX_TOKENS_COMPLEX:-16384}"  # complex_table describe 전용 max_tokens

# --- 본문 하이브리드 전사(FMDW_BODY_HYBRID) 기본 ON (2026-07-04) ---
# glm-ocr(빠름·정확하나 표지/법적고지/목차머리 페이지 스킵 위험) 을 1차로 쓰고,
# 그 페이지의 selectable 텍스트 대비 glm 출력이 너무 짧으면(커버리지 미달) qwen3-vl:32b
# 로 그 페이지만 재전사(repair)한다. 표처럼 glm 이 잘 처리하는 조밀한 페이지는 그대로
# 빠르게 유지된다(상세: extract_all_via_pdf.py _hybrid_transcribe_page).
export FMDW_BODY_HYBRID="${FMDW_BODY_HYBRID:-1}"
export FMDW_BODY_PRIMARY_MODEL="${FMDW_BODY_PRIMARY_MODEL:-glm-ocr}"       # 1차: 빠른 OCR
export FMDW_BODY_FALLBACK_MODEL="${FMDW_BODY_FALLBACK_MODEL:-qwen3-vl:32b}" # 폴백: 스킵 페이지 재전사
export FMDW_HYBRID_COVERAGE_MIN="${FMDW_HYBRID_COVERAGE_MIN:-0.30}"   # glm출력 < 30%×pdf텍스트 → 폴백
export FMDW_HYBRID_MIN_TEXT="${FMDW_HYBRID_MIN_TEXT:-120}"            # 이 미만 pdf텍스트는 이미지위주로 간주(가드)

# --- ollama 살아있는지 확인 ---
if ! curl -s -m 5 "${OLLAMA_BASE_URL%/v1}/api/version" >/dev/null 2>&1; then
  echo "[!] ollama serve 응답 없음 (${OLLAMA_BASE_URL}). launchd 서비스 확인 필요." >&2
  exit 1
fi

cd "$(dirname "$0")"
echo "[fmdw] model=${OLLAMA_VISION_MODEL} dpi=${EXTRACT_RENDER_DPI} chunk=${EXTRACT_CHUNK_SIZE}"
exec .venv/bin/python convert_project.py "$@"
