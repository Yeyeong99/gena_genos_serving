"""번역 preview/다운로드 산출물을 Azure Blob Storage 로 업로드하고 SAS URL 을 발급한다.

정적 서빙 경로(`/preview-files/...`)는 더 이상 사용하지 않는다. PNG, HTML preview,
office 다운로드 파일 모두 Azure Blob 에 업로드해 SAS URL 로 FE 가 직접 접근한다 —
운영/로컬 동일한 URL 형식.

환경변수
- `AZURE_STORAGE_CONNECTION_STRING` (필수) — `;` 로 구분된 표준 Azure connection string.
- `AZURE_STORAGE_CONTAINER_NAME` (선택, default `chat-uploads-dev`) — 업로드 컨테이너.
- `AZURE_STORAGE_PREVIEW_PREFIX` (선택, default `translate-previews`) — blob 경로 prefix.
- `AZURE_STORAGE_PREVIEW_SAS_HOURS` (선택, default `24`) — 발급 SAS 의 read 만료 시간(시).

`is_azure_preview_enabled()` 가 False 면 업로드를 스킵하고 None/빈 리스트를 반환한다 —
정적 서빙이 사라졌으므로 Azure 가 비활성이면 preview/다운로드 모두 사용 불가.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


_logger = logging.getLogger("uvicorn.error")

_DEFAULT_CONTAINER = "chat-uploads-dev"
_DEFAULT_PREFIX = "translate-previews"
_DEFAULT_SAS_HOURS = 24


def _connection_string() -> str:
    return os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()


def is_azure_preview_enabled() -> bool:
    """Azure preview 업로드 활성 여부 — connection string 보유로만 판단."""

    return bool(_connection_string())


def _container_name() -> str:
    return os.environ.get("AZURE_STORAGE_CONTAINER_NAME", "").strip() or _DEFAULT_CONTAINER


def _preview_prefix() -> str:
    raw = os.environ.get("AZURE_STORAGE_PREVIEW_PREFIX", "").strip() or _DEFAULT_PREFIX
    return raw.strip("/")


def _sas_hours() -> int:
    try:
        return max(1, int(os.environ.get("AZURE_STORAGE_PREVIEW_SAS_HOURS", str(_DEFAULT_SAS_HOURS))))
    except (TypeError, ValueError):
        return _DEFAULT_SAS_HOURS


def _parse_connection_string(conn_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in conn_str.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key.strip()] = value.strip()
    return out


def _build_blob_url(account_name: str, endpoint_suffix: str, container: str, blob_name: str) -> str:
    return f"https://{account_name}.blob.{endpoint_suffix}/{container}/{blob_name}"


def _office_content_type(ext: str) -> str:
    e = ext.lower().lstrip(".")
    if e == "pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if e == "docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if e == "xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "application/octet-stream"


def upload_office_to_azure(
    local_path: Path,
    *,
    job_token: str,
    subdir: str = "download",
    download_filename: str | None = None,
) -> str | None:
    """번역 결과 docx/pptx/xlsx 파일을 Azure 에 업로드하고 SAS read URL 을 반환한다.

    blob 경로 형식: ``{prefix}/{job_token}/{subdir}/{filename}``. ``Content-Disposition``
    을 attachment 로 명시해 브라우저가 새 탭 열기 없이 곧바로 다운로드 다이얼로그를
    띄우도록 한다 — FE 의 hidden iframe / anchor.click 트리거가 일관되게 동작.

    Returns:
        성공 시 SAS read URL, 실패 또는 Azure 비활성·파일 누락 시 ``None``.
    """

    if not is_azure_preview_enabled():
        return None
    if not local_path.exists() or not local_path.is_file():
        return None

    try:
        from azure.storage.blob import (
            BlobSasPermissions,
            BlobServiceClient,
            ContentSettings,
            generate_blob_sas,
        )
    except ImportError as exc:
        _logger.warning(
            "[azure_uploader] azure-storage-blob 미설치 — office 업로드 비활성: %s", exc
        )
        return None

    conn_str = _connection_string()
    parsed = _parse_connection_string(conn_str)
    account_name = parsed.get("AccountName", "").strip()
    account_key = parsed.get("AccountKey", "").strip()
    endpoint_suffix = parsed.get("EndpointSuffix", "core.windows.net").strip()
    if not account_name or not account_key:
        _logger.warning(
            "[azure_uploader] AZURE_STORAGE_CONNECTION_STRING 에 AccountName/AccountKey 가 없습니다."
        )
        return None

    container = _container_name()
    prefix = _preview_prefix()
    expiry = datetime.now(timezone.utc) + timedelta(hours=_sas_hours())

    try:
        client = BlobServiceClient.from_connection_string(conn_str)
    except Exception as exc:
        _logger.warning("[azure_uploader] BlobServiceClient 생성 실패: %s", exc)
        return None

    blob_name = f"{prefix}/{job_token}/{subdir}/{local_path.name}".strip("/")
    suggested = (download_filename or local_path.name).strip()
    content_type = _office_content_type(local_path.suffix)
    try:
        try:
            container_client = client.get_container_client(container)
            with open(local_path, "rb") as fh:
                container_client.upload_blob(
                    name=blob_name,
                    data=fh,
                    overwrite=True,
                    content_settings=ContentSettings(
                        content_type=content_type,
                        content_disposition=f'attachment; filename="{suggested}"',
                    ),
                )
        except Exception as exc:
            _logger.warning("[azure_uploader] office upload 실패 (%s): %s", blob_name, exc)
            return None
        try:
            sas = generate_blob_sas(
                account_name=account_name,
                container_name=container,
                blob_name=blob_name,
                account_key=account_key,
                permission=BlobSasPermissions(read=True),
                expiry=expiry,
            )
        except Exception as exc:
            _logger.warning("[azure_uploader] office SAS 발급 실패 (%s): %s", blob_name, exc)
            return None
        return f"{_build_blob_url(account_name, endpoint_suffix, container, blob_name)}?{sas}"
    finally:
        try:
            client.close()
        except Exception:
            pass


_HTML_ASSET_CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


def _asset_content_type(suffix: str) -> str:
    return _HTML_ASSET_CONTENT_TYPES.get(suffix.lower(), "application/octet-stream")


def upload_html_assets_to_azure(
    local_paths: list[Path],
    *,
    job_token: str,
    subdir: str,
) -> dict[str, str]:
    """LibreOffice 가 HTML 옆에 떨어뜨린 보조 자산 (이미지/폰트/CSS) 을 Azure 에 업로드한다.

    blob 경로 형식: ``{prefix}/{job_token}/{subdir}/{filename}``. 파일명은 LibreOffice 가
    생성한 그대로 사용해 HTML 안의 상대 src/href 를 절대 SAS URL 로 직접 매핑할 수 있게 한다.

    Args:
        local_paths: 업로드할 자산 경로 목록.
        job_token: 번역 job 식별자 — blob path 첫 segment.
        subdir: HTML 과 동일한 subdir.

    Returns:
        ``{filename: SAS_URL}`` 매핑. Azure 비활성/누락/실패 항목은 매핑에서 제외된다 —
        호출측은 누락된 자산을 그대로 두고 placeholder 로 빠지도록 처리한다.
    """

    if not local_paths or not is_azure_preview_enabled():
        return {}

    try:
        from azure.storage.blob import (
            BlobSasPermissions,
            BlobServiceClient,
            ContentSettings,
            generate_blob_sas,
        )
    except ImportError as exc:
        _logger.warning(
            "[azure_uploader] azure-storage-blob 미설치 — html assets 업로드 비활성: %s", exc
        )
        return {}

    conn_str = _connection_string()
    parsed = _parse_connection_string(conn_str)
    account_name = parsed.get("AccountName", "").strip()
    account_key = parsed.get("AccountKey", "").strip()
    endpoint_suffix = parsed.get("EndpointSuffix", "core.windows.net").strip()
    if not account_name or not account_key:
        _logger.warning(
            "[azure_uploader] AZURE_STORAGE_CONNECTION_STRING 에 AccountName/AccountKey 가 없습니다."
        )
        return {}

    container = _container_name()
    prefix = _preview_prefix()
    expiry = datetime.now(timezone.utc) + timedelta(hours=_sas_hours())

    try:
        client = BlobServiceClient.from_connection_string(conn_str)
    except Exception as exc:
        _logger.warning("[azure_uploader] BlobServiceClient 생성 실패: %s", exc)
        return {}

    urls: dict[str, str] = {}
    try:
        container_client = client.get_container_client(container)
        for path in local_paths:
            if not path.exists() or not path.is_file():
                continue
            blob_name = f"{prefix}/{job_token}/{subdir}/{path.name}".strip("/")
            content_type = _asset_content_type(path.suffix)
            try:
                with open(path, "rb") as fh:
                    container_client.upload_blob(
                        name=blob_name,
                        data=fh,
                        overwrite=True,
                        content_settings=ContentSettings(content_type=content_type),
                    )
            except Exception as exc:
                _logger.warning("[azure_uploader] asset upload 실패 (%s): %s", blob_name, exc)
                continue
            try:
                sas = generate_blob_sas(
                    account_name=account_name,
                    container_name=container,
                    blob_name=blob_name,
                    account_key=account_key,
                    permission=BlobSasPermissions(read=True),
                    expiry=expiry,
                )
            except Exception as exc:
                _logger.warning("[azure_uploader] asset SAS 발급 실패 (%s): %s", blob_name, exc)
                continue
            urls[path.name] = (
                f"{_build_blob_url(account_name, endpoint_suffix, container, blob_name)}?{sas}"
            )
    finally:
        try:
            client.close()
        except Exception:
            pass

    return urls


def upload_html_to_azure(
    local_path: Path,
    *,
    job_token: str,
    subdir: str,
    blob_name: str = "index.html",
) -> str | None:
    """Preview HTML 을 Azure Blob 에 업로드하고 SAS read URL 을 반환한다.

    blob 경로 형식: ``{prefix}/{job_token}/{subdir}/{blob_name}``. Content-Type 을
    ``text/html; charset=utf-8`` 로 명시해 iframe 이 그대로 렌더 가능. ``index.html``
    내부의 ``<img src>`` 는 미리 업로드된 PNG 의 절대 SAS URL 을 가리키므로 base
    경로 충돌은 발생하지 않는다.

    Returns:
        성공 시 SAS read URL, Azure 비활성·파일 누락·업로드 실패 시 ``None``.
    """

    if not is_azure_preview_enabled():
        _logger.warning(
            "[azure_uploader] html upload skip — Azure 비활성 (AZURE_STORAGE_CONNECTION_STRING 미설정/빈 값). path=%s",
            local_path,
        )
        return None
    if not local_path.exists() or not local_path.is_file():
        _logger.warning(
            "[azure_uploader] html upload skip — 로컬 HTML 파일 누락: %s (exists=%s is_file=%s)",
            local_path,
            local_path.exists(),
            local_path.is_file() if local_path.exists() else False,
        )
        return None

    try:
        from azure.storage.blob import (
            BlobSasPermissions,
            BlobServiceClient,
            ContentSettings,
            generate_blob_sas,
        )
    except ImportError as exc:
        _logger.warning(
            "[azure_uploader] azure-storage-blob 미설치 — html 업로드 비활성: %s", exc
        )
        return None

    conn_str = _connection_string()
    parsed = _parse_connection_string(conn_str)
    account_name = parsed.get("AccountName", "").strip()
    account_key = parsed.get("AccountKey", "").strip()
    endpoint_suffix = parsed.get("EndpointSuffix", "core.windows.net").strip()
    if not account_name or not account_key:
        _logger.warning(
            "[azure_uploader] AZURE_STORAGE_CONNECTION_STRING 에 AccountName/AccountKey 가 없습니다."
        )
        return None

    container = _container_name()
    prefix = _preview_prefix()
    expiry = datetime.now(timezone.utc) + timedelta(hours=_sas_hours())

    try:
        client = BlobServiceClient.from_connection_string(conn_str)
    except Exception as exc:
        _logger.warning("[azure_uploader] BlobServiceClient 생성 실패: %s", exc)
        return None

    full_blob_name = f"{prefix}/{job_token}/{subdir}/{blob_name}".strip("/")
    try:
        try:
            container_client = client.get_container_client(container)
            with open(local_path, "rb") as fh:
                container_client.upload_blob(
                    name=full_blob_name,
                    data=fh,
                    overwrite=True,
                    content_settings=ContentSettings(
                        content_type="text/html; charset=utf-8",
                        cache_control="no-cache, max-age=0",
                    ),
                )
        except Exception as exc:
            _logger.warning(
                "[azure_uploader] html upload 실패 (%s): %s",
                full_blob_name,
                exc,
                exc_info=True,
            )
            return None
        try:
            sas = generate_blob_sas(
                account_name=account_name,
                container_name=container,
                blob_name=full_blob_name,
                account_key=account_key,
                permission=BlobSasPermissions(read=True),
                expiry=expiry,
            )
        except Exception as exc:
            _logger.warning(
                "[azure_uploader] html SAS 발급 실패 (%s): %s",
                full_blob_name,
                exc,
                exc_info=True,
            )
            return None
        url = f"{_build_blob_url(account_name, endpoint_suffix, container, full_blob_name)}?{sas}"
        _logger.info(
            "[azure_uploader] html upload 성공 — blob=%s account=%s container=%s",
            full_blob_name,
            account_name,
            container,
        )
        return url
    finally:
        try:
            client.close()
        except Exception:
            pass


def upload_pngs_to_azure(
    local_paths: list[Path],
    *,
    job_token: str,
    subdir: str,
) -> list[str | None]:
    """슬라이드 PNG 파일들을 Azure 에 업로드하고 SAS read URL 리스트를 반환한다.

    blob 경로 형식: ``{prefix}/{job_token}/{subdir}/{filename}``

    Args:
        local_paths: 업로드할 로컬 PNG 경로. 파일이 존재하지 않으면 해당 항목은 건너뛴다.
        job_token: 번역 job 식별자 — blob path 의 첫 번째 segment.
        subdir: live/preview-job 등 subdir 식별자.

    Returns:
        ``local_paths`` 와 동일한 길이/순서의 리스트. 각 항목은 성공 시 SAS URL,
        실패(누락·업로드 실패·SAS 발급 실패) 시 ``None``. Azure 비활성/입력 비어 있음
        시 ``[]``. 호출측은 인덱스로 슬라이드별 URL 을 안전하게 매핑할 수 있다.
    """

    if not local_paths or not is_azure_preview_enabled():
        return []

    # azure-storage-blob 은 lazy import — 환경에 미설치 시 친절한 에러로 폴백.
    try:
        from azure.storage.blob import (
            BlobSasPermissions,
            BlobServiceClient,
            ContentSettings,
            generate_blob_sas,
        )
    except ImportError as exc:
        _logger.warning(
            "[azure_uploader] azure-storage-blob 미설치 — preview 업로드 비활성: %s", exc
        )
        return []

    conn_str = _connection_string()
    parsed = _parse_connection_string(conn_str)
    account_name = parsed.get("AccountName", "").strip()
    account_key = parsed.get("AccountKey", "").strip()
    endpoint_suffix = parsed.get("EndpointSuffix", "core.windows.net").strip()
    if not account_name or not account_key:
        _logger.warning(
            "[azure_uploader] AZURE_STORAGE_CONNECTION_STRING 에 AccountName/AccountKey 가 없습니다."
        )
        return []

    container = _container_name()
    prefix = _preview_prefix()
    expiry = datetime.now(timezone.utc) + timedelta(hours=_sas_hours())

    try:
        client = BlobServiceClient.from_connection_string(conn_str)
    except Exception as exc:
        _logger.warning("[azure_uploader] BlobServiceClient 생성 실패: %s", exc)
        return []

    urls: list[str | None] = []
    try:
        container_client = client.get_container_client(container)
        for path in local_paths:
            if not path.exists() or not path.is_file():
                _logger.warning("[azure_uploader] 누락된 PNG: %s", path)
                urls.append(None)
                continue
            blob_name = f"{prefix}/{job_token}/{subdir}/{path.name}".strip("/")
            try:
                with open(path, "rb") as fh:
                    container_client.upload_blob(
                        name=blob_name,
                        data=fh,
                        overwrite=True,
                        content_settings=ContentSettings(content_type="image/png"),
                    )
            except Exception as exc:
                _logger.warning("[azure_uploader] upload 실패 (%s): %s", blob_name, exc)
                urls.append(None)
                continue
            try:
                sas = generate_blob_sas(
                    account_name=account_name,
                    container_name=container,
                    blob_name=blob_name,
                    account_key=account_key,
                    permission=BlobSasPermissions(read=True),
                    expiry=expiry,
                )
            except Exception as exc:
                _logger.warning("[azure_uploader] SAS 발급 실패 (%s): %s", blob_name, exc)
                urls.append(None)
                continue
            urls.append(
                f"{_build_blob_url(account_name, endpoint_suffix, container, blob_name)}?{sas}"
            )
    finally:
        try:
            client.close()
        except Exception:
            pass

    return urls
