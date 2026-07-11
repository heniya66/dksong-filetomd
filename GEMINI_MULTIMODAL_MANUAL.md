# 멀티모달 문서 추출 운영 매뉴얼 (v2.0 — ollama_cloud 기준)

`filestomdwgem` 워크스페이스에서 **PDF/문서 → Markdown(MD)** 변환을 운영하기 위한 가이드입니다.
이 문서는 실제 코드(`lib/ollama_extractor.py`, `lib/config.py`, `lib/vision_qa.py`,
`lib/pdf_pipeline.py`, `config.yaml`)에 근거하며, 코드에 없는 옵션은 기술하지 않습니다.

> 파일명 'GEMINI'는 레거시 명칭입니다. 현행 기본 추출 경로는 **Gemini 직접 호출이 아니라
> Ollama Cloud 로컬 게이트웨이**입니다. (구버전 지침 `GEMINI.md`는 DEPRECATED.)

---

## 1. 한눈에 보는 현행 구조 (3단)

| 단계 | 역할 | 활성 조건 | 키 필요 |
|------|------|-----------|---------|
| ① 1차 추출 (기본) | Ollama Cloud 로컬 게이트웨이 vision 추출 | `EXTRACT_PROVIDER=ollama_cloud` (기본값) | **불필요** (localhost) |
| ② 1차 추출 (fallback) | Gemini File API 직접 호출 — 품질 비교·롤백용 | `EXTRACT_PROVIDER=gemini` (opt-in) | `GEMINI_API_KEY` (Keychain) |
| ③ 2차 보정 (선택) | Claude CLI vision verifier 가 1차 MD 를 원본 이미지와 대조·교정 | `VISION_QA=claude_cli` + `VISION_QA_TRUSTED=1` | Claude CLI 구독 OAuth |

- **기본 동작**: ① 만 동작 (env 아무것도 안 줘도 ollama_cloud, 키 불필요).
- ②, ③ 은 명시적으로 켤 때만 동작. 둘 다 끄면 가장 단순한 1-pass 추출.
- 모든 시크릿은 **macOS Keychain SSoT(Single Source of Truth)**. 코드/`.env`/`config.yaml`
  평문 저장 금지. 사전 주입: `source $HOME/workspace/_shared/keychain-env.sh`.

---

## 2. 1차 추출 — Ollama Cloud (기본 경로)

### 동작 방식 (`lib/ollama_extractor.py`)
- PDF 페이지를 `fitz`(PyMuPDF)로 PNG 래스터화 → base64 → 로컬 게이트웨이
  (`http://localhost:11434/v1`)의 vision 모델(`gemini-3-flash-preview`)에 전송.
- localhost 경유이므로 **Authorization 헤더/키 불필요**. (게이트웨이가 인증을 대행.)
- 핵심 함수:
  - `extract_pdf_pages(prompt, pdf_path, start, end)` — 페이지 범위 추출 (청크 단위).
  - `extract_pdf_single_page(prompt, pdf_path, page)` — 단일 페이지 (표 재추출 등).
  - `count_pdf_pages(pdf_path)` — 페이지 수 조회.
  - `provider_label()` — 현재 provider 표시 문자열 (로그용).

### 사전 준비
```bash
# Ollama 로컬 데몬(게이트웨이)이 떠 있어야 함
ollama serve            # (별도 터미널 / LaunchAgent)
# 최초 1회 device-key 로그인
ollama signin
```

---

## 3. 1차 추출 — Gemini fallback (opt-in, 품질 비교·롤백 전용)

`EXTRACT_PROVIDER=gemini` 일 때만 `google.generativeai` File API 경로로 전환됩니다.

- 사용 모델: `gemini-2.5-pro` (config `options.gemini_fallback_model`, env `GEMINI_VISION_MODEL`).
- **`GEMINI_API_KEY` 필요** — `os.getenv("GEMINI_API_KEY")` 로만 읽음 (Keychain → 셸 주입).
  미설정 시 명시적 오류. 키를 argv/코드/파일로 전달하지 않음 (구버전과 달라진 핵심 차이).

```bash
source $HOME/workspace/_shared/keychain-env.sh   # GEMINI_API_KEY 주입
EXTRACT_PROVIDER=gemini python3 extract_gemini_multimodal.py
```

---

## 4. 2차 보정 — Claude Vision verifier (선택)

`lib/vision_qa.py`. 1차 추출(generator)이 만든 MD 를, **원본 페이지 이미지를 실제로 보는**
Claude CLI verifier 로 대조하여 값 환각(예: 22µF↔220 flip)·범위 일반화 추정·핀/부품
fabrication 을 교정합니다. 추출자와 검증자를 분리하는 독립 검증 원칙.

### 활성화 (2개 게이트 모두 필요)
```bash
VISION_QA=claude_cli \
VISION_QA_TRUSTED=1 \
python3 extract_all_via_pdf.py
```
- `VISION_QA=claude_cli` — verifier 백엔드 선택. 미설정 시 2차 보정 전체 OFF (기본).
- `VISION_QA_TRUSTED=1` — **보안 게이트(M-9)**. claude CLI verifier 는 전역 Read 권한이라
  신뢰할 수 없는 PDF 의 프롬프트 인젝션으로 로컬 파일이 노출될 수 있음. 따라서 **신뢰 입력일
  때만** 코드가 verifier 를 켜도록 강제. truthy(`1/true/yes/on`) 아니면 비활성(거부+경고).

### 동작 메모
- 호출: `claude -p --allowed-tools "Read" --permission-mode acceptEdits` (프롬프트는 stdin —
  argv E2BIG 방지). claude 가 프롬프트에 적힌 PNG 경로를 Read 도구로 **이미지로 로드(vision)**.
- 페이지 PNG 는 1차 추출과 동일 DPI 로 렌더하여 격리 임시 디렉터리에 staging.
- (보조) `VISION_QA_SANDBOX_PROFILE=auto` 로 `sandbox-exec` blocklist 를 추가 적용 가능
  (완전 jail 아님 — 보조 완화).

---

## 5. 주요 설정 knob (config.yaml / 환경변수)

우선순위는 항상 **env > config.yaml > 코드기본값** (`lib/config.py` 로더가 강제).
시크릿(`GEMINI_API_KEY`/`OLLAMA_API_KEY`)은 이 로더를 경유하지 않고 호출 측 `os.getenv()` 직접.

### 추출 (options:)
| config.yaml 경로 | 환경변수 | 기본값 | 설명 |
|------------------|----------|--------|------|
| `options.provider` | `EXTRACT_PROVIDER` | `ollama_cloud` | `ollama_cloud` \| `gemini` |
| `options.model` | `OLLAMA_VISION_MODEL` | `gemini-3-flash-preview` | ollama_cloud vision 모델 |
| `options.gemini_fallback_model` | `GEMINI_VISION_MODEL` | `gemini-2.5-pro` | gemini fallback 모델 |
| `options.chunk_size` | `EXTRACT_CHUNK_SIZE` | `20` | 청크당 페이지 수 |
| `options.rate_limit_s` | `EXTRACT_RATE_LIMIT_S` | `10.0` | 청크 간 sleep 초수 (convert_pdf 기본) |
| `options.render_dpi` | `EXTRACT_RENDER_DPI` | `150` | 추출 경로 페이지 PNG 렌더 DPI |
| `options.vision_qa_dpi` | `VISION_QA_DPI` > `EXTRACT_RENDER_DPI` | `220` | 2차 verifier PNG 렌더 DPI |
| `options.max_tokens` | `OLLAMA_CLOUD_MAX_TOKENS` | `8192` | thinking 모델 빈응답 방지 |
| `options.resume` | `EXTRACT_RESUME` | `true` | 대용량 PDF resume(중단 후 이어받기) 캐시. 중단 시 완료 청크는 건너뛰고 미완료만 재추출. `0`/`false`/`no`/`off` 로 비활성(끄면 기존 동작 100% 동일) |
| `options.resume_keep_cache` | `EXTRACT_RESUME_KEEP` | `false` | 전체 성공 시 resume 캐시 보존 여부(기본=성공 시 정리). `1`/`true` 면 `output/<...>/.resume_cache/` 유지(디버깅용) |

> resume 캐시 키 = PDF 지문(size+mtime) + 청크정책(chunk_size·single_chunk_max) + 프롬프트 해시 + provider.
> PDF/설정이 바뀌면 키가 달라져 옛 캐시는 자동 무시(stale 방지). 캐시 손상·부재 시 graceful 하게 처음부터 재추출.
> `lib/pdf_pipeline.convert_pdf` 경유 7개 스크립트(ava1/ava2/eda_*/sim_platform/gemini_multimodal/blockdiagram/image_analysis)에 자동 적용.

### Ollama Cloud 게이트웨이 (ollama_cloud:)
| config.yaml 경로 | 환경변수 | 기본값 | 설명 |
|------------------|----------|--------|------|
| `ollama_cloud.base_url` | `OLLAMA_BASE_URL` > `OLLAMA_CLOUD_BASE_URL` | `http://localhost:11434/v1` | 로컬 게이트웨이 |
| `ollama_cloud.timeout` | `OLLAMA_CLOUD_TIMEOUT` | `600` | 단일 호출 timeout(초) |
| `ollama_cloud.max_retries` | `OLLAMA_MAX_RETRIES` | `4` | 재시도 최대 횟수 |
| `ollama_cloud.retry_base_delay` | `OLLAMA_RETRY_BASE_DELAY` | `1.0` | 지수 백오프 기저(초) |
| `ollama_cloud.retry_max_delay` | `OLLAMA_RETRY_MAX_DELAY` | `60.0` | 계산 백오프 상한(초) |
| `ollama_cloud.retry_after_cap` | `OLLAMA_RETRY_AFTER_CAP` | `120.0` | Retry-After 헤더 절대 상한(초) |

### 2차 보정 verifier (vision_qa: + extract_all_via_pdf 전용 env)
| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `VISION_QA` | (미설정 = OFF) | `claude_cli` 로 2차 보정 활성 |
| `VISION_QA_TRUSTED` | (미설정 = 비활성) | `1` 이어야 claude_cli 실제 동작 (보안 게이트) |
| `VISION_QA_CLAUDE_BIN` | `claude` | claude CLI 실행파일 |
| `VISION_QA_MODEL` | (CLI 기본) | verifier 모델 |
| `VISION_QA_TIMEOUT` | `600` | verifier 호출 timeout(초) |
| `VISION_QA_SANDBOX_PROFILE` | (미설정) | `auto` 또는 `.sb` 경로 — 보조 sandbox |
| `VISION_QA_ENSEMBLE` | `0` | ≥2 시 n-회 앙상블 검증 (extract_all_via_pdf) |
| `VISION_QA_NETCHECK` | `0` | `1` 시 net_tracer 교차검증 (vision QA 활성 시만) |
| `VISION_QA_AUTO` | `0` | 페이지별 자동 티어링 (dense/light) |
| `VISION_QA_RATE_DELAY` | `0.0` | verifier 호출 간 기본 대기(초) — M-6 적응형과 별개 |

> 주의: `EXTRACT_RATE_LIMIT_S`(청크 추출 sleep)와 `VISION_QA_RATE_DELAY`(verifier 호출 간격)는
> **서로 다른 concern** 입니다. 전자는 1차 추출 청크 사이, 후자는 2차 verifier 호출 사이.

---

## 6. 실행 진입점

| 스크립트 | 용도 |
|----------|------|
| `extract_all_via_pdf.py` | `input/pdf/*.pdf` 일괄 — 앙상블/티어링/2차 보정 풀 파이프라인 |
| `extract_gemini_multimodal.py` | `input/pdf/*.pdf` 일괄 — 한국어 멀티모달(파일명 'gemini'는 레거시) |
| `extract_ava1.py` / `extract_ava2.py` | AVA_1/2.pdf 문서특화 프롬프트 |
| `extract_eda_pangdan.py` / `extract_eda_pyojun.py` | EDA 판단서/표준구조 문서특화 |
| `extract_sim_platform.py` | Sim_Platform.pdf (이미지 순번 후처리 포함) |
| `scripts/extract_tables.py` | 특정 페이지 표만 GFM 으로 재추출 (보수 유틸) |

per-document 스크립트(ava/eda/sim 등)는 공통 파이프라인 `lib/pdf_pipeline.convert_pdf` 경유.
경로는 모두 `Path(__file__).parent` 기준 상대경로 (워크스페이스 이동에 안전).

```bash
source $HOME/workspace/_shared/keychain-env.sh
python3 extract_all_via_pdf.py            # 기본: ollama_cloud, 2차 보정 OFF
```

---

## 7. 품질 팁
- 입력 해상도가 높을수록 수치/표 판독 정밀. 도면·표 고밀도 PDF 는 `EXTRACT_RENDER_DPI` 상향 고려.
- 모델 변경(2.5-pro → 3-flash-preview)으로 도면/표 품질이 의심되면 실제 PDF 1건 전후 비교 권장.
- 회로도 등 값 환각이 우려되는 신뢰 입력은 ③ 2차 보정(`VISION_QA=claude_cli` + `VISION_QA_TRUSTED=1`) 활성.
- 프롬프트 튜닝은 각 `extract_*.py` 의 `PROMPT_TEMPLATE` 상수에서 수정 (문서특화 프롬프트 보존).
