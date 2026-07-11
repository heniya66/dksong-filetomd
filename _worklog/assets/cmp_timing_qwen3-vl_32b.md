# 음향 및 전기적 사양  
**MP34DT01-M**  

## 2.3 타이밍 특성  

### 표 5. 타이밍 특성  
| Parameter | Description                                      | Min.   | Max.    | Unit |
|-----------|--------------------------------------------------|--------|---------|------|
| f<sub>CLK</sub> | 정상 모드 클럭 주파수                            | 1      | 3.25    | MHz  |
| f<sub>PD</sub>  | 파워다운 모드 클럭 주파수                        |        | 0.23    | MHz  |
| T<sub>CLK</sub> | 정상 모드 클럭 주기                              | 308    | 1000    | ns   |
| T<sub>R,EN</sub> | DATA 라인 데이터 활성화 (L/R 핀 = 1)             | 18<sup>(1)</sup> |        | ns   |
| T<sub>R,DIS</sub>| DATA 라인 데이터 비활성화 (L/R 핀 = 1)            |        | 16<sup>(1)</sup> | ns   |
| T<sub>L,EN</sub> | DATA 라인 데이터 활성화 (L/R 핀 = 0)             | 18<sup>(1)</sup> |        | ns   |
| T<sub>L,DIS</sub>| DATA 라인 데이터 비활성화 (L/R 핀 = 0)            |        | 16<sup>(1)</sup> | ns   |

**1.** 설계 시뮬레이션에서 유래  

---

### 그림 3. 타이밍 웨이브폼  
- **신호 구성 및 특성**:  
  - **CLK (클럭 신호)**: 정방파 형태로 주기(T<sub>CLK</sub>)가 표시됨.  
  - **PDM R (오른쪽 채널 PDM 신호)**:  
    - L/R 핀이 1일 때 데이터 활성화 시간(T<sub>R,EN</sub>) 동안 신호 전달.  
    - 이후 비활성화 시간(T<sub>R,DIS</sub>) 동안 "High Z" 상태로 유지됨.  
  - **PDM L (왼쪽 채널 PDM 신호)**:  
    - L/R 핀이 0일 때 데이터 활성화 시간(T<sub>L,EN</sub>) 동안 신호 전달.  
    - 이후 비활성화 시간(T<sub>L,DIS</sub>) 동안 "High Z" 상태로 유지됨.  

- **타이밍 파라미터 관계**:  
  - T<sub>CLK</sub>: CLK의 한 주기 길이 (308~1000 ns).  
  - T<sub>R,EN</sub>, T<sub>L,EN</sub>: 각 채널에서 데이터가 활성화되는 최소 시간 (18 ns).  
  - T<sub>R,DIS</sub>, T<sub>L,DIS</sub>: 각 채널에서 데이터가 비활성화되는 최대 시간 (16 ns).  
  - "High Z" 상태: 신호 전달이 중단된 고 임피던스 상태.  

- **참고**:  
  - PDM R 및 PDM L 신호는 L/R 핀의 값(0 또는 1)에 따라 활성화/비활성화 시간이 결정됨.  
  - T<sub>R,EN</sub>과 T<sub>L,EN</sub>은 최소값만, T<sub>R,DIS</sub>와 T<sub>L,DIS</sub>는 최대값만 표기됨 (18(1) ns, 16(1) ns).  

---

**페이지**: 6/17  
**문서 ID**: DocID026514 Rev 3  
**제조사 로고**: ST (STMicroelectronics)