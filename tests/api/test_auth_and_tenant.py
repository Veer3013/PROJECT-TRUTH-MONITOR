from __future__ import annotations

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import OrganizationMembership, Project, User
from app.services.normalization import comparison_text


def test_login_me_and_logout_require_real_actor(client, auth) -> None:
    headers = auth("analyst")
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["membership"]["role"] == "analyst"
    logout = client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204


def test_html_login_form_enters_dashboard(client) -> None:
    response = client.post(
        "/login",
        data={
            "email": "admin@example.com",
            "password": "TestPassword!2026",
            "next": "/dashboard",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert response.cookies.get(get_settings().session_cookie_name)

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "ProjectTruth Monitor" in dashboard.text


def test_api_login_accepts_documented_local_demo_domain(client, db, identity) -> None:
    user = User(
        email="demo@projecttruth.local",
        password_hash=hash_password("LocalDemo!2026"),
        display_name="Local Demo",
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(
        OrganizationMembership(
            organization_id=identity["organization"].id,
            user_id=user.id,
            role="analyst",
            is_active=True,
        )
    )
    db.commit()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "demo@projecttruth.local", "password": "LocalDemo!2026"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == "demo@projecttruth.local"


def test_viewer_cannot_mutate_and_analyst_cannot_administer(client, auth) -> None:
    viewer = auth("viewer")
    denied = client.post(
        "/api/v1/watchlists",
        json={"name": "Forbidden", "monitoring_cadence": "weekly"},
        headers=viewer,
    )
    assert denied.status_code == 403
    analyst = auth("analyst")
    denied_admin = client.post(
        "/api/v1/organizations/current/members",
        json={
            "email": "new@example.com",
            "display_name": "New User",
            "role": "viewer",
            "temporary_password": "Temporary!2026",
        },
        headers=analyst,
    )
    assert denied_admin.status_code == 403


def test_cross_organization_project_access_is_hidden(client, auth, db, identity) -> None:
    project = Project(
        organization_id=identity["other_organization"].id,
        name="Other Tenant Project",
        normalized_name=comparison_text("Other Tenant Project"),
        registration_number="HRERA-OTHER-1",
        identity_status="confirmed",
        is_provisional=False,
    )
    db.add(project)
    db.commit()
    response = client.get(f"/api/v1/projects/{project.id}", headers=auth("analyst"))
    assert response.status_code == 404
