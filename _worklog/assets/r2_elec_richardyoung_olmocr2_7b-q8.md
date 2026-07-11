4.2 Electrical characteristics

@ Vdd = 1.8 V, T = 25 °C unless otherwise noted.

Table 4. Electrical characteristics

<table>
  <tr>
    <th>Symbol</th>
    <th>Parameter</th>
    <th>Test conditions</th>
    <th>Min.</th>
    <th>Typ.(1)</th>
    <th>Max.</th>
    <th>Unit</th>
  </tr>
  <tr>
    <td>Vdd</td>
    <td>Supply voltage</td>
    <td></td>
    <td>1.71</td>
    <td>1.8</td>
    <td>3.6</td>
    <td>V</td>
  </tr>
  <tr>
    <td>Vdd_IO</td>
    <td>Power supply for I/O</td>
    <td></td>
    <td>1.62</td>
    <td></td>
    <td>3.6</td>
    <td>V</td>
  </tr>
  <tr>
    <td>IddHP</td>
    <td>Gyroscope and accelerometer current consumption in high-performance mode</td>
    <td>ODR = 1.6 kHz</td>
    <td></td>
    <td>0.65</td>
    <td></td>
    <td>mA</td>
  </tr>
  <tr>
    <td>IddNM</td>
    <td>Gyroscope and accelerometer current consumption in normal mode</td>
    <td>ODR = 208 Hz</td>
    <td></td>
    <td>0.45</td>
    <td></td>
    <td>mA</td>
  </tr>
  <tr>
    <td>IddLP</td>
    <td>Gyroscope and accelerometer current consumption in low-power mode</td>
    <td>ODR = 52 Hz</td>
    <td></td>
    <td>0.29</td>
    <td></td>
    <td>mA</td>
  </tr>
  <tr>
    <td>LA_IddHP</td>
    <td>Accelerometer current consumption in high-performance mode</td>
    <td>ODR &lt; 1.6 kHz<br>ODR ≥ 1.6 kHz</td>
    <td></td>
    <td>150<br>160</td>
    <td></td>
    <td>μA</td>
  </tr>
  <tr>
    <td>LA_IddNM</td>
    <td>Accelerometer current consumption in normal mode</td>
    <td>ODR = 208 Hz</td>
    <td></td>
    <td>85</td>
    <td></td>
    <td>μA</td>
  </tr>
  <tr>
    <td>LA_IddLM</td>
    <td>Accelerometer current consumption in low-power mode</td>
    <td>ODR = 52 Hz<br>ODR = 12.5 Hz<br>ODR = 1.6 Hz</td>
    <td></td>
    <td>25<br>9<br>4.5</td>
    <td></td>
    <td>μA</td>
  </tr>
  <tr>
    <td>IddPD</td>
    <td>Gyroscope and accelerometer current consumption during power-down</td>
    <td></td>
    <td></td>
    <td>3</td>
    <td></td>
    <td>μA</td>
  </tr>
  <tr>
    <td>Ton</td>
    <td>Turn-on time</td>
    <td></td>
    <td></td>
    <td>35</td>
    <td></td>
    <td>ms</td>
  </tr>
  <tr>
    <td>V<sub>IH</sub></td>
    <td>Digital high-level input voltage</td>
    <td></td>
    <td>0.7 *VDD_IO</td>
    <td></td>
    <td></td>
    <td>V</td>
  </tr>
  <tr>
    <td>V<sub>IL</sub></td>
    <td>Digital low-level input voltage</td>
    <td></td>
    <td></td>
    <td>0.3 *VDD_IO</td>
    <td></td>
    <td>V</td>
  </tr>
  <tr>
    <td>V<sub>OH</sub></td>
    <td>High-level output voltage</td>
    <td>I<sub>OH</sub> = 4 mA (2)</td>
    <td>VDD_IO - 0.2</td>
    <td></td>
    <td></td>
    <td>V</td>
  </tr>
  <tr>
    <td>V<sub>OL</sub></td>
    <td>Low-level output voltage</td>
    <td>I<sub>OL</sub> = 4 mA (2)</td>
    <td></td>
    <td>0.2</td>
    <td></td>
    <td>V</td>
  </tr>
  <tr>
    <td>Top</td>
    <td>Operating temperature range</td>
    <td></td>
    <td>-40</td>
    <td></td>
    <td>+85</td>
    <td>°C</td>
  </tr>
</table>

1. Typical specifications are not guaranteed.
2. 4 mA is the maximum driving capability, i.e. the maximum DC current that can be sourced/sunk by the digital pad in order to guarantee the correct digital output voltage levels V<sub>OH</sub> and V<sub>OL</sub>.