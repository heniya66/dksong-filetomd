# Phase 3 Net Crosscheck(넷 교차검증) 실효성 측정 보고서

**작성일**: 2026-06-03
**대상**: `PICOMATRIX_NG-ULTRA_EMV_REV01(230103).pdf` page 11 (SoC DDR2(Double Data Rate 2) (2))
**입력 MD(Markdown)**: `/tmp/ensemble_p11_final.md` (앙상블 최종본 — `[unverified]` 63건·`[unreadable]` 17건 포함)
**교차검증 모듈**: `lib/net_crosscheck.crosscheck()`
**net_tracer(넷 트레이서) 인터프리터**: `~/.claude/skills/pdf-to-kicad/.venv/bin/python`

---

## 1. 입력 요약

| 항목 | 값 |
|------|-----|
| vision MD(마크다운) 라인 수 | 248 |
| `[unverified]` 토큰 수 (교차검증 승격 대상) | 63 |
| `[unreadable]` 토큰 수 (교차검증 승격 대상) | 17 |
| PDF(Portable Document Format) 경로 | `/Users/heni/workspace/04_NX/01_raw/schematics/PICOMATRIX_NG-ULTRA_EMV_REV01(230103).pdf` |
| 대상 페이지 | 11 |

---

## 2. net_tracer(넷 트레이서) 산출 통계

net_tracer가 page 11을 벡터 분석하여 산출한 넷리스트:

| 항목 | 값 |
|------|-----|
| 분석 라인 수 | 2,477 |
| 텍스트 요소 수 | 1,193 |
| 총 넷 수 (`total_nets`) | 259 |
| named net(명명 넷) 수 (`named_nets`) | 14 |

> 총 넷 259개 중 명명 넷은 14개. 나머지 245개는 `Net_`/`Pin_` 등 아티팩트 접두 넷 또는 익명 연결로 `named_nets` 집합에서 제외됨.

---

## 3. CrosscheckResult(교차검증 결과) 요약

| 지표 | 값 | 설명 |
|------|----|------|
| `applied` | **True** | 벡터 PDF(Portable Document Format) 정상 처리 — 교차검증 실제 적용됨 |
| `vector_confirmed` | **16** | `[unverified]`/`[unreadable]` 라인이 `[vector-confirmed]`로 승격된 수 |
| `spurious_flagged` | **13** | vision 검출 designator(지정자)가 net_tracer 미검출 → `[spurious?]` 부착 수 |
| `suppressed_prefixes` | **["U"]** | U-prefix(IC 계열) spurious 전량 억제 (C1 blind-spot 완화 규칙) |
| `mixed_lines` | **6** | C2 모순 억제: confirmed+spurious 동시 발생 → spurious 억제된 라인 수 |
| `vector_only_nets` | **1** | net_tracer named net 중 vision MD에 없는 넷 (`LDM`) |
| 입력 라인 수 | 248 | |
| 출력 라인 수 | 248 | diff = 0 (라인 자동삭제 0건 — 형식 보존 완전) |

---

## 4. 형식 보존 검증

- **라인 수 불변**: 입력 248 → 출력 248, diff = **0** (자동삭제·재배열 없음)
- `### Figure` 헤더 섹션 구조 유지 확인
- `PIN -> NET` 테이블 행 구조 유지 확인
- `\n\n---\n\n` 청크 결합 구조 유지 확인
- 플래그 토큰은 기존 라인 **끝에만** 부착, 본문 내용 수정 없음

---

## 5. 정성(定性) 샘플 분석

### 5-1. `[vector-confirmed]` 승격 샘플 (R/C/D designator 중심)

| 라인 번호 | 승격 내용 | 비고 |
|-----------|-----------|------|
| L19 | `R91 [unverified]-R113` Termination Resistor Bank | R-prefix net_tracer 검출 → 존재 확정 |
| L21 | `C71 [unreadable]` Bulk Decoupling Capacitor | C-prefix net_tracer 검출 → 존재 확정 |
| L60 | `R3 \| NC_R3 [unverified]` PIN→NET 행 | R3 벡터 refpin 일치 |
| L61 | `R7 \| NC_R7 [unverified]` PIN→NET 행 | R7 벡터 refpin 일치 |
| L62 | `R8 \| NC_R8 [unverified]` PIN→NET 행 | R8 벡터 refpin 일치 |
| L170 | `R91-R103 [unverified range]` 어레이 행 | 범위 행 전체 승격 |
| L171 | `R104-R106 [unverified range]` 어레이 행 | 범위 행 전체 승격 |
| L184 | C59–C89 콘덴서 뱅크 설명 행 | C-prefix 검출 + named net 일치 |
| L188 | `C71 [unverified]` SOC_DDR_VTO → GND 행 | refpin 또는 named net 일치 |
| L189 | C59/63/64/66/67/68/70/72/73/74/78/79/80/81/82/83/84/85/89 범위 행 | 대규모 콘덴서 뱅크 존재 확정 |

**총 16라인 승격** — 모두 R(저항) 또는 C(커패시터) designator 행. D(다이오드) designator는 본 페이지에 없음.

### 5-2. U-prefix 억제 확인 (C1 blind-spot 완화 규칙)

`suppressed_prefixes = ["U"]`

net_tracer는 page 11에서 **U-prefix ref를 0개** 산출. 이는 DDR2 SDRAM(Static Random-Access Memory) IC(Integrated Circuit)(U24~U26)가 BGA(Ball Grid Array) 볼 넷 형식으로 표현되어 일반 ref 대신 `_NET_LABEL_` sentinel(파수꾼) 또는 익명 연결로 처리된 결과다. C1 규칙이 이를 blind-spot으로 인식, U-prefix designator의 `[spurious?]` 플래그를 **전량 억제**하여 IC 행의 거짓 spurious(가짜 오류) 오염을 방지했다. U-prefix에 대한 `[spurious?]` 부착 = **0건** (억제 완전 동작).

### 5-3. `[spurious?]` 플래그 샘플 5건

| 라인 번호 | 내용 | 분석 |
|-----------|------|------|
| L20 | `R114` Differential Clock Termination | net_tracer 미검출 — 차동 클럭 종단 저항, 텍스트-only 가능성 |
| L26 | first-pass 인벤토리 주석 행 (C81·C82 중복) | 주석 행에 C81·C82 포함 → designator 인식 후 미검출 플래그 |
| L78 | `J2 \| 0V9_SVREF` PIN→NET 행 | J-prefix 미검출 — 전원/접지 핀 텍스트-only 가능성 |
| L80 | `C1 \| 1V8(VDDQ)` PIN→NET 행 | C1이 net_tracer refs에 없음 — 소형 decoupling cap(디커플링 커패시터) |
| L175 | `R110 [unverified]` DDRB_CASn 어레이 행 | R107~R109 승격됐으나 R110 미검출 — 아티팩트/경계 누락 가능성 |

> 주목: **주석 행(L26)에 spurious 부착**은 false positive(위양성) 유사 현상. 단, 자동삭제 없고 플래그만이므로 안전. 개선 여지 있음.

### 5-4. `vector_only_nets` — vision MD 미등장 넷

| net name | 비고 |
|----------|------|
| `LDM` | net_tracer 검출, vision MD에 없는 named net. DDR2 Data Mask 신호 계열 추정 |

1건으로 vision MD의 넷 커버리지는 net_tracer named net(14개) 중 13/14 = **93%** 수준.

---

## 6. 결론 및 권고

### 6-1. 교차검증 실질 가치 (확정 보강 vs 노이즈)

**확정 보강 효과 (positive)**

- `[unverified]`/`[unreadable]` 63+17 = 80건 후보 중 **16건(20%)을 `[vector-confirmed]`로 승격**. 벡터 근거로 "존재 확정"된 R/C 부품 및 넷 연결이 생성됨.
- U-prefix IC(DDR2 SDRAM) 행 전체에 대해 **거짓 spurious 0건** — C1 억제 규칙이 완전 동작하여 IC 행 오염 없음.
- C2 혼합 억제 6건 처리로 모순 토큰 동시 부착 방지.
- 라인 수 불변(자동삭제 0) — 형식 계약 완전 보존.

**노이즈 요소 (주의)**

- `spurious_flagged` 13건 중 **주석/설명 행 spurious 부착** (예: L26 first-pass 주석 행)은 false positive 유사. designator 토큰이 서술 문맥에 등장해도 인식되는 한계.
- net_tracer named net이 14개(총 259 중 5%)에 불과 — DDR2 어드레스·데이터 버스 넷(DDRB_A0 등)이 대부분 `Net_` 아티팩트 접두로 분류되어 `[vector-confirmed]` 승격 기회가 제한됨.

### 6-2. U-recall(U-prefix 재현율) 한계의 실제 영향

page 11에서 U24~U26 DDR2 SDRAM IC의 ref는 net_tracer가 **전혀 검출 못함** (U-prefix refs = 0). BGA 볼 연결이 `_NET_LABEL_` sentinel로 처리되는 구조적 한계다. C1 규칙이 이를 slient degrade(조용한 성능 저하)로 처리(억제)하여 **IC 행 오염은 없지만 IC 핀-넷 행의 `[unverified]`는 승격 불가**. DDR2 BGA 페이지의 핵심 정보(핀 매핑)가 교차검증 사각지대에 있음 — 이 한계는 net_tracer 아키텍처 레벨의 이슈이며 net_crosscheck 로직으로는 해소 불가.

### 6-3. 적용 권고

| 항목 | 권고 |
|------|------|
| **적용 여부** | 권고(RECOMMENDED). applied=True 확인, 형식 보존 완전, 거짓 spurious 억제 동작 |
| **R/C 종단 어레이 페이지** | 효과 최대 — net_tracer가 저항/커패시터 ref를 다수 검출하므로 `[unverified]` 행 승격률 높음 |
| **BGA IC 중심 페이지** | 제한적 — U-prefix recall 0으로 IC 핀 매핑 행은 승격 불가. 그러나 C1 억제로 오염도 없음 |
| **주석 행 spurious 개선** | 향후: `_scan_designators` 호출 전 주석/인용 행(`>` 시작) 필터링 고려 |
| **`LDM` 넷 추가 검토** | vision MD에 `LDM` 넷 미등장 — 누락 가능성 검토 권고 |

---

## 7. 산출 파일

| 파일 | 설명 |
|------|------|
| `/tmp/crosschecked_p11.md` | 교차검증 토큰 부착된 최종 MD(248라인, 자동삭제 0) |
| `/tmp/netcheck_test.py` | 실행 스크립트 |
| `/tmp/netcheck_test_out.txt` | 실행 로그 (summary JSON(JavaScript Object Notation) + 샘플 출력) |
