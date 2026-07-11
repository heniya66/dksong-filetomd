"""net_crosscheck.py — vision QA(확률적) ↔ net_tracer(벡터 결정적 넷리스트) 교차검증.

vision QA 가 만든 교정 Markdown 을, pdf-to-kicad 스킬의 `net_tracer`(벡터 와이어
트레이싱)가 산출한 **결정적 넷리스트**로 보강한다. net_tracer 는 다른 워크스페이스
(`~/.claude/skills/pdf-to-kicad/`)의 read-only 코드이므로 **직접 import 하지 않고**
`scripts/run_net_tracer.py` 를 서브프로세스(shell=False)로 호출해 **JSON 만** 소비한다
(프로세스 경계 = read-only 룰·venv 충돌·버전 변동 리스크 회피).

[STRICT] 환각 판정은 **플래그만** — 자동삭제·자동교정 금지. 벡터 없음(래스터)/실패
시에는 vision_md 를 **그대로 반환**(applied=False) = 안전 degrade.

공개 API:
    crosscheck(vision_md, pdf_path, page) -> CrosscheckResult(markdown, summary, applied)

교차검증 규칙(플래그만):
  1. vision `[unverified]`/`[unreadable]` designator 가 net_tracer 에 **존재**, 또는
     `PIN -> NET` 행의 (ref,pin)/net 이 net_tracer refpin/named net 과 **일치**
     → 해당 라인에 `[vector-confirmed]` 토큰 부착(승격).
  2. vision designator 가 net_tracer ref 집합에 **없음**
     → `[spurious?]` 토큰 부착 + 주석(net_tracer 미검출 — 텍스트-only 부품 가능성,
        자동삭제 안 함). net_tracer 가 텍스트만 읽지 못하는 부품 오탐 방지를 위해
        **보수적으로 플래그만**.
     → [C1 완화] net_tracer 가 그 designator 의 alpha-prefix 클래스를 **0개** 검출한
        경우(예: BGA ball·net 라벨을 ref 로 오인해 U-prefix ref 가 0 인 페이지)는
        net_tracer 사각지대로 보고 그 prefix 의 spurious 를 **억제**한다
        (`summary["suppressed_prefixes"]`). 정당 IC 의 거짓 spurious 오염 방지.
     → [C2] 한 라인에 confirmed 와 spurious 가 동시 발생하면 confirmed 우선,
        spurious 억제(`[vector-confirmed] [spurious?]` 동시부착 모순 제거,
        `summary["mixed_lines"]`).
  3. net_tracer named net 중 vision MD 에 없는 것
     → MD 본문에는 추가하지 않고 `summary["vector_only_nets"]` 목록으로만 보고.

출력 형식 계약(`### Figure N`, `PIN -> NET` 행, GFM, `\n\n---\n\n` 청크 결합)은
**절대 보존**. 표기는 기존 라인 끝에 플래그 토큰만 부착하며 라인을 삭제/재배열하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

# designator 파싱 — net_crosscheck **전용** 로컬 정규식(W3).
#
# vision_qa_ensemble._DESIGNATOR_RE 는 `U/Q/R/C/L/J/D/X` 만 인식한다. 그러나
# net_tracer 는 `M1`(머신/기구), `ANT1`(안테나), `Y1`(크리스털), `K1`(릴레이),
# `FB1`(페라이트비드), `SW1`(스위치), `TP1`(테스트포인트) 등 더 넓은 designator
# 클래스를 ref 로 내보낸다. ensemble 파서에 그대로 위임하면 이 클래스들이 **조용히
# 누락**되어 교차검증 기회를 잃는다(W3).
#
# 따라서 ensemble._DESIGNATOR_RE 를 **수정하지 않고**(앙상블 회귀 위험 회피) 본
# 모듈 전용 확장 패턴을 분리 정의한다. ensemble 위임은 폐지(로컬 패턴 단일 SSoT).
#
# alternation 순서: BGA ball(U24.M8) → 다문자 prefix(SW/TP/FB/ANT) → 단문자 prefix.
# 최장 매칭 우선을 위해 다문자 prefix 를 단문자보다 앞에 둔다(`ANT1` 이 `A`+`NT1`
# 처럼 쪼개지지 않게). 커버 못 하는 클래스(전원심볼·테스트네트 라벨 등)는 매칭되지
# 않으며 그 경우 보수적으로 누락(데이터 손실 방지 — 베이스 라인 유지).
_DESIG_RE = re.compile(
    r"\b("
    r"[UQ]\d+\.[A-Z]\d+"                  # BGA ball: U24.M8 / Q3.A1 (최장매칭 위해 선두)
    r"|ANT\d+|SW\d+|TP\d+|FB\d+"          # 다문자 prefix(W3): 안테나/스위치/테스트포인트/페라이트
    r"|U\d+[A-Z]?"                        # U24, U24A (게이트 접미 1자)
    r"|R\d+|C\d+|L\d+|J\d+|D\d+|Q\d+|X\d+"  # 기존 클래스
    r"|Y\d+|K\d+|M\d+"                    # W3: 크리스털/릴레이/기구(머신)
    r")\b"
)

# designator 의 alpha-prefix 추출(prefix별 net_tracer 검출 수 집계용 — C1).
# 예) "U24"->"U", "ANT1"->"ANT", "SW3"->"SW", "U24.M8"->"U".
_PREFIX_RE = re.compile(r"^([A-Z]+)")

# 정확 매칭(승격 판정용).
_UNVERIFIED_TOKEN_RE = re.compile(r"\[unverified(?:\s+range)?\]", re.IGNORECASE)
_UNREADABLE_TOKEN_RE = re.compile(r"\[unreadable\]", re.IGNORECASE)

# `PIN -> NET` 행 파서(refpin 배선 — C1/W1). vision MD 의 연결 표기 변형을 수용:
#   "U24 pin M8 -> DDR_DQ48"  /  "U24 M8 -> DDR_DQ48"  /  "U24.M8 -> DDR_DQ48"
# ref(designator) + pin + net 을 캡처. pin 은 영숫자(BGA ball: M8, A1, NC1 등).
_PIN_NET_RE = re.compile(
    r"\b([A-Z]{1,3}\d+)"                  # ref designator
    r"(?:\.([A-Z]?\d*[A-Z]?\d*)"          # BGA ball dotted form: U24.M8
    r"|\s*(?:pin\s+)?([A-Z]{0,2}\d+[A-Z]?))"  # 'pin M8' / 'M8' / '1'
    r"\s*-+>\s*"                          # -> / -->
    r"([A-Za-z0-9_]+)",                  # net name
    re.IGNORECASE,
)

# ── L-5: crosscheck summary dict 키 상수(SSoT) ───────────────────────────────
# summary dict 의 키를 문자열 리터럴로 흩뿌리면 producer(여기) ↔ consumer
# (extract_all_via_pdf.apply_netcheck/_netcheck_single_page)가 오타 시 조용히 0 을
# 집계한다(계약 침묵 위반). 키를 단일 상수로 고정해 양측이 같은 이름을 import 하도록 한다.
SK_VECTOR_CONFIRMED = "vector_confirmed"   # 벡터로 승격된 라인 수
SK_SPURIOUS_FLAGGED = "spurious_flagged"   # [spurious?] 부착 라인 수
SK_VECTOR_ONLY_NETS = "vector_only_nets"   # net_tracer 에만 있는 named net 수
SK_APPLIED = "applied"                     # net_tracer 결과를 실제 적용했는지
SK_REASON = "reason"                        # 미적용/적용 사유 문자열

# 이미 부착된 교차검증 토큰(중복 부착 방지).
_VECTOR_CONFIRMED = "[vector-confirmed]"
_SPURIOUS = "[spurious?]"
_SPURIOUS_NOTE = (
    "[spurious?] <!-- net_tracer 미검출 — 텍스트-only 부품 가능성, 자동삭제 안 함 -->"
)

# net_tracer OCR 아티팩트 net 접두(extraction_verifier._EXCLUDED_NET_PREFIXES 모사).
# named net 보고(규칙 3)에서 이들을 제외해 노이즈를 거른다.
_EXCLUDED_NET_PREFIXES = (
    "Net_", "Pin_",
    "PiUn", "PiJn", "PiLn", "PiMn",
    "COC", "COR", "PIU",
    "N_D_", "SUBV_", "DXT", "BF_C", "SGP",
)
# net_tracer 가상 라벨 connection 의 sentinel ref(실제 부품 아님 — ref 집합 제외).
_NET_LABEL_SENTINEL = "_NET_LABEL_"
# 단일/2문자 버스 라벨은 아티팩트(extraction_verifier._MAX_ARTIFACT_LEN 모사).
_MAX_ARTIFACT_LEN = 2

# 러너 경로(이 파일 기준 ../scripts/run_net_tracer.py).
_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "run_net_tracer.py",
)
# 러너 실행 파이썬: pdfplumber+pymupdf 의존이 있는 인터프리터 필요.
# 1순위 env NET_TRACER_PYTHON, 2순위 스킬 .venv, 3순위 현재 인터프리터.
_SKILL_VENV_PY = os.path.expanduser(
    "~/.claude/skills/pdf-to-kicad/.venv/bin/python"
)


def _is_runnable_python(path: str) -> bool:
    """경로가 실행가능 파일이고 이름이 python 류인지 가벼운 확인(I3, 진단성).

    degrade 는 이미 정상(서브프로세스 실패 시 안전 반환)이므로 강한 검증은 불필요.
    여기서는 (a) 실제 파일, (b) 실행 권한, (c) basename 에 'python' 포함만 본다
    (실제 실행/`--version` 호출은 비용·부작용 우려로 하지 않음).
    """
    if not os.path.isfile(path):
        return False
    if not os.access(path, os.X_OK):
        return False
    return "python" in os.path.basename(path).lower()


def _runner_python() -> str:
    env = os.getenv("NET_TRACER_PYTHON")
    if env and _is_runnable_python(env):
        return env
    if _is_runnable_python(_SKILL_VENV_PY):
        return _SKILL_VENV_PY
    return sys.executable


def _normalize_ref(ref: str) -> str:
    """레퍼런스 정규화 — cross_validator.normalize_ref 모사 + 게이트 접미 제거.

    공백·하이픈·밑줄 제거, 대문자, 그리고 `U24A`(게이트 접미) -> `U24` 로 비교 키화.
    BGA ball(`U24.M8`)은 ref 부분(`U24`)만 키로 쓴다.
    예) "U 1" -> "U1", "U24A" -> "U24", "U24.M8" -> "U24", "ANT1" -> "ANT1"

    주의: 본 함수는 designator 토큰(`U24`/`ANT1`)에만 적용한다. `ESP32-S3` 같은
    파트번호 문자열은 `_scan_designators` 가 토큰으로 뽑지 않으므로 교차검증 대상이
    아니다(I1 — 도달불가 예시 제거).
    """
    s = re.sub(r"[\s\-_]", "", ref).upper()
    # BGA ball: U24.M8 -> U24 (점 이후 절단)
    s = s.split(".", 1)[0]
    # 게이트 접미: 마지막이 단일 영문자이고 그 앞이 숫자면 제거(U24A -> U24).
    m = re.match(r"^([A-Z]{1,3}\d+)[A-Z]$", s)
    if m:
        s = m.group(1)
    return s


def _ref_prefix(nref: str) -> str:
    """정규화 ref 의 alpha-prefix 반환(C1 prefix 억제 키). 예) "U24"->"U", "ANT1"->"ANT"."""
    m = _PREFIX_RE.match(nref)
    return m.group(1) if m else ""


def _scan_designators(line: str) -> list[str]:
    """라인에서 designator 토큰 추출(net_crosscheck 전용 확장 패턴 — W3).

    `U/Q/R/C/L/J/D/X` 외에 `Y/K/M/ANT/SW/TP/FB` 클래스도 인식한다. ensemble 파서에
    위임하지 않고 본 모듈 `_DESIG_RE` 단일 SSoT 를 쓴다(앙상블 회귀 분리).
    커버하지 못하는 클래스(전원/그라운드 심볼, 순수 net 라벨 등)는 토큰으로 뽑히지
    않으며 보수적으로 누락된다(베이스 라인 유지).
    """
    seen: list[str] = []
    for m in _DESIG_RE.finditer(line):
        d = m.group(1)
        if d not in seen:
            seen.append(d)
    return seen


@dataclass
class CrosscheckResult:
    """net_crosscheck 결과.

    markdown : 교차검증 표기가 부착된 MD(applied=False 면 입력 vision_md 그대로).
    summary  : 카운트/사유 dict.
    applied  : net_tracer 결과로 실제 표기를 적용했는지 여부.
    """

    markdown: str
    summary: dict
    applied: bool


def _run_net_tracer(pdf_path: str, page: int, timeout: float) -> dict:
    """run_net_tracer.py 서브프로세스 호출(단일 페이지) → JSON dict.

    실패/타임아웃/파싱불가 시 {"ok": False, "reason": ...} 반환(비크래시).
    """
    if not os.path.isfile(_RUNNER):
        return {"ok": False, "reason": f"runner not found: {_RUNNER}"}
    try:
        proc = subprocess.run(
            [_runner_python(), _RUNNER, str(pdf_path), str(page)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"net_tracer timeout ({timeout}s)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subprocess error: {type(exc).__name__}: {exc}"}

    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "reason": f"empty stdout (rc={proc.returncode})"}
    # 러너는 마지막 줄에 JSON 을 출력. 안전하게 마지막 비어있지 않은 줄 파싱.
    last = ""
    for ln in out.splitlines():
        if ln.strip():
            last = ln.strip()
    try:
        return json.loads(last)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"json parse failed: {exc}"}


def run_net_tracer_range(
    pdf_path: str,
    start_page: int,
    end_page: int,
    timeout: float = 120.0,
) -> dict[int, dict]:
    """run_net_tracer.py 를 **범위로 1회** 호출 → {page: tracer_json} 맵 (M-7).

    러너가 PdfVectorParser 를 1회만 오픈하고 페이지당 JSON 1줄을 출력한다. 이 함수는
    그 줄들을 파싱해 page→json 으로 매핑한다. 전체 호출 실패(타임아웃/import 실패 등)
    시에는 범위의 **모든 페이지**에 동일한 degrade dict({"ok": False, ...})를 채워
    호출자가 페이지별로 안전 degrade 하도록 한다(누락 페이지 없음).

    Args:
        pdf_path  : 원본 PDF 경로.
        start_page: 1-based 시작 페이지(inclusive).
        end_page  : 1-based 끝 페이지(inclusive).
        timeout   : 서브프로세스 타임아웃(초). 범위 전체에 적용.

    Returns:
        {page(int): tracer_json(dict)} — start..end 모든 페이지 키 포함.
    """
    pages = list(range(start_page, end_page + 1))

    def _all_degraded(reason: str) -> dict[int, dict]:
        return {p: {"ok": False, "page": p, "reason": reason} for p in pages}

    if not pages:
        return {}
    if not os.path.isfile(_RUNNER):
        return _all_degraded(f"runner not found: {_RUNNER}")
    try:
        proc = subprocess.run(
            [_runner_python(), _RUNNER, str(pdf_path), str(start_page), str(end_page)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return _all_degraded(f"net_tracer timeout ({timeout}s)")
    except Exception as exc:  # noqa: BLE001
        return _all_degraded(f"subprocess error: {type(exc).__name__}: {exc}")

    out = (proc.stdout or "").strip()
    if not out:
        return _all_degraded(f"empty stdout (rc={proc.returncode})")

    # 페이지별 JSON 라인 파싱 → page 키로 매핑. 누락 페이지는 degrade 로 채움.
    result: dict[int, dict] = {}
    for ln in out.splitlines():
        s = ln.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:  # noqa: BLE001 — 한 줄 파싱 실패는 무시(다른 줄은 유효 가능)
            continue
        pg = obj.get("page")
        if isinstance(pg, int):
            result[pg] = obj
    # 범위 내 응답 없는 페이지는 degrade dict 로 보강(완전성).
    for p in pages:
        if p not in result:
            result[p] = {"ok": False, "page": p, "reason": "no tracer line for page"}
    return result


@dataclass
class _TracerIndex:
    """net_tracer JSON 색인(교차검증 대조용).

    refs            : 정규화 ref 집합(connection.ref + no_connect.ref).
    named_nets      : 아티팩트 접두 제외, 길이>2 인 net name 원형 집합.
    refpin          : f"{norm_ref}|{PIN}" 키 집합 — PIN→NET 행 대조(C1/W1).
    refs_by_prefix  : alpha-prefix별 검출 ref 수(C1 blind-spot 억제 키).
    """

    refs: set[str] = field(default_factory=set)
    named_nets: set[str] = field(default_factory=set)
    refpin: set[str] = field(default_factory=set)
    refs_by_prefix: dict = field(default_factory=dict)


def _build_index(tracer_json: dict) -> _TracerIndex:
    """net_tracer JSON → _TracerIndex(refs / named_nets / refpin / refs_by_prefix).

    - refs: connection 의 ref + no_connect 의 ref(있으면), sentinel/추론 제외 후 정규화.
    - named_nets: 아티팩트 접두 제외, 길이>2 인 net name 원형.
    - refpin: f"{norm_ref}|{pin}" (PIN→NET 행 대조용 — C1/W1 실사용).
    - refs_by_prefix: prefix(U/R/ANT...)별 **고유 ref 수**. 0 이면 net_tracer 가
      그 designator-클래스를 못 뽑는 사각지대 → 해당 클래스 spurious 억제(C1).
    """
    idx = _TracerIndex()

    def _add_ref(nref: str) -> None:
        if not nref:
            return
        if nref not in idx.refs:
            idx.refs.add(nref)
            pfx = _ref_prefix(nref)
            if pfx:
                idx.refs_by_prefix[pfx] = idx.refs_by_prefix.get(pfx, 0) + 1

    for net in tracer_json.get("nets", []):
        name = str(net.get("name", ""))
        if (
            len(name) > _MAX_ARTIFACT_LEN
            and not any(name.startswith(p) for p in _EXCLUDED_NET_PREFIXES)
        ):
            idx.named_nets.add(name)
        for c in net.get("connections", []):
            ref = str(c.get("ref", ""))
            if not ref or ref == _NET_LABEL_SENTINEL:
                continue
            nref = _normalize_ref(ref)
            _add_ref(nref)
            if nref:
                pin = str(c.get("pin", ""))
                if pin:
                    idx.refpin.add(f"{nref}|{pin.upper()}")

    for nc in tracer_json.get("no_connects", []):
        ref = nc.get("ref")
        if ref and ref != _NET_LABEL_SENTINEL:
            _add_ref(_normalize_ref(str(ref)))

    return idx


def _line_has_token(line: str, token: str) -> bool:
    return token in line


def _pin_net_matches(line: str, idx: "_TracerIndex") -> bool:
    """`PIN -> NET` 행을 net_tracer refpin/named net 과 대조(C1/W1 refpin 실사용).

    라인의 각 `REF[.|pin] PIN -> NET` 표기에서:
      - (정규화 ref, PIN) 키가 idx.refpin 에 있으면 → True(핀 단위 벡터 일치).
      - net 명이 idx.named_nets 에 있으면 → True(넷 단위 벡터 일치).
    하나라도 일치하면 그 라인은 벡터 확인됨. 일치 없으면 False(보강 안 함 — 보수적).
    """
    for m in _PIN_NET_RE.finditer(line):
        ref = m.group(1) or ""
        # pin 은 dotted(group2) 또는 'pin X'/'X'(group3) 중 매칭된 쪽.
        pin = (m.group(2) or m.group(3) or "").strip()
        net = (m.group(4) or "").strip()
        nref = _normalize_ref(ref)
        if nref and pin and f"{nref}|{pin.upper()}" in idx.refpin:
            return True
        if net and net in idx.named_nets:
            return True
    return False


def crosscheck(
    vision_md: str,
    pdf_path: str,
    page: int,
    timeout: float = 120.0,
) -> CrosscheckResult:
    """vision QA MD ↔ net_tracer 넷리스트 교차검증(플래그만, 안전 degrade).

    net_tracer 를 해당 페이지에 대해 호출한 뒤 crosscheck_with_tracer 로 위임한다.
    pre-fetch 한 tracer JSON 이 이미 있으면(M-7 범위 1회 호출) crosscheck_with_tracer 를
    직접 호출해 서브프로세스 재실행을 피할 수 있다.

    Args:
        vision_md : vision QA(단일/앙상블) 산출 교정 Markdown.
        pdf_path  : 원본 PDF 경로.
        page      : 1-based 페이지 번호(net_tracer 대상 페이지).
        timeout   : 러너 서브프로세스 타임아웃(초).

    Returns:
        CrosscheckResult. net_tracer 무력/실패/빈 결과면 vision_md 그대로(applied=False).
    """
    tracer = _run_net_tracer(pdf_path, page, timeout)
    return crosscheck_with_tracer(vision_md, tracer)


def crosscheck_with_tracer(vision_md: str, tracer: dict) -> CrosscheckResult:
    """pre-fetch 된 net_tracer JSON 으로 교차검증(서브프로세스 호출 없음 — M-7).

    crosscheck 의 순수 로직 부분. tracer 는 run_net_tracer_range/ _run_net_tracer 가
    낸 dict({"ok": bool, "nets": [...], ...}). tracer.ok=False 이거나 빈 넷리스트면
    vision_md 그대로(applied=False) — 안전 degrade.

    Args:
        vision_md : vision QA(단일/앙상블) 산출 교정 Markdown.
        tracer    : run_net_tracer_range(또는 _run_net_tracer) 가 낸 페이지 JSON dict.

    Returns:
        CrosscheckResult(markdown, summary, applied).
    """
    base_summary = {
        SK_VECTOR_CONFIRMED: 0,
        SK_SPURIOUS_FLAGGED: 0,
        SK_VECTOR_ONLY_NETS: 0,
        SK_APPLIED: False,
        SK_REASON: "",
    }

    if not tracer.get("ok"):
        base_summary[SK_REASON] = tracer.get("reason", "net_tracer not ok")
        return CrosscheckResult(markdown=vision_md, summary=base_summary, applied=False)

    idx = _build_index(tracer)
    if not idx.refs and not idx.named_nets:
        base_summary[SK_REASON] = "net_tracer returned empty netlist"
        return CrosscheckResult(markdown=vision_md, summary=base_summary, applied=False)

    refs = idx.refs
    named_nets = idx.named_nets

    # C1: net_tracer 가 어떤 designator-prefix 도 못 뽑은 vision designator 의 prefix
    #     집합을 미리 산출한다(blind-spot). 그 prefix designator 의 spurious 는 억제한다
    #     — net_tracer 가 BGA ball/net 라벨을 ref 로 오인하는 페이지에서 U-prefix ref 가
    #     0개가 되어 정당 IC 가 전부 [spurious?] 로 오염되는 거짓양성을 막는다.
    vision_prefixes: set[str] = set()
    for raw_line in vision_md.splitlines():
        for desig in _scan_designators(raw_line):
            nref = _normalize_ref(desig)
            if nref:
                vision_prefixes.add(_ref_prefix(nref))
    suppressed_prefixes = sorted(
        p for p in vision_prefixes
        if p and idx.refs_by_prefix.get(p, 0) == 0
    )
    suppressed_set = set(suppressed_prefixes)

    # MD 라인별로 designator 표기 부착(플래그만, 라인 삭제·재배열 금지).
    vision_refs: set[str] = set()
    out_lines: list[str] = []
    vector_confirmed = 0
    spurious_flagged = 0
    mixed_lines = 0  # C2: confirmed+spurious 가 같은 라인에서 충돌해 억제된 라인 수.
    comment_spurious_suppressed = 0  # GFM 블록쿼트(`>`) 행 spurious 억제 건수.

    for raw_line in vision_md.splitlines():
        desigs = _scan_designators(raw_line)
        # refpin 보강(C1/W1): designator 가 없어도 PIN -> NET 행은 벡터 대조 대상.
        has_pin_net = bool(_PIN_NET_RE.search(raw_line))
        if not desigs and not has_pin_net:
            out_lines.append(raw_line)
            continue

        line = raw_line
        has_unverified = bool(_UNVERIFIED_TOKEN_RE.search(line))
        has_unreadable = bool(_UNREADABLE_TOKEN_RE.search(line))

        line_confirmed = False
        line_spurious = False

        for desig in desigs:
            nref = _normalize_ref(desig)
            if not nref:
                continue
            vision_refs.add(nref)
            in_tracer = nref in refs

            # 규칙 1: 미확정 플래그 라인의 designator 가 벡터에 존재 → 승격.
            if in_tracer and (has_unverified or has_unreadable):
                line_confirmed = True
            # 규칙 2: 벡터에 전혀 없는 designator → spurious 의심.
            #   단 C1: net_tracer 가 해당 prefix 클래스를 0개 검출(사각지대)이면 억제.
            if not in_tracer and _ref_prefix(nref) not in suppressed_set:
                line_spurious = True

        # refpin 배선(C1/W1): `PIN -> NET` 행을 net_tracer refpin/named net 과 대조.
        #   (ref,pin) 또는 net 명이 벡터와 일치하면 [vector-confirmed] 보강.
        if has_pin_net and _pin_net_matches(raw_line, idx):
            line_confirmed = True

        # C2: 한 라인에 confirmed 가 1개 이상이면 같은 라인 spurious 를 억제한다
        #     (`[vector-confirmed] [spurious?]` 동시 부착 모순 제거). 존재(confirmed)는
        #     더 강한 신호 — 다중 designator 그룹 라인의 거짓 spurious 를 차단.
        if line_confirmed and line_spurious:
            line_spurious = False
            mixed_lines += 1

        # GFM 블록쿼트(`>`) 행 spurious 억제: 서술/노트 문맥의 designator 오탐 방지.
        # `[vector-confirmed]` 는 무해한 정보이므로 블록쿼트 행에서도 유지한다.
        if line_spurious and raw_line.lstrip().startswith(">"):
            line_spurious = False
            comment_spurious_suppressed += 1

        # 표기 부착(중복 방지).
        if line_confirmed and not _line_has_token(line, _VECTOR_CONFIRMED):
            line = line.rstrip("\n") + " " + _VECTOR_CONFIRMED
            vector_confirmed += 1
        if line_spurious and not _line_has_token(line, _SPURIOUS):
            line = line.rstrip("\n") + " " + _SPURIOUS_NOTE
            spurious_flagged += 1

        out_lines.append(line)

    # 규칙 3: net_tracer named net 중 vision 에 등장하지 않는 것 → summary 만.
    # vision 등장 판정: net name 토큰이 MD 어디든 문자열로 존재하는지(보수적 substring).
    vector_only = sorted(
        n for n in named_nets if n not in vision_md
    )

    # I2: splitlines 는 말미 빈 라인(트레일링 개행)을 소거하되, 트레일링 개행이 2개
    #     이상이면 그 사이의 빈 토큰(`''`)이 본문 라인으로 남는다. 예) "a\n\n" ->
    #     ['a', ''] -> join("\n") = "a\n" 처럼 join 이 트레일링 개행 일부를
    #     **이미 재생성**한다. 여기에 trailing 을 그대로 더하면 2개→3개로 **중복 가산**
    #     (멀티페이지 루프에서 매 페이지 2배 증가). 따라서 join 본문의 말미 개행을 먼저
    #     제거한 뒤 원본 트레일링 개행 **개수**만큼 1회만 부착한다(round-trip 정확).
    trailing = len(vision_md) - len(vision_md.rstrip("\n"))
    core = "\n".join(out_lines).rstrip("\n")
    new_md = core + ("\n" * trailing)

    summary = {
        SK_VECTOR_CONFIRMED: vector_confirmed,
        SK_SPURIOUS_FLAGGED: spurious_flagged,
        SK_VECTOR_ONLY_NETS: len(vector_only),
        "vector_only_net_names": vector_only,
        "suppressed_prefixes": suppressed_prefixes,
        "mixed_lines": mixed_lines,
        "comment_spurious_suppressed": comment_spurious_suppressed,
        "tracer_stats": tracer.get("stats", {}),
        SK_APPLIED: True,
        SK_REASON: "ok",
    }
    return CrosscheckResult(markdown=new_md, summary=summary, applied=True)
