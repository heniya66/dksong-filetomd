# Figure 이미지 GraphRAG 통합 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 각 Task 완료 후 Advisor(Opus) QA 게이트(이슈 0까지) 통과 후 커밋. 커밋/push는 사용자 승인 후. 재인덱싱은 codesign-rag MCP(`codesign_ingest` + `codesign_graph build`)로만.

**Goal:** PDF의 `Figure N` 다이어그램만 크롭·저장하고, codesign-rag GraphRAG 검색 시 관련 청크에 연결된 figure 이미지(base64)를 동반 반환한다.

**Architecture:** filestomdwgem이 figure를 하이브리드 검출·크롭하고 `_figures.json` 사이드카를 만든다. codesign-rag ingest가 이를 흡수해 청크 메타(`figure_ids`)·ontology figure 노드로 연결하고, `codesign_search`가 결과 청크의 figure를 base64로 동봉한다. `figure_id`가 전 단계를 관통하는 연결 키.

**Tech Stack:** Python 3.x, PyMuPDF(fitz), Ollama Cloud `gemini-3-flash-preview`(멀티모달, localhost 게이트웨이), ChromaDB(청크), OntologyStore(JSONL+SQLite), Pillow(다운스케일).

**근거:** PoC(`scripts/poc_figure_crop.py`) 검증 완료 — Figure 다이어그램 3/3 정확 크롭, 하이브리드 검출 IoU 0.88~0.996. 설계: `docs/superpowers/specs/2026-06-05-figure-image-graphrag-design.md`.

---

## File Structure

**filestomdwgem (변환):**
- Create: `lib/figure_extractor.py` — 하이브리드 figure 검출·크롭·사이드카(핵심 모듈, PoC 승격)
- Modify: `extract_all_via_pdf.py` — opt-in env(`EXTRACT_FIGURES=1`)로 figure_extractor 연결(기본 동작 보존)
- Create: `tests/test_figure_extractor.py`

**codesign-rag (검색):**
- Modify: `src/mcp/tools/ingest.py` / `src/rag/indexer.py` — `_figures.json` 흡수 + 이미지 보관 + 청크 `figure_ids` 연결
- Modify: `src/rag/ontology/chunk_linker.py` — figure 노드 + `chunk_references_figure` 링크
- Modify: `projects/nx.yaml` — figure entity/predicate 등록(E 승인)
- Modify: `src/mcp/tools/search.py` — 검색 응답 `images` base64 동봉(다운스케일+상한)
- Create: `tests/regression/test_figure_image_graphrag.py`

---

## Task 1: filestomdwgem `figure_extractor` (PoC → 운영 모듈)

**Files:**
- Create: `/Users/heni/workspace/filestomdwgem/lib/figure_extractor.py`
- Reference: `/Users/heni/workspace/filestomdwgem/scripts/poc_figure_crop.py` (PoC 로직), `lib/ollama_extractor.py`(render/멀티모달), `extract_all_via_pdf.py`(extract_figure_id 류 캡션 헬퍼가 있으면 재활용)
- Test: `/Users/heni/workspace/filestomdwgem/tests/test_figure_extractor.py`

**인터페이스(확정):**
```python
def extract_figures(pdf_path: Path, out_dir: Path, *, dpi: int = 300,
                    strict_caption: bool = True) -> list[dict]:
    """Figure N 캡션 다이어그램만 검출·크롭. 반환 = _figures.json 항목 리스트.
    각 항목: {figure_id, page, image_path, caption, figure_no, type, bbox, source, snap_iou}
    strict_caption=True: 'Figure N' 캡션 매칭 안 되면 제외(로고/표/캡션없는 도형 배제)."""
```

- [ ] **Step 1: 실패 테스트 작성** — 캡션 엄격 필터(핵심). PoC 대상 PDF로 "Figure 캡션 figure만 채택, 로고(LLM 0박스 raster)·표는 제외" 단언.
```python
# tests/test_figure_extractor.py
from pathlib import Path
from lib.figure_extractor import extract_figures
PDF = Path("/Users/heni/workspace/04_NX/01_raw/datasheets/NGULTRA/Application_Notes_v0_0_1-SoC AXI test.pdf")

def test_strict_caption_only_accepts_figure_n(tmp_path):
    figs = extract_figures(PDF, tmp_path, strict_caption=True)
    # 모든 채택 figure는 'Figure N' figure_no 를 가진다(표/로고 배제)
    assert figs, "Figure 다이어그램이 검출되어야 함"
    assert all(f["figure_no"].lower().startswith("figure") for f in figs)
    # 로고 페이지(p01)는 figure로 채택되지 않음
    assert not any(f["page"] == 1 for f in figs)
    # 본문 Figure 1/2/3(아키텍처/AXI/FSM) 캡션이 포함됨
    captions = " ".join(f["caption"] for f in figs).lower()
    assert "architecture" in captions and "fsm" in captions
```

- [ ] **Step 2: 실패 확인** — `cd /Users/heni/workspace/filestomdwgem && .venv/bin/python -m pytest tests/test_figure_extractor.py -v` → FAIL(모듈 없음).

- [ ] **Step 3: 구현** — `lib/figure_extractor.py` 작성. PoC `scripts/poc_figure_crop.py`의 하이브리드 로직(페이지 렌더 → LLM bbox(ollama_extractor 멀티모달 경로) → raster bbox(`get_image_rects`)/벡터 군집(`get_drawings`) 스냅 → 캡션 위치(`get_text("dict")`)) 이식. **추가 운영화**: (a) `strict_caption`: `Figure N` 정규식 매칭 안 되는 후보 제외(LLM 0박스 raster·표 배제), (b) 동일 페이지 다중 figure y좌표 오름차순 정렬 후 `pNN_figK`, (c) `_figures.json` 사이드카 기록(스키마는 설계 §6.1). 크롭 저장: `out_dir/figures/<stem>__pNN_figK.png`. 키 값 출력 금지.

- [ ] **Step 4: 통과 확인** — 위 pytest PASS. 추가로 사이드카 스키마 테스트(`figure_id`/`figure_no`/`image_path`/`bbox` 키 존재) PASS.

- [ ] **Step 5: extract_all_via_pdf opt-in 통합 테스트** — `EXTRACT_FIGURES` 미설정 시 기존 출력 byte-identical(동작 보존), 설정 시 figures/ + `_figures.json` 생성. 테스트로 양쪽 단언.

- [ ] **Step 6: 커밋(승인 후)** — `feat(figures): figure_extractor — Figure 캡션 다이어그램 하이브리드 검출·크롭·사이드카(opt-in)`

**Advisor QA 게이트:** 캡션 엄격 필터 정확성(Figure만, 로고/표 제외) · 동작 보존(opt-in off) · 사이드카 스키마 · teeth.

---

## Task 2: codesign-rag ingest — `_figures.json` 흡수 + 청크 연결

**Files:**
- Modify: `/Users/heni/workspace/codesign-rag/src/mcp/tools/ingest.py`, `/Users/heni/workspace/codesign-rag/src/rag/indexer.py`
- Reference: `src/convert/datasheet.py`(`_tables.json` 사이드카 흡수 패턴), `src/rag/ontology/chunk_linker.py`(`entity_ids` 메타 연결 패턴), `src/convert/base.py`(`extract_figure_id`)
- Test: `/Users/heni/workspace/codesign-rag/tests/regression/test_figure_image_graphrag.py`

- [ ] **Step 1: 실패 테스트** — ingest 시 `<stem>_figures.json`이 있으면, 청크 본문의 "Figure N" 언급을 가진 청크 메타에 `figure_ids`(JSON 문자열)가 연결되고, 이미지가 `${base_path}/figures/`로 복사됨을 단언. (figure_id ↔ 청크 매칭은 `extract_figure_id`로 청크 텍스트의 figure_no를 사이드카 figure_no와 대조.)

- [ ] **Step 2: 실패 확인** — `cd /Users/heni/workspace/codesign-rag && .venv/bin/python -m pytest tests/regression/test_figure_image_graphrag.py -v` → FAIL.

- [ ] **Step 3: 구현** — ingest 경로에 사이드카 로더 추가(`_tables.json` 흡수와 동일 위치/패턴). 청크 메타 `figure_ids` 사후 연결(`chunk_linker`의 `entity_ids` 갱신 패턴 재사용, idempotent sorted set). 이미지 파일 `${base_path}/figures/` 복사(있으면 skip). 매칭 실패 figure는 경고 로깅(저장은 됨).

- [ ] **Step 4: 통과 확인** — pytest PASS. 역호환: 사이드카 없으면 기존 ingest 동작 불변(별도 테스트).

- [ ] **Step 5: 커밋(승인 후)** — `feat(ingest): _figures.json 흡수 — 청크 figure_ids 연결 + 이미지 보관`

**Advisor QA 게이트:** figure_ids 연결 idempotent · 매칭 정확성 · 역호환(사이드카 없을 때 불변) · 이미지 복사 안전.

---

## Task 3: codesign-rag ontology figure 노드 + 멀티홉

**Files:**
- Modify: `/Users/heni/workspace/codesign-rag/src/rag/ontology/chunk_linker.py`, `/Users/heni/workspace/codesign-rag/projects/nx.yaml`
- Reference: `src/rag/ontology/schema.py`(Entity/Fact), multihop_searcher(멀티홉)
- Test: 동 `test_figure_image_graphrag.py`에 멀티홉 케이스 추가

- [ ] **Step 1: 실패 테스트** — figure가 `node_type=figure` entity로 OntologyStore에 등록되고, `chunk_references_figure`(chunk→figure) fact가 생성되며, 멀티홉 검색이 "figure 참조 청크 → figure"를 따라가 figure_id를 반환함을 단언.

- [ ] **Step 2: 실패 확인** — pytest → FAIL.

- [ ] **Step 3: 구현** — (a) `nx.yaml`의 `entities`에 `figure`, `allowed_predicates`에 `chunk_references_figure` 추가. (b) `chunk_linker`에 figure entity 등록 + chunk→figure fact append(기존 entity/fact append 패턴 재사용). (c) 멀티홉 탐색이 figure 노드를 hop 대상으로 포함하도록 결선(기존 multihop이 entity 노드를 타는 경로 확인 후 figure node_type 허용).

- [ ] **Step 4: 통과 확인** — pytest PASS. ontology store에 figure 노드·fact 생성 확인.

- [ ] **Step 5: 커밋(승인 후)** — `feat(ontology): figure 노드 + chunk_references_figure 멀티홉(nx.yaml 등록)`. ⚠️ nx.yaml 변경은 codesign-rag 워크스페이스 변경(승인됨). 재인덱싱은 MCP.

**Advisor QA 게이트:** figure 노드/predicate 정확 등록 · 멀티홉 추적 동작 · nx.yaml 스키마 유효 · 기존 ontology 무회귀.

---

## Task 4: codesign-rag 검색 응답 — figure base64 동봉

**Files:**
- Modify: `/Users/heni/workspace/codesign-rag/src/mcp/tools/search.py`
- Reference: `_build_grounding_block`/`_add_confidence`(additive 필드 패턴), Pillow(다운스케일)
- Test: 동 `test_figure_image_graphrag.py`에 검색 응답 케이스 추가

**정책(확정):** 최대변 1024px 다운스케일(PNG 재인코딩), 응답당 figure 상한 기본 3(env `SEARCH_MAX_FIGURES`), 원본 `image_path`도 함께 제공.

- [ ] **Step 1: 실패 테스트** — `handle_search` 결과 청크 메타에 `figure_ids`가 있으면 응답에 `images:[{figure_id, caption, image_b64, image_path}]`가 additive로 포함되고, base64 디코딩 시 최대변 ≤1024px, figure 수 ≤ 상한임을 단언. figure 없는 결과는 `images` 필드 생략(역호환).

- [ ] **Step 2: 실패 확인** — pytest → FAIL.

- [ ] **Step 3: 구현** — `handle_search`의 결과 후처리(grounding 블록 직후)에서, 각 결과 청크 `figure_ids` → `${base_path}/figures/` 이미지 로드 → Pillow로 최대변 1024px 다운스케일 → base64 인코딩 → `result["images"]` 구성(상한 적용, dedup). 키/시크릿 출력 금지. 검색 자체에 graceful(이미지 로드 실패해도 검색 결과 정상 — try/except).

- [ ] **Step 4: 통과 확인** — pytest PASS. 역호환(figure 없을 때 `images` 미포함) PASS. M-4 캐시·F-4 metrics·F-13 grounding 경로 무회귀.

- [ ] **Step 5: 커밋(승인 후)** — `feat(search): 검색 응답 figure base64 동봉(1024px 다운스케일+상한, 역호환)`

**Advisor QA 게이트:** base64 다운스케일/상한 정확 · 역호환 · graceful(이미지 실패 무영향) · 기존 search 경로 무충돌.

---

## Task 5: nx 프로젝트 E2E 검증

**Files:**
- Reference: 전체 파이프라인. 대상 PDF: `/Users/heni/workspace/04_NX/01_raw/datasheets/NGULTRA/Application_Notes_v0_0_1-SoC AXI test.pdf`
- Test: E2E 스모크(수동 + 자동 혼합)

- [ ] **Step 1: 변환** — `EXTRACT_FIGURES=1`로 대상 PDF 변환 → MD + `figures/` + `_figures.json` 생성 확인(Figure 1/2/3 크롭).

- [ ] **Step 2: ingest + graph (codesign-rag MCP)** — `codesign_ingest(project=nx, ...)` + `codesign_graph(action=build)`로 nx 프로젝트에 적재·그래프 빌드. 청크 `figure_ids`·figure 노드 생성 확인.

- [ ] **Step 3: 검색 검증** — `codesign_search(project=nx, query="NG-ULTRA architecture")` → 응답에 관련 청크 + `images`(Figure 1 아키텍처 base64) 포함 확인. 멀티홉으로 "FSM/AXI 관련 질의 → 해당 figure" 반환 확인.

- [ ] **Step 4: 결과 리포트** — 검출률·이미지 동반 반환 성공률·base64 크기 측정. 크롭 이미지 육안 확인(사용자 보고).

- [ ] **Step 5: 커밋(승인 후) + 완료 보고** — E2E 검증 리포트. 필요 시 메모리에 "figure 이미지 GraphRAG 통합 완료" 기록.

**Advisor QA 게이트:** E2E 전 과정 동작 · 이미지 정확 반환 · 멀티홉 figure 추적 · 성능(base64 크기) 수용 가능.

---

## Self-Review (작성자 점검 완료)

- **Spec coverage:** 설계 §5(컴포넌트) → Task 1~4, §8(단계) → Task 1~5, §6(데이터구조) → Task 1(사이드카)/2(메타)/3(노드), §5.4(base64 정책) → Task 4. 누락 없음.
- **Placeholder:** 각 Task에 파일 경로·인터페이스·테스트 케이스·검증 명령·커밋 메시지 명시. 완전한 구현 코드는 실행 서브에이전트가 코드베이스(기존 패턴 `_tables.json`/`entity_ids`/`grounding`) 참조해 작성 — 각 Task에 참조 위치 명시.
- **Type consistency:** `figure_id`(pNN_figK), `figure_no`(Figure N), `figure_ids`(청크 메타 JSON), `chunk_references_figure`(predicate), `images`(검색 응답) — 전 Task 일관.
- **재인덱싱**: Task 5에서 codesign-rag MCP 단일 경로. 커밋/push 사용자 승인.
