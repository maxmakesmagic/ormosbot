"""Cached and rate-limited session for HTTP requests."""

from datetime import timedelta

from requests import Session
from requests_cache import CacheMixin
from requests_ratelimiter import LimiterMixin, SQLiteBucket


class CachedLimiterSession(CacheMixin, LimiterMixin, Session):
    """Session class with caching and rate-limiting behavior."""


def get_session() -> CachedLimiterSession:
    """Create a CachedLimiterSession with default settings.

    Returns:
        CachedLimiterSession: A session with caching and rate-limiting.

    """
    session = CachedLimiterSession(
        # Limit to 1 request per second so it's 10/s across 10 jobs
        per_second=1,
        expire_after=timedelta(days=7),
        allowable_codes=[200, 400, 404],
        cache_name="cache.db",
        bucket_class=SQLiteBucket,
        bucket_kwargs={
            "path": "cache.db",
            "isolation_level": "EXCLUSIVE",
            "check_same_thread": False,
        },
    )
    return session
