"""page_tier.py — PDF 페이지별 성격 자동 분류(회로도 밀도 티어링).

filestomdwgem PDF→Markdown(MD) 배치 변환에서 **페이지마다** vision QA(Quality
Assurance) 처리 강도를 다르게 적용하기 위한 분류기다. 고밀도 회로도(dense)는 앙상블
+ net_tracer 교차검증, 저밀도 회로도(light)는 단일 vision QA + 교차검증, 순수 텍스트
(text)는 vision QA skip — 이렇게 **3티어**로 나눠 비용/품질을 자동 균형한다.

────────────────────────────────────────────────────────────────────────────
분류 신호 (2종 — 벡터 + MD 휴리스틱)
────────────────────────────────────────────────────────────────────────────
1. **벡터 신호** (filestomdwgem 자체 — pdf-to-kicad 스킬 의존 없음):
   PyMuPDF(`fitz`) `page.get_drawings()` 로 line/path segment 수를 센다. 회로도는
   와이어·심볼이 벡터 line/bezier/rect 로 그려지므로 segment 수가 폭증한다. 래스터
   (스캔본)/벡터 없는 페이지는 0 → MD 휴리스틱으로만 판별(graceful degrade).
   - `l`(line) = +1, `c`(cubic bezier) = +1, `re`(rect) = +4(4변), `qu`(quad) = +4.
   - fitz import/파싱 실패 시 vector_lines=0 + signals["vector_error"] 기록.

2. **MD 휴리스틱** (1차 추출 Markdown 기반 — 벡터 없어도 동작):
   - **designator 수**: `net_crosscheck._DESIG_RE`(R/C/U/L/J/D/Q/X/Y/K/M/ANT/SW/TP/FB
     + BGA ball) 로 고유 designator 카운트(net_crosscheck 단일 SSoT 재사용 — 회귀 분리).
   - **PIN→NET 행 수**: `net_crosscheck._PIN_NET_RE`(`-> `) 매칭 행 + `→`(유니코드
     화살표) 행. 핀별 배선 표기는 회로도의 강한 신호.
   - **schematic 키워드**: `schematic|회로도|net|GND|VDD|pin`(대소문자 무시) 매칭.

────────────────────────────────────────────────────────────────────────────
티어 임계값 (모두 환경변수로 조정 가능)
────────────────────────────────────────────────────────────────────────────
  dense : (vector_lines >= VTIER_DENSE_LINES(800)  AND  designators >= VTIER_DENSE_DESIG(15))
          OR  vector_lines >= VTIER_DENSE_LINES_STRONG(2500)   ← strong 분기(MD 신호 무관)
  light : dense 미달이나 schematic 신호 있음 —
            designators >= VTIER_LIGHT_DESIG(5)  OR  pin_rows >= 1
            OR vector_lines >= VTIER_LIGHT_LINES(200)
  text  : 그 외(회로도 신호 없음 — vision QA skip 대상)

벡터 파싱 실패 시에도 designators/pin_rows/keyword 만으로 light/text 판별이 가능하다
(dense 는 벡터+designator 동시 충족이 필요하므로 벡터 실패 시 최대 light 로 떨어진다 —
보수적, 비용 폭주 방지). strong 분기(VTIER_DENSE_LINES_STRONG)도 vec=0 이면 발동 안 함.

공개 API:
    classify_page(pdf_path, page, primary_md) -> PageTier

`__main__` 으로 직접 실행하면 PDF 의 각 페이지를 분류만 하고 표로 출력한다(비용 0 dry-run).
단, MD 휴리스틱은 1차 추출 MD 가 필요하므로 CLI dry-run 은 벡터 신호 위주가 된다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# net_crosscheck 의 designator/PIN→NET 패턴을 단일 SSoT 로 재사용(회귀 분리).
# 패키지 경로 우선, 직접 실행(`python fmdw/page_tier.py`) 대비 fallback.
try:
    from fmdw import net_crosscheck as _nc  # 패키지 경로
except Exception:  # noqa: BLE001 - 직접 실행/경로 차이 대비
    import net_crosscheck as _nc  # type: ignore


# ──────────────────────────────────────────────────────────────────────────────
# 임계값 (환경변수 override — 기본값은 모듈 docstring 참조)
# ──────────────────────────────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    """환경변수를 int 로 읽되 비정상 값이면 기본값(graceful)."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# dense 조건: 벡터 line 폭증 + designator 다수 동시 충족.
VTIER_DENSE_LINES = _env_int("VTIER_DENSE_LINES", 800)
VTIER_DENSE_DESIG = _env_int("VTIER_DENSE_DESIG", 15)
# dense strong 분기: 벡터 line 이 이 값 이상이면 MD 신호 무관 dense 승격.
# MD 추출 저품질·CLI dry-run 등 designator=0 인 명백한 고밀도 회로도를 구제.
VTIER_DENSE_LINES_STRONG = _env_int("VTIER_DENSE_LINES_STRONG", 2500)
# light 조건: schematic 신호(저밀도 회로도) 임계.
VTIER_LIGHT_DESIG = _env_int("VTIER_LIGHT_DESIG", 5)
VTIER_LIGHT_LINES = _env_int("VTIER_LIGHT_LINES", 200)

# schematic 키워드(회로도 텍스트 신호). 한글 '회로도' 포함. 대소문자 무시.
_SCHEMATIC_KW_RE = re.compile(
    r"schematic|회로도|\bnet\b|\bGND\b|\bVDD\b|\bpin\b",
    re.IGNORECASE,
)
# 유니코드 화살표(`→`) 기반 PIN→NET 변형(_PIN_NET_RE 는 ASCII `->` 만 본다).
_UNICODE_ARROW_RE = re.compile(r"→")

# 유효 티어 라벨(검증/문서용).
TIERS = ("dense", "light", "text")


@dataclass
class PageTier:
    """페이지 분류 결과.

    tier    : "dense" | "light" | "text".
    signals : 분류 근거 dict —
                vector_lines (int), designators (int), pin_rows (int),
                matched_keywords (list[str]), vector_error (str|None).
    page    : 1-based 페이지 번호(편의 — dry-run/로그용).
    """

    tier: str
    signals: dict = field(default_factory=dict)
    page: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# 벡터 신호 (PyMuPDF get_drawings)
# ──────────────────────────────────────────────────────────────────────────────
def _count_vector_segments(pdf_path, page: int, doc=None) -> tuple[int, str | None]:
    """페이지의 vector line/path segment 수를 센다(fitz get_drawings).

    Args:
        pdf_path: 원본 PDF 경로(doc 미지정 시 open).
        page    : 1-based 페이지 번호.
        doc     : (M-5) 이미 열린 fitz Document 핸들. 주어지면 재사용하고 닫지 않는다
                  (호출자 소유). None 이면 내부에서 1회 open/close(기존 동작 동일).

    Returns:
        (segment_count, error). error 는 실패 사유 문자열(없으면 None).
        실패(파일없음/fitz없음/파싱오류)는 (0, reason) — 호출부에서 MD 휴리스틱만으로
        판별하도록 graceful degrade.

    segment 가중:
        'l'(line)=+1, 'c'(cubic bezier)=+1, 're'(rect)=+4(4변), 'qu'(quad)=+4.
    """
    owns_doc = doc is None
    if owns_doc:
        try:
            import fitz  # PyMuPDF — 지연 import(테스트에서 monkeypatch 가능, 의존 격리).
        except Exception as exc:  # noqa: BLE001
            return 0, f"fitz import failed: {type(exc).__name__}"
        try:
            doc = fitz.open(str(pdf_path))
        except Exception as exc:  # noqa: BLE001
            return 0, f"open failed: {type(exc).__name__}: {exc}"

    try:
        # page 는 1-based 입력 → fitz 는 0-based.
        idx = page - 1
        if idx < 0 or idx >= doc.page_count:
            return 0, f"page out of range (page={page}, count={doc.page_count})"
        pg = doc.load_page(idx)
        drawings = pg.get_drawings()
        count = 0
        for path in drawings:
            for item in path.get("items", []):
                if not item:
                    continue
                op = item[0]
                if op == "l":          # line segment
                    count += 1
                elif op == "c":        # cubic bezier
                    count += 1
                elif op == "re":       # rectangle = 4 edges
                    count += 4
                elif op == "qu":       # quad = 4 edges
                    count += 4
                # 알 수 없는 op 은 무시(보수적).
        return count, None
    except Exception as exc:  # noqa: BLE001
        return 0, f"get_drawings failed: {type(exc).__name__}: {exc}"
    finally:
        if owns_doc:
            try:
                doc.close()  # 내부에서 연 것만 닫는다(외부 핸들은 호출자 소유).
            except Exception:  # noqa: BLE001
                pass


# ──────────────────────────────────────────────────────────────────────────────
# MD 휴리스틱
# ──────────────────────────────────────────────────────────────────────────────
def _scan_md_signals(primary_md: str) -> tuple[int, int, list[str]]:
    """1차 추출 MD 에서 (designator 수, PIN→NET 행 수, 매칭 키워드) 추출.

    - designators: net_crosscheck._DESIG_RE 로 고유 designator 카운트.
    - pin_rows: net_crosscheck._PIN_NET_RE(ASCII `->`) 매칭 행 + 유니코드 `→` 행.
    - matched_keywords: schematic 키워드(중복 제거, 소문자).
    """
    if not primary_md:
        return 0, 0, []

    designators: set[str] = set()
    pin_rows = 0
    matched_kw: set[str] = set()

    for raw_line in primary_md.splitlines():
        # designator 수집(net_crosscheck 패턴 단일 SSoT 재사용).
        for d in _nc._scan_designators(raw_line):
            designators.add(d)
        # PIN→NET 행: ASCII `->`(net_crosscheck 패턴) 또는 유니코드 `→`.
        if _nc._PIN_NET_RE.search(raw_line) or _UNICODE_ARROW_RE.search(raw_line):
            pin_rows += 1
        # schematic 키워드.
        for m in _SCHEMATIC_KW_RE.finditer(raw_line):
            matched_kw.add(m.group(0).lower())

    return len(designators), pin_rows, sorted(matched_kw)


# ──────────────────────────────────────────────────────────────────────────────
# 티어 결정
# ──────────────────────────────────────────────────────────────────────────────
def _decide_tier(vector_lines: int, designators: int, pin_rows: int,
                 matched_keywords: list[str]) -> tuple[str, str]:
    """신호 → (티어, dense_reason) 결정(순수 함수 — 임계값 비교만, 단위 테스트 용이).

    dense : (vector_lines >= DENSE_LINES AND designators >= DENSE_DESIG)  — "vec+desig"
            OR vector_lines >= DENSE_LINES_STRONG                          — "strong_vector"
    light : (dense 미달) schematic 신호 있음 —
              designators >= LIGHT_DESIG OR pin_rows >= 1 OR vector_lines >= LIGHT_LINES
              OR schematic 키워드 매칭
    text  : 그 외

    Returns:
        (tier, dense_reason) — dense_reason 은 dense 시 "vec+desig" 또는 "strong_vector",
        dense 미달 시 "" (빈 문자열).
    """
    # strong 분기: 벡터 line 이 압도적으로 많으면 MD 신호(designator) 무관 dense 승격.
    # vec=0(파싱 실패)은 0 < STRONG 이므로 발동 안 함 — 기존 보수적 정책 유지.
    if vector_lines >= VTIER_DENSE_LINES_STRONG:
        return "dense", "strong_vector"
    # 기존 AND 분기: 벡터 line 폭증 + designator 다수 동시 충족.
    if vector_lines >= VTIER_DENSE_LINES and designators >= VTIER_DENSE_DESIG:
        return "dense", "vec+desig"
    if (
        designators >= VTIER_LIGHT_DESIG
        or pin_rows >= 1
        or vector_lines >= VTIER_LIGHT_LINES
        or bool(matched_keywords)
    ):
        return "light", ""
    return "text", ""


def classify_page(pdf_path, page: int, primary_md: str, doc=None) -> PageTier:
    """페이지 1건을 dense/light/text 로 분류한다.

    Args:
        pdf_path   : 원본 PDF 경로(벡터 신호 추출용).
        page       : 1-based 페이지 번호.
        primary_md : 그 페이지(또는 청크) 1차 추출 Markdown(MD 휴리스틱용).
        doc        : (M-5) 이미 열린 fitz Document 핸들(재사용). None 이면 내부 open
                     (기존 동작). 동일 PDF 를 페이지마다 재오픈하지 않기 위함.

    Returns:
        PageTier(tier, signals, page). 벡터 파싱 실패 시 signals["vector_error"] 에
        사유를 기록하고 MD 휴리스틱만으로 판별한다(graceful degrade — dense 는 벡터가
        필요하므로 자연히 최대 light 로 떨어짐).
    """
    # M-5: 공유 핸들이 있으면 doc kwarg 로 전달(재오픈 회피). 없으면 기존 2-인자
    #      호출 그대로 — _count_vector_segments 를 monkeypatch 한 기존 테스트 계약 보존.
    if doc is not None:
        vector_lines, vec_err = _count_vector_segments(pdf_path, page, doc=doc)
    else:
        vector_lines, vec_err = _count_vector_segments(pdf_path, page)
    designators, pin_rows, matched_kw = _scan_md_signals(primary_md)

    tier, dense_reason = _decide_tier(vector_lines, designators, pin_rows, matched_kw)

    signals: dict = {
        "vector_lines": vector_lines,
        "designators": designators,
        "pin_rows": pin_rows,
        "matched_keywords": matched_kw,
        "vector_error": vec_err,
        "thresholds": {
            "dense_lines": VTIER_DENSE_LINES,
            "dense_desig": VTIER_DENSE_DESIG,
            "dense_lines_strong": VTIER_DENSE_LINES_STRONG,
            "light_desig": VTIER_LIGHT_DESIG,
            "light_lines": VTIER_LIGHT_LINES,
        },
    }
    # dense 승격 근거 기록(strong_vector / vec+desig). dense 미달이면 키 없음.
    if dense_reason:
        signals["dense_reason"] = dense_reason
    return PageTier(tier=tier, signals=signals, page=page)


# ──────────────────────────────────────────────────────────────────────────────
# CLI dry-run (비용 0 — 벡터 신호 위주 분류 점검)
# ──────────────────────────────────────────────────────────────────────────────
def _dryrun_pdf(pdf_path: str) -> int:
    """PDF 전 페이지를 벡터 신호만으로 분류해 표로 출력(MD 없이 — text/light 위주).

    MD 휴리스틱(designator/pin)은 1차 추출 MD 가 있어야 정확하다. CLI dry-run 은
    추출 비용 0 을 위해 primary_md="" 로 벡터 신호만 본다(designator=0).
    실제 배치 dry-run(VISION_QA_AUTO_DRYRUN)은 1차 MD 를 넘겨 정확히 분류한다.
    """
    try:
        import fitz
    except Exception as exc:  # noqa: BLE001
        print(f"[!] fitz import 실패 — dry-run 불가: {exc}")
        return 1
    try:
        doc = fitz.open(pdf_path)
        n = doc.page_count
        doc.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] PDF open 실패: {exc}")
        return 1

    print(f"# page_tier dry-run: {pdf_path}  ({n} pages)")
    print(f"# thresholds: dense(lines>={VTIER_DENSE_LINES} & desig>={VTIER_DENSE_DESIG} "
          f"OR lines>={VTIER_DENSE_LINES_STRONG}[strong]) "
          f"light(desig>={VTIER_LIGHT_DESIG} | pin>=1 | lines>={VTIER_LIGHT_LINES} | kw)")
    print("# NOTE: CLI dry-run 은 MD 없이 벡터 신호만 본다(designator=0). "
          "정확 분류는 배치 VISION_QA_AUTO_DRYRUN 사용.")
    print(f"{'page':>5} | {'vec_lines':>9} | {'tier':>6} | note")
    print("-" * 48)
    counts = {"dense": 0, "light": 0, "text": 0}
    for p in range(1, n + 1):
        pt = classify_page(pdf_path, p, "")
        counts[pt.tier] += 1
        note = pt.signals.get("vector_error") or ""
        print(f"{p:>5} | {pt.signals['vector_lines']:>9} | {pt.tier:>6} | {note}")
    print("-" * 48)
    print(f"summary: dense={counts['dense']} light={counts['light']} text={counts['text']}")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python lib/page_tier.py <pdf_path>")
        sys.exit(2)
    sys.exit(_dryrun_pdf(sys.argv[1]))
