"""filestomdwgem 앙상블(ensemble) vision QA — 항목 다수결(majority vote) 통합.

────────────────────────────────────────────────────────────────────────────
왜 앙상블인가 (배경)
────────────────────────────────────────────────────────────────────────────
단일 pass vision QA(lib.vision_qa.review)는 환각 교정이 **확률적**이다. 다회
검증(docs/qa/phase3_dpi300_multirun.md)에서:
  - C71 값 환각 교정([unreadable]): 2/4 (50% — 절반은 놓침)
  - C81/C82 중복 제거: 0/4 (항상 실패, 단발 성공은 운)
즉 단일 verifier run은 vision 모델 비결정성 때문에 같은 환각을 매번 잡지 못한다.

본 모듈은 **verifier(Claude vision)를 N=3회 독립 실행**하고, designator(부품 지정자)
단위로 각 run의 판정을 추출(`_parse_verdicts`)하여 **다수결(`_majority_vote`)** 로
통합한다. 한 run이 환각을 놓쳐도, 다른 2개 run이 잡으면 다수결로 교정된다.

확정 설계(사용자 승인): 항목 다수결, N=3.
  generator(Ollama 1차, 고정) → verifier(Claude vision) 3회 독립 → designator별
  판정 추출 → 다수결 통합.

────────────────────────────────────────────────────────────────────────────
출력 형식 계약 보존 (절대 위반 금지)
────────────────────────────────────────────────────────────────────────────
  - 통합 MD는 1차/단일 QA와 동일한 Markdown 전문(`### Figure N` 섹션 구조, 표/리스트
    형식)을 유지한다. 청크 결합 계약(`{stem}.md`, `\n\n---\n\n`)은 상위
    호출부(extract_all_via_pdf)에서 그대로 유지된다.
  - 성공 run < 2면 안전 degrade: 1차 primary_md를 그대로 반환(corrected=False).
  - 모든 실패는 파이프라인을 중단시키지 않는다(단일 QA와 동일 degrade 철학).

────────────────────────────────────────────────────────────────────────────
다수결 규칙 (designator별, N개 run의 판정 리스트 → 최종 판정)
────────────────────────────────────────────────────────────────────────────
  - 2+ run이 `[unreadable]`           → 최종 `[unreadable]`
  - 2+ run이 동일 구체값               → 그 값 채택(confirmed value)
  - 값이 run마다 다름(2:1 이상 충돌)    → `[unverified]` (환각 의심, 단정 거부)
  - 2+ run이 `confirmed`(플래그 없음)   → confirmed (베이스 라인 유지)
  - 동률/불명                          → 보수적으로 `[unverified]`
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 단일 vision QA 재사용 — review()를 N회 호출한다(수정 최소화).
try:
    from fmdw import vision_qa as _vqa  # 패키지 경로
except Exception:  # noqa: BLE001 - 직접 실행/경로 차이 대비
    import vision_qa as _vqa  # type: ignore

# QAResult 재사용(동일 컨테이너 계약).
QAResult = _vqa.QAResult


# ──────────────────────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────────────────────

#: 디버그용 designator별 투표 분포 JSON 덤프 스위치(기본 off).
#: 설정 시 /tmp(또는 TMPDIR)에 votes JSON을 남긴다.
VISION_QA_KEEP_VOTES = bool(os.getenv("VISION_QA_KEEP_VOTES"))

#: verifier 독립 실행 횟수 상한(C-7). 순차 호출이므로 과도한 n은 rate limit·시간 폭증.
#: n은 max(1, min(int(n), VISION_QA_N_CAP))로 클램프된다.
VISION_QA_N_CAP = 5


# ──────────────────────────────────────────────────────────────────────────────
# verdict(판정) 상수 — designator별 최종/run 판정의 의미 라벨.
# ──────────────────────────────────────────────────────────────────────────────

V_UNREADABLE = "[unreadable]"   # 판독 불가(값 환각 차단)
V_UNVERIFIED = "[unverified]"   # 검증 불가(값 run마다 충돌 → 단정 거부)
V_CONFIRMED = "confirmed"       # 플래그 없는 정상 전사(구체값 없음/생략)
V_ABSENT = "absent"             # 해당 run에 designator가 없음


def _warn(msg: str) -> None:
    print(f"    [vision_qa_ensemble][warn] {msg}", file=sys.stderr, flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# designator 파싱
# ──────────────────────────────────────────────────────────────────────────────

# designator 정규식. BGA ball(U24.M8 등)을 일반 designator보다 먼저 매칭하도록
# alternation 순서를 BGA-first로 둔다(최장 매칭 우선).
#   - BGA ball:  U\d+\.[A-Z]\d+        (예: U24.M8, U7.L2)
#   - 일반:      R/C/U/L/J/D/Q/X \d+   (+ U는 게이트 접미 [A-Z] 허용: U24A)
# 단어 경계로 감싸 'CON2'·'DDR_DQ48' 같은 라벨 내부 부분일치를 피한다(앞뒤 word
# boundary). designator는 표 셀/리스트 항목의 토큰으로 등장한다.
_DESIGNATOR_RE = re.compile(
    r"\b("
    r"[UQ]\d+\.[A-Z]\d+"          # BGA ball: U24.M8 / Q3.A1 (앞에 와야 최장매칭)
    r"|U\d+[A-Z]?"                # U24, U24A (게이트 접미 1자)
    r"|R\d+|C\d+|L\d+|J\d+|D\d+|Q\d+|X\d+"
    r")\b"
)

# 그룹 범위(예: R91-R112, C59-C74). 같은 prefix 두 designator를 '-'로 잇는다.
# 통합 시 그룹 라인은 베이스 유지(범위 전체 다수결은 신뢰도 낮음 — 보수적).
_RANGE_RE = re.compile(r"\b([RCULJDQX]\d+)\s*[-–]\s*([RCULJDQX]\d+)\b")

# 플래그 토큰(값 자리에 오면 그 자체가 판정).
_FLAG_UNREADABLE_RE = re.compile(r"\[unreadable\]", re.IGNORECASE)
_FLAG_UNVERIFIED_RE = re.compile(r"\[unverified(?:\s+range)?\]", re.IGNORECASE)

# 구체값 후보 추출: 전자부품 값 패턴(우선) — 캡/저항/인덕터/전압/허용오차/패키지.
#   22uF, 220uF, 2.2uF, 0.1uF, 100nF, 10K, 22K, 25R5, 4.7k, 1%, 16V, 0603, 2012 ...
# 너무 공격적이면 net 이름을 값으로 오인하므로, 단위/허용오차/패키지 토큰에 한정.
#
# C-4: `10K`/`22K`(Ω 생략 kilo/mega) 분기 추가 — 단, net 이름(`CLK10`, `DQ22M`)
# 오탐 방지를 위해 뒤에 영숫자/언더스코어가 이어지지 않는 경계를 요구한다
# (`\b` 뒤 추가로 `(?![A-Za-z0-9_])` 음의 lookahead). 앞 경계는 공통 lookbehind.
_VALUE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"("
    r"\d+(?:\.\d+)?\s*[pnuµμmkKMR]?[FΩ]"     # 22uF, 100nF, 4.7k? (cap/res 단위)
    r"|\d+(?:\.\d+)?\s*[kKmM]?[ΩR]\b"        # 10K?, 25R5(=25.5R 표기 변형 일부), 4R7
    r"|\d+R\d+"                              # 25R5, 4R7 (R as decimal point)
    r"|\d+(?:\.\d+)?\s*[kKmM](?![A-Za-z0-9_])"  # C-4: 10K, 22K, 4.7k (ohm 생략 kilo/mega)
    r"|\d+(?:\.\d+)?\s*[pnuµμ]?H"            # 인덕턴스 10uH
    r"|\d+(?:\.\d+)?\s*V\b"                  # 16V, 3.3V
    r"|\d+(?:\.\d+)?\s*%"                    # 1%, 0.5%
    r"|\b[0-9]{4}\b(?![A-Za-z0-9_])"         # 패키지 코드 0603/0402/2012/1206
    r")",
)


def _normalize_value(v: str) -> str:
    """값 토큰 정규화 — 다수결 동일성 비교용(공백 제거, 대소문자/µ 통일)."""
    s = v.strip().replace(" ", "")
    s = s.replace("µ", "u").replace("μ", "u")
    # 단위 대문자 R/F/V/H 통일, 접두 k 통일(K→k 는 회로값 관습이나 양방향 혼용 →
    # 비교 안정성 위해 소문자 통일). Ω → R(ohm) 표기 통일.
    s = s.replace("Ω", "R")
    s = s.replace("K", "k")  # 10K == 10k
    return s.lower()


def _extract_values(segment: str) -> list[str]:
    """라인 세그먼트에서 구체값 토큰들을 추출(정규화 전 원형)."""
    return [m.group(1).strip() for m in _VALUE_TOKEN_RE.finditer(segment)]


@dataclass
class _LineVerdict:
    """한 designator가 한 run의 한 라인에서 가진 판정."""

    verdict: str          # V_UNREADABLE / V_UNVERIFIED / V_CONFIRMED / 구체값
    raw_value: str = ""   # 구체값일 때 원형 토큰(통합 치환용)


def _parse_verdicts(md: str) -> dict[str, _LineVerdict]:
    """교정본 MD에서 designator별 판정을 추출한다.

    각 designator가 등장하는 라인을 보고:
      - 라인에 `[unreadable]` 있음           → V_UNREADABLE
      - 라인에 `[unverified]`/`[unverified range]` 있고 구체값 없음 → V_UNVERIFIED
      - 라인에 구체값 토큰 있음(플래그보다 우선 X) → 그 정규화 값(첫 토큰),
        단 `raw_value`에 원형 토큰을 함께 보존(C-5: 단위 대소문자/µ 비가역 강등 방지).
      - 그 외(designator만 등장, 값/플래그 없음) → V_CONFIRMED

    같은 designator가 여러 라인에 등장하면(중복 나열) 첫 등장 라인의 판정을 사용.
    (중복 자체는 통합 단계에서 1회만 남기도록 처리.)

    완벽 파싱은 목표가 아니다 — 매칭 안 되는 항목은 dict에서 누락되며, 통합 시
    베이스 라인이 그대로 유지된다(데이터 손실 방지).

    Returns:
        {designator: _LineVerdict}. verdict는 V_* 상수 또는 정규화된 구체값 문자열,
        raw_value는 구체값일 때 원형 토큰(정규화 전).
    """
    verdicts: dict[str, _LineVerdict] = {}
    for raw_line in md.splitlines():
        # 표 구분선(|---|---|)·헤더 가드: designator 없으면 어차피 skip되지만,
        # 명시적으로 빈 처리.
        desigs = _scan_line_designators(raw_line)
        if not desigs:
            continue
        for desig in desigs:
            if desig in verdicts:
                continue  # 첫 등장 우선
            verdicts[desig] = _classify_line_for(raw_line, desig)
    return verdicts


def _scan_line_designators(line: str) -> list[str]:
    """한 라인에서 등장하는 designator 토큰을 순서대로(중복 제거) 반환."""
    seen: list[str] = []
    for m in _DESIGNATOR_RE.finditer(line):
        d = m.group(1)
        if d not in seen:
            seen.append(d)
    return seen


#: 셀 내 괄호 주석(`(see 2012 note)`)을 값 추출 전에 제거하기 위한 패턴.
#: C-3: 주석 속 4자리(`2012`)·전압류가 designator/value 셀로 오인되는 것 방지.
_PAREN_COMMENT_RE = re.compile(r"\([^)]*\)")


def _designator_value_segment(line: str, desig: str) -> str:
    """desig의 '값 셀' 구간을 값 추출 범위로 좁힌다(C-3).

    표 행(`| C71 (see 2012 note) | 16V | ...`)에서 컬럼 인식 없이 라인 전체를
    값 추출하면 designator 셀 속 주석의 4자리(`2012`)를 값으로 오인 → 오교정한다.

    규칙:
      - 파이프 표 행이면: designator 셀의 **괄호 주석을 제거**한 잔여 + **그 다음
        value 셀**을 후보로 본다(value는 보통 designator 다음 컬럼). 다음 designator
        셀로의 침범은 막는다(다음 셀까지만).
      - 표 행이 아니면(리스트/프로즈): designator 토큰 직후 ~ 다음 designator 직전,
        괄호 주석 제거.
    """
    s = line
    if s.lstrip().startswith("|"):
        # GFM 표 행: 셀 분해(인덱스 정합 유지). 앞쪽 빈 셀 포함.
        cells = s.split("|")
        for i, cell in enumerate(cells):
            if _DESIGNATOR_RE.search(cell) and desig in _scan_line_designators(cell):
                # designator 셀: 주석 제거 + designator 토큰 이후 잔여(같은 셀 동거 값).
                desig_cell = _PAREN_COMMENT_RE.sub(" ", cell)
                after = desig_cell.split(desig, 1)
                same_cell_tail = after[1] if len(after) > 1 else ""
                # 다음 셀(value 컬럼) — 단 그 셀에 또 다른 designator가 있으면 침범 금지.
                next_cell = ""
                if i + 1 < len(cells):
                    cand = cells[i + 1]
                    if not _scan_line_designators(cand):
                        next_cell = _PAREN_COMMENT_RE.sub(" ", cand)
                return f"{same_cell_tail} | {next_cell}"
        return _PAREN_COMMENT_RE.sub(" ", s)  # 안전 fallback
    # 비표 라인: desig 등장 위치 ~ 다음 designator 직전.
    idx = s.find(desig)
    if idx < 0:
        return _PAREN_COMMENT_RE.sub(" ", s)
    rest = s[idx + len(desig):]
    nxt = _DESIGNATOR_RE.search(rest)
    if nxt:
        rest = rest[:nxt.start()]
    return _PAREN_COMMENT_RE.sub(" ", rest)


def _classify_line_for(line: str, desig: str) -> _LineVerdict:
    """라인 + 특정 designator → 그 designator의 판정.

    플래그(unreadable/unverified)는 라인 전체에서 본다(verifier가 그 행 값을 못 믿어
    붙인 신호이므로 라인 단위 적용). 단 **구체값 추출은 designator 셀 구간으로 한정**
    (C-3: 컬럼 인식 없는 전역 첫 값 토큰 치환 오교정 방지).
    구체값일 때 `raw_value`에 원형 토큰을 보존(C-5).
    """
    # 1) [unreadable] 최우선(값 환각 차단 신호) — 라인 단위.
    if _FLAG_UNREADABLE_RE.search(line):
        return _LineVerdict(V_UNREADABLE)
    # 3) [unverified] 플래그 — 라인 단위.
    has_unverified = bool(_FLAG_UNVERIFIED_RE.search(line))
    # 2) 구체값 토큰 — designator 셀 구간으로 한정(C-3).
    segment = _designator_value_segment(line, desig)
    vals = _extract_values(segment)
    if vals:
        # 값이 있는데 unverified 플래그도 있으면 → 값 신뢰 불가로 보고 unverified.
        if has_unverified:
            return _LineVerdict(V_UNVERIFIED)
        first = vals[0]
        return _LineVerdict(_normalize_value(first), raw_value=first)
    if has_unverified:
        return _LineVerdict(V_UNVERIFIED)
    # 4) designator만, 값/플래그 없음 → 정상 전사로 간주.
    return _LineVerdict(V_CONFIRMED)


# ──────────────────────────────────────────────────────────────────────────────
# 다수결
# ──────────────────────────────────────────────────────────────────────────────

def _majority_vote(run_verdicts: list[str]) -> str:
    """한 designator의 run별 판정 리스트 → 최종 판정.

    run_verdicts: 성공한 각 run에서 이 designator가 받은 판정. 해당 run에
    designator가 없으면 V_ABSENT가 들어온다(호출부가 채움).

    규칙(설계 확정):
      1) 2+ run이 [unreadable]            → [unreadable]
      2) 값이 run마다 다름(서로 다른 구체값 2종 이상이 충돌, 단일 다수 없음)
                                          → [unverified]
      3) 2+ run이 동일 구체값              → 그 값(confirmed value)
      4) 2+ run이 confirmed               → confirmed
      5) 동률/불명                         → 보수적으로 [unverified]
      6) R10 M8: 단일 run 에만 등장한 구체값(나머지 전부 ABSENT) → 정족수(2+) 미달
         → [unverified] 강등(값은 라인에 남고 플래그 부착 — 무플래그 확정 금지)

    구체값 vs [unreadable] 동수 등 모호한 경우는 안전하게 [unverified]로 수렴
    (값 환각보다 '검증 불가' 표기가 보수적).
    """
    # absent는 투표에서 제외(그 run은 이 designator에 대해 무의견).
    present = [v for v in run_verdicts if v != V_ABSENT]
    if not present:
        return V_ABSENT

    counts = Counter(present)
    n_unreadable = counts.get(V_UNREADABLE, 0)
    n_unverified = counts.get(V_UNVERIFIED, 0)
    n_confirmed = counts.get(V_CONFIRMED, 0)

    # 구체값들(플래그/상수 제외)만 집계.
    value_counts = {
        v: c for v, c in counts.items()
        if v not in (V_UNREADABLE, V_UNVERIFIED, V_CONFIRMED, V_ABSENT)
    }
    distinct_values = list(value_counts.keys())

    # (1) 2+ run이 [unreadable] → [unreadable]
    if n_unreadable >= 2:
        return V_UNREADABLE

    # 값 다수결 후보 계산.
    if value_counts:
        top_val, top_n = max(value_counts.items(), key=lambda kv: kv[1])
    else:
        top_val, top_n = None, 0

    # (3) 2+ run이 동일 구체값 → 그 값.
    #     단, 동일 표 수의 다른 값이 동시에 2+면(불가능: 2+2 require n>=4) 충돌로.
    if top_n >= 2:
        # 다른 구체값도 동일 최다면 충돌 → unverified.
        tied = [v for v, c in value_counts.items() if c == top_n]
        if len(tied) >= 2:
            return V_UNVERIFIED
        return top_val

    # (2) 값이 run마다 다름(서로 다른 구체값 2종 이상, 단일 다수 없음=top_n<2)
    #     → 3원 충돌 [22uF,220uF,2.2uF] 또는 1:1:1 → unverified.
    if len(distinct_values) >= 2:
        return V_UNVERIFIED

    # 여기부터 distinct_values 는 0개 또는 1개(=top_n==1).
    # (4) 2+ run confirmed → confirmed (값 없는 정상 전사 다수).
    if n_confirmed >= 2:
        return V_CONFIRMED

    # 단일 값 1개 + 나머지 confirmed/unverified 혼재 등 약한 신호 처리.
    # 값 1표 + confirmed 1+ 이면 값은 단정 못하나 존재는 인정 → 보수적 unverified가
    # 아니라, 값이 충돌하지 않으므로 그 값을 약하게 채택할지 고민되지만, 설계상
    # '2+ 동의'가 채택 조건이므로 단일 값(1표)은 confirmed로 강등하지 않고
    # unverified로 둔다(환각 단정 방지).
    if top_n == 1 and (n_confirmed >= 1 or n_unverified >= 1 or n_unreadable >= 1):
        return V_UNVERIFIED

    # 단일 run만 존재(present 길이 1) — 나머지 run 은 전부 ABSENT:
    if len(present) == 1:
        # R10 M8(QA 2026-07-15): 구 동작은 단일 의견의 '구체값'을 그대로 확정 채택
        # → 모듈 계약('2+ run 동의가 채택 조건')의 미문서 우회 + 환각 run 1개의
        # 값이 무플래그로 병합 MD 에 유입(n=3 에서 [100nF, ABSENT, ABSENT] 사례).
        # 정족수(2+) 미달 단일 구체값은 [unverified] 로 강등한다 — 값 자체는 라인에
        # 남고 _apply_verdict_to_line 이 [unverified] 플래그만 부착(값 단정 약화,
        # 회로도 전사 원칙 '정확성 > 완전성' 정합). 플래그 판정(unreadable/
        # unverified/confirmed) 단일 의견은 이미 보수적 표기라 그대로 반영(기존 유지).
        if present[0] in (V_UNREADABLE, V_UNVERIFIED, V_CONFIRMED):
            return present[0]
        return V_UNVERIFIED

    # (5) 동률/불명 → 보수적 unverified.
    return V_UNVERIFIED


# ──────────────────────────────────────────────────────────────────────────────
# 통합 MD 생성
# ──────────────────────────────────────────────────────────────────────────────

def _pick_base_md(run_mds: list[str], run_verdict_maps: list[dict[str, _LineVerdict]]) -> int:
    """베이스 run 선택 — designator 최다, 동수면 MD 길이 최대.

    Returns:
        run_mds 인덱스.
    """
    best_idx = 0
    best_key = (-1, -1)
    for i, (md, vm) in enumerate(zip(run_mds, run_verdict_maps)):
        key = (len(vm), len(md))
        if key > best_key:
            best_key = key
            best_idx = i
    return best_idx


def _apply_verdict_to_line(
    line: str,
    desig: str,
    final: str,
    single_designator: bool = True,
    raw_value: str = "",
) -> str:
    """베이스 라인의 desig 항목을 최종 판정으로 후처리한다.

    - final == V_CONFIRMED            → 라인 그대로(베이스 유지).
    - final == V_UNREADABLE           → 값 자리를 `[unreadable]`로(또는 플래그 부착).
    - final == V_UNVERIFIED           → `[unverified]` 플래그 부착(값 단정 제거).
    - final == 구체값                  → 라인의 값 토큰을 그 값으로 치환(없으면 부착).

    C-1: **값 치환은 단일 designator 라인에서만** 수행한다. 다중-designator 라인이나
    범위 라인(`R91-R112`)은 `single_designator=False`로 호출되며, 값 치환을 절대 하지
    않는다(행 구조 붕괴 방지). 대신 라인 단위 플래그(`[unreadable]`/`[unverified]`)만
    designator 옆에 부착할 수 있다.

    C-5: 구체값 치환 시 정규화 문자열이 아닌 `raw_value`(원형 토큰)로 치환해
    단위 대소문자·µ 강등을 막는다(raw_value 미제공 시 보수적으로 치환 생략).

    형식 계약을 깨지 않도록 표/리스트 라인 구조(파이프, 들여쓰기)는 보존하고
    값 토큰만 in-place 치환하거나 플래그를 덧붙인다. 매칭 안 되면 라인 유지.
    """
    if final in (V_CONFIRMED, V_ABSENT):
        return line

    # C-3: 치환/플래그 대상 값 토큰은 designator의 '값 셀' 구간에서만 고른다
    # (라인 전역 첫 토큰 = 주석 4자리 오교정 방지). 단일 designator 라인일 때만 의미.
    target_val = ""
    if single_designator:
        seg_vals = _extract_values(_designator_value_segment(line, desig))
        if seg_vals:
            target_val = seg_vals[0]

    if final == V_UNREADABLE:
        if _FLAG_UNREADABLE_RE.search(line):
            return line  # 이미 표기됨
        if single_designator and target_val:
            # 단일 designator 라인 — 값 셀 토큰을 [unreadable]로 치환.
            return _replace_first_value(line, target_val, "[unreadable]")
        # 다중 라인이거나 값이 없으면 designator 뒤에 라인 단위 플래그 부착.
        return _append_flag_near(line, desig, "[unreadable]")

    if final == V_UNVERIFIED:
        if _FLAG_UNVERIFIED_RE.search(line):
            return line
        # 값이 있으면 값 옆에 [unverified] 부착(값 단정 약화).
        if single_designator and target_val:
            return _append_flag_after_value(line, target_val, "[unverified]")
        # 다중 라인이거나 값 없음 → designator 옆 라인 단위 플래그.
        return _append_flag_near(line, desig, "[unverified]")

    # ── 구체값 채택 ──
    # C-1: 다중 designator 라인에선 값 치환 금지(베이스 유지). 어느 셀이 desig의 값인지
    # 안전 식별 불가 → 행 garbling 위험. 베이스 그대로 둔다.
    if not single_designator:
        return line
    if target_val:
        if _normalize_value(target_val) == final:
            return line  # 이미 같은 값 — 표기 유지
        # C-5: 정규화 문자열이 아닌 원형 토큰으로 치환. raw 없으면 치환 보류(강등 방지).
        if not raw_value:
            return line
        return _replace_first_value(line, target_val, raw_value)
    # 베이스 라인에 값이 없는데 다수결 값이 있으면 designator 옆에 부착(원형 우선).
    return _append_flag_near(line, desig, raw_value or final)


def _replace_first_value(line: str, old_val: str, new_val: str) -> str:
    """라인에서 old_val 첫 등장만 new_val로 치환(정규식 메타 escape)."""
    return re.sub(re.escape(old_val), new_val, line, count=1)


def _append_flag_after_value(line: str, val: str, flag: str) -> str:
    """값 토큰 바로 뒤에 ` flag` 부착(이미 있으면 중복 방지)."""
    if flag in line:
        return line
    return re.sub(re.escape(val), f"{val} {flag}", line, count=1)


def _append_flag_near(line: str, desig: str, flag: str) -> str:
    """designator 토큰 뒤에 ` flag` 부착(표 셀 구조 보존, 중복 방지)."""
    if flag in line:
        return line
    return re.sub(re.escape(desig), f"{desig} {flag}", line, count=1)


def _normalize_line_content(line: str) -> str:
    """라인 중복 판정용 정규화 키 — 공백/백틱/대소문자 차이를 무시한 셀 내용 비교.

    C-2: designator만 같고 셀 내용이 다른 행(예: IC 핀맵 `U24|pin1->NET_A` vs
    `U24|pin2->NET_B`)을 보존하려면, designator-set이 아니라 **라인 전체 내용**으로
    중복을 판정해야 한다. 표기 차이(공백/백틱/대소문자)만 다른 진짜 중복(C81/C81)만
    동일 키가 되도록 정규화한다.
    """
    s = line.strip().lower()
    s = s.replace("`", "")
    s = re.sub(r"\s+", " ", s)            # 연속 공백 1칸
    s = re.sub(r"\s*\|\s*", "|", s)       # 셀 경계 공백 제거
    return s


def _dedupe_designator_lines(lines: list[str]) -> list[str]:
    """**라인 전체 내용이 완전 동일한** 항목 라인 중복을 첫 1회만 남긴다(C-2).

    C81/C82 같은 *완전 중복 나열*만 제거한다. designator는 같지만 셀 내용이 다른
    행(IC 핀맵 `U24|pin1->NET_A`, `U24|pin2->NET_B` — FIGURE_RULES per-pin `PIN->NET`
    보존 대상)은 내용이 다르므로 **둘 다 유지**한다.

    표 헤더/구분선/섹션 헤더는 항목 라인이 아니므로 영향 없음. 정규화(공백·백틱·
    대소문자 무시)로 표기 차이만 다른 진짜 중복은 잡되, 실데이터가 다르면 보존한다.
    """
    seen_content: set[str] = set()
    out: list[str] = []
    for line in lines:
        if not _is_item_line(line):
            out.append(line)
            continue
        # 항목 라인이라도 designator가 없으면(순수 데이터 행) 중복 제거 대상 아님.
        if not _scan_line_designators(line):
            out.append(line)
            continue
        key = _normalize_line_content(line)
        if key in seen_content:
            continue  # 완전 동일 항목 라인 — 드롭.
        seen_content.add(key)
        out.append(line)
    return out


def _is_item_line(line: str) -> bool:
    """표 데이터 행 또는 리스트 항목 라인인지(중복 제거 대상 후보) 판별."""
    s = line.strip()
    if not s:
        return False
    # GFM 표 데이터 행: 파이프 포함 + 구분선(---) 아님.
    if s.startswith("|") and "|" in s[1:]:
        if re.fullmatch(r"\|[\s:\-|]+\|?", s):
            return False  # 구분선
        return True
    # 리스트 항목.
    if re.match(r"^[-*+]\s+", s):
        return True
    return False


def _is_range_line(line: str) -> bool:
    """범위 라인(`R91-R112`, `C59 – C74`)인지(C-6: _RANGE_RE 실사용)."""
    return bool(_RANGE_RE.search(line))


def _build_merged_md(
    run_mds: list[str],
    run_verdict_maps: list[dict[str, _LineVerdict]],
    final_verdicts: dict[str, str],
    raw_map: Optional[dict[str, str]] = None,
) -> str:
    """베이스 run을 골라 각 designator 라인을 다수결 판정으로 후처리한 통합 MD.

    형식 계약(`### Figure N`, 표/리스트 구조, 청크 결합 규칙)을 보존한다.
    매칭 안 된 라인은 베이스 그대로.

    C-1: 한 라인의 designator가 2개 이상이거나(다중) 범위 라인(`R91-R112`)이면
    `single_designator=False`로 처리 — 값 치환을 금지하고 라인 단위 플래그만 허용한다
    (행 구조 붕괴 방지).
    C-5: raw_map의 원형 토큰을 치환에 사용(비가역 강등 방지).
    """
    raw_map = raw_map or {}
    base_idx = _pick_base_md(run_mds, run_verdict_maps)
    base_md = run_mds[base_idx]

    out_lines: list[str] = []
    for raw_line in base_md.splitlines():
        desigs = _scan_line_designators(raw_line)
        if not desigs:
            out_lines.append(raw_line)
            continue
        # C-1/C-6: 단일 designator + 비범위 라인일 때만 값 치환 허용.
        single = (len(desigs) == 1) and not _is_range_line(raw_line)
        line = raw_line
        for desig in desigs:
            final = final_verdicts.get(desig)
            if final is None:
                continue  # 다수결에 없으면 베이스 유지
            line = _apply_verdict_to_line(
                line, desig, final,
                single_designator=single,
                raw_value=raw_map.get(desig, ""),
            )
        out_lines.append(line)

    # C81/C82 완전 중복 항목 라인 제거(다수 run이 중복 나열해도 통합본은 1회).
    deduped = _dedupe_designator_lines(out_lines)
    return "\n".join(deduped)


# ──────────────────────────────────────────────────────────────────────────────
# 투표 집계 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _tally_votes(
    run_verdict_maps: list[dict[str, _LineVerdict]],
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    """run별 designator 판정 맵들 → (최종 판정, run 판정 리스트, 채택값 raw 맵).

    모든 run에 등장한 designator 합집합에 대해, 각 run에서의 판정(없으면 V_ABSENT)을
    모아 _majority_vote에 넘긴다. 최종 판정이 구체값이면, 그 정규화 값에 투표한 run들의
    **원형 토큰(raw_value)을 다수결**로 골라 raw_map에 보존한다(C-5: 비가역 강등 방지).

    Returns:
        (final, vote_lists, raw_map)
          final: {designator: 최종 verdict(V_* 또는 정규화 값)}
          vote_lists: {designator: [run별 verdict 문자열]} (디버그/덤프용)
          raw_map: {designator: 출력에 쓸 원형 값 토큰} (final이 구체값일 때만)
    """
    all_desigs: set[str] = set()
    for vm in run_verdict_maps:
        all_desigs.update(vm.keys())

    vote_lists: dict[str, list[str]] = defaultdict(list)
    # designator·정규화값 → 원형 토큰들(다수결로 대표 원형 선택).
    raw_by_norm: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for desig in all_desigs:
        for vm in run_verdict_maps:
            lv = vm.get(desig)
            if lv is None:
                vote_lists[desig].append(V_ABSENT)
            else:
                vote_lists[desig].append(lv.verdict)
                if lv.raw_value:
                    raw_by_norm[desig][lv.verdict][lv.raw_value] += 1

    final: dict[str, str] = {}
    raw_map: dict[str, str] = {}
    for desig, votes in vote_lists.items():
        fv = _majority_vote(votes)
        final[desig] = fv
        # 최종이 구체값(플래그/상수 아님)이고 그 값에 투표한 원형 토큰이 있으면 보존.
        if fv not in (V_UNREADABLE, V_UNVERIFIED, V_CONFIRMED, V_ABSENT):
            raw_counter = raw_by_norm.get(desig, {}).get(fv)
            if raw_counter:
                # 가장 많이 등장한 원형 토큰(동수면 임의 안정 선택).
                raw_map[desig] = raw_counter.most_common(1)[0][0]
    return final, dict(vote_lists), raw_map


def _summarize(final_verdicts: dict[str, str]) -> tuple[int, int, int, int]:
    """최종 판정 요약 카운트 → (agreed_value, confirmed, unverified, unreadable)."""
    agreed_value = 0
    confirmed = 0
    unverified = 0
    unreadable = 0
    for v in final_verdicts.values():
        if v == V_UNREADABLE:
            unreadable += 1
        elif v == V_UNVERIFIED:
            unverified += 1
        elif v == V_CONFIRMED:
            confirmed += 1
        elif v == V_ABSENT:
            continue
        else:
            agreed_value += 1
    return agreed_value, confirmed, unverified, unreadable


def _dump_votes(vote_lists: dict[str, list[str]], final: dict[str, str]) -> Optional[str]:
    """VISION_QA_KEEP_VOTES 시 designator별 투표 분포를 /tmp JSON으로 덤프."""
    if not VISION_QA_KEEP_VOTES:
        return None
    try:
        payload = {
            "final": final,
            "votes": vote_lists,
        }
        fd, path = tempfile.mkstemp(prefix="visionqa_votes_", suffix=".json")
        os.close(fd)
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    except Exception as e:  # noqa: BLE001
        _warn(f"votes 덤프 실패: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────────────────────

def review_ensemble(
    primary_md: str,
    pdf_path: Path,
    start_page: int,
    end_page: int,
    n: int = 3,
    dpi: Optional[int] = None,
) -> QAResult:
    """1차 추출 MD를 vision verifier N회 독립 실행 + 항목 다수결로 교정한다.

    `lib.vision_qa.review`를 n회 **순차** 호출(동시 금지 — claude OAuth rate limit)하여
    각 교정본 MD를 수집하고, designator별 판정을 다수결로 통합한 MD를 반환한다.

    안전 degrade:
      - vision QA 비활성(VISION_QA 미설정) → 1차 MD 그대로(corrected=False).
      - 성공 run < 2 → 1차 primary_md 그대로(corrected=False, note에 사유).
        (1개 run만 성공해도 다수결이 불가능하므로 단정하지 않는다.)
      - 모든 예외는 파이프라인을 중단시키지 않는다.

    Args:
        primary_md: Ollama 1차 추출 Markdown(이 청크 전문).
        pdf_path: 원본 PDF 경로.
        start_page: 청크 시작 페이지(1-based, inclusive).
        end_page: 청크 끝 페이지(1-based, inclusive).
        n: verifier 독립 실행 횟수(기본 3).
        dpi: 렌더 DPI(미지정 시 vision_qa.VISION_QA_DPI).

    Returns:
        QAResult. markdown=통합 MD(또는 degrade 시 primary_md).
    """
    backend = _vqa.backend_label()

    if not _vqa.is_enabled():
        return QAResult(markdown=primary_md, corrected=False, backend=backend,
                        note="vision QA disabled (VISION_QA unset)")

    if not (primary_md and primary_md.strip()):
        return QAResult(markdown=primary_md, corrected=False, backend=backend,
                        note="1차 MD 비어있음 — 앙상블 skip")

    n = max(1, min(int(n), VISION_QA_N_CAP))  # C-7: 상한 클램프(순차 호출 폭증 방지)

    # W5(vision_qa와 동일): 페이지 범위 가드 — 음수·역전 방지.
    start_page = max(1, start_page)
    end_page = max(start_page, end_page)

    # ── H-6: 페이지 PNG를 **1회만** 렌더해 N회 verifier 호출에서 재사용 ──────────
    # 기존: review()를 N번 호출 → 같은 페이지 이미지를 N번 재래스터화(300 DPI 고밀도
    # 회로도에서 무거움). 렌더된 이미지는 바이트 동일이므로 1회 렌더 후 동일 PNG를
    # N개 독립 verifier run에 전달한다. 다수결/통합 입력(run_mds)은 불변 — 각 run은
    # 원래도 같은 페이지를 보았다(동작 보존, 오직 렌더 중복만 제거).
    png_paths: list[Path] = []
    try:
        png_paths, page_label, use_dpi = _vqa.render_qa_pngs(
            Path(pdf_path), start_page, end_page, dpi
        )
    except Exception as e:  # noqa: BLE001 - 렌더 실패 시 안전 degrade(파이프라인 보호)
        _warn(f"앙상블 렌더 실패 → 1차 MD 유지: {e}")
        _vqa.cleanup_pngs(png_paths)
        return QAResult(markdown=primary_md, corrected=False, backend=backend,
                        note=f"ensemble degrade(render): {e}")

    try:
        # N회 순차 호출 — 동일 PNG 재사용, 각 run의 교정본 수집(성공한 것만).
        run_mds: list[str] = []
        failed = 0
        for i in range(n):
            try:
                res = _vqa.review_with_pngs(primary_md, png_paths, page_label, use_dpi)
            except Exception as e:  # noqa: BLE001 - review 내부에서 이미 degrade하지만 방어적
                _warn(f"run {i+1}/{n} 예외 → skip: {e}")
                failed += 1
                continue
            if res.corrected and res.markdown and res.markdown.strip():
                run_mds.append(res.markdown)
            else:
                # corrected=False = 그 run은 degrade(1차 그대로) → 다수결 표본 아님.
                _warn(f"run {i+1}/{n} degrade/실패 → 투표 제외 ({res.note})")
                failed += 1

        # 성공 run 통합(< 2면 _merge_runs가 안전 degrade).
        return _merge_runs(primary_md, run_mds, failed, n, backend)
    finally:
        # H-6: 렌더 소유자가 정리(VISION_QA_KEEP_PNG=1 이면 보존).
        _vqa.cleanup_pngs(png_paths)


def _merge_runs(
    primary_md: str, run_mds: list[str], failed: int, n: int, backend: str
) -> QAResult:
    """수집된 성공 run 교정본들을 다수결 통합한다(공통 로직).

    review_ensemble / review_image_ensemble 가 공유. 성공 run < 2면 degrade.
    """
    if len(run_mds) < 2:
        return QAResult(
            markdown=primary_md, corrected=False, backend=backend,
            note=(f"ensemble n={n}: 성공 run {len(run_mds)} < 2 "
                  f"(failed={failed}) → 다수결 불가, 1차 MD 유지"),
        )

    run_verdict_maps = [_parse_verdicts(md) for md in run_mds]
    final_verdicts, vote_lists, raw_map = _tally_votes(run_verdict_maps)
    merged_md = _build_merged_md(run_mds, run_verdict_maps, final_verdicts, raw_map)

    if "### Figure" in primary_md and "### Figure" not in merged_md:
        _warn("통합본에서 ### Figure 소실 → 베이스 run 그대로 사용")
        base_idx = _pick_base_md(run_mds, run_verdict_maps)
        merged_md = run_mds[base_idx]

    agreed_value, confirmed, unverified, unreadable = _summarize(final_verdicts)
    votes_path = _dump_votes(vote_lists, final_verdicts)

    note = (
        f"ensemble n={n}, success_runs={len(run_mds)}, failed={failed}, "
        f"designators={len(final_verdicts)}, agreed_value={agreed_value}, "
        f"confirmed={confirmed}, unverified_by_conflict={unverified}, "
        f"unreadable={unreadable}"
    )
    if votes_path:
        note += f" [votes:{votes_path}]"
    return QAResult(markdown=merged_md, corrected=True, backend=backend, note=note)


def review_image_ensemble(
    primary_md: str,
    image_path: Path,
    n: int = 3,
) -> QAResult:
    """단일 이미지(PNG/JPG) 1차 MD를 vision verifier N회 + 다수결로 교정한다.

    `lib.vision_qa.review_image`를 n회 순차 호출 후 designator 다수결 통합.
    안전 degrade 철학은 review_ensemble과 동일(성공 run < 2 → 1차 MD 유지).

    Args:
        primary_md: 1차 추출 Markdown.
        image_path: 원본 이미지 경로.
        n: verifier 독립 실행 횟수(기본 3).

    Returns:
        QAResult.
    """
    backend = _vqa.backend_label()

    if not _vqa.is_enabled():
        return QAResult(markdown=primary_md, corrected=False, backend=backend,
                        note="vision QA disabled (VISION_QA unset)")
    if not (primary_md and primary_md.strip()):
        return QAResult(markdown=primary_md, corrected=False, backend=backend,
                        note="1차 MD 비어있음 — 앙상블 skip")

    n = max(1, min(int(n), VISION_QA_N_CAP))  # C-7: 상한 클램프(순차 호출 폭증 방지)

    # H-6 대칭: 원본 이미지는 렌더가 없지만(=재래스터화 없음), N회 호출에서 존재
    # 확인을 1회로 모으고 verifier 호출만 review_with_pngs로 N회 반복한다(PDF 경로와
    # 동일한 구조 — 다수결 입력 run_mds 는 불변). 원본 파일이므로 정리하지 않는다.
    img = Path(image_path)
    label = f"image {img.name}"
    if not img.exists():
        _warn(f"{label} 없음 → 1차 MD 유지")
        return QAResult(markdown=primary_md, corrected=False, backend=backend,
                        note=f"ensemble degrade(image): 이미지 없음: {img}")

    run_mds: list[str] = []
    failed = 0
    for i in range(n):
        try:
            res = _vqa.review_with_pngs(primary_md, [img], label, use_dpi=None)
        except Exception as e:  # noqa: BLE001 - 방어적
            _warn(f"image run {i+1}/{n} 예외 → skip: {e}")
            failed += 1
            continue
        if res.corrected and res.markdown and res.markdown.strip():
            run_mds.append(res.markdown)
        else:
            _warn(f"image run {i+1}/{n} degrade/실패 → 투표 제외 ({res.note})")
            failed += 1

    return _merge_runs(primary_md, run_mds, failed, n, backend)
