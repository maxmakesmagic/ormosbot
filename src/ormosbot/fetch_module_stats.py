"""Fetch Scryfall stats for a matrix shard of queries."""

from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import nullcontext
from pathlib import Path

import pywikibot
import requests
from tenacity import (
    Retrying,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

from ormosbot.cachedlimiter import CachedLimiterSession, get_session
from ormosbot.colors import COLOR_ORDER
from ormosbot.wiki_stats import load_existing_stats

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


@retry
def scryfall_query(session: CachedLimiterSession, query: str) -> requests.Response:
    """Perform a Scryfall API search query and return the JSON response."""
    return _request_scryfall(session, query)


def _request_scryfall(
    session: CachedLimiterSession, query: str, *, force_refresh: bool = False
) -> requests.Response:
    log.info("Querying Scryfall API with query: %s", query)

    cache = session.cache_disabled() if "+" in query else nullcontext()
    with cache:
        return session.get(
            "https://api.scryfall.com/cards/search",
            timeout=10,
            headers={"User-Agent": "OrmosBot/1.0", "Accept": "application/json"},
            params={"q": query},
            force_refresh=force_refresh,
        )


class _RetryableSearchError(Exception):
    """A Scryfall response that can be retried."""


def _count_from_response(response: requests.Response, query: str) -> int | None:
    status = response.status_code
    if status == 408 or 500 <= status < 600:
        raise _RetryableSearchError(f"HTTP {status}")

    if 200 <= status < 300 or status == 404:
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            data = None

        if status == 404:
            if (
                isinstance(data, dict)
                and data.get("object") == "error"
                and data.get("code") == "not_found"
            ):
                return 0
        else:
            count = data.get("total_cards") if isinstance(data, dict) else None
            if type(count) is int and count >= 0:
                return count
            raise _RetryableSearchError("Invalid or missing total_cards")

    if status == 429:
        log.warning(
            "Scryfall rate limited %s. Retry-After: %s",
            query,
            response.headers.get("Retry-After", "unknown"),
        )
    else:
        log.warning("No Scryfall count for %s (HTTP %d)", query, status)
    return None


def _fetch_raw_count(
    session: CachedLimiterSession, query: str, color: str
) -> int | None:
    bracketed_search = f"({query}) id={color}"
    search = bracketed_search
    retryable = (requests.exceptions.RequestException, _RetryableSearchError)
    try:
        for attempt in Retrying(
            retry=retry_if_exception_type(retryable),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1),
            reraise=True,
        ):
            with attempt:
                refresh = attempt.retry_state.attempt_number > 1
                response = _request_scryfall(session, search, force_refresh=refresh)
                if (
                    search == bracketed_search
                    and response.status_code == 400
                    and "Display options" in response.text
                ):
                    search = f"{query} id={color}"
                    log.info("Retrying without brackets for query: %s", search)
                    response = _request_scryfall(session, search, force_refresh=refresh)
                return _count_from_response(response, search)
    except retryable as exc:
        log.warning("No Scryfall count for %s after retries: %s", search, exc)
    return None


def fetch_scryfall_raw_stats(
    session: CachedLimiterSession, query: str
) -> dict[str, int | None]:
    """Return per-color counts with None for unavailable results."""
    return {color: _fetch_raw_count(session, query, color) for color in COLOR_ORDER}


def fallback_count(
    fallback_stats: dict[str, dict[str, str]], query: str, color: str
) -> int:
    """Return the previously-published count for a query/color, or 0.

    Used when a live Scryfall request fails so we keep the last known good value
    rather than overwriting the wiki with a spurious 0. Wiki stats are keyed by
    the casefolded query (see update_module_data.switch_from_mapping).
    """
    raw_value = fallback_stats.get(query.casefold(), {}).get(color.lower(), "0")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 0
    if value:
        log.warning(
            "Using previously-published count %d for %s id=%s (Scryfall unavailable)",
            value,
            query,
            color,
        )
    return value


def fetch_scryfall_stats(
    session: CachedLimiterSession,
    query: str,
    fallback_stats: dict[str, dict[str, str]] | None = None,
) -> dict[str, int]:
    """Fetch stats from Scryfall API.

    When a request fails (rate limiting, 503 maintenance, etc.) the count falls
    back to the previously-published value from ``fallback_stats`` so transient
    Scryfall outages do not zero out the wiki data. A genuine 404 (no matching
    cards) still records 0.
    """
    fallback_stats = fallback_stats or {}
    stats = {}

    for color in COLOR_ORDER:
        full_query = f"({query}) id={color}"
        no_brackets = f"{query} id={color}"

        log.debug("Fetching Scryfall stats for query: %s", full_query)
        response = scryfall_query(session, full_query)
        log.debug("Response status: %s", response.status_code)
        if response.ok:
            data = response.json()
            log.debug("Total cards for %s: %s", color, data.get("total_cards", 0))
            stats[color.lower()] = data.get("total_cards", 0)
        elif response.status_code == 404:
            log.debug("No cards found for query: %s", full_query)
            stats[color.lower()] = 0
        elif response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            log.warning(
                "Scryfall rate limited query %s (429). Retry-After: %s",
                full_query,
                retry_after,
            )
            stats[color.lower()] = fallback_count(fallback_stats, query, color)
        elif response.status_code == 400 and "Display options" in str(response.text):
            log.info("Retrying without brackets for query: %s", no_brackets)
            response = scryfall_query(session, no_brackets)
            if response.ok:
                data = response.json()
                log.debug("Total cards for %s: %s", color, data.get("total_cards", 0))
                stats[color.lower()] = data.get("total_cards", 0)
            elif response.status_code == 404:
                log.debug("No cards found for query: %s", no_brackets)
                stats[color.lower()] = 0
            elif response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "unknown")
                log.warning(
                    "Scryfall rate limited query %s (429). Retry-After: %s",
                    no_brackets,
                    retry_after,
                )
                stats[color.lower()] = fallback_count(fallback_stats, query, color)
            else:
                log.error(
                    "Error fetching stats for query %s: %s %s",
                    no_brackets,
                    response.status_code,
                    response.text,
                )
                stats[color.lower()] = fallback_count(fallback_stats, query, color)
        else:
            log.error(
                "Error fetching stats for query %s: %s %s",
                full_query,
                response.status_code,
                response.text,
            )
            stats[color.lower()] = fallback_count(fallback_stats, query, color)

    return stats


def update_data_module(
    session: CachedLimiterSession,
    queries: list[str],
    fallback_stats: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """Fetch stats for each query and return a mapping ready for serialization."""
    results: dict[str, dict[str, str]] = {}
    for query in tqdm(queries, desc="Updating stats"):
        stats = fetch_scryfall_stats(session, query, fallback_stats)
        results[query] = {color: str(value) for color, value in stats.items()}
    return results


def parse_int_env(name: str) -> int | None:
    """Return the given environment variable parsed as an int, if set."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def load_queries_from_file(path_str: str) -> list[str]:
    """Load queries from a JSON file containing a list of strings."""
    path = Path(path_str)
    if not path.exists():
        raise RuntimeError(f"Query file {path} does not exist")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise RuntimeError(f"Query file {path} must contain a JSON list")
    queries: list[str] = []
    for idx, item in enumerate(data):
        if not isinstance(item, str):
            raise RuntimeError(
                f"Query at index {idx} in {path} is not a string: {item!r}"
            )
        queries.append(item)
    return queries


def require_env_int(name: str) -> int:
    """Return the integer value of an environment variable or raise."""
    value = parse_int_env(name)
    if value is None:
        raise RuntimeError(
            f"Environment variable {name} is required for shard processing"
        )
    return value


def main() -> None:
    """Fetch stats for the current matrix shard and write them to disk."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-file",
        default="chunk_stats.json",
        help="Path to the JSON file that will store the shard's stats",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to config.json providing HTTP headers for the wiki",
    )
    parser.add_argument(
        "--raw-results",
        action="store_true",
        help="Write integer counts and nulls for failed lookups",
    )
    args = parser.parse_args()

    Path("logs").mkdir(exist_ok=True)
    file_handler = logging.FileHandler("logs/fetch_module_stats.log", encoding="utf-8")
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[file_handler, stream_handler],
    )

    chunk_index = require_env_int("MATRIX_CHUNK_INDEX")
    queries_file = os.environ.get("MATRIX_QUERIES_FILE")
    if not queries_file:
        raise RuntimeError(
            "MATRIX_QUERIES_FILE must be set to the chunk query JSON file"
        )
    queries = load_queries_from_file(queries_file)
    pywikibot.info(
        "Shard %d fetching %d queries",
        chunk_index,
        len(queries),
    )

    ordered_queries = sorted(set(queries))
    stats_mapping: dict[str, dict[str, str]] | dict[str, dict[str, int | None]]
    if args.raw_results:
        session = get_session()
        stats_mapping = {
            query: fetch_scryfall_raw_stats(session, query)
            for query in tqdm(ordered_queries, desc="Fetching stats")
        }
    else:
        fallback_stats = load_existing_stats(Path(args.config))
        if fallback_stats:
            pywikibot.info(
                "Loaded %d existing query stats from the wiki for fallback",
                len(fallback_stats),
            )
        else:
            # Surface this prominently: without fallback data a Scryfall outage will
            # zero out the wiki module data, which is exactly what we want to avoid.
            log.warning(
                "No existing stats loaded from the wiki; Scryfall failures will "
                "fall back to 0 for this shard"
            )

        stats_mapping = update_data_module(
            get_session(), ordered_queries, fallback_stats
        )

    payload = {
        "chunk_index": chunk_index,
        "query_count": len(queries),
        "stats": stats_mapping,
    }

    output_path = Path(args.output_file)
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    pywikibot.info("Wrote shard stats to %s", output_path)


if __name__ == "__main__":
    main()
