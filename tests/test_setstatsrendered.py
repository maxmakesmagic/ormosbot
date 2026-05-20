"""Tests for setstatsrendered cache invalidation helpers."""

from ormosbot.setstatsrendered import (
    DETECTED_QUERY_CACHE_VERSION,
    QUERY_CACHE_VERSION,
    cached_detected_revision_matches,
    cached_revision_matches,
)


class TestCachedRevisionMatches:
    """Tests for query-extraction cache version matching."""

    TEMPLATE_FINGERPRINT = '{"Template:Scryfall stats":123}'

    def test_matches_same_revision_and_version(self) -> None:
        """Cache entries match only when both revision and extractor version match."""
        cached_revision = {
            "rev_id": 123,
            "detected_query_cache_version": DETECTED_QUERY_CACHE_VERSION,
            "query_cache_version": QUERY_CACHE_VERSION,
            "template_dependency_fingerprint": self.TEMPLATE_FINGERPRINT,
            "detected_queries": ['color="WB"'],
            "queries": ['color="WB"'],
        }

        assert (
            cached_revision_matches(
                cached_revision,
                123,
                self.TEMPLATE_FINGERPRINT,
            )
            is True
        )

    def test_detected_cache_can_match_without_query_cache_version(self) -> None:
        """Detected queries can be reused even when filtered-query cache is stale."""
        cached_revision = {
            "rev_id": 123,
            "detected_query_cache_version": DETECTED_QUERY_CACHE_VERSION,
            "template_dependency_fingerprint": self.TEMPLATE_FINGERPRINT,
            "detected_queries": ['color="WB"'],
            "query_cache_version": QUERY_CACHE_VERSION - 1,
        }

        assert (
            cached_detected_revision_matches(
                cached_revision,
                123,
                self.TEMPLATE_FINGERPRINT,
            )
            is True
        )
        assert (
            cached_revision_matches(
                cached_revision,
                123,
                self.TEMPLATE_FINGERPRINT,
            )
            is False
        )

    def test_rejects_same_revision_without_cache_version(self) -> None:
        """Older cache entries are invalidated after extractor changes."""
        cached_revision = {
            "rev_id": 123,
            "queries": ["t:creature"],
        }

        assert (
            cached_revision_matches(
                cached_revision,
                123,
                self.TEMPLATE_FINGERPRINT,
            )
            is False
        )

    def test_rejects_different_revision(self) -> None:
        """Revision mismatches still invalidate the cache."""
        cached_revision = {
            "rev_id": 122,
            "detected_query_cache_version": DETECTED_QUERY_CACHE_VERSION,
            "template_dependency_fingerprint": self.TEMPLATE_FINGERPRINT,
            "detected_queries": ['color="WB"'],
            "query_cache_version": QUERY_CACHE_VERSION,
        }

        assert (
            cached_revision_matches(
                cached_revision,
                123,
                self.TEMPLATE_FINGERPRINT,
            )
            is False
        )

    def test_rejects_same_page_revision_when_template_fingerprint_changes(self) -> None:
        """Template edits invalidate cached rendered queries without page edits."""
        cached_revision = {
            "rev_id": 123,
            "detected_query_cache_version": DETECTED_QUERY_CACHE_VERSION,
            "query_cache_version": QUERY_CACHE_VERSION,
            "template_dependency_fingerprint": '{"Template:Scryfall stats":122}',
            "detected_queries": ['color="WB"'],
            "queries": ['color="WB"'],
        }

        assert (
            cached_detected_revision_matches(
                cached_revision,
                123,
                self.TEMPLATE_FINGERPRINT,
            )
            is False
        )
        assert (
            cached_revision_matches(
                cached_revision,
                123,
                self.TEMPLATE_FINGERPRINT,
            )
            is False
        )
