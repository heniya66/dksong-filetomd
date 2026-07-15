"""_pipeline_recorder.py — Phase 1~3 동작 보존 안전망용 녹화기 헬퍼.

목적:
    원본 per-doc 스크립트(extract_pdf_blockdiagram 등)를 ox.extract_pdf_pages 를
    mock 한 상태로 실행하여, 실제 클라우드 호출 없이 다음을 기록한다:
      - (prompt, start, end) 호출 시퀀스
      - 출력 파일 경로
      - 병합 구분자(기본 \\n\\n---\\n\\n 계약 확인용)

Phase 1~3 사용 방법:
    1. 원본 스크립트로 골든 스냅샷 녹화 (record_script).
    2. 통합본(convert_pdf 호출 래퍼)으로 동일하게 녹화.
    3. 두 스냅샷이 동일함을 assert — 프롬프트 드리프트·청크 경계 off-by-one 자동 검출.

애로사항 (문서화):
    원본 스크립트 대부분이 모듈-레벨에서 OUTPUT_DIR.mkdir() 을 실행하므로,
    importlib.import_module() 시 실제 출력 디렉토리를 생성하려 한다.
    이를 막기 위해 record_script 는 Path.mkdir 을 추가 patch 한다.
    또한 일부 스크립트(eda_pyojun, ava1, sim_platform 등)는 main() 안에
    하드코딩된 PDF 경로를 직접 열므로, pdf_path 인자를 주입하기 위해
    각 스크립트의 process_pdf / main 함수를 직접 호출하는 방식보다
    ox.extract_pdf_pages 를 모듈 진입 전에 patch 하는 방식이 더 안전하다.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@dataclass
class PipelineSnapshot:
    """녹화 결과.

    Attributes:
        calls       : [(prompt, start, end), ...] — extract_pdf_pages 호출 시퀀스.
        output_path : 스크립트가 write_text 를 호출한 경로 (None 이면 저장 안 함).
        separator   : 병합에 사용된 구분자 (None 이면 단일 청크).
        chunk_count : 청크 수.
    """
    calls: list[tuple[str, int, int]] = field(default_factory=list)
    output_path: Optional[Path] = None
    separator: Optional[str] = None
    chunk_count: int = 0

    def assert_equals(self, other: "PipelineSnapshot", label: str = "") -> None:
        """두 스냅샷이 동일함을 assert (Phase 1~3 골든 비교용).

        대조 항목:
          - chunk_count / 호출 횟수
          - 각 청크의 (prompt, start, end)
          - separator (병합 구분자 — 단일/다중 청크 모두 결정적이므로 항상 대조)
          - output_path (양쪽 모두 캡처된 경우에만 대조 — 출력 경로 드리프트 검출)

        output_path 를 "둘 다 있을 때만" 비교하는 이유: abort 등으로 한쪽이
        파일을 쓰지 않는 시나리오를 위해서다. 정상 골든 비교(원본·통합본 모두
        저장)에서는 항상 대조되어 Phase 2 teeth 가 유지된다.
        """
        prefix = f"[{label}] " if label else ""
        assert self.chunk_count == other.chunk_count, (
            f"{prefix}chunk_count 불일치: {self.chunk_count} != {other.chunk_count}"
        )
        assert len(self.calls) == len(other.calls), (
            f"{prefix}호출 횟수 불일치: {len(self.calls)} != {len(other.calls)}"
        )
        for i, (a, b) in enumerate(zip(self.calls, other.calls)):
            assert a[0] == b[0], (
                f"{prefix}청크 {i+1} 프롬프트 드리프트:\n"
                f"  원본: {a[0][:120]!r}\n"
                f"  통합: {b[0][:120]!r}"
            )
            assert a[1] == b[1], f"{prefix}청크 {i+1} start 불일치: {a[1]} != {b[1]}"
            assert a[2] == b[2], f"{prefix}청크 {i+1} end 불일치: {a[2]} != {b[2]}"

        # separator 대조 — 병합 구분자 드리프트 검출 (항상 결정적).
        assert self.separator == other.separator, (
            f"{prefix}separator 불일치: {self.separator!r} != {other.separator!r}"
        )

        # output_path 대조 — 양쪽 모두 캡처된 경우에만 (출력 경로 드리프트 검출).
        # R11(2026-07-15): 머신 무관 정규화 비교로 전환. 골든 fixture 는 캡처 당시
        # 머신의 '절대' 경로(/Users/heni/workspace/filestomdwgem/...)를 박제하고
        # 있어, 다른 머신/레포명(filetomd)에서는 워크스페이스 접두가 달라 순수
        # 환경 차이로 전멸했다(이 레포에서 한 번도 그린인 적 없던 이식 부채).
        # 드리프트 teeth 의 실체는 'output/ 이하 구조 + 파일명'이므로 'output'
        # 컴포넌트부터의 상대 경로로 대조한다(양쪽에 output 이 없으면 종전대로
        # 절대 경로 대조 — 완화 아님).
        if self.output_path is not None and other.output_path is not None:
            def _norm(p):
                parts = Path(p).parts
                if "output" in parts:
                    return str(Path(*parts[parts.index("output"):]))
                return str(p)
            assert _norm(self.output_path) == _norm(other.output_path), (
                f"{prefix}output_path 불일치(output/ 이하 정규화 비교):\n"
                f"  원본: {self.output_path}\n"
                f"  통합: {other.output_path}"
            )

    # ── JSON 직렬화 (고정 골든 fixture 박제용) ──────────────────────────────────
    def to_dict(self) -> dict:
        """JSON 직렬화 가능한 dict 로 변환 (fixture 저장용).

        calls 의 prompt 는 그대로(verbatim) 보존하여 byte-동일 골든을 유지한다.
        output_path 는 문자열로 직렬화.
        """
        return {
            "calls": [[p, s, e] for (p, s, e) in self.calls],
            "output_path": (str(self.output_path)
                            if self.output_path is not None else None),
            "separator": self.separator,
            "chunk_count": self.chunk_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineSnapshot":
        """to_dict 로 저장된 dict 에서 PipelineSnapshot 복원 (fixture 로드용)."""
        snap = cls()
        snap.calls = [(c[0], c[1], c[2]) for c in data["calls"]]
        op = data.get("output_path")
        snap.output_path = Path(op) if op is not None else None
        snap.separator = data.get("separator")
        snap.chunk_count = data.get("chunk_count", len(snap.calls))
        return snap


def record_convert_pdf(
    pdf_path: Path,
    prompt_template: str,
    *,
    output_path: Path,
    total_pages: int,
    chunk_size: Optional[int] = None,
    single_chunk_max: Optional[int] = None,
    rate_limit_s: float = 0.0,
    post_process=None,
    on_failure: str = "abort",
) -> PipelineSnapshot:
    """lib.pdf_pipeline.convert_pdf 호출을 녹화.

    실제 PDF / 클라우드 호출 없이 (prompt, start, end) 시퀀스를 캡처.
    """
    from fmdw import pdf_pipeline as pp

    snap = PipelineSnapshot()
    captured_write: list[tuple[Path, str]] = []

    fake_extract_calls: list[tuple[str, int, int]] = []

    def fake_extract(prompt, pdf_p, start, end):
        fake_extract_calls.append((prompt, start, end))
        return f"chunk_{start}_{end}"

    orig_write = Path.write_text

    def capturing_write(self_path, data, **kwargs):
        captured_write.append((Path(self_path), data))

    with patch.object(pp.ox, "count_pdf_pages", return_value=total_pages), \
         patch.object(pp.ox, "extract_pdf_pages", side_effect=fake_extract), \
         patch.object(pp, "time"), \
         patch.object(Path, "write_text", capturing_write), \
         patch.object(Path, "mkdir", return_value=None):
        pp.convert_pdf(
            pdf_path, prompt_template,
            output_path=output_path,
            chunk_size=chunk_size,
            single_chunk_max=single_chunk_max,
            rate_limit_s=rate_limit_s,
            post_process=post_process,
            on_failure=on_failure,
            # 골든 비교는 *추출/병합/저장 계약*(=resume off 기준선)을 검증한다.
            # resume(중단 후 이어받기) 캐시는 별도 단위 테스트(test_resume_cache.py)에서
            # 다루며, 여기서는 캐시의 중간 write_text 가 output_path 캡처를 오염시키지
            # 않도록 명시적으로 비활성한다(프로덕션 기본값 ON 은 영향 없음).
            resume=False,
        )

    snap.calls = fake_extract_calls
    snap.chunk_count = len(fake_extract_calls)
    if captured_write:
        snap.output_path = captured_write[0][0]
        combined = captured_write[0][1]
        sep = "\n\n---\n\n"
        snap.separator = sep if sep in combined else None
    return snap


def record_script(
    script_path: Path | str,
    entry_fn: str,
    entry_args: tuple,
    total_pages: int,
    *,
    mock_pdf_exists: bool = True,
    passthrough_read: bool = False,
    output_match: Optional[str] = None,
) -> PipelineSnapshot:
    """원본 스크립트의 추출 호출 시퀀스를 녹화.

    Args:
        script_path  : 스크립트 파일 경로 (예: Path("extract_pdf_blockdiagram.py")).
        entry_fn     : 호출할 함수 이름 (예: "process_pdf" 또는 "main").
        entry_args   : entry_fn 에 전달할 positional 인자 튜플.
        total_pages  : mock count_pdf_pages 반환값.
        mock_pdf_exists: Path.exists() 를 True 로 patch 할지 여부.
        passthrough_read: True 면 write_text 로 쓴 내용을 read_text 가 되돌려준다.
                          임시 청크파일을 써뒀다 다시 읽어 병합하는 스크립트
                          (gemini_multimodal) 의 병합 결과를 정확히 재현하기 위함.
                          기존 스크립트(phase1/2)는 read-back 이 없으므로 영향 없음.
        output_match : None 이면 첫 write 를 출력으로 본다(기존 동작).
                       문자열이면, 그 suffix 로 끝나는 *마지막* write 를 최종 출력으로
                       선택한다(임시 _chunk_ 파일을 건너뛰고 최종 {stem}.md 선택용).

    Returns:
        PipelineSnapshot — 호출 시퀀스 + 출력 경로.

    애로사항:
        - 모듈-레벨 OUTPUT_DIR.mkdir() 를 막기 위해 Path.mkdir 을 patch.
        - 하드코딩 PDF 경로 존재 확인(Path.exists)도 patch.
        - 스크립트가 sys.exit() 을 호출하면 SystemExit 을 잡아 스냅샷 반환.
    """
    script_path = Path(script_path)
    snap = PipelineSnapshot()
    fake_extract_calls: list[tuple[str, int, int]] = []
    captured_write: list[tuple[Path, str]] = []
    write_store: dict[str, str] = {}

    def fake_extract(prompt, pdf_p, start, end):
        fake_extract_calls.append((prompt, start, end))
        return f"chunk_{start}_{end}"

    def fake_count(pdf_p):
        return total_pages

    def capturing_write(self_path, data, **kwargs):
        captured_write.append((Path(self_path), data))
        write_store[str(Path(self_path))] = data

    def passthrough_read_text(self_path, **kwargs):
        # 임시파일 read-back: 직전에 쓴 내용을 그대로 반환(병합 정확 재현).
        return write_store.get(str(Path(self_path)), "")

    # 스크립트를 격리 모듈로 로드 (sys.modules 오염 방지)
    module_name = f"_rec_{script_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    mod = importlib.util.module_from_spec(spec)

    patches = [
        patch("fmdw.ollama_extractor.extract_pdf_pages", side_effect=fake_extract),
        patch("fmdw.ollama_extractor.count_pdf_pages", side_effect=fake_count),
        patch.object(Path, "mkdir", return_value=None),
        patch.object(Path, "write_text", capturing_write),
    ]
    if passthrough_read:
        # 임시파일 삭제(unlink)도 무해화 — 실제 파일이 없으므로.
        patches.append(patch.object(Path, "read_text", passthrough_read_text))
        patches.append(patch.object(Path, "unlink", lambda self_path, **k: None))
    if mock_pdf_exists:
        patches.append(patch.object(Path, "exists", return_value=True))

    try:
        for p in patches:
            p.start()
        spec.loader.exec_module(mod)
        fn = getattr(mod, entry_fn)
        try:
            fn(*entry_args)
        except SystemExit:
            pass
    finally:
        for p in reversed(patches):
            p.stop()

    snap.calls = fake_extract_calls
    snap.chunk_count = len(fake_extract_calls)
    if captured_write:
        if output_match is not None:
            # suffix 로 끝나는 마지막 write 를 최종 출력으로 선택(임시파일 건너뜀).
            matches = [w for w in captured_write
                       if str(w[0]).endswith(output_match)]
            chosen = matches[-1] if matches else captured_write[-1]
        else:
            chosen = captured_write[0]
        snap.output_path = chosen[0]
        combined = chosen[1]
        sep = "\n\n---\n\n"
        snap.separator = sep if sep in combined else None
    return snap


def record_migrated_script(
    script_path: Path | str,
    entry_fn: str,
    entry_args: tuple,
    total_pages: int,
    *,
    mock_pdf_exists: bool = True,
) -> PipelineSnapshot:
    """*마이그레이션된* 통합본 스크립트가 convert_pdf 에 넘기는 실제 인자를 녹화.

    record_script 와 달리, 통합본은 process_pdf/extract_chunk 가 없고 main() 안에서
    lib.pdf_pipeline.convert_pdf(...) 를 호출한다. 이 헬퍼는:
      1. 통합본 스크립트의 entry_fn(보통 main) 을 실행하되 convert_pdf 를 patch 해
         실제 호출 인자(prompt_template, chunk_size, single_chunk_max, output_path,
         rate_limit_s, on_failure ...)를 *그대로* 캡처한다.
      2. 캡처된 인자로 진짜 convert_pdf 를 (ox mock 하에) 다시 구동해
         (prompt,start,end) 시퀀스·separator·output_path 스냅샷을 만든다.

    이로써 통합본의 **프롬프트/청크정책(single_chunk_max·chunk_size)/출력경로**가
    스크립트 소스에서 직접 읽혀 골든과 대조된다 — 테스트가 파라미터를 하드코딩하지
    않으므로, 스크립트의 청크정책을 변형하면 골든 비교가 실패한다(teeth 강화).

    Args:
        script_path  : 통합본 스크립트 경로 (예: extract_ava2.py).
        entry_fn     : 실행할 함수(보통 "main").
        entry_args   : entry_fn 인자(보통 () — main 은 인자 없음).
        total_pages  : mock count_pdf_pages 반환값.
        mock_pdf_exists: Path.exists() 를 True 로 patch 할지.

    Returns:
        PipelineSnapshot — 통합본 실제 convert_pdf 호출 기준 시퀀스.
    """
    from fmdw import pdf_pipeline as pp

    script_path = Path(script_path)
    captured: dict = {}

    def capturing_convert_pdf(pdf_path, prompt_template, **kwargs):
        # 통합본이 넘긴 실제 인자를 캡처 (스크립트는 1회 호출).
        captured["pdf_path"] = pdf_path
        captured["prompt_template"] = prompt_template
        captured["kwargs"] = kwargs
        # 통합본 main() 의 후속 코드(result.stat() 등)가 죽지 않도록 가짜 Path 반환.
        return Path(kwargs.get("output_path", "/tmp/_unused.md"))

    # 통합본 스크립트를 격리 모듈로 로드.
    module_name = f"_recmig_{script_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    mod = importlib.util.module_from_spec(spec)

    patches = [
        patch.object(pp, "convert_pdf", side_effect=capturing_convert_pdf),
        patch.object(Path, "mkdir", return_value=None),
        # 통합본 main() 이 result.stat()/read_text() 를 호출할 수 있으므로 무해 mock.
        patch.object(Path, "stat", return_value=os.stat_result(
            (0, 0, 0, 0, 0, 0, 0, 0, 0, 0))),
        patch.object(Path, "read_text", return_value=""),
        patch("time.sleep", return_value=None),
    ]
    if mock_pdf_exists:
        patches.append(patch.object(Path, "exists", return_value=True))

    try:
        for p in patches:
            p.start()
        spec.loader.exec_module(mod)
        fn = getattr(mod, entry_fn)
        try:
            fn(*entry_args)
        except SystemExit:
            pass
    finally:
        for p in reversed(patches):
            p.stop()

    if "prompt_template" not in captured:
        raise AssertionError(
            f"{script_path.name} 의 {entry_fn}() 가 convert_pdf 를 호출하지 않았다 "
            f"(통합 누락 또는 진입점 변경?)"
        )

    # 캡처된 실제 인자로 record_convert_pdf 를 구동 → 시퀀스/separator/output_path.
    kw = captured["kwargs"]
    return record_convert_pdf(
        captured["pdf_path"], captured["prompt_template"],
        output_path=kw["output_path"],
        total_pages=total_pages,
        chunk_size=kw.get("chunk_size"),
        single_chunk_max=kw.get("single_chunk_max"),
        rate_limit_s=kw.get("rate_limit_s", 0.0),
        post_process=kw.get("post_process"),
        on_failure=kw.get("on_failure", "abort"),
    )
