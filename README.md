# GenA Translation

GenA 문서 번역 로컬 실행용 저장소입니다. Next.js 프론트엔드에서 파일을 업로드하고, FastAPI 백엔드가 DOCX/PPTX/XLSX 문서를 추출, 번역, 미리보기, 다운로드 형태로 처리합니다. 백엔드에는 PDF 파이프라인도 포함되어 있지만, 현재 프론트엔드 파일 선택 UI는 DOCX/PPTX/XLSX 업로드를 받습니다.

## 실행 포트

| 구분 | 실행 위치 | 기본 포트 | URL |
| --- | --- | --- | --- |
| Frontend | `frontend/apps/client` | `3000` | `http://localhost:3000` |
| Backend | 저장소 루트 | `8000` | `http://127.0.0.1:8000` |

프론트엔드는 Next.js API Route를 통해 백엔드로 요청을 프록시합니다. 로컬 백엔드를 사용할 때는 프론트엔드 환경변수 `DOC_TRANSLATION_BACKEND_URL=http://127.0.0.1:8000`가 필요합니다.

`./run-fastapi-local.sh`는 백엔드만 실행합니다. 처음 클론한 팀원은 먼저 Python 의존성을 설치해야 하고, 화면까지 보려면 프론트엔드 의존성 설치와 `frontend/apps/client/.env.local` 생성 후 `npm run dev`도 별도 터미널에서 실행해야 합니다.

## 사전 준비

- Git
- Node.js 20 이상 권장
- npm
- Python 3.11 이상 권장
- LibreOffice. DOCX/XLSX HTML preview와 PPTX PDF->PNG 이미지 preview 생성에 사용합니다.

macOS 예시:

```bash
brew install --cask libreoffice
```

설치 후 `soffice --version` 또는 `/Applications/LibreOffice.app/Contents/MacOS/soffice --version`으로 실행 가능 여부를 확인합니다. 필요하면 `LIBREOFFICE_BIN=/Applications/LibreOffice.app/Contents/MacOS/soffice`처럼 실행 파일 경로를 직접 지정할 수 있습니다.

LLM 번역 API는 `.env` 또는 쉘 환경변수로 설정합니다. 값이 없으면 코드의 기본값을 사용하지만, 팀별/개인별 토큰이 있다면 아래처럼 명시하는 것을 권장합니다.

```bash
# OpenAI-compatible endpoint를 직접 사용할 때
MODEL_API_BASE_URL=https://your-openai-compatible-endpoint/v1
MODEL_API_KEY=your-api-key
DEFAULT_TRANSLATION_MODEL=your-model

# GenOS serving endpoint를 사용할 때
GENOS_URL=https://genos.genon.ai/api/gateway/
SERVING_ID=676
BEARER_TOKEN=your-token
MODEL_NAME=qwen/qwen3.5-397b-a17b

# 선택 사항
DEEPSEEK_ID=655
DEEPSEEK_KEY=your-token
DEEPSEEK_NAME=deepseek/deepseek-r1-0528
GPTOSS_ID=589
GPTOSS_KEY=your-token
GPROSS_NAME=openai/gpt-oss-120b
LLM_RETRY_COUNT=2
MODEL_TEMP=0.3
MAX_TOKENS=16384
```

실제 LLM 호출 없이 흐름만 확인하고 싶다면 백엔드 실행 시 mock 모드를 사용할 수 있습니다.

```bash
AI_TRANSLATION_TRANSLATOR_MODE=mock ./run-fastapi-local.sh
```

## 처음 클론한 뒤 실행하기

### 1. 저장소 클론

```bash
git clone <repo-url>
cd gena_translation
```

### 2. 백엔드 설치 및 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
./run-fastapi-local.sh
```

백엔드는 `http://127.0.0.1:8000`에서 실행됩니다. 이후부터는 의존성이 이미 설치되어 있다면 `source .venv/bin/activate` 후 `./run-fastapi-local.sh`만 다시 실행하면 됩니다. 상태 확인:

```bash
curl http://127.0.0.1:8000/health
```

정상 실행 시 다음 응답이 옵니다.

```json
{"status":"ok"}
```

### 3. 프론트엔드 설치 및 실행

새 터미널을 열고 실행합니다.

```bash
cd frontend/apps/client
npm install
```

`frontend/apps/client/.env.local` 파일을 만들고 로컬 백엔드 주소를 넣습니다.

```bash
DOC_TRANSLATION_BACKEND_URL=http://127.0.0.1:8000
```

개발 서버를 실행합니다.

```bash
npm run dev
```

프론트엔드는 `http://localhost:3000`에서 실행됩니다.

## 실행 방식 요약

최초 1회 설치 후에는 아래처럼 두 터미널을 열어 실행합니다.

백엔드 터미널:

```bash
cd gena_translation
source .venv/bin/activate
./run-fastapi-local.sh
```

프론트엔드 터미널:

```bash
cd gena_translation/frontend/apps/client
npm run dev
```

## 주요 환경변수

### 백엔드

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `GENOS_URL` | `https://genos.genon.ai/api/gateway/` | GenOS API Gateway 주소 |
| `SERVING_ID` | `676` | 기본 번역 모델 serving id |
| `BEARER_TOKEN` | 코드 기본값 | 기본 번역 모델 인증 토큰 |
| `MODEL_NAME` | `qwen/qwen3.5-397b-a17b` | 기본 모델 이름 |
| `MODEL_API_BASE_URL` | 없음 | OpenAI-compatible API endpoint를 직접 사용할 때의 base URL |
| `MODEL_API_KEY` | 없음 | OpenAI-compatible API key |
| `DEFAULT_TRANSLATION_MODEL` | `DEFAULT_LIGHT_MODEL` 또는 기본 모델 | 번역에 사용할 모델명 |
| `MAX_TOKENS` | `16384` | LLM 응답 최대 토큰 수 |
| `DEEPSEEK_ID`, `DEEPSEEK_KEY`, `DEEPSEEK_NAME` | 코드 기본값 | 선택 모델 1 설정 |
| `GPTOSS_ID`, `GPTOSS_KEY`, `GPROSS_NAME` | 코드 기본값 | 선택 모델 2 설정 |
| `AI_TRANSLATION_TRANSLATOR_MODE` | `llm` | `mock`으로 설정하면 LLM 호출 없이 테스트 가능 |
| `AI_TRANSLATION_PREVIEW_ROOT` | OS temp 폴더 아래 `ai_translation_previews` | preview 정적 파일 저장 위치 |
| `LIBREOFFICE_BIN` | 자동 탐색 | LibreOffice `soffice` 실행 파일 경로. 자동 탐색이 실패할 때 지정 |
| `AI_TRANSLATION_OFFICE_STREAM_LLM_CONCURRENCY` | `20` | Office 문서 스트리밍 번역 동시성 |
| `AI_TRANSLATION_PPTX_STREAM_LLM_CONCURRENCY` | `4` | PPTX slide 순차 처리용 번역 동시성 |
| `AI_TRANSLATION_PPTX_STREAM_PREVIEW_FLUSH_SLIDES` | `1` | PPTX 번역 중간 preview 갱신 슬라이드 간격 |
| `AI_TRANSLATION_DOCX_ESTIMATED_CHARS_PER_PAGE` | `1800` | DOCX estimated page 계산용 페이지당 글자 수 |
| `AI_TRANSLATION_DOCX_PROGRESSIVE_CHAR_THRESHOLD` | `18000` | DOCX 중간 preview를 켤 최소 글자 수 |
| `AI_TRANSLATION_DOCX_MAX_ITEMS_PER_BATCH` | `12` | DOCX 문맥 번역 배치 최대 항목 수 |
| `AI_TRANSLATION_DOCX_MAX_CHARS_PER_BATCH` | `6000` | DOCX 문맥 번역 배치 최대 글자 수 |
| `AI_TRANSLATION_PPTX_MAX_ITEMS_PER_BATCH` | `24` | PPTX 문맥 번역 배치 최대 항목 수 |
| `AI_TRANSLATION_PPTX_MAX_CHARS_PER_BATCH` | `9000` | PPTX 문맥 번역 배치 최대 글자 수 |
| `AI_TRANSLATION_XLSX_MAX_ITEMS_PER_BATCH` | `24` | XLSX 표 문맥 번역 배치 최대 항목 수 |
| `AI_TRANSLATION_XLSX_MAX_CHARS_PER_BATCH` | `9000` | XLSX 표 문맥 번역 배치 최대 글자 수 |
| `AI_TRANSLATION_DISABLE_THINKING` | `0` | 지원 모델에서 thinking 비활성화 옵션을 보낼 때 사용 |

### 프론트엔드

| 이름 | 필수 여부 | 설명 |
| --- | --- | --- |
| `DOC_TRANSLATION_BACKEND_URL` | 로컬 실행 시 필수 | FastAPI 백엔드 URL. 예: `http://127.0.0.1:8000` |
| `DOC_TRANSLATION_WORKFLOW_URL` | 선택 | 로컬 백엔드 대신 원격 문서 번역 workflow를 호출할 때 사용 |
| `DOC_TRANSLATION_WORKFLOW_TOKEN` | 원격 workflow 사용 시 필수 | 문서 번역 workflow 인증 토큰 |
| `DOC_TRANSLATION_REALTIME_WORKFLOW_URL` | 선택 | 원격 실시간 번역 workflow URL |
| `DOC_TRANSLATION_REALTIME_WORKFLOW_TOKEN` | 원격 workflow 사용 시 필수 | 실시간 번역 workflow 인증 토큰 |

## 폴더 구조

```text
.
├── app.py                          # SaaS/GenOS serving용 service(config, data) wrapper
├── fastapi_app.py                  # FastAPI 앱, CORS, API route, preview 정적 파일 서빙
├── translation_ochestration.py     # 파일 확장자별 번역 파이프라인 라우팅
├── translation_pipeline/           # 문서 추출/번역/저장 파이프라인 구현
│   ├── common/                     # LLM 호출, preview, node 처리, job/event 공통 로직
│   ├── office/                     # DOCX/PPTX/XLSX 처리 및 LibreOffice preview 로직
│   └── pdf/                        # PDF 처리 로직
├── frontend/
│   └── apps/client/                # Next.js 프론트엔드 앱
│       ├── src/app/                # App Router 페이지와 API Route
│       ├── src/components/         # 문서 번역 UI 컴포넌트
│       ├── src/api/                # 클라이언트 API 호출 함수
│       ├── src/hooks/              # 번역/다운로드 관련 React hook
│       ├── src/store/              # Zustand 상태 관리
│       └── src/design-system/      # 공통 UI 컴포넌트
├── requirements.fastapi.txt        # 백엔드 Python 의존성
├── run-fastapi-local.sh            # 로컬 FastAPI 실행 스크립트
├── test/resources/                 # 테스트용 샘플 문서
└── skills/                         # 프로젝트 참고용 작업 지침/문서
```

## 주요 API

백엔드 로컬 주소 기준:

| Method | Path | 설명 |
| --- | --- | --- |
| `GET` | `/health` | 백엔드 상태 확인 |
| `POST` | `/api/document-translation/translate` | 문서 번역 요청 |
| `POST` | `/api/document-translation/translate/start` | 스트리밍 문서 번역 job 시작 |
| `POST` | `/api/document-translation/translate/genos-stream` | GenOS UI 호환 문서 번역 SSE 스트림 |
| `GET` | `/api/document-translation/translate/events/{job_id}` | 번역 진행 이벤트 SSE |
| `GET` | `/api/document-translation/preview-status/{job_id}` | preview 생성 상태 조회 |
| `POST` | `/api/document-translation/realtime` | 실시간 텍스트 번역 |
| `GET/POST` | `/api/gateway/workflow/{workflow_id}/run/v2` | GenOS workflow 호환 endpoint |

GenOS Python Step에서 직접 스트리밍을 받을 때는 `/api/document-translation/translate/genos-stream`에 POST합니다. 각 청크는 `data: {"event": string, "data": any}` 형식이며, 진행 상태는 `agentFlowExecutedData`의 `visible_rationale`, 완료 preview 링크는 `visible_url`, 최종 완료 신호는 `result`로 전송됩니다.

## 개발용 검사 명령

프론트엔드:

```bash
cd frontend/apps/client
npm run lint
npm run typecheck
```

백엔드:

```bash
source .venv/bin/activate
python -m compileall app.py fastapi_app.py translation_ochestration.py translation_pipeline
```

## 문제 해결

- `DOC_TRANSLATION_BACKEND_URL 환경변수가 설정되지 않았습니다.`: `frontend/apps/client/.env.local`에 `DOC_TRANSLATION_BACKEND_URL=http://127.0.0.1:8000`를 추가한 뒤 `npm run dev`를 다시 실행합니다.
- LibreOffice 실행 파일을 찾을 수 없음: `brew install --cask libreoffice`로 설치한 뒤 백엔드를 다시 실행합니다. 자동 탐색이 실패하면 `LIBREOFFICE_BIN=/Applications/LibreOffice.app/Contents/MacOS/soffice`를 지정합니다.
- PPTX preview가 느림: PPTX는 LibreOffice로 PDF 변환 후 PNG를 생성합니다. 중간 preview 갱신 빈도는 `AI_TRANSLATION_PPTX_STREAM_PREVIEW_FLUSH_SLIDES`로 조절합니다.
- LLM API 호출 실패: `.env`의 토큰/serving id/model name과 사내 네트워크 접근 가능 여부를 확인합니다. 로컬 UI만 확인할 때는 `AI_TRANSLATION_TRANSLATOR_MODE=mock ./run-fastapi-local.sh`로 실행합니다.
