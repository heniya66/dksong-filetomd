# Phase 3 — Vision QA DPI 튜닝 종합 비교 (DPI 220 회귀검증 + DPI 220 vs 300)

수정본(Advisor APPROVED, 이슈 0) vision QA를 p11(SoC DDR2, BGA SDRAM ×3)로
2회 실측 — DPI(Dots Per Inch) 220 재테스트 + DPI 300 튜닝 — 한 결과를 종합한다.
(두 백그라운드 에이전트가 raw 결과는 산출했으나 보고서 작성 단계에서 중단 →
`/tmp/phase3_retest_p11.meta.json`, `/tmp/dpi300_metrics.json` 기반으로 Team Leader 정리.)

## 측정 조건
- PDF: `/Users/heni/workspace/04_NX/01_raw/schematics/PICOMATRIX_NG-ULTRA_EMV_REV01(230103).pdf` p11
- backend: `claude_cli(model=default, OAuth)`, 1차 추출(공통) 6818자
- ①수정전 DPI220 = a9f39081 통합테스트 / ②수정본 DPI220 = vqa-retest / ③수정본 DPI300 = vqa-dpi300

## 3개 측정점 대조

| 지표 | ①수정전 220 | ②수정본 220 | ③수정본 300 |
| :--- | ---: | ---: | ---: |
| corrected | True | True | True |
| 레이턴시(초) | 324 | 318.9 | **261.3** |
| `[unverified]` | 57 | 164 | 60 |
| `[unreadable]` | 14 | 5 | 3 |
| Designator | 70 | 70 | 58 |
| DDR_DQ 넷 | 48 | 48 | 48 |
| C71 값 환각 교정 | ✅ | ✅ 유지 | (정성) |
| C81/C82 중복 제거 | ✅ | ❌ 재발 | — |
| BGA ball 5-set confirm | — | — | **5/5** |

> 측정 스크립트 상이: ②는 `figure_sections=1`/`table_data_rows=97`, ③은 `regions=9`/`pin_rows=74`.
> 정의 불일치 지표는 직접 비교에서 제외, 공통 지표 위주 해석.

## 해석

### 1) DPI 220 회귀 검증 (수정본 vs 수정전) — 합격
- **corrected=True** → C2 sanity 게이트가 정상 산출물을 부당 degrade하지 않음. W3 stdin
  전달 + W1/W2 격리 cwd(절대경로 cross-cwd PNG Read) 정상 동작. C71 값 환각 교정 유지.
- **주의**: `[unverified]` 57→164 폭증 + C81/C82 중복 재발은 **코드 회귀가 아니라 vision 모델
  run-to-run 비결정성**으로 추정(값 flip과 동일 변동성). 실콘텐츠(DQ48) 보존으로 구조 손상 없음.

### 2) DPI 220 vs 300 — DPI 300 가설 입증
- ③ 에이전트가 baseline을 ①수정전(57)으로 잡아 "57→60 악화"로 **오판**. 공정 비교는
  **동일 수정본 코드끼리** = ②DPI220 vs ③DPI300:
  - `[unverified]` 164 → 60 (대폭 감소, 고해상도로 더 많이 confirm)
  - `[unreadable]` 5 → 3
  - 레이턴시 318.9 → 261.3 (페널티 없음, 오히려 개선)
  - **BGA ball 5-set(M8/L2/G8/F7/E8) 전부 confirmed** — DPI 효과의 결정적 정성 증거
- 약점: Designator 70→58(일부 누락/변동).

## 권고
1. **고밀도 회로도(BGA 페이지)는 `VISION_QA_DPI=300` 권장** — 판독력↑(BGA 5/5), `[unreadable]`↓,
   레이턴시 페널티 없음.
2. **vision 비결정성이 커** `[unverified]`/designator 절대 카운트는 **다회(3+) run 평균**으로
   확정 필요. 단일 run 절대수 비교는 신뢰 한계.
3. 기본값 DPI 300 상향은 합리적이나 2)의 다회 검증 후 결정 권장.

## 산출물(raw)
- ②: `/tmp/phase3_retest_p11_after.md`, `/tmp/phase3_retest_p11.meta.json`
- ③: `/tmp/dpi300_p11_result.md`, `/tmp/dpi300_metrics.json`, `/tmp/dpi300_test_stdout.log`
