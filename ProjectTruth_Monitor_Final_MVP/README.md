# ProjectTruth Monitor

ProjectTruth Monitor is an evidence-first real-estate project monitoring MVP for a narrow HRERA Gurugram pilot. It captures immutable source snapshots, compares compatible facts deterministically, keeps marketing claims separate from official records, routes uncertain or material items to human review, and produces evidence-linked timelines and reports.

The dependable local path uses frozen synthetic fixtures. Live HRERA access is disabled by default, is never used by the test suite or demo seed, and must not be enabled without a current source-policy review.

> ProjectTruth Monitor is a decision-support tool. It is not a property listing portal, broker, title-verification service, legal due-diligence authority, investment adviser, valuation system, fraud classifier, or completion guarantee.

## Architecture at a glance

The application is a Python 3.13 FastAPI modular monolith:

```text
Jinja2/HTMX UI and /api/v1
  -> signed session, CSRF, tenant and role checks
  -> watchlist/project services
  -> source-policy gate
  -> collector and versioned parser
  -> immutable snapshot and source facts
  -> deterministic diff, signals and evidence
  -> review, timeline, digest and report exports
```

SQLAlchemy and Alembic own persistence. SQLite is the configured local and Docker path. `DATABASE_URL` is the database abstraction and the PostgreSQL driver is included, but this MVP has not been qualified as a production PostgreSQL deployment. Authorized uploads and generated reports are private filesystem artifacts served only through authenticated routes.

See [Architecture](docs/architecture.md) for the trust layers and module map.

## Windows setup - Python 3.13

Open Windows PowerShell in the repository root. Confirm that the Python 3.13 launcher entry exists:

```powershell
py -0p
py -3.13 --version
```

For the shortest setup path, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup_windows.ps1
.\scripts\run_windows.ps1
```

The manual commands below perform the same setup and remain useful for troubleshooting.

Then run these commands exactly:

```powershell
py -3.13 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m alembic upgrade head
python -m app.seed_demo
python -m pytest -q
python -m uvicorn app.main:app --reload
```

Before any non-local use, edit `.env` and replace `SECRET_KEY`. The value in `.env.example` is an obvious placeholder, not a deployment secret. The application starts without OCR credentials, an LLM key, Redis, or live government access.

The seed is idempotent. Running `python -m app.seed_demo` again updates the same synthetic demo organization instead of intentionally creating another one.

## Local URLs

| Purpose | URL |
| --- | --- |
| Dashboard | [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard) |
| Login | [http://127.0.0.1:8000/login](http://127.0.0.1:8000/login) |
| Swagger API docs | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) |
| OpenAPI JSON | [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json) |
| Liveness | [http://127.0.0.1:8000/health/live](http://127.0.0.1:8000/health/live) |
| Readiness | [http://127.0.0.1:8000/health/ready](http://127.0.0.1:8000/health/ready) |

Readiness verifies database access and that the database revision equals the current Alembic head. Source degradation is reported separately and does not become a false factual no-change.

## Local-only demo accounts

These users belong to the seeded local demo organization. Never reuse these credentials outside local development.

| Role | Email | Password |
| --- | --- | --- |
| Admin | `admin@projecttruth.local` | `DemoAdmin!2026` |
| Reviewer | `reviewer@projecttruth.local` | `DemoReviewer!2026` |
| Analyst | `analyst@projecttruth.local` | `DemoAnalyst!2026` |
| Viewer | `viewer@projecttruth.local` | `DemoViewer!2026` |

Use the analyst account for watchlists, permitted fixture checks, authorized uploads, and report drafts. Use the reviewer account for alert, conflict, correction, merge, and report decisions. Use the admin account for membership, source-policy, retention, organization deletion, and final-report controls. The viewer is read-only.

## Implemented MVP features

- Signed HTTP-only session authentication, Argon2 password hashing, cookie CSRF protection, four roles, organization membership checks, tenant-scoped lookups, request IDs, safe errors, and in-process login/refresh/upload rate limits.
- Watchlist CRUD, manual/daily/weekly cadence, alert preferences, project adds, duplicate-safe CSV preview and commit, row errors, and formula-safe CSV output.
- Registration-first project identity, provisional matches, reviewed and reversible merge proposals, aliases, parent/phase relationships, and a phase relationship map.
- Source-policy records, a synthetic fixture collector, policy-gated HRERA and developer-page collector paths, SSRF and redirect checks, immutable snapshots, source facts, deterministic changes, evidence, signals, and explicit degraded monitoring runs.
- Change Ledger filters, Evidence Cards, Project Truth Timeline, Monitoring Confidence dimensions, Project Truth Passport, source conflicts, role-specific review-action templates, alert review, correction review/reversal, merge review/reversal, and audit history.
- Authorized PDF upload, MIME/signature/size checks, sanitized names, private storage, immutable document versions, deterministic searchable-text extraction, page-linked marketing claims, brochure-versus-official comparison, and document deletion.
- Weekly digest, monitored-dataset locality summary, source health, internal-only draft reports, approved viewer-readable reports, PDF/CSV/JSON/evidence exports, report expiry checks, report deletion, and fail-closed review/evidence approval gates.
- Optional local APScheduler dispatch with a process lock, database-backed jobs, idempotent monitoring windows, retry/circuit primitives, frozen fixtures, demo seed data, pytest coverage, Alembic migrations, and Docker files.
- Server-rendered Jinja2 pages with local CSS and vendored HTMX. There is no Node build, CDN, remote font, or required JavaScript package manager.

The four seeded synthetic scenarios are:

1. Aster Heights Phase II (`HRERA-DEMO-101`): official completion moves from 30 June 2027 to 31 March 2028, a deterministic `+275 days` change.
2. Lakeview Residences (`HRERA-DEMO-202`): brochure claims for six towers, 1,200 units, and December 2027 conflict with captured official values of five towers, 1,080 units, and June 2028.
3. Orion Greens Phase III (`HRERA-DEMO-303`): a new registration and parent-phase relationship increments the Sector 79 monitored-dataset pulse.
4. Metro Square (`HRERA-DEMO-404`): the official fixture has no completion date, low-confidence OCR fixture data suggests December 2029, and a developer fixture claims June 2029. The result remains unresolved and is not a confirmed external alert.

The seed materializes the trust records used by the walkthrough. Its brochure record is synthetic metadata, not a downloadable user PDF. Upload an authorized local PDF to exercise private file download and immutable revision controls. See [Fixture-backed demo walkthrough](docs/demo-walkthrough.md).

## Configuration

Copy `.env.example` to `.env`; Pydantic loads it from the repository root. Relative storage paths are resolved from that root.

| Variable | Local behavior |
| --- | --- |
| `APP_NAME`, `ENVIRONMENT` | Display name and `development`, `test`, or `production` mode. Production rejects the built-in development secret and secrets shorter than 32 characters. |
| `SECRET_KEY` | Signs sessions and derives CSRF tokens. Replace it before any non-local use. |
| `DATABASE_URL` | SQLAlchemy URL. The tested local default is repository-local SQLite. |
| `UPLOAD_DIR`, `REPORT_DIR` | Private artifact roots, outside the static directory. |
| `MAX_UPLOAD_MB` | PDF upload limit, from 1 to 100 MB. |
| `SESSION_COOKIE_SECURE` | Keep `false` only for local HTTP. Set `true` behind HTTPS. |
| `ALLOWED_HOSTS` | Comma-separated trusted request hosts. Add the deployed hostname explicitly. |
| `ENABLE_LIVE_HRERA` | Additional process-level gate for the HRERA network collector. It does not override a disabled or unreviewed source policy. |
| `ENABLE_SCHEDULER` | Starts the single-process APScheduler dispatcher when `true`. Keep it off with multiple web workers unless scheduling is moved to a dedicated process. |
| `DEFAULT_MONITORING_CADENCE` | Default for new watchlists and CSV rows that omit cadence: `manual`, `daily`, or `weekly`. |
| `SOURCE_REQUEST_TIMEOUT`, `SOURCE_MAX_RESPONSE_MB`, `HRERA_RATE_LIMIT_SECONDS` | Bounded live-source request settings; policy values still gate collection. |
| `OCR_PROVIDER` | Provider label. The repository currently wires the disabled OCR implementation only. Unreadable/image-only PDFs remain `needs_review`; adding a real provider requires code and policy approval. |
| `LLM_PROVIDER`, `LLM_API_KEY` | Optional provider boundary. The repository currently wires the disabled claim parser only, and deterministic operation does not need a key. |
| `RETENTION_UPLOAD_DAYS` | Configuration boundary for upload retention. Automatic age-based upload purging is not implemented in this MVP. |
| `RETENTION_REPORT_DAYS` | Process-level default reserved for deployments and bootstrap configuration. New reports use the authenticated organization's stored report-retention window. |
| `LOGIN_RATE_LIMIT`, `REFRESH_RATE_LIMIT`, `UPLOAD_RATE_LIMIT` | Per-process request counts per 60-second window. A multi-instance deployment needs a shared limiter. |

The per-organization retention screen stores and audits upload/report windows. Current report creation uses that organization's `retention_report_days` value for `expires_at`. There is no scheduled retention purge, so expiry blocks downloads but physical age-based cleanup still needs a lifecycle worker. Organization deletion is enforced immediately as described below.

### Live HRERA boundary

The live path requires all of the following:

1. `ENABLE_LIVE_HRERA=true`.
2. An enabled, current source access policy for the exact domain and `httpx` access mode.
3. Recorded robots and terms review, reviewer, and review timestamp.
4. An approved public HTTPS URL that passes DNS, redirect, size, timeout, and SSRF checks.

The adapter stops on CAPTCHA, authentication, restriction, unsafe redirects, rate limits, or layout/parser failure. It has not been qualified against the current public HRERA site, so the frozen fixture adapter remains the supported demo/test path. See [Source access and evidence policy](docs/source-policy.md).

### OCR and LLM boundary

Searchable PDFs use deterministic extraction. If the PDF has no reliable searchable text, the disabled OCR provider records a manual-review limitation. `OCR_PROVIDER` and `LLM_PROVIDER` do not dynamically install or activate a third-party service. A hosted implementation must add a concrete provider, credentials, policy permission, privacy review, tests, and failure handling. OCR or LLM candidates remain marketing claims and can never overwrite official facts.

## Migrations

Application startup does not create tables. Apply Alembic before seeding or serving:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic heads
python -m alembic check
```

The repository contains the initial schema plus follow-up identity and source-endpoint integrity revisions. `upgrade head` is the supported target; do not depend on a hard-coded revision identifier in deployment scripts.

To verify a fresh SQLite migration without touching the normal demo database:

```powershell
$env:DATABASE_URL = "sqlite:///./data/migration-check.db"
python -m alembic upgrade head
python -m alembic current
Remove-Item -LiteralPath .\data\migration-check.db -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL
```

## Tests and lint

Run the test suite:

```powershell
python -m pytest -q
```

For a clean run without the local pytest cache:

```powershell
python -m pytest -q -p no:cacheprovider
```

Ruff configuration is in `pyproject.toml`, but Ruff is not installed by the runtime `requirements.txt`. Install it in the development virtual environment and run:

```powershell
python -m pip install ruff
python -m ruff check app tests alembic --no-cache
```

Documentation intentionally does not pin a passing test count: use the command output from the exact revision being released. Tests use isolated temporary databases and frozen fixtures; they must not call a government server.

## Docker

Docker Desktop is required. Create the local environment file first and replace its secret if the service will be reachable by anyone else:

```powershell
Copy-Item .env.example .env
docker compose config
docker compose build
docker compose up
```

Or build and start in one command:

```powershell
docker compose up --build
```

Compose stores the SQLite database, authorized uploads, and reports in the named `projecttruth_data` volume. It applies Alembic migrations and runs the idempotent demo seed before starting Uvicorn. It deliberately keeps live HRERA disabled.

Stop the stack:

```powershell
docker compose down
```

Remove the local Docker demo data only when a reset is intended:

```powershell
docker compose down --volumes
```

## Retention and deletion behavior

- Admins can update organization retention values from `/settings/retention`; changes are validated and audited.
- Individual document/report deletion is tenant-scoped and idempotent, removes private files, soft-deletes the database record, creates a minimal tombstone, and makes old download routes unavailable.
- Organization deletion requires an admin, CSRF protection for cookie requests, an exact organization-name confirmation, and a reason.
- Organization deletion deactivates the organization, soft-deletes projects/watchlists/documents/reports, disables sources and source policies, cancels queued/running/retry monitoring work, deletes recognized private upload/report artifacts, and preserves one minimal idempotency tombstone plus audit metadata.
- Immutable monitoring history is retained in the database rather than hard-deleted or anonymized by the current service. If legal/privacy policy requires hard deletion or anonymization, implement and validate that policy before deployment.
- Organization deletion is destructive. Back up any authorized data that must be retained before submitting it.

## Troubleshooting

### The wrong Python is selected

`python --version` may resolve to a global Python other than 3.13. Recreate the environment with the launcher and activate it:

```powershell
deactivate 2>$null
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

The final command must report Python 3.13.x.

### PowerShell will not activate the environment

The execution-policy change is process-scoped and disappears when the window closes:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### `python` or `alembic` uses global packages

The prompt should begin with `(.venv)`. Use module form to avoid a global executable:

```powershell
python -m pip --version
python -m alembic upgrade head
python -m pytest -q
```

### Reset the local SQLite demo

Stop Uvicorn first. Confirm that `.env` points to the repository-local demo database, then run:

```powershell
Remove-Item -LiteralPath .\data\projecttruth.db -ErrorAction SilentlyContinue
python -m alembic upgrade head
python -m app.seed_demo
```

Do not remove an unknown or production database. Check `DATABASE_URL` before resetting anything.

### Optional OCR is missing

Keep `OCR_PROVIDER=disabled`. Searchable PDFs still use deterministic text extraction. Image-only or unreadable documents are sent to manual review with a safe message and do not become confirmed official facts.

### The optional LLM is not configured

Keep `LLM_PROVIDER=disabled` and leave `LLM_API_KEY` blank. Identity, normalization, snapshots, diffs, signals, evidence, timelines, confidence dimensions, and report counts continue to work deterministically.

### A source adapter is disabled

This is expected for live HRERA. Use the seeded fixture source. An admin must record current robots and terms reviews, approved domains, access mode, rate limit, and retention rules before a network source policy can be enabled. `ENABLE_LIVE_HRERA=true` alone is insufficient.

### Windows path or OneDrive issues

- Always quote paths that contain spaces.
- Keep `UPLOAD_DIR` and `REPORT_DIR` relative to the repository or set explicit absolute Windows paths.
- If OneDrive locks the SQLite file, stop Uvicorn and any test processes, then retry. For persistent locking, copy or clone the repository into a non-synchronized development directory.
- Do not place `.venv`, uploads, reports, databases, caches, or secrets in a shared repository archive.

### Readiness fails while liveness succeeds

`/health/live` shows that the process is running. `/health/ready` returns `503` when the database is unavailable or not at Alembic head. Run `python -m alembic upgrade head`, check `DATABASE_URL`, and inspect the console using the request ID. Normal users should not receive a stack trace or internal path.

## Intentionally excluded or incomplete

- Property listings, recommendations, rankings, pricing/valuation predictions, title verification, legal compliance certification, fraud classification, automated credit/contract/purchase decisions, full India coverage, CAPTCHA solving, unauthorized scraping, construction computer vision, supplier lead generation, and undocumented Suproc integration.
- A concrete OCR service, concrete LLM service, and production malware scanner. The current local malware scanner is an explicit `not_scanned` stub.
- A Redis/RQ hosted worker profile, distributed scheduler lock, and distributed rate limiter. The implemented scheduler and limiter are single-process local components.
- Automatic age-based upload cleanup and physical cleanup of expired reports. Authenticated report downloads do enforce `expires_at`, and explicit deletion removes artifacts.
- Production-qualified PostgreSQL operations, object storage, backups/restores, TLS termination, secret manager integration, telemetry backend, and deployment-specific security testing.
- Validated live HRERA coverage. The collector interface is policy-gated and fail-closed; fixtures are the reliable release path.

## Next recommended improvements

1. Add a policy-approved concrete OCR provider and malware scanner, with privacy/security tests and manual-review thresholds.
2. Add a retention worker that applies per-organization settings and records deletion outcomes.
3. Validate PostgreSQL migrations and concurrency, then move rate limiting, scheduling, and artifact storage to shared hosted services.
4. Perform current legal/robots/source review and fixture-based parser qualification before any limited live pilot.
5. Add backup/restore exercises, operational metrics export, TLS/secret management, and deployment-specific penetration testing.

## Documentation

- [Architecture](docs/architecture.md)
- [API overview](docs/api-overview.md)
- [Source access policy](docs/source-policy.md)
- [Product and legal boundaries](docs/boundaries.md)
- [Demo walkthrough](docs/demo-walkthrough.md)
- [UI integration contract](docs/ui-integration.md)

## Repository safety

Do not commit `.env`, `.venv`, SQLite databases, authorized uploads, generated reports, rendered temporary files, cache directories, or secrets. The included `.gitignore` covers the expected local artifacts. Source fixtures are synthetic and use reserved or `.invalid` references where practical.

This MVP is not production-ready. Production use requires current source-policy review, deployment secrets, TLS and secure cookies, hardened storage, database backup and recovery, malware scanning, operational monitoring, privacy/legal review, and environment-specific security testing.
