"""Track revisions and dependencies shared by cached wiki pages."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Self


@dataclass(frozen=True)
class DependencyNode:
    """The revision and dependency titles for one wiki page."""

    rev_id: int | None
    dependencies: frozenset[str]


@dataclass
class DependencyGraph(MutableMapping[str, DependencyNode]):
    """Shared dependency nodes and operations on their revisions."""

    inner: dict[str, DependencyNode] = field(default_factory=dict)

    def __getitem__(self, title: str) -> DependencyNode:
        """Return the node for a wiki title."""
        return self.inner[title]

    def __setitem__(self, title: str, node: DependencyNode) -> None:
        """Store a node under its wiki title."""
        self.inner[title] = node

    def __delitem__(self, title: str) -> None:
        """Remove a node by its wiki title."""
        del self.inner[title]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the titles in the graph."""
        return iter(self.inner)

    def __len__(self) -> int:
        """Return the number of nodes in the graph."""
        return len(self.inner)

    def invalidated_nodes(self, current: DependencyGraph) -> set[str]:
        """Return changed nodes and their dependents across both graphs."""
        invalidated = {
            title
            for title in self.keys() | current.keys()
            if self.get(title) != current.get(title)
        }
        reverse_dependencies: dict[str, set[str]] = {}
        for graph in (self, current):
            for title, node in graph.items():
                for dependency in node.dependencies:
                    reverse_dependencies.setdefault(dependency, set()).add(title)

        pending = deque(invalidated)
        while pending:
            title = pending.popleft()
            for dependent in reverse_dependencies.get(title, ()):
                if dependent not in invalidated:
                    invalidated.add(dependent)
                    pending.append(dependent)

        return invalidated

    def serialize(self) -> dict[str, Any]:
        """Return a JSON-compatible graph with a stable title order."""
        return {
            title: {
                "rev_id": node.rev_id,
                "dependencies": sorted(node.dependencies),
            }
            for title, node in sorted(self.items())
        }

    @classmethod
    def deserialize(cls, data: Any) -> Self:
        """Load cached graph data, or return an empty graph if it is malformed."""
        if not isinstance(data, dict):
            return cls()

        graph = cls()
        for title, node in data.items():
            if not isinstance(title, str) or not title or not isinstance(node, dict):
                return cls()
            if "rev_id" not in node or "dependencies" not in node:
                return cls()

            rev_id = node["rev_id"]
            if rev_id is not None and (
                not isinstance(rev_id, int) or isinstance(rev_id, bool)
            ):
                return cls()
            dependencies = node["dependencies"]
            if not isinstance(dependencies, list) or not all(
                isinstance(dependency, str) and dependency
                for dependency in dependencies
            ):
                return cls()

            graph[title] = DependencyNode(rev_id, frozenset(dependencies))

        return graph
