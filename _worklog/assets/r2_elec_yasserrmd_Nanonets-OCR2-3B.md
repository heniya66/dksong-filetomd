# Module specifications

## 4.2 Electrical characteristics

@ Vdd = 1.8 V, T = 25 °C unless otherwise noted.

### Table 4. Electrical characteristics

| Symbol | Parameter                  | Test conditions | Min.   | Typ.(1) | Max.   | Unit |
|--------|-----------------------------|-----------------|--------|---------|--------|------|
| Vdd    | Supply voltage              |                 |        |         |        |      |
|        |                             |                 | 1.71   | 1.8     | 3.6    | V    |
| Vdd_IO | Power supply for I/O        |                 |        |         |        |      |
|        |                             |                 | 1.62   | 3.6     |        |      |
| IddHP  | Gyroscope and accelerometer current consumption in high-performance mode | ODR = 1.6 kHz |        | 0.65    |        | mA   |
| IddNM  | Gyroscope and accelerometer current consumption in normal mode | ODR = 208 Hz |        | 0.45    |        | mA   |
| IddLP  | Gyroscope and accelerometer current consumption in low-power mode | ODR = 52 Hz |        | 0.29    |        | mA   |
| LA_IddHP | Accelerometer current consumption in high-performance mode | ODR < 1.6 kHz, ODR ≥ 1.6 kHz |        | 150     | 160    | µA   |
| LA_IddNM | Accelerometer current consumption in normal mode | ODR = 208 Hz |        | 85      |        | µA   |
| LA_IddLM | Accelerometer current consumption in low-power mode | ODR = 52 Hz, ODR = 12.5 Hz, ODR = 1.6 Hz |        | 25      | 9       | µA   |
| IddPD  | Gyroscope and accelerometer current consumption during power-down |                 |        |         |        | µA   |
| Ton    | Turn-on time                |                 |        |         |        | ms   |
| VIH    | Digital high-level input voltage |             |        |         |        |      |
| VIL    | Digital low-level input voltage |             |        |         |        |      |
| VOH    | High-level output voltage     | IOH = 4 mA (2) |        | VDD_IO - 0.2 |        | V   |
| VOL    | Low-level output voltage      | IOL = 4 mA (2) |        |         |        | V   |
| Top    | Operating temperature range   |                 |        |         |        | °C   |

1.  Typical specifications are not guaranteed.
2.  4 mA is the maximum driving capability, i.e. the maximum DC current that can be sourced/sunk by the digital pad in order to guarantee the correct digital output voltage levels VOH and VOL.

<footer>26/126</footer>
<footer>DocID028165 Rev 7</footer>
<img>ST logo</img>