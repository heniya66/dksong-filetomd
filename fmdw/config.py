"""filestomdwgem 설정 SSoT(Single Source of Truth) 로더 — Option C(하이브리드).

우선순위: **env > config.yaml > 코드기본값** (엄수).

설계 원칙:
  1. import 시 1회 config.yaml safe_load. 파일 없음/파싱 실패/PyYAML 미설치는
     빈 dict 로 degrade — 파이프라인이 절대 죽지 않는다.
  2. env 는 get() 호출 시점에 읽는다(테스트에서 monkeypatch 가능).
  3. 각 모듈의 모듈-레벨 상수는 기존처럼 import 시 1회 스냅샷.
  4. 시크릿(OLLAMA_API_KEY/GEMINI_API_KEY)은 이 모듈을 절대 경유하지 않는다.
     호출 측이 os.getenv() 직접 사용 — Keychain SSoT 보존.
  5. bool any-nonempty 의미 보존: VISION_QA_KEEP_PNG 등 "비어있지 않으면 truthy"
     ('0'/'false'도 참) 변수는 cast=cfg.bool_nonempty 로 명시.

KNOBS 레지스트리:
  각 knob 은 (env_var, yaml_path, default, cast, post) 5-tuple 로 정의.
  - env_var : 환경변수 이름 또는 이름 리스트(fallback 체인, 순서대로).
  - yaml_path: config.yaml 내 점(.) 구분 경로.
  - default  : env/yaml 모두 없을 때 반환할 코드기본값.
  - cast     : 변환 함수(str/int/float/bool_nonempty). None → str.
  - post     : 변환 후 적용할 1-arg 함수(예: str.strip). None → no-op.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional, Union

# ──────────────────────────────────────────────────────────────────────────────
# config.yaml 로드 (import 시 1회, degrade-safe)
# ──────────────────────────────────────────────────────────────────────────────

# config.yaml 위치 결정 (우선순위):
#   1) 환경변수 FMDW_CONFIG 가 지정되면 그 경로를 사용 (외부 프로젝트에서
#      라이브러리로 import 했을 때 자기 config.yaml 을 주입하기 위한 override).
#   2) 미지정 시 기존 동작 유지(하위호환): 이 파일(fmdw/config.py)의
#      부모(fmdw/) 의 부모 = 워크스페이스 루트의 config.yaml.
# 어느 경우든 파일 없음/파싱 실패는 아래 try/except 에서 degrade-safe 처리됨.
_FMDW_CONFIG_ENV = os.getenv("FMDW_CONFIG")
if _FMDW_CONFIG_ENV:
    _CONFIG_PATH = Path(_FMDW_CONFIG_ENV).expanduser()
else:
    _CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_raw: dict = {}
_load_error: Optional[str] = None

try:
    import yaml as _yaml  # PyYAML

    with open(_CONFIG_PATH, encoding="utf-8") as _f:
        _loaded = _yaml.safe_load(_f)
    if isinstance(_loaded, dict):
        _raw = _loaded
    else:
        _load_error = f"config.yaml 파싱 결과가 dict 가 아님: {type(_loaded)}"
except FileNotFoundError:
    _load_error = f"config.yaml 없음 ({_CONFIG_PATH}) — 코드기본값/env 로 동작"
except ImportError:
    _load_error = "PyYAML 미설치 — 코드기본값/env 로 동작 (pip install PyYAML)"
except Exception as _e:  # noqa: BLE001
    _load_error = f"config.yaml 파싱 실패: {_e} — 코드기본값/env 로 동작"


def _yaml_get(path: str) -> Any:
    """점(.) 구분 경로로 _raw 에서 값 추출. 없으면 None."""
    parts = path.split(".")
    node: Any = _raw
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


# ──────────────────────────────────────────────────────────────────────────────
# cast 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def bool_nonempty(v: Any) -> bool:
    """any-nonempty 의미의 bool 변환.

    '0', 'false', 'no' 등 **비어있지 않은 문자열은 모두 truthy**.
    이는 기존 extract_all_via_pdf 의 VISION_QA_KEEP_PNG / KEEP_VOTES 의미를 보존한다.
    표준 bool() 또는 'true'/'false' 파싱 적용 금지.
    """
    if isinstance(v, bool):
        return v
    return bool(str(v).strip())


def _truthy(v: Any) -> bool:
    """표준 bool 파싱('0'/'false'/'no'/'off'/''→False, 그 외 truthy).

    bool_nonempty(any-nonempty) 와 의미가 다르다 — resume on/off 처럼
    사용자가 EXTRACT_RESUME=0 으로 *끌 수 있어야* 하는 플래그 전용.
    """
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("", "0", "false", "no", "off", "none"):
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# 핵심 get() — env > yaml > default
# ──────────────────────────────────────────────────────────────────────────────

def get(
    yaml_path: str,
    default: Any,
    cast: Optional[Callable] = None,
    env_var: Union[str, list[str], None] = None,
    post: Optional[Callable] = None,
) -> Any:
    """env > config.yaml > default 순으로 값 조회.

    Args:
        yaml_path : config.yaml 내 점 구분 경로 (예: "options.max_tokens").
        default   : env/yaml 모두 없을 때 반환할 기본값.
        cast      : 변환 함수 (str/int/float/bool_nonempty 등). None → default 타입 유지.
        env_var   : 환경변수 이름 또는 체인 리스트 (순서대로 첫 번째 비어있지 않은 값 사용).
                    None → yaml_path 로부터 자동 도출하지 않음(env skip).
        post      : cast 후 적용할 1-arg 변환 (예: lambda s: s.rstrip('/') ).

    Returns:
        최종 값. cast/post 적용 완료.
    """
    raw_value: Any = None
    source = "default"

    # 1. env (호출 시점 읽기 — monkeypatch 가능)
    if env_var is not None:
        vars_to_try = [env_var] if isinstance(env_var, str) else list(env_var)
        for ev in vars_to_try:
            v = os.getenv(ev)
            if v is not None:  # 빈 문자열("")도 env 에 명시된 것이므로 채택
                raw_value = v
                source = f"env:{ev}"
                break

    # 2. config.yaml
    if raw_value is None:
        yaml_val = _yaml_get(yaml_path)
        if yaml_val is not None:
            raw_value = yaml_val
            source = f"yaml:{yaml_path}"

    # 3. 코드기본값
    if raw_value is None:
        raw_value = default
        # source = "default"  # 이미 설정됨

    # cast 적용
    if cast is not None and raw_value is not None:
        try:
            raw_value = cast(raw_value)
        except (ValueError, TypeError):
            raw_value = default  # cast 실패 시 default 로 fallback

    # post 처리
    if post is not None and raw_value is not None:
        raw_value = post(raw_value)

    return raw_value


# ──────────────────────────────────────────────────────────────────────────────
# 타입별 편의 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def get_int(yaml_path: str, default: int, env_var: Union[str, list[str], None] = None) -> int:
    return get(yaml_path, default, cast=int, env_var=env_var)


def get_float(yaml_path: str, default: float, env_var: Union[str, list[str], None] = None) -> float:
    return get(yaml_path, default, cast=float, env_var=env_var)


def get_str(
    yaml_path: str,
    default: str,
    env_var: Union[str, list[str], None] = None,
    post: Optional[Callable] = None,
) -> str:
    return get(yaml_path, default, cast=str, env_var=env_var, post=post)


def get_bool_nonempty(
    yaml_path: str,
    default: bool,
    env_var: Union[str, list[str], None] = None,
) -> bool:
    """any-nonempty 의미 bool 조회 (VISION_QA_KEEP_PNG 계열)."""
    return get(yaml_path, default, cast=bool_nonempty, env_var=env_var)


# ──────────────────────────────────────────────────────────────────────────────
# KNOBS 레지스트리 — 각 모듈의 상수가 이 함수들을 호출해 스냅샷
# ──────────────────────────────────────────────────────────────────────────────

def knob_extract_provider() -> str:
    """추출 provider: ollama_cloud(기본) | gemini."""
    return get(
        "options.provider", "ollama_cloud",
        cast=str, env_var="EXTRACT_PROVIDER",
        post=lambda s: s.strip().lower(),
    )


def knob_ollama_base_url() -> str:
    """Ollama 로컬 게이트웨이 base URL (2-var fallback 체인 보존)."""
    return get(
        "ollama_cloud.base_url", "http://localhost:11434/v1",
        cast=str,
        env_var=["OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL"],
        post=lambda s: s.rstrip("/"),
    )


def knob_vision_model() -> str:
    """Ollama vision 모델 ID (로컬 모델). 하드 로컬 전환(2026-06-30): config 누락 시에도
    cloud 모델로 새지 않도록 코드 기본값을 로컬 모델로 고정.
    FIX D-R2(2026-07-09 Advisor QA): 구 기본 "qwen3-vl:32b" 삭제(404), qwen2.5vl:32b 는
    CLIP blob 손상 HTTP 500(비전·텍스트 모두 불능 실측) → 신규 설치·실측 동작(4.5s
    VISION_OK) qwen3-vl:8b-instruct-q8_0 로 교체(env OLLAMA_VISION_MODEL override 유지)."""
    return get_str("options.model", "qwen3-vl:8b-instruct-q8_0", env_var="OLLAMA_VISION_MODEL")


def knob_model_structure() -> str:
    """역할A — PDF 레이아웃/표/블록다이어그램 구조 추출 모델.

    env FMDW_MODEL_STRUCTURE 미설정 시 knob_vision_model() 폴백.
    빈값 처리: env가 설정됐어도 빈 문자열이면 폴백(기존 단일 모델 동작 보존).
    """
    v = os.getenv("FMDW_MODEL_STRUCTURE", "").strip()
    return v if v else knob_vision_model()


def knob_model_caption() -> str:
    """역할B — 이미지 상세 설명 모델.

    env FMDW_MODEL_CAPTION 미설정 시 knob_vision_model() 폴백.
    빈값 처리: env가 설정됐어도 빈 문자열이면 폴백(기존 단일 모델 동작 보존).
    """
    v = os.getenv("FMDW_MODEL_CAPTION", "").strip()
    return v if v else knob_vision_model()


# ──────────────────────────────────────────────────────────────────────────────
# 도메인(domain) → 모델 라우팅 — 역할(role) 라우팅 위에 얹는 상위 선택 (2026-06-25)
# ──────────────────────────────────────────────────────────────────────────────
#
# 배경(원격 M4 Max 실측): 문서 형태별로 최적 vision 모델이 다르다.
#   - 표·텍스트(datasheet 특성표, 일반 텍스트) → glm-ocr(0.9B): 8~9초 정확 OCR.
#     같은 표에 qwen3-vl(32B)은 258초(32배 느림) — OCR엔 OCR 특화 모델이 압도적.
#   - 회로도·도면·핀배치 그림 → qwen3-vl:32b 만 그림을 "해석"(축/핀/구조). glm-ocr
#     등 OCR 모델은 그림을 못 읽고 텍스트만 추출 → 도면 손실.
# 따라서 convert_project 가 아는 도메인(datasheet/schematic/...) 별로 본문 전사
# 모델을 자동 선택한다. ★단, GLM-OCR 은 OCR 특화라 figure bbox 검출용 JSON 프롬프트
# (detect_figures_llm)를 제대로 못 따를 위험이 있어, **도면 검출은 항상 structure
# 모델(qwen) 유지**하고 도메인 라우팅은 '본문 전사' 경로에만 적용한다(역할 분리).
#
# 우선순위(모델 결정): per-domain env(FMDW_DOMAIN_MODEL_<DOMAIN>) > yaml(domain_models.<domain>)
#                       > 코드 기본 매핑 > knob_vision_model()(폴백, 회귀 0).
# 미지정/미매핑 도메인은 항상 knob_vision_model() 로 폴백 → 신규 설정 미적용 시 기존 동작 동일.

#: 도메인 코드 기본 매핑 (env/yaml 미설정 시 적용). 빈 문자열 = "기본 모델 사용"(폴백).
#: schematic/design_doc 는 도면 해석이 필요 → 기존 vision 모델(qwen 계열) 유지 위해
#: 빈 문자열로 두지 않고 명시값을 둘 수도 있으나, 회귀 0 보장을 위해 *기본 모델 폴백*을
#: 택한다(아래 _DOMAIN_MODEL_DEFAULTS 의 값이 빈 문자열이면 knob_vision_model() 사용).
#: 즉 "datasheet/reference 는 glm-ocr 로 전환, schematic/design_doc/source_code 는
#:  기존 모델 그대로(빈 문자열)" — 사용자 권장 매핑을 회귀 안전하게 표현.
_DOMAIN_MODEL_DEFAULTS: dict[str, str] = {
    # 기본 qwen 단일; env로 도메인별 모델 켤 수 있음 (예: FMDW_DOMAIN_MODEL_DATASHEET=glm-ocr)
    "datasheet":   "",   # qwen 단일 기본; env FMDW_DOMAIN_MODEL_DATASHEET=glm-ocr 로 opt-in
    "reference":   "",   # 〃 env FMDW_DOMAIN_MODEL_REFERENCE=glm-ocr 로 opt-in
    "schematic":   "",   # 도면 해석 필요 → 기본 vision 모델(qwen 계열) 폴백
    "design_doc":  "",   # 〃
    "source_code": "",   # 텍스트 래핑 수집이라 vision 무관 → 기본 폴백
}


def _domain_env_var(domain: str) -> str:
    """도메인명 → per-domain env 변수명. 예: 'datasheet' → 'FMDW_DOMAIN_MODEL_DATASHEET'.

    영숫자 외 문자는 '_' 로 정규화(예: 'design_doc' → 'DESIGN_DOC' 유지).
    """
    norm = "".join(c if c.isalnum() else "_" for c in domain).upper()
    return f"FMDW_DOMAIN_MODEL_{norm}"


def knob_extract_domain() -> str:
    """이번 변환의 도메인 힌트 (env EXTRACT_DOMAIN). 없으면 빈 문자열.

    convert_project 가 도메인별 변환 시 subprocess env 로 주입한다. 비어 있으면
    도메인 라우팅 미적용 → 역할(role) 모델 그대로 사용(기존 동작 100% 보존).
    대소문자/공백 정규화(strip+lower)하여 매핑 키와 일치시킨다.
    """
    return get_str("options.extract_domain", "", env_var="EXTRACT_DOMAIN",
                   post=lambda s: s.strip().lower())


def knob_model_for_domain(domain: Optional[str]) -> str:
    """도메인 → 본문 전사 vision 모델 ID 결정.

    우선순위:
        1. per-domain env  FMDW_DOMAIN_MODEL_<DOMAIN>  (비어있지 않은 값)
        2. yaml            domain_models.<domain>       (비어있지 않은 값)
        3. 코드 기본 매핑  _DOMAIN_MODEL_DEFAULTS        (비어있지 않은 값)
        4. 폴백            knob_vision_model()           (위가 모두 빈 값/미매핑)

    domain 이 None/빈 문자열이거나 매핑 결과가 빈 문자열이면 항상 knob_vision_model()
    로 폴백한다 → 신규 설정 미적용 시 기존 단일 모델 동작과 byte-identical(회귀 0).

    Args:
        domain: 도메인명(datasheet/schematic/source_code/design_doc/reference 등).
                대소문자 무관(내부에서 strip+lower 정규화).

    Returns:
        선택된 모델 ID(항상 비어있지 않은 문자열).
    """
    fallback = knob_vision_model()
    if not domain:
        return fallback
    key = domain.strip().lower()
    if not key:
        return fallback

    # 1. per-domain env (env > 모든 것)
    env_val = os.getenv(_domain_env_var(key), "").strip()
    if env_val:
        return env_val

    # 2. yaml domain_models.<domain>
    yaml_val = _yaml_get(f"domain_models.{key}")
    if isinstance(yaml_val, str) and yaml_val.strip():
        return yaml_val.strip()

    # 3. 코드 기본 매핑 (빈 문자열이면 폴백으로 떨어짐)
    default_val = _DOMAIN_MODEL_DEFAULTS.get(key, "")
    if default_val.strip():
        return default_val.strip()

    # 4. 폴백 — 기존 단일 vision 모델 (회귀 0)
    return fallback


# ──────────────────────────────────────────────────────────────────────────────
# 앙상블(ensemble) 모드 — N개 vision 모델 병렬 추출 후 1개 merger 모델로 병합/교차검증
# (2026-06-25 신설). 비용이 크므로(~4배) 중요 문서에만 켜는 opt-in 옵션.
# 본문 전사(role='structure')에만 적용, figure 검출/캡션은 단일 유지(비용·복잡도 관리).
#
# 우선순위: env > config.yaml(ensemble.*) > 코드기본값. 미설정 시 비활성 → 회귀 0.
# ──────────────────────────────────────────────────────────────────────────────

#: 앙상블 모델 코드 기본 리스트 (env/yaml 미설정 시). qwen3-vl(상세) + gemma4(보완).
_ENSEMBLE_MODELS_DEFAULT = ["qwen3-vl:32b", "gemma4:31b"]


def knob_ensemble_enabled() -> bool:
    """앙상블 모드 활성 여부 (기본 False — 미설정 시 기존 단일 동작과 byte-identical).

    env EXTRACT_ENSEMBLE 또는 yaml ensemble.enabled. '1'/'true'/'on'/'yes' 등이면
    활성(표준 bool 파싱 — '0'/'false'/'off'/'' 은 비활성으로 끌 수 있어야 함).
    """
    raw = get("ensemble.enabled", False, cast=None, env_var="EXTRACT_ENSEMBLE")
    return _truthy(raw)


def knob_ensemble_models() -> list[str]:
    """앙상블에 사용할 vision 모델 리스트 (콤마 구분 파싱).

    우선순위: env FMDW_ENSEMBLE_MODELS > yaml ensemble.models > 코드기본값
             (["qwen3-vl:32b", "gemma4:31b"]).
    파싱: 콤마로 분리 후 각 항목 strip, 빈 값 제거. 결과가 비면 코드기본값으로 폴백.

    yaml 의 ensemble.models 는 list 또는 콤마 구분 str 모두 수용한다.
    """
    # 1. env (콤마 구분 문자열)
    env_raw = os.getenv("FMDW_ENSEMBLE_MODELS")
    if env_raw is not None:
        models = _parse_model_list(env_raw)
        if models:
            return models

    # 2. yaml (list 또는 콤마 구분 str)
    yaml_val = _yaml_get("ensemble.models")
    if isinstance(yaml_val, list):
        models = [str(m).strip() for m in yaml_val if str(m).strip()]
        if models:
            return models
    elif isinstance(yaml_val, str):
        models = _parse_model_list(yaml_val)
        if models:
            return models

    # 3. 코드기본값
    return list(_ENSEMBLE_MODELS_DEFAULT)


def _parse_model_list(raw: str) -> list[str]:
    """콤마 구분 모델 문자열 → strip+빈값제거 리스트."""
    return [m.strip() for m in raw.split(",") if m.strip()]


def knob_ensemble_merger() -> str:
    """앙상블 N개 결과를 병합·교차검증할 merger 모델 ID.

    우선순위: env FMDW_ENSEMBLE_MERGER > yaml ensemble.merger > 코드기본값
             (qwen3-vl:32b). 빈값이면 코드기본값 폴백.
    """
    return get_str("ensemble.merger", "qwen3-vl:32b", env_var="FMDW_ENSEMBLE_MERGER",
                   post=lambda s: s.strip() or "qwen3-vl:32b")


def knob_gemini_fallback_model() -> str:
    """Gemini fallback 모델 (롤백/품질비교 전용)."""
    return get_str("options.gemini_fallback_model", "gemini-2.5-pro", env_var="GEMINI_VISION_MODEL")


def knob_max_tokens() -> int:
    """Ollama 응답 최대 토큰 (thinking 모델 빈 응답 방지)."""
    return get_int("options.max_tokens", 8192, env_var="OLLAMA_CLOUD_MAX_TOKENS")


def knob_timeout() -> int:
    """단일 호출 timeout(초)."""
    return get_int("ollama_cloud.timeout", 600, env_var="OLLAMA_CLOUD_TIMEOUT")


def knob_render_dpi() -> int:
    """추출 경로 페이지 PNG 렌더 해상도(DPI). vision-QA 전용은 knob_vision_qa_dpi()."""
    return get_int("options.render_dpi", 150, env_var="EXTRACT_RENDER_DPI")


def knob_vision_qa_dpi() -> int:
    """vision-QA 경로 페이지 PNG 렌더 해상도(DPI).

    중첩 fallback 보존: VISION_QA_DPI > EXTRACT_RENDER_DPI > yaml > 220.
    추출 경로(render_dpi=150)와 별도 유지.
    """
    return get_int(
        "options.vision_qa_dpi", 220,
        env_var=["VISION_QA_DPI", "EXTRACT_RENDER_DPI"],
    )


def knob_chunk_size() -> int:
    """PDF 청크 크기(페이지 수). env EXTRACT_CHUNK_SIZE(신설) 또는 yaml.

    기본=5: chunk가 크면(20/8) 응답 토큰 한계로 후반 페이지가 에러·마커 없이 조용히
    잘려 유실되는 silent truncation 발생(2026-06-10 실측: 8도 이미지 고밀도 문서에서 발생).
    무결성 우선으로 안전한 5를 기본값으로 둔다(필요 시 env/yaml로 상향).
    """
    return get_int("options.chunk_size", 5, env_var="EXTRACT_CHUNK_SIZE")


def knob_rate_limit_s() -> float:
    """청크 간 sleep 초수(rate-limit). env EXTRACT_RATE_LIMIT_S 또는 yaml.

    pdf_pipeline.convert_pdf 의 청크 간 대기 기본값. 호출자(per-document 스크립트)가
    rate_limit_s 를 명시하면 그 값이 우선하고, 미지정 시 이 knob 으로 결정한다.
    코드기본값 10.0 은 기존 convert_pdf 시그니처 기본값 및 원본 스크립트 동작과 동일.
    extract_all_via_pdf 의 vision-QA 경로(M-6 _RateLimiter, VISION_QA_RATE_DELAY)는
    별개 concern 이며 이 knob 과 독립적으로 동작한다.
    """
    return get_float("options.rate_limit_s", 10.0, env_var="EXTRACT_RATE_LIMIT_S")


def knob_resume_enabled() -> bool:
    """대용량 PDF resume(중단 후 이어받기) 캐시 활성 여부.

    기본 ON(안전 — off 시 기존 동작 100% 동일). env EXTRACT_RESUME 또는 yaml.
    '0'/'false'/'no'/'off' 은 명시적 비활성으로 해석(any-nonempty 의미가 아님).
    resume 는 동작에 영향 없는 순수 가속/내성 기능이라 표준 bool 파싱을 쓴다.
    """
    raw = get(
        "options.resume", True,
        cast=None, env_var="EXTRACT_RESUME",
    )
    return _truthy(raw)


def knob_resume_keep_cache() -> bool:
    """전체 변환 성공 시 resume 캐시를 보존할지 여부.

    기본 False — 성공하면 캐시 디렉토리 정리(디스크 절약). env EXTRACT_RESUME_KEEP.
    '1'/'true'/'yes'/'on' 이면 보존(재현/디버깅용).
    """
    raw = get(
        "options.resume_keep_cache", False,
        cast=None, env_var="EXTRACT_RESUME_KEEP",
    )
    return _truthy(raw)


def knob_max_retries() -> int:
    """Ollama 재시도 최대 횟수."""
    return get_int("ollama_cloud.max_retries", 4, env_var="OLLAMA_MAX_RETRIES")


def knob_retry_base_delay() -> float:
    """지수 백오프 기저(초)."""
    return get_float("ollama_cloud.retry_base_delay", 1.0, env_var="OLLAMA_RETRY_BASE_DELAY")


def knob_retry_max_delay() -> float:
    """계산 백오프 최대 상한(초)."""
    return get_float("ollama_cloud.retry_max_delay", 60.0, env_var="OLLAMA_RETRY_MAX_DELAY")


def knob_retry_after_cap() -> float:
    """Retry-After 헤더 유도값 절대 상한(초)."""
    return get_float("ollama_cloud.retry_after_cap", 120.0, env_var="OLLAMA_RETRY_AFTER_CAP")


# ──────────────────────────────────────────────────────────────────────────────
# 진단 헬퍼 (디버깅/운영)
# ──────────────────────────────────────────────────────────────────────────────

def load_status() -> str:
    """config.yaml 로드 상태 반환 (로그/진단용)."""
    if _load_error:
        return f"[config] WARN: {_load_error}"
    return f"[config] OK: {_CONFIG_PATH} 로드 완료 (keys={list(_raw.keys())})"
