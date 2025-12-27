"""Update the Scryfall stats data module and template on the wiki."""

import argparse
import json
import logging
from pathlib import Path

import pywikibot
import requests
from tenacity import retry
from tqdm import tqdm

from ormosbot.cachedlimiter import CachedLimiterSession, get_session
from ormosbot.site import get_site

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"
COLOR_ORDER = ["c", "w", "u", "b", "r", "g", "m"]


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


def lua_from_mapping(data: dict[str, dict[str, str]]) -> str:
    """Render the stats mapping into Lua source code."""
    lines = ["-- Auto-generated data. Edit carefully.", "return {"]
    for query, stats in data.items():
        lines.append(f"    ['{query}'] = {{")
        color_chunks = [f"{color} = {stats.get(color, '0')}" for color in COLOR_ORDER]
        lines.append("        " + ", ".join(color_chunks))
        lines.append("    },")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def switch_from_mapping(data: dict[str, dict[str, str]]) -> str:
    """Render the stats mapping into a wikitext #switch helper template."""
    lines = [
        "<noinclude>Auto-generated data. Edit carefully.</noinclude>",
        "{{#switch:{{lc:{{{query|}}}}}",
    ]
    for query, stats in data.items():
        normalized = query.casefold()
        values: list[int] = []
        for color in COLOR_ORDER:
            value = int(stats.get(color, "0"))
            values.append(int(value))

        total = sum(values)
        csv_values = [str(v) for v in values]
        csv_values.append(str(total))
        csv_value_str = ",".join(csv_values)
        lines.append(f" | {normalized} = {csv_value_str}")
    lines.append(" | default = ")
    lines.append("}}")
    return "\n".join(lines)


def main() -> None:
    """Main entry point for update-module-data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="en", help="Project language code")
    parser.add_argument(
        "--family",
        default="mtg",
        help="Pywikibot family key (default: mtg)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to config.json providing HTTP headers",
    )
    parser.add_argument(
        "--input-file",
        default="scryfall_queries.json",
        help="Path to input JSON file for queries",
    )

    args = parser.parse_args()
    config_path = Path(args.config)

    site = get_site(config_path, lang=args.site, family=args.family)
    site.login()

    logging.basicConfig(
        level=logging.DEBUG, filename="update_module_data.log", filemode="w"
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Load the queries from the input file
    input_file = Path(args.input_file)
    with input_file.open("r", encoding="utf-8") as f:
        queries = set(json.load(f))
    pywikibot.info(f"Loaded {len(queries)} queries from {input_file}")

    session = get_session()

    stats_mapping = update_data_module(session, sorted(queries))
    lua_code = lua_from_mapping(stats_mapping)

    # Write the lua code to file
    output_path = "ScryfallStats_data.lua"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(lua_code)
    pywikibot.info(f"Wrote Lua data module to {output_path}")

    switch_code = switch_from_mapping(stats_mapping)
    switch_path = "giantswitch.txt"
    with open(switch_path, "w", encoding="utf-8") as f:
        f.write(switch_code)
    pywikibot.info(f"Wrote switch template data to {switch_path}")

    # Save to wiki
    page = pywikibot.Page(site, "Template:Scryfall stats/data")
    page.text = switch_code
    page.save("Updated Scryfall stats data via OrmosBot")


if __name__ == "__main__":
    main()
