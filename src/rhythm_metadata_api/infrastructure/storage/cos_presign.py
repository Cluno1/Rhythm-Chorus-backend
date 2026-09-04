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
    the ``q-sign-algorithm=sha1`` scheme documented for COS: an HMAC-SHA1 sign key
    derived from the key-time window, then an HMAC-SHA1 over the canonicalised
    request. No request headers or URL params are signed (empty header/param lists),
    so the client may issue a plain ranged GET against the returned URL.
    """
    if not secret_id or not secret_key:
        raise ValueError("COS credentials are not configured")

    object_key = key.lstrip("/")
    encoded_path = "/" + quote(object_key, safe="/")
    host = f"{bucket}.cos.{region}.myqcloud.com"

    start = int(time.time())
    end = start + int(expires_seconds)
    key_time = f"{start};{end}"

    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    http_string = f"get\n{encoded_path}\n\n\n"
    string_to_sign = "sha1\n{}\n{}\n".format(
        key_time, hashlib.sha1(http_string.encode()).hexdigest()
    )
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()

    query = (
        "q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        "&q-header-list="
        "&q-url-param-list="
        f"&q-signature={signature}"
    )
    url = f"https://{host}{encoded_path}?{query}"
    return url, datetime.fromtimestamp(end, tz=UTC)
