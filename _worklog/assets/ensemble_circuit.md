## 1. 통합본  
# STEVAL-BCNKT01V1 회로도 문서 (버전 2) - 페이지 2/6  
**STMicroelectronics**  
*All information on this page is subject to the Evaluation Board License Agreement included in this document*

---

## **Figure 3: STM32F446MEY6 회로도 (2 of 4)**

### **1. 주요 구성 요소**
- **MCU**: STM32F446MEY6 (중앙 처리 장치)
- **크리스탈(Crystal)**: X1 (외부 클럭 소스, 16MHz)
- **LED 구동 회로**: "Leds on TOP side" 섹션
- **오실레이터 회로**: OSC_IN/OSC_OUT 연결

---

### **2. MCU (STM32F446MEY6) 핀 연결 상세**
#### **(1) 전원 및 리셋 관련**
| 핀 | 신호명         | 연결 구성                     |
|----|----------------|------------------------------|
| VDD | VDD            | 100nF, 100nF, 4.7uF 커패시터로 필터링 (C5, C6, C8) |
| GND | GND            | GND 연결                    |
| RESET | RESET        | 100nF 커패시터 (C41) 및 10kΩ 저항을 통해 VDD 연결 |
| VDDA | -             | (B에서 언급됨, 구체적 연결 정보 미제공) |

#### **(2) 주요 신호 입력/출력**
- **BLE 관련**:  
  - `BLE_RST` → PA0  
  - `BLE_SCK` → PB15, `BLE_MISO` → PB14, `BLE_MOSI` → PB13  
  - `BLE_CS` → PB12 (560R 저항을 통해 LED_90 연결)  

- **USB/OTG**:  
  - `OTG_FS_VBUS` → PA9, `OTG_FS_ID` → PA10, `OTG_FS_DM` → PA11, `OTG_FS_DP` → PA12  

- **I²C**:  
  - `I2C1_SCL` → PB6, `I2C1_SDA` → PB7 (560R 저항을 통해 LED_45 연결)  

- **PDM 마이크**:  
  - `MIC_CLK_234` → PC12, `MIC_PDM12` → PC13, `MIC_PDM34` → PC14  

- **기타 신호**:  
  - `SHUTDOWN` → PB8, `MULTI_MIC_EN` → PB9, `INT1_AG` → PA15  

---

### **3. LED 구동 회로 (TOP side)**
#### **(1) LED 연결 및 저항**
| LED 번호 | LED 라벨 | 저항 값 | 연결 신호       |
|----------|----------|---------|----------------|
| LED_0    | LED0     | 560R    | PA4            |
| LED_45   | LED1     | 560R    | PB12 (BLE_CS)  |
| LED_90   | LED2     | 560R    | PA8            |
| LED_135  | LED3     | 560R    | PA7            |
| LED_180  | LED4     | 560R    | PB10           |
| LED_225  | LED5     | 560R    | PB9 (MULTI_MIC_EN) |
| LED_270  | LED6     | 560R    | PA3            |
| LED_315  | LED7     | 560R    | PA2            |

#### **(2) 추가 LED**
- `LED_8` → PB1 (560R 저항, LED8)

---

### **4. 오실레이터 회로**
- **입력**: OSC_IN (16MHz 크리스탈)
- **출력**: OSC_OUT
- **커패시터**: C9 (10pF), C12 (10pF)  
  - C9: OSC_IN → GND, C12: OSC_OUT → GND

---

## **Figure 4: MEMS 회로도**

### **1. 주요 구성 요소**
- **MEMS 마이크**: MIC_PDM12, MIC_PDM34
- **가속도계/자기 센서**: U7 (LIS3DSH)
- **압력 센서**: U8 (LPS25H)
- **I²C 인터페이스**: I2C1_SCL, I2C1_SDA

---

### **2. MEMS 마이크 회로**
#### **(1) PDM 마이크 4개 구성**
| 마이크 | IC (MP34DT06J) | 신호 연결               | 저항/커패시터 |
|--------|----------------|-------------------------|---------------|
| MIC_PDM12 | M1             | `MIC_CLK_234` → CLK, `MIC_PDM12` → DOUT | C21 (100nF)   |
| MIC_PDM34 | M3             | `MIC_CLK_234` → CLK, `MIC_PDM34` → DOUT | C14 (100nF)   |
| MIC_PDM12 | M2             | `MIC_CLK_234` → CLK, `MIC_PDM12` → DOUT | C22 (100nF)   |
| MIC_PDM34 | M4             | `MIC_CLK_234` → CLK, `MIC_PDM34` → DOUT | C13 (100nF)   |

#### **(2) 공통 구성**
- 모든 마이크: VDD → 100nF 커패시터 (C21, C22, C14, C13)
- `MIC_CLK_234` 신호: U6 (U6A15P1TLR)에서 출력

---

### **3. 가속도계/자기 센서 (LIS3DSH)**
#### **(1) IC 연결**
| 핀 | 신호명         | 연결 구성                     |
|----|----------------|------------------------------|
| SCL | I2C1_SCL       | 4.7kΩ 저항 (R7)을 통해 VDD 연결 |
| SDA | I2C1_SDA       | 4.7kΩ 저항 (R8)을 통해 VDD 연결 |
| INT1_AG | INT1_AG      | GND → C16 (100nF), C18 (100nF) |

#### **(2) 전원 및 필터**
- VDD: 4.7uF 커패시터 (C34, C35)로 필터링

---

### **4. 압력 센서 (LPS25H)**
#### **(1) IC 연결**
| 핀 | 신호명         | 연결 구성                     |
|----|----------------|------------------------------|
| SCL | I2C1_SCL       | 4.7kΩ 저항 (R7, R8)을 통해 VDD 연결 |
| SDA | I2C1_SDA       | 4.7kΩ 저항 (R7, R8)을 통해 VDD 연결 |
| INT_P | INT_P        | GND → C36 (10uF), C39 (2.2uF) |

#### **(2) 전원 및 필터**
- VDD: 4.7uF 커패시터 (C34, C35)로 필터링

---

### **5. I²C 인터페이스 공통 구성**
- **I2C1_SCL**: R7 (4.7kΩ), R8 (4.7kΩ) 저항을 통해 VDD 연결
- **I2C1_SDA**: R7, R8 저항을 통해 VDD 연결

---

## **참고사항**
- 모든 전원/지선 연결은 100nF, 4.7uF 커패시터로 필터링 처리됨.
- LED 회로의 저항 값: 560R (560Ω)
- MEMS 마이크 및 센서는 `MIC_CLK_234` 신호를 공유하여 동작.
- **주의**: "Leds on TOP side" 구역은 PCB 상단에 위치한 LED를 나타냄.

---

## 2. 상호 보완 분석  
### - A에만 있는 정보
1. MCU 핀 연결 상세 테이블 (VDD, GND, RESET 등 구체적 커패시터/저항 값 및 부품 번호: C5, C6, C8, C41, 10kΩ)
2. LED 구동 회로의 전체 테이블 (LED_0 ~ LED_315, 저항 값, 연결 신호)
3. 오실레이터 회로의 16MHz 크리스탈 및 C9/C12 (10pF) 정확한 연결 방식
4. MEMS 마이크 4개의 구체적 IC (M1~M4), 신호 연결, 커패시터 (C13~C22)
5. 가속도계/압력 센서의 I²C 핀 연결 및 INT1_AG/INT_P 신호의 C16/C18/C36/C39 정확한 값
6. I²C 인터페이스 공통 구성 (R7, R8 4.7kΩ 저항)
7. "Leds on TOP side" 구역 설명

### - B에만 있는 정보
1. **VDDA** 전원 레일 언급 (구체적 연결 정보 미제공)
2. **크리스탈(X1)**의 "외부 클럭 소스" 명칭 (16MHz 및 C9/C12 정보는 A에서 제공)

### - 서로 불일치(모순)하는 항목
- **없음**  
  (B의 VDDA 언급은 A에 미제공된 추가 정보로, 구체적 연결 방식이 없어 모순되지 않음. 크리스탈(X1) 명칭과 A의 "16MHz"는 보완적 정보로 간주됨.)