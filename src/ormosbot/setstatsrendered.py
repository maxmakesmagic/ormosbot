"""Minimal helper for reading a wiki page with Pywikibot."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import bs4
import pywikibot
from mwparserfromhell.nodes.extras.parameter import Parameter
from pywikibot.site import BaseSite

from ormosbot.site import get_site

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
SCRYFALL_STATS_ALIASES = [
    "Template:Scryfall stats",
    "Template:Scryfall_stats",
    "scryfall stats",
    "scryfall_stats",
]
TEMPLATES_TO_CHECK = [
    "Template:Scryfall stats",
    "Template:Scryfall count",
]


try:
    _handle_args = pywikibot.handle_args
except AttributeError:  # Pywikibot < 9 kept camelCase helper
    _handle_args = pywikibot.handleArgs  # type: ignore[attr-defined]


class ScryfallTemplateUsageGenerator:
    """Iterate over pages that transclude a given template."""

    def __init__(
        self,
        *,
        site: BaseSite,
        template_title: str,
        namespaces: Sequence[int] | None = None,
    ) -> None:
        """Create a ScryfallTemplateUsageGenerator."""
        self.site = site
        self.namespaces = namespaces
        self.template_title = template_title

    def __iter__(self) -> Iterator[pywikibot.Page]:
        """Yield pages that include the template."""
        template_page = pywikibot.Page(self.site, self.template_title)
        embedded_pages = template_page.embeddedin(namespaces=self.namespaces)
        yield from embedded_pages


def normalize_template_name(name: str) -> str:
    """Normalize a template name for matching."""
    # Remove the Template: prefix and normalize spaces/underscores and case.
    if name.lower().startswith("template:"):
        name = name[9:]
    return name.strip().lower().replace(" ", "_")


def clean_value(param: Parameter) -> str:
    """Clean a parameter value by stripping."""
    return str(param.value).strip()


def process_page(site: BaseSite, page: pywikibot.Page) -> list[str]:
    """Return all unique Scryfall queries referenced on the page."""
    page_title = str(page.title()).strip()
    pywikibot.info(f"Processing page: {page_title}")

    # Get the fully rendered content of the page (HTML-expanded)
    parsed_page = page.get_parsed_page()

    # Use beautifulsoup to extract all external links from the rendered HTML
    soup = bs4.BeautifulSoup(parsed_page, "html.parser")

    page_queries: set[str] = set()

    for link in soup.find_all("a", href=True):
        url = str(link.attrs["href"])

        if "scryfall.com/search?q=" in url:
            pywikibot.debug(f"MD: url is {url}")
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            pywikibot.debug(f"  query_params: {query_params}")

            if "q" in query_params and "utm_source" not in query_params:
                # skip links that have both q and utm_source (these are likely
                # tracking links, not direct search links)
                search = query_params["q"][0]

                # Skip any searches that don't have colons (:) in them
                if ":" not in search:
                    pywikibot.info(f"  Skipping non-colon search link: {search}")
                    continue

                pywikibot.info(f"  Found Scryfall query: {search}")

                page_queries.add(search)

    return sorted(page_queries)


def register_page_queries(
    page_title: str, page_queries: Iterable[str], queries: dict[str, list[str]]
) -> None:
    """Record each query for the given page in the aggregate mapping."""
    for search in page_queries:
        queries.setdefault(search, []).append(page_title)


def dump_queries_to_file(
    queries: dict[str, list[str]],
    output_file: Path,
) -> None:
    """Dump the collected Scryfall queries to a JSON file."""
    sorted_queries = sorted(queries.keys())
    with output_file.open("w", encoding="utf-8") as f:
        json.dump(sorted_queries, f, indent=2, ensure_ascii=False)
    pywikibot.info(f"Dumped {len(sorted_queries)} queries to {output_file}")

    with output_file.with_suffix(".map").open("w", encoding="utf-8") as f:
        json.dump(queries, f, indent=2, ensure_ascii=False)


def load_revision_cache(path: Path) -> dict[str, dict[str, Any]]:
    """Return cached revision metadata keyed by page title."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pywikibot.warning(f"Failed to parse revision cache at {path}; rebuilding")
    return {}


def dump_revision_cache(cache: dict[str, dict[str, Any]], path: Path) -> None:
    """Persist page revision metadata."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def current_revision_record(
    page: pywikibot.Page,
    rev_id: int | None = None,
    page_queries: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a serializable record for the page's latest revision."""
    revision = page.latest_revision
    timestamp = revision.timestamp.isoformat() if revision else None
    if rev_id is None:
        rev_id = page.latest_revision_id
    record: dict[str, Any] = {"rev_id": rev_id, "timestamp": timestamp}
    if page_queries is not None:
        record["queries"] = list(page_queries)
    return record


def main() -> None:
    """Main entry point for setstatsrendered"""
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
        "--output-file",
        default="scryfall_queries.json",
        help="Path to output JSON file for queries",
    )
    parser.add_argument(
        "--revision-cache",
        default="scryfall_revision_cache.json",
        help="Path to JSON file storing last processed revisions",
    )

    # handle_args strips global Pywikibot flags before argparse sees them
    cli_args = _handle_args()
    args = parser.parse_args(cli_args)
    config_path = Path(args.config)
    output_file = Path(args.output_file)
    revision_cache_path = Path(args.revision_cache)
    revision_cache = load_revision_cache(revision_cache_path)

    site = get_site(config_path, lang=args.site, family=args.family)
    site.login()

    seen_pages: set[str] = set()
    queries: dict[str, list[str]] = {}

    for template_title in TEMPLATES_TO_CHECK:
        pywikibot.info(f"Processing template: {template_title}")
        scryfall_generator = ScryfallTemplateUsageGenerator(
            site=site, template_title=template_title
        )
        for idx, page in enumerate(scryfall_generator):
            page_title = str(page.title())
            if page_title in seen_pages:
                continue
            seen_pages.add(page_title)

            latest_rev_id = page.latest_revision_id
            cached_revision = revision_cache.get(page_title)
            if cached_revision and cached_revision.get("rev_id") == latest_rev_id:
                cached_queries = cached_revision.get("queries")
                if cached_queries is None:
                    pywikibot.info(
                        f"  Cache missing queries for {page_title}; reprocessing"
                    )
                else:
                    pywikibot.info(
                        f"  Skipping unchanged page: {page_title} (rev {latest_rev_id})"
                    )
                    register_page_queries(page_title, cached_queries, queries)
                    continue

            try:
                page_queries = process_page(site, page)
                register_page_queries(page_title, page_queries, queries)
                revision_cache[page_title] = current_revision_record(
                    page, latest_rev_id, page_queries
                )
                if (idx + 1) % 100 == 0:
                    pywikibot.info(f"Processed {idx + 1} pages...")
                    pywikibot.info(f"  Current queries: {len(queries)}")
                    dump_queries_to_file(queries, output_file)
                    dump_revision_cache(revision_cache, revision_cache_path)
            except pywikibot.exceptions.TimeoutError as exc:
                pywikibot.error(f"  TimeoutError processing {page_title}: {exc}")
                continue

    dump_queries_to_file(queries, output_file)
    dump_revision_cache(revision_cache, revision_cache_path)


if __name__ == "__main__":
    main()
