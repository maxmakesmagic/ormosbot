"""Update the Scryfall stats data module and template on the wiki."""

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import pywikibot

from ormosbot.colors import COLOR_ORDER
from ormosbot.site import get_site

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


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
        "<noinclude>{{Documentation}}</noinclude>",
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


def load_stats_file(path: Path) -> dict[str, dict[str, str]]:
    """Load and normalize the stats payload stored at the given path."""
    with path.open("r", encoding="utf-8") as handle:
        payload: Any = json.load(handle)

    stats_obj: Any
    if isinstance(payload, dict) and "stats" in payload:
        stats_obj = payload["stats"]
    else:
        stats_obj = payload

    if not isinstance(stats_obj, dict):
        raise RuntimeError(f"Stats file {path} does not contain a mapping of queries")

    normalized: dict[str, dict[str, str]] = {}
    for query, color_counts in stats_obj.items():
        if not isinstance(query, str):
            raise RuntimeError(
                f"Stats file {path} has a non-string query key: {query!r}"
            )
        if not isinstance(color_counts, dict):
            raise RuntimeError(
                f"Stats file {path} has a non-mapping entry for query {query!r}: {color_counts!r}"
            )
        normalized[query] = {color: str(value) for color, value in color_counts.items()}
    return normalized


def merge_stats_from_dir(stats_dir: Path) -> dict[str, dict[str, str]]:
    """Merge all shard stats JSON files from the provided directory."""
    if not stats_dir.exists():
        raise RuntimeError(f"Stats directory {stats_dir} does not exist")

    json_files = sorted(p for p in stats_dir.rglob("*.json") if p.is_file())
    if not json_files:
        raise RuntimeError(f"No stats JSON files found under {stats_dir}")

    combined: dict[str, dict[str, str]] = {}
    for shard_file in json_files:
        shard_stats = load_stats_file(shard_file)
        overlap = combined.keys() & shard_stats.keys()
        if overlap:
            pywikibot.warning(
                "Stats file %s overlaps %d queries; overwriting with latest data",
                shard_file,
                len(overlap),
            )
        combined.update(shard_stats)
        pywikibot.info(
            "Merged %s containing %d queries (running total: %d)",
            shard_file,
            len(shard_stats),
            len(combined),
        )

    pywikibot.info(
        "Merged %d shard files yielding %d unique queries",
        len(json_files),
        len(combined),
    )
    return combined


def should_update_wiki(env_var: str = "UPDATE_WIKI") -> bool:
    """Return True if the environment variable indicates wiki updates should run."""
    raw_value = os.environ.get(env_var)
    if raw_value is None:
        return False
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


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
        "--stats-dir",
        default="shard-stats",
        help="Directory containing shard JSON files to merge",
    )

    args = parser.parse_args()
    config_path = Path(args.config)
    stats_dir = Path(args.stats_dir)

    logging.basicConfig(
        level=logging.DEBUG, filename="update_module_data.log", filemode="w"
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    stats_mapping = merge_stats_from_dir(stats_dir)
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

    update_wiki = should_update_wiki()
    if update_wiki:
        site = get_site(config_path, lang=args.site, family=args.family)
        site.login()

        page = pywikibot.Page(site, "Template:Scryfall stats/data")
        page.text = switch_code
        page.save("Updated Scryfall stats data via OrmosBot")
        pywikibot.info("Uploaded merged stats to Template:Scryfall stats/data")
    else:
        pywikibot.info(
            "UPDATE_WIKI is not true; skipping wiki edit. Generated outputs locally instead."
        )


if __name__ == "__main__":
    main()
