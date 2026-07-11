### Figure 4: LSM6DSM 모듈의 전기적 특성 표  
**한 줄 요약 (쉬운 말 / one-line plain summary)**: 이 그림은 LSM6DSM 센서 모듈이 사용하는 전원, 소모 전류, 작동 온도 등 다양한 전기적 특성을 표로 정리해 놓은 것입니다.  
**Type**: table-image  
**Caption (original)**: Table 4. Electrical characteristics  

**Components / Elements**:  
- Columns: Symbol, Parameter, Test conditions, Min., Typ.(1), Max., Unit  
- Rows (by symbol): Vdd, Vdd_IO, IddHP, IddNM, IddLP, LA_IddHP, LA_IddNM, LA_IddLM, IddPD, Ton, VIH, VIL, VOH, VOL, Top  

**Relations / Connections**:  
- No arrows or connections; it is a static data table.  

**Quantitative Data**:  
- Not applicable (it's a table, not a chart/graph).  

**Visual Encoding**:  
- N/A (monochrome text without color coding)  

**Textual Content in Image**:  
Module specifications  
4.2 Electrical characteristics  
@ Vdd = 1.8 V, T = 25 °C unless otherwise noted.  
Table 4. Electrical characteristics  

| Symbol | Parameter | Test conditions | Min. | Typ.(1) | Max. | Unit |
|--------|-----------|-----------------|------|---------|------|------|
| Vdd    | Supply voltage |              | 1.71 | 1.8     | 3.6  | V    |
| Vdd_IO | Power supply for I/O |          | 1.62 |         | 3.6  | V    |
| IddHP  | Gyroscope and accelerometer current consumption in high-performance mode | ODR = 1.6 kHz |      | 0.65   |      | mA   |
| IddNM  | Gyroscope and accelerometer current consumption in normal mode | ODR = 208 Hz |      | 0.45   |      | mA   |
| IddLP  | Gyroscope and accelerometer current consumption in low-power mode | ODR = 52 Hz |      | 0.29   |      | mA   |
| LA_IddHP | Accelerometer current consumption in high-performance mode | ODR < 1.6 kHz<br>ODR ≥ 1.6 kHz |      | 150<br>160 |      | μA   |
| LA_IddNM | Accelerometer current consumption in normal mode | ODR = 208 Hz |      | 85     |      | μA   |
| LA_IddLM | Accelerometer current consumption in low-power mode | ODR = 52 Hz<br>ODR = 12.5 Hz<br>ODR = 1.6 Hz |      | 25<br>9<br>4.5 |      | μA   |
| IddPD  | Gyroscope and accelerometer current consumption during power-down |              |      | 3      |      | μA   |
| Ton    | Turn-on time |                |      | 35     |      | ms   |
| VIH    | Digital high-level input voltage |          | 0.7 *VDD_IO |        |      | V    |
| VIL    | Digital low-level input voltage |         |      |        | 0.3 *VDD_IO | V    |
| VOH    | High-level output voltage | I_OH = 4 mA (2) | VDD_IO - 0.2 |        |      | V    |
| VOL    | Low-level output voltage | I_OL = 4 mA (2) |      |        | 0.2  | V    |
| Top    | Operating temperature range |          | -40  |         | +85  | °C   |

1. Typical specifications are not guaranteed.  
2. 4 mA is the maximum driving capability, i.e. the maximum DC current that can be sourced/sunk by the digital pad in order to guarantee the correct digital output voltage levels VOH and VOL.  

26/126  
DocID028165 Rev 7  
LSM6DSM  

**쉬운 설명 (비전문가용, 자세히 / easy detailed explanation)**:  
이 표는 LSM6DSM 센서 모듈(가속도계와 자이로스코프를 포함한 센서)의 전기적 특성을 정리해 놓은 것입니다. 이 모듈을 사용할 때 필요한 정보를 모두 담고 있습니다.  

먼저, "Vdd"는 모듈의 주 전원 전압을 말합니다. 이 값은 1.71V에서 3.6V 사이로, 보통 1.8V로 작동합니다. "Vdd_IO"는 I/O(입출력) 부분에 공급되는 전원으로 1.62V부터 3.6V까지입니다.  

다음으로, "IddHP", "IddNM", "IddLP"는 각각 고성능 모드, 정상 모드, 저전력 모드에서 가속도계와 자이로스코프가 소모하는 전류를 나타냅니다. 예를 들어, 고성능 모드(ODR=1.6kHz)에서는 0.65mA의 전류를 사용합니다. ODR(출력 데이터 주파수)는 센서가 얼마나 빠르게 데이터를 내보내는지 결정하는 값입니다.  

"LA_IddHP", "LA_IddNM", "LA_IddLM"은 가속도계만 고려한 전류 소모로, 각 모드에서의 값을 보여줍니다. 예를 들어, 고성능 모드에서는 150μA 또는 160μA가 측정됩니다.  

"Ton"은 모듈이 작동하기 시작하는 시간(35ms)을 말합니다. "VIH", "VIL"은 디지털 신호의 높은 수준과 낮은 수준 입력 전압을 나타내며, 이는 VDD_IO에 따라 결정됩니다.  

"VOH", "VOL"은 출력 전압의 높은 수준과 낮은 수준입니다. 예를 들어, VOH는 VDD_IO에서 0.2V가 감소한 값이고, VOL은 0.2V입니다. 이 값들은 디지털 신호가 올바르게 작동하도록 보장하기 위해 4mA의 최대 전류를 공급해야 한다는 조건이 있습니다.  

마지막으로 "Top"은 모듈이 정상적으로 작동할 수 있는 온도 범위(-40°C ~ +85°C)입니다.  

이 표는 센서가 어떤 조건에서 어떻게 동작하는지, 얼마나 많은 전력을 사용하는지, 어떤 전압을 필요로 하는지 등을 알기 위해 사용됩니다. 예를 들어, 배터리로 작동하는 장치에서는 저전력 모드(0.29mA)를 선택해 전원 소모를 줄일 수 있습니다.  

**용어 풀이 (Glossary)**:  
- ODR(출력 데이터 주파수): 센서가 데이터를 얼마나 빠르게 내보내는지 나타내는 값.  
- Vdd(핵심 회로 전원 전압): IC의 주 전원 공급 전압.  
- Idd(전류 소모): IC가 사용하는 전류.  
- μA(마이크로암페어): 1/1,000,000 암페어. 매우 작은 전류 단위.  
- ms(밀리초): 1/1,000 초.  

**Interpretation / Purpose**:  
This table provides electrical specifications for the LSM6DSM module, including power supply voltages, current consumption under various conditions, and operating temperature range. It helps engineers ensure that the sensor operates within safe limits when designing circuits.
