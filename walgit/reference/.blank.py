"""Turn the reference implementation into fill-in-the-blank exercises.

Keeps signatures, decorators and docstrings; replaces every body with a
NotImplementedError carrying the exercise number.
"""

import ast
import sys
from pathlib import Path

TARGETS = {
    "objects.py": {
        "ContentStore.digest_bytes": "1.1",
        "ContentStore.digest_file": "1.2",
        "ContentStore.key_for": "1.3",
        "ContentStore.exists": "1.4",
        "ContentStore.missing": "1.5",
        "ContentStore.get": "1.6",
        "ContentStore.download": "1.7",
        "ContentStore.put": "1.8",
        "ContentStore.put_file": "1.9",
    },
    "wal.py": {
        "_apply_updates": "2.1",
        "WalIndex.refs_at": "2.2",
        "WalIndex.apply_push": "2.3",
        "WalLog.load": "2.4",
        "WalLog.load_if_changed": "2.5",
        "WalLog.commit": "2.6",
        "WalLog.append": "2.7",
    },
    "compact.py": {
        "should_compact": "3.1",
        "plan_compaction": "3.2",
        "apply_compaction": "3.3",
        "_repack_snapshot": "3.4",
        "compact": "3.5",
        "unreferenced_packs": "3.6",
        "gc": "3.7",
    },
    "repo.py": {
        "LocalRepo.sync": "4.1",
        "LocalRepo._fetch_packs": "4.2",
        "LocalRepo._prune_packs": "4.3",
        "materialize_snapshot": "4.4",
    },
    "hooks.py": {
        "parse_updates": "4.5",
        "find_quarantine_pack": "4.6",
        "pre_receive": "4.7",
    },
}


def functions(tree):
    """Yield (node, qualified_name) for every function, one level of nesting."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, node.name
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child, f"{node.name}.{child.name}"


def blank_file(path: Path, targets: dict[str, str]) -> int:
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    edits = []
    for node, qual in functions(tree):
        tag = targets.get(qual)
        if tag is None:
            continue
        body = node.body
        start = 0
        if (
            isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            start = 1
        if start >= len(body):
            continue
        first = body[start]
        indent = " " * first.col_offset
        edits.append(
            (
                first.lineno - 1,
                node.end_lineno,
                f'{indent}raise NotImplementedError(\n'
                f'{indent}    "EXERCISE {tag}: {qual} -- implement me (see the docstring above)"\n'
                f'{indent})\n',
            )
        )

    for start_line, end_line, replacement in sorted(edits, reverse=True):
        lines[start_line:end_line] = [replacement]

    path.write_text("".join(lines))
    return len(edits)


def main() -> int:
    pkg = Path(sys.argv[1])
    total = 0
    for name, targets in TARGETS.items():
        count = blank_file(pkg / name, targets)
        print(f"{name}: blanked {count}/{len(targets)}")
        if count != len(targets):
            print(f"  !! some targets in {name} were not found", file=sys.stderr)
            return 1
        total += count
    print(f"total blanks: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
