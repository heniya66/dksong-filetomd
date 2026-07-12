import math
import os
import re
import sys
import time
import subprocess
import shutil
from pathlib import Path

# 공통 추출 모듈(fmdw.ollama_extractor) import 경로 보장 — 워크스페이스 루트 추가.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmdw import ollama_extractor as ox  # noqa: E402
from fmdw import config as _cfg  # noqa: E402  # config SSoT 로더
from fmdw import vision_qa as vqa  # noqa: E402
from fmdw import vision_qa_ensemble as vqa_ensemble  # noqa: E402
from fmdw import net_crosscheck as netcheck  # noqa: E402
from fmdw import page_tier as ptier  # noqa: E402

# 앙상블 vision QA — verifier(Claude vision)를 N회 독립 실행 후 designator별 항목
# 다수결로 통합한다. 단일 pass의 확률적 환각 교정 한계(C71 값환각 2/4, C81/C82 중복
# 0/4)를 보완한다. 기본 0/미설정 = 단일 pass 기존 동작(no-op). VISION_QA_ENSEMBLE>=2
# 일 때만 vqa.review 대신 vqa_ensemble.review_ensemble(n=...) 사용.
try:
    VISION_QA_ENSEMBLE = int(os.getenv("VISION_QA_ENSEMBLE", "0"))
except ValueError:
    VISION_QA_ENSEMBLE = 0

# net_tracer 교차검증 — vision QA(확률적) 산출을 벡터 결정적 넷리스트로 보강한다.
# 기본 0/미설정 = 비활성(기존 동작 무변경). =1 이면 vision QA(단일/앙상블) 산출
# Markdown 에 `lib.net_crosscheck.crosscheck(md, pdf, page)` 를 적용한다.
#   - vision QA(VISION_QA) 가 꺼져 있으면 netcheck 도 무의미 → vision QA 활성 시에만 동작.
#   - net_tracer 무력(래스터)/실패 시 안전 degrade(MD 그대로, 플래그만, 자동삭제 없음).
#   - 출력 형식 계약(`### Figure N`, `PIN -> NET`, GFM, 청크 결합)은 보존된다.
try:
    VISION_QA_NETCHECK = int(os.getenv("VISION_QA_NETCHECK", "0"))
except ValueError:
    VISION_QA_NETCHECK = 0

# ── 페이지별 자동 티어링(VISION_QA_AUTO) ─────────────────────────────────────
# 기본 0/미설정 = 기존 수동 동작(VISION_QA_ENSEMBLE/VISION_QA_NETCHECK) 완전 무변경.
# =1 이면 1차 추출 후 **페이지별** lib.page_tier.classify_page 로 dense/light/text 를
# 판별하여 처리 강도를 자동 선택한다(조합 분류 + 3티어 자동):
#   dense : vision_qa_ensemble.review_ensemble(n=3, DPI 300 override) + net_crosscheck
#   light : vision_qa.review(단일) + net_crosscheck
#   text  : vision QA skip(1차 MD 그대로)
# AUTO 모드에선 분류/QA/netcheck 를 페이지 단위로 적용해야 하므로 1차 추출도 페이지
# 단위로 운영한다(효과적 CHUNK_SIZE=1). vision_qa.review/ensemble/crosscheck 가 이미
# 페이지 인자를 받으므로 페이지 루프가 자연스럽다. AUTO=1 이면 수동 ENSEMBLE/NETCHECK
# 플래그보다 자동 티어가 우선한다.
try:
    VISION_QA_AUTO = int(os.getenv("VISION_QA_AUTO", "0"))
except ValueError:
    VISION_QA_AUTO = 0

# dry-run: vision QA 실행 없이 페이지별 tier 분류 결과만 출력(비용 0 분류 점검).
# AUTO=1 이고 DRYRUN=1 이면 1차 추출만 하고 tier 표를 찍은 뒤 1차 MD 를 그대로 저장.
try:
    VISION_QA_AUTO_DRYRUN = int(os.getenv("VISION_QA_AUTO_DRYRUN", "0"))
except ValueError:
    VISION_QA_AUTO_DRYRUN = 0

# 비용 가드: 앙상블(dense) 적용 페이지 상한. 초과하면 나머지 dense 페이지는 light 로
# 강등하고 경고 로그를 남긴다(누적 카운트). 회로도 PDF 의 앙상블 폭주를 막는다.
try:
    VISION_QA_MAX_ENSEMBLE_PAGES = int(os.getenv("VISION_QA_MAX_ENSEMBLE_PAGES", "10"))
except ValueError:
    VISION_QA_MAX_ENSEMBLE_PAGES = 10

# dense 페이지 앙상블 렌더 DPI override(고밀도 회로도 판독력 향상). env 로 조정.
try:
    VISION_QA_AUTO_DENSE_DPI = int(os.getenv("VISION_QA_AUTO_DENSE_DPI", "300"))
except ValueError:
    VISION_QA_AUTO_DENSE_DPI = 300

# dense 앙상블 run 수(기본 3).
try:
    VISION_QA_AUTO_ENSEMBLE_N = int(os.getenv("VISION_QA_AUTO_ENSEMBLE_N", "3"))
except ValueError:
    VISION_QA_AUTO_ENSEMBLE_N = 3

# ── M-6: API 호출 간 레이트리밋 대기 ─────────────────────────────────────────
# 기존엔 QA 페이지/청크마다 **무조건** time.sleep(10) 했다(40p ≈ 6.5분 순수 대기,
# 429 가 없어도 항상 대기). 이를 다음으로 개선한다(출력 MD 불변 — 타이밍만 변경):
#   - VISION_QA_RATE_DELAY(기본 0.0): 호출 사이 기본 대기(초). 0 이면 적응형만.
#       기존 10초 동작이 필요하면 env VISION_QA_RATE_DELAY=10 로 복구 가능.
#   - 적응형(429): provider 가 429 신호(레이트리밋)를 줄 때만 추가 백오프 대기.
#       ox 내부 재시도가 429 를 흡수하므로 여기서는 "마지막 호출 결과가 truncated/
#       degrade 였는지"가 아니라 호출 간 최소 간격만 적용한다. 토큰버킷 형태로
#       마지막 호출 시각을 추적해 필요한 만큼만 잔여 대기한다.
#   - **마지막 호출 뒤 sleep 생략**: 다음 호출이 없으면 대기 의미가 없다.
try:
    VISION_QA_RATE_DELAY = float(os.getenv("VISION_QA_RATE_DELAY", "0.0"))
except ValueError:
    VISION_QA_RATE_DELAY = 0.0

# ── M-8: 청크 실패 시 페이지/소청크 단위 재추출 폴백 ──────────────────────────
# 기존엔 멀티페이지 청크가 한 페이지 때문에 실패해도 **청크 전체**(최대 CHUNK_SIZE
# 페이지)를 통째로 MISSING 처리했다(페이지별 resume 없음). 이를 개선한다:
#   - 청크 실패 시 그 범위를 **페이지 단위**로 재추출 시도(소청크 폴백).
#   - 각 페이지는 최대 EXTRACT_PAGE_RETRIES 회 재시도(ox 내부 429 재시도와 별개의
#     상위 재시도 — 전체 페이지 실패에 대한 제한 재시도).
#   - 성공 페이지는 살리고, 끝까지 실패한 페이지만 인라인 MISSING 마커로 표기.
#   - VISION_QA_AUTO=0(청크 경로)에서만 동작. AUTO 경로는 이미 페이지 단위라 무관.
#   - **출력 동작 보존**: 폴백이 모든 페이지를 성공 복구하면 결과는 정상 청크와 동일
#     (Figure 전역 리넘버·결합 구분자 보존). 일부만 실패하면 H-5 와 동일하게
#     해당 페이지에 MISSING 마커가 남아 .partial.md 로 저장된다.
try:
    EXTRACT_PAGE_RETRIES = int(os.getenv("EXTRACT_PAGE_RETRIES", "1"))
except ValueError:
    EXTRACT_PAGE_RETRIES = 1


# ── 본문 하이브리드 전사(FMDW_BODY_HYBRID, opt-in, 기본 OFF) ─────────────────
# 실측(2026-07-04, LN08LPU Design Manual testpages): glm-ocr(FMDW_DOMAIN_MODEL_
# DATASHEET=glm-ocr 라우팅)은 8~9초/페이지로 빠르고 본문/표 정확도가 높지만,
# 표지·법적고지(Important Notice)·목차 머리말 같은 "안 중요해 보이는" 앞부분
# 페이지를 조용히 스킵한다(빈 출력, 에러 없음). qwen3-vl:32b 는 지시를 그대로
# 따라 전부 전사하지만 조밀한 페이지에서 10~26분/페이지로 느려 문서 전체 적용은
# 비현실적이다. 반면 glm-ocr 이 잘 처리하는 조밀한 표 페이지는 계속 빠르게 두고,
# glm 이 실제로 스킵한 소수의 페이지만 qwen 으로 재전사(repair)하면 속도와 완전성을
# 모두 얻는다. 기본 0/미설정 = 기존 단일 모델 경로 완전 보존(회귀 0, extract_chunk
# 가 ox.extract_pdf_pages 를 model 인자 없이 호출 — 기존 도메인 라우팅 그대로).
try:
    FMDW_BODY_HYBRID = int(os.getenv("FMDW_BODY_HYBRID", "0"))
except ValueError:
    FMDW_BODY_HYBRID = 0

#: 하이브리드 1차(fast) 모델 — 표/본문 전사 담당(빠름, 표지류 스킵 위험).
FMDW_BODY_PRIMARY_MODEL = os.getenv("FMDW_BODY_PRIMARY_MODEL", "").strip() or "glm-ocr"

#: 하이브리드 폴백(repair) 모델 — 1차가 스킵한 소수 페이지만 재전사.
#: FIX D-R1(2026-07-09 Advisor QA): 구 기본 "qwen3-vl:32b" 는 삭제(404), 1차 교체 후보
#: qwen2.5vl:32b 는 CLIP blob 손상으로 HTTP 500(실측 1.7s 즉사, 비전·텍스트 모두 불능).
#: 이 호스트에서 실측 동작 확인된 glm-ocr(3.6s, 일관 출력)로 교체 — best-of 로직이
#: 폴백 결과가 더 길 때만 채택하므로 동일 계열 재시도여도 회귀 없음(순수 이득/무해).
#: env FMDW_BODY_FALLBACK_MODEL 로 override 가능(계약 유지).
FMDW_BODY_FALLBACK_MODEL = (
    os.getenv("FMDW_BODY_FALLBACK_MODEL", "").strip() or "glm-ocr"
)

# 커버리지 판정 분모의 최소 selectable 텍스트 길이(글자수). 이 미만이면 페이지가
# 원래 이미지/도면 위주(진짜 텍스트가 거의 없는 페이지)라 glm 출력이 짧아도 "스킵"
# 이 아니라 정상이므로 폴백을 트리거하지 않는다(오탐 방지 가드).
try:
    FMDW_HYBRID_MIN_TEXT = int(os.getenv("FMDW_HYBRID_MIN_TEXT", "120"))
except ValueError:
    FMDW_HYBRID_MIN_TEXT = 120

# glm 출력 길이가 (커버리지 최소비율 × pdf_text_len) 미만이면 "스킵됨"으로 간주해
# qwen 폴백을 트리거한다. 0.30 = glm 출력이 원문 selectable 텍스트의 30% 미만.
try:
    FMDW_HYBRID_COVERAGE_MIN = float(os.getenv("FMDW_HYBRID_COVERAGE_MIN", "0.30"))
except ValueError:
    FMDW_HYBRID_COVERAGE_MIN = 0.30


def _pdf_page_text_len(pdf_path, page: int) -> int:
    """fitz selectable 텍스트 길이(strip 후) — 하이브리드 커버리지 체크의 분모.

    실패 시 0 반환(안전 degrade). 0 이면 FMDW_HYBRID_MIN_TEXT 가드에 걸려 폴백이
    트리거되지 않으므로, 스캔 실패가 불필요한 qwen 폴백 폭주로 이어지지 않는다.
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        try:
            return len(doc[page - 1].get_text().strip())
        finally:
            doc.close()
    except Exception as e:  # noqa: BLE001 — 스캔 실패가 본문 OCR 을 막지 않게 안전 degrade
        print(f"    [~] hybrid p{page}: pdf_text_len 계산 실패(무시): {e}", flush=True)
        return 0


# ── 2열 정의 레이아웃(Syntax/Description) → GFM 표 (FMDW_TWOCOL_TABLES, 기본 ON) ──
# 사용자 확정(2026-07-09): S6 프롬프트 규칙만으로는 glm-ocr 이 좌(용어)/우(설명) 정의
# 레이아웃을 표로 내지 못한다(레이아웃 지시 무시 — 기지 특성). 따라서 PDF 벡터 텍스트
# 좌표(PyMuPDF)로 2열 정의 영역을 '결정론적으로' 검출해 `| Syntax | Description |`
# GFM 표를 생성한다. 100% 로컬·LLM 무관.
#
# 통합 방식(코드 현실 기반 선택): glm OCR 문자열에는 좌표가 없고 벡터 텍스트와
# 문자단위로 일치하지도 않아, glm 출력 내 해당 영역만 정밀 치환(splicing)하는 것은
# 불가능하다. 대신 '영역이 검출된 페이지'는 페이지 전체를 PDF 벡터 텍스트에서 직접
# 렌더한다(전폭 라인=읽기순 문단, 정의 영역=표). 벡터 텍스트가 ground truth 이므로
# OCR 보다 정확하며 glm 호출도 절약된다.
# 보수 가드(오탐 = 미탐보다 나쁨):
#   - 2열 '연속 산문'(Important Notice 류) → 좌열 채움비(fill ratio) 가드로 스킵.
#   - 격자 표/다중 열 페이지 → 중간 x 라인(OTHER) 비율 가드로 페이지 자체 스킵.
#   - 러닝 헤더/푸터(상·하 5%) 제외. 유효 영역이 하나도 없으면 기존 glm 경로 그대로.
#   - L/R 형태이지만 정의 레이아웃으로 안전하게 해석되지 않는 런이 '하나라도' 있으면
#     페이지 전체를 포기하고 glm 경로로 폴백(부분 렌더로 인한 내용 유실 0 보장).

def _twocol_enabled() -> bool:
    return os.getenv("FMDW_TWOCOL_TABLES", "1").strip().lower() not in ("0", "false", "no")


# F1 primary(2026-07-09, coordinator upgrade): 워터마크를 '내용'이 아니라 '회전각'으로
# 판별해 PDF 텍스트 레이어에서 드롭한다. 진단(LN08LPU p106/p111 rawdict 'dir' 실측):
# 실제 본문/표는 전부 수평(0°), 유일한 텍스트-레이어 워터마크는 45° 대각 추적 스탬프
# ('ml.ko at ...', dir≈(0.7071,-0.7071)). 큰 'Samsung Confidential' 대각선은 래스터
# 이미지라 텍스트 레이어에 없음. 축정렬(0/90/180/270°, ±tol)만 남기고 대각선은 드롭 →
# 계정/날짜 무관하게 모든 뷰어 스탬프 제거(내용 독립적). 세로 라벨(90/270°)은 보존.
def _rotated_watermark_drop_enabled() -> bool:
    return os.getenv("FMDW_DROP_ROTATED_WATERMARK", "1").strip().lower() not in (
        "0", "false", "no")


def _is_diagonal_dir(dir_xy, tol_deg: float = 10.0) -> bool:
    """line 'dir'(dx,dy) 이 축정렬(0/90/180/270°)에서 tol 이상 벗어난 대각선이면 True(=드롭).

    angle = degrees(atan2(-dy, dx)) (PyMuPDF y-하향 좌표 보정). 축까지 최소 원거리 > tol.
    dir 없음/영벡터/비수치는 보수적으로 False(=유지 — 회전정보 없으면 드롭하지 않음).
    """
    if not dir_xy:
        return False
    try:
        dx, dy = float(dir_xy[0]), float(dir_xy[1])
    except (TypeError, ValueError, IndexError):
        return False
    if dx == 0.0 and dy == 0.0:
        return False
    a = math.degrees(math.atan2(-dy, dx)) % 360.0
    d = min(abs(a - k) for k in (0.0, 90.0, 180.0, 270.0, 360.0))
    return d > tol_deg


# ── 불릿 글리프 병합(F9 보조, 2026-07-09) ─────────────────────────────────────────
#   PDF 에서 불릿 기호가 텍스트와 '별도 스팬/라인'으로 떨어져 있을 때(예 `•` x=42.5,
#   본문 x=56.7, 동일 y) PyMuPDF 는 단독 `•` 라인을 만든다. 완전성 가드는 이 단독 1자
#   라인을 '유의미토큰<2 → 스킵'해 버려 복구 불릿이 마커를 잃고, F9(•→-)도 `•` 를 못 본다.
#   → 라인 추출 단계에서 단독 불릿 글리프를 같은 y-밴드 우측 텍스트와 결합해 `• <text>`/
#   `– <text>` 로 만든다(완전성 가드·2col 공용). 이어 F9 가 `- `/`  - ` 로 정규화.
# 명확한 원형 불릿(항상 병합 — 단어 1개짜리 항목 `• Inductors` 도 병합).
_BULLET_GLYPHS_CLEAR = frozenset("•●▪‣◦∙◾◼")
# 모호 마커(하이픈/엔대시/미들닷) — 표의 N/A·범위 셀(`-`,`–`)과 혼동. 우측이 '명백한 산문'일
# 때만 병합(표 셀 오병합 방지, Advisor 하드닝 2026-07-10).
_BULLET_GLYPHS_AMBIG = frozenset("-–·")


def _lone_bullet_marker(text: str):
    """text 가 '순수 불릿 글리프 1자'면 정규 마커('•'=top / '–'=sub) 반환, 아니면 None.

    보수적: 정확히 1자여야 병합 대상(다른 텍스트가 붙은 스팬은 절대 병합 안 함).
    미들닷 `·`=top('•'), 하이픈/엔대시 `-`/`–`=sub('–'). 모호 여부는 _is_ambiguous_bullet_glyph 로 별도 판정."""
    s = text.strip()
    if len(s) != 1:
        return None
    if s in _BULLET_GLYPHS_CLEAR:
        return "•"
    if s in _BULLET_GLYPHS_AMBIG:
        return "•" if s == "·" else "–"
    return None


def _is_ambiguous_bullet_glyph(text: str) -> bool:
    """마커가 모호(하이픈/엔대시/미들닷)해 표 셀 값과 혼동될 수 있는 글리프인지."""
    s = text.strip()
    return len(s) == 1 and s in _BULLET_GLYPHS_AMBIG


def _is_prose_right_text(text: str) -> bool:
    """모호 마커 병합 허용 조건: 우측 텍스트가 '명백한 산문'인가.

    산문 = 유의미토큰 ≥2개  OR  (표 셀 같은 짧은 코드가 아님).
    표 셀 같은 코드 = 공백 없는 ≤3자 토큰(예 '2','1','IB','GI','N/A') → 병합 금지.
    → `– Continued from previous section`(산문) 병합 O, `– IB`/`- N/A`(셀) 병합 X."""
    st = (text or "").strip()
    if len(_sig_tokens(st)) >= 2:
        return True
    table_cell_like = (" " not in st) and len(st) <= 3
    return not table_cell_like


def _merge_bullet_glyph_lines(lines):
    """정렬된 라인 목록에서 '단독 불릿 글리프' 라인을 같은 y-밴드 우측 텍스트 라인과 결합.

    조건(전부 충족 시에만, 보수적):
      - 좌 라인이 순수 불릿 글리프 1자(_lone_bullet_marker)
      - 우 라인(바로 다음, 정렬상 동일 y → 인접)이 같은 y-밴드: |Δy0| ≤ 0.6·행높이
      - 우 라인 x0 > 불릿 x0 (오른쪽) 且 가로 간격(우.x0 − 불릿.x1) ≤ 40pt (교차 열 방지)
      - 우 라인이 비어있지 않은 '비-불릿' 텍스트
    결합 결과: `<marker> <text>` (marker '•'=top / '–'=sub). 이후 F9 가 `- `/`  - ` 변환.
    비대상(일반 텍스트·표 셀)은 무변경 → p1-4 저작권 블록 등은 그대로 평문 유지.
    """
    if not lines:
        return lines
    out = []
    k = 0
    n = len(lines)
    while k < n:
        cur = lines[k]
        marker = _lone_bullet_marker(cur[4])
        if marker is not None and k + 1 < n:
            nxt = lines[k + 1]
            lh = max(cur[3] - cur[1], 1.0)
            nxt_txt = nxt[4].strip()
            if (abs(cur[1] - nxt[1]) <= 0.6 * lh          # 같은 y-밴드
                    and nxt[0] > cur[0]                    # 우측
                    and (nxt[0] - cur[2]) <= 40.0          # 인접(교차 열 아님)
                    and nxt_txt                            # 텍스트 존재
                    and _lone_bullet_marker(nxt_txt) is None   # 우 라인은 불릿 아님
                    # 모호 마커(-,–,·)는 우측이 '명백한 산문'일 때만 병합 → 표 N/A·코드 셀
                    #   (`– 2`,`– IB`,`- N/A`) 오병합 방지. 명확한 원형 불릿(•●…)은 무조건 병합.
                    and not (_is_ambiguous_bullet_glyph(cur[4])
                             and not _is_prose_right_text(nxt_txt))):
                out.append([cur[0], min(cur[1], nxt[1]), nxt[2],
                            max(cur[3], nxt[3]), marker + " " + nxt_txt])
                k += 2
                continue
        out.append(cur)
        k += 1
    return out


def _twocol_page_lines(page):
    """페이지 텍스트 라인 [x0,y0,x1,y1,text] 목록(읽기순) — 노이즈 제거 포함(dict 위임)."""
    return _twocol_lines_from_dict(
        page.get_text("dict"), page.rect.height or 1.0, page.rect.width or 1.0)


def _twocol_lines_from_dict(d, ph: float, pw: float):
    """get_text('dict') 결과에서 본문 라인 추출 + 노이즈 제거(테스트 가능하도록 분리).

    제거 대상(보수적):
      - F1 primary: 대각(≈45°) 회전 라인(뷰어 추적 워터마크 스탬프) — dir 기반, 내용 무관.
      - 러닝 헤더/워터마크 패턴(_running_header_patterns, case-insensitive).
      - F1 backstop: 텍스트가 오버레이 스탬프 형태(`<id> at <date> <time> <TZ>`)면 드롭.
      - 상단 11% 밴드의 '우측 절반 시작' 라인(우상단 코너 러닝 헤더).
      - 하단 10% 밴드의 순수 페이지 번호(숫자/로마자 ≤6자).
    """
    hpats = _running_header_patterns()
    drop_rot = _rotated_watermark_drop_enabled()
    out = []
    for blk in d.get("blocks", []):
        if blk.get("type", 0) != 0:
            continue
        for ln in blk.get("lines", []):
            if drop_rot and _is_diagonal_dir(ln.get("dir")):
                continue  # F1 primary: 대각 회전 워터마크 스탬프 — 회전각 기반 제거
            t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            if not t:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            if any(p.fullmatch(t) for p in hpats):
                continue  # 러닝 헤더/워터마크 — 위치 무관 제거(FIX B 와 동일 계약)
            if _OVERLAY_STAMP_RE.match(t):
                continue  # F1 backstop(텍스트): 스탬프 형태 — 회전정보 없을 때 대비
            if y1 <= 0.11 * ph and x0 > 0.5 * pw:
                continue  # 우상단 코너 러닝 헤더
            if y0 >= 0.90 * ph and re.fullmatch(r"[0-9ivxlcIVXLC]{1,6}", t):
                continue  # 하단 페이지 번호
            out.append([x0, y0, x1, y1, t])
    out.sort(key=lambda l: (round(l[1], 1), l[0]))
    if _bullet_list_enabled():
        out = _merge_bullet_glyph_lines(out)  # 단독 불릿 글리프+텍스트 결합(F9 보조)
    return out


def _twocol_cell(text: str) -> str:
    """GFM 셀 텍스트: 공백 압축 + 파이프 이스케이프(심볼 ≥ ≤ µ ° 등은 그대로 보존)."""
    return re.sub(r"\s+", " ", text.strip()).replace("|", "\\|")


#: 페이지 폴백 신호(2열 연속 산문 등 — 부분 렌더 시 내용 순서 훼손 위험 → glm 경로로).
_TWOCOL_ABORT = object()


def _twocol_analyze(lines):
    """정의 레이아웃 분석 → 세그먼트 목록 [("para", text) | ("table", rows)] 또는 None.

    rows = [(syntax, description), ...]. 유효 정의 영역(행 ≥2)이 1개 이상일 때만
    세그먼트를 돌려준다. 2열 연속 산문 의심 런이 하나라도 있으면 None(페이지 전체
    glm 폴백 — 부분 렌더로 인한 내용 유실/순서 훼손 0 보장).
    """
    from collections import Counter

    if len(lines) < 6:
        return None
    left_m = min(l[0] for l in lines)
    right_e = max(l[2] for l in lines)
    W = right_e - left_m
    if W <= 50:
        return None
    hs = sorted(l[3] - l[1] for l in lines)
    h_med = hs[len(hs) // 2] or 10.0

    # 우측 열 시작 x(r_x): 본문 폭 25% 이후 시작 라인 x0 의 3pt 클러스터 중 빈도 ≥4 이고
    # 좌여백 대비 ≥30% 지점인 '가장 왼쪽' 클러스터(실측: 설명 열 300.6 + 불릿 들여쓰기
    # 314.8 이 공존 — 최빈이 아니라 최좌측이 열 시작).
    cl = Counter(round(x / 3.0) for x in (l[0] for l in lines if l[0] > left_m + 0.25 * W))
    valid_rx = sorted(k * 3.0 for k, n in cl.items()
                      if n >= 4 and (k * 3.0 - left_m) >= 0.30 * W)
    if not valid_rx:
        return None
    r_x = valid_rx[0]

    # 라인 분류: R(우측 열 시작 이후 — 들여쓴 불릿 연속 포함) / L(좌측 짧은 항목) /
    # F(전폭) / O(중간 x — 격자 표/다중 열 의심)
    cls = []
    for x0, y0, x1, y1, t in lines:
        if x0 >= r_x - 4.5:
            cls.append("R")
        elif x0 <= left_m + 0.10 * W:
            cls.append("L" if x1 <= r_x - 6.0 else "F")
        else:
            cls.append("O")
    if sum(1 for c in cls if c == "O") > 0.15 * len(lines):
        return None  # 격자 표/다중 열 페이지 의심 — 보수적으로 전체 스킵

    segments = []
    para_buf = []
    prev_para_y1 = None

    def _flush_para():
        nonlocal para_buf, prev_para_y1
        if para_buf:
            segments.append(("para", re.sub(r"\s+", " ", " ".join(para_buf).strip())))
        para_buf = []
        prev_para_y1 = None

    i = 0
    nlines = len(lines)
    found_region = False
    while i < nlines:
        if cls[i] in ("F", "O"):
            x0, y0, x1, y1, t = lines[i]
            if para_buf and prev_para_y1 is not None and (y0 - prev_para_y1) > 1.5 * h_med:
                _flush_para()
            para_buf.append(t)
            prev_para_y1 = y1
            i += 1
            continue
        # 연속 L/R 런 수집 — 단, 런 라인과 수직으로 겹치는 O 라인(워터마크/스탬프
        # 오버레이, 예 'ml.ko at ...')은 런을 끊지 않고 통과(overlay)시켜 뒤에 문단으로
        # 보존한다(런 중단 시 표가 스탬프 위치마다 쪼개지는 실측 문제 방지).
        j = i
        run = []
        overlay = []
        while j < nlines:
            if cls[j] in ("L", "R"):
                run.append((lines[j], cls[j]))
                j += 1
                continue
            if cls[j] == "O" and run:
                y0o = lines[j][1]
                if abs(y0o - run[-1][0][1]) <= 1.2 * h_med or (
                        j + 1 < nlines and cls[j + 1] in ("L", "R")
                        and abs(lines[j + 1][1] - y0o) <= 1.2 * h_med):
                    overlay.append(lines[j][4])
                    j += 1
                    continue
            break
        parsed = _twocol_parse_run(run, h_med)
        if parsed is _TWOCOL_ABORT:
            return None  # 2열 연속 산문 의심 — 페이지 전체 glm 폴백
        _flush_para()
        for kind, payload in parsed:
            segments.append((kind, payload))
            if kind == "table":
                found_region = True
        # F1(2026-07-09): 오버레이로 통과된 O-라인 중 추적 워터마크 스탬프는 문단으로도
        # 남기지 않고 폐기(원래 '통과' 정책 → '폐기'로 변경). 스탬프가 아닌 정상 오버레이
        # 텍스트만 문단으로 보존한다.
        for ov in overlay:
            if _OVERLAY_STAMP_RE.match(ov.strip()):
                continue
            segments.append(("para", ov))
        i = j
    _flush_para()
    if not found_region:
        return None
    # 표 행 총합 ≥2 확인(안전 하한 — parse_run 이 이미 보장하지만 이중 방어).
    total = sum(len(p) for k, p in segments if k == "table")
    return segments if total >= 2 else None


def _twocol_parse_run(run, h_med):
    """연속 L/R 라인 런 해석 → 세그먼트 목록 [("para",text)|("table",rows)] 또는 _TWOCOL_ABORT.

    규칙(보수):
      - L 라인을 수직 근접(갭<0.6×행높이)으로 항목 그룹화. 항목이 4줄 이상/60자 초과면
        '용어'가 아니므로: R 라인과 섞여 있으면 2열 연속 산문 의심 → ABORT(페이지 폴백),
        R 이 없으면 그냥 좁은 문단 → 문단으로 렌더.
      - top y 가 R 라인과 ±0.8×행높이로 정렬되는 항목 = 표 행(paired). paired ≥2 일 때만
        표 생성. paired ≤1 이면 런 전체를 읽기순 문단으로 렌더(표 오탐 방지).
      - 미페어 항목(섹션 소제목 등)은 행 경계로 쓰되 자체는 문단으로 보존.
      - 좌열 채움비 >0.55 → ABORT(2열 산문). 행 설명이 비면 ABORT(확신 불가).
    """
    if not run:
        return []
    L = [l for l, c in run if c == "L"]
    R = [l for l, c in run if c == "R"]

    def _as_paras():
        out = []
        buf = []
        prev_y1 = None
        for l, _c in run:
            if buf and prev_y1 is not None and (l[1] - prev_y1) > 1.5 * h_med:
                out.append(("para", re.sub(r"\s+", " ", " ".join(buf).strip())))
                buf = []
            buf.append(l[4])
            prev_y1 = l[3]
        if buf:
            out.append(("para", re.sub(r"\s+", " ", " ".join(buf).strip())))
        return out

    if not L or not R:
        return _as_paras()

    # 항목 그룹화 — 실측(LN08LPU p106-112 p3): 인접한 '별개 행'의 용어 간 갭 ≈5.6pt,
    # 진짜 줄바꿈(wrap) 갭 ≈0pt. 0.6×행높이(≈7.4)로 병합하면 별개 행 용어들이 하나로
    # 합쳐지므로, 병합 임계는 0.25×행높이(진짜 wrap 만 병합)로 둔다. 2열 연속 산문은
    # 줄 피치가 wrap 수준이라 전부 한 항목으로 병합 → 아래 4줄 가드로 ABORT(안전 유지).
    entries = []
    cur = [L[0]]
    for l in L[1:]:
        if (l[1] - cur[-1][3]) < 0.25 * h_med:
            cur.append(l)
        else:
            entries.append(cur)
            cur = [l]
    entries.append(cur)

    for e in entries:
        # 용어 상한: ≤3줄 + ≤200자. 줄수(≤3)가 1차 산문 신호(진짜 산문 문단은 wrap-연속
        # 이라 한 entry 로 뭉쳐 >3줄 → ABORT). 좌열 폭상 ≤3줄 term 최대 ~165자라 200 은
        # 사실상 '줄수+아래 fill>0.55 게이트 신뢰'. 120 은 실측 122자 정의 term(p118
        # 'A minimum overlap past B ...(rectangular enclosure).')을 오abort 시켜 완화.
        if len(e) > 3 or len(" ".join(x[4] for x in e)) > 200:
            return _TWOCOL_ABORT  # 좌열이 '용어'가 아님 + R 존재 = 2열 산문 의심

    tol = 0.8 * h_med
    paired = [e for e in entries if any(abs(r[1] - e[0][1]) <= tol for r in R)]
    if len(paired) < 2:
        return _as_paras()

    # 좌열 채움비 가드(2열 연속 산문 배제) — paired 스팬 '내부'만 계산.
    # (2026-07-09 fix) span_bot 은 paired 항목 + 그 설명(R) 범위로 한정하고, 채움비
    # 분자 L 도 [span_top, span_bot] 범위로 상·하한 제한한다. 이전엔 상한이 없어, 표
    # 영역 '아래'의 무관한 L(캡션 'Figure 13'·다음 소절 표제 등)이 분자를 부풀려 정상
    # 정의 표를 오abort 했다(실측: 워터마크 스탬프가 우연히 run 구분자 역할을 하다가
    # F1 로 스탬프가 제거되자 run 이 병합되며 재현). 상한 도입으로 스탬프 유무와 무관하게
    # 동일 판정.
    span_top = min(e[0][1] for e in paired)
    span_bot = max(max(x[3] for x in e) for e in paired)
    r_in = [r[3] for r in R if span_top - tol <= r[1] <= span_bot + 4 * tol]
    if r_in:
        span_bot = max(span_bot, max(r_in))
    span = max(span_bot - span_top, 1.0)
    fill = sum(l[3] - l[1] for l in L
               if span_top - tol <= l[1] <= span_bot + tol) / span
    if fill > 0.55:
        return _TWOCOL_ABORT

    # 세그먼트 조립: 선행 R 문단 → (문단|표행) 인터리브.
    segs = []
    first_top = entries[0][0][1]
    lead = [r for r in R if r[1] < first_top - tol]
    if lead:
        segs.append(("para", re.sub(r"\s+", " ", " ".join(r[4] for r in lead).strip())))
    body_R = [r for r in R if r[1] >= first_top - tol]

    bounds = [e[0][1] for e in entries] + [float("inf")]
    rows = []

    def _flush_rows():
        nonlocal rows
        if rows:
            segs.append(("table", rows))
            rows = []

    for idx, e in enumerate(entries):
        top = bounds[idx] - tol
        nxt = bounds[idx + 1] - tol
        desc = [r for r in body_R if top <= r[1] < nxt]
        if e in paired:
            if not desc:
                return _TWOCOL_ABORT  # 설명 없는 행 — 확신 불가
            rows.append((
                _twocol_cell(" ".join(x[4] for x in e)),
                _twocol_cell(" ".join(x[4] for x in desc)),
            ))
        else:
            # 미페어 항목(소제목 류): 행 경계 유지, 자체+뒤따르는 R 은 문단 보존.
            _flush_rows()
            segs.append(("para", re.sub(r"\s+", " ", " ".join(x[4] for x in e).strip())))
            if desc:
                segs.append(("para", re.sub(
                    r"\s+", " ", " ".join(r[4] for r in desc).strip())))
    _flush_rows()
    # 표가 하나도 안 만들어졌으면(전부 미페어) 문단 런과 동일 취급.
    if not any(k == "table" for k, _ in segs):
        return _as_paras()
    return segs


def _twocol_try_render_page(pdf_path, page_no: int):
    """정의 영역 검출 시 페이지 전체 MD(마커 포함) 반환, 아니면 None(기존 경로 유지).

    ★ F11 그리드 우선(2026-07-09): find_tables 그리드 표가 있는 페이지는 2COL 미적용
    (return None) — 12-col 연속 표를 2COL 이 2-col 로 오검출·선점하던 사고 방지.
    진짜 borderless 정의 페이지(p98-102/p106-112 등)는 그리드 0건이라 영향 없음.
    """
    if _grid_tables_enabled() and _page_has_grid(pdf_path, page_no):
        return None
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        try:
            lines = _twocol_page_lines(doc[page_no - 1])
        finally:
            doc.close()
        segments = _twocol_analyze(lines)
    except Exception as e:  # noqa: BLE001 — 분석 실패는 기존 OCR 경로로 안전 폴백
        print(f"    [~] 2col p{page_no}: 분석 실패(무시, OCR 경로): {e}", flush=True)
        return None
    if not segments:
        return None
    parts = [f"<!-- page {page_no} -->"]
    nrows = 0
    nregions = 0
    for kind, payload in segments:
        if kind == "para":
            parts.append(payload)
        else:
            nregions += 1
            nrows += len(payload)
            tbl = ["| Syntax | Description |", "| :--- | :--- |"]
            tbl += [f"| {s} | {d} |" for s, d in payload]
            parts.append("\n".join(tbl))
    print(f"    [2COL] p{page_no}: {nregions} 영역/{nrows} 행 → Syntax/Description 표",
          flush=True)
    return "\n\n".join(parts)


def _twocol_detect_rx(lines):
    """설명(우측) 열 시작 x(r_x) 검출 — `_twocol_analyze` 와 동일 로직(페이지 걸침
    판정용 재사용). (left_m, W, r_x) 또는 None."""
    from collections import Counter
    if len(lines) < 6:
        return None
    left_m = min(l[0] for l in lines)
    right_e = max(l[2] for l in lines)
    W = right_e - left_m
    if W <= 50:
        return None
    cl = Counter(round(x / 3.0) for x in (l[0] for l in lines if l[0] > left_m + 0.25 * W))
    valid_rx = sorted(k * 3.0 for k, n in cl.items()
                      if n >= 4 and (k * 3.0 - left_m) >= 0.30 * W)
    if not valid_rx:
        return None
    return (left_m, W, valid_rx[0])


def _twocol_straddle_tail(pdf_path, page_no: int):
    """페이지 걸침 정의 표 검출: 현재 페이지(page_no) '맨 끝'에 단일 정의 행
    (짧은 좌측 구문 + 우측 설명)이 있고, 그 표가 다음 페이지(page_no+1)의 선두
    2COL Syntax/Description 표로 이어지는가를 PDF 벡터 기하로 판정한다.

    조건(보수):
      - page_no 가 마지막 페이지가 아님.
      - 현재 페이지 자체는 2COL 페이지가 아님(_twocol_analyze(cur) is None) — 이미 표로
        렌더되는 페이지(전체 2열 표)는 대상 아님(오검출 방지).
      - 다음 페이지 _twocol_analyze → 첫 세그먼트가 table(표로 시작)이고 r_x 검출됨.
      - 다음 페이지 r_x 기준, 현재 페이지 '맨 끝'에 우측(R) 설명 라인(≤6줄)과 같은 y 로
        정렬된 '단 하나의' 짧은 좌측(L) 구문 라인 존재(그 아래 다른 내용 없음).
      - 구문 라인 위는 'N.N[.N] 제목' 이거나 충분한 세로 갭(영역 시작).
    충족 시 (syntax_text, description_text)[PDF 벡터] 반환, 아니면 None.
    """
    if not _twocol_enabled():
        return None
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        try:
            npg = doc.page_count
            if page_no < 1 or page_no >= npg:
                return None
            cur = _twocol_page_lines(doc[page_no - 1])
            nxt = _twocol_page_lines(doc[page_no])
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 — 분석 실패는 안전 폴백(None)
        return None
    if len(cur) < 3 or len(nxt) < 6:
        return None
    # 현재 페이지가 이미 2COL 로 렌더되면 대상 아님.
    if _twocol_analyze(cur) is not None:
        return None
    # 다음 페이지가 2COL 표로 시작해야 함.
    nsegs = _twocol_analyze(nxt)
    if not nsegs or nsegs[0][0] != "table":
        return None
    rxinfo = _twocol_detect_rx(nxt)
    if not rxinfo:
        return None
    r_x = rxinfo[2]
    hs = sorted(l[3] - l[1] for l in cur)
    h_med = hs[len(hs) // 2] or 10.0
    lm = min(l[0] for l in cur)
    W = max(l[2] for l in cur) - lm
    if W <= 50:
        return None
    tol = 0.8 * h_med
    r_lines = [l for l in cur if l[0] >= r_x - 4.5]
    if not r_lines or len(r_lines) > 6:
        return None
    desc_top = min(l[1] for l in r_lines)
    desc_bot = max(l[3] for l in r_lines)
    l_lines = [l for l in cur
               if l[0] <= lm + 0.10 * W and l[2] < r_x - 6.0
               and abs(l[1] - desc_top) <= tol]
    if len(l_lines) != 1:
        return None
    syn = l_lines[0]
    if len(syn[4].strip()) > 120:
        return None
    # 꼬리(맨 끝)여야 — 설명 아래 다른 내용 없음.
    if any(l[1] > desc_bot + tol for l in cur):
        return None
    # 구문 위 = 제목이거나 세로 갭(영역 시작).
    above = [l for l in cur if l[3] <= syn[1] - 0.1]
    region_start = True
    if above:
        prev = max(above, key=lambda l: l[3])
        is_heading = bool(re.match(r"^\d+(\.\d+)+\s+\S", prev[4].strip()))
        region_start = is_heading or (syn[1] - prev[3]) >= 1.0 * h_med
    if not region_start:
        return None
    syn_txt = re.sub(r"\s+", " ", syn[4].strip())
    desc_txt = re.sub(r"\s+", " ",
                      " ".join(l[4] for l in sorted(r_lines, key=lambda l: (l[1], l[0]))).strip())
    if not desc_txt:
        return None
    return (syn_txt, desc_txt)


def _twocol_reshape_straddle_tail(pdf_path, page_no: int, md):
    """glm 이 문단(구문/설명)으로 렌더한 '페이지 걸침 단일 정의 행'을 결정론적으로
    1행짜리 | Syntax | Description | 표로 승격(게이트: _twocol_straddle_tail, PDF 벡터 기하).

    앵커=PDF 벡터로 얻은 구문 텍스트(syntax hint). glm 이 제목을 '###' 로 냈든 평문으로
    냈든(F8 폰트헤딩 승격은 문서단계라 이 시점엔 평문일 수 있음) 무관하게, md 말미에서
    '구문 힌트로 시작하는 평문 문단'을 앵커로 찾아 그 문단부터 끝까지(평문 문단 ≤3개)를
    표행으로 재구성한다. 게이트/앵커 미충족 시 md 원본 그대로(회귀 0). FMDW_TWOCOL_TABLES
    로 게이트. 멱등(이미 표면 평문 문단이 없어 no-op).
    """
    if not md or not _twocol_enabled():
        return md
    hint = _twocol_straddle_tail(pdf_path, page_no)
    if not hint:
        return md
    syn_hint, _desc_hint = hint

    def _norm(s):
        return re.sub(r"\s+", "", s).lower()

    raw = md.rstrip("\n").split("\n")
    # 빈 줄 경계로 블록 분해: (start_idx, [lines]).
    blocks = []
    buf, start = [], 0
    for idx, ln in enumerate(raw):
        if ln.strip() == "":
            if buf:
                blocks.append((start, buf))
                buf = []
        else:
            if not buf:
                start = idx
            buf.append(ln)
    if buf:
        blocks.append((start, buf))
    if not blocks:
        return md

    def _plain(tl):
        for t in tl:
            s = t.lstrip()
            if s.startswith(("|", "#", "- ", "* ", "> ", "```", "<!--")) or \
                    re.match(r"^\d+[.)]\s", s):
                return False
        return True

    # 구문 힌트로 '시작하는' 평문 문단을 말미(마지막 3블록 이내)에서 앵커로 탐색.
    nh = _norm(syn_hint)
    anchor = -1
    lo = max(0, len(blocks) - 3)
    for bi in range(len(blocks) - 1, lo - 1, -1):
        _s, tl = blocks[bi]
        if not _plain(tl):
            continue
        btxt = _norm(re.sub(r"\s+", " ", " ".join(tl).strip()))
        if btxt.startswith(nh) or (len(btxt) >= 8 and nh.startswith(btxt)):
            anchor = bi
            break
    if anchor < 0:
        return md
    tail = blocks[anchor:]
    # 앵커부터 끝까지 전부 평문 문단, 총 ≤3개(구문 + 설명 랩).
    if len(tail) > 3 or not all(_plain(tl) for _s, tl in tail):
        return md
    tail_texts = [re.sub(r"\s+", " ", " ".join(tl).strip()) for _s, tl in tail]
    if len(tail_texts) >= 2:
        syn_cell, desc_cell = tail_texts[0], " ".join(tail_texts[1:])
    else:
        whole = tail_texts[0]
        if _norm(whole).startswith(nh) and len(nh) < len(_norm(whole)):
            cut = len(syn_hint)
            syn_cell, desc_cell = whole[:cut].strip(), whole[cut:].strip()
        else:
            return md
    a, b = _norm(syn_cell), nh
    if not (a.startswith(b) or b.startswith(a)):
        return md
    if len(syn_cell) > 140 or not desc_cell:
        return md
    tbl = ["| Syntax | Description |", "| :--- | :--- |",
           "| {} | {} |".format(_twocol_cell(syn_cell), _twocol_cell(desc_cell))]
    head = raw[:tail[0][0]]
    while head and head[-1].strip() == "":
        head.pop()
    print("    [2COL-straddle] p{}: 페이지 걸침 단일 정의 행 → Syntax/Description 표 승격"
          .format(page_no), flush=True)
    return "\n".join(head + [""] + tbl)


def _dedup_page_repetition(md: str):
    """glm-ocr intra-page 블록 중복 제거(결정적·보수적).

    glm-ocr(0.9B)가 한 페이지 응답 안에서 본문 전체(또는 큰 연속 블록)를 통째로
    2회 출력하는 사고를 잡는다. "큰 인접(연속) 블록이 그대로 2번 연속 반복"되는
    경우에만 뒤쪽 사본을 제거(첫 출현 유지)한다. 정당하게 반복되는 짧은 표 행 등은
    최소 라인수/문자수 임계로 보존한다. 반복이 없으면 입력과 byte-identical 문자열을
    돌려준다(회귀 0).

    반환: (dedup된 md, 제거했으면 True).
    """
    if not md:
        return md, False
    lines = md.split("\n")
    # 비어있지 않은 라인만 인덱싱해 블록 매칭(공백/구분 라인 무시).
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    n = len(idx)
    if n < 6:  # 너무 짧으면 대상 아님(짧은 표/캡션 보존).
        return md, False
    stripped = [lines[i].strip() for i in idx]

    MIN_BLOCK_LINES = 3
    MIN_BLOCK_CHARS = 200

    best = None  # (s, m): s=블록 시작(비어있지않은 라인 인덱스), m=블록 라인수
    max_m = n // 2
    # 가장 큰 인접 중복 블록을 채택하기 위해 m 을 큰 값부터 탐색.
    for m in range(max_m, MIN_BLOCK_LINES - 1, -1):
        for s in range(0, n - 2 * m + 1):
            # 앵커 저비용 리젝트: 첫 라인이 다르면 슬라이스 비교 생략.
            if stripped[s] != stripped[s + m]:
                continue
            if stripped[s:s + m] != stripped[s + m:s + 2 * m]:
                continue
            if sum(len(x) for x in stripped[s:s + m]) < MIN_BLOCK_CHARS:
                continue
            best = (s, m)
            break
        if best is not None:
            break

    if best is None:
        return md, False

    s, m = best
    # 뒤쪽 사본(비어있지 않은 라인 인덱스 [s+m, s+2m))에 해당하는 원본 라인 구간 제거.
    cut_start = idx[s + m]
    cut_end = idx[s + 2 * m - 1]  # inclusive
    head = lines[:cut_start]
    tail = lines[cut_end + 1:]
    while head and not head[-1].strip():
        head.pop()
    while tail and not tail[0].strip():
        tail.pop(0)
    if tail:
        return "\n".join(head) + "\n\n" + "\n".join(tail), True
    return "\n".join(head), True


def _prepend_page_marker(md: str, page: int) -> str:
    """페이지 결과 선두에 `<!-- page N -->` 마커를 결정적으로 부착.

    모델이 이미 선두에 페이지 마커를 낸 경우(번호 무관) 그 하나를 제거해 중복을
    방지한 뒤 올바른 번호로 재부착한다(_oversized_placeholder 와 동일 규약).
    """
    body = md.lstrip("\n")
    mobj = re.match(r"<!--\s*page\s+\d+\s*-->[ \t]*\n?", body, re.IGNORECASE)
    if mobj:
        body = body[mobj.end():].lstrip("\n")
    return f"<!-- page {page} -->\n\n" + body


# ── 하이브리드 완전성 가드(FMDW_HYBRID_COMPLETENESS, 기본 ON) — 2026-07-09 ─────────
# glm 이 페이지의 일부 블록을 통째로 누락하는 사고(실측: LN08LPU p1-4 p2 좌하단 저작권/
# 연락처 7줄 블록이 kept-glm 인데도 유실). 선택된 본문에서 '진짜 빠진' PDF 벡터텍스트
# 블록을 토큰 겹침(token-overlap) 기반으로 검출해 복구한다.
# 원리(코멘트로 명시): 복구 텍스트는 PDF 원본(ground-truth)이므로 오탐 복구는 최악의
# 경우 '약간의 중복'일 뿐 '창작(fabrication)'이 아니다 → 완전성 쪽으로 편향한다.
# 이메일/URL/전화/저작권 같은 하드 시그널은 본문과 토큰 겹침이 낮아 자연히 '미커버'로
# 잡히므로 여전히 보장되지만, 더 이상 유일 트리거가 아니다(일반 산문 블록도 복구).
_COMPLETE_STOPWORDS = frozenset(
    "the of a an and to in for is on or by with at as be are was were this that its it "
    "from no not any all may can do does has have will shall which who whom into out "
    "about over under such per via than then thus but if so we you they he she".split()
)


def _hybrid_completeness_enabled() -> bool:
    return os.getenv("FMDW_HYBRID_COMPLETENESS", "1").strip().lower() not in (
        "0", "false", "no")


def _sig_tokens(text: str) -> list:
    """유의미 토큰 = 소문자 영숫자 길이≥2, 불용어 제외."""
    return [t for t in re.findall(r"[a-z0-9]{2,}", (text or "").lower())
            if t not in _COMPLETE_STOPWORDS]


def _norm_present(s: str) -> str:
    """존재 판정용 정규화: 소문자 + 구두점 제거 + 공백 압축(substring 비교용)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (s or "").lower())).strip()


def _line_covered(line_text: str, body_tokens: set) -> bool:
    """PDF 라인이 본문에 '커버'됐는지: 유의미 토큰의 ≥60%가 본문 토큰집합에 존재.
    유의미 토큰 <2 개면 커버로 간주(스트레이 숫자/구두점 복구 방지)."""
    st = _sig_tokens(line_text)
    if len(st) < 2:
        return True
    hits = sum(1 for t in st if t in body_tokens)
    return hits / len(st) >= 0.60


def _recover_absent_blocks(clean_lines, body_md: str):
    """읽기순 PDF 라인(F1/F2 정제)에서 본문에 없는 연속 블록(run)을 복구.

    반환: (recovered[run 별 라인], run_spans[(a,b)], covered[라인별 bool], total).
      run_spans[i] = recovered[i] 의 clean_lines 인덱스 범위 [a,b). 배치부가 covered 와
      함께 '단조(읽기순 forward) 정렬'로 삽입 위치를 정한다(같은 텍스트가 본문 앞부분에도
      있을 때 뒤 블록이 앞으로 튀는 것 방지).
    가드: run 내 최소 1줄이 유의미토큰 ≥3(스트레이 단어 run 배제). 이미 본문에 있는
    라인(normalized substring)·이미 복구한 라인은 스킵(중복 0).
    """
    if not clean_lines or not body_md:
        return [], [], [], 0
    body_tokens = set()
    for bl in body_md.split("\n"):
        body_tokens.update(_sig_tokens(bl))
    body_norm = _norm_present(body_md)
    covered = [_line_covered(l[4], body_tokens) for l in clean_lines]
    n = len(clean_lines)
    runs = []
    i = 0
    while i < n:
        if covered[i]:
            i += 1
            continue
        j = i
        while j < n and not covered[j]:
            j += 1
        runs.append((i, j))
        i = j
    recovered = []
    run_spans = []
    seen_norm = set()
    total = 0
    for (a, b) in runs:
        run_lines = []
        for k in range(a, b):
            t = clean_lines[k][4]
            nt = _norm_present(t)
            if len(nt) < 2 or nt in body_norm or nt in seen_norm:
                continue  # 이미 본문/복구분에 존재 → 중복 스킵
            run_lines.append(t)
            seen_norm.add(nt)
        if not run_lines:
            continue
        if not any(len(_sig_tokens(t)) >= 3 for t in run_lines):
            continue  # 스트레이 단어 run 배제(과복구 노이즈 가드)
        recovered.append(run_lines)
        run_spans.append((a, b))
        total += len(run_lines)
    return recovered, run_spans, covered, total


def _best_forward_match(text, body_line_tokens, after_pos, thresh: float = 0.6):
    """text 의 유의미 토큰과 가장 겹치는 본문 라인 index(단, after_pos '초과'만 탐색)."""
    at = set(_sig_tokens(text))
    if not at:
        return None
    best, best_ov = None, 0.0
    for idx in range(after_pos + 1, len(body_line_tokens)):
        bt = body_line_tokens[idx]
        if not bt:
            continue
        ov = len(at & bt) / len(at)
        if ov > best_ov:
            best_ov, best = ov, idx
    return best if best_ov >= thresh else None


def _anchor_outside_table_fence(body_lines, idx: int) -> int:
    """삽입 앵커가 GFM 표(`|` 행)·코드펜스 '내부'면 그 블록 '바로 앞'으로 당긴다(방어).

    복구 블록을 표/펜스 중간에 끼워 넣어 구조를 깨는 것을 방지(Advisor 비차단 권고
    2026-07-10, 현 코퍼스엔 미발생하나 향후 안전장치). 앵커가 표/펜스 밖이면 그대로 반환."""
    n = len(body_lines)
    if idx <= 0 or idx >= n:
        return idx
    # 코드펜스 내부? idx 이전 펜스 토글이 홀수면 내부 → 여는 펜스 줄 앞으로.
    in_fence = False
    fence_open = -1
    for i in range(idx):
        if _MD_STYLE_FENCE_RE.match(body_lines[i]):
            in_fence = not in_fence
            if in_fence:
                fence_open = i
    if in_fence and fence_open >= 0:
        return fence_open
    # GFM 표 행 내부? 앵커가 `|` 행이면 표 블록 시작 행으로 당긴다.
    if body_lines[idx].lstrip().startswith("|"):
        j = idx
        while j > 0 and body_lines[j - 1].lstrip().startswith("|"):
            j -= 1
        return j
    return idx


def _estimate_body_line_y(clean_lines, covered, body_lines):
    """각 body 라인의 추정 y좌표 리스트(매칭 실패=None).

    covered PDF 라인(ground-truth 위치)을 body 라인에 토큰 겹침(clean 토큰 기준, ≥0.6)
    으로 매핑해, 매칭된 body 라인에 그 PDF 라인의 y 를 부여한다. 한 body 라인(문단 병합)
    에 여러 PDF 라인이 매핑되면 최상단(최소 y) 을 채택(문단은 그 시작 y 로 대표). 매칭
    공식·임계값(0.6)은 기존 _best_forward_match / _line_covered 와 동일 계약(단조 body 에서
    forward-match 와 같은 앵커를 내도록).
    """
    body_line_tokens = [set(_sig_tokens(bl)) for bl in body_lines]
    body_y = [None] * len(body_lines)
    for k in range(len(clean_lines)):
        if not covered[k]:
            continue
        ct = set(_sig_tokens(clean_lines[k][4]))
        if not ct:
            continue
        best_idx, best_ov = None, 0.0
        for bidx, bt in enumerate(body_line_tokens):
            if not bt:
                continue
            ov = len(ct & bt) / len(ct)
            if ov > best_ov:
                best_ov, best_idx = ov, bidx
        if best_idx is not None and best_ov >= 0.6:
            cy = clean_lines[k][1]
            if body_y[best_idx] is None or cy < body_y[best_idx]:
                body_y[best_idx] = cy
    return body_y


def _place_recovered_blocks(clean_lines, covered, run_spans, recovered, body_lines):
    """복구 블록을 '원본 PDF y좌표(읽기순)' 위치에 삽입해 body 를 재조립한다(결정론).

    PLACEMENT(2026-07-11): 각 복구 블록의 y = run 첫 라인의 실제 PDF y(block_y). body
    라인별 추정 y(_estimate_body_line_y)를 body 순서로 스캔해 '블록보다 y 가 큰(아래) 첫
    라인' 직전에 삽입한다 → glm 이 앵커(캡션 등)를 본문 말미로 재배치해도 블록이 함께
    밀려나지 않고 읽기순이 보존된다(p149-151 p1 intro → 4.2.9 앞).

    단조(읽기순) body 에서는 '블록 뒤 첫 covered 라인 직전' = 구(舊) forward-match 앵커와
    동일 결과(회귀 0). 블록보다 아래인 매칭 body 라인이 하나도 없으면(진짜 후미 블록:
    p1-4 저작권) 페이지 말미 append(현행 폴백 유지). 위치를 확신할 때만 원위치 삽입.
    표/펜스 방어(_anchor_outside_table_fence)는 그대로 적용.
    """
    body_y = _estimate_body_line_y(clean_lines, covered, body_lines)
    inserts_before = {}   # body_idx → [block, ...]
    append_blocks = []
    # recovered/run_spans 는 읽기순(y 오름차순) → ri 순회가 곧 block_y 오름차순.
    for ri in range(len(recovered)):
        block_y = clean_lines[run_spans[ri][0]][1]
        anchor = None
        for bidx in range(len(body_lines)):
            by = body_y[bidx]
            if by is not None and by > block_y:
                anchor = bidx
                break
        if anchor is None:
            append_blocks.append(recovered[ri])  # 후미 블록 → 말미 append(분리 없음)
        else:
            anchor = _anchor_outside_table_fence(body_lines, anchor)  # 표/펜스 방어
            inserts_before.setdefault(anchor, []).append(recovered[ri])
    out = []
    for idx, bl in enumerate(body_lines):
        if idx in inserts_before:
            for block in inserts_before[idx]:
                out.extend(block)
                out.append("")  # 블록과 앵커 사이 빈 줄
        out.append(bl)
    text = "\n".join(out).rstrip("\n")
    if append_blocks:
        adds = "\n\n".join("\n".join(b) for b in append_blocks)
        return text + "\n\n" + adds + "\n"
    return text + "\n"


def _apply_hybrid_completeness(pdf_path, page: int, body_md):
    """선택된 페이지 본문에 glm 이 누락한 PDF 벡터텍스트 블록을 '읽기 위치'에 복구.

    PLACEMENT(2026-07-11 개선): 복구 블록을 원본 PDF y좌표(읽기순) 위치에 삽입한다
    (_place_recovered_blocks). 구(舊) 'run 뒤 첫 covered 라인 forward-match' 앵커는 glm 이
    그 covered 라인(예: Figure 캡션)을 본문 말미로 재배치했을 때 복구 블록까지 함께 밀어내
    읽기순을 역전시켰다(p149-151 p1: intro 가 DRC 뒤로). 개선안은 body 각 라인의 추정 y 를
    스캔해 '블록보다 y 큰(아래) 첫 라인' 직전에 삽입하므로, 앵커가 본문 어디에 있든 블록은
    자기 y 자리에 놓인다. 단조(읽기순) body 에선 구 앵커와 동일 결과(회귀 0). y 로 위치를
    확신 못하면(아래 매칭 라인 없음) 페이지 말미 append(현행 폴백).
    """
    if not body_md or not _hybrid_completeness_enabled():
        return body_md
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        try:
            clean_lines = _twocol_page_lines(doc[page - 1])  # F1/F2 정제 + 읽기순
        finally:
            doc.close()
    except Exception as e:  # noqa: BLE001 — 완전성 가드 실패는 비차단(본문 그대로)
        print(f"    [~] p{page}: completeness 스캔 실패(무시): {e}", flush=True)
        return body_md
    recovered, run_spans, covered, total = _recover_absent_blocks(clean_lines, body_md)
    if not recovered:
        return body_md
    print(f"    [COMPLETE] p{page}: recovered {total} lines", flush=True)
    return _place_recovered_blocks(
        clean_lines, covered, run_spans, recovered, body_md.split("\n"))


# ── F11: 결정론적 그리드 표 추출(FMDW_GRID_TABLES, 기본 ON) — 2026-07-09 ──────────
# glm-OCR 이 그룹 헤더를 한 열로 붕괴시키고 다중행 셀을 어긋나게 하는 사고(실측 p16-18
# Table 2: 'Minimum Dimension' 그룹의 Line/Space 서브헤더 소실·main/SRAM 행 shift).
# PyMuPDF find_tables + F1 회전 필터(대각 워터마크 셀 혼입 방지)로 결정론적 셀 추출.
# 안전 원칙(회귀가 최대 리스크): (a) 잘-형성된(consistent cols·≥2r·≥2c·fill≥0.4·비-mangle)
# 표만 채택, (b) glm 이 이미 `|`-블록으로 낸 표에만 교체(헤더 토큰 겹침 매칭) — find_tables
# 오검출(where: 류)·미매칭 표는 삽입 안 함(보수), (c) 회전헤더가 열경계로 쪼개져 mangle
# 되는 표(p149-151 Table23 실측)는 mangle 가드로 거부→glm 유지.
_GRID_MANGLE_RE = re.compile(r"(?:^|\s)[a-z]?_[0-9A-Za-z]")  # 열경계가 코드토큰 쪼갬(예 x_1Gx)


def _grid_tables_enabled() -> bool:
    return os.getenv("FMDW_GRID_TABLES", "1").strip().lower() not in ("0", "false", "no")


def _grid_clean_cell(page, bbox) -> str:
    """셀 bbox 의 수평 텍스트만 추출(F1: 대각 워터마크 라인 제외). find_tables 권장 패턴."""
    if bbox is None:
        return ""
    import fitz

    parts = []
    for b in page.get_text("dict", clip=fitz.Rect(bbox)).get("blocks", []):
        for l in b.get("lines", []):
            if abs(l.get("dir", (1, 0))[1]) > 0.2:
                continue  # 회전(대각/세로) 라인 제외
            t = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
            if t:
                parts.append(t)
    return " ".join(parts)


_GFM_SEP_RE = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")


def _grid_header_rows(grid) -> int:
    """헤더 행 수: 2단 그룹헤더(row0=그룹명+빈칸, row1=서브라벨)면 2, 아니면 1.
    _render_grid_gfm 의 grouped 판정과 동일 규약."""
    if len(grid) >= 2:
        ncol = max(len(r) for r in grid)
        g0 = list(grid[0]) + [""] * (ncol - len(grid[0]))
        g1 = list(grid[1]) + [""] * (ncol - len(grid[1]))
        grouped = (
            any(not c.strip() for c in g0) and not g1[0].strip()
            and any(g1[j].strip() and not g0[j].strip() for j in range(ncol))
        )
        if grouped:
            return 2
    return 1


def _grid_header_mangled(grid) -> bool:
    """헤더 행(그룹/서브)에 mangle(회전헤더 열경계 쪼갬: 'x_1Gx 12M_3M' 류) 흔적이 있는가."""
    for r in grid[:_grid_header_rows(grid)]:
        for c in r:
            if _GRID_MANGLE_RE.search(c):
                return True
    return False


def _splice_glm_header(rendered_gi, block_lines, grid) -> list:
    """헤더-mangle 격자: 데이터는 find_tables(결정론, 무손실), 헤더는 glm 의 깨끗한 라벨로 합성.

    find_tables 열경계(데이터 격자선 기준)는 데이터엔 정확하나, 긴 수평 헤더 라벨이 좁은
    열을 가로질러 넘쳐(overflow) 셀 중심이 옆 열로 떨어진다 → 어떤 클립/센터 전략으로도
    헤더 혼입('x_1Gx 12M_3M')을 못 없앤다(기하 문제, 클립 버그 아님, p151 Table23 실측).
    그래서 헤더 라벨만 glm(전체 이미지를 논리 단위로 읽음)에서 취한다. 데이터는 100% 격자.
    2단 그룹헤더면 grid 그룹행(row0=그룹명 스팬)은 유지하고 서브헤더(row1)만 glm 셀로 교체 후
    _render_grid_gfm 의 완전수식 경로로 재렌더('Preferred Orientation 12M_3Mx_5Dx' — p85-87
    Table19 골든과 동형). 단일 헤더 mangle 이면 glm flat 헤더 + grid 데이터. 열 수 불일치/
    무데이터/헤더 비-mangle 이면 rendered_gi 그대로(회귀 0·데이터 무손실)."""
    if not _grid_header_mangled(grid):
        return rendered_gi
    sep_i = next((k for k, l in enumerate(block_lines) if _GFM_SEP_RE.match(l)), None)
    if sep_i is None or sep_i == 0 or len(rendered_gi) < 3:
        return rendered_gi
    glm_cells = [c.strip() for c in block_lines[sep_i - 1].split("|")][1:-1]
    grid_data = rendered_gi[2:]

    def _ncols(line):
        return len([c for c in line.split("|")]) - 2

    if not grid_data:
        return rendered_gi
    n_data = _ncols(grid_data[0])
    ncol = max(len(r) for r in grid)
    if len(glm_cells) != n_data or ncol != n_data:
        return rendered_gi  # 열 수 불일치 → 안전 폴백

    def _clean(s):
        return re.sub(r"\s*_\s*", "_", s).strip()  # 코드토큰 내부 공백 아티팩트 정리

    if _grid_header_rows(grid) >= 2:
        g0 = list(grid[0]) + [""] * (ncol - len(grid[0]))
        new_sub = []
        for j in range(ncol):
            outside = bool(g0[j].strip()) and (j + 1 >= ncol or bool(g0[j + 1].strip()))
            new_sub.append("" if outside else _clean(glm_cells[j]))
        # 하드닝(2026-07-10, Advisor QA Minor #1): glm col0 라벨이 grid col0 공란을
        # 채우면 _render_grid_gfm 의 grouped 판정(row1[0] 공란 필수)이 깨져 서브헤더
        # 행이 조용히 데이터 행으로 강등된다(표 손상). splice 전 grouped 유지 여부를
        # 동일 조건으로 사전 검증 — 깨질 경우 splice 를 건너뛰고 splice 전 grid 렌더
        # (rendered_gi)로 폴백해 데이터 행 손실 0 을 보장한다.
        grouped_ok = (not new_sub[0].strip()) and any(
            new_sub[j].strip() and not g0[j].strip() for j in range(ncol)
        )
        if not grouped_ok:
            return rendered_gi
        new_grid = [g0, new_sub] + [list(r) for r in grid[2:]]
        return _render_grid_gfm(new_grid)  # grouped 완전수식 경로 재사용
    header = "| " + " | ".join(glm_cells) + " |"
    sep = "| " + " | ".join([":---"] * len(glm_cells)) + " |"
    return [header, sep] + list(grid_data)


def _table_well_formed(grid) -> bool:
    """find_tables 결과 채택 가드: 일관된 열수·≥2행·≥2열·비-사소 fill·비-mangle."""
    if len(grid) < 2:
        return False
    if len({len(r) for r in grid}) != 1:  # 열 수 일관성
        return False
    if len(grid[0]) < 2:
        return False
    filled = sum(1 for r in grid for c in r if c.strip())
    total = sum(len(r) for r in grid) or 1
    if filled / total < 0.4:  # 대부분 빈 오검출 배제(그룹표 0.69~0.73 은 통과)
        return False
    # mangle(헤더 라벨 overflow 로 인한 열경계 혼입)는 '데이터 행'에서만 거부(2026-07-10).
    # 헤더에만 mangle 이면 표를 채택하고 배치 시 glm 의 깨끗한 라벨로 splice(데이터 무손실 우선).
    # 실측: 이 완화가 well_formed 를 바꾸는 표는 전 페이지 통틀어 p151 Table23 단 하나(회귀 0).
    for r in grid[_grid_header_rows(grid):]:
        for c in r:
            if _GRID_MANGLE_RE.search(c):
                return False  # 데이터 행 mangle → 거부(glm 유지)
    return True


def _render_grid_gfm(grid) -> list:
    """grid(행×열 텍스트)를 GFM 표 라인 목록으로.

    2단 그룹헤더(row0=그룹명+빈 이웃, row1=그 아래 서브라벨)는 '완전수식 단일 헤더'로
    평탄화한다(2026-07-10, RAG/LLM 자기서술): 각 서브열 헤더 = "{그룹명} {서브라벨}",
    그룹 밖 열은 원래 헤더 유지. 별도 **굵은** 서브라벨 행은 방출하지 않는다(추론 불필요).
    또 스퍼리어스 빈-헤더 열(find_tables 병합셀 아티팩트)은 '데이터 무손실'일 때만 제거
    (헤더 공란 且 데이터 전부 공란 또는 같은 행 다른 열에 중복 = 유니크 데이터 0)."""
    ncol = max(len(r) for r in grid)

    def _row(r):
        return [_twocol_cell(c) for c in r] + [""] * (ncol - len(r))

    g = [_row(r) for r in grid]
    grouped = (
        len(g) >= 2 and any(not c.strip() for c in g[0]) and not g[1][0].strip()
        and any(g[1][j].strip() and not g[0][j].strip() for j in range(ncol))
    )
    if grouped:
        header = []
        group_name = ""
        for j in range(ncol):
            h0, h1 = g[0][j].strip(), g[1][j].strip()
            if h0:
                group_name = h0            # 새 그룹/평범 열 이름(오른쪽으로 전파)
            if h1:
                header.append((group_name + " " + h1).strip() if group_name else h1)
            else:
                header.append(h0)          # 그룹 밖 평범 열(빈 헤더 가능)
        data_rows = g[2:]                  # row0(그룹)+row1(서브)를 헤더로 병합 → 데이터는 이후
    else:
        header = list(g[0])
        data_rows = g[1:]

    # 스퍼리어스 빈-헤더 열 제거(무손실 보장): 헤더 공란 且 모든 데이터셀이 공란이거나
    #   같은 행의 다른 열에도 존재(중복). 유니크 값이 하나라도 있으면 보존.
    keep = []
    for j in range(ncol):
        if header[j].strip():
            keep.append(j)
            continue
        droppable = True
        for r in data_rows:
            v = r[j].strip()
            if v and not any(k != j and r[k].strip() == v for k in range(len(r))):
                droppable = False
                break
        if not droppable:
            keep.append(j)
    if not keep:
        keep = list(range(ncol))           # 안전장치: 전부 드롭 방지
    header = [header[j] for j in keep]
    data_rows = [[r[j] for j in keep] for r in data_rows]
    nc = len(keep)
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join([":---"] * nc) + " |"]
    for r in data_rows:
        out.append("| " + " | ".join(r) + " |")
    return out


def _find_gfm_blocks(lines) -> list:
    """본문에서 연속 `|`-라인 블록(표) 범위 목록 [(start,end)] (포함). 최소 2줄."""
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            if j - i >= 2:
                blocks.append((i, j - 1))
            i = j
        else:
            i += 1
    return blocks


def _grid_page_info(pdf_path, page: int):
    """(grids[(grid,bbox)], page_height, non_table_line_count). 실패 시 ([],0.0,0).

    non_table_line_count = F1/F2 정제 후 그리드 bbox 밖에 남는 라인 수(0 이면 페이지가
    사실상 표만 = grid-only). grids 는 잘-형성된(_table_well_formed) 표만.
    """
    try:
        import fitz

        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return [], 0.0, 0
    grids = []
    ph = 0.0
    n_nontable = 0
    try:
        pg = doc[page - 1]
        ph = pg.rect.height or 0.0
        pw = pg.rect.width or 1.0
        try:
            tabs = pg.find_tables()
        except Exception:  # noqa: BLE001
            return [], ph, 0
        for t in tabs.tables:
            try:
                grid = [[_grid_clean_cell(pg, c) for c in r.cells] for r in t.rows]
            except Exception:  # noqa: BLE001
                continue
            if _table_well_formed(grid):
                grids.append((grid, tuple(t.bbox)))
        if grids:
            def _in_grid(line):
                # Advisor 하드닝(2026-07-09): y-밴드 + x-겹침 동시 확인 — 표 세로 밴드에
                # 있어도 가로로 표 밖(옆 단)인 산문 라인은 in-table 로 세지 않는다.
                lx0, ly0, lx1 = line[0], line[1], line[2]
                for (_g, b) in grids:
                    if (b[1] - 2 <= ly0 <= b[3] + 2
                            and lx0 < b[2] + 2 and lx1 > b[0] - 2):
                        return True
                return False
            try:
                clean = _page_lines_with_size(pg.get_text("dict"), ph or 1.0, pw)
                n_nontable = sum(1 for l in clean if not _in_grid(l))
            except Exception:  # noqa: BLE001
                n_nontable = 1  # 불확실 → 보수적으로 grid-only 로 보지 않음
    finally:
        doc.close()
    return grids, ph, n_nontable


def _page_has_grid(pdf_path, page: int) -> bool:
    """페이지에 잘-형성된 find_tables 표가 있는가(2COL 우선순위 판정용)."""
    if not _grid_tables_enabled():
        return False
    grids, _ph, _n = _grid_page_info(pdf_path, page)
    return bool(grids)


def _apply_grid_tables(pdf_path, page: int, body_md):
    """find_tables 표를 본문에 반영(F11). 배치 전략(회귀 최소):

    (0) grid-only 페이지(비-표 라인 0 + 커버리지≥0.6): 본문(마커·헤딩·캡션 보존)을 표로 대체
        — 머리없는 연속 표(p85-87 p3) 처리. glm 이 잘못 낸 것(2-col/산문)을 폐기.
    (1) 그 외: glm `|`-블록에 헤더 토큰겹침≥0.5 매칭 → 교체(빠른 경로, p16-18 Table2).
    (2) 미매칭 표는 남은 `|`-블록과 읽기순으로 짝지어 교체(glm 이 헤더를 뭉갠 경우).
    매칭/블록이 전혀 없으면 무변경(보수).
    """
    if not body_md or not _grid_tables_enabled():
        return body_md
    grids_info, ph, n_nontable = _grid_page_info(pdf_path, page)
    if not grids_info:
        return body_md
    grids_info = sorted(grids_info, key=lambda gb: gb[1][1])  # 읽기순(bbox y0)
    rendered = [_render_grid_gfm(g) for (g, _b) in grids_info]
    lines = body_md.split("\n")

    # (0) grid-only 페이지 → 본문 전체를 그리드로 대체(마커/헤딩/캡션만 보존)
    cov = (sum((b[3] - b[1]) for (_g, b) in grids_info) / ph) if ph else 0.0
    if n_nontable == 0 and cov >= 0.6:
        keep = [l for l in lines if l.strip().startswith("<!-- page")
                or l.lstrip().startswith("#")
                or l.lstrip().startswith("**Table") or l.lstrip().startswith("**Figure")]
        out = keep[:]
        for r in rendered:
            out.append("")
            out.extend(r)
        print(f"    [GRID] p{page}: grid-only 페이지 → find_tables 표 "
              f"{len(rendered)}개로 본문 대체(F11)", flush=True)
        return "\n".join(out).strip("\n") + "\n"

    # (1)+(2) glm |-블록 splice
    blocks = _find_gfm_blocks(lines)
    used = set()
    replacements = {}
    matched = [False] * len(grids_info)
    for gi, (grid, _b) in enumerate(grids_info):
        ht = set(_sig_tokens(" ".join(grid[0])))
        if not ht:
            continue
        best, best_ov = None, 0.0
        for (bs, be) in blocks:
            if (bs, be) in used:
                continue
            gt = set(_sig_tokens(lines[bs]))
            if not gt:
                continue
            ov = len(ht & gt) / min(len(ht), len(gt))
            if ov > best_ov:
                best_ov, best = ov, (bs, be)
        if best and best_ov >= 0.5:
            replacements[best[0]] = (best[1], _splice_glm_header(
                rendered[gi], lines[best[0]:best[1] + 1], grids_info[gi][0]))
            used.add(best)
            matched[gi] = True
    # (2) 미매칭 표 ↔ 남은 블록: '내용 포함(containment)' 확인 후에만 짝지어 교체.
    #     glm 이 같은 표를 뭉갠 경우 그 블록 토큰은 표 전체 토큰의 부분집합 → 높은 포함율.
    #     무관한 다른 표(예 p149-151 Abbreviation grid ↔ Level 블록)는 포함율 낮아 미짝(오교체 방지).
    grid_all = [set(_sig_tokens(" ".join(c for r in g for c in r)))
                for (g, _b) in grids_info]
    for gi in range(len(grids_info)):
        if matched[gi]:
            continue
        best_b, best_cont = None, 0.0
        for b in blocks:
            if b in used:
                continue
            bt = set(_sig_tokens(" ".join(lines[b[0]:b[1] + 1])))
            if not bt:
                continue
            cont = len(bt & grid_all[gi]) / len(bt)
            if cont > best_cont:
                best_cont, best_b = cont, b
        if best_b and best_cont >= 0.5:
            replacements[best_b[0]] = (best_b[1], _splice_glm_header(
                rendered[gi], lines[best_b[0]:best_b[1] + 1], grids_info[gi][0]))
            used.add(best_b)
            matched[gi] = True
    # (3) PATH 3(2026-07-10): glm 이 검출된 격자표를 `|`-블록이 아니라 '불릿/산문'으로 낸 경우.
    #     path0/1/2 미배치 격자에 대해, 비-표(`|`아님)·비-펜스·비-헤딩·비-마커 라인의 '연속 run'
    #     중 격자 셀 토큰과 '양방향 강매칭'(≥0.7 & ≥0.7) 且 길이≈행수(±1) 인 run 을 표로 교체.
    #     (p85-87 §3.6.1 'where:' 3열 표를 glm 이 불릿으로 낸 사례.) 강가드로 정상 불릿리스트 보존.
    if any(not m for m in matched):
        reserved = set()
        for _s, (_e, _g) in replacements.items():
            reserved.update(range(_s, _e + 1))
        for (bs, be) in blocks:
            reserved.update(range(bs, be + 1))
        in_fence = [False] * len(lines)
        _f = False
        for _idx, _l in enumerate(lines):
            if _MD_STYLE_FENCE_RE.match(_l):
                _f = not _f
                in_fence[_idx] = True   # 펜스 토글 라인 자체도 제외
            else:
                in_fence[_idx] = _f

        def _p3_eligible(idx):
            if idx in reserved or in_fence[idx]:
                return False
            st = lines[idx].strip()
            if not st or st.startswith("#") or st.startswith("<!-- page") or st.startswith("|"):
                return False
            return True

        runs = []
        _i = 0
        while _i < len(lines):
            if _p3_eligible(_i):
                _j = _i
                while _j < len(lines) and _p3_eligible(_j):
                    _j += 1
                runs.append((_i, _j - 1))
                _i = _j
            else:
                _i += 1
        used_runs = set()
        for gi in range(len(grids_info)):
            if matched[gi]:
                continue
            gtoks = grid_all[gi]
            grow = len(grids_info[gi][0])   # 격자 행수
            if not gtoks:
                continue
            best_run, best_score = None, 0.0
            for (rs, re_) in runs:
                if (rs, re_) in used_runs:
                    continue
                rlen = re_ - rs + 1
                # 길이 가드: run ≈ 격자 행수(리드인 포함) 또는 데이터행수(행수-1) ±1
                if abs(rlen - grow) > 1 and abs(rlen - (grow - 1)) > 1:
                    continue
                rtoks = set(_sig_tokens(" ".join(lines[rs:re_ + 1])))
                if not rtoks:
                    continue
                inter = len(gtoks & rtoks)
                fwd = inter / len(gtoks)    # 격자 토큰이 run 에 존재하는 비율
                bwd = inter / len(rtoks)    # run 토큰이 격자에서 온 비율
                if fwd >= 0.70 and bwd >= 0.70 and min(fwd, bwd) > best_score:
                    best_score, best_run = min(fwd, bwd), (rs, re_)
            if best_run is not None:
                replacements[best_run[0]] = (best_run[1], rendered[gi])
                used_runs.add(best_run)
                matched[gi] = True
    if not replacements:
        return body_md
    out = []
    i = 0
    while i < len(lines):
        if i in replacements:
            end, gfm = replacements[i]
            out.extend(gfm)
            i = end + 1
        else:
            out.append(lines[i])
            i += 1
    print(f"    [GRID] p{page}: find_tables 로 표 {sum(matched)}개 반영(F11)", flush=True)
    return "\n".join(out)


# ── F12: 결정론적 runaway/truncation 정리(FMDW_DERUNAWAY, 기본 ON) — 2026-07-09 ────
# glm 표-폭주 환각(같은 짧은 토큰 'DV/LV' 를 1531× 반복 + finish_reason=length 잘림)이
# 정상 표(F11 grid) 뒤에 쓰레기로 남는 사고. 실제 데이터는 F11 grid 에 이미 있으므로
# 이 폭주 라인/마커를 제거한다(무손실). 보수: '짧은(≤12자) 동일 토큰이 ≥10회 연속' 인
# 명백한 퇴행만 트리거 — 정상 표(연속 최대 ~7)·산문은 무변경.
_TRUNCATED_MARKER_RE = re.compile(r"^\s*<!--\s*TRUNCATED\b.*?-->\s*$",
                                  re.IGNORECASE | re.DOTALL)


def _derunaway_enabled() -> bool:
    return os.getenv("FMDW_DERUNAWAY", "1").strip().lower() not in ("0", "false", "no")


def _consec_short_repeat(tokens, min_rep: int = 20, max_tok: int = 12,
                         code_like: bool = False) -> bool:
    """토큰열에서 '비어있지 않은 동일 짧은(≤max_tok) 토큰이 min_rep 회 이상 연속'이면 True.
    빈 토큰은 연속을 끊는다(그룹표 빈 셀 오탐 방지). code_like=True 면 순수 알파벳 단어는
    제외(반복 '단어'는 미판정, 'DV/LV'·'S5' 처럼 숫자/기호 포함 코드 셀만 판정).
    min_rep=20(Advisor 하드닝 2026-07-09): 실제 폭주 1531× 대비 여유 크게 — 진리표/레지스터
    행의 정당한 10~19 연속 동일 셀값(예 '1 1 1 … ×12')을 보호."""
    run = 1
    prev = None
    for p in tokens:
        p = p.strip()
        if p == "":
            prev = None
            run = 1
            continue
        if p == prev:
            run += 1
        else:
            prev = p
            run = 1
        if len(p) <= max_tok and run >= min_rep:
            if code_like and p.isalpha():
                continue  # 순수 단어 반복은 tier-2 에서 트리거 안 함
            return True
    return False


def _is_gfm_separator(line: str) -> bool:
    """GFM 표 구분행(`| :--- | :--- | ... |`) 인가 — 넓은 표에서 `:---` 가 ≥10 반복돼
    폭주로 오탐되는 것을 막기 위해 별도 판정(구분행은 절대 폭주 아님)."""
    cells = [c.strip() for c in line.split("|") if c.strip() != ""]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _is_runaway_line(line: str) -> bool:
    """퇴행 폭주 라인 판정: (1) `|`/`,` 구분 셀 연속 반복(모든 짧은 토큰), (2) 긴 라인의
    셀 내부 공백구분 연속 반복(코드형 토큰만). GFM 구분행·정상 표행(빈 셀이 연속을 끊음,
    알려진 표 최대 연속 ~7)·산문은 트리거되지 않는다."""
    if _is_gfm_separator(line):
        return False
    if _consec_short_repeat(re.split(r"[|,]", line)):
        return True
    if len(line) > 200 and _consec_short_repeat(re.split(r"\s+", line), code_like=True):
        return True
    return False


def _clean_runaway(md):
    """F12: `<!-- TRUNCATED... -->` 마커 + 퇴행 폭주 라인 제거. 무변경 시 원문 그대로."""
    if not md or not _derunaway_enabled():
        return md
    lines = md.split("\n")
    out = []
    n_run = 0
    n_trunc = 0
    for l in lines:
        if _TRUNCATED_MARKER_RE.match(l):
            n_trunc += 1
            continue
        if _is_runaway_line(l):
            n_run += 1
            continue
        out.append(l)
    if n_run or n_trunc:
        print(f"    [DERUNAWAY] p?: 폭주라인 {n_run}개·TRUNCATED마커 {n_trunc}개 제거(F12)",
              flush=True)
        return "\n".join(out)
    return md


def _chunk_shows_truncation(md) -> bool:
    """멀티페이지 청크 전사물이 거대표 폭주/절단 실패 서명을 보이는가(F12b, 2026-07-10).

    `<!-- TRUNCATED ... -->` 마커 또는 퇴행 폭주 라인(_is_runaway_line)이 하나라도 있으면
    True. 이 서명은 거대 매트릭스 표를 본문 LLM 이 GFM 으로 전사하다 길이초과로 잘린
    전형적 실패 모드다 — 멀티페이지 청크에서는 이 잘림이 **같은 청크 뒤 페이지를 통째로
    삼켜(page-loss)** 버린다. 호출자는 이 서명이 보이면 페이지 단위 재추출로 전환해
    페이지 간 오염/유실을 차단한다. 정상 전사물(폭주/절단 없음)에는 False → 무영향."""
    if not md:
        return False
    for l in md.split("\n"):
        if _TRUNCATED_MARKER_RE.match(l) or _is_runaway_line(l):
            return True
    return False


def _hybrid_transcribe_page(pdf_path, page: int, prompt: str):
    """단일 페이지 하이브리드 전사: glm(1차) → 커버리지 체크 → 필요시 qwen(폴백) →
    best-of 선택(둘 중 더 완전한 쪽 채택).

    커버리지 체크: pdf_text_len(selectable 텍스트) >= FMDW_HYBRID_MIN_TEXT 이고
    glm 출력 길이 < FMDW_HYBRID_COVERAGE_MIN × pdf_text_len 이면 glm 이 페이지를
    스킵한 것으로 간주하고 그 페이지만 qwen 으로 재전사를 "시도"한다. 실제 이미지
    위주라 pdf_text_len 이 작은 페이지(도면/오버사이즈 표 등)는 가드에 걸려 폴백이
    발동하지 않는다.

    2026-07-04 best-of 선택 추가(실사고): 커버리지 미달 판정이 곧 glm 이 실제로
    불완전하다는 보장은 아니며, qwen3-vl 은 조밀한 페이지(예: 밀집 TOC)에서
    비결정적으로 매우 짧은 결과를 낼 수 있다(실측: 같은 페이지가 어떤 run 에서는
    6136자, 다른 run 에서는 56자). 폴백 결과를 무조건 채택하면 glm 1411자가 qwen
    56자로 **교체되어 오히려 내용이 나빠지는** 사고가 발생했다. 따라서 qwen 결과가
    "실제로 glm 보다 더 길 때만" 채택하고, 그렇지 않으면 glm 을 그대로 유지한다 —
    최악의 경우에도 glm 베이스라인보다 나빠지지 않는다(폴백은 순수 이득이거나 무해,
    손해는 없음).

    Returns:
        (markdown|None, 사용모델 라벨). 1차/폴백 모두 실패(둘 다 비어있음)하면
        (None, "failed") — 호출자(_hybrid_extract_range)가 MISSING 마커로 처리.
    """
    # 2열 정의 레이아웃 결정론 변환(FMDW_TWOCOL_TABLES, 2026-07-09): 정의 영역이
    # 검출된 페이지는 PDF 벡터 텍스트(ground truth)로 페이지 전체를 렌더(영역=GFM 표)
    # 하고 OCR 호출 자체를 생략한다. 미검출/분석실패 시 기존 glm 경로 그대로(회귀 0).
    if _twocol_enabled():
        _t2 = _twocol_try_render_page(pdf_path, page)
        if _t2:
            return _t2, "pdftext-2col"
    pdf_text_len = _pdf_page_text_len(pdf_path, page)

    # glm(primary)에는 FIGURE_RULES 를 뺀 순수 전사 프롬프트를 준다(2026-07-07).
    # glm-ocr(0.9B)은 큰 FIGURE_RULES 를 "출력 지시"로 오해해, 도면이 없는 표/본문
    # 페이지에까지 figure 템플릿(**Figure N:** ... / **Type**: ... / **Visual Encoding**)을
    # 복창·환각한다(실측 2026-07-07 LN08LPU p3 Revision History 표 → 9,334자 오염).
    # 도면 설명은 별도 figure 단계(qwen3-vl:32b detect/describe)가 담당하므로 glm 은
    # 순수 텍스트/표 전사만 수행한다. qwen 폴백에는 기존 prompt(FIGURE_RULES 포함) 유지.
    glm_prompt = _build_transcription_prompt(page, page, include_figure_rules=False)
    try:
        glm_md = ox.extract_pdf_pages(
            glm_prompt, pdf_path, page, page, model=FMDW_BODY_PRIMARY_MODEL,
        )
    except Exception as e:  # noqa: BLE001 — 1차 실패는 폴백으로 안전 degrade
        print(f"    [~] hybrid p{page}: primary({FMDW_BODY_PRIMARY_MODEL}) 오류(무시, "
              f"fallback 시도): {e}", flush=True)
        glm_md = None

    # Fix 1(2026-07-04): glm-ocr intra-page 블록 중복 제거 — 잘림 여부와 무관하게 상시
    # 검사한다. 큰 연속 블록이 통째로 2회 반복되면 뒤 사본을 제거(첫 출현 유지)하고,
    # 반복이 없으면 byte-identical 이라 회귀 0. (_has_degenerate_repetition 는 라인 루프
    # 계열을 담당 — 여기선 결정적 블록-중복만 제거.)
    # Fix 2(2026-07-04): 아래 glm_len / best-of(qwen_len>glm_len) 비교가 dedup 후 길이를
    # 쓰도록, 길이 산정 전에 dedup 을 적용한다.
    if glm_md:
        _glm_before = len(glm_md)
        glm_md, _deduped = _dedup_page_repetition(glm_md)
        if _deduped:
            print(f"    p{page}: glm intra-page 중복 제거 "
                  f"({_glm_before}→{len(glm_md)} chars)", flush=True)
        elif ox._has_degenerate_repetition(glm_md):
            print(f"    p{page}: glm 반복 감지(블록-중복 아님) — 잘림복구/폴백에 위임",
                  flush=True)

    # 페이지 걸침 단일 정의 행 승격(2COL straddle, 2026-07-10): glm 이 두 문단
    # (구문/설명)으로 렌더한 꼬리를, 다음 페이지가 2COL 표로 이어질 때만 결정론적으로
    # 1행 Syntax/Description 표로 재구성한다. 게이트 미충족 시 no-op(회귀 0).
    if glm_md and _twocol_enabled():
        glm_md = _twocol_reshape_straddle_tail(pdf_path, page, glm_md)

    glm_len = len(glm_md.strip()) if glm_md else 0
    needs_fallback = glm_md is None or (
        pdf_text_len >= FMDW_HYBRID_MIN_TEXT
        and glm_len < FMDW_HYBRID_COVERAGE_MIN * pdf_text_len
    )

    if not needs_fallback:
        print(f"    p{page}: glm={glm_len} pdf={pdf_text_len} → kept-glm", flush=True)
        return _apply_hybrid_completeness(
            pdf_path, page,
            _clean_runaway(_apply_grid_tables(pdf_path, page, glm_md))), "glm"

    try:
        qwen_md = ox.extract_pdf_pages(
            prompt, pdf_path, page, page, model=FMDW_BODY_FALLBACK_MODEL,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    [!] hybrid p{page}: fallback({FMDW_BODY_FALLBACK_MODEL}) 오류: {e}",
              flush=True)
        qwen_md = None

    if qwen_md and _twocol_enabled():
        qwen_md = _twocol_reshape_straddle_tail(pdf_path, page, qwen_md)
    qwen_len = len(qwen_md.strip()) if qwen_md else 0

    # best-of: qwen 이 실제로 glm 보다 "더 길 때만" 채택(같거나 짧으면 glm 유지).
    if qwen_md and qwen_len > glm_len:
        print(f"    p{page}: glm={glm_len} pdf={pdf_text_len} → "
              f"qwen-repair({qwen_len})", flush=True)
        return _apply_hybrid_completeness(
            pdf_path, page,
            _clean_runaway(_apply_grid_tables(pdf_path, page, qwen_md))), "qwen"

    if glm_md:
        if qwen_md:
            # qwen 이 응답은 했으나 glm 보다 짧거나 같음 — 완성도 저하 방지를 위해
            # glm 유지(비결정적 qwen 이 조밀한 페이지에서 짧게 나온 경우).
            print(f"    p{page}: glm={glm_len} pdf={pdf_text_len} → "
                  f"kept-glm(qwen shorter: {qwen_len}<{glm_len})", flush=True)
            return _apply_hybrid_completeness(
                pdf_path, page,
                _clean_runaway(_apply_grid_tables(pdf_path, page, glm_md))), "glm(qwen-shorter)"
        print(f"    p{page}: glm={glm_len} pdf={pdf_text_len} → "
              f"qwen-repair-failed, kept-glm({glm_len})", flush=True)
        return _apply_hybrid_completeness(
            pdf_path, page,
            _clean_runaway(_apply_grid_tables(pdf_path, page, glm_md))), "glm(fallback-failed)"

    print(f"    [!] hybrid p{page}: primary+fallback 모두 실패", flush=True)
    return None, "failed"


def _hybrid_extract_range(pdf_path, start_page: int, end_page: int):
    """청크 범위를 페이지별로 하이브리드 전사(_hybrid_transcribe_page) 후 결합.

    단일 페이지 청크(start_page == end_page, 예: EXTRACT_CHUNK_SIZE=1)에서는
    루프 1회로 그 페이지 결과를 그대로 반환한다. 멀티페이지 청크(예: 기본
    EXTRACT_CHUNK_SIZE=5)에서는 각 페이지를 개별 전사한 뒤 기존 M-8 페이지 폴백
    결합 계약과 동일한 구분자("\n\n---\n\n")로 이어붙인다.

    2026-07-04 수정(Advisor QA Warning): 청크 전체(start..end) 프롬프트를 페이지별
    호출에 그대로 재사용하지 않는다 — 렌더되는 이미지는 항상 페이지 1장인데 프롬프트
    문구가 "pages {start} to {end}"(예: "1 to 5")로 남아있으면 모델이 자신이 지금
    전사 중인 절대 페이지 번호를 알 수 없어 `<!-- page N -->` 마커가 어긋날 수 있었다
    (내용 유실은 아니고 감사 마커 정확도 문제). 각 페이지마다
    `_build_transcription_prompt(page, page)` 로 그 페이지 자신의 번호를 명시한
    프롬프트를 새로 만들어 전달한다(M-8 단일 페이지 폴백 `extract_chunk(pdf, page,
    page, ...)` 과 동일한 start==end 계약).

    Returns:
        결합 MD(부분/완전 성공) 또는 None(범위 내 모든 페이지 실패).
    """
    parts: list[str] = []
    any_ok = False
    for page in range(start_page, end_page + 1):
        page_prompt = _build_transcription_prompt(page, page)
        md_text, _model_used = _hybrid_transcribe_page(pdf_path, page, page_prompt)
        if md_text is not None:
            # Fix 3(2026-07-04): 페이지 마커를 코드로 결정적 부착(모델 지시 의존 제거).
            # 모델이 이미 선두에 마커를 냈으면 중복 방지 후 올바른 번호로 재부착.
            parts.append(_prepend_page_marker(md_text, page))
            any_ok = True
        else:
            parts.append(
                f"<!-- page {page} -->\n\n"
                f"<!-- MISSING page {page}: extraction failed (hybrid) -->"
            )
    if not any_ok:
        return None
    return "\n\n---\n\n".join(parts)


# ── figure 이미지 크롭(opt-in, 기본 OFF) ─────────────────────────────────────
# 기본 0/미설정 = 기존 동작 byte-identical 보존(figure 경로 미진입). EXTRACT_FIGURES=1
# 일 때만 lib.figure_extractor.extract_figures 를 호출해 `Figure N` 다이어그램을 크롭하고
# output/<type>_md/figures/ + output/<type>_md/<stem>_figures.json 사이드카를 만든다.
# (설계 §5.1 — codesign-rag ingest 가 흡수할 다운스트림 사이드카.)
# figure 크롭은 MD 본문 생성과 **완전 독립**이다: 실패해도 MD 변환 결과에 영향 없음.
# lazy import — OFF 경로에서는 figure_extractor 를 import 하지 않아 의존성/비용 0.


def _figures_enabled() -> bool:
    """EXTRACT_FIGURES 환경변수가 '1'(또는 truthy)인지 — figure 크롭 opt-in 게이트.

    매 호출 시 env 를 평가한다(테스트 monkeypatch.setenv 가 즉시 반영되도록).
    """
    val = os.getenv("EXTRACT_FIGURES", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def maybe_extract_figures(pdf_path, output_dir):
    """opt-in(EXTRACT_FIGURES=1) 시에만 figure 크롭 + 사이드카 생성. 아니면 no-op.

    Args:
        pdf_path   : 원본(또는 변환된) PDF 경로.
        output_dir : MD 출력 디렉터리(예: output/pdf_md). 여기 아래 figures/ 와
                     <stem>_figures.json 이 만들어진다.

    Returns:
        figure 항목 리스트(생성 시) 또는 None(OFF/실패 — MD 변환에 영향 없음).
    """
    if not _figures_enabled():
        return None
    try:
        from fmdw import figure_extractor as _fx  # lazy import (OFF 경로 비용 0)

        figs = _fx.extract_figures(Path(pdf_path), Path(output_dir))
        print(f"[+] figure 크롭: {len(figs)}개 → {output_dir}/figures/ "
              f"(+ {Path(pdf_path).stem}_figures.json)", flush=True)
        return figs
    except Exception as e:  # noqa: BLE001 — figure 실패가 MD 변환을 깨지 않게 안전 degrade
        print(f"[~] figure 크롭 실패(무시, MD 변환 영향 없음): {e}", flush=True)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 오버사이즈 행렬표 페이지 사전 스캔 + 본문 OCR 스킵 (Fix 2, 2026-07-04)
# ──────────────────────────────────────────────────────────────────────────────
# EXTRACT_OVERSIZED_MATRIX_TABLES=1 인 표(예: Design Manual p11~13, 표 전체가
# raster 이미지)는 이미 figure_extractor 가 별도로 크롭 + AI describe 하여
# "## 추출 이미지" 섹션에 넣어준다. 이런 페이지를 본문 OCR(vision LLM 전사)에도
# 또 보내면 (1) 중복 작업, (2) 페이지당 10~20분씩 걸려 truncation 유발, (3) 어차피
# 텍스트 전사 품질이 낮아 무의미하다. 따라서 본문 추출 전에 같은 판정 로직으로
# 오버사이즈 페이지를 미리 찾아내 OCR 대상에서 제외하고 자리표시자만 남긴다.

def _scan_oversized_pages(pdf_path) -> set:
    """오버사이즈 행렬표(raster) 페이지 번호(1-indexed) 집합을 사전 스캔한다.

    EXTRACT_OVERSIZED_MATRIX_TABLES 게이트가 꺼져 있으면(기본) 즉시 빈 집합을
    반환해 기존 동작을 완전히 보존한다(회귀 0). 켜져 있을 때만 fitz 로 PDF 를 열어
    figure_extractor 의 판정 로직(밀도/점유율 + 워터마크 xref 제외)을 그대로
    재사용한다 — extract_figures() 내부의 om_enabled/om_watermark_xrefs 계산과
    동일한 호출 방식(같은 함수·같은 인자)이라 크롭 단계와 판정이 어긋나지 않는다.
    """
    try:
        from fmdw import figure_extractor as _fx  # lazy import (OFF 경로 비용 0)

        if not _fx._is_oversized_matrix_enabled():
            return set()
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        try:
            watermark_xrefs = _fx._watermark_xrefs(doc)
            pages = set()
            for pidx in range(doc.page_count):
                page = doc[pidx]
                try:
                    boxes = _fx.detect_oversized_matrix_tables(
                        page, exclude_xrefs=watermark_xrefs)
                except Exception:  # noqa: BLE001 — 판정 실패는 해당 페이지 정상 OCR로 degrade
                    boxes = []
                if boxes:
                    pages.add(pidx + 1)
            return pages
        finally:
            doc.close()
    except Exception as e:  # noqa: BLE001 — 스캔 실패가 본문 OCR 을 막지 않게 안전 degrade
        print(f"[~] 오버사이즈 표 사전 스캔 실패(무시, 본문 OCR 정상 진행): {e}", flush=True)
        return set()


def _oversized_placeholder(page: int) -> str:
    """오버사이즈 행렬표 페이지의 본문 자리표시자(OCR 대신 삽입).

    CHANGE 4(2026-07-08): 본문에 안내문(📊 ...)을 넣지 않는다 — 실제 표 내용은 별도
    크롭 + AI describe 로 문서 끝 추출 이미지 블록에만 들어간다. 본문에는 페이지 마커만
    남겨 페이지 순서/번호 감사(audit)가 깨지지 않게 한다.
    """
    return f"<!-- page {page} -->\n"


# ──────────────────────────────────────────────────────────────────────────────
# 오버사이즈 행렬표 페이지의 '표 밖' 본문 보존 (2026-07-10)
# ──────────────────────────────────────────────────────────────────────────────
# 문제: 오버사이즈 행렬표 페이지는 body OCR 을 통째로 스킵(_oversized_placeholder)해
# 왔다. 그 결과 거대표는 크롭 이미지로 잘 빠지지만, 같은 페이지의 '표 밖' 본문(Note
# 박스·절 텍스트·intro 문장·0/1/x 정의표)이 전부 소실됐다(실측 LN08LPU p94).
# 해결: 거대표(raster/vector 무관)는 detect 박스로 제외하고, 표 밖에 남은 PDF 벡터
# 텍스트만 전사한다. 100% PDF 벡터 텍스트라 환각 0, vision LLM 미호출. 거대표 셀 값은
# 박스 안이라 제외되므로 본문에 GFM 로 쏟아지지 않는다(제약 1 준수). 표 밖 텍스트가
# 전혀 없는 페이지(표만 있는 페이지)는 순수 자리표시자를 그대로 돌려줘 회귀 0.

def _line_in_boxes(line, boxes, frac: float = 0.5) -> bool:
    """라인 bbox 가 표 박스와 면적기준 frac 이상 겹치면 True(표 내부 → 표 밖 본문에서 제외)."""
    x0, y0, x1, y1 = line[0], line[1], line[2], line[3]
    la = max(x1 - x0, 1e-6) * max(y1 - y0, 1e-6)
    for b in boxes:
        bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
        ix = max(0.0, min(x1, bx1) - max(x0, bx0))
        iy = max(0.0, min(y1, by1) - max(y0, by0))
        if ix * iy >= frac * la:
            return True
    return False


def _oversized_generic_render(lines) -> str:
    """_twocol_analyze 가 None 인 소규모 '표 밖' 영역용 경량 렌더(페이지 마커 제외).

    정의행 = 좁은 좌측 토큰(우측 설명 열에 못 미침) + 동일 y 대역의 우측 설명 라인.
    우측열 검출 빈도 임계를 2 로 낮춰(_twocol_analyze 는 4) 3행짜리 진리표 정의(0/1/x)도
    Syntax/Description 표로 복원한다. 페어 ≥2 면 표, 그 외는 전부 산문. 전부 PDF 벡터
    텍스트라 환각 0. 라인 읽기순을 보존한다(좌토큰 위치에 행을 배치, 우측 파트너는 소비).
    """
    if not lines:
        return ""

    def _norm(t):
        return re.sub(r"\s+", " ", (t or "").strip())

    left_m = min(l[0] for l in lines)
    right_e = max(l[2] for l in lines)
    W = max(right_e - left_m, 1.0)
    hs = sorted((l[3] - l[1]) for l in lines)
    h_med = hs[len(hs) // 2] or 10.0

    from collections import Counter
    cl = Counter(round(l[0] / 3.0) for l in lines if l[0] > left_m + 0.30 * W)
    rx_cands = sorted(k * 3.0 for k, nn in cl.items()
                      if nn >= 2 and (k * 3.0 - left_m) >= 0.30 * W)
    r_x = rx_cands[0] if rx_cands else None

    n = len(lines)
    used = [False] * n
    seq = []  # ("para", text) | ("row", (left, right))
    for i in range(n):
        if used[i]:
            continue
        x0, y0, x1, y1, t = lines[i]
        if r_x is not None and x0 <= left_m + 0.15 * W and x1 < r_x - 6.0:
            partner = None
            for j in range(n):
                if used[j] or j == i:
                    continue
                xj0, yj0 = lines[j][0], lines[j][1]
                if xj0 >= r_x - 4.5 and abs(yj0 - y0) <= 0.8 * h_med:
                    partner = j
                    break
            if partner is not None:
                used[i] = True
                used[partner] = True
                seq.append(("row", (_norm(t), _norm(lines[partner][4]))))
                continue
        used[i] = True
        seq.append(("para", _norm(t)))

    parts = []
    rows = []

    def _flush():
        nonlocal rows
        if not rows:
            return
        if len(rows) >= 2:
            tbl = ["| Syntax | Description |", "| :--- | :--- |"]
            tbl += [f"| {_twocol_cell(s)} | {_twocol_cell(d)} |" for s, d in rows]
            parts.append("\n".join(tbl))
        else:
            for s, d in rows:
                parts.append(_norm(f"{s} {d}"))
        rows = []

    for kind, payload in seq:
        if kind == "row":
            rows.append(payload)
        else:
            _flush()
            if payload:
                parts.append(payload)
    _flush()
    return "\n\n".join(p for p in parts if p.strip())


def _render_outside_lines_md(lines) -> str:
    """표 밖 라인 → 산문 문단 + 정의행(Syntax/Description 표) MD(페이지 마커 제외).

    우선 기존 _twocol_analyze(≥4행 정의표 등)로 시도하고, None 이면 경량
    _oversized_generic_render 로 소규모 정의행/산문을 복원한다. 둘 다 PDF 벡터 텍스트만.
    """
    if not lines:
        return ""
    segs = _twocol_analyze(lines)
    if segs:
        parts = []
        for kind, payload in segs:
            if kind == "para":
                parts.append(payload)
            else:
                tbl = ["| Syntax | Description |", "| :--- | :--- |"]
                tbl += [f"| {s} | {d} |" for s, d in payload]
                parts.append("\n".join(tbl))
        return "\n\n".join(p for p in parts if p.strip())
    return _oversized_generic_render(lines)


def _oversized_body_md(pdf_path, page: int) -> str:
    """오버사이즈 행렬표 페이지의 '표 밖' 본문만 PDF 벡터 텍스트로 전사한다(환각 0).

    거대 매트릭스 표(raster/vector 무관)는 detect 박스로 제외하므로 표 셀 값이 본문에
    유출되지 않는다(제약 1). 표 밖에 남은 산문(Note·소절 텍스트·intro)과 2열 정의행
    (0/1/x → Syntax/Description)만 복원한다. 표 밖 텍스트가 전혀 없으면(표만 있는
    페이지) 기존과 동일한 순수 자리표시자를 돌려줘 회귀 0(추가 텍스트 없음).
    """
    marker = _oversized_placeholder(page)  # "<!-- page N -->\n"
    try:
        from fmdw import figure_extractor as _fx
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        try:
            pg = doc[page - 1]
            try:
                wx = _fx._watermark_xrefs(doc)
                boxes = _fx.detect_oversized_matrix_tables(pg, exclude_xrefs=wx) or []
            except Exception:  # noqa: BLE001 — 박스 재검출 실패는 전체 라인을 표 밖으로 간주
                boxes = []
            lines = _twocol_page_lines(pg)  # F1/F2 정제(러닝헤더·워터마크 제거) + 읽기순
        finally:
            doc.close()
    except Exception as e:  # noqa: BLE001 — 실패는 자리표시자로 안전 degrade(본문 손실만, 크래시 없음)
        print(f"    [~] oversized p{page}: 표 밖 본문 추출 실패(무시, 자리표시자): {e}", flush=True)
        return marker

    outside = [l for l in lines if not _line_in_boxes(l, boxes)]
    if not outside:
        return marker  # 표만 있는 페이지 — 기존과 동일(추가 텍스트 0)
    body = _render_outside_lines_md(outside)
    if not body.strip():
        return marker
    return marker.rstrip("\n") + "\n\n" + body + "\n"


def _build_chunk_plan(total_pages: int, chunk_size: int, oversized_pages: set) -> list:
    """페이지 1..total_pages 를 순서대로 훑어 OCR 청크 계획을 만든다.

    오버사이즈 페이지는 ("skip", page) 항목으로 단독 배치하고, 그 사이의 연속된
    비-오버사이즈 페이지는 chunk_size 이하로 묶어 ("ocr", start, end) 항목으로
    배치한다. 결과 리스트를 순서대로 처리하면 최종 결합 MD 의 페이지 오름차순이
    보존된다(오버사이즈 게이트 OFF 시 oversized_pages 가 빈 집합이므로 기존
    range(1, total_pages+1, chunk_size) 청크 분할과 완전히 동일 — 회귀 0).
    """
    plan = []
    page = 1
    while page <= total_pages:
        if page in oversized_pages:
            plan.append(("skip", page))
            page += 1
            continue
        start = page
        end = start
        while (end + 1 <= total_pages
               and (end + 1 - start + 1) <= chunk_size
               and (end + 1) not in oversized_pages):
            end += 1
        plan.append(("ocr", start, end))
        page = end + 1
    return plan


#: MD 본문에 주입한 추출-이미지 섹션의 시작 마커(재처리 시 중복 주입 방지용).
_FIGURE_REF_MARKER = "<!-- fmdw:extracted-figures -->"


def _md_image_alt(text: str) -> str:
    """Markdown 이미지 alt 텍스트로 안전하게 정리(대괄호/줄바꿈 → 깨짐 방지)."""
    return (
        (text or "")
        .replace("[", "(")
        .replace("]", ")")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def _inline_figures_enabled() -> bool:
    """FMDW_INLINE_FIGURES(기본 ON): 크롭 이미지를 본문 캡션 바로 아래에 인라인 배치(F6).
    0 이면 기존 '본문 끝 일괄 섹션' 배치로 복귀."""
    return os.getenv("FMDW_INLINE_FIGURES", "1").strip().lower() not in ("0", "false", "no")


#: 본문 캡션 라인(정규화 후 '**Figure N: ...**' / 'Table N: ...') 매칭 — 번호 캡처.
_BODY_CAP_LINE_RE = re.compile(r"^\s*\*{0,2}\s*(?:figure|table)\s+(\d+)\s*:", re.IGNORECASE)
_INJECT_PAGE_MARKER_RE = re.compile(r"^<!-- page (\d+) -->\s*$")


def _fig_caption_number(text: str):
    """캡션/텍스트에서 Figure/Table 번호 추출('Figure 12: ...'→'12', 대소문자 무시). 없으면 None."""
    m = re.search(r"\b(?:figure|fig\.?|table)\s+(\d+)\b", text or "", re.IGNORECASE)
    return m.group(1) if m else None


#: figure_id 말미 'p{PP}_fig{K}' 에서 페이지/순번(K, y-오름차순) 추출.
_FIGID_PAGE_FIG_RE = re.compile(r"_p(\d+)_fig(\d+)$")


def _reorder_figure_captions_by_id(lines: list, valid: list) -> None:
    """같은 페이지에 여러 figure 캡션이 있을 때, 본문(glm 캡셔닝) 순서가 figure_id
    의 원본 y-순서(fig1<fig2<...)와 어긋나면 캡션 라인 '텍스트'만 맞바꿔 정렬한다.

    이미지/설명은 캡션 라인 바로 아래에 별도로 삽입되므로(inject_figure_refs_into_md),
    캡션 라인의 순서만 바로잡으면 이미지도 자동으로 올바른 순서로 배치된다(라인 위치·
    개수·페이지 마커·본문 산문은 전혀 건드리지 않음). in-place로 `lines` 를 수정한다.
    회귀 0 조건: 페이지당 캡션 매칭 1개 이하이거나 이미 순서가 맞으면 무변화.
    """
    # 1) figure_id → (page, fig순번=y-오름차순) 매핑, 캡션 번호 기준.
    order_by_num = {}
    for f in valid:
        num = _fig_caption_number(f.get("caption") or "")
        if num is None or num in order_by_num:
            continue
        m = _FIGID_PAGE_FIG_RE.search(f.get("figure_id") or "")
        if not m:
            continue
        try:
            pg = int(f.get("page", 0))
        except (TypeError, ValueError):
            continue
        order_by_num[num] = (pg, int(m.group(2)))

    if not order_by_num:
        return

    # 2) 본문에서 캡션 라인 위치 스캔(번호별 첫 매칭만 — 하단 cap_idx_by_num 규칙과 동일).
    cap_idx = {}
    for i, l in enumerate(lines):
        m = _BODY_CAP_LINE_RE.match(l)
        if m and m.group(1) in order_by_num and m.group(1) not in cap_idx:
            cap_idx[m.group(1)] = i

    # 3) 페이지별로 묶어 기대 순서(y) vs 실제 문서 순서 비교, 다르면 캡션 텍스트만 스왑.
    by_page = {}
    for num, (pg, fidx) in order_by_num.items():
        if num in cap_idx:
            by_page.setdefault(pg, []).append((num, fidx, cap_idx[num]))

    for items in by_page.values():
        if len(items) < 2:
            continue  # figure 1개 페이지 — 무변화
        expected = sorted(items, key=lambda t: t[1])   # figure_id fig{K} 오름차순(원본 y-순서)
        actual = sorted(items, key=lambda t: t[2])      # 문서 내 라인 위치 오름차순
        if [n for n, _, _ in expected] == [n for n, _, _ in actual]:
            continue  # 이미 순서 일치 — 무변화
        positions = [ln for _, _, ln in actual]
        texts_by_num = {n: lines[ln] for n, _, ln in items}
        for pos, (num, _, _) in zip(positions, expected):
            lines[pos] = texts_by_num[num]


def _figure_block_lines(f, *, with_heading: bool) -> list:
    """한 figure 의 주입 블록(이미지 + 설명). with_heading=True 면 캡션 헤딩(### ...)도 포함(orphan/bucket용).

    인라인(F6)에서는 본문에 이미 '**Figure N: ...**' 캡션이 있으므로 heading 없이 이미지+설명만.
    """
    ip = (f.get("image_path") or "").strip()
    typ = (f.get("type") or "figure").strip()
    cap = (f.get("caption") or "").strip()
    figno = (f.get("figure_no") or "").strip()
    desc = (f.get("description") or "").strip()
    if typ == "complex_table":
        alt = _md_image_alt(cap or "복잡 표(이미지)")
    else:
        alt = _md_image_alt(cap or figno or typ)
    blk = []
    if with_heading and cap:
        blk.append(f"### {cap}")
        blk.append("")
    blk.append(f"![{alt}]({ip})")
    blk.append("")
    if desc:  # FIX C: 빈 설명은 문단 생략(빈 문단 금지)
        blk.append(desc)
        blk.append("")
    return blk


def _reposition_single_figure_captions_by_y(lines: list, valid: list, pdf_path) -> None:
    """단일-figure 페이지에서 본문 Figure 캡션 라인을 '원본 figure y' 위치로 끌어올린다.

    결함(2026-07-11): glm 이 Figure 캡션(본문 텍스트 '**Figure N: ...**')을 원본 위치보다
    아래(후속 소절·리스트 뒤)로 배치하면, 캡션 바로 뒤에 이미지를 인라인하는 규칙상 이미지
    까지 통째로 밀려난다(p149-151 p1: Figure 77 이 4.2.9/DRC 뒤로). 같은 페이지에 figure 가
    2개 이상일 때만 도는 _reorder_figure_captions_by_id 는 이 케이스(페이지당 figure 1개,
    캡션이 본문 텍스트)를 못 고친다 → 상보적으로 '단일-figure 페이지'만 여기서 y-재배치.

    알고리즘(결정론): 각 figure 원본 y = 사이드카 bbox top(y0). 해당 페이지 섹션
    (`<!-- page N -->` 경계) 본문 라인들의 추정 y(_estimate_body_line_y — covered PDF 라인 ↔
    body 토큰겹침≥0.6, 완전성 가드와 동일 계약)를 문서순 스캔해 'figure y 보다 아래(y 큰) 첫
    본문 라인' 직전으로 캡션 라인을 옮긴다. 이미지는 이후 inject 단계가 캡션 바로 뒤에 넣으
    므로 캡션↔이미지 쌍이 함께 이동한다.

    회귀 0 원칙:
      (a) 위로 이동만(anchor 가 캡션 현위치보다 앞일 때만) → 멱등(이미 올바르면 무변화).
      (b) y 추정 실패로 anchor 를 못 찾으면 무변화(안전 폴백).
      (c) figure 가 페이지 최하단(모든 본문보다 아래)이면 anchor 없음 → 무변화.
      (d) 다중-figure 페이지는 제외(_reorder_figure_captions_by_id 관할 — 간섭 방지).
      (e) 표/코드펜스 내부 앵커는 _anchor_outside_table_fence 로 블록 앞으로 당김.
      (f) pdf_path 없음/열기 실패/FMDW_FIG_CAPTION_REPOSITION=0 → 무변화.
    """
    if pdf_path is None or not valid:
        return
    if os.getenv("FMDW_FIG_CAPTION_REPOSITION", "1").strip().lower() in ("0", "false", "no"):
        return
    # figure(비 complex_table) + page + bbox top 확보, 페이지별 그룹.
    figs_by_page = {}
    for f in valid:
        if (f.get("type") or "figure").strip() != "figure":
            continue
        num = _fig_caption_number(f.get("caption") or "")
        bbox = f.get("bbox")
        if num is None or not isinstance(bbox, (list, tuple)) or len(bbox) < 2:
            continue
        try:
            pg = int(f.get("page", 0))
            fig_y = float(bbox[1])
        except (TypeError, ValueError):
            continue
        if pg <= 0:
            continue
        figs_by_page.setdefault(pg, []).append((num, fig_y))
    # 단일-figure 페이지만(회귀 최소 — 다중 figure 페이지는 _reorder 담당).
    single = {pg: v[0] for pg, v in figs_by_page.items() if len(v) == 1}
    if not single:
        return
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001 — 재배치 실패는 비차단(본문 그대로)
        return
    try:
        for pg in sorted(single):
            num, fig_y = single[pg]
            # 페이지 마커 경계(이전 페이지 이동으로 인덱스가 바뀌므로 매 페이지 재스캔).
            marker_idx = {}
            for i, l in enumerate(lines):
                pm = _INJECT_PAGE_MARKER_RE.match(l)
                if pm:
                    marker_idx.setdefault(int(pm.group(1)), i)
            if pg not in marker_idx:
                continue
            sec_start = marker_idx[pg] + 1
            sec_end = len(lines)
            for oidx in marker_idx.values():
                if marker_idx[pg] < oidx < sec_end:
                    sec_end = oidx
            # 섹션 내 캡션 라인(첫 매칭).
            cap_abs = None
            for i in range(sec_start, sec_end):
                m = _BODY_CAP_LINE_RE.match(lines[i])
                if m and m.group(1) == num:
                    cap_abs = i
                    break
            if cap_abs is None:
                continue
            if not (0 <= pg - 1 < doc.page_count):
                continue
            clean_lines = _twocol_page_lines(doc[pg - 1])
            section_lines = lines[sec_start:sec_end]
            _, _, covered, _ = _recover_absent_blocks(clean_lines, "\n".join(section_lines))
            body_y = _estimate_body_line_y(clean_lines, covered, section_lines)
            cap_rel = cap_abs - sec_start
            anchor_rel = None
            for r in range(len(section_lines)):
                if r == cap_rel:
                    continue
                by = body_y[r]
                if by is not None and by > fig_y:
                    anchor_rel = r
                    break
            if anchor_rel is None:
                continue  # 후미 figure → 무변화(c)
            if anchor_rel >= cap_rel:
                continue  # 이미 올바른 위치(위 이동만) → 무변화(a)
            anchor_rel = _anchor_outside_table_fence(section_lines, anchor_rel)  # (e)
            if anchor_rel >= cap_rel:
                continue
            anchor_abs = sec_start + anchor_rel
            # 캡션 라인(단일 라인) 이동: pop(캡션) → anchor 앞 삽입.
            # cap_abs > anchor_abs 이므로 pop 은 anchor_abs 를 이동시키지 않는다(위 이동만).
            cap_text = lines.pop(cap_abs)
            # 캡션 자리에 남은 이중 빈줄 하나 정리(cosmetic, 가드).
            if 0 < cap_abs < len(lines) and lines[cap_abs].strip() == "" \
               and lines[cap_abs - 1].strip() == "":
                lines.pop(cap_abs)
            block = [cap_text, ""]
            if anchor_abs > 0 and lines[anchor_abs - 1].strip() != "":
                block = [""] + block  # 앞 본문과 빈 줄 분리
            lines[anchor_abs:anchor_abs] = block
            print(f"    [FIGPOS] p{pg}: Figure {num} 캡션 → 원본 y({fig_y:.0f}) 위치로 상향 재배치",
                  flush=True)
    finally:
        doc.close()


def _orphan_reposition_enabled() -> bool:
    """FMDW_ORPHAN_REPOSITION(기본 ON): 본문 캡션이 누락돼 orphan 버킷으로 갈 figure 를
    원본 페이지의 y 위치 본문에 캡션+이미지로 재삽입한다. 0/false/no 이면 기존 orphan
    버킷 동작(문서 끝 `<!-- fmdw:extracted-figures -->` 하위)."""
    return os.getenv("FMDW_ORPHAN_REPOSITION", "1").strip().lower() not in ("0", "false", "no")


def _reinsert_orphan_figure_captions_by_y(lines: list, valid: list, pdf_path) -> int:
    """본문 캡션이 누락돼 orphan 버킷으로 갈 figure 를, 원본 페이지의 y 위치 본문에
    '**Figure N: caption**' 캡션 라인으로 재삽입한다(이미지는 이후 inject 단계가 캡션 바로
    뒤에 인라인 → 캡션↔이미지 쌍이 함께 배치). 삽입에 성공하면 본문에 캡션이 생겨 정상
    인라인 경로를 타고 orphan 버킷에서 빠진다. 반환: 재삽입한 figure 수. in-place 로 `lines`.

    결함(2026-07-11): glm 이 Figure 캡션을 본문 전사에서 누락하면(예: LN08LPU_p118-121 의
    Figure 33/35) 매칭할 캡션이 없어 이미지가 문서 끝 orphan 버킷으로 밀린다(회귀 — 이전
    배포본은 Figure 31~35 전부 inline 이었다). figure_id(`p{PP}_fig{K}`)로 원본 페이지 PP 와
    페이지 내 y-순번 K 를, 사이드카 caption/bbox 로 캡션·원본 y 를 복원해 본문에 되돌린다.

    앵커(결정론, 우선순위):
      (1) 같은 페이지의 '이미 본문에 캡션이 있는' figure(inline) 의 figure_id fig{K} 순서로
          하/상한을 잡는다: orphan 의 K 바로 앞 K(predecessor) 캡션 라인 '뒤'가 하한,
          바로 뒤 K(successor) 캡션 라인 '앞'이 상한.
      (2) 그 [하한,상한) 안에서 _estimate_body_line_y(covered PDF 라인 ↔ body 토큰겹침≥0.6,
          완전성 가드와 동일 계약)로 본문 라인 y 를 추정, 'figure y(사이드카 bbox top)보다
          아래(y 큰) 첫 본문 라인' 앞에 삽입. y 미세앵커 실패 시 predecessor 캡션 바로 뒤
          (없으면 successor 캡션 바로 앞)로 폴백.

    회귀 0 원칙:
      (a) 확신 불가(페이지 마커 부재·같은 페이지 inline 앵커 없음·앵커 모순·y 추정 실패)면
          무삽입 → 기존 orphan 버킷 유지(손실 0).
      (b) 이미 본문에 캡션이 있는 figure 는 대상 아님(orphan 아님) → 중복 삽입 금지.
      (c) type!=figure / 캡션번호·figure_id·page 결측이면 대상 아님(complex_table 등 불변).
      (d) FMDW_ORPHAN_REPOSITION=0 → 전체 무동작. orphan 이 없는 파일은 완전 무변화(byte).
      (e) 캡션 라인만 삽입(이미지는 inject 가 바로 뒤에) → 캡션↔이미지 쌍이 붙는다.
      (f) 표/코드펜스 내부 앵커는 _anchor_outside_table_fence 로 블록 앞으로 당김(구조 보호).
    """
    if not _orphan_reposition_enabled() or not valid:
        return 0

    # 1) figure 메타 파싱(type=figure + 캡션번호 + figure_id p{PP}_fig{K} + page[, bbox top]).
    def _meta(f):
        if (f.get("type") or "figure").strip() != "figure":
            return None
        cap = (f.get("caption") or "").strip()
        num = _fig_caption_number(cap)
        m = _FIGID_PAGE_FIG_RE.search(f.get("figure_id") or "")
        if not cap or num is None or not m:
            return None
        try:
            pg = int(f.get("page", 0))
            K = int(m.group(2))
        except (TypeError, ValueError):
            return None
        if pg <= 0:
            return None
        fig_y = None
        bbox = f.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
            try:
                fig_y = float(bbox[1])
            except (TypeError, ValueError):
                fig_y = None
        return {"num": num, "cap": cap, "pg": pg, "K": K, "fig_y": fig_y}

    metas = []
    for f in valid:
        mt = _meta(f)
        if mt is not None:
            metas.append(mt)
    if not metas:
        return 0

    # 2) 현재 본문 캡션 번호(초기) → orphan(본문에 캡션 없는 figure) 판정.
    present0 = set()
    for l in lines:
        m = _BODY_CAP_LINE_RE.match(l)
        if m:
            present0.add(m.group(1))
    orphans = [mt for mt in metas if mt["num"] not in present0]
    if not orphans:
        return 0   # 회귀 0: orphan 없으면 완전 무변화

    # 페이지·K 오름차순(predecessor 를 먼저 삽입 → 뒤 orphan 이 그것을 앵커로 재사용).
    orphans.sort(key=lambda mt: (mt["pg"], mt["K"]))

    # PDF(선택) — body-y 미세앵커용. 없거나 실패면 inline-figure 앵커만 사용.
    doc = None
    if pdf_path is not None:
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))
        except Exception:  # noqa: BLE001 — PDF 실패는 비차단(inline 앵커만)
            doc = None

    reinserted = 0
    try:
        for orph in orphans:
            pg, K_o, num, cap, fig_y = (orph["pg"], orph["K"], orph["num"],
                                        orph["cap"], orph["fig_y"])
            # (재)스캔: 삽입으로 인덱스가 바뀌므로 매 orphan 마다 페이지마커/캡션위치 갱신.
            marker_idx = {}
            cap_pos = {}
            for i, l in enumerate(lines):
                pm = _INJECT_PAGE_MARKER_RE.match(l)
                if pm:
                    marker_idx.setdefault(int(pm.group(1)), i)
                m = _BODY_CAP_LINE_RE.match(l)
                if m:
                    cap_pos.setdefault(m.group(1), i)

            if num in cap_pos:
                continue  # 이미 본문에 존재 → 중복 삽입 금지(b)
            if pg not in marker_idx:
                continue  # 페이지 마커 부재 → 확신 불가 → orphan 유지(a)

            sec_start = marker_idx[pg] + 1
            sec_end = len(lines)
            for oi in marker_idx.values():
                if marker_idx[pg] < oi < sec_end:
                    sec_end = oi

            # 같은 페이지 inline figure(현재 본문에 캡션 존재, 이 섹션 내) 앵커: (K, cap_idx).
            pred_idx = None   # K < K_o 중 최대 K 의 캡션 라인
            succ_idx = None   # K > K_o 중 최소 K 의 캡션 라인
            pred_K = None
            succ_K = None
            for mt in metas:
                if mt["pg"] != pg or mt["num"] == num:
                    continue
                ci = cap_pos.get(mt["num"])
                if ci is None or not (sec_start <= ci < sec_end):
                    continue
                K_a = mt["K"]
                if K_a < K_o and (pred_K is None or K_a > pred_K):
                    pred_K, pred_idx = K_a, ci
                elif K_a > K_o and (succ_K is None or K_a < succ_K):
                    succ_K, succ_idx = K_a, ci

            lower = (pred_idx + 1) if pred_idx is not None else sec_start
            upper = succ_idx if succ_idx is not None else sec_end
            if lower > upper:
                continue  # 앵커 모순 → orphan 유지(a)

            # (2) body-y 미세앵커: [lower, upper) 안에서 y 추정 > fig_y 첫 라인 앞.
            insert_at = None
            if fig_y is not None and doc is not None and 0 <= pg - 1 < doc.page_count:
                try:
                    clean_lines = _twocol_page_lines(doc[pg - 1])
                    section_lines = lines[sec_start:sec_end]
                    _, _, covered, _ = _recover_absent_blocks(
                        clean_lines, "\n".join(section_lines))
                    body_y = _estimate_body_line_y(clean_lines, covered, section_lines)
                    for r in range(lower - sec_start, upper - sec_start):
                        by = body_y[r]
                        if by is not None and by > fig_y:
                            insert_at = sec_start + r
                            break
                except Exception:  # noqa: BLE001 — 미세앵커 실패는 비차단
                    insert_at = None

            if insert_at is None:
                # inline-figure 앵커 폴백: predecessor 캡션 바로 뒤 / successor 캡션 바로 앞.
                if pred_idx is not None:
                    insert_at = lower
                elif succ_idx is not None:
                    insert_at = upper
                else:
                    continue  # 앵커 없음 → 확신 불가 → orphan 유지(a)

            # 표/코드펜스 내부 앵커는 블록 앞으로(구조 보호, f). 단 하한 아래로는 넘기지 않음.
            rel = _anchor_outside_table_fence(lines[sec_start:sec_end], insert_at - sec_start)
            insert_at = sec_start + rel
            if insert_at < lower:
                insert_at = lower

            block = [f"**{cap}**", ""]
            if insert_at > 0 and lines[insert_at - 1].strip() != "":
                block = [""] + block  # 앞 본문과 빈 줄 분리
            lines[insert_at:insert_at] = block
            reinserted += 1
            print(f"    [ORPHAN->INLINE] p{pg}: Figure {num} 캡션 원본 y 위치 재삽입"
                  f"(orphan 버킷 대신)", flush=True)
    finally:
        if doc is not None:
            doc.close()
    return reinserted


def inject_figure_refs_into_md(md_path, figs, output_dir, pdf_path=None):
    """추출된 figure/complex_table 이미지를 변환 MD 에 주입한다.

    F6(2026-07-09): 기본(FMDW_INLINE_FIGURES=1)은 각 이미지를 본문의 해당 'Figure N:'
    캡션 라인 '바로 아래'에 인라인 배치(읽기 흐름 보존). 본문 캡션과 매칭되지 않는
    figure 는 문서 끝 orphan bucket(`<!-- fmdw:extracted-figures -->` 하위)에 배치해
    무손실을 보장한다. 오버사이즈 행렬표(complex_table)는 캡션이 없으면 그 페이지
    마커 바로 아래(페이지 섹션 상단)에 배치한다. FMDW_INLINE_FIGURES=0 이면 기존
    '본문 끝 일괄 섹션' 배치.

    회귀 안전: figs 비었거나 PNG 없으면 no-op(byte-identical). 마커 존재 시 재주입 금지(멱등).
    """
    if not figs:
        return 0
    md_path = Path(md_path)
    output_dir = Path(output_dir)
    try:
        md = md_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    if _FIGURE_REF_MARKER in md:
        return 0  # 이미 주입됨(중복 방지, 멱등 앵커)

    valid = []
    for f in figs:
        ip = (f.get("image_path") or "").strip()
        if not ip or not (output_dir / ip).exists():
            continue
        valid.append(f)
    if not valid:
        return 0

    def _sort_key(f):
        try:
            pg = int(f.get("page", 0))
        except (TypeError, ValueError):
            pg = 0
        return (pg, f.get("image_path", ""))

    valid.sort(key=_sort_key)

    # ── FMDW_INLINE_FIGURES=0: 기존 '본문 끝 일괄 섹션' 경로(하위호환) ──────────────
    if not _inline_figures_enabled():
        tail = ["", "---", "", _FIGURE_REF_MARKER, ""]
        for f in valid:
            tail.extend(_figure_block_lines(f, with_heading=True))
        new_md = md.rstrip("\n") + "\n\n" + "\n".join(tail).rstrip("\n") + "\n"
        md_path.write_text(new_md, encoding="utf-8")
        print(f"[+] MD 본문 끝에 이미지 참조 {len(valid)}개 주입(trailing): {md_path}", flush=True)
        return len(valid)

    # ── F6 인라인 배치 ──────────────────────────────────────────────────────────
    lines = md.split("\n")
    _reorder_figure_captions_by_id(lines, valid)  # 같은 페이지 다중 figure 캡션 순서 정정(F7)
    # F12(2026-07-11): 단일-figure 페이지에서 glm 이 캡션을 원본 위치보다 아래로 놓은 경우
    #   원본 figure y(사이드카 bbox top) 자리로 상향 재배치(캡션↔이미지 쌍 함께 이동).
    _reposition_single_figure_captions_by_y(lines, valid, pdf_path)
    # F13(2026-07-11): glm 이 Figure 캡션을 본문에서 통째 누락 → orphan 버킷으로 밀릴
    #   figure 를 원본 페이지 y 위치 본문에 캡션 재삽입(이후 정상 인라인). orphan 없으면 무변화.
    _reinsert_orphan_figure_captions_by_y(lines, valid, pdf_path)
    cap_idx_by_num = {}     # figure/table 번호 → 본문 캡션 라인 index(첫 매칭)
    page_marker_idx = {}    # 페이지 번호 → '<!-- page N -->' 라인 index(첫 매칭)
    for i, l in enumerate(lines):
        m = _BODY_CAP_LINE_RE.match(l)
        if m:
            cap_idx_by_num.setdefault(m.group(1), i)
        pm = _INJECT_PAGE_MARKER_RE.match(l)
        if pm:
            page_marker_idx.setdefault(int(pm.group(1)), i)

    inserts = {}   # 라인 index → 그 라인 '뒤'에 삽입할 블록 라인들
    orphans = []
    inlined = 0
    for f in valid:
        typ = (f.get("type") or "figure").strip()
        num = _fig_caption_number(f.get("caption") or "")
        target = None
        if num is not None and num in cap_idx_by_num:
            target = cap_idx_by_num[num]           # 캡션 라인 바로 아래
        elif typ == "complex_table":
            try:
                pg = int(f.get("page", 0))
            except (TypeError, ValueError):
                pg = 0
            if pg in page_marker_idx:
                target = page_marker_idx[pg]        # 페이지 섹션 상단
        if target is None:
            orphans.append(f)
            continue
        inserts.setdefault(target, []).extend(_figure_block_lines(f, with_heading=False))
        inlined += 1

    out = []
    for i, l in enumerate(lines):
        out.append(l)
        if i in inserts:
            out.append("")
            out.extend(inserts[i])
            if out and out[-1] == "":
                out.pop()  # 블록 말미 중복 빈줄 정리(뒤 원문과 blank-line 1개로 이어짐)

    body_text = "\n".join(out).rstrip("\n")
    orphan_n = len(orphans)
    if orphans:
        tail = ["", "---", "", _FIGURE_REF_MARKER, ""]
        for f in orphans:
            tail.extend(_figure_block_lines(f, with_heading=True))
        new_md = body_text + "\n\n" + "\n".join(tail).rstrip("\n") + "\n"
    else:
        # 전부 인라인 → orphan 없음. 멱등 앵커로 '빈' 마커만 남긴다(현행 마커 기반
        # 재주입 방지 계약 유지 — 이 선택 이유는 리포트에 명시).
        new_md = body_text + "\n\n" + _FIGURE_REF_MARKER + "\n"
    md_path.write_text(new_md, encoding="utf-8")
    print(f"[+] figure 주입(F6): inline {inlined}개 / orphan-bucket {orphan_n}개 → {md_path}",
          flush=True)
    return inlined + orphan_n


class _RateLimiter:
    """호출 간 최소 간격 토큰버킷 + 429 적응형 백오프(M-6).

    - base_delay: 직전 호출 종료 후 다음 호출까지 보장할 최소 간격(초). 토큰버킷처럼
      이미 그만큼 경과했으면 추가 대기 0. 0 이면 적응형(429)만 동작.
    - note_rate_limited(): provider 429 신호 시 호출. 다음 1회 대기를 가산(적응형).
    - wait_before_next(): 다음 호출 직전에 호출. 필요한 잔여 시간만 sleep.
    - 마지막 호출 뒤에는 호출자가 wait_before_next 를 부르지 않으므로 sleep 생략됨.
    """

    def __init__(self, base_delay: float = 0.0):
        self.base_delay = max(0.0, base_delay)
        self._last_call_end: float | None = None
        self._adaptive_extra: float = 0.0

    def note_rate_limited(self, extra: float) -> None:
        """429 등 레이트리밋 신호 — 다음 대기에 extra(초)를 1회 가산."""
        if extra > 0:
            self._adaptive_extra = max(self._adaptive_extra, extra)

    def wait_before_next(self) -> float:
        """다음 호출 직전 필요한 잔여 시간만 대기. 실제 sleep 한 초 반환(테스트용)."""
        import time as _t
        now = _t.monotonic()
        need = self._adaptive_extra
        if self._last_call_end is not None and self.base_delay > 0:
            elapsed = now - self._last_call_end
            remaining = self.base_delay - elapsed
            if remaining > need:
                need = remaining
        self._adaptive_extra = 0.0  # 적응형은 1회성
        if need > 0:
            _t.sleep(need)
            return need
        return 0.0

    def mark_call_end(self) -> None:
        """방금 호출이 끝난 시각을 기록(토큰버킷 기준점)."""
        import time as _t
        self._last_call_end = _t.monotonic()

# 추출 provider: 기본 ollama_cloud(로컬 게이트웨이, 키 불필요),
# EXTRACT_PROVIDER=gemini 로 기존 Gemini File API 경로 fallback.
#
# 2차 QA(Quality Assurance) — Claude Vision verifier (opt-in, 기본 OFF):
#   환경변수 VISION_QA=claude_cli 설정 시, Ollama 1차 추출(generator) 결과를
#   원본 페이지 이미지를 실제로 보는 Claude vision(verifier)으로 대조·교정한다.
#   환각·범위 일반화 추정·값 오독·서브회로 누락을 잡는 게 목적이다.
#   미설정 시 기존 동작 그대로(1차 추출본 = 최종). 호출 실패 시 안전 degrade로
#   1차 추출본을 그대로 사용하므로 파이프라인은 중단되지 않는다.
#   출력 형식 계약({stem}.md, `\n\n---\n\n` 청크 결합, Figure 섹션 구조)은 보존.

# Configuration — env > config.yaml > 코드기본값 (lib/config.py SSoT 경유)
#: PDF 청크 크기(페이지 수). env EXTRACT_CHUNK_SIZE 또는 config.yaml options.chunk_size.
CHUNK_SIZE: int = _cfg.knob_chunk_size()
TEMP_PDF_DIR = Path("input/pdf/temp_all_converted")

# [VISION_QA_AUTO] 앙상블(dense) 적용 페이지 누적 카운터(비용 가드). PDF 단위로
# process_pdf_auto 진입 시 0 으로 리셋한다.
_AUTO_ENSEMBLE_USED = 0

# Strong figure-analysis prompt shared by all multimodal calls.
#
# 회로도(Schematic) 전사 강화 (2026-05-29 적용, 사용자 명시 승인):
#   프롬프트 변형 실험(baseline/V1 환각억제/V2 커버리지/V3 1:1강제 × p11/p7) 결과,
#   V2(region enumeration, 영역 열거 후 전사)가 서브회로 커버리지와 BGA(Ball Grid Array)
#   ball 좌표 포착을 결정적으로 개선(p11 region 1->14, ball 좌표 0/5->5/5)함이 검증됨.
#   V3(무조건 핀별 1:1 강제)는 BOM(Bill of Materials) 폭주로 토큰 소진->truncate(역효과).
#   따라서 최적안 = V2 region-inventory 기반 + V1 환각가드를 '값/부품번호 컬럼에 한정' +
#   V3 폭주 방지를 위한 '동일값 부품 그룹화 허용'. 출력 형식 계약(Figure 섹션 구조,
#   `{stem}.md`, `\n\n---\n\n` 청크 결합)은 그대로 보존하고 transcription 지시만 강화한다.
#
# [G-3 한계 — 설계 트레이드오프, 명시적 인지]
#   아래 "**Quantitative Data**"(차트 X/Y 라벨·단위·범위, series, 피크/교차/임계 등)는
#   *프롬프트 지시*로만 강제된다. **추출된 차트 수치를 원본 픽셀과 알고리즘으로 대조해
#   검증하는 코드는 없다** — 구조 충실도가 모델의 프롬프트 준수에 달려 있고(보장이 아님),
#   값 정확도는 보증되지 않는다. 차트 정량값을 알고리즘으로 검증하려면 별도 chart-parsing
#   computer-vision(축 눈금 검출·데이터점 픽셀 회귀)이 필요한데, 그 비용/복잡도는 본 도구의
#   범위를 벗어나므로 의도적으로 채택하지 않았다. 대신 (a) 회로도 designator/net 은
#   net_tracer 벡터 교차검증(net_crosscheck)으로 결정적 보강이 있고, (b) vision QA 의
#   _sanity_gate(구조 가드·길이 가드·L-13 designator 보존율 가드)가 *대량 누락*은 잡지만
#   *차트 수치 오독*은 잡지 못한다. 다운스트림(RAG 등)에서 차트 수치를 신뢰할 때는 이
#   한계(=모델 의존, 알고리즘 보장 아님)를 인지해야 한다. (출처: FITNESS_REVIEW G-3)
FIGURE_RULES = (
    "FIGURE TYPE GATE (decide FIRST, before any transcription):\n"
    "- Classify each figure as either (A) a real CIRCUIT/PCB SCHEMATIC or dense datasheet "
    "diagram with legible part designators (U1, R12), pin numbers and net labels; or (B) a "
    "CONCEPTUAL figure — block diagram, system architecture, flowchart, timing diagram, "
    "data-flow, concept drawing, chart, table-image, or photo.\n"
    "- Apply the SCHEMATIC strategy below ONLY to type (A). For type (B), DO NOT emit pin->net "
    "tables, a 'REGION INVENTORY' list, BGA(Ball Grid Array) balls, or part-number tables — "
    "describe the visible blocks, arrows and labels using the figure section format further below.\n"
    "- ANTI-FABRICATION (HIGHEST priority, all figure types): NEVER invent or 'complete' content "
    "that is not actually legible in the image. Do NOT introduce specific part numbers, FPGA/SoC "
    "model names (e.g. XC7Z010, Zynq-7000), JTAG/configuration nets, connector pinouts, or "
    "designators unless they are clearly printed in THIS figure. If a figure is blurry or its type "
    "is unclear, describe only what is genuinely visible and mark uncertain parts as "
    "[판독 불가]/[unclear]. A plausible-looking schematic invented for an unreadable conceptual "
    "figure is a CRITICAL hallucination error — describing less is always better than inventing.\n"
    "- LANGUAGE / LABELS: preserve original Korean figure ids and captions verbatim "
    "(도면 N, 도 N, 그림 N) and all 부재번호 / reference numerals (110, 200, 230 ...) exactly as "
    "printed; do not translate designators or invent numerals.\n\n"
    "PLAIN-LANGUAGE EXPLANATION (REQUIRED for EVERY figure type, ADDITIVE — it is ADDED ON TOP OF, "
    "and NEVER replaces or shortens, the exact transcription such as PIN -> NET tables, designators, "
    "GFM tables, the region inventory and numeric values): besides the precise data, you MUST also "
    "explain each figure so that a NON-EXPERT with no engineering background can understand it fully. "
    "Write this explanation in the document's main language (Korean for Korean documents). Provide "
    "three things:\n"
    "  (1) ONE-LINE PLAIN SUMMARY first — say in plain everyday words WHAT the figure is "
    "(e.g. '이것은 ~의 신호 흐름을 보여주는 블록 다이어그램이다').\n"
    "  (2) EASY, DETAILED WALK-THROUGH — explain the parts, how they connect, the flow, and the role "
    "of each part in plain everyday language, as if telling a story, with enough detail that a "
    "beginner can follow it end to end. Do NOT drop or summarise away the precise data to do this — "
    "the easy text is in ADDITION to the exact transcription, not instead of it.\n"
    "  (3) GLOSS EVERY TERM / ACRONYM inline the first time it appears, in parentheses with a plain "
    "meaning (e.g. 'GPIO(범용 입출력 핀)', 'PWM(폭이 변하는 펄스 신호)', 'net(부품을 잇는 전기 배선)').\n"
    "  ANTI-FABRICATION STILL DOMINATES here: explain ONLY what is genuinely visible. If anything is "
    "unclear or not actually shown, write '불명확/판독 불가' — never invent a plausible-sounding "
    "explanation just to make it readable. When easy wording and accuracy conflict, ACCURACY WINS "
    "and completeness of the precise data is never sacrificed.\n\n"
    "SCHEMATIC / DENSE-DIAGRAM TRANSCRIPTION STRATEGY (apply ONLY to type (A) real circuit schematics; "
    "keep the inventory internal — do NOT print a literal 'REGION INVENTORY' heading):\n"
    "- STEP 1 — REGION INVENTORY: Scan the WHOLE page from top-left to bottom-right and FIRST "
    "emit a numbered list of EVERY distinct sub-circuit, IC, connector, standalone table, note, "
    "and the title block. Do not skip small auxiliary blocks (reset supervisors, power-on-reset, "
    "decoupling banks, side notes, corner tables). This manifest comes before detailed output.\n"
    "- STEP 2 — DETAIL EACH REGION: Then transcribe every region from the manifest in full. Do "
    "not collapse the page into a single figure; do not focus only on the largest block.\n"
    "- PIN-LEVEL CONNECTIONS: For each IC give per-pin connections as `PIN -> NET` (one row per "
    "pin). Preserve BGA(Ball Grid Array) ball coordinates verbatim (e.g. M8, L2, G8, F7, E8); "
    "never replace a ball/pin number with a family range. Do NOT collapse a pin family such as "
    "A0-A12 into one row for an IC pin map — list each pin you can read.\n"
    "- REPEATED-IDENTICAL INSTANCES: If several IC instances share the SAME pinout (e.g. U25/U26 "
    "identical to U24 except for the data-net names), emit the FULL per-pin table ONCE for the "
    "first instance, then for the others give only the differing rows plus a note 'pinout "
    "identical to <first> except: ...'. Do not repeat the entire identical address/power/ground "
    "table for every instance — that wastes the token budget and causes truncation.\n"
    "- CONNECTORS / LEGIBILITY: For dense connectors (MICTOR, etc.) emit a per-pin `PIN -> NET` "
    "table ONLY when the pin numbers are clearly legible. When pin numbers are NOT legible, do "
    "NOT fabricate a numbered pin list — instead give a grouped signal list (the nets/bus names "
    "actually visible) and mark the uncertain pin assignment as such. Never invent specific "
    "pin->net assignments to fill a table.\n"
    "- VALUE / PART-NUMBER ACCURACY (anti-hallucination, scoped to value & part-number cells "
    "ONLY): If a component value or part number is unreadable, blurry, or ambiguous, output "
    "[unreadable] for that cell — never guess, infer, or 'complete' a plausible value. Designator "
    "labels (R91, U24A, CON2 ...) and net/signal labels (DDR_DQ48, JTRST ...) must be copied "
    "verbatim as seen.\n"
    "- GROUPING TO AVOID BOM(Bill of Materials) OVERFLOW: Many discrete parts that share one value "
    "MAY be grouped on a single line, e.g. `R91-R112: 25R5/1%` or `C59-C74: 0.1uF/50V`, to prevent "
    "token exhaustion and truncation. Unique IC pin maps stay one row per pin; connector tables "
    "follow the legibility rule above.\n"
    "- OUTPUT BUDGET / PRIORITY: Keep the whole page within the response budget. Spend tokens in "
    "this priority order so nothing at the end is lost to truncation: (1) region inventory, "
    "(2) every distinct component/part-number/designator and net, (3) small auxiliary blocks, "
    "notes and the TITLE BLOCK, (4) exhaustive per-pin connector enumeration last. Always finish "
    "the title block.\n"
    "- COMPLETENESS REMINDER: Do not skip whole small auxiliary circuits, reset/supervisor ICs, "
    "jumpers, or corner tables just because the main schematic block is larger — but only those you "
    "can actually read; this is about not ignoring visible regions, never about guessing parts you "
    "cannot see.\n"
    "- COMPONENT ROSTER (accuracy-first, NOT a fill-in-the-blanks quota): List the component "
    "designators you can ACTUALLY and CLEARLY read on the page — R / C / L / D / Q / U / J / CON / "
    "TP / FB / Y / SW / K references (R1, C3, U2, J4, Q1, D5, Y1 ...) together with the value or "
    "part-number you can read (or [unreadable]) and, for ICs, the pin labels. Emit a compact "
    "one-line-per-designator roster. You MAY group a contiguous range like `R91-R112: 25R5/1%` "
    "ONLY when you can actually read every designator in that range AND they truly share one value; "
    "never manufacture a range to look complete.\n"
    "- ★★ NEVER GUESS OR FILL NUMBERS SEQUENTIALLY (top anti-hallucination rule): Write a designator "
    "ONLY if you genuinely see it. If the parts you can read jump from C22 to C34, that is CORRECT — "
    "write C22 then C34 and DO NOT invent C23...C33 to bridge the gap. Do not assume a missing number "
    "exists, do not continue a numeric sequence, and do not repeat one value (e.g. '100nF') across a "
    "guessed run of consecutive parts. Blurry, cropped, tiny or uncertain designators/values must be "
    "LEFT OUT — if a dense region is not reliably readable, write '[일부 부품 판독 불가]' for that "
    "region instead of inventing entries to fill it.\n"
    "- ACCURACY OVER COMPLETENESS (absolute priority): It is FAR better to list fewer, certain parts "
    "than to invent even one. ONE fabricated part is much worse than TEN honest omissions. Missing a "
    "hard-to-read part is acceptable; adding a part that is not on the page is a CRITICAL "
    "hallucination failure. Target: 100 percent of what you list is genuinely READABLE, 0 percent "
    "invented — completeness is bounded strictly by what is truly legible and must never become "
    "hallucination.\n\n"
    "GENERAL RULES:\n"
    "- Preserve all text structure (headings, lists, paragraphs).\n"
    "- Tables -> GFM(GitHub Flavored Markdown) pipe format. Merge cells stated explicitly.\n"
    "- Math -> LaTeX inline ($...$) or block ($$...$$).\n"
    "- Code/commands -> fenced code blocks.\n\n"
    "CRITICAL: For EVERY figure/diagram/chart/photo/schematic, insert a dedicated section:\n\n"
    "### Figure N: <auto-generated descriptive title>\n"
    "**한 줄 요약 (쉬운 말 / one-line plain summary)**: <이 그림이 무엇인지 한 문장으로, 전문지식 없는 "
    "사람도 알 수 있는 평범한 말로>\n"
    "**Type**: <block diagram | circuit schematic | flowchart | timing diagram | photo | "
    "table-image | chart(bar/line/scatter) | screenshot | floor plan | mechanical drawing | other>\n"
    "**Caption (original)**: <if present in document, verbatim>\n\n"
    "**Components / Elements**:\n"
    "- (list each labeled block, symbol, axis, legend item with its exact text/label)\n"
    "- For a circuit schematic this is a roster of the component designators you can CLEARLY read "
    "(R / C / L / D / Q / U / J / CON / TP ...) with its value/part-number and pin labels. Do NOT "
    "guess or fill in numbers you cannot see — if readable parts jump C22->C34, never invent "
    "C23-C33; mark unreadable/dense regions [일부 부품 판독 불가] rather than guessing. List only what "
    "is genuinely visible and invent nothing — accuracy over completeness (one fabricated part is far "
    "worse than ten omissions).\n\n"
    "**Relations / Connections**:\n"
    "- (arrows, lines, signal flow, hierarchy — describe source -> target with any label on the edge)\n\n"
    "**Quantitative Data** (if chart/graph):\n"
    "- Axes: X=<label, unit, range>, Y=<label, unit, range>\n"
    "- Series: <name, color/style, key data points or trend>\n"
    "- Notable values: peaks, intersections, thresholds\n\n"
    "**Visual Encoding**:\n"
    "- Color meaning (e.g., red = error path, blue = clock domain)\n"
    "- Symbol meaning (e.g., dashed = optional, double arrow = bidirectional)\n\n"
    "**Textual Content in Image**: <verbatim transcription of any text inside the image, "
    "including small labels, watermarks, units>\n\n"
    "Then, with NO header/label of any kind (do not write a heading such as '쉬운 설명' or "
    "'easy explanation' before it), include a plain paragraph that, based on the precise data above "
    "(components/connections/values), explains the components, connections, flow, and role of each "
    "part in everyday language a non-expert can follow, in a narrative style. (Do not shorten the "
    "precise data above — add this on top of it. State only what is visible; if unclear or "
    "unreadable, say so rather than guessing.)\n\n"
    "**용어 풀이 (Glossary)**: 위 설명에 등장한 전문용어·약어를 '약어(쉬운 뜻)' 형태로 모아 정리한다 "
    "(예: GPIO(범용 입출력 핀)). 등장한 용어가 없으면 생략 가능.\n\n"
    "**Interpretation / Purpose**: 1-3 sentences on what this figure communicates in context.\n\n"
    "If figure is a photo, describe: subject, viewpoint, key visible components, environmental "
    "context, any visible measurements/labels.\n\n"
    "Do NOT summarize away details. Err on side of completeness for figures.\n"
)

def setup_dirs():
    TEMP_PDF_DIR.mkdir(parents=True, exist_ok=True)

def convert_to_pdf(input_path):
    """Converts various formats to PDF using LibreOffice."""
    print(f"[*] Converting {input_path.name} to PDF...", flush=True)
    try:
        cmd = [
            "soffice", "--headless", "--convert-to", "pdf", 
            "--outdir", str(TEMP_PDF_DIR), str(input_path)
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        pdf_path = TEMP_PDF_DIR / input_path.with_suffix(".pdf").name
        return pdf_path if pdf_path.exists() else None
    except Exception as e:
        print(f"[!] PDF conversion error for {input_path.name}: {e}")
        return None

def apply_netcheck(md, pdf_path, start_page, end_page):
    """vision QA 산출 MD 에 net_tracer 교차검증을 적용(VISION_QA_NETCHECK=1 시).

    청크는 start_page..end_page 다중 페이지를 포함하나 net_tracer 는 페이지 단위다.
    M-7: net_tracer 서브프로세스를 **범위로 1회만** 실행(run_net_tracer_range)하여
    페이지별 tracer JSON 을 미리 받아두고, 각 페이지에 crosscheck_with_tracer 를
    순차 적용한다(플래그 누적). 기존엔 페이지마다 서브프로세스를 재실행해 같은 PDF 를
    페이지 수만큼 재파싱했다 → 1회 오픈으로 축약(출력/플래그 동작은 불변).
    net_tracer 무력/실패 시 안전 degrade(해당 페이지는 MD 무변경). 형식 계약 보존.
    """
    if VISION_QA_NETCHECK != 1 or not md:
        return md
    # M-7: 범위 1회 호출로 페이지별 tracer JSON 사전 취득(서브프로세스 N→1).
    try:
        tracer_map = netcheck.run_net_tracer_range(pdf_path, start_page, end_page)
    except Exception as e:  # noqa: BLE001 — 전체 degrade(페이지별 fallback)
        print(f"    [~] net_tracer range error p{start_page}-{end_page}: {e}", flush=True)
        tracer_map = {}
    current = md
    agg = {"vector_confirmed": 0, "spurious_flagged": 0, "vector_only_nets": 0,
           "applied_pages": [], "skipped_pages": []}
    for page in range(start_page, end_page + 1):
        try:
            tracer = tracer_map.get(
                page, {"ok": False, "page": page, "reason": "no tracer line for page"}
            )
            res = netcheck.crosscheck_with_tracer(current, tracer)
        except Exception as e:  # noqa: BLE001 — 안전 degrade
            print(f"    [~] net_tracer crosscheck error p{page}: {e}", flush=True)
            agg["skipped_pages"].append(page)
            continue
        current = res.markdown
        if res.applied:
            # L-5: 문자열 리터럴 대신 net_crosscheck 의 summary-key 상수(SSoT)를 사용해
            #      producer↔consumer 키 오타로 인한 조용한 0 집계를 방지.
            agg["vector_confirmed"] += res.summary.get(netcheck.SK_VECTOR_CONFIRMED, 0)
            agg["spurious_flagged"] += res.summary.get(netcheck.SK_SPURIOUS_FLAGGED, 0)
            agg["vector_only_nets"] += res.summary.get(netcheck.SK_VECTOR_ONLY_NETS, 0)
            agg["applied_pages"].append(page)
        else:
            agg["skipped_pages"].append(page)
    if agg["applied_pages"]:
        print(
            f"    [+] net_tracer crosscheck applied p{agg['applied_pages']}: "
            f"vector_confirmed={agg['vector_confirmed']} "
            f"spurious_flagged={agg['spurious_flagged']} "
            f"vector_only_nets={agg['vector_only_nets']} "
            f"(skipped={agg['skipped_pages']})",
            flush=True,
        )
    else:
        print(f"    [~] net_tracer crosscheck no-op (all pages degraded: "
              f"{agg['skipped_pages']})", flush=True)
    return current


def _netcheck_single_page(md, pdf_path, page):
    """단일 페이지 net_tracer 교차검증(AUTO 모드 — VISION_QA_NETCHECK 게이트 무시).

    AUTO 모드에서는 tier(dense/light)가 netcheck 적용을 결정하므로, 수동 플래그
    VISION_QA_NETCHECK 와 무관하게 호출된다. net_tracer 무력/실패 시 안전 degrade
    (MD 그대로). 형식 계약 보존. 반환: (markdown, applied:bool, summary).
    """
    if not md:
        return md, False, {}
    try:
        res = netcheck.crosscheck(md, pdf_path, page)
    except Exception as e:  # noqa: BLE001 — 안전 degrade
        print(f"    [~] net_tracer crosscheck error p{page}: {e}", flush=True)
        return md, False, {}
    return res.markdown, res.applied, res.summary


def extract_page_auto(pdf_path, page, doc=None):
    """[VISION_QA_AUTO] 단일 페이지 1차 추출 + 페이지별 자동 티어링 처리.

    1) 페이지 1건을 1차 추출(효과적 CHUNK_SIZE=1).
    2) lib.page_tier.classify_page 로 dense/light/text 판별.
    3) tier별 vision QA 강도 자동 선택:
         dense → review_ensemble(n=N, DPI override) + net_crosscheck
         light → review(단일) + net_crosscheck
         text  → vision QA skip(1차 MD 그대로)
    4) 비용 가드: 앙상블 적용 페이지 상한(VISION_QA_MAX_ENSEMBLE_PAGES) 초과 시 dense→light 강등.

    Args:
        pdf_path : 원본 PDF 경로.
        page     : 1-based 페이지 번호.
        doc      : (M-5) 이미 열린 fitz Document 핸들(재사용). None 이면 각 하위
                   호출이 내부에서 open(기존 동작). 같은 PDF 를 페이지당 1차추출
                   render + classify 가 각각 재오픈하던 것을 1회 오픈으로 축약한다.

    Returns:
        (markdown|None, tier_record:dict). tier_record 는 summary 집계용
        (page, tier, signals, applied_strength). 1차 추출 실패 시 (None, record).

    부작용: 모듈 전역 _AUTO_ENSEMBLE_USED 를 증가시킨다(비용 가드 누적 카운터).
    """
    global _AUTO_ENSEMBLE_USED

    # 1) 1차 추출(페이지 단위).
    print(f"    - [AUTO] Extracting page {page} (primary)...", flush=True)
    try:
        prompt = (
            "You are converting documents to high-quality Markdown.\n\n"
            f"Extract the full content of page {page} from this document.\n\n"
            + FIGURE_RULES +
            "\nOutput ONLY the Markdown content (no surrounding code fence)."
        )
        # M-5: 공유 핸들이 있으면 재사용(재오픈 회피). 없으면 기존 4-인자 호출 그대로
        #      — ox.extract_pdf_pages 를 monkeypatch 한 기존 테스트 계약 보존.
        if doc is not None:
            primary_md = ox.extract_pdf_pages(prompt, pdf_path, page, page, doc=doc)
        else:
            primary_md = ox.extract_pdf_pages(prompt, pdf_path, page, page)
    except Exception as e:  # noqa: BLE001
        print(f"    [!] [AUTO] Error extracting page {page}: {e}", flush=True)
        return None, {"page": page, "tier": "error", "signals": {}, "strength": "extract-failed"}

    if not primary_md:
        return None, {"page": page, "tier": "error", "signals": {}, "strength": "empty-primary"}

    # 2) 페이지 성격 분류(벡터 + MD 휴리스틱). M-5: 공유 doc 핸들 재사용.
    if doc is not None:
        pt = ptier.classify_page(pdf_path, page, primary_md, doc=doc)
    else:
        pt = ptier.classify_page(pdf_path, page, primary_md)
    tier = pt.tier
    sig = pt.signals
    print(f"    - [AUTO] page {page} tier={tier} "
          f"(vec={sig.get('vector_lines')} desig={sig.get('designators')} "
          f"pin={sig.get('pin_rows')} kw={sig.get('matched_keywords')})", flush=True)

    # W2: 실제 QA(vision verifier) 호출이 발생했는지 표식. process_pdf_auto 의
    # rate-limit sleep 게이트가 tier 라벨이 아니라 **실제 API 호출 여부**로 판단하도록
    # record 에 실어 보낸다(vqa-disabled/text/dryrun 페이지의 무의미한 sleep 제거).
    record = {"page": page, "tier": tier, "signals": sig, "strength": tier,
              "qa_called": False}

    # dry-run: vision QA 실행 없이 분류만 — 1차 MD 그대로 반환.
    if VISION_QA_AUTO_DRYRUN:
        record["strength"] = f"dryrun({tier})"
        return primary_md, record

    # vision QA 가 비활성(VISION_QA 미설정)이면 tier 무관 1차 MD 그대로(자동 모드도
    # verifier 없이는 강화 불가). text 와 동일하게 1차 반환.
    if not vqa.is_enabled():
        print(f"    [~] [AUTO] vision QA disabled — page {page} 1차 MD 그대로", flush=True)
        record["strength"] = "vqa-disabled"
        return primary_md, record

    # 3) tier별 처리.
    if tier == "text":
        record["strength"] = "text(skip)"
        return primary_md, record

    effective_tier = tier
    # 비용 가드: dense 앙상블 상한 초과 시 light 로 강등.
    if tier == "dense":
        if _AUTO_ENSEMBLE_USED >= VISION_QA_MAX_ENSEMBLE_PAGES:
            print(f"    [!] [AUTO] ensemble budget exceeded "
                  f"({_AUTO_ENSEMBLE_USED}/{VISION_QA_MAX_ENSEMBLE_PAGES}) — "
                  f"page {page} dense→light 강등", flush=True)
            effective_tier = "light"
            record["strength"] = "dense→light(budget)"

    # W1: QA(dense/light) 호출 + result 사용 + netcheck 블록을 try/except 로 감싼다.
    # vqa_ensemble.review_ensemble / vqa.review 가 **예상 외** 예외(RuntimeError 등,
    # 내부 degrade 를 벗어난)를 던지면, 그것이 process_pdf_auto→process_file 로 전파되어
    # PDF 전체가 abort 되고 이미 성공한 다른 페이지(text 포함)까지 유실된다. 바로 위
    # _netcheck_single_page 는 try/except 로 보호되는데 QA 만 무방비였던 비대칭 해소.
    # 예외 시 primary_md 로 안전 degrade + strength 에 사유 기록 후 **계속 진행**한다
    # (한 페이지 실패가 배치 중단으로 이어지지 않게 — netcheck 와 동일 degrade 철학).
    md = primary_md
    try:
        if effective_tier == "dense":
            _AUTO_ENSEMBLE_USED += 1
            print(f"    - [AUTO] Vision QA ENSEMBLE n={VISION_QA_AUTO_ENSEMBLE_N} "
                  f"DPI={VISION_QA_AUTO_DENSE_DPI} ({vqa.backend_label()}) page {page} "
                  f"[{_AUTO_ENSEMBLE_USED}/{VISION_QA_MAX_ENSEMBLE_PAGES}]...", flush=True)
            result = vqa_ensemble.review_ensemble(
                primary_md, pdf_path, page, page,
                n=VISION_QA_AUTO_ENSEMBLE_N, dpi=VISION_QA_AUTO_DENSE_DPI)
            record["qa_called"] = True  # W2: 실제 ensemble 호출 발생.
            record["strength"] = "dense(ensemble+netcheck)"
        else:  # light
            print(f"    - [AUTO] Vision QA single ({vqa.backend_label()}) page {page}...",
                  flush=True)
            result = vqa.review(primary_md, pdf_path, page, page)
            record["qa_called"] = True  # W2: 실제 single 호출 발생.
            if record["strength"] == tier:  # 강등이 아니었으면 light 라벨.
                record["strength"] = "light(single+netcheck)"

        if result.corrected:
            print(f"    [+] [AUTO] Vision QA corrected page {page} ({result.note})",
                  flush=True)
        else:
            print(f"    [~] [AUTO] Vision QA skipped/degraded page {page} ({result.note})",
                  flush=True)
        md = result.markdown

        # dense/light 공통: net_tracer 교차검증(결정적 게이트).
        # W4: netcheck 적용 여부(applied)에 따라 strength 라벨의 "netcheck" 접미를
        #     정확화한다(적용=+netcheck / degrade=+netcheck(noop)). 데이터 영향 없음 —
        #     운영 가시성만 정확화. 위에서 strength 는 "...(ensemble+netcheck)" /
        #     "...(single+netcheck)" 로 가정 라벨링됐으므로, degrade 시 "netcheck" 를
        #     "netcheck(noop)" 로 치환한다.
        md, applied, summary = _netcheck_single_page(md, pdf_path, page)
        if applied:
            # L-5: summary 읽기는 net_crosscheck 상수(SSoT)로. record["netcheck"] 의 키는
            #      manifest 출력 스키마(외부 계약)라 리터럴 그대로 유지(상수값과 동일 문자열).
            _vc = summary.get(netcheck.SK_VECTOR_CONFIRMED, 0)
            _sf = summary.get(netcheck.SK_SPURIOUS_FLAGGED, 0)
            _vo = summary.get(netcheck.SK_VECTOR_ONLY_NETS, 0)
            print(f"    [+] [AUTO] net_tracer crosscheck applied p{page}: "
                  f"vector_confirmed={_vc} "
                  f"spurious_flagged={_sf} "
                  f"vector_only_nets={_vo}", flush=True)
            record["netcheck"] = {
                "vector_confirmed": _vc,
                "spurious_flagged": _sf,
                "vector_only_nets": _vo,
            }
        else:
            print(f"    [~] [AUTO] net_tracer crosscheck no-op p{page} (degraded)",
                  flush=True)
            # W4: netcheck 가 적용되지 않았으므로 strength 의 "+netcheck)" 접미를
            #     "+netcheck(noop))" 로 정정(가시성). 예) "dense(ensemble+netcheck)" →
            #     "dense(ensemble+netcheck(noop))".
            if record["strength"].endswith("+netcheck)"):
                record["strength"] = (
                    record["strength"][: -len("+netcheck)")] + "+netcheck(noop))")
    except Exception as e:  # noqa: BLE001 — W1: QA 예상외 예외도 페이지 degrade(배치 보호).
        print(f"    [!] [AUTO] Vision QA error page {page} → 1차 MD 유지(배치 계속): {e}",
              flush=True)
        # strength 에 실패 사유 기록(예: dense(ensemble-failed:<err>)).
        base = "dense" if effective_tier == "dense" else "light"
        record["strength"] = f"{base}(qa-failed:{type(e).__name__})"
        md = primary_md

    return md, record


_GLM_FIGURE_PLACEHOLDER = (
    "FIGURES: If a page has a figure/diagram/chart/photo, do NOT describe its internal "
    "details and do NOT invent any part numbers, pins, or labels — emit only a short "
    "placeholder line noting it exists (e.g. '[그림: <visible caption if any>]'). Figures "
    "are cropped and described by a separate dedicated step. Still transcribe all real TEXT "
    "and TABLES on the page fully and exactly once.\n"
)


def _build_transcription_prompt(start_page: int, end_page: int,
                                include_figure_rules: bool = True) -> str:
    """본문 전사 프롬프트(전사 계약 + FIGURE_RULES) 를 [start_page, end_page] 범위로 생성.

    전사 계약(transcription contract, 2026-07-03/04): 표지/법적고지 등 "안 중요해
    보이는" 페이지 무단 스킵, TOC 항목 누락, 블록 재정렬을 방지하기 위해 페이지
    완전성·순서·읽기순서·목차 보존 규칙을 명시. FIGURE_RULES/출력형식은 기존 유지.
    2026-07-04 blank-escape 제거: qwen3-vl 이 "genuinely blank" 예외 문구를 악용해
    실제 내용(TOC/용어/마스크·금속화 표 등)이 있는 밀도 높은 페이지까지 `(blank)` 로
    마킹해 내용을 통째로 유실시키는 사고가 발생했다(Design Manual testpages p3,4,6~10
    전량 blank 마킹, 실제로는 전부 실데이터 페이지). 따라서 blank 이스케이프를 완전히
    삭제하고 "페이지에 내용이 있으면 반드시 전사"를 강제하는 엄격 규칙으로 대체한다.

    2026-07-04 하이브리드 전사 마커 정확도 수정(Advisor QA Warning): 이 함수는
    start_page/end_page 를 프롬프트 문구에 그대로 반영하므로, 청크 전체(start..end)
    호출뿐 아니라 하이브리드 단일 페이지 재전사(start==end==그 페이지 자신)에도 그대로
    재사용할 수 있다 — 프롬프트가 항상 실제 렌더된 페이지 범위와 일치해야
    `<!-- page N -->` 마커가 절대 페이지 번호와 어긋나지 않는다(청크 범위 문구를 그대로
    쓰면서 이미지는 페이지 1장만 주면 모델이 절대 페이지 번호를 알 수 없어 마커가
    틀어질 수 있었음 — 내용 유실은 아니고 감사(audit) 마커 정확도 문제였다).

    Args:
        start_page : 이번 호출에서 실제 렌더되는 시작 페이지(1-based, inclusive).
        end_page   : 이번 호출에서 실제 렌더되는 끝 페이지(1-based, inclusive).

    Returns:
        완성된 전사 프롬프트 문자열.
    """
    TRANSCRIPTION_CONTRACT = (
        "TRANSCRIPTION RULES (follow exactly):\n"
        f"- COMPLETE PAGE COVERAGE: Transcribe EVERY page in the range "
        f"{start_page} to {end_page} without exception, including cover/title "
        "pages, legal/copyright/\"Important Notice\" pages, disclaimers, page "
        "headers/footers, and any page that looks like boilerplate. NEVER skip a "
        "page because it seems unimportant.\n"
        "- NO BLANK SHORTCUTS: Every page in this document contains content — "
        "transcribe ALL visible text, tables, and figures on each page fully. A "
        "page that has ANY text, table, or figure is NOT blank and MUST be "
        "transcribed in full. Do NOT emit a blank/empty marker for a page that has "
        "content. Only if a page is TRULY, VISIBLY empty (no marks at all) may you "
        "note it — but when in doubt, prefer transcribing over marking blank.\n"
        "- PAGE ORDER + SEPARATOR: Output pages in strict ascending page-number "
        "order. Emit a page marker `<!-- page N -->` at the start of each page's "
        "content so coverage is auditable. Never merge or reorder pages.\n"
        "- READING ORDER: Within each page, transcribe strictly in natural reading "
        "order (top-to-bottom, and for multi-column layouts left-column fully then "
        "right-column). Preserve the source arrangement/sequence of blocks; do NOT "
        "reorder sections, notes, or tables.\n"
        "- TABLE OF CONTENTS pages: When a page is a Table of Contents / List of "
        "Tables / List of Figures, reproduce EVERY entry (including the very first "
        "ones like \"List of Tables\"), preserving the hierarchy (indentation for "
        "sub-sections) and each entry's trailing page number. Render each TOC line "
        "as `title ... N` (keep the dotted-leader style with \" ... \" and the page "
        "number). Do not summarize or drop entries.\n"
        "- SMALL PRINT / CORNERS: Transcribe EVERY text element on the page without "
        "exception, including small print in page corners, headers, and footers \u2014 "
        "especially version numbers, dates, and document IDs (for example a cover-page "
        "version/date block such as \"Version: A00-V0.9.2.0  2025-07-24\"). NEVER omit "
        "these small-print items.\n"
        "- TABLE OF CONTENTS IS NOT HEADINGS: Table-of-Contents / index / \"List of ...\" "
        "entries that use dotted leaders (\". . . .\") are plain list/text lines, NOT "
        "section headings. Do NOT convert TOC entries into Markdown headings (#, ##, "
        "###); keep each TOC entry as an ordinary text/list line exactly as written.\n"
        "- NO DUPLICATE OUTPUT: Never output the same content twice. Transcribe each "
        "block, paragraph, table, or section exactly once \u2014 do not repeat a block "
        "you already transcribed.\n"
        "- CHINESE TO ENGLISH: If any text on the page is written in Chinese (Han / CJK "
        "characters), translate that text into natural English in place and output the "
        "English translation; do NOT keep the original Chinese characters. Text already "
        "in English or Korean stays exactly as written.\n"
        "- CHAPTER-OPENING NUMERALS: A chapter often opens with a LARGE decorative "
        "number + title drawn as artwork (e.g. a big \"3\" next to \"Signal Description\"). "
        "This is real text, not a graphic — transcribe it as a heading `# N Title` and "
        "NEVER skip or drop it.\n"
        "- GROUPED (2-ROW) TABLE HEADERS: When a table header spans two rows (a group "
        "label covering several columns, with bold sub-labels beneath it), keep the group "
        "label in the GFM header row and put the bold sub-labels as the FIRST data row "
        "(GFM has no colspan). Do not lose the grouping.\n"
        "- TWO-COLUMN TERM/DESCRIPTION LAYOUT: When a page region shows a left column of "
        "short bold syntax/terms (e.g. \"A operator (B1, B2, B3)\", \"A length\", "
        "\"Different or same net\") with its description in a right column, transcribe it "
        "as a GFM table `| Syntax | Description |` with ONE row per term. NEVER "
        "concatenate a term and its description into a single paragraph, and never emit "
        "them as separate paragraphs. If the layout continues from a previous page, still "
        "emit a table with the same `| Syntax | Description |` header for this page's rows.\n"
    )
    return (
        "You are converting documents to high-quality Markdown.\n\n"
        f"Extract the full content of pages {start_page} to {end_page} from this document.\n\n"
        + TRANSCRIPTION_CONTRACT + "\n"
        + (FIGURE_RULES if include_figure_rules else _GLM_FIGURE_PLACEHOLDER) +
        "\nOutput ONLY the Markdown content (no surrounding code fence)."
    )


def extract_chunk(pdf_path, start_page, end_page, chunk_idx):
    print(f"    - Extracting pages {start_page}-{end_page} (Chunk {chunk_idx})...", flush=True)
    try:
        prompt = _build_transcription_prompt(start_page, end_page)
        if FMDW_BODY_HYBRID:
            # 하이브리드 경로(2026-07-04 수정): 청크 범위 프롬프트를 페이지별로
            # 재사용하지 않는다 — _hybrid_extract_range 가 페이지마다 자기 자신의
            # 절대 페이지 번호로 프롬프트를 새로 빌드해 <!-- page N --> 마커 정확도를
            # 보장한다(위 chunk 프롬프트는 non-hybrid 경로에서만 그대로 사용).
            primary_md = _hybrid_extract_range(pdf_path, start_page, end_page)
        else:
            primary_md = ox.extract_pdf_pages(prompt, pdf_path, start_page, end_page)
    except Exception as e:
        print(f"    [!] Error in chunk {chunk_idx}: {e}")
        return None

    # 2차 QA — Claude vision verifier (opt-in; 비활성 시 즉시 1차 그대로 반환).
    if primary_md and vqa.is_enabled():
        if VISION_QA_ENSEMBLE >= 2:
            print(f"    - Vision QA ENSEMBLE n={VISION_QA_ENSEMBLE} "
                  f"({vqa.backend_label()}) on pages {start_page}-{end_page}...",
                  flush=True)
            result = vqa_ensemble.review_ensemble(
                primary_md, pdf_path, start_page, end_page, n=VISION_QA_ENSEMBLE)
        else:
            print(f"    - Vision QA ({vqa.backend_label()}) on pages "
                  f"{start_page}-{end_page}...", flush=True)
            result = vqa.review(primary_md, pdf_path, start_page, end_page)
        if result.corrected:
            print(f"    [+] Vision QA corrected ({result.note})", flush=True)
        else:
            print(f"    [~] Vision QA skipped/degraded ({result.note})", flush=True)
        # net_tracer 교차검증(opt-in) — vision QA 산출 후 결정적 게이트 1단 추가.
        return apply_netcheck(result.markdown, pdf_path, start_page, end_page)
    return primary_md


def _extract_page_with_retries(pdf_path, page, retries):
    """단일 페이지를 최대 (retries+1)회 시도해 추출(M-8 소청크 폴백 단위).

    extract_chunk 를 start==end 로 호출(같은 프롬프트/QA/netcheck 경로 — 출력 동작
    동일). 모두 실패하면 None. ox 내부 429 재시도와 별개의 상위 제한 재시도다.
    """
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        text = extract_chunk(pdf_path, page, page, f"{page}-retry{attempt}")
        if text:
            if attempt > 0:
                print(f"    [+] page {page} recovered on retry {attempt}", flush=True)
            return text
        print(f"    [~] page {page} attempt {attempt + 1}/{attempts} failed", flush=True)
    return None


def extract_chunk_with_page_fallback(pdf_path, start_page, end_page, chunk_idx,
                                     page_retries=None):
    """청크 추출(extract_chunk) — 실패 시 페이지 단위 폴백(M-8).

    1) 우선 청크 전체(start..end) 1회 시도(기존 extract_chunk 경로 그대로).
    2) 실패하면 청크를 **페이지 단위**로 쪼개 각 페이지를 제한 재시도로 재추출한다
       (소청크 폴백). 한 페이지 실패가 청크 전체 손실로 번지지 않게 한다.

    Args:
        pdf_path    : 원본 PDF 경로.
        start_page  : 청크 시작(1-based, inclusive).
        end_page    : 청크 끝(1-based, inclusive).
        chunk_idx   : 로그용 청크 번호.
        page_retries: 페이지별 상위 재시도 횟수(None → EXTRACT_PAGE_RETRIES).

    Returns:
        (text|None, failed_pages:list[int]).
          - text : 청크/폴백 결과 MD. 폴백 시 페이지 MD 를 `\n\n---\n\n` 로 결합하고
                   끝까지 실패한 페이지 위치엔 인라인 MISSING 마커를 둔다(H-5 형식).
          - failed_pages : 끝까지 실패한 페이지 목록(비면 완전 성공).
        모든 페이지가 실패하면 (None, [start..end]) — 호출자가 청크 MISSING 처리.
    """
    pr = EXTRACT_PAGE_RETRIES if page_retries is None else page_retries

    # 1) 청크 전체 1회 시도(단일 페이지 청크면 이게 곧 페이지 시도).
    text = extract_chunk(pdf_path, start_page, end_page, chunk_idx)
    if text:
        # F12b(2026-07-10) 안전 폴백: 멀티페이지 청크 전사물이 거대표 폭주/절단 서명을
        #   보이면(검출기가 놓친 거대 매트릭스 표를 본문 LLM 이 GFM 으로 전사하다 잘림),
        #   그 잘림이 **같은 청크 뒤 페이지를 삼켜(page-loss)** 버린다. 페이지 단위로
        #   재추출해 페이지 간 유실을 차단하고, 잔여 폭주/TRUNCATED 는 _clean_runaway 로
        #   무손실 정리한다. 정상 전사물(폭주/절단 없음)에는 무영향(이 분기 미진입).
        if start_page != end_page and _chunk_shows_truncation(text):
            print(f"    [!] Chunk {chunk_idx} (pages {start_page}-{end_page}) 폭주/절단 "
                  f"서명 감지 → 페이지 단위 재추출(뒤 페이지 유실 방지, F12b)...",
                  flush=True)
            pp_text, pp_failed = _per_page_reextract(pdf_path, start_page, end_page, pr)
            if pp_text is not None:
                return _clean_runaway(pp_text), pp_failed
            # 페이지 재추출이 한 페이지도 못 살린 극단적 경우: 원본 전사물의 폭주만
            # 정리해 반환(page-loss 는 남을 수 있으나 최소한 garbage 는 제거).
            return _clean_runaway(text), []
        return text, []

    # 2) 멀티페이지면 페이지 단위 폴백. 단일 페이지 청크는 상위 재시도만.
    if start_page == end_page:
        print(f"    [!] Chunk {chunk_idx} (page {start_page}) failed — "
              f"retrying page (up to {pr} retries)...", flush=True)
        page_text = _extract_page_with_retries(pdf_path, start_page, pr)
        if page_text:
            return page_text, []
        return None, [start_page]

    print(f"    [!] Chunk {chunk_idx} (pages {start_page}-{end_page}) failed — "
          f"falling back to per-page re-extraction...", flush=True)
    return _per_page_reextract(pdf_path, start_page, end_page, pr)


def _per_page_reextract(pdf_path, start_page, end_page, pr):
    """멀티페이지 청크를 페이지 단위로 재추출해 결합(M-8 소청크 폴백 본체).

    각 페이지를 제한 재시도로 재추출하고, 끝까지 실패한 페이지 위치엔 인라인 MISSING
    마커(H-5 형식)를 둔다. 페이지 MD 는 청크 결합 계약(`\n\n---\n\n`)으로 합친다.

    Returns:
        (text|None, failed_pages). 한 페이지도 못 살리면 (None, [start..end]).
    """
    parts: list[str] = []
    failed_pages: list[int] = []
    any_ok = False
    for page in range(start_page, end_page + 1):
        page_text = _extract_page_with_retries(pdf_path, page, pr)
        if page_text:
            parts.append(page_text)
            any_ok = True
        else:
            parts.append(f"<!-- MISSING page {page}: extraction failed -->")
            failed_pages.append(page)

    if not any_ok:
        # 폴백으로도 한 페이지도 못 살림 → 청크 전체 MISSING 처리(호출자 위임).
        return None, list(range(start_page, end_page + 1))
    # 부분/완전 복구 — 페이지 MD 를 청크 결합 계약(`\n\n---\n\n`)으로 합친다.
    return "\n\n---\n\n".join(parts), failed_pages


def extract_image(image_path):
    print(f"[*] Processing image {image_path.name}...", flush=True)
    try:
        prompt = (
            "You are converting an image to high-quality Markdown.\n\n"
            "Treat this single image as Figure 1. Transcribe ALL visible text verbatim "
            "(invoices, labels, watermarks, units, stamps, signatures).\n"
            "If the image contains tables, render them in GFM pipe format.\n\n"
            + FIGURE_RULES +
            "\nOutput ONLY the Markdown content (no surrounding code fence)."
        )
        primary_md = ox.extract_image(prompt, image_path)
    except Exception as e:
        print(f"    [!] Error in image extraction: {e}")
        return None

    # 2차 QA — 단일 이미지는 원본 파일을 직접 verifier가 Read(렌더 불필요).
    if primary_md and vqa.is_enabled():
        if VISION_QA_ENSEMBLE >= 2:
            print(f"    - Vision QA ENSEMBLE n={VISION_QA_ENSEMBLE} "
                  f"({vqa.backend_label()}) on image {image_path.name}...",
                  flush=True)
            result = vqa_ensemble.review_image_ensemble(
                primary_md, image_path, n=VISION_QA_ENSEMBLE)
        else:
            print(f"    - Vision QA ({vqa.backend_label()}) on image "
                  f"{image_path.name}...", flush=True)
            result = vqa.review_image(primary_md, image_path)
        if result.corrected:
            print(f"    [+] Vision QA corrected ({result.note})", flush=True)
        else:
            print(f"    [~] Vision QA skipped/degraded ({result.note})", flush=True)
        return result.markdown
    return primary_md

def hwp_to_pdf(hwp_path):
    """Converts HWP to PDF via HTML using hwp5html and Chrome Headless."""
    print(f"[*] Converting {hwp_path.name} to PDF via HTML...", flush=True)
    temp_dir = Path(f"input/pdf/temp_hwp_{hwp_path.stem}")
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # L-7: temp_dir 정리를 finally 로 일원화한다. 과거엔 html_file 미발견 early-return
    #      (아래 'No HTML output') 분기에서 temp_dir 를 지우지 않아 임시 디렉터리가 누수됐다.
    #      finally 로 성공/실패/early-return 모든 경로에서 1회 정리를 보장한다.
    try:
        # 1. HWP to HTML
        html_out = temp_dir / "html_output"
        cmd_html = [
            "uvx", "--with", "six", "--with", "lxml", "--from", "pyhwp",
            "hwp5html", "--output", str(html_out), str(hwp_path)
        ]
        subprocess.run(cmd_html, check=True, capture_output=True)

        # Find index file
        html_file = next(html_out.glob("index.xhtml"), next(html_out.glob("index.html"), None))
        if not html_file:
            print(f"[!] No HTML output for {hwp_path.name}")
            return None

        # 2. HTML to PDF via Chrome
        pdf_path = TEMP_PDF_DIR / hwp_path.with_suffix(".pdf").name
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        cmd_pdf = [
            chrome_path, "--headless", "--disable-gpu",
            f"--print-to-pdf={pdf_path}", f"file://{html_file.absolute()}"
        ]
        subprocess.run(cmd_pdf, check=True, capture_output=True)

        return pdf_path if pdf_path.exists() else None
    except Exception as e:
        print(f"[!] HWP to PDF conversion error: {e}")
        return None
    finally:
        # 모든 경로(성공/early-return/예외)에서 HTML 임시 디렉터리 정리 보장.
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception as cleanup_err:  # noqa: BLE001 — 정리 실패가 변환 결과를 덮지 않게
                print(f"[~] temp_dir 정리 실패(무시): {temp_dir} ({cleanup_err})", flush=True)

def process_pdf_auto(pdf_path, total_pages):
    """[VISION_QA_AUTO] PDF 전 페이지를 페이지 단위로 추출 + 자동 티어링 처리.

    각 페이지를 extract_page_auto 로 처리하고, 페이지별 tier/strength 를 누적 집계해
    summary 로 출력한다(어떤 페이지가 어떤 tier 로 처리됐는지 가시화). 페이지 MD 는
    기존 청크 결합 계약(`\\n\\n---\\n\\n`)을 따르는 리스트로 반환한다.

    Returns:
        (page_texts:list[str], failed_pages:list[int]).
    """
    global _AUTO_ENSEMBLE_USED
    _AUTO_ENSEMBLE_USED = 0  # PDF 단위 비용 가드 리셋.

    mode = "DRY-RUN(분류만)" if VISION_QA_AUTO_DRYRUN else "AUTO(페이지별 티어링)"
    print(f"    [*] VISION_QA_AUTO {mode}: {total_pages} pages "
          f"(ensemble budget={VISION_QA_MAX_ENSEMBLE_PAGES}, "
          f"rate_delay={VISION_QA_RATE_DELAY}s)", flush=True)

    page_texts: list[str] = []
    failed_pages: list[int] = []
    tier_records: list[dict] = []

    # M-5: 같은 PDF 를 페이지마다 재오픈/재파싱하지 않도록 fitz 핸들을 **1회만** 열어
    #      1차추출 render + classify_page 벡터 분석에 공유한다(net_tracer 는 별 프로세스).
    #      open 실패 시 doc=None → 각 하위 호출이 기존처럼 내부 open(graceful).
    doc = None
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
    except Exception as e:  # noqa: BLE001 — 핸들 공유 실패는 기존 경로로 degrade
        print(f"    [~] [AUTO] shared fitz handle 미사용(개별 open): {e}", flush=True)
        doc = None

    # M-6: 호출 간 레이트리밋 — 무조건 sleep(10) 제거. base_delay 토큰버킷 +
    #      **마지막 QA 호출 뒤 sleep 생략**. base_delay=0(기본)이면 대기 0.
    limiter = _RateLimiter(base_delay=VISION_QA_RATE_DELAY)

    try:
        for page in range(1, total_pages + 1):
            # M-5: 공유 핸들이 있으면 전달(재오픈 회피). 없으면 기존 2-인자 호출 그대로
            #      — extract_page_auto 를 monkeypatch 한 기존 테스트 계약 보존.
            if doc is not None:
                md, record = extract_page_auto(pdf_path, page, doc=doc)
            else:
                md, record = extract_page_auto(pdf_path, page)
            tier_records.append(record)
            if md:
                page_texts.append(md)
                # W2: rate-limit 은 **실제 vision QA(API) 호출이 발생한** 페이지에서만
                #     의미가 있다(vqa-disabled/dry-run/text/QA예외 페이지는 API 0건).
                # M-6: 방금 QA 호출이 끝난 시각을 기록하고, **다음 페이지가 남아 있을
                #      때만** base_delay 잔여시간을 대기한다. 마지막 페이지(또는 이후
                #      QA 호출이 없을 가능성)에서는 대기를 건너뛴다(불필요한 끝 sleep 제거).
                if record.get("qa_called"):
                    limiter.mark_call_end()
                    if page < total_pages:
                        limiter.wait_before_next()
            else:
                failed_pages.append(page)
                print(f"    [!] [AUTO] page {page} failed, continuing...", flush=True)
                # H-5 대칭화: 청크 경로와 동일하게 실패 페이지 위치에 인라인 MISSING 마커 삽입.
                # 부분본을 열었을 때 어느 페이지가 빠졌는지 사람·파이프라인 모두 식별 가능.
                page_texts.append(
                    f"<!-- MISSING page {page}: extraction failed -->"
                )
    finally:
        # M-5: 공유 핸들 정리(여기서 연 것만 닫음 — 하위 호출은 외부 핸들 미소유).
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass

    # 페이지별 tier 매핑 summary(가시화).
    counts: dict[str, int] = {}
    for r in tier_records:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    print("    " + "=" * 56, flush=True)
    print(f"    [AUTO] tier summary: "
          + " ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    print(f"    [AUTO] ensemble pages used: "
          f"{_AUTO_ENSEMBLE_USED}/{VISION_QA_MAX_ENSEMBLE_PAGES}", flush=True)
    for r in tier_records:
        s = r.get("signals", {})
        print(f"    [AUTO] p{r['page']:>3}: {r['strength']:<24} "
              f"(vec={s.get('vector_lines', '-')} desig={s.get('designators', '-')} "
              f"pin={s.get('pin_rows', '-')})", flush=True)
    print("    " + "=" * 56, flush=True)
    if failed_pages:
        print(f"    [!] [AUTO] {len(failed_pages)} page(s) failed: {failed_pages}",
              flush=True)
    return page_texts, failed_pages


# M-3: Figure 헤딩(`### Figure N: ...`)을 청크/페이지 결합 후 **문서 전역**으로
#       1부터 순차 리넘버한다. 1차 추출 프롬프트(FIGURE_RULES)는 청크/페이지마다
#       `### Figure 1`,`### Figure 2`... 를 1부터 재시작하므로, 결합 결과물에 동일
#       번호가 중복되어 상호참조·인덱싱이 깨진다. 결합 단계에서 한 번 전역 리넘버해
#       유일 번호를 부여한다.
#
# 보존 계약(깨지 말 것):
#   - `### Figure` 가 아닌 헤딩/본문/표/리스트는 무변경.
#   - MISSING/TRUNCATED 주석 마커(`<!-- MISSING ... -->`, `<!-- TRUNCATED ... -->`)는
#     `### ` 로 시작하지 않으므로 정규식에 매칭되지 않아 그대로 보존된다.
#   - 콜론 뒤 제목/공백/대소문자 변형(`### Figure 3:`, `### figure 3 -`, `###  Figure 3`)
#     을 폭넓게 수용하되, 번호만 교체하고 나머지(제목·구분자)는 원문 그대로 둔다.
#   - 코드펜스(```) 내부의 `### Figure N` 유사 텍스트는 헤딩이 아니므로 건드리지 않는다
#     (펜스 토글 추적으로 제외).
#
# 헤딩 인식: 줄 선두 1~6개 `#` + 공백 + 'Figure' + 공백 + 정수. 'Figure' 는
# 대소문자 무시(추출 프롬프트는 'Figure' 대문자 N 형식이나 모델 변형 방어).
_FIGURE_HEADING_RE = re.compile(
    r"^(?P<hashes>\#{1,6})(?P<sp1>[ \t]+)(?P<word>[Ff]igure)(?P<sp2>[ \t]+)(?P<num>\d+)"
)


def renumber_figures(md: str) -> str:
    """결합된 Markdown 의 `### Figure N` 헤딩을 전역 1..K 로 순차 리넘버한다.

    번호만 교체하고 헤딩의 나머지(원래 `#` 개수, 간격, 제목, 콜론/구분자)는 보존한다.
    코드펜스(```/~~~) 내부 라인은 헤딩으로 보지 않는다(전사된 예시 텍스트 보호).

    Args:
        md: 청크/페이지 결합 후 Markdown 전문.

    Returns:
        Figure 번호가 전역 유일(1부터 등장 순)로 치환된 Markdown.
    """
    if not md or "Figure" not in md:
        return md
    counter = 0
    in_fence = False
    fence_token = ""
    out_lines: list[str] = []
    for line in md.splitlines(keepends=True):
        stripped = line.lstrip()
        # 코드펜스 토글 추적(``` 또는 ~~~). 펜스 내부는 헤딩 치환 제외.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            token = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_token = token
            elif token == fence_token:
                in_fence = False
                fence_token = ""
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        m = _FIGURE_HEADING_RE.match(line)
        if m:
            counter += 1
            # 번호(span)만 교체 — 앞/뒤(제목·구분자·줄바꿈) 원문 보존.
            s, e = m.span("num")
            line = line[:s] + str(counter) + line[e:]
        out_lines.append(line)
    return "".join(out_lines)


def _md_is_incomplete(md_path: Path) -> bool:
    """H-5: .md 파일에 불완전 마커(MISSING/TRUNCATED)가 있으면 True 반환.

    재실행 스킵 가드에서 호출한다. 마커가 있는 파일은 완성본이 아니므로 재처리해야 함.
    """
    try:
        content = md_path.read_text(encoding="utf-8")
        return (
            "<!-- MISSING" in content
            or "<!-- TRUNCATED" in content
        )
    except OSError:
        return False


# ── Guard A: 번역 모델 메타응답 유출 차단(2026-07-10) ─────────────────────────
# gemma 번역 모델이 빈/공백 입력에 대해 실제 번역 대신 "please provide the text ..."
# 같은 메타 안내문을 반환해 그대로 본문에 주입되는 사고(LN08LPU p98-102 실측)를 막는다.
# MLX describe 의 degenerate 가드와 동일 철학: 메타응답이면 번역 실패로 간주 → 원문 유지.
# 화이트리스트는 소형 모델의 전형적 거절/요청 구절만 소문자 substring 으로 매칭(정확·보수적),
# 정상 번역문은 이 구절을 담지 않으므로 오탐 0. 추가 안전장치로 '짧은' 응답에만 적용한다.
_TRANSLATE_META_PHRASES = (
    "please provide the markdown",
    "please provide the text",
    "please provide the content",
    "it appears that no content",
    "no content was included",
    "there is no content",
    "there is no text to translate",
    "i need the text",
    "i need the content",
    "i need the markdown",
    "you would like me to translate",
    "you haven't provided",
    "it seems you haven't",
    "no text was provided",
    "provide the content you would like",
)


def _translation_looks_meta(text: str) -> bool:
    """번역 모델 출력이 번역이 아니라 메타응답이면 True(→ 원문 유지)."""
    s = (text or "").strip()
    if not s:
        return False  # 빈 응답은 기존 `if out` 가드가 처리
    # 메타응답은 짧다(한두 문장). 실제 문단 번역과 혼동을 피하려 길이 상한을 둔다.
    if len(s) > 400:
        return False
    low = s.lower()
    return any(p in low for p in _TRANSLATE_META_PHRASES)


def _translate_cjk_paragraphs_to_english(md: str) -> str:
    """MD 본문에서 중국어(CJK 한자) 포함 문단을 gemma 로 영어 번역해 교체(2026-07-07).

    glm-ocr(OCR 소형 모델)이 프롬프트의 '중국어→영어' 지시를 못 따라 중국어를 그대로
    전사하므로, 저장 직전 후처리로 보정한다. 기본 ON(FMDW_CJK_TO_EN=1). 중국어가 없으면
    즉시 원본 반환(회귀 0). 번역 실패/붕괴 문단은 원문 유지(정보 손실 0).
    """
    if os.getenv("FMDW_CJK_TO_EN", "1").strip().lower() in ("0", "false", "no"):
        return md
    if not re.search(r"[一-鿿]", md):
        return md
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return md
    model = os.getenv("FMDW_TRANSLATE_MODEL", "gemma4:31b")
    nump = int(os.getenv("FMDW_TRANSLATE_NUM_PREDICT", "4000"))

    def _looks_bad(t: str) -> bool:
        s = (t or "").strip()
        if not s:
            return True
        c = re.sub(r"\s+", "", s)
        return len(c) >= 15 and len(set(c)) <= 4  # 퇴행 반복(예 '의의의…')

    def _tr(text: str) -> str:
        prompt = (
            "Translate all Chinese text in the following Markdown into natural English, "
            "in place. Keep all non-Chinese text, Markdown structure, and formatting "
            "exactly as-is. Output only the resulting Markdown.\n\n" + text
        )
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
                out = (r.json().get("message", {}).get("content", "") or "").strip()
            except Exception:  # noqa: BLE001
                out = ""
            if out and not _looks_bad(out) and not _translation_looks_meta(out) and not re.search(r"[一-鿿]", out):
                return out
        return ""

    paras = md.split("\n\n")
    changed = False
    for i, p in enumerate(paras):
        # 중국어 우세 문단만 번역: 한자([一-鿿])가 있고 '한글(가-힣)이 없는' 문단만.
        # 한국어 문서의 한자(漢字·電源·法規 등)를 중국어로 오탐해 한글 문단을 변형시키는
        # 회귀를 막는다(Advisor QA 2026-07-07). 순수 중국어 블록(한글 0)만 대상.
        if re.search(r"[一-鿿]", p) and not re.search(r"[가-힣]", p):
            en = _tr(p)
            if en:
                paras[i] = en
                changed = True
    return "\n\n".join(paras) if changed else md


# ── Markdown 스타일 정규화 후처리(FMDW_MD_STYLE, 기본 ON) ────────────────────────
# 로컬 LLM 전사 결과를 저장 직전에 사용자 표준 서식으로 결정론적 정규화한다.
# LLM 준수에 의존하지 않는 '신뢰성 계층'. FMDW_MD_STYLE=0 이면 완전 비활성(회귀 탈출구).
#
# 적용 표준:
#   S1 제목 계층:  `N.N Title`→`## ...`, `N.N.N+`→`### ...`
#                  (챕터 `N Title`→`# ` 승격은 프롬프트 S4 담당 — 결정론 규칙에서는
#                   본문 오탐("3 Phase motors ...") 때문에 제외, Advisor QA 2026-07-09)
#   S2 굵게:       `Table N:` / `Figure N:` 캡션 줄 전체 굵게, `Note:` 접두 `**Note:**`
#   S3 목차 페이지: 점 리더(`....`) 목록 → 2열 GFM 표(`| Section/Table | Page |`, page 우정렬)
#
# 불변 계약(깨지 말 것):
#   - 코드펜스(``` / ~~~) 내부, GFM 표 행(`|` 시작), 이미 헤딩(`#`)/굵게(`**`)인 줄은 무변경.
#   - `### Figure N`(renumber_figures 산출)은 `#` 로 시작 → S1/S2 미적용(중복 방지).
#   - 숫자 리스트(`1. Audience`)는 제목이 아님 → 헤딩 변환 제외.
#   - 보수적: 오탐 헤딩이 미탐보다 나쁨 → 앞뒤 빈 줄 + 짧은 길이 + 문장부호 없음 요구.
_MD_STYLE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# 점 리더 목차 항목: "Title .......... 16" / "Title . . . 16" / "Title...16"
_TOC_ENTRY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<title>\S.*?)\s*(?:\.[ \t]*){3,}(?P<page>\d+)\s*$"
)
_TOC_TITLES = {"table of contents", "list of tables", "list of figures", "contents"}
# S1 제목 정규식(깊은 것 우선). 토큰은 순수 숫자그룹 + 뒤에 공백 필수.
# Advisor QA FIX 1(2026-07-09): 맨숫자 H1(`^\d+ Title`) 결정론 승격은 **제거** —
# "3 Phase motors are common"/"5 Volt supply rail" 류 본문 오탐이 실측 확인됨.
# 챕터 H1 복원은 S4 프롬프트 규칙 + 에이전트 비전 검수로 담당(fmdw 워크플로우 §검증).
_H3_RE = re.compile(r"^(\d+\.\d+(?:\.\d+)+)\s+(\S.*)$")
_H2_RE = re.compile(r"^(\d+\.\d+)\s+(\S.*)$")
# S2 캡션/노트
# F4(2026-07-09): 대소문자 무시 매칭 — glm-ocr 이 캡션을 대문자('FIGURE 17:')로 전사한
# 페이지가 실측됐다. 매칭은 case-insensitive 로, 출력 표기는 title-case('Figure'/'Table')로
# 정규화한다(_apply_caption_bold). Note 도 대소문자 무시.
_CAP_RE = re.compile(r"^(Table|Figure)\s+(\d+):", re.IGNORECASE)
_NOTE_RE = re.compile(r"^Note:\s", re.IGNORECASE)


def _md_style_enabled() -> bool:
    return os.getenv("FMDW_MD_STYLE", "1").strip().lower() not in ("0", "false", "no")


def _md_clean_enabled() -> bool:
    """FMDW_MD_CLEAN(기본 ON): 페이지 내 중복 Figure 캡션 dedup(F3)·도면 라벨 정리(F5)·
    러닝헤더 인접 챕터표제 제거(F2 chapter). 0 이면 이 휴리스틱 정리들을 비활성(회귀 탈출구).
    (러닝헤더 case-insensitive·오버레이 스탬프 제거 같은 순수 노이즈 제거는 상시 ON.)"""
    return os.getenv("FMDW_MD_CLEAN", "1").strip().lower() not in ("0", "false", "no")


# F1(2026-07-09): 대각 추적 워터마크 타임스탬프 스탬프(예 'ml.ko at 2026.03.25 10:09 KST').
# 형태 = `<토큰> at <YYYY.MM.DD> <H:MM[:SS]> <TZ>`. 줄 전체가 이 형태일 때만 매칭(본문 오탐 0).
_OVERLAY_STAMP_RE = re.compile(
    r"^[\w.]+\s+at\s+\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?\s+[A-Za-z]{2,5}$"
)


def _toc_level(indent: str, title: str) -> int:
    """목차 항목 계층 깊이: 섹션번호 깊이(1.2.3→2)와 들여쓰기(2칸=1레벨) 중 큰 값."""
    lvl = 0
    m = re.match(r"(\d+(?:\.\d+)+)", title)
    if m:
        lvl = m.group(1).count(".")
    sp = len(indent.replace("\t", "    "))
    lvl = max(lvl, sp // 2)
    return min(lvl, 6)


def _is_toc_block(lines) -> bool:
    """목차/표목록/그림목록 페이지 판정: 첫 유의미 줄이 목차 제목이거나 리더 항목 ≥5."""
    for l in lines:
        s = l.strip()
        if not s or s.startswith("<!--"):
            continue
        t = re.sub(r"^#+\s*", "", s).strip().lower()
        if t in _TOC_TITLES:
            return True
        break
    return sum(1 for l in lines if _TOC_ENTRY_RE.match(l)) >= 5


def _convert_toc_block(lines) -> str:
    """목차 페이지 블록을 `# <title>` + 2열 GFM 표로 변환(리더 제거, 계층 &emsp;)."""
    # Advisor QA FIX 2(2026-07-09, 멱등 가드): 점-리더 항목이 하나도 없으면 이미
    # 변환된 블록(`# <title>` + GFM 표)이거나 변환 대상이 없는 페이지 — 재변환 시
    # 표 행이 주석으로 흡수되며 순서가 붕괴하므로 원본 그대로 통과시킨다.
    if not any(_TOC_ENTRY_RE.match(l) for l in lines):
        return "\n".join(lines)
    comments = []          # <!-- page N --> 등 주석 마커는 표 위에 보존
    title_text = None
    col0 = "Section"
    rows = []
    for l in lines:
        s = l.strip()
        if not s:
            continue
        if s.startswith("<!--"):
            comments.append(s)
            continue
        m = _TOC_ENTRY_RE.match(l)
        if m:
            lvl = _toc_level(m.group("indent"), m.group("title"))
            cell = ("&emsp;" * lvl) + m.group("title").strip()
            rows.append((cell, m.group("page")))
            continue
        # 제목 후보(목차/표목록/그림목록)
        t = re.sub(r"^#+\s*", "", s).strip()
        tl = t.lower()
        if tl in _TOC_TITLES and title_text is None:
            title_text = t
            if tl == "list of tables":
                col0 = "Table"
            elif tl == "list of figures":
                col0 = "Figure"
            continue
        # 그 외 비항목 줄은 주석처럼 표 위에 보존(정보 손실 0)
        comments.append(s)
    out = list(comments)
    if title_text:
        out.append(f"# {title_text}")
    if rows:
        out.append("")
        out.append(f"| {col0} | Page |")
        out.append("| --- | ---: |")
        for cell, page in rows:
            out.append(f"| {cell} | {page} |")
    return "\n".join(out)


def _canon_caption(st: str) -> str:
    """캡션 줄의 선두 단어를 title-case 로 정규화('FIGURE 17:'→'Figure 17:', 'TABLE'→'Table')."""
    m = _CAP_RE.match(st)
    if not m:
        return st
    word = m.group(1)              # 원본 표기(대문자일 수 있음)
    canon = word.capitalize()     # Figure / Table
    return canon + st[len(word):]  # 뒤(번호·콜론·본문)는 그대로


def _apply_caption_bold(line: str):
    """S2: Table/Figure 캡션 줄 전체 굵게(+대소문자 정규화), Note: 접두 굵게.

    F4(2026-07-09): 'FIGURE 17:'(대문자·미굵게) → '**Figure 17: ...**' 로 정규화.
    이미 굵은('**...**') 캡션도 내부 표기가 대문자면 title-case 로 교정한다. 비대상이면 None.
    """
    st = line.strip()
    if not st:
        return None
    indent = line[: len(line) - len(line.lstrip())]
    if st.startswith("**"):
        # 이미 굵음 — Figure/Table 캡션이고 표기가 비정규(대문자 등)면 case 만 교정.
        inner = st[2:-2] if st.endswith("**") else st[2:]
        inner = inner.strip()
        m = _CAP_RE.match(inner)
        if m and m.group(1) not in ("Figure", "Table"):
            fixed = _canon_caption(inner)
            if st.endswith("**"):
                return f"{indent}**{fixed}**"
        return None  # 굵은 캡션은 재굵게 금지(멱등)
    if _CAP_RE.match(st):
        return f"{indent}**{_canon_caption(st)}**"
    if _NOTE_RE.match(st):
        return f"{indent}**Note:** " + st[len("Note:"):].lstrip()
    return None


def _apply_heading(lines, i):
    """S1: 독립 제목 줄을 `#`/`##`/`###` 로 승격(보수적). 비대상이면 None."""
    line = lines[i]
    st = line.strip()
    if not st or st.startswith("#") or st.startswith("|") or st.startswith("**"):
        return None
    if len(st) > 90:
        return None
    if st[-1] in ".!?,;:":
        return None
    prev_blank = (i == 0) or (lines[i - 1].strip() == "")
    next_blank = (i == len(lines) - 1) or (lines[i + 1].strip() == "")
    if not (prev_blank and next_blank):
        return None

    def _valid_secno(num: str, text: str) -> bool:
        # Advisor QA FIX 1: 버전 문자열 오탐 가드 — "0.9.2.0 Released build" 방지.
        # (a) 세그먼트 5개 이상 금지, (b) 선행 0 세그먼트("0","09" 류) 금지,
        # (c) 제목 텍스트는 소문자 단어로 시작 금지(섹션 제목은 대문자/기호 시작).
        segs = num.split(".")
        if len(segs) > 4:
            return False
        if any(s == "0" or (len(s) > 1 and s[0] == "0") for s in segs):
            return False
        if text[:1].islower():
            return False
        return True

    m = _H3_RE.match(st)
    if m:
        return f"### {m.group(1)} {m.group(2)}" if _valid_secno(m.group(1), m.group(2)) else None
    m = _H2_RE.match(st)
    if m:
        return f"## {m.group(1)} {m.group(2)}" if _valid_secno(m.group(1), m.group(2)) else None
    # 맨숫자 H1 결정론 승격은 제거(Advisor QA FIX 1) — S4 프롬프트가 담당.
    return None


def _style_lines(lines) -> str:
    """비-목차 블록에 S1/S2 적용. 코드펜스·표 행·기존 헤딩은 무변경."""
    res = []
    in_fence = False
    for i, l in enumerate(lines):
        if _MD_STYLE_FENCE_RE.match(l):
            in_fence = not in_fence
            res.append(l)
            continue
        if in_fence or l.strip().startswith("|"):
            res.append(l)
            continue
        nl = _apply_caption_bold(l)
        if nl is not None:
            res.append(nl)
            continue
        nl = _apply_heading(lines, i)
        res.append(nl if nl is not None else l)
    return "\n".join(res)


# ── FIX A: 프롬프트 계약 누출 필터(2026-07-09) ───────────────────────────────────
# LLM 이 fallback/retry 경로에서 전사 계약(TRANSCRIPTION RULES ...) 텍스트를 본문에
# 그대로 복창하는 사고(LN08LPU 도면 밀집 5개 페이지 실측). 계약에서만 나올 수 있는
# 고유 시그니처 문구를 포함하는 블록(문단)만 제거한다 — 정상 본문은 이 문구를 가질 수
# 없으므로 오제거 위험 0. 시그니처가 하나도 없으면 원문 byte-identical 반환(회귀 0).
_PROMPT_LEAK_SIGNATURES = (
    "TRANSCRIPTION RULES",
    "COMPLETE PAGE COVERAGE",
    "NO BLANK SHORTCUTS",
    "NEVER skip a page because it seems unimportant",
    "PAGE ORDER + SEPARATOR",
    "do NOT invent any part numbers",
    "Figures are cropped and described by a separate dedicated step",
)

# 프롬프트 섹션 라벨(전사 프롬프트에서 실제 쓰는 헤더)이 glm-ocr 샘플링 변동으로 본문에
# '단독 줄'로 토해내지는 유출(FIX A2, 2026-07-10). 줄 전체가 라벨과 정확히 일치할 때만
# (콜론 뒤 내용 없음) 결정론 제거. 화이트리스트는 실제 프롬프트 헤더로 좁게 한정 —
# 표 셀/캡션(`Table 1: ...`)/`Note: 내용`/코드펜스 내부/콜론 뒤 내용 있는 정상 줄은
# '정확히 일치'가 아니라 무변경(오제거 0). 대소문자 무시(관측 유출은 대문자이나 방어적).
_PROMPT_LEAK_LINE_LABELS = frozenset({
    "FIGURES:",
    "GENERAL RULES:",
    "TRANSCRIPTION RULES:",
    "TRANSCRIPTION RULES (FOLLOW EXACTLY):",
    "TABLE OF CONTENTS:",
    "TABLE OF CONTENTS PAGES:",
})


# 전사 프롬프트 본문의 '고유·긴' 지시문 문구(소문자). glm-ocr 이 라벨 뒤에 지시문
# 문장을 통째로 붙여 토해내는 유출(예: "TABLE OF CONTENTS pages: When a page region
# shows a left column of short bold ... transcribe it as a GFM table ...")은 A2(줄 전체
# 정확일치)로는 안 걸린다 → 줄에 '포함'되면 삭제(FIX A3). 실제 프롬프트에서 도출한
# 명령형 문구라 정상 datasheet 본문에는 등장하지 않아 오제거 0.
_PROMPT_LEAK_LINE_SIGNATURES = (
    "when a page region shows a left column of short bold",
    "transcribe it as a gfm table",
    "two-column term/description layout",
    "never concatenate a term and its description",
    "reproduce every entry (including",
    "do not convert toc entries into markdown headings",
    "transcribe every text element on the page",
    "prefer transcribing over marking blank",
    "never output the same content twice. transcribe",
    "no blank shortcuts: every",
    "transcribe all visible text, tables, and figures",
)


def _strip_prompt_leak(md: str) -> str:
    """전사 프롬프트 누출 제거(결정론·보수적·멱등).

    (A) 전사 계약 시그니처를 포함한 블록(빈 줄 구분 문단)을 통째로 제거 — 정상 본문엔
        없는 고유 문구라 오제거 0.
    (A2) 프롬프트 섹션 라벨과 '줄 전체가 정확히 일치'하는 단독 줄만 제거 — 콜론 뒤 내용
        있는 정상 줄/캡션/표 셀/코드펜스 내부는 불일치라 보존.
    둘 다 해당 없으면 원문 그대로(byte-identical, 회귀 0).
    """
    if not md:
        return md
    out = md
    # (A) 블록 단위 시그니처 제거.
    if any(sig in out for sig in _PROMPT_LEAK_SIGNATURES):
        blocks = re.split(r"\n[ \t]*\n", out)
        kept = [b for b in blocks if not any(sig in b for sig in _PROMPT_LEAK_SIGNATURES)]
        removed = len(blocks) - len(kept)
        if removed:
            print(f"    [MD-FILTER] 프롬프트 계약 누출 블록 {removed}개 제거(FIX A)", flush=True)
        out = "\n\n".join(kept)
    # (A2) 프롬프트 섹션 라벨 단독 줄 제거(코드펜스 내부 보존).
    lines = out.split("\n")
    new_lines = []
    fence = False
    removed_lines = 0
    for ln in lines:
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            new_lines.append(ln)
            continue
        if (not fence) and s.upper() in _PROMPT_LEAK_LINE_LABELS:
            removed_lines += 1
            continue
        new_lines.append(ln)
    if removed_lines:
        out = re.sub(r"\n{3,}", "\n\n", "\n".join(new_lines))
        print(f"    [MD-FILTER] 프롬프트 섹션 라벨 유출 단독 줄 {removed_lines}개 제거(FIX A2)",
              flush=True)
    # (A3) 프롬프트 지시문 시그니처를 '포함'한 줄 제거(코드펜스/표 셀 보존).
    lines = out.split("\n")
    new_lines = []
    fence = False
    removed_sig = 0
    for ln in lines:
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            new_lines.append(ln)
            continue
        if (not fence) and not s.startswith("|"):
            low = s.lower()
            if any(sig in low for sig in _PROMPT_LEAK_LINE_SIGNATURES):
                removed_sig += 1
                continue
        new_lines.append(ln)
    if removed_sig:
        out = re.sub(r"\n{3,}", "\n\n", "\n".join(new_lines))
        print(f"    [MD-FILTER] 프롬프트 지시문 유출 줄 {removed_sig}개 제거(FIX A3)",
              flush=True)
    # (A4) 프롬프트 섹션 라벨(FIGURES:/TABLES: 등)로 시작 + 콜론 뒤 내용이 문서 내 어떤
    #   캡션(**Figure N: X** / ### Figure N: X / Figure N: X 평문, Table 동일)과 중복이면
    #   그 라벨 줄만 제거(FIX A4 강화 2026-07-10: 4줄 창 폐기→문서 전체, H3/평문 캡션 포함).
    #   glm 이 라벨+캡션내용을 중복 토한 아티팩트. 중복 입증 시에만 삭제(코드펜스/표셀 보존, 오삭제 0).
    _a4_labels = tuple(_PROMPT_LEAK_LINE_LABELS) + ("TABLES:",)
    _a4_cap_re = re.compile(
        r"^\s*(?:#{1,6}\s*)?\**\s*(?:figure|table)\s+[\w.]+\s*[:.]\s*(.+)$", re.I)

    def _a4_norm(t):
        t = t.lower()
        t = re.sub(r"[*`_#>]", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    a4_lines = out.split("\n")
    a4_n = len(a4_lines)
    # 1) 문서 전체 캡션 X 정규화 수집(코드펜스/표셀 제외).
    _cap_norms = []
    _cf = False
    for ln in a4_lines:
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            _cf = not _cf
            continue
        if _cf or s.startswith("|"):
            continue
        m = _a4_cap_re.match(s)
        if m:
            cx = _a4_norm(m.group(1))
            if len(cx) >= 3:
                _cap_norms.append(cx)
    # 2) 라벨 접두 줄의 콜론 뒤 내용이 문서 내 어떤 캡션 X 와 중복이면 제거
    #    (인접 4줄 제한 폐기 — 라벨 접두 자체가 유출 신호 + 캡션 중복 입증 → 위치 무관).
    a4_keep = [True] * a4_n
    a4_fence = False
    removed_cap = 0
    for i, ln in enumerate(a4_lines):
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            a4_fence = not a4_fence
            continue
        if a4_fence or s.startswith("|"):
            continue
        up = s.upper()
        lab = next((L for L in _a4_labels if up.startswith(L)), None)
        if not lab:
            continue
        content = _a4_norm(s[len(lab):])
        if len(content) < 3:
            continue
        if any(content in cx or cx in content for cx in _cap_norms):
            a4_keep[i] = False
            removed_cap += 1
    if removed_cap:
        out = re.sub(r"\n{3,}", "\n\n",
                     "\n".join(l for k, l in zip(a4_keep, a4_lines) if k))
        print(f"    [MD-FILTER] 프롬프트 라벨+캡션중복 유출 줄 {removed_cap}개 제거(FIX A4)",
              flush=True)
    return out


# ── FIX B: 중복 전사 collapse + 러닝헤더 제거(2026-07-09) ─────────────────────────
# 도면이 텍스트 앞에 오는 페이지에서 같은 내용이 2회 전사되고, 그 사이에 문서 워터마크/
# 러닝헤더("Samsung Confidential" / "LN08LPU_Design Manual_...")가 삽입돼 재시작되는
# 사고(실측). 보수적으로: (1) 문서 중간(맨 앞/맨 뒤 아님)에 낀 러닝헤더 줄만 제거,
# (2) 정규화(공백 압축) 후 '완전히 동일'한 연속 ≥2 문단 시퀀스가 바로 반복되면 뒤 사본만
# 제거. 유사(비동일) 문단은 절대 건드리지 않는다(fuzzy 금지).
_RUNNING_HEADER_DEFAULT_PATTERNS = (
    r"Samsung Confidential",
    r"LN08LPU_Design Manual[\w.\-]*",
    # TASK2(2026-07-09): 하단 푸터 브랜드 — 정확히 이 줄만(fullmatch). 'SAMSUNG ELECTRONICS
    # RESERVES THE RIGHT...' 같은 본문·'© Samsung Electronics Co...' 는 fullmatch 안 됨(보존).
    r"SAMSUNG ELECTRONICS",
)

#: TASK2: 단독 페이지 번호(footer) — 러닝헤더/푸터 라인과 인접할 때만 제거(보수).
_BARE_PAGENUM_RE = re.compile(r"[0-9ivxlcIVXLC]{1,4}")


def _running_header_patterns():
    """러닝헤더 정규식 목록(줄 전체 fullmatch용). env 로 추가 가능(`|||` 구분).

    F2(2026-07-09): case-insensitive 매칭 — glm-ocr 이 워터마크/헤더를 전대문자
    ('SAMSUNG Confidential')로 전사한 페이지가 실측됐다. fullmatch 라 본문 오탐 없음.
    """
    pats = list(_RUNNING_HEADER_DEFAULT_PATTERNS)
    env = os.getenv("FMDW_RUNNING_HEADER_PATTERNS", "").strip()
    if env:
        pats += [p for p in env.split("|||") if p.strip()]
    return [re.compile(r"[ \t>*_#-]*" + p + r"[ \t]*", re.IGNORECASE) for p in pats]


def _norm_block(b: str) -> str:
    return re.sub(r"\s+", " ", b.strip())


def _drop_adjacent_dup_runs(blocks):
    """연속으로 '완전히 동일'하게 반복되는 ≥2 문단 런의 뒤 사본만 제거(첫 사본 유지).

    정규화(공백 압축)한 값이 정확히 일치할 때만 제거 — 유사/fuzzy 는 절대 제거 안 함.
    """
    n = len(blocks)
    if n < 4:
        return blocks
    norm = [_norm_block(b) for b in blocks]
    keep = [True] * n
    for L in range(n // 2, 1, -1):  # 긴 런 우선
        i = 0
        while i + 2 * L <= n:
            if (
                all(keep[i + j] for j in range(2 * L))
                and all(norm[i + j] for j in range(L))  # 빈 블록은 대상 제외
                and norm[i : i + L] == norm[i + L : i + 2 * L]
            ):
                for j in range(L):
                    keep[i + L + j] = False
                i += 2 * L
            else:
                i += 1
    return [b for b, k in zip(blocks, keep) if k]


# F2(2026-07-09): 챕터 러닝헤더('N Chapter Title', 예 '4 Physical Design Rules').
# 진짜 챕터 표제와 구분 불가하므로, '러닝헤더(Samsung Confidential 등) 라인과 바로 인접'
# 할 때만 러닝헤더로 간주해 제거한다. 단독 챕터 표제는 절대 건드리지 않는다.
_CHAPTER_HDR_RE = re.compile(r"^\d+\s+[A-Z].+$")


def _drop_reprinted_body_run(blocks):
    """glm 이 한 페이지 본문을 통째로 2회 전사한 '재인쇄(reprint)' 사고 제거(2026-07-10).

    _drop_adjacent_dup_runs 는 두 사본이 '정확히 인접·동일 길이'일 때만 잡는다. glm 재전사는
    한 사본에 여분 라인·그림 캡션이 끼어 인접·동일길이가 깨진다. 여기서는 '뒤쪽 사본의 연속
    콘텐츠 블록이 각각 앞쪽에 정확(정규화) 쌍둥이를 가지며 같은 순서로 나타나는' 최대 run 을
    찾아 뒤 사본만 제거(첫 사본 유지). 매우 보수적: 대상=산문/헤딩/캡션(표|·이미지![·마커<!--·
    구분선---·빈줄 제외), run>=3블록 且 총>=400자, 쌍둥이는 <=MAX_SPAN 콘텐츠 위치 앞쪽,
    fuzzy 금지(정규화 후 완전동일만). 무변경 시 입력 그대로(회귀 0). 실측 9개 기존 md no-op."""
    from collections import defaultdict

    MIN_RUN, MIN_CHARS, MAX_SPAN = 3, 400, 60

    def _norm(b):
        return re.sub(r"\s+", " ", b.strip())

    def _is_content(b):
        s = b.strip()
        if not s or s.startswith("![") or s.startswith("<!--") or s.startswith("|"):
            return False
        if re.fullmatch(r"-{3,}", s):
            return False
        return True

    n = len(blocks)
    norm = [_norm(b) for b in blocks]
    cpos = [i for i in range(n) if _is_content(blocks[i])]
    cnorm = [norm[i] for i in cpos]
    K = len(cnorm)
    if K < 2 * MIN_RUN:
        return blocks
    where = defaultdict(list)
    for k, s in enumerate(cnorm):
        where[s].append(k)
    best = None
    for start in range(K):
        run_len, prev_twin, total, k = 0, -1, 0, start
        while k < K:
            s = cnorm[k]
            twin = -1
            for q in where[s]:
                if q < start and q > prev_twin and (start - q) <= MAX_SPAN:
                    twin = q
                    break
            if twin < 0:
                break
            prev_twin, k = twin, k + 1
            total += len(s)
            run_len += 1
        if run_len >= MIN_RUN and total >= MIN_CHARS and (best is None or total > best[2]):
            best = (start, run_len, total)
    if best is None:
        return blocks
    start, run_len, _t = best
    drop_orig = set(cpos[start:start + run_len])
    print(f"    [MD-DEDUP] 재인쇄 본문 run {run_len}블록({_t}자) 제거(reprint)", flush=True)
    # 하드닝(2026-07-10, Advisor QA Minor #2): 삭제는 복구 불가이므로, 삭제 지점에
    # 감사 마커(coverage-low 마커 관례와 정합, HTML 주석 — md 렌더 무영향)를 남긴다.
    # 삭제 블록이 여러 개여도 마커는 첫 삭제 위치에 1개만 삽입.
    marker = f"<!-- fmdw:reprint-dropped {run_len} blocks -->"
    out, marked = [], False
    for i, b in enumerate(blocks):
        if i in drop_orig:
            if not marked:
                out.append(marker)
                marked = True
            continue
        out.append(b)
    return out


def _collapse_duplicate_transcription(md: str) -> str:
    """러닝헤더/워터마크 스탬프(중간) 제거 + 연속 완전중복 문단 런 collapse.

    제거 대상(모두 '문서 중간'=맨 앞/맨 뒤 아님 에서만):
      - 러닝헤더/워터마크(_running_header_patterns, case-insensitive) — F2.
      - 대각 추적 워터마크 타임스탬프(_OVERLAY_STAMP_RE) — F1.
      - 챕터 러닝헤더('N Title', `#` 미접두)로서 인접 nonblank 라인이 러닝헤더인 경우 — F2.
        (단독 챕터 표제는 인접 러닝헤더가 없어 보존됨. FMDW_MD_CLEAN=0 이면 이 규칙 비활성.)
    무변경 시 원문 그대로 반환(회귀 0).
    """
    if not md:
        return md
    lines = md.split("\n")
    hpats = _running_header_patterns()
    clean = _md_clean_enabled()

    def _is_hdr(s: str) -> bool:
        return bool(s) and any(p.fullmatch(s) for p in hpats)

    nonblank = [idx for idx, l in enumerate(lines) if l.strip()]
    removed_hdr = 0
    if nonblank:
        first_c, last_c = nonblank[0], nonblank[-1]
        drop = set()
        for pos, idx in enumerate(nonblank):
            s = lines[idx].strip()
            # 순수 러닝헤더/워터마크/스탬프는 위치 무관 제거(이들은 정당한 본문이 될 수 없음).
            if _is_hdr(s) or _OVERLAY_STAMP_RE.match(s):
                drop.add(idx)
                continue
            # 챕터 러닝헤더 제거는 '문서 중간'에서만(맨 앞/맨 뒤 = 진짜 챕터 표제일 수 있어 보존).
            if (clean and first_c < idx < last_c
                    and _CHAPTER_HDR_RE.match(s) and not lines[idx].lstrip().startswith("#")):
                prev_s = lines[nonblank[pos - 1]].strip() if pos > 0 else ""
                next_s = lines[nonblank[pos + 1]].strip() if pos < len(nonblank) - 1 else ""
                # 인접(±1 nonblank)이 '러닝헤더 라인'일 때만 제거 — 챕터표제 옆 러닝헤더는
                # 챕터 라인(러닝헤더 아님)이므로, 진짜 본문 챕터표제는 보존된다.
                if _is_hdr(prev_s) or _is_hdr(next_s):
                    drop.add(idx)
            # TASK2(2026-07-09): 단독 페이지 번호(footer) — 러닝헤더/푸터 라인과 바로 인접할
            # 때만 제거. 본문 속 숫자(표·수치)는 인접 러닝헤더가 없어 보존된다(보수).
            elif (clean and first_c < idx < last_c and _BARE_PAGENUM_RE.fullmatch(s)):
                prev_s = lines[nonblank[pos - 1]].strip() if pos > 0 else ""
                next_s = lines[nonblank[pos + 1]].strip() if pos < len(nonblank) - 1 else ""
                if _is_hdr(prev_s) or _is_hdr(next_s):
                    drop.add(idx)
        removed_hdr = len(drop)
        md2 = "\n".join(l for i, l in enumerate(lines) if i not in drop)
    else:
        md2 = md
    blocks = re.split(r"\n[ \t]*\n", md2)
    deduped = _drop_adjacent_dup_runs(blocks)
    deduped = _drop_reprinted_body_run(deduped)
    dropped = len(blocks) - len(deduped)
    if removed_hdr or dropped:
        print(
            f"    [MD-DEDUP] 러닝헤더 {removed_hdr}줄 제거, 중복 문단블록 "
            f"{dropped}개 collapse(FIX B)",
            flush=True,
        )
        return "\n\n".join(b.strip("\n") for b in deduped)
    return md


# ── F3+F5: 페이지 내 중복 Figure 캡션 dedup + 도면 라벨 정리(2026-07-09) ────────────
# F3: 같은 페이지에서 동일 Figure 번호 캡션이 2회 이상(예 'FIGURE 17:'(상단 대문자) +
#     '**Figure 17:**'(하단 굵게)) 나오면 '가장 잘 갖춰진' 1개만 남긴다(굵게·title-case 선호).
# F5(보수): 페이지의 '캡션 아닌 본문 라인'이 전부 같은 페이지 캡션의 정확한 조각(쉼표/세미콜론
#     분할)일 때만 = '순수 도면 라벨 페이지' → 그 라벨 라인들을 제거. 하나라도 조각이 아니면
#     그 페이지는 손대지 않는다(실제 본문 오탐 0). FMDW_MD_CLEAN=0 이면 F3/F5 모두 비활성.
# 주입 섹션(### Figure / ![...])은 이 단계(저장 전) 이후에 추가되므로 여기서 보이지 않는다.
# `### Figure`·`![Figure`·`| ... |` 표행은 캡션 정규식(선두 `#`/`!`/`|` 불일치)에 안 걸린다.
_FIGCAP_RE = re.compile(r"^\*{0,2}\s*figure\s+(\d+)\s*:", re.IGNORECASE)
_PAGE_MARKER_RE = re.compile(r"^<!-- page \d+ -->\s*$")


def _caption_score(line: str) -> int:
    """캡션 라인 '갖춰짐' 점수: 굵게(+2) + title-case 'Figure'(+1). 높을수록 선호."""
    st = line.strip()
    score = 2 if st.startswith("**") else 0
    m = re.match(r"^\*{0,2}\s*(figure)\b", st, re.IGNORECASE)
    if m and m.group(1) == "Figure":
        score += 1
    return score


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().strip("*").strip()).lower()


def _caption_body_text(line: str) -> str:
    """'**Figure 17: A, B**' → 'A, B' (선두 굵게·'Figure N:'·후미 굵게 제거)."""
    st = line.strip()
    after = re.sub(r"^\*{0,2}\s*figure\s+\d+\s*:\s*", "", st, flags=re.IGNORECASE)
    return after.rstrip("*").strip()


def _is_structural_line(s: str) -> bool:
    """구조/분리 라인(수평선 `---`, 페이지 마커·HTML 주석) — F5 순수판정에서 제외."""
    return bool(re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s)) or (
        s.startswith("<!--") and s.endswith("-->"))


def _drop_figure_label_lines(seg: list) -> list:
    """F5: 순수 도면 라벨 페이지에서 캡션 조각과 정확히 일치하는 라벨 라인 제거.

    '순수 라벨 페이지' = 캡션·구조라인(`---`/주석) 을 제외한 모든 본문 라인이, 같은
    페이지 캡션의 정확한 조각(쉼표/세미콜론 분할)일 때. 하이브리드 전사는 페이지를
    `---` 로 구분하므로 세그먼트 말미의 `---` 를 구조라인으로 제외해야 F5 가 발동한다
    (2026-07-09 실측 수정). 표행(`| ... |`)·산문은 조각이 아니므로 페이지가 impure →
    F5 미발동(오탐 0).
    """
    caps = [_caption_body_text(l) for l in seg if _FIGCAP_RE.match(l.strip())]
    if not caps:
        return seg
    seg_set = set()
    for c in caps:
        seg_set.add(_norm_label(c))
        for piece in re.split(r"[,;]", c):
            p = _norm_label(piece)
            if p:
                seg_set.add(p)
    body = [(i, l.strip()) for i, l in enumerate(seg)
            if l.strip() and not _FIGCAP_RE.match(l.strip())
            and not _is_structural_line(l.strip())]
    if not body:
        return seg
    # 순수 라벨 페이지 판정: 캡션 아닌 모든 본문 라인이 캡션 조각과 정확히 일치할 때만.
    if all(_norm_label(s) in seg_set for _, s in body):
        drop = {i for i, _ in body}
        return [l for i, l in enumerate(seg) if i not in drop]
    return seg


def _dedup_page_captions(seg: list) -> list:
    """한 페이지 세그먼트: F3 중복 캡션 collapse → F5 라벨 정리."""
    by_num = {}
    for i, l in enumerate(seg):
        m = _FIGCAP_RE.match(l.strip())
        if m:
            by_num.setdefault(m.group(1), []).append(i)
    drop = set()
    for num, idxs in by_num.items():
        if len(idxs) > 1:
            best = max(idxs, key=lambda i: (_caption_score(seg[i]), -i))
            drop.update(i for i in idxs if i != best)
    seg2 = [l for i, l in enumerate(seg) if i not in drop]
    return _drop_figure_label_lines(seg2)


def _dedup_figure_captions(md: str) -> str:
    """F3+F5 를 페이지(`<!-- page N -->`) 단위로 적용. 무변경 시 원문 그대로 반환."""
    if not md or not _md_clean_enabled():
        return md
    lines = md.split("\n")
    segments = []          # (marker|None, [lines])
    cur_marker = None
    cur = []
    for l in lines:
        if _PAGE_MARKER_RE.match(l):
            segments.append((cur_marker, cur))
            cur_marker, cur = l, []
        else:
            cur.append(l)
    segments.append((cur_marker, cur))
    out = []
    changed = False
    for marker, seg in segments:
        if marker is not None:
            out.append(marker)
        new_seg = _dedup_page_captions(seg)
        if new_seg != seg:
            changed = True
        out.extend(new_seg)
    if changed:
        print("    [MD-FIGCAP] 페이지 내 중복 Figure 캡션/도면 라벨 정리(F3/F5)", flush=True)
        return "\n".join(out)
    return md


# ── F8: 폰트 크기 기반 헤딩 매핑(FMDW_FONT_HEADINGS, 기본 ON) ─────────────────────
# MD_STYLE(S1)는 '번호 있는' 제목(N.N Title)만 헤딩화한다. 이 F8 은 PDF 벡터 텍스트의
# '폰트 크기'로 번호 없는 대형 제목(LN08LPU 48pt / Design Manual 24pt / Important
# Notice 18pt / Table of Contents 20pt 등)을 헤딩으로 승격해 S1 을 보완한다.
# 원리: 본문 크기(최빈 size)보다 확실히 큰 크기 계층을 내림차순으로 버킷(±2pt)해
# 상위 4개를 #/##/###/#### 로 매핑. 대각(F1)·러닝헤더/푸터(F2) 라인은 매핑 전에
# 제외해 'Samsung Confidential'(14pt) 등이 절대 헤딩이 되지 않게 한다.
def _font_headings_enabled() -> bool:
    return os.getenv("FMDW_FONT_HEADINGS", "1").strip().lower() not in ("0", "false", "no")


def _font_map_from_dicts(dicts):
    """get_text('dict') 결과 리스트에서 (fmap {정규화 제목→레벨}, size_to_level) 생성.

    대각(F1)·러닝헤더/푸터(F2)·스탬프 라인 제외 → 본문 최빈 크기 → 그보다 확실히 큰
    (×1.12) 크기 계층을 ±2pt 버킷 → 상위 4버킷을 #..#### 매핑 → 짧고 제목다운 후보만 채택.
    size_to_level 은 F10(누락 헤딩 복구)이 라인 크기→레벨 조회에 재사용한다.
    """
    from collections import Counter

    hpats = _running_header_patterns()
    sizes = []
    cand = []
    for d in dicts:
        for blk in d.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            for ln in blk.get("lines", []):
                if _is_diagonal_dir(ln.get("dir")):
                    continue  # F1: 대각 워터마크 제외
                t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if not t:
                    continue
                if any(p.fullmatch(t) for p in hpats):
                    continue  # F2: 러닝헤더/푸터 제외
                if _OVERLAY_STAMP_RE.match(t):
                    continue
                sz = round(max((s.get("size", 0) for s in ln.get("spans", [])),
                               default=0), 1)
                if len(t) >= 3:
                    sizes.append(sz)
                if (len(t) <= 80 and not re.search(r"[.:;,]$", t)
                        and not t.startswith("|") and not re.match(r"^[-*+•]\s", t)):
                    cand.append((sz, _norm_label(t), t))
    if not sizes:
        return {}, {}
    body = Counter(sizes).most_common(1)[0][0]  # 본문 크기 = 최빈
    thresh = body * 1.12
    hsizes = sorted({s for s in sizes if s > thresh}
                    | {s for (s, _, _) in cand if s > thresh}, reverse=True)
    if not hsizes:
        return {}, {}
    buckets = []  # ±2pt 인접 크기를 하나의 버킷으로(내림차순 체인)
    for s in hsizes:
        if buckets and abs(buckets[-1][-1] - s) <= 2.0:
            buckets[-1].append(s)
        else:
            buckets.append([s])
    size_to_level = {}
    for lvl, bk in enumerate(buckets[:4], start=1):
        for s in bk:
            size_to_level[s] = lvl
    for bk in buckets[4:]:  # 5번째 이하 크기는 최심(4)로
        for s in bk:
            size_to_level[s] = 4
    fmap = {}
    for (s, norm, _raw) in cand:
        lvl = size_to_level.get(s)
        if lvl and norm and norm not in fmap:
            fmap[norm] = lvl
    return fmap, size_to_level


# ── F11: 볼드 무번호 소제목 승격(FMDW_FONT_HEADINGS 게이트, F8 보완) — 2026-07-11 ──────
# 실측(LN08LPU p170-173): 'Diamond Necklace Structures'·'Non-Diamond Structures'·
# 'Example 1' 은 본문과 동일 10pt 이지만 Helvetica-Bold(flag&16) 인 무번호 소제목이다.
# F8 은 '본문보다 큰 폰트'만 승격하므로 이런 본문 크기 볼드 소제목을 놓친다. F11 은
# PDF span 의 볼드 속성으로 '단일 라인 블록·짧음·문장부호로 안 끝남·label:value/주소/
# 연락처/캡션/러닝헤더 아님' 후보만 골라 `### ` 로 승격한다(오탐 0 우선, 좁은 게이트).
def _bold_subheading_labels(dicts) -> dict:
    """get_text('dict') 목록에서 '무번호 볼드 소제목' 후보의 {정규화 라벨→True} 맵.

    후보 판정(전부 충족, 매우 보수적):
      - 블록에 유효 라인이 정확히 1개(단일 라인 블록) — 문단 내 인라인 볼드 배제.
      - 그 라인의 (텍스트 있는) 모든 span 이 볼드(flag&16 또는 font 이름에 'bold').
      - 크기 ≤ 본문 최빈크기 ×1.12 — 그보다 큰 대형 제목은 F8/F10 담당(중복 회피).
      - 2~60자, ≤6 단어, 문장부호(. : , ;)로 안 끝남.
      - 콜론/쉼표/@/http 미포함 → 'Contact Us:'·'TEL:'·'Version:'·주소('...,...') 배제.
      - 번호 헤딩(`N`/`N.N ...`) 아님(MD_STYLE 담당), 순수 숫자/로마자 아님(페이지번호).
      - Figure|Table|Note 접두 아님, 러닝헤더/워터마크(_running_header_patterns) 아님.
    무후보 시 빈 dict. (승격은 _promote_bold_subheadings 가 본문 매칭 시에만 수행.)
    """
    from collections import Counter

    hpats = _running_header_patterns()
    sizes = []
    lines_info = []  # (size, text)
    for d in dicts:
        for blk in d.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            valid = []
            for ln in blk.get("lines", []):
                spans = ln.get("spans", [])
                t = "".join(s.get("text", "") for s in spans).strip()
                if t:
                    valid.append((ln, spans, t))
            for (_ln, _spans, t) in valid:
                sz = round(max((s.get("size", 0) for s in _spans), default=0), 1)
                if len(t) >= 3:
                    sizes.append(sz)
            if len(valid) != 1:
                continue  # 단일 라인 블록만(문단 내 볼드 배제)
            ln, spans, t = valid[0]
            allbold = spans and all(
                ((s.get("flags") or 0) & 16) or ("bold" in (s.get("font") or "").lower())
                for s in spans if s.get("text", "").strip())
            if not allbold:
                continue
            sz = round(max((s.get("size", 0) for s in spans), default=0), 1)
            lines_info.append((sz, t))
    if not sizes:
        return {}
    body = Counter(sizes).most_common(1)[0][0]
    thr = body * 1.12
    labels = {}
    for (sz, t) in lines_info:
        if sz > thr:
            continue  # 대형 제목 = F8/F10
        if not (2 <= len(t) <= 60) or len(t.split()) > 6:
            continue
        if re.search(r"[.:,;]$", t):
            continue
        if (":" in t) or ("," in t) or ("@" in t) or ("http" in t.lower()):
            continue  # label:value / 주소 / 연락처 배제
        if re.fullmatch(r"[0-9ivxlcIVXLC]{1,6}", t):
            continue  # 페이지번호
        if re.match(r"^\d+(\.\d+)*(\s|$)", t):
            continue  # 번호 헤딩(MD_STYLE)
        if re.match(r"^(figure|table|note)\b", t, re.IGNORECASE):
            continue
        if any(p.fullmatch(t) for p in hpats):
            continue  # 러닝헤더/워터마크
        labels[_norm_label(t)] = True
    return labels


def _promote_bold_subheadings(md: str, bold_labels: dict) -> str:
    """_bold_subheading_labels 에 매칭되는 '독립 문단 평문 라인'만 `### ` 로 승격(F11).

    승격 조건(오탐 0): 라인이 이미 헤딩(`#`)·표(`|`)·이미지(`![`)·굵게(`**`)·인용(`>`)·
    리스트·주석(`<!--`)·구분선 아님 且 정규화 라벨이 bold_labels 에 있음 且 '독립 문단'
    (직전·직후 라인이 빈 줄/페이지 마커/구분선). 코드펜스 내부 무변경. 매칭 없으면 원문 그대로.
    본문 문단 오승격 금지: 문단 중간(직전·직후가 본문)인 동일 문구는 건드리지 않는다.
    """
    if not md or not bold_labels or not _font_headings_enabled():
        return md
    lines = md.split("\n")
    n = len(lines)

    def _boundary(x: str) -> bool:
        return x == "" or x == "---" or x.startswith("<!--")

    out = []
    in_fence = False
    changed = 0
    for i, l in enumerate(lines):
        if _MD_STYLE_FENCE_RE.match(l):
            in_fence = not in_fence
            out.append(l)
            continue
        s = l.strip()
        if (in_fence or not s or s.startswith("#") or s.startswith("|")
                or s.startswith("![") or s.startswith("**") or s.startswith(">")
                or s.startswith("<!--") or s == "---"
                or re.match(r"^([-*+]\s|\d+\.\s)", s)):
            out.append(l)
            continue
        if _norm_label(s) not in bold_labels:
            out.append(l)
            continue
        prev_l = lines[i - 1].strip() if i > 0 else ""
        next_l = lines[i + 1].strip() if i + 1 < n else ""
        if not (_boundary(prev_l) and _boundary(next_l)):
            out.append(l)  # 문단 중간 → 보존(보수)
            continue
        indent = l[: len(l) - len(l.lstrip())]
        out.append(f"{indent}### {s}")
        changed += 1
    if changed:
        print(f"    [MD-FONT] 볼드 무번호 소제목 {changed}개 ### 승격(F11)", flush=True)
        return "\n".join(out)
    return md


# ── TASK3: 위치 기반 footer 페이지번호 유출 제거(FMDW_MD_CLEAN 게이트) — 2026-07-11 ────
# 실측(LN08LPU p170-173): 본문 말미에 마지막 페이지 footer 번호('173')만 남는 사고. 기존
# TASK2 는 '러닝헤더 인접'을 요구해 본문 끝(인접 헤더 없음) 번호를 못 걸렀다. TASK3 은 PDF
# 하단 footer 영역(y0≥0.90·순수 숫자/로마자)에서 실제로 나온 번호 집합만 대상으로,
# md 의 '페이지 경계(다음 nonblank=페이지마커/---/마커 또는 본문 마지막) 순수번호 줄'을
# 제거한다. 위치+값 이중 게이트라 본문 숫자·표 셀은 절대 지우지 않는다(오탐 0).
def _strip_footer_pagenums(md: str, footer_nums) -> str:
    """PDF footer 영역에서 온 순수 페이지번호 줄을 페이지 경계에서만 제거(TASK3).

    보수 게이트(전부 충족): 줄 전체가 순수 숫자/로마자 且 그 값이 footer_nums(PDF 하단
    footer 영역 실측값)에 속함 且 직전·직후 nonblank 가 표행(`|`) 아님 且 '페이지 경계'
    (직후 nonblank 가 `<!-- page`/`---`/`<!--` 마커이거나 본문 마지막 nonblank). 코드펜스
    내부 무변경. footer_nums 빈 집합/무매칭 시 원문 그대로(회귀 0)."""
    if not md or not footer_nums or not _md_clean_enabled():
        return md
    lines = md.split("\n")
    n = len(lines)
    in_fence = False
    drop = set()
    for i, l in enumerate(lines):
        if _MD_STYLE_FENCE_RE.match(l):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        s = l.strip()
        if s not in footer_nums:
            continue
        if not re.fullmatch(r"[0-9ivxlcIVXLC]{1,6}", s):
            continue
        prev_nb = next((lines[k].strip() for k in range(i - 1, -1, -1)
                        if lines[k].strip()), "")
        next_nb = next((lines[k].strip() for k in range(i + 1, n)
                        if lines[k].strip()), "")
        if prev_nb.startswith("|") or next_nb.startswith("|"):
            continue  # 표 인접 숫자 보호
        at_boundary = (next_nb == "" or next_nb.startswith("<!--")
                       or next_nb == "---")
        if at_boundary:
            drop.add(i)
    if not drop:
        return md
    print(f"    [MD-DEDUP] footer 페이지번호 {len(drop)}줄 제거(TASK3, position)", flush=True)
    return "\n".join(l for i, l in enumerate(lines) if i not in drop)


def _page_lines_with_size(d, ph: float, pw: float):
    """`_twocol_lines_from_dict` 와 동일한 F1/F2 정제 + 라인당 최대 span 크기를 포함한
    6-튜플 [x0,y0,x1,y1,text,size] 목록(읽기순). F10 폰트 헤딩 복구용."""
    hpats = _running_header_patterns()
    drop_rot = _rotated_watermark_drop_enabled()
    out = []
    for blk in d.get("blocks", []):
        if blk.get("type", 0) != 0:
            continue
        for ln in blk.get("lines", []):
            if drop_rot and _is_diagonal_dir(ln.get("dir")):
                continue
            t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            if not t:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            if any(p.fullmatch(t) for p in hpats):
                continue
            if _OVERLAY_STAMP_RE.match(t):
                continue
            if y1 <= 0.11 * ph and x0 > 0.5 * pw:
                continue  # 우상단 코너 러닝 헤더
            if y0 >= 0.90 * ph and re.fullmatch(r"[0-9ivxlcIVXLC]{1,6}", t):
                continue  # 하단 페이지 번호
            sz = round(max((s.get("size", 0) for s in ln.get("spans", [])), default=0), 1)
            out.append([x0, y0, x1, y1, t, sz])
    out.sort(key=lambda l: (round(l[1], 1), l[0]))
    return out


def _font_heading_map(pdf_path):
    """PDF 에서 (fmap, size_to_level, pages_lines, footer_nums, bold_labels) 생성.

    실패/비PDF 시 ({}, {}, [], set(), {}).
    pages_lines[i] = i+1 페이지의 정제 6-튜플 라인(_page_lines_with_size). F10 재사용.
    footer_nums = PDF 하단 footer 영역(y0≥0.90·순수 숫자/로마자)에서 실측한 페이지번호
    집합(TASK3 위치 기반 유출 제거용). bold_labels = 볼드 무번호 소제목 후보 라벨(F11).
    footer/bold 는 FMDW_FONT_HEADINGS 와 무관하게 항상 수집(각자 apply 단계에서 게이트).
    """
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        return {}, {}, [], set(), {}
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return {}, {}, [], set(), {}
    fh_on = _font_headings_enabled()
    dicts = []
    pages_lines = []
    footer_nums = set()
    try:
        for page in doc:
            try:
                d = page.get_text("dict")
            except Exception:  # noqa: BLE001
                dicts.append({})
                pages_lines.append([])
                continue
            dicts.append(d)
            ph = page.rect.height or 1.0
            pages_lines.append(
                _page_lines_with_size(d, ph, page.rect.width or 1.0))
            for blk in d.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                for ln in blk.get("lines", []):
                    t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                    if not t:
                        continue
                    y0 = ln.get("bbox", [0, 0, 0, 0])[1]
                    if y0 >= 0.90 * ph and re.fullmatch(r"[0-9ivxlcIVXLC]{1,6}", t):
                        footer_nums.add(t)
    finally:
        doc.close()
    fmap, size_to_level = _font_map_from_dicts(dicts) if fh_on else ({}, {})
    bold_labels = _bold_subheading_labels(dicts)
    if not fh_on:
        pages_lines = []  # F10(누락 헤딩 복구) 비활성 시 라인 미전달
    return fmap, size_to_level, pages_lines, footer_nums, bold_labels


def _apply_font_headings(md: str, fmap: dict) -> str:
    """apply_md_style 이후, fmap 에 매칭되는 '아직 헤딩 아님' 라인만 헤딩으로 승격.

    이미 헤딩(`#`)·표행(`|`)·이미지(`![`)·굵은 캡션(`**`)·리스트·코드펜스 내부는 건드리지
    않는다(중복 접두 방지, S1/S3 과 비충돌). 매칭 없으면 원문 그대로.
    """
    if not md or not fmap or not _font_headings_enabled():
        return md
    lines = md.split("\n")
    out = []
    in_fence = False
    changed = 0
    for l in lines:
        if _MD_STYLE_FENCE_RE.match(l):
            in_fence = not in_fence
            out.append(l)
            continue
        s = l.strip()
        if (in_fence or not s or s.startswith("#") or s.startswith("|")
                or s.startswith("![") or s.startswith("**")
                or re.match(r"^([-*+]\s|\d+\.\s)", s)):
            out.append(l)
            continue
        lvl = fmap.get(_norm_label(s))
        if lvl:
            indent = l[: len(l) - len(l.lstrip())]
            out.append(f"{indent}{'#' * lvl} {s}")
            changed += 1
        else:
            out.append(l)
    if changed:
        print(f"    [MD-FONT] 폰트크기 기반 헤딩 {changed}개 승격(F8)", flush=True)
        return "\n".join(out)
    return md


# ── F10: 누락된 폰트-헤딩 복구(FMDW_FONT_HEADINGS 게이트, F8 확장) — 2026-07-09 ─────
# glm 이 '장식 대형 숫자 + 제목' 형태의 챕터 표제를 그래픽으로 오인해 통째로 누락하는
# 사고(실측: p16-18 p1 의 70pt '1' + 18pt 'About This Manual'). F8 은 본문에 그 라인이
# 있어야 승격하므로 복구 불가. 여기서는 헤딩-티어 PDF 라인(F1/F2 정제 후) 중 본문에 없는
# 것을 그 페이지 상단에 삽입한다. 인접 대형 span(대형 숫자+제목)은 하나로 병합해
# '# 1 About This Manual' 로 만든다(레벨=더 큰 폰트).
def _merge_display_headings(page_lines, size_to_level):
    """페이지 6-튜플 라인에서 헤딩-티어 라인만 골라, 세로로 겹치거나 근접한 것을 하나의
    디스플레이 헤딩으로 병합. 반환: [(merged_text, level, y0)] (읽기순)."""
    ht = [(l[0], l[1], l[3], l[4], round(l[5], 1)) for l in page_lines
          if size_to_level.get(round(l[5], 1))]
    if not ht:
        return []
    groups = [[ht[0]]]
    for h in ht[1:]:
        prev = groups[-1][-1]
        big = max(prev[2] - prev[1], h[2] - h[1], 6.0)
        gap = h[1] - prev[2]  # 이전 라인 하단 ~ 현재 라인 상단
        if gap <= 0.5 * big:  # 겹침/근접 → 같은 디스플레이 헤딩(대형숫자+제목·2줄 제목)
            groups[-1].append(h)
        else:
            groups.append([h])
    result = []
    for g in groups:
        text = re.sub(r"\s+", " ", " ".join(x[3].strip() for x in g)).strip()
        level = min(size_to_level[x[4]] for x in g)  # 가장 큰 폰트(=최소 레벨)
        result.append((text, level, min(x[1] for x in g)))
    return result


def _recover_missing_headings(md: str, pages_lines, size_to_level) -> str:
    """각 페이지의 헤딩-티어 디스플레이 헤딩을 복구/승격한다(glm 비결정성 대응).

    각 디스플레이 헤딩(대형숫자+제목 병합, 짧은 ≤80자 제목형)에 대해:
      - 본문에 '정확히 그 줄'로 존재하고 아직 헤딩(`#`)이 아니면 → 그 자리에서 헤딩 승격
        (예 glm 이 '1. About This Manual' 로 옮긴 챕터표제 → '# 1 About This Manual').
      - 어떤 줄과도 매칭되지 않고(=glm 이 통째 누락) 본문에 substring 으로도 없으면
        → 페이지 마커 직후(상단)에 삽입.
      - 이미 헤딩으로 존재하거나(dedup) 더 긴 줄의 일부면 → 무변경.
    러닝헤더(F2 제외)·본문 문단은 대상 아님. FMDW_FONT_HEADINGS=0 이면 비활성.
    """
    if not md or not pages_lines or not size_to_level or not _font_headings_enabled():
        return md
    lines = md.split("\n")
    marker_re = re.compile(r"^<!-- page (\d+) -->\s*$")
    seg_bounds = []  # (page_no, marker_idx)
    for i, l in enumerate(lines):
        m = marker_re.match(l)
        if m:
            seg_bounds.append((int(m.group(1)), i))
    if not seg_bounds:
        return md
    inserts = {}       # marker_idx → [heading lines] (누락 삽입)
    promotions = {}    # line_idx → heading line (제자리 승격)
    n_insert = 0
    n_promote = 0
    for si, (page_no, midx) in enumerate(seg_bounds):
        end = seg_bounds[si + 1][1] if si + 1 < len(seg_bounds) else len(lines)
        sect_norm = _norm_present("\n".join(lines[midx + 1:end]))
        idx = page_no - 1
        if idx < 0 or idx >= len(pages_lines):
            continue
        heads = _merge_display_headings(pages_lines[idx], size_to_level)
        for (text, level, _y0) in heads:
            if len(text) > 80:
                continue  # 긴 본문형 배제(보수)
            nt = _norm_present(text)
            if len(nt) < 1:
                continue
            hline = "#" * min(level, 6) + " " + text
            # 1) 본문 섹션에서 '정확히 그 줄' 찾기(정규화 완전일치).
            matched = None
            for bi in range(midx + 1, end):
                s = lines[bi].strip()
                if s and bi not in promotions and _norm_present(s) == nt:
                    matched = bi
                    break
            if matched is not None:
                if lines[matched].lstrip().startswith("#"):
                    continue  # 이미 헤딩 → dedup
                promotions[matched] = hline  # 제자리 승격
                n_promote += 1
                continue
            # 2) 정확한 줄은 없지만 substring 으로 존재 → 더 긴 줄의 일부, 무변경(보수).
            if nt in sect_norm:
                continue
            # 3) 완전 부재 → 페이지 상단 삽입.
            inserts.setdefault(midx, []).append(hline)
            sect_norm += " " + nt
            n_insert += 1
    if not inserts and not promotions:
        return md
    out = []
    for i, l in enumerate(lines):
        out.append(promotions.get(i, l))
        if i in inserts:
            for h in inserts[i]:
                out.append("")
                out.append(h)
    print(f"    [MD-FONT] 누락 헤딩 삽입 {n_insert}개 / 제자리 승격 {n_promote}개(F10)",
          flush=True)
    return "\n".join(out)


# ── F9: 불릿 정규화(FMDW_BULLET_LIST, 기본 ON) — 2026-07-09 ──────────────────────
# 파이프라인이 리터럴 `•`/`–` 를 그대로 방출 → Markdown 에서 `•` 는 리스트 마커가 아니고
# 빈 줄 없는 연속 라인은 한 문단으로 soft-wrap 병합돼 불릿이 가로로 붙어 렌더된다.
# 수정: `•`→`- `(top), `–`→`  - `(nested 2-space). 그룹 앞 빈 줄 보장 + 불릿 사이 빈 줄 제거
# + 랩(wrap) 연속행은 앞 불릿에 병합. 코드펜스/표행은 무변경.
def _bullet_list_enabled() -> bool:
    return os.getenv("FMDW_BULLET_LIST", "1").strip().lower() not in ("0", "false", "no")


_BULLET_TOP_RE = re.compile(r"^\s*•\s+(.*)$")
_BULLET_SUB_RE = re.compile(r"^\s*–\s+(.*)$")


def _is_md_bullet(s: str) -> bool:
    return bool(re.match(r"^\s*-\s+", s))


def _is_wrap_continuation(s: str) -> bool:
    """앞 불릿의 랩(wrap) 연속행인지: 소문자 시작 / `,` 시작 / 긴 옵션코드(공백無·언더스코어).
    헤딩·새 불릿·표행은 아님(보수)."""
    st = s.strip()
    if not st:
        return False
    if (st.startswith("#") or st.startswith("|") or st.startswith("![")
            or re.match(r"^[•–]", st) or _is_md_bullet(st)):
        return False
    if st[0].islower() or st.startswith(","):
        return True
    if " " not in st and ("_" in st or len(st) >= 18) and re.match(r"^[\w.,/+\-]+$", st):
        return True
    return False


def _normalize_bullets(md: str) -> str:
    """`•`/`–` 리터럴 불릿을 Markdown 리스트로 정규화(F9). 무변경 시 원문 그대로."""
    if not md or not _bullet_list_enabled():
        return md
    if "•" not in md and "–" not in md:
        return md
    lines = md.split("\n")
    out = []
    in_fence = False
    prev_bullet_idx = None  # out 내 마지막 불릿 라인 index(연속 랩 병합용)
    changed = False
    for l in lines:
        if _MD_STYLE_FENCE_RE.match(l):
            in_fence = not in_fence
            out.append(l)
            prev_bullet_idx = None
            continue
        if in_fence or l.strip().startswith("|"):
            out.append(l)
            prev_bullet_idx = None
            continue
        mt = _BULLET_TOP_RE.match(l)
        ms = _BULLET_SUB_RE.match(l) if not mt else None
        if mt or ms:
            marker = "- " if mt else "  - "
            text = (mt or ms).group(1).strip()
            # 이전 불릿과의 사이에 낀 빈 줄 제거(타이트한 리스트)
            if len(out) >= 2 and out[-1].strip() == "" and _is_md_bullet(out[-2]):
                out.pop()
            # 그룹 첫 불릿이면 앞 문단/헤딩과 사이에 빈 줄 보장
            if out and out[-1].strip() != "" and not _is_md_bullet(out[-1]):
                out.append("")
            out.append(marker + text)
            prev_bullet_idx = len(out) - 1
            changed = True
            continue
        # 비-불릿 라인
        if prev_bullet_idx is not None and l.strip() and _is_wrap_continuation(l):
            out[prev_bullet_idx] = out[prev_bullet_idx].rstrip() + " " + l.strip()
            changed = True
            continue  # 아직 같은 불릿 안(prev_bullet_idx 유지)
        out.append(l)
        if l.strip():
            prev_bullet_idx = None
    if changed:
        print("    [MD-BULLET] 불릿 정규화(F9)", flush=True)
        return "\n".join(out)
    return md


def _merge_straddle_continuation(md: str) -> str:
    """페이지 걸침으로 쪼개진 표 셀 설명 병합(결함1, 2026-07-10, 콘텐츠 손실 0).

    `| Syntax | Description |` 표 직후(구분선 `---`·`<!-- page N -->` 마커만 건너뛴)
    첫 콘텐츠 블록이 '소문자로 시작하는 산문 문단'(문장 연속 신호, 예 'fashion and
    distance...')이고 앞 표 마지막 행의 설명 셀이 문장부호로 끝나지 않으면(잘림 신호)
    → 그 셀에 이어붙이고 문단 제거. 대문자/헤딩/term/이미지/표/마커 시작 또는 셀이
    이미 완결(.?!)이면 미적용(정상 새 콘텐츠 보존). 파이프 이스케이프·공백 정규화.
    """
    if not md or "|" not in md:
        return md
    blocks = md.split("\n\n")
    _sep_re = re.compile(r"^\|[\s:|-]+\|$")

    def _is_marker(b):
        s = b.strip()
        return s == "---" or s.startswith("<!-- page")

    def _last_rowidx(lines):
        idx = None
        for k, ln in enumerate(lines):
            s = ln.strip()
            if s.startswith("|") and s.endswith("|") and not _sep_re.match(s):
                idx = k
        return idx

    changed = False
    for i in range(len(blocks)):
        b = blocks[i]
        if b is None:
            continue
        s = b.strip()
        # 병합 후보: 단일 문단, 소문자 알파벳 시작, 표/헤딩/이미지/마커 아님.
        if not (s and "\n" not in s and re.match(r"[a-z]", s) and not s.startswith("|")):
            continue
        j = i - 1
        while j >= 0 and (blocks[j] is None or not blocks[j].strip() or _is_marker(blocks[j])):
            j -= 1
        if j < 0:
            continue
        prev_lines = blocks[j].split("\n")
        ri = _last_rowidx(prev_lines)
        if ri is None:
            continue
        row = prev_lines[ri].rstrip()
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if not cells:
            continue
        last_cell = cells[-1]
        if last_cell[-1:] in (".", "?", "!", ":"):
            continue  # 이미 완결된 셀 — 새 문단은 정상 콘텐츠라 병합 안 함
        core = row[:-1].rstrip() if row.endswith("|") else row
        cont = re.sub(r"\s+", " ", s).replace("|", "\\|")
        prev_lines[ri] = core + " " + cont + " |"
        blocks[j] = "\n".join(prev_lines)
        blocks[i] = None
        changed = True
    if not changed:
        return md
    print("    [MD-STRADDLE] 페이지 걸침 표 셀 병합(결함1)", flush=True)
    return "\n\n".join(b for b in blocks if b is not None)


# ── 페이지/청크 걸침 표·도면 캡션 반복(FMDW_CAPTION_CONTINUATION, 기본 ON) — 2026-07-12 ──
# RAG(Retrieval-Augmented Generation) 파편의 '소속' 명시: 캡션 있는 표/도면이 다음 페이지로
# (캡션 없이) 이어지면, 이어지는 조각 상단에
#   표   → '**Table N: <제목> (continued)**' + 원본 헤더 행 반복(열 이름 반복)
#   도면 → '**Figure N: <제목> (continued)**'
# 을 결정적으로 삽입한다. 이러면 청크가 조각 하나만 담아도 각 열 의미/도면 소속을 알 수 있고,
# codesign-rag figure_linker 의 'Table N/Figure N' 언급→figure_id 매칭과 시너지가 난다.
#
# 보수적 검출(오탐 0 우선): '진짜 이어지는 표/도면'만 —
#   (1) 이전 페이지 객체가 페이지 하단까지 닿음(잘림 신호=해당 페이지의 마지막 콘텐츠),
#   (2) 다음 페이지 상단에 '캡션 없는' 동일 열구조(열수 동일) 객체가 시작(첫 콘텐츠),
#   (3) 페이지 연속(N, N+1) — 청크(`---`) 경계도 절대 페이지 마커로 이어짐,
#   (4) 이전 표에 반복할 캡션('Table N: 제목')이 존재.
# 하나라도 애매하면 미삽입(별개 표에 엉뚱한 제목 붙이는 것이 최악). `(continued)` 표기로
# 중복 캡션/2개 표 오인을 방지하고 멱등(재실행 시 이미 캡션 있으면 첫-콘텐츠 아님→무동작).
_CONTCAP_TBL_RE = re.compile(r"^\s*\*{0,2}\s*table\s+(\d+)\s*[:.]\s*(.*)$", re.IGNORECASE)
_CONTCAP_FIG_RE = re.compile(r"^\s*\*{0,2}\s*figure\s+(\d+)\s*[:.]\s*(.*)$", re.IGNORECASE)
_CONTCAP_IMG_RE = re.compile(r"^\s*!\[")
_CONTCAP_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")


def _caption_continuation_enabled() -> bool:
    return os.getenv("FMDW_CAPTION_CONTINUATION", "1").strip().lower() not in (
        "0", "false", "no")


def _cc_is_skip(s: str) -> bool:
    """경계 판정에서 건너뛰는 라인: 빈 줄·수평선·HTML 주석(페이지 마커는 세그먼트 분해가 처리)."""
    if not s:
        return True
    if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
        return True
    return s.startswith("<!--") and s.endswith("-->")


def _cc_cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _cc_norm_cells(cells) -> str:
    return "|".join(re.sub(r"\s+", " ", c.strip().strip("*")).lower() for c in cells)


def _cc_strip_continued(title: str) -> str:
    return re.sub(r"\s*\(continued\)\s*$", "", title, flags=re.IGNORECASE).strip()


def _cc_segment(md: str):
    """md → [[marker_line|None, page_no|None, [body lines]], ...] (round-trip 보존)."""
    segments = []
    cur_marker, cur_pg, cur = None, None, []
    for l in md.split("\n"):
        m = _INJECT_PAGE_MARKER_RE.match(l)
        if m:
            segments.append([cur_marker, cur_pg, cur])
            cur_marker, cur_pg, cur = l, int(m.group(1)), []
        else:
            cur.append(l)
    segments.append([cur_marker, cur_pg, cur])
    return segments


def _cc_last_table_bottom(body: list):
    """세그먼트 body 의 '마지막 콘텐츠'가 GFM 표면 (start,end) 반환, 아니면 None."""
    blocks = _find_gfm_blocks(body)
    if not blocks:
        return None
    s, e = blocks[-1]
    for k in range(e + 1, len(body)):
        if not _cc_is_skip(body[k].strip()):
            return None
    return (s, e)


def _cc_first_table_top(body: list):
    """세그먼트 body 의 '첫 콘텐츠'가 (캡션 없는) GFM 표면 (start,end) 반환, 아니면 None."""
    blocks = _find_gfm_blocks(body)
    if not blocks:
        return None
    s, e = blocks[0]
    for k in range(0, s):
        if not _cc_is_skip(body[k].strip()):
            return None
    return (s, e)


def _cc_caption_above(body: list, start: int, rx):
    """표/도면 시작 바로 위 최근접 비-skip 라인이 캡션이면 (num, title). 아니면 None."""
    k = start - 1
    while k >= 0 and _cc_is_skip(body[k].strip()):
        k -= 1
    if k < 0:
        return None
    m = rx.match(body[k])
    if not m:
        return None
    title = _cc_strip_continued(m.group(2).strip().rstrip("*").strip())
    return (m.group(1), title)


def _cc_last_figure_bottom(body: list):
    """마지막 콘텐츠가 도면(이미지, 또는 이미지 없는 Figure 캡션)이면 (num, title)."""
    k = len(body) - 1
    while k >= 0 and _cc_is_skip(body[k].strip()):
        k -= 1
    if k < 0:
        return None
    line = body[k]
    if _CONTCAP_IMG_RE.match(line):                 # 마지막이 이미지 → 위쪽에서 Figure 캡션 탐색
        j = k - 1
        while j >= 0:
            st = body[j].strip()
            fm = _CONTCAP_FIG_RE.match(body[j])
            if fm:
                return (fm.group(1), _cc_strip_continued(fm.group(2).strip().rstrip("*").strip()))
            if st and not _cc_is_skip(st) and not _CONTCAP_IMG_RE.match(body[j]):
                return None                          # 이미지↔캡션 사이 다른 본문 → 도면 아님
            j -= 1
        return None
    fm = _CONTCAP_FIG_RE.match(line)                # 마지막이 Figure 캡션(이미지 없음)
    if fm:
        return (fm.group(1), _cc_strip_continued(fm.group(2).strip().rstrip("*").strip()))
    return None


def _cc_first_image_top(body: list):
    """첫 콘텐츠가 '캡션 없는' bare 이미지면 그 라인 index, 아니면 None."""
    k = 0
    while k < len(body) and _cc_is_skip(body[k].strip()):
        k += 1
    if k >= len(body):
        return None
    return k if _CONTCAP_IMG_RE.match(body[k]) else None


def _apply_caption_continuation(md: str) -> str:
    """페이지/청크 걸침 표·도면에 '(continued)' 캡션(+표 헤더 반복)을 삽입. gate OFF 시 무변경."""
    if not md or not _caption_continuation_enabled():
        return md
    segments = _cc_segment(md)
    changed = False

    # ── 표 이어짐 ──
    for i in range(len(segments) - 1):
        pg_a, body_a = segments[i][1], segments[i][2]
        pg_b, body_b = segments[i + 1][1], segments[i + 1][2]
        if pg_a is None or pg_b is None or pg_b != pg_a + 1:
            continue
        la = _cc_last_table_bottom(body_a)
        if not la:
            continue
        cap = _cc_caption_above(body_a, la[0], _CONTCAP_TBL_RE)
        if not cap:
            continue                    # 반복할 캡션 제목이 없음 → 미삽입
        fb = _cc_first_table_top(body_b)
        if not fb:
            continue
        num, title = cap
        head_a = _cc_cells(body_a[la[0]])
        sb, eb = fb
        head_b = _cc_cells(body_b[sb])
        if len(head_a) < 2 or len(head_a) != len(head_b):
            continue                    # 열수 불일치 → 별개 표(미삽입)
        cont_cap = (f"**Table {num}: {title} (continued)**" if title
                    else f"**Table {num} (continued)**")
        if _cc_norm_cells(head_a) == _cc_norm_cells(head_b):
            # 헤더 이미 반복됨 → 캡션만 표 위에 삽입.
            new_body = list(body_b)
            new_body[sb:sb] = [cont_cap, ""]
        else:
            # 다음 표 첫 행이 원 헤더와 다름 = GFM 강제 헤더(실은 데이터 행 가능) → 원 헤더+
            #   구분선 prepend + 기존 첫 행을 데이터로 강등(무손실).
            ncol = len(head_a)
            rebuilt = [cont_cap, "", body_a[la[0]],
                       "| " + " | ".join([":---"] * ncol) + " |"]
            has_sep = (sb + 1 <= eb) and bool(_CONTCAP_SEP_RE.match(body_b[sb + 1].strip()))
            data_start = sb + 2 if has_sep else sb
            if has_sep:
                rebuilt.append(body_b[sb])           # 가짜 헤더 → 데이터 행
            rebuilt.extend(body_b[data_start:eb + 1])
            new_body = body_b[:sb] + rebuilt + body_b[eb + 1:]
        segments[i + 1][2] = new_body
        changed = True

    # ── 도면 이어짐(보수적: 이전 페이지 말미가 도면 N + 다음 페이지 첫 콘텐츠가 캡션없는 이미지) ──
    for i in range(len(segments) - 1):
        pg_a, body_a = segments[i][1], segments[i][2]
        pg_b, body_b = segments[i + 1][1], segments[i + 1][2]
        if pg_a is None or pg_b is None or pg_b != pg_a + 1:
            continue
        fig = _cc_last_figure_bottom(body_a)
        if not fig:
            continue
        img_idx = _cc_first_image_top(body_b)
        if img_idx is None:
            continue
        num, title = fig
        cont_cap = (f"**Figure {num}: {title} (continued)**" if title
                    else f"**Figure {num} (continued)**")
        new_body = list(body_b)
        new_body[img_idx:img_idx] = [cont_cap, ""]
        segments[i + 1][2] = new_body
        changed = True

    if not changed:
        return md
    out = []
    for marker, _pg, body in segments:
        if marker is not None:
            out.append(marker)
        out.extend(body)
    print("    [MD-CONTCAP] 페이지/청크 걸침 표·도면 캡션 반복 삽입"
          "(FMDW_CAPTION_CONTINUATION)", flush=True)
    return "\n".join(out)


_FIGLABEL_CAP_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*(?:figure|table)\s+[\w.]+\s*[:.]\s*(.+)$", re.I)


def _figlabel_norm(t: str) -> str:
    t = re.sub(r"\$[^$]*\$", " ", t)      # LaTeX 수식 스팬 제거
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)    # 영숫자+공백만
    return re.sub(r"\s+", " ", t).strip()


def _drop_figure_label_leak(md: str) -> str:
    """도면 내부 라벨이 본문 평문으로 유출된 라인 제거(결함2, 2026-07-10, 콘텐츠 손실 0).

    glm-ocr 이 도면(래스터) 안 라벨을 본문 텍스트로 OCR 해 흘린 유출(좌표 없음 → bbox
    불가)을, 인접(±4 블록) Figure 캡션과의 substring 중복으로만 제거. 표 셀/헤딩/이미지/
    캡션/일반 산문은 미대상 — 캡션 내용에 명백히 포함되는 짧은 라벨 라인만 삭제(정의문·
    실제 본문은 캡션과 불일치라 보존). 애매하면 보존(콘텐츠 손실 0 우선).
    """
    if not md:
        return md
    blocks = md.split("\n\n")
    caps = {}
    for i, b in enumerate(blocks):
        s = b.strip()
        if "\n" in s:
            continue
        m = _FIGLABEL_CAP_RE.match(s)
        if m:
            cx = _figlabel_norm(m.group(1))
            if len(cx) >= 8:
                caps[i] = cx
    if not caps:
        return md
    drop = set()
    for i, b in enumerate(blocks):
        s = b.strip()
        if not s or "\n" in s:
            continue
        if s[:1] in "|#!<>-*`[" or s[:1].isdigit():
            continue
        if _FIGLABEL_CAP_RE.match(s):
            continue  # 캡션 자체 보존
        if len(s) > 120:
            continue  # 긴 산문/정의문 — 도면 라벨 아님
        core = _figlabel_norm(s)
        if len(core.split()) < 4:
            continue
        for ci, cx in caps.items():
            if abs(ci - i) <= 4 and core in cx:
                drop.add(i)
                break
    if not drop:
        return md
    print(f"    [MD-FIGLABEL] 도면 라벨 유출 {len(drop)}줄 제거(결함2)", flush=True)
    return "\n\n".join(b for k, b in enumerate(blocks) if k not in drop)



def apply_md_style(md: str) -> str:
    """저장 직전 결정론적 스타일 정규화(S1/S2/S3). FMDW_MD_STYLE=0 이면 원본 그대로."""
    if not _md_style_enabled():
        return md
    blocks = md.split("\n\n---\n\n")
    out = []
    for blk in blocks:
        lines = blk.split("\n")
        if _is_toc_block(lines):
            out.append(_convert_toc_block(lines))
        else:
            out.append(_style_lines(lines))
    return "\n\n---\n\n".join(out)


def process_file(input_path, output_dir_base):
    output_dir = Path(f"output/{output_dir_base}")
    output_dir.mkdir(parents=True, exist_ok=True)

    final_output_path = output_dir / f"{input_path.stem}.md"
    partial_output_path = output_dir / f"{input_path.stem}.partial.md"

    # H-5: 존재 스킵 가드 강화 —
    #   - .partial.md 가 있으면 이전 실행이 실패한 것이므로 재처리.
    #   - .md 가 있어도 MISSING/TRUNCATED 마커가 있으면 불완전본이므로 재처리.
    #   - 두 조건 모두 해당 없을 때만(완전한 .md 존재) 스킵.
    if final_output_path.exists() and not _md_is_incomplete(final_output_path) \
            and not partial_output_path.exists():
        print(f"[*] Skipping {input_path.name} (already exists)", flush=True)
        return

    if partial_output_path.exists():
        print(f"[*] Found .partial.md for {input_path.name}, re-processing...", flush=True)
    elif final_output_path.exists() and _md_is_incomplete(final_output_path):
        print(f"[*] Found incomplete markers in {input_path.name}.md, re-processing...",
              flush=True)

    print(f"[*] Provider for {input_path.name}: {ox.provider_label()}", flush=True)

    # 0. Handle Images directly
    if input_path.suffix.lower() in [".png", ".jpg", ".jpeg"]:
        md_text = extract_image(input_path)
        if md_text:
            final_output_path.write_text(md_text, encoding="utf-8")
            print(f"[+] Image MD saved: {final_output_path}")
        return

    # 1a. Hybrid extract (HWP/HWPX/DOCX/PPTX/XLSX) — HYBRID_EXTRACT=1(기본) 시 우선 처리.
    #     HYBRID_EXTRACT=0 이면 기존 convert_to_pdf 경로로 폴백(하위호환).
    #     frontmatter는 convert_project가 별도 주입하므로 source_rel="" 로 미포함.
    _hybrid_exts = {".hwp", ".hwpx", ".docx", ".pptx", ".xlsx"}
    if input_path.suffix.lower() in _hybrid_exts and os.environ.get("HYBRID_EXTRACT", "1") != "0":
        try:
            from fmdw.hybrid_extract import hybrid_convert  # lazy import
            print(f"[*] Hybrid extract: {input_path.name}", flush=True)
            res = hybrid_convert(input_path, source_rel="")
            final_output_path.write_text(res["md"], encoding="utf-8")
            print(
                f"[+] Hybrid MD saved: {final_output_path} "
                f"(text={res['text_len']:,}, imgs={res['n_images']}, figs={res['n_figures']})",
                flush=True,
            )
        except Exception as _he:
            print(f"[!] hybrid_convert 실패 ({input_path.name}): {_he} → PDF 경로로 폴백", flush=True)
        else:
            return  # 성공 시 PDF 경로 불필요

    # 1. Convert to PDF (unless it's already a PDF)
    if input_path.suffix.lower() == ".pdf":
        pdf_path = input_path
    elif input_path.suffix.lower() in (".hwp", ".hwpx"):
        pdf_path = hwp_to_pdf(input_path)
    else:
        pdf_path = convert_to_pdf(input_path)

    if not pdf_path:
        return

    # 2. PDF to MD
    try:
        total_pages = ox.count_pdf_pages(pdf_path)
    except Exception as e:
        print(f"[!] Error opening PDF for {input_path.name}: {e}")
        return

    # 오버사이즈 행렬표 페이지 사전 스캔(Fix 2) — 게이트 OFF(기본) 이면 빈 집합이라
    # 아래 청크 계획(_build_chunk_plan)이 기존 range(1, total_pages+1, CHUNK_SIZE)
    # 분할과 완전히 동일하게 동작한다(회귀 0).
    oversized_pages = _scan_oversized_pages(pdf_path)
    if oversized_pages:
        print(f"[*] 오버사이즈 행렬표 페이지 사전 스캔: {sorted(oversized_pages)} "
              f"(본문 OCR 스킵 → 크롭+AI 설명으로 대체)", flush=True)

    # [VISION_QA_AUTO] 페이지별 자동 티어링 경로(분류·QA·netcheck 를 페이지 단위 적용).
    # AUTO=0/미설정 시에는 기존 청크 단위 경로(아래 else)로 완전 무변경.
    if VISION_QA_AUTO == 1:
        chunk_texts, failed_chunks = process_pdf_auto(pdf_path, total_pages)
    else:
        chunk_texts = []
        failed_chunks = []
        chunk_ranges = []  # H-5: 실패 청크의 페이지 범위 추적용
        # M-6: 청크 간 레이트리밋 — 무조건 sleep(10) 제거. base_delay 토큰버킷 +
        #      **마지막 청크 뒤 sleep 생략**(설정화). base_delay=0(기본)이면 대기 0.
        # Fix 2: 고정 range 분할 대신 오버사이즈 페이지를 제외/자리표시자 처리하는
        #        계획(plan)을 사용 — OCR 청크는 계획 내 등장 순서대로만 카운트한다.
        plan = _build_chunk_plan(total_pages, CHUNK_SIZE, oversized_pages)
        n_chunks = sum(1 for item in plan if item[0] == "ocr")
        limiter = _RateLimiter(base_delay=VISION_QA_RATE_DELAY)
        ocr_idx = 0
        for item in plan:
            if item[0] == "skip":
                # 오버사이즈 행렬표 페이지 — 거대표는 크롭+describe 로 별도 처리하되,
                # 표 '밖' 본문(Note·절 텍스트·intro·정의표)은 PDF 벡터 텍스트로 전사해
                # 보존한다(vision body OCR 은 여전히 미호출 — 환각/절단 방지). 표만 있는
                # 페이지면 순수 자리표시자 → 회귀 0.
                page = item[1]
                body_md = _oversized_body_md(pdf_path, page)
                recovered = not body_md.strip().endswith("-->")
                print(f"    - Page {page}: oversized matrix table → figure crop"
                      f"{' + 표 밖 본문 전사' if recovered else ''} "
                      f"(vision body OCR skipped)", flush=True)
                chunk_texts.append(body_md)
                continue

            _, start, end = item
            ocr_idx += 1
            # M-8: 청크 실패 시 페이지 단위 폴백(소청크 재추출 + 제한 재시도).
            #      한 페이지 실패가 청크 전체 손실로 번지지 않게 한다. 완전 복구되면
            #      결과는 정상 청크와 동일(출력 동작 보존), 일부 실패 시 H-5 형식
            #      인라인 MISSING 마커가 그 페이지에만 남는다.
            chunk_text, fb_failed_pages = extract_chunk_with_page_fallback(
                pdf_path, start, end, ocr_idx)
            if chunk_text:
                chunk_texts.append(chunk_text)
                # 폴백에서 일부 페이지가 끝내 실패했으면 부분본으로 표시(.partial.md).
                if fb_failed_pages:
                    failed_chunks.append(ocr_idx)
                    chunk_ranges.append((start, end))
                    print(f"    [!] Chunk {ocr_idx} partially recovered "
                          f"(failed pages: {fb_failed_pages})", flush=True)
                # M-6: 다음 OCR 청크가 남아 있을 때만 base_delay 대기(마지막 뒤 생략).
                limiter.mark_call_end()
                if ocr_idx < n_chunks:
                    limiter.wait_before_next()
            else:
                # W4: break 대신 continue — 한 청크 실패가 후속 페이지 전부 누락 방지.
                #     M-8: 페이지 폴백으로도 한 페이지도 못 살린 경우만 청크 전체 MISSING.
                failed_chunks.append(ocr_idx)
                chunk_ranges.append((start, end))
                print(f"    [!] Chunk {ocr_idx} (pages {start}-{end}) failed "
                      f"(page fallback exhausted), continuing...", flush=True)
                # H-5: 실패 청크 위치에 누락 마커 플레이스홀더 삽입 (결합 시 위치 보존)
                chunk_texts.append(
                    f"<!-- MISSING pages {start}-{end}: extraction failed -->"
                )
                continue
        if failed_chunks:
            print(f"    [!] {len(failed_chunks)} chunk(s) failed/partial: {failed_chunks}",
                  flush=True)

    # 3. Save and Cleanup
    if chunk_texts:
        combined = "\n\n---\n\n".join(chunk_texts)
        # M-3: 결합 직후 Figure 헤딩을 문서 전역 1..K 로 리넘버(청크/페이지마다
        #       1부터 재시작하던 중복 제거). MISSING/TRUNCATED 마커는 헤딩이 아니므로
        #       보존된다(partial/final 양쪽 동일 적용 — 부분본도 번호 일관).
        combined = renumber_figures(combined)
        # CJK(중국어) → 영어 후처리(2026-07-07): glm-ocr 이 중국어를 그대로 전사하므로
        # 저장 직전에 중국어 포함 문단을 영어로 번역해 교체(FMDW_CJK_TO_EN=1 기본).
        combined = _translate_cjk_paragraphs_to_english(combined)
        # FIX A(2026-07-09): LLM 이 fallback/retry 경로에서 본문에 복창한 전사 계약
        #   (TRANSCRIPTION RULES ...) 누출 블록 제거. MD_STYLE 전, 결정론적 신뢰성 계층.
        combined = _strip_prompt_leak(combined)
        # 결함1: 페이지 걸침으로 쪼개진 표 셀 설명 병합(소문자 연속 문단 → 앞 표 셀).
        combined = _merge_straddle_continuation(combined)
        # 결함2: 도면 내부 라벨의 본문 평문 유출 제거(인접 Figure 캡션과 substring 중복).
        combined = _drop_figure_label_leak(combined)
        # FIX B(2026-07-09): 중간 러닝헤더/워터마크 스탬프 제거 + 연속 완전중복 문단 런
        #   collapse + 챕터 러닝헤더 인접제거(F1 텍스트 backstop·F2). fuzzy 금지.
        combined = _collapse_duplicate_transcription(combined)
        # F3+F5(2026-07-09): 페이지 내 중복 Figure 캡션 dedup + 순수 도면라벨 페이지 정리.
        #   FMDW_MD_CLEAN=0 이면 비활성. 주입 섹션(### Figure/![)은 저장 후 추가라 미포함.
        combined = _dedup_figure_captions(combined)
        # 사용자 MD 서식 표준 결정론적 정규화(2026-07-09): 제목계층(S1)·캡션/노트 굵게(S2)·
        # 목차 페이지 2열 표(S3). renumber_figures/CJK 후, 저장·figure 주입 전. FMDW_MD_STYLE=0
        # 이면 비활성. `### Figure` 헤딩·코드펜스·표 행은 무변경이라 inject 계약 보존.
        combined = apply_md_style(combined)
        # F8/F10(2026-07-09): PDF 폰트 크기 기반 헤딩. MD_STYLE 이후 적용해 이미 헤딩(#)·표·
        #   코드펜스는 무변경(S1/S3 비충돌). F8=본문 대형제목 승격, F10=glm 이 통째로 누락한
        #   대형 챕터표제(대형숫자+제목) 복구. FMDW_FONT_HEADINGS=0 비활성.
        _fmap, _size2lvl, _pages_lines, _footer_nums, _bold_labels = \
            _font_heading_map(pdf_path)
        # TASK3(2026-07-11): PDF footer 영역 실측 페이지번호가 본문 경계에 유출된 줄 제거.
        combined = _strip_footer_pagenums(combined, _footer_nums)
        combined = _apply_font_headings(combined, _fmap)
        combined = _recover_missing_headings(combined, _pages_lines, _size2lvl)
        # F11(2026-07-11): 본문 크기 볼드 무번호 소제목을 `### ` 로 승격(F8 보완).
        combined = _promote_bold_subheadings(combined, _bold_labels)
        # F9(2026-07-09): 리터럴 `•`/`–` 불릿 → Markdown 리스트 정규화(가로붙음 방지).
        #   FMDW_BULLET_LIST=0 비활성. 코드펜스/표행 무변경.
        combined = _normalize_bullets(combined)
        # CONTCAP(2026-07-12): 페이지/청크 걸침 표·도면에 '(continued)' 캡션+헤더 반복 삽입
        #   (RAG 파편 소속 명시). 최종 표/캡션 형태가 확정된 마지막 콘텐츠 후처리로 배치.
        #   FMDW_CAPTION_CONTINUATION=0 비활성(회귀 0). 페이지 마커·표 행만 사용(결정론).
        combined = _apply_caption_continuation(combined)
        has_failures = bool(failed_chunks)

        # H-5: 실패 청크가 있으면 .partial.md 로 저장 — 완성본으로 위장하지 않는다.
        #       완전 성공 시에는 .md 로 저장하고 기존 .partial.md 가 있으면 제거.
        if has_failures:
            partial_output_path.write_text(combined, encoding="utf-8")
            print(f"[!] Partial MD saved (some chunks failed): {partial_output_path}",
                  flush=True)
            # 이전에 생성된 완성본 .md 가 있으면 제거 (부분본이 최신)
            if final_output_path.exists():
                final_output_path.unlink()
                print(f"[*] Removed stale complete .md: {final_output_path}", flush=True)
        else:
            final_output_path.write_text(combined, encoding="utf-8")
            print(f"[+] Final MD saved: {final_output_path}", flush=True)
            # 성공적으로 완성 → 기존 .partial.md 제거
            if partial_output_path.exists():
                partial_output_path.unlink()
                print(f"[*] Removed .partial.md (now complete): {partial_output_path}",
                      flush=True)

    # figure 이미지 크롭(opt-in, EXTRACT_FIGURES=1 시에만). MD 변환과 독립이며,
    # 미설정 시 no-op 라 기존 동작 byte-identical 보존. temp-PDF 정리 전에 호출해야
    # pdf_path 가 아직 존재한다(변환 포맷도 figure 크롭 가능).
    figs = maybe_extract_figures(pdf_path, output_dir)
    # W2: 추출 이미지(complex_table + figure)를 방금 기록된 MD 본문 끝에 참조로 주입해
    #     마크다운 뷰어에서 실제 이미지로 보이게 한다. figs 가 None/빈 리스트면(OFF/0건)
    #     no-op → 기존 MD byte-identical(회귀 0). 본문 전사는 보존하고 '추가'만 한다.
    if figs:
        target_md = partial_output_path if partial_output_path.exists() else final_output_path
        if target_md.exists():
            inject_figure_refs_into_md(target_md, figs, output_dir, pdf_path)

    # Only delete if we converted it
    if input_path.suffix.lower() in [".docx", ".pptx", ".xlsx", ".hwp", ".hwpx"] and pdf_path.exists():
        pdf_path.unlink()
        print(f"[*] Cleaned up temporary PDF: {pdf_path.name}")

def _unload_all_llms_at_exit():
    """종료 시 로컬 LLM 전원 언로드(사용자 의무 요건, 2026-07-09).

    (a) ollama 상주 모델 전부 /api/ps + keep_alive:0 언로드(figure_extractor 헬퍼 재사용)
    (b) MLX describe 메모리 해제(_unload_mlx_describe, 미로드면 no-op)
    FMDW_NO_UNLOAD=1 이면 생략(convert_project.py 와 동일 opt-out 계약). 실패는 비차단.
    """
    if os.getenv("FMDW_NO_UNLOAD", "").strip() == "1":
        print("[UNLOAD] FMDW_NO_UNLOAD=1 — 종료 언로드 생략", flush=True)
        return
    try:
        from fmdw.figure_extractor import _unload_mlx_describe, _unload_ollama_models

        n = "?"
        try:
            import httpx

            r = httpx.get("http://localhost:11434/api/ps", timeout=10)
            n = len(r.json().get("models") or [])
        except Exception:  # noqa: BLE001 — 카운트는 로그용, 실패해도 언로드는 진행
            pass
        _unload_ollama_models()
        _unload_mlx_describe()
        print(f"[UNLOAD] 종료: ollama {n}개 모델 언로드, MLX 해제", flush=True)
    except Exception as e:  # noqa: BLE001 — 종료 정리 실패가 종료 코드를 오염시키지 않게
        print(f"[!] [UNLOAD] 종료 언로드 실패(무시): {e}", flush=True)


def main():
    # 종료 시 로컬 LLM 전원 언로드 보장(정상 종료·예외·KeyboardInterrupt 모두 finally).
    try:
        _main_convert()
    finally:
        _unload_all_llms_at_exit()


def _main_convert():
    setup_dirs()
    
    # Define tasks: (input_dir_suffix, output_dir_suffix, extension_pattern)
    tasks = [
        ("docx", "docx_md", "*.docx"),
        ("pptx", "pptx_md", "*.pptx"),
        ("xlsx", "xlsx_md", "*.xlsx"),
        ("pdf", "pdf_md", "*.pdf"),
        ("hwp", "hwp_md", "*.hwp"),
        ("hwp", "hwp_md", "*.hwpx"),
        ("image", "image_md", "*.[pP][nN][gG]"),
        ("image", "image_md", "*.[jJ][pP][gG]"),
        ("image", "image_md", "*.[jJ][pP][eE][gG]")
    ]
    
    for in_sfx, out_sfx, ext in tasks:
        input_dir = Path(f"input/{in_sfx}")
        if not input_dir.exists(): continue
        
        files = sorted(list(input_dir.glob(ext)))
        if not files: continue
        
        print(f"\n>>> Processing {in_sfx.upper()} files...")
        for f in files:
            process_file(f, out_sfx)

if __name__ == "__main__":
    main()
