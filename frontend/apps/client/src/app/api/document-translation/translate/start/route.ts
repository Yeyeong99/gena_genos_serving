import axios from "axios";
import { NextResponse } from "next/server";

const LOCAL_BACKEND_URL =
  process.env.DOC_TRANSLATION_BACKEND_URL ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);

export async function POST(request: Request) {
  if (!LOCAL_BACKEND_URL) {
    return NextResponse.json(
      { message: "DOC_TRANSLATION_BACKEND_URL 환경변수가 설정되지 않았습니다." },
      { status: 500 },
    );
  }

  try {
    const payload = await request.json();
    const response = await axios.post(
      `${LOCAL_BACKEND_URL.replace(/\/$/, "")}/api/document-translation/translate/start`,
      payload,
      {
        validateStatus: () => true,
      },
    );

    if (response.status >= 400) {
      return NextResponse.json(
        { message: response.data?.message ?? "문서 번역 시작 요청에 실패했습니다." },
        { status: response.status },
      );
    }

    return NextResponse.json(response.data);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "문서 번역 시작 요청 중 알 수 없는 오류가 발생했습니다.";
    return NextResponse.json({ message }, { status: 500 });
  }
}
