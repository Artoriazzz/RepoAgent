from __future__ import annotations

import builtins
import symtable
from dataclasses import dataclass, field


@dataclass(frozen=True, order=True)
class UndefinedName:
    name: str
    scope: str
    # 行号仅用于展示；插入 import 不能把原有引用变成新错误。
    lineno: int = field(compare=False)


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

    def visit(scope: symtable.SymbolTable, parent: str = "") -> None:
        qualified_scope = f"{parent}.{scope.get_name()}" if parent else scope.get_name()
        for name in scope.get_identifiers():
            symbol = scope.lookup(name)
            if (
                symbol.is_referenced()
                and symbol.is_global()
                and not symbol.is_assigned()
                and name not in known
            ):
                issues.append(UndefinedName(name=name, scope=qualified_scope, lineno=scope.get_lineno()))
        for child in scope.get_children():
            visit(child, qualified_scope)

    for child in table.get_children():
        visit(child)
    return issues


def _defines_name(symbol: symtable.Symbol) -> bool:
    return symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
