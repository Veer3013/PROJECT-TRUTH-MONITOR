# API overview

The JSON API is versioned under `/api/v1`. Interactive OpenAPI documentation is available at `/docs` in local development.

## Authentication and request rules

- Login returns a signed session token, sets an HTTP-only same-site cookie, and returns the CSRF token needed by cookie-authenticated mutations.
- Authenticated API clients may send the token as `Authorization: Bearer <token>`.
- Cookie-authenticated state changes send `X-CSRF-Token` or a `csrf_token` form field where form data is accepted.
- `X-Organization-ID` may select among the authenticated user's active memberships. It never grants membership.
- Every organization-owned lookup includes the authenticated `organization_id`.
- State-changing endpoints validate input, enforce a minimum role, record the authenticated actor, return safe structured errors, and use idempotency where a retry could duplicate work.
- JSON is the normal mutation format. Login accepts JSON or form encoding; document upload accepts multipart form data.

Typical safe error shape:

```json
{
  "error": {
    "code": "source_required",
    "message": "Add an approved source first.",
    "request_id": "..."
  }
}
```

Validation errors use the same `error` envelope and add a `fields` array. FastAPI route code may raise an internal `HTTPException.detail`, but the application exception handler does not expose that internal envelope to API clients.

Normal users do not receive stack traces, SQL details, storage paths, or cross-tenant resource metadata.

## Endpoint map

Roles show the minimum expected role. Admin and reviewer inherit lower-role capabilities where the permission model allows it.

### Authentication

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/login` | Public, rate limited | Validate credentials and create a session. |
| POST | `/api/v1/auth/logout` | Authenticated + CSRF | Clear the session and audit logout. |
| GET | `/api/v1/auth/me` | Authenticated | Return the current user and membership. |

### Organization and members

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/organizations/current` | Viewer | Current tenant-safe organization. |
| GET | `/api/v1/organizations/current/members` | Viewer | Members in the current organization. |
| POST | `/api/v1/organizations/current/members` | Admin + CSRF | Create a user if needed and add membership. |
| PATCH | `/api/v1/organizations/current/members/{membership_id}` | Admin + CSRF | Change role or active state; the last active admin is protected. |

### Watchlists and CSV import

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/watchlists` | Analyst + CSRF | Create a watchlist. |
| GET | `/api/v1/watchlists` | Viewer | List current watchlists. |
| GET | `/api/v1/watchlists/{id}` | Viewer | Get one watchlist. |
| PATCH | `/api/v1/watchlists/{id}` | Analyst + CSRF | Rename or change cadence/preferences. |
| DELETE | `/api/v1/watchlists/{id}` | Analyst + CSRF | Idempotently deactivate a watchlist and its items. |
| POST | `/api/v1/watchlists/{id}/items` | Analyst + CSRF | Resolve or create a project and add it without duplication. |
| DELETE | `/api/v1/watchlists/{id}/items/{item_id}` | Analyst + CSRF | Idempotently remove an item. |
| POST | `/api/v1/imports/projects/preview` | Analyst + CSRF | Validate CSV text and return a signed 30-minute preview token. |
| POST | `/api/v1/imports/projects/commit` | Analyst + CSRF | Commit a valid preview using an idempotency key. |

Preview accepts `{ "csv_text": "..." }`. Commit accepts `watchlist_id`, `preview_token`, and an `idempotency_key` of at least eight characters.

### Projects, phases, and intelligence

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/projects` | Analyst + CSRF | Resolve exact identity or create a provisional project. |
| GET | `/api/v1/projects/{id}` | Viewer | Canonical project profile. |
| GET | `/api/v1/projects/{id}/passport` | Viewer | Current Project Truth Passport. |
| GET | `/api/v1/projects/{id}/timeline` | Viewer | Evidence and audit-linked chronology. |
| GET | `/api/v1/projects/{id}/changes` | Viewer | Field-level Change Ledger. |
| GET | `/api/v1/projects/{id}/signals` | Viewer | Transparent versioned rule signals. |
| GET | `/api/v1/projects/{id}/signals/{signal_id}/impact-actions` | Viewer | Deterministic role-specific review suggestions and their reason. |
| GET | `/api/v1/projects/{id}/conflicts` | Viewer | Preserved source disagreements. |
| GET | `/api/v1/projects/{id}/phase-map` | Viewer | Parent and phase relationships. |

### Sources and monitoring

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/projects/{id}/sources` | Analyst + CSRF | Attach a fixture or policy-reviewed source. |
| GET | `/api/v1/sources/{id}` | Viewer | Source metadata. |
| GET | `/api/v1/source-policies` | Viewer | Current tenant policy versions. |
| POST | `/api/v1/source-policies` | Admin + CSRF | Create a reviewed policy version. |
| PATCH | `/api/v1/source-policies/{id}` | Admin + CSRF | Supersede a current policy. |
| POST | `/api/v1/watchlist-items/{id}/refresh` | Analyst + CSRF, rate limited | Run an allowed check with `idempotency_key` and optional fixture key. |
| GET | `/api/v1/monitoring-runs/{id}` | Viewer | Monitoring result and access completeness. |
| GET | `/api/v1/source-health` | Viewer | Fetch/parser rates, latency, failures, circuit, policy/parser version, and retries. |

Source failure never becomes a false no-change. Unsupported, blocked, CAPTCHA, and parser-invalid states use explicit categories.

### Documents

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/projects/{id}/documents` | Analyst + CSRF, rate limited | Multipart PDF upload with `authorization_attested=true`. |
| GET | `/api/v1/documents/{id}` | Viewer | Tenant-safe document metadata. |
| GET | `/api/v1/documents/{id}/download` | Viewer | Private no-store PDF download. |
| POST | `/api/v1/documents/{id}/versions` | Analyst + CSRF, rate limited | Upload a revised authorized PDF as a new immutable version. |
| GET | `/api/v1/documents/{id}/versions` | Viewer | List immutable versions newest first. |
| GET | `/api/v1/documents/{id}/versions/{version_id}/download` | Viewer | Download a tenant-owned historical version. |
| POST | `/api/v1/documents/{id}/extract` | Analyst + CSRF | Deterministic PDF text extraction and page-linked claims. |
| POST | `/api/v1/documents/{id}/compare` | Analyst + CSRF | Compare claims with the latest valid/partial official snapshot. |
| DELETE | `/api/v1/documents/{id}` | Analyst + CSRF | Delete the artifact and create an idempotent tombstone. |

Encrypted or corrupted PDFs return a safe error and create no official facts. If searchable text is unavailable, the current disabled OCR provider records manual review; setting an arbitrary `OCR_PROVIDER` value does not dynamically install a provider.

### Changes, evidence, alerts, and review

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/changes/{id}` | Viewer | One detected change. |
| GET | `/api/v1/changes/{id}/evidence` | Viewer | Old/new evidence references. |
| GET | `/api/v1/alerts` | Viewer | Filter review items by state or severity. |
| GET | `/api/v1/alerts/{id}` | Viewer | One alert. |
| POST | `/api/v1/alerts/{id}/review` | Reviewer + CSRF | Confirm, dismiss, correct, request another source, mark expected, or keep unresolved. |
| POST | `/api/v1/alerts/{id}/dismiss` | Reviewer + CSRF | Recorded dismissal shortcut. |
| POST | `/api/v1/corrections` | Analyst + CSRF | Preserve a correction separately for review. |
| POST | `/api/v1/corrections/{id}/review` | Reviewer + CSRF | Confirm, dismiss, or keep a correction unresolved. |
| POST | `/api/v1/corrections/{id}/reverse` | Reviewer + CSRF | Reverse a prior correction decision without erasing history. |
| POST | `/api/v1/entity-merges/{id}/review` | Reviewer + CSRF | Review a reversible identity proposal. |
| POST | `/api/v1/entity-merges/{id}/reverse` | Reviewer + CSRF | Reverse a confirmed merge and preserve an audit record. |
| GET | `/api/v1/conflicts` | Viewer | List current tenant source conflicts. |
| POST | `/api/v1/conflicts/{id}/review` | Reviewer + CSRF | Select display precedence, request another source, or keep unresolved. |

### Reports, market summary, audit, and deletion

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/reports` | Analyst + CSRF | Create an idempotent draft. |
| GET | `/api/v1/reports` | Viewer | List tenant reports. |
| GET | `/api/v1/reports/{id}` | Viewer | Report metadata and readiness. |
| POST | `/api/v1/reports/{id}/approve` | Reviewer/Admin + CSRF | Apply the unresolved-required-review approval gate. |
| GET | `/api/v1/reports/{id}/download?format=pdf|csv|json|evidence` | Analyst for drafts; Viewer after approval | Access-controlled report artifact. `evidence` is a CSV manifest. |
| DELETE | `/api/v1/reports/{id}` | Analyst + CSRF | Remove generated artifacts and retain a deletion tombstone. |
| GET | `/api/v1/markets/{location_id}/summary` | Viewer | Aggregate only the monitored dataset. |
| GET | `/api/v1/weekly-digest` | Viewer | Deterministic weekly monitoring/signal/review counts. |
| GET | `/api/v1/audit` | Viewer or policy-limited role | Tenant-safe audit history. |
| PATCH | `/api/v1/organizations/current/retention` | Admin + CSRF | Update validated retention windows. |
| POST | `/api/v1/organizations/current/deletion` | Admin + CSRF | Start idempotent organization deletion. |

CSV output neutralizes leading `=`, `+`, `-`, and `@`. Report approval must fail while included high-impact required-review alerts remain unresolved.

Report creation sets `expires_at` from the active organization's stored `retention_report_days`; the download route returns `410` after that timestamp. This MVP does not include a scheduled age-based purge. Explicit document, report, and organization deletion remove recognized private artifacts.

Draft artifacts are labelled internal-only and cannot be downloaded by viewers. Approval fails closed while any included required-review item is unresolved or while core evidence provenance (source reference, capture time, content hash, and parser version) is incomplete. Approval regenerates the PDF with an approved label and embedded evidence references; approved artifacts are then available to tenant viewers.

Organization deletion requires `{ "confirmation": "<exact organization name>", "reason": "..." }`. It deactivates and soft-deletes mutable tenant entry points, disables policies/sources, cancels queued or active monitoring work, removes recognized files, and keeps a minimal stable tombstone. Immutable monitoring history is not hard-deleted or anonymized by the current implementation.

## Health endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health/live` | Process liveness. |
| GET | `/health/ready` | Database and required startup readiness. |

Source adapter health is an authenticated organization view at `/api/v1/source-health`; a degraded external source does not masquerade as a factual no-change.
