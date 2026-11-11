"""Tests for dependency changes and cached graph persistence."""

import json
from typing import Any

import pytest

from ormosbot.dependency_graph import (
    DependencyGraph,
    DependencyNode,
)


def _node(rev_id: int | None, *dependencies: str) -> DependencyNode:
    """Build a node for a test graph."""
    return DependencyNode(rev_id, frozenset(dependencies))


def test_graph_supports_mapping_access() -> None:
    """Callers can read, update, and remove nodes through the mapping API."""
    graph = DependencyGraph()
    helper = _node(1)
    graph["Template:Helper"] = helper
    graph.update({"Page": _node(2, "Template:Helper")})

    assert graph["Template:Helper"] == helper
    assert list(graph) == ["Template:Helper", "Page"]
    assert len(graph) == 2
    assert graph.get("Missing") is None
    assert dict(graph) == {
        "Template:Helper": helper,
        "Page": _node(2, "Template:Helper"),
    }

    del graph["Page"]
    del graph["Template:Helper"]
    assert not graph
    with pytest.raises(KeyError):
        graph["Missing"]


class TestInvalidatedNodes:
    """Tests for changes that propagate to dependent pages."""

    def test_unchanged_graph_has_no_invalidations(self) -> None:
        """Stable revisions and dependency membership preserve cached pages."""
        previous = DependencyGraph({"Page": _node(1, "Template:A", "Template:B")})
        current = DependencyGraph({"Page": _node(1, "Template:B", "Template:A")})

        assert previous.invalidated_nodes(current) == set()

    def test_nested_shared_dependency_leaves_unrelated_pages_unchanged(self) -> None:
        """A shared helper change reaches its pages through nested templates."""
        previous = DependencyGraph(
            {
                "Template:Helper": _node(1),
                "Template:Wrapper": _node(2, "Template:Helper"),
                "First page": _node(3, "Template:Wrapper"),
                "Second page": _node(4, "Template:Helper"),
                "Template:Unrelated": _node(5),
                "Unrelated page": _node(6, "Template:Unrelated"),
            }
        )
        current = DependencyGraph({**previous, "Template:Helper": _node(7)})

        assert previous.invalidated_nodes(current) == {
            "Template:Helper",
            "Template:Wrapper",
            "First page",
            "Second page",
        }

    def test_parent_change_does_not_invalidate_its_dependencies(self) -> None:
        """Changes propagate toward dependent pages rather than their helpers."""
        previous = DependencyGraph(
            {
                "Template:Helper": _node(1),
                "Page": _node(2, "Template:Helper"),
            }
        )
        current = DependencyGraph({**previous, "Page": _node(3, "Template:Helper")})

        assert previous.invalidated_nodes(current) == {"Page"}

    @pytest.mark.parametrize("dependencies", [(), ("Template:New",)])
    def test_dependency_membership_change_invalidates_dependents(
        self, dependencies: tuple[str, ...]
    ) -> None:
        """Removing or replacing a dependency reaches unchanged parent pages."""
        previous = DependencyGraph(
            {
                "Template:Old": _node(1),
                "Template:New": _node(2),
                "Template:Wrapper": _node(3, "Template:Old"),
                "Page": _node(4, "Template:Wrapper"),
            }
        )
        current = DependencyGraph(
            {**previous, "Template:Wrapper": _node(3, *dependencies)}
        )

        assert previous.invalidated_nodes(current) == {"Template:Wrapper", "Page"}

    def test_added_dependency_invalidates_dependents(self) -> None:
        """Adding a dependency changes the node even without a revision change."""
        previous = DependencyGraph(
            {
                "Template:Helper": _node(1),
                "Template:Wrapper": _node(2),
                "Page": _node(3, "Template:Wrapper"),
            }
        )
        current = DependencyGraph(
            {
                **previous,
                "Template:Wrapper": _node(2, "Template:Helper"),
            }
        )

        assert previous.invalidated_nodes(current) == {"Template:Wrapper", "Page"}

    @pytest.mark.parametrize("added", [False, True])
    def test_added_or_removed_node_invalidates_its_dependents(
        self, added: bool
    ) -> None:
        """Creating or deleting a template reaches pages that reference it."""
        without_helper = DependencyGraph(
            {
                "Template:Wrapper": _node(2, "Template:Helper"),
                "Page": _node(3, "Template:Wrapper"),
            }
        )
        with_helper = DependencyGraph({**without_helper, "Template:Helper": _node(1)})
        previous, current = (
            (without_helper, with_helper) if added else (with_helper, without_helper)
        )

        assert previous.invalidated_nodes(current) == {
            "Template:Helper",
            "Template:Wrapper",
            "Page",
        }

    def test_cycles_terminate_and_reach_dependent_pages(self) -> None:
        """A cycle and a self-reference do not stop change propagation."""
        previous = DependencyGraph(
            {
                "Template:A": _node(1, "Template:A", "Template:B"),
                "Template:B": _node(2, "Template:A"),
                "Page": _node(3, "Template:B"),
                "Unrelated page": _node(4),
            }
        )
        current = DependencyGraph(
            {**previous, "Template:A": _node(5, "Template:A", "Template:B")}
        )

        assert previous.invalidated_nodes(current) == {
            "Template:A",
            "Template:B",
            "Page",
        }

    def test_revision_change_for_missing_dependency_invalidates_dependents(
        self,
    ) -> None:
        """An existing graph node can gain a revision when its page appears."""
        previous = DependencyGraph(
            {
                "Template:Missing": _node(None),
                "Page": _node(1, "Template:Missing"),
            }
        )
        current = DependencyGraph({**previous, "Template:Missing": _node(2)})

        assert previous.invalidated_nodes(current) == {"Template:Missing", "Page"}


class TestGraphPersistence:
    """Tests for JSON graph storage and malformed cache data."""

    def test_json_round_trip_preserves_graph(self) -> None:
        """Serialized data retains revisions, missing pages, and dependency sets."""
        graph: DependencyGraph = DependencyGraph(
            {
                "Page": _node(1, "Template:Z", "Template:A"),
                "Template:Z": _node(None),
                "Template:A": _node(2),
            }
        )

        stored = graph.serialize()

        assert list(stored) == ["Page", "Template:A", "Template:Z"]
        assert stored["Page"]["dependencies"] == ["Template:A", "Template:Z"]
        assert DependencyGraph.deserialize(json.loads(json.dumps(stored))) == graph

    @pytest.mark.parametrize(
        "malformed",
        [
            None,
            [],
            {1: {"rev_id": 1, "dependencies": []}},
            {"": {"rev_id": 1, "dependencies": []}},
            {"Page": None},
            {"Page": {"dependencies": []}},
            {"Page": {"rev_id": 1}},
            {"Page": {"rev_id": "1", "dependencies": []}},
            {"Page": {"rev_id": True, "dependencies": []}},
            {"Page": {"rev_id": 1, "dependencies": "Template:A"}},
            {"Page": {"rev_id": 1, "dependencies": [1]}},
            {"Page": {"rev_id": 1, "dependencies": [""]}},
        ],
    )
    def test_malformed_data_rejects_entire_graph(self, malformed: Any) -> None:
        """Invalid cache data cannot preserve a partial dependency graph."""
        if isinstance(malformed, dict):
            malformed = {
                "Valid page": {"rev_id": 1, "dependencies": []},
                **malformed,
            }

        assert DependencyGraph.deserialize(malformed) == DependencyGraph()
