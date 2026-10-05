from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.domain.enums import SourceClass
from app.domain.source import SourceAccessPolicy


class SourcePolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UnsafeURLError(SourcePolicyError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    normalized_url: str
    hostname: str
    port: int
    resolved_addresses: tuple[str, ...]


Resolver = Callable[[str, int], Iterable[str]]


def _default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    addresses: set[str] = set()
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError("dns_resolution_failed", "The source hostname could not be resolved.") from exc
    for record in records:
        addresses.add(record[4][0].split("%", 1)[0])
    if not addresses:
        raise UnsafeURLError("dns_resolution_failed", "The source hostname did not resolve to an address.")
    return tuple(sorted(addresses))


def _is_forbidden_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _normalize_hostname(hostname: str) -> str:
    try:
        return hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeURLError("invalid_hostname", "The source hostname is invalid.") from exc


def domain_is_allowed(hostname: str, allowed_domains: Sequence[str]) -> bool:
    normalized = _normalize_hostname(hostname)
    for configured in allowed_domains:
        allowed = _normalize_hostname(configured.lstrip("*."))
        if normalized == allowed or normalized.endswith(f".{allowed}"):
            return True
    return False


def validate_public_url(
    url: str,
    *,
    allowed_domains: Sequence[str],
    allowed_schemes: Iterable[str] = ("https",),
    resolver: Resolver | None = None,
    resolve_dns: bool = True,
) -> ValidatedURL:
    """Validate an outbound URL against policy and public-address SSRF rules.

    This function must be called again for every redirect target. A domain allowlist
    alone is not sufficient because DNS may resolve an approved hostname to a local
    or metadata-service address.
    """

    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("invalid_url", "A non-empty absolute source URL is required.")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("invalid_url", "The source URL is invalid.") from exc
    allowed_scheme_set = {item.lower() for item in allowed_schemes}
    if parsed.scheme.lower() not in allowed_scheme_set:
        raise UnsafeURLError("unsupported_scheme", "The source URL scheme is not permitted.")
    if not parsed.hostname or not parsed.netloc:
        raise UnsafeURLError("invalid_url", "An absolute source URL with a hostname is required.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("embedded_credentials", "Credentials may not be embedded in a source URL.")
    hostname = _normalize_hostname(parsed.hostname)
    if hostname in {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"}:
        raise UnsafeURLError("private_address", "Local and metadata-service addresses are not permitted.")
    if not domain_is_allowed(hostname, allowed_domains):
        raise UnsafeURLError("domain_not_allowed", "The source domain is not approved by policy.")
    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (str(literal),)
    elif resolve_dns:
        addresses = tuple((resolver or _default_resolver)(hostname, effective_port))
    else:
        addresses = ()
    if any(_is_forbidden_address(address) for address in addresses):
        raise UnsafeURLError("private_address", "Private, local and metadata-service addresses are not permitted.")
    normalized_parts = SplitResult(
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        parsed.query,
        "",  # Fragments are client-side and never sent to the source.
    )
    return ValidatedURL(urlunsplit(normalized_parts), hostname, effective_port, tuple(addresses))


def validate_policy_for_mode(policy: SourceAccessPolicy, mode: str) -> None:
    if not policy.enabled:
        raise SourcePolicyError("policy_disabled", "The source policy is disabled.")
    if mode not in policy.permitted_access_modes:
        raise SourcePolicyError("access_mode_not_permitted", f"The policy does not permit {mode} access.")
    if mode in {"httpx", "playwright"}:
        if policy.robots_review_status.lower() not in {"approved", "reviewed", "not_applicable"}:
            raise SourcePolicyError("robots_review_required", "Robots access must be reviewed before collection.")
        if policy.terms_review_status.lower() not in {"approved", "reviewed", "not_applicable"}:
            raise SourcePolicyError("terms_review_required", "Source terms must be reviewed before collection.")
        if not policy.allowed_domains:
            raise SourcePolicyError("domain_policy_required", "A network source requires approved domains.")
    if mode == "playwright" and not policy.playwright_allowed:
        raise SourcePolicyError("playwright_not_permitted", "Browser automation is not allowed by source policy.")


class SourcePolicyRegistry:
    """Small deterministic registry suitable for configuration or database adapters."""

    def __init__(self, policies: Iterable[SourceAccessPolicy] = ()) -> None:
        self._policies: dict[str, SourceAccessPolicy] = {policy.source_name: policy for policy in policies}

    def register(self, policy: SourceAccessPolicy) -> None:
        self._policies[policy.source_name] = policy

    def get(self, source_name: str) -> SourceAccessPolicy:
        try:
            return self._policies[source_name]
        except KeyError as exc:
            raise SourcePolicyError("unknown_source", "No approved access policy exists for this source.") from exc

    def find_for_url(self, url: str, *, resolve_dns: bool = True) -> SourceAccessPolicy:
        matches: list[SourceAccessPolicy] = []
        parsed = urlsplit(url)
        if not parsed.hostname:
            raise SourcePolicyError("invalid_url", "An absolute source URL is required.")
        for policy in self._policies.values():
            if policy.enabled and domain_is_allowed(parsed.hostname, policy.allowed_domains):
                matches.append(policy)
        if len(matches) != 1:
            code = "unknown_source" if not matches else "ambiguous_source_policy"
            raise SourcePolicyError(code, "Exactly one approved source policy must match the URL.")
        validate_public_url(
            url,
            allowed_domains=matches[0].allowed_domains,
            allowed_schemes=matches[0].allowed_schemes,
            resolve_dns=resolve_dns,
        )
        return matches[0]

    def enabled(self) -> tuple[SourceAccessPolicy, ...]:
        return tuple(policy for policy in self._policies.values() if policy.enabled)


validate_source_url = validate_public_url


def source_policy_from_model(
    source: object,
    policy: object,
    *,
    max_response_bytes: int = 8 * 1024 * 1024,
    request_timeout_seconds: float = 15.0,
    max_redirects: int = 3,
) -> SourceAccessPolicy:
    """Translate persisted source/policy rows into the collector's immutable policy."""

    source_type = str(source.source_type)
    configured_modes = frozenset(str(item) for item in (getattr(policy, "permitted_access_modes", ()) or ()))
    domain = str(getattr(source, "domain", "") or "").lower().strip()
    base_url = str(getattr(source, "base_url", "") or "")
    scheme = urlsplit(base_url).scheme.lower()
    class_value = str(getattr(source, "source_class", "unknown"))
    class_aliases = {
        "user_authorized_document": SourceClass.USER_AUTHORIZED,
        "uploaded_document": SourceClass.USER_AUTHORIZED,
    }
    try:
        source_class = SourceClass(class_value)
    except ValueError:
        source_class = class_aliases.get(class_value, SourceClass.UNKNOWN)
    requests = max(1, int(getattr(policy, "rate_limit_requests", 1) or 1))
    period = float(getattr(policy, "rate_limit_period_seconds", 3.0) or 3.0)
    is_network = bool(configured_modes & {"httpx", "playwright"})
    return SourceAccessPolicy(
        source_name=str(source.name),
        source_type=source_type,
        base_url=base_url,
        allowed_domains=(domain,) if domain and is_network else (),
        enabled=bool(getattr(source, "is_enabled", False) and getattr(policy, "is_enabled", False)),
        permitted_access_modes=configured_modes,
        rate_limit_seconds=period / requests,
        robots_review_status=str(getattr(policy, "robots_review_status", "not_reviewed")),
        terms_review_status=str(getattr(policy, "terms_review_status", "not_reviewed")),
        evidence_retention_policy=f"{int(getattr(policy, 'evidence_retention_days', 90))}d",
        may_store_raw_content=bool(getattr(policy, "raw_content_may_be_stored", False)),
        playwright_allowed=bool(getattr(policy, "playwright_allowed", False)),
        ocr_allowed=bool(getattr(policy, "ocr_allowed", False)),
        llm_processing_allowed=bool(getattr(policy, "llm_allowed", False)),
        policy_version=str(policy.policy_version),
        reviewer=getattr(policy, "reviewer_user_id", None),
        reviewed_at=getattr(policy, "reviewed_at", None),
        max_response_bytes=max_response_bytes,
        request_timeout_seconds=request_timeout_seconds,
        max_redirects=max_redirects,
        allowed_schemes=frozenset({scheme}) if is_network and scheme in {"http", "https"} else frozenset({"https"}),
        source_class=source_class,
    )


def load_source_policies(path: Path) -> tuple[SourceAccessPolicy, ...]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load source policy configuration") from exc
    with path.open("r", encoding="utf-8") as stream:
        payload = yaml.safe_load(stream) or {}
    raw_policies = payload.get("policies")
    if not isinstance(raw_policies, list):
        raise SourcePolicyError("invalid_policy_file", "Source policy YAML must contain a policies list.")
    policies: list[SourceAccessPolicy] = []
    for raw in raw_policies:
        if not isinstance(raw, dict):
            raise SourcePolicyError("invalid_policy_file", "Each source policy must be an object.")
        modes = frozenset(str(value) for value in raw.get("permitted_access_modes", ()))
        base_url = str(raw.get("base_url", ""))
        scheme = urlsplit(base_url).scheme.lower()
        source_type = str(raw.get("source_type", "unknown"))
        source_class = (
            SourceClass.FIXTURE
            if "fixture" in modes
            else SourceClass.OFFICIAL_REGULATOR
            if source_type in {"official_regulator", "hrera_gurugram"}
            else SourceClass.OFFICIAL_DEVELOPER
            if source_type in {"official_developer", "developer_page"}
            else SourceClass.UNKNOWN
        )
        reviewed_at = raw.get("reviewed_at")
        if isinstance(reviewed_at, str):
            reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        policies.append(
            SourceAccessPolicy(
                source_name=str(raw.get("source_name", raw.get("id", "unknown"))),
                source_type=source_type,
                base_url=base_url,
                allowed_domains=tuple(str(item) for item in raw.get("allowed_domains", ())),
                enabled=bool(raw.get("enabled", False)),
                permitted_access_modes=modes,
                rate_limit_seconds=float(raw.get("rate_limit_seconds", 3.0)),
                robots_review_status=str(raw.get("robots_review_status", "pending")),
                terms_review_status=str(raw.get("terms_review_status", "pending")),
                evidence_retention_policy=str(raw.get("evidence_retention_policy", "structured_only")),
                may_store_raw_content=bool(raw.get("may_store_raw_content", False)),
                playwright_allowed=bool(raw.get("playwright_allowed", False)),
                ocr_allowed=bool(raw.get("ocr_allowed", False)),
                llm_processing_allowed=bool(raw.get("llm_processing_allowed", False)),
                policy_version=str(raw.get("policy_version", "1.0")),
                reviewer=raw.get("reviewer"),
                reviewed_at=reviewed_at,
                allowed_schemes=frozenset({scheme}) if scheme else frozenset({"https"}),
                source_class=source_class,
            )
        )
    return tuple(policies)
