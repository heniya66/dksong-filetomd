### Figure 1: Timing characteristics (table)
**한 줄 요약 (쉬운 말 / one-line plain summary)**: 이 표는 장치의 시계 주파수와 데이터 활성화/비활성화 시간 등 타이밍 관련 규격을 정리한 것입니다.  
**Type**: table-image  
**Caption (original)**: Table 5. Timing characteristics  

**Components / Elements**:  
- **Parameter 열**: f_CLK, f_PD, T_CLK, T_R,EN, T_R,DIS, T_L,EN, T_L,DIS  
- **Description 열**:  
  - "Clock frequency for normal mode" (f_CLK)  
  - "Clock frequency for power-down mode" (f_PD)  
  - "Clock period for normal mode" (T_CLK)  
  - "Data enabled on DATA line, L/R pin = 1" (T_R,EN)  
  - "Data disabled on DATA line, L/R pin = 1" (T_R,DIS)  
  - "Data enabled on DATA line, L/R pin = 0" (T_L,EN)  
  - "Data disabled on DATA line, L/R pin = 0" (T_L,DIS)  
- **Min. 열**: 1, [blank], 308, 18⁽¹⁾, [blank], 18⁽¹⁾, [blank]  
- **Max. 열**: 3.25, 0.23, 1000, [blank], 16⁽¹⁾, [blank], 16⁽¹⁾  
- **Unit 열**: MHz, MHz, ns, ns, ns, ns, ns  
- **Footnote (1)**: "From design simulations"  

**Relations / Connections**:  
- 표는 파라미터별 최소/최대 값과 단위를 정리한 데이터 구조로, 행과 열 간 연결 관계가 없습니다.  

**Quantitative Data**:  
- f_CLK: 1 MHz ~ 3.25 MHz (정상 모드 시계 주파수)  
- T_CLK: 308 ns ~ 1000 ns (정상 모드 시계 주기)  
- T_R,EN/T_L,EN: 18 ns (L/R 핀이 1/0일 때 데이터 활성화 시간)  
- T_R,DIS/T_L,DIS: 16 ns (L/R 핀이 1/0일 때 데이터 비활성화 시간)  

**Visual Encoding**:  
- 표는 표준 테이블 구조로, 행/열 분리선과 텍스트 정렬만 사용합니다. 색상이나 특수 기호 없음.  

**Textual Content in Image**:  
- "Acoustic and electrical specifications", "MP34DT01-M"  
- "2.3 Timing characteristics"  
- "Table 5. Timing characteristics"  
- "1. From design simulations"  
- "6/17", "DocID026514 Rev 3"  

**쉬운 설명 (비전문가용, 자세히 / easy detailed explanation)**:  
이 표는 MP34DT01-M 장치의 타이밍 관련 규격을 정리한 것입니다. 'f_CLK'는 정상 작동 모드에서 사용하는 시계 주파수로 1MHz부터 3.25MHz까지 변할 수 있습니다. 'T_CLK'는 이 시계 신호가 한 번 완전히 반복되는 시간(주기)으로 308나노초(ns)에서 1000ns 사이입니다.  
다음으로, 오디오 데이터가 활성화되거나 비활성화되는 시간을 나타내는 값들이 있습니다. 예를 들어, L/R 핀이 1일 때(오른쪽 채널) 데이터가 활성화되는 시간(T_R,EN)은 설계 시뮬레이션에서 18ns로 측정되었고, 비활성화되는 시간(T_R,DIS)은 16ns입니다. L/R 핀이 0일 때(왼쪽 채널)도 유사한 값들이 적용됩니다.  
이 표는 장치가 정상 작동하거나 전원을 끈 상태에서 시계 신호와 데이터 전송에 대한 시간적 특성을 명시합니다. 이 정보는 장치를 다른 회로와 연결할 때 타이밍을 맞추기 위해 필요합니다.  

**용어 풀이 (Glossary)**:  
- 시계 주파수(1초당 반복 횟수): 1초에 발생하는 신호의 주기 수  
- 시계 주기(1회 반복 시간): 시계 신호가 한 번 완전히 반복되는 시간  
- L/R 핀(오른쪽/왼쪽 채널 구분 핀): 오디오에서 오른쪽/왼쪽 채널을 구분하는 신호 핀  
- ns(나노초): 10억 분의 1 초 (10⁻⁹초)  
- High Z(고 임피던스 상태): 신호가 비활성화되어 다른 회로와 간섭하지 않는 상태  

**Interpretation / Purpose**:  
이 표는 장치의 타이밍 특성을 수치로 정리하여, 설계자나 사용자가 시계 주파수 및 데이터 전송 시간을 정확히 이해하고 다른 회로와 호환시킬 때 참조할 수 있도록 합니다.  

---

### Figure 2: Timing waveforms
**한 줄 요약 (쉬운 말 / one-line plain summary)**: 이 그림은 시계 신호(CLK)와 오디오 데이터(PDM R, PDM L)의 타이밍을 파형으로 보여주는 타이밍 다이어그램입니다.  
**Type**: timing diagram  
**Caption (original)**: Figure 3. Timing waveforms  

**Components / Elements**:  
- **신호 라인**:  
  - CLK (시계 신호)  
  - PDM R (오른쪽 채널 오디오 데이터, Pulse Density Modulation)  
  - PDM L (왼쪽 채널 오디오 데이터)  
- **타이밍 레이블**: T_CLK, T_R,EN, T_R,DIS, T_L,EN  
- **상태 표시**: "High Z" (고 임피던스 상태, 신호 비활성화)  

**Relations / Connections**:  
- CLK의 상승 에지(rising edge)에서 PDM R/L의 데이터 활성화 시간(T_R,EN/T_L,EN)이 시작됩니다.  
- PDM R/L은 "High Z" 상태로 전환될 때까지 데이터를 전송합니다.  
- T_R,DIS는 PDM R의 데이터 비활성화 시간을 나타내며, 이는 다음 데이터 활성화 시점까지의 간격입니다.  

**Quantitative Data**:  
- T_CLK: CLK 신호의 주기 (표 5에서 308ns~1000ns)  
- T_R,EN/T_L,EN: 18ns (표 5 참조)  
- T_R,DIS/T_L,DIS: 16ns (표 5 참조)  

**Visual Encoding**:  
- 파형은 직사각형으로 표현되며, "High Z"는 텍스트로 명시된 비활성화 구간입니다.  
- 시간 간격(T_CLK, T_R,EN 등)은 화살표와 레이블로 표시됩니다.  

**Textual Content in Image**:  
- "Figure 3. Timing waveforms"  
- "CLK", "PDM R", "PDM L"  
- "T_CLK", "T_R,EN", "T_R,DIS", "T_L,EN"  
- "High Z" (2회 반복)  
- "AM045165v1"  

**쉬운 설명 (비전문가용, 자세히 / easy detailed explanation)**:  
이 그림은 시계 신호(CLK)와 오디오 데이터(PDM R, PDM L)의 타이밍을 파형으로 보여줍니다. CLK는 정기적으로 반복되는 신호로, T_CLK가 이 신호의 주기를 나타냅니다.  
- **PDM R (오른쪽 채널)**:  
  - CLK의 상승 에지 이후 T_R,EN(18ns) 시간이 지나면 데이터 전송이 시작됩니다.  
  - 데이터 전송이 끝나면 "High Z" 상태로 들어가며, 이는 신호가 비활성화된 상태입니다.  
  - 다음 데이터 전송까지의 시간은 T_R,DIS(16ns)로 표시됩니다.  
- **PDM L (왼쪽 채널)**:  
  - CLK의 상승 에지 이후 T_L,EN(18ns) 시간이 지나면 데이터 전송이 시작됩니다.  
  - 이 그림에서 보이는 것처럼, 오른쪽 채널과 왼쪽 채널은 번갈아 가며 데이터를 전송합니다 (예: 첫 번째 시계 주기에는 오른쪽 채널만 활성화되고, 두 번째 주기에는 왼쪽 채널이 활성화됩니다).  
- "High Z" 상태는 신호가 비활성화되어 다른 회로와 간섭하지 않는 상태를 의미합니다.  

**용어 풀이 (Glossary)**:  
- 시계 신호(CLK): 주기적으로 반복되는 신호로, 다른 신호들이 동기화되도록 합니다.  
- 상승 에지(rising edge): 신호가 0에서 1로 변하는 순간 (예: 전압이 증가하는 지점)  
- PDM(펄스 밀도 모드): 오디오 신호를 디지털로 변환하는 방식 중 하나 (Pulse Density Modulation)  
- High Z(고 임피던스 상태): 신호가 비활성화되어 다른 회로와 간섭하지 않는 상태  

**Interpretation / Purpose**:  
이 그림은 MP34DT01-M 장치에서 오디오 데이터(오른쪽/왼쪽 채널)가 시계 신호에 맞춰 어떻게 전송되는지 보여줍니다. 이 정보는 장치를 다른 회로와 연결할 때 타이밍을 정확히 맞추기 위해 필요합니다.
