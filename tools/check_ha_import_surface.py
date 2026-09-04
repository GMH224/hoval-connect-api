"""Verify every `homeassistant.*` symbol imported by the integration exists in HA source.

Static (AST) check: we cannot import HA here (it needs Python 3.14), so we parse the
target module's AST and collect its top-level public names, including names it
re-exports via its own imports.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

HA_ROOT = Path(os.environ.get("HA_SOURCE_ROOT", "/tmp/ha_full"))


def module_path(mod: str) -> Path | None:
    rel = mod.replace(".", "/")
    for cand in (HA_ROOT / f"{rel}.py", HA_ROOT / rel / "__init__.py"):
        if cand.exists():
            return cand
    return None


_RE_DEF = re.compile(r"^(?:class|def|async def)\s+([A-Za-z_]\w*)", re.M)
_RE_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=", re.M)
_RE_FROM = re.compile(r"^\s*from\s+[\w.]+\s+import\s+\(?([^\n()]+|[^)]+)\)?", re.M)


def _names_by_regex(text: str) -> set[str]:
    """Fallback extractor for HA modules using syntax newer than our parser.

    HA 2026.9 uses PEP 696 type-parameter defaults (`class ConfigEntry[_DataT = Any]`)
    which Python 3.12's ast cannot parse. For an existence check, a lexical scan of
    top-level definitions, assignments and re-exports is sufficient.
    """
    names: set[str] = set(_RE_DEF.findall(text))
    names |= set(_RE_ASSIGN.findall(text))
    for chunk in _RE_FROM.findall(text):
        for part in chunk.split(","):
            part = part.strip()
            if not part:
                continue
            if " as " in part:
                part = part.split(" as ")[-1].strip()
            if part.isidentifier():
                names.add(part)
    return names


def exported_names(path: Path) -> set[str]:
    text = path.read_text()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return _names_by_regex(text)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            # names defined under TYPE_CHECKING / try-import blocks
            for sub in ast.walk(node):
                if isinstance(sub, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Name):
                            names.add(tgt.id)
                elif isinstance(sub, ast.ImportFrom):
                    for alias in sub.names:
                        names.add(alias.asname or alias.name)
    return names


def main(src_root: str) -> int:
    problems: list[str] = []
    checked = 0
    for py in sorted(Path(src_root).rglob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("homeassistant"):
                continue
            mp = module_path(node.module)
            if mp is None:
                problems.append(f"{py.name}: module not found: {node.module}")
                continue
            available = exported_names(mp)
            for alias in node.names:
                checked += 1
                if alias.name == "*":
                    continue
                if alias.name in available:
                    continue
                # may be a submodule, e.g. `from homeassistant.util import dt`
                if module_path(f"{node.module}.{alias.name}") is not None:
                    continue
                problems.append(f"{py.name}:{node.lineno}: {node.module}.{alias.name} NOT FOUND")

    print(f"checked {checked} imported symbols from homeassistant.*")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print("  " + p)
        return 1
    print("all imported symbols resolve against HA 2026.9.0")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
