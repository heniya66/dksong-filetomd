### Figure 1: LSM6DSM의 핀 연결 및 감지 방향 설명  
**한 줄 요약 (쉬운 말 / one-line plain summary)**: 이 그림은 LSM6DSM 센서의 핀 배치와 가속도/각속도를 감지하는 방향을 보여주는 도면입니다.  
**Type**: pinout diagram (conceptual figure)  
**Caption (original)**: Figure 1. Pin connections  

**Components / Elements**:  
- "Direction of detectable acceleration (top view)" 라벨과 함께 X, Y, Z 축이 표시된 3D 블록 (X: 파란 화살표, Y: 빨간 화살표, Z: 초록 화살표)  
- "Direction of detectable angular rate (top view)" 라벨과 함께 Ω_v, Ω_r, Ω_p 축이 표시된 3D 블록  
- "BOTTOM VIEW" 라벨이 있는 IC 핀 배치도 (14개의 핀)  
  - 상단:  
    * 왼쪽: "12" -> CS  
    * 중앙: [번호 없음] -> SCL  
    * 오른쪽: "14" -> SDA  
  - 좌측:  
    * 위: "11" -> SDO_Aux  
    * 다음: [번호 없음] -> OCS_Aux  
    * 다음: [번호 없음] -> INT2  
    * 아래: "8" -> VDD  
  - 하단:  
    * 왼쪽: "7" -> GND  
    * 중앙: [번호 없음] -> GND  
    * 오른쪽: "5" -> VDDIO  
  - 우측:  
    * 위: "1" -> SDO/SA0  
    * 다음: [번호 없음] -> SDx  
    * 다음: [번호 없음] -> SCx  
    * 아래: "4" -> INT1  

**Relations / Connections**:  
- 이 그림은 연결 관계를 보여주지 않으며, 각 핀의 기능을 설명합니다.  
  - X, Y, Z 축은 가속도 감지 방향을 나타내며, Ω_v, Ω_r, Ω_p는 각속도 감지 방향을 나타냅니다.  
  - IC 핀 배치도에서 각 핀은 특정 기능 (예: CS, SCL, SDA 등)에 대응합니다.  

**Quantitative Data**: (not applicable for this figure)  

**Visual Encoding**:  
- 색상: X축(파랑), Y축(빨강), Z축(초록)  
- 화살표: 방향을 나타냄  

**Textual Content in Image**:  
- "Pin description"  
- "3 Pin description"  
- "Figure 1. Pin connections"  
- "Direction of detectable acceleration (top view)"  
- "Z", "Y", "X" (축 라벨)  
- "Ω_v", "Ω_r", "Ω_p" (각속도 축 라벨)  
- "Direction of detectable angular rate (top view)"  
- "BOTTOM VIEW"  
- "12", "CS", "SCL", "14", "SDA"  
- "11", "SDO_Aux", "OCS_Aux", "INT2", "8", "VDD"  
- "7", "GND", "GND", "5", "VDDIO"  
- "1", "SDO/SA0", "SDx", "SCx", "4", "INT1"  

**쉬운 설명 (비전문가용, 자세히 / easy detailed explanation)**:  
이 그림은 LSM6DSM 센서(가속도계와 각속도계가 하나로 합쳐진 소형 센서)의 핀 배치와 이 센서가 어떤 방향을 감지하는지 보여줍니다. 첫 번째 3D 그림은 가속도를 측정할 때 X, Y, Z 방향을 나타냅니다. X는 파란 화살표로 오른쪽, Y는 빨간 화살표로 위-오른쪽, Z는 초록 화살표로 위로 향합니다. 이 센서가 이 세 방향의 가속도(예: 전자기기에서 움직일 때 생기는 힘)를 측정할 수 있음을 알려줍니다. 두 번째 3D 그림은 각속도(회전 속도)를 측정하는 방향을 보여줍니다. Ω_v는 초록 화살표로 위, Ω_r은 빨간 화살표로 오른쪽, Ω_p는 파란 화살표로 앞으로 향합니다. 이 센서가 이 세 방향의 회전(예: 스마트폰을 돌릴 때 생기는 회전)을 측정할 수 있음을 알려줍니다. 그리고 오른쪽에 있는 "BOTTOM VIEW" 라벨이 붙은 그림은 이 센서의 핀 배치를 보여줍니다. 상단에서 12번 핀은 CS(칩 선택)로, 중앙에는 SCL(시계 신호), 14번 핀은 SDA(데이터 신호)입니다. 좌측에서 11번 핀은 SDO_Aux(보조 데이터 출력), 그 아래 두 개의 핀은 OCS_Aux와 INT2, 8번 핀은 VDD(전원)입니다. 하단에서 7번 핀과 중앙 핀이 GND(지선), 5번 핀은 VDDIO(입출력 전원)입니다. 우측에서 1번 핀은 SDO/SA0(데이터 출력/주소 선택), 그 아래 두 개의 핀은 SDx와 SCx, 4번 핀은 INT1(중단 신호)입니다. 이 센서는 여러 가지 기능을 수행하기 위해 다양한 핀들이 필요합니다. 예를 들어, CS, SCL, SDA는 데이터 전송에 사용되며, VDD와 GND는 전원 공급을 위한 것입니다. INT1과 INT2는 센서가 특정 이벤트(예: 가속도가 임계값을 넘었을 때)를 알리기 위해 사용됩니다.  

**용어 풀이 (Glossary)**:  
- CS(칩 선택): 센서를 활성화하거나 비활성화하는 신호  
- SCL(시계 신호): I2C 통신에서 데이터 전송의 타이밍을 제어하는 신호  
- SDA(데이터 신호): I2C 통신에서 데이터를 전송하는 신호  
- VDD: 센서에 공급되는 전원 (5V 또는 3.3V)  
- GND: 지선 (0V 참조, 전원의 기준점)  
- INT1, INT2: 중단 신호 (센서가 특정 이벤트를 감지했을 때 CPU에게 알리는 신호)  
- SDO/SA0: 데이터 출력 및 주소 선택 (다중 센서 연결 시 사용)  

**Interpretation / Purpose**:  
이 그림은 LSM6DSM 센서의 물리적 핀 배치와 각 핀의 기능을 설명하며, 또한 센서가 가속도와 각속도를 어떤 방향으로 측정하는지 보여줍니다. 이 정보는 회로 설계 시 센서를 올바르게 연결하고, 데이터를 정확히 읽어들이기 위해 필요합니다. 특히, SCL/SDA 핀은 I2C 통신을 통해 컴퓨터와 데이터를 주고받는 데 사용되며, VDD/GND는 전원 공급을 위한 필수 핀입니다.
