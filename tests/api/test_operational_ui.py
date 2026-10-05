from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Location, Organization, Report, Source, User, Watchlist
from app.seed_demo import seed_demo


@pytest.fixture
def seeded_operational_ui(client, db):
    seed_demo(db)
    db.commit()
    login = client.post(
        "/login",
        data={
            "email": "analyst@projecttruth.local",
            "password": "DemoAnalyst!2026",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    return client, db


def test_dashboard_and_watchlists_render_seeded_operational_data(
    seeded_operational_ui,
) -> None:
    client, db = seeded_operational_ui

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Metro Square" in dashboard.text
    assert "Project pending identity review" not in dashboard.text
    assert "3 configured sources" in dashboard.text
    assert "100%" in dashboard.text

    listing = client.get("/watchlists")
    assert listing.status_code == 200
    assert "Gurugram Pilot" in listing.text
    assert "<td>4</td>" in listing.text
    assert "Weekly" in listing.text
    assert "Current" in listing.text

    watchlist = db.scalar(select(Watchlist).where(Watchlist.name == "Gurugram Pilot"))
    assert watchlist is not None
    detail = client.get(f"/watchlists/{watchlist.id}")
    assert detail.status_code == 200
    assert "Aster Heights Phase II" in detail.text
    assert "Lakeview Residences" in detail.text
    assert "Orion Greens Phase III" in detail.text
    assert "Metro Square" in detail.text
    assert "Sector 79" in detail.text
    assert "Project identity pending" not in detail.text
    assert "Not yet verified" not in detail.text


def test_source_health_digest_market_and_reports_render_read_models(
    seeded_operational_ui,
) -> None:
    client, db = seeded_operational_ui

    health = client.get("/sources/health")
    assert health.status_code == 200
    assert "ProjectTruth frozen HRERA demo fixtures" in health.text
    assert "policy 1.0.0" in health.text
    assert "fixture_html" in health.text
    assert "100%" in health.text

    digest = client.get("/digest")
    assert digest.status_code == 200
    assert "Metro Square" in digest.text
    assert "Official completion is missing" in digest.text
    assert "OFFICIAL VS DEVELOPER CONFLICT" in digest.text
    assert 'href="/alerts/' in digest.text
    assert "Date not recorded" not in digest.text

    sector_79 = db.scalar(
        select(Location).where(Location.locality_or_sector == "Sector 79")
    )
    sector_65 = db.scalar(
        select(Location).where(Location.locality_or_sector == "Sector 65")
    )
    assert sector_79 is not None and sector_65 is not None

    market_79 = client.get(f"/markets/summary?location_id={sector_79.id}")
    assert market_79.status_code == 200
    assert "Sector 79, Gurugram" in market_79.text
    assert "Aster Demo Developers Private Limited" in market_79.text
    assert f'value="{sector_79.id}" selected' in market_79.text

    market_65 = client.get(f"/markets/summary?location_id={sector_65.id}")
    assert market_65.status_code == 200
    assert "Sector 65, Gurugram" in market_65.text
    assert "Metro Demo Developments LLP" in market_65.text
    assert f'value="{sector_65.id}" selected' in market_65.text

    reports = client.get("/reports")
    assert reports.status_code == 200
    assert "Gurugram Pilot Weekly Evidence Digest" in reports.text
    assert "Demo Analyst" in reports.text
    assert "6 items" in reports.text
    assert "required-review item(s) must be resolved before approval" in reports.text
    assert "Artifacts not generated or no longer available" in reports.text
    assert 'name="formats" value="evidence"' in reports.text
    assert reports.text.count('name="project_ids"') == 4


def test_operational_pages_paginate_and_reject_cross_tenant_rows(
    seeded_operational_ui,
) -> None:
    client, db = seeded_operational_ui
    demo_org = db.scalar(
        select(Organization).where(Organization.slug == "projecttruth-demo")
    )
    other_org = db.scalar(select(Organization).where(Organization.slug == "other-advisory"))
    analyst = db.scalar(
        select(User).where(User.email == "analyst@projecttruth.local")
    )
    other_user = db.scalar(select(User).where(User.email == "other@example.com"))
    assert demo_org is not None and other_org is not None
    assert analyst is not None and other_user is not None

    for index in range(18):
        db.add(
            Source(
                organization_id=demo_org.id,
                name=f"ZZ Tenant Source {index:02d}",
                source_type="fixture",
                source_class="official_regulator",
                domain=f"source-{index}.projecttruth.invalid",
                is_enabled=True,
                health_status="unknown",
            )
        )
    for index in range(20):
        db.add(
            Report(
                organization_id=demo_org.id,
                title=f"ZZ Paginated Report {index:02d}",
                report_type="weekly_digest",
                state="draft",
                generated_by_user_id=analyst.id,
                expires_at=datetime.now(UTC) + timedelta(days=30),
                product_disclaimer="Test report.",
                summary_metrics={"formats": []},
                limitations=[],
                deduplication_key=f"pagination-report-{index}",
            )
        )
    other_location = Location(
        organization_id=other_org.id,
        country="India",
        state="Haryana",
        city="Gurugram",
        locality_or_sector="Other Tenant Sector",
    )
    db.add_all(
        [
            other_location,
            Source(
                organization_id=other_org.id,
                name="OTHER TENANT SECRET SOURCE",
                source_type="fixture",
                source_class="official_regulator",
                domain="other-tenant.projecttruth.invalid",
                is_enabled=True,
            ),
            Report(
                organization_id=other_org.id,
                title="OTHER TENANT SECRET REPORT",
                report_type="weekly_digest",
                state="draft",
                generated_by_user_id=other_user.id,
                product_disclaimer="Other tenant.",
                summary_metrics={},
                limitations=[],
                deduplication_key="other-tenant-report",
            ),
        ]
    )
    db.commit()

    health_page_two = client.get("/sources/health?page=2")
    assert health_page_two.status_code == 200
    assert "Page 2 of 2" in health_page_two.text
    assert "ZZ Tenant Source 17" in health_page_two.text
    assert "ZZ Tenant Source 00" not in health_page_two.text
    assert "OTHER TENANT SECRET SOURCE" not in health_page_two.text

    reports_page_two = client.get("/reports?page=2")
    assert reports_page_two.status_code == 200
    assert "Page 2 of 2" in reports_page_two.text
    assert "Gurugram Pilot Weekly Evidence Digest" in reports_page_two.text
    assert "OTHER TENANT SECRET REPORT" not in reports_page_two.text

    cross_tenant_market = client.get(
        f"/markets/summary?location_id={other_location.id}"
    )
    assert cross_tenant_market.status_code == 404


def test_html_login_and_existing_session_reject_inactive_organization(
    client,
    db,
    identity,
) -> None:
    login = client.post(
        "/login",
        data={"email": "analyst@example.com", "password": "TestPassword!2026"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    organization = identity["organization"]
    organization.is_active = False
    db.commit()

    dashboard = client.get("/dashboard", follow_redirects=False)
    assert dashboard.status_code == 303
    assert dashboard.headers["location"].startswith("/login")

    client.cookies.clear()
    rejected_login = client.post(
        "/login",
        data={"email": "analyst@example.com", "password": "TestPassword!2026"},
        follow_redirects=False,
    )
    assert rejected_login.status_code == 403
    assert "No active organization access was found" in rejected_login.text
