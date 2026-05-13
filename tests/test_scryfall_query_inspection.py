"""Tests for Scryfall query extraction and missing-query diagnostics."""

from ormosbot.scryfall_query_inspection import (
    determine_missing_query_reason,
    extract_scryfall_queries_from_html,
    inspect_scryfall_templates_in_wikitext,
)


class TestExtractScryfallQueriesFromHtml:
    """Tests for rendered Scryfall query extraction."""

    def test_extracts_direct_queries_only(self) -> None:
        """Keeps direct search links and skips tracking and non-query URLs."""
        html = """
        <div>
          <a href="https://scryfall.com/search?q=t%3Acreature">Creatures</a>
          <a href="https://scryfall.com/search?q=cmc%3E3&utm_source=mtgwiki">Tracked</a>
          <a href="https://example.com/elsewhere">Elsewhere</a>
          <a href="https://scryfall.com/search?q=angel">No colon</a>
        </div>
        """

        assert extract_scryfall_queries_from_html(html) == ["t:creature"]


class TestInspectScryfallTemplatesInWikitext:
    """Tests for raw template inspection."""

    def test_finds_query_like_template_parameters(self) -> None:
        """Recognizes Scryfall templates and captures colon-bearing values."""
        wikitext = "{{Scryfall stats|q=t:creature mv:3|format=modern}}"

        inspections = inspect_scryfall_templates_in_wikitext(wikitext)

        assert len(inspections) == 1
        assert inspections[0].normalized_name == "scryfall stats"
        assert inspections[0].query_like_values == ["t:creature mv:3"]


class TestDetermineMissingQueryReason:
    """Tests for high-level missing-query classification."""

    def test_reports_rendered_queries_found(self) -> None:
        """Rendered queries win over all missing-query explanations."""
        inspections = inspect_scryfall_templates_in_wikitext(
            "{{Scryfall count|q=t:artifact}}"
        )

        reason = determine_missing_query_reason(inspections, ["t:artifact"])

        assert reason == "rendered_queries_found"

    def test_reports_query_like_template_without_rendered_link(self) -> None:
        """Flags pages where the template looks right but emits no rendered query."""
        inspections = inspect_scryfall_templates_in_wikitext(
            "{{Scryfall count|q=t:artifact}}"
        )

        reason = determine_missing_query_reason(inspections, [])

        assert reason == "template_has_query_like_parameters_but_no_rendered_query"

    def test_reports_template_without_query_like_parameters(self) -> None:
        """Flags templates present without any obvious raw query value."""
        inspections = inspect_scryfall_templates_in_wikitext(
            "{{Scryfall count|label=Artifacts}}"
        )

        reason = determine_missing_query_reason(inspections, [])

        assert reason == "template_found_but_no_query_like_parameters"
