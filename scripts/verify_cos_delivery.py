"""Verify the backend COS signer with byte ranges without printing signed URLs."""

from __future__ import annotations

import argparse
import os
from urllib.request import Request, urlopen

from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    url, expires_at = presign_cos_get(
        bucket=args.bucket,
        region=os.environ["COS_REGION"],
        key=args.key,
        secret_id=os.environ["COS_SECRET_ID"],
        secret_key=os.environ["COS_SECRET_KEY"],
        expires_seconds=900,
    )
    request = Request(url, headers={"Range": "bytes=0-31"})
    with urlopen(request, timeout=20) as response:
        body = response.read()
        print(
            f"status={response.status} bytes={len(body)} "
            f"content_range={bool(response.headers.get('Content-Range'))} "
            f"expiry_utc={expires_at.tzinfo is not None}"
        )
        if response.status != 206 or len(body) != 32 or not response.headers.get("Content-Range"):
            raise SystemExit("COS byte-range verification failed")


if __name__ == "__main__":
    main()
