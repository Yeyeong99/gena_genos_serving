const LOCAL_BACKEND_URL =
  process.env.DOC_TRANSLATION_BACKEND_URL ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);

export async function GET(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
) {
  if (!LOCAL_BACKEND_URL) {
    return new Response("DOC_TRANSLATION_BACKEND_URL 환경변수가 설정되지 않았습니다.", {
      status: 500,
    });
  }

  const { path } = await context.params;
  const upstreamUrl = new URL(
    `${LOCAL_BACKEND_URL.replace(/\/$/, "")}/preview-files/${path.map(encodeURIComponent).join("/")}`,
  );
  const incomingUrl = new URL(request.url);
  upstreamUrl.search = incomingUrl.search;

  const upstream = await fetch(upstreamUrl, {
    cache: "no-store",
  });

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text();
    return new Response(text || "미리보기 파일을 불러오지 못했습니다.", {
      status: upstream.status || 500,
    });
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "application/octet-stream",
      "Cache-Control": "no-cache, no-store, must-revalidate",
    },
  });
}
