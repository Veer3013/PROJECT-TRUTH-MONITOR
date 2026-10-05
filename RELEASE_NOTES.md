# ProjectTruth Monitor v0.1.0-final

Release date: 6 August 2026

## Release status

This package is a complete, demonstration-ready MVP for an evidence-first HRERA Gurugram real-estate project monitoring workflow. It is designed to run deterministically with frozen synthetic fixtures and authorized user uploads.

## Final fixes

- Corrected alert-detail rendering so review items without a detected change explicitly direct the reviewer to preserved timeline/source context, including conflict-backed alerts.
- Corrected timeline filtering so a new-phase registration remains visible as both a phase relationship and a deterministic signal.
- Made seeded Metro Square conflict ordering deterministic so the first displayed comparison contains two preserved source values.
- Added Windows setup and run scripts.
- Added Docker build exclusions and a recorded release-validation report.

## Validated release gates

- 80 automated tests passed.
- Application code coverage: 80% in the release environment.
- Fresh Alembic migration reached current head with no pending schema operations.
- Idempotent demo seed completed with four users, four watched projects, and three source classes.
- Liveness, readiness, login, dashboard, and Swagger routes passed HTTP smoke tests.

## Deliberate boundaries

- Live HRERA network collection remains disabled by default and is not represented as production-qualified.
- CAPTCHA, login walls, restricted access, unsafe redirects, and unreviewed source policies fail closed.
- Hosted OCR and LLM providers are interfaces only; no provider is silently activated.
- The product is decision support, not legal due diligence, valuation, investment advice, fraud classification, title verification, or a completion guarantee.
- PostgreSQL is supported by the database abstraction and driver, but this release is validated primarily with SQLite.
