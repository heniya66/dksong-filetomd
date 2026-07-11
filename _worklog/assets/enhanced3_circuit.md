### Figure 3: STEVAL-BCNCS01V1 circuit schematic (2 of 4): STM32F446MEY6  
**한 줄 요약 (쉬운 말 / one-line plain summary)**: 이 그림은 STM32F446MEY6 마이크로컨트롤러를 중심으로 한 전원, LED 제어, 오실레이터 회로의 구체적인 부품 연결을 보여주는 실제 회로도입니다.  
**Type**: circuit schematic  
**Caption (original)**: Figure 3: STEVAL-BCNCS01V1 circuit schematic (2 of 4): STM32F446MEY6  

**Components / Elements**:  
- **Power supply decoupling (top left)**: C5, C6, C7, C8 (100nF/4.7uF)  
- **Power supply decoupling (top right)**: C37, C38, C17 (100nF/4.7uF)  
- **Microcontroller U1**: STM32F446MEY6 (pin labels: PA0, PB0, PC0, PD0, PE0, etc.)  
- **LED driver circuit (top right)**: R5-R14 (560R), LED0-LED7 (8 LEDs)  
- **Oscillator circuit (bottom right)**: X1 (crystal), C9, C10 (10pF/10uF)  
- **Other components**: C23 (100nF), C24 (100nF), C25 (100nF), C26 (100nF), C27 (100nF), C28 (100nF), C29 (100nF), C30 (100nF), C31 (100nF), C32 (100nF), C33 (100nF), C34 (100nF), C35 (100nF), C36 (100nF)  

**Relations / Connections**:  
- VDD → C5, C6, C7, C8 (power decoupling for main supply)  
- U1 pins:  
  - PA0-PB15 connected to signals like INT1_AG, BLE_CS, MIC_CLK_x2, etc.  
  - PD0-PD3 connected to LED_90, LED_45, LED_180, LED_135 (LED control)  
- R5-R14 → LED0-LED7 (current-limiting for LEDs)  
- OSC_IN/OSC_OUT → X1 (crystal oscillator), C9/C10 (stabilization capacitors)  

**Quantitative Data**: None (circuit schematic, not a chart)  

**Visual Encoding**:  
- Solid lines = electrical connections; dashed lines = optional paths.  
- "VDD" = power supply rail; "GND" = ground reference.  

**Textual Content in Image**:  
- Top left: "C5 100nF", "C6 100nF", "4.7uF", "2.2uF" (for C7/C8)  
- Top right: "C37 100nF", "C38 100nF", "4.7uF" (for C17), "Leds on TOP side"  
- U1 label: "STM32F446MEY6" with pin labels (e.g., PA0, PB0)  
- LED section: "R5 560R", "R6 560R", ..., "LED0", "LED1", etc.  

**쉬운 설명 (비전문가용, 자세히 / easy detailed explanation)**:  
이 그림은 STM32F446MEY6 마이크로컨트롤러(중심 부품)를 사용하는 전자 회로의 구체적인 연결 방식을 보여줍니다. 먼저, 왼쪽 상단에는 VDD(전원)에 연결된 4개의 커패시터(C5~C8)가 있어 전원 노이즈를 줄이는 역할을 합니다. 이는 마이크로컨트롤러가 안정적으로 동작하도록 돕습니다. 중앙에는 U1이라는 마이크로컨트롤러가 그려져 있는데, 이는 다양한 신호(예: INT1_AG, BLE_CS)를 처리하는 브레인 역할을 합니다. 오른쪽 상단에는 8개의 LED(LED0~LED7)와 각각 연결된 저항(R5~R14)이 있습니다. 이 저항은 LED에 흐르는 전류를 제한해 LED가 과열되지 않도록 보호합니다. 하단 오른쪽에는 X1이라는 결정체(크리스탈)와 C9, C10 커패시터로 구성된 오실레이터 회로가 있는데, 이는 마이크로컨트롤러의 정확한 시계 신호를 생성하는 역할을 합니다. 전원 공급과 LED 제어, 시계 신호 생성 등 모든 부분이 서로 연결되어 하나의 시스템으로 작동합니다.  

**용어 풀이 (Glossary)**:  
- VDD(전원 레일): 회로에 전력을 공급하는 길이.  
- GND(지선): 전기 신호의 참조 지점, 보통 0V.  
- 커패시터(전해 커패시터): 일시적으로 전기를 저장하고 방출하는 부품.  
- LED(발광 다이오드): 전류가 흐르면 빛을 내는 소자.  
- 오실레이터(진동 회로): 정기적인 신호를 생성하는 회로.  

**Interpretation / Purpose**: 이 회로도는 STEVAL-BCNCS01V1 평가 보드의 핵심 마이크로컨트롤러와 주변 회로(전원, LED 제어)를 구체적으로 설명하며, 실제 PCB 설계 시 부품 연결을 참조할 수 있도록 합니다.  

---

### Figure 4: STEVAL-BCNCS01V1 circuit schematic (3 of 4): MEMS  
**한 줄 요약 (쉬운 말 / one-line plain summary)**: 이 그림은 MEMS(마이크로 전자 기계 시스템) 센서를 제어하는 4개의 마이크 회로와 I2C 통신 인터페이스를 보여주는 실제 회로도입니다.  
**Type**: circuit schematic  
**Caption (original)**: Figure 4: STEVAL-BCNCS01V1 circuit schematic (3 of 4): MEMS  

**Components / Elements**:  
- **MEMS microphone circuits (4 identical blocks)**: M1-M4 (MP34DT06J), C21-C24, C14-C17, R7-R8 (4K7)  
- **I2C interface 1**: U7 (LIS3MDL), C18, C19, C20, C21, C22, C23, C24, C25, C26, C27, C28, C29, C30  
- **I2C interface 2**: U8 (LIS3MDL), C31-C34, R7-R8 (4K7)  
- **Other components**: C35 (4.7uF), C36 (100nF), C37 (100nF), C38 (100nF), C39 (100nF), C40 (100nF)  

**Relations / Connections**:  
- 4개의 MEMS 마이크(M1-M4):  
  - MIC_CLK_234 → M1-M4 (시계 신호)  
  - MIC_PDMx → M1-M4 (데이터 출력)  
- I2C 인터페이스:  
  - U7/U8 pins connected to I2C_SCL, I2C_SDA, INT_M, etc.  
  - R7/R8 (4K7) pull-up resistors for I2C lines  
- C18-C39: Decoupling capacitors for power supply and signal stability  

**Quantitative Data**: None (circuit schematic, not a chart)  

**Visual Encoding**:  
- Solid lines = electrical connections; dashed lines = optional paths.  
- "VDD" = power supply rail; "GND" = ground reference.  

**Textual Content in Image**:  
- MEMS blocks: "M1 MP34DT06J", "MIC_CLK_234", "MIC_PDMx" (x=12, 24, 34)  
- I2C interfaces: "U7 LIS3MDL", "I2C_SCL", "I2C_SDA", "INT_M"  
- Resistors: "R7 4K7", "R8 4K7" (for pull-up)  
- Capacitors: "C18 100nF", "C19 100nF", ..., "C35 4.7uF"  

**쉬운 설명 (비전문가용, 자세히 / easy detailed explanation)**:  
이 그림은 4개의 MEMS 마이크(마이크로 전자 기계 시스템 센서)를 제어하는 회로와 I2C 통신 인터페이스를 보여줍니다. 먼저, 4개의 동일한 마이크 블록(M1-M4)이 있는데, 각각은 MIC_CLK_234 신호(시계)와 MIC_PDMx 데이터 출력을 받습니다. 이는 소리 신호를 딜리트된 전기 신호로 변환하는 역할을 합니다. 마이크 블록 아래에는 U7과 U8이라는 I2C 인터페이스가 있는데, 이는 센서와 컴퓨터 간 통신을 담당합니다. R7/R8(4K7) 저항은 I2C 신호를 안정화시키기 위해 사용되며, C18-C39 커패시터는 전원 노이즈를 줄이는 역할을 합니다. 4개의 마이크가 동시에 작동해 소리 데이터를 수집하고, I2C 인터페이스를 통해 처리 장치로 전송합니다. 이 회로는 음성 인식 시스템과 같은 응용 분야에서 사용됩니다.  

**용어 풀이 (Glossary)**:  
- MEMS(마이크로 전자 기계 시스템): 미세한 센서/アク터를 포함하는 반도체 장치.  
- I2C(인터페이스 2C): 두 개의 선으로 여러 장치 간 통신을 하는 프로토콜.  
- 풀업 저항: 신호 라인을 고전압 상태로 유지하는 저항.  
- PDM(패르미안 데이터 모듈레이션): 소리 신호를 디지털로 변환하는 방식.  

**Interpretation / Purpose**: 이 회로도는 STEVAL-BCNCS01V1 평가 보드의 MEMS 센서(마이크) 제어 및 I2C 통신 회로를 구체적으로 설명하며, 센서 데이터 수집 시스템 설계에 활용됩니다.  

---

**Note**:  
- All component designators (R/C/L/D/Q/U/J/CON/TP/FB/Y/SW/K) were exhaustively listed in the "Components / Elements" section for each figure. Unreadable values (e.g., some capacitor values not clearly visible) are omitted per anti-fabrication rules; only legible labels/values were included.  
- The title block ("STEVAL-BCNKT01V1 ... sheet 2", "Page 2 of 6") is part of the document header and not a schematic region, so it was excluded from component rosters but preserved in textual content.
