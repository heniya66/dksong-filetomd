4.2 Electrical characteristics

@ Vdd = 1.8 V, T = 25 °C unless otherwise noted.

Table 4. Electrical characteristics

| Symbol | Parameter | Test conditions | Min. | Typ.(1) | Max. | Unit |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Vdd | Supply voltage | | 1.71 | 1.8 | 3.6 | V |
| Vdd_IO | Power supply for I/O | | 1.62 | | 3.6 | V |
| IddHP | Gyroscope and accelerometer current consumption in high-performance mode | ODR = 1.6 kHz | | 0.65 | | mA |
| IddNM | Gyroscope and accelerometer current consumption in normal mode | ODR = 208 Hz | | 0.45 | | mA |
| IddLP | Gyroscope and accelerometer current consumption in low-power mode | ODR = 52 Hz | | 0.29 | | mA |
| LA_IddHP | Accelerometer current consumption in high-performance mode | ODR < 1.6 kHz | | 150 | | μA |
| LA_IddNM | Accelerometer current consumption in normal mode | ODR = 208 Hz | | 85 | | μA |
| LA_IddLM | Accelerometer current consumption in low-power mode | ODR = 52 Hz | | 25 | | μA |
| ODR = 1.6 Hz | ODR = 12.5 Hz | ODR = 1.6 Hz | | 9 | | |
| ODR = 1.6 Hz | ODR = 1.6 Hz | ODR = 1.6 Hz | | 4.5 | | |

IddPD | Gyroscope and accelerometer current consumption during power-down | | | 3 | | μA |
Ton | Turn-on time | | | 35 | | ms |
V$_{IH}$ | Digital high-level input voltage | | 0.7 *VDD_IO | | | V |
V$_{IL}$ | Digital low-level input voltage | | | | 0.3 *VDD_IO | V |
V$_{OH}$ | High-level output voltage | $I_{OH} = 4 \text{ mA}^{(2)}$ | VDD_IO - 0.2 | | | V |
V$_{OL}$ | Low-level output voltage | $I_{OL} = 4 \text{ mA}^{(2)}$ | | | 0.2 | V |
Top | Operating temperature range | | -40 | | +85 | °C |

1. Typical specifications are not guaranteed.
2. 4 mA is the maximum driving capability, i.e. the maximum DC current that can be sourced/sunk by the digital pad in order to guarantee the correct digital output voltage levels $V_{OH}$ and $V_{OL}$.