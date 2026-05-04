"""Azure Blob Storage preview publisher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import mimetypes
import os
from pathlib import Path
import re
from typing import Callable
from urllib.parse import quote, unquote, urlsplit


_HTML_URL_ATTR_RE = re.compile(
    r"(?P<prefix>\b(?:src|href)\s*=\s*['\"])(?P<url>[^'\"]+)(?P<suffix>['\"])",
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(r"url\((?P<quote>['\"]?)(?P<url>[^)'\"\s]+)(?P=quote)\)", re.IGNORECASE)


@dataclass(frozen=True)
class AzurePreviewConfig:
    connection_string: str
    container_name: str
    blob_prefix: str
    use_sas: bool
    sas_ttl_seconds: int


def is_azure_preview_enabled() -> bool:
    mode = os.getenv("AI_TRANSLATION_PREVIEW_STORAGE", "").strip().lower()
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if mode in {"0", "false", "off", "local"}:
        return False
    return mode == "azure" or bool(connection_string)


def _get_config() -> AzurePreviewConfig | None:
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not connection_string:
        return None
    return AzurePreviewConfig(
        connection_string=connection_string,
        container_name=os.getenv("AI_TRANSLATION_AZURE_PREVIEW_CONTAINER", "translation-previews").strip()
        or "translation-previews",
        blob_prefix=os.getenv("AI_TRANSLATION_AZURE_PREVIEW_PREFIX", "preview-files").strip().strip("/"),
        use_sas=os.getenv("AI_TRANSLATION_AZURE_PREVIEW_USE_SAS", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        sas_ttl_seconds=max(60, int(os.getenv("AI_TRANSLATION_AZURE_PREVIEW_SAS_TTL_SECONDS", "86400"))),
    )


def _parse_connection_string(connection_string: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in connection_string.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        parsed[key] = value
    return parsed


def _quote_blob_name(blob_name: str) -> str:
    return "/".join(quote(part) for part in blob_name.split("/"))


def _is_external_url(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered or lowered.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
        return True
    parsed = urlsplit(value)
    return bool(parsed.scheme or parsed.netloc)


def _content_type(path: Path) -> str:
    if path.suffix.lower() == ".html":
        return "text/html; charset=utf-8"
    if path.suffix.lower() == ".css":
        return "text/css; charset=utf-8"
    if path.suffix.lower() == ".js":
        return "application/javascript; charset=utf-8"
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _build_blob_url_factory(config: AzurePreviewConfig) -> Callable[[str], str]:
    try:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("azure-storage-blob 패키지가 설치되어 있지 않습니다.") from exc

    values = _parse_connection_string(config.connection_string)
    account_name = values.get("AccountName", "")
    account_key = values.get("AccountKey", "")
    blob_endpoint = values.get("BlobEndpoint") or (
        f"https://{account_name}.blob.{values.get('EndpointSuffix', 'core.windows.net')}"
        if account_name
        else ""
    )
    if not account_name or not blob_endpoint:
        raise RuntimeError("Azure Storage connection string에서 AccountName/BlobEndpoint를 읽지 못했습니다.")

    def build_url(blob_name: str) -> str:
        base_url = f"{blob_endpoint.rstrip('/')}/{quote(config.container_name)}/{_quote_blob_name(blob_name)}"
        if not config.use_sas:
            return base_url
        if not account_key:
            raise RuntimeError("SAS URL 생성을 위한 AccountKey가 connection string에 없습니다.")
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=config.container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(seconds=config.sas_ttl_seconds),
        )
        return f"{base_url}?{sas}"

    return build_url


def _rewrite_html_asset_urls(html: str, *, root_dir: Path, blob_prefix: str, build_url: Callable[[str], str]) -> str:
    root_real = Path(os.path.realpath(root_dir)).resolve()

    def resolve_asset_url(value: str) -> str:
        if _is_external_url(value):
            return value
        parsed = urlsplit(value)
        rel_path = unquote(parsed.path).lstrip("/")
        local_path = Path(os.path.realpath(root_dir / rel_path)).resolve()
        try:
            relative_path = local_path.relative_to(root_real)
        except ValueError:
            return value
        if not local_path.exists() or not local_path.is_file():
            return value
        blob_name = f"{blob_prefix}/{relative_path.as_posix()}"
        return build_url(blob_name)

    html = _HTML_URL_ATTR_RE.sub(
        lambda match: f"{match.group('prefix')}{resolve_asset_url(match.group('url'))}{match.group('suffix')}",
        html,
    )
    return _CSS_URL_RE.sub(
        lambda match: f"url({resolve_asset_url(match.group('url'))})",
        html,
    )


def publish_preview_directory(
    directory: str | Path,
    *,
    blob_prefix: str,
    index_filename: str = "index.html",
) -> str | None:
    """Upload a preview directory to Azure Blob Storage and return the index URL.

    The generated HTML is rewritten so local relative assets point to Azure Blob URLs.
    This keeps previews working even when the container is private and URLs require SAS.
    """

    if not is_azure_preview_enabled():
        return None

    config = _get_config()
    if config is None:
        return None

    try:
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        print(f"[Azure Preview] azure-storage-blob import 실패: {exc}")
        return None

    root_dir = Path(directory)
    index_path = root_dir / index_filename
    if not root_dir.exists() or not index_path.exists():
        return None

    normalized_prefix = "/".join(
        part.strip("/") for part in (config.blob_prefix, blob_prefix.strip("/")) if part.strip("/")
    )

    try:
        build_url = _build_blob_url_factory(config)
        service = BlobServiceClient.from_connection_string(config.connection_string)
        container = service.get_container_client(config.container_name)
        try:
            container.create_container()
        except Exception:
            pass

        for path in root_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root_dir).as_posix()
            blob_name = f"{normalized_prefix}/{relative}"
            content: bytes
            if path.resolve() == index_path.resolve():
                html = path.read_text(encoding="utf-8", errors="ignore")
                html = _rewrite_html_asset_urls(
                    html,
                    root_dir=root_dir,
                    blob_prefix=normalized_prefix,
                    build_url=build_url,
                )
                content = html.encode("utf-8")
            else:
                content = path.read_bytes()
            container.upload_blob(
                name=blob_name,
                data=content,
                overwrite=True,
                content_settings=ContentSettings(content_type=_content_type(path)),
            )
        return build_url(f"{normalized_prefix}/{index_filename}")
    except Exception as exc:
        print(f"[Azure Preview] 업로드 실패 - 로컬 preview URL fallback 사용: {exc}")
        return None
