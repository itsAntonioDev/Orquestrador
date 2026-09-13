"""Testes da correspondência entre eventos e gatilhos ``on``."""

from __future__ import annotations

from typing import Any

import pytest

from orquestrador.pipeline import parse_pipeline_data
from orquestrador.projects import Provider
from orquestrador.webhooks import TriggerEvent, event_matches_triggers, matches_patterns


def event(kind: str = "push", **fields: Any) -> TriggerEvent:
    return TriggerEvent(
        provider=Provider.GITHUB, event=kind, repository="acme/api", commit="a" * 40, **fields
    )


def triggers(on: Any) -> Any:
    data: dict[str, Any] = {"name": "x", "jobs": {"j": {"steps": [{"run": "a"}]}}}
    if on is not None:
        data["on"] = on
    return parse_pipeline_data(data).triggers


@pytest.mark.parametrize(
    ("value", "patterns", "expected"),
    [
        ("main", ["main"], True),
        ("main", ["dev"], False),
        ("feature/x", ["feature/*"], True),
        ("feature/x/y", ["feature/*"], False),
        ("feature/x/y", ["feature/**"], True),
        ("v1", ["v?"], True),
        ("release/beta", ["release/**", "!release/beta"], False),
        ("release/1.0", ["release/**", "!release/beta"], True),
        ("main", ["!main", "main"], True),
        ("a.b", ["a.b"], True),
        ("axb", ["a.b"], False),
        (None, ["*"], False),
    ],
)
def test_matches_patterns(value: str | None, patterns: list[str], expected: bool) -> None:
    assert matches_patterns(value, patterns) is expected


@pytest.mark.parametrize(
    ("on", "incoming", "expected"),
    [
        (None, event(branch="qualquer"), True),
        (None, event("pull_request", base_branch="main"), True),
        ("push", event(branch="dev"), True),
        ("push", event(tag="v1"), True),
        ("push", event("pull_request", base_branch="main"), False),
        (["push", "pull_request"], event("pull_request", base_branch="x"), True),
        ({"push": {"branches": ["main"]}}, event(branch="main"), True),
        ({"push": {"branches": ["main"]}}, event(branch="dev"), False),
        ({"push": {"branches": ["main"]}}, event(tag="v1.0"), False),
        ({"push": {"tags": ["v*"]}}, event(tag="v1.0"), True),
        ({"push": {"tags": ["v*"]}}, event(tag="beta"), False),
        ({"push": {"tags": ["v*"]}}, event(branch="main"), False),
        ({"push": {"branches": ["main"], "tags": ["v*"]}}, event(tag="v2"), True),
        ({"push": {"branches": ["main"], "tags": ["v*"]}}, event(branch="main"), True),
        ({"pull_request": {"branches": ["main"]}}, event("pull_request", base_branch="main"), True),
        ({"pull_request": {"branches": ["main"]}}, event("pull_request", base_branch="dev"), False),
        ({"pull_request": {}}, event("pull_request", base_branch="dev"), True),
    ],
)
def test_event_matches_triggers(on: Any, incoming: TriggerEvent, expected: bool) -> None:
    assert event_matches_triggers(triggers(on), incoming) is expected
