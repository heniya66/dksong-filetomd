"""filestomdwgem (fmdw) 공통 라이브러리 패키지 — 다포맷 → Markdown 변환.

ollama_extractor: PDF/이미지 멀티모달 추출 provider 추상화
(Ollama Cloud 로컬 게이트웨이 기본, Gemini fallback).
vision_qa: 2차 QA(Quality Assurance) — Claude vision verifier로 1차 추출 MD를
원본 이미지 대조 교정 (opt-in, VISION_QA=claude_cli; 기본 OFF).

단일 진입점 (외부 프로젝트 사용):
    from fmdw import convert_file
    convert_file("report.pdf", output_dir="proj/02_processed_md")

지원 포맷(확장자 라우팅):
    .pdf                          → pdf_pipeline.convert_pdf (멀티모달, LLM)
    .docx                         → markitdown_pipeline.convert_docx
    .pptx/.xlsx/.xls/.html/.htm/.csv → markitdown_pipeline.convert_office
    .hwp/.hwpx                    → hwp_pipeline.convert_hwp

[지연 import(lazy import) 원칙]
  markitdown / pyhwp2md 등 무거운 optional 의존성은 **함수 호출 시점에만** import 한다.
  `import fmdw` 는 이들 미설치 환경에서도 항상 성공해야 한다(top-level import 금지).
  optional extra: `pip install 'filestomdwgem[office]'` / `'filestomdwgem[hwp]'`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# 외부 프로젝트의 02_processed_md/ 등으로 변환 산출물을 보내기 위한 단일 진입점.
__all__ = [
    "convert_file",
    "convert_pdf",
    "convert_docx",
    "convert_office",
    "convert_pptx",
    "convert_xlsx",
    "convert_html",
    "convert_csv",
    "convert_hwp",
    "DEFAULT_PDF_PROMPT",
    "SUPPORTED_EXTENSIONS",
]

# ──────────────────────────────────────────────────────────────────────────────
# 확장자 → 카테고리 라우팅 테이블 (소문자 기준)
# ──────────────────────────────────────────────────────────────────────────────
_OFFICE_EXTS = {".pptx", ".xlsx", ".xls", ".html", ".htm", ".csv"}
_DOCX_EXTS = {".docx"}
_HWP_EXTS = {".hwp", ".hwpx"}
_PDF_EXTS = {".pdf"}

# 사용자 안내/검증용 — 지원하는 전체 확장자 집합.
SUPPORTED_EXTENSIONS = frozenset(_PDF_EXTS | _DOCX_EXTS | _OFFICE_EXTS | _HWP_EXTS)

# ──────────────────────────────────────────────────────────────────────────────
# PDF 기본 프롬프트 — convert_file 가 pdf 라우팅 시 사용(범용 추출).
#   pdf_pipeline.convert_pdf 는 prompt 가 필수 인자다. 여기에 일반 추출용 기본
#   프롬프트를 모듈 상수로 두고, 호출자는 convert_file(..., prompt="...") 로 override.
#   ({start}/{end} 플레이스홀더 포함 → convert_pdf 가 청크 범위로 치환.)
#   (출처: extract_pdf_image_analysis.py PROMPT_TEMPLATE 계열 — 범용 추출용.)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_PDF_PROMPT = """PDF 페이지 {start}~{end} 전체 내용을 고품질 Markdown으로 추출하라.

일반 규칙:
- 페이지 헤더: `## Page N`
- 모든 텍스트·표(GFM pipe)·번호·서식 보존
- 식별번호·도면번호([Fig. N], [도 N], [그림 N] 등) 원문 표기 유지
- 이미지·도면·다이어그램이 등장하면, 본문 흐름상 위치에 **상세하고 서술적으로** 설명하라. 단순 라벨 나열이 아니라 다음을 문단으로 풍부하게 기술: (1) 이 그림이 무엇을 나타내는지(종류·목적), (2) 주요 구성요소 각각의 이름과 역할·기능, (3) 요소 간 연결·신호 흐름·배치 관계, (4) 그림이 전달하는 기능적 의미·맥락. 캡션·라벨·구성요소·식별번호는 빠짐없이 포함하되, 위 4가지를 서술 문단으로 자세히 풀어라.
- 이미지 내부 텍스트/숫자는 OCR(Optical Character Recognition) 수준으로 전사
"""


# ──────────────────────────────────────────────────────────────────────────────
# 출력 경로 결정 헬퍼
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_output_path(
    src_path: Path,
    output_dir: Optional[Path | str],
    output_path: Optional[Path | str],
) -> Path:
    """변환 산출물 .md 경로 결정.

    우선순위:
      1) output_path 명시 → 그대로 사용.
      2) output_dir 명시  → output_dir/<stem>.md (디렉토리는 호출 측 변환 함수가 생성).
      3) 둘 다 없음       → src 와 같은 위치 <stem>.md.
    """
    if output_path is not None:
        return Path(output_path)
    if output_dir is not None:
        return Path(output_dir) / f"{src_path.stem}.md"
    return src_path.with_suffix(".md")


# ──────────────────────────────────────────────────────────────────────────────
# 공개 래퍼 — 모두 lazy import (무거운 의존성을 import fmdw 시점에 끌어오지 않음)
# ──────────────────────────────────────────────────────────────────────────────
def convert_pdf(*args, **kwargs):
    """PDF → Markdown (멀티모달, LLM). fmdw.pdf_pipeline.convert_pdf 위임.

    시그니처: convert_pdf(pdf_path, prompt_template, *, output_path, ...).
    (pdf_pipeline 은 절대 수정하지 않으며 그대로 재노출한다.)
    """
    from fmdw.pdf_pipeline import convert_pdf as _impl
    return _impl(*args, **kwargs)


def convert_docx(*args, **kwargs):
    """DOCX → Markdown. fmdw.markitdown_pipeline.convert_docx 위임(lazy)."""
    from fmdw.markitdown_pipeline import convert_docx as _impl
    return _impl(*args, **kwargs)


def convert_office(*args, **kwargs):
    """오피스/웹 포맷 → Markdown. markitdown_pipeline.convert_office 위임(lazy)."""
    from fmdw.markitdown_pipeline import convert_office as _impl
    return _impl(*args, **kwargs)


def convert_pptx(*args, **kwargs):
    """PPTX → Markdown. markitdown_pipeline.convert_pptx 위임(lazy)."""
    from fmdw.markitdown_pipeline import convert_pptx as _impl
    return _impl(*args, **kwargs)


def convert_xlsx(*args, **kwargs):
    """XLSX/XLS → Markdown. markitdown_pipeline.convert_xlsx 위임(lazy)."""
    from fmdw.markitdown_pipeline import convert_xlsx as _impl
    return _impl(*args, **kwargs)


def convert_html(*args, **kwargs):
    """HTML/HTM → Markdown. markitdown_pipeline.convert_html 위임(lazy)."""
    from fmdw.markitdown_pipeline import convert_html as _impl
    return _impl(*args, **kwargs)


def convert_csv(*args, **kwargs):
    """CSV → Markdown. markitdown_pipeline.convert_csv 위임(lazy)."""
    from fmdw.markitdown_pipeline import convert_csv as _impl
    return _impl(*args, **kwargs)


def convert_hwp(*args, **kwargs):
    """HWP/HWPX → Markdown. fmdw.hwp_pipeline.convert_hwp 위임(lazy)."""
    from fmdw.hwp_pipeline import convert_hwp as _impl
    return _impl(*args, **kwargs)


# ──────────────────────────────────────────────────────────────────────────────
# 단일 진입점 — convert_file
# ──────────────────────────────────────────────────────────────────────────────
def convert_file(
    src_path: Path | str,
    output_dir: Optional[Path | str] = None,
    output_path: Optional[Path | str] = None,
    **kwargs,
) -> Optional[Path]:
    """확장자를 보고 적절한 변환기로 라우팅하는 단일 진입점.

    외부 프로젝트에서 소스 파일을 그 프로젝트의 02_processed_md/ 등으로 변환할 때
    사용한다::

        from fmdw import convert_file
        convert_file("spec.docx", output_dir="proj/02_processed_md")

    Args:
        src_path   : 원본 파일 경로.
        output_dir : 출력 디렉토리(미존재 시 변환 함수가 자동 생성). 지정 시
                     산출물은 `output_dir/<stem>.md`.
        output_path: 출력 .md 파일 경로(지정 시 output_dir 보다 우선).
        **kwargs   : 각 convert_* 로 그대로 전달.
                     - PDF 의 경우 `prompt=` 로 추출 프롬프트 override 가능
                       (미지정 시 DEFAULT_PDF_PROMPT 사용). 그 외 chunk_size,
                       single_chunk_max, rate_limit_s, on_failure, resume 등도
                       convert_pdf 시그니처대로 전달된다.
                     - docx 의 경우 `engine="markitdown"|"vision"`.

    Returns:
        저장된 .md 경로(Path). 변환 실패/빈 결과 시 None.

    Raises:
        FileNotFoundError: src_path 미존재.
        ValueError       : 미지원 확장자.
        ImportError      : 해당 포맷의 optional 의존성 미설치(설치 안내 포함).
    """
    src_path = Path(src_path)
    if not src_path.exists():
        raise FileNotFoundError(f"입력 파일 없음: {src_path}")

    ext = src_path.suffix.lower()
    out = _resolve_output_path(src_path, output_dir, output_path)

    if ext in _PDF_EXTS:
        # PDF 는 prompt 가 필수 — 호출자 override(prompt=) 없으면 기본 프롬프트 사용.
        prompt = kwargs.pop("prompt", None)
        if prompt is None:
            prompt = DEFAULT_PDF_PROMPT
        return convert_pdf(src_path, prompt, output_path=out, **kwargs)

    if ext in _DOCX_EXTS:
        return convert_docx(src_path, out, **kwargs)

    if ext in _OFFICE_EXTS:
        return convert_office(src_path, out, **kwargs)

    if ext in _HWP_EXTS:
        return convert_hwp(src_path, out, **kwargs)

    supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
    raise ValueError(
        f"미지원 확장자: {ext!r} (파일: {src_path.name}). 지원 확장자: {supported}"
    )
