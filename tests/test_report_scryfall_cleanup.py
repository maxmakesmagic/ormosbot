"""Tests for the Scryfall cleanup report."""

import json
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from ormosbot import report_scryfall_cleanup
from ormosbot.report_scryfall_cleanup import CleanupCandidate, ScryfallCleanupReport

DATA_ROOT = "User:OrmosBot/ScryfallStats/"


def _record(title: str, query: str) -> dict[str, object]:
    return {
        "title": title,
        "revisions": [
            {
                "slots": {
                    "main": {
                        "contentmodel": "json",
                        "content": json.dumps({"query": query}),
                    },
                },
            },
        ],
    }


def test_find_candidates_respects_all_batches_and_rechecks() -> None:
    """References found later exclude pages from the report."""
    candidate = f"{DATA_ROOT}{'a' * 16}.json"
    transcluded = f"{DATA_ROOT}{'b' * 16}.json"
    rechecked = f"{DATA_ROOT}{'c' * 16}.json"
    sandbox = f"{DATA_ROOT}Sandbox/{'d' * 16}.json"
    wikitext = f"{DATA_ROOT}{'e' * 16}.json"
    inventory_next = {"continue": "gapcontinue||", "ticontinue": "2|17"}
    recheck_next = {"continue": "||", "lhcontinue": "2|15"}
    site = Mock()
    site.simple_request.return_value.submit.side_effect = [
        {
            "query": {
                "pages": [
                    {"title": title, "contentmodel": "json"}
                    for title in (candidate, transcluded, rechecked, sandbox)
                ]
                + [{"title": wikitext, "contentmodel": "wikitext"}],
            },
            "continue": inventory_next,
        },
        {
            "query": {
                "pages": [{"title": transcluded, "transcludedin": [{"pageid": 17}]}]
            }
        },
        {
            "query": {
                "pages": [_record(candidate, "t:cat"), _record(rechecked, "t:dog")]
            },
            "continue": recheck_next,
        },
        {"query": {"pages": [{"title": rechecked, "linkshere": [{"pageid": 15}]}]}},
    ]

    assert ScryfallCleanupReport(site).find_candidates() == [
        CleanupCandidate(candidate, "t:cat"),
    ]

    first, second, third, fourth = [
        call.kwargs for call in site.simple_request.call_args_list
    ]
    assert first["generator"] == "allpages"
    assert second == first | inventory_next
    assert third["titles"] == f"{candidate}|{rechecked}"
    assert fourth == third | recheck_next


def test_report_renders_candidate_count_and_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make data pages clickable without adding internal links to them."""
    title = f"{DATA_ROOT}{'a' * 16}.json"
    url = f"https://mtg.wiki/page/{title}"
    page = Mock()
    page.full_url.return_value = url
    monkeypatch.setattr(
        report_scryfall_cleanup.pywikibot, "Page", Mock(return_value=page)
    )
    report = ScryfallCleanupReport(Mock())
    scanned_at = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)

    text = report.render([CleanupCandidate(title, "a & [[x]] </nowiki>")], scanned_at)

    assert "Last scan at 2026-11-25 15:00 UTC" in text
    assert "== Candidates for deletion (1) ==" in text
    assert f"[{url} {title}]" in text
    assert f"[[Special:WhatLinksHere/{title}|references]]" in text
    assert f"[[{title}" not in text
    assert "<nowiki>a &amp; [[x]] &lt;/nowiki&gt;</nowiki>" in text
    assert "== Candidates for deletion (0) ==" in report.render([], scanned_at)


def test_publish_uses_the_exact_report_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Save the report only to the requested page as OrmosBot."""
    site = Mock()
    site.user.return_value = "OrmosBot"
    make_page = Mock()
    monkeypatch.setattr(report_scryfall_cleanup.pywikibot, "Page", make_page)

    text = "== Candidates for deletion (0) ==\n"
    ScryfallCleanupReport(site).publish(text)

    site.login.assert_called_once_with()
    make_page.assert_called_once_with(site, "User:OrmosBot/ScryfallStats cleanup")
    page = make_page.return_value
    assert page.text == text
    page.save.assert_called_once()
