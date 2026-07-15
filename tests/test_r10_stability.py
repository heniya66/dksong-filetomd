"""R10 완결 라운드(2026-07-15) 단위테스트 — QA 잔여 7건.

M5  고아 crop PNG 정리 (fmdw/figure_extractor.extract_figures)
M6  update_sources 원본 소실 가드 (convert_project)
M7  work_dir 동시 실행 락 (convert_project)
M8  앙상블 단일 run 정족수 미달 → [unverified] 강등 (fmdw/vision_qa_ensemble)
D-2 페이지 경계 미닫힘 코드펜스 결정론 교정 (extract_all_via_pdf)
CLI extract --help / doc_audit usage 위생
(#7 stale 테스트 갱신은 test_hybrid_body_repair.py 자체가 검증)

픽스처는 전부 tmp_path — input/pdf·기존 산출물 무접촉(09_edge_blank 는 읽기 전용).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import convert_project as cp  # noqa: E402
import extract_all_via_pdf as x  # noqa: E402
from fmdw import figure_extractor as fe  # noqa: E402
from fmdw import vision_qa_ensemble as vqe  # noqa: E402


# ── M5: 고아 crop PNG 정리 ───────────────────────────────────────────────────

def _blank_pdf(path: Path, pages: int = 1) -> Path:
    import fitz
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def test_m5_orphan_crops_cleaned_same_stem_only(tmp_path):
    """재변환 시 새 사이드카 미참조 PNG 만 삭제 — 타 문서 stem 무접촉."""
    pdf = _blank_pdf(tmp_path / "docA.pdf")
    figs = tmp_path / "figures"
    figs.mkdir()
    orphan = figs / "docA__p01_fig9.png"          # 이전 실행 잔재(고아)
    other = figs / "docB__p01_fig1.png"           # 타 문서 산출물
    lookalike = figs / "docA_extra__p01_fig1.png" # stem 프리픽스 불일치(무접촉)
    # R10b(Advisor LOW): 형제 파일명 케이스 — 'docA.pdf' 와 'docA__pipeline.pdf' 가
    # 공존하면 후자의 crop 은 'docA__p' 로 시작하지만 __p 뒤가 숫자가 아님 → 보존.
    sibling = figs / "docA__pipeline__p01_fig1.png"
    for f in (orphan, other, lookalike, sibling):
        f.write_bytes(b"png")

    items = fe.extract_figures(pdf, tmp_path)     # 빈 페이지 → items=[]
    assert items == []
    assert not orphan.exists(), "같은 stem 미참조 고아는 삭제돼야 함"
    assert other.exists(), "타 문서 PNG 무접촉"
    assert lookalike.exists(), "프리픽스 불일치 stem 무접촉"
    assert sibling.exists(), "형제 stem(docA__pipeline) crop 무접촉(R10b 숫자 경계)"
    assert json.loads((tmp_path / "docA_figures.json").read_text()) == []


def test_m5_no_cleanup_when_sidecar_write_fails(tmp_path, monkeypatch):
    """사이드카 쓰기 실패 시 삭제 0(안전측) — 기존 crop 보존."""
    pdf = _blank_pdf(tmp_path / "docA.pdf")
    figs = tmp_path / "figures"
    figs.mkdir()
    orphan = figs / "docA__p01_fig9.png"
    orphan.write_bytes(b"png")

    real_write = Path.write_text

    def boom(self, *a, **k):
        if self.name.endswith("_figures.json"):
            raise OSError("injected sidecar write failure")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        fe.extract_figures(pdf, tmp_path)
    assert orphan.exists(), "사이드카 실패 시 고아 삭제 금지(안전측)"


# ── M6: 원본 소실 시 update_sources 가드 ─────────────────────────────────────

def test_m6_missing_source_marked_not_crash(tmp_path):
    base = tmp_path / "proj"
    base.mkdir()
    gone = base / "03_DOC/design/gone.pdf"        # 존재하지 않음(소실 시뮬)
    alive = base / "03_DOC/design/alive.pdf"
    alive.parent.mkdir(parents=True)
    alive.write_bytes(b"%PDF-1.4 dummy")

    data = {"version": 1, "entries": []}
    out = cp.update_sources(
        data,
        [(gone, "design_doc", "pdf"), (alive, "design_doc", "pdf")],
        [], [],
        {"03_DOC/design/gone.pdf": "04_RAG/processed_md/design_doc/gone.md",
         "03_DOC/design/alive.pdf": "04_RAG/processed_md/design_doc/alive.md"},
        base,
    )
    by_src = {e["src"]: e for e in out["entries"]}
    ge = by_src["03_DOC/design/gone.pdf"]
    ae = by_src["03_DOC/design/alive.pdf"]
    assert ge["missing_source"] is True and ge["sha256"] is None
    assert ge["status"] == "converted"            # 기록 보존(변환 자체는 성공)
    assert "missing_source" not in ae and ae["sha256"], "정상 파일 영향 0"


# ── M7: 동시 실행 락 ────────────────────────────────────────────────────────

def test_m7_second_instance_rejected_first_unaffected(tmp_path):
    import fcntl
    base = tmp_path / "proj"
    (base / "04_RAG").mkdir(parents=True)
    lock_path = base / "04_RAG" / ".convert.lock"

    # 제1 인스턴스 시뮬: 이 테스트 프로세스가 락 보유
    fh = open(lock_path, "w")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = subprocess.run(
            [sys.executable, str(REPO / "convert_project.py"),
             "--project", str(base), "--dry-run"],
            capture_output=True, text=True, timeout=120)
        assert r.returncode == 3, f"제2 인스턴스는 명확 거부(rc=3): {r.stderr}"
        assert "다른 convert_project.py 인스턴스" in r.stderr
        # 제1 인스턴스(우리)의 락은 여전히 유효 — 재획득 시도가 실패해야 정상
        fh2 = open(lock_path, "w")
        with pytest.raises(OSError):
            fcntl.flock(fh2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh2.close()
    finally:
        fh.close()  # 락 해제

    # 락 해제 후엔 정상 실행(dry-run, 빈 프로젝트 → rc 0)
    r2 = subprocess.run(
        [sys.executable, str(REPO / "convert_project.py"),
         "--project", str(base), "--dry-run"],
        capture_output=True, text=True, timeout=120)
    assert r2.returncode == 0, r2.stderr


# ── M8: 앙상블 정족수 미달 단일 값 → [unverified] ────────────────────────────

def test_m8_single_run_value_downgraded_to_unverified():
    # 환각 시나리오: run1 만 값, run2·3 은 그 라인 자체를 전사 안 함(ABSENT)
    assert vqe._majority_vote(["100nF", vqe.V_ABSENT, vqe.V_ABSENT]) \
        == vqe.V_UNVERIFIED
    # 정족수 충족(2+ 동의) → 기존 동작 유지
    assert vqe._majority_vote(["100nF", "100nF", vqe.V_ABSENT]) == "100nF"
    # 단일 플래그 판정은 기존 유지(이미 보수적 표기)
    assert vqe._majority_vote([vqe.V_CONFIRMED, vqe.V_ABSENT, vqe.V_ABSENT]) \
        == vqe.V_CONFIRMED
    assert vqe._majority_vote([vqe.V_UNREADABLE, vqe.V_ABSENT, vqe.V_ABSENT]) \
        == vqe.V_UNREADABLE


def test_m8_unverified_keeps_value_with_flag():
    """[unverified] 강등 시 값은 라인에 남고 플래그만 부착(값 소실 0)."""
    line = "| C99 | 100nF | 0402 |"
    out = vqe._apply_verdict_to_line(line, "C99", vqe.V_UNVERIFIED)
    assert "100nF" in out, "값 보존"
    assert "[unverified]" in out, "플래그 부착"


# ── D-2: 페이지 경계 코드펜스 균형 ──────────────────────────────────────────

def test_d2_dangling_empty_fence_removed():
    md = ("<!-- page 1 -->\n\nbody text\n\n---\n\n"
          "<!-- page 2 -->\n\n```markdown")
    out = x._balance_page_fences(md)
    assert "```" not in out, "빈 여는 펜스는 제거"
    assert "body text" in out and "<!-- page 2 -->" in out


def test_d2_unclosed_fence_with_content_closed():
    md = ("<!-- page 1 -->\n\n```c\nint main(void);\n\n---\n\n"
          "<!-- page 2 -->\n\nnext page\n")
    out = x._balance_page_fences(md)
    seg1 = out.split("<!-- page 2 -->")[0]
    assert seg1.count("```") == 2, f"닫는 펜스 추가돼야 함: {seg1!r}"
    assert "int main(void);" in out and "next page" in out


def test_d2_balanced_doc_untouched():
    md = ("<!-- page 1 -->\n\n```c\ncode\n```\n\ntext\n\n---\n\n"
          "<!-- page 2 -->\n\nplain\n")
    assert x._balance_page_fences(md) == md, "균형 문서는 바이트 동일"


EDGE_BLANK = REPO / "output/qa_matrix_260715/09_edge_blank/edge_blank.md"


@pytest.mark.skipif(not EDGE_BLANK.exists(), reason="09_edge_blank 픽스처 없음")
def test_d2_real_edge_blank_artifact_fixed_readonly():
    """실결함 픽스처(09_edge_blank, 읽기 전용): p2 '```markdown' 고아 펜스 교정."""
    md = EDGE_BLANK.read_text(encoding="utf-8")
    assert md.count("```") % 2 == 1, "픽스처 전제: 불균형 펜스 존재"
    out = x._balance_page_fences(md)
    assert out.count("```") % 2 == 0, "교정 후 균형"
    # 내용 보존: 펜스 외 모든 라인 유지
    kept = [l for l in md.split("\n") if not l.strip().startswith("```")]
    for l in kept:
        assert l in out


# ── CLI 위생 ────────────────────────────────────────────────────────────────

def test_cli_extract_help_exits_without_converting():
    r = subprocess.run(
        [sys.executable, str(REPO / "extract_all_via_pdf.py"), "--help"],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert "환경변수" in r.stdout and "EXTRACT_" in r.stdout
    assert "Processing" not in r.stdout, "변환 미시작"
    assert "[UNLOAD]" not in r.stdout, "모델 경로 미접촉"


def test_cli_doc_audit_usage_no_crash():
    r_help = subprocess.run(
        [sys.executable, str(REPO / "doc_audit.py"), "--help"],
        capture_output=True, text=True, timeout=120)
    assert r_help.returncode == 0
    assert "usage:" in r_help.stdout

    r_noargs = subprocess.run(
        [sys.executable, str(REPO / "doc_audit.py")],
        capture_output=True, text=True, timeout=120)
    assert r_noargs.returncode == 2, "인자 부족 = usage + exit 2(크래시 아님)"
    assert "usage:" in r_noargs.stderr
    assert "Traceback" not in r_noargs.stderr
