"""Tests for Scryfall query extraction and missing-query diagnostics."""

from ormosbot.scryfall_query_inspection import (
    detect_scryfall_queries_from_html,
    determine_missing_query_reason,
    extract_scryfall_queries_from_html,
    filter_structured_scryfall_queries,
    inspect_scryfall_templates_in_wikitext,
)


class TestDetectScryfallQueriesFromHtml:
    """Tests for raw rendered Scryfall query detection."""

    def test_detects_only_queries_inside_marked_stats_and_counts(self) -> None:
        """Read marked template links before structured-query filtering."""
        html = """
        <div class="scrylinks" data-scryfall-stats="1">
          <span><a href="https://scryfall.com/search?q=t%3Acreature">Creatures</a></span>
          <a href="https://scryfall.com/search?q=cmc%3E3&amp;utm_source=mtgwiki">Tracked</a>
          <a href="https://example.com/elsewhere">Elsewhere</a>
        </div>
        <span data-scryfall-stats="1">
          <a href="https://scryfall.com/search?q=color%3D%22WB%22">White-black</a>
          <a href="https://scryfall.com/search?q=angel">Plain text</a>
        </span>
        <a href="https://scryfall.com/search?q=t%3Aartifact">Hard-coded link</a>
        <div class="scrylinks">
          <a href="https://scryfall.com/search?q=t%3Aland">Class only</a>
        </div>
        <a data-scryfall-stats="1" href="https://scryfall.com/search?q=t%3Ainstant">
          Anchor is not a descendant
        </a>
        <span data-scryfall-stats="0">
          <a href="https://scryfall.com/search?q=t%3Aenchantment">Wrong marker value</a>
        </span>
        """

        assert detect_scryfall_queries_from_html(html) == [
            "angel",
            'color="WB"',
            "t:creature",
        ]


class TestExtractScryfallQueriesFromHtml:
    """Tests for structured rendered Scryfall query extraction."""

    def test_extracts_structured_queries_inside_marked_output(self) -> None:
        """Keep structured marked links and skip tracking and plain text."""
        html = """
        <div data-scryfall-stats="1">
          <a href="https://scryfall.com/search?q=t%3Acreature">Creatures</a>
          <a href="https://scryfall.com/search?q=color%3D%22WB%22">White-black</a>
          <a href="https://scryfall.com/search?q=cmc%3E3&amp;utm_source=mtgwiki">Tracked</a>
          <a href="https://example.com/elsewhere">Elsewhere</a>
          <a href="https://scryfall.com/search?q=angel">No colon</a>
        </div>
        <a href="https://scryfall.com/search?q=t%3Aartifact">Hard-coded link</a>
        """

        assert extract_scryfall_queries_from_html(html) == [
            'color="WB"',
            "t:creature",
        ]


class TestFilterStructuredScryfallQueries:
    """Tests for structured-query filtering after raw detection."""

    def test_filters_plain_text_queries(self) -> None:
        """Plain-text detections are excluded from the final structured query set."""
        queries = ['color="WB"', "angel", "t:creature"]

        assert filter_structured_scryfall_queries(queries) == [
            'color="WB"',
            "t:creature",
        ]


class TestInspectScryfallTemplatesInWikitext:
    """Tests for raw template inspection."""

    def test_finds_query_like_template_parameters(self) -> None:
        """Recognizes Scryfall templates and captures colon-bearing values."""
        wikitext = "{{Scryfall stats|q=t:creature mv:3|format=modern}}"

        inspections = inspect_scryfall_templates_in_wikitext(wikitext)

        assert len(inspections) == 1
        assert inspections[0].normalized_name == "scryfall stats"
        assert inspections[0].query_like_values == ["t:creature mv:3"]

    def test_accepts_equals_based_scryfall_queries(self) -> None:
        """Structured queries using equals are kept as query-like values."""
        wikitext = '{{Scryfall count|1=color="WB"}}'

        inspections = inspect_scryfall_templates_in_wikitext(wikitext)

        assert len(inspections) == 1
        assert inspections[0].query_like_values == ['color="WB"']


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
