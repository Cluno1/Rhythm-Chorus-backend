from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


def _camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)

class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)


class AdminSessionRequest(CamelModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1000)


class AdminSessionResponse(CamelModel):
    access_token: str
    expires_in: int


class InviteCreateRequest(CamelModel):
    user_id: str = Field(pattern=r"^[A-Za-z0-9._@+-]{1,100}$")
    display_name: str | None = Field(default=None, max_length=300)
    replace_existing_device: bool = False


class InviteCreateResponse(CamelModel):
    invite_code: str
    user_id: str
    expires_at: str


class EnrollmentChallengeRequest(CamelModel):
    invite_code: str = Field(min_length=20, max_length=200)


class NonceResponse(CamelModel):
    nonce: str
    expires_at: str


class DeviceEnrollRequest(CamelModel):
    invite_code: str = Field(min_length=20, max_length=200)
    nonce: str = Field(min_length=20, max_length=200)
    public_key_spki: str = Field(min_length=80, max_length=2000)
    signature: str = Field(min_length=40, max_length=1000)
    display_name: str | None = Field(default=None, max_length=300)


class DeviceSessionResponse(CamelModel):
    user_id: str
    device_id: str
    session_id: str
    access_token: str
    access_token_expires_in: int
    session_expires_at: str


class DeviceNonceRequest(CamelModel):
    device_id: str = Field(min_length=36, max_length=36)


class DeviceRefreshRequest(CamelModel):
    device_id: str = Field(min_length=36, max_length=36)
    session_id: str = Field(min_length=36, max_length=36)
    timestamp: int
    nonce: str = Field(min_length=20, max_length=200)
    signature: str = Field(min_length=40, max_length=1000)


class DeviceStatusResponse(CamelModel):
    user_id: str
    device_id: str | None
    display_name: str | None
    status: str | None
    last_seen_at: str | None


class RevokeResponse(CamelModel):
    revoked: bool
