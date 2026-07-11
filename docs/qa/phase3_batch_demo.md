# Phase 3 — 실전 배치 데모 (VISION_QA_AUTO 페이지별 자동 티어링)

티어 대표 페이지(dense 1 + light 1)로 `VISION_QA_AUTO=1` 실전 배치를 실행해 자동
티어링·티어별 처리·최종 MD 품질을 검증한다.

## 실행 조건
- 소형 PDF `/tmp/tier_demo.pdf`(PICOMATRIX p7→light, p11→dense 추출, 2페이지)
- env: `VISION_QA=claude_cli VISION_QA_AUTO=1 VISION_QA_TIMEOUT=900`
- **완전 detached 실행**: macOS `setsid` 부재 → Python `os.setsid()`+`Popen(start_new_session=True)`로
  새 세션(PID 56716, PPID=1). 부모(에이전트) 종료 무관 완주. (1차 시도는 detached 미보장으로
  앙상블 중 중단 → 재실행으로 해소.)

## 자동 티어링·처리 결과 (로그)

| page | tier | 신호 | 처리 | netcheck |
| :--: | :--- | :--- | :--- | :--- |
| 1 | **light** | vec=772, desig=19 | 단일 vision QA(DPI 220) | vc=11 sp=12 vo=2 |
| 2 | **dense** | vec=3887, desig=57 | **앙상블 N=3 DPI=300** (success 3/3, confirmed=28, unverified=22, unreadable=0) | vc=10 sp=10 vo=1 |

- tier summary: `dense=1 light=1`, ensemble budget `1/10`(비용 가드 정상).
- AUTO 분류 정확: p1 `vec772<800` → light(AND/strong 미달), p2 `vec3887` → dense(strong_vector).
- 티어별 분기 정확: light=단일, dense=앙상블+DPI300. degrade 0(앙상블 3 run 전원 성공).

## 최종 MD 품질 (`output/pdf_md/tier_demo.md`, 18KB, 303줄)

| 항목 | 결과 | 판정 |
| :--- | ---: | :--- |
| `### Figure` 섹션 | 2 (둘 다 circuit schematic) | ✅ 구조 보존 |
| 청크 결합(`---`) | 14 | ✅ page1+page2 결합 |
| garbling(깨진 파이프) | 0 | ✅ C-1 무결 |
| designator 표행 / 연결행 | 50 / 24 | ✅ 핀맵 보존(C-2) |
| `[vector-confirmed]` | 21 (=p1 11 + p2 10) | ✅ netcheck 승격 |
| `[spurious?]` | 22 (=p1 12 + p2 10) | ✅ |
| `[unverified]` / `[unreadable]` | 60 / 4 | ✅ 환각 플래깅 |

플래그 합계가 페이지별 로그와 정확히 일치 — 파이프라인이 1차→vision QA→netcheck 끝까지 정상.

## 3단 신뢰도 실전 증거
```
| C2 [unverified] | SPI_CSn_C1 | [vector-confirmed]
```
vision이 확신 못한(`[unverified]`) C2를 net_tracer 벡터가 `SPI_CSn_C1` 넷으로 결정적 확정.
+ `C30-C33 [unverified] — value [unreadable]`(앙상블 환각 가드), `U17/U18/U20 [ordering
suffix unverified]`(부품번호 suffix 환각 거부) — vision 확률적 → 앙상블 → net_tracer 결정적
3단 신뢰도가 한 문서에서 모두 작동.

## 결론
- **AUTO 티어링 실전 정상**: 페이지 자동 분류 → 티어별 처리(light 단일 / dense 앙상블+DPI300)
  → netcheck 교차검증 → 형식 보존 MD. 비용 가드·degrade 안전.
- **MD 품질 우수**: 형식 계약 완전 보존, garbling 0, 3단 신뢰도 플래그 정상.
- **운영 검증 완료**: `VISION_QA=claude_cli VISION_QA_AUTO=1`로 고밀도 회로도만 앙상블,
  저밀도는 단일 처리하여 비용·품질 최적 균형. 실배치 투입 가능.

## 운영 주의
- detached 실행 시 macOS는 `os.setsid()`+`start_new_session=True` 필요(`setsid` 명령 부재).
- 대량 배치는 앙상블 페이지(`VISION_QA_MAX_ENSEMBLE_PAGES`)·timeout 모니터링 권장.
