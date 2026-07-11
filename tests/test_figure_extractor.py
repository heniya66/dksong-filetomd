"""test_figure_extractor.py — Task 1 회귀 가드 (figure_extractor 운영 모듈).

설계 문서 §6.1 사이드카 스키마 + 캡션 엄격 필터 + opt-in 통합을 검증한다.

테스트 분류
-----------
1. 실제 LLM(Large Language Model) 검출 테스트 (gateway 필요):
   - test_strict_caption_only_accepts_figure_n
   - test_sidecar_schema_and_file_match
   Ollama Cloud 멀티모달 게이트웨이(localhost:11434)가 떠 있어야 한다.
   게이트웨이 다운 시 pytest.skip (mock 으로 우회하지 않음 — 캡션 필터 정확성은
   실제 검출 결과로 검증해야 한다는 Task 제약).

2. LLM 비의존 단위 테스트 (항상 실행):
   - test_synthetic_sidecar_schema_keys / 스키마 키·파일 일치 (합성 PDF, LLM monkeypatch)
   - test_strict_caption_filter_excludes_non_figure (캡션 필터 순수 단위)
   - test_figure_id_format / 정렬 / 경로 규약
   - test_optin_off_does_not_enter_figure_path (동작 보존)
   - test_optin_on_creates_sidecar_and_figures_dir (opt-in on 연결, LLM monkeypatch)

실행:
    .venv/bin/python -m pytest tests/test_figure_extractor.py -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fmdw import figure_extractor as fx  # noqa: E402
from fmdw.figure_extractor import extract_figures  # noqa: E402

# PROJECT-STRUCTURE-STANDARD 마이그레이션 후 경로: 01_raw/datasheets → 01_Hardware/datasheet.
PDF = Path(
    "/Users/heni/workspace/04_NX/01_Hardware/datasheet/NGULTRA/"
    "Application_Notes_v0_0_1-SoC AXI test.pdf"
)

# 설계 §6.1 사이드카 항목 필수 키 (다운스트림 codesign-rag ingest 계약).
_REQUIRED_KEYS = {
    "figure_id", "page", "image_path", "caption",
    "figure_no", "type", "bbox", "source", "snap_iou",
}


def _gateway_up() -> bool:
    """Ollama Cloud 로컬 게이트웨이 가용성 점검 (실제 LLM 검출 테스트 게이트)."""
    import httpx

    from fmdw import ollama_extractor as ox

    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{ox.OLLAMA_BASE_URL}/models")
        return r.status_code < 400
    except Exception:  # noqa: BLE001 — 연결 실패 = 게이트웨이 다운
        return False


_GATEWAY = _gateway_up()
_needs_gateway = pytest.mark.skipif(
    not _GATEWAY,
    reason="Ollama Cloud 게이트웨이(localhost:11434) 다운 — 실제 LLM 검출 테스트 skip "
           "(mock 금지: 캡션 필터 정확성은 실제 검출로만 검증). 'ollama signin' 후 재실행.",
)
# 실제 워크스페이스 datasheet에 의존하는 테스트 — 자료 부재 환경(CI 등)에서는 graceful skip.
_needs_pdf = pytest.mark.skipif(
    not PDF.exists(),
    reason=f"테스트 fixture PDF 부재: {PDF} — 실제 워크스페이스 자료 의존 테스트 skip.",
)


# ──────────────────────────────────────────────────────────────────────────────
# 1) 실제 LLM 검출 테스트 (gateway 필요) — Task Step 1 명시 단언
# ──────────────────────────────────────────────────────────────────────────────

@_needs_gateway
@_needs_pdf
def test_strict_caption_only_accepts_figure_n(tmp_path):
    figs = extract_figures(PDF, tmp_path, strict_caption=True)
    assert figs, "Figure 다이어그램이 검출되어야 함"
    assert all(f["figure_no"].lower().startswith("figure") for f in figs)
    # 로고 페이지(p01)는 figure로 채택되지 않음
    assert not any(f["page"] == 1 for f in figs)
    # 본문 Figure 1/2/3(아키텍처/AXI/FSM) 캡션 포함
    captions = " ".join(f["caption"] for f in figs).lower()
    assert "architecture" in captions and "fsm" in captions


@_needs_gateway
@_needs_pdf
def test_sidecar_schema_and_file_match(tmp_path):
    """반환 각 항목 스키마 + out_dir/<stem>_figures.json 파일이 반환과 일치."""
    figs = extract_figures(PDF, tmp_path, strict_caption=True)
    assert figs, "Figure 다이어그램이 검출되어야 함"

    # 각 항목 필수 키 존재.
    for f in figs:
        assert _REQUIRED_KEYS.issubset(f.keys()), (
            f"누락 키: {_REQUIRED_KEYS - set(f.keys())}"
        )
        assert isinstance(f["bbox"], list) and len(f["bbox"]) == 4
        assert f["figure_no"].lower().startswith("figure")
        # image_path 는 figures/<stem>__pNN_figK.png 상대형 + 실제 파일 존재.
        assert f["image_path"].startswith("figures/")
        assert (tmp_path / f["image_path"]).exists(), f"크롭 PNG 없음: {f['image_path']}"

    # 사이드카 파일이 생성되고 반환 리스트와 내용 일치.
    sidecar = tmp_path / f"{PDF.stem}_figures.json"
    assert sidecar.exists(), "사이드카 _figures.json 생성되어야 함"
    on_disk = json.loads(sidecar.read_text(encoding="utf-8"))
    assert on_disk == figs, "사이드카 내용 == 반환 리스트"


# ──────────────────────────────────────────────────────────────────────────────
# 2) LLM 비의존 단위 테스트 (항상 실행)
# ──────────────────────────────────────────────────────────────────────────────

def _make_synthetic_pdf(path: Path) -> None:
    """3페이지 합성 PDF: p1=로고(캡션 없음), p2='Figure 1' 캡션, p3='Figure 2' 캡션.

    figure_extractor 의 캡션/정렬/사이드카 기계적 동작을 LLM 없이 검증하기 위한 픽스처.
    """
    import fitz

    doc = fitz.open()
    # p1: 캡션 없는 페이지(로고 가정).
    p1 = doc.new_page(width=400, height=600)
    p1.insert_text((50, 50), "ACME Corp Logo Page")
    # p2: 'Figure 1' 캡션 + 본문.
    p2 = doc.new_page(width=400, height=600)
    p2.draw_rect(fitz.Rect(60, 80, 340, 300))
    p2.insert_text((70, 320), "Figure 1: System Architecture")
    # p3: 'Figure 2' 캡션 두 개(다중 figure y정렬 검증용).
    p3 = doc.new_page(width=400, height=600)
    p3.draw_rect(fitz.Rect(60, 60, 340, 200))
    p3.insert_text((70, 215), "Figure 2: AXI FSM")
    p3.draw_rect(fitz.Rect(60, 320, 340, 480))
    p3.insert_text((70, 495), "Figure 3: Timing")
    doc.save(str(path))
    doc.close()


def _fake_detect(pages_boxes):
    """detect_figures_llm monkeypatch 팩토리.

    pages_boxes: {page_no(1-based): [box-dict, ...]}.
    figure_extractor 가 페이지별로 호출하는 detect 를 대체한다.
    """
    state = {"n": 0}

    def _inner(images_b64, max_tokens=2048):  # noqa: ARG001
        state["n"] += 1
        boxes = pages_boxes.get(state["n"], [])
        return ("FAKE", list(boxes))

    return _inner


def test_synthetic_sidecar_schema_keys(tmp_path, monkeypatch):
    """합성 PDF + monkeypatch LLM 으로 스키마 키·파일 일치·figure_id 규약 검증."""
    pdf = tmp_path / "synthetic.pdf"
    _make_synthetic_pdf(pdf)

    # p1=캡션없는 raster box(필터로 배제 기대), p2=Figure 1, p3=Figure 2/3.
    pages_boxes = {
        1: [{"bbox": [100, 50, 300, 150], "type": "logo", "caption": "ACME logo"}],
        2: [{"bbox": [150, 130, 850, 520], "type": "block", "caption": "Figure 1: System Architecture"}],
        3: [
            {"bbox": [150, 100, 850, 340], "type": "block", "caption": "Figure 2: AXI FSM"},
            {"bbox": [150, 530, 850, 810], "type": "diagram", "caption": "Figure 3: Timing"},
        ],
    }
    monkeypatch.setattr(fx, "detect_figures_llm", _fake_detect(pages_boxes))

    figs = extract_figures(pdf, tmp_path, strict_caption=True)
    assert figs, "Figure 캡션 박스가 채택되어야 함"

    # 캡션 없는 p1 로고는 strict 필터로 배제.
    assert not any(f["page"] == 1 for f in figs)
    # 모두 figure_no 가 'Figure' 로 시작.
    assert all(f["figure_no"].lower().startswith("figure") for f in figs)

    # 필수 키 + image_path 규약 + 파일 존재.
    for f in figs:
        assert _REQUIRED_KEYS.issubset(f.keys())
        assert f["image_path"].startswith("figures/")
        assert (tmp_path / f["image_path"]).exists()

    # figure_id 형식: <stem>__pNN_figK.
    stem = pdf.stem
    ids = {f["figure_id"] for f in figs}
    assert f"{stem}__p02_fig1" in ids
    # p3 두 figure 는 y 오름차순 정렬 후 fig1, fig2.
    p3 = sorted([f for f in figs if f["page"] == 3], key=lambda x: x["figure_id"])
    assert [f["figure_id"] for f in p3] == [f"{stem}__p03_fig1", f"{stem}__p03_fig2"]
    # y정렬: 위(Figure 2)가 fig1, 아래(Figure 3)가 fig2.
    assert "fsm" in p3[0]["caption"].lower()
    assert "timing" in p3[1]["caption"].lower()

    # 사이드카 파일 == 반환.
    sidecar = tmp_path / f"{stem}_figures.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == figs


def _make_no_figure_pdf(path: Path) -> None:
    """'Figure N' 텍스트가 페이지에 전혀 없는 PDF(로고/표/캡션없는 도형만).

    LLM 캡션도 페이지 텍스트(캡션 앵커)도 'Figure N' 이 없어야 strict 필터가 전부
    배제함을 검증할 수 있다(_make_synthetic_pdf 는 실제 'Figure N' 텍스트를 그려서
    캡션 앵커 fallback 이 정상적으로 그것을 잡아버리므로 이 테스트엔 부적합).
    """
    import fitz

    doc = fitz.open()
    p1 = doc.new_page(width=400, height=600)
    p1.insert_text((50, 50), "ACME Corp Logo Page")
    p2 = doc.new_page(width=400, height=600)
    p2.draw_rect(fitz.Rect(60, 80, 340, 300))
    p2.insert_text((70, 320), "Table 5: registers")
    p3 = doc.new_page(width=400, height=600)
    p3.draw_rect(fitz.Rect(60, 60, 340, 200))
    p3.insert_text((70, 215), "block with no number")
    doc.save(str(path))
    doc.close()


def test_strict_caption_filter_excludes_non_figure(tmp_path, monkeypatch):
    """strict_caption=True: 'Figure N' 캡션 없는 후보(로고/표/캡션없는 도형) 전부 배제."""
    pdf = tmp_path / "nofig.pdf"
    _make_no_figure_pdf(pdf)

    pages_boxes = {
        1: [{"bbox": [100, 50, 300, 150], "type": "logo", "caption": "company logo"}],
        2: [{"bbox": [150, 130, 850, 520], "type": "table", "caption": "Table 5: registers"}],
        3: [{"bbox": [150, 100, 850, 340], "type": "diagram", "caption": "block with no number"}],
    }
    monkeypatch.setattr(fx, "detect_figures_llm", _fake_detect(pages_boxes))

    figs = extract_figures(pdf, tmp_path, strict_caption=True)
    # 'Figure N' 매칭이 하나도 없으므로 전부 배제.
    assert figs == [], f"비-Figure 캡션은 모두 배제되어야 함, got {figs}"
    # 사이드카는 빈 리스트로 생성(다운스트림이 항상 파일 존재 가정 가능).
    sidecar = tmp_path / f"{pdf.stem}_figures.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == []


def test_strict_false_keeps_non_figure(tmp_path, monkeypatch):
    """strict_caption=False: 캡션 없는 후보도 채택(엄격 필터 off 대조군)."""
    pdf = tmp_path / "nofig.pdf"
    _make_no_figure_pdf(pdf)
    pages_boxes = {
        2: [{"bbox": [150, 130, 850, 520], "type": "diagram", "caption": "no figure number"}],
    }
    monkeypatch.setattr(fx, "detect_figures_llm", _fake_detect(pages_boxes))
    figs = extract_figures(pdf, tmp_path, strict_caption=False)
    assert figs, "strict=False 이면 캡션 없는 후보도 채택"


# ──────────────────────────────────────────────────────────────────────────────
# 3) opt-in 통합 (extract_all_via_pdf) — 동작 보존 + opt-in on 연결
# ──────────────────────────────────────────────────────────────────────────────

def test_optin_off_does_not_enter_figure_path(monkeypatch):
    """EXTRACT_FIGURES 미설정 → figure 경로 미진입(동작 보존).

    figure_extractor.extract_figures 를 폭탄으로 monkeypatch 해두고,
    extract_all_via_pdf 의 figure hook 이 OFF 일 때 호출되지 않음을 단언한다.
    """
    import extract_all_via_pdf as e

    monkeypatch.delenv("EXTRACT_FIGURES", raising=False)

    called = {"hit": False}

    def _boom(*a, **k):
        called["hit"] = True
        raise AssertionError("OFF 인데 figure_extractor 가 호출됨 (동작 보존 위반)")

    monkeypatch.setattr(fx, "extract_figures", _boom)

    # hook 게이트가 OFF 를 정확히 판정하는지 (env 직접 평가).
    assert e._figures_enabled() is False
    # 게이트 함수가 OFF 면 maybe_extract_figures 가 no-op (extract_figures 미호출).
    out = e.maybe_extract_figures(Path("/nonexistent.pdf"), Path("/tmp/out"))
    assert out is None
    assert called["hit"] is False


def test_optin_on_creates_sidecar_and_figures_dir(tmp_path, monkeypatch):
    """EXTRACT_FIGURES=1 → figures/ 디렉터리 + _figures.json 생성(연결 지점 단위검증).

    실제 LLM 호출은 회피(detect monkeypatch)하되, opt-in ON 경로가 figure_extractor 를
    호출하고 사이드카·디렉터리를 만드는지를 단위로 검증한다.
    """
    import extract_all_via_pdf as e

    pdf = tmp_path / "synthetic.pdf"
    _make_synthetic_pdf(pdf)
    out_dir = tmp_path / "pdf_md"
    out_dir.mkdir()

    pages_boxes = {
        2: [{"bbox": [150, 130, 850, 520], "type": "block", "caption": "Figure 1: System Architecture"}],
    }
    monkeypatch.setattr(fx, "detect_figures_llm", _fake_detect(pages_boxes))
    monkeypatch.setenv("EXTRACT_FIGURES", "1")

    assert e._figures_enabled() is True
    result = e.maybe_extract_figures(pdf, out_dir)
    assert result is not None and len(result) >= 1

    assert (out_dir / "figures").is_dir(), "figures/ 디렉터리 생성되어야 함"
    sidecar = out_dir / f"{pdf.stem}_figures.json"
    assert sidecar.exists(), "_figures.json 생성되어야 함"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data == result
    # 크롭 PNG 실제 존재.
    for f in data:
        assert (out_dir / f["image_path"]).exists()
