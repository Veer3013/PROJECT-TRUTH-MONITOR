from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.domain import SourceAccessPolicy
from app.services.source_policy import SourcePolicyError, validate_policy_for_mode

from .base import CollectorPolicyError
from .hrera_gurugram import HRERAGurugramCollector


class DeveloperPageCollector(HRERAGurugramCollector):
    """Optional developer-site adapter enabled only by an explicit domain policy."""

    collector_name = "developer_page"
    OFFICIAL_DOMAINS: tuple[str, ...] = ()

    def can_handle(self, source: Any) -> bool:
        url = self._url_from_source(source)
        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
        policy = getattr(source, "policy", None)
        return bool(
            hostname
            and policy
            and any(hostname == domain or hostname.endswith(f".{domain}") for domain in policy.allowed_domains)
        )

    def validate_policy(self, source_policy: SourceAccessPolicy) -> None:
        try:
            validate_policy_for_mode(source_policy, "httpx")
        except SourcePolicyError as exc:
            raise CollectorPolicyError(exc.code, str(exc)) from exc
        if source_policy.source_type not in {"developer_page", "official_developer"}:
            raise CollectorPolicyError(
                "wrong_source_type",
                "A developer page requires an explicit official-developer source policy.",
            )

