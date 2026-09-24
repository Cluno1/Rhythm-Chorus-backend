from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from urllib.parse import quote


def presign_cos_get(
    bucket: str,
    region: str,
    key: str,
    secret_id: str,
    secret_key: str,
    expires_seconds: int = 900,
    *,
    query_parameters: Iterable[tuple[str, str | None]] = (),
    host: str | None = None,
) -> tuple[str, datetime]:
    """Build a Tencent COS v5 pre-signed GET URL using only the standard library.

    Returns the signed URL and its absolute expiry (UTC). The signature follows
    the ``q-sign-algorithm=sha1`` scheme and mirrors the official qcloud-cos SDK:

    - the canonical HttpString path is the **raw (un-encoded)** object key path,
      while the request URL path is percent-encoded (safe ``/-_.~``);
    - the ``host`` header is signed (``q-header-list=host``), so the HTTP client
      must send a matching Host header — which it does automatically because the
      URL host is the COS endpoint;
    - optional COS/CI query parameters are included in ``q-url-param-list``.
    """
    return presign_cos_request(
        method="GET",
        bucket=bucket,
        region=region,
        key=key,
        secret_id=secret_id,
        secret_key=secret_key,
        expires_seconds=expires_seconds,
        query_parameters=query_parameters,
        host=host,
    )


def presign_cos_put(
    bucket: str,
    region: str,
    key: str,
    secret_id: str,
    secret_key: str,
    expires_seconds: int = 900,
    *,
    headers: Mapping[str, str] | None = None,
) -> tuple[str, datetime]:
    """Build a Tencent COS v5 pre-signed PUT URL for one exact object key."""
    return presign_cos_request(
        method="PUT",
        bucket=bucket,
        region=region,
        key=key,
        secret_id=secret_id,
        secret_key=secret_key,
        expires_seconds=expires_seconds,
        headers=headers,
    )


def presign_cos_post(
    bucket: str,
    region: str,
    key: str,
    secret_id: str,
    secret_key: str,
    expires_seconds: int = 900,
    *,
    query_parameters: Iterable[tuple[str, str | None]] = (),
    headers: Mapping[str, str] | None = None,
    host: str | None = None,
) -> tuple[str, datetime]:
    """Build a COS/CI v5 pre-signed POST URL for a control-plane request."""
    return presign_cos_request(
        method="POST",
        bucket=bucket,
        region=region,
        key=key,
        secret_id=secret_id,
        secret_key=secret_key,
        expires_seconds=expires_seconds,
        query_parameters=query_parameters,
        headers=headers,
        host=host,
    )


def presign_cos_head(
    bucket: str,
    region: str,
    key: str,
    secret_id: str,
    secret_key: str,
    expires_seconds: int = 900,
) -> tuple[str, datetime]:
    """Build a Tencent COS v5 pre-signed HEAD URL for one exact object key."""
    return presign_cos_request(
        method="HEAD",
        bucket=bucket,
        region=region,
        key=key,
        secret_id=secret_id,
        secret_key=secret_key,
        expires_seconds=expires_seconds,
    )


def presign_cos_delete(
    bucket: str,
    region: str,
    key: str,
    secret_id: str,
    secret_key: str,
    expires_seconds: int = 900,
) -> tuple[str, datetime]:
    """Build a Tencent COS v5 pre-signed DELETE URL for one exact object key."""
    return presign_cos_request(
        method="DELETE",
        bucket=bucket,
        region=region,
        key=key,
        secret_id=secret_id,
        secret_key=secret_key,
        expires_seconds=expires_seconds,
    )


def presign_cos_request(
    *,
    method: str,
    bucket: str,
    region: str,
    key: str,
    secret_id: str,
    secret_key: str,
    expires_seconds: int = 900,
    query_parameters: Iterable[tuple[str, str | None]] = (),
    headers: Mapping[str, str] | None = None,
    host: str | None = None,
) -> tuple[str, datetime]:
    normalized_method = method.strip().lower()
    if normalized_method not in {"delete", "get", "head", "post", "put"}:
        raise ValueError("only DELETE, GET, HEAD, POST, and PUT COS requests can be signed")
    if not secret_id or not secret_key:
        raise ValueError("COS credentials are not configured")

    object_key = key.lstrip("/")
    raw_path = "/" + object_key  # 签名用：原始未编码路径（与官方 SDK 一致）
    encoded_path = "/" + quote(object_key, safe="/-_.~")  # URL 用：百分号编码路径
    request_host = host or f"{bucket}.cos.{region}.myqcloud.com"
    if not request_host or any(character in request_host for character in "/?#@"):
        raise ValueError("COS request host is invalid")

    start = int(time.time())
    end = start + int(expires_seconds)
    key_time = f"{start};{end}"

    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    canonical_headers = {"host": request_host}
    for name, value in (headers or {}).items():
        normalized_name = name.strip().lower()
        if not normalized_name or normalized_name == "authorization":
            raise ValueError("invalid COS signed header")
        canonical_headers[normalized_name] = " ".join(value.strip().split())
    header_names = sorted(canonical_headers)
    headers_str = "&".join(
        f"{quote(name, safe='-_.~')}={quote(canonical_headers[name], safe='-_.~')}"
        for name in header_names
    )

    request_parameters = list(query_parameters)
    canonical_parameters = sorted(
        (
            quote(name.strip().lower(), safe="-_.~").lower(),
            "" if value is None else str(value),
            name.strip(),
        )
        for name, value in request_parameters
    )
    if any(not encoded_name for encoded_name, _, _ in canonical_parameters):
        raise ValueError("COS query parameter names must not be blank")
    parameter_string = "&".join(
        f"{encoded_name}={quote(value, safe='-_.~')}"
        for encoded_name, value, _ in canonical_parameters
    )
    parameter_names = ";".join(encoded_name for encoded_name, _, _ in canonical_parameters)
    http_string = (
        f"{normalized_method}\n{raw_path}\n{parameter_string}\n{headers_str}\n"
    )
    http_digest = hashlib.sha1(http_string.encode()).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{http_digest}\n"
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()

    authorization_query = (
        "q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        f"&q-header-list={';'.join(header_names)}"
        f"&q-url-param-list={quote(parameter_names, safe=';-_.~')}"
        f"&q-signature={signature}"
    )
    request_query = "&".join(
        f"{quote(original_name, safe='-_.~')}"
        if value is None
        else f"{quote(original_name, safe='-_.~')}={quote(str(value), safe='-_.~')}"
        for original_name, value in request_parameters
    )
    # Match Tencent's official SDK: authorization fields come first and signed
    # operation parameters follow them. Some CI image operations are routed by
    # this layout even though ordinary query parameters are order-independent.
    query = "&".join(part for part in (authorization_query, request_query) if part)
    url = f"https://{request_host}{encoded_path}?{query}"
    return url, datetime.fromtimestamp(end, tz=UTC)
