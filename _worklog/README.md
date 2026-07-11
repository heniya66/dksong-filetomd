# _worklog — 작업 타임라인 (자동 빌드형)

이 폴더는 프로젝트의 **작업·결정·산출물**을 비전문가도 이해하기 쉽게 단계적으로 보여주는 자료입니다.

## 구성

| 파일 | 역할 |
|------|------|
| `index.html` | 화면 (좌측 단계 타임라인 + 우측 내용) |
| `serve.py` | localhost 서버 (`python3 _worklog/serve.py` → 크롬 자동 열림) |
| `meta.json` | 덱 정보 (제목·부제·갱신일·프로젝트명) |
| `cards/NNN_*.json` | **카드 1장 = 1파일**. 여기만 추가/수정 |
| `build_worklog.py` | `cards/*.json` + `meta.json` → `slides_data.js` 자동 생성 |
| `slides_data.js` | **자동 생성물(직접 수정 금지)** |

## 갱신 (가장 쉬운 방법)

Claude Code에서 프로젝트 폴더에서 작업 후:

```
/worklog-add
```

→ Claude가 최근 작업을 **쉬운 말 카드**로 정리해 `cards/`에 추가하고 자동 빌드합니다.

## 수동 갱신

1. `cards/NNN_제목.json` 새로 작성 (아래 스키마)
2. `python3 _worklog/build_worklog.py` 실행
3. 브라우저 새로고침(F5)

### 카드 스키마

```json
{
  "group": "front | stage | appendix",
  "label": "목차에 보일 내용 (번호는 자동 부여)",
  "type": "cover | info | rich",
  "title": "카드 제목",
  "bullets": ["info 타입: 불릿 (HTML 태그 가능)"],
  "html": "rich 타입: 표 등 자유 HTML",
  "links": [{ "label": "링크 이름", "kind": "html|pdf|md|doc|link|video",
              "note": "보조설명", "href": "/경로" }]
}
```

- **group**: `front`(표지 뒤 안내) · `stage`(번호 본문, 자동 1·2·3…) · `appendix`(부록)
- **NNN**: 파일명 앞 숫자 = 정렬 순서(중간 삽입 시 010·020 사이에 015 식)
- 링크 `href`는 프로젝트 루트 기준 절대경로(`/docs/...`) 권장 (serve.py가 루트 서빙)

## 단계(stage) 권장 체계

`1단계 제안 → 2단계 검토·개선 → 3단계 실행 → 4단계 개발 → 5단계 검증·인증` …
새 작업은 해당 단계 뒤에 카드를 추가합니다. 큰 재정렬은 가끔만.

> 도구 원본: `~/workspace/_shared/worklog/` (init_worklog.py 가 이 폴더로 배포)
