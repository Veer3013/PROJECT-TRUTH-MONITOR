from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AnalystActor, CSRFProtected, CurrentActor, DBSession, ReviewerActor
from app.api.utils import add_audit_event, commit_or_rollback
from app.api.v1.projects import get_tenant_project
from app.models import (
    Alert,
    AlertReview,
    ChangeEvidence,
    DetectedChange,
    EntityMergeProposal,
    ProjectAlias,
    SourceConflict,
    UserCorrection,
)
from app.schemas.alerts import AlertResponse, AlertReviewCreate, EvidenceResponse
from app.schemas.projects import (
    ChangeResponse,
    ConflictReview,
    CorrectionCreate,
    CorrectionReversal,
    CorrectionReview,
    MergeReversal,
    MergeReview,
)
from app.services.normalization import comparison_text

router = APIRouter(tags=["Changes and review"])


def get_tenant_alert(db: DBSession, organization_id: str, alert_id: str) -> Alert:
    alert = db.scalar(
        select(Alert).where(
            Alert.id == alert_id,
            Alert.organization_id == organization_id,
        )
    )
    if alert is None:
        raise HTTPException(status_code=404, detail={"code": "alert_not_found"})
    return alert


@router.get("/changes/{change_id}", response_model=ChangeResponse)
def get_change(change_id: str, db: DBSession, actor: CurrentActor) -> DetectedChange:
    change = db.scalar(
        select(DetectedChange).where(
            DetectedChange.id == change_id,
            DetectedChange.organization_id == actor.organization_id,
        )
    )
    if change is None:
        raise HTTPException(status_code=404, detail={"code": "change_not_found"})
    return change


@router.get("/changes/{change_id}/evidence", response_model=list[EvidenceResponse])
def get_change_evidence(
    change_id: str, db: DBSession, actor: CurrentActor
) -> list[ChangeEvidence]:
    get_change(change_id, db, actor)
    return list(
        db.scalars(
            select(ChangeEvidence)
            .where(
                ChangeEvidence.organization_id == actor.organization_id,
                ChangeEvidence.detected_change_id == change_id,
            )
            .order_by(ChangeEvidence.evidence_side)
        )
    )


@router.get("/alerts", response_model=list[AlertResponse])
def list_alerts(
    db: DBSession,
    actor: CurrentActor,
    state: str | None = None,
    severity: str | None = None,
) -> list[Alert]:
    query = select(Alert).where(Alert.organization_id == actor.organization_id)
    if state:
        query = query.where(Alert.state == state)
    if severity:
        query = query.where(Alert.severity == severity)
    return list(db.scalars(query.order_by(Alert.first_seen_at.desc())))


@router.get("/alerts/{alert_id}", response_model=AlertResponse)
def get_alert(alert_id: str, db: DBSession, actor: CurrentActor) -> Alert:
    return get_tenant_alert(db, actor.organization_id, alert_id)


def _review_alert(
    *,
    alert: Alert,
    payload: AlertReviewCreate,
    request: Request,
    db: DBSession,
    actor: object,
) -> Alert:
    before = {"state": alert.state}
    review = AlertReview(
        organization_id=actor.organization_id,
        alert_id=alert.id,
        reviewer_user_id=actor.user_id,
        decision=payload.decision,
        reason_code=payload.reason_code,
        note=payload.note,
        before_reference=before,
        after_reference={"state": payload.decision},
        is_reversible=True,
    )
    db.add(review)
    state_map = {
        "confirm": "confirmed",
        "dismiss": "dismissed",
        "correct": "corrected",
        "request_another_source": "unresolved",
        "mark_expected": "expected",
        "keep_unresolved": "unresolved",
    }
    alert.state = state_map[payload.decision]
    alert.resolved_at = (
        None if payload.decision in {"request_another_source", "keep_unresolved"} else datetime.now(UTC)
    )
    if alert.detected_change_id:
        change = db.scalar(
            select(DetectedChange).where(
                DetectedChange.id == alert.detected_change_id,
                DetectedChange.organization_id == actor.organization_id,
            )
        )
        if change:
            change.review_state = alert.state
    add_audit_event(
        db,
        actor=actor,
        action="alert.reviewed",
        resource_type="alert",
        resource_id=alert.id,
        request=request,
        metadata={"decision": payload.decision, "reason_code": payload.reason_code},
    )
    commit_or_rollback(db)
    db.refresh(alert)
    return alert


@router.post("/alerts/{alert_id}/review", response_model=AlertResponse)
def review_alert(
    alert_id: str,
    payload: AlertReviewCreate,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> Alert:
    del csrf
    alert = get_tenant_alert(db, actor.organization_id, alert_id)
    return _review_alert(alert=alert, payload=payload, request=request, db=db, actor=actor)


@router.post("/alerts/{alert_id}/dismiss", response_model=AlertResponse)
def dismiss_alert(
    alert_id: str,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> Alert:
    del csrf
    alert = get_tenant_alert(db, actor.organization_id, alert_id)
    payload = AlertReviewCreate(
        decision="dismiss", reason_code="reviewer_dismissed", note=None
    )
    return _review_alert(alert=alert, payload=payload, request=request, db=db, actor=actor)


@router.post("/corrections")
def create_correction(
    payload: CorrectionCreate,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    del csrf
    get_tenant_project(db, actor.organization_id, payload.project_id)
    correction = UserCorrection(
        organization_id=actor.organization_id,
        project_id=payload.project_id,
        field_key=payload.field_key,
        corrected_value=payload.corrected_value,
        reason_code="user_submitted",
        note=payload.reason,
        status="pending",
        submitted_by_user_id=actor.user_id,
    )
    db.add(correction)
    db.flush()
    add_audit_event(
        db,
        actor=actor,
        action="correction.submitted",
        resource_type="user_correction",
        resource_id=correction.id,
        request=request,
        metadata={"project_id": payload.project_id, "field_key": payload.field_key},
    )
    commit_or_rollback(db)
    return {
        "id": correction.id,
        "status": correction.status,
        "message": "The correction is preserved separately and awaits review.",
    }


@router.post("/corrections/{correction_id}/review")
def review_correction(
    correction_id: str,
    payload: CorrectionReview,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    del csrf
    correction = db.scalar(
        select(UserCorrection).where(
            UserCorrection.id == correction_id,
            UserCorrection.organization_id == actor.organization_id,
        )
    )
    if correction is None:
        raise HTTPException(status_code=404, detail={"code": "correction_not_found"})
    if correction.status not in {"pending", "unresolved"}:
        raise HTTPException(status_code=409, detail={"code": "correction_already_decided"})
    correction.status = {
        "confirm": "confirmed",
        "dismiss": "dismissed",
        "keep_unresolved": "unresolved",
    }[payload.decision]
    correction.reviewed_by_user_id = actor.user_id
    correction.decided_at = datetime.now(UTC)
    if payload.note:
        correction.note = " ".join(filter(None, (correction.note, payload.note)))
    add_audit_event(
        db,
        actor=actor,
        action="correction.reviewed",
        resource_type="user_correction",
        resource_id=correction.id,
        request=request,
        metadata={"decision": payload.decision},
    )
    commit_or_rollback(db)
    return {"id": correction.id, "status": correction.status, "reversible": True}


@router.post("/corrections/{correction_id}/reverse")
def reverse_correction(
    correction_id: str,
    payload: CorrectionReversal,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    del csrf
    correction = db.scalar(
        select(UserCorrection).where(
            UserCorrection.id == correction_id,
            UserCorrection.organization_id == actor.organization_id,
        )
    )
    if correction is None:
        raise HTTPException(status_code=404, detail={"code": "correction_not_found"})
    if correction.status != "confirmed" or correction.is_reversed:
        raise HTTPException(status_code=409, detail={"code": "correction_not_reversible"})
    correction.is_reversed = True
    correction.status = "reversed"
    reversal = UserCorrection(
        organization_id=actor.organization_id,
        project_id=correction.project_id,
        field_key=correction.field_key,
        previous_value=correction.corrected_value,
        corrected_value=correction.previous_value,
        reason_code="reviewed_correction_reversed",
        note=payload.note,
        status="confirmed",
        submitted_by_user_id=actor.user_id,
        reviewed_by_user_id=actor.user_id,
        decided_at=datetime.now(UTC),
        related_alert_id=correction.related_alert_id,
        related_conflict_id=correction.related_conflict_id,
        source_snapshot_id=correction.source_snapshot_id,
        reverses_correction_id=correction.id,
    )
    db.add(reversal)
    db.flush()
    add_audit_event(
        db,
        actor=actor,
        action="correction.reversed",
        resource_type="user_correction",
        resource_id=correction.id,
        request=request,
        metadata={"reversal_id": reversal.id},
    )
    commit_or_rollback(db)
    return {
        "id": correction.id,
        "status": correction.status,
        "reversal_id": reversal.id,
        "reversible": False,
    }


@router.post("/entity-merges/{proposal_id}/review")
def review_merge_proposal(
    proposal_id: str,
    payload: MergeReview,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    del csrf
    proposal = db.scalar(
        select(EntityMergeProposal).where(
            EntityMergeProposal.id == proposal_id,
            EntityMergeProposal.organization_id == actor.organization_id,
        )
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail={"code": "merge_proposal_not_found"})
    if proposal.status not in {"pending", "unresolved"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "merge_proposal_already_decided"},
        )
    proposal.status = {
        "confirm": "confirmed",
        "dismiss": "dismissed",
        "keep_unresolved": "unresolved",
    }[payload.decision]
    proposal.note = payload.note
    proposal.reviewed_by_user_id = actor.user_id
    proposal.decided_at = datetime.now(UTC)
    if payload.decision == "confirm" and proposal.entity_type == "project":
        source = get_tenant_project(db, actor.organization_id, proposal.source_entity_id)
        target = get_tenant_project(db, actor.organization_id, proposal.target_entity_id)
        source.merged_into_project_id = target.id
        source.identity_status = "merged"
        source.is_provisional = True
        normalized_alias = comparison_text(source.name)
        alias = db.scalar(
            select(ProjectAlias).where(
                ProjectAlias.organization_id == actor.organization_id,
                ProjectAlias.project_id == target.id,
                ProjectAlias.normalized_alias == normalized_alias,
            )
        )
        if alias is None:
            db.add(
                ProjectAlias(
                    organization_id=actor.organization_id,
                    project_id=target.id,
                    alias_name=source.name,
                    normalized_alias=normalized_alias,
                    alias_type="reviewed_merge",
                    original_value=source.name,
                    confidence=proposal.confidence,
                    review_state="confirmed",
                )
            )
    add_audit_event(
        db,
        actor=actor,
        action="entity_merge.reviewed",
        resource_type="entity_merge_proposal",
        resource_id=proposal.id,
        request=request,
        metadata={"decision": payload.decision},
    )
    commit_or_rollback(db)
    return {"id": proposal.id, "status": proposal.status, "reversible": True}


@router.post("/entity-merges/{proposal_id}/reverse")
def reverse_merge_proposal(
    proposal_id: str,
    payload: MergeReversal,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    del csrf
    proposal = db.scalar(
        select(EntityMergeProposal).where(
            EntityMergeProposal.id == proposal_id,
            EntityMergeProposal.organization_id == actor.organization_id,
        )
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail={"code": "merge_proposal_not_found"})
    if proposal.status != "confirmed" or proposal.entity_type != "project":
        raise HTTPException(
            status_code=409,
            detail={"code": "merge_proposal_not_reversible"},
        )
    source = get_tenant_project(db, actor.organization_id, proposal.source_entity_id)
    if source.merged_into_project_id != proposal.target_entity_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "merge_state_changed"},
        )
    source.merged_into_project_id = None
    source.identity_status = "confirmed" if source.registration_number else "unverified"
    source.is_provisional = not bool(source.registration_number)
    normalized_alias = comparison_text(source.name)
    alias = db.scalar(
        select(ProjectAlias).where(
            ProjectAlias.organization_id == actor.organization_id,
            ProjectAlias.project_id == proposal.target_entity_id,
            ProjectAlias.normalized_alias == normalized_alias,
            ProjectAlias.alias_type == "reviewed_merge",
        )
    )
    if alias is not None:
        alias.review_state = "reversed"
    proposal.status = "reversed"
    reversal = EntityMergeProposal(
        organization_id=actor.organization_id,
        entity_type="project",
        source_entity_id=proposal.target_entity_id,
        target_entity_id=proposal.source_entity_id,
        status="confirmed",
        confidence=proposal.confidence,
        reason_codes=["reviewed_merge_reversed"],
        note=payload.note,
        proposed_by_user_id=actor.user_id,
        reviewed_by_user_id=actor.user_id,
        decided_at=datetime.now(UTC),
        reversal_of_proposal_id=proposal.id,
        deduplication_key=hashlib.sha256(
            f"merge-reversal:{actor.organization_id}:{proposal.id}".encode()
        ).hexdigest(),
    )
    db.add(reversal)
    db.flush()
    add_audit_event(
        db,
        actor=actor,
        action="entity_merge.reversed",
        resource_type="entity_merge_proposal",
        resource_id=proposal.id,
        request=request,
        metadata={"reversal_id": reversal.id},
    )
    commit_or_rollback(db)
    return {
        "id": proposal.id,
        "status": proposal.status,
        "reversal_id": reversal.id,
        "reversible": False,
    }


@router.get("/conflicts")
def list_conflicts(db: DBSession, actor: CurrentActor) -> list[dict[str, object]]:
    conflicts = db.scalars(
        select(SourceConflict)
        .where(SourceConflict.organization_id == actor.organization_id)
        .order_by(SourceConflict.detected_at.desc())
    )
    return [
        {
            "id": item.id,
            "project_id": item.project_id,
            "field_key": item.field_key,
            "official_value": item.source_a_value,
            "other_value": item.source_b_value,
            "official_source_class": item.source_a_class,
            "other_source_class": item.source_b_class,
            "state": item.state,
            "detected_at": item.detected_at,
        }
        for item in conflicts
    ]


@router.post("/conflicts/{conflict_id}/review")
def review_conflict(
    conflict_id: str,
    payload: ConflictReview,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    del csrf
    conflict = db.scalar(
        select(SourceConflict).where(
            SourceConflict.id == conflict_id,
            SourceConflict.organization_id == actor.organization_id,
        )
    )
    if conflict is None:
        raise HTTPException(status_code=404, detail={"code": "conflict_not_found"})
    if payload.decision in {"request_another_source", "keep_unresolved"}:
        conflict.state = "unresolved"
        conflict.selected_value = None
        conflict.resolved_at = None
    else:
        conflict.state = "resolved"
        conflict.selected_value = (
            conflict.source_a_value
            if payload.decision in {"select_official", "prefer_official_display"}
            else conflict.source_b_value
        )
        conflict.resolved_at = datetime.now(UTC)
    conflict.resolved_by_user_id = actor.user_id
    conflict.resolution_reason = payload.reason
    add_audit_event(
        db,
        actor=actor,
        action="source_conflict.reviewed",
        resource_type="source_conflict",
        resource_id=conflict.id,
        request=request,
        metadata={"decision": payload.decision},
    )
    commit_or_rollback(db)
    return {
        "id": conflict.id,
        "state": conflict.state,
        "selected_value": conflict.selected_value,
        "historical_values_preserved": True,
    }
