from __future__ import annotations

import builtins
import symtable
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class UndefinedName:
    name: str
    scope: str
    lineno: int


def newly_undefined_names(base_source: str, current_source: str, filename: str) -> list[UndefinedName]:
    """Find unresolved global references introduced by a patch, including removed definitions."""

    base = set(_undefined_names(base_source, filename)) if base_source else set()
    current = set(_undefined_names(current_source, filename))
    return sorted(current - base)


def _undefined_names(source: str, filename: str) -> list[UndefinedName]:
    table = symtable.symtable(source, filename, "exec")
    module_defined = {
        name
        for name in table.get_identifiers()
        if _defines_name(table.lookup(name))
    }
    known = module_defined | set(dir(builtins)) | {"__file__", "__name__", "__package__"}
    issues: list[UndefinedName] = []

    def visit(scope: symtable.SymbolTable) -> None:
        for name in scope.get_identifiers():
            symbol = scope.lookup(name)
            if (
                symbol.is_referenced()
                and symbol.is_global()
                and not symbol.is_assigned()
                and name not in known
            ):
                issues.append(UndefinedName(name=name, scope=scope.get_name(), lineno=scope.get_lineno()))
        for child in scope.get_children():
            visit(child)

    for child in table.get_children():
        visit(child)
    return issues


def _defines_name(symbol: symtable.Symbol) -> bool:
    return symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
