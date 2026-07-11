#!/usr/bin/env python3
"""test_net_crosscheck.py — lib.net_crosscheck 단위 테스트(서브프로세스/claude 호출 없음).

net_tracer 서브프로세스는 `_run_net_tracer` 를 monkeypatch 하여 mock JSON 으로 대체한다.
또한 run_net_tracer.py 는 인터페이스 계약(JSON 스키마) 정적 검증만 수행한다.

실행: python tests/test_net_crosscheck.py    (PASS/FAIL 요약 출력, 실패 시 exit 1)
"""
from __future__ import annotations

import os
import sys

# 워크스페이스 루트 import 경로(lib.* 패키지 import 보장).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import net_crosscheck as nc  # noqa: E402


# ── 테스트 픽스처 ────────────────────────────────────────────────────────────
_MOCK_TRACER_OK = {
    "ok": True,
    "page": 11,
    "nets": [
        {
            "name": "DDR_DQ48",
            "connections": [{"ref": "U24", "pin": "M8"}],
            "label_count": 1,
            "wire_count": 5,
        },
        {
            # 3자 이상 named net(2자 이하는 _MAX_ARTIFACT_LEN 으로 아티팩트 제외됨).
            "name": "VSS",
            "connections": [
                {"ref": "U24", "pin": "A1"},
                {"ref": "_NET_LABEL_", "pin": "VSS"},  # sentinel — ref 집합 제외
            ],
            "label_count": 1,
            "wire_count": 12,
        },
        {
            "name": "Net_0007",  # 아티팩트 net — named net 보고 제외
            "connections": [{"ref": "R5", "pin": "1"}],
            "label_count": 0,
            "wire_count": 1,
        },
    ],
    "no_connects": [{"x": 1.0, "y": 2.0, "ref": "U24", "pin": "NC1"}],
    "junctions": [],
    "stats": {"lines": 2477, "texts": 1193, "total_nets": 3, "named_nets": 2},
}

_MOCK_TRACER_RASTER = {
    "ok": False,
    "reason": "no vector lines on page (raster/scanned?) — net_tracer inert",
}

# C1 재현: net_tracer 가 U-prefix ref 를 0개 검출한 페이지(BGA ball/net 라벨을 ref 로
# 오인). R/C 류는 정상 검출. → U24 등은 spurious 억제, R99 는 정상 spurious.
_MOCK_TRACER_NO_U = {
    "ok": True,
    "page": 7,
    "nets": [
        {
            "name": "DDR_DQ48",
            "connections": [
                {"ref": "R5", "pin": "1"},
                {"ref": "C12", "pin": "2"},
            ],
        },
        {
            "name": "VDD_CORE",
            "connections": [{"ref": "R5", "pin": "2"}],
        },
    ],
    "no_connects": [],
    "junctions": [],
    "stats": {"lines": 100, "texts": 50, "total_nets": 2, "named_nets": 2},
}

# W3 재현: net_tracer 가 Y1(크리스털)/M1(기구)/ANT1(안테나) ref 를 내보냄.
_MOCK_TRACER_W3 = {
    "ok": True,
    "page": 3,
    "nets": [
        {
            "name": "XTAL_IN",
            "connections": [
                {"ref": "Y1", "pin": "1"},
                {"ref": "M1", "pin": "MNT"},
                {"ref": "ANT1", "pin": "FEED"},
            ],
        },
    ],
    "no_connects": [],
    "junctions": [],
    "stats": {"lines": 80, "texts": 30, "total_nets": 1, "named_nets": 1},
}


def _patch_tracer(monkeypatch_value):
    """nc._run_net_tracer 를 고정 dict 반환으로 치환(반환 복원자 제공)."""
    orig = nc._run_net_tracer
    nc._run_net_tracer = lambda pdf_path, page, timeout=120.0: monkeypatch_value  # type: ignore
    return orig


def _restore_tracer(orig):
    nc._run_net_tracer = orig  # type: ignore


# ── 테스트 케이스 ────────────────────────────────────────────────────────────
_RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(cond), detail))


def test_vector_confirmed():
    """vision `[unverified]` designator(U24) 가 net_tracer 에 존재 → [vector-confirmed]."""
    md = (
        "### Figure 1: DDR block\n"
        "| Designator | Value |\n|---|---|\n"
        "| U24 | [unverified] |\n"
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    _check("vector_confirmed: applied=True", res.applied, str(res.summary))
    _check("vector_confirmed: token present",
           "[vector-confirmed]" in res.markdown, res.markdown)
    _check("vector_confirmed: count>=1",
           res.summary.get("vector_confirmed", 0) >= 1, str(res.summary))


def test_spurious_flag():
    """vision U99(net_tracer 없음) → [spurious?] 표기 + 자동삭제 안 함 주석."""
    md = (
        "### Figure 1\n"
        "| Designator | Value |\n|---|---|\n"
        "| U99 | 10K |\n"
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    _check("spurious: [spurious?] present", "[spurious?]" in res.markdown, res.markdown)
    _check("spurious: no-autodelete note present",
           "자동삭제 안 함" in res.markdown, res.markdown)
    _check("spurious: count>=1",
           res.summary.get("spurious_flagged", 0) >= 1, str(res.summary))
    _check("spurious: U99 line not deleted", "U99" in res.markdown, res.markdown)


def test_ref_normalization():
    """U24A(게이트 접미) ↔ U24 매칭 — net_tracer U24 로 vector-confirmed."""
    md = (
        "### Figure 1\n"
        "| Designator | Value |\n|---|---|\n"
        "| U24A | [unreadable] |\n"
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    _check("normalize: U24A->U24 confirmed",
           "[vector-confirmed]" in res.markdown, res.markdown)
    _check("normalize: not spurious",
           "[spurious?]" not in res.markdown, res.markdown)
    # 직접 정규화 함수 단위 검증.
    _check("normalize: _normalize_ref(U24A)==U24",
           nc._normalize_ref("U24A") == "U24", nc._normalize_ref("U24A"))
    _check("normalize: _normalize_ref(U24.M8)==U24",
           nc._normalize_ref("U24.M8") == "U24", nc._normalize_ref("U24.M8"))
    _check("normalize: _normalize_ref(ESP32-S3)==ESP32S3",
           nc._normalize_ref("ESP32-S3") == "ESP32S3", nc._normalize_ref("ESP32-S3"))


def test_raster_degrade():
    """ok:false(래스터) → vision_md 그대로, applied=False(degrade)."""
    md = "### Figure 1\n| U24 | [unverified] |\n"
    orig = _patch_tracer(_MOCK_TRACER_RASTER)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    _check("raster: applied=False", res.applied is False, str(res.summary))
    _check("raster: markdown unchanged", res.markdown == md, res.markdown)
    _check("raster: no tokens added",
           "[vector-confirmed]" not in res.markdown
           and "[spurious?]" not in res.markdown, res.markdown)
    _check("raster: reason recorded", bool(res.summary.get("reason")), str(res.summary))


def test_format_contract_preserved():
    """형식 계약 보존(### Figure / PIN -> NET 라인) + 자동삭제 없음."""
    md = (
        "### Figure 2: MICTOR connector\n"
        "**Type**: circuit schematic\n\n"
        "**Relations / Connections**:\n"
        "- U24 pin M8 -> DDR_DQ48\n"
        "- U99 pin 1 -> SOME_NET\n"
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    _check("format: ### Figure preserved",
           "### Figure 2: MICTOR connector" in res.markdown, res.markdown)
    _check("format: PIN -> NET row preserved",
           "-> DDR_DQ48" in res.markdown and "-> SOME_NET" in res.markdown,
           res.markdown)
    _check("format: **Relations** heading preserved",
           "**Relations / Connections**:" in res.markdown, res.markdown)
    # 라인 수는 그대로(삭제 없음). 토큰만 부착되므로 splitlines 길이 동일.
    _check("format: line count unchanged (no deletion)",
           len(res.markdown.splitlines()) == len(md.splitlines()),
           f"{len(res.markdown.splitlines())} vs {len(md.splitlines())}")
    # U24 행은 confirmed(존재) — U24 는 unverified 플래그 없으나 PIN->NET 행이라
    # 미확정 플래그가 없으면 confirmed 부착 대상이 아님(승격은 플래그 라인 한정).
    # U99 는 net_tracer 부재 → spurious.
    _check("format: U99 line spurious-flagged",
           "[spurious?]" in res.markdown, res.markdown)


def test_vector_only_nets_report_only():
    """net_tracer named net 중 vision 에 없는 것 → summary 만(MD 본문 미추가)."""
    md = "### Figure 1\n| U24 | DDR_DQ48 |\n"  # VSS net 은 MD 에 없음
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    # VSS 는 named net(3자)이고 MD 에 없음 → vector_only 로 보고되어야.
    names = res.summary.get("vector_only_net_names", [])
    _check("vector_only: VSS reported", "VSS" in names, str(names))
    # DDR_DQ48 은 MD 에 있으므로 vector_only 아님.
    _check("vector_only: DDR_DQ48 not reported", "DDR_DQ48" not in names, str(names))
    # MD 본문에 VSS 가 자동 추가되지 않음(보수적).
    _check("vector_only: MD body unchanged (no auto-add of VSS net)",
           "VSS" not in res.markdown, res.markdown)
    # Net_0007(아티팩트)은 named net 집합에서 제외 → vector_only 미보고.
    _check("vector_only: artifact Net_0007 excluded",
           "Net_0007" not in names, str(names))


def test_runner_interface_contract():
    """run_net_tracer.py 정적 인터페이스 계약 — 모듈 로드 + JSON 스키마 키 확인.

    (서브프로세스 mock 어려움 → import + 함수 존재/스키마 키 계약만 검증.)
    """
    import importlib.util
    runner_path = os.path.join(_ROOT, "scripts", "run_net_tracer.py")
    _check("runner: file exists", os.path.isfile(runner_path), runner_path)
    spec = importlib.util.spec_from_file_location("run_net_tracer", runner_path)
    mod = importlib.util.module_from_spec(spec)
    # import 시점에 부작용 없음(if __name__ guard) — 안전 로드.
    spec.loader.exec_module(mod)  # type: ignore
    _check("runner: has main()", callable(getattr(mod, "main", None)), "")
    _check("runner: has _build_vector_raw()",
           callable(getattr(mod, "_build_vector_raw", None)), "")
    _check("runner: has _emit()", callable(getattr(mod, "_emit", None)), "")
    # 잘못된 argv → ok:false JSON(비크래시) 계약.
    import io
    import json as _json
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = mod.main(["run_net_tracer.py"])  # argv 부족
    finally:
        sys.stdout = old
    _check("runner: bad argv returns rc 0 (non-crash)", rc == 0, str(rc))
    try:
        payload = _json.loads(buf.getvalue().strip().splitlines()[-1])
        _check("runner: bad argv -> ok:false", payload.get("ok") is False, str(payload))
        _check("runner: ok:false has reason", "reason" in payload, str(payload))
    except Exception as e:  # noqa: BLE001
        _check("runner: bad argv emits valid JSON", False, f"{e}: {buf.getvalue()!r}")


def test_c1_prefix_suppression():
    """C1: net_tracer U-prefix ref 0개(blind-spot) → U24 [unverified] 의 spurious 억제.

    동시에 R 류는 정상 검출되므로 R99(미검출)는 정상 spurious 유지(억제 분리 확인).
    """
    md = (
        "### Figure 1: DDR\n"
        "| Designator | Value |\n|---|---|\n"
        "| U24 | [unverified] |\n"   # U-prefix: net_tracer 0개 → 억제 대상
        "| R99 | 10K |\n"            # R-prefix: net_tracer 검출(R5) → 정상 spurious
    )
    orig = _patch_tracer(_MOCK_TRACER_NO_U)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 7)
    finally:
        _restore_tracer(orig)
    # U24 라인은 spurious 억제(거짓 spurious 제거).
    u_line = [l for l in res.markdown.splitlines() if "U24" in l][0]
    _check("c1: U24 spurious suppressed", "[spurious?]" not in u_line, u_line)
    # R99 라인은 정상 spurious(R prefix 는 검출되므로 사각지대 아님).
    r_line = [l for l in res.markdown.splitlines() if "R99" in l][0]
    _check("c1: R99 still spurious", "[spurious?]" in r_line, r_line)
    # summary 에 억제된 prefix 기록.
    _check("c1: suppressed_prefixes records U",
           "U" in res.summary.get("suppressed_prefixes", []),
           str(res.summary.get("suppressed_prefixes")))
    _check("c1: R not in suppressed_prefixes",
           "R" not in res.summary.get("suppressed_prefixes", []),
           str(res.summary.get("suppressed_prefixes")))


def test_c1_refpin_confirmed():
    """C1/W1: `PIN -> NET` 행이 net_tracer refpin/named net 과 일치 → [vector-confirmed]."""
    md = (
        "### Figure 2: connections\n"
        "**Relations / Connections**:\n"
        "- U24 pin M8 -> DDR_DQ48\n"   # (U24,M8) refpin 일치 + DDR_DQ48 named net 일치
        "- U24.A1 -> VSS\n"            # (U24,A1) refpin 일치(dotted form)
        "- U24 pin Z9 -> UNKNOWN_NET\n"  # refpin 불일치 + net 불일치 → 보강 안 됨
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    m8_line = [l for l in res.markdown.splitlines() if "M8" in l][0]
    _check("c1-refpin: U24 M8 confirmed", "[vector-confirmed]" in m8_line, m8_line)
    a1_line = [l for l in res.markdown.splitlines() if "A1" in l][0]
    _check("c1-refpin: U24.A1 confirmed (dotted)",
           "[vector-confirmed]" in a1_line, a1_line)
    # 불일치 행: U24 는 ref 집합에 있으나 refpin/net 불일치 → confirmed 보강 안 됨.
    # (U24 자체가 ref 집합에 있어 spurious 도 아님 — 토큰 없음이 정상.)
    z9_line = [l for l in res.markdown.splitlines() if "Z9" in l][0]
    _check("c1-refpin: unmatched pin not confirmed",
           "[vector-confirmed]" not in z9_line, z9_line)
    _check("c1-refpin: vector_confirmed count>=2",
           res.summary.get("vector_confirmed", 0) >= 2, str(res.summary))


def test_c2_no_simultaneous_tokens():
    """C2: 다중 designator 라인(U24 and U99)에 confirmed+spurious 동시 부착 안 됨."""
    md = (
        "### Figure 1\n"
        "| Designators | Note |\n|---|---|\n"
        "| U24 and U99 | [unverified] |\n"  # U24 존재(confirmed), U99 미존재(spurious)
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    line = [l for l in res.markdown.splitlines() if "U24 and U99" in l][0]
    # U24 가 net_tracer 에 존재 + [unverified] → confirmed. U99 미존재 → spurious 후보.
    # C2: 같은 라인이므로 confirmed 우선, spurious 억제.
    _check("c2: confirmed present", "[vector-confirmed]" in line, line)
    _check("c2: spurious NOT also attached", "[spurious?]" not in line, line)
    _check("c2: mixed_lines recorded>=1",
           res.summary.get("mixed_lines", 0) >= 1, str(res.summary))


def test_w3_extended_designators():
    """W3: Y1/M1/ANT1 designator 스캔·매칭(net_tracer ref 와 교차검증)."""
    # 스캔 커버리지 직접 확인.
    scanned = nc._scan_designators("| Y1 | M1 | ANT1 | SW2 | TP3 | FB4 | K5 |")
    for d in ("Y1", "M1", "ANT1", "SW2", "TP3", "FB4", "K5"):
        _check(f"w3: scan covers {d}", d in scanned, str(scanned))
    # ANT1 이 A+NT1 로 쪼개지지 않는지(최장매칭) 확인.
    _check("w3: ANT1 not split into A/NT", "A" not in scanned and "ANT1" in scanned,
           str(scanned))
    # 교차검증: Y1/M1/ANT1 이 net_tracer 에 존재 → [unverified] 라인 confirmed.
    md = (
        "### Figure 1: oscillator\n"
        "| Designator | Value |\n|---|---|\n"
        "| Y1 | [unverified] |\n"
        "| M1 | [unverified] |\n"
        "| ANT1 | [unverified] |\n"
    )
    orig = _patch_tracer(_MOCK_TRACER_W3)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 3)
    finally:
        _restore_tracer(orig)
    for d in ("Y1", "M1", "ANT1"):
        dl = [l for l in res.markdown.splitlines() if l.strip().startswith("| " + d)][0]
        _check(f"w3: {d} vector-confirmed", "[vector-confirmed]" in dl, dl)


def test_i2_trailing_newlines_preserved():
    """I2: 원본 트레일링 개행(\\n\\n) 개수 보존(splitlines 소거 복원)."""
    md = (
        "### Figure 1\n"
        "| U24 | [unverified] |\n"
        "\n"   # 트레일링 빈 줄 → 원문 말미가 '\n\n'
    )
    assert md.endswith("\n\n")
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    # endswith("\n\n") 는 3개일 때도 참이라 중복가산 버그를 가린다 → **정확한 개수** 검증.
    def _trail(s: str) -> int:
        return len(s) - len(s.rstrip("\n"))
    _check("i2: trailing count exactly 2 (no doubling)", _trail(res.markdown) == 2,
           f"{_trail(res.markdown)} :: {res.markdown[-6:]!r}")
    # 단일 트레일링 개행 케이스도 1개 보존(과부착·소거 없음).
    md1 = "### Figure 1\n| U24 | [unverified] |\n"
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res1 = nc.crosscheck(md1, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    _check("i2: single trailing \\n preserved",
           res1.markdown.endswith("\n") and not res1.markdown.endswith("\n\n"),
           repr(res1.markdown[-5:]))
    _check("i2: single trailing count exactly 1", _trail(res1.markdown) == 1,
           str(_trail(res1.markdown)))
    # 3개 트레일링도 정확 보존(중복가산 회귀 방지).
    md3 = "### Figure 1\n| U24 | [unverified] |\n\n\n"
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res3 = nc.crosscheck(md3, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    _check("i2: trailing count exactly 3 preserved", _trail(res3.markdown) == 3,
           str(_trail(res3.markdown)))
    # 멀티페이지 루프 멱등성: 동일 MD 를 반복 crosscheck 해도 트레일링 개행 불변
    # (apply_netcheck 가 청크 범위 각 페이지에 current=res.markdown 으로 재적용).
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        cur = "### Figure 1\n| U24 | [unverified] |\n\n"
        trails = []
        for _ in range(4):
            cur = nc.crosscheck(cur, "/fake.pdf", 11).markdown
            trails.append(_trail(cur))
    finally:
        _restore_tracer(orig)
    _check("i2: multi-page loop trailing stable (no growth)",
           trails == [2, 2, 2, 2], str(trails))


def test_blockquote_spurious_suppressed():
    """`>` GFM 블록쿼트 행의 spurious 억제 + comment_spurious_suppressed 카운트.

    _MOCK_TRACER_OK 에는 U24(U-prefix 검출)가 있으므로 U-prefix 는 C1 억제 대상이 아님.
    U99 는 net_tracer 미검출 → 블록쿼트 행이면 spurious 억제, 일반 행이면 spurious 부착.
    """
    md = (
        "### Figure 1\n"
        "| Designator | Value |\n|---|---|\n"
        "> note: U99 mentioned here\n"   # 블록쿼트, net_tracer 미검출 → spurious 억제
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    bq_line = [l for l in res.markdown.splitlines() if l.startswith(">")][0]
    _check("bq_spurious: [spurious?] NOT attached to blockquote line",
           "[spurious?]" not in bq_line, bq_line)
    _check("bq_spurious: comment_spurious_suppressed>=1",
           res.summary.get("comment_spurious_suppressed", 0) >= 1, str(res.summary))


def test_non_blockquote_spurious_normal():
    """일반 표 행(`| U99 |`)은 블록쿼트 아님 → spurious 정상 부착(회귀 확인).

    _MOCK_TRACER_OK 에 U24(U-prefix)가 있으므로 U-prefix 는 C1 억제 대상이 아님.
    U99 는 net_tracer 미검출 + 일반 행 → [spurious?] 정상 부착.
    """
    md = (
        "### Figure 1\n"
        "| Designator | Value |\n|---|---|\n"
        "| U99 | 10K |\n"   # 일반 행, net_tracer 미검출, U-prefix C1 미억제 → 정상 spurious
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    u99_line = [l for l in res.markdown.splitlines() if "U99" in l][0]
    _check("non_bq_spurious: [spurious?] still attached to normal line",
           "[spurious?]" in u99_line, u99_line)
    _check("non_bq_spurious: comment_spurious_suppressed==0",
           res.summary.get("comment_spurious_suppressed", 0) == 0, str(res.summary))


def test_blockquote_vector_confirmed_kept():
    """`>` 블록쿼트 행이라도 net_tracer 검출 designator → [vector-confirmed] 유지."""
    md = (
        "### Figure 1\n"
        "| Designator | Value |\n|---|---|\n"
        "| U24 | [unverified] |\n"
        "> C2 confirmed net: U24 is present [unverified]\n"  # 블록쿼트 + U24 존재 + unverified
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    bq_line = [l for l in res.markdown.splitlines() if l.startswith(">")][0]
    _check("bq_confirmed: [vector-confirmed] retained in blockquote",
           "[vector-confirmed]" in bq_line, bq_line)
    _check("bq_confirmed: [spurious?] NOT attached to blockquote",
           "[spurious?]" not in bq_line, bq_line)


def test_info1_indented_blockquote_spurious_suppressed():
    """Info-1 (M3 kill): 들여쓰기 블록쿼트(`   > ...`) spurious 억제.

    앞에 공백 3칸 + `>` 형태. raw_line.lstrip().startswith(">") 로만 검출 가능.
    `.lstrip()` 이 제거되면 raw_line.startswith(">") 가 False 가 되어 억제 미발동,
    U99 에 [spurious?] 가 부착됨 → 이 테스트가 FAIL 해야 한다(M3 kill).

    _MOCK_TRACER_OK: U-prefix(U24) 검출 → U-prefix 는 C1 억제 대상 아님.
    U99 는 net_tracer 미검출 + 들여쓰기 블록쿼트 행 → spurious 억제, [spurious?] 미부착.
    """
    md = (
        "### Figure 1\n"
        "| Designator | Value |\n|---|---|\n"
        "   > note: U99 mentioned\n"   # 앞 3칸 공백 + `>`, net_tracer 미검출 U99
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    # 들여쓰기 블록쿼트 행: [spurious?] 미부착이어야 함.
    bq_line = [l for l in res.markdown.splitlines() if "U99" in l][0]
    _check("info1: [spurious?] NOT attached to indented blockquote",
           "[spurious?]" not in bq_line, bq_line)
    # comment_spurious_suppressed 카운터 >= 1 이어야 함.
    _check("info1: comment_spurious_suppressed>=1",
           res.summary.get("comment_spurious_suppressed", 0) >= 1, str(res.summary))


def test_info2_blockquote_confirmed_spurious_c2_priority():
    """Info-2 (M4 kill): 블록쿼트 행에 confirmed(U24) + spurious(U99) 동시 존재.

    스펙: C2(confirmed 우선 억제)가 블록쿼트 억제보다 **먼저** 적용된다.
    → confirmed(U24) 발동 + spurious(U99) 억제 = mixed_lines += 1,
      comment_spurious_suppressed 는 0 (블록쿼트 억제 분기에 도달 안 함).

    만약 블록쿼트 억제를 C2 앞으로 이동하면(M4 mutation):
      → spurious 가 블록쿼트 억제로 먼저 잡혀 comment_spurious_suppressed += 1,
        mixed_lines 는 0 이 됨 → 이 테스트의 두 assert 가 FAIL 해야 한다(M4 kill).

    _MOCK_TRACER_OK: U24 존재(confirmed), U99 미존재(spurious 후보).
    행: `> U24 [unverified] and U99` → unverified 플래그 있음, U24 confirmed, U99 spurious.
    """
    md = (
        "### Figure 1\n"
        "| Designator | Value |\n|---|---|\n"
        "> U24 [unverified] and U99 are on this net\n"  # 블록쿼트 + U24(confirmed) + U99(spurious)
    )
    orig = _patch_tracer(_MOCK_TRACER_OK)
    try:
        res = nc.crosscheck(md, "/fake.pdf", 11)
    finally:
        _restore_tracer(orig)
    # C2 우선: confirmed(U24) 발동 → spurious(U99) 억제 → mixed_lines == 1.
    _check("info2: mixed_lines==1 (C2 applied before blockquote suppression)",
           res.summary.get("mixed_lines", 0) == 1, str(res.summary))
    # 블록쿼트 억제 분기에 도달 안 함 → comment_spurious_suppressed == 0.
    _check("info2: comment_spurious_suppressed==0 (blockquote branch not reached)",
           res.summary.get("comment_spurious_suppressed", 0) == 0, str(res.summary))


def test_runner_python_validation():
    """I3: _runner_python 이 실행불가 경로를 무시하고 fallback(sys.executable)."""
    import sys as _sys
    saved = os.environ.get("NET_TRACER_PYTHON")
    try:
        # 디렉터리(실행 파일 아님) → 무시되고 fallback(스킬 venv 또는 sys.executable).
        os.environ["NET_TRACER_PYTHON"] = _ROOT
        chosen = nc._runner_python()
        _check("i3: invalid env ignored -> valid fallback",
               chosen != _ROOT and nc._is_runnable_python(chosen), chosen)
        # _is_runnable_python 단위 검증.
        _check("i3: dir not runnable", nc._is_runnable_python(_ROOT) is False, _ROOT)
        _check("i3: sys.executable runnable",
               nc._is_runnable_python(_sys.executable) is True, _sys.executable)
    finally:
        if saved is None:
            os.environ.pop("NET_TRACER_PYTHON", None)
        else:
            os.environ["NET_TRACER_PYTHON"] = saved


def main() -> int:
    tests = [
        test_vector_confirmed,
        test_spurious_flag,
        test_ref_normalization,
        test_raster_degrade,
        test_format_contract_preserved,
        test_vector_only_nets_report_only,
        test_runner_interface_contract,
        test_c1_prefix_suppression,
        test_c1_refpin_confirmed,
        test_c2_no_simultaneous_tokens,
        test_w3_extended_designators,
        test_i2_trailing_newlines_preserved,
        test_blockquote_spurious_suppressed,
        test_non_blockquote_spurious_normal,
        test_blockquote_vector_confirmed_kept,
        test_runner_python_validation,
        test_info1_indented_blockquote_spurious_suppressed,
        test_info2_blockquote_confirmed_spurious_c2_priority,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            _check(f"{t.__name__}: EXCEPTION", False, repr(e))

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print("=" * 64)
    for name, ok, detail in _RESULTS:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f"\n        detail: {detail[:300]}"
        print(line)
    print("=" * 64)
    print(f"{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
