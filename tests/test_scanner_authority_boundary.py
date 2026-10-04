"""Conservative static import boundary for the scanner integration.

This detects direct and transitive Python imports, including imports inside
functions. It is not a proof against dynamic loading, reflection, or a future
external adapter. Runtime contract and authorization tests remain necessary.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path


def _imports(source: str, module: str) -> set[str]:
    dependencies: set[str] = set()
    package = module.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level:
                parent = package.split(".")[: len(package.split(".")) - node.level + 1]
                prefix = ".".join((*parent, prefix)).rstrip(".")
            dependencies.add(prefix)
            dependencies.update(f"{prefix}.{alias.name}" for alias in node.names)
    return dependencies


def _forbidden_path(
    graph: dict[str, set[str]], start: str, forbidden: set[str]
) -> tuple[str, ...] | None:
    queue = deque([(start,)])
    visited: set[str] = set()
    while queue:
        path = queue.popleft()
        current = path[-1]
        if current in forbidden:
            return path
        if current in visited:
            continue
        visited.add(current)
        for dependency in sorted(graph.get(current, ())):
            queue.append((*path, dependency))
    return None


def test_scanner_imports_cannot_reach_control_or_dispatch_modules() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "scorpion"
    sources = {
        "scorpion." + ".".join(path.relative_to(source_root).with_suffix("").parts): path
        for path in source_root.rglob("*.py")
    }
    graph: dict[str, set[str]] = {}
    for module, path in sources.items():
        graph[module] = set()
        for dependency in _imports(path.read_text(encoding="utf-8"), module):
            while dependency.startswith("scorpion."):
                if dependency in sources:
                    graph[module].add(dependency)
                    break
                dependency = dependency.rsplit(".", 1)[0]

    forbidden = {
        "scorpion." + name
        for name in (
            "authorization", "broker", "delivery", "fastpath", "pipeline",
            "runtime", "store", "transactional", "release_guard",
            "_deployment_state_machine_core", "fail_safe_control", "execution_journal",
        )
    }
    roots = sorted(name for name in sources if name.startswith("scorpion.scanner_"))
    assert roots, "scanner integration sources were not found"
    # Importing any submodule also executes the package initializer.
    roots.append("scorpion.__init__")
    for module in roots:
        path = _forbidden_path(graph, module, forbidden)
        assert path is None, f"scanner authority boundary crossed: {' -> '.join(path or ())}"
    for module in sorted(forbidden & sources.keys()):
        path = _forbidden_path(graph, module, set(roots))
        assert path is None, f"control consumes scanner research: {' -> '.join(path or ())}"


def test_import_guard_detects_an_indirect_function_local_dispatch_import() -> None:
    scanner = "scorpion.scanner_context"
    helper = "scorpion.research_helper"
    broker = "scorpion.broker"
    graph = {
        scanner: _imports("from .research_helper import evaluate", scanner),
        helper: _imports("def evaluate():\n    from .broker import PaperBroker\n", helper),
    }
    assert _forbidden_path(graph, scanner, {broker}) == (scanner, helper, broker)
