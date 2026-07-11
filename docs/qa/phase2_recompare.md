# Phase 2 — 강화 FIGURE_RULES 효과 실측 재비교

회로도(Schematic) 전사 강화 프롬프트(`extract_all_via_pdf.py` `FIGURE_RULES`,
REGION INVENTORY + 핀별 PIN→NET + BGA(Ball Grid Array) ball 좌표 보존 + 값/부품번호
환각가드 + 동일값 그룹화 + 커버리지 리마인더)의 효과를 **강화 전(baseline)** 대비
정량 측정한다.

## 측정 조건 (동일 변수 고정)

| 항목 | 값 |
| :--- | :--- |
| Provider | `ollama_cloud` (gemini-3-flash-preview, 로컬 게이트웨이 `http://localhost:11434/v1`) |
| 게이트웨이 생존 | OK (`/v1/models` 200, gemini-3-flash-preview:latest 응답) |
| 테스트 PDF | `/Users/heni/workspace/04_NX/01_raw/schematics/PICOMATRIX_NG-ULTRA_EMV_REV01(230103).pdf` (42p) |
| 테스트 페이지 | p11 (SoC DDR2(2), BGA SDRAM ×3), p7 (FPGA & SoC JTAG, 커넥터/스위치) |
| 렌더 DPI | 220 (baseline 동일) |
| max_tokens | 8192 |
| baseline 출처 | `/tmp/exp_p{11,7}_baseline.md` (직전 실험, 강화 전 프롬프트) |
| enhanced 출처 | `/tmp/phase2_p{11,7}_enhanced.md` (현재 코드 `FIGURE_RULES` 경로로 신규 추출) |

> baseline 재현 대신 직전 실험 `/tmp/exp_*_baseline.md`(강화 전 프롬프트 산출물)와
> 현재 코드 경로 enhanced 산출물을 대조 — 측정 변수(provider/DPI/page/max_tokens) 동일.

## 정량 결과 — p11 (SoC DDR2(2), BGA SDRAM ×3)

| 지표 | baseline (강화 전) | enhanced (현재) | 변화 |
| :--- | ---: | ---: | :--- |
| 문자 수 | 4,537 | 6,818 | +50% |
| Region(서브회로) 커버리지 | 0 | 9 | **0 → 9** |
| Region inventory 항목 | 0 | 9 | **0 → 9** |
| BGA ball 좌표 총 포착 수 | 3 | 86 | **3 → 86** |
| BGA ball 기준 5-set 포착률 (M8/L2/G8/F7/E8) | 0/5 | 5/5 | **0/5 → 5/5** |
| 핀 테이블 행(PIN→NET) | 0 | 67 | **0 → 67** |
| Designator 토큰 수 | 17 | 70 | **17 → 70** |
| DDR_DQ 데이터 넷 포착 | 6 | 48 | **6 → 48** (전체 DQ48–DQ95 버스) |

**해석**: 직전 실험 결론(p11 region 1→14, ball 0/5→5/5)을 **현재 코드 경로로 재현**.
강화 프롬프트가 (a) 서브회로 열거(0→9 region), (b) BGA ball 좌표 보존(0/5→5/5,
총 3→86), (c) 핀별 1:1 전사(0→67행), (d) 데이터 버스 전수 포착(6→48 DQ넷)을 결정적으로
개선함이 정량 확인됨. 동일 핀맵 IC(U25A/U26A)는 "identical to U24A except…"로 압축되어
토큰 폭주 없이 커버리지 확보(V3 truncate 역효과 회피).

## 정량 결과 — p7 (FPGA & SoC JTAG, 커넥터/스위치/MICTOR)

| 지표 | baseline (강화 전) | enhanced (현재) | 변화 |
| :--- | ---: | ---: | :--- |
| 문자 수 | 3,615 | 5,805 | +61% |
| Region inventory 항목 | 0 | 9 | **0 → 9** |
| Figure/Region 섹션 | 1 | 9+ | **1 → 9+** |
| Designator 토큰 수 | 29 | 35 | +21% |
| BGA ball 좌표 | 0 | 0 | (해당 없음 — JTAG 페이지는 BGA 미포함, 정상) |

**해석**: BGA가 없는 커넥터 중심 페이지에서도 region inventory(0→9)와 커넥터 per-pin
표(J8 26핀, CON2/CON3 MICTOR, U15/U16 스위치, R/C 값 전수)가 신규 확보됨. baseline은
단일 Figure로 요약 붕괴되어 서브회로·노트·타이틀블록을 누락했으나 enhanced는 9개 영역을
개별 전사. ball 5-set 0/5는 BGA 부재에 따른 정상값(회귀 아님).

## 한계 — 프롬프트만으로 못 잡는 잔존 오류 (→ Phase 3 vision QA 대상)

강화 프롬프트는 **커버리지·좌표·구조**를 개선하지만, 1차 모델(gemini-3-flash-preview)의
시각 한계에서 비롯되는 다음 오류는 프롬프트만으로 제거 불가:

1. **값 환각**: C71 값이 run마다 `22µF ↔ 220µF`로 flip (baseline·enhanced 모두 잔존).
   이미지 재판독 없이는 진위 판정 불가.
2. **inventory 내 중복/fabrication**: enhanced p11 Region 7/8 decoupling 목록에서
   `C81`/`C82`가 중복 등장(동일 부품 2회 나열) — 실제 부품 수 과대.
3. **범위 일반화 추정**: 본 PDF에서는 경미하나, 동일 모델이 다른 회로도(sound-cam
   `StampS3_verify.md`)에서 `C8-C60 = 100nF (all 53 capacitors)`, `JP1-JP120 = 2-pin
   header`, `JP40-JP120 → VDD_3V3` 같은 과도 일반화·추정 환각을 생성한 전례 존재.

→ 이 (1)(2)(3) 클래스가 **Phase 3 Claude vision 2차 QA**의 교정 대상이다.
   (원본 이미지를 다시 보는 독립 검증자만 잡을 수 있음 — pdf-to-kicad 스킬
   `stage4_verification.md`의 'AGENT INDEPENDENCE' 원칙과 동일.)

## 산출물 경로

- baseline: `/tmp/exp_p11_baseline.md`, `/tmp/exp_p7_baseline.md`
- enhanced: `/tmp/phase2_p11_enhanced.md`, `/tmp/phase2_p7_enhanced.md`
- 추출 스크립트(임시): `/tmp/phase2_extract.py` (현재 코드 `extract_chunk` 직접 호출)
- 분석 스크립트(임시): `/tmp/phase2_analyze.py`
