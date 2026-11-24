"""Tests for publishing per-query Scryfall counts."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ormosbot import publish_scryfall_json
from ormosbot.colors import COLOR_ORDER
from ormosbot.publish_scryfall_json import (
    PendingEdit,
    StatsRecord,
    build_records,
    plan_edits,
    publish_edits,
)
from ormosbot.wiki_stats import parse_switch_template


def _stats(**counts: int) -> dict[str, str]:
    return {color: str(counts.get(color, 0)) for color in COLOR_ORDER}


def _page(title: str, revision: int, data: str) -> Mock:
    page = Mock(content_model="json")
    page.title.return_value = title
    page.latest_revision = SimpleNamespace(revid=revision, text=data)
    return page


def test_build_records_matches_live_unicode_lookup() -> None:
    """Use the exact UTF-8 hash and named counts that the Lua module expects."""
    query = "art:andúril include:extras unique:art"
    title = "User:OrmosBot/ScryfallStats/5533b6167cc54b15.json"
    records = build_records({query: _stats(c=9, w=2, u=3, m=8)})

    assert set(records) == {title}
    assert records[title].as_json() == {
        "query": query,
        "counts": {"c": 9, "w": 2, "u": 3, "b": 0, "r": 0, "g": 0, "m": 8},
        "total": 22,
    }


def test_seed_rejects_an_inconsistent_total() -> None:
    """Abort a backfill if a row disagrees with its total."""
    with pytest.raises(ValueError, match="Inconsistent stats total"):
        parse_switch_template(" | o:x = 1,0,0,0,0,0,0,2", strict=True)


def test_build_records_rejects_a_title_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject two different queries that would write to the same title."""
    monkeypatch.setattr(
        StatsRecord,
        "title",
        property(lambda _: "User:OrmosBot/ScryfallStats/test.json"),
    )

    with pytest.raises(ValueError, match="Conflicting Scryfall records"):
        build_records({"kw:flying": _stats(), "kw:plot": _stats()})


def test_plan_edits_skips_matching_and_unrelated_pages() -> None:
    """Plan a create and a revision-checked update while skipping a no-op."""
    records = build_records(
        {
            "kw:flying": _stats(w=752),
            "kw:plot": _stats(m=5),
            "banned:standardbrawl": _stats(),
        }
    )
    by_query = {record.query: record for record in records.values()}
    unchanged = by_query["kw:flying"]
    update = by_query["kw:plot"]
    create = by_query["banned:standardbrawl"]
    old_data = dict(update.as_json(), counts=dict.fromkeys(COLOR_ORDER, 0), total=0)
    same_counts = dict(unchanged.as_json(), note="Existing page metadata")
    site = Mock()
    site.allpages.return_value = [
        _page(unchanged.title, 41, json.dumps(same_counts, sort_keys=True)),
        _page(update.title, 42, json.dumps(old_data)),
        _page("User:OrmosBot/ScryfallStats/Sandbox/Fixtures/test", 43, "{"),
    ]

    planned = plan_edits(site, records)

    assert {edit.record.query: edit.base_revision for edit in planned} == {
        update.query: 42,
        create.query: None,
    }
    site.simple_request.assert_not_called()


@pytest.mark.parametrize("stored_record", ["{", '{"query":"kw:plot"}'])
def test_plan_edits_rejects_invalid_existing_records(stored_record: str) -> None:
    """Fail before writing when an existing title has invalid or unrelated data."""
    records = build_records({"kw:flying": _stats(w=752)})
    title = next(iter(records))
    site = Mock()
    site.allpages.return_value = [_page(title, 41, stored_record)]

    with pytest.raises(ValueError):
        plan_edits(site, records)

    site.simple_request.assert_not_called()


def test_publish_edits_requires_the_correct_revision_or_a_missing_page() -> None:
    """Send guarded edit requests, including for the zero-count record."""
    records = build_records({"kw:flying": _stats(w=752), "kw:plot": _stats()})
    by_query = {record.query: record for record in records.values()}
    create, update = by_query["kw:plot"], by_query["kw:flying"]
    site = Mock(tokens={"csrf": "test-csrf"})
    site.user.return_value = "OrmosBot"
    site.has_right.return_value = True
    site.simple_request.return_value.submit.return_value = {
        "edit": {"result": "Success", "newrevid": 43}
    }

    publish_edits(site, [PendingEdit(create, None), PendingEdit(update, 42)], "Test")

    create_request, update_request = [
        call.kwargs for call in site.simple_request.call_args_list
    ]
    for request, record in [(create_request, create), (update_request, update)]:
        assert request["action"] == "edit"
        assert request["title"] == record.title
        assert request["assertuser"] == "OrmosBot"
        assert request["bot"] is True
        assert request["summary"] == "Test"
        assert json.loads(request["text"]) == record.as_json()
    assert create_request["createonly"] is True
    assert "baserevid" not in create_request
    assert update_request["nocreate"] is True
    assert update_request["baserevid"] == 42
    assert "basetimestamp" not in update_request


def test_publish_edits_checks_the_wiki_account() -> None:
    """Refuse live writes under an unexpected account."""
    record = next(iter(build_records({"kw:flying": _stats()}).values()))
    site = Mock()
    site.user.return_value = "UnexpectedAccount"

    with pytest.raises(RuntimeError):
        publish_edits(site, [PendingEdit(record, None)], "Test")

    site.simple_request.assert_not_called()


def test_publish_estimates_time_left_from_successful_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the configured interval first, then the observed time between saves."""
    records = build_records({query: _stats() for query in ("t:cat", "t:dog", "t:rat")})
    site = Mock(tokens={"csrf": "test-csrf"})
    site.user.return_value = "OrmosBot"
    site.has_right.return_value = True
    site.simple_request.return_value.submit.return_value = {
        "edit": {"result": "Success"}
    }
    report = Mock()
    monkeypatch.setattr(publish_scryfall_json.pywikibot, "info", report)
    monkeypatch.setattr(publish_scryfall_json.pywikibot.config, "put_throttle", 5)
    monkeypatch.setattr(
        publish_scryfall_json, "monotonic", iter((1.0, 7.5, 14.5)).__next__
    )

    publish_edits(
        site, [PendingEdit(record, None) for record in records.values()], "Test"
    )

    assert [call.args[-1] for call in report.call_args_list] == [
        timedelta(seconds=10),
        timedelta(seconds=7),
        timedelta(0),
    ]
