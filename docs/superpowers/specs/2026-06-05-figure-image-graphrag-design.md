# Figure 이미지 GraphRAG 통합 — 설계 문서

- **작성일**: 2026-06-05
- **대상 시스템**: filestomdwgem (변환) + codesign-rag (GraphRAG 검색)
- **상태**: 설계 확정 (사용자 승인 대기 → writing-plans)

---

## 1. 목적 (Purpose)

PDF를 Markdown(MD)으로 변환할 때 **본문 `Figure N` 캡션이 붙은 다이어그램/도면 이미지만** 검출·크롭하여 별도 저장하고, codesign-rag의 GraphRAG(그래프 기반 Retrieval-Augmented Generation) 검색에서 **관련 청크(chunk)가 해당 figure를 참조하면 그 이미지(base64)를 검색 응답에 함께 반환**한다. 사용자는 텍스트 설명과 도면 이미지를 함께 받는다.

비목표(Non-goals, YAGNI):
- 표(Table) 이미지화 — codesign-rag가 이미 `_tables.json`/`_tables.md`로 처리하므로 제외.
- 로고·머리말·장식 raster 이미지 — `Figure N` 캡션 엄격 필터로 자동 배제.
- 캡션 없는 다이어그램 — 1차 범위에서 제외(엄격 모드).
- 답변 생성(Generation) — codesign-rag 정책상 호스트 LLM(Large Language Model) 책임(이중 LLM 호출 금지).

---

## 2. 배경 (Background) — 현재 상태와 PoC 결과

### 현재 상태
- **filestomdwgem**: 멀티모달 LLM으로 figure를 풍부하게 **텍스트 분석**해 MD에 작성(`FIGURE_RULES`). 단 **이미지를 파일로 저장하지 않음**(페이지를 base64로 LLM에 보내고 버림). MD에 이미지 참조 없음.
- **codesign-rag**: 사이드카 JSON 패턴(`_tables.json`/`_callgraph.json`), 청크 메타 커스텀 필드(`entity_ids`), `chunk_linker`(청크↔entity 양방향 링크), 검색 응답 additive 필드(grounding/confidence), `extract_figure_id`/`extract_caption` 헬퍼를 이미 보유 — **이미지 통합에 재활용 가능한 hook이 거의 다 존재**.

### PoC 결과 (2026-06-05, 대상: NGULTRA Application_Notes SoC AXI test.pdf, 16페이지)
- 하이브리드 검출(LLM bbox + raster/벡터 스냅)로 본문 figure **12/12 검출·크롭**, 그 중 벡터 도면 **8/8** 성공.
- 스냅 보정 정밀도(IoU, Intersection over Union) **0.88~0.996**.
- **본 설계 범위(`Figure N` 다이어그램만)** 기준: 본문 `Figure 1/2/3`(아키텍처 블록도·SOC AXI wrapper·FSM 전이도) **3/3 정확 크롭**.
- 발견된 한계 2가지(본 설계에 반영): (1) 로고 false positive → 캡션 엄격 필터로 해결, (2) 동일 페이지 다중 figure y좌표 정렬.

---

## 3. 요구사항 (확정)

| # | 결정 | 확정값 |
|---|------|--------|
| A | figure 검출 대상 | **엄격** — 본문 `Figure N` 캡션이 매칭되는 다이어그램만 |
| B | 검출 전략 | **하이브리드** — LLM 정규화 bbox + raster bbox/벡터 군집 스냅 + 캡션 필터 |
| C | 검색 응답 이미지 형태 | **base64** (최대변 1024px 다운스케일 + 응답당 figure 상한) |
| D | 그래프 연결 | **ontology figure 노드 + 멀티홉(multi-hop)** |
| E | ontology yaml 변경 | **승인됨** (nx 프로젝트) |
| F | 적용 범위 | 기능은 codesign-rag 시스템 전체(전 프로젝트 호환), 검증·ontology는 **nx 프로젝트(04_NX NGULTRA)** |

---

## 4. 아키텍처 (Architecture)

```
PDF
 │  [filestomdwgem] lib/figure_extractor.py
 ├─ 페이지 렌더(300 DPI) → LLM bbox 검출 → raster/벡터 스냅 → Figure N 캡션 엄격 필터
 ├─ 크롭 PNG 저장: output/<type>_md/figures/<stem>__<figure_id>.png
 └─ 사이드카: output/<type>_md/<stem>_figures.json
        │  (figure_id 가 전 단계를 관통하는 연결 키)
        ▼
 [codesign-rag] ingest
 ├─ <stem>_figures.json 흡수 → 청크 메타 figure_ids 연결
 ├─ 이미지 파일을 ${base_path}/figures/ 로 보관
 └─ ontology: figure 노드 등록 + chunk —references→ figure (chunk_linker)
        ▼
 [codesign-rag] codesign_search
 └─ 결과 청크에 연결된 figure → images:[{figure_id, caption, image_b64}] additive 동봉
        ▼
 호스트 LLM(Claude 등)이 텍스트 + 이미지 함께 표시
```

**연결 키**: `figure_id`(문서 내 식별자, 예: `p07_fig1` = 페이지+순번; 전역 참조는 `source_file` + `figure_id` 조합)가 변환→ingest→graph→검색을 일관되게 관통한다. 본문 캡션 번호는 별도 필드 `figure_no`(예: `Figure 1`)로 보존한다.

---

## 5. 컴포넌트 명세

### 5.1 filestomdwgem — `lib/figure_extractor.py` (신규)
PoC 스크립트(`scripts/poc_figure_crop.py`)를 운영 모듈로 승격.

- **입력**: PDF 경로, 출력 디렉토리.
- **검출 파이프라인**:
  1. 페이지를 300 DPI PNG로 렌더(`render_pdf_pages_to_base64` 재활용, 렌더 px 기록).
  2. 멀티모달 LLM(Ollama Cloud `gemini-3-flash-preview`, localhost 게이트웨이)에 페이지 이미지 → figure별 `{bbox(0~1000 정규화), type, caption}` JSON 요청.
  3. 결정적 신호로 스냅 보정: raster bbox(`page.get_image_rects`), 벡터 군집(`page.get_drawings` 공간 군집), 캡션 위치(`page.get_text("dict")`의 `Figure N`).
  4. **Figure N 캡션 엄격 필터**(`extract_figure_id`): 캡션이 `Figure N`으로 매칭되는 것만 채택. **LLM 0박스 raster·캡션 없는 도형은 제외**(로고 false positive 제거).
  5. 동일 페이지 다중 figure는 y좌표 오름차순 정렬 후 `fig1, fig2…` 번호.
- **출력**:
  - 크롭 PNG: `output/<type>_md/figures/<stem>__<figure_id>.png`
  - 사이드카 `output/<type>_md/<stem>_figures.json` (스키마는 §6).
- **통합**: `extract_all_via_pdf.py`에 **opt-in env**(예: `EXTRACT_FIGURES=1`)로 연결. 미설정 시 기존 동작 byte-identical 보존.

### 5.2 codesign-rag — ingest 확장
- `_figures.json` 사이드카를 읽음(`_tables.json` 흡수 패턴 동일).
- 청크 본문에서 `extract_figure_id`로 `Figure N` 언급을 찾아 해당 figure_id를 **청크 메타 `figure_ids`(JSON 문자열)**에 연결(`entity_ids` 패턴 동일, idempotent).
- 이미지 파일을 `${base_path}/figures/`로 복사(검색 서버가 접근 가능하게).

### 5.3 codesign-rag — ontology figure 노드 (`chunk_linker` 확장)
- figure를 `node_type=figure` entity로 OntologyStore(JSONL + SQLite)에 등록(figure_id, caption, image_path, page).
- predicate `chunk_references_figure`(청크 → figure) 추가 → nx.yaml `allowed_predicates`에 등록(E 승인).
- 멀티홉 검색이 "부품/개념 설명 청크 → 참조하는 figure"를 따라가도록.

### 5.4 codesign-rag — 검색 응답 확장 (`handle_search`)
- 결과 청크 메타에 `figure_ids`가 있으면, 각 figure를 로드해 `images:[{figure_id, caption, image_b64}]`를 응답에 additive 추가(grounding 블록 패턴, `result["images"]`).
- **base64 정책**: 이미지 최대변 1024px 다운스케일(PNG 재인코딩) + **응답당 figure 상한(기본 3, env 조정)**. 원본 경로(`image_path`)도 함께 제공해 호스트가 선택 가능.
- 역호환: figure 연결이 없으면 `images` 필드 생략(기존 응답 불변).

---

## 6. 데이터 구조

### 6.1 `_figures.json` 사이드카 스키마
```json
[
  {
    "figure_id": "p07_fig1",
    "page": 7,
    "image_path": "figures/<stem>__p07_fig1.png",
    "caption": "Figure 1: NG-ULTRA Architecture",
    "figure_no": "Figure 1",
    "type": "block",
    "bbox": [x0, y0, x1, y1],
    "source": "raster|vector",
    "snap_iou": 0.886
  }
]
```

### 6.2 청크 메타데이터 (ChromaDB)
- 기존 필드(source_file, breadcrumb, section, entity_ids…) + **`figure_ids`** (JSON 문자열, sorted set, idempotent).

### 6.3 ontology figure 노드 (OntologyStore)
- entity: `{id: figure_id, node_type: "figure", caption, image_path, page, source_file}`
- fact(predicate): `chunk_references_figure` (subject=chunk_id, object=figure_id, provenance).

---

## 7. 엣지케이스 / 에러 처리

| 상황 | 처리 |
|------|------|
| 캡션 없는 도면 | 제외(엄격) — 로깅만 |
| LLM 0박스 raster(로고 등) | 제외(figure 아님) |
| 멀티페이지 figure | 1차 단일 페이지 크롭(드묾) — 향후 옵션 |
| figure-청크 매칭 실패 | figure는 저장되나 청크 연결 없음 → 검색에 안 나옴, 경고 로깅 |
| base64 한도 초과 | 1024px 다운스케일; 그래도 크면 응답 상한으로 figure 수 제한 |
| 검출 0건 페이지 | 이미지 없음(폴백 페이지 저장 안 함 — 텍스트는 MD로 충분) |
| 재인덱싱 | codesign-rag MCP(`codesign_ingest` + `codesign_graph build`)로만 수행 |

---

## 8. 단계 구현 계획 (각 단계 Advisor(Opus) QA 게이트)

1. **filestomdwgem `figure_extractor`**: PoC → 운영 모듈(캡션 엄격 필터·y정렬·사이드카), opt-in 통합. 동작 보존(미설정 시 무변경) + 검출 회귀 테스트.
2. **codesign-rag ingest 사이드카 연결**: `_figures.json` 흡수 → 청크 `figure_ids` 연결 + 이미지 보관. 회귀 테스트.
3. **ontology figure 노드 + 멀티홉**: chunk_linker 확장 + nx.yaml predicate. 멀티홉 추적 테스트.
4. **검색 응답 base64 동봉**: `handle_search` 확장 + 다운스케일·상한. 역호환 테스트.
5. **nx 프로젝트 E2E 검증**: 실제 NGULTRA PDF로 변환→ingest→graph→검색 전 과정, 이미지 동반 반환 확인. 재인덱싱은 codesign-rag MCP.

---

## 9. 테스트 전략

- 각 단계 회귀 테스트(teeth 포함) + Advisor 적대적 QA(이슈 0까지).
- filestomdwgem: 캡션 필터 정확성(Figure만 채택, 로고/표 제외), 사이드카 스키마, 동작 보존(opt-in off byte-identical).
- codesign-rag: figure_ids 연결 idempotent, 검색 역호환(images 없으면 기존 응답 불변), base64 다운스케일/상한, 멀티홉 figure 추적.
- E2E: nx 프로젝트에서 "이 figure 관련 질의 → 텍스트 + 올바른 이미지" 반환.

---

## 10. 제약 (codesign-rag 정책 준수)

- 답변 생성은 호스트 LLM 책임(이중 LLM 호출 금지) — figure는 검색 응답에 **재료(이미지)**로만 제공.
- 시크릿은 Keychain SSoT(Single Source of Truth) — 키 평문 금지.
- 외부 CLI LLM(코드 생성) 금지(Claude 단독). 멀티모달 검출 LLM(gemini-3-flash-preview)은 figure 검출 대상 도구로 정상 사용.
- git commit/push는 사용자 승인 후. 재인덱싱은 codesign-rag MCP 단일 경로.
