# net_tracer 교차검증 연계 조사 (Survey)

> **목적**: filestomdwgem vision QA(Quality Assurance, PDF→Markdown 추출 검증)와
> `pdf-to-kicad` 스킬의 `net_tracer`(벡터 와이어 트레이싱) **교차검증 연계** 설계를
> 위한 read-only 조사. 코드 변경 없음(본 보고서 Write만).
>
> - 조사일: 2026-06-03
> - 조사 범위(read-only): `~/.claude/skills/pdf-to-kicad/scripts/{net_tracer,cross_validator,pdf_vector_parser,pin_resolver,extraction_verifier}.py`,
>   `SKILL.md`, `prompts/stage1_extraction.md`, `prompts/stage4_verification.md`,
>   filestomdwgem `lib/{vision_qa,vision_qa_ensemble,ollama_extractor}.py`
> - 대상 PDF: `/Users/heni/workspace/04_NX/01_raw/schematics/PICOMATRIX_NG-ULTRA_EMV_REV01(230103).pdf` (42p)

---

## 0. 핵심 결론 요약 (TL;DR)

1. **`cross_validator.py`는 이미 vector↔vision 교차검증 모듈이다.** 부품 병합
   (`merge_all`)과 **넷 병합(`merge_nets`)** 두 경로를 모두 가진다. `merge_nets`는
   `net_tracer` 출력(traced nets) ↔ vision nets를 넷 이름 기준으로 합집합 병합한다.
   따라서 "신규 교차검증 엔진"은 불필요하고, **어댑터(filestomdwgem MD → cross_validator
   입력 자료구조)** 가 본질이다.
2. **대상 PDF는 벡터 PDF다.** 회로도 페이지(p11)에 line 세그먼트 2,477개가 있고,
   net_tracer가 실제로 259개 net(named 14개, no_connect 122개)을 추출했다. 즉
   net_tracer의 벡터 의존성은 이 PDF에서 충족된다. (래스터 스캔 PDF였다면 무력)
3. **매칭 난점은 "정렬 키"다.** filestomdwgem vision QA 산출은 **designator/value
   단위**(예: `C71 [unreadable]`)이고, net_tracer 산출은 **net→(ref,pin) 그래프
   엣지**다. 둘을 잇는 공통 키는 (a) **designator(ref)** 와 (b) net name 문자열뿐이며,
   PIN→NET 행 단위 정렬은 vision MD가 그 구조를 명시적으로 갖고 있을 때만 가능하다.
4. **권장**: **옵션 B(신규 경량 교차검증) + 옵션 C(net_tracer를 ground-truth로 주입)
   의 결합**을 권장. cross_validator 재사용(옵션 A)은 자료구조 어댑팅 비용 대비
   이득이 작고, 스킬 코드 import 경계(다른 워크스페이스/venv)가 운영 리스크가 크다.
   net_tracer는 **읽기 전용 결정적 넷리스트 생성기로만** filestomdwgem 안에서 호출하고,
   교차검증 로직은 filestomdwgem `lib/`에 신규 경량 모듈로 두는 것이 결합도·유지보수
   측면에서 유리하다.

---

## 1. 모듈 인터페이스 표

각 스크립트의 public 표면과 입출력 자료구조.

### 1.1 `net_tracer.py` — 벡터 → 결정적 넷리스트

| 항목 | 내용 |
| --- | --- |
| 클래스 | `NetTracer(vector_raw: dict)` |
| **입력** | `vector_raw` dict — **PDF 자체가 아님**. `{"lines":[{x0,y0,x1,y1},...], "texts":[{text,x,y,...},...]}`. 이 dict는 `pdf_vector_parser.PdfVectorParser`가 생성(stage1 Step1에서 `vector_raw.json`으로 저장). 즉 **net_tracer 입력 = pdf_vector_parser 출력**. |
| public 메서드 | `trace() -> dict`, `trace_and_merge() -> dict` (trace + OCR 중복 net normalize/병합 + 핀이름 추론) |
| **출력** | `{"nets":[{name, connections:[{ref,pin}], label_count:int, wire_count:int}], "no_connects":[{x,y[,ref,pin]}], "junctions":[]}` |
| 동작 | H/V 선분만 필터 → 끝점 union-find로 net 그룹화 → 근처 텍스트로 net 이름 라벨링(`NET_PATTERNS` 정규식: GND/VCC/SPI_*/I2C_* 등) → ref 텍스트 주변 끝점을 `(ref,pin)` connection으로 등록 → 45° X마커를 no_connect로 검출 |
| 자료구조 키 | net 식별 = **name 문자열**, connection = **(ref, pin) 튜플** |
| **벡터 의존성** | 절대적. `lines`가 비면 `{"nets":[], ...}` 반환. 래스터 PDF에선 `pdf_vector_parser.extract_lines()`가 빈 리스트 → net_tracer 무력. |

### 1.2 `cross_validator.py` — **이미 vector↔vision 교차검증 모듈**

| 항목 | 내용 |
| --- | --- |
| 클래스 | `CrossValidator()` (상태 없음, 순수 함수 집합) |
| `normalize_value(str)->str` | 단위 정규화: `10K`→`10kω`, `100NF`→`100nf`, `4.7UF`→`4.7uf` |
| `normalize_ref(str)->str` | ref 정규화: `ESP32-S3`→`ESP32S3`, `U 1`→`U1` (공백/하이픈/밑줄 제거, 대문자) |
| `is_same_position(a,b,threshold_mm=5.0)->bool` | 좌표 근접 매칭 |
| `merge_component(vector, vision)->dict` | 단일 부품 병합. 둘 다 있으면 `source="vector+vision"`, value는 **vision 우선**, confidence=max |
| **`merge_all(vectors, visions)->(merged, conflicts)`** | 부품 리스트 전체 병합. **Phase1: ref 매칭 → Phase2: 위치 근접(5mm) 매칭 → Phase3: 단일소스 추가** (정확히 vision↔vector 교차검증 패턴) |
| **`merge_nets(traced_nets, vision_nets)->list`** | **넷 교차검증**. 동일 name: connections 합집합. traced에만: 유지. vision에만: `source="vision_only"`. |
| 입력 자료구조 | 부품: `{ref, value, position:{x,y}, confidence?}`. 넷: `{name, connections:[{ref,pin}], ...}` |
| **재사용 판정** | **vision↔net 교차검증 본체가 이미 존재**(`merge_nets`). 다만 "병합/합집합" 성격이라 *환각 제거*나 *unverified 확정* 같은 판정 로직은 없음(아래 §2). |

### 1.3 `pdf_vector_parser.py` — net_tracer 입력 생성기

| 항목 | 내용 |
| --- | --- |
| 클래스 | `PdfVectorParser(pdf_path)` — `fitz`(pymupdf)+`pdfplumber` 기반 |
| `extract_texts(page)->[(text,x,y,fs)]` | 텍스트 블록(좌표 포함) |
| `extract_lines(page)->[(x0,y0,x1,y1)]` | **선분 추출(pdfplumber+fitz 병합·중복제거)** — net_tracer의 핵심 입력 |
| `extract_rects`, `get_page_dimensions`, `render_page(dpi)`, `identify_components(page)` | 사각형/페이지크기/PNG렌더/부품그룹핑 |
| **래스터 제약** | `extract_lines`는 PDF 벡터 drawing(`page.lines`, `fitz get_drawings`)에 의존. **스캔/래스터 PDF는 drawing이 없어 빈 리스트 → net_tracer 무력**. 이 경우 vision-only 폴백(stage1 에러처리 표에 명시됨). |

### 1.4 `pin_resolver.py` — 핀↔넷 배정

| 항목 | 내용 |
| --- | --- |
| `PinResolver(symbols_dir)` | KiCad `.kicad_sym`에서 핀 정의 파싱 |
| `get_pins_from_lib(lib_id)->[{name,number,at_x,at_y,angle}]` | 라이브러리 핀 정의 |
| **`resolve(part_mapping, traced_netlist)->dict`** | **net_tracer 넷리스트를 입력**으로 부품별 핀-넷 배정. `(ref,pin)→net_name` 역인덱스 구축 + ESP32/USB 핀명 퍼지 매칭. 출력 `{assignments, coverage}` |
| 관찰 | net_tracer 출력이 **하위 단계(핀 배정)의 입력**임을 보여줌 = net_tracer는 결정적 ground-truth 소스로 설계됨. |

### 1.5 `extraction_verifier.py` — QA 검증(추출자와 분리)

| 항목 | 내용 |
| --- | --- |
| `verify_phase_a(components, traced_netlist, vector_raw, labels_json?)->dict` | **부품 커버리지 + 넷 완전성** 검증. `vector_raw`의 ref 집합 ↔ 추출 ref 집합 비교로 `missing`(누락)·`spurious`(허위/환각)·`dangling`(고립 넷) 산출. verdict PASS/FAIL |
| `verify_phase_b(pin_assignments)->dict` | IC/패시브 핀 커버리지 100% 검증 |
| 관찰 | **`spurious_refs`(추출됐는데 벡터에 없는 ref) = vision 환각 검출 로직이 이미 존재**. net_tracer/vector_raw가 결정적 ground-truth, 추출(vision)이 검증 대상이라는 구도. filestomdwgem의 "환각 제거" 시나리오와 직접 대응. |

### 1.6 filestomdwgem vision QA 산출 형식 (정렬 키 분석)

| 항목 | 내용 |
| --- | --- |
| `lib/vision_qa.py::review()` | 단일 Claude vision verifier. 1차 MD(Ollama) ↔ 원본 페이지 PNG 대조 → 교정 MD 전문 반환. 4대 오류(환각존재/범위일반화/값오독/누락) 교정. 플래그: `[unverified]`, `[unverified range]`, `[unreadable]` |
| `lib/vision_qa_ensemble.py` | verifier N=3 독립 실행 → **designator별 다수결**. `_parse_verdicts(md)`가 designator(예: `C71`, `R91-R112`, BGA ball `U24.M8`)별 판정(confirmed value / `[unreadable]` / `[unverified]`) 추출 |
| **출력 granularity** | **designator + value(+flag)**. 즉 vision QA의 1급 키는 **designator(=ref)** 와 **value**다. **PIN→NET 그래프 엣지는 1급 산출이 아님** — MD에 `**Relations**`/`PIN -> NET` 행이 있으면 텍스트로 존재하나, ensemble 투표는 designator-value 축에서만 동작. |
| **공통 정렬 키** | net_tracer와의 교집합 키 = **(1) designator/ref 문자열**, (2) **net name 문자열**. 좌표는 vision MD에 대개 없음(BGA ball 좌표는 예외적). |

---

## 2. cross_validator 재사용 가능성

**결론: 기존 교차검증은 "vision↔vector 병합(합집합)"이며 vision↔net 경로도 이미 있음.
그러나 filestomdwgem의 목적(환각 제거·unverified 확정)에는 판정 로직이 부족하고,
입력 자료구조 어댑팅 + 크로스-워크스페이스 import 비용이 크다.**

- **이미 vision↔net인가? → 예(부분).** `merge_nets(traced_nets, vision_nets)`가 정확히
  그 경로. `merge_all`은 부품(designator/value) 교차검증. `extraction_verifier.verify_phase_a`의
  `spurious_refs`는 **환각 검출**(filestomdwgem 시나리오 (b))을 이미 수행.
- **filestomdwgem MD를 입력으로 어댑트 가능한가? → 가능하나 비용 존재.**
  - cross_validator는 **dict 자료구조**(`{ref,value,position}`, `{name,connections:[{ref,pin}]}`)를
    받는다. filestomdwgem 산출은 **Markdown 텍스트**다. 따라서 **MD→dict 파서 어댑터**가
    반드시 선행돼야 한다(designator/value는 `vision_qa_ensemble._parse_verdicts` 류 파싱으로
    추출 가능, 그러나 `position`은 MD에 없어 `is_same_position`/`merge_all` Phase2(좌표매칭)는
    무력 → ref 매칭만 동작).
  - `merge_nets`는 vision 쪽이 `{name, connections:[{ref,pin}]}`를 요구. filestomdwgem MD가
    PIN→NET 행을 구조적으로 담고 있지 않으면 connections를 만들 수 없음 → **net 교차검증은
    designator(ref)↔net.connections.ref 수준의 약한 매칭으로 축소**.
- **재사용의 함정 (스킬 import 경계).** cross_validator/net_tracer는 다른 워크스페이스
  (`~/.claude/skills/pdf-to-kicad/`)의 코드이고 전용 `.venv`(pdfplumber/pymupdf)를 쓴다.
  filestomdwgem이 이를 직접 `import`하면: (i) 스킬 버전 변동에 filestomdwgem이 깨질 수 있고,
  (ii) [STRICT] read-only/변경금지 룰상 스킬 쪽을 수정해 맞출 수 없으며, (iii) 두 venv
  의존성이 충돌할 수 있다. → **프로세스 경계(서브프로세스로 net_tracer만 실행하고 JSON을
  받는 방식)** 가 import보다 안전.

---

## 3. 입력 제약 — 대상 PDF 벡터 여부 (실측)

net_tracer 동작에는 **PDF에 벡터 line drawing이 필요**하다. 대상 PDF를 실측했다.

**대상**: `PICOMATRIX_NG-ULTRA_EMV_REV01(230103).pdf` (42p)

`fitz.get_drawings()` / `pdf_vector_parser` 실측 결과:

| page | drawings | lines | curves | rects | images | words | 판정 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| p1 (표지/타이틀) | 48 | 24 | 1 | 46 | 1 | 291 | 저밀도 |
| p2 | 45 | 24 | 1 | 43 | 3 | 119 | 저밀도 |
| **p11 (회로도)** | **1897** | **2459** | 1245 | 45 | 1 | 1074 | **벡터 고밀도** |
| **p12 (회로도)** | 1647 | 2006 | 933 | 43 | 1 | 866 | **벡터 고밀도** |
| p21 | 384 | 372 | 121 | 43 | 1 | 241 | 중밀도 |

추가로 **net_tracer를 p11에 실제 실행**(`PdfVectorParser.extract_lines` → `NetTracer.trace_and_merge`):

```
page11: texts 1193, lines 2477, identify_components 399
total nets 259 | named nets 14 | no_connects 122
named net 예: CS(conns=404, label=1), VS(conns=529), DRB_A10(wire=39),
             DRB_CASn(conns=18), LDM(conns=64), NC_E2(conns=200) ...
```

**판정: 대상 PDF는 벡터 PDF다.** 회로도 페이지에 수천 개 line 세그먼트가 있고,
net_tracer가 실제로 의미 있는 넷리스트(named net + no_connect + (ref,pin) connection)를
생성한다. → **net_tracer 적용 가능.** (각 페이지에 image도 1개씩 있으나 이는 로고/배경이며
벡터 라인이 지배적. 만약 향후 래스터 스캔본을 다루면 `extract_lines`가 비어 net_tracer는
무력해지고 vision-only로 폴백해야 함.)

> ⚠️ 단, p1/p2 같은 저밀도 페이지(타이틀블록·표)는 named net이 거의 없으므로
> 교차검증 가치는 회로도 본문 페이지(p11~)에 집중된다.

---

## 4. 연계 설계 옵션 (2~3안)

### 공통 전제

- net_tracer 산출 = **결정적 넷리스트**(높은 신뢰의 (ref,pin)↔net 그래프, 단 OCR
  아티팩트 net과 추론 connection 포함 → 노이즈 있음).
- vision QA 산출 = **designator/value 중심 MD**(+ 선택적 PIN→NET 텍스트 행), 환각·범위
  일반화·값오독 가능.
- **정렬 키**: 1순위 **ref(designator) 문자열**(양측 정규화 후), 2순위 **net name 문자열**.
  좌표 매칭은 MD에 좌표가 거의 없어 비현실적.

---

### 옵션 A — cross_validator 재사용 어댑터

filestomdwgem MD를 파싱해 cross_validator dict 입력으로 변환 후 `merge_all`/`merge_nets`
+ `extraction_verifier.verify_phase_a` 호출.

- **매칭 방식**: `normalize_ref` 기반 ref 매칭(좌표 없으므로 Phase2 위치매칭 비활성).
- **출력 형태**: merged components/nets dict + Phase A report(`spurious_refs`=환각,
  `missing_refs`=누락, `dangling_nets`).
- **장점**: 검증된 코드 재사용, `spurious_refs` 환각 검출 즉시 활용.
- **단점**:
  - MD→dict 파서 어댑터를 어차피 새로 작성해야 함(이득 상쇄).
  - **크로스-워크스페이스 import**(스킬 venv 의존) → 운영 취약, read-only 룰상 스킬 측
    수정 불가.
  - cross_validator는 "병합"이지 "vision 환각 제거/ unverified 확정" 판정기가 아님 →
    filestomdwgem 시나리오 (a)(unverified 확정), (b)(환각 제거)를 직접 수행하려면 결국
    추가 로직 필요.
- **구현 규모**: 중(어댑터 + import/서브프로세스 경계 + venv 관리).

### 옵션 B — 신규 경량 교차검증 (vision MD ↔ net_tracer 넷리스트 매칭) **[권장 핵심]**

filestomdwgem `lib/`에 신규 경량 모듈(예: `lib/net_crosscheck.py`)을 두고, net_tracer
넷리스트(JSON)를 입력으로 받아 MD의 designator/PIN→NET와 대조.

- **매칭 방식**:
  1. **ref 집합 대조**: net_tracer connection의 `ref` 집합 ↔ MD designator 집합.
     - net_tracer에 **없는** MD designator → **환각 의심**(시나리오 b). 단 net_tracer가
       못 읽는 텍스트-only 라벨/표 부품은 예외 처리(보수적 플래그만, 자동삭제 금지).
     - MD에 없는 net_tracer ref → vision 누락 후보(시나리오 c 보강).
  2. **net name 대조**: MD에 등장하는 net name 토큰 ↔ net_tracer named net.
  3. **PIN→NET 행 확정**(시나리오 a): MD가 `Uxx pin N -> NETNAME` 류 행을 가질 때,
     net_tracer의 `(ref,pin)→net` 역인덱스로 대조. 일치 시 `[unverified]` 제거(확정),
     불일치 시 net_tracer 값으로 교정 후보 제시, net_tracer에 (ref,pin) 자체가 없으면
     환각 플래그 유지.
- **출력 형태**: filestomdwgem 계약 유지(교정 MD 전문) + 부가 리포트(JSON: confirmed /
  hallucination_suspect / missing / conflict 목록). 플래그는 기존 `[unverified]`/`[unreadable]`
  체계 재사용.
- **장점**:
  - 결합도 최소(스킬 코드 직접 import 불필요 — net_tracer는 **서브프로세스로 JSON만 산출**).
  - filestomdwgem 룰/플래그 체계와 자연 통합, ensemble 다수결 위에 결정적 게이트 1단 추가.
  - read-only 룰 위반 없음(스킬 변경 0).
- **단점**: 매칭 정확도가 **MD의 PIN→NET 구조 명시성**에 의존. MD가 designator-value
  중심이고 PIN→NET 행이 빈약하면 시나리오 (a)는 부분만 동작(ref/net 집합 대조까지는 항상 가능).
- **구현 규모**: 소~중(파서 + 매칭 + 리포트, ~300~400 LOC).

### 옵션 C — net_tracer 넷리스트를 vision QA의 "결정적 ground truth"로 주입

vision QA verifier 프롬프트(`vision_qa._build_qa_prompt`)에 net_tracer 넷리스트를
**참조 사실(reference netlist)** 로 주입하여, verifier가 환각/값을 그것과 대조해 교정.

- **매칭 방식**: 매칭을 **LLM에 위임**. 프롬프트에 "아래는 벡터 트레이싱으로 추출한
  결정적 넷리스트다. 이미지와 함께 이를 ground-truth 참조로 삼아 환각/누락/PIN→NET을
  교정하라"는 지시 + net_tracer JSON 요약을 UNTRUSTED와 분리된 TRUSTED 블록으로 첨부.
- **출력 형태**: 기존 교정 MD 전문(형식 계약 그대로). 별도 리포트 없음.
- **장점**: 구현 극소(프롬프트 + JSON 직렬화). 매칭의 모호성(핀명 변형 IO/GPIO 등)을
  LLM이 흡수.
- **단점**:
  - **비결정적**(LLM 판단) → ensemble의 결정성 약화. net_tracer의 OCR 아티팩트 net이
    프롬프트 노이즈로 환각을 *유발*할 위험.
  - net_tracer 넷리스트가 크면(259 net) 프롬프트 토큰 급증·E2BIG 위험(현재 stdin 전달로
    일부 완화). 신뢰 경계 설계 필요(net_tracer는 TRUSTED, MD는 UNTRUSTED).
  - "결정적 확정"이라는 본래 이점(시나리오 a)을 LLM 확률 판단으로 희석.
- **구현 규모**: 소(프롬프트 1곳 + 요약 직렬화).

---

## 5. 권장안 + 리스크

### 권장: **옵션 B 중심 + 옵션 C 보조 (B∘C 하이브리드)**

1. **net_tracer는 filestomdwgem 안에서 "결정적 넷리스트 생성기"로만 서브프로세스 호출**한다.
   스킬 코드를 import하지 말고, 스킬 `.venv/bin/python`으로 `pdf_vector_parser → net_tracer.trace_and_merge`를
   실행해 `traced_netlist.json`만 받는다(프로세스 경계 = read-only 룰·venv 충돌·버전
   변동 리스크 모두 회피).
2. **옵션 B**: filestomdwgem `lib/net_crosscheck.py`(신규)에서 net_tracer JSON ↔ ensemble
   교정 MD를 **ref 집합·net name·(가능 시)PIN→NET 행**으로 대조하여:
   - net_tracer에 (ref,pin)이 결정적으로 존재 → vision의 `[unverified]` 해제(시나리오 a),
   - net_tracer에 전혀 없는 designator/핀 → 환각 플래그 유지/강화(시나리오 b, **자동 삭제는
     금지**·플래그만 — 텍스트-only 부품 오탐 방지),
   - net_tracer에만 있는 ref/net → vision 누락 후보로 리포트(시나리오 c는 vision이 보강
     주체이므로 net_tracer가 못 읽는 값/라벨은 그대로 vision 유지).
3. **옵션 C 보조(선택)**: 회로도 본문 페이지 한정으로, net_tracer **named net 요약만**(전체
   259개가 아닌 label_count>0 또는 connections 다수인 상위 net)을 verifier 프롬프트에
   TRUSTED 참조로 주입해 PIN→NET 교정 recall을 보강. 단 기본 OFF(품질 비교 후 on).
4. **`extraction_verifier.verify_phase_a`의 `spurious_refs` 알고리즘을 차용**(코드 import이
   아니라 *로직 모사*)하여 환각 검출 게이트를 구현 → 검증된 접근을 read-only로 가져옴.

### 리스크

| 리스크 | 내용 | 완화 |
| --- | --- | --- |
| **매칭 정확도(정렬 키)** | MD는 designator-value 중심, PIN→NET 행이 빈약하면 시나리오 (a) recall 저하. net name 문자열 매칭은 변형(IO/GPIO, 3V3/VD_3V3)에 취약 | `pin_resolver._match_net_by_name`의 변환 규칙을 *모사*해 net name 퍼지 매칭; ref 집합 대조는 항상 가능하므로 최소 (b)는 보장 |
| **net_tracer 노이즈** | OCR 아티팩트 net(`Net_*`, `PIU*`, `COC/COR`)·추론 connection(`_infer_from_pin_name`)이 ground-truth를 오염 → 정상 vision 값을 환각으로 오탐 | `extraction_verifier._EXCLUDED_NET_PREFIXES` 모사로 아티팩트 net 제외; 환각 판정은 **자동삭제 금지·플래그만** 보수적 적용 |
| **벡터 의존성** | 래스터/스캔 PDF는 net_tracer 무력(빈 넷리스트) → 교차검증 무효 | net_tracer 빈 출력 시 교차검증 자동 skip(vision-only 폴백), 페이지별 `lines>0` 가드 |
| **스킬 코드 import 경계** | 스킬은 read-only·전용 venv. 직접 import 시 버전/의존성 결합 | **서브프로세스 + JSON 계약**으로 격리(스킬 변경 0, filestomdwgem만 신규 파일) |
| **AGENT INDEPENDENCE 원칙** | 스킬 원칙(추출자≠검증자)과 정합 필요 — filestomdwgem도 이미 동일 원칙(vision_qa.py 주석) | 교차검증을 vision QA *이후* 독립 게이트로 배치(추출=Ollama, 검증=Claude vision, 교차검증=결정적 net_tracer 게이트 3계층) |
| **프롬프트 토큰/주입(옵션 C)** | net 요약 주입 시 토큰 급증·UNTRUSTED 경계 혼입 | 상위 net만 요약·TRUSTED/UNTRUSTED nonce 분리(기존 `_build_qa_prompt` C3 가드 확장) |

---

## 6. 참조 파일 경로

- `~/.claude/skills/pdf-to-kicad/scripts/net_tracer.py` (`NetTracer.trace`/`trace_and_merge`)
- `~/.claude/skills/pdf-to-kicad/scripts/cross_validator.py` (`merge_all`, **`merge_nets`**, `normalize_ref`)
- `~/.claude/skills/pdf-to-kicad/scripts/pdf_vector_parser.py` (`extract_lines`, `extract_texts`)
- `~/.claude/skills/pdf-to-kicad/scripts/pin_resolver.py` (`resolve`, `_match_net_by_name`)
- `~/.claude/skills/pdf-to-kicad/scripts/extraction_verifier.py` (`verify_phase_a` → `spurious_refs` 환각검출)
- `~/.claude/skills/pdf-to-kicad/prompts/stage1_extraction.md` (net_tracer→cross_validator 배선)
- `~/.claude/skills/pdf-to-kicad/prompts/stage4_verification.md` (AGENT INDEPENDENCE)
- `/Users/heni/workspace/filestomdwgem/lib/vision_qa.py` (`review`, `_build_qa_prompt`)
- `/Users/heni/workspace/filestomdwgem/lib/vision_qa_ensemble.py` (`_parse_verdicts`, designator 다수결)
- 대상 PDF: `/Users/heni/workspace/04_NX/01_raw/schematics/PICOMATRIX_NG-ULTRA_EMV_REV01(230103).pdf`
</content>
</invoke>
