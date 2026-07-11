# Phase 3 — Claude Vision 2차 QA 보정 단계: 구현 + 통합 테스트

generator(Ollama 1차 추출) + verifier(Claude vision 2차 검증·교정) 파이프라인을
구현하고, 회로도 1페이지(p11 SoC DDR2(2))에 대해 before/after를 실측한다.

## 구현 요약

| 항목 | 내용 |
| :--- | :--- |
| 신규 모듈 | `lib/vision_qa.py` |
| 공개 API | `review(primary_md, pdf_path, start, end)` (PDF 페이지), `review_image(primary_md, image_path)` (단일 이미지), `is_enabled()`, `backend_label()` |
| 통합 지점 | `extract_all_via_pdf.py` `extract_chunk()`(PDF), `extract_image()`(이미지) — 1차 추출 후 `vqa.review*()` 경유 |
| opt-in 플래그 | 환경변수 `VISION_QA=claude_cli` (미설정 시 기본 OFF — 기존 동작 그대로) |
| verifier 백엔드 | claude CLI 구독 OAuth(`claude -p ... --allowed-tools "Read" --permission-mode acceptEdits`) |
| 정공법 분기(TODO) | `VISION_QA=claude_api` → Anthropic messages API 이미지 content block (`_review_via_api` 스텁, 키 Keychain SSoT) |
| 형식 계약 보존 | Figure 섹션 구조 / `{stem}.md` / `\n\n---\n\n` 청크 결합 그대로 (verifier가 교정 MD 전문 반환, 코드펜스 자동 제거) |
| 안전 degrade | 호출 실패·타임아웃·빈 응답 시 1차 추출본 그대로 반환(corrected=False) + stderr 경고 → 파이프라인 무중단 |

### claude CLI vision 입력 실제 동작 방식 (실측 검증)

```
claude -p "<프롬프트 — /tmp/visionqa_pNN.png 경로 명시>" \
    --allowed-tools "Read" \
    --permission-mode acceptEdits
```

- `claude -p`(--print) = 비대화형 1-shot. 이미지 직접 첨부 플래그는 없으나,
  **프롬프트에 로컬 PNG 경로를 주고 `--allowed-tools "Read"`를 허용하면 claude가
  Read 도구로 그 PNG를 이미지(=vision)로 로드**한다. 텍스트가 아니라 실제 픽셀을 본다.
- 실측 확인(2026-06-02): 격리 테스트에서 타이틀블록 "SoC DDR2", 제조사,
  DDR2 라우팅 회로 IC 개수까지 정확히 판독 → vision 입력 동작 확정.
- `--permission-mode acceptEdits`로 비대화형 Read 권한 프롬프트 통과.
- 페이지 PNG는 1차 추출과 **동일 DPI(220)/페이지**로
  `ollama_extractor.render_pdf_pages_to_base64` 렌더 후 임시 파일에 저장 → 경로 주입.
  단일 이미지(`review_image`)는 원본 파일을 직접 Read(렌더 불필요, 원본 미삭제).

## 통합 테스트 — p11 (SoC DDR2(2)) before/after 정량

| 지표 | BEFORE (1차 Ollama) | AFTER (vision QA) | 판정 |
| :--- | ---: | ---: | :--- |
| 문자 수 | 6,818 | 10,071 | 검증 주석·확인표기 추가 |
| Region 커버리지 | 9 | 9 | **보존**(실콘텐츠 미파괴) |
| 핀 테이블 행 | 67 | 67 | **보존** |
| Designator 토큰 | 70 | 70 | **보존** |
| DDR_DQ 넷 | 48 | 48 | **보존** |
| `[unverified]` 표기 | 0 | **57** | 환각 의심 항목 플래그 |
| `[unreadable]` 표기 | 0 | **14** | 판독 불가 값 정직 표기 |
| verifier 소요 | — | 323.5s | (timeout 540s 내 완료) |

### 잡아낸 대표 환각/오류 사례 (image 대조로 교정)

1. **값 환각(C71) 교정 — 핵심 목표 사례**
   - BEFORE: `| C71 | 22uF/16V/2012 | SOC_DDR_VTO to GND |`
   - AFTER: `| C71 | `[unreadable]` | SOC_DDR_VTO to GND `[unverified]` |`
   - → 원본 이미지에서 값 확인 불가 → 환각된 `22µF`를 단정하지 않고 `[unreadable]` 표기.
     (C71 220↔22µF flip 클래스 = 프롬프트만으로 못 잡던 오류를 vision QA가 차단.)

2. **중복/fabrication 부품 제거**
   - 1차 decoupling 목록의 `C81`/`C82` 2회 중복 나열 → AFTER에서 명시적으로
     "duplicate listing of C81/C82 in the first pass removed". `C59`/`C89`도 3회→2회.

3. **범위 일반화 추정 차단**
   - R91–R114, decoupling 뱅크를 `[unverified range]` + "exact designator span /
     per-signal mapping not legible" 주석으로 표기 → C8-C60류 false 일반화 방지.

4. **누락 없이 확인 항목 분리 표기**
   - 판독 가능한 항목은 "confirmed"(타이틀 "SoC DDR2 (2)", "Keep Routing Guidelines",
     U24/U25/U26, 적/청 색부호)로 명시, 불확실 항목만 `[unverified]` → 커버리지 보존 +
     환각 억제 균형 달성.

### before/after 산출물 경로

- BEFORE(1차): `/tmp/phase3_p11_before.md`
- AFTER(vision QA): `/tmp/phase3_p11_after.md`
- 메타: `/tmp/phase3_p11.meta.json`
- 테스트/분석 스크립트(임시): `/tmp/phase3_test.py`, `/tmp/phase3_analyze.py`
- verifier에 전달된 원본 렌더 PNG(보존): `/tmp/visionqa_p11_*.png`

## 리스크 / 한계 / 관찰

1. **레이턴시**: 고밀도 회로도 1페이지(6.8K자) 교정 MD 전문 재생성에 OAuth
   스트리밍으로 **~324s** 소요. 첫 시도(timeout=300s)는 초과 → 안전 degrade(1차 유지)
   발동. 이에 기본 `VISION_QA_TIMEOUT`을 **600s**로 상향(env override 가능). 대량 배치
   시 페이지당 5분대 = 비용·시간 큼 → 회로도 등 고가치 페이지에 선택 적용 권장.

2. **DPI ↔ 검증 신뢰도 트레이드오프**: 220 DPI 렌더에서는 verifier가 미세 BGA 핀맵을
   "not legible at provided resolution"으로 보수적 `[unverified]` 처리. 이는 *정직한*
   거동(거짓 확신보다 안전)이나 과도 플래그 경향. **DPI 상향(`VISION_QA_DPI=300`)**으로
   confirm 비율을 높일 수 있음(후속 튜닝 권장).

3. **ToS 회색지대**: claude CLI 배치 자동화 verifier는 Anthropic ToS 회색지대.
   사용자 명시 승인 하 구현, **기본 OFF** 보존. 운영 자동화 정공법은
   `VISION_QA=claude_api`(messages 이미지 content block, 키 Keychain SSoT) — 모듈에
   `_review_via_api` TODO 스텁으로 분기 예약.

4. **안전성 검증 완료**: claude bin 부재/타임아웃 시 `review()`·`review_image()` 모두
   1차 MD 그대로 반환(corrected=False) + 경고 로그 → 파이프라인 무중단. 격리 테스트로
   확인.
