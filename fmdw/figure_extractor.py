"""figure_extractor — PDF의 `Figure N` 다이어그램만 검출·크롭하는 운영 모듈.

PoC(`scripts/poc_figure_crop.py`)의 검증된 하이브리드 검출 로직을 운영 모듈로 승격한다.
설계 문서(`docs/superpowers/specs/2026-06-05-figure-image-graphrag-design.md`) §5.1/§6.1을
계약으로 따른다.

파이프라인 (PoC 이식 + 운영화)
------------------------------
1. 페이지 렌더(기본 300 DPI(Dots Per Inch)): `lib.ollama_extractor.render_pdf_pages_to_base64`
   재활용. 렌더 px 크기는 좌표 변환에 직접 쓰지 않고(PoC 와 동일하게 PDF pt 좌표 + dpi
   배율로 크롭) 검출용 base64 만 만든다.
2. LLM(Large Language Model) bbox 검출: 멀티모달 LLM(Ollama Cloud `gemini-3-flash-preview`,
   localhost 게이트웨이)에 페이지 이미지를 주고 figure별 `{bbox(0~1000 정규화), type,
   caption}` JSON 을 요청(`detect_figures_llm`, PoC 재사용).
3. 결정적(deterministic) 스냅 보정: raster bbox(`page.get_image_rects`)/벡터 군집
   (`page.get_drawings`)/캡션 위치(`page.get_text("dict")`)로 LLM bbox 를 가까운 경계에 스냅.
4. **Figure N 캡션 엄격 필터**(`strict_caption=True`): 'Figure' + 숫자 정규식 매칭되는
   후보만 채택. LLM 캡션 또는 페이지의 캡션 앵커(`page.get_text`) 중 하나라도 매칭되면
   채택. 매칭 안 되면 제외(로고/표/캡션없는 도형 배제).
5. 동일 페이지 다중 figure 는 y좌표 오름차순 정렬 후 `pNN_figK`(K는 1부터).
6. figure_id = `<stem>__pNN_figK`. 크롭 PNG = `out_dir/figures/<stem>__pNN_figK.png`.
   사이드카 = `out_dir/<stem>_figures.json` (§6.1 스키마, 반환 리스트와 동일 내용).

설계 원칙 (CLAUDE.md 글로벌 룰)
------------------------------
- Claude 단일 LLM 룰: 본 모듈은 Claude 가 직접 작성. 검출용 멀티모달 LLM
  (gemini-3-flash-preview)은 figure 검출 대상 도구라 정상 사용(PoC 와 동일 경로).
- 시크릿/API(Application Programming Interface) 키 값 출력 금지(Keychain SSoT). 본 모듈은
  키를 직접 다루지 않음(localhost 게이트웨이는 Authorization 불필요).
- 워크스페이스 독립: codesign-rag 미접촉. filestomdwgem 내부에서만 동작.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

from fmdw import ollama_extractor as ox

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 상수 / 기본값 (PoC 와 동일 — 검증된 튜닝값 보존)
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_DPI = 300

#: 헤더 로고 등 소형 raster 를 figure 후보에서 거르는 최소 페이지 점유율.
MIN_RASTER_PAGE_COVER = 0.03
#: 벡터 도형 군집 시 같은 figure 로 묶는 최대 간극(PDF pt).
VECTOR_CLUSTER_GAP_PT = 28.0
#: 벡터 군집을 유효 후보로 인정하는 최소 도형 수 / 최소 면적 점유율.
VECTOR_MIN_DRAWINGS = 6
VECTOR_MIN_AREA_COVER = 0.02

# ── 고립 full-width 수평 구분선(러닝 헤더/푸터 선) 제외 상수 (2026-07-04) ──────────
#: 러닝 헤더/푸터 아래의 full-width 수평 구분선이 벡터 클러스터에 흡수돼 도형 상단
#: 크롭에 헤더 텍스트가 딸려 들어가는 사고(LN08LPU Design p14/p18 실측) 방지. '양 끝점
#: 근처에 수직선분이 없는 full-width 수평선'만 제외한다 — 닫힌 프레임 테두리의 top/bottom
#: 수평선은 양 끝에 프레임 좌우 수직선이 붙어 있어 보존되고, 헤더/푸터 구분선(고립 수평선)
#: 만 걸러진다. 도형 내부의 (페이지 폭 미만) 수평선·raster 경로에는 무영향.
#: 수평선으로 볼 최대 높이(pt): 이보다 얇으면 선.
HLINE_MAX_H_PT = 2.5
#: 수직선으로 볼 최대 폭(pt).
VLINE_MAX_W_PT = 2.5
#: full-width(고립 구분선 후보) 판정: 페이지 폭의 이 비율 이상.
HLINE_FULLWIDTH_RATIO = 0.8
#: 수평선 끝점 x ↔ 수직선 x 근접 허용(pt).
HLINE_ENDPOINT_X_TOL = 12.0
#: 수직선 y-range 가 수평선 y 에 '닿는다'고 볼 허용(pt).
HLINE_ENDPOINT_Y_TOL = 12.0
#: LLM bbox ↔ 결정적 bbox 스냅 판정 IoU(Intersection over Union) 임계.
SNAP_IOU_THRESHOLD = 0.15
#: 스냅 대상 없을 때 LLM bbox 에 더하는 여백 패딩(PDF pt). 기존 6.0pt 는 너무 작아
#: 테두리 있는 도형(예: bordered box diagram)의 외곽선이 크롭에서 잘리는 사고 발생
#: (2026-07-03 LN08LPU Figure 2/3 실측) → 16.0pt 로 상향. env EXTRACT_FIGURE_CROP_PAD_PT
#: 로 override 가능.
def _llm_padding_pt() -> float:
    try:
        return float(os.getenv("EXTRACT_FIGURE_CROP_PAD_PT", "16.0"))
    except ValueError:
        return 16.0


LLM_PADDING_PT = _llm_padding_pt()
#: 텍스트 라인/아이콘으로 간주해 제외할 최소 figure 면적 점유율.
MIN_FIGURE_AREA_COVER = 0.015

# ── Fix 2: 결정적 도면 검출(캡션↔raster/vector 페어링) 상수 (2026-07-02) ────────
#: 캡션 앵커 중심이 도형 x범위에서 이 여백(pt) 안이면 x정렬로 간주.
DET_CAPTION_X_TOL_PT = 30.0
#: 캡션 앵커와 도형 영역의 최대 세로 간극(pt). 캡션은 보통 figure 바로 위/아래에
#: 붙으므로 이 이내여야 페어링. 초과하면 무관한 캡션으로 보고 페어링 안 함.
DET_CAPTION_MAX_GAP_PT = 72.0

#: [Fix B, 2026-07-04] 캡션을 통째로 감싸는 '과대(oversized) wrapping 후보' 판정 기준.
#: 페이지 점유율이 이 값을 초과하면서 캡션을 세로로 감싸는 후보는, 같은 캡션에 더 작은
#: 대안(진짜 tight 도형)이 있을 때만 후순위로 밀어낸다. 유일 후보인 정당한 full-page
#: 도면(대안 없음)은 그대로 채택 → 회귀 없음. 실측: LN08LPU Design 배경 raster
#: cover 0.591, HSPICE 거대 vector cover 0.87 이 이 문턱을 넘어 밀려나고 tight 도형이 이김.
DET_OVERSIZED_COVER = 0.5
#: 상대 과대 판정: 캡션을 감싸는 후보 면적이 '더 작은 대안' 면적의 이 배수를 초과하면
#: 과대 wrapping 으로 간주(cover 0.5 미만이지만 진짜 도형보다 훨씬 큰 wrapping 방어).
DET_OVERSIZED_AREA_FACTOR = 4.0
#: [분할 타일 병합, 2026-07-04] 한 도형이 좌우로 인접한 raster 타일 여러 장으로 삽입된
#: 경우(같은 세로 밴드 공유 + 가로 인접) 하나로 병합해 절반 잘림을 막는다. 이 값은 타일
#: 간 최대 가로 간극(pt) — 타일은 보통 맞붙어 있어 작게 둔다(다른 도형 오병합 방지).
#: 세로 밴드가 다르면(=다른 도형) y겹침 게이트에서 병합 안 됨(회귀 0).
DET_TILE_UNION_GAP_PT = 12.0
#: 분할 타일로 인정할 최소 세로 겹침 비율(작은 타일 높이 대비).
DET_TILE_UNION_Y_OVERLAP = 0.6

#: 캡션 'Figure N' 엄격 매칭 정규식. 'Figure 1', 'Fig. 2', 'FIGURE 12', 한글 '그림 3',
#: 'Figure A-1'(부록 도면)류 수용. 번호는 `[A-Za-z]?-?\d+`(선택적 문자 접두 + 선택적
#: 하이픈 + 숫자)로 확장. 'Table N'/'표 N' 은 매칭하지 않음(표 배제 유지). 단어 경계로
#: 'configure3' 류 오탐 방지(fig 분기에 \b 유지).
#: 번호 캡처는 하위번호(4-2, 4.3, A-1)를 **온전히** 포착한다(2026-07-04). 기존
#: `([A-Za-z]?-?\d+)` 는 "Figure 4-2" 에서 "4"만 잡아 "Figure 4-2"·"Figure 4-3" 가
#: 같은 라벨("Figure 4")로 뭉개져 서로 다른 도형이 dedup 으로 유실됐다(실측 HSPICE p2).
#: `(?:[.-]\d+)*` 를 붙여 "4-2"/"4-3"/"4.3" 를 구분한다. figure_no 필드 정확도도 향상.
_FIGURE_CAPTION_RE = re.compile(
    r"(?:\bfig(?:ure)?\.?\s*|그림\s*)([A-Za-z]?-?\d+(?:[.-]\d+)*)", re.IGNORECASE
)
#: 캡션에서 정규화된 'Figure N' 라벨을 만들 때 쓰는 번호 추출(figure_no 필드).
_FIGURE_NO_RE = _FIGURE_CAPTION_RE

#: [캡션 정리, 2026-07-04] 캡션 앵커(도형 페어링용) 전용 엄격 정규식. 본문 문장 속 참조
#: ("... see Figure 11")·문장형 캡션("Figure 4-3 illustrates ...")을 배제하기 위해:
#:   (a) 줄머리 매칭(^\s*) — 줄 중간 참조 배제.
#:   (b) 'Figure N' 뒤가 콜론/대시/닫는괄호/마침표/줄끝/(공백+대문자·괄호) 인 경우만 캡션
#:       으로 인정 → 번호 뒤 소문자 동사(illustrates/shows/is ...)로 이어지는 문장형 배제.
#: 접두(figure/fig/그림)는 대소문자 무시(?i:...), 종결부의 대문자 판정은 대소문자 구분
#: (문장형 소문자 배제의 핵심)이라 전역 IGNORECASE 대신 스코프 플래그로 분리한다. 이
#: 정규식은 caption_anchors 에서만 쓰여 match_figure_caption 등 _FIGURE_CAPTION_RE 를
#: 쓰는 다른 경로에 영향이 없다(영향 범위를 캡션-앵커 판정으로 국한).
#: 번호 연속부는 possessive(`*+`)로 매칭해 "4-3" 이 "4"로 backtrack 하는 것을 막는다.
#: (backtrack 하면 종결부의 구분자 대시 `-` 가 "4" 뒤 하이픈에 걸려 문장형 "Figure 4-3
#: illustrates ..." 가 오탐된다. Python 3.11 possessive 지원.)
_CAPTION_ANCHOR_RE = re.compile(
    r"^\s*(?i:fig(?:ure)?\.?|그림)\s*"
    r"([A-Za-z]?-?\d+(?:[.-]\d+)*+)"
    r"\s*(?::|[)–—-]|\.(?:\s|$)|$|\s+[A-Z(])"
)

#: 'Table N' 캡션 매칭(복잡 표 크롭의 caption 보강용 — figure 엄격필터와는 별개 경로).
_TABLE_CAPTION_RE = re.compile(r"\btable\s*(\d+)", re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────────────
# 사선·비정형 표 → 이미지 저장 (opt-in, EXTRACT_DIAGONAL_TABLE=1) 상수
# ──────────────────────────────────────────────────────────────────────────────
# 판정은 결정적(deterministic) 벡터 분석이다(LLM 비의존). 표가 순수 수직·수평 격자면
# markdown GFM 표로 전사(기존 동작 유지)되고, 표 격자 영역 안에 사선(대각선) 구분선이
# 존재하면 markdown 표로 표현 불가하므로 PNG 로 크롭해 사이드카에 type:"complex_table"
# 로 남긴다. 일반 격자 표를 이미지로 오분류하지 않도록 보수적 임계로 설계.

_DIAG_TRUTHY = {"1", "true", "on", "yes", "y"}

#: H/V 판정 허용오차(pt): |dy|<=tol → 수평, |dx|<=tol → 수직.
DIAG_HV_TOL = 1.5
#: 격자선으로 셀 최소 길이(pt) — 미세 stray 선분 배제.
DIAG_GRID_MIN_LEN = 14.0
#: 사선으로 셀 최소 길이(pt).
DIAG_SEG_MIN_LEN = 14.0
#: 사선 인정 최소 off-axis 각도(deg). 수평/수직에서 이 각도 이상 벗어나야 사선.
#: (살짝 기운 격자선이 사선으로 오탐되는 것 방지.)
DIAG_MIN_ANGLE_DEG = 15.0
#: 격자선 bbox 를 표 영역으로 군집할 때 최대 간극(pt).
DIAG_CLUSTER_GAP_PT = 24.0
#: 표 격자로 인정할 최소 수평/수직 선 수(영역 내). 표는 최소 몇 행·열을 가짐.
DIAG_MIN_GRID_H = 3
DIAG_MIN_GRID_V = 3
#: 표 영역으로 인정할 최소/최대 페이지 점유율(배경 프레임·미세 잡선 배제).
DIAG_TABLE_MIN_COVER = 0.01
DIAG_TABLE_MAX_COVER = 0.95
#: 복잡 표 판정에 필요한 영역 내 최소 사선 수.
DIAG_MIN_DIAG_SEGS = 1
#: 복잡 표 크롭 시 더하는 여백 패딩(pt). 기존 4.0pt 는 표 테두리 잘림 위험 →
#: LLM_PADDING_PT 와 동일 수준(16.0)으로 상향(2026-07-03). env
#: EXTRACT_DIAGONAL_TABLE_PAD_PT 로 override 가능.
def _diag_pad_pt() -> float:
    try:
        return float(os.getenv("EXTRACT_DIAGONAL_TABLE_PAD_PT", "16.0"))
    except ValueError:
        return 16.0


DIAG_PAD_PT = _diag_pad_pt()


def _is_diagonal_table_enabled() -> bool:
    """EXTRACT_DIAGONAL_TABLE 가 truthy 면 사선/비정형 표 → 이미지 경로 활성(기본 OFF).

    기본 OFF 이면 detect_complex_tables 가 호출되지 않아 사이드카/크롭이 기존과
    byte-identical 보존된다(회귀 0). EXTRACT_FIGURES 경로 안에서만 평가된다.
    """
    return os.getenv("EXTRACT_DIAGONAL_TABLE", "").strip().lower() in _DIAG_TRUTHY


# ──────────────────────────────────────────────────────────────────────────────
# 오버사이즈 행렬표(raster) → 이미지 저장 (opt-in, EXTRACT_OVERSIZED_MATRIX_TABLES=1)
# ──────────────────────────────────────────────────────────────────────────────
# 2026-07-03 실측(LN08LPU Design Manual p11~13 "Table 21: Design Truth Table",
# ~50열×~60행): 이 표는 벡터 격자선이 전혀 없고 **표 전체가 고해상도 raster 이미지로
# 페이지에 삽입**되어 있다(선택 가능한 텍스트 0, get_drawings() 선분도 장식 테두리
# 9개뿐). 따라서 detect_complex_tables 의 H/V 벡터 격자 군집 방식으로는 원천적으로
# 검출 불가 — 유일한 결정적 신호는 "삽입 이미지의 px 밀도(px/pt)·페이지 점유율"이다.
# detect_complex_tables(사선 벡터 경로)와는 **완전히 독립적인 탐지 경로**이며 서로의
# 판정 로직을 공유/의존하지 않는다(코드 얽힘 방지) — 크롭 이후의 사이드카 적재·describe
# 호출 지점만 공용 인프라를 재사용한다.
#
# 실측 밀도/점유율 분리(양쪽 testpages PDF 전수 스윕, 2026-07-03):
#   - 오버사이즈 행렬표 이미지(p11~13): 밀도 4.4~6.9 px/pt, 점유율 0.32~0.51
#   - 일반 삽화/사진/워터마크 이미지(그 외 전 페이지): 밀도 1.3~2.4 px/pt, 점유율 ≤0.36
#   → 밀도 3.5 · 점유율 0.30 AND 게이트로 여유 있게 분리(문턱값 사이 안전마진 확보).

#: 페이지 전역에 반복 등장하는 워터마크/배경 이미지를 몇 페이지 이상 등장 시 제외할지.
OVERSIZED_WATERMARK_MIN_PAGES = 3

# ── 극단 단일축 게이트(2026-07-10) ──────────────────────────────────────────────
# 근본원인(실측 LN08LPU p91~97, 7페이지 전부 거대 진리표): 원래 게이트는
# `density>=3.5 AND cover>=0.30` 의 순수 AND 조건이라, **시각적으로 동일한** 거대표
# 페이지가 한 축만 문턱을 살짝 벗어나면 들쭉날쭉 놓쳤다.
#   - p2: density 6.91(정상 삽화 최대 2.4의 ~3배!) 인데 cover 0.274 < 0.30 → 놓침
#   - p5: cover 0.603(정상 삽화 최대 0.36의 ~1.7배!) 인데 density 3.14 < 3.5 → 놓침
#   - p6: cover 0.707(페이지의 71%!) 인데 density 3.14 < 3.5 → 놓침
# 놓친 페이지는 본문 OCR 로 흘러가 거대표 GFM 전사 → 길이초과 절단 → 같은 청크 뒤
# 페이지(예: p7 Note 박스)까지 통째로 유실됐다. 사람이 매번 MIN_DENSITY/MIN_COVER 를
# 수동 하향해야 하는 상태 = 자체완결 원칙 위반.
# 해결: 정상 삽화 군집(density 1.3~2.4 且 cover ≤0.36)과 거대표 군집은 잘 분리돼 있으므로,
# 원래 both-axes 게이트에 더해 **한 축이 정상 범위를 크게 초과**하는 경우를 추가로 채택한다:
#   (A) density >= DENSITY_HI(기본 5.0, 정상 최대 2.4의 2배 이상) 이고 cover >= 0.15
#   (B) cover >= COVER_HI(기본 0.45, 정상 최대 0.36 초과) 이고 density >= 2.8
# 두 보조 분기 모두 정상 삽화 봉투(density≤2.4·cover≤0.36)를 여유 있게 벗어난 값만
# 잡으므로, 순수 벡터 GFM 표(raster 이미지 없음 → 이 경로 원천 미진입)는 물론 정상 삽화
# 문서도 과크롭되지 않는다(실측 p16-18=이미지 0개, p98-102=cover≤0.21·density 1.57 → NEW 0건).
OVERSIZED_DENSITY_HI_DEFAULT = 5.0   # 이 밀도 이상이면 cover 문턱을 크게 완화(단일축 채택)
OVERSIZED_DENSITY_HI_MIN_COVER = 0.15  # 밀도-우세 분기의 최소 점유율(아이콘/스파크라인 배제)
OVERSIZED_COVER_HI_DEFAULT = 0.45    # 이 점유율 이상이면 density 문턱을 완화(단일축 채택)
OVERSIZED_COVER_HI_MIN_DENSITY = 2.8   # 점유율-우세 분기의 최소 밀도(정상 삽화 2.4 초과)


def _is_oversized_matrix_enabled() -> bool:
    """EXTRACT_OVERSIZED_MATRIX_TABLES 가 truthy 면 raster 오버사이즈 행렬표 검출 활성
    (기본 OFF). 기본 OFF 이면 detect_oversized_matrix_tables 가 호출되지 않아 기존과
    byte-identical 보존(회귀 0). EXTRACT_FIGURES 경로 안에서만 평가된다.
    """
    return os.getenv("EXTRACT_OVERSIZED_MATRIX_TABLES", "").strip().lower() in _DIAG_TRUTHY


def _oversized_min_density() -> float:
    """오버사이즈 행렬표 판정 최소 px 밀도(px/pt, 짧은 축 기준). env
    EXTRACT_OVERSIZED_IMG_MIN_DENSITY. 기본 3.5 — 실측 정상 삽화(1.3~2.4)와 오버사이즈
    표(4.4~6.9) 사이 안전마진."""
    try:
        return float(os.getenv("EXTRACT_OVERSIZED_IMG_MIN_DENSITY", "3.5"))
    except ValueError:
        return 3.5


def _oversized_min_cover() -> float:
    """오버사이즈 행렬표 판정 최소 페이지 점유율. env EXTRACT_OVERSIZED_IMG_MIN_COVER.
    기본 0.30 — 실측 정상 삽화(≤0.36 이지만 밀도가 낮아 AND 게이트로 배제)와 오버사이즈
    표(0.32~0.51) 분리."""
    try:
        return float(os.getenv("EXTRACT_OVERSIZED_IMG_MIN_COVER", "0.30"))
    except ValueError:
        return 0.30


def _oversized_density_hi() -> float:
    """극단 밀도-우세 단일축 채택 문턱. env EXTRACT_OVERSIZED_IMG_DENSITY_HI(기본 5.0)."""
    try:
        return float(os.getenv("EXTRACT_OVERSIZED_IMG_DENSITY_HI",
                               str(OVERSIZED_DENSITY_HI_DEFAULT)))
    except ValueError:
        return OVERSIZED_DENSITY_HI_DEFAULT


def _oversized_cover_hi() -> float:
    """극단 점유율-우세 단일축 채택 문턱. env EXTRACT_OVERSIZED_IMG_COVER_HI(기본 0.45)."""
    try:
        return float(os.getenv("EXTRACT_OVERSIZED_IMG_COVER_HI",
                               str(OVERSIZED_COVER_HI_DEFAULT)))
    except ValueError:
        return OVERSIZED_COVER_HI_DEFAULT


def _accept_oversized(density: float, cover: float) -> bool:
    """거대 매트릭스 표 이미지 채택 판정(3분기 OR — 위 '극단 단일축 게이트' 주석 참조).

    (0) 원래 both-axes 게이트: density>=min_density 且 cover>=min_cover — 기존 동작 보존.
    (A) 밀도-우세: density>=density_hi 且 cover>=DENSITY_HI_MIN_COVER — 초고밀도 표(정상 삽화
        밀도 최대 2.4를 크게 상회)를 낮은 점유율에서도 채택(예 p2: 6.91/0.274).
    (B) 점유율-우세: cover>=cover_hi 且 density>=COVER_HI_MIN_DENSITY — 페이지 대부분을 덮는
        표(정상 삽화 점유율 최대 0.36 초과)를 낮은 밀도에서도 채택(예 p5/p6: 3.14/0.60~0.71).
    (A)(B) 는 원래 게이트에 detection 을 **추가만** 하며, 정상 삽화 봉투(density<=2.4·
    cover<=0.36)는 세 분기 모두 미충족이라 회귀 0.
    """
    min_density = _oversized_min_density()
    min_cover = _oversized_min_cover()
    if density >= min_density and cover >= min_cover:
        return True
    if density >= _oversized_density_hi() and cover >= OVERSIZED_DENSITY_HI_MIN_COVER:
        return True
    if cover >= _oversized_cover_hi() and density >= OVERSIZED_COVER_HI_MIN_DENSITY:
        return True
    return False


def _watermark_xrefs(doc, min_pages: int = OVERSIZED_WATERMARK_MIN_PAGES) -> set[int]:
    """문서 전체에서 ``min_pages`` 페이지 이상 반복 등장하는 이미지 xref 집합(워터마크/
    배경 이미지로 간주해 오버사이즈 행렬표 후보에서 항상 제외 — opt-out 불가).

    같은 xref 가 여러 페이지에 반복 삽입되는 것은 로고·배경워터마크의 전형적 패턴이며,
    설령 밀도/점유율 임계를 만족하더라도 표가 아니므로 무조건 배제한다. 밀도가 낮은
    워터마크는 이미 _oversized_min_density() 로 걸러지지만, 고밀도 반복 이미지(예: 고해상도
    배경 워터마크)에 대한 방어선을 이중으로 둔다.
    """
    counts: dict[int, int] = {}
    for page in doc:
        try:
            xrefs_on_page = {im.get("xref") for im in page.get_image_info(xrefs=True)}
        except Exception:  # noqa: BLE001
            continue
        for x in xrefs_on_page:
            if x is None:
                continue
            counts[x] = counts.get(x, 0) + 1
    return {x for x, c in counts.items() if c >= min_pages}


def detect_oversized_matrix_tables(
    page, exclude_xrefs: Optional[set[int]] = None
) -> list[list[float]]:
    """표 전체가 고해상도 raster 이미지로 삽입된 '오버사이즈 행렬표' bbox 리스트(PDF pt).

    결정적 판정(LLM 비의존): 페이지에 배치된 각 raster 이미지에 대해 밀도(px/pt, 짧은 축
    기준)·페이지 점유율을 계산하고 ``_accept_oversized()`` 3분기 게이트로 채택한다(both-axes
    슬램덩크 + 극단 단일축 A/B — 위 '극단 단일축 게이트' 주석 참조). 워터마크(반복 xref)는
    항상 제외한다. 표 이미지 자체가 이미 표 전체 영역이므로 bbox 는 검출된 이미지 bbox 를
    그대로 반환한다(서브박스로 축소하지 않음 — 크롭 시 표 전체가 잘리지 않도록 보수적으로
    전체 이미지 영역을 우선).
    """
    pw, ph = page.rect.width, page.rect.height
    if pw <= 0 or ph <= 0:
        return []
    exclude = exclude_xrefs or set()
    out: list[list[float]] = []
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001
        return []
    for im in infos:
        xref = im.get("xref")
        if xref in exclude:
            continue
        bbox = im.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            continue
        cover = _area_cover([x0, y0, x1, y1], pw, ph)
        pxw, pxh = im.get("width", 0), im.get("height", 0)
        if pxw <= 0 or pxh <= 0:
            continue
        density = min(pxw / w, pxh / h)
        if _accept_oversized(density, cover):
            out.append([x0, y0, x1, y1])
    return out


def _is_figure_describe_enabled() -> bool:
    """EXTRACT_FIGURE_DESCRIBE 가 truthy 면 크롭 figure 에 qwen3-vl 비전문가 설명 생성.

    기본 OFF(회귀 0): describe 호출이 전혀 없어 주입 MD 는 캡션만(하위호환).
    사이드카 항목은 기존 키를 모두 보존한 채 빈 description 필드가 additive 추가된다.
    ON 이면 크롭된 각 figure/complex_table PNG 에 대해 로컬
    qwen3-vl:32b 로 '쉬운 설명'을 생성해 사이드카 항목에 description 필드를 추가한다.
    """
    return os.getenv("EXTRACT_FIGURE_DESCRIBE", "").strip().lower() in _DIAG_TRUTHY


def _is_describe_complex_tables_enabled() -> bool:
    """EXTRACT_DESCRIBE_COMPLEX_TABLES 가 truthy 면 complex_table(사선표/오버사이즈
    행렬표) 크롭은 **전역 EXTRACT_FIGURE_DESCRIBE 가 꺼져 있어도** 독립적으로 설명을
    생성한다(기본 OFF). 오버사이즈 행렬표는 텍스트로 전사 불가능한 유일한 정보원이라
    describe 가치가 크므로 전역 스위치와 분리된 별도 게이트를 둔다.
    """
    return os.getenv("EXTRACT_DESCRIBE_COMPLEX_TABLES", "").strip().lower() in _DIAG_TRUTHY


def _describe_complex_max_tokens() -> int:
    """complex_table(사선표/오버사이즈 행렬표) describe 전용 max_tokens.

    오버사이즈 행렬표는 컬럼·행 수가 많아 설명이 길어지므로 일반 figure 기본값(8192)
    으로는 thinking 예산 소진 후 잘릴 위험이 있다 → 기본 16384. env
    EXTRACT_FIGURE_DESC_MAX_TOKENS_COMPLEX 로 override 가능.
    """
    try:
        return int(os.getenv("EXTRACT_FIGURE_DESC_MAX_TOKENS_COMPLEX", "16384"))
    except ValueError:
        return 16384


def _exclude_plain_tables() -> bool:
    """순수 격자 표(LLM 검출 type=="table")를 이미지 크롭에서 제외할지(기본 ON).

    기본 ON: 일반 표는 markdown GFM 표로만 전사하고 이미지로 크롭하지 않는다(작업 B).
    롤백용 opt-out: EXTRACT_KEEP_TABLE_FIGURES 가 truthy 면 기존 동작(LLM 이 table 로
    준 것도 크롭). 사선/복잡표(detect_complex_tables 의 벡터 경로, type="complex_table")
    는 본 게이트와 무관하게 항상 유지된다 — 순수 격자 표만 배제한다.
    """
    return os.getenv("EXTRACT_KEEP_TABLE_FIGURES", "").strip().lower() not in _DIAG_TRUTHY


def _figure_detect_mode() -> str:
    """figure 검출 모드 반환 (Fix 2). env EXTRACT_FIGURE_DETECT.

    - "hybrid"(기본): 결정적 후보(캡션↔raster/vector 페어링) 우선. 결정적 0건 AND
      페이지에 강한 raster/vector 신호가 있을 때만 LLM detect 폴백.
    - "deterministic": 결정적만(LLM 절대 미호출).
    - "llm": 레거시(항상 LLM detect).
    미인식 값은 hybrid 로 폴백.
    """
    m = os.getenv("EXTRACT_FIGURE_DETECT", "hybrid").strip().lower()
    return m if m in ("hybrid", "deterministic", "llm") else "hybrid"


# ──────────────────────────────────────────────────────────────────────────────
# 캡션 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def match_figure_caption(text: str) -> Optional[str]:
    """text 에 'Figure N' 패턴이 있으면 정규화 라벨('Figure N')을, 없으면 None.

    'Table 5' 같은 비-figure 캡션 또는 캡션 없는 도형은 None 을 반환한다(엄격 배제용).
    """
    if not text:
        return None
    m = _FIGURE_NO_RE.search(text)
    if not m:
        return None
    return f"Figure {m.group(1)}"


# ──────────────────────────────────────────────────────────────────────────────
# 좌표 유틸 (PoC 이식)
# ──────────────────────────────────────────────────────────────────────────────

def _norm_to_pdf(bbox_norm: list[float], pw: float, ph: float) -> list[float]:
    """0~1000 정규화 bbox(좌상단 원점) → PDF pt 좌표(좌상단 원점, fitz 동일)."""
    x0, y0, x1, y1 = bbox_norm
    fx, fy = pw / 1000.0, ph / 1000.0
    rx0, rx1 = sorted((x0 * fx, x1 * fx))
    ry0, ry1 = sorted((y0 * fy, y1 * fy))
    rx0 = max(0.0, min(rx0, pw)); rx1 = max(0.0, min(rx1, pw))
    ry0 = max(0.0, min(ry0, ph)); ry1 = max(0.0, min(ry1, ph))
    return [rx0, ry0, rx1, ry1]


def _iou(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _area_cover(bbox: list[float], pw: float, ph: float) -> float:
    x0, y0, x1, y1 = bbox
    return (max(0.0, x1 - x0) * max(0.0, y1 - y0)) / (pw * ph)


def _pad(bbox: list[float], pad: float, pw: float, ph: float) -> list[float]:
    x0, y0, x1, y1 = bbox
    return [max(0.0, x0 - pad), max(0.0, y0 - pad),
            min(pw, x1 + pad), min(ph, y1 + pad)]


# ──────────────────────────────────────────────────────────────────────────────
# 1) LLM figure 검출 (PoC 재사용)
# ──────────────────────────────────────────────────────────────────────────────

_DETECT_PROMPT = (
    "You are a precise figure detector for PDF pages. "
    "Look at this single PDF page image. "
    "Find EVERY figure, diagram, schematic, block-diagram, flow-chart, "
    "timing-waveform, table, chart, or photo on the page. "
    "Do NOT include: body-text paragraphs, headings/titles, the page header logo, "
    "footers, or page numbers. "
    "Return ONLY a JSON array, no prose, no markdown fences. "
    "Each element MUST be: "
    '{"bbox":[x0,y0,x1,y1],"type":"diagram|schematic|block|flow|timing|table|chart|photo",'
    '"caption":"the figure caption text if visible else short description"}. '
    "Coordinates: normalized integers 0-1000, origin TOP-LEFT, x0<x1, y0<y1, "
    "x grows rightward, y grows downward. "
    "Make each bbox tight around the figure (include its caption line). "
    "If the page has no figure at all, return []."
)


def _strip_json_fence(text: str) -> str:
    """```json ... ``` fence 제거 + 첫 '[' ~ 마지막 ']' 만 추출(앞뒤 산문 방어)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    t = t.strip()
    if "[" in t and "]" in t:
        start = t.index("[")
        end = t.rindex("]")
        if end > start:
            t = t[start:end + 1]
    return t


def _detect_max_tokens() -> int:
    """figure 검출(detect_figures_llm) max_tokens 기본값. qwen3-vl:32b 은 thinking 모델이라
    2048 은 부족해 빈 응답→도면 누락(2026-07-01 실측 p13/15/16 등). 기본 8192, env override
    (EXTRACT_FIGURE_DETECT_MAX_TOKENS). describe(_describe_max_tokens)와 동일 취지."""
    try:
        return max(2048, int(os.getenv("EXTRACT_FIGURE_DETECT_MAX_TOKENS", "8192")))
    except ValueError:
        return 8192


def detect_figures_llm(images_b64: list[str], max_tokens: Optional[int] = None) -> tuple[str, list[dict]]:
    """단일 페이지 이미지에 대해 LLM figure 검출 → (원응답, 파싱된 박스 리스트).

    파싱 실패 시 (원응답, []) 반환 — 호출부가 빈 페이지로 처리. PoC 와 동일 계약.
    max_tokens None 이면 _detect_max_tokens()(기본 8192, thinking 예산 확보).
    """
    if max_tokens is None:
        max_tokens = _detect_max_tokens()
    # role="figure_detect": 도메인 OCR 모델(glm-ocr 등)을 우회하고 항상 구조 모델(qwen)
    # 사용. GLM-OCR 은 OCR 특화라 JSON bbox 프롬프트를 못 따를 위험 → 도면 검출은 qwen 유지.
    raw = ox._ollama_vision(
        _DETECT_PROMPT, images_b64, image_mime="image/png", max_tokens=max_tokens,
        role="figure_detect",
    )
    cleaned = _strip_json_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return raw, []
    if not isinstance(parsed, list):
        return raw, []
    boxes: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        if (
            isinstance(bbox, list)
            and len(bbox) == 4
            and all(isinstance(v, (int, float)) for v in bbox)
        ):
            boxes.append({
                "bbox": [float(v) for v in bbox],
                "type": str(item.get("type", "figure")),
                "caption": str(item.get("caption", "")).strip(),
            })
    return raw, boxes


# ──────────────────────────────────────────────────────────────────────────────
# 1b) figure 비전문가용 설명 생성 (opt-in, EXTRACT_FIGURE_DESCRIBE=1)
# ──────────────────────────────────────────────────────────────────────────────
# 크롭된 도면/차트/사선표 이미지를 로컬 qwen3-vl:32b 에 주고, 비전문가가 이해할 수 있는
# 쉬운 설명(요약 + 자세한 풀이 + 용어 풀이)을 생성한다. 본문 전사(도메인 라우팅=glm-ocr)
# 는 그대로 두고, 그림 해석만 qwen 으로 보강한다(모델 고정). ANTI-FABRICATION: 보이는
# 사실만, 판독 불가는 지어내지 말고 명시하도록 프롬프트로 강제. localhost 게이트웨이만
# 사용하므로 외부 연결 0(회귀·보안 안전).
# 출력 언어는 FMDW_DESCRIBE_LANG(기본 "en")로 선택한다: 기본값 "en"이면 영어 설명이 그대로
# 최종 산출물(ollama 경로는 영어 프롬프트로 직접 생성, mlx 경로는 2-pass 한국어 번역 생략).
# "ko"로 지정하면 기존처럼 한국어(ollama 경로는 한국어 프롬프트, mlx 경로는 영어 describe 후
# 번역)로 출력한다.

_DESCRIBE_PROMPT_KO = (
    "당신은 회로도·블록도·타이밍도·차트·표 등 기술 그림을 '비전문가'에게 쉽게 풀어 "
    "설명하는 전문가입니다. 아래 이미지 한 장을 보고, 한국어로 다음 세 부분을 순서대로 "
    "작성하세요.\n"
    "1) 한 줄 요약: 이 그림이 '무엇'인지 평범한 말로 한 문장.\n"
    "2) 쉬운 설명: 그림에 실제로 보이는 구성요소·연결·신호 흐름·역할을 일상 언어로 "
    "자세히. 초보자가 이해할 수 있게 단계적으로 풀어 쓰세요. 만약 이 그림이 회로도"
    "(schematic)·배선도·PCB 도면이라면, 쉬운 설명에 더해 '확실히 판독되는' 주요 부품만 "
    "'부품 참조번호(R1·C3·U2·J4 등) = 그 부품이 하는 일' 형태로 목록화하세요. "
    "★번호를 추측하거나 연속으로 채우지 마세요 — 보이는 부품이 C22 다음 C34면 C23~C33을 "
    "지어내지 말고, 흐릿하거나 불확실한 번호·값은 아예 적지 말며 필요하면 '[일부 부품 판독 "
    "불가]'로만 표기하세요. 정확성(환각 0)이 완전성보다 절대 우선입니다 — 가짜 부품 1개가 "
    "누락 10개보다 훨씬 나쁩니다. 아래 [매우 중요] 규칙이 항상 우선하며, 실제로 보이는 "
    "부품만 적고 없는 부품을 만들어내지 마세요.\n"
    "3) 용어 풀이: 그림에 나온 전문용어·약어를 '약어(풀이)' 형식으로 정리. 없으면 생략.\n\n"
    "[매우 중요 — 지어내지 말 것] 이미지에서 실제로 보이는 사실만 쓰세요. 글자가 흐리거나 "
    "판독이 불가능하면 추측하지 말고 '이 부분은 판독 불가'라고 명시하세요. 보이지 않는 "
    "값·부품명·수치·연결을 임의로 만들어내지 마세요. 마크다운 제목(#)은 쓰지 말고 위 "
    "1) 2) 3) 형식의 본문 텍스트만 출력하세요."
)

#: FMDW_DESCRIBE_LANG=en(기본)일 때 쓰는 영어 설명 프롬프트. _DESCRIBE_PROMPT_KO 와 구조·
#: anti-fabrication 제약은 동일하고 출력 언어만 영어.
_DESCRIBE_PROMPT_EN = (
    "You are an expert at explaining technical diagrams (schematics, block diagrams, timing "
    "diagrams, charts, tables, etc.) to a non-expert in plain terms. Looking at the single image "
    "below, write the following three parts in order, in English.\n"
    "1) One-line summary: a plain sentence describing what this figure is.\n"
    "2) Easy explanation: describe in detail, in everyday language, the components/connections/"
    "signal flow/role that are actually visible in the image. Break it down step by step so a "
    "beginner can understand. If this figure is a circuit schematic/wiring diagram/PCB layout, in "
    "addition to the easy explanation, list only the 'clearly readable' major components as "
    "'component reference (R1, C3, U2, J4, etc.) = what that component does'. "
    "★Do not guess numbers or fill in sequential ranges — if the visible parts jump from "
    "C22 to C34, do not invent C23-C33; do not write blurry or uncertain numbers/values at all, and "
    "if needed mark them only as '[some components unreadable]'. Accuracy (zero hallucination) "
    "always takes priority over completeness — one fake component is far worse than ten "
    "omissions. The [CRITICAL] rule below always takes precedence, and only components that are "
    "actually visible should be written — never invent parts that are not there.\n"
    "3) Glossary: list technical terms/abbreviations that appear in the figure as 'abbreviation "
    "(plain meaning)'. Omit if none appear.\n\n"
    "[CRITICAL — do not fabricate] Write only facts actually visible in the image. If text is "
    "blurry or unreadable, do not guess — state clearly that 'this part is unreadable'. Do not "
    "invent values, component names, numbers, or connections that are not visible. Do not use "
    "markdown headings (#); output only the body text in the 1) 2) 3) format above."
)


#: describe 호출 max_tokens 기본값. qwen3-vl:32b 은 thinking 모델이라 예산이 작으면
#: (1536·2048·4096 실측 모두) thinking 단계에서 소진돼 content 가 빈 응답으로 온다.
#: 본문 전사(role='structure')가 쓰는 모듈 기본과 동일하게 8192 를 줘야 thinking 이
#: 끝나고 실제 설명 텍스트가 나온다(실측: 8192 에서 정상 3-파트 설명 생성). env
#: EXTRACT_FIGURE_DESC_MAX_TOKENS 로 override 가능.
def _describe_max_tokens() -> int:
    try:
        return int(os.getenv("EXTRACT_FIGURE_DESC_MAX_TOKENS", "8192"))
    except ValueError:
        return 8192


# ── MLX(mlx-vlm) describe 백엔드 (2026-07-07) ──────────────────────────────────
#: FMDW_DESCRIBE_BACKEND=mlx 면 도면 설명을 로컬 Apple MLX(mlx-vlm)로 생성한다.
#: ollama qwen3-vl:32b(GGUF)는 이 맥(Apple Silicon)의 llama.cpp Metal 백엔드 미성숙으로
#: GPU 99% 풀가동인데도 hang(초저속)하므로(외부 이슈 llama.cpp#16895 등), MLX 로 우회한다.
#: 모델은 FMDW_MLX_DESCRIBE_MODEL(기본 Qwen3-VL-30B-A3B MoE 4bit, 실측 107 tok/s).
#: 무거운 모델 로드는 프로세스당 1회만 하고 전역 캐시로 재사용(도면 여러 개면 상각).
_MLX_DESCRIBE_CACHE = None

#: MLX 전용 describe 프롬프트(2026-07-07 실측 확정). Qwen3-VL-30B-A3B(4bit/8bit)는
#: '긴 프롬프트'와 '이미지+한국어 출력' 조합에서 빈 응답을 내는 특성이 있다(_DESCRIBE_PROMPT_KO
#: 한국어 795자→0자, 긴 영어→0자, fig에 따라 짧은 한국어도 0자). 반면 '짧은 영어 1문장'은
#: 모든 도면에서 안정적으로 상세 설명을 낸다(fig1 1263자·fig2 1379자 실측). 따라서 MLX
#: describe 는 항상 짧은 영어로 뽑는다. FMDW_DESCRIBE_LANG(기본 "en")이 "en"이면 이 영어
#: 결과를 그대로 최종 산출물로 쓰고, "ko"일 때만 별도 번역 단계(_translate_ko_via_ollama)로
#: 한국어화한다.
_DESCRIBE_PROMPT_MLX = (
    "Describe this technical diagram in detail, including all visible shapes, labels, "
    "arrows, text, and their spatial relationships."
)

#: 재시도용 '더 짧은' 폴백 프롬프트(2026-07-07 실측). 일부 도면(예 fig2)은 긴 프롬프트에서
#: 빈/붕괴 응답을 내지만 짧은 1문장에서는 안정적으로 성공한다. 재시도마다 점점 짧게 바꾼다.
_MLX_DESCRIBE_FALLBACK_PROMPTS = [
    "Describe this technical diagram in detail, including all visible shapes, labels, arrows, and text.",
    "Describe this diagram in detail.",
]


def _describe_backend() -> str:
    return os.getenv("FMDW_DESCRIBE_BACKEND", "ollama").strip().lower()


def _describe_lang() -> str:
    """FMDW_DESCRIBE_LANG: figure 설명 출력 언어(기본 "en").

    "en"(기본): 영어 설명을 그대로 최종 산출물로 사용 — ollama 경로는 영어 프롬프트로
    직접 생성하고, mlx 경로는 5b) 2-pass 한국어 번역 단계를 건너뛴다.
    "ko": 기존 동작 유지 — ollama 경로는 한국어 프롬프트, mlx 경로는 영어 describe 후
    _translate_ko_via_ollama 로 한국어 번역.
    """
    return os.getenv("FMDW_DESCRIBE_LANG", "en").strip().lower()


def _is_degenerate_text(t: str) -> bool:
    """설명/번역 결과가 실패(빈 응답·퇴행 반복)인지 판정.

    ox._has_degenerate_repetition 은 라인/블록 반복을 잡지만, 단일 문자 과다 반복
    (예 '의의의…')은 놓친다(2026-07-07 fig2 실측). 이를 보완해, 너무 짧거나 고유
    문자 종류가 극히 적은 출력도 실패로 본다.
    """
    s = (t or "").strip()
    if len(s) < 20:  # 정상 도면 설명은 수백 자 — 너무 짧으면 실패
        return True
    if ox._has_degenerate_repetition(s):
        return True
    compact = re.sub(r"\s+", "", s)
    if len(compact) >= 15 and len(set(compact)) <= 4:  # 예 '의의의…'
        return True
    return False


# ── GPU 시간 분리(FMDW_GPU_SERIALIZE, 기본 ON — 2026-07-09 Metal OOM fix) ────────
# 실사고: fix-4b 로 qwen3-vl:8b(≈13.6GB)가 '실제로 동작'하게 되면서 ollama 상주
# (glm-ocr ≈4GB + qwen3-vl:8b ≈13.6GB [+ gemma4:31b ≈21GB]) 와 MLX 30B describe
# (≈17GB) 가 동시에 GPU 를 요구 → mlx snapshot 로드 중 "[METAL] Insufficient Memory"
# 크래시. 해법 = 기존 MLX↔gemma OOM 과 동일한 '시간 분리':
#   body OCR(ollama) → crop(CPU) → [ollama 전체 언로드] → MLX describe →
#   [MLX 언로드] → inject/save → 다음 파일.
# FMDW_GPU_SERIALIZE=0 이면 기존 동작 그대로(회귀 탈출구).


def _gpu_serialize_enabled() -> bool:
    return os.getenv("FMDW_GPU_SERIALIZE", "1").strip().lower() not in ("0", "false", "no")


def _unload_ollama_models(base_url: str = "http://localhost:11434",
                          wait_s: float = 20.0) -> None:
    """MLX 로드 직전 ollama 상주 모델 전부 언로드(keep_alive=0) — best-effort.

    /api/ps 로 상주 모델을 조회하고 각 모델에 /api/generate {keep_alive:0} 를 보낸 뒤,
    /api/ps 가 비워질 때까지 최대 wait_s 초 대기한다. 어떤 실패도 치명적이지 않다 —
    경고 로그만 남기고 진행(언로드 실패 시 MLX 로드가 OOM 날 수 있으나, 그것은
    현행과 동일한 최악 케이스일 뿐 새 실패 모드가 아님).
    """
    try:
        import httpx
    except Exception:  # noqa: BLE001 — httpx 미설치면 언로드 skip(비차단)
        _log.warning("[GPU-SERIALIZE] httpx 미설치 — ollama 언로드 생략")
        return
    try:
        r = httpx.get(f"{base_url}/api/ps", timeout=10)
        models = [
            (m.get("name") or m.get("model") or "").strip()
            for m in (r.json().get("models") or [])
        ]
        models = [m for m in models if m]
    except Exception as e:  # noqa: BLE001
        _log.warning("[GPU-SERIALIZE] ollama /api/ps 조회 실패(무시, 진행): %s", e)
        return
    if not models:
        print("    [GPU-SERIALIZE] ollama 상주 모델 없음 — 계속 진행", flush=True)
        return
    print(f"    [GPU-SERIALIZE] MLX 로드 전 ollama 언로드 요청: {models}", flush=True)
    for name in models:
        try:
            # /api/generate + keep_alive:0 = 공식 즉시 언로드 계약(프롬프트 없음 = 생성 0).
            httpx.post(
                f"{base_url}/api/generate",
                json={"model": name, "keep_alive": 0},
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("[GPU-SERIALIZE] %s 언로드 요청 실패(무시): %s", name, e)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/ps", timeout=10)
            if not (r.json().get("models") or []):
                print("    [GPU-SERIALIZE] ollama 전 모델 언로드 확인 — 계속 진행",
                      flush=True)
                return
        except Exception as e:  # noqa: BLE001
            _log.warning("[GPU-SERIALIZE] 언로드 확인 조회 실패(무시, 진행): %s", e)
            return
        time.sleep(1.0)
    _log.warning("[GPU-SERIALIZE] %.0fs 내 ollama 언로드 미확인 — 그대로 진행(비차단)",
                 wait_s)


def _get_mlx_describe():
    """(model, processor, config) 를 1회 로드해 전역 캐시로 재사용."""
    global _MLX_DESCRIBE_CACHE
    if _MLX_DESCRIBE_CACHE is None:
        # GPU 시간 분리(2026-07-09): 17GB MLX 로드 전에 ollama 상주 모델(글로벌 ≈18~38GB)
        # 을 비워 Metal OOM 을 방지. FMDW_GPU_SERIALIZE=0 이면 기존 동작.
        if _gpu_serialize_enabled():
            _unload_ollama_models()
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        model_id = os.getenv(
            "FMDW_MLX_DESCRIBE_MODEL",
            "mlx-community/Qwen3-VL-30B-A3B-Instruct-4bit",
        )
        model, processor = load(model_id)
        config = load_config(model_id)
        _MLX_DESCRIBE_CACHE = (model, processor, config)
    return _MLX_DESCRIBE_CACHE


def _describe_via_mlx(image_b64: str, prompt: str, max_tokens: int) -> str:
    """base64 PNG 를 임시파일로 저장 후 mlx-vlm 으로 설명 생성 → 텍스트."""
    import tempfile

    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    model, processor, config = _get_mlx_describe()
    # 작은 예산(예 700)에서 'This is not visible.' 류 오응답 실측 → 최소 2048 보장.
    mt = max(2048, int(max_tokens or 0))
    # 재시도 시 프롬프트를 점점 더 짧게 폴백 + 온도 상향. Qwen3-VL 4bit MoE 는 greedy 여도
    # 도면에 따라 비결정적으로 짧은 오응답('not visible')·퇴행 반복('의의의…')·빈 응답을
    # 내는데, 일부 도면은 짧은 1문장 프롬프트에서만 안정적으로 성공한다(fig2 실측).
    prompts = [prompt] + _MLX_DESCRIBE_FALLBACK_PROMPTS
    tmp = None
    text = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(base64.b64decode(image_b64))
            tmp = tf.name
        for attempt, pr in enumerate(prompts):
            temp = 0.0 if attempt == 0 else 0.5
            formatted = apply_chat_template(processor, config, pr, num_images=1)
            result = generate(
                model, processor, formatted, image=[tmp],
                max_tokens=mt, temperature=temp, verbose=False,
            )
            text = (getattr(result, "text", None) or "").strip()
            # 성공 판정: 충분히 길고, 'not visible' 오응답이 아니며, 퇴행 반복
            # (예 '의의의…')이 아닐 것. 어느 하나라도 걸리면 다음 폴백으로 재시도.
            if (
                len(text) >= 60
                and "not visible" not in text.lower()
                and not _is_degenerate_text(text)
            ):
                break
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return text


def _unload_mlx_describe() -> None:
    """전역 MLX describe 모델을 해제하고 Metal 캐시를 비운다(2-pass 의 pass 경계).

    describe(MLX vision)를 모두 끝낸 뒤 호출해 GPU 메모리를 반납해야, 이어지는
    한국어 번역(ollama 대형모델)이 Metal OOM 없이 로드된다.
    """
    global _MLX_DESCRIBE_CACHE
    if _MLX_DESCRIBE_CACHE is None:
        return
    _MLX_DESCRIBE_CACHE = None
    try:
        import gc

        import mlx.core as mx

        gc.collect()
        mx.clear_cache()
    except Exception as e:  # noqa: BLE001 — 캐시 정리 실패는 비차단
        _log.warning("MLX 언로드 중 캐시 정리 실패(무시): %s", e)


def _translate_ko_via_ollama(en_text: str) -> str:
    """영어 describe 결과를 로컬 ollama 로 한국어 번역(2026-07-07 실측 확정).

    MLX vision(Qwen3-VL)은 한국어 출력이 불안정해 describe 는 영어로 뽑고 여기서 번역한다.
    thinking 모델(qwen3 계열)은 num_predict 를 thinking 에 소진해 content 가 비므로, 번역
    모델은 gemma4:31b 기본 + num_predict 를 크게 준다(작으면 done_reason=length 로 빈 content).
    번역 실패 시 빈 문자열 → 호출부가 영어 원문으로 degrade.
    """
    try:
        import httpx
    except Exception:  # noqa: BLE001 — httpx 미설치면 번역 skip(영어 원문 유지, 산출물 보존)
        return ""

    model = os.getenv("FMDW_TRANSLATE_MODEL", "gemma4:31b")
    prompt = (
        "Translate the following English into natural Korean. "
        "Output only the Korean translation.\n\n" + en_text
    )
    nump = int(os.getenv("FMDW_TRANSLATE_NUM_PREDICT", "4000"))
    text = ""
    # 번역 모델이 비결정적으로 퇴행 반복('의의의…')·빈 응답을 낼 때가 있어(fig2 실측),
    # 반복/빈 응답이면 온도를 올려 최대 3회 재시도한다.
    for attempt in range(3):
        temp = 0.0 if attempt == 0 else 0.6
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_predict": nump, "temperature": temp},
        }
        try:
            r = httpx.post("http://localhost:11434/api/chat", json=payload, timeout=300)
            text = (r.json().get("message", {}).get("content", "") or "").strip()
        except Exception as e:  # noqa: BLE001 — 번역 실패는 영어 원문으로 degrade
            _log.warning("한국어 번역 실패(무시): %s", e)
            text = ""
        if text and not _is_degenerate_text(text):
            return text
    return text


def describe_figure_llm(
    image_b64: str, *, caption: str = "", max_tokens: Optional[int] = None
) -> str:
    """크롭된 figure 이미지(base64)를 로컬 qwen3-vl 로 비전문가용 설명 생성 → 텍스트.

    도메인 라우팅과 무관하게 model=OLLAMA_VISION_MODEL(기본은 config/env 로 결정) 고정.
    localhost 게이트웨이만 사용(외부 연결 0). 실패 시 예외는 호출부(_maybe_describe_figure)가
    흡수하고, figure_describe role 은 retries=0(fail-fast, R3 2026-07-09).
    max_tokens None 이면 _describe_max_tokens()(기본 8192, thinking 예산 확보).
    """
    if max_tokens is None:
        max_tokens = _describe_max_tokens()
    cap = (caption or "").strip()
    # 백엔드 분기(2026-07-07): mlx 면 MLX 전용 단일문단 프롬프트로 Apple MLX 호출,
    # 아니면 기존 ollama 경로(회귀 0). MLX 는 개행/특수문자에 취약해 캡션도 개행 없이 이어붙인다.
    if _describe_backend() == "mlx":
        # 2-pass(2026-07-07): 여기선 '짧은 영어' describe 만 반환하고, 한국어 번역은
        # extract_figures 가 모든 도면 describe 를 끝낸 뒤 MLX 를 언로드하고 일괄 수행한다.
        # (MLX 30B + 번역 31B 동시 GPU 로드 시 Metal OOM 실측 → 시간 분리로 회피.)
        # 캡션은 사이드카에 별도 보존되므로 프롬프트에 결합하지 않는다(결합 시 빈 응답 실측).
        _mlx = (_describe_via_mlx(image_b64, _DESCRIBE_PROMPT_MLX, max_tokens) or "").strip()
        # FIX C(2026-07-09): _describe_via_mlx 는 모든 재시도 소진 시 마지막(실패) 결과를
        # 그대로 반환한다 — 단어 하나('The'/'This')·퇴행 반복이 사이드카·본문에 저장되는
        # 사고(LN08LPU 24개 중 4개) 발생. 최종 결과가 degenerate 면 쓰레기 대신 "" 저장.
        # 주입부(inject_figure_refs_into_md)는 desc=="" 를 `if desc:` 로 스킵하므로 빈
        # 문단이 생기지 않는다(회귀 0).
        if _mlx and _is_degenerate_text(_mlx):
            _log.warning(
                "figure describe(mlx) 최종 결과가 degenerate → 빈 설명으로 폐기: %r",
                _mlx[:60],
            )
            return ""
        return _mlx
    lang = _describe_lang()
    prompt = _DESCRIBE_PROMPT_KO if lang == "ko" else _DESCRIBE_PROMPT_EN
    if cap:
        if lang == "ko":
            prompt += f"\n\n[참고] 원본에 적힌 캡션(맞으면 활용, 아니면 무시): {cap}"
        else:
            prompt += (
                f"\n\n[Note] Caption from the original document "
                f"(use if correct, ignore otherwise): {cap}"
            )
    raw = ox._ollama_vision(
        prompt, [image_b64], image_mime="image/png",
        max_tokens=max_tokens, model=ox.OLLAMA_VISION_MODEL, role="figure_describe",
    )
    _out = (raw or "").strip()
    # FIX C(2026-07-09): ollama 경로도 동일하게 degenerate 최종 결과는 "" 로 폐기(대칭).
    if _out and _is_degenerate_text(_out):
        _log.warning(
            "figure describe(ollama) 최종 결과가 degenerate → 빈 설명으로 폐기: %r",
            _out[:60],
        )
        return ""
    return _out


def _png_to_b64(png_path: Path) -> str:
    return base64.b64encode(Path(png_path).read_bytes()).decode("ascii")


def _maybe_describe_figure(
    png_path: Path, *, caption: str = "", item_type: str = "figure"
) -> str:
    """describe 활성 시에만 크롭 PNG 에 대한 설명 생성. 비활성/실패면 "" (하위호환).

    describe OFF(기본)면 즉시 "" → 사이드카 description 필드가 빈 값이라 주입 MD 는
    캡션만(기존 동작). 개별 그림 설명 실패는 로그만 남기고 빈 문자열로 degrade한다.

    item_type=="complex_table"(사선표/오버사이즈 행렬표) 인 경우:
      - 전역 EXTRACT_FIGURE_DESCRIBE 가 꺼져 있어도 EXTRACT_DESCRIBE_COMPLEX_TABLES 가
        켜져 있으면 독립적으로 describe 를 수행한다(OR 조건).
      - max_tokens 는 항상 _describe_complex_max_tokens()(기본 16384)를 사용한다 —
        어느 게이트로 트리거됐든 복잡 표는 설명이 길어 일반 figure 기본값(8192)보다
        큰 예산이 필요하다.
    """
    is_complex = item_type == "complex_table"
    enabled = _is_figure_describe_enabled() or (
        is_complex and _is_describe_complex_tables_enabled()
    )
    if not enabled:
        return ""
    try:
        max_tokens = _describe_complex_max_tokens() if is_complex else None
        return describe_figure_llm(
            _png_to_b64(png_path), caption=caption, max_tokens=max_tokens
        )
    except Exception as e:  # noqa: BLE001 — 설명 실패가 figure 추출을 깨지 않음
        _log.warning("figure 설명 생성 실패(무시): %s", e)
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# 2) 결정적 신호 추출 (PoC 이식)
# ──────────────────────────────────────────────────────────────────────────────

def raster_figure_rects(
    page, exclude_xrefs: Optional[set[int]] = None
) -> list[list[float]]:
    """삽입 raster 이미지 중 figure 후보(소형 로고 제외) bbox 리스트.

    [Fix A, 2026-07-04] exclude_xrefs: 문서 전체에서 반복(≥3페이지) 등장하는 배경/워터마크
    이미지 xref 집합. 해당 xref 이미지는 후보에서 제외한다 — 전 페이지 반복되는 '전면 배경
    raster'(예: LN08LPU Design Manual xref=10, cover 0.591)가 캡션↔도형 페어링에서 과대
    wrapping 후보로 항상 이겨 진짜 도형 2~3개를 하나로 붕괴시키는 사고를 근본 차단한다.
    반복 이미지만 제외하므로 1회성 도면(고유 xref)은 영향 없음(회귀 0).
    """
    pw, ph = page.rect.width, page.rect.height
    exclude = exclude_xrefs or set()
    rects: list[list[float]] = []
    for im in page.get_images(full=True):
        xref = im[0]
        if xref in exclude:
            continue
        try:
            for r in page.get_image_rects(xref):
                bbox = [r.x0, r.y0, r.x1, r.y1]
                if _area_cover(bbox, pw, ph) >= MIN_RASTER_PAGE_COVER:
                    rects.append(bbox)
        except Exception:  # noqa: BLE001
            continue
    return rects


def _drawing_bbox(d: dict) -> Optional[list[float]]:
    r = d.get("rect")
    if r is not None:
        try:
            return [r.x0, r.y0, r.x1, r.y1]
        except Exception:  # noqa: BLE001
            pass
    xs: list[float] = []
    ys: list[float] = []
    for it in d.get("items", []):
        for el in it[1:]:
            try:
                if hasattr(el, "x") and hasattr(el, "y"):
                    xs.append(el.x); ys.append(el.y)
                elif hasattr(el, "x0"):
                    xs += [el.x0, el.x1]; ys += [el.y0, el.y1]
            except Exception:  # noqa: BLE001
                continue
    if xs and ys:
        return [min(xs), min(ys), max(xs), max(ys)]
    return None


def _rects_close(a: list[float], b: list[float], gap: float) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(0.0, max(ax0, bx0) - min(ax1, bx1))
    dy = max(0.0, max(ay0, by0) - min(ay1, by1))
    return dx <= gap and dy <= gap


def _drop_isolated_hlines(
    boxes: list[list[float]], pw: float, ph: float
) -> list[list[float]]:
    """'고립된 full-width 수평 구분선'(러닝 헤더/푸터 선)을 클러스터 대상에서 제외.

    판정: 어떤 박스가 (a) 얇은 수평선(h<=HLINE_MAX_H_PT)이고 (b) full-width(폭 >=
    HLINE_FULLWIDTH_RATIO*page_width)이며 (c) 그 **양 끝점 근처에 닿는 수직선분이 하나도
    없으면** 고립 구분선으로 보고 제외한다. 닫힌 프레임의 top/bottom 수평선은 프레임 좌우
    수직선이 끝점에 닿아 보존되고(회귀 0), 헤더/푸터 구분선만 걸러진다. 도형 내부의
    페이지 폭 미만 수평선은 (b) full-width 게이트에서 애초에 대상이 아니라 무영향.

    보수적: 수직선이 **한쪽 끝점에라도** 닿으면 보존(부분적으로 닫힌 수평선 안전).
    """
    verticals: list[list[float]] = []
    for b in boxes:
        w, h = b[2] - b[0], b[3] - b[1]
        if w <= VLINE_MAX_W_PT and h > VLINE_MAX_W_PT:
            verticals.append(b)

    full_w = HLINE_FULLWIDTH_RATIO * pw
    kept: list[list[float]] = []
    for b in boxes:
        w, h = b[2] - b[0], b[3] - b[1]
        is_full_hline = h <= HLINE_MAX_H_PT and w >= full_w
        if is_full_hline:
            hy = (b[1] + b[3]) / 2.0
            has_vertical_at_end = False
            for v in verticals:
                vx = (v[0] + v[2]) / 2.0
                near_end = (abs(vx - b[0]) <= HLINE_ENDPOINT_X_TOL
                            or abs(vx - b[2]) <= HLINE_ENDPOINT_X_TOL)
                if not near_end:
                    continue
                # 수직선 y-range 가 수평선 y 에 닿는가(허용오차 포함).
                if v[1] - HLINE_ENDPOINT_Y_TOL <= hy <= v[3] + HLINE_ENDPOINT_Y_TOL:
                    has_vertical_at_end = True
                    break
            if not has_vertical_at_end:
                continue  # 고립 full-width 수평 구분선(헤더/푸터) → 클러스터 제외.
        kept.append(b)
    return kept


def vector_figure_clusters(page) -> list[list[float]]:
    """벡터 도형(get_drawings)을 공간 군집해 도면 영역 bbox 후보 리스트로 반환."""
    pw, ph = page.rect.width, page.rect.height
    boxes: list[list[float]] = []
    for d in page.get_drawings():
        bb = _drawing_bbox(d)
        if bb is None:
            continue
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        if w <= 0 and h <= 0:
            continue
        boxes.append(bb)

    # 고립 full-width 수평 구분선(러닝 헤더/푸터 선) 제외 → 도형 상단 크롭에 헤더 딸림 방지.
    boxes = _drop_isolated_hlines(boxes, pw, ph)

    if not boxes:
        return []

    clusters: list[dict] = []
    for bb in boxes:
        placed = False
        for cl in clusters:
            if _rects_close(cl["bbox"], bb, VECTOR_CLUSTER_GAP_PT):
                cl["bbox"] = [
                    min(cl["bbox"][0], bb[0]), min(cl["bbox"][1], bb[1]),
                    max(cl["bbox"][2], bb[2]), max(cl["bbox"][3], bb[3]),
                ]
                cl["count"] += 1
                placed = True
                break
        if not placed:
            clusters.append({"bbox": list(bb), "count": 1})

    changed = True
    while changed and len(clusters) > 1:
        changed = False
        merged: list[dict] = []
        used = [False] * len(clusters)
        for i in range(len(clusters)):
            if used[i]:
                continue
            cur = clusters[i]
            for j in range(i + 1, len(clusters)):
                if used[j]:
                    continue
                if _rects_close(cur["bbox"], clusters[j]["bbox"], VECTOR_CLUSTER_GAP_PT):
                    cur = {
                        "bbox": [
                            min(cur["bbox"][0], clusters[j]["bbox"][0]),
                            min(cur["bbox"][1], clusters[j]["bbox"][1]),
                            max(cur["bbox"][2], clusters[j]["bbox"][2]),
                            max(cur["bbox"][3], clusters[j]["bbox"][3]),
                        ],
                        "count": cur["count"] + clusters[j]["count"],
                    }
                    used[j] = True
                    changed = True
            used[i] = True
            merged.append(cur)
        clusters = merged

    result: list[list[float]] = []
    for cl in clusters:
        cover = _area_cover(cl["bbox"], pw, ph)
        if cl["count"] >= VECTOR_MIN_DRAWINGS or cover >= VECTOR_MIN_AREA_COVER:
            if cover < 0.95:  # 페이지 거의 전체 = 배경 프레임 → 제외
                result.append(cl["bbox"])
    return result


def caption_anchors(page) -> list[tuple[str, list[float]]]:
    """페이지에서 'Figure N' *캡션* 의 (라벨, bbox) 리스트(도형 페어링용 신호).

    [캡션 정리, 2026-07-04] _CAPTION_ANCHOR_RE 로 **줄머리 표준 캡션만** 인정한다:
      (a) 본문 문장 속 참조("... see Figure 11")는 줄머리가 아니라 배제.
      (b) 줄머리라도 문장형("Figure 4-3 illustrates ...")은 번호 뒤 소문자 이어짐으로 배제.
      (c) 같은 도형번호가 복수로 잡히면 가장 짧은 라벨 1개만 남겨 문장형/중복 캡션 제거.
    결과: "Figure 2: A width..."(콜론)·"Figure 4-1"(줄끝)은 남고, "Figure 4-3 illustrates
    ..."·"... see Figure 11" 은 버려진다.
    """
    raw: list[tuple[str, list[float], str]] = []
    try:
        d = page.get_text("dict")
    except Exception:  # noqa: BLE001
        return []
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            line_text = "".join(s.get("text", "") for s in line.get("spans", []))
            if not _CAPTION_ANCHOR_RE.match(line_text):
                continue
            key = match_figure_caption(line_text)
            if key is None:
                continue
            bb = line.get("bbox")
            if bb and len(bb) == 4:
                raw.append((line_text.strip(), [float(v) for v in bb], key))
    # (c) 같은 도형번호(key)당 가장 짧은 라벨(표준 캡션) 1개만 유지 → 문장형 중복 제거.
    best_by_key: dict[str, tuple[str, list[float]]] = {}
    for text, bb, key in raw:
        cur = best_by_key.get(key)
        if cur is None or len(text) < len(cur[0]):
            best_by_key[key] = (text, bb)
    return [(text, bb) for text, bb in best_by_key.values()]


def table_caption_anchors(page) -> list[tuple[str, list[float]]]:
    """페이지에서 'Table N' 텍스트의 (라벨, bbox) 리스트(복잡 표 caption 보강용)."""
    anchors: list[tuple[str, list[float]]] = []
    try:
        d = page.get_text("dict")
    except Exception:  # noqa: BLE001
        return anchors
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            line_text = "".join(s.get("text", "") for s in line.get("spans", []))
            if _TABLE_CAPTION_RE.search(line_text):
                bb = line.get("bbox")
                if bb and len(bb) == 4:
                    anchors.append((line_text.strip(), [float(v) for v in bb]))
    return anchors


# ──────────────────────────────────────────────────────────────────────────────
# 2b) 사선·비정형 표 결정적 검출 (opt-in, EXTRACT_DIAGONAL_TABLE=1)
# ──────────────────────────────────────────────────────────────────────────────
# get_drawings() 선분을 수평/수직/사선으로 분류 → 수평·수직선이 격자를 이루는 영역을
# 표 후보로 군집 → 그 영역 안에 사선 구분선이 있으면 '복잡 표'로 판정. 사선은 직선('l')
# 과 quad('qu') 모서리에서만 인정하고 bezier('c')는 제외(로고·곡선 오탐 방지). 격자 없는
# 사선(차트·회로도)은 표가 아니므로 군집 게이트(H>=3 & V>=3)에서 자연히 배제된다.

def _segment_kind(p0: tuple[float, float], p1: tuple[float, float]) -> Optional[str]:
    """선분을 'h'(수평)/'v'(수직)/'d'(사선) 또는 None(짧음/모호)으로 분류."""
    dx = p1[0] - p0[0]
    dy = p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length < min(DIAG_GRID_MIN_LEN, DIAG_SEG_MIN_LEN):
        return None
    adx, ady = abs(dx), abs(dy)
    if ady <= DIAG_HV_TOL and adx > DIAG_HV_TOL:
        return "h"
    if adx <= DIAG_HV_TOL and ady > DIAG_HV_TOL:
        return "v"
    if adx > DIAG_HV_TOL and ady > DIAG_HV_TOL:
        ang = math.degrees(math.atan2(ady, adx))
        if DIAG_MIN_ANGLE_DEG <= ang <= (90.0 - DIAG_MIN_ANGLE_DEG):
            return "d"
    return None


def _iter_drawing_segments(page):
    """get_drawings() → (p0, p1) 직선 선분 제너레이터.

    직선('l')·사각형('re' 4변)·quad('qu' 4변)만 산출. bezier 곡선('c')은 사선 오탐
    원인이라 의도적으로 제외(표 격자선/사선 구분선은 직선이다).
    """
    for d in page.get_drawings():
        for it in d.get("items", []):
            try:
                op = it[0]
                if op == "l":
                    p0, p1 = it[1], it[2]
                    yield (p0.x, p0.y), (p1.x, p1.y)
                elif op == "re":
                    r = it[1]
                    yield (r.x0, r.y0), (r.x1, r.y0)
                    yield (r.x1, r.y0), (r.x1, r.y1)
                    yield (r.x1, r.y1), (r.x0, r.y1)
                    yield (r.x0, r.y1), (r.x0, r.y0)
                elif op == "qu":
                    q = it[1]
                    pts = [(q.ul.x, q.ul.y), (q.ur.x, q.ur.y),
                           (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)]
                    for a in range(4):
                        yield pts[a], pts[(a + 1) % 4]
            except Exception:  # noqa: BLE001 — 비정형 item 은 건너뛰고 계속
                continue


def _seg_bbox(p0: tuple[float, float], p1: tuple[float, float]) -> list[float]:
    return [min(p0[0], p1[0]), min(p0[1], p1[1]),
            max(p0[0], p1[0]), max(p0[1], p1[1])]


def _bbox_overlap(a: list[float], b: list[float], margin: float = 2.0) -> bool:
    return not (a[2] < b[0] - margin or a[0] > b[2] + margin
                or a[3] < b[1] - margin or a[1] > b[3] + margin)


def detect_complex_tables(page) -> list[list[float]]:
    """사선(대각선) 구분선을 가진 '복잡 표' 영역 bbox 리스트(PDF pt)를 반환.

    결정적 판정: (1) 수평·수직선이 격자를 이루는 영역을 군집하고, (2) 그 영역 안에
    충분히 긴 사선이 존재하면 채택. 순수 H/V 격자(일반 표)는 사선이 없어 빈 리스트
    → 절대 이미지로 안 빠진다. 격자 없는 사선(차트/회로도)도 배제.
    """
    pw, ph = page.rect.width, page.rect.height
    if pw <= 0 or ph <= 0:
        return []
    h_segs: list[list[float]] = []
    v_segs: list[list[float]] = []
    d_segs: list[list[float]] = []
    for p0, p1 in _iter_drawing_segments(page):
        kind = _segment_kind(p0, p1)
        if kind is None:
            continue
        bb = _seg_bbox(p0, p1)
        if kind == "h":
            h_segs.append(bb)
        elif kind == "v":
            v_segs.append(bb)
        else:
            d_segs.append(bb)

    # 사선이 하나도 없으면 복잡 표 없음(일반 격자 표 fast-path → 회귀 안전).
    if not d_segs:
        return []
    grid = h_segs + v_segs
    if not grid:
        return []

    # 격자선 bbox 를 표 영역으로 공간 군집(figure 군집과 동일 gap-merge).
    clusters: list[list[float]] = []
    for bb in grid:
        placed = False
        for cl in clusters:
            if _rects_close(cl, bb, DIAG_CLUSTER_GAP_PT):
                cl[0] = min(cl[0], bb[0]); cl[1] = min(cl[1], bb[1])
                cl[2] = max(cl[2], bb[2]); cl[3] = max(cl[3], bb[3])
                placed = True
                break
        if not placed:
            clusters.append(list(bb))
    changed = True
    while changed and len(clusters) > 1:
        changed = False
        merged: list[list[float]] = []
        used = [False] * len(clusters)
        for i in range(len(clusters)):
            if used[i]:
                continue
            cur = clusters[i]
            for j in range(i + 1, len(clusters)):
                if used[j]:
                    continue
                if _rects_close(cur, clusters[j], DIAG_CLUSTER_GAP_PT):
                    cur = [min(cur[0], clusters[j][0]), min(cur[1], clusters[j][1]),
                           max(cur[2], clusters[j][2]), max(cur[3], clusters[j][3])]
                    used[j] = True
                    changed = True
            used[i] = True
            merged.append(cur)
        clusters = merged

    out: list[list[float]] = []
    for cl in clusters:
        nh = sum(1 for bb in h_segs if _bbox_overlap(bb, cl))
        nv = sum(1 for bb in v_segs if _bbox_overlap(bb, cl))
        if nh < DIAG_MIN_GRID_H or nv < DIAG_MIN_GRID_V:
            continue
        cover = _area_cover(cl, pw, ph)
        if cover < DIAG_TABLE_MIN_COVER or cover > DIAG_TABLE_MAX_COVER:
            continue
        nd = sum(
            1 for bb in d_segs
            if _bbox_overlap(bb, cl, margin=0.0)
            and math.hypot(bb[2] - bb[0], bb[3] - bb[1]) >= DIAG_SEG_MIN_LEN
        )
        if nd >= DIAG_MIN_DIAG_SEGS:
            out.append([cl[0], cl[1], cl[2], cl[3]])
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 3) 스냅 보정 (PoC 이식)
# ──────────────────────────────────────────────────────────────────────────────

def snap_bbox(
    llm_bbox_pdf: list[float],
    raster_rects: list[list[float]],
    vector_clusters: list[list[float]],
    pw: float,
    ph: float,
) -> tuple[list[float], str, Optional[float]]:
    """LLM bbox 를 가장 잘 겹치는 결정적 경계(raster > vector)에 스냅.

    Returns:
        (보정 bbox, source['raster'|'vector'|'llm'], 스냅 IoU 또는 None)
    """
    best_iou = 0.0
    best_box: Optional[list[float]] = None
    best_src = "llm"

    for rb in raster_rects:
        iou = _iou(llm_bbox_pdf, rb)
        if iou > best_iou:
            best_iou, best_box, best_src = iou, rb, "raster"
    for vb in vector_clusters:
        iou = _iou(llm_bbox_pdf, vb)
        if iou > best_iou:
            best_iou, best_box, best_src = iou, vb, "vector"

    if best_box is not None and best_iou >= SNAP_IOU_THRESHOLD:
        # 스냅된 raster/vector bbox 도 여백 없이 그대로 반환하면 도형 테두리가 크롭에서
        # 잘림(2026-07-03 실측) → llm 분기와 동일하게 패딩 적용(_pad 가 페이지 경계로
        # clamp 하므로 안전).
        return _pad(list(best_box), LLM_PADDING_PT, pw, ph), best_src, round(best_iou, 3)

    return _pad(llm_bbox_pdf, LLM_PADDING_PT, pw, ph), "llm", None


def deterministic_figure_candidates(
    page,
    raster_rects: list[list[float]],
    vector_clusters: list[list[float]],
    anchors: list[tuple[str, list[float]]],
    pw: float,
    ph: float,
) -> list[dict]:
    """LLM 비의존 결정적 도면 후보 (Fix 2, 2026-07-02).

    각 'Figure N' 캡션 앵커를 **raster_figure_rects + vector_figure_clusters 양쪽**의
    근접 후보와 페어링한다(vector-only 페어링 금지 → raster-only 도면 누락 방지).
    페어링은 snap_bbox 의 '가장 잘 맞는 결정적 경계' 개념을 캡션 근접(caption anchor 가
    보통 figure 바로 위/아래) 기준으로 재사용한다. 매칭된 도형 영역을 figure bbox 로 채택.

    Returns:
        page_candidates 와 동일 스키마 dict 리스트(bbox=PDF pt, page 는 caller 가 채움).
        raster/vector 신호가 없거나 앵커가 없으면 빈 리스트.
    """
    combined: list[tuple[list[float], str]] = (
        [(rb, "raster") for rb in raster_rects]
        + [(vb, "vector") for vb in vector_clusters]
    )
    candidates: list[dict] = []
    if not combined or not anchors:
        return candidates

    for label, abox in anchors:
        figure_no = match_figure_caption(label)
        if figure_no is None:
            continue  # 'Figure N' 아닌 앵커(Table 등)는 결정적 후보에서 배제.
        acx = (abox[0] + abox[2]) / 2.0
        acy = (abox[1] + abox[3]) / 2.0

        # (1) reasonable 후보: 캡션 중심이 도형 x범위(여백 포함) 안(in_x) & 세로간극이
        #     MAX_GAP 이내. (원래 페어링 채택 조건과 동일 — non-in_x 는 +1000 페널티로
        #     사실상 MAX_GAP 초과라 배제됐던 것을 명시적으로 in_x 필터로 표현.)
        reasonable: list[dict] = []
        for box, src in combined:
            bx0, by0, bx1, by1 = box
            in_x = bx0 - DET_CAPTION_X_TOL_PT <= acx <= bx1 + DET_CAPTION_X_TOL_PT
            if not in_x:
                continue
            dy = max(0.0, max(by0, abox[1]) - min(by1, abox[3]))
            if dy > DET_CAPTION_MAX_GAP_PT:
                continue
            area = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
            cover = _area_cover(box, pw, ph)
            wraps = by0 <= acy <= by1  # 캡션을 세로로 감싸는 후보인가.
            reasonable.append({
                "box": list(box), "src": src, "dy": dy,
                "area": area, "cover": cover, "wraps": wraps,
            })
        if not reasonable:
            continue  # 근접 도형 없음 → 이 캡션은 결정적 페어링 실패.

        # (2) [Fix B] 캡션을 감싸는 '과대 wrapping' 후보 후순위. 단 '더 작은 대안'이 실제
        #     존재할 때만 밀어낸다 → 유일 후보인 정당한 full-page 도면은 그대로 채택(회귀 0).
        min_small_area = min(
            (c["area"] for c in reasonable if c["cover"] <= DET_OVERSIZED_COVER),
            default=None,
        )

        def _is_oversized_wrapper(c: dict) -> bool:
            if not c["wraps"]:
                return False  # 캡션을 감싸지 않는 후보(위/아래 인접 tight 도형)는 정상.
            if c["cover"] > DET_OVERSIZED_COVER:
                return True   # page cover 과대 + 캡션 wrapping → 배경/전면 raster·거대 vector.
            if (min_small_area is not None
                    and c["area"] > min_small_area * DET_OVERSIZED_AREA_FACTOR):
                return True   # 더 작은 대안 대비 면적 과도 + wrapping → 상대적 과대.
            return False

        non_demoted = [c for c in reasonable if not _is_oversized_wrapper(c)]
        # 모두 과대 wrapping(=대안 없음)이면 그대로 유지(정당한 full-page 도면 안전장치).
        pool = non_demoted if non_demoted else reasonable

        # (3) dy 오름차순, 동점 시 면적 작은 쪽 우선(tight 도형 선택).
        pool.sort(key=lambda c: (round(c["dy"], 1), c["area"]))
        best = pool[0]
        best_box = list(best["box"])
        best_src = best["src"]

        # (4) [분할 타일 병합] 선택된 도형이 raster 이고, 같은 세로 밴드를 공유하며 가로로
        #     인접한 다른 raster 타일이 있으면 union 한다(한 도형이 좌우 타일로 쪼개져 절반만
        #     크롭되는 것 방지 — 실측 HSPICE p1 xref56/58). raster_rects 전체를 대상으로 하되
        #     (캡션 x정렬에서 벗어난 반대쪽 타일도 포함), y겹침·가로인접 게이트로 다른 세로
        #     밴드의 별개 도형은 절대 병합하지 않는다. raster 소스에만 적용(vector 군집은
        #     이미 내부 병합됨).
        if best_src == "raster":
            bx0, by0, bx1, by1 = best_box
            for rb in raster_rects:
                if rb == best["box"]:
                    continue
                rx0, ry0, rx1, ry1 = rb
                y_overlap = min(by1, ry1) - max(by0, ry0)
                min_h = min(by1 - by0, ry1 - ry0)
                x_gap = max(0.0, max(bx0, rx0) - min(bx1, rx1))
                if (min_h > 0
                        and y_overlap >= DET_TILE_UNION_Y_OVERLAP * min_h
                        and x_gap <= DET_TILE_UNION_GAP_PT):
                    bx0, by0 = min(bx0, rx0), min(by0, ry0)
                    bx1, by1 = max(bx1, rx1), max(by1, ry1)
            best_box = [bx0, by0, bx1, by1]

        if _area_cover(best_box, pw, ph) < MIN_FIGURE_AREA_COVER:
            continue  # 텍스트 라인/아이콘 크기 → 배제.
        snapped = _pad(best_box, LLM_PADDING_PT, pw, ph)
        candidates.append({
            "page": None,               # caller 가 page_no 로 채움.
            "_y": snapped[1],           # 정렬 키(상단 y).
            "bbox": [round(v, 1) for v in snapped],
            "type": "figure",
            "caption": label,
            "figure_no": figure_no,
            "source": best_src,
            "snap_iou": None,           # 결정적 페어링은 IoU 스냅 아님.
        })
    return candidates


# ──────────────────────────────────────────────────────────────────────────────
# 4) 크롭·저장 (PoC 이식)
# ──────────────────────────────────────────────────────────────────────────────

def _crop_and_save(page, bbox_pdf: list[float], out_path: Path, dpi: int) -> tuple[int, int]:
    """PDF bbox 영역을 dpi 로 렌더해 PNG 저장 → (width_px, height_px)."""
    import fitz

    zoom = dpi / 72.0
    clip = fitz.Rect(*bbox_pdf)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    return pix.width, pix.height


# ──────────────────────────────────────────────────────────────────────────────
# 메인 공개 API
# ──────────────────────────────────────────────────────────────────────────────

def extract_figures(
    pdf_path: Path,
    out_dir: Path,
    *,
    dpi: int = DEFAULT_DPI,
    strict_caption: bool = True,
) -> list[dict]:
    """`Figure N` 캡션 다이어그램만 검출·크롭. 반환 = `_figures.json` 항목 리스트.

    각 항목 스키마(설계 §6.1):
        {figure_id, page, image_path, caption, figure_no, type, bbox, source, snap_iou}

    Args:
        pdf_path: 원본 PDF 경로.
        out_dir: 출력 디렉터리. 크롭 PNG 는 ``out_dir/figures/<stem>__pNN_figK.png``,
                 사이드카는 ``out_dir/<stem>_figures.json`` 에 저장된다.
        dpi: 크롭 렌더 해상도(기본 300).
        strict_caption: True 면 'Figure N' 캡션이 매칭되는 후보만 채택(로고/표/캡션없는
            도형 배제). False 면 캡션 없는 후보도 채택(대조군).

    Returns:
        figure 항목 dict 리스트(사이드카 파일과 동일 내용). 0건이면 빈 리스트
        (사이드카는 빈 리스트로 생성 — 다운스트림이 항상 파일 존재를 가정 가능).
    """
    import fitz

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    stem = pdf_path.stem
    figures_dir = out_dir / "figures"

    # 워터마크 제거(opt-in, EXTRACT_REMOVE_WATERMARK=1): figure 크롭도 워터마크가
    # 제거된 임시 PDF에서 렌더해 crop PNG에 워터마크가 남지 않게 한다. 비활성(기본)
    # 이면 원본 경로 그대로 → 회귀 0.
    open_path = str(pdf_path)
    try:
        from . import watermark_remover as _wm
        open_path = _wm.maybe_clean_pdf(open_path)
    except Exception:  # noqa: BLE001 — 워터마크 모듈 문제로 figure 추출을 깨지 않음
        open_path = str(pdf_path)

    doc = fitz.open(open_path)
    items: list[dict] = []
    # 사선/비정형 표 → 이미지(opt-in). 기본 OFF 면 아래 표 경로 미진입 → 회귀 0.
    diag_enabled = _is_diagonal_table_enabled()
    # 오버사이즈 행렬표(raster) → 이미지(opt-in, diag_enabled 와 완전히 독립적인 게이트/
    # 탐지 경로). 기본 OFF 면 아래 표 경로 미진입 → 회귀 0. 워터마크 xref 집합은 문서
    # 전체를 1회 스캔해야 하므로(여러 페이지 반복 등장 판정), 게이트 켜졌을 때만 계산해
    # 비활성 시 오버헤드 0을 유지한다.
    om_enabled = _is_oversized_matrix_enabled()
    # [Fix A, 2026-07-04] 반복 배경/워터마크 raster xref 집합은 figure 후보 붕괴 방지를
    # 위해 **항상** 계산한다(과거엔 om_enabled 일 때만 계산). raster_figure_rects 에서 이
    # 집합을 제외해야 전 페이지 반복 배경 raster 가 과대 wrapping 후보로 도형을 붕괴시키는
    # 사고를 근본 차단할 수 있다. 오버헤드는 페이지당 get_image_info 1회(렌더 대비 미미).
    bg_xrefs: set[int] = _watermark_xrefs(doc)
    om_watermark_xrefs: set[int] = bg_xrefs  # 오버사이즈 행렬표 검출도 동일 집합 재사용.
    try:
        for pidx in range(doc.page_count):
            page_no = pidx + 1
            page = doc[pidx]
            pw, ph = page.rect.width, page.rect.height

            # 1) 결정적 신호(캡션/raster/vector) — LLM 무관하게 항상 먼저 계산(Fix 2).
            #    [Fix A] 반복 배경/워터마크 raster(bg_xrefs)는 후보에서 제외.
            r_rects = raster_figure_rects(page, exclude_xrefs=bg_xrefs)
            v_clusters = vector_figure_clusters(page)
            anchors = caption_anchors(page)
            mode = _figure_detect_mode()

            # page_fig_bboxes: 이 페이지에서 figure 로 채택된 bbox(복잡 표 dedup 용).
            page_fig_bboxes: list[list[float]] = []
            page_candidates: list[dict] = []
            exclude_tables = _exclude_plain_tables()

            # 2a) 결정적 후보(hybrid/deterministic): 캡션↔raster/vector 양쪽 페어링.
            det_candidates: list[dict] = []
            if mode in ("hybrid", "deterministic"):
                det_candidates = deterministic_figure_candidates(
                    page, r_rects, v_clusters, anchors, pw, ph
                )
                for c in det_candidates:
                    c["page"] = page_no
                page_candidates.extend(det_candidates)

            # 2b) LLM 검출 사용 여부:
            #   - "llm": 항상. "deterministic": 절대 미호출.
            #   - "hybrid"(기본): 결정적 0건 AND 강한 raster/vector 신호 존재 시에만 폴백.
            use_llm = mode == "llm" or (
                mode == "hybrid" and not det_candidates and bool(r_rects or v_clusters)
            )
            boxes: list[dict] = []
            if use_llm:
                # 검출용 페이지 렌더는 LLM 을 실제 쓸 때만(결정적 경로는 렌더 생략 → 속도).
                imgs = ox.render_pdf_pages_to_base64(
                    pdf_path, page_no, page_no, dpi=dpi, doc=doc
                )
                # figure_detect role → ollama_extractor 에서 timeout 단축+retries=0 캡(hang 차단).
                try:
                    _raw, boxes = detect_figures_llm(imgs)
                except Exception as e:  # noqa: BLE001
                    _log.warning("p%02d LLM 검출 실패 → 빈 페이지로 처리: %s", page_no, e)
                    boxes = []

            # 3~4) LLM 후보 스냅 + 캡션 엄격 필터 → page_candidates 에 추가.
            #   (hybrid 에서 det 성공 페이지는 use_llm=False 라 boxes 비어 있음 → det 만 채택,
            #    det↔llm 중복 없음. llm 모드는 det 비어 있어 llm 만 채택.)
            if boxes:
                for b in boxes:
                    # 작업 B: 순수 격자 표(LLM type=="table")는 이미지 크롭 제외(기본 ON).
                    #   → 일반 표는 markdown GFM 표로만 전사. 사선/복잡표는 별도 결정적
                    #   경로(detect_complex_tables, type="complex_table")로 처리되며 여기서
                    #   배제되지 않는다. 도면/schematic/chart 등 비-table 타입도 모두 유지.
                    if exclude_tables and str(b.get("type", "")).strip().lower() == "table":
                        continue
                    llm_pdf = _norm_to_pdf(b["bbox"], pw, ph)
                    if _area_cover(llm_pdf, pw, ph) < MIN_FIGURE_AREA_COVER:
                        continue
                    snapped, src, iou = snap_bbox(llm_pdf, r_rects, v_clusters, pw, ph)

                    # 캡션: LLM 캡션 우선, 없으면 스냅 bbox 와 겹치는 캡션 앵커 텍스트.
                    caption = b.get("caption", "").strip()
                    figure_no = match_figure_caption(caption)
                    if figure_no is None:
                        # 페이지 캡션 앵커 중 스냅 bbox 와 겹치는 'Figure N' 라인 보강.
                        anchor_text = _nearest_anchor_text(snapped, anchors)
                        if anchor_text:
                            figure_no = match_figure_caption(anchor_text)
                            if figure_no and not caption:
                                caption = anchor_text

                    if strict_caption and figure_no is None:
                        # 'Figure N' 매칭 실패 → 로고/표/캡션없는 도형 배제.
                        continue

                    page_candidates.append({
                        "page": page_no,
                        "_y": snapped[1],          # 정렬 키(상단 y).
                        "bbox": [round(v, 1) for v in snapped],
                        "type": b.get("type", "figure"),
                        "caption": caption,
                        "figure_no": figure_no or "",
                        "source": src,
                        "snap_iou": iou,
                    })

            # 5) 동일 페이지 다중 figure y 오름차순 정렬 후 fig1..figK (det+llm 통합).
            if page_candidates:
                page_candidates.sort(key=lambda c: c["_y"])
                for k, c in enumerate(page_candidates, start=1):
                    figure_id = f"{stem}__p{page_no:02d}_fig{k}"
                    rel_image_path = f"figures/{figure_id}.png"
                    png_path = figures_dir / f"{figure_id}.png"
                    _crop_and_save(page, c["bbox"], png_path, dpi)
                    page_fig_bboxes.append(c["bbox"])
                    # 작업 A: 크롭 PNG 에 대한 비전문가용 설명(opt-in). 비활성 시 "".
                    desc = _maybe_describe_figure(png_path, caption=c["caption"])
                    items.append({
                        "figure_id": figure_id,
                        "page": page_no,
                        "image_path": rel_image_path,
                        "caption": c["caption"],
                        "figure_no": c["figure_no"],
                        "type": c["type"],
                        "bbox": c["bbox"],
                        "source": c["source"],
                        "snap_iou": c["snap_iou"],
                        "description": desc,
                    })

            # page_table_bboxes: 이 페이지에서 complex_table 로 채택된 bbox(사선표 +
            # 오버사이즈 행렬표 공용 dedup 용 — 두 탐지기 자체는 독립이지만 동일 영역
            # 중복 크롭만 막는다).
            page_table_bboxes: list[list[float]] = []

            # 6) 사선·비정형 표 → 이미지(opt-in, LLM 독립). 일반 격자 표는 사선이 없어
            #    detect_complex_tables 가 빈 리스트 → 절대 이미지로 안 빠짐(회귀 0).
            if diag_enabled:
                try:
                    tbl_boxes = detect_complex_tables(page)
                except Exception as e:  # noqa: BLE001 — 표 검출 실패가 파이프라인 중단 X
                    _log.warning("p%02d 복잡 표 검출 실패(무시): %s", page_no, e)
                    tbl_boxes = []
                if tbl_boxes:
                    t_anchors = table_caption_anchors(page)
                    tk = 0
                    for cl in tbl_boxes:
                        # 이미 figure 로 채택된 영역과 크게 겹치면 중복이므로 skip.
                        if any(_iou(cl, fb) > 0.5 for fb in page_fig_bboxes):
                            continue
                        tk += 1
                        figure_id = f"{stem}__p{page_no:02d}_tbl{tk}"
                        rel_image_path = f"figures/{figure_id}.png"
                        padded = _pad(cl, DIAG_PAD_PT, pw, ph)
                        bbox_r = [round(v, 1) for v in padded]
                        png_path = figures_dir / f"{figure_id}.png"
                        _crop_and_save(page, bbox_r, png_path, dpi)
                        page_table_bboxes.append(bbox_r)
                        cap = _nearest_anchor_text(bbox_r, t_anchors) or ""
                        _log.info(
                            "p%02d 복잡 표(사선) → 이미지: %s bbox=%s",
                            page_no, figure_id, bbox_r,
                        )
                        # 작업 A: 사선/복잡표도 비전문가용 설명 생성(opt-in). 비활성 시 "".
                        desc = _maybe_describe_figure(
                            png_path, caption=cap, item_type="complex_table"
                        )
                        items.append({
                            "figure_id": figure_id,
                            "page": page_no,
                            "image_path": rel_image_path,
                            "caption": cap,
                            "figure_no": "",
                            "type": "complex_table",
                            "bbox": bbox_r,
                            "source": "diagonal_vector",
                            "snap_iou": None,
                            "description": desc,
                        })

            # 7) 오버사이즈 행렬표(raster) → 이미지(opt-in, 6번과 완전히 독립적인 탐지
            #    경로 — 벡터 격자가 아니라 삽입 이미지 px밀도/점유율 기반). 표 전체가
            #    이미 이미지이므로 표 전체 영역(bbox)을 그대로 크롭한다(서브박스 축소 X).
            if om_enabled:
                try:
                    om_boxes = detect_oversized_matrix_tables(
                        page, exclude_xrefs=om_watermark_xrefs
                    )
                except Exception as e:  # noqa: BLE001 — 검출 실패가 파이프라인 중단 X
                    _log.warning("p%02d 오버사이즈 행렬표 검출 실패(무시): %s", page_no, e)
                    om_boxes = []
                if om_boxes:
                    t_anchors = table_caption_anchors(page)
                    ok = 0
                    dedup_against = page_fig_bboxes + page_table_bboxes
                    for cl in om_boxes:
                        # 이미 figure/사선표로 채택된 영역과 크게 겹치면 중복이므로 skip.
                        if any(_iou(cl, fb) > 0.5 for fb in dedup_against):
                            continue
                        ok += 1
                        figure_id = f"{stem}__p{page_no:02d}_omtbl{ok}"
                        rel_image_path = f"figures/{figure_id}.png"
                        # 표 전체가 이미지이므로 검출 bbox(=이미지 bbox) 에 여백만 추가.
                        padded = _pad(cl, DIAG_PAD_PT, pw, ph)
                        bbox_r = [round(v, 1) for v in padded]
                        png_path = figures_dir / f"{figure_id}.png"
                        _crop_and_save(page, bbox_r, png_path, dpi)
                        page_table_bboxes.append(bbox_r)
                        cap = _nearest_anchor_text(bbox_r, t_anchors) or ""
                        _log.info(
                            "p%02d 오버사이즈 행렬표 → 이미지: %s bbox=%s",
                            page_no, figure_id, bbox_r,
                        )
                        # EXTRACT_DESCRIBE_COMPLEX_TABLES(전역 describe 무관) 또는 전역
                        # EXTRACT_FIGURE_DESCRIBE 중 하나라도 켜지면 설명 생성.
                        desc = _maybe_describe_figure(
                            png_path, caption=cap, item_type="complex_table"
                        )
                        items.append({
                            "figure_id": figure_id,
                            "page": page_no,
                            "image_path": rel_image_path,
                            "caption": cap,
                            "figure_no": "",
                            "type": "complex_table",
                            "kind": "oversized_matrix",
                            "bbox": bbox_r,
                            "source": "raster_density",
                            "snap_iou": None,
                            "description": desc,
                        })
    finally:
        doc.close()

    # 5b) 2-pass 한국어 번역(2026-07-07, opt-in FMDW_DESCRIBE_LANG=ko): mlx 백엔드로 영어
    #     describe 된 항목들을, MLX 를 언로드해 GPU 를 비운 뒤 gemma 로 일괄 번역한다(MLX 30B +
    #     번역 31B 동시 GPU 로드 시 Metal OOM 실측 → 시간 분리로 회피). 사이드카 기록 전에
    #     수행해 사이드카·반환값·본문 주입 모두 한국어로 반영한다. 번역 실패분은 영어 원문
    #     유지(비차단). FMDW_DESCRIBE_LANG 기본값 "en"이면 이 블록 자체를 건너뛰어 영어
    #     describe 결과가 그대로 최종 산출물이 된다.
    if _describe_backend() == "mlx" and _describe_lang() == "ko":
        _to_ko = [it for it in items if str(it.get("description", "")).strip()]
        if _to_ko:
            _unload_mlx_describe()
            for _it in _to_ko:
                _ko = _translate_ko_via_ollama(_it["description"])
                # 번역 성공(정상 한국어)일 때만 교체. 실패/붕괴('의의의…')면 영어 원문
                # 유지(정보 손실 0) — fig2 류 로컬 모델 한계 흡수.
                if _ko and not _is_degenerate_text(_ko):
                    _it["description"] = _ko

    # 5c) GPU 시간 분리(2026-07-09 Metal OOM fix): 이 파일의 describe 배치가 끝나면
    #     MLX(≈17GB)를 '무조건' 해제한다. 기존에는 ko 번역 경로(5b)에서만 언로드해서
    #     FMDW_DESCRIBE_LANG=en(기본)일 때 MLX 가 상주한 채 다음 파일 body 단계의
    #     ollama(glm/qwen) 재로드와 충돌 → Metal OOM. _unload_mlx_describe 는 멱등
    #     (미로드 시 no-op)이라 describe 0건/OFF 여도 안전. FMDW_GPU_SERIALIZE=0 이면
    #     기존 동작(ko 경로에서만 언로드) 그대로.
    if _describe_backend() == "mlx" and _gpu_serialize_enabled():
        if _MLX_DESCRIBE_CACHE is not None:
            print("    [GPU-SERIALIZE] describe 배치 종료 — MLX 언로드(다음 파일 "
                  "ollama 재로드 대비)", flush=True)
        _unload_mlx_describe()

    # 6) 사이드카 기록(반환 리스트와 동일 내용). 0건이어도 빈 리스트로 생성.
    out_dir.mkdir(parents=True, exist_ok=True)
    sidecar = out_dir / f"{stem}_figures.json"
    sidecar.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return items


def _nearest_anchor_text(
    bbox: list[float], anchors: list[tuple[str, list[float]]]
) -> Optional[str]:
    """스냅 bbox 와 가장 잘 맞는 캡션 앵커 텍스트 반환(겹침/근접 우선)."""
    if not anchors:
        return None
    bx0, by0, bx1, by1 = bbox
    best_text: Optional[str] = None
    best_score = float("inf")
    for text, abox in anchors:
        ax0, ay0, ax1, ay1 = abox
        # 앵커 중심이 bbox x범위에 들고 bbox 하단 근처(캡션은 보통 figure 아래)면 가점.
        acx = (ax0 + ax1) / 2.0
        in_x = bx0 - 20 <= acx <= bx1 + 20
        dy = min(abs(ay0 - by1), abs(ay0 - by0))
        score = dy if in_x else dy + 1000.0
        if score < best_score:
            best_score = score
            best_text = text
    return best_text
