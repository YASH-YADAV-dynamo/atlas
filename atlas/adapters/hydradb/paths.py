"""Turning an `algo.SPpaths` response into plain, validated path data (R-PATH-1, R-PATH-2).

Two steps keep the Bolt driver out of the parser: `plain_path_rows` converts driver records
to dict/list data shaped like `tests/fixtures/sppaths_response.json`, and `parse_path_rows`
validates that data. Anything it cannot interpret raises `PathFormatError`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from atlas.contracts.errors import PathFormatError


@dataclass(frozen=True)
class RawPath:
    """A path as the engine returned it: node ids in path order and the engine's cost."""

    node_ids: tuple[int, ...]
    cost: float


def _int_id(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise PathFormatError("path node id is not an integer")


def _node_id(node: object) -> int:
    if isinstance(node, Mapping):
        if "id" not in node:
            raise PathFormatError("path node has no id")
        return _int_id(node["id"])
    return _int_id(node)


def _is_list(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def parse_path_rows(rows: object) -> tuple[RawPath, ...]:
    """Validate rows of `{"path": {"nodes": [...], "relationships": [...]}, "pathCost": n}`."""
    if not _is_list(rows):
        raise PathFormatError("path rows must be a list")
    parsed: list[RawPath] = []
    for row in rows:  # type: ignore[attr-defined]
        if not isinstance(row, Mapping) or "path" not in row or "pathCost" not in row:
            raise PathFormatError("row needs path and pathCost")
        path = row["path"]
        if not isinstance(path, Mapping):
            raise PathFormatError("path must be an object")
        nodes = path.get("nodes")
        if not _is_list(nodes) or not nodes:
            raise PathFormatError("path has no nodes")
        rels = path.get("relationships")
        if rels is not None and (not _is_list(rels) or len(rels) != len(nodes) - 1):
            raise PathFormatError("relationship count does not match node count")
        cost = row["pathCost"]
        if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost):
            raise PathFormatError("pathCost is not a finite number")
        ids = tuple(_node_id(node) for node in nodes)
        if len(set(ids)) != len(ids):
            raise PathFormatError("path revisits a node")
        parsed.append(RawPath(node_ids=ids, cost=float(cost)))
    return tuple(parsed)


def _driver_node_id(node: Any) -> int:  # noqa: ANN401
    """Id of a driver node. HydraDB keeps our id as the node's own id, not as a property:
    the element id is its decimal string (docs/architecture.md, engine facts)."""
    try:
        return _int_id(node.element_id)
    except AttributeError as exc:
        raise PathFormatError("path node carries no element id") from exc


def plain_path_rows(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert driver records (`path`, `pathCost`) to the plain shape `parse_path_rows` reads."""
    rows: list[dict[str, Any]] = []
    for record in records:
        try:
            path = record["path"]
            cost = record["pathCost"]
        except (KeyError, TypeError) as exc:
            raise PathFormatError("record lacks path or pathCost") from exc
        if isinstance(path, Mapping):  # already plain
            rows.append({"path": dict(path), "pathCost": cost})
            continue
        try:
            nodes = [{"id": _driver_node_id(n)} for n in path.nodes]
            rels = [
                {"src": _driver_node_id(r.start_node), "dst": _driver_node_id(r.end_node)}
                for r in path.relationships
            ]
        except AttributeError as exc:
            raise PathFormatError("path value is not a graph path") from exc
        rows.append({"path": {"nodes": nodes, "relationships": rels}, "pathCost": cost})
    return rows
