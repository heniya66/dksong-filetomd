"""test_provider_reachability.py — 개선 3: 실제 의존 reachability 통합 테스트.

목적
----
단위 테스트(mock)는 Ollama 로컬 데몬이 죽어 있어도 통과하므로 "실제 연결"은
점검되지 않는다. 본 통합 테스트는 실제 의존(Ollama 게이트웨이·선택적으로 1페이지
추출 스모크)이 실제로 도달 가능한지 확인한다.

기본 동작 / CI 안전성
---------------------
- 환경변수 RUN_INTEGRATION 이 설정되지 않으면 **모듈 전체를 skip** 한다.
  → 기본 `pytest tests/` 실행과 CI 는 자동으로 통합 테스트를 건너뛴다(회귀 0).
- RUN_INTEGRATION 설정 시에도, 의존(데몬·키)이 없으면 **항목별 graceful skip**.
  (실패가 아니라 skip — 환경 미비를 회귀로 오인하지 않기 위함.)

실행 예:
    RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v

시크릿 정책
-----------
- API 키 값을 로그/assert 메시지에 절대 출력하지 않는다(존재 여부만 사용).
- 로컬 게이트웨이(localhost)는 데몬이 device key 로 자체 인증하므로 키 불필요.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# tests/integration → 워크스페이스 루트는 2단계 상위.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 모듈 전체 게이트: RUN_INTEGRATION 미설정 시 수집 단계에서 전부 skip.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("RUN_INTEGRATION"),
        reason="RUN_INTEGRATION 미설정 — 통합(실제 의존) 테스트 skip. "
               "실행: RUN_INTEGRATION=1 pytest tests/integration",
    ),
]

from fmdw import ollama_extractor as ox  # noqa: E402


def _gateway_reachable(timeout: float = 5.0) -> bool:
    """Ollama 로컬 게이트웨이(localhost:11434) /models 도달 가능성 점검.

    네트워크/연결 실패는 예외 없이 False 로 흡수(게이트웨이 다운 = 미도달).
    키 값은 일절 다루지 않는다.
    """
    import httpx

    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{ox.OLLAMA_BASE_URL}/models")
        return r.status_code < 400
    except Exception:  # noqa: BLE001 — 연결 실패 = 미도달
        return False


def _find_smoke_pdf() -> Path | None:
    """1페이지 추출 스모크용 작은 PDF 를 탐색. 없으면 None(→ graceful skip).

    우선순위: 환경변수 INTEGRATION_SMOKE_PDF > input/ 하위 첫 PDF.
    input/ 은 테스트가 생성/수정하지 않고 읽기만 한다(존재 시).
    """
    env_pdf = os.getenv("INTEGRATION_SMOKE_PDF")
    if env_pdf:
        p = Path(env_pdf)
        return p if p.is_file() else None

    input_dir = _ROOT / "input"
    if input_dir.is_dir():
        for cand in sorted(input_dir.rglob("*.pdf")):
            if cand.is_file():
                return cand
    return None


def test_ollama_daemon_reachable():
    """(a) Ollama 로컬 데몬(localhost:11434) reachability.

    도달 가능하면 통과. 데몬 다운이면 graceful skip(실패 아님 — 환경 미비).
    """
    if not _gateway_reachable():
        pytest.skip(
            f"Ollama 게이트웨이 미도달({ox.OLLAMA_BASE_URL}). "
            "'ollama serve' / 'ollama signin' 후 재실행."
        )
    # 도달 가능 = 정상. base_url 형태도 최소 sanity 점검(키 미노출).
    assert ox.OLLAMA_BASE_URL.startswith(("http://", "https://"))


def test_models_endpoint_returns_list():
    """(a-2) /models 엔드포인트가 200 + JSON 본문(키 미노출) 반환."""
    if not _gateway_reachable():
        pytest.skip(f"Ollama 게이트웨이 미도달({ox.OLLAMA_BASE_URL}).")

    import httpx

    with httpx.Client(timeout=10) as c:
        r = c.get(f"{ox.OLLAMA_BASE_URL}/models")
    assert r.status_code < 400
    # JSON 파싱만 확인(모델 목록 스키마는 게이트웨이 버전별 상이 → 느슨히).
    body = r.json()
    assert isinstance(body, (dict, list))


def test_real_one_page_extraction_smoke():
    """(b) 실제 1페이지 추출 스모크 — 게이트웨이 + 테스트 PDF 둘 다 있을 때만.

    의존 미비 시 graceful skip:
      - 게이트웨이 다운 → skip
      - 스모크용 PDF 미발견 → skip
    실제 추출이 일어나면 비어있지 않은 Markdown 문자열이어야 한다.
    네트워크 호출이므로 ollama_cloud 경로를 명시 override 하여 격리.
    """
    if not _gateway_reachable():
        pytest.skip(f"Ollama 게이트웨이 미도달({ox.OLLAMA_BASE_URL}).")

    pdf = _find_smoke_pdf()
    if pdf is None:
        pytest.skip(
            "스모크용 PDF 미발견 — INTEGRATION_SMOKE_PDF 설정 또는 input/ 하위 PDF 필요."
        )

    prompt = (
        "이 페이지(1페이지)를 간단히 Markdown 으로 요약하라. "
        "표/도면이 있으면 핵심만 1~2문장으로."
    )
    # 개선 1의 provider override 를 활용해 이번 호출을 ollama_cloud 로 격리.
    md = ox.extract_pdf_pages(prompt, pdf, 1, 1, provider="ollama_cloud")
    assert isinstance(md, str)
    assert md.strip(), "실제 추출 결과 Markdown 이 비어있으면 안 됨"


def test_gemini_key_presence_optional_smoke():
    """(b-2) Gemini fallback 키가 있을 때만 모델 핸들 생성 스모크.

    키 미설정(localhost 운영 기본) → graceful skip. 키 값은 출력하지 않는다.
    실제 추론 호출(과금/네트워크)은 하지 않고, 클라이언트 구성까지만 점검.
    """
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY 미설정 — gemini fallback 스모크 skip(키 미노출).")

    try:
        import google.generativeai  # noqa: F401
    except ImportError:
        pytest.skip("google.generativeai 미설치 — gemini fallback 스모크 skip.")

    # configure + GenerativeModel 구성까지만(추론 호출 없음 = 과금 없음).
    genai, gmodel = ox._gemini_model()
    assert gmodel is not None
    assert genai is not None
