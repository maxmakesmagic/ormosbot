"""Publish changed Scryfall query pages and save the combined data in a release."""

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pywikibot
from pywikibot.site import APISite

from ormosbot.colors import COLOR_ORDER
from ormosbot.publish_scryfall_json import (
    PendingEdit,
    StatsRecord,
    build_records,
    normalize_query,
    publish_edits,
    query_title,
    read_stored_record,
)
from ormosbot.scryfall_releases import SNAPSHOT_ASSET, ReleaseStore, release_version
from ormosbot.site import get_site
from ormosbot.update_module_data import DEFAULT_CONFIG_PATH

type ColorResults = dict[str, int | None]


@dataclass(frozen=True)
class Snapshot:
    """The complete counts and the queries considered for wiki edits."""

    records: dict[str, StatsRecord]
    planned_queries: set[str]

    @classmethod
    def load(cls, path: Path) -> "Snapshot":
        """Read and validate a combined snapshot from an earlier release."""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError(f"Unsupported Scryfall snapshot format in {path}")
        records = data.get("queries")
        planned = data.get("planned_queries")
        if not isinstance(records, dict) or not isinstance(planned, list):
            raise ValueError(f"Missing Scryfall snapshot fields in {path}")
        for query, counts in records.items():
            if not query or normalize_query(query) != query:
                raise ValueError(f"Invalid snapshot query {query!r}")
            if not isinstance(counts, dict) or counts.keys() != set(COLOR_ORDER):
                raise ValueError(f"Missing snapshot counts for {query!r}")
            if any(type(count) is not int or count < 0 for count in counts.values()):
                raise ValueError(f"Invalid snapshot counts for {query!r}")
        if any(
            not isinstance(query, str) or not query or normalize_query(query) != query
            for query in planned
        ):
            raise ValueError(f"Invalid planned queries in {path}")
        verified = build_records(records)
        return cls({record.query: record for record in verified.values()}, set(planned))

    def save(self, path: Path) -> None:
        """Write the full snapshot to one JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "queries": {query: record.counts for query, record in self.records.items()},
            "planned_queries": sorted(self.planned_queries),
        }
        path.write_text(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n",
            encoding="utf-8",
        )


@dataclass(frozen=True)
class ReleasePlan:
    """The next snapshot, wiki edits, and new queries with incomplete counts."""

    snapshot: Snapshot
    edits: list[PendingEdit]
    deferred: set[str]


def load_results(stats_dir: Path, queries_file: Path) -> dict[str, ColorResults]:
    """Read the nightly shards and require exactly the discovered queries."""
    queries = json.loads(queries_file.read_text(encoding="utf-8"))
    if (
        not isinstance(queries, list)
        or not queries
        or any(
            not isinstance(query, str) or not normalize_query(query)
            for query in queries
        )
    ):
        raise ValueError("Expected a nonempty list of discovered Scryfall queries")
    expected = set(queries)
    if len(expected) != len(queries):
        raise ValueError("Duplicate query in the discovery list")

    results: dict[str, ColorResults] = {}
    seen_queries: set[str] = set()
    seen_shards: set[int] = set()
    for path in sorted(stats_dir.glob("chunk-*.json")):
        shard = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(shard, dict):
            raise ValueError(f"Invalid Scryfall shard in {path}")
        index = shard.get("chunk_index")
        query_count = shard.get("query_count")
        stats = shard.get("stats")
        if (
            type(index) is not int
            or index < 0
            or index in seen_shards
            or path.name != f"chunk-{index}.json"
            or not isinstance(stats, dict)
            or type(query_count) is not int
            or query_count != len(stats)
        ):
            raise ValueError(f"Invalid Scryfall shard in {path}")
        seen_shards.add(index)
        for raw_query, counts in stats.items():
            if raw_query not in expected or raw_query in seen_queries:
                raise ValueError(f"Unexpected or repeated result for {raw_query!r}")
            seen_queries.add(raw_query)
            if not isinstance(counts, dict) or counts.keys() != set(COLOR_ORDER):
                raise ValueError(f"Expected seven color results for {raw_query!r}")
            query = normalize_query(raw_query)
            merged = results.setdefault(query, dict.fromkeys(COLOR_ORDER))
            for color, count in counts.items():
                if count is None:
                    continue
                if type(count) is not int or count < 0:
                    raise ValueError(f"Invalid result for {raw_query!r} color {color}")
                previous = merged[color]
                if previous is not None and previous != count:
                    raise ValueError(f"Conflicting results for {query!r} color {color}")
                merged[color] = count
    if seen_queries != expected:
        missing = sorted(expected - seen_queries)
        raise ValueError(
            f"Missing {len(missing)} Scryfall results, including {missing[:5]!r}"
        )
    return results


def resolve_counts(
    query: str, results: ColorResults, previous: StatsRecord | None
) -> StatsRecord | None:
    """Use a stored count when a lookup fails, if a previous record exists."""
    counts: dict[str, int] = {}
    for color in COLOR_ORDER:
        count = results[color]
        if count is None:
            if previous is None:
                return None
            count = previous.counts[color]
        counts[color] = count
    return StatsRecord(query, counts)


def plan_release(
    site: APISite,
    baseline: Snapshot,
    results: Mapping[str, ColorResults],
    pending: set[str],
) -> ReleasePlan:
    """Diff the two snapshots and read only the affected wiki pages."""
    desired = baseline.records.copy()
    deferred: set[str] = set()
    for query, colors in results.items():
        record = resolve_counts(query, colors, baseline.records.get(query))
        if record is None:
            deferred.add(query)
        else:
            desired[query] = record

    targets = {
        query
        for query, record in desired.items()
        if record != baseline.records.get(query)
    } | pending
    all_queries = baseline.records.keys() | results.keys() | pending
    titles = {query_title(query): query for query in all_queries}
    if len(titles) != len(all_queries):
        raise ValueError("Scryfall query hashes collide")
    target_titles = {query_title(query): query for query in sorted(targets)}
    edits: list[PendingEdit] = []
    if targets:
        seen: set[str] = set()
        pages = (pywikibot.Page(site, title) for title in target_titles)
        for page in site.preloadpages(pages, groupsize=50):
            query = target_titles[page.title()]
            seen.add(query)
            current = read_stored_record(page, query) if page.pageid else None
            if query in results:
                # A partial publish can leave newer valid counts in the wiki.
                record = resolve_counts(
                    query, results[query], current or baseline.records.get(query)
                )
            else:
                record = current or baseline.records.get(query)
            if record is None:
                continue
            deferred.discard(query)
            desired[query] = record
            if current != record:
                revision = page.latest_revision.revid if current is not None else None
                edits.append(PendingEdit(record, revision))
        if seen != targets:
            raise RuntimeError("The wiki did not return all requested Scryfall pages")
    return ReleasePlan(Snapshot(desired, targets), edits, deferred)


def main() -> None:
    """Plan or publish the nightly Scryfall stats release."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--baseline-tag", required=True)
    parser.add_argument("--stats-dir", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--put-throttle",
        type=int,
        metavar="SECONDS",
        help="Set the minimum interval between wiki edits",
    )
    args = parser.parse_args()
    if args.put_throttle is not None:
        if args.put_throttle < 1:
            parser.error("--put-throttle must be at least one second")
        pywikibot.config.put_throttle = args.put_throttle

    version = release_version(args.tag)
    if args.apply and (
        version == 0 or os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1"
    ):
        raise ValueError("Start a new workflow run to publish fresh Scryfall counts")

    baseline = Snapshot.load(args.baseline)
    results = load_results(args.stats_dir, args.queries)
    store = ReleaseStore(args.repo)
    pending_tags = store.pending_tags(args.baseline_tag)
    if args.apply and any(
        version <= release_version(tag) for tag in [args.baseline_tag, *pending_tags]
    ):
        raise ValueError(
            "This workflow run is older than the publishing state. Run the full workflow again."
        )

    pending: set[str] = set()
    with TemporaryDirectory(prefix="scryfall-release-") as tempdir:
        for tag in pending_tags:
            path = Path(tempdir) / f"{tag}.json"
            store.download_snapshot(tag, path)
            pending.update(Snapshot.load(path).planned_queries)

    site = get_site(args.config)
    plan = plan_release(site, baseline, results, pending)
    plan.snapshot.save(args.output)
    creates = sum(edit.base_revision is None for edit in plan.edits)
    pywikibot.info(
        "%d queries in snapshot, %d checked on wiki, %d to create, "
        "%d to update, %d deferred",
        len(plan.snapshot.records),
        len(plan.snapshot.planned_queries),
        creates,
        len(plan.edits) - creates,
        len(plan.deferred),
    )
    for query in sorted(plan.deferred):
        pywikibot.warning(
            "Deferred new Scryfall query %r because counts are missing", query
        )
    if not args.apply:
        pywikibot.info("Preview complete")
        return
    if not plan.edits and not pending and plan.snapshot.records == baseline.records:
        pywikibot.info("The existing release already matches the data")
        return

    # Save the full plan before the first edit so a later run can check every
    # affected query even if its newly gathered counts match the old release.
    store.create_draft(args.tag, args.output, args.commit)
    with TemporaryDirectory(prefix="scryfall-release-verify-") as tempdir:
        staged = Path(tempdir) / SNAPSHOT_ASSET
        store.download_snapshot(args.tag, staged)
        if staged.read_bytes() != args.output.read_bytes():
            raise RuntimeError(
                "The staged release snapshot does not match the edit plan"
            )

    publish_edits(site, plan.edits, f"Update Scryfall stats for {args.tag}")
    store.publish(args.tag)
    pywikibot.info("Published Scryfall data release %s", args.tag)


if __name__ == "__main__":
    main()
