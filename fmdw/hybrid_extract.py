"""
fmdw.hybrid_extract — 통합 하이브리드 추출기

지원 포맷:
  .hwpx  → pyhwp2md(텍스트) + ZIP BinData 이미지 + vision + 캡션 매핑 + 인라인 삽입
  .hwp   → pyhwp2md(텍스트, 빈출력이면 hwp5txt 폴백) + hwp5proc cat(이미지) + vision
  .docx  → markitdown(텍스트) + ZIP word/media/ 이미지 + vision
  .pptx  → markitdown(텍스트) + ZIP ppt/media/ 이미지 + vision
  .xlsx  → markitdown(텍스트) + ZIP xl/media/ 이미지 + vision

hwpx 전용 개선(3가지):
  1) XML section 파싱으로 도면 N. 캡션(ground truth)을 이미지에 매핑
  2) vision 프롬프트에 공식 캡션 ground truth + 상세화 요구 추가
  3) vision 결과를 본문 캡션 라인 뒤에 인라인 삽입(못 찾으면 말미 섹션으로 폴백)

이미지 필터:
  - 크기 > 8192 bytes 만 유지
  - sha256 중복 제거(반복 로고/머리말 제거)
  - .bmp → Pillow로 PNG 변환; .emf/.wmf → 변환 불가, 경고 후 스킵

반환: dict {"md": str, "text_len": int, "n_images": int, "n_filtered": int, "n_figures": int}
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# fmdw 워크스페이스 루트를 path에 추가(이 파일이 fmdw/ 하위에 있으므로 부모 디렉토리)
_FMDW_ROOT = Path(__file__).resolve().parent.parent
if str(_FMDW_ROOT) not in sys.path:
    sys.path.insert(0, str(_FMDW_ROOT))

_VENV_BIN = _FMDW_ROOT / ".venv" / "bin"

# ─── 이미지 크기 필터 (bytes) ────────────────────────────────────────────────
_IMAGE_MIN_BYTES = 8192

# ─── 지원 확장자 ────────────────────────────────────────────────────────────
HWP_EXTS = {".hwp", ".hwpx"}
OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
SUPPORTED_EXTS = HWP_EXTS | OFFICE_EXTS

# ZIP 내 미디어 경로 (포맷별)
_ZIP_MEDIA: dict[str, str] = {
    ".docx": "word/media/",
    ".pptx": "ppt/media/",
    ".xlsx": "xl/media/",
    ".hwpx": "BinData/",
}

# vision에서 처리할 이미지 확장자
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".emf", ".wmf"}


# ─── 텍스트 추출 ─────────────────────────────────────────────────────────────

def _extract_text_hwp(input_path: Path) -> str:
    """HWP/HWPX 텍스트 추출: pyhwp2md → hwp5txt 폴백 (둘 다 실패 시 빈 문자열)."""
    pyhwp2md = _VENV_BIN / "pyhwp2md"
    hwp5txt = _VENV_BIN / "hwp5txt"

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        # 1차: pyhwp2md
        result = subprocess.run(
            [str(pyhwp2md), str(input_path), "-o", str(tmp_path)],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and tmp_path.exists():
            text = tmp_path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text
            print(f"  [INFO] pyhwp2md 빈출력 → hwp5txt 폴백")

        # 2차: hwp5txt (stderr 무시, stdout만)
        if hwp5txt.exists():
            r2 = subprocess.run(
                [str(hwp5txt), str(input_path)],
                capture_output=True,
            )
            text2 = r2.stdout.decode("utf-8", errors="replace")
            if text2.strip():
                return text2
            print(f"  [INFO] hwp5txt도 빈출력")

        return ""
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _extract_text_office(input_path: Path) -> str:
    """DOCX/PPTX/XLSX 텍스트 추출: markitdown."""
    try:
        from markitdown import MarkItDown
        result = MarkItDown().convert(str(input_path))
        return result.text_content if hasattr(result, "text_content") else result.markdown
    except Exception as e:
        print(f"  [WARN] markitdown 실패 ({input_path.name}): {e}")
        return ""


# ─── 이미지 추출 ─────────────────────────────────────────────────────────────

def _extract_images_zip(input_path: Path, media_prefix: str, work_dir: Path) -> list[Path]:
    """ZIP 기반 파일(hwpx/docx/pptx/xlsx)에서 이미지 추출."""
    images: list[Path] = []
    try:
        with zipfile.ZipFile(input_path, "r") as zf:
            entries = [
                n for n in zf.namelist()
                if n.startswith(media_prefix)
                and Path(n).suffix.lower() in _IMG_EXTS
                and not n.endswith("/")
            ]
            for entry in sorted(entries):
                fname = Path(entry).name
                out_path = work_dir / fname
                out_path.write_bytes(zf.read(entry))
                images.append(out_path)
    except zipfile.BadZipFile:
        print(f"  [WARN] ZIP 열기 실패: {input_path.name}")
    return images


def _extract_images_hwp_ole(input_path: Path, work_dir: Path) -> list[Path]:
    """
    HWP(OLE) BinData 이미지 추출.
    1차: hwp5proc cat --ole <file> BinData/BINxxxx.ext
    2차: hwp5proc 실패 시 olefile 로 직접 OLE 스트림 읽기
    """
    hwp5proc = _VENV_BIN / "hwp5proc"
    images: list[Path] = []

    # hwp5proc ls --ole 로 BinData 스트림 목록 확인 (--ole: OLE 구조 그대로 탐색)
    try:
        ls_result = subprocess.run(
            [str(hwp5proc), "ls", "--ole", str(input_path)],
            capture_output=True, text=True,
        )
        bin_entries = [
            line.strip() for line in ls_result.stdout.splitlines()
            if line.strip().startswith("BinData/")
            and Path(line.strip()).suffix.lower() in _IMG_EXTS
        ]
    except Exception as e:
        print(f"  [WARN] hwp5proc ls 실패: {e}")
        bin_entries = []

    if not bin_entries:
        print(f"  [INFO] BinData 스트림 없음")
        return images

    print(f"  [INFO] BinData 스트림 {len(bin_entries)}개 발견: {[Path(e).name for e in bin_entries]}")

    # hwp5proc cat (--ole 없음) 으로 각 스트림 추출 — zlib 압축 자동 해제됨.
    # ※ --ole 플래그는 압축 해제를 건너뛰어 raw zlib 데이터를 반환하므로 사용하지 않음.
    for entry in bin_entries:
        fname = Path(entry).name
        out_path = work_dir / fname
        try:
            cat_result = subprocess.run(
                [str(hwp5proc), "cat", str(input_path), entry],
                capture_output=True,
            )
            if cat_result.returncode == 0 and cat_result.stdout:
                out_path.write_bytes(cat_result.stdout)
                images.append(out_path)
            else:
                raise RuntimeError(f"returncode={cat_result.returncode}, stderr={cat_result.stderr[:200]}")
        except Exception as e:
            print(f"  [WARN] hwp5proc cat 실패 ({entry}): {e} → olefile 폴백")
            # olefile 폴백: OLE 스트림 raw 읽기 후 zlib 수동 해제
            try:
                import zlib
                import olefile
                ole = olefile.OleFileIO(str(input_path))
                parts = entry.split("/")
                if ole.exists(parts):
                    raw = ole.openstream(parts).read()
                    # HWP BinData 스트림은 zlib compressed (헤더 없는 deflate)
                    try:
                        data = zlib.decompress(raw, -15)  # raw deflate
                    except zlib.error:
                        try:
                            data = zlib.decompress(raw)    # zlib 헤더 있는 경우
                        except zlib.error:
                            data = raw  # 이미 해제된 경우
                    out_path.write_bytes(data)
                    images.append(out_path)
                else:
                    print(f"  [WARN] olefile: 스트림 없음 ({entry})")
                ole.close()
            except Exception as e2:
                print(f"  [WARN] olefile 폴백도 실패 ({entry}): {e2}")

    return images


# ─── 이미지 필터 (크기 + 중복 제거) ─────────────────────────────────────────

def _filter_images(images: list[Path]) -> list[Path]:
    """크기 > 8192B 필터 + sha256 중복 제거."""
    seen: set[str] = set()
    filtered: list[Path] = []
    for img in images:
        if not img.exists():
            continue
        size = img.stat().st_size
        if size <= _IMAGE_MIN_BYTES:
            print(f"  [SKIP] 크기 미달 {img.name} ({size}B <= {_IMAGE_MIN_BYTES}B)")
            continue
        digest = hashlib.sha256(img.read_bytes()).hexdigest()
        if digest in seen:
            print(f"  [SKIP] 중복 {img.name}")
            continue
        seen.add(digest)
        filtered.append(img)
    return filtered


# ─── 이미지 포맷 변환 ─────────────────────────────────────────────────────────

def _normalize_image(img_path: Path, work_dir: Path) -> Path | None:
    """
    비PNG/JPG 포맷을 Pillow로 PNG 변환.
    .emf/.wmf는 변환 불가 → None 반환(스킵).
    """
    suffix = img_path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        return img_path
    if suffix in {".emf", ".wmf"}:
        print(f"  [WARN] EMF/WMF 변환 불가, 스킵: {img_path.name}")
        return None
    if suffix == ".bmp":
        try:
            from PIL import Image
            png_path = work_dir / (img_path.stem + "_converted.png")
            Image.open(img_path).convert("RGBA").save(png_path, "PNG")
            print(f"  [INFO] BMP→PNG 변환: {img_path.name}")
            return png_path
        except Exception as e:
            print(f"  [WARN] BMP 변환 실패 ({img_path.name}): {e}")
            return None
    print(f"  [WARN] 미지원 포맷, 스킵: {img_path.name}")
    return None


# ─── hwpx 캡션 매핑 ───────────────────────────────────────────────────────────

def _hwpx_figure_captions(hwpx_path: Path) -> dict[str, str]:
    """
    hwpx XML에서 이미지 파일명 → "도면 N: 제목" 캡션 매핑을 반환.

    알고리즘:
    1. Contents/section*.xml 을 파일명 순으로 이어붙여 단일 XML 문자열 생성
    2. binaryItemIDRef="imageN" 위치 수집
    3. <hp:t>도면 N. 제목</hp:t> 마침표형 캡션 위치 수집
       (콜론형 "도면 N :" 은 "도면의 간단한 설명" 목차이므로 제외)
    4. 각 이미지 ref 위치에서 역방향으로 가장 가까운 마침표형 캡션 할당
    5. 중복 참조(image1이 2회) → 마침표형 캡션 우선, 없으면 '대표도' fallback
    6. 캡션을 "도면 N: 제목" 형태로 정규화

    반환: {이미지파일명(imageN.png/.bmp 등): "도면 N: 제목"}
    실패 시 빈 dict 반환(호출자가 폴백 처리)
    """
    try:
        with zipfile.ZipFile(hwpx_path, "r") as zf:
            # section 파일 목록을 파일명 순으로 정렬하여 이어붙임
            section_names = sorted(
                n for n in zf.namelist()
                if re.match(r"Contents/section\d*\.xml$", n)
            )
            xml = "\n".join(
                zf.read(n).decode("utf-8", errors="replace")
                for n in section_names
            )
            # BinData 내 실제 이미지 파일명 목록 (imageN.ext)
            bindata_files = [
                Path(n).name for n in zf.namelist()
                if n.startswith("BinData/") and Path(n).suffix.lower() in _IMG_EXTS
            ]
    except Exception as e:
        print(f"  [WARN] hwpx XML 읽기 실패: {e}")
        return {}

    # ── binaryItemIDRef 위치 수집 ───────────────────────────────────────────
    # {imageN: [(xml_pos, ...)]} — 같은 이미지가 여러 번 참조될 수 있음
    ref_positions: dict[str, list[int]] = {}
    for m in re.finditer(r'binaryItemIDRef="([^"]+)"', xml):
        ref_id = m.group(1)  # e.g. "image1"
        ref_positions.setdefault(ref_id, []).append(m.start())

    # ── 마침표형 캡션 위치 수집 ("도면 N. 제목", "도면 N: 제목") ─────────────
    # 콜론형 중에서도 제목이 비어 있는 것("도면 2 : ") 은 제외하고
    # 제목이 있는 마침표형만 수집한다.
    # 패턴: 도면 + 공백* + 숫자+ + 공백* + [.] + 공백* + 제목(1글자 이상)
    dot_caps: list[tuple[int, int, str]] = []  # (xml_pos, fig_num, title)
    for m in re.finditer(
        r"<hp:t[^>]*>\s*(도\s*면\s*(\d+)\s*\.\s*(.+?))\s*</hp:t>",
        xml,
    ):
        fig_num = int(m.group(2))
        title = re.sub(r"\s+", " ", m.group(3)).strip()
        if title:
            dot_caps.append((m.start(), fig_num, title))

    # 대표도 위치 (fallback용)
    rep_caps: list[tuple[int, str]] = []
    for m in re.finditer(r"<hp:t[^>]*>\s*(대표도[^<]*)</hp:t>", xml):
        rep_caps.append((m.start(), re.sub(r"\s+", " ", m.group(1)).strip()))

    if not dot_caps:
        print("  [INFO] hwpx 마침표형 캡션 0건 → 캡션 매핑 불가")
        return {}

    print(f"  [INFO] hwpx 마침표형 캡션 {len(dot_caps)}건 발견")

    # ── 이미지별 캡션 할당 ──────────────────────────────────────────────────
    # 전략: 각 이미지 ref 위치에서 역방향으로 가장 가까운 dot_cap을 찾는다.
    # image1 은 두 번 참조되므로 마침표형 캡션이 있는 ref(뒤쪽)를 우선 사용.
    result: dict[str, str] = {}  # {imageN_stem: "도면 N: 제목"}

    for ref_id, positions in ref_positions.items():
        best_caption: str | None = None
        best_dist = float("inf")

        for ref_pos in positions:
            # ref_pos 이전에서 가장 가까운 dot_cap 탐색
            candidates = [(ref_pos - cap_pos, fig_num, title)
                          for (cap_pos, fig_num, title) in dot_caps
                          if cap_pos < ref_pos]
            if candidates:
                candidates.sort(key=lambda x: x[0])
                dist, fig_num, title = candidates[0]
                if dist < best_dist:
                    best_dist = dist
                    best_caption = f"도면 {fig_num}: {title}"

        # fallback: 대표도
        if best_caption is None and rep_caps:
            for rep_pos, rep_text in rep_caps:
                candidates2 = [(abs(rep_pos - p), rep_text) for p in positions]
                if candidates2:
                    best_caption = rep_text

        if best_caption:
            # BinData 실제 파일명과 매핑 (imageN → imageN.png / imageN.bmp 등)
            for fname in bindata_files:
                stem = Path(fname).stem  # "image1"
                if stem == ref_id:
                    result[fname] = best_caption
                    break
            else:
                # BinData에 없어도 일단 stem으로 보관
                result[ref_id] = best_caption

    print(f"  [INFO] 캡션 매핑 완료: {result}")
    return result


# ─── Vision 분석 ──────────────────────────────────────────────────────────────

def _analyze_image_vision(image_path: Path, figure_index: int, caption: str | None = None) -> str | None:
    """
    fmdw ollama_extractor로 이미지 vision 분석. 실패 시 None.

    Args:
        image_path: 분석할 이미지 파일
        figure_index: 순번 (폴백 헤더용)
        caption: hwpx 공식 캡션 ground truth (있으면 프롬프트에 포함)
    """
    try:
        from fmdw import ollama_extractor as ox
        import extract_all_via_pdf as E

        caption_instruction = (
            f"이 그림의 문서상 공식 명칭(ground truth): {caption}. 이 명칭을 제목으로 사용하라.\n"
            if caption else ""
        )
        prompt = (
            "You are converting a patent/document figure to highly detailed Markdown.\n"
            + caption_instruction
            + "아래 항목을 **매우 구체적이고 상세히** 작성하라(추측 금지, 보이는 것만):\n"
            + E.FIGURE_RULES
            + "\n추가 상세 요구: "
            "(a) 이미지 내 모든 텍스트·숫자·부재번호를 verbatim 전사, "
            "(b) 모든 화살표/연결선을 source→target + 선 위 라벨까지 빠짐없이, "
            "(c) 블록의 공간 배치(상/하/좌/우), "
            "(d) 해석을 3~6문장으로 구체적으로.\n"
            "Output ONLY Markdown."
        )
        result = ox.extract_image(prompt, str(image_path))
        return result
    except Exception as e:
        print(f"  [WARN] vision 분석 실패 ({image_path.name}): {e}")
        return None


# ─── hwpx 인라인 삽입 ─────────────────────────────────────────────────────────

def _inline_insert_figures(text_md: str, figure_map: dict[int, str]) -> tuple[str, set[int]]:
    """
    pyhwp2md 본문 텍스트에 figure 섹션을 인라인 삽입.

    본문에서 "도면 N. 제목" 마침표형 라인을 찾아 그 바로 뒤에 figure_map[N]을 삽입.
    "도면의 간단한 설명" 섹션 안의 콜론형("도면 N :") 라인은 매칭에서 제외.

    Returns:
        (수정된 텍스트, 인라인 삽입된 도면번호 집합)
    """
    lines = text_md.splitlines(keepends=True)
    result_lines: list[str] = []
    inserted: set[int] = set()

    # "도면의 간단한 설명" 섹션 범위 감지 (이 범위 내 라인은 매칭 금지)
    desc_section_start = -1
    desc_section_end = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "도면의 간단한 설명":
            desc_section_start = i
        # 섹션 끝: 도면 설명 이후 첫 번째 비어있지 않은 다른 섹션 헤더
        # (여기서는 단순히 "도면 N. " 마침표형 캡션 구역까지만 보호)

    for i, line in enumerate(lines):
        result_lines.append(line)
        stripped = line.strip()

        # 마침표형 캡션 매칭: "도면 N." 뒤에 내용이 있으면 매칭
        # (공백 여러 개 포함, "도면 N. 제목" / "도면 N.제목" / "도면 N.  제목" 모두)
        # "도면의 간단한 설명" 섹션 콜론형은 마침표가 없으므로 자연히 제외됨
        m = re.match(r"^도\s*면\s*(\d+)\s*\.\s*\S", stripped.replace("  ", " "))
        if m:
            fig_num = int(m.group(1))
            # 마침표형이므로 그냥 매칭 (콜론형은 마침표가 없어 자연히 제외)
            if fig_num in figure_map:
                # 현재 라인이 개행 없이 끝나는 경우(마지막 라인 등) 앞에 개행 보장
                if result_lines and not result_lines[-1].endswith("\n"):
                    result_lines.append("\n")
                result_lines.append(figure_map[fig_num] + "\n")
                inserted.add(fig_num)

    return "".join(result_lines), inserted


# ─── 메인 공개 API ────────────────────────────────────────────────────────────

def hybrid_convert(input_path: Path, source_rel: str = "") -> dict:
    """
    통합 하이브리드 변환.

    Args:
        input_path: 변환할 파일 경로
        source_rel: frontmatter의 source 값 (빈 문자열이면 frontmatter 미포함)

    Returns:
        {
            "md": str,          # 최종 Markdown 텍스트
            "text_len": int,    # 텍스트 부분 길이
            "n_images": int,    # 필터 전 추출 이미지 수
            "n_filtered": int,  # 필터 후 이미지 수 (vision 투입)
            "n_figures": int,   # 생성된 Figure 섹션 수
        }
    """
    input_path = Path(input_path).resolve()
    suffix = input_path.suffix.lower()

    print(f"[hybrid_convert] {input_path.name} ({suffix})")

    # ── 1. 텍스트 추출 ──────────────────────────────────────────────────────
    print("  [1/3] 텍스트 추출 중...")
    if suffix in HWP_EXTS:
        text_md = _extract_text_hwp(input_path)
    elif suffix in OFFICE_EXTS:
        text_md = _extract_text_office(input_path)
    else:
        text_md = ""

    if not text_md.strip() and suffix in HWP_EXTS:
        text_note = "\n\n> [참고] 텍스트 추출 실패(도구 한계) — 이미지 분석만 수록\n"
    else:
        text_note = ""

    print(f"  텍스트 길이: {len(text_md):,} chars")

    # ── 2. 이미지 추출 ──────────────────────────────────────────────────────
    print("  [2/3] 이미지 추출 중...")
    figure_sections: list[str] = []
    n_images_raw = 0
    n_filtered = 0

    with tempfile.TemporaryDirectory() as _tmp:
        work_dir = Path(_tmp)

        if suffix == ".hwp":
            raw_images = _extract_images_hwp_ole(input_path, work_dir)
        elif suffix in _ZIP_MEDIA:
            prefix = _ZIP_MEDIA[suffix]
            raw_images = _extract_images_zip(input_path, prefix, work_dir)
        else:
            raw_images = []

        n_images_raw = len(raw_images)
        print(f"  추출 이미지: {n_images_raw}개")

        # 필터 적용
        filtered_images = _filter_images(raw_images)
        n_filtered = len(filtered_images)
        print(f"  필터 후: {n_filtered}개")

        # ── 2b. hwpx 전용: 캡션 매핑 ────────────────────────────────────────
        # {이미지파일명(image1.png): "도면 N: 제목"}
        caption_map: dict[str, str] = {}
        if suffix == ".hwpx" and raw_images:
            print("  [hwpx] 도면 캡션 매핑 중...")
            caption_map = _hwpx_figure_captions(input_path)

        # ── 3. Vision 분석 ──────────────────────────────────────────────────
        # figure_map: {도면번호(int): "### 캡션\n\n본문..."} — 인라인 삽입용
        # figure_sections: 인라인 삽입 실패분 + 非hwpx용 말미 섹션 목록
        figure_map: dict[int, str] = {}   # hwpx 인라인 삽입용 {N: section_str}
        figure_sections: list[str] = []   # 말미 섹션(폴백 + 非hwpx)

        if filtered_images:
            print(f"  [3/3] Vision 분석 중 ({n_filtered}개)...")
            for i, img_path in enumerate(filtered_images, start=1):
                # 포맷 정규화
                normalized = _normalize_image(img_path, work_dir)
                if normalized is None:
                    continue

                # hwpx 캡션 조회
                caption = caption_map.get(img_path.name)
                if caption is None:
                    # stem 매칭 시도 (확장자 달라질 경우 대비)
                    caption = caption_map.get(img_path.stem)

                print(f"    [{i}/{n_filtered}] {img_path.name} "
                      f"캡션={repr(caption)} 분석 중...")
                result = _analyze_image_vision(normalized, i, caption=caption)

                if result:
                    # ── 헤더 결정 ──────────────────────────────────────────
                    # hwpx + 캡션 있음 → 공식 캡션을 헤더로 사용
                    # 그 외 → vision 자체 헤더 흡수 or 순번 헤더
                    res_stripped = result.lstrip()
                    first_line, _sep, rest = res_stripped.partition("\n")
                    fl = first_line.strip()
                    fl_label = fl.lstrip("#").strip()
                    is_fig_header = fl.startswith("#") and (
                        fl_label.lower().startswith("figure")
                        or fl_label.startswith("도면")
                        or fl_label.startswith("그림")
                    )

                    if caption:
                        # 공식 캡션 헤더 사용 (vision 자체 헤더는 흡수)
                        header = f"### {caption}"
                        body = rest.lstrip() if is_fig_header else res_stripped
                        # body 내부에 동일 캡션의 ### 헤더가 재등장하면 제거(이중 헤더 방지)
                        # 예: vision이 중간에 "### 도면 N: ..." 을 다시 삽입하는 경우
                        body = re.sub(
                            r"(?m)^#{1,4}\s+" + re.escape(caption) + r"\s*\n?",
                            "",
                            body,
                        )
                        section = (
                            f"{header}\n"
                            f"*(출처 이미지: `{img_path.name}`)*\n\n{body}"
                        )
                        # 도면번호 추출해 figure_map에 저장 (인라인 삽입용)
                        nm = re.match(r"도면\s*(\d+)", caption)
                        if nm:
                            figure_map[int(nm.group(1))] = section
                        else:
                            figure_sections.append(section)
                    elif is_fig_header:
                        # vision 자체 헤더 흡수
                        if ":" in fl:
                            title = fl.split(":", 1)[1].strip()
                        elif "：" in fl:
                            title = fl.split("：", 1)[1].strip()
                        else:
                            title = ""
                        header = (f"### 도면 {i}: {title}" if title
                                  else f"### 도면 {i}")
                        section = (
                            f"{header}\n"
                            f"*(출처 이미지: `{img_path.name}`)*\n\n{rest.lstrip()}"
                        )
                        figure_sections.append(section)
                    else:
                        section = (
                            f"### 도면 {i} (`{img_path.name}`)\n\n{res_stripped}"
                        )
                        figure_sections.append(section)

                    print(f"    -> 완료 ({len(result)} chars)")
                else:
                    print(f"    -> 스킵 (vision 실패)")
        else:
            print("  [3/3] 이미지 없음 — vision 생략")

    # ── 4. MD 조립 ──────────────────────────────────────────────────────────
    parts: list[str] = []

    if source_rel:
        parts.append(f"---\nsource: {source_rel}\n---\n\n")

    # hwpx + figure_map 있음 → 인라인 삽입
    inline_inserted: set[int] = set()
    if suffix == ".hwpx" and figure_map:
        print(f"  [hwpx] 본문 인라인 삽입 시도 ({len(figure_map)}개)...")
        text_md_inlined, inline_inserted = _inline_insert_figures(text_md, figure_map)
        parts.append(text_md_inlined)
        not_inserted = set(figure_map.keys()) - inline_inserted
        if not_inserted:
            print(f"  [hwpx] 인라인 미삽입 → 말미 섹션으로: 도면 {sorted(not_inserted)}")
            for fig_num in sorted(not_inserted):
                figure_sections.append(figure_map[fig_num])
        print(f"  [hwpx] 인라인 삽입 완료: 도면 {sorted(inline_inserted)}")
    else:
        parts.append(text_md)

    if text_note:
        parts.append(text_note)

    if figure_sections:
        parts.append("\n\n---\n\n## 도면 분석 (이미지)\n\n")
        parts.append("\n\n---\n\n".join(figure_sections))

    # 전체 figure 수 = 인라인 삽입 + 말미 섹션
    n_figures_total = len(inline_inserted) + len(figure_sections)
    final_md = "".join(parts)

    return {
        "md": final_md,
        "text_len": len(text_md),
        "n_images": n_images_raw,
        "n_filtered": n_filtered,
        "n_figures": n_figures_total,
        "n_inline": len(inline_inserted),
    }
