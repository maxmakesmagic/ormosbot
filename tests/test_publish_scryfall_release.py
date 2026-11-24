"""Tests for planning wiki edits from the last published data release."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ormosbot import publish_scryfall_release as publisher
from ormosbot.colors import COLOR_ORDER
from ormosbot.publish_scryfall_json import PendingEdit, StatsRecord, query_title
from ormosbot.publish_scryfall_release import Snapshot, load_results, plan_release


def _counts(**counts: int) -> dict[str, int]:
    return {color: counts.get(color, 0) for color in COLOR_ORDER}


def _page(query: str, revision: int, **counts: int) -> Mock:
    record = StatsRecord(query, _counts(**counts))
    page = Mock(pageid=1, content_model="json")
    page.title.return_value = record.title
    page.latest_revision = SimpleNamespace(
        revid=revision, text=json.dumps(record.as_json())
    )
    return page


def _wiki(monkeypatch: pytest.MonkeyPatch, pages: dict[str, Mock]) -> Mock:
    site = Mock()
    monkeypatch.setattr(publisher.pywikibot, "Page", lambda _, title: pages[title])
    site.preloadpages.side_effect = lambda requested, **_: iter(requested)
    return site


def test_release_diff_skips_unchanged_pages_and_defers_incomplete_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read only new or changed pages, even when a color change keeps the total."""
    baseline = Snapshot(
        {
            "t:cat": StatsRecord("t:cat", _counts(w=2)),
            "t:dog": StatsRecord("t:dog", _counts(w=3)),
            "t:rat": StatsRecord("t:rat", _counts(w=4)),
        },
        set(),
    )
    missing = Mock(pageid=0)
    missing.title.return_value = query_title("t:imp")
    site = _wiki(
        monkeypatch,
        {query_title("t:dog"): _page("t:dog", 35, w=3), query_title("t:imp"): missing},
    )
    latest = {
        "t:cat": dict(_counts(w=2), w=None),
        "t:dog": _counts(w=2, u=1),
        "t:imp": _counts(),
        "t:orc": dict(_counts(), b=None),
    }

    plan = plan_release(site, baseline, latest, set())

    assert plan.edits == [
        PendingEdit(StatsRecord("t:dog", _counts(w=2, u=1)), 35),
        PendingEdit(StatsRecord("t:imp", _counts()), None),
    ]
    assert plan.snapshot.planned_queries == {"t:dog", "t:imp"}
    assert plan.snapshot.records["t:cat"] == baseline.records["t:cat"]
    assert plan.snapshot.records["t:rat"] == baseline.records["t:rat"]
    assert "t:orc" not in plan.snapshot.records and plan.deferred == {"t:orc"}


def test_release_diff_does_not_read_the_wiki_when_counts_match() -> None:
    """A normal no-op run uses only the two snapshots."""
    record = StatsRecord("t:cat", _counts(w=2))
    site = Mock()

    plan = plan_release(
        site,
        Snapshot({record.query: record}, set()),
        {record.query: dict(record.counts)},
        set(),
    )

    assert plan.edits == []
    assert plan.snapshot.records == {record.query: record}
    site.preloadpages.assert_not_called()


def test_reconcile_partial_write_and_prefer_newer_live_count_for_failed_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read pending titles even when new results equal the last released counts."""
    baseline = Snapshot(
        {
            "t:cat": StatsRecord("t:cat", _counts(w=7)),
            "t:dog": StatsRecord("t:dog", _counts(w=4)),
        },
        set(),
    )
    site = _wiki(
        monkeypatch,
        {
            query_title("t:cat"): _page("t:cat", 40, w=9),
            query_title("t:dog"): _page("t:dog", 41, w=8),
        },
    )

    plan = plan_release(
        site,
        baseline,
        {"t:cat": _counts(w=7), "t:dog": dict(_counts(u=2), w=None)},
        {"t:cat", "t:dog"},
    )

    assert plan.edits == [
        PendingEdit(StatsRecord("t:cat", _counts(w=7)), 40),
        PendingEdit(StatsRecord("t:dog", _counts(w=8, u=2)), 41),
    ]


def test_load_results_rejects_the_wrong_query_set(tmp_path: Path) -> None:
    """Catch a missing query even when the artifact contains the expected count."""
    queries = tmp_path / "queries.json"
    queries.write_text('["t:cat"]', encoding="utf-8")
    (tmp_path / "chunk-0.json").write_text(
        json.dumps({"chunk_index": 0, "query_count": 1, "stats": {"t:dog": _counts()}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unexpected or repeated result"):
        load_results(tmp_path, queries)


def test_failed_write_leaves_draft_available_to_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the plan durable before any edits and publish only after they succeed."""
    baseline = tmp_path / "previous.json"
    record = StatsRecord("t:cat", _counts(w=7))
    Snapshot({record.query: record}, set()).save(baseline)
    queries = tmp_path / "queries.json"
    queries.write_text('["t:cat"]', encoding="utf-8")
    (tmp_path / "chunk-0.json").write_text(
        json.dumps(
            {"chunk_index": 0, "query_count": 1, "stats": {"t:cat": _counts(w=9)}}
        ),
        encoding="utf-8",
    )
    output = tmp_path / "scryfall-stats.json"
    actions: list[str] = []
    store = Mock()
    store.pending_tags.return_value = []
    store.create_draft.side_effect = lambda *_: actions.append("draft")
    store.download_snapshot.side_effect = lambda _, path: path.write_bytes(
        output.read_bytes()
    )
    monkeypatch.setattr(publisher, "ReleaseStore", lambda _: store)
    monkeypatch.setattr(
        publisher,
        "get_site",
        lambda _: _wiki(monkeypatch, {query_title("t:cat"): _page("t:cat", 40, w=7)}),
    )

    def fail_to_save(*_args: object) -> None:
        actions.append("edit")
        raise RuntimeError("edit conflict")

    monkeypatch.setattr(publisher, "publish_edits", fail_to_save)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish-scryfall-release",
            "--baseline",
            str(baseline),
            "--baseline-tag",
            "scryfall-stats-data-0.0",
            "--stats-dir",
            str(tmp_path),
            "--queries",
            str(queries),
            "--output",
            str(output),
            "--repo",
            "example/ormosbot",
            "--tag",
            "scryfall-stats-data-1",
            "--commit",
            "fake-sha",
            "--apply",
        ],
    )

    with pytest.raises(RuntimeError, match="edit conflict"):
        publisher.main()

    assert actions == ["draft", "edit"]
    assert Snapshot.load(output).planned_queries == {"t:cat"}
    store.publish.assert_not_called()
