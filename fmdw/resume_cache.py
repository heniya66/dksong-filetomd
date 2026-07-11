"""filestomdwgem 대용량 PDF resume(중단 후 이어받기) 캐시 — 중장기 업그레이드.

목적:
  100페이지 이상 대용량 PDF 변환 중 타임아웃·중단 시, 처음부터 재시작하지 않고
  이미 추출 완료한 청크는 디스크 캐시에서 로드하고 미완료 청크만 다시 추출한다.

설계 원칙(기존 동작 보존 최우선):
  1. **additive only**: convert_pdf 의 기존 추출/병합/저장 계약을 바꾸지 않는다.
     resume off(EXTRACT_RESUME 비활성) 시 이 모듈은 전혀 개입하지 않으며
     동작은 기존과 100% 동일하다.
  2. **graceful degrade**: 캐시 디렉토리 부재·읽기 실패·manifest 손상·키 불일치 등
     어떤 캐시 이상도 "캐시 없음"으로 간주하고 처음부터 추출한다(예외 전파 금지).
  3. **stale 방지(캐시 무효화 키)**: 캐시 키는
       소스 PDF (경로 + size + mtime_ns)  ← 내용 변경 감지
       + 청크 정책 (chunk_size, single_chunk_max)
       + 프롬프트 템플릿 해시
       + provider 라벨(ollama_cloud/gemini 모델)
     을 모두 포함한다. 무엇이든 바뀌면 캐시 키(디렉토리 해시)가 달라져
     이전 캐시는 자동으로 무시된다.
  4. **per-chunk 단위**: 각 청크 결과(Markdown)를 chunk_<idx>.md 로 저장하고
     manifest.json 에 (chunk index → start/end/저장여부)를 기록한다. 재실행 시
     manifest 의 done 청크만 캐시에서 로드, 나머지는 추출한다.
  5. **시크릿 무관**: 캐시 키/내용에 OLLAMA_API_KEY/GEMINI_API_KEY 등 시크릿을
     절대 포함하지 않는다(provider *라벨* 만 사용 — Keychain SSoT 보존).

캐시 레이아웃:
  <cache_root>/<pdf_stem>__<key_hash>/
      manifest.json          # {schema, key, pdf, chunk_size, single_chunk_max,
                             #  prompt_sha, provider, chunks: {idx: {start,end,file,sha}}}
      chunk_0.md
      chunk_1.md
      ...

  <cache_root> 기본값: <output_path 의 부모>/.resume_cache
  (예: output/pdf_md/foo.md → output/pdf_md/.resume_cache/foo__<hash>/)
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Optional

# manifest 스키마 버전 — 포맷 변경 시 올리면 옛 캐시는 키 불일치로 자동 무시.
_SCHEMA_VERSION = 1

# manifest 파일 이름.
_MANIFEST_NAME = "manifest.json"

# 캐시 루트 디렉토리 이름(output_path 부모 아래).
_CACHE_DIRNAME = ".resume_cache"


# ──────────────────────────────────────────────────────────────────────────────
# 캐시 키 계산
# ──────────────────────────────────────────────────────────────────────────────

def _pdf_fingerprint(pdf_path: Path) -> str:
    """PDF 내용 변경 감지용 지문.

    (절대경로 + 파일 크기 + mtime_ns) 조합. 내용 해시 대신 size+mtime 을 쓰는 이유:
      대용량 PDF(수백 MB) 전체를 매 실행 해싱하면 느리다. size+mtime 은 사실상
      모든 일반적 편집을 감지하며(편집하면 mtime 변동), graceful 무효화로 충분하다.
    stat 실패 시 'no-stat' 토큰 — 이 경우 키가 불안정해져 캐시 미스가 잦아질 뿐
    오작동(stale 로드)은 없다.
    """
    try:
        st = pdf_path.stat()
        return f"{pdf_path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return f"{pdf_path}|no-stat"


def compute_cache_key(
    pdf_path: Path,
    *,
    chunk_size: int,
    single_chunk_max: Optional[int],
    prompt_template: str,
    provider_label: str,
) -> str:
    """캐시 무효화 키(16진 해시) 계산.

    PDF 지문 + 청크 정책 + 프롬프트 해시 + provider 라벨을 결합해 SHA-256.
    어느 하나라도 바뀌면 키가 달라져 옛 캐시를 자동 무시한다.
    """
    h = hashlib.sha256()
    h.update(f"schema={_SCHEMA_VERSION}\n".encode("utf-8"))
    h.update(f"pdf={_pdf_fingerprint(Path(pdf_path))}\n".encode("utf-8"))
    h.update(f"chunk_size={chunk_size}\n".encode("utf-8"))
    h.update(f"single_chunk_max={single_chunk_max}\n".encode("utf-8"))
    # 프롬프트 템플릿 자체를 해시(범위 치환은 청크 index 로 결정되므로 템플릿이면 충분).
    h.update(b"prompt=")
    h.update(prompt_template.encode("utf-8"))
    h.update(b"\n")
    h.update(f"provider={provider_label}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def _sanitize_stem(stem: str) -> str:
    """디렉토리 이름에 안전한 stem(영숫자/._- 만, 그 외 _)."""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in stem)[:80] or "doc"


# ──────────────────────────────────────────────────────────────────────────────
# ResumeCache — per-chunk 디스크 캐시
# ──────────────────────────────────────────────────────────────────────────────

class ResumeCache:
    """단일 PDF 변환에 대한 청크 단위 resume 캐시.

    사용 흐름(convert_pdf 내부):
        cache = ResumeCache.open(pdf_path, output_path, key=..., chunks=...)
        for i, (start, end) in enumerate(chunks):
            hit = cache.load(i)
            if hit is not None:
                text = hit                       # 추출 skip
            else:
                text = ox.extract_pdf_pages(...)
                cache.store(i, start, end, text) # 청크 캐시 기록
        ...
        cache.cleanup()  # 전체 성공 시 (보존 옵션이면 skip)

    어떤 디스크 오류도 삼켜 graceful 하게 동작한다(load→None, store→무시).
    """

    def __init__(self, cache_dir: Path, key: str, manifest: dict):
        self.cache_dir = cache_dir
        self.key = key
        self._manifest = manifest

    # ── 생성/로드 ────────────────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        pdf_path: Path,
        output_path: Path,
        *,
        key: str,
        chunk_size: int,
        single_chunk_max: Optional[int],
        prompt_template: str,
        provider_label: str,
        cache_root: Optional[Path] = None,
    ) -> "ResumeCache":
        """캐시 디렉토리를 열거나(키 일치 manifest 재사용) 새로 만든다.

        키 불일치/manifest 손상/디렉토리 부재 시 → 새 manifest(빈 청크)로 시작.
        어떤 단계 실패도 예외를 던지지 않는다(빈 캐시로 degrade).
        """
        pdf_path = Path(pdf_path)
        output_path = Path(output_path)
        stem = _sanitize_stem(pdf_path.stem)

        if cache_root is None:
            cache_root = output_path.parent / _CACHE_DIRNAME
        cache_dir = Path(cache_root) / f"{stem}__{key}"

        manifest = cls._read_valid_manifest(cache_dir, key)
        if manifest is None:
            manifest = {
                "schema": _SCHEMA_VERSION,
                "key": key,
                "pdf": str(pdf_path),
                "chunk_size": chunk_size,
                "single_chunk_max": single_chunk_max,
                "prompt_sha": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()[:16],
                "provider": provider_label,
                "chunks": {},  # str(idx) -> {start, end, file, sha}
            }
        return cls(cache_dir, key, manifest)

    @staticmethod
    def _read_valid_manifest(cache_dir: Path, key: str) -> Optional[dict]:
        """캐시 디렉토리의 manifest 를 읽어 키 검증. 유효하면 dict, 아니면 None."""
        mpath = cache_dir / _MANIFEST_NAME
        try:
            if not mpath.is_file():
                return None
            data = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # 스키마/키 일치 검증 — 불일치면 stale 로 간주.
        if data.get("schema") != _SCHEMA_VERSION or data.get("key") != key:
            return None
        if not isinstance(data.get("chunks"), dict):
            return None
        return data

    # ── 청크 로드/저장 ──────────────────────────────────────────────────────

    def load(
        self,
        idx: int,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> Optional[str]:
        """청크 idx 가 캐시에 완료 기록되어 있으면 그 Markdown 텍스트, 없으면 None.

        manifest 기록은 있으나 파일이 사라졌거나 sha 불일치면 미스(None) — 재추출 유도.

        belt-and-suspenders(안전망): ``start``/``end`` 를 주면 manifest 에 저장된 청크
        경계와 대조하여 불일치 시 미스(None)로 처리한다. 캐시 키(PDF 지문+청크 정책)가
        이미 동일 경계를 보장하므로 정상 경로에선 항상 일치하지만, 만에 하나라도
        경계가 어긋난 청크가 다른 위치에 병합되는 사고를 원천 차단한다(출력 오염 방지).
        ``None`` 이면 검증을 생략한다(역호환 — 기존 호출 동작 유지).
        """
        entry = self._manifest["chunks"].get(str(idx))
        if not isinstance(entry, dict):
            return None
        # 청크 경계 방어적 재검증 — 어긋나면 캐시 미스 처리(재추출 유도).
        if start is not None and entry.get("start") != start:
            return None
        if end is not None and entry.get("end") != end:
            return None
        fname = entry.get("file")
        if not fname:
            return None
        fpath = self.cache_dir / fname
        try:
            if not fpath.is_file():
                return None
            text = fpath.read_text(encoding="utf-8")
        except OSError:
            return None
        # 무결성 확인: 저장 시 기록한 sha 와 비교.
        expected = entry.get("sha")
        if expected is not None:
            actual = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            if actual != expected:
                return None
        return text

    def store(self, idx: int, start: int, end: int, text: str) -> None:
        """청크 idx 결과를 chunk_<idx>.md 로 저장하고 manifest 갱신(원자적 쓰기).

        실패해도 예외 없이 무시(resume 는 best-effort — 추출 자체는 이미 성공).
        """
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fname = f"chunk_{idx}.md"
            fpath = self.cache_dir / fname
            # 원자적 쓰기: tmp 에 쓰고 rename.
            tmp = fpath.with_suffix(".md.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(fpath)
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            self._manifest["chunks"][str(idx)] = {
                "start": start,
                "end": end,
                "file": fname,
                "sha": sha,
            }
            self._write_manifest()
        except OSError:
            # 디스크 오류 — resume 혜택만 포기, 변환은 계속.
            pass

    def _write_manifest(self) -> None:
        """manifest.json 원자적 쓰기(tmp→rename)."""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            mpath = self.cache_dir / _MANIFEST_NAME
            tmp = mpath.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(mpath)
        except OSError:
            pass

    # ── 진단/정리 ────────────────────────────────────────────────────────────

    def cached_indices(self) -> set:
        """현재 manifest 에 완료 기록된 청크 index 집합(정수)."""
        out = set()
        for k in self._manifest["chunks"].keys():
            try:
                out.add(int(k))
            except (ValueError, TypeError):
                continue
        return out

    def cleanup(self) -> None:
        """이 PDF 의 캐시 디렉토리 전체 삭제(전체 변환 성공 후 호출).

        실패해도 무시 — 캐시 잔존이 정확성을 해치지 않는다(다음 실행 시 키로 검증).
        """
        try:
            if self.cache_dir.is_dir():
                shutil.rmtree(self.cache_dir, ignore_errors=True)
        except OSError:
            pass
