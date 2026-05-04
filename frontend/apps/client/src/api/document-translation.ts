import {
  type RealtimeTranslatePayload,
  type RealtimeTranslateResponse,
  type TranslateRevisionPayload,
  type TranslateWorkflowPayload,
  type TranslateWorkflowResponse,
} from "@/components/document-translation/types";

export type GenosStreamEvent = {
  event: string;
  data: unknown;
};

const GENOS_STREAM_TIMEOUT_MS = 10 * 60 * 1000;

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "요청 처리 중 오류가 발생했습니다.");
  }
  return (await response.json()) as T;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

async function parseGenosStream<T>(
  response: Response,
  onEvent?: (event: GenosStreamEvent) => void,
): Promise<T> {
  if (!response.ok || !response.body) {
    const text = await response.text();
    throw new Error(text || "GenOS 스트림 요청에 실패했습니다.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: unknown = null;
  let errorMessage = "";

  const handleChunk = (chunk: string) => {
    const dataLines = chunk
      .split("\n")
      .map((line) => line.trimEnd())
      .filter((line) => line.startsWith("data: "));

    for (const line of dataLines) {
      const rawPayload = line.slice("data: ".length);
      const parsed = JSON.parse(rawPayload) as GenosStreamEvent;
      onEvent?.(parsed);

      if (parsed.event === "error") {
        errorMessage = typeof parsed.data === "string" ? parsed.data : JSON.stringify(parsed.data);
      }
      if (parsed.event === "result") {
        result = parsed.data;
      }
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      if (chunk.trim()) {
        handleChunk(chunk);
      }
    }
  }

  if (buffer.trim()) {
    handleChunk(buffer);
  }

  if (isRecord(result)) {
    return result as T;
  }

  if (errorMessage) {
    throw new Error(errorMessage);
  }

  throw new Error("GenOS 스트림에서 최종 result 이벤트를 받지 못했습니다.");
}

function isAbortError(error: unknown) {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

async function requestGenosStream<T>(
  url: string,
  payload: unknown,
  onEvent?: (event: GenosStreamEvent) => void,
): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => {
    controller.abort();
  }, GENOS_STREAM_TIMEOUT_MS);

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    return await parseGenosStream<T>(response, onEvent);
  } catch (error) {
    if (isAbortError(error)) {
      throw new Error("GenOS 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.");
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
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
  onEvent?: (event: GenosStreamEvent) => void,
): Promise<TranslateWorkflowResponse> {
  return requestGenosStream<TranslateWorkflowResponse>(
    "/api/document-translation/translate/start",
    payload,
    onEvent,
  );
}

export async function requestDocumentTranslationRevision(
  payload: TranslateRevisionPayload,
  onEvent?: (event: GenosStreamEvent) => void,
): Promise<TranslateWorkflowResponse> {
  return requestGenosStream<TranslateWorkflowResponse>(
    "/api/document-translation/translate/revise",
    payload,
    onEvent,
  );
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
