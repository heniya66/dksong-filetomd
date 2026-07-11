"""filestomdwgem 멀티모달 추출 provider 추상화 — Ollama Cloud(로컬 게이트웨이) + Gemini fallback.

본 모듈은 filestomdwgem의 extract_*.py 스크립트들이 공유하는 단일 추출 진입점을 제공한다.
PDF(Portable Document Format) 페이지 범위 또는 단일 이미지를 입력받아, 선택된
provider(Provider)로 멀티모달 분석한 Markdown(MD) 텍스트를 반환한다.

전환 배경 (2026-05-29, 사용자 명시 지시):
  - 기존: 각 extract_*.py가 `google.generativeai`(deprecated) / `google.genai`로
    `gemini-2.5-pro`/`gemini-2.5-flash`를 직접 호출 (genai.upload_file +
    model.generate_content). PDF 전체를 File API로 업로드하고 프롬프트에 페이지 범위
    지시를 넣는 방식.
  - 신규(기본): 로컬 `ollama serve`(localhost:11434) 게이트웨이 경유 OpenAI(Open AI)
    호환 vision 호출. 로컬 데몬이 ed25519 device key로 cloud 모델
    (gemini-3-flash-preview)을 프록시하므로 localhost 호출에는 Authorization 헤더가
    불필요(키 불필요). 모델은 thinking 모델이라 max_tokens 충분히(기본 2048+) 필요.

provider 스위치 (환경변수 EXTRACT_PROVIDER):
  - "ollama_cloud" (기본): 로컬 Ollama 게이트웨이 vision 경로.
  - "gemini": 기존 google.generativeai File API 경로 (품질 비교/롤백 대비 보존).

설계 원칙 (CLAUDE.md 글로벌 룰 준수):
  - Claude 단일 LLM(Large Language Model) 룰: 본 코드는 Claude가 직접 작성.
  - 시크릿: localhost 게이트웨이는 키 불필요. gemini fallback만 macOS Keychain SSoT
    (Single Source of Truth)의 GEMINI_API_KEY를 env로 주입받아 사용. 키 값 출력 금지.
  - 의존성: 신규 하드 의존성 추가 없이 httpx + fitz(PyMuPDF, 이미 사용 중)만 사용.
    gemini 경로는 호출 시점에만 google.generativeai를 import (지연 import).
  - 워크스페이스 독립: codesign-rag src.rag.ollama_cloud를 참고하되 import하지 않고
    filestomdwgem 자체 구현.

PDF → 페이지 이미지 렌더 주의:
  - Gemini File API는 PDF를 통째로 업로드하고 프롬프트의 페이지 번호로 범위를 지정했다.
  - Ollama vision은 이미지(base64) 입력이므로, fitz로 해당 페이지 범위를 PNG로 래스터화
    하여 전달한다. 따라서 프롬프트 내 "{start}~{end}페이지" 지시는 그대로 보존하되,
    실제로 전달되는 이미지도 그 범위로 한정되어 일관성이 유지된다.
"""

from __future__ import annotations

import base64
import email.utils
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# config SSoT(Single Source of Truth) 로더 — env > config.yaml > 코드기본값.
# 파일 없음/PyYAML 미설치 시 degrade-safe(전부 env/default 동작).
try:
    from fmdw import config as _cfg  # 패키지 경로 (extract_all_via_pdf 등 호출 시)
except ImportError:
    try:
        import config as _cfg  # type: ignore  # 직접 실행 시 경로 차이 대비
    except ImportError:
        _cfg = None  # type: ignore  # 최후 fallback: 모든 knob 이 None 반환


# ──────────────────────────────────────────────────────────────────────────────
# provider 선택 + 상수 — config SSoT 경유 (import 시 1회 스냅샷)
# ──────────────────────────────────────────────────────────────────────────────

#: 추출 provider. "ollama_cloud"(기본) | "gemini"(fallback/품질비교).
EXTRACT_PROVIDER: str = (
    _cfg.knob_extract_provider() if _cfg is not None
    else os.getenv("EXTRACT_PROVIDER", "ollama_cloud").strip().lower()
)

#: Ollama 로컬 게이트웨이 OpenAI(Open AI) 호환 base URL. localhost라 Authorization 불필요.
#: 2-var fallback 체인 보존: OLLAMA_BASE_URL > OLLAMA_CLOUD_BASE_URL.
OLLAMA_BASE_URL: str = (
    _cfg.knob_ollama_base_url() if _cfg is not None
    else (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_CLOUD_BASE_URL")
        or "http://localhost:11434/v1"
    ).rstrip("/")
)

#: Ollama 경유 멀티모달(vision) 모델 — 로컬 모델. 하드 로컬 전환(2026-06-30):
#: 코드 하드 기본값을 cloud 모델에서 로컬 모델 qwen3-vl:32b 로 변경 → config/env
#: 누락 시에도 외부 cloud 로 새지 않는다.
OLLAMA_VISION_MODEL: str = (
    _cfg.knob_vision_model() if _cfg is not None
    # FIX D-R2(2026-07-09 Advisor QA): 구 기본 "qwen3-vl:32b" 삭제(404), qwen2.5vl:32b 는
    # CLIP 손상 HTTP 500. 신규 설치 qwen3-vl:8b-instruct-q8_0(실측 4.5s VISION_OK)로 교체.
    else os.getenv("OLLAMA_VISION_MODEL", "qwen3-vl:8b-instruct-q8_0")
)

#: 역할A — PDF 레이아웃/표/블록다이어그램 구조 추출 모델.
#: env FMDW_MODEL_STRUCTURE 미설정·빈값 → OLLAMA_VISION_MODEL 폴백.
MODEL_STRUCTURE: str = (
    _cfg.knob_model_structure() if _cfg is not None
    else (os.getenv("FMDW_MODEL_STRUCTURE", "").strip() or OLLAMA_VISION_MODEL)
)

#: 역할B — 이미지 상세 설명 모델.
#: env FMDW_MODEL_CAPTION 미설정·빈값 → OLLAMA_VISION_MODEL 폴백.
MODEL_CAPTION: str = (
    _cfg.knob_model_caption() if _cfg is not None
    else (os.getenv("FMDW_MODEL_CAPTION", "").strip() or OLLAMA_VISION_MODEL)
)

#: 이번 변환의 도메인 힌트(datasheet/schematic/...). convert_project 가 도메인별
#: 변환 시 subprocess env EXTRACT_DOMAIN 으로 주입한다. 빈 문자열이면 도메인 라우팅
#: 미적용(역할 모델 그대로) → 기존 동작 100% 보존. import 시 1회 스냅샷.
EXTRACT_DOMAIN: str = (
    _cfg.knob_extract_domain() if _cfg is not None
    else os.getenv("EXTRACT_DOMAIN", "").strip().lower()
)

#: 도메인(EXTRACT_DOMAIN) → 본문 전사 vision 모델. 도메인이 비어있거나 미매핑이면
#: OLLAMA_VISION_MODEL 로 폴백한다(회귀 0). 표/텍스트 도메인은 glm-ocr(고속 OCR),
#: 도면 도메인은 기본 vision 모델(qwen 계열)로 폴백되도록 config 가 결정한다.
#: ★ figure bbox 검출(detect_figures_llm)에는 절대 적용하지 않는다 — 아래
#:   _model_for_role 의 "figure_detect" 역할은 도메인 모델을 우회하고 MODEL_STRUCTURE
#:   를 강제한다(GLM-OCR 은 JSON bbox 프롬프트를 못 따를 위험 → 도면 검출은 qwen 유지).
DOMAIN_VISION_MODEL: str = (
    _cfg.knob_model_for_domain(EXTRACT_DOMAIN) if _cfg is not None
    else OLLAMA_VISION_MODEL
)


def _model_for_role(role: Optional[str]) -> str:
    """역할(role)에 따라 적용할 모델 ID를 반환한다.

    우선순위: 명시 model 인자(호출부) > **도메인 라우팅(본문 전사 한정)** > role 기반
              > 레거시 기본값(OLLAMA_VISION_MODEL).

    역할별 동작:
    - "structure" → 본문 전사(PDF 레이아웃/표/블록다이어그램 구조 추출).
                    도메인 모델이 설정돼 있으면 **도메인 모델 우선**(예: datasheet→glm-ocr).
                    미설정/폴백 시 MODEL_STRUCTURE.
    - "caption"   → 이미지 상세 설명(본문 전사 성격). 도메인 모델 우선, 미설정 시 MODEL_CAPTION.
    - "figure_detect" → figure bbox 검출(JSON 프롬프트). **도메인 모델을 우회**하고 항상
                    MODEL_STRUCTURE 사용(GLM-OCR 의 JSON 미준수 위험 회피 — 도면 검출은
                    qwen 유지). 도메인 라우팅이 본문만 바꾸고 검출은 안전하게 둔다.
    - None/기타   → OLLAMA_VISION_MODEL (기존 동작 100% 보존).

    도메인 라우팅 게이트:
        DOMAIN_VISION_MODEL 가 OLLAMA_VISION_MODEL 과 다를 때만 "도메인 모델이 명시
        설정됨"으로 간주해 본문 전사 역할에 적용한다. 같으면(=폴백 상태) role 기반
        모델을 그대로 써서 기존 동작과 byte-identical 을 유지한다.
    """
    # figure bbox 검출은 도메인 OCR 모델을 절대 쓰지 않는다(구조 모델 강제).
    if role == "figure_detect":
        return MODEL_STRUCTURE

    # 본문 전사(structure/caption)에만 도메인 라우팅 적용.
    # DOMAIN_VISION_MODEL != OLLAMA_VISION_MODEL 이면 도메인 모델이 *명시 설정*된 것.
    if role in ("structure", "caption") and DOMAIN_VISION_MODEL != OLLAMA_VISION_MODEL:
        return DOMAIN_VISION_MODEL

    if role == "structure":
        return MODEL_STRUCTURE
    if role == "caption":
        return MODEL_CAPTION
    return OLLAMA_VISION_MODEL

#: thinking 모델 대응 기본 max_tokens. 너무 작으면 thinking 단계에서 예산 소진되어
#: content가 빈 문자열로 반환됨. 2048 이상 권장.
#: 2026-05-29 강화 회로도 프롬프트(REGION INVENTORY + 핀별 PIN->NET)는 출력량이 많아
#: 4096이면 truncate 위험 → 기본 8192로 상향(env OLLAMA_CLOUD_MAX_TOKENS로 override 가능).
OLLAMA_MAX_TOKENS: int = (
    _cfg.knob_max_tokens() if _cfg is not None
    else int(os.getenv("OLLAMA_CLOUD_MAX_TOKENS", "8192"))
)

#: 단일 호출 timeout(초). cloud thinking 모델 + 다중 페이지 이미지라 넉넉히.
OLLAMA_TIMEOUT: int = (
    _cfg.knob_timeout() if _cfg is not None
    else int(os.getenv("OLLAMA_CLOUD_TIMEOUT", "600"))
)

#: PDF 페이지 렌더 해상도(DPI, Dots Per Inch). 도면/표 가독성과 페이로드 균형.
#: vision-QA 전용 DPI 는 lib/vision_qa.py 의 VISION_QA_DPI 참조.
RENDER_DPI: int = (
    _cfg.knob_render_dpi() if _cfg is not None
    else int(os.getenv("EXTRACT_RENDER_DPI", "150"))
)

#: 기존 Gemini(google.generativeai) fallback 모델 (롤백/품질비교 전용).
GEMINI_VISION_MODEL: str = (
    _cfg.knob_gemini_fallback_model() if _cfg is not None
    else os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-pro")
)

# ── 앙상블(ensemble) 모드 상수 — import 시 1회 스냅샷 (2026-06-25) ──────────────
#: 앙상블 활성 여부. False(기본)면 본문 전사도 기존 단일 _ollama_vision 경로 그대로
#: (byte-identical 회귀 0). True면 본문 전사(role='structure')만 N모델→병합 경로 사용.
ENSEMBLE_ENABLED: bool = (
    _cfg.knob_ensemble_enabled() if _cfg is not None
    else os.getenv("EXTRACT_ENSEMBLE", "").strip().lower() in ("1", "true", "on", "yes")
)

#: 앙상블에 사용할 vision 모델 리스트. 1개뿐이면 사실상 단일(병합 skip, graceful).
ENSEMBLE_MODELS: list[str] = (
    _cfg.knob_ensemble_models() if _cfg is not None
    else [m.strip() for m in os.getenv(
        "FMDW_ENSEMBLE_MODELS", "qwen3-vl:32b,gemma4:31b").split(",") if m.strip()]
)

#: N개 결과를 병합·교차검증할 merger 모델 ID (텍스트 전용 호출).
ENSEMBLE_MERGER: str = (
    _cfg.knob_ensemble_merger() if _cfg is not None
    else (os.getenv("FMDW_ENSEMBLE_MERGER", "").strip() or "qwen3-vl:32b")
)

# ── H-3: 재시도/백오프 상수 — config SSoT 경유 ────────────────────────────────
#: 일시 오류(429/5xx/httpx.HTTPError) 시 최대 재시도 횟수 (최초 시도 제외).
#: env OLLAMA_MAX_RETRIES 또는 config.yaml ollama_cloud.max_retries 로 override.
OLLAMA_MAX_RETRIES: int = (
    _cfg.knob_max_retries() if _cfg is not None
    else int(os.getenv("OLLAMA_MAX_RETRIES", "4"))
)

#: 지수 백오프 기저(초). 실제 대기 = base * 2^attempt + jitter.
OLLAMA_RETRY_BASE_DELAY: float = (
    _cfg.knob_retry_base_delay() if _cfg is not None
    else float(os.getenv("OLLAMA_RETRY_BASE_DELAY", "1.0"))
)

#: 계산 백오프 최대 상한(초). 지수 백오프 결과가 이 값을 넘지 않도록 클램프.
#: Retry-After 헤더 값과는 별개 — 헤더가 있으면 OLLAMA_RETRY_AFTER_CAP 로 별도 제한.
OLLAMA_RETRY_MAX_DELAY: float = (
    _cfg.knob_retry_max_delay() if _cfg is not None
    else float(os.getenv("OLLAMA_RETRY_MAX_DELAY", "60.0"))
)

#: Retry-After 헤더에서 유도된 대기 시간의 절대 상한(초).
#: 서버가 비정상적으로 큰 값(예: 99999)을 보내도 이 값으로 클램프한다.
#:   OLLAMA_RETRY_MAX_DELAY  = 지수 백오프 계산값의 상한
#:   OLLAMA_RETRY_AFTER_CAP  = 서버 Retry-After 헤더 지시값의 상한
OLLAMA_RETRY_AFTER_CAP: float = (
    _cfg.knob_retry_after_cap() if _cfg is not None
    else float(os.getenv("OLLAMA_RETRY_AFTER_CAP", "120.0"))
)

#: 재시도 대상 HTTP 상태 코드 집합 (일시 오류만 — 4xx 중 429만 포함).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# ── Fix 1: 본문 잘림(truncation) 자동복구 상수 (2026-07-02) ────────────────────
#: H-4 잘림 마커(SSoT). _ollama_vision 이 finish_reason=length 시 본문 말미에 삽입하고,
#: extract_pdf_pages 의 _maybe_recover_truncation 이 이 마커로 잘림을 감지한다.
_TRUNCATION_MARKER = "<!-- TRUNCATED: finish_reason=length -->"

#: 잘림 폴백 재추출 모델. 본문모델이 조밀 표에서 degenerate repetition 루프로
#: finish_reason=length 소진 시, 해당 chunk 를 chunk=1(단일 호출)로 재추출한다.
#: FIX D-R1(2026-07-09 Advisor QA): 구 기본 "qwen3-vl:32b" 삭제(404), qwen2.5vl:32b 는
#: CLIP blob 손상 HTTP 500 — 이 호스트 실측 동작 모델 glm-ocr 로 교체(chunk=1 단일
#: 페이지 재시도는 degenerate 루프 회피에 유효). env override 가능.
FMDW_TRUNCATION_FALLBACK_MODEL: str = (
    os.getenv("FMDW_TRUNCATION_FALLBACK_MODEL", "").strip() or "glm-ocr"
)

#: 잘림 폴백 페이지 수 상한(가드). chunk 페이지 수가 이 값을 넘으면 느린 폴백 모델을
#: 호출하지 않고 경고 로그 후 원본(잘린) 유지 → 폭주/무한 지연 방지.
try:
    FMDW_TRUNCATION_FALLBACK_MAX_PAGES: int = int(
        os.getenv("FMDW_TRUNCATION_FALLBACK_MAX_PAGES", "8")
    )
except ValueError:
    FMDW_TRUNCATION_FALLBACK_MAX_PAGES = 8

#: 잘림 폴백 페이지당 호출 timeout(초). qwen3-vl:32b 는 조밀한 페이지에서 ~4분/페이지가
#: 걸릴 수 있어(2026-07-03 실측: 3페이지 chunk 단일 호출 시 500|10m0s 로 OLLAMA_TIMEOUT
#: (기본 600s) 초과·재시도 5회 소진 후 원본 유지되는 구조적 결함 확인) 일반 OLLAMA_TIMEOUT
#: 보다 넉넉히 잡는다. 페이지 단위 재추출로 전환한 뒤에도(아래 _maybe_recover_truncation)
#: 단일 페이지가 유난히 조밀하면 초과할 수 있어 여유를 둔다.
#: 900→1800 상향(2026-07-03 재실측): LN08LPU p10(DPI300) 같은 초고밀도 표 페이지는
#: qwen3-vl:32b 단일 페이지 처리에도 600초를 넘는다 — ollama serve.log 에 동일하게
#: `500 | 10m0s`(정확히 OLLAMA_TIMEOUT=600s 시점에 클라이언트 타임아웃, 모델은 아직
#: 생성 중) 가 2건 관측되어 폴백조차 항상 실패했다. 1800초(30분) 예산이면 성공.
try:
    FMDW_TRUNCATION_FALLBACK_TIMEOUT: int = int(
        os.getenv("FMDW_TRUNCATION_FALLBACK_TIMEOUT", "1800")
    )
except ValueError:
    FMDW_TRUNCATION_FALLBACK_TIMEOUT = 1800

#: 잘림 폴백 호출의 max_tokens. thinking 모델(qwen3-vl:32b)은 기본 max_tokens(8192)로
#: 호출하면 초고밀도 표 페이지에서 thinking 토큰이 예산을 전부 소진해 최종 content 가
#: 비어 응답되고 ExtractError("Ollama 응답 content 비어있음 — thinking 모델 max_tokens
#: 부족 의심")로 실패한다(2026-07-03 실측). make_enhanced4.py 가 동일 모델에 고정해
#: 쓰는 16384 를 기본값으로 맞춘다.
try:
    FMDW_TRUNCATION_FALLBACK_MAX_TOKENS: int = int(
        os.getenv("FMDW_TRUNCATION_FALLBACK_MAX_TOKENS", "16384")
    )
except ValueError:
    FMDW_TRUNCATION_FALLBACK_MAX_TOKENS = 16384

#: degenerate 반복 감지 임계(비어있지 않은 stripped 라인 중 최다 반복 라인 횟수).
#: 실측: 정상 표=1, 루프=5 → 임계 5로 false positive(정상 표) 방지.
try:
    FMDW_TRUNCATION_REPEAT_THRESHOLD: int = int(
        os.getenv("FMDW_TRUNCATION_REPEAT_THRESHOLD", "5")
    )
except ValueError:
    FMDW_TRUNCATION_REPEAT_THRESHOLD = 5

# ── Fix 3: 한 줄 내부(intra-line) 구절 반복 감지 (2026-07-03) ──────────────────
#: 위 inter-line(라인간) 로직은 같은 "라인"이 여러 번 나오는 반복만 잡는다. 실측 사고
#: (LN08LPU_Design_Manual testpages)에서는 표 셀 안에서 잘림이 일어나 단일 라인
#: (37,252자)이 통째로 하나의 "청크"가 되고, 그 라인 *내부*에서 구절("teaching
#: evaluation" 류)이 275회 이상 반복되어 inter-line 로직으로는 감지되지 않았다.
#: 이를 잡기 위해 일정 길이 이상인 라인만 단어(whitespace) 단위 n-gram 반복을 검사한다.

#: intra-line 검사 대상 최소 라인 길이(문자수). 이보다 짧은 라인은 검사하지 않는다
#: (저비용 가드 — 정상 문서에는 이 길이를 넘는 라인이 거의 없다).
#: 실측(2026-07-03, 적대적 테스트): 한글 degenerate 반복은 동일 반복 횟수라도 영문
#: 대비 문자 수가 적어(예: "평가 교육 평가)) 교육 " × 120 ≈ 1,560자) 초기 임계 2000자
#: 가드에 미달해 감지되지 않는 사각지대가 있었다. 본 파이프라인의 주 corpus 가 한글
#: 문서이므로 실사용 위험이 커 1000으로 하향했다. 하향에 따른 오탐 비용은 낮다 —
#: 복구(_maybe_recover_truncation)는 TRUNCATED 마커가 함께 있을 때만 발동하고,
#: 폴백 재추출 실패 시에도 원본(잘린) 텍스트를 그대로 유지하는 가드가 있어 최악의
#: 경우도 "느린 재추출 1회 시도"에 그친다(본문 손상 없음).
try:
    FMDW_TRUNCATION_INTRALINE_MIN_LEN: int = int(
        os.getenv("FMDW_TRUNCATION_INTRALINE_MIN_LEN", "1000")
    )
except ValueError:
    FMDW_TRUNCATION_INTRALINE_MIN_LEN = 1000

#: intra-line n-gram 크기(연속 단어 개수). 3 이상 연속 단어가 반복되어야 "구절 반복"으로
#: 간주 — 단일 단어("1", "text")만 반복되는 경우의 오탐을 줄인다.
try:
    FMDW_TRUNCATION_INTRALINE_NGRAM: int = int(
        os.getenv("FMDW_TRUNCATION_INTRALINE_NGRAM", "3")
    )
except ValueError:
    FMDW_TRUNCATION_INTRALINE_NGRAM = 3

#: intra-line n-gram 반복 임계(같은 n-gram 이 몇 번 이상 나오면 degenerate 로 볼지).
#: 실측: 실제 사고 라인은 최다 n-gram 이 500회+ 반복 → 임계 20(보수적)으로도 충분히
#: 감지되며, 정상 dense 표(공백 없는 `<td>...</td>` 나열)는 whitespace 토큰이 거의
#: 생기지 않아 애초에 n-gram 후보가 없다(오탐 방지).
try:
    FMDW_TRUNCATION_INTRALINE_REPEAT: int = int(
        os.getenv("FMDW_TRUNCATION_INTRALINE_REPEAT", "20")
    )
except ValueError:
    FMDW_TRUNCATION_INTRALINE_REPEAT = 20

# ── Fix 2: figure 검출 role 인지 timeout/retries 캡 (2026-07-02) ───────────────
#: figure_detect 역할(detect_figures_llm)의 단일 호출 timeout(초). qwen thinking 모델이
#: hang/빈결과일 때 OLLAMA_TIMEOUT(기본 600s)×retries(4) 로 증폭되어 크롭 0 이 되는 것을
#: 차단하기 위해 짧게 캡한다. 이 role 은 재시도도 0 회로 강제(아래 _ollama_vision).
try:
    FMDW_FIGURE_DETECT_TIMEOUT: int = int(
        os.getenv("FMDW_FIGURE_DETECT_TIMEOUT", "90")
    )
except ValueError:
    FMDW_FIGURE_DETECT_TIMEOUT = 90

_log = logging.getLogger(__name__)


def _has_degenerate_repetition(
    text: str, threshold: Optional[int] = None
) -> bool:
    """두 단계로 degenerate 반복(무한루프성 잘림)을 감지한다.

    (A) inter-line(라인간): 비어있지 않은 stripped 라인 중 최다 반복 라인이
        threshold 이상이면 True. glm-ocr 이 조밀 표에서 같은 라인(표 헤더
        `| ... |`/구분선 `| :--- |`)을 반복 출력하는 루프를 감지한다. 정상 표는
        동일 라인 반복이 1~수회 수준이라 임계 5(기본)로 false positive 를 방지한다.

    (B) intra-line(라인내): (A)는 라인 "전체"가 반복될 때만 잡히므로, 표 셀 안에서
        잘림이 발생해 하나의 거대한 단일 라인 *내부*에서 구절이 반복되는 경우
        (예: "teaching evaluation ..." 275회 반복)를 놓친다. 이를 잡기 위해
        FMDW_TRUNCATION_INTRALINE_MIN_LEN(기본 1000자, 한글 실측 반영 하향) 이상인
        라인만 대상으로, 공백 기준 토큰의 연속 n-gram(기본 3단어)이
        FMDW_TRUNCATION_INTRALINE_REPEAT
        (기본 20회) 이상 반복되면 True 로 판정한다. 공백 없이 다닥다닥 붙은 정상
        dense 표(`<td>1</td><td>1</td>...`)는 토큰화 시 거의 1개 토큰으로 뭉쳐
        n-gram 후보 자체가 생기지 않아 오탐하지 않는다.
    """
    if not text:
        return False
    if threshold is None:
        threshold = FMDW_TRUNCATION_REPEAT_THRESHOLD
    from collections import Counter

    counts: Counter[str] = Counter()
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        counts[s] += 1
    if counts and max(counts.values()) >= threshold:
        return True

    # (B) intra-line n-gram 반복 검사 — 긴 라인에만 적용(저비용 가드).
    ngram_size = FMDW_TRUNCATION_INTRALINE_NGRAM
    intraline_threshold = FMDW_TRUNCATION_INTRALINE_REPEAT
    for line in text.splitlines():
        if len(line) < FMDW_TRUNCATION_INTRALINE_MIN_LEN:
            continue
        tokens = line.split()
        if len(tokens) < ngram_size:
            continue
        gram_counts: Counter[tuple] = Counter()
        for i in range(len(tokens) - ngram_size + 1):
            gram = tuple(tokens[i : i + ngram_size])
            gram_counts[gram] += 1
            if gram_counts[gram] >= intraline_threshold and any(
                any(ch.isalpha() for ch in tok) for tok in gram
            ):
                return True
    return False


class ExtractError(RuntimeError):
    """멀티모달 추출 실패(네트워크/HTTP/provider 오류) 시 발생."""


# ──────────────────────────────────────────────────────────────────────────────
# provider 런타임 결정 — 함수 인자 우선, 없으면 모듈 env 기본값(EXTRACT_PROVIDER)
# ──────────────────────────────────────────────────────────────────────────────

#: 지원 provider 정식 집합 (미지원 값은 안전하게 ollama_cloud 기본 경로로 흡수).
#: 하드 로컬 전환(2026-06-30): 'gemini'(외부 cloud) 는 _resolve_provider 에서
#: 'ollama_cloud' 로 흡수되므로 정상 경로에서는 도달 불가(아래 가드 참조).
_KNOWN_PROVIDERS = {"ollama_cloud", "gemini"}
#: 이미 warning 을 출력한 미지원 provider (호출마다 스팸 방지 — 값당 1회).
_warned_unknown_providers: set[str] = set()
#: 이미 warning 을 출력한 cloud 모델명 (호출마다 스팸 방지 — 값당 1회).
_warned_cloud_models: set[str] = set()

#: 하드 로컬 가드(2026-06-30, 사용자 명시 지시) — cloud 모델 차단 시 치환할 로컬 모델.
#: FIX D-R2(2026-07-09 Advisor QA): 구 "qwen3-vl:32b" 삭제(404), qwen2.5vl:32b 는 CLIP
#: 손상 HTTP 500 → 실측 동작(4.5s VISION_OK) qwen3-vl:8b-instruct-q8_0 로 교체(로컬 유지).
_LOCAL_FALLBACK_MODEL = "qwen3-vl:8b-instruct-q8_0"


def _is_cloud_model(model: str) -> bool:
    """모델명이 외부 cloud 추론 경로로 간주되는지 판정 (하드 로컬 가드용).

    차단 대상(외부 cloud 추론 경로):
      - ollama cloud 변형: 태그가 'cloud'(예: deepseek-v4-pro:cloud) 또는 '-cloud'로
        끝남(예: gpt-oss:120b-cloud, qwen3-coder:480b-cloud). ollama 데몬이 외부로 프록시.
      - 'gemini-*' / 'gemini': cloud 모델명.
    로컬 모델(qwen3-vl:32b, glm-ocr, gemma4:31b 등)은 False → 그대로 통과(회귀 0).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    # ollama 모델 ref = name:tag. cloud 변형은 tag 가 'cloud' 또는 '-cloud' 로 끝난다.
    tag = m.rsplit(":", 1)[-1] if ":" in m else ""
    if tag == "cloud" or tag.endswith("-cloud"):
        return True
    if m.startswith("gemini-") or m == "gemini":
        return True
    return False


def _guard_local_model(model: str, role: Optional[str] = None) -> str:
    """cloud 모델명이면 로컬 모델로 강제 치환하고 경고 1회 (하드 로컬 가드).

    로컬 모델은 그대로 반환(회귀 0). 외부 cloud 로 새는 모델명만 _LOCAL_FALLBACK_MODEL
    로 치환한다 — ollama 데몬이 ':cloud' 모델을 외부로 프록시하는 것을 원천 차단.
    """
    if _is_cloud_model(model):
        if model not in _warned_cloud_models:
            _warned_cloud_models.add(model)
            _log.warning(
                "하드 로컬 가드: cloud 모델 %r 차단 → 로컬 모델 %r 로 치환 "
                "(외부 cloud 연결 금지).", model, _LOCAL_FALLBACK_MODEL,
            )
        return _LOCAL_FALLBACK_MODEL
    return model


def _guard_local_base_url(base_url: str) -> None:
    """base_url 이 localhost/127.0.0.1 이 아니면 ExtractError (하드 로컬 가드).

    멀티모달 추출이 외부 host 게이트웨이로 나가는 것을 원천 차단한다.
    """
    if not _is_localhost(base_url):
        raise ExtractError(
            f"하드 로컬 가드(2026-06-30): 외부 base_url 차단됨 ({base_url!r}). "
            "멀티모달 추출은 로컬 ollama(localhost:11434) 게이트웨이로만 허용됩니다."
        )


def _resolve_provider(provider: Optional[str] = None) -> str:
    """이번 호출에 적용할 provider 를 결정한다 (함수 인자 우선).

    개선(provider 런타임 override): 기존에는 공개 추출 함수들이 import 시점에 1회
    스냅샷된 모듈 상수 EXTRACT_PROVIDER 만 참조해, 런타임 스위칭이나 테스트
    monkeypatch 가 어려웠다. 본 헬퍼를 통해 호출 단위 override 를 지원한다.

    하드 로컬 가드(2026-06-30, 사용자 명시 지시): provider 'gemini'(외부 cloud)는
    차단하고 로컬 'ollama_cloud'(localhost) 경로로 흡수한다(경고 1회). 따라서 정상
    경로에서 gemini File API 코드는 도달하지 않는다 — 외부 cloud 연결 원천 차단.

    Args:
        provider: 이번 호출에만 적용할 provider 명. None 이면 모듈 기본값 EXTRACT_PROVIDER.
            대소문자/공백은 정규화(strip+lower).

    Returns:
        정규화된 provider 문자열 (gemini 는 ollama_cloud 로 흡수됨).
    """
    resolved = EXTRACT_PROVIDER if provider is None else provider.strip().lower()
    # 하드 로컬 가드: gemini(외부 cloud) → ollama_cloud(localhost) 흡수.
    if resolved == "gemini":
        if "gemini" not in _warned_unknown_providers:
            _warned_unknown_providers.add("gemini")
            _log.warning(
                "하드 로컬 가드: provider 'gemini'(외부 cloud) 차단 → 로컬 "
                "'ollama_cloud'(localhost) 경로로 처리합니다 (외부 연결 금지)."
            )
        return "ollama_cloud"
    if resolved not in _KNOWN_PROVIDERS and resolved not in _warned_unknown_providers:
        _warned_unknown_providers.add(resolved)
        _log.warning(
            "알 수 없는 provider %r — ollama_cloud 기본 경로로 처리합니다 "
            "(지원: ollama_cloud).",
            resolved,
        )
    return resolved


# ──────────────────────────────────────────────────────────────────────────────
# PDF → 페이지 PNG base64 렌더 (PyMuPDF/fitz 재사용)
# ──────────────────────────────────────────────────────────────────────────────

def render_pdf_pages_to_base64(
    pdf_path: Path,
    start: int,
    end: int,
    dpi: int = RENDER_DPI,
    doc=None,
) -> list[str]:
    """PDF의 [start, end] (1-based, inclusive) 페이지를 PNG base64 리스트로 렌더.

    Args:
        pdf_path: 원본 PDF 경로.
        start: 시작 페이지(1-based, inclusive).
        end: 끝 페이지(1-based, inclusive).
        dpi: 렌더 해상도(Dots Per Inch).
        doc: (M-5) 이미 열린 fitz Document 핸들. 주어지면 재사용하고 닫지 않는다
             (호출자 소유). None 이면 내부에서 1회 open/close(기존 동작 동일).

    Returns:
        페이지 순서대로의 base64(Base64) 인코딩 PNG(Portable Network Graphics) 문자열 리스트.

    Raises:
        ExtractError: PyMuPDF 미설치 또는 PDF 열기 실패 시.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:  # pragma: no cover
        raise ExtractError("PyMuPDF(fitz) 미설치 — pip install pymupdf") from e

    zoom = dpi / 72.0  # PDF 기본 72 DPI 기준 배율
    matrix = fitz.Matrix(zoom, zoom)
    images_b64: list[str] = []

    # M-5: 외부 핸들이 주어지면 재사용(open/close 생략), 아니면 내부에서 1회 open.
    owns_doc = doc is None
    if owns_doc:
        # 워터마크 제거(opt-in, EXTRACT_REMOVE_WATERMARK=1): 추적형 워터마크가 제거된
        # 임시 PDF 경로로 교체 후 렌더. 비활성(기본)이면 원본 경로 그대로 → 회귀 0.
        open_path = str(pdf_path)
        try:
            from . import watermark_remover as _wm
            open_path = _wm.maybe_clean_pdf(open_path)
        except Exception:  # noqa: BLE001 — 워터마크 모듈 문제로 본 변환을 깨지 않음
            open_path = str(pdf_path)
        try:
            doc = fitz.open(open_path)
        except Exception as e:  # noqa: BLE001
            raise ExtractError(f"PDF 열기 실패: {pdf_path} ({e})") from e

    try:
        total = doc.page_count
        lo = max(1, start)
        hi = min(end, total)
        for page_num in range(lo, hi + 1):
            page = doc[page_num - 1]  # fitz는 0-based
            pixmap = page.get_pixmap(matrix=matrix)
            png_bytes = pixmap.tobytes("png")
            images_b64.append(base64.b64encode(png_bytes).decode("ascii"))
    finally:
        if owns_doc:
            doc.close()  # 내부에서 연 것만 닫는다(외부 핸들은 호출자 소유).

    return images_b64


def render_image_to_base64(image_path: Path) -> tuple[str, str]:
    """단일 이미지 파일을 base64 + MIME(Multipurpose Internet Mail Extensions)로 반환.

    Returns:
        (base64 문자열, image MIME 타입) 튜플.
    """
    data = Path(image_path).read_bytes()
    suffix = Path(image_path).suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return base64.b64encode(data).decode("ascii"), mime


def count_pdf_pages(pdf_path: Path) -> int:
    """PDF 전체 페이지 수 반환."""
    try:
        import fitz  # PyMuPDF
    except ImportError as e:  # pragma: no cover
        raise ExtractError("PyMuPDF(fitz) 미설치 — pip install pymupdf") from e
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"PDF 열기 실패: {pdf_path} ({e})") from e
    try:
        return doc.page_count
    finally:
        doc.close()


# ──────────────────────────────────────────────────────────────────────────────
# Ollama Cloud (로컬 게이트웨이) vision 호출
# ──────────────────────────────────────────────────────────────────────────────

def _is_localhost(base_url: str) -> bool:
    """base_url이 로컬 데몬(localhost/127.0.0.1)인지 판정 — Authorization 헤더 생략용."""
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _ollama_vision(
    prompt: str,
    images_b64: list[str],
    image_mime: str = "image/png",
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    role: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> str:
    """Ollama 로컬 게이트웨이 OpenAI(Open AI) 호환 vision 호출 → 응답 텍스트.

    H-3 — 멱등 재시도/백오프:
        429/500/502/503/504 및 httpx.HTTPError/타임아웃에 대해 최대 OLLAMA_MAX_RETRIES회
        재시도한다. 각 재시도 전 지수 백오프(base * 2^attempt) + jitter(0~1초)로 대기하며,
        응답에 Retry-After 헤더가 있으면 그 값을 우선한다. 추출은 read-only이므로 재시도 안전.
        재시도 소진 후에도 ExtractError 로 깔끔히 실패 (무한 루프/무한 대기 없음).

    H-4 — 응답 잘림(truncation) 감지:
        finish_reason 이 'length'(또는 동등 잘림 신호)이면 경고 로그 후 잘림 마커
        <!-- TRUNCATED: finish_reason=length --> 를 본문에 삽입한다.
        잘린 본문을 완성본으로 조용히 채택하지 않는다.

    Args:
        prompt: 텍스트 지시.
        images_b64: base64(Base64) 인코딩 이미지 리스트 (OpenAI image_url data URL로 전달).
        image_mime: 이미지 MIME(Multipurpose Internet Mail Extensions) 타입.
        model: vision 모델 ID(기본 OLLAMA_VISION_MODEL).
        temperature: 샘플링 temperature.
        max_tokens: 응답 최대 토큰 (None이면 OLLAMA_MAX_TOKENS — thinking 모델 빈 응답 방지).
        timeout_s: 이번 호출에만 적용할 timeout(초) override. None(기본)이면 기존
            role 기반 eff_timeout(OLLAMA_TIMEOUT/FMDW_FIGURE_DETECT_TIMEOUT) 그대로
            사용 — 하위호환(회귀 0). 값이 주어지면 그 값으로 eff_timeout 을 덮어쓴다
            (예: 잘림 폴백 페이지별 재추출은 FMDW_TRUNCATION_FALLBACK_TIMEOUT 사용).

    Returns:
        추출된 Markdown 텍스트. truncation 시 마커 포함.

    Raises:
        ExtractError: 재시도 소진 후에도 네트워크/HTTP 오류, 또는 빈 응답 시.
    """
    content: list[dict] = [{"type": "text", "text": prompt}]
    for b64 in images_b64:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image_mime};base64,{b64}"},
            }
        )

    resolved_max_tokens = max_tokens if max_tokens is not None else OLLAMA_MAX_TOKENS
    # 하드 로컬 가드(2026-06-30): ① base_url 이 localhost 가 아니면 즉시 차단,
    # ② 최종 모델명이 cloud(:cloud/gemini-*)면 로컬 모델로 치환(경고 1회).
    _guard_local_base_url(OLLAMA_BASE_URL)
    resolved_model = _guard_local_model(model or _model_for_role(role), role)
    payload = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": resolved_max_tokens,
    }

    headers = {"Content-Type": "application/json"}
    # localhost 게이트웨이는 데몬이 device key로 자체 인증 — Authorization 불필요.
    if not _is_localhost(OLLAMA_BASE_URL):
        # 외부 base_url로 override한 경우에만 키 첨부 (Keychain SSoT(Single Source of Truth), 평문 출력 금지).
        api_key = os.getenv("OLLAMA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    url = f"{OLLAMA_BASE_URL}/chat/completions"
    last_error: Exception | None = None

    # Fix 2: figure 검출 role 은 timeout 단축 + retries=0 캡(모드 무관). qwen thinking
    # 모델 hang/빈결과가 OLLAMA_TIMEOUT(600s)×retries(4)로 증폭되어 크롭 0 되는 것 차단.
    if role == "figure_detect":
        eff_timeout = FMDW_FIGURE_DETECT_TIMEOUT
        eff_retries = 0
    elif role == "figure_describe":
        # R3(2026-07-09 Advisor QA): describe 도 fail-fast — 로컬 비전 모델이 죽어 있을 때
        # (예 qwen2.5vl:32b CLIP 손상 HTTP 500, 1.7s 즉사) 500×retries(4)×backoff 재시도
        # 폭풍을 차단한다. 실패는 호출부(_maybe_describe_figure)가 경고 1줄 + "" 로 degrade.
        eff_timeout = OLLAMA_TIMEOUT
        eff_retries = 0
    else:
        eff_timeout = OLLAMA_TIMEOUT
        eff_retries = OLLAMA_MAX_RETRIES
    # timeout_s override(선택): 호출자가 명시하면 role 기반 기본값 대신 사용.
    if timeout_s is not None:
        eff_timeout = timeout_s

    for attempt in range(eff_retries + 1):  # 최초 시도 + 최대 eff_retries 회 재시도
        try:
            with httpx.Client(timeout=eff_timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as e:
            last_error = e
            if attempt < eff_retries:
                delay = _backoff_delay(attempt, retry_after=None)
                _log.warning(
                    "Ollama 게이트웨이 네트워크 오류 (attempt %d/%d), %.1fs 후 재시도: %s",
                    attempt + 1, eff_retries + 1, delay, e,
                )
                time.sleep(delay)
                continue
            raise ExtractError(f"Ollama 게이트웨이 네트워크 오류 (재시도 소진): {e}") from e

        # H-3: 재시도 대상 HTTP 상태 코드 처리
        if resp.status_code in _RETRYABLE_STATUS and attempt < eff_retries:
            retry_after_hdr = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            delay = _backoff_delay(attempt, retry_after=retry_after_hdr)
            _log.warning(
                "Ollama HTTP %d (attempt %d/%d), %.1fs 후 재시도",
                resp.status_code, attempt + 1, eff_retries + 1, delay,
            )
            last_error = None  # HTTP 오류는 exception 아님, 상태코드로 추적
            time.sleep(delay)
            continue

        # 재시도 불가 오류 또는 재시도 소진
        if resp.status_code >= 400:
            body_tail = resp.text[-500:] if resp.text else "empty"
            raise ExtractError(
                f"Ollama 게이트웨이 HTTP {resp.status_code} (재시도 {attempt}회 후): {body_tail}"
            )

        # ── 성공 응답 처리 ────────────────────────────────────────────────────
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ExtractError("Ollama 응답에 choices 없음 (빈 응답)")

        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ExtractError(
                "Ollama 응답 content 비어있음 — thinking 모델 max_tokens 부족 의심 "
                f"(현재 max_tokens={resolved_max_tokens}, OLLAMA_CLOUD_MAX_TOKENS로 상향)"
            )

        # H-4: finish_reason 잘림 감지
        finish_reason = choice.get("finish_reason") or ""
        if finish_reason == "length":
            _log.warning(
                "Ollama 응답 잘림 감지 (finish_reason=length, max_tokens=%d). "
                "OLLAMA_CLOUD_MAX_TOKENS 상향 또는 청크 크기 축소 권장. 잘림 마커 삽입.",
                resolved_max_tokens,
            )
            text = text.strip() + "\n\n" + _TRUNCATION_MARKER
        else:
            text = text.strip()

        return text

    # 루프 정상 종료는 불가능(마지막 attempt에서 반드시 return/raise) — 방어 코드
    raise ExtractError(
        f"Ollama 재시도 루프 비정상 종료 (last_error={last_error})"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 앙상블(ensemble) vision — N모델 병렬 추출 → merger 모델 텍스트 병합/교차검증
# (2026-06-25). 본문 전사(role='structure')에만 사용. figure 검출/캡션은 단일 유지.
# ──────────────────────────────────────────────────────────────────────────────

#: 앙상블 병합(merge) 호출의 max_tokens — 두/N 본문 합산 후 통합본 작성이라 넉넉히.
ENSEMBLE_MERGE_MAX_TOKENS = 8192


def _build_merge_prompt(model_outputs: list[tuple[str, str]]) -> str:
    """N개 모델의 변환 결과를 비교·병합하도록 지시하는 텍스트 프롬프트 구성.

    같은 페이지를 서로 다른 vision 모델로 전사한 결과들을 merger 모델에게 넘겨,
    한 모델이 놓친 정보·환각을 교차검증으로 보완한 통합본을 만들게 한다(이미지 없이
    순수 텍스트 작업). 한국어 출력.

    Args:
        model_outputs: (모델ID, 그 모델의 변환 결과 텍스트) 튜플 리스트. 빈 출력은
            호출 측(_ensemble_vision)에서 이미 걸러진 비어있지 않은 결과만 들어온다.

    Returns:
        merger 모델에 전달할 병합 지시 프롬프트.
    """
    parts: list[str] = [
        "당신은 여러 비전(vision) 모델이 **같은 문서 페이지**를 각각 전사(轉寫)한 "
        "결과들을 받아, 이를 비교·교차검증하여 하나의 정확한 통합본으로 합치는 "
        "전문 편집자입니다.",
        "",
        f"아래에 {len(model_outputs)}개 모델의 변환 결과가 있습니다. 각 모델은 같은 "
        "페이지를 보았지만, 어떤 모델은 더 상세하고 어떤 모델은 특정 항목(예: 부품값, "
        "주파수, 핀 이름, 표의 일부 셀)을 더 정확히 잡았을 수 있습니다. 한 모델에만 "
        "있는 정보는 누락이 아니라 **보완**일 수 있고, 서로 다르게 적힌 값은 **불일치**"
        "(둘 중 하나가 환각이거나 오독)일 수 있습니다.",
        "",
    ]
    for idx, (model_id, text) in enumerate(model_outputs, start=1):
        parts.append(f"========== [결과 {idx}] 모델: {model_id} ==========")
        parts.append(text)
        parts.append("")

    parts.extend([
        "========== 지시 ==========",
        "위 결과들을 비교하여 **한국어로** 다음 두 부분을 작성하세요.",
        "",
        "## ① 통합본",
        "- 어느 한 결과에라도 등장한 정보는 **빠짐없이** 합쳐 하나의 완성된 Markdown "
        "본문으로 작성합니다(표·리스트·블록다이어그램 설명 등 원본 구조 보존).",
        "- 서로 일치하는 내용은 한 번만 적되, 더 상세하고 구체적인 표현을 채택합니다.",
        "- 값이 충돌하는 항목은 통합본에서 더 신뢰할 만한 값을 택하고, 어느 쪽인지 "
        "애매하면 둘 다 병기(예: `16MHz(결과1) / 16.0MHz(결과2)`)합니다.",
        "",
        "## ② 상호 보완·불일치",
        "- **A에만 있음**: 특정 결과에만 등장한 정보 항목을 모델명과 함께 나열.",
        "- **불일치**: 같은 항목인데 결과마다 값이 다른 것을 모델명과 각 값으로 나열.",
        "- 보완·불일치가 전혀 없으면 '없음'이라고 적습니다.",
        "",
        "환각을 만들지 말고, 위 결과들에 실제로 등장한 정보만 사용하세요. "
        "통합본(①)이 이 페이지의 최종 본문이 되므로 완전성을 최우선으로 합니다.",
    ])
    return "\n".join(parts)


def _ensemble_vision(
    prompt: str,
    images_b64: list[str],
    image_mime: str = "image/png",
    role: Optional[str] = None,
) -> str:
    """앙상블 본문 전사 — ENSEMBLE_MODELS 각 모델로 추출 후 MERGER 로 병합.

    데이터 흐름:
        1) ENSEMBLE_MODELS 의 각 모델로 _ollama_vision(prompt, images_b64, model=M)
           을 호출해 N개 변환 결과를 수집한다(이미지 = 실제 페이지).
        2) 빈 출력(0자: thinking 토큰 부족 등) 또는 호출 실패 모델은 제외한다.
        3) 살아남은 결과가
             - 0개 → 마지막 에러로 ExtractError(전부 실패).
             - 1개 → 그 결과를 그대로 반환(병합 skip — 단일 모델/모델1개 graceful).
             - 2개+ → MERGER 모델에 **이미지 없이**(images_b64=[]) 텍스트 병합 프롬프트로
                      _ollama_vision 을 호출해 통합본을 만들어 반환.

    회귀/안전:
        - 본 함수는 ENSEMBLE_ENABLED 일 때 extract_pdf_pages(role='structure')에서만
          호출된다. 비활성 경로는 기존 _ollama_vision 그대로(byte-identical).
        - 병합은 텍스트 작업이므로 images 빈 배열, max_tokens 는 넉넉히
          (ENSEMBLE_MERGE_MAX_TOKENS=8192).

    Args:
        prompt: 페이지 범위 지시가 포함된 추출 프롬프트(모든 모델에 동일 전달).
        images_b64: 페이지 PNG base64 리스트(각 vision 모델에 전달, 병합엔 미전달).
        image_mime: 이미지 MIME 타입.
        role: 역할 힌트(로깅/일관성용 — 모델은 ENSEMBLE_MODELS 가 우선).

    Returns:
        통합된 Markdown 텍스트.

    Raises:
        ExtractError: 모든 앙상블 모델이 실패/빈 출력일 때.
    """
    models = [m for m in ENSEMBLE_MODELS if m and m.strip()]
    if not models:
        # 설정이 비정상(빈 리스트) — 단일 기본 경로로 graceful 폴백(회귀 안전).
        _log.warning("앙상블 모델 리스트가 비어 단일 경로로 폴백합니다.")
        return _ollama_vision(prompt, images_b64, image_mime=image_mime, role=role)

    model_outputs: list[tuple[str, str]] = []
    last_error: Exception | None = None
    for model_id in models:
        try:
            text = _ollama_vision(
                prompt, images_b64, image_mime=image_mime, model=model_id, role=role
            )
        except ExtractError as e:
            # 개별 모델 실패는 치명적이지 않음 — 제외하고 나머지로 병합 시도.
            last_error = e
            _log.warning("앙상블 모델 %r 추출 실패 — 제외하고 계속: %s", model_id, e)
            continue
        if not text or not text.strip():
            _log.warning("앙상블 모델 %r 빈 출력 — 제외합니다.", model_id)
            continue
        model_outputs.append((model_id, text.strip()))

    if not model_outputs:
        raise ExtractError(
            f"앙상블 전 모델 실패/빈 출력 (models={models}, last_error={last_error})"
        )

    if len(model_outputs) == 1:
        # 결과가 1개뿐 — 병합할 대상이 없으므로 그대로 반환(graceful single).
        only_model, only_text = model_outputs[0]
        _log.info(
            "앙상블 유효 결과 1개(%s) — 병합 생략하고 단일 결과 반환.", only_model
        )
        return only_text

    # 2개+ → merger 모델로 텍스트 병합(이미지 없이).
    merge_prompt = _build_merge_prompt(model_outputs)
    used_models = ", ".join(m for m, _ in model_outputs)
    _log.info(
        "앙상블 병합: %d개 결과(%s) → merger %s",
        len(model_outputs), used_models, ENSEMBLE_MERGER,
    )
    return _ollama_vision(
        merge_prompt,
        images_b64=[],
        image_mime=image_mime,
        model=ENSEMBLE_MERGER,
        max_tokens=ENSEMBLE_MERGE_MAX_TOKENS,
        role=role,
    )


def _backoff_delay(attempt: int, retry_after: Optional[str]) -> float:
    """지수 백오프 + jitter 대기 시간(초) 계산.

    #1 — Retry-After 과도 대기 방지:
        헤더에서 유도된 대기 시간은 OLLAMA_RETRY_AFTER_CAP 으로 클램프한다.
        서버가 비정상적으로 큰 값(예: 99999)을 보내도 cap 이상 대기하지 않는다.

    #2 — Retry-After HTTP-date 형식 지원 (RFC 7231):
        "Wed, 21 Oct 2015 07:28:00 GMT" 형식을 email.utils.parsedate_to_datetime 으로 파싱,
        현재 UTC 시각과의 delta(초)를 계산한다. 음수 delta 는 0 으로 처리.
        파싱 실패 시 계산 백오프로 무해하게 fallback.

    Args:
        attempt: 현재 재시도 횟수(0-based, 최초 시도 후 첫 재시도 = 0).
        retry_after: Retry-After 헤더 값 문자열(없으면 None).
            정수/float 초 단위 또는 RFC 7231 HTTP-date 형식 모두 수용.

    Returns:
        대기 시간(초).
        - 계산 백오프: OLLAMA_RETRY_MAX_DELAY 상한.
        - Retry-After 헤더 유도값: OLLAMA_RETRY_AFTER_CAP 상한.
        - 최종값 = max(계산 백오프, 헤더 유도값).
    """
    # 지수 백오프: base * 2^attempt + jitter(0~1초 uniform)
    exp_delay = OLLAMA_RETRY_BASE_DELAY * (2 ** attempt)
    jitter = random.uniform(0.0, 1.0)
    calc_delay = min(exp_delay + jitter, OLLAMA_RETRY_MAX_DELAY)

    if retry_after is None:
        return calc_delay

    # Retry-After 헤더 파싱 — 초 단위 정수/float 먼저, 실패 시 HTTP-date 시도
    header_seconds: Optional[float] = None
    try:
        header_seconds = float(retry_after)
    except (ValueError, TypeError):
        # #2: RFC 7231 HTTP-date 형식 파싱
        try:
            dt = email.utils.parsedate_to_datetime(retry_after)
            # UTC 기준 delta 계산 (Python 3.11+: datetime.UTC, 하위 호환: timezone.utc)
            import datetime as _dt
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            # parsedate_to_datetime 결과가 naive 이면 UTC 로 간주
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            delta_secs = (dt - now_utc).total_seconds()
            header_seconds = max(0.0, delta_secs)  # 음수 → 0
        except Exception:  # noqa: BLE001
            pass  # 파싱 완전 실패 → 계산 백오프 그대로

    if header_seconds is None:
        return calc_delay

    # #1: 헤더 유도값에 절대 상한(cap) 적용 후 계산 백오프와 합산
    capped_header = min(header_seconds, OLLAMA_RETRY_AFTER_CAP)
    return max(calc_delay, capped_header)


# ──────────────────────────────────────────────────────────────────────────────
# Gemini (google.generativeai) fallback 호출 — 롤백/품질비교 전용 (지연 import)
# ──────────────────────────────────────────────────────────────────────────────

def _gemini_model(model_name: Optional[str] = None):
    """Gemini(google.generativeai) GenerativeModel 핸들을 생성한다 (중복 제거 헬퍼).

    리팩토링: 기존에 _gemini_pdf_pages / _gemini_image / extract_text_prompt 의 gemini
    경로가 각각 동일한 ① 지연 import ② GEMINI_API_KEY(Keychain SSoT) 존재 검사
    ③ genai.configure(api_key=...) ④ GenerativeModel(model_name=...) 4단계를 반복했다.
    본 헬퍼로 묶어 중복을 제거한다. 동작·결과는 불변(순수 리팩토링) — 키는 env(Keychain
    주입) 경유 그대로 사용하고 평문 노출하지 않는다.

    Args:
        model_name: 사용할 Gemini 모델 ID. None 이면 GEMINI_VISION_MODEL 기본값.

    Returns:
        (genai 모듈, GenerativeModel 인스턴스) 튜플.
        genai 모듈도 함께 반환하는 이유: 호출부가 upload_file/delete_file 등 모듈 레벨
        API 를 동일 import 핸들로 재사용하기 위함(중복 import 방지).

    Raises:
        ExtractError: google.generativeai 미설치 또는 GEMINI_API_KEY 미설정 시.
    """
    # 하드 로컬 가드(2026-06-30, 사용자 명시 지시) — defense-in-depth.
    # 정상 경로에서는 _resolve_provider 가 gemini 를 ollama_cloud 로 흡수하므로 여기
    # 도달하지 않지만, 어떤 코드가 직접 호출하더라도 외부 cloud(Gemini File API)로
    # 나가지 못하도록 진입부에서 즉시 차단한다.
    raise ExtractError(
        "하드 로컬 가드(2026-06-30): Gemini(외부 cloud) 경로는 비활성화되었습니다. "
        "모든 멀티모달 추출은 로컬 ollama(localhost) 모델로만 동작합니다."
    )
    try:
        import google.generativeai as genai  # 지연 import
    except ImportError as e:  # pragma: no cover
        raise ExtractError(
            "google.generativeai 미설치 — gemini fallback 사용 시 설치 필요"
        ) from e

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ExtractError(
            "GEMINI_API_KEY 미설정 — gemini fallback 사용 시 "
            "source $HOME/workspace/_shared/keychain-env.sh 필요"
        )
    genai.configure(api_key=api_key)
    gmodel = genai.GenerativeModel(model_name=model_name or GEMINI_VISION_MODEL)
    return genai, gmodel


def _gemini_response_text(response) -> str:
    """Gemini generate_content 응답에서 텍스트를 안전 추출(L-8).

    google.generativeai 의 `response.text` 는 편의 accessor 로, 후보 part 가 텍스트가
    아니거나(멀티파트/함수콜) 안전필터로 차단(finish_reason=SAFETY)되면 **ValueError**를
    던진다. 가공 없이 `.text` 를 반환하면 이 ValueError 가 호출 스택 위로 그대로 새어
    파이프라인이 비정상 종료된다. 여기서 ExtractError 로 감싸 일관된 실패 계약으로 만든다
    (호출부의 기존 ExtractError degrade 경로 재사용 — 추출은 read-only 라 안전).

    Returns:
        응답 텍스트(str).

    Raises:
        ExtractError: `.text` 추출 불가(안전차단/멀티파트/빈 응답) 시.
    """
    try:
        text = response.text
    except ValueError as e:
        # 차단 사유를 가능하면 진단에 포함(키/원문 노출 없이 메타만).
        reason = ""
        try:
            feedback = getattr(response, "prompt_feedback", None)
            if feedback is not None:
                reason = f" (prompt_feedback={feedback})"
        except Exception:  # noqa: BLE001
            pass
        raise ExtractError(
            f"Gemini 응답에서 텍스트 추출 불가 — 안전차단/멀티파트 의심{reason}"
        ) from e
    if not isinstance(text, str) or not text.strip():
        raise ExtractError("Gemini 응답 텍스트 비어있음")
    return text


def _gemini_pdf_pages(prompt: str, pdf_path: Path, model: Optional[str] = None) -> str:
    """기존 Gemini File API 경로 — PDF 전체 업로드 + 프롬프트 페이지 범위 지시.

    품질 비교/롤백 대비 보존. EXTRACT_PROVIDER=gemini 일 때만 사용.
    GEMINI_API_KEY(macOS Keychain SSoT, 셸 주입) 필요.
    """
    genai, gmodel = _gemini_model(model)  # 공통 헬퍼(중복 제거)
    doc_file = None
    try:
        doc_file = genai.upload_file(str(pdf_path), mime_type="application/pdf")
        response = gmodel.generate_content([doc_file, prompt])
        return _gemini_response_text(response)  # L-8: .text ValueError 가드
    finally:
        if doc_file is not None:
            try:
                genai.delete_file(doc_file.name)
            except Exception:  # noqa: BLE001
                pass


def _gemini_image(prompt: str, image_path: Path, model: Optional[str] = None) -> str:
    """기존 Gemini File API 경로 — 단일 이미지 업로드 + 프롬프트."""
    genai, gmodel = _gemini_model(model)  # 공통 헬퍼(중복 제거)
    suffix = Path(image_path).suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    img_file = None
    try:
        img_file = genai.upload_file(str(image_path), mime_type=mime)
        response = gmodel.generate_content([img_file, prompt])
        return _gemini_response_text(response)  # L-8: .text ValueError 가드
    finally:
        if img_file is not None:
            try:
                genai.delete_file(img_file.name)
            except Exception:  # noqa: BLE001
                pass


# ──────────────────────────────────────────────────────────────────────────────
# 공개 API — provider 추상화 진입점
# ──────────────────────────────────────────────────────────────────────────────

def provider_label(provider: Optional[str] = None) -> str:
    """선택된 provider + 모델을 사람이 읽을 수 있는 라벨로 반환 (로그용).

    Args:
        provider: 라벨링할 provider override. None 이면 모듈 기본값(EXTRACT_PROVIDER).
    """
    resolved = _resolve_provider(provider)
    if resolved == "gemini":
        return f"gemini({GEMINI_VISION_MODEL}, File API)"
    return f"ollama_cloud({OLLAMA_VISION_MODEL}, localhost gateway)"


def extract_pdf_pages(
    prompt: str,
    pdf_path: Path,
    start: int,
    end: int,
    doc=None,
    provider: Optional[str] = None,
    role: str = "structure",
    model: Optional[str] = None,
) -> str:
    """PDF의 [start, end] 페이지를 현재 provider로 멀티모달 추출 → Markdown 텍스트.

    - ollama_cloud(기본): fitz로 페이지 범위를 PNG 래스터화 → base64 → Ollama vision.
    - gemini(fallback): PDF 전체 업로드 + 프롬프트 페이지 범위 지시 (기존 동작).

    프롬프트는 호출 측이 페이지 범위 지시("{start}~{end}페이지")를 포함하여 전달하므로,
    provider와 무관하게 동일 프롬프트로 일관된 출력 계약을 유지한다.

    Args:
        prompt: 페이지 범위 지시가 포함된 추출 프롬프트.
        pdf_path: 원본 PDF 경로.
        start: 시작 페이지(1-based, inclusive).
        end: 끝 페이지(1-based, inclusive).
        doc: (M-5) 이미 열린 fitz Document 핸들(재사용). None 이면 내부 open
             (기존 동작). gemini 경로에서는 무시(PDF 전체 업로드라 핸들 불필요).
        provider: 이번 호출에만 적용할 provider override("ollama_cloud"|"gemini").
             None(기본)이면 모듈 기본값 EXTRACT_PROVIDER 사용 → 기존 동작 100% 보존.
        role: 모델 역할 힌트 ("structure"=PDF 레이아웃/표 구조 추출, 기본값).
             FMDW_MODEL_STRUCTURE 미설정 시 OLLAMA_VISION_MODEL 폴백 → 회귀 없음.
        model: (하이브리드 전사, 2026-07-04) 이번 호출에만 강제할 vision 모델 ID.
             None(기본)이면 기존 라우팅(도메인/role 기반 _model_for_role) 그대로 →
             회귀 0. 명시하면 도메인 라우팅·앙상블(ENSEMBLE_ENABLED)을 모두 우회하고
             단일 _ollama_vision 호출을 이 모델로 강제한다(예: 본문 하이브리드 전사가
             1차=glm-ocr, 폴백=qwen3-vl:32b 를 페이지별로 명시 지정할 때 사용).

    Returns:
        추출된 Markdown 텍스트.
    """
    pdf_path = Path(pdf_path)
    if _resolve_provider(provider) == "gemini":
        return _gemini_pdf_pages(prompt, pdf_path)

    images_b64 = render_pdf_pages_to_base64(pdf_path, start, end, doc=doc)
    if not images_b64:
        raise ExtractError(
            f"렌더된 페이지 없음: {pdf_path.name} pages {start}~{end} "
            "(범위가 PDF 페이지 수를 벗어났을 수 있음)"
        )
    # 앙상블(opt-in): 본문 전사(role='structure')에서 ENSEMBLE_ENABLED 일 때만 N모델→병합.
    # model 명시 호출(하이브리드 전용)은 특정 모델을 강제하려는 의도이므로 앙상블을
    # 우회하고 단일 _ollama_vision 으로 그 모델을 그대로 사용한다.
    # 그 외(비활성/다른 role/model 미지정)는 기존 단일 _ollama_vision 그대로
    # → byte-identical(회귀 0).
    if model is None and ENSEMBLE_ENABLED and role == "structure":
        text = _ensemble_vision(prompt, images_b64, image_mime="image/png", role=role)
    else:
        text = _ollama_vision(
            prompt, images_b64, image_mime="image/png", role=role, model=model,
        )
    # Fix 1: 본문 잘림 자동복구 — TRUNCATED 마커 AND degenerate 반복 둘 다일 때만 발동.
    return _maybe_recover_truncation(text, prompt, images_b64, role, start, end)


def _maybe_recover_truncation(
    text: str,
    prompt: str,
    images_b64: list[str],
    role: str,
    start: int,
    end: int,
) -> str:
    """본문 잘림 자동복구 (Fix 1, 2026-07-02 / Fix 4 페이지 단위 재추출, 2026-07-03).

    트리거(둘 다 참): (A) H-4 잘림 마커 존재(finish_reason=length) AND
    (B) degenerate 반복 감지(_has_degenerate_repetition). 두 조건이 모두 참일 때만
    폴백 모델(FMDW_TRUNCATION_FALLBACK_MODEL, 기본 glm-ocr — R1 2026-07-09)로 재추출한다.

    Fix 4(2026-07-03, 실사고): 잘린 chunk 전체를 **단일 vision 호출**로 재추출하는
    구(旧) 방식은 조밀한 다중 페이지에서 항상 실패했다 — qwen3-vl:32b 는 페이지당
    ~4분이 걸릴 수 있어 3페이지 chunk 단일 호출이 12분+ 소요되며 OLLAMA_TIMEOUT
    (기본 600s)을 초과, 재시도 5회를 전부 소진하고 원본(잘린) 텍스트를 유지하는
    구조적 결함이 있었다(실측 로그: 500|10m0s × 2회). 이를 **페이지 단위 재추출**로
    교체한다: images_b64 가 페이지당 1장씩 [start,end] 순서로 대응한다는 전제 하에
    (render_pdf_pages_to_base64 로 만들어지므로 정상 경로에서는 항상 성립 — 아래
    가드 참조), 각 페이지 이미지를 개별 vision 호출로 재추출한 뒤 페이지 순서대로
    "\\n\\n" 로 이어붙여 chunk 전체 교체본을 만든다.

    주의(과거 docstring 정정): "페이지 단위 스플라이싱은 하지 않는다"는 *잘린 본문에
    페이지 조각을 끼워넣는(구분자 없이 짜깁기하는) 것*을 금지한 것이었다. 지금 하는
    "페이지 단위 재추출 + chunk 전체 교체"는 다르다 — 모든 페이지가 처음부터 새로
    온전히 전사되고, 원본(잘린) 텍스트는 통째로 버려진다(일부만 재사용하지 않음).
    따라서 안전하다.

    가드:
      - chunk 페이지 수가 FMDW_TRUNCATION_FALLBACK_MAX_PAGES 초과 시 폴백 없이 원본 유지.
      - images_b64 개수가 페이지 수(end-start+1)와 다르면 1:1 매핑을 신뢰할 수 없으므로
        레거시 chunk 단위 단일 호출 경로로 폴백(안전한 열화, 기존 동작과 동일).
      - 페이지 하나라도 재추출 실패(ExtractError)면 즉시 복구를 포기하고 원본 유지
        (부분 성공 페이지가 있어도 짜깁기하지 않음 — 무결성 우선).
      - 이어붙인 최종 결과가 여전히 잘림 마커 포함 AND degenerate 반복이면 원본 유지
        (무한 지연/폭주 방지).

    Returns:
        복구된 텍스트(성공 시) 또는 원본 text(비발동/실패/가드 초과 시).
    """
    if _TRUNCATION_MARKER not in text:
        return text
    if not _has_degenerate_repetition(text):
        # 잘림 마커는 있으나 반복 아님 → 정상적으로 긴 본문일 수 있어 폴백 생략(FP 방지).
        return text

    page_count = end - start + 1
    if page_count > FMDW_TRUNCATION_FALLBACK_MAX_PAGES:
        _log.warning(
            "잘림+반복 감지(pages %d~%d) 이나 페이지 수 %d > 상한 %d → 폴백 생략, 원본(잘린) 유지",
            start, end, page_count, FMDW_TRUNCATION_FALLBACK_MAX_PAGES,
        )
        return text

    t0 = time.time()

    # images_b64 ↔ 페이지 1:1 매핑 가드. render_pdf_pages_to_base64 는 항상 페이지
    # 순서대로 1장씩 렌더하므로 정상 경로에서는 len(images_b64) == page_count 가 성립.
    # 불일치 시(예: 다른 소스로 이미지가 합성된 chunk) 매핑을 신뢰하지 않고 레거시
    # chunk 단위 단일 호출로 안전하게 폴백한다.
    if len(images_b64) == page_count:
        _log.warning(
            "잘림+반복 감지(pages %d~%d) → 폴백 모델 %s 로 페이지 단위 재추출 시작",
            start, end, FMDW_TRUNCATION_FALLBACK_MODEL,
        )
        return _recover_truncation_per_page(
            text, prompt, images_b64, role, start, end, t0,
        )

    _log.warning(
        "잘림+반복 감지(pages %d~%d) 이나 images_b64 개수(%d) != 페이지 수(%d) → "
        "1:1 매핑 불확실, 레거시 chunk 단위 단일 호출로 폴백 모델 %s 재추출 시작",
        start, end, len(images_b64), page_count, FMDW_TRUNCATION_FALLBACK_MODEL,
    )
    return _recover_truncation_whole_chunk(
        text, prompt, images_b64, role, start, end, t0,
    )


def _recover_truncation_whole_chunk(
    text: str,
    prompt: str,
    images_b64: list[str],
    role: str,
    start: int,
    end: int,
    t0: float,
) -> str:
    """레거시 경로: chunk 전체를 단일 vision 호출로 재추출.

    images_b64 개수가 페이지 수와 달라 1:1 매핑을 신뢰할 수 없을 때만 호출된다
    (정상 경로는 _recover_truncation_per_page 사용). 조밀한 다중 페이지 chunk 에서는
    OLLAMA_TIMEOUT(FMDW_TRUNCATION_FALLBACK_TIMEOUT)을 초과해 실패할 수 있다.
    """
    try:
        recovered = _ollama_vision(
            prompt, images_b64, image_mime="image/png",
            model=FMDW_TRUNCATION_FALLBACK_MODEL, role=role,
            timeout_s=FMDW_TRUNCATION_FALLBACK_TIMEOUT,
            max_tokens=FMDW_TRUNCATION_FALLBACK_MAX_TOKENS,
        )
    except ExtractError as e:
        _log.warning(
            "잘림 폴백(레거시 chunk 단위) 재추출 실패(%.1fs 경과) → 원본(잘린) 유지: %s",
            time.time() - t0, e,
        )
        return text

    elapsed = time.time() - t0
    if _TRUNCATION_MARKER in recovered and _has_degenerate_repetition(recovered):
        _log.warning(
            "잘림 폴백(레거시) 결과도 잘림+반복(pages %d~%d, %.1fs) → 원본 유지",
            start, end, elapsed,
        )
        return text

    _log.info(
        "잘림 폴백(레거시 chunk 단위) 성공(pages %d~%d, %.1fs, model=%s): %d→%d chars",
        start, end, elapsed, FMDW_TRUNCATION_FALLBACK_MODEL, len(text), len(recovered),
    )
    return recovered


def _recover_truncation_per_page(
    text: str,
    prompt: str,
    images_b64: list[str],
    role: str,
    start: int,
    end: int,
    t0: float,
) -> str:
    """페이지 단위 재추출 → chunk 전체 교체(Fix 4, 2026-07-03).

    images_b64[i] 는 페이지 (start + i) 에 대응한다는 전제(호출측 가드로 보장)로,
    각 페이지를 개별 vision 호출로 재추출한 뒤 페이지 순서대로 "\\n\\n" 로 이어붙여
    chunk 전체 교체본을 만든다. 원본(잘린) 텍스트는 성공 시 통째로 버려진다.
    """
    page_texts: list[str] = []
    for i, img in enumerate(images_b64):
        page_no = start + i
        p0 = time.time()
        try:
            page_text = _ollama_vision(
                prompt, [img], image_mime="image/png",
                model=FMDW_TRUNCATION_FALLBACK_MODEL, role=role,
                timeout_s=FMDW_TRUNCATION_FALLBACK_TIMEOUT,
                max_tokens=FMDW_TRUNCATION_FALLBACK_MAX_TOKENS,
            )
        except ExtractError as e:
            _log.warning(
                "잘림 폴백(페이지 단위) page %d 재추출 실패(%.1fs, 누적 %.1fs) → "
                "복구 포기, 원본(잘린) 유지: %s",
                page_no, time.time() - p0, time.time() - t0, e,
            )
            return text

        _log.info(
            "잘림 폴백(페이지 단위) page %d 완료 (%.1fs, 누적 %.1fs)",
            page_no, time.time() - p0, time.time() - t0,
        )
        page_texts.append(page_text)

    recovered = "\n\n".join(page_texts)
    elapsed = time.time() - t0
    if _TRUNCATION_MARKER in recovered and _has_degenerate_repetition(recovered):
        _log.warning(
            "잘림 폴백(페이지 단위) 이어붙임 결과도 잘림+반복(pages %d~%d, %.1fs) → 원본 유지",
            start, end, elapsed,
        )
        return text

    _log.info(
        "잘림 폴백(페이지 단위) 성공(pages %d~%d, %.1fs, model=%s): %d→%d chars",
        start, end, elapsed, FMDW_TRUNCATION_FALLBACK_MODEL, len(text), len(recovered),
    )
    return recovered


def extract_pdf_single_page(
    prompt: str, pdf_path: Path, page: int, provider: Optional[str] = None
) -> str:
    """PDF 단일 페이지를 현재 provider로 추출 (reextract_figures 용).

    Args:
        provider: 이번 호출에만 적용할 provider override. None 이면 모듈 기본값.
    """
    return extract_pdf_pages(prompt, pdf_path, page, page, provider=provider)


def extract_image(
    prompt: str,
    image_path: Path,
    provider: Optional[str] = None,
    role: str = "caption",
) -> str:
    """단일 이미지 파일을 현재 provider로 멀티모달 추출 → Markdown 텍스트.

    - ollama_cloud(기본): 이미지를 base64로 읽어 Ollama vision 호출.
    - gemini(fallback): 이미지를 File API 업로드 + 프롬프트.

    Args:
        provider: 이번 호출에만 적용할 provider override("ollama_cloud"|"gemini").
             None(기본)이면 모듈 기본값 EXTRACT_PROVIDER 사용 → 기존 동작 100% 보존.
        role: 모델 역할 힌트 ("caption"=이미지 상세 설명, 기본값).
             FMDW_MODEL_CAPTION 미설정 시 OLLAMA_VISION_MODEL 폴백 → 회귀 없음.
    """
    image_path = Path(image_path)
    if _resolve_provider(provider) == "gemini":
        return _gemini_image(prompt, image_path)

    b64, mime = render_image_to_base64(image_path)
    return _ollama_vision(prompt, [b64], image_mime=mime, role=role)


def extract_text_prompt(prompt: str, provider: Optional[str] = None) -> str:
    """순수 텍스트 프롬프트(이미지 없음) 생성 → 응답 텍스트.

    docx 텍스트/표를 Markdown으로 정형화하는 등 비-멀티모달 정형화에 사용.
    - ollama_cloud: 이미지 없이 chat/completions 텍스트 호출.
    - gemini(fallback): generate_content(prompt) 텍스트 호출.

    Args:
        provider: 이번 호출에만 적용할 provider override("ollama_cloud"|"gemini").
             None(기본)이면 모듈 기본값 EXTRACT_PROVIDER 사용 → 기존 동작 100% 보존.
    """
    if _resolve_provider(provider) == "gemini":
        # 공통 헬퍼(_gemini_model)로 import/configure/모델생성 중복 제거.
        _genai, gmodel = _gemini_model()
        return _gemini_response_text(gmodel.generate_content(prompt))  # L-8 가드

    # Ollama 텍스트 경로 (이미지 없는 vision 호출 = 일반 chat completion)
    return _ollama_vision(prompt, images_b64=[], image_mime="image/png")


# ──────────────────────────────────────────────────────────────────────────────
# 자가 점검 (python -m lib.ollama_extractor 또는 직접 실행)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[ollama_extractor] provider={provider_label()}", flush=True)
    print(f"[ollama_extractor] base_url={OLLAMA_BASE_URL}", flush=True)
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{OLLAMA_BASE_URL}/models")
        ok = r.status_code < 400
        print(f"[ollama_extractor] /models HTTP {r.status_code} ({'OK' if ok else 'FAIL'})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[ollama_extractor] 게이트웨이 접근 실패: {e}", flush=True)
        sys.exit(1)
