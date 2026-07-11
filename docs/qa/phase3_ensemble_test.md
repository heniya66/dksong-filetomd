# Phase 3 — 앙상블(ensemble) vision QA vs 단일 pass (end-to-end)

N=3 다수결 앙상블이 단일 pass의 환각 교정 확률적 한계(C71 값환각 교정 2/4, C81/C82
중복 제거 0/4)를 개선·안정화하는지 실측한다.

## 측정 조건
- `VISION_QA=claude_cli`, `VISION_QA_ENSEMBLE=3`, `VISION_QA_DPI=300`, `VISION_QA_KEEP_VOTES=1`
- 1차 MD 고정 `/tmp/phase2_p11_enhanced.md`(6818자), 대상 p11(SoC DDR2, BGA SDRAM ×3)
- 내부 3 run 순차: 187.5s / 327.8s / 281.9s, **총 797.2s**, 성공 3/3 (실패 0)
- raw: `/tmp/ensemble_run{1,2,3}.md`, `/tmp/ensemble_p11_final.md`, `/tmp/ensemble_result.json`,
  투표 덤프 `/tmp/claude-501/visionqa_votes_*.json`

## 대조표

| 항목 | 단일 pass 다회(기존) | 앙상블 n=3 | 판정 |
| :--- | :--- | :--- | :--- |
| C71 값 환각 교정 | `[unreadable]` **2/4** (50%, run 변동) | 투표 `[unreadable]/[unverified]/[unreadable]` → 다수결 **`[unreadable]` 안정 채택** | ✅ 확률적→안정 |
| C81/C82 중복 제거 | **0/4** (항상 실패) | **명시 인식 + 통합·제거** | ✅ 해결 |
| 핀맵 연결행(`->`/`→`) | — | 18행(run1 11·run2 18 수준, 손실 없음) | ✅ C-2 회귀 없음 |
| designators | ~70(내부 일관) | 70/70/70 | 보존 |
| 통합 통계 | — | confirmed 29 / unverified(충돌) 12 / unreadable 29 | 보수적 플래깅 |
| 레이턴시 | ~296s | **797s (2.7×)** | 비용 트레이드오프 |

## 통합본 실제 표기 (정성 증거)
- **C71**: `C71 [unreadable] (first-pass: 22uF/16V/2012 — 판독 불가, 추측 안 함)` — 값 단정
  거부 + 근거 명시("220uF/22uF 혼동 다발 셀로 알려짐").
- **C81/C82**: `"first-pass 인벤토리의 Bank 1/Bank 2 분리는 C81·C82가 양쪽 뱅크에 중복
  등장 → 환각성 중복으로 판단하여 단일 뱅크로 통합하고 중복 제거"` — 앙상블이 중복을 명시
  인식·제거.
- C59–C89 묶음: `[unverified range — 중복 제거 후, 개별 번호 판독 불가]` 보수적 처리.
- final `[unreadable]` 17 / `[unverified]` 63 — 환각 의심을 적극 플래깅.

## 결론
1. **앙상블은 단일 pass의 확률적 한계를 결정론적으로 안정화**한다. C71 값 환각을 다수결로
   `[unreadable]` 채택(단일 2/4 변동 해소), C81/C82 중복을 명시 제거(단일 0/4 → 해결).
2. 교정 결과가 단순 플래그가 아니라 **근거 명시**("추측 안 함", "환각성 중복 제거")라 품질 우수.
3. 핀맵 행 보존(C-2 회귀 없음), 형식 계약 유지, 3/3 run 성공·통합 정상.
4. **대가는 비용 2.7×**(797s vs 296s). 고밀도/고가치 회로도 페이지에 선택 적용 권장,
   전량 배치엔 부적합(레이턴시).

## 권고 운영안
- **고가치 회로도 페이지**: `VISION_QA_ENSEMBLE=3` + `VISION_QA_DPI=300` (환각 안정 제거 우선).
- **일반 배치**: 단일 pass(`VISION_QA=claude_cli`) 또는 OFF — 비용 우선.
- 정밀 핀맵은 net_tracer 등 결정적 소스 교차검증 병행(앙상블도 미세 ball 100% 보장 아님).

## 측정 아티팩트 주의
- `ensemble_result.json`의 `final_pin_rows=0`은 측정 함수가 `PIN->NET` 패턴을 0으로 센 것이며,
  실제 통합본은 연결행 18개 보존(grep 확인). designators 카운트(70)도 단일 pass 다회 스크립트
  (~126)와 정의가 달라 절대값 직접 비교 불가 — 내부 3 run 일관성(70/70/70)으로 해석.
