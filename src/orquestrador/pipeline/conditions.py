"""Avaliação segura das expressões ``if`` de jobs e steps.

As expressões usam uma sintaxe próxima de Python, com alguns açúcares do
GitHub Actions (``&&``, ``||``, ``!`` e o invólucro ``${{ ... }}``)::

    if: branch == 'main' && event == 'push'
    if: ${{ startsWith(tag, 'v') }}
    if: always()

A expressão é convertida em AST e interpretada por um avaliador que só
aceita um subconjunto fechado de nós — **nunca** usamos ``eval``. Assim um
pipeline malicioso não consegue executar código Python no orquestrador.

Semântica de status (igual ao GitHub Actions): se a expressão não chama
nenhuma função de status (``success()``, ``failure()``, ``always()``,
``cancelled()``), ela é implicitamente combinada com ``success()``.
"""

from __future__ import annotations

import ast
import fnmatch
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


class ConditionError(ValueError):
    """Expressão de condição inválida ou não permitida."""


@dataclass(frozen=True)
class ConditionContext:
    """Dados disponíveis para uma expressão de condição.

    Attributes:
        variables: Variáveis nomeadas (``branch``, ``event``, ``commit``...).
        env: Variáveis de ambiente, acessíveis como ``env.NOME``.
        failed: Se algo anterior falhou (step anterior ou job dependência).
        cancelled: Se a execução foi cancelada.
    """

    variables: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    failed: bool = False
    cancelled: bool = False


STATUS_FUNCTIONS = frozenset({"success", "failure", "always", "cancelled"})

_WRAPPER = re.compile(r"^\s*\$\{\{(?P<body>.*)\}\}\s*$", re.DOTALL)

_LITERAL_NAMES: dict[str, Any] = {"true": True, "false": False, "null": None, "None": None}

_COMPARATORS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


_HELPERS: dict[str, Callable[..., Any]] = {
    "contains": lambda haystack, needle: (
        _as_text(needle) in (haystack if isinstance(haystack, list | tuple) else _as_text(haystack))
    ),
    "startsWith": lambda value, prefix: _as_text(value).startswith(_as_text(prefix)),
    "endsWith": lambda value, suffix: _as_text(value).endswith(_as_text(suffix)),
    "matches": lambda value, pattern: fnmatch.fnmatchcase(_as_text(value), _as_text(pattern)),
}


def _translate_operators(expression: str) -> str:
    """Troca ``&&``, ``||`` e ``!`` por ``and``, ``or`` e ``not`` fora de strings.

    Args:
        expression: Expressão original.

    Returns:
        Expressão com operadores no estilo Python.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(expression):
        char = expression[i]
        if quote:
            out.append(char)
            if char == "\\" and i + 1 < len(expression):
                out.append(expression[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
        elif expression.startswith("&&", i):
            out.append(" and ")
            i += 1
        elif expression.startswith("||", i):
            out.append(" or ")
            i += 1
        elif char == "!" and not expression.startswith("!=", i):
            out.append(" not ")
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _uses_status_function(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in STATUS_FUNCTIONS
        for node in ast.walk(tree)
    )


class _Evaluator:
    """Interpretador de um subconjunto seguro da AST do Python."""

    def __init__(self, context: ConditionContext) -> None:
        self.context = context
        self.functions: dict[str, Callable[..., Any]] = {
            "success": lambda: not context.failed and not context.cancelled,
            "failure": lambda: context.failed,
            "always": lambda: True,
            "cancelled": lambda: context.cancelled,
            **_HELPERS,
        }

    def evaluate(self, node: ast.AST) -> Any:
        """Avalia um nó da AST.

        Args:
            node: Nó a avaliar.

        Returns:
            O valor resultante.

        Raises:
            ConditionError: Se o nó não for permitido ou a avaliação falhar.
        """
        handler = getattr(self, f"_eval_{type(node).__name__}", None)
        if handler is None:
            raise ConditionError(f"construção não permitida: {type(node).__name__}")
        return handler(node)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        if node.value is not None and not isinstance(node.value, str | int | float | bool):
            raise ConditionError(f"literal não permitido: {node.value!r}")
        return node.value

    def _eval_Name(self, node: ast.Name) -> Any:
        if node.id in _LITERAL_NAMES:
            return _LITERAL_NAMES[node.id]
        if node.id == "env":
            return self.context.env
        if node.id in self.context.variables:
            return self.context.variables[node.id]
        raise ConditionError(f"variável desconhecida: {node.id!r}")

    def _eval_Attribute(self, node: ast.Attribute) -> Any:
        if node.attr.startswith("_"):
            raise ConditionError(f"atributo não permitido: {node.attr!r}")
        target = self.evaluate(node.value)
        if not isinstance(target, Mapping):
            raise ConditionError(f"acesso a atributo só é permitido em mapeamentos: .{node.attr}")
        return target.get(node.attr)

    def _eval_Subscript(self, node: ast.Subscript) -> Any:
        target = self.evaluate(node.value)
        key = self.evaluate(node.slice)
        if isinstance(target, Mapping):
            return target.get(key)
        if isinstance(target, list | tuple | str) and isinstance(key, int):
            try:
                return target[key]
            except IndexError:
                return None
        raise ConditionError("indexação não suportada para este valor")

    def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.evaluate(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self.evaluate(value)
            if result:
                return result
        return result

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        if isinstance(node.op, ast.Not):
            return not self.evaluate(node.operand)
        if isinstance(node.op, ast.USub):
            operand = self.evaluate(node.operand)
            if isinstance(operand, int | float) and not isinstance(operand, bool):
                return -operand
        raise ConditionError("operador unário não permitido")

    def _eval_Compare(self, node: ast.Compare) -> bool:
        left = self.evaluate(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.evaluate(comparator)
            func = _COMPARATORS.get(type(op))
            if func is None:
                raise ConditionError(f"operador não permitido: {type(op).__name__}")
            try:
                ok = func(left, right)
            except TypeError as exc:
                raise ConditionError(f"comparação inválida: {exc}") from exc
            if not ok:
                return False
            left = right
        return True

    def _eval_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name) or node.func.id not in self.functions:
            raise ConditionError("apenas funções embutidas podem ser chamadas")
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise ConditionError("argumentos nomeados ou desempacotados não são permitidos")
        args = [self.evaluate(arg) for arg in node.args]
        try:
            return self.functions[node.func.id](*args)
        except TypeError as exc:
            raise ConditionError(f"chamada inválida de {node.func.id}(): {exc}") from exc

    def _eval_List(self, node: ast.List) -> list[Any]:
        return [self.evaluate(element) for element in node.elts]

    def _eval_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.evaluate(element) for element in node.elts)


def evaluate_condition(expression: str | None, context: ConditionContext) -> bool:
    """Avalia uma expressão ``if``.

    Args:
        expression: A expressão; ``None`` ou vazia equivale a ``success()``.
        context: Variáveis e estado disponíveis para a expressão.

    Returns:
        ``True`` se o job/step deve ser executado.

    Raises:
        ConditionError: Se a expressão for sintaticamente inválida ou usar
            construções não permitidas.
    """
    if expression is None or not expression.strip():
        expression = "success()"
    wrapped = _WRAPPER.match(expression)
    if wrapped:
        expression = wrapped.group("body")
    source = _translate_operators(expression).strip()
    if not source:
        raise ConditionError("expressão vazia")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"sintaxe inválida em {expression.strip()!r}") from exc

    value = bool(_Evaluator(context).evaluate(tree.body))
    if not _uses_status_function(tree):
        return value and not context.failed and not context.cancelled
    return value
