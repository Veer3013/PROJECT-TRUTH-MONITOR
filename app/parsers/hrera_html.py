from __future__ import annotations

from app.domain import FetchResult

from .fixture_html import FixtureHTMLParser


class HRERAHTMLParser(FixtureHTMLParser):
    parser_name = "hrera_gurugram_html"
    parser_version = "1.0.0"
    minimum_recognized_fields = 2
    required_core_fields = frozenset({"project_name", "registration_number"})

    def can_parse(self, fetch_result: FetchResult) -> bool:
        # Parser selection is content based. Collector/policy selection is where
        # official-domain authorization is enforced.
        return super().can_parse(fetch_result)


HRERAGurugramHTMLParser = HRERAHTMLParser

