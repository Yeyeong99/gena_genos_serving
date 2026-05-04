import axios from "axios";
import { NextResponse } from "next/server";

const LOCAL_BACKEND_URL =
  process.env.DOC_TRANSLATION_BACKEND_URL ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);
const WORKFLOW_URL =
  process.env.DOC_TRANSLATION_REALTIME_WORKFLOW_URL ??
  "https://genos.genon.ai/api/gateway/workflow/4311/run/v2";

function unwrapWorkflowResponse(payload: unknown) {
  if (payload && typeof payload === "object" && "data" in payload) {
    const nested = (payload as { data?: unknown }).data;
    if (nested && typeof nested === "object") {
      return nested;
    }
  }
  return payload;
}

export async function POST(request: Request) {
  const token = process.env.DOC_TRANSLATION_REALTIME_WORKFLOW_TOKEN;

  try {
    const payload = await request.json();
    const isLocalBackend = Boolean(LOCAL_BACKEND_URL);
    const responseUrl = isLocalBackend
      ? `${LOCAL_BACKEND_URL?.replace(/\/$/, "")}/api/document-translation/realtime`
      : WORKFLOW_URL;
    const method = isLocalBackend ? "POST" : "GET";
    const headers: Record<string, string> = {};

    if (!isLocalBackend) {
      if (!token) {
        return NextResponse.json(
          { message: "DOC_TRANSLATION_REALTIME_WORKFLOW_TOKEN 환경변수가 설정되지 않았습니다." },
          { status: 500 },
        );
      }
      headers.Authorization = `Bearer ${token}`;
    }

    const response = await axios.request({
      url: responseUrl,
      method,
      headers,
      data: payload,
      validateStatus: () => true,
    });

    if (response.status >= 400) {
      return NextResponse.json(
        { message: response.data?.message ?? "실시간 번역 요청에 실패했습니다." },
        { status: response.status },
      );
    }

    return NextResponse.json(unwrapWorkflowResponse(response.data));
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "실시간 번역 요청 중 알 수 없는 오류가 발생했습니다.";

    return NextResponse.json({ message }, { status: 500 });
  }
}
