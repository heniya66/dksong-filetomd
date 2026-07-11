"""Adversarial attack suite for _has_degenerate_repetition (remote filetomd).

Run on remote: .venv/bin/python adversarial_truncation.py
Prints compact PASS/FAIL table. Exit 1 if any case fails.
"""
import sys, glob
from fmdw.ollama_extractor import _has_degenerate_repetition as det

CASES = []  # (name, text, expected)

def add(name, text, expected):
    CASES.append((name, text, expected))

# ── MUST DETECT (True) — sneaky-bad cases ────────────────────────────────
add("A1 실사고형: 영어구절 x275 한줄", ("teaching evaluation teaching)) " * 275), True)
add("A2 한글 폭주 x200 한줄 (>=2000자)", ("평가 교육 평가)) 교육 " * 200), True)
add("A2b 한글 폭주 짧은줄(1560자) — MIN_LEN 사각지대 확인",
    ("평가 교육 평가)) 교육 " * 120), None)  # 관찰: 현재 False 예상(가드 미달)
add("A3 잡음 섞인 반복(반복20 + 다양한 사이잡음, >=2000자)",
    " ".join(f"noise{i} corrupt data loop corrupt data loop" for i in range(20))
    + " " + " ".join(f"filler{i}x{i*3}" for i in range(150)), True)
add("A4 두단어 루프 error mark x300", ("error mark " * 300), True)
add("A5 경계: 3-gram 정확히 20회 (>=2000자)",
    ("alpha beta gamma " * 20) + ("x" * 1700), True)
add("A6 멀티라인 끝줄에만 폭주",
    "정상 본문 첫 줄\n| a | b |\n" + ("repeat garbage token " * 150), True)
add("A7 기존 inter-line: 동일라인 x5", ("| Vth | 0.4 | V |\n" * 5), True)

# ── MUST NOT DETECT (False) — false-positive traps ───────────────────────
add("B1 경계: 3-gram 19회 (임계 미달)",
    ("alpha beta gamma " * 19) + ("y" * 1700) , False)  # 19 < 20
add("B2 경계갱신: 1999자 반복다수 (신임계 1000 초과 → 탐지)", ("loop word " * 199)[:1999], True)
add("B2b 경계: 999자 반복다수 (플로어 미달 → 미탐)", ("loop word " * 99)[:999], False)
add("B2c 한글 x60 (840자, 플로어 미달 → 미탐)", ("평가 교육 평가)) 교육 " * 60), False)
add("B3 GFM 숫자 동일셀 광폭행 (no alpha)", "| " + "1 | " * 800, False)
add("B4 마크다운 구분선 300열", "|" + " :--- |" * 300, False)
add("B5 HTML dense 무공백 행", "<td>1</td>" * 400, False)
add("B6 페이지 나열 Page 1..Page 400",
    " ".join(f"Page {i}" for i in range(1, 401)), False)
add("B7 정상 장문 단락(다양한 어휘 3000자)",
    " ".join(f"word{i} value{i*7%97} spec{i%13}" for i in range(400)), False)
add("B8 inter-line 동일라인 x3 (임계5 미달)", ("| Vth | 0.4 | V |\n" * 3), False)
add("B9 핀 교차나열 VDD VSS x30 (짧은줄)", ("VDD VSS " * 30), False)  # 240자 <2000
add("B10 빈/공백 텍스트", "  \n\n  \n", False)
add("B11 2000자 이상이지만 토큰 2개", ("a" * 1200) + " " + ("b" * 1200), False)

# ── Known-tradeoff probe (정보 수집용 — FAIL 아님, 관찰만) ────────────────
PROBES = [
    ("P1 GFM 동일 텍스트셀 광폭행 '| yes' x400", "| " + "yes | " * 400),
    ("P2 단위 반복 '10mA max 10mA typ' 스타일 x100", ("10mA max 10mA typ " * 100)),
]

fails = 0
print(f"{'case':46s} {'expect':7s} {'got':6s} verdict")
print("-" * 72)
for name, text, exp in CASES:
    got = det(text)
    if exp is None:  # 관찰 전용 (판정 제외)
        print(f"{name:46s} {'(obs)':7s} {str(got):6s} OBSERVE")
        continue
    ok = got == exp
    if not ok:
        fails += 1
    print(f"{name:46s} {str(exp):7s} {str(got):6s} {'PASS' if ok else '** FAIL **'}")

print("\n[관찰 프로브 — 트레이드오프 확인용]")
for name, text in PROBES:
    print(f"{name:46s} -> {det(text)}")

# ── 실전 코퍼스 전수 스윕 (라인 단위 = 신규 intra-line 경로만 검증) ────────
# 주의: det(문서전체)는 inter-line 검사가 정상적인 반복 줄('---' 등)에 걸리므로
# 청크 단위인 실운용과 다름. 신규 경로 검증은 라인별 호출이 정확.
print("\n[실전 코퍼스 스윕 — 라인 단위(intra-line 경로)]")
known_bad = "LN08LPU_Design_Manual_A00-V0.9.2.0_testpages.md"
files = sorted(set(glob.glob("output/**/*.md", recursive=True)
                   + glob.glob("_worklog/assets/*.md")))
hits = []  # (file, lineno, preview)
for f in files:
    try:
        t = open(f, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    for i, ln in enumerate(t.splitlines(), 1):
        if len(ln) >= 2000 and det(ln):
            hits.append((f, i, ln[:110].replace("\n", " ")))
print(f"검사 파일 수: {len(files)}, 탐지 라인: {len(hits)}")
unexpected = []
bad_found = False
for f, i, pv in hits:
    if known_bad in f:
        bad_found = True
        print(f"  [정상탐지] {f}:{i}")
    else:
        unexpected.append((f, i, pv))
        print(f"  [검토필요] {f}:{i}\n      preview: {pv}")
if not bad_found:
    print("** FAIL: 알려진 잘림 문서를 못 잡음 **"); fails += 1
if unexpected:
    print(f"** 예상외 탐지 라인 {len(unexpected)}건 — preview 로 진짜 폭주인지 판별 **")

print(f"\n총 {len(CASES)}케이스 중 실패 {fails}건, 코퍼스 예상외 {len(unexpected)}건")
sys.exit(1 if fails else 0)
