제공된 이미지는 **LSM6DSM** 반도체 소자의 **핀 설명(Pin description)** 섹션입니다. 이미지에 포함된 모든 정보를 상세히 정리하여 다음과 같이 구성하였습니다.

---

# LSM6DSM 기술 문서: 핀 설명 (Pin description)

## 3. 핀 설명 (Pin description)

### Figure 1. 핀 연결 (Pin connections)

본 섹션에서는 소자의 좌표계 방향과 물리적인 핀 배치(Bottom View 기준)를 정의합니다.

#### 1. 감지 방향 (Coordinate Systems)
소자의 상면도(Top view) 기준으로 가속도와 각속도의 측정 방향은 다음과 같습니다.

*   **가속도 감지 방향 (Direction of detectable acceleration):**
    *   **X축:** 수평 방향의 한 축
    *   **Y축:** 수평 방향의 다른 한 축 (X축과 직교)
    *   **Z축:** 소자 수직 상방향
*   **각속도 감지 방향 (Direction of detectable angular rate):**
    *   **$\Omega_x$**: X축을 중심으로 하는 회전 성분
    *   **$\Omega_y$**: Y축을 중심으로 하는 회전 성분
    *   **$\Omega_z$**: Z축을 중심으로 하는 회전 성분

#### 2. 핀 배치 (Pinout - BOTTOM VIEW)
소자 바닥면(Bottom View) 기준의 핀 번호와 신호 이름은 다음과 같습니다.

| 핀 번호 | 신호명 | 비고 |
| :---: | :--- | :--- |
| **1** | SDO/SA0 | |
| **2** | SDx | |
| **3** | SCx | |
| **4** | INT1 | |
| **5** | VDDIO | |
| **6** | GND | |
| **7** | GND | |
| **8** | VDD | |
| **9** | INT2 | |
| **10** | OCS_Aux | |
| **11** | SDO_Aux | |
| **12** | $\overline{\text{CS}}$ | Chip Select (Active Low) |
| **13** | SCL | Serial Clock |
| **14** | SDA | Serial Data |

---
**문서 정보:**
*   **페이지:** 20/126
*   **문서 번호:** DocID028165 Rev 7
*   **제조사:** STMicroelectronics (ST 로고 표시)