"""Tests for page reuse, dependency discovery, and interrupted updates."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pywikibot

from ormosbot import setstatsrendered
from ormosbot.dependency_graph import (
    DependencyGraph,
    DependencyNode,
)


@dataclass
class FakePage:
    """A wiki page with fixed revision and dependency metadata."""

    name: str
    latest_revision_id: int
    included: list["FakePage"] = field(default_factory=list)
    present: bool = True

    def title(self) -> str:
        """Return the wiki title."""
        return self.name

    def templates(self) -> list["FakePage"]:
        """Return the templates expanded on this page."""
        return self.included

    def exists(self) -> bool:
        """Return the page's recorded existence state."""
        return self.present

    @property
    def latest_revision(self) -> SimpleNamespace:
        """Return a fixed revision timestamp."""
        return SimpleNamespace(timestamp=datetime(2026, 11, 11, tzinfo=UTC))


class FakeSite:
    """A site that supplies metadata without a wiki connection."""

    def __init__(self) -> None:
        """Record the page titles supplied in each metadata batch."""
        self.batches: list[list[str]] = []

    def preloadpages(self, pages: Any, **kwargs: Any) -> Any:
        """Yield the supplied pages without fetching page content."""
        assert kwargs.get("content") is False
        batch = list(pages)
        self.batches.append([page.name for page in batch])
        return iter(batch)


def cached_queries(page: FakePage, query: str) -> dict[str, Any]:
    """Build a cached query record from fake page metadata."""
    return setstatsrendered.current_revision_record(
        page, detected_page_queries=[query], page_queries=[query]
    )


def test_dependency_builder_uses_expanded_page_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared helpers are leaves, and generated data never enters the graph."""
    wrapper = FakePage("Template:Wrapper", 1)
    helper = FakePage("Template:Scryfall art", 2)
    documentation = FakePage("Template:Wrapper/doc", 3)
    generated = FakePage("Template:Scryfall stats/data", 4)
    generated_doc = FakePage("Template:Scryfall stats/data/doc", 5)
    missing = FakePage("Template:Missing", 0, present=False)
    wrapper.included = [documentation]
    second = FakePage("Second", 7, [helper])
    first = FakePage(
        "First", 6, [wrapper, helper, generated, generated_doc, missing, second]
    )

    monkeypatch.setattr(
        setstatsrendered,
        "collect_recursive_template_usage",
        lambda *args, **kwargs: iter([first, second]),
    )
    site = FakeSite()

    previous, pages = setstatsrendered.build_page_dependency_graph(site, ["Root"])

    assert pages == [first, second]
    assert dict(previous) == {
        "First": DependencyNode(
            6, frozenset({wrapper.name, helper.name, missing.name, second.name})
        ),
        "Second": DependencyNode(7, frozenset({helper.name})),
        wrapper.name: DependencyNode(1, frozenset()),
        helper.name: DependencyNode(2, frozenset()),
        missing.name: DependencyNode(None, frozenset()),
    }
    assert site.batches == [
        [first.name, second.name],
        [wrapper.name, helper.name, missing.name],
    ]

    generated.latest_revision_id = 8
    generated_doc.latest_revision_id = 9
    current, _ = setstatsrendered.build_page_dependency_graph(site, ["Root"])
    assert previous.invalidated_nodes(current) == set()

    first.included = [wrapper, generated, generated_doc, missing, second]
    current, _ = setstatsrendered.build_page_dependency_graph(site, ["Root"])
    assert previous.invalidated_nodes(current) == {first.name}


def test_only_pages_affected_by_shared_template_change_are_reprocessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared helper refreshes its users while unrelated queries stay cached."""
    first = FakePage("First", 1)
    second = FakePage("Second", 2)
    unrelated = FakePage("Unrelated", 3)
    pages = [first, second, unrelated]
    previous: DependencyGraph = DependencyGraph(
        {
            "First": DependencyNode(1, frozenset({"Template:Shared"})),
            "Second": DependencyNode(2, frozenset({"Template:Shared"})),
            "Unrelated": DependencyNode(3, frozenset({"Template:Other"})),
            "Template:Shared": DependencyNode(4, frozenset()),
            "Template:Other": DependencyNode(5, frozenset()),
        }
    )
    current = DependencyGraph(
        {**previous, "Template:Shared": DependencyNode(6, frozenset())}
    )
    cache = setstatsrendered.RevisionCache(
        pages={page.name: cached_queries(page, "t:old") for page in pages},
        dependency_graph=current,
        pending_pages=previous.invalidated_nodes(current) & {p.name for p in pages},
    )
    processed: list[str] = []

    def process_page(site: Any, page: FakePage) -> tuple[list[str], list[str]]:
        processed.append(page.name)
        return ["t:new"], ["t:new"]

    monkeypatch.setattr(setstatsrendered, "process_page", process_page)

    queries = setstatsrendered.collect_page_queries(FakeSite(), pages, cache)

    assert processed == ["First", "Second"]
    assert queries == {"t:new": ["First", "Second"], "t:old": ["Unrelated"]}
    assert cache.pending_pages == set()


def test_timeout_stays_pending_after_cache_save_and_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A saved new graph cannot make a failed page's old queries reusable."""
    page = FakePage("Page", 1)
    graph = DependencyGraph(
        {
            "Page": DependencyNode(1, frozenset({"Template:Helper"})),
            "Template:Helper": DependencyNode(3, frozenset()),
        }
    )
    old_record = cached_queries(page, "t:old")
    cache = setstatsrendered.RevisionCache(
        pages={page.name: old_record},
        dependency_graph=graph,
        pending_pages={page.name},
    )

    def timeout(site: Any, page: FakePage) -> tuple[list[str], list[str]]:
        raise pywikibot.exceptions.TimeoutError("The wiki request timed out")

    monkeypatch.setattr(setstatsrendered, "process_page", timeout)
    assert setstatsrendered.collect_page_queries(FakeSite(), [page], cache) == {}
    assert cache.pages[page.name] == old_record
    assert cache.pending_pages == {page.name}

    path = tmp_path / "revision-cache.json"
    setstatsrendered.dump_revision_cache(cache, path)
    resumed = setstatsrendered.load_revision_cache(path)
    assert resumed.dependency_graph.invalidated_nodes(graph) == set()
    assert resumed.pending_pages == {page.name}

    monkeypatch.setattr(
        setstatsrendered, "process_page", lambda site, page: (["t:new"], ["t:new"])
    )
    queries = setstatsrendered.collect_page_queries(FakeSite(), [page], resumed)

    assert queries == {"t:new": [page.name]}
    assert resumed.pages[page.name]["queries"] == ["t:new"]
    assert resumed.pending_pages == set()


def test_legacy_cache_requires_a_dependency_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old shared snapshot cannot bypass the initial graph refresh."""
    page = FakePage("Page", 1)
    old_record = cached_queries(page, "t:old")
    old_record["template_dependency_fingerprint"] = '{"Template:Helper":2}'
    path = tmp_path / "revision-cache.json"
    path.write_text(json.dumps({page.name: old_record}), encoding="utf-8")

    cache = setstatsrendered.load_revision_cache(path)
    assert cache.pages == {page.name: old_record}
    assert cache.dependency_graph == DependencyGraph()

    graph = DependencyGraph({page.name: DependencyNode(1, frozenset())})
    cache.pending_pages.update(cache.dependency_graph.invalidated_nodes(graph))
    cache.dependency_graph = graph
    monkeypatch.setattr(
        setstatsrendered, "process_page", lambda site, page: (["t:new"], ["t:new"])
    )

    assert setstatsrendered.collect_page_queries(FakeSite(), [page], cache) == {
        "t:new": [page.name]
    }
    assert "template_dependency_fingerprint" not in cache.pages[page.name]


@pytest.mark.parametrize(
    "pending_state",
    [{}, {"pending_pages": None}, {"pending_pages": "Page"}, {"pending_pages": [1]}],
)
def test_invalid_pending_state_requires_a_dependency_refresh(
    tmp_path: Path, pending_state: dict[str, Any]
) -> None:
    """Missing pending updates cannot make an otherwise valid graph reusable."""
    page = FakePage("Page", 1)
    record = cached_queries(page, "t:old")
    graph = DependencyGraph(
        {
            page.name: DependencyNode(1, frozenset({"Template:Helper"})),
            "Template:Helper": DependencyNode(2, frozenset()),
        }
    )
    stored = {
        "cache_version": setstatsrendered.REVISION_CACHE_VERSION,
        "pages": {page.name: record},
        "dependency_graph": graph.serialize(),
        **pending_state,
    }
    path = tmp_path / "revision-cache.json"
    path.write_text(json.dumps(stored), encoding="utf-8")

    cache = setstatsrendered.load_revision_cache(path)

    assert cache.pages == {page.name: record}
    assert cache.dependency_graph == DependencyGraph()
    assert cache.dependency_graph.invalidated_nodes(graph) == set(graph)


def test_filter_version_change_reuses_detected_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filter update recomputes queries without rendering an unchanged page."""
    page = FakePage("Page", 1)
    record = cached_queries(page, "t:creature")
    record["detected_queries"] = ["angel", "t:creature"]
    record["query_cache_version"] = setstatsrendered.QUERY_CACHE_VERSION - 1
    cache = setstatsrendered.RevisionCache(pages={page.name: record})

    def unexpected_render(site: Any, page: FakePage) -> tuple[list[str], list[str]]:
        pytest.fail("Unchanged detected links must not refetch rendered HTML")

    monkeypatch.setattr(setstatsrendered, "process_page", unexpected_render)

    queries = setstatsrendered.collect_page_queries(FakeSite(), [page], cache)

    assert queries == {"t:creature": [page.name]}
    assert cache.pages[page.name]["query_cache_version"] == (
        setstatsrendered.QUERY_CACHE_VERSION
    )
