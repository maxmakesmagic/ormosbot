"""Helpers for extracting and diagnosing Scryfall queries on wiki pages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlparse

import bs4
import mwparserfromhell

SCRYFALL_TEMPLATE_ALIASES = frozenset(
    {
        "scryfall stats",
        "scryfall_stats",
        "scryfall count",
        "scryfall_count",
    }
)


@dataclass(frozen=True)
class TemplateInspection:
    """A compact view of one Scryfall-related template invocation."""

    name: str
    normalized_name: str
    parameters: list[tuple[str, str]]
    query_like_values: list[str]


def is_structured_scryfall_query(value: str) -> bool:
    """Return whether a value looks like a structured Scryfall query."""
    stripped = value.strip()
    if not stripped:
        return False

    # Structured Scryfall searches commonly use field operators like
    # `t:creature`, `color="WB"`, or range comparisons such as `mv<=3`.
    return any(operator in stripped for operator in (":", "=", "<", ">"))


def normalize_template_name(name: str) -> str:
    """Normalize a template name for matching."""
    # Remove the Template: prefix and normalize spaces/underscores and case.
    if name.lower().startswith("template:"):
        name = name[9:]
    return name.strip().lower().replace("_", " ")


def detect_scryfall_queries_from_html(parsed_page: str) -> list[str]:
    """Detect all direct Scryfall search queries from rendered page HTML."""
    # Use beautifulsoup to extract all external links from the rendered HTML.
    soup = bs4.BeautifulSoup(parsed_page, "html.parser")
    detected_queries: set[str] = set()

    for link in soup.find_all("a", href=True):
        url = str(link.attrs["href"])
        if "scryfall.com/search?q=" not in url:
            continue

        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        # Skip links that have both q and utm_source. These are likely tracking
        # links, not the direct search URLs this pipeline consumes.
        if "q" not in query_params or "utm_source" in query_params:
            continue

        search = query_params["q"][0]
        detected_queries.add(search)

    return sorted(detected_queries)


def filter_structured_scryfall_queries(queries: Iterable[str]) -> list[str]:
    """Filter detected Scryfall queries down to the structured ones the bot uses."""
    return sorted({query for query in queries if is_structured_scryfall_query(query)})


def extract_scryfall_queries_from_html(parsed_page: str) -> list[str]:
    """Extract unique structured Scryfall search queries from rendered page HTML."""
    detected_queries = detect_scryfall_queries_from_html(parsed_page)

    # Skip loose text searches and keep only structured Scryfall queries.
    return filter_structured_scryfall_queries(detected_queries)


def is_query_like_value(value: str) -> bool:
    """Return whether a raw template parameter looks like a Scryfall query."""
    stripped = value.strip()
    if not stripped:
        return False
    if not is_structured_scryfall_query(stripped):
        return False
    if stripped.startswith(("http://", "https://")):
        return False
    return True


def inspect_scryfall_templates_in_wikitext(wikitext: str) -> list[TemplateInspection]:
    """Return Scryfall-related templates and their query-like parameters."""
    wikicode = mwparserfromhell.parse(wikitext)
    inspections: list[TemplateInspection] = []

    for template in wikicode.filter_templates(recursive=True):
        raw_name = str(template.name).strip()
        normalized_name = normalize_template_name(raw_name)
        if normalized_name not in SCRYFALL_TEMPLATE_ALIASES:
            continue

        parameters: list[tuple[str, str]] = []
        query_like_values: list[str] = []
        for parameter in template.params:
            param_name = str(parameter.name).strip()
            value = str(parameter.value).strip()
            parameters.append((param_name, value))
            if is_query_like_value(value):
                query_like_values.append(value)

        inspections.append(
            TemplateInspection(
                name=raw_name,
                normalized_name=normalized_name,
                parameters=parameters,
                query_like_values=query_like_values,
            )
        )

    return inspections


def determine_missing_query_reason(
    inspections: list[TemplateInspection], rendered_queries: list[str]
) -> str:
    """Classify why a page is likely missing a rendered Scryfall query."""
    if rendered_queries:
        return "rendered_queries_found"
    if not inspections:
        return "no_known_scryfall_template_found"
    if any(inspection.query_like_values for inspection in inspections):
        return "template_has_query_like_parameters_but_no_rendered_query"
    return "template_found_but_no_query_like_parameters"


def inspection_to_json_ready(inspection: TemplateInspection) -> dict[str, object]:
    """Convert a template inspection to a JSON-serializable mapping."""
    return asdict(inspection)
