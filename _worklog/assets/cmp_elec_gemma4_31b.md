제공된 이미지는 **LSM6DSM** 센서의 기술 문서 중 **전기적 특성(Electrical characteristics)** 섹션입니다. 해당 내용을 바탕으로 상세히 정리한 내용은 다음과 같습니다.

---

# LSM6DSM 모듈 사양 (Module specifications)

## 4.2 전기적 특성 (Electrical characteristics)
본 섹션의 모든 값은 별도의 언급이 없는 한 **@ Vdd = 1.8 V, T = 25 °C** 조건에서의 기준입니다.

### 표 4. 전기적 특성 (Table 4. Electrical characteristics)

| 심볼 (Symbol) | 파라미터 (Parameter) | 테스트 조건 (Test conditions) | 최소값 (Min.) | 전형값 (Typ. $^{(1)}$) | 최대값 (Max.) | 단위 (Unit) |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| $\text{V}_{\text{dd}}$ | 공급 전압 (Supply voltage) | - | 1.71 | 1.8 | 3.6 | V |
| $\text{V}_{\text{dd\_IO}}$ | I/O 전원 공급 (Power supply for I/O) | - | 1.62 | - | 3.6 | V |
| $\text{IddHP}$ | 고성능 모드에서의 자이로스코프 및 가속도계 전류 소모 (Gyroscope and accelerometer current consumption in high-performance mode) | $\text{ODR} = 1.6\text{ kHz}$ | - | 0.65 | - | mA |
| $\text{IddNM}$ | 일반 모드에서의 자이로스코프 및 가속도계 전류 소모 (Gyroscope and accelerometer current consumption in normal mode) | $\text{ODR} = 208\text{ Hz}$ | - | 0.45 | - | mA |
| $\text{IddLP}$ | 저전력 모드에서의 자이로스코프 및 가속도계 전류 소모 (Gyroscope and accelerometer current consumption in low-power mode) | $\text{ODR} = 52\text{ Hz}$ | - | 0.29 | - | mA |
| $\text{LA\_IddHP}$ | 고성능 모드에서의 가속도계 전류 소모 (Accelerometer current consumption in high-performance mode) | $\text{ODR} < 1.6\text{ kHz}$<br>$\text{ODR} = 1.6\text{ kHz}$ | - | 150<br>160 | - | $\mu\text{A}$ |
| $\text{LA\_IddNM}$ | 일반 모드에서의 가속도계 전류 소모 (Accelerometer current consumption in normal mode) | $\text{ODR} = 208\text{ Hz}$ | - | 85 | - | $\mu\text{A}$ |
| $\text{LA\_IddLM}$ | 저전력 모드에서의 가속도계 전류 소모 (Accelerometer current consumption in low-power mode) | $\text{ODR} = 52\text{ Hz}$<br>$\text{ODR} = 12.5\text{ Hz}$<br>$\text{ODR} = 1.6\text{ Hz}$ | - | 25<br>9<br>4.5 | - | $\mu\text{A}$ |
| $\text{IddPD}$ | 전원 다운 상태에서의 자이로스코프 및 가속도계 전류 소모 (Gyroscope and accelerometer current consumption during power-down) | - | - | 3 | - | $\mu\text{A}$ |
| $\text{T}_{\text{on}}$ | 턴온 시간 (Turn-on time) | - | - | 35 | - | ms |
| $\text{V}_{\text{IH}}$ | 디지털 하이 레벨 입력 전압 (Digital high-level input voltage) | - | $0.7 \times \text{VDD\_IO}$ | - | - | V |
| $\text{V}_{\text{IL}}$ | 디지털 로우 레벨 입력 전압 (Digital low-level input voltage) | - | - | - | $0.3 \times \text{VDD\_IO}$ | V |
| $\text{V}_{\text{OH}}$ | 하이 레벨 출력 전압 (High-level output voltage) | $\text{I}_{\text{OH}} = 4\text{ mA } ^{(2)}$ | $\text{VDD\_IO} - 0.2$ | - | - | V |
| $\text{V}_{\text{OL}}$ | 로우 레벨 출력 전압 (Low-level output voltage) | $\text{I}_{\text{OL}} = 4