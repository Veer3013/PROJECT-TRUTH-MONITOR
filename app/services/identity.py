from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

from .normalization import comparison_text, registration_identity_key


class IdentityMatchKind(StrEnum):
    EXACT_REGISTRATION = "exact_registration"
    REGISTRATION_AND_SOURCE = "registration_and_source"
    NAME_DEVELOPER_LOCATION = "name_developer_location"
    PROBABLE_ALIAS = "probable_alias"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    registration_number: str | None = None
    official_source: str | None = None
    project_name: str | None = None
    developer_name: str | None = None
    city: str | None = None
    locality_or_sector: str | None = None
    provisional: bool = False


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    match_kind: IdentityMatchKind
    project_id: str | None
    candidate_project_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    requires_review: bool = False
    reason_codes: tuple[str, ...] = ()

    @property
    def may_auto_link(self) -> bool:
        return self.match_kind in {
            IdentityMatchKind.EXACT_REGISTRATION,
            IdentityMatchKind.REGISTRATION_AND_SOURCE,
            IdentityMatchKind.NAME_DEVELOPER_LOCATION,
        } and not self.requires_review and self.project_id is not None


def _read(candidate: Any, field: str) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(field)
    return getattr(candidate, field, None)


def _as_identity(candidate: Any) -> ProjectIdentity:
    if isinstance(candidate, ProjectIdentity):
        return candidate
    return ProjectIdentity(
        project_id=str(_read(candidate, "project_id") or _read(candidate, "id")),
        registration_number=_read(candidate, "registration_number"),
        official_source=_read(candidate, "official_source") or _read(candidate, "source_id"),
        project_name=_read(candidate, "project_name") or _read(candidate, "name"),
        developer_name=_read(candidate, "developer_name") or _read(candidate, "developer"),
        city=_read(candidate, "city"),
        locality_or_sector=_read(candidate, "locality_or_sector") or _read(candidate, "locality"),
        provisional=bool(_read(candidate, "provisional")),
    )


def _unique_project_ids(candidates: Iterable[ProjectIdentity]) -> tuple[str, ...]:
    return tuple(sorted({candidate.project_id for candidate in candidates}))


def resolve_project_identity(
    incoming: ProjectIdentity | Mapping[str, Any] | Any,
    existing: Iterable[ProjectIdentity | Mapping[str, Any] | Any],
    *,
    alias_threshold: float = 0.86,
) -> IdentityResolution:
    """Resolve identity conservatively; probable aliases are never auto-merged."""

    item = _as_identity(incoming)
    candidates = tuple(_as_identity(candidate) for candidate in existing)
    registration_key = registration_identity_key(item.registration_number)
    if registration_key:
        exact = tuple(
            candidate
            for candidate in candidates
            if registration_identity_key(candidate.registration_number) == registration_key
        )
        exact_ids = _unique_project_ids(exact)
        if len(exact_ids) == 1:
            return IdentityResolution(
                IdentityMatchKind.EXACT_REGISTRATION,
                exact_ids[0],
                exact_ids,
                1.0,
                False,
                ("exact_registration_number",),
            )
        if len(exact_ids) > 1 and item.official_source:
            source_exact = tuple(
                candidate
                for candidate in exact
                if comparison_text(candidate.official_source) == comparison_text(item.official_source)
            )
            source_ids = _unique_project_ids(source_exact)
            if len(source_ids) == 1:
                return IdentityResolution(
                    IdentityMatchKind.REGISTRATION_AND_SOURCE,
                    source_ids[0],
                    exact_ids,
                    1.0,
                    False,
                    ("exact_registration_number", "exact_official_source"),
                )
        if exact_ids:
            return IdentityResolution(
                IdentityMatchKind.AMBIGUOUS,
                None,
                exact_ids,
                1.0,
                True,
                ("duplicate_registration_candidates",),
            )

    name_key = comparison_text(item.project_name)
    developer_key = comparison_text(item.developer_name)
    location_key = comparison_text(item.locality_or_sector or item.city)
    # A known but different registration number represents a distinct project or
    # phase until reviewed. Composite-name matching must never override it.
    if not registration_key and name_key and developer_key and location_key:
        exact_composite = tuple(
            candidate
            for candidate in candidates
            if comparison_text(candidate.project_name) == name_key
            and comparison_text(candidate.developer_name) == developer_key
            and comparison_text(candidate.locality_or_sector or candidate.city) == location_key
        )
        composite_ids = _unique_project_ids(exact_composite)
        if len(composite_ids) == 1:
            return IdentityResolution(
                IdentityMatchKind.NAME_DEVELOPER_LOCATION,
                composite_ids[0],
                composite_ids,
                0.95,
                False,
                ("exact_name_developer_location",),
            )
        if len(composite_ids) > 1:
            return IdentityResolution(
                IdentityMatchKind.AMBIGUOUS,
                None,
                composite_ids,
                0.95,
                True,
                ("ambiguous_name_developer_location",),
            )

    probable: list[tuple[float, ProjectIdentity]] = []
    if name_key:
        for candidate in candidates:
            candidate_name = comparison_text(candidate.project_name)
            if not candidate_name:
                continue
            name_score = SequenceMatcher(None, name_key, candidate_name).ratio()
            developer_score = SequenceMatcher(
                None, developer_key, comparison_text(candidate.developer_name)
            ).ratio() if developer_key else 0.5
            location_score = SequenceMatcher(
                None, location_key, comparison_text(candidate.locality_or_sector or candidate.city)
            ).ratio() if location_key else 0.5
            score = (0.60 * name_score) + (0.25 * developer_score) + (0.15 * location_score)
            if score >= alias_threshold:
                probable.append((score, candidate))
    if probable:
        probable.sort(key=lambda pair: (-pair[0], pair[1].project_id))
        ids = _unique_project_ids(candidate for _, candidate in probable)
        return IdentityResolution(
            IdentityMatchKind.PROBABLE_ALIAS,
            None,
            ids,
            probable[0][0],
            True,
            ("probable_alias_requires_review",),
        )
    return IdentityResolution(
        IdentityMatchKind.NO_MATCH,
        None,
        (),
        0.0,
        bool(item.provisional or not registration_key),
        ("new_project",) if registration_key else ("unverified_identity",),
    )


def exact_registration_match(left: Any, right: Any) -> bool:
    left_key = registration_identity_key(left)
    return left_key is not None and left_key == registration_identity_key(right)


def canonical_project_key(identity: ProjectIdentity | Mapping[str, Any] | Any) -> str | None:
    item = _as_identity(identity)
    registration_key = registration_identity_key(item.registration_number)
    if registration_key:
        return f"rera:{registration_key}"
    name = comparison_text(item.project_name)
    developer = comparison_text(item.developer_name)
    location = comparison_text(item.locality_or_sector or item.city)
    if name and developer and location:
        return f"ndl:{name}|{developer}|{location}"
    return None


resolve_identity = resolve_project_identity
