from __future__ import annotations

from sqlalchemy import select

from app.models import EntityMergeProposal, Project


def test_watchlist_project_and_csv_import_are_idempotent(client, auth) -> None:
    headers = auth("analyst")
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Pilot", "monitoring_cadence": "weekly"},
        headers=headers,
    )
    assert watchlist.status_code == 201, watchlist.text
    watchlist_id = watchlist.json()["id"]

    project_payload = {
        "registration_number": "HRERA-TEST-101",
        "name": "Test Heights",
        "developer_name": "Test Developer",
        "city": "Gurugram",
        "locality_or_sector": "Sector 79",
    }
    first_project = client.post("/api/v1/projects", json=project_payload, headers=headers)
    second_project = client.post("/api/v1/projects", json=project_payload, headers=headers)
    assert first_project.status_code == 201
    assert second_project.json()["id"] == first_project.json()["id"]

    csv_text = (
        "registration_number,project_name,developer_name,city,locality_or_sector,official_url,monitoring_cadence\n"
        "HRERA-TEST-102,CSV One,Dev One,Gurugram,Sector 80,,weekly\n"
        "HRERA-TEST-102,CSV One duplicate,Dev One,Gurugram,Sector 80,,weekly\n"
    )
    preview = client.post(
        "/api/v1/imports/projects/preview", json={"csv_text": csv_text}, headers=headers
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"][1]["duplicate"] is True
    commit_payload = {
        "watchlist_id": watchlist_id,
        "preview_token": preview.json()["preview_token"],
        "idempotency_key": "csv-test-idempotency-101",
    }
    committed = client.post(
        "/api/v1/imports/projects/commit", json=commit_payload, headers=headers
    )
    repeated = client.post(
        "/api/v1/imports/projects/commit", json=commit_payload, headers=headers
    )
    assert committed.status_code == 201, committed.text
    assert repeated.status_code == 201
    assert repeated.json()["created_count"] == 0


def test_probable_alias_creates_reviewed_reversible_merge(client, auth, db) -> None:
    analyst = auth("analyst")
    common = {
        "name": "Orion Greens",
        "developer_name": "Orion Demo Estates",
        "city": "Gurugram",
        "locality_or_sector": "Sector 79",
    }
    canonical = client.post(
        "/api/v1/projects",
        json={**common, "registration_number": "HRERA-MERGE-301"},
        headers=analyst,
    )
    candidate = client.post(
        "/api/v1/projects",
        json={**common, "registration_number": "HRERA-MERGE-303"},
        headers=analyst,
    )

    assert canonical.status_code == 201 and candidate.status_code == 201
    assert candidate.json()["id"] != canonical.json()["id"]
    assert candidate.json()["identity_status"] == "needs_review"
    assert candidate.json()["is_provisional"] is True

    proposal = db.scalar(
        select(EntityMergeProposal).where(
            EntityMergeProposal.source_entity_id == candidate.json()["id"],
            EntityMergeProposal.target_entity_id == canonical.json()["id"],
        )
    )
    assert proposal is not None

    reviewer = auth("reviewer")
    confirmed = client.post(
        f"/api/v1/entity-merges/{proposal.id}/review",
        json={"decision": "confirm", "note": "Reviewed as the same canonical project."},
        headers=reviewer,
    )
    assert confirmed.status_code == 200, confirmed.text

    merged_source = db.get(Project, candidate.json()["id"])
    assert merged_source is not None
    db.refresh(merged_source)
    assert merged_source.merged_into_project_id == canonical.json()["id"]
    replay = client.post(
        "/api/v1/projects",
        json={**common, "registration_number": "HRERA-MERGE-303"},
        headers=analyst,
    )
    assert replay.json()["id"] == canonical.json()["id"]

    reversed_response = client.post(
        f"/api/v1/entity-merges/{proposal.id}/reverse",
        json={"note": "New evidence confirms a distinct registered phase."},
        headers=reviewer,
    )
    assert reversed_response.status_code == 200, reversed_response.text
    db.refresh(merged_source)
    assert merged_source.merged_into_project_id is None
    assert merged_source.identity_status == "confirmed"
