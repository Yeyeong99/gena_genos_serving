# GenOS SaaS Code Serving API 명세서

프론트엔드는 문서 번역, 텍스트 번역, 수정 번역을 모두 GenOS SaaS Code Serving 단일 엔드포인트로 요청한다.

## 공통 호출 정보

| 항목 | 값 |
| --- | --- |
| Method | `POST` |
| Endpoint | `https://genos.genon.ai/api/gateway/code_serving/{CODE_SERVING_ID}/json` |
| 인증 | Bearer Token |
| 기본 응답 방식 | SSE (`text/event-stream`) |

### Header

```http
Authorization: Bearer <GENOS_TOKEN>
Content-Type: application/json
```

프론트 앱에서는 브라우저에 토큰을 노출하지 않고 Next API route에서 아래 서버 환경변수를 사용해 GenOS로 프록시한다.

| 환경변수 | 설명 |
| --- | --- |
| `DOC_TRANSLATION_CODE_SERVING_URL` | 전체 Code Serving URL. 예: `https://genos.genon.ai/api/gateway/code_serving/{CODE_SERVING_ID}/json` |
| `DOC_TRANSLATION_CODE_SERVING_ID` | URL 대신 ID만 지정할 때 사용 |
| `DOC_TRANSLATION_CODE_SERVING_TOKEN` | GenOS Bearer token |
| `GENOS_TOKEN` | `DOC_TRANSLATION_CODE_SERVING_TOKEN` 대신 사용할 수 있는 token fallback |

### 공통 Request Body 필드

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `format` | `string` | 필수 | 번역 대상 언어. 예: `Korean`, `English`, `Japanese`, `Chinese` |
| `style_options` | `object` | 선택 | 번역 스타일 옵션 |
| `is_return_file` | `boolean` | 선택 | 파일 번역 요청 기본값은 `true`. `true`면 최종 번역 파일을 `file_base64`로 반환 |
| `stream` | `boolean` | 선택 | 기본값은 streaming. `false`면 SSE가 아니라 최종 JSON만 반환 |

### style_options

| 필드 | 예시 값 | 설명 |
| --- | --- | --- |
| `purpose` | `presentation`, `business`, `default`, `casual_use` | 번역 목적 |
| `formality` | `formal`, `formal_hamnida`, `informal_friendly`, `eum_ham` | 문체/격식 |
| `terminology` | `preserve`, `preserve_key_terms`, `natural_translation`, `technical_terms` | 용어 처리 방식 |
| `script` | `simplified`, `traditional` | 중국어 번역 시 문자 체계 |

## 1. 문서 번역

파일은 presigned URL로 전달하는 방식을 권장한다.

`is_return_file`을 생략하면 기본값은 `true`다. 번역 파일 base64가 필요 없을 때만 명시적으로 `false`를 보낸다.

### Request Body

```json
{
  "sources": [
    {
      "presigned_url": "https://.../sample.xlsx?...",
      "metadata": {
        "file_name": "sample.xlsx"
      }
    }
  ],
  "format": "Korean",
  "is_return_file": true,
  "style_options": {
    "purpose": "presentation",
    "formality": "formal",
    "terminology": "preserve"
  }
}
```

### sources item

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `presigned_url` | `string` | 필수 | 번역할 원본 파일 다운로드 URL |
| `metadata.file_name` | `string` | 필수 권장 | 확장자 판별에 사용하는 원본 파일명 |

지원 파일 형식:

| 형식 | 확장자 |
| --- | --- |
| Word | `.docx` |
| PowerPoint | `.pptx` |
| Excel | `.xlsx` |
| PDF | `.pdf` |

### SSE Response 예시

```text
data: {"event":"token","data":"문서 번역을 시작합니다.\n"}

data: {"event":"agentFlowExecutedData","data":{"nodeLabel":"Document Translation","data":{"output":{"content":"{\"visible_rationale\":\"문서 번역 스트리밍 작업을 시작했습니다.\"}"}}}}

data: {"event":"agentFlowExecutedData","data":{"nodeLabel":"Document Translation Progress","data":{"output":{"content":"{\"visible_rationale\":\"시트 1/3 번역을 완료했습니다.\"}"}}}}

data: {"event":"result","data":{"job_id":"...","text":"...","file_base64":"...","output_filename":"sample_translated.xlsx","mime_type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}}
```

### result.data 주요 필드

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `job_id` | `string` | 수정 번역 요청에 사용할 번역 job ID |
| `text` | `string` | 번역된 텍스트 요약 또는 전체 텍스트 |
| `translation_status` | `string` | `done`, `error` 등 처리 상태 |
| `file_base64` | `string` | `is_return_file: true`일 때 반환되는 번역 파일 base64 |
| `output_filename` | `string` | 다운로드 시 사용할 파일명 |
| `mime_type` | `string` | 번역 파일 MIME type |

## 2. 텍스트 번역

### Request Body

```json
{
  "input_text": "Good morning. The meeting agenda includes revenue growth.",
  "format": "Korean",
  "style_options": {
    "purpose": "business",
    "formality": "formal",
    "terminology": "preserve"
  }
}
```

### SSE Response 예시

```text
data: {"event":"token","data":"좋은 아침입니다. 회의 안건에는 매출 성장..."}

data: {"event":"result","data":{"input_text":"Good morning. The meeting agenda includes revenue growth.","format":"Korean","text":"좋은 아침입니다. 회의 안건에는 매출 성장..."}}
```

## 3. 수정 번역

문서 번역 응답의 `result.data.job_id`를 사용한다. 수정 번역은 기존 번역 job의 컨텍스트를 재사용하므로, 기존 번역이 완료된 뒤 같은 Code Serving 환경에서 호출해야 한다.

### Request Body

```json
{
  "mode": "revise",
  "job_id": "기존_번역_job_id",
  "format": "Korean",
  "instruction": "전문적인 보고서 문체로 다듬어줘",
  "is_return_file": true
}
```

### 수정 번역 필드

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `mode` | `string` | 필수 | 수정 번역 요청은 반드시 `"revise"`로 지정 |
| `job_id` | `string` | 필수 | 기존 문서 번역 결과의 `result.data.job_id` |
| `format` | `string` | 필수 | 번역 대상 언어 |
| `instruction` | `string` | 선택 | 수정 방향 지시문 |
| `scope` | `object` | 선택 | 수정 범위. 생략 시 전체 문서 |
| `is_return_file` | `boolean` | 선택 | `true`면 수정된 번역 파일을 `file_base64`로 반환 |

### 지원 scope

`index`는 1부터 시작한다.

| 문서 | Scope |
| --- | --- |
| 전체 | 생략 또는 `{ "type": "document" }` |
| PPTX | `{ "type": "slide", "index": 2 }` |
| XLSX | `{ "type": "sheet", "index": 1 }` |
| XLSX | `{ "type": "sheet", "name": "Sheet1" }` |
| DOCX | `{ "type": "page", "index": 3 }` |

### SSE Response 예시

```text
data: {"event":"agentFlowExecutedData","data":{"nodeLabel":"Document Revision","data":{"output":{"content":"{\"visible_rationale\":\"수정 번역을 시작합니다.\"}"}}}}

data: {"event":"token","data":"수정 번역이 완료되었습니다."}

data: {"event":"result","data":{"job_id":"...","revision_status":"done","translation_status":"done","file_base64":"...","output_filename":"sample_translated.xlsx"}}
```

## 4. JSON 응답 방식

SSE가 아니라 최종 JSON만 받고 싶으면 `stream: false`를 넣는다.

### Request Body

```json
{
  "stream": false,
  "sources": [
    {
      "presigned_url": "https://.../sample.xlsx?...",
      "metadata": {
        "file_name": "sample.xlsx"
      }
    }
  ],
  "format": "Korean",
  "is_return_file": true,
  "style_options": {
    "purpose": "presentation",
    "formality": "formal",
    "terminology": "preserve"
  }
}
```

### JSON Response 예시

```json
{
  "job_id": "...",
  "text": "...",
  "file_base64": "...",
  "output_filename": "sample_translated.xlsx",
  "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
}
```

## 프론트엔드 호출 예시

### SSE 요청

```ts
const response = await fetch(
  `https://genos.genon.ai/api/gateway/code_serving/${CODE_SERVING_ID}/json`,
  {
    method: "POST",
    headers: {
      Authorization: `Bearer ${GENOS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      sources: [
        {
          presigned_url: filePresignedUrl,
          metadata: {
            file_name: file.name,
          },
        },
      ],
      format: "Korean",
      is_return_file: true,
      style_options: {
        purpose: "presentation",
        formality: "formal",
        terminology: "preserve",
      },
    }),
  },
);

if (!response.ok || !response.body) {
  throw new Error(`번역 요청 실패: ${response.status}`);
}

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";

while (true) {
  const { value, done } = await reader.read();
  if (done) break;

  buffer += decoder.decode(value, { stream: true });
  const chunks = buffer.split("\n\n");
  buffer = chunks.pop() ?? "";

  for (const chunk of chunks) {
    const line = chunk
      .split("\n")
      .find((item) => item.startsWith("data: "));

    if (!line) continue;

    const payload = JSON.parse(line.slice("data: ".length)) as {
      event: string;
      data: unknown;
    };

    if (payload.event === "token") {
      console.log("progress text", payload.data);
    }

    if (payload.event === "agentFlowExecutedData") {
      console.log("progress metadata", payload.data);
    }

    if (payload.event === "result") {
      console.log("final result", payload.data);
    }

    if (payload.event === "error") {
      console.error("translation error", payload.data);
    }
  }
}
```

### JSON 요청

```ts
const response = await fetch(
  `https://genos.genon.ai/api/gateway/code_serving/${CODE_SERVING_ID}/json`,
  {
    method: "POST",
    headers: {
      Authorization: `Bearer ${GENOS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      stream: false,
      sources: [
        {
          presigned_url: filePresignedUrl,
          metadata: {
            file_name: file.name,
          },
        },
      ],
      format: "Korean",
      is_return_file: true,
    }),
  },
);

const result = await response.json();
```

## 에러 응답

SSE 응답에서 오류가 발생하면 `error` 이벤트와 `result` 이벤트가 내려온다.

```text
data: {"event":"error","data":"문서 번역 처리 중 문제가 발생했습니다: ..."}

data: {"event":"result","data":{"success":false,"message":"..."}}
```

JSON 응답 방식에서는 일반 JSON body에 에러 메시지가 포함될 수 있다.
