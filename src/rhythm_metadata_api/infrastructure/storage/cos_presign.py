from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from urllib.parse import quote


def presign_cos_get(
    bucket: str,
    region: str,
    key: str,
    secret_id: str,
    secret_key: str,
    expires_seconds: int = 900,
) -> tuple[str, datetime]:
    """Build a Tencent COS v5 pre-signed GET URL using only the standard library.

    Returns the signed URL and its absolute expiry (UTC). The signature follows
    the ``q-sign-algorithm=sha1`` scheme and mirrors the official qcloud-cos SDK:

    - the canonical HttpString path is the **raw (un-encoded)** object key path,
      while the request URL path is percent-encoded (safe ``/-_.~``);
    - the ``host`` header is signed (``q-header-list=host``), so the HTTP client
      must send a matching Host header — which it does automatically because the
      URL host is the COS endpoint;
    - no URL parameters are signed (empty ``q-url-param-list``).
    """
    if not secret_id or not secret_key:
        raise ValueError("COS credentials are not configured")

    object_key = key.lstrip("/")
    raw_path = "/" + object_key  # 签名用：原始未编码路径（与官方 SDK 一致）
    encoded_path = "/" + quote(object_key, safe="/-_.~")  # URL 用：百分号编码路径
    host = f"{bucket}.cos.{region}.myqcloud.com"

    start = int(time.time())
    end = start + int(expires_seconds)
    key_time = f"{start};{end}"

    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    headers_str = "host=" + quote(host, safe="-_.~")
    http_string = f"get\n{raw_path}\n\n{headers_str}\n"
    http_digest = hashlib.sha1(http_string.encode()).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{http_digest}\n"
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()

    query = (
        "q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        "&q-header-list=host"
        "&q-url-param-list="
        f"&q-signature={signature}"
    )
    url = f"https://{host}{encoded_path}?{query}"
    return url, datetime.fromtimestamp(end, tz=UTC)
