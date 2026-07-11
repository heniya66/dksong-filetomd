"""filestomdwgem HWP/HWPX → Markdown 변환 파이프라인 (pyhwp2md 경유).

대상 포맷: hwp / hwpx (한글 워드프로세서 문서).

설계 원칙:
  - **lazy import**: `pyhwp2md` 는 함수 호출 시점에만 import 한다. 미설치여도
    `import fmdw` / `import fmdw.hwp_pipeline` 자체는 성공해야 한다
    (optional extra `filestomdwgem[hwp]` 로 분리; pyhwp 베타라 설치 실패 가능).
  - 미설치/import 실패 시 사용 시점에 친절한 ImportError 로 설치 안내.
  - 변환 결과는 호출자가 지정한 output_path(.md)에 UTF-8 로 저장하고 Path 를 반환.
    빈 결과(공백만)면 파일을 만들지 않고 None 을 반환한다.

참고:
  - 'hephaex/hwp2md' 패키지는 존재하지 않으므로 PyPI 의 `pyhwp2md`(MIT)를 사용.
  - `pyhwp2md` 는 `convert(path) -> markdown(str)` API 를 제공한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# hwp extra 미설치/설치 실패 시 사용자에게 보여줄 친절한 설치 안내.
_HWP_INSTALL_HINT = (
    "pyhwp2md 가 설치되어 있지 않거나 import 에 실패했습니다. hwp/hwpx 변환에는 "
    "추가 설치가 필요합니다:\n"
    "    pip install 'filestomdwgem[hwp]'\n"
    "(pyhwp 계열은 베타라 환경에 따라 설치/동작이 실패할 수 있습니다.)"
)


def _require_pyhwp2md():
    """pyhwp2md.convert 함수를 lazy import. 미설치/실패 시 친절한 ImportError.

    pyhwp 계열은 베타라 import 자체가 ImportError 외 다른 예외를 낼 수도 있어
    Exception 을 광범위하게 잡아 설치 안내로 재포장한다.
    """
    try:
        from pyhwp2md import convert  # type: ignore
    except Exception as e:  # noqa: BLE001 — 베타 패키지 import 실패 폭넓게 흡수.
        raise ImportError(_HWP_INSTALL_HINT) from e
    return convert


def convert_hwp(
    input_path: Path | str,
    output_path: Path | str,
    *,
    vision: bool = False,
    **kwargs,
) -> Optional[Path]:
    """HWP/HWPX → Markdown 변환.

    pyhwp2md.convert 로 markdown 문자열을 얻어 output_path 에 .md 로 저장하고
    Path 를 반환한다.

    Args:
        input_path : 원본 .hwp/.hwpx 경로.
        output_path: 출력 .md 경로.
        vision     : True 면 문서 내 이미지를 fmdw vision 엔진으로 해설해 삽입(opt-in,
                     기본 False). **hwpx(.hwpx)만** 지원 — ZIP(Zip archive) 구조라 내부
                     media 를 추출해 본문 끝 `## 이미지 해설` 섹션에 모아 삽입한다(위치
                     정보를 본문 좌표로 매핑하기 어려워 말미 모아삽입 — 한계). **hwp
                     (구형 바이너리)는 이미지 추출 난이도가 높아 vision=True 여도 텍스트만**
                     변환한다(이미지 skip). False(기본)면 기존 동작과 100% 동일.
        **kwargs   : 향후 확장용 + vision provider override(provider=) 등 수용.

    Returns:
        저장된 파일 경로(Path). 변환 결과가 비어 있으면 None.

    Raises:
        ImportError      : pyhwp2md 미설치/import 실패 시(설치 안내 포함).
        FileNotFoundError: input_path 가 존재하지 않을 때.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"입력 파일 없음: {input_path}")

    convert = _require_pyhwp2md()
    markdown = convert(str(input_path))

    if not markdown or not str(markdown).strip():
        return None

    markdown = str(markdown)
    # vision 하이브리드 — hwpx(ZIP)만 이미지 해설 삽입(hwp 바이너리는 텍스트만).
    if vision and input_path.suffix.lower() == ".hwpx":
        markdown = _apply_hwpx_vision(markdown, input_path, **kwargs)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def _apply_hwpx_vision(
    markdown: str,
    input_path: Path,
    *,
    provider: Optional[str] = None,
    **_ignored,
) -> str:
    """hwpx vision 경로: ZIP media 이미지를 vision 해설해 본문 끝에 모아 삽입(safe degrade).

    office_vision 자체가 모든 실패를 흡수(입력 markdown 반환)하므로 기존 텍스트 결과를
    절대 깨지 않는다(이미지 0개/추출 실패/vision 실패 시 입력 그대로).
    """
    try:
        from fmdw import office_vision  # lazy — vision=True 일 때만 로드.
    except ImportError:  # pragma: no cover — 직접 실행/경로 차이 대비.
        import office_vision  # type: ignore
    return office_vision.augment_with_vision(
        markdown, input_path, "hwpx", provider=provider
    )
