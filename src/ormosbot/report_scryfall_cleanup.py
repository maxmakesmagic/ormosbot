"""List unreferenced Scryfall JSON pages on MTG Wiki."""

import argparse
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from itertools import batched
from pathlib import Path
from typing import Any

import pywikibot
from pywikibot.site import APISite

from ormosbot.publish_scryfall_json import PAGE_PREFIX
from ormosbot.site import get_site
from ormosbot.update_module_data import DEFAULT_CONFIG_PATH

REPORT_TITLE = "User:OrmosBot/ScryfallStats cleanup"
DATA_TITLE = re.compile(rf"User:{re.escape(PAGE_PREFIX)}[0-9a-f]{{16}}\.json")
REFERENCE_PROPERTIES = ("transcludedin", "linkshere", "redirects")
REFERENCE_LIMITS = {"tilimit": "max", "lhlimit": "max", "rdlimit": "max"}


@dataclass(frozen=True)
class CleanupCandidate:
    """A data page and its stored Scryfall query."""

    title: str
    query: str | None


@dataclass
class ScryfallCleanupReport:
    """Read the wiki's reference index and update the report page."""

    site: APISite

    def page_results(self, **params: str | int) -> Iterator[dict[str, Any]]:
        """Read every batch, including extra batches of references to one page."""
        continuation: dict[str, str | int] = {}
        while True:
            result = self.site.simple_request(
                action="query",
                formatversion=2,
                **REFERENCE_LIMITS,
                **params,
                **continuation,
            ).submit()
            if "warnings" in result:
                raise RuntimeError(
                    f"Incomplete Scryfall cleanup scan: {result['warnings']}"
                )
            yield from result.get("query", {}).get("pages", ())
            next_batch = result.get("continue")
            if not next_batch:
                return
            if next_batch == continuation:
                raise RuntimeError("The Scryfall cleanup scan repeated a batch")
            continuation = next_batch

    def find_candidates(self) -> list[CleanupCandidate]:
        """Find production JSON pages and recheck them for new references."""
        data_titles: set[str] = set()
        referenced: set[str] = set()
        reference_props = "|".join(REFERENCE_PROPERTIES)
        for page in self.page_results(
            generator="allpages",
            gapnamespace=2,
            gapprefix=PAGE_PREFIX,
            gaplimit=500,
            prop=f"info|{reference_props}",
        ):
            title = page["title"]
            if page.get("contentmodel") == "json" and DATA_TITLE.fullmatch(title):
                data_titles.add(title)
            if any(page.get(prop) for prop in REFERENCE_PROPERTIES):
                referenced.add(title)

        candidates: dict[str, str | None] = {}
        for batch in batched(sorted(data_titles - referenced), 50):
            seen = set()
            for page in self.page_results(
                titles="|".join(batch),
                prop=f"revisions|{reference_props}",
                rvprop="content|contentmodel",
                rvslots="main",
            ):
                title = page["title"]
                seen.add(title)
                if any(page.get(prop) for prop in REFERENCE_PROPERTIES):
                    referenced.add(title)
                if revisions := page.get("revisions"):
                    slot = revisions[0]["slots"]["main"]
                    if slot["contentmodel"] == "json":
                        record = json.loads(slot["content"])
                        query = (
                            record.get("query") if isinstance(record, dict) else None
                        )
                        candidates[title] = query if isinstance(query, str) else None
            if seen != set(batch):
                raise RuntimeError(
                    "The wiki did not return all Scryfall cleanup candidates"
                )

        return [
            CleanupCandidate(title, candidates[title])
            for title in sorted(candidates.keys() - referenced)
        ]

    def render(self, candidates: list[CleanupCandidate], scanned_at: datetime) -> str:
        """Format the list without adding backlinks to the data pages."""
        lines = [
            f"Last scan at {scanned_at:%Y-%m-%d %H:%M UTC}",
            "",
            f"== Candidates for deletion ({len(candidates)}) ==",
        ]
        if candidates:
            lines.extend(
                [
                    "",
                    "The wiki recorded no incoming links, redirects, or uses of these pages.",
                    "Make sure that the page still has no references before you delete it.",
                    "",
                ]
            )
        for candidate in candidates:
            title = candidate.title
            url = pywikibot.Page(self.site, title).full_url()
            line = f"* [{url} {title}] ([[Special:WhatLinksHere/{title}|references]])"
            if candidate.query:
                query = escape(" ".join(candidate.query.splitlines()), quote=False)
                line += f" · <code><nowiki>{query}</nowiki></code>"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def publish(self, text: str) -> None:
        """Save the report as OrmosBot."""
        self.site.login()
        if self.site.user() != "OrmosBot":
            raise RuntimeError(
                "Log in as OrmosBot to publish the Scryfall cleanup report"
            )
        page = pywikibot.Page(self.site, REPORT_TITLE)
        page.text = text
        page.save(summary="Update Scryfall cleanup candidates", bot=True)


def main() -> None:
    """Scan the wiki and optionally save the cleanup report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--apply", action="store_true", help="Save the report to the wiki"
    )
    args = parser.parse_args()

    report = ScryfallCleanupReport(get_site(args.config))
    candidates = report.find_candidates()
    pywikibot.info("Found %d Scryfall cleanup candidates", len(candidates))
    text = report.render(candidates, datetime.now(UTC))
    if args.apply:
        report.publish(text)
    else:
        pywikibot.info(text)


if __name__ == "__main__":
    main()
