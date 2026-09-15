from pathlib import Path

from fastapi.testclient import TestClient

from rhythm_metadata_api.application.device_auth import hash_admin_password
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.main import create_app


def test_private_api_accepts_admin_login_without_expanding_catalog_scope(
    tmp_path: Path,
) -> None:
    settings = Settings(
        bootstrap_token="private-catalog-token",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        public_token_secret="test-only-secret-that-is-longer-than-32-bytes",
        public_admin_username="owner",
        public_admin_password_hash=hash_admin_password("correct horse battery staple"),
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v2/admin/session",
            json={"username": "owner", "password": "correct horse battery staple"},
        )
        assert response.status_code == 200
        admin_token = response.json()["accessToken"]

        devices = client.get(
            "/v2/admin/devices",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert devices.status_code == 200

        catalog = client.get(
            "/v2/works",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert catalog.status_code == 401

        private_catalog = client.get(
            "/v2/works",
            headers={"Authorization": "Bearer private-catalog-token"},
        )
        assert private_catalog.status_code == 200
