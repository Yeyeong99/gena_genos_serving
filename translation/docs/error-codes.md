# Translation Agent Error Codes

번역 에이전트는 SSE `error` 이벤트의 `data`를 아래 형식으로 반환한다.

```json
{
  "event": "error",
  "data": {
    "error_code": "08003002",
    "msg": "문서 번역 중 오류가 발생했습니다.",
    "errMsg": "문서 번역 중 오류가 발생했습니다.",
    "detail": "optional detail"
  }
}
```

`msg`가 표준 메시지 필드이며, `errMsg`는 기존 `agent-service` 번역 wrapper 호환을 위해 함께 내려준다.

`job_error` 이벤트에도 동일한 `error_code`와 `msg`가 포함된다.

## 코드 대역

- `08000xxx`: 입력·요청 검증
- `08001xxx`: LLM 호출
- `08002xxx`: 파일·프리뷰·스토리지
- `08003xxx`: 번역 파이프라인
- `08099xxx`: 알 수 없는 오류

## 정의

| Code | Name | Description |
| --- | --- | --- |
| `08000001` | `ERR_TRANSLATION_INPUT_VALIDATION` | 요청 payload 형식 오류 |
| `08000002` | `ERR_TRANSLATION_UNSUPPORTED_FORMAT` | 지원하지 않는 문서/번역 형식 |
| `08000003` | `ERR_TRANSLATION_JOB_NOT_FOUND` | 재시도/수정 대상 job 없음 |
| `08001001` | `ERR_TRANSLATION_LLM_CONTEXT_EXCEEDED` | 컨텍스트 길이 초과 |
| `08001002` | `ERR_TRANSLATION_LLM_BAD_REQUEST` | BadRequestError |
| `08001003` | `ERR_TRANSLATION_LLM_API_STATUS` | APIStatusError |
| `08001004` | `ERR_TRANSLATION_LLM_GENERAL` | LLM 일반 오류 |
| `08002001` | `ERR_TRANSLATION_FILE_DOWNLOAD_FAILED` | presigned_url 파일 다운로드 실패 |
| `08002002` | `ERR_TRANSLATION_FILE_NOT_FOUND` | 파일 경로/입력 파일 없음 |
| `08002003` | `ERR_TRANSLATION_PREVIEW_FAILED` | 원본/번역본 preview 생성 실패 |
| `08002004` | `ERR_TRANSLATION_UPLOAD_FAILED` | 결과 파일 업로드 실패 |
| `08003001` | `ERR_TRANSLATION_START_FAILED` | 스트리밍 번역 job 시작 실패 |
| `08003002` | `ERR_TRANSLATION_PIPELINE_FAILED` | Office 번역 파이프라인 실패 |
| `08003003` | `ERR_TRANSLATION_REVISION_FAILED` | 부분 재시도/수정 번역 실패 |
| `08003004` | `ERR_TRANSLATION_VALIDATION_FAILED` | 번역 응답 검증 실패 |
| `08099999` | `ERR_TRANSLATION_UNKNOWN` | 알 수 없는 오류 |
