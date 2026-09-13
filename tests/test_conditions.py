"""Testes do avaliador de expressões ``if``."""

from __future__ import annotations

from typing import Any

import pytest

from orquestrador.pipeline import ConditionContext, ConditionError, evaluate_condition

VARIABLES: dict[str, Any] = {
    "branch": "main",
    "event": "push",
    "commit": "abc123",
    "tag": None,
}


def ctx(*, failed: bool = False, cancelled: bool = False) -> ConditionContext:
    return ConditionContext(
        variables=VARIABLES, env={"DEPLOY": "true"}, failed=failed, cancelled=cancelled
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("branch == 'main'", True),
        ("branch != 'main'", False),
        ("event in ['push', 'tag']", True),
        ("event not in ('push',)", False),
        ("branch == 'main' && event == 'push'", True),
        ("branch == 'dev' || event == 'push'", True),
        ("!(branch == 'dev')", True),
        ("branch != 'x' and not false", True),
        ("env.DEPLOY == 'true'", True),
        ("env['DEPLOY'] == 'true'", True),
        ("env.MISSING == null", True),
        ("tag == None", True),
        ("startsWith(branch, 'ma')", True),
        ("endsWith(branch, 'in')", True),
        ("contains(commit, 'c12')", True),
        ("contains(['a', 'b'], 'b')", True),
        ("matches(branch, 'release/*')", False),
        ("matches('release/1.0', 'release/*')", True),
        ("${{ branch == 'main' }}", True),
        ("true", True),
        ("false", False),
        ("1 < 2 <= 2", True),
        ("-1 < 0", True),
        ("commit[0] == 'a'", True),
        ("'a!b' == \"a!b\"", True),
        ("'a&&b' == 'a&&b'", True),
    ],
)
def test_expressions(expression: str, expected: bool) -> None:
    assert evaluate_condition(expression, ctx()) is expected


@pytest.mark.parametrize("expression", [None, "", "   "])
def test_empty_condition_means_success(expression: str | None) -> None:
    assert evaluate_condition(expression, ctx()) is True
    assert evaluate_condition(expression, ctx(failed=True)) is False


def test_status_functions() -> None:
    assert evaluate_condition("always()", ctx(failed=True)) is True
    assert evaluate_condition("failure()", ctx(failed=True)) is True
    assert evaluate_condition("failure()", ctx()) is False
    assert evaluate_condition("success()", ctx(failed=True)) is False
    assert evaluate_condition("cancelled()", ctx(cancelled=True)) is True
    assert evaluate_condition("cancelled()", ctx()) is False


def test_expression_without_status_function_implies_success() -> None:
    assert evaluate_condition("branch == 'main'", ctx(failed=True)) is False
    assert evaluate_condition("branch == 'main'", ctx(cancelled=True)) is False


def test_status_function_combined_with_expression() -> None:
    assert evaluate_condition("failure() && branch == 'main'", ctx(failed=True)) is True
    assert evaluate_condition("always() && branch == 'dev'", ctx(failed=True)) is False


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hacked')",
        "branch.upper()",
        "(lambda: 1)()",
        "unknown_variable == 1",
        "[x for x in 'ab']",
        "branch ==",
        "1 +",
        "1 + 2",
        "env.__class__",
        "open('arquivo')",
        "'a' if true else 'b'",
        "branch.startswith",
        "contains(haystack='a', needle='b')",
        "contains(*['a', 'b'])",
        "b'bytes' == 1",
        "branch < 1",
        "startsWith('a')",
        "-'texto'",
    ],
)
def test_rejected_expressions(expression: str) -> None:
    with pytest.raises(ConditionError):
        evaluate_condition(expression, ctx())


def test_subscript_on_list_and_missing_index() -> None:
    context = ConditionContext(variables={"items": ["a", "b"]})
    assert evaluate_condition("items[1] == 'b'", context) is True
    assert evaluate_condition("items[5] == null", context) is True
