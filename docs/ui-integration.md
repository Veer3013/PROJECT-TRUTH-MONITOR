# Dashboard route and context contract

The dashboard is server-rendered Jinja2 with official HTMX 2.0.10 vendored under `app/static/vendor`. There is no Node, bundle, CDN, font, or image dependency. Local `app.js` converts JSON API forms, handles multipart uploads, renders CSV previews, and provides small accessibility enhancements. If JavaScript is unavailable, links and read-only pages still work; server form adapters may preserve full mutation fallback where required.

## Page routes and templates

| HTML route | Template |
| --- | --- |
| `/login` | `login.html` |
| `/dashboard` | `dashboard.html` |
| `/watchlists` | `watchlists/list.html` |
| `/watchlists/{id}` | `watchlists/detail.html` |
| `/watchlists/{id}/import` | `watchlists/import.html` |
| `/watchlists/import` | Optional destination-selector alias for the same import template. |
| `/projects/{id}` | `projects/profile.html` |
| `/projects/{id}/passport` | `projects/passport.html` |
| `/projects/{id}/phase-map` | `projects/phase_map.html` |
| `/projects/{id}/timeline` | `projects/timeline.html` |
| `/projects/{id}/changes` | `projects/changes.html` |
| `/evidence/{id}` | `evidence/detail.html` |
| `/alerts` | `alerts/list.html` |
| `/alerts/{id}` | Tenant-checking alias to the selected alert workflow. |
| `/conflicts` | `conflicts/list.html` |
| `/conflicts/{id}` | Tenant-checking alias to the selected conflict workflow. |
| `/projects/{id}/documents` | `documents/upload.html` |
| `/projects/{id}/documents?document_id={document_id}` | Selected document extraction/comparison/version workflow. |
| `/markets/summary` | `markets/summary.html` |
| `/markets/{location_id}` | Location-scoped monitored-dataset summary. |
| `/digest` | `digest.html` |
| `/reports` | `reports/list.html` |
| `/reports/{id}` | Tenant-checking alias that currently redirects to `/reports`; there is no separate HTML detail template. |
| `/sources/health` | `sources/health.html` |
| `/organization/members` | `organizations/members.html` |
| `/audit` | `audit/list.html` |
| `/settings/retention` | `settings/retention.html` |

The source health page uses a real HTMX GET to refresh `#main-content` from a normal full-page response using `hx-select`. The anchor remains a normal navigation link if HTMX cannot load.

## Shared context

All dashboard templates tolerate missing top-level data and empty collections. Context values may be mapping-like objects or ORM/Pydantic objects with the same attributes.

Recommended shared keys:

| Key | Meaning |
| --- | --- |
| `current_user` or `user` | `id`, `display_name`, `email`. |
| `organization` | `id`, `name`, and optional slug. |
| `csrf_token` | Session-derived token for cookie-authenticated mutations. |
| `overall_source_state` | `healthy`, `degraded`, `partial`, `stale`, `blocked`, or `failed`. |
| `flash_messages` | List of `{kind, title, message}` where kind is info/success/warning/error. |
| `page`, `total_pages` | Optional pagination state. |

Each major factual screen also accepts a source name/class, freshness label, source date, and source state. If omitted, the template says that verification/source information is unavailable rather than inventing a current value.

## Project context

Project templates expect `project` with `id`, `name`, `registration_number`, developer and location labels, identity status, source/freshness state, and optional `watchlist_item_id`. The project profile additionally accepts `facts` and `sources`. Passport accepts `passport` and `confidence`. Timeline accepts `events`. Change Ledger accepts `changes`. Phase map accepts `parent`, `phases`, and optional merge `proposals`.

Confidence dimensions are intentionally separate: source reliability, access completeness, identity, extraction, change, freshness, and conflict state. Pass a label/band and optional 0-100 display percentage for each. The percentage is a UI meter, not a hidden risk score.

## Forms and API alignment

Forms marked `data-api-form` are serialized to JSON, send `X-CSRF-Token`, and redirect to their `data-success-url` after a safe response. Forms marked `data-multipart-form` retain `FormData`. Login/logout use form encoding. CSV preview reads the selected local text file in the browser and sends `{csv_text}` to the preview API; the signed token is then committed with destination watchlist and idempotency key. The selected document workflow posts JSON to extraction/comparison endpoints and multipart data to immutable-version upload.

The backend should provide a fresh idempotency key for each logical monitoring window/report/import where possible. Template defaults exist only as a last-resort demo fallback.

Integration-specific mutation contracts referenced by the settings/conflict screens are:

- `POST /api/v1/conflicts/{id}/review`.
- `PATCH /api/v1/organizations/current/retention`.
- `POST /api/v1/organizations/current/deletion`.

These routes are exposed in the current application. Continue to hide/disable controls by role in the HTML context; the API role check remains authoritative.

The OCR and LLM status badges reflect configuration labels. The current backend wires disabled provider implementations only, so a non-`disabled` label must not be presented as proof that a third-party provider is active.

## Source and freshness language

`source_meta` should appear on every major factual screen. State should be one of current/verified/healthy, stale/partial/degraded, blocked/failed, or unverified. When monitoring is incomplete, provide the last successful verification timestamp and do not label the result no-change.

## Safety expectations

- Jinja autoescaping remains enabled.
- Never pass a local storage path to a template.
- Source URLs are rendered only when the evidence policy permits them.
- Uploaded/downloaded artifacts use authenticated routes.
- Viewer/analyst/reviewer/admin action visibility is context-controlled, but API authorization remains authoritative.
- GET views do not mutate state.
- Every destructive form uses explicit wording, role gating, CSRF, and a server-side idempotency check.

## Accessibility

The shell provides skip navigation, visible focus, landmarks, table headers, labels, live status regions, keyboard-dismissable navigation, reduced-motion handling, responsive tables, and print styles. Do not replace text status labels with color-only indicators. Preserve `aria-current`, `role=status/alert`, source/freshness labels, and the locality disclaimer when adapting the contexts.
