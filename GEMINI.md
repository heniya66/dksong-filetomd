> ⚠️ DEPRECATED — 현행 운영 구조는 GEMINI_MULTIMODAL_MANUAL.md / config.yaml / lib/ollama_extractor.py 참조 (ollama_cloud 기본)
>
> 이 문서는 ollama_cloud 전환(2026-05-29) 이전 작성된 레거시 지침입니다. 아래 "Gemini Multimodal 우선",
> "기본 모델 gemini-2.5-flash", "API 키 코드 참조" 항목은 더 이상 유효하지 않습니다. 현행 동작은
> ① EXTRACT_PROVIDER=ollama_cloud(localhost:11434, gemini-3-flash-preview, 키 불필요) 기본,
> ② EXTRACT_PROVIDER=gemini opt-in fallback(이때만 GEMINI_API_KEY Keychain 사용),
> ③ VISION_QA=claude_cli 2차 보정 입니다.

# Document to Markdown Workspace Mandates

이 워크스페이스에서 작업할 때 다음 규칙을 항상 준수하십시오.

## PDF 및 문서 변환 규칙

1.  **Gemini Multimodal 우선**: PDF, 이미지, 복합 문서 변환 시 항상 Gemini Multimodal 추출 기술을 사용합니다.
2.  **기본 모델**: `models/gemini-2.5-flash` 모델을 기본으로 사용합니다. (2.0-flash가 사용 불가능할 경우)
3.  **추출 품질 지침**:
    - 텍스트뿐만 아니라 차트, 그래프의 데이터 트렌드와 X/Y축 값을 상세히 분석하여 마크다운 블록쿼트(`>`) 내에 기술합니다.
    - 표는 항상 GFM Pipe 형식을 유지합니다.
    - 제품 이미지나 레이아웃의 디자인 컨셉을 텍스트로 묘사합니다.
4.  **스크립트 참조**: 변환 작업 시 `.agents/skills/gemini-pdf-to-md/scripts/gemini_pdf_converter.py` 또는 `extract_gemini_multimodal.py`의 로직을 우선 참조하십시오.
5.  **API 키**: 환경 변수에 설정된 키가 없을 경우, `extract_gemini_multimodal.py`에 명시된 API 키를 안전하게 참조하여 작업을 수행합니다.

---
*이 문서는 이 워크스페이스 내 모든 작업의 최우선 지침입니다.*
