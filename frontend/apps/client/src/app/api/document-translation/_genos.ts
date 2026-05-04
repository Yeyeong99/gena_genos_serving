import { NextResponse } from "next/server";

const DEFAULT_CODE_SERVING_BASE_URL = "https://genos.genon.ai/api/gateway/code_serving";

function getCodeServingUrl() {
  if (process.env.DOC_TRANSLATION_CODE_SERVING_URL) {
    return process.env.DOC_TRANSLATION_CODE_SERVING_URL;
  }

  const servingId = process.env.DOC_TRANSLATION_CODE_SERVING_ID ?? process.env.GENOS_CODE_SERVING_ID;
  if (!servingId) {
    return "";
  }

  return `${DEFAULT_CODE_SERVING_BASE_URL}/${servingId}/json`;
}

function getCodeServingToken() {
  return process.env.DOC_TRANSLATION_CODE_SERVING_TOKEN ?? process.env.GENOS_TOKEN ?? "";
}

function unwrapCodeServingResponse(payload: unknown) {
  if (payload && typeof payload === "object" && "data" in payload) {
    const nested = (payload as { data?: unknown }).data;
    if (nested && typeof nested === "object") {
      return nested;
    }
  }
  return payload;
}

function buildMissingConfigResponse() {
  const missing = [];
  if (!getCodeServingUrl()) {
    missing.push("DOC_TRANSLATION_CODE_SERVING_URL 또는 DOC_TRANSLATION_CODE_SERVING_ID");
  }
  if (!getCodeServingToken()) {
    missing.push("DOC_TRANSLATION_CODE_SERVING_TOKEN 또는 GENOS_TOKEN");
  }

  return NextResponse.json(
    { message: `${missing.join(", ")} 환경변수가 설정되지 않았습니다.` },
    { status: 500 },
  );
}

export async function proxyCodeServingRequest(
  payload: unknown,
  options: { json?: boolean } = {},
) {
  const url = getCodeServingUrl();
  const token = getCodeServingToken();

  if (!url || !token) {
    return buildMissingConfigResponse();
  }

  const requestPayload =
    options.json && payload && typeof payload === "object"
      ? { ...(payload as Record<string, unknown>), stream: false }
      : payload;

  const upstream = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(requestPayload),
    cache: "no-store",
  });

  if (!upstream.ok) {
    const text = await upstream.text();
    return NextResponse.json(
      { message: text || "GenOS Code Serving 요청에 실패했습니다." },
      { status: upstream.status },
    );
  }

  if (options.json) {
    const text = await upstream.text();
    let data: unknown = text;
    try {
      data = JSON.parse(text);
    } catch {
      // Keep plain text responses as-is.
    }
    return NextResponse.json(unwrapCodeServingResponse(data));
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "Content-Type": upstream.headers.get("content-type") ?? "text/event-stream",
      "X-Accel-Buffering": "no",
    },
  });
}
