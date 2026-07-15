#!/usr/bin/env python3
"""convert_project.py — filestomdwgem CWD 스테이징 래퍼

FILESTOMDWGEM-INTEGRATION.md §2 "Option A CWD 스테이징 래퍼" 구현.
프로젝트 원본(01_Hardware/·03_DOC/ 등)을 스캔해 Markdown으로 변환 후
04_RAG/processed_md/<domain>/ 에 수집하고 sources.yaml 을 갱신한다.

사용법:
  python convert_project.py --project <base_path> [옵션]

  --project  <path>  프로젝트 루트 절대 경로 (필수)
  --domain   <name>  특정 도메인만 처리 (선택)
  --force            sources.yaml 지문 무시, 전체 재변환
  --figures          figure 크롭 PNG 별도 저장 (기본 ON — 명시 안 해도 동작)
  --no-figures       figure 크롭 비활성화 (이미지 별도 저장 끔)
  --dry-run          파일 변경 없이 계획만 출력
  --help             도움말 출력

상태 어휘 (sources.yaml status 필드):
  pending    변환 예정(초기값)
  converted  정상 변환 완료
  stale      원본이 변경됨 (sha256 불일치, 재변환 필요)
  skipped    증분 판정으로 건너뜀 (sha256 일치)
  native_md  이미 MD/텍스트 원본 — 변환 불필요
  failed     변환 실행 후 출력 MD 없음 — 재실행 시 재시도
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("[!] PyYAML 이 없습니다. 설치: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ── 상수 ──────────────────────────────────────────────────────────────────────
FMDW_MAIN = Path(__file__).parent / "extract_all_via_pdf.py"

# R10 M7: 동시 실행 락 파일핸들 — 프로세스 생존 동안 유지(GC 조기 해제 방지).
_LOCK_FH = None

# 변환 대상 확장자 (이미 MD/텍스트인 파일은 native_md 처리)
# .hwpx: hwp_pipeline.py 및 extract_all_via_pdf.py process_file/.main()이 지원
CONVERTIBLE_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".hwp", ".hwpx"}

# 도구가 실제 지원하는 이미지 포맷 (extract_all_via_pdf.py main() tasks 기준)
IMAGE_EXTS_SUPPORTED = {".png", ".jpg", ".jpeg"}
# 지원 불가 이미지 포맷 — 스캔 시 경고 후 제외 (silent drop 금지, W-1)
IMAGE_EXTS_UNSUPPORTED = {".tiff", ".tif", ".bmp", ".gif", ".webp"}

NATIVE_MD_EXTS = {".md", ".txt", ".rst"}

# ── 소스코드 래핑 수집 (code collect) ─────────────────────────────────────────
# 소스코드는 vision/ollama "변환"이 아니라 raw 텍스트를 .md 로 "래핑 수집" 한다.
# (PyMuPDF 등 문서 파서 미사용 — 그냥 텍스트로 읽어 코드펜스로 감쌈)
# 확장자 → 언어 식별자 (codesign-rag SourceCodeChunker frontmatter language: 계약)
# 난해한 언어는 제외하고 널리 쓰이는 언어 + 주요 설정 파일만 포함.
CODE_EXT_TO_LANG: dict[str, str] = {
    # 프로그래밍 언어
    ".py":    "python",
    ".c":     "c",
    ".h":     "cpp",
    ".hpp":   "cpp",
    ".hh":    "cpp",
    ".hxx":   "cpp",
    ".cpp":   "cpp",
    ".cc":    "cpp",
    ".cxx":   "cpp",
    ".ts":    "typescript",
    ".tsx":   "tsx",
    ".js":    "javascript",
    ".jsx":   "jsx",
    ".go":    "go",
    ".rs":    "rust",
    ".java":  "java",
    ".cs":    "csharp",
    ".kt":    "kotlin",
    ".swift": "swift",
    # 설정/빌드 파일
    ".ini":   "ini",
    ".toml":  "toml",
    ".yaml":  "yaml",
    ".yml":   "yaml",
    ".json":  "json",
    ".cmake": "cmake",
}

# 소스코드 수집은 source_code 도메인에서만 동작 (다른 도메인은 종전대로 무시)
CODE_COLLECT_DOMAIN = "source_code"

# 수집 제외 디렉토리 (가상환경·빌드 산출물·캐시 등)
CODE_EXCLUDE_DIRS = {
    ".venv", "venv", "env", ".env", ".pio", ".pio-venv", ".pio-venv-pip",
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".tox", ".ruff_cache", "site-packages",
    ".next", ".idea", ".vscode", ".omc", ".egg-info", "egg-info",
    ".cache", "target", "vendor", ".gradle",
}

# lock/생성/미니파이 파일 — 코드가 아니라 노이즈(검색·온톨로지 품질 저하) → 수집 제외.
CODE_EXCLUDE_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum", "uv.lock",
}
CODE_EXCLUDE_SUFFIXES = (".min.js", ".min.css", ".map")

# 원본 폴더 → (domain, filestomdwgem type) 매핑
# "어느 원본 폴더에서 왔는가"로 도메인 결정 (INTEGRATION §2 표 참조)
FOLDER_DOMAIN_MAP = [
    # (원본 폴더 패턴, domain, fmdw_type_hint)
    ("01_Hardware/datasheet", "datasheet",  None),
    ("01_Hardware/schematic", "schematic",  None),
    ("01_Hardware/pcb",       "schematic",  None),   # pcb도 schematic 도메인
    ("01_Hardware/mech",      "reference",  None),
    ("02_Software",           "source_code", None),
    ("03_DOC/design",         "design_doc", None),
    ("03_DOC/patent",         "design_doc", None),
    ("03_DOC/reference",      "reference",  None),
]

# 확장자 → filestomdwgem input type 매핑
EXT_TO_TYPE: dict[str, str] = {
    ".pdf":  "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".hwp":  "hwp",
    ".hwpx": "hwp",   # C-1: .hwpx는 hwp input 디렉토리로 스테이징
}
for _ext in IMAGE_EXTS_SUPPORTED:
    EXT_TO_TYPE[_ext] = "image"

TYPE_OUTPUT_DIR = {
    "pdf":   "output/pdf_md",
    "docx":  "output/docx_md",
    "pptx":  "output/pptx_md",
    "xlsx":  "output/xlsx_md",
    "hwp":   "output/hwp_md",
    "image": "output/image_md",
}


# ── SHA-256 지문 ──────────────────────────────────────────────────────────────
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha8(path: Path) -> str:
    """파일 경로 기반 8자 hex prefix — 스테이징 고유 이름 생성용."""
    return hashlib.sha256(path.as_posix().encode()).hexdigest()[:8]


# ── sources.yaml 로드/저장 ────────────────────────────────────────────────────
def load_sources(sources_yaml: Path) -> dict:
    if not sources_yaml.exists():
        return {"version": 1, "entries": []}
    with open(sources_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "version" not in data:
        data["version"] = 1
    if "entries" not in data or data["entries"] is None:
        data["entries"] = []
    return data


def _atomic_write(path: Path, data: str) -> None:
    """R9 F3(QA M4): 동일 디렉토리 tmp 기록 → os.replace 원자 치환.

    수백 엔트리 fingerprint 이력을 담는 sources.yaml 을 dump 도중 kill/디스크풀로
    반쯤 쓰인 채 남기지 않는다(다음 실행 load_sources 파싱 크래시·이력 유실 방지)."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save_sources(sources_yaml: Path, data: dict, dry_run: bool) -> None:
    if dry_run:
        return
    sources_yaml.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.dump(data, allow_unicode=True, default_flow_style=False,
                        sort_keys=False)
    _atomic_write(sources_yaml, payload)  # R9 F3: 비원자 dump → 원자 치환


def find_entry(entries: list, src_rel: str) -> dict | None:
    """sources.yaml entries 에서 src_rel 매칭 엔트리 반환."""
    for e in entries:
        if e.get("src") == src_rel:
            return e
    return None


# ── 스테이징 고유 파일명 생성 (C-2) ──────────────────────────────────────────
def staged_name(fpath: Path, base_path: Path) -> str:
    """src_rel 기준 고유 스테이징 파일명 생성.

    동일 stem(예: 명세서.pdf, 명세서.docx)이 다른 폴더에 있을 때 덮어쓰기를 방지한다.
    형식: <sha8_of_src_rel>__<원본파일명>
    collect_outputs 에서 동일 규칙으로 expected_md 파일명을 재구성할 수 있다.
    """
    src_rel = fpath.relative_to(base_path).as_posix()
    prefix = _sha8(fpath.relative_to(base_path))
    return f"{prefix}__{fpath.name}"


def staged_md_name(staged_fname: str) -> str:
    """스테이징 파일명에서 도구 출력 MD 파일명을 계산.

    extract_all_via_pdf.py 는 input_path.stem + ".md" 로 출력하므로
    스테이징 파일명의 stem(= "<sha8>__<원본stem>")에 .md 를 붙인다.
    """
    stem = Path(staged_fname).stem
    return stem + ".md"


def final_md_name(fpath: Path) -> str:
    """수집 후 processed_md 에 저장할 최종 MD 파일명.

    사람이 읽기 쉽도록 원본 stem 기반으로 복원한다.
    ⚠️ R9 F1(QA C2/C3): FOLDER_DOMAIN_MAP 은 서로 다른 원본 폴더를 같은 도메인으로
    매핑하므로(schematic+pcb→schematic, mech+reference→reference, design+patent→
    design_doc) 같은 stem 이 도메인 폴더 안에서 충돌할 수 있다 — collect_outputs 가
    소유자 대조 후 '타 원본 충돌'일 때만 unique_final_md_name 으로 유니크화한다.
    (같은 원본의 재변환 갱신은 기존 그대로 덮어쓰기.)
    """
    return fpath.stem + ".md"


def unique_final_md_name(fpath: Path, base_path: Path) -> str:
    """R9 F1: stem 충돌 시 최종 MD 파일명 — <stem>__<sha8(src_rel)>.md.

    src_rel 기반 sha8 이라 결정적: 같은 원본의 재변환은 항상 같은 이름으로
    수렴한다(자기 갱신 안정). 스테이징 sha8(staged_name)과 동일 해시 규칙."""
    return f"{fpath.stem}__{_sha8(fpath.relative_to(base_path))}.md"


def _md_owner(entries: list, md_rel: str) -> str | None:
    """R9 F1: sources.yaml 에서 md_rel 산출물을 소유한 원본(src_rel)을 찾는다."""
    for e in entries or []:
        if e.get("md") == md_rel:
            return e.get("src")
    return None


# ── 소스코드 래핑 수집 헬퍼 ───────────────────────────────────────────────────
def _is_excluded_code_path(fpath: Path, scan_dir: Path) -> bool:
    """fpath 경로상에 제외 디렉토리(.venv·node_modules 등)가 포함되는지 판정.

    scan_dir 자신은 검사 대상에서 제외하고, scan_dir 이하의 상대 경로 부분에서만
    디렉토리명을 대조한다 (상위 경로에 우연히 같은 이름이 있어도 무시).
    """
    try:
        rel = fpath.relative_to(scan_dir)
    except ValueError:
        rel = fpath
    # 마지막 요소(파일명)를 제외한 디렉토리 부분만 검사
    for part in rel.parts[:-1]:
        if part in CODE_EXCLUDE_DIRS:
            return True
    # 파일명 기반 제외: lock/생성/미니파이 파일
    name = fpath.name
    if name in CODE_EXCLUDE_FILENAMES:
        return True
    if name.endswith(CODE_EXCLUDE_SUFFIXES):
        return True
    return False


def code_collect_final_name(fpath: Path) -> str:
    """소스코드 수집 후 최종 .md 파일명 — 원본 파일명 보존(확장자 포함) + .md.

    예: main.cpp → main.cpp.md, config.ini → config.ini.md
    (PDF 경로의 final_md_name 은 stem 기반이지만, 소스코드는 확장자 충돌
     방지 + 원본 식별성을 위해 전체 파일명 + .md 를 사용한다.)
    """
    return fpath.name + ".md"


def build_code_md(fpath: Path, src_rel: str, project_name: str,
                  language: str) -> str:
    """raw 소스코드를 codesign-rag SourceCodeChunker 계약 형식의 .md 로 래핑.

    계약(반드시 준수):
      - frontmatter `language:` 필드
      - 첫 ```<언어> 펜스 안에 raw 코드 전체
    이 형식이 깨지면 청킹이 실패한다.
    """
    # raw 코드 읽기 (인코딩 견고성: utf-8 우선, 실패 시 대체 문자 치환)
    try:
        code = fpath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        code = fpath.read_text(encoding="utf-8", errors="replace")

    # 코드 본문에 ``` 펜스가 있으면 닫는 펜스 길이를 늘려 충돌 방지
    fence = "```"
    while fence in code:
        fence += "`"

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "---",
        "type: source_code",
        f"project: {project_name}",
        f"source: {src_rel}",
        f"language: {language}",
        f"last_collected: {now_iso}",
        "---",
        "",
        f"# {fpath.name}",
        "",
        f"경로: `{src_rel}`",
        "",
        f"{fence}{language}",
        code.rstrip("\n"),
        fence,
        "",
    ]
    return "\n".join(lines)


def collect_code_file(fpath: Path, src_rel: str, domain: str,
                      base_path: Path, project_name: str,
                      dry_run: bool) -> str | None:
    """단일 소스코드 파일을 래핑해 processed_md/source_code/<원본구조>/ 에 출력.

    원본 디렉토리 구조를 보존한다.
    예: 02_Software/apps/prototype/firmware/main.cpp
        → 04_RAG/processed_md/source_code/apps/prototype/firmware/main.cpp.md

    Returns: 생성된 md 의 base_path 기준 상대경로 (없으면 None)
    """
    ext = fpath.suffix.lower()
    language = CODE_EXT_TO_LANG.get(ext)
    if language is None:
        return None

    # 원본 구조 보존: 02_Software 등 도메인 루트 폴더 이하의 상대 경로를 사용
    # FOLDER_DOMAIN_MAP 의 source_code 매핑 폴더(예: 02_Software)를 기준으로 자른다
    rel_under_root = _code_rel_under_root(fpath, base_path)

    final_name = code_collect_final_name(fpath)
    dest_dir = (base_path / "04_RAG" / "processed_md" / domain
                / rel_under_root.parent)
    dest = dest_dir / final_name
    dest_rel = dest.relative_to(base_path).as_posix()

    if dry_run:
        print(f"  [dry-run] 코드수집: {src_rel} → {dest_rel}")
        return dest_rel

    content = build_code_md(fpath, src_rel, project_name, language)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    print(f"  [코드수집] {src_rel} → {dest_rel}")
    return dest_rel


def _code_rel_under_root(fpath: Path, base_path: Path) -> Path:
    """소스코드 파일의 출력 구조용 상대 경로 계산.

    source_code 도메인 루트 폴더(02_Software 등)를 기준으로 그 이하 경로를 반환.
    매칭되는 source_code 폴더가 없으면 base_path 기준 전체 상대 경로 사용.
    """
    src_rel = fpath.relative_to(base_path)
    for folder_prefix, domain, _ in FOLDER_DOMAIN_MAP:
        if domain != CODE_COLLECT_DOMAIN:
            continue
        root = Path(folder_prefix)
        # src_rel 이 이 루트 폴더 이하인지 확인
        try:
            return src_rel.relative_to(root)
        except ValueError:
            continue
    return src_rel


# ── 단계 ①: 스캔 ─────────────────────────────────────────────────────────────
def scan_project(base_path: Path, domain_filter: str | None,
                 entries: list, force: bool) -> tuple[list, list, list, list]:
    """01_Hardware·02_Software·03_DOC 에서 변환/수집 대상 파일 수집.

    Returns:
        (to_convert, native_md_list, skip_list, to_collect_code)
        - to_convert     : 변환이 필요한 (Path, domain, type) 튜플 목록
        - native_md_list : 이미 MD/텍스트인 (Path, domain) 튜플 목록
        - skip_list      : 지문 일치로 건너뛸 (Path, domain) 튜플 목록
        - to_collect_code: 래핑 수집할 소스코드 (Path, domain, language) 튜플 목록
    """
    scan_dirs = []
    for folder_prefix, domain, _ in FOLDER_DOMAIN_MAP:
        d = base_path / folder_prefix
        if d.exists():
            scan_dirs.append((d, domain))

    # 중복 디렉토리 제거 (같은 경로가 여러 규칙에 매핑될 경우 대비)
    seen_dirs: set[Path] = set()
    unique_scan_dirs = []
    for d, dom in scan_dirs:
        if d not in seen_dirs:
            seen_dirs.add(d)
            unique_scan_dirs.append((d, dom))

    to_convert: list[tuple[Path, str, str]] = []
    native_md_list: list[tuple[Path, str]] = []
    skip_list: list[tuple[Path, str]] = []
    to_collect_code: list[tuple[Path, str, str]] = []

    for scan_dir, domain in unique_scan_dirs:
        if domain_filter and domain != domain_filter:
            continue
        all_files = sorted(scan_dir.rglob("*"))
        for fpath in all_files:
            if not fpath.is_file():
                continue
            ext = fpath.suffix.lower()
            src_rel = fpath.relative_to(base_path).as_posix()

            # 이미 MD/텍스트 원본 → native_md
            if ext in NATIVE_MD_EXTS:
                native_md_list.append((fpath, domain))
                continue

            # W-1: 미지원 이미지 포맷 — silent drop 금지, 경고 후 제외
            if ext in IMAGE_EXTS_UNSUPPORTED:
                print(f"  [경고] 미지원 이미지 포맷({ext}) — 수동 변환 필요: {src_rel}")
                continue

            # ── 소스코드 래핑 수집 분기 (차단점 우회) ─────────────────────
            # source_code 도메인이고 코드/설정 확장자면 "code_collect" 경로로.
            # PDF 변환을 거치지 않고 raw 텍스트를 .md 로 감싸 수집한다.
            if domain == CODE_COLLECT_DOMAIN and ext in CODE_EXT_TO_LANG:
                # 제외 디렉토리(.venv·node_modules·build 등) 건너뜀
                if _is_excluded_code_path(fpath, scan_dir):
                    continue
                # 숨김 점파일(.bkit-memory.json·.pdca-status.json 등 도구 산출물)
                # 은 소스코드가 아니므로 제외 (정상 config.json 등은 유지)
                if fpath.name.startswith("."):
                    continue
                language = CODE_EXT_TO_LANG[ext]
                # 증분 판정: sha256 대조 ("collected" 상태 + 지문 일치 시 skip)
                if not force:
                    entry = find_entry(entries, src_rel)
                    if entry and entry.get("status") == "collected":
                        current_sha = sha256_file(fpath)
                        if entry.get("sha256") == current_sha:
                            skip_list.append((fpath, domain))
                            continue
                    # status="failed" 인 파일은 재시도(건너뜀 없음)
                to_collect_code.append((fpath, domain, language))
                continue

            # 변환 가능 파일 여부 확인
            all_image_exts = IMAGE_EXTS_SUPPORTED | IMAGE_EXTS_UNSUPPORTED
            if ext not in CONVERTIBLE_EXTS and ext not in all_image_exts:
                continue  # 알 수 없는 확장자 조용히 무시

            fmdw_type = EXT_TO_TYPE.get(ext)
            if fmdw_type is None:
                continue

            # 증분 판정: sha256 대조 (failed 상태는 재시도 대상 — I-1)
            if not force:
                entry = find_entry(entries, src_rel)
                if entry and entry.get("status") == "converted":
                    current_sha = sha256_file(fpath)
                    if entry.get("sha256") == current_sha:
                        skip_list.append((fpath, domain))
                        continue
                # status="failed" 인 파일은 재시도(건너뜀 없음)

            to_convert.append((fpath, domain, fmdw_type))

    return to_convert, native_md_list, skip_list, to_collect_code


# ── 단계 ②: 스테이징 ─────────────────────────────────────────────────────────
def stage_files(
    to_convert: list,
    work_dir: Path,
    base_path: Path,
    dry_run: bool,
) -> dict[str, tuple[str, str, str]]:
    """변환 대상 파일을 .convert_work/input/<type>/ 에 고유 이름으로 복사.

    C-2: 동일 stem 충돌 방지를 위해 staged_name() 으로 고유 파일명 생성.

    Returns:
        {src_rel: (fmdw_type, staged_fname, staged_md_fname)}
        staged_md_fname = 도구가 출력할 예상 MD 파일명
    """
    staging_map: dict[str, tuple[str, str, str]] = {}
    for fpath, domain, fmdw_type in to_convert:
        src_rel = fpath.relative_to(base_path).as_posix()
        s_name = staged_name(fpath, base_path)
        s_md = staged_md_name(s_name)
        dest_dir = work_dir / "input" / fmdw_type
        dest = dest_dir / s_name
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fpath, dest)
        staging_map[src_rel] = (fmdw_type, s_name, s_md)
    return staging_map


# ── 단계 ③: 실행 ─────────────────────────────────────────────────────────────
def run_fmdw(work_dir: Path, use_figures: bool, dry_run: bool,
             extract_domain: str | None = None,
             ensemble: bool = False,
             remove_watermark: bool = False) -> bool:
    """cwd=.convert_work 에서 extract_all_via_pdf.py 실행.

    Args:
        extract_domain: 이번 실행의 도메인 힌트(datasheet/schematic/...). 주어지면
            subprocess env EXTRACT_DOMAIN 으로 주입되어, fmdw 의 도메인→모델 라우팅
            (config.knob_model_for_domain)이 본문 전사 vision 모델을 자동 선택한다.
            None/빈값이면 미주입 → 도메인 라우팅 미적용(기존 동작 100% 보존).
            ※ 모델 선택은 fmdw 모듈 import 시점 스냅샷이라 env 로만 전달 가능하다
              (subprocess 경계). 그래서 도메인별로 분리 실행한다.
        ensemble: True 면 subprocess env 에 EXTRACT_ENSEMBLE=1 을 주입하여, 본문
            전사(role='structure')를 N개 vision 모델 병렬 추출 후 1개 merger 모델로
            병합·교차검증한다(비용 ~4배, 중요 문서 opt-in). False(기본)면 미주입 →
            앙상블 비활성, 기존 단일 모델 동작과 동일(회귀 0). 앙상블 모델/merger 는
            fmdw 측 env(FMDW_ENSEMBLE_MODELS/FMDW_ENSEMBLE_MERGER)·config 로 결정되며,
            상위 셸에서 export 한 그 변수들은 env 복사(os.environ.copy)로 전파된다.

    Returns: True(성공) / False(실패)
    """
    if dry_run:
        dom_note = f" EXTRACT_DOMAIN={extract_domain}" if extract_domain else ""
        ens_note = " EXTRACT_ENSEMBLE=1" if ensemble else ""
        wm_note = " EXTRACT_REMOVE_WATERMARK=1" if remove_watermark else ""
        print(f"  [dry-run] subprocess:{dom_note}{ens_note}{wm_note} python {FMDW_MAIN} (cwd={work_dir})")
        return True

    env = os.environ.copy()
    if use_figures:
        env["EXTRACT_FIGURES"] = "1"
    # 도메인→모델 라우팅: subprocess 가 import 시 EXTRACT_DOMAIN 을 읽어 모델 선택.
    if extract_domain:
        env["EXTRACT_DOMAIN"] = extract_domain
    # 앙상블(opt-in): subprocess 가 import 시 EXTRACT_ENSEMBLE 을 읽어 본문 전사를 앙상블화.
    if ensemble:
        env["EXTRACT_ENSEMBLE"] = "1"
    # 워터마크 제거(opt-in): subprocess(렌더)가 EXTRACT_REMOVE_WATERMARK 을 읽어
    # 추적형 워터마크를 제거한 임시 PDF에서 렌더한다. 미주입(기본)이면 원본 그대로.
    if remove_watermark:
        env["EXTRACT_REMOVE_WATERMARK"] = "1"

    result = subprocess.run(
        [sys.executable, str(FMDW_MAIN)],
        cwd=str(work_dir),
        env=env,
    )
    return result.returncode == 0


# ── 머리말(front-matter) source 주입 (W-3) ───────────────────────────────────
def inject_source_frontmatter(md_path: Path, src_rel: str) -> None:
    """MD 파일 머리말에 source: <원본 경로> 를 주입 (이중 추적).

    W-3 엄격화:
    - 선행 BOM(Byte Order Mark, UTF-8 BOM ﻿) 제거 후 판정
    - front-matter 인정 조건: 파일이 정확히 `---` 만 있는 줄로 시작 (줄 단위 판정)
    - 종료 `---` 도 자기 줄 기준으로만 탐색 (본문 중간 수평선 오인 금지)
    - 위 조건 불충족 시 새 front-matter 블록을 안전하게 앞에 추가
    - 기존 source: 가 있으면 교체, 없으면 추가
    """
    raw = md_path.read_bytes()
    # BOM(Byte Order Mark) strip
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    content = raw.decode("utf-8")

    source_line = f"source: {src_rel}"

    lines = content.splitlines(keepends=True)

    # front-matter 인정: 첫 줄이 정확히 "---\n" 또는 "---" (EOF)
    has_fm_open = lines and lines[0].rstrip("\r\n") == "---"

    if has_fm_open:
        # 종료 `---` 탐색: 1번 인덱스(두 번째 줄)부터 자기 줄 기준
        close_idx = None
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r\n") == "---":
                close_idx = i
                break
        if close_idx is not None:
            # 정상 front-matter — source: 교체/추가
            fm_lines = lines[1:close_idx]
            new_fm = [l for l in fm_lines if not l.startswith("source:")]
            new_fm.insert(0, source_line + "\n")
            new_content = (
                "---\n"
                + "".join(new_fm)
                + "---\n"
                + "".join(lines[close_idx + 1 :])
            )
            md_path.write_text(new_content, encoding="utf-8")
            return
        # 종료 --- 없음 → 기존 front-matter 불완전 → 안전하게 앞에 추가

    # front-matter 없음 또는 불완전 — 새 블록을 맨 앞에 추가
    new_content = f"---\n{source_line}\n---\n" + content
    md_path.write_text(new_content, encoding="utf-8")


# ── 단계 ④: 수집 ─────────────────────────────────────────────────────────────
def collect_outputs(
    to_convert: list,
    staging_map: dict,
    work_dir: Path,
    base_path: Path,
    dry_run: bool,
    entries: list | None = None,
    claimed: dict | None = None,
    collisions: list | None = None,
) -> dict[str, str | None]:
    """output/<type>_md/*.md → 04_RAG/processed_md/<domain>/ 이동.

    C-2: staging_map 의 staged_md_fname 으로 출력 파일을 찾고,
    수집 후 최종 이름은 원본 stem 기반(final_md_name)으로 복원한다.

    R9 F1(QA C2/C3, stem 충돌 가드): 서로 다른 원본이 같은 도메인·같은 stem 으로
    수렴하면(FOLDER_DOMAIN_MAP 다대일 매핑) 뒤에 수집되는 쪽이 조용히 덮어써 문서가
    영구 유실됐다. 소유자 대조 = 이번 실행 claimed(dest_rel→src_rel) 우선, 다음
    sources.yaml md 소유 엔트리(_md_owner) — 로 '같은 원본의 재변환 갱신'(정상
    덮어쓰기, 기존 동작 유지)과 '타 원본 충돌'을 구분해, 충돌일 때만
    unique_final_md_name 으로 유니크화한다. figures JSON 사이드카도 동일 stem 동반.
    충돌 내역은 collisions 리스트에 기록(main 이 sources.yaml 메타로 저장).

    Returns: {src_rel: output_md_rel | None}
    """
    results: dict[str, str | None] = {}
    claimed = claimed if claimed is not None else {}

    for fpath, domain, fmdw_type in to_convert:
        src_rel = fpath.relative_to(base_path).as_posix()
        output_subdir = TYPE_OUTPUT_DIR.get(fmdw_type)
        if not output_subdir:
            results[src_rel] = None
            continue

        # C-2: staging_map 에서 예상 출력 MD 파일명을 가져옴
        _type, s_name, s_md = staging_map[src_rel]
        expected_md = work_dir / output_subdir / s_md
        final_name = final_md_name(fpath)          # 원본 stem 복원 이름
        dest_domain_dir = base_path / "04_RAG" / "processed_md" / domain

        # ── R9 F1: 목적지 소유자 대조 — 타 원본 유래 충돌이면 유니크화 ──
        dest_rel_plan = f"04_RAG/processed_md/{domain}/{final_name}"
        owner = claimed.get(dest_rel_plan) or _md_owner(entries or [], dest_rel_plan)
        if owner is None:
            # R9b(Advisor): 대소문자 무관 FS(macOS APFS 기본)에서 stem 이 대소문자만
            # 다른 두 원본은 dest_rel_plan 키가 갈려 정확 일치 조회를 우회한다 —
            # 이번 실행 claimed 를 casefold 로 재조회(배치 내 케이스 변형 검출).
            cf = dest_rel_plan.casefold()
            owner = next((v for k, v in claimed.items() if k.casefold() == cf), None)
        collide_with = None
        if owner is not None and owner != src_rel:
            collide_with = owner
        elif owner is None and (dest_domain_dir / final_name).exists():
            # R9b(Advisor): 소유자 미상인데 목적지에 실존 파일 → '미상 소유자 충돌'로
            # 안전측 유니크화. 커버 시나리오 = ①sources.yaml 소실 후 고아 파일 +
            # 같은 stem 타 원본 변환 ②케이스 무관 FS 의 대소문자 변형(exists() 가
            # FS 대소문자 의미론을 그대로 따르므로 별도 분기 불필요). 자기 재변환
            # 갱신은 위 owner==src_rel 경로(yaml 정상 시)가 덮어쓰기로 처리하므로
            # 여기 도달 = 소유자 미상 상황뿐이며, yaml 소실 후 자기 재실행의 최악은
            # 중복 1건 생성(유실 0 안전측 — Advisor 허용).
            try:
                actual = next((f.name for f in dest_domain_dir.iterdir()
                               if f.name.casefold() == final_name.casefold()),
                              final_name)
            except OSError:
                actual = final_name
            collide_with = f"(unknown: on-disk '{actual}')"
        if collide_with is not None:
            uniq = unique_final_md_name(fpath, base_path)
            print(f"  [WARN] stem collision: '{final_name}'({domain}) 은 "
                  f"'{collide_with}' 소유 → '{src_rel}' 는 '{uniq}' 로 저장")
            if collisions is not None:
                collisions.append({
                    "src": src_rel,
                    "collided_with": collide_with,
                    "original_name": final_name,
                    "renamed_to": uniq,
                    "domain": domain,
                })
            final_name = uniq
            dest_rel_plan = f"04_RAG/processed_md/{domain}/{final_name}"

        if dry_run:
            if expected_md.exists():
                print(f"  [dry-run] 수집: {s_md} → {dest_rel_plan}")
                results[src_rel] = dest_rel_plan
                claimed[dest_rel_plan] = src_rel   # R9 F1: 배치 내 후속 충돌 검출
            else:
                print(f"  [dry-run] 출력 없음(예정): {expected_md}")
                results[src_rel] = None
            continue

        if expected_md.exists():
            dest_domain_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_domain_dir / final_name
            shutil.move(str(expected_md), str(dest))
            inject_source_frontmatter(dest, src_rel)
            dest_rel = dest.relative_to(base_path).as_posix()
            results[src_rel] = dest_rel
            claimed[dest_rel] = src_rel            # R9 F1: 배치 내 후속 충돌 검출
            print(f"  [수집] {fpath.name} → {dest_rel}")

            # figures 사이드카 이동 (있으면)
            figures_src = work_dir / output_subdir / "figures"
            if figures_src.exists():
                figures_dest = dest_domain_dir / "figures"
                if not figures_dest.exists():
                    shutil.move(str(figures_src), str(figures_dest))
                else:
                    for fig_file in figures_src.iterdir():
                        shutil.move(str(fig_file), str(figures_dest / fig_file.name))

            # figures JSON 사이드카 (스테이징 이름 stem 기준으로 생성됨)
            # R9 F1: 목적지 사이드카 이름은 최종 MD stem 을 따른다(충돌 유니크화 동반).
            json_sidecar = work_dir / output_subdir / (Path(s_name).stem + "_figures.json")
            if json_sidecar.exists():
                shutil.move(str(json_sidecar),
                            str(dest_domain_dir / (Path(final_name).stem + "_figures.json")))
        else:
            print(f"  [경고] 출력 MD 없음: {expected_md} (변환 실패 가능)")
            results[src_rel] = None

    return results


# ── 단계 ⑤: sources.yaml 갱신 ────────────────────────────────────────────────
def update_sources(
    data: dict,
    to_convert: list,
    native_md_list: list,
    skip_list: list,
    collect_results: dict,
    base_path: Path,
    to_collect_code: list | None = None,
    code_results: dict | None = None,
) -> dict:
    """sources.yaml entries 를 갱신.

    상태 어휘 (I-1):
      converted  정상 변환 완료
      collected  소스코드 래핑 수집 완료 (PDF 변환 아님)
      failed     변환/수집 실행 후 출력 MD 없음 (재실행 시 재시도)
      native_md  이미 MD 원본
    """
    entries: list[dict] = data.get("entries") or []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def upsert(src_rel: str, entry_data: dict) -> None:
        for i, e in enumerate(entries):
            if e.get("src") == src_rel:
                entries[i] = entry_data
                return
        entries.append(entry_data)

    # R10 M6(QA): 변환 완료 후 sha256 재계산 시점에 원본이 사라져 있으면(NAS 언마운트·
    # 폴더 정리 등 — 스테이징 copy2 와 재계산 사이 시간 창 실재) FileNotFoundError 가
    # main() 까지 전파돼 save_sources 미도달 → 이미 성공한 다른 파일들의 기록까지
    # 함께 유실됐다. 파일별 가드로 해당 엔트리만 sha=None + missing_source 마킹하고
    # 배치는 계속한다(기록 보존).
    def _safe_sha(fpath: Path, src_rel: str):
        try:
            return sha256_file(fpath), False
        except OSError as e:
            print(f"  [WARN] 원본 소실 — sha256 재계산 불가: {src_rel} ({e}) → "
                  "missing_source 마킹 후 계속")
            return None, True

    # 변환된(또는 실패한) 파일
    for fpath, domain, fmdw_type in to_convert:
        src_rel = fpath.relative_to(base_path).as_posix()
        md_rel = collect_results.get(src_rel)
        sha, missing_src = _safe_sha(fpath, src_rel)   # R10 M6
        # I-1: 출력 MD 없으면 "failed" (stale은 원본 변경 의미로 구분)
        status = "converted" if md_rel else "failed"
        entry = {
            "src": src_rel,
            "domain": domain,
            "md": md_rel,
            "sha256": sha,
            "status": status,
            "converted_at": now_str,
        }
        if missing_src:
            entry["missing_source"] = True
        upsert(src_rel, entry)

    # native_md 파일
    for fpath, domain in native_md_list:
        src_rel = fpath.relative_to(base_path).as_posix()
        upsert(src_rel, {
            "src": src_rel,
            "domain": domain,
            "md": None,
            "sha256": None,
            "status": "native_md",
            "converted_at": None,
        })

    # 소스코드 래핑 수집 파일
    code_results = code_results or {}
    for fpath, domain, language in (to_collect_code or []):
        src_rel = fpath.relative_to(base_path).as_posix()
        md_rel = code_results.get(src_rel)
        sha, missing_src = _safe_sha(fpath, src_rel)   # R10 M6: 동일 가드
        status = "collected" if md_rel else "failed"
        entry = {
            "src": src_rel,
            "domain": domain,
            "md": md_rel,
            "sha256": sha,
            "status": status,
            "language": language,
            "converted_at": now_str,
        }
        if missing_src:
            entry["missing_source"] = True
        upsert(src_rel, entry)

    # skip 파일 — 기존 엔트리 유지 (converted_at 갱신 안 함, upsert 생략)

    data["entries"] = entries
    return data


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="filestomdwgem CWD 스테이징 래퍼 (FILESTOMDWGEM-INTEGRATION §2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python convert_project.py --project ~/workspace/02_QnQ/01_sound-cam
  python convert_project.py --project ~/workspace/02_QnQ/01_sound-cam --domain datasheet
  python convert_project.py --project ~/workspace/02_QnQ/01_sound-cam --force   # figures 기본 ON
  python convert_project.py --project ~/workspace/02_QnQ/01_sound-cam --no-figures  # figures 끔
  python convert_project.py --project ~/workspace/02_QnQ/01_sound-cam --dry-run
        """,
    )
    parser.add_argument("--project", required=True,
                        help="프로젝트 루트 절대 경로")
    parser.add_argument("--domain",  default=None,
                        help="특정 도메인만 처리 (datasheet|schematic|source_code|design_doc|reference)")
    parser.add_argument("--force",   action="store_true",
                        help="sha256 지문 무시, 전체 재변환")
    # figure 크롭 PNG 별도 저장 — 기본 ON(2026-06-30, 사용자 결정). 끄려면 --no-figures.
    # 기존 --figures 입력도 그대로 동작(하위호환): default=True 위에 store_true 라 무해.
    parser.add_argument("--figures", dest="figures", action="store_true", default=True,
                        help="figure 크롭 PNG 별도 저장 (기본 ON, EXTRACT_FIGURES=1)")
    parser.add_argument("--no-figures", dest="figures", action="store_false",
                        help="figure 크롭 비활성화 (이미지 별도 저장 끔)")
    parser.add_argument("--ensemble", action="store_true",
                        help="EXTRACT_ENSEMBLE=1 (본문 전사를 N개 vision 모델 병렬 추출"
                             " 후 merger 모델로 병합·교차검증; 비용 ~4배, 중요 문서 opt-in)")
    parser.add_argument("--remove-watermark", dest="remove_watermark",
                        action="store_true",
                        help="EXTRACT_REMOVE_WATERMARK=1 (PDF 추적형 워터마크[예: "
                             "'Samsung Confidential' 회전 스탬프]를 렌더 전 무손실 제거; "
                             "기본 OFF, 켤 때만 동작 → 기존 변환 동작 그대로 보존)")
    parser.add_argument("--dry-run", action="store_true",
                        help="파일 변경 없이 계획만 출력")
    args = parser.parse_args()

    base_path = Path(args.project).expanduser().resolve()
    dry_run: bool = args.dry_run

    print("=" * 64)
    print("  filestomdwgem CWD 스테이징 래퍼 (INTEGRATION §2 Option A)")
    print("=" * 64)
    print(f"  project : {base_path}")
    print(f"  domain  : {args.domain or '전체'}")
    print(f"  force   : {args.force}")
    print(f"  figures : {args.figures}")
    print(f"  ensemble: {args.ensemble}")
    print(f"  remove_watermark: {args.remove_watermark}")
    print(f"  dry-run : {dry_run}")
    print("=" * 64)

    if not base_path.exists():
        print(f"[!] 프로젝트 경로가 존재하지 않습니다: {base_path}", file=sys.stderr)
        sys.exit(1)

    if not FMDW_MAIN.exists():
        print(f"[!] extract_all_via_pdf.py 를 찾을 수 없습니다: {FMDW_MAIN}", file=sys.stderr)
        sys.exit(1)

    sources_yaml = base_path / "04_RAG" / "sources.yaml"
    work_dir = base_path / "04_RAG" / ".convert_work"

    # ── R10 M7(QA): 프로젝트 단위 동시 실행 락 ────────────────────────────────
    # work_dir 이 base_path 기준 고정 경로 + 무락이라, 두 인스턴스가 동시 기동하면
    # (도메인별 nohup 병렬 호출·스케줄+수동 재실행) 한쪽의 rmtree 가 다른 쪽의
    # 스테이징 복사/run_fmdw 쓰기와 충돌 → 파일 소실·오염 변환이 가능했다.
    # flock(EX|NB) 락파일로 제2 인스턴스를 명확한 에러로 조기 종료시킨다(제1
    # 인스턴스 무영향). 락은 프로세스 종료 시 OS 가 자동 해제(크래시 포함)라
    # stale lock 문제 없음. dry-run 도 락 대상(실행 중 계획 출력이 반쯤 정리된
    # work_dir 를 읽는 혼선 방지).
    # 한계(R10b 명기): flock 은 '로컬 FS' 전제 — NFS 등 네트워크/원격 FS 의
    # base_path 에서는 잠금 의미론이 보장되지 않을 수 있다(현 운영 = 로컬 APFS).
    # 부수효과: R9b collect_outputs 의 잔여 TOCTOU(소유자 대조 시점과 shutil.move
    # 사이 타 인스턴스 개입 창)도 이 락으로 자연 해소된다 — 프로젝트당 단일 실행
    # 이 보장되므로 대조·이동 사이에 끼어들 두 번째 쓰기 주체가 없다.
    import fcntl
    lock_path = base_path / "04_RAG" / ".convert.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    global _LOCK_FH
    _LOCK_FH = open(lock_path, "w")
    try:
        fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"[!] 이미 다른 convert_project.py 인스턴스가 이 프로젝트를 처리 중"
              f"입니다 (lock: {lock_path}).\n"
              "    동시 실행은 스테이징(.convert_work)을 파괴하므로 중단합니다. "
              "기존 실행 종료 후 재시도하세요.", file=sys.stderr)
        sys.exit(3)
    _LOCK_FH.write(f"{os.getpid()}\n")
    _LOCK_FH.flush()

    # ── ① 스캔 ────────────────────────────────────────────────────────────────
    print("\n[①] 스캔 중...")
    data = load_sources(sources_yaml)
    entries = data.get("entries") or []

    to_convert, native_md_list, skip_list, to_collect_code = scan_project(
        base_path, args.domain, entries, args.force
    )

    project_name = base_path.name  # 소스코드 frontmatter project: 값

    print(f"  변환 대상   : {len(to_convert)}개")
    print(f"  코드 수집   : {len(to_collect_code)}개 (래핑)")
    print(f"  native_md   : {len(native_md_list)}개 (변환 불필요)")
    print(f"  건너뜀(skip): {len(skip_list)}개 (지문 동일)")

    for fpath, domain, fmdw_type in to_convert:
        src_rel = fpath.relative_to(base_path).as_posix()
        print(f"    -> [{domain}/{fmdw_type}] {src_rel}")

    for fpath, domain, language in to_collect_code:
        src_rel = fpath.relative_to(base_path).as_posix()
        print(f"    -> [code/{language}] {src_rel}")

    for fpath, domain in native_md_list:
        src_rel = fpath.relative_to(base_path).as_posix()
        print(f"    -> [native_md/{domain}] {src_rel}")

    # ── 소스코드 래핑 수집 (PDF 변환과 독립적으로 항상 수행) ──────────────────
    code_results: dict[str, str | None] = {}
    if to_collect_code:
        print("\n[코드 수집] raw 소스코드 → .md 래핑 수집 중...")
        for fpath, domain, language in to_collect_code:
            src_rel = fpath.relative_to(base_path).as_posix()
            md_rel = collect_code_file(
                fpath, src_rel, domain, base_path, project_name, dry_run
            )
            code_results[src_rel] = md_rel
        code_ok = sum(1 for v in code_results.values() if v is not None)
        print(f"  코드 수집 성공: {code_ok}개 / 실패: {len(code_results) - code_ok}개")

    if dry_run and not to_convert:
        # 코드 수집 계획은 위에서 이미 출력됨
        if not to_collect_code:
            print("\n[dry-run] 변환/수집 대상 없음. 종료.")
        else:
            print("\n[dry-run] 변환 대상 없음(코드 수집 계획만 출력). 종료.")
        return

    # 변환 대상이 없으면 native_md + 코드 수집만 갱신하고 종료
    if not to_convert:
        print("\nPDF 변환 대상 파일 없음. sources.yaml 만 갱신합니다.")
        data = update_sources(data, [], native_md_list, skip_list, {}, base_path,
                              to_collect_code=to_collect_code,
                              code_results=code_results)
        save_sources(sources_yaml, data, dry_run)
        print("[완료] PDF 변환 대상 없음.")
        return

    # ── ②③④ 도메인별 스테이징 → 실행 → 수집 ─────────────────────────────────
    # 도메인→모델 라우팅을 위해 *도메인 단위로 분리 실행*한다. fmdw 의 모델 선택은
    # 모듈 import 시점 스냅샷이라(subprocess 경계) EXTRACT_DOMAIN env 로만 전달되며,
    # 한 subprocess 는 input/ 전체를 한 모델로 처리한다 → 도메인이 섞이면 모델이
    # 잘못 적용된다. 따라서 to_convert 를 도메인별 그룹으로 나눠 각 그룹마다
    # 스테이징→실행(EXTRACT_DOMAIN 주입)→수집 사이클을 돈다.
    #
    # 회귀 안전성:
    #   - 단일 도메인(흔한 --domain 케이스)이면 그룹이 1개 → 기존 1회 실행과 동일
    #     (추가로 EXTRACT_DOMAIN 만 주입; 매핑이 폴백이면 모델도 동일 → byte-identical).
    #   - 도메인 정렬 순서로 결정적(deterministic) 실행 순서 보장.
    from collections import OrderedDict
    domain_groups: "OrderedDict[str, list]" = OrderedDict()
    for item in to_convert:
        domain_groups.setdefault(item[1], []).append(item)

    collect_results: dict[str, str | None] = {}
    collect_claimed: dict[str, str] = {}    # R9 F1: dest_rel → src_rel (이번 실행 전체)
    collect_collisions: list[dict] = []     # R9 F1: 충돌 기록(sources.yaml 메타 저장용)
    n_groups = len(domain_groups)
    for gi, (group_domain, group_items) in enumerate(sorted(domain_groups.items()), start=1):
        print(f"\n[②③④] 도메인 '{group_domain}' 처리 ({gi}/{n_groups}, "
              f"{len(group_items)}개 파일, 모델 라우팅 EXTRACT_DOMAIN={group_domain})")

        # ── ② 스테이징 (그룹마다 work_dir 재초기화) ─────────────────────────────
        print("  [②] 스테이징 중...")
        if not dry_run:
            # 이전 그룹/이전 실행 산출물 정리 후 새로 시작.
            if work_dir.exists():
                shutil.rmtree(work_dir)
            work_dir.mkdir(parents=True, exist_ok=True)

        staging_map = stage_files(group_items, work_dir, base_path, dry_run)
        type_counts: dict[str, int] = {}
        for _type, s_name, s_md in staging_map.values():
            type_counts[_type] = type_counts.get(_type, 0) + 1
        for fmdw_type, cnt in type_counts.items():
            print(f"    staged [{fmdw_type}]: {cnt}개")

        # ── ③ 실행 (EXTRACT_DOMAIN 주입) ────────────────────────────────────────
        print("  [③] filestomdwgem 실행 중...")
        if not dry_run:
            success = run_fmdw(work_dir, args.figures, dry_run,
                               extract_domain=group_domain,
                               ensemble=args.ensemble,
                               remove_watermark=args.remove_watermark)
            if not success:
                print("[!] extract_all_via_pdf.py 가 실패했습니다.", file=sys.stderr)
                print(f"    작업 디렉토리 보존(다음 실행 시 자동 정리): {work_dir}",
                      file=sys.stderr)
                sys.exit(1)
        else:
            run_fmdw(work_dir, args.figures, dry_run, extract_domain=group_domain,
                     ensemble=args.ensemble,
                     remove_watermark=args.remove_watermark)

        # ── ④ 수집 ──────────────────────────────────────────────────────────────
        print("  [④] 출력 수집 중...")
        group_results = collect_outputs(group_items, staging_map, work_dir,
                                        base_path, dry_run,
                                        entries=entries,
                                        claimed=collect_claimed,
                                        collisions=collect_collisions)
        collect_results.update(group_results)

    converted_ok = sum(1 for v in collect_results.values() if v is not None)
    converted_fail = sum(1 for v in collect_results.values() if v is None)
    print(f"\n  성공: {converted_ok}개 / 실패: {converted_fail}개")

    # ── ⑤ sources.yaml 갱신 + .convert_work 정리 ─────────────────────────────
    print("\n[⑤] sources.yaml 갱신 및 정리 중...")
    data = update_sources(data, to_convert, native_md_list, skip_list,
                          collect_results, base_path,
                          to_collect_code=to_collect_code,
                          code_results=code_results)
    # R9 F1: stem 충돌 기록을 sources.yaml 메타(top-level collisions)에 남긴다.
    # 재실행 시 동일 충돌 중복 기록 방지((src, renamed_to) 기준 dedupe).
    if collect_collisions:
        prev = data.get("collisions") or []
        seen = {(c.get("src"), c.get("renamed_to")) for c in prev}
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for c in collect_collisions:
            if (c["src"], c["renamed_to"]) in seen:
                continue
            c["at"] = now_str
            prev.append(c)
        data["collisions"] = prev
    save_sources(sources_yaml, data, dry_run)

    if not dry_run and work_dir.exists():
        shutil.rmtree(work_dir)
        print("  .convert_work 정리 완료")

    if dry_run:
        print("\n[dry-run] 실제 파일 변경 없이 계획 출력 완료.")
    else:
        print(f"\n  sources.yaml 저장: {sources_yaml}")

    code_ok = sum(1 for v in code_results.values() if v is not None)
    print("\n" + "=" * 64)
    print(f"  완료: 변환 {converted_ok}개 / 실패 {converted_fail}개 / "
          f"코드수집 {code_ok}개 / "
          f"건너뜀 {len(skip_list)}개 / native_md {len(native_md_list)}개")
    print("=" * 64)


if __name__ == "__main__":
    main()
