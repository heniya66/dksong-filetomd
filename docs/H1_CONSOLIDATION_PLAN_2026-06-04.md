# H-1 스크립트 통합 계획안 (B③) — 2026-06-04

> 목표: 루트의 per-document `extract_*.py` 스크립트들이 청크→추출→병합→저장 파이프라인을 **중복 재구현**한 문제(H-1)를 해소.
> 방식: 읽기 전용 분석(13 에이전트)으로 각 스크립트의 실제 차이를 전수 매핑 → 안전한 단계별 통합 설계.
> **전체 위험도: medium. 핵심 원칙: Phase 1~3은 관찰 가능한 동작을 ZERO 변경(완전 보존), 실제 개선은 Phase 4에서 명시적 opt-in.**

## 분류 (총 11개 extract_*.py)

| 분류 | 스크립트 | 처리 |
|------|----------|------|
| **통합 대상 (중복)** | extract_pdf_blockdiagram, extract_eda_pyojun, extract_ava2, extract_eda_pangdan, extract_pdf_image_analysis, extract_ava1, extract_sim_platform, extract_gemini_multimodal (8개) | `lib/pdf_pipeline.py`로 통합 |
| **제외 (다른 파이프라인)** | extract_docx_multimodal (텍스트 경로, PDF 아님 — QA M-4와 얽힘), extract_tables (.txt 출력 일회성 보수툴) | 손대지 않음 |
| **리팩터 소스** | extract_all_via_pdf.py (메인 — Phase 3까지 미변경) | 통합 대상 아님 |

8개 중복은 **딱 5가지만 다릅니다**: ① 프롬프트 ② 청크 정책(20페이지 / ≤25면 단일 / ava1은 항상 단일) ③ 호출 간 sleep(10초 vs 15초) ④ 출력 경로 ⑤ 실패 정책(전부 abort). 나머지(청크 루프·`\n\n---\n\n` 병합·`{stem}.md` 저장)는 동일합니다.

## 통합 설계

**결정: 새 모듈 `lib/pdf_pipeline.py` 신설** (메인 `extract_all_via_pdf.py`를 import하지 않음 — 그러면 AUTO/ensemble/netcheck 전체가 딸려와 오히려 비대해짐).

```
def convert_pdf(pdf_path, prompt_template, *, output_path,
                chunk_size=None, single_chunk_max=None,
                rate_limit_s=10, post_process=None,
                on_failure='abort') -> Path | None   # 기본 abort = 원본 동작 보존
```

- **프롬프트는 각 스크립트의 현재 문자열을 그대로(verbatim) 전달** — 프롬프트 한 글자만 바뀌어도 모델 출력이 달라지므로, byte-동일 프롬프트 = byte-동일 추출 호출 (동작 보존의 핵심).
- `single_chunk_max`: ≤N페이지면 단일 청크(ava1=무한대, ava2/eda=25 등) → 각 스크립트의 청크 정책 그대로 재현.
- `chunk_size=20 핀 고정`: config 변경이 과거 동작을 바꾸지 못하게(SSoT 채택은 Phase 4 opt-in).
- `rate_limit_s`: 스크립트별 sleep(10/15) 보존.
- `post_process`: sim_platform의 `renumber_images`(전역 이미지 번호 재정렬) 훅으로 보존.
- `on_failure='abort'`(기본): 현재 동작(실패 시 None 반환, 아무것도 안 씀) 그대로. `'partial'`은 H-5(부분본/재개) 개선을 **명시적으로 켜는 레버**.
- H-3(재시도)·H-4(truncation)는 `ox.extract_pdf_pages` 호출로 **자동 상속**(공짜).

각 per-doc 스크립트는 → **docstring + PROMPT_TEMPLATE 상수 + convert_pdf() 호출 한 번** (현재 85~131줄 → 30줄 미만).

## 동작 보존 방법 — "특성화(characterization) 테스트"

⭐ **핵심 안전망**: 모델 호출을 결정하는 건 `(프롬프트, pdf, start, end)` 튜플뿐입니다. 그래서:
1. **원본 스크립트**를 `ox.extract_pdf_pages`를 mock한 상태로 돌려 → `(프롬프트, start, end)` 호출 시퀀스 + 병합/저장 경로를 **골든 스냅샷**으로 기록.
2. **통합본**이 동일 스냅샷을 재현하는지 단언.
→ **실제 클라우드 LLM 토큰을 한 푼도 안 쓰고**, 청크 경계 off-by-one·프롬프트 드리프트·경로 변화를 결정적으로 잡아냄.
3. phase당 1회만 가장 작은 실제 PDF로 구조 스모크(헤더/`---`/표/figure 개수 비교 — 비결정적 LLM이라 byte-diff 아닌 구조 비교).

## 단계별 롤아웃 (각 단계 롤백 가능, 스크립트 1개당 1커밋, Advisor QA 게이트)

| Phase | 범위 | 동작 변경 |
|-------|------|-----------|
| **0. 골격+안전망** | `lib/pdf_pipeline.py` + 단위테스트 + 8개 원본 골든 스냅샷 레코더. **기존 스크립트 미변경** | 없음 |
| **1. 저위험 2개** | blockdiagram, eda_pyojun (깔끔한 청커) | 없음 |
| **2. 중위험 3개** | ava2, eda_pangdan, image_analysis (≤25 단일청크 그룹) | 없음 |
| **3. 고위험 3개** | ava1(항상 단일), sim_platform(renumber 훅), gemini_multimodal(임시파일→메모리 병합) — **각각 사용자 결정 후** | 없음(임시파일 소멸 제외) |
| **4. (선택) 개선** | opt-in: abort→partial(H-5), 핀→config SSoT, (선택) 메인 파이프라인도 lib 채택 | **의도적 변경** |

## 사용자 결정 필요 (Open Questions)

가장 중요 ↓
1. **이 스크립트들이 아직 계속 쓰이나요, 아니면 특정 PDF 대상 일회성 작업이 끝난 건가요?** (ava1→AVA_1.pdf, eda_pangdan→판단서, sim_platform→Sim_Platform.pdf 등 특정 파일 하드코딩. CI/Makefile 참조 없음.) **끝난 일회성이면 통합보다 아카이브 후 삭제가 더 깔끔.**
2. ava1은 현재 **항상 단일 청크**(자기 주석의 ≤25 규칙 무시). 그대로 보존? 아니면 표준 20페이지/≤25로 통일? (통일 시 25페이지 초과 문서 출력이 바뀜)
3. sleep(10/15초): 스크립트별 유지 vs 10초 통일?(처리량/레이트리밋 노출 변화)
4. config SSoT 채택 지금? vs chunk_size=20 핀 유지?(과거 출력 재현성)
5. 실패 정책: abort 유지 vs H-5 partial 개선?(파일 출력 달라짐)
6. gemini_multimodal의 임시 `_chunk_NNN.md` 파일을 읽는 다운스트림이 있나요?(없으면 메모리 병합 선호)
7. 출력 경로: 절대경로 표준화 vs 원본 리터럴 보존?
8. Phase 4에서 메인 파이프라인도 lib 채택?(장기적으론 깔끔, 큰 변경)

**기본 권고**: 위 2~8은 "현재 동작 보존"을 기본값으로 채택(Phase 1~3 무변경), 개선은 Phase 4 opt-in. **#1만 먼저 답해주시면** 전체 범위가 정해집니다.

## 권고
**PROCEED — 단, 점진적·동작보존.** 빅뱅 재작성 금지. 먼저 **Phase 0(공통 모듈+안전망)** 만 만들면 이후 모든 통합이 토큰 없이 "동작 동일"임을 증명 가능. 위험도 순(저→중→고)으로, 스크립트당 1커밋, 각 배치마다 Advisor QA.
