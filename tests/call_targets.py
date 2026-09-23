"""Resolve each name a module reads to the fully qualified name it refers to, through imports.

``test_browser_safety.called_names`` reads the last segment of a call, which is
right for a deny-list of method names (``launch``, ``new_context``): any object
with that method is suspect. It is wrong for "does anything use *this*
function", which is what posture's enforcement scan asks (#162). By last
segment, an unrelated ``queue.consume()`` enforces the budget, and
``spend()`` after ``from netkeeper.services.budgets import consume as spend``
does not.

So this follows Python's own name binding, statically:

- ``import a.b.c`` binds ``a``; ``import a.b.c as x`` binds ``x`` to ``a.b.c``.
- ``from m import n as x`` binds ``x`` to ``m.n``; a relative ``m`` is resolved
  against the file's package.
- ``def f`` or ``class F`` at module level binds ``f`` to ``<module>.f``.
- Any other binding (an assignment, a parameter, a loop target, a nested
  ``def``) makes the name local and unknown.

A load of ``x.y.z`` -- called or not -- resolves to ``<what x is bound to>.y.z``,
looked up through the enclosing function scopes and then the module, the way
Python does (class bodies are not visible from their methods). A base that
cannot be resolved -- a local variable, a call result, a builtin -- yields
nothing. Within one scope the last binding in source order wins, so a local
``def consume`` shadows an earlier ``import consume`` for bare names, while
``budgets.consume`` in the same file still resolves to the real one.

Known limits. These point the safe way, toward reporting a protection as
unenforced: ``spend = budgets.consume`` then ``spend()``, ``getattr``, and a
re-export through another module's namespace are not followed. These point
the other way and are accepted as theoretical rather than handled: "last
binding wins" ignores control flow (an ``import`` in one branch of an
``if``/``else`` and a ``def`` in the other), a ``match`` capture pattern does
not rebind a name here, and a reference in dead code counts like any other.
Any load counts, not only a call or a hand-over, so ``x is consume``,
logging a handler, ``callable(consume)``, and a module-level annotation
naming it all read as a use.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

_Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
_Bindings = dict[str, str | None]


def module_name(path: Path, repo_root: Path) -> str:
    """``netkeeper/services/budgets.py`` -> ``netkeeper.services.budgets``."""
    parts = list(path.resolve().relative_to(repo_root.resolve()).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def qualified_references(source: str, module: str, *, is_package: bool = False) -> Iterator[str]:
    """The fully qualified target of every resolvable name ``source`` *reads*.

    Every load of a name or dotted attribute, not only a call's function
    position, so ``functools.partial(consume, ...)``, ``handler=consume`` and a
    class attribute holding it all count as using it -- the spellings a
    handler-injection design makes likely. An import on its own is not a use.
    ``module`` is the dotted name of the file itself; ``is_package`` says it is
    an ``__init__.py``, which changes what a relative import is relative to.
    """
    package = module if is_package else module.rpartition(".")[0]
    tree = ast.parse(source)
    resolver = _Resolver(module, package)
    resolver.collect(tree)
    yield from resolver.references(tree)


class _Resolver:
    def __init__(self, module: str, package: str) -> None:
        self.module = module
        self.package = package
        self.bindings: dict[ast.AST, _Bindings] = {}

    # --- pass 1: what each scope binds ---------------------------------------

    def collect(self, scope: _Scope) -> None:
        bound: _Bindings = {}
        self.bindings[scope] = bound
        if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            for arg in _arguments(scope.args):
                bound[arg] = None
        body: list[ast.AST] = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
        declared_global: set[str] = set()
        for node in _walk_scope(body):
            if isinstance(node, ast.Global | ast.Nonlocal):
                declared_global.update(node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname is not None:
                        bound[alias.asname] = alias.name
                    else:
                        head = alias.name.partition(".")[0]
                        bound[head] = head
            elif isinstance(node, ast.ImportFrom):
                base = self._absolute(node)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    target = None if base is None else f"{base}.{alias.name}"
                    bound[alias.asname or alias.name] = target
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                bound[node.name] = (
                    f"{self.module}.{node.name}" if isinstance(scope, ast.Module) else None
                )
                self.collect(node)
            elif isinstance(node, ast.Lambda):
                self.collect(node)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
                bound[node.id] = None
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound[node.name] = None
        for name in declared_global:
            bound.pop(name, None)

    def _absolute(self, node: ast.ImportFrom) -> str | None:
        if node.level == 0:
            return node.module
        parts = self.package.split(".") if self.package else []
        if node.level - 1 > len(parts):
            return None
        prefix = ".".join(parts[: len(parts) - (node.level - 1)])
        if node.module is None:
            return prefix or None
        return f"{prefix}.{node.module}" if prefix else node.module

    # --- pass 2: resolve every load against the scope chain it sits in -------

    def references(self, tree: ast.Module) -> Iterator[str]:
        yield from self._references_in(tree, chain=[tree])

    def _references_in(self, scope: _Scope, chain: list[_Scope]) -> Iterator[str]:
        body: list[ast.AST] = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
        for node in _walk_scope(body):
            if isinstance(node, ast.ClassDef):
                yield from self._references_in(node, [*chain, node])
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                # A class body is not visible from the methods defined in it.
                outer = [s for s in chain if not isinstance(s, ast.ClassDef)]
                yield from self._references_in(node, [*outer, node])
            elif isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
                target = self._resolve(node, chain)
                if target is not None:
                    yield target

    def _resolve(self, func: ast.expr, chain: list[_Scope]) -> str | None:
        attributes: list[str] = []
        while isinstance(func, ast.Attribute):
            attributes.append(func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        base = self._lookup(func.id, chain)
        if base is None:
            return None
        return ".".join([base, *reversed(attributes)])

    def _lookup(self, name: str, chain: list[_Scope]) -> str | None:
        innermost = chain[-1]
        for scope in reversed(chain):
            if isinstance(scope, ast.ClassDef) and scope is not innermost:
                continue
            bound = self.bindings[scope]
            if name in bound:
                return bound[name]
        return None


def _walk_scope(nodes: list[ast.AST]) -> Iterator[ast.AST]:
    """Every node in ``nodes``, in source order, not descending into nested scopes.

    A nested ``def``/``class``/``lambda`` node itself is yielded (it is a binding,
    and the caller recurses into it), but not its body.
    """
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            # Decorators, bases, and argument defaults run in *this* scope.
            outer: list[ast.AST] = [] if isinstance(node, ast.Lambda) else [*node.decorator_list]
            if isinstance(node, ast.ClassDef):
                outer.extend(node.bases)
                outer.extend(node.keywords)
            else:
                outer.extend(_argument_defaults(node.args))
            stack.extend(reversed(outer))
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _arguments(args: ast.arguments) -> Iterator[str]:
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        yield arg.arg
    if args.vararg is not None:
        yield args.vararg.arg
    if args.kwarg is not None:
        yield args.kwarg.arg


def _argument_defaults(args: ast.arguments) -> list[ast.AST]:
    return [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
