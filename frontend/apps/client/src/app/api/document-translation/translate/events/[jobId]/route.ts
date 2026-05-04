const LOCAL_BACKEND_URL =
  process.env.DOC_TRANSLATION_BACKEND_URL ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);

export async function GET(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
) {
  if (!LOCAL_BACKEND_URL) {
    return new Response("DOC_TRANSLATION_BACKEND_URL 환경변수가 설정되지 않았습니다.", {
      status: 500,
    });
  }

  const { jobId } = await context.params;
  const upstreamUrl = new URL(
    `${LOCAL_BACKEND_URL.replace(/\/$/, "")}/api/document-translation/translate/events/${encodeURIComponent(jobId)}`,
  );
  const lastEventId = request.headers.get("last-event-id");

  const upstream = await fetch(upstreamUrl, {
    headers: lastEventId ? { "last-event-id": lastEventId } : undefined,
    cache: "no-store",
  });

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text();
    return new Response(text || "번역 이벤트 스트림 연결에 실패했습니다.", {
      status: upstream.status || 500,
    });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}
