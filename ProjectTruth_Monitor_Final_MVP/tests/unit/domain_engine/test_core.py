from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.collectors import FixtureCollector
from app.domain import (
    AccessCompleteness,
    AccessStatus,
    FetchResult,
    SourceAccessPolicy,
    SourceClass,
)
from app.domain.signals import SignalRuleCode
from app.parsers import FixtureHTMLParser
from app.services.conflicts import FactObservation, detect_source_conflicts
from app.services.diff import DeterministicDiffEngine, detect_changes
from app.services.evidence import EvidenceBuilder
from app.services.identity import IdentityMatchKind, ProjectIdentity, resolve_project_identity
from app.services.reports import escape_csv_formula, export_csv
from app.services.signals import generate_signals
from app.services.snapshots import InMemorySnapshotRepository, SnapshotService
from app.services.source_policy import UnsafeURLError, validate_public_url

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "html"


def _parsed_snapshot(filename: str, snapshot_id: str) -> dict[str, object]:
    content = (FIXTURES / filename).read_bytes()
    fetched = FetchResult(
        requested_url=f"fixture://{filename}",
        final_url=f"fixture://{filename}",
        source_id="source-1",
        http_status=200,
        fetched_at=datetime.now(UTC),
        content_type="text/html",
        access_status=AccessStatus.SUCCESS,
        parser_eligible=True,
        completeness=AccessCompleteness.COMPLETE,
        content=content,
    )
    parsed = FixtureHTMLParser().parse(fetched)
    return {
        "id": snapshot_id,
        "project_id": "project-1",
        "source_id": "source-1",
        "parser_name": parsed.parser_name,
        "parser_version": parsed.parser_version,
        "validation_status": parsed.validation_status.value,
        "access_completeness": "complete",
        "fields": parsed.fields,
        "content_hash": fetched.content_hash,
        "fetched_at": fetched.fetched_at,
    }


def test_case_a_extension_is_exactly_275_days_and_evidence_linked() -> None:
    old = _parsed_snapshot("case_a_completion_old.html", "snapshot-old")
    new = _parsed_snapshot("case_a_completion_extended.html", "snapshot-new")

    changes = detect_changes(old, new)
    extension = next(change for change in changes if change.change_type.value == "date_extended")

    assert extension.field_key == "revised_completion_date"
    assert extension.old_canonical_value == date(2027, 6, 30)
    assert extension.new_canonical_value == date(2028, 3, 31)
    assert extension.magnitude_days == 275
    assert extension.requires_review is True
    assert extension.old_snapshot_id == "snapshot-old"
    assert extension.new_snapshot_id == "snapshot-new"

    card = EvidenceBuilder().build(
        extension,
        old_snapshot=old,
        new_snapshot=new,
        source_class=SourceClass.FIXTURE,
    )
    assert card.old_snapshot_id == "snapshot-old"
    assert card.new_snapshot_id == "snapshot-new"
    assert "+275 days" in card.neutral_description

    codes = {signal.rule_code for signal in generate_signals(changes)}
    assert SignalRuleCode.COMPLETION_DATE_EXTENDED in codes
    assert SignalRuleCode.EXACT_LINKED_NOTICE_ADDED in codes


def test_reversal_matches_earlier_historical_value() -> None:
    first = _parsed_snapshot("value_reverted_1.html", "snapshot-1")
    second = _parsed_snapshot("value_reverted_2.html", "snapshot-2")
    third = _parsed_snapshot("value_reverted_3.html", "snapshot-3")

    changes = detect_changes(second, third, history=(first,))

    assert any(change.change_type.value == "value_reverted" for change in changes)
    assert SignalRuleCode.VALUE_REVERSED in {
        signal.rule_code for signal in generate_signals(changes)
    }


def test_parser_major_version_change_suppresses_factual_changes() -> None:
    old = {
        "id": "old",
        "project_id": "project-1",
        "parser_name": "fixture_html",
        "parser_version": "1.2.0",
        "structured_facts": {"unit_count": 10},
        "access_completeness": "complete",
        "validation_status": "valid",
    }
    new = {
        **old,
        "id": "new",
        "parser_version": "2.0.0",
        "structured_facts": {"unit_count": 20},
    }

    changes = detect_changes(old, new)

    assert len(changes) == 1
    assert changes[0].change_type.value == "parser_version_incompatible"
    assert changes[0].field_key == "__parser__"


def test_exact_registration_wins_and_similar_name_never_auto_merges() -> None:
    existing = (
        ProjectIdentity("one", "HRERA-DEMO-601", project_name="Sunrise Avenue", developer_name="A", city="Gurugram"),
        ProjectIdentity("two", "HRERA-DEMO-602", project_name="Sun Rise Avenue", developer_name="B", city="Gurugram"),
    )
    exact = resolve_project_identity(ProjectIdentity("new", " hrera-demo-602 "), existing)
    probable = resolve_project_identity(
        ProjectIdentity("new", project_name="Sunrise Ave", developer_name="A", city="Gurugram"),
        existing,
        alias_threshold=0.70,
    )

    assert exact.match_kind == IdentityMatchKind.EXACT_REGISTRATION
    assert exact.project_id == "two"
    assert exact.may_auto_link is True
    assert probable.match_kind == IdentityMatchKind.PROBABLE_ALIAS
    assert probable.project_id is None
    assert probable.requires_review is True


def test_different_registration_is_not_merged_by_same_name_developer_location() -> None:
    existing = (
        ProjectIdentity(
            "phase-one",
            "HRERA-DEMO-301",
            project_name="Orion Greens",
            developer_name="Orion Demo Estates",
            city="Gurugram",
            locality_or_sector="Sector 79",
        ),
    )
    incoming = ProjectIdentity(
        "incoming",
        "HRERA-DEMO-303",
        project_name="Orion Greens",
        developer_name="Orion Demo Estates",
        city="Gurugram",
        locality_or_sector="Sector 79",
    )

    resolution = resolve_project_identity(incoming, existing)

    assert resolution.project_id is None
    assert resolution.requires_review is True
    assert resolution.match_kind == IdentityMatchKind.PROBABLE_ALIAS


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/private",
    ),
)
def test_ssrf_blocks_local_private_and_metadata_addresses(url: str) -> None:
    host = url.split("//", 1)[1].split("/", 1)[0].strip("[]")
    with pytest.raises(UnsafeURLError):
        validate_public_url(
            url,
            allowed_domains=(host,),
            allowed_schemes=("http",),
            resolve_dns=False,
        )


def test_ssrf_blocks_approved_hostname_if_dns_resolves_private() -> None:
    with pytest.raises(UnsafeURLError):
        validate_public_url(
            "https://approved.example/project",
            allowed_domains=("approved.example",),
            resolver=lambda host, port: ("10.0.0.8",),
        )


def test_csv_formula_values_are_neutralized() -> None:
    assert escape_csv_formula("=1+1") == "'=1+1"
    assert escape_csv_formula(" +SUM(A1:A2)") == "' +SUM(A1:A2)"
    payload = export_csv([{"project_name": "=HYPERLINK(\"https://bad.invalid\")"}])
    assert b"'=HYPERLINK" in payload


def test_fixture_collector_requires_policy_and_returns_immutable_hash() -> None:
    policy = SourceAccessPolicy(
        source_name="fixture",
        source_type="fixture",
        base_url="fixture://",
        allowed_domains=(),
        enabled=True,
        permitted_access_modes=frozenset({"fixture"}),
        robots_review_status="not_applicable",
        terms_review_status="not_applicable",
    )
    collector = FixtureCollector({"clean": FIXTURES / "clean_project_page.html"}, fixture_root=FIXTURES)
    from app.domain import FetchRequestContext

    result = asyncio.run(
        collector.fetch(
            FetchRequestContext(
                source_id="source-1",
                requested_url="fixture://clean",
                fixture_key="clean",
                policy=policy,
            )
        )
    )

    assert result.is_successful
    assert result.parser_eligible
    assert result.content_hash and len(result.content_hash) == 64


def test_partial_snapshot_degrades_monitoring_without_false_removal() -> None:
    old = {
        "id": "old",
        "project_id": "project-1",
        "parser_name": "fixture_html",
        "parser_version": "1.0.0",
        "structured_facts": {"project_name": "Cedar", "land_or_project_area": "8.5"},
        "access_completeness": "complete",
        "validation_status": "valid",
    }
    partial = {
        **old,
        "id": "partial",
        "structured_facts": {"project_name": "Cedar"},
        "access_completeness": "partial",
        "validation_status": "partial",
    }

    changes = detect_changes(old, partial)

    assert [change.change_type.value for change in changes] == ["source_degraded"]
    assert all(change.field_key != "land_or_project_area" for change in changes)


def test_snapshot_service_reuses_content_across_no_change_runs() -> None:
    repository = InMemorySnapshotRepository()
    service = SnapshotService(repository)
    policy = SourceAccessPolicy(
        source_name="fixture",
        source_type="fixture",
        base_url="fixture://",
        allowed_domains=(),
        permitted_access_modes=frozenset({"fixture"}),
    )
    content = (FIXTURES / "clean_project_page.html").read_bytes()
    fetch = FetchResult(
        "fixture://clean",
        "fixture://clean",
        "source-1",
        200,
        datetime.now(UTC),
        "text/html",
        AccessStatus.SUCCESS,
        True,
        AccessCompleteness.COMPLETE,
        content=content,
    )
    parsed = FixtureHTMLParser().parse(fetch)

    first = service.capture(
        project_id="project-1",
        monitoring_run_id="run-1",
        fetch_result=fetch,
        parsed=parsed,
        policy=policy,
    )
    second = service.capture(
        project_id="project-1",
        monitoring_run_id="run-2",
        fetch_result=fetch,
        parsed=parsed,
        policy=policy,
    )

    assert first.snapshot is second.snapshot
    assert second.duplicate is True
    assert second.reason == "content_not_modified"
    assert len(repository.history("project-1", "source-1")) == 1
    assert first.snapshot and first.snapshot.raw_content is None


def test_case_b_preserves_three_official_vs_marketing_conflicts() -> None:
    observations = (
        FactObservation("project-b", "building_count", 5, "official", SourceClass.OFFICIAL_REGULATOR, "official-snapshot"),
        FactObservation("project-b", "building_count", 6, "brochure", SourceClass.MARKETING_BROCHURE, "brochure-snapshot"),
        FactObservation("project-b", "unit_count", 1080, "official", SourceClass.OFFICIAL_REGULATOR, "official-snapshot"),
        FactObservation("project-b", "unit_count", 1200, "brochure", SourceClass.MARKETING_BROCHURE, "brochure-snapshot"),
        FactObservation("project-b", "proposed_completion_date", date(2028, 6, 30), "official", SourceClass.OFFICIAL_REGULATOR, "official-snapshot"),
        FactObservation("project-b", "proposed_completion_date", date(2027, 12, 31), "brochure", SourceClass.MARKETING_BROCHURE, "brochure-snapshot"),
    )

    conflicts = detect_source_conflicts(observations)
    changes = DeterministicDiffEngine().source_conflicts(observations)

    assert len(conflicts) == 3
    assert all(conflict.reason_code == "official_vs_marketing" for conflict in conflicts)
    assert len(changes) == 3
    assert all(change.change_type.value == "source_conflict" for change in changes)
    assert all(change.requires_review for change in changes)
    assert {
        signal.rule_code for signal in generate_signals(changes)
    } == {SignalRuleCode.OFFICIAL_VS_MARKETING_CONFLICT}
