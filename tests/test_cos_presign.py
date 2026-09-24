from datetime import UTC, datetime
from unittest import mock

import pytest

from rhythm_metadata_api.infrastructure.storage.cos_presign import (
    presign_cos_get,
    presign_cos_post,
    presign_cos_put,
)


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
        "&q-header-list=host&q-url-param-list="
        "&q-signature=783730746a6143aa1c3aefecfa2853ef4169ec04"
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
    assert url.startswith("https://bible-1328751369.cos.ap-guangzhou.myqcloud.com/music/221-")
    assert "%2F" not in url  # path separators stay literal slashes
    assert "%E6" in url  # unicode is percent-encoded


def test_presign_cos_get_requires_credentials() -> None:
    with pytest.raises(ValueError):
        presign_cos_get("b1", "ap-guangzhou", "music/x.mp3", "", "")


def test_presign_cos_put_signs_the_exact_object_and_method() -> None:
    get_url, _ = presign_cos_get(
        "chorus-1328751369",
        "ap-guangzhou",
        "tracks/user/take.m4a",
        "secret-id",
        "secret-key",
    )
    put_url, _ = presign_cos_put(
        "chorus-1328751369",
        "ap-guangzhou",
        "tracks/user/take.m4a",
        "secret-id",
        "secret-key",
    )

    assert put_url.startswith(
        "https://chorus-1328751369.cos.ap-guangzhou.myqcloud.com/tracks/user/take.m4a?"
    )
    assert "q-header-list=host" in put_url
    assert put_url != get_url


def test_presign_cos_put_binds_required_content_headers() -> None:
    first, _ = presign_cos_put(
        "images-1250000000",
        "ap-guangzhou",
        "labs/images/tmp/upload/original",
        "secret-id",
        "secret-key",
        headers={"Content-Type": "image/png", "Content-MD5": "AAAAAAAAAAAAAAAAAAAAAA=="},
    )
    changed, _ = presign_cos_put(
        "images-1250000000",
        "ap-guangzhou",
        "labs/images/tmp/upload/original",
        "secret-id",
        "secret-key",
        headers={"Content-Type": "image/png", "Content-MD5": "AQAAAAAAAAAAAAAAAAAAAA=="},
    )

    assert "q-header-list=content-md5;content-type;host" in first
    assert first != changed


def test_presign_cos_get_double_encodes_signed_ci_recipe_in_parameter_list() -> None:
    with mock.patch(
        "rhythm_metadata_api.infrastructure.storage.cos_presign.time.time",
        return_value=1_700_000_000,
    ):
        url, _ = presign_cos_get(
            "images-1250000000",
            "ap-guangzhou",
            "labs/images/assets/a1/digest",
            "secret-id",
            "secret-key",
            query_parameters=(("imageMogr2/thumbnail/512x512>", None),),
            host="preview.example.test",
        )

    assert url.startswith("https://preview.example.test/")
    assert "q-url-param-list=imagemogr2%252fthumbnail%252f512x512%253e" in url
    assert "imageMogr2%2Fthumbnail%2F512x512%3E=" in url


def test_presign_cos_post_supports_ci_control_plane_host() -> None:
    url, _ = presign_cos_post(
        "images-1250000000",
        "ap-guangzhou",
        "file_bucket",
        "secret-id",
        "secret-key",
        headers={"Content-Type": "application/xml"},
        host="images-1250000000.ci.ap-guangzhou.myqcloud.com",
    )

    assert url.startswith(
        "https://images-1250000000.ci.ap-guangzhou.myqcloud.com/file_bucket?"
    )
    assert "q-header-list=content-type;host" in url
