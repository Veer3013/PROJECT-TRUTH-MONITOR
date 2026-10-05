# Release validation record

Validated on 6 August 2026 using Python 3.13.5.

## Automated tests

Command:

```text
python -m pytest -q -p no:cacheprovider
```

Result: **80 passed**.

Coverage command:

```text
python -m pytest --cov=app --cov-report=term -q -p no:cacheprovider
```

Result: **80 passed; 80% total application coverage**.

## Fresh migration and seed

A new SQLite database was migrated through all four revisions to Alembic head `e61b4c2d7a90`. `alembic check` reported no new upgrade operations. The demo seed completed with:

- Organization: `projecttruth-demo`
- Users: 4
- Watched projects: 4
- Source classes: 3

## HTTP smoke validation

The following routes were exercised using the FastAPI test client against the fresh migrated and seeded database:

| Route | Expected result | Result |
| --- | --- | --- |
| `GET /health/live` | 200 | Passed |
| `GET /health/ready` | 200 and current migration | Passed |
| `GET /login` | 200 | Passed |
| `GET /docs` | 200 | Passed |
| `POST /login` | 303 to dashboard | Passed |
| `GET /dashboard` after login | 200 | Passed |

## Static analysis note

Ruff configuration is included in `pyproject.toml`. Ruff could not be installed in the isolated release environment because the package index was unavailable. Syntax, imports, migrations, runtime startup, and behavior were instead exercised by the complete automated suite and smoke checks. Run the documented Ruff command in a connected development environment before a hosted production release.

## Release interpretation

This validation establishes a stable local/demo MVP. It does not certify live government portal compatibility, legal permission for automated access, hosted OCR accuracy, production PostgreSQL operation, multi-instance scheduler correctness, or production security certification.
