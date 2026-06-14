# Gena Reference Tags — 백엔드 / 프론트엔드 연동 가이드

LLM 최종 답변에는 일반 텍스트 토큰 사이에 **Gena Reference Tag**가 삽입됩니다.  
프론트엔드는 이 태그를 파싱해 웹 출처 카드, 이미지, 차트 등 리치 UI 컴포넌트로 렌더링합니다.

---

## 1. 태그 형식

id-form 은 LLM 컨텍스트·DB·SSE·history 까지 **모두 동일하게 흐릅니다.** ref_id → 데이터 변환은 LLM 컨텍스트 바깥의 두 지점에서만 일어납니다 (#640):

1. 이미지 도구 진입점 (`gen_image` / `edit_image`) — `states.tool_state.id_to_url[ref_id]` 로 URL 조회 후 외부 API 호출
2. frontend Gena Agent 컴포넌트 — `useGenaRef(id)` 가 세션 store 의 `toolResults[id]` 객체에서 자기 필드 추출 (`<GenaImage>` 는 `.url`, `<GenaWeb>` 은 `.title` / `.snippet` / `.url` / `.thumbnailUrl`)

| 태그 | 용도 | 형식 |
|------|------|------|
| `<GenaWeb id="…" />` | 웹 검색 결과 / 방문 페이지 인용 | `<GenaWeb id="2:3" />` |
| `<GenaImage id="…" />` | AI 생성·편집 이미지 | `<GenaImage id="img:1:0" />` |
| `<GenaChart id="…" />` | 차트 (향후 확장) | `<GenaChart id="chart:0:0" />` |

### ID 체계

```
{turn}:{idx}          웹 검색 결과 (예: "2:3")
img:{counter}:{idx}   생성/편집 이미지 — counter 는 cross-turn 영속 (`tool_state.img_counter`)
chart:{turn}:{idx}    차트
```

- `turn` — `States.turn` 값 (request 진입 시 0 으로 초기화 → 각 툴 호출 후 +1). **request scope** 이며 cross-turn 영속 안 됨
- `idx` — 해당 호출 내 결과 순서 (0부터)
- `open_url` 내부 링크도 `{turn}:{link_id}` 형식. LLM 본문 진행 중에는 `<GenaWeb id="…" label="…" domain="…" />` 로 보이고, 최종 답변에서는 `id` 만 사용

> ⚠️ **알려진 이슈 — `<GenaWeb>` cross-turn ID 충돌**: `States.turn` 이 매 request 0 으로 리셋되므로 이전 turn 에서 발급된 `<GenaWeb id="0:0" />` 와 새 request 의 `<GenaWeb id="0:0" />` 가 같은 키를 가집니다. frontend `toolResults` 맵은 session-wide 라 새 결과로 덮어씌워져 과거 인용이 잘못 해소될 수 있습니다. (이미지 도구는 `tool_state.img_counter` 로 cross-turn 고유성 보장 — 동일한 패턴을 web/open_url 에도 적용 예정.)

### Cross-turn 매핑 영속

agent-service `build_message_history` 가 메시지 트리에서 `tool_results` (ref_id → 풀 객체) 와 `img_counter` 를 누적해 `body.tool_state` 로 송신. genos `States.set_context` 가 복원해 다음 turn 의 `edit_image(ref_id)` URL 해소 + 새 `gen_image` 의 ref_id 충돌 방지를 보장합니다. (`turn` 자체는 복원하지 않음 — 위 알려진 이슈 참조.)

frontend 의 `useChatSessionStore.sessions[sid].toolResults` 는 두 경로로 채워집니다:
- 스트림 중: SSE `tool_state.tool_results` → `streamManager.onToolState` → `mergeToolResults`
- Reload 시: 메시지 트리의 `content.toolResults` 스냅샷을 `ChatSessionContainer` 가 hydrate

---

## 2. 백엔드 이벤트 흐름

백엔드는 HTTP POST → `StreamingResponse(text/event-stream)` 로 응답합니다.  
모든 SSE 라인은 다음 형식입니다.

```
data: {"event": "<이벤트명>", "data": <페이로드>}\n\n
```

하나의 요청에서 순서대로 발생하는 이벤트 목록:

```
session            세션 초기화 완료
tool_state         ← 매 LLM 호출 직전 emit (tool_state 섹션 참조)
  token            LLM 텍스트 청크 스트리밍
  usage            청크별 토큰 사용량
  reasoning_token  (추론 모델 전용) CoT 청크
tool_call          LLM이 툴을 호출함
  tool_result      툴 실행 성공 (data 구조는 툴마다 다름)
  tool_error       툴 실행 실패
assistant_reset    툴 루프 재진입 전 UI 리셋 신호
tool_state         (다음 LLM 호출 전 다시 emit)
  token / usage / …
follow_up_question 후속 질문 제안 (최대 3개)
title_summary      대화 제목 요약
usage_total        최종 누적 사용량
result             스트림 종료 마커
```

> **heartbeat**: 연결 유지를 위해 10초마다 `: keep-alive\n\n` 주석이 삽입됩니다. 이벤트가 아니므로 무시하세요.

---

## 3. `tool_state` 이벤트 상세

`tool_state`는 **LLM 호출 직전마다** emit 되며, 세션에서 지금까지 수집된 모든 리소스 매핑을 담고 있습니다. 프론트엔드는 이 이벤트를 받아 `id → URL` 조회 테이블을 갱신합니다.

### 페이로드 스키마

```jsonc
{
  "event": "tool_state",
  "data": {
    "id_to_url": {
      // ref_id → 실제 URL 매핑 (웹 결과, 이미지, 페이지 링크 모두 포함)
      "2:3": "https://example.com/article",
      "img:1:0": "https://cdn.openai.com/…/img.png",
      "3:0": "https://another.com/page"
    },
    "url_to_page": {
      // open_url 툴이 방문한 페이지 텍스트 캐시 (프론트에서는 통상 무시)
    },
    "current_url": "https://another.com/page",   // 현재 열려 있는 페이지 (없으면 null)
    "tool_results": {
      // ref_id → 툴 결과 전체 객체 (tool_result 이벤트와 동일한 내용 + 히스토리)
      "2:3": { "id": "2:3", "title": "…", "url": "…", "snippet": "…", … },
      "img:1:0": { "ref_id": "img:1:0", "url": "…", "prompt": "…", … }
    },
    "id_to_iframe": {}   // iframe 삽입이 필요한 경우 (차트에서 사용하는 iframe 담겨있음)
  }
}
```

### 핵심 필드 설명

| 필드 | 타입 | 설명 |
|------|------|------|
| `id_to_url` | `Record<string, string>` | **ref_id → URL** 조회 테이블. `<GenaWeb id="X" />` 를 렌더링할 때 이 맵에서 URL을 가져옵니다. |
| `tool_results` | `Record<string, object>` | ref_id별 전체 결과 객체. 웹 결과 카드에 표시할 `title`, `snippet`, `source`, `thumbnail` 등이 여기 있습니다. |
| `current_url` | `string \| null` | `open_url` 스크롤 컨텍스트. 프론트에서 "현재 읽는 중인 URL" 표시에 사용할 수 있습니다. |

### 갱신 타이밍

```
요청 시작
  ↓ tool_state (id_to_url = {})          ← 초기 빈 상태
  ↓ token×N                              LLM 스트리밍
  ↓ tool_call: web_search
  ↓ tool_result: web_search              클라이언트에 결과 데이터 전달
  ↓ tool_state (id_to_url = {"0:0":…})  ← 웹 검색 결과 추가
  ↓ token×N                              LLM이 <GenaWeb id="0:0" /> 포함해 답변
  ↓ result
```

`tool_state`는 누적 갱신입니다. 새 이벤트를 받으면 기존 맵에 머지(덮어쓰기)하면 됩니다.
