"""filestomdwgem 오피스/웹 포맷 → Markdown 변환 파이프라인 (markitdown 경유).

대상 포맷: docx / pptx / xlsx / xls / html / htm / csv.

설계 원칙:
  - **lazy import**: `markitdown` 은 함수 호출 시점에만 import 한다. 미설치여도
    `import fmdw` / `import fmdw.markitdown_pipeline` 자체는 성공해야 한다
    (무거운 의존성을 optional extra `filestomdwgem[office]` 로 분리).
  - 미설치 시 사용 시점에 친절한 ImportError 로 설치 안내.
  - 변환 결과는 호출자가 지정한 output_path(.md)에 UTF-8 로 저장하고 Path 를 반환.
    빈 결과(공백만)면 파일을 만들지 않고 None 을 반환한다.
  - docx 는 두 가지 engine 지원:
      engine="markitdown"(기본): markitdown(mammoth 기반, 구조 보존) 경유.
      engine="vision"          : 기존 멀티모달 경로(이미지 핵심 docx) 재사용.
        (extract_docx_multimodal.py 의 공개 함수 재활용 — 그 모듈은 수정/삭제 금지.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# office extra 미설치 시 사용자에게 보여줄 친절한 설치 안내.
_OFFICE_INSTALL_HINT = (
    "markitdown 이 설치되어 있지 않습니다. office 포맷(docx/pptx/xlsx/xls/html/csv) "
    "변환에는 추가 설치가 필요합니다:\n"
    "    pip install 'filestomdwgem[office]'"
)


def _require_markitdown():
    """markitdown.MarkItDown 클래스를 lazy import. 미설치 시 친절한 ImportError.

    무거운 의존성이라 모듈 top-level 이 아닌 호출 시점에만 import 한다.
    """
    try:
        from markitdown import MarkItDown  # type: ignore
    except ImportError as e:  # noqa: BLE001 — 설치 안내로 재포장.
        raise ImportError(_OFFICE_INSTALL_HINT) from e
    return MarkItDown


def _resolve_markdown(result) -> str:
    """MarkItDown().convert(...) 반환 객체에서 markdown 문자열을 안전 추출.

    markitdown 버전에 따라 `.markdown` 또는 `.text_content` 속성을 제공한다.
    `.markdown` 우선, 없으면 `.text_content` fallback, 둘 다 없으면 str() 캐스팅.
    """
    md = getattr(result, "markdown", None)
    if md is None:
        md = getattr(result, "text_content", None)
    if md is None:
        md = str(result)
    return md or ""


def _write_md(markdown: str, output_path: Path) -> Optional[Path]:
    """markdown 문자열을 output_path(.md)에 저장. 빈 결과면 미생성 + None.

    빈 결과(공백/개행만) 판정은 strip() 으로 한다 — 실제 콘텐츠 없는 변환을
    완성본으로 위장하지 않는다.
    """
    if not markdown or not markdown.strip():
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


# vision 하이브리드(이미지 해설 삽입)를 지원하는 포맷(확장자 → office_vision fmt).
#   docx/pptx/xlsx 만 이미지 해설 매핑 대상. xls(구형 바이너리)/html/htm/csv 는
#   문서 내 임베드 이미지 개념이 없거나 ZIP 구조가 아니라 vision 대상에서 제외.
_VISION_FMT_BY_EXT = {".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx"}


def _apply_office_vision(
    markdown: str,
    input_path: Path,
    *,
    provider: Optional[str] = None,
    **_ignored,
) -> str:
    """vision=True 경로: markdown 에 문서 내 이미지 vision 해설을 삽입(safe degrade).

    확장자가 vision 지원 포맷(docx/pptx/xlsx)일 때만 office_vision 으로 위임한다.
    그 외 확장자(xls/html/csv 등)는 임베드 이미지 매핑 대상이 아니라 그대로 반환한다.
    office_vision 자체가 모든 실패를 흡수(입력 markdown 반환)하므로 이 경로는 기존
    텍스트 결과를 절대 깨지 않는다.
    """
    fmt = _VISION_FMT_BY_EXT.get(input_path.suffix.lower())
    if fmt is None:
        return markdown
    from fmdw import office_vision  # lazy — vision=True 일 때만 로드.

    return office_vision.augment_with_vision(
        markdown, input_path, fmt, provider=provider
    )


def convert_office(
    input_path: Path | str,
    output_path: Path | str,
    *,
    vision: bool = False,
    **kwargs,
) -> Optional[Path]:
    """오피스/웹 포맷(docx/pptx/xlsx/xls/html/htm/csv) → Markdown 범용 변환.

    markitdown 으로 변환 후 output_path 에 .md 로 저장하고 Path 를 반환한다.

    Args:
        input_path : 원본 파일 경로.
        output_path: 출력 .md 파일 경로.
        vision     : True 면 문서 내 이미지(그림/사진/도면)를 fmdw vision 엔진으로
                     해설(Markdown)해 본문 해당 위치에 삽입한다(opt-in, 기본 False).
                     False(기본)면 기존 markitdown 텍스트 변환과 100% 동일. vision 대상은
                     docx/pptx/xlsx 만(그 외 확장자는 vision=True 여도 텍스트 그대로).
        **kwargs   : 향후 확장용 + vision provider override(provider=) 등 수용.

    Returns:
        저장된 파일 경로(Path). 변환 결과가 비어 있으면 None.

    Raises:
        ImportError      : markitdown 미설치 시(설치 안내 포함).
        FileNotFoundError: input_path 가 존재하지 않을 때.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"입력 파일 없음: {input_path}")

    MarkItDown = _require_markitdown()
    result = MarkItDown().convert(str(input_path))
    markdown = _resolve_markdown(result)
    if vision and markdown and markdown.strip():
        markdown = _apply_office_vision(markdown, input_path, **kwargs)
    return _write_md(markdown, output_path)


# ──────────────────────────────────────────────────────────────────────────────
# 포맷별 편의 래퍼 — 모두 convert_office 위임 (호출 측 가독성/명시성).
# ──────────────────────────────────────────────────────────────────────────────

def convert_pptx(
    input_path: Path | str, output_path: Path | str, *, vision: bool = False, **kwargs
) -> Optional[Path]:
    """PPTX(PowerPoint) → Markdown. convert_office 위임.

    vision=True 면 슬라이드별 그림을 vision 해설해 해당 `<!-- Slide number: N -->`
    섹션 뒤에 삽입(정밀 매핑). 기본 False(기존 동작).
    """
    return convert_office(input_path, output_path, vision=vision, **kwargs)


def convert_xlsx(
    input_path: Path | str, output_path: Path | str, *, vision: bool = False, **kwargs
) -> Optional[Path]:
    """XLSX/XLS(Excel) → Markdown. convert_office 위임.

    vision=True 면 시트별 이미지를 vision 해설해 해당 `## <시트명>` 섹션 뒤에 삽입.
    (xls 구형 바이너리는 vision 대상 아님 — 텍스트 그대로.) 기본 False(기존 동작).
    """
    return convert_office(input_path, output_path, vision=vision, **kwargs)


def convert_html(input_path: Path | str, output_path: Path | str, **kwargs) -> Optional[Path]:
    """HTML/HTM → Markdown. convert_office 위임."""
    return convert_office(input_path, output_path, **kwargs)


def convert_csv(input_path: Path | str, output_path: Path | str, **kwargs) -> Optional[Path]:
    """CSV → Markdown(표). convert_office 위임."""
    return convert_office(input_path, output_path, **kwargs)


# ──────────────────────────────────────────────────────────────────────────────
# DOCX — engine="markitdown"(기본) | engine="vision"
# ──────────────────────────────────────────────────────────────────────────────

# vision docx 청크 결합 구분자 — extract_docx_multimodal 와 동일 계약.
_VISION_CHUNK_SEP = "\n\n---\n\n"
# extract_docx_multimodal 가 truncation(finish_reason=length) 시 본문에 삽입하는 마커.
_VISION_TRUNCATED_MARKER = "<!-- TRUNCATED"


def _convert_docx_vision(
    input_path: Path,
    output_path: Path,
    *,
    chunk_chars: Optional[int] = None,
    **kwargs,
) -> Optional[Path]:
    """이미지 핵심 docx 용 멀티모달(vision) 변환 경로.

    기존 extract_docx_multimodal.py 의 **공개 함수를 재활용**한다
    (그 모듈은 수정/삭제 금지). 단, 원본 process_docx() 는 모듈-레벨 OUTPUT_DIR 에
    저장하고 반환값이 없어 convert_file(output_path) 계약과 맞지 않으므로,
    동일 로직(블록 추출 → 길이 청크 → LLM 정형화 → 결합)을 여기서 조립해
    호출자가 지정한 output_path 에 저장한다.

    truncation 감지 시 .partial.md 로 저장(완성본 위장 금지 — 원본 동작 보존).

    Args:
        chunk_chars: 길이 기준 청크 분할 임계(문자). None → 원본 DOCX_CHUNK_CHARS.
    """
    # 멀티모달 docx 모듈 + provider 추상화 lazy import (워크스페이스 루트 import 경로).
    try:
        import extract_docx_multimodal as _dm  # type: ignore
    except ImportError as e:  # noqa: BLE001
        raise ImportError(
            "vision docx 경로에는 워크스페이스의 extract_docx_multimodal 모듈이 "
            "필요합니다(라이브러리 단독 배포 시 미포함). engine='markitdown' 을 "
            "사용하거나 워크스페이스 내에서 실행하세요."
        ) from e

    try:
        from fmdw import ollama_extractor as ox  # type: ignore
    except ImportError:  # pragma: no cover — 패키지 내부 경로.
        import ollama_extractor as ox  # type: ignore

    blocks = _dm.extract_blocks_from_docx(str(input_path))
    if not blocks:
        return None

    max_chars = chunk_chars if chunk_chars is not None else _dm.DOCX_CHUNK_CHARS
    chunks = _dm.chunk_blocks(blocks, max_chars)

    md_parts: list[str] = []
    truncated = False
    for chunk in chunks:
        part = ox.extract_text_prompt(_dm._build_prompt(chunk))
        md_parts.append(part)
        if part and _VISION_TRUNCATED_MARKER in part:
            truncated = True

    md_text = _VISION_CHUNK_SEP.join(md_parts)
    if not md_text or not md_text.strip():
        return None

    # truncation 시 완성본으로 위장하지 않는다 → .partial.md 로 저장.
    if truncated:
        save_path = output_path.with_suffix("").with_name(output_path.stem + ".partial.md")
    else:
        save_path = output_path
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_text(md_text, encoding="utf-8")
    return save_path


def convert_docx(
    input_path: Path | str,
    output_path: Path | str,
    *,
    engine: str = "markitdown",
    vision: bool = False,
    **kwargs,
) -> Optional[Path]:
    """DOCX → Markdown. engine 으로 변환 경로 선택.

    Args:
        input_path : 원본 .docx 경로.
        output_path: 출력 .md 경로.
        engine     : "markitdown"(기본) — markitdown(mammoth, 구조 보존) 경유.
                     "vision"          — 멀티모달(이미지 분석) 경로(LLM 호출).
        vision     : engine="markitdown" 경로에서 True 면 docx 내 이미지를 fmdw vision
                     엔진으로 해설해 본문 끝 `## 이미지 해설` 섹션에 모아 삽입한다(근사
                     매핑 — markitdown 본문에 인라인 이미지 마커가 없어 정밀 위치 매핑은
                     불가). 기본 False(기존 동작). engine="vision" 일 때는 그 경로가 이미
                     이미지를 LLM 으로 다루므로 본 인자는 무시(중복 해설 방지).
        **kwargs   : engine="vision" 시 chunk_chars 등 전달, vision provider override
                     (provider=) 등도 수용.

    Returns:
        저장된 파일 경로(Path). 빈 결과면 None. (vision 경로에서 truncation 시
        .partial.md 경로를 반환.)

    Raises:
        ValueError : 알 수 없는 engine.
        ImportError: 해당 engine 의존성 미설치.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"입력 파일 없음: {input_path}")

    if engine == "markitdown":
        return convert_office(input_path, output_path, vision=vision, **kwargs)
    if engine == "vision":
        # engine="vision" 은 자체 멀티모달 경로 — 이미지 해설 하이브리드(vision 인자)는
        # 미적용(중복 LLM 해설 방지). kwargs 에서 provider 등은 그대로 흘려보낸다.
        return _convert_docx_vision(input_path, output_path, **kwargs)
    raise ValueError(
        f"알 수 없는 docx engine: {engine!r} (지원: 'markitdown', 'vision')"
    )
