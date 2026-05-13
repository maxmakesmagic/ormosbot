"""Tests for setstatsrendered cache invalidation helpers."""

from ormosbot.setstatsrendered import QUERY_CACHE_VERSION, cached_revision_matches


class TestCachedRevisionMatches:
    """Tests for query-extraction cache version matching."""

    def test_matches_same_revision_and_version(self) -> None:
        """Cache entries match only when both revision and extractor version match."""
        cached_revision = {
            "rev_id": 123,
            "query_cache_version": QUERY_CACHE_VERSION,
            "queries": ['color="WB"'],
        }

        assert cached_revision_matches(cached_revision, 123) is True

    def test_rejects_same_revision_without_cache_version(self) -> None:
        """Older cache entries are invalidated after extractor changes."""
        cached_revision = {
            "rev_id": 123,
            "queries": ["t:creature"],
        }

        assert cached_revision_matches(cached_revision, 123) is False

    def test_rejects_different_revision(self) -> None:
        """Revision mismatches still invalidate the cache."""
        cached_revision = {
            "rev_id": 122,
            "query_cache_version": QUERY_CACHE_VERSION,
        }

        assert cached_revision_matches(cached_revision, 123) is False
