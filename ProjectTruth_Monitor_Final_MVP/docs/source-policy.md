# Source access and evidence policy

## Purpose

A source adapter may run only after a current source access policy authorizes the source, domain, access mode, rate, and retention behavior. This registry is both a technical gate and a record of the review that allowed collection.

The repository default is [source_access.yml](../app/policies/source_access.yml). It contains:

- An enabled synthetic fixture policy for deterministic local tests and demonstrations.
- A disabled HRERA Gurugram network policy with robots and terms review still required.

`ENABLE_LIVE_HRERA=false` remains the safe default. The process flag and an enabled reviewed policy are both required. Changing the environment value alone does not bypass a disabled or unreviewed source policy.

The repository includes a fail-closed HRERA `httpx` adapter, but it has not been qualified against the current public site and the test suite never calls it. Frozen synthetic fixtures are the supported demo and regression path.

## Required policy fields

Each policy records:

- Source name, type, class, base URL, and allowed domains.
- Enabled state and permitted access modes.
- Request rate, timeout, maximum response size, redirect count, and schemes.
- Robots and terms review states.
- Evidence retention policy and whether raw content may be stored.
- Whether Playwright, OCR, or LLM processing is allowed.
- Policy version, reviewer, review timestamp, and notes.

An update creates a new policy version. Historical snapshots retain the policy version used for collection.

## Approval checklist for a network source

An administrator must record all of the following before enablement:

1. The exact official domain and any redirect domains.
2. A current robots review.
3. A current terms and authorization review.
4. The permitted access surface and method.
5. A conservative rate and response limit.
6. Whether conditional requests are available.
7. The minimum evidence that may be retained.
8. Whether raw content, browser automation, OCR, or LLM processing is permitted.
9. The reviewer and timestamp.
10. A re-review trigger, such as a policy, domain, or layout change.

## URL and SSRF rules

For every initial URL and redirect:

- Require an approved absolute URL and permitted scheme, normally HTTPS.
- Reject embedded credentials.
- Match exactly one enabled source policy.
- Resolve the hostname and reject private, loopback, link-local, multicast, reserved, unspecified, and metadata-service addresses.
- Reject localhost and metadata hostnames explicitly.
- Enforce timeout, redirect, and response-size limits.
- Revalidate every redirect rather than trusting the first hostname.

User input cannot add a domain to the allowlist. A new domain needs an admin-reviewed policy version.

## Collector behavior

Collectors use `httpx` by default. The policy model can record whether browser automation is allowed, but this MVP does not ship a Playwright runtime path. Requests use conservative spacing, conditional headers where practical, bounded retries with jitter for transient failures, and a per-source circuit breaker.

The following are never permitted:

- CAPTCHA solving or challenge bypass.
- Authentication or paywall bypass.
- Ignoring robots, terms, or rate limits.
- Unsafe redirect following.
- Collection from an unknown domain.
- Claiming a live check succeeded when access or parsing was incomplete.

## Failure semantics

| Condition | Safe message and data behavior |
| --- | --- |
| Unsupported URL | "This source is not supported yet. Add a supported official URL or upload an authorized document." No collection occurs. |
| CAPTCHA, login, or restriction | "Automatic access is blocked. No data was changed. You may upload an authorized copy." Previous verified facts remain. |
| Rate limit or timeout | "The source could not be checked yet. Last verified data remains visible with its date." The run is degraded, not no-change. |
| Layout or parser change | "The source format changed. This check needs maintenance; no factual alert was generated." Parser validation fails closed. |
| Partial snapshot | "Only part of the source was readable. Some fields could not be verified." Known fields remain qualified as partial. |
| Value removed | "A previously displayed value is no longer present. This may be a source change and requires review." History is preserved. |

Unsupported, authentication, CAPTCHA, and policy failures are not retried until source access or policy changes. Transient network failures may retry up to the configured limit with exponential backoff and jitter.

## Evidence retention

Raw content is stored only when the active policy allows it. Otherwise the application retains the content hash, structured facts, parser and policy versions, timestamps, permitted locator, and minimum allowed excerpt. Uploaded evidence is stored outside the static web root and served only through authenticated tenant-aware routes.

Evidence links never reveal a local filesystem path. Deleting an authorized upload or expired report invalidates its old access route. A minimal deletion tombstone may remain only where policy requires it.

Source-policy retention controls what may be captured; it is not an automatic purge scheduler. Per-organization retention values are stored and audited, report downloads enforce their recorded `expires_at`, and explicit deletion removes artifacts. Automated age-based upload/report cleanup is a documented post-MVP requirement.

An `ocr_allowed` or `llm_processing_allowed` policy value is permission, not provider activation. The current application wires disabled OCR and LLM implementations only. A concrete provider requires separate code, credentials, privacy review, tests, and a policy that permits that processing.

## Fixture policy

All files under `tests/fixtures` are synthetic. Their `.invalid` URLs cannot represent a live public service. Fixture collection may retain raw content because the project controls this synthetic content. Tests and the four-case demo never depend on live government availability.
