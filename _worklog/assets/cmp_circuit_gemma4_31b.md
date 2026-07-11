제시된 이미지는 **STEVAL-BCNKT01V1** 평가 보드의 회로도 중 일부(sheet 2)이며, Figure 3와 Figure 4 두 개의 섹션으로 구성되어 있습니다. 요청하신 대로 이미지의 모든 정보를 상세히 한국어로 정리하여 Markdown 형식으로 작성합니다.

---

# STEVAL-BCNKT01V1 회로도 분석 보고서 (Sheet 2)

## 1. Figure 3: STM32F446MEY6 회로도 (2 of 4)
이 섹션은 메인 컨트롤러인 MCU(STM32F446MEY6)와 그 주변 전원, 클럭, 리셋 및 상태 표시 LED 회로를 포함합니다.

### 1.1 주요 부품 정보
*   **MCU:** STM32F446MEY6 (Main Microcontroller)
*   **크리스탈(Crystal):** X1 (외부 클럭 소스)
*   **LED:** LED0, LED1, LED2, LED3 (상태 표시용)

### 1.2 전원 및 디커플링 회로 (Power & Decoupling)
MCU의 안정적인 동작을 위해 VDD와 VDDA