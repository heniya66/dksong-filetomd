"""oversized_table.py — 초대형/초고밀도 표 → 크롭 PNG + 사이드카 + MD 치환 범용 도구.

배경(사용자 지시, 글로벌 CLAUDE.md 박제): 60행×50열 진리표·rowspan 다중·회전 헤더 등
markdown 전사 시 왜곡·환각이 발생하는 초대형/초고밀도 표는, GFM(GitHub Flavored
Markdown) 전사 대신 다음 4단계로 처리한다.

  ① 원본 크롭 PNG(환각 0) — 본 모듈 :func:`crop_table_region`.
  ② MD 오염 표를 이미지 링크로 교체 — 본 모듈 :func:`replace_polluted_table_in_md`.
  ③ AI 정밀 설명(RAG[Retrieval-Augmented Generation] 검색용) 사이드카+MD 주입 —
     본 모듈 :func:`inject_description_block` (+ describe 연동).
  ④ 에이전트 비전 검수 — **도구 밖 수동 단계**. 본 모듈은 자동화하지 않는다.

본 모듈은 원격(filetomd)에 존재하던 하드코딩 1회성 스크립트 2개(``crop_dense_tables.py``
+ ``describe_ctables.py`` — STEM/PAGES 하드코딩)를 **파라미터화된 재사용 도구**로
일반화한 것이다(로컬 filestomdwgem ``fmdw/oversized_table.py`` 이식). 문서·페이지·캡션을
인자로 받아 어떤 문서에도 재사용할 수 있다.

codesign-rag 연동 계약(★★★ 절대 준수 — codesign-rag 코드는 읽기만, 수정 금지)
----------------------------------------------------------------------------------
codesign-rag ``src/rag/figure_linker.py`` 실코드 확인 결과:

1. **``kind`` 는 ``figure_linker._TABLE_KINDS = {"oversized_table",
   "diagonal_table"}`` 소속이어야 한다.** 표 링크 경로(``figure_linker.py`` L349)는
   ``kind in _TABLE_KINDS`` 또는 ``type == "table"`` 인 항목만 청크에 연결한다. 원격
   자동경로의 옛 ``kind="oversized_matrix"`` 는 이 집합에 없어(且 ``type="complex_table"``)
   표-링크 분기 자체를 통째로 skip 당했다(청크에 절대 연결 안 됨). 본 도구·자동경로
   모두 **``kind="oversized_table"``** 를 사용한다(``fx._oversized_table_kind()``).
2. **figure_id 접미사 규약** — ``_FIG_ID_SUFFIX_RE =
   r"__p\\d+_(?:fig|table)(?:_[a-z]+)*\\d+$"`` 로 ``doc_stem`` 을 도출한다. 옛
   ``<stem>__pNN_omtbl<k>`` / ``<stem>__pNN_ctable1`` 은 ``fig``/``table`` 로 시작하지
   않아 매치되지 않는다. 본 도구는 **``<stem>__pNN_table_oversized<k>``** 를 사용한다
   (``table`` + ``_oversized`` + 숫자 → 매치 확인됨). figure_id 생성은 자동경로와 단일
   SSoT 를 이루도록 ``fx._oversized_table_figure_id()`` 에 위임한다(``FMDW_OVERSIZED_FIGID_LEGACY``
   게이트로 구 형식 폴백 가능).
3. **caption 은 표 번호 포함 권장** — 링크 키는 ``normalize_table_no(caption)`` 우선.
   caption 에서 번호 실패 시 ``text_transcription`` 첫 줄 fallback 을 시도하므로
   ``text_transcription`` 은 항상(빈 문자열이라도) 채운다.
4. **사이드카 갱신 = figure_id 단위 upsert** — 문서 내 일부 표만 CLI 로 크롭하는
   용도라, kind 기준 전체 wipe(``diagonal_table`` 경로 방식)는 이전 크롭 항목을 지우는
   사고를 낸다. :func:`upsert_sidecar_entry` 는 ``figure_id`` 를 키로 멱등 upsert 한다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fmdw import figure_extractor as fx

_log = logging.getLogger(__name__)

__all__ = [
    "detect_oversized_table_bbox",
    "crop_table_region",
    "upsert_sidecar_entry",
    "replace_polluted_table_in_md",
    "inject_description_block",
    "find_missing_descriptions",
    "fill_missing_description",
]

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

#: 본문영역 휴리스틱 — 상단(헤더) 제외 비율(페이지 높이 대비). 원격 crop_dense 스펙 그대로.
OVERSIZED_TABLE_TOP_MARGIN_RATIO = 0.10
#: 본문영역 휴리스틱 — 하단(푸터) 제외 비율(페이지 높이 대비). 원격 crop_dense 스펙 그대로.
OVERSIZED_TABLE_BOTTOM_MARGIN_RATIO = 0.07

#: 원격 스키마 호환용 type 값(계약 §1 — kind 가 있으면 무방).
OVERSIZED_TABLE_TYPE = "complex_table"
#: 사이드카 source 필드(원격 crop_dense 스펙 그대로 — 정보성, 어떤 코드도 게이트하지 않음).
OVERSIZED_TABLE_SOURCE = "complex_table_crop"


def _table_text(page: Any, bbox: list[float]) -> str:
    """bbox 영역의 선택 가능한 텍스트를 추출(초대형 raster 표는 보통 ""). 비차단."""
    try:
        import fitz

        return page.get_text("text", clip=fitz.Rect(*bbox)) or ""
    except Exception as exc:  # noqa: BLE001 — 텍스트 추출 실패는 비차단(빈 문자열).
        _log.warning("표 텍스트 추출 실패(무시): %s", exc)
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# 1) 표 영역 검출 — 벡터/래스터 검출기 우선 → 본문영역 휴리스틱 폴백
# ──────────────────────────────────────────────────────────────────────────────

def detect_oversized_table_bbox(
    page: Any,
    *,
    top_margin_ratio: float = OVERSIZED_TABLE_TOP_MARGIN_RATIO,
    bottom_margin_ratio: float = OVERSIZED_TABLE_BOTTOM_MARGIN_RATIO,
) -> list[float]:
    """페이지에서 초대형/초고밀도 표 bbox 를 결정적으로 판정(단일 bbox 반환).

    절차(원격 filetomd 의 검증된 프리미티브 재사용 — 신규 검출 로직 창작 없음):

      1. **벡터 사선표 검출**(``fx.detect_complex_tables``) → 후보가 있으면 최대 면적 채택.
      2. **래스터 오버사이즈 행렬표 검출**(``fx.detect_oversized_matrix_tables``) →
         후보가 있으면 최대 면적 채택(격자선 없는 순수 이미지 표 커버).
      3. **폴백(본문영역 휴리스틱)**: 상단 ``top_margin_ratio``(헤더)·하단
         ``bottom_margin_ratio``(푸터)를 제외한 영역을 표로 간주한다(초대형 표는 통상
         페이지 대부분을 차지 — CLI 사용자가 페이지를 명시하므로 안전한 근사).

    Returns:
        [x0, y0, x1, y1] (PDF pt, 좌상단 원점). 항상 값을 반환한다(페이지당 표 1개 가정).
    """
    for detector in (fx.detect_complex_tables, fx.detect_oversized_matrix_tables):
        try:
            boxes = detector(page) or []
        except Exception as exc:  # noqa: BLE001 — 검출 실패는 다음 폴백으로.
            _log.warning("%s 검출 실패(다음 폴백): %s", getattr(detector, "__name__", "?"), exc)
            boxes = []
        if boxes:
            best = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            return [round(v, 1) for v in best]

    pw, ph = page.rect.width, page.rect.height
    top = ph * top_margin_ratio
    bottom = ph * (1.0 - bottom_margin_ratio)
    return [0.0, round(top, 1), round(pw, 1), round(bottom, 1)]


# ──────────────────────────────────────────────────────────────────────────────
# 2) 크롭·항목 생성
# ──────────────────────────────────────────────────────────────────────────────

def crop_table_region(
    pdf_path: Path,
    page_no: int,
    out_dir: Path,
    *,
    dpi: int = fx.DEFAULT_DPI,
    table_index: int = 1,
    caption: str = "",
    bbox: Optional[list[float]] = None,
) -> dict[str, Any]:
    """PDF 특정 페이지의 초대형/초고밀도 표 영역을 PNG 로 크롭 + 사이드카 항목 반환.

    Args:
        pdf_path: 원본 PDF 경로.
        page_no: 1-base 페이지 번호(사용자 CLI 입력과 동일 규약).
        out_dir: 출력 base 디렉터리. 크롭 PNG 는 ``out_dir/figures/<figure_id>.png``,
            반환 항목의 ``image_path`` 는 ``out_dir`` 기준 상대경로(사이드카가 이
            ``out_dir`` 에 위치한다고 가정 — figure_extractor 관례와 동일).
        dpi: 크롭 렌더 해상도(기본 300).
        table_index: 같은 페이지에 여러 표가 있을 때의 순번(``k``, 1부터).
        caption: 표 제목(가능하면 원본 그대로, 예 "Table 21: Register Map" — 표 번호를
            포함하면 codesign-rag ``normalize_table_no`` 가 본문 참조와 자동 매칭).
        bbox: 명시 bbox(PDF pt). None 이면 :func:`detect_oversized_table_bbox` 로 자동 판정.

    Returns:
        사이드카 항목 dict(codesign-rag 연동 계약 필드 포함). figure_id/kind 는
        ``fx._oversized_table_figure_id`` / ``fx._oversized_table_kind`` 에 위임해
        자동경로와 단일 계약(및 ``FMDW_OVERSIZED_FIGID_LEGACY`` 게이트)을 공유한다.

    Raises:
        ValueError: page_no 가 문서 페이지 범위를 벗어남.
    """
    import fitz

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    stem = pdf_path.stem
    figures_dir = out_dir / "figures"

    doc = fitz.open(str(pdf_path))
    try:
        if not (1 <= page_no <= doc.page_count):
            raise ValueError(
                f"page_no={page_no} 범위 초과(문서 페이지 수={doc.page_count}): {pdf_path}"
            )
        page = doc[page_no - 1]
        resolved_bbox = (
            [round(v, 1) for v in bbox] if bbox is not None
            else detect_oversized_table_bbox(page)
        )
        figure_id = fx._oversized_table_figure_id(stem, page_no, table_index)
        rel_image_path = f"figures/{figure_id}.png"
        png_path = figures_dir / f"{figure_id}.png"
        fx._crop_and_save(page, resolved_bbox, png_path, dpi)
        text_transcription = _table_text(page, resolved_bbox)
    finally:
        doc.close()

    return {
        "figure_id": figure_id,
        "page": page_no,
        "image_path": rel_image_path,
        "caption": caption,
        "figure_no": None,
        "type": OVERSIZED_TABLE_TYPE,
        "bbox": resolved_bbox,
        "source": OVERSIZED_TABLE_SOURCE,
        "snap_iou": None,
        "kind": fx._oversized_table_kind(),
        "text_transcription": text_transcription,
        "description": "",
    }


# ──────────────────────────────────────────────────────────────────────────────
# 3) 사이드카 upsert(figure_id 단위, 멱등)
# ──────────────────────────────────────────────────────────────────────────────

def upsert_sidecar_entry(sidecar_path: Path, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """entry(``figure_id`` 기준)를 사이드카(list JSON)에 멱등 upsert.

    같은 ``figure_id`` 의 기존 항목은 제거 후 새 entry 로 교체(재실행 멱등). 다른
    ``figure_id`` 항목(다른 표·기존 figure/diagonal_table 크롭 결과 포함)은 그대로 보존.
    사이드카가 없거나 손상(list 아님/파싱 실패)이면 빈 리스트에서 새로 시작(non-fatal).

    Returns:
        갱신된 사이드카 전체 리스트(파일에 쓴 내용과 동일).
    """
    sidecar_path = Path(sidecar_path)
    existing: list[dict[str, Any]] = []
    if sidecar_path.exists():
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
            else:
                _log.warning("사이드카 형식 오류(list 아님, 새로 생성): %s", sidecar_path)
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("사이드카 파싱 실패(새로 생성, non-fatal): %s — %s", sidecar_path, exc)

    fid = entry.get("figure_id")
    filtered = [
        e for e in existing
        if not (isinstance(e, dict) and e.get("figure_id") == fid)
    ]
    filtered.append(entry)

    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return filtered


# ──────────────────────────────────────────────────────────────────────────────
# 4) MD 오염 표 → 안내문 + 이미지 링크 치환
# ──────────────────────────────────────────────────────────────────────────────

_NOTICE_TEXT = (
    "⚠️ 초고밀도 표라 markdown 전사 시 왜곡·환각 발생, "
    "원본 이미지 크롭으로 대체 (fmdw 특수표 규칙)"
)


def _find_block_end(body: str, start: int) -> int:
    """marker 시작 위치(start)로부터 치환 대상 표 블록의 끝 인덱스를 결정.

    - marker 직후가 ``<table`` (HTML 표 시작, 대소문자 무관)이면 대응 ``</table>`` 뒤까지
      (닫는 태그 없으면 문서 끝까지 — 안전 폴백).
    - 그 외(GFM pipe 표 등)는 marker 이후 첫 빈 줄(``\\n\\n``) 직전까지.
    """
    lookahead = body[start:start + 20].lower()
    if "<table" in lookahead:
        close_idx = body.lower().find("</table>", start)
        if close_idx == -1:
            return len(body)
        return close_idx + len("</table>")
    blank_idx = body.find("\n\n", start)
    return blank_idx if blank_idx != -1 else len(body)


def replace_polluted_table_in_md(
    md_path: Path,
    marker: str,
    *,
    image_path: str,
    figure_id: str,
) -> None:
    """MD 에서 marker 로 시작하는 오염된 거대 표 블록을 안내문+이미지 링크로 치환.

    **marker 매치가 MD 전체에서 정확히 1건일 때만** 치환한다 — 0건/2건↑ 이면
    ``ValueError`` 로 중단(잘못된 위치 치환 방지, 원격 crop_dense 안전장치와 동일).

    Raises:
        ValueError: marker 매치가 0건이거나 2건 이상인 경우.
    """
    md_path = Path(md_path)
    body = md_path.read_text(encoding="utf-8")
    count = body.count(marker)
    if count == 0:
        raise ValueError(f"replace marker 가 MD 에 없음(0건 매치): {marker!r}")
    if count > 1:
        raise ValueError(
            f"replace marker 가 MD 에 {count}건 매치(정확히 1건이어야 함, 모호한 "
            f"위치 치환 방지): {marker!r}"
        )

    start = body.index(marker)
    end = _find_block_end(body, start)
    replacement = f"{_NOTICE_TEXT}\n\n![{figure_id}]({image_path})"
    new_body = body[:start] + replacement + body[end:]
    md_path.write_text(new_body, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# 5) AI 정밀 설명 블록 주입(멱등)
# ──────────────────────────────────────────────────────────────────────────────

#: 설명 블록 선두 마커 — 중복 주입 판정 및 파싱 기준(원격 describe_ctables 와 동일 문구).
DESCRIPTION_BLOCK_PREFIX = "> **표 설명 (AI 정밀분석)**:"


def inject_description_block(md_path: Path, image_path: str, description: str) -> bool:
    """MD 의 ``image_path`` 이미지 링크 바로 아래에 AI 정밀 설명 블록을 주입(멱등).

    - description 이 빈 문자열/공백뿐이면 아무 것도 하지 않고 False(describe 실패/OFF degrade).
    - 이미지 링크(``](image_path)``)를 못 찾으면 False(안전 skip).
    - 이미지 링크 직후에 이미 :data:`DESCRIPTION_BLOCK_PREFIX` 가 있으면 False(멱등).

    Returns:
        실제로 주입했으면 True, 그 외(빈 설명/링크 없음/이미 존재)는 False.
    """
    if not description or not description.strip():
        return False
    md_path = Path(md_path)
    body = md_path.read_text(encoding="utf-8")
    img_marker = f"]({image_path})"
    idx = body.find(img_marker)
    if idx == -1:
        return False
    insert_at = idx + len(img_marker)
    lookahead = body[insert_at:insert_at + 400]
    if DESCRIPTION_BLOCK_PREFIX in lookahead:
        return False  # 이미 주입됨(멱등)

    block = f"\n\n{DESCRIPTION_BLOCK_PREFIX} {description.strip()}\n"
    new_body = body[:insert_at] + block + body[insert_at:]
    md_path.write_text(new_body, encoding="utf-8")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# 6) describe-only 재실행 지원(쿼터/네트워크 실패 후 설명만 나중에 채움)
# ──────────────────────────────────────────────────────────────────────────────

def find_missing_descriptions(sidecar_path: Path) -> list[dict[str, Any]]:
    """사이드카에서 ``kind`` 가 오버사이즈 표(신·구 형식 모두)이면서 description 이 빈
    항목만 반환. 사이드카 없음/파싱 실패/list 아님이면 빈 리스트(non-fatal).
    """
    sidecar_path = Path(sidecar_path)
    if not sidecar_path.exists():
        return []
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    _oversized_kinds = {"oversized_table", "oversized_matrix"}
    return [
        e for e in data
        if isinstance(e, dict)
        and e.get("kind") in _oversized_kinds
        and not str(e.get("description", "")).strip()
    ]


def fill_missing_description(entry: dict[str, Any], image_root: Path) -> str:
    """entry(``image_path`` 상대경로)의 실제 크롭 PNG 로 설명 생성(비차단).

    이미지 원본 부재/ describe 실패(쿼터/네트워크)면 빈 문자열 반환
    (``fx._maybe_describe_figure`` 가 예외를 흡수 — 그 계약을 그대로 재사용).

    Args:
        entry: 사이드카 항목(``image_path``/``caption`` 사용).
        image_root: ``image_path`` 상대경로의 base(보통 사이드카가 위치한 out_dir).
    """
    img_rel = entry.get("image_path")
    if not img_rel:
        return ""
    png_path = Path(image_root) / img_rel
    if not png_path.exists():
        _log.warning("표 이미지 원본 부재(describe-only skip): %s", png_path)
        return ""
    return fx._maybe_describe_figure(
        png_path, caption=str(entry.get("caption", "")), item_type="complex_table"
    )
