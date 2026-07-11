"""filestomdwgem 공통 PDF 추출 파이프라인 — H-1 스크립트 통합용.

8개 per-document extract_*.py 스크립트의 청크→추출→병합→저장 로직을 단일 함수로 제공.
각 스크립트는 PROMPT_TEMPLATE 상수 + convert_pdf() 호출 한 줄로 축소된다(Phase 1~3).

설계 원칙:
  - **동작 보존 최우선**: 프롬프트는 byte-동일하게 전달(임의 preamble/suffix 주입 금지).
  - 청크 윈도우·sleep·실패 정책은 호출자가 명시 — 기본값은 기존 스크립트 관행.
  - H-3(재시도)/H-4(truncation) 는 ox.extract_pdf_pages 호출로 자동 상속.
  - extract_all_via_pdf.py 를 import하지 않음(AUTO/ensemble 딸려옴 방지).
  - 시크릿(OLLAMA_API_KEY/GEMINI_API_KEY)은 ox 내부가 직접 처리 — 이 모듈 경유 금지.

on_failure 정책:
  'abort'  : 한 청크라도 실패 → 즉시 None 반환, 파일 미생성 (기존 스크립트 기본 동작).
  'partial': 실패 청크 위치에 MISSING 마커 삽입 + continue → {stem}.partial.md 저장
             (H-5 개선 레버 — 명시 opt-in 시만 사용).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Literal, Optional

# fmdw.ollama_extractor 와 fmdw.config 만 의존 (extract_all_via_pdf 미사용).
try:
    from fmdw import ollama_extractor as ox
    from fmdw import config as _cfg
    from fmdw import resume_cache as _rc
except ImportError:
    import ollama_extractor as ox  # type: ignore
    import config as _cfg  # type: ignore
    import resume_cache as _rc  # type: ignore

# 병합 구분자 — 8개 스크립트 및 extract_all_via_pdf 공통 계약.
_CHUNK_SEP = "\n\n---\n\n"

# MISSING 마커 형식 — H-5 와 동일.
def _missing_marker(start: int, end: int) -> str:
    return f"<!-- MISSING pages {start}-{end}: extraction failed -->"


def _build_chunks(
    total_pages: int,
    chunk_size: int,
    single_chunk_max: Optional[int],
) -> list[tuple[int, int]]:
    """청크 윈도우 리스트 생성.

    Args:
        total_pages    : PDF 전체 페이지 수.
        chunk_size     : 청크 크기(페이지 수). single_chunk_max 가 없으면 항상 적용.
        single_chunk_max: total_pages <= 이 값이면 단일 청크 (1, total_pages).
                          None → 항상 chunk_size 분할.
                          10**9 같은 큰 값 → 사실상 항상 단일 청크(ava1 식).

    Returns:
        [(start, end), ...] 1-based, inclusive.
    """
    if single_chunk_max is not None and total_pages <= single_chunk_max:
        return [(1, total_pages)]
    chunks = []
    for start in range(1, total_pages + 1, chunk_size):
        end = min(start + chunk_size - 1, total_pages)
        chunks.append((start, end))
    return chunks


def convert_pdf(
    pdf_path: Path | str,
    prompt_template: str,
    *,
    output_path: Path | str,
    chunk_size: Optional[int] = None,
    single_chunk_max: Optional[int] = None,
    rate_limit_s: Optional[float] = None,
    post_process: Optional[Callable[[str], str]] = None,
    on_failure: Literal["abort", "partial"] = "abort",
    resume: Optional[bool] = None,
) -> Optional[Path]:
    """PDF 를 청크 단위로 추출해 Markdown 으로 저장하는 공통 파이프라인.

    Args:
        pdf_path        : 원본 PDF 파일 경로.
        prompt_template : 추출 프롬프트. {start}/{end} 플레이스홀더가 있으면
                          각 청크 범위로 .format(start=, end=) 치환.
                          없으면 verbatim 그대로 전달(byte-동일 보장).
        output_path     : 최종 출력 .md 파일 경로.
        chunk_size      : 청크 크기(페이지 수). None 이면 lib.config.knob_chunk_size() 사용.
        single_chunk_max: total_pages <= N 이면 단일 청크 전송.
                          None → 항상 chunk_size 분할.
                          10**9 → 사실상 항상 단일 청크(ava1 식).
        rate_limit_s    : 청크 간 sleep 초수(마지막 청크 뒤는 생략).
                          None 이면 lib.config.knob_rate_limit_s() 사용(기본 10.0초).
        post_process    : 병합 결과 문자열에 적용할 1-arg callable
                          (예: sim_platform renumber_images). None → no-op.
        on_failure      : 청크 실패 시 정책.
                          'abort'  → 즉시 None 반환, 파일 미생성 (기본).
                          'partial'→ MISSING 마커 삽입 + {stem}.partial.md 저장.
        resume          : 대용량 PDF resume(중단 후 이어받기) 캐시 사용 여부.
                          None → lib.config.knob_resume_enabled() 사용(기본 ON).
                          True → 강제 활성, False → 강제 비활성(기존 동작 100% 동일).
                          활성 시 각 청크 결과를 <output 부모>/.resume_cache/ 에
                          저장하고, 재실행 시 이미 완료된 청크는 추출을 건너뛰고
                          캐시에서 로드한다. 캐시 키에 PDF 지문(size+mtime)·청크정책·
                          프롬프트 해시·provider 를 포함하여 변경 시 자동 무효화한다.
                          전체 성공 시 캐시는 정리(EXTRACT_RESUME_KEEP 으로 보존 가능).

    Returns:
        저장된 파일 경로(Path). 실패 또는 abort 시 None.
    """
    pdf_path = Path(pdf_path)
    output_path = Path(output_path)

    # 청크 크기 결정 — 호출자 명시 > config SSoT > 코드 기본값 20
    cs = chunk_size if chunk_size is not None else _cfg.knob_chunk_size()

    # rate-limit 결정 — 호출자 명시 > config SSoT(EXTRACT_RATE_LIMIT_S) > 코드 기본값 10.0
    rl = rate_limit_s if rate_limit_s is not None else _cfg.knob_rate_limit_s()

    # 페이지 수 조회
    try:
        total_pages = ox.count_pdf_pages(pdf_path)
    except Exception as e:
        print(f"[!] PDF 열기 실패: {pdf_path.name} ({e})", flush=True)
        return None

    print(
        f"[*] {pdf_path.name}: {total_pages}p, provider={ox.provider_label()}",
        flush=True,
    )

    chunks = _build_chunks(total_pages, cs, single_chunk_max)
    print(f"[*] 청크 수: {len(chunks)}", flush=True)

    # resume(중단 후 이어받기) 캐시 — knob > 호출자 override.
    resume_on = _cfg.knob_resume_enabled() if resume is None else resume
    cache = None
    if resume_on:
        try:
            key = _rc.compute_cache_key(
                pdf_path,
                chunk_size=cs,
                single_chunk_max=single_chunk_max,
                prompt_template=prompt_template,
                provider_label=ox.provider_label(),
            )
            cache = _rc.ResumeCache.open(
                pdf_path,
                output_path,
                key=key,
                chunk_size=cs,
                single_chunk_max=single_chunk_max,
                prompt_template=prompt_template,
                provider_label=ox.provider_label(),
            )
            n_cached = len(cache.cached_indices() & set(range(len(chunks))))
            if n_cached:
                print(
                    f"[*] resume: 캐시에서 {n_cached}/{len(chunks)} 청크 재사용 "
                    f"({cache.cache_dir})",
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001 — 캐시 이상은 graceful 무시(처음부터).
            print(f"[!] resume 캐시 초기화 실패 — 캐시 없이 진행 ({e})", flush=True)
            cache = None

    # 프롬프트에 {start}/{end} 플레이스홀더가 있는지 1회만 판별
    _has_range_fields = "{start}" in prompt_template and "{end}" in prompt_template

    chunk_texts: list[str] = []
    has_failures = False

    for i, (start, end) in enumerate(chunks):
        chunk_num = i + 1

        # resume: 이미 완료된 청크는 캐시에서 로드, 추출 skip (rate-limit sleep 도 skip).
        if cache is not None:
            cached_text = cache.load(i, start, end)
            if cached_text is not None:
                chunk_texts.append(cached_text)
                print(
                    f"[=] Chunk {chunk_num}: pages {start}~{end} 캐시 재사용 "
                    f"({len(cached_text)} chars) — 추출 skip",
                    flush=True,
                )
                continue

        print(f"[*] Chunk {chunk_num}: pages {start}~{end} ...", flush=True)

        # 프롬프트 구성 — verbatim 또는 range 치환
        if _has_range_fields:
            prompt = prompt_template.format(start=start, end=end)
        else:
            prompt = prompt_template

        try:
            text = ox.extract_pdf_pages(prompt, pdf_path, start, end)
            chunk_texts.append(text)
            if cache is not None:
                cache.store(i, start, end, text)
            print(f"[+] Chunk {chunk_num} 완료 ({len(text)} chars)", flush=True)
        except Exception as e:
            print(f"[!] Chunk {chunk_num} (pages {start}-{end}) 오류: {e}", flush=True)
            if on_failure == "abort":
                print(f"[!] on_failure=abort — 중단, 파일 미생성.", flush=True)
                return None
            # on_failure == "partial"
            has_failures = True
            chunk_texts.append(_missing_marker(start, end))
            print(f"[!] on_failure=partial — MISSING 마커 삽입, 계속.", flush=True)
            # 실패 청크 뒤에도 sleep 생략(다음 청크 없을 수 있음) — 아래 sleep 로직에서 처리
            if i < len(chunks) - 1:
                time.sleep(rl)
            continue

        # 마지막 청크가 아니면 rate-limit sleep
        if i < len(chunks) - 1:
            time.sleep(rl)

    if not chunk_texts or all(t.startswith("<!-- MISSING") for t in chunk_texts):
        print("[!] 추출 결과 없음 (모든 청크 실패)", flush=True)
        return None

    combined = _CHUNK_SEP.join(chunk_texts)

    # post_process 적용 (sim_platform renumber_images 등)
    if post_process is not None:
        combined = post_process(combined)

    # 저장 — 실패 있으면 .partial.md
    if has_failures:
        save_path = output_path.with_suffix("").with_name(
            output_path.stem + ".partial.md"
        )
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(combined, encoding="utf-8")
        print(f"[!] 부분 결과 저장 (일부 청크 실패): {save_path}", flush=True)
        # 부분 실패 시 캐시 보존 — 다음 실행이 실패 청크만 재시도하도록(resume 핵심).
        return save_path
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(combined, encoding="utf-8")
        print(f"[+] 저장 완료: {output_path}", flush=True)
        # 전체 성공 → resume 캐시 정리(보존 옵션 시 유지).
        if cache is not None and not _cfg.knob_resume_keep_cache():
            cache.cleanup()
        return output_path
