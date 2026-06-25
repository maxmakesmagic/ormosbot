"""Fetch Scryfall stats for a matrix shard of queries."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import pywikibot
import requests
from tenacity import retry
from tqdm import tqdm

from ormosbot.cachedlimiter import CachedLimiterSession, get_session
from ormosbot.colors import COLOR_ORDER
from ormosbot.wiki_stats import load_existing_stats

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


@retry
def scryfall_query(session: CachedLimiterSession, query: str) -> requests.Response:
    """Perform a Scryfall API search query and return the JSON response."""
    log.info("Querying Scryfall API with query: %s", query)

    if "+" in query:
        with session.cache_disabled():
            response = session.get(
                "https://api.scryfall.com/cards/search",
                timeout=10,
                headers={"User-Agent": "OrmosBot/1.0"},
                params={"q": query},
            )
    else:
        response = session.get(
            "https://api.scryfall.com/cards/search",
            timeout=10,
            headers={"User-Agent": "OrmosBot/1.0"},
            params={"q": query},
        )
    return response


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

    session = get_session()
    stats_mapping = update_data_module(
        session, sorted(set(queries)), fallback_stats
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
