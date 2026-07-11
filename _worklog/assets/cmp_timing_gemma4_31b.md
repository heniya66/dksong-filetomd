제공된 이미지는 **MP34DT01-M** 부품의 기술 문서 중 **'Acoustic and electrical specifications(음향 및 전기적 사양)'** 섹션의 일부입니다. 해당 페이지의 내용을 한국어로 상세히 정리한 결과입니다.

---

# MP34DT01-M 기술 문서 정리

## 2.3 타이밍 특성 (Timing characteristics)

이 섹션에서는 장치의 동작에 필요한 시간 및 주파수 관련 파라미터를 정의합니다.

### 표 5. 타이밍 특성 (Table 5. Timing characteristics)

| 파라미터 (Parameter) | 설명 (Description) | 최소값 (Min.) | 최대값 (Max.) | 단위 (Unit) |
| :--- | :--- | :---: | :---: | :---: |
| $f_{CLK}$ | 일반 모드에서의 클록 주파수 (Clock frequency for normal mode) | 1 | 3.25 | MHz |
| $f_{PD}$ | 전원 다운 모드에서의 클록 주파수 (Clock frequency for power-down mode) | - | 0.23 | MHz |
| $T_{CLK}$ | 일반 모드에서의 클록 주기 (Clock period for normal mode) | 308 | 1000 | ns |
| $T_{R\_EN}$ | L/R 핀 = 1일 때, DATA 라인에서 데이터 활성화 (Data enabled on DATA line, L/R pin = 1) | $18^{(1)}$ | - | ns |
| $T_{R\_DIS}$ | L/R 핀 = 1일 때, DATA 라인에서 데이터 비활성화 (Data disabled on DATA line, L/R pin = 1) | - | $16^{(1)}$ | ns |
| $T_{L\_EN}$ | L/R 핀 = 0일 때, DATA 라인에서 데이터 활성화 (Data enabled on DATA line, L/R pin = 0) | $18^{(1)}$ | - | ns |
| $T_{L\_DIS}$ | L/R 핀 = 0일 때, DATA 라인에서 데이터 비활성화 (Data disabled on DATA line, L/R pin = 0) | - | $16^{(1)}$ | ns |

**(1) 주석:** 설계 시뮬레이션 결과로부터 도출된 값임 (From design simulations).

---

### 그림 3. 타이밍 파형 (Figure 3. Timing waveforms)

이 다이어그램은 클록 신호($CLK$)와 두 개의 데이터 출력 신호($PDM\ R$, $PDM\ L$) 간의 시간적 관계를 보여줍니다.

#### 1. 포함된 신호
*   **CLK**: 시스템 클록 신호.
*   **PDM R**: 우측 채널 PDM 데이터 출력.
*   **PDM L**: 좌측 채널 PDM 데이터 출력.

#### 2. 타이밍 파라미터 및 관계 분석
파형도에 표시된 각 파라미터의 정의와 동작 순서는 다음과 같습니다.

*   **$T_{CLK}$ (Clock Period)**: $CLK$ 신호의 한 주기 시간입니다.
*   **PDM R 채널 동작**:
    *   **활성화 ($T_{R\_EN}$)**: $CLK$ 신호가 **상승 엣지(Rising Edge)**일 때, 일정 시간($T_{R\_EN}$) 후에 $PDM\ R$ 신호가 High Z(하이 임피던스) 상태에서 벗어나 데이터 출력을 시작합니다.
    *   **비활성화 ($T_{R\_DIS}$)**: $CLK$ 신호가 **하강 엣지(Falling Edge)**일 때, 일정 시간($T_{R\_DIS}$) 후에 $PDM\ R$ 신호가 다시 High Z 상태로 돌아갑니다.
*   **PDM L 채널 동작**:
    *   **활성화 ($T_{L\_EN}$)**: $CLK$ 신호가 **하강 엣지(Falling Edge)**일 때, 일정 시간($T_{L\_EN}$) 후에 $PDM\ L$ 신호가 High Z 상태에서 벗어나 데이터 출력을 시작합니다.
    *   **비활성화 ($T_{L\_DIS}$)**: $CLK$ 신호가 **상승 엣지(Rising Edge)**일 때, 일정 시간($T_{L\_DIS}$) 후에 $PDM\ L$ 신호가 다시 High Z 상태로 돌아갑니다.

#### 3. 요약
*   $PDM\ R$과 $PDM\ L$은 서로 교차하여 활성화됩니다.
*   **CLK 상승 $\rightarrow$ PDM R 활성화 / PDM L 비활성화**
*   **CLK 하강 $\rightarrow$ PDM L 활성화 / PDM R 비활성화**

---
**문서 정보:**
*   페이지 번호: 6/17
*   문서 번호: DocID026514 Rev 3
*   제조사 로고: STMicroelectronics (ST)
*   그림 식별자: AM045165v1