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

log = logging.getLogger(__name__)


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


def fetch_scryfall_stats(session: CachedLimiterSession, query: str) -> dict[str, int]:
    """Fetch stats from Scryfall API"""
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
            stats[color.lower()] = 0
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
                stats[color.lower()] = 0
        else:
            log.error(
                "Error fetching stats for query %s: %s %s",
                full_query,
                response.status_code,
                response.text,
            )
            stats[color.lower()] = 0

    return stats


def update_data_module(
    session: CachedLimiterSession, queries: list[str]
) -> dict[str, dict[str, str]]:
    """Fetch stats for each query and return a mapping ready for serialization."""
    results: dict[str, dict[str, str]] = {}
    for query in tqdm(queries, desc="Updating stats"):
        stats = fetch_scryfall_stats(session, query)
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
    args = parser.parse_args()

    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.DEBUG, filename="logs/fetch_module_stats.log")

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

    session = get_session()
    stats_mapping = update_data_module(session, sorted(set(queries)))

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
