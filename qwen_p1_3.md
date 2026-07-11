<!-- page 1 -->
LN08LPU
Design Manual
Version: A00-V0.9.2.0
2025-07-24

Design Manual

SAMSUNG ELECTRONICS RESERVES THE RIGHT TO CHANGE PRODUCTS, INFORMATION AND SPECIFICATIONS WITHOUT NOTICE.

Products and specifications discussed herein are for reference purposes only. All information discussed herein is provided on an "AS IS" basis, without warranties of any kind.

This document and all information discussed herein remain the sole and exclusive property of Samsung Electronics. No license of any patent, copyright, mask work, trademark or any other intellectual property right is granted by one party to the other party under this document, by implication, estoppel or otherwise.

Samsung products are not intended for use in life support, critical care, medical, safety equipment, or similar applications where product failure could result in loss of life or personal or physical harm, or any military or defense application, or any governmental procurement to which special terms or provisions may apply.

For updates or additional information about Samsung products, contact your nearest Samsung office.

All brand names, trademarks and registered trademarks belong to their respective owners.

© Samsung Electronics Co., Ltd. All rights reserved.
<!-- page 2 -->
LN08LPU_Design Manual_A00-V0.9.2.0
Samsung Confidential
Important Notice

Important Notice

Samsung Electronics Co. Ltd. ("Samsung") reserves the right to make changes to the information in this publication at any time without prior notice. All information provided is for reference purpose only. Samsung assumes no responsibility for possible errors or omissions, or for any consequences resulting from the use of the information contained herein.

This publication on its own does not convey any license, either express or implied, relating to any Samsung and/or third-party products, under the intellectual property rights of Samsung and/or any third parties.

Samsung makes no warranty, representation, or guarantee regarding the suitability of its products for any particular purpose, nor does Samsung assume any liability arising out of the application or use of any product or circuit and specifically disclaims any and all liability, including without limitation any consequential or incidental damages.

Customers are responsible for their own products and applications. "Typical" parameters can and do vary in different applications. All operating parameters, including "Typicals" must be validated for each customer application by the customer's technical experts.

Samsung products are not designed, intended, or authorized for use in applications intended to support or sustain life, or for any other application in which the failure of the Samsung product could reasonably be expected to create a situation where personal injury or death may occur. Customers acknowledge and agree that they are solely responsible to meet all other legal and regulatory requirements regarding their applications using Samsung products notwithstanding any information provided in this publication. Customer shall indemnify and hold Samsung and its officers, employees, subsidiaries, affiliates, and distributors harmless against all claims, costs, damages, expenses, and reasonable attorney fees arising out of, either directly or indirectly, any claim (including but not limited to personal injury or death) that may be associated with such unintended, unauthorized and/or illegal use.

WARNING No part of this publication may be reproduced, stored in a retrieval system, or transmitted in any form or by any means, electric or mechanical, by photocopying, recording, or otherwise, without the prior written consent of Samsung. This publication is intended for use by designated recipients only. This publication contains confidential information (including trade secrets) of Samsung protected by Competition Law, Trade Secrets Protection Act and other related laws, and therefore may not be, in part or in whole, directly or indirectly publicized, distributed, photocopied or used (including in a posting on the Internet where unspecified access is possible) by any unauthorized third party. Samsung reserves its right to take any and all measures both in equity and law available to it and claim full damages against any party that misappropriates Samsung's trade secrets and/or confidential information.

警告 本文件仅向经韩国三星电子株式会社授权的人员提供，其内容含有商业秘密保护相关法规规定并受其保护的三星电子株式会社商业秘密，任何直接或间接非法向第三人披露、传播、复制或允许第三人使用该文件全部或部分内容的行为（包括在互联网等公开媒介刊登该商业秘密而可能导致不特定第三人获取相关信息的行为）皆为法律严格禁止。此等违法行为一经发现，三星电子株式会社有权根据相关法规对其采取法律措施，包括但不限于提出损害赔偿请求。

Copyright © 2023-2025 Samsung Electronics Co., Ltd.
Samsung Electronics Co., Ltd.
1, Samsungjeonja-ro, Hwaseong-si,
Gyeonggi-Do, Korea 445-330
Contact Us: tom82.kim@samsung.com
TEL: +82-31-325-5191
Home Page: http://www.samsung.com/semiconductor

SAMSUNG ELECTRONICS
2
<!-- page 3 -->
LN08LPU_Design Manual_A00-V0.9.2.0
Samsung Confidential
Table of Contents

Table of Contents

List of Tables ... 12
1 About This Manual ... 16
    1.1 Terminology Used in This Manual ... 16
2 Technology Introduction ... 17
    2.1 Features ... 17
3 Physical Design Information ... 18
    3.1 Mask Level Table ... 18
    3.2 CAD Layer Tables ... 23
        3.2.1 Front-End-of-Line (FEOL) Design and Utility Levels CAD Layer Table ... 23
        3.2.2 Device Design and Utility Levels CAD Layer Table ... 25
        3.2.3 SRAM Design and Utility Levels CAD Layer Table ... 28
        3.2.4 Back-End-of-Line (BEOL) Utility Levels CAD Layer Table ... 29
        3.2.5 ESD Design and Utility Levels CAD Layer Table ... 39
        3.2.6 Embedded Metrology CAD Layer Table ... 40
        3.2.7 Reserved Design and Utility Levels CAD Layer Table ... 45
        3.2.8 Custom Fill CAD Layer Table ... 45
        3.2.9 General Design and Utility Levels CAD Layer Table ... 65
        3.2.10 Kerf Design and Utility Levels CAD Layer Table ... 66
        3.2.11 Data Preparation Level CAD Layer Table ... 72
        3.2.12 MRAM Design and Utility Levels CAD Layer Table ... 79
        3.2.13 Text Type Attributes CAD Layer Table ... 80
    3.3 Data Preparation Levels ... 84
    3.4 Kerf Design Levels ... 84
    3.5 Reference Levels ... 85
    3.6 Mask Alignment Sequence and Metallization Options ... 85
        3.6.1 Metal Stack Naming Convention ... 86
        3.6.2 BEOL Metallization Options ... 86
    3.7 Design Preparation ... 88
        3.7.1 Boolean Level Generation Keywords ... 88
    3.8 Truth Tables ... 90
        3.8.1 Design Truth Table ... 90

SAMSUNG ELECTRONICS
4