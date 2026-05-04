import { NextRequest, NextResponse } from "next/server";

const HTML_CONTENT_TYPES = ["text/html", "application/xhtml+xml"];

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const rawUrl = request.nextUrl.searchParams.get("url") ?? "";
  const targetUrl = parseRemotePreviewUrl(rawUrl);

  if (!targetUrl) {
    return NextResponse.json({ message: "유효한 preview URL이 필요합니다." }, { status: 400 });
  }

  if (!isAllowedPreviewUrl(targetUrl)) {
    return NextResponse.json({ message: "허용되지 않은 preview URL입니다." }, { status: 400 });
  }

  try {
    const upstream = await fetch(targetUrl.toString(), {
      cache: "no-store",
      redirect: "follow",
    });

    if (!upstream.ok) {
      return NextResponse.json(
        { message: `preview 파일을 불러오지 못했습니다. HTTP ${upstream.status}` },
        { status: upstream.status },
      );
    }

    const contentType = upstream.headers.get("content-type") ?? "application/octet-stream";
    const headers = new Headers({
      "Cache-Control": "no-store",
      "Content-Type": contentType,
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
    });

    if (isHtmlContentType(contentType)) {
      const html = await upstream.text();
      return new Response(rewriteHtmlAssetUrls(html, targetUrl), {
        status: upstream.status,
        headers,
      });
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers,
    });
  } catch (error) {
    return NextResponse.json(
      { message: error instanceof Error ? error.message : "preview 파일 요청 중 오류가 발생했습니다." },
      { status: 502 },
    );
  }
}

function parseRemotePreviewUrl(value: string) {
  if (!value) {
    return null;
  }
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

function isAllowedPreviewUrl(url: URL) {
  const allowedHosts = (process.env.DOC_TRANSLATION_PREVIEW_REMOTE_ALLOWED_HOSTS ?? "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);

  if (allowedHosts.length > 0) {
    return allowedHosts.some((host) => url.hostname.toLowerCase() === host);
  }

  if (process.env.DOC_TRANSLATION_PREVIEW_REMOTE_ALLOW_PRIVATE === "1") {
    return true;
  }

  return !isPrivateHostname(url.hostname);
}

function isPrivateHostname(hostname: string) {
  const host = hostname.toLowerCase();
  if (
    host === "localhost" ||
    host === "0.0.0.0" ||
    host === "::1" ||
    host.endsWith(".localhost") ||
    host.endsWith(".local")
  ) {
    return true;
  }

  const parts = host.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return false;
  }

  const [first, second] = parts;
  return (
    first === 10 ||
    first === 127 ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168)
  );
}

function isHtmlContentType(contentType: string) {
  const normalized = contentType.split(";")[0].trim().toLowerCase();
  return HTML_CONTENT_TYPES.includes(normalized);
}

function rewriteHtmlAssetUrls(html: string, pageUrl: URL) {
  const withBase = injectBaseTag(html, pageUrl);
  return withBase.replace(
    /\b(src|href)\s*=\s*(["'])([^"']+)\2/gi,
    (match, attr: string, quote: string, value: string) => {
      const resolved = resolveRemoteAssetUrl(value, pageUrl);
      return resolved ? `${attr}=${quote}${resolved}${quote}` : match;
    },
  );
}

function injectBaseTag(html: string, pageUrl: URL) {
  const baseHref = pageUrl.toString();
  const baseTag = `<base href="${escapeHtmlAttribute(baseHref)}">`;

  if (/<base\b/i.test(html)) {
    return html;
  }
  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head([^>]*)>/i, `<head$1>${baseTag}`);
  }
  return `${baseTag}${html}`;
}

function resolveRemoteAssetUrl(value: string, pageUrl: URL) {
  const trimmed = value.trim();
  if (
    !trimmed ||
    trimmed.startsWith("#") ||
    /^(data|mailto|tel|javascript):/i.test(trimmed)
  ) {
    return null;
  }

  try {
    return new URL(trimmed, pageUrl).toString();
  } catch {
    return null;
  }
}

function escapeHtmlAttribute(value: string) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
