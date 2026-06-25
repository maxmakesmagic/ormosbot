"""Load existing Scryfall stats from the wiki as a fallback data source.

When Scryfall is unavailable (e.g. a 503 maintenance window) the fetch job would
otherwise default missing counts to 0, which corrupts the module data on the
wiki. To guard against that we load the previously-published #switch template
back from the wiki, parse it into a query -> {color: count} mapping, and reuse
those values when a live Scryfall request fails.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pywikibot

from ormosbot.colors import COLOR_ORDER
from ormosbot.site import get_site

log = logging.getLogger(__name__)

STATS_DATA_PAGE = "Template:Scryfall stats/data"

# Reverse of the escaping applied by update_module_data.escape_switch_case_value.
_SWITCH_UNESCAPES: dict[str, str] = {
    "{{=}}": "=",
    "{{!}}": "|",
}


def unescape_switch_case_value(value: str) -> str:
    """Reverse the #switch case escaping applied when the template is written."""
    result = value
    for placeholder, char in _SWITCH_UNESCAPES.items():
        result = result.replace(placeholder, char)
    return result


def parse_switch_template(text: str) -> dict[str, dict[str, str]]:
    """Parse a Scryfall stats #switch template into a query -> color map.

    Keys are the casefolded (and unescaped) query strings, matching the form
    produced by :func:`ormosbot.update_module_data.switch_from_mapping`.
    """
    mapping: dict[str, dict[str, str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        # Drop the leading "| " switch-case marker.
        body = line[1:].strip()
        if " = " not in body:
            continue
        key, _, csv = body.partition(" = ")
        key = unescape_switch_case_value(key.strip())
        if key in {"default", ""}:
            continue
        parts = [part.strip() for part in csv.split(",")]
        # Stored CSV is one value per color followed by a total; ignore the total
        # and any unexpected trailing values.
        if len(parts) < len(COLOR_ORDER):
            log.warning("Skipping malformed stats line for %r: %s", key, csv)
            continue
        color_counts = {
            color: parts[index] for index, color in enumerate(COLOR_ORDER)
        }
        mapping[key] = color_counts
    return mapping


def load_existing_stats(
    config_path: Path,
    lang: str = "en",
    family: str = "mtg",
) -> dict[str, dict[str, str]]:
    """Load and parse the published stats template from the wiki.

    Returns an empty mapping (never raises) if the page cannot be loaded so that
    callers can treat the fallback as best-effort.
    """
    try:
        site = get_site(config_path, lang=lang, family=family)
        page = pywikibot.Page(site, STATS_DATA_PAGE)
        text = page.text
    except Exception:  # best-effort fallback; never block the fetch on this
        log.warning(
            "Could not load existing stats from %s; falling back to zeros",
            STATS_DATA_PAGE,
            exc_info=True,
        )
        return {}

    if not text:
        log.warning("Existing stats page %s is empty", STATS_DATA_PAGE)
        return {}

    mapping = parse_switch_template(text)
    log.info(
        "Loaded %d existing query stats from %s", len(mapping), STATS_DATA_PAGE
    )
    return mapping
