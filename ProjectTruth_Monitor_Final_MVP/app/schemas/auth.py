from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=200)

    @field_validator("email")
    @classmethod
    def normalize_login_email(cls, value: str) -> str:
        email = value.strip().lower()
        local, separator, domain = email.partition("@")
        if not separator or not local or not domain or any(char.isspace() for char in email):
            raise ValueError("Enter a valid email address.")
        return email


class UserResponse(ORMModel):
    id: str
    email: str
    display_name: str
    is_active: bool


class MembershipResponse(ORMModel):
    id: str
    organization_id: str
    role: str
    is_active: bool


class SessionResponse(BaseModel):
    token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a credential
    csrf_token: str
    user: UserResponse
    membership: MembershipResponse


class MeResponse(BaseModel):
    user: UserResponse
    membership: MembershipResponse
