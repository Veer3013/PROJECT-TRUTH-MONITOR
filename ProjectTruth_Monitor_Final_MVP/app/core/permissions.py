from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, status


class Role(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"
    ANALYST = "analyst"
    VIEWER = "viewer"


ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.ANALYST: 1,
    Role.REVIEWER: 2,
    Role.ADMIN: 3,
}


def role_allows(actual: str | Role, minimum: str | Role) -> bool:
    try:
        return ROLE_RANK[Role(actual)] >= ROLE_RANK[Role(minimum)]
    except ValueError:
        return False


def enforce_role(actual: str | Role, minimum: str | Role) -> None:
    if not role_allows(actual, minimum):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "insufficient_role", "message": "Your role cannot perform this action."},
        )

