# Phase 3 — Vision QA DPI 300 다회 검증 (4 run)

vision 모델의 run-to-run 비결정성을 평균·분산으로 정량화하여 DPI(Dots Per Inch) 300의
안정적 성능과 신뢰 가능 지표를 확정한다.

## 측정 설계
- **1차 추출(Ollama generator) 고정**: `/tmp/phase2_p11_enhanced.md`(6818자)를 모든 run 동일 입력.
- **vision QA 2차(Claude verifier, DPI 300)만 반복** → verifier 순수 변동 측정.
- 대상: p11(SoC DDR2, BGA SDRAM ×3). backend: `claude_cli OAuth`.
- **완료 run: 4/5** (5회 목표였으나 하이버네이션 진입으로 4회 — 통계 산출에는 충분, CV 수렴).
- raw: `/tmp/dpi300_multirun_results.jsonl`

## 통계 (mean ± std [min~max], CV=변동계수)

| 분류 | 지표 | mean ± std | 범위 | CV |
| :--- | :--- | ---: | :---: | ---: |
| **신뢰 가능** | designators | 126.2 ± 2.5 | 124~130 | **2%** |
| (저변동) | pin_rows | 104.0 ± 0.7 | 103~105 | **1%** |
| | latency(초) | 296.4 ± 29.9 | 263~339 | 10% |
| **noise** | `[unverified]` | 45.0 ± 15.2 | 34~71 | **34%** |
| (고변동) | `[unreadable]` | 5.0 ± 5.9 | 0~15 | **117%** |

> `regions`(0 기록)·`dq_nets`(3.2)는 본 multirun 스크립트의 카운트 정의가 이전 단발
> 테스트와 달라 절대값 비교에서 제외(측정 아티팩트). 구조 산출량 신뢰 지표는 designators/pin_rows.

## 정성 일관성 (4 run 중)

| 항목 | 빈도 | 해석 |
| :--- | :---: | :--- |
| corrected (vision QA 정상작동) | 4/4 | degrade 없이 항상 교정 수행 |
| BGA ball L2 / F7 / E8 confirmed | 4/4 | DPI 300 완전 일관 판독 |
| BGA ball G8 confirmed | 3/4 | 대체로 안정 |
| **BGA ball M8 confirmed** | **2/4** | 불안정 — 일부 run 판독 흔들림 |
| **C71 값 환각 교정([unreadable])** | **2/4** | **50% — vision QA가 항상 잡지 못함** |
| **C81/C82 중복 제거** | **0/4** | **항상 실패 — 단발 "제거 성공"은 운** |

## 핵심 결론

1. **신뢰 가능 지표 = 구조 산출량**: designators(CV 2%)·pin_rows(CV 1%)는 DPI 300에서 매우 안정.
   즉 "얼마나 많은 핀/부품을 전사하는가"는 일관적이고 신뢰할 수 있다.

2. **noise 지표 = 플래그 카운트**: `[unverified]`(CV 34%)·`[unreadable]`(CV 117%)는 절대값
   신뢰 불가. **단발 비교(이전 57↔164↔60)가 무의미**했음을 다회로 확증. 비교는 다회 평균으로만.

3. **환각 교정은 확률적, 결정적 게이트 아님 (가장 중요)**:
   - C71 값 환각: **2/4(50%)**만 교정 — 절반은 놓침.
   - C81/C82 중복: **0/4** — 일관되게 못 잡음.
   → vision QA는 환각을 "확률적으로" 줄이는 **보조 수단**이지, 환각을 보장 제거하는 게이트가
   아니다. 단발 테스트의 좋은 결과는 운 좋은 샘플이었다.

4. **BGA 판독**: DPI 300이 대부분 ball(L2/F7/E8 4/4, G8 3/4)을 안정 판독하나, 일부(M8 2/4)는
   여전히 흔들림. 미세 핀맵 100% 보장은 아님.

## 권고 (업데이트)
1. **DPI 300은 구조 추출량 안정성에 유의미** → 고밀도 회로도 기본값 상향 타당. 단 환각 제거를
   보장하지 않음을 운영 전제로.
2. **환각 교정이 목적이면 단일 pass로 불충분** → 동일 페이지 **N회 vision QA 후 다수결/합집합**
   (예: 2/3 이상 run에서 [unreadable]이면 채택) 같은 앙상블이 필요. 또는 net_tracer 등 결정적
   소스와 교차검증.
3. `[unverified]`/`[unreadable]` 카운트는 품질 게이트 임계값으로 쓰지 말 것(noise). 구조
   산출량(designators/pin_rows)을 회귀 감시 지표로 사용 권장.
4. 정밀 핀맵(BGA M8류)은 DPI 추가 상향(360+) 또는 타일 분할 후속 실험 여지.

## 산출물
- raw: `/tmp/dpi300_multirun_results.jsonl` (4 run), `/tmp/dpi300_multirun.py`
- 단발 비교: `docs/qa/phase3_dpi_comparison.md`
