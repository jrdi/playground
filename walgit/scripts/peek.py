"""Print the reference solution for one exercise.

    python scripts/peek.py 2        # everything in exercise 2
    python scripts/peek.py 2.3      # just that one function

Peeking is allowed. Peeking *before* you have a failing test in front of you
and a theory about why is how you end up having read a design instead of
understood one.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "reference"


def load_targets() -> dict[str, dict[str, str]]:
    spec = importlib.util.spec_from_file_location("_blank", REFERENCE / ".blank.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TARGETS


def source_of(path: Path, wanted: dict[str, str], prefix: str) -> list[tuple[str, str, str]]:
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    out = []

    def emit(node, qual):
        tag = wanted.get(qual)
        if tag is None or not tag.startswith(prefix):
            return
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1
        out.append((tag, qual, "".join(lines[start : node.end_lineno])))

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            emit(node, node.name)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    emit(child, f"{node.name}.{child.name}")
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    prefix = sys.argv[1].rstrip(".")
    targets = load_targets()

    found = []
    for filename, wanted in targets.items():
        found += [(filename, *item) for item in source_of(REFERENCE / filename, wanted, prefix)]

    if not found:
        print(f"nothing matches exercise {prefix!r}")
        return 1

    for filename, tag, qual, body in sorted(found, key=lambda f: f[1]):
        print(f"\n\033[1m--- {tag}  walgit/{filename}  {qual} ---\033[0m\n")
        print(body.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
