"""_golden_fixtures.py — 마이그레이션 골든의 "고정 fixture" 생성·로드 헬퍼.

배경 (왜 fixture 로 박제하나):
    Phase 1/2 통합본이 커밋(b05a124)되면서 HEAD 의 extract_*.py 에서
    process_pdf/extract_chunk 가 사라졌다. 기존 테스트는 골든 기준을
    `git show HEAD:<script>` → record_script(entry_fn="process_pdf") 로
    동적 캡처했기에, HEAD 에 process_pdf 가 없어 AttributeError 로 전부 실패한다.
    ("HEAD = 원본" 전제가 통합 커밋으로 붕괴.)

해결:
    통합 *직전* 커밋(b05a124^ = fbb61dc)의 원본 스크립트를 한 번 record_script 로
    녹화해 JSON fixture 로 박제한다. 이후 테스트는 git 의존 없이 fixture 를 로드해
    "원본 동작" 골든으로 사용한다.

비순환(non-vacuous) 보장:
    - fixture = 통합 직전 *원본*(process_pdf 보유)에서 1회 캡처된 독립 소스.
    - 검증 대상 = 현재 워킹트리 *통합본*이 호출하는 convert_pdf 파라미터.
    두 독립 소스를 비교하므로 순환이 없다.
    ⚠️ fixture 를 현재 HEAD/워킹트리 통합본으로 만들면 vacuous(자기 자신과 비교)다 —
       반드시 PRE_INTEGRATION_REF(통합 직전) 소스로만 생성한다(아래 generate_* 강제).

fixture 위치:
    tests/golden/migration_phase1/<script_stem>__<pages>p.json
    tests/golden/migration_phase2/<script_stem>__<pages>p.json

재생성 (원본 스크립트나 검증 범위가 바뀐 드문 경우에만):
    .venv/bin/python -m tests._golden_fixtures   # 또는 regenerate_all() 호출
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests._pipeline_recorder import PipelineSnapshot, record_script

# 통합 직전(원본 process_pdf 보유) 커밋 — fixture 생성의 유일한 기준.
PRE_INTEGRATION_REF = "fbb61dc"  # == b05a124^ (Phase 1/2 통합 직전)

# Phase 3(ava1/sim_platform/gemini_multimodal) 통합 직전 HEAD SHA.
# Phase 3 대상은 통합 시점까지 원본 그대로였으므로 "통합 직전 = 통합 작업 시작 HEAD".
# 통합 커밋이 이후 들어가도 fixture 기준이 고정되도록 SHA 하드코딩(비순환 보장).
PRE_INTEGRATION_REF_PHASE3 = "5b2b0a835e9c1734872442b981b7949b690fc6c8"

_GOLDEN_ROOT = Path(_THIS_DIR) / "golden"

# ── fixture 사양 ──────────────────────────────────────────────────────────────
#    모든 스크립트를 통합 직전 원본의 main() 진입으로 녹화한다(record_script).
#    이렇게 하면 통합본도 main() 진입으로 검증(record_migrated_script)하므로
#    원본·통합본이 **동일 진입·동일 PDF 소스·동일 출력경로**를 거쳐 fully-faithful
#    골든 비교가 된다(프롬프트·청크정책·출력경로 전부 소스에서 직접 읽힘 → teeth 강화).
#
#    PDF 소스:
#      - 하드코딩 PDF_PATH 스크립트(eda_pyojun/ava2/eda_pangdan): main() 인자 없음.
#        PDF_PATH 리터럴은 통합 전후 동일하므로 출력경로도 양쪽 동일.
#      - CLI(argv) 스크립트(blockdiagram/image_analysis): main() 이 sys.argv[1] 사용
#        → argv 필드로 동일 가짜 PDF 경로를 양쪽에 주입(출력경로 동일).
#    페이지 케이스는 각 테스트가 검증하던 값을 그대로 포함한다.
FIXTURE_SPECS = [
    # phase1
    {
        "phase": "migration_phase1",
        "script": "extract_pdf_blockdiagram.py",
        "stem": "extract_pdf_blockdiagram",
        "argv": ["extract_pdf_blockdiagram.py", "/fake/test.pdf"],
        "pages": [20, 21, 50],
    },
    {
        "phase": "migration_phase1",
        "script": "extract_eda_pyojun.py",
        "stem": "extract_eda_pyojun",
        "argv": None,   # 하드코딩 PDF_PATH
        "pages": [25, 26, 40],
    },
    # phase2
    {
        "phase": "migration_phase2",
        "script": "extract_ava2.py",
        "stem": "extract_ava2",
        "argv": None,   # 하드코딩 PDF_PATH
        "pages": [25, 26, 40],
    },
    {
        "phase": "migration_phase2",
        "script": "extract_eda_pangdan.py",
        "stem": "extract_eda_pangdan",
        "argv": None,   # 하드코딩 PDF_PATH
        "pages": [25, 26, 40],
    },
    {
        "phase": "migration_phase2",
        "script": "extract_pdf_image_analysis.py",
        "stem": "extract_pdf_image_analysis",
        "argv": ["extract_pdf_image_analysis.py", "/fake/img.pdf"],
        "pages": [25, 26, 50],
    },
    # phase3 (고위험 — ref 는 Phase 3 통합 직전 HEAD SHA)
    {
        # ava1: 원본은 start,end=1,total_pages 로 **항상 단일 청크**(페이지 수 무관).
        # 26p·50p 도 1청크여야 함(보존 검증 케이스). main() 은 하드코딩 PDF_PATH.
        "phase": "migration_phase3",
        "script": "extract_ava1.py",
        "stem": "extract_ava1",
        "ref": PRE_INTEGRATION_REF_PHASE3,
        "argv": None,
        "pages": [10, 26, 50],
    },
    {
        # sim_platform: 20p 단위 분할(항상). main() 은 하드코딩 PDF_PATH.
        # renumber_images 는 청크별 적용되나 extract 호출 시퀀스엔 영향 없음.
        "phase": "migration_phase3",
        "script": "extract_sim_platform.py",
        "stem": "extract_sim_platform",
        "ref": PRE_INTEGRATION_REF_PHASE3,
        "argv": None,
        "pages": [20, 21, 50],
    },
    {
        # gemini_multimodal: main() 이 input/pdf/*.pdf 글롭이라 비결정적 →
        # 단위 함수 process_pdf(pdf_path) 로 녹화. 원본은 임시 _chunk_NNN.md 를
        # 썼다 읽어 병합하므로 passthrough_read=True 로 read-back 재현,
        # output_match=".md" 의 *마지막* write(최종 {stem}.md) 를 출력으로 선택.
        "phase": "migration_phase3",
        "script": "extract_gemini_multimodal.py",
        "stem": "extract_gemini_multimodal",
        "ref": PRE_INTEGRATION_REF_PHASE3,
        "entry_fn": "process_pdf",
        "entry_args": (Path("/fake/gemini.pdf"),),
        "passthrough_read": True,
        "output_match": "gemini.md",
        "pages": [20, 21, 50],
    },
]

# 테스트(통합본 검증)에서 동일 argv 를 써야 출력경로가 fixture 와 일치한다.
SCRIPT_ARGV = {s["stem"]: s.get("argv") for s in FIXTURE_SPECS}

# 통합본 검증 시 사용할 진입점 정보(gemini 처럼 main 글롭이 비결정적인 경우 process_pdf 사용).
SCRIPT_ENTRY = {
    s["stem"]: {
        "entry_fn": s.get("entry_fn", "main"),
        "entry_args": s.get("entry_args", ()),
    }
    for s in FIXTURE_SPECS
}


def _git_show_ref(ref: str, rel_path: str) -> str:
    """git show <ref>:<rel_path> 로 특정 커밋의 소스를 반환."""
    r = subprocess.run(
        ["git", "show", f"{ref}:{rel_path}"],
        capture_output=True, text=True, cwd=_ROOT, check=True,
    )
    return r.stdout


def fixture_path(phase: str, stem: str, total_pages: int) -> Path:
    """fixture JSON 파일 경로."""
    return _GOLDEN_ROOT / phase / f"{stem}__{total_pages}p.json"


def load_golden(phase: str, stem: str, total_pages: int) -> PipelineSnapshot:
    """박제된 fixture 를 PipelineSnapshot 으로 로드 (테스트 런타임 — git 의존 없음)."""
    fp = fixture_path(phase, stem, total_pages)
    if not fp.exists():
        raise FileNotFoundError(
            f"골든 fixture 없음: {fp}\n"
            f"  → .venv/bin/python -m tests._golden_fixtures 로 재생성 필요."
        )
    data = json.loads(fp.read_text(encoding="utf-8"))
    return PipelineSnapshot.from_dict(data["snapshot"])


def _record_pre_integration(spec: dict, total_pages: int) -> PipelineSnapshot:
    """통합 직전 원본 스크립트를 녹화 (spec 별 ref/진입점/모드 사용).

    ref 는 spec["ref"](phase3) 또는 기본 PRE_INTEGRATION_REF(phase1/2).
    진입점은 spec["entry_fn"](기본 main) + spec["entry_args"](기본 ()).
    main() 진입 스크립트는 write 후처리(stat/read_text)를 호출하므로 무해 mock.
    임시파일 read-back 스크립트(gemini)는 passthrough_read=True 로 정확 재현하며,
    이 경우 read_text 를 "" 로 막지 않는다(record_script 내부에서 read-back 처리).
    CLI(argv) 스크립트는 sys.argv 를 주입한다.
    """
    ref = spec.get("ref", PRE_INTEGRATION_REF)
    src = _git_show_ref(ref, spec["script"])
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as f:
        f.write(src)
        tmp = Path(f.name)

    argv = spec.get("argv")
    entry_fn = spec.get("entry_fn", "main")
    entry_args = spec.get("entry_args", ())
    passthrough_read = spec.get("passthrough_read", False)
    output_match = spec.get("output_match")
    saved_argv = sys.argv
    try:
        cms = [
            # 원본 main() 의 write 후처리(stat)는 항상 무해 mock.
            patch.object(Path, "stat", return_value=os.stat_result(
                (0, 0, 0, 0, 0, 0, 0, 0, 0, 0))),
            patch("time.sleep", return_value=None),
        ]
        # passthrough_read 모드가 아닐 때만 read_text 를 "" 로 막는다
        # (main() 의 후처리 read_text(이미지 수 카운트 등) 무해화).
        if not passthrough_read:
            cms.append(patch.object(Path, "read_text", return_value=""))
        for cm in cms:
            cm.start()
        if argv is not None:
            sys.argv = list(argv)
        try:
            return record_script(
                tmp, entry_fn=entry_fn, entry_args=entry_args,
                total_pages=total_pages,
                passthrough_read=passthrough_read,
                output_match=output_match,
            )
        finally:
            for cm in reversed(cms):
                cm.stop()
    finally:
        sys.argv = saved_argv
        tmp.unlink(missing_ok=True)


def regenerate_all() -> list[Path]:
    """모든 fixture 를 PRE_INTEGRATION_REF 원본에서 1회 박제 (생성/갱신).

    Returns:
        생성된 fixture 파일 경로 목록.
    """
    written: list[Path] = []
    for spec in FIXTURE_SPECS:
        out_dir = _GOLDEN_ROOT / spec["phase"]
        out_dir.mkdir(parents=True, exist_ok=True)
        for total_pages in spec["pages"]:
            snap = _record_pre_integration(spec, total_pages)
            payload = {
                "_comment": (
                    "고정 골든 fixture — 통합 직전 원본에서 박제됨. 수동 편집 금지. "
                    "재생성: .venv/bin/python -m tests._golden_fixtures"
                ),
                "source_ref": spec.get("ref", PRE_INTEGRATION_REF),
                "script": spec["script"],
                "entry_fn": spec.get("entry_fn", "main"),
                "argv": spec.get("argv"),
                "total_pages": total_pages,
                "snapshot": snap.to_dict(),
            }
            fp = fixture_path(spec["phase"], spec["stem"], total_pages)
            fp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written.append(fp)
    return written


if __name__ == "__main__":
    paths = regenerate_all()
    print(f"[+] {len(paths)} 개 골든 fixture 생성 (source_ref={PRE_INTEGRATION_REF}):")
    for p in paths:
        print(f"    {p}")
