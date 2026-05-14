# agents/genos — GenOS 에이전트 하네스

GenON AI의 멀티모드 AI 에이전트 서버. FastAPI + SSE 기반 비동기 스트리밍 아키텍처.

---

## 역할 & 모드

| 모드 | 엔드포인트 | 설명 |
|------|-----------|------|
| `basic` | `POST /v1/chat/basic` | 일반 Q&A (웹 검색·RAG·이미지·차트) |
| `research` | `POST /v1/chat/research` | PLAN → EXECUTE → SYNTHESIZE 3단계 심층 리서치 |
| `slide` | `POST /v1/chat/slide` | HTML 기반 프레젠테이션 생성·편집 (파일시스템) |
| `image` | `POST /v1/chat/image` | 이미지 생성·편집 |
| `/json` | `POST /json` | 레거시 호환 (body의 `mode`로 내부 라우팅) |

---

## 디렉토리 구조

```
agents/genos/
├── app.py                    # FastAPI ASGI 진입점 (uvicorn, port 5599)
├── service.py                # 모드 라우팅 · 서비스 조정
├── services/
│   ├── chat_base.py          # 추상 베이스 (공통 로직)
│   ├── chat_basic.py         # basic 모드
│   ├── chat_research.py      # research 3단계 파이프라인
│   ├── chat_slide.py         # slide 모드
│   ├── chat_image.py         # image 모드
│   └── schemas_filecards.py  # 파일카드 Pydantic 모델
├── tools/
│   ├── __init__.py           # CHAT_TOOLS / SLIDE_TOOLS / RESEARCH_TOOLS / IMAGE_TOOLS 셋 정의
│   ├── base.py               # ToolRegistry + 베이스 클래스
│   └── *.py                  # 개별 도구 (25개)
├── utils/
│   ├── llm.py                # call_llm_stream() — 스트리밍 + 토큰 추적
│   ├── stream.py             # SSE create_sse_response()
│   ├── states.py             # States · ToolState 데이터클래스
│   ├── prompts.py            # load_prompts() — Jinja2 버전 관리
│   ├── session_logger.py     # events.jsonl + metadata.json
│   ├── workspace.py          # 슬라이드 파일시스템 관리
│   ├── pricing.py            # gena_credit_usage 계산
│   └── ...
├── stores/
│   └── session_store.py      # Redis (폴백: 인메모리)
├── prompts/                  # 버전 관리 프롬프트 (이름-버전.txt, 32개)
└── logs/
    ├── chat_sessions/        # 세션 이벤트 로그 (SESSION_LOGGING=true 시)
    └── genos_workspace/      # 슬라이드 작업 디렉토리
```

---

## 실행

```bash
# 의존성 설치
uv sync

# 로컬 실행 (port 5599, 핫 리로드)
python app.py

# 로컬 풀스택 연동 (infra docker-compose + genos 로컬 분리)
cp .env.local.fullstack .env.local
python app.py
# → services/agent-service/.env.local 의 *_AGENT_URL을 host.docker.internal:5599 로 변경
```

---

## 환경변수 (.env.local)

| 키 | 설명 |
|----|------|
| `MODEL_API_BASE_URL` / `MODEL_API_KEY` | GenON LLM API |
| `DEFAULT_MODEL` | 기본 모델 (현재: `moonshotai/kimi-k2.6`) |
| `DEFAULT_LIGHT_MODEL` | 경량 모델 |
| `DEFAULT_VLM_MODEL` / `DEFAULT_RESEARCH_MODEL` / `DEFAULT_SLIDE_MODEL` | 모드별 모델 오버라이드 |
| `IMAGE_MODEL_API_KEY` / `IMAGE_MODEL_API_BASE_URL` | 이미지 생성 API |
| `DEFAULT_IMAGE_GEN_MODEL` / `DEFAULT_IMAGE_EDIT_MODEL` | 이미지 모델 |
| `SEARCHAPI_KEY` | 웹 검색 (SearchAPI.io) |
| `UNSPLASH_ACCESS_KEY` | 이미지 검색 |
| `REDIS_URL` | 세션 저장소 (없으면 인메모리 폴백) |
| `SLIDE_WORKSPACE_DIR` | 슬라이드 파일 저장 경로 |
| `SESSION_LOGGING` | `true` 시 logs/ 에 JSONL 기록 |
| `RESEARCH_MIN_SUB_GOALS` / `RESEARCH_MAX_SUB_GOALS` | 리서치 서브 목표 수 |
| `MAX_SLIDE_LIMIT` | 슬라이드 최대 장 수 (기본 15) |
| `MAIN_CHAT_TEMPERATURE` | LLM 온도 (기본 0.7) |

---

## 핵심 패턴

### SSE 이벤트 흐름

```
session → tool_state → token... → usage → tool_call → tool_result → tool_state
→ token... → follow_up_question → title_summary → usage_total → gena_credit_usage → result
```

### 새 도구 추가

1. `tools/<도구명>.py` 작성 — `BaseTool` 상속, `Pydantic` 입력 모델 정의
2. `tools/__init__.py` 의 적절한 셋(`CHAT_TOOLS` 등)에 이름 추가
3. `ToolRegistry`가 자동 등록 (import 시)

### 새 프롬프트 추가

`prompts/<이름>-<버전>.txt` 로 저장 → `load_prompts(["<이름>"])` 호출 시 자동 최신 버전 로드.

### 세션 상태

- `States` 데이터클래스가 단일 요청의 전체 상태 보유 (메시지 히스토리, ToolState, WorkspaceManager)
- 멀티턴은 Redis(`session_id` 키)에서 `messages` 복구

---

## 도구 셋

| 셋 | 포함 도구 |
|----|----------|
| `CHAT_TOOLS` | web_search, open_url, rag_search, gen_image, edit_image, gen_chart, document_summarize, bio |
| `RESEARCH_TOOLS` | web_search, open_url |
| `SLIDE_TOOLS` | initialize_presentation, write_slide, read_slide, delete_slide, reorder_slides, str_replace_edit, grep_slide, set_working_dir, register_assets, image_search, web_search |
| `IMAGE_TOOLS` | gen_image, edit_image |

---

## 주의 사항

- Python **3.12+** 필수 (`uv` 권장)
- Redis 없어도 동작하나 멀티턴 세션 상태 유실됨
- 슬라이드는 HTML 파일시스템 기반 — `SLIDE_WORKSPACE_DIR` 경로 반드시 존재해야 함
- LLM 모델은 모두 환경변수로 교체 가능 (런타임 전환 지원)
- 인증 없음 — 상위 `services/agent-service` 에서 처리
