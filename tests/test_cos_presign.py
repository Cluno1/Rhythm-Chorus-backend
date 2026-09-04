from datetime import UTC, datetime
from unittest import mock

import pytest

from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get


def test_presign_cos_get_is_deterministic_golden() -> None:
    with mock.patch(
        "rhythm_metadata_api.infrastructure.storage.cos_presign.time.time",
        return_value=1_700_000_000,
    ):
        url, expires_at = presign_cos_get(
            bucket="b1",
            region="ap-guangzhou",
            key="music/a b.mp3",
            secret_id="AKIDx",
            secret_key="skx",
            expires_seconds=900,
        )

    assert url == (
        "https://b1.cos.ap-guangzhou.myqcloud.com/music/a%20b.mp3"
        "?q-sign-algorithm=sha1&q-ak=AKIDx"
        "&q-sign-time=1700000000;1700000900"
        "&q-key-time=1700000000;1700000900"
        "&q-header-list=&q-url-param-list="
        "&q-signature=7e3e47f1975f75d35e4c46ee828e0781bc3efd4b"
    )
    assert expires_at == datetime(2023, 11, 14, 22, 28, 20, tzinfo=UTC)


def test_presign_cos_get_encodes_unicode_key_but_keeps_slashes() -> None:
    url, _ = presign_cos_get(
        bucket="bible-1328751369",
        region="ap-guangzhou",
        key="music/221-我罪极重.mp3",
        secret_id="AKIDx",
        secret_key="skx",
    )
    assert url.startswith(
        "https://bible-1328751369.cos.ap-guangzhou.myqcloud.com/music/221-"
    )
    assert "%2F" not in url  # path separators stay literal slashes
    assert "%E6" in url  # unicode is percent-encoded


def test_presign_cos_get_requires_credentials() -> None:
    with pytest.raises(ValueError):
        presign_cos_get("b1", "ap-guangzhou", "music/x.mp3", "", "")
