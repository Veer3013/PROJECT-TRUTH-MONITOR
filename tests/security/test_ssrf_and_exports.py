from __future__ import annotations

import pytest

from app.services.reports import export_csv
from app.services.source_policy import UnsafeURLError, validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
    ],
)
def test_ssrf_blocks_local_metadata_and_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_public_url(
            url,
            allowed_domains=("example.com", "localhost", "127.0.0.1", "169.254.169.254"),
            allowed_schemes=("http", "https"),
            resolve_dns=False,
        )


def test_csv_export_neutralizes_spreadsheet_formulas() -> None:
    payload = export_csv(
        [
            {"name": "=HYPERLINK(\"https://bad.invalid\")"},
            {"name": "+1+1"},
            {"name": "-2+3"},
            {"name": "@SUM(A1:A2)"},
        ],
        fieldnames=("name",),
    ).decode("utf-8-sig")
    assert "'=HYPERLINK" in payload
    assert "'+1+1" in payload
    assert "'-2+3" in payload
    assert "'@SUM" in payload

