"""Minimal helper for reading a wiki page with Pywikibot."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pywikibot
from pywikibot.site import BaseSite

from ormosbot.scryfall_query_inspection import (
    detect_scryfall_queries_from_html,
    filter_structured_scryfall_queries,
)
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
TEMPLATE_NAMESPACE = 10
# Bump this when raw Scryfall-link detection semantics change and cached
# detected queries should be recomputed from rendered page HTML.
DETECTED_QUERY_CACHE_VERSION = 1
# Bump this when structured-query filtering semantics change. Matching pages can
# then reuse cached detected queries without refetching unchanged wiki pages.
QUERY_CACHE_VERSION = 3


try:
    _handle_args = pywikibot.handle_args
except AttributeError:  # Pywikibot < 9 kept camelCase helper
    _handle_args = pywikibot.handleArgs  # type: ignore[attr-defined]


def collect_recursive_template_usage(
    root_template_titles: Sequence[str],
    transclusion_provider: Callable[[str], Iterable[pywikibot.Page]],
    namespaces: Sequence[int] | None = None,
) -> Iterator[pywikibot.Page]:
    """Yield non-template pages that transitively include the root templates."""
    seen_templates: set[str] = set()
    yielded_pages: set[str] = set()
    pending_templates = deque(root_template_titles)

    while pending_templates:
        template_title = pending_templates.popleft()
        if template_title in seen_templates:
            continue
        seen_templates.add(template_title)
        pywikibot.info(f"Expanding template transclusions for {template_title}")

        for page in transclusion_provider(template_title):
            page_title = str(page.title())
            page_namespace = int(page.namespace())
            if page_namespace == TEMPLATE_NAMESPACE:
                if page_title not in seen_templates:
                    pywikibot.info(
                        f"  Found parent template {page_title} via {template_title}"
                    )
                    pending_templates.append(page_title)
                continue
            if namespaces is not None and page_namespace not in namespaces:
                continue
            if page_title in yielded_pages:
                continue
            yielded_pages.add(page_title)
            pywikibot.debug(
                f"  Yielding page {page_title} from template closure rooted at {template_title}"
            )
            yield page


def collect_recursive_template_titles(
    root_template_titles: Sequence[str],
    transclusion_provider: Callable[[str], Iterable[pywikibot.Page]],
) -> set[str]:
    """Return template titles reachable from the given roots via transclusion."""
    seen_templates: set[str] = set()
    pending_templates = deque(root_template_titles)

    while pending_templates:
        template_title = pending_templates.popleft()
        if template_title in seen_templates:
            continue
        seen_templates.add(template_title)

        for page in transclusion_provider(template_title):
            if int(page.namespace()) != TEMPLATE_NAMESPACE:
                continue
            page_title = str(page.title())
            if page_title not in seen_templates:
                pending_templates.append(page_title)

    return seen_templates


def collect_recursive_scryfall_template_dependencies(
    site: BaseSite,
    root_template_titles: Sequence[str],
) -> set[str]:
    """Return Scryfall template descendants that can affect rendered queries."""
    seen_templates: set[str] = set()
    pending_templates = deque(root_template_titles)

    while pending_templates:
        template_title = pending_templates.popleft()
        if template_title in seen_templates:
            continue
        seen_templates.add(template_title)

        template_page = pywikibot.Page(site, template_title)
        for child_template in template_page.templates(namespaces=[TEMPLATE_NAMESPACE]):
            child_title = str(child_template.title())
            if not child_title.lower().startswith("template:scryfall"):
                continue
            if child_title not in seen_templates:
                pending_templates.append(child_title)

    return seen_templates


def build_template_dependency_fingerprint(
    site: BaseSite,
    root_template_titles: Sequence[str],
) -> str:
    """Build a cache fingerprint for templates that can affect rendered queries."""
    parent_templates = collect_recursive_template_titles(
        root_template_titles,
        lambda title: pywikibot.Page(site, title).embeddedin(),
    )
    scryfall_dependencies = collect_recursive_scryfall_template_dependencies(
        site,
        root_template_titles,
    )
    tracked_templates = sorted(parent_templates | scryfall_dependencies)

    template_revisions = {
        template_title: pywikibot.Page(site, template_title).latest_revision_id
        for template_title in tracked_templates
    }
    return json.dumps(
        template_revisions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ScryfallTemplateUsageGenerator:
    """Iterate over pages that directly or indirectly use Scryfall templates."""

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
        """Yield pages that include the template, following template transclusions."""

        def transclusion_provider(title: str) -> Iterable[pywikibot.Page]:
            template_page = pywikibot.Page(self.site, title)
            yield from template_page.embeddedin()

        yield from collect_recursive_template_usage(
            [self.template_title],
            transclusion_provider,
            namespaces=self.namespaces,
        )


def process_page(site: BaseSite, page: pywikibot.Page) -> tuple[list[str], list[str]]:
    """Return detected and structured Scryfall queries referenced on the page."""
    page_title = str(page.title()).strip()
    pywikibot.info(f"Processing page: {page_title}")

    # Get the fully rendered content of the page (HTML-expanded)
    parsed_page = page.get_parsed_page()
    detected_queries = detect_scryfall_queries_from_html(parsed_page)
    page_queries = filter_structured_scryfall_queries(detected_queries)
    for search in detected_queries:
        pywikibot.debug(f"  Detected Scryfall query: {search}")
    for search in page_queries:
        pywikibot.info(f"  Using structured Scryfall query: {search}")
    return detected_queries, page_queries


def register_page_queries(
    page_title: str, page_queries: Iterable[str], queries: dict[str, list[str]]
) -> None:
    """Record each query for the given page in the aggregate mapping."""
    for search in page_queries:
        queries.setdefault(search, []).append(page_title)


def dump_queries_to_file(
    queries: dict[str, list[str]],
    output_file: Path,
) -> list[str]:
    """Dump the collected Scryfall queries to a JSON file and return them."""
    sorted_queries = sorted(queries.keys())
    with output_file.open("w", encoding="utf-8") as f:
        json.dump(sorted_queries, f, indent=2, ensure_ascii=False)
    pywikibot.info(f"Dumped {len(sorted_queries)} queries to {output_file}")

    with output_file.with_suffix(".map").open("w", encoding="utf-8") as f:
        json.dump(queries, f, indent=2, ensure_ascii=False)

    return sorted_queries


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


def build_chunk_files(
    sorted_queries: Sequence[str],
    chunk_count: int,
    *,
    chunk_dir: Path,
) -> None:
    """Split the queries into evenly sized chunks and write them to disk."""
    chunk_count = max(1, chunk_count)
    query_total = len(sorted_queries)
    chunk_size = math.ceil(query_total / chunk_count) if query_total else 0
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for chunk_index in range(chunk_count):
        if chunk_size:
            start = chunk_index * chunk_size
            end = min(start + chunk_size, query_total)
        else:
            start = 0
            end = 0
        chunk_queries = list(sorted_queries[start:end])
        chunk_file = chunk_dir / f"chunk-{chunk_index}.json"
        chunk_file.write_text(
            json.dumps(chunk_queries, ensure_ascii=False), encoding="utf-8"
        )


def render_matrix_json(chunk_count: int) -> str:
    """Render a minimal matrix JSON listing chunk indexes only."""
    payload = {"chunk_index": list(range(chunk_count))}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def write_github_output_value(key: str, value: str) -> None:
    """Append a single-line value to the GitHub Actions output file."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT environment variable is not set")
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def current_revision_record(
    page: pywikibot.Page,
    rev_id: int | None = None,
    detected_page_queries: Sequence[str] | None = None,
    page_queries: Sequence[str] | None = None,
    template_dependency_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build a serializable record for the page's latest revision."""
    revision = page.latest_revision
    timestamp = revision.timestamp.isoformat() if revision else None
    if rev_id is None:
        rev_id = page.latest_revision_id
    record: dict[str, Any] = {
        "rev_id": rev_id,
        "timestamp": timestamp,
        "detected_query_cache_version": DETECTED_QUERY_CACHE_VERSION,
        "query_cache_version": QUERY_CACHE_VERSION,
    }
    if template_dependency_fingerprint is not None:
        record["template_dependency_fingerprint"] = template_dependency_fingerprint
    if detected_page_queries is not None:
        record["detected_queries"] = list(detected_page_queries)
    if page_queries is not None:
        record["queries"] = list(page_queries)
    return record


def cached_detected_revision_matches(
    cached_revision: dict[str, Any] | None,
    latest_rev_id: int,
    template_dependency_fingerprint: str | None = None,
) -> bool:
    """Return whether cached detected queries can be reused."""
    if not cached_revision:
        return False
    if cached_revision.get("rev_id") != latest_rev_id:
        return False
    if cached_revision.get("detected_query_cache_version") != DETECTED_QUERY_CACHE_VERSION:
        return False
    if template_dependency_fingerprint is not None and cached_revision.get(
        "template_dependency_fingerprint"
    ) != template_dependency_fingerprint:
        return False
    return isinstance(cached_revision.get("detected_queries"), list)


def cached_revision_matches(
    cached_revision: dict[str, Any] | None,
    latest_rev_id: int,
    template_dependency_fingerprint: str | None = None,
) -> bool:
    """Return whether a cached revision record can be fully reused."""
    if not cached_detected_revision_matches(
        cached_revision,
        latest_rev_id,
        template_dependency_fingerprint,
    ):
        return False
    assert cached_revision is not None
    return cached_revision.get("query_cache_version") == QUERY_CACHE_VERSION


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
    parser.add_argument(
        "--matrix-splits",
        type=int,
        default=10,
        help="Number of lists to split queries into for matrix output (default: 10)",
    )
    parser.add_argument(
        "--chunk-dir",
        required=True,
        help="Directory to write per-chunk query files for downstream processing",
    )
    parser.add_argument(
        "--total-output-key",
        help="Emit the total number of queries to $GITHUB_OUTPUT using this key",
    )

    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.DEBUG, filename="logs/setstatsrendered.log")

    # handle_args strips global Pywikibot flags before argparse sees them
    cli_args = _handle_args()
    args = parser.parse_args(cli_args)
    config_path = Path(args.config)
    output_file = Path(args.output_file)
    revision_cache_path = Path(args.revision_cache)
    revision_cache = load_revision_cache(revision_cache_path)

    site = get_site(config_path, lang=args.site, family=args.family)
    site.login()
    template_dependency_fingerprint = build_template_dependency_fingerprint(
        site,
        TEMPLATES_TO_CHECK,
    )

    seen_pages: set[str] = set()
    queries: dict[str, list[str]] = {}

    pywikibot.info(
        "Processing transitive template closure for %s", ", ".join(TEMPLATES_TO_CHECK)
    )
    for idx, page in enumerate(
        collect_recursive_template_usage(
            TEMPLATES_TO_CHECK,
            lambda title: pywikibot.Page(site, title).embeddedin(),
        )
    ):
        page_title = str(page.title())
        if page_title in seen_pages:
            continue
        seen_pages.add(page_title)

        latest_rev_id = page.latest_revision_id
        cached_revision = revision_cache.get(page_title)
        if cached_revision_matches(
            cached_revision,
            latest_rev_id,
            template_dependency_fingerprint,
        ):
            assert cached_revision is not None
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
        elif cached_detected_revision_matches(
            cached_revision,
            latest_rev_id,
            template_dependency_fingerprint,
        ):
            assert cached_revision is not None
            cached_detected_queries = cached_revision.get("detected_queries")
            assert isinstance(cached_detected_queries, list)
            page_queries = filter_structured_scryfall_queries(cached_detected_queries)
            pywikibot.info(
                "  Recomputing structured queries for %s from cached detected links (%s != %s)",
                page_title,
                cached_revision.get("query_cache_version"),
                QUERY_CACHE_VERSION,
            )
            register_page_queries(page_title, page_queries, queries)
            revision_cache[page_title] = current_revision_record(
                page,
                latest_rev_id,
                cached_detected_queries,
                page_queries,
                template_dependency_fingerprint,
            )
            continue
        elif cached_revision and cached_revision.get("rev_id") == latest_rev_id:
            pywikibot.info(
                "  Reprocessing %s due to cache dependency mismatch",
                page_title,
            )

        try:
            detected_queries, page_queries = process_page(site, page)
            register_page_queries(page_title, page_queries, queries)
            revision_cache[page_title] = current_revision_record(
                page,
                latest_rev_id,
                detected_queries,
                page_queries,
                template_dependency_fingerprint,
            )
            if (idx + 1) % 100 == 0:
                pywikibot.info(f"Processed {idx + 1} pages...")
                pywikibot.info(f"  Current queries: {len(queries)}")
                dump_queries_to_file(queries, output_file)
                dump_revision_cache(revision_cache, revision_cache_path)
        except pywikibot.exceptions.TimeoutError as exc:
            pywikibot.error(f"  TimeoutError processing {page_title}: {exc}")
            continue

    sorted_queries = dump_queries_to_file(queries, output_file)
    dump_revision_cache(revision_cache, revision_cache_path)

    if args.total_output_key:
        total_queries = len(sorted_queries)
        write_github_output_value(args.total_output_key, str(total_queries))
        pywikibot.info(
            "Appended total query count (%d) to $GITHUB_OUTPUT with key %s",
            total_queries,
            args.total_output_key,
        )

    chunk_dir = Path(args.chunk_dir)
    build_chunk_files(sorted_queries, args.matrix_splits, chunk_dir=chunk_dir)


if __name__ == "__main__":
    main()
