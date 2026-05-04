import {
  type RealtimeTranslatePayload,
  type RealtimeTranslateResponse,
  type TranslateRevisionPayload,
  type TranslatePreviewStatusResponse,
  type TranslationStreamEvent,
  type TranslateWorkflowPayload,
  type TranslateWorkflowResponse,
} from "@/components/document-translation/types";

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "요청 처리 중 오류가 발생했습니다.");
  }
  return (await response.json()) as T;
}

export async function requestDocumentTranslation(
  payload: TranslateWorkflowPayload,
): Promise<TranslateWorkflowResponse> {
  const response = await fetch("/api/document-translation/translate", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return parseJson<TranslateWorkflowResponse>(response);
}

export async function requestDocumentTranslationStart(
  payload: TranslateWorkflowPayload,
): Promise<TranslateWorkflowResponse> {
  const response = await fetch("/api/document-translation/translate/start", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return parseJson<TranslateWorkflowResponse>(response);
}

export async function requestDocumentTranslationRevision(
  payload: TranslateRevisionPayload,
): Promise<TranslateWorkflowResponse> {
  const response = await fetch("/api/document-translation/translate/revise", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return parseJson<TranslateWorkflowResponse>(response);
}

export async function requestRealtimeTranslation(
  payload: RealtimeTranslatePayload,
): Promise<RealtimeTranslateResponse> {
  const response = await fetch("/api/document-translation/realtime", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return parseJson<RealtimeTranslateResponse>(response);
}

export async function requestDocumentPreviewStatus(
  jobId: string,
): Promise<TranslatePreviewStatusResponse> {
  const response = await fetch(`/api/document-translation/preview-status/${encodeURIComponent(jobId)}`, {
    method: "GET",
  });

  return parseJson<TranslatePreviewStatusResponse>(response);
}

export function createDocumentTranslationEventSource(jobId: string): EventSource {
  return new EventSource(`/api/document-translation/translate/events/${encodeURIComponent(jobId)}`);
}

export function parseTranslationStreamEvent(event: MessageEvent<string>): TranslationStreamEvent {
  return {
    event: event.type as TranslationStreamEvent["event"],
    data: JSON.parse(event.data) as TranslationStreamEvent["data"],
  };
}
