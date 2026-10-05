from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from alembic import command

TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="projecttruth-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_RUNTIME / 'tests.db').as_posix()}"
os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "test-only-secret-key-at-least-thirty-two-characters"
os.environ["UPLOAD_DIR"] = str(TEST_RUNTIME / "uploads")
os.environ["REPORT_DIR"] = str(TEST_RUNTIME / "reports")
os.environ["ENABLE_SCHEDULER"] = "false"


def _cleanup_runtime() -> None:
    shutil.rmtree(TEST_RUNTIME, ignore_errors=True)


atexit.register(_cleanup_runtime)


from app.core.rate_limit import rate_limiter  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import Base, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, OrganizationMembership, User  # noqa: E402

config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
command.upgrade(config, "head")
PASSWORD = "TestPassword!2026"
PASSWORD_HASH = hash_password(PASSWORD)


@pytest.fixture
def db() -> Session:
    # API handlers intentionally call ``commit``.  SQLite's savepoint behavior
    # makes an outer-transaction test fixture unreliable in that situation, so
    # isolate tests by clearing rows in dependency order instead.
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    session = Session(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())


@pytest.fixture
def identity(db: Session) -> dict[str, object]:
    org = Organization(name="Test Advisory", slug="test-advisory")
    other_org = Organization(name="Other Advisory", slug="other-advisory")
    db.add_all([org, other_org])
    db.flush()
    users: dict[str, User] = {}
    for role in ("admin", "reviewer", "analyst", "viewer"):
        user = User(
            email=f"{role}@example.com",
            password_hash=PASSWORD_HASH,
            display_name=role.title(),
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(
            OrganizationMembership(
                organization_id=org.id,
                user_id=user.id,
                role=role,
                is_active=True,
            )
        )
        users[role] = user
    other_user = User(
        email="other@example.com",
        password_hash=PASSWORD_HASH,
        display_name="Other Analyst",
        is_active=True,
    )
    db.add(other_user)
    db.flush()
    db.add(
        OrganizationMembership(
            organization_id=other_org.id,
            user_id=other_user.id,
            role="analyst",
            is_active=True,
        )
    )
    db.commit()
    return {"organization": org, "other_organization": other_org, "users": users, "other_user": other_user}


@pytest.fixture
def client(db: Session, identity: dict[str, object]):
    del identity

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    rate_limiter.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limiter.clear()


@pytest.fixture
def auth(client: TestClient):
    def login(role: str = "analyst") -> dict[str, str]:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": f"{role}@example.com", "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['token']}"}

    return login
