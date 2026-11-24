"""Publish one JSON page per Scryfall query."""

import argparse
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from pathlib import Path
from time import monotonic

import pywikibot
from pywikibot.page import BasePage
from pywikibot.site import APISite

from ormosbot.colors import COLOR_ORDER
from ormosbot.site import get_site
from ormosbot.update_module_data import (
    DEFAULT_CONFIG_PATH,
    merge_stats_from_dir,
    parse_expected_total,
)
from ormosbot.wiki_stats import STATS_DATA_PAGE, parse_switch_template

PAGE_PREFIX = "OrmosBot/ScryfallStats/"


def normalize_query(query: str) -> str:
    """Trim outer whitespace and lowercase the query used by the Lua lookup."""
    return query.strip(" \t\n\r\v\f").lower()


def query_title(query: str) -> str:
    """Hash the normalized query to get the page title used by the Lua lookup."""
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return f"User:{PAGE_PREFIX}{digest}.json"


@dataclass(frozen=True)
class StatsRecord:
    """A query and its named color counts."""

    query: str
    counts: dict[str, int]

    @property
    def title(self) -> str:
        """Return the page title used by Module:ScryfallStats."""
        return query_title(self.query)

    @property
    def total(self) -> int:
        """Return the sum of the seven counts."""
        return sum(self.counts.values())

    def as_json(self) -> dict[str, str | dict[str, int] | int]:
        """Return the JSON fields for this query."""
        return {"query": self.query, "counts": self.counts, "total": self.total}


@dataclass(frozen=True)
class PendingEdit:
    """A record and the observed revision, if the page exists."""

    record: StatsRecord
    base_revision: int | None


def build_records(
    stats: Mapping[str, Mapping[str, str | int]],
) -> dict[str, StatsRecord]:
    """Validate the counts and index the records by wiki title."""
    records: dict[str, StatsRecord] = {}
    for raw_query in sorted(stats):
        # Discovery gets the query from the lowercased Scryfall URL. Strip the
        # same outer whitespace as Lua and avoid Python's broader case folding.
        query = normalize_query(raw_query)
        raw_counts = stats[raw_query]
        if not query or raw_counts.keys() != set(COLOR_ORDER):
            raise ValueError(
                f"Expected a query and seven color counts for {raw_query!r}"
            )
        counts = {color: int(raw_counts[color]) for color in COLOR_ORDER}
        if any(count < 0 for count in counts.values()):
            raise ValueError(f"Negative Scryfall count for {raw_query!r}")

        record = StatsRecord(query, counts)
        title = record.title
        if title in records and records[title] != record:
            raise ValueError(f"Conflicting Scryfall records for {title}")
        records[title] = record
    if not records:
        raise ValueError("No Scryfall records to publish")
    return records


def read_stored_record(page: BasePage, query: str) -> StatsRecord:
    """Read a wiki record and make sure that it matches the expected query."""
    title = page.title()
    if page.content_model != "json":
        raise ValueError(f"Expected JSON content at {title}")
    stored = json.loads(page.latest_revision.text)
    if not isinstance(stored, dict) or stored.get("query") != query:
        raise ValueError(f"Stored query does not match {query!r} at {title}")
    counts = stored.get("counts")
    total = stored.get("total")
    if (
        not isinstance(counts, dict)
        or counts.keys() != set(COLOR_ORDER)
        or any(type(count) is not int or count < 0 for count in counts.values())
        or type(total) is not int
        or total != sum(counts.values())
    ):
        raise ValueError(f"Invalid stored counts at {title}")
    return StatsRecord(query, counts)


def plan_edits(site: APISite, records: Mapping[str, StatsRecord]) -> list[PendingEdit]:
    """Compare the intended records with the stored pages in batches."""
    current: dict[str, PendingEdit | None] = {}
    for page in site.allpages(prefix=PAGE_PREFIX, namespace=2, content=True):
        title = page.title()
        if title not in records:
            continue
        record = records[title]
        stored = read_stored_record(page, record.query)
        current[title] = (
            None
            if stored.counts == record.counts
            else PendingEdit(record, page.latest_revision.revid)
        )
    edits = []
    for title, record in records.items():
        edit = current.get(title, PendingEdit(record, None))
        if edit is not None:
            edits.append(edit)
    return edits


def publish_edits(site: APISite, edits: list[PendingEdit], summary: str) -> None:
    """Save planned edits with the bot flag and revision conflict protection."""
    if not edits:
        return
    site.login()
    required_rights = ("bot", "editmyuserjson", "createpage")
    if site.user() != "OrmosBot" or not all(
        site.has_right(right) for right in required_rights
    ):
        raise RuntimeError("Log in as OrmosBot with bot and user JSON edit rights")

    edit_count = len(edits)
    first_save: float | None = None
    for index, edit in enumerate(edits, start=1):
        record = edit.record
        preconditions: dict[str, int | bool]
        if edit.base_revision is None:
            preconditions = {"createonly": True}
        else:
            preconditions = {"nocreate": True, "baserevid": edit.base_revision}
        result = site.simple_request(
            action="edit",
            title=record.title,
            text=json.dumps(record.as_json(), ensure_ascii=False),
            contentmodel="json",
            summary=summary,
            token=site.tokens["csrf"],
            assertuser="OrmosBot",
            bot=True,
            watchlist="nochange",
            **preconditions,
        ).submit()
        if result["edit"]["result"] != "Success":
            raise RuntimeError(f"Could not save {record.title}")
        saved_at = monotonic()
        if first_save is None:
            first_save = saved_at
            seconds_per_edit = pywikibot.config.put_throttle
        else:
            seconds_per_edit = (saved_at - first_save) / (index - 1)
        remaining = timedelta(seconds=ceil((edit_count - index) * seconds_per_edit))
        pywikibot.info(
            "Saved %d/%d %s (about %s left)", index, edit_count, record.title, remaining
        )


def main() -> None:
    """Preview or publish JSON records from the wiki or nightly results."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-legacy", action="store_true", help="Read the live wikitext table"
    )
    source.add_argument("--stats-dir", type=Path, help="Read merged nightly stats")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--apply", action="store_true", help="Save the planned edits")
    parser.add_argument("--max-edits", type=int, help="Limit this run to N edits")
    args = parser.parse_args()
    if args.max_edits is not None and args.max_edits < 1:
        parser.error("--max-edits must be at least 1")

    site = get_site(args.config)
    if args.from_legacy:
        source_page = pywikibot.Page(site, STATS_DATA_PAGE)
        legacy_revision = source_page.latest_revision
        text = legacy_revision.text
        if not text or "{{#switch:" not in text:
            raise RuntimeError(f"{STATS_DATA_PAGE} no longer contains the stats table")
        stats = parse_switch_template(text, strict=True)
        summary = f"Seed Scryfall stats from data revision {legacy_revision.revid}"
        pywikibot.info("Read %s revision %d", STATS_DATA_PAGE, legacy_revision.revid)
    else:
        stats = merge_stats_from_dir(args.stats_dir)
        expected = parse_expected_total()
        if expected is not None and len(stats) != expected:
            raise RuntimeError(f"Expected {expected} queries but merged {len(stats)}")
        summary = "Update Scryfall stats via OrmosBot"

    records = build_records(stats)
    edits = plan_edits(site, records)
    creates = sum(edit.base_revision is None for edit in edits)
    pywikibot.info(
        "%d records: %d to create, %d to update, %d already match",
        len(records),
        creates,
        len(edits) - creates,
        len(records) - len(edits),
    )
    planned = edits[: args.max_edits]
    if args.apply:
        publish_edits(site, planned, summary)
    else:
        pywikibot.info("Preview: %d edits in this batch", len(planned))


if __name__ == "__main__":
    main()
