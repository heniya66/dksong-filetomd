"""R9 안정성 라운드(2026-07-15) 단위테스트 — 전수 QA 확정 결함 즉시 5건.

F1 stem 충돌 조용한 덮어쓰기 가드 (convert_project.collect_outputs)
F2 subprocess 무타임아웃 → _run_subproc 래퍼 (extract_all_via_pdf)
F3 비원자적 쓰기 → _atomic_write (extract_all_via_pdf + convert_project)
F4 hybrid 성공 경로 stale .partial.md 정리 (extract_all_via_pdf.process_file)
F5 산문 완전성 게이트 g_prose (doc_audit.py — 실산출물 대조 통합테스트)

픽스처는 전부 tmp_path — input/pdf·기존 산출물 무접촉(F5 는 읽기 전용 대조만).
"""
import json
import os
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import convert_project as cp  # noqa: E402
import extract_all_via_pdf as x  # noqa: E402


# ── F1: stem 충돌 가드 ───────────────────────────────────────────────────────

def _mk_staged(work_dir: Path, fpath: Path, base: Path, text: str) -> None:
    s_name = cp.staged_name(fpath, base)
    s_md = cp.staged_md_name(s_name)
    out = work_dir / "output/pdf_md"
    out.mkdir(parents=True, exist_ok=True)
    (out / s_md).write_text(text, encoding="utf-8")


def _staging_map(items, base):
    m = {}
    for fpath, _dom, _t in items:
        src_rel = fpath.relative_to(base).as_posix()
        s_name = cp.staged_name(fpath, base)
        m[src_rel] = ("pdf", s_name, cp.staged_md_name(s_name))
    return m


def test_f1_stem_collision_two_sources_no_loss(tmp_path):
    """동명 stem 2원본(schematic/pcb → 같은 도메인) → 유실 0, 두 MD 공존."""
    base = tmp_path / "proj"
    work = base / "04_RAG" / ".convert_work"
    f1 = base / "01_Hardware/schematic/board.pdf"
    f2 = base / "01_Hardware/pcb/board.pdf"
    to_convert = [(f1, "schematic", "pdf"), (f2, "schematic", "pdf")]
    _mk_staged(work, f1, base, "SCHEMATIC-CONTENT\n")
    _mk_staged(work, f2, base, "PCB-CONTENT\n")

    collisions = []
    results = cp.collect_outputs(to_convert, _staging_map(to_convert, base),
                                 work, base, dry_run=False,
                                 entries=[], claimed={}, collisions=collisions)
    dom = base / "04_RAG/processed_md/schematic"
    mds = sorted(p.name for p in dom.glob("*.md"))
    assert len(mds) == 2, f"두 MD 공존해야 함: {mds}"
    assert "board.md" in mds
    uniq = [n for n in mds if n != "board.md"][0]
    assert uniq.startswith("board__") and len(uniq) == len("board__.md") + 8
    # 내용 유실 0 — 각 파일이 각자의 내용 보존
    joined = "".join((dom / n).read_text() for n in mds)
    assert "SCHEMATIC-CONTENT" in joined and "PCB-CONTENT" in joined
    # results 는 서로 다른 md_rel
    vals = list(results.values())
    assert len(set(vals)) == 2 and all(v for v in vals)
    # 충돌 기록 1건
    assert len(collisions) == 1
    assert collisions[0]["collided_with"] == "01_Hardware/schematic/board.pdf"
    assert collisions[0]["renamed_to"] == uniq


def test_f1_self_update_still_single_file(tmp_path):
    """같은 원본의 재변환 갱신 = 기존 동작(단일 파일 덮어쓰기, 충돌 아님)."""
    base = tmp_path / "proj"
    work = base / "04_RAG" / ".convert_work"
    f1 = base / "01_Hardware/schematic/board.pdf"
    to_convert = [(f1, "schematic", "pdf")]
    dom = base / "04_RAG/processed_md/schematic"
    dom.mkdir(parents=True)
    (dom / "board.md").write_text("OLD\n", encoding="utf-8")
    _mk_staged(work, f1, base, "NEW-CONTENT\n")
    entries = [{"src": "01_Hardware/schematic/board.pdf",
                "md": "04_RAG/processed_md/schematic/board.md",
                "status": "converted"}]

    collisions = []
    results = cp.collect_outputs(to_convert, _staging_map(to_convert, base),
                                 work, base, dry_run=False,
                                 entries=entries, claimed={},
                                 collisions=collisions)
    mds = sorted(p.name for p in dom.glob("*.md"))
    assert mds == ["board.md"], f"자기 갱신은 단일 파일 유지: {mds}"
    assert "NEW-CONTENT" in (dom / "board.md").read_text()
    assert collisions == []
    assert results["01_Hardware/schematic/board.pdf"] \
        == "04_RAG/processed_md/schematic/board.md"


def test_f1_second_run_collided_source_stable(tmp_path):
    """충돌로 유니크화된 원본의 재변환 → 같은 유니크 이름으로 자기 갱신(증식 0)."""
    base = tmp_path / "proj"
    work = base / "04_RAG" / ".convert_work"
    f1 = base / "01_Hardware/schematic/board.pdf"
    f2 = base / "01_Hardware/pcb/board.pdf"
    uniq = cp.unique_final_md_name(f2, base)
    dom = base / "04_RAG/processed_md/schematic"
    dom.mkdir(parents=True)
    (dom / "board.md").write_text("SCH\n", encoding="utf-8")
    (dom / uniq).write_text("PCB-OLD\n", encoding="utf-8")
    entries = [
        {"src": "01_Hardware/schematic/board.pdf",
         "md": "04_RAG/processed_md/schematic/board.md"},
        {"src": "01_Hardware/pcb/board.pdf",
         "md": f"04_RAG/processed_md/schematic/{uniq}"},
    ]
    to_convert = [(f2, "schematic", "pdf")]
    _mk_staged(work, f2, base, "PCB-NEW\n")
    collisions = []
    cp.collect_outputs(to_convert, _staging_map(to_convert, base),
                       work, base, dry_run=False,
                       entries=entries, claimed={}, collisions=collisions)
    mds = sorted(p.name for p in dom.glob("*.md"))
    assert mds == sorted(["board.md", uniq]), f"파일 증식 금지: {mds}"
    assert "PCB-NEW" in (dom / uniq).read_text()
    assert "SCH" in (dom / "board.md").read_text()  # 타 문서 무손상
    assert len(collisions) == 1  # 여전히 충돌로 분류(기록 dedupe 는 main 소관)


def test_f1b_case_variant_stems_no_loss(tmp_path):
    """R9b: 대소문자만 다른 stem 2원본 — 케이스 무관 FS(APFS)에서도 유실 0·공존.

    claimed casefold 재조회가 배치 내 케이스 변형을 충돌로 검출한다(FS 무관 동작)."""
    base = tmp_path / "proj"
    work = base / "04_RAG" / ".convert_work"
    f1 = base / "01_Hardware/schematic/Board.pdf"
    f2 = base / "01_Hardware/pcb/board.pdf"
    to_convert = [(f1, "schematic", "pdf"), (f2, "schematic", "pdf")]
    _mk_staged(work, f1, base, "UPPER-CONTENT\n")
    _mk_staged(work, f2, base, "LOWER-CONTENT\n")

    collisions = []
    results = cp.collect_outputs(to_convert, _staging_map(to_convert, base),
                                 work, base, dry_run=False,
                                 entries=[], claimed={}, collisions=collisions)
    dom = base / "04_RAG/processed_md/schematic"
    mds = sorted(p.name for p in dom.glob("*.md"))
    # 케이스 무관 FS 에서 물리 파일이 하나로 접히지 않았는지(유실 0) 내용으로 검증
    joined = "".join((dom / n).read_text() for n in mds)
    assert "UPPER-CONTENT" in joined and "LOWER-CONTENT" in joined, \
        f"케이스 변형 덮어쓰기 유실: {mds}"
    assert len({n.casefold() for n in mds}) == 2, f"두 MD 공존해야 함: {mds}"
    assert len(collisions) == 1
    assert collisions[0]["renamed_to"].startswith("board__")
    vals = list(results.values())
    assert len(set(vals)) == 2 and all(vals)


def test_f1b_orphan_unknown_owner_uniquified(tmp_path):
    """R9b: sources.yaml 소실(고아 파일) + 같은 stem 타 원본 변환 → 미상 소유자
    충돌로 유니크화(고아 파일 무손상)."""
    base = tmp_path / "proj"
    work = base / "04_RAG" / ".convert_work"
    dom = base / "04_RAG/processed_md/schematic"
    dom.mkdir(parents=True)
    (dom / "board.md").write_text("ORPHAN-CONTENT\n", encoding="utf-8")  # yaml 소실 고아

    f2 = base / "01_Hardware/pcb/board.pdf"
    to_convert = [(f2, "schematic", "pdf")]
    _mk_staged(work, f2, base, "PCB-CONTENT\n")

    collisions = []
    results = cp.collect_outputs(to_convert, _staging_map(to_convert, base),
                                 work, base, dry_run=False,
                                 entries=[], claimed={}, collisions=collisions)
    assert "ORPHAN-CONTENT" in (dom / "board.md").read_text(), "고아 파일 무손상"
    uniq = cp.unique_final_md_name(f2, base)
    assert (dom / uniq).exists() and "PCB-CONTENT" in (dom / uniq).read_text()
    assert results["01_Hardware/pcb/board.pdf"] \
        == f"04_RAG/processed_md/schematic/{uniq}"
    assert len(collisions) == 1
    assert collisions[0]["collided_with"].startswith("(unknown"), collisions


# ── F2: subprocess 타임아웃 래퍼 ─────────────────────────────────────────────

def test_f2_run_subproc_timeout_kills_and_batch_continues(tmp_path, monkeypatch):
    """sleep 스텁 타임아웃 → TimeoutExpired + 프로세스 킬 + 다음 파일 진행."""
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        x._run_subproc(["sleep", "30"], timeout=1, capture_output=True)
    assert time.time() - t0 < 10, "타임아웃 후 즉시 반환해야 함(킬 확인)"

    # env 기본값 경로: FMDW_SUBPROC_TIMEOUT 로 조정 가능
    monkeypatch.setenv("FMDW_SUBPROC_TIMEOUT", "1")
    assert x._subproc_timeout() == 1
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        x._run_subproc(["sleep", "30"], capture_output=True)
    assert time.time() - t0 < 10

    # '배치는 계속' — convert_to_pdf 가 타임아웃을 파일 단위 실패(None)로 흡수
    stub = tmp_path / "bin"
    stub.mkdir()
    soffice = stub / "soffice"
    soffice.write_text("#!/bin/sh\nsleep 30\n")
    soffice.chmod(soffice.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    t0 = time.time()
    assert x.convert_to_pdf(tmp_path / "dummy.docx") is None
    assert time.time() - t0 < 10
    # 후속 호출 정상 동작(파이프라인 미정지)
    cp2 = x._run_subproc(["true"], timeout=5)
    assert cp2.returncode == 0


# ── F3: 원자적 쓰기 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("aw", [x._atomic_write, cp._atomic_write],
                         ids=["extract", "convert_project"])
def test_f3_atomic_write_preserves_original_on_failure(tmp_path, monkeypatch, aw):
    """치환 직전 예외 주입 → 기존 파일 무손상 + tmp 잔존물 정리. 정상 시 교체."""
    target = tmp_path / "final.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    def boom(src, dst):
        raise OSError("injected crash before replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        aw(target, "TRUNCATED-NEW")
    assert target.read_text(encoding="utf-8") == "ORIGINAL", "기존 파일 무손상"
    assert list(tmp_path.glob("*.tmp.*")) == [], "tmp 잔존물 정리"

    monkeypatch.undo()
    aw(target, "NEW-COMPLETE")
    assert target.read_text(encoding="utf-8") == "NEW-COMPLETE"
    assert list(tmp_path.glob("*.tmp.*")) == []


# ── F4: hybrid 성공 시 stale .partial.md 정리 ───────────────────────────────

def test_f4_hybrid_success_removes_partial_and_skips_next_run(
        tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_hybrid_convert(input_path, source_rel=""):
        calls["n"] += 1
        return {"md": "# converted\n\nbody\n", "text_len": 10,
                "n_images": 0, "n_figures": 0}

    fake_mod = types.ModuleType("fmdw.hybrid_extract")
    fake_mod.hybrid_convert = fake_hybrid_convert
    monkeypatch.setitem(sys.modules, "fmdw.hybrid_extract", fake_mod)
    monkeypatch.setenv("HYBRID_EXTRACT", "1")
    monkeypatch.chdir(tmp_path)  # output/ 이 tmp 아래 생성되도록(실산출물 무접촉)

    out_dir = tmp_path / "output" / "r9test"
    out_dir.mkdir(parents=True)
    partial = out_dir / "doc.partial.md"
    partial.write_text("stale partial", encoding="utf-8")

    x.process_file(Path("doc.docx"), "r9test")
    assert (out_dir / "doc.md").exists()
    assert not partial.exists(), "hybrid 성공 후 .partial.md 삭제돼야 함"
    assert calls["n"] == 1

    # 재실행: partial 이 없으니 스킵 가드 발동 → 재변환 미발동(호출수 불변)
    x.process_file(Path("doc.docx"), "r9test")
    assert calls["n"] == 1, "완성본 존재 시 재변환/재과금 없어야 함"


# ── F5: g_prose 산문 완전성 게이트 (실산출물 읽기 전용 통합테스트) ───────────

QA_DIR = REPO / "output/qa_matrix_260715"
PDF_0018_0024 = REPO / "input/pdf/DM_p0018-0024.pdf"


def _run_audit(md: Path, pdf: Path) -> dict:
    r = subprocess.run(
        [sys.executable, str(REPO / "doc_audit.py"), str(md), str(pdf)],
        capture_output=True, text=True, timeout=600)
    # pymupdf 가 stdout 에 안내 문구를 섞을 수 있음 → 첫 '{' 부터 파싱
    return json.loads(r.stdout[r.stdout.find("{"):])


@pytest.mark.skipif(not (QA_DIR.exists() and PDF_0018_0024.exists()),
                    reason="qa_matrix_260715 픽스처 없음")
def test_f5_g_prose_detects_hybrid_off_loss_and_baseline_clean():
    """06_hybrid_off(Note 소실본) → g_prose FAIL 검출 / 01_baseline → CLEAN."""
    bad = _run_audit(QA_DIR / "06_hybrid_off/DM_p0018-0024.md", PDF_0018_0024)
    assert bad["status"] == "FAIL"
    assert bad["summary"].get("g_prose", 0) >= 1, \
        f"산문 소실을 g_prose 가 검출해야 함: {bad['summary']}"

    good = _run_audit(QA_DIR / "01_baseline/DM_p0018-0024.md", PDF_0018_0024)
    assert good["summary"].get("g_prose", 0) == 0, \
        f"baseline 오탐 0 이어야 함: {good['failures']}"
    assert good["status"] == "CLEAN", f"baseline CLEAN 유지: {good['summary']}"
